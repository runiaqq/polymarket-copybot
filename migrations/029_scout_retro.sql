-- Migration 029: Blueprint 53 — donor scout retro-scoring.
-- Idempotent.
--
-- Retro fields: would-be copy result of the wallet's OWN 30d BUY history
-- (Data API) against market resolutions, at the nominal probation stake.
-- Replaces "wait a week of live probation to see zeros" as the primary
-- ranking: only retro-profitable wallets get probation seats.

alter table donor_candidates add column if not exists retro_trades integer not null default 0;
alter table donor_candidates add column if not exists retro_resolved integer not null default 0;
alter table donor_candidates add column if not exists retro_wins integer not null default 0;
alter table donor_candidates add column if not exists retro_pnl numeric not null default 0;
alter table donor_candidates add column if not exists retro_median_price double precision;
