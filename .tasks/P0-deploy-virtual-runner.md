# P0: Deploy virtual_runner — get virtual traders executing every 5 min

> **From:** Hermes 🪽 | **Priority:** P0 | **Depends on:** Nothing

## What
Casper built `src/virtual_runner.py` (723 lines). It reads virtual trader configs from Postgres, calls signal engine + LLM, and logs decisions as `trade_source='virtual'`. It's never been deployed.

## Steps
1. Verify virtual_runner.py works with `--once` flag against 1 virtual
2. Deploy to docker.klo as a cron or persistent process
3. Configure to run every 5 min during market hours (09:30-16:00 ET)
4. Verify virtual trades appear in `trading.trades` with `trade_source='virtual'`
5. Fix model ID: `google/gemini-2.5-flash-lite` → `google/gemini-3.5-flash`

## Files
- `src/virtual_runner.py` — Casper's code, needs model fix + deployment
- `trading.virtual_traders` — 5 Kairos variants registered, ready to go

## Verification
```sql
SELECT trade_source, COUNT(*) FROM trading.trades 
WHERE created_at >= CURRENT_DATE GROUP BY trade_source;
```
— should show 'virtual' count > 0
