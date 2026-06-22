-- Per-user sizing mode: 'fixed' (legacy flat cap) or 'kelly' (fractional Kelly).
-- NULL / 'fixed' → existing behaviour; 'kelly' → dynamic sizing via core/sizing.py.
-- Apply in Supabase SQL Editor. Idempotent.

alter table users add column if not exists sizing_mode text default 'fixed';
