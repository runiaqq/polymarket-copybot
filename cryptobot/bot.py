"""BP33 Telegram bot: minimal product UI for the BTC-bot pilot.

Onboarding mirrors BP27 (explicit wallet creation, staged messages, nothing
happens until the user switches trading on), scoped to crypto_users.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from core.config import settings
from cryptobot import db

log = structlog.get_logger(__name__)

# telegram_ids with a wallet-creation flow currently in flight (double-tap guard).
_creating: set[int] = set()


# ── texts ─────────────────────────────────────────────────────────────────────

WELCOME_TEXT = (
    "⚡️ <b>Добро пожаловать в PolyMind BTC!</b>\n\n"
    "Это бот алгоритмической торговли на рынках Polymarket "
    "<b>«Bitcoin Up/Down»</b> (5-минутные окна). Алгоритм в реальном времени "
    "оценивает вероятность движения BTC по живой волатильности и входит только "
    "тогда, когда рыночная цена заметно ниже модельной вероятности.\n\n"
    "👛 <b>Сначала создадим ваш торговый кошелёк Polymarket</b>\n\n"
    "Бот создаст для вас отдельный кошелёк в сети Polygon через "
    "API-инфраструктуру Polymarket — прямо внутри Telegram, без регистрации "
    "на сайте. На этом адресе будут учитываться баланс, позиции и история "
    "сделок.\n\n"
    "Пока вы не включите торговлю, бот не совершает сделок."
)

HOW_WALLET_TEXT = (
    "❓ <b>Как это устроено</b>\n\n"
    "Telegram-бот здесь работает как удобный интерфейс. Сам торговый кошелёк "
    "создаётся не во внутреннем балансе PolyMind, а через инфраструктуру "
    "Polymarket.\n\n"
    "Когда вы нажимаете кнопку создания, бот разворачивает отдельный кошелёк "
    "для вашего профиля. Он получает уникальный адрес в сети Polygon — все "
    "операции по нему видны на Polygonscan.\n\n"
    "Само создание кошелька бесплатно, ничего не списывает и не запускает "
    "торговлю."
)

CREATING_STAGE1 = (
    "⏳ <b>Создаём ваш Polymarket-кошелёк</b>\n\n"
    "Получаем отдельный адрес для вашего профиля…"
)

CREATING_STAGE2 = (
    "⏳ <b>Создаём ваш Polymarket-кошелёк</b>\n\n"
    "Адрес получен. Регистрируем кошелёк\nв инфраструктуре Polymarket…"
)

HOW_STRATEGY_TEXT = (
    "🧠 <b>Как торгует бот</b>\n\n"
    "На Polymarket каждые 5 минут открывается рынок «Bitcoin Up or Down»: "
    "закроется ли BTC выше или ниже цены открытия окна.\n\n"
    "📐 <b>Модель.</b> Бот получает поток цены BTC и считает вероятность "
    "исхода по текущему отклонению от открытия и живой волатильности.\n\n"
    "🎯 <b>Вход.</b> Сделка открывается только когда модельная вероятность "
    "заметно выше рыночной цены (порог edge) и цена BTC уже ощутимо отошла "
    "от страйка. Таких сигналов немного — обычно единицы за час.\n\n"
    "⏱ <b>Выход.</b> Позиция держится до закрытия окна (до 2 минут). Победа — "
    "токены гасятся в $1, проигрыш — ставка сгорает. Средние потери на "
    "сделку ограничены размером ставки.\n\n"
    "🛡 <b>Защита.</b> Дневной лимит потерь останавливает торговлю до конца "
    "дня. Отключить торговлю можно в один клик в меню."
)

CUSTODY_TEXT = (
    "🔐 <b>Кто управляет кошельком</b>\n\n"
    "Кошелёк создан для вашего профиля, и средства на нём — ваши.\n\n"
    "🤖 <b>Сделки.</b> Бот подписывает сделки ключами кошелька, которые "
    "хранит в зашифрованном виде, — и только пока торговля включена вами. "
    "Выключите её — и бот не совершит ни одной операции.\n\n"
    "🔎 <b>Прозрачность.</b> Кошелёк живёт в блокчейне Polygon — все операции "
    "видны по его адресу на Polygonscan."
)

WITHDRAW_TEXT = (
    "💸 <b>Вывод средств</b>\n\n"
    "В рамках пилота вывод выполняется вручную командой PolyMind: напишите "
    "нам, укажите сумму и ваш адрес в сети Polygon — средства отправим в "
    "течение дня и пришлём ссылку на транзакцию.\n\n"
    "Автоматический вывод кнопкой появится в следующей версии."
)

CLOSED_PILOT_TEXT = (
    "🔒 <b>PolyMind BTC сейчас в закрытом пилоте</b>\n\n"
    "Доступ выдаётся вручную. Если вам нужен доступ — свяжитесь с командой "
    "PolyMind."
)


def _menu_text(user: dict, pusd: float, stats: dict) -> str:
    dw = user.get("deposit_wallet_address") or "—"
    trading = "включена ✅" if user.get("trading_on") else "выключена ⏸"
    stake = float(user.get("stake_usdc") or settings.crypto_default_stake_usdc)
    today = stats["today"]
    today_line = (
        f"Сегодня: {today['trades']} сделок, PnL <b>${today['pnl']:+.2f}</b>"
        if today["trades"]
        else "Сегодня сделок ещё не было"
    )
    return (
        "⚡️ <b>PolyMind BTC</b>\n\n"
        f"📬 Торговый адрес:\n<code>{dw}</code>\n\n"
        f"💵 Торговый баланс: <b>${pusd:.2f} pUSD</b>\n"
        f"🤖 Торговля: <b>{trading}</b>\n"
        f"🎯 Ставка на сделку: <b>${stake:.2f}</b>\n"
        f"📊 {today_line}"
    )


def _menu_kb(user: dict) -> InlineKeyboardMarkup:
    toggle = (
        InlineKeyboardButton("⏸ Остановить торговлю", callback_data="crb_toggle")
        if user.get("trading_on")
        else InlineKeyboardButton("▶️ Включить торговлю", callback_data="crb_toggle")
    )
    return InlineKeyboardMarkup(
        [
            [toggle],
            [
                InlineKeyboardButton("💳 Пополнить", callback_data="crb_fund"),
                InlineKeyboardButton("🔄 Обновить", callback_data="crb_menu"),
            ],
            [
                InlineKeyboardButton("🎯 Ставка", callback_data="crb_stake"),
                InlineKeyboardButton("📊 Статистика", callback_data="crb_stats"),
            ],
            [
                InlineKeyboardButton("❓ Как торгует бот", callback_data="crb_how"),
                InlineKeyboardButton("💸 Вывод", callback_data="crb_withdraw"),
            ],
        ]
    )


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ В меню", callback_data="crb_menu")]]
    )


def _welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Создать кошелёк", callback_data="crb_create")],
            [InlineKeyboardButton("❓ Как это устроено", callback_data="crb_how_wallet")],
        ]
    )


def _allowed(telegram_id: int) -> bool:
    whitelist = settings.crypto_whitelist_telegram_ids
    return not whitelist or telegram_id in whitelist


# ── screens ───────────────────────────────────────────────────────────────────

async def _send_menu(update: Update, user: dict, *, edit: bool = False) -> None:
    from core.polygon import get_balances

    pusd = 0.0
    dw = user.get("deposit_wallet_address")
    if dw:
        try:
            pusd = (await asyncio.to_thread(get_balances, dw)).get("pusd", 0.0)
        except Exception:
            log.warning("crypto_menu_balance_failed", user_id=user["id"])
    stats = await asyncio.to_thread(db.user_stats, user["id"])
    text = _menu_text(user, pusd, stats)
    kb = _menu_kb(user)
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode="HTML", reply_markup=kb
            )
            return
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
            # Any other edit failure (message too old, deleted) — send fresh.
    await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=kb)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if tg_user is None:
        return
    if not _allowed(tg_user.id):
        await update.effective_chat.send_message(CLOSED_PILOT_TEXT, parse_mode="HTML")
        return
    user = await asyncio.to_thread(db.upsert_user, tg_user.id, tg_user.username)
    if user.get("wallet_registered"):
        await _send_menu(update, user)
        return
    await update.effective_chat.send_message(
        WELCOME_TEXT, parse_mode="HTML", reply_markup=_welcome_kb()
    )


async def cb_how_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        HOW_WALLET_TEXT, parse_mode="HTML", reply_markup=_welcome_kb()
    )


async def cb_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.clob import register_deposit_wallet
    from core.wallet import generate_wallet

    query = update.callback_query
    tg_user = update.effective_user
    await query.answer()
    if not _allowed(tg_user.id):
        return
    user = await asyncio.to_thread(db.upsert_user, tg_user.id, tg_user.username)

    if user.get("wallet_registered"):
        await _send_menu(update, user, edit=True)
        return
    if tg_user.id in _creating:
        return
    _creating.add(tg_user.id)
    try:
        await query.edit_message_text(CREATING_STAGE1, parse_mode="HTML")

        if not user.get("wallet_private_key_enc"):
            wallet = generate_wallet()
            fields = {
                "wallet_address": wallet["address"],
                "wallet_private_key_enc": wallet["private_key_enc"],
                "wallet_created_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
            }
            await asyncio.to_thread(db.update_user, user["id"], fields)
            user.update(fields)
        await asyncio.sleep(5)

        await query.edit_message_text(CREATING_STAGE2, parse_mode="HTML")
        registration = await asyncio.to_thread(
            register_deposit_wallet, user["wallet_private_key_enc"]
        )
        creds = registration["creds"]
        fields = {
            "deposit_wallet_address": registration["deposit_wallet"],
            "clob_api_key": creds["clob_api_key"],
            "clob_secret": creds["clob_secret"],
            "clob_passphrase": creds["clob_passphrase"],
            "wallet_registered": True,
        }
        await asyncio.to_thread(db.update_user, user["id"], fields)
        user.update(fields)
    except Exception as exc:
        log.exception("crypto_wallet_creation_failed", telegram_id=tg_user.id)
        await query.edit_message_text(
            "❌ <b>Не удалось создать кошелёк</b>\n\n"
            f"<code>{str(exc)[:200]}</code>\n\n"
            "Нажмите кнопку ещё раз — процесс продолжится с того же места.",
            parse_mode="HTML",
            reply_markup=_welcome_kb(),
        )
        return
    finally:
        _creating.discard(tg_user.id)

    await query.edit_message_text(
        "✅ <b>Ваш Polymarket-кошелёк готов</b>\n\n"
        "Кошелёк развёрнут и зарегистрирован в инфраструктуре Polymarket. "
        "Сейчас он пустой, торговля выключена — бот ничего не делает без "
        "вашей команды.",
        parse_mode="HTML",
    )
    await _send_menu(update, user)


async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = await asyncio.to_thread(db.get_user, update.effective_user.id)
    if not user:
        return
    await _send_menu(update, user, edit=True)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await asyncio.to_thread(db.get_user, update.effective_user.id)
    if not user or not user.get("wallet_registered"):
        await cmd_start(update, context)
        return
    await _send_menu(update, user)


async def cb_fund(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = await asyncio.to_thread(db.get_user, update.effective_user.id)
    if not user or not user.get("wallet_address"):
        return
    addr = user["wallet_address"]
    text = (
        "💳 <b>Пополнение — 3 шага</b>\n\n"
        "📬 <b>Ваш адрес для пополнения (сеть Polygon):</b>\n"
        f"<code>{addr}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ Отправьте <b>USDC</b> на адрес выше — <b>строго в сети Polygon</b>\n"
        "2️⃣ Отправьте туда же <b>~0.1 POL</b> (газ для конвертации)\n"
        "3️⃣ Бот сам переведёт средства на торговый баланс (pUSD) и напишет вам\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ <b>Только сеть Polygon</b> — не Ethereum, не BSC, не Arbitrum!\n"
        "ℹ️ Рекомендуемый стартовый баланс: <b>$100–200</b>.\n"
        "ℹ️ Пока торговля выключена, бот не совершает сделок — даже с балансом."
    )
    rows = []
    try:
        from telegram import CopyTextButton

        rows.append(
            [InlineKeyboardButton("📋 Скопировать адрес", copy_text=CopyTextButton(addr))]
        )
    except ImportError:
        pass
    rows.append(
        [
            InlineKeyboardButton(
                "🔎 Polygonscan", url=f"https://polygonscan.com/address/{addr}"
            )
        ]
    )
    rows.append([InlineKeyboardButton("🔐 Кто управляет кошельком", callback_data="crb_custody")])
    rows.append([InlineKeyboardButton("⬅️ В меню", callback_data="crb_menu")])
    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
    )


async def cb_custody(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(CUSTODY_TEXT, parse_mode="HTML", reply_markup=_back_kb())


async def cb_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.polygon import get_balances

    query = update.callback_query
    user = await asyncio.to_thread(db.get_user, update.effective_user.id)
    if not user or not user.get("wallet_registered"):
        await query.answer("Сначала создайте кошелёк: /start", show_alert=True)
        return
    turning_on = not user.get("trading_on")
    if turning_on:
        try:
            pusd = (
                await asyncio.to_thread(get_balances, user["deposit_wallet_address"])
            ).get("pusd", 0.0)
        except Exception:
            pusd = 0.0
        if pusd < settings.exchange_min_order_usdc:
            await query.answer(
                f"Торговый баланс ${pusd:.2f} — меньше минимальной ставки "
                f"${settings.exchange_min_order_usdc:.0f}. Сначала пополните кошелёк.",
                show_alert=True,
            )
            return
    await query.answer("Торговля включена ✅" if turning_on else "Торговля остановлена ⏸")
    await asyncio.to_thread(db.update_user, user["id"], {"trading_on": turning_on})
    user["trading_on"] = turning_on
    await _send_menu(update, user, edit=True)


async def cb_stake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = await asyncio.to_thread(db.get_user, update.effective_user.id)
    if not user:
        return
    current = float(user.get("stake_usdc") or settings.crypto_default_stake_usdc)
    buttons = [
        InlineKeyboardButton(
            f"{'✅ ' if abs(preset - current) < 0.01 else ''}${preset:.0f}",
            callback_data=f"crb_stakeset:{preset:g}",
        )
        for preset in settings.crypto_stake_presets
    ]
    await query.edit_message_text(
        "🎯 <b>Ставка на сделку</b>\n\n"
        f"Текущая ставка: <b>${current:.2f}</b>\n\n"
        "Столько бот вкладывает в каждый сигнал. Если свободного баланса "
        "меньше, ставка автоматически уменьшается; ниже $5 (минимум биржи) "
        "сделка пропускается.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([buttons, _back_kb().inline_keyboard[0]]),
    )


async def cb_stake_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        value = float(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer()
        return
    if value not in settings.crypto_stake_presets:
        await query.answer()
        return
    user = await asyncio.to_thread(db.get_user, update.effective_user.id)
    if not user:
        return
    await asyncio.to_thread(db.update_user, user["id"], {"stake_usdc": value})
    user["stake_usdc"] = value
    await query.answer(f"Ставка: ${value:.0f} ✅")
    await _send_menu(update, user, edit=True)


async def cb_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = await asyncio.to_thread(db.get_user, update.effective_user.id)
    if not user:
        return
    stats = await asyncio.to_thread(db.user_stats, user["id"])

    def _block(title: str, bucket: dict) -> str:
        if not bucket["trades"]:
            return f"<b>{title}</b>\nСделок не было"
        winrate = bucket["wins"] / bucket["settled"] if bucket["settled"] else 0.0
        return (
            f"<b>{title}</b>\n"
            f"Сделок: {bucket['trades']} (открыто: {bucket['open']})\n"
            f"Winrate: {winrate:.0%} ({bucket['wins']}/{bucket['settled']})\n"
            f"PnL: <b>${bucket['pnl']:+.2f}</b>"
        )

    await query.edit_message_text(
        "📊 <b>Статистика</b>\n\n"
        f"{_block('Сегодня', stats['today'])}\n\n"
        f"{_block('За всё время', stats['total'])}",
        parse_mode="HTML",
        reply_markup=_back_kb(),
    )


async def cb_how(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        HOW_STRATEGY_TEXT, parse_mode="HTML", reply_markup=_back_kb()
    )


async def cb_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(WITHDRAW_TEXT, parse_mode="HTML", reply_markup=_back_kb())


# ── application ───────────────────────────────────────────────────────────────

COMMANDS = [
    BotCommand("start", "Запуск и онбординг"),
    BotCommand("menu", "Главное меню"),
]


def build_application() -> Application:
    app = Application.builder().token(settings.crypto_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CallbackQueryHandler(cb_create, pattern="^crb_create$"))
    app.add_handler(CallbackQueryHandler(cb_how_wallet, pattern="^crb_how_wallet$"))
    app.add_handler(CallbackQueryHandler(cb_menu, pattern="^crb_menu$"))
    app.add_handler(CallbackQueryHandler(cb_fund, pattern="^crb_fund$"))
    app.add_handler(CallbackQueryHandler(cb_custody, pattern="^crb_custody$"))
    app.add_handler(CallbackQueryHandler(cb_toggle, pattern="^crb_toggle$"))
    app.add_handler(CallbackQueryHandler(cb_stake, pattern="^crb_stake$"))
    app.add_handler(CallbackQueryHandler(cb_stake_set, pattern="^crb_stakeset:"))
    app.add_handler(CallbackQueryHandler(cb_stats, pattern="^crb_stats$"))
    app.add_handler(CallbackQueryHandler(cb_how, pattern="^crb_how$"))
    app.add_handler(CallbackQueryHandler(cb_withdraw, pattern="^crb_withdraw$"))
    return app
