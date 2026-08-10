"""
Fast-path Celery task: copy a donor trade to a subscriber's wallet.
Uses Polymarket CLOB v2 for real order placement.

Integrations in this file:
  BP1  — denormalize condition_id / token_id / outcome_index / neg_risk /
          entry_price / shares onto copy_trades row for on-chain reconciliation.
  BP2  — throttle _notify_low_balance to ≤1 alert per lowbal_alert_throttle_sec.
  BP3  — fractional Kelly sizing (sizing_mode="kelly"); "fixed" keeps legacy behavior.
  BP4  — tail-risk gates (exposure cap, event cap, drawdown, daily loss) evaluated
          before place_order; pauses stored on users table.
  BP7  — Gates 1 & 2 now clamp instead of hard-block on small balances.
          concentration warn ("concentration_over_60") appended to the trade notification.
          Soft-limit "$100" warning is mode-aware: shown only in kelly mode.
  BP13 — Correct sizing-mode hierarchy (User DB > global ENV); zero-edge Kelly skip
          (risk_gate:zero_edge); per-user daily-trade cap (risk_gate:max_daily_trades).
  BP14 — Price-aware Kelly edge damping (core.sizing); AI analysis now returns
          structured risk_score/signal_type/thesis/caution, emoji+verdict derived
          via core.risk_label.risk_label() (single source of truth, shared with
          worker.tasks.ai_filter).
"""

import time

import structlog
from celery import Task

from worker.celery_app import celery_app

log = structlog.get_logger(__name__)


def _confirm_fill(
    wallet_address: str,
    token_id: str,
    intended_usdc: float,
    *,
    baseline_shares: float = 0.0,
    baseline_cost: float = 0.0,
):
    """
    Fallback only: confirm a fill from the Data API when the order response has
    no matched amounts. Cost is shares × average execution price, never the
    volatile current position value.
    """
    from core.order_fill import BuyFill, fill_status
    from core.polymarket import get_positions

    for attempt in range(5):
        try:
            time.sleep(4)
            positions = get_positions(wallet_address)
            pos = next(
                (p for p in positions if p["token_id"] == token_id),
                None,
            )
            if pos and float(pos["shares"]) > baseline_shares:
                total_shares = float(pos["shares"])
                avg_price = float(pos.get("avg_price") or 0)
                shares = total_shares - baseline_shares
                filled = (total_shares * avg_price) - baseline_cost
                fill_price = filled / shares if shares > 0 else 0.0
                if filled > 0 and fill_price > 0:
                    return BuyFill(
                        filled_usdc=filled,
                        shares=shares,
                        fill_price=fill_price,
                        status=fill_status(filled, intended_usdc),
                    )
            log.debug("confirm_fill_retry", token=token_id[:18], attempt=attempt + 1)
        except Exception:
            log.warning("confirm_fill_failed", token=token_id[:18], attempt=attempt + 1)

    return BuyFill(
        filled_usdc=0.0,
        shares=0.0,
        fill_price=0.0,
        status="none",
    )


class ExecuteCopyTask(Task):
    abstract = True
    max_retries = 3
    default_retry_delay = 5


@celery_app.task(
    bind=True,
    base=ExecuteCopyTask,
    name="worker.tasks.execute_copy_trade",
    queue="trades",
)
def execute_copy_trade(self: ExecuteCopyTask, user_id: int, signal: dict) -> dict:
    from core.clob import generate_api_creds, get_market_token_id, place_order
    from core.config import settings
    from core.db import get_supabase, insert_copy_trade, insert_trade_signal

    sb = get_supabase()

    # Load user
    res = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = res.data if res else None

    if not user:
        log.warning("skip_no_user", user_id=user_id)
        return {"skipped": True, "reason": "no_user"}

    # ── Subscription Enforcer ─────────────────────────────────────────────────
    # Gate BEFORE copying OR signalling. Expired subscriptions are skipped and
    # the user is alerted exactly once (DB-backed flag, resets on renewal).
    if not _subscription_guard(user):
        return {"skipped": True, "reason": "subscription_expired"}

    # ── Signal-Only Mode ──────────────────────────────────────────────────────
    # The user opted out of custodial trading: we send a fully-detailed signal
    # but make NO smart-contract / Web3 calls. Open positions (if any) are still
    # managed by sync_positions — this flag only gates ENTRY into new trades.
    if user.get("is_signal_only"):
        _notify_signal_only(user["telegram_id"], signal)
        log.info("signal_only_delivered", user_id=user_id,
                 market=(signal.get("market_id") or "")[:14])
        return {"signal_only": True, "user_id": user_id}

    if not user.get("wallet_private_key_enc"):
        log.warning("skip_no_wallet", user_id=user_id)
        return {"skipped": True, "reason": "no_wallet"}

    # V2: trading goes through the user's deposit wallet (POLY_1271 funder).
    deposit_wallet = user.get("deposit_wallet_address")
    if not deposit_wallet:
        log.warning("skip_not_registered", user_id=user_id)
        _notify_not_registered(user["telegram_id"])
        return {"skipped": True, "reason": "not_registered"}

    # Only copy BUY signals — SELL requires owning the token first
    if signal.get("side", "").upper() in ("SELL", "NO"):
        log.debug("skip_sell_signal", user_id=user_id)
        return {"skipped": True, "reason": "sell_not_supported"}

    # Honor Blueprint 4 drawdown / daily-loss pause (belt-and-suspenders: the
    # fan-out in get_active_subscribers also excludes paused users, but a
    # concurrent pause could arrive between fan-out and task execution).
    from datetime import datetime, timezone
    paused_until = user.get("copy_paused_until")
    if paused_until:
        try:
            from dateutil.parser import parse as _parse_dt
            pu = _parse_dt(paused_until)
            if pu.tzinfo is None:
                pu = pu.replace(tzinfo=timezone.utc)
            if pu > datetime.now(timezone.utc):
                log.info("skip_copy_paused", user_id=user_id,
                         paused_until=paused_until)
                return {"skipped": True, "reason": "copy_paused"}
        except Exception:
            pass

    # ── BP13.2: per-user daily trade cap (fail-fast, before any I/O) ────────
    # Checked here — before balance reads, position loads, and order-book RPCs —
    # so a blocked user costs almost nothing.  Fails open on DB errors (never
    # blocks a trade due to a count failure).
    max_daily = user.get("max_daily_trades")
    if max_daily is not None:
        try:
            from core.db import get_daily_trade_count
            used = get_daily_trade_count(user_id)
            if used >= int(max_daily):
                log.info("skip_max_daily_trades", user_id=user_id,
                         used=used, limit=int(max_daily))
                _notify_daily_limit(user["telegram_id"], used, int(max_daily))
                return {"skipped": True, "reason": "risk_gate:max_daily_trades"}
        except Exception:
            log.warning("daily_trade_count_failed", user_id=user_id)

    # ── Check collateral ─────────────────────────────────────────────────────
    try:
        from core.polygon import fund_deposit_wallet, get_balances
        tradeable = get_balances(deposit_wallet).get("pusd", 0)
    except Exception:
        log.warning("balance_check_failed", user_id=user_id)
        tradeable = 0.0
    balance_cap = tradeable * max(0.0, 1.0 - settings.fee_headroom_pct)

    # ── BP13.1 / BP3: sizing mode — User DB setting beats global ENV ─────────
    # Authoritative priority: users.sizing_mode (DB) > settings.sizing_mode (ENV).
    # Global ENV is only a default for users who never explicitly chose a mode.
    user_mode = user.get("sizing_mode")
    effective_mode = user_mode if user_mode in ("fixed", "kelly") else settings.sizing_mode

    user_max  = float(user.get("max_position_usdc") or 25)
    depth_cap = float(signal.get("max_copy_usdc") or signal.get("size_usdc") or 0)

    # BP8: load cost-basis ledger for both modes (cost-basis equity, no phantom drawdown).
    try:
        from core.polymarket import get_positions as _gp
        positions = _gp(deposit_wallet)
        positions_loaded = True
    except Exception:
        positions = []
        positions_loaded = False
    try:
        from core.db import get_open_trades_cost
        from core.risk import total_equity
        ledger_cost = get_open_trades_cost(user_id)
        equity = total_equity(
            tradeable,
            positions,
            ledger_cost,
            mode=settings.drawdown_equity_mode,
        )
    except Exception:
        ledger_cost = {}
        equity = tradeable + sum(
            float(p.get("current_value") or 0) for p in positions if p.get("shares", 0) > 0
        )

    if effective_mode == "kelly":
        from core.sizing import kelly_stake
        from core.wallet_score import score_wallet
        try:
            score = score_wallet(signal.get("source_wallet") or signal.get("whale_wallet") or "")
        except Exception:
            score = None
        # BP14.A Step 2 observability: a wallet falling through to the unscored
        # default (quality=0.5 in kelly_stake) means the edge can't differentiate
        # signals. Track the scored/unscored split to catch a regression in
        # source_wallet population upstream (poll_tracked_wallets / scan_markets).
        if not score or not score.get("resolved_count"):
            log.info("kelly_wallet_unscored", user_id=user_id,
                     source_wallet=(signal.get("source_wallet") or "")[:10])
        k_stake = kelly_stake(
            p=float(signal.get("price") or 0),
            score=score,
            consensus=int(signal.get("consensus") or 1),
            equity=equity,
            free_pusd=tradeable,
            cfg=settings,
        )
        size_usdc = min(k_stake, user_max)
        if depth_cap > 0:
            size_usdc = min(size_usdc, depth_cap)
        # BP13.1b: zero-edge or sub-minimum stake → skip entirely; never fall back to fixed.
        # The exchange_min floor below must NOT resurrect a sub-minimum Kelly stake.
        # NOTE: the floor at line ~235 only runs if we pass this guard.
        if k_stake <= 0 or size_usdc < settings.exchange_min_order_usdc:
            log.info("skip_zero_edge", user_id=user_id,
                     k_stake=round(k_stake, 4), capped=round(size_usdc, 4),
                     exchange_min=settings.exchange_min_order_usdc)
            return {"skipped": True, "reason": "risk_gate:zero_edge"}
        log.info("sizing_kelly", stake=round(k_stake, 2), capped=round(size_usdc, 2),
                 equity=round(equity, 2), user_id=user_id)
    else:
        # Fixed cap: user chose fixed, or no explicit per-user choice and global default is fixed.
        size_usdc = min(user_max, depth_cap) if depth_cap > 0 else user_max
        score = None
        log.debug("sizing_fixed", cap=round(size_usdc, 2), user_id=user_id)

    # ── BP8 / BP39: unified per-trade risk cap — KELLY MODE ONLY ──────────────
    # Worst-case loss of a binary trade = full stake; in kelly mode cap it at
    # max_risk_per_trade × equity. BP39: fixed mode is exempt — the user chose
    # an explicit dollar size and the cap silently overrode it (a $15 fixed
    # stake on a $45 account was cut to 5% ≈ $2.3, then floored back up to the
    # $5 exchange minimum, so every trade entered at $5). Fixed means fixed;
    # the tail-risk gates below (exposure/event/drawdown/daily-loss) still apply.
    if (effective_mode == "kelly"
            and settings.enforce_risk_per_trade_cap and equity > 0):
        hard_cap = settings.max_risk_per_trade * equity
        if size_usdc > hard_cap:
            log.info("unified_risk_cap_applied", user_id=user_id,
                     original=round(size_usdc, 2), capped=round(hard_cap, 2),
                     equity=round(equity, 2))
            size_usdc = hard_cap

    # BP8: profit-protection trailing cap — don't give back >max_trade_loss_vs_profit_pct
    # of accumulated realized profit above the baseline in a single trade.
    # BP39: kelly-only for the same reason as the unified cap above.
    if (effective_mode == "kelly"
            and settings.max_trade_loss_vs_profit_pct > 0 and equity > 0):
        try:
            from core.db import get_realized_baseline
            baseline = get_realized_baseline(user_id)
            if baseline is None:
                baseline = equity  # first ever trade — no profit cushion yet
            profit_above_baseline = max(0.0, equity - baseline)
            if profit_above_baseline > 0:
                profit_cap = (
                    settings.max_trade_loss_vs_profit_pct * profit_above_baseline
                    + settings.max_risk_per_trade * baseline
                )
                if size_usdc > profit_cap:
                    log.info("profit_protection_cap_applied", user_id=user_id,
                             original=round(size_usdc, 2), capped=round(profit_cap, 2),
                             profit_cushion=round(profit_above_baseline, 2))
                    size_usdc = profit_cap
        except Exception:
            log.warning("profit_protection_cap_failed", user_id=user_id)

    # ── BP3.1 / BP7: soft balance warning — warn but never hard-block ────────
    # A $5 wallet trades at the platform minimum; we only skip when the wallet
    # genuinely cannot afford even that minimum order.
    # BP7: the "$100 recommended" warning is shown only in kelly mode — in fixed
    # mode the user chose their own size, so we stay silent about balance.
    exchange_min = settings.exchange_min_order_usdc
    if equity < settings.recommended_min_balance_usdc:
        if effective_mode == "kelly":
            from core.cache import notify_once as _no
            if _no(f"trading_min:{user_id}", ttl=settings.lowbal_alert_throttle_sec):
                _notify_trading_at_minimum(
                    user["telegram_id"], tradeable, settings.recommended_min_balance_usdc,
                )
        log.debug("equity_below_recommended", user_id=user_id,
                  equity=round(equity, 2),
                  recommended=settings.recommended_min_balance_usdc)

    # Floor size up to the exchange minimum so Kelly's tiny fractions still execute.
    size_usdc = max(size_usdc, exchange_min)
    # Reserve fee headroom whenever the balance is the binding cap.
    size_usdc = min(size_usdc, balance_cap)

    # ── Fund deposit wallet on demand if short ───────────────────────────────
    if balance_cap < exchange_min:
        eoa = {}
        try:
            from core.polygon import get_balances as _gb
            eoa = _gb(user["wallet_address"])
        except Exception:
            pass
        on_eoa = eoa.get("pusd", 0) + eoa.get("usdc_e", 0) + eoa.get("usdc", 0)
        if on_eoa >= 0.5:
            try:
                from core.polygon import fund_deposit_wallet
                fund_deposit_wallet(
                    user["wallet_private_key_enc"],
                    user["wallet_address"],
                    deposit_wallet,
                )
                tradeable = get_balances(deposit_wallet).get("pusd", 0)
                balance_cap = (
                    tradeable * max(0.0, 1.0 - settings.fee_headroom_pct)
                )
            except Exception:
                log.warning("ondemand_fund_failed", user_id=user_id)

    # Re-clamp after possible sweep.
    size_usdc = min(max(size_usdc, exchange_min), balance_cap)

    # Only skip if the wallet genuinely cannot afford the platform minimum.
    if balance_cap < exchange_min:
        _notify_low_balance(user["telegram_id"], tradeable, exchange_min, signal)
        log.warning("skip_insufficient_for_min_order", user_id=user_id,
                    pusd=round(tradeable, 4), available_after_fee=round(balance_cap, 4),
                    min_order=exchange_min)
        return {"skipped": True, "reason": "insufficient_for_min_order"}

    # ── Portfolio guards: already-in-market + max open positions ─────────────
    cond = signal.get("market_id")
    consensus = int(signal.get("consensus") or 1)
    try:
        if any(p for p in positions if p.get("condition_id") == cond and p["shares"] > 0):
            if consensus >= 2:
                _notify_consensus(user["telegram_id"], signal, consensus)
            log.info("skip_already_in_market", user_id=user_id,
                     market=(cond or "")[:14], consensus=consensus)
            return {"skipped": True, "reason": "already_in_market"}

        # BP31: count only economically live positions. Resolved leftovers stay in
        # the Data-API forever with shares>0 (losing tokens are never redeemed,
        # redeemable winners are freed shortly) — they must not consume the
        # max_open_positions slots and starve regular whale copying.
        open_count = sum(
            1
            for p in positions
            if p["shares"] > 0
            and not p.get("redeemable")
            and float(p.get("current_value") or 0) >= 0.01
        )
        if open_count >= settings.max_open_positions:
            log.info("skip_max_positions", user_id=user_id, open=open_count)
            return {"skipped": True, "reason": "max_positions"}
    except Exception:
        log.warning("positions_count_failed", user_id=user_id)

    # ── Ensure CLOB API credentials ──────────────────────────────────────────
    api_creds = {
        "clob_api_key":    user.get("clob_api_key"),
        "clob_secret":     user.get("clob_secret"),
        "clob_passphrase": user.get("clob_passphrase"),
    }
    if not api_creds["clob_api_key"]:
        try:
            api_creds = generate_api_creds(user["wallet_private_key_enc"],
                                           funder=deposit_wallet)
            sb.table("users").update(api_creds).eq("id", user_id).execute()
            log.info("clob_creds_generated", user_id=user_id)
        except Exception as exc:
            log.exception("clob_creds_failed", user_id=user_id)
            raise self.retry(exc=exc)

    # ── Resolve token_id ─────────────────────────────────────────────────────
    token_id: str | None = signal.get("token_id")
    if not token_id:
        token_id = get_market_token_id(signal["market_id"], signal.get("side", "YES"))
    if not token_id:
        log.warning("skip_no_token_id", market_id=signal["market_id"])
        return {"skipped": True, "reason": "no_token_id"}

    # Capture a pre-order baseline so the rare Data-API fallback can isolate this
    # order from any shares that were already held. The exact response path does
    # not use this snapshot.
    fallback_baseline_known = positions_loaded
    fallback_baseline_shares = 0.0
    fallback_baseline_cost = 0.0
    if positions_loaded:
        baseline_position = next(
            (position for position in positions if position.get("token_id") == token_id),
            {},
        )
        fallback_baseline_shares = float(baseline_position.get("shares") or 0)
        fallback_baseline_cost = fallback_baseline_shares * float(
            baseline_position.get("avg_price") or 0
        )

    # ── Fresh order-book re-check ────────────────────────────────────────────
    entry_price = float(signal.get("price") or 0)
    # Blueprint 17 Layer 3: capture best_bid at fill time for the bid-vs-bid
    # drop comparison in the position monitor.
    entry_bid_at_fill: float = 0.0
    try:
        from core.polymarket import get_order_book

        book = get_order_book(token_id)
        if book and book.get("best_ask"):
            entry_price = float(book["best_ask"])
            if entry_price > settings.max_entry_price or entry_price < settings.min_entry_price:
                log.info("skip_price_out_of_range", user_id=user_id, price=entry_price)
                return {"skipped": True, "reason": "price_out_of_range"}
            band = entry_price * (1.0 + settings.order_slippage_pct)
            fillable = sum(
                lvl["price"] * lvl["size"] for lvl in book["asks"] if lvl["price"] <= band
            )
            cap = fillable * settings.book_safe_frac
            if cap > 0:
                size_usdc = min(size_usdc, cap)
        if book and book.get("best_bid"):
            entry_bid_at_fill = float(book["best_bid"])
    except Exception:
        log.warning("exec_book_check_failed", user_id=user_id)

    # After the book re-check, ensure the balance/depth caps still
    # leave enough room for a platform-minimum order.
    size_usdc = max(size_usdc, exchange_min)
    size_usdc = min(size_usdc, balance_cap)
    if size_usdc < exchange_min or balance_cap < exchange_min:
        log.debug("skip_depth_below_min", user_id=user_id,
                  size=round(size_usdc, 4), exchange_min=exchange_min)
        return {"skipped": True, "reason": "depth_below_min_order"}

    # ── BP4 / BP7 / BP8: tail-risk gates ─────────────────────────────────────
    # BP7: Gates 1 & 2 now clamp instead of hard-block on small balances.
    # BP8: execute_copy is now an ENFORCER only — it honors the pause but never
    #      sends the pause notification (that is solely manage_positions' job).
    # decision.max_stake is applied below; decision.warn is forwarded to _notify.
    #
    # Blueprint 17.B: if risk_override_until is in the future the user has
    # explicitly accepted risk until midnight UTC.  Pass daily_pnl=0 so gate 4
    # (daily-loss) does not block new entries for the rest of the day.
    # BP26.9: the override must ALSO neutralize gate 3 (drawdown). The unlock
    # handler resets the HWM, but that reset can be undone within minutes: the
    # monitor pushes the HWM back up from cost-basis equity that still counts
    # doomed-but-unsettled positions at entry cost, and once those losses settle
    # the stale HWM re-blocks every entry — silently, for a user who just
    # explicitly accepted the risk (prod: 7 manual unlocks with zero effect).
    # Passing hwm=0 makes check_risk_gates use hwm=max(0, equity)=equity →
    # drawdown=0 for the rest of the UTC day. Gates 1-2 (exposure) still apply.
    concentration_warn: str | None = None
    try:
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        from core.db import get_daily_realized_pnl, get_risk_override_until, get_user_equity_hwm
        from core.risk import check_risk_gates
        hwm = get_user_equity_hwm(user_id)
        daily_pnl = get_daily_realized_pnl(user_id)
        try:
            _override_ts = get_risk_override_until(user_id)
            if _override_ts:
                from dateutil.parser import parse as _pdt
                _override_exp = _pdt(_override_ts)
                if _override_exp.tzinfo is None:
                    _override_exp = _override_exp.replace(tzinfo=_tz.utc)
                if _dt.now(_tz.utc) < _override_exp:
                    log.debug("risk_gates34_bypassed_override",
                              user_id=user_id, override_until=_override_ts)
                    daily_pnl = 0.0  # gate 4 sees no loss → does not block
                    hwm = 0.0        # gate 3 sees no drawdown → does not block
        except Exception:
            pass
        decision = check_risk_gates(
            signal=signal,
            stake=size_usdc,
            open_positions=positions,
            equity=equity,
            equity_hwm=hwm,
            daily_pnl=daily_pnl,
            cfg=settings,
            ledger_cost_by_token=ledger_cost,
        )
        if not decision.allowed:
            gate = decision.gate
            # BP8: no notification here — manage_positions owns the pause alert.
            log.info("skip_risk_gate", user_id=user_id,
                     gate=gate, reason=decision.reason[:80])
            return {"skipped": True, "reason": f"risk_gate:{gate}"}

        # BP7: apply clamped stake when Gates 1/2 reduced it (e.g. partial headroom).
        if decision.max_stake is not None:
            clamped = max(min(decision.max_stake, balance_cap), exchange_min)
            log.info("risk_gate_clamp_applied", user_id=user_id,
                     original=round(size_usdc, 2), clamped=round(clamped, 2),
                     gate=decision.gate)
            size_usdc = clamped

        # Carry the concentration warning forward to the trade notification.
        concentration_warn = decision.warn
    except Exception:
        log.warning("risk_gate_check_failed", user_id=user_id)

    # ── Idempotency guard & signal insert ────────────────────────────────────
    signal_id = signal.get("signal_id")
    if not signal_id:
        sig_row = insert_trade_signal({
            "market_id":      signal["market_id"],
            "title":          signal.get("title", ""),
            "side":           signal["side"],
            "price":          signal["price"],
            "size_usdc":      size_usdc,
            "token_id":       token_id,
            "source_tx_hash": signal.get("source_tx_hash", ""),
        })
        signal_id = sig_row["id"]

    try:
        existing = (
            sb.table("copy_trades")
            .select("id,status")
            .eq("user_id", user["id"])
            .eq("signal_id", signal_id)
            .neq("status", "failed")
            .neq("status", "executing")
            .maybe_single()
            .execute()
        )
        if existing and existing.data:
            log.info("skip_already_executed", user_id=user_id, signal_id=signal_id)
            return {"skipped": True, "reason": "already_executed"}
    except Exception:
        pass

    # ── BP1: denormalize settlement fields onto copy_trades row ──────────────
    outcome_index = signal.get("outcome_index")
    if outcome_index is None:
        # Infer from outcome name when not explicitly provided.
        outcome_name = str(signal.get("outcome") or "").strip().lower()
        outcome_index = 0 if outcome_name.startswith("yes") else 1

    # BP16.4: entry_price invariant (defense-in-depth). entry_price is the fresh
    # order-book best_ask with a fallback to the signal price; if BOTH are missing
    # we must NOT persist a 0 cost basis (it breaks the /positions view and % PnL).
    if entry_price <= 0:
        entry_price = float(signal.get("price") or 0)
    if entry_price <= 0:
        log.warning("entry_price_zero_guard", user_id=user_id,
                    market=(cond or "")[:14], token=token_id[:18])
        return {"skipped": True, "reason": "no_entry_price"}

    trade_row = insert_copy_trade({
        "user_id":        user["id"],
        # BP24: stamp the wallet that opened this trade (the user's ACTIVE wallet,
        # mirrored on the users row). Exits/redeems sign with THIS wallet even after
        # the user later switches. NULL-safe for the pre-migration-018 schema.
        **({"wallet_id": user["active_wallet_id"]} if user.get("active_wallet_id") else {}),
        "signal_id":      signal_id,
        "status":         "executing",
        "size_usdc":      size_usdc,
        # Settlement ledger fields (migration 008)
        "condition_id":   cond,
        "token_id":       token_id,
        "outcome_index":  int(outcome_index),
        "neg_risk":       bool(signal.get("neg_risk", False)),
        "entry_price":    round(entry_price, 6),
        # Blueprint 17 Layer 3: CLOB best_bid at fill time (Layer 3 bid-vs-bid guard).
        # NULL-safe: column may be absent in old DB schema — insert only when present.
        **({"entry_bid": round(entry_bid_at_fill, 6)} if entry_bid_at_fill > 0 else {}),
    })

    try:
        result = place_order(
            private_key_enc=user["wallet_private_key_enc"],
            api_creds=api_creds,
            token_id=token_id,
            side=signal["side"],
            price=entry_price,
            size_usdc=size_usdc,
            tick_size=str(signal.get("tick_size", "0.01")),
            neg_risk=bool(signal.get("neg_risk", False)),
            slippage_pct=settings.order_slippage_pct,
            deposit_wallet=deposit_wallet,
        )

        order_id = result.get("orderID") or result.get("order_id") or ""

        try:
            sb.table("copy_trades").update({
                "status":   "placed",
                "order_id": order_id,
            }).eq("id", trade_row["id"]).execute()
        except Exception:
            # The legacy live schema has no order_id. Keep the state transition
            # instead of dropping the whole update.
            try:
                sb.table("copy_trades").update({"status": "placed"}).eq(
                    "id", trade_row["id"]
                ).execute()
            except Exception:
                log.warning("copy_trade_placed_update_failed", trade_id=trade_row["id"])

        from core.order_fill import extract_buy_fill

        fill = extract_buy_fill(result, size_usdc)
        fill_source = "order_response"
        if fill is None:
            fill_source = "data_api_fallback"
            if fallback_baseline_known:
                fill = _confirm_fill(
                    deposit_wallet,
                    token_id,
                    size_usdc,
                    baseline_shares=fallback_baseline_shares,
                    baseline_cost=fallback_baseline_cost,
                )
            else:
                log.error(
                    "fill_fallback_baseline_unknown",
                    user_id=user_id,
                    order_id=order_id,
                    token=token_id[:18],
                )
                try:
                    sb.table("copy_trades").update({
                        "error_msg": "fill accounting requires reconciliation",
                    }).eq("id", trade_row["id"]).execute()
                except Exception:
                    pass
                return {
                    "order_id": order_id,
                    "user_id": user_id,
                    "error": "fill_reconciliation_required",
                }

        filled = fill.filled_usdc
        fill_status = fill.status
        shares_filled = round(fill.shares, 6)
        fee_estimate = round(size_usdc * settings.fee_headroom_pct, 6)
        if fill.fee_usdc is None:
            log.info(
                "order_fee_estimate",
                user_id=user_id,
                order_id=order_id,
                estimate_usdc=fee_estimate,
                headroom_pct=settings.fee_headroom_pct,
            )

        try:
            from core.polygon import get_balances
            remaining = get_balances(deposit_wallet).get("pusd", 0.0)
        except Exception:
            remaining = 0.0

        score_val, signal_type_val, thesis_val, caution_val = None, None, None, None
        try:
            from worker.tasks.ai_filter import _call_gpt
            score_val, signal_type_val, thesis_val, caution_val = _call_gpt(signal)
        except Exception:
            log.warning("ai_inline_failed", user_id=user_id)

        final_status = "confirmed" if fill_status != "none" else "unfilled"
        update_payload: dict = {
            "status": final_status,
            "size_usdc": (
                round(filled, 2)
                if fill_status in ("full", "partial")
                else size_usdc
            ),
        }
        if fill_status in ("full", "partial") and shares_filled > 0:
            update_payload["shares"] = shares_filled
            update_payload["fill_price"] = round(fill.fill_price, 6)
        if fill.fee_usdc is not None:
            update_payload["fee_usdc"] = round(fill.fee_usdc, 6)

        ledger_updated = False
        ledger_error: Exception | None = None
        for ledger_attempt in range(settings.trade_ledger_update_attempts):
            try:
                sb.table("copy_trades").update(update_payload).eq(
                    "id", trade_row["id"]
                ).execute()
                ledger_updated = True
                break
            except Exception as exc:
                ledger_error = exc
                # Migration 020 may not be applied yet. Preserve the core ledger
                # update and omit only the optional fee column.
                if "fee_usdc" in update_payload:
                    update_payload.pop("fee_usdc")
                    continue
                if ledger_attempt < settings.trade_ledger_update_attempts - 1:
                    time.sleep(
                        settings.trade_ledger_update_retry_sec * (ledger_attempt + 1)
                    )

        if not ledger_updated:
            # Never re-place an already matched order. Leave the row at least in
            # "placed" and surface a loud reconciliation error for operators.
            log.error(
                "copy_trade_ledger_update_failed",
                trade_id=trade_row["id"],
                order_id=order_id,
                error=str(ledger_error)[:300] if ledger_error else "unknown",
            )
            return {
                "order_id": order_id,
                "user_id": user_id,
                "error": "ledger_update_failed",
            }

        # BP4: update HWM after a successful trade (equity may have changed).
        try:
            from core.db import get_user_equity_hwm, update_user_equity_hwm
            current_hwm = get_user_equity_hwm(user_id)
            if equity > current_hwm:
                update_user_equity_hwm(user_id, equity)
        except Exception:
            pass

        log.info("copy_trade_ok", user_id=user_id, order_id=order_id,
                 fill=fill_status, filled=round(filled, 2),
                 shares=shares_filled, fill_price=round(fill.fill_price, 6),
                 fill_source=fill_source, fee_usdc=fill.fee_usdc,
                 cond=(cond or "")[:14])
        _notify(user["telegram_id"], signal, order_id, size_usdc, filled,
                fill_status, remaining, score_val, signal_type_val, thesis_val, caution_val,
                concentration_warn=concentration_warn)

        return {"order_id": order_id, "user_id": user_id,
                "fill": fill_status, "filled": filled}

    except Exception as exc:
        try:
            sb.table("copy_trades").update({
                "status":    "failed",
                "error_msg": str(exc)[:1000],
            }).eq("id", trade_row["id"]).execute()
        except Exception:
            pass
        log.exception("copy_trade_failed", user_id=user_id)
        raise self.retry(exc=exc)


# ── Subscription Enforcer ───────────────────────────────────────────────────

def _subscription_guard(user: dict) -> bool:
    """Return True when the user's subscription is active (copy/signal allowed).

    Idempotent side-effects:
      * active subscription  → clear ``subscription_notified_expired`` once, so a
        future expiry can alert again.
      * expired/inactive     → send the expiry message exactly once and set
        ``subscription_notified_expired = True`` to prevent per-trade spam.

    Returns False when the subscription is not active (caller must skip the trade).
    """
    from core.db import is_subscription_active, set_subscription_notified_expired

    uid = user.get("id")
    if is_subscription_active(user):
        if user.get("subscription_notified_expired"):
            try:
                set_subscription_notified_expired(uid, False)
            except Exception:
                log.warning("sub_flag_reset_failed", user_id=uid)
        return True

    if not user.get("subscription_notified_expired"):
        _notify_subscription_expired(user["telegram_id"])
        try:
            set_subscription_notified_expired(uid, True)
        except Exception:
            log.warning("sub_flag_set_failed", user_id=uid)
    log.info("skip_subscription_expired", user_id=uid)
    return False


def _tg_send(chat_id: int, text: str, *, disable_preview: bool = False) -> None:
    """BP23: gevent-safe Telegram sendMessage.

    The worker runs on a gevent pool — one OS thread, many greenlets.  The old
    notifier pattern ``asyncio.run(PTB Bot.send_message)`` blew up with
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``
    whenever two notifications overlapped: greenlet A's loop was still running
    (parked on the Telegram HTTP await) when gevent switched to greenlet B, which
    then called ``asyncio.run()`` in the *same* thread.  Under a fan-out burst
    (e.g. the signal-only broadcast after an Alchemy outage cleared) that dropped
    most messages.  A plain synchronous ``httpx.post`` (httpx is gevent-patched)
    owns no event loop, so it is concurrency-safe and never raises that error.

    ``raise_for_status`` is intentional: it preserves the previous behaviour where
    a Telegram 403 (user blocked the bot) surfaces to the caller's ``except`` and
    is logged as ``notify_*_failed`` rather than silently swallowed.
    """
    import httpx

    from core.config import settings

    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if disable_preview:
        payload["disable_web_page_preview"] = True
    resp = httpx.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
        json=payload,
        timeout=10.0,
    )
    resp.raise_for_status()


def _notify_subscription_expired(telegram_id: int) -> None:
    try:
        _tg_send(
            telegram_id,
            (
                "❌ <b>Ваша подписка истекла.</b>\n\n"
                "Торговля и отправка сигналов остановлены. "
                "Продлите подписку для продолжения работы."
            ),
        )
    except Exception:
        log.exception("notify_sub_expired_failed", telegram_id=telegram_id)


# ── Notifications ─────────────────────────────────────────────────────────────

def _notify_signal_only(telegram_id: int, signal: dict) -> None:
    """Signal-Only Mode alert — a complete manual-trade brief.

    Carries everything the user needs to place the trade by hand on Polymarket:
    event title, the concrete outcome (Yes/No or candidate/team name), the live
    price / implied probability, and the whale conviction metrics.
    """
    from core.polymarket import event_url, format_time_left, get_order_book

    title = (signal.get("title") or "—")[:80]
    url = event_url(signal.get("event_slug"))
    title_html = f"<a href=\"{url}\">{title}</a>" if url else f"<b>{title}</b>"
    outcome = signal.get("outcome") or "—"

    # Prefer a fresh order-book ask; fall back to the signal's VWAP entry.
    price = float(signal.get("price") or 0)
    token_id = signal.get("token_id")
    if token_id:
        try:
            book = get_order_book(token_id)
            if book and book.get("best_ask"):
                price = float(book["best_ask"])
        except Exception:
            pass
    prob = f"{price * 100:.0f}%" if price else "—"

    whale_usdc = float(signal.get("size_usdc") or 0)
    fills = int(signal.get("fills") or 0)
    consensus = int(signal.get("consensus") or 1)
    whale_line = (
        f"🐳 Кит вошёл на: <b>${whale_usdc:,.0f}</b>"
        if whale_usdc else "🐳 Сигнал от кита"
    )
    if fills > 1:
        whale_line += f" ({fills} сделок)"
    if consensus >= 2:
        whale_line += f"\n🔥 <b>Консенсус: {consensus} кита</b> в этом исходе"

    # BP5: compute time-left fresh at send time, never from a cached scalar.
    time_left = format_time_left(signal.get("resolution_iso"))
    hours_line = f" · ⏳ {time_left}" if time_left else ""
    link_line = f"\n🔗 <a href=\"{url}\">Открыть рынок на Polymarket</a>" if url else ""

    msg = (
        f"🔔 <b>Новый сигнал по киту</b>\n\n"
        f"📌 {title_html}\n"
        f"🎯 Исход: <b>{outcome}</b> @ {price:.3f} (~{prob}){hours_line}\n"
        f"{whale_line}\n"
        "━━━━━━━━━━━━━━━━━\n"
        "💡 Режим <b>«Только сигналы»</b>: бот не открывает сделку за тебя.\n"
        f"Чтобы войти — открой рынок и купи <b>{outcome}</b> вручную."
        f"{link_line}"
    )

    try:
        _tg_send(telegram_id, msg, disable_preview=True)
    except Exception as exc:
        # BP26.8: a user who blocked the bot 403s on EVERY signal fan-out
        # (prod: 876 error logs / 72 h from two users). Log it once a day per
        # user as a warning instead of a full traceback per signal.
        if "403" in str(exc):
            from core.cache import notify_once
            if notify_once(f"tg_blocked:{telegram_id}", ttl=86400):
                log.warning("notify_signal_only_blocked", telegram_id=telegram_id)
            return
        log.exception("notify_signal_only_failed", telegram_id=telegram_id)


def _notify_consensus(telegram_id: int, signal: dict, consensus: int) -> None:
    from core.cache import notify_once
    from core.polymarket import event_url

    cond = signal.get("market_id", "")
    if not notify_once(f"consensus:{telegram_id}:{cond}:{consensus}"):
        return

    title = (signal.get("title") or "—")[:60]
    outcome = signal.get("outcome") or "—"
    url = event_url(signal.get("event_slug"))
    link = f"\n🔗 <a href=\"{url}\">Смотреть позицию</a>" if url else ""

    try:
        _tg_send(
            telegram_id,
            (
                f"🔥 <b>Ещё один профи зашёл!</b>\n\n"
                f"📌 {title}\n"
                f"🎯 Исход: <b>{outcome}</b>\n\n"
                f"Уже <b>{consensus} проверенных кита</b> в этом исходе — "
                f"уверенность в победе растёт. Твоя позиция уже открыта, "
                f"повторно не входим.{link}"
            ),
            disable_preview=True,
        )
    except Exception:
        log.exception("notify_consensus_failed", telegram_id=telegram_id)


def _notify_daily_limit(telegram_id: int, used: int, limit: int) -> None:
    """BP13.2: one throttled nudge per user when the daily trade cap is hit."""
    from core.cache import notify_once
    from core.config import settings

    if not notify_once(f"daily_limit:{telegram_id}", ttl=settings.lowbal_alert_throttle_sec):
        return

    try:
        _tg_send(
            telegram_id,
            (
                f"🔁 <b>Дневной лимит сделок исчерпан</b>\n\n"
                f"Ты установил лимит <b>{limit} сделок/день</b> — "
                f"сегодня уже вошли в <b>{used}</b>.\n\n"
                "Новые сигналы пропускаются до 00:00 UTC.\n"
                "Изменить лимит: ⚙️ Настройки."
            ),
        )
    except Exception:
        log.exception("notify_daily_limit_failed", telegram_id=telegram_id)


def _notify_not_registered(telegram_id: int) -> None:
    try:
        _tg_send(
            telegram_id,
            (
                "⚙️ <b>Нужна настройка кошелька</b>\n\n"
                "Чтобы бот копировал сделки, разверни торговый кошелёк: /register "
                "(без газа с твоей стороны). После этого пополни баланс и включи /resume."
            ),
        )
    except Exception:
        log.exception("notify_not_registered_failed", telegram_id=telegram_id)


def _notify_low_balance(
    telegram_id: int,
    balance: float,
    needed: float,
    signal: dict,
    reason: str = "low_balance",
) -> None:
    """
    BP2: at most one low-balance alert per user per lowbal_alert_throttle_sec.
    The throttle key is per-user, independent of the signal, so a high-volume
    signal day does not produce alert spam.
    """
    from core.cache import notify_once
    from core.config import settings

    # Throttled per-user regardless of which signal triggered it.
    if not notify_once(f"lowbal:{telegram_id}", ttl=settings.lowbal_alert_throttle_sec):
        return

    title = signal.get("title") or "—"
    if reason == "min_balance":
        text = (
            f"⚠️ <b>Баланс ниже минимума для копирования</b>\n\n"
            f"📌 {title}\n\n"
            f"💰 Минимальный баланс: <b>${needed:.2f} USDC</b>\n"
            f"💼 Текущий капитал: <b>${balance:.2f} USDC</b>\n\n"
            f"Пополни кошелёк через /wallet чтобы не пропускать сделки."
        )
    else:
        text = (
            f"⚠️ <b>Недостаточно средств для сделки</b>\n\n"
            f"📌 {title}\n\n"
            f"💰 Нужно: <b>${needed:.2f} USDC</b>\n"
            f"💼 На балансе: <b>${balance:.2f} USDC</b>\n\n"
            f"Пополни кошелёк через /wallet чтобы не пропускать сделки."
        )

    try:
        _tg_send(telegram_id, text)
    except Exception:
        log.exception("notify_low_balance_failed", telegram_id=telegram_id)


def _notify_trading_at_minimum(telegram_id: int, balance: float, recommended: float) -> None:
    """
    BP3.1: soft warning — balance below recommended, but we still trade at minimum.
    Throttled via notify_once in the caller (once per lowbal_alert_throttle_sec).
    """
    try:
        _tg_send(
            telegram_id,
            (
                f"⚠️ <b>Торгуем на минимальном объёме</b>\n\n"
                f"💼 Баланс: <b>${balance:.2f} pUSD</b> "
                f"(рекомендуется ≥ ${recommended:.0f})\n\n"
                f"Бот продолжает копировать сделки, но использует минимально "
                f"допустимый размер ордера.\n"
                f"Пополни кошелёк через /wallet для нормального риск-менеджмента."
            ),
        )
    except Exception:
        log.exception("notify_trading_at_minimum_failed", telegram_id=telegram_id)


def _notify_risk_pause(telegram_id: int, reason: str) -> None:
    """BP4: notify user that copying was paused by a risk gate."""
    try:
        _tg_send(
            telegram_id,
            (
                f"🛡 <b>Риск-защита сработала</b>\n\n"
                f"{reason}\n\n"
                f"Копирование приостановлено автоматически для защиты депозита. "
                f"После отдыха бот возобновится сам."
            ),
        )
    except Exception:
        log.exception("notify_risk_pause_failed", telegram_id=telegram_id)


def _notify(
    telegram_id: int, signal: dict, order_id: str,
    intended_usdc: float, filled_usdc: float, fill_status: str,
    remaining: float = 0.0,
    ai_score: int | None = None,
    ai_signal_type: str | None = None,
    ai_thesis: str | None = None,
    ai_caution: str | None = None,
    concentration_warn: str | None = None,
) -> None:
    """One combined message: trade result + AI analysis + balance remaining.

    concentration_warn: when "concentration_over_60" (BP7), append a risk note
    about position concentration directly into this message (no separate alert).
    """
    from core.polymarket import event_url, format_time_left

    title = (signal.get("title") or "—")[:60]
    url = event_url(signal.get("event_slug"))
    title_html = f"<a href=\"{url}\">{title}</a>" if url else f"<b>{title}</b>"
    price = float(signal.get("price") or 0)
    outcome = signal.get("outcome") or "—"
    prob = f"{price * 100:.0f}%"
    whale_usdc = float(signal.get("size_usdc") or 0)
    fills = int(signal.get("fills") or 0)
    consensus = int(signal.get("consensus") or 1)
    whale_line = f"🐳 Профи вошёл на: <b>${whale_usdc:,.0f}</b>" if whale_usdc else "🐳 Сигнал от профи-кошелька"
    if fills > 1:
        whale_line += f" ({fills} сделок)"
    if consensus >= 2:
        whale_line += f"\n🔥 <b>Консенсус: {consensus} профи</b> в этом исходе"
    # BP5: compute time-left fresh at notification time, never from a cached scalar.
    time_left = format_time_left(signal.get("resolution_iso"))
    hours_line = f" · ⏳ {time_left}"
    link_line = f"\n🔗 <a href=\"{url}\">Смотреть позицию</a>" if url else ""

    if fill_status == "none":
        msg = (
            f"⚠️ <b>Сделка не прошла</b>\n\n"
            f"📌 {title_html}\n"
            f"🎯 Исход: <b>{outcome}</b>\n\n"
            f"Стакан слишком тонкий — ордер не наполнился, позиция не открыта.{link_line}"
        )
    else:
        head = "✅ <b>Бот открыл позицию</b>"
        if fill_status == "partial":
            head = "✅ <b>Бот открыл позицию (частично)</b>"

        invested = filled_usdc if fill_status in ("full", "partial") else intended_usdc
        partial_note = f" из ${intended_usdc:.2f} (тонкий стакан)" if fill_status == "partial" else ""

        ai_block = ""
        if ai_score is not None and ai_thesis:
            from core.risk_label import risk_label
            emoji, verdict = risk_label(ai_score)
            caution_line = f"\n⚠️ {ai_caution}" if ai_caution else ""
            ai_block = (
                f"\n\n━━━━━━━━━━━━━━━━━\n"
                f"🧠 <b>ИИ-анализ</b>\n"
                f"{emoji} <b>{verdict}</b> · риск {ai_score}/10\n"
                f"💬 {ai_thesis}"
                f"{caution_line}"
            )

        # BP7: inline concentration note — appended here, no separate message.
        concentration_line = ""
        if concentration_warn == "concentration_over_60":
            concentration_line = "\n⚠️ Позиция заняла >60% капитала — риск концентрации"

        msg = (
            f"{head}\n\n"
            f"📌 {title_html}\n"
            f"🎯 Исход: <b>{outcome}</b> @ {price:.3f} (~{prob}){hours_line}\n"
            + (f"{whale_line}\n" if whale_line else "")
            + "━━━━━━━━━━━━━━━━━\n"
            f"💵 Бот вложил: <b>${invested:.2f}{partial_note}</b>\n"
            f"💼 Остаток: <b>${remaining:.2f} pUSD</b>"
            f"{concentration_line}"
            f"{ai_block}"
            f"{link_line}"
        )

    try:
        _tg_send(telegram_id, msg, disable_preview=True)
    except Exception:
        log.exception("notify_failed", telegram_id=telegram_id)
