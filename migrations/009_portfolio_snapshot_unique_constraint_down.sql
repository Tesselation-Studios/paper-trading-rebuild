-- Migration 009: Rollback — drop UNIQUE constraint, restore non-unique index

BEGIN;

ALTER TABLE trading.portfolio_snapshots
    DROP CONSTRAINT IF EXISTS uq_portfolio_snap_agent_ts;

CREATE INDEX IF NOT EXISTS idx_portfolio_snap_agent_ts
    ON trading.portfolio_snapshots (agent_id, "timestamp");

COMMIT;
