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
    MAX_WALLETS_PER_USER,
    count_wallets,
    create_access_code,
    create_wallet,
    get_active_wallet,
    get_subscription_status,
    get_user_by_telegram_id,
    get_user_by_username,
    get_wallet,
    is_admin,
    list_wallets,
    redeem_access_code,
    set_active_wallet,
    set_subscription,
    update_user,
    upsert_user,
)
from core.wallet import generate_wallet

log = structlog.get_logger(__name__)

# ─── Keyboards ────────────────────────────────────────────────────────────────

def _is_risk_paused(db_user: dict) -> bool:
    """Return True when the user's copying is currently paused by a risk breaker."""
    from datetime import datetime, timezone
    paused_until = db_user.get("copy_paused_until")
    if not paused_until:
        return False
    try:
        from dateutil.parser import parse as _p
        pu = _p(paused_until)
        if pu.tzinfo is None:
            pu = pu.replace(tzinfo=timezone.utc)
        return pu > datetime.now(timezone.utc)
    except Exception:
        return False


def _onboarding_stage(db_user: dict) -> str:
    """Derive onboarding stage from existing DB fields — no migration required.

    fresh  → wallet not yet created
    demo   → wallet exists, NOT registered, signals-only (BP15 legacy funnel)
    intent → opted into autotrade but not yet registered on Polymarket
    active → registered on Polymarket (BP27: registration happens at creation,
             so a registered user is never routed back to the demo welcome —
             the dashboard checklist covers funding / system-on from here)
    """
    if not db_user.get("wallet_address"):
        return "fresh"
    if db_user.get("wallet_registered"):
        return "active"
    return "demo" if db_user.get("is_signal_only", True) else "intent"


def _main_kb(is_paused: bool = False, is_demo: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("💼 Кошелёк",  callback_data="wallet"),
            InlineKeyboardButton("📊 Позиции",  callback_data="positions"),
        ],
        [
            InlineKeyboardButton("💰 P&L",      callback_data="pnl"),
            InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
        ],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
    ]
    if is_demo:
        rows.insert(0, [InlineKeyboardButton(
            "🚀 Перейти к автоторговле", callback_data="onb_autotrade"
        )])
    if is_paused:
        rows.insert(0, [InlineKeyboardButton(
            "🔓 Снять блокировку риск-менеджера", callback_data="unlock_drawdown"
        )])
    return InlineKeyboardMarkup(rows)


def _signals_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐️ Подписка", callback_data="subscription"),
            InlineKeyboardButton("❓ Как работает", callback_data="help"),
        ],
    ])


# ── BP15: Progressive-disclosure onboarding keyboards ────────────────────────

def _onboarding_kb() -> InlineKeyboardMarkup:
    """L0 — Welcome screen. Primary CTA is risk-free demo, money CTA is secondary."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Смотреть сигналы (без риска)", callback_data="onb_signals")],
        [InlineKeyboardButton("🚀 Перейти к автоторговле",       callback_data="onb_autotrade")],
        [
            InlineKeyboardButton("❓ Как это работает", callback_data="help"),
            InlineKeyboardButton("🛡 Это безопасно?",  callback_data="onb_trust"),
        ],
    ])


def _autotrade_gate_kb() -> InlineKeyboardMarkup:
    """L1 — Trust gate. Still no address; deposit reveal requires an explicit tap."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Показать адрес для пополнения", callback_data="onb_fund_steps")],
        [InlineKeyboardButton("💸 А как выводить деньги?",        callback_data="onb_withdraw_info")],
        [InlineKeyboardButton("↩️ Вернуться в режим сигналов",    callback_data="onb_signals")],
    ])


def _funding_steps_kb(registered: bool = False) -> InlineKeyboardMarkup:
    """L2 — Funding screen. The deposit address lives only behind this screen.

    BP27: users from the new flow are already registered at wallet creation, so
    their action button is «Включить систему»; legacy demo users still see the
    register button (their EOA has no deposit wallet yet).
    """
    action = (
        InlineKeyboardButton("🚀 Включить систему", callback_data="system_on")
        if registered
        else InlineKeyboardButton("🔐 Зарегистрировать кошелёк", callback_data="register")
    )
    return InlineKeyboardMarkup([
        [action],
        [
            InlineKeyboardButton("🔄 Проверить баланс", callback_data="wallet_balance"),
            InlineKeyboardButton("💸 Как вывести",      callback_data="onb_withdraw_info"),
        ],
        [InlineKeyboardButton("↩️ Назад", callback_data="onb_autotrade")],
    ])


# ── BP27: explicit wallet-creation onboarding ────────────────────────────────

def _onb_create_kb() -> InlineKeyboardMarkup:
    """BP27 Screen A — wallet creation is the primary CTA, demo stays available."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Создать мой Polymarket-кошелёк", callback_data="onb_create_wallet")],
        [InlineKeyboardButton("🎬 Смотреть сигналы (без риска)",   callback_data="onb_signals")],
        [InlineKeyboardButton("❓ Как это устроено?",              callback_data="onb_how_wallet")],
    ])


def _copy_address_button(addr: str) -> InlineKeyboardButton:
    """Native tap-to-copy button (Bot API 7.11, PTB >= 21.7).

    On older PTB installs falls back to a callback that sends the address as a
    <code> message — tapping monospace text in Telegram copies it too.
    """
    try:
        from telegram import CopyTextButton
        return InlineKeyboardButton("📋 Скопировать адрес кошелька",
                                    copy_text=CopyTextButton(addr))
    except ImportError:
        return InlineKeyboardButton("📋 Скопировать адрес кошелька",
                                    callback_data="onb_copy_addr")


def _wallet_ready_kb(addr: str) -> InlineKeyboardMarkup:
    """BP27 Screen C keyboard — verify, copy, fund, custody."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Проверить кошелёк в блокчейне",
                              url=f"https://polygonscan.com/address/{addr}")],
        [_copy_address_button(addr)],
        [InlineKeyboardButton("💳 Пополнить мой Polymarket-кошелёк",
                              callback_data="onb_fund_steps")],
        [InlineKeyboardButton("❓ Кто управляет кошельком?", callback_data="onb_custody")],
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
        [InlineKeyboardButton("👛 Мои кошельки", callback_data="wallet_list")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
    ])


# ── BP24: multi-wallet ────────────────────────────────────────────────────────

import re as _re

_WALLET_NAME_RE = _re.compile(r"^[A-Za-z0-9 ]{1,24}$")


def _valid_wallet_name(name: str) -> bool:
    """Letters, numbers and spaces only, 1–24 chars (mirrors the Kreo-style flow)."""
    return bool(_WALLET_NAME_RE.match((name or "").strip()))


def _wallets_list_text(wallets: list[dict]) -> str:
    lines = ["👛 <b>Мои кошельки</b>\n"]
    for w in wallets:
        mark = "✅ " if w.get("is_active") else "▫️ "
        dw = w.get("deposit_wallet_address") or w.get("wallet_address") or ""
        short = f"{dw[:6]}…{dw[-4:]}" if dw else "—"
        active = " <i>(активный)</i>" if w.get("is_active") else ""
        lines.append(f"{mark}<b>{w.get('name')}</b>{active}\n<code>{short}</code>")
    lines.append(
        "\nНовые сделки копируются только на <b>активный</b> кошелёк. "
        "Открытые позиции остальных кошельков продолжают отслеживаться и клеймиться."
    )
    return "\n".join(lines)


def _wallets_list_kb(wallets: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for w in wallets:
        if not w.get("is_active"):
            rows.append([InlineKeyboardButton(
                f"🔄 Сделать активным: {w.get('name')}",
                callback_data=f"wal_switch:{w['id']}",
            )])
    if len(wallets) < MAX_WALLETS_PER_USER:
        rows.append([InlineKeyboardButton("➕ Создать кошелёк", callback_data="wallet_new")])
    rows.append([InlineKeyboardButton("↩️ Назад", callback_data="wallet")])
    return InlineKeyboardMarkup(rows)


def _stop_resume_kb(copy_active: bool) -> InlineKeyboardMarkup:
    if copy_active:
        btn = InlineKeyboardButton("⏸ Приостановить", callback_data="stop")
    else:
        btn = InlineKeyboardButton("▶️ Возобновить", callback_data="resume")
    return InlineKeyboardMarkup([[btn], [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")]])


def _settings_kb(
    copy_active: bool,
    current_max: float,
    sizing_mode: str = "fixed",
    max_daily: int | None = None,
    is_signal_only: bool = False,
) -> InlineKeyboardMarkup:
    def _label(val: int) -> str:
        mark = " ✓" if abs(current_max - val) < 0.5 else ""
        return f"${val}{mark}"

    def _daily_label(n: int) -> str:
        mark = " ✓" if max_daily == n else ""
        return f"{n}/день{mark}"

    toggle_btn = (
        InlineKeyboardButton("⏸ Приостановить", callback_data="stop")
        if copy_active
        else InlineKeyboardButton("▶️ Возобновить", callback_data="resume")
    )

    # Mode toggle: full copy-trading vs signals-only (no on-chain trades).
    copy_mode_btn = InlineKeyboardButton(
        "🤖 Копитрейдинг ✓" if not is_signal_only else "🤖 Копитрейдинг",
        callback_data="mode_copy",
    )
    signal_mode_btn = InlineKeyboardButton(
        "🔔 Только сигналы ✓" if is_signal_only else "🔔 Только сигналы",
        callback_data="mode_signal",
    )

    is_kelly = sizing_mode == "kelly"
    fixed_btn = InlineKeyboardButton(
        "📊 Фиксированный ✓" if not is_kelly else "📊 Фиксированный",
        callback_data="sizing_fixed",
    )
    kelly_btn = InlineKeyboardButton(
        "🤖 Kelly ✓ (рекомендуется)" if is_kelly else "🤖 Kelly (рекомендуется)",
        callback_data="sizing_kelly",
    )

    off_label = "♾ Без лимита" + (" ✓" if max_daily is None else "")

    return InlineKeyboardMarkup([
        # Trading mode: automatic copy vs signals-only.
        [copy_mode_btn, signal_mode_btn],
        [
            InlineKeyboardButton(_label(10),  callback_data="setmax_10"),
            InlineKeyboardButton(_label(25),  callback_data="setmax_25"),
            InlineKeyboardButton(_label(50),  callback_data="setmax_50"),
            InlineKeyboardButton(_label(100), callback_data="setmax_100"),
        ],
        [InlineKeyboardButton("✏️ Своё значение", callback_data="setmax_custom")],
        # Sizing mode toggle
        [fixed_btn, kelly_btn],
        [InlineKeyboardButton("❓ Что такое Kelly-сайзинг?", callback_data="kelly_info")],
        # BP13.2: daily trade limit
        [
            InlineKeyboardButton(_daily_label(1),  callback_data="setdaily_1"),
            InlineKeyboardButton(_daily_label(5),  callback_data="setdaily_5"),
            InlineKeyboardButton(_daily_label(10), callback_data="setdaily_10"),
        ],
        [
            InlineKeyboardButton("✏️ Свой лимит/день", callback_data="setdaily_custom"),
            InlineKeyboardButton(off_label,         callback_data="setdaily_off"),
        ],
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
    funded_enough = (dw_pusd + on_eoa) >= MIN_USDC_READY

    # Hide checklist once all SETUP steps are done.
    # Balance level is NOT a setup condition — money may be deployed in open positions,
    # and the bot will alert separately when funds are insufficient for a new trade.
    setup_done = registered and sub.get("active") and copy_active
    if setup_done:
        return ""

    def mark(ok: bool) -> str:
        return "✅" if ok else "⬜️"

    lines = ["📋 <b>Чек-лист запуска</b>\n"]
    lines.append(f"{mark(registered)} 1. Настроить торговый кошелёк (/register, без газа)")
    lines.append(f"{mark(funded_enough)} 2. Пополнить <b>USDC</b> (сеть Polygon)")
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
    is_paused = _is_risk_paused(db_user)
    if is_paused:
        risk_state = db_user.get("risk_state") or "paused_drawdown"
        pause_reason = "просадка" if risk_state == "paused_drawdown" else "дневной лимит"
        copy_icon = f"🛑 Заблокировано ({pause_reason})"
    elif copy_active:
        copy_icon = "🟢 Работает"
    else:
        copy_icon = "⏸ Пауза"
    max_pos = db_user.get("max_position_usdc") or 25
    sizing_mode = db_user.get("sizing_mode") or "fixed"
    max_daily = db_user.get("max_daily_trades")
    sizing_icon = "🤖 Kelly" if sizing_mode == "kelly" else f"📊 Фикс ${max_pos:.0f}"
    daily_icon = f"{max_daily}/день" if max_daily is not None else "♾"
    checklist = _checklist(db_user)
    mid = (
        f"{checklist}\n\n"
        if checklist
        else "✅ <b>Всё настроено</b> — бот отслеживает китов и копирует сделки.\n\n"
    )
    paused_banner = (
        "\n🔓 <b>Нажми «Снять блокировку» чтобы возобновить досрочно.</b>\n"
        if is_paused else ""
    )
    return (
        f"👋 <b>Привет, {first_name}!</b>\n\n"
        "🧠 <b>PolyMind AI</b> — интеллектуальный копитрейдинг\n"
        "Бот следит за крупными покупками китов на быстрых рынках "
        "и копирует их на твой кошелёк, а ИИ присылает анализ.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Кошелёк: <code>{addr_short}</code>\n"
        f"🔄 Автокопирование: {copy_icon}\n"
        f"💵 Позиция: <b>{sizing_icon}</b>\n"
        f"🔁 Лимит/день: <b>{daily_icon}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{mid}"
        f"{paused_banner}"
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


# ── BP15: Progressive-disclosure onboarding texts ────────────────────────────

def _onboarding_welcome_text() -> str:
    """L0 — First message. Zero money asks, zero deposit address."""
    return (
        "🧠 <b>Добро пожаловать в PolyMind AI!</b>\n\n"
        "Мы копируем сделки проверенных <b>китов Polymarket</b> — трейдеров, которые "
        "годами стабильно зарабатывают на прогнозах. Наш ИИ следит за их крупными "
        "покупками 24/7 и присылает разбор каждой.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🎬 <b>Начни без риска</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Мы понимаем, что доверие нужно заслужить. Начни с <b>режима сигналов</b> — "
        "посмотри, как мы торгуем, без риска для твоих средств.\n\n"
        "Ты будешь получать те же сигналы по китам с ИИ-анализом, что и платные "
        "подписчики, а решение о деньгах примешь позже — когда сам увидишь результат.\n\n"
        "👇 С чего начнём?"
    )


def _onb_signals_text() -> str:
    """Confirm that demo / signals mode is on."""
    return (
        "🎬 <b>Режим сигналов включён</b>\n\n"
        "Теперь ты получаешь сигналы по китам с ИИ-анализом — <b>без единого цента на "
        "счёте</b>. По каждому сигналу: событие, исход, цена входа кита, объём и оценка "
        "риска от ИИ + ссылка на рынок.\n\n"
        "Когда захочешь, чтобы бот торговал это <b>за тебя автоматически</b> — "
        "нажми «🚀 Перейти к автоторговле». Это займёт пару минут."
    )


def _autotrade_gate_text() -> str:
    """L1 — Trust reassurance before showing the deposit address."""
    return (
        "🚀 <b>Автоторговля — сделки копируются сами</b>\n\n"
        "В этом режиме бот сам открывает позиции на <b>твоём личном кошельке</b>, как "
        "только кит заходит крупно. Перед первым пополнением — 3 факта, чтобы было "
        "спокойно:\n\n"
        "🔑 <b>Кошелёк под твоим контролем.</b> Вывести средства можно в любой момент "
        "кнопкой «💸 Вывод» — без подтверждений с нашей стороны.\n"
        "🌐 <b>Только сеть Polygon.</b> Не Ethereum, не BSC — иначе деньги уйдут в "
        "чужую сеть.\n"
        "🪙 <b>Только USDC.</b> Бот сам сконвертирует в торговый баланс (pUSD). Газ "
        "(POL) для старта не нужен — регистрация газлесс.\n\n"
        "Готов? Покажу адрес и пошаговую инструкцию по пополнению."
    )


def _onb_trust_text() -> str:
    """On-demand trust FAQ — answers the most common fears honestly."""
    return (
        "🛡 <b>Часто волнует — отвечаем честно</b>\n\n"
        "💸 <b>Деньги выводятся в любой момент.</b>\n"
        "Кнопка «💸 Вывод» отправит USDC на любой твой адрес Polygon. Мы не держим "
        "твои средства в заложниках и не требуем разрешений.\n\n"
        "🌐 <b>Только сеть Polygon.</b>\n"
        "Пополняй строго в сети Polygon (не Ethereum / BSC / Arbitrum) — иначе монеты "
        "уйдут в чужую сеть и потеряются. Это главное правило безопасности.\n\n"
        "🪙 <b>Только USDC, и старт без газа.</b>\n"
        "Достаточно обычного USDC — бот сам сконвертирует его в торговый баланс. "
        "Регистрация кошелька газлесс: POL на старте не нужен.\n\n"
        "🎬 <b>Старт — бесплатный и без депозита.</b>\n"
        "Режим сигналов не требует ни цента на счёте. Сначала смотришь, потом решаешь."
    )


def _onb_withdraw_info_text() -> str:
    """Withdraw reassurance — shown on demand from L1/L2."""
    return (
        "💸 <b>Вывод средств — в любой момент</b>\n\n"
        "Нажимаешь «💸 Вывод» → вводишь свой адрес Polygon → сумму → подтверждаешь.\n"
        "Бот сконвертирует pUSD обратно в USDC и отправит на указанный адрес; в ответ "
        "придёт ссылка на транзакцию в Polygonscan.\n\n"
        "Никаких блокировок и периодов ожидания — деньги твои."
    )


# ── BP27: explicit wallet-creation onboarding texts ──────────────────────────

def _onb_create_text() -> str:
    """BP27 Screen A — /start for a fresh user: explicit wallet creation."""
    return (
        "🧠 <b>Добро пожаловать в PolyMind AI!</b>\n\n"
        "👛 <b>Создадим ваш торговый кошелёк Polymarket</b>\n\n"
        "Сначала мы создадим для вас отдельный торговый кошелёк Polymarket в сети "
        "Polygon. Бот делает это через API-инфраструктуру Polymarket прямо внутри "
        "Telegram — вам не нужно регистрироваться на сайте и разбираться с "
        "технической настройкой.\n\n"
        "После создания у кошелька появится собственный адрес формата 0x… Именно на "
        "этом адресе будут учитываться ваш торговый баланс, позиции и история "
        "сделок.\n\n"
        "Пока вы не нажмёте кнопку «Включить систему», бот не будет совершать сделки."
    )


def _onb_how_wallet_text() -> str:
    """BP27 Screen B — plain-words explainer behind «Как это устроено?»."""
    return (
        "❓ <b>Как это устроено</b>\n\n"
        "Простыми словами: Telegram-бот здесь работает как удобный интерфейс. Сам "
        "торговый кошелёк создаётся не во внутреннем балансе PolyMind, а через "
        "инфраструктуру Polymarket.\n\n"
        "Когда вы нажимаете кнопку создания, бот отправляет запрос на развёртывание "
        "отдельного кошелька для вашего профиля. После создания Polymarket-кошелёк "
        "получает уникальный адрес в сети Polygon.\n\n"
        "В дальнейшем на этом адресе учитываются ваш торговый баланс, открытые "
        "позиции и результаты сделок.\n\n"
        "Само создание кошелька ничего не списывает и не запускает торговлю."
    )


_ONB_CREATING_STAGE1 = (
    "⏳ <b>Создаём ваш Polymarket-кошелёк</b>\n\n"
    "Получаем отдельный адрес для вашего профиля…"
)

_ONB_CREATING_STAGE2 = (
    "⏳ <b>Создаём ваш Polymarket-кошелёк</b>\n\n"
    "Адрес получен. Регистрируем кошелёк\n"
    "в инфраструктуре Polymarket…"
)


def _wallet_ready_text(addr: str, pusd: float, created_str: str) -> str:
    """BP27 Screen C — the finished wallet card."""
    return (
        "✅ <b>Ваш Polymarket-кошелёк готов</b>\n\n"
        "Это ваш торговый адрес для работы на Polymarket:\n"
        f"<code>{addr}</code>\n\n"
        "🌐 Сеть: <b>Polygon</b>\n"
        f"💵 Баланс: <b>{pusd:.2f} pUSD</b>\n"
        "🤖 Торговая система: <b>выключена</b>\n"
        f"🗓 Дата создания: {created_str}\n\n"
        "Сейчас кошелёк пустой и ничего не делает. Вы можете открыть его адрес в "
        "блокчейне и убедиться, что для вашего профиля создан отдельный on-chain "
        "кошелёк.\n\n"
        "Следующим шагом вы сможете пополнить именно этот кошелёк. После пополнения "
        "система всё равно останется выключенной, пока вы самостоятельно не "
        "подтвердите запуск."
    )


def _onb_custody_text() -> str:
    """BP27 — «Кто управляет кошельком?» explainer."""
    return (
        "🔐 <b>Кто управляет кошельком</b>\n\n"
        "Кошелёк создан для вашего профиля, и средства на нём — ваши.\n\n"
        "🤖 <b>Сделки.</b> Бот подписывает сделки ключами кошелька, которые хранит "
        "в зашифрованном виде, — и только после того, как вы нажмёте «Включить "
        "систему». До этого он не совершит ни одной операции.\n\n"
        "💸 <b>Вывод.</b> В любой момент кнопкой «💸 Вывод»: USDC уходит на любой "
        "ваш адрес в сети Polygon, без подтверждений с нашей стороны.\n\n"
        "🔎 <b>Прозрачность.</b> Кошелёк живёт в блокчейне Polygon — все операции "
        "видны по его адресу на Polygonscan."
    )


def _funding_steps_text(addr: str, registered: bool = False) -> str:
    """L2 — Only place the deposit address and network instructions appear."""
    if registered:
        # BP27: wallet already registered at creation — step 2 is automatic,
        # the explicit action is enabling the system.
        return (
            "💳 <b>Пополнение — 3 шага</b>\n\n"
            "📬 <b>Ваш адрес для пополнения (USDC, сеть Polygon):</b>\n"
            f"<code>{addr}</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ Отправьте <b>USDC</b> на адрес выше — <b>строго в сети Polygon</b>\n"
            "2️⃣ Бот сам переведёт средства на торговый баланс (pUSD)\n"
            "3️⃣ Нажмите <b>🚀 Включить систему</b> — и бот начнёт копировать сделки\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⚠️ <b>Только сеть Polygon</b> — не Ethereum, не BSC, не Arbitrum!\n"
            "ℹ️ Пока система выключена, бот не совершает сделок — даже с балансом."
        )
    return (
        "🚀 <b>Пополнение — 3 шага</b>\n\n"
        "📬 <b>Твой адрес для пополнения (USDC, сеть Polygon):</b>\n"
        f"<code>{addr}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ Отправь <b>USDC</b> на адрес выше — <b>строго в сети Polygon</b>\n"
        "2️⃣ Нажми <b>🔐 Зарегистрировать кошелёк</b> (газлесс, 30–60 сек)\n"
        "3️⃣ Готово — бот начнёт копировать крупные сделки китов\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ <b>Только сеть Polygon</b> — не Ethereum, не BSC, не Arbitrum!\n"
        "ℹ️ Бот сам сконвертирует USDC в торговый баланс (pUSD). Вывести средства можно "
        "в любой момент кнопкой «💸 Вывод»."
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


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global PTB error handler — BP9 Layer 2a.

    Converts silent handler crashes into a structured log entry and a user-visible
    fallback, so a bug can never again produce a dead button with no trace.
    """
    data = ""
    user_id = None
    if isinstance(update, Update):
        if update.callback_query:
            data = update.callback_query.data or ""
        if update.effective_user:
            user_id = update.effective_user.id
    log.error(
        "telegram_callback_error",
        data=data,
        user_id=user_id,
        exc=str(context.error),
    )
    if isinstance(update, Update) and update.callback_query:
        try:
            await update.callback_query.answer(
                "⚠️ Что-то пошло не так — открой /start", show_alert=True
            )
        except Exception:
            pass


def build_application() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).post_init(_set_commands).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("onboarding", cmd_onboarding))
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
    app.add_handler(CommandHandler("sub",       cmd_grant))
    app.add_handler(CommandHandler("newcode",   cmd_newcode))
    app.add_handler(CommandHandler("codes",     cmd_codes))
    app.add_handler(CallbackQueryHandler(callback_handler))
    # Must be last — catches free-text input
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    # BP9 Layer 2a: global error handler — no more silent dead buttons.
    app.add_error_handler(on_error)
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
        # BP27: explicit wallet creation — no silent key generation on /start.
        # Keys are generated only when the user taps «Создать мой кошелёк».
        await update.message.reply_text(  # type: ignore[union-attr]
            _onb_create_text(),
            parse_mode="HTML",
            reply_markup=_onb_create_kb(),
        )
    else:
        # BP15: route by onboarding stage — demo users get the value-first welcome.
        if _onboarding_stage(db_user) == "demo":
            await update.message.reply_text(  # type: ignore[union-attr]
                _onboarding_welcome_text(),
                parse_mode="HTML",
                reply_markup=_onboarding_kb(),
            )
        else:
            await update.message.reply_text(  # type: ignore[union-attr]
                _dashboard_text(db_user, tg_user.first_name),
                parse_mode="HTML",
                reply_markup=_main_kb(is_paused=_is_risk_paused(db_user)),
            )


async def cmd_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """BP32: replay the wallet-creation onboarding from screen A (partner review).

    Safe for everyone: the create step is idempotent — an existing registered
    wallet only replays the staged messages and re-renders the card, so repeat
    runs never generate new keys or deploy anything.
    """
    tg_user = update.effective_user
    if not tg_user:
        return
    if not settings.auto_copy_enabled:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Онбординг с кошельком доступен только в режиме автокопирования.",
        )
        return
    upsert_user(tg_user.id)
    await update.message.reply_text(  # type: ignore[union-attr]
        _onb_create_text(),
        parse_mode="HTML",
        reply_markup=_onb_create_kb(),
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
    for k in ("withdraw_step", "withdraw_to", "withdraw_amount"):
        context.user_data.pop(k, None)
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


def _is_dead_loss(p: dict) -> bool:
    """A resolved-and-lost position: market settled (redeemable) but this outcome
    is worth ~$0. It can never be sold (no bid) and clutters the active view —
    the loss is already reported via the resolution notification."""
    return bool(p.get("redeemable")) and float(p.get("cur_price") or 0) < 0.05


def _live_positions(wallet: str | None) -> list[dict]:
    """Open positions the user still has real exposure to (excludes settled losses)."""
    if not wallet:
        return []
    try:
        from core.polymarket import get_positions
        return [
            p for p in get_positions(wallet)
            if p["shares"] > 0 and not _is_dead_loss(p)
        ]
    except Exception:
        return []


def _db_entry_prices(db_user: dict) -> dict:
    """BP16: {token_id: entry_price} from our copy_trades ledger (fallback cost basis).

    Fails open (empty dict) so the positions view degrades to the on-chain avg
    rather than erroring if the DB read fails."""
    try:
        from core.db import get_entry_prices_by_token
        uid = db_user.get("id")
        return get_entry_prices_by_token(uid) if uid is not None else {}
    except Exception:
        return {}


def _effective_entry(p: dict, db_entry: dict) -> float:
    """BP16: prefer the Data-API blended avg when populated, else our DB entry_price.

    Once the indexer catches up, avgPrice is the blended truth across partial fills;
    our single-row entry_price is the reliable zero-gap fallback that kills the
    "@ 0.000" display for freshly-opened positions."""
    api_avg = float(p.get("avg_price") or 0)
    if api_avg > 0:
        return api_avg
    return float(db_entry.get(p.get("token_id"), 0.0) or 0.0)


def _position_pnl(p: dict, entry: float) -> tuple[float, float | None]:
    """BP16: PnL ($, %) from a single effective entry, guarded against divide-by-zero.

    Returns (pnl_usd, pct) where pct is None when no cost basis exists anywhere
    (legacy pre-BP1 rows) — callers render "—" instead of a misleading +0%."""
    cur = float(p.get("cur_price") or 0)
    shares = float(p.get("shares") or 0)
    if entry > 0:
        return shares * (cur - entry), (cur - entry) / entry
    return float(p.get("cash_pnl") or 0), None


def _build_positions(db_user: dict, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, InlineKeyboardMarkup]:
    """Render live positions (real P&L from data-api) with per-position close buttons."""
    wallet = _trading_wallet(db_user)
    positions = _live_positions(wallet)

    # Cache token_ids for the close-by-index callback (callback_data is 64-byte capped).
    context.user_data["pos_cache"] = [p["token_id"] for p in positions]

    if not positions:
        return (
            "📊 <b>Открытые позиции</b>\n\n"
            "Нет открытых позиций.\n\n"
            "💡 Позиции появятся здесь, когда бот скопирует сделку.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="menu")]]),
        )

    # BP16: the Data-API avgPrice is 0 for freshly-opened positions on our proxy
    # wallets. Overlay our own stored entry_price (copy_trades) as the zero-gap
    # fallback so entry price and % PnL never render as 0.000 / +0%.
    db_entry = _db_entry_prices(db_user)

    lines = [f"📊 <b>Активные позиции</b> ({len(positions)})\n"]
    buttons: list[list[InlineKeyboardButton]] = []
    total_value = 0.0
    total_pnl = 0.0
    from core.polymarket import event_url
    for i, p in enumerate(positions):
        title = (p.get("title") or "—")[:40]
        outcome = p.get("outcome") or "—"
        shares = p["shares"]
        cur = p["cur_price"]
        # BP16: prefer a valid on-chain blended avg, fall back to our DB entry.
        entry = _effective_entry(p, db_entry)
        pnl, pct = _position_pnl(p, entry)
        total_value += p["current_value"]
        total_pnl += pnl
        icon = "📈" if pnl >= 0 else "📉"
        tag = " · ✅ к выводу" if p.get("redeemable") else ""
        url = event_url(p.get("event_slug"))
        title_html = f"<a href=\"{url}\">{title}</a>" if url else f"<b>{title}</b>"
        entry_str = f"{entry:.3f}" if entry > 0 else "—"
        pct_str = f"{pct:+.0%}" if pct is not None else "—"
        lines.append(
            f"{i+1}. {title_html} · {outcome}{tag}\n"
            f"   {shares:.0f} шт @ {entry_str} → {cur:.3f} · {icon} <b>{pnl:+.2f}$</b> ({pct_str})"
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
    """Real P&L: realized over the chosen period + current unrealized snapshot.

    BP26.7: realized stats come from OUR copy_trades ledger, not the Data-API
    /closed-positions feed — lost positions never appear there (their worthless
    tokens are never sold/redeemed), which showed users a fake 100% winrate.
    """
    import time as _t
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    wallet = _trading_wallet(db_user)
    open_pos = []
    if wallet:
        try:
            open_pos = _live_positions(wallet)
        except Exception:
            pass

    window = _PNL_WINDOWS.get(period)
    since_iso = (
        (_dt.now(_tz.utc) - _td(seconds=window)).isoformat() if window else None
    )
    try:
        from core.db import get_realized_pnl_rows
        period_closed = get_realized_pnl_rows(db_user["id"], since_iso)
    except Exception:
        period_closed = []
    realized = sum(float(c["realized_pnl"] or 0) for c in period_closed)
    wins = sum(1 for c in period_closed if float(c["realized_pnl"] or 0) > 0)
    losses = sum(1 for c in period_closed if float(c["realized_pnl"] or 0) < 0)

    invested = sum(p["current_value"] for p in open_pos)
    # Compute unrealized P&L robustly from shares × (cur − entry). The API's cashPnl
    # is unreliable on freshly-opened positions (avg_price momentarily 0 → it reports
    # the whole position value as "profit"). BP16: fall back to our stored entry_price
    # when the on-chain avg is 0, and skip positions with no cost basis anywhere.
    db_entry = _db_entry_prices(db_user)
    unrealized = 0.0
    for p in open_pos:
        entry = _effective_entry(p, db_entry)
        cur = float(p.get("cur_price") or 0)
        shares = float(p.get("shares") or 0)
        if entry > 0.001:
            unrealized += shares * (cur - entry)

    r_icon = "📈" if realized >= 0 else "📉"
    u_icon = "📈" if unrealized >= 0 else "📉"
    settled_n = len(period_closed)
    winrate = f" · винрейт <b>{wins / settled_n * 100:.0f}%</b>" if settled_n else ""

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
    sizing_mode = db_user.get("sizing_mode") or "fixed"
    max_daily = db_user.get("max_daily_trades")
    is_signal_only = db_user.get("is_signal_only", False)
    copy_status = "▶️ Активно" if copy_active else "⏸ Приостановлено"
    mode_line = (
        "🔔 Режим: <b>Только сигналы</b> (бот не торгует за тебя)\n"
        if is_signal_only
        else "🤖 Режим: <b>Копитрейдинг</b> (бот торгует автоматически)\n"
    )

    if sizing_mode == "kelly":
        sizing_line = "📐 Сайзинг: <b>🤖 Kelly (авто-расчёт)</b>\n"
        size_hint = (
            f"💵 Потолок позиции: <b>${max_pos:.0f} USDC</b>\n\n"
            "Kelly рассчитает оптимальный размер автоматически — "
            "потолок нужен как страховочный лимит 👇"
        )
    else:
        sizing_line = "📐 Сайзинг: <b>📊 Фиксированный</b>\n"
        size_hint = (
            f"💵 Макс. позиция: <b>${max_pos:.0f} USDC</b>\n\n"
            "Выбери максимальный размер одной позиции 👇"
        )

    daily_line = (
        f"🔁 Лимит/день: <b>{max_daily} сделок</b>\n"
        if max_daily is not None
        else "🔁 Лимит/день: <b>♾ без ограничений</b>\n"
    )

    return (
        f"⚙️ <b>Настройки</b>\n\n"
        f"{mode_line}"
        f"🔄 Копирование: {copy_status}\n"
        f"{sizing_line}"
        f"{daily_line}"
        f"{size_hint}"
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
    sizing_mode = db_user.get("sizing_mode") or "fixed"
    await update.message.reply_text(  # type: ignore[union-attr]
        _settings_text(db_user),
        parse_mode="HTML",
        reply_markup=_settings_kb(copy_active, max_pos, sizing_mode, db_user.get("max_daily_trades"), db_user.get("is_signal_only", False)),
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
    payload = {
        "deposit_wallet_address":  result["deposit_wallet"],
        "deposit_wallet_deployed": True,
        "wallet_registered":       True,
        "clob_api_key":            creds.get("clob_api_key"),
        "clob_secret":             creds.get("clob_secret"),
        "clob_passphrase":         creds.get("clob_passphrase"),
    }
    update_user(telegram_id, payload)
    # BP24: keep the ACTIVE user_wallets row in sync with the users mirror.
    try:
        from core.db import update_wallet
        active = get_active_wallet(db_user["id"]) if db_user.get("id") else None
        if active:
            update_wallet(active["id"], payload)
    except Exception:
        log.warning("register_wallet_row_sync_failed", user=telegram_id)
    return result


async def _show_wallet_card(query, db_user: dict) -> None:
    """BP27 Screen C — render the finished wallet card (edit in place)."""
    addr = db_user.get("wallet_address") or ""

    pusd = 0.0
    try:
        from core.polygon import get_balances
        dw = db_user.get("deposit_wallet_address")
        if dw:
            pusd = get_balances(dw).get("pusd", 0.0)
    except Exception:
        log.warning("wallet_card_balance_read_failed", user_id=db_user.get("id"))

    created_iso = None
    try:
        active = get_active_wallet(db_user["id"]) if db_user.get("id") else None
        created_iso = (active or {}).get("created_at")
    except Exception:
        pass
    created_iso = created_iso or db_user.get("created_at")
    try:
        from dateutil.parser import parse as _pdt
        created_str = _pdt(created_iso).strftime("%d.%m.%Y %H:%M UTC")
    except Exception:
        from datetime import datetime, timezone
        created_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    await query.edit_message_text(
        _wallet_ready_text(addr, pusd, created_str),
        parse_mode="HTML",
        reply_markup=_wallet_ready_kb(addr),
        disable_web_page_preview=True,
    )


def _create_named_wallet(telegram_id: int, user_id: int, name: str) -> dict:
    """BP24: create a brand-new named wallet (no key import), auto-register its
    deposit wallet (gasless deploy + approvals + CLOB creds) and make it active.

    make_active mirrors the new wallet onto users.* so the entry/balance/deposit
    path immediately operates on it; existing wallets keep their open positions
    monitored/redeemed via copy_trades.wallet_id.
    """
    from core.clob import register_deposit_wallet

    w = generate_wallet()
    fields: dict = {
        "wallet_address":         w["address"],
        "wallet_private_key_enc": w["private_key_enc"],
    }
    result = register_deposit_wallet(w["private_key_enc"])
    creds = result.get("creds") or {}
    fields.update({
        "deposit_wallet_address":  result["deposit_wallet"],
        "deposit_wallet_deployed": True,
        "wallet_registered":       True,
        "clob_api_key":            creds.get("clob_api_key"),
        "clob_secret":             creds.get("clob_secret"),
        "clob_passphrase":         creds.get("clob_passphrase"),
    })
    row = create_wallet(user_id, name, fields, make_active=True)
    # New named wallet implies intent to auto-trade (same hand-off as /register).
    update_user(telegram_id, {"is_signal_only": False})
    return {"deposit_wallet": result["deposit_wallet"], "wallet": row}


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
        # BP15: hand-off from demo → active auto-trading mode.
        update_user(tg_user.id, {"is_signal_only": False})
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

    Usage: /grant <@username | telegram_id> [days]
           /sub   <@username | telegram_id> [days]
    Extends an active subscription or reactivates an expired one.
    Protected: accessible only to admins (super-admin env var + admins DB table).
    """
    tg_user = update.effective_user
    if not tg_user or not is_admin(tg_user.id):
        return
    args = context.args or []
    if len(args) < 1:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Использование: <code>/grant &lt;@username | telegram_id&gt; [дней]</code>\n"
            "Пример: <code>/grant @ivan 30</code> · <code>/sub username 14</code>\n\n"
            "Продлевает активную подписку и реактивирует истёкшую.",
            parse_mode="HTML",
        )
        return

    ident = args[0]
    try:
        days = int(args[1]) if len(args) > 1 else 30
    except ValueError:
        days = 30

    # Resolve: bare numeric id (no leading @) → lookup by telegram_id; else by username.
    if ident.lstrip("@").isdigit() and not ident.startswith("@"):
        target_id = int(ident)
        target = get_user_by_telegram_id(target_id) or {"telegram_id": target_id}
    else:
        target = get_user_by_username(ident)
        if not target:
            display = ident if ident.startswith("@") else f"@{ident}"
            await update.message.reply_text(  # type: ignore[union-attr]
                f"❌ Пользователь <b>{display}</b> не найден в базе.\n\n"
                "Он должен сначала запустить основной бот (/start), чтобы ник сохранился. "
                "Либо укажи числовой Telegram ID.",
                parse_mode="HTML",
            )
            return

    target_id = target["telegram_id"]
    try:
        user = set_subscription(target_id, days)
        exp = (user.get("sub_expires_at") or "")[:10]
        uname = f"@{target.get('username')}" if target.get("username") else f"id{target_id}"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"✅ Подписка для <b>{uname}</b> продлена на <b>{days}</b> дней.\n"
            f"Новая дата окончания: <b>{exp}</b>",
            parse_mode="HTML",
        )
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"🎉 <b>Администратор выдал вам подписку на {days} дней!</b>\n\n"
                    f"Действует до: <b>{exp}</b>\n\n"
                    "Бот готов к работе."
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
    """Admin: create a one-time access code and return its deep link. /newcode [days]"""
    tg_user = update.effective_user
    if not tg_user or not is_admin(tg_user.id):
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
    if not tg_user or not is_admin(tg_user.id):
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

    # ── BP24: create-wallet flow (name step) ───────────────────────────────────
    if context.user_data.get("awaiting_wallet_name"):
        if not _valid_wallet_name(text):
            await update.message.reply_text(  # type: ignore[union-attr]
                "⚠️ Только буквы, цифры и пробелы (до 24 символов). Попробуй ещё раз "
                "или нажми Отмена:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="wallet_list")
                ]]),
            )
            return
        name = text.strip()
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user or not db_user.get("wallet_address"):
            context.user_data["awaiting_wallet_name"] = False
            await update.message.reply_text("Сначала отправь /start", parse_mode="HTML")  # type: ignore[union-attr]
            return
        existing = list_wallets(db_user["id"])
        if any((w.get("name") or "").lower() == name.lower() for w in existing):
            await update.message.reply_text(  # type: ignore[union-attr]
                f"⚠️ Кошелёк с именем «{name}» уже есть. Придумай другое имя:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="wallet_list")
                ]]),
            )
            return
        if len(existing) >= MAX_WALLETS_PER_USER:
            context.user_data["awaiting_wallet_name"] = False
            await update.message.reply_text(  # type: ignore[union-attr]
                f"⚠️ Достигнут лимит — максимум {MAX_WALLETS_PER_USER} кошельков.",
                parse_mode="HTML",
            )
            return
        context.user_data["awaiting_wallet_name"] = False
        msg = await update.message.reply_text(  # type: ignore[union-attr]
            f"⏳ <b>Создаю кошелёк «{name}»…</b>\n\n"
            "Генерирую адрес и настраиваю торговлю на Polymarket "
            "(без газа с твоей стороны). Это займёт 30–60 секунд.",
            parse_mode="HTML",
        )
        try:
            result = _create_named_wallet(tg_user.id, db_user["id"], name)
            dw = result.get("deposit_wallet", "")
            await msg.edit_text(  # type: ignore[union-attr]
                f"✅ <b>Кошелёк «{name}» создан и активирован!</b>\n\n"
                f"📬 Адрес для пополнения (USDC, Polygon):\n<code>{dw}</code>\n\n"
                "Новые сделки теперь копируются на этот кошелёк. Пополни его в сети "
                "<b>Polygon</b>, чтобы начать торговлю.",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👛 Мои кошельки", callback_data="wallet_list")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
                ]),
            )
        except Exception as exc:
            log.exception("create_wallet_failed", user=tg_user.id)
            await msg.edit_text(  # type: ignore[union-attr]
                f"❌ <b>Не удалось создать кошелёк:</b>\n<code>{str(exc)[:250]}</code>\n\n"
                "Попробуй ещё раз через минуту.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👛 Мои кошельки", callback_data="wallet_list")
                ]]),
            )
        return

    # ── Withdraw flow ──────────────────────────────────────────────────────────
    withdraw_step = context.user_data.get("withdraw_step")

    if withdraw_step == "address":
        from core.polygon import is_valid_address
        if not is_valid_address(text):
            await update.message.reply_text(  # type: ignore[union-attr]
                "⚠️ Неверный адрес. Укажи корректный адрес Polygon (0x…, 42 символа).\n\nПопробуй ещё раз или нажми Отмена:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="withdraw_cancel")
                ]]),
            )
            return

        context.user_data["withdraw_to"] = text
        context.user_data["withdraw_step"] = "amount"

        db_user = get_user_by_telegram_id(tg_user.id) or {}
        from core.polygon import withdrawable_usdc as _withdrawable_usdc
        avail = _withdrawable_usdc(db_user)

        await update.message.reply_text(  # type: ignore[union-attr]
            f"💵 <b>Сколько USDC вывести?</b>\n\n"
            f"Доступно: <b>${avail:.2f} USDC</b>\n"
            f"На адрес: <code>{text[:10]}…{text[-6:]}</code>\n\n"
            "Введи сумму (например: <code>25</code>):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="withdraw_cancel")
            ]]),
        )
        return

    if withdraw_step == "amount":
        from core.config import settings as _settings
        from core.polygon import withdrawable_usdc as _withdrawable_usdc

        amount_text = text.replace("$", "").replace(",", ".")
        try:
            amount = float(amount_text)
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "⚠️ Неверный формат. Введи число, например: <code>25</code>",
                parse_mode="HTML",
            )
            return

        if amount < _settings.min_withdraw_usdc:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"⚠️ Минимум <b>${_settings.min_withdraw_usdc:.2f} USDC</b>",
                parse_mode="HTML",
            )
            return

        db_user = get_user_by_telegram_id(tg_user.id) or {}
        avail = _withdrawable_usdc(db_user)
        if amount > avail:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"⚠️ Недостаточно средств.\n"
                f"Доступно: <b>${avail:.2f} USDC</b>, запрошено: <b>${amount:.2f} USDC</b>.\n\n"
                "Введи другую сумму или нажми Отмена:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="withdraw_cancel")
                ]]),
            )
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
                    InlineKeyboardButton("✅ Подтвердить вывод", callback_data="withdraw_confirm"),
                    InlineKeyboardButton("❌ Отмена",            callback_data="withdraw_cancel"),
                ]
            ]),
        )
        return

    # ── BP13.2: custom daily trade limit ──────────────────────────────────────
    if context.user_data.get("awaiting_daily_limit"):
        context.user_data["awaiting_daily_limit"] = False
        clean = text.strip().replace(",", "")
        try:
            n = int(float(clean))
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "⚠️ Введи целое число, например: <code>5</code>",
                parse_mode="HTML",
            )
            return
        db_user = get_user_by_telegram_id(tg_user.id) or {}
        copy_active = db_user.get("copy_active", False)
        max_pos = float(db_user.get("max_position_usdc") or 25)
        sizing_mode = db_user.get("sizing_mode") or "fixed"
        if n <= 0:
            update_user(tg_user.id, {"max_daily_trades": None})
            db_user = get_user_by_telegram_id(tg_user.id) or db_user
            await update.message.reply_text(  # type: ignore[union-attr]
                "♾ <b>Лимит снят</b> — сделок без ограничений в день.",
                parse_mode="HTML",
                reply_markup=_settings_kb(copy_active, max_pos, sizing_mode, None, db_user.get("is_signal_only", False)),
            )
        elif n > 100:
            await update.message.reply_text(  # type: ignore[union-attr]
                "⚠️ Максимум — <b>100 сделок в день</b>.",
                parse_mode="HTML",
            )
        else:
            update_user(tg_user.id, {"max_daily_trades": n})
            db_user = get_user_by_telegram_id(tg_user.id) or db_user
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ <b>Лимит установлен: {n} сделок/день (UTC)</b>",
                parse_mode="HTML",
                reply_markup=_settings_kb(copy_active, max_pos, sizing_mode, n, db_user.get("is_signal_only", False)),
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
    sizing_mode = db_user.get("sizing_mode") or "fixed"

    await update.message.reply_text(  # type: ignore[union-attr]
        f"✅ <b>Готово!</b> Макс. позиция: <b>${val:.0f} USDC</b>",
        parse_mode="HTML",
        reply_markup=_settings_kb(copy_active, val, sizing_mode, db_user.get("max_daily_trades"), db_user.get("is_signal_only", False)),
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
                # BP15: demo users return to the value-first welcome, not the full dashboard.
                stage = _onboarding_stage(db_user)
                if stage == "fresh":
                    # BP27: signals-only user without a wallet yet → creation screen.
                    await query.edit_message_text(
                        _onb_create_text(),
                        parse_mode="HTML",
                        reply_markup=_onb_create_kb(),
                    )
                elif stage == "demo":
                    await query.edit_message_text(
                        _onboarding_welcome_text(),
                        parse_mode="HTML",
                        reply_markup=_onboarding_kb(),
                    )
                else:
                    await query.edit_message_text(
                        _dashboard_text(db_user, tg_user.first_name),
                        parse_mode="HTML",
                        reply_markup=_main_kb(is_paused=_is_risk_paused(db_user)),
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

    # ── BP24: multi-wallet management ─────────────────────────────────────────
    if data == "wallet_list":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user or not db_user.get("wallet_address"):
            await query.answer("Сначала отправь /start", show_alert=True)
            return
        wallets = list_wallets(db_user["id"])
        if not wallets:
            # Legacy account created before migration 018 — fold the users-row
            # wallet into an implicit "Wallet 1" view so the UI still works.
            wallets = [{
                "id": db_user.get("active_wallet_id") or 0,
                "name": "Wallet 1",
                "is_active": True,
                "deposit_wallet_address": db_user.get("deposit_wallet_address"),
                "wallet_address": db_user.get("wallet_address"),
            }]
        await query.edit_message_text(
            _wallets_list_text(wallets),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_wallets_list_kb(wallets),
        )
        return

    if data == "wallet_new":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user or not db_user.get("wallet_address"):
            await query.answer("Сначала отправь /start", show_alert=True)
            return
        if count_wallets(db_user["id"]) >= MAX_WALLETS_PER_USER:
            await query.answer(
                f"Максимум {MAX_WALLETS_PER_USER} кошельков.", show_alert=True)
            return
        context.user_data["awaiting_wallet_name"] = True
        await query.edit_message_text(
            "🔐 <b>Создание кошелька — шаг 1 из 1</b>\n\n"
            "Как назвать новый кошелёк?\n\n"
            "Только буквы, цифры и пробелы (до 24 символов).\n"
            "Например: <code>MainWallet</code>, <code>Wallet123</code> или "
            "<code>God of Gamblers</code>.\n\n"
            "<i>Будет создан новый пустой кошелёк — импорт ключа не требуется. "
            "После создания просто пополни его адрес.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="wallet_list")
            ]]),
        )
        return

    if data and data.startswith("wal_switch:"):
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return
        try:
            target_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await query.answer("Некорректный кошелёк", show_alert=True)
            return
        target = get_wallet(target_id)
        if not target or target.get("user_id") != db_user["id"]:
            await query.answer("Кошелёк не найден", show_alert=True)
            return
        set_active_wallet(db_user["id"], target_id)
        await query.answer(f"Активный кошелёк: {target.get('name')}")
        wallets = list_wallets(db_user["id"])
        await query.edit_message_text(
            _wallets_list_text(wallets),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_wallets_list_kb(wallets),
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
            # BP15: hand-off from demo → active auto-trading mode.
            update_user(tg_user.id, {"is_signal_only": False})
            await query.edit_message_text(
                "✅ <b>Кошелёк готов к торговле!</b>\n\n"
                f"Торговый адрес (deposit wallet):\n<code>{dw}</code>\n\n"
                "Бот может копировать сделки. Убедись, что копирование включено: /resume",
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

    # ── BP15: Progressive-disclosure onboarding callbacks ─────────────────────

    if data == "onb_signals":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return
        update_user(tg_user.id, {"is_signal_only": True})
        await query.edit_message_text(
            _onb_signals_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Перейти к автоторговле", callback_data="onb_autotrade")],
                [
                    InlineKeyboardButton("⭐️ Подписка",      callback_data="subscription"),
                    InlineKeyboardButton("🏠 Меню",           callback_data="menu"),
                ],
            ]),
        )
        return

    if data == "onb_autotrade":
        # L1 — show trust gate; does NOT flip is_signal_only yet (abandoned funnel stays safe).
        await query.edit_message_text(
            _autotrade_gate_text(),
            parse_mode="HTML",
            reply_markup=_autotrade_gate_kb(),
        )
        return

    if data == "onb_trust":
        await query.edit_message_text(
            _onb_trust_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 Остаться на сигналах",    callback_data="onb_signals")],
                [InlineKeyboardButton("🚀 Перейти к автоторговле",  callback_data="onb_autotrade")],
            ]),
        )
        return

    if data == "onb_fund_steps":
        # L2 — the ONLY place the deposit address and network instructions are shown.
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return
        if not db_user.get("wallet_address"):
            # BP27: walletless demo user opted into autotrade — the wallet must
            # be created first, so route into the explicit creation flow.
            await query.edit_message_text(
                _onb_create_text(),
                parse_mode="HTML",
                reply_markup=_onb_create_kb(),
            )
            return
        addr = db_user["wallet_address"]
        registered = bool(db_user.get("wallet_registered"))
        await query.edit_message_text(
            _funding_steps_text(addr, registered),
            parse_mode="HTML",
            reply_markup=_funding_steps_kb(registered),
        )
        return

    if data == "onb_withdraw_info":
        await query.edit_message_text(
            _onb_withdraw_info_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Продолжить к пополнению", callback_data="onb_fund_steps")],
                [InlineKeyboardButton("🏠 Меню",                    callback_data="menu")],
            ]),
        )
        return

    # ── BP27: explicit wallet-creation onboarding callbacks ──────────────────

    if data == "onb_how_wallet":
        await query.edit_message_text(
            _onb_how_wallet_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Понятно, создать мой кошелёк",
                                      callback_data="onb_create_wallet")],
                [InlineKeyboardButton("↩️ Назад", callback_data="onb_back_create")],
            ]),
        )
        return

    if data == "onb_back_create":
        await query.edit_message_text(
            _onb_create_text(),
            parse_mode="HTML",
            reply_markup=_onb_create_kb(),
        )
        return

    if data == "onb_create_wallet":
        import asyncio

        db_user = get_user_by_telegram_id(tg_user.id) or upsert_user(tg_user.id)

        # Idempotent re-entry / partner demo replay (BP32, /onboarding): the
        # wallet already exists — replay the staged UX with zero writes and no
        # relayer calls, then land on the same card. Repeat runs can never
        # create a second wallet.
        if db_user.get("wallet_address") and db_user.get("wallet_registered"):
            await query.edit_message_text(_ONB_CREATING_STAGE1, parse_mode="HTML")
            await asyncio.sleep(7)
            await query.edit_message_text(_ONB_CREATING_STAGE2, parse_mode="HTML")
            await asyncio.sleep(5)
            await _show_wallet_card(query, db_user)
            return

        # Double-tap guard: registration takes up to a minute; a second tap
        # must not start a parallel run. Lease slightly above the worst case.
        from core.cache import clear_once, notify_once
        if not notify_once(f"onb_create:{tg_user.id}", ttl=180):
            await query.answer("Уже создаём кошелёк — это займёт до минуты…",
                               show_alert=True)
            return

        try:
            # Stage 1 — key pair. Generation is instant; the pause keeps the
            # staged flow readable instead of flashing three edits at once.
            await query.edit_message_text(_ONB_CREATING_STAGE1, parse_mode="HTML")
            if not db_user.get("wallet_address"):
                wallet = generate_wallet()
                update_user(tg_user.id, {
                    "wallet_address":         wallet["address"],
                    "wallet_private_key_enc": wallet["private_key_enc"],
                    # System stays OFF until the explicit «Включить систему» tap.
                    "is_signal_only":         True,
                })
                db_user = get_user_by_telegram_id(tg_user.id) or db_user
            await asyncio.sleep(7)

            # Stage 2 — the real Polymarket registration (deploy + approvals +
            # CLOB creds, gasless, 30–60 s). to_thread keeps the bot loop alive.
            await query.edit_message_text(_ONB_CREATING_STAGE2, parse_mode="HTML")
            await asyncio.to_thread(_register_deposit_wallet, tg_user.id, db_user)
            db_user = get_user_by_telegram_id(tg_user.id) or db_user

            log.info("onb_wallet_created", user=tg_user.id,
                     addr=(db_user.get("wallet_address") or "")[:10])
            await _show_wallet_card(query, db_user)
        except Exception:
            log.exception("onb_create_wallet_failed", user=tg_user.id)
            await query.edit_message_text(
                "❌ <b>Не удалось создать кошелёк.</b>\n\n"
                "Такое бывает при высокой нагрузке на сеть. Попробуйте ещё раз "
                "через минуту — прогресс не потеряется.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔁 Повторить", callback_data="onb_create_wallet")
                ]]),
            )
        finally:
            clear_once(f"onb_create:{tg_user.id}")
        return

    if data == "onb_wallet_card":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user or not db_user.get("wallet_address"):
            await query.answer("Отправь /start", show_alert=True)
            return
        await _show_wallet_card(query, db_user)
        return

    if data == "onb_copy_addr":
        # Fallback for PTB < 21.7 (no native copy_text button): send the address
        # as a monospace message — tapping it in Telegram copies to clipboard.
        db_user = get_user_by_telegram_id(tg_user.id)
        addr = (db_user or {}).get("wallet_address")
        if not addr:
            await query.answer("Сначала создайте кошелёк — /start", show_alert=True)
            return
        await context.bot.send_message(chat_id=tg_user.id,
                                       text=f"<code>{addr}</code>", parse_mode="HTML")
        await query.answer("Адрес отправлен сообщением — нажмите на него, чтобы скопировать")
        return

    if data == "onb_custody":
        await query.edit_message_text(
            _onb_custody_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Пополнить кошелёк", callback_data="onb_fund_steps")],
                [InlineKeyboardButton("↩️ Назад к кошельку", callback_data="onb_wallet_card")],
            ]),
        )
        return

    if data == "system_on":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user or not db_user.get("wallet_registered"):
            await query.answer("Сначала создайте кошелёк — /start", show_alert=True)
            return
        update_user(tg_user.id, {"is_signal_only": False, "copy_active": True})

        pusd = 0.0
        try:
            from core.polygon import get_balances
            dw = db_user.get("deposit_wallet_address")
            if dw:
                pusd = get_balances(dw).get("pusd", 0.0)
        except Exception:
            pass
        hint = (
            "" if pusd >= MIN_USDC_READY else
            "\n\n💳 На торговом балансе пока пусто — пополните кошелёк, "
            "и бот начнёт копировать сделки."
        )
        await query.edit_message_text(
            "🚀 <b>Торговая система включена</b>\n\n"
            "Бот будет копировать сделки китов на ваш кошелёк по вашим настройкам. "
            "Приостановить можно в любой момент: «⚙️ Настройки» → «⏸ Приостановить»."
            f"{hint}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
            ]]),
        )
        return

    # ──────────────────────────────────────────────────────────────────────────

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
        # Reset any stale state from a previous abandoned flow
        for k in ("withdraw_step", "withdraw_to", "withdraw_amount"):
            context.user_data.pop(k, None)
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

        to_addr = context.user_data.get("withdraw_to", "")
        amount  = float(context.user_data.get("withdraw_amount", 0))
        # Clear state before dispatching so a retry can't re-confirm
        for k in ("withdraw_step", "withdraw_to", "withdraw_amount"):
            context.user_data.pop(k, None)

        try:
            from worker.tasks import withdraw_funds
            withdraw_funds.delay(db_user["id"], to_addr, amount)
            await query.edit_message_text(
                "⏳ <b>Выполняю вывод…</b>\n\n"
                "Конвертирую pUSD → USDC и отправляю на указанный адрес.\n"
                "✅ Результат с ссылкой на Polygonscan придёт отдельным сообщением.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
                ]]),
            )
            log.info("withdrawal_queued", user_id=tg_user.id, amount=amount, to=to_addr[:10])
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
        sizing_mode = db_user.get("sizing_mode") or "fixed"
        await query.edit_message_text(
            _settings_text(db_user),
            parse_mode="HTML",
            reply_markup=_settings_kb(copy_active, max_pos, sizing_mode, db_user.get("max_daily_trades"), db_user.get("is_signal_only", False)),
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
            sizing_mode = db_user.get("sizing_mode") or "fixed"
            hint = (
                "Это потолок для Kelly-сайзинга — реальный ордер может быть меньше."
                if sizing_mode == "kelly"
                else "Бот будет входить ровно на эту сумму."
            )
            await query.edit_message_text(
                "✏️ <b>Введи сумму в долларах</b>\n\n"
                "Напиши число в чат, например: <code>75</code>\n\n"
                f"<i>{hint}</i>\n"
                "<i>Диапазон: $5 — $10 000</i>",
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
        sizing_mode = db_user.get("sizing_mode") or "fixed"
        await query.answer(f"✅ Позиция: ${val:.0f} USDC")
        await query.edit_message_text(
            _settings_text(db_user),
            parse_mode="HTML",
            reply_markup=_settings_kb(copy_active, val, sizing_mode, db_user.get("max_daily_trades"), db_user.get("is_signal_only", False)),
        )
        return

    # ── BP13.2: daily trade limit ─────────────────────────────────────────────
    if data.startswith("setdaily_"):
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return

        suffix = data[len("setdaily_"):]

        if suffix == "off":
            update_user(tg_user.id, {"max_daily_trades": None})
            db_user = get_user_by_telegram_id(tg_user.id) or db_user
            copy_active = db_user.get("copy_active", False)
            max_pos = float(db_user.get("max_position_usdc") or 25)
            sizing_mode = db_user.get("sizing_mode") or "fixed"
            await query.answer("♾ Лимит снят — сделок без ограничений")
            await query.edit_message_text(
                _settings_text(db_user),
                parse_mode="HTML",
                reply_markup=_settings_kb(copy_active, max_pos, sizing_mode, None, db_user.get("is_signal_only", False)),
            )
            return

        if suffix == "custom":
            context.user_data["awaiting_daily_limit"] = True
            context.user_data["awaiting_max_pos"] = False  # mutually exclusive
            await query.answer()
            await query.edit_message_text(
                "✏️ <b>Введи лимит сделок в день</b>\n\n"
                "Напиши число в чат, например: <code>3</code>\n\n"
                "<i>0 или пустое — снять ограничение (♾)</i>\n"
                "<i>Диапазон: 1 — 100</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Назад", callback_data="settings")
                ]]),
            )
            return

        try:
            n = int(suffix)
        except ValueError:
            await query.answer("Ошибка", show_alert=True)
            return

        update_user(tg_user.id, {"max_daily_trades": n})
        db_user = get_user_by_telegram_id(tg_user.id) or db_user
        copy_active = db_user.get("copy_active", False)
        max_pos = float(db_user.get("max_position_usdc") or 25)
        sizing_mode = db_user.get("sizing_mode") or "fixed"
        await query.answer(f"✅ Лимит: {n} сделок/день")
        await query.edit_message_text(
            _settings_text(db_user),
            parse_mode="HTML",
            reply_markup=_settings_kb(copy_active, max_pos, sizing_mode, n, db_user.get("is_signal_only", False)),
        )
        return

    # ── Trading mode toggle: copy-trading vs signals-only ─────────────────────
    if data in ("mode_copy", "mode_signal"):
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return
        new_signal_only = data == "mode_signal"
        update_user(tg_user.id, {"is_signal_only": new_signal_only})
        if new_signal_only:
            await query.answer("🔔 Режим «Только сигналы» включён")
            note = (
                "🔔 <b>Режим «Только сигналы» включён</b>\n\n"
                "Бот больше не открывает сделки за тебя — вместо этого присылает "
                "детальный сигнал: событие, исход, цену и метрики кита. "
                "Заходишь на Polymarket и торгуешь вручную.\n\n"
                "ℹ️ Уже открытые позиции продолжают отслеживаться: бот закроет их "
                "по стоп-лоссу / тейк-профиту и заберёт выигрыш при резолве."
            )
        else:
            await query.answer("🤖 Копитрейдинг включён")
            note = (
                "🤖 <b>Копитрейдинг включён</b>\n\n"
                "Бот снова автоматически копирует сделки китов на твой кошелёк.\n\n"
                "Убедись, что копирование не на паузе и на балансе есть <b>USDC</b>."
            )
        await query.edit_message_text(
            note,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
            ]),
        )
        return

    # ── Sizing mode toggle ────────────────────────────────────────────────────
    if data in ("sizing_fixed", "sizing_kelly"):
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Отправь /start", show_alert=True)
            return
        new_mode = "kelly" if data == "sizing_kelly" else "fixed"
        update_user(tg_user.id, {"sizing_mode": new_mode})
        db_user = get_user_by_telegram_id(tg_user.id) or db_user
        copy_active = db_user.get("copy_active", False)
        max_pos = float(db_user.get("max_position_usdc") or 25)
        label = "🤖 Kelly-сайзинг включён" if new_mode == "kelly" else "📊 Фиксированный сайзинг включён"
        await query.answer(f"✅ {label}")
        await query.edit_message_text(
            _settings_text(db_user),
            parse_mode="HTML",
            reply_markup=_settings_kb(copy_active, max_pos, new_mode, db_user.get("max_daily_trades"), db_user.get("is_signal_only", False)),
        )
        return

    if data == "kelly_info":
        await query.edit_message_text(
            "🤖 <b>Что такое Kelly-сайзинг?</b>\n\n"
            "Формула Келли автоматически рассчитывает <b>оптимальный размер ставки</b> "
            "на основе статистики кошелька-кита и текущей цены рынка — "
            "без угадывания вручную.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📐 <b>Как работает</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "• Чем сильнее кит (высокий win-rate) → тем больше ставка\n"
            "• Чем выше цена уже отражает исход (0.8, 0.9…) → тем <i>меньше</i> ставка\n"
            "• Размер никогда не превышает твой лимит и 5% капитала\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <b>Почему рекомендуется</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Фиксированный размер ($25 на каждую сделку) не учитывает качество сигнала. "
            "Kelly адаптируется: на слабых сигналах входит минимумом, "
            "на сильных — жмёт сильнее.\n\n"
            "⚠️ Твой лимит позиции остаётся потолком — Kelly не превысит его.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Назад к настройкам", callback_data="settings")
            ]]),
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

    # ── BP8: manual drawdown unblock ─────────────────────────────────────────
    if data == "unlock_drawdown":
        db_user = get_user_by_telegram_id(tg_user.id)
        if not db_user:
            await query.answer("Пользователь не найден.", show_alert=True)
            return

        uid = db_user["id"]
        # Use copy_paused_until as the source of truth — risk_state may be NULL
        # if migration 013 hasn't been applied yet.
        if not _is_risk_paused(db_user):
            await query.answer("Блокировка уже снята.", show_alert=True)
            return

        import structlog as _sl
        _log = _sl.get_logger(__name__)

        # ── Step 1: critical — lift the pause (always works, pre-dates BP8) ──
        try:
            from core.db import resume_user_copying
            resume_user_copying(uid)
        except Exception:
            _log.exception("unlock_drawdown_resume_failed", user_id=uid)
            await query.answer("Ошибка при снятии блокировки. Попробуй позже.", show_alert=True)
            return

        # ── Step 2: compute current equity for HWM reset ─────────────────────
        new_equity: float = 0.0
        try:
            from core.polygon import get_balances as _gb
            from core.polymarket import get_positions as _gp
            from core.risk import total_equity
            from core.db import get_open_trades_cost
            dw = db_user.get("deposit_wallet_address")
            free_pusd = _gb(dw).get("pusd", 0.0) if dw else 0.0
            positions = _gp(dw) if dw else []
            ledger_cost = get_open_trades_cost(uid)
            new_equity = total_equity(
                free_pusd, positions, ledger_cost, mode=settings.drawdown_equity_mode
            )
        except Exception:
            _log.warning("unlock_drawdown_equity_failed", user_id=uid)
            new_equity = float(db_user.get("equity_hwm") or 0.0)

        # ── Step 3: BP8 audit trail (graceful — columns may not exist yet) ───
        old_hwm = float(db_user.get("equity_hwm") or 0.0)
        try:
            from core.db import reset_risk_baseline
            reset_risk_baseline(uid, new_equity)
        except Exception:
            _log.warning("unlock_drawdown_baseline_failed", user_id=uid)

        try:
            from core.db import record_risk_override
            record_risk_override(uid)
        except Exception:
            _log.warning("unlock_drawdown_override_record_failed", user_id=uid)

        # ── Step 3b: Blueprint 17.B — set self-expiring override flag ────────
        # Persists risk_override_until = next 00:00 UTC so the monitor cannot
        # re-trip either breaker for the rest of the UTC day, even though the
        # daily-loss counter is still > 0.
        try:
            from core.db import set_risk_override_until
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            _next_midnight = (
                _dt.now(_tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                + _td(days=1)
            ).isoformat()
            set_risk_override_until(uid, _next_midnight)
            _log.info("risk_override_until_set", user_id=uid, until=_next_midnight)
        except Exception:
            _log.warning("unlock_drawdown_override_until_failed", user_id=uid)

        try:
            from core.db import set_risk_state
            set_risk_state(uid, "active")
        except Exception:
            _log.warning("unlock_drawdown_state_failed", user_id=uid)

        # ── Step 4: clear Redis notify-once keys ─────────────────────────────
        try:
            from core.cache import _client as _redis_client
            r = _redis_client()
            r.delete(f"once:drawdown_alert:{uid}")
            r.delete(f"once:risk_gate:{uid}:drawdown")
            r.delete(f"once:resume_alert:{uid}")
        except Exception:
            pass

        _log.info(
            "risk_override_manual",
            user_id=uid,
            old_hwm=round(old_hwm, 2),
            new_hwm=round(new_equity, 2),
        )

        equity_line = f"Точка отсчёта просадки сброшена на <b>${new_equity:.2f}</b>.\n" if new_equity > 0 else ""
        await query.edit_message_text(
            "✅ <b>Блокировка снята. Ты берёшь риск на себя.</b>\n\n"
            f"{equity_line}"
            "Копирование возобновлено. Защита по дневному лимиту отключена "
            "<b>до 00:00 UTC</b> — после полуночи риск-менеджер снова включится автоматически.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Главное меню", callback_data="menu")
            ]]),
        )
        return
