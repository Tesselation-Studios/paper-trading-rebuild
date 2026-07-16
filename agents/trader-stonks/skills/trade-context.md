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
curl -s localhost:5000/portfolio?account=stonks
```
Returns: cash, equity, buying_power, positions, daily_pnl, drawdown

### 2. Current Positions
```
curl -s localhost:5000/positions?account=stonks
```
Returns: open positions with entry price, current price, P&L, days held

### 3. Market Quotes
```
curl -s localhost:5000/quotes?symbols=KO,F,INTC,PFE,WBD,VZ,CSCO,HPQ,KHC,WBA
```
Returns: RSI, MACD, MA20, price, volume, volume_ratio for each symbol

### 4. Social Sentiment
```
curl -s localhost:5000/social?source=all
```
Returns: aggregated Reddit/Bluesky/Stocktwits sentiment

### 5. ML Signal / Regime
```
curl -s localhost:5000/ml-signal?symbol=ALL
```
Returns: current market regime (TRENDING/CHOPPY/EXHAUSTED) and confidence

### 6. Fear & Greed
```
curl -s localhost:5000/fear_greed
```
Returns: Fear & Greed Index for market sentiment context

## Formatting Your Context

After gathering data, format it as structured text for your decision:

```
## Portfolio
Cash: $X,XXX.XX | Equity: $X,XXX.XX | Daily PnL: +$XX.XX (+X.X%)
Positions: TICKER (entry $X.XX, now $X.XX, PnL +$XX.XX, held X days)

## Market
Regime: TRENDING (confidence 0.85)
Watchlist: KO RSI 62 MACD bullish vol 1.3x, F RSI 45 MACD neutral vol 0.9x, ...

## Signals
Social: KO trending bullish (0.72), INTC bearish (-0.31)
Fear & Greed: 52 (Neutral)
```

## Pre-Assembled Context

If you're in a hurry, `tick_prompt.py` already assembles this context for you. The data above is the same data injected into every tick — you can also call these endpoints directly for mid-tick refreshes or ad-hoc queries.

## Self-Stats

```bash
curl -s localhost:5000/self-stats?account=stonks
```
Returns: win rate (last 10/50/all), avg PnL per trade, drawdown, position concentration

Note: This endpoint may not exist yet — if it 404s, skip it and calculate from your journal.