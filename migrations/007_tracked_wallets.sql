-- Migration 007: curated whitelist of profitable wallets to copy (Model B).
-- Run once in the Supabase SQL editor. Idempotent.

create table if not exists tracked_wallets (
    id          bigint generated always as identity primary key,
    address     text unique not null,
    label       text,
    active      boolean not null default true,
    added_at    timestamptz not null default now()
);

create index if not exists idx_tracked_wallets_active on tracked_wallets (active);

-- Consensus: how many distinct tracked wallets backed a signal's market/outcome.
alter table trade_signals add column if not exists consensus integer default 1;
alter table trade_signals add column if not exists source_wallet text;
