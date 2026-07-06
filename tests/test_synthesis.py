"""Tests for nightly synthesis + auto-promotion.

REF: .hermes/plans/2026-07-06_learning-loop-closure.md Task 4
Every night, aggregate all journal insights from the day's sweeps into a summary.
Rank suggestions by confidence. Auto-promote changes that meet thresholds.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from typing import Dict, List

from src.journal_analyzer import JournalInsight
from src.synthesis import (
    Synthesizer,
    Promoter,
    TraderSynthesis,
    NightlySummary,
    synthesize_nightly,
    synthesize_trader,
    rank_insights,
    evaluate_promotion,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_insight(
    category: str = "HIGH_CONVICTION_LOSS",
    description: str = "Test insight",
    suggestion: str = "Test suggestion",
    confidence: float = 0.7,
    night: int = 0,
) -> JournalInsight:
    """Create a JournalInsight for testing."""
    return JournalInsight(
        category=category,
        description=description,
        suggestion=suggestion,
        confidence=confidence,
        evidence=["evidence 1"],
        night=night,
    )


def make_trader_scenarios(
    trader_name: str = "kairos",
    n_scenarios: int = 100,
    n_trades: int = 25,
    best_score: float = 3.5,
) -> dict:
    """Create scenario summary for a trader."""
    return {
        "trader": trader_name,
        "n_scenarios": n_scenarios,
        "n_trades": n_trades,
        "best_score": best_score,
        "top_variant": "v1-aggressive",
    }


# ── Rank Insights Tests ───────────────────────────────────────────────────────


class TestRankInsights:
    """Test insight ranking and deduplication."""

    def test_ranks_by_confidence(self):
        insights = [
            make_insight(confidence=0.4, description="Low confidence"),
            make_insight(confidence=0.9, description="High confidence"),
            make_insight(confidence=0.6, description="Medium confidence"),
        ]

        ranked = rank_insights(insights)
        assert ranked[0].confidence >= ranked[1].confidence >= ranked[2].confidence
        assert ranked[0].confidence == 0.9
        assert ranked[-1].confidence == 0.4

    def test_dedup_by_category_and_description(self):
        """Similar insights should be deduplicated."""
        insights = [
            make_insight(category="REGIME_WEAKNESS",
                         description="HIGH_VOLATILITY regime has losses"),
            make_insight(category="REGIME_WEAKNESS",
                         description="HIGH_VOLATILITY regime has losses"),
            make_insight(category="REGIME_WEAKNESS",
                         description="TRENDING_DOWN has worse results"),
        ]

        ranked = rank_insights(insights, dedup_threshold=0.8)
        assert len(ranked) <= 2  # One should be deduplicated

    def test_empty_list(self):
        assert rank_insights([]) == []


# ── Trader Synthesis Tests ────────────────────────────────────────────────────


class TestSynthesizeTrader:
    """Test per-trader synthesis."""

    def test_produces_trader_synthesis(self):
        insights = [
            make_insight(category="HIGH_CONVICTION_LOSS",
                         description="3 high-conviction losses on AAPL",
                         suggestion="Lower conviction multiplier",
                         confidence=0.85),
            make_insight(category="REGIME_WEAKNESS",
                         description="HIGH_VOLATILITY worst regime",
                         suggestion="Skip trades in HIGH_VOLATILITY",
                         confidence=0.72),
            make_insight(category="MISSED_OPPORTUNITY",
                         description="Missed 2 rallies",
                         suggestion="Lower momentum threshold",
                         confidence=0.55),
        ]
        scenarios = make_trader_scenarios("kairos", 324, 47, 3.5)

        synth = synthesize_trader("kairos", insights, scenarios)

        assert isinstance(synth, TraderSynthesis)
        assert synth.trader == "kairos"
        assert synth.n_scenarios == 324
        assert synth.n_trades == 47
        assert len(synth.insights) == 3
        # Top insight should have highest confidence
        assert synth.top_insight.confidence == 0.85

    def test_handles_no_insights(self):
        scenarios = make_trader_scenarios("aldridge", 50, 0, 0.0)

        synth = synthesize_trader("aldridge", [], scenarios)

        assert synth.trader == "aldridge"
        assert synth.n_trades == 0
        assert len(synth.insights) == 0
        assert synth.top_insight is None

    def test_handles_no_trades(self):
        insights = [make_insight(description="No trades — thresholds too tight")]
        scenarios = make_trader_scenarios("stonks", 100, 0, 0.0)

        synth = synthesize_trader("stonks", insights, scenarios)

        assert synth.n_trades == 0
        assert synth.top_insight is not None

    def test_trader_synthesis_to_dict(self):
        insights = [make_insight(confidence=0.8)]
        synth = synthesize_trader("kairos", insights, make_trader_scenarios())

        d = synth.to_dict()
        assert d["trader"] == "kairos"
        assert len(d["insights"]) == 1
        assert d["n_scenarios"] == 100


# ── Promotion Tests ───────────────────────────────────────────────────────────


class TestPromotion:
    """Test auto-promotion threshold logic."""

    def test_promotes_high_confidence_sustained(self):
        """Confidence > 0.75 AND sustained 3+ nights → AUTO-PROMOTE."""
        insight = make_insight(
            confidence=0.85,
            suggestion="Skip trades in HIGH_VOLATILITY regime",
            night=3,
        )

        result = evaluate_promotion(insight)
        assert result["action"] == "AUTO_PROMOTE"
        assert result["eligible"] is True

    def test_needs_more_validation_low_confidence(self):
        """Confidence < 0.5 → Needs more validation."""
        insight = make_insight(
            confidence=0.42,
            night=2,
        )

        result = evaluate_promotion(insight)
        assert result["action"] == "NEEDS_VALIDATION"
        assert result["eligible"] is False

    def test_create_pr_medium_confidence(self):
        """Confidence > 0.5 AND sustained 2 nights → Create PR."""
        insight = make_insight(
            confidence=0.65,
            night=2,
        )

        result = evaluate_promotion(insight)
        assert result["action"] == "CREATE_PR"

    def test_just_below_auto_promote(self):
        """Confidence high but not sustained enough → Create PR."""
        insight = make_insight(
            confidence=0.90,
            night=1,  # Only 1 night
        )

        result = evaluate_promotion(insight)
        assert result["action"] == "CREATE_PR"  # High conf but < 3 nights

    def test_barely_above_auto_promote_with_sustain(self):
        """Confidence at boundary with sustain → AUTO_PROMOTE."""
        insight = make_insight(
            confidence=0.76,  # Just above 0.75
            night=3,
        )

        result = evaluate_promotion(insight)
        assert result["action"] == "AUTO_PROMOTE"

    def test_promotion_returns_details(self):
        insight = make_insight(confidence=0.8, night=4, suggestion="Test change")
        result = evaluate_promotion(insight)
        assert "action" in result
        assert "eligible" in result
        assert "reason" in result
        assert "suggestion" in result
        assert result["suggestion"] == "Test change"

    def test_no_promotion_with_zero_nights(self):
        """New insights with night=0 should not be promoted."""
        insight = make_insight(confidence=0.95, night=0)
        result = evaluate_promotion(insight)
        assert result["eligible"] is False
        assert result["action"] in ("NEEDS_VALIDATION", "CREATE_PR")

    def test_promoted_insights_increment_night(self):
        """Test that the Promoter tracks night count."""
        promoter = Promoter()
        insight = make_insight(confidence=0.90, night=2)

        # First iteration: night 2 → CREATE_PR
        result, updated = promoter.evaluate_and_track(insight)
        assert result["action"] == "CREATE_PR"
        assert updated.night == 3  # Night incremented

        # Second iteration: night 3 → AUTO_PROMOTE
        result, updated = promoter.evaluate_and_track(updated)
        assert result["action"] == "AUTO_PROMOTE"
        assert updated.night == 4


# ── Synthesizer Tests ─────────────────────────────────────────────────────────


class TestSynthesizer:
    """Test the main Synthesizer class."""

    def test_synthesize_multiple_traders(self):
        kairos_insights = [
            make_insight(confidence=0.85, description="Kairos: high conviction losses",
                         night=3),
            make_insight(confidence=0.5, description="Kairos: regime weakness",
                         night=1),
        ]
        aldridge_insights = [
            make_insight(confidence=0.72, description="Aldridge: fund data missing",
                         night=2),
        ]
        stonks_insights: List[JournalInsight] = []  # Stonks: no insights

        scenarios = {
            "kairos": make_trader_scenarios("kairos", 324, 47, 3.5),
            "aldridge": make_trader_scenarios("aldridge", 100, 0, 0.0),
            "stonks": make_trader_scenarios("stonks", 156, 12, 1.2),
        }

        synthesizer = Synthesizer()
        summary = synthesizer.synthesize(
            trader_insights={
                "kairos": kairos_insights,
                "aldridge": aldridge_insights,
                "stonks": stonks_insights,
            },
            scenarios=scenarios,
            date=datetime(2026, 7, 6),
        )

        assert isinstance(summary, NightlySummary)
        assert summary.date == "2026-07-06"
        assert summary.n_traders == 3

        # Kairos should have insights
        kairos_synth = summary.trader_syntheses.get("kairos")
        assert kairos_synth is not None
        assert len(kairos_synth.insights) == 2

        # Check promotions
        assert len(summary.promotions) > 0

        # Top insight should be Kairos's high-confidence one
        auto_promo = [p for p in summary.promotions if p["action"] == "AUTO_PROMOTE"]
        assert len(auto_promo) == 1
        assert "Kairos" in auto_promo[0]["insight"]["description"]

    def test_synthesize_empty(self):
        synthesizer = Synthesizer()
        summary = synthesizer.synthesize(
            trader_insights={},
            scenarios={},
        )

        assert summary.n_traders == 0
        assert len(summary.promotions) == 0

    def test_formatted_summary(self):
        kairos_insights = [
            make_insight(confidence=0.88, description="Learning 1", night=3,
                         suggestion="Change A"),
            make_insight(confidence=0.45, description="Learning 2", night=1,
                         suggestion="Change B"),
        ]
        scenarios = {"kairos": make_trader_scenarios("kairos", 324, 47, 3.5)}

        synthesizer = Synthesizer()
        summary = synthesizer.synthesize(
            trader_insights={"kairos": kairos_insights},
            scenarios=scenarios,
        )

        formatted = summary.format()
        assert "Nightly Learning Summary" in formatted
        assert "Kairos" in formatted
        assert "AUTO-PROMOTED" in formatted
        assert "Change A" in formatted
        assert "47 trades" in formatted

    def test_convenience_function(self):
        """Test the module-level synthesize_nightly function."""
        summary = synthesize_nightly(
            trader_insights={
                "kairos": [make_insight(confidence=0.85, night=3)],
                "aldridge": [],
            },
            scenarios={
                "kairos": make_trader_scenarios("kairos"),
                "aldridge": make_trader_scenarios("aldridge", 100, 0),
            },
        )

        assert isinstance(summary, NightlySummary)
        assert len(summary.trader_syntheses) == 2

    def test_global_insight_ranking(self):
        """Top-level insights should be ranked across all traders."""
        kairos = [
            make_insight(confidence=0.9, description="K1"),
            make_insight(confidence=0.4, description="K2"),
        ]
        aldridge = [
            make_insight(confidence=0.75, description="A1"),
        ]
        stonks = [
            make_insight(confidence=0.6, description="S1"),
        ]

        synthesizer = Synthesizer()
        summary = synthesizer.synthesize(
            trader_insights={"kairos": kairos, "aldridge": aldridge, "stonks": stonks},
            scenarios={
                "kairos": make_trader_scenarios("kairos"),
                "aldridge": make_trader_scenarios("aldridge"),
                "stonks": make_trader_scenarios("stonks"),
            },
        )

        # Top ranked should be K1 (0.9)
        assert len(summary.top_insights) >= 1
        assert summary.top_insights[0].confidence == 0.9

    def test_summary_to_dict(self):
        synthesizer = Synthesizer()
        summary = synthesizer.synthesize(
            trader_insights={
                "kairos": [make_insight(confidence=0.8, night=3)],
            },
            scenarios={"kairos": make_trader_scenarios()},
        )

        d = summary.to_dict()
        assert d["date"] is not None
        assert d["n_traders"] == 1
        assert len(d["promotions"]) > 0
        assert "trader_syntheses" in d


# ── Promoter Class Tests ──────────────────────────────────────────────────────


class TestPromoterClass:
    """Test the Promoter class persistence behavior."""

    def test_tracks_insights_across_calls(self):
        promoter = Promoter()

        # Add an insight on night 1
        insight = make_insight(confidence=0.85, night=1, description="Test insight")
        result, updated = promoter.evaluate_and_track(insight)
        assert updated.night == 2

        # Evaluate again — should remember previous state
        result2, updated2 = promoter.evaluate_and_track(updated)
        assert updated2.night == 3

    def test_get_eligible_promotions(self):
        promoter = Promoter()

        # Track multiple insights
        i1 = make_insight(confidence=0.88, night=3, description="Ready to promote")
        i2 = make_insight(confidence=0.50, night=1, description="Needs time")
        i3 = make_insight(confidence=0.65, night=2, description="PR ready")

        promoter.evaluate_and_track(i1)
        promoter.evaluate_and_track(i2)
        promoter.evaluate_and_track(i3)

        eligible = promoter.get_eligible_promotions()
        # i1 should be auto-promoted, i3 should be PR-ready
        assert len(eligible) >= 1

        auto = [e for e in eligible if e["action"] == "AUTO_PROMOTE"]
        assert len(auto) >= 1
        assert auto[0]["insight"]["description"] == "Ready to promote"


# ── Nightly Flow Integration ──────────────────────────────────────────────────


class TestNightlyFlow:
    """Test the full nightly synthesis flow."""

    def test_end_to_end_nightly(self):
        """Simulate a complete nightly synthesis run."""
        synthesizer = Synthesizer()

        # Simulate insights from the journal analyzer for each trader
        trader_insights = {
            "kairos": [
                make_insight(
                    category="REGIME_WEAKNESS",
                    description="HIGH_VOLATILITY regime loses 80%",
                    suggestion="Skip trades in HIGH_VOLATILITY",
                    confidence=0.85,
                    night=3,
                ),
                make_insight(
                    category="HIGH_CONVICTION_LOSS",
                    description="3 high-conviction losses on TSLA",
                    suggestion="Reduce conviction multiplier",
                    confidence=0.5,
                    night=1,
                ),
            ],
            "aldridge": [
                make_insight(
                    category="SIZE_MISTAKE",
                    description="No trades — fundamentals data missing",
                    suggestion="Backfill fundamentals for watchlist",
                    confidence=0.72,
                    night=2,
                ),
            ],
            "stonks": [
                make_insight(
                    category="PATTERN_DETECTED",
                    description="News sentiment lags price by 2+ ticks",
                    suggestion="Use sentiment as confirmation, not trigger",
                    confidence=0.42,
                    night=1,
                ),
            ],
        }

        scenarios = {
            "kairos": make_trader_scenarios("kairos", 324, 47, 3.5),
            "aldridge": make_trader_scenarios("aldridge", 100, 0, 0.0),
            "stonks": make_trader_scenarios("stonks", 156, 12, 1.2),
        }

        summary = synthesizer.synthesize(
            trader_insights=trader_insights,
            scenarios=scenarios,
        )

        # Verify structure
        assert summary.n_traders == 3

        # Verify top-level ranking
        assert len(summary.top_insights) > 0

        # Only Kairos's high-confidence REGIME_WEAKNESS should auto-promote
        auto_promote = [p for p in summary.promotions if p["action"] == "AUTO_PROMOTE"]
        assert len(auto_promote) == 1

        # Format should produce readable output
        formatted = summary.format()
        assert "AUTO-PROMOTED" in formatted
        assert "324 scenarios" in formatted
        assert "47 trades" in formatted
        assert "Needs more validation" in formatted
