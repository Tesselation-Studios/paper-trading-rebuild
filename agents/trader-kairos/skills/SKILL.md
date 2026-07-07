---
name: kairos-strategy
description: HMM regime-filtered momentum trading — entry rules, signal stack, and output format for Kairos Capital
---

# Kairos Capital — Momentum Strategy

Your edge: HMM regime filter + orthogonal momentum signals on affordable liquid stocks. The learning loop tightens your parameters over time. Your job is to generate trading data.

## Regime → Action

Get regime: `curl -s "http://localhost:5000/market-regime"`

| Regime | Action | Max Position | Stop |
|--------|--------|-------------|------|
| SUSTAINABLE | Standard momentum entries | 10% portfolio | 3% |
| CHOPPY | Buy oversold (RSI < 45, price > MA200) | 5% portfolio | 3% |
| EXHAUSTED | Probe only, mean-reversion entries | 5% portfolio | 4% |
| UNREACHABLE | Continue with technicals, -30% conviction | 7% portfolio | 3% |

**FearContrarian**: Fear & Greed ≤ 30 = BUY signal. RSI < 45 + green candle = entry.

## Signal Stack

Use these data bus endpoints to gather signals:
- `data-bus__get_quotes` — prices, RSI, MACD, volume
- `data-bus__get_technical_scan` — multi-timeframe scan
- `data-bus__get_market_regime` — HMM regime
- `data-bus__get_sentiment` — news sentiment
- `data-bus__get_macro` — Fear & Greed, yield curve

**Confirmation patterns (need 2+):**
- RSI > 55 rising
- MACD bullish crossover
- Price > MA50
- Volume > 1.2x average
- Positive news sentiment
- Sector ETF momentum

**VIX overlay:**
- VIX < 20: standard sizing
- VIX 20-30: reduce to 25% intended size, widen stops to 4%
- VIX > 30: halve all positions
- VIX > 35: consider cash only

## Entry/Exit

- **BUY**: 2+ signals confirmed, conviction > 0.3, market open
- **SELL**: Stop-loss hit, thesis broken, or profit target (20-30%) reached
- **HOLD**: Only if no setup meets criteria. Cash deployed > idle cash.

## Learning Mode

Your confidence threshold starts at 0.3. Take swings. Missing entries teaches us nothing. The nightly sweeps will tighten parameters as data accumulates.

Stock universe: KO, F, INTC, PFE, WBD, VZ, CSCO, HPQ, KHC, WBA — all under $40, all >1M daily volume. Evolve this list over time.
