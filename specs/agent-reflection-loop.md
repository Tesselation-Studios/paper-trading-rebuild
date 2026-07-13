# Spec: Agent Reflection & Meta-Cognition Loop

> **META-SPEC**: v0.22
> **Status**: Draft — v1.0.0
> **Author**: Neko-chan 🐱
> **Date**: 2026-07-13

## 1. Purpose

Every agent in the paper trading system (Kairos, Aldridge, Stonks, Neko-chan, Casper, Hermes) needs a standarized end-of-day reflection. They load their own decisions, grade them against outcomes, identify patterns, and write improvement suggestions back to the database. This turns script-running agents into learning agents.

## 2. Data Model

### 2.1 `trading.agent_reflections` table

```sql
CREATE TABLE IF NOT EXISTS trading.agent_reflections (
    id              SERIAL PRIMARY KEY,
    agent_id        VARCHAR(64) NOT NULL,
    reflection_date DATE NOT NULL DEFAULT CURRENT_DATE,
    source_type     VARCHAR(16) DEFAULT 'llm',
    insights        JSONB,        -- {best_signal, worst_signal, confidence_correlation, signal_performance}
    suggested_changes JSONB,      -- [{action, reason, confidence}]
    metrics_snapshot JSONB,       -- snapshot of decisions/trades/win-rate at reflection time
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reflections_agent_date ON trading.agent_reflections(agent_id, reflection_date);
```

### 2.2 `trading.signal_performance` view (optional)

```sql
CREATE OR REPLACE VIEW trading.signal_performance AS
SELECT 
    d.trader_id,
    d.decision_json->>'signals_used' as signal_type,
    COUNT(*) as trades,
    COUNT(CASE WHEN t.pnl > 0 THEN 1 END) as wins,
    ROUND(AVG(t.pnl), 2) as avg_pnl,
    ROUND(COUNT(CASE WHEN t.pnl > 0 THEN 1 END)::float / NULLIF(COUNT(*), 0), 3) as win_rate
FROM trading.decisions d
JOIN trading.trades t ON t.buy_decision_id = d.id
WHERE d.decision = 'BUY'
GROUP BY d.trader_id, d.decision_json->>'signals_used';
```

## 3. Reflection Protocol

Every agent, once per day after market close (16:00+ ET):

### Step 1: Query your decisions
```sql
SELECT id, ticker, decision, conviction, decision_json
FROM trading.decisions 
WHERE trader_id = '{agent_id}' AND timestamp::date = CURRENT_DATE
ORDER BY timestamp;
```

### Step 2: Compute signal-level win rates
For each unique signal type used today:
- count of trades where that signal was the primary trigger
- win rate for those trades
- average P&L per trade

### Step 3: Identify patterns
- Best signal (highest win rate with >3 trades)
- Worst signal (lowest win rate)
- Sector performance (group tickers by first letter or sector DB)
- Time-of-day performance (morning vs afternoon)
- Confidence correlation (does higher conviction = higher win rate?)

### Step 4: Write improvement suggestions
```json
{
  "agent_id": "trader-kairos",
  "insights": {
    "best_signal": "momentum_breakout",
    "best_win_rate": 0.72,
    "best_signal_trades": 12,
    "worst_signal": "oversold_bounce",
    "worst_win_rate": 0.33,
    "confidence_correlation": 0.42,
    "best_sector": "technology",
    "signal_performance": {
      "momentum_breakout": {"trades": 12, "wins": 8, "avg_pnl": 45.20},
      "oversold_bounce": {"trades": 6, "wins": 2, "avg_pnl": -12.50},
      "volume_breakout": {"trades": 8, "wins": 6, "avg_pnl": 38.10}
    }
  },
  "suggested_changes": [
    "Focus on momentum_breakout signals — 72% win rate",
    "Avoid oversold_bounce — only 33% win rate",
    "Increase position size on tech stocks by 10%"
  ]
}
```

### Step 5: Save suggestion as task for the orchestrator
If any change has >70% win rate confidence, write a P3 task to `.tasks/` for review:
```
# P3: [Agent] strategy suggestion — increase momentum_breakout sizing

> **From:** trader-kairos | **Priority:** P3

Based on reflection, momentum_breakout signals have 72% win rate (12 trades).
Suggested change: Increase position sizing on this signal from 2% to 2.5%.
```

## 4. Cron Schedule

- **Trigger**: 16:30 ET daily (Mon-Fri) — 30 min after market close
- **Target**: All trader agents (Kairos, Aldridge, Stonks, Neko-chan, virtuals)
- **Format**: Cron fires agent with `meta-cognition` skill loaded
- **Output**: Write to `trading.agent_reflections` + optional `.tasks/` suggestion

## 5. Verification

```sql
-- Did reflections happen today?
SELECT agent_id, reflection_date, insights->>'best_signal' as best_signal
FROM trading.agent_reflections 
WHERE reflection_date = CURRENT_DATE
ORDER BY agent_id;

-- Is signal_performance view working?
SELECT * FROM trading.signal_performance 
WHERE trader_id = 'trader-kairos'
ORDER BY win_rate DESC;
```

## 6. Dependencies

- `market_data.agent_reflections` table must exist (CREATE TABLE above)
- Agents need read access to `trading.decisions` + `trading.trades`
- Postgres must be reachable from all agent environments