from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from aiogram import Bot

from . import __version__
from .config import Config, ConfigError
from .kosell import KosellClient
from .playerok import PlayerokGateway
from .service import RentalService


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def check_config(config: Config) -> int:
    print("Конфигурация .env: OK")

    kosell = KosellClient(config.kosell_api_key, config.kosell_base_url)
    try:
        balance = await kosell.get_balance()
        print(
            f"KOSell: OK, пользователь {balance.username}, "
            f"баланс {balance.rub:.2f} ₽ / ${balance.usd:.2f}"
        )
    finally:
        await kosell.close()

    bot = Bot(config.telegram_bot_token)
    try:
        me = await bot.get_me()
        print(f"Telegram: OK, @{me.username}")
    finally:
        await bot.session.close()

    async def ignore_event(_: object) -> None:
        return None

    playerok = PlayerokGateway(
        cookies=config.playerok_cookies,
        user_agent=config.playerok_user_agent,
        proxy=config.playerok_proxy,
        event_handler=ignore_event,
    )
    username = await playerok.check_connection()
    print(f"Playerok: OK, {username}")
    print("Проверка завершена. Аренда не создавалась.")
    return 0


async def run(config: Config) -> None:
    service = RentalService(config)
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        if not service._stopping:
            asyncio.create_task(service.dispatcher.stop_polling())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    await service.run()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="eucalrent — KOSell × Playerok")
    parser.add_argument("--check", action="store_true", help="проверить настройки без запуска")
    parser.add_argument("--env", default=".env", help="путь к .env")
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        config = Config.load(args.env)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    configure_logging(config.log_level)
    try:
        if args.check:
            raise SystemExit(asyncio.run(check_config(config)))
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.getLogger(__name__).exception("Бот остановлен из-за ошибки")
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
