-- Migration 002: one-time access codes for deep-link subscription activation.
-- Run once in the Supabase SQL editor. Idempotent.

create table if not exists access_codes (
    code        text primary key,
    tier        text not null default 'active',
    days        integer not null default 30,
    note        text,
    used_by     bigint,                 -- telegram_id that redeemed it (null = unused)
    used_at     timestamptz,
    created_at  timestamptz not null default now()
);

create index if not exists idx_access_codes_unused on access_codes (code) where used_by is null;
