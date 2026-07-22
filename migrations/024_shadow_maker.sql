-- Migration 024: BP30.3 virtual maker lifecycle. Idempotent.
alter table shadow_trades
    drop constraint if exists shadow_trades_status_check;

alter table shadow_trades
    add constraint shadow_trades_status_check
    check (status in ('open', 'win', 'loss', 'void', 'unfilled'));

alter table shadow_trades
    add column if not exists placed_at timestamptz;

alter table shadow_trades
    add column if not exists note text;
