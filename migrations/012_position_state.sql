-- Blueprint 6: position state machine — a token-sale exit is a TERMINAL state.
-- 'status' = 'closed': position was sold before resolution (hard-stop or manual).
-- 'result' documented values: win | loss | closed | null
-- exit_tx: CLOB order ID or tx hash of the closing sale (audit trail).
-- The (user_id, condition_id) index speeds up has_terminal_trade() lookups.
-- Idempotent — safe to run multiple times.

alter table copy_trades add column if not exists exit_tx text;

create index if not exists idx_copy_trades_user_condition
  on copy_trades (user_id, condition_id);
