#!/usr/bin/env python3
"""
GradingScorer + LeaderboardBuilder — SPEC-v3 §1.2 and nightly-prompt-grading.md.

Computes composite scores from sweep/variant results, performs rank normalization,
applies knockout conditions, and produces leaderboard output for the dashboard
and Canvas cards.

Architecture:
    GradingScorer
      ├── score_variant(variant_data) -> CompositeScore
      │     ├─ calmar  (weight=0.30)
      │     ├─ sortino (weight=0.25)
      │     ├─ profit_factor (weight=0.20)
      │     ├─ win_rate (weight=0.15)
      │     └─ expectancy (weight=0.10)
      ├── rank_normalize(scores) -> ranked scores (0..1)
      ├── apply_knockouts(scores) -> filtered scores
      └── build_leaderboard(variants) -> sorted, ranked, filtered list

    LeaderboardBuilder
      ├── to_canvas_card(leaderboard) -> markdown card
      ├── to_dashboard_json(leaderboard) -> JSON for /api/leaderboard
      └── to_fusion_format(leaderboard) -> for fusion-review

Usage:
    from src.grading import GradingScorer, LeaderboardBuilder

    scorer = GradingScorer()
    scores = [scorer.score_variant(v) for v in variants]
    scores = scorer.apply_knockouts(scores)
    ranked = scorer.rank_normalize(scores)

    builder = LeaderboardBuilder()
    card = builder.to_canvas_card(ranked)
    j = builder.to_dashboard_json(ranked)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("grading")


# ═══════════════════════════════════════════════════════════════════════════════
# Domain Types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class VariantResult:
    """Raw result data for a single prompt variant from a sweep run."""
    trader_id: str
    variant_id: int
    params_hash: str
    total_return_pct: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    sortino: float
    calmar: float
    expectancy: float
    n_trades: int
    n_ticks: int
    model_used: str = ""
    run_id: int = 0
    date: str = ""


@dataclass
class CompositeScore:
    """Normalized composite score for a variant, with component breakdown."""
    trader_id: str
    variant_id: int
    params_hash: str
    composite: float           # 0.0 – 1.0 composite score
    rank: int = 0              # 1 = best
    rank_normalized: float = 0.0  # 0.0 (worst) – 1.0 (best)

    # Component scores (before normalization)
    raw_calmar: float = 0.0
    raw_sortino: float = 0.0
    raw_profit_factor: float = 0.0
    raw_win_rate: float = 0.0
    raw_expectancy: float = 0.0

    # Normalized components (0.0 – 1.0)
    calmar_score: float = 0.0
    sortino_score: float = 0.0
    profit_factor_score: float = 0.0
    win_rate_score: float = 0.0
    expectancy_score: float = 0.0

    # Knockout flags
    knocked_out: bool = False
    knockout_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════════════
# GradingScorer
# ═══════════════════════════════════════════════════════════════════════════════


class GradingScorer:
    """Computes composite scores for prompt variants.

    Weights (from nightly-prompt-grading.md §4.1):
        calmar:          0.30
        sortino:         0.25
        profit_factor:   0.20
        win_rate:        0.15
        expectancy:      0.10

    Knockout conditions (§4.3):
        - n_trades < 10
        - max_drawdown > 50%
        - total_return_pct < -20%
        - profit_factor < 0.5
    """

    WEIGHTS: Dict[str, float] = {
        "calmar": 0.30,
        "sortino": 0.25,
        "profit_factor": 0.20,
        "win_rate": 0.15,
        "expectancy": 0.10,
    }

    KNOCKOUTS: Dict[str, Tuple[str, Any]] = {
        "min_trades": ("n_trades < 10", 10),
        "max_drawdown": ("max_drawdown > 50%", 50.0),
        "min_return": ("total_return_pct < -20%", -20.0),
        "min_profit_factor": ("profit_factor < 0.5", 0.5),
    }

    def __init__(self, weights: Optional[Dict[str, float]] = None) -> None:
        self.weights = weights or dict(self.WEIGHTS)

    # ── Public API ────────────────────────────────────────────────────────

    def score_variant(self, variant: VariantResult) -> CompositeScore:
        """Compute CompositeScore for a single variant.

        Returns scored variant with raw components; knockout and normalization
        are applied separately via apply_knockouts() and rank_normalize().
        """
        score = CompositeScore(
            trader_id=variant.trader_id,
            variant_id=variant.variant_id,
            params_hash=variant.params_hash,
            composite=0.0,
            raw_calmar=variant.calmar,
            raw_sortino=variant.sortino,
            raw_profit_factor=variant.profit_factor,
            raw_win_rate=variant.win_rate,
            raw_expectancy=variant.expectancy,
        )

        # Check knockout conditions
        if variant.n_trades < self.KNOCKOUTS["min_trades"][1]:
            score.knocked_out = True
            score.knockout_reason = f"Too few trades: {variant.n_trades} < {self.KNOCKOUTS['min_trades'][1]}"
            return score

        if variant.max_drawdown > self.KNOCKOUTS["max_drawdown"][1]:
            score.knocked_out = True
            score.knockout_reason = f"Max drawdown too high: {variant.max_drawdown:.1f}% > 50%"
            score.raw_max_drawdown = variant.max_drawdown  # type: ignore[attr-defined]
            return score

        if variant.total_return_pct < self.KNOCKOUTS["min_return"][1]:
            score.knocked_out = True
            score.knockout_reason = f"Return too low: {variant.total_return_pct:.1f}% < -20%"
            return score

        if variant.profit_factor < self.KNOCKOUTS["min_profit_factor"][1]:
            score.knocked_out = True
            score.knockout_reason = f"Profit factor too low: {variant.profit_factor:.2f} < 0.5"
            return score

        # Compute component scores (raw -> component, winsorized)
        components = {
            "calmar": self._cap_score(variant.calmar, 5.0),
            "sortino": self._cap_score(variant.sortino, 5.0),
            "profit_factor": self._cap_score(variant.profit_factor * 0.2, 1.0),  # scale to 0-1
            "win_rate": variant.win_rate / 100.0 if variant.win_rate > 1 else variant.win_rate,
            "expectancy": self._cap_score(variant.expectancy * 0.1, 1.0),
        }

        score.calmar_score = components["calmar"]
        score.sortino_score = components["sortino"]
        score.profit_factor_score = components["profit_factor"]
        score.win_rate_score = components["win_rate"]
        score.expectancy_score = components["expectancy"]

        # Composite = weighted sum
        score.composite = sum(
            components[k] * self.weights.get(k, 0.0)
            for k in self.weights
        )

        return score

    def score_batch(self, variants: List[VariantResult]) -> List[CompositeScore]:
        """Score a batch of variants and return scored + knocked-out list."""
        return [self.score_variant(v) for v in variants]

    def apply_knockouts(self, scores: List[CompositeScore]) -> List[CompositeScore]:
        """Filter out knocked-out variants, return only passing scores."""
        return [s for s in scores if not s.knocked_out]

    def rank_normalize(
        self,
        scores: List[CompositeScore],
    ) -> List[CompositeScore]:
        """Rank and normalize composite scores to 0.0 – 1.0.

        Best variant gets rank_normalized = 1.0, worst = 0.0.
        Also assigns rank (1 = best).
        """
        passing = [s for s in scores if not s.knocked_out]
        if not passing:
            return scores

        passing.sort(key=lambda s: s.composite, reverse=True)

        composites = [s.composite for s in passing]
        min_c, max_c = min(composites), max(composites)
        range_c = max_c - min_c if max_c > min_c else 1.0

        for i, s in enumerate(passing):
            s.rank = i + 1
            s.rank_normalized = (s.composite - min_c) / range_c

        return scores

    def _cap_score(self, value: float, cap: float = 5.0) -> float:
        """Clamp positive values to cap, floor negative values to 0."""
        if value <= 0:
            return 0.0
        capped = value / cap
        return min(capped, 1.0)

    def build_leaderboard(
        self,
        variants: List[VariantResult],
    ) -> List[CompositeScore]:
        """Full pipeline: score → rank normalization.

        Returns sorted list of all CompositeScore objects (including knocked-out).
        Knocked-out variants have rank 0 and rank_normalized = 0.0.
        Use apply_knockouts() separately if only passing variants are needed.
        """
        scores = self.score_batch(variants)
        scores = self.rank_normalize(scores)
        # Sort: passing first (by rank), then knocked-out
        passing = sorted(
            [s for s in scores if not s.knocked_out],
            key=lambda s: s.rank,
        )
        knocked = [s for s in scores if s.knocked_out]
        return passing + knocked


# ═══════════════════════════════════════════════════════════════════════════════
# LeaderboardBuilder
# ═══════════════════════════════════════════════════════════════════════════════


class LeaderboardBuilder:
    """Builds leaderboard output formats from scored variants.

    Produces:
        - Canvas card (markdown)
        - Dashboard JSON
        - Fusion format
    """

    @staticmethod
    def to_canvas_card(
        scores: List[CompositeScore],
        title: str | None = None,
    ) -> str:
        """Format leaderboard as a markdown canvas card.

        Args:
            scores: Ranked list of CompositeScore objects
            title: Optional custom title (default: auto-generated)

        Returns:
            Markdown string suitable for Canvas/HEARTBEAT.md
        """
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        title = title or f"📊 Leaderboard — {date_str}"

        lines = [
            f"## {title}",
            "",
            "| Rank | Trader | Variant | Composite | Calmar | Sortino | PF | WR |",
            "|------|--------|---------|-----------|--------|---------|----|----|",
        ]

        for s in scores:
            if s.knocked_out:
                lines.append(
                    f"| KO | {s.trader_id} | #{s.variant_id} "
                    f"| — | — | — | — | — |"
                    f"\n| | _KO: {s.knockout_reason}_ | | | | | |"
                )
            else:
                lines.append(
                    f"| {s.rank} | {s.trader_id} | #{s.variant_id} "
                    f"| {s.composite:.4f} "
                    f"| {s.calmar_score:.2f} "
                    f"| {s.sortino_score:.2f} "
                    f"| {s.profit_factor_score:.2f} "
                    f"| {s.win_rate_score:.2f} |"
                )

        lines.extend(["", f"_{len([s for s in scores if not s.knocked_out])} passing, "
                           f"{len([s for s in scores if s.knocked_out])} knocked out_"])
        return "\n".join(lines)

    @staticmethod
    def to_dashboard_json(scores: List[CompositeScore]) -> str:
        """Format leaderboard as JSON for the dashboard API.

        Returns:
            JSON string with 'leaderboard' and 'metadata' keys.
        """
        entries = []
        for s in scores:
            entry = {
                "trader_id": s.trader_id,
                "variant_id": s.variant_id,
                "rank": s.rank,
                "composite": round(s.composite, 4),
                "rank_normalized": round(s.rank_normalized, 4),
                "knocked_out": s.knocked_out,
            }
            if s.knocked_out:
                entry["knockout_reason"] = s.knockout_reason
            entries.append(entry)

        payload = {
            "leaderboard": entries,
            "metadata": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "n_total": len(scores),
                "n_passing": len([s for s in scores if not s.knocked_out]),
                "n_knocked_out": len([s for s in scores if s.knocked_out]),
            },
        }
        return json.dumps(payload, indent=2)

    @staticmethod
    def to_fusion_format(scores: List[CompositeScore]) -> Dict[str, Any]:
        """Format leaderboard for fusion-review ingestion.

        Returns:
            Dict with 'variants' list and 'summary'.
        """
        return {
            "variants": [
                {
                    "trader_id": s.trader_id,
                    "variant_id": s.variant_id,
                    "composite": round(s.composite, 4),
                    "rank": s.rank,
                    "knocked_out": s.knocked_out,
                }
                for s in scores
            ],
            "summary": {
                "count": len(scores),
                "passing": len([s for s in scores if not s.knocked_out]),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }