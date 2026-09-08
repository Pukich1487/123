from __future__ import annotations

import sqlite3
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Lot, Order, PaidOrder, PlayerokItem


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playerok_item_id TEXT NOT NULL UNIQUE,
                playerok_slug TEXT,
                playerok_name TEXT NOT NULL,
                kosell_product_id INTEGER NOT NULL,
                kosell_product_name TEXT NOT NULL,
                duration_hours INTEGER NOT NULL CHECK(duration_hours > 0),
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'kosell',
                service_type TEXT NOT NULL DEFAULT 'rental',
                service_config TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS lot_item_aliases (
                playerok_item_id TEXT PRIMARY KEY,
                lot_id INTEGER NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_lot_item_aliases_lot
            ON lot_item_aliases(lot_id);

            CREATE TABLE IF NOT EXISTS orders (
                deal_id TEXT PRIMARY KEY,
                playerok_item_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                buyer_id TEXT,
                buyer_username TEXT,
                kosell_product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                duration_hours INTEGER NOT NULL,
                rental_uid TEXT UNIQUE,
                status TEXT NOT NULL,
                expires_at TEXT,
                credentials_sent_at TEXT,
                mark_sent_at TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                last_error TEXT,
                termination_result TEXT,
                provider TEXT NOT NULL DEFAULT 'kosell',
                service_type TEXT NOT NULL DEFAULT 'rental',
                service_config TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_orders_due
            ON orders(status, next_retry_at);

            CREATE INDEX IF NOT EXISTS idx_orders_expiry
            ON orders(status, expires_at);

            CREATE INDEX IF NOT EXISTS idx_orders_chat
            ON orders(chat_id, status, expires_at);
            """
        )
        # Backward-compatible migrations for databases created before SteamSMM support.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(lots)").fetchall()}
        for name, ddl in (("provider", "TEXT NOT NULL DEFAULT 'kosell'"), ("service_type", "TEXT NOT NULL DEFAULT 'rental'"), ("service_config", "TEXT NOT NULL DEFAULT '{}'")):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE lots ADD COLUMN {name} {ddl}")
        order_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(orders)").fetchall()}
        for name, ddl in (("provider", "TEXT NOT NULL DEFAULT 'kosell'"), ("service_type", "TEXT NOT NULL DEFAULT 'rental'"), ("service_config", "TEXT NOT NULL DEFAULT '{}'")):
            if name not in order_columns:
                self._conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {ddl}")
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )
            self._conn.commit()

    def ensure_defaults(self, bot_enabled: bool, low_balance_rub: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('bot_enabled', ?)",
                ("1" if bot_enabled else "0",),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('low_balance_rub', ?)",
                (str(low_balance_rub),),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('low_balance_active', '0')"
            )
            self._conn.commit()

    def is_enabled(self) -> bool:
        return self.get_setting("bot_enabled", "1") == "1"

    def set_enabled(self, enabled: bool) -> None:
        self.set_setting("bot_enabled", "1" if enabled else "0")

    def low_balance_threshold(self) -> float:
        return float(self.get_setting("low_balance_rub", "100") or 100)

    def set_low_balance_threshold(self, value: float) -> None:
        self.set_setting("low_balance_rub", f"{value:.2f}")
        self.set_setting("low_balance_active", "0")
        self.set_setting("low_balance_last_alert", "")

    @staticmethod
    def _lot(row: sqlite3.Row | None) -> Lot | None:
        if not row:
            return None
        return Lot(
            id=row["id"],
            playerok_item_id=row["playerok_item_id"],
            playerok_slug=row["playerok_slug"],
            playerok_name=row["playerok_name"],
            kosell_product_id=row["kosell_product_id"],
            kosell_product_name=row["kosell_product_name"],
            duration_hours=row["duration_hours"],
            enabled=bool(row["enabled"]),
            provider=str(row["provider"] or "kosell"),
            service_type=str(row["service_type"] or "rental"),
            service_config=str(row["service_config"] or "{}"),
        )

    def add_lot(
        self, item: PlayerokItem, product_id: int, product_name: str, duration_hours: int,
        provider: str = "kosell", service_type: str = "rental", service_config: str = "{}"
    ) -> Lot:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO lots(
                    playerok_item_id, playerok_slug, playerok_name,
                    kosell_product_id, kosell_product_name, duration_hours,
                    enabled, created_at, updated_at, provider, service_type, service_config
                ) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(playerok_item_id) DO UPDATE SET
                    playerok_slug = excluded.playerok_slug,
                    playerok_name = excluded.playerok_name,
                    kosell_product_id = excluded.kosell_product_id,
                    kosell_product_name = excluded.kosell_product_name,
                    duration_hours = excluded.duration_hours,
                    enabled = 1,
                    provider = excluded.provider,
                    service_type = excluded.service_type,
                    service_config = excluded.service_config,
                    updated_at = excluded.updated_at
                """,
                (
                    item.id,
                    item.slug,
                    item.name,
                    product_id,
                    product_name,
                    duration_hours,
                    now,
                    now,
                    provider,
                    service_type,
                    service_config,
                ),
            )
            self._conn.commit()
            return self.get_lot_by_item(item.id)  # type: ignore[return-value]

    def get_lot_by_item(self, playerok_item_id: str) -> Lot | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lots WHERE playerok_item_id = ?", (playerok_item_id,)
            ).fetchone()
            if row is None:
                row = self._conn.execute(
                    """
                    SELECT lots.* FROM lots
                    JOIN lot_item_aliases ON lot_item_aliases.lot_id = lots.id
                    WHERE lot_item_aliases.playerok_item_id = ?
                    """,
                    (playerok_item_id,),
                ).fetchone()
            return self._lot(row)

    def rebind_lot_item(self, lot_id: int, item: PlayerokItem) -> tuple[Lot, str]:
        """Moves a lot to a relisted Playerok item while retaining its old ID as an alias."""
        now = utc_now()
        with self._lock:
            current_row = self._conn.execute(
                "SELECT * FROM lots WHERE id = ?", (lot_id,)
            ).fetchone()
            current = self._lot(current_row)
            if current is None:
                raise ValueError("Сопоставление лота не найдено")
            if current.playerok_item_id == item.id:
                return current, current.playerok_item_id

            occupied = self._conn.execute(
                "SELECT id FROM lots WHERE playerok_item_id = ? AND id != ?",
                (item.id, lot_id),
            ).fetchone()
            alias = self._conn.execute(
                "SELECT lot_id FROM lot_item_aliases WHERE playerok_item_id = ?",
                (item.id,),
            ).fetchone()
            if occupied or (alias and int(alias["lot_id"]) != lot_id):
                raise ValueError("Новый ID Playerok уже привязан к другому лоту")

            old_item_id = current.playerok_item_id
            self._conn.execute(
                """
                INSERT OR IGNORE INTO lot_item_aliases(playerok_item_id, lot_id, created_at)
                VALUES(?, ?, ?)
                """,
                (old_item_id, lot_id, now),
            )
            self._conn.execute(
                "DELETE FROM lot_item_aliases WHERE playerok_item_id = ? AND lot_id = ?",
                (item.id, lot_id),
            )
            self._conn.execute(
                """
                UPDATE lots SET playerok_item_id = ?, playerok_slug = ?,
                    playerok_name = ?, updated_at = ? WHERE id = ?
                """,
                (item.id, item.slug, item.name, now, lot_id),
            )
            self._conn.commit()
            updated = self.get_lot(lot_id)
            if updated is None:
                raise RuntimeError("Не удалось сохранить новый ID лота")
            return updated, old_item_id

    def get_lot(self, lot_id: int) -> Lot | None:
        with self._lock:
            return self._lot(
                self._conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
            )

    def list_lots(self) -> list[Lot]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM lots ORDER BY id DESC").fetchall()
            return [self._lot(row) for row in rows if row]  # type: ignore[misc]

    def toggle_lot(self, lot_id: int) -> Lot | None:
        with self._lock:
            self._conn.execute(
                "UPDATE lots SET enabled = NOT enabled, updated_at = ? WHERE id = ?",
                (utc_now(), lot_id),
            )
            self._conn.commit()
            return self.get_lot(lot_id)

    def delete_lot(self, lot_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM lots WHERE id = ?", (lot_id,))
            self._conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _order(row: sqlite3.Row | None) -> Order | None:
        if not row:
            return None
        return Order(
            deal_id=row["deal_id"],
            playerok_item_id=row["playerok_item_id"],
            chat_id=row["chat_id"],
            buyer_id=row["buyer_id"],
            buyer_username=row["buyer_username"],
            kosell_product_id=row["kosell_product_id"],
            product_name=row["product_name"],
            duration_hours=row["duration_hours"],
            rental_uid=row["rental_uid"],
            status=row["status"],
            expires_at=row["expires_at"],
            credentials_sent_at=row["credentials_sent_at"],
            mark_sent_at=row["mark_sent_at"],
            retry_count=row["retry_count"],
            next_retry_at=row["next_retry_at"],
            last_error=row["last_error"],
            provider=str(row["provider"] or "kosell"),
            service_type=str(row["service_type"] or "rental"),
            service_config=str(row["service_config"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_order(self, paid: PaidOrder, lot: Lot, paused: bool = False) -> tuple[Order, bool]:
        now = utc_now()
        status = "paused" if paused else "processing"
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO orders(
                    deal_id, playerok_item_id, chat_id, buyer_id, buyer_username,
                    kosell_product_id, product_name, duration_hours, status,
                    next_retry_at, provider, service_type, service_config, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    paid.deal_id,
                    paid.item_id,
                    paid.chat_id,
                    paid.buyer_id,
                    paid.buyer_username,
                    lot.kosell_product_id,
                    lot.kosell_product_name,
                    lot.duration_hours,
                    status,
                    now,
                    lot.provider,
                    lot.service_type,
                    lot.service_config,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            order = self.get_order(paid.deal_id)
            if order is None:
                raise RuntimeError("Не удалось сохранить заказ")
            return order, cur.rowcount > 0

    def get_order(self, deal_id: str) -> Order | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orders WHERE deal_id = ?", (deal_id,)
            ).fetchone()
            return self._order(row)

    def set_rental(self, deal_id: str, rental_uid: str, expires_at: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE orders SET rental_uid = ?, expires_at = ?, status = 'processing',
                    last_error = NULL, updated_at = ?
                WHERE deal_id = ?
                """,
                (rental_uid, expires_at, utc_now(), deal_id),
            )
            self._conn.commit()

    def update_order_service_config(self, deal_id: str, config: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET service_config = ?, updated_at = ? WHERE deal_id = ?",
                (json.dumps(config, ensure_ascii=False), utc_now(), deal_id),
            )
            self._conn.commit()

    def mark_delivered(self, deal_id: str) -> None:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE orders SET status = 'delivered', credentials_sent_at = ?,
                    next_retry_at = NULL, last_error = NULL, updated_at = ?
                WHERE deal_id = ?
                """,
                (now, now, deal_id),
            )
            self._conn.commit()

    def mark_playerok_sent(self, deal_id: str) -> None:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET mark_sent_at = ?, updated_at = ? WHERE deal_id = ?",
                (now, now, deal_id),
            )
            self._conn.commit()

    def schedule_retry(self, deal_id: str, error: str, next_retry_at: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE orders SET status = 'retry', retry_count = retry_count + 1,
                    next_retry_at = ?, last_error = ?, updated_at = ?
                WHERE deal_id = ? AND status NOT IN ('delivered', 'terminated', 'refunded')
                """,
                (next_retry_at, error[:1000], utc_now(), deal_id),
            )
            self._conn.commit()

    def mark_failed(self, deal_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE orders SET status = 'failed', next_retry_at = NULL,
                    last_error = ?, updated_at = ? WHERE deal_id = ?
                """,
                (error[:1000], utc_now(), deal_id),
            )
            self._conn.commit()

    def mark_terminated(self, deal_id: str, result: str, refunded: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE orders SET status = ?, termination_result = ?,
                    next_retry_at = NULL, updated_at = ? WHERE deal_id = ?
                """,
                ("refunded" if refunded else "terminated", result[:2000], utc_now(), deal_id),
            )
            self._conn.commit()

    def mark_refund_pending(self, deal_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE orders SET status = 'refund_pending', next_retry_at = NULL,
                    updated_at = ? WHERE deal_id = ?
                """,
                (utc_now(), deal_id),
            )
            self._conn.commit()

    def due_orders(self, now: str, retry_max: int) -> list[Order]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM orders
                WHERE status IN ('processing', 'retry', 'paused')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                  AND retry_count < ?
                ORDER BY created_at ASC
                """,
                (now, retry_max),
            ).fetchall()
            return [self._order(row) for row in rows if row]  # type: ignore[misc]

    def due_expirations(self, now: str) -> list[Order]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM orders
                WHERE (status = 'delivered' AND expires_at IS NOT NULL AND expires_at <= ?)
                   OR status = 'refund_pending'
                ORDER BY expires_at ASC
                """,
                (now,),
            ).fetchall()
            return [self._order(row) for row in rows if row]  # type: ignore[misc]

    def unsent_playerok_orders(self) -> list[Order]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM orders
                WHERE status = 'delivered' AND mark_sent_at IS NULL
                ORDER BY updated_at ASC LIMIT 20
                """
            ).fetchall()
            return [self._order(row) for row in rows if row]  # type: ignore[misc]

    def active_order_for_chat(self, chat_id: str, now: str) -> Order | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM orders
                WHERE chat_id = ?
                  AND status IN ('processing', 'retry', 'delivered')
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC LIMIT 1
                """,
                (chat_id, now),
            ).fetchone()
            return self._order(row)

    def list_active_orders(self, now: str, limit: int = 20) -> list[Order]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM orders
                WHERE status = 'delivered' AND expires_at > ?
                ORDER BY expires_at ASC LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            return [self._order(row) for row in rows if row]  # type: ignore[misc]

    def stats(self, now: str) -> dict[str, int]:
        with self._lock:
            lot_count = self._conn.execute(
                "SELECT COUNT(*) FROM lots WHERE enabled = 1"
            ).fetchone()[0]
            active = self._conn.execute(
                "SELECT COUNT(*) FROM orders WHERE status = 'delivered' AND expires_at > ?",
                (now,),
            ).fetchone()[0]
            waiting = self._conn.execute(
                "SELECT COUNT(*) FROM orders WHERE status IN ('processing', 'retry', 'paused')"
            ).fetchone()[0]
            return {"lots": lot_count, "active": active, "waiting": waiting}
