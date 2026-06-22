-- Blueprint 4: per-user risk controls (high-water mark + copy pause)
-- Apply in Supabase SQL editor. Idempotent.

alter table users add column if not exists equity_hwm double precision default 0;
alter table users add column if not exists copy_paused_until timestamptz;
