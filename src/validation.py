"""Walk-forward validation — SPEC-v3 §6.

No random shuffling. Time series data must respect temporal order.
Training always precedes validation. Out-of-sample is sacred.
"""
from dataclasses import dataclass
from datetime import date
from typing import Iterator


@dataclass
class TimeWindow:
    """A single walk-forward window."""
    train_start: int
    train_end: int
    val_start: int
    val_end: int


def walk_forward_split(
    n_days: int,
    train_window: int = 90,
    val_window: int = 30,
    step: int = 1,
) -> Iterator[TimeWindow]:
    """Generate walk-forward train/validation windows.

    Window layout:
        [T-120 ... T-30] → train
        [T-30  ... T   ] → val

    Next step:
        [T-119 ... T-29] → train
        [T-29  ... T+1 ] → val

    Args:
        n_days: Total number of days of data.
        train_window: Days in training window (default 90).
        val_window: Days in validation window (default 30).
        step: Days to advance per window (default 1).

    Yields:
        TimeWindow objects with integer indices.
    """
    total_window = train_window + val_window
    if n_days < total_window:
        return  # Not enough data

    for start in range(0, n_days - total_window + 1, step):
        yield TimeWindow(
            train_start=start,
            train_end=start + train_window,
            val_start=start + train_window,
            val_end=start + total_window,
        )


def is_overfit(
    train_score: float,
    val_score: float,
    threshold: float = 0.30,
) -> bool:
    """Detect overfitting: validation score significantly worse than training.

    If val_score < train_score × (1 - threshold), the model is overfit.

    Example: train Sharpe 1.5, val Sharpe 0.9, threshold 0.30:
        0.9 < 1.5 × 0.7 = 1.05 → OVERFIT (rejected)

    Args:
        train_score: Metric on training window.
        val_score: Metric on validation (out-of-sample) window.
        threshold: Maximum acceptable degradation (default 0.30 = 30%).

    Returns:
        True if overfit (reject the change).
    """
    return val_score < train_score * (1.0 - threshold)


def is_significant(
    baseline_scores: list[float],
    candidate_scores: list[float],
    p_threshold: float = 0.05,
) -> tuple[bool, float]:
    """Test if candidate improvement is statistically significant.

    Uses paired t-test. Requires at least 5 data points.

    Args:
        baseline_scores: Metric values under current config (per window).
        candidate_scores: Metric values under proposed config (per window).
        p_threshold: Significance threshold (default 0.05).

    Returns:
        (is_significant: bool, p_value: float)
    """
    import numpy as np
    from scipy import stats as scipy_stats

    if len(baseline_scores) < 5:
        return False, 1.0

    baseline_arr = np.array(baseline_scores)
    candidate_arr = np.array(candidate_scores)

    try:
        t_stat, p_value = scipy_stats.ttest_rel(candidate_arr, baseline_arr)
    except (ImportError, Exception):
        # Fallback: simple z-test of differences
        diffs = candidate_arr - baseline_arr
        mean_diff = np.mean(diffs)
        std_diff = np.std(diffs, ddof=1)
        if std_diff < 1e-10:
            return False, 1.0
        z = mean_diff / (std_diff / np.sqrt(len(diffs)))
        # Two-tailed test: p ≈ 2 * (1 - Φ(|z|))
        p_value = 2.0 * (1.0 - 0.5 * (1.0 + np.tanh(np.sqrt(2.0 / np.pi) * (abs(z) + 0.044715 * abs(z) ** 3))))  # approx
        is_sig = abs(z) > 1.96  # 95% confidence

    return bool(p_value < p_threshold), float(p_value)


def evaluate_config(
    replay_fn,
    config: dict,
    data_window: TimeWindow,
) -> float:
    """Evaluate a config on a specific data window.

    Args:
        replay_fn: Function that takes (config, data_slice) → metrics dict.
        config: Parameter configuration to test.
        data_window: Which data to use.

    Returns:
        Calmar ratio (or whichever metric replay_fn returns).
    """
    result = replay_fn(config, data_window)
    # replay_fn returns dict with 'calmar' or 'objective_score'
    return result.get("objective_score", result.get("calmar", result.get("sharpe", 0.0)))


# ═══════════════════════════════════════════════════════════════════════════════
# Walk-Forward Validation Integration (SPEC §6.1)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ValidationResult:
    """Result of walk-forward validation for a parameter change.

    Per SPEC §6.1 acceptance criteria:
      1. Validation Sharpe > 0        (positive on unseen data)
      2. Validation Sharpe > Baseline Sharpe (improved vs current params)
      3. Validation Sharpe > Training Sharpe × 0.7 (not grossly overfit)

    If all three pass → accepted = True.
    """

    accepted: bool
    train_sharpe: float
    val_sharpe: float
    baseline_val_sharpe: float
    confidence: float  # val_sharpe / train_sharpe — higher = better generalization
    reason: str  # If rejected, why
    checks: dict  # Per-criterion pass/fail details


@dataclass
class WalkForwardConfig:
    """Configuration for walk-forward validation runs."""

    train_window_days: int = 90
    val_window_days: int = 30
    min_trades: int = 5  # Minimum trades required for meaningful metrics
    overfit_threshold: float = 0.30  # Max acceptable degradation
    step: int = 1  # Day step between windows (1 = dense, 30 = monthly)


class WalkForwardValidator:
    """Walk-forward validation harness — SPEC §6.1.

    Splits historical market data into train/validation windows using
    strictly temporal order (no shuffling). For each window:
      1. Train: [T-90 days, T-30 days] — fit parameters
      2. Validate: [T-30 days, T today] — measure out-of-sample

    Applies three-gate acceptance:
      - Positive validation Sharpe (not just overfit noise)
      - Improved over baseline (candidate > current)
      - No gross overfitting (val ≥ train × 0.7)

    Usage:
        validator = WalkForwardValidator(harness, config)
        result = validator.validate(
            all_ticks=[...],
            candidate_params={"momentum_threshold": 0.60},
            baseline_params={"momentum_threshold": 0.55},
        )
        if result.accepted:
            print(f"Accepted with confidence {result.confidence:.2f}")
    """

    def __init__(
        self,
        config: WalkForwardConfig | None = None,
    ):
        self.config = config or WalkForwardConfig()

    # ── Public API ──────────────────────────────────────────────────────────

    def validate(
        self,
        all_ticks: list,
        candidate_params: dict,
        baseline_params: dict,
        trader_fn=None,
        initial_balance: float = 100_000.0,
        cost_model=None,
    ) -> ValidationResult:
        """Run walk-forward validation of candidate vs baseline parameters.

        Splits the tick data into training and validation windows according
        to the configured window sizes. Runs replay on both windows for
        both parameter sets, computes Sharpe ratios, and applies acceptance
        criteria from SPEC §6.1.

        Args:
            all_ticks: Chronological list of market ticks (oldest first).
            candidate_params: Proposed parameter changes.
            baseline_params: Current production parameters.
            trader_fn: Callable (tick, portfolio) → TraderDecision for replay.
                If None, uses a simple deterministic signal-based trader.
            initial_balance: Starting cash for replay.
            cost_model: Optional CostModel for transaction cost adjustment.

        Returns:
            ValidationResult with accept/reject + diagnostics.
        """
        from src.replay import ReplayHarness

        # Build train/val windows
        windows = list(walk_forward_split(
            n_days=len(all_ticks),
            train_window=self.config.train_window_days,
            val_window=self.config.val_window_days,
            step=self.config.step,
        ))

        if not windows:
            return ValidationResult(
                accepted=False,
                train_sharpe=0.0,
                val_sharpe=0.0,
                baseline_val_sharpe=0.0,
                confidence=0.0,
                reason=f"Not enough data: need {self.config.train_window_days + self.config.val_window_days} "
                       f"days, got {len(all_ticks)}",
                checks={},
            )

        if trader_fn is not None:
            candidate_fn = trader_fn
        else:
            candidate_fn = _default_trader_from_params(candidate_params)

        # Run across all windows and aggregate
        train_sharpes: list[float] = []
        val_sharpes: list[float] = []
        baseline_val_sharpes: list[float] = []

        for window in windows:
            # Slice data
            train_ticks = _slice_ticks(all_ticks, window.train_start, window.train_end)
            val_ticks = _slice_ticks(all_ticks, window.val_start, window.val_end)

            if len(train_ticks) < self.config.min_trades * 2 or len(val_ticks) < self.config.min_trades * 2:
                continue

            # Candidate: train window
            harness = ReplayHarness(
                initial_balance=initial_balance,
                cost_model=cost_model,
            )
            train_result = harness.run(train_ticks, candidate_fn)

            if len(train_result.trades) < self.config.min_trades:
                continue

            # Candidate: val window
            harness_val = ReplayHarness(
                initial_balance=initial_balance,
                cost_model=cost_model,
            )
            val_result = harness_val.run(val_ticks, candidate_fn)

            # Baseline: val window
            harness_base = ReplayHarness(
                initial_balance=initial_balance,
                cost_model=cost_model,
            )
            baseline_fn = _default_trader_from_params(baseline_params)
            baseline_val_result = harness_base.run(val_ticks, baseline_fn)

            # Compute metrics
            try:
                import numpy as np
                from src.metrics import compute_sharpe

                train_sharpe = compute_sharpe(np.array(train_result.returns))
                val_sharpe = compute_sharpe(np.array(val_result.returns))
                baseline_val_sharpe = compute_sharpe(np.array(baseline_val_result.returns))

                train_sharpes.append(train_sharpe)
                val_sharpes.append(val_sharpe)
                baseline_val_sharpes.append(baseline_val_sharpe)
            except (ImportError, Exception):
                pass

        if not val_sharpes:
            return ValidationResult(
                accepted=False,
                train_sharpe=0.0,
                val_sharpe=0.0,
                baseline_val_sharpe=0.0,
                confidence=0.0,
                reason="No windows produced enough trades for valid metrics",
                checks={},
            )

        # Aggregate across windows (mean)
        avg_train_sharpe = sum(train_sharpes) / len(train_sharpes)
        avg_val_sharpe = sum(val_sharpes) / len(val_sharpes)
        avg_baseline_val_sharpe = sum(baseline_val_sharpes) / len(baseline_val_sharpes)

        # Apply acceptance criteria
        checks = {}
        failures: list[str] = []

        # Criterion 1: Validation Sharpe > 0
        checks["val_sharpe_positive"] = avg_val_sharpe > 0
        if not checks["val_sharpe_positive"]:
            failures.append(f"Validation Sharpe {avg_val_sharpe:.3f} ≤ 0 (no edge on unseen data)")

        # Criterion 2: Validation Sharpe > Baseline Sharpe
        checks["beats_baseline"] = avg_val_sharpe > avg_baseline_val_sharpe
        if not checks["beats_baseline"]:
            failures.append(
                f"Validation Sharpe {avg_val_sharpe:.3f} ≤ Baseline {avg_baseline_val_sharpe:.3f}"
            )

        # Criterion 3: Not grossly overfit
        is_overfit_result = is_overfit(avg_train_sharpe, avg_val_sharpe, self.config.overfit_threshold)
        checks["not_overfit"] = not is_overfit_result
        if is_overfit_result:
            failures.append(
                f"Overfit: val Sharpe {avg_val_sharpe:.3f} < train Sharpe "
                f"{avg_train_sharpe:.3f} × 0.7 = {avg_train_sharpe * 0.7:.3f}"
            )

        # Statistical significance check
        is_sig, p_val = is_significant(baseline_val_sharpes, val_sharpes)
        checks["significant"] = is_sig
        if not is_sig and len(baseline_val_sharpes) >= 5:
            failures.append(f"Improvement not statistically significant (p={p_val:.3f} ≥ 0.05)")

        confidence = avg_val_sharpe / avg_train_sharpe if avg_train_sharpe > 0 else 0.0
        accepted = len(failures) == 0

        return ValidationResult(
            accepted=accepted,
            train_sharpe=avg_train_sharpe,
            val_sharpe=avg_val_sharpe,
            baseline_val_sharpe=avg_baseline_val_sharpe,
            confidence=min(confidence, 1.0),
            reason="; ".join(failures) if failures else "All acceptance criteria met",
            checks=checks,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _slice_ticks(ticks: list, start_idx: int, end_idx: int) -> list:
    """Slice a list of ticks by index range, clamping to available data."""
    end = min(end_idx, len(ticks))
    if start_idx >= end:
        return []
    return ticks[start_idx:end]


def _default_trader_from_params(params: dict):
    """Build a simple deterministic trader callable from signal parameters.

    This trader uses momentum threshold + RSI from params to make
    BUY/SELL decisions. Designed for walk-forward validation — no
    LLM calls, fast enough for many windows.

    Args:
        params: Dict of signal parameters (momentum_threshold, rsi_oversold,
            rsi_overbought, stop_loss_pct, take_profit_pct).

    Returns:
        Callable (tick, portfolio) → TraderDecision.
    """
    from src.replay import Tick, Portfolio, TraderDecision  # noqa: F811

    momentum_threshold = params.get("momentum_threshold", 0.55)
    rsi_oversold = params.get("rsi_oversold", 30.0)
    rsi_overbought = params.get("rsi_overbought", 70.0)
    stop_loss_pct = params.get("stop_loss_pct", 0.05)

    def trader(tick, portfolio):
        # Check stop-loss exits
        for pos in list(portfolio.positions.values()):
            if pos.current_price > 0 and pos.entry_price > 0:
                pnl_pct = (pos.current_price - pos.entry_price) / pos.entry_price
                if pnl_pct <= -stop_loss_pct:
                    return TraderDecision(
                        ticker=pos.ticker,
                        decision="SELL",
                        conviction=1.0,
                        rationale=f"Stop-loss triggered: {pnl_pct:.1%}",
                    )

        # Simple momentum-based entry
        # In practice, these would come from the signal engine
        momentum = params.get("_momentum_override", tick.momentum if hasattr(tick, "momentum") else 0.5)
        rsi = params.get("_rsi_override", tick.rsi if hasattr(tick, "rsi") else 50.0)

        if momentum > momentum_threshold and rsi < rsi_overbought:
            return TraderDecision(
                ticker=tick.ticker,
                decision="BUY",
                conviction=min(momentum, 0.95),
                rationale=f"Momentum {momentum:.2f} > {momentum_threshold}, RSI {rsi:.0f}",
            )

        if rsi > rsi_overbought:
            return TraderDecision(
                ticker=tick.ticker,
                decision="SELL",
                conviction=0.8,
                rationale=f"RSI {rsi:.0f} > {rsi_overbought} (overbought)",
            )

        return TraderDecision(
            ticker=tick.ticker,
            decision="HOLD",
            conviction=0.0,
            rationale="No signal",
        )

    return trader


def walk_forward_validate(
    ticks: list,
    candidate_params: dict,
    baseline_params: dict | None = None,
    train_days: int = 90,
    val_days: int = 30,
    initial_balance: float = 100_000.0,
    cost_model=None,
) -> ValidationResult:
    """Convenience function: walk-forward validate a parameter change.

    This is the main entry point for nightly pipeline integration.
    Call it with a list of ticks + candidate parameter set, and it
    returns a structured ValidationResult.

    Args:
        ticks: Chronological market ticks (oldest first).
        candidate_params: Proposed parameter dict.
        baseline_params: Current production params (defaults to candidate
            with no changes if None — for initial bootstrap validation).
        train_days: Training window in days (default 90 per SPEC §6.1).
        val_days: Validation window in days (default 30 per SPEC §6.1).
        initial_balance: Starting cash for replay.
        cost_model: Optional CostModel.

    Returns:
        ValidationResult with accept/reject + diagnostics.
    """
    if baseline_params is None:
        baseline_params = dict(candidate_params)  # Bootstrap: compare against self

    config = WalkForwardConfig(
        train_window_days=train_days,
        val_window_days=val_days,
    )

    validator = WalkForwardValidator(config=config)
    return validator.validate(
        all_ticks=ticks,
        candidate_params=candidate_params,
        baseline_params=baseline_params,
        initial_balance=initial_balance,
        cost_model=cost_model,
    )
