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
    MessageHandler,
    filters,
)

from core.config import settings
from core.db import (
    get_user_by_telegram_id,
    get_user_open_positions,
    get_user_pnl_stats,
    update_user,
    upsert_user,
)
from core.wallet import generate_wallet

log = structlog.get_logger(__name__)

# ─── Keyboards ────────────────────────────────────────────────────────────────

def _main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💼 Кошелёк",  callback_data="wallet"),
            InlineKeyboardButton("📊 Позиции",  callback_data="positions"),
        ],
        [
            InlineKeyboardButton("💰 P&L",      callback_data="pnl"),
            InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
        ],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
    ])


def _wallet_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Обновить баланс", callback_data="wallet_balance"),
            InlineKeyboardButton("💸 Вывод",           callback_data="withdraw_start"),
        ],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
    ])


def _stop_resume_kb(copy_active: bool) -> InlineKeyboardMarkup:
    if copy_active:
        btn = InlineKeyboardButton("⏸ Приостановить", callback_data="stop")
    else:
        btn = InlineKeyboardButton("▶️ Возобновить", callback_data="resume")
    return InlineKeyboardMarkup([[btn], [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")]])


def _settings_kb(copy_active: bool, current_max: float) -> InlineKeyboardMarkup:
    def _label(val: int) -> str:
        mark = " ✓" if abs(current_max - val) < 0.5 else ""
        return f"${val}{mark}"

    toggle_btn = (
        InlineKeyboardButton("⏸ Приостановить", callback_data="stop")
        if copy_active
        else InlineKeyboardButton("▶️ Возобновить", callback_data="resume")
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(_label(10),  callback_data="setmax_10"),
            InlineKeyboardButton(_label(25),  callback_data="setmax_25"),
            InlineKeyboardButton(_label(50),  callback_data="setmax_50"),
            InlineKeyboardButton(_label(100), callback_data="setmax_100"),
        ],
        [InlineKeyboardButton("✏️ Своё значение", callback_data="setmax_custom")],
        [toggle_btn],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
    ])


# ─── Text builders ────────────────────────────────────────────────────────────

def _dashboard_text(db_user: dict, first_name: str) -> str:
    addr = db_user.get("wallet_address", "—")
    addr_short = f"{addr[:6]}…{addr[-4:]}" if addr != "—" else "—"
    copy_active = db_user.get("copy_active")
    copy_icon = "🟢 Работает" if copy_active else "⏸ Пауза"
    max_pos = db_user.get("max_position_usdc") or 25
    return (
        f"👋 <b>Привет, {first_name}!</b>\n\n"
        "🧠 <b>PolyMind AI</b> — интеллектуальный копитрейдинг\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Кошелёк: <code>{addr_short}</code>\n"
        f"🔄 Автокопирование: {copy_icon}\n"
        f"💵 Макс. позиция: <b>${max_pos:.0f} USDC</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 Управляй через кнопки"
    )


def _new_user_text(addr: str) -> str:
    return (
        "🧠 <b>Добро пожаловать в PolyMind AI!</b>\n\n"
        "Твой персональный торговый кошелёк создан и готов к работе.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📬 <b>Адрес для пополнения:</b>\n"
        f"<code>{addr}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡️ <b>Как начать зарабатывать:</b>\n"
        "1️⃣ Пополни баланс в USDC (сеть Polygon)\n"
        "2️⃣ PolyMind автоматически скопирует сделки топ-трейдеров\n"
        "3️⃣ Отслеживай позиции и P&L в боте\n\n"
        "⚠️ Только <b>Polygon</b> — не Ethereum, не BSC!"
    )


HELP_TEXT = (
    "🧠 <b>PolyMind AI — Руководство</b>\n\n"

    "━━━━━━━━━━━━━━━━━━━━━\n"
    "⚡️ <b>Принцип работы</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "1️⃣ <b>Мониторинг</b> — PolyMind каждые 30 секунд "
    "отслеживает сделки проверенных трейдеров Polymarket\n\n"
    "2️⃣ <b>ИИ-анализ</b> — нейросеть оценивает риск каждой "
    "сделки и фильтрует нежелательные позиции\n\n"
    "3️⃣ <b>Автокопирование</b> — прошедшие проверку сделки "
    "мгновенно дублируются на твоём кошельке\n\n"

    "━━━━━━━━━━━━━━━━━━━━━\n"
    "📋 <b>Команды</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "/start — 🏠 Главное меню\n"
    "/wallet — 💼 Кошелёк и баланс\n"
    "/balance — 💵 Быстрая проверка баланса\n"
    "/positions — 📊 Активные позиции\n"
    "/pnl — 💰 Статистика прибыли\n"
    "/settings — ⚙️ Размер позиций\n"
    "/withdraw — 💸 Вывод средств\n"
    "/stop — ⏸ Пауза копирования\n"
    "/resume — ▶️ Возобновить\n\n"

    "━━━━━━━━━━━━━━━━━━━━━\n"
    "💡 <b>Советы</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "• Рекомендуемый баланс: <b>$50–200 USDC</b>\n"
    "• Оптимальный размер позиции: <b>$5–25 USDC</b>\n"
    "• Используй /stop чтобы взять паузу в любой момент\n"
    "• Кошелёк полностью в твоём управлении\n\n"

    "💬 Вопросы? Обратись к администратору."
)


# ─── App builder ──────────────────────────────────────────────────────────────

async def _set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",    "🏠 Главное меню"),
        BotCommand("wallet",   "💼 Кошелёк и баланс"),
        BotCommand("balance",  "💵 Проверить баланс"),
        BotCommand("withdraw", "💸 Вывод средств"),
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
    app.add_handler(CommandHandler("balance",   cmd_balance))
    app.add_handler(CommandHandler("withdraw",  cmd_withdraw))
    app.add_handler(CommandHandler("register",  cmd_register))
    app.add_handler(CallbackQueryHandler(callback_handler))
    # Must be last — catches free-text input
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    return app


# ─── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return

    db_user = upsert_user(tg_user.id)

    if not db_user.get("wallet_address"):
        await update.message.reply_text(  # type: ignore[union-attr]
            "⏳ Создаём твой кошелёк…",
            parse_mode="HTML",
        )
        try:
            wallet = generate_wallet()
            update_user(tg_user.id, {
                "wallet_address":       wallet["address"],
                "wallet_private_key_enc": wallet["private_key_enc"],
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
                "❌ <b>Не удалось создать кошелёк.</b>\n\nПопробуй ещё раз через минуту или обратись в поддержку.",
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


def _wallet_text(addr: str, balances: dict | None = None) -> str:
    text = (
        f"💼 <b>Кошелёк PolyMind</b>\n\n"
        f"<code>{addr}</code>\n\n"
    )
    if balances is not None:
        total = balances.get("total_usdc", 0)
        pol = balances.get("matic", 0)
        status = "✅ Готов к торговле" if total >= 5 else "⚠️ Пополни баланс"
        text += (
            f"💵 USDC: <b>${total:.2f}</b>\n"
            f"⛽️ POL (газ): <b>{pol:.4f}</b>\n"
            f"📊 Статус: {status}\n\n"
        )
    text += (
        f"🔗 <a href=\"https://polygonscan.com/address/{addr}\">Посмотреть на Polygonscan</a>\n\n"
        "📌 Пополняй <b>USDC</b> в сети <b>Polygon</b>\n"
        "⚠️ Только Polygon — не Ethereum, не BSC!"
    )
    return text


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user or not db_user.get("wallet_address"):
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    addr = db_user["wallet_address"]
    from core.polygon import get_balances
    balances = get_balances(addr)
    await update.message.reply_text(  # type: ignore[union-attr]
        _wallet_text(addr, balances),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_wallet_kb(),
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user or not db_user.get("wallet_address"):
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    addr = db_user["wallet_address"]
    msg = await update.message.reply_text("⏳ Проверяю баланс…", parse_mode="HTML")  # type: ignore[union-attr]
    from core.polygon import get_balances
    balances = get_balances(addr)
    total = balances.get("total_usdc", 0)
    pol = balances.get("matic", 0)
    await msg.edit_text(  # type: ignore[union-attr]
        f"💵 <b>Баланс кошелька</b>\n\n"
        f"USDC: <b>${total:.2f}</b>\n"
        f"POL (газ): <b>{pol:.4f}</b>\n\n"
        f"<code>{addr}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("💸 Вывод", callback_data="withdraw_start"),
            InlineKeyboardButton("🏠 Меню",  callback_data="menu"),
        ]]),
    )


async def cmd_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user or not db_user.get("wallet_address"):
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    context.user_data["withdraw_step"] = "address"
    await update.message.reply_text(  # type: ignore[union-attr]
        "💸 <b>Вывод USDC</b>\n\n"
        "Введи адрес кошелька Polygon для вывода:\n\n"
        "<i>Пример: 0x1234…abcd</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="withdraw_cancel")
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
    lines = [f"📊 <b>Активные позиции</b> ({len(positions)})\n"]
    total_invested = 0.0
    for i, trade in enumerate(positions, 1):
        sig = trade.get("trade_signals") or {}
        title = sig.get("title") or str(sig.get("market_id", "—"))[:40]
        price = float(sig.get("price") or 0)
        size = float(trade.get("size_usdc") or 0)
        total_invested += size
        status = trade.get("status", "—")
        st_icon = "✅" if status == "confirmed" else "⏳"
        lines.append(
            f"{i}. {st_icon} <b>{title}</b>\n"
            f"   BUY @ {price:.3f} · <b>${size:.2f} USDC</b>"
        )
    lines.append(f"\n💼 <b>Итого вложено: ${total_invested:.2f} USDC</b>")
    await update.message.reply_text(  # type: ignore[union-attr]
        "\n\n".join(lines),
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


def _settings_text(db_user: dict) -> str:
    copy_active = db_user.get("copy_active", False)
    max_pos = db_user.get("max_position_usdc") or 25
    copy_status = "▶️ Активно" if copy_active else "⏸ Приостановлено"
    return (
        f"⚙️ <b>Настройки</b>\n\n"
        f"🔄 Копирование: {copy_status}\n"
        f"💵 Макс. позиция: <b>${max_pos:.0f} USDC</b>\n\n"
        "Выбери максимальный размер одной позиции 👇"
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user:
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    copy_active = db_user.get("copy_active", False)
    max_pos = float(db_user.get("max_position_usdc") or 25)
    await update.message.reply_text(  # type: ignore[union-attr]
        _settings_text(db_user),
        parse_mode="HTML",
        reply_markup=_settings_kb(copy_active, max_pos),
    )


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user or not db_user.get("wallet_private_key_enc"):
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    msg = await update.message.reply_text(  # type: ignore[union-attr]
        "⏳ <b>Регистрирую кошелёк в Polymarket...</b>\n\nЭто займёт 30-60 секунд.",
        parse_mode="HTML",
    )
    try:
        from core.clob import register_wallet
        result = register_wallet(db_user["wallet_private_key_enc"])
        await msg.edit_text(  # type: ignore[union-attr]
            "✅ <b>Кошелёк зарегистрирован!</b>\n\n"
            "Теперь бот может копировать сделки на Polymarket.\n"
            "Убедись что копирование включено: /resume",
            parse_mode="HTML",
        )
    except ValueError as exc:
        await msg.edit_text(  # type: ignore[union-attr]
            f"⛽️ <b>Нужен POL для газа</b>\n\n"
            f"{exc}\n\n"
            f"Отправь хотя бы <b>0.1 POL</b> на кошелёк и повтори /register\n\n"
            f"<code>{db_user.get('wallet_address', '')}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        log.exception("register_failed", user=tg_user.id)
        await msg.edit_text(  # type: ignore[union-attr]
            f"❌ <b>Ошибка регистрации:</b>\n<code>{str(exc)[:300]}</code>",
            parse_mode="HTML",
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
        "▶️ <b>PolyMind снова в игре!</b>\n\n"
        "🧠 ИИ отслеживает топ-трейдеров и копирует лучшие сделки.\n\n"
        "Убедись что на кошельке есть <b>USDC</b> для открытия позиций.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
        ]]),
    )


# ─── Free-text input handler (custom position size) ──────────────────────────

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return

    text = (update.message.text or "").strip()  # type: ignore[union-attr]

    # ── Withdraw flow ──────────────────────────────────────────────────────────
    withdraw_step = context.user_data.get("withdraw_step")

    if withdraw_step == "address":
        from core.polygon import is_valid_address
        if not is_valid_address(text):
            await update.message.reply_text(  # type: ignore[union-attr]
                "⚠️ Неверный адрес. Укажи корректный адрес Polygon (0x…).\n\nПопробуй ещё раз или нажми Отмена:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="withdraw_cancel")
                ]]),
            )
            return

        context.user_data["withdraw_to"] = text
        context.user_data["withdraw_step"] = "amount"

        db_user = get_user_by_telegram_id(tg_user.id) or {}
        addr = db_user.get("wallet_address", "")

        from core.polygon import get_balances
        balances = get_balances(addr) if addr else {}
        total = balances.get("total_usdc", 0)

        await update.message.reply_text(  # type: ignore[union-attr]
            f"💵 <b>Сколько USDC вывести?</b>\n\n"
            f"Баланс: <b>${total:.2f} USDC</b>\n"
            f"На адрес: <code>{text[:10]}…{text[-6:]}</code>\n\n"
            "Введи сумму (например: <code>25</code>):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="withdraw_cancel")
            ]]),
        )
        return

    if withdraw_step == "amount":
        amount_text = text.replace("$", "").replace(",", ".")
        try:
            amount = float(amount_text)
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "⚠️ Неверный формат. Введи число, например: <code>25</code>",
                parse_mode="HTML",
            )
            return

        if amount < 1:
            await update.message.reply_text("⚠️ Минимум <b>$1 USDC</b>", parse_mode="HTML")  # type: ignore[union-attr]
            return

        context.user_data["withdraw_amount"] = amount
        context.user_data["withdraw_step"] = "confirm"
        to_addr = context.user_data.get("withdraw_to", "")

        await update.message.reply_text(  # type: ignore[union-attr]
            f"📋 <b>Подтверди вывод:</b>\n\n"
            f"💵 Сумма: <b>${amount:.2f} USDC</b>\n"
            f"📬 На адрес: <code>{to_addr}</code>\n"
            f"🌐 Сеть: <b>Polygon</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Подтвердить", callback_data="withdraw_confirm"),
                    InlineKeyboardButton("❌ Отмена",      callback_data="withdraw_cancel"),
                ]
            ]),
        )
        return

    # ── Custom position size ───────────────────────────────────────────────────
    if not context.user_data.get("awaiting_max_pos"):
        return

    context.user_data["awaiting_max_pos"] = False
    clean = text.replace("$", "").replace(",", ".")

    try:
        val = float(clean)
    except ValueError:
        await update.message.reply_text(  # type: ignore[union-attr]
            "⚠️ Неверный формат. Введи число, например: <code>75</code>",
            parse_mode="HTML",
        )
        return

    if val < 5:
        await update.message.reply_text("⚠️ Минимальное значение — <b>$5 USDC</b>", parse_mode="HTML")  # type: ignore[union-attr]
        return
    if val > 10_000:
        await update.message.reply_text("⚠️ Максимальное значение — <b>$10 000 USDC</b>", parse_mode="HTML")  # type: ignore[union-attr]
        return

    update_user(tg_user.id, {"max_position_usdc": val})
    db_user = get_user_by_telegram_id(tg_user.id) or {}
    copy_active = db_user.get("copy_active", False)

    await update.message.reply_text(  # type: ignore[union-attr]
        f"✅ <b>Готово!</b> Макс. позиция: <b>${val:.0f} USDC</b>",
        parse_mode="HTML",
        reply_markup=_settings_kb(copy_active, val),
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

    if data in ("wallet", "wallet_balance"):
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user or not db_user.get("wallet_address"):
            await query.answer("Кошелёк не найден. Отправь /start", show_alert=True)
            return
        addr = db_user["wallet_address"]
        await query.answer("⏳ Загружаю баланс…")
        from core.polygon import get_balances
        balances = get_balances(addr)
        await query.edit_message_text(
            _wallet_text(addr, balances),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_wallet_kb(),
        )
        return

    if data == "withdraw_start":
        context.user_data["withdraw_step"] = "address"
        await query.edit_message_text(
            "💸 <b>Вывод USDC</b>\n\n"
            "Введи адрес кошелька Polygon для вывода:\n\n"
            "<i>Пример: 0x742d35Cc…</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="withdraw_cancel")
            ]]),
        )
        return

    if data == "withdraw_cancel":
        context.user_data.pop("withdraw_step", None)
        context.user_data.pop("withdraw_to", None)
        context.user_data.pop("withdraw_amount", None)
        await query.edit_message_text(
            "❌ Вывод отменён.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
            ]]),
        )
        return

    if data == "withdraw_confirm":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user or not db_user.get("wallet_private_key_enc"):
            await query.answer("Кошелёк не найден", show_alert=True)
            return

        to_addr   = context.user_data.get("withdraw_to", "")
        amount    = float(context.user_data.get("withdraw_amount", 0))
        context.user_data.pop("withdraw_step", None)

        await query.edit_message_text(
            "⏳ <b>Выполняю транзакцию…</b>\n\nЭто займёт 5–15 секунд.",
            parse_mode="HTML",
        )

        try:
            from core.polygon import transfer_usdc
            tx_hash = transfer_usdc(
                private_key_enc=db_user["wallet_private_key_enc"],
                wallet_address=db_user["wallet_address"],
                to_address=to_addr,
                amount_usdc=amount,
            )
            await query.edit_message_text(
                f"✅ <b>Вывод выполнен!</b>\n\n"
                f"💵 <b>${amount:.2f} USDC</b>\n"
                f"📬 На: <code>{to_addr}</code>\n\n"
                f"🔗 <a href=\"https://polygonscan.com/tx/{tx_hash}\">Посмотреть транзакцию</a>",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
                ]]),
            )
            log.info("withdrawal_ok", user_id=tg_user.id, amount=amount, tx=tx_hash[:20])
        except Exception as exc:
            await query.edit_message_text(
                f"❌ <b>Ошибка вывода</b>\n\n<code>{str(exc)[:200]}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
                ]]),
            )
            log.exception("withdrawal_failed", user_id=tg_user.id)
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
        lines = [f"📊 <b>Активные позиции</b> ({len(positions)})\n"]
        total_invested = 0.0
        for i, trade in enumerate(positions, 1):
            sig = trade.get("trade_signals") or {}
            title = sig.get("title") or str(sig.get("market_id", "—"))[:40]
            price = float(sig.get("price") or 0)
            size = float(trade.get("size_usdc") or 0)
            total_invested += size
            status = trade.get("status", "—")
            st_icon = "✅" if status == "confirmed" else "⏳"
            lines.append(
                f"{i}. {st_icon} <b>{title}</b>\n"
                f"   BUY @ {price:.3f} · <b>${size:.2f} USDC</b>"
            )
        lines.append(f"\n💼 <b>Итого вложено: ${total_invested:.2f} USDC</b>")
        await query.edit_message_text(
            "\n\n".join(lines),
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
        max_pos = float(db_user.get("max_position_usdc") or 25)
        await query.edit_message_text(
            _settings_text(db_user),
            parse_mode="HTML",
            reply_markup=_settings_kb(copy_active, max_pos),
        )
        return

    if data.startswith("setmax_"):
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return

        suffix = data[len("setmax_"):]

        if suffix == "custom":
            context.user_data["awaiting_max_pos"] = True
            await query.answer()
            await query.edit_message_text(
                "✏️ <b>Введи сумму в долларах</b>\n\n"
                "Напиши число в чат, например: <code>75</code>\n\n"
                "<i>Допустимый диапазон: $5 — $10 000</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Назад", callback_data="settings")
                ]]),
            )
            return

        try:
            val = float(suffix)
        except ValueError:
            await query.answer("Ошибка", show_alert=True)
            return

        update_user(tg_user.id, {"max_position_usdc": val})
        db_user = get_user_by_telegram_id(tg_user.id) or db_user
        copy_active = db_user.get("copy_active", False)
        await query.answer(f"✅ Позиция: ${val:.0f} USDC")
        await query.edit_message_text(
            _settings_text(db_user),
            parse_mode="HTML",
            reply_markup=_settings_kb(copy_active, val),
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
            "▶️ <b>PolyMind снова в игре!</b>\n\n"
            "🧠 ИИ отслеживает топ-трейдеров и копирует лучшие сделки.\n\n"
            "Убедись что на кошельке есть <b>USDC</b>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
            ]]),
        )
        return
