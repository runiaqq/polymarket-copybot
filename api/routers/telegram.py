from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from core.config import settings
from core.db import (
    get_user_by_telegram_id,
    get_user_open_positions,
    get_user_pnl_stats,
    update_user,
    upsert_user,
)
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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    db_user = upsert_user(user.id)

    if not db_user.get("wallet_address"):
        await update.message.reply_text("Создаём твой кошелёк... (5–10 секунд)")  # type: ignore[union-attr]

        wallet_data = await privy_client.create_wallet(user.id)
        update_user(user.id, {
            "privy_user_id": wallet_data["privy_user_id"],
            "wallet_address": wallet_data["wallet_address"],
        })
        db_user = get_user_by_telegram_id(user.id)

        await update.message.reply_text(  # type: ignore[union-attr]
            f"Готово! Твой кошелёк:\n`{db_user['wallet_address']}`\n\n"
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
            f"Привет! Твой кошелёк:\n`{db_user['wallet_address']}`\n\n"
            f"Статус подписки: {db_user['sub_tier']}\n"
            f"Копирование: {'включено' if db_user['copy_active'] else 'выключено'}",
            parse_mode="Markdown",
        )


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    db_user = get_user_by_telegram_id(user.id)
    if not db_user or not db_user.get("wallet_address"):
        await update.message.reply_text("Сначала отправь /start")  # type: ignore[union-attr]
        return

    addr = db_user["wallet_address"]
    await update.message.reply_text(  # type: ignore[union-attr]
        f"Твой кошелёк (Polygon):\n`{addr}`\n\n"
        f"[Посмотреть на Polygonscan](https://polygonscan.com/address/{addr})\n\n"
        "Пополняй только через сеть Polygon (MATIC gas + USDC).",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(  # type: ignore[union-attr]
        "Тарифы:\n\n"
        "Basic — $9.99/мес\nКопирование всех сигналов\n\n"
        "Pro — $19.99/мес\nКопирование + приоритет исполнения\n\n"
        "Whale — $49.99/мес\nМаксимальный размер позиций\n\n"
        "Оплата через Telegram Stars (скоро)."
    )


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    db_user = get_user_by_telegram_id(user.id)
    if not db_user:
        await update.message.reply_text("Сначала отправь /start")  # type: ignore[union-attr]
        return

    positions = get_user_open_positions(db_user["id"])
    if not positions:
        await update.message.reply_text("Нет открытых позиций.")  # type: ignore[union-attr]
        return

    lines = ["Открытые позиции:\n"]
    for trade in positions:
        sig = trade.get("trade_signals", {})
        lines.append(
            f"• {str(sig.get('market_id', ''))[:30]}...\n"
            f"  {sig.get('side')} @ {sig.get('price', 0):.4f} | ${trade.get('size_usdc', 0):.2f} USDC"
        )
    await update.message.reply_text("\n".join(lines))  # type: ignore[union-attr]


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    db_user = get_user_by_telegram_id(user.id)
    if not db_user:
        await update.message.reply_text("Сначала отправь /start")  # type: ignore[union-attr]
        return

    stats = get_user_pnl_stats(db_user["id"])
    pnl = stats["pnl"]
    await update.message.reply_text(  # type: ignore[union-attr]
        f"Статистика:\n\n"
        f"Всего сделок: {stats['total']}\n"
        f"Объём: ${stats['volume']:.2f} USDC\n"
        f"P&L: {'+' if pnl >= 0 else ''}{pnl:.2f} USDC"
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    db_user = get_user_by_telegram_id(user.id)
    if not db_user:
        await update.message.reply_text("Сначала отправь /start")  # type: ignore[union-attr]
        return

    if context.args:
        args = context.args
        if args[0] == "max" and len(args) > 1:
            try:
                update_user(user.id, {"max_position_usdc": float(args[1])})
                await update.message.reply_text(f"Макс. позиция: ${float(args[1]):.2f} USDC")  # type: ignore[union-attr]
            except ValueError:
                await update.message.reply_text("Неверное значение")  # type: ignore[union-attr]
        elif args[0] == "pause":
            update_user(user.id, {"copy_active": False})
            await update.message.reply_text("Копирование приостановлено.")  # type: ignore[union-attr]
        elif args[0] == "resume":
            update_user(user.id, {"copy_active": True})
            await update.message.reply_text("Копирование возобновлено.")  # type: ignore[union-attr]
        return

    await update.message.reply_text(  # type: ignore[union-attr]
        f"Настройки:\n\n"
        f"Макс. размер позиции: ${db_user.get('max_position_usdc', 25):.2f} USDC\n"
        f"Копирование: {'включено' if db_user.get('copy_active') else 'выключено'}\n\n"
        "/settings max 50 — изменить макс. позицию\n"
        "/settings pause — приостановить\n"
        "/settings resume — возобновить"
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    update_user(user.id, {"copy_active": False})
    await update.message.reply_text(  # type: ignore[union-attr]
        "Копирование остановлено. Открытые позиции НЕ закрываются автоматически.\n"
        "Чтобы возобновить: /settings resume"
    )
