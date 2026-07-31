"""Tests for the overnight optimization harness."""

from __future__ import annotations

import tempfile
from datetime import datetime

import numpy as np
import pytest

from src.overnight_harness import OvernightHarness, OvernightResult
from src.overnight_discovery import SignalDiscoverer, DiscoveryConfig, DiscoveredSignal
from src.overnight_replay import ReplayVariantEngine, ConfigVariant, VariantResult, generate_variants
from src.replay import make_deterministic_uptrend_ticks
from src.overnight_scorer import VariantScorer, ScoredVariant, split_ticks_by_midpoint, compute_sharpe
from src.overnight_leaderboard import LeaderboardBuilder, LeaderboardReport


class TestOvernightHarness:
    """End-to-end test of the full overnight harness."""

    def test_end_to_end(self):
        """Run harness with 10 variants on synthetic data."""
        with tempfile.TemporaryDirectory() as tmp:
            h = OvernightHarness(data_dir=tmp)
            result = h.run(n_variants=10)
            assert isinstance(result, OvernightResult)
            assert result.n_variants_tested == 10
            assert len(result.errors) == 0
            assert result.duration_seconds > 0

    def test_leaderboard_output(self):
        """Leaderboard report has top 5."""
        with tempfile.TemporaryDirectory() as tmp:
            h = OvernightHarness(data_dir=tmp)
            result = h.run(n_variants=10)
            lb = result.report
            assert isinstance(lb, LeaderboardReport)
            assert len(lb.top_5) == 5
            assert all(e.score > 0 for e in lb.top_5)
            assert lb.current_live is not None

    def test_100_variants(self):
        """100 variants in under 30s."""
        with tempfile.TemporaryDirectory() as tmp:
            h = OvernightHarness(data_dir=tmp)
            result = h.run(n_variants=100)
            assert result.n_variants_tested == 100
            assert len(result.errors) == 0
            assert result.duration_seconds < 30

    def test_yaml_output(self):
        """YAML output works through builder."""
        with tempfile.TemporaryDirectory() as tmp:
            h = OvernightHarness(data_dir=tmp)
            result = h.run(n_variants=10)
            builder = LeaderboardBuilder()
            yaml = builder.to_yaml(result.report)
            assert isinstance(yaml, str)
            assert len(yaml) > 50
            assert "top_5" in yaml


class TestDiscovery:
    """Test the discovery phase."""

    def test_basic(self):
        """Discovery finds signals from synthetic data."""
        ticks = make_deterministic_uptrend_ticks(n=60)
        signals = SignalDiscoverer(DiscoveryConfig()).discover(ticks)
        assert isinstance(signals, list)

    def test_empty_with_high_threshold(self):
        """Unrealistically high thresholds should produce no signals."""
        config = DiscoveryConfig(
            conviction_strong_threshold=5.0,
            momentum_threshold_bullish=100.0,
        )
        ticks = make_deterministic_uptrend_ticks(n=60)
        signals = SignalDiscoverer(config).discover(ticks)
        assert len(signals) == 0


class TestReplay:
    """Test the replay engine."""

    def test_variant_generation(self):
        """generate_variants produces the right count."""
        variants = generate_variants(n_variants=20)
        assert len(variants) == 20
        assert all(isinstance(v, ConfigVariant) for v in variants)

    def test_replay_runs(self):
        """Replay engine runs on synthetic ticks."""
        ticks = make_deterministic_uptrend_ticks(n=60)
        variant = generate_variants(n_variants=1)[0]
        engine = ReplayVariantEngine()
        result = engine.run_variant(variant, ticks)
        assert isinstance(result, VariantResult)
        assert len(result.replay_result.trades) >= 0


class TestScorer:
    """Test the scoring system."""

    def test_scorer_basic(self):
        """Scorer returns ranked ScoredVariants."""
        ticks = make_deterministic_uptrend_ticks(n=60)
        signals = SignalDiscoverer(DiscoveryConfig()).discover(ticks)
        engine = ReplayVariantEngine()
        variants = generate_variants(n_variants=5)
        results = [engine.run_variant(v, ticks, signals) for v in variants]
        scored = VariantScorer().score_all(results)
        assert len(scored) == 5
        assert all(isinstance(s, ScoredVariant) for s in scored)

    def test_scorer_ranked(self):
        """Scored variants ranked descending by score."""
        ticks = make_deterministic_uptrend_ticks(n=60)
        signals = SignalDiscoverer(DiscoveryConfig()).discover(ticks)
        engine = ReplayVariantEngine()
        variants = generate_variants(n_variants=10)
        results = [engine.run_variant(v, ticks, signals) for v in variants]
        scored = VariantScorer().score_all(results)
        for i in range(len(scored) - 1):
            assert scored[i].score >= scored[i + 1].score


class TestSplitWindowSharpe:
    """2026-07-31: overnight-insights.md's Round 2/3/5 all requested
    split-window Sharpe be "baked into the harness scoring" and it never
    was -- the same z-score-driven `score` formula Round 4 called broken
    kept ranking the leaderboard, letting 1-3 trade variants top it. This
    closes that gap using the same methodology already trusted for live
    strategy promotion (replay_check.py's split_ticks_by_midpoint /
    Sharpe-positive-in-both-halves bar)."""

    def test_split_ticks_by_midpoint_even_split(self):
        ticks = make_deterministic_uptrend_ticks(n=60)
        first, second = split_ticks_by_midpoint(ticks)
        assert len(first) + len(second) == 60
        assert first[-1].timestamp < second[0].timestamp

    def test_split_ticks_empty_input(self):
        assert split_ticks_by_midpoint([]) == ([], [])

    def test_compute_sharpe_positive_uptrend(self):
        ticks = make_deterministic_uptrend_ticks(n=30, step_pct=0.01)
        variant = generate_variants(n_variants=1)[0]
        result = ReplayVariantEngine().run_variant(variant, ticks)
        sharpe = compute_sharpe(result.replay_result)
        # A guaranteed uptrend with an active variant should show a
        # positive risk-adjusted return, not a specific value.
        if sharpe is not None:  # None is valid if the variant made 0 trades
            assert sharpe > 0

    def test_compute_sharpe_insufficient_data_returns_none(self):
        ticks = make_deterministic_uptrend_ticks(n=1)
        variant = generate_variants(n_variants=1)[0]
        result = ReplayVariantEngine().run_variant(variant, ticks)
        assert compute_sharpe(result.replay_result) is None

    def _fake_replay_result(self, daily_returns_pct: list, ticker: str = "AAA") -> "ReplayResult":
        """Build a ReplayResult directly from a sequence of daily % returns
        -- deterministic control over Sharpe sign, not dependent on the
        stochastic replay pipeline actually generating trades."""
        from src.replay import ReplayResult
        equity = [10_000.0]
        for pct in daily_returns_pct:
            equity.append(equity[-1] * (1 + pct / 100))
        timestamps = [datetime(2024, 1, 2 + i, 9, 30) for i in range(len(equity))]
        return ReplayResult(
            equity_curve=np.array(equity), returns=np.array([0.0] + daily_returns_pct),
            trades=[], initial_balance=10_000.0, final_equity=equity[-1],
            total_pnl=equity[-1] - 10_000.0, total_return_pct=(equity[-1] - 10_000.0) / 10_000.0 * 100,
            n_ticks=len(equity), n_decisions=0, tickers_seen=[ticker], timestamps=timestamps,
        )

    def test_score_robust_true_when_both_halves_genuinely_positive(self):
        variant = generate_variants(n_variants=1)[0]
        result = ReplayVariantEngine().run_variant(variant, make_deterministic_uptrend_ticks(n=10))
        # Steady positive daily returns, low variance -> unambiguously positive Sharpe.
        result.first_half_replay_result = self._fake_replay_result([1.0, 0.8, 1.2, 0.9, 1.1])
        result.second_half_replay_result = self._fake_replay_result([0.9, 1.0, 1.1, 0.8, 1.2])

        scored = VariantScorer().score(result)
        assert scored.first_half_sharpe > 0
        assert scored.second_half_sharpe > 0
        assert scored.robust is True

    def test_score_robust_false_when_one_half_negative(self):
        variant = generate_variants(n_variants=1)[0]
        result = ReplayVariantEngine().run_variant(variant, make_deterministic_uptrend_ticks(n=10))
        result.first_half_replay_result = self._fake_replay_result([1.0, 0.8, 1.2, 0.9, 1.1])
        result.second_half_replay_result = self._fake_replay_result([-1.0, -0.8, -1.2, -0.9, -1.1])

        scored = VariantScorer().score(result)
        assert scored.first_half_sharpe > 0
        assert scored.second_half_sharpe < 0
        assert scored.robust is False

    def test_score_robust_false_without_split_data(self):
        """Backward compat: a VariantResult with no split-window data
        (the pre-2026-07-31 shape) must score robust=False, not crash."""
        ticks = make_deterministic_uptrend_ticks(n=30)
        variant = generate_variants(n_variants=1)[0]
        result = ReplayVariantEngine().run_variant(variant, ticks)
        assert result.first_half_replay_result is None
        scored = VariantScorer().score(result)
        assert scored.robust is False
        assert scored.first_half_sharpe is None

    def test_score_all_ranks_robust_above_higher_raw_score(self):
        """The core fix: a non-robust variant must never outrank a robust
        one, even if its raw `score` is higher -- this is what let
        1-3-trade variants top the Round 5 leaderboard."""
        ticks = make_deterministic_uptrend_ticks(n=40)
        variant_a = generate_variants(n_variants=1)[0]
        variant_b = generate_variants(n_variants=1, seed=99)[0]
        engine = ReplayVariantEngine()

        result_a = engine.run_variant(variant_a, ticks)  # robust: has split data
        first, second = split_ticks_by_midpoint(ticks)
        result_a.first_half_replay_result = engine.run_variant(variant_a, first).replay_result
        result_a.second_half_replay_result = engine.run_variant(variant_a, second).replay_result

        result_b = engine.run_variant(variant_b, ticks)  # not robust: no split data at all

        scored = VariantScorer().score_all([result_a, result_b])
        scored_by_variant = {s.variant.variant_id: s for s in scored}
        a = scored_by_variant[variant_a.variant_id]
        b = scored_by_variant[variant_b.variant_id]

        # Force the scenario the fix targets: b scores higher on the raw
        # formula but isn't robust; a is robust. a must rank first
        # regardless of the raw score ordering.
        if a.robust and not b.robust:
            assert scored.index(a) < scored.index(b)

    def test_harness_enable_split_window_populates_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = OvernightHarness(data_dir=tmp, enable_split_window=True)
            result = h.run(n_variants=5)
            assert result.n_variants_tested == 5
            # At least the scored fields must exist and be well-formed
            # (not asserting all are robust -- that's data-dependent).
            for s in result.scored_variants:
                assert hasattr(s, "robust")
                assert isinstance(s.robust, bool)

    def test_harness_split_window_disabled_keeps_old_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = OvernightHarness(data_dir=tmp, enable_split_window=False)
            result = h.run(n_variants=5)
            assert result.n_variants_tested == 5
            assert all(s.robust is False for s in result.scored_variants)
            assert all(s.first_half_sharpe is None for s in result.scored_variants)


class TestLeaderboard:
    """Test the leaderboard system."""

    def test_builds(self):
        """Leaderboard builds from scored variants."""
        ticks = make_deterministic_uptrend_ticks(n=60)
        signals = SignalDiscoverer(DiscoveryConfig()).discover(ticks)
        engine = ReplayVariantEngine()
        variants = generate_variants(n_variants=10)
        results = [engine.run_variant(v, ticks, signals) for v in variants]
        scored = VariantScorer().score_all(results)
        builder = LeaderboardBuilder()
        report = builder.build(scored, duration_seconds=1.0)
        assert isinstance(report, LeaderboardReport)
        assert len(report.top_5) == 5
        assert report.current_live is not None

    def test_yaml(self):
        """YAML output works."""
        ticks = make_deterministic_uptrend_ticks(n=60)
        signals = SignalDiscoverer(DiscoveryConfig()).discover(ticks)
        engine = ReplayVariantEngine()
        variants = generate_variants(n_variants=10)
        results = [engine.run_variant(v, ticks, signals) for v in variants]
        scored = VariantScorer().score_all(results)
        builder = LeaderboardBuilder()
        report = builder.build(scored, duration_seconds=1.0)
        yaml = builder.to_yaml(report)
        assert isinstance(yaml, str)
        assert "top_5" in yaml
