---
name: kairos-aggressive-strategy
description: Aggressive momentum — lower threshold, bigger positions, wider stops
---

# Kairos Aggressive Strategy

## Parameter Changes from Live

| Parameter | Live | Aggressive |
|-----------|------|------------|
| Conviction min | 0.65 | **0.25** |
| Position size | 1.5% | **3.0%** |
| Max positions | 3 | **5** |
| Stop loss | 4% | **5%** |
| Volume filter | 1.2x avg | **1.0x avg** |
| RSI entry trigger | >55 | **>50** |
| Confirmations needed | 3/4 | **2/4** |

## When to Enter

- **SUSTAINABLE regime:** Full position on any 2 of RSI>50, MACD bullish, volume>avg
- **CHOPPY regime:** Half-size probe on same triggers; tight 3% stop
- **EXHAUSTED regime:** 1-share probe only on 3/3 alignment
- **FearContrarian (F&G ≤ 30):** Single trigger enough. Full aggressor mode.

## When to Exit

- Stop loss hit: hard exit, no second-guessing
- RSI fell through 40 on daily: thesis warning, tighten stop
- Position aged > 7 days without catalyst: exit and rotate
- Portfolio reached max positions: must exit one before new entry

## Sizing

Base = 3% of portfolio. Conviction multiplier:
- 2/4 confirmations: 1.0x (= 3%)
- 3/4 confirmations: 1.5x (= 4.5%)
- 4/4 confirmations: 2.0x (= 6%)
- FearContrarian: 1.0x (don't over-commit)

## Risk

Wider stops means accepting bigger swings. The trade-off is more time for the thesis to play out. If a -5% move is technical noise, you survive it. If it's real, you're out.