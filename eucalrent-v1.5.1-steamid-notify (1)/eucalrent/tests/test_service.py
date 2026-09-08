from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kosell_bot.config import Config
from kosell_bot.kosell import Balance
from kosell_bot.models import PaidOrder, PlayerokItem
from kosell_bot.service import RentalService, is_steam_code_command


class FakeKosell:
    def __init__(self):
        self.rent_calls = 0
        self.code_calls = 0
        self.terminate_calls = 0

    async def calculate_price(self, product_id, hours):
        return {"total_rub": 10.0, "total_usd": 0.1}

    async def get_balance(self):
        return Balance(500.0, 5.0, "seller", "user")

    async def rent(self, product_id, duration_hours, currency, idempotency_key):
        self.rent_calls += 1
        return {
            "rental_uid": "rental-uid",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        }

    async def get_credentials(self, rental_uid):
        return {"steam_login": "login", "steam_password": "password"}

    async def get_code(self, rental_uid):
        self.code_calls += 1
        return {"success": True, "code": "ABCDE", "expires_in": 20}

    async def terminate(self, rental_uid):
        self.terminate_calls += 1
        return {"success": True, "method": "password_change"}

    async def close(self):
        pass


class FakePlayerok:
    def __init__(self):
        self.messages = []
        self.marked = []
        self.connected = True

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))

    async def mark_sent(self, deal_id):
        self.marked.append(deal_id)

    async def list_active_items(self):
        return []

    def stop(self):
        pass


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


def test_steam_code_command_is_exact():
    assert is_steam_code_command("!код")
    assert is_steam_code_command("  !КОД  ")
    assert not is_steam_code_command("код")
    assert not is_steam_code_command("/код")
    assert not is_steam_code_command("!код пожалуйста")


def make_config(tmp_path):
    return Config(
        kosell_api_key="secret",
        kosell_base_url="https://example.test/api/v1",
        rent_currency="RUB",
        low_balance_rub=100,
        balance_check_seconds=300,
        low_balance_repeat_seconds=21600,
        telegram_bot_token="123456:TEST_TOKEN",
        telegram_owner_id=123,
        playerok_cookies="token=fake",
        playerok_user_agent="test-agent",
        playerok_proxy=None,
        auto_mark_sent=True,
        auto_relist_sync=True,
        relist_sync_seconds=60,
        bot_enabled_default=True,
        display_timezone="Europe/Moscow",
        database_path=tmp_path / "bot.sqlite3",
        order_retry_seconds=10,
        order_retry_max=3,
        expiry_check_seconds=5,
        steam_code_cooldown_seconds=1,
        log_level="INFO",
    )


@pytest.mark.asyncio
async def test_order_is_rented_once_delivered_and_marked(tmp_path):
    service = RentalService(make_config(tmp_path))
    await service.kosell.close()
    await service.bot.session.close()
    fake_kosell = FakeKosell()
    fake_playerok = FakePlayerok()
    service.kosell = fake_kosell
    service.playerok = fake_playerok
    service.bot = FakeBot()

    item = PlayerokItem("item-id", "slug", "Playerok Game Lot")
    lot = service.db.add_lot(item, 9, "KOSell Game", 24)
    service.db.create_order(PaidOrder("deal", item.id, "chat", "buyer", "name"), lot)

    await service.process_order("deal")
    await service.process_order("deal")

    order = service.db.get_order("deal")
    assert order is not None and order.status == "delivered"
    assert fake_kosell.rent_calls == 1
    assert fake_playerok.marked == ["deal"]
    assert any("Логин Steam: login" in text for _, text in fake_playerok.messages)
    service.db.close()


@pytest.mark.asyncio
async def test_relisted_paid_item_matches_short_id_at_slug_start(tmp_path):
    service = RentalService(make_config(tmp_path))
    await service.kosell.close()
    await service.bot.session.close()
    service.kosell = FakeKosell()
    service.playerok = FakePlayerok()
    service.bot = FakeBot()

    old = PlayerokItem(
        "1f14ac1f-b6c2-6c30-5e22-2332c147e2e5",
        "2332c147e2e5-forza-horizon-6-1-den",
        "Forza Horizon 6 — 1 день",
    )
    service.db.add_lot(old, 9, "Forza Horizon 6", 24)
    paid = PaidOrder(
        "deal-prefix",
        "2a000000-1111-2222-3333-2332c147e2e5",
        "chat-prefix",
        "buyer",
        "buyer",
        "2332c147e2e5-forza-horizon-6-1-den",
        "Forza Horizon 6 — 1 день",
    )

    await service.handle_paid_order(paid)

    lot = service.db.get_lot_by_item(paid.item_id)
    assert lot is not None and lot.playerok_item_id == paid.item_id
    assert service.db.get_order(paid.deal_id).status == "delivered"
    assert any("короткому ID" in text for _, text in service.bot.messages)
    service.db.close()


@pytest.mark.asyncio
async def test_relisted_paid_item_falls_back_to_unique_exact_title(tmp_path):
    service = RentalService(make_config(tmp_path))
    await service.kosell.close()
    await service.bot.session.close()
    service.kosell = FakeKosell()
    service.playerok = FakePlayerok()
    service.bot = FakeBot()

    old = PlayerokItem(
        "1f14ac1f-b6c2-6c30-5e22-2332c147e2e5",
        "2332c147e2e5-forza-horizon-6-3-dnya",
        "Forza Horizon 6 — 3 дня",
    )
    service.db.add_lot(old, 9, "Forza Horizon 6", 72)
    paid = PaidOrder(
        "deal-title",
        "2a000000-1111-2222-3333-aaaaaaaaaaaa",
        "chat-title",
        "buyer",
        "buyer",
        "aaaaaaaaaaaa-forza-horizon-6-3-dnya",
        "Forza Horizon 6 — 3 дня",
    )

    await service.handle_paid_order(paid)

    assert service.db.get_lot_by_item(paid.item_id) is not None
    assert service.db.get_order(paid.deal_id).status == "delivered"
    assert any("точному названию" in text for _, text in service.bot.messages)
    service.db.close()


@pytest.mark.asyncio
async def test_periodic_sync_rebinds_unique_relisted_item(tmp_path):
    service = RentalService(make_config(tmp_path))
    await service.kosell.close()
    await service.bot.session.close()
    service.bot = FakeBot()
    fake_playerok = FakePlayerok()
    service.playerok = fake_playerok

    old = PlayerokItem(
        "1f14ac1f-b6c2-6c30-5e22-2332c147e2e5",
        "2332c147e2e5-fears-to-fathom-1-den",
        "Fears to Fathom: Scratch Creek — 1 день",
    )
    service.db.add_lot(old, 11, "Fears to Fathom: Scratch Creek", 24)
    new = PlayerokItem(
        "2a000000-1111-2222-3333-bbbbbbbbbbbb",
        "bbbbbbbbbbbb-fears-to-fathom-1-den",
        "Fears to Fathom: Scratch Creek — 1 день",
    )

    async def active_items():
        return [new]

    fake_playerok.list_active_items = active_items
    rebound = await service.sync_relisted_lots()

    assert len(rebound) == 1
    assert service.db.get_lot_by_item(new.id).playerok_item_id == new.id
    assert service.db.get_lot_by_item(old.id).id == rebound[0][0].id
    service.db.close()


@pytest.mark.asyncio
async def test_code_and_refund_terminate_access(tmp_path):
    service = RentalService(make_config(tmp_path))
    await service.kosell.close()
    await service.bot.session.close()
    fake_kosell = FakeKosell()
    fake_playerok = FakePlayerok()
    service.kosell = fake_kosell
    service.playerok = fake_playerok
    service.bot = FakeBot()

    item = PlayerokItem("item-id", "slug", "Lot")
    lot = service.db.add_lot(item, 9, "Game", 24)
    service.db.create_order(PaidOrder("deal", item.id, "chat", "buyer", "name"), lot)
    await service.process_order("deal")

    await service.handle_steam_code("chat")
    assert fake_kosell.code_calls == 1
    assert any("ABCDE" in text for _, text in fake_playerok.messages)

    await service.handle_refund("deal")
    assert fake_kosell.terminate_calls == 1
    assert service.db.get_order("deal").status == "refunded"
    service.db.close()
