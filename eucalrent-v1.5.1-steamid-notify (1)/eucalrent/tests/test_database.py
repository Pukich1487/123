from datetime import datetime, timedelta, timezone

from kosell_bot.db import Database
from kosell_bot.models import PaidOrder, PlayerokItem
from kosell_bot.playerok import short_item_id


def test_playerok_short_id_is_read_from_slug_and_full_uuid():
    assert short_item_id(None, "0767d9214e5a-2-busta-discord") == "0767d9214e5a"
    assert (
        short_item_id("1f14ac1f-b6c2-6c30-5e22-2332c147e2e5", None)
        == "2332c147e2e5"
    )


def test_lot_and_order_are_idempotent(tmp_path):
    db = Database(tmp_path / "bot.sqlite3")
    db.ensure_defaults(True, 100)
    item = PlayerokItem("full-item-id", "short-slug", "Forza Horizon 6 — 1 день")
    lot = db.add_lot(item, 77, "Forza Horizon 6", 24)
    paid = PaidOrder("deal-1", item.id, "chat-1", "buyer-1", "buyer")

    first, first_created = db.create_order(paid, lot)
    second, second_created = db.create_order(paid, lot)

    assert first_created is True
    assert second_created is False
    assert first.deal_id == second.deal_id == "deal-1"
    assert db.stats(datetime.now(timezone.utc).isoformat())["waiting"] == 1
    db.close()


def test_active_order_and_expiration(tmp_path):
    db = Database(tmp_path / "bot.sqlite3")
    db.ensure_defaults(True, 100)
    lot = db.add_lot(PlayerokItem("item", "slug", "Game"), 1, "Game", 24)
    paid = PaidOrder("deal", "item", "chat", None, None)
    db.create_order(paid, lot)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    db.set_rental("deal", "rent-uid", future)
    db.mark_delivered("deal")

    assert db.active_order_for_chat("chat", datetime.now(timezone.utc).isoformat()) is not None
    assert db.due_expirations(datetime.now(timezone.utc).isoformat()) == []

    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    db.set_rental("deal", "rent-uid", past)
    db.mark_delivered("deal")
    assert [order.deal_id for order in db.due_expirations(datetime.now(timezone.utc).isoformat())] == ["deal"]
    db.close()


def test_refund_pending_is_due_without_rental(tmp_path):
    db = Database(tmp_path / "bot.sqlite3")
    db.ensure_defaults(True, 100)
    lot = db.add_lot(PlayerokItem("item", "slug", "Game"), 1, "Game", 24)
    db.create_order(PaidOrder("deal", "item", "chat", None, None), lot)
    db.mark_refund_pending("deal")

    due = db.due_expirations(datetime.now(timezone.utc).isoformat())
    assert len(due) == 1
    assert due[0].status == "refund_pending"
    db.close()


def test_steamsmm_lot_configuration_is_persisted(tmp_path):
    db = Database(tmp_path / "bot.sqlite3")
    db.ensure_defaults(True, 100)
    lot = db.add_lot(
        PlayerokItem("steam-item", "slug", "Похвала CS2 100"),
        0,
        "Похвала CS2",
        1,
        provider="steamsmm",
        service_type="commend_cs2",
        service_config='{"friendly":100,"teacher":0,"leader":100}',
    )
    saved = db.get_lot(lot.id)
    assert saved is not None
    assert saved.provider == "steamsmm"
    assert saved.service_type == "commend_cs2"
    assert saved.service_config == '{"friendly":100,"teacher":0,"leader":100}'
    db.close()
