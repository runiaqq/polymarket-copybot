"""BP33 service entrypoint: Telegram bot (polling) + signal executor in one
process — the executor reuses the bot's event loop, notifications go straight
to the Bot API.

Run: python -m cryptobot.main   (compose service `cryptobot`)
"""

from __future__ import annotations

import asyncio

import structlog

from core.config import settings

log = structlog.get_logger(__name__)


async def main() -> None:
    if not settings.crypto_bot_enabled or not settings.crypto_bot_token:
        log.warning("crypto_bot_disabled")
        while True:
            await asyncio.sleep(300)

    from cryptobot.bot import COMMANDS, build_application
    from cryptobot.executor import CryptoExecutor

    app = build_application()
    await app.initialize()
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )
    await app.start()
    try:
        await app.bot.set_my_commands(COMMANDS)
    except Exception:
        log.exception("crypto_set_commands_failed")
    log.info("crypto_bot_polling_started")

    await CryptoExecutor().run()


if __name__ == "__main__":
    asyncio.run(main())
