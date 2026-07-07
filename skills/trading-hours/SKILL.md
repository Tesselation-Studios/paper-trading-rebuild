---
name: trading-hours
description: Check if the market is open and handle closed-market behavior
---

# Trading Hours

The market is open 9:30 AM – 4:00 PM ET, Monday–Friday. No trading on weekends or NYSE holidays.

## Check

```bash
# Quick check
curl -s "http://localhost:5000/market-regime"
# If response includes "market_closed" or is empty → market is closed

# Verify via script
python3 src/trading_hours.py
```

## Closed-Market Behavior

If market is closed:
1. Reply `HEARTBEAT_OK` immediately
2. No trading, no position checks, no signal pushes
3. Don't call Alpaca API (it will just error)

## Weekend Behavior

Saturday/Sunday: reply `HEARTBEAT_OK` at the top of every heartbeat. Do nothing else.

## Pre-Market / After-Hours

- 9:00-9:30 ET: Prepare watchlist, check overnight news, sync positions
- 4:00-4:30 ET: EOD reflection, journal, learning loop
- Outside these windows with market closed: do nothing
