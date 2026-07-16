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
- When social sentiment shifts dramatically (regime change)

## Process

### Step 1: Gather Journal Entries
Read your recent journal entries (last 20-30 ticks). Focus on:
- Trades that went well (sentiment signal confirmed, crowd was right)
- Trades that went poorly (sentiment faded, crowd was wrong or too late)
- Social sentiment accuracy (did Reddit/Stocktwits predict the move?)
- Sentiment divergence wins (EN bullish + ZH bearish = fade the crowd)

### Step 2: Distill Insights
Extract patterns specific to sentiment-driven trading:
```
Pattern: "Reddit sentiment spike on WBD, bought, +4.2% in 2 hours"
Insight: "WBD responds to social catalysts within 1-2 bars"
MEMORY.md entry: "## WBD — Social momentum stock, enter on sentiment spike > 0.7"
```

### Step 3: Update MEMORY.md
Write insights into your MEMORY.md file. Structure:
```
## Sentiment Signals (updated {date})
- Reddit bullish > 0.6: {win_rate}% win rate, avg PnL {amount}
- Stocktwits bearish < -0.4: {win_rate}% contrarian win rate
- Sentiment divergence (EN/ZH > 0.5 gap): {win_rate}%

## Ticker Notes
- {TICKER}: social beta {X}, responds to {source} ({date})

## Social Sources Performance
- Reddit: {win_rate}% — best for {sector}
- Stocktwits: {win_rate}% — best for {sector}
- Bluesky: {win_rate}% — unreliable, low signal

## Lessons Learned
- {date}: {lesson} — {evidence}
```

### Step 4: Prune Stale Entries
- Remove entries > 14 days old (sentiment shifts fast)
- When a ticker's social interest dies (volume < 0.5x avg), remove from watchlist
- Keep at most 50 lines in MEMORY.md — archive old entries to `memory/archive/`
- If a sentiment source is consistently wrong, flag it, don't delete it

### Step 5: Prune Journal
- Journal entries older than 7 days → archive only
- Keep last 50 entries for context
- Exception: entries where sentiment divergence was the key signal — keep for analysis

## Example

**Before (journal entries):**
- Tick 201: BUY WBD, Reddit sentiment 0.82, +3.1% profit
- Tick 203: BUY INTC, Reddit sentiment 0.45, stopped out -1.8%
- Tick 206: BUY KO, sentiment divergence EN+0.3/ZH-0.4, +2.7%

**After (MEMORY.md addition):**
```
## Sentiment Thresholds (updated 2026-07-16)
- Reddit signal: threshold raised to 0.65 (was 0.5) — 80% win rate above 0.65
- Sentiment divergence: best signal, 3/3 wins — increase priority
- INTC: social sentiment unreliable, 40% win rate — lower conviction weight
```

## Integration
- Call this after `data-bus__get_self_stats` for performance context
- Call `data-bus__get_sentiment_divergence` for cross-language signal validation
- Write MEMORY.md updates with the `write` tool
- Prune with the `edit` tool (remove old sections, not whole file)