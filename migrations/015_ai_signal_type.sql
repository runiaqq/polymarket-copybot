-- Blueprint 14.B: structured-output AI analysis pipeline.
-- ai_signal_type is the machine tag of the trade's structure (penny_collecting,
-- value_bet, momentum, longshot_size, consensus_stack, coin_flip) returned by the
-- strict-schema LLM call. ai_score / ai_reason already exist on trade_signals.
-- Apply in Supabase SQL editor. Idempotent.

alter table trade_signals add column if not exists ai_signal_type text;
