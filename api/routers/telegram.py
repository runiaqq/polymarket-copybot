"""
Telegram bot handlers — production-ready UI with inline keyboards.
"""

import structlog
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from core.config import settings
from core.db import (
    get_user_by_telegram_id,
    get_user_open_positions,
    get_user_pnl_stats,
    update_user,
    upsert_user,
)
from core.privy import privy_client

log = structlog.get_logger(__name__)

# ─── Keyboards ────────────────────────────────────────────────────────────────

def _main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💼 Кошелёк", callback_data="wallet"),
            InlineKeyboardButton("📊 Позиции", callback_data="positions"),
        ],
        [
            InlineKeyboardButton("💰 P&L", callback_data="pnl"),
            InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
        ],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
    ])


def _stop_resume_kb(copy_active: bool) -> InlineKeyboardMarkup:
    if copy_active:
        btn = InlineKeyboardButton("⏸ Приостановить", callback_data="stop")
    else:
        btn = InlineKeyboardButton("▶️ Возобновить", callback_data="resume")
    return InlineKeyboardMarkup([[btn], [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")]])


# ─── Text builders ────────────────────────────────────────────────────────────

def _dashboard_text(db_user: dict, first_name: str) -> str:
    addr = db_user.get("wallet_address", "—")
    addr_short = f"{addr[:6]}…{addr[-4:]}" if addr != "—" else "—"
    copy_icon = "▶️ Включено" if db_user.get("copy_active") else "⏸ Приостановлено"
    max_pos = db_user.get("max_position_usdc") or 25
    return (
        f"👋 <b>Привет, {first_name}!</b>\n\n"
        "🤖 <b>Polymarket AI Copy-Trader</b>\n\n"
        f"💼 Кошелёк: <code>{addr_short}</code>\n"
        f"🔄 Копирование: {copy_icon}\n"
        f"💵 Макс. позиция: <b>${max_pos:.0f} USDC</b>\n\n"
        "Используй кнопки ниже для управления ботом 👇"
    )


def _new_user_text(addr: str) -> str:
    return (
        "🎉 <b>Кошелёк создан!</b>\n\n"
        f"<code>{addr}</code>\n\n"
        "📌 <b>Следующий шаг:</b> пополни кошелёк в сети <b>Polygon</b>\n"
        "Принимается: <b>USDC</b> (минимум $10)\n\n"
        "⚡️ После пополнения бот автоматически начнёт отслеживать "
        "сделки топ-трейдеров и копировать лучшие из них.\n\n"
        "Нажми <b>💼 Кошелёк</b> чтобы увидеть детали для пополнения 👇"
    )


HELP_TEXT = (
    "❓ <b>Инструкция по использованию</b>\n\n"

    "━━━━━━━━━━━━━━━━━━━━━\n"
    "⚡️ <b>Как работает бот</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "1️⃣ Бот отслеживает сделки топ-трейдеров Polymarket в реальном времени\n"
    "2️⃣ ИИ анализирует каждую сделку и оценивает риск\n"
    "3️⃣ Одобренные сделки автоматически копируются на твой кошелёк\n\n"

    "━━━━━━━━━━━━━━━━━━━━━\n"
    "📋 <b>Команды</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "/start — 🏠 Главное меню и статус\n"
    "/wallet — 💼 Данные кошелька и пополнение\n"
    "/positions — 📊 Текущие открытые позиции\n"
    "/pnl — 💰 Статистика прибыли и убытков\n"
    "/settings — ⚙️ Настройки копирования\n"
    "/stop — ⏸ Приостановить копирование\n"
    "/resume — ▶️ Возобновить копирование\n"
    "/help — ❓ Эта инструкция\n\n"

    "━━━━━━━━━━━━━━━━━━━━━\n"
    "⚙️ <b>Настройки</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "<code>/settings max 50</code> — макс. размер позиции $50 USDC\n"
    "<code>/settings max 100</code> — макс. размер позиции $100 USDC\n\n"

    "━━━━━━━━━━━━━━━━━━━━━\n"
    "💡 <b>Важно знать</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "• Минимальный баланс для торговли: <b>$10 USDC</b>\n"
    "• Сеть: <b>Polygon</b> (не Ethereum!)\n"
    "• /stop не закрывает открытые позиции — только прекращает новые сделки\n"
    "• Средства всегда на твоём кошельке — бот только исполняет сделки\n\n"

    "💬 Вопросы? Напиши администратору."
)


# ─── App builder ──────────────────────────────────────────────────────────────

async def _set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",    "🏠 Главное меню"),
        BotCommand("wallet",   "💼 Мой кошелёк"),
        BotCommand("positions","📊 Открытые позиции"),
        BotCommand("pnl",      "💰 Статистика P&L"),
        BotCommand("settings", "⚙️ Настройки"),
        BotCommand("stop",     "⏸ Приостановить копирование"),
        BotCommand("resume",   "▶️ Возобновить копирование"),
        BotCommand("help",     "❓ Помощь и инструкция"),
    ])


def build_application() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).post_init(_set_commands).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("wallet",    cmd_wallet))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("pnl",       cmd_pnl))
    app.add_handler(CommandHandler("settings",  cmd_settings))
    app.add_handler(CommandHandler("stop",      cmd_stop))
    app.add_handler(CommandHandler("resume",    cmd_resume))
    app.add_handler(CallbackQueryHandler(callback_handler))
    return app


# ─── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return

    db_user = upsert_user(tg_user.id)

    if not db_user.get("wallet_address"):
        await update.message.reply_text(  # type: ignore[union-attr]
            "⏳ Создаём твой кошелёк… (5–10 секунд)",
            parse_mode="HTML",
        )
        try:
            wallet_data = await privy_client.create_wallet(tg_user.id)
            update_user(tg_user.id, {
                "privy_user_id": wallet_data["privy_user_id"],
                "wallet_address": wallet_data["wallet_address"],
            })
            db_user = get_user_by_telegram_id(tg_user.id) or db_user
            addr = db_user.get("wallet_address", "—")
            await update.message.reply_text(  # type: ignore[union-attr]
                _new_user_text(addr),
                parse_mode="HTML",
                reply_markup=_main_kb(),
            )
        except Exception:
            log.exception("wallet_create_failed", telegram_id=tg_user.id)
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Не удалось создать кошелёк. Попробуй ещё раз через минуту.",
                parse_mode="HTML",
            )
    else:
        await update.message.reply_text(  # type: ignore[union-attr]
            _dashboard_text(db_user, tg_user.first_name),
            parse_mode="HTML",
            reply_markup=_main_kb(),
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(  # type: ignore[union-attr]
        HELP_TEXT,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
        ]]),
    )


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user or not db_user.get("wallet_address"):
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    addr = db_user["wallet_address"]
    await update.message.reply_text(  # type: ignore[union-attr]
        f"💼 <b>Твой кошелёк</b>\n\n"
        f"<code>{addr}</code>\n\n"
        f"🔗 <a href=\"https://polygonscan.com/address/{addr}\">Посмотреть на Polygonscan</a>\n\n"
        "📌 Для пополнения переведи <b>USDC</b> в сети <b>Polygon</b> на этот адрес.\n"
        "⚠️ Не отправляй токены из других сетей — средства будут потеряны.",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
        ]]),
    )


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user:
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    positions = get_user_open_positions(db_user["id"])
    if not positions:
        await update.message.reply_text(  # type: ignore[union-attr]
            "📊 <b>Открытые позиции</b>\n\n"
            "Нет открытых позиций.\n\n"
            "💡 Позиции появятся здесь когда бот скопирует сделку.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
            ]]),
        )
        return
    lines = ["📊 <b>Открытые позиции</b>\n"]
    for i, trade in enumerate(positions, 1):
        sig = trade.get("trade_signals") or {}
        market = str(sig.get("market_id", "—"))[:28]
        side = sig.get("side", "—")
        price = sig.get("price", 0)
        size = trade.get("size_usdc", 0)
        side_icon = "🟢" if side == "YES" else "🔴"
        lines.append(
            f"{i}. {side_icon} <b>{side}</b> @ <code>{price:.4f}</code>\n"
            f"   💵 ${size:.2f} USDC | <i>{market}…</i>"
        )
    await update.message.reply_text(  # type: ignore[union-attr]
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
        ]]),
    )


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user:
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    stats = get_user_pnl_stats(db_user["id"])
    pnl: float = stats["pnl"]
    pnl_icon = "📈" if pnl >= 0 else "📉"
    pnl_sign = "+" if pnl >= 0 else ""
    await update.message.reply_text(  # type: ignore[union-attr]
        f"💰 <b>Статистика P&L</b>\n\n"
        f"📋 Всего сделок: <b>{stats['total']}</b>\n"
        f"💵 Объём: <b>${stats['volume']:.2f} USDC</b>\n"
        f"{pnl_icon} P&L: <b>{pnl_sign}{pnl:.2f} USDC</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
        ]]),
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user:
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return

    if context.args:
        arg0 = context.args[0]
        if arg0 == "max" and len(context.args) > 1:
            try:
                val = float(context.args[1])
                if val < 5:
                    await update.message.reply_text("⚠️ Минимум $5 USDC", parse_mode="HTML")  # type: ignore[union-attr]
                    return
                update_user(tg_user.id, {"max_position_usdc": val})
                await update.message.reply_text(  # type: ignore[union-attr]
                    f"✅ Макс. размер позиции обновлён: <b>${val:.0f} USDC</b>",
                    parse_mode="HTML",
                )
            except ValueError:
                await update.message.reply_text("⚠️ Укажи число. Например: <code>/settings max 50</code>", parse_mode="HTML")  # type: ignore[union-attr]
        return

    copy_active = db_user.get("copy_active", False)
    max_pos = db_user.get("max_position_usdc") or 25
    copy_status = "▶️ Включено" if copy_active else "⏸ Приостановлено"
    await update.message.reply_text(  # type: ignore[union-attr]
        f"⚙️ <b>Настройки</b>\n\n"
        f"🔄 Копирование: {copy_status}\n"
        f"💵 Макс. позиция: <b>${max_pos:.0f} USDC</b>\n\n"
        f"<i>Изменить макс. позицию:</i>\n"
        f"<code>/settings max 50</code>\n"
        f"<code>/settings max 100</code>",
        parse_mode="HTML",
        reply_markup=_stop_resume_kb(copy_active),
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    update_user(tg_user.id, {"copy_active": False})
    await update.message.reply_text(  # type: ignore[union-attr]
        "⏸ <b>Копирование приостановлено</b>\n\n"
        "Новые сделки не будут открываться.\n"
        "Уже открытые позиции остаются без изменений.\n\n"
        "Чтобы возобновить — нажми кнопку ниже или отправь /resume",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Возобновить", callback_data="resume")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
        ]),
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user or not db_user.get("wallet_address"):
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    update_user(tg_user.id, {"copy_active": True})
    await update.message.reply_text(  # type: ignore[union-attr]
        "▶️ <b>Копирование возобновлено!</b>\n\n"
        "Бот снова отслеживает сделки топ-трейдеров и копирует лучшие из них.\n\n"
        "Убедись что на кошельке есть <b>USDC</b> для открытия позиций.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
        ]]),
    )


# ─── Inline button handler ────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data
    tg_user = update.effective_user
    if not tg_user:
        return

    if data == "menu":
        db_user = get_user_by_telegram_id(tg_user.id)
        if db_user:
            await query.edit_message_text(
                _dashboard_text(db_user, tg_user.first_name),
                parse_mode="HTML",
                reply_markup=_main_kb(),
            )
        return

    if data == "help":
        await query.edit_message_text(
            HELP_TEXT,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
            ]]),
        )
        return

    if data == "wallet":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user or not db_user.get("wallet_address"):
            await query.answer("Кошелёк не найден. Отправь /start", show_alert=True)
            return
        addr = db_user["wallet_address"]
        await query.edit_message_text(
            f"💼 <b>Твой кошелёк</b>\n\n"
            f"<code>{addr}</code>\n\n"
            f"🔗 <a href=\"https://polygonscan.com/address/{addr}\">Polygonscan</a>\n\n"
            "📌 Пополняй <b>USDC</b> в сети <b>Polygon</b>.\n"
            "⚠️ Только Polygon — не Ethereum, не BSC!",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
            ]]),
        )
        return

    if data == "positions":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return
        positions = get_user_open_positions(db_user["id"])
        if not positions:
            await query.edit_message_text(
                "📊 <b>Открытые позиции</b>\n\n"
                "Нет открытых позиций.\n\n"
                "💡 Позиции появятся когда бот скопирует сделку.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
                ]]),
            )
            return
        lines = ["📊 <b>Открытые позиции</b>\n"]
        for i, trade in enumerate(positions, 1):
            sig = trade.get("trade_signals") or {}
            side = sig.get("side", "—")
            price = sig.get("price", 0)
            size = trade.get("size_usdc", 0)
            market = str(sig.get("market_id", "—"))[:28]
            side_icon = "🟢" if side == "YES" else "🔴"
            lines.append(
                f"{i}. {side_icon} <b>{side}</b> @ <code>{price:.4f}</code>\n"
                f"   💵 ${size:.2f} USDC | <i>{market}…</i>"
            )
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
            ]]),
        )
        return

    if data == "pnl":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return
        stats = get_user_pnl_stats(db_user["id"])
        pnl: float = stats["pnl"]
        pnl_icon = "📈" if pnl >= 0 else "📉"
        pnl_sign = "+" if pnl >= 0 else ""
        await query.edit_message_text(
            f"💰 <b>Статистика P&L</b>\n\n"
            f"📋 Всего сделок: <b>{stats['total']}</b>\n"
            f"💵 Объём: <b>${stats['volume']:.2f} USDC</b>\n"
            f"{pnl_icon} P&L: <b>{pnl_sign}{pnl:.2f} USDC</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
            ]]),
        )
        return

    if data == "settings":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return
        copy_active = db_user.get("copy_active", False)
        max_pos = db_user.get("max_position_usdc") or 25
        copy_status = "▶️ Включено" if copy_active else "⏸ Приостановлено"
        await query.edit_message_text(
            f"⚙️ <b>Настройки</b>\n\n"
            f"🔄 Копирование: {copy_status}\n"
            f"💵 Макс. позиция: <b>${max_pos:.0f} USDC</b>\n\n"
            f"<i>Изменить макс. позицию:</i>\n"
            f"<code>/settings max 50</code>",
            parse_mode="HTML",
            reply_markup=_stop_resume_kb(copy_active),
        )
        return

    if data == "stop":
        update_user(tg_user.id, {"copy_active": False})
        await query.edit_message_text(
            "⏸ <b>Копирование приостановлено</b>\n\n"
            "Новые сделки не открываются.\n"
            "Открытые позиции остаются без изменений.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Возобновить", callback_data="resume")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
            ]),
        )
        return

    if data == "resume":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user or not db_user.get("wallet_address"):
            await query.answer("Сначала отправь /start", show_alert=True)
            return
        update_user(tg_user.id, {"copy_active": True})
        await query.edit_message_text(
            "▶️ <b>Копирование возобновлено!</b>\n\n"
            "Бот снова отслеживает топ-трейдеров и копирует лучшие сделки.\n\n"
            "Убедись что на кошельке есть <b>USDC</b>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
            ]]),
        )
        return
