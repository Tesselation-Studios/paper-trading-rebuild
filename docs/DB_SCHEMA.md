# Database Schema

> Postgres database on Docker (.179:5433), database: `trading`, schema: `trading`.

## Connection

```
Host:     192.168.1.179
Port:     5433
Database: trading
User:     trader
Schema:   trading
```

Connection pooling via `asyncpg` (`src/db/connection.py`). DSN format:
```
PG_DSN="host=192.168.1.179 port=5433 dbname=trading user=trader password=trade123"
```

## Migrations

Migrations live in `migrations/` and are applied via `scripts/apply_migrations.py`. Current migration count: 011.

| Migration | Description |
|-----------|-------------|
| 000 | Baseline marker (schema created manually before migrations) |
| 001 | Sentiment JSONB type conversion |
| 002 | Sweep results validation metadata |
| 003 | Parameter history tracking |
| 004 | Circuit breaker state |
| 005 | System parameters |
| 006 | Tick queue + orchestrator log |
| 007 | Promotion log |
| 008a | Trade signals + daily reflections + signal win rates |
| 008b | Promotion tiers + tier snapshots |
| 009a | Cleanup and constraints |
| 009b | Portfolio snapshot unique constraint |
| 010 | Data cleanup — orphaned positions/decisions |
| 011 | Missing tables: agent_reflections, signal_performance, promotion_summary, tier_snapshots |

## Core Tables

### Trading

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `agent_profile` | One row per trader | `agent_id`, `name`, `company`, `performance` (JSONB) |
| `agent_state` | Operational state | `agent_id`, `is_active`, `cash`, `equity`, `pnl`, `last_heartbeat` |
| `positions` | Current open positions | `agent_id`, `ticker`, `quantity`, `entry_price`, `current_price`, `unrealized_pl` |
| `executed_trades` | Closed trades | `agent_id`, `ticker`, `action`, `pnl`, `pnl_pct`, `exit_reason` |
| `portfolio_snapshots` | Daily/tick equity snapshots | `agent_id`, `timestamp`, `equity`, `cash`, `pnl`, `positions_json` |
| `daily_pnl` | Daily P&L summary | `agent_id`, `date`, `pnl`, `pnl_pct`, `trades_count`, `win_count` |
| `orders` | Alpaca order log | `agent_id`, `order_id`, `ticker`, `status`, `filled_qty` |

### Risk

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `risk_state` | Circuit breaker, veto counts | `agent_id`, `is_paused`, `paused_reason`, `max_drawdown`, `veto_count_24h` |

### Learning

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `param_history` | Parameter values over time | `agent_id`, `param_name`, `value`, `validation_score`, `tick_id` |
| `system_params` | Global system parameters | `param_name`, `value`, `description` |
| `tick_queue` | Enqueued tick events | `trader_id`, `tick_time`, `status` (pending/processing/done) |
| `orchestrator_log` | Orchestrator actions | `action`, `trader_id`, `status`, `details` |

### Promotion & Virtual Traders

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `virtual_traders` | Virtual trader registry | `name`, `base_trader`, `tier` (shadow/beta/live), `status` |
| `promotion_log` | Promotion event history | `virtual_trader_id`, `from_tier`, `to_tier`, `reason` |
| `promotion_summary` | Aggregated promotion stats | `base_trader`, `total_promotions`, `promotion_rate` |
| `tier_snapshots` | Daily tier distribution | `snapshot_date`, `tier`, `count` |

### Signals & Analysis

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `trade_signals` | Signal outputs per trade | `signal_name`, `ticker`, `value`, `confidence` |
| `signal_win_rates` | Win rate by signal | `signal_name`, `win_count`, `total_count`, `win_rate` |
| `signal_performance` | Signal accuracy tracking | `trader_id`, `ticker`, `signal_name`, `correct` |
| `daily_reflections` | Daily trader reflections | `trader_id`, `reflection_date`, `reflection_text`, `sentiment` |
| `agent_reflections` | Agent self-reflection | `trader_id`, `reflection_date`, `content`, `type` |

### Data Cache

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `sentiment` | Cached sentiment scores | `source`, `ticker`, `score`, `fetched_at` |
| `bars` | OHLCV bar data | `symbol`, `timestamp`, `open`, `high`, `low`, `close`, `volume` |
| `news` | Cached news articles | `ticker`, `headline`, `source`, `url`, `published_at` |

## Key Constraints

- `uq_agent_profile_id`: One profile per agent
- `uq_agent_state_id`: One state row per agent
- `uq_positions_agent_ticker`: One position per agent per ticker
- `uq_daily_pnl_agent_date`: One P&L row per agent per day
- `uq_risk_state_agent`: One risk state per agent
- `uq_orders_order_id`: No duplicate orders

## Common Queries

### Get trader portfolio snapshot
```sql
SELECT p.ticker, p.quantity, p.entry_price, p.current_price,
       p.unrealized_pl, p.unrealized_plpc
FROM trading.positions p
WHERE p.agent_id = 'kairos';
```

### Get daily P&L history
```sql
SELECT date, pnl, pnl_pct, trades_count, win_count, loss_count
FROM trading.daily_pnl
WHERE agent_id = 'kairos'
ORDER BY date DESC
LIMIT 30;
```

### Get recent trades
```sql
SELECT ticker, action, entry_price, exit_price, pnl, pnl_pct, exit_reason
FROM trading.executed_trades
WHERE agent_id = 'kairos' AND status = 'closed'
ORDER BY exit_time DESC
LIMIT 20;
```

### Get circuit breaker status
```sql
SELECT agent_id, is_paused, paused_reason, max_drawdown, veto_count_24h
FROM trading.risk_state;
```

### Get virtual trader leaderboard
```sql
SELECT name, base_trader, tier, status, sharpe, calmar, profit_factor
FROM trading.virtual_traders
WHERE status = 'active'
ORDER BY sharpe DESC;
```

## Indexes

Key performance indexes:
- `idx_positions_agent` — position lookups by agent
- `idx_executed_trades_agent` — trade history by agent + time
- `idx_portfolio_snap_agent_ts` — portfolio history by agent + time
- `idx_sentiment_ticker_fetched` — sentiment lookups by ticker
- `idx_reflection_trader_date` — reflection lookups by trader + date
- `idx_signal_perf_trader`, `idx_signal_perf_ticker` — signal performance
- `idx_promotion_summary_trader` — promotion stats
- `idx_tier_snapshots_date` — tier history
- `idx_virtual_trader_tier`, `idx_vt_base_tier`, `idx_vt_status_tier` — virtual trader filtering

## Related Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — System architecture and data flow
- [API.md](API.md) — Data bus endpoint reference
- [RUNBOOK.md](RUNBOOK.md) — Operational runbook
