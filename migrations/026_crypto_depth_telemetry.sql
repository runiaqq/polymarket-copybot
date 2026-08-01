-- Migration 026: Blueprint 36 — capacity telemetry for multi-account scaling.
-- Idempotent. Book-depth bands at signal time (dollars purchasable within the
-- execution price bands), publish->response latency, and whether the fill came
-- from a BP34 re-quote.

alter table crypto_trades add column if not exists depth_best_usdc numeric;
alter table crypto_trades add column if not exists depth_150bp_usdc numeric;
alter table crypto_trades add column if not exists depth_300bp_usdc numeric;
alter table crypto_trades add column if not exists latency_ms integer;
alter table crypto_trades add column if not exists requoted boolean not null default false;
