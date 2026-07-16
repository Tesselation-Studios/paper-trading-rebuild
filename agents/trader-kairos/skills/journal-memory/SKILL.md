---
name: journal-memory
description: Distill journal entries into durable MEMORY.md — the heartbeat maintenance skill
---

# Journal → Memory Pipeline

This skill is the "distill" step in your heartbeat cycle. After every 6th trading tick, run this process to convert raw journal entries into durable, indexed knowledge.

## When to Run

- During Cycle 2 (heartbeat maintenance), after reflection
- When journal entries have accumulated (≥ 6 new entries since last distillation)
- When you notice contradictory patterns in your trading

## Process

### Step 1: Gather Journal Entries
Read your recent journal entries (last 20-30 ticks). Focus on:
- Trades that went well (P&L positive, thesis confirmed)
- Trades that went poorly (stop-loss hit, thesis broken)
- Missed opportunities (HOLD when you should have acted)
- Regime-specific performance patterns

### Step 2: Distill Insights
Extract patterns and convert them into actionable rules:
```
Pattern: "Bought INTC in CHOPPY regime, stopped out 3x in a row"
Insight: "INTC is too volatile for CHOPPY regime — exclude or reduce size"
MEMORY.md entry: "## INTC — Avoid in CHOPPY regime (stopped out 3x, high beta)"
```

### Step 3: Update MEMORY.md
Write insights into your MEMORY.md file. Structure:
```
## Signal Performance (updated {date})
- {signal}: {win_rate}% win rate, avg PnL {amount}
- {signal}: avoid below {threshold}, works above {threshold}

## Ticker Notes
- {TICKER}: {observation} ({date}, {regime})

## Regime Performance
- {regime}: {win_rate}% win rate, {trade_count} trades
- {regime}: reduce position size to {pct}%

## Lessons Learned
- {date}: {lesson} — {evidence}
```

### Step 4: Prune Stale Entries
- Remove entries > 30 days old that no longer reflect current market
- Remove contradictory entries (keep the newer observation)
- Keep at most 50 lines in MEMORY.md — archive old entries to `memory/archive/`
- If a rule has been contradicted 3+ times, delete it

### Step 5: Prune Journal
- Journal entries older than 7 days → archive only
- Keep last 50 entries for context
- Exception: entries with P&L > 2% or novel patterns — keep indefinitely

## Example

**Before (journal entries):**
- Tick 142: BUY KO, CHOPPY regime, RSI 38, stopped out -2.1%
- Tick 143: BUY F, CHOPPY regime, RSI 42, profit +1.8%
- Tick 144: BUY INTC, CHOPPY regime, RSI 36, stopped out -1.9%

**After (MEMORY.md addition):**
```
## CHOPPY Regime Notes (updated 2026-07-16)
- RSI < 40 entries: 33% win rate (1/3) — tighten threshold to RSI < 35 only
- INTC: too volatile for CHOPPY, 0/2 wins — blacklist in CHOPPY
- F: works in CHOPPY, 1/1 — keep on watchlist
```

## Integration
- Call this after `data-bus__get_self_stats` for performance context
- Write MEMORY.md updates with the `write` tool
- Prune with the `edit` tool (remove old sections, not whole file)