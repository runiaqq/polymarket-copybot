"""
Telegram bot command handlers.
All commands are processed here; the webhook is registered in api/main.py.
"""

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from core.config import settings
from core.db import AsyncSessionLocal, get_user_by_telegram_id, upsert_user
from core.privy import privy_client


def build_application() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("stop", cmd_stop))
    return app


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    async with AsyncSessionLocal() as session:
        db_user = await upsert_user(session, user.id)

        if not db_user.wallet_address:
            # Create embedded wallet via Privy (first-time only)
            await update.message.reply_text(  # type: ignore[union-attr]
                "Создаём твой кошелёк... (5–10 секунд)"
            )
            wallet_data = await privy_client.create_wallet(user.id)
            db_user.privy_user_id = wallet_data["privy_user_id"]
            db_user.wallet_address = wallet_data["wallet_address"]
            await session.commit()

            await update.message.reply_text(  # type: ignore[union-attr]
                f"Готово! Твой кошелёк:\n`{db_user.wallet_address}`\n\n"
                "Пополни его USDC на сети Polygon чтобы начать копирование.\n\n"
                "Команды:\n"
                "/subscribe — оформить подписку\n"
                "/wallet — информация о кошельке\n"
                "/positions — открытые позиции\n"
                "/pnl — статистика прибыли\n"
                "/stop — остановить копирование",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"Привет! Твой кошелёк:\n`{db_user.wallet_address}`\n\n"
                f"Статус подписки: {db_user.sub_tier.value}\n"
                f"Копирование: {'включено' if db_user.copy_active else 'выключено'}",
                parse_mode="Markdown",
            )


# ── /wallet ───────────────────────────────────────────────────────────────────

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    async with AsyncSessionLocal() as session:
        db_user = await get_user_by_telegram_id(session, user.id)

    if not db_user or not db_user.wallet_address:
        await update.message.reply_text("Сначала отправь /start")  # type: ignore[union-attr]
        return

    polygonscan = f"https://polygonscan.com/address/{db_user.wallet_address}"
    await update.message.reply_text(  # type: ignore[union-attr]
        f"Твой кошелёк (Polygon):\n`{db_user.wallet_address}`\n\n"
        f"[Посмотреть на Polygonscan]({polygonscan})\n\n"
        "Пополняй только через сеть Polygon (MATIC gas + USDC).",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ── /subscribe ────────────────────────────────────────────────────────────────

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(  # type: ignore[union-attr]
        "Тарифы:\n\n"
        "Basic — $9.99/мес\n"
        "Копирование всех сигналов\n\n"
        "Pro — $19.99/мес\n"
        "Копирование + приоритет исполнения\n\n"
        "Whale — $49.99/мес\n"
        "Максимальный размер позиций\n\n"
        "Оплата через Telegram Stars (скоро)."
    )


# ── /positions ────────────────────────────────────────────────────────────────

async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        from core.db.models import CopyTrade, TradeSignal, TradeStatus, User

        result = await session.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            await update.message.reply_text("Сначала отправь /start")  # type: ignore[union-attr]
            return

        result = await session.execute(
            select(CopyTrade, TradeSignal)
            .join(TradeSignal)
            .where(
                CopyTrade.user_id == db_user.id,
                CopyTrade.status == TradeStatus.CONFIRMED,
                CopyTrade.pnl_usdc == None,  # noqa: E711  — still open
            )
            .order_by(CopyTrade.created_at.desc())
            .limit(10)
        )
        rows = result.all()

    if not rows:
        await update.message.reply_text("Нет открытых позиций.")  # type: ignore[union-attr]
        return

    lines = ["Открытые позиции:\n"]
    for trade, signal in rows:
        lines.append(
            f"• {signal.market_id[:30]}...\n"
            f"  {signal.side} @ {signal.price:.4f} | ${trade.size_usdc:.2f} USDC"
        )

    await update.message.reply_text("\n".join(lines))  # type: ignore[union-attr]


# ── /pnl ──────────────────────────────────────────────────────────────────────

async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    async with AsyncSessionLocal() as session:
        from sqlalchemy import func, select

        from core.db.models import CopyTrade, TradeStatus, User

        result = await session.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            await update.message.reply_text("Сначала отправь /start")  # type: ignore[union-attr]
            return

        result = await session.execute(
            select(
                func.count(CopyTrade.id).label("total"),
                func.sum(CopyTrade.pnl_usdc).label("total_pnl"),
                func.sum(CopyTrade.size_usdc).label("total_volume"),
            ).where(CopyTrade.user_id == db_user.id, CopyTrade.status == TradeStatus.CONFIRMED)
        )
        stats = result.one()

    total = stats.total or 0
    pnl = stats.total_pnl or 0.0
    volume = stats.total_volume or 0.0

    await update.message.reply_text(  # type: ignore[union-attr]
        f"Статистика:\n\n"
        f"Всего сделок: {total}\n"
        f"Объём: ${volume:.2f} USDC\n"
        f"P&L: {'+' if pnl >= 0 else ''}{pnl:.2f} USDC"
    )


# ── /settings ─────────────────────────────────────────────────────────────────

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    async with AsyncSessionLocal() as session:
        db_user = await get_user_by_telegram_id(session, user.id)

    if not db_user:
        await update.message.reply_text("Сначала отправь /start")  # type: ignore[union-attr]
        return

    await update.message.reply_text(  # type: ignore[union-attr]
        f"Настройки:\n\n"
        f"Макс. размер позиции: ${db_user.max_position_usdc:.2f} USDC\n"
        f"Копирование: {'включено' if db_user.copy_active else 'выключено'}\n\n"
        "Для изменения напиши:\n"
        "/settings max 50 — изменить макс. позицию на $50\n"
        "/settings pause — приостановить копирование\n"
        "/settings resume — возобновить копирование"
    )

    # Handle inline args: /settings max 50
    if context.args:
        async with AsyncSessionLocal() as session:
            db_user = await get_user_by_telegram_id(session, user.id)
            if not db_user:
                return
            args = context.args
            if args[0] == "max" and len(args) > 1:
                try:
                    db_user.max_position_usdc = float(args[1])
                    await session.commit()
                    await update.message.reply_text(  # type: ignore[union-attr]
                        f"Макс. позиция установлена: ${db_user.max_position_usdc:.2f} USDC"
                    )
                except ValueError:
                    await update.message.reply_text("Неверное значение")  # type: ignore[union-attr]
            elif args[0] == "pause":
                db_user.copy_active = False
                await session.commit()
                await update.message.reply_text("Копирование приостановлено.")  # type: ignore[union-attr]
            elif args[0] == "resume":
                db_user.copy_active = True
                await session.commit()
                await update.message.reply_text("Копирование возобновлено.")  # type: ignore[union-attr]


# ── /stop ─────────────────────────────────────────────────────────────────────

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    async with AsyncSessionLocal() as session:
        db_user = await get_user_by_telegram_id(session, user.id)
        if db_user:
            db_user.copy_active = False
            await session.commit()

    await update.message.reply_text(  # type: ignore[union-attr]
        "Копирование остановлено. Открытые позиции НЕ закрываются автоматически.\n"
        "Чтобы возобновить: /settings resume"
    )
