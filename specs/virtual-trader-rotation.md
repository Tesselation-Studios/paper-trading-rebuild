# SPEC: Virtual Trader Rotation System

> **Status:** Planning | **Agent:** Hermes 🪽 | **Date:** 2026-07-07

---

## Concept

Each live trader maintains a **stable of virtual traders** (8-10 each). Virtuals trade on paper during market hours. Every night, the best virtual is selected to become tomorrow's live trader. Weak performers are periodically culled and replaced with new variants.

This replaces the complex gradient descent + prompt sweep pipeline with a simpler evolutionary approach: **generate variants, measure P&L, keep winners.**

---

## How It Works

### Daily cycle

```
09:30 ET — Market opens
  → Yesterday's winner is today's LIVE trader (Kairos, Aldridge, Stonks)
  → All virtuals start trading on paper

09:30–16:00 ET — Trading day
  → LIVE trader: real orders via Alpaca
  → Virtuals: paper trades only, logged to Postgres (trade_source='virtual')
  → Every 5 min: data bus snapshot → each trader decides

16:00 ET — Market closes
  → All positions marked
  → P&L computed for live trader AND all virtuals

20:00 ET — Selection
  → Rank virtuals by today's P&L
  → The #1 virtual becomes tomorrow's LIVE trader
  → Its parameters/prompt are deployed to the live config
  → Old live config becomes a virtual (or gets culled if bottom-ranked)

Sunday 23:00 ET — Culling
  → Rank ALL virtuals by 7-day rolling P&L
  → Bottom 3 get culled
  → 3 new variants generated from the top performer
  → Total virtual count stays at 8-10
```

### Visual

```
Monday:    [V1] [V2] [V3] [V4] [V5] [V6] [V7] [V8]  ← all virtuals
           └───────── LIVE: V3 ─────────┘              ← yesterday's winner

Monday night:
  Rank by P&L: V5 > V1 > V3 > V7 > V2 > V8 > V6 > V4
                                    └── culled ──┘

Tuesday:   [V1] [V3] [V7] [V2] [Vnew] [Vnew] [Vnew]
           └─── LIVE: V5 ───┘                          ← promoted

... repeat daily
```

---

## Variant Types

Each virtual trader is one of:

| Type | Example | What changes |
|------|---------|-------------|
| **Param variant** | `kairos-rsi-20` | One or two signal engine params tweaked |
| **Prompt variant** | `kairos-prompt-v7` | Different system prompt (from sweep) |
| **Regime variant** | `kairos-trending-only` | Same params but only trades TRENDING_UP |
| **Portfolio variant** | `kairos-5-positions` | Different max positions or sizing |

Initial set per trader (8 virtuals):

```
Kairos (momentum):
  kairos-looser    — RSI oversold 20 (easier entries)
  kairos-tighter   — RSI oversold 35 (harder entries)
  kairos-aggro     — momentum threshold 0.40
  kairos-patient   — momentum threshold 0.65
  kairos-wide      — stop_loss 0.08
  kairos-tight     — stop_loss 0.03
  kairos-big       — base_size 0.25
  kairos-small     — base_size 0.08
```

---

## Selection Rules

### Daily scoring — every trader gets a daily win/loss

```
At 20:00 ET, after all P&L is computed:
  1. Rank ALL traders (main + all virtuals) by today's P&L
  2. The #1 trader gets a WIN for today
  3. Everyone else gets a LOSS
  4. Win counts are cumulative — tracked in the virtual_traders table
```

### Promotion — championship belt model

```
IF challenger.wins > main.wins:
    → ROTATE: challenger takes the belt, becomes main
    → Old main becomes a virtual, keeps its win count
ELSE:
    → KEEP: main stays. Even if it lost today, its track record holds.
```

### Why this works

```
Day 1:  Main wins.     Main: 1-0,  V1: 0-1,  V2: 0-1...
Day 2:  V4 wins.       Main: 1-1,  V4: 1-1
Day 3:  V4 wins again. Main: 1-2,  V4: 2-1  ← V4 now leads!
        → V4 PROMOTED. V4 becomes main. Old main becomes virtual.
        → Win counts: Main(V4): 2-1, Old: 1-2

Day 4:  Old main wins. Main(V4): 2-2, Old: 2-2  ← tied, no rotation
Day 5:  Old main wins. Main(V4): 2-3, Old: 3-2  ← Old reclaims the belt!
```

The belt only moves when someone has *earned* it over multiple days. One lucky day doesn't flip the system.

### Reset on new virtuals

```
When a virtual is culled and replaced:
  → New virtual starts at 0-0
  → Main's win count is NOT reset
  → New virtual must earn wins from scratch before challenging
```

### Edge cases

```
Tie (same P&L):     Main keeps the win (incumbent advantage)
Main has 0 trades:  No win awarded to anyone that day
All negative P&L:   Fewest losses gets the win (survival, not profit)
Main <3 days old:   No promotion allowed (let the new main prove itself first)
```

### Weekly culling (Sunday)

```
1. Rank all virtuals by 7-day rolling P&L
2. Bottom 3: culled (removed from rotation)
3. Generate 3 new variants:
   a. 1 param variant: random perturbation of the #1 trader's params
   b. 1 prompt variant: latest sweep winner for this trader
   c. 1 wildcard: random new config within safe bounds
4. New virtuals start with a 2-day probation:
   - Day 1-2: paper only, CANNOT be promoted to live
   - Day 3+: eligible for promotion if P&L is positive
```

### Anti-overfitting

```
- A virtual must have >10 closed trades before being promoted (minimum data)
- A virtual that wins 3 days in a row gets "locked" — it can stay LIVE for up to 5 days
  even if another virtual beats it on a single day (reduces thrashing)
- If ALL virtuals have negative P&L for 3 consecutive days:
  → Freeze the system. Revert to baseline config. Alert human.
```

---

## Schema

```sql
-- One new column on existing table
ALTER TABLE trading.executed_trades 
ADD COLUMN trade_source VARCHAR(16) DEFAULT 'live';
-- 'live' = real Alpaca orders, 'virtual' = paper only

-- Virtual trader registry
CREATE TABLE trading.virtual_traders (
  id SERIAL PRIMARY KEY,
  name VARCHAR(64) NOT NULL,           -- 'kairos-looser'
  base_trader VARCHAR(32) NOT NULL,    -- 'kairos'
  variant_type VARCHAR(16) NOT NULL,   -- 'params', 'prompt', 'regime', 'portfolio'
  config JSONB NOT NULL,               -- the overrides
  status VARCHAR(16) DEFAULT 'active', -- 'active', 'live', 'culled', 'probation'
  live_dates DATE[],                   -- dates this virtual was the LIVE trader
  created_at DATE DEFAULT CURRENT_DATE,
  culled_at DATE
);

-- Daily rotation log
CREATE TABLE trading.rotation_log (
  id SERIAL PRIMARY KEY,
  date DATE NOT NULL,
  base_trader VARCHAR(32) NOT NULL,
  live_virtual VARCHAR(64),            -- which virtual was LIVE today
  live_pnl NUMERIC,
  top_virtual VARCHAR(64),             -- which virtual ranked #1
  top_virtual_pnl NUMERIC,
  promoted BOOLEAN,                    -- did we switch?
  reason TEXT                          -- why/why not
);
```

---

## Code

### `src/virtual_runner.py` (~200 lines)

```python
"""
Runs every 5 min during market hours.
1. Fetch data bus snapshot
2. For each active virtual trader:
   a. Load its config overrides
   b. Apply to signal engine
   c. Build prompt with overridden signals
   d. Call LLM (Gemini Flash)
   e. Parse BUY/SELL/HOLD
   f. Log to executed_trades (trade_source='virtual')
3. Also runs the LIVE trader's config for comparison
"""
```

### `src/virtual_rotate.py` (~150 lines)

```python
"""
Runs at 20:00 ET daily.
1. Compute today's P&L for all virtuals + LIVE
2. Rank by P&L
3. Apply promotion rules (safety guard, minimum trades)
4. If rotating: update LIVE trader config, log to rotation_log
5. If keeping: log reason
"""
```

### `src/virtual_cull.py` (~100 lines)

```python
"""
Runs Sunday 23:00 ET.
1. Rank all virtuals by 7-day P&L
2. Cull bottom 3
3. Generate 3 new variants
4. 2-day probation for new ones
"""
```

---

## Rollout

| Step | What | When |
|------|------|------|
| 1 | Schema migration (3 SQL statements) | Day 1 |
| 2 | `virtual_runner.py` — paper trading during hours | Day 2-3 |
| 3 | `virtual_rotate.py` — nightly selection | Day 3-4 |
| 4 | `virtual_cull.py` — weekly cleanup | Day 4 |
| 5 | **Shadow mode: 1 week.** Virtuals run but don't rotate LIVE. Collect data. | Week 2 |
| 6 | Enable rotation. Virtuals can become LIVE. | Week 3 |

**Shadow mode is critical.** Before a virtual ever touches real money, we need proof that the rotation system would have made better decisions than the static trader. If virtual rotation underperforms the status quo during shadow week, we fix the selection rules before going live.

---

## Why This Is Better Than What We Have

| Current (gradient + sweep) | Rotating virtuals |
|---|---|
| Gradient on 10 ticks = 95% noise | Daily P&L on live data = real signal |
| Prompt sweep: 100 variants on 1 day of replay → overfit | 8 variants on live data every day → actual edge |
| Parameters drift slowly, never converge | Worst params die weekly, best survive |
| Complex: gradient math, replay harness, validation gates | Simple: run variants, score P&L, keep winners |
| Learning loop doesn't close | Learning loop closes every night |
| Can't tell if changes help | Every day you see who won |
