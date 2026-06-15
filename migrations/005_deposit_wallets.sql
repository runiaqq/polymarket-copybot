-- Migration 005: per-user deposit wallets (Polymarket V2 / POLY_1271 trading)
-- Run once in the Supabase SQL editor. Safe to re-run (idempotent).

-- Each user trades through a deterministic deposit wallet (ERC-1967 proxy owned
-- by their EOA). It holds the pUSD collateral and is the order `funder`.
alter table users add column if not exists deposit_wallet_address text;
alter table users add column if not exists deposit_wallet_deployed boolean not null default false;

create index if not exists idx_users_deposit_wallet on users (deposit_wallet_address);
