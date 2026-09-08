from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Lot:
    id: int
    playerok_item_id: str
    playerok_slug: str | None
    playerok_name: str
    kosell_product_id: int
    kosell_product_name: str
    duration_hours: int
    enabled: bool
    provider: str = "kosell"
    service_type: str = "rental"
    service_config: str = "{}"


@dataclass(frozen=True, slots=True)
class Order:
    deal_id: str
    playerok_item_id: str
    chat_id: str
    buyer_id: str | None
    buyer_username: str | None
    kosell_product_id: int
    product_name: str
    duration_hours: int
    rental_uid: str | None
    status: str
    expires_at: str | None
    credentials_sent_at: str | None
    mark_sent_at: str | None
    retry_count: int
    next_retry_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str
    provider: str = "kosell"
    service_type: str = "rental"
    service_config: str = "{}"


@dataclass(frozen=True, slots=True)
class PaidOrder:
    deal_id: str
    item_id: str
    chat_id: str
    buyer_id: str | None
    buyer_username: str | None
    item_slug: str | None = None
    item_name: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerokItem:
    id: str
    slug: str | None
    name: str
