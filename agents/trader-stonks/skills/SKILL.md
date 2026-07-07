---
name: stonks-strategy
description: Community-driven momentum trading — social signal stacking, freshness decay, and multi-source conviction for Stonks Capital
---

# Stonks Capital — Community Momentum Strategy

Your edge: multi-source community momentum with freshness decay. You combine social signals, news catalysts, options flow, and volume confirmation. Diamond hands, but intelligent diamond hands.

## Signal Stack

Stack signals. More sources = higher conviction. All signals decay with age.

| Source | Tool | Weight |
|--------|------|--------|
| Overnight sentiment delta | `src/overnight_sentiment.py` | 25% |
| News sentiment | `data-bus__get_sentiment` | 20% |
| Price + volume confirmation | `data-bus__get_quotes` | 20% |
| Options flow | `data-bus__get_flow` | 15% |
| Social buzz (Reddit, StockTwits, Bluesky) | `web_search` | 20% |

**Freshness rule**: Signals older than 1 hour → halved weight. Signals older than 4 hours → ignored.

## Entry Triggers

Need **2+ signals** with at least 1 technical confirmation:

1. Social buzz trending + volume > 1.5x average → BUY
2. Overnight sentiment strong bull + RSI > 55 → BUY
3. Options flow unusual (sweeps/dark pool) + positive news → BUY
4. Community consensus + MACD bullish → BUY

**Confidence threshold**: 0.3 (learning mode — take swings, we tighten later)

## Exit Rules

- Take profits at 20-30% gain
- Cut at stop-loss (3% below entry)
- Thesis broken (community sentiment reverses, news sours, catalyst fails)
- 48-hour hard stop: if a play hasn't moved by day 2, exit. Capital is ammo.

## Community Sources

- StockTwits: trending tickers, message velocity
- Reddit (r/wallstreetbets, r/stocks, r/investing): DD posts, mention spikes
- Bluesky: fintech/stonk accounts, viral threads
- Discord: your crew's radar (simulated via `web_search`)

## Learning Mode

Starting with affordable momentum plays: KO, F, INTC, PFE, WBD, VZ, CSCO, HPQ, KHC, WBA. All under $40. Stack wins, earn bigger names. Confidence starts at 0.3 — the learning loop tightens it.
