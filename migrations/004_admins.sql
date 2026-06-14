-- Migration 004: admin bot — admins registry and one-time admin invite codes.
-- Run once in the Supabase SQL editor. Idempotent.
-- The super-admin (ADMIN_TELEGRAM_ID from env) is always authorized regardless of this table.

create table if not exists admins (
    telegram_id bigint primary key,
    username    text,
    active      boolean not null default true,
    added_by    bigint,
    created_at  timestamptz not null default now()
);

create index if not exists idx_admins_username_lower on admins (lower(username));

create table if not exists admin_codes (
    code        text primary key,
    note        text,
    used_by     bigint,
    used_at     timestamptz,
    created_at  timestamptz not null default now()
);

create index if not exists idx_admin_codes_unused on admin_codes (code) where used_by is null;
