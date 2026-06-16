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
    create_access_code,
    create_admin_code,
    get_user_by_telegram_id,
    get_user_by_username,
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
from core.leaderboard import (
    fmt_money,
    polyloly_stats,
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


def build_admin_application() -> Application | None:
    if not is_enabled():
        return None
    app = Application.builder().token(settings.telegram_admin_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("grant", cmd_grant))
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
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await _deny(update)
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Использование: <code>/grant &lt;@ник|id&gt; [дней]</code>\n"
            "Продлевает активную подписку и реактивирует истёкшую.", parse_mode="HTML"
        )
        return
    try:
        days = int(args[1]) if len(args) > 1 else 30
    except ValueError:
        days = 30
    tid, urow = _resolve(args[0])
    if tid is None:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"❌ Пользователь {args[0]} не найден. Он должен запустить основной бот (/start), "
            "либо укажи числовой Telegram ID.", parse_mode="HTML"
        )
        return
    try:
        user = set_subscription(tid, days)
        exp = (user.get("sub_expires_at") or "")[:10]
        label = f"@{urow.get('username')}" if (urow and urow.get("username")) else str(tid)
        await update.message.reply_text(  # type: ignore[union-attr]
            f"✅ Подписка продлена на <b>{days}</b> дн.\nПользователь: {label}\nДо: <b>{exp}</b>",
            parse_mode="HTML",
        )
        await _notify_user(tid, f"✅ <b>Подписка продлена!</b>\nДействует до: <b>{exp}</b>")
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


async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not is_admin(tg.id):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text("Использование: <code>/user &lt;@ник|id&gt;</code>", parse_mode="HTML")  # type: ignore[union-attr]
        return
    tid, u = _resolve(context.args[0])
    if not u:
        await update.message.reply_text("Пользователь не найден.")  # type: ignore[union-attr]
        return

    addr = u.get("wallet_address")
    bal_txt, pos_txt = "—", "—"
    if addr:
        try:
            from core.polymarket import get_positions
            from core.polygon import get_balances
            b = get_balances(addr)
            bal_txt = f"pUSD ${b.get('pusd', 0):.2f} · USDC.e ${b.get('usdc_e', 0):.2f} · POL {b.get('matic', 0):.3f}"
            pos_txt = str(sum(1 for p in get_positions(addr) if p["shares"] > 0))
        except Exception:
            pass

    dl = _days_left(u.get("sub_expires_at"))
    nick = f"@{u['username']}" if u.get("username") else "—"
    await update.message.reply_text(  # type: ignore[union-attr]
        f"👤 <b>Пользователь</b>\n\n"
        f"Ник: {nick}\nID: <code>{tid}</code>\n"
        f"Подписка: <b>{dl if dl is not None else '—'} дн</b> (до {(u.get('sub_expires_at') or '—')[:10]})\n"
        f"Копирование: {'🟢 вкл' if u.get('copy_active') else '⏸ выкл'}\n"
        f"Макс. позиция: <b>${float(u.get('max_position_usdc') or 0):.0f}</b>\n"
        f"Кошелёк зарегистрирован: {'✓' if u.get('wallet_registered') else '✗'}\n"
        f"Баланс: {bal_txt}\n"
        f"Открытых позиций: {pos_txt}\n"
        f"<code>{addr or '—'}</code>",
        parse_mode="HTML", disable_web_page_preview=True,
    )


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

    # Polyloly: free public API, no auth needed.
    pl = polyloly_stats(addr)
    if pl:
        wr = pl.get("winRate")
        closed = pl.get("closedPositions")
        insider = pl.get("knownInsider")
        category = pl.get("topCategory") or ""
        wr_str = f"{wr * 100:.0f}%" if wr is not None else "—"
        insider_str = "🔴 INSIDER" if insider else ("✅ чистый" if wr is not None else "—")
        closed_str = str(closed) if closed is not None else "—"
        cat_str = f" · {category}" if category else ""
        polyloly_block = (
            f"\n\n📈 <b>Polyloly Analytics</b>\n"
            f"🎯 Insider: <b>{insider_str}</b>\n"
            f"📊 Win rate: <b>{wr_str}</b> ({closed_str} закрытых){cat_str}"
        )
    else:
        polyloly_block = "\n\n📈 <i>Polyloly: нет данных</i>"

    text = (
        f"🐳 <b>Кит</b>\n<code>{addr}</code>\n\n"
        f"💰 Прибыль 7д: <b>{p7s}</b>\n"
        f"💰 Прибыль 30д: <b>{p30s}</b>\n"
        f"📥 В белом списке: {'✅ да' if tracked else '❌ нет'}"
        f"{polyloly_block}"
    )
    rows = []
    if tracked:
        rows.append([InlineKeyboardButton("🗑 Убрать из списка", callback_data=f"dw:{origin}:{addr}")])
    else:
        rows.append([InlineKeyboardButton("➕ Добавить в список", callback_data=f"aw:{origin}:{addr}")])
    rows.append([
        InlineKeyboardButton("📊 Последние сделки", callback_data=f"wt:{origin}:{addr}"),
        InlineKeyboardButton("🔗 Polymarket", url=profile_url(addr)),
    ])
    rows.append([InlineKeyboardButton("📈 Polyloly", url=f"https://polyloly.com")])
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
    if r.get("removed"):
        lines.append("\n<b>Убраны (маркет-мейкеры / LP):</b>")
        for p in r["removed"][:25]:
            name = f" · {p['name']}" if p.get("name") else ""
            lines.append(f"🧹 <code>{_short_addr(p['wallet'])}</code>{name}")
    if r["added"]:
        lines.append("\n<b>Новые кошельки:</b>")
        for p in r["added"][:25]:
            name = f" · {p['name']}" if p.get("name") else ""
            ratio = p.get("ratio")
            ratio_txt = f" · edge {ratio * 100:.0f}%" if ratio else ""
            lines.append(
                f"🐳 <code>{_short_addr(p['wallet'])}</code>{name}\n"
                f"   +${p['realized']:,.0f}{ratio_txt}"
            )
    else:
        lines.append("\nНовых кошельков не нашлось — список уже актуален.")
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
