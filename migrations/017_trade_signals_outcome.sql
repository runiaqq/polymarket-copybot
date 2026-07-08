-- 017: BP22 — Admin-bot audit fixes.
-- Guarantee the trade_signals display columns the admin trade history reads.
-- The admin /user history view selects trade_signals.title/outcome; `outcome`
-- was never guaranteed by any migration and is never inserted by the Model B
-- poller (fixed in code alongside this migration). Idempotent — safe to re-run.
-- Apply in the Supabase SQL editor BEFORE deploying the BP22 code.

alter table trade_signals add column if not exists outcome text;

-- Optional perf index for the admin PnL/history reads (BP18 §18.3 suggestion).
create index if not exists ix_copy_trades_user_redeemed
  on copy_trades (user_id, redeemed_at desc)
  where redeemed_at is not null;
