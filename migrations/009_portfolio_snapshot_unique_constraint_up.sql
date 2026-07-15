-- Migration 009: Add UNIQUE constraint on portfolio_snapshots (agent_id, timestamp)
-- Issue #134
--
-- Step 1: Deduplicate existing rows — keep the latest row per (agent_id, timestamp)
-- Step 2: Drop the non-unique index (superseded by the UNIQUE constraint)
-- Step 3: Add the UNIQUE constraint

BEGIN;

-- Step 1: Remove duplicate rows where the same (agent_id, timestamp) appears
-- multiple times. Keep the row with the highest id (most recent insert).
DELETE FROM trading.portfolio_snapshots
WHERE id NOT IN (
    SELECT DISTINCT ON (agent_id, "timestamp")
        id
    FROM trading.portfolio_snapshots
    ORDER BY agent_id, "timestamp", id DESC
);

-- Step 2: Drop the old non-unique index (it's redundant once we have a UNIQUE constraint)
DROP INDEX IF EXISTS trading.idx_portfolio_snap_agent_ts;

-- Step 3: Add the UNIQUE constraint
ALTER TABLE trading.portfolio_snapshots
    ADD CONSTRAINT uq_portfolio_snap_agent_ts UNIQUE (agent_id, "timestamp");

COMMIT;
