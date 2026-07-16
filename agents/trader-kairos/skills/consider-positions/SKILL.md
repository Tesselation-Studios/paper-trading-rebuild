---
name: consider-positions
description: Discover new stocks matching your strategy — the watchlist expansion skill
---

# Consider New Positions

This skill is the "consider new positions" step in your heartbeat cycle. It helps you discover stocks beyond your current watchlist that match your momentum strategy.

## When to Run

- During Cycle 2 (heartbeat maintenance)
- When you have < 3 open positions (cash is idle)
- When current watchlist stocks are all overbought (RSI > 65)
- When market regime shifts (new regime = new opportunities)

## Process

### Step 1: Audit Current State
```
Current positions: {count} / {max}
Cash utilization: {pct}%
Watchlist tickers: {list}
Sectors represented: {list}
```

### Step 2: Scan for Candidates
Use these criteria for momentum candidates:
- Under $40/share (capital efficiency for 2% position sizing)
- Daily volume > 1M shares (liquidity)
- RSI between 40-60 (not overbought, not oversold — momentum setup zone)
- MACD turning bullish or about to cross
- Price above MA50 (uptrend confirmation)

Use `data-bus__get_quotes` to scan tickers you're considering.

### Step 3: Verify Against Strategy
For each candidate, check:
- Does it fit the current regime? (CHOPPY: oversold, TRENDING: momentum)
- Is it in a different sector than current positions? (diversification)
- Is there positive sentiment? Call `data-bus__get_sentiment`
- Is there unusual options flow? Call `data-bus__get_flow`

### Step 4: Score Candidates
Score each candidate 0-10:
```
Score = Regime Fit (0-3) + Technical Setup (0-3) + Sentiment (0-2) + Diversification (0-2)
```

- Regime Fit: 3 = perfect regime match, 0 = wrong regime
- Technical: 3 = RSI 45-55 + MACD crossover + vol > 1.2x, 0 = overbought
- Sentiment: 2 = strong positive, 0 = negative
- Diversification: 2 = new sector, 0 = same sector as existing position

### Step 5: Decide
- Score ≥ 7: Add to watchlist, consider entry on next tick
- Score 5-6: Add to watchlist, monitor
- Score < 5: Skip

## Suggested Scan List
Start with these tickers that match momentum criteria (under $40, liquid):
```
AAL, BB, CLF, F, GE, GPS, HBAN, KEY, KMI, M, NCLH, OXY, PBR, RIG, SNAP,
SWN, T, UAA, VALE, WBA, X, ZION
```

Filter by current regime:
- TRENDING: Look for RSI 55-65, MACD bullish, vol > 1.2x
- CHOPPY: Look for RSI < 45, above MA200
- EXHAUSTED: Look for RSI < 35, oversold bounces

## Integration
- Record decisions in journal: "considered {ticker}, score {X}, {added/skipped}"
- Update watchlist in prompt.txt if adding new tickers (use edit tool)
- Remove tickers from watchlist if they've been dead for 2+ weeks