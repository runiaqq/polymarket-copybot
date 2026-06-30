-- Production Signal-Only Mode + Subscription Enforcer.
-- Apply in the Supabase SQL editor BEFORE deploying the matching code.

alter table users add column if not exists is_signal_only boolean default false;
  -- true  = deliver signals only; the bot NEVER places on-chain orders for this user.
  --         Open positions (if any) are still monitored & exited by sync_positions.
  -- false = normal custodial copy-trading (default).

alter table users add column if not exists subscription_notified_expired boolean default false;
  -- true  = the "subscription expired" alert was already sent — prevents per-trade /
  --         per-cron spam while the subscription stays expired.
  -- Reset to false automatically whenever an ACTIVE subscription is observed
  -- (subscription guard in execute_copy) or on renewal (set_subscription).
