-- Migration 020: Blueprint 29 — record CLOB taker fees when returned. Idempotent.
alter table copy_trades add column if not exists fee_usdc numeric null;
