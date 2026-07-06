"""Per-tick trader reflection — 'what did I learn?' after each LLM decision.

After each tick, the simulator calls reflect_on_decision() which asks the LLM
(using the cheapest available model) to reflect on its trading decision. The
insight is fed into the prompt context for the NEXT tick, creating a learning
loop that compounds across the simulation.

Design constraints:
  - Uses cheapest model (v4-flash) to avoid doubling simulation cost
  - Only last 3 reflections in prompt context (avoid prompt bloat)
  - If reflection call fails, the caller should log warning and continue
  - Reflections are stored alongside journal entries for later analysis

Usage:
    from src.reflection import Reflection, reflect_on_decision, format_reflections_for_prompt

    engine = LLMEngine(model="deepseek/deepseek-v4-flash")
    reflection = reflect_on_decision(tick, decision, signal, engine)
    prompt_context = format_reflections_for_prompt(all_reflections, max_count=3)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional, Tuple

log = logging.getLogger("reflection")


# ── Data type ─────────────────────────────────────────────────────────────────


@dataclass
class Reflection:
    """A single per-tick reflection: what the trader learned from this decision.

    Stored in the journal and used as context for future ticks.
    """
    timestamp: str
    ticker: str
    decision: str          # BUY, SELL, or HOLD
    rationale: str          # Original decision rationale
    learning: str           # LLM-generated: "what I learned"
    would_do_differently: str  # LLM-generated: specific change for next time


# ── Core functions ────────────────────────────────────────────────────────────


def reflect_on_decision(
    tick: Any,           # Tick from src.replay
    decision: Any,       # TraderDecision from src.replay
    signal: Any,         # SignalReport from src.signals
    llm_engine: Any,     # LLMEngine from src.llm_engine
    prev_reflections: Optional[List[Reflection]] = None,
) -> Reflection:
    """Ask the LLM 'what did you learn from this decision?'

    Uses the LLMEngine passed by the caller (typically configured with the
    cheapest model, e.g. deepseek/deepseek-v4-flash).

    Args:
        tick: Current market tick.
        decision: The trader's decision at this tick.
        signal: Signal engine output for this tick (may be None).
        llm_engine: LLMEngine instance with .reflect() method.
        prev_reflections: Previous reflections for context (may be None).

    Returns:
        Reflection with learning and would_do_differently fields populated.

    Raises:
        Propagates any exception from llm_engine.reflect() — caller should
        catch and handle (log + continue).
    """
    timestamp = (
        tick.timestamp.isoformat() if hasattr(tick.timestamp, "isoformat")
        else str(tick.timestamp)
        if hasattr(tick, "timestamp")
        else datetime.now().isoformat()
    )

    learning, would_do = llm_engine.reflect(
        tick=tick,
        decision=decision,
        signal=signal,
        prev_reflections=prev_reflections,
    )

    return Reflection(
        timestamp=timestamp,
        ticker=decision.ticker if hasattr(decision, "ticker") else tick.ticker,
        decision=decision.decision if hasattr(decision, "decision") else str(decision),
        rationale=decision.rationale if hasattr(decision, "rationale") else "",
        learning=learning,
        would_do_differently=would_do,
    )


def format_reflections_for_prompt(
    reflections: Optional[List[Reflection]],
    max_count: int = 3,
) -> str:
    """Format the last N reflections as prompt context for the next tick.

    Args:
        reflections: List of Reflection objects (newest last). None is treated
                     as empty.
        max_count: Maximum number of reflections to include (default 3).

    Returns:
        Formatted string suitable for inclusion in the LLM prompt,
        or empty string if no reflections are available.
    """
    if not reflections:
        return ""

    # Take last N only
    recent = reflections[-max_count:] if len(reflections) > max_count else reflections

    lines = ["## Recent Reflections (what you learned from recent decisions)"]
    for i, r in enumerate(recent):
        ts = r.timestamp[:16] if len(r.timestamp) >= 16 else r.timestamp
        lines.append(
            f"{i + 1}. [{ts}] {r.decision} {r.ticker}: {r.rationale}\n"
            f"   Learned: {r.learning}\n"
            f"   Would do differently: {r.would_do_differently}"
        )

    return "\n".join(lines)
