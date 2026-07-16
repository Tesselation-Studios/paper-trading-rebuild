# Validation & Overfitting Prevention

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-15
**Status**: 🟢 Active — multi-date walk-forward with 4-gate acceptance criteria

---

## Walk-Forward Validation

Every parameter change must pass walk-forward validation across multiple
dates (minimum 5 out-of-sample windows per sweep):

```
Training window: [T-7 days]
Validation window: [T+1, T+3 days]

Acceptance criteria (4 gates):
  1. Validation Sharpe > 0 (positive on unseen data)
  2. Validation Sharpe > Baseline Sharpe (improved vs current params)
  3. Validation Sharpe > Training Sharpe × 0.7 (not grossly overfit)
  4. Statistical significance: paired t-test p < 0.05 (95% confidence)

If any criterion fails: REJECT.
If all pass: ACCEPT with confidence = validation_sharpe / training_sharpe.

## Implementation

- `src/validation.py`: WalkForwardValidator, walk_forward_split, is_overfit,
  is_significant (paired t-test with scipy fallback)
- `src/prompt_sweep.py`: Multi-date sweep via `_run_multidate_sweep()` with
  4-gate winner criteria — win_rate ≥ 0.6, avg_val > baseline + 0.05,
  stability < 2× baseline, significance p < 0.05
- `src/sweep_validation.py`: `two_phase_validate()` with signal → LLM gate;
  both phases must agree for promotion
- CLI default: `--dates 20` (multi-date walk-forward) — single-date mode
  (`--dates 1`) still available but warns about synthetic data fallback
- Minimum 5 out-of-sample windows enforced (warning issued below threshold)