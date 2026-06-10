import hmac
import hashlib
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from telegram import Update

from api.routers.admin import router as admin_router
from api.routers.telegram import build_application
from core.config import settings

log = structlog.get_logger(__name__)

# Build telegram application once at startup
tg_app = build_application()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await tg_app.initialize()
    log.info("telegram_app_initialized")
    yield
    await tg_app.shutdown()


app = FastAPI(title="Polymarket CopyBot API", lifespan=lifespan)
app.include_router(admin_router)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ── Telegram webhook ──────────────────────────────────────────────────────────

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> Response:
    # Validate the secret token Telegram sends in the header
    secret_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret_token, settings.telegram_webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid secret token")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return Response(status_code=200)


# ── Register webhook on startup (call once manually or on deploy) ─────────────

@app.post("/internal/register-webhook")
async def register_webhook() -> dict:
    url = f"{settings.webhook_base_url}/webhook/telegram"
    await tg_app.bot.set_webhook(
        url=url,
        secret_token=settings.telegram_webhook_secret,
        allowed_updates=["message", "callback_query"],
    )
    log.info("webhook_registered", url=url)
    return {"webhook_url": url}
