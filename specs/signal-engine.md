# Signal Engine — Gradient-Descent Tuned

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-09

---

## Tunable Parameters (Version-Controlled)

```yaml
signal_params:
  momentum:
    threshold: 0.55          # float [0.3, 0.9]
    lookback_days: 20        # int [5, 60]
    decay_rate: 0.85         # float [0.5, 0.99]

  mean_reversion:
    rsi_oversold: 30.0       # float [15, 40]
    rsi_overbought: 70.0     # float [60, 85]
    bollinger_std: 2.0       # float [1.0, 3.0]

  volatility:
    regime_threshold: 0.25   # float [0.1, 0.5]
    reduction_multiplier: 0.7 # float [0.3, 1.0]

  position_sizing:
    base_size_pct: 0.15      # float [0.05, 0.30]
    conviction_multiplier: 1.5  # float [1.0, 3.0]
    max_positions: 5         # int [1, 10]

  risk:
    stop_loss_pct: 0.05      # float [0.02, 0.10]
    take_profit_pct: 0.15    # float [0.05, 0.30]
    trailing_stop_pct: 0.03  # float [0.01, 0.08]

  regime_weights:
    trending_up: 1.0         # float [0.2, 2.0]
    trending_down: 0.5       # float [0.0, 1.5]
    mean_reverting: 0.8      # float [0.2, 2.0]
    high_volatility: 0.4     # float [0.0, 1.0]
```

## Finite-Difference Gradient Descent

Runs every trading tick. Perturbs each param ±ε, replays last N ticks to estimate gradient, steps toward improvement.

**Constraints:**
- Learning rate: 0.01 per tick
- Max change per tick: 5% of range
- Minimum 3 ticks between changes to same param
- All changes logged with before/after scores

## XGBoost Momentum Classifier

> **⚠️ NOT DEPLOYED (2026-07-16)**: No trained model file exists in the repository.
> The 78% accuracy claim was aspirational. The actual model was never persisted
> after its stated training date (Jul 9, 2026) — no `.pkl`, `.joblib`, or
> `xgboost` import exists anywhere in the codebase. Kairos's prompt does NOT
> claim 63% accuracy (contrary to a prior SPEC.md drift note); no live
> accuracy can be measured because Kairos has zero closed trades with P&L.
>
> **TODO**: Either train + persist an XGBoost classifier (`models/xgb_momentum.pkl`)
> or remove this section and rely solely on the gradient-descent signal engine.

Originally specified: Predicts whether a given momentum signal will produce a
winning trade. Target accuracy: 78%, ROC-AUC: 0.82. Intended as a secondary
signal gate — when score < 0.25, position size is halved.

**Target top features:** RSI, momentum_composite, volume_ratio, regime_prob_sustainable.

## Relaxed Threshold Presets

For sweep starting points when conservative defaults produce zero trades:

| Preset | momentum_threshold | rsi_oversold | rsi_overbought | Use |
|--------|-------------------|--------------|----------------|-----|
| **conservative** | 0.55 | 30 | 70 | Production defaults |
| **relaxed** | 0.25 | 35 | 65 | Sweep starting point |
| **aggressive** | 0.15 | 40 | 60 | Max sensitivity |

## Pre-Warm Mechanism

Each `run_scenario` feeds 30 initial ticks silently before counting trades. Only trades from tick 31+ are scored.