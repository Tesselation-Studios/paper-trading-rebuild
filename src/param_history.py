#!/usr/bin/env python3
"""
Parameter History Tracking (#23).

Records every parameter change with before/after objective scores,
tracks convergence toward stable values, detects oscillation cycles,
and provides analysis functions for the nightly synthesis pipeline.

USAGE:
    from src.param_history import ParamHistory

    ph = ParamHistory()
    ph.record_change("momentum_threshold", old=0.55, new=0.58,
                     before_score=1.2, after_score=1.4, source="gradient_descent")

    # Convergence: is this parameter settling?
    conv = ph.convergence_score("momentum_threshold", window=10)
    # → {"converging": True, "stable_value": 0.58, "variance_last_half": 0.0001}

    # Oscillation: is this parameter cycling?
    osc = ph.oscillation_check("momentum_threshold", window=20)
    # → {"oscillating": False, "cycles": 0, "amplitude": 0.02}

    # Full report for nightly synthesis
    report = ph.generate_report(trader_id="kairos", days=30)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.db.connection import get_connection

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ParamChange:
    """A single parameter change record."""
    id: int
    agent_id: str
    param_name: str
    old_value: Optional[float]
    new_value: Optional[float]
    before_score: Optional[float]
    after_score: Optional[float]
    changed_at: datetime
    source: str
    reason: str
    trader_id: str
    score_metric: str

    @property
    def delta(self) -> Optional[float]:
        """Absolute change in parameter value."""
        if self.old_value is not None and self.new_value is not None:
            return self.new_value - self.old_value
        return None

    @property
    def score_delta(self) -> Optional[float]:
        """Change in objective score."""
        if self.before_score is not None and self.after_score is not None:
            return self.after_score - self.before_score
        return None


@dataclass
class ConvergenceResult:
    """Result of convergence analysis for a parameter."""
    param_name: str
    converging: bool
    stable_value: Optional[float]
    variance_last_half: float
    trend_slope: float  # linear regression slope over window
    recent_mean: float
    recent_std: float
    samples: int

    @property
    def stability_score(self) -> float:
        """0–1 score: higher = more stable."""
        if self.variance_last_half == 0:
            return 1.0
        # Normalize: variance < 0.0001 → highly stable, > 0.01 → unstable
        return max(0.0, min(1.0, 1.0 - (self.variance_last_half / 0.01)))


@dataclass
class OscillationResult:
    """Result of oscillation analysis for a parameter."""
    param_name: str
    oscillating: bool
    cycles: int  # number of detected direction-reversal cycles
    amplitude: float  # peak-to-trough range
    direction_changes: int
    samples: int

    @property
    def oscillation_score(self) -> float:
        """0–1 score: 0 = stable, 1 = highly oscillatory."""
        if self.samples < 3:
            return 0.0
        ratio = self.direction_changes / self.samples
        return min(1.0, ratio * 2.0)  # scale up


@dataclass
class ParameterReport:
    """Full analysis report for a parameter or trader."""
    trader_id: str
    period_days: int
    total_changes: int
    params_analyzed: int
    converging_params: List[ConvergenceResult]
    oscillating_params: List[OscillationResult]
    score_improvements: Dict[str, float]  # param → avg score delta
    net_improvement: float  # total score change over period
    recommendations: List[str]


# ═══════════════════════════════════════════════════════════════════════════════
# Core class
# ═══════════════════════════════════════════════════════════════════════════════


class ParamHistory:
    """Record and analyze parameter change history with before/after scores.

    Args:
        agent_id: Default agent for recording (default: 'default').
        score_metric: Default metric name (default: 'calmar').
    """

    def __init__(self, agent_id: str = "default", score_metric: str = "calmar"):
        self.agent_id = agent_id
        self.score_metric = score_metric

    # ── Record ────────────────────────────────────────────────────────────

    def record_change(
        self,
        param_name: str,
        old_value: float,
        new_value: float,
        before_score: Optional[float] = None,
        after_score: Optional[float] = None,
        source: str = "manual",
        reason: str = "",
        trader_id: str = "",
        score_metric: Optional[str] = None,
    ) -> int:
        """Record a parameter change with before/after scores.

        Returns the new record's ID.
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO trading.param_history
                   (agent_id, param_name, old_value, new_value,
                    before_score, after_score, changed_at,
                    source, reason, trader_id, score_metric)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    self.agent_id, param_name,
                    float(old_value) if old_value is not None else None,
                    float(new_value) if new_value is not None else None,
                    float(before_score) if before_score is not None else None,
                    float(after_score) if after_score is not None else None,
                    datetime.now(),
                    source, reason, trader_id,
                    score_metric or self.score_metric,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"INSERT returned no id for {param_name}")
            record_id = row[0]
            conn.commit()
            cur.close()

            log.debug(
                "Recorded param change: %s %.4f→%.4f score: %s→%s [%s]",
                param_name, old_value, new_value, before_score, after_score, source,
            )
            return record_id

        except Exception as e:
            conn.rollback()
            log.error("Failed to record param change %s: %s", param_name, e)
            raise
        finally:
            conn.close()

    def update_after_score(self, record_id: int, after_score: float) -> bool:
        """Update the after_score for an existing record.

        Use when the outcome score becomes available later (e.g., after replay).
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE trading.param_history SET after_score = %s WHERE id = %s",
                (float(after_score), record_id),
            )
            conn.commit()
            updated = cur.rowcount > 0
            cur.close()
            return updated
        except Exception as e:
            conn.rollback()
            log.error("Failed to update after_score for record %s: %s", record_id, e)
            return False
        finally:
            conn.close()

    # ── Query ─────────────────────────────────────────────────────────────

    def get_history(
        self,
        param_name: Optional[str] = None,
        trader_id: Optional[str] = None,
        source: Optional[str] = None,
        days: int = 30,
        limit: int = 100,
    ) -> List[ParamChange]:
        """Query parameter change history with filters."""
        conn = None
        try:
            conn = get_connection()
            cur = conn.cursor()
            conditions = ["agent_id = %s"]
            params: List[Any] = [self.agent_id]

            cutoff = datetime.now() - timedelta(days=days)
            conditions.append("changed_at >= %s")
            params.append(cutoff)

            if param_name:
                conditions.append("param_name = %s")
                params.append(param_name)
            if trader_id:
                conditions.append("trader_id = %s")
                params.append(trader_id)
            if source:
                conditions.append("source = %s")
                params.append(source)

            query = (
                "SELECT id, agent_id, param_name, old_value, new_value, "
                "before_score, after_score, changed_at, source, reason, "
                "trader_id, score_metric "
                "FROM trading.param_history "
                f"WHERE {' AND '.join(conditions)} "
                "ORDER BY changed_at DESC "
                "LIMIT %s"
            )
            params.append(limit)

            cur.execute(query, tuple(params))
            rows = cur.fetchall()
            cur.close()
            return [ParamChange(*row) for row in rows]

        except Exception as e:
            log.error("Failed to query param history: %s", e)
            return []
        finally:
            if conn:
                conn.close()

    def get_latest(self, param_name: str, trader_id: str = "") -> Optional[ParamChange]:
        """Get the most recent change record for a parameter."""
        conn = get_connection()
        try:
            cur = conn.cursor()
            if trader_id:
                cur.execute(
                    """SELECT id, agent_id, param_name, old_value, new_value,
                       before_score, after_score, changed_at, source, reason,
                       trader_id, score_metric
                       FROM trading.param_history
                       WHERE agent_id = %s AND param_name = %s AND trader_id = %s
                       ORDER BY changed_at DESC LIMIT 1""",
                    (self.agent_id, param_name, trader_id),
                )
            else:
                cur.execute(
                    """SELECT id, agent_id, param_name, old_value, new_value,
                       before_score, after_score, changed_at, source, reason,
                       trader_id, score_metric
                       FROM trading.param_history
                       WHERE agent_id = %s AND param_name = %s
                       ORDER BY changed_at DESC LIMIT 1""",
                    (self.agent_id, param_name),
                )
            row = cur.fetchone()
            cur.close()
            return ParamChange(*row) if row else None

        except Exception as e:
            log.error("Failed to get latest param change %s: %s", param_name, e)
            return None
        finally:
            conn.close()

    # ── Convergence ───────────────────────────────────────────────────────

    def convergence_score(
        self,
        param_name: str,
        window: int = 10,
        trader_id: str = "",
    ) -> ConvergenceResult:
        """Check whether a parameter is converging toward a stable value.

        Uses two metrics:
        1. Variance ratio: variance of second half vs first half of window
        2. Trend slope: linear regression — near-zero slope = converging

        Returns:
            ConvergenceResult with converging flag and stability metrics.
        """
        records = self.get_history(
            param_name=param_name, trader_id=trader_id if trader_id else None,
            days=90, limit=window,
        )

        if len(records) < 4:
            return ConvergenceResult(
                param_name=param_name, converging=False, stable_value=None,
                variance_last_half=0.0, trend_slope=0.0,
                recent_mean=0.0, recent_std=0.0, samples=len(records),
            )

        # Values in chronological order (oldest first)
        values = np.array([r.new_value for r in reversed(records) if r.new_value is not None])

        if len(values) < 4:
            return ConvergenceResult(
                param_name=param_name, converging=False, stable_value=None,
                variance_last_half=0.0, trend_slope=0.0,
                recent_mean=float(np.mean(values)), recent_std=float(np.std(values)),
                samples=len(values),
            )

        mid = len(values) // 2
        first_half = values[:mid]
        second_half = values[mid:]

        var_first = float(np.var(first_half)) if len(first_half) > 1 else 0.0
        var_second = float(np.var(second_half)) if len(second_half) > 1 else 0.0

        # Trend slope using simple linear regression
        x = np.arange(len(values))
        slope = 0.0
        if len(values) > 1:
            x_mean = np.mean(x)
            y_mean = np.mean(values)
            numerator = np.sum((x - x_mean) * (values - y_mean))
            denominator = np.sum((x - x_mean) ** 2)
            slope = float(numerator / denominator) if denominator > 0 else 0.0

        # Convergence criteria:
        # 1. Variance decreasing (second half less variable than first)
        # 2. Near-zero slope (not trending up or down)
        # 3. Variance in second half is small
        var_decreasing = var_second < var_first if var_first > 0 else True
        slope_near_zero = abs(slope) < 0.001  # essentially flat

        # Normalize slope to parameter range scale
        range_est = np.ptp(values) if np.ptp(values) > 0 else 1.0
        normalized_slope = abs(slope) / range_est if range_est > 0 else 0.0

        # Converging if: variance is decreasing AND either the trend is near-flat
        # or the second half has negligible variance (settled to a stable value).
        converging = bool(var_decreasing and (
            normalized_slope < 0.12 or var_second < 0.0005
        ))

        return ConvergenceResult(
            param_name=param_name,
            converging=converging,
            stable_value=float(np.mean(second_half)),
            variance_last_half=var_second,
            trend_slope=slope,
            recent_mean=float(np.mean(values)),
            recent_std=float(np.std(values)),
            samples=len(values),
        )

    # ── Oscillation ───────────────────────────────────────────────────────

    def oscillation_check(
        self,
        param_name: str,
        window: int = 20,
        trader_id: str = "",
    ) -> OscillationResult:
        """Detect whether a parameter is oscillating (cycling between values).

        Counts direction changes: a param that goes up→down→up→down repeatedly
        is oscillating. High direction-change ratio = oscillatory behavior.

        Returns:
            OscillationResult with oscillating flag and cycle metrics.
        """
        records = self.get_history(
            param_name=param_name, trader_id=trader_id if trader_id else None,
            days=90, limit=window,
        )

        if len(records) < 4:
            return OscillationResult(
                param_name=param_name, oscillating=False,
                cycles=0, amplitude=0.0, direction_changes=0,
                samples=len(records),
            )

        # Values in chronological order (oldest first)
        values = [r.new_value for r in reversed(records) if r.new_value is not None]

        if len(values) < 4:
            return OscillationResult(
                param_name=param_name, oscillating=False,
                cycles=0, amplitude=0.0, direction_changes=0,
                samples=len(records),  # report actual record count
            )

        # Count direction changes
        direction_changes = 0
        current_direction = 0  # +1 up, -1 down, 0 starting

        for i in range(1, len(values)):
            if values[i] is None or values[i-1] is None:
                continue
            delta = values[i] - values[i-1]
            if abs(delta) < 1e-10:
                continue  # no change, skip

            new_direction = 1 if delta > 0 else -1
            if current_direction != 0 and new_direction != current_direction:
                direction_changes += 1
            current_direction = new_direction

        cycles = direction_changes // 2  # one cycle = two direction changes
        amplitude = float(max(values) - min(values)) if values else 0.0

        # Oscillation criteria: more than 2 direction changes and
        # direction changes exceed 40% of samples
        oscillating = direction_changes >= 2 and (direction_changes / len(values)) >= 0.4

        return OscillationResult(
            param_name=param_name,
            oscillating=oscillating,
            cycles=cycles,
            amplitude=amplitude,
            direction_changes=direction_changes,
            samples=len(values),
        )

    # ── Report ────────────────────────────────────────────────────────────

    def generate_report(
        self,
        trader_id: Optional[str] = None,
        days: int = 30,
    ) -> ParameterReport:
        """Generate a parameter history analysis report for nightly synthesis.

        Args:
            trader_id: Filter by trader (None = all).
            days: Lookback window.

        Returns:
            ParameterReport with convergence, oscillation, and score metrics.
        """
        records = self.get_history(
            trader_id=trader_id, days=days, limit=500,
        )

        if not records:
            return ParameterReport(
                trader_id=trader_id or "all",
                period_days=days,
                total_changes=0,
                params_analyzed=0,
                converging_params=[],
                oscillating_params=[],
                score_improvements={},
                net_improvement=0.0,
                recommendations=["No parameter changes in the last {} days.".format(days)],
            )

        # Group by param_name
        param_names = sorted(set(r.param_name for r in records))

        converging_params: List[ConvergenceResult] = []
        oscillating_params: List[OscillationResult] = []
        score_improvements: Dict[str, float] = {}
        total_score_delta = 0.0
        score_count = 0

        for pname in param_names:
            conv = self.convergence_score(
                pname, window=min(len(records), 20),
                trader_id=trader_id or "",
            )
            if conv.converging:
                converging_params.append(conv)

            osc = self.oscillation_check(
                pname, window=min(len(records), 20),
                trader_id=trader_id or "",
            )
            if osc.oscillating:
                oscillating_params.append(osc)

            # Average score improvement for this parameter
            param_records = [r for r in records if r.param_name == pname]
            deltas = [r.score_delta for r in param_records if r.score_delta is not None]
            if deltas:
                score_improvements[pname] = float(np.mean(deltas))
                total_score_delta += sum(deltas)
                score_count += len(deltas)

        net_improvement = total_score_delta  # cumulative score change

        # Build recommendations
        recommendations: List[str] = []
        for c in converging_params:
            if c.stability_score > 0.8:
                recommendations.append(
                    f"{c.param_name}: Highly stable (stability={c.stability_score:.2f}). "
                    f"Consider reducing change frequency or freezing."
                )
        for o in oscillating_params:
            recommendations.append(
                f"⚠️ {o.param_name}: Oscillating ({o.cycles} cycles, amplitude={o.amplitude:.4f}). "
                f"Check if gradient noise or regime shift is causing instability."
            )
        if not recommendations and records:
            recommendations.append(
                f"All {len(param_names)} parameters are stable. "
                f"Net score change: {net_improvement:+.4f} over {score_count} changes."
            )

        return ParameterReport(
            trader_id=trader_id or "all",
            period_days=days,
            total_changes=len(records),
            params_analyzed=len(param_names),
            converging_params=converging_params,
            oscillating_params=oscillating_params,
            score_improvements=score_improvements,
            net_improvement=net_improvement,
            recommendations=recommendations,
        )

    def summary_str(self, report: ParameterReport) -> str:
        """Format a ParameterReport as a human-readable summary string."""
        lines = [
            f"📊 Parameter History Report — {report.trader_id} ({report.period_days}d)",
            f"   Total changes: {report.total_changes} | Params tracked: {report.params_analyzed}",
            f"   Net score change: {report.net_improvement:+.4f}",
        ]

        if report.converging_params:
            lines.append(f"   ✅ Converging: {len(report.converging_params)} params")
            for c in report.converging_params[:5]:
                lines.append(
                    f"      {c.param_name}: stable≈{c.stable_value:.4f} "
                    f"(stability={c.stability_score:.2f})"
                )

        if report.oscillating_params:
            lines.append(f"   ⚠️  Oscillating: {len(report.oscillating_params)} params")
            for o in report.oscillating_params[:5]:
                lines.append(
                    f"      {o.param_name}: {o.cycles} cycles, "
                    f"amplitude={o.amplitude:.4f} (score={o.oscillation_score:.2f})"
                )

        if report.score_improvements:
            improvements = sorted(report.score_improvements.items(),
                                   key=lambda x: x[1], reverse=True)
            best = improvements[:3]
            worst = improvements[-3:]
            lines.append(f"   📈 Top improvers: {', '.join(f'{k}({v:+.3f})' for k, v in best)}")
            if len(worst) > 0 and worst != best:
                lines.append(f"   📉 Regressors: {', '.join(f'{k}({v:+.3f})' for k, v in worst)}")

        if report.recommendations:
            lines.append("   💡 Recommendations:")
            for rec in report.recommendations[:5]:
                lines.append(f"      • {rec}")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience functions
# ═══════════════════════════════════════════════════════════════════════════════


def record_gradient_step(
    param_name: str,
    old_value: float,
    new_value: float,
    before_score: float,
    trader_id: str = "",
    learning_rate: float = 0.01,
    gradient: float = 0.0,
) -> int:
    """Record a gradient descent parameter change with full context."""
    ph = ParamHistory()
    return ph.record_change(
        param_name=param_name,
        old_value=old_value,
        new_value=new_value,
        before_score=before_score,
        after_score=None,  # filled in after replay validates
        source="gradient_descent",
        reason=(
            f"gradient step: grad={gradient:+.4f}, lr={learning_rate}, "
            f"delta={new_value - old_value:+.4f}"
        ),
        trader_id=trader_id,
    )


def record_prompt_sweep(
    param_name: str,
    old_value: float,
    new_value: float,
    before_score: float,
    after_score: float,
    trader_id: str = "",
    variant_id: str = "",
) -> int:
    """Record a prompt sweep parameter change."""
    ph = ParamHistory()
    return ph.record_change(
        param_name=param_name,
        old_value=old_value,
        new_value=new_value,
        before_score=before_score,
        after_score=after_score,
        source="prompt_sweep",
        reason=f"Prompt sweep variant: {variant_id}",
        trader_id=trader_id,
    )


def get_nightly_summary(trader_id: str = "", days: int = 1) -> str:
    """Get a quick summary of last night's parameter changes.

    Intended for the nightly synthesis cron pipeline.
    """
    ph = ParamHistory()
    report = ph.generate_report(trader_id=trader_id, days=max(days, 1))
    return ph.summary_str(report)
