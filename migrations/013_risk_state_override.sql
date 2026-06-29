-- Blueprint 8: risk state machine + manual drawdown override + realized baseline.
-- Apply before deploying Blueprint 8 code.

alter table users add column if not exists risk_state text default 'active';
  -- valid values: active | paused_drawdown | paused_daily_loss

alter table users add column if not exists risk_override_at timestamptz;
  -- timestamp of the last manual "Снять блокировку" action (consent audit trail)

alter table users add column if not exists risk_override_count int default 0;
  -- cumulative count of manual overrides (audit trail: user accepted responsibility)

alter table users add column if not exists realized_baseline double precision;
  -- equity snapshot at last reset (registration / top-up / manual override).
  -- used by the profit-protection trailing cap as the "zero profit" reference point.
  -- NULL = treat current equity as baseline (no accumulated profit to protect yet).
