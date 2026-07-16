---
name: consider-positions
description: Discover new value stocks — the watchlist expansion skill
---

# Consider New Positions

This skill is the "consider new positions" step in your heartbeat cycle. It helps you discover value stocks beyond your current watchlist that match your contrarian value strategy.

## When to Run

- During Cycle 2 (heartbeat maintenance)
- When you have < 3 open positions (cash is idle)
- When Fear & Greed is in Fear zone (≤ 35) — contrarian opportunity
- When yield curve shifts (sector rotation signal)

## Process

### Step 1: Audit Current State
```
Current positions: {count} / {max}
Cash utilization: {pct}%
Watchlist tickers: {list}
Sectors represented: {list}
```

### Step 2: Screen for Value Candidates
Use these value criteria:
- P/E ratio < 15 (or below sector average)
- Dividend yield > 3% (income while waiting)
- Price < 50% of 52-week high (contrarian entry zone)
- RSI < 45 (oversold, not catching a falling knife below 25)
- Market cap > $2B (avoid micro-cap value traps)

Use `data-bus__get_quotes` to check technicals on candidates.

### Step 3: Verify Value Thesis
For each candidate:
- Check insider buying: `data-bus__get_insiders(symbol)` — are executives buying?
- Check sentiment: `data-bus__get_sentiment(symbol)` — is there a reason for the discount?
- Check macro: `data-bus__get_macro()` — is the sector out of favor cyclically?
- Check risk: `data-bus__get_risk(symbol)` — concentration and VaR

### Step 4: Score Candidates
Score each candidate 0-10:
```
Score = Value (0-3) + Margin of Safety (0-3) + Catalyst (0-2) + Diversification (0-2)
```

- Value: 3 = P/E < 10 + div > 4%, 0 = P/E > 20
- Margin of Safety: 3 = price < 40% of 52w high, 0 = near 52w high
- Catalyst: 2 = insider buying + positive divergence, 0 = no catalyst
- Diversification: 2 = new sector, 0 = same sector as existing position

### Step 5: Decide
- Score ≥ 7: Add to watchlist, consider entry on next tick
- Score 5-6: Add to watchlist, monitor for better entry
- Score < 5: Skip

## Suggested Scan List
Start with these value candidates (low P/E, decent dividends):
```
BTI, CVX, DOW, DUK, IBM, IP, JNJ, KHC, KMI, LYB, MO, OKE, PFE, PM, PRU,
SO, T, UPS, VZ, WBA, XOM
```

Filter by current conditions:
- Fear zone (F&G ≤ 35): Aggressively scan, focus on beaten-down sectors
- Yield curve flattening: Rotate to defensive (utilities, staples, healthcare)
- Yield curve steepening: Rotate to cyclicals (energy, financials, industrials)

## Integration
- Record decisions in journal: "considered {ticker}, score {X}, {added/skipped}"
- Update watchlist in prompt.txt if adding new tickers (use edit tool)
- Remove tickers from watchlist if fundamentals deteriorate (P/E spike, dividend cut)