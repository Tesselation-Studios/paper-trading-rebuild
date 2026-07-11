# P1: Wire learning loop to Postgres — make run_loop() work on live data

> **From:** Hermes 🪽 | **Priority:** P1 | **Depends on:** P0-deploy-virtual-runner
> **Status:** 🔴 DEFERRED — requires Postgres migration. Current stack is SQLite.

## What
`learning_loop.py` (46KB) has `grade_trade()`, `analyze_patterns()`, `optimize_params()`, `run_loop()`. But it connects to old SQLite. Traders produce decisions every 5 min — those need to flow through the loop into parameter improvements.

## Steps
1. Add Postgres connection to learning_loop.py (use `src/db/connection.py` pattern)
2. Wire `run_loop()` to query `trading.decisions` + `trading.trades` from Postgres
3. Run a dry-loop: grade today's decisions, see what insights emerge
4. Add a cron to run `run_loop()` hourly during market hours
5. Verify parameter changes actually get applied to next tick's signal engine

## Key insight
The learning loop has `trade_grader.py` (grades each decision ✓/❌) and `param_optimizer.py` (gradient descent on params). If the loop produces a parameter change, it needs to:
- Write to `trading.param_history`
- Make the new params available to the signal engine on the next tick

## Files
- `src/learning_loop.py` — exists, needs PG connection
- `src/trade_grader.py` — exists, grades decisions
- `src/param_optimizer.py` — exists, gradient descent
- `src/db/connection.py` — PG pool pattern to reuse

## Verification
```sql
SELECT * FROM trading.param_history 
WHERE created_at >= CURRENT_DATE ORDER BY created_at DESC LIMIT 5;
```
— should show parameter changes from the learning loop
