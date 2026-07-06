# Paper Trading Rebuild — Database Layer

Postgres-backed append-only data store for portfolio replay, signal generation,
decision logging, trade tracking, and hyper-parameter sweep results.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Apply schema (idempotent)
python -c "
import asyncio
from src.db.queries import init_schema
asyncio.run(init_schema())
print('Schema applied.')
"

# 3. Quick test
python -c "
import asyncio, decimal, datetime
from src.db.queries import insert_bar, get_bars, init_schema

async def main():
    await init_schema()
    await insert_bar('AAPL', datetime.datetime.now(), decimal.Decimal('150'),
                     decimal.Decimal('152'), decimal.Decimal('149'),
                     decimal.Decimal('151'), 1000000)
    rows = await get_bars('AAPL',
                          datetime.datetime.now() - datetime.timedelta(days=1),
                          datetime.datetime.now())
    print(f'Bars returned: {len(rows)}')

asyncio.run(main())
"
```

## Configuration

Edit `src/db/connection.py` to change the database URL.
Default:

```
postgresql://trader:trader-dev-2026@192.168.1.179:5433/trading
```

## Schema

Two schemas: `market_data` and `trading`.

| Schema       | Table              | Purpose                         |
|-------------|-------------------|---------------------------------|
| market_data | bars              | OHLCV price bars                |
| market_data | news              | News articles with sentiment    |
| market_data | regimes           | Daily market regime snapshots   |
| trading     | signals           | Composite trading signals       |
| trading     | decisions         | Trader decisions (BUY/SELL/HOLD) |
| trading     | trades            | Completed trade audit log       |
| trading     | journal           | Decision journal with equity    |
| trading     | params            | Tuneable trader parameters      |
| trading     | sweep_runs        | Hyper-parameter sweep runs      |
| trading     | sweep_results     | Per-variant sweep metrics       |
| trading     | equity_snapshots  | Daily portfolio state           |

All tables are append-only with `SERIAL`/`BIGSERIAL` primary keys and
`created_at TIMESTAMPTZ DEFAULT NOW()`.

To apply or re-apply the schema: `python -c "import asyncio; from src.db.queries import init_schema; asyncio.run(init_schema())"`

## API

```python
from src.db.queries import (
    # market_data.bars
    insert_bar, insert_bars_batch, get_bars, get_latest_bar,
    # market_data.news
    insert_news, get_news,
    # market_data.regimes
    insert_regime, get_regime,
    # trading.signals
    insert_signal, get_recent_signals,
    # trading.decisions
    insert_decision, get_recent_decisions,
    # trading.trades
    insert_trade, get_open_trades, close_trade, get_trades,
    # trading.journal
    insert_journal_entry,
    # trading.params
    insert_param, get_params,
    # trading.sweep_runs
    insert_sweep_run, complete_sweep_run, get_sweep_runs,
    # trading.sweep_results
    insert_sweep_result,
    # trading.equity_snapshots
    insert_equity_snapshot, get_equity_snapshot, get_equity_history,
    # schema init
    init_schema,
)
```

## Requirements

- Python 3.12+
- asyncpg ≥ 0.29
- Running Postgres instance (defaults to `192.168.1.179:5433`)
