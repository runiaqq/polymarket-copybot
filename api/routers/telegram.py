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
    create_access_code,
    get_subscription_status,
    get_user_by_telegram_id,
    get_user_by_username,
    redeem_access_code,
    set_subscription,
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


def _signals_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐️ Подписка", callback_data="subscription"),
            InlineKeyboardButton("❓ Как работает", callback_data="help"),
        ],
    ])


def _wallet_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Обновить баланс", callback_data="wallet_balance"),
            InlineKeyboardButton("💸 Вывод",           callback_data="withdraw_start"),
        ],
        [
            InlineKeyboardButton("♻️ В pUSD", callback_data="wrap"),
            InlineKeyboardButton("🔐 Регистрация", callback_data="register"),
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

MIN_USDC_READY = 5.0
MIN_POL_READY = 0.05


def _checklist(db_user: dict) -> str:
    """Onboarding checklist with live status of each prerequisite step."""
    addr = db_user.get("wallet_address")
    dw = db_user.get("deposit_wallet_address")
    eoa_bal, dw_bal = {}, {}
    try:
        from core.polygon import get_balances
        if addr:
            eoa_bal = get_balances(addr)
        if dw:
            dw_bal = get_balances(dw)
    except Exception:
        pass

    dw_pusd = dw_bal.get("pusd", 0)
    on_eoa = eoa_bal.get("pusd", 0) + eoa_bal.get("usdc_e", 0) + eoa_bal.get("usdc", 0)
    pol = eoa_bal.get("matic", 0)

    sub = get_subscription_status(db_user.get("telegram_id")) if db_user.get("telegram_id") else {"active": False}
    registered = bool(db_user.get("wallet_registered"))
    copy_active = bool(db_user.get("copy_active"))
    funded = (dw_pusd + on_eoa) >= MIN_USDC_READY

    steps_done = (
        registered
        and dw_pusd >= MIN_USDC_READY
        and pol >= MIN_POL_READY
        and sub.get("active")
        and copy_active
    )
    if steps_done:
        return ""

    def mark(ok: bool) -> str:
        return "✅" if ok else "⬜️"

    lines = ["📋 <b>Чек-лист запуска</b>\n"]
    lines.append(f"{mark(registered)} 1. Настроить торговый кошелёк (/register, без газа)")
    lines.append(f"{mark(funded)} 2. Пополнить <b>USDC</b> (сеть Polygon)")
    lines.append(f"{mark(pol >= MIN_POL_READY)} 3. Пополнить <b>POL</b> для газа (~0.1)")
    lines.append(f"{mark(dw_pusd >= MIN_USDC_READY)} 4. Средства на торговом кошельке (авто / /wrap)")
    if on_eoa >= 1.0 and dw_pusd < MIN_USDC_READY:
        lines.append("    ♻️ Есть USDC на кошельке — нажми /wrap, чтобы перевести в торговый баланс")
    lines.append(f"{mark(sub.get('active'))} 5. Активная подписка")
    lines.append(f"{mark(copy_active)} 6. Копирование включено")
    return "\n".join(lines)


def _dashboard_text(db_user: dict, first_name: str) -> str:
    addr = db_user.get("wallet_address", "—")
    addr_short = f"{addr[:6]}…{addr[-4:]}" if addr != "—" else "—"
    copy_active = db_user.get("copy_active")
    copy_icon = "🟢 Работает" if copy_active else "⏸ Пауза"
    max_pos = db_user.get("max_position_usdc") or 25
    checklist = _checklist(db_user)
    mid = (
        f"{checklist}\n\n"
        if checklist
        else "✅ <b>Всё настроено</b> — бот отслеживает китов и копирует сделки.\n\n"
    )
    return (
        f"👋 <b>Привет, {first_name}!</b>\n\n"
        "🧠 <b>PolyMind AI</b> — интеллектуальный копитрейдинг\n"
        "Бот следит за крупными покупками китов на быстрых рынках "
        "и копирует их на твой кошелёк, а ИИ присылает анализ.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Кошелёк: <code>{addr_short}</code>\n"
        f"🔄 Автокопирование: {copy_icon}\n"
        f"💵 Макс. позиция: <b>${max_pos:.0f} USDC</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{mid}"
        "👇 Управляй через кнопки"
    )


def _new_user_text(addr: str) -> str:
    return (
        "🧠 <b>Добро пожаловать в PolyMind AI!</b>\n\n"
        "Твой персональный торговый кошелёк создан.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📬 <b>Адрес для пополнения:</b>\n"
        f"<code>{addr}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡️ <b>Как запустить (по шагам):</b>\n"
        "1️⃣ Пополни <b>USDC</b> в сети <b>Polygon</b> (бот сам сконвертирует в pUSD)\n"
        "2️⃣ Пополни немного <b>POL</b> (~0.1) — нужен на газ\n"
        "3️⃣ Нажми <b>🔐 Зарегистрировать кошелёк</b> или /register\n"
        "4️⃣ Готово — бот копирует крупные сделки китов\n\n"
        "⚠️ Только сеть <b>Polygon</b> — не Ethereum, не BSC!\n"
        "ℹ️ Торговля идёт в <b>pUSD</b> (V2) — конвертация из USDC автоматическая."
    )


def _signals_dashboard_text(db_user: dict, first_name: str) -> str:
    sub = get_subscription_status(db_user.get("telegram_id")) if db_user.get("telegram_id") else {"active": False}
    if sub.get("active"):
        exp = (sub.get("expires_at") or "")[:10]
        status = f"🟢 <b>Активна</b> (до {exp})" if exp else "🟢 <b>Активна</b>"
        tail = "Сигналы приходят автоматически. Жди уведомления о ките и заходи на Polymarket по ссылке."
    else:
        status = "⛔️ <b>Не активна</b>"
        tail = "Чтобы получать сигналы — оформи подписку у администратора."
    return (
        f"👋 <b>Привет, {first_name}!</b>\n\n"
        "🧠 <b>PolyMind AI</b> — сигналы по китам Polymarket\n\n"
        "Бот в реальном времени ловит крупные покупки китов на быстрых "
        "рынках (резолв 1–2 дня), а нейросеть оценивает риск и присылает "
        "тебе готовый разбор со ссылкой на рынок.\n"
        "Сделку ты открываешь сам на Polymarket — деньги остаются у тебя.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"⭐️ Подписка: {status}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{tail}"
    )


def _signals_welcome_text() -> str:
    return (
        "🧠 <b>Добро пожаловать в PolyMind AI!</b>\n\n"
        "Это бот-сигналы по китам Polymarket.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡️ <b>Как это работает</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ Бот сканирует быстрые рынки (резолв 1–2 дня) в реальном времени\n"
        "2️⃣ Ловит крупную покупку кита и считает ликвидность стакана\n"
        "3️⃣ Нейросеть оценивает риск и присылает тебе разбор\n"
        "4️⃣ Ты заходишь на Polymarket по ссылке и открываешь сделку сам\n\n"
        "💼 Деньги и сделки полностью под твоим контролем — бот ничего не "
        "хранит и не торгует за тебя.\n\n"
        "⭐️ Для доступа к сигналам нужна активная подписка — оформи её у "
        "администратора."
    )


HELP_TEXT_SIGNALS = (
    "🧠 <b>PolyMind AI — Руководство</b>\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "⚡️ <b>Что делает бот</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "PolyMind в реальном времени отслеживает крупные покупки китов на "
    "быстрых рынках Polymarket и присылает тебе сигнал + ИИ-анализ риска "
    "со ссылкой на рынок.\n\n"
    "Сделку открываешь <b>сам на Polymarket</b> — это не кастодиальный бот, "
    "деньги остаются у тебя.\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "📋 <b>Команды</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "/start — 🏠 Главное меню\n"
    "/subscription — ⭐️ Статус подписки\n"
    "/help — ❓ Это руководство\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "💡 <b>Как читать сигнал</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "• Размер покупки кита и цена входа\n"
    "• Оценка риска и вердикт от ИИ\n"
    "• Ссылка на рынок — заходи и решай сам\n\n"
    "💬 Вопросы по подписке — к администратору."
)


HELP_TEXT = (
    "🧠 <b>PolyMind AI — Руководство</b>\n\n"

    "━━━━━━━━━━━━━━━━━━━━━\n"
    "⚡️ <b>Принцип работы</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "1️⃣ <b>Мониторинг китов</b> — PolyMind сканирует быстрые рынки "
    "(резолв 1–2 дня) и ловит крупные покупки (от $5000)\n\n"
    "2️⃣ <b>Автокопирование</b> — при крупной покупке бот сразу "
    "открывает позицию на твоём кошельке\n\n"
    "3️⃣ <b>ИИ-анализ</b> — нейросеть оценивает риск и присылает "
    "тебе разбор сделки\n\n"

    "━━━━━━━━━━━━━━━━━━━━━\n"
    "🚀 <b>С чего начать</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "1. Пополни <b>USDC</b> (Polygon) — бот сконвертирует в pUSD\n"
    "2. Пополни <b>POL</b> (~0.1) — на газ\n"
    "3. /register — регистрация кошелька в Polymarket\n"
    "4. /wrap — конвертация USDC → pUSD (если не авто)\n"
    "5. /resume — включи копирование\n\n"

    "━━━━━━━━━━━━━━━━━━━━━\n"
    "📋 <b>Команды</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "/start — 🏠 Главное меню\n"
    "/wallet — 💼 Кошелёк и баланс\n"
    "/balance — 💵 Быстрая проверка баланса\n"
    "/register — 🔐 Регистрация кошелька\n"
    "/subscription — ⭐️ Статус подписки\n"
    "/positions — 📊 Активные позиции\n"
    "/pnl — 💰 Статистика прибыли\n"
    "/settings — ⚙️ Размер позиций\n"
    "/withdraw — 💸 Вывод средств\n"
    "/stop — ⏸ Пауза копирования\n"
    "/resume — ▶️ Возобновить\n\n"

    "━━━━━━━━━━━━━━━━━━━━━\n"
    "💡 <b>Важно</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "• Торговля идёт в <b>pUSD</b> (V2); пополняешь USDC, бот конвертирует\n"
    "• Без <b>POL</b> на газ регистрация, конвертация и вывод не пройдут\n"
    "• Оптимальный размер позиции: <b>$5–25 USDC</b>\n"
    "• Кошелёк полностью в твоём управлении\n\n"

    "💬 Вопросы? Обратись к администратору."
)


# ─── App builder ──────────────────────────────────────────────────────────────

async def _set_commands(app: Application) -> None:
    if not settings.auto_copy_enabled:
        await app.bot.set_my_commands([
            BotCommand("start",        "🏠 Главное меню"),
            BotCommand("subscription", "⭐️ Статус подписки"),
            BotCommand("help",         "❓ Как работает"),
        ])
        return
    await app.bot.set_my_commands([
        BotCommand("start",    "🏠 Главное меню"),
        BotCommand("wallet",   "💼 Кошелёк и баланс"),
        BotCommand("balance",  "💵 Проверить баланс"),
        BotCommand("register", "🔐 Зарегистрировать кошелёк"),
        BotCommand("wrap",     "♻️ Конвертировать в pUSD"),
        BotCommand("subscription", "⭐️ Статус подписки"),
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
    app.add_handler(CommandHandler("wrap",      cmd_wrap))
    app.add_handler(CommandHandler("subscription", cmd_subscription))
    app.add_handler(CommandHandler("grant",     cmd_grant))
    app.add_handler(CommandHandler("newcode",   cmd_newcode))
    app.add_handler(CommandHandler("codes",     cmd_codes))
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

    # Keep the stored username fresh so admins can extend by @nick.
    if tg_user.username and db_user.get("username") != tg_user.username:
        try:
            update_user(tg_user.id, {"username": tg_user.username})
        except Exception:
            log.warning("username_update_failed", telegram_id=tg_user.id)

    # Deep-link activation: t.me/Bot?start=<code> → /start <code>
    if context.args:
        code = (context.args[0] or "").strip()
        if code:
            result = redeem_access_code(code, tg_user.id)
            if result.get("ok"):
                exp = (result.get("expires_at") or "")[:10]
                tail = (
                    "Теперь жди сигналы по китам — бот пришлёт разбор и ссылку на рынок."
                    if not settings.auto_copy_enabled
                    else "Теперь пройди шаги ниже, и бот начнёт копировать сделки."
                )
                await update.message.reply_text(  # type: ignore[union-attr]
                    f"✅ <b>Подписка активирована!</b>\n\n"
                    f"Действует до: <b>{exp}</b>\n\n"
                    f"{tail}",
                    parse_mode="HTML",
                )
            elif result.get("reason") in ("used", "invalid"):
                await update.message.reply_text(  # type: ignore[union-attr]
                    "⚠️ <b>Код недействителен или уже использован.</b>\n"
                    "Обратись к администратору за новой ссылкой.",
                    parse_mode="HTML",
                )

    # Signals mode: no custodial wallet — user trades on Polymarket themselves.
    if not settings.auto_copy_enabled:
        await update.message.reply_text(  # type: ignore[union-attr]
            _signals_dashboard_text(db_user, tg_user.first_name),
            parse_mode="HTML",
            reply_markup=_signals_kb(),
        )
        return

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
        HELP_TEXT_SIGNALS if not settings.auto_copy_enabled else HELP_TEXT,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
        ]]),
    )


def _wallet_text(addr: str, balances: dict | None = None, dw_pusd: float | None = None) -> str:
    text = (
        f"💼 <b>Кошелёк PolyMind</b>\n\n"
        f"📬 Адрес для пополнения (USDC, Polygon):\n<code>{addr}</code>\n\n"
    )
    if balances is not None:
        on_eoa = balances.get("usdc_e", 0) + balances.get("usdc", 0) + balances.get("pusd", 0)
        pol = balances.get("matic", 0)
        trading = dw_pusd if dw_pusd is not None else 0.0
        status = "✅ Готов к торговле" if trading >= 5 else "⚠️ Пополни баланс"
        text += f"💵 Торговый баланс (pUSD): <b>${trading:.2f}</b>\n"
        if on_eoa >= 0.01:
            text += f"♻️ На кошельке (к переводу): <b>${on_eoa:.2f}</b>\n"
        text += (
            f"⛽️ POL (газ): <b>{pol:.4f}</b>\n"
            f"📊 Статус: {status}\n\n"
        )
    text += (
        f"🔗 <a href=\"https://polygonscan.com/address/{addr}\">Посмотреть на Polygonscan</a>\n\n"
        "📌 Пополняй <b>USDC</b> в сети <b>Polygon</b> — бот сам переведёт на торговый кошелёк\n"
        "⚠️ Только сеть <b>Polygon</b> — не Ethereum, не BSC!"
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
    dw = db_user.get("deposit_wallet_address")
    dw_pusd = get_balances(dw).get("pusd", 0.0) if dw else 0.0
    await update.message.reply_text(  # type: ignore[union-attr]
        _wallet_text(addr, balances, dw_pusd),
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
    dw = db_user.get("deposit_wallet_address")
    trading = get_balances(dw).get("pusd", 0.0) if dw else 0.0
    on_eoa = balances.get("usdc_e", 0) + balances.get("usdc", 0) + balances.get("pusd", 0)
    pol = balances.get("matic", 0)
    extra = f"♻️ На кошельке (к переводу): <b>${on_eoa:.2f}</b>\n" if on_eoa >= 0.01 else ""
    await msg.edit_text(  # type: ignore[union-attr]
        f"💵 <b>Баланс</b>\n\n"
        f"Торговый баланс (pUSD): <b>${trading:.2f}</b>\n"
        f"{extra}"
        f"POL (газ): <b>{pol:.4f}</b>\n\n"
        f"<code>{addr}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("♻️ В pUSD", callback_data="wrap"),
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


def _trading_wallet(db_user: dict) -> str | None:
    """Address that actually holds positions/collateral — the deposit wallet in V2,
    falling back to the EOA for legacy users."""
    return db_user.get("deposit_wallet_address") or db_user.get("wallet_address")


def _build_positions(db_user: dict, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, InlineKeyboardMarkup]:
    """Render live positions (real P&L from data-api) with per-position close buttons."""
    wallet = _trading_wallet(db_user)
    positions = []
    if wallet:
        try:
            from core.polymarket import get_positions
            positions = [p for p in get_positions(wallet) if p["shares"] > 0]
        except Exception:
            positions = []

    # Cache token_ids for the close-by-index callback (callback_data is 64-byte capped).
    context.user_data["pos_cache"] = [p["token_id"] for p in positions]

    if not positions:
        return (
            "📊 <b>Открытые позиции</b>\n\n"
            "Нет открытых позиций.\n\n"
            "💡 Позиции появятся здесь, когда бот скопирует сделку.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="menu")]]),
        )

    lines = [f"📊 <b>Активные позиции</b> ({len(positions)})\n"]
    buttons: list[list[InlineKeyboardButton]] = []
    total_value = 0.0
    total_pnl = 0.0
    from core.polymarket import event_url
    for i, p in enumerate(positions):
        title = (p.get("title") or "—")[:40]
        outcome = p.get("outcome") or "—"
        shares = p["shares"]
        avg = p["avg_price"]
        cur = p["cur_price"]
        pnl = p["cash_pnl"]
        pct = p["percent_pnl"]
        total_value += p["current_value"]
        total_pnl += pnl
        icon = "📈" if pnl >= 0 else "📉"
        tag = " · ✅ к выводу" if p.get("redeemable") else ""
        url = event_url(p.get("event_slug"))
        title_html = f"<a href=\"{url}\">{title}</a>" if url else f"<b>{title}</b>"
        lines.append(
            f"{i+1}. {title_html} · {outcome}{tag}\n"
            f"   {shares:.0f} шт @ {avg:.3f} → {cur:.3f} · {icon} <b>{pnl:+.2f}$</b> ({pct:+.0%})"
        )
        buttons.append([InlineKeyboardButton(f"❌ Закрыть #{i+1}", callback_data=f"close_{i}")])

    pnl_icon = "📈" if total_pnl >= 0 else "📉"
    lines.append(
        f"\n💼 Стоимость: <b>${total_value:.2f}</b> · {pnl_icon} P&L: <b>{total_pnl:+.2f}$</b>"
    )
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu")])
    return "\n\n".join(lines), InlineKeyboardMarkup(buttons)


_PNL_WINDOWS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400, "all": None}
_PNL_LABELS = {"day": "за день", "week": "за неделю", "month": "за месяц", "all": "за всё время"}


def _pnl_kb(active: str) -> InlineKeyboardMarkup:
    def lbl(p: str, text: str) -> str:
        return f"• {text} •" if p == active else text
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(lbl("day", "День"), callback_data="pnl_day"),
            InlineKeyboardButton(lbl("week", "Неделя"), callback_data="pnl_week"),
            InlineKeyboardButton(lbl("month", "Месяц"), callback_data="pnl_month"),
            InlineKeyboardButton(lbl("all", "Всё"), callback_data="pnl_all"),
        ],
        [InlineKeyboardButton("📊 Позиции", callback_data="positions"),
         InlineKeyboardButton("🏠 Меню", callback_data="menu")],
    ])


def _build_pnl(db_user: dict, period: str = "day") -> str:
    """Real P&L: realized over the chosen period + current unrealized snapshot."""
    import time as _t

    wallet = _trading_wallet(db_user)
    open_pos, closed = [], []
    if wallet:
        try:
            from core.polymarket import get_closed_positions, get_positions
            open_pos = [p for p in get_positions(wallet) if p["shares"] > 0]
            closed = get_closed_positions(wallet)
        except Exception:
            pass

    window = _PNL_WINDOWS.get(period)
    cutoff = (_t.time() - window) if window else 0
    period_closed = [c for c in closed if c["timestamp"] >= cutoff]
    realized = sum(c["realized_pnl"] for c in period_closed)
    wins = sum(1 for c in period_closed if c["realized_pnl"] > 0)
    losses = sum(1 for c in period_closed if c["realized_pnl"] < 0)

    invested = sum(p["current_value"] for p in open_pos)
    unrealized = sum(p["cash_pnl"] for p in open_pos)

    r_icon = "📈" if realized >= 0 else "📉"
    u_icon = "📈" if unrealized >= 0 else "📉"
    settled_n = len(period_closed)
    winrate = f" · винрейт {wins}/{settled_n}" if settled_n else ""

    return (
        f"💰 <b>Статистика P&L · {_PNL_LABELS[period]}</b>\n\n"
        f"{r_icon} Реализованный {_PNL_LABELS[period]}: <b>{realized:+.2f}$</b>\n"
        f"📋 Закрыто сделок: <b>{settled_n}</b>{winrate}\n"
        f"   ✅ {wins} в плюс · ❌ {losses} в минус\n\n"
        f"📂 Открытых позиций: <b>{len(open_pos)}</b>\n"
        f"💼 В позициях сейчас: <b>${invested:.2f}</b>\n"
        f"{u_icon} Нереализованный P&L: <b>{unrealized:+.2f}$</b>"
    )


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user:
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    text, kb = _build_positions(db_user, context)
    await update.message.reply_text(  # type: ignore[union-attr]
        text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
    )


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user:
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    await update.message.reply_text(  # type: ignore[union-attr]
        _build_pnl(db_user, "day"),
        parse_mode="HTML",
        reply_markup=_pnl_kb("day"),
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


async def cmd_wrap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user or not db_user.get("wallet_address"):
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    try:
        from worker.tasks import wrap_collateral
        wrap_collateral.delay(db_user["id"])
        await update.message.reply_text(  # type: ignore[union-attr]
            "♻️ <b>Конвертирую USDC.e → pUSD…</b>\n\nРезультат придёт отдельным сообщением.",
            parse_mode="HTML",
        )
    except Exception:
        await update.message.reply_text("❌ Не удалось запустить конвертацию.", parse_mode="HTML")  # type: ignore[union-attr]


def _register_deposit_wallet(telegram_id: int, db_user: dict) -> dict:
    """Gasless per-user deposit-wallet setup (deploy + approvals + CLOB creds).
    Persists the deposit wallet + creds. Returns the clob.register_deposit_wallet result."""
    from core.clob import register_deposit_wallet

    result = register_deposit_wallet(db_user["wallet_private_key_enc"])
    creds = result.get("creds") or {}
    update_user(telegram_id, {
        "deposit_wallet_address":  result["deposit_wallet"],
        "deposit_wallet_deployed": True,
        "wallet_registered":       True,
        "clob_api_key":            creds.get("clob_api_key"),
        "clob_secret":             creds.get("clob_secret"),
        "clob_passphrase":         creds.get("clob_passphrase"),
    })
    return result


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    db_user = get_user_by_telegram_id(tg_user.id)
    if not db_user or not db_user.get("wallet_private_key_enc"):
        await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
        return
    msg = await update.message.reply_text(  # type: ignore[union-attr]
        "⏳ <b>Настраиваю торговый кошелёк Polymarket…</b>\n\n"
        "Разворачиваю deposit-wallet и проставляю разрешения (без газа с твоей стороны). "
        "Это займёт 30–60 секунд.",
        parse_mode="HTML",
    )
    try:
        result = _register_deposit_wallet(tg_user.id, db_user)
        dw = result.get("deposit_wallet", "")
        await msg.edit_text(  # type: ignore[union-attr]
            "✅ <b>Кошелёк готов к торговле!</b>\n\n"
            f"Торговый адрес (deposit wallet):\n<code>{dw}</code>\n\n"
            "Бот может копировать сделки. Убедись, что копирование включено: /resume",
            parse_mode="HTML",
        )
    except Exception as exc:
        log.exception("register_failed", user=tg_user.id)
        await msg.edit_text(  # type: ignore[union-attr]
            f"❌ <b>Ошибка регистрации:</b>\n<code>{str(exc)[:300]}</code>\n\n"
            "Попробуй ещё раз через минуту или напиши в поддержку.",
            parse_mode="HTML",
        )


def _sub_text(status: dict) -> str:
    if status.get("active"):
        exp = (status.get("expires_at") or "")[:10]
        return (
            f"⭐️ <b>Подписка активна</b>\n\n"
            f"Действует до: <b>{exp}</b>\n\n"
            "Бот копирует крупные сделки китов на твой кошелёк."
        )
    return (
        "⛔️ <b>Подписка неактивна</b>\n\n"
        "Без активной подписки бот не открывает сделки.\n\n"
        "Подписка активируется автоматически при переходе по ссылке от администратора."
    )


async def cmd_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return
    status = get_subscription_status(tg_user.id)
    await update.message.reply_text(  # type: ignore[union-attr]
        _sub_text(status),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
        ]]),
    )


async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: grant/extend a subscription by @username or telegram_id.
    /grant <@username|telegram_id> [days]. Works whether active (extends) or expired (reactivates)."""
    tg_user = update.effective_user
    if not tg_user or tg_user.id != settings.admin_telegram_id:
        return
    args = context.args or []
    if len(args) < 1:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Использование: <code>/grant &lt;@username | telegram_id&gt; [days]</code>\n"
            "days: число дней (по умолчанию 30).\n"
            "Команда и продлевает активную подписку, и реактивирует истёкшую.",
            parse_mode="HTML",
        )
        return

    ident = args[0]
    try:
        days = int(args[1]) if len(args) > 1 else 30
    except ValueError:
        days = 30

    # Resolve target: numeric id or @username.
    target = None
    if ident.lstrip("@").isdigit() and not ident.startswith("@"):
        target_id = int(ident)
        target = get_user_by_telegram_id(target_id) or {"telegram_id": target_id}
    else:
        target = get_user_by_username(ident)
        if not target:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"❌ Пользователь <b>{ident}</b> не найден.\n"
                "Он должен сначала запустить бота (/start), чтобы ник сохранился. "
                "Либо выдай по числовому Telegram ID.",
                parse_mode="HTML",
            )
            return

    target_id = target["telegram_id"]
    try:
        user = set_subscription(target_id, days)
        exp = (user.get("sub_expires_at") or "")[:10]
        uname = f"@{target.get('username')}" if target.get("username") else f"<code>{target_id}</code>"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"✅ Подписка продлена на <b>{days}</b> дн.\n"
            f"Пользователь: {uname}\n"
            f"Действует до: <b>{exp}</b>",
            parse_mode="HTML",
        )
        # Let the user know their subscription was extended.
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"✅ <b>Подписка продлена!</b>\n\n"
                    f"Действует до: <b>{exp}</b>\n\n"
                    f"Спасибо! Бот продолжает копировать сделки."
                ),
                parse_mode="HTML",
            )
        except Exception:
            log.info("grant_user_notify_skipped", target_id=target_id)
    except Exception as exc:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"❌ Ошибка: <code>{str(exc)[:200]}</code>", parse_mode="HTML"
        )


async def cmd_newcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: create a one-time access code and return its deep link. /newcode [tier] [days]"""
    tg_user = update.effective_user
    if not tg_user or tg_user.id != settings.admin_telegram_id:
        return
    args = context.args or []
    try:
        days = int(args[0]) if len(args) > 0 else 30
    except ValueError:
        days = 30
    code = create_access_code(days)
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={code}"
    await update.message.reply_text(  # type: ignore[union-attr]
        f"🎟 <b>Код создан</b> · <b>{days}</b> дн.\n\n"
        f"Код: <code>{code}</code>\n\n"
        f"Ссылка для клиента (одноразовая):\n{link}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: list recent access codes and their status."""
    tg_user = update.effective_user
    if not tg_user or tg_user.id != settings.admin_telegram_id:
        return
    from core.db import get_supabase
    sb = get_supabase()
    res = (
        sb.table("access_codes").select("code,days,used_by,created_at")
        .order("created_at", desc=True).limit(15).execute()
    )
    rows = res.data or []
    if not rows:
        await update.message.reply_text("Кодов пока нет. Создай: /newcode 30", parse_mode="HTML")  # type: ignore[union-attr]
        return
    lines = ["🎟 <b>Последние коды</b>\n"]
    for r in rows:
        status = f"✅ использован ({r['used_by']})" if r.get("used_by") else "🟢 свободен"
        lines.append(f"<code>{r['code']}</code> · {r['days']}д · {status}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")  # type: ignore[union-attr]


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
            if not settings.auto_copy_enabled:
                await query.edit_message_text(
                    _signals_dashboard_text(db_user, tg_user.first_name),
                    parse_mode="HTML",
                    reply_markup=_signals_kb(),
                )
            else:
                await query.edit_message_text(
                    _dashboard_text(db_user, tg_user.first_name),
                    parse_mode="HTML",
                    reply_markup=_main_kb(),
                )
        return

    if data == "subscription":
        db_user = get_user_by_telegram_id(tg_user.id)
        sub = get_subscription_status(tg_user.id) if db_user else {"active": False}
        if sub.get("active"):
            exp = (sub.get("expires_at") or "")[:10]
            txt = (
                "⭐️ <b>Подписка активна</b>\n\n"
                f"Действует до: <b>{exp or '—'}</b>\n\n"
                "Сигналы приходят автоматически — жди уведомление о ките."
            )
        else:
            txt = (
                "⛔️ <b>Подписка не активна</b>\n\n"
                "Чтобы получать сигналы по китам с ИИ-анализом — оформи "
                "подписку у администратора. После оплаты он пришлёт тебе "
                "ссылку для активации."
            )
        await query.edit_message_text(
            txt,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
            ]]),
        )
        return

    if data == "help":
        await query.edit_message_text(
            HELP_TEXT_SIGNALS if not settings.auto_copy_enabled else HELP_TEXT,
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
        dw = db_user.get("deposit_wallet_address")
        dw_pusd = get_balances(dw).get("pusd", 0.0) if dw else 0.0
        await query.edit_message_text(
            _wallet_text(addr, balances, dw_pusd),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_wallet_kb(),
        )
        return

    if data == "register":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user or not db_user.get("wallet_private_key_enc"):
            await query.answer("Сначала отправь /start", show_alert=True)
            return
        await query.edit_message_text(
            "⏳ <b>Настраиваю торговый кошелёк Polymarket…</b>\n\n"
            "Разворачиваю deposit-wallet и проставляю разрешения (без газа с твоей стороны). "
            "Это займёт 30–60 секунд.",
            parse_mode="HTML",
        )
        try:
            result = _register_deposit_wallet(tg_user.id, db_user)
            dw = result.get("deposit_wallet", "")
            await query.edit_message_text(
                "✅ <b>Кошелёк готов к торговле!</b>\n\n"
                f"Торговый адрес (deposit wallet):\n<code>{dw}</code>\n\n"
                "Бот может копировать сделки. Включи копирование: /resume",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
                ]]),
            )
        except Exception as exc:
            log.exception("register_failed_cb", user=tg_user.id)
            await query.edit_message_text(
                f"❌ <b>Ошибка регистрации:</b>\n<code>{str(exc)[:300]}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔁 Повторить", callback_data="register"),
                    InlineKeyboardButton("🏠 Главное меню", callback_data="menu"),
                ]]),
            )
        return

    if data == "wrap":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user or not db_user.get("wallet_address"):
            await query.answer("Сначала отправь /start", show_alert=True)
            return
        await query.answer("♻️ Конвертирую…")
        try:
            from worker.tasks import wrap_collateral
            wrap_collateral.delay(db_user["id"])
            await query.edit_message_text(
                "♻️ <b>Конвертирую USDC.e → pUSD…</b>\n\nРезультат придёт отдельным сообщением.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💼 Кошелёк", callback_data="wallet"),
                    InlineKeyboardButton("🏠 Меню", callback_data="menu"),
                ]]),
            )
        except Exception:
            await query.answer("Не удалось запустить", show_alert=True)
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

        try:
            from worker.tasks import withdraw_funds
            withdraw_funds.delay(db_user["id"], to_addr, amount)
            await query.edit_message_text(
                "⏳ <b>Выполняю вывод…</b>\n\n"
                "При необходимости конвертирую pUSD → USDC.e и отправляю. "
                "Результат с ссылкой на транзакцию придёт отдельным сообщением.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
                ]]),
            )
            log.info("withdrawal_queued", user_id=tg_user.id, amount=amount)
        except Exception:
            await query.edit_message_text(
                "❌ <b>Не удалось запустить вывод.</b> Попробуй позже.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
                ]]),
            )
            log.exception("withdrawal_dispatch_failed", user_id=tg_user.id)
        return

    if data == "positions":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return
        await query.answer("⏳ Загружаю позиции…")
        text, kb = _build_positions(db_user, context)
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )
        return

    if data.startswith("close_"):
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return
        try:
            idx = int(data.split("_", 1)[1])
        except ValueError:
            await query.answer("Ошибка", show_alert=True)
            return
        cache = context.user_data.get("pos_cache") or []
        if idx >= len(cache):
            await query.answer("Список устарел, открой /positions заново", show_alert=True)
            return
        token_id = cache[idx]
        await query.answer("⏳ Закрываю позицию…")
        try:
            from worker.tasks import close_position
            close_position.delay(db_user["id"], token_id, "manual")
            await query.edit_message_text(
                "⏳ <b>Закрываю позицию…</b>\n\n"
                "Отправляю ордер на продажу. Результат придёт отдельным сообщением.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📊 Позиции", callback_data="positions"),
                    InlineKeyboardButton("🏠 Меню", callback_data="menu"),
                ]]),
            )
        except Exception:
            log.exception("close_dispatch_failed", user=tg_user.id)
            await query.answer("Не удалось закрыть, попробуй позже", show_alert=True)
        return

    if data == "pnl" or data.startswith("pnl_"):
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return
        period = data[len("pnl_"):] if data.startswith("pnl_") else "day"
        if period not in _PNL_WINDOWS:
            period = "day"
        await query.answer("⏳ Считаю P&L…")
        await query.edit_message_text(
            _build_pnl(db_user, period),
            parse_mode="HTML",
            reply_markup=_pnl_kb(period),
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
