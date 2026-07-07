-- ============================================================================
-- Paper Trading Rebuild — Postgres Database Schema
-- All tables are append-only (immutable rows), SERIAL/BIGSERIAL PKs,
-- every table has `created_at TIMESTAMPTZ DEFAULT NOW()`.
--
-- Schemas:
--   market_data — bars, news, fundamentals, market regime snapshots
--   trading     — signals, decisions, trades, journal, params, sweeps, equity
--
-- Last synced with live Postgres: 2026-07-06 (commit 859a7c4)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Schema: market_data
-- ----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS market_data;

-- Market price bars (OHLCV)
CREATE TABLE IF NOT EXISTS market_data.bars (
    id          BIGSERIAL PRIMARY KEY,
    ticker      VARCHAR(10)     NOT NULL,
    timestamp   TIMESTAMPTZ     NOT NULL,
    open        DECIMAL         NOT NULL,
    high        DECIMAL         NOT NULL,
    low         DECIMAL         NOT NULL,
    close       DECIMAL         NOT NULL,
    volume      BIGINT          NOT NULL,
    interval    VARCHAR(10)     NOT NULL,    -- e.g. 1m, 5m, 1h, 1d
    source      VARCHAR(32),                 -- yahoo, alpaca, polygon
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bars_ticker_ts
    ON market_data.bars (ticker, timestamp);

CREATE INDEX IF NOT EXISTS idx_bars_ts
    ON market_data.bars (timestamp);

-- Fundamentaldaten (P/E, P/B, market cap, etc.) — Yahooquery source
CREATE TABLE IF NOT EXISTS market_data.fundamentals (
    id               SERIAL PRIMARY KEY,
    ticker           VARCHAR(10)     NOT NULL,
    pe_ratio         DOUBLE PRECISION,
    pb_ratio         DOUBLE PRECISION,
    market_cap       DOUBLE PRECISION,
    dividend_yield   DOUBLE PRECISION,
    earnings_growth  DOUBLE PRECISION,
    revenue_growth   DOUBLE PRECISION,
    debt_to_equity   DOUBLE PRECISION,
    free_cash_flow   DOUBLE PRECISION,
    sector           VARCHAR(128),
    industry         VARCHAR(128),
    fetched_at       TIMESTAMPTZ     NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fundamentals_ticker
    ON market_data.fundamentals (ticker, fetched_at);

-- News headlines / articles with optional sentiment
CREATE TABLE IF NOT EXISTS market_data.news (
    id           BIGSERIAL PRIMARY KEY,
    source       VARCHAR(32),                   -- finnhub, reddit, bluesky
    url_hash     VARCHAR(64)    NOT NULL,       -- dedup key for crawled articles
    ticker       VARCHAR(10)    NOT NULL,
    title        TEXT           NOT NULL,
    body         TEXT,
    sentiment    DECIMAL,                       -- -1.0 to 1.0
    published_at TIMESTAMPTZ   NOT NULL,
    fetched_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_news_ticker_pub
    ON market_data.news (ticker, published_at);

-- Market regime snapshots (one per date)
CREATE TABLE IF NOT EXISTS market_data.regimes (
    id             BIGSERIAL PRIMARY KEY,
    date           DATE            NOT NULL,
    regime         VARCHAR(32)     NOT NULL, -- e.g. bull, bear, sideways, volatile
    confidence     DECIMAL         NOT NULL, -- 0.0–1.0
    spy_return     DECIMAL,                   -- SPY daily return
    vix_level      DECIMAL,                   -- VIX close
    adx            DECIMAL,                   -- ADX value
    features       JSONB,                     -- raw feature vector for audit
    created_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_regimes_date UNIQUE (date)
);

-- ----------------------------------------------------------------------------
-- Schema: trading
-- ----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS trading;

-- Composite signal per trader-ticker-timestamp
CREATE TABLE IF NOT EXISTS trading.signals (
    id               BIGSERIAL PRIMARY KEY,
    trader_id        VARCHAR(32)    NOT NULL,
    ticker           VARCHAR(10)    NOT NULL,
    timestamp        TIMESTAMPTZ    NOT NULL,
    composite_signal DECIMAL        NOT NULL,  -- overall signal (-1..+1)
    conviction       DECIMAL        NOT NULL,  -- 0..1
    momentum         DECIMAL,
    rsi              DECIMAL,
    volatility       DECIMAL,
    regime           VARCHAR(32),
    raw_params       JSONB,                    -- snapshot of SignalParams used
    created_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signals_trader_ts
    ON trading.signals (trader_id, timestamp);

-- Discrete trading decisions (BUY, SELL, HOLD, etc.)
CREATE TABLE IF NOT EXISTS trading.decisions (
    id                BIGSERIAL PRIMARY KEY,
    trader_id         VARCHAR(32)    NOT NULL,
    ticker            VARCHAR(10)    NOT NULL,
    timestamp         TIMESTAMPTZ    NOT NULL,
    decision          VARCHAR(16)    NOT NULL,  -- BUY, SELL, HOLD, EXIT
    conviction        DECIMAL        NOT NULL,  -- 0..1
    rationale         TEXT,
    prompt_variant_id INTEGER,
    params_hash       VARCHAR(64),
    signal_id         INTEGER,                  -- FK to trading.signals
    created_at        TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_decisions_trader_ts
    ON trading.decisions (trader_id, timestamp);

-- Completed (closed) trades — append-only audit log
CREATE TABLE IF NOT EXISTS trading.trades (
    id              BIGSERIAL PRIMARY KEY,
    trader_id       VARCHAR(32)    NOT NULL,
    trade_id        VARCHAR(64)    NOT NULL,
    ticker          VARCHAR(10)    NOT NULL,
    entry_time      TIMESTAMPTZ    NOT NULL,
    exit_time       TIMESTAMPTZ,
    entry_price     DECIMAL        NOT NULL,
    exit_price      DECIMAL,
    shares          INTEGER        NOT NULL,
    pnl             DECIMAL,
    return_pct      DECIMAL,
    buy_decision_id INTEGER,                   -- FK to trading.decisions
    sell_decision_id INTEGER,                  -- FK to trading.decisions
    regime          VARCHAR(32),               -- regime at entry
    created_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_trades_trade_id UNIQUE (trade_id)
);

CREATE INDEX IF NOT EXISTS idx_trades_trader_entry
    ON trading.trades (trader_id, entry_time);

CREATE INDEX IF NOT EXISTS idx_trades_ticker_entry
    ON trading.trades (ticker, entry_time);

-- Journal — per-decision equity & drawdown snapshot
CREATE TABLE IF NOT EXISTS trading.journal (
    id           BIGSERIAL PRIMARY KEY,
    trader_id    VARCHAR(32)    NOT NULL,
    timestamp    TIMESTAMPTZ    NOT NULL,
    ticker       VARCHAR(10)    NOT NULL,
    decision     VARCHAR(16)    NOT NULL,
    rationale    TEXT,
    equity       DECIMAL        NOT NULL,
    drawdown_pct DECIMAL        NOT NULL,
    decision_id  INTEGER,                    -- FK to trading.decisions
    created_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_journal_trader_ts
    ON trading.journal (trader_id, timestamp);

-- Tuneable parameters (persisted per-trader)
CREATE TABLE IF NOT EXISTS trading.params (
    id           BIGSERIAL PRIMARY KEY,
    trader_id    VARCHAR(32)    NOT NULL,
    param_name   VARCHAR(64)    NOT NULL,
    param_value  DECIMAL        NOT NULL,
    min_val      DECIMAL        NOT NULL,
    max_val      DECIMAL        NOT NULL,
    updated_by   VARCHAR(32)    NOT NULL,    -- trader_id or "system"
    sweep_run_id INTEGER,                    -- FK to trading.sweep_runs
    created_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_params_trader_name UNIQUE (trader_id, param_name)
);

-- Sweep runs (hyper-parameter search meta-data)
CREATE TABLE IF NOT EXISTS trading.sweep_runs (
    id               BIGSERIAL PRIMARY KEY,
    run_id           SERIAL UNIQUE,              -- legacy id for backward compat
    trader_id        VARCHAR(32)    NOT NULL,
    started_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    finished_at      TIMESTAMPTZ,
    n_scenarios      INTEGER        NOT NULL DEFAULT 0,
    n_completed      INTEGER        NOT NULL DEFAULT 0,
    n_failed         INTEGER        NOT NULL DEFAULT 0,
    best_score       DECIMAL,
    best_variant_id  INTEGER,
    best_params_hash VARCHAR(64),
    model_used       VARCHAR(32),               -- which LLM model
    data_days        INTEGER,                   -- how many days of data
    created_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sweep_runs_trader_start
    ON trading.sweep_runs (trader_id, started_at);

-- Sweep results — one row per variant per run
CREATE TABLE IF NOT EXISTS trading.sweep_results (
    id               BIGSERIAL PRIMARY KEY,
    run_id           INTEGER         NOT NULL REFERENCES trading.sweep_runs(id),
    trader_id        VARCHAR(32)     NOT NULL,
    variant_id       INTEGER         NOT NULL,
    params_hash      VARCHAR(64)     NOT NULL,
    objective_score  DECIMAL,                    -- composite optimization score
    calmar           DECIMAL,
    sortino          DECIMAL,
    profit_factor    DECIMAL,
    expectancy       DECIMAL,
    total_pnl        DECIMAL,
    total_return_pct DECIMAL,                   -- % return over sweep window
    max_drawdown     DECIMAL,                   -- % max drawdown
    n_ticks          INTEGER,
    n_trades         INTEGER,
    win_rate         DECIMAL,
    elapsed_s        DECIMAL,                   -- wall-clock seconds
    model_used       VARCHAR(32),               -- which LLM model
    journal_sample   TEXT,                      -- sample journal output
    validation_meta  JSONB,                     -- two-phase validation metadata
    created_at       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_sweep_results_run_variant UNIQUE (run_id, variant_id)
);

-- Equity snapshots — daily portfolio state
CREATE TABLE IF NOT EXISTS trading.equity_snapshots (
    id             BIGSERIAL PRIMARY KEY,
    trader_id      VARCHAR(32)    NOT NULL,
    date           DATE           NOT NULL,
    equity         DECIMAL        NOT NULL,
    cash           DECIMAL        NOT NULL,
    pnl            DECIMAL        NOT NULL DEFAULT 0,
    calmar_30d     DECIMAL,
    calmar_90d     DECIMAL,
    sortino_30d    DECIMAL,
    sharpe_30d     DECIMAL,
    profit_factor  DECIMAL,
    win_rate       DECIMAL,
    max_drawdown   DECIMAL,
    trades_closed  INTEGER        NOT NULL DEFAULT 0,
    trades_won     INTEGER        NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_equity_snap_trader_date UNIQUE (trader_id, date)
);
