# CURSOR.md — Nexa AI (Polymarket Copy-Trading SaaS)

> **Read this file fully before writing any code.** It is the single source of truth for
> architecture, current state, and **mandatory** safety rules. This system moves **real user
> money** on-chain. A wrong edit can drain a subscriber's deposit. When unsure, STOP and ask —
> do not guess or hallucinate APIs, contract addresses, or DB columns.

Product aliases seen in code: **Nexa AI** (product name), `PolyMind` (user-facing bot copy),
`Polymarket CopyBot` (FastAPI title). They are the same project.

---

## 1. Project Overview

Nexa AI is a **commercial subscription (SaaS) copy-trading bot for [Polymarket](https://polymarket.com)**,
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
- Activation: admin `/grant` (by id/username) → `set_subscription(days)`, or one-time
  **access codes** (`access_codes`, redeemed via deep-link). Expiry reminders run every 6h.
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

### 6.10 Migration order

`001` whale strategy → `002` access codes → `003` username → `004` admins → `005` deposit wallets →
`006` wallet score → `007` tracked wallets → `008` settlement ledger → `009` tracked avg size →
`010` risk controls → `011` user sizing mode → `012` position state (exit_tx + user/condition index) →
`013` risk state + manual override (Blueprint 8).

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

**Migrations applied as of the last deploy (2026-06-22):** 001–010. Apply 011 and 012 before the next deploy.

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
