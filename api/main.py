import asyncio
import hmac
import threading
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from telegram import BotCommand, Update

from api.routers.admin import router as admin_router
from api.routers.admin_bot import build_admin_application
from api.routers.telegram import build_application
from core.config import settings

log = structlog.get_logger(__name__)


def _check_core_imports() -> None:
    """BP9/BP12 Layer 2b: fail loud at boot if core.db exports are missing.

    Catches the 'release drift' class of bugs (untracked files not deployed)
    at container start time instead of silently at periodic-task runtime.
    """
    import core.db as _db
    missing = [n for n in _db.__all__ if not hasattr(_db, n)]
    _REQUIRED = {
        "get_open_trade_by_token",
        "mark_trade_closed",
        "mark_trade_settled",
        "has_terminal_trade",
        "get_outstanding_copy_trades",
        "get_open_trades_cost",
        "get_supabase",
    }
    missing += sorted(_REQUIRED - set(dir(_db)))
    if missing:
        raise ImportError(
            f"core.db is missing required exports: {missing}. "
            "Apply pending migrations and rebuild the image."
        )


_check_core_imports()

# Build the main bot application once at startup.
tg_app = build_application()
# Optional admin bot (only if a token is configured).
admin_app = build_admin_application()

# Commands shown in the Telegram "/" menu (next to the input field).
MAIN_COMMANDS = [
    BotCommand("start", "Запуск / активация"),
    BotCommand("wallet", "Кошелёк и баланс"),
    BotCommand("balance", "Балансы (pUSD/USDC/POL)"),
    BotCommand("positions", "Открытые позиции"),
    BotCommand("pnl", "Доход за период"),
    BotCommand("register", "Регистрация кошелька"),
    BotCommand("wrap", "Конвертировать в pUSD"),
    BotCommand("withdraw", "Вывод средств"),
    BotCommand("subscription", "Статус подписки"),
    BotCommand("settings", "Настройки"),
    BotCommand("stop", "Пауза копирования"),
    BotCommand("resume", "Возобновить копирование"),
    BotCommand("help", "Помощь"),
]
ADMIN_COMMANDS = [
    BotCommand("top", "🔥 Топ китов (неделя)"),
    BotCommand("wallets", "📋 Мои кошельки"),
    BotCommand("refresh", "♻️ Обновить белый список"),
    BotCommand("addwallet", "Добавить кошелёк"),
    BotCommand("delwallet", "Убрать кошелёк"),
    BotCommand("grant", "Выдать/продлить подписку"),
    BotCommand("newcode", "Код-ссылка для клиента"),
    BotCommand("subs", "Активные подписчики"),
    BotCommand("user", "Инфо о пользователе"),
    BotCommand("addadmin", "Пригласить админа"),
    BotCommand("admins", "Список админов"),
    BotCommand("deladmin", "Убрать админа"),
    BotCommand("help", "Все команды"),
]


async def _set_commands() -> None:
    """Publish the slash-command menus so they appear next to the input field."""
    try:
        await tg_app.bot.set_my_commands(MAIN_COMMANDS)
        if admin_app:
            await admin_app.bot.set_my_commands(ADMIN_COMMANDS)
        log.info("bot_commands_published")
    except Exception:
        log.exception("set_commands_failed")


def _run_polling(app, name: str) -> None:
    """Run a telegram Application in polling mode in its own event loop thread."""
    async def _start() -> None:
        await app.initialize()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        await app.start()
        # Publish this bot's command menu.
        cmds = ADMIN_COMMANDS if name == "admin_bot" else MAIN_COMMANDS
        try:
            await app.bot.set_my_commands(cmds)
        except Exception:
            log.exception(f"{name}_set_commands_failed")
        log.info(f"{name}_polling_started")
        # Keep alive until the process exits.
        stop_event = asyncio.Event()
        await stop_event.wait()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_start())
    except Exception:
        log.exception(f"{name}_polling_crashed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if settings.use_polling:
        # Polling mode: each bot runs in its own background thread with its own loop.
        t_main = threading.Thread(target=_run_polling, args=(tg_app, "main_bot"),
                                  daemon=True, name="tg-poll-main")
        t_main.start()
        if admin_app:
            t_admin = threading.Thread(target=_run_polling, args=(admin_app, "admin_bot"),
                                       daemon=True, name="tg-poll-admin")
            t_admin.start()
        log.info("polling_mode_active")
    else:
        # Webhook mode: standard initialization, updates come via POST /webhook/*.
        await tg_app.initialize()
        log.info("telegram_app_initialized")
        if admin_app:
            await admin_app.initialize()
            log.info("admin_app_initialized")
        await _set_commands()
    yield
    if not settings.use_polling:
        await tg_app.shutdown()
        if admin_app:
            await admin_app.shutdown()


app = FastAPI(title="Polymarket CopyBot API", lifespan=lifespan)
app.include_router(admin_router)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ── Telegram webhook (main bot) ───────────────────────────────────────────────

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> Response:
    secret_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret_token, settings.telegram_webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid secret token")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return Response(status_code=200)


# ── Telegram webhook (admin bot) ──────────────────────────────────────────────

@app.post("/webhook/admin")
async def admin_webhook(request: Request) -> Response:
    if not admin_app:
        raise HTTPException(status_code=404, detail="Admin bot not configured")
    secret_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret_token, settings.telegram_admin_webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid secret token")

    data = await request.json()
    update = Update.de_json(data, admin_app.bot)
    await admin_app.process_update(update)
    return Response(status_code=200)


# ── Register webhooks (call once manually or on deploy) ───────────────────────

@app.post("/internal/register-webhook")
async def register_webhook() -> dict:
    url = f"{settings.webhook_base_url}/webhook/telegram"
    await tg_app.bot.set_webhook(
        url=url,
        secret_token=settings.telegram_webhook_secret,
        allowed_updates=["message", "callback_query"],
    )
    result = {"webhook_url": url}
    log.info("webhook_registered", url=url)

    if admin_app:
        admin_url = f"{settings.webhook_base_url}/webhook/admin"
        await admin_app.bot.set_webhook(
            url=admin_url,
            secret_token=settings.telegram_admin_webhook_secret,
            allowed_updates=["message", "callback_query"],
        )
        result["admin_webhook_url"] = admin_url
        log.info("admin_webhook_registered", url=admin_url)

    return result
