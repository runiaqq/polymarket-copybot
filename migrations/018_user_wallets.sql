-- Migration 018: multi-wallet support (Blueprint 24)
-- Named, switchable, multiple deposit wallets per Telegram user.
-- Run once in the Supabase SQL editor. Safe to re-run (idempotent).

-- One row per named wallet. Mirrors the wallet-related columns that used to live
-- only on `users`; `users.*` continues to hold the ACTIVE wallet (see BP24).
create table if not exists user_wallets (
    id                      bigserial primary key,
    user_id                 bigint not null references users(id) on delete cascade,
    name                    text   not null,
    wallet_address          text   not null,
    wallet_private_key_enc  text   not null,
    deposit_wallet_address  text,
    deposit_wallet_deployed boolean not null default false,
    wallet_registered       boolean not null default false,
    clob_api_key            text,
    clob_secret             text,
    clob_passphrase         text,
    balance_usdc            double precision default 0,
    is_active               boolean not null default false,
    created_at              timestamptz not null default now()
);

create index if not exists idx_user_wallets_user    on user_wallets (user_id);
create index if not exists idx_user_wallets_deposit  on user_wallets (deposit_wallet_address);
-- At most one active wallet per user (partial unique index).
create unique index if not exists uq_user_wallets_one_active
    on user_wallets (user_id) where is_active;

-- Fast pointer to the active wallet (kept in sync with user_wallets.is_active).
alter table users add column if not exists active_wallet_id bigint references user_wallets(id);

-- Each copy_trade records which wallet opened it, so exits / redeems sign with the
-- correct key even after the user switches their active wallet.
alter table copy_trades add column if not exists wallet_id bigint references user_wallets(id);
create index if not exists idx_copy_trades_wallet on copy_trades (wallet_id);

-- ── Backfill: fold each existing user's single wallet into a "Wallet 1" row ──
-- Existing users are untouched functionally — their current wallet simply becomes
-- the active "Wallet 1".
insert into user_wallets (
    user_id, name, wallet_address, wallet_private_key_enc,
    deposit_wallet_address, deposit_wallet_deployed, wallet_registered,
    clob_api_key, clob_secret, clob_passphrase, balance_usdc, is_active
)
select
    u.id, 'Wallet 1', u.wallet_address, u.wallet_private_key_enc,
    u.deposit_wallet_address, coalesce(u.deposit_wallet_deployed, false),
    coalesce(u.wallet_registered, false),
    u.clob_api_key, u.clob_secret, u.clob_passphrase,
    coalesce(u.balance_usdc, 0), true
from users u
where u.wallet_address is not null
  and not exists (select 1 from user_wallets w where w.user_id = u.id);

-- Point users.active_wallet_id at the freshly-created Wallet 1.
update users u
set active_wallet_id = w.id
from user_wallets w
where w.user_id = u.id and w.is_active and u.active_wallet_id is null;

-- Stamp existing copy_trades with their user's Wallet 1 id (exits/redeems match).
update copy_trades c
set wallet_id = u.active_wallet_id
from users u
where c.user_id = u.id and c.wallet_id is null and u.active_wallet_id is not null;
