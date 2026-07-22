-- Migration 023: BP30.2 stressed volatility diagnostics. Idempotent.
alter table shadow_trades
    add column if not exists sigma_fast double precision;

alter table shadow_trades
    add column if not exists q_cal double precision;
