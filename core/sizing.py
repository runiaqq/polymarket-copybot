"""
Blueprint 3 — Fractional Kelly position sizing (pure, no I/O).

Entry point: ``kelly_stake(p, score, consensus, equity, free_pusd, cfg) -> float``

Design rationale (from CURSOR.md §4 Blueprint 3):
- We cannot observe the true win probability q directly.  Instead we build a
  *small, bounded* edge on top of the market price using the tracked wallet's
  track record, shrunk with a Beta prior to kill small-sample noise.
- Full Kelly overbets with an estimated edge; we use quarter-Kelly (kelly_lambda)
  and let hard caps dominate.

Blueprint 13.1: mode selection is the CALLER's responsibility.  ``kelly_stake``
is pure math — it never checks ``cfg.sizing_mode``.  Return 0.0 means "no edge;
do not bet".  The caller must skip the trade, not fall back to a fixed cap.

Blueprint 14.A: prod logs showed a near-constant additive edge (wallet trust does
not vary with market price), which makes the undamped fraction f_kelly = edge/(1-p)
diverge as p->1 — Kelly auto-maxes the bet on expensive favorites (penny-collecting,
high tail-risk) regardless of how thin the edge actually is. ``_damp_edge`` scales
the edge by (1-p)^gamma before dividing, so a fixed wallet-trust edge no longer
blows up at high prices; gamma=0 reproduces the legacy (undamped) behaviour.
"""

import structlog

log = structlog.get_logger(__name__)


def _shrunk_winrate(wins: int, n: int, prior: float) -> float:
    """Beta-shrunk win rate toward 0.5.  α=β=prior pulls small samples to 0.5."""
    return (wins + prior) / (n + 2 * prior)


def _damp_edge(raw_edge: float, p: float, gamma: float) -> float:
    """Damp the estimated edge so f_kelly = damped_edge/(1-p) does not diverge as p->1.

    gamma=0.0 -> damped_edge == raw_edge (legacy undamped behaviour).
    gamma=1.0 -> f_kelly == raw_edge (flat in price, fully cancels the 1/(1-p) blow-up).
    """
    if raw_edge <= 0 or p <= 0 or p >= 1:
        return 0.0
    return raw_edge * (1.0 - p) ** gamma


def kelly_stake(
    p: float,
    score: dict | None,
    consensus: int,
    equity: float,
    free_pusd: float,
    cfg,
) -> float:
    """
    Compute the Kelly-derived position size (USDC) for a single BUY signal.

    Parameters
    ----------
    p           Entry price of the YES/NO share (0–1); used as market probability.
    score       Wallet score dict from ``core.wallet_score.score_wallet``
                (fields: wins, resolved_count, winrate) or None if unscored.
    consensus   Number of distinct tracked wallets backing this outcome.
    equity      Total equity (free pUSD + open-position value) in USDC.
    free_pusd   Available pUSD in the deposit wallet right now.
    cfg         ``core.config.settings`` (or any object with the Kelly knobs).

    Returns
    -------
    float  Recommended stake in USDC, already clamped by max_risk_per_trade and
           bounded to [0, free_pusd].  Caller must still apply depth / user caps.
           Returns 0.0 when there is no measurable edge (q_hat <= p) — the caller
           must skip the trade entirely; 0.0 never means "fall back to fixed cap".
    """
    if equity <= 0 or free_pusd <= 0:
        return 0.0

    p = float(p)
    if p <= 0 or p >= 1:
        return 0.0

    # ── Edge estimation ───────────────────────────────────────────────────────
    if score and score.get("resolved_count", 0) > 0:
        n = int(score["resolved_count"])
        # Reconstruct wins from winrate * resolved_count (rounded).
        wins = round(float(score.get("winrate", 0)) * n)
        w_hat = _shrunk_winrate(wins, n, cfg.kelly_prior_strength)
        # Convert track record into a bounded trust factor in [0, 1].
        quality = max(0.0, min(1.0, (w_hat - 0.5) * 2))
    else:
        # No track record — use half the base edge as minimum trust.
        quality = 0.5

    consensus_mult = min(1.0 + 0.25 * (max(1, int(consensus)) - 1), 1.5)
    edge_hat = min(cfg.kelly_base_edge * quality * consensus_mult, cfg.kelly_edge_cap)
    q_hat = min(p + edge_hat, 0.99)

    log.debug(
        "kelly_edge",
        p=round(p, 4),
        q_hat=round(q_hat, 4),
        edge_hat=round(edge_hat, 4),
        quality=round(quality, 3),
        consensus=consensus,
    )

    # ── Kelly fraction ────────────────────────────────────────────────────────
    # Binary market: net odds b = (1-p)/p.  f* = (q-p)/(1-p).
    if q_hat <= p:
        return 0.0  # no edge → no bet

    # BP14.A: damp the edge before dividing by (1-p) so a flat wallet-trust edge
    # cannot auto-max the bet on expensive favorites.  q_hat itself (our belief
    # about the true probability) is left undamped — only the risk-sizing fraction
    # is adjusted.
    raw_edge = q_hat - p
    edge_eff = _damp_edge(raw_edge, p, cfg.kelly_edge_damping_gamma)
    f_kelly = edge_eff / (1.0 - p)
    f = cfg.kelly_lambda * f_kelly
    f = min(f, cfg.max_risk_per_trade)

    stake = f * equity
    stake = min(stake, free_pusd)
    stake = max(stake, 0.0)

    log.info(
        "kelly_stake",
        p=round(p, 4),
        q_hat=round(q_hat, 4),
        raw_edge=round(raw_edge, 4),
        edge_eff=round(edge_eff, 4),
        f_kelly=round(f_kelly, 4),
        f_final=round(f, 4),
        equity=round(equity, 2),
        stake=round(stake, 2),
    )
    return round(stake, 4)
