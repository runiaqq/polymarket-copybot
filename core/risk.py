"""
Blueprint 4 — Tail-risk portfolio controls (pure, no I/O).

Entry point: ``check_risk_gates(user, signal, stake, open_positions, cfg) -> RiskDecision``

Four gates evaluated cheapest-first (§4 Blueprint 4):
  1. Aggregate exposure cap  — total deployed capital ≤ max_portfolio_exposure_pct of equity
  2. Per-event correlation cap — exposure in same event ≤ max_event_exposure_pct of equity
  3. Drawdown circuit breaker — equity < (1 - max_drawdown_pct) × HWM → pause
  4. Daily loss limit — realized losses in trailing 24 h ≥ daily_loss_limit_pct × equity

Gates 3 & 4 need DB reads — they are called from the pre-trade path in execute_copy_trade
with data already loaded by the caller to keep this module pure and testable.

Blueprint 7 — Gates 1 & 2 clamp instead of hard-block on small balances:
  - headroom ≥ stake → allow unchanged.
  - exchange_min ≤ headroom < stake → clamp max_stake=headroom (enter smaller, no warning).
  - headroom < exchange_min AND equity < recommended → enter at exchange_min, warn=concentration_over_60.
  - headroom < exchange_min AND equity ≥ recommended → block with notification (funded but fully deployed).
  Gates 3 & 4 (loss-breakers) are unchanged — they must still pause copying.

Blueprint 8 — total_equity() centralises the equity definition for the drawdown
  breaker, HWM, and exposure gates.  In "cost_basis" mode (default) open positions
  are valued at their entry cost so that the act of opening a trade is capital-neutral
  and does not fabricate phantom drawdown.
"""

from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


def total_equity(
    free_pusd: float,
    open_positions: list[dict],
    ledger_cost_by_token: dict[str, float],
    mode: str = "cost_basis",
) -> float:
    """Authoritative equity for drawdown breaker, HWM, and exposure gates.

    Parameters
    ----------
    free_pusd            Liquid pUSD on the deposit wallet.
    open_positions       Live positions list from ``core.polymarket.get_positions``.
    ledger_cost_by_token {token_id: size_usdc} from ``get_open_trades_cost`` (may be empty).
    mode                 "cost_basis" (default) or "mark" (legacy rollback).

    Returns
    -------
    float  Equity value suitable for drawdown / HWM calculations.
    """
    if mode == "mark":
        open_val = sum(
            float(p.get("current_value") or 0)
            for p in open_positions if p.get("shares", 0) > 0
        )
        return free_pusd + open_val

    # cost_basis: price each open position at its entry cost in priority order:
    #   1) copy_trades.size_usdc for the matching open trade (true filled cost)
    #   2) shares × avg_price  (Data-API cost-basis fallback)
    #   3) current_value       (last-resort mark, only when no cost basis known)
    open_cost = 0.0
    for p in open_positions:
        if p.get("shares", 0) <= 0:
            continue
        token_id = p.get("token_id") or ""
        if token_id and token_id in ledger_cost_by_token:
            open_cost += ledger_cost_by_token[token_id]
        elif p.get("shares") and p.get("avg_price"):
            open_cost += float(p["shares"]) * float(p["avg_price"])
        else:
            open_cost += float(p.get("current_value") or 0)
    return free_pusd + open_cost


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""
    gate: str = ""
    # BP7: when set, the caller must clamp the stake to this value before placing the order.
    # None means use the original stake unchanged.
    max_stake: float | None = None
    # BP7: when set, append this warning key to the trade success notification (not a separate message).
    warn: str | None = None


def check_risk_gates(
    signal: dict,
    stake: float,
    open_positions: list[dict],
    equity: float,
    equity_hwm: float,
    daily_pnl: float,
    cfg,
    ledger_cost_by_token: dict[str, float] | None = None,
) -> RiskDecision:
    """
    Evaluate all four tail-risk gates before placing a BUY order.

    Parameters
    ----------
    signal               Signal dict (needs ``event_slug`` or ``market_id`` for gate 2).
    stake                Proposed USDC stake (after Kelly / depth caps).
    open_positions       Live positions list from ``core.polymarket.get_positions``.
    equity               Current user equity (cost-basis or mark, pre-computed by caller).
    equity_hwm           Per-user high-water mark from the DB.
    daily_pnl            Sum of realized_pnl for the trailing 24 h (from DB).
    cfg                  ``core.config.settings``.
    ledger_cost_by_token {token_id: size_usdc} for open trades (BP8 cost-basis exposure).

    Returns
    -------
    RiskDecision  ``.allowed=True`` when all gates pass.
    """
    if equity <= 0 or stake <= 0:
        return RiskDecision(allowed=True)

    cost_map = ledger_cost_by_token or {}

    # ── Gate 1: aggregate exposure cap ───────────────────────────────────────
    # BP8: use cost-basis value for open exposure so partially-marked-down positions
    # don't inflate or deflate the headroom calculation.
    def _position_cost(p: dict) -> float:
        tid = p.get("token_id") or ""
        if tid and tid in cost_map:
            return cost_map[tid]
        if p.get("shares") and p.get("avg_price"):
            return float(p["shares"]) * float(p["avg_price"])
        return float(p.get("current_value") or p.get("size") or 0)

    open_exposure = sum(_position_cost(p) for p in open_positions if p.get("shares", 0) > 0)
    exposure_headroom = cfg.max_portfolio_exposure_pct * equity - open_exposure
    if (open_exposure + stake) > cfg.max_portfolio_exposure_pct * equity:
        exchange_min = cfg.exchange_min_order_usdc
        log.info(
            "risk_gate_exposure",
            gate=1,
            open_exposure=round(open_exposure, 2),
            stake=round(stake, 2),
            equity=round(equity, 2),
            headroom=round(exposure_headroom, 2),
            limit_pct=cfg.max_portfolio_exposure_pct,
        )
        if exposure_headroom >= exchange_min:
            # Clamp: enter smaller, still within cap — no user-facing warning.
            log.info("risk_gate_exposure_clamp", max_stake=round(exposure_headroom, 2),
                     user_equity=round(equity, 2))
            return RiskDecision(allowed=True, gate="exposure_cap",
                                max_stake=exposure_headroom)
        elif equity < cfg.recommended_min_balance_usdc:
            # Small balance: cap is mathematically incompatible with the platform minimum.
            # Enter at the minimum anyway and show a one-line concentration note in the
            # trade notification (not a separate message).
            log.info("risk_gate_exposure_small_balance_override",
                     equity=round(equity, 2), exchange_min=exchange_min)
            return RiskDecision(allowed=True, gate="exposure_cap",
                                max_stake=exchange_min,
                                warn="concentration_over_60")
        else:
            # Funded account that is legitimately fully deployed — hard block with notification.
            return RiskDecision(
                allowed=False,
                reason=(
                    f"Открытые позиции ({open_exposure:.2f}$) + ставка ({stake:.2f}$) "
                    f"превышают {cfg.max_portfolio_exposure_pct*100:.0f}% капитала."
                ),
                gate="exposure_cap",
            )

    # ── Gate 2: per-event correlation cap ────────────────────────────────────
    event_slug = signal.get("event_slug") or signal.get("market_id") or ""
    if event_slug:
        event_exposure = sum(
            _position_cost(p)
            for p in open_positions
            if p.get("shares", 0) > 0 and (
                p.get("event_slug") == event_slug or p.get("market_id") == event_slug
            )
        )
        event_headroom = cfg.max_event_exposure_pct * equity - event_exposure
        if (event_exposure + stake) > cfg.max_event_exposure_pct * equity:
            exchange_min = cfg.exchange_min_order_usdc
            log.info(
                "risk_gate_event",
                gate=2,
                event=event_slug[:20],
                event_exposure=round(event_exposure, 2),
                stake=round(stake, 2),
                equity=round(equity, 2),
                headroom=round(event_headroom, 2),
                limit_pct=cfg.max_event_exposure_pct,
            )
            if event_headroom >= exchange_min:
                # Clamp to event headroom — enter smaller, still within the event cap.
                log.info("risk_gate_event_clamp", max_stake=round(event_headroom, 2),
                         event=event_slug[:20])
                return RiskDecision(allowed=True, gate="event_cap",
                                    max_stake=event_headroom)
            elif equity < cfg.recommended_min_balance_usdc:
                # Small balance: event cap incompatible with platform minimum — override.
                log.info("risk_gate_event_small_balance_override",
                         equity=round(equity, 2), exchange_min=exchange_min)
                return RiskDecision(allowed=True, gate="event_cap",
                                    max_stake=exchange_min,
                                    warn="concentration_over_60")
            else:
                # Funded account genuinely over the event cap — block with notification.
                return RiskDecision(
                    allowed=False,
                    reason=(
                        f"Экспозиция по событию ({event_exposure:.2f}$) + ставка ({stake:.2f}$) "
                        f"превышают {cfg.max_event_exposure_pct*100:.0f}% капитала."
                    ),
                    gate="event_cap",
                )

    # ── Gate 3: drawdown circuit breaker ─────────────────────────────────────
    hwm = max(equity_hwm, equity)  # refresh HWM if equity is a new peak
    if hwm > 0:
        drawdown = (hwm - equity) / hwm
        if drawdown >= cfg.max_drawdown_pct:
            log.info(
                "risk_gate_drawdown",
                gate=3,
                equity=round(equity, 2),
                hwm=round(hwm, 2),
                drawdown=round(drawdown, 4),
                limit=cfg.max_drawdown_pct,
            )
            return RiskDecision(
                allowed=False,
                reason=(
                    f"Просадка {drawdown*100:.1f}% от пика ({hwm:.2f}$) "
                    f"превышает лимит {cfg.max_drawdown_pct*100:.0f}%. "
                    "Копирование приостановлено."
                ),
                gate="drawdown",
            )

    # ── Gate 4: daily loss limit ──────────────────────────────────────────────
    if daily_pnl <= -(cfg.daily_loss_limit_pct * equity):
        log.info(
            "risk_gate_daily_loss",
            gate=4,
            daily_pnl=round(daily_pnl, 2),
            equity=round(equity, 2),
            limit_pct=cfg.daily_loss_limit_pct,
        )
        return RiskDecision(
            allowed=False,
            reason=(
                f"Дневной убыток {abs(daily_pnl):.2f}$ превышает лимит "
                f"{cfg.daily_loss_limit_pct*100:.0f}% капитала. "
                "Копирование до следующего UTC-дня."
            ),
            gate="daily_loss",
        )

    return RiskDecision(allowed=True)
