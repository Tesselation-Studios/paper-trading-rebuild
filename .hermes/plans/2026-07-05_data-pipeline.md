# Trading Data Pipeline — Implementation Plan

> **Goal:** Append-only Postgres database + 24/7 data fetcher + simulation engine that reads/writes to the same DB, with seamless swap to live paper trading during market hours.

## Phase 0: Database

Create a dedicated Postgres database `trading` on docker.klo with append-only schema.

### Tables (all append-only, `created_at TIMESTAMPTZ DEFAULT NOW()`)

| Schema | Table | What | Primary Key |
|--------|-------|------|-------------|
| `market_data` | `bars` | OHLCV per ticker per interval | `(ticker, timestamp, interval)` |
| `market_data` | `news` | News articles with sentiment | `(url_hash)` |
| `market_data` | `regimes` | Daily regime classifications | `(date)` |
| `trading` | `signals` | Signal engine output per tick | `(trader_id, ticker, timestamp)` |
| `trading` | `decisions` | Every BUY/SELL/HOLD decision | `(trader_id, timestamp)` |
| `trading` | `trades` | Completed trades | `(trader_id, trade_id)` |
| `trading` | `journal` | Per-tick rationales | `(trader_id, timestamp)` |
| `trading` | `params_history` | Every parameter change with before/after | `(trader_id, param_name, changed_at)` |
| `trading` | `equity_snapshots` | Daily equity snapshots | `(trader_id, date)` |
| `trading` | `sweep_runs` | Simulation batch metadata | `(run_id)` |
| `trading` | `sweep_results` | Per-scenario results | `(run_id, trader_id, variant_id, param_hash)` |

### ML-Tweakable Params Table

Every parameter in the signal engine is stored with bounds, current value, and history:

```sql
CREATE TABLE trading.params (
    trader_id    TEXT NOT NULL,
    param_name   TEXT NOT NULL,  -- e.g. 'momentum_threshold'
    param_value  DOUBLE PRECISION NOT NULL,
    min_val      DOUBLE PRECISION NOT NULL,
    max_val      DOUBLE PRECISION NOT NULL,
    updated_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_by   TEXT,  -- 'gradient_descent', 'sweep', 'human'
    PRIMARY KEY (trader_id, param_name, updated_at)
);
```

## Phase 1: Data Fetcher (24/7)

Refactor existing `fetch.py` → `src/data_fetcher.py`:

- Runs as a cron job or daemon
- Collects: Alpaca bars (1min, 5min, daily), news (Finnhub, Alpha Vantage), fundamentals
- Stores everything in Postgres
- Agents query via the data bus (HTTP API on :5000)
- `SELECT-only` for agents, `INSERT-only` for fetcher
- Historical backfill: fetch 2 years of bars for Kairos (technical data)

## Phase 2: Simulation Engine

Build `src/simulator.py`:

- Reads historical data from Postgres
- Runs prompt × param × regime scenarios
- Calls OpenRouter API per tick (direct, no OpenClaw agent dispatch)
- Writes results to `trading.sweep_results`
- Generates hypothesis for next night from result patterns

## Phase 3: Live Trading Swap

Same code path for simulation and live:

```python
class TradingContext:
    def __init__(self, mode: str, db_conn):
        self.mode = mode  # 'live' | 'simulation'

    def get_data(self, ticker, start, end):
        if self.mode == 'simulation':
            return self._from_db(ticker, start, end)  # historical
        else:
            return self._from_alpaca_live(ticker)      # real-time

    def execute_fill(self, decision):
        if self.mode == 'simulation':
            self._simulate_fill(decision)     # paper fill at close
        else:
            self._alpaca_order(decision)     # real order
```

Swap is a config flag. Same DB, same signal engine, same prompt assembly.

## Files to Create/Modify

| File | Action |
|------|--------|
| `docker.klo:/stacks/trading/docker-compose.yml` | Create Postgres + pgvector container |
| `src/db/schema.sql` | Create all append-only tables |
| `src/db/connection.py` | Connection pool, query helpers |
| `src/data_fetcher.py` | 24/7 data collection daemon |
| `src/simulator.py` | Sweep runner, variant manager |
| `src/hypothesis.py` | Pattern analysis, variant generation |
| `src/prompt_builder.py` | Assemble agent prompt from files + journal + tick |
| `src/llm_engine.py` | OpenRouter API calls |
| `src/trading_context.py` | Live vs simulation mode switch |
| `config/sweep.yaml` | Scenario matrix, cost limits |
