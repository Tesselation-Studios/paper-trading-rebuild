---
name: trade-execution
description: Execute buy/sell orders and check portfolio on Alpaca paper trading
---

# Trade Execution

Place buy/sell orders and query portfolio via Alpaca paper trading.

## Accounts

| Agent | Account | Env Vars |
|-------|---------|----------|
| Kazas | kairos | `KAIROS_API_KEY`, `KAIROS_SECRET_KEY` |
| Aldridge | aldridge | `ALDRIDGE_API_KEY`, `ALDRIDGE_SECRET_KEY` |
| Stonks | stonks | `STONKS_API_KEY`, `STONKS_SECRET_KEY` |

All scripts validate `PAPER=true` on startup. No live trading.

## Commands

```bash
# Portfolio
python3 src/skill_alpaca.py --account kairos --portfolio

# Buy (stop-loss required)
python3 src/skill_alpaca.py --account kairos --buy TICKER --qty N --stop-loss PRICE

# Sell
python3 src/skill_alpaca.py --account kairos --sell TICKER --qty N

# Sync positions from Alpaca → local DB
python3 src/sync_alpaca_positions.py
```

## Position Sizing

Max 10% of portfolio per position. The script enforces this. If formula yields 0 shares but 1 share ≤ 10%, allow 1 share (prevents sizing paralysis on small accounts).

## Sync Workflow

Every tick, BEFORE making decisions:
```bash
python3 src/sync_alpaca_positions.py
```
This pulls actual Alpaca positions into the local DB so the risk gate has current data.

## Error Responses

| Status | Meaning |
|--------|---------|
| `unauthorized` | Bad API key |
| `risk_limit_exceeded` | Position sizing violation |
| `market_closed` | Outside trading hours |
| `insufficient_funds` | Not enough cash |
| `rejected` | Validation failure (bad ticker, negative qty, etc.) |

## What NOT to Do

- Don't call Alpaca API directly — use the wrapper script
- Don't trade without `--stop-loss`
- Don't trade outside market hours
- Don't exceed 10% per position
