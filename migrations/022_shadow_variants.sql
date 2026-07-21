-- Migration 022: BP30.1 parallel shadow entry-time variants. Idempotent.
alter table shadow_trades
    add column if not exists variant text not null default 'full';

drop index if exists uq_shadow_trades_condition;

create unique index if not exists uq_shadow_trades_condition_variant
    on shadow_trades (condition_id, variant);
