"""Journal analysis + counterfactual loop — SPEC-v3 §4.4 Task 3.

After a sweep completes, analyze journals for patterns:
  - High-conviction losses: decisions with conviction > 0.5 that lost money
  - Regime weaknesses: which regime has worst win rate?
  - Missed opportunities: ticks with strong signals where HOLD was chosen
  - Size mistakes: positions that were too large for the drawdown

Output: List of JournalInsight objects with concrete suggestions.

Usage:
    from src.journal_analyzer import JournalAnalyzer, analyze_journal

    analyzer = JournalAnalyzer()
    insights = analyzer.analyze(journal=journal, reflections=reflections, trades=trades)
    # Or with LLM enhancement:
    analyzer = JournalAnalyzer(llm_engine=engine)
    insights = analyzer.analyze(...)
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("journal_analyzer")


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class JournalInsight:
    """A single insight extracted from journal analysis.

    These feed into the nightly synthesis pipeline and can trigger
    prompt/param changes when confidence exceeds thresholds.
    """

    category: str  # "HIGH_CONVICTION_LOSS", "REGIME_WEAKNESS", etc.
    description: str  # Human-readable finding
    suggestion: str  # Concrete change to make
    confidence: float  # 0.0-1.0
    evidence: List[str] = field(default_factory=list)
    night: int = 0  # Nights this insight has persisted (for auto-promotion)
    source: str = "heuristic"  # "heuristic" or "llm"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "description": self.description,
            "suggestion": self.suggestion,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "night": self.night,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "JournalInsight":
        return cls(
            category=d.get("category", ""),
            description=d.get("description", ""),
            suggestion=d.get("suggestion", ""),
            confidence=float(d.get("confidence", 0.0)),
            evidence=list(d.get("evidence", [])),
            night=int(d.get("night", 0)),
            source=d.get("source", "heuristic"),
        )


# ── Detection functions ───────────────────────────────────────────────────────


def detect_high_conviction_losses(
    reflections: List[Any],
    trades: List[Dict[str, Any]],
    conviction_threshold: float = 0.5,
) -> List[JournalInsight]:
    """Detect trades with high conviction that resulted in losses.

    Args:
        reflections: List of Reflection objects.
        trades: List of trade dicts with 'pnl', 'conviction', 'ticker'.
        conviction_threshold: Minimum conviction to flag.

    Returns:
        List of JournalInsight objects.
    """
    insights: List[JournalInsight] = []

    # Find high-conviction losing trades
    losing_high_conv = []
    for i, trade in enumerate(trades):
        pnl = trade.get("pnl", 0)
        conv = trade.get("conviction", 0)
        ticker = trade.get("ticker", "?")

        if pnl < 0 and conv >= conviction_threshold:
            # Match with reflection if available
            reflection_text = ""
            if i < len(reflections):
                try:
                    r = reflections[i]
                    reflection_text = f"Reflection: {r.learning}"
                except (IndexError, AttributeError):
                    pass

            losing_high_conv.append({
                "ticker": ticker,
                "pnl": pnl,
                "conviction": conv,
                "regime": trade.get("regime", "unknown"),
                "reflection": reflection_text,
            })

    if losing_high_conv:
        total_loss = sum(t["pnl"] for t in losing_high_conv)
        avg_conv = sum(t["conviction"] for t in losing_high_conv) / len(losing_high_conv)
        tickers = list({t["ticker"] for t in losing_high_conv})

        # Build evidence from the losing trades
        evidence = [
            f"{t['ticker']}: lost ${abs(t['pnl']):,.0f} at conviction {t['conviction']:.2f} "
            f"(regime: {t['regime']})"
            for t in losing_high_conv
        ]

        # Confidence scales with number of losses and average conviction
        confidence = min(0.95, 0.5 + (len(losing_high_conv) * 0.15) + (avg_conv - 0.5) * 0.3)

        insights.append(JournalInsight(
            category="HIGH_CONVICTION_LOSS",
            description=(
                f"Found {len(losing_high_conv)} high-conviction losing trade(s) "
                f"on {', '.join(tickers)} totaling ${total_loss:,.0f}. "
                f"Average conviction was {avg_conv:.2f}."
            ),
            suggestion=(
                "Consider reducing conviction multiplier or adding a regime filter "
                "when trading at high conviction in losing regimes."
            ),
            confidence=round(confidence, 4),
            evidence=evidence,
        ))

    return insights


def compute_regime_stats(
    trades: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Compute per-regime statistics for trade performance.

    Args:
        trades: List of trade dicts with 'pnl', 'regime'.

    Returns:
        Dict mapping regime → {count, total_pnl, win_count, win_rate, avg_pnl}.
    """
    stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0, "total_pnl": 0.0, "win_count": 0,
        "win_rate": 0.0, "avg_pnl": 0.0,
    })

    for trade in trades:
        regime = trade.get("regime", "unknown")
        pnl = trade.get("pnl", 0.0)
        stats[regime]["count"] += 1
        stats[regime]["total_pnl"] += pnl
        if pnl > 0:
            stats[regime]["win_count"] += 1

    # Compute derived stats
    for regime, s in stats.items():
        if s["count"] > 0:
            s["win_rate"] = s["win_count"] / s["count"]
            s["avg_pnl"] = s["total_pnl"] / s["count"]

    return dict(stats)


def detect_regime_weaknesses(
    trades: List[Dict[str, Any]],
    min_trades_per_regime: int = 2,
) -> List[JournalInsight]:
    """Detect regimes where the trader consistently loses money.

    Args:
        trades: List of trade dicts with 'pnl', 'regime'.
        min_trades_per_regime: Minimum trades to consider a regime significant.

    Returns:
        List of JournalInsight objects.
    """
    insights: List[JournalInsight] = []

    stats = compute_regime_stats(trades)

    if not stats:
        return insights

    # Find the worst regime by win rate (among regimes with enough trades)
    worst_regime = None
    worst_win_rate = 1.0
    worst_pnl = 0.0
    worst_count = 0

    for regime, s in stats.items():
        if s["count"] >= min_trades_per_regime:
            if s["win_rate"] < worst_win_rate:
                worst_win_rate = s["win_rate"]
                worst_regime = regime
                worst_pnl = s["total_pnl"]
                worst_count = s["count"]

    if worst_regime is not None and worst_win_rate < 0.4:
        confidence = min(0.95, 0.5 + (1.0 - worst_win_rate) * 0.8)
        regime_weight = worst_count / sum(s["count"] for s in stats.values())

        insights.append(JournalInsight(
            category="REGIME_WEAKNESS",
            description=(
                f"'{worst_regime}' regime has win rate of {worst_win_rate:.0%} "
                f"({worst_count} trades, total P&L: ${worst_pnl:,.0f}). "
                f"This represents {regime_weight:.0%} of all trades."
            ),
            suggestion=(
                f"Reduce position size or skip trades entirely in "
                f"{worst_regime} regime. Consider setting "
                f"weight_{worst_regime.lower()} to 0.0 "
                f"to deactivate trading in this regime."
            ),
            confidence=round(confidence, 4),
            evidence=[
                f"Regime {worst_regime}: {worst_count} trades, "
                f"win rate {worst_win_rate:.0%}, total P&L ${worst_pnl:,.0f}"
            ],
        ))

    # Also check if there are regimes with very few trades (underexplored)
    for regime, s in stats.items():
        if s["count"] < min_trades_per_regime and len(stats) > 1:
            insights.append(JournalInsight(
                category="REGIME_WEAKNESS",
                description=(
                    f"'{regime}' regime has only {s['count']} trade(s) — "
                    f"insufficient data to evaluate performance."
                ),
                suggestion=(
                    f"Increase weight_{regime.lower()} or reduce thresholds "
                    f"to generate more trades in {regime} for evaluation."
                ),
                confidence=0.3,
                evidence=[
                    f"Regime {regime}: {s['count']} trades, "
                    f"total P&L ${s['total_pnl']:,.0f}"
                ],
            ))

    return insights


def detect_missed_opportunities(
    reflections: List[Any],
    journal: List[str],
) -> List[JournalInsight]:
    """Detect ticks where strong signals were present but HOLD was chosen.

    Looks for patterns in journal entries and reflections where:
    - A HOLD decision was made with low conviction
    - The reflection mentions missing out or not acting
    - Subsequent journal entries show price moved favorably

    Args:
        reflections: List of Reflection objects.
        journal: List of journal entry strings.

    Returns:
        List of JournalInsight objects.
    """
    insights: List[JournalInsight] = []
    missed_count = 0
    evidence = []

    # Check reflections for missed opportunity language
    missed_keywords = [
        "missed", "should have bought", "should have sold",
        "could have", "would have", "didn't act", "hesitated",
        "not enough conviction", "missed out", "left money",
        "went up", "surged", "rallied", "despite",
        "could've", "would've", "wish", "regret",
    ]

    hold_reflections = []
    for i, r in enumerate(reflections):
        try:
            decision = getattr(r, "decision", "")
            learning = getattr(r, "learning", "").lower()
            would_do = getattr(r, "would_do_differently", "").lower()
            combined_text = f"{learning} {would_do}"
        except AttributeError:
            continue

        if decision == "HOLD":
            for kw in missed_keywords:
                if kw in combined_text:
                    hold_reflections.append((i, r, kw))
                    missed_count += 1
                    try:
                        evidence.append(
                            f"HOLD on {r.ticker}: '{r.learning[:100]}' — {r.would_do_differently[:100]}"
                        )
                    except AttributeError:
                        evidence.append(f"HOLD reflection with '{kw}'")
                    break

    if missed_count >= 1:
        confidence = min(0.9, 0.4 + missed_count * 0.15)

        # Look for price moves in journal following HOLD decisions
        suggestion = (
            "Lower the conviction threshold for entering trades, or add a "
            "'trend confirmation' rule: if momentum > 0.5 and price is moving "
            "in signal direction for 2+ consecutive ticks, override HOLD."
        )

        insights.append(JournalInsight(
            category="MISSED_OPPORTUNITY",
            description=(
                f"Detected {missed_count} missed opportunity reflection(s) "
                f"where HOLD was chosen on potentially profitable signals."
            ),
            suggestion=suggestion,
            confidence=round(confidence, 4),
            evidence=evidence,
        ))

    return insights


def detect_size_mistakes(
    trades: List[Dict[str, Any]],
    max_position_pct: float = 0.20,
) -> List[JournalInsight]:
    """Detect positions that were oversized relative to portfolio.

    Args:
        trades: List of trade dicts with 'pnl', 'shares', 'position_pct'.
        max_position_pct: Maximum acceptable position size as fraction.

    Returns:
        List of JournalInsight objects.
    """
    insights: List[JournalInsight] = []

    oversized = []
    for trade in trades:
        pos_pct = trade.get("position_pct", 0)
        if pos_pct > max_position_pct:
            oversized.append(trade)

    if oversized:
        total_loss = sum(t.get("pnl", 0) for t in oversized)
        avg_size = sum(t.get("position_pct", 0) for t in oversized) / len(oversized)
        tickers = list({t.get("ticker", "?") for t in oversized})

        evidence = [
            f"{t.get('ticker', '?')}: {t.get('position_pct', 0):.0%} of portfolio, "
            f"P&L: ${t.get('pnl', 0):,.0f}"
            for t in oversized
        ]

        confidence = min(0.9, 0.5 + len(oversized) * 0.15)

        insights.append(JournalInsight(
            category="SIZE_MISTAKE",
            description=(
                f"Found {len(oversized)} oversized position(s) on {', '.join(tickers)} "
                f"(avg {avg_size:.0%} of portfolio vs max {max_position_pct:.0%}). "
                f"Total P&L impact: ${total_loss:,.0f}."
            ),
            suggestion=(
                f"Enforce max position size of {max_position_pct:.0%}. "
                f"Reduce base_size_pct to keep positions within limits."
            ),
            confidence=round(confidence, 4),
            evidence=evidence,
        ))

    return insights


def analyze_reflections(
    reflections: List[Any],
    min_pattern_occurrences: int = 2,
) -> List[JournalInsight]:
    """Analyze reflection text for recurring patterns.

    Args:
        reflections: List of Reflection objects.
        min_pattern_occurrences: Minimum times a pattern must appear.

    Returns:
        List of JournalInsight objects.
    """
    insights: List[JournalInsight] = []

    if len(reflections) < min_pattern_occurrences:
        return insights

    # Collect all learning and would_do_differently text
    learning_texts = []
    for r in reflections:
        try:
            learning_texts.append((r.learning or "").lower())
            learning_texts.append((r.would_do_differently or "").lower())
        except AttributeError:
            continue

    # Pattern keywords to check
    pattern_keywords = {
        "early": ("entering too early", "Add confirmation requirement before entry"),
        "late": ("entering too late", "Speed up signal evaluation"),
        "overtrading": ("trading too frequently", "Increase conviction threshold"),
        "hesita": ("hesitating on signals", "Lower conviction threshold"),
        "chasing": ("chasing price", "Wait for pullback, don't chase"),
        "reversal": ("failing to recognize reversals", "Add reversal detection filter"),
        "size": ("position sizing issues", "Adjust base_size_pct"),
        "noise": ("trading on noise", "Increase signal smoothing lookback"),
    }

    for keyword, (description_template, suggestion) in pattern_keywords.items():
        count = sum(1 for t in learning_texts if keyword in t)
        if count >= min_pattern_occurrences:
            frequency = count / len(reflections)
            confidence = min(0.85, 0.4 + frequency * 0.6)

            insights.append(JournalInsight(
                category="PATTERN_DETECTED",
                description=(
                    f"Reflection pattern detected: '{keyword}' appears in "
                    f"{count}/{len(reflections)} reflections ({frequency:.0%}). "
                    f"{description_template}."
                ),
                suggestion=suggestion,
                confidence=round(confidence, 4),
                evidence=[
                    f"Reflection {i}: ...{t[:80]}..."
                    for i, t in enumerate(learning_texts)
                    if keyword in t
                ][:5],
            ))

    return insights


# ── LLM Enhancement ───────────────────────────────────────────────────────────


def _build_llm_analysis_prompt(
    insights: List[JournalInsight],
    journal: List[str],
    trades: List[Dict[str, Any]],
) -> str:
    """Build a prompt for LLM-enhanced journal analysis.

    Args:
        insights: Heuristic insights already detected.
        journal: Journal entries.
        trades: Trade data.

    Returns:
        Prompt string for the LLM.
    """
    # Summarize existing insights
    insight_summary = "\n".join(
        f"- [{i.category}] {i.description[:150]}"
        for i in insights[:10]
    ) if insights else "(no heuristic insights found)"

    trade_summary = "\n".join(
        f"- {t.get('ticker', '?')}: P&L=${t.get('pnl', 0):,.0f}, "
        f"regime={t.get('regime', '?')}, conv={t.get('conviction', 0):.2f}"
        for t in trades[:20]
    ) if trades else "(no trades)"

    journal_sample = "\n".join(journal[-15:]) if journal else "(no journal entries)"

    return (
        "You are a trading performance analyst. Review this paper trading journal "
        "and identify patterns that the heuristic detectors may have missed.\n\n"
        f"## Heuristic Insights (already detected)\n{insight_summary}\n\n"
        f"## Recent Journal\n{journal_sample}\n\n"
        f"## Trade Summary\n{trade_summary}\n\n"
        "Identify 0-3 additional insights the heuristics missed. Consider:\n"
        "- Behavioral biases (anchoring, loss aversion, overconfidence)\n"
        "- Market condition interactions (e.g., news + regime effects)\n"
        "- Time-of-day patterns\n"
        "- Signal quality degradation over the simulation\n\n"
        "Respond with JSON array:\n"
        '[{"category": "BEHAVIORAL"|"MARKET_INTERACTION"|"TIME_PATTERN"|"SIGNAL_QUALITY", '
        '"description": "...", "suggestion": "...", "confidence": 0.0-1.0, '
        '"evidence": ["..."]}]'
    )


def _parse_llm_insights(content: Optional[str]) -> List[JournalInsight]:
    """Parse LLM response into JournalInsight objects.

    Args:
        content: Raw LLM response string.

    Returns:
        List of JournalInsight objects.
    """
    if not content:
        return []

    # Strip markdown fences
    content = re.sub(r'^```(?:json)?\s*', '', content.strip())
    content = re.sub(r'\s*```$', '', content)

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Try to extract JSON array
        match = re.search(r'\[.*\]', content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        else:
            return []

    if not isinstance(data, list):
        return []

    insights = []
    for item in data:
        if not isinstance(item, dict):
            continue
        insights.append(JournalInsight(
            category=item.get("category", "LLM_INSIGHT"),
            description=item.get("description", ""),
            suggestion=item.get("suggestion", ""),
            confidence=float(item.get("confidence", 0.5)),
            evidence=list(item.get("evidence", [])),
            source="llm",
        ))

    return insights


# ── Main Analyzer ─────────────────────────────────────────────────────────────


class JournalAnalyzer:
    """Analyzes trading journals for patterns and generates improvement insights.

    Uses heuristic detectors for fast, deterministic analysis. Optionally
    enhances results with LLM-powered analysis for deeper pattern detection.

    Args:
        llm_engine: Optional LLMEngine for enhanced analysis.
        conviction_threshold: Minimum conviction to flag a loss.
        min_trades_per_regime: Minimum trades before analyzing regime.
        max_position_pct: Maximum acceptable position size.
    """

    def __init__(
        self,
        llm_engine: Optional[Any] = None,
        conviction_threshold: float = 0.5,
        min_trades_per_regime: int = 2,
        max_position_pct: float = 0.20,
    ):
        self.llm_engine = llm_engine
        self.conviction_threshold = conviction_threshold
        self.min_trades_per_regime = min_trades_per_regime
        self.max_position_pct = max_position_pct

    def analyze(
        self,
        journal: List[str],
        reflections: List[Any],
        trades: List[Dict[str, Any]],
        use_llm: bool = False,
    ) -> List[JournalInsight]:
        """Analyze journal data and generate insights.

        Args:
            journal: List of journal entry strings.
            reflections: List of Reflection objects.
            trades: List of trade dicts with 'pnl', 'ticker', 'regime', etc.
            use_llm: Whether to use LLM enhancement (requires llm_engine).

        Returns:
            List of JournalInsight objects, sorted by confidence descending.
        """
        if not journal and not reflections and not trades:
            return []

        insights: List[JournalInsight] = []

        # Run heuristic detectors
        insights.extend(detect_high_conviction_losses(
            reflections, trades, self.conviction_threshold,
        ))
        insights.extend(detect_regime_weaknesses(
            trades, self.min_trades_per_regime,
        ))
        insights.extend(detect_missed_opportunities(
            reflections, journal,
        ))
        insights.extend(detect_size_mistakes(
            trades, self.max_position_pct,
        ))
        insights.extend(analyze_reflections(reflections))

        # Optional LLM enhancement
        if use_llm and self.llm_engine:
            try:
                prompt = _build_llm_analysis_prompt(insights, journal, trades)
                response = self.llm_engine._call_api(prompt)
                llm_insights = _parse_llm_insights(response)
                insights.extend(llm_insights)
                log.info("LLM enhancement added %d insights", len(llm_insights))
            except Exception as e:
                log.warning("LLM enhancement failed: %s — using heuristics only", e)

        # Sort by confidence descending
        insights.sort(key=lambda i: i.confidence, reverse=True)

        return insights


# ── Convenience function ──────────────────────────────────────────────────────


def analyze_journal(
    journal: List[str],
    reflections: List[Any],
    trades: List[Dict[str, Any]],
    llm_engine: Optional[Any] = None,
    **kwargs: Any,
) -> List[JournalInsight]:
    """One-line journal analysis.

    Args:
        journal: List of journal entry strings.
        reflections: List of Reflection objects.
        trades: List of trade dicts with 'pnl', 'ticker', 'regime'.
        llm_engine: Optional LLMEngine for enhanced analysis.
        **kwargs: Passed to JournalAnalyzer constructor.

    Returns:
        List of JournalInsight objects, sorted by confidence.
    """
    analyzer = JournalAnalyzer(llm_engine=llm_engine, **kwargs)
    return analyzer.analyze(
        journal=journal,
        reflections=reflections,
        trades=trades,
        use_llm=llm_engine is not None,
    )
