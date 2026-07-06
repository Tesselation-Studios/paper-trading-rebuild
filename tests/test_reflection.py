"""Tests for per-tick trader reflection mechanism.

REF: spec-v3 §4.4 — Learning Loop Closure
After each LLM decision, ask 'what did I learn?' and feed insight into next tick.
"""

from __future__ import annotations

import pytest
from datetime import datetime
from typing import Any, List, Optional, Tuple

from src.reflection import Reflection, reflect_on_decision, format_reflections_for_prompt
from src.replay import Tick, TraderDecision
from src.signals import SignalReport


# ── Mock LLM Engine ───────────────────────────────────────────────────────────


class MockLLMEngine:
    """Mock LLM engine that returns pre-programmed reflection responses."""

    def __init__(self, responses: Optional[List[Tuple[str, str]]] = None):
        self.responses = responses or [("I learned something", "I would do X differently")]
        self.call_count = 0
        self.reflect_calls: List[dict] = []

    def reflect(
        self,
        tick: Tick,
        decision: TraderDecision,
        signal: Any,
        prev_reflections: Optional[List[Reflection]] = None,
    ) -> Tuple[str, str]:
        self.reflect_calls.append({
            "tick": tick,
            "decision": decision,
            "signal": signal,
            "prev_reflections": prev_reflections,
        })
        if not self.responses:
            return ("No learning available", "No changes suggested")
        resp = self.responses[self.call_count % len(self.responses)]
        self.call_count += 1
        return resp


def make_tick(minute: int = 0, ticker: str = "AAPL", price: float = 150.0) -> Tick:
    """Helper: create a Tick for testing."""
    return Tick(
        timestamp=datetime(2024, 1, 5, 9, 30 + minute if minute < 30 else 9 + minute // 60, minute % 60),
        ticker=ticker,
        open=price, high=price + 0.5, low=price - 0.5, close=price,
        volume=1_000_000,
    )


def make_decision(decision: str = "HOLD", conviction: float = 0.5, rationale: str = "test") -> TraderDecision:
    """Helper: create a TraderDecision for testing."""
    return TraderDecision(
        ticker="AAPL",
        decision=decision,
        conviction=conviction,
        rationale=rationale,
    )


# ── Tests: format_reflections_for_prompt ──────────────────────────────────────


def test_format_reflections_max_3():
    """When given 5 reflections, only the last 3 are included in the output."""
    reflections = [
        Reflection(
            timestamp="2024-01-05T09:30", ticker="AAPL",
            decision="BUY", rationale="momentum signal",
            learning="learn 1: momentum works", would_do_differently="wait longer",
        ),
        Reflection(
            timestamp="2024-01-05T09:35", ticker="AAPL",
            decision="HOLD", rationale="waiting",
            learning="learn 2: patience pays", would_do_differently="none",
        ),
        Reflection(
            timestamp="2024-01-05T09:40", ticker="AAPL",
            decision="SELL", rationale="peak detected",
            learning="learn 3: sold too early", would_do_differently="hold 2 more ticks",
        ),
        Reflection(
            timestamp="2024-01-05T09:45", ticker="AAPL",
            decision="BUY", rationale="dip buy",
            learning="learn 4: dip was real", would_do_differently="size up",
        ),
        Reflection(
            timestamp="2024-01-05T09:50", ticker="AAPL",
            decision="HOLD", rationale="sideways",
            learning="learn 5: sideways is common", would_do_differently="be patient",
        ),
    ]

    result = format_reflections_for_prompt(reflections, max_count=3)

    # Last 3 present
    assert "learn 3" in result
    assert "learn 4" in result
    assert "learn 5" in result
    # First 2 excluded
    assert "learn 1" not in result
    assert "learn 2" not in result

    # Structural checks
    assert "Reflection" in result or "reflection" in result.lower()
    assert "SELL" in result


def test_format_reflections_empty_list():
    """An empty reflections list returns an empty string."""
    result = format_reflections_for_prompt([], max_count=3)
    assert result == ""


def test_format_reflections_none():
    """None input returns empty string."""
    result = format_reflections_for_prompt(None, max_count=3)
    assert result == ""


def test_format_reflections_fewer_than_max():
    """When we have fewer reflections than max_count, all are included."""
    reflections = [
        Reflection(
            timestamp="2024-01-05T09:30", ticker="AAPL",
            decision="BUY", rationale="momentum",
            learning="only reflection", would_do_differently="check volume",
        ),
    ]
    result = format_reflections_for_prompt(reflections, max_count=3)
    assert "only reflection" in result
    assert "check volume" in result


def test_format_reflections_default_max():
    """Default max_count is 3."""
    reflections = [
        Reflection(
            timestamp=f"2024-01-05T09:{30 + i * 5:02d}", ticker="AAPL",
            decision="HOLD", rationale="...",
            learning=f"learn {i+1}", would_do_differently=f"diff {i+1}",
        )
        for i in range(5)
    ]
    result = format_reflections_for_prompt(reflections)
    assert "learn 1" not in result
    assert "learn 2" not in result
    assert "learn 3" in result
    assert "learn 4" in result
    assert "learn 5" in result


# ── Tests: Reflection dataclass ────────────────────────────────────────────────


def test_reflection_dataclass_fields():
    """Reflection dataclass has all required fields."""
    r = Reflection(
        timestamp="2024-01-05T09:30:00",
        ticker="SPY",
        decision="BUY",
        rationale="Strong uptrend, RSI 32 oversold bounce",
        learning="RSI bounces from 30 in uptrends are 80% reliable within 5 bars",
        would_do_differently="Scale in over 3 bars instead of all at once",
    )
    assert r.timestamp == "2024-01-05T09:30:00"
    assert r.ticker == "SPY"
    assert r.decision == "BUY"
    assert r.rationale == "Strong uptrend, RSI 32 oversold bounce"
    assert "80% reliable" in r.learning
    assert "Scale in" in r.would_do_differently


def test_reflection_equality():
    """Two reflections with the same data are equal."""
    r1 = Reflection("t1", "AAPL", "BUY", "r", "l", "w")
    r2 = Reflection("t1", "AAPL", "BUY", "r", "l", "w")
    assert r1 == r2


def test_reflection_inequality():
    """Reflections with different data are not equal."""
    r1 = Reflection("t1", "AAPL", "BUY", "r", "l", "w")
    r2 = Reflection("t2", "AAPL", "BUY", "r", "l", "w")
    assert r1 != r2


# ── Tests: reflect_on_decision ────────────────────────────────────────────────


def test_reflect_on_decision_returns_reflection():
    """reflect_on_decision returns a properly populated Reflection."""
    mock = MockLLMEngine(responses=[("learned: momentum>0.5 is actionable", "increase size on >0.7")])

    tick = make_tick(minute=5, price=151.0)
    decision = make_decision(decision="BUY", conviction=0.75, rationale="momentum signal 0.65")
    signal = SignalReport(
        ticker="AAPL", timestamp=tick.timestamp,
        momentum_score=0.65, momentum_signal="BULLISH",
        rsi=45.0, rsi_signal="NEUTRAL",
        volatility=0.22, volatility_regime="NORMAL",
        regime="TRENDING_UP", regime_confidence=0.8, regime_weight=1.0,
        recommended_size_pct=0.12, max_positions=5,
        stop_loss=143.45, take_profit=173.65,
        composite_signal=0.52, conviction=0.65,
    )

    reflection = reflect_on_decision(tick, decision, signal, mock)

    assert isinstance(reflection, Reflection)
    assert reflection.ticker == "AAPL"
    assert reflection.decision == "BUY"
    assert reflection.rationale == "momentum signal 0.65"
    assert "momentum>0.5" in reflection.learning
    assert "increase size" in reflection.would_do_differently
    assert reflection.timestamp is not None

    # Verify engine was called correctly
    assert len(mock.reflect_calls) == 1
    assert mock.reflect_calls[0]["tick"] is tick
    assert mock.reflect_calls[0]["decision"] is decision


def test_reflect_on_decision_passes_prev_reflections():
    """reflect_on_decision forwards prev_reflections to the engine."""
    mock = MockLLMEngine(responses=[("ok", "ok")])

    tick = make_tick()
    decision = make_decision()
    prev = [
        Reflection("t0", "AAPL", "BUY", "r0", "l0", "w0"),
    ]

    reflect_on_decision(tick, decision, None, mock, prev)

    assert mock.reflect_calls[0]["prev_reflections"] == prev


def test_reflect_on_decision_default_prev_reflections():
    """When prev_reflections is omitted, engine receives None."""
    mock = MockLLMEngine(responses=[("ok", "ok")])

    tick = make_tick()
    decision = make_decision()

    reflect_on_decision(tick, decision, None, mock)

    assert mock.reflect_calls[0]["prev_reflections"] is None


def test_reflect_on_decision_handle_none_signal():
    """reflect_on_decision works with signal=None (graceful)."""
    mock = MockLLMEngine(responses=[("learned something", "different approach")])

    tick = make_tick()
    decision = make_decision()

    reflection = reflect_on_decision(tick, decision, None, mock)

    assert reflection.learning == "learned something"
    assert reflection.would_do_differently == "different approach"


# ── Tests: Integration (5-tick simulation) ────────────────────────────────────


def test_integration_5_ticks_produces_5_reflections():
    """A 5-tick simulation produces exactly 5 reflections."""
    responses = [
        ("Tick 1: momentum signals are noisy early in session", "Wait for confirmation"),
        ("Tick 2: confirmation came on tick 2, learned to wait", "Build position gradually"),
        ("Tick 3: gradual position building works", "Add more on strength"),
        ("Tick 4: overconfidence was a mistake, missed the peak", "Take profit at +2%"),
        ("Tick 5: learned from tick 4 — selling now", "Stick to the 2% rule"),
    ]
    mock = MockLLMEngine(responses=responses)

    reflections: List[Reflection] = []
    for i in range(5):
        tick = make_tick(minute=i * 5, price=150.0 + i * 0.5)
        decision = make_decision(
            decision="BUY" if i < 3 else ("SELL" if i == 3 else "HOLD"),
            conviction=0.5 + i * 0.1,
            rationale=f"Decision {i+1}",
        )
        prev = reflections[-3:] if reflections else []
        reflection = reflect_on_decision(tick, decision, None, mock, prev)
        reflections.append(reflection)

    assert len(reflections) == 5
    assert all(isinstance(r, Reflection) for r in reflections)

    # Later reflections should reference learning progression
    # (these are mock responses, so verify the mock delivered the right ones)
    assert "learned from tick 4" in reflections[4].learning
    assert "gradual position building works" in reflections[2].learning


def test_integration_reflections_feed_into_prompt():
    """After 5 ticks, format the last 3 and verify they appear in prompt context."""
    responses = [
        ("learn: A", "diff: A"),
        ("learn: B", "diff: B"),
        ("learn: C", "diff: C"),
        ("learn: D", "diff: D"),
        ("learn: E", "diff: E"),
    ]
    mock = MockLLMEngine(responses=responses)

    reflections = []
    for i in range(5):
        tick = make_tick(minute=i * 5)
        decision = make_decision(rationale=f"tick {i}")
        prev = reflections[-3:] if reflections else []
        reflection = reflect_on_decision(tick, decision, None, mock, prev)
        reflections.append(reflection)

    # Format last 3 for prompt
    prompt_context = format_reflections_for_prompt(reflections, max_count=3)

    # Verify only last 3 are present
    assert "learn: A" not in prompt_context
    assert "learn: B" not in prompt_context
    assert "learn: C" in prompt_context
    assert "learn: D" in prompt_context
    assert "learn: E" in prompt_context


def test_integration_llm_failure_doesnt_block():
    """If the reflection engine raises, reflect_on_decision should propagate
    the error so the caller can catch it (simulator wraps in try/except).
    """
    class FailingEngine:
        def reflect(self, tick, decision, signal, prev_reflections=None):
            raise RuntimeError("API timeout")

    tick = make_tick()
    decision = make_decision()

    with pytest.raises(RuntimeError, match="API timeout"):
        reflect_on_decision(tick, decision, None, FailingEngine())


# ── Tests: Edge cases ──────────────────────────────────────────────────────────


def test_format_reflections_custom_max_count():
    """max_count can be customized."""
    reflections = [
        Reflection(f"t{i}", "AAPL", "HOLD", "r", f"l{i}", f"w{i}")
        for i in range(10)
    ]
    result = format_reflections_for_prompt(reflections, max_count=5)
    assert "l5" in result
    assert "l4" not in result  # only 5-9

    result2 = format_reflections_for_prompt(reflections, max_count=1)
    assert "l9" in result2
    assert "l8" not in result2
