# P1: Build meta-cog loop — self-improvement for the learning loop itself

> **From:** Hermes 🪽 | **Priority:** P1 | **Depends on:** P1-wire-learning-loop-pg
> **Status:** 🔴 DEFERRED — depends on PG wiring (also deferred).

## What
The learning loop optimizes trader parameters. But who watches the watcher? The meta-cog loop tracks whether learning loop changes actually improve P&L, and tunes the learning loop's own hyperparameters (gradient step size, sweep frequency, promotion thresholds).

## Architecture
```
Traders trade → decisions + P&L logged
       ↓
Learning loop: grade → analyze → optimize → propose param changes
       ↓
Meta-cog loop: did the last N param changes improve P&L?
       ├─ YES → continue, maybe increase step size
       └─ NO  → rollback, shrink step size, alert
```

## Steps
1. Create `src/meta_cog.py` — queries `trading.param_history` + `trading.trades`
2. Track: for each param change, what happened to P&L in the next 10 trades?
3. Compute: rolling win rate of learning loop proposals (how often did they help?)
4. Auto-tune: if win rate < 40%, halve gradient step size. If > 70%, increase 10%.
5. Alert: if 5 consecutive changes made things worse, freeze learning loop + notify
6. Add to Canvas: meta-cog dashboard card showing learning loop effectiveness

## Metrics to track
- **Loop win rate**: % of param changes that improved subsequent P&L
- **Time to improvement**: median ticks before P&L improved after a change
- **Oscillation count**: params that flip-flop (increase → decrease → increase)
- **Stuck detection**: no param changes in 2+ trading days

## Verification
```sql
-- Did the last parameter change help?
SELECT ph.param_name, ph.old_value, ph.new_value, ph.changed_at,
       (SELECT AVG(pnl) FROM trading.trades 
        WHERE entry_time > ph.changed_at 
        AND entry_time < ph.changed_at + INTERVAL '1 hour') as post_change_pnl
FROM trading.param_history ph
ORDER BY ph.changed_at DESC LIMIT 5;
```
