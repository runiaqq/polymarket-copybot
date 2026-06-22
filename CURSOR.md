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
- **[BP4] Tail-risk portfolio controls** (`core/risk.py`): four pre-trade gates —
  aggregate exposure cap (60% of equity), per-event correlation cap (15% of equity), drawdown
  circuit breaker (25% drawdown from HWM → 24h pause), daily loss limit (10% of equity → pause
  to 00:00 UTC). `equity_hwm` and `copy_paused_until` stored on `users` (migration 010).
  `get_active_subscribers` honors `copy_paused_until`; `sync_positions` refreshes HWM each cycle.

---

## 4. Known Bugs & Missing Features

This section contains **implementation blueprints**. Each is precise enough to implement directly.
Treat every formula and contract call as authoritative; do not substitute your own. All money-moving
code MUST follow §5 (idempotency, fail-closed, key safety).

**Conventions used below**
- `equity(user)` = pUSD in the deposit wallet **+** sum of current value of open positions
  (`get_balances(dw).pusd + Σ position.current_value`). Use **free pUSD only** where a cash balance is
  required (placing a new order). Never count unredeemed/illiquid tokens as free cash.
- `p` = entry price of a YES/NO share (0–1). `q` = estimated true win probability.
- All new tunables live in `core/config.py`; all new columns in a new `migrations/00X_*.sql` + a helper
  in `core/db/queries.py`. Never hardcode magic numbers in task logic.

---

> **Blueprints 1–4 have been implemented** (see §3 Working Features for details).
> Migrations 008–010 are defined below and must be applied manually in the Supabase SQL editor.

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

**Acceptance:** a won position credits pUSD within ~2 min of on-chain resolution **even if it never
appears as `redeemable` in the Data API**; no double-redeem; loss recorded with correct P&L; restart
mid-flight resumes safely.

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

**Final stake (caps dominate):**
```
f_kelly = max((q_hat - p) / (1 - p), 0)
f       = kelly_lambda * f_kelly                 # kelly_lambda = 0.25 (quarter-Kelly)
f       = min(f, max_risk_per_trade)             # hard ceiling, e.g. 0.05 of equity
stake   = f * equity(user)
stake   = min(stake, user.max_position_usdc, depth_cap)   # existing depth/cap clamps stay
stake   = 0 if (stake < min_order_usdc or free_pusd < stake or equity < min_balance)
```
This **replaces** the flat `size_usdc = min(user_max, depth_cap)` in `execute_copy_trade`. Keep all
existing depth/book-safety/price-band clamps; Kelly only sets the **upper** bound more intelligently.

**Minimum balance — derived, not guessed.** Require that the smallest trade we'd place is meaningful
and that risk-per-trade stays ≤ `max_risk_per_trade`:
```
min_balance = max(
    min_order_usdc / max_risk_per_trade,   # so f_max·B ≥ exchange/effective min order
    n_target_positions * min_order_usdc    # so equity can diversify across N concurrent bets
)
# with min_order_usdc = $5 (effective, fees+slippage make sub-$5 copies pointless),
# max_risk_per_trade = 0.05, n_target = 5  →  min_balance = max($100, $25) = $100
```
Below `min_balance`, **disable copying** for that user with a clear one-time message (do not place
dust trades that slippage/fees eat). Surface `min_balance` in onboarding and `/balance`.

**Files:** `core/sizing.py` (new pure module: `kelly_stake(p, score, consensus, equity, free_pusd,
cfg) -> float`, fully unit-testable, no I/O), `worker/tasks/execute_copy.py` (call it),
`core/config.py` (knobs below). Keep it a **pure function** so it can be tested without the chain.

**Config:**
```python
sizing_mode: str = "kelly"          # "fixed" (legacy) | "kelly"
kelly_lambda: float = 0.25          # fraction of full Kelly
kelly_base_edge: float = 0.03       # edge for a fully-trusted single wallet
kelly_edge_cap: float = 0.06        # absolute edge ceiling
kelly_prior_strength: float = 10.0  # α=β for winrate shrinkage
max_risk_per_trade: float = 0.05    # hard cap: ≤5% of equity per position
min_order_usdc: float = 5.0
min_balance_usdc: float = 100.0     # below this, copying disabled
n_target_positions: int = 5
```

**Acceptance:** stake scales up with wallet quality/consensus and **down** as `p → 1` (favorites get
small bets), never exceeds `max_risk_per_trade · equity`, never below `min_order_usdc`, and copying is
disabled below `min_balance_usdc`. `sizing_mode="fixed"` reproduces current behavior for rollback.

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
> realized_pnl, resolved_at, redeemed_at, redeem_tx`) via migration 008 — **not yet applied**.

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

### 6.10 Migration order

`001` whale strategy → `002` access codes → `003` username → `004` admins → `005` deposit wallets →
`006` wallet score → `007` tracked wallets → (planned) `008`–`010`.
