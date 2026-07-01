-- Blueprint 17: Spread-Trap stop-loss hardening + Broken-Override state reset.
-- Apply before deploying Blueprint 17 code.

-- 17.A: store the CLOB best_bid observed at fill time for a like-for-like
-- bid-vs-bid drop comparison (Layer 3).  NULL for legacy rows — code gracefully
-- falls back to Layers 1+2+4 when this is missing.
alter table copy_trades add column if not exists entry_bid double precision;

-- 17.B: self-expiring override flag.  Set to next 00:00 UTC when the user
-- taps "Снять блокировку".  While now() < risk_override_until, both the
-- drawdown and daily-loss breakers are suppressed in the monitor.
-- Distinct from risk_override_at (audit stamp) which already exists.
alter table users add column if not exists risk_override_until timestamptz;
