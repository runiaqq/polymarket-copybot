"""
Separate admin-only Telegram bot for subscription management.

Super-admin = ADMIN_TELEGRAM_ID (env), always authorized. The super-admin can
invite more admins via one-time deep-link codes. Admins can issue/extend
subscriptions for the MAIN bot and inspect subscribers. Fully isolated from the
main bot (own token + webhook); shares only the database.

Disabled gracefully when TELEGRAM_ADMIN_BOT_TOKEN is not set.
"""

from datetime import datetime, timezone

import structlog
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from core.config import settings
from core.db import (
    add_admin,
    create_access_code,
    create_admin_code,
    get_user_by_telegram_id,
    get_user_by_username,
    is_admin,
    is_super_admin,
    list_active_subscribers_detail,
    list_admins,
    redeem_admin_code,
    remove_admin,
    set_subscription,
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
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("deladmin", cmd_deladmin))
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
