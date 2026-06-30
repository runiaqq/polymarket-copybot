-- Blueprint 13.2: per-user daily trade cap.
-- NULL (default) = unlimited — preserves existing behaviour for all current users.
-- Counted against copy_trades rows created since 00:00 UTC today with status != 'failed'.
-- Apply in Supabase SQL editor. Idempotent.

alter table users add column if not exists max_daily_trades int;
