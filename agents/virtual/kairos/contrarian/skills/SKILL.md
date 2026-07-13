---
name: kairos-contrarian-strategy
description: Contrarian mean-reversion — buy weakness, sell strength
---

# Kairos Contrarian Strategy

## Entry Signals

### Long Entry (Buy Dip)
- RSI < 40 (oversold)
- Price touching or below lower Bollinger Band (2.0 std)
- Volume spike > 1.5x average (climax selling)
- Bullish MACD divergence (price lower low, MACD higher low)
- Fear & Greed ≤ 30: conviction bonus +0.15

### Short Entry (Sell Rally)
- RSI > 70 (overbought)
- Price touching or above upper Bollinger Band
- Volume declining on rally (exhaustion)
- Bearish MACD divergence
- Fear & Greed ≥ 85: conviction bonus +0.15

## Exit Rules

### Long Exits
- RSI recovers to 60: take half profit
- RSI > 70: exit fully (overbought on bounce)
- Price touches upper Bollinger: exit
- Stop loss at 4% below entry

### Short Exits
- RSI falls back to 50: cover
- Price touches lower Bollinger: cover
- Stop loss at 4% above entry

## Sizing

Base = 2% of portfolio. Adjust:
- F&G extremes (≤30 or ≥85): 1.5x (= 3%)
- Lower confidence: 0.5x (= 1%)
- Max position: 8% of portfolio