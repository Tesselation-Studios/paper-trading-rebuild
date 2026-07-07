-- Migration 003: Enhanced parameter history with before/after scores
-- Supports #23: Parameter history tracking with convergence/oscillation detection

CREATE SCHEMA IF NOT EXISTS trading;

CREATE TABLE IF NOT EXISTS trading.param_history (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL DEFAULT 'default',
    param_name      TEXT NOT NULL,
    old_value       DOUBLE PRECISION,
    new_value       DOUBLE PRECISION,
    before_score    DOUBLE PRECISION,   -- objective score before change
    after_score     DOUBLE PRECISION,   -- objective score after change (or NULL if not yet measured)
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source          TEXT NOT NULL DEFAULT 'manual',  -- gradient_descent, prompt_sweep, manual, auto_promote
    reason          TEXT DEFAULT '',
    trader_id       TEXT DEFAULT '',     -- which trader (kairos, aldridge, stonks)
    score_metric    TEXT DEFAULT 'calmar' -- which objective metric was used (calmar, sharpe, sortino, pf)
);

CREATE INDEX IF NOT EXISTS idx_param_history_agent_param
    ON trading.param_history (agent_id, param_name, changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_param_history_trader
    ON trading.param_history (trader_id, changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_param_history_source
    ON trading.param_history (source, changed_at DESC);
