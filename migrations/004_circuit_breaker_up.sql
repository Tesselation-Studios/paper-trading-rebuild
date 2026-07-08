-- Migration 004: Circuit breaker support — add tool_call tracking columns to risk_state
-- Supports #36: Circuit breakers for OpenClaw agent tool loops
--
-- The risk_state table already has is_paused, paused_reason, and paused_at
-- from the live_schema.sql baseline. This migration adds tool-call loop
-- tracking columns for finer-grained diagnostics and trip history.

ALTER TABLE IF EXISTS trading.risk_state
    ADD COLUMN IF NOT EXISTS tool_call_count_24h INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_tool_call_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS trip_count_24h INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_trip_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_trip_reason TEXT,
    ADD COLUMN IF NOT EXISTS cooldown_minutes INTEGER NOT NULL DEFAULT 5;
