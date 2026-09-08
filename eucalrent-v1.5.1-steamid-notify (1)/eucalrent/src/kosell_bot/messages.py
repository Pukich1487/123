from __future__ import annotations

import html
import json

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Order


def duration_text(hours: int) -> str:
    if hours % 24 == 0:
        days = hours // 24
        if 11 <= days % 100 <= 14:
            word = "дней"
        elif days % 10 == 1:
            word = "день"
        elif days % 10 in {2, 3, 4}:
            word = "дня"
        else:
            word = "дней"
        return f"{days} {word}"
    return f"{hours} ч."


def credentials_message(
    *,
    product_name: str,
    duration_hours: int,
    login: str,
    password: str,
    expires_at: str,
    timezone_name: str,
) -> str:
    expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    local = expires.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y в %H:%M")
    return (
        "✅ АРЕНДА ВЫДАНА\n\n"
        f"Игра: {product_name}\n"
        f"Срок: {duration_text(duration_hours)}\n"
        f"Логин Steam: {login}\n"
        f"Пароль Steam: {password}\n"
        f"Доступ до: {local}\n\n"
        "🔐 Steam Guard\n"
        "Чтобы получить код, напишите команду: !код\n"
        "Если Steam покажет ошибку или попросит новый код, снова напишите: !код\n\n"
        "❗ Не меняйте пароль, почту и настройки аккаунта. "
        "После окончания срока доступ будет закрыт автоматически."
    )


def steam_code_message(code: str, expires_in: int | None, product_name: str) -> str:
    lifetime = f" Код действует ещё примерно {expires_in} сек." if expires_in else ""
    return (
        f"🔐 Steam Guard для {product_name}: {code}\n"
        f"{lifetime}\n"
        "Если Steam покажет ошибку или попросит новый код, снова напишите: !код"
    ).replace("\n\n\n", "\n\n")


def owner_order_text(order: Order, heading: str) -> str:
    buyer = f"@{order.buyer_username}" if order.buyer_username else order.buyer_id or "неизвестен"
    steam_id = "—"
    if order.provider == "steamsmm":
        try:
            cfg = json.loads(order.service_config or "{}")
            steam_id = str(cfg.get("target") or "ожидается от клиента")
        except (TypeError, ValueError):
            steam_id = "ожидается от клиента"
    return (
        f"{heading}\n"
        f"Заказ: <code>{html.escape(str(order.deal_id))}</code>\n"
        f"Покупатель: {html.escape(str(buyer))}\n"
        f"Лот: {html.escape(str(order.product_name))}\n"
        f"Steam ID: <code>{html.escape(steam_id)}</code>\n"
        f"Срок: {duration_text(order.duration_hours)}"
    )
