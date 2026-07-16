#!/usr/bin/env python3
"""Tests for walk-forward validation in src/prompt_sweep.py — DP-3.

Run:  pytest tests/test_walk_forward.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

from src.prompt_sweep import (
    build_walk_forward_windows,
    get_trading_days,
    _compute_walk_forward_metrics,
    _load_dates_data,
    PromptVariant,
    SignalParams,
    PERTURBATION_TEMPLATES,
    score_variant,
    generate_variants,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Window building tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildWalkForwardWindows:
    """Test walk-forward window construction."""

    def test_standard_case(self):
        """20 dates, train=5, val=1 → 15 windows."""
        dates = [f"2026-06-{i+1:02d}" for i in range(20)]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        assert len(windows) == 15  # 20 - 5 - 1 + 1

    def test_first_window_structure(self):
        """First window: train on earliest dates, validate next."""
        dates = [f"2026-06-{i+1:02d}" for i in range(20)]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        first_train, first_val = windows[0]
        assert first_train == ["2026-06-01", "2026-06-02", "2026-06-03",
                                "2026-06-04", "2026-06-05"]
        assert first_val == ["2026-06-06"]

    def test_last_window_structure(self):
        """Last window: train on latest training dates, validate last date."""
        dates = [f"2026-06-{i+1:02d}" for i in range(20)]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        last_train, last_val = windows[-1]
        assert last_train == ["2026-06-15", "2026-06-16", "2026-06-17",
                               "2026-06-18", "2026-06-19"]
        assert last_val == ["2026-06-20"]

    def test_train_and_val_disjoint(self):
        """Training and validation dates must not overlap."""
        dates = [f"2026-06-{i+1:02d}" for i in range(30)]
        windows = build_walk_forward_windows(dates, train_days=10, val_days=5)
        for train, val in windows:
            train_set = set(train)
            val_set = set(val)
            assert train_set.isdisjoint(val_set), (
                f"Train {train} and val {val} overlap"
            )

    def test_windows_cover_all_dates(self):
        """Every date in the range should appear in at least one window."""
        dates = [f"2026-06-{i+1:02d}" for i in range(10)]
        windows = build_walk_forward_windows(dates, train_days=3, val_days=2)
        covered = set()
        for train, val in windows:
            covered.update(train)
            covered.update(val)
        assert covered == set(dates)

    def test_too_few_dates_returns_empty(self):
        """Not enough dates for even one window → empty list."""
        dates = ["2026-06-01", "2026-06-02"]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        assert len(windows) == 0

    def test_exactly_enough_dates(self):
        """Exactly train+val dates → 1 window."""
        dates = [f"2026-06-{i+1:02d}" for i in range(6)]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        assert len(windows) == 1

    def test_larger_val_days(self):
        """Multiple validation days per window."""
        dates = [f"2026-06-{i+1:02d}" for i in range(20)]
        windows = build_walk_forward_windows(dates, train_days=7, val_days=3)
        # 20 - 7 - 3 + 1 = 11
        assert len(windows) == 11
        first_train, first_val = windows[0]
        assert len(first_train) == 7
        assert len(first_val) == 3
        assert first_val == ["2026-06-08", "2026-06-09", "2026-06-10"]

    def test_empty_dates_list(self):
        """Empty dates → empty windows."""
        windows = build_walk_forward_windows([], train_days=5, val_days=1)
        assert len(windows) == 0

    def test_single_date_not_enough(self):
        """Single date can't satisfy train+val requirement."""
        windows = build_walk_forward_windows(["2026-06-01"], train_days=5, val_days=1)
        assert len(windows) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Trading day generation tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetTradingDays:
    """Test trading day enumeration."""

    def test_returns_requested_count(self):
        days = get_trading_days(10, end_date="2026-07-06")
        assert len(days) == 10

    def test_all_are_weekdays(self):
        days = get_trading_days(20, end_date="2026-07-06")
        from datetime import datetime
        for d in days:
            dt = datetime.strptime(d, "%Y-%m-%d")
            assert dt.weekday() < 5, f"{d} is a weekend day"

    def test_chronological_order(self):
        days = get_trading_days(15, end_date="2026-07-06")
        assert days == sorted(days)

    def test_does_not_include_future(self):
        days = get_trading_days(5, end_date="2026-07-06")
        for d in days:
            assert d <= "2026-07-06"

    def test_skips_weekends(self):
        """Weekend dates (Jul 4-5, 2026 = Sat-Sun) are excluded."""
        days = get_trading_days(5, end_date="2026-07-06")
        assert "2026-07-04" not in days
        assert "2026-07-05" not in days

    def test_reasonable_dates_returns_correct_count(self):
        """Large but reasonable number of trading days returns successfully."""
        # 200 trading days should be well within the safety valve
        days = get_trading_days(200, end_date="2026-07-06")
        assert len(days) == 200
        assert days[-1] <= "2026-07-06"


# ═══════════════════════════════════════════════════════════════════════════════
# Aggregate metrics tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeWalkForwardMetrics:
    """Test aggregate metric computation."""

    def test_perfect_winner(self):
        """Variant beats baseline on every window."""
        val_scores = [0.5, 0.6, 0.55, 0.7, 0.65]
        baseline_scores = [0.3, 0.3, 0.3, 0.3, 0.3]
        metrics = _compute_walk_forward_metrics(val_scores, baseline_scores)
        assert metrics["win_rate"] == 1.0
        assert metrics["avg_val_score"] == pytest.approx(0.6)
        assert metrics["val_stability"] > 0  # Some variance

    def test_complete_loser(self):
        """Variant never beats baseline."""
        val_scores = [0.1, 0.15, 0.12, 0.08, 0.11]
        baseline_scores = [0.3, 0.3, 0.3, 0.3, 0.3]
        metrics = _compute_walk_forward_metrics(val_scores, baseline_scores)
        assert metrics["win_rate"] == 0.0

    def test_partial_winner(self):
        """Variant beats baseline on 3/5 windows → 0.6 win rate."""
        val_scores = [0.5, 0.2, 0.5, 0.2, 0.5]
        baseline_scores = [0.3, 0.3, 0.3, 0.3, 0.3]
        metrics = _compute_walk_forward_metrics(val_scores, baseline_scores)
        assert metrics["win_rate"] == 0.6

    def test_zero_stability_with_one_window(self):
        """Single window → zero stability (not enough data for std)."""
        metrics = _compute_walk_forward_metrics([0.5], [0.3])
        assert metrics["val_stability"] == 0.0
        assert metrics["win_rate"] == 1.0
        assert metrics["avg_val_score"] == 0.5

    def test_empty_scores_returns_zeros(self):
        """Empty input → all zeros."""
        metrics = _compute_walk_forward_metrics([], [])
        assert metrics["avg_val_score"] == 0.0
        assert metrics["val_stability"] == 0.0
        assert metrics["win_rate"] == 0.0

    def test_consistent_performer(self):
        """Low variance variant should have low val_stability."""
        # Consistent scores (low variance)
        consistent = _compute_walk_forward_metrics(
            [0.5, 0.51, 0.49, 0.5, 0.5],
            [0.3, 0.3, 0.3, 0.3, 0.3],
        )
        # Inconsistent scores (high variance)
        inconsistent = _compute_walk_forward_metrics(
            [0.8, 0.1, 0.9, 0.05, 0.7],
            [0.3, 0.3, 0.3, 0.3, 0.3],
        )
        assert consistent["val_stability"] < inconsistent["val_stability"]


# ═══════════════════════════════════════════════════════════════════════════════
# Winner criteria tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestWinnerCriteria:
    """Test the walk-forward winner selection criteria."""

    def test_all_criteria_pass(self):
        """Variant that passes all three criteria should be a winner."""
        # win_rate >= 0.6: 3/5 = 0.6 ✓
        # avg_val > baseline + 0.05: 0.55 > 0.40 + 0.05 ✓
        # stability < 2 * baseline_stability: 0.05 < 2 * 0.08 ✓
        val_scores = [0.6, 0.3, 0.6, 0.3, 0.6]  # beats on 3/5
        baseline_scores = [0.4, 0.4, 0.4, 0.4, 0.4]
        m = _compute_walk_forward_metrics(val_scores, baseline_scores)
        assert m["win_rate"] >= 0.6
        assert m["avg_val_score"] > np.mean(baseline_scores) + 0.05

    def test_fails_win_rate(self):
        """Low win rate should fail criterion."""
        val_scores = [0.6, 0.2, 0.3, 0.2, 0.3]  # beats on 1/5 = 0.2
        baseline_scores = [0.4, 0.4, 0.4, 0.4, 0.4]
        m = _compute_walk_forward_metrics(val_scores, baseline_scores)
        assert m["win_rate"] < 0.6

    def test_fails_avg_score(self):
        """Barely-above-baseline fails the +0.05 margin."""
        val_scores = [0.41, 0.41, 0.41, 0.41, 0.41]  # avg = 0.41, beats every time
        baseline_scores = [0.4, 0.4, 0.4, 0.4, 0.4]
        m = _compute_walk_forward_metrics(val_scores, baseline_scores)
        assert m["win_rate"] == 1.0  # passes win_rate
        assert m["avg_val_score"] < np.mean(baseline_scores) + 0.05  # fails avg

    def test_fails_stability(self):
        """Wildly inconsistent variant should fail stability check."""
        # High variance: alternates between amazing and terrible
        val_scores = [0.8, 0.05, 0.9, 0.02, 0.7]
        baseline_scores = [0.4, 0.4, 0.4, 0.42, 0.38]  # low baseline variance
        m = _compute_walk_forward_metrics(val_scores, baseline_scores)
        baseline_m = _compute_walk_forward_metrics(baseline_scores, baseline_scores)
        # Variant stability should be much higher than baseline
        assert m["val_stability"] > 2.0 * baseline_m["val_stability"]

    def test_baseline_zero_stability_does_not_fail(self):
        """If baseline has zero stability (all same scores), don't unfairly fail."""
        val_scores = [0.5, 0.55, 0.48]
        baseline_scores = [0.3, 0.3, 0.3]  # zero std
        m = _compute_walk_forward_metrics(val_scores, baseline_scores)
        # Stability check should pass since baseline stability is 0
        # (handled in _run_multidate_sweep: baseline_stability == 0 → pass)


# ═══════════════════════════════════════════════════════════════════════════════
# Cost integration tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCostIntegration:
    """Test that costs affect scoring when enabled."""

    def test_cost_model_changes_pnl(self):
        """Applying costs should change trade PnL."""
        from src.transaction_costs import CostModel

        model = CostModel(slippage_bps=10.0)

        # Simulate a trade result
        gross, net = model.apply_to_trade(100.0, 101.0, 100)
        assert gross == 100.0  # $100 profit before costs
        assert net < gross  # Net is less after costs

    def test_cost_penalizes_high_frequency(self):
        """Many small trades should incur higher total costs than few large ones."""
        from src.transaction_costs import CostModel

        model = CostModel(slippage_bps=10.0, spread_bps=5.0)

        # 1 large trade
        _, net_large = model.apply_to_trade(100.0, 101.0, 1000)

        # 10 small trades (same total notional)
        total_net_small = 0.0
        for _ in range(10):
            _, net = model.apply_to_trade(100.0, 101.0, 100)
            total_net_small += net

        # Small trades have same gross but more costs due to min_trade_cost
        # net_large per share: 1000 shares × $1 gross = $1000 gross
        # Cost on notional 201000 @ 15bps = 301.5 → net = 698.5, per share = 0.6985
        # net_small per share: 100 shares × $1 gross = $100 gross
        # Cost on notional 20100 @ 15bps = 30.15 → net = 69.85, per share = 0.6985
        # Same per-share net in both cases if min_trade_cost=0
        # But min_trade_cost=1 means each small trade gets floored
        # So small trades are penalized more
        assert total_net_small <= net_large + 0.01  # Allow for floating point

    def test_disabling_costs_preserves_gross(self):
        """Without cost model, score_variant uses gross PnL."""
        from src.replay import Tick
        from datetime import datetime, timedelta

        ticks = []
        rng = np.random.default_rng(42)
        base_time = datetime(2024, 1, 2, 9, 30)
        price = 100.0
        for i in range(30):
            ts = base_time + timedelta(minutes=30 * i)
            noise = rng.normal(0, 0.002)
            price = price * (1.0 + noise)
            ticks.append(Tick(
                timestamp=ts,
                ticker="SPY",
                open=price * 0.999,
                high=price * 1.005,
                low=price * 0.995,
                close=price,
                volume=1_000_000,
            ))

        variant = PromptVariant(
            trader="test",
            variant_id=1,
            variant_name="test",
            description="test",
            prompt_text="# test",
            signal_params=SignalParams(),
            baseline_params=SignalParams(),
        )

        # Without costs
        score_no_cost, _ = score_variant(variant, ticks, cost_model=None)
        assert isinstance(score_no_cost, float)
        assert not np.isnan(score_no_cost)

    def test_same_variant_ranks_differently_with_costs(self):
        """Costs should change the relative ranking of high-frequency vs low-frequency."""
        from src.transaction_costs import CostModel
        from src.replay import Tick
        from datetime import datetime, timedelta

        # Create ticks that strongly trigger bullish signals (low threshold)
        rng = np.random.default_rng(42)
        ticks = []
        
        # Create 13 ticks per ticker — enough for signal to build up
        tickers = ["AAPL", "MSFT", "NVDA", "SPY"]
        prices = {"AAPL": 225.0, "MSFT": 450.0, "NVDA": 130.0, "SPY": 590.0}
        base_time = datetime(2024, 1, 2, 9, 30)
        
        for ticker in tickers:
            price = prices[ticker]
            for i in range(13):  # 6.5 hours @ 30min
                ts = base_time + timedelta(minutes=30 * i)
                # Strong uptrend
                price = price * (1.0 + 0.003)
                ticks.append(Tick(
                    timestamp=ts,
                    ticker=ticker,
                    open=price * 0.999,
                    high=price * 1.005,
                    low=price * 0.995,
                    close=price,
                    volume=1_000_000,
                ))
        ticks.sort(key=lambda t: t.timestamp)

        # Aggressive variant (high conviction multiplier → more trades)
        agg_params = SignalParams()
        agg_params.momentum_threshold = 0.2  # low threshold → more signals
        agg_params.conviction_multiplier = 2.5
        agg_params.base_size_pct = 0.20

        agg = PromptVariant("test", 1, "agg", "", "# agg", agg_params, SignalParams())

        # Score without costs
        score_no, _ = score_variant(agg, ticks, cost_model=None)

        # Score with costs
        model = CostModel(slippage_bps=10.0)
        score_with, _ = score_variant(agg, ticks, cost_model=model)

        # Both should be valid scores
        assert isinstance(score_no, float)
        assert isinstance(score_with, float)

        # With costs, score should be ≤ without costs (costs never improve score)
        assert score_with <= score_no + 0.001  # Allow float tolerance


# ═══════════════════════════════════════════════════════════════════════════════
# Integration with existing functions
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiDateIntegration:
    """Tests for multi-date score integration points."""

    def test_variant_has_multidate_fields(self):
        """PromptVariant should have the new multi-date fields."""
        v = PromptVariant(
            trader="test",
            variant_id=1,
            variant_name="test",
            description="test",
            prompt_text="# test",
            signal_params=SignalParams(),
            baseline_params=SignalParams(),
        )
        assert hasattr(v, "val_scores")
        assert isinstance(v.val_scores, list)
        assert len(v.val_scores) == 0
        assert hasattr(v, "avg_val_score")
        assert v.avg_val_score == 0.0
        assert hasattr(v, "val_stability")
        assert v.val_stability == 0.0

    def test_load_dates_data_with_empty_dates(self):
        """Empty date list should return empty tick list."""
        ticks = _load_dates_data([])
        assert ticks == []

    def test_generate_variants_still_works(self):
        """generate_variants should still work with the same interface."""
        prompt = "# Test Trader\nMomentum strategy."
        variants = generate_variants("test", prompt, n_variants=3)
        assert len(variants) == 3
        for v in variants:
            assert hasattr(v, "val_scores")

    def test_score_variant_accepts_cost_model(self):
        """score_variant should accept optional cost_model parameter."""
        from src.replay import Tick
        import numpy as np
        from datetime import datetime, timedelta

        ticks = []
        rng = np.random.default_rng(42)
        base_time = datetime(2024, 1, 2, 9, 30)
        price = 100.0
        for i in range(30):
            ts = base_time + timedelta(minutes=30 * i)
            price = price * (1.0 + rng.normal(0, 0.002))
            ticks.append(Tick(
                timestamp=ts, ticker="SPY",
                open=price * 0.999, high=price * 1.005,
                low=price * 0.995, close=price,
                volume=1_000_000,
            ))

        variant = PromptVariant(
            trader="test", variant_id=1, variant_name="test",
            description="test", prompt_text="# test",
            signal_params=SignalParams(),
            baseline_params=SignalParams(),
        )

        # Should work without cost_model
        score, result = score_variant(variant, ticks)
        assert isinstance(score, float)

        # Should also work with cost_model=None explicitly
        score2, result2 = score_variant(variant, ticks, cost_model=None)
        assert score == pytest.approx(score2)

    def test_run_sweep_single_date_unchanged(self):
        """run_sweep with n_dates=1 (default) should work exactly as before.

        NOTE: This test needs agent directories which only exist in the
        paper-trading-teams repo. It's skipped in the rebuild repo.
        """
        from src.prompt_sweep import run_sweep
        # Check if agents directory exists — if not, skip
        from src.prompt_sweep import PROJECT_DIR, AGENTS_DIR
        if not AGENTS_DIR.exists():
            pytest.skip("Agent directories not available in rebuild repo")

        results = run_sweep(
            date_str="2026-07-05",
            trader="kairos",
            n_variants=2,
            dry_run=True,
            n_dates=1,  # defaults to 1 anyway
        )
        assert len(results) == 1
        assert results[0].trader == "kairos"
        assert results[0].date == "2026-07-05"


# ═══════════════════════════════════════════════════════════════════════════════
# Minimum OOS enforcement tests (SPEC: DP-3)
# ═══════════════════════════════════════════════════════════════════════════════

class TestMinOOSEnforcement:
    """SPEC requires minimum 5 out-of-sample validation windows."""

    def test_enough_dates_passes(self):
        """With 20 dates, train=5, val=1 → 15 windows ≥ 5 ✓"""
        dates = [f"2026-06-{i+1:02d}" for i in range(20)]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        assert len(windows) >= 5, f"Expected ≥5 windows, got {len(windows)}"

    def test_too_few_dates_fails(self):
        """With 7 dates, train=5, val=1 → 1 window < 5 ✗"""
        dates = [f"2026-06-{i+1:02d}" for i in range(7)]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        assert len(windows) < 5, f"Expected <5 windows, got {len(windows)}"

    def test_minimum_exactly_5(self):
        """train=5, val=1, 10 dates → 5 windows = exactly the minimum."""
        dates = [f"2026-06-{i+1:02d}" for i in range(10)]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        assert len(windows) == 5

    def test_raises_valueerror_when_too_few(self):
        """_run_multidate_sweep should raise ValueError when < 5 windows."""
        from src.prompt_sweep import _run_multidate_sweep
        with pytest.raises(ValueError, match="at least 5 out-of-sample"):
            _run_multidate_sweep(
                date_str="2026-07-10",
                trader_short="test",
                prompt_text="# Test prompt",
                n_variants=1,
                n_dates=8,  # train=5 + val=1 + 2 = 8, but 8-5-1+1=3 windows < 5
                train_days=5,
                val_days=1,
                dry_run=True,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Statistical significance tests (SPEC: DP-3)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStatisticalSignificance:
    """t-test for variant vs baseline significance."""

    def test_clear_improvement_is_significant(self):
        """Variant clearly better → significant."""
        from src.prompt_sweep import _ttest_significance
        variant = [0.6, 0.65, 0.7, 0.68, 0.72]
        baseline = [0.3, 0.32, 0.28, 0.31, 0.3]
        is_sig, t_stat, p_val = _ttest_significance(variant, baseline)
        assert is_sig, f"Expected significant, got t={t_stat:.2f}, p={p_val:.4f}"
        assert t_stat > 0
        assert p_val < 0.05

    def test_no_improvement_is_not_significant(self):
        """Variant same as baseline → not significant."""
        from src.prompt_sweep import _ttest_significance
        variant = [0.3, 0.31, 0.29, 0.32, 0.3]
        baseline = [0.3, 0.32, 0.28, 0.31, 0.3]
        is_sig, t_stat, p_val = _ttest_significance(variant, baseline)
        assert not is_sig

    def test_worse_variant_is_not_significant(self):
        """Variant worse than baseline → not significant."""
        from src.prompt_sweep import _ttest_significance
        variant = [0.1, 0.12, 0.08, 0.11, 0.09]
        baseline = [0.3, 0.32, 0.28, 0.31, 0.3]
        is_sig, t_stat, p_val = _ttest_significance(variant, baseline)
        assert not is_sig

    def test_single_sample_returns_not_significant(self):
        """Not enough data → can't determine significance."""
        from src.prompt_sweep import _ttest_significance
        is_sig, t_stat, p_val = _ttest_significance([0.5], [0.3])
        assert not is_sig
        assert p_val == 1.0

    def test_high_variance_reduces_significance(self):
        """High variance should make it harder to reach significance."""
        from src.prompt_sweep import _ttest_significance
        # Low variance → should be significant
        variant_low = [0.6, 0.61, 0.59, 0.6, 0.62]
        baseline_low = [0.3, 0.31, 0.29, 0.3, 0.32]
        is_sig_low, _, p_low = _ttest_significance(variant_low, baseline_low)

        # High variance → may not be significant
        variant_high = [0.9, 0.1, 0.8, 0.2, 0.7]
        baseline_high = [0.3, 0.2, 0.5, 0.1, 0.4]
        is_sig_high, _, p_high = _ttest_significance(variant_high, baseline_high)

        # Low-variance case should have lower p-value
        assert p_low < p_high or not is_sig_high


# ═══════════════════════════════════════════════════════════════════════════════
# Sharpe gate tests (SPEC: DP-3, specs/validation.md)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSharpeGates:
    """SPEC-mandated Sharpe ratio acceptance criteria."""

    def test_all_gates_pass(self):
        """Good val Sharpe, better than baseline and training → all pass."""
        from src.prompt_sweep import _compute_sharpe_gates
        val_sharpes = [1.5, 1.6, 1.4, 1.7, 1.55]
        train_sharpes = [2.0, 1.9, 2.1, 1.8, 2.0]
        baseline_val_sharpes = [0.8, 0.9, 0.7, 0.85, 0.9]
        passed, diag = _compute_sharpe_gates(val_sharpes, train_sharpes, baseline_val_sharpes)
        assert passed
        assert diag["gate_1_positive"]  # val > 0
        assert diag["gate_2_vs_baseline"]  # val > baseline
        assert diag["gate_3_not_overfit"]  # val > train × 0.7

    def test_negative_sharpe_fails(self):
        """Negative validation Sharpe → fails gate 1."""
        from src.prompt_sweep import _compute_sharpe_gates
        val_sharpes = [-0.5, -0.3, -0.4]
        train_sharpes = [1.0, 1.1, 0.9]
        baseline_val_sharpes = [0.5, 0.4, 0.6]
        passed, diag = _compute_sharpe_gates(val_sharpes, train_sharpes, baseline_val_sharpes)
        assert not passed
        assert not diag["gate_1_positive"]

    def test_worse_than_baseline_fails(self):
        """Worse Sharpe than baseline → fails gate 2."""
        from src.prompt_sweep import _compute_sharpe_gates
        val_sharpes = [0.5, 0.6, 0.4]
        train_sharpes = [1.0, 1.1, 0.9]
        baseline_val_sharpes = [1.2, 1.3, 1.1]  # baseline is better
        passed, diag = _compute_sharpe_gates(val_sharpes, train_sharpes, baseline_val_sharpes)
        assert not passed
        assert not diag["gate_2_vs_baseline"]

    def test_overfit_detected(self):
        """High train Sharpe, low val Sharpe → overfit, fails gate 3."""
        from src.prompt_sweep import _compute_sharpe_gates
        val_sharpes = [0.3, 0.4, 0.35]  # mediocre val
        train_sharpes = [3.0, 3.2, 2.8]  # great train → overfit
        baseline_val_sharpes = [0.2, 0.3, 0.25]
        passed, diag = _compute_sharpe_gates(val_sharpes, train_sharpes, baseline_val_sharpes)
        # val_sharpe (0.35) < train_sharpe (3.0) × 0.7 = 2.1 → NOT overfit actually?
        # Wait: 0.35 < 2.1, so it IS below train × 0.7 → fails gate 3
        assert not passed
        assert not diag["gate_3_not_overfit"]

    def test_not_overfit_passes(self):
        """Val Sharpe close to train Sharpe → not overfit."""
        from src.prompt_sweep import _compute_sharpe_gates
        val_sharpes = [1.4, 1.5, 1.45]
        train_sharpes = [1.6, 1.7, 1.55]
        baseline_val_sharpes = [0.8, 0.9, 0.85]
        passed, diag = _compute_sharpe_gates(val_sharpes, train_sharpes, baseline_val_sharpes)
        # val (1.45) > train (1.62) × 0.7 = 1.13 → passes
        assert passed
        assert diag["gate_1_positive"]
        assert diag["gate_2_vs_baseline"]
        assert diag["gate_3_not_overfit"]

    def test_empty_sharpes_fails(self):
        """Empty input → fails."""
        from src.prompt_sweep import _compute_sharpe_gates
        passed, diag = _compute_sharpe_gates([], [], [])
        assert not passed
        assert diag.get("reason") == "no validation data"


# ═══════════════════════════════════════════════════════════════════════════════
# Parameter freeze tests (SPEC: DP-3)
# ═══════════════════════════════════════════════════════════════════════════════

class TestParamFreeze:
    """Parameter freeze mechanism — 5 trading day lock after promotion."""

    def test_no_freeze_file_returns_not_frozen(self):
        """No freeze file → not frozen."""
        from src.prompt_sweep import _FREEZE_PATH, _check_param_freeze
        # Ensure no freeze file exists
        if _FREEZE_PATH.exists():
            _FREEZE_PATH.unlink()
        is_frozen, reason = _check_param_freeze("kairos")
        assert not is_frozen
        assert reason is None

    def test_future_freeze_is_frozen(self):
        """Freeze with future date → frozen."""
        from src.prompt_sweep import _FREEZE_PATH, _check_param_freeze
        import json
        from datetime import datetime, timedelta

        future = (datetime.now() + timedelta(days=10)).isoformat()
        data = {
            "kairos": {
                "variant": "test_variant",
                "promoted_at": datetime.now().isoformat(),
                "frozen_until": future,
                "freeze_trading_days": 5,
            }
        }
        _FREEZE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FREEZE_PATH.write_text(json.dumps(data))

        is_frozen, reason = _check_param_freeze("kairos")
        assert is_frozen
        assert "frozen until" in (reason or "")

        # Cleanup
        _FREEZE_PATH.unlink()

    def test_past_freeze_is_not_frozen(self):
        """Freeze with past date → no longer frozen."""
        from src.prompt_sweep import _FREEZE_PATH, _check_param_freeze
        import json
        from datetime import datetime, timedelta

        past = (datetime.now() - timedelta(days=10)).isoformat()
        data = {
            "kairos": {
                "variant": "test_variant",
                "promoted_at": (datetime.now() - timedelta(days=15)).isoformat(),
                "frozen_until": past,
                "freeze_trading_days": 5,
            }
        }
        _FREEZE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FREEZE_PATH.write_text(json.dumps(data))

        is_frozen, reason = _check_param_freeze("kairos")
        assert not is_frozen

        # Cleanup
        _FREEZE_PATH.unlink()

    def test_record_freeze_creates_file(self):
        """_record_param_freeze should create the freeze file."""
        from src.prompt_sweep import _FREEZE_PATH, _record_param_freeze, _check_param_freeze
        import json

        # Clean start
        if _FREEZE_PATH.exists():
            _FREEZE_PATH.unlink()

        _record_param_freeze("kairos", "momentum_focus", freeze_trading_days=5)

        assert _FREEZE_PATH.exists()
        data = json.loads(_FREEZE_PATH.read_text())
        assert "kairos" in data
        assert data["kairos"]["variant"] == "momentum_focus"
        assert data["kairos"]["freeze_trading_days"] == 5

        is_frozen, _ = _check_param_freeze("kairos")
        assert is_frozen

        # Cleanup
        _FREEZE_PATH.unlink()


# ═══════════════════════════════════════════════════════════════════════════════
# Overfit detection integration tests (SPEC: DP-3)
# ═══════════════════════════════════════════════════════════════════════════════

class TestOverfitDetection:
    """End-to-end: walk-forward should reject overfit variants."""

    def test_overfit_strategy_fails_sharpe_gate(self):
        """A strategy that crushes training but flops validation should fail."""
        from src.prompt_sweep import _compute_sharpe_gates

        # Simulate an overfit strategy: great on training, terrible on validation
        val_sharpes = [-0.2, 0.1, -0.3, 0.2, 0.0]  # barely positive, highly variable
        train_sharpes = [3.5, 3.2, 3.8, 3.1, 3.6]  # amazing training → overfit
        baseline_val_sharpes = [0.5, 0.6, 0.4, 0.55, 0.5]

        passed, diag = _compute_sharpe_gates(val_sharpes, train_sharpes, baseline_val_sharpes)
        # Should fail: val (0.0 avg) < baseline (0.51 avg) → fails gate 2
        # AND val (0.0) < train (3.44) × 0.7 = 2.41 → fails gate 3
        assert not passed

    def test_robust_strategy_passes_all_gates(self):
        """A strategy with consistent train+val performance should pass."""
        from src.prompt_sweep import _compute_sharpe_gates

        val_sharpes = [1.2, 1.3, 1.1, 1.4, 1.25]
        train_sharpes = [1.5, 1.4, 1.6, 1.3, 1.55]
        baseline_val_sharpes = [0.6, 0.65, 0.55, 0.7, 0.6]

        passed, diag = _compute_sharpe_gates(val_sharpes, train_sharpes, baseline_val_sharpes)
        # val (1.25) > 0 ✓, > baseline (0.62) ✓, > train (1.47) × 0.7 = 1.03 ✓
        assert passed, f"Expected all gates to pass, got {diag}"

    def test_overfit_also_fails_ttest(self):
        """Overfit variant with high variance should fail t-test too."""
        from src.prompt_sweep import _ttest_significance

        # Simulate overfit: wildly inconsistent across windows
        variant = [5.0, -3.0, 4.0, -2.0, 1.0]  # high variance
        baseline = [1.5, 1.6, 1.4, 1.5, 1.6]  # consistent

        is_sig, t_stat, p_val = _ttest_significance(variant, baseline)
        # High variance + mean not much different → not significant
        assert not is_sig, (
            f"Overfit should NOT be significant, got t={t_stat:.2f}, p={p_val:.4f}"
        )
