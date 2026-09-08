from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .config import Config
from .db import Database, utc_now
from .kosell import Balance, KosellClient, KosellError
from .messages import credentials_message, owner_order_text, steam_code_message
from .models import Lot, Order, PaidOrder, PlayerokItem
from .playerok import PlayerokGateway, PlayerokUnavailable, short_item_id
from .steamsmm import SteamSmmClient

logger = logging.getLogger(__name__)


def is_steam_code_command(text: str) -> bool:
    """Return True only for the exact buyer command used to request Steam Guard."""
    return text.strip().casefold() == "!код"


_STEAM_PROFILE_ID_RE = re.compile(
    r"(?:https?://)?(?:www\.)?steamcommunity\.com/profiles/(7\d{16})\b",
    re.IGNORECASE,
)
_STEAM_ID64_RE = re.compile(r"\b(7656119\d{10})\b")


def extract_steam_id64(text: str) -> str | None:
    """Extract SteamID64 from a Playerok chat message.

    Supports a raw 17-digit SteamID64 and a steamcommunity.com/profiles/<id> URL.
    Vanity URLs are intentionally ignored because they require external resolution.
    """
    text = text or ""
    match = _STEAM_PROFILE_ID_RE.search(text)
    if match:
        return match.group(1)
    match = _STEAM_ID64_RE.search(text)
    return match.group(1) if match else None


class RentalService:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.database_path)
        self.db.ensure_defaults(config.bot_enabled_default, config.low_balance_rub)
        self.kosell = KosellClient(config.kosell_api_key, config.kosell_base_url)
        self.steamsmm = SteamSmmClient(config.steamsmm_api_token, config.steamsmm_base_url) if config.steamsmm_api_token else None
        self.bot = Bot(
            config.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dispatcher = Dispatcher()
        self.playerok = PlayerokGateway(
            cookies=config.playerok_cookies,
            user_agent=config.playerok_user_agent,
            proxy=config.playerok_proxy,
            event_handler=self.handle_playerok_event,
            state_handler=self.handle_playerok_state,
        )
        self.last_balance: Balance | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._order_locks: dict[str, asyncio.Lock] = {}
        self._code_locks: dict[str, asyncio.Lock] = {}
        self._code_last_at: dict[str, float] = {}
        self._relist_lock = asyncio.Lock()
        self._last_playerok_state: bool | None = None
        self._stopping = False

    async def run(self) -> None:
        from .telegram_ui import TelegramUI

        TelegramUI(self).register(self.dispatcher)
        loop = asyncio.get_running_loop()
        self.playerok.start(loop)
        self._tasks = [
            asyncio.create_task(self._retry_loop(), name="order-retry"),
            asyncio.create_task(self._expiry_loop(), name="rental-expiry"),
            asyncio.create_task(self._balance_loop(), name="balance-check"),
        ]
        if self.config.auto_relist_sync:
            self._tasks.append(
                asyncio.create_task(self._relist_loop(), name="playerok-relist-sync")
            )

        try:
            await self.dispatcher.start_polling(
                self.bot,
                allowed_updates=self.dispatcher.resolve_used_update_types(),
            )
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self.playerok.stop()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.kosell.close()
        if self.steamsmm:
            await self.steamsmm.close()
        await self.bot.session.close()
        self.db.close()

    async def notify_owner(self, text: str) -> None:
        try:
            await self.bot.send_message(self.config.telegram_owner_id, text)
        except Exception:
            logger.exception("Не удалось отправить уведомление владельцу")

    async def handle_playerok_state(self, connected: bool, error: str | None) -> None:
        previous = self._last_playerok_state
        self._last_playerok_state = connected
        if connected and previous is False:
            await self.notify_owner("✅ Playerok снова подключён.")
        elif not connected and previous is not False:
            safe_error = html.escape((error or "неизвестная ошибка")[:800])
            await self.notify_owner(
                "⚠️ Playerok отключён. Бот повторяет подключение.\n"
                f"Ошибка: <code>{safe_error}</code>"
            )

    async def handle_playerok_event(self, event: object) -> None:
        try:
            name = self.playerok.event_name(event)
            if name == "NEW_DEAL":
                await self.handle_paid_order(self.playerok.paid_order_from_event(event))
                return
            if name == "DEAL_ROLLED_BACK":
                deal = getattr(event, "deal")
                await self.handle_refund(str(deal.id))
                return
            if name == "NEW_MESSAGE" and self.playerok.is_buyer_message(
                event, self.playerok.account_id
            ):
                message = getattr(event, "message")
                text = str(getattr(message, "text", "") or "")
                chat = getattr(event, "chat")
                chat_id = str(chat.id)
                steam_id64 = extract_steam_id64(text)
                if steam_id64:
                    await self.handle_steam_id_message(chat_id, steam_id64)
                if is_steam_code_command(text):
                    await self.handle_steam_code(chat_id)
        except Exception:
            logger.exception("Необработанная ошибка события Playerok")

    async def handle_paid_order(self, paid: PaidOrder) -> None:
        lot = self.db.get_lot_by_item(paid.item_id)
        if lot is None:
            lot = await self._bind_relisted_paid_item(paid)
        if lot is None:
            logger.info("Оплаченная сделка %s не относится к лотам KOSell", paid.deal_id)
            return
        if not lot.enabled:
            await self.notify_owner(
                f"⚠️ Оплачен выключенный лот\nЗаказ: <code>{html.escape(paid.deal_id)}</code>\n"
                f"Лот: {html.escape(lot.playerok_name)}"
            )
            return

        paused = not self.db.is_enabled()
        order, created = self.db.create_order(paid, lot, paused=paused)
        if not created and order.status in {"delivered", "terminated", "refunded", "failed"}:
            return

        if created:
            try:
                await self.playerok.send_message(
                    order.chat_id,
                    "✅ Оплата получена. Подготавливаем аккаунт Steam. Обычно это занимает до 1 минуты.",
                )
            except Exception:
                logger.exception("Не удалось отправить подтверждение оплаты")

        if paused:
            await self.notify_owner(
                owner_order_text(order, "⏸ Получен заказ, но автовыдача выключена")
            )
            try:
                await self.playerok.send_message(
                    order.chat_id,
                    "⚠️ Автовыдача временно приостановлена. Продавец уже получил уведомление о заказе.",
                )
            except Exception:
                logger.exception("Не удалось предупредить покупателя о паузе")
            return

        if created:
            try:
                cfg = json.loads(order.service_config or "{}")
                if order.provider == "steamsmm" and order.service_type == "commend_cs2" and not str(cfg.get("target") or "").strip():
                    await self.notify_owner(
                        owner_order_text(order, "🛒 НОВАЯ ПРОДАЖА — ожидается Steam ID")
                    )
                else:
                    await self.notify_owner(
                        owner_order_text(order, "🛒 НОВАЯ ПРОДАЖА")
                    )
            except Exception:
                logger.exception("Не удалось отправить уведомление о новой продаже %s", order.deal_id)

        await self.process_order(order.deal_id)

    @staticmethod
    def _title_key(value: str) -> str:
        value = value.casefold().replace("ё", "е")
        return re.sub(r"[^a-zа-я0-9]+", " ", value).strip()

    def _matching_lots(self, item: PlayerokItem) -> list[Lot]:
        key = self._title_key(item.name)
        if not key:
            return []
        return [
            lot
            for lot in self.db.list_lots()
            if lot.enabled
            and lot.playerok_item_id != item.id
            and self._title_key(lot.playerok_name) == key
        ]

    def _short_id_matching_lots(self, item: PlayerokItem) -> list[Lot]:
        """Matches the short ID that Playerok puts at the beginning of the slug."""
        item_key = short_item_id(item.id, item.slug)
        if not item_key:
            return []
        return [
            lot
            for lot in self.db.list_lots()
            if lot.playerok_item_id != item.id
            and short_item_id(lot.playerok_item_id, lot.playerok_slug) == item_key
        ]

    async def _bind_relisted_paid_item(self, paid: PaidOrder) -> Lot | None:
        item = PlayerokItem(paid.item_id, paid.item_slug, paid.item_name or "")
        matches = self._short_id_matching_lots(item)
        matched_by = "короткому ID в начале ссылки"
        if not matches and item.name:
            matches = self._matching_lots(item)
            matched_by = "точному названию лота"
        if len(matches) != 1:
            if len(matches) > 1:
                await self.notify_owner(
                    "⚠️ <b>eucalrent не выбрал лот автоматически</b>\n"
                    f"Playerok: {html.escape(item.name)}\n"
                    f"Новый ID: <code>{html.escape(item.id)}</code>\n"
                    "Найдено несколько одинаковых сопоставлений. Добавьте новый ID вручную."
                )
            return None

        updated, old_item_id = self.db.rebind_lot_item(matches[0].id, item)
        await self.notify_owner(
            "🔁 <b>eucalrent нашёл перевыставленный лот</b>\n"
            f"{html.escape(updated.playerok_name)}\n"
            f"Совпадение по: {matched_by}\n"
            f"Старый ID: <code>{html.escape(old_item_id)}</code>\n"
            f"Новый ID: <code>{html.escape(updated.playerok_item_id)}</code>\n"
            "Заказ продолжает обрабатываться автоматически."
        )
        return updated

    async def sync_relisted_lots(self, *, notify: bool = True) -> list[tuple[Lot, str]]:
        """Finds active relisted items whose previous mapped ID is no longer active."""
        async with self._relist_lock:
            active_items = await self.playerok.list_active_items()
            active_ids = {item.id for item in active_items}
            stale_lots = [
                lot
                for lot in self.db.list_lots()
                if lot.enabled and lot.playerok_item_id not in active_ids
            ]
            unknown_items = [
                item
                for item in active_items
                if self.db.get_lot_by_item(item.id) is None
            ]

            rebound: list[tuple[Lot, str]] = []
            used_item_ids: set[str] = set()
            used_lot_ids: set[int] = set()

            # Playerok's short 12-character ID is placed at the beginning of
            # the slug. This is the same fast matching method used by the old
            # boosts bot and is preferred over title matching.
            for lot in stale_lots:
                lot_key = short_item_id(lot.playerok_item_id, lot.playerok_slug)
                if not lot_key:
                    continue
                items = [
                    item
                    for item in unknown_items
                    if item.id not in used_item_ids
                    and short_item_id(item.id, item.slug) == lot_key
                ]
                if len(items) != 1:
                    continue
                updated, old_item_id = self.db.rebind_lot_item(lot.id, items[0])
                rebound.append((updated, old_item_id))
                used_lot_ids.add(lot.id)
                used_item_ids.add(items[0].id)

            # A real "Sell again" can also generate a completely new short
            # ID. In that case only rebind a single stale lot to a single new
            # active item with the exact same normalized title.
            remaining_lots = [lot for lot in stale_lots if lot.id not in used_lot_ids]
            remaining_items = [
                item for item in unknown_items if item.id not in used_item_ids
            ]
            stale_by_name: dict[str, list[Lot]] = {}
            for lot in remaining_lots:
                stale_by_name.setdefault(self._title_key(lot.playerok_name), []).append(lot)
            unknown_by_name: dict[str, list[PlayerokItem]] = {}
            for item in remaining_items:
                unknown_by_name.setdefault(self._title_key(item.name), []).append(item)

            for key, lots in stale_by_name.items():
                items = unknown_by_name.get(key, [])
                if not key or len(lots) != 1 or len(items) != 1:
                    continue
                updated, old_item_id = self.db.rebind_lot_item(lots[0].id, items[0])
                rebound.append((updated, old_item_id))

            if notify and rebound:
                lines = ["🔁 <b>eucalrent обновил ID перевыставленных лотов</b>", ""]
                for lot, old_item_id in rebound:
                    lines.append(
                        f"• {html.escape(lot.playerok_name)}\n"
                        f"  <code>{html.escape(old_item_id)}</code> → "
                        f"<code>{html.escape(lot.playerok_item_id)}</code>"
                    )
                await self.notify_owner("\n\n".join(lines))
            return rebound

    def _lock_for_order(self, deal_id: str) -> asyncio.Lock:
        return self._order_locks.setdefault(deal_id, asyncio.Lock())

    @staticmethod
    def _error_retryable(exc: Exception) -> bool:
        if isinstance(exc, (PlayerokUnavailable, TimeoutError, ConnectionError)):
            return True
        if not isinstance(exc, KosellError):
            return True
        if exc.retryable:
            return True
        text = f"{exc.error_code or ''} {exc}".lower()
        retry_words = (
            "balance",
            "insufficient",
            "stock",
            "available account",
            "no account",
            "баланс",
            "средств",
            "аккаунт",
            "налич",
        )
        return exc.status_code == 400 and any(word in text for word in retry_words)

    async def process_order(self, deal_id: str) -> None:
        async with self._lock_for_order(deal_id):
            order = self.db.get_order(deal_id)
            if order is None or order.status in {"delivered", "terminated", "refunded", "failed"}:
                return
            if not self.db.is_enabled():
                return

            try:
                if order.provider == "steamsmm":
                    await self._process_steamsmm_order(order)
                    return

                rental_uid = order.rental_uid
                expires_at = order.expires_at
                if not rental_uid:
                    quote, balance = await asyncio.gather(
                        self.kosell.calculate_price(order.kosell_product_id, order.duration_hours),
                        self.kosell.get_balance(),
                    )
                    self.last_balance = balance
                    required = float(quote["total_rub"] if self.config.rent_currency == "RUB" else quote["total_usd"])
                    available = balance.rub if self.config.rent_currency == "RUB" else balance.usd
                    if available + 1e-9 < required:
                        currency_symbol = "₽" if self.config.rent_currency == "RUB" else "$"
                        raise KosellError(
                            f"Недостаточно средств: {available:.2f}{currency_symbol}, нужно {required:.2f}{currency_symbol}",
                            status_code=400, error_code="INSUFFICIENT_BALANCE"
                        )
                    rental = await self.kosell.rent(order.kosell_product_id, order.duration_hours, self.config.rent_currency, f"playerok-{order.deal_id}"[:128])
                    rental_uid = str(rental["rental_uid"])
                    expires_at = str(rental["expires_at"])
                    self.db.set_rental(order.deal_id, rental_uid, expires_at)
                if not expires_at:
                    raise RuntimeError("KOSell не вернул время завершения аренды")
                credentials = await self.kosell.get_credentials(rental_uid)
                message = credentials_message(
                    product_name=order.product_name, duration_hours=order.duration_hours,
                    login=credentials["steam_login"], password=credentials["steam_password"],
                    expires_at=expires_at, timezone_name=self.config.display_timezone
                )
                await self.playerok.send_message(order.chat_id, message)
                self.db.mark_delivered(order.deal_id)
                delivered = self.db.get_order(order.deal_id) or order
                await self.notify_owner(owner_order_text(delivered, "✅ Аренда выдана"))
                if self.config.auto_mark_sent:
                    await self._mark_playerok_sent(delivered)
                else:
                    self.db.mark_playerok_sent(delivered.deal_id)
                await self.check_balance()
            except Exception as exc:
                logger.exception("Ошибка выдачи заказа %s", deal_id)
                await self._handle_order_failure(order, exc)

    async def _process_steamsmm_order(self, order: Order) -> None:
        if not self.steamsmm:
            raise RuntimeError("STEAM_SMM_API_TOKEN не настроен")
        cfg = json.loads(order.service_config or "{}")
        if order.service_type == "commend_cs2":
            target = str(cfg.get("target") or "").strip()
            if not target:
                await self.playerok.send_message(
                    order.chat_id,
                    "📌 Для выдачи похвалы пришлите в этот чат ваш SteamID64 "
                    "(17 цифр) или ссылку вида https://steamcommunity.com/profiles/7656..."
                )
                return
            check = await self.steamsmm.commend_check(target)
            data = check.get("data") if isinstance(check, dict) else {}
            if not bool(isinstance(data, dict) and data.get("on_server")):
                await self.playerok.send_message(
                    order.chat_id,
                    "⚠️ Оплата получена, но игрок сейчас не находится на сервере CS2. "
                    "Пожалуйста, зайдите на сервер, указанный в описании лота. Бот повторит выдачу автоматически."
                )
                raise RuntimeError("Игрок ещё не на сервере CS2 (on_server=false)")
            result = await self.steamsmm.commend_create(
                target, int(cfg.get("friendly", 0)), int(cfg.get("teacher", 0)), int(cfg.get("leader", 0))
            )
            data = result.get("data") if isinstance(result, dict) else {}
            order_id = str(data.get("order_id") or "—") if isinstance(data, dict) else "—"
            self.db.mark_delivered(order.deal_id)
            await self.playerok.send_message(
                order.chat_id,
                "✅ <b>Похвала CS2 запущена</b>.\n"
                f"Дружелюбный: {int(cfg.get('friendly', 0))}\n"
                f"Учитель: {int(cfg.get('teacher', 0))}\n"
                f"Лидер: {int(cfg.get('leader', 0))}\n"
                f"ID заказа SteamSMM: <code>{html.escape(order_id)}</code>"
            )
            delivered = self.db.get_order(order.deal_id) or order
            await self.notify_owner(owner_order_text(delivered, "✅ SteamSMM: похвала запущена") + f"\nSteamSMM ID: <code>{html.escape(order_id)}</code>")
        elif order.service_type == "steam_autoreg":
            category_id = int(cfg.get("category_id", order.kosell_product_id))
            quantity = int(cfg.get("quantity", 1))
            products = await self.steamsmm.list_autoreg_products()
            current = next((p for p in products if int(p.get("category_id", -1)) == category_id), None)
            if current is None:
                title = str(cfg.get("title") or order.product_name)
                current = next((p for p in products if str(p.get("title", "")).strip() == title.strip()), None)
            if current is None:
                raise RuntimeError("Категория авторегов больше не найдена в актуальном каталоге SteamSMM")
            actual_category_id = int(current.get("category_id"))
            stock = int(current.get("stock", 0))
            minimum = int(current.get("min_count", 1))
            maximum = int(current.get("max_count", quantity))
            if stock < quantity:
                raise RuntimeError(f"Недостаточно авторегов: доступно {stock}, требуется {quantity}")
            if quantity < minimum or quantity > maximum:
                raise RuntimeError(f"Количество авторегов вне диапазона {minimum}–{maximum}")
            result = await self.steamsmm.autoreg_create(actual_category_id, quantity)
            data = result.get("data") if isinstance(result, dict) else {}
            accounts = data.get("accounts") if isinstance(data, dict) else None
            accounts_text = data.get("accounts_text") if isinstance(data, dict) else None
            if not accounts and not accounts_text:
                raise RuntimeError("SteamSMM не вернул аккаунты")
            payload = str(accounts_text or "\n".join(str(x) for x in accounts))
            self.db.mark_delivered(order.deal_id)
            await self.playerok.send_message(
                order.chat_id,
                "✅ <b>Ваши Steam-автореги</b>\n\n" + html.escape(payload)
            )
            order_id = str(data.get("order_id") or "—") if isinstance(data, dict) else "—"
            delivered = self.db.get_order(order.deal_id) or order
            await self.notify_owner(owner_order_text(delivered, "✅ SteamSMM: автореги выданы") + f"\nSteamSMM ID: <code>{html.escape(order_id)}</code>")
        else:
            raise RuntimeError(f"Неизвестный тип SteamSMM: {order.service_type}")

        delivered = self.db.get_order(order.deal_id) or order
        if self.config.auto_mark_sent:
            await self._mark_playerok_sent(delivered)
        else:
            self.db.mark_playerok_sent(delivered.deal_id)

    async def _handle_order_failure(self, order: Order, exc: Exception) -> None:
        current = self.db.get_order(order.deal_id) or order
        error_text = f"{type(exc).__name__}: {exc}"[:1000]
        retryable = self._error_retryable(exc)
        next_count = current.retry_count + 1

        if retryable and next_count < self.config.order_retry_max:
            next_retry = (
                datetime.now(timezone.utc)
                + timedelta(seconds=self.config.order_retry_seconds)
            ).isoformat()
            self.db.schedule_retry(current.deal_id, error_text, next_retry)
            if current.retry_count == 0:
                try:
                    await self.playerok.send_message(
                        current.chat_id,
                        "⚠️ Выдача немного задерживается. Бот повторит попытку автоматически, продавец уведомлён.",
                    )
                except Exception:
                    logger.exception("Не удалось сообщить покупателю о задержке")
                await self.notify_owner(
                    owner_order_text(current, "⚠️ Задержка автовыдачи")
                    + f"\nОшибка: <code>{html.escape(error_text)}</code>"
                )
            return

        self.db.mark_failed(current.deal_id, error_text)
        try:
            await self.playerok.send_message(
                current.chat_id,
                "❌ Автовыдача не завершилась. Продавец получил уведомление и проверит заказ вручную.",
            )
        except Exception:
            logger.exception("Не удалось сообщить покупателю об ошибке")
        await self.notify_owner(
            owner_order_text(current, "❌ Автовыдача остановлена")
            + f"\nОшибка: <code>{html.escape(error_text)}</code>"
        )

    async def _mark_playerok_sent(self, order: Order) -> None:
        try:
            await self.playerok.mark_sent(order.deal_id)
            self.db.mark_playerok_sent(order.deal_id)
        except Exception as exc:
            logger.exception("Не удалось отметить сделку %s отправленной", order.deal_id)
            await self.notify_owner(
                "⚠️ Данные Steam выданы, но Playerok не отметил товар отправленным. "
                "Бот повторит попытку.\n"
                f"Заказ: <code>{html.escape(order.deal_id)}</code>\n"
                f"Ошибка: <code>{html.escape(str(exc)[:700])}</code>"
            )

    async def handle_steam_id_message(self, chat_id: str, steam_id64: str) -> None:
        """Save a SteamID64 sent by the buyer in chat and use it for SteamSMM orders."""
        order = self.db.active_order_for_chat(chat_id, utc_now())
        if order is None or order.provider != "steamsmm" or order.service_type != "commend_cs2":
            return
        cfg = json.loads(order.service_config or "{}")
        previous = str(cfg.get("target") or "").strip()
        if previous == steam_id64:
            return
        cfg["target"] = steam_id64
        cfg["target_source"] = "playerok_chat"
        self.db.update_order_service_config(order.deal_id, cfg)
        updated_order = self.db.get_order(order.deal_id) or order
        try:
            await self.notify_owner(
                owner_order_text(updated_order, "🎮 STEAM ID ПОЛУЧЕН — заказ обновлён")
            )
        except Exception:
            logger.exception("Не удалось уведомить владельца о SteamID заказа %s", order.deal_id)
        try:
            await self.playerok.send_message(
                chat_id,
                f"✅ SteamID64 получен: <code>{html.escape(steam_id64)}</code>\n"
                "Данные сохранены. Бот использует этот SteamID для автоматической выдачи похвалы.",
            )
        except Exception:
            logger.exception("Не удалось подтвердить получение SteamID64 для заказа %s", order.deal_id)
        if order.status in {"paid", "processing", "retry"}:
            await self.process_order(order.deal_id)

    async def handle_steam_code(self, chat_id: str) -> None:
        lock = self._code_locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            now_loop = asyncio.get_running_loop().time()
            previous = self._code_last_at.get(chat_id, 0.0)
            if now_loop - previous < self.config.steam_code_cooldown_seconds:
                return
            self._code_last_at[chat_id] = now_loop

            order = self.db.active_order_for_chat(chat_id, utc_now())
            if order is None or not order.rental_uid:
                await self.playerok.send_message(
                    chat_id,
                    "⛔ Активная оплаченная аренда не найдена или её срок уже закончился.",
                )
                return
            try:
                result = await self.kosell.get_code(order.rental_uid)
                if result.get("success") and result.get("code"):
                    await self.playerok.send_message(
                        chat_id,
                        steam_code_message(
                            str(result["code"]),
                            int(result["expires_in"]) if result.get("expires_in") else None,
                            order.product_name,
                        ),
                    )
                    return
                error = str(result.get("error") or "код временно недоступен")
                await self.playerok.send_message(
                    chat_id,
                    f"⚠️ Не удалось получить код: {error}. Подождите несколько секунд и снова напишите: !код",
                )
            except Exception as exc:
                logger.exception("Ошибка Steam Guard для заказа %s", order.deal_id)
                await self.playerok.send_message(
                    chat_id,
                    "⚠️ Сервис Steam Guard временно не ответил. Подождите несколько секунд и снова напишите: !код",
                )
                await self.notify_owner(
                    "⚠️ Ошибка Steam Guard\n"
                    f"Заказ: <code>{html.escape(order.deal_id)}</code>\n"
                    f"Ошибка: <code>{html.escape(str(exc)[:700])}</code>"
                )

    async def handle_refund(self, deal_id: str) -> None:
        order = self.db.get_order(deal_id)
        if order is None or order.status in {"refunded", "terminated"}:
            return
        self.db.mark_refund_pending(deal_id)
        await self._terminate_order(self.db.get_order(deal_id) or order, refunded=True)

    async def _terminate_order(self, order: Order, refunded: bool | None = None) -> None:
        is_refund = refunded if refunded is not None else order.status == "refund_pending"
        async with self._lock_for_order(order.deal_id):
            current = self.db.get_order(order.deal_id) or order
            if current.status in {"terminated", "refunded"}:
                return
            if not current.rental_uid:
                self.db.mark_terminated(
                    current.deal_id, "Аренда KOSell ещё не была создана", refunded=is_refund
                )
                return
            try:
                result = await self.kosell.terminate(current.rental_uid)
                result_text = json.dumps(result, ensure_ascii=False)
            except KosellError as exc:
                if exc.status_code in {400, 404}:
                    result_text = f"KOSell уже завершил аренду: {exc}"
                else:
                    logger.exception("Не удалось завершить аренду %s", current.deal_id)
                    await self.notify_owner(
                        "⚠️ Не удалось закрыть доступ KOSell, бот повторит попытку.\n"
                        f"Заказ: <code>{html.escape(current.deal_id)}</code>\n"
                        f"Ошибка: <code>{html.escape(str(exc)[:700])}</code>"
                    )
                    return
            except Exception as exc:
                logger.exception("Не удалось завершить аренду %s", current.deal_id)
                await self.notify_owner(
                    "⚠️ Не удалось закрыть доступ KOSell, бот повторит попытку.\n"
                    f"Заказ: <code>{html.escape(current.deal_id)}</code>\n"
                    f"Ошибка: <code>{html.escape(str(exc)[:700])}</code>"
                )
                return

            self.db.mark_terminated(current.deal_id, result_text, refunded=is_refund)
            if not is_refund:
                try:
                    await self.playerok.send_message(
                        current.chat_id,
                        "⛔ Срок аренды завершён. Доступ к Steam-аккаунту закрыт автоматически.",
                    )
                except Exception:
                    logger.exception("Не удалось сообщить покупателю о завершении аренды")
            await self.notify_owner(
                owner_order_text(
                    current,
                    "↩️ Возврат: доступ закрыт" if is_refund else "⛔ Аренда завершена",
                )
            )

    async def check_balance(self, force_notify: bool = False) -> Balance:
        balance = await self.kosell.get_balance()
        self.last_balance = balance
        threshold = self.db.low_balance_threshold()
        is_low = balance.rub < threshold
        was_low = self.db.get_setting("low_balance_active", "0") == "1"
        last_alert_raw = self.db.get_setting("low_balance_last_alert", "") or ""
        now = datetime.now(timezone.utc)
        repeat_due = True
        if last_alert_raw:
            try:
                last_alert = datetime.fromisoformat(last_alert_raw)
                repeat_due = (now - last_alert).total_seconds() >= self.config.low_balance_repeat_seconds
            except ValueError:
                repeat_due = True

        if is_low and (force_notify or not was_low or repeat_due):
            await self.notify_owner(
                "🔴 НИЗКИЙ БАЛАНС KOSELL\n"
                f"Баланс: <b>{balance.rub:.2f} ₽</b>\n"
                f"Порог: {threshold:.2f} ₽\n"
                "Пополните баланс, чтобы автовыдача не остановилась."
            )
            self.db.set_setting("low_balance_last_alert", now.isoformat())
        elif was_low and not is_low:
            await self.notify_owner(
                f"🟢 Баланс KOSell восстановлен: <b>{balance.rub:.2f} ₽</b>."
            )

        self.db.set_setting("low_balance_active", "1" if is_low else "0")
        return balance

    async def _retry_loop(self) -> None:
        while True:
            try:
                if self.db.is_enabled():
                    for order in self.db.due_orders(utc_now(), self.config.order_retry_max):
                        await self.process_order(order.deal_id)
                    if self.config.auto_mark_sent:
                        for order in self.db.unsent_playerok_orders():
                            await self._mark_playerok_sent(order)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка цикла повторной выдачи")
            await asyncio.sleep(min(self.config.order_retry_seconds, 30))

    async def _expiry_loop(self) -> None:
        while True:
            try:
                for order in self.db.due_expirations(utc_now()):
                    await self._terminate_order(order)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка цикла завершения аренд")
            await asyncio.sleep(self.config.expiry_check_seconds)

    async def _balance_loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            try:
                await self.check_balance()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Не удалось проверить баланс KOSell")
            await asyncio.sleep(self.config.balance_check_seconds)

    async def _relist_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                if self.playerok.connected:
                    await self.sync_relisted_lots()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Не удалось проверить перевыставленные лоты Playerok")
            await asyncio.sleep(self.config.relist_sync_seconds)
