"""Scorer — compares variant trades against discovered signals.

Weighted formula:
    score = 0.40 * catch_rate
          - 0.25 * false_positive_rate
          + 0.35 * normalized_return

Secondary metrics (display only):
    - Cash idle %
    - Win rate
    - Avg hold time
    - Max drawdown
    - Signal quality (avg conviction/catalyst of caught signals)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from src.overnight_replay import ConfigVariant, VariantResult
from src.replay import Tick, Trade

log = logging.getLogger(__name__)

# ── Weights for primary scoring formula ───────────────────────────────────────

CATCH_RATE_WEIGHT = 0.40
FALSE_POSITIVE_WEIGHT = -0.25
RETURN_WEIGHT = 0.35
DEPLOYMENT_WEIGHT = 0.10

TRADING_DAYS_PER_YEAR = 252


# ── Split-window Sharpe validation ──────────────────────────────────────────
# 2026-07-31: ported from workspace-trader-stonks/scripts/replay_check.py's
# split_ticks_by_midpoint()/compute_risk_metrics() -- the SAME methodology
# already used as this system's live-promotion bar (e.g. the 2026-07-27
# stop_patience.py revert, TRAIL_K=40 research): a config only counts as
# "robust" if Sharpe is positive in BOTH halves of the period, not just on
# one aggregate full-window number. overnight-insights.md's Round 2/3/5 all
# requested this be "baked into the harness scoring" and it never was --
# this closes that gap using the proven implementation rather than a new one.


def split_ticks_by_midpoint(ticks: List[Tick]) -> tuple[List[Tick], List[Tick]]:
    """Split a chronological tick stream into two halves by DATE midpoint
    (not tick/index count, which skews toward whichever half has more
    multi-ticker density)."""
    if not ticks:
        return [], []
    dates = sorted({t.timestamp for t in ticks})
    mid_date = dates[len(dates) // 2]
    first_half = [t for t in ticks if t.timestamp < mid_date]
    second_half = [t for t in ticks if t.timestamp >= mid_date]
    return first_half, second_half


def _resample_to_daily_equity(result) -> Optional[List[float]]:
    """Collapse (ticker, tick) equity samples to one value per calendar day
    (last observation wins) before computing return-based metrics --
    without this, Sharpe would be computed over an oversampled per-tick
    series and badly understate volatility relative to a real daily
    portfolio return series."""
    if not result.timestamps or len(result.timestamps) != len(result.equity_curve):
        return None
    daily: Dict[Any, float] = {}
    for ts, equity in zip(result.timestamps, result.equity_curve):
        day = ts.date() if hasattr(ts, "date") else ts
        daily[day] = float(equity)
    days = sorted(daily.keys())
    return [daily[d] for d in days]


def compute_sharpe(result, risk_free_rate: float = 0.0) -> Optional[float]:
    """Annualized Sharpe from a resampled daily equity series. None if
    there isn't enough daily history (need 2+ trading days)."""
    daily_equity = _resample_to_daily_equity(result)
    if daily_equity is None or len(daily_equity) < 2:
        return None

    equity = np.array(daily_equity, dtype=np.float64)
    daily_returns = np.diff(equity) / equity[:-1]

    mean_daily = daily_returns.mean()
    std_daily = daily_returns.std(ddof=1) if len(daily_returns) > 1 else 0.0
    if std_daily <= 0:
        return None

    sharpe = (mean_daily - risk_free_rate / TRADING_DAYS_PER_YEAR) / std_daily * np.sqrt(TRADING_DAYS_PER_YEAR)
    return round(float(sharpe), 3)


@dataclass
class ScoredVariant:
    """A variant with its computed score and all metrics."""

    variant: ConfigVariant
    score: float

    # Primary metrics
    catch_rate: float
    false_positive_rate: float
    total_return_pct: float

    # Secondary metrics
    cash_idle_pct: float = 0.0
    win_rate: float = 0.0
    avg_hold_time_minutes: float = 0.0
    max_drawdown_pct: float = 0.0
    signal_quality: float = 0.0
    n_trades: int = 0

    # Split-window Sharpe validation (2026-07-31) -- None/False when the
    # harness didn't run split-window replays for this variant (backward
    # compatible). robust=True only when Sharpe is positive in BOTH
    # halves, matching replay_check.py's established promotion bar -- this
    # is the PRIMARY sort key in score_all(), not folded into `score`,
    # so a tiny-sample lucky variant can never silently outrank a
    # consistently-profitable one on the leaderboard.
    first_half_sharpe: Optional[float] = None
    second_half_sharpe: Optional[float] = None
    robust: bool = False


class VariantScorer:
    """Scores variant results against discovered signals.

    Args:
        catch_weight: Weight for catch rate (default 0.40).
        fp_weight: Weight for false positive rate (default -0.25).
        return_weight: Weight for total return (default 0.35).
        deployment_weight: Weight for deployment efficiency (default 0.10).
    """

    def __init__(
        self,
        catch_weight: float = CATCH_RATE_WEIGHT,
        fp_weight: float = FALSE_POSITIVE_WEIGHT,
        return_weight: float = RETURN_WEIGHT,
        deployment_weight: float = DEPLOYMENT_WEIGHT,
    ):
        self.catch_weight = catch_weight
        self.fp_weight = fp_weight
        self.return_weight = return_weight
        self.deployment_weight = deployment_weight

    def score(self, result: VariantResult) -> ScoredVariant:
        """Compute the composite score for one variant result.

        Args:
            result: VariantResult from the replay engine.

        Returns:
            ScoredVariant with score and all metrics.
        """
        variant = result.variant

        # ── Primary metrics ───────────────────────────────────────────

        # Catch rate: % of discovered signals caught
        catch_rate = result.catch_rate()

        # False positive rate: % of trades not matching any signal
        fp_rate = result.false_positive_rate()

        # Total return from replay
        total_return_pct = result.replay_result.total_return_pct

        # ── Secondary metrics ─────────────────────────────────────────

        trades = result.replay_result.trades

        # Win rate
        win_rate = result.replay_result.win_rate

        # Average hold time
        avg_hold_minutes = self._compute_avg_hold_time(trades)

        # Max drawdown (from equity curve)
        max_dd_pct = self._compute_max_drawdown(result.replay_result.equity_curve)

        # Cash idle percentage
        cash_idle_pct = self._compute_cash_idle_pct(trades, result.replay_result.n_ticks)

        # Signal quality: avg conviction of caught signals
        signal_quality = self._compute_signal_quality(result.caught_signals)

        # ── Normalized return (0-1 scale across all variants) ─────────
        # We normalize at the aggregate level in score_all(), but for
        # single scoring we use a soft sigmoid-like transform
        normalized_return = self._normalize_return(total_return_pct)

        # ── Composite score ───────────────────────────────────────────
        score = (
            self.catch_weight * catch_rate
            + self.fp_weight * fp_rate
            + self.return_weight * normalized_return
        )

        # Clip to a reasonable range
        score = max(-1.0, min(1.0, score))

        # ── Split-window Sharpe (2026-07-31) ───────────────────────────
        # Only computed when the harness ran split replays for this
        # variant (result.first_half_replay_result set) -- see
        # overnight_harness.py Phase 3. robust requires a real Sharpe
        # value (not None) on BOTH halves, both positive -- a variant
        # with too few trades to compute Sharpe on a half is NOT robust
        # by default, matching "insufficient data" being treated as a
        # failure to demonstrate robustness, not a pass.
        first_half_sharpe = None
        second_half_sharpe = None
        robust = False
        if result.first_half_replay_result is not None and result.second_half_replay_result is not None:
            first_half_sharpe = compute_sharpe(result.first_half_replay_result)
            second_half_sharpe = compute_sharpe(result.second_half_replay_result)
            robust = bool(
                first_half_sharpe is not None and second_half_sharpe is not None
                and first_half_sharpe > 0 and second_half_sharpe > 0
            )

        return ScoredVariant(
            variant=variant,
            score=round(score, 4),
            catch_rate=round(catch_rate, 4),
            false_positive_rate=round(fp_rate, 4),
            total_return_pct=round(total_return_pct, 4),
            cash_idle_pct=round(cash_idle_pct, 2),
            win_rate=round(win_rate, 4),
            avg_hold_time_minutes=round(avg_hold_minutes, 1),
            max_drawdown_pct=round(max_dd_pct, 4),
            signal_quality=round(signal_quality, 4),
            n_trades=len(trades),
            first_half_sharpe=first_half_sharpe,
            second_half_sharpe=second_half_sharpe,
            robust=robust,
        )

    def score_all(
        self, results: List[VariantResult]
    ) -> List[ScoredVariant]:
        """Score all variants and rank them.

        Normalizes returns across all variants for fair comparison.

        Args:
            results: List of VariantResult from the replay engine.

        Returns:
            Ranked list of ScoredVariant (best first).
        """
        scored = [self.score(r) for r in results]

        # Normalize returns across all variants (z-score, then scale to 0-1)
        returns = np.array([s.total_return_pct for s in scored])
        if len(returns) > 1 and np.std(returns) > 1e-10:
            z_scores = (returns - np.mean(returns)) / np.std(returns)
            normalized = 1.0 / (1.0 + np.exp(-z_scores))  # sigmoid → [0, 1]
        else:
            normalized = np.full_like(returns, 0.5)

        # Recompute scores with normalized returns
        for i, s in enumerate(scored):
            s.score = (
                self.catch_weight * s.catch_rate
                + self.fp_weight * s.false_positive_rate
                + self.return_weight * float(normalized[i])
            )
            s.score = max(-1.0, min(1.0, round(s.score, 4)))

        # 2026-07-31: robust (Sharpe positive in both split-window halves)
        # is the PRIMARY sort key, score breaks ties within each group --
        # matches replay_check.py's established sort_key=(robust, sharpe)
        # pattern. Without this, overnight-insights.md's Round 5 leaderboard
        # problem repeats: a 1-3 trade variant's z-score-inflated `score`
        # could outrank a variant with a real, consistent edge. Variants
        # scored without split-window data (robust=False by default, see
        # score()) simply sort by score within the non-robust group, same
        # as today -- this is purely additive when split data is present.
        scored.sort(key=lambda s: (s.robust, s.score), reverse=True)
        return scored

    # ── Metric helpers ─────────────────────────────────────────────────

    @staticmethod
    def _compute_avg_hold_time(trades: List[Trade]) -> float:
        """Average hold time in minutes."""
        if not trades:
            return 0.0
        total_minutes = 0.0
        count = 0
        for t in trades:
            if t.exit_time and t.entry_time:
                delta = (t.exit_time - t.entry_time).total_seconds()
                total_minutes += delta / 60.0
                count += 1
        return total_minutes / count if count > 0 else 0.0

    @staticmethod
    def _compute_max_drawdown(equity_curve: np.ndarray) -> float:
        """Maximum drawdown percentage from peak."""
        if len(equity_curve) < 2:
            return 0.0
        peak = np.maximum.accumulate(equity_curve)
        drawdown = (equity_curve - peak) / peak
        return float(abs(np.min(drawdown)) * 100)

    @staticmethod
    def _compute_cash_idle_pct(trades: List[Trade], n_ticks: int) -> float:
        """Estimate cash idle percentage over the replay period."""
        if n_ticks == 0 or len(trades) == 0:
            return 100.0  # all idle if no trades
        # Rough proxy: fraction of ticks with no trade activity
        return 100.0 * (1.0 - min(len(trades) / max(n_ticks, 1), 0.5) * 2)

    @staticmethod
    def _compute_signal_quality(
        caught_signals: List[Any],
    ) -> float:
        """Average conviction of caught signals."""
        if not caught_signals:
            return 0.0
        convictions = [s.conviction for s in caught_signals]
        return float(np.mean(convictions))

    @staticmethod
    def _normalize_return(return_pct: float) -> float:
        """Sigmoid-normalize return to 0-1 scale.

        A 0% return maps to ~0.5, positive returns approach 1.0,
        negative returns approach 0.0.
        """
        return 1.0 / (1.0 + np.exp(-return_pct / 5.0))


def compute_catch_rate(result: VariantResult) -> float:
    """Convenience function: catch rate for a single result."""
    return result.catch_rate()


def compute_false_positive_rate(result: VariantResult) -> float:
    """Convenience function: false positive rate for a single result."""
    return result.false_positive_rate()
