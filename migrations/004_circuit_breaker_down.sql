-- Migration 004: Down — remove circuit breaker tracking columns
ALTER TABLE IF EXISTS trading.risk_state
    DROP COLUMN IF EXISTS tool_call_count_24h,
    DROP COLUMN IF EXISTS last_tool_call_at,
    DROP COLUMN IF EXISTS trip_count_24h,
    DROP COLUMN IF EXISTS last_trip_at,
    DROP COLUMN IF EXISTS last_trip_reason,
    DROP COLUMN IF EXISTS cooldown_minutes;
