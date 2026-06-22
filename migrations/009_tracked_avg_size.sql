-- Blueprint 2: avg trade size on tracked wallets (used for dynamic conviction threshold)
-- Apply in Supabase SQL editor. Idempotent.

alter table tracked_wallets add column if not exists avg_trade_usdc double precision;
