-- Migration 006: persist whale wallet + track-record score on each signal.
-- Lets us validate (in observe mode) whether profitable-wallet signals outperform.
-- Run once in the Supabase SQL editor. Idempotent.

alter table trade_signals add column if not exists whale_wallet text;
alter table trade_signals add column if not exists whale_realized_pnl double precision;
alter table trade_signals add column if not exists whale_resolved_count integer;
alter table trade_signals add column if not exists whale_winrate double precision;
alter table trade_signals add column if not exists whale_passed boolean;

create index if not exists idx_trade_signals_whale_wallet on trade_signals (whale_wallet);
