import hmac
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from telegram import Update

from api.routers.admin import router as admin_router
from api.routers.admin_bot import build_admin_application
from api.routers.telegram import build_application
from core.config import settings

log = structlog.get_logger(__name__)

# Build the main bot application once at startup.
tg_app = build_application()
# Optional admin bot (only if a token is configured).
admin_app = build_admin_application()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await tg_app.initialize()
    log.info("telegram_app_initialized")
    if admin_app:
        await admin_app.initialize()
        log.info("admin_app_initialized")
    yield
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
            allowed_updates=["message"],
        )
        result["admin_webhook_url"] = admin_url
        log.info("admin_webhook_registered", url=admin_url)

    return result
