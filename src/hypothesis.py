"""Hypothesis Generator — analyzes sweep results and proposes new variants.

Nightly analysis:
  1. Load sweep results
  2. Group by: trader, regime, variant, params
  3. Find patterns in what works
  4. Generate new prompt+param combos to test

Usage:
    gen = HypothesisGenerator()
    hypotheses = gen.analyze(sweep_results, trader)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

log = logging.getLogger("hypothesis")


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class Hypothesis:
    """One new scenario to test next night."""
    trader: str
    description: str
    confidence: float  # 0.0-1.0 — how likely to improve
    prompt_diff: str = ""       # diff against base AGENTS.md
    param_suggestions: Dict[str, float] = field(default_factory=dict)
    source: str = ""            # what pattern generated this


@dataclass
class Pattern:
    """A discovered pattern in sweep results."""
    trader: str
    pattern_type: str           # "param_correlation", "prompt_win", "regime_specific"
    description: str
    confidence: float
    supporting_evidence: List[Dict[str, Any]] = field(default_factory=list)


# ── Hypothesis Generator ─────────────────────────────────────────────────────


class HypothesisGenerator:
    """Analyzes sweep results and generates new hypotheses to test.

    Args:
        min_confidence: Minimum confidence to output a hypothesis (0.0-1.0).
        max_hypotheses: Maximum new hypotheses per night.
        improvement_threshold: Minimum score improvement to consider significant.
    """

    def __init__(
        self,
        min_confidence: float = 0.3,
        max_hypotheses: int = 5,
        improvement_threshold: float = 0.02,
    ):
        self.min_confidence = min_confidence
        self.max_hypotheses = max_hypotheses
        self.improvement_threshold = improvement_threshold

    def analyze(
        self,
        results: List[Any],  # List[ScenarioResult]
        trader: str,
        baseline_score: Optional[float] = None,
    ) -> List[Hypothesis]:
        """Analyze sweep results and generate new hypotheses.

        Args:
            results: List of ScenarioResult from a sweep run.
            trader: Trader ID being analyzed.
            baseline_score: Score of the baseline variant (if None, inferred).

        Returns:
            Ranked list of Hypotheses to test next night.
        """
        if not results:
            log.warning("No results to analyze for %s", trader)
            return []

        patterns = self._find_patterns(results, trader)
        hypotheses = self._generate_from_patterns(patterns, results, trader, baseline_score)

        # Rank by confidence
        hypotheses.sort(key=lambda h: h.confidence, reverse=True)

        # Cap
        return hypotheses[:self.max_hypotheses]

    def _find_patterns(
        self,
        results: List[Any],
        trader: str,
    ) -> List[Pattern]:
        """Discover patterns in what scored well vs poorly."""
        patterns: List[Pattern] = []

        if not results:
            return patterns

        # Sort by score
        sorted_results = sorted(results, key=lambda r: r.objective_score, reverse=True)
        top = sorted_results[:max(3, len(sorted_results) // 4)]
        bottom = sorted_results[-max(3, len(sorted_results) // 4):]

        mean_top = np.mean([r.objective_score for r in top]) if top else 0
        mean_bottom = np.mean([r.objective_score for r in bottom]) if bottom else 0

        if mean_top - mean_bottom < self.improvement_threshold:
            patterns.append(Pattern(
                trader=trader,
                pattern_type="no_signal",
                description="No significant difference between best and worst — optimizer may be at a plateau",
                confidence=0.9,
            ))
            return patterns

        # Find param correlations
        param_effects = self._analyze_param_effects(results)
        for param_name, effect in param_effects.items():
            if abs(effect["correlation"]) > 0.3:
                direction = "higher" if effect["correlation"] > 0 else "lower"
                patterns.append(Pattern(
                    trader=trader,
                    pattern_type="param_correlation",
                    description=f"{param_name}: {direction} values correlate with "
                                f"{'better' if effect['correlation'] > 0 else 'worse'} scores "
                                f"(r={effect['correlation']:.2f})",
                    confidence=min(0.9, abs(effect["correlation"])),
                    supporting_evidence=[{
                        "param": param_name,
                        "correlation": round(effect["correlation"], 3),
                        "best_value": effect["best_value"],
                        "worst_value": effect["worst_value"],
                    }],
                ))

        # Find prompt patterns
        prompt_effects = self._analyze_prompt_effects(results)
        for variant_id, effect in prompt_effects.items():
            if effect["mean_score"] > 0 and effect["count"] >= 2:
                patterns.append(Pattern(
                    trader=trader,
                    pattern_type="prompt_win",
                    description=f"Variant '{variant_id}' consistently scores above average "
                                f"(mean +{effect['mean_score']:.4f} vs baseline)",
                    confidence=min(0.8, effect["count"] / 5),
                    supporting_evidence=[{
                        "variant": variant_id,
                        "mean_improvement": round(effect["mean_score"], 4),
                        "count": effect["count"],
                    }],
                ))

        return patterns

    def _analyze_param_effects(
        self,
        results: List[Any],
    ) -> Dict[str, Dict[str, float]]:
        """Compute correlation between param values and scores."""
        if not results:
            return {}

        # Collect all param names
        param_names = set()
        for r in results:
            param_names.update(r.params.keys())

        effects = {}
        for param_name in param_names:
            values = []
            scores = []
            for r in results:
                if param_name in r.params:
                    values.append(r.params[param_name])
                    scores.append(r.objective_score)

            if len(values) < 3:
                continue

            values_arr = np.array(values)
            scores_arr = np.array(scores)

            # Pearson correlation
            if np.std(values_arr) > 0 and np.std(scores_arr) > 0:
                corr = np.corrcoef(values_arr, scores_arr)[0, 1]
            else:
                corr = 0.0

            # Find best and worst values
            best_idx = np.argmax(scores_arr)
            worst_idx = np.argmin(scores_arr)

            effects[param_name] = {
                "correlation": float(corr),
                "best_value": float(values_arr[best_idx]),
                "worst_value": float(values_arr[worst_idx]),
            }

        return effects

    def _analyze_prompt_effects(
        self,
        results: List[Any],
    ) -> Dict[str, Dict[str, Any]]:
        """Group results by variant and compute mean improvement."""
        if not results:
            return {}

        # Find baseline score
        baseline_scores = [r.objective_score for r in results if r.variant_id == "baseline"]
        baseline = np.mean(baseline_scores) if baseline_scores else np.mean([r.objective_score for r in results])

        effects = {}
        for r in results:
            if r.variant_id not in effects:
                effects[r.variant_id] = {"scores": [], "count": 0}
            effects[r.variant_id]["scores"].append(r.objective_score - baseline)
            effects[r.variant_id]["count"] += 1

        for vid, data in effects.items():
            data["mean_score"] = float(np.mean(data["scores"]))
            del data["scores"]

        return effects

    def _generate_from_patterns(
        self,
        patterns: List[Pattern],
        results: List[Any],
        trader: str,
        baseline_score: Optional[float] = None,
    ) -> List[Hypothesis]:
        """Convert patterns into actionable hypotheses."""
        hypotheses: List[Hypothesis] = []

        for pattern in patterns:
            if pattern.confidence < self.min_confidence:
                continue

            if pattern.pattern_type == "param_correlation":
                hypotheses.extend(self._param_hypotheses(pattern, results, trader))

            elif pattern.pattern_type == "prompt_win":
                hypotheses.extend(self._prompt_hypotheses(pattern, results, trader))

            elif pattern.pattern_type == "no_signal":
                # At plateau: try more aggressive or defensive variants
                hypotheses.append(Hypothesis(
                    trader=trader,
                    description="Plateau detected — try more aggressive sizing",
                    confidence=0.4,
                    prompt_diff="- Never exceed 20% of portfolio in one position\n+ Never exceed 30% of portfolio in one position",
                    param_suggestions={"base_size_pct": 0.22},
                    source="plateau_exploration",
                ))
                hypotheses.append(Hypothesis(
                    trader=trader,
                    description="Plateau detected — try tighter stops",
                    confidence=0.4,
                    param_suggestions={"stop_loss_pct": 0.03},
                    source="plateau_exploration",
                ))

        return hypotheses

    def _param_hypotheses(
        self,
        pattern: Pattern,
        results: List[Any],
        trader: str,
    ) -> List[Hypothesis]:
        """Generate hypotheses from parameter correlations."""
        hypotheses = []
        for evidence in pattern.supporting_evidence:
            param = evidence["param"]
            best_val = evidence["best_value"]
            corr = evidence["correlation"]

            # Propose values around the best found
            for delta in [0.02, -0.02, 0.05]:
                new_val = round(best_val * (1 + delta), 3)
                if new_val <= 0:
                    continue

                hypotheses.append(Hypothesis(
                    trader=trader,
                    description=f"Try {param}={new_val} (near best={best_val}, r={corr:.2f})",
                    confidence=min(0.7, abs(corr) * 0.8),
                    param_suggestions={param: new_val},
                    source=f"param_correlation:{param}",
                ))

        return hypotheses

    def _prompt_hypotheses(
        self,
        pattern: Pattern,
        results: List[Any],
        trader: str,
    ) -> List[Hypothesis]:
        """Generate hypotheses from prompt variant successes."""
        hypotheses = []

        for evidence in pattern.supporting_evidence:
            variant = evidence["variant"]
            improvement = evidence["mean_improvement"]

            # If a non-baseline variant wins, propose variations on it
            if variant != "baseline" and improvement > self.improvement_threshold:
                hypotheses.append(Hypothesis(
                    trader=trader,
                    description=f"Winning variant '{variant}' (+{improvement:.4f}) — "
                                f"propose enhanced version",
                    confidence=min(0.8, improvement * 10),
                    prompt_diff=f"# Enhanced from {variant}\n# Original improvement: +{improvement:.4f}",
                    source=f"prompt_enhancement:{variant}",
                ))

        return hypotheses


# ── Report Generation ─────────────────────────────────────────────────────────


def generate_weekly_summary(
    trader_results: Dict[str, List[Any]],  # trader_id → List[ScenarioResult]
    start_date: str,
    end_date: str,
) -> str:
    """Generate a human-readable weekly summary of learning.

    Args:
        trader_results: Map of trader_id → all scenario results for the week.
        start_date: Start of the week (YYYY-MM-DD).
        end_date: End of the week (YYYY-MM-DD).

    Returns:
        Markdown-formatted summary string.
    """
    lines = [f"# Weekly Learning Summary: {start_date} to {end_date}\n"]

    for trader_id, results in trader_results.items():
        if not results:
            continue

        total = len(results)
        best = max(results, key=lambda r: r.objective_score)
        worst = min(results, key=lambda r: r.objective_score)

        lines.append(f"## {trader_id.title()} — {total} scenarios tested\n")
        lines.append(f"- **Best:** variant `{best.variant_id}` score={best.objective_score:.4f} "
                     f"pnl=${best.replay_result.total_pnl:,.0f}")
        if best.params:
            lines.append(f"  params: {json.dumps(best.params)}")
        lines.append(f"- **Worst:** variant `{worst.variant_id}` score={worst.objective_score:.4f}")
        lines.append(f"- **Spread:** {best.objective_score - worst.objective_score:.4f}")
        lines.append("")

    return "\n".join(lines)
