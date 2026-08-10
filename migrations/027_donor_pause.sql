-- Migration 027: Blueprint 42 — auto-pause cold donors on a loss streak.
-- Idempotent. A donor whose last N unique copied markets ALL resolved at a loss
-- is paused until this timestamp (enforced in poll_tracked_wallets /
-- poll_sniper_wallets). NULL = not paused.

alter table tracked_wallets add column if not exists paused_until timestamptz;
