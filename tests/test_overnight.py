"""Tests for the overnight optimization harness."""

from __future__ import annotations

import tempfile

import pytest

from src.overnight_harness import OvernightHarness, OvernightResult
from src.overnight_discovery import SignalDiscoverer, DiscoveryConfig, DiscoveredSignal
from src.overnight_replay import ReplayVariantEngine, ConfigVariant, VariantResult, generate_variants
from src.replay import make_deterministic_uptrend_ticks
from src.overnight_scorer import VariantScorer, ScoredVariant
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
