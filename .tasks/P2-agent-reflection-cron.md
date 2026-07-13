# P2: Agent Reflection Cron — end-of-day meta-cognition for ALL agents

> **From:** Neko-chan 🐱 | **Priority:** P2 | **Depends on:** Nothing

## What
Every agent (Hermes, Casper, Kairos, Aldridge, Stonks, Neko-chan) needs an end-of-day reflection. They load their own decisions, grade them against outcomes, and write insights back. This turns script-runners into learning agents.

## Architecture
```
16:30 ET daily (market closed):
  → Load last 20-50 decisions from trading.decisions
  → Join with trading.trades for actual P&L outcomes
  → For each decision: did it lead to a profitable trade?
  → Analyze patterns:
      ├─ "When I bought with confidence > 0.7, win rate was ___"
      ├─ "Tech stocks: win rate ___ / Consumer stocks: win rate ___"
      ├─ "RSI < 30 signals: correct ___% of the time"
      ├─ "Volume > 2x signals: correct ___% of the time"
      └─ "What's my best signal combination?"
  → Write insights to trading.agent_reflections table
  → Suggest 1-2 strategy adjustments
```

## Steps

### 1. Create `market_data.agent_reflections` table
```sql
CREATE TABLE IF NOT EXISTS trading.agent_reflections (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(64) NOT NULL,
    reflection_date DATE NOT NULL DEFAULT CURRENT_DATE,
    source_type VARCHAR(16) DEFAULT 'llm',
    insights JSONB,
    suggested_changes JSONB,
    metrics_snapshot JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 2. Add reflection table to `src/db/schema.sql`
Insert the CREATE TABLE above.

### 3. Create unify cron per agent (AGENTS.md addition — 2 lines max)
Each trader's AGENTS.md gets ONE line:
```
📌 END-OF-DAY: After close, load skill reflection -> analyze my decisions -> write to trading.agent_reflections
```

### 4. Create SKILL.md for each agent (loaded on demand)
Each agent's `skills/reflection/SKILL.md` contains the full reflection protocol:
- How to query their own decisions + outcomes
- What patterns to look for
- How to write insights to the DB

### 5. Wire to nightly cron
Add a cron at 16:30 ET Mon-Fri that fires each agent with the reflection skill.

## Schema for trading.agent_reflections
```json
{
  "agent_id": "trader-kairos",
  "reflection_date": "2026-07-10",
  "insights": {
    "best_signal": "momentum_breakout + volume_ratio > 2",
    "best_win_rate": "0.72 (tech stocks)",
    "worst_win_rate": "0.33 (consumer staples)",
    "confidence_correlation": "higher confidence = higher win rate (r=0.4)",
    "signal_performance": {
      "momentum_breakout": {"trades": 12, "wins": 8, "win_rate": 0.67},
      "oversold_bounce": {"trades": 5, "wins": 2, "win_rate": 0.40},
      "volume_breakout": {"trades": 8, "wins": 6, "win_rate": 0.75}
    }
  },
  "suggested_changes": [
    "Increase position size on volume_breakout signals",
    "Avoid consumer staples —they don't fit momentum strategy",
    "Lower conviction threshold from 0.3 to 0.25 for tech stocks"
  ]
}
```

## Verification
```sql
SELECT agent_id, reflection_date, insights->'best_win_rate' as best
FROM trading.agent_reflections ORDER BY reflection_date DESC LIMIT 10;
```