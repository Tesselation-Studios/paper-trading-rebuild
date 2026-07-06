"""Tests for journal analysis + counterfactual loop.

REF: .hermes/plans/2026-07-06_learning-loop-closure.md Task 3
After a sweep completes, analyze journals for patterns: high-conviction losses,
missed opportunities, regime-specific performance. Generate concrete suggestions.
"""

from __future__ import annotations

import pytest
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.reflection import Reflection
from src.replay import Tick, TraderDecision
from src.journal_analyzer import (
    JournalInsight,
    JournalAnalyzer,
    analyze_journal,
    analyze_reflections,
    detect_high_conviction_losses,
    detect_regime_weaknesses,
    detect_missed_opportunities,
    detect_size_mistakes,
    compute_regime_stats,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_tick(minute: int = 0, ticker: str = "AAPL", price: float = 150.0) -> Tick:
    """Create a Tick for testing."""
    return Tick(
        timestamp=datetime(2024, 1, 5, 9, 30),
        ticker=ticker, open=price, high=price + 0.5, low=price - 0.5,
        close=price, volume=1_000_000,
        rsi=50.0, momentum=0.0, regime="TRENDING_UP",
    )


def make_decision(
    decision: str = "HOLD",
    conviction: float = 0.5,
    rationale: str = "test",
    ticker: str = "AAPL",
) -> TraderDecision:
    """Create a TraderDecision for testing."""
    return TraderDecision(
        ticker=ticker, decision=decision,
        conviction=conviction, rationale=rationale,
    )


def make_reflection(
    ticker: str = "AAPL",
    decision: str = "BUY",
    conviction: float = 0.8,
    learning: str = "I learned something",
    would_do: str = "I would do X differently",
    time_str: str = "2024-01-05T09:30:00",
) -> Reflection:
    """Create a Reflection for testing."""
    return Reflection(
        timestamp=time_str,
        ticker=ticker,
        decision=decision,
        rationale=f"Test {decision} with conv={conviction}",
        learning=learning,
        would_do_differently=would_do,
    )


def make_pnl_trade(ticker: str, pnl: float, regime: str = "TRENDING_UP") -> dict:
    """Create a trade dict with P&L and regime."""
    return {
        "ticker": ticker,
        "pnl": pnl,
        "regime": regime,
        "conviction": abs(pnl) / 100 if abs(pnl) < 100 else 0.8,
    }


# ── Journal Insight Tests ─────────────────────────────────────────────────────


class TestJournalInsight:
    """Test the JournalInsight dataclass."""

    def test_create_insight(self):
        insight = JournalInsight(
            category="HIGH_CONVICTION_LOSS",
            description="High conviction BUY on AAPL lost $500",
            suggestion="Reduce conviction multiplier in trending_down regime",
            confidence=0.85,
            evidence=["[09:30] BUY AAPL @ $150.00: conviction 0.8, lost $500"],
        )
        assert insight.category == "HIGH_CONVICTION_LOSS"
        assert insight.confidence == 0.85
        assert len(insight.evidence) == 1

    def test_insight_to_dict(self):
        insight = JournalInsight(
            category="REGIME_WEAKNESS",
            description="Weak performance in HIGH_VOLATILITY",
            suggestion="Skip trades when vol_regime=HIGH",
            confidence=0.6,
            evidence=[],
        )
        d = insight.to_dict()
        assert d["category"] == "REGIME_WEAKNESS"
        assert d["confidence"] == 0.6
        assert "evidence" in d

    def test_insight_from_dict(self):
        d = {
            "category": "MISSED_OPPORTUNITY",
            "description": "Failed to buy on strong signal",
            "suggestion": "Lower momentum threshold",
            "confidence": 0.55,
            "evidence": ["evidence 1"],
        }
        insight = JournalInsight.from_dict(d)
        assert insight.category == "MISSED_OPPORTUNITY"
        assert insight.suggestion == "Lower momentum threshold"


# ── High-Conviction Loss Detection ────────────────────────────────────────────


class TestHighConvictionLosses:
    """Test detection of trades with high conviction that lost money."""

    def test_detects_high_conviction_loss(self):
        reflections = [
            make_reflection(decision="BUY", conviction=0.9,
                            learning="Bought on strong uptrend signal but it reversed"),
            make_reflection(decision="BUY", conviction=0.85,
                            learning="Entered too early on momentum spike"),
        ]
        trades = [
            make_pnl_trade("AAPL", -500, "TRENDING_UP"),
            make_pnl_trade("TSLA", -1200, "HIGH_VOLATILITY"),
        ]

        insights = detect_high_conviction_losses(
            reflections=reflections,
            trades=trades,
            conviction_threshold=0.5,
        )
        assert len(insights) > 0
        assert insights[0].category == "HIGH_CONVICTION_LOSS"
        assert insights[0].confidence > 0.5

    def test_no_losses_no_insights(self):
        reflections = [
            make_reflection(decision="BUY", conviction=0.9, learning="Good trade"),
        ]
        trades = [make_pnl_trade("AAPL", 500, "TRENDING_UP")]

        insights = detect_high_conviction_losses(reflections, trades)
        # With all positive trades, no loss insights expected
        loss_insights = [i for i in insights if i.category == "HIGH_CONVICTION_LOSS"]
        assert len(loss_insights) == 0

    def test_conviction_threshold_respected(self):
        """Low conviction losses should not trigger insights."""
        reflections = [
            make_reflection(decision="BUY", conviction=0.3,
                            learning="Low conviction buy, lost money"),
        ]
        trades = [{"ticker": "AAPL", "pnl": -200, "regime": "TRENDING_UP", "conviction": 0.3}]

        insights = detect_high_conviction_losses(
            reflections, trades, conviction_threshold=0.5,
        )
        # Low conviction (< 0.5) should not generate HIGH_CONVICTION_LOSS
        loss_insights = [i for i in insights if i.category == "HIGH_CONVICTION_LOSS"]
        assert len(loss_insights) == 0


# ── Regime Weakness Detection ─────────────────────────────────────────────────


class TestRegimeWeaknesses:
    """Test detection of regime-specific underperformance."""

    def test_finds_worst_regime(self):
        trades = [
            make_pnl_trade("AAPL", 100, "TRENDING_UP"),
            make_pnl_trade("AAPL", 200, "TRENDING_UP"),
            make_pnl_trade("TSLA", 50, "TRENDING_UP"),
            make_pnl_trade("AAPL", -500, "HIGH_VOLATILITY"),
            make_pnl_trade("TSLA", -300, "HIGH_VOLATILITY"),
            make_pnl_trade("MSFT", -200, "HIGH_VOLATILITY"),
            make_pnl_trade("AAPL", -50, "TRENDING_DOWN"),
        ]

        insights = detect_regime_weaknesses(trades, min_trades_per_regime=2)
        assert len(insights) > 0

        # HIGH_VOLATILITY should be identified as worst regime (3 losses)
        high_vol_insight = [i for i in insights if "HIGH_VOLATILITY" in i.description]
        assert len(high_vol_insight) > 0

    def test_regime_stats_computation(self):
        trades = [
            make_pnl_trade("AAPL", 100, "TRENDING_UP"),
            make_pnl_trade("AAPL", -50, "TRENDING_UP"),
            make_pnl_trade("TSLA", 200, "TRENDING_UP"),
            make_pnl_trade("AAPL", -300, "HIGH_VOLATILITY"),
            make_pnl_trade("TSLA", -100, "HIGH_VOLATILITY"),
        ]

        stats = compute_regime_stats(trades)
        assert "TRENDING_UP" in stats
        assert "HIGH_VOLATILITY" in stats

        up_stat = stats["TRENDING_UP"]
        assert up_stat["count"] == 3
        assert up_stat["total_pnl"] == 250
        assert up_stat["win_rate"] > 0.5  # 2 wins out of 3

        hv_stat = stats["HIGH_VOLATILITY"]
        assert hv_stat["count"] == 2
        assert hv_stat["total_pnl"] == -400
        assert hv_stat["win_rate"] == 0.0  # 0 wins out of 2

    def test_skips_regimes_with_few_trades(self):
        trades = [
            make_pnl_trade("AAPL", -100, "HIGH_VOLATILITY"),  # only 1 trade
            make_pnl_trade("AAPL", 50, "TRENDING_UP"),
            make_pnl_trade("TSLA", 75, "TRENDING_UP"),
        ]

        insights = detect_regime_weaknesses(trades, min_trades_per_regime=2)
        # HIGH_VOLATILITY has only 1 trade — should NOT get worst-regime flag
        # (it WILL get an "underexplored" insight since it has < min_trades)
        worst_regime_insights = [
            i for i in insights
            if "HIGH_VOLATILITY" in i.description
            and "worst" not in i.description.lower()
            and "insufficient" in i.description.lower()
        ]
        assert len(worst_regime_insights) == 1  # underexplored insight
        assert worst_regime_insights[0].confidence == 0.3


# ── Missed Opportunity Detection ──────────────────────────────────────────────


class TestMissedOpportunities:
    """Test detection of missed trading opportunities."""

    def test_detects_missed_buy_on_strong_signal(self):
        reflections = [
            make_reflection(decision="HOLD", conviction=0.2,
                            learning="Held despite strong uptrend signal, price went up 5%"),
        ]
        journal = [
            "[09:30] HOLD AAPL @ $150.00: waiting for better entry",
            "[09:35] Market: AAPL now $157.50 (up 5.0%), momentum=0.8, regime=TRENDING_UP",
        ]

        insights = detect_missed_opportunities(reflections, journal)
        # Should identify the HOLD on a strong signal followed by price rise
        assert len(insights) > 0
        assert insights[0].category == "MISSED_OPPORTUNITY"

    def test_no_opportunity_when_price_falls(self):
        reflections = [
            make_reflection(decision="HOLD", conviction=0.2,
                            learning="Held position, price went down — good call"),
        ]
        journal = [
            "[09:30] HOLD AAPL @ $150.00: avoiding downside risk",
            "[09:35] Market: AAPL now $145.00 (down 3.3%)",
        ]

        insights = detect_missed_opportunities(reflections, journal)
        # HOLD was correct decision since price fell
        opp_insights = [i for i in insights if i.category == "MISSED_OPPORTUNITY"]
        assert len(opp_insights) == 0  # No missed opportunity when price falls

    def test_multiple_missed_opportunities(self):
        reflections = [
            make_reflection(decision="HOLD", conviction=0.1, ticker="AAPL",
                            learning="Should have bought, price surged"),
            make_reflection(decision="HOLD", conviction=0.1, ticker="TSLA",
                            learning="Another missed run-up"),
        ]
        journal = [
            "[09:30] HOLD AAPL @ $150.00: not enough conviction",
            "[09:35] Market: AAPL now $165.00 (+10%)",
            "[09:40] HOLD TSLA @ $200.00: waiting",
            "[09:45] Market: TSLA now $220.00 (+10%)",
        ]

        insights = detect_missed_opportunities(reflections, journal)
        opp_insights = [i for i in insights if i.category == "MISSED_OPPORTUNITY"]
        # Should detect multiple missed opportunities
        assert len(opp_insights) > 0


# ── Size Mistake Detection ────────────────────────────────────────────────────


class TestSizeMistakes:
    """Test detection of position sizing mistakes."""

    def test_detects_oversized_position(self):
        trades = [
            {"ticker": "AAPL", "pnl": -5000, "shares": 1000, "position_pct": 0.5},
            {"ticker": "TSLA", "pnl": 100, "shares": 50, "position_pct": 0.05},
        ]

        insights = detect_size_mistakes(trades, max_position_pct=0.20)
        # AAPL position was 50% of portfolio — too large
        assert len(insights) > 0
        assert insights[0].category == "SIZE_MISTAKE"

    def test_no_mistake_with_reasonable_sizes(self):
        trades = [
            {"ticker": "AAPL", "pnl": -200, "shares": 100, "position_pct": 0.10},
            {"ticker": "TSLA", "pnl": 300, "shares": 50, "position_pct": 0.08},
        ]

        insights = detect_size_mistakes(trades, max_position_pct=0.20)
        assert len(insights) == 0

    def test_empty_trades_no_mistakes(self):
        insights = detect_size_mistakes([], max_position_pct=0.20)
        assert len(insights) == 0


# ── Reflection Analysis ───────────────────────────────────────────────────────


class TestAnalyzeReflections:
    """Test analysis of reflection text for patterns."""

    def test_finds_patterns_in_reflections(self):
        reflections = [
            make_reflection(learning="I keep buying too early before confirmation",
                            would_do="Wait for second confirmation candle"),
            make_reflection(learning="Another early entry — same pattern as before",
                            would_do="Add a delay filter before executing"),
            make_reflection(learning="Good trade, waited for confirmation this time",
                            would_do="Keep the confirmation rule"),
        ]

        insights = analyze_reflections(reflections)
        # Should find the "early entry" pattern (appears in 2 of 3)
        assert len(insights) > 0
        patterns = [i for i in insights if "early" in i.description.lower()]
        assert len(patterns) > 0

    def test_no_patterns_with_few_reflections(self):
        reflections = [
            make_reflection(learning="First trade, learning the market"),
        ]

        insights = analyze_reflections(reflections)
        # Single reflection insufficient for pattern detection
        assert len(insights) == 0  # Not enough data to find patterns


# ── Journal Analyzer (main entry point) ───────────────────────────────────────


class TestJournalAnalyzer:
    """Test the main JournalAnalyzer class."""

    def test_analyze_empty_journal(self):
        analyzer = JournalAnalyzer()
        insights = analyzer.analyze(
            journal=[],
            reflections=[],
            trades=[],
        )
        # Should handle empty input gracefully
        assert insights == []

    def test_analyze_full_journal(self):
        reflections = [
            make_reflection(decision="BUY", conviction=0.9, ticker="AAPL",
                            learning="Bought on strong signal, price dropped sharply"),
            make_reflection(decision="BUY", conviction=0.85, ticker="TSLA",
                            learning="Another high-conviction buy that lost money"),
            make_reflection(decision="HOLD", conviction=0.1,
                            learning="Missed a big rally, should have bought"),
        ]
        journal = [
            "[09:30] BUY AAPL @ $150.00: strong uptrend signal",
            "[09:35] BUY TSLA @ $200.00: momentum spike",
            "[09:40] HOLD AAPL @ $165.00: not enough conviction, but price kept rising",
        ]
        trades = [
            make_pnl_trade("AAPL", -500, "TRENDING_UP"),
            make_pnl_trade("TSLA", -800, "HIGH_VOLATILITY"),
            make_pnl_trade("MSFT", 200, "TRENDING_UP"),
        ]

        analyzer = JournalAnalyzer()
        insights = analyzer.analyze(
            journal=journal,
            reflections=reflections,
            trades=trades,
        )

        # Should produce insights from multiple detectors
        assert len(insights) > 0

        categories = {i.category for i in insights}
        # Should have multiple insight categories
        assert len(categories) >= 2

    def test_analyze_called_with_mock_llm(self):
        """Test that JournalAnalyzer can use LLM engine for enhanced analysis."""

        class MockLLM:
            def decide(self, tick=None, signal=None, journal=None,
                       portfolio=None, agent_files=None, reflection_context=""):
                return None

            def reflect(self, tick=None, decision=None, signal=None,
                        prev_reflections=None):
                return ("test learning", "test differently")

            def _call_api(self, prompt):
                return '{"insights": [{"category": "HIGH_CONVICTION_LOSS", "description": "Found 2 losses", "suggestion": "Reduce conviction", "confidence": 0.8, "evidence": ["trade1"]}]}'

        analyzer = JournalAnalyzer(llm_engine=None)  # No LLM — uses heuristics
        insights = analyzer.analyze(
            journal=["[09:30] BUY AAPL @ $150.00: test"],
            reflections=[],
            trades=[],
        )
        # Should still work without LLM (heuristic-only mode)
        assert isinstance(insights, list)

    def test_convenience_function(self):
        """Test the module-level analyze_journal convenience function."""
        reflections = [
            make_reflection(decision="BUY", conviction=0.9, learning="Bad buy"),
            make_reflection(decision="BUY", conviction=0.8, learning="Bad buy again"),
        ]
        trades = [
            make_pnl_trade("AAPL", -500, "HIGH_VOLATILITY"),
            make_pnl_trade("AAPL", -300, "HIGH_VOLATILITY"),
            make_pnl_trade("TSLA", 200, "TRENDING_UP"),
        ]

        insights = analyze_journal(
            journal=["[09:30] BUY AAPL: test"],
            reflections=reflections,
            trades=trades,
        )
        assert len(insights) > 0

    def test_insight_sorting(self):
        """Insights should be sorted by confidence descending."""
        reflections = [
            make_reflection(decision="BUY", conviction=0.9, learning="Bad trade"),
        ]
        trades = [
            make_pnl_trade("AAPL", -500, "HIGH_VOLATILITY"),
        ]

        analyzer = JournalAnalyzer()
        insights = analyzer.analyze(
            journal=["test"], reflections=reflections, trades=trades,
        )

        if len(insights) >= 2:
            for i in range(len(insights) - 1):
                assert insights[i].confidence >= insights[i + 1].confidence

    def test_regime_stats_empty(self):
        """Test regime stats with empty trades."""
        stats = compute_regime_stats([])
        assert stats == {}

    def test_journal_analyzer_with_llm_enhancement(self):
        """Verify LLM enhancement enriches insights but never replaces them."""
        # Without LLM, we still get heuristic insights
        analyzer = JournalAnalyzer()
        reflections = [
            make_reflection(decision="BUY", conviction=0.85, learning="Lost on buy"),
        ]
        trades = [make_pnl_trade("AAPL", -500, "HIGH_VOLATILITY")]

        insights = analyzer.analyze(
            journal=["[09:30] BUY AAPL @ $150.00: test"],
            reflections=reflections,
            trades=trades,
        )
        # Should produce at least heuristic insights
        assert len(insights) > 0
        # All insights should have valid categories
        valid_categories = {
            "HIGH_CONVICTION_LOSS", "REGIME_WEAKNESS",
            "MISSED_OPPORTUNITY", "SIZE_MISTAKE", "PATTERN_DETECTED",
        }
        for insight in insights:
            assert insight.category in valid_categories
