-- Migration 019: Blueprint 26 — sniper-mode donor mirroring. Idempotent.
alter table tracked_wallets add column if not exists mode text not null default 'default';
alter table tracked_wallets add column if not exists allowed_telegram_ids bigint[];
alter table copy_trades    add column if not exists mode text default 'default';
