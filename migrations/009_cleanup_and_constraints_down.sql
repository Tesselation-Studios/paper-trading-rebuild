-- ============================================================================
-- Migration 009 Rollback
-- Reverses: unique constraint, cleanup, PnL backfill
-- ============================================================================

BEGIN;

-- Drop unique constraint
ALTER TABLE trading.portfolio_snapshots
DROP CONSTRAINT IF EXISTS uq_portfolio_snapshots_agent_ts;

COMMIT;
