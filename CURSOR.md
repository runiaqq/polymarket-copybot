# CURSOR.md — PolyMind AI (Polymarket Copy-Trading SaaS)

> **Read this file fully before writing any code.** It is the single source of truth for
> architecture, current state, and **mandatory** safety rules. This system moves **real user
> money** on-chain. A wrong edit can drain a subscriber's deposit. When unsure, STOP and ask —
> do not guess or hallucinate APIs, contract addresses, or DB columns.

Product aliases seen in code: **PolyMind AI** (product name), `PolyMind` (user-facing bot copy),
`Polymarket CopyBot` (FastAPI title). They are the same project.

---

## 1. Project Overview

PolyMind AI is a **commercial subscription (SaaS) copy-trading bot for [Polymarket](https://polymarket.com)**,
operated entirely through **Telegram**.

**What it does, end to end:**
1. A curated whitelist of proven profitable wallets ("master accounts" / "whales") is stored in
   the `tracked_wallets` table.
2. A background worker **polls each tracked wallet's recent on-chain trades** every ~15 seconds.
3. When a tracked wallet makes a fresh **BUY** on a fast-resolving market, the bot generates a
   **signal** and **mirrors that trade onto every active paying subscriber's wallet** with real
   funds, via the Polymarket CLOB V2.
4. Positions are **held to resolution** (binary markets pay $1 or $0). On resolution, winnings are
   **auto-redeemed** on-chain and converted back to tradeable collateral (pUSD).
5. Subscribers pay for access; subscription state gates whether their account is copied.

**Custodial model:** the bot **generates and holds each user's wallet private key** (encrypted).
This is the highest-risk part of the system — see §5.

**Business model:** single subscription tier, time-based expiry, activated by an admin or a
one-time access code.

---

## 2. Current Architecture & Modules

### 2.1 Process topology

Four processes (see `docker-compose.yml`):

| Process | Entrypoint | Role |
|---|---|---|
| **api** | `api/main.py` (uvicorn) | Telegram bots (user + admin), health, webhook/polling |
| **worker** | `worker/entrypoint.py` | Celery worker, **gevent** pool, concurrency 100, queues `trades,ai,periodic` |
| **beat** | `worker/beat.py` | Celery beat scheduler — **MUST be exactly ONE replica** (gevent pool cannot embed `--beat`) |
| **redis** | redis:7 | Celery broker/backend + cross-process dedup guards |

**Data store:** Supabase (Postgres) accessed at runtime via the **supabase client** (`core/db/queries.py`).
`core/db/models.py` (SQLAlchemy) is **schema reference only** — not used at runtime. Schema changes are
manual SQL in `migrations/*.sql` (applied by hand in the Supabase SQL editor).

**Deploy targets:** self-hosted `docker-compose` on a VPS in a **non-geoblocked region** (Polymarket
geoblocks some countries; trading must run where it's allowed). `fly.toml`/`railway.toml` exist for
split hosting but compose is the primary path.

### 2.2 Copy-trading flow (Model B — the PRIMARY strategy)

```
beat → poll_tracked_wallets (every tracked_poll_sec = 15s)
        │  for each tracked wallet:
        │    fetch_donor_recent_trades(addr)            # Polymarket Data API /activity
        │    aggregate sliced BUY fills per (market, outcome)   # one entry = many tiny fills
        │    filters: side==BUY, age < tracked_max_trade_age_sec (2h),
        │             aggregate size >= tracked_min_copy_usdc ($50),
        │             market in get_fast_markets() (resolves within window & liquid),
        │             dedup (in-memory _seen + DB trade_signals lookup, reentry 12h)
        │    insert trade_signals row, compute consensus count
        └──> for EACH active subscriber: execute_copy_trade.delay(uid, signal)   # queue=trades

execute_copy_trade(user_id, signal):                     # worker/tasks/execute_copy.py
    load user; require deposit_wallet_address
    only BUY (SELL/NO skipped — we don't hold the token to sell)
    compute size (see §2.3)
    balance check: ensure deposit wallet has enough pUSD (sweep EOA→DW on demand)
    portfolio guards: skip if already in this market (send consensus boost instead),
                      skip if open positions >= max_open_positions (15)
    ensure CLOB API creds (generate + persist if missing)
    re-check order book (price in band, depth cap)
    insert copy_trades row (idempotency guard against Celery retries)
    place_order(...)  → CLOB V2 marketable FAK BUY, slippage-protected
    confirm fill from on-chain positions; notify user (trade + AI + remaining balance)
```

`scan_markets.py` (global REST whale scanner), `ws_listener.py`/`signals.py` (real-time WS whale
detector), and `poll_donors.py` (donor wallets) are **Model A / legacy**. They are **not on the beat
schedule** and are effectively dormant. Do not extend them without explicit instruction.

### 2.3 Copy sizing — IMPORTANT: FIXED CAP, **not** proportional

The bot does **NOT** size proportionally to the whale's stake. Sizing is a **fixed per-user cap,
then clamped down by liquidity**:

1. Start at `user.max_position_usdc` (default **$25** per position).
2. Clamp by the signal's depth-derived cap (`max_copy_usdc`) if present.
3. Clamp by order-book depth: `book_safe_frac` (**0.25**) of the fillable ask depth within the
   slippage band (`order_slippage_pct` = 2%).
4. Require `>= $1.0`, else skip.

The whale's size is only a **trigger + conviction floor** (`tracked_min_copy_usdc` = $50 aggregate),
**not** a multiplier. `copy_scale` exists in config (default 1.0) but proportional sizing is not the
active model. If you implement proportional sizing, treat it as a **new feature**, gate it behind a
config flag, and never let it bypass the depth/balance caps.

### 2.4 On-chain trading path (Polymarket V2)

- Each user has a generated **EOA** (`users.wallet_address`, key encrypted in `wallet_private_key_enc`).
- Each user trades through a deterministic **deposit wallet** (`users.deposit_wallet_address`) — an
  ERC-1967 proxy owned by the EOA, deployed gaslessly via the **relayer/Builder** (`core/relayer.py`).
- Collateral is **pUSD** (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`). Deposits arrive as USDC/USDC.e
  and are converted (`core/polygon.py`: swap → wrap → sweep into the deposit wallet).
- Orders are signed **POLY_1271** with the deposit wallet as `funder` (`core/clob.py`). Plain EOA
  makers are rejected by V2.
- Contract addresses live in `core/clob.py` (`CTF_EXCHANGE`, `NEG_RISK_CTF_EXCHANGE`,
  `NEG_RISK_ADAPTER`, `CONDITIONAL_TOKENS`, `PUSD_ADDRESS`). **Never hardcode new addresses elsewhere —
  import from `core/clob.py`.**

### 2.5 Exits, resolution & redemption (`worker/tasks/manage_positions.py`)

- Strategy = **hold to resolution**. Percentage TP/SL are disabled.
- Single early exit: **hard stop** when the market prices the held outcome below
  `hard_stop_abs_price` (0.07) and we're not within `tp_sl_min_hours` of resolution.
- `sync_positions` (beat, every 120s) detects resolved positions and:
  - sends win/loss notifications,
  - if won + `auto_redeem_enabled`, dispatches `redeem_position` (queue `trades`).
- `redeem_winnings` (`core/relayer.py`) **auto-detects** market type on-chain (matches the held
  token's `positionId` against candidate collaterals): WrappedCollateral → neg-risk
  (`NegRiskAdapter.redeemPositions`), pUSD/USDC.e/USDC → binary (`ConditionalTokens.redeemPositions`).
  **Do NOT trust the `negativeRisk` flag from the Data API — it is often null/missing.**
- Redeemed funds arrive as **USDC.e**, then `convert_dw_usdce_to_pusd` wraps them to pUSD so they're
  tradeable again. Both steps run gaslessly through the relayer batch.

### 2.6 Whitelist discovery (`core/wallet_discovery.py`)

`discover_quality()` builds/cleans the `tracked_wallets` whitelist from Polymarket leaderboards and
filters out market makers / arbitrageurs / gamblers using:
- profit/volume ratio (`discovery_min_profit_volume_ratio` 0.15),
- trade density (`discovery_max_trades_per_day` 20),
- average trade size (`discovery_min_avg_trade_size` $300),
- **directionality score** D = |V_yes − V_no| / (V_yes + V_no), min 0.5,
- **scattershot/hedge** filter: max distinct markets per event (`discovery_max_event_outcomes` 3).

Triggered from the admin bot (`/refresh`, `/top`) and `scripts/seed_quality.py`.

### 2.7 Subscriptions & admin

- Single tier: `users.sub_tier = "active"` with `users.sub_expires_at`. Anyone non-`free` and
  unexpired is "active". `get_active_subscribers()` is the gate for copying.
- Activation: admin `/grant` or `/sub` (by `@username` or numeric id) → `set_subscription(days)`,
  or one-time **access codes** (`access_codes`, redeemed via deep-link). Expiry reminders run every 6h.
- Auth guard on admin commands: `is_admin(telegram_id)` — covers `settings.admin_telegram_id` (super-admin)
  plus any row in `admins` table (multi-admin, invited via one-time codes).
- Two Telegram bots: **user bot** (`api/routers/telegram.py`) and an optional **admin bot**
  (`api/routers/admin_bot.py`, separate token) for subscriptions + whitelist management.
- There is also a Bearer-auth REST admin API (`api/routers/admin.py`).

### 2.8 Key config flags (`core/config.py`)

- `auto_copy_enabled` — master switch for custodial auto-copy (must be ON only where Polymarket is
  not geoblocked). When OFF, beat schedule is empty.
- `use_polling` — Telegram polling vs webhook.
- `auto_redeem_enabled` — on-chain winnings redemption (default ON).
- `wallet_filter_mode` — `off | observe | enforce` for the buyer track-record filter (default `observe`).
- Strategy knobs: market window, dynamic large-buy detection, break-even guards, copy sizing,
  exits, Model-B polling, discovery thresholds, AI risk threshold.

### 2.9 Core module index

| Module | Responsibility |
|---|---|
| `core/config.py` | Pydantic settings from `.env` (all thresholds live here) |
| `core/wallet.py` | EOA generation + Fernet encrypt/decrypt of private keys |
| `core/clob.py` | CLOB V2 client, API creds, `place_order`/`sell_position`, contract addresses |
| `core/relayer.py` | Gasless deposit-wallet deploy/approve/transfer + `redeem_winnings` + USDC.e→pUSD wrap |
| `core/polygon.py` | Balances, transfers, swaps (Uniswap), wrap/unwrap (CollateralOnramp/Offramp) |
| `core/polymarket.py` | Data/Gamma API: fast markets, positions, closed positions, recent trades |
| `core/detector.py` | Pure whale-detection logic (depth/volume significance, break-even) |
| `core/leaderboard.py` | Profit/volume leaderboard fetch + cache |
| `core/wallet_discovery.py` | Quality-trader discovery + MM/arb/gambler filters |
| `core/wallet_score.py` | Score a buyer's track record (observe/enforce filter) |
| `core/cache.py` | Redis one-shot guards (`notify_once`, `claim`) + BP2 accumulator helpers |
| `core/sizing.py` | **[BP3]** Pure fractional Kelly sizing (`kelly_stake`) — no I/O |
| `core/risk.py` | **[BP4]** Pure tail-risk gate evaluator (`check_risk_gates`) — no I/O |
| `core/db/queries.py` | All runtime Supabase reads/writes |

---

## 3. Working Features (stable, running on real funds)

- Custodial wallet lifecycle: EOA generation, encrypted key storage, gasless deposit-wallet deploy +
  approvals, deposit detection, USDC/USDC.e → pUSD conversion and sweep into the deposit wallet.
- **Model B copy-trading**: poll tracked wallets → aggregate sliced fills → dedup → fast-market filter
  → fan-out to all subscribers → CLOB V2 FAK BUY with slippage + depth caps → fill confirmation →
  combined Telegram notification (trade + AI risk + remaining balance).
- Portfolio guards: per-market single-entry (with consensus boost), max open positions, balance checks.
- Position management: hold-to-resolution, hard-stop exit, resolution win/loss detection.
- **On-chain redemption**: auto-detecting binary vs neg-risk redeem, then USDC.e→pUSD wrap (gasless).
- Subscriptions: single tier, admin grant + access codes, expiry reminders, separate admin bot.
- Whitelist discovery with MM/arb/gambler filtering (ratio, density, size, directionality, scattershot).
- AI risk analysis per signal (OpenAI), informational by default (`ai_block_enabled=false`).
- Redis-backed dedup across restarts for notifications and settlements.
- **[BP1] Deterministic on-chain settlement reconciler** (`reconcile_settlements`, beat every 120s):
  reads `payoutDenominator`/`payoutNumerators` directly from the CTF contract, so neg-risk positions
  that disappear from the Data API are settled deterministically. Settlement fields
  (`condition_id`, `token_id`, `outcome_index`, `neg_risk`, `entry_price`, `shares`, `result`,
  `realized_pnl`, `resolved_at`, `redeemed_at`, `redeem_tx`) persisted on `copy_trades`
  (migration 008). `execute_copy_trade` denormalizes these at fill time.
- **[BP2] Redis accumulation tracker with quiet-period debounce**: replaces the in-memory `_seen`
  map with cross-process Redis buckets per `(wallet, cond, token)`. Signal fires once after the
  whale's slicing settles (`slice_quiet_period_sec=45s`) or hits the hard window cap
  (`slice_max_window_sec=180s`). VWAP of the full burst is used as signal price. Dynamic
  conviction threshold: `max(abs_floor, conviction_frac × whale_avg_size)`. Pre-fan-out balance
  gate skips users below `min_balance_usdc` with a throttled nudge; `_notify_low_balance` also
  throttled to ≤1 alert per `lowbal_alert_throttle_sec` (6h). Migration 009 adds
  `tracked_wallets.avg_trade_usdc`.
- **[BP3] Fractional Kelly position sizing** (`core/sizing.py`, `sizing_mode="kelly"`):
  Bayesian-shrunk winrate → bounded edge estimate (`edge_hat ≤ kelly_edge_cap=0.06`) → quarter-Kelly
  (`kelly_lambda=0.25`) → hard cap at `max_risk_per_trade=0.05` of equity. `sizing_mode="fixed"`
  reproduces the legacy flat-cap behaviour for instant rollback. Copying disabled below
  `min_balance_usdc=$100` to avoid dust trades eaten by fees.
- **[BP3.1] Soft minimum-balance + minimum-order fallback**: removed the hard `$100` block.
  Below `recommended_min_balance_usdc=$100` a throttled soft warning is sent and the bot trades
  at the exchange minimum (`exchange_min_order_usdc=$1`). Copying is only skipped when
  `free_pusd < $1` (cannot afford even the platform minimum). `_notify_trading_at_minimum`
  replaces the old hard `below_min_balance` skip.
- **[BP1-GAP] Backfill legacy redemptions** (`backfill_legacy_redemptions`, beat every 600s):
  recovers funds for positions opened before migration 008 (NULL ledger fields). Enumerates
  resolved-won holdings from the Data API and on-chain CTF contract, dispatches `redeem_position`
  for each. Self-healing USDC.e sweep also added to `monitor_deposits` (every 120s).
- **[BP4] Tail-risk portfolio controls** (`core/risk.py`): four pre-trade gates —
  aggregate exposure cap (60% of equity), per-event correlation cap (15% of equity), drawdown
  circuit breaker (25% drawdown from HWM → 24h pause), daily loss limit (10% of equity → pause
  to 00:00 UTC). `equity_hwm` and `copy_paused_until` stored on `users` (migration 010).
  `get_active_subscribers` honors `copy_paused_until`; `sync_positions` refreshes HWM each cycle.
- **[BP5] Correct time-to-resolution parsing** (`core/polymarket.py`): `resolution_dt()` selects
  the authoritative end datetime from market + event fields, prioritising `events[0].endDate`
  (the event boundary) over the per-market `endDate` which is often an understated placeholder.
  Date-only strings are treated as 23:59:59Z (end of day, never midnight). `format_time_left()`
  computes the human-readable countdown fresh at notification send time — never a cached scalar.
  `resolution_iso` is carried on the signal dict; all notification sites (`execute_copy._notify`,
  `ai_filter._call_gpt`, `ai_filter.run_ai_analysis`) call `format_time_left` at send time.
  Migration 012 is not required (signal is an in-memory dict).
- **[BP6] Position state machine & terminal P&L** (`worker/tasks/manage_positions.py`): `close_position`
  now books realized P&L from the actual sale proceeds (`shares_sold × best_bid − entry_cost`) and
  calls `mark_trade_closed` (sets `status='closed'`, `result='closed'`, `redeemed_at=now`), removing
  the row from `get_outstanding_copy_trades` permanently. Defense-in-depth guards added to
  `reconcile_settlements` and `backfill_legacy_redemptions`: terminal-state check
  (`has_terminal_trade`) and on-chain dust check (`ctf_token_balance / 1e6 < claim_dust_min_shares`)
  block phantom re-claims. `redeem_position` applies the same guards plus hydrates the market
  title from `trade_signals` when called from the on-chain reconciler path. `_resolve_user_for_notification`
  hydrates the title from the ledger. New helpers: `get_open_trade_by_token`, `mark_trade_closed`,
  `has_terminal_trade` in `core/db/queries.py`. Migration 012 (`exit_tx` column +
  `(user_id, condition_id)` index) applied (see §6.9).
- **[BP7] Small-balance silent-skip fix: exposure-cap clamping + sizing-mode-aware messaging**
  (`core/risk.py`, `worker/tasks/execute_copy.py`, `worker/celery_app.py`): Eliminated the prod
  silent-skip where BP4's Gate 1 (`exposure_cap`) hard-blocked every trade for sub-$100 balances.
  Gates 1 & 2 (`RiskDecision`) now carry `max_stake` and `warn` fields and clamp instead of blocking:
  if headroom ≥ exchange_min → clamp stake to headroom; if headroom < exchange_min and
  equity < recommended → enter at the platform minimum (`exchange_min_order_usdc`) with
  `warn="concentration_over_60"`; if equity ≥ $100 → still block (funded but fully deployed).
  The concentration warning is appended inline to the existing trade success notification — no
  separate spam message. Soft-limit "$100" warning (`_notify_trading_at_minimum`) is now
  mode-aware: fires only in `kelly` mode (rationale holds for Kelly; in `fixed` mode the user
  set their own size → stay silent). `result_expires=3600` added to Celery to prevent
  `celery-task-meta-*` key explosion in Redis. No new migration required.

- **[BP9] Release-integrity: fail-loud architecture + money-safety win invariant**:
  global PTB error handler on both bots (`add_error_handler`) converts silent dead buttons
  into logged + user-visible fallbacks; boot self-check (`_check_core_imports`) fails the
  container immediately if `core.db` exports are missing; Celery `task_failure` signal
  escalates crashed periodic tasks; `_emit_win_pending` + `_emit_win_retry_failed` replace
  false-success notifications — "✅ Выигрыш зачислен" is only ever sent after the
  `redeemPositions` + `convert_dw_usdce_to_pusd` batch confirms on-chain.

- **[BP14] Signal-Only Mode + Subscription Enforcer** (migration **014**):
  **(A) Signal-Only Mode** — per-user `users.is_signal_only` flag. When `true`,
  `execute_copy_trade` short-circuits **before any wallet/balance/risk/CLOB path**
  and sends a rich manual-trade brief via `_notify_signal_only` (event title,
  concrete outcome, live order-book price + implied probability, whale size /
  fills / consensus, market link) — **zero on-chain / Web3 calls**. The flag gates
  **ENTRY only**: open positions are still synced, exited on TP/SL and redeemed by
  `sync_positions` (which must never skip `is_signal_only` users). `get_active_subscribers`
  includes signal-only users in auto-copy mode regardless of `copy_active`/`wallet_address`,
  and `poll_tracked_wallets` fans signals out to them bypassing the balance gate.
  Toggle UI lives in `/settings` (🤖 Копитрейдинг / 🔔 Только сигналы → `mode_copy`/`mode_signal`).
  **(B) Subscription Enforcer** — `_subscription_guard(user)` in `execute_copy.py` checks
  `is_subscription_active(user)` before copying **or** signalling; expired users are skipped
  and alerted **exactly once** via the DB-backed `users.subscription_notified_expired` flag
  (no per-trade spam). The flag auto-resets to `false` whenever an active subscription is
  observed and on renewal (`set_subscription`). `check_subscription_expiry` shares the same
  flag for its "just expired" branch so the cron and the guard never double-notify.
  New `core/db/queries.py` helpers (re-exported from `core/db/__init__.py`):
  `is_subscription_active`, `set_subscription_notified_expired`, `set_signal_only`.
  **This release replaces a temporary hardcoded signal-only hack (single client by
  Telegram ID) with the production flag-driven feature.** Migration 014 must be applied
  to the live DB before deploying this code.

- **[BP12] Close-handler import fix + withdrawal balance fix & FSM redesign**:
  **(A)** `get_open_trade_by_token` re-exported from `core/db/__init__.py`; boot self-checks
  in `api/main.py` and `worker/celery_app.py` extended with an explicit required-name set so
  the drift regression class fails loud at container start, not at runtime on the money path.
  **(B)** `withdrawable_usdc(db_user)` added to `core/polygon.py` — single source of truth
  (deposit-wallet pUSD + EOA pusd/usdc_e/usdc); `transfer_usdc` now waits for the on-chain
  receipt; `withdraw_funds` task rewritten as fail-loud: pre-flight balance gate, POL gas
  check, per-leg error propagation (no swallowing), Polygonscan link on success. Withdrawal
  FSM in `telegram.py`: stale-state reset on re-entry, real "Доступно" balance from
  `withdrawable_usdc`, pre-flight amount validation, confirm step kept. `min_withdraw_usdc`
  config constant added. No new migration required.

- **[BP17] Spread-Trap stop-loss hardening + Broken-Override state reset** (2026-07-02):
  **(A — Spread Trap)** Four-layer Delta-Drop hardening in `manage_positions.py`: Layer 1 —
  drop computed from mid `(bid+ask)/2` instead of raw best_bid; Layer 2 — spread veto skips
  the stop when `(ask-bid)/mid > max_spread_for_stop_pct (0.08)`, logging
  `stop_skipped_wide_spread`; Layer 3 — optional bid-vs-bid comparison using `entry_bid`
  persisted at fill time; Layer 4 — persistence debounce requires `delta_drop_confirm_ticks
  (2)` consecutive breaching polls, logging `delta_drop_confirming` on unconfirmed ticks.
  Hold-time anchor moved from in-process `_first_seen` dict to `copy_trades.created_at`
  (survives worker restarts); `delta_drop_min_hold_sec` raised 600 → 900 s. Five new config
  knobs in `core/config.py`. Migration `016_entry_bid.sql` adds `copy_trades.entry_bid`.
  **(B — Broken Override)** `risk_override_until timestamptz` added to `users`
  (migration 016). `unlock_drawdown` handler now sets this flag to next 00:00 UTC via
  `set_risk_override_until`. The `_update_hwm_and_check_breakers` monitor returns immediately
  while the flag is active, suppressing both the drawdown and daily-loss breakers. The
  `execute_copy` pre-trade path passes `daily_pnl=0` to `check_risk_gates` while the override
  is active, so gate 4 does not block new entries. Confirmation message updated to state
  "принято до 00:00 UTC". Two new DB helpers (`get_risk_override_until`,
  `set_risk_override_until`) exported from `core/db`.

- **[BP19] Global Stop-Loss Invariant — DB-first effective-entry resolver** (2026-07-03):
  Closed the **Stop-Loss Leak**: `sync_positions` previously read cost basis exclusively from
  the Data-API `avg_price` which is `0` for POLY_1271 proxy wallets (indexing lag), causing
  the `entry_px > 0` Delta-Drop guard to silently skip every poll — positions rode to
  resolution without being stopped. **5-tier fallback chain** implemented in
  `worker/tasks/manage_positions.py` (`_db_entry_prices` fetched once per user per cycle via
  `get_entry_prices_by_token`):
  **Tier 1** — `copy_trades.entry_price` (DB, our true fill cost — preferred for risk path);
  **Tier 2** — Data-API `avg_price` (only when Tier 1 is 0/NULL);
  **Tier 3** — `size_usdc / shares` (ledger-derived, when both are present);
  **Tier 4** — `trade_signals.price` (VWAP entry from the originating signal, via new
  `get_signal_price` helper in `core/db/queries.py`);
  **Tier 5** — hard floor `0.01` + `log.error("stop_no_cost_basis", …)` — a skipped stop
  is **never silent again**.
  `position_mark` log ungated (now fires for every position regardless of API avg) and
  extended with `entry_source` field showing which tier resolved the value.
  Fix 3 (defence-in-depth): `stop_mid_floor_enabled` fires `hard_stop` when `mid <
  hard_stop_abs_price` even before `best_bid` reaches the floor; hollow-book positions
  (`best_bid == 0`) emit `stop_unsellable_hollow_book` (throttled via `notify_once`) for ops
  visibility. Three new config knobs added to `core/config.py`:
  `stop_use_db_entry` (default `True`), `stop_mid_floor_enabled` (default `True`),
  `stop_no_cost_basis_alert` (default `True`). No DB migration required — `entry_price`,
  `size_usdc`, `shares`, `signal_id` already exist (migration 008).

- **[BP20] Redeem-Hang Self-Healing + Win-Notification UX** (2026-07-03):
  Fixed four prod defects; **20.A is money-critical** (real winnings were stuck on-chain
  for up to 7 days due to a stale Redis dedup key, never self-healing):
  **A1 — Short lease TTL**: `once:redeem:{uid}:{cond}` now uses `redeem_lease_sec=900 s`
  (15 min) instead of the 7-day default. The key is also **cleared** (`clear_once`) in
  `redeem_position` failure/skip paths and inside `reconcile_settlements` A3 so the
  reconciler re-attempts on the next cycle instead of waiting a week.
  **A2 — trade_id always passed**: `sync_positions` now calls `get_open_trade_by_condition`
  before dispatching `redeem_position` to pass the real `trade_id` + `entry_cost` (via the
  BP19 5-tier resolver). The `mark_trade_settled` ledger update that was silently skipped
  for the `sync_positions` redeem path now runs on every successful on-chain redeem.
  **A3 — ledger-desync drain**: `reconcile_settlements` detects `won && shares_on_chain==0`
  (tokens already redeemed on-chain, ledger never settled) and calls `mark_trade_settled`
  directly without dispatching a second redeem, clearing the stale key and notifying the user.
  **A4 — loud logs**: every silent `continue` in the redeem/reconcile win branch is replaced
  with throttled `reconcile_redeem_blocked` / `redeem_skipped_reason` / `reconcile_user_not_found`
  log lines so a stuck claim is visible within minutes.
  **B — Outcome fallback**: centralized `resolve_outcome_name(outcome, outcome_index,
  condition_id, signal_id)` in `core/polymarket.py` with 5-tier chain (API → signal DB →
  Gamma outcomes[idx] → Gamma groupItemTitle → Yes/No); applied in all `_emit_*` notifiers
  and `redeem_position`.
  **C — Word-boundary truncation**: `smart_truncate(text, limit=50)` in `core/polymarket.py`
  replaces all scattered `[:N]` hard-slices on title strings throughout notifications.
  **D — Net PnL**: "Выигрыш зачислен" now shows `🏆 Net PnL: +$X.XX$ (выплата − вход)` using
  the BP19 cost-basis; renders `—` when `entry_cost` is unknown (legacy rows).
  New helpers: `clear_once` in `core/cache.py`; `get_open_trade_by_condition` in
  `core/db/queries.py`; `redeem_lease_sec=900` in `core/config.py`. No new DB migration.

- **[BP21] The Great Leak Plug & Risk Revamp** (2026-07-06): closes the three
  stop-loss leaks proven on prod for @sto1ner (id 891787021) and decouples position
  monitoring from the risk pause. **Root-cause context:** BP19 correctly wired the
  DB-first cost basis (`entry_source=db` on every mark), so the stop *ran* — but it
  still leaked money through three holes in `worker/tasks/manage_positions.py`.
  **(A) Stop-net — 3 leaks plugged:**
  **A1 — Phantom resolved-book guard (Fix 1):** a market whose `end_date` has passed
  (`hours < 0`) can keep returning a stale one-sided CLOB book (`best_ask==0`,
  `best_bid≈0.999`) for hours after it actually resolved. The stop math read that as
  "position up +30..150%" and silently disarmed — this is what let `80327923422237`
  (−$5.21) and `80084323272370` (−$4.94) ride to a $0 loss. Now, when
  `phantom_book_guard_enabled` and `hours<0` and `best_ask==0` and
  `best_bid >= phantom_book_bid_min` (0.90), the stop is skipped for that garbage price
  (`stop_skipped_phantom_book`, throttled) and the position is left to the
  redeemable/`reconcile_settlements` settlement path.
  **A2 — Hard-stop before spread-veto (Fix 2):** the catastrophic `hard_stop` floor
  block was moved to run **before** the Layer-2 spread veto and now `continue`s on fire.
  Previously a book collapsing to `best_bid=0.001` was vetoed by the wide-spread guard
  and never hard-stopped (`66649395439683`, −$4.92). The floor now pierces every spread
  protection.
  **A3 — Smart spread-veto bypass (Fix 3):** the veto no longer blocks emergency exits
  on a real collapse. When `drop_pct >= spread_veto_bypass_drop_mult (2.0) ×
  delta_drop_stop_pct` **or** `mid < hard_stop_abs_price`, the wide spread is ignored and
  the stop proceeds (`spread_veto_bypassed`). Rationale: on a genuine crash the spread
  widens precisely when we must sell.
  **A4 — Confirm-tick persistence (Fix 4):** a spread-vetoed poll no longer pops
  `_drop_ticks`. Before, an illiquid fast crash could never accumulate the two
  consecutive breaching ticks it needed because each intervening veto reset the counter
  (`11482573122404`, −$6.61). Non-breaching (recovered) polls still reset it.
  **(B) Monitoring decoupled from pause:** new `get_users_for_monitoring()` in
  `core/db/queries.py` returns every paying, unexpired subscriber with a deposit wallet,
  **without** the `copy_paused_until` / `copy_active` filters. `sync_positions` now uses
  it so a drawdown/daily-loss pause (`paused_drawdown`/`paused_daily_loss`) blocks
  **only new entries** — open positions of a paused user are still stop-lossed and their
  wins still redeemed. New entries stay blocked because both `get_active_subscribers`
  (fan-out gate) and `execute_copy_trade` (L124-140 belt-and-suspenders pause check)
  are unchanged; `get_active_subscribers` was deliberately **not** loosened, to avoid
  resurfacing new trades/signals to paused users through the shared fan-out path.
  Three new config knobs in `core/config.py`: `phantom_book_guard_enabled` (True),
  `phantom_book_bid_min` (0.90), `spread_veto_bypass_drop_mult` (2.0). New helper
  `get_users_for_monitoring` re-exported from `core/db/__init__.py` (import block **and**
  `__all__`, per the BP12-A drift guard). **No DB migration required** — read-path rewire
  only. The −EV entry root-cause (single-whale consensus on high-priced fast-resolving
  favorites) is intentionally **out of scope** for this release (no market/price/consensus
  filter yet).
- **[BP22.8] Redeem-lease 7-day-TTL deadlock fix** (2026-07-10): winnings stopped
  being auto-redeemed for **days** — `reconcile_settlements` looped
  `reconcile_redeem_blocked` with `checked=97 processed=0`, `redeem_position` never ran,
  and Redis held **104 live `once:redeem:{uid}:{cond}` leases**. **Root cause:**
  `backfill_legacy_redemptions` claimed the redeem lease via
  `notify_once(f"redeem:{uid}:{cond}")` **without `ttl=`**, so it used the `notify_once`
  **7-day default (604800 s)** instead of `redeem_lease_sec` (900 s) — sampled leases had
  TTLs of 12 435 / 70 981 / 171 904 s, i.e. created 5–7 days earlier. Those week-long leases
  gated **both** re-dispatch paths (`reconcile_settlements` L1147 and `sync_positions` L158
  share the same key), and `sync_positions` only fires on the Data-API `redeemable` flag
  (which had since flipped off), so nothing could ever re-dispatch and clear them → a
  self-sustaining deadlock. Compounding it, three `redeem_position` early-returns
  (`no_wallet`, `not_registered`, and especially `terminal_state`) returned **without**
  releasing the lease, so a backfill-claimed 7-day lease stuck permanently once the trade
  was already terminal. **Fix (both leak points):** (1) both `backfill_legacy_redemptions`
  claim sites now pass `ttl=settings.redeem_lease_sec`; (2) `redeem_position` now
  `clear_once(f"redeem:{uid}:{cond}")` on the `no_wallet` / `not_registered` /
  `terminal_state` early-returns too, so a claimed lease can never linger. **Safe by
  design:** the terminal state is the DB (`copy_trades.redeemed_at`), never the Redis key,
  and `redeem_position` re-verifies terminal state on entry — clearing a lease is never a
  double-redeem risk. **Operational recovery:** the pre-existing 7-day leases were flushed
  from Redis (`DEL once:redeem:*`); redemption resumed immediately (verified: uid 2 paid
  `+$6.89`, tx `0x1732b260…` confirmed in 16 s; `checked` fell 97→95). Redeem tx/gas were
  never the problem. Diagnosed alongside BP23.
- **[BP23] Gevent-safe Telegram notifier** (2026-07-10): fixes a prod flood of
  `RuntimeError: asyncio.run() cannot be called from a running event loop` that was
  **silently dropping most user notifications** — most visibly the signal-only alerts to
  @sto1ner (uid 4) and the other signal-mode users after the Alchemy outage cleared and a
  backlog of signals fanned out at once. **Root cause:** the worker runs on a **gevent**
  pool (one OS thread, many greenlets), but every notifier in
  `worker/tasks/execute_copy.py` sent via `asyncio.run(PTB Bot.send_message(...))`. When
  two notifications overlapped, greenlet A's event loop was still *running* (parked on the
  Telegram HTTP await) when gevent switched to greenlet B, whose `asyncio.run()` in the
  *same* thread then raised. It only worked when calls happened to not overlap, so it
  looked intermittent; a fan-out burst turned it into a near-total drop. **Fix:** one new
  module-level helper `_tg_send(chat_id, text, *, disable_preview=False)` posts to the
  Telegram Bot API with a plain **synchronous `httpx.post`** (httpx is gevent-patched, owns
  no event loop → concurrency-safe, cannot raise that error). All 10 notifiers
  (`_notify`, `_notify_signal_only`, `_notify_consensus`, `_notify_subscription_expired`,
  `_notify_daily_limit`, `_notify_not_registered`, `_notify_low_balance`,
  `_notify_trading_at_minimum`, `_notify_risk_pause`) were converted from the
  `async def _send` / `asyncio.run` pattern to build the message synchronously and call
  `_tg_send`; the top-level `import asyncio` was removed as now-unused. `_tg_send` uses
  `resp.raise_for_status()` on purpose so a Telegram **403** (user blocked the bot — a
  benign, expected case seen for a few telegram_ids) still surfaces to each caller's
  `except` and is logged as `notify_*_failed`, exactly as before. **No behaviour change**
  to message content, throttling (`notify_once`) or the trade/redeem money-path — this is
  a transport fix only. This bug was **unrelated to Alchemy**; the outage merely created
  the burst that exposed it.

- **[BP24] Multi-wallet: named, switchable wallets** (2026-07-13, migration **018**):
  users can now create several **named** wallets (no key/seed import — just a name),
  switch which one is **active**, and see them all in a `👛 Мои кошельки` menu.
  Existing users are untouched — their current wallet is folded into an active
  **"Wallet 1"** by the migration backfill. **Semantics (confirmed with product):**
  copying is **active-wallet-only** — new whale trades are only copied onto the user's
  active wallet; wallets the user switched away from keep holding positions and are
  **still monitored, stop-lossed and redeemed**. New wallets are **auto-registered**
  (gasless deposit-wallet deploy + approvals + CLOB creds) at creation, so they are
  ready to trade once funded. Cap: `MAX_WALLETS_PER_USER = 5`.
  **Architecture (hybrid mirror):** new table `user_wallets` (id, user_id, name,
  wallet_address, wallet_private_key_enc, deposit_wallet_address, deposit_wallet_deployed,
  wallet_registered, clob_*, balance_usdc, is_active) is the source of truth; a partial
  unique index enforces one active wallet per user. The wallet columns on `users` are
  kept as a live **mirror of the ACTIVE wallet** (`core/db/wallets._mirror_wallet_to_user`),
  so the entire ENTRY / balance / deposit / withdraw / UI path keeps reading `users.*`
  unchanged and always operates on the active wallet — near-zero churn and no regression
  for single-wallet users. `copy_trades.wallet_id` (new FK) records which wallet OPENED
  each trade. `users.active_wallet_id` points at the active row.
  **Money-path (`worker/tasks/manage_positions.py`):** `close_position` and
  `redeem_position` gained a `wallet_id` arg and now load their signing context via
  `resolve_signing_wallet(user_id, wallet_id)` (falls back to the active wallet, then to
  the raw users row for pre-migration accounts) — so a trade always signs/redeems with the
  wallet that opened it, even after a switch. `get_users_for_monitoring()` now returns one
  **flattened entry per wallet** (users row with wallet-scoped fields overridden + a
  `wallet_id`), so `sync_positions` monitors ALL of a user's wallets; single-wallet users
  get exactly one entry (identical to BP21 behaviour). `get_outstanding_copy_trades()`
  selects `wallet_id`; `reconcile_settlements` and `backfill_legacy_redemptions` resolve /
  iterate per wallet and pass `wallet_id` through. `execute_copy_trade` stamps
  `copy_trades.wallet_id = users.active_wallet_id` on insert. New helpers in
  `core/db/wallets.py` (`list_wallets`, `count_wallets`, `get_wallet`, `get_active_wallet`,
  `create_wallet`, `update_wallet`, `rename_wallet`, `set_active_wallet`,
  `resolve_signing_wallet`, `MAX_WALLETS_PER_USER`) are re-exported from
  `core/db/__init__.py` (import block **and** `__all__`, per the BP12-A drift guard).
  **Telegram UI (`api/routers/telegram.py`):** `_wallet_kb` gained `👛 Мои кошельки`
  (`wallet_list`); `wallet_new` starts a one-step name prompt (`awaiting_wallet_name`,
  validated to letters/digits/spaces ≤24 chars, dedup by name); the name handler calls
  `_create_named_wallet` (generate → gasless register → `create_wallet(make_active=True)`
  → `is_signal_only=False` hand-off); `wal_switch:{id}` activates a wallet via
  `set_active_wallet`. `_register_deposit_wallet` now also syncs the active `user_wallets`
  row so the mirror and the row never diverge. **Known limitations (documented, out of
  scope for v1):** the deposit monitor (`monitor_deposits`) still watches only the ACTIVE
  wallet's EOA (users.*), so a user should fund the wallet that is currently active; the
  per-wallet `balance_usdc` baseline can go stale after switching away and back (worst case
  a harmless re-sweep, never fund loss). No behaviour change for anyone who never creates a
  second wallet.

- **[BP25] Neg-risk redemption via pUSD collateral adapter (relayer-allowlist fix)**
  (2026-07-14): neg-risk (multi-bucket) winnings — e.g. temperature markets like
  "highest temperature in Madrid 35°C" — could **never be redeemed** and produced the
  repeating "⏳ Выигрыш определён, зачисление задерживается" message. `redeem_winnings`
  routed the neg-risk claim through the **raw NegRiskAdapter**
  (`0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`), but Polymarket's relayer **allowlist
  blocks direct calls to that contract from a deposit wallet**
  (`RelayerApiException 400: "call blocked: call[0] blocked: calls to 0xd91E…35296 are not
  permitted"`). Binary Yes/No wins were unaffected — they go through `ConditionalTokens`,
  which is permitted. **Fix:** neg-risk redemptions now route through the pUSD-native
  **`NegRiskCtfCollateralAdapter`** (`0xadA2005600Dec949baf300f4C6120000bDB6eAab`), the
  relayer-sanctioned redeem path, which burns the WCOL-collateralised ERC-1155 via CTF and
  **returns pUSD directly** (no separate WCOL/USDC.e unwrap). Uses the 4-arg CTF-style
  `redeemPositions(address collateral=pUSD, bytes32 parentCollectionId=0, bytes32
  conditionId, uint256[] indexSets=[1,2])` the docs document for the adapter path.
  The adapter needs `CTF.setApprovalForAll(adapter, true)` from the deposit wallet to burn
  tokens, so (a) `set_trading_approvals` now also approves both pUSD-native adapters
  (`CtfCollateralAdapter` + `NegRiskCtfCollateralAdapter`) for new wallets, and (b) the
  neg-risk redeem batch **self-heals** that approval inline (idempotent `setApprovalForAll`
  as `call[0]`, redeem as `call[1]`) so wallets registered before BP25 redeem without a
  separate re-approval pass. Binary redemption is unchanged. New constants
  `CTF_COLLATERAL_ADAPTER` / `NEG_RISK_CTF_COLLATERAL_ADAPTER` in `core/clob.py`; changes in
  `core/relayer.py` (`set_trading_approvals`, `redeem_winnings`). No DB/schema change; no
  funds were ever at risk — the winning tokens sit safely in the deposit wallet until the
  next `reconcile_settlements` retry, which now succeeds.

- **[BP22] Admin-bot audit: DB-first PnL, trade-history titles, positions cost, honest balance**
  (2026-07-09, migration **017**): full audit of `api/routers/admin_bot.py` after the admin
  reported implausible PnL, invisible trade history, wrong positions amount and wrong balance.
  Four defects fixed (see full Blueprint 22 at the end of this file):
  **(A) PnL source inverted back to the ledger.** Commit `e6f56a0` had made the Data-API
  `/closed-positions` the *primary* PnL source for the admin `/user` card, but its
  `realizedPnl` is derived from `avgPrice`, which is **0/late for our POLY_1271 proxy
  wallets** (the proven BP16/BP19 wrong-source class) → wins were reported as the full
  payout (cost basis 0) → inflated, implausible numbers; `limit=100` also silently capped
  "за всё время". Now `get_pnl_summary` (the `copy_trades` ledger — the same numbers the
  risk breakers use) is primary; the Data API only kicks in when the ledger has **zero**
  settled rows (pre-008 legacy users) or the DB read fails. The card shows the source
  (`леджер`/`Data API`).
  **(B) Trade-history titles fixed.** `get_user_trade_history`'s signal lookup selected
  `event_slug` from `trade_signals` — a column no migration ever created — so PostgREST
  rejected the whole batch lookup, the bare `except: pass` swallowed it, and **every**
  history row rendered `—` (the "не видно какие сделки" symptom). The select is now
  `id, title, outcome` only, and a lookup failure logs `trade_history_signal_lookup_failed`
  loudly instead of degrading silently. History rows now also show trade size, settle date
  and use `smart_truncate` (BP20.C) instead of a mid-word `[:32]` slice.
  **(C) Positions amount added + outcome persisted.** The card shows
  `в позициях ≈ $X` from `get_open_trades_cost` (cost basis, the equity convention).
  `poll_tracked_wallets` now persists the concrete `outcome` name on every
  `trade_signals` insert (previously always NULL for Model B → the BP20 tier-2 outcome
  fallback was dead), with a fail-safe retry-without-column so the money path survives
  even if migration 017 is not applied.
  **(D) Honest balance.** `_user_view` rendered `$0.00 доступно` on **any** RPC failure
  (`except: avail = 0.0`) — indistinguishable from a truly empty wallet. It now renders
  `⚠️ ошибка чтения (RPC)` and logs `admin_balance_read_failed`; POL renders `—` on error.
  Migration **017** (`trade_signals.outcome` + partial index
  `(user_id, redeemed_at desc)` on `copy_trades`) must be applied before deploy.
  **Follow-ups (same day):** 22.5 dead pagination arrow (HTML-escape titles, N+1 lookahead,
  alert-on-edit-failure); 22.6 live-first "в позициях" with a ledger-desync ⚠️ line;
  **22.7 💰 redeem stampede** — `reconcile_settlements` mass-dispatched every resolved win
  at once, the parallel `redeem_winnings` calls collided on the relayer's one-action-per-wallet
  rule, so WON positions were never redeemed (43-row / ~$222 backlog on one user). Now: max
  1 redeem dispatch per user per cycle + per-wallet Redis mutex in `redeem_position` +
  same cap in `backfill_legacy_redemptions` + FIFO drain. See Blueprint 22.7 for details
  and the legacy NULL-condition-row cleanup SQL. **No new migration.**

---

## 4. Known Bugs & Missing Features

This section contains **implementation blueprints**. Each is precise enough to implement directly.
Treat every formula and contract call as authoritative; do not substitute your own. All money-moving
code MUST follow §5 (idempotency, fail-closed, key safety).

**Conventions used below**
- `equity(user)` = pUSD in the deposit wallet **+** value of open positions. For **display/info**, value
  positions at mark (`get_balances(dw).pusd + Σ position.current_value`). For the **drawdown breaker, HWM,
  and exposure gates**, value open positions at **cost basis** (filled entry cost), not at the depressed
  mark — see **Blueprint 8** (`drawdown_equity_mode="cost_basis"`). Marking open, un-resolved positions to
  the live bid fabricated a phantom drawdown in live tests. Use **free pUSD only** where a cash balance is
  required (placing a new order). Never count unredeemed/illiquid tokens as free cash.
- `p` = entry price of a YES/NO share (0–1). `q` = estimated true win probability.
- All new tunables live in `core/config.py`; all new columns in a new `migrations/00X_*.sql` + a helper
  in `core/db/queries.py`. Never hardcode magic numbers in task logic.

---

> **Blueprints 1–6 have been implemented** (see §3 Working Features for details).
> Migrations 008–012 are defined below and must be applied manually in the Supabase SQL editor.
> Migration 012 (exit_tx + user/condition index) must be applied to the live DB before deploying
> this code (see §6.9 / §7.4).
>
> ✅ **Blueprint 7 is IMPLEMENTED** — fixes the prod silent-skip where Blueprint 4's
> exposure cap hard-blocked every trade on small balances. Gates 1 & 2 now clamp.
> No new migration required.
>
> ✅ **Blueprint 8 is IMPLEMENTED** — fixes the live-test
> **phantom drawdown** (cost-basis equity replaces mark-to-market on open positions),
> the **block-alert spam** (compare-and-set state machine, notify only on transition),
> the **mode-asymmetric per-trade risk cap** (unified cap applies in both fixed and Kelly),
> and adds a **manual "🔓 Снять блокировку" inline button** with consent audit trail.
> **Migration 013 applied** (`risk_state`, `risk_override_at`, `risk_override_count`,
> `realized_baseline` columns on `users`).
>
> ✅ **Blueprint 9 is IMPLEMENTED** — three-layer fix for the live-test prod failures
> caused by **release drift**: **(Layer 1)** working-tree untracked files committed and
> deployed (no `settings` shadow in `callback_handler`, `has_terminal_trade` exported
> from `core.db`); **(Layer 2)** fail-loud architecture: global PTB `add_error_handler`
> on both bots, boot self-check `_check_core_imports()` in api+worker, `task_failure`
> Celery signal escalating crashed periodic tasks; **(Layer 3)** money-safety invariant:
> `_emit_win_pending` replaces false-success `_emit_win` at resolution detection in all
> three paths (`sync_positions` redeemable branch, closed-positions branch,
> `reconcile_settlements`), final "✅ Выигрыш зачислен" fires only from `redeem_position`
> after on-chain tx + pUSD balance change confirmed, retry-exhausted sends
> `_emit_win_retry_failed`. No new migration required.
>
> 🟢 **Blueprint 10 is IMPLEMENTED** — RCA of the 2026-06-30 prod incident
> where one losing outcome erased the profit of multiple wins by riding to $0 with **no stop-loss
> sell ever attempted** (`close_position` never invoked, ledger `exit_tx=0`; monitoring loop was
> healthy the whole night). The only exit was a dead-deep `hard_stop_abs_price=0.07` floor,
> disabled in the final `tp_sl_min_hours` and blind to neg-risk tokens that delist from the Data
> API. **Product decision: ship a simple "Delta-Drop" stop, not heavy quant.** Approved params:
> relative **X = 0.30** (exit when `best_bid ≤ entry×0.70`, caps loss ≈30% of stake — a relative
> stop fixes dollar risk at `X·size` regardless of entry); **`min_entry_price` raised 0.05 → 0.40**
> (don't enter dead-zone outcomes where the book is empty); `hard_stop 0.07` kept as floor; one
> `position_mark` log line/cycle for future data-driven tuning of X. Implementation = Delta-Drop
> check in `sync_positions` on the **live CLOB best_bid** + reuse of existing `close_position` for
> the on-chain sell. **X could NOT be fitted to history** — the P&L ledger is corrupt (see
> Blueprint 11) and the intra-trade price path is not stored; only `entry_price` is reliable.
>
> 🟢 **Blueprint 11 is IMPLEMENTED** — P&L booking bug surfaced during BP10
> data-mining: `copy_trades.realized_pnl` is computed from a fragile wallet **balance-delta**
> (`credited = bal_after − bal_before`), so on-chain-confirmed **wins are booked as −100%** (e.g.
> id 695: real `redeem_tx`, `realized_pnl=-4.88`) or left NULL, while losses book correctly. The
> ledger shows a false net-negative and **feeds phantom losses to the Blueprint 4/8 circuit
> breakers**. Fix: source `realized_pnl` from actual redeemed proceeds / `sell_position` fill, not
> a balance delta.
>
> ✅ **Blueprint 12 is IMPLEMENTED** — two prod operational failures reported 2026-06-30.
> **(Bug A — close crash)** `get_open_trade_by_token` added to `core/db/__init__.py` imports and
> `__all__`; boot self-checks in `api/main.py` and `worker/celery_app.py` extended with explicit
> required-name sets so this class of regression fails loud at container start.
> **(Bug B — broken withdrawal + UX redesign)** `withdrawable_usdc(db_user)` helper added to
> `core/polygon.py` as single source of truth (deposit-wallet pUSD + EOA pusd/usdc_e/usdc);
> `transfer_usdc` now waits for the on-chain receipt before returning; `withdraw_funds` task
> (fail-loud): pre-flight balance gate, POL gas check, per-leg error propagation (no silent
> swallow), on-chain receipt confirmed before "✅ Вывод успешно завершён + Polygonscan" notify;
> withdrawal FSM in `telegram.py` uses `withdrawable_usdc` for the "Доступно" display and
> pre-flight amount validation, stale state reset on re-entry, confirm step kept.
> `core/config.py` gains `min_withdraw_usdc = 1.0`. No new migration required.
> *(Numbered 12 because Blueprint 11 was already taken by the P&L-booking fix above.)*

---

### Blueprint 1 — Deterministic resolution tracking & auto-redeem ✅ IMPLEMENTED

**Current state (read before changing):** redemption already exists — `manage_positions.sync_positions`
→ `redeem_position` → `core.relayer.redeem_winnings` (auto-detects binary vs neg-risk on-chain) →
`convert_dw_usdce_to_pusd`. **Do not rewrite this; it works.**

**Why the bug persists — the real root cause:** the trigger is `position.redeemable == True` from the
Polymarket **Data API**. For neg-risk markets the position **disappears from the Data API** once the
parent event fully resolves, so `sync_positions` never sees it as redeemable and never fires the
redeem. Result: winnings sit as unredeemed CTF tokens (or as already-redeemed **USDC.e** that was
never wrapped) and the pUSD balance never updates — exactly the reported symptom. **Trusting the Data
API for settlement is the architectural defect. Settlement must be sourced on-chain.**

**Design — make our own `copy_trades` ledger the source of truth, confirm resolution on-chain:**

1. **Persist what we need to redeem at trade time.** In `execute_copy_trade`, when a fill is confirmed,
   denormalize onto the `copy_trades` row: `condition_id`, `token_id`, `outcome_index`, `neg_risk`,
   `entry_price`, `shares`. (Today these live only on the linked `trade_signals`; we need them on the
   trade for deterministic redeem without API lookups.)
2. **New periodic task `reconcile_settlements`** (beat, every 120s, queue `periodic`,
   `worker/tasks/manage_positions.py`). For every `copy_trades` row with
   `status='confirmed' AND redeemed_at IS NULL`:
   - Read on-chain resolution from the CTF contract (`CONDITIONAL_TOKENS` in `core/clob.py`):
     - `payoutDenominator(bytes32 conditionId) -> uint` — `> 0` ⟺ **resolved**.
     - `payoutNumerators(bytes32 conditionId, uint256 index) -> uint` — winning index has `> 0`.
   - If `payoutDenominator == 0`: not resolved, skip.
   - If resolved: `won = payoutNumerators(cond, outcome_index) > 0`.
     - **Loss:** set `result='loss'`, `realized_pnl = -entry_cost`, `resolved_at=now`,
       `redeemed_at=now` (nothing to claim), notify once.
     - **Win:** dispatch `redeem_position` (existing, idempotent). On success set `result='win'`,
       `realized_pnl`, `resolved_at`, `redeemed_at`, `redeem_tx`.
   - This is independent of Data API visibility → deterministic and self-healing across restarts.
3. **Self-healing sweep (catch stranded value).** Add `convert_dw_usdce_to_pusd` to the redeem path
   AND run it opportunistically in `monitor_deposits`: if the deposit wallet holds USDC.e with no
   pending external deposit, wrap it to pUSD (covers funds redeemed by Polymarket's UI/keeper or by a
   prior partial run). This alone recovers the "money is there as USDC.e" case.
4. **Keep the existing Data-API path as a fast complement,** but the on-chain reconciler is the
   guarantee. Dedup across both with Redis `claim(f"settle:{user_id}:{condition_id}")` /
   `notify_once` so a position is never redeemed or notified twice.

**Contract ABIs to add (in `core/relayer.py`, alongside `_CTF_ABI`):**
```json
[
  {"inputs":[{"name":"conditionId","type":"bytes32"}],"name":"payoutDenominator",
   "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"conditionId","type":"bytes32"},{"name":"index","type":"uint256"}],
   "name":"payoutNumerators","outputs":[{"name":"","type":"uint256"}],
   "stateMutability":"view","type":"function"}
]
```

**Files:** `worker/tasks/manage_positions.py` (new `reconcile_settlements`, wire into beat in
`worker/celery_app.py`), `worker/tasks/execute_copy.py` (denormalize fields), `core/relayer.py`
(resolution read helpers), `core/db/queries.py` (+ `get_outstanding_copy_trades`,
`mark_trade_settled`), `migrations/008_settlement_ledger.sql`.

**Migration 008 (idempotent):**
```sql
alter table copy_trades add column if not exists condition_id text;
alter table copy_trades add column if not exists token_id text;
alter table copy_trades add column if not exists outcome_index int;
alter table copy_trades add column if not exists neg_risk boolean default false;
alter table copy_trades add column if not exists entry_price double precision;
alter table copy_trades add column if not exists shares double precision;
alter table copy_trades add column if not exists result text;          -- win|loss|null
alter table copy_trades add column if not exists realized_pnl double precision;
alter table copy_trades add column if not exists resolved_at timestamptz;
alter table copy_trades add column if not exists redeemed_at timestamptz;
alter table copy_trades add column if not exists redeem_tx text;
create index if not exists idx_copy_trades_open
  on copy_trades (status) where redeemed_at is null;
```

#### Canonical `redeemPositions` reference (Problem 2 — read carefully, correct the misconceptions)

The on-chain claim **is already implemented and verified on mainnet** (`core/relayer.redeem_winnings`,
confirmed tx hashes, pUSD recovered). It is **NOT** a missing feature. Do **not** rewrite it from
scratch or with ethers.js. The canonical flow below documents exactly how it works so future edits
stay correct.

**Three misconceptions to reject:**
1. ❌ *"Send the tx from the user's wallet with ethers.js."* — Wrong stack and wrong signer. We use
   **Python `web3.py`** and the user **EOA usually holds no POL for gas**. The claim is executed
   **gaslessly** as a **relayer `DepositWalletCall` batch from the user's deposit wallet** (the
   ERC-1967 proxy that actually holds the outcome tokens). See `relayer.execute_deposit_wallet_batch`.
2. ❌ *"Listen to the `ConditionResolution` event."* — Event-log subscriptions miss events on restart
   and need reorg handling. Our beat-driven architecture **polls on-chain state** instead:
   `payoutDenominator(conditionId) > 0` ⟺ resolved. Idempotent, restart-safe, no missed events. (The
   event may be added later only as an optional latency accelerator, never as the source of truth.)
3. ❌ *"Plain CTF `redeemPositions` works for everything."* — **Neg-risk markets do not redeem through
   the CTF directly.** They redeem through the **NegRiskAdapter**. `redeem_winnings` auto-detects which
   by matching the held token's on-chain `positionId` (see below). Never trust the Data API
   `negativeRisk` flag.

**Contracts (import from `core/clob.py`, never hardcode):**
| Constant | Address | Role |
|---|---|---|
| `CONDITIONAL_TOKENS` | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | CTF (binary redeem, payout reads, ERC-1155 balances) |
| `NEG_RISK_ADAPTER` | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` | neg-risk redeem + WrappedCollateral |
| `PUSD_ADDRESS` | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` | V2 collateral |

**Market-type detection (web3.py `eth_call`, no tx):** with `idx = 1 << outcome_index`,
`collectionId = CTF.getCollectionId(0x00…00, conditionId, idx)`:
- if `CTF.getPositionId(NegRiskAdapter.wcol(), collectionId) == held_token_id` → **neg-risk**;
- elif `CTF.getPositionId(collateral, collectionId) == held_token_id` for `collateral ∈
  {pUSD, USDC.e, USDC}` → **binary** with that collateral.

**Claim calldata (encode with `eth_abi`, wrap in a `DepositWalletCall`, send via the relayer batch):**
- **Binary:** `ConditionalTokens.redeemPositions(address collateralToken, bytes32 parentCollectionId,
  bytes32 conditionId, uint256[] indexSets)` with `parentCollectionId = 0x00…00` and
  `indexSets = [1, 2]`. The CTF burns the deposit wallet's outcome tokens and pays the collateral to
  the deposit wallet (msg.sender).
- **Neg-risk:** `NegRiskAdapter.redeemPositions(bytes32 conditionId, uint256[] amounts)` where
  `amounts[outcome_index] = on-chain ERC-1155 balance of the held token` (read via
  `CTF.balanceOf(dw, token_id)`), others `0`.
- **Payout currency = USDC.e** (empirically confirmed for both paths). The flow then calls
  `convert_dw_usdce_to_pusd` (CollateralOnramp `wrap`, also a gasless relayer batch) so the winnings
  become tradeable pUSD. **This second step is mandatory — without it the balance "doesn't update".**

**Approvals:** the deposit wallet must have `setApprovalForAll(NEG_RISK_ADAPTER, true)` on the CTF
(done at registration in `relayer.set_trading_approvals`) and USDC.e `approve(CollateralOnramp)` (done
inside `convert_dw_usdce_to_pusd`).

**Serialization:** the relayer permits **one in-flight action per deposit wallet**. On
`"wallet busy"`, retry with backoff (see `scripts/verify_v2.py redeem`). Manual recovery commands:
`python scripts/verify_v2.py inspectpos | redeem | wrapdw`.

**REMAINING GAP — ✅ IMPLEMENTED:**
1. **Migration 008 must be applied** in the target DB (see §7.4).
2. **Legacy positions (NULL ledger fields) — now recovered by `backfill_legacy_redemptions`**
   (beat every 600s, `worker/tasks/manage_positions.py`): enumerates resolved-won holdings via
   Data API (`get_positions(dw)`) and on-chain (`is_condition_resolved` + `get_payout_numerator`),
   dispatches `redeem_position` for each winner not yet redeemed. Deduped by Redis.
   Also self-heals stranded USDC.e on the deposit wallet (`convert_dw_usdce_to_pusd`).
3. **Self-healing USDC.e sweep** also added to `monitor_deposits` (runs every 120s): wraps any
   USDC.e ≥ $0.10 on the deposit wallet into tradeable pUSD opportunistically.
4. **Deploy/rebuild**: the worker image must be rebuilt (`docker compose up -d --build worker`).

**Acceptance:** a won position credits pUSD within ~2 min of on-chain resolution **even if it never
appears as `redeemable` in the Data API**; legacy positions with NULL ledger fields are recovered by
the backfill reconciler; no double-redeem; loss recorded with correct P&L; restart mid-flight resumes
safely.

---

### Blueprint 2 — Slice aggregation (accumulation tracker) + alert debounce ✅ IMPLEMENTED

**Current state:** `poll_tracked_wallets` already groups sliced fills per `(condition, token)` inside
the fetch window and dedups with an in-memory `_seen` map + a DB lookup. **The aggregation is real.**

**Reject the naive "just debounce" framing — diagnose precisely:**
- *Aggregation defect:* the current code fires the moment the running sum crosses
  `tracked_min_copy_usdc` — i.e. **mid-slice**, on a partial sum, often at a worse VWAP, while the
  whale keeps adding. We want to fire **once the slicing settles**.
- *Spam defect (the actual user-visible symptom):* the spam is **low-balance alerts**, not duplicate
  trades. Each fanned-out `execute_copy_trade` for an underfunded user calls `_notify_low_balance`
  with no throttle, so every signal re-alerts. Aggregation does not fix this; **alert throttling does.**

**Design A — Redis accumulation tracker with quiet-period + max-window debounce.**
Replace the in-memory `_seen` aggregation with a cross-process Redis bucket per `(wallet, cond, token)`:

State per bucket (Redis hash, TTL = `max_window + reentry`):
`first_ts, last_fill_ts, acc_usdc, acc_notional (Σ price·size), fills, fired(bool)`.

Each poll cycle, for fresh BUY fills not already counted (dedupe individual fills by `tx_hash` in a
Redis set), add to the bucket. **Fire exactly one signal when BOTH hold:**
```
acc_usdc >= threshold                                  # conviction floor (below)
AND ( now - last_fill_ts >= quiet_period_sec           # slicing has settled, OR
      OR now - first_ts   >= max_window_sec )          # hard cap so we never wait forever
AND not fired
```
On fire: set `fired=True`, emit signal with `price = acc_notional / acc_usdc` (true VWAP), keep the
bucket until `reentry` elapses to block re-fire. This guarantees **one entry + one alert per burst**,
entered after the whale finishes building.

**Threshold algorithm (be honest about what it does):** our copy size is a **fixed cap clamped by
depth** (§3), so the whale's absolute size is **not** a sizing input — the threshold's only job is
**noise filtering** (real directional entry vs dust/rebalance). Compute it relative to the whale, not
a flat constant:
```
threshold = max(
    abs_floor,                       # hard floor, default $50  (tracked_min_copy_usdc)
    conviction_frac * whale_avg_size # e.g. 0.5 × wallet avg trade size from discovery profile
)
```
Whale avg size is already computed in `core/wallet_discovery._activity_profile` (`avg_size`); cache it
on the `tracked_wallets` row (add column `avg_trade_usdc`) so the poller reads it cheaply. Rationale:
a $200 entry from a wallet that averages $5k is noise; the same $200 from a $300-avg wallet is a real
position. Defaults: `abs_floor=$50`, `conviction_frac=0.5`, `quiet_period=45s`, `max_window=180s`.

**Design B — alert throttling (fixes the actual spam).**
- Throttle `_notify_low_balance` with `notify_once(f"lowbal:{user_id}", ttl=6h)` so an underfunded
  user gets at most one low-balance nudge per 6h, not one per signal.
- Better: **check free pUSD once before fan-out** in `poll_tracked_wallets`; skip dispatch for users
  below `min_balance` and send the throttled nudge from there, so we don't even enqueue doomed trades.

**Files:** `worker/tasks/poll_tracked_wallets.py` (Redis accumulator + pre-fan-out balance gate),
`core/cache.py` (small hash-bucket helpers: `accum_add`, `accum_get`, `accum_mark_fired`),
`worker/tasks/execute_copy.py` (throttle `_notify_low_balance`), `core/config.py` (new knobs),
`migrations/009_tracked_avg_size.sql` (`alter table tracked_wallets add column if not exists
avg_trade_usdc double precision;`).

**Config:**
```python
slice_quiet_period_sec: int = 45
slice_max_window_sec: int = 180
slice_conviction_frac: float = 0.5
lowbal_alert_throttle_sec: int = 21600   # 6h
```

**Acceptance:** a whale that slices one $4k entry into 40 fills over 2 min → **one** copy + **one**
alert, entered at the blended VWAP after slicing settles; an underfunded user gets ≤1 low-balance
alert per 6h regardless of signal volume.

---

### Blueprint 3 — Risk-based position sizing (fractional Kelly, done correctly) ✅ IMPLEMENTED

**Reject the naive Kelly the prompt hints at.** Two things must be stated plainly:
1. Kelly needs an **edge** `q − p`. In copy-trading we **do not observe `q`**. Using a wallet's raw
   `winrate` as `q` is **wrong**: winrate is unconditional on price, while `q` must be the win
   probability **of this specific market at price `p`**. A 90%-winrate wallet betting a 0.95 favorite
   has ~zero edge, not 90%.
2. Full Kelly is variance-optimal only with a **known** edge; with an **estimated** edge it badly
   overbets. Always use **fractional Kelly** and let hard caps dominate.

**Correct binary-market Kelly.** Buying one YES share at price `p` pays `1` on win, `0` on loss →
net odds `b = (1−p)/p`. Kelly fraction of equity:
```
f* = (q − p) / (1 − p)          # bet only if q > p; else f* = 0
```
(Derivation: f* = q − (1−q)·p/(1−p) = (q−p)/(1−p).)

**Edge estimation — conservative, bounded, shrunk (this is the defensible part):**
We never trust raw winrate as `q`. Instead build a small bounded edge and add it to the market price:
```
# 1) shrink the wallet's winrate toward the market to kill small-sample noise (Bayesian)
n      = score.resolved_count
w_hat  = (wins + α) / (n + α + β)        # Beta prior, e.g. α=β=10  → pulls toward 0.5 when n small
# 2) convert track record into a *small* edge, NOT a probability
quality = clamp((w_hat - 0.5) * 2, 0, 1) # 0..1 trust factor from track record
consensus_mult = min(1 + 0.25 * (consensus - 1), 1.5)
edge_hat = min(base_edge * quality * consensus_mult, edge_cap)   # base_edge≈0.03, edge_cap≈0.06
q_hat = min(p + edge_hat, 0.99)
```
`wins` and `resolved_count` come from `core.wallet_score.score_wallet`; `consensus` is already on the
signal. If the wallet is unscored (lag), use `edge_hat = base_edge * 0.5` (minimum trust).

**Final stake (caps dominate, with a minimum-order FLOOR — see Problem 1 below):**
```
f_kelly = max((q_hat - p) / (1 - p), 0)
f       = kelly_lambda * f_kelly                 # kelly_lambda = 0.25 (quarter-Kelly)
f       = min(f, max_risk_per_trade)             # hard ceiling, e.g. 0.05 of equity
stake   = f * equity(user)
stake   = min(stake, user.max_position_usdc, depth_cap)   # existing depth/cap clamps stay
# Floor up to the exchange minimum so small accounts still trade (NOT a hard block):
stake   = max(stake, exchange_min_order(market))          # see Problem 1
stake   = min(stake, free_pusd)                           # never spend more than we have
stake   = 0 only if free_pusd < exchange_min_order(market) OR depth < exchange_min_order(market)
```
This **replaces** the flat `size_usdc = min(user_max, depth_cap)` in `execute_copy_trade`. Keep all
existing depth/book-safety/price-band clamps; Kelly sets the **upper** bound, the exchange minimum sets
the **lower** bound.

**Files:** `core/sizing.py` (pure module: `kelly_stake(p, score, consensus, equity, free_pusd, cfg)
-> float`, unit-testable, no I/O), `worker/tasks/execute_copy.py` (call it), `core/config.py` (knobs).

**Config:**
```python
sizing_mode: str = "kelly"          # "fixed" (legacy) | "kelly"
kelly_lambda: float = 0.25          # fraction of full Kelly
kelly_base_edge: float = 0.03       # edge for a fully-trusted single wallet
kelly_edge_cap: float = 0.06        # absolute edge ceiling
kelly_prior_strength: float = 10.0  # α=β for winrate shrinkage
max_risk_per_trade: float = 0.05    # hard cap: ≤5% of equity per position
recommended_min_balance_usdc: float = 100.0  # SOFT target (warn only, never blocks)
exchange_min_order_usdc: float = 1.0         # Polymarket platform floor (fallback size)
n_target_positions: int = 5
```

**Acceptance:** stake scales up with wallet quality/consensus and **down** as `p → 1` (favorites get
small bets), never exceeds `max_risk_per_trade · equity`. `sizing_mode="fixed"` reproduces legacy
behavior for rollback. **(See Problem 1 below for small-balance behavior.)**

---

### Blueprint 3.1 — Soft minimum balance + minimum-order fallback ✅ IMPLEMENTED

**Current bug:** `execute_copy_trade` does `if equity < settings.min_balance_usdc ($100): skip` and
`if size_usdc < settings.min_order_usdc ($5): skip`. A real $3.06 wallet is **hard-blocked** and never
trades. For a SaaS this silently breaks the product for small accounts.

**Reject the hard block.** `$100` is a **risk-management recommendation**, not a gate. Below it we
**still trade**, at the smallest valid Polymarket order, and **warn** the user to top up.

**Polymarket minimum order size.** The CLOB enforces a per-market minimum. Use **`$1` USDC notional**
as the platform floor, and prefer the market-specific value when available:
```
# /markets/{conditionId} → minimum_order_size (shares) and minimum_tick_size; the order book
# (get_order_book) also exposes these. Notional floor for a BUY:
exchange_min_order(market) = max(
    exchange_min_order_usdc,                       # platform floor, default $1
    minimum_order_size_shares * entry_price        # per-market share minimum × price, if known
)
```
Round up to satisfy tick/size rules; if the market min can't be read, fall back to `$1`.

**Fallback algorithm (replace the two hard `return skip` blocks):**
```
stake = kelly_or_fixed_stake(...)                 # may be tiny for a small account
floor = exchange_min_order(market)

# 1) SOFT recommendation: warn (throttled) when equity is below the recommended target,
#    but DO NOT block.
if equity < recommended_min_balance_usdc:
    warn_once(user, "trading_min", ttl=6h,
              text="⚠️ Баланс ниже рекомендованного. Торгуем на минималках — пополни баланс для нормального риск-менеджмента.")

# 2) Size: respect Kelly/caps, but never below the exchange minimum.
stake = max(stake, floor)
stake = min(stake, free_pusd)                     # can't spend more than we have

# 3) ONLY skip if we genuinely cannot place a valid order:
if free_pusd < floor:
    notify_low_balance_once(user, free_pusd, floor)   # throttled (BP2)
    return skip("insufficient_for_min_order")
if fillable_depth < floor:
    return skip("depth_below_min_order")              # book too thin even for the minimum

place_order(stake)                                    # otherwise: TRADE at the minimum
```
So a $3.06 wallet places a ~$1 order (capped by free balance), gets **one** "trading at minimum" warning,
and keeps copying. It is only skipped when it can't afford even the $1 platform minimum.

**Notes / guardrails:**
- Keep `max_position_usdc` and depth caps as the **upper** bounds; this change only fixes the **lower**
  bound. Risk gates (Blueprint 4: exposure %, drawdown) still apply and are percentage-based, so they
  scale correctly for small accounts.
- The "trading at minimum" warning and the "insufficient for minimum order" alert are **separate** and
  both throttled via `notify_once` (BP2) so small accounts are not spammed.
- Optional: at the very low end the bot can also auto-sweep the EOA (existing `fund_deposit_wallet`)
  before deciding it's underfunded (already attempted in current code).

**Files:** `worker/tasks/execute_copy.py` (remove the `below_min_balance` hard return and the flat
`min_order_usdc` skip; implement the fallback above), `core/sizing.py` (add `exchange_min_order` helper
or compute in the task from the book), `core/polymarket.py` (expose `minimum_order_size` from the
market/book if not already), `core/config.py` (replace `min_balance_usdc`/`min_order_usdc` with
`recommended_min_balance_usdc` + `exchange_min_order_usdc`).

**Acceptance:** a $3.06 wallet trades at the ~$1 minimum and receives exactly one throttled
"trading at minimum" warning; copying is skipped **only** when free pUSD or book depth is below the
platform minimum order; large accounts are unaffected; no hard `$100` block remains anywhere.

---

### Blueprint 4 — Tail-risk controls (exposure, correlation, drawdown, daily loss) ✅ IMPLEMENTED

**The math behind the "verняк" trap.** A 0.75 favorite pays only `(1−p)/p = 0.33` per $1 risked but
loses the **entire** stake on the rare miss → strongly **negative skew**. Flat or oversized, a single
loss erases many wins (5 wins × +0.33 = +1.67, wiped by ~5 losses... but at large `f` one loss can be
ruinous). Kelly (Blueprint 3) sizes each bet correctly; Blueprint 4 is the **portfolio backstop** for
estimation error and **correlation** (favorites cluster — same event, same team, same day).

Implement four gates as a single **pre-trade risk check** in `execute_copy_trade`, evaluated **after**
sizing and **before** `place_order`. Any breach → skip with a throttled alert (reuse Blueprint 2's
throttle). Order them cheapest-first.

1. **Aggregate exposure cap.** Total cost of open positions must stay within a fraction of equity:
   ```
   open_exposure = Σ open_position.cost
   skip if (open_exposure + stake) > max_portfolio_exposure_pct * equity   # e.g. 0.60
   ```
   Keeps dry powder; prevents being 100% deployed into correlated favorites.

2. **Per-event / correlation cap.** Two outcomes in the **same event** can lose together → treat them
   as one correlated bet. Bucket by `event_slug` (present on signals/positions; fall back to
   `eventId`). The existing "already in this market" guard is **per-condition** — upgrade it to
   **per-event**:
   ```
   event_exposure = Σ cost of open positions sharing this signal's event_slug
   skip if (event_exposure + stake) > max_event_exposure_pct * equity      # e.g. 0.15
   ```
   This subsumes the scattershot risk and limits team/league clustering at the event level. (Stretch:
   group by Gamma category/tag for cross-event correlation; event-level is the pragmatic v1.)

3. **Drawdown circuit breaker (high-water mark).** Track per-user realized equity HWM; if current
   equity falls too far from peak, **pause copying**:
   ```
   hwm = max(hwm, equity)                          # update each settlement/sync
   drawdown = (hwm - equity) / hwm
   if drawdown >= max_drawdown_pct:                # e.g. 0.25
       set users.copy_paused_until = now + cooldown   # e.g. 24h
       notify user once; reconcile loop auto-resumes after cooldown
   ```
   Store `equity_hwm` and `copy_paused_until` on `users`. `get_active_subscribers` and the pre-trade
   gate must honor `copy_paused_until`.

4. **Daily loss limit.** Sum `realized_pnl` of `copy_trades` settled in the trailing 24h; if losses
   exceed a fraction of start-of-day equity, pause until the next UTC day:
   ```
   if Σ realized_pnl (last 24h) <= -daily_loss_limit_pct * equity_at_day_start:  # e.g. 0.10
       pause copying until 00:00 UTC; notify once
   ```

**Note on the existing hard-stop** (`hard_stop_abs_price = 0.07`): keep it, but it is a
**capital-recycling** tool (exit a near-dead outcome), **not** a risk control. The real protection is
correct sizing (BP3) + these portfolio gates. Do not present the hard-stop as the stop-loss system.

**Files:** `core/risk.py` (new pure module: `check_risk_gates(user, signal, stake, open_positions,
cfg) -> RiskDecision`), `worker/tasks/execute_copy.py` (call before `place_order`),
`worker/tasks/manage_positions.py` (update `equity_hwm`, evaluate breaker/daily-loss in the reconcile
loop, set/clear `copy_paused_until`), `core/db/queries.py` (HWM + pause helpers; honor pause in
`get_active_subscribers`), `migrations/010_risk_controls.sql`.

**Migration 010 (idempotent):**
```sql
alter table users add column if not exists equity_hwm double precision default 0;
alter table users add column if not exists copy_paused_until timestamptz;
```

**Config:**
```python
max_portfolio_exposure_pct: float = 0.60
max_event_exposure_pct: float = 0.15
max_drawdown_pct: float = 0.25
drawdown_cooldown_sec: int = 86400
daily_loss_limit_pct: float = 0.10
```

**Acceptance:** a user cannot deploy >60% of equity, cannot stack >15% into one event, is auto-paused
on a 25% drawdown (auto-resumes after cooldown) and on a 10% daily loss; all pauses notify once and
are honored by both the fan-out and the pre-trade gate.

---

### Blueprint 5 — Correct time-to-resolution parsing & display ✅ IMPLEMENTED

**Symptom (live test):** the bot tells the user "до закрытия рынка ~1 ч" while the event
actually runs until end of the US day (~12 h). The countdown is both **wrong** and **frozen**.

**Root cause — three compounding defects, do NOT "just add hours":**

1. **Wrong source field (dominant cause).** `core/polymarket._build_market_meta` reads
   `end_iso = m.get("endDateIso") or m.get("endDate")` (`core/polymarket.py:101`). For any market
   that belongs to an **event** (sports / daily / multi-outcome — i.e. most fast markets), the
   *per-market* `endDate` is frequently an **understated placeholder** (a nominal cutoff or the
   game's scheduled time), while the real resolution boundary is the **event** `endDate`
   ("end of US day"). The event object is already fetched — `events = m.get("events")` — but only
   `events[0].slug` is kept; **`events[0].endDate` is discarded.** That discarded field is the
   correct one.
2. **Frozen value, never recomputed.** `hours_to_resolve` is computed **once** at fast-markets
   cache-build time (`refresh_fast_markets`, every `fast_markets_refresh_sec=120s`) and copied
   verbatim into the signal (`worker/tasks/poll_tracked_wallets.py:259` → `meta.get("hours_to_resolve")`).
   The user notification (`worker/tasks/execute_copy.py:660`) and the AI prompt
   (`worker/tasks/ai_filter.py:50`) read `signal["hours_to_resolve"]` directly — a stale scalar,
   never recomputed at send time. `core/detector._hours_fresh` already recomputes correctly **but
   the live Model-B path never calls it**, and the signal does not even carry the ISO end date, so
   downstream *cannot* recompute.
3. **Timezone / date-only edge.** Polymarket ISO strings are UTC (`…Z`); the arithmetic in
   `_hours_until` is correctly tz-aware, so there is **no EST math bug** — the EST aspect is purely
   a *display* concern ("end of US day"). The real parsing trap is **date-only** strings
   (`"2026-06-24"`), which `dateutil` parses to `00:00:00Z` — understating the deadline by up to a
   full day.

**Design — one authoritative time source, computed fresh, formatted for humans.**

**(A) Single resolution-time resolver (pure, in `core/polymarket.py`).**
```
def resolution_dt(obj: dict) -> datetime | None:
    # obj = a Gamma market dict OR a normalized position dict.
    # Candidate ISO strings, in AUTHORITY order (most reliable first):
    #   market endDateIso, event endDate (events[0].endDate), market endDate, position endDate
    # Parse each → tz-aware UTC:
    #   - naive (no tz)      → assume UTC
    #   - date-only "YYYY-MM-DD" → 23:59:59Z of that day (END of day, never 00:00)
    # Selection rule (this is the fix for the placeholder-endDate bug):
    #   among all parsed candidates, return the LATEST one that is still in the future;
    #   if none are in the future, return the latest candidate overall (resolving/closing).
    # Rationale: Polymarket keeps a market tradeable until the real event concludes, and the
    # per-market endDate is the field that UNDER-states; taking the latest plausible boundary
    # (which is the event endDate for grouped markets) matches reality.
    # gameStartTime is a START, not a resolution — never use it as the deadline.
```

**(B) Carry the ISO end on the signal, not a frozen scalar.**
- In `_build_market_meta`: also extract `event_end_iso = (events[0] or {}).get("endDate")` and store
  `resolution_iso = resolution_dt(m).isoformat()` in the meta (keep `hours_to_resolve` for the
  internal window filter only — see (D)).
- In `poll_tracked_wallets` signal dict: replace `"hours_to_resolve": meta.get("hours_to_resolve")`
  with `"resolution_iso": meta.get("resolution_iso")` (and keep `hours_to_resolve` only if some
  consumer still needs the coarse number).

**(C) Format fresh at the moment of sending (pure, in `core/polymarket.py`).**
```
def format_time_left(resolution_iso: str | None, now: datetime | None = None) -> str:
    dt = _parse_iso(resolution_iso); now = now or datetime.now(timezone.utc)
    if dt is None:                    return "время уточняется"
    delta = (dt - now).total_seconds()
    if delta <= 0:                    return "резолв скоро"
    if delta < 3600:                  return "<1 ч"
    if delta < 86400:                 return f"~{delta/3600:.0f} ч"
    d, h = divmod(int(delta // 3600), 24)   # d days, h hours
    return f"{d}д {h}ч"
```
- Replace **every** display site that does `f"~{hours:.0f} ч"`:
  `execute_copy._notify` (`hours_line`) and `ai_filter._call_gpt` (`hours=`) must call
  `format_time_left(signal.get("resolution_iso"))` evaluated **at send time**, not the frozen field.
- Optional nicety (recommended for "end of US day" markets): also render the ET wall-clock deadline
  via `zoneinfo.ZoneInfo("America/New_York")`, e.g. `"до 23:59 ET (через ~12 ч)"`. This is display
  only; all math stays in UTC.

**(D) Keep `hours_to_resolve` for the coarse window filter only.** The
`market_min/max_hours_to_resolve` gate in `_build_market_meta` can keep using the freshly computed
hours from `resolution_dt` — small staleness there is harmless. It must **not** be surfaced to users.

**Files:** `core/polymarket.py` (`resolution_dt`, `format_time_left`, use event endDate in
`_build_market_meta`, add `resolution_iso` to meta), `worker/tasks/poll_tracked_wallets.py` (carry
`resolution_iso` on the signal), `worker/tasks/execute_copy.py` (`_notify` formats fresh),
`worker/tasks/ai_filter.py` (`_call_gpt` formats fresh), `core/detector.py` (route `_hours_fresh`
through `resolution_dt` so all paths agree). No migration required (signal is an in-memory dict).

**Config (optional):**
```python
show_resolution_in_et: bool = True   # also render the America/New_York deadline in notifications
```

**Acceptance:** for an event that resolves at end of US day, the notification and the AI prompt show
~12 h (and optionally the ET deadline), not 1 h; the value is correct even minutes after the cache
was built (recomputed at send time); date-only `endDate` markets are treated as end-of-day, never
midnight; markets grouped under an event use the event boundary, not the understated per-market one.

---

### Blueprint 6 — Position state machine & P&L on manual / stop close ✅ IMPLEMENTED

**Symptom (live test):** user manually sells a position **in profit**. Hours later the contract
resolves on-chain and the bot (a) sends an **empty** win notification (no title) crediting **$0.01**
of dust, then (b) immediately trips the **daily-loss limit** and blocks trading — though there was no
loss.

**Root cause — the ledger is never closed on a token-sale exit (code-cited):**

1. `manage_positions.close_position` sells the outcome tokens, sets a **TTL-bound** Redis key
   (`_claim_settled` → `settle:{uid}:{cond}`), and notifies the user — but **never updates the
   `copy_trades` row**. The row stays `status='confirmed', redeemed_at IS NULL`.
2. `get_outstanding_copy_trades` (`core/db/queries.py:378`) therefore keeps returning that row.
   When the condition later resolves on-chain, `reconcile_settlements` acts on it. By then the
   Redis `settle:`/`redeem:` keys set at close time have **expired** (TTL ≪ hours-to-resolution),
   so the dedup guard is gone.
3. If the exited outcome happens to **win**, reconcile dispatches `redeem_position`, which claims the
   **dust** left behind by `close_position`'s floor-to-2-dp truncation (`manage_positions.py:281`,
   `math.floor(shares*100)/100`), emits `_emit_win`/"Выигрыш зачислен" with `title=None, outcome=None`
   (the empty message — symptom **a**), and writes
   `realized_pnl = credited(≈$0.01) − entry_cost(full size)` ⇒ a large **phantom loss** on a
   `result='win'` row.
4. `get_daily_realized_pnl` sums that phantom negative ⇒ BP4 daily-loss breaker trips ⇒ trading
   paused (symptom **b**). (On the loss branch reconcile would also write `-entry_cost` for an
   already-exited position — double-counting.)

**Design — the `copy_trades` ledger is the single state machine; a token-sale exit is TERMINAL.**

**(1) Close = write the ledger, atomically, in `close_position`.** On a successful `sell_position`:
- Compute realized P&L from the **actual sale**, not from any later resolution:
  ```
  entry_cost  = copy_trades.size_usdc          # filled cost basis (denormalized at fill)
  proceeds    = shares_sold * fill_price        # executed sale value (from the sell result)
  realized_pnl = proceeds - entry_cost          # correct sign immediately (+ on a profitable exit)
  ```
- Call a new `mark_trade_closed(trade_id, result='closed', realized_pnl, exit_tx)` that sets
  `status='closed', result='closed', realized_pnl, resolved_at=now, redeemed_at=now`. Setting
  `status='closed'` **and** `redeemed_at` removes the row from `get_outstanding_copy_trades` forever
  (it filters `status='confirmed' AND redeemed_at IS NULL`) — independent of any Redis TTL.
- This books Realized P&L into the daily stats **once, with the right sign, at close time**, and
  permanently removes the position from the resolve / auto-claim queue.

**(2) Find the row to close.** `close_position(user_id, token_id, reason)` lacks the trade id. Add
`get_open_trade_by_token(user_id, token_id) -> dict|None` (status `confirmed`, `redeemed_at IS NULL`,
matching `token_id`, newest first) to obtain `trade_id` + `size_usdc`. Where `sync_positions`
dispatches `close_position` it may pass the `trade_id` directly when known.

**(3) Make reconcile / backfill respect ledger state (defense in depth).**
- `get_outstanding_copy_trades` already excludes `status!='confirmed'`, so step (1) auto-fixes the
  main path. **Additionally**, in `backfill_legacy_redemptions` (which enumerates Data-API holdings
  irrespective of the ledger) add a guard: before dispatching a redeem for `(uid, condition_id)`,
  skip if any `copy_trades` row for that `(user_id, condition_id)` is in a **terminal** state
  (`status IN ('closed') OR redeemed_at IS NOT NULL`). Helper: `has_terminal_trade(user_id, condition_id) -> bool`.

**(4) Dust + active-trade guard in the claim path (covers legacy rows with NULL ledger fields).**
Before any redeem **and** before any win/loss notification, in `redeem_position`,
`reconcile_settlements`, and `backfill_legacy_redemptions`:
- Read the on-chain ERC-1155 balance `CTF.balanceOf(deposit_wallet, token_id)` (the relayer already
  reads this for neg-risk amounts — expose a small `get_ctf_balance(dw, token_id) -> int` helper).
  Convert to shares; if `shares < claim_dust_min_shares` (or notional `shares * resolve_price <
  claim_dust_min_usdc`), **skip the claim and the notification** and log `skip_dust_claim`. This
  alone kills the empty "$0.01 win" message, even for pre-existing rows.
- Require the trade to still be active: if a matching `copy_trades` row exists and is **terminal**,
  skip (the user already exited — do not claim, do not notify).

**(5) Never notify with a missing title.** `_emit_win`/`_emit_loss` must not fire with
`title=None`. On the on-chain-only path, hydrate the title from the trade's `signal_id`
(join `trade_signals.title`) or from the Data API position; if still unknown **and** the balance is
dust, suppress entirely (per step 4). A real, non-dust win must always carry a human title.

**(6) Leave genuine holds untouched.** For positions actually held to resolution,
`realized_pnl = credited − entry_cost` stays correct **because** `credited` is the full redemption,
not dust — the dust + terminal guards guarantee that formula never runs on an exited position.

**Files:** `worker/tasks/manage_positions.py` (`close_position` books P&L + `mark_trade_closed`;
`reconcile_settlements`/`backfill_legacy_redemptions`/`redeem_position` get dust + terminal-state +
title guards), `core/db/queries.py` (`get_open_trade_by_token`, `mark_trade_closed`,
`has_terminal_trade`), `core/relayer.py` (`get_ctf_balance` helper if not already exposed),
`core/config.py` (knobs), `migrations/011_position_state.sql`.

**Migration 011 (idempotent):**
```sql
-- 'result' is free-text; documented values now: win | loss | closed | null
alter table copy_trades add column if not exists exit_tx text;
create index if not exists idx_copy_trades_user_condition
  on copy_trades (user_id, condition_id);
```

**Config:**
```python
claim_dust_min_shares: float = 1.0   # ignore outcome-token balances below this at claim time
claim_dust_min_usdc:   float = 1.0   # …or notional floor (≈ exchange minimum) at resolve price
```

**Acceptance:** a manual (or hard-stop) close in profit immediately books `result='closed'` with a
correct **positive** realized P&L and disappears from the outstanding queue; the later on-chain
resolution produces **no** claim and **no** notification (dust + terminal guards); the daily-loss
breaker is **not** tripped; genuine holds still redeem fully and send exactly one titled win/loss
message; no empty or `$0.01` notifications ever occur; behavior is restart-safe because state lives in
the DB ledger, not in Redis TTLs.

---

### Blueprint 7 — Small-balance silent-skip RCA: exposure-cap → warn-only + sizing-mode-aware messaging ✅ IMPLEMENTED

**Symptom (prod, silent failure).** A small-balance user receives the "торгуем на минимальном объёме"
soft-limit alert, and then the bot **opens no positions at all** — no new trades, no error in the
worker logs, no user-facing failure. The bot looks alive but never trades.

**Root cause (from prod worker logs, user_id=2).** This is **not** an Ethers/decimals, relayer, CLOB,
or event-loop problem (all ruled out by logs: no `place_order_failed`, no web3/signature tracebacks,
worker heartbeat + periodic tasks healthy). The trade dies on **Blueprint 4's Gate 1 (exposure_cap)**,
which **hard-skips** instead of clamping:
```
[info] equity_below_recommended       equity=14.21 recommended=100.0 user_id=2     # the soft-limit alert
[info] skip_risk_gate  gate=exposure_cap
       reason='Открытые позиции (5.00$) + ставка (8.00$) превышают 60% капитала.'  # every signal dies here
```
Math: `equity≈$14.21`, open `$5.00`, fixed stake `$8.00`, cap `0.60 × 14.21 = $8.53`. Since
`5.00 + 8.00 = 13.00 > 8.53` every BUY is blocked. **At low equity the $5 platform minimum order and
the 60% exposure cap are mathematically incompatible**: after the first position there is almost no
headroom left for a new one. The skip is logged at `info` and the `skip_risk_gate` user alert is
throttled to once/hour (`notify_once(..., ttl=3600)`), so after the first soft-limit message the user
sees only silence. (Secondary, **not** a bug: many signals also legitimately hit
`skip_price_out_of_range` because tracked whales buy favorites priced > `max_entry_price=0.95`.)

**Product decision (agreed).** Below the recommended balance, **never block** on the exposure cap —
trade what fits, and if even the minimum order can't fit under the cap, **place it anyway and warn**
that the bot entered with >60% of balance. `$100` stays a recommendation, shown **conditionally on the
sizing mode**.

**Fix (sub-blueprints):**

1. **Capacity gates clamp, never silent-block (core RCA fix).** In `core/risk.py`, Gates 1
   (exposure_cap) and 2 (event_cap) stop returning a hard `allowed=False`. Extend `RiskDecision` with
   `max_stake: float | None` and `warn: str | None`. Compute `headroom = cap − already_deployed`:
   - `headroom ≥ stake` → allow, no message.
   - `exchange_min ≤ headroom < stake` → **clamp** `max_stake = headroom` (enter smaller, under cap, no
     warning).
   - `headroom < exchange_min ($5)`:
     - equity **< `recommended_min_balance_usdc` ($100)** → **enter at the $5 minimum, cap ignored**,
       set `warn="concentration_over_60"`.
     - equity **≥ $100** → throttled honest message + skip (rare: funded but ~fully deployed account;
       never silent).
   - **Gates 3 & 4 (drawdown, daily_loss) are unchanged** — loss-breakers must still pause.
   In `execute_copy_trade`, apply `decision.max_stake` (re-floor to `exchange_min`, re-clamp to
   `tradeable`) and forward `decision.warn` to the trade notification.

2. **Sizing-mode-aware messaging.** In `worker/tasks/execute_copy.py`:
   - Soft-limit "$100" warning (`_notify_trading_at_minimum` + `equity_below_recommended`) fires
     **only in `kelly` mode** (Kelly without capital yields tiny stakes — rationale holds). In `fixed`
     mode the user chose their own size → **stay silent about balance** during trading.
   - The **concentration warning** (`warn="concentration_over_60"`) is appended as a line to the
     existing success notification `_notify` (not a separate spam message) and shows in **both** modes
     (it's a risk note, not a balance nag): e.g. "⚠️ Позиция заняла >60% капитала — риск концентрации".
   - Drop `equity_below_recommended` to `log.debug`.

3. **Onboarding one-time recommendation.** At wallet registration/setup, send **once** (idempotent via
   a `users` flag or `notify_once`): recommend choosing **Kelly** and topping up to **≥ $100**, with a
   one-line rationale. Never blocks.

4. **Centralize the "$100 recommended + why" copy** in one place, reused by onboarding, the Kelly
   low-balance warning, and the `/wallet` + sizing-settings screens, so wording stays consistent.

5. **(Hygiene, optional, unrelated to the RCA)** Celery result backend bloat observed in prod
   (`celery-task-meta-*`, 7800+ keys in Redis). Add `result_expires` (e.g. 3600s) in
   `worker/celery_app.py`.

**Files:** `core/risk.py` (Gate 1/2 clamp + `RiskDecision.max_stake/warn`), `worker/tasks/execute_copy.py`
(apply clamp; mode-gate soft-limit alert; append concentration warning to `_notify`), onboarding handler
(`core/clob.register_deposit_wallet` / Telegram setup flow — locate at implementation), shared messaging
copy module, optional `worker/celery_app.py` (`result_expires`).

**Config:** no new keys required; behavior keyed off existing `recommended_min_balance_usdc` (100),
`exchange_min_order_usdc` (5.0), `max_portfolio_exposure_pct` (0.60), `sizing_mode` ("fixed").

**Acceptance:** a ~$14 wallet with an open $5 position **still copies** (enters at the $5 minimum) and
receives a single "entered >60% of capital" concentration note instead of silence; in `fixed` mode no
per-trade balance warnings are sent; in `kelly` mode the "recommend ≥ $100" warning is shown (throttled);
funded accounts (≥ $100) keep the real exposure cap via clamping and are never silently skipped; no
`skip_risk_gate gate=exposure_cap` ever results in a silent no-trade for sub-$100 balances.

---

### Blueprint 8 — Equity accounting RCA: phantom drawdown, unified per-trade risk cap, manual override ✅ IMPLEMENTED

**Symptoms (live test, real funds):**
1. **Phantom drawdown + auto-block.** The bot enters a trade for almost the whole
   balance. The funds lock into an open (un-resolved) Polymarket position. The bot
   *immediately* records equity falling from **$18.07 → $12.95** and pauses copying
   on **"Просадка 28.3%"**, even though nothing actually lost — the position is just
   open and un-resolved.
2. **Block-alert spam.** After the pause, the user keeps receiving repeated
   "копирование приостановлено" notifications over time.
3. **(Problem 2)** A fully-lost trade in `fixed` mode wipes the entire accumulated
   profit; the stop-loss behaves inconsistently between `fixed` and `kelly`.
4. **(Problem 3)** There is no way for the user to lift a drawdown pause early.

**Root cause — three independent defects (code-cited). Do NOT "just lower the
drawdown threshold" — that hides the bug and weakens real protection.**

**✅ Confirmed by production worker logs (`drawdown_breaker_tripped`, 2026-06-26 → 29):**
```
2026-06-28 20:23:38  drawdown_breaker_tripped  drawdown=0.2834  equity=12.95  user_id=1
2026-06-29 20:27:40  drawdown_breaker_tripped  drawdown=0.2834  equity=12.95  user_id=1   ← identical, ~24h later
2026-06-27 20:19:42  drawdown_breaker_tripped  drawdown=0.6292  equity=6.70   user_id=1
2026-06-29 00:03:37  drawdown_breaker_tripped  drawdown=0.6046  equity=8.45   user_id=2
```
Two independent confirmations, both predicted above:
1. **Phantom & frozen (RCA-1).** `equity=12.95, drawdown=0.2834` is the exact reported
   symptom, and it recurs **with byte-identical values 24 h apart** (06-28 20:23 →
   06-29 20:27). Equity is *stuck* because the position is held and un-resolved — the
   breaker is reacting to a frozen mark-to-market markdown, not to any realized loss.
   The deeper trips (62.9% / 60.5%) are the same effect compounded across several open
   lots (incl. a cheap longshot: `filled=4.85 shares=28.5` ⇒ entry ≈ 0.17, marked even
   lower). All `copy_trade_ok` lines show clean `fill=full` — **nothing actually lost.**
2. **Re-pause spam loop (RCA-2).** The 24 h gap = `drawdown_cooldown_sec` (86 400 s): the
   pause expires → `sync_positions` auto-resumes → the very next cycle re-evaluates the
   **same frozen markdown** → re-trips → re-notifies. This is the user-visible
   "повторно шлёт уведомления" spam, and it will repeat every 24 h forever until the
   underlying market resolves. Cost-basis equity (RCA-1 fix) breaks the loop at the
   source; the transition-only state machine (RCA-2 fix) stops the re-notify.

#### RCA-1 — Equity is marked-to-market on the open position's depressed price

`equity = free_pusd + Σ current_value`, computed identically in three places:
- `worker/tasks/execute_copy.py:142-149` and `:176-183` (pre-trade gate),
- `worker/tasks/manage_positions.py:181-186` (`sync_positions` HWM update),
- `core/risk.py:72-74` (`open_exposure` inside the gate).

`current_value = shares × curPrice` (`core/polymarket.py:440`), where `curPrice` is
Polymarket's **live mark** (bid/mid side). The instant a BUY fills, the position is
re-priced at this mark, which for a freshly-bought fast-resolving favorite sits
**below entry** (spread + thin-book mark). So the act of opening a trade converts
cash into a position whose *marked* value is lower than what we just paid:

```
before:  free=$18.07, open=$0          → equity=$18.07   → HWM=$18.07
buy ~$15 into one position
after:   free≈$3.05,  current_value≈$9.90 (marked at curPrice, not entry)
         → equity=$12.95               → drawdown=(18.07-12.95)/18.07 = 28.3%
```

28.3% > `max_drawdown_pct` (0.25) ⇒ the breaker trips on a position that has **not
resolved and has not lost anything**. The HWM was captured on all-cash equity, then
compared against marked-down equity one cycle later. **Mark-to-market noise on open,
un-resolved positions is being treated as realized drawdown.** This is the phantom.

**Fix — drawdown/HWM must run on a REALIZED (cost-basis) equity definition.**
Opening a position is **capital-neutral**: cash becomes cost basis of equal value.
Define a single authoritative function (pure, in `core/risk.py`):

```
def total_equity(free_pusd, open_positions, ledger_cost_by_token, cfg) -> float:
    # cost_basis per open position, in AUTHORITY order:
    #   1) copy_trades.size_usdc for the matching open trade (true filled cost)   ← preferred
    #   2) shares * avg_price    (Data-API cost basis fallback)
    #   3) current_value         (last-resort mark, only if no cost basis known)
    open_cost = Σ cost_basis(p)  for p in open_positions if p.shares > 0
    return free_pusd + open_cost
```

- The **drawdown circuit breaker, HWM, exposure cap, and event cap** all switch to
  this cost-basis equity (`drawdown_equity_mode="cost_basis"`, default). With it,
  the buy above gives `equity = 3.05 + 15.00 = 18.05 ≈ HWM` → **drawdown ≈ 0%**, no
  false trip. A breaker only fires when a position **actually resolves/closes at a
  loss** — and that realized loss is already booked on the `copy_trades` ledger
  (`realized_pnl`, BP1/BP6) and flows through `get_daily_realized_pnl`.
- Mark-to-market (`current_value`/`cur_price`) stays for **display only** (P&L
  screens, position lists) — never for the breaker.
- `drawdown_equity_mode="mark"` preserves the legacy behaviour for instant rollback.

> **Why cost-basis, not mark:** binary Polymarket positions are held to resolution
> (§2.5). Their interim mark is economically meaningless (illiquid, wide spread) and
> mean-reverts to $1 or $0 at resolve. Drawdown protection must react to **realized**
> capital destruction, not to transient bid marks on positions we intend to hold.

#### RCA-2 — Broken pause state machine → duplicate / repeated block alerts

- **Two notifiers, two throttle keys.** The drawdown pause is announced from
  *both* `manage_positions._update_hwm_and_check_breakers` (`:914`,
  `notify_once("drawdown_alert:{uid}", ttl=86400)`) *and*
  `execute_copy_trade` (`:331`, `notify_once("risk_gate:{uid}:drawdown", ttl=3600)`).
  Different keys ⇒ the user gets **two** alerts per event, and the 1 h key lets the
  execute path **re-alert every hour** while paused.
- **Re-pause every cycle.** `sync_positions` runs every 120 s; while equity stays
  below the HWM threshold it calls `pause_user_copying` again each pass, pushing
  `copy_paused_until` forward — so a *persistent* (phantom) markdown can keep the
  account paused indefinitely and re-evaluate forever. There is no explicit state,
  only an inferred timestamp.

**Fix — one explicit state machine, notify once on transition.** Add a
`users.risk_state` column and make the *transition* (not the evaluation) the event:

```
states: active → paused_drawdown → active
        active → paused_daily_loss → active
        paused_* → override_active → active   (manual unblock, RCA-3)

transition rules (evaluated only in sync_positions / the single breaker owner):
  active        & drawdown ≥ max_drawdown_pct  → set paused_drawdown,
                                                 set copy_paused_until,
                                                 send ONE notification (with unblock button)
  paused_*      & cooldown elapsed             → set active (auto-resume), notify once
  paused_*      & still tripped                → DO NOTHING (no re-pause, no re-notify)
```

- **Single owner of the notification.** Only `sync_positions` sends the pause alert
  (on the `active → paused_*` edge). `execute_copy_trade` becomes a pure *enforcer*:
  it still honors `copy_paused_until` and skips the trade, but **never sends the
  pause notification** (drop the `_notify_risk_pause` call for the `drawdown`/
  `daily_loss` gates; keep it only for genuinely new info if any). This removes the
  second path entirely.
- **Idempotent transition guard.** The notification fires only when
  `risk_state` actually changes value (compare-and-set), so repeated evaluations
  while already `paused_drawdown` send nothing — independent of any Redis TTL.
- Keep a Redis `notify_once` as a belt-and-suspenders cross-process guard, but the
  DB `risk_state` edge is the source of truth.

#### Problem 2 — Unified Risk-per-Trade Cap (mode-agnostic stop-loss)

**Root cause:** `max_risk_per_trade` (0.05 of equity) is enforced **only inside
`kelly_stake`** (`core/sizing.py:94`). The `fixed` branch
(`execute_copy.py:169-172`) sizes `min(user_max, depth_cap)` with **no** per-trade
equity ceiling. A user with `max_position_usdc=$25` on a `$30` balance bets ~83% of
equity; one fully-lost binary trade (worst case = the entire stake) erases the whole
account / all accumulated profit.

**Fix — hoist the cap out of Kelly into one place that runs in BOTH modes.** After
sizing (Kelly *or* fixed) and before the BP4/BP7 gates, in `execute_copy_trade`:

```
# Unified per-trade risk cap — applies regardless of sizing_mode.
if cfg.enforce_risk_per_trade_cap:
    hard_cap = cfg.max_risk_per_trade * equity        # e.g. 0.05 × equity
    size_usdc = min(size_usdc, hard_cap)

# Profit-protection (trailing): never let one trade's worst-case loss (= full stake
# for a binary outcome) give back more than a fraction of accumulated realized profit.
profit_above_baseline = max(0.0, equity_hwm - realized_baseline)   # see note
if cfg.max_trade_loss_vs_profit_pct > 0 and profit_above_baseline > 0:
    size_usdc = min(size_usdc, cfg.max_trade_loss_vs_profit_pct * profit_above_baseline
                               + cfg.max_risk_per_trade * realized_baseline)

# Then the existing BP7 small-balance semantics still apply:
size_usdc = max(size_usdc, exchange_min)   # floor (small accounts still trade)
size_usdc = min(size_usdc, tradeable)      # never spend more than free pUSD
```

- This makes the stop-loss **structural**: the maximum loss of any single trade is
  bounded by `max_risk_per_trade × equity` (5%), so ~20 consecutive worst-case losses
  would be needed to halve the account — and the drawdown breaker (now correct, per
  RCA-1) stops it long before. One fixed trade can no longer "сжечь весь профит".
- `kelly_stake` keeps its internal `max_risk_per_trade` clamp (harmless — the hoisted
  cap is identical), but the cap is **no longer mode-dependent**.
- The exchange-minimum floor (BP3.1/BP7) still wins for tiny accounts: a sub-$100
  wallet may exceed 5% on a single $5 order — that is the intended, warned override
  (`concentration_over_60`), not a regression.

> **`realized_baseline`** = the equity recorded when the user last reset their risk
> baseline (registration, top-up, or manual override). Stored alongside the HWM.
> If unset, treat `realized_baseline = current equity` (no profit to protect yet).

#### Problem 3 — Manual override: "Снять блокировку" inline button

**Design — a one-tap consented reset of the drawdown baseline.**

1. **Button on the pause message.** The single pause notification (RCA-2) attaches:
   ```
   InlineKeyboardMarkup([[InlineKeyboardButton(
       "🔓 Снять блокировку", callback_data="unlock_drawdown")]])
   ```
   (Sent via `manage_positions._notify`; add an optional `reply_markup` arg, or send
   the keyboard through the bot directly. The user bot's `callback_handler`
   (`api/routers/telegram.py:1155`) is the dispatcher — add an `unlock_drawdown` branch.)

2. **Handler logic (`callback_data == "unlock_drawdown"`):**
   ```
   db_user = get_user_by_telegram_id(tg.id);  require ownership
   if db_user.risk_state not in ("paused_drawdown", "paused_daily_loss"):
       answer("Блокировка уже снята"); return
   equity = total_equity(...)                 # current cost-basis equity
   reset_risk_baseline(uid, equity)           # equity_hwm = equity  → drawdown resets to 0
   record_risk_override(uid)                  # risk_override_at=now, risk_override_count += 1
   resume_user_copying(uid)                   # copy_paused_until = NULL
   set_risk_state(uid, "active")              # NOT 'override_active' permanently — back to normal guard
   clear Redis keys: drawdown_alert:{uid}, risk_gate:{uid}:drawdown  # so future REAL DD can alert again
   edit message → "✅ Блокировка снята. Ты берёшь риск на себя. Точка отсчёта просадки сброшена."
   ```

3. **Consent / liability record.** `risk_override_at` (timestamptz) +
   `risk_override_count` (int) on `users` are the audit trail that the user accepted
   responsibility. Log `risk_override_manual` at `info` (user_id, old_hwm, new_hwm).
   Resetting the HWM to current equity makes the **current** equity the new peak, so
   the breaker measures drawdown *from here forward* — exactly "сбросить точку отсчёта".

4. **State after override:** `risk_state='active'`, `copy_paused_until=NULL`, HWM reset.
   The normal breaker is fully armed again from the new baseline (a *further* real
   drop still protects them). We do **not** disable the breaker — we only reset its
   reference point, per the user's consent.

**State machine (combined):**
```
            drawdown ≥ 25% (cost-basis)              cooldown elapsed
  active ──────────────────────────────▶ paused_drawdown ───────────────▶ active
    ▲                                         │
    │            tap "🔓 Снять блокировку"     │
    └─────────────────────────────────────────┘   (reset HWM = equity, record consent)
```

**Files:**
- `core/risk.py` — `total_equity()` (pure, cost-basis); breaker reads cost-basis equity;
  Gates 1–4 use cost-basis `open_exposure`.
- `worker/tasks/manage_positions.py` — `_update_hwm_and_check_breakers` rewritten as a
  compare-and-set state machine (notify once on edge, no re-pause, auto-resume on edge);
  `_notify` gains an optional `reply_markup` for the unblock button; `sync_positions`
  computes cost-basis equity (pass ledger cost via `get_open_trades_cost(uid)`).
- `worker/tasks/execute_copy.py` — apply the **unified per-trade risk cap** in both
  modes after sizing; compute equity cost-basis; **stop** sending the drawdown/daily-loss
  pause notification (enforce-only).
- `core/sizing.py` — unchanged math; the cap is now also enforced by the caller.
- `api/routers/telegram.py` — `unlock_drawdown` branch in `callback_handler`.
- `core/db/queries.py` — `set_risk_state`, `get_risk_state`, `reset_risk_baseline`,
  `record_risk_override`, `get_open_trades_cost(user_id) -> {token_id: size_usdc}`,
  `get_realized_baseline(user_id)`; `get_active_subscribers` already honors
  `copy_paused_until` (keep).
- `core/config.py` — new knobs (below).
- `migrations/013_risk_state_override.sql`.

**Migration 013 (idempotent):**
```sql
-- Blueprint 8: risk state machine + manual drawdown override + realized baseline.
alter table users add column if not exists risk_state text default 'active';
  -- active | paused_drawdown | paused_daily_loss
alter table users add column if not exists risk_override_at timestamptz;
alter table users add column if not exists risk_override_count int default 0;
alter table users add column if not exists realized_baseline double precision;
  -- equity snapshot used for profit-protection; reset on top-up / manual override
```

**Config (`core/config.py`):**
```python
# ── Blueprint 8: equity accounting + unified per-trade risk cap ───────────────
# Equity definition used by the drawdown breaker / HWM / exposure gates.
# "cost_basis" = open positions valued at filled entry cost (no phantom drawdown).
# "mark"       = legacy mark-to-market on curPrice (rollback only).
drawdown_equity_mode: str = "cost_basis"
# Apply max_risk_per_trade × equity as a hard ceiling in BOTH fixed and kelly modes.
enforce_risk_per_trade_cap: bool = True
# Profit-protection: a single trade's worst-case loss may not give back more than
# this fraction of accumulated realized profit above the baseline (0 disables).
max_trade_loss_vs_profit_pct: float = 0.50
```
(`max_risk_per_trade=0.05`, `max_drawdown_pct=0.25`, `drawdown_cooldown_sec=86400`
are reused unchanged.)

**Acceptance:**
- Opening a position no longer moves cost-basis equity ⇒ **no phantom drawdown**; the
  $18.07→$12.95 mark-to-market dip does **not** trip the breaker. The breaker fires
  only on **realized** losses (resolution/close), and the $18.07→$12.95 figure now
  shows only as an informational unrealized mark on the P&L screen.
- A drawdown pause produces **exactly one** notification (on the `active→paused_drawdown`
  edge), carries an unblock button, and is **not** repeated on subsequent sync cycles
  or by the execute path; it auto-resumes once after cooldown.
- In **both** `fixed` and `kelly` modes a single trade risks ≤ `max_risk_per_trade`
  (5%) of equity (modulo the warned sub-$100 exchange-minimum override); one lost trade
  can no longer wipe accumulated profit.
- Tapping "🔓 Снять блокировку" resets the HWM to current equity, records consent
  (`risk_override_at`, `risk_override_count`), clears the pause, returns `risk_state`
  to `active`, and re-arms the breaker from the new baseline; the action is logged and
  the message updates in place. Behaviour is restart-safe (state in the DB, not Redis).

---

### Blueprint 9 — Release-integrity RCA: silent callback crash + dead redemption safety-net ✅ IMPLEMENTED

**Symptoms (live test, real funds):**
1. **Telegram navigation dead.** From any deeper screen (settings / positions / wallet),
   tapping "🏠 Главное меню" does nothing — the bot ignores the press, no return to root.
2. **Late resolve + no claim.** A resolved Polymarket event sent "Событие выиграно! +1.25$"
   only ~24 h later, and **no USDC was credited** (deposit-wallet balance unchanged).

**✅ Confirmed by production logs (2026-06-29), do NOT re-diagnose from symptoms:**
```
# api container — once PER button press, swallowed (no error handler):
No error handlers are registered, logging exception.
  File "/app/api/routers/telegram.py", line 1169, in callback_handler
    if not settings.auto_copy_enabled:
UnboundLocalError: cannot access local variable 'settings' where it is not associated with a value

# worker container — every 600 s:
Task worker.tasks.backfill_legacy_redemptions ... raised unexpected:
  ImportError("cannot import name 'has_terminal_trade' from 'core.db' (/app/core/db/__init__.py)")
reconcile_settlements_done   checked=6 processed=0          # runs, but redeems nothing
# beat schedules every task correctly; all 4 containers Up; redis healthy.
```

**Infra reminder:** this stack is **Docker Compose, not PM2** (§7.1). Diagnose with
`docker compose logs --no-color {api|worker|beat}`, not `pm2 logs`.

#### Root cause — two code defects, one shared meta-cause (RELEASE DRIFT)

The running images execute **older code than the working tree**. Both fixes already exist
locally but were never committed / pushed / rebuilt, so `git pull` + `docker compose build`
on the VPS baked the stale versions. Evidence: `core/db/__init__.py` **already exports**
`has_terminal_trade` locally (lines 19/63) yet the container throws `ImportError`; and
`callback_handler` already carries the comment *"settings is already imported at module
level — no local re-import"* (`telegram.py:1579`) yet the container still shadows it. The
relevant files show as **untracked** in `git status` (`core/db/queries.py`, `core/risk.py`,
`core/config.py`, `api/routers/telegram.py`, `worker/tasks/manage_positions.py`,
`worker/tasks/execute_copy.py`, `migrations/013_*`).

**Defect A (Problem 1) — `settings` shadowed as a function-local.**
Python binds a name as **function-local for the entire scope** if it is assigned/imported
*anywhere* in that function. The deployed `callback_handler` re-imports `settings`
(`from core.config import settings`) inside one of its branches, so the **first** read at
`telegram.py:1169` (`if not settings.auto_copy_enabled:`, the `data == "menu"` branch)
raises `UnboundLocalError`. Because `query.answer()` already fired (`:1159`) **and there is
no `app.add_error_handler`** (`:359-377`), the exception is logged by PTB and the user just
sees a dead button. This breaks **every** callback path that touches `settings`
(menu / help / wallet / …), not only "Главное меню". The FSM hypothesis is wrong — there is
no `ConversationHandler`; navigation is a single global `CallbackQueryHandler`.

**Defect B (Problem 2) — the on-chain redemption safety-net is dead, and the win
notification is decoupled from the actual claim.**
1. `backfill_legacy_redemptions` (the catch-all that redeems neg-risk/legacy winners the
   Data API hides) imports `has_terminal_trade` from `core.db`
   (`manage_positions.py:740`), which the **deployed** `core/db/__init__.py` does not export
   → `ImportError` on **every** run → the task never does anything.
2. `reconcile_settlements` runs but `processed=0` (the 6 outstanding rows are not detected as
   resolved on-chain via `is_condition_resolved`, `manage_positions.py:614`).
3. The Data-API `redeemable` branch in `sync_positions` never fires for neg-risk (Blueprint 1).
4. The only path that *did* fire was `sync_positions`' **closed-positions branch**
   (`manage_positions.py:159-175`): it sends `_emit_win` (the "+$1.25") **but never dispatches
   a redeem**. That is where the late, content-rich message came from (~1 day later, when the
   position finally surfaced as closed in the Data API).
5. **Net:** no `redeemPositions` tx was ever sent → balance unchanged, exactly the symptom.

**Secondary defect (money-safety, independent of the deploy gap).** In `sync_positions`
(`:97-115`) the win **notification** and the **redeem** dispatch sit behind *separate* Redis
guards (`settle:` vs `redeem:`), and the closed-positions branch notifies with **no** redeem
at all. So a "выиграно" message is **never proof of an on-chain credit** — the core invariant
this blueprint must restore.

#### Design — three layers (do all three; Layer 1 is the hotfix, 2–3 are the architecture)

**Layer 1 — Immediate correctness (commit + redeploy the working tree).**
- Ensure `callback_handler` has **no** function-local rebinding of `settings`; rely on the
  module-level import (`telegram.py:16`). (Working tree already correct.)
- Ensure `has_terminal_trade` (+ `get_open_trade_by_token`, `mark_trade_closed`,
  `get_open_trades_cost`, …) exist in `core/db/queries.py` **and** are listed in
  `core/db/__init__.__all__`. (Working tree already correct.)
- Commit the untracked files, push, apply any unapplied migrations (008–012 per §7.4), then
  `docker compose build --no-cache api worker beat && docker compose up -d`.

**Layer 2 — Fail-loud, never-silent (the architectural fix that would have caught both).**
- **(2a) Global PTB error handler.** Register `app.add_error_handler(on_error)` on **both**
  bots. Log structured (`telegram_callback_error`, `data`, `user_id`) and reply with a safe
  fallback ("⚠️ Что-то пошло не так — открой /start"), so a handler exception can never again
  produce a silent dead button. Converts Problem 1 from invisible to logged + recoverable.
- **(2b) Import-time integrity self-check.** At api/worker boot **and** in CI, import every
  task module and assert that all names in `core.db.__all__` resolve
  (`python -c "import worker.tasks, api.routers.telegram"`). A missing export then fails the
  container HEALTHCHECK / CI **loudly at deploy time**, instead of crashing one periodic task
  forever. Prefer module-top imports over per-function `from core.db import …` so an
  ImportError surfaces at import, not silently per-run.
- **(2c) Crashed-periodic-task alerting.** A Celery `task_failure` signal handler (or a beat
  watchdog) escalates when a periodic task — especially `backfill_legacy_redemptions` /
  `reconcile_settlements` — raises repeatedly, so a dead safety-net pages us within minutes
  instead of rotting silently for a day.

**Layer 3 — Couple the win message to a CONFIRMED on-chain credit (money-safety invariant).**
- **Never** send "Выигрыш зачислён +$X" until `redeem_winnings` **and**
  `convert_dw_usdce_to_pusd` have **confirmed** (a tx hash + observed pUSD balance delta).
- On resolution detection, send an interim "🏁 Событие выиграно — оформляю зачисление…"
  (pending); send the final "✅ Выигрыш зачислён: +$X (pUSD)" **only** after the redeem+wrap
  relayer batch confirms. On failure, send "⏳ Выигрыш определён, зачисление задерживается —
  повторяю" and keep the row in the outstanding queue for idempotent retry — never a false
  success.
- Concretely: move the terminal win emit **into `redeem_position` after `redeem_done`**
  (gate it on `mark_trade_settled` success), and make `sync_positions`' closed-positions
  branch (`:159-175`) **not** emit a terminal win without also ensuring redemption
  (dispatch `redeem_position` / leave to reconcile). Keep `notify_once`/`claim` dedup so
  retries never double-notify. Result: **notification ⟺ credited**, always.

**Files:** `api/routers/telegram.py` (error handler; verify no `settings` shadow),
`api/main.py` + `worker/entrypoint.py` (boot self-check / healthcheck), `core/db/__init__.py`
(verify exports), `worker/tasks/manage_positions.py` (win emit moved post-redeem;
closed-branch must not falsely claim a win), `worker/celery_app.py` (`task_failure` alert
hook). **No new migration required.**

**Acceptance:**
- Every inline button works; any future handler exception shows a fallback **and** a
  structured log — never a dead button.
- A missing `core.db` export (or any task-module ImportError) **fails CI / the container
  healthcheck at deploy**, and pages on repeated runtime failure; the redemption safety-net
  can never again be silently disabled.
- A won position credits pUSD on-chain (**confirmed tx**) **before** any "зачислён" message;
  on redeem failure the user sees a pending/retry state, never a false success; **no win
  notification is ever sent without a corresponding confirmed credit.**

---

### Other known gaps (lower priority)

- **No automated tests / CI.** The new `core/sizing.py` and `core/risk.py` are pure functions — add
  `pytest` unit tests for them first (deterministic, no chain/API).
- **SELL copying not supported** — only BUY signals are mirrored.
- **Geoblock dependency** — `auto_copy_enabled` must only be ON where Polymarket trading is allowed.
- **Beat single-replica** requirement is operational, not enforced (2 beats double-fire).
- **Model A dormant** — `ws_listener.py`/`signals.py` not wired in; `scan_markets.py`/`poll_donors.py`
  off-schedule.
- **`wallet_filter_mode` defaults to `observe`** — buyer track-record logged but not enforced.
- **Single subscription tier** — no pricing/feature differentiation.
- **`get_closed_positions` capped at `limit=100`** — long histories truncated (affects winrate stats).

---

### Blueprint 10 — Delta-Drop Stop-Loss (simple, data-informed) 🟡 FINAL DESIGN / READY TO IMPLEMENT

**Product decision (Product Owner, 2026-06-30):** reject heavy quant (trailing stops, R/R entry
filters). Ship a **dumb, robust "Delta-Drop" stop**: if the outcome price falls by **X** from our
entry, sell the shares into the CLOB and exit. If price rises or holds, hold to resolution (that
branch is profitable). One knob, predictable behaviour.

#### Diagnosis recap (confirmed by prod logs + DB, do NOT re-diagnose)

**Finding 1 — there is no working stop; the bot is pure hold-to-resolution.** The only exit in
`sync_positions` is `cur_price < hard_stop_abs_price (0.07)` (`manage_positions.py:161-169`), and
it is disabled in the final `tp_sl_min_hours=4 h` (`:144-145`) and blind to neg-risk tokens that
delist from the Data API before reaching 0.07. A 12 h grep over `{hard_stop_triggered, sell_placed,
sell_order_failed, position_closed, no_bid}` returned **zero** matches and the ledger has
**`exit_tx = 0`** across all 368 rows → `close_position` was **never** invoked. The monitoring loop
itself was healthy (`sync_positions_done` every 120 s, no failures).

**Finding 2 (CRITICAL, blocks data-driven calibration) — the P&L ledger is corrupt.** Census of
`copy_trades` (368 rows): 343 `failed`, 19 `confirmed`/`result=NULL`, 5 `loss`, 1 `win`;
`result IS NULL` for 362/368; only 1 `redeem_tx`. The single `win` (id 695, entry 0.69) has a real
on-chain `redeem_tx` but `realized_pnl = -4.88` (booked as −99.9 %) because `realized_pnl =
credited − entry_cost` and `credited = bal_after − bal_before` measured ≈ $0 (the USDC.e→pUSD wrap
had not settled when the balance was read). **Losses book correctly** (via on-chain
`get_payout_numerator == 0`); **wins are systematically mis-booked or left NULL.** Net effect: the
ledger shows a false −$26 when the live portfolio is positive.

**Consequence for calibration:** `realized_pnl` is unusable, and the intra-trade **price path /
drawdown is not stored anywhere** (logs only have `order_placed` on entry + `sync_positions_done`
counts). Only `entry_price` is reliable (verified by arithmetic, e.g. id 714: 4.85/0.17 = 28.5
shares ✓). Therefore **X cannot be fitted to historical drawdown** — it is chosen from first
principles below and tuned later from the new `position_mark` logs.

#### Why a RELATIVE X (% of entry), not absolute cents

For a binary, the dollar loss when stopped is:
`loss_$ = shares · (entry − exit) = (size/entry) · (entry − exit) = size · (1 − exit/entry) = size · X`.
So a **relative** Delta-Drop caps the dollar risk at exactly **`X · size` regardless of entry
price** — the predictable, "dumb" property we want. Absolute cents give a floating risk that is
worse at low entries. → **use relative X.**

#### Approved parameters (PO sign-off 2026-06-30)

| Knob | Value | Rationale |
|---|---|---|
| `delta_drop_stop_pct` (**X**) | **0.30** | exit when `best_bid ≤ entry × 0.70`; caps loss at ≈30 % of stake. Wide enough to ride normal prediction-market noise on eventual winners, tight enough to convert −100 % wipeouts into −30 %. |
| `min_entry_price` | **0.40** (was 0.05) | **Do not enter outcomes priced < 0.40.** Solves the dead-zone case (e.g. the 0.17 loss) at the source: a 30 % drop from ≥0.40 exits at ≥0.28, where the book still has bids and the stop can actually fill. |
| `hard_stop_abs_price` | **0.07** (kept) | residual lower floor, runs alongside Delta-Drop. Harmless no-op in normal operation. |
| mark logging | **ON** | one `position_mark` log line per cycle → calibrate the optimal X from real drawdown after 1–2 weeks. |

**What X=0.30 would have done to the real (correctly-booked) losers, if the stop had filled:**
id 710 (0.77) −$5.03→≈−$1.51 · id 712 (0.59) −$4.96→≈−$1.49 · id 711 (0.526) −$4.75→≈−$1.43 ·
id 697 (0.42) −$2.02→≈−$0.61. The win (695 @ 0.69, resolved up) is never touched. (Assumes Step 2
live-book exit is in place.)

#### Step-by-step implementation

**Step 1 — Config (`core/config.py`).**
```python
# Blueprint 10 — Delta-Drop stop-loss (relative drop from entry)
delta_drop_stop_pct: float = 0.30          # exit when best_bid <= entry_price * (1 - X)
delta_drop_min_hold_sec: int = 600         # ignore the first 10 min (avoid entry-tick whipsaw)
log_position_marks: bool = True            # emit one position_mark line per open position per cycle
```
Also change the existing `min_entry_price: float = 0.05` → `0.40`. Keep `hard_stop_abs_price = 0.07`
and `tp_sl_min_hours` as-is (the Delta-Drop path below ignores the `tp_sl_min_hours` skip).

**Step 2 — Delta-Drop checker in `sync_positions`** (`worker/tasks/manage_positions.py`, replace the
hold/hard-stop block `:135-169`). For each open position with `shares > 0` and not `redeemable`:
1. `entry = float(p.get("avg_price") or 0)` (fallback to the ledger `entry_price` via
   `get_open_trade_by_token` if `avg_price` is missing — neg-risk safety).
2. `book = get_order_book(token_id)`; `best_bid = float(book.get("best_bid") or 0)`.
   **Use the live CLOB book, not the Data-API `cur_price`** (the book does not delist neg-risk early
   and is the price we can actually exit at).
3. **Mark logging** (when `log_position_marks`): emit
   `log.info("position_mark", token=token_id[:14], entry=entry, best_bid=best_bid,
   drop=round(1-best_bid/entry,3), hours=...)` — this is the dataset for future X tuning.
4. Enforce `delta_drop_min_hold_sec` (reuse the existing `_first_seen` map) to skip very fresh
   positions.
5. **Trigger:** if `entry > 0 and best_bid > 0 and (1 - best_bid/entry) >= delta_drop_stop_pct` →
   `_closing.add(ckey); close_position.delay(uid, token_id, "delta_drop_stop"); continue`.
   **Do NOT apply the `hours < tp_sl_min_hours: continue` skip on this path** (that guard is what
   let the loss ride to zero — Finding 1).
6. Leave the existing `hard_stop_abs_price (0.07)` check below as the residual floor (unchanged).

**Step 3 — Emergency sell = reuse existing `close_position`** (`manage_positions.py:262`). It already
places a marketable FAK SELL into the book at `best_bid` with `exit_slippage_pct` and books P&L
(Blueprint 6). Because `min_entry_price=0.40` keeps us out of dead books, the existing
`best_bid < 0.01 → no_bid` abort (`:303-327`) will not fire on a Delta-Drop exit. **No change to the
sell logic needed** — only add the `"delta_drop_stop"` label to `_notify_closed` (`:592-613`):
`"delta_drop_stop": "🛑 Стоп-лосс (−30% от входа)"`.

**Step 4 — Entry filter** is already enforced at `execute_copy.py:312-321` (and `detector.py:142`);
it reads `settings.min_entry_price`, so raising the config value to 0.40 is sufficient — no new code.

**Cadence note:** v1 runs inside the existing 120 s `sync_positions` loop (simplest, "dumb"). If a
faster reaction is needed later, lift the Delta-Drop block into a dedicated 30 s task — the logic is
identical.

#### Acceptance criteria / test plan
- Unit: pure helper `delta_drop_hit(entry, best_bid, X) -> bool` (deterministic, no I/O) with cases
  0.77→0.50 (hit), 0.77→0.60 (no), 0.69→1.0 (no, win), entry/bid ≤ 0 (no).
- Integration (staging): open a tiny position, push a synthetic book where `best_bid ≤ entry·0.70`,
  assert `delta_drop_triggered` + `position_closed reason=delta_drop_stop` + a non-empty `exit_tx`
  written to the ledger.
- Prod smoke: confirm `position_mark` lines appear every cycle, and `min_entry_price=0.40` rejects a
  sub-0.40 copy with `skip_price_out_of_range`.

> **Out of scope → Blueprint 11 (separate):** the P&L booking bug (Finding 2). `realized_pnl` must be
> sourced from the actual redeemed proceeds / `sell_position` fill, not a fragile wallet
> balance-delta. Until fixed, the daily-loss / drawdown circuit breakers (Blueprint 4/8) are fed
> phantom −100 % wins and cannot be trusted.

---

### Blueprint 12 — Close-handler import regression + withdrawal balance fix & FSM redesign ✅ IMPLEMENTED

> **Numbering note:** the prompt asked for "Blueprint 11", but Blueprint 11 already exists (the
> P&L-booking fix, IMPLEMENTED). This work is filed as **Blueprint 12** to avoid overwriting it.

Two unrelated prod incidents reported on 2026-06-30, bundled here because both are operational
breakages on the money path. Root causes were confirmed by code inspection — **no `pm2 logs`
were required** (see the optional verification commands at the end if you want runtime confirmation).

---

#### Part A — Bug A: "Закрыть #N" crashes with `ImportError`

**Symptom:** tapping **❌ Закрыть #1** alerts:
`cannot import name 'get_open_trade_by_token' from 'core.db' (/app/core/db/__init__.py)`.

**Exact crash path:**
1. `api/routers/telegram.py` (`close_` callback, ~L1435) dispatches `close_position.delay(uid, token_id, "manual")`.
2. The worker task `worker/tasks/manage_positions.py::close_position` runs and, inside its `try:` block, executes:

```381:381:worker/tasks/manage_positions.py
        from core.db import get_open_trade_by_token, mark_trade_closed
```

3. `get_open_trade_by_token` **is defined** in `core/db/queries.py` (L454) but is **not re-exported**
   from `core/db/__init__.py` — it is missing from both the `from core.db.queries import (...)` block
   and from `__all__`. (`mark_trade_closed` *is* exported, so only the first name fails.)
4. The `ImportError` is caught by `close_position`'s `except Exception as exc:` (L439), retried twice,
   and after `max_retries` the task notifies the user with `…<code>{str(exc)[:200]}</code>` — which is
   the exact ImportError text the user sees in the alert.

**This is a repeat of the Blueprint 9 Layer-1 class of bug** (`has_terminal_trade` was missing from
`core.db` exports). The package boundary `core/db/__init__.py` is hand-maintained and drifts from
`queries.py`.

**Fix (one file, two edits):** in `core/db/__init__.py`
1. Add `get_open_trade_by_token` to the `from core.db.queries import (...)` import block (alphabetical,
   next to `get_open_trades_cost`).
2. Add `"get_open_trade_by_token",` to `__all__`.

**Hardening (prevent the whole class, pick at least the first):**
- Extend the BP9 Layer-2 boot self-check `_check_core_imports()` to assert every name `close_position`,
  `redeem_position`, `reconcile_settlements`, and `backfill_legacy_redemptions` import from `core.db`
  (`get_open_trade_by_token`, `mark_trade_closed`, `mark_trade_settled`, `has_terminal_trade`,
  `get_outstanding_copy_trades`, `get_open_trades_cost`, `get_supabase`, …). Fail loud at boot, not
  on the money path.
- Add a trivial test `tests/test_db_exports.py` that asserts `import core.db` then `getattr(core.db, n)`
  for every `n in core.db.__all__`, **and** that every public `def` in `queries.py` used by workers is
  present in `__all__`. This makes the drift a red CI, not a prod alert.

**Acceptance (Bug A):** with the export added, `python -c "from core.db import get_open_trade_by_token"`
succeeds; closing a live position writes a non-empty `exit_tx` + `realized_pnl` to the `copy_trades`
row and the user gets "✅ Позиция закрыта" instead of the ImportError alert.

---

#### Part B — Bug B: withdrawal reads/sends the wrong balance + UX redesign

**Symptom:** user has **$7.00 USDC**, taps Вывод, and gets
*"Недостаточно средств: доступно $0.00, запрошено $7.00"*.

**Root cause (two compounding defects):**

1. **Wrong wallet for the "available" figure.** The amount step in
   `api/routers/telegram.py` (~L1134–1139) computes the balance from the **EOA**:
   `addr = db_user.get("wallet_address")` → `get_balances(addr)["total_usdc"]`. But in V2 the user's
   liquid funds live as **pUSD in the deposit wallet** (`deposit_wallet_address`), which `get_balances`
   only reads when called on that wallet. So the EOA shows ≈$0 even though the trading wallet holds $7.

2. **Wrong asset checked at transfer time + silent conversion failures.**
   `worker/tasks/wallet_ops.py::withdraw_funds` tries to assemble **native USDC on the EOA** by:
   pull pUSD from deposit wallet → EOA (relayer), unwrap pUSD→USDC.e, swap USDC.e→native USDC — but
   **every leg is wrapped in a swallowing `try/except` that only logs a warning**. If the relayer pull
   or a swap fails (e.g. no POL for gas, relayer busy), execution proceeds anyway to
   `transfer_usdc(..., use_bridged=False)`, whose own balance guard (`core/polygon.py` L267–270) finds
   **0 native USDC** and raises the exact *"доступно $0.00, запрошено $7.00"* message. The user's hunch
   ("checks locked pUSD vs free USDC") is the right shape: the *displayed* balance and the *actually
   transferable* balance are computed from different wallets/assets and never reconciled.

3. **No pre-flight validation.** The amount is accepted with only a `>= $1` check; the real
   insufficiency surfaces deep inside an on-chain revert instead of an upfront, friendly message.

**Design principle — ONE source of truth for "withdrawable":**

Add a single helper (put it in `core/polygon.py` or a thin wrapper in the telegram router) and use it
for **both** the displayed "Доступно" **and** the pre-flight validation **and** the task's own guard:

```python
def withdrawable_usdc(db_user: dict) -> float:
    """Total liquid USD the bot can actually convert + send out, across both wallets.
    = deposit-wallet pUSD (trading collateral)  +  EOA (pusd + usdc_e + usdc native)."""
    eoa = db_user.get("wallet_address")
    dw  = db_user.get("deposit_wallet_address")
    dw_pusd = get_balances(dw).get("pusd", 0.0) if dw else 0.0
    e = get_balances(eoa) if eoa else {}
    return dw_pusd + e.get("pusd", 0.0) + e.get("usdc_e", 0.0) + e.get("usdc", 0.0)
```

This is exactly the pool `withdraw_funds` already tries to mobilize, so display, validation, and
execution can never disagree again.

**New withdrawal FSM (replaces the current address → amount → confirm flow).**
State key: `context.user_data["withdraw_step"]`. The existing handlers in `telegram.py` (cmd_withdraw
~L617, the `withdraw_start`/`withdraw_cancel`/`withdraw_confirm` callbacks ~L1363, and the text router
~L1117) are refactored — do **not** add a parallel flow.

| # | State | Bot action | On valid input → |
|---|-------|-----------|------------------|
| 1 | `start` (button `withdraw_start` or `/withdraw`) | reset any stale `withdraw_*` keys; set step=`address`; prompt: **"Введите адрес кошелька (Polygon) для вывода:"** + ❌ Отмена | `address` |
| 2 | `address` (text in) | `from core.polygon import is_valid_address`; reject non-`0x…`/bad-length with a re-prompt | store `withdraw_to`; step=`amount` |
| 3 | `amount` prompt | compute `avail = withdrawable_usdc(db_user)`; prompt: **"Введите сумму для вывода (Доступно: {avail:.2f} USDC)"** + ❌ Отмена | — |
| 4 | `amount` (text in) | parse (`$`,`,`→`.`); validate `>= MIN_WITHDRAW_USDC` (1.0) **and `<= avail`** (recompute `avail` fresh here — this is the pre-flight gate). On `> avail`: friendly "Недостаточно средств. Доступно: {avail:.2f} USDC" + re-prompt | store `withdraw_amount`; go to execute |
| 5 | execute | edit message to **"⏳ Выполняю вывод…"**; clear `withdraw_*` step keys; `withdraw_funds.delay(uid, to, amount)` | task runs |
| 6 | task success | `transfer_usdc` waits for the receipt; task notifies **"✅ Вывод успешно завершён.\nТранзакция: https://polygonscan.com/tx/{tx}"** | done |

> **Confirm step:** the prompt's 6-step spec omits the old "✅ Подтвердить" screen, so this design
> goes straight from amount → execution. If you'd rather keep an explicit money-movement confirmation
> (recommended for safety), insert a one-tap confirm between steps 4 and 5 showing amount + address;
> it does not change any of the balance logic below. **Decision left to the implementer; default to
> following the 6-step spec (no confirm).**

**Fixes inside `withdraw_funds` (`worker/tasks/wallet_ops.py`) — fail loud, never on bad data (§5.2):**
1. **Pre-flight guard:** recompute `avail = withdrawable_usdc(user)` at task start; if `amount > avail`,
   notify a clear "Недостаточно средств: доступно ${avail:.2f}" and return **before** any on-chain
   action. (Belt-and-suspenders with the UI gate, in case balances moved.)
2. **POL/gas pre-check** on the EOA before conversions; if `< ~0.02 POL`, notify "⛽️ Недостаточно POL
   на газ" and abort (mirror `wrap_collateral`).
3. **Stop swallowing conversion errors.** The relayer pull / unwrap / swap legs must surface failures:
   on any leg failure, abort with a specific message ("не удалось вывести средства из торгового
   кошелька / конвертация не удалась — попробуйте позже") instead of falling through to a misleading
   "$0.00" transfer revert.
4. **Confirm the tx before declaring success.** `core/polygon.py::transfer_usdc` currently sends the
   raw tx and returns **without waiting for the receipt**. Route it through the existing `_exec_tx`
   helper (or add `wait_for_transaction_receipt` + `status == 1` check) so the "✅ Вывод успешно
   завершён" message + Polygonscan link is sent **only after on-chain confirmation** (matches the
   prompt's "после успешного подтверждения").

**Consistency cleanup (optional but recommended):** the wallet/positions/menu screens
(`telegram.py` L150–182, L573–599, L1295–1300) compute "available" several different ways
(`total_usdc`, `dw_pusd + on_eoa`, etc.). Point them at `withdrawable_usdc()` too so every screen shows
the same number the user can actually withdraw.

**Config:** add `MIN_WITHDRAW_USDC = 1.0` to `core/config.py::Settings` (no magic numbers, §5.6).
No DB migration required.

**Acceptance / test plan (Bug B):**
- Unit: `withdrawable_usdc` returns `dw_pusd + eoa(pusd+usdc_e+usdc)` for mocked balances (deposit-only,
  EOA-only, split, empty).
- FSM (staging): a user whose $7 sits as deposit-wallet pUSD sees **"Доступно: 7.00 USDC"** at step 3;
  entering `7` passes the pre-flight gate; entering `8` is rejected upfront (no on-chain call).
- Integration (staging, tiny amount): full path pulls pUSD→EOA, converts, transfers native USDC, waits
  for receipt, and the success message contains a valid `polygonscan.com/tx/…` link; the position/wallet
  screens then show the reduced "Доступно".
- Negative: simulate a failed swap leg → user gets a specific failure message, **never** a misleading
  "$0.00 available", and no partial silent drain.

---

#### Optional runtime verification (only if you want logs, not required)

```bash
# Bug A — confirm the import actually fails in the deployed image
pm2 logs nexa-worker --lines 200 --nostream | grep -iE "get_open_trade_by_token|close_position_failed|ImportError"
docker exec -it <worker_container> python -c "from core.db import get_open_trade_by_token; print('OK')"

# Bug B — confirm where the $7 actually is and which wallet the flow read
pm2 logs nexa-worker --lines 300 --nostream | grep -iE "withdraw_failed|withdraw_dw_pull_failed|Недостаточно"
# (substitute the user's deposit + EOA addresses to compare pUSD vs native USDC balances)
```

---

### Blueprint 13 — Sizing-mode hierarchy fix, zero-edge skip, max-daily-trades limit & stop-loss invariant ✅ IMPLEMENTED

> **Audit context (2026-06-30, Lead Quant):** three risk-management defects/gaps found during the
> Kelly audit. 13.1 fixes a settings-priority bug that silently disables per-user Kelly and a
> dangerous "no-edge → trade anyway" fallback. 13.2 adds a user-controllable daily trade cap.
> 13.3 records the (already-correct) stop-loss invariant so it is never regressed by sizing work.
> **No code in this pass — design only.** Code snippets below are the implementation contract.

#### Diagnosis recap (confirmed by code inspection — do NOT re-diagnose)

**Bug 13.1a — global ENV silently overrides per-user Kelly.** `execute_copy_trade` resolves the
effective mode correctly (`user_sizing_mode = user.get("sizing_mode") or settings.sizing_mode`,
`execute_copy.py:128`) and branches on it (`:152 if user_sizing_mode == "kelly":`). **But**
`kelly_stake` itself re-checks the *global* config and bails:

```52:53:core/sizing.py
    if cfg.sizing_mode != "kelly":
        return 0.0  # caller falls back to legacy flat cap
```

Global default is `sizing_mode: str = "fixed"` (`config.py:249`). So a user who enables Kelly in
Telegram enters the `kelly` branch, calls `kelly_stake`, gets `0.0` (because *global* mode is
`fixed`), and is dropped into the no-edge fallback → **fixed sizing**. The per-user choice is
silently ignored unless `SIZING_MODE=kelly` is also set in the environment. This is a coupling bug:
mode selection is the **caller's** job; `kelly_stake` must be **pure math**.

**Bug 13.1b — "no edge" falls through to a fixed-size trade.** When Kelly legitimately returns `0`
(`q_hat <= p`, i.e. no measurable edge over the market price), the bot does **not** skip — it trades
the flat cap anyway:

```173:177:worker/tasks/execute_copy.py
        else:
            # Kelly returned 0 (no edge detected) — fall back to fixed cap
            size_usdc = min(user_max, depth_cap) if depth_cap > 0 else user_max
            log.info("sizing_kelly_no_edge_fallback", fixed_cap=round(size_usdc, 2),
                     user_id=user.get("id"))
```

This defeats the entire point of Kelly: a user who chose Kelly is telling us "only bet when there is
an edge". Trading the flat $25 on a zero-edge signal is the opposite of that contract. The same is
true when Kelly returns a positive but **sub-$5** stake: the downstream `size_usdc = max(size_usdc,
exchange_min)` floor (`execute_copy.py:235`) silently inflates a $0.80 Kelly stake back up to the
$5 platform minimum — again overriding the math.

**Gap 13.2 — no per-user daily trade cap.** On a high-activity whale day a user can be copied into
dozens of positions; there is no way to say "max N entries/day". `max_open_positions`
(`config.py`) caps *concurrent* positions globally, not *daily entries* per user.

**Invariant 13.3 — stop-loss is already sizing-agnostic; lock it in.** The Delta-Drop stop
(Blueprint 10) lives in `sync_positions` (`manage_positions.py`) and triggers purely on
`entry_price` vs live `best_bid`. It never reads `sizing_mode`. `entry_price` is persisted on every
`copy_trades` row for both modes (BP1, `execute_copy.py:431`). So Kelly vs Fixed cannot change stop
behaviour today — but nothing *documents* or *tests* that, so a future sizing change could regress
it. This blueprint records the invariant + a guard test.

---

#### Blueprint 13.1 — Correct sizing-mode hierarchy + zero-edge skip

**Priority rule (authoritative):** `users.sizing_mode` (DB) **>** `settings.sizing_mode` (global ENV).
The global value is only a **default for users who never chose**. An explicit per-user choice always
wins. Encoded as: `effective_mode = user.sizing_mode if user.sizing_mode in {"fixed","kelly"} else settings.sizing_mode`.

**Behaviour matrix (the contract):**

| `effective_mode` | Kelly math result | Action |
|---|---|---|
| `kelly` | stake ≥ `exchange_min` (after user/depth caps) | **trade** the Kelly stake |
| `kelly` | `0` (no edge, `q_hat ≤ p`) | **SKIP** → `risk_gate:zero_edge` |
| `kelly` | `> 0` but `< exchange_min` | **SKIP** → `risk_gate:zero_edge` (do **not** floor up to $5) |
| `fixed` | n/a (Kelly never called) | trade the flat `min(user_max, depth_cap)` cap |

Fixed sizing is used **only** when the user explicitly chose `fixed`. There is no "Kelly fell back to
fixed" path anymore.

**Step 1 — make `kelly_stake` pure math (`core/sizing.py`).** Delete the global-mode early return
(lines 52–53). Mode selection no longer belongs here. Keep the genuine "no input" guards
(`equity <= 0`, `free_pusd <= 0`, `p` out of `(0,1)`) and the no-edge guard (`q_hat <= p → 0.0`).
Update the docstring: `kelly_stake` now returns the recommended stake assuming the caller has
already decided to size with Kelly; `0.0` means **"no edge — do not bet"**, not "use fixed".

**Step 2 — rewrite the sizing block (`execute_copy.py:124–182`).**

```python
# ── BP13.1: resolve effective sizing mode (User DB > global ENV) ──────────
user_mode = user.get("sizing_mode")
effective_mode = user_mode if user_mode in ("fixed", "kelly") else settings.sizing_mode

user_max  = float(user.get("max_position_usdc") or 25)
depth_cap = float(signal.get("max_copy_usdc") or signal.get("size_usdc") or 0)

if effective_mode == "kelly":
    from core.sizing import kelly_stake
    from core.wallet_score import score_wallet
    try:
        score = score_wallet(signal.get("source_wallet") or signal.get("whale_wallet") or "")
    except Exception:
        score = None
    k_stake = kelly_stake(
        p=float(signal.get("price") or 0), score=score,
        consensus=int(signal.get("consensus") or 1),
        equity=equity, free_pusd=tradeable, cfg=settings,
    )
    size_usdc = min(k_stake, user_max)
    if depth_cap > 0:
        size_usdc = min(size_usdc, depth_cap)
    # BP13.1b: no edge OR sub-minimum Kelly stake → SKIP, never floor up, never fall back to fixed.
    if k_stake <= 0 or size_usdc < settings.exchange_min_order_usdc:
        log.info("skip_zero_edge", user_id=user_id, k_stake=round(k_stake, 4),
                 capped=round(size_usdc, 4), exchange_min=settings.exchange_min_order_usdc)
        return {"skipped": True, "reason": "risk_gate:zero_edge"}
    log.info("sizing_kelly", stake=round(k_stake, 2), capped=round(size_usdc, 2),
             equity=round(equity, 2), user_id=user_id)
else:
    size_usdc = min(user_max, depth_cap) if depth_cap > 0 else user_max
    score = None
    log.debug("sizing_fixed", cap=round(size_usdc, 2), user_id=user_id)
```

**Step 3 — protect the exchange-min floor from resurrecting a skipped Kelly stake.** The existing
`size_usdc = max(size_usdc, exchange_min)` (`:235`, `:261`, `:334`) is correct for **fixed** mode (a
small fixed cap should still execute at $5). It is only dangerous on the Kelly path, and Step 2
already `return`s before reaching it when the Kelly stake is sub-minimum. **No change to the floor
lines** — the early `return` is the guard. (Document this dependency in a comment so the floor is not
later moved above the Kelly skip.)

**Step 4 — `zero_edge` is a silent skip, not a notification.** Mirror the existing
`risk_gate:{gate}` skips (`:362-367`): log + return, **no Telegram message** (a "no edge" non-event
must not spam the user). It surfaces only in logs / metrics.

**Acceptance criteria (13.1):**
- `kelly_stake` unit tests no longer depend on `cfg.sizing_mode`; add a case asserting a positive
  stake is returned with `cfg.sizing_mode="fixed"` (proves decoupling).
- Integration: user `sizing_mode='kelly'`, `SIZING_MODE=fixed` in env, edge present → trade is
  **Kelly-sized** (regression test for 13.1a).
- Integration: user `sizing_mode='kelly'`, signal with `q_hat ≤ p` → `{"skipped": True,
  "reason": "risk_gate:zero_edge"}`, **no order placed, no fixed fallback** (13.1b).
- Integration: user `sizing_mode='kelly'`, Kelly stake `$0.80` (< $5) → skipped `zero_edge`, **not**
  floored to $5.
- Integration: user `sizing_mode='fixed'` → unchanged flat-cap behaviour, no Kelly call.

---

#### Blueprint 13.2 — Per-user "Max daily trades" limit

**13.2.1 — Database (migration `014_max_daily_trades.sql`).** New nullable column; `NULL` = unlimited
(preserves current behaviour for every existing user).

```sql
-- Blueprint 13.2: per-user daily trade cap. NULL = unlimited (default, legacy behaviour).
-- Counted against copy_trades rows CREATED in the current UTC day that actually entered the
-- market (status <> 'failed'). Apply in Supabase SQL editor. Idempotent.
alter table users add column if not exists max_daily_trades int;
```

Add to §6.9 pending-migrations list as **014** and to the §6.10 migration order. Update
`core/db/models.py` `User` reference (add `max_daily_trades: Mapped[int | None]`). No global config
knob is required; optionally add `default_max_daily_trades: int | None = None` to `Settings` if a
fleet-wide default is ever wanted (default `None` = off).

**13.2.2 — DB helper (`core/db/queries.py`).** Mirror `get_daily_realized_pnl` but key on
`created_at` and the **UTC calendar day** (not a trailing 24 h window — the user's mental model is
"per day", resets at 00:00 UTC).

```python
def get_daily_trade_count(user_id: int) -> int:
    """Number of copy_trades this user ENTERED since 00:00 UTC today.

    Counts rows that reached order placement (status != 'failed'). copy_trades rows are only
    inserted once the bot commits to placing an order (execute_copy.py), so skipped signals
    never consume a slot. 'failed' rows (order never landed) are excluded.
    """
    sb = get_supabase()
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    res = (
        sb.table("copy_trades")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .neq("status", "failed")
        .gte("created_at", since)
        .execute()
    )
    return int(res.count or 0)
```

**13.2.3 — Risk gate (fail-fast, before sizing & RPCs).** This cap must reject **before** the
expensive balance/positions/order-book reads, so it is **not** folded into `check_risk_gates`
(which runs late, post-book). Instead add an early guard in `execute_copy_trade`, immediately after
the `copy_paused_until` check (`:114`) and before the balance check (`:117`):

```python
# ── BP13.2: per-user daily trade cap (fail-fast, UTC day) ─────────────────
max_daily = user.get("max_daily_trades")
if max_daily is not None:
    try:
        from core.db import get_daily_trade_count
        used = get_daily_trade_count(user_id)
        if used >= int(max_daily):
            log.info("skip_max_daily_trades", user_id=user_id, used=used, limit=int(max_daily))
            _notify_daily_limit(user["telegram_id"], used, int(max_daily))  # throttled, see below
            return {"skipped": True, "reason": "risk_gate:max_daily_trades"}
    except Exception:
        log.warning("daily_trade_count_failed", user_id=user_id)  # fail-open: never block on a count error
```

- **Counting semantics:** the slot is consumed when a `copy_trades` row is inserted
  (`execute_copy.py:420`), i.e. the moment we commit to placing. The check reads the count *before*
  this trade is inserted, so `used >= max_daily` is the correct "already at limit" test.
- **Known race (accepted, documented):** two signals fanned out concurrently can both observe
  `used = max-1` and both proceed, overshooting by one. This is a soft risk cap, not a financial
  invariant; an atomic counter is out of scope. Note it in the code comment.
- **Reset:** purely time-based — the next 00:00 UTC the `since` boundary moves and the count resets.
  No cron, no stored counter to clear.
- **Notification:** `_notify_daily_limit` is **throttled** via `core.cache.notify_once`
  (key `daily_limit:{telegram_id}`, ttl ≈ `lowbal_alert_throttle_sec`) so a busy day produces at
  most one "daily limit reached" nudge, mirroring `_notify_low_balance` (BP2).

**13.2.4 — Telegram UI state machine (`api/routers/telegram.py`).** Mirror the existing
`max_position_usdc` template+custom pattern (`_settings_kb` + `setmax_*` + `awaiting_max_pos`).

*Keyboard (`_settings_kb`)* — add a `max_daily_trades` parameter and a new template row with a ✓ on
the active value:

```python
def _settings_kb(copy_active, current_max, sizing_mode="fixed", max_daily=None):
    def _daily_label(n):
        mark = " ✓" if max_daily == n else ""
        return f"{n}/день{mark}"
    off_label = "♾ Без лимита" + (" ✓" if max_daily is None else "")
    ...
    # new rows (placed after the position-size rows, before the sizing toggle):
    [InlineKeyboardButton(_daily_label(1),  callback_data="setdaily_1"),
     InlineKeyboardButton(_daily_label(5),  callback_data="setdaily_5"),
     InlineKeyboardButton(_daily_label(10), callback_data="setdaily_10")],
    [InlineKeyboardButton("✏️ Свой лимит", callback_data="setdaily_custom"),
     InlineKeyboardButton(off_label,        callback_data="setdaily_off")],
```

Every existing `_settings_kb(...)` call site (`:854`, `:1237`, `:1519`, `:1566`, `:1586`) must pass
`max_daily=db_user.get("max_daily_trades")`.

*Callback handler* — add a `data.startswith("setdaily_")` branch alongside `setmax_` (`:1523`):

```python
if data.startswith("setdaily_"):
    suffix = data[len("setdaily_"):]
    if suffix == "off":
        update_user(tg_user.id, {"max_daily_trades": None})
        ... re-render settings with confirmation "♾ Лимит снят" ...
        return
    if suffix == "custom":
        context.user_data["awaiting_daily_limit"] = True
        context.user_data["awaiting_max_pos"] = False   # mutually exclusive FSM flags
        ... prompt: "Введи число сделок в день (1–100), 0 = без лимита" ...
        return
    val = int(suffix)                                    # 1 | 5 | 10
    update_user(tg_user.id, {"max_daily_trades": val})
    ... re-render settings, answer "✅ Лимит: N/день" ...
    return
```

*Custom text input* — add a block in the message handler mirroring `awaiting_max_pos`
(`:1206-1238`). **Guard mutual exclusion:** the handler checks `awaiting_daily_limit` and
`awaiting_max_pos` as separate branches; setting one flag clears the other (shown above) so a typed
number is never ambiguous.

```python
if context.user_data.get("awaiting_daily_limit"):
    context.user_data["awaiting_daily_limit"] = False
    clean = text.strip().replace(",", "")
    try:
        n = int(float(clean))
    except ValueError:
        ... reply "⚠️ Введи целое число, напр. 5" ...; return
    if n <= 0:
        update_user(tg_user.id, {"max_daily_trades": None})   # 0 / negative = unlimited
        ... reply "♾ Лимит снят — без ограничения по сделкам в день" ...
    elif n > 100:
        ... reply "⚠️ Максимум 100 сделок в день"; return
    else:
        update_user(tg_user.id, {"max_daily_trades": n})
        ... reply f"✅ Лимит: {n} сделок в день (UTC)" ...
    ... re-render settings keyboard with new max_daily ...
    return
```

*FSM summary:* `idle → [tap ✏️ Свой лимит] → awaiting_daily_limit → [valid int] → idle (saved)`;
template buttons (`setdaily_1/5/10/off`) write immediately with no intermediate state. `awaiting_*`
flags are mutually exclusive — entering either clears the other.

*Display* — surface the limit in `_settings_text` and the dashboard (`_dashboard_text:204`):
`f"🔁 Лимит/день: {max_daily}" if max_daily else "🔁 Лимит/день: ♾"`.

**Acceptance criteria (13.2):**
- Migration applies idempotently; existing users read `NULL` → unlimited (no behaviour change).
- `get_daily_trade_count` counts only today-UTC, non-`failed` rows; excludes yesterday and `failed`.
- With `max_daily_trades=1` and one entered trade today, the next signal returns
  `{"skipped": True, "reason": "risk_gate:max_daily_trades"}` and places **no** order; the check
  runs **before** any balance/book RPC (assert via call order / logs).
- After 00:00 UTC rollover the count resets and trading resumes with no manual action.
- Telegram: tapping `5/день` persists `max_daily_trades=5` and shows ✓; `✏️ Свой лимит` → typing
  `7` persists 7; `0` or `♾ Без лимита` clears to `NULL`; daily-limit nudge is throttled to ≤1.

---

#### Blueprint 13.3 — Stop-loss consistency invariant

**Invariant (must hold for all future sizing work):** the unified **Delta-Drop stop-loss**
(Blueprint 10) applies to **every** open position with `shares > 0`, evaluated solely on
`entry_price` vs the live CLOB `best_bid` at the **price-tracking layer** (`sync_positions` in
`worker/tasks/manage_positions.py`). The stop is **completely independent of how the entry size was
computed** — Kelly or Fixed. Position size affects only the *dollar* magnitude of a stopped loss
(`loss_$ = size · X`, see BP10), never *whether* or *when* the stop fires.

**Why it already holds (do not "fix"):**
- `sync_positions` reads `entry` from the position `avg_price` (fallback: ledger `entry_price` via
  `get_open_trade_by_token`) and `best_bid` from the live book. It **never reads `sizing_mode`**.
- `entry_price` is persisted on **every** `copy_trades` row at insert time (BP1,
  `execute_copy.py:431`) regardless of sizing mode, so the stop has its reference price in both modes.
- The trigger `(1 - best_bid/entry) >= delta_drop_stop_pct` contains no size/mode term.

**Guardrails to add so it stays true:**
- **Test (pure):** extend BP10's `delta_drop_hit(entry, best_bid, X)` unit suite with an explicit
  comment/case asserting the function signature takes **no size and no mode** argument — the type
  system enforces sizing-independence.
- **Test (integration):** open two positions at the **same `entry_price`**, one created in Kelly
  mode and one in Fixed mode; push a synthetic book with `best_bid ≤ entry·(1-X)`; assert **both**
  fire `position_closed reason=delta_drop_stop`. Sizing mode must not appear in the assertion path.
- **Doc lock:** a one-line comment at the Delta-Drop trigger in `manage_positions.py`:
  `# BP13.3 invariant: stop is sizing-mode-agnostic — do NOT branch on users.sizing_mode here.`
- **Coupling caution:** BP13.1 makes Kelly able to **skip** entry (`zero_edge`) and BP13.2 can cap
  daily entries — both reduce *how many* positions exist, but once a position is open it is governed
  by the **same** stop. No stop-loss code reads the daily-cap or sizing fields.

---

#### Cross-cutting notes
- **Config defaults unchanged:** global `sizing_mode` stays `"fixed"` (conservative). The 13.1 fix
  means a per-user `kelly` choice now actually takes effect without touching the env.
- **Rollback:** 13.1 is behaviour-preserving for `fixed` users; to revert, restore the
  `kelly_stake` global-mode early return. 13.2 is fully gated by `max_daily_trades IS NULL` (off by
  default), so shipping the migration alone changes nothing until a user sets a limit.
- **Skip-reason taxonomy:** two new `reason` values join the existing `risk_gate:*` family —
  `risk_gate:zero_edge` (13.1) and `risk_gate:max_daily_trades` (13.2) — so dashboards/log greps
  already filtering `risk_gate:` pick them up for free.

---

### Blueprint 14 — Kelly edge-degeneracy fix + AI-analysis pipeline redesign ✅ IMPLEMENTED

> **Audit context (2026-06-30, Lead Quant):** two production problems on PolyMind AI. 14.A — Kelly
> sizing looks fixed (~$6.2 every executed trade) and only ever fires on expensive favorites.
> 14.B — the LLM risk analysis is internally inconsistent (emoji/verdict/score disagree) and
> content-free.
>
> **Implemented 2026-06-30:** 14.A shipped as `core/sizing._damp_edge()` +
> `kelly_edge_damping_gamma` config knob, **default 0.0 (OFF — legacy undamped formula,
> behaviour unchanged on deploy)**. Stress-testing the fix before shipping the default ON
> revealed a severe side effect: at `gamma=1.0` (full damping), the *maximum possible* stake
> (best wallet quality, max consensus, edge at the 0.06 cap) is `kelly_lambda · kelly_edge_cap ·
> equity` — independent of price. At current prod equity (~$124) that ceiling is **$1.86**,
> below `exchange_min_order_usdc` ($5) — meaning full damping makes Kelly skip **every** signal
> until equity exceeds ≈$333 (`5 / (kelly_lambda · kelly_edge_cap)`), or `kelly_base_edge` /
> `kelly_edge_cap` are raised to compensate. Per §5.6 (strategy/money-moving changes need a
> conservative default), this is shipped **off by default** — operators must explicitly set
> `KELLY_EDGE_DAMPING_GAMMA` after either growing equity past the threshold or raising the edge
> caps, and validate on the `kelly_stake` logs that real trades still clear the minimum before
> enabling fleet-wide. 14.B shipped as `core/risk_label.py` (single source of truth for
> emoji/verdict, shared by `ai_filter._broadcast` and `execute_copy._notify`) + strict
> `json_schema` structured output in `worker/tasks/ai_filter.py`
> (`risk_score`/`signal_type`/`thesis`/`caution`) + migration 015 (`trade_signals.ai_signal_type`).
> 14.B is live unconditionally (no flag) — it only changes how the existing AI message is
> composed, not how money moves.

---

#### Part A — Kelly edge degeneracy (the real cause of "fixed-looking" sizing)

**Diagnosis (confirmed from 30+ prod `kelly_stake` log lines, 2026-06-30 15:36–20:13):**

The user hypothesis ("risk gates inflate a $1 Kelly stake up to the $5 minimum") is **DISPROVEN** —
BP13 works: sub-$5 Kelly stakes log `skip_zero_edge` and are skipped, `sizing_kelly_no_edge_fallback`
count is 0. The true defect is deeper.

**Empirical fact:** `q_hat − p ≡ 0.0214` for *every* signal, across all prices observed
(0.126, 0.37, 0.39, 0.513, 0.67, 0.79, 0.82, 0.83, 0.905, 0.91, 0.938, 0.94, 0.95). The estimated
edge is a **flat constant**, independent of market or price.

**Mechanism.** With a constant edge `E = 0.0214`, the Kelly fraction collapses to a pure function of
price: `f_kelly = E / (1 − p)`, then `f_final = min(λ·f_kelly, max_risk_per_trade)`. With
`λ=0.25`, `max_risk_per_trade=0.05`, equity ≈ $124:

| Price band | `f_final` | stake | Outcome |
|---|---|---|---|
| `p < 0.867` | < 0.0403 | < $5 | **skip** (`skip_zero_edge`) — every cheaper signal |
| `0.867 ≤ p < 0.893` | 0.0403–0.05 | $5–$6.2 | rare (logs: `capped=5.1`, `5.2`) |
| `p ≥ 0.893` | **clamped to 0.05** | ≈ 5%·equity ≈ **$6.2** | every executed trade |

So the bot **only trades expensive favorites (p ≳ 0.89), and every one clamps to the 5% hard cap** —
hence the near-identical $6.18–$6.33 sizes. Quarter-Kelly (`λ`) is dead weight: on every *executed*
trade the 5% cap binds, not Kelly.

**Root cause.** `edge_hat = kelly_base_edge · quality · consensus_mult` (`core/sizing.py:74-75`)
encodes only *wallet trust*, never *market mispricing*, and is a **flat additive bump** on top of the
price the whale paid. Two compounding failures:
1. **Additive edge explodes at high p.** A flat +0.0214 added to 0.95 yields a tiny *relative* edge
   but `f_kelly = E/(1−p)` blows up as `p→1`, so Kelly always maxes out on favorites — the exact
   "penny-collecting, high tail-risk" trades. This is adverse selection baked into the math.
2. **`quality` is currently pinned constant** (single dominant whitelisted wallet and/or unscored
   default `quality=0.5`), so edge does not differentiate signals at all.

**Step 0 — confirm the `quality`/`consensus` decomposition (one command, before coding).** `kelly_edge`
is logged at DEBUG; raise the worker to `--loglevel=debug` briefly and capture:
```bash
docker compose logs --since 10m worker | grep "kelly_edge"   # edge_hat, quality, consensus
```
Confirm whether `quality` is constant (single-wallet/unscored) or genuinely flat from scoring.

**Step 1 — make edge price-aware (kill the high-p explosion).** The additive model is the core flaw.
Scale the edge so the Kelly fraction does **not** diverge as `p→1` — a 2pp edge on a 0.95 favorite
must be worth far less risk-adjusted stake than 2pp on a 0.55 coin-flip. Concretely, damp `edge_hat`
by a concave factor of `(1−p)` (design target; tune the exponent on the `position_mark`/`kelly_edge`
dataset):
```
edge_hat_eff = edge_hat · (1 − p)^γ      # γ ∈ [0.5, 1.0]; γ=1 fully cancels the 1/(1−p) blow-up
f_kelly      = edge_hat_eff / (1 − p)
```
With `γ=1` this makes `f_kelly = edge_hat` (flat in p) → favorites no longer auto-max the cap, and
mid-price signals with real edge can clear the minimum. This directly counters the penny-collecting
adverse selection surfaced above.

**Step 2 — make `quality` actually vary.** Ensure `signal["source_wallet"]` is reliably populated
(via `wallet_score.resolve_buyer`) so `score_wallet` returns a real per-wallet `resolved_count > 0`;
otherwise every signal falls to the `quality=0.5` default and edge can never differentiate. Add a
`kelly_edge` log assertion / metric on the share of signals scored vs. defaulted.

**Step 3 — stop the 5% cap from being the de-facto sizer.** Once Steps 1–2 let `f_final` land below
`max_risk_per_trade` on most trades, λ (quarter-Kelly) governs sizing again and stakes genuinely
vary. The 5% cap returns to being a *safety ceiling*, not the primary knob. Keep `max_risk_per_trade`
as-is; verify post-fix that executed trades are **not** all pinned at `f_final = 0.05`.

**Acceptance criteria (14.A):**
- Post-fix `kelly_stake` logs show `q_hat − p` **varying** across signals (no longer a constant).
- Executed trades show a **spread** of `f_final` values (not all `0.05`); stakes are not all ≈5%·equity.
- The bot enters at least some non-favorite (p < 0.89) signals when a real edge exists.
- Pure unit test for the new `edge_hat_eff`/`f_kelly` helper: assert `f_kelly` is non-increasing-then-
  bounded in p (no divergence at p→1), and 0 when `q_hat ≤ p`.

> **Note:** 14.A is a *strategy* change (moves money differently) → gate behind a config flag with a
> conservative default and validate on the `kelly_edge` dataset before enabling fleet-wide, per §5.6.

---

#### Part B — AI-analysis pipeline redesign

**Diagnosis (confirmed from code, `worker/tasks/ai_filter.py`).** The LLM returns **both** a free-text
`verdict` *and* a `score`, independently:
```43:44:worker/tasks/ai_filter.py
{{"score": <целое 1-10, 1=низкий риск>, "verdict": "<Сильный сигнал|Умеренный|Рискованно>",
```
while the bot derives the emoji from `score`:
```128:128:worker/tasks/ai_filter.py
    risk_icon = "🟢" if score <= 4 else ("🟡" if score <= 6 else "🔴")
```
→ `score=3` (bot → 🟢) + hallucinated `verdict="Рискованно"` ⇒ **"🟢 Рискованно · риск 3/10"**. Three
sources of truth (emoji, verdict, number) are unsynchronised. The `reason` text also just restates the
probability/time — no analytical value.

**Principle: `risk_score` is the single source of truth.** The LLM returns *only* a number + analysis.
The emoji **and** the verdict label are **derived deterministically by the bot** from `risk_score`.
The LLM never emits emoji or verdict.

**B.1 — Structured Outputs (strict JSON schema).** Replace `response_format={"type":"json_object"}`
with a strict `json_schema` so the model cannot return extra/contradictory fields:
```json
{
  "name": "trade_analysis",
  "strict": true,
  "schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["risk_score", "signal_type", "thesis", "caution"],
    "properties": {
      "risk_score":  { "type": "integer", "minimum": 1, "maximum": 10 },
      "signal_type": { "type": "string",
        "enum": ["penny_collecting","value_bet","momentum","longshot_size","consensus_stack","coin_flip"] },
      "thesis":  { "type": "string", "maxLength": 180 },
      "caution": { "type": "string", "maxLength": 120 }
    }
  }
}
```
- `risk_score` — the only driver of colour/verdict.
- `signal_type` — machine tag of the trade's structure (analytics + future filters).
- `thesis` — the core insight; `caution` — the main risk. No emoji, no number-restating.

**B.2 — Deterministic mapping (bot, single helper `risk_label(score)`).** Used by *both*
`ai_filter._broadcast` and `execute_copy._notify` so the two paths render identically:
```
1–2  → 🟢  "Сильный сетап"
3–4  → 🟢  "Уверенный сигнал"
5–6  → 🟡  "Умеренный риск"
7–8  → 🟠  "Высокий риск"
9–10 → 🔴  "Опасная зона"
```
Emoji, label and number now come from one integer → they cannot disagree. (Replaces the inline
`risk_icon` ternary at `ai_filter.py:128` and the equivalent in `execute_copy._notify`.)

**B.3 — New system prompt (draft).** Forbid number-restating; force hedge-fund-analyst reasoning:
```
Ты — старший аналитик хедж-фонда, специализация — рынки предсказаний (Polymarket).
Тебе дают сделку проверенного прибыльного кита из белого списка. Оцени КАЧЕСТВО СДЕЛКИ
как инвестиционный кейс, а не описывай вводные.

ЗАПРЕЩЕНО:
- Пересказывать цифры («цена 0.94, до закрытия 3 часа») — пользователь видит их сам.
- Общие фразы («высокая вероятность, но есть риск») — это мусор.
- Эмодзи, вердикты, слово «риск N/10» — это проставит система.

ТРЕБУЕТСЯ распознать СТРУКТУРУ сделки и дать инсайт уровня деска:
- Дорогой фаворит (0.90+) на крупный размер → «сбор копеек»: малый апсайд, жирный tail-risk.
- Низкая вероятность (<0.30) + агрессивный размер кита → возможный инсайд / асимметрия:
  кит видит то, чего не видит рынок. Подсвети это.
- Консенсус нескольких китов в одном исходе → усиление, но проверь скученность.
- Тонкий запас времени до резолва → нет места для разворота, риск выше.
- Цена ~0.50 → монетка; нужна причина, почему это не шум.

Верни строго JSON по схеме: risk_score (1=низкий риск сделки, 10=высокий), signal_type,
thesis (главный тезис), caution (главный риск). Русский, без воды, тон — аналитик, не маркетолог.
```
Set `temperature=0.2` (was 0.3) for verdict stability. On invalid JSON / schema refusal → fallback
`risk_score=5`, `thesis="ИИ временно недоступен"`, label/colour from the mapping. Never crash.

**B.4 — Persistence & migration.** Store `ai_score`, `ai_signal_type`, `ai_reason` on
`trade_signals`. New column needs migration **015**: `alter table trade_signals add column if not
exists ai_signal_type text;` (add to §6.9 / §6.10 with the implementation PR).

**Acceptance criteria (14.B):**
- Emoji, verdict label and number are always consistent (derived from one `risk_score`) — no more
  "🟢 Рискованно · риск 3/10".
- `thesis` never merely restates price/time; it names the structural pattern (e.g. penny-collecting,
  longshot-size) — spot-check 10 live analyses.
- Strict-schema call: malformed model output cannot leak emoji/verdict; fallback path verified.
- Both the signals-mode broadcast and the copied-trade notification use the shared `risk_label`.

---

#### Cross-cutting
- **The two problems are linked:** 14.A's degenerate Kelly funnels the bot into high-p penny-
  collecting favorites — precisely the trades 14.B's analyst prompt is built to flag as high tail-
  risk. Fixing 14.A widens the signal set; 14.B makes the per-trade risk legible.
- **Migrations:** 14.B adds **015** (`trade_signals.ai_signal_type`). 14.A adds a Kelly config flag
  (no schema change). Apply 015 with the BP14 code.
- Both parts are strategy/UX changes → gate behind config flags with conservative defaults (§5.6) and
  validate on real data before fleet-wide enable.

---

### Blueprint 15 — Onboarding & Trust UX Redesign (Progressive Disclosure) 🟡 FINAL DESIGN / READY TO IMPLEMENT

**Symptom (conversion drop at onboarding).** In the custodial copy-trading deployment
(`AUTO_COPY_ENABLED=true`), the very first message after `/start` creates a wallet and immediately
fires `_new_user_text()` (`api/routers/telegram.py` L267–282) — a wall of *"пополни USDC / пополни POL
/ зарегистрируй кошелёк"*. For a cold Web3 user this reads as a scam: an unknown bot demanding money
before delivering any value. They don't deposit, and they leave.

**Goal.** Replace "money-first" with **value-first, money-when-ready** (Progressive Disclosure):
welcome → explain the value → default into a **risk-free signals (demo) mode** → and only reveal
deposit/network/USDC instructions when the user *taps a button to opt in* to auto-trading.

> **Scope.** This blueprint redesigns the **`AUTO_COPY_ENABLED=true` (custodial)** onboarding only.
> The `AUTO_COPY_ENABLED=false` deployment is already a pure non-custodial signals bot with a soft
> intro (`_signals_welcome_text`, L308) and is the *tone reference* for this redesign.

---

#### ⚠️ Honesty constraint — DO NOT call the wallet "non-custodial"

The prompt asks to mention "кошелёк некастодиальный (если это так)". **It is NOT.** Per §1 and §5.1 the
bot **generates and holds the user's private key (encrypted)** — this is a **custodial** model. Claiming
"non-custodial" would be a false safety claim and is **forbidden** (§5.1, §5.7). Use only **truthful**
trust levers instead:

1. **"Выводи в любой момент"** — true: `/withdraw` (BP12) sends funds to any Polygon address the user
   names, no approval from us. This is the strongest *honest* fear-reducer.
2. **"Старт без депозита"** — the demo/signals mode genuinely requires $0 on-chain.
3. **"Только сеть Polygon"** — concrete, protects the user from losing funds on the wrong chain.
4. **"Только USDC, газлесс-регистрация"** — bot auto-converts USDC→pUSD; registration costs the user
   no gas (relayer-deployed deposit wallet, see §2 / `_register_deposit_wallet`).
5. **Optional, only if implemented:** "ключ хранится в зашифрованном виде" — true (Fernet at rest),
   but do **not** oversell it as "только у тебя".

---

#### Part A — Progressive-disclosure model (3 layers)

| Layer | When shown | Contains | Money mentioned? |
|---|---|---|---|
| **L0 Welcome / Demo** | immediately on first `/start` (wallet silently created in background) | value pitch + the trust quote + the offer to start in signals mode | **No** |
| **L1 Upsell gate** | only after user taps **🚀 Перейти к автоторговле** | the 3 honest trust facts (withdraw-anytime / Polygon-only / USDC-only) + a single "show me the address" CTA | network/asset rules, **no address yet** |
| **L2 Funding steps** | only after user taps **✅ Показать адрес для пополнения** | deposit address + numbered steps + 🔐 register button (the *content* of the old `_new_user_text`, but earned, not pushed) | **Yes — fully** |

The deposit address / network / "buy USDC" instructions live **only in L2**. They are never the first
message and never appear unless the user explicitly walks L0 → L1 → L2.

---

#### Part B — Onboarding FSM

**State source of truth.** Derive the stage from existing fields (mirrors how `_checklist()` already
derives status) — **no migration required**:

```python
# Proposed helper in telegram.py — pure function over the db_user row.
def _onboarding_stage(db_user: dict) -> str:
    if not db_user.get("wallet_address"):
        return "fresh"                      # /start not finished creating wallet
    if db_user.get("is_signal_only", True): # NEW DEFAULT: True for new users
        return "demo"                       # L0 — risk-free signals, no money ask
    if not db_user.get("wallet_registered"):
        return "intent"                     # opted into autotrade, not yet registered
    # funded? reuse _checklist math (dw_pusd + on_eoa) >= MIN_USDC_READY
    return "active"                         # auto-trading
```

> **One small behavioural change required:** new users must default to **`is_signal_only = True`**.
> Today new rows default to copy-mode. Set `is_signal_only=True` at wallet-creation time in `cmd_start`
> (and as the column default in a future migration if convenient — not blocking).

| # | State | Entry trigger | Bot shows | Exit → |
|---|-------|--------------|-----------|--------|
| 1 | `fresh` | `/start`, no wallet | "⏳ Готовлю твой аккаунт…" then silently `generate_wallet()`; set `is_signal_only=True` | → `demo` |
| 2 | `demo` | wallet ready / `onb_signals` / `/start` returning demo user | **L0 welcome** (`_onboarding_welcome_text`) + `_onboarding_kb()` | tap 🚀 → `intent` · tap 🎬 → stays `demo`, confirms signals on |
| 3 | `intent` | `onb_autotrade` | **L1 upsell** (`_autotrade_gate_text`) + `_autotrade_gate_kb()` | tap ✅ → `funding` view · tap ↩️ → `demo` |
| 4 | `funding` (view, not a stored state) | `onb_fund_steps` | **L2 funding** (`_funding_steps_text` = deposit addr + steps) + 🔐 register | tap 🔐 → existing `register` flow |
| 5 | `register` | `register` callback (unchanged, BP9/BP12) | gasless deploy → "✅ Кошелёк готов" | on success set `is_signal_only=False`; → `active` |
| 6 | `active` | registered + (auto-detected funds) | normal `_dashboard_text` + `_main_kb` (existing) | — |

**Seamless demo → autotrade hand-off.** The user never re-enters anything: tapping **🚀 Перейти к
автоторговле** in `demo` flips intent, L1 reassures, L2 reveals the address, 🔐 register reuses the
existing gasless flow and **flips `is_signal_only=False` on success** — at which point the dashboard
becomes the full auto-trading view. Going back is always one tap (**↩️ Вернуться в режим сигналов**),
which never deletes the wallet — it just sets `is_signal_only=True`.

> **Subscription gate (product decision, flag it).** In copy-mode deployments, signal broadcasts are
> gated by an active subscription. To make the demo *genuinely* risk-free and not a dead end, **default
> recommendation:** grant new demo users a small free sample — e.g. the next **3 signals** or a **48h
> window** (Redis counter keyed by `telegram_id`, no schema change). If the business refuses a free
> tier, L0's copy must instead say signals require a subscription (still framed as "watch us trade
> before funding"). **Default to the 3-signal sample; leave the final call to the operator.**

---

#### Part C — Concrete message drafts & inline-button structure (start screen)

All texts are HTML parse-mode, Russian (matches the existing bot voice). These are **drafts ready to
paste** into new builders in `telegram.py`.

**L0 — Welcome / Demo (`_onboarding_welcome_text`)** — replaces `_new_user_text` as the first message:

```
🧠 <b>Добро пожаловать в PolyMind AI!</b>

Мы копируем сделки проверенных <b>китов Polymarket</b> — трейдеров, которые
годами стабильно зарабатывают на прогнозах. Наш ИИ следит за их крупными
покупками 24/7 и присылает разбор каждой.

━━━━━━━━━━━━━━━━━━━━━
🎬 <b>Начни без риска</b>
━━━━━━━━━━━━━━━━━━━━━
Мы понимаем, что доверие нужно заслужить. Начни с <b>режима сигналов</b> —
посмотри, как мы торгуем, без риска для твоих средств.

Ты будешь получать те же сигналы по китам с ИИ-анализом, что и платные
подписчики, а решение о деньгах примешь позже — когда сам увидишь результат.

👇 С чего начнём?
```

`_onboarding_kb()` — inline keyboard (primary CTA first, money CTA secondary):

```python
InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Смотреть сигналы (без риска)", callback_data="onb_signals")],
    [InlineKeyboardButton("🚀 Перейти к автоторговле",       callback_data="onb_autotrade")],
    [
        InlineKeyboardButton("❓ Как это работает", callback_data="help"),
        InlineKeyboardButton("🛡 Это безопасно?",  callback_data="onb_trust"),
    ],
])
```

**`onb_trust` — trust FAQ (fear-killer), shown on demand:**

```
🛡 <b>Часто волнует — отвечаем честно</b>

💸 <b>Деньги выводятся в любой момент.</b>
Кнопка «💸 Вывод» отправит USDC на любой твой адрес Polygon. Мы не держим
твои средства в заложниках и не требуем разрешений.

🌐 <b>Только сеть Polygon.</b>
Пополняй строго в сети Polygon (не Ethereum / BSC / Arbitrum) — иначе монеты
уйдут в чужую сеть и потеряются. Это главное правило безопасности.

🪙 <b>Только USDC, и старт без газа.</b>
Достаточно обычного USDC — бот сам сконвертирует его в торговый баланс.
Регистрация кошелька газлесс: POL на старте не нужен.

🎬 <b>Старт — бесплатный и без депозита.</b>
Режим сигналов не требует ни цента на счёте. Сначала смотришь, потом решаешь.
```
Buttons: `[🎬 Остаться на сигналах → onb_signals]` · `[🚀 Перейти к автоторговле → onb_autotrade]`

> Note: this text deliberately says **"деньги выводятся в любой момент"** and **"мы не держим в
> заложниках"** (true), and **never** the word "некастодиальный" (false). Keep it that way.

**`onb_signals` — confirm demo mode (sets `is_signal_only=True`, no deposit):**

```
🎬 <b>Режим сигналов включён</b>

Теперь ты получаешь сигналы по китам с ИИ-анализом — <b>без единого цента на
счёте</b>. По каждому сигналу: событие, исход, цена входа кита, объём и оценка
риска от ИИ + ссылка на рынок.

Когда захочешь, чтобы бот торговал это <b>за тебя автоматически</b> —
нажми «🚀 Перейти к автоторговле». Это займёт пару минут.
```
Buttons: `[🚀 Перейти к автоторговле → onb_autotrade]` · `[⭐️ Подписка → subscription]` · `[🏠 Меню → menu]`

**L1 — Autotrade upsell gate (`_autotrade_gate_text`)** — shown on `onb_autotrade`. **Still no address:**

```
🚀 <b>Автоторговля — сделки копируются сами</b>

В этом режиме бот сам открывает позиции на <b>твоём личном кошельке</b>, как
только кит заходит крупно. Перед первым пополнением — 3 факта, чтобы было
спокойно:

🔑 <b>Кошелёк под твоим контролем.</b> Вывести средства можно в любой момент
кнопкой «💸 Вывод» — без подтверждений с нашей стороны.
🌐 <b>Только сеть Polygon.</b> Не Ethereum, не BSC — иначе деньги уйдут в чужую
сеть.
🪙 <b>Только USDC.</b> Бот сам сконвертирует в торговый баланс (pUSD). Газ (POL)
для старта не нужен — регистрация газлесс.

Готов? Покажу адрес и пошаговую инструкцию по пополнению.
```

`_autotrade_gate_kb()`:

```python
InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Показать адрес для пополнения", callback_data="onb_fund_steps")],
    [InlineKeyboardButton("💸 А как выводить деньги?",        callback_data="onb_withdraw_info")],
    [InlineKeyboardButton("↩️ Вернуться в режим сигналов",    callback_data="onb_signals")],
])
```

**L2 — Funding steps (`_funding_steps_text(addr)`)** — shown on `onb_fund_steps`. This is the *only*
place the address + deposit steps appear (refined `_new_user_text`):

```
🚀 <b>Пополнение — 3 шага</b>

📬 <b>Твой адрес для пополнения (USDC, сеть Polygon):</b>
<code>{addr}</code>

━━━━━━━━━━━━━━━━━━━━━
1️⃣ Отправь <b>USDC</b> на адрес выше — <b>строго в сети Polygon</b>
2️⃣ Нажми <b>🔐 Зарегистрировать кошелёк</b> (газлесс, 30–60 сек)
3️⃣ Готово — бот начнёт копировать крупные сделки китов
━━━━━━━━━━━━━━━━━━━━━

⚠️ <b>Только сеть Polygon</b> — не Ethereum, не BSC, не Arbitrum!
ℹ️ Бот сам сконвертирует USDC в торговый баланс (pUSD). Вывести средства можно
в любой момент кнопкой «💸 Вывод».
```

Buttons:

```python
InlineKeyboardMarkup([
    [InlineKeyboardButton("🔐 Зарегистрировать кошелёк", callback_data="register")],
    [
        InlineKeyboardButton("🔄 Проверить баланс", callback_data="wallet_balance"),
        InlineKeyboardButton("💸 Как вывести",      callback_data="onb_withdraw_info"),
    ],
    [InlineKeyboardButton("↩️ Назад", callback_data="onb_autotrade")],
])
```

**`onb_withdraw_info` — withdraw reassurance (links to the BP12 flow):**

```
💸 <b>Вывод средств — в любой момент</b>

Нажимаешь «💸 Вывод» → вводишь свой адрес Polygon → сумму → подтверждаешь.
Бот сконвертирует pUSD обратно в USDC и отправит на указанный адрес; в ответ
придёт ссылка на транзакцию в Polygonscan.

Никаких блокировок и периодов ожидания — деньги твои.
```
Buttons: `[🚀 Продолжить к пополнению → onb_fund_steps]` · `[🏠 Меню → menu]`

---

#### Part D — Wiring (files & touch-points; implement later)

1. **`cmd_start` (L485–562).** For `AUTO_COPY_ENABLED=true`:
   - On wallet creation, set `is_signal_only=True` (default into demo).
   - Returning users: branch on `_onboarding_stage(db_user)` → `demo` shows `_onboarding_welcome_text`
     + `_onboarding_kb`; `active` shows the existing `_dashboard_text` + `_main_kb`.
   - **Delete the immediate `_new_user_text` push.** Its content survives, relocated to L2.
2. **New callbacks in `callback_handler` (L1333+):** `onb_signals`, `onb_autotrade`, `onb_trust`,
   `onb_fund_steps`, `onb_withdraw_info`. `onb_signals` sets `is_signal_only=True`; `onb_autotrade`
   only renders L1 (does **not** flip the flag yet — the flag flips to copy-mode on **register
   success**, so an abandoned funnel never silently arms auto-trading).
3. **`register` success (L1428–1438 and `cmd_register` L963–968):** on success
   `update_user(tg_user.id, {"is_signal_only": False})` so the hand-off to `active` is automatic.
4. **Dashboard (`_dashboard_text` L222 / `_checklist` L177):** when stage==`demo`, suppress the
   funding checklist entirely and show a one-line demo banner + the single 🚀 CTA. The checklist only
   makes sense once the user has opted into autotrade.
5. **`_main_kb` (L50):** for demo users, surface **🚀 Перейти к автоторговле** as the top row instead
   of wallet/positions clutter (those are meaningless with $0 and no positions).
6. **Reuse, don't fork:** registration, wrap, withdraw, subscription flows are unchanged (BP9/BP12).
   This blueprint adds *only* the pre-funding disclosure layer and the demo default.

**Config (§5.6, no magic literals):** add `ONBOARDING_FREE_SIGNALS = 3` (or `ONBOARDING_DEMO_HOURS`)
to `core/config.py::Settings` if the free-sample option is taken. No DB migration is required for the
FSM (stage is derived); the only data change is the `is_signal_only=True` default for new users.

---

#### Acceptance criteria (Blueprint 15)

- A brand-new `/start` (copy-mode deployment) shows the **value-first L0 welcome** containing the exact
  trust quote — and **no deposit address, network instructions, or "пополни" wording**.
- The deposit address and funding steps appear **only** after `onb_autotrade` → `onb_fund_steps`
  (two explicit taps). They never appear in L0/L1.
- New users land in `is_signal_only=True` (demo); the dashboard hides the funding checklist until they
  opt in.
- **🚀 Перейти к автоторговле** → L1 reassurance → L2 address → 🔐 register → on success
  `is_signal_only` flips to False and the full auto-trading dashboard renders — with no re-entry of any
  data. **↩️ Вернуться в режим сигналов** returns to demo without destroying the wallet.
- No user-facing copy claims the wallet is "non-custodial"; only truthful levers (withdraw-anytime,
  Polygon-only, USDC-only, gasless, no-deposit demo) are used.
- (If free-sample taken) a demo user with no subscription receives the configured sample of real
  signals, then is prompted to subscribe — proving "watch before you fund" end to end.

---

## 5. Coding Guidelines (STRICT — safety first)

These rules are non-negotiable. Money and private keys are at stake.

### 5.1 Private-key protection (highest priority)
- Private keys exist in two forms only: **encrypted at rest** (`wallet_private_key_enc`, Fernet via
  `ENCRYPTION_KEY`) and **decrypted in-memory at the moment of signing**. Never widen this.
- **NEVER** log, print, return in an API/Telegram response, store unencrypted, or put a decrypted key
  (or `ENCRYPTION_KEY`) into Redis, Supabase, error messages, or AI prompts.
- Decrypt only via `core.wallet.decrypt_key` and only inside the function that signs. Do not pass
  decrypted keys across task boundaries or persist them.
- Never include key material, full addresses with secrets, or seed phrases in logs. Truncate
  addresses in logs (existing code uses `addr[:10]`/`[:12]`).
- Treat `.env`, `ENCRYPTION_KEY`, `SUPABASE_SERVICE_KEY`, `BUILDER_*`, `RELAYER_*` as secrets. Never
  commit them; never echo them.

### 5.2 Fail safe — never drain a deposit on API desync
- Polymarket Data/Gamma/CLOB APIs lag and occasionally return stale or partial data. **Assume any
  external call can fail, time out, or lie.** Wrap every external call in try/except and **fail
  CLOSED** (skip the action) rather than trading on bad data.
- Before any BUY: re-check the order book (price band + fillable depth) and the deposit-wallet pUSD
  balance. If balance/depth checks fail, **skip** — do not place the order.
- Always cap order size by BOTH `max_position_usdc` AND order-book depth (`book_safe_frac`). Never
  remove these caps.
- Use **FAK marketable orders with slippage protection** (`order_slippage_pct` / `exit_slippage_pct`)
  via `_worst_buy_price`/`_worst_sell_price`. Never place naked market orders without a worst-price
  bound.
- **Idempotency is mandatory.** Celery tasks retry. Guard against double-execution with: the
  `copy_trades` (user_id, signal_id) existence check, the in-memory `_seen` map, the `trade_signals`
  DB dedup, and Redis `notify_once`/`claim`. Any new money-moving task MUST be idempotent.
- For redemption, **never trust the API `negativeRisk` flag** — detect market type on-chain
  (`redeem_winnings` already does this). A wrong path silently redeems $0.

### 5.3 Concurrency & async in the worker (gevent)
- Workers run in a **gevent pool with no asyncio event loop in the task thread**. To send Telegram
  messages from a task, use **`asyncio.run(_send())`** — **NEVER `asyncio.get_event_loop()`** (it
  raises in non-main threads and silently drops notifications). This bug has bitten us repeatedly.
- The relayer allows **one in-flight action per deposit wallet**. Serialize on-chain actions per
  wallet; on `"wallet busy"` errors, retry with a delay (see `scripts/verify_v2.py redeem`).

### 5.4 Data isolation
- Always scope DB reads/writes by `user_id` / `telegram_id`. Never let one user's data, balance,
  positions, or wallet leak into another user's view or trade.
- A signal fans out to subscribers individually; each `execute_copy_trade` operates on exactly one
  user. Keep it that way.
- Use the supabase client in `core/db/queries.py`. Do not open raw DB connections or scatter table
  names across the codebase — add a query helper instead.

### 5.5 Trade logging & observability
- Use **`structlog`** (`log = structlog.get_logger(__name__)`) with **structured key/value** fields,
  not f-strings. Example: `log.info("copy_trade_ok", user_id=uid, order_id=oid, fill=status)`.
- Worker runs at `--loglevel=info`; `log.debug` is invisible in prod — put diagnostics needed in prod
  at `info`.
- Every money-moving action must leave an audit trail: a `trade_signals` row, a `copy_trades` row with
  status transitions (`executing → placed → confirmed/unfilled/failed`), and an `info` log line.
- Never log secrets (see §5.1). Truncate addresses, tx hashes, and token ids in logs.

### 5.6 Config & constants
- All tunable thresholds live in `core/config.py` (`Settings`). **Do not hardcode magic numbers** in
  task logic — add a setting with a clear comment and a safe default.
- Contract addresses come from `core/clob.py` / `core/polygon.py`. Import them; never re-declare.
- New behavior that moves money or changes strategy should be **gated behind a config flag** with a
  conservative default (off / smallest size).

### 5.7 General
- Match existing style: small focused functions, early returns, `try/except` around all I/O, no
  narrating comments (comment only non-obvious intent/constraints).
- Schema changes go in a new `migrations/00X_*.sql` (idempotent SQL), and the corresponding helper in
  `core/db/queries.py`. Keep `core/db/models.py` updated as reference.
- Prefer editing existing modules over adding new ones. Do not introduce new external dependencies
  without a strong reason.
- After editing, mentally run the failure cases: API down, partial fill, retry, restart, Redis empty,
  insufficient balance. The bot must degrade safely in all of them.

---

## 6. Database Schema (Supabase / Postgres)

**Runtime access is via the Supabase client** (`core/db/queries.py`) — there is **no ORM at runtime**.
`core/db/models.py` (SQLAlchemy) is a **reference that has drifted** from the live schema; the live
schema is the **base tables + the applied `migrations/00X_*.sql` files**. Where they disagree, the
**migrations + actual `queries.py` usage win** (noted inline below).

Migrations are applied **manually** in the Supabase SQL editor, in order, and are idempotent
(`create table if not exists` / `add column if not exists`).

### 6.1 `users` — subscribers (custodial wallets + subscription state)

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | internal id (FK target for `copy_trades.user_id`) |
| `telegram_id` | bigint unique | Telegram user id |
| `username` | text | Telegram @username (migration 003); indexed `lower(username)` |
| `wallet_address` | text(42) | user EOA (signer) |
| `wallet_private_key_enc` | text | **Fernet-encrypted** EOA private key — NEVER expose/log (§5.1) |
| `deposit_wallet_address` | text | V2 deposit wallet (ERC-1967 proxy, order `funder`) — migration 005 |
| `deposit_wallet_deployed` | bool | proxy deployed on-chain — migration 005 |
| `wallet_registered` | bool | on-chain approvals done — migration 001 |
| `clob_api_key` / `clob_secret` / `clob_passphrase` | text | per-user CLOB creds — migration 001 (treat as secrets) |
| `sub_tier` | text | `'free'` = inactive; any non-`free` (runtime uses `'active'`) = active |
| `sub_expires_at` | timestamptz | subscription expiry; gate = non-`free` AND `> now()` |
| `copy_active` | bool | custodial copy enabled (checked only when `auto_copy_enabled`) |
| `max_position_usdc` | float | per-position cap (default 25.0) — current fixed-sizing input |
| `balance_usdc` | double precision | cached balance for the deposit monitor — migration 001 |
| `equity_hwm` | double precision | per-user equity high-water mark (Blueprint 4) — migration 010 |
| `copy_paused_until` | timestamptz | risk-pause expiry; fan-out + pre-trade gate honor it — migration 010 |
| `sizing_mode` | text | per-user `fixed`/`kelly` override (Blueprint 3) — migration 011 |
| `risk_state` | text | `active`/`paused_drawdown`/`paused_daily_loss` state machine (Blueprint 8) — migration 013 |
| `risk_override_at` / `risk_override_count` | timestamptz / int | manual-unblock consent audit trail (Blueprint 8) — migration 013 |
| `realized_baseline` | double precision | equity baseline for profit-protection cap (Blueprint 8) — migration 013 |
| `is_signal_only` | boolean | Signal-Only Mode: deliver signals, never trade on-chain (BP14) — migration 014 |
| `subscription_notified_expired` | boolean | dedup flag for the "subscription expired" alert (BP14) — migration 014 |
| `created_at` | timestamptz | |

> `models.py` lists `privy_user_id` (legacy/unused) and a `SubTier` enum (`basic/pro/whale`) that the
> runtime does **not** use — production is a **single `'active'` tier**.

### 6.2 `tracked_wallets` — Model B whitelist (whales to copy) — migration 007

| Column | Type | Notes |
|---|---|---|
| `id` | bigint identity PK | |
| `address` | text unique | whale wallet (lowercased on write) |
| `label` | text | display name |
| `active` | bool | only `active=true` are polled; indexed |
| `added_at` | timestamptz | |

### 6.3 `trade_signals` — detected copy signals (one per whale entry burst)

Base (`models.py`) + migrations 001/006/007:

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `donor_id` | int FK → `donor_wallets.id` | **nullable** (migration 001) — whale signals have no donor |
| `market_id` | text | Polymarket `conditionId` |
| `title` | text | market title (**runtime inserts `title`**; `models.py` calls it `market_title` — stale) |
| `side` | text | `BUY` (YES/NO outcome captured via `token_id`) |
| `price` | float | VWAP of the aggregated entry |
| `size_usdc` | float | aggregated whale notional |
| `token_id` | text | exact outcome token bought — migration 001 |
| `source_tx_hash` | text | dedup key across restarts — migration 001 (indexed) |
| `source_wallet` | text | tracked wallet that triggered it — migration 007 |
| `consensus` | int default 1 | distinct tracked wallets backing this market/outcome — migration 007 |
| `whale_wallet` | text | resolved buyer (observe-mode scoring) — migration 006 |
| `whale_realized_pnl` / `whale_resolved_count` / `whale_winrate` / `whale_passed` | float/int/float/bool | track-record snapshot — migration 006 |
| `ai_score` / `ai_reason` | int / text | OpenAI risk analysis (informational) |
| `created_at` | timestamptz | indexed desc |

### 6.4 `copy_trades` — per-subscriber mirrored trades (audit trail)

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `user_id` | int FK → `users.id` | the subscriber |
| `signal_id` | int FK → `trade_signals.id` | source signal |
| `status` | text | `executing → placed → confirmed \| unfilled \| failed` |
| `size_usdc` | float | intended, then updated to filled amount |
| `order_id` | text | CLOB order id (**runtime writes `order_id`**; not in `models.py`) |
| `tx_hash` | text | on-chain tx (legacy field in `models.py`) |
| `fill_price` | float | |
| `pnl_usdc` | float | realized P&L (settlement) |
| `error_msg` | text | failure reason (truncated) |
| `created_at` / `updated_at` | timestamptz | |

> Idempotency uses `(user_id, signal_id)` + status (see §5.2). **Blueprint 1 (§4) adds settlement
> columns** (`condition_id, token_id, outcome_index, neg_risk, entry_price, shares, result,
> realized_pnl, resolved_at, redeemed_at, redeem_tx`) via migration 008. **Blueprint 6 (§4)** makes
> a token-sale exit terminal: `status='closed'`, `result='closed'`, P&L booked from the actual sale,
> plus `exit_tx` via migration 012 (apply before next deploy). `status='closed' OR redeemed_at IS NOT NULL`
> ⇒ the row is permanently excluded from settlement/auto-claim.

### 6.5 `donor_wallets` — legacy donor-copy (Model A, dormant) — `models.py`

`id`, `address` (unique), `label`, `win_rate_30d`, `roi_30d`, `total_volume_usdc`, `active`,
`last_seen_at`, `updated_at`. Not used by the active Model B path.

### 6.6 `access_codes` — one-time subscription activation — migration 002

| Column | Type | Notes |
|---|---|---|
| `code` | text PK | redeemable code (`secrets.token_urlsafe`) |
| `tier` | text default `'active'` | tier granted |
| `days` | int default 30 | subscription length |
| `note` | text | admin note |
| `used_by` | bigint | telegram_id that redeemed (null = unused; partial index on unused) |
| `used_at` | timestamptz | |
| `created_at` | timestamptz | |

> Redeem is **atomic**: `update ... where code=? and used_by is null` (guards double-redeem races).

### 6.7 `admins` — admin registry — migration 004

`telegram_id` (bigint PK), `username`, `active` (bool), `added_by` (bigint), `created_at`. The
super-admin (`ADMIN_TELEGRAM_ID` from env) is always authorized regardless of this table.

### 6.8 `admin_codes` — one-time admin invite codes — migration 004

`code` (text PK), `note`, `used_by` (bigint), `used_at`, `created_at`. Same atomic-claim pattern as
`access_codes`.

### 6.9 Pending migrations (SQL written, **must be applied manually** in Supabase SQL editor)

- **008** — `copy_trades` settlement ledger (Blueprint 1) → `migrations/008_settlement_ledger.sql`
- **009** — `tracked_wallets.avg_trade_usdc` (Blueprint 2) → `migrations/009_tracked_avg_size.sql`
- **010** — `users.equity_hwm`, `users.copy_paused_until` (Blueprint 4) → `migrations/010_risk_controls.sql`
- **011** — `users.sizing_mode` (per-user Kelly/fixed toggle) → `migrations/011_user_sizing_mode.sql`
- **012** 🔴 — `copy_trades.exit_tx` + `(user_id, condition_id)` index (Blueprint 6) →
  `migrations/012_position_state.sql` (also documents `copy_trades.result='closed'` for token-sale exits)
- **013** ✅ — `users.risk_state` + `risk_override_at` + `risk_override_count` +
  `realized_baseline` (Blueprint 8) → `migrations/013_risk_state_override.sql`
  (**applied** — deploy with Blueprint 8 code)
- **014** ✅ — `users.max_daily_trades` (Blueprint 13.2; `NULL` = unlimited) →
  `migrations/014_max_daily_trades.sql` (applied — deploy with Blueprint 13 code)
- **015** ✅ — `trade_signals.ai_signal_type` (Blueprint 14.B structured AI output) →
  `migrations/015_ai_signal_type.sql` (applied — deploy with Blueprint 14 code)
- **016** ✅ — `copy_trades.entry_bid` + `users.risk_override_until` (Blueprint 17) →
  `migrations/016_entry_bid.sql`
- **017** 🔴 — `trade_signals.outcome` + partial index `(user_id, redeemed_at desc)` on
  `copy_trades` (Blueprint 22 — admin-bot audit) → `migrations/017_trade_signals_outcome.sql`
  (**apply before deploying the BP22 code**; the poller has a retry-without-column
  fail-safe, but the admin history outcome display needs the column)

### 6.10 Migration order

`001` whale strategy → `002` access codes → `003` username → `004` admins → `005` deposit wallets →
`006` wallet score → `007` tracked wallets → `008` settlement ledger → `009` tracked avg size →
`010` risk controls → `011` user sizing mode → `012` position state (exit_tx + user/condition index) →
`013` risk state + manual override (Blueprint 8) → `014` max daily trades (Blueprint 13.2) →
`015` ai_signal_type (Blueprint 14.B) → `016` entry_bid + risk_override_until (Blueprint 17) →
`017` trade_signals.outcome + history index (Blueprint 22).

---

## 7. Server & Deploy Guide

### 7.1 Infrastructure

| Component | Where |
|---|---|
| **VPS** | AWS EC2 — `ubuntu@ip-172-26-8-174` |
| **Project root** | `/home/ubuntu/app` |
| **Process manager** | Docker (plugin, `docker compose` without hyphen) |
| **Redis** | container `app-redis-1` (redis:7-alpine, port 6379, internal only) |

Running containers (check with `docker compose ps`):

| Container | Image | Role |
|---|---|---|
| `app-api-1` | `app-api` | FastAPI (uvicorn), port 8000 → host |
| `app-worker-1` | built image | Celery worker (gevent, queues: trades/ai/periodic) |
| `app-beat-1` | `app-beat` | Celery beat scheduler — **must be exactly 1 replica** |
| `app-redis-1` | redis:7-alpine | Broker + backend |

### 7.2 Standard deploy (after `git push` from local)

```bash
# 1. SSH into the server
ssh ubuntu@ip-172-26-8-174

# 2. Pull latest code
cd /home/ubuntu/app
git pull origin master

# 3. Apply any new migrations in Supabase SQL Editor (see §7.4)

# 4. Rebuild images and restart containers
docker compose build --no-cache api worker beat
docker compose up -d api worker beat

# 5. Verify
docker compose ps
docker compose logs --tail=50 worker
docker compose logs --tail=50 beat
```

### 7.3 Quick restart (no code changes, just config / env)

```bash
cd /home/ubuntu/app
docker compose restart api worker beat
```

### 7.4 Applying database migrations

Migrations are plain idempotent SQL files in `migrations/`. They **must be applied manually**
in the Supabase SQL Editor **before** restarting the worker, because new code may write to
columns that don't exist yet.

1. Open [supabase.com](https://supabase.com) → your project → **SQL Editor**.
2. Copy-paste the contents of each new `migrations/00X_*.sql` file and click **Run**.
3. Apply in order: `008` → `009` → `010` → …

**Migrations applied as of the last deploy (2026-07-02):** 001–014 (all applied in prod).
Migration **014** (`is_signal_only`, `subscription_notified_expired`) backs the production Signal-Only Mode +
Subscription Enforcer (BP14); verified live on 2026-07-02 (columns present, enforcer end-to-end healthy).
Any future migration (015+) must be applied in the Supabase SQL Editor **before** restarting the worker.

### 7.5 Useful diagnostic commands

```bash
cd /home/ubuntu/app

# Tail live worker logs
docker compose logs -f worker

# Tail beat logs
docker compose logs -f beat

# Check all container health
docker compose ps

# Drop into a running worker shell (for one-off scripts)
docker compose exec worker bash

# Run a one-off Python script inside the worker context
docker compose exec worker python scripts/seed_quality.py

# Restart only the beat scheduler (e.g. after beat_schedule change)
docker compose restart beat

# Hard rebuild a single service
docker compose build --no-cache worker
docker compose up -d worker
```

### 7.6 Environment / secrets

Secrets are in `.env` at the project root (not committed). After SSH-ing in:

```bash
cat /home/ubuntu/app/.env   # view current config
nano /home/ubuntu/app/.env  # edit (then restart the affected containers)
```

Key variables to verify are set: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ENCRYPTION_KEY`,
`TELEGRAM_BOT_TOKEN`, `POLYGON_RPC_URL`, `ALCHEMY_API_KEY`, `BUILDER_API_KEY`,
`BUILDER_SECRET`, `BUILDER_PASSPHRASE`, `REDIS_URL`, `AUTO_COPY_ENABLED`.

---

## Blueprint 16: Fix Entry Price & PnL in Positions List 🟡 FINAL DESIGN / READY TO IMPLEMENT

> **Class of bug:** Data Persistence / State read-path defect (NOT a write-path loss). The correct
> entry price *is* in our DB — the `/positions` view simply never reads it and trusts an on-chain
> source that returns `0` for our custodial proxy wallets.

### 16.0 Symptom (prod, PolyMind AI)

1. On entry, the trade notification (`_notify` in `worker/tasks/execute_copy.py`) shows the **correct**
   price (e.g. `@ 0.875`) and invested amount (e.g. `$5.60`). ✅
2. In `/positions` (`_build_positions` in `api/routers/telegram.py`) **every** position renders entry
   price as `@ 0.000`, so the PnL % renders `(+0%)` and the "→ cur" arrow starts from zero. ❌

The two numbers come from **two different sources** that were never reconciled — the exact same class
of "displayed value ≠ stored value" split diagnosed for withdrawals in **Blueprint 12 Part B**.

### 16.1 Root-cause analysis

**Write path — CORRECT (not the bug).** `execute_copy_trade` already denormalizes the entry price onto
the `copy_trades` row (Blueprint 1, migration 008):

```485:486:worker/tasks/execute_copy.py
        "entry_price":    round(entry_price, 6),
    })
```

`entry_price` is the fresh order-book `best_ask` (L368–373), a non-zero float. So the DB **does** hold
the right cost basis. (Caveat hardened in §16.4: nothing currently *guarantees* it is `> 0`.)

**Read path — THE BUG.** `/positions` is sourced **100% on-chain / Data-API**, with **zero JOIN** to
our own `copy_trades` table:

```883:890:api/routers/telegram.py
    for i, p in enumerate(positions):
        title = (p.get("title") or "—")[:40]
        outcome = p.get("outcome") or "—"
        shares = p["shares"]
        avg = p["avg_price"]
        cur = p["cur_price"]
        pnl = p["cash_pnl"]
        pct = p["percent_pnl"]
```

`avg_price` / `percent_pnl` originate solely from the Polymarket Data API:

```437:445:core/polymarket.py
                "shares":       float(p.get("size") or 0),
                "avg_price":    float(p.get("avgPrice") or 0),
                "cur_price":    float(p.get("curPrice") or 0),
                "current_value": float(p.get("currentValue") or 0),
                "cash_pnl":     float(p.get("cashPnl") or 0),
                # ...
                "percent_pnl":  float(p.get("percentPnl") or 0) / 100.0,
```

For our **custodial deposit wallets** (POLY_1271 relayer-funded proxy funders — §5), the Data API
`avgPrice` is unreliable and frequently `0`:
- **Indexing lag** — freshly-opened positions surface with `avgPrice = 0` before the indexer back-fills
  the fill price (the code already *knows* this — see the comment at `_build_pnl`,
  `api/routers/telegram.py` L952–954: *"avg_price momentarily 0 → it reports the whole position value
  as profit"*).
- **Proxy attribution** — fills routed through the 1271 proxy are not always attributed with a blended
  `avgPrice` on the funder address the way a plain EOA trade is.

When `avgPrice = 0`: `avg` renders `0.000`, and the API's own `percentPnl` is `0` (it cannot compute a
return with no cost basis) → `(+0%)`. **The bug is not that the price was lost — it is that the read
path throws away the DB copy we already have and trusts a source that returns 0.**

**Math-safety latent bug.** `_build_positions` currently consumes the *pre-computed* `percent_pnl`, so
it does not divide by zero today. But the moment we compute the return locally from the entry price
(the fix), `pct = (cur - entry) / entry` divides by zero when `entry == 0`. `_build_pnl` already dodges
this defensively (`if avg > 0.001`) — the fix must apply the **same guard** in `_build_positions`.

### 16.2 Fix strategy — DB is the source of truth for entry price

Three coordinated changes; **no new migration** (the `entry_price` column already exists — migration 008).

**A. New read helper — `get_entry_prices_by_token(user_id)` (`core/db/queries.py`).**
Return `{token_id: entry_price}` for the user's open (`status in ('confirmed','executing')`,
`redeemed_at IS NULL`) `copy_trades`. Mirror `get_open_trades_cost` (L618) exactly, but select
`token_id, entry_price` and keep the **latest non-zero** entry per token (guards partial-fill dupes):

```python
def get_entry_prices_by_token(user_id: int) -> dict:
    """{token_id: entry_price} for open (confirmed/executing, unredeemed) copy_trades.
    Local cost-basis fallback for the /positions view when the Data-API avgPrice is 0."""
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("token_id, entry_price, created_at")
        .eq("user_id", user_id)
        .in_("status", ["confirmed", "executing"])
        .is_("redeemed_at", "null")
        .order("created_at", desc=True)
        .execute()
    )
    out: dict = {}
    for row in (res.data or []):
        tid = row.get("token_id")
        ep = float(row.get("entry_price") or 0)
        if tid and ep > 0 and tid not in out:   # first (newest) non-zero wins
            out[tid] = ep
    return out
```

> **CRITICAL — export it.** Add `get_entry_prices_by_token` to **both** the
> `from core.db.queries import (...)` block **and** `__all__` in `core/db/__init__.py`. This is the
> exact drift that caused **Blueprint 12 Part A** (`get_open_trade_by_token` missing from exports) —
> do not repeat it. If the BP9 boot self-check `_check_core_imports()` exists, add the name there too.

**B. Overlay DB entry price in the read path (`api/routers/telegram.py`).**
In `_build_positions` (and reuse the same map in `_build_pnl`), fetch the map once and pick the
**effective entry** per position — prefer a valid on-chain blended `avgPrice`, fall back to our DB copy:

```python
# once, before the loop:
from core.db import get_entry_prices_by_token
db_entry = get_entry_prices_by_token(db_user["id"])

# per position:
api_avg = float(p.get("avg_price") or 0)
entry   = api_avg if api_avg > 0 else db_entry.get(p["token_id"], 0.0)
```

Rationale for the precedence (`API-if-nonzero, else DB`): once the indexer catches up, `avgPrice` is
the *blended* truth across partial fills / add-ons; our single-row `entry_price` is only the first
fill. So we trust the API when it is populated and use the DB purely as the **zero-gap fallback** that
kills the `0.000` display. (If a market is later found where the API blends *worse* than our ledger,
revisit — but do not silently override a valid non-zero API avg.)

**C. Compute PnL locally with a divide-by-zero guard (single source of math truth).**
Replace the direct `pct = p["percent_pnl"]` / `pnl = p["cash_pnl"]` reads with a guarded local compute,
so display, %, and $ all derive from the same `entry`:

```python
if entry > 0:
    pnl = shares * (cur - entry)
    pct = (cur - entry) / entry
else:
    # No cost basis anywhere — happens for legacy pre-BP1 rows (verified in §16.7,
    # ids 674–690: entry_price AND shares both NULL). Show a dash, never 0%/NaN.
    pnl = float(p.get("cash_pnl") or 0)
    pct = None
```

Render `pct` as `"—"` when `None` (never `+0%`, never `ZeroDivisionError`, never `inf/NaN`). Keep the
`avg:.3f` in the row using `entry` so the arrow reads `0.875 → 0.910` instead of `0.000 → 0.910`.

### 16.3 Files touched (no migration)

| File | Change |
|---|---|
| `core/db/queries.py` | **+** `get_entry_prices_by_token(user_id)` (new helper, §16.2 A) |
| `core/db/__init__.py` | **+** re-export the helper in the import block **and** `__all__` (BP12-A guard) |
| `api/routers/telegram.py` | `_build_positions` + `_build_pnl`: overlay DB entry, guarded local PnL (§16.2 B/C) |
| `worker/tasks/execute_copy.py` | **hardening only** — enforce `entry_price > 0` invariant on write (§16.4) |

### 16.4 Write-path hardening (defense-in-depth, secondary)

`entry_price` is written today, but nothing guarantees it is non-zero, and if it ever is, the fallback
in §16.2 silently degrades back to `0.000`. Add a cheap invariant in `execute_copy_trade` right before
the `insert_copy_trade` at L475:

- `entry_price` is derived from `book["best_ask"]` and falls back to `signal["price"]`. If **both** are
  `0`/missing, either **skip** (`reason="no_entry_price"`) or persist the best available signal price —
  never insert a `0` cost basis. Log `log.warning("entry_price_zero_guard", ...)` when it triggers so a
  regression in the order-book path is visible, not silent.

**Model drift note (cosmetic, flag don't chase):** `core/db/models.py::CopyTrade` still declares
`fill_price` and has **no** `entry_price` column (models are "schema reference only", §2.1, and are not
used at runtime — writes go through raw dicts to Supabase). Update the SQLAlchemy model to match the
live schema (`entry_price`, `shares`, `condition_id`, `token_id`, `outcome_index`, `neg_risk`,
`result`, `realized_pnl`, `resolved_at`, `redeemed_at`, `redeem_tx`, `exit_tx`) so the reference stops
lying — but this does not affect the runtime fix.

### 16.5 Acceptance criteria

1. For a position opened at `0.875`, `/positions` shows `… @ 0.875 → {cur} · {icon} {pnl:+.2f}$ ({pct:+.0%})`
   with the **real** entry and a non-zero, correctly-signed % — even immediately after entry while the
   Data-API `avgPrice` is still `0`.
2. A position with **no** DB entry and a `0` API avg renders `—` for the %, **never** `+0%` and never
   raises `ZeroDivisionError`.
3. `python -c "from core.db import get_entry_prices_by_token"` succeeds (export wired, BP12-A guard).
4. `_build_pnl` unrealized total uses the same effective entry (no phantom "whole value = profit" when
   `avgPrice=0`).
5. No new migration required; `copy_trades.entry_price` (migration 008) is unchanged.

### 16.6 DB verification commands (run BEFORE coding to confirm the hypothesis)

The store is **Supabase Postgres** (§2.1) — use `psql` with the project's pooler/direct connection
string (Supabase Dashboard → Project Settings → Database → Connection string), or the supabase-client
one-liner below (works with the `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` already in `.env`).

```bash
# --- Option A: psql against Supabase Postgres -------------------------------
# Paste your connection string (Session pooler / Direct). NEVER commit it.
export PGURL='postgresql://postgres.<ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:5432/postgres'

# 1) Distribution of entry_price on OPEN trades — is it really zeros/nulls?
psql "$PGURL" -c "
  select
    count(*)                                   as open_trades,
    count(*) filter (where entry_price is null) as null_entry,
    count(*) filter (where entry_price = 0)     as zero_entry,
    count(*) filter (where entry_price > 0)     as good_entry,
    min(entry_price), max(entry_price)
  from copy_trades
  where status in ('confirmed','executing') and redeemed_at is null;
"

# 2) Eyeball the most recent open rows — compare stored entry_price vs size_usdc/shares.
psql "$PGURL" -c "
  select id, user_id, left(token_id,14) as token, status,
         entry_price, shares, size_usdc, created_at
  from copy_trades
  where redeemed_at is null
  order by created_at desc
  limit 20;
"

# 3) Cross-check: are the notified trades the ones showing 0.000?  (entry_price=0 but shares>0)
psql "$PGURL" -c "
  select count(*) as zero_priced_open_positions
  from copy_trades
  where status in ('confirmed','executing')
    and redeemed_at is null
    and coalesce(entry_price,0) = 0;
"
```

```bash
# --- Option B: no psql? use the supabase client already configured in .env --
# Reads SUPABASE_URL + SUPABASE_SERVICE_KEY from the environment / .env.
python - <<'PY'
from core.db.session import get_supabase
sb = get_supabase()
rows = (sb.table("copy_trades")
        .select("id,user_id,token_id,status,entry_price,shares,size_usdc,created_at")
        .is_("redeemed_at","null")
        .order("created_at", desc=True)
        .limit(50).execute().data or [])
open_rows = [r for r in rows if r["status"] in ("confirmed","executing")]
zero = [r for r in open_rows if not r.get("entry_price")]
print(f"open (confirmed/executing, unredeemed): {len(open_rows)}")
print(f"  with entry_price 0/NULL: {len(zero)}")
print(f"  with entry_price > 0   : {len(open_rows)-len(zero)}")
for r in open_rows[:15]:
    print(f"  id={r['id']:>5} tok={str(r['token_id'])[:12]:<12} "
          f"entry={r.get('entry_price')!s:<8} shares={r.get('shares')!s:<8} "
          f"size=${r.get('size_usdc')}")
PY
```

**How to read the result:**
- **`entry_price > 0` in the DB (good_entry ≫ 0) while `/positions` shows `0.000`** → confirms the
  read-path/JOIN diagnosis (§16.1): the data is fine, the view ignores it. Fix = §16.2 B/C only.
- **`entry_price` is `0`/`NULL` in the DB (zero_entry/null_entry ≫ 0)** → the write path is *also*
  degrading; apply the §16.4 write guard **in addition to** the read-path fix.

### 16.7 Verification result — prod, 2026-07-01 ✅ HYPOTHESIS CONFIRMED

Ran Option B against prod (`docker compose exec -T worker python …`). Of **25** open
(`confirmed`/`executing`, unredeemed) `copy_trades`:

| bucket | count | meaning |
|---|---|---|
| `entry_price > 0` in DB | **18** | correct cost basis stored (`0.88`, `0.931`, `0.95`, …) yet `/positions` renders `0.000` |
| `entry_price` NULL in DB | **7** *(8 within a 200-row window)* | **all legacy** — see below |

**Conclusion 1 — read-path is THE bug, and the fix is sufficient.** 18/18 of the *recent* trades
(ids ≥ 709, created ≥ 2026-06-21) carry a valid `entry_price`, but the on-chain-only `/positions`
view shows `0.000` for them. The data was never lost — the view throws it away. **§16.2 (B/C) fully
resolves the active symptom.**

**Conclusion 2 — write-path is NOT currently degrading.** Every NULL-entry row is a **legacy tail**
created **2026-06-15 → 2026-06-20** (ids 674–690) with **both** `entry_price = NULL` **and**
`shares = NULL` — i.e. rows written *before* the Blueprint 1 / migration 008 denormalization went
live. No post-BP1 row is missing its price. Therefore **§16.4 is demoted to defense-in-depth**
(cheap future insurance), **not** a required part of this fix.

**Consequence for the legacy rows.** Those 8 rows have no cost basis *anywhere* (not in the API,
not in our DB), so the §16.2-B fallback cannot rescue them — they rely purely on the §16.2-C
divide-by-zero guard to render `—` (never `+0%`, never a crash) and will age out on resolution.
This makes the §16.2-C guard **load-bearing**, not merely defensive.

**Incidental finding (out of scope, log for later):** the query revealed **`copy_trades.order_id`
does not exist** in the live schema, yet `execute_copy_trade` writes it inside a swallowing
`try/except: pass`:

```505:508:worker/tasks/execute_copy.py
            sb.table("copy_trades").update({
                "status":   "placed",
                "order_id": order_id,
            }).eq("id", trade_row["id"]).execute()
```

Because both fields go in one `update`, the missing column makes the **entire** statement fail
silently → the interim `status='placed'` transition never persists (the row stays `executing` until
the later `confirmed`/`unfilled` update, which omits `order_id`, succeeds). Not part of Blueprint 16;
file as a follow-up (either add an `order_id text` column via migration, or drop the field from the
write). Flagging so it is not lost.

---

## Blueprint 17 — Spread-Trap stop-loss hardening + Broken-Override state reset ✅ IMPLEMENTED 2026-07-02

Two independent prod risk-manager defects, designed together because both live in the risk/exit
path and share the same monitor (`sync_positions` → `_update_hwm_and_check_breakers`) and the same
override button (`callback_data="unlock_drawdown"`).

- **17.A — Spread Trap:** the Delta-Drop stop fires within minutes of entry on illiquid markets,
  closing profitable/neutral positions at a loss because it compares the **ask-based fill price**
  against the **live best_bid** across a wide spread — measuring the spread, not a real move.
- **17.B — Broken Override State:** the manual "Снять блокировку" handler lifts the pause and resets
  the HWM baseline but **never neutralizes the daily-loss counter**, so the next monitor cycle
  re-trips `paused_daily_loss` and re-locks the user in a loop, with no new losing trades.

### 17.1 Root cause — 17.A (Spread Trap)

`worker/tasks/manage_positions.py` (BP10 Delta-Drop block, L166–195):

```183:195:worker/tasks/manage_positions.py
            # Delta-Drop trigger: exit if best_bid fell >= X from entry.
            if entry_px > 0 and best_bid > 0:
                drop_pct = 1.0 - best_bid / entry_px
                if drop_pct >= settings.delta_drop_stop_pct:
                    log.info("delta_drop_triggered", ...)
                    _closing.add(ckey)
                    close_position.delay(uid, token_id, "delta_drop_stop")
```

- `entry_px = float(p.get("avg_price"))` — the Data-API cost basis, i.e. we **bought at the ask**
  (`execute_copy` sets `entry_price = book["best_ask"]`, L368–373).
- `best_bid` — the **live top-of-book bid** from `get_order_book(token_id)`.

On an illiquid binary market the resting spread is enormous (buy fills at ask `0.80`, the best
standing bid is `0.50`). `drop_pct = 1 - 0.50/0.80 = 0.375 ≥ 0.30` → **instant stop**, even though
the market's fair value (mid ≈ `0.65`) hasn't moved and the probability hasn't fallen. We are
measuring the **bid-ask spread**, not a drop in win probability, then crystallising it as a real
loss by market-selling into that same thin bid.

The existing `delta_drop_min_hold_sec = 600` (10 min) guard does **not** save us: the spread is
present from t=0 and persists, so the drop is still there after the hold window expires. And
`_first_seen` is an in-process dict that **resets on worker restart** (L32–34), so a restart can
make an old position instantly "mature" and eligible.

### 17.2 Design — 17.A Spread-Trap defense (layered, defense-in-depth)

The stop must trigger on a **real fall in fair value**, not on an empty book. Four layers; ship all
of them (cheap, independent, each closes a different failure mode):

**Layer 1 — Measure drop from MID, not from best_bid.**
`get_order_book` already returns `best_bid` **and** `best_ask`. Compute
`mid = (best_bid + best_ask) / 2` and `drop_pct = 1 - mid/entry_px`. Mid is spread-insensitive:
a hollow bid with a sane ask no longer fabricates a drop. Fall back to best_bid only if `best_ask`
is missing/zero (and in that case Layer 2 will veto anyway).

**Layer 2 — Spread / liquidity gate (veto).**
Before trusting *any* stop signal, compute `spread_pct = (best_ask - best_bid) / mid`. If
`spread_pct > settings.max_spread_for_stop_pct` (proposed `0.08`), the book is too thin to be a
reliable price source → **skip the stop this cycle** and log `stop_skipped_wide_spread`
(user_id, token, best_bid, best_ask, spread_pct). Optionally also require a minimum bid size/notional
(`min_bid_notional_usdc`) if depth is available from the book. This is the single highest-leverage
fix: it directly rejects the Spread-Trap condition.

**Layer 3 — Entry-anchored bid baseline (reference the book we actually entered against).**
Persist the CLOB `best_bid` observed **at buy time** as `copy_trades.entry_bid` (written in
`execute_copy` next to `entry_price`). Then the "real move" test becomes
`drop_from_entry_bid = 1 - best_bid/entry_bid` — a like-for-like bid-vs-bid comparison that is
immune to the ask-vs-bid spread asymmetry entirely. Fire the stop only when **both** the mid-based
drop (Layer 1) *and* the entry-bid-based drop clear the threshold. (If `entry_bid` is NULL for
legacy rows, gracefully skip this leg and rely on Layers 1+2+4.)

**Layer 4 — Persistence / debounce + robust cooldown.**
- Require the drop to persist across **N consecutive polls** (`delta_drop_confirm_ticks`, proposed
  `2`) before closing — a single hollow-book snapshot never triggers. Track a small per-token
  breach counter (in-process is fine; a transient false reading simply resets it).
- Anchor min-hold to the **trade's real age**, not the in-process `_first_seen` dict: read
  `copy_trades.created_at` for the token and enforce `age >= delta_drop_min_hold_sec`. Bump the
  window to **900s (15 min)** per the incident. This survives worker restarts (the current dict does
  not).

**Recommended shipping combination:** Layer 2 (spread veto) + Layer 1 (mid) are mandatory and
sufficient to kill the reported symptom; Layers 3 + 4 make it robust and calibratable. `hard_stop`
(absolute floor at `0.07`) stays as-is — it is a true "market has abandoned this outcome" net and is
not spread-sensitive at that price.

### 17.3 Config knobs — 17.A (add to `core/config.py`)

| Setting | Proposed default | Meaning |
|---|---|---|
| `max_spread_for_stop_pct` | `0.08` | above this (`(ask-bid)/mid`) the book is untrustworthy → skip stop |
| `delta_drop_use_mid` | `True` | measure drop from mid instead of best_bid |
| `delta_drop_confirm_ticks` | `2` | consecutive breaching polls required before closing |
| `delta_drop_min_hold_sec` | `900` | raise from 600 → 15 min, anchored to `created_at` |
| `min_bid_notional_usdc` | `0` (opt-in) | optional depth floor before trusting the bid |

### 17.4 Files touched — 17.A

| File | Change |
|---|---|
| `worker/tasks/manage_positions.py` | Delta-Drop block: fetch `best_ask`; compute `mid` + `spread_pct`; add spread veto (Layer 2), mid drop (Layer 1), entry-bid drop (Layer 3), persistence counter + `created_at`-anchored hold (Layer 4); new log events `stop_skipped_wide_spread`, `delta_drop_confirming` |
| `worker/tasks/execute_copy.py` | Persist `entry_bid` (CLOB best_bid at fill) alongside `entry_price` |
| `core/config.py` | Add the five knobs in §17.3 |
| `core/db/queries.py` | `get_open_trade_by_token` / cost helper: also return `created_at`, `entry_bid` |
| `migrations/016_entry_bid.sql` | `alter table copy_trades add column if not exists entry_bid double precision;` |

### 17.5 Acceptance criteria — 17.A

1. A position bought at ask `0.80` with a resting bid `0.50` and ask `0.80` (mid `0.65`, spread
   `~0.46`) is **NOT** closed: the spread veto (Layer 2) skips it and logs `stop_skipped_wide_spread`.
2. A position whose **mid** genuinely falls ≥30% from entry across ≥2 polls on a tight book (spread
   < 8%) **is** closed with `reason="delta_drop_stop"` — real stops still work.
3. No stop can fire before `created_at + 900s`, and this holds **across a worker restart**.
4. `hard_stop` behaviour at `< 0.07` is unchanged.

---

### 17.6 Root cause — 17.B (Broken Override State / cyclic re-lock)

The monitor re-pauses on daily loss using a counter the override never touches:

```1138:1147:worker/tasks/manage_positions.py
        # ── Daily loss limit ──────────────────────────────────────────────────
        daily_pnl = get_daily_realized_pnl(uid)
        if daily_pnl <= -(settings.daily_loss_limit_pct * equity):
            ...
            pause_user_copying(uid, next_utc_day)
            set_risk_state(uid, "paused_daily_loss")
```

`get_daily_realized_pnl` sums `copy_trades.realized_pnl` over the trailing 24h (queries.py L515–529).
The `unlock_drawdown` handler (`api/routers/telegram.py` L2117+) does:
`resume_user_copying` (clears `copy_paused_until`) → `reset_risk_baseline` (resets `equity_hwm` +
`realized_baseline`, fixing **only** the *drawdown* breaker) → `record_risk_override` (audit) →
`set_risk_state("active")` → deletes Redis keys `drawdown_alert` / `risk_gate:*:drawdown` /
`resume_alert`.

**Nothing resets or masks the daily-loss sum.** So on the next `sync_positions` (seconds later):
`current_state` is now `active` (the guard `if current_state in (paused_*): return` no longer
short-circuits) → `daily_pnl` is still the same large negative number → the daily-loss branch
re-fires, re-pauses, and re-sends the alert. Infinite re-lock, zero new losing trades. (The
`daily_loss` alert has no `notify_once` guard — it relied on the state machine that the override just
cleared.)

### 17.7 Design — 17.B State Reset (hard, self-expiring override flag)

Adopt the user-proposed **`manual_override_until_midnight_utc`** as a first-class, hard-respected
flag. This is preferred over "zero the counter" because (a) it is self-documenting, (b) it expires
exactly when the daily-loss window rolls over (00:00 UTC), matching daily-loss semantics, and (c) it
also protects the drawdown breaker from an immediate re-trip while equity is still depressed.

**Schema (migration 016):**
`alter table users add column if not exists risk_override_until timestamptz;`
(distinct from the existing `risk_override_at` audit stamp).

**Handler (`unlock_drawdown`, api/routers/telegram.py):** in addition to the current steps, set
`risk_override_until = next 00:00 UTC` via a new `set_risk_override_until(uid, ts)` query. Keep the
existing HWM/baseline reset and audit increment.

**Monitor (`_update_hwm_and_check_breakers`):** at the very top, after loading state, add a hard
guard:

```text
override = user.get("risk_override_until")
if override and now_utc < parse(override):
    return   # user explicitly accepted risk for the rest of the UTC day — do NOT re-pause
# (optional) if override elapsed, clear it and continue normally
```

This suppresses **both** breakers for the remainder of the UTC day, so no re-lock is possible. At
midnight UTC the flag lapses and normal protection resumes automatically.

**Pre-trade path (`execute_copy.check_risk_gates` caller, L400–411):** honor the same flag so the
user can actually open trades after overriding. Either skip gate 4 (daily loss) when the override is
active, or pass an `override_active: bool` into `check_risk_gates` and short-circuit gate 4 there.
The pure `check_risk_gates` stays pure — the caller reads `risk_override_until` and decides.

**Precise alternative (documented, not required):** instead of a blanket flag, snapshot
`daily_loss_baseline = get_daily_realized_pnl(uid)` at override time and change the gate to compare
`(daily_pnl - daily_loss_baseline)` against the limit, so **only losses booked *after* the override**
count. This re-arms protection intra-day (stricter) but adds a column + baseline-reset-at-midnight
logic. The flag approach is simpler and safer for the reported bug; ship the flag, keep this as a
future tightening.

### 17.8 Files touched — 17.B

| File | Change |
|---|---|
| `migrations/016_entry_bid.sql` | also add `users.risk_override_until timestamptz` (combined migration) |
| `core/db/queries.py` | **+** `set_risk_override_until(user_id, ts)`, `get_risk_override_until(user_id)`; export both |
| `core/db/__init__.py` | re-export the two helpers (import block **and** `__all__` — BP12-A guard) |
| `api/routers/telegram.py` | `unlock_drawdown`: set `risk_override_until = next UTC midnight`; message copy: "принято до 00:00 UTC" |
| `worker/tasks/manage_positions.py` | `_update_hwm_and_check_breakers`: top-of-function hard guard that returns while override is active (covers drawdown **and** daily-loss) |
| `worker/tasks/execute_copy.py` | pre-trade: honor override — skip gate-4 daily-loss block when `risk_override_until` is in the future |

### 17.9 Acceptance criteria — 17.B

1. User hits `paused_daily_loss`, taps "Снять блокировку": copying resumes and **stays** active — no
   re-lock alert on the next `sync_positions` cycle (or any cycle before 00:00 UTC), with no new
   losing trades.
2. `risk_override_until` is set to the next 00:00 UTC; after it passes, normal daily-loss/drawdown
   protection re-arms automatically without code intervention.
3. While the override is active, `check_risk_gates` does not block new entries on gate 4 (daily loss),
   but gates 1–3 (exposure/event/drawdown-from-new-baseline) still apply.
4. The same button correctly clears **both** `paused_drawdown` and `paused_daily_loss` (single handler,
   flag respected by both breaker branches).
5. `python -c "from core.db import set_risk_override_until, get_risk_override_until"` succeeds.

### 17.10 Evidence pass (run before coding)

- **17.A:** `grep "delta_drop_triggered" ~/.pm2/logs/*-out.log` — confirm `entry` vs `best_bid` gap is
  a spread, not a move (cross-check `position_mark` history for the same token; a bid that is hollow
  from t=0 = trap). Confirms Layer 2 is the right primary fix.
- **17.B:** for an affected user, verify `copy_paused_until` was cleared by the override yet a fresh
  `daily_loss_limit_tripped` log line appears seconds later on the next `sync_positions` — that pair is
  the re-lock signature.

### 17.11 Verification result — prod, 2026-07-01 ✅ BOTH HYPOTHESES CONFIRMED

Runtime is Docker (`app-worker-1` = celery worker running `sync_positions`; `app-api-1` = the
`unlock_drawdown` handler), **not** pm2. Evidence pulled with `docker logs app-worker-1` /
`app-api-1`. Note: structlog is emitted at Celery's `WARNING/MainProcess` wrapper level but the inner
`[info]` events (`position_mark`, `delta_drop_triggered`, …) are intact and parseable.

**17.A — every Delta-Drop stop was a single-cycle bid collapse, not a real move.** Three
`delta_drop_stop` closes in the window, **all losses**, total ≈ **−$8.52** on high-probability
(entry 0.88–0.93) positions:

| token | entry | best_bid before (steady, multiple polls) | best_bid @ trigger (one poll) | drop | booked P&L |
|---|---|---|---|---|---|
| `77344318364307` (trade 755) | 0.8899 | **0.88** for ≥6 min | **0.13** | 0.854 | **−$5.03** |
| `10205670933524` (trade 756) | 0.9299 | **0.89–0.90** | **0.65** | 0.301 *(barely over)* | **−$1.77** |
| `47685817448…` (trade 716)   | — | — | — | — | −$1.72 |

**Control case that proves the mechanism:** token `77893912283105` (entry 0.90) dipped to `0.71`
(drop `0.211`, *just under* the 0.30 threshold) at 16:08, was therefore **not** stopped, and then
**fully recovered to `0.999`** over the next ~25 min. The stopped tokens showed the *identical*
transient-collapse shape — their bid simply cratered for a **single 2-min poll**. This is a hollow
top-of-book / liquidity flush, not a fall in win probability.

Consequences for the design priority (reordered vs §17.2):
1. **Layer 4 (persistence/debounce) is now the PRIMARY fix.** Requiring the breach to persist across
   **≥2 consecutive polls** (`delta_drop_confirm_ticks=2`, ~4 min at the observed 2-min cadence)
   would have prevented **all three** false stops — the very next poll showed the bid restored to
   0.88–0.90.
2. **Layer 2 (spread veto) is co-primary.** At trigger, bid `0.13` against a ~`0.90` ask ⇒
   `spread_pct ≈ 1.5`; bid `0.65` vs ~`0.92` ask ⇒ `spread_pct ≈ 0.34`. Both are far above the
   proposed `0.08` cutoff and would be vetoed.
3. **Layer 1 (mid) alone is INSUFFICIENT** for the 0.13 flush if the ask held (`mid ≈ 0.51`,
   `drop ≈ 0.43` still fires). Keep mid, but it must be paired with Layers 2+4. `delta_drop_min_hold_sec`
   was irrelevant here (trades were 20–30 min old) — do not rely on it as the guard.

**17.B — re-lock loop reproduced verbatim.** From `app-worker-1` + `app-api-1`:

```text
17:00:30  daily_loss_limit_tripped  daily_pnl=-11.66  equity=114.75   ← first daily-loss pause
23:21:53  risk_override_manual      new_hwm=116.54 old_hwm=128.71     ← user taps "Снять блокировку"
23:22:30  daily_loss_limit_tripped  daily_pnl=-12.0   equity=116.54   ← RE-LOCK, +37 s
23:22:55  risk_override_manual      new_hwm=116.54 old_hwm=116.54     ← taps again (HWM no-op)
23:24:29  daily_loss_limit_tripped  daily_pnl=-12.0   equity=116.54   ← RE-LOCK again
```

`daily_pnl` is frozen at `-12.0` across both re-locks (no new trades) — a pure "counter/flag never
reset" defect, exactly §17.6. The override moved the HWM (fixing the *drawdown* breaker) but the
daily-loss gate reads `get_daily_realized_pnl` which is untouched, and `-12.0 < -(0.10 × 116.54) =
-11.65` re-trips every cycle. **§17.7's `risk_override_until` flag (respected by the daily-loss
branch) is confirmed as the correct fix.**

**Cross-defect finding (important):** the `-12.0` daily loss was **substantially manufactured by
17.A** — the three phantom spread-trap stops booked ≈ **−$8.52** of it. So **17.A is a root cause
feeding 17.B**: false stops crystallised losses → tripped the daily-loss breaker → the broken
override couldn't clear it → user locked out. Shipping 17.A materially reduces how often 17.B is even
reached; ship both, but 17.A is the higher-leverage fix.

---

## Blueprint 18 — Admin `/user` Dashboard Upgrade ✅ IMPLEMENTED 2026-07-03

> **Fix applied 2026-07-03.** V2 deposit-wallet tracking and paginated trade history shipped in
> commit `feat: upgrade admin /user command with V2 wallet tracking, PnL metrics, and paginated
> transaction history (Blueprint 18)`.
>
> **What was fixed:**
> - `cmd_user` now reads balances via `withdrawable_usdc(db_user)` (deposit-wallet pUSD + EOA USDC)
>   instead of the EOA directly — eliminates the "fake $0" balance display.
> - Open positions count pulled from `copy_trades` DB (`count_open_positions`) instead of the
>   on-chain EOA call that returned `0` for V2 users.
> - New `📈 PnL` block shows Daily / Weekly / All-Time realized PnL via `get_pnl_summary()`.
> - Inline button `📜 Последние 5 сделок` opens paginated settled-trade history (`uh:` callback).
> - `🔙 К пользователю` re-renders the dashboard via reusable `_user_view()` factory (`uc:` callback).
> - Divide-by-zero guard for legacy rows with `shares=NULL` (Blueprint 16 §16.7).
> - New helpers exported: `count_open_positions`, `get_pnl_summary`, `get_user_trade_history`
>   in `core/db/queries.py` and `core/db/__init__.py`.

> **Class of bug:** same *wrong-wallet / read-path* family as Blueprint 12-B and Blueprint 16 — the
> data exists, the admin view reads it from the wrong source. **No user money moves in this blueprint**
> (admin-bot, read-only display). But the numbers it shows drive support decisions, so "fake $0" is a
> trust/operational bug, not cosmetic.

### 18.0 Symptom (prod, admin bot `/user`)

`/user @sto1ner` renders a card where **every on-chain number is zero** even though the user has real
funds and open positions:

```text
Баланс: pUSD $0.00 · USDC.e $0.00 · POL 4.843
Открытых позиций: 0
```

(`POL 4.843` is correct because gas *does* sit on the EOA — which is the tell: only the EOA is being
read.) The user has collateral and live trades; the admin sees nothing.

### 18.1 Root-cause analysis — the admin reads the EOA, not the deposit wallet

`cmd_user` queries `users.wallet_address` (the **EOA**) for **both** balances and positions:

```316:324:api/routers/admin_bot.py
    addr = u.get("wallet_address")
    bal_txt, pos_txt = "—", "—"
    if addr:
        try:
            from core.polymarket import get_positions
            from core.polygon import get_balances
            b = get_balances(addr)
            bal_txt = f"pUSD ${b.get('pusd', 0):.2f} · USDC.e ${b.get('usdc_e', 0):.2f} · POL {b.get('matic', 0):.3f}"
            pos_txt = str(sum(1 for p in get_positions(addr) if p["shares"] > 0))
```

But per **§2.4**, in Polymarket V2 the user trades through a deterministic **deposit wallet**
(`users.deposit_wallet_address`, an ERC-1967 proxy). Collateral is **swept EOA → deposit wallet**, and
orders are POLY_1271-signed with the **deposit wallet as `funder`**. Therefore:

- **Trading pUSD collateral lives on the deposit wallet**, not the EOA → `get_balances(EOA).pusd == 0`.
- **On-chain positions are attributed to the deposit wallet** → `get_positions(EOA)` is empty → `0`.
- Only leftover gas (POL) and un-swept deposits remain on the EOA → that is why `POL` is the one
  non-zero field.

The main bot already knows this: `_trading_wallet()` returns `deposit_wallet_address or wallet_address`
(`api/routers/telegram.py` L835–838), and `withdrawable_usdc()` sums **deposit-wallet pUSD + EOA USDC
variants** (`core/polygon.py` L394–416, Blueprint 12-B). The admin bot was simply never migrated to the
V2 wallet model — it is still reading the pre-deposit-wallet EOA.

**Two independent defects, same cause:**
1. **Balance = fake $0** → must read `withdrawable_usdc(db_user)` (the established single source of
   truth), not `get_balances(EOA)`.
2. **Open positions = fake 0** → the on-chain call is against the wrong wallet *and* is an unnecessary
   Data-API round-trip. Count from **our own `copy_trades` ledger** instead — authoritative, cheap, and
   already the definition the copy-engine uses for the `max_open_positions` guard (§2.2).

### 18.2 Fix Data Fetching (kill the fake zeros)

**A. Balance — use `withdrawable_usdc` (single source of truth).**
Replace the EOA `get_balances` call with the same helper the withdrawal path uses, so the admin sees
exactly the number the user can act on. Keep `POL` (gas) as a separate on-chain read on the EOA (gas
genuinely lives there and admins need it to diagnose stuck withdrawals/redemptions).

```python
from core.polygon import withdrawable_usdc, get_balances

avail = withdrawable_usdc(u)                     # deposit pUSD + EOA (pusd+usdc_e+usdc)
eoa   = u.get("wallet_address")
pol   = get_balances(eoa).get("matic", 0.0) if eoa else 0.0
bal_txt = f"${avail:.2f} доступно · ⛽️ POL {pol:.3f}"
```

> Optional richer breakdown (nice-to-have, not required): show `trading pUSD` (deposit-wallet pUSD) and
> `free EOA USDC` separately by calling `get_balances(dw)` / `get_balances(eoa)` directly. But the
> **headline number MUST be `withdrawable_usdc`** so the admin card, the user's wallet screen, and the
> withdrawal "Доступно" all agree (the consistency goal from BP12-B §"Consistency cleanup").

**B. Open positions — count from `copy_trades`, not on-chain.**
Add a query helper that counts open (unresolved, unredeemed) rows — the same predicate as
`get_open_trades_cost` (queries.py L638) and `get_open_trade_by_token` (L534):

```python
def count_open_positions(user_id: int) -> int:
    """Count of the user's currently-open copy_trades (confirmed/executing, unredeemed).

    Authoritative, DB-sourced position count for the admin dashboard — mirrors the
    predicate the copy-engine uses for the max_open_positions guard (§2.2). Avoids the
    unreliable on-chain Data-API call against the EOA that returned a fake 0.
    """
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .in_("status", ["confirmed", "executing"])
        .is_("redeemed_at", "null")
        .execute()
    )
    return int(res.count or 0)
```

> **CRITICAL — export it** (BP12-A guard): add `count_open_positions` to **both** the
> `from core.db.queries import (...)` block **and** `__all__` in `core/db/__init__.py`. Same for every
> new helper below. This is the exact drift that caused the BP12 close-handler crash — do not repeat it.

> **Design note — display parity with the user's `/positions`.** The DB count is the source of truth
> for the *number*. If the admin later wants a per-position live view, source it from
> `_trading_wallet(u)` (deposit wallet) exactly like the user bot, **never** the EOA. For this blueprint
> the count is sufficient and avoids a network dependency in the admin card.

### 18.3 PnL Aggregation (Daily / Weekly / All-Time)

**Source of truth:** `copy_trades.realized_pnl`, booked on terminal transition — resolution redeem
(Blueprint 1) or token-sale close (Blueprint 6, `mark_trade_closed` L557). A row counts toward realized
PnL iff `realized_pnl IS NOT NULL AND resolved_at IS NOT NULL` (identical to the existing
`get_daily_realized_pnl`, L515, so the daily-loss breaker and the admin card stay consistent).

**Window semantics (UTC):**
- 📅 **Daily** — settled since **00:00 UTC today** (calendar day; intuitive "today" for an admin).
- 🗓 **Weekly** — settled in the **trailing 7×24 h**.
- 🏆 **All-Time** — every settled row for the user.

> Note the deliberate difference from `get_daily_realized_pnl` (which is a **trailing 24 h** window used
> by the risk breaker). For an *admin display* "PnL за сегодня" reads more naturally as the calendar day.
> If you prefer strict reuse, call `get_daily_realized_pnl(uid)` for the Daily figure and document it as
> trailing-24h — but do **not** silently mix the two definitions.

**Canonical SQL (one round-trip, `FILTER` aggregates):**

```sql
SELECT
  COALESCE(SUM(realized_pnl) FILTER (
      WHERE resolved_at >= date_trunc('day', now() AT TIME ZONE 'utc')), 0) AS pnl_today,
  COALESCE(SUM(realized_pnl) FILTER (
      WHERE resolved_at >= (now() AT TIME ZONE 'utc') - interval '7 days'), 0) AS pnl_week,
  COALESCE(SUM(realized_pnl), 0)                                             AS pnl_all,
  COUNT(*)                                                                   AS settled_trades
FROM copy_trades
WHERE user_id = :uid
  AND realized_pnl IS NOT NULL
  AND resolved_at  IS NOT NULL;
```

**Runtime implementation (supabase client — the actual runtime path, §2.1).** The supabase client does
not do `FILTER` aggregates, so fetch the settled rows once and bucket in Python (one query, small
result set — a user has tens–hundreds of settled trades, not millions):

```python
def get_pnl_summary(user_id: int) -> dict:
    """Realized-PnL rollup for the admin dashboard: today (UTC calendar day),
    trailing 7 days, and all-time — plus the settled-trade count.

    Uses the same 'realized_pnl IS NOT NULL AND resolved_at IS NOT NULL' terminal
    predicate as get_daily_realized_pnl so the risk breaker and the admin card never
    disagree on what 'settled' means.
    """
    from datetime import datetime, timedelta, timezone
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("realized_pnl, resolved_at")
        .eq("user_id", user_id)
        .not_.is_("realized_pnl", "null")
        .not_.is_("resolved_at", "null")
        .execute()
    )
    now = datetime.now(timezone.utc)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week0 = now - timedelta(days=7)
    today = week = all_time = 0.0
    n = 0
    for r in (res.data or []):
        pnl = float(r.get("realized_pnl") or 0)
        ts = _parse_ts(r.get("resolved_at"))        # tz-aware UTC parse helper
        all_time += pnl
        n += 1
        if ts and ts >= day0:
            today += pnl
        if ts and ts >= week0:
            week += pnl
    return {"today": today, "week": week, "all_time": all_time, "settled": n}
```

> **Optional performance migration (only if needed):** a partial index
> `create index if not exists ix_copy_trades_user_resolved on copy_trades (user_id, resolved_at) where resolved_at is not null;`
> (→ `migrations/017_copy_trades_resolved_idx.sql`). Not required at current row counts; add it if the
> admin bot ever scans large users. **No schema/column change is needed for Blueprint 18** — every field
> already exists (migration 008).

> **Unrealized PnL (open positions) — explicitly out of scope for the card headline.** The three PnL
> metrics above are **realized** only. Live/unrealized PnL of open positions is already visible in the
> user bot's `/positions` (Blueprint 16) and depends on live marks; if an admin wants it later, compute
> it from `_trading_wallet(u)` positions × current price, but keep it off the primary card to avoid a
> network call and a flickering number.

### 18.4 UI Redesign — `/user` card + inline history

**A. Refactor `cmd_user` into a reusable view factory.** The card must be re-renderable from a callback
(the "back" button from the history view returns to it), so extract the body into:

```python
def _user_view(tid: int, u: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Build the admin /user dashboard text + inline keyboard. Pure (no I/O beyond
    the balance/PnL/count reads) so both cmd_user and the callback can call it."""
```

`cmd_user` then does `_resolve(arg)` → `_user_view(tid, u)` → `reply_text(text, reply_markup=kb)`.

**B. New message template** (adds the PnL block; fixes the balance/position lines):

```text
👤 Пользователь

Ник: @sto1ner
ID: 891787021
Подписка: 4 дн (до 2026-07-07)
Копирование: 🟢 вкл
Макс. позиция: $10
Кошелёк зарегистрирован: ✓

💰 Баланс: $42.18 доступно · ⛽️ POL 4.843
📊 Открытых позиций: 3

📈 PnL
📅 Сегодня:   +$3.10
🗓 7 дней:    −$1.45
🏆 За всё:    +$28.90   (147 сделок)

0xa9108131f342e3C749CBbB6fAc4fB609BF831D20
```

Formatting rules: sign every PnL with `+`/`−` and a red/green cue if desired
(`🟢`/`🔴`), `${abs:.2f}`; render `$0.00` when settled==0 (never `—` for PnL — zero is a real answer).
Keep the wallet address on its own line as `<code>` (tap-to-copy) as today.

**C. Inline keyboard under the card:**

```python
kb = InlineKeyboardMarkup([
    [InlineKeyboardButton("📜 Последние 5 сделок", callback_data=f"uh:{tid}:0")],
])
```

`callback_data` is `uh:<telegram_id>:<offset>` — well under Telegram's 64-byte cap (e.g.
`uh:891787021:0`). We encode the target user in the callback because callbacks are stateless (the admin
who tapped ≠ the subject user). Guard: only `is_admin(tg.id)` may use it (the existing `on_callback`
gate already enforces this — L555–558).

### 18.5 Trade-history view (`📜 Последние 5 сделок` + pagination)

**A. New query helper** — most-recent terminal trades, paginated, with the market title joined from
`trade_signals` (title lives on the signal, not on `copy_trades` — see execute_copy L464–466):

```python
def get_user_trade_history(user_id: int, limit: int = 5, offset: int = 0) -> list[dict]:
    """Most-recent settled/closed copy_trades for the admin history view, newest first.

    Embeds the source signal's title/outcome/event via the signal_id FK so the row can
    show a human ticker. Falls back gracefully if the embed is unavailable.
    """
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select(
            "id, entry_price, shares, size_usdc, result, realized_pnl, "
            "outcome_index, resolved_at, created_at, status, "
            "trade_signals(title, outcome, event_slug)"          # FK embed
        )
        .eq("user_id", user_id)
        .not_.is_("resolved_at", "null")                          # terminal only
        .order("resolved_at", desc=True)
        .range(offset, offset + limit - 1)                        # supabase pagination
        .execute()
    )
    return res.data or []
```

> If the `trade_signals(...)` FK embed is not wired in the Supabase schema, fall back to a second
> batched lookup: collect `signal_id`s and `select id, title, outcome from trade_signals in (...)`, then
> map. Keep the embed as the primary path (one round-trip).

**B. Row rendering — Тикер · Исход · Вход · Выход · PnL.** There is **no `exit_price` column** on
`copy_trades` (verified in §6.4). Derive the exit for display:

- `result='win'` → resolution paid **1.00**; `result='lose'`/`'loss'` → **0.00** (binary payout, §1).
- `result='closed'` (token-sale exit, Blueprint 6) → no stored sale price; derive
  `exit ≈ entry_price + realized_pnl / shares` when `shares > 0`, else show `—`. **Guard the divide by
  zero** (legacy rows have `shares=NULL`, see BP16 §16.7).
- Outcome label: `"YES" if outcome_index == 0 else "NO"` (fallback to the embedded
  `trade_signals.outcome` string when present).

```python
def _fmt_history_row(t: dict) -> str:
    sig   = t.get("trade_signals") or {}
    title = (sig.get("title") or "—")[:32]
    oc    = sig.get("outcome") or ("YES" if t.get("outcome_index") == 0 else "NO")
    entry = float(t.get("entry_price") or 0)
    shares = float(t.get("shares") or 0)
    pnl   = float(t.get("realized_pnl") or 0)
    result = (t.get("result") or "").lower()
    if result == "win":
        exit_px = 1.0
    elif result in ("lose", "loss"):
        exit_px = 0.0
    elif shares > 0:
        exit_px = entry + pnl / shares
    else:
        exit_px = None
    icon    = "🟢" if pnl >= 0 else "🔴"
    exit_s  = f"{exit_px:.2f}" if exit_px is not None else "—"
    entry_s = f"{entry:.2f}" if entry > 0 else "—"
    return (f"{icon} <b>{title}</b> · {oc}\n"
            f"   вход {entry_s} → выход {exit_s} · PnL {pnl:+.2f}$")
```

**C. History message + pagination keyboard** (callback `uh:<tid>:<offset>`, page size 5):

```text
📜 История сделок · @sto1ner
(показаны 1–5 из последних)

🟢 Trump wins Iowa · YES
   вход 0.88 → выход 1.00 · PnL +2.40$
🔴 Fed cuts in July · NO
   вход 0.62 → выход 0.00 · PnL −3.10$
...
```

```python
rows = []
nav = []
if offset > 0:
    nav.append(InlineKeyboardButton("◀️", callback_data=f"uh:{tid}:{max(0, offset - 5)}"))
if len(trades) == 5:                       # a full page ⇒ there may be more
    nav.append(InlineKeyboardButton("▶️", callback_data=f"uh:{tid}:{offset + 5}"))
if nav:
    rows.append(nav)
rows.append([InlineKeyboardButton("🔙 К пользователю", callback_data=f"uc:{tid}")])
kb = InlineKeyboardMarkup(rows)
```

**D. Callback wiring** — add two branches to `on_callback` (`admin_bot.py` L559+), mirroring the existing
`tp:`/`mp:` dispatch style, and re-using the message-edit + "not modified" swallow already there:

```python
elif data.startswith("uh:"):
    _, tid_s, off_s = data.split(":", 2)
    text, kb = _history_view(int(tid_s), int(off_s))
elif data.startswith("uc:"):
    tid = int(data[3:])
    u = get_user_by_telegram_id(tid)
    text, kb = _user_view(tid, u) if u else ("Пользователь не найден.", None)
```

`_history_view(tid, offset)` resolves the user (`get_user_by_telegram_id`), calls
`get_user_trade_history(u["id"], 5, offset)`, and renders §18.5-B/C. Empty result → "Нет завершённых
сделок." with only the 🔙 back button.

### 18.6 Files touched (no migration required)

| File | Change |
|---|---|
| `api/routers/admin_bot.py` | Refactor `cmd_user` → `_user_view(tid, u)`; **balance** via `withdrawable_usdc` + EOA `POL` (§18.2-A); **positions** via `count_open_positions` (§18.2-B); add **PnL block** via `get_pnl_summary` (§18.3); attach inline keyboard (§18.4-C); add `_history_view` + `uh:`/`uc:` branches in `on_callback` (§18.5) |
| `core/db/queries.py` | **+** `count_open_positions(user_id)`, `get_pnl_summary(user_id)`, `get_user_trade_history(user_id, limit, offset)` (+ a `_parse_ts` helper if none exists) |
| `core/db/__init__.py` | **+** re-export all three helpers in the import block **and** `__all__` (BP12-A guard) |
| `migrations/017_copy_trades_resolved_idx.sql` | *(optional, perf-only)* partial index `(user_id, resolved_at)` — §18.3 |

No column is added; every field (`realized_pnl`, `resolved_at`, `entry_price`, `shares`, `result`,
`outcome_index`) already exists from migration 008.

### 18.7 Acceptance criteria

1. For a user with $42.18 withdrawable and 3 open trades, `/user` shows **`💰 Баланс: $42.18 доступно`**
   and **`📊 Открытых позиций: 3`** — never `$0.00`/`0` when funds/positions exist. The headline balance
   equals the number shown on the user's own wallet/withdrawal screen (`withdrawable_usdc` parity).
2. `POL` gas continues to display from the EOA (it is not part of `withdrawable_usdc`).
3. The card shows Daily / Weekly / All-Time realized PnL, correctly signed, with `$0.00` (not `—`) when a
   window has no settled trades. All-Time equals the sum of every settled `realized_pnl` for the user.
4. `📜 Последние 5 сделок` opens a list of the 5 most-recent **settled** trades with Ticker, Outcome,
   Entry, derived Exit, and PnL; `▶️`/`◀️` paginate in steps of 5; `🔙 К пользователю` re-renders the card.
5. A `closed` (token-sale) trade with `shares=0`/NULL renders Exit `—` and never raises
   `ZeroDivisionError` (BP16 §16.7 legacy-row guard).
6. `python -c "from core.db import count_open_positions, get_pnl_summary, get_user_trade_history"`
   succeeds (exports wired — BP12-A guard). If the boot self-check name-set exists (`api/main.py` /
   `worker/celery_app.py`), add the three names there too.
7. Only admins can trigger the `uh:`/`uc:` callbacks (existing `on_callback` `is_admin` gate).
8. No new DB column; `/user` makes **at most** the balance reads + 2 small DB queries (count + PnL) and
   **zero** on-chain position calls.

### 18.8 Evidence pass (run before coding — confirm the wrong-wallet hypothesis)

```bash
# For the affected user (telegram_id 891787021 from the screenshot): compare EOA vs deposit wallet.
docker compose exec -T worker python - <<'PY'
from core.db.session import get_supabase
from core.polygon import get_balances, withdrawable_usdc
sb = get_supabase()
u = sb.table("users").select("*").eq("telegram_id", 891787021).maybe_single().execute().data
print("EOA        :", u.get("wallet_address"),        get_balances(u.get("wallet_address")))
print("DepositWlt :", u.get("deposit_wallet_address"), get_balances(u.get("deposit_wallet_address")))
print("withdrawable_usdc:", withdrawable_usdc(u))
open_ct = (sb.table("copy_trades").select("id", count="exact")
           .eq("user_id", u["id"]).in_("status", ["confirmed","executing"])
           .is_("redeemed_at","null").execute().count)
print("open copy_trades :", open_ct)
PY
```

**Expected:** EOA pUSD/USDC ≈ 0 (only POL), deposit-wallet pUSD > 0, `withdrawable_usdc` > 0, and
`open copy_trades` > 0 — confirming §18.1 (the card reads the EOA; the money and positions are on the
deposit wallet / in the ledger). Fix = §18.2 + §18.3 + §18.5; no migration.

---

## Blueprint 19 — Global Stop-Loss Invariant & Fixed-Sizing Leak ✅ IMPLEMENTED (2026-07-03)

> **Class of bug:** a **risk-path** instance of the exact **Blueprint 16 wrong-source** defect. The
> stop-loss monitor reads its cost basis from the Polymarket **Data-API `avg_price`** — the same field
> BP16 proved returns **`0`** for our custodial POLY_1271 proxy wallets. When it is `0`, a `> 0` guard
> **silently skips the entire Delta-Drop stop**, so the position rides to resolution. BP16 fixed this for
> the *display* path but the fix was **never wired into the money-losing risk path.** This is a
> **stop-loss leak that costs real money**, not a cosmetic bug — §5 fail-closed applies.

### 19.0 Symptom (prod, user @sto1ner / id 891787021)

Trade-history rows where the **−30% Delta-Drop stop (Blueprint 17) never fired**:

| Entry | Exit | Drawdown | Should have stopped at |
|---|---|---|---|
| 0.54 | 0.17 | ~68% | ~0.38 (−30%) |
| 0.66 | 0.00 | 100% | ~0.46 (−30%) |
| 0.88 | 0.55 | ~37% | ~0.62 (−30%) |

The admin's own account (`SIZING_MODE=kelly`) stops correctly; this user is on `SIZING_MODE=fixed`,
which produced the **plausible-but-wrong** hypothesis that the monitor filters by sizing mode.

### 19.1 Root-cause analysis — TWO findings

**Finding A — the sizing-mode hypothesis is REFUTED (spurious correlation).**
There is **no `sizing_mode` branch anywhere in the monitor path**:
- `get_active_subscribers()` (`core/db/queries.py` L54–86) filters on `sub_tier`, `sub_expires_at`,
  `copy_paused_until`, and (auto-copy) `copy_active`/`wallet_address` — **never** `sizing_mode`.
- `sync_positions` iterates every returned subscriber identically and the stop block is explicitly
  annotated as sizing-agnostic:

```150:150:worker/tasks/manage_positions.py
            # BP13.3 invariant: stop is sizing-agnostic; never branch on sizing_mode.
```

`sizing_mode` (per-user, migration 011) affects **entry sizing only** (`execute_copy`, §2.3 / BP13.1);
it is invisible to `sync_positions`. Kelly vs Fixed working differently is a **coincidence of indexer
timing** (see Finding B) — not causation.

**Finding B — the real leak: stop-loss uses the Data-API `avg_price` (BP16's known-zero source), and a `> 0` guard skips silently.**
The monitor takes its cost basis straight from the on-chain Data-API position:

```198:199:worker/tasks/manage_positions.py
            # Cost-basis entry price from Data-API avg_price.
            entry_px = float(p.get("avg_price") or 0)
```

The **entire** Delta-Drop evaluation is then gated on that value being non-zero:

```234:235:worker/tasks/manage_positions.py
            if entry_px > 0 and price_ref > 0:
                drop_pct = 1.0 - price_ref / entry_px
```

Per **Blueprint 16 §16.1 (verified in §16.7)**, `avgPrice` from the Data API is **`0` for freshly-opened
positions on POLY_1271 proxy wallets** (indexing lag + proxy attribution). When `entry_px == 0`:
- `if entry_px > 0` is **False** → Delta-Drop is **skipped every poll**, with **no log line** (this is
  the silent leak — `position_mark` at L208 is *also* gated on `entry_px > 0`, so there is literally no
  trace the position was ever evaluated).
- The **only** remaining net is the absolute `hard_stop`, which itself has a guard that leaks:

```280:283:worker/tasks/manage_positions.py
            if best_bid > 0 and best_bid < settings.hard_stop_abs_price:
                log.info("hard_stop_triggered",
                         user_id=uid, token=token_id[:14],
                         cur_price=best_bid, threshold=settings.hard_stop_abs_price)
```

This explains the table exactly:
- **0.54 → 0.17** and **0.88 → 0.55**: mid stayed **above** `hard_stop_abs_price` (0.07), and Delta-Drop
  was dead (`entry_px=0`) → **nothing fired**; the loss only crystallised at resolution/manual close.
- **0.66 → 0.00**: rode all the way down; once the book went **hollow (`best_bid == 0`)**, even
  `hard_stop` is vetoed by its own `best_bid > 0` guard → **no exit possible** → settled at $0.

**Why the admin (Kelly) was protected and this user (Fixed) was not — the real differentiator.** It is
*not* the sizing mode. It is **whether the Data-API had back-filled `avgPrice` by the time the monitor
polled** — a function of indexing lag, market liquidity, and fill timing. The admin's positions happened
to have a non-zero `avgPrice` (guard passed → stop worked); this user's were still `0` at evaluation
time (guard failed → stop skipped). The BP16 evidence pass already proved **18/25** open positions carry
a good `entry_price` in our DB while the on-chain `avgPrice` reads `0` — i.e. **the correct cost basis
was sitting in `copy_trades` the whole time; the monitor just never looked at it.**

**Finding C — the fix ingredient already exists but is unused on the risk path.**
BP16 shipped `get_entry_prices_by_token(user_id)` (`core/db/queries.py` L663–691) and the `/positions`
view uses it (`_db_entry_prices`). `manage_positions.py` does **not** import or use it — the only
`entry_price`/`avg_price` reference in the whole monitor is the raw L199 read above.

### 19.2 Design — stop-loss as a GLOBAL, data-resilient invariant

The stop must be **(1)** independent of sizing mode (already structurally true — keep it that way, never
add a `sizing_mode` branch), and **(2)** resilient to a missing/zero on-chain cost basis via a
deterministic fallback chain, and **(3)** never able to fail *silently* again.

**Fix 1 — Effective-entry resolver with a fallback chain (the core fix).**
Introduce a single cost-basis resolver used by the stop, mirroring BP16 but **DB-first** (the risk path
should trust *our own recorded fill*, not an indexer that is provably zero/late):

```text
effective_entry(uid, token, api_avg_price):
    1. DB copy_trades.entry_price        (get_entry_prices_by_token — our true fill cost)   ← preferred
    2. Data-API avg_price                (only if DB is 0/NULL and api_avg_price > 0)
    3. size_usdc / shares                (derive from the ledger row when both are present)
    4. trade_signals.price               (via signal_id — the VWAP entry the signal carried)
    5. None                              → cannot compute Delta-Drop; log LOUD + rely on Fix 3 floor
```

> **Precedence rationale (differs from BP16 on purpose).** BP16 chose *API-if-nonzero, else DB* for a
> *display* because the API blends partial fills. For a **stop-loss** we want the deterministic,
> tamper-proof number we actually paid — our `entry_price` — so the trigger can never be moved by
> indexer noise. Both are near-identical in practice; DB-first is the safer risk default.

Batch it once per user per cycle (one query, same shape as BP16): call
`get_entry_prices_by_token(uid)` at the top of the per-user loop and look up `token_id` inside the
position loop — do **not** query per position.

**Fix 2 — Never skip silently: LOUD logging on missing cost basis.**
The defining property of this incident is that the leak left **no trace**. Add an explicit branch: when
`effective_entry` is `None`, emit `log.warning("stop_no_cost_basis", user_id, token, api_avg=…, cur=…)`
every poll (throttle via `notify_once`/Redis if noisy). A skipped stop must be **observable** so the next
leak is caught in minutes, not weeks. Also drop the `entry_px > 0` gate on `position_mark` so a position
is marked (with `entry=None`/`source=…`) even when the API avg is 0.

**Fix 3 — Cost-basis-independent catastrophic floor (defense-in-depth).**
`hard_stop` is the only sizing- and entry-independent net; keep it, and harden its two leaks:
- It fires on `best_bid < hard_stop_abs_price` **only while `best_bid > 0`**. That guard is *correct*
  (you cannot market-sell into an empty book), but the **hollow-book → rides-to-$0** case must be made
  **visible**: when `best_bid == 0` on an aged position, log `stop_unsellable_hollow_book` so ops can see
  a position that is un-exitable (its loss is already locked; nothing to do but wait for resolution).
- Consider a second absolute floor keyed on the **mid** (`price_ref < hard_stop_abs_price`) so a
  collapsing-but-not-yet-hollow book is caught even if the exact `best_bid` guard is borderline.

**Fix 4 — Keep the BP17 hardening intact.** All four BP17 layers (mid, spread veto, entry-bid,
persistence/debounce) sit *inside* the `if entry_px > 0` block, so today they never even run when
`entry_px=0`. Once Fix 1 supplies a real entry, BP17 resumes protecting against the spread-trap
false-positive. The two blueprints compose: **BP17 stops false stops; BP19 stops missed stops.** Layer 3
(`entry_bid`) already degrades gracefully to `layer3_ok=True` when `entry_bid` is NULL (L240–248) — no
change needed there.

### 19.3 Config knobs (add to `core/config.py`)

| Setting | Proposed default | Meaning |
|---|---|---|
| `stop_use_db_entry` | `True` | resolve cost basis DB-first (Fix 1); `False` = legacy API-only |
| `stop_mid_floor_enabled` | `True` | also fire `hard_stop` when the **mid** < `hard_stop_abs_price` (Fix 3) |
| `stop_no_cost_basis_alert` | `True` | emit `stop_no_cost_basis` warnings when entry is unresolved |

No new *threshold* is introduced — `delta_drop_stop_pct` (0.30) and `hard_stop_abs_price` (0.07) are
unchanged. The fix is about **feeding the existing math a correct, always-present input.**

### 19.4 Files touched

| File | Change |
|---|---|
| `worker/tasks/manage_positions.py` | Replace the L199 `avg_price`-only read with the `effective_entry` resolver (Fix 1); fetch `get_entry_prices_by_token(uid)` once per user; add `stop_no_cost_basis` + `stop_unsellable_hollow_book` logs (Fix 2/3); ungate `position_mark`; optional mid-floor hard_stop (Fix 3) |
| `core/db/queries.py` | `get_open_trade_by_token` (or a small new helper) to also return `size_usdc`, `shares`, and the signal's `price` for fallback tiers 3–4; **or** a dedicated `get_cost_basis(uid, token)` that returns the resolved entry with a `source` label |
| `core/db/__init__.py` | re-export any new helper (import block **and** `__all__` — BP12-A guard) |
| `core/config.py` | add the three knobs in §19.3 |

**No migration required** — `entry_price` (008), `shares`/`size_usdc` (008), `signal_id`→`trade_signals.price`
all already exist. This is a read-path rewire, not a schema change.

### 19.5 Acceptance criteria

1. A position bought at `0.54` whose **DB `entry_price` is 0.54** but whose Data-API `avg_price` is `0`
   **is stopped** at ~`0.38` (−30%, subject to BP17 spread/persistence layers) — the stop no longer
   depends on the on-chain avg being populated.
2. When cost basis is unresolvable from **all** tiers, the monitor emits `stop_no_cost_basis` **every
   evaluation** (throttled) — a skipped stop is never silent again.
3. A hollow-book (`best_bid == 0`) aged position logs `stop_unsellable_hollow_book` (visibility), and
   `hard_stop` still fires the instant a bid ≥ tick reappears below `0.07`.
4. **No code path branches on `sizing_mode`** in `sync_positions`/`close_position`; Kelly and Fixed users
   are byte-for-byte identical through the stop logic (grep proof in acceptance).
5. BP17's spread veto + persistence still suppress the false-positive spread-trap once a real entry is
   supplied (BP19 does not reintroduce BP17's bug).
6. `python -c "from core.db import get_entry_prices_by_token"` (and any new helper) succeeds.

### 19.6 Evidence pass — bash/SQL to prove the diagnosis on prod

> **Runtime is Docker Compose** (`app-worker-1` runs `sync_positions`; `app-api-1` the bots) — **not
> pm2** (confirmed in BP17 §17.11). Use `docker compose logs`. A pm2 fallback is included last in case a
> host still runs the legacy process manager. Structlog events are emitted under Celery's
> `WARNING/MainProcess` wrapper but the inner `[info]` events (`position_mark`, `delta_drop_triggered`,
> `hard_stop_triggered`, …) are intact and greppable.

**Step 0 — resolve the internal `user_id`, the user's `sizing_mode`, and the offending ledger rows.**
This alone proves Finding B if `entry_price` is populated in the DB while the trade shows a big loss with
no stop.

```bash
docker compose exec -T worker python - <<'PY'
from core.db.session import get_supabase
sb = get_supabase()
u = (sb.table("users")
     .select("id,telegram_id,username,sizing_mode,wallet_address,deposit_wallet_address")
     .eq("telegram_id", 891787021).maybe_single().execute().data)
print("USER:", u)
uid = u["id"]
rows = (sb.table("copy_trades")
        .select("id,token_id,entry_price,entry_bid,shares,size_usdc,status,result,"
                "realized_pnl,resolved_at,created_at,signal_id")
        .eq("user_id", uid).order("created_at", desc=True).limit(80).execute().data or [])
print(f"\nsizing_mode={u.get('sizing_mode')}  |  {len(rows)} recent trades for uid={uid}:")
for r in rows:
    print(f"  id={r['id']:>5} tok={str(r['token_id'])[:14]:<14} "
          f"entry={str(r.get('entry_price')):<7} shares={str(r.get('shares')):<7} "
          f"size=${str(r.get('size_usdc')):<6} status={r['status']:<9} "
          f"result={str(r.get('result')):<7} pnl={str(r.get('realized_pnl')):<8} "
          f"resolved={str(r.get('resolved_at'))[:10]}")
PY
```

*Read it:* if the big-loss rows (entry ≈ 0.54 / 0.66 / 0.88) have **`entry_price` populated** in the DB,
the cost basis was never lost — the monitor simply didn't use it. Note the `token_id` (first 14 chars)
of each offender for Steps 1–2. Confirm `sizing_mode` is `fixed` (context only — not causal).

**Step 1 — did the monitor EVER evaluate these tokens? (the smoking gun).**
`position_mark` and every Delta-Drop log are gated on `entry_px > 0`. **Absence** of any monitor line for
a token whose DB `entry_price` is populated = the silent-skip leak (Finding B).

```bash
# Substitute the 14-char token prefixes from Step 0.
for TOK in <tok14_a> <tok14_b> <tok14_c>; do
  echo "===== token $TOK ====="
  docker compose logs --since 720h worker 2>&1 | grep -F "$TOK" \
    | grep -E "position_mark|delta_drop_confirming|delta_drop_triggered|stop_skipped_wide_spread|hard_stop_triggered" \
    || echo "  NO monitor lines for this token — stop path never ran (entry_px was 0)."
done
```

**Step 2 — any stop/close activity at all for this user? (compare against the admin).**
Use the internal `uid` from Step 0 (logs key on `user_id=<uid>`, not the telegram id).

```bash
UID=<uid_from_step0>
docker compose logs --since 720h worker 2>&1 \
  | grep -E "user_id=$UID\b|user_id=$UID'" \
  | grep -E "delta_drop_triggered|hard_stop_triggered|delta_drop_confirming|stop_skipped_wide_spread|close_position" \
  | tail -60
# Contrast with the admin's uid (Kelly, stops working) to show the difference is
# presence/absence of a usable entry, not the sizing mode:
ADMIN_UID=<admin_uid>
docker compose logs --since 720h worker 2>&1 | grep -E "user_id=$ADMIN_UID\b" \
  | grep -E "position_mark|delta_drop_triggered" | tail -30
```

**Step 3 — LIVE proof of the `avg_price==0` vs `entry_price>0` divergence** (run on any *still-open*
position of the user; this is the single most direct confirmation).

```bash
docker compose exec -T worker python - <<'PY'
from core.db.session import get_supabase
from core.db import get_entry_prices_by_token
from core.polymarket import get_positions
sb = get_supabase()
u = (sb.table("users").select("id,deposit_wallet_address")
     .eq("telegram_id", 891787021).maybe_single().execute().data)
uid, dw = u["id"], u["deposit_wallet_address"]
db_entry = get_entry_prices_by_token(uid)
print("DB entry_price by token:", {k[:14]: v for k, v in db_entry.items()})
for p in get_positions(dw):
    if p["shares"] <= 0:
        continue
    tok = p["token_id"]; api = float(p.get("avg_price") or 0)
    print(f"tok={tok[:14]} API_avg={api:<7} cur={p.get('cur_price')} "
          f"DB_entry={db_entry.get(tok)}  ->  stop entry_px={api} "
          f"{'(ZERO => Delta-Drop SKIPPED)' if api == 0 else ''}")
PY
```

*Expected:* one or more rows with `API_avg=0` while `DB_entry` is a real price → the guard at
`manage_positions.py` L234 was False → Delta-Drop skipped. This is Finding B, live.

**Step 4 (optional) — SQL variant via `psql`** (if a Postgres connection string is available; never
commit it):

```bash
export PGURL='postgresql://postgres.<ref>:<PWD>@aws-0-<region>.pooler.supabase.com:5432/postgres'
psql "$PGURL" -c "
  select ct.id, left(ct.token_id,14) as token, ct.entry_price, ct.shares, ct.size_usdc,
         ct.status, ct.result, ct.realized_pnl, ct.resolved_at, ts.price as signal_price
  from copy_trades ct
  join users u on u.id = ct.user_id
  left join trade_signals ts on ts.id = ct.signal_id
  where u.telegram_id = 891787021
  order by ct.created_at desc
  limit 80;
"
```

**pm2 fallback (legacy hosts only):**

```bash
for TOK in <tok14_a> <tok14_b> <tok14_c>; do
  grep -F "$TOK" ~/.pm2/logs/worker-out.log \
    | grep -E "position_mark|delta_drop|hard_stop" || echo "no monitor lines for $TOK";
done
```

**How this confirms Blueprint 19:** Step 0 shows the DB held a valid `entry_price`; Steps 1–2 show the
monitor produced **zero** stop/mark lines for those tokens (silent skip) while the admin's tokens *were*
marked; Step 3 shows *live* that `avg_price=0` while `entry_price>0` for this custodial wallet. Together
they prove the leak is the **Data-API cost-basis source (Finding B)**, independent of `sizing_mode`
(Finding A) — exactly the fix in §19.2.

---

## Blueprint 20 — Redeem-Hang Self-Healing + Win-Notification UX (outcome/title/Net-PnL) ✅ IMPLEMENTED 2026-07-03

> Four prod defects captured 2026-07-03. **20.A is money-critical** (real winnings stuck on-chain,
> unclaimed for up to 7 days); 20.B/C/D are win-notification UX. BP9's money-safety invariant worked
> (no fake credit), but the claim pipeline **stalls and never self-heals** — §5 fail-closed +
> idempotency apply to the fixes.

### 20.0 Symptoms (prod, PolyMind AI)

1. **Claim hang.** "⏳ Выигрыш определён, зачисление задерживается" (`_emit_win_retry_failed`) stays
   forever; winnings are never credited.
2. **Missing outcome.** Notifications show `🎯 Исход: —` (empty on markets like "Any Other Score").
3. **Truncated titles.** Event names cut mid-word in lists/alerts.
4. **No Net PnL.** The win notice shows only the credited amount, not profit (payout − entry cost).

### 20.1 Root-cause analysis — 20.A (the redeem hang) — CONFIRMED on prod

The claim is **not** failing on-chain (no gas/revert/RPC). A 48h grep for
`redeem_failed|did not confirm|Traceback|retry` returned **nothing**. The redeem simply **never
re-runs**, for two compounding reasons:

**A1 — Stale 7-day dedup key, never cleared on failure/skip.** `notify_once` defaults to a **7-day** TTL:

```29:35:core/cache.py
def notify_once(key: str, ttl: int = 7 * 86400) -> bool:
    """Return True only the first time `key` is seen (Redis SETNX with TTL)."""
    try:
        return bool(_client().set(f"once:{key}", "1", nx=True, ex=ttl))
    except Exception:
        # Fail-open: a dead Redis means the worker is degraded anyway.
        return True
```

Both redeem paths set `once:redeem:{uid}:{cond}` at **dispatch time** and gate re-dispatch on it:

```965:966:worker/tasks/manage_positions.py
                if not notify_once(f"redeem:{uid}:{cond}"):
                    continue  # already dispatched
```

```110:110:worker/tasks/manage_positions.py
                    redeem_claimed = _notify_once(f"redeem:{uid}:{condition_id}")
```

The key is set once and **never deleted when the redeem fails, is skipped, or the tokens are still
held**. So after the *first* attempt, `reconcile_settlements` L965 hits `notify_once(...) == False` →
**silent `continue`** (no log, `processed` never increments) for the full 7 days. This is exactly the
observed `checked=67 processed=0` on every cycle with zero redeem/win/loss log lines.

**A2 — Redeem dispatched WITHOUT `trade_id`, so the ledger can never be marked settled.** The
`sync_positions` redeemable branch omits `trade_id`/`entry_cost`:

```120:126:worker/tasks/manage_positions.py
                    if redeem_claimed:
                        redeem_position.delay(
                            uid, token_id, condition_id,
                            bool(p.get("neg_risk", False)), p.get("outcome"),
                            p.get("title"), p.get("event_slug"),
                            p.get("outcome_index"),
                        )
```

But `mark_trade_settled` inside `redeem_position` is guarded by `if trade_id:`:

```761:769:worker/tasks/manage_positions.py
        if trade_id:
            try:
                from core.db import mark_trade_settled
                gross = shares_bal if shares_bal > 0 else credited
                pnl = gross - float(entry_cost or 0)
                mark_trade_settled(trade_id, result="win",
                                   realized_pnl=pnl, redeem_tx=redeem_tx)
            except Exception:
                log.warning("mark_trade_settled_failed", trade_id=trade_id)
```

So when the redeem is dispatched from `sync_positions`, even a **successful** on-chain redeem (tokens
burned) leaves `copy_trades` at `status=confirmed, redeemed_at=null` → the trade stays "outstanding"
forever, the user gets no "зачислено", and PnL is never booked. (Note: `redeem_winnings`
skip returns — `no_token_balance` / `collateral_unmatched`, `core/relayer.py` L245/262/265 — also just
`log.info("redeem_skipped")` and return, clearing nothing.)

**Prod evidence (user 4 / `@sto1ner`, 15 outstanding):**

| Category | Trades | `resolved` | `payout` | on-chain `shares` | `redeem_key_ttl` | Diagnosis |
|---|---|---|---|---|---|---|
| Open (correct) | 809,808,807,806,805,801 | False | 0 | > 0 | `-2` (no key) | genuinely unresolved — waiting |
| **WON, stuck** | **799,791,777** | True | 1 | **7.35 / 5.26 / 5.26** | ~5.4–6.5 d | **winnings held, redeem blocked by stale key (A1)** |
| WON, ledger desync | 796,795,788,785,783,773 | True | 1 | **0.0** | ~6.2 d | tokens already redeemed on-chain, ledger never settled (A2) |

≈ **17.9 shares (~$17.90)** of confirmed winnings for a single user are sitting unclaimed with ~6 days
left on the blocking key; six more are redeemed-but-unbooked.

### 20.2 Design — 20.A: make the redeem pipeline self-healing & idempotent

**Fix A1 — dedup TTL + failure-clear (short, self-expiring lease, not a 7-day lock).**
Treat the redeem key as an **in-flight lease**, not a permanent "done" marker:
- Set it with a **short TTL** (`redeem_lease_sec`, proposed **900s**) at dispatch — long enough to
  prevent double-dispatch within a cycle, short enough to auto-retry soon after a failure.
- **Clear it** (`_client().delete("once:redeem:{uid}:{cond}")`) in `redeem_position`'s failure and skip
  paths (the `except` before `self.retry`, and every `redeem_skipped` return) so the next
  `reconcile_settlements` cycle re-attempts instead of waiting a week. Add a `clear_once(key)` helper to
  `core/cache.py`.
- The **success** terminal state is `copy_trades.redeemed_at IS NOT NULL` (the ledger), **not** the
  Redis key — so `get_outstanding_copy_trades` (which filters `redeemed_at IS NULL`) is the real dedup
  and the Redis lease only prevents intra-window stampedes.

**Fix A2 — always pass `trade_id` + a real `entry_cost`, and make marking mandatory.**
- `sync_positions` redeemable dispatch (L121) must pass `trade_id` and `entry_cost` — look up the open
  `copy_trades` row for `(uid, condition_id)` (reuse the same lookup `reconcile` uses at L975/L1004) so
  the success path can mark it. Never dispatch a redeem that can't be reconciled.
- Compute `entry_cost` via the **Blueprint 19 cost-basis resolver** (DB `entry_price × shares`, with the
  BP19 fallback chain) rather than the zero-prone Data-API — this also feeds 20.D (Net PnL).

**Fix A3 — reconcile the "already redeemed on-chain" case (shares == 0 winners).**
In `reconcile_settlements`, when a trade is `resolved && won` but the on-chain `ctf_token_balance == 0`
(tokens already gone), do **not** dispatch a redeem (there is nothing to claim). Instead
`mark_trade_settled(result="win", realized_pnl = proceeds − entry_cost)` directly, where `proceeds` is
the pre-redeem `shares` (from `copy_trades.shares`, or `size_usdc/entry_price`), and send the win
notification once. This drains the six ledger-desync rows and books their PnL. Clear the stale key.

**Fix A4 — stop skipping silently.** Every early `continue` in the reconcile win branch and every
`redeem_skipped` return must emit a distinct throttled log (`reconcile_redeem_blocked`,
`redeem_skipped_reason`) so a stuck claim is visible within minutes, not discovered by a user complaint.

**One-time prod cleanup (ops runbook, not code):** delete the stale keys so recovery is immediate rather
than waiting ~6 days: `redis-cli --scan --pattern 'once:redeem:*' | xargs redis-cli del` (safe — the
ledger `redeemed_at` is the real dedup; worst case a redeemed trade re-dispatches and
`redeem_winnings` returns `no_token_balance`, now handled by A3/A4). Run **after** deploying A1–A4.

### 20.3 Root cause & design — 20.B: outcome fallback (`Исход: —`)

Outcome comes straight from the Data-API position and is empty for grouped/scalar markets:

```436:436:core/polymarket.py
                "outcome":      p.get("outcome", ""),
```

**Fix — a central `resolve_outcome_name(...)` helper**, applied in every `_emit_*` notifier, with a
fallback chain: (1) API `outcome`; (2) `trade_signals.outcome` via `signal_id` (we persisted it at
entry — `execute_copy` L496); (3) the market's `outcomes[outcome_index]` from Gamma; (4)
`groupItemTitle` for grouped markets ("Any Other Score" etc.); (5) last-resort binary default
`Yes/No` from `outcome_index`. Only fall to `—` when all fail.

### 20.4 Root cause & design — 20.C: full titles + word-boundary truncation

Two issues: notifiers hard-slice `[:50]`/`[:40]`/`[:48]` (mid-word cut), and the positions Data-API
`title` can be partial. **Fix:** (1) source the **full** title from `trade_signals.title` (the complete
question stored at signal time, `execute_copy` L466) or the Gamma `question`, not the positions feed;
(2) add one `smart_truncate(text, limit)` helper (cut on the last word boundary ≤ limit, append `…`)
and replace the scattered `[:N]` slices in `_notify_closed`, `_emit_win`, `_emit_loss`,
`_emit_win_pending`, `_emit_win_retry_failed`, and `redeem_position`'s notify.

### 20.5 Root cause & design — 20.D: Net PnL in the win notification

The win notice shows only the credited amount:

```771:779:worker/tasks/manage_positions.py
        _notify(
            user["telegram_id"],
            f"💸 <b>Выигрыш зачислен</b>\n\n"
            f"📌 {(title or '—')[:50]}\n"
            f"🎯 Исход: <b>{outcome or '—'}</b>\n"
            f"➕ Зачислено: <b>+${credited:.2f} pUSD</b>\n"
            f"💼 Торговый баланс: <b>${bal_after:.2f} pUSD</b>"
            f"{_event_link(event_slug)}",
        )
```

`entry_cost` is already a parameter but is (a) not passed by `sync_positions` (20.A2) and (b) never
displayed. **Fix:** resolve `entry_cost` via the **Blueprint 19** cost-basis resolver (never the API
avg), compute `net_pnl = gross − entry_cost` (gross = `shares_bal` × $1.00), and add a line:

```text
💸 Выигрыш зачислен
📌 <full event title, word-safe>
🎯 Исход: <resolved outcome>
➕ Выплата: +$12.35
🏆 Net PnL: +$4.60  (выплата $12.35 − вход $7.75)
💼 Торговый баланс: $42.18 pUSD
```

Guard the unknown-cost-basis case (legacy rows, BP16/BP19): if `entry_cost` is unresolved, render
`Net PnL: —` — never a wrong/negative-looking number.

### 20.6 Files touched

| File | Change |
|---|---|
| `core/cache.py` | **+** `clear_once(key)` helper (DELETE `once:{key}`); document the lease vs. terminal-state distinction |
| `worker/tasks/manage_positions.py` | A1: short `redeem_lease_sec` TTL on the redeem key + clear on failure/skip; A2: `sync_positions` passes `trade_id`+`entry_cost` (BP19 resolver); A3: reconcile `shares==0 && won` → `mark_trade_settled` directly (no dispatch); A4: loud `reconcile_redeem_blocked`/`redeem_skipped_reason` logs; 20.B/C/D in the `_emit_*` + `redeem_position` notify |
| `core/polymarket.py` | **+** `resolve_outcome_name(...)` (20.B) and a Gamma title/outcomes fetch by condition/slug; **+** `smart_truncate(text, limit)` (20.C) |
| `core/db/queries.py` | reuse BP19 cost-basis resolver for `entry_cost`; helper to fetch open `(uid, cond)` row (`trade_id`, `shares`, `entry_price`, `signal.title/outcome`) for 20.A2/20.B/20.D; export (BP12-A guard) |
| `core/config.py` | **+** `redeem_lease_sec = 900` |

**No new migration.** Every column used (`entry_price`, `shares`, `size_usdc`, `signal_id`, `result`,
`realized_pnl`, `redeemed_at`) already exists (migration 008); `trade_signals.title/outcome` exist.

### 20.7 Acceptance criteria

1. A resolved win with tokens still held (e.g. trade 799, 7.35 shares) is **redeemed within one
   reconcile cycle** even after a prior failed attempt — the redeem key is a ≤15-min lease, cleared on
   failure, and re-armed automatically.
2. A resolved win already redeemed on-chain (`shares==0`, e.g. trade 796) is **marked settled**
   (`result='win'`, `realized_pnl` booked, `redeemed_at` set) by reconcile **without** dispatching a
   redeem, leaves the outstanding set, and notifies the user once.
3. No redeem is ever dispatched without a `trade_id`; on success `mark_trade_settled` always runs.
4. A stuck/blocked claim emits a throttled `reconcile_redeem_blocked` / `redeem_skipped_reason` log —
   never a silent `continue`.
5. Win/loss/pending notifications show a **resolved outcome** (never `—` when the signal or Gamma has
   it) and a **full, word-boundary-truncated** title (no mid-word cut).
6. The "Выигрыш зачислен" message shows **Net PnL = payout − entry cost** (BP19 cost basis), or `—`
   when the cost basis is genuinely unknown.
7. After the one-time key purge + deploy, `reconcile_settlements` `processed` > 0 while winners drain,
   and the outstanding count falls to only genuinely-unresolved trades.

### 20.8 Evidence pass (already executed — 2026-07-03) ✅ CONFIRMED

- 48h grep: **no** `redeem_failed`/`did not confirm`/`Traceback`/`retry` → not an on-chain failure.
- `redis-cli --scan 'once:redeem:*'`: dozens of keys with **~5–6.9 day** TTLs (7-day default), incl.
  keys for resolved winners still holding tokens.
- Live probe (user 4): 3 resolved winners with `shares>0` blocked by live redeem keys (money stuck) and
  6 resolved winners with `shares==0` still `confirmed/unredeemed` (ledger desync). Directly validates
  §20.1 A1 (stale key) + A2 (dispatch-without-`trade_id`) → fixes §20.2 A1–A4.

---

## Blueprint 22 — Admin-bot Audit: PnL / History / Positions / Balance ✅ IMPLEMENTED 2026-07-09

> **Class of bug:** the recurring *wrong-source / fail-silent* family (BP12-B, BP16, BP18, BP19).
> All four defects are **read-path/display** — no user money moves — but the numbers drive admin
> support decisions, so implausible PnL and fake-$0 balances are operational trust bugs.

### 22.0 Symptoms (prod, admin bot `/user`)

1. **Неправдоподобный PnL** — the card shows inflated/implausible realized PnL per user.
2. **Не видно, какие сделки совершил бот** — `📜 Последние 5 сделок` renders every row's
   title as `—`.
3. **Неправильная сумма в позициях** — no dollar amount for open positions, only a count.
4. **Неправильный баланс** — the balance line shows `$0.00 доступно` for funded users.

### 22.1 Root causes

**A — PnL: Data API primary + broken proxy-wallet cost basis.** Commit `e6f56a0`
("use Polymarket Data API for admin PnL") inverted the BP18 design: the API became primary and
the `copy_trades` ledger a fallback. But `/closed-positions` `realizedPnl` is derived from the
indexer's `avgPrice` — **provably 0/late for POLY_1271 proxy wallets** (BP16 §16.1, BP19
Finding B). Zero cost basis ⇒ a win's "profit" = the entire payout ⇒ inflated totals. The
`limit=100` request also silently truncates all-time PnL for active users, and the API window
buckets disagree with the ledger buckets the risk breakers use.

**B — History titles: nonexistent `event_slug` column kills the whole lookup.**
`get_user_trade_history` batch-selected `id, title, outcome, event_slug` from `trade_signals`.
`event_slug` was **never created by any migration and never inserted by any writer** — PostgREST
rejects the entire select, the bare `except: pass` swallows the error, `sig_map` stays empty and
**every** history row falls back to title `—`. (The FK-embed → two-step rewrite in `a0c45a9`
fixed the embed failure but kept the phantom column.)

**C — Positions: count only, and `outcome` never persisted for Model B.** The card had no
$-amount for open positions. Separately, the active poller (`poll_tracked_wallets`) never wrote
`outcome` on `trade_signals` inserts (only dormant Model A `scan_markets` carried it in the
in-memory dict, and even it didn't insert the column) — so history's `sig.get("outcome")` and
BP20's tier-2 outcome fallback always came up NULL; grouped markets showed a wrong YES/NO derived
from `outcome_index`.

**D — Balance: fail-silent zero.** `_user_view` wrapped `withdrawable_usdc(u)` in
`except Exception: avail = 0.0` — any RPC hiccup rendered a confident `$0.00 доступно`,
indistinguishable from a truly empty wallet. Same for POL.

### 22.2 Fixes shipped

| # | File | Change |
|---|---|---|
| A | `api/routers/admin_bot.py` `_user_view` | **DB-first PnL**: `get_pnl_summary(uid)` (ledger) primary; Data API only when the ledger has `settled == 0` (pre-008 legacy) or the DB read throws. Card labels the source: `📈 PnL (леджер)` / `(Data API)`. |
| B | `core/db/queries.py` `get_user_trade_history` | Signal lookup selects only `id, title, outcome`; failure now logs `trade_history_signal_lookup_failed` (never a bare pass). |
| B+ | `api/routers/admin_bot.py` `_fmt_history_row` | `smart_truncate(title, 40)` (BP20.C) instead of `[:32]`; row now shows trade **size** (`$X.XX`) and **settle date**. |
| C | `api/routers/admin_bot.py` `_user_view` | `📊 Открытых позиций: N · в позициях ≈ $X` — cost-basis sum via `get_open_trades_cost(uid)` (the §4/BP8 equity convention; no on-chain call). |
| C+ | `worker/tasks/poll_tracked_wallets.py` | `outcome` persisted on every `trade_signals` insert. **Fail-safe (§5):** on insert error the payload retries **without** `outcome` (`signal_insert_retry_no_outcome`) so the money path survives a missing migration. |
| D | `api/routers/admin_bot.py` `_user_view` | Balance read failure renders `⚠️ ошибка чтения (RPC)` + `admin_balance_read_failed` log; POL renders `—` on error. Never a fake `$0.00`. |
| — | `migrations/017_trade_signals_outcome.sql` | `alter table trade_signals add column if not exists outcome text;` + partial index `ix_copy_trades_user_redeemed (user_id, redeemed_at desc) where redeemed_at is not null` (admin history/PnL reads). |

New import in `admin_bot.py`: `get_open_trades_cost` (already exported from `core/db` — no
`__init__.py` change needed, BP12-A guard satisfied).

### 22.3 Deliberate design points

- **The ledger is the admin's source of truth.** `get_pnl_summary` uses the same
  `redeemed_at IS NOT NULL` + `COALESCE(realized_pnl, pnl_usdc)` predicate family as the risk
  breakers — the admin card and the risk engine can never disagree again. The user bot's `/pnl`
  (Data-API based) may legitimately differ from the ledger; the source label on the admin card
  makes that explicit instead of hiding it.
- **Positions valued at cost basis, not mark** — consistent with the §4 conventions (BP8):
  marking open positions to a depressed bid fabricates phantom numbers.
- **No silent zeros anywhere** in `_user_view` — every degraded read either shows an explicit
  error/`—` or falls back with a logged warning.

### 22.4 Acceptance criteria

1. `/user` PnL for a user with settled ledger rows equals `Σ realized_pnl` from `copy_trades`
   (source label `леджер`); a pre-008 user with an empty ledger falls back to Data API with the
   label `Data API`.
2. `📜 Последние 5 сделок` shows real market titles, outcome, entry→exit, size, PnL and date for
   ledger rows with `signal_id`; if the signal lookup fails it logs
   `trade_history_signal_lookup_failed` (grep-able) instead of silently rendering `—`.
3. The card shows `в позициях ≈ $X` = sum of open `copy_trades.size_usdc` (cost basis).
4. With the RPC down, the balance line reads `⚠️ ошибка чтения (RPC)` — never `$0.00`.
5. New Model B signals persist `outcome`; if migration 017 is missing, the poller logs
   `signal_insert_retry_no_outcome` and still fires the signal (money path never blocked).
6. Migration 017 applied before deploy (idempotent; safe to re-run).

### 22.5 Follow-up fix — dead pagination arrow in the history view (2026-07-09)

**Symptom:** tapping `▶️` in `📜 История сделок` did nothing (reported right after the BP22
deploy, once titles became visible).

**Root causes (three independent, all in the same render path):**
1. **Unescaped HTML in market titles.** `_fmt_history_row` interpolated the raw
   `trade_signals.title` into `<b>{title}</b>`. Polymarket titles legitimately contain
   `<`/`>`/`&` (e.g. *"Will BTC dip <$100K…"*) — Telegram's HTML parser rejects the message,
   `edit_message_text` throws, and the page flip dies. Page 1 happened to have clean titles;
   a dirty title on page 2 made the arrow look dead. Fix: `html.escape()` on title and
   outcome in `_fmt_history_row` (and in `_trades_view`, same latent bug).
2. **Silent failure UX.** The `on_callback` catch-all only logged
   `admin_callback_failed`; because `q.answer()` had already been consumed, the retry toast
   never displayed → dead button with zero user feedback. Fix: HTML-parse failures now
   retry the same content as **plain text** (tags stripped) so the page always renders;
   other errors answer with `show_alert=True` including the error snippet.
3. **Phantom arrow on an exact-multiple-of-5 history.** `len(trades) == 5` was the
   "has next page" test, so a user with exactly 5/10/15 settled trades got a `▶️` leading
   to an empty page. Fix: fetch `limit+1` rows as a next-page probe (`has_more`), render
   the 5, show `▶️` only when the 6th exists.

**Diagnosis command (server):** `docker compose logs api | grep admin_callback_failed` —
the swallowed exception (`Can't parse entities…`) is visible there for any past occurrence.

### 22.6 Follow-up fix — "в позициях" overstated by a desynced ledger (2026-07-09)

**Symptom:** the card showed `Открытых позиций: 16 · в позициях ≈ $78.28` for a user whose
real open exposure was ~$0 (balance $0.01).

**Root cause:** the BP22 positions line was ledger-only (`count_open_positions` +
`get_open_trades_cost`: `status IN ('confirmed','executing') AND redeemed_at IS NULL`). The
ledger **overstates** open positions in two known ways:
1. **BP20 A2/A3 desync** — positions settled/redeemed on-chain whose rows were never marked
   terminal (reconcile lag or blocked claims);
2. **stale `executing` rows** — a worker crash between `insert_copy_trade` and the status
   update leaves `executing` forever; `get_outstanding_copy_trades` filters
   `status='confirmed'`, so **the reconciler never drains them by design**.

**Fix (live-first, §18.2 design note):** the headline count/value now comes from the **live
Data-API positions of the deposit wallet** (identical predicate to the user bot's
`_live_positions`/`_is_dead_loss`: `shares > 0`, minus resolved-lost dust), valued at
`current_value`. The ledger is only the fallback when the Data API is down (labelled
`(леджер — Data API недоступен)`). When `ledger_count > live_n` the card appends
`⚠️ В леджере N незакрытых записей ($X) — reconcile отстаёт` — the desync is now an ops
signal on the card instead of a lie in the headline number.

**Ops note:** a persistent ⚠️ line means stuck ledger rows. Inspect them with:
`copy_trades where user_id=<uid> and redeemed_at is null and status in ('confirmed','executing')`
— `executing` rows older than ~1h are crash orphans (safe to mark `failed` manually);
`confirmed` rows on resolved markets mean `reconcile_settlements` is blocked — check
`reconcile_redeem_blocked` / `redeem_skipped_reason` logs (BP20 A4).

### 22.7 Follow-up fix — redeem stampede: won positions never redeemed (2026-07-09) 💰 MONEY-CRITICAL

**Symptom:** the 22.6 ⚠️ line revealed 43 stale `confirmed` rows for one user (~$222), some
dating back to June 26. Worker logs showed `reconcile_redeem_blocked … redeem key still live`
for ~30 conditions on **every** 2-min cycle, for days. Diagnostics confirmed: the markets are
resolved and WON on-chain, the winning tokens are still held by the deposit wallets — the
redeems were dispatched over and over but **never completed**. Users' winnings were stuck.

**Root cause — parallel redeems for one wallet always collide.** `reconcile_settlements`
dispatched `redeem_position` for **every** resolved win in a single burst (~20 tasks for one
user in 11 s). The worker is a gevent pool (concurrency 100), so all of them ran
`redeem_winnings` concurrently **against the same deposit wallet**. The relayer executes ONE
action per deposit wallet at a time and each call fetches its own relayer nonce → concurrent
calls collide → (nearly) all fail → BP20 A1 clears the lease on failure → the next cycle
re-dispatches the whole pack → permanent stampede that never drains. The BP20 short lease
(good on its own) turned a one-shot failure into a perpetual retry storm because the retry
was always a *mass* retry. Two burst snapshots in the logs showing *different* condition sets
proved the leases were churning (dispatch → fail → clear → re-dispatch), not sitting idle.

**Fixes (3 layers):**
1. **`reconcile_settlements` — one redeem dispatch per user per cycle.** A `redeem_dispatched`
   set defers every subsequent win for that user to the next cycle (releasing the 300s
   `reconcile:` dedup key so the row isn't skipped an extra cycle; logged as
   `reconcile_redeem_deferred`, info-level). Drain rate: 1 redeem / 2 min / user → a 40-row
   backlog clears in ~80 min, serially, exactly how the relayer wants it.
2. **`redeem_position` — per-wallet in-flight mutex** (`once:redeemwallet:{dw}`, TTL 300 s,
   released in a `finally`). Covers the other dispatch sources (sync_positions,
   close_position resolved-detection, backfill) that could still overlap with reconcile. On
   contention the task releases the condition lease and returns `{"skipped": "wallet_busy"}`
   — the next reconcile cycle re-attempts.
3. **`backfill_legacy_redemptions` — same cap** (max 1 dispatch per user per 10-min pass) +
   `get_outstanding_copy_trades` now orders by `id` so the backlog drains FIFO (oldest first).

**Legacy NULL-condition rows** (pre-migration-008, e.g. ids 675/676/683/685 from June 15–19):
the reconciler can never process them (`condition_id IS NOT NULL` filter) and
`backfill_legacy_redemptions` recovers the *funds* but never marks the *rows*, so they inflate
the open-ledger count forever. One-off cleanup (PnL-neutral — `realized_pnl` stays NULL, and
`get_pnl_summary` COALESCEs it to 0):

```sql
update copy_trades
   set result = 'legacy_unknown', resolved_at = now(), redeemed_at = now()
 where condition_id is null and redeemed_at is null
   and status = 'confirmed' and created_at < now() - interval '7 days';
```

**Watch after deploy:** `reconcile_win_dispatched` should appear once per user per cycle,
followed by `redeemed` + `redeem_done` (success) or `redeem_failed` (tracebacks now readable,
one at a time). If redeems STILL fail serially, the relayer path itself is broken — grep
`docker compose logs --since 1h worker | grep -A5 redeem_failed` and investigate the actual
exception (nonce/auth/RPC).

---

## Blueprint 26 — Sniper-Mode Donor Mirroring (5-min BTC markets) ✅ IMPLEMENTED 2026-07-15

### 26.0 Product decision (confirmed with PO)

We found a profitable donor bot that enters **5-minute Bitcoin up/down markets in the last
~30 seconds** when the outcome is priced at **70–80%**. Requirements:

1. Copy this wallet **in addition to** the existing Model-B copying (which is untouched).
2. **Mirror sizing** — enter with the donor's exact USDC amount. NO Kelly, NO per-user
   `max_position_usdc` cap, NO 5%-of-equity cap, NO profit-protection cap, NO exposure /
   event / drawdown / daily-loss gates, NO daily-trade cap, NO `copy_paused_until` honor.
3. **Same price or small slippage** — enter at the fresh best_ask, but ONLY if it is within
   `sniper_slippage_pct` of the donor's fill price (skip on drift — EV preservation).
4. The ONLY risk control left: the **30% stop-loss** (= the existing `delta_drop_stop_pct=0.30`
   Delta-Drop stop with all BP17/BP19/BP21 hardening), with sniper-specific overrides:
   `min_hold = 0`, `confirm_ticks = 1`.
5. Only **two allowlisted users** copy this wallet: telegram_ids **879714159** and **504677064**.
   Nobody else may ever receive these signals. Existing users see zero behaviour change.

### 26.1 Why the existing pipeline CANNOT do this (5 hard blockers)

1. **Market-universe filter**: `_build_market_meta` (core/polymarket.py) drops markets with
   `hours_left < market_min_hours_to_resolve` (0.5h) and `liquidityNum < 2000` — a 5-min BTC
   market NEVER enters the `fast_markets` cache → `poll_tracked_wallets` skips the signal as
   `skipped_no_market`. The cache TTL (120s) also exceeds the market's entire lifetime.
2. **Slice accumulator latency**: the Redis accumulator fires `slice_quiet_period_sec=45`
   seconds after the last fill. The market resolves before the signal fires.
3. **Poll cadence**: `tracked_poll_sec=15` + Data-API indexing lag (1–10s) eats the whole
   30-second window.
4. **Sizing/risk gates rewrite the stake**: fixed mode clamps to `max_position_usdc`
   (default $25), then unified risk cap, profit cap, book_safe_frac=0.25 depth clamp,
   risk gates 1–4, daily-trade cap.
5. **The stop can never fire**: `delta_drop_min_hold_sec=900` + `delta_drop_confirm_ticks=2`
   at a 120s sync cadence ≈ 19 minutes of required age; sniper positions live ~30 seconds.

### 26.2 Design (additive, zero churn for existing users)

**Schema (migration 019)** — `tracked_wallets.mode text not null default 'default'`
(`'sniper'` marks this donor), `tracked_wallets.allowed_telegram_ids bigint[]` (allowlist;
NULL/empty = nobody), `copy_trades.mode text default 'default'` (stamps sniper trades so the
stop-loss overrides and ops queries can find them).

**New fast poll task** `worker/tasks/poll_sniper_wallets.py` (beat: every `sniper_poll_sec=3.0`s,
queue periodic). Per sniper wallet: one Data-API activity fetch (limit `sniper_fetch_limit=10`);
group fresh BUY fills (age ≤ `sniper_max_trade_age_sec=25`) by (condition, token); per group an
**atomic Redis once-key** `sniper:{addr}:{cond}:{token}` (TTL 900s — one entry per market, later
slice-fills of the same burst are folded into the first batch or dropped); market meta comes
from **CLOB `/markets/{condition_id}` directly** (new helper `get_clob_market` in core/clob.py —
NOT the fast-markets cache), skipping when `accepting_orders=false` (already closed/resolved).
VWAP of the batch = signal price; SUM of the batch = mirror size. The signal carries
`"mode": "sniper"` and fans out ONLY to the allowlisted users (queried by telegram_id with an
active paid sub; `copy_paused_until` deliberately ignored per §26.0-2).
`poll_tracked_wallets` (slow path) MUST now skip `mode='sniper'` wallets — otherwise the donor
would be double-copied through both paths.

**Execute path** (`execute_copy_trade`, branch on `signal["mode"]=="sniper"`): skip pause check,
skip daily cap, `size_usdc = signal["size_usdc"]` (mirror; clamped only to free balance and the
$5 exchange minimum), skip Kelly/fixed sizing + unified risk cap + profit cap + risk gates 1–4 +
`max_open_positions` (unredeemed 5-min wins would exhaust the 15 cap in an hour), keep
`already_in_market`. Book re-check: skip when `best_ask > signal_price * (1 + sniper_slippage_pct)`
(reason `sniper_price_drift`) or `best_ask > sniper_max_entry_price=0.97`; depth cap = full
fillable within the band (no 0.25 fraction). Skip the inline AI call (market resolves before the
text is read; saves ~1s + tokens). On exception: mark failed and **return — NO Celery retry**
(a retry 5s later fires into a resolved market). Stamp `"mode": "sniper"` on the copy_trades row.

**Stop-loss** (`sync_positions`): `get_open_trade_by_token` additionally selects `mode`; when
`db_trade.mode == 'sniper'` → hold-time guard uses 0 instead of `delta_drop_min_hold_sec`, and
the confirm-ticks requirement is 1 instead of `delta_drop_confirm_ticks`. The threshold itself
stays `delta_drop_stop_pct=0.30` — exactly the PO's 30%. All other layers (phantom-book guard,
hard-stop-first, spread veto + catastrophic bypass) apply unchanged.

**New settings** (core/config.py): `sniper_poll_sec=3.0`, `sniper_max_trade_age_sec=25`,
`sniper_fetch_limit=10`, `sniper_slippage_pct=0.02`, `sniper_max_entry_price=0.97`,
`sniper_dedup_ttl_sec=900`.

### 26.3 Accepted limitations & pitfalls (documented, NOT bugs)

- **Latency race**: donor at T-30s → Data-API indexing (1–10s) → poll (≤3s) → execute (~2-4s).
  We land at T-25…T-5s; sometimes the market closes first → CLOB rejects the order → row goes
  `failed` with the rejection text. Expected; the drift-skip and accepting_orders check keep
  most doomed orders from even being sent.
- **Capital velocity**: winnings sit as unredeemed tokens until `reconcile_settlements` drains
  them (serialized 1 redeem/user/2min — BP22.7). With a donor trading every 5 min, part of the
  bankroll is always in flight; entries during that window get clamped to free balance (or skip
  under $5). This is balance-limited mirroring, not a bug.
- **Stop-loss is best-effort on a 30s lifetime**: sync runs every 120s, so a position that
  lives 30s usually resolves before the first stop evaluation. The 30% stop matters mainly for
  donor trades on longer markets and for the tail case where the 5-min market glitches. This
  is understood and accepted; we deliberately did NOT build a per-second stop loop for v1.
- **Notification volume**: every entry/win/loss produces the standard messages; a busy donor
  = many messages to the two users. Accepted for v1.
- **Multiple fills**: the donor may slice; only fills present in the same poll batch are
  summed. A slice arriving in the NEXT poll is dropped by the once-key (mirror size can
  undershoot the donor's final total). Accepted — never overshoots.

### 26.4 Ops — enable the donor (after deploy + migration 019)

```sql
-- Supabase SQL editor; replace 0xDONOR with the real address (lowercase!)
insert into tracked_wallets (address, label, active, mode, allowed_telegram_ids)
values ('0xdonor…', 'BTC 5-min sniper', true, 'sniper', array[879714159, 504677064]::bigint[])
on conflict (address) do update
  set active = true, mode = 'sniper',
      allowed_telegram_ids = array[879714159, 504677064]::bigint[];
```

Verify: `docker compose logs --since 10m worker | grep -E "sniper_signal_fired|sniper_skip|copy_trade_ok"`;
`poll_sniper_wallets` should tick every ~3s (grep `sniper_poll`); slow path must show the donor
excluded. A dry check without funds: allowlist a signal-only test user first.

## Blueprint 26.5 — Real-Time Sniper Feed (RTDS WebSocket) ✅ IMPLEMENTED 2026-07-15

### 26.5.0 Why

The Data-API polling path (§26.2) has an irreducible 5–20 s donor→entry latency:
Data-API activity indexing (measured 4–15 s) + poll cadence (≤3 s) + execute. For a donor
entering at T-30 s of a 5-minute market that eats most of the edge. PO requires 1–2 s.

### 26.5.1 Source — verified live against the updated (CLOB v2 era) docs

Polymarket's **RTDS** (`wss://ws-live-data.polymarket.com`, docs "Real-Time Data Socket")
streams **every trade on the platform** on topic `activity`. Verified live 2026-07-15
(scripts/rtds_smoke.py): **receive lag ≈ 1.5 s after the on-chain match**, payload carries
`proxyWallet`, `conditionId`, `asset` (token id), `side`, `price`, `size` (**SHARES**, not
USDC — usdc = size × price), `outcome`, `outcomeIndex`, `eventSlug`, `timestamp` (seconds),
`transactionHash`. Payload is a single object per frame.

Hard-won protocol facts (github.com/Polymarket/real-time-data-client + issue #34):
1. Subscription type **`trades` is dead — only `orders_matched` delivers** (same schema).
2. Server-side filters accept ONLY `event_slug`/`market_slug` (exact string match on a
   compact JSON string!), NOT wallets → we subscribe **unfiltered** and match
   `proxyWallet` client-side against the sniper donor set (dict lookup, trivial CPU).
3. Server expects a literal text `"ping"` every ~5 s; protocol-level keepalive misbehaves
   (silent drops) → `ping_interval=None` + manual text ping + a **silence watchdog**: no
   frames for `sniper_ws_silence_reconnect_sec=60` s → force reconnect (the unfiltered
   firehose is never quiet for long, so silence == dead socket).

### 26.5.2 Architecture

**`worker/sniper_ws.py`** — `SniperFeed`: synchronous `websockets.sync.client` loop
(connect → subscribe → recv with 5 s timeout → ping/watchdog/donor-refresh), exponential
reconnect backoff (max 30 s), donor set re-read from `tracked_wallets` every
`sniper_ws_refresh_donors_sec=60` s. A matching fresh BUY (same
`sniper_max_trade_age_sec` bar as the poller — also guards reconnect replays) is
normalized to the poller's fill shape and handed to `fire_sniper_signal`.

**Runs as a daemon thread in the BEAT container** (`worker/beat.py::_start_sniper_ws`),
NOT the worker: beat is exactly one replica (no duplicate listeners when the worker
scales) and a plain non-gevent interpreter (the worker's gevent monkey-patching breaks
socket/asyncio threads — the BP23 lesson). Beat only *dispatches* Celery tasks
(`execute_copy_trade.delay`), execution stays on the worker's trades queue.

**Shared exit point**: `fire_sniper_signal(addr, allowed, cond, token, fills)` extracted
from `poll_sniper_wallets` — once-key claim, CLOB meta check, signal build, fan-out.
The WS listener AND the 3-second poller both call it; the atomic Redis once-key
`sniper:{addr}:{cond}:{token}` guarantees at most ONE entry per market whichever path
sees the fill first. **The poller stays enabled as the WS-downtime fallback.**

**Execute-path latency trim** (`execute_copy_trade`): sniper skips the Data-API
`get_positions` fetch (~0.5–1 s) and the ledger-equity computation — they only feed the
risk gates / `already_in_market` / `max_open_positions` guards which sniper bypasses
anyway (`positions=[]`, `equity=tradeable`; the once-key already enforces one entry per
market). The "$100 recommended" soft warning is also skipped for sniper.

**New settings**: `sniper_ws_enabled=true`, `sniper_ws_url`,
`sniper_ws_refresh_donors_sec=60`, `sniper_ws_silence_reconnect_sec=60`.

### 26.5.3 Expected end-to-end latency

RTDS delivery ~1.5 s + fire_sniper_signal (CLOB meta + signal insert ~0.4 s) + Celery
dispatch (~0.1 s) + execute (balance + book + order ~1.5–2 s) ≈ **3–5 s after the donor's
match** (vs 8–25 s via the polling path). The floor is bounded by RTDS's own ~1.5 s and
by order placement RPCs; sub-2 s total would require a private fill feed that Polymarket
does not expose.

### 26.5.4 Pitfalls / accepted

- WS fill `size` is SHARES — the listener converts (× price) before summing; the Data-API
  poller keeps using `usdcSize`. Mixing the two units would corrupt mirror sizing.
- The WS sees each donor fill individually (no 3 s batch window), so the mirror fires on
  the FIRST fill of a sliced burst; later slices are dropped by the once-key → mirror can
  undershoot a slicing donor (same "never overshoot" stance as §26.3).
- Beat restart = listener restart; fills during the gap are picked up by the 3 s poller
  (slower but not lost). No state to persist — the once-key lives in Redis.
- If RTDS someday kills `orders_matched` like it killed `trades`, the symptom is
  `sniper_ws_session_error: no frames for 60s` reconnect loops in beat logs while the
  poller keeps working; swap the subscription type per the client repo's README.

## Blueprint 26.6 — Patient Entry + WS Hygiene ✅ IMPLEMENTED 2026-07-16

Prod findings after one day of BP26.5 (16 signals, 7.5 h):
1. WS latency solved (donor fill seen in 0.16–1.03 s), **but 62% of entries were skipped as
   `price_drift`**: the donor's own $14 order sweeps the thin BTC book, so 1-2 s later the
   best ask sits 5–19 cents above his price (0.81→0.86, 0.80→0.99). MMs requote these books
   from spot within seconds — the one-shot check gave up exactly when waiting would win.
   The one-shot rule was also adversely selective: it kept entries where the price FELL
   after the donor (market disagreeing with him) and skipped those where it rose.
2. RTDS silently dropped ~hourly (`no frames for 60s`); with reconnect backoff capped at
   30 s one real donor fill was lost (`stale_fill lag=37s`).

**Fixes** (`execute_copy` sniper branch, `sniper_ws`, config):
- *Patient entry*: poll the book every `sniper_entry_poll_sec=0.7` for up to
  `sniper_entry_wait_sec=10`; enter the moment best_ask ≤ donor_price × (1+2%) AND
  ≤ `sniper_max_entry_price`; on timeout skip (`price_drift_timeout`). Depth clamp within
  the band unchanged.
- *WS hygiene*: reconnect backoff cap 30 s → 3 s; silence watchdog 60 s → 25 s
  (`sniper_ws_silence_reconnect_sec` default changed).

Measured capacity note (mass-rollout input): after the donor's sweep the refilled band
holds $10–25; two mirror users already split it (both got $9.38 partial fills on one
signal). Mirror-size copying does NOT scale past ~3-6 users; mass rollout must use small
fixed per-user stakes and accept depth rationing.

## Blueprint 26.7 — Settlement & Stats Truth Fixes ✅ IMPLEMENTED 2026-07-16

Three prod bugs, one root theme: wrong or missing ground truth.

**1. Eternal `collateral_unmatched` redeem loops (≈300 skips/day).** Rows 715/764/799:
`copy_trades.outcome_index=1` stored by the legacy entry fallback ("not Yes → 1"), but the
held token is index 0 — which LOST. Reconcile read the winning leg's payout → classified
as win → dispatched redeem forever; redeem_winnings probed positionId with the wrong index
→ `collateral_unmatched` skip, no settle, repeat. **Fix**: new
`core/relayer.py::detect_outcome_index(cond, token_id)` — matches the token's on-chain
positionId across both indices × all collaterals (pUSD/USDC.e/USDCn/WCOL). Reconcile now
verifies+repairs the stored index (log `reconcile_outcome_index_repaired`) before
classifying; redeem_winnings also self-corrects (`redeem_outcome_index_corrected`).
Effect: those three rows settle as LOSSES on the next reconcile pass (users get the
correct 💔 notification once, capital ledger unblocks).

**2. Wrong outcome in win/loss notifications ("Исход: New Rihanna Album" on a Tel-Aviv
market).** `resolve_outcome_name` queried Gamma with a **non-existent `conditionId`
param** — Gamma ignored it and returned the default market list; Tier 4 then took the
first market's groupItemTitle. **Fix**: correct param `condition_ids`, with a
`closed=true` retry (Gamma's default filter hides resolved markets). Tier order also
corrected: informative `outcomes[idx]` (Up/Down/Over/Under/teams) beats groupItemTitle;
groupItemTitle still wins over bare Yes/No on grouped markets.

**3. /pnl showed 100% winrate while losses existed.** `_build_pnl` used Data-API
`/closed-positions`, where a LOST position never appears (its worthless tokens are never
sold or redeemed — the API keeps it "open"). **Fix**: realized stats now come from the
copy_trades ledger (`get_realized_pnl_rows`; losses booked by reconcile, wins by redeem,
manual exits by close_position). Unrealized/open snapshot still live from Data-API.

## Blueprint 26.8 — Audit Follow-ups (FAK retry, log hygiene) ✅ IMPLEMENTED 2026-07-19

Findings from the 72-h full audit (2026-07-19). Trading core confirmed healthy: sniper
mirrors donor size in full, delta-drop stop fires, settlements + payouts flow, the
collateral_unmatched backlog self-healed (18 → 0 stuck; the remaining 5 "stuck" rows were
genuinely unresolved markets — golf/box-office/football all resolving 2026-07-19).

**1. Sniper FAK burn (21 failed trades / 3 days).** `place_order` FAK died with
`no orders found to match with FAK order` — the ask seen by the patient-entry loop
vanished before our order hit the CLOB (donor sweep / MM pull-and-requote), and the
sniper branch marked the trade failed with no retry. **Fix** (`execute_copy.py`): on that
specific error, sniper-only, re-read the book and re-place at the fresh ask while it's
still inside the slippage band + `sniper_max_entry_price`, up to
`sniper_fak_max_retries` (default 4) attempts spaced `sniper_entry_poll_sec` apart.
Logs: `sniper_fak_retry` / `sniper_fak_retry_drift`. Non-sniper path unchanged.

**2. Dedup-race noise (`tracked_signal_insert_failed` ×30).** The unique constraint
`trade_signals_source_tx_hash_key` (23505) rejecting a racing insert is the dedup working
— but the handler retried without `outcome` (migration-017 fail-safe), failed again, and
logged a full error traceback. **Fix** (`poll_tracked_wallets.py`): 23505/duplicate-key is
recognized before the fail-safe retry and skipped quietly (`signal_duplicate_skip`, info).
No signal is lost — the racing insert already dispatched it.

**3. Blocked-user log spam (`notify_signal_only_failed` ×876).** Two signal-only users
(7522224802, 566121446) blocked the bot; every fan-out logged a 403 traceback. **Fix**
(`execute_copy.py::_notify_signal_only`): Telegram 403 now logs one
`notify_signal_only_blocked` warning per user per day (Redis `notify_once`), no traceback.

**Historic stop-loss case clarified (not a bug today):** the "South Korea knockout" loss
(-$4.85, trade 714) was opened 2026-06-27 — *before* BP21 shipped the delta-drop rework.
Price fell 0.17→0.075 within min-hold-era rules, recovered to 0.29, then gapped to ~0 at
match end. Current stop logic (confirmed by `delta_drop_triggered` → `position_closed`
events in the same audit window) would have exited near 21:30 UTC.

## Blueprint 26.9 — Risk-Override Gate 3 Bypass + Phantom-Equity Fix ✅ IMPLEMENTED 2026-07-20

**Symptom (prod, 2026-07-20):** zero entries for days despite active whales and a healthy
signal funnel. User pressed «Снять блокировку» 7× (uid 1) / 4× (uid 2) with no effect.

**Root cause chain (three interlocking bugs):**
1. **Override never bypassed Gate 3.** `execute_copy` honored `risk_override_until` only
   for gate 4 (`daily_pnl=0`); gate 3 (drawdown) kept blocking per-trade against the stale
   HWM — silently (BP8 routes pause alerts to the monitor, so the user saw "active").
2. **Unlock's HWM reset was undone within minutes.** The monitor's HWM update used
   cost-basis equity that counted doomed-but-unsettled positions at entry cost, pushing
   the freshly-reset HWM straight back up (251 → 254 on 2026-07-18).
3. **Phantom equity from Data-API "open" losers.** The Data API keeps LOST positions open
   forever (worthless tokens are never redeemed). `total_equity`'s `shares×avg_price`
   fallback valued that dead weight at entry cost: uid 1 showed $254 equity vs $113.6
   real; uid 2 $253 vs $77.6. When settlements booked the losses, drawdown jumped to
   55%/69% vs the inflated HWM → Gate 3 blocked everything, forever.

**Fixes:**
- `execute_copy`: active override now passes `hwm=0` alongside `daily_pnl=0` →
  `check_risk_gates` computes `hwm=max(0,equity)=equity` → drawdown=0 for the rest of the
  UTC day (log `risk_gates34_bypassed_override`). Gates 1–2 still apply.
- `core/risk.py` (`total_equity` + Gate 1 `_position_cost`): resolved (`redeemable`)
  positions not in our open ledger are valued at `current_value` (0 for losers, face for
  winners) instead of entry cost — kills phantom equity/exposure at the source.
- One-time state repair: `equity_hwm` + `realized_baseline` reset to true equity
  (uid 1 → 113.60, uid 2 → 77.55). NOTE: re-run the reset AFTER deploying, the old
  monitor code re-inflates the HWM every cycle until restarted.

**Also 2026-07-20:** admin accidentally ran `/refresh`, which deactivated 12 curated
wallets (`active=false`, soft) and added 2 auto-discovered ones. Restored the 12 via DB
update (ids 5,6,8,10,11,12,14,16,17,18,19,20); the 2 new ones (Oddscompiler, rambinsky)
left inactive per user judgment. `/refresh` is soft — recovery is always an
`active=true` flip, never data loss.

## Blueprint 27: Explicit wallet-creation onboarding (2026-07-20)

**Why.** BP15 onboarding silently generated EOA keys on `/start` and hid the real
Polymarket registration behind a «Зарегистрировать кошелёк» button on the funding
screen. Users didn't understand that a dedicated on-chain wallet exists for them.
New flow makes wallet creation an explicit, staged, verifiable act.

**Flow (all in `api/routers/telegram.py`, no DB migration):**
- `/start` for a user without `wallet_address` → Screen A `_onb_create_text()` with
  buttons: `✅ Создать мой Polymarket-кошелёк` (`onb_create_wallet`),
  `🎬 Смотреть сигналы (без риска)` (existing `onb_signals`, secondary),
  `❓ Как это устроено?` (`onb_how_wallet` → Screen B, back via `onb_back_create`).
  **No keys are generated on /start anymore.**
- `onb_create_wallet`: staged edits of one message —
  1. «Получаем отдельный адрес…» → `generate_wallet()` + save EOA
     (`is_signal_only=True`), then 7 s perception pause;
  2. «Регистрируем кошелёк в инфраструктуре Polymarket…» → REAL
     `_register_deposit_wallet` (deploy + approvals + CLOB creds, 30–60 s) via
     `asyncio.to_thread` so the bot loop isn't blocked;
  3. Screen C wallet card (`_show_wallet_card`): EOA address, Polygon, pUSD balance,
     «Торговая система: выключена», creation date (from active `user_wallets` row).
  Guards: Redis lease `onb_create:{tg_id}` (ttl 180 s, `notify_once`/`clear_once`)
  against double-tap; idempotent re-entry (EOA kept, registered user → card);
  failure → error message + `🔁 Повторить`.
- Card buttons: Polygonscan URL, `📋 Скопировать адрес` (native `copy_text` /
  `CopyTextButton`, PTB ≥ 21.7 — requirements bumped; runtime fallback
  `onb_copy_addr` sends the address as a `<code>` message), `💳 Пополнить` →
  `onb_fund_steps`, `❓ Кто управляет кошельком?` → `onb_custody` (custody explainer,
  back via `onb_wallet_card`).
- Funding screen: `_funding_steps_text/_kb` now take `registered` — registered users
  see step 3 «🚀 Включить систему» (`system_on`: `is_signal_only=False`,
  `copy_active=True`, empty-balance hint) instead of the register step; unregistered
  legacy demo users still get the old `register` button (repair path kept).
- `_onboarding_stage` reordered: `wallet_registered=True` → `active` regardless of
  `is_signal_only`, so registered-but-off users land on the dashboard (checklist),
  never back on the demo welcome. `demo` now means "has EOA, NOT registered,
  signals-only" (legacy BP15 funnel, unchanged for existing users).
- Compat: walletless signal-only users (possible post-BP27) tapping
  «Показать адрес для пополнения» are routed into Screen A; stuck users
  (EOA yes / deposit no) can tap create — generation is skipped, registration runs.

Deploy: `git pull && docker compose up -d --build api` (image rebuild picks up
PTB ≥ 21.7 for the copy button; no worker/beat changes).

## Blueprint 28: Sniper economics audit, de-crutching plan & crypto-platform roadmap (2026-07-21)

Analyst/architect pass — no code changes in this BP. Everything below is backed by
prod data pulled 2026-07-21 (`scripts/audit_bp28*.py`): 109 settled sniper trades,
118 donor signals over 7 days, donor's full Data-API history (823 closed markets).

### 28.1 Verified findings (numbers first)

**F1. The PnL ledger is corrupted by `_confirm_fill` — this is the root of "impossible
PnL" AND most of the "chaotic sizes".** 64 of 109 settled sniper rows fail the sanity
check `realized_pnl == shares − size_usdc` (wins) — some absurdly (id 1169: stake
$6.21 @ 0.78 → recorded pnl +$21.40; math ceiling is +$1.75). Mechanism:
- `_confirm_fill` (execute_copy.py) measures the fill by polling **Data-API positions
  4–20 s after the order** and takes `current_value` = shares × *price at read time*.
  In a 5-min market the price moves violently in those seconds, so the recorded
  `size_usdc` (cost!) and derived `shares = current_value / entry_price` are both wrong
  in either direction.
- Redemption then credits the TRUE on-chain share balance, and
  `pnl = true_payout − corrupted_cost` produces the phantom profits/losses users see.
- Extreme case id 1126: true spend ≈ $19 @ 0.18 (≈107 shares), but a post-fill price
  bounce made `current_value` = $91.28 → recorded loss −$91.28 on a $19 trade.
- So the "$6–8 vs $20–30 entries" are ~half measurement artifact: true spend/donor
  ratio has mean 0.99 / median 0.96; the real dispersion comes from (a) FAK partial
  fills in a thin book (real), (b) the balance clamp when the wallet is short (real),
  (c) `current_value` mis-measurement (artifact).
- **Fix direction (P0):** take exact cost & shares from the CLOB order response
  (`create_and_post_market_order` returns matched making/taking amounts) instead of
  Data-API polling. Kills the 4–20 s confirm latency AND the corruption. Data-API
  read stays only as a fallback when the response is missing fields.

**F2. "Patient entry" is adverse selection in disguise.** Bucketing settled trades by
our fill price vs the donor's price:
- fill ≥5% BELOW donor: n=40, winrate 62.5%, ROI **−24.5%** (we caught falling knives —
  the ask came back down because the market was turning against the donor's side);
- fill 2–5% below donor: n=21, winrate 81.0%, ROI +12.6%;
- fill within ±2% of donor: n=49, winrate 77.6%, ROI **+13.7%**;
- fills above donor: n=0 (the 2% ceiling already blocks chasing up).
The trades we MISS (price ran away, e.g. donor @ 0.90 and the chart kept climbing)
are disproportionately the GOOD ones — 45/118 signals had no entry attempt and 11 more
failed all attempts (47% missed overall, avg donor px of missed = 0.81). Copy-with-lag
on 5-min markets systematically buys the bad fills and skips the good ones.
**Fix direction:** tighten the entry band to [donor_px − ~4%, donor_px + 2%] — never
"wait for a dip" below that; a skipped trade is cheaper than a −24.5% ROI bucket.
This is a parameter/logic simplification, not a new mechanism.

**F3. The donor's edge is razor-thin — copying it with fees and lag is structurally
≤ 0 for followers.** Donor lifetime: 823 closed markets, 83.5% winrate, avg entry
~0.82–0.83 → breakeven winrate 82.6% → net **+1.11% ROI** (+$105 on $9.5k turnover);
last week −$133. Our copy sample: donor winrate 68.9% on the signals we saw,
hypothetical hold-to-resolution at donor's own prices = −$187 on $1.1k. On top of
that 5-min crypto markets charge a ~1–2% taker fee (observed fee estimates 1.2–2.0%
in prod errors), pushing follower breakeven to ~84%+ winrate at 0.82 entries.
**Conclusion: no amount of engineering makes copying THIS donor at THESE prices
profitable for users. The edge must come from entering earlier at lower prices —
which is exactly the own-bot plan.**

**F4. Book depth / capacity.** The book on these markets is thin at signal time: the
donor's own ~$19 sweeps it (prod: ask 0.81→0.86 right after his fill), and our two
users already fight for the remainder (partial fills like $1.73). Within a ±2% band
the immediate depth at signal time is tens of dollars, not hundreds. 50–100 users ×
$15–20 = $1.5–2k demand per signal is 1–2 orders of magnitude above capacity on ONE
market. Depth is NOT always thin (MMs requote from spot within seconds), but at the
moment that matters — the last 30 s, right after the donor's sweep — it is.
**Fix direction:** (a) log a book snapshot with every sniper signal (we already fetch
the book — one structured log line, zero extra I/O) to measure capacity empirically
for 2–4 weeks; (b) plan capacity as: more assets × more market instances × user
sharding, not bigger orders in one book.

**F5. $200 minimum balance recommendation is justified.** Worst observed day at donor
mirror sizing (~$19/trade): −$159.74 across 32 trades; typical bad day −$40…−90.
With mirror sizing a $100 wallet can be wiped in a day (and empirically got close).
Recommendation stands as a SOFT floor. Better: switch sniper sizing from "mirror
donor absolute $" to "fixed fraction of the user's bankroll" (e.g. 8–10% per trade,
$5 exchange minimum still applies) — the $200 recommendation then becomes
self-enforcing (10% × $200 = $20 ≈ donor size today) and scales both down AND up.

### 28.2 De-crutching audit (what to remove/simplify, in priority order)

Codebase: core 3.2k + tasks 4.0k + routers 3.4k lines. The accumulated BP1–BP27
layers left overlapping mechanisms. Removal candidates, safest first:

1. **`_confirm_fill` Data-API polling** → replace with order-response accounting (F1).
   Deletes a 20 s latency tail, 5 retries, and the corruption source. ~40 lines → ~10.
2. **Triple entry-guard overlap in the sniper path** — patient-entry loop + FAK retry
   loop + price-drift ceiling are three band-checks around one decision. Collapse into
   ONE entry loop with ONE band [−4%, +2%] and one retry budget (F2). Also deletes the
   adverse-selection bug as a side effect.
3. **Sizing pipeline** — `size_usdc` is currently mutated by up to 8 sequential
   clamps/floors spread over 300 lines (kelly/fixed → depth cap → unified risk cap →
   profit-protection cap → exchange-min floor → tradeable clamp → on-demand fund →
   re-clamp → book cap → re-floor → risk-gate clamp). The double `exchange_min` floor
   can resurrect a stake that a cap deliberately reduced. Extract one pure
   `compute_stake(signal, user, balances, book) -> Decision` function with ordered
   rules and unit tests; behavior-preserving except the floor-after-cap bug.
4. **Drawdown gate (Gate 3) + equity HWM machinery** — highest incident density in the
   project (phantom equity, 7 no-op manual unlocks, monitor race re-inflating HWM,
   risk_override half-bypass). The daily-loss gate (Gate 4, DB-ledger based) covers
   the same user story without Data-API equity. Proposal: retire Gate 3 + `equity_hwm`
   entirely; keep exposure caps (Gates 1–2) + daily loss (Gate 4). One-way door —
   do it AFTER the ledger is trustworthy (item 1), since Gate 4 reads the ledger.
5. **Dual sniper feed** — RTDS WS (primary) + 3 s Data-API poller (redundant copy of
   the same funnel incl. dedup). Keep WS; demote the poller to a 30–60 s watchdog that
   only alerts (not fires) when WS misses fills, or delete it once the own-bot fan-out
   ships (Phase 3 makes both feeds obsolete).
6. **Legacy onboarding funnel** — BP15 demo-first screens now coexist with BP27
   explicit creation; the `register` callback survives only as a repair path for
   pre-BP27 users (a handful). After the last unregistered user converts, delete the
   L0/L1 gate screens and `register`.
7. **`outcome_index` triple safety net** — entry-time Yes/No fallback (wrong for
   Up/Down), CLOB token-order fallback, and on-chain `detect_outcome_index` repair.
   Make the CLOB token order the ONLY entry-time source, keep `detect_outcome_index`
   as reconcile-time verification, delete the Yes/No string heuristic.
8. **`scripts/` pile** — 20 one-off diagnostics accumulated; move to `scripts/archive/`
   (they're documentation of past incidents, not runtime code).
9. **Accepted crutches to keep (documented, not removed):** `users` ↔ `user_wallets`
   mirror (BP24), Gamma `resolve_outcome_name` tiers, Redis `notify_once` leases —
   each guards a real external-API deficiency and has been stable.

### 28.3 Own-bot plan (the strategic answer) — architecture assessment

The user's plan: replicate the donor's strategy in-house, run it on ALL 5-min crypto
markets (BTC, ETH, SOL, XRP…), enter seconds EARLIER than the donor, and have users
copy OUR bot. Assessment: **this is the only path that fixes the economics** (F3) —
but the build must be sequenced to avoid betting user money on an unproven model.

Key architecture decisions:
- **Signal engine, not wallet-watching.** The strategy is mechanical: in the last
  T seconds of a 5-min window, if spot momentum vs the strike implies P(win) ≥
  threshold and the market still prices it ≤ P − margin, buy. Inputs: exchange spot
  WS (Binance/Coinbase combined), the market's strike & window (CLOB), and the book.
  No dependency on the donor, no RTDS `orders_matched` filtering, no Data-API lag.
- **Internal fan-out beats on-chain copying.** When the signal is OURS, users don't
  need to "copy" anything on-chain: the signal engine calls the existing
  `execute_copy_trade` fan-out directly (same pipeline as sniper mode today, minus
  the WS/poller detection stage). Latency budget collapses from 1.5–3 s to ~0.2 s,
  and every user enters at effectively the same price. A master wallet trading our
  own capital is OPTIONAL (track record / skin in the game), not a dependency.
- **Enter earlier = the actual edge.** Entering 45–90 s before close at 0.70–0.78
  instead of the donor's last-15-s 0.82–0.90 buys margin for fees (1–2%) and price
  impact, and the book is deeper before the final scramble. The model must prove it
  keeps winrate above breakeven at those earlier, cheaper entries.
- **Capacity by sharding, not by size.** 4 assets × 12 windows/hour ≈ 50 tradeable
  markets/hour. With per-signal capacity ~$100–300 (to be confirmed by F4 logging),
  100 users × $15 needs ~10 concurrent signals or user rotation (each user gets every
  N-th signal). Rotation must be fairness-aware (round-robin per user, not random).
- **Fees are a first-class model input.** Every EV calc (entry threshold, stop-loss)
  must subtract the taker fee curve; at 0.80+ entries the fee is ~25–50% of the gross
  edge. The fee-headroom clamp from the 2026-07-21 finding (balance must cover
  order + fee estimate) is part of this.

### 28.4 Roadmap (strict order — each phase de-risks the next)

**Phase 0 — Truth in the ledger (days).** Prereq for everything else; without it we
cannot even measure whether any strategy works.
- 0.1 Order-response fill accounting (F1) — exact cost/shares on every trade.
- 0.2 Fee-aware balance clamp (yesterday's finding: reserve ~3% when clamping to
  balance) + record the paid fee per trade in `copy_trades`.
- 0.3 Book snapshot logging at signal time (F4 capacity measurement).
- 0.4 One-off ledger repair: recompute `size_usdc`/`shares`/`realized_pnl` for the
  109 settled sniper rows from on-chain transfers (script), so user-facing PnL stats
  stop lying retroactively.

**Phase 1 — Stop the bleeding on the existing donor copy (days, parallel with 0).**
- 1.1 Entry band → [donor−4%, donor+2%], patient-entry dip-buying removed (F2).
- 1.2 Sniper sizing → fraction-of-bankroll (default ~10%, min $5) instead of donor
  mirror; $200 soft-minimum message at sniper opt-in and on low balance (F5).
- 1.3 Honest expectation-setting for the two sniper users: this strategy at current
  prices is ~breakeven-to-negative after fees (F3). Do NOT open donor copying to
  50–100 users — capacity (F4) and economics (F3) both forbid it. The wide rollout
  waits for the own bot.

**Phase 2 — Signal engine in shadow mode (1–2 weeks).**
- 2.1 Build the momentum/strike model service (spot WS + CLOB meta + book reader).
- 2.2 Run it in shadow: log would-be entries (time, price, size cap) on ALL 4+ assets,
  settle them virtually against actual resolutions. Zero user money.
- 2.3 Success gate: ≥2 weeks shadow, ≥300 virtual trades, net ROI after fees > +3%,
  AND beats a same-period virtual copy of the donor. If it fails — iterate on the
  model, not on the pipeline.

**Phase 3 — Master bot live, small (1–2 weeks).**
- 3.1 Trade house capital ($200–500) via the existing pipeline; users still on donor.
- 3.2 Compare live fills vs shadow assumptions (slippage, fee, partial fills).
- 3.3 Success gate: live ROI after fees > 0 over ≥150 trades, max daily drawdown
  within model expectations.

**Phase 4 — Users switch to the internal signal (weeks).**
- 4.1 Fan-out from the signal engine (direct, no WS detection); per-user
  fraction-of-bankroll sizing; round-robin sharding across concurrent markets once
  per-signal demand approaches measured capacity.
- 4.2 Migrate the two sniper users first, then staged rollout (10 → 30 → all),
  watching aggregate price impact per signal.
- 4.3 Donor copying demoted to legacy; RTDS/poller feeds retired (de-crutch item 5).

**Phase 5 — Cleanup & scale (ongoing).**
- 5.1 De-crutch items 3, 4, 6, 7, 8 (sizing function, Gate 3 retirement, legacy
  onboarding, outcome_index, scripts archive).
- 5.2 Add assets/windows as capacity data justifies; consider maker-side entries
  (resting bids 60–120 s out) to earn spread instead of paying it — the single
  biggest potential edge improvement, but only after Phase 3 proves the base model.

### 28.5 Explicit answers to the four questions

1. **Chaotic sums** — half accounting artifact (F1, fix in Phase 0), half real
   (partial fills + balance clamp; Phase 1 sizing makes the real part deterministic).
2. **Missed volatile entries** — mostly the system protecting users (F2: the chased
   fills lose 24.5% ROI); the miss-rate is solved structurally by entering earlier
   with our own signal (Phase 2–4), not by loosening the band for donor copying.
3. **"PnL counted from the donor's entry"** — close: PnL is counted from a corrupted
   cost basis recorded via Data-API `current_value` (F1). Confirmed on 64/109 rows.
   Real profitability of the sniper is worse than the notifications suggested:
   ledger says −$73 total, and even that is distorted.
4. **Is the book always thin?** At the moment we trade (post-donor-sweep, last 30 s) —
   yes, tens of dollars within ±2%. Between signals MMs requote quickly. Capacity for
   more users comes from more markets and sharding, not from deeper single books;
   Phase 0.3 logging turns this from estimate into measurement.

## Blueprint 29: Ledger truth and sniper leak stop (2026-07-21)

Implements BP28 phases 0 and 1. The money-path changes require a **worker + beat
image rebuild**. Migration 020 and the historical repair remain operator actions.

### 29.1 Order-response accounting and fees

Installed `py-clob-client-v2==1.0.1` and the CLOB V2 OpenAPI were checked before
implementation. `create_and_post_market_order` returns the raw `SendOrderResponse`
dict. For a BUY, matched `makingAmount` is pUSD spent and `takingAmount` is outcome
shares received. Installed 1.0.1 returns human-unit decimal strings in production
(`"9.999999"`), while the OpenAPI examples show fixed-6 integer strings; the parser
supports both representations and only trusts `status="matched"`. It computes
`fill_price = making / taking` and uses the existing 5%/90% none/partial/full
thresholds. Non-dict SDK responses are serialized without dropping fields, and an
explicit `success=false` raises instead of being treated as a placed order.

The old Data-API `_confirm_fill` is fallback-only when either response amount is
missing/zero. Its cost is now `shares × avg_price`; `current_value` is never used.
Migration `020_trade_fee.sql` adds nullable `copy_trades.fee_usdc`. The documented
V2 response has no actual-fee field, so current orders log `order_fee_estimate` and
leave the column NULL; recognized future fee fields are stored with a pre-migration
retry that omits only `fee_usdc`.

Every balance-bound stake now reserves `sniper_fee_headroom_pct=0.03`, including
default (non-sniper) copying. This prevents a max-balance order from failing CLOB's
separate fee-coverage check.

### 29.2 Sniper entry, sizing, and capacity logs

The patient-entry loop and nested FAK retry were replaced by one deadline-bound loop.
It enters only when best ask is inside the inclusive band
`[donor × (1-sniper_max_below_pct), donor × (1+sniper_slippage_pct)]` and below
`sniper_max_entry_price`. Asks below the band are treated as a falling market and
waited out, not bought. A vanished FAK ask consumes the same
`sniper_fak_max_retries` budget and returns to the same book loop. Timeout logs
`sniper_skip reason=price_out_of_band` plus `direction=below|above|no_book`.

Sniper stakes no longer mirror donor dollars. `calculate_sniper_stake` starts from
`sniper_stake_frac=0.10` of free pUSD, applies the $5 exchange floor, then caps by
`sniper_stake_cap_usdc=50`, ask depth inside the donor band, and fee-adjusted free
balance. Donor size remains in the signal notification only. Users below
`sniper_recommended_balance_usdc=200` get a non-blocking Russian warning at most
once per 24 hours through `notify_once`.

`fire_sniper_signal` now emits one `sniper_book_snapshot` per claimed signal:
best bid/ask, spread, ask dollar depth at +2/+5/+10%, and bid dollar depth at
-2/-5/-10%. Snapshot failures are swallowed and never block fan-out.

New settings:
- `sniper_max_below_pct=0.04`
- `sniper_fee_headroom_pct=0.03`
- `trade_ledger_update_attempts=3`
- `trade_ledger_update_retry_sec=0.2`
- `sniper_stake_frac=0.10`
- `sniper_stake_cap_usdc=50`
- `sniper_recommended_balance_usdc=200`

### 29.3 Historical repair

`scripts/repair_sniper_ledger.py` reads all settled sniper rows and authenticates a
CLOB V2 client with the signing wallet recorded on each trade. It queries
`get_trades(TradeParams(asset_id=token_id))`, groups BUY fills by taker order, and
matches only an unambiguous token/order/time group. It recomputes cost as
`Σ(size × price)`, shares as `Σ(size)`, then PnL as `shares-cost` for wins or
`-cost` for losses. Missing or ambiguous history is reported and skipped.

Default mode is read-only and prints every `было -> станет` change. `--apply` writes
only `size_usdc`, `shares`, and `realized_pnl`; it must be run manually after review.
Operator sequence: rebuild worker/beat, apply migration 020, run the repair dry-run
again, then explicitly decide whether to run `--apply`.

## Blueprint 30: Own 5-minute crypto signal engine, shadow phase (2026-07-21)

Implements BP28 Phase 2 as a separate `shadow` compose service. It reads Gamma,
Chainlink RTDS, CLOB books, and on-chain resolution state; its only write is to the
new `shadow_trades` table. It does not import or call `place_order`, `sell_position`,
`redeem_winnings`, copy fan-out, or any other money-moving path. Worker, beat, and
API behavior are unchanged.

### 30.1 Pre-implementation research

**Market discovery.** The four recurring series use deterministic event/market
slugs:

```
{asset}-updown-5m-{window_start_unix}
```

where `asset` is `btc|eth|sol|xrp` and `window_start_unix` is UTC Unix time
floored to 300 seconds. A direct
`GET https://gamma-api.polymarket.com/events?slug=btc-updown-5m-1784651400`
returned exactly one event for the live 12:30–12:35 ET window; the same request
for `window_start+300` returned the next event before that window began. This
worked for all four assets. Discovery therefore fetches current and next slugs
every 30 seconds, takes the nested market's `conditionId`, `clobTokenIds`, and
`outcomes`, and validates `eventStartTime`/`endDate` against the expected
five-minute boundaries. It then validates `ao`, canonical token/outcome order,
and fee details against `GET /clob-markets/{conditionId}`. It does not depend on
broad Gamma search ordering or Gamma's misleading creation-time `startDate`.

**Resolution price.** Live Gamma descriptions for BTC, ETH, SOL, and XRP all say
that Up wins when the Chainlink Data Streams USD price at the end of the named
range is greater than or equal to its price at the beginning; otherwise Down
wins. Their `resolutionSource` values are respectively
`data.chain.link/streams/btc-usd`, `eth-usd`, `sol-usd`, and `xrp-usd`, and the
description explicitly says not to use another spot source. The engine therefore
uses Polymarket RTDS `crypto_prices_chainlink` (`btc/usd`, `eth/usd`, `sol/usd`,
`xrp/usd`), not Binance or Pyth. RTDS sends a roughly two-minute subscription
snapshot under topic `crypto_prices`/type `subscribe`, then Chainlink updates;
both envelope forms are accepted. A restart always skips the current window even
if the snapshot contains its opening tick. The next window records its first
Chainlink tick as S0.

**Taker fee.** The official CLOB V2 fee documentation defines
`fee = shares × rate × (price × (1-price))^exponent`, rounded to five decimal
places. Live Gamma market metadata for every tested 5-minute asset had
`feesEnabled=true`, `feeType=crypto_fees_v2`, and
`feeSchedule={rate: 0.07, exponent: 1, takerOnly: true}`; canonical CLOB V2
metadata independently returned `fd={r: 0.07, e: 1, to: true}`. The older
`makerBaseFee`/`takerBaseFee=1000` fields are not sufficient to reproduce this
curve. `core/shadow_model.py::fee_usdc` implements the documented formula and
the engine uses canonical CLOB `fd`, with Gamma's per-market schedule and the
verified 0.07/1 settings only as fallbacks. At an 0.80 entry this is 1.4% of cash
cost, consistent with the 1.2–2.0% production estimates.

### 30.2 Architecture and model

- `worker/shadow_engine.py` runs five independent asyncio loops: Chainlink RTDS
  with PING/liveness/reconnect backoff, deterministic Gamma discovery, CLOB book
  evaluation, on-chain virtual settlement, and the optional daily admin digest.
  External and missing-table failures are logged and retried without terminating
  the process.
- Spot is sampled at fixed one-second intervals. EWMA variance is calculated from
  squared log returns per second, so sigma has the units required by
  `P(Up)=Φ(log(S/S0)/(sigma*sqrt(tau)))`. The default alpha 0.003 has an effective
  10–15 minute memory. A minimum sample count and sigma floor guard cold/flat data.
- In the final 20–120 seconds, the side with model probability at least 0.5 is
  evaluated. `walk_order_book` consumes asks cheapest-first for the configured
  virtual stake, records partial depth, and computes shares, effective price, and
  the fee at every level. Entry requires
  `model_p - effective_price - fee_per_share >= shadow_min_edge` and effective
  price at or below the ceiling. A unique DB index plus a pre-insert lookup allows
  at most one virtual entry per condition and entry-time variant.
- Every completed asset/window emits one `shadow_window` log with the best
  observed edge, model probability, ask, entry flag, and skip reason. Entries and
  settlements have their own structured records.
- Open rows are settled from ConditionalTokens
  `payoutDenominator`/`payoutNumerators`; the token's real outcome index is derived
  with `detect_outcome_index`. Net PnL is `shares-stake-fee` on a win and
  `-stake-fee` on a loss. Rows still unresolved after the configurable 24-hour
  limit become void with zero PnL.
- `scripts/shadow_report.py` reports count, winrate, gross/net PnL and ROI, asset,
  edge, and entry-time buckets for the canonical `full` variant. It also compares
  all entry-time variants overall and by strike distance. The same-period BTC
  donor benchmark is resolved from `trade_signals` on-chain and reported at donor
  prices with the same fee curve.

Migrations `021_shadow_trades.sql`, `022_shadow_variants.sql`,
`023_shadow_stress_sigma.sql`, and `024_shadow_maker.sql` own the isolated
ledger, variant uniqueness, BP30.2 diagnostics, and BP30.3 maker lifecycle.
Apply them manually before starting the service.
The service is started only with:

```
docker compose up -d --build shadow
```

### 30.3 Settings and Phase 2 gate

Primary strategy settings: `shadow_assets`, `shadow_entry_min_sec`,
`shadow_entry_max_sec`, `shadow_min_edge`, `shadow_max_price`, and
`shadow_stake_usdc`. Feed/model settings are grouped under `shadow_rtds_*`,
`shadow_ewma_alpha`, `shadow_vol_sample_sec`, `shadow_vol_min_samples`,
`shadow_sigma_floor`, and `shadow_model_z_cap`. Fee, discovery, retry,
resolution, report-bucket, and digest timing also have `shadow_*` settings; there
are no strategy magic numbers in the service. `shadow_digest_telegram_ids=[]`
disables Telegram delivery by default.

Real-time signals: `shadow_signal_telegram_ids` (default `[]`) lists chat ids
that receive a Telegram message the moment a virtual entry is recorded (asset,
side, fill price, stake, model probability, edge, window end) and again at
settlement (win/loss with PnL and ROI, or void after 24 h). Entry/settlement
sends run as fire-and-forget tasks so the 1-second evaluation loop never blocks
on Telegram.

Do not proceed to live trading until the unchanged BP28 gate is met: at least two
weeks of shadow data, at least 300 settled virtual trades, net ROI after fees
above +3%, and better same-period performance than the donor benchmark. Failure
means recalibrating the model in shadow, not enabling the money path.

### 30.4 BP30.1 parallel entry-time variants

Each five-minute condition can now record one independent virtual trade per
entry-time variant. `full` remains the canonical policy and keeps the unchanged
inclusive `[shadow_entry_min_sec, shadow_entry_max_sec]` range. Adjacent pairs in
`shadow_variant_edges_sec` define research buckets; the default edges
`[20, 30, 60, 90, 120]` produce `t20-30`, `t30-60`, `t60-90`, and `t90-120`.
Every variant enters on the first eligible one-second tick whose edge clears the
same threshold. Boundary seconds are inclusive for both adjacent buckets.

The probability, side, and CLOB walk are computed once per asset/tick and shared
by all active variants. Deduplication is by `(condition_id, variant)`, and each
row settles independently. Only `full` emits Telegram entry/settlement messages
and contributes to the daily digest, the main report sections, donor comparison,
and the unchanged BP28 Phase 2 gate (≥300 settled trades and net ROI above +3%).
Research variants are silent and appear only in the report's comparative section.

### 30.5 BP30.2 stressed volatility and divergence ceiling

The first 283 settled `full` trades exposed two systematic leaks. Near-strike
entries below 3 bp during volatile European-morning periods used a slow EWMA
that understated current volatility: model probabilities of 70–75% produced
only 38–44% wins. Entries where model probability exceeded execution price by
more than 10–12 points were also usually wrong relative to the market.

Each asset now maintains the original slow EWMA plus a fast EWMA with a
configurable 0.02 alpha and 30-sample warm-up. The slow estimate must still pass
its original warm-up gate. Once the fast estimate is warm, the model uses the
maximum of both sigmas, so short volatility spikes increase uncertainty without
discarding the stable baseline.

After the single CLOB walk per asset/tick, entry is rejected with
`model_divergence_ceiling` when `model_p - effective_price` exceeds the
configurable 0.12 ceiling. Existing edge, fee, price, variants, signals,
settlement, and digest behavior remains unchanged. New rows store the warmed
fast sigma and `q_cal`, the model probability shrunk halfway toward execution
price by default. `q_cal` is diagnostic only: it is logged to
`shadow_trades` and does not gate entries or choose sides. The report adds a
`full`-variant breakdown by model-price divergence.

### 30.6 BP30.3 virtual maker entries

Canonical CLOB metadata marks the crypto fee schedule as taker-only. Since the
taker fee and ask spread erased the observed edge, the shadow service now runs
one parallel virtual maker attempt per condition. It improves the best bid by
one Gamma `orderPriceMinTickSize` tick without crossing the ask, requires the
configured maker edge, divergence, and price gates, and charges no fee. The
order remains entirely virtual; no money-moving path or real order API is used.

Pending maker orders live only in memory. On each later tick, an order fills at
its bid only when the token's best ask is at or below that bid, cancels when its
current model edge falls strictly below `shadow_maker_cancel_edge`, or expires
at the window end. The ask-touch rule deliberately ignores market sells that
may have traded into the bid between one-second snapshots, so reported fill
rate is a conservative lower bound. A model-side book is reused when possible;
an opposite-side pending order adds at most one CLOB request per asset/tick.

Maker outcomes use `variant='maker'`: fills enter settlement as `open` with
zero fee, while cancellations and expirations are stored as `unfilled`.
Placement diagnostics remain fixed at order creation in `placed_at`, and
`note` records `filled`, `cancelled_edge_lost`, or `expired`. The report shows
maker placement, fill, cancellation, expiry, settled performance, and the
same-period `full` taker comparison. Maker entries and settlements never send
Telegram signals and do not affect the BP28 Phase 2 gate.

## Blueprint 30.4: hard signal filter for shadow signals (2026-07-24)

Analysis of 682 settled full-variant trades showed every execution slice losing
(taker −4.6%, maker −5.7%, all entry windows, all hours), while the only
positive-expectancy pockets were edge ≥ 0.07 (+2.8…+7.1% ROI) combined with spot
≥ 3 bp from the strike (near-strike entries are toxic: WR ~60% at price ~0.63).
`passes_signal_filter` in `core/shadow_model.py` encodes this
(`shadow_filter_min_edge=0.07`, `shadow_filter_min_strike_bp=3.0`). Collection is
deliberately NOT filtered — all variants keep accruing rows for research. The
filter gates only: (1) real-time Telegram entry/settlement signals (full variant
only; settlement re-evaluates the filter from the stored `edge`/`spot`/`open_price`
via `_row_is_signal`), and (2) the "Фильтр BP30.4" section in `shadow_report`,
which prints ПРОШЛИ/ОТСЕЯНЫ side by side for forward validation. Backtest on
collected data: passed n=184 WR 81.0% net −0.6% vs rejected −6.8%; BP30.2-era
passed n=102 WR 83.3% net +0.9% vs rejected −6.0%. `shadow_virtual_entry` logs
now carry `signal=true/false`.

## Blueprint 32: replayable onboarding for partner review (2026-07-27)

Partners need to walk through the BP27 wallet-creation onboarding repeatedly
without minting a new wallet each time. `/onboarding` (available to any user in
auto-copy mode) re-opens Screen A. The `onb_create_wallet` idempotent re-entry
now *replays* the staged UX — stage 1 (7 s) → stage 2 (5 s) → wallet card —
with zero DB writes and no relayer calls when `wallet_address` and
`wallet_registered` are already set. First-ever run still creates the wallet
for real; a user stuck between key generation and registration resumes the real
registration (no new keys). Repeat runs can never create a second wallet.

## Blueprint 31: live-position counting for the max_open_positions guard (2026-07-23)

Data-API `/positions` reports resolved leftovers forever: losing tokens are never
redeemed, so they sit with `shares>0`, `current_value=0`, `redeemable=true`. Every
lost 5-min sniper trade therefore consumed a `max_open_positions` slot permanently;
both real traders accumulated 20+ dead rows and regular whale copying was silently
skipped with `skip_max_positions` (after they switched off Kelly — before that the
`zero_edge` gate fired first and masked it). Fix in `execute_copy_trade`: the guard
now counts only economically live positions (`shares>0` AND not `redeemable` AND
`current_value >= $0.01`). Redeemable winners are also excluded — their capital is
freed by the redemption task within minutes. The `already_in_market` guard is
unchanged (resolved markets emit no new BUY signals).

## Blueprint 33: BTC-bot real-money pilot — standalone product (PLAN, 2026-07-30)

Status: **Phase A implemented 2026-07-30** — see «33.9 Implementation» at the
end of this blueprint. Gate that unlocked this plan: 302
filter-passing trades since BP30.4 deploy, WR 87.7%, net +3.9% ROI (+$178),
survived one bad day (28.07 −5.4% vs unfiltered −8.3%); all three Europe day
segments positive; maker permanently rejected (−2.8% under filter vs +3.9%
taker).

### 33.1 Product shape

A **separate Telegram bot** (own `TELEGRAM_BOT_TOKEN`, own product name) that
auto-trades our own BP30 signal engine (BTC 5-min up/down, BP30.4 filter,
taker-only) with real user money. It is NOT another mode inside the copytrade
bot: different audience, different risk story, different lifecycle (positions
live 20–120 s, resolution within minutes). Sharing one bot UI would force every
menu into mode-branches and double regression surface.

What IS shared: the repo, `core/*` (wallets, CLOB client, order-fill
accounting, relayer redemption, shadow model), Supabase instance, deploy
pipeline. The new bot is one more compose service reading the same signal
stream.

### 33.2 Wallet decision: per-user wallets (BP27 flow), NOT one house account

Considered two options:

**(A) Per-user custodial wallet — CHOSEN.** Exactly the BP27 pipeline: generate
EOA → deploy deposit proxy → register on Polymarket → persist CLOB creds. Users
deposit to their own address; every fill, fee and redemption lands on their own
ledger; withdrawals are per-user; a partner can verify their wallet on
Polygonscan. All of this code exists and is battle-tested (BP25/27/29 fixes).

**(B) One "house" Polymarket account trading pooled funds — REJECTED.** One
fast order instead of N is tempting (thin books), but: (1) commingled funds —
internal shares ledger becomes the source of truth for user money, any bug is
a direct liability; (2) deposits/withdrawals need attribution and queuing
against open positions; (3) one banned/limited account halts every user; (4)
per-user PnL must be synthesized pro-rata, which breaks the moment stakes
change mid-window; (5) our sniper history shows partial fills — splitting a
partial fill fairly across a pool is exactly the accounting swamp BP29 drained.
Pooling buys ~1 s of latency and costs custody integrity. No.

Consequence of (A): execution fans out N CLOB orders per signal. Mitigations in
33.5 (parallel dispatch, depth cap, user cap for the pilot).

### 33.3 Data model (new tables, no reuse of copytrade rows)

- `crypto_users` — telegram_id, username, wallet columns mirroring `users`
  (EOA, enc key, deposit wallet, CLOB creds, `wallet_registered`), plus
  `stake_usdc` (default 5), `trading_on` (default false), `daily_loss_limit_usdc`
  (default = 3 × stake), `created_at`. Deliberately NOT the copytrade `users`
  table: same telegram_id may exist in both products with different wallets and
  settings; mixing them recreates the BP24 mirror-drift class of bugs.
- `crypto_trades` — one row per user per market: condition_id, token_id, side,
  signal price/edge/model_p, requested/filled USDC, shares, fill price, fee
  (from order response — BP29 `extract_buy_fill` is mandatory from day one),
  status (`open/win/loss/void/skipped`), skip_reason, resolved_at, pnl_usdc.
  Unique index `(user_id, condition_id)` = idempotency guard.
- Shadow tables stay untouched — the shadow engine keeps running unfiltered as
  the control group (33.7).

### 33.4 Signal path

Shadow engine stays the single brain. On a **full-variant entry that passes
`passes_signal_filter`** it publishes an execution signal (Redis pub/sub or a
`crypto_signals` row + NOTIFY; decide at build time — Redis already in
compose). Payload: condition_id, token_id, side, signal price, book snapshot
timestamp, window_end. The executor consumes; the shadow row itself remains a
pure simulation so live results never contaminate research data.

Hard constraint: signal fires 20–120 s before window close; orders must land
within ~2–3 s. Executor therefore holds a warm CLOB client per user (creds
cached, no per-order Supabase round-trip) and dispatches users concurrently
(`asyncio.gather`), not sequentially.

### 33.5 Execution rules (pilot)

- Taker FAK buy at ask, per-user stake = `min(stake_usdc, free_pusd × 0.97)`
  (3% fee headroom — BP29 lesson).
- Price guard: skip if executable price > signal price + 1.5% or > 0.95
  absolute. Never chase; a skipped trade is logged with skip_reason (the BP28
  audit proved dip-buying below signal price is adverse selection — same rule
  here: band, not discount-hunting).
- Depth cap: spend at most 25% of visible band depth per user; dispatch users
  in random order per signal so nobody is systematically last.
- One position per market per user (unique index), no averaging, no exits
  before resolution — strategy holds to resolution by design; stop-losses in
  5-min binaries were already analyzed and rejected (whipsaw + double costs).
- Settlement: reuse the relayer resolution detector + redeem path; PnL from
  true on-chain payout against the order-response ledger. Void after 24 h like
  shadow.
- Risk rails (all pilot-simple): `trading_on` toggle per user; global
  `crypto_kill_switch` env/admin command checked before every dispatch;
  per-user daily loss limit (skip new entries after breach, message the user);
  max 1 concurrent open position (windows are 5 min — overlap is minimal
  anyway); min recommended balance $50, hard floor $10.

### 33.6 Bot UX (deliberately minimal, but complete)

- `/start` → BP27-style staged wallet creation (reuse the exact flow partners
  already reviewed, including `/onboarding` replay) → wallet card.
- Main menu (single screen, buttons): `💰 Баланс` (pUSD + deposit address),
  `▶️/⏸ Торговля вкл/выкл`, `⚙️ Ставка` (presets $5/$10/$15 — no free input in
  the pilot), `📊 Сегодня` (n, WR, net PnL today + since start), `💸 Вывод`
  (reuse copytrade withdraw path), `❓ Как это работает`.
- Notifications: entry (`🎯 BTC Up, вход 0.84, ставка $10`), settlement
  (`✅ +$1.90 (+22%)` — same math users already sanity-checked), daily digest
  21:00 UTC, skip notices only for balance/limit reasons (not for price-guard
  skips — too noisy).
- Copy in plain language: this bot trades OUR strategy, not a copied whale;
  expected profile "many small wins, occasional −100% losses on a trade";
  показываем net после комиссий.

### 33.7 Pilot protocol and gates

- Phase A (build): bot skeleton + wallet flow + executor behind whitelist.
  Whitelist = the two internal users (504677064, 879714159), stake $5.
- Phase B (1 week live): compare EVERY live fill against its shadow row —
  live_fill_price − sim_fill_price is the honest slippage measure; shadow
  stays the control. Daily report section: live vs shadow ROI, fill rate,
  skip reasons.
- Gate to Phase C (partners, stake ≤$15, ~10 users): live ROI within 2 pp of
  shadow over ≥100 live trades AND median entry latency ≤3 s AND zero ledger
  mismatches (order-response totals vs on-chain redemption).
- Capacity ceiling from BP28 book study still applies: tens of dollars of
  depth near close. 10 users × $15 = $150 per signal is already at the edge —
  scaling beyond the pilot needs the depth-cap telemetry from Phase B before
  any promises to partners.

### 33.8 Known pitfalls (carried from earlier blueprints, must not regress)

1. Fees: clamp stake by fee headroom or CLOB rejects at full balance (BP29).
2. Ledger truth only from order responses, never Data-API `current_value`
   (BP29).
3. Data-API resolved leftovers are immortal — never count them as open
   positions (BP31).
4. Relayer neg-risk path irrelevant here (standalone binaries) but redemption
   retry/backoff from BP25 applies.
5. Live slippage will be worse than `walk_order_book` on a snapshot — that is
   exactly what Phase B measures; do not promise shadow ROI to partners.
6. Filter thresholds (`0.07` / `3 bp`) are config, not code — retuning must be
   a config change with a CURSOR.md note, not a silent edit.

### 33.9 Implementation (Phase A, 2026-07-30)

Shipped as a new top-level package `cryptobot/` plus one compose service.
Copytrade code paths untouched; the only shared-file edits are the config
block, the Redis publish hook in the shadow engine, and compose.

**Data (migration `025_crypto_bot.sql`).** `crypto_users` (telegram_id, BP27
wallet fields: EOA + key enc + deposit wallet + CLOB creds, `trading_on`,
`stake_usdc`, `eoa_stable_baseline` for deposit detection) and `crypto_trades`
(signal context + exact fill facts from the order response + lifecycle
open→win/loss/void, `skipped` rows for attempted-but-failed orders). Unique
index `(user_id, condition_id)` = one position per user per market, the
executor's idempotency backstop.

**Signal path.** `worker/shadow_engine.py` now publishes every filter-passing
`full` entry to Redis pub/sub channel `crypto_signals` (fire-and-forget; no
subscribers → message vanishes, no stale queue). Payload: condition/token ids,
side, sim fill price, best ask, tick size, fee rate/exponent, window_end,
model_p, edge, published_at.

**Service (`cryptobot/main.py`, compose `cryptobot`).** One process runs both
the PTB bot (manual `initialize/start_polling/start`, so the loop stays free)
and `CryptoExecutor` with three loops:

- `signal_loop` — subscribes to the channel; guards: `crypto_trading_enabled`
  kill switch, signal age ≤4 s, ≥8 s left in window, ask ≤0.95. Loads
  whitelisted active traders, shuffles (fair book access), places FAK BUYs in
  parallel threads. Per user: daily-loss gate (−3× stake realized today, UTC),
  stake = preset capped by free pUSD − 3% fee headroom (skip below $5 exchange
  min, throttled Telegram warning), then `core.clob.place_order` (1.5%
  slippage band) and `extract_buy_fill` for exact cost/shares/fee; fee falls
  back to the BP30 formula when the response omits it. No-fill and order
  errors are recorded as `skipped` rows.
- `resolution_loop` — every 20 s settles finished windows via
  `is_condition_resolved` / `detect_outcome_index` / `get_payout_numerator`;
  winners redeemed with `redeem_winnings` (relayer, idempotent — row stays
  open and retries if redemption fails); void after 24 h. PnL = shares − cost
  − fee. Entry/settlement notifications match the shadow-signal format.
- `funding_loop` — every 90 s replicates the copytrade deposit monitor for
  `crypto_users`: detects EOA USDC arrivals against `eoa_stable_baseline`,
  auto-sweeps to pUSD via `fund_deposit_wallet` when POL gas is present
  (otherwise tells the user to send ~0.1 POL), self-heals stranded USDC.e on
  the deposit wallet.

**Bot UX (`cryptobot/bot.py`).** `/start` → closed-pilot gate (whitelist) →
BP27-style explicit wallet creation (staged messages, honest error + retry
resumes from where it stopped, double-tap guard) → main menu card: deposit
address, pUSD balance, trading on/off, stake, today's PnL. Buttons: toggle
trading (refuses to enable below $5 balance), Пополнить (address +
CopyTextButton + Polygonscan + custody explainer + POL note), Ставка (presets
from `crypto_stake_presets`), Статистика (today/all-time), Как торгует бот
(honest strategy explainer), Вывод (manual-by-support in the pilot — the
copytrade auto-withdraw path needs EOA gas orchestration, deferred).

**Config (all env-overridable).** `crypto_bot_enabled` (default False — the
service idles until enabled), `crypto_bot_token`, `crypto_signals_channel`,
`crypto_whitelist_telegram_ids` (empty = closed), `crypto_trading_enabled`
kill switch, `crypto_stake_presets` [5,10,15], `crypto_max_entry_price` 0.95,
`crypto_entry_slippage_pct` 0.015, `crypto_signal_max_age_sec` 4,
`crypto_daily_loss_mult` 3, `crypto_fee_headroom_pct` 0.03, resolution/funding
poll cadences.

**Tests.** `tests/test_cryptobot_logic.py` covers the pure gates
(`pilot_stake`, `signal_is_fresh`, `entry_price_ok`, `daily_loss_exceeded`).
DB/бот/executor I/O paths are thin wrappers over battle-tested `core/*` calls.

**Deploy.** Apply migration 025 in Supabase; add to `.env`:
`CRYPTO_BOT_ENABLED=true`, `CRYPTO_BOT_TOKEN=<new BotFather token>`,
`CRYPTO_WHITELIST_TELEGRAM_IDS=[504677064,879714159]`; then
`docker compose up -d --build cryptobot shadow` (shadow rebuild picks up the
publisher). Kill switch without redeploy: `CRYPTO_TRADING_ENABLED=false` +
`docker compose up -d cryptobot`.

## Blueprint 34: onboarding polish + executor FAK re-quote (implemented, 2026-07-31)

Context: first live night of BP33 (2026-07-30 18:37 → 07-31 09:23 UTC) plus a
partner walking the copytrade onboarding.

### 34.1 First-night executor audit (facts, no code change needed for these)

Signal delivery is airtight: 33 filter-passing full signals in shadow_trades,
33 matching crypto_trades rows — zero lost between Redis publish and executor.
27 filled (25 win / 2 loss, net +$46 on $15 stakes, WR 92.6%), 6 skipped.

### 34.2 Executor: one re-quote retry on FAK kill — THE code change

All 6 skips share one failure: CLOB 400 `no orders found to match with FAK
order`. Between the shadow book snapshot and our order (~1 s) the ask moved
above our worst-price band (signal ask × 1.015), so the FAK found nothing and
was killed. Shadow counted those 6 as simulated trades (5 would-be wins, 1
loss) — that is exactly why the copytrade bot "has more trades" than the
crypto bot. 18% signal loss is worth fixing; expected-value of the missed six
was positive.

Worse: `_place_trade` records the failure as a `skipped` row in crypto_trades,
and the `(user_id, condition_id)` unique index then blocks any retry — one
unlucky FAK permanently forfeits the signal.

Fix (in `cryptobot/executor.py::_place_trade`):
1. Catch specifically the FAK no-match failure (match "no orders found to
   match" in the exception text); all other order errors keep the current
   record-skip-and-stop path.
2. On no-match: re-fetch the live top of book (`core.polymarket.get_order_book`
   on the signal's token_id), take fresh best_ask, and re-check the same entry
   guards: best_ask ≤ `crypto_max_entry_price`, window_end − now ≥ 8 s
   (MIN_TIME_LEFT_SEC), and best_ask not worse than signal ask by more than a
   re-quote ceiling — new setting `crypto_requote_max_worse_pct` (default
   0.03: re-quote may pay up to 3% above the signal price, beyond that the
   edge thesis is broken).
3. Re-place the same FAK once with price = fresh best_ask (same slippage band).
   ONE retry only — no loops; 5-min books move too fast for more.
4. Only after the retry fails (or guards reject it) insert the `skipped` row
   with reason `no_fill_after_requote` / original error.
Add `requoted` boolean column? NO — keep schema; encode via skip_reason and a
log line (`crypto_requote_placed`) — pilot telemetry lives in logs.

Tests: pure guard for the re-quote decision goes into `cryptobot/logic.py`
(e.g. `requote_price_ok(signal_ask, fresh_ask, max_worse_pct, max_entry)`) +
unit tests in tests/test_cryptobot_logic.py.

### 34.3 Copytrade bot: /onboarding lands wrong + «Это безопасно?» removal

Facts: BP32 code in the repo is CORRECT — `/onboarding` renders Screen A
(`_onb_create_text` + create button), is NOT in MAIN_COMMANDS (hidden, known
only to those told about it), and the create callback replays staged messages
idempotently for registered wallets. The partner's `/onboarding` landed on the
OLD autotrade-gate screen because the api container on the VPS is 3 days old —
BP32/BP27 fixes were committed later and `docker compose up -d --build` was
run only for `cryptobot shadow`. Deploy, not code: rebuild `api`.

Code change that IS needed: remove the «🛡 Это безопасно?» button from the L0
welcome keyboard (`_onboarding_kb` in api/routers/telegram.py). Keep the
`onb_trust` callback handler (old messages keep working buttons), just stop
offering it. Nothing else on that keyboard changes.

### 34.4 Deploy

```
cd /home/ubuntu/app && git pull
docker compose up -d --build api cryptobot
```

(shadow untouched by this blueprint; api rebuild also finally ships BP27/32.)

## Blueprint 35: decouple settlement from redemption in the crypto executor (implemented, 2026-08-01)

### 35.1 Incident

Trade crypto_trades#61 (BTC Up, window end 2026-07-31 23:20 UTC, $15 @ 0.87)
stayed `open` for 25+ minutes while the condition was verifiably resolved
on-chain with our outcome winning (payoutNumerators[0]=1 — confirmed from a
second machine with the same code). The user saw the entry notification and
then silence.

Root cause class (exact trigger visible only in server logs —
`is_condition_resolved_failed` vs `crypto_redeem_failed`): the settlement path
in `cryptobot/executor.py::_resolve_trade` is ALL-OR-NOTHING. The row is
settled and the user notified only after `redeem_winnings` succeeds; any
persistent failure earlier in the chain (RPC errors swallowed to
False/0/None by `is_condition_resolved` / `detect_outcome_index` /
`get_payout_numerator`, or relayer redeem flakes) silently re-loops every
20 s with zero user-facing signal. The outcome was KNOWN; only the money move
was stuck — yet the user-visible result was held hostage to it.

### 35.2 First 30 hours of live trading — audit (no defects found)

- 61 filter-passing shadow signals → 61 crypto_trades rows. Zero lost.
- 52 filled: 44 win / 7 loss / 1 stuck-open; WR 86.3%; net +$25.19 on $780
  turnover (3.2% ROI). Same-period shadow: WR 85.2%, ROI 1.0% — live BEATS
  the simulation.
- Slippage (fill − signal price): mean −0.93 c, median 0.00 — execution at or
  better than the shadow snapshot; the BP34 re-quote sometimes catches a
  cheaper ask (one fill 0.76 vs signal 0.89).
- Skips: 9. All 7 raw FAK kills predate the BP34 deploy; after it only 2
  `requote_price_too_worse` (correct refusals — the +3% chase ceiling held).
  Shadow's PnL on the 9 skipped conditions: −$9.72, i.e. the skips saved money.

### 35.3 Fix plan (executor only, no schema change)

1. `_resolve_trade`: once payouts are known, IMMEDIATELY settle the row
   (`win`/`loss`, pnl, resolved_at) and send the user notification. For wins,
   attempt redemption AFTER settling; on success update `redeem_tx`.
   PnL math unchanged (shares − cost − fee; redemption does not change it).
2. Redemption retry sweep: the resolution loop additionally scans
   `status='win' AND redeem_tx IS NULL` rows (bounded: resolved_at within
   the last 7 days) and retries `redeem_winnings` (idempotent). Successful
   retry logs `crypto_redeem_recovered` and fills `redeem_tx`. No user
   notification for the money move — they already got the result.
3. Stuck-resolution watchdog: an `open` row whose window_end is > 15 min old
   logs `crypto_resolution_stuck` (throttled via notify_once, 1 h TTL) and
   sends the user a single reassurance: «Рынок ещё не рассчитан — результат
   придёт автоматически, средства в безопасности». The 24 h void path stays.
4. Keep the swallow-to-False semantics of the core relayer helpers (copytrade
   depends on them); the executor gains visibility purely through the
   watchdog + settle-first ordering.

Tests: pure decision `should_flag_stuck(window_end, now, threshold_min)` in
cryptobot/logic.py + unit tests; the settle-before-redeem reorder is covered
by reading the code path (I/O-thin).

### 34.5 What was actually implemented

* `core/config.py`: new `crypto_requote_max_worse_pct: float = 0.03` in the BP33 block.
* `cryptobot/logic.py`: pure guard `requote_price_ok(signal_ask, fresh_ask,
  max_worse_pct, max_entry_price)` — fresh ask must be valid (>0), ≤ the hard
  entry ceiling, and ≤ signal_ask × (1 + max_worse_pct); parameterized tests in
  tests/test_cryptobot_logic.py cover the exact-3% boundary, the price ceiling,
  and None/0 asks on both sides.
* `cryptobot/executor.py::_place_trade`: when place_order raises with
  "no orders found to match" (case-insensitive) it calls `_requote_once`,
  which re-checks window_end − now ≥ MIN_TIME_LEFT_SEC, fetches the live book
  via `core.polymarket.get_order_book`, runs `requote_price_ok`, and re-places
  the same FAK once at the fresh best ask (same slippage band). Success logs
  `crypto_requote_placed` (user_id, cond, old_ask, new_ask). The skipped row is
  inserted only after the retry fails or a guard rejects; skip_reason is one of
  `requote_window_closing` / `requote_no_book` / `requote_price_ceiling` /
  `requote_price_too_worse` / `no_fill_after_requote` /
  `order_error_after_requote: …`. All other order errors keep the old
  skip-immediately path. No schema change, no `requoted` column.
* `api/routers/telegram.py::_onboarding_kb`: «🛡 Это безопасно?» button removed
  from the L0 welcome keyboard; the `onb_trust` callback handler stays so old
  messages keep working. `/onboarding` verified absent from MAIN_COMMANDS.

### 35.4 What was actually implemented

* `cryptobot/executor.py::_resolve_trade`: settle-first — once payouts are
  known the row is settled (`db.settle_trade` with `redeem_tx=None`) and the
  user notified immediately; for wins `_try_redeem` runs AFTER and on success
  writes the tx via `db.set_redeem_tx`. Redemption failure only logs
  `crypto_redeem_failed` — it no longer blocks settlement or the notification.
  A `no_token_balance` skip stores `redeem_tx='recovered_externally'`.
* `cryptobot/executor.py::_redeem_sweep` (called each resolution cycle):
  scans `db.unredeemed_wins()` (`status='win' AND redeem_tx IS NULL`,
  resolved_at within 7 days), re-detects the outcome index and retries
  `redeem_winnings` (idempotent). Success logs `crypto_redeem_recovered` and
  fills `redeem_tx`; no user notification.
* Watchdog: `_flag_stuck` fires on every open-row exit path that leaves the
  row unsettled (unresolved, index unknown, payouts unavailable) when
  `should_flag_stuck` (pure, `cryptobot/logic.py`, threshold 15 min) holds;
  throttled via `notify_once` (key `crypto-stuck:{trade_id}`, TTL 1 h). Logs
  `crypto_resolution_stuck` + sends the user «⏳ Рынок ещё не рассчитан…».
  The 24 h void path is unchanged.
* `cryptobot/db.py`: new `unredeemed_wins()` and `set_redeem_tx(trade_id, tx)`.
  No schema change. `core/relayer.py` untouched (copytrade keeps the
  swallow-to-False semantics).
* Tests: `TestShouldFlagStuck` (parameterized boundary cases incl. the exact
  15-minute edge and clock skew) in tests/test_cryptobot_logic.py.

### 35.5 Incident #61 postmortem (confirmed after deploy, 2026-07-31 23:59 UTC)

The exact trigger of the 40-minute hang: **relayer API quota** —
`RelayerApiException[status_code=429, 'quota exceeded: 0 units remaining,
resets in 20 seconds']` on `redeem_winnings`. The relayer quota is shared
with the copytrade worker, whose auto-claim burst around midnight UTC (daily
markets resolving en masse) exhausted it. Under the old coupled path this
starved settlement indefinitely; under BP35 the deploy settled #61 within one
cycle (user notified, pnl +$2.10) and `_redeem_sweep` recovered the money a
few cycles later once quota freed (`redeem_tx 0x9ff77c…`). Working exactly as
designed; no further change needed. If 429 bursts ever become chronic, the
lever is a small backoff/jitter in `_redeem_sweep` — not a priority now.

## Blueprint 36: capacity telemetry for multi-account scaling (2026-08-01)

Purpose: collect, during normal pilot trading, the data needed to design the
100-user architecture (native signal sharding, NOT data-api copytrading —
rationale in the 2026-08-01 deep analysis: entries happen up to 2 min before
resolution, so per-signal book depth is the binding constraint; within one
asset windows never overlap).

What we now record on EVERY crypto_trades row (filled AND skipped):

- `depth_best_usdc` / `depth_150bp_usdc` / `depth_300bp_usdc` — dollars
  purchasable from the signal-side asks at the best-ask level, within +1.5%
  (our FAK slippage band) and +3% (the BP34 re-quote ceiling). Computed in the
  shadow engine from the SAME book snapshot the signal was generated from
  (pure `ask_depth_usdc` in core/shadow_model.py) and carried in the Redis
  payload — the executor just persists it.
- `latency_ms` — signal publish → order outcome (fill or error), measured in
  the executor.
- `requoted` — whether the fill came from the BP34 re-quote path (previously
  visible only in logs).

Migration `026_crypto_depth_telemetry.sql` (idempotent ALTERs, no data loss).

What this unlocks after ~1-2 weeks of collection:
1. Depth distribution per band × time-of-day → hard number for "how many
   $15 accounts fit one signal" and the rotation quota for N users.
2. Slippage vs consumed-depth curve (our own $15 already pays >0.1% extra on
   33% of fills) → per-signal stake budget formula.
3. Latency distribution → whether laddered/staggered entries are feasible
   inside the 2-minute pre-resolution window.
4. Requote win/loss quality vs first-attempt fills → keep or tighten the +3%
   chase ceiling.

Existing data already covering other scaling questions: skip_reason taxonomy,
signal_price vs fill_price slippage, created_at time-of-day, relayer 429
events in logs. Remaining known ceilings for 100 users (documented, not yet
instrumented): shared relayer quota (already saw 429 with 2 users at the
midnight copytrade claim burst) and per-signal book depth.

## Blueprint 37: entry-price ceiling 0.95 -> 0.89 (2026-08-03)

Data-driven tuning from the 5-day live review (2026-07-30..08-03, 142 settled
trades). Findings that motivated it:

- Execution is NOT the problem: on matched condition_ids the live bot made
  +$30.40 vs shadow's +$2.23; mean fill was ~1 cent BETTER than the sim
  (FAK top-of-book + BP34 re-quotes catch dips). Trade-count gap vs shadow is
  fully explained by justified skips (mostly `requote_price_too_worse`), and
  shadow LOST $15.15 on the skipped conditions — the chase guard adds value.
- The one systematically losing segment is entries filled at 0.90+: breakeven
  win rate there is ~93%, actual was 88% → -$16.17 all-time and -$29.38 over
  just 08-02..08-03. Every other price bucket is at/above breakeven; the
  profit core is 0.85-0.90 (+$33.70).
- The 08-03 red day itself was model variance, not decay: shadow logged
  -$34.38 on the same signals vs the bot's -$31.08.

Change: `crypto_max_entry_price` 0.95 -> 0.89 in core/config.py (one setting,
enforced at both signal intake `entry_price_ok` and the BP34 re-quote guard).
0.89 not 0.90 because the check is `ask <= ceiling` and 0.90-exactly fills sit
in the losing bucket. Shadow filters unchanged — shadow keeps recording the
0.90+ segment so we can revisit with a bigger sample.

First BP36 telemetry read (83 rows): depth@1.5% median $185 / p25 $87 /
min $2 (median signal fits ~12 x $15 accounts, a quarter only ~5);
latency median 1.37s, max 4.2s — max already brushes the 4s freshness cap;
6 re-quoted fills.

## Blueprint 38: price-collapse guard (2026-08-03)

Incident: trade #176 — signal ask 0.81 (model 92%, edge +10.2%), FAK filled
1.35s later at 0.49 and lost -$15.54. BTC spot spiked toward the strike in
that second; makers repriced Down from 0.81 to ~0.49. A FAK's limit only
bounds the WORSE side (0.81 x 1.015), so any "better" price fills silently —
but the model probability was computed on the pre-jump spot, i.e. the bot
bought a coin-flip priced as one with the edge thesis already dead.

History scan (154 fills): 17 filled >5% below signal. Moderate dips
(-5..-20%) are liquidity noise and mostly WON (12/15) — do not block them.
Extreme collapses: -28% (won, lucky) and -40% (#176, lost). The guard cuts
only the tail.

Implementation:
- `price_collapsed(signal_ask, fresh_ask, max_drop_pct)` in cryptobot/logic.py
  (pure, tested). Fails open on missing/invalid fresh ask — it protects
  against a rare tail, not against book-fetch downtime.
- `crypto_max_price_drop_pct: float = 0.25` in core/config.py.
- `_handle_signal`: ONE fresh-book fetch per signal (shared by all users,
  scales to N accounts), skip reason `price_collapsed` when the fresh ask is
  >25% below the signal ask. Costs ~200-300ms against a 4s freshness budget
  and 1.37s median latency.
- BP34 re-quote path gets the same lower bound (`requote_price_collapsed`) —
  it already fetches the fresh book, the check is free.

Residual risk: a collapse inside the ~200ms between the guard's book fetch
and the FAK hitting the exchange is still unguarded; accepted (window shrinks
from ~1.4s to ~0.2s, event base rate is ~1/154 fills).

## Blueprint 39: fixed sizing means fixed (2026-08-04)

Incident: a copytrade user set fixed sizing with max position $15 and the bot
entered $5.00. Chain: fixed size $15 → BP8 unified risk cap (5% of ~$45
equity ≈ $2.30) → floored back up to the $5 exchange minimum. Every trade on
an account under $300 equity silently entered at $5 regardless of the chosen
size — the setting was dead weight and looked like a bug to the user.

Decision (product owner call, option "fixed means fixed"): the per-trade caps
that silently override an EXPLICIT user-chosen dollar size are now
kelly-only:

- BP8 unified risk cap (`max_risk_per_trade` × equity) — kelly-only.
- BP8 profit-protection trailing cap (`max_trade_loss_vs_profit_pct`) —
  kelly-only (same silent-override class; would have re-surfaced as
  "set 15, entered 8" once a profit cushion accumulated).

Unchanged, still apply in fixed mode:

- Tail-risk gates 1-4 (portfolio exposure 60%, per-event 15%, drawdown
  breaker, daily loss) — portfolio-level protections, not per-trade sizing.
  NOTE: gate 2 still clamps a single trade to 15% of equity per event, so a
  $15 fixed stake on a ~$45 account clamps to ~$6.75. Raising equity to
  ~$100+ makes the user's $15 effective. If product wants event-cap
  exemption for fixed mode too, that is a separate deliberate decision.
- Exchange minimum floor ($5) and balance/fee-headroom cap.
- Depth cap from the donor signal (can't buy size the book doesn't have).

## Blueprint 40: mutually-exclusive text-dialog states (2026-08-07)

Incident: a client could not withdraw — every pasted Polygon address got
"Только буквы, цифры и пробелы (до 24 символов)". Root cause: he had once
opened the BP24 create-wallet dialog; `awaiting_wallet_name` was set and
NEVER cleared on any exit path (its "Отмена" button routes to `wallet_list`,
which did not reset it). `handle_text_input` checks the wallet-name flag
BEFORE `withdraw_step`, so the stale flag intercepted all text input forever
(until an api restart, since user_data is in-memory, no PTB persistence).
Bonus hazard: any short text would have silently CREATED a wallet named after
it.

Fix: `_TEXT_FLOW_KEYS` + `_reset_text_flows(context)` in api/routers/
telegram.py — the full set of text-dialog states (`awaiting_wallet_name`,
`withdraw_step/_to/_amount`, `awaiting_daily_limit`, `awaiting_max_pos`) is
wiped at:

- every flow ENTRY: /withdraw, `withdraw_start`, `wallet_new`,
  `setmax_custom`, `setdaily_custom` — then the flow sets only its own flag;
- every navigation EXIT reachable from a dialog's cancel/back buttons:
  `menu`, `settings`, `wallet_list`, `withdraw_cancel`;
- `withdraw_confirm` (reads to/amount first, then wipes — retry can't
  re-confirm, unchanged semantics).

Invariant going forward: at most ONE text-dialog flag may be set at any time,
and any new text flow must enter through `_reset_text_flows`. `pos_cache` is
navigation state, not a text dialog — deliberately not in the set.

## Blueprint 41: allowance verification retries (2026-08-10)

Incident: client withdrawal aborted with "конвертация pUSD → USDC.e не
удалась: allowance_not_set spender=<offramp>". On-chain check showed the
approve WAS mined (allowance at MAX_UINT); `_ensure_allowance` read the
allowance immediately after `wait_for_transaction_receipt`, hit a lagging
node of the load-balanced RPC and saw pre-approve state — a false negative
that aborted the whole withdrawal. The client's manual retry succeeded
because the allowance persisted from the "failed" attempt.

Fix (core/polygon.py): after sending the approve, `_ensure_allowance` polls
the allowance up to 6 times / 2s apart (logs `allowance_read_lag` per miss)
before raising. Affects every wrap/unwrap path (deposits, withdrawals,
cryptobot funding sweep) — all shared this race.

## Blueprint 42: per-donor loss-streak circuit breaker (2026-08-10)

Incident: donor `donthackme` went cold 2026-08-07..09 and bled −$41 across
all copiers (the week's +100% was given back in two days) before the admin
manually removed the wallet. The old `deactivate_underperforming_donors`
task targets the legacy Model-A `donor_wallets` table and never sees
Model-B `tracked_wallets` — there was NO automatic brake on a cold donor.

Mechanism:
- Migration 027: `tracked_wallets.paused_until timestamptz` (NULL = live).
- `core/donor_guard.py`: pure `pause_decision()` + `donor_is_paused()` +
  `notify_admins()` (admin-bot token, falls back to the main bot; recipients
  = super-admin + `admins` table).
- `worker/tasks/donor_refresh.py::check_donor_streaks()` — called at the end
  of `sync_positions` (every 2 min, right after resolutions land). Joins the
  last 7 days of resolved `copy_trades` to donors via `trade_signals
  .source_wallet` and pauses a donor whose last `donor_pause_loss_streak`
  (default 5) most recent UNIQUE markets (dedup by condition_id — 3 users
  copying one losing market = ONE loss) all have `realized_pnl < 0`.
- Enforcement: `poll_tracked_wallets` skips paused wallets in the fan-out
  loop; the sniper path is gated inside `fire_sniper_signal` so BOTH sources
  (RTDS WS listener + Data-API poller) are covered.
- Admin notification (Russian, HTML) fires once per pause with the label,
  address and auto-resume time.

Sizing of the knobs (config): streak 5 → P(false positive) ≈ 0.4^5 ≈ 1% per
window for a healthy 60%-WR donor; pause 24 h ≈ 3-10 skipped trades for an
active donor, enough for a losing market regime to pass. Auto-resume with a
re-arm guard: re-pausing requires at least one NEW loss resolved after the
previous pause ended (the old streak alone can't re-trigger), so a donor
that is still cold costs at most ~1 extra trade per day instead of bleeding
all day. Open positions of a paused donor keep being managed normally
(stop-loss/redeem run in sync_positions regardless of the pause).

## Blueprint 43: kill the FAK re-quote path (2026-08-10)

Full-history audit of the crypto bot (275 resolved real-money trades,
2026-07-30..08-10, cum −$0.35 after giving back the +$82 peak) decomposed
the PnL and found the BP34 re-quote path is systematically toxic:

- requoted=True: 16 trades, WR 62%, **−$65.86**;
- requoted=False: 259 trades, WR 85%, **+$65.51** (peak cum +$117).

Mechanism (adverse selection): a FAK killed with "no orders found to match"
means the book moved between the shadow snapshot and our order — informed
flow ate the ask. The re-quote then chased the price +2..+4% ABOVE the
signal ask, so wins shrank to avg +$2.6 while losses stayed full −$15.
Mirror confirmation: plain entries whose fill came ≥3% CHEAPER than the
signal ran WR 88% / +$106.75 (n=48) — the bot's whole profit lives where
the market gives a better price than the model priced, never a worse one.

Change: `_requote_once` deleted from cryptobot/executor.py; a FAK kill now
inserts a skipped row with `skip_reason='fak_killed'` and moves on.
`requote_price_ok` removed from cryptobot/logic.py (+ its tests);
`crypto_requote_max_worse_pct` removed from config. The
`crypto_trades.requoted` column stays for historical data; new rows default
to false. BP38's collapse guard is untouched (it protects the primary
entry, which remains).

Caveat: the `requoted` flag exists only since migration 026 (08-01), so the
first ~75 trades may hide unflagged re-quotes — the true historical damage
is likely somewhat larger than −$65.86.

## Blueprint 44: remove sniper-mode donor mirroring (BP26/26.5-29) (2026-08-10)

Incident: three weeks after sniper copying of the 5-min BTC bot was
abandoned, the copytrade bot re-sent a loss notification for a July-20
sniper trade ("Bitcoin Up or Down - July 20, 9:40AM-9:45AM ET", −$19.77,
resolved 07-20 13:46 UTC, re-notified 08-10 13:49 UTC). Root cause chain:
(1) the sniper donor row was still active=true in tracked_wallets; (2) the
`settle:{uid}:{cond}` Redis dedup key expires after 7 days; (3) a
redemption/backfill sweep touched the ancient position and the Data API
re-surfaced it with a FRESH close timestamp, defeating the
settlement_lookback_sec "too old" filter in sync_positions.

Removed (code):
- `worker/tasks/poll_sniper_wallets.py` (fast poller + fire_sniper_signal),
  `worker/sniper_ws.py` (RTDS listener), `core/sniper_entry.py` (patient
  entry helpers) + their tests and `scripts/repair_sniper_ledger.py`.
- All `is_sniper` branches in `worker/tasks/execute_copy.py` — the patient
  entry loop, bankroll sizing, gate bypasses, no-retry branch, low-balance
  warning. Every entry now goes through the full default risk pipeline.
- Sniper delta-drop specializations in manage_positions (zero min-hold,
  1-tick confirm).
- Beat: sniper WS daemon thread; celery: poll-sniper-wallets schedule/route.
- Config: whole BP26/26.5 settings block. Two settings survived under new
  names because the DEFAULT path uses them: `sniper_fee_headroom_pct` ->
  `fee_headroom_pct` (0.03), plus `trade_ledger_update_*` kept as-is.

Kept: `tracked_wallets.mode` and `copy_trades.mode` DB columns (historical
data); poll_tracked_wallets skips any row with mode != 'default' so a legacy
sniper row can never be copied by the slow path. `core/order_fill.py`
(extract_buy_fill) stays — the cryptobot executor uses it.

DB state change (already applied in prod): the sniper donor row
(tracked_wallets id=21, 'BTC 5-min sniper') set active=false,
allowed_telegram_ids=null.

Re-notification fix (sync_positions, closed-positions loop): before emitting
a win/loss notice, look up the trade's `resolved_at` in copy_trades — if the
market was resolved for this user longer than settlement_lookback_sec ago,
skip regardless of the Data-API timestamp. The DB ledger is terminal truth;
the 7-day Redis key is now only a fast-path cache in front of it.

### BP44.1 follow-up (same day): the OPEN-positions loop was the real emitter

A second stale notice arrived after BP44 shipped. The closed-positions fix
was necessary but NOT the firing path: worthless losing tokens are never
redeemed, so they sit in `get_positions` as `redeemable=true` FOREVER, and
the open-positions branch of sync_positions re-notified each old loss every
time its 7-day Redis settle-key expired (staggered per market — hence one
ghost notice at a time for weeks). reconcile_settlements was exonerated:
`mark_trade_settled` sets `redeemed_at`, so settled losses drop out of
`get_outstanding_copy_trades` (checked live: 0 outstanding-but-resolved).

Fix: shared helper `_settled_long_ago(uid, condition_id)` (ledger
`resolved_at` older than settlement_lookback_sec; fails open) used in BOTH
loops. In the open-positions branch an old settled market burns the Redis
key (short-circuits next cycles' DB read) and skips the notice; old WINS
suppress only the message — the redeem dispatch still runs.

### Backlog (agreed 2026-08-10)

1. **Crypto drought gate.** When the shadow filter passed <15 signals in the
   trailing 24 h, the few that do pass are toxic: shadow full-history
   drought trades ran WR 74% / −$46 (positive-flow trades: WR 85% / +$249,
   still positive after 08-04); on real money drought trades are −$11.86
   (n=11). Plan: shadow engine adds its rolling 24h pass-count to the signal
   payload; executor skips entries when the count is below a
   `crypto_drought_min_signals_24h` threshold (default 15). Note: 08-10 was
   a HIGH-flow losing day, so this gate alone is not sufficient — see BP45.
2. ~~Tighten the crypto daily loss stop 3× → 2× stake.~~ Done in BP45.

## Blueprint 45: crypto regime gate + daily stop 2× (2026-08-10)

### Diagnosis that led here

08-10 closed at −$37 (15W/6L) and pushed all-time real PnL negative (−$11
from a +$83 peak on 08-06). Deploy-lag forensics first: BP43 (kill re-quote)
went live only at 13:53 UTC that day — the DB shows `requote_*` skip reasons
until 12:58 and a requoted losing trade at 13:23, with the first
`fak_killed` skip at 13:53. So 4 of the day's 6 losses happened on PRE-fix
code. Post-fix trades (5W/2L, −$11) were then cleared individually: the
scary 15:53 entry (fill 0.710 vs signal 0.880) is NOT a bug — historical
scan shows fills 10–25% below signal run 11W/2L, +$36.6 (a cheap fill is a
discount, not adverse selection), so BP38's 25% bound stays as is.

The real driver is a regime break on 08-07: real-money WR fell 86% → 70%
across ALL price buckets simultaneously. Control experiment: replaying the
executor's exact filter (btc, full variant, edge ≥ shadow_filter_min_edge,
strike ≥ 3 bp, price ≤ 0.89) over shadow_trades reproduces the break
exactly — shadow WR 86% before 08-07, 73% after. Shadow has no execution
path, so the degradation is model-vs-market, not anything we shipped. At
0.83–0.88 entries (win ≈ +$2.5, loss −$15) breakeven needs ~85% WR; a 73%
regime bleeds every single day until it ends. Bad days cluster
(08-07..08-10 consecutive), which is what makes a trailing-window gate
work.

### Mechanism

1. **Regime gate** (`wr_gate_blocks` in cryptobot/logic.py, global check in
   `_handle_signal` after the price ceiling): skip all entries while the
   win rate of the last `crypto_wr_gate_lookback` (15) RESOLVED shadow
   trades matching the executor-filter proxy (`recent_shadow_outcomes` in
   cryptobot/db.py) is below `crypto_wr_gate_min_wr` (0.80). The window is
   built from the SHADOW stream, which keeps trading while the real bot
   sits out — the window keeps sliding and the bot auto-resumes when the
   model warms up (no frozen-window deadlock, no persisted pause state,
   fully stateless recompute per signal ≈ 2/hour). Fails open until a full
   window exists.
2. **Daily loss stop: tried 2×, REVERTED to 3× the same day.** The month
   simulation's guard decomposition (replay on the real 283-trade sequence)
   falsified the backlog reasoning: with wins ≈ +$2.5 and losses = −$15, a
   NORMAL profitable day routinely troughs at ~−$30 (two losses) before the
   small wins grind it back — 08-05 trough −$30.38 → close +$24.82, 08-04
   trough −$28.34 → close +$18.28, 08-06 trough −$26.64 → close +$20.64.
   Stop 2× alone replays to −$56.65 vs −$26.66 actual (it locks in the
   trough); gate + 3× replays to +$34.86 and the 3× stop adds no drag on
   top of the gate. Lesson recorded: the original "damage past 2× is rarely
   recovered" claim came from eyeballing LOSING days only — survivor bias.

### Sizing (backtest over all 282 real-money trades)

Grid N ∈ {15,20,30} × min_wr ∈ {0.75,0.80,0.85}: N=15/0.80 skips 64 trades
(23%) including 14 of 39 losses, turning all-time −$11 into +$35 while
keeping volume; N=30/0.85 saves slightly more (+$52) but halves trade count
— rejected, the goal is to keep trading in good regimes. At deploy time the
gate is correctly ON (trailing shadow WR 67%).

Log marker: `crypto_signal_skipped reason=wr_gate trailing_wr=…`.

Hotfix 08-11: the gate's shadow fetch (120 raw rows) under-spanned the
window in low-flow periods — live it yielded 9/15 outcomes overnight and
the gate failed open exactly when low flow made it most needed (the 03:28
−$15.34 entry would likely have been blocked by a full window). Now the
min-edge cut is pushed into the PostgREST query and the fetch is 1000 rows
(~ weeks of coverage). Fail-open below a full window remains the intended
behavior for a genuinely young table.

### Month-ahead expectation (simulated 2026-08-10)

Method: replay BP45 guards on the real trade sequence → guarded day PnLs →
circular 3-day-block bootstrap into 30-day months (20k paths, $15 stakes).
Mixed regime (good:cold ≈ 2:1 as observed): median +$91/mo, mean +$87,
IQR +$12..+$169, p5 −$112, p95 +$272, P(losing month) 22%, typical
intramonth drawdown −$81 (bad case −$195). Bounds: full good-regime month
median +$148; full cold-regime month median −$34 (the gate caps regime
bleed at roughly one stake/month of gate-lag leakage). Caveats: 12 days of
history, exactly one regime break observed; block bootstrap can't imagine
regimes worse than seen; assumes signal flow ~25/day and one $15 account.

## Recovery plan (agreed 2026-08-15) — full-book audit findings

Context: user reported both bots "bled the balance". Full audit (all
copy_trades rows, not just result∈{win,loss}) uncovered that every previous
"profitable" report was blind to stop-loss exits (status='closed', result
not set). TRUE all-time copytrade ledger:

| component                     | trades | PnL       |
|-------------------------------|--------|-----------|
| resolution wins               | 579    | +$1,056.50|
| resolution losses             | 65     | −$734.41  |
| stop exits (closed)           | 223    | −$514.14  |
| **total**                     |        | **−$192.06** |

Stop autopsy (on-chain payout check per stopped (condition, outcome)):
119 of 220 resolvable stops (54%) were PREMATURE — the market recovered and
resolved FOR us. Cost: −$233.20 realized + $538.54 missed win payout.
Saved stops (101): paid −$271.20 to avoid −$533.81 riding to zero =
+$262.61 salvage. NET effect of the whole stop engine vs hold-to-resolution:
**−$275.93** — negative in EVERY category (esports −$145, other −$131) and
EVERY entry-price bucket. Depth split (loss as fraction of stake — proxy
for which mechanism fired): shallow <0.5 (classic delta-drop) net −$161
(95 premature / 60 saved); mid 0.5–0.8 net −$67; deep ≥0.8 (hard-stop-like)
net −$45 (even 7 positions priced ≤0.2 recovered to full wins, costing more
than all deep salvage). Binary-market whipsaw defeats price stops at every
threshold we ran.

Priority plan:
1. **BP46 — copytrade stop engine OFF (hold to resolution).** Biggest
   single EV change: +$276 vs historical trajectory.
2. **Crypto bot back ON** — DONE by user 08-15 (trading_on had been off
   since ~08-13; the bot missed a 24-signal 92%-WR day on 08-14 while the
   BP45 gate was open). Probation criteria agreed: run untouched 2–3 weeks;
   success = cumulative PnL back above zero AND gate open ≥~60% of signals;
   otherwise rework the model economics or close the pilot.
3. **BP47 — throttle "Сделка не прошла" notifications** (70% of esports
   copy attempts are zero fills on thin books; ~10 noise messages/day).
4. Backlog: KAGE leaderboard parsing for donor discovery; crypto drought
   gate (see BP44 backlog).
Rejected: weekday scheduling for the crypto bot ("trade Wed–Fri") — only
2–3 observations per weekday; the "toxic Monday" (−$95) is two regime-break
dates (08-03, 08-10), and the BP45 WR gate already does regime selection
adaptively without a calendar.

## Blueprint 46: copytrade stop engine off — hold to resolution (2026-08-15)

### Decision

Disable BOTH the Delta-Drop stop (BP10/17/19/21 stack) and the hard-stop
floor by default. The strategy returns to its original thesis: binary
positions are held to resolution; capital risk per trade is bounded by the
stake; donor edge realizes at resolution, and the 223-stop autopsy proves
every stop threshold sells recoveries more often than it saves stakes.

### Mechanism (one kill-switch, machinery preserved)

- New setting `stop_engine_enabled: bool = False`. In sync_positions, gate
  the ENTIRE post-redeemable stop section behind it (hold-time guard, book
  fetch, 5-tier entry resolver, phantom guard, position marks, hard-stop
  floor, spread veto, delta-drop evaluation). With the engine off the loop
  goes straight to the next position after the redeemable branch —
  also saving one CLOB book call per open position per 2-min cycle.
- NOT deleted: the whole stop stack stays in code for reversibility (it is
  battle-hardened across 4 blueprints); `close_position` task untouched
  (manual close paths still use it); settlement/redeemable branch, BP44.1
  dedup and notifications untouched.
- `_drop_ticks`/`_first_seen`/`_closing` bookkeeping only runs when the
  engine is on.
- Invariant: the flag must not affect settlement detection — only the
  pre-resolution EXIT logic.

### Tests

Implementation note: the gate is a 2-line config check inside the
monolithic sync_positions task; a dedicated dispatch test would require
mocking the full Data-API/CLOB/DB surface, which this repo intentionally
avoids (tests cover pure decision helpers). Verified instead by code path
review: the gate sits AFTER the redeemable branch and BEFORE any stop
logic, so settlement/notification behavior is provably unchanged and no
close_position dispatch is reachable while the flag is False.

## Blueprint 47: throttle zero-fill "Сделка не прошла" notices (2026-08-15)

Esports books are so thin that the donor's own order empties the ask side;
~70% of esports copy attempts end status='unfilled' (106 of ~150 since
07-25, all users zero-filled on ALL of them) — each currently emitting a
"Сделка не прошла" push. The attempts themselves are correct (filled
esports copies run 15W/2L +$47.88; we must keep trying at fair prices and
never chase — BP43 lesson), only the noise is wrong.

Implemented in execute_copy `_notify`, fill_status=='none' branch:
- Redis `notify_once(f"unfilled-note:{telegram_id}", ttl=4h)` gates the
  push; a suppressed notice INCRs the `unfilled:{telegram_id}` counter
  (new `incr_counter`/`pop_counter` helpers in core/cache.py, 24h expiry,
  fail-open to 0 on Redis outage — worst case is pre-BP47 behavior).
- When the gate is open, the message appends the suppressed count since
  the last push ("За последние часы ещё N сделок не прошли по той же
  причине…") and the counter resets.
- DB rows (status='unfilled') unchanged — stats and audits keep full data.
- No throttle for partial fills (real positions, must stay loud).

## Blueprint 48: donor scout — automated donor discovery from our own tape (2026-08-15)

### Why the old pipeline kept picking market makers

KAGE is dead (site down) and its 4 imported wallets earned +$10/mo. The
admin `/refresh` (`core/wallet_discovery.discover_quality`) has the same
structural defect plus one active bug:
- **Wrong source**: candidates come from the GLOBAL profit leaderboard —
  populated by long-dated-politics whales (outside our 0.5–72h universe)
  and industrial MMs. The anti-MM heuristics (profit/volume ratio,
  directionality, density, MAKER_REBATE) are decent but polish a wrong
  funnel: the leaderboard tells us who is RICH, not who is COPYABLE.
- **Wrong criterion**: ranking by leaderboard PnL ≠ "our pipeline can fill
  this wallet's entries at fair prices in our market window".
- **Active bug**: the prune step REMOVES tracked wallets that fail the
  current leaderboard filter. Proven manual donors don't appear on the
  global boards at all (their pnl_map=0 → ratio=None → activity-feed
  heuristics decide), so `/refresh` could silently swap good donors for
  leaderboard MMs. This is the user-reported "заменяет хорошие на плохие".

### Design: harvest → score → shadow probation → human promote

Principle inversion: a quality donor is measured ONLY by our own pipeline.
1. **Harvest** (`worker/tasks/donor_scout.harvest_wallet_sightings`, beat
   60s): passive record-only revival of the Model-A whale feed.
   `fetch_whale_trades` (global taker BUY feed, server-side cash filter at
   `scout_min_trade_usdc`=$200) ∩ fast-markets universe → insert into
   `wallet_sightings` (unique tx_hash; tracked wallets excluded). No
   copies, no notifications — just tape.
2. **Score** (`score_donor_candidates`, nightly): wallets with
   ≥`scout_min_sightings` (5) sightings/14d get one Data-API activity pull
   (reuses `_activity_profile` anti-MM fingerprints: directionality,
   trades/day, avg size, MAKER_REBATE) + resolution check of their sighted
   markets via Gamma (`outcomePrices`). Hard filters kill MM profiles;
   survivors get a Laplace-smoothed WR score `(wins+1)/(resolved+2)` into
   `donor_candidates`. Top qualifying candidates are auto-enrolled into
   probation (`tracked_wallets.mode='candidate'`) up to
   `scout_probation_slots` (5) concurrent seats. Also prunes sightings
   >30d.
3. **Shadow probation**: poll_tracked_wallets now lets mode='candidate'
   wallets through the full accumulate→fire path, inserts their signals
   into trade_signals with `probation=true`, but NEVER dispatches
   execute_copy_trade. The signal row stores OUR vwap at OUR signal time —
   after 2 weeks the would-be PnL at a nominal $15 stake measures exactly
   "what our users would have earned". `_consensus_count` excludes
   probation rows.
4. **Digest** (`donor_scout_digest`, weekly Mon 09:00 UTC): per-candidate
   probation stats (signals, resolved, WR, would-be PnL) pushed to admins
   via the admin bot with inline buttons — `dc:p:<addr>` promote to
   mode='default' (live copying), `dc:x:<addr>` dismiss (active=false +
   donor_candidates.status='dismissed'). Live donors are NEVER auto-removed:
   demotion is BP42 pause + a digest retirement hint for stale (no signals
   14d) or negative-30d donors. Human confirms; the bot never swaps donors
   silently.

### /refresh repurposed, prune killed

- `discovery_prune_enabled: bool = False` — the auto-prune block in
  discover_quality is gated off (the "replaces good wallets" bug).
- discover_quality now adds leaderboard survivors as mode='candidate'
  (probation), never straight to live copying. The leaderboard remains a
  SECONDARY candidate source feeding the same probation funnel.

### Schema (migration 028)

- `wallet_sightings`: wallet, tx_hash (unique), condition_id, token_id,
  outcome, price, size_usdc, title, traded_at; indexed (wallet, created_at)
  and (created_at).
- `donor_candidates`: wallet (unique), name, sightings/volume/avg size 14d,
  resolved_count/wins, directionality, trades_per_day, is_mm, score,
  status ∈ new|candidate|promoted|dismissed, timestamps.
- `tracked_wallets.mode` gets value 'candidate' (column exists since BP26).
- `trade_signals.probation boolean not null default false`.

### Invariants

- Probation signals must never reach execute_copy_trade and never count
  toward consensus.
- Harvest inserts are idempotent (unique tx_hash, ignore 23505).
- Scoring/digest failures must never touch the live donor list.
- add_tracked_wallet(mode=...) preserves an existing live donor's mode on
  re-add (a candidate insert cannot demote a live donor).

### BP48.1 fix (2026-08-21): resolution step was dead — Gamma ignores the filter

First live audit: harvest worked (17,747 sightings / 2,989 wallets in 2
days), MM fingerprints worked (industrial 372-394 trades/day wallets
correctly flagged), but ALL 475 scored candidates had resolved_count=0 —
Gamma's /markets endpoint silently ignores unknown query params and its
condition_ids filter returned zero rows for real (esports) conditions, so
the WR tally was empty, scores froze at the 0.5 prior and probation
enrollment (which requires resolved_count>0) never fired.
resolve_winning_outcomes now uses CLOB /markets/{condition_id} (closed +
per-token `winner` flag, authoritative), one call per condition with a
politeness delay, failure-tolerant. Verified live: 25/34 sample conditions
resolved; candidates now score properly (e.g. 12/14 wins -> 0.812).

## Blueprint 49: crypto entry window 60-90s (2026-08-19)

### The finding (full audit 08-19)

Crypto bot all-time: -$58.68 over 309 resolved trades (WR 82.5%). Split by
SECONDS TO WINDOW CLOSE at entry, real trades:

| window    | trades | WR    | PnL     | avg    |
|-----------|--------|-------|---------|--------|
| 90-120s   | 246    | 80.9% | -$90.38 | -$0.37 |
| 60-90s    | 45     | 91.1% | +$42.91 | +$0.95 |
| 30-60s    | 14     | 85.7% | -$2.40  | -$0.17 |

80% of entries happened at 90-120s because signals publish the moment the
edge first appears — i.e. at window open. Executor-filtered shadow over the
whole era (07-30..08-19, 14,266 resolved rows) confirms on larger n:
t60-90 +$92.48 (n=299, WR 85.3%) is the ONLY positive bucket; t90-120
-$241, t30-60 -$79, t20-30 -$13, full -$216, maker -$383. t60-90 was
positive/flat in all three weeks including the cold regime (+$94.0 / +$1.3
/ -$2.8) while full bled every week. Mechanism: at 2 minutes out the model
prices its least reliable horizon at an already-high ask; at 60-90s the
same ask buys a markedly sharper forecast.

### Change (publisher-side only)

- `crypto_signal_time_left_min_sec=60` / `crypto_signal_time_left_max_sec=90`.
- shadow_engine publishes the executor signal (and the Telegram "Сигнал"
  broadcast) on ANY variant entry whose time_left falls inside the window,
  once per condition (`published_conditions` in-memory set, capped at 4000,
  executor DB dedup is the durable backstop). Previously: `variant=='full'`
  → fired at first edge appearance, usually 90-120s.
- Shadow COLLECTION unchanged — every variant keeps recording the wide
  window, so the full stream keeps feeding the BP45 gate and re-analysis.
- BP45 gate DELIBERATELY unchanged (gauges the wide full stream):
  cross-gate replay: full-gauged gate over t60-90 entries takes +$114.26
  (n=219, WR 86.8%), skips -$21.79; re-gauging on t60-90 itself halves the
  take to +$48 by skipping 90.5%-WR trades. Wide stream = earlier smoke
  alarm for regime breaks.
- Executor untouched (freshness, ceiling, collapse guard, daily stop all
  apply as before).

### Hypotheses tested and REJECTED (replay 07-30..08-19, t60-90 stream)

- Widen to 30-90s: late-edge t30-60 entries (no passing t60-90 sibling)
  are -$52.36/142 — the window closes at 60s.
- Lower ceiling to 0.85: +$100.69/140 ungated — more per trade but half
  the flow and same total as 0.89 (+$92.48/299); with the cross-gate the
  0.89 ceiling nets more (+$114). Keep 0.89.
- Price floor 0.70: kills profit (+$92 -> -$16); cheap 0.5-0.6 fills are
  +$55. No floor.
- min_edge 0.08/0.10: -$15 / -$53 (vs +$92 at 0.07). Keep 0.07.
- strike >=5bp / >=10bp: -$36 / -$39. Keep 3bp.
- eth/sol/xrp at t60-90: ZERO rows pass the executor filter — nothing to
  trade there, not a config problem.
- Drought gate: only 1 day with <5 passing signals (-$15) — not enough
  evidence, stays backlog.
- Daily 3x stop on the gated stream: never triggers (no change); keep as
  tail insurance.

### Expectation

$200 + flat $15 on ungated t60-90 replay: final $292 (peak $338, maxDD
-$85) vs actual-behavior counterfactual $129. Honest caveat: the last two
weeks are ~flat (+$1.3, -$2.8 ungated; the cross-gate improves cold-week
skips) — the window change removes the systematic bleed, it does not
manufacture edge in a cold regime.

## Blueprint 50: alt data-collection mode (2026-08-19)

### Why

Per-asset models for eth/sol/xrp were requested; the data says NOT YET: a
month of shadow collection produced 18/26/23 resolved rows per alt
(~0.7/day) vs 21,283 for btc — any grid search on n≈20 is noise mining.
Root cause: the shadow entry bar (edge >= shadow_min_edge 0.05) almost
never clears on thin alt books, so nothing gets recorded to learn from.

### Change

- `shadow_alt_min_edge=0.02`: non-BTC assets record virtual entries at a
  lower bar purely to grow the dataset (BTC keeps 0.05). Expected: tens of
  alt rows/day -> a model-fit sample in ~2-3 weeks.
- `crypto_signal_assets=["btc"]`: hard whitelist between shadow and real
  money, enforced TWICE — at publish (engine) and in the executor
  (_handle_signal skips `asset_not_whitelisted`). Alts stay
  data-collection-only until a per-asset model validates on a real sample.
- `_row_is_signal` re-keyed (also fixes a BP49 gap): settlement win/loss
  broadcasts now mirror the PUBLISHED set — execution-window variant
  (t60-90 by default, name derived from the window settings) + whitelisted
  asset + signal filter. Previously keyed on variant='full', which after
  BP49 would notify results for entries subscribers never saw (and alt
  rows would have started notifying once BP50 grew their flow).

### Analysis protocol (when the sample exists, ~2-3 weeks)

Per asset: calibration of model_p vs realized WR, then the BP49-style
grid (window × edge × ceiling × strike) with split-half time validation
and n>=200 per surviving config. Only a config that is positive in BOTH
halves gets a real-money pilot, and only via its own entry in
crypto_signal_assets.

## Blueprint 51: edge corridor 7-8% (2026-08-21)

### Why

After BP49 the t60-90 stream itself went red in the W33-34 regime
(weekly: +13/+85/+94/-30/-154 over the five ISO weeks of full shadow
history, 22,230 resolved btc rows since 07-21). Factor scan for a
regime-robust confidence signal found ONE: the edge itself, and the
relationship is inverted at the top. In t60-90 (executor filters
applied):

- edge 7-7.5%:  +$46.69 / 74  (WR 85.1%)
- edge 7.5-8%:  +$87.84 / 69  (WR 89.9%)
- edge 8-9%:    -$10.50 / 155
- edge 9-10%:    +$2.41 / 125
- edge 10%+:   -$118.20 / 148

Mechanism: a huge model-market divergence usually means the MARKET
knows something the model doesn't (informed flow holding the price away
from the model's fair value) — adverse selection against an
overconfident model (calibration: stated 90%+ realizes ~85%). A
moderate divergence is genuine mispricing.

### Robustness (full history)

Corridor (7% floor from BP30.4 filter, <8% cap) on t60-90: +$134.53 /
143 trades, WR 87.4%, positive in ALL FIVE ISO weeks including toxic
W34 (+$32.02 while the uncapped stream lost -$154). Both halves of the
corridor independently positive. Does NOT generalize to other windows
(t90-120 corridor: -$128; t30-60: -$5) — the alpha is the combination
"reliable 60-90s horizon × moderate divergence", which also means it is
not a data-mining artifact that shows up everywhere.

### Change

- `crypto_max_edge=0.08`: publish gate in the shadow engine now requires
  `edge < crypto_max_edge` (floor stays `shadow_filter_min_edge=0.07`
  inside passes_signal_filter).
- `edge_exceeds_cap` (cryptobot/logic.py) + executor belt-and-suspenders
  skip `edge_above_cap` — real money never depends on a single gate.
- `_row_is_signal` mirrors the cap so settlement notices track the
  published set exactly.
- Collection untouched: all variants/edges keep recording.

### Expectation & monitoring

~3-5 signals/day (was ~15). At $15 stakes: ~+$0.94/trade, ~+$30/week in
a normal regime, ~breakeven-to-positive in cold ones. Review after ~30
live trades. Phase 2 candidate (NOT implemented): decelerating-vol
filter `sigma_fast/sigma < 1.0` on top of the corridor showed +$169.46
/ 88 (WR 93.2%, all thirds positive) but needs sigmas in the executor
payload and halves the flow — add only after the corridor validates
live, one variable at a time.

## Blueprint 50.1: per-asset spot silence watchdog (2026-08-21)

### Why

After BP50 shipped, alts recorded ZERO rows while server logs showed
eth/sol/xrp closing every window with `reason=no_window_open` for hours
— i.e. not a single spot tick per asset (window open price is only set
in the tick handler). Root cause is structural: the RTDS silence
watchdog was global (`last_spot_rx_monotonic`), so as long as btc
ticked, the connection looked healthy and a server-side drop of an
individual symbol's subscription was never repaired. The alt dataset
BP50 exists for silently stops growing, and nothing alerts.

### Change

- `SpotState.last_rx_monotonic` + `_silent_assets()`: any subscribed
  asset with no tick for `shadow_spot_asset_silence_sec` (600s; healthy
  Chainlink heartbeats every symbol at least ~1/min) triggers a logged
  `shadow_spot_asset_silent` warning and a forced reconnect, which
  re-subscribes every symbol. Baseline = max(last tick, connection
  start), so fresh connections get a full grace period and stale state
  can't cause a reconnect loop.
- If RTDS genuinely never streams a symbol, the engine now reconnects
  every 10 min and the warning makes that VISIBLE in logs instead of
  silent starvation. EWMA/vol state lives outside the connection loop
  and survives reconnects; alts still need vol warm-up after first
  ticks arrive.
