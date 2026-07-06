-- ============================================================================
-- Live Trading Schema Extension — tables needed for dashboard + live operations
-- Appended to the core rebuild schema (market_data.*, trading.*)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Schema: trading (extensions)
-- ----------------------------------------------------------------------------

-- Agent profile (one row per trader)
CREATE TABLE IF NOT EXISTS trading.agent_profile (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        VARCHAR(32)     NOT NULL,
    name            VARCHAR(128),
    company         VARCHAR(128),
    tagline         TEXT,
    identity        TEXT,
    current_state   VARCHAR(32),
    performance     JSONB,
    strategic_focus TEXT,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_agent_profile_id UNIQUE (agent_id)
);

-- Agent operational state
CREATE TABLE IF NOT EXISTS trading.agent_state (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        VARCHAR(32)     NOT NULL,
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    last_heartbeat  TIMESTAMPTZ,
    last_trade      TIMESTAMPTZ,
    cash            DECIMAL         NOT NULL DEFAULT 10000.00,
    equity          DECIMAL         NOT NULL DEFAULT 10000.00,
    pnl             DECIMAL         NOT NULL DEFAULT 0,
    pnl_pct         DECIMAL         NOT NULL DEFAULT 0,
    positions_count INTEGER         NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_agent_state_id UNIQUE (agent_id)
);

-- Current positions (live, updated in-place)
CREATE TABLE IF NOT EXISTS trading.positions (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        VARCHAR(32)     NOT NULL,
    ticker          VARCHAR(10)     NOT NULL,
    quantity        DECIMAL         NOT NULL,
    entry_price     DECIMAL         NOT NULL,
    current_price   DECIMAL,
    market_value    DECIMAL,
    unrealized_pl   DECIMAL,
    unrealized_plpc DECIMAL,
    opened_at       TIMESTAMPTZ     NOT NULL,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_positions_agent_ticker UNIQUE (agent_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_positions_agent
    ON trading.positions (agent_id);

-- Executed (closed) trades — migrated from SQLite trades table
CREATE TABLE IF NOT EXISTS trading.executed_trades (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        VARCHAR(32)     NOT NULL,
    ticker          VARCHAR(10)     NOT NULL,
    action          VARCHAR(8)      NOT NULL,  -- buy, sell
    quantity        DECIMAL         NOT NULL,
    entry_price     DECIMAL         NOT NULL,
    exit_price      DECIMAL,
    entry_time      TIMESTAMPTZ     NOT NULL,
    exit_time       TIMESTAMPTZ,
    exit_reason     VARCHAR(64),
    pnl             DECIMAL,
    pnl_pct         DECIMAL,
    status          VARCHAR(16)     NOT NULL DEFAULT 'open',  -- open, closed
    decision_id     INTEGER,
    entry_reason    TEXT,
    stop_loss       DECIMAL,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_executed_trades_agent
    ON trading.executed_trades (agent_id, entry_time);

-- Portfolio snapshots (daily or per-tick)
CREATE TABLE IF NOT EXISTS trading.portfolio_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        VARCHAR(32)     NOT NULL,
    timestamp       TIMESTAMPTZ     NOT NULL,
    equity          DECIMAL         NOT NULL,
    cash            DECIMAL         NOT NULL,
    invested        DECIMAL         NOT NULL DEFAULT 0,
    pnl             DECIMAL         NOT NULL DEFAULT 0,
    pnl_pct         DECIMAL         NOT NULL DEFAULT 0,
    positions_json  JSONB,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_portfolio_snap_agent_ts
    ON trading.portfolio_snapshots (agent_id, timestamp);

-- Daily PnL summary (for leaderboard)
CREATE TABLE IF NOT EXISTS trading.daily_pnl (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        VARCHAR(32)     NOT NULL,
    date            DATE            NOT NULL,
    pnl             DECIMAL         NOT NULL DEFAULT 0,
    pnl_pct         DECIMAL         NOT NULL DEFAULT 0,
    start_equity    DECIMAL         NOT NULL,
    end_equity      DECIMAL         NOT NULL,
    trades_count    INTEGER         NOT NULL DEFAULT 0,
    win_count       INTEGER         NOT NULL DEFAULT 0,
    loss_count      INTEGER         NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_daily_pnl_agent_date UNIQUE (agent_id, date)
);

-- Risk state (circuit breaker, veto counts)
CREATE TABLE IF NOT EXISTS trading.risk_state (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        VARCHAR(32)     NOT NULL,
    is_paused       BOOLEAN         NOT NULL DEFAULT FALSE,
    paused_reason   TEXT,
    paused_at       TIMESTAMPTZ,
    veto_count_24h  INTEGER         NOT NULL DEFAULT 0,
    max_drawdown    DECIMAL,
    daily_loss      DECIMAL,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_risk_state_agent UNIQUE (agent_id)
);

-- Orders (pending/filled from Alpaca)
CREATE TABLE IF NOT EXISTS trading.orders (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        VARCHAR(32)     NOT NULL,
    order_id        VARCHAR(64)     NOT NULL,
    ticker          VARCHAR(10)     NOT NULL,
    action          VARCHAR(8)      NOT NULL,
    quantity        DECIMAL         NOT NULL,
    order_type      VARCHAR(16),
    limit_price     DECIMAL,
    status          VARCHAR(16)     NOT NULL,
    filled_qty      DECIMAL         DEFAULT 0,
    filled_avg_price DECIMAL,
    submitted_at    TIMESTAMPTZ,
    filled_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orders_order_id UNIQUE (order_id)
);

-- Sentiment cache (market-wide + per-ticker)
CREATE TABLE IF NOT EXISTS trading.sentiment (
    id              BIGSERIAL PRIMARY KEY,
    source          VARCHAR(32)     NOT NULL,  -- reddit, bluesky, finnhub
    ticker          VARCHAR(10),
    score           DECIMAL         NOT NULL,  -- -1.0 to 1.0
    magnitude       DECIMAL,
    articles_count  INTEGER,
    fetched_at      TIMESTAMPTZ     NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sentiment_ticker_fetched
    ON trading.sentiment (ticker, fetched_at);
