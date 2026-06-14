-- Migration 001: whale-tracking strategy + manual subscriptions
-- Run once in the Supabase SQL editor (Project → SQL → New query).
-- Safe to re-run: all statements are idempotent.

-- 1. Whale signals have no donor — allow NULL donor_id.
alter table trade_signals alter column donor_id drop not null;

-- 2. Store the exact outcome token bought by the whale, for precise copying.
alter table trade_signals add column if not exists token_id text;

-- 3. Source tx hash is used for de-duplication of signals across restarts.
alter table trade_signals add column if not exists source_tx_hash text;
create index if not exists idx_trade_signals_source_tx on trade_signals (source_tx_hash);
create index if not exists idx_trade_signals_created_at on trade_signals (created_at desc);

-- 4. Track whether the user's wallet has been registered (on-chain approvals done).
alter table users add column if not exists wallet_registered boolean not null default false;

-- 5. (Optional) Ensure CLOB credential columns exist for per-user API keys.
alter table users add column if not exists clob_api_key text;
alter table users add column if not exists clob_secret text;
alter table users add column if not exists clob_passphrase text;

-- 6. (Optional) Balance cache used by the deposit monitor.
alter table users add column if not exists balance_usdc double precision default 0;
