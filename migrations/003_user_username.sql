-- Migration 003: store Telegram username so admins can extend subscriptions by @nick.
-- Run once in the Supabase SQL editor. Idempotent.

alter table users add column if not exists username text;
create index if not exists idx_users_username_lower on users (lower(username));
