from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .db import utc_now
from .messages import duration_text


class TelegramUI:
    def __init__(self, service):
        self.service = service
        self.owner_id = service.config.telegram_owner_id
        self.router = Router(name="owner-panel")
        self.pending: dict[int, dict[str, Any]] = {}

    def register(self, dispatcher) -> None:
        self.router.message.register(self.start, CommandStart())
        self.router.message.register(self.cancel, Command("cancel"))
        self.router.callback_query.register(self.callbacks)
        self.router.message.register(self.owner_text)
        dispatcher.include_router(self.router)

    def _owner(self, user_id: int | None) -> bool:
        return user_id == self.owner_id

    @staticmethod
    def _main_keyboard(enabled: bool) -> InlineKeyboardMarkup:
        state = "🟢 Бот включён" if enabled else "🔴 Бот выключен"
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=state, callback_data="toggle_bot"),
                    InlineKeyboardButton(text="💰 Баланс", callback_data="balance"),
                ],
                [
                    InlineKeyboardButton(text="➕ Добавить лот", callback_data="add_lot"),
                    InlineKeyboardButton(text="📦 Лоты", callback_data="lots"),
                ],
                [
                    InlineKeyboardButton(text="🎮 Аренды", callback_data="rentals"),
                    InlineKeyboardButton(text="⚙️ Порог баланса", callback_data="threshold"),
                ],
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="menu")],
            ]
        )

    async def _menu_text(self) -> str:
        stats = self.service.db.stats(utc_now())
        enabled = self.service.db.is_enabled()
        playerok = "подключён" if self.service.playerok.connected else "не подключён"
        balance = self.service.last_balance
        balance_text = f"{balance.rub:.2f} ₽ / ${balance.usd:.2f}" if balance else "ещё не проверен"
        return (
            "🤖 <b>eucalrent</b>\n\n"
            f"Автовыдача: {'🟢 включена' if enabled else '🔴 выключена'}\n"
            f"Playerok: {'🟢' if self.service.playerok.connected else '🔴'} {playerok}\n"
            f"Баланс KOSell: <b>{balance_text}</b>\n"
            f"Порог: {self.service.db.low_balance_threshold():.2f} ₽\n\n"
            f"Активных лотов: {stats['lots']}\n"
            f"Активных аренд: {stats['active']}\n"
            f"Ожидают выдачи: {stats['waiting']}"
        )

    async def start(self, message: Message) -> None:
        if not self._owner(message.from_user.id if message.from_user else None):
            return
        self.pending.pop(self.owner_id, None)
        await message.answer(
            await self._menu_text(),
            reply_markup=self._main_keyboard(self.service.db.is_enabled()),
        )

    async def cancel(self, message: Message) -> None:
        if not self._owner(message.from_user.id if message.from_user else None):
            return
        self.pending.pop(self.owner_id, None)
        await message.answer(
            "Действие отменено.",
            reply_markup=self._main_keyboard(self.service.db.is_enabled()),
        )

    async def callbacks(self, callback: CallbackQuery) -> None:
        if not self._owner(callback.from_user.id if callback.from_user else None):
            await callback.answer()
            return
        data = callback.data or ""
        await callback.answer()
        message = callback.message
        if message is None:
            return

        if data == "menu":
            self.pending.pop(self.owner_id, None)
            await message.edit_text(
                await self._menu_text(),
                reply_markup=self._main_keyboard(self.service.db.is_enabled()),
            )
            return

        if data == "toggle_bot":
            enabled = not self.service.db.is_enabled()
            self.service.db.set_enabled(enabled)
            await message.edit_text(
                await self._menu_text(),
                reply_markup=self._main_keyboard(enabled),
            )
            return

        if data == "balance":
            try:
                balance = await self.service.check_balance(force_notify=False)
                text = (
                    "💰 <b>Баланс KOSell</b>\n\n"
                    f"RUB: <b>{balance.rub:.2f} ₽</b>\n"
                    f"USD: <b>${balance.usd:.2f}</b>\n"
                    f"Пользователь: {html.escape(balance.username)}\n"
                    f"Порог: {self.service.db.low_balance_threshold():.2f} ₽"
                )
            except Exception as exc:
                text = f"❌ Не удалось получить баланс: <code>{html.escape(str(exc)[:800])}</code>"
            await message.edit_text(text, reply_markup=self._back_keyboard())
            return

        if data == "add_lot":
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🎮 KOSell · Аренда Steam", callback_data="add_type:kosell")],
                    [InlineKeyboardButton(text="👍 SteamSMM · Похвала CS2", callback_data="add_type:commend")],
                    [InlineKeyboardButton(text="👤 SteamSMM · Автореги Steam", callback_data="add_type:autoreg")],
                    [InlineKeyboardButton(text="← В меню", callback_data="menu")],
                ]
            )
            await message.edit_text("➕ <b>Добавление лота</b>\n\nВыберите тип товара:", reply_markup=keyboard)
            return

        if data.startswith("add_type:"):
            provider = data.split(":", 1)[1]
            if provider not in {"kosell", "commend", "autoreg"}:
                await message.edit_text("Неизвестный тип лота.", reply_markup=self._back_keyboard())
                return
            self.pending[self.owner_id] = {"stage": "item", "provider": provider}
            name = {"kosell": "KOSell · аренда", "commend": "SteamSMM · похвала CS2", "autoreg": "SteamSMM · автореги"}[provider]
            await message.edit_text(
                f"➕ <b>{name}</b>\n\nПришлите ссылку на уже созданный товар Playerok или его ID.\n\nДля отмены: /cancel",
                reply_markup=self._back_keyboard(),
            )
            return

        if data == "lots":
            await self._show_lots(message)
            return

        if data.startswith("lot_toggle:"):
            lot_id = int(data.split(":", 1)[1])
            self.service.db.toggle_lot(lot_id)
            await self._show_lots(message)
            return

        if data.startswith("lot_delete:"):
            lot_id = int(data.split(":", 1)[1])
            lot = self.service.db.get_lot(lot_id)
            if lot:
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🗑 Да, удалить", callback_data=f"lot_delete_confirm:{lot_id}"
                            )
                        ],
                        [InlineKeyboardButton(text="← Назад", callback_data="lots")],
                    ]
                )
                await message.edit_text(
                    f"Удалить сопоставление <b>{html.escape(lot.playerok_name)}</b>?\n"
                    "Активные аренды при этом продолжат работать.",
                    reply_markup=keyboard,
                )
            return

        if data.startswith("lot_delete_confirm:"):
            lot_id = int(data.split(":", 1)[1])
            self.service.db.delete_lot(lot_id)
            await self._show_lots(message)
            return

        if data == "rentals":
            await self._show_rentals(message)
            return

        if data == "threshold":
            self.pending[self.owner_id] = {"stage": "threshold"}
            await message.edit_text(
                "⚙️ Пришлите новый порог низкого баланса в рублях.\n"
                "Например: <code>100</code>\n\nДля отмены: /cancel",
                reply_markup=self._back_keyboard(),
            )
            return

        if data.startswith("autoreg_pick:"):
            pending = self.pending.get(self.owner_id)
            if not pending or "products" not in pending:
                await message.edit_text("Сессия добавления истекла. Начните заново.", reply_markup=self._back_keyboard())
                return
            index = int(data.split(":", 1)[1])
            products = pending["products"]
            if index < 0 or index >= len(products):
                await message.edit_text("Товар больше недоступен.", reply_markup=self._back_keyboard())
                return
            product = products[index]
            pending["product"] = product
            pending["stage"] = "autoreg_quantity"
            await message.edit_text(f"👤 <b>{html.escape(str(product.get('title', 'Автореги Steam')))}</b>\nРегион: {html.escape(str(product.get('region', '—')))}\nОстаток: {int(product.get('stock', 0))}\nЦена: {float(product.get('price_per_item', 0)):.2f} ₽/шт.\n\nВведите количество:", reply_markup=self._back_keyboard())
            return

        if data.startswith("pick_product:"):
            await self._pick_product(message, data)
            return

        if data.startswith("duration:"):
            hours = int(data.split(":", 1)[1])
            await self._set_duration(message, hours)
            return

        if data == "duration_custom":
            pending = self.pending.get(self.owner_id)
            if not pending or "product" not in pending:
                await message.edit_text("Сессия добавления истекла. Начните заново.", reply_markup=self._back_keyboard())
                return
            pending["stage"] = "duration_custom"
            await message.edit_text(
                "Пришлите срок аренды в часах, например <code>48</code>.",
                reply_markup=self._back_keyboard(),
            )
            return

        if data == "confirm_add":
            pending = self.pending.get(self.owner_id)
            if not pending or "item" not in pending:
                await message.edit_text("Сессия добавления истекла. Начните заново.", reply_markup=self._back_keyboard())
                return
            item = pending["item"]
            provider = pending.get("provider", "kosell")
            import json
            if provider == "kosell":
                if not all(key in pending for key in ("product", "hours")):
                    await message.edit_text("Сессия добавления истекла. Начните заново.", reply_markup=self._back_keyboard())
                    return
                product = pending["product"]
                lot = self.service.db.add_lot(item, int(product["id"]), str(product["name"]), int(pending["hours"]))
                self.pending.pop(self.owner_id, None)
                text = ("✅ <b>Лот добавлен</b>\n\n" f"Тип: 🎮 KOSell\nPlayerok: {html.escape(lot.playerok_name)}\n" f"Услуга: {html.escape(lot.kosell_product_name)}\n" f"Срок: {duration_text(lot.duration_hours)}")
            elif provider == "commend":
                cfg = pending.get("commend")
                if not cfg:
                    await message.edit_text("Сессия добавления истекла. Начните заново.", reply_markup=self._back_keyboard())
                    return
                lot = self.service.db.add_lot(item, 0, "Похвала CS2", 1, provider="steamsmm", service_type="commend_cs2", service_config=json.dumps(cfg, ensure_ascii=False))
                self.pending.pop(self.owner_id, None)
                text = ("✅ <b>Лот добавлен</b>\n\n" f"Тип: 👍 SteamSMM · Похвала CS2\nPlayerok: {html.escape(lot.playerok_name)}\n" f"Дружелюбный: {cfg['friendly']}\nУчитель: {cfg['teacher']}\nЛидер: {cfg['leader']}")
            else:
                cfg = pending.get("autoreg")
                product = pending.get("product")
                if not cfg or not product:
                    await message.edit_text("Сессия добавления истекла. Начните заново.", reply_markup=self._back_keyboard())
                    return
                lot = self.service.db.add_lot(item, int(product["category_id"]), str(product.get("title", "Автореги Steam")), 1, provider="steamsmm", service_type="steam_autoreg", service_config=json.dumps(cfg, ensure_ascii=False))
                self.pending.pop(self.owner_id, None)
                text = ("✅ <b>Лот добавлен</b>\n\n" f"Тип: 👤 SteamSMM · Автореги Steam\nPlayerok: {html.escape(lot.playerok_name)}\n" f"Товар: {html.escape(str(product.get('title', 'Автореги Steam')))}\n" f"Количество: {cfg['quantity']} шт.")
            await message.edit_text(text, reply_markup=self._back_keyboard())
            return

    @staticmethod
    def _back_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="← В меню", callback_data="menu")]]
        )

    async def _show_lots(self, message: Message) -> None:
        lots = self.service.db.list_lots()
        if not lots:
            await message.edit_text(
                "📦 Лотов пока нет. Нажмите «Добавить лот» в меню.",
                reply_markup=self._back_keyboard(),
            )
            return
        lines = ["📦 <b>Лоты</b>", ""]
        keyboard: list[list[InlineKeyboardButton]] = []
        for lot in lots[:20]:
            state = "🟢" if lot.enabled else "🔴"
            if lot.provider == "kosell":
                detail = f"🎮 KOSell · {html.escape(lot.kosell_product_name)}, {duration_text(lot.duration_hours)}"
            elif lot.service_type == "commend_cs2":
                import json
                cfg = json.loads(lot.service_config or "{}")
                detail = f"👍 SteamSMM · Похвала {cfg.get('friendly', 0)}/{cfg.get('teacher', 0)}/{cfg.get('leader', 0)}"
            else:
                import json
                cfg = json.loads(lot.service_config or "{}")
                detail = f"👤 SteamSMM · Автореги, {cfg.get('quantity', 0)} шт."
            lines.append(f"{state} <b>#{lot.id}</b> {html.escape(lot.playerok_name)}\n→ {detail}")
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=f"{state} #{lot.id}", callback_data=f"lot_toggle:{lot.id}"
                    ),
                    InlineKeyboardButton(
                        text="🗑", callback_data=f"lot_delete:{lot.id}"
                    ),
                ]
            )
        keyboard.append([InlineKeyboardButton(text="← В меню", callback_data="menu")])
        await message.edit_text(
            "\n\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        )

    async def _show_rentals(self, message: Message) -> None:
        orders = self.service.db.list_active_orders(utc_now())
        if not orders:
            text = "🎮 Активных аренд сейчас нет."
        else:
            lines = ["🎮 <b>Активные аренды</b>", ""]
            for order in orders:
                expiry = datetime.fromisoformat(order.expires_at or utc_now()).strftime("%d.%m %H:%M UTC")
                buyer = f"@{order.buyer_username}" if order.buyer_username else order.buyer_id or "—"
                lines.append(
                    f"• {html.escape(order.product_name)}\n"
                    f"  заказ <code>{html.escape(order.deal_id)}</code>\n"
                    f"  покупатель {html.escape(buyer)}, до {expiry}"
                )
            text = "\n\n".join(lines)
        await message.edit_text(text, reply_markup=self._back_keyboard())

    async def owner_text(self, message: Message) -> None:
        if not self._owner(message.from_user.id if message.from_user else None):
            return
        pending = self.pending.get(self.owner_id)
        if not pending or not message.text:
            return
        stage = pending.get("stage")
        value = message.text.strip()

        if stage == "item":
            await message.answer("🔎 Проверяю лот Playerok…")
            try:
                item = await self.service.playerok.resolve_item(value)
            except Exception as exc:
                await message.answer("❌ Не удалось открыть лот Playerok. Проверьте ссылку и подключение.\n" f"<code>{html.escape(str(exc)[:800])}</code>")
                return
            pending["item"] = item
            provider = pending.get("provider", "kosell")
            if provider == "kosell":
                pending["stage"] = "search"
                await message.answer(f"✅ Лот найден: <b>{html.escape(item.name)}</b>\n\nТеперь напишите название игры для поиска в KOSell.")
            elif provider == "commend":
                pending["stage"] = "commend_target"
                await message.answer(
                    f"✅ Лот найден: <b>{html.escape(item.name)}</b>\n\n"
                    "Пришлите SteamID64 или ссылку на профиль игрока, которому нужно выдавать похвалы."
                )
            else:
                if not self.service.steamsmm:
                    await message.answer("❌ Не задан STEAM_SMM_API_TOKEN в .env.")
                    return
                await message.answer("🔎 Получаю актуальный каталог авторегов SteamSMM…")
                try:
                    products = await self.service.steamsmm.list_autoreg_products()
                except Exception as exc:
                    await message.answer(f"❌ SteamSMM: <code>{html.escape(str(exc)[:800])}</code>")
                    return
                if not products:
                    await message.answer("Каталог авторегов пуст.")
                    return
                pending["products"] = products[:20]
                pending["stage"] = "autoreg_pick"
                keyboard = []
                for i, product in enumerate(pending["products"]):
                    title = str(product.get("title", "Автореги"))
                    price = float(product.get("price_per_item", 0))
                    stock = int(product.get("stock", 0))
                    keyboard.append([InlineKeyboardButton(text=f"{title[:38]} · {price:.2f}₽ · {stock} шт."[:60], callback_data=f"autoreg_pick:{i}")])
                keyboard.append([InlineKeyboardButton(text="← В меню", callback_data="menu")])
                await message.answer("Выберите категорию авторегов:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
            return

        if stage == "search":
            if len(value) < 2:
                await message.answer("Введите хотя бы 2 символа названия игры.")
                return
            await message.answer("🔎 Ищу игру в KOSell…")
            try:
                products = await self.service.kosell.list_products(value)
            except Exception as exc:
                await message.answer(
                    f"❌ Ошибка KOSell: <code>{html.escape(str(exc)[:800])}</code>"
                )
                return
            if not products:
                await message.answer("Ничего не найдено. Попробуйте другое название.")
                return
            pending["products"] = products[:10]
            pending["stage"] = "pick_product"
            keyboard = []
            for index, product in enumerate(pending["products"]):
                stock = int(product.get("available_accounts", 0))
                price = float(product.get("price_per_hour_rub", 0))
                label = f"{product.get('name')} · {price:.2f}₽/ч · {stock} шт."
                keyboard.append(
                    [InlineKeyboardButton(text=label[:60], callback_data=f"pick_product:{index}")]
                )
            keyboard.append([InlineKeyboardButton(text="← В меню", callback_data="menu")])
            await message.answer(
                "Выберите игру KOSell:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            )
            return

        if stage == "commend_target":
            if len(value) < 8:
                await message.answer("Пришлите SteamID64 или полную ссылку на профиль Steam.")
                return
            if not self.service.steamsmm:
                await message.answer("❌ Не задан STEAM_SMM_API_TOKEN в .env.")
                return
            try:
                check = await self.service.steamsmm.commend_check(value)
            except Exception as exc:
                await message.answer(f"❌ SteamSMM: <code>{html.escape(str(exc)[:800])}</code>")
                return
            data = check.get("data") if isinstance(check, dict) else {}
            normalized = str(data.get("steam_id") or data.get("steamid64") or value) if isinstance(data, dict) else value
            pending["commend_target"] = normalized
            pending["stage"] = "commend_counts"
            await message.answer(
                "✅ Игрок найден/проверен.\n\n"
                "Укажите количества в формате <code>15 5 10</code> — "
                "Дружелюбный, Учитель, Лидер."
            )
            return

        if stage == "commend_counts":
            parts = value.replace(",", " ").split()
            if len(parts) != 3:
                await message.answer("Нужно 3 числа: <code>15 5 10</code>.")
                return
            try:
                friendly, teacher, leader = (int(x) for x in parts)
            except ValueError:
                await message.answer("Количество должно быть целым числом.")
                return
            if min(friendly, teacher, leader) < 0 or max(friendly, teacher, leader) > 7500 or (max(friendly, teacher, leader) and max(friendly, teacher, leader) < 15):
                await message.answer("Максимальное количество одного типа должно быть от 15 до 7500.")
                return
            if not self.service.steamsmm:
                await message.answer("❌ Не задан STEAM_SMM_API_TOKEN в .env.")
                return
            try:
                quote = await self.service.steamsmm.commend_price(friendly, teacher, leader)
            except Exception as exc:
                await message.answer(f"❌ SteamSMM: <code>{html.escape(str(exc)[:800])}</code>")
                return
            pending["commend"] = {"friendly": friendly, "teacher": teacher, "leader": leader}
            pending["stage"] = "confirm"
            await message.answer("Проверьте лот\n\n" f"Playerok: <b>{html.escape(pending['item'].name)}</b>\nПохвала: {friendly}/{teacher}/{leader}\n" f"Себестоимость: <b>{float(quote.get('total_cost', 0)):.2f} ₽</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Добавить", callback_data="confirm_add")],[InlineKeyboardButton(text="← В меню", callback_data="menu")]]))
            return

        if stage == "autoreg_quantity":
            try:
                quantity = int(value)
            except ValueError:
                await message.answer("Количество должно быть целым числом.")
                return
            product = pending.get("product")
            if not product:
                await message.answer("Сессия добавления истекла.")
                return
            minimum = int(product.get("min_count", 1))
            maximum = int(product.get("max_count", 1000))
            stock = int(product.get("stock", 0))
            upper = min(maximum, stock) if stock > 0 else maximum
            if quantity < minimum or quantity > upper:
                await message.answer(f"Количество: от {minimum} до {upper}.")
                return
            if not self.service.steamsmm:
                await message.answer("❌ Не задан STEAM_SMM_API_TOKEN в .env.")
                return
            try:
                quote = await self.service.steamsmm.autoreg_price(int(product["category_id"]), quantity)
            except Exception as exc:
                await message.answer(f"❌ SteamSMM: <code>{html.escape(str(exc)[:800])}</code>")
                return
            pending["autoreg"] = {"category_id": int(product["category_id"]), "quantity": quantity, "title": str(product.get("title", "Автореги Steam")), "region": str(product.get("region", ""))}
            pending["stage"] = "confirm"
            await message.answer("Проверьте лот\n\n" f"Playerok: <b>{html.escape(pending['item'].name)}</b>\nАвтореги: <b>{html.escape(str(product.get('title', 'Автореги')))}</b>\nКоличество: <b>{quantity} шт.</b>\n" f"Себестоимость: <b>{float(quote.get('total_cost', quote.get('cost', 0))):.2f} ₽</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Добавить", callback_data="confirm_add")],[InlineKeyboardButton(text="← В меню", callback_data="menu")]]))
            return

        if stage == "duration_custom":
            try:
                hours = int(value)
            except ValueError:
                await message.answer("Пришлите целое количество часов, например <code>48</code>.")
                return
            await self._set_duration(message, hours)
            return

        if stage == "threshold":
            try:
                amount = float(value.replace(",", "."))
            except ValueError:
                await message.answer("Пришлите число, например <code>100</code>.")
                return
            if amount < 0 or amount > 10_000_000:
                await message.answer("Порог должен быть от 0 до 10 000 000 ₽.")
                return
            self.service.db.set_low_balance_threshold(amount)
            self.pending.pop(self.owner_id, None)
            await message.answer(
                f"✅ Порог низкого баланса установлен: <b>{amount:.2f} ₽</b>.",
                reply_markup=self._main_keyboard(self.service.db.is_enabled()),
            )

    async def _pick_product(self, message: Message, data: str) -> None:
        pending = self.pending.get(self.owner_id)
        if not pending or "products" not in pending:
            await message.edit_text("Сессия добавления истекла. Начните заново.", reply_markup=self._back_keyboard())
            return
        index = int(data.split(":", 1)[1])
        products = pending["products"]
        if index < 0 or index >= len(products):
            await message.edit_text("Игра больше недоступна в списке. Начните заново.", reply_markup=self._back_keyboard())
            return
        product = products[index]
        pending["product"] = product
        pending["stage"] = "duration"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="1 день", callback_data="duration:24"),
                    InlineKeyboardButton(text="3 дня", callback_data="duration:72"),
                    InlineKeyboardButton(text="7 дней", callback_data="duration:168"),
                ],
                [InlineKeyboardButton(text="Другой срок", callback_data="duration_custom")],
                [InlineKeyboardButton(text="← В меню", callback_data="menu")],
            ]
        )
        await message.edit_text(
            f"KOSell: <b>{html.escape(str(product.get('name')))}</b>\n"
            f"Доступно аккаунтов: {int(product.get('available_accounts', 0))}\n"
            f"Разрешённый срок: {int(product.get('min_hours', 1))}–{int(product.get('max_hours', 8760))} ч.\n\n"
            "Выберите срок аренды:",
            reply_markup=keyboard,
        )

    async def _set_duration(self, message: Message, hours: int) -> None:
        pending = self.pending.get(self.owner_id)
        if not pending or "product" not in pending or "item" not in pending:
            await message.answer("Сессия добавления истекла. Начните заново.")
            return
        product = pending["product"]
        min_hours = int(product.get("min_hours", 1))
        max_hours = int(product.get("max_hours", 8760))
        if hours < min_hours or hours > max_hours:
            await message.answer(
                f"Для этой игры разрешено от {min_hours} до {max_hours} часов."
            )
            return
        try:
            quote = await self.service.kosell.calculate_price(int(product["id"]), hours)
        except Exception as exc:
            await message.answer(
                f"❌ Не удалось рассчитать цену: <code>{html.escape(str(exc)[:800])}</code>"
            )
            return
        pending["hours"] = hours
        pending["stage"] = "confirm"
        item = pending["item"]
        total_rub = float(quote.get("total_rub", 0))
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Добавить", callback_data="confirm_add")],
                [InlineKeyboardButton(text="← В меню", callback_data="menu")],
            ]
        )
        text = (
            "Проверьте сопоставление:\n\n"
            f"Playerok: <b>{html.escape(item.name)}</b>\n"
            f"KOSell: <b>{html.escape(str(product.get('name')))}</b>\n"
            f"Срок: <b>{duration_text(hours)}</b>\n"
            f"Текущая себестоимость KOSell: <b>{total_rub:.2f} ₽</b>\n\n"
            "Цена KOSell может измениться к моменту заказа."
        )
        if hasattr(message, "edit_text"):
            try:
                await message.edit_text(text, reply_markup=keyboard)
                return
            except Exception:
                pass
        await message.answer(text, reply_markup=keyboard)
