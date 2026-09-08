from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(ValueError):
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on", "да"}:
        return True
    if value in {"0", "false", "no", "off", "нет"}:
        return False
    raise ConfigError(f"{name}: ожидается true или false")


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидается целое число") from exc
    if value < minimum:
        raise ConfigError(f"{name}: значение должно быть не меньше {minimum}")
    return value


def _float(name: str, default: float, minimum: float = 0) -> float:
    raw = os.getenv(name, str(default)).strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидается число") from exc
    if value < minimum:
        raise ConfigError(f"{name}: значение должно быть не меньше {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    kosell_api_key: str
    steamsmm_api_token: str
    steamsmm_base_url: str
    kosell_base_url: str
    rent_currency: str
    low_balance_rub: float
    balance_check_seconds: int
    low_balance_repeat_seconds: int
    telegram_bot_token: str
    telegram_owner_id: int
    playerok_cookies: str
    playerok_user_agent: str
    playerok_proxy: str | None
    auto_mark_sent: bool
    auto_relist_sync: bool
    relist_sync_seconds: int
    bot_enabled_default: bool
    display_timezone: str
    database_path: Path
    order_retry_seconds: int
    order_retry_max: int
    expiry_check_seconds: int
    steam_code_cooldown_seconds: int
    log_level: str

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Config":
        load_dotenv(env_file, override=False)

        required = {
            "KOSELL_API_KEY": os.getenv("KOSELL_API_KEY", "").strip(),
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            "TELEGRAM_OWNER_ID": os.getenv("TELEGRAM_OWNER_ID", "").strip(),
            "PLAYEROK_COOKIES": os.getenv("PLAYEROK_COOKIES", "").strip(),
            "PLAYEROK_USER_AGENT": os.getenv("PLAYEROK_USER_AGENT", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigError("Не заполнены обязательные поля: " + ", ".join(missing))

        try:
            owner_id = int(required["TELEGRAM_OWNER_ID"])
        except ValueError as exc:
            raise ConfigError("TELEGRAM_OWNER_ID должен быть цифровым ID") from exc
        if owner_id <= 0:
            raise ConfigError("TELEGRAM_OWNER_ID должен быть больше нуля")

        currency = os.getenv("RENT_CURRENCY", "RUB").strip().upper()
        if currency not in {"RUB", "USD"}:
            raise ConfigError("RENT_CURRENCY должен быть RUB или USD")

        timezone = os.getenv("DISPLAY_TIMEZONE", "Europe/Moscow").strip()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"Неизвестный DISPLAY_TIMEZONE: {timezone}") from exc

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("LOG_LEVEL должен быть DEBUG, INFO, WARNING, ERROR или CRITICAL")

        database_path = Path(os.getenv("DATABASE_PATH", "data/kosell_rental.sqlite3")).expanduser()

        return cls(
            kosell_api_key=required["KOSELL_API_KEY"],
            steamsmm_api_token=os.getenv("STEAM_SMM_API_TOKEN", "").strip(),
            steamsmm_base_url=os.getenv("STEAM_SMM_BASE_URL", "https://steamsmm.ru/api").strip().rstrip("/"),
            kosell_base_url=os.getenv(
                "KOSELL_BASE_URL", "https://kosell.store/api/v1"
            ).strip().rstrip("/"),
            rent_currency=currency,
            low_balance_rub=_float("LOW_BALANCE_RUB", 100.0),
            balance_check_seconds=_int("BALANCE_CHECK_SECONDS", 300, 30),
            low_balance_repeat_seconds=_int("LOW_BALANCE_REPEAT_SECONDS", 21600, 300),
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            telegram_owner_id=owner_id,
            playerok_cookies=required["PLAYEROK_COOKIES"],
            playerok_user_agent=required["PLAYEROK_USER_AGENT"],
            playerok_proxy=os.getenv("PLAYEROK_PROXY", "").strip() or None,
            auto_mark_sent=_bool("AUTO_MARK_SENT", True),
            auto_relist_sync=_bool("AUTO_RELIST_SYNC", True),
            relist_sync_seconds=_int("RELIST_SYNC_SECONDS", 60, 30),
            bot_enabled_default=_bool("BOT_ENABLED_DEFAULT", True),
            display_timezone=timezone,
            database_path=database_path,
            order_retry_seconds=_int("ORDER_RETRY_SECONDS", 60, 10),
            order_retry_max=_int("ORDER_RETRY_MAX", 15, 1),
            expiry_check_seconds=_int("EXPIRY_CHECK_SECONDS", 20, 5),
            steam_code_cooldown_seconds=_int("STEAM_CODE_COOLDOWN_SECONDS", 5, 1),
            log_level=log_level,
        )


def redact(value: str, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible * 2:
        return "***"
    return f"{value[:visible]}…{value[-visible:]}"
