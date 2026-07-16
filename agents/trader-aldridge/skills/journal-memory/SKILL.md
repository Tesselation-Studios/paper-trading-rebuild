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
- Trades that went well (P&L positive, thesis confirmed, fundamentals validated)
- Trades that went poorly (thesis broken, value trap identified)
- Missed opportunities (HOLD when fundamentals signaled BUY)
- Dividend capture effectiveness (ex-div dates, holding periods)

### Step 2: Distill Insights
Extract patterns specific to value investing:
```
Pattern: "Bought KHC at P/E 12, dividend 4%, price dropped further on earnings miss"
Insight: "Don't buy before earnings — value traps often reveal on earnings"
MEMORY.md entry: "## Earnings Blackout — No new positions 3 days before earnings"
```

### Step 3: Update MEMORY.md
Write insights into your MEMORY.md file. Structure:
```
## Value Signals (updated {date})
- P/E < 10: {win_rate}% win rate, avg PnL {amount}
- Dividend > 4%: {win_rate}% win rate, avg hold {days} days

## Ticker Notes
- {TICKER}: P/E target {X}, current {Y}, thesis: {intact/broken} ({date})

## Sector Performance
- Defensive sectors (staples, utilities): {win_rate}% during {regime}
- Cyclical sectors: avoid during yield curve flattening

## Lessons Learned
- {date}: {lesson} — {evidence}
```

### Step 4: Prune Stale Entries
- Remove entries > 30 days old that no longer reflect current valuations
- When a ticker's P/E or dividend changes significantly, update or remove old thesis
- Keep at most 50 lines in MEMORY.md — archive old entries to `memory/archive/`
- If a value thesis has been invalidated by fundamentals change, delete the entry

### Step 5: Prune Journal
- Journal entries older than 14 days → archive only (value trades hold longer)
- Keep last 50 entries for context
- Exception: multi-week value thesis entries — keep until thesis resolution

## Example

**Before (journal entries):**
- Tick 89: BUY KHC, P/E 12.2, div 4.1%, thesis: undervalued consumer staple
- Tick 92: KHC earnings miss, -8% gap down, stopped out -5%
- Tick 95: BUY VZ, P/E 8.5, div 6.8%, thesis: telecom yield play

**After (MEMORY.md addition):**
```
## Lessons Learned (2026-07-16)
- Earnings blackout: Do not open positions within 3 days of earnings
- KHC: value trap confirmed, thesis broken — remove from watchlist
- VZ: telecom yield play working (+3.2%), continue monitoring
```

## Integration
- Call this after `data-bus__get_self_stats` for performance context
- Write MEMORY.md updates with the `write` tool
- Prune with the `edit` tool (remove old sections, not whole file)