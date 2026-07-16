---
name: consider-positions
description: Discover new stocks with social momentum — the watchlist expansion skill
---

# Consider New Positions

This skill is the "consider new positions" step in your heartbeat cycle. It helps you discover stocks beyond your current watchlist that match your community-driven sentiment strategy.

## When to Run

- During Cycle 2 (heartbeat maintenance)
- When you have < 3 open positions (cash is idle)
- When social volume spikes on new tickers (opportunity detection)
- When sentiment divergence appears (EN vs ZH gaps)

## Process

### Step 1: Audit Current State
```
Current positions: {count} / {max}
Cash utilization: {pct}%
Watchlist tickers: {list}
Sectors represented: {list}
Social sources active: {Reddit, Stocktwits, Bluesky}
```

### Step 2: Hunt for Social Momentum
Use these criteria for social momentum candidates:
- Under $40/share (retail-friendly price points)
- Daily volume > 1M shares (liquidity for quick entries/exits)
- Social mention volume > 2x normal (trending)
- Sentiment score > 0.5 (bullish crowd)
- Recent price movement < 5% (not already pumped)

### Step 3: Verify the Signal
For each candidate:
- Check sentiment: `data-bus__get_sentiment(symbol)` — is the crowd right?
- Check divergence: `data-bus__get_sentiment_divergence(symbol)` — EN vs ZH split?
- Check flow: `data-bus__get_flow(symbol)` — smart money confirming?
- Check technicals: `data-bus__get_technical_scan(symbol)` — is price supporting?

### Step 4: Score Candidates
Score each candidate 0-10:
```
Score = Social Signal (0-4) + Technical Setup (0-3) + Smart Money (0-2) + Diversification (0-1)
```

- Social Signal: 4 = multi-source bullish + rising volume, 0 = single source, weak
- Technical: 3 = RSI 45-55 + vol > 1.5x, 0 = overbought RSI > 70
- Smart Money: 2 = unusual options flow + insider buying, 0 = no confirmation
- Diversification: 1 = new sector, 0 = crowded sector

### Step 5: Decide
- Score ≥ 7: Add to watchlist, consider entry on next tick
- Score 5-6: Add to watchlist, monitor for social volume increase
- Score < 5: Skip — noise, not signal

## Social Momentum Tickers to Watch
Retail favorites that frequently trend (under $40):
```
AMC, BB, BBBYQ, CLOV, F, GPS, M, NIO, PLTR, RIVN, SNAP, SOFI, TLRY, UAA, WBD
```

Filter by social source:
- Reddit trending (r/wallstreetbets, r/stocks, r/investing): momentum plays
- Stocktwits watchlist trending: sentiment confirmation
- Bluesky trending: early signal detection (lower volume, higher alpha)

## Warning Signs (SKIP the ticker)
- Pump and dump pattern (vertical spike + no fundamental catalyst)
- Single-source hype (only Reddit, no Stocktwits/Bluesky confirmation)
- Already +15% on the day (you're too late)
- Insider selling while crowd is buying (fade the crowd)

## Integration
- Record decisions in journal: "considered {ticker}, social score {X}, {added/skipped}"
- Update watchlist in prompt.txt if adding new tickers (use edit tool)
- Remove tickers from watchlist if social volume drops below 0.5x avg for 1 week
- Flag tickers that were pump-and-dumps in MEMORY.md — don't repeat mistakes