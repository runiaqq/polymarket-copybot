-- Blueprint 1: deterministic settlement ledger on copy_trades
-- Apply in Supabase SQL editor. All statements are idempotent.

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
