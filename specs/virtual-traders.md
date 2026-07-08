# Virtual Traders — Simple Spec

Each live trader gets 5-10 shadows. Each shadow is one param change or prompt variant. They trade on paper during market hours, log everything. Weekly: keep the top 3, replace the bottom 3 with new variants.

---

## Schema

One new column on `executed_trades`:
```sql
ALTER TABLE trading.executed_trades ADD COLUMN trade_source VARCHAR(16) DEFAULT 'live';
-- 'live' = real money, 'virtual' = shadow trader
```

One new table:
```sql
CREATE TABLE trading.virtual_traders (
  id SERIAL PRIMARY KEY,
  name VARCHAR(64),              -- 'kairos-looser-rsi'
  base_trader VARCHAR(32),       -- 'kairos'
  variant_type VARCHAR(16),      -- 'params' or 'prompt'
  variant JSONB,                 -- the actual override
  created_at DATE DEFAULT now(),
  status VARCHAR(8) DEFAULT 'active'  -- 'active', 'promoted', 'culled'
);
```

## How it works

```
Every 5 min during market hours:
  1. Data bus snapshot (quotes, signals, regime)
  2. For each virtual trader:
     a. Apply its param overrides to the signal engine
     b. Call LLM with (maybe modified prompt + signal report)
     c. Log BUY/SELL/HOLD + P&L to executed_trades (source='virtual')
  3. That's it. No orders. Just data.
```

**Virtual trader examples:**

| Name | Base | Variant |
|------|------|---------|
| kairos-looser-rsi | Kairos | `{"rsi_oversold": 20}` (easier entries) |
| kairos-tighter-rsi | Kairos | `{"rsi_oversold": 35}` (harder entries) |
| kairos-wider-stops | Kairos | `{"stop_loss_pct": 0.08}` |
| kairos-aggro-size | Kairos | `{"base_size_pct": 0.25}` |
| kairos-prompt-v2 | Kairos | Prompt variant from sweep |
| aldridge-deep-value | Aldridge | `{"pe_max": 10}` |
| aldridge-wide-net | Aldridge | `{"pe_max": 30}` |
| stonks-hype | Stonks | `{"sentiment_threshold": 0.40}` |
| stonks-contrarian | Stonks | `{"sentiment_threshold": 0.75}` |

## Weekly culling (Sunday night)

```
1. Score each virtual trader: total P&L over the week
2. Rank by P&L
3. Top 3 stay
4. Middle 2-4 stay (or get minor tweaks)
5. Bottom 3 get culled — replaced with new variants
6. If the #1 virtual trader outperforms the LIVE trader by >20% for 2 consecutive weeks:
   → promote it (its params become the live config)
   → the old live config becomes a virtual
```

## New code needed

| File | Lines | What |
|------|-------|------|
| `src/virtual_runner.py` | ~150 | Main loop: fetch data, call LLM for each virtual, log |
| `src/virtual_cull.py` | ~80 | Weekly scoring + culling, runs Sunday |
| Schema migration | 1 SQL file | Column + table |

That's it. No tournament engine, no attribution math, no Q-learning. Just: run variants, score by P&L, keep winners, kill losers.
