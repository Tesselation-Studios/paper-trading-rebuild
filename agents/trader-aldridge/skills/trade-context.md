---
name: trade-context
description: Gather current positions, portfolio state, and market data — the "gather tool" the spec promises
---

# Trade Context Skill

Call this skill to gather your current trading context during a tick. This is the "tool" the spec describes — you proactively fetch data, not just receive pre-assembled context.

## How to Use

During your heartbeat, call these data bus endpoints to build your own context:

### 1. Portfolio State
```
curl -s localhost:5000/portfolio?account=aldridge
```
Returns: cash, equity, buying_power, positions, daily_pnl, drawdown

### 2. Current Positions
```
curl -s localhost:5000/positions?account=aldridge
```
Returns: open positions with entry price, current price, P&L, days held

### 3. Fundamentals
```
curl -s localhost:5000/fundamentals?symbols=KHC,WBA,INTC,PFE,VZ,CSCO,F,HPQ,KO
```
Returns: P/E, EPS, dividend yield, analyst target for each symbol

### 4. Market Quotes
```
curl -s localhost:5000/quotes?symbols=KHC,WBA,INTC,PFE,VZ,CSCO,F,HPQ,KO
```
Returns: RSI, MACD, MA20, price, volume for position-checking

### 5. News
```
curl -s localhost:5000/news?symbols=KHC,WBA,INTC,PFE
```
Returns: recent news items for thesis integrity checks

### 6. Macro Context
```
curl -s localhost:5000/macro
```
Returns: FRED indicators, yield curve (steepening/flattening signals sector rotation)

### 7. Fear & Greed
```
curl -s localhost:5000/fear_greed
```
Returns: Fear & Greed Index — contrarian indicator

## Formatting Your Context

After gathering data, format it as structured text for your decision:

```
## Portfolio
Cash: $X,XXX.XX | Equity: $X,XXX.XX | Daily PnL: +$XX.XX (+X.X%)
Positions: TICKER (entry $X.XX, now $X.XX, PnL +$XX.XX, held X days)

## Fundamentals
KO: P/E 22, EPS $2.10, div 3.2%, target $68
F: P/E 8, EPS $1.85, div 5.1%, target $14

## Market
Yield curve: flattening (defensive rotation signal)
F&G: 35 (Fear) — contrarian buy signal
```

## Pre-Assembled Context

If you're in a hurry, `tick_prompt.py` already assembles this context for you. The data above is the same data injected into every tick — you can also call these endpoints directly for mid-tick refreshes or ad-hoc queries.

## Self-Stats

```bash
curl -s localhost:5000/self-stats?account=aldridge
```
Returns: win rate, avg PnL per trade, drawdown, position concentration

Note: This endpoint may not exist yet — if it 404s, skip it and calculate from your journal.