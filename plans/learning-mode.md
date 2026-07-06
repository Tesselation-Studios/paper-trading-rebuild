# Plan: Learning Mode — Loose Start, Let the Learning Loop Tighten

**Date:** 2026-07-06
**Status:** Implemented
**Decision log:** `DECISIONS.md#2026-07-06`

## Problem

After 10+ days of operation:

| Trader | Live buys | Trading data |
|--------|-----------|-------------|
| Kairos | 0 | Nothing to learn from |
| Stonks | 0 | Nothing to learn from |
| Aldridge | 1 (sell) | ~10 data points |

The learning loop can't optimize what doesn't exist. Conservatism is the enemy of improvement.

## Insight

The starting prompt just needs to be loose enough to generate 1-2 trades per session. The learning loop should be the force that:

- Tightens confidence thresholds
- Narrows watchlists to what's actually working
- Reduces position sizing after bad runs
- Raises signal requirements after false positives
- Evolves entry/exit criteria based on what wins

**Don't pre-optimize the starting prompt.** Let the nightly sweeps discover what works.

## Changes

### Kairos

Before: $50-200 stock range, 0.55 confidence, CHOPPY=HOLD, volume 2x filter
After: $10-40 stock range, 0.30 confidence, CHOPPY=BUY oversold, volume 1.2x filter
Stock watchlist: KO, F, INTC, PFE, WBD, VZ, CSCO, HPQ, KHC, WBA

### Aldridge

Before: Mega-cap only, "do nothing is an underrated strategy"
After: Mid-cap value included ($10-40 range), "do nothing" suspended, minimum 1 BUY/session
Emphasis: fundamental value signals on cheap, established businesses

### Stonks

Before: Small-cap momentum/meme stocks, market-rate confidence
After: Same strategy but STARTING with cheap stocks, 0.30 confidence
Emphasis: community signal + technical confirmation on affordable names

## Expected Trajectory

```
Week 1-2: 30-60 noisy trades, 50-55% win rate, -2% to +2% P&L
Week 3-4: Learning loop has data → parameters start tightening
Week 5-8: Confidence thresholds rise, watchlists narrow, win rate rises
Week 9+: Emergence of actual edge on the stocks that work
```

## Risk Mitigation

- $10-40 stocks = max loss ~$5-20/trade at 2% position sizing
- Circuit breaker still active (pauses at 10% DD)
- Nightly sweeps validate every parameter change out-of-sample
- If a trader loses 5% in week 1, the learning loop will naturally raise their confidence threshold and reduce position size

## No Flag Needed

The "LEARNING MODE" concept is implied by the starting prompt being loose. No explicit flag, no mode switch, no "30 trades then flip." The learning loop handles the transition — as parameters tighten, the trader naturally becomes more selective. Good parameters + good prompts = good trading. Bad data prevents both from improving.
