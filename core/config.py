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

    # App
    app_env: str = "development"
    webhook_base_url: str

    # ── Strategy: whale-tracking on fast markets ────────────────────────────────
    # Market universe: only watch markets resolving within this window.
    market_max_hours_to_resolve: float = 48.0
    # Skip ultra-fast markets below this (e.g. 5-15 min crypto) — thin & HFT-dominated.
    market_min_hours_to_resolve: float = 0.5
    # Skip illiquid markets (Gamma liquidityNum, in USDC). 0 disables the filter.
    market_min_liquidity_usdc: float = 1000.0
    # Max number of markets (tokens) to subscribe to over WebSocket at once.
    watch_max_markets: int = 300
    # How often (seconds) to rebuild the watched-markets set from Gamma.
    fast_markets_refresh_sec: int = 120
    # Cooldown (seconds) before the same market can produce another signal.
    market_signal_cooldown_sec: int = 600

    # ── Dynamic "large buy" detection (liquidity/volume relative) ────────────────
    # Absolute floor: never treat a buy smaller than this (USDC) as a whale.
    dyn_abs_floor_usdc: float = 100.0
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
    min_entry_price: float = 0.05
    # Skip if the market taker fee exceeds this (basis points).
    fee_bps_max: float = 500.0
    # Require at least this much fillable ask depth (USDC) to copy into.
    dyn_min_book_depth_usdc: float = 50.0

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
    # Hybrid exit: hold to resolution when close to resolve, TP/SL otherwise.
    # TP/SL only applies when the market still has at least this many hours left.
    tp_sl_min_hours: float = 6.0
    # Take-profit / stop-loss as fractional P&L on the position (0.25 = +25%).
    take_profit_pct: float = 0.25
    stop_loss_pct: float = 0.40
    # Wider slippage when exiting (books thin out near resolution).
    exit_slippage_pct: float = 0.03
    # Portfolio cap: max simultaneous open positions per user.
    max_open_positions: int = 10
    # How often (seconds) to sync positions, evaluate TP/SL and detect resolution.
    positions_sync_sec: int = 60
    # Only emit win/loss notices for settlements newer than this lookback (anti-spam
    # on restart). Settlements older than this are considered already handled.
    settlement_lookback_sec: int = 7200
    # On-chain redemption after resolution. OFF by default — requires verifying the
    # V2/pUSD collateral-adapter flow on a live wallet before enabling.
    auto_redeem_enabled: bool = False

    # ── Legacy donor-copy / REST-scan knobs (optional) ──────────────────────────
    whale_min_usdc: float = 5000.0
    scan_trades_limit: int = 100
    min_trade_size_usdc: float = 5.0
    min_market_hours_to_close: float = 0.0

    # ── AI ───────────────────────────────────────────────────────────────────────
    # Risk score (1-10) at or above which the user gets a HIGH-RISK warning.
    ai_risk_warn_threshold: int = 7
    # If True, AI risk >= warn threshold BLOCKS the auto-copy entry.
    # Default False: per product spec the bot enters first, AI sends its analysis after.
    ai_block_enabled: bool = False


settings = Settings()  # type: ignore[call-arg]
