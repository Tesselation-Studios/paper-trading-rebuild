"""Nightly synthesis + auto-promotion — SPEC-v3 §4.4 Task 4.

Every night, aggregates all journal insights from the day's sweeps into a summary.
Ranks suggestions by confidence. Auto-promotes changes that meet thresholds.

Promotion thresholds:
  - Confidence > 0.75 AND sustained 3+ nights → AUTO-PROMOTE to trader config
  - Confidence > 0.5 AND sustained 2 nights → Create PR for review
  - Below threshold → Log for next night's analysis

Usage:
    from src.synthesis import Synthesizer, synthesize_nightly

    synthesizer = Synthesizer()
    summary = synthesizer.synthesize(
        trader_insights={"kairos": kairos_insights, "aldridge": aldridge_insights},
        scenarios={"kairos": {...}, "aldridge": {...}},
    )
    print(summary.format())
"""

from __future__ import annotations

import logging
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.journal_analyzer import JournalInsight

log = logging.getLogger("synthesis")


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class TraderSynthesis:
    """Synthesized insights for a single trader."""

    trader: str
    n_scenarios: int = 0
    n_trades: int = 0
    best_score: float = 0.0
    top_variant: str = ""
    insights: List[JournalInsight] = field(default_factory=list)

    @property
    def top_insight(self) -> Optional[JournalInsight]:
        """Highest-confidence insight, or None."""
        return self.insights[0] if self.insights else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trader": self.trader,
            "n_scenarios": self.n_scenarios,
            "n_trades": self.n_trades,
            "best_score": self.best_score,
            "top_variant": self.top_variant,
            "insights": [i.to_dict() for i in self.insights],
        }


@dataclass
class NightlySummary:
    """Complete nightly synthesis across all traders.

    Produced by Synthesizer.synthesize() — feeds into the night pipeline
    and generates the markdown report delivered to the user.
    """

    date: str = ""
    n_traders: int = 0
    trader_syntheses: Dict[str, TraderSynthesis] = field(default_factory=dict)
    top_insights: List[JournalInsight] = field(default_factory=list)
    promotions: List[Dict[str, Any]] = field(default_factory=list)
    n_auto_promoted: int = 0
    n_pr_ready: int = 0
    n_validation: int = 0

    def format(self) -> str:
        """Produce the markdown-formatted nightly summary."""
        date = self.date or datetime.now().strftime("%Y-%m-%d")
        lines = [
            f"=== Nightly Learning Summary: {date} ===",
            "",
        ]

        for trader_name, synth in sorted(self.trader_syntheses.items()):
            trader_display = trader_name.capitalize()
            lines.append(
                f"## {trader_display} — {synth.n_scenarios} scenarios, "
                f"{synth.n_trades} trades (best score: {synth.best_score:.2f})"
            )

            if not synth.insights:
                if synth.n_trades == 0:
                    lines.append("  No trades — thresholds may be too conservative.")
                else:
                    lines.append("  No significant insights detected.")
                lines.append("")
                continue

            for insight in synth.insights:
                promotion = evaluate_promotion(insight)
                action_label = _action_label(promotion["action"])

                lines.append(f"  Learned: \"{insight.description}\"")
                lines.append(f"  Suggestion: \"{insight.suggestion}\"")
                lines.append(
                    f"  Confidence: {insight.confidence:.2f} "
                    f"(nights: {insight.night}) → {action_label}"
                )
                lines.append("")

        # Promotion summary
        if self.promotions:
            lines.append("## Promotion Summary")
            auto = [p for p in self.promotions if p["action"] == "AUTO_PROMOTE"]
            pr = [p for p in self.promotions if p["action"] == "CREATE_PR"]
            val = [p for p in self.promotions if p["action"] == "NEEDS_VALIDATION"]

            if auto:
                lines.append(f"\n### AUTO-PROMOTED ({len(auto)})")
                for p in auto:
                    lines.append(
                        f"- **{p['trader'].capitalize()}**: {p['insight']['description']}"
                    )
                    lines.append(f"  → {p['insight']['suggestion']}")

            if pr:
                lines.append(f"\n### PR-Ready ({len(pr)})")
                for p in pr:
                    lines.append(
                        f"- {p['trader'].capitalize()}: {p['insight']['description']}"
                    )

            if val:
                lines.append(f"\n### Needs Validation ({len(val)})")
                for p in val:
                    lines.append(
                        f"- {p['trader'].capitalize()}: {p['insight']['description']} "
                        f"(confidence: {p['insight']['confidence']:.2f})"
                    )

        lines.append(f"\n---\nGenerated at {datetime.now().isoformat()}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "n_traders": self.n_traders,
            "trader_syntheses": {
                name: ts.to_dict() for name, ts in self.trader_syntheses.items()
            },
            "top_insights": [i.to_dict() for i in self.top_insights],
            "promotions": self.promotions,
            "n_auto_promoted": self.n_auto_promoted,
            "n_pr_ready": self.n_pr_ready,
            "n_validation": self.n_validation,
        }


def _action_label(action: str) -> str:
    """Convert promotion action to a display label."""
    labels = {
        "AUTO_PROMOTE": "AUTO-PROMOTED ✓",
        "CREATE_PR": "Needs PR review",
        "NEEDS_VALIDATION": "Needs more validation",
    }
    return labels.get(action, action)


# ── Ranking ───────────────────────────────────────────────────────────────────


def rank_insights(
    insights: List[JournalInsight],
    dedup_threshold: float = 0.85,
) -> List[JournalInsight]:
    """Rank insights by confidence, deduplicating near-duplicates.

    Two insights are considered duplicates if they have the same category
    and their descriptions are very similar (Jaccard-like token overlap).

    Args:
        insights: List of JournalInsight objects.
        dedup_threshold: Similarity threshold for dedup (0.85 = 85% overlap).

    Returns:
        Sorted, deduplicated list (highest confidence first).
    """
    if not insights:
        return []

    # Sort by confidence descending
    sorted_insights = sorted(insights, key=lambda i: i.confidence, reverse=True)

    # Deduplicate: if two insights share category and have high token overlap,
    # keep the one with higher confidence
    unique: List[JournalInsight] = []
    for insight in sorted_insights:
        is_dup = False
        for existing in unique:
            if insight.category == existing.category:
                # Simple token overlap check
                tokens_new = set(insight.description.lower().split())
                tokens_existing = set(existing.description.lower().split())
                if tokens_new and tokens_existing:
                    overlap = len(tokens_new & tokens_existing) / max(
                        len(tokens_new | tokens_existing), 1
                    )
                    if overlap >= dedup_threshold:
                        is_dup = True
                        break

        if not is_dup:
            unique.append(insight)

    return unique


# ── Promotion evaluation ──────────────────────────────────────────────────────


def evaluate_promotion(
    insight: JournalInsight,
) -> Dict[str, Any]:
    """Evaluate whether an insight qualifies for promotion.

    Thresholds:
        - Confidence > 0.75 AND night >= 3 → AUTO_PROMOTE
        - Confidence > 0.50 AND night >= 2 → CREATE_PR
        - Otherwise → NEEDS_VALIDATION

    Args:
        insight: JournalInsight to evaluate.

    Returns:
        Dict with 'action', 'eligible', 'reason', 'suggestion', 'insight'.
    """
    confidence = insight.confidence
    night = insight.night

    if confidence > 0.75 and night >= 3:
        action = "AUTO_PROMOTE"
        eligible = True
        reason = (
            f"Confidence {confidence:.2f} > 0.75 AND sustained {night} nights "
            f"(>= 3) — threshold met for auto-promotion."
        )
    elif confidence > 0.50 and night >= 2:
        action = "CREATE_PR"
        eligible = True
        reason = (
            f"Confidence {confidence:.2f} > 0.50 AND sustained {night} nights "
            f"(>= 2) — ready for PR review."
        )
    elif confidence > 0.50 and night >= 1:
        action = "CREATE_PR"
        eligible = False
        reason = (
            f"Confidence {confidence:.2f} is promising but only {night} night(s) "
            f"sustained — need 2+ nights for PR."
        )
    else:
        action = "NEEDS_VALIDATION"
        eligible = False
        reason = (
            f"Confidence {confidence:.2f} or {night} nights sustained — "
            f"needs more data/validation."
        )

    return {
        "action": action,
        "eligible": eligible,
        "reason": reason,
        "suggestion": insight.suggestion,
        "insight": insight.to_dict(),
    }


# ── Per-trader synthesis ──────────────────────────────────────────────────────


def synthesize_trader(
    trader_name: str,
    insights: List[JournalInsight],
    scenarios: Dict[str, Any],
) -> TraderSynthesis:
    """Synthesize insights for a single trader.

    Args:
        trader_name: Name of the trader (e.g., 'kairos').
        insights: List of JournalInsight objects for this trader.
        scenarios: Dict with 'n_scenarios', 'n_trades', 'best_score', etc.

    Returns:
        TraderSynthesis object.
    """
    ranked = rank_insights(list(insights))

    return TraderSynthesis(
        trader=trader_name,
        n_scenarios=scenarios.get("n_scenarios", 0),
        n_trades=scenarios.get("n_trades", 0),
        best_score=scenarios.get("best_score", 0.0),
        top_variant=scenarios.get("top_variant", ""),
        insights=ranked,
    )


# ── Promoter ──────────────────────────────────────────────────────────────────


class Promoter:
    """Manages insight promotion tracking across nights.

    Tracks which insights have been seen before, increments their night
    counters, and evaluates promotion eligibility.

    Args:
        confidence_threshold: Minimum confidence for auto-promotion (0.75).
        auto_promote_nights: Nights sustained for auto-promotion (3).
        pr_nights: Nights sustained for PR creation (2).
        pr_confidence: Minimum confidence for PR creation (0.5).
    """

    def __init__(
        self,
        confidence_threshold: float = 0.75,
        auto_promote_nights: int = 3,
        pr_nights: int = 2,
        pr_confidence: float = 0.50,
    ):
        self.confidence_threshold = confidence_threshold
        self.auto_promote_nights = auto_promote_nights
        self.pr_nights = pr_nights
        self.pr_confidence = pr_confidence
        self._tracked: Dict[str, JournalInsight] = {}  # keyed by insight fingerprint

    @staticmethod
    def _fingerprint(insight: JournalInsight) -> str:
        """Create a stable fingerprint for an insight (category + first 80 chars of desc)."""
        return f"{insight.category}:{insight.description[:80]}"

    def evaluate_and_track(
        self,
        insight: JournalInsight,
    ) -> Tuple[Dict[str, Any], JournalInsight]:
        """Evaluate promotion eligibility and track this insight.

        The evaluation uses the insight's current night count. After evaluation,
        the night counter is incremented for the next call.

        Args:
            insight: JournalInsight to evaluate.

        Returns:
            Tuple of (promotion_result_dict, updated_insight_with_incremented_night).
        """
        fp = self._fingerprint(insight)

        # Determine the current night (before increment)
        if fp in self._tracked:
            current_night = self._tracked[fp].night
        else:
            current_night = max(0, insight.night)

        # Evaluate using the current night
        eval_insight = deepcopy(insight)
        eval_insight.night = current_night
        result = evaluate_promotion(eval_insight)

        # Increment night for next time
        updated = deepcopy(insight)
        updated.night = current_night + 1
        self._tracked[fp] = updated

        result["trader"] = ""  # Caller can set this
        return result, updated

    def get_eligible_promotions(self) -> List[Dict[str, Any]]:
        """Get all currently eligible promotions.

        Returns:
            List of promotion result dicts.
        """
        eligible = []
        for fingerprint, insight in self._tracked.items():
            result = evaluate_promotion(insight)
            if result["eligible"]:
                eligible.append(result)
        return eligible

    def reset(self) -> None:
        """Clear all tracked insights."""
        self._tracked.clear()


# ── Main Synthesizer ──────────────────────────────────────────────────────────


class Synthesizer:
    """Nightly synthesis engine — aggregates and promotes learning insights.

    Args:
        promoter: Optional pre-configured Promoter instance.
        confidence_threshold: Minimum confidence for auto-promotion.
        auto_promote_nights: Nights sustained for auto-promotion.
    """

    def __init__(
        self,
        promoter: Optional[Promoter] = None,
        confidence_threshold: float = 0.75,
        auto_promote_nights: int = 3,
    ):
        self.promoter = promoter or Promoter(
            confidence_threshold=confidence_threshold,
            auto_promote_nights=auto_promote_nights,
        )

    def synthesize(
        self,
        trader_insights: Dict[str, List[JournalInsight]],
        scenarios: Dict[str, Dict[str, Any]],
        date: Optional[datetime] = None,
    ) -> NightlySummary:
        """Run nightly synthesis across all traders.

        Args:
            trader_insights: Dict mapping trader_name → list of JournalInsight.
            scenarios: Dict mapping trader_name → scenario summary dict.
            date: Optional date for the summary (defaults to today).

        Returns:
            NightlySummary with per-trader syntheses and promotions.
        """
        if date is None:
            date = datetime.now()

        summary = NightlySummary(
            date=date.strftime("%Y-%m-%d"),
            n_traders=len(trader_insights),
        )

        all_insights: List[JournalInsight] = []

        # Synthesize each trader
        for trader_name, insights in trader_insights.items():
            trader_scenarios = scenarios.get(trader_name, {})
            synth = synthesize_trader(trader_name, insights, trader_scenarios)
            summary.trader_syntheses[trader_name] = synth
            all_insights.extend(synth.insights)

        # Rank all insights globally
        summary.top_insights = rank_insights(all_insights)

        # Evaluate promotions for all insights
        for trader_name, synth in summary.trader_syntheses.items():
            for insight in synth.insights:
                result, updated = self.promoter.evaluate_and_track(insight)
                result["trader"] = trader_name
                summary.promotions.append(result)

        # Sort promotions by action priority
        action_priority = {"AUTO_PROMOTE": 0, "CREATE_PR": 1, "NEEDS_VALIDATION": 2}
        summary.promotions.sort(key=lambda p: (
            action_priority.get(p["action"], 99),
            -p["insight"]["confidence"],
        ))

        # Count by action
        for p in summary.promotions:
            if p["action"] == "AUTO_PROMOTE":
                summary.n_auto_promoted += 1
            elif p["action"] == "CREATE_PR":
                summary.n_pr_ready += 1
            else:
                summary.n_validation += 1

        return summary


# ── Convenience function ──────────────────────────────────────────────────────


def synthesize_nightly(
    trader_insights: Dict[str, List[JournalInsight]],
    scenarios: Dict[str, Dict[str, Any]],
    date: Optional[datetime] = None,
    **kwargs: Any,
) -> NightlySummary:
    """One-line nightly synthesis.

    Args:
        trader_insights: Dict mapping trader_name → list of JournalInsight.
        scenarios: Dict mapping trader_name → scenario summary dict.
        date: Optional date for the summary.
        **kwargs: Passed to Synthesizer constructor.

    Returns:
        NightlySummary.
    """
    synthesizer = Synthesizer(**kwargs)
    return synthesizer.synthesize(
        trader_insights=trader_insights,
        scenarios=scenarios,
        date=date,
    )
