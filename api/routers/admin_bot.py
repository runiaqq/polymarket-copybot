"""
Separate admin-only Telegram bot for subscription management.

Super-admin = ADMIN_TELEGRAM_ID (env), always authorized. The super-admin can
invite more admins via one-time deep-link codes. Admins can issue/extend
subscriptions for the MAIN bot and inspect subscribers. Fully isolated from the
main bot (own token + webhook); shares only the database.

Disabled gracefully when TELEGRAM_ADMIN_BOT_TOKEN is not set.
"""

import asyncio
from datetime import datetime, timezone

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from core.config import settings
from core.db import (
    add_admin,
    add_tracked_wallet,
    count_open_positions,
    create_access_code,
    create_admin_code,
    get_pnl_summary,
    get_user_by_telegram_id,
    get_user_by_username,
    get_user_trade_history,
    is_admin,
    is_super_admin,
    list_active_subscribers_detail,
    list_tracked_wallets,
    remove_tracked_wallet,
    list_admins,
    redeem_admin_code,
    remove_admin,
    set_subscription,
)
from core.polygon import get_balances, withdrawable_usdc
from core.polymarket import get_closed_positions
from core.leaderboard import (
    fmt_money,
    profile_url,
    top_profit_wallets,
    wallet_profit,
    wallet_recent_trades,
)

log = structlog.get_logger(__name__)

_main_bot_username: str | None = None


def is_enabled() -> bool:
    return bool(settings.telegram_admin_bot_token)


async def _main_bot_link(code: str) -> str:
    """Deep link into the MAIN bot (for subscription activation codes)."""
    global _main_bot_username
    if _main_bot_username is None:
        from telegram import Bot
        me = await Bot(token=settings.telegram_bot_token).get_me()
        _main_bot_username = me.username
    return f"https://t.me/{_main_bot_username}?start={code}"


def _days_left(expires_at: str | None) -> int | None:
    if not expires_at:
        return None
    try:
        from dateutil.parser import parse as p
        d = p(expires_at)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0, round((d - datetime.now(timezone.utc)).total_seconds() / 86400))
    except Exception:
        return None


def _resolve(ident: str) -> tuple[int | None, dict | None]:
    """Resolve '@username' or numeric id to (telegram_id, user_row|None)."""
    ident = ident.strip()
    if ident.lstrip("@").isdigit() and not ident.startswith("@"):
        tid = int(ident)
        return tid, get_user_by_telegram_id(tid)
    u = get_user_by_username(ident)
    return (u["telegram_id"] if u else None), u


HELP_ADMIN = (
    "🛠 <b>Админ-панель — команды</b>\n\n"
    "<b>Подписки</b>\n"
    "/grant <code>&lt;@ник|id&gt; [дней]</code> — выдать/продлить подписку\n"
    "/sub <code>&lt;@ник|id&gt; [дней]</code> — то же самое (алиас /grant)\n"
    "/newcode <code>[дней]</code> — создать код-ссылку для клиента\n"
    "/subs — список активных подписчиков\n"
    "/user <code>&lt;@ник|id&gt;</code> — детали пользователя\n"
    "\n<b>Белый список китов (копирование)</b>\n"
    "/top — 🔥 топ прибыльных китов за неделю (меню)\n"
    "/wallets — отслеживаемые кошельки (меню)\n"
    "/refresh — обновить белый список лучшими трейдерами\n"
    "/addwallet <code>&lt;адрес&gt; [метка]</code> — добавить вручную\n"
    "/delwallet <code>&lt;адрес&gt;</code> — убрать вручную\n"
)
HELP_SUPER = (
    "\n<b>Управление админами</b> (только главный админ)\n"
    "/addadmin — пригласить нового админа (ссылка с кодом)\n"
    "/admins — список админов\n"
    "/deladmin <code>&lt;@ник|id&gt;</code> — убрать админа\n"
)


async def on_admin_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global PTB error handler for the admin bot — BP9 Layer 2a."""
    data = ""
    user_id = None
    if isinstance(update, Update):
        if update.callback_query:
            data = update.callback_query.data or ""
        if update.effective_user:
            user_id = update.effective_user.id
    log.error(
        "admin_bot_callback_error",
        data=data,
        user_id=user_id,
        exc=str(context.error),
    )
    if isinstance(update, Update) and update.callback_query:
        try:
            await update.callback_query.answer(
                "⚠️ Ошибка — попробуй ещё раз или отправь /help", show_alert=True
            )
        except Exception:
            pass


def build_admin_application() -> Application | None:
    if not is_enabled():
        return None
    app = Application.builder().token(settings.telegram_admin_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("sub", cmd_grant))
    app.add_handler(CommandHandler("newcode", cmd_newcode))
    app.add_handler(CommandHandler("subs", cmd_subs))
    app.add_handler(CommandHandler("user", cmd_user))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("wallets", cmd_wallets))
    app.add_handler(CommandHandler("addwallet", cmd_addwallet))
    app.add_handler(CommandHandler("delwallet", cmd_delwallet))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("deladmin", cmd_deladmin))
    app.add_handler(CallbackQueryHandler(on_callback))
    # BP9 Layer 2a: global error handler — converts silent crashes into logs.
    app.add_error_handler(on_admin_error)
    return app


# ── Guards ────────────────────────────────────────────────────────────────────

async def _deny(update: Update) -> None:
    await update.message.reply_text(  # type: ignore[union-attr]
        "⛔️ Доступ только для админов. Нужен код-приглашение от главного администратора.",
    )


def _help_text(uid: int) -> str:
    return HELP_ADMIN + (HELP_SUPER if is_super_admin(uid) else "")


# ── Handlers ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg:
        return
    # Invite-code activation for new admins.
    if context.args and not is_admin(tg.id):
        if redeem_admin_code(context.args[0].strip(), tg.id, tg.username):
            await update.message.reply_text(  # type: ignore[union-attr]
                "✅ <b>Доступ админа выдан!</b>\n\n" + _help_text(tg.id), parse_mode="HTML"
            )
            return
        await update.message.reply_text(  # type: ignore[union-attr]
            "⚠️ Код недействителен или уже использован.",
        )
        return
    if not is_admin(tg.id):
        await _deny(update)
        return
    await update.message.reply_text(  # type: ignore[union-attr]
        "🛠 <b>Админ-панель копитрейдинг-бота</b>\n\n" + _help_text(tg.id), parse_mode="HTML"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await _deny(update)
        return
    await update.message.reply_text(_help_text(tg.id), parse_mode="HTML")  # type: ignore[union-attr]


async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Grant or extend a subscription.

    Usage: /grant <@ник|id> [дней]   or   /sub <@ник|id> [дней]
    Extends an active subscription or reactivates an expired one.
    """
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await _deny(update)
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Использование: <code>/grant &lt;@ник|id&gt; [дней]</code>\n"
            "Пример: <code>/grant @ivan 30</code> · <code>/sub username 14</code>\n\n"
            "Продлевает активную подписку и реактивирует истёкшую.", parse_mode="HTML"
        )
        return
    try:
        days = int(args[1]) if len(args) > 1 else 30
    except ValueError:
        days = 30
    tid, urow = _resolve(args[0])
    if tid is None:
        raw = args[0]
        display = raw if raw.startswith("@") else f"@{raw}"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"❌ Пользователь <b>{display}</b> не найден в базе.\n\n"
            "Он должен запустить основной бот (/start), чтобы ник сохранился. "
            "Либо укажи числовой Telegram ID.", parse_mode="HTML"
        )
        return
    try:
        user = set_subscription(tid, days)
        exp = (user.get("sub_expires_at") or "")[:10]
        label = f"@{urow.get('username')}" if (urow and urow.get("username")) else f"id{tid}"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"✅ Подписка для <b>{label}</b> продлена на <b>{days}</b> дней.\n"
            f"Новая дата окончания: <b>{exp}</b>",
            parse_mode="HTML",
        )
        await _notify_user(
            tid,
            f"🎉 <b>Администратор выдал вам подписку на {days} дней!</b>\n\n"
            f"Действует до: <b>{exp}</b>\n\n"
            "Бот готов к работе.",
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Ошибка: <code>{str(exc)[:200]}</code>", parse_mode="HTML")  # type: ignore[union-attr]


async def cmd_newcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await _deny(update)
        return
    try:
        days = int(context.args[0]) if context.args else 30
    except ValueError:
        days = 30
    code = create_access_code(days)
    link = await _main_bot_link(code)
    await update.message.reply_text(  # type: ignore[union-attr]
        f"🎟 <b>Код для клиента</b> · <b>{days}</b> дн.\n\n"
        f"Ссылка (одноразовая):\n{link}",
        parse_mode="HTML", disable_web_page_preview=True,
    )


async def cmd_subs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await _deny(update)
        return
    subs = list_active_subscribers_detail()
    if not subs:
        await update.message.reply_text("Активных подписчиков нет.")  # type: ignore[union-attr]
        return
    lines = [f"👥 <b>Активных подписчиков: {len(subs)}</b>\n"]
    for s in subs[:50]:
        nick = f"@{s['username']}" if s.get("username") else "—"
        dl = _days_left(s.get("sub_expires_at"))
        mp = float(s.get("max_position_usdc") or 0)
        copy = "🟢" if s.get("copy_active") else "⏸"
        reg = "✅ кошелёк" if s.get("wallet_registered") else "❌ не зарег."
        lines.append(
            f"{copy} {nick} · <code>{s['telegram_id']}</code>\n"
            f"   ⏳ {dl} дн · макс. ${mp:.0f} · {reg}"
        )
    if len(subs) > 50:
        lines.append(f"\n…и ещё {len(subs) - 50}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")  # type: ignore[union-attr]


def _pnl_fmt(v: float) -> str:
    """Format a PnL value with a sign and a green/red icon."""
    icon = "🟢" if v >= 0 else "🔴"
    sign = "+" if v >= 0 else "−"
    return f"{icon} {sign}${abs(v):.2f}"


def _user_view(tid: int, u: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Build the admin /user dashboard text + inline keyboard.

    Pure (no I/O beyond the balance/PnL/count reads) so both cmd_user and the
    'back' callback can call it. Blueprint 18: reads V2 deposit wallet balance via
    withdrawable_usdc, counts positions from copy_trades (not on-chain EOA).
    """
    eoa = u.get("wallet_address")

    # Balance: withdrawable_usdc covers deposit-wallet pUSD + all EOA USDC variants.
    try:
        avail = withdrawable_usdc(u)
    except Exception:
        avail = 0.0

    # POL (gas) genuinely lives on the EOA — keep as separate on-chain read.
    try:
        pol = get_balances(eoa).get("matic", 0.0) if eoa else 0.0
    except Exception:
        pol = 0.0

    bal_txt = f"${avail:.2f} доступно · ⛽️ POL {pol:.3f}"

    # Open positions: DB count, not on-chain (avoids fake-0 from EOA read).
    db_uid = u.get("id")
    try:
        open_pos = count_open_positions(db_uid) if db_uid else 0
    except Exception:
        open_pos = 0

    # PnL: read from Polymarket Data API (same source as user bot /pnl command) so
    # the admin card and the user's own stats always agree. Falls back to DB on error.
    trading_wallet = u.get("deposit_wallet_address") or eoa
    pnl: dict = {"today": 0.0, "week": 0.0, "all_time": 0.0, "settled": 0}
    if trading_wallet:
        try:
            import time as _t
            closed = get_closed_positions(trading_wallet)
            now_unix   = _t.time()
            day0_unix  = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
            week0_unix = now_unix - 7 * 86400
            p_today = p_week = p_all = 0.0
            for c in closed:
                rpnl = float(c.get("realized_pnl") or 0)
                ts   = int(c.get("timestamp") or 0)
                p_all += rpnl
                if ts >= day0_unix:
                    p_today += rpnl
                if ts >= week0_unix:
                    p_week  += rpnl
            pnl = {"today": p_today, "week": p_week,
                   "all_time": p_all, "settled": len(closed)}
        except Exception:
            # Fallback to DB if Polymarket API is unavailable.
            try:
                pnl = get_pnl_summary(db_uid) if db_uid else pnl
            except Exception:
                pass

    dl = _days_left(u.get("sub_expires_at"))
    nick = f"@{u['username']}" if u.get("username") else "—"

    text = (
        f"👤 <b>Пользователь</b>\n\n"
        f"Ник: {nick}\nID: <code>{tid}</code>\n"
        f"Подписка: <b>{dl if dl is not None else '—'} дн</b> (до {(u.get('sub_expires_at') or '—')[:10]})\n"
        f"Копирование: {'🟢 вкл' if u.get('copy_active') else '⏸ выкл'}\n"
        f"Макс. позиция: <b>${float(u.get('max_position_usdc') or 0):.0f}</b>\n"
        f"Кошелёк зарегистрирован: {'✓' if u.get('wallet_registered') else '✗'}\n\n"
        f"💰 Баланс: {bal_txt}\n"
        f"📊 Открытых позиций: {open_pos}\n\n"
        f"📈 <b>PnL</b>\n"
        f"📅 Сегодня:   {_pnl_fmt(pnl['today'])}\n"
        f"🗓 7 дней:    {_pnl_fmt(pnl['week'])}\n"
        f"🏆 За всё:    {_pnl_fmt(pnl['all_time'])}   ({pnl['settled']} сделок)\n\n"
        f"<code>{eoa or '—'}</code>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Последние 5 сделок", callback_data=f"uh:{tid}:0")],
    ])
    return text, kb


async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Использование: <code>/user &lt;@ник|id&gt;</code>", parse_mode="HTML")
        return
    tid, u = _resolve(context.args[0])
    if not u:
        await update.message.reply_text("Пользователь не найден.")  # type: ignore[union-attr]
        return
    text, kb = _user_view(tid, u)
    await update.message.reply_text(  # type: ignore[union-attr]
        text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


def _fmt_history_row(t: dict) -> str:
    """Render one settled copy_trade as a compact text line for the history view.

    Derives exit price from result/shares — guards against divide-by-zero for legacy
    rows where shares is NULL (Blueprint 16 §16.7).
    """
    sig    = t.get("trade_signals") or {}
    title  = (sig.get("title") or "—")[:32]
    oc     = sig.get("outcome") or ("YES" if t.get("outcome_index") == 0 else "NO")
    entry  = float(t.get("entry_price") or 0)
    shares = float(t.get("shares") or 0)
    # COALESCE: realized_pnl for modern rows, pnl_usdc fallback for pre-008 legacy rows.
    pnl    = float(t.get("realized_pnl") or t.get("pnl_usdc") or 0)
    result = (t.get("result") or "").lower()

    if result == "win":
        exit_px: float | None = 1.0
    elif result in ("lose", "loss"):
        exit_px = 0.0
    elif shares > 0:
        exit_px = entry + pnl / shares
    else:
        exit_px = None  # divide-by-zero guard for legacy rows with shares=NULL

    icon    = "🟢" if pnl >= 0 else "🔴"
    exit_s  = f"{exit_px:.2f}" if exit_px is not None else "—"
    entry_s = f"{entry:.2f}" if entry > 0 else "—"
    return (
        f"{icon} <b>{title}</b> · {oc}\n"
        f"   вход {entry_s} → выход {exit_s} · PnL {pnl:+.2f}$"
    )


def _history_view(tid: int, offset: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build trade history text + pagination keyboard for the admin history view."""
    u = get_user_by_telegram_id(tid)
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К пользователю", callback_data=f"uc:{tid}")]])
    if not u:
        return "Пользователь не найден.", back_kb

    nick   = f"@{u['username']}" if u.get("username") else f"id{tid}"
    trades = get_user_trade_history(u["id"], 5, offset)

    header = f"(с позиции {offset + 1})" if offset > 0 else "(показаны последние записи)"
    lines  = [f"📜 <b>История сделок · {nick}</b>", header, ""]

    if not trades:
        lines.append("Нет завершённых сделок.")
    else:
        for t in trades:
            lines.append(_fmt_history_row(t))

    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"uh:{tid}:{max(0, offset - 5)}"))
    if len(trades) == 5:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"uh:{tid}:{offset + 5}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 К пользователю", callback_data=f"uc:{tid}")])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ── Interactive wallet browser (top whales + tracked list) ────────────────────

_PAGE = 10


def _tracked_set() -> set[str]:
    return {w["address"].lower() for w in list_tracked_wallets()}


def _short_addr(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def _top_view(page: int) -> tuple[str, InlineKeyboardMarkup]:
    wallets = top_profit_wallets("7d", 50)
    if not wallets:
        return ("⚠️ Не удалось загрузить лидерборд. Попробуй позже.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить", callback_data="tp:0")]]))
    tracked = _tracked_set()
    pages = (len(wallets) - 1) // _PAGE + 1
    page = max(0, min(page, pages - 1))
    chunk = wallets[page * _PAGE:(page + 1) * _PAGE]
    rows = []
    for w in chunk:
        mark = "✅ " if w["wallet"].lower() in tracked else ""
        name = (w["name"] or _short_addr(w["wallet"]))[:22]
        rows.append([InlineKeyboardButton(
            f"{mark}{name} · +${fmt_money(w['pnl'])}",
            callback_data=f"wd:t:{w['wallet']}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"tp:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"tp:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("📋 Мои кошельки", callback_data="mp:0")])
    text = ("🔥 <b>Топ китов по прибыли (7 дней)</b>\n"
            "✅ — уже в белом списке. Нажми на кошелёк для деталей.")
    return text, InlineKeyboardMarkup(rows)


def _mine_view(page: int) -> tuple[str, InlineKeyboardMarkup]:
    wallets = list_tracked_wallets()
    if not wallets:
        return ("📋 Белый список пуст.\nОткрой /top и добавь китов, "
                "или вручную: <code>/addwallet &lt;адрес&gt;</code>",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔥 Топ китов", callback_data="tp:0")]]))
    pages = (len(wallets) - 1) // _PAGE + 1
    page = max(0, min(page, pages - 1))
    chunk = wallets[page * _PAGE:(page + 1) * _PAGE]
    rows = []
    for w in chunk:
        label = (w.get("label") or _short_addr(w["address"]))[:24]
        rows.append([InlineKeyboardButton(
            f"🐳 {label}", callback_data=f"wd:m:{w['address']}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"mp:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"mp:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🔥 Топ китов", callback_data="tp:0")])
    text = f"📋 <b>Отслеживаемые кошельки: {len(wallets)}</b>\nНажми для деталей."
    return text, InlineKeyboardMarkup(rows)


def _detail_view(addr: str, origin: str) -> tuple[str, InlineKeyboardMarkup]:
    addr = addr.lower()
    tracked = addr in _tracked_set()

    def _pnl_str(v: float | None) -> str:
        if v is None:
            return "— <i>(вне топ-50)</i>"
        sign = "+" if v >= 0 else "−"
        return f"{sign}${fmt_money(abs(v))}"

    p7s = _pnl_str(wallet_profit(addr, "7d"))
    p30s = _pnl_str(wallet_profit(addr, "30d"))
    text = (
        f"🐳 <b>Кит</b>\n<code>{addr}</code>\n\n"
        f"💰 Прибыль 7д: <b>{p7s}</b>\n"
        f"💰 Прибыль 30д: <b>{p30s}</b>\n"
        f"📥 В белом списке: {'✅ да' if tracked else '❌ нет'}"
    )
    rows = []
    if tracked:
        rows.append([InlineKeyboardButton("🗑 Убрать из списка", callback_data=f"dw:{origin}:{addr}")])
    else:
        rows.append([InlineKeyboardButton("➕ Добавить в список", callback_data=f"aw:{origin}:{addr}")])
    rows.append([
        InlineKeyboardButton("📊 Последние сделки", callback_data=f"wt:{origin}:{addr}"),
        InlineKeyboardButton("🔗 Профиль", url=profile_url(addr)),
    ])
    back = "tp:0" if origin == "t" else "mp:0"
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data=back)])
    return text, InlineKeyboardMarkup(rows)


def _trades_view(addr: str, origin: str) -> tuple[str, InlineKeyboardMarkup]:
    addr = addr.lower()
    trades = wallet_recent_trades(addr, 8)
    lines = [f"📊 <b>Последние сделки</b>\n<code>{_short_addr(addr)}</code>\n"]
    if not trades:
        lines.append("Нет недавних сделок (или API недоступен).")
    for t in trades:
        side = "🟢 BUY" if str(t.get("side", "")).upper() == "BUY" else "🔴 SELL"
        title = (t.get("title") or "—")[:48]
        outcome = t.get("outcome") or ""
        oc = f" · {outcome}" if outcome else ""
        price = float(t.get("price") or 0)
        size = float(t.get("size_usdc") or 0)
        lines.append(f"{side}{oc} · ${size:.0f} @ {price:.2f}\n   <i>{title}</i>")
    rows = [[InlineKeyboardButton("🔙 К киту", callback_data=f"wd:{origin}:{addr}")]]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await _deny(update)
        return
    text, kb = _top_view(0)
    await update.message.reply_text(  # type: ignore[union-attr]
        text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


async def cmd_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await _deny(update)
        return
    text, kb = _mine_view(0)
    await update.message.reply_text(  # type: ignore[union-attr]
        text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await _deny(update)
        return
    try:
        target = int(context.args[0]) if context.args else 20
    except ValueError:
        target = 20

    msg = await update.message.reply_text(  # type: ignore[union-attr]
        "⏳ Сканирую лидерборд и отбираю лучших направленных трейдеров…\n"
        "Это займёт 1–2 минуты.")
    try:
        from core.wallet_discovery import discover_quality
        r = await asyncio.to_thread(discover_quality, target, True)
    except Exception as exc:
        log.exception("refresh_failed")
        await msg.edit_text(f"❌ Ошибка обновления: <code>{str(exc)[:200]}</code>", parse_mode="HTML")
        return

    lines = [
        "✅ <b>Белый список обновлён</b>\n",
        f"🔎 Проверено кандидатов: <b>{r['scanned']}</b>",
        f"🎯 Прошли фильтр качества: <b>{r['qualified']}</b>",
        f"➕ Добавлено новых: <b>{len(r['added'])}</b>",
        f"✓ Уже были в списке: <b>{len(r['kept'])}</b>",
        f"🧹 Убрано маркет-мейкеров: <b>{len(r.get('removed', []))}</b>",
        f"📋 Всего в списке: <b>{r['total']}</b>",
    ]
    def _wallet_score_line(p: dict, icon: str) -> str:
        name = f" · {p['name']}" if p.get("name") else ""
        ratio = p.get("ratio")
        ratio_txt = f" · edge {ratio * 100:.0f}%" if ratio else ""
        d = p.get("directionality")
        dir_txt = f" · dir {d:.2f}" if d is not None else ""
        return (
            f"{icon} <code>{_short_addr(p['wallet'])}</code>{name}\n"
            f"   +${p['realized']:,.0f}{ratio_txt}{dir_txt}"
        )

    if r.get("removed"):
        reason_lbl = {
            "low_ratio": "ММ/арб",
            "mm": "ММ/ликвидность",
            "scattershot": "разбрасыватель",
        }
        lines.append("\n<b>Убраны:</b>")
        for p in r["removed"][:25]:
            name = f" · {p['name']}" if p.get("name") else ""
            why = reason_lbl.get(p.get("reason", ""), "")
            why_txt = f" — {why}" if why else ""
            lines.append(f"🧹 <code>{_short_addr(p['wallet'])}</code>{name}{why_txt}")
    if r["added"]:
        lines.append("\n<b>Новые кошельки:</b>")
        for p in r["added"][:25]:
            lines.append(_wallet_score_line(p, "🐳"))
    else:
        lines.append("\nНовых кошельков не нашлось — список уже актуален.")
    if r.get("kept"):
        lines.append("\n<b>Уже в списке (прошли фильтр):</b>")
        for p in r["kept"][:15]:
            lines.append(_wallet_score_line(p, "✅"))
    lines.append("\nОткрой /wallets для управления.")
    await msg.edit_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await q.answer("⛔️ Только для админов", show_alert=True)
        return
    data = q.data or ""
    toast = ""
    try:
        if data == "noop":
            await q.answer()
            return
        elif data.startswith("tp:"):
            text, kb = _top_view(int(data[3:]))
        elif data.startswith("mp:"):
            text, kb = _mine_view(int(data[3:]))
        elif data.startswith("wd:"):
            _, origin, addr = data.split(":", 2)
            text, kb = _detail_view(addr, origin)
        elif data.startswith("wt:"):
            _, origin, addr = data.split(":", 2)
            text, kb = _trades_view(addr, origin)
        elif data.startswith("aw:"):
            _, origin, addr = data.split(":", 2)
            add_tracked_wallet(addr)
            toast = "✅ Добавлен"
            text, kb = _detail_view(addr, origin)
        elif data.startswith("dw:"):
            _, origin, addr = data.split(":", 2)
            remove_tracked_wallet(addr)
            toast = "🗑 Убран"
            text, kb = _detail_view(addr, origin)
        elif data.startswith("uh:"):
            _, tid_s, off_s = data.split(":", 2)
            text, kb = _history_view(int(tid_s), int(off_s))
        elif data.startswith("uc:"):
            back_tid = int(data[3:])
            back_u = get_user_by_telegram_id(back_tid)
            if back_u:
                text, kb = _user_view(back_tid, back_u)
            else:
                text, kb = "Пользователь не найден.", InlineKeyboardMarkup([])
        else:
            await q.answer()
            return

        await q.answer(toast)
        await q.edit_message_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except Exception as exc:
        # "not modified" no-op edits happen after answer() was already sent — ignore.
        if "not modified" in str(exc).lower():
            return
        log.warning("admin_callback_failed", data=data, error=str(exc))
        try:
            await q.answer("⚠️ Ошибка, попробуй снова")
        except Exception:
            pass


async def cmd_addwallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Использование: <code>/addwallet &lt;0x-адрес&gt; [метка]</code>", parse_mode="HTML")
        return
    addr = context.args[0].strip()
    if not (addr.startswith("0x") and len(addr) == 42):
        await update.message.reply_text("❌ Неверный адрес (нужен 0x… длиной 42 символа).")  # type: ignore[union-attr]
        return
    label = " ".join(context.args[1:]) or None
    add_tracked_wallet(addr, label)
    await update.message.reply_text(  # type: ignore[union-attr]
        f"✅ Кошелёк добавлен в белый список:\n<code>{addr.lower()}</code>", parse_mode="HTML")


async def cmd_delwallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Использование: <code>/delwallet &lt;0x-адрес&gt;</code>", parse_mode="HTML")
        return
    addr = context.args[0].strip()
    remove_tracked_wallet(addr)
    await update.message.reply_text(  # type: ignore[union-attr]
        f"🗑 Кошелёк убран из белого списка:\n<code>{addr.lower()}</code>", parse_mode="HTML")


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_super_admin(tg.id):
        await _deny(update)
        return
    code = create_admin_code()
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={code}"
    await update.message.reply_text(  # type: ignore[union-attr]
        f"🔑 <b>Приглашение админа</b> (одноразовое)\n\n"
        f"Отправь эту ссылку человеку — он станет админом:\n{link}",
        parse_mode="HTML", disable_web_page_preview=True,
    )


async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_super_admin(tg.id):
        await _deny(update)
        return
    admins = list_admins()
    lines = [f"👮 <b>Главный админ:</b> <code>{settings.admin_telegram_id}</code>\n"]
    if admins:
        lines.append("<b>Админы:</b>")
        for a in admins:
            nick = f"@{a['username']}" if a.get("username") else "—"
            lines.append(f"• {nick} · <code>{a['telegram_id']}</code>")
    else:
        lines.append("Других админов нет.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")  # type: ignore[union-attr]


async def cmd_deladmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_super_admin(tg.id):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text("Использование: <code>/deladmin &lt;@ник|id&gt;</code>", parse_mode="HTML")  # type: ignore[union-attr]
        return
    tid, _ = _resolve(context.args[0])
    if tid is None:
        await update.message.reply_text("Пользователь не найден.")  # type: ignore[union-attr]
        return
    if tid == settings.admin_telegram_id:
        await update.message.reply_text("Главного админа удалить нельзя.")  # type: ignore[union-attr]
        return
    remove_admin(tid)
    await update.message.reply_text(f"✅ Админ <code>{tid}</code> удалён.", parse_mode="HTML")  # type: ignore[union-attr]


async def _notify_user(telegram_id: int, text: str) -> None:
    """Notify a MAIN-bot user from the admin bot (uses the main bot token)."""
    from telegram import Bot
    try:
        await Bot(token=settings.telegram_bot_token).send_message(
            chat_id=telegram_id, text=text, parse_mode="HTML"
        )
    except Exception:
        log.info("admin_notify_user_skipped", telegram_id=telegram_id)
