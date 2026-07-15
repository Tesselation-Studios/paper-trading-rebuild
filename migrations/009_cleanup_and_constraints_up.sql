-- ============================================================================
-- Migration 009: Cleanup + Constraints + PnL Backfill
-- ============================================================================
-- 1. Add unique constraint to portfolio_snapshots (fixes #134)
-- 2. Clean legacy sweep/trial entries from trading.trades (fixes #130)
-- 3. Backfill PnL for trades with entry_price + exit_price (fixes #132, #129)
-- 4. Set default PnL to NULL for open trades (prevent 0.0 masking)
-- ============================================================================

BEGIN;

-- ── 1. Add unique constraint on portfolio_snapshots ─────────────────────────
-- Prevents duplicate snapshots for same agent at same timestamp
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_portfolio_snapshots_agent_ts'
    ) THEN
        -- Remove exact duplicates first (keep latest)
        DELETE FROM trading.portfolio_snapshots a
        USING trading.portfolio_snapshots b
        WHERE a.id < b.id
          AND a.agent_id = b.agent_id
          AND a.timestamp = b.timestamp;

        ALTER TABLE trading.portfolio_snapshots
        ADD CONSTRAINT uq_portfolio_snapshots_agent_ts
        UNIQUE (agent_id, timestamp);
    END IF;
END $$;

-- ── 2. Clean legacy sweep/trial entries from trading.trades ─────────────────
-- These agent_ids are sweep runs, not live traders. They pollute dashboards.
-- Legitimate agent_ids: trader-kairos, trader-aldridge, trader-stonks
DELETE FROM trading.trades
WHERE trader_id NOT IN ('trader-kairos', 'trader-aldridge', 'trader-stonks')
  AND trader_id NOT LIKE 'trader-%';

-- Also clean entries where trader_id is 'kairos', 'aldridge', 'stonks' (bare names)
-- These are from old code paths. Consolidate any remaining data.
UPDATE trading.trades
SET trader_id = 'trader-' || trader_id
WHERE trader_id IN ('kairos', 'aldridge', 'stonks');

-- ── 3. Backfill PnL for closed trades (has both entry and exit prices) ──────
-- Many trades were inserted with pnl=0 and never had exit_price populated.
-- Only backfill trades with valid exit_price + entry_price where pnl is 0 or NULL
UPDATE trading.trades
SET pnl = ROUND((exit_price - entry_price) * shares::numeric, 4),
    return_pct = CASE
        WHEN entry_price > 0
        THEN ROUND(((exit_price - entry_price) / entry_price * 100)::numeric, 4)
        ELSE 0
    END
WHERE exit_price IS NOT NULL
  AND entry_price IS NOT NULL
  AND entry_price > 0
  AND shares > 0
  AND (pnl IS NULL OR pnl = 0);

-- ── 4. Set PnL to NULL for truly open trades (exit_price is NULL) ──────────
-- 0.0 PnL on open trades is misleading — NULL means "not yet realized"
UPDATE trading.trades
SET pnl = NULL
WHERE exit_price IS NULL
  AND pnl = 0;

-- ── 5. Clean legacy decisions entries ───────────────────────────────────────
-- Remove HOLD decisions older than 7 days (keep recent for context)
-- Only keep the most recent HOLD per trader per ticker per day
DELETE FROM trading.decisions
WHERE decision IN ('HOLD', 'hold', 'HOLD_CASH', 'hold_all', 'HOLD_ALL',
                   'PASS', 'NO_ENTRY', 'OBSERVATION', 'OVERVIEW')
  AND timestamp < NOW() - INTERVAL '24 hours';

-- ── 6. Clean trader_id to agent_id in decisions (fixes #135) ────────────────
-- Ensure decisions always use agent_id format (trader-*), not bare names
UPDATE trading.decisions
SET trader_id = 'trader-' || trader_id
WHERE trader_id IN ('kairos', 'aldridge', 'stonks');

-- ── Verify ──────────────────────────────────────────────────────────────────
-- Log what was cleaned
DO $$
DECLARE
    trade_count INTEGER;
    snap_count INTEGER;
    dec_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO trade_count FROM trading.trades
    WHERE trader_id IN ('trader-kairos', 'trader-aldridge', 'trader-stonks');
    SELECT COUNT(*) INTO snap_count FROM trading.portfolio_snapshots;
    SELECT COUNT(*) INTO dec_count FROM trading.decisions
    WHERE trader_id IN ('trader-kairos', 'trader-aldridge', 'trader-stonks');

    RAISE NOTICE 'Migration 009 complete: % trades, % snapshots, % decisions remain',
        trade_count, snap_count, dec_count;
END $$;

COMMIT;
