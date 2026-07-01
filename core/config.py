from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram
    telegram_bot_token: str
    telegram_webhook_secret: str
    admin_telegram_id: int

    # Admin bot (optional second bot for subscription management).
    # Leave blank to disable — the main bot works without it.
    telegram_admin_bot_token: str = ""
    telegram_admin_webhook_secret: str = ""

    # Supabase / DB
    supabase_url: str
    supabase_service_key: str
    database_url: str

    # Redis
    redis_url: str

    # Wallet encryption (Fernet key — generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    encryption_key: str

    # Privy (legacy — kept for reference, not used for new wallets)
    privy_app_id: str = ""
    privy_app_secret: str = ""

    # OpenAI
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"

    # Polymarket
    polymarket_chain_id: int = 137
    polymarket_clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/trade"
    polymarket_clob_rest_url: str = "https://clob.polymarket.com"

    # Alchemy
    alchemy_api_key: str
    polygon_rpc_url: str

    # Polymarket V2 relayer / deposit wallets (auto-copy). Builder creds (3-part HMAC)
    # are required by the Python relayer SDK; the 2-part relayer api key is stored too.
    relayer_url: str = "https://relayer-v2.polymarket.com/"
    builder_api_key: str = ""
    builder_secret: str = ""
    builder_passphrase: str = ""
    relayer_api_key: str = ""
    relayer_api_key_address: str = ""

    # App
    app_env: str = "development"
    webhook_base_url: str = ""

    # ── Strategy: whale-tracking on fast markets ────────────────────────────────
    # Market universe: only watch markets resolving within this window.
    # 72h covers 1-3 day markets where most directional whales operate.
    market_max_hours_to_resolve: float = 72.0
    # Skip ultra-fast markets below this (e.g. 5-15 min crypto) — thin & HFT-dominated.
    market_min_hours_to_resolve: float = 0.5
    # Skip illiquid markets (Gamma liquidityNum, in USDC). 0 disables the filter.
    market_min_liquidity_usdc: float = 2000.0
    # Max number of markets (tokens) to subscribe to over WebSocket at once.
    watch_max_markets: int = 300
    # How often (seconds) to rebuild the watched-markets set from Gamma.
    fast_markets_refresh_sec: int = 120
    # Cooldown (seconds) before the same market can produce another signal.
    market_signal_cooldown_sec: int = 1800

    # ── Dynamic "large buy" detection (liquidity/volume relative) ────────────────
    # Absolute floor: never treat a buy smaller than this (USDC) as a whale.
    # $1000 cuts the noise of routine trades; only real whales pass.
    dyn_abs_floor_usdc: float = 1000.0
    # Trade must be >= this fraction of the ask liquidity within the slippage band.
    dyn_rel_depth: float = 0.5
    # Trade must be >= this multiple of the recent trade-size p90 for the market.
    dyn_rel_vol: float = 3.0
    # Lookback window (seconds) for recent-trade statistics.
    recent_trade_window_sec: int = 600
    # Minimum recent-trade samples before the volume-relative rule is trusted.
    recent_trade_min_samples: int = 5

    # ── Break-even guards ────────────────────────────────────────────────────────
    # Skip markets whose spread exceeds this fraction of mid price.
    max_spread_pct: float = 0.03
    # Don't enter above/below these prices (little upside / likely already resolved).
    max_entry_price: float = 0.95
    min_entry_price: float = 0.40
    # Skip if the market taker fee exceeds this (basis points).
    fee_bps_max: float = 500.0
    # Require at least this much fillable ask depth (USDC) to copy into.
    # $750 ensures we can both enter AND exit without being stuck in a thin book.
    dyn_min_book_depth_usdc: float = 750.0

    # ── Copy sizing ──────────────────────────────────────────────────────────────
    # Scale applied to the whale size before other caps.
    copy_scale: float = 1.0
    # Never take more than this fraction of the fillable depth within slippage.
    book_safe_frac: float = 0.25
    # Slippage tolerance for copy entries (e.g. 0.02 = pay up to 2% worse price).
    order_slippage_pct: float = 0.02

    # ── WebSocket (real-time market data) ────────────────────────────────────────
    ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ws_heartbeat_sec: int = 10
    ws_refresh_markets_sec: int = 120

    # ── Exits / position management (Phase 3) ────────────────────────────────────
    # ── Exit strategy: HOLD TO RESOLUTION ───────────────────────────────────────
    # Binary prediction markets pay $1/share on win or $0 on loss.
    # Selling early almost always hurts EV: you cap upside AND crystallise losses
    # on positions that might still resolve in your favour.
    #
    # Primary strategy: hold all positions to resolution.
    # Single exception: "hard stop" — exit if the MARKET itself prices the
    # outcome at near-zero (< hard_stop_abs_price), meaning it has effectively
    # been ruled out. Recycles capital instead of waiting for a $0 resolution.
    #
    # Percentage TP/SL are disabled (set to unreachable values).
    take_profit_pct: float = 99.0   # disabled — hold to resolution
    stop_loss_pct: float = 99.0     # disabled — use delta_drop_stop_pct instead
    # Residual absolute floor (harmless no-op in normal operation — book is empty
    # by the time price hits 0.07, but kept as a last-resort safety net).
    hard_stop_abs_price: float = 0.07
    # tp_sl_min_hours kept for the redeemable / closed-position hold guard only.
    # Delta-Drop intentionally ignores this guard (it was what let losses ride to $0).
    tp_sl_min_hours: float = 4.0
    # Minimum hold time before any automated exit is evaluated.
    position_min_hold_sec: int = 1800

    # ── Blueprint 10 — Delta-Drop stop-loss ──────────────────────────────────────
    # Exit when best_bid (live CLOB) drops X from entry. Dollar risk = X * size,
    # independent of entry price — the "dumb, robust" stop.
    # X = 0.30 approved by PO 2026-06-30 (data-informed: covers real loss cases,
    # wide enough to avoid whipsawing winners).
    delta_drop_stop_pct: float = 0.30
    # Ignore the first N seconds after entry (avoids tick-level entry whipsaw).
    # Blueprint 17: raised from 600 → 900s (15 min), anchored to created_at in DB.
    delta_drop_min_hold_sec: int = 900

    # ── Blueprint 17 — Spread-Trap hardening (Layers 1–4) ────────────────────────
    # Layer 2 — Spread veto: if (ask-bid)/mid > this, the book is too thin to
    # trust as a price signal — skip the stop this cycle.
    max_spread_for_stop_pct: float = 0.08
    # Layer 1 — Use mid price ((bid+ask)/2) instead of best_bid for drop math.
    # Mid is spread-insensitive; a hollow bid with a sane ask no longer fabricates
    # a drop.  Falls back to best_bid when best_ask is missing/zero.
    delta_drop_use_mid: bool = True
    # Layer 4 — Persistence / debounce: require the drop to breach the threshold
    # this many consecutive polls before closing.  A single hollow-book snapshot
    # never triggers; the counter resets on any non-breaching poll.
    delta_drop_confirm_ticks: int = 2
    # Layer 3 — Optional minimum bid notional (USDC) before trusting the bid.
    # 0 = disabled (opt-in).
    min_bid_notional_usdc: float = 0.0
    # Emit one position_mark log line per open position per sync cycle.
    # Use to calibrate delta_drop_stop_pct from real drawdown after ~2 weeks.
    log_position_marks: bool = True
    # Wider slippage when exiting (books thin out near resolution).
    exit_slippage_pct: float = 0.03
    # Portfolio cap: max simultaneous open positions per user.
    max_open_positions: int = 15
    # How often (seconds) to sync positions, evaluate TP/SL and detect resolution.
    positions_sync_sec: int = 60
    # Only emit win/loss notices for settlements newer than this lookback (anti-spam
    # on restart). Settlements older than this are considered already handled.
    settlement_lookback_sec: int = 7200
    # On-chain redemption after resolution: converts winning outcome tokens back to
    # pUSD on the deposit wallet (gasless via relayer). When ON, the bot credits
    # winnings automatically; when OFF, the user must claim on Polymarket manually.
    auto_redeem_enabled: bool = True

    # ── Legacy donor-copy / REST-scan knobs (optional) ──────────────────────────
    whale_min_usdc: float = 5000.0
    scan_trades_limit: int = 100
    min_trade_size_usdc: float = 5.0
    min_market_hours_to_close: float = 0.0

    # ── Telegram delivery mode ─────────────────────────────────────────────────
    # True  = polling (no domain/HTTPS needed; good for VPS without a domain)
    # False = webhook (default; needs WEBHOOK_BASE_URL + HTTPS)
    use_polling: bool = False

    # ── Product mode ───────────────────────────────────────────────────────────
    # False = signals only (detect + AI + link, user trades on Polymarket themselves).
    # True  = custodial auto-copy (requires Builder Program + deposit-wallet rework).
    auto_copy_enabled: bool = False

    # ── Model B: copy a curated whitelist of profitable wallets ─────────────────
    # How often to poll each tracked wallet's recent trades (seconds).
    tracked_poll_sec: float = 15.0
    # Ignore a tracked wallet's trade older than this (avoid copying stale entries).
    # 2 hours gives us enough history to catch sliced entries and survive restarts.
    # The in-memory + DB dedup prevents double-copying within reentry_hours window.
    tracked_max_trade_age_sec: int = 7200
    # Window (hours) over which we count distinct tracked wallets for consensus.
    consensus_window_hours: int = 24
    # How many recent fills to pull per wallet (a sliced entry can be dozens of fills).
    tracked_fetch_limit: int = 50
    # Aggregate USDC a wallet must buy in ONE market+outcome (summed across sliced
    # fills within the freshness window) before we copy. Filters dust, captures
    # order-slicing whales that build a big position with many tiny buys.
    tracked_min_copy_usdc: float = 50.0
    # Don't re-copy the same wallet→market+outcome again within this window
    # (one entry per burst, not one per fill / per poll cycle).
    tracked_reentry_hours: int = 12

    # ── Wallet track-record filter (validate the edge before enforcing) ──────────
    # off     = ignore the buyer's history entirely
    # observe = resolve the buyer, score their P&L, LOG it on the signal, never block
    # enforce = only copy when the buyer's history passes the thresholds below
    wallet_filter_mode: str = "observe"
    # Minimum resolved markets before a wallet's P&L is trusted (avoid lucky-gambler noise).
    wallet_min_resolved: int = 20
    # Required realized P&L (USDC) of the buyer to copy (enforce mode).
    wallet_min_realized_pnl: float = 0.0
    # Cache a wallet's score for this long (seconds) to avoid refetching per signal.
    wallet_score_ttl_sec: int = 3600
    # Retries/delay to resolve the buyer via the Data API (handles indexing lag).
    wallet_resolve_retries: int = 4
    wallet_resolve_delay_sec: float = 1.0
    # Whitelist discovery: minimum profit/volume ratio to qualify as a directional
    # trader. Market makers/churners earn big absolute profit on huge volume (ratio
    # ~1-4%); directional bettors clear 10%+. Validated: catches skk1ch/swisstony
    # (MMs, ~4%) while keeping mintblade/fishalive/weatherman12 (14-68%).
    discovery_min_profit_volume_ratio: float = 0.15
    # Max average trades per day (computed over the activity window).
    # MMs place 30-200 orders/day; real whales rarely exceed 20.
    discovery_max_trades_per_day: float = 20.0
    # Min average trade size (USDC). MMs scalp with $50-500 orders;
    # copy-worthy directional whales bet $300+ per order on average.
    discovery_min_avg_trade_size: float = 300.0
    # Directionality Score: D = |V_yes - V_no| / (V_yes + V_no) per market,
    # averaged across all markets the wallet bought into.
    # D = 1.0 → fully directional (only bought one outcome per market).
    # D = 0.0 → perfectly hedged (equal YES/NO volume) → classic MM.
    # MMs score 0.0-0.3; real directional traders typically 0.7-1.0.
    discovery_min_directionality: float = 0.5
    # Scattershot/hedge filter: max distinct markets a wallet may buy within a
    # single event before it's flagged a gambler (e.g. betting every exact
    # football score 1:0, 2:1, ...). 3+ outcomes in one event → excluded/removed.
    discovery_max_event_outcomes: int = 3

    # ── AI ───────────────────────────────────────────────────────────────────────
    # Risk score (1-10) at or above which the user gets a HIGH-RISK warning.
    ai_risk_warn_threshold: int = 7
    # If True, AI risk >= warn threshold BLOCKS the auto-copy entry.
    # Default False: per product spec the bot enters first, AI sends its analysis after.
    ai_block_enabled: bool = False

    # ── Blueprint 2: slice aggregation (Redis accumulator) ────────────────────
    # Quiet period after last fill before firing a signal (whale still slicing?).
    slice_quiet_period_sec: int = 45
    # Hard max window: fire even if whale is still slicing after this time.
    slice_max_window_sec: int = 180
    # Conviction fraction: threshold = max(abs_floor, conviction_frac * whale_avg_size).
    slice_conviction_frac: float = 0.5
    # Low-balance alert throttle: at most one nudge per user per this interval.
    lowbal_alert_throttle_sec: int = 21600   # 6 h

    # ── Blueprint 3: fractional Kelly position sizing ─────────────────────────
    # "fixed" = legacy flat cap; "kelly" = risk-based fractional Kelly.
    sizing_mode: str = "fixed"
    # Fraction of full Kelly to bet (0.25 = quarter-Kelly; keeps variance low).
    kelly_lambda: float = 0.25
    # Edge attributed to a fully-trusted single wallet on top of market price.
    kelly_base_edge: float = 0.03
    # Absolute edge ceiling (prevents overbetting on exceptional wallets).
    kelly_edge_cap: float = 0.06
    # Beta-prior strength for winrate shrinkage (α=β=prior → pulls toward 0.5 when n small).
    kelly_prior_strength: float = 10.0
    # Blueprint 14.A: damping exponent applied to edge_hat before dividing by (1-p).
    # Without damping, f_kelly = edge_hat/(1-p) diverges as p->1, so any fixed wallet
    # edge auto-maxes the bet on expensive favorites (penny-collecting, high tail-risk) —
    # this was the root cause of Kelly stakes clustering at the 5% cap in prod.
    # gamma=1.0 fully cancels the 1/(1-p) blow-up (f_kelly == edge_hat, flat in price);
    # gamma=0.0 reproduces the legacy (undamped) behaviour.
    #
    # DEFAULT IS 0.0 (OFF) — strategy-changing, money-moving knob (§5.6).  WARNING:
    # at gamma=1.0, with the current kelly_edge_cap=0.06 and kelly_lambda=0.25, the
    # MAXIMUM possible stake (best wallet, max consensus) is lambda*edge_cap*equity —
    # Kelly cannot clear exchange_min_order_usdc ($5) until equity exceeds roughly
    # exchange_min/(kelly_lambda*kelly_edge_cap) ≈ $333.  Below that, gamma>0 makes
    # Kelly skip EVERY signal.  Before raising this above 0, either grow equity past
    # that threshold or raise kelly_base_edge/kelly_edge_cap to compensate, then
    # validate on the kelly_stake logs that real trades still clear the minimum.
    kelly_edge_damping_gamma: float = 0.0
    # Hard risk cap per trade as a fraction of equity.
    max_risk_per_trade: float = 0.05
    # Polymarket platform minimum order size.
    # The CLOB rejects orders below $5 USDC notional.
    exchange_min_order_usdc: float = 5.0
    # SOFT recommendation only — below this we warn but do NOT block.
    # A $3 wallet trades at the $1 platform minimum; it is only skipped when it
    # cannot afford even that minimum (free_pusd < exchange_min_order_usdc).
    recommended_min_balance_usdc: float = 100.0
    # Target concurrent open positions for min-balance floor calculation.
    n_target_positions: int = 5

    # ── Blueprint 4: tail-risk portfolio controls ─────────────────────────────
    # Max fraction of equity that may be deployed in open positions simultaneously.
    max_portfolio_exposure_pct: float = 0.60
    # Max fraction of equity in positions sharing the same event (correlation cap).
    max_event_exposure_pct: float = 0.15
    # Drawdown circuit breaker: pause copying when equity falls this far from HWM.
    max_drawdown_pct: float = 0.25
    # How long (seconds) copying stays paused after a drawdown breaker trip.
    drawdown_cooldown_sec: int = 86400    # 24 h
    # Daily loss limit as a fraction of start-of-day equity.
    daily_loss_limit_pct: float = 0.10

    # ── Blueprint 5: time-to-resolution display ───────────────────────────────
    # When True, also render the America/New_York wall-clock deadline in
    # notification messages (display only; all internal math stays in UTC).
    show_resolution_in_et: bool = True

    # ── Blueprint 6: dust guard for auto-claim / reconcile ────────────────────
    # Skip claiming and notifying when the on-chain ERC-1155 balance (in shares,
    # i.e. raw_balance / 1e6) is below this floor.  Prevents phantom "$0.01 win"
    # notifications from dust left behind by close_position truncation.
    claim_dust_min_shares: float = 1.0
    # Additional notional floor: also skip when shares * resolve_price < this.
    claim_dust_min_usdc: float = 1.0

    # ── Blueprint 12: withdrawal ───────────────────────────────────────────────
    # Minimum single withdrawal (USDC). Below $1 is dust; platform fees eat it.
    min_withdraw_usdc: float = 1.0

    # ── Blueprint 8: equity accounting + unified per-trade risk cap ────────────
    # Equity definition used by the drawdown breaker / HWM / exposure gates.
    # "cost_basis" = open positions valued at filled entry cost (no phantom drawdown).
    # "mark"       = legacy mark-to-market on curPrice (rollback only).
    drawdown_equity_mode: str = "cost_basis"
    # Apply max_risk_per_trade × equity as a hard ceiling in BOTH fixed and kelly modes.
    # Ensures the worst-case loss of any single binary trade ≤ 5% of equity.
    enforce_risk_per_trade_cap: bool = True
    # Profit-protection trailing cap: cap a single trade's stake so worst-case loss
    # ≤ this fraction of accumulated realized profit above realized_baseline.
    # 0.0 = disabled.  0.25 = no single trade can give back >25% of profit cushion.
    max_trade_loss_vs_profit_pct: float = 0.25


settings = Settings()  # type: ignore[call-arg]
