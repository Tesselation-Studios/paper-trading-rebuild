-- Trader config table for per-agent exploration mode + runtime configuration
-- Allows external agents (Hermes) and the data bus to read/update trader params
-- without modifying agent prompt files or HEARTBEAT.md.

CREATE TABLE IF NOT EXISTS trading.trader_config (
    agent_id TEXT PRIMARY KEY,
    exploration_mode BOOLEAN DEFAULT false,
    exploration_started_at TIMESTAMPTZ,
    max_position_pct FLOAT DEFAULT 25.0,
    conviction_threshold FLOAT DEFAULT 0.6,
    watchlist_size INT DEFAULT 20,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Trigger to auto-update updated_at on row modification
CREATE OR REPLACE FUNCTION trading.update_trader_config_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_trader_config_updated_at ON trading.trader_config;
CREATE TRIGGER trg_trader_config_updated_at
    BEFORE UPDATE ON trading.trader_config
    FOR EACH ROW
    EXECUTE FUNCTION trading.update_trader_config_updated_at();