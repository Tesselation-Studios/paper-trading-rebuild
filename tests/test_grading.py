"""Tests for grading — GradingScorer + LeaderboardBuilder (SPEC-v3 §1.2)."""

import json
import pytest
from src.grading import (
    GradingScorer,
    LeaderboardBuilder,
    VariantResult,
    CompositeScore,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def scorer() -> GradingScorer:
    return GradingScorer()


@pytest.fixture
def builder() -> LeaderboardBuilder:
    return LeaderboardBuilder()


@pytest.fixture
def good_variant() -> VariantResult:
    return VariantResult(
        trader_id="kairos",
        variant_id=1,
        params_hash="abc123",
        total_return_pct=12.5,
        max_drawdown=15.0,
        win_rate=55.0,
        profit_factor=1.8,
        sortino=1.5,
        calmar=0.8,
        expectancy=0.05,
        n_trades=45,
        n_ticks=1000,
    )


@pytest.fixture
def bad_variant() -> VariantResult:
    return VariantResult(
        trader_id="kairos",
        variant_id=2,
        params_hash="def456",
        total_return_pct=-25.0,
        max_drawdown=60.0,
        win_rate=30.0,
        profit_factor=0.3,
        sortino=-0.5,
        calmar=-0.3,
        expectancy=-0.02,
        n_trades=5,
        n_ticks=500,
    )


@pytest.fixture
def mixed_variants(good_variant: VariantResult, bad_variant: VariantResult) -> list:
    v3 = VariantResult(
        trader_id="kairos",
        variant_id=3,
        params_hash="ghi789",
        total_return_pct=8.3,
        max_drawdown=10.0,
        win_rate=62.0,
        profit_factor=2.1,
        sortino=2.0,
        calmar=1.2,
        expectancy=0.08,
        n_trades=60,
        n_ticks=1000,
    )
    return [good_variant, bad_variant, v3]


# ═══════════════════════════════════════════════════════════════════════════════
# GradingScorer
# ═══════════════════════════════════════════════════════════════════════════════


class TestGradingScorer:
    def test_score_good_variant(self, scorer: GradingScorer, good_variant: VariantResult):
        score = scorer.score_variant(good_variant)
        assert not score.knocked_out
        assert score.composite > 0

    def test_score_bad_variant_knocked_out(self, scorer: GradingScorer, bad_variant: VariantResult):
        score = scorer.score_variant(bad_variant)
        assert score.knocked_out
        assert score.knockout_reason

    def test_knockout_too_few_trades(self, scorer: GradingScorer):
        v = VariantResult(
            trader_id="test", variant_id=1, params_hash="x",
            total_return_pct=10, max_drawdown=10, win_rate=50,
            profit_factor=1.5, sortino=1.0, calmar=0.5, expectancy=0.03,
            n_trades=3, n_ticks=100,
        )
        score = scorer.score_variant(v)
        assert score.knocked_out
        assert "trades" in score.knockout_reason.lower()

    def test_knockout_high_drawdown(self, scorer: GradingScorer):
        v = VariantResult(
            trader_id="test", variant_id=1, params_hash="x",
            total_return_pct=10, max_drawdown=55, win_rate=50,
            profit_factor=1.5, sortino=1.0, calmar=0.5, expectancy=0.03,
            n_trades=20, n_ticks=100,
        )
        score = scorer.score_variant(v)
        assert score.knocked_out
        assert "drawdown" in score.knockout_reason.lower()

    def test_knockout_low_return(self, scorer: GradingScorer):
        v = VariantResult(
            trader_id="test", variant_id=1, params_hash="x",
            total_return_pct=-30, max_drawdown=10, win_rate=50,
            profit_factor=1.5, sortino=1.0, calmar=0.5, expectancy=0.03,
            n_trades=20, n_ticks=100,
        )
        score = scorer.score_variant(v)
        assert score.knocked_out
        assert "return" in score.knockout_reason.lower()

    def test_knockout_low_profit_factor(self, scorer: GradingScorer):
        v = VariantResult(
            trader_id="test", variant_id=1, params_hash="x",
            total_return_pct=10, max_drawdown=10, win_rate=50,
            profit_factor=0.3, sortino=1.0, calmar=0.5, expectancy=0.03,
            n_trades=20, n_ticks=100,
        )
        score = scorer.score_variant(v)
        assert score.knocked_out
        assert "profit factor" in score.knockout_reason.lower()

    def test_batch_scoring(self, scorer: GradingScorer, mixed_variants: list):
        scores = scorer.score_batch(mixed_variants)
        assert len(scores) == 3
        knocked = [s for s in scores if s.knocked_out]
        assert len(knocked) == 1  # bad_variant should be knocked out

    def test_apply_knockouts(self, scorer: GradingScorer, mixed_variants: list):
        scores = scorer.score_batch(mixed_variants)
        passing = scorer.apply_knockouts(scores)
        assert len(passing) == 2
        assert all(not s.knocked_out for s in passing)

    def test_rank_normalize(self, scorer: GradingScorer, mixed_variants: list):
        scores = scorer.score_batch(mixed_variants)
        ranked = scorer.rank_normalize(scores)
        passing = [s for s in ranked if not s.knocked_out]
        assert len(passing) >= 2
        # Sort by rank to ensure order-independent assertion
        by_rank = sorted(passing, key=lambda s: s.rank)
        assert by_rank[0].rank == 1
        # All ranks should be unique among passing
        ranks = [s.rank for s in passing]
        assert len(set(ranks)) == len(ranks)

    def test_rank_normalize_empty(self, scorer: GradingScorer):
        assert scorer.rank_normalize([]) == []

    def test_build_leaderboard(self, scorer: GradingScorer, mixed_variants: list):
        scores = scorer.build_leaderboard(mixed_variants)
        assert len(scores) == 3  # includes knocked-out
        # Passing variants first
        non_ko = [s for s in scores if not s.knocked_out]
        ko = [s for s in scores if s.knocked_out]
        assert len(non_ko) == 2
        assert len(ko) == 1
        # All passing variants should have ranks
        assert all(s.rank > 0 for s in non_ko)
        assert all(s.rank_normalized >= 0 for s in non_ko)

    def test_cap_score(self, scorer: GradingScorer):
        assert scorer._cap_score(0, 5.0) == 0.0
        assert scorer._cap_score(-1, 5.0) == 0.0
        assert scorer._cap_score(2.5, 5.0) == 0.5
        assert scorer._cap_score(10, 5.0) == 1.0

    def test_weights_are_valid(self, scorer: GradingScorer):
        total = sum(scorer.weights.values())
        assert abs(total - 1.0) < 0.001, f"Weights sum to {total}, expected 1.0"

    def test_custom_weights(self):
        custom = GradingScorer(weights={"calmar": 0.5, "sortino": 0.5})
        assert sum(custom.weights.values()) == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# LeaderboardBuilder
# ═══════════════════════════════════════════════════════════════════════════════


class TestLeaderboardBuilder:
    def test_to_canvas_card(self, builder: LeaderboardBuilder, good_variant: VariantResult):
        scorer = GradingScorer()
        score = scorer.score_variant(good_variant)
        card = builder.to_canvas_card([score])
        assert "Leaderboard" in card
        assert "kairos" in card
        assert "## " in card

    def test_to_canvas_card_with_knockout(self, builder: LeaderboardBuilder, mixed_variants: list):
        scorer = GradingScorer()
        scores = scorer.build_leaderboard(mixed_variants)
        card = builder.to_canvas_card(scores)
        assert "knocked out" in card.lower()
        assert "KO" in card

    def test_to_dashboard_json(self, builder: LeaderboardBuilder, mixed_variants: list):
        scorer = GradingScorer()
        scores = scorer.build_leaderboard(mixed_variants)
        js = builder.to_dashboard_json(scores)
        data = json.loads(js)
        assert "leaderboard" in data
        assert "metadata" in data
        assert data["metadata"]["n_total"] == 3
        assert data["metadata"]["n_passing"] == 2

    def test_to_fusion_format(self, builder: LeaderboardBuilder, mixed_variants: list):
        scorer = GradingScorer()
        scores = scorer.build_leaderboard(mixed_variants)
        result = builder.to_fusion_format(scores)
        assert "variants" in result
        assert "summary" in result
        assert result["summary"]["passing"] == 2