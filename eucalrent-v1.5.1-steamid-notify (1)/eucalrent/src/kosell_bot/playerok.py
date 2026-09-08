from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from .models import PaidOrder, PlayerokItem

logger = logging.getLogger(__name__)

_SHORT_ITEM_ID_RE = re.compile(r"(?i)^([0-9a-f]{12})(?:-|$)")
_FULL_ITEM_ID_RE = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-([0-9a-f]{12})$"
)


def short_item_id(item_id: str | None, slug: str | None) -> str | None:
    """Returns the 12-char Playerok ID shown at the start of an item URL slug."""
    for value in (slug, item_id):
        candidate = str(value or "").strip().split("?", 1)[0].strip("/ ")
        if not candidate:
            continue
        candidate = candidate.rsplit("/", 1)[-1]
        match = _SHORT_ITEM_ID_RE.match(candidate)
        if match:
            return match.group(1).lower()
        match = _FULL_ITEM_ID_RE.fullmatch(candidate)
        if match:
            return match.group(1).lower()
    return None


class PlayerokUnavailable(RuntimeError):
    pass


class PlayerokGateway:
    def __init__(
        self,
        *,
        cookies: str,
        user_agent: str,
        proxy: str | None,
        event_handler: Callable[[object], Awaitable[None]],
        state_handler: Callable[[bool, str | None], Awaitable[None]] | None = None,
    ):
        self.cookies = cookies
        self.user_agent = user_agent
        self.proxy = proxy
        self.event_handler = event_handler
        self.state_handler = state_handler
        self.account = None
        self.connected = False
        self.last_error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._call_lock = threading.RLock()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._thread = threading.Thread(
            target=self._run, name="playerok-listener", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _notify_state(self, connected: bool, error: str | None) -> None:
        if not self.state_handler or not self._loop:
            return
        asyncio.run_coroutine_threadsafe(
            self.state_handler(connected, error), self._loop
        )

    def _connect(self):
        from playerokapi.account import Account

        kwargs = {
            "cookies": self.cookies,
            "user_agent": self.user_agent,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return Account(**kwargs).get()

    def _run(self) -> None:
        from playerokapi.listener.listener import EventListener

        while not self._stop.is_set():
            try:
                self.account = self._connect()
                self.connected = True
                self.last_error = None
                logger.info("Playerok подключён")
                self._notify_state(True, None)

                listener = EventListener(self.account)
                for event in listener.listen(get_new_review_events=False):
                    if self._stop.is_set():
                        return
                    if self._loop:
                        asyncio.run_coroutine_threadsafe(
                            self.event_handler(event), self._loop
                        )
            except Exception as exc:
                was_connected = self.connected
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Ошибка Playerok, повтор подключения через 10 секунд")
                if was_connected or self.account is None:
                    self._notify_state(False, self.last_error)
                self._stop.wait(10)

    def _require_account(self):
        if self.account is None or not self.connected:
            raise PlayerokUnavailable(self.last_error or "Playerok ещё не подключён")
        return self.account

    async def resolve_item(self, reference: str) -> PlayerokItem:
        return await asyncio.to_thread(self._resolve_item_sync, reference)

    async def list_active_items(self) -> list[PlayerokItem]:
        return await asyncio.to_thread(self._list_active_items_sync)

    @staticmethod
    def _extract_reference(reference: str) -> str:
        value = reference.strip()
        if value.startswith("http://") or value.startswith("https://"):
            parsed = urlparse(value)
            parts = [part for part in parsed.path.split("/") if part]
            if not parts:
                raise ValueError("В ссылке Playerok не найден ID лота")
            value = parts[-1]
        value = value.split("?")[0].strip("/ ")
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,100}", value):
            raise ValueError("Неверная ссылка или ID лота Playerok")
        return value

    def _resolve_item_sync(self, reference: str) -> PlayerokItem:
        account = self._require_account()
        value = self._extract_reference(reference)
        with self._call_lock:
            try:
                item = account.get_item(slug=value)
            except Exception:
                item = account.get_item(id=value)
        if item is None:
            raise ValueError("Лот Playerok не найден")
        return PlayerokItem(
            id=str(item.id),
            slug=str(getattr(item, "slug", "") or "") or None,
            name=str(getattr(item, "name", "Без названия")),
        )

    def _list_active_items_sync(self) -> list[PlayerokItem]:
        account = self._require_account()
        result: list[PlayerokItem] = []
        cursor: str | None = None

        with self._call_lock:
            for _ in range(100):
                page = account.get_my_items(count=24, after_cursor=cursor)
                for item in getattr(page, "items", []) or []:
                    result.append(
                        PlayerokItem(
                            id=str(item.id),
                            slug=str(getattr(item, "slug", "") or "") or None,
                            name=str(getattr(item, "name", "Без названия")),
                        )
                    )
                page_info = getattr(page, "page_info", None)
                if not page_info or not getattr(page_info, "has_next_page", False):
                    break
                next_cursor = str(getattr(page_info, "end_cursor", "") or "")
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
        return result

    async def send_message(self, chat_id: str, text: str) -> None:
        await asyncio.to_thread(self._send_message_sync, chat_id, text)

    def _send_message_sync(self, chat_id: str, text: str) -> None:
        account = self._require_account()
        with self._call_lock:
            account.send_message(chat_id=chat_id, text=text, mark_chat_as_read=True)

    async def mark_sent(self, deal_id: str) -> None:
        await asyncio.to_thread(self._mark_sent_sync, deal_id)

    def _mark_sent_sync(self, deal_id: str) -> None:
        from playerokapi.enums import ItemDealStatuses

        account = self._require_account()
        with self._call_lock:
            account.update_deal(deal_id, ItemDealStatuses.SENT)

    async def check_connection(self) -> str:
        return await asyncio.to_thread(self._check_connection_sync)

    def _check_connection_sync(self) -> str:
        account = self._connect()
        return str(getattr(account, "username", None) or getattr(account, "id", "подключён"))

    @staticmethod
    def paid_order_from_event(event: object) -> PaidOrder:
        deal = getattr(event, "deal")
        chat = getattr(event, "chat")
        item = getattr(deal, "item")
        user = getattr(deal, "user", None)
        return PaidOrder(
            deal_id=str(deal.id),
            item_id=str(item.id),
            chat_id=str(chat.id),
            buyer_id=str(getattr(user, "id", "") or "") or None,
            buyer_username=str(getattr(user, "username", "") or "") or None,
            item_slug=str(getattr(item, "slug", "") or "") or None,
            item_name=str(getattr(item, "name", "") or "") or None,
        )

    @staticmethod
    def event_name(event: object) -> str:
        event_type = getattr(event, "type", None)
        return str(getattr(event_type, "name", ""))

    @staticmethod
    def is_buyer_message(event: object, own_account_id: str | None) -> bool:
        message = getattr(event, "message", None)
        user = getattr(message, "user", None)
        user_id = str(getattr(user, "id", "") or "")
        return bool(message and user_id and user_id != str(own_account_id or ""))

    @property
    def account_id(self) -> str | None:
        return str(getattr(self.account, "id", "") or "") or None
