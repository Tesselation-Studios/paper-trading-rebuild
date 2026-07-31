"""Replay engine — extends replay.py with a config variant system.

For each config variant, replays historical bars through Stan's entry gate,
risk manager, position sizing, and exits. Records all trades taken so the
scorer can compare them against discovered signals.

Usage:
    from src.overnight_replay import (
        ConfigVariant,
        ReplayVariantEngine,
        generate_variants,
        load_variant_from_dict,
    )

    engine = ReplayVariantEngine()
    result = engine.run_variant(variant, ticks, discovered_signals)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np

from src.replay import (
    ReplayHarness,
    ReplayResult,
    Tick,
    Portfolio,
    TraderDecision,
)
from src.overnight_discovery import DiscoveredSignal

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Default values for all tunable parameters
DEFAULT_PARAMS: Dict[str, Any] = {
    "rsi_period": 14,
    "rsi_entry_min": 50,
    "rsi_entry_max": 70,
    "volume_min_mult": 2.0,
    "conviction_min": 0.60,
    "catalyst_min": 0.0,
    "ma_period": 20,
    "price_above_ma": True,
    "macd_fast": 12,
    "macd_slow": 26,
    "max_position_pct": 10.0,
    "ceiling_pct": 20.0,
    "trailing_stop_k": 40,
}

# ── Sweep dimensions for Phase 1 ─────────────────────────────────────────────

# Each entry: (param_name, sweep_values, fixed_default)
SWEEP_DIMENSIONS: Dict[str, tuple] = {
    "rsi_period": ("rsi_period", [7, 14, 21], 14),
    "rsi_entry_min": ("rsi_entry_min", [40, 45, 50, 55], 50),
    "rsi_entry_max": ("rsi_entry_max", [60, 65, 70, 75], 70),
    "volume_min_mult": ("volume_min_mult", [1.0, 1.5, 2.0, 3.0], 2.0),
    "conviction_min": ("conviction_min", [0.40, 0.50, 0.60, 0.70], 0.60),
    "catalyst_min": ("catalyst_min", [0.0, 0.3, 0.5], 0.0),
    "ma_period": ("ma_period", [10, 20, 50], 20),
    "price_above_ma": ("price_above_ma", [True, False], True),
    "macd_fast": ("macd_fast", [8, 12, 16], 12),
    "macd_slow": ("macd_slow", [20, 26, 32], 26),
    "max_position_pct": ("max_position_pct", [6, 10, 15, 25], 10.0),
    "ceiling_pct": ("ceiling_pct", [10, 20, 30], 20.0),
}


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class ConfigVariant:
    """A single parameter configuration to test.

    Each variant has a unique id derived from its parameter values.
    """

    variant_id: str
    params: Dict[str, Any]

    def description(self) -> str:
        """Human-readable description of this variant."""
        key_parts = []
        for k, v in self.params.items():
            if k in ("rsi_entry_min", "rsi_entry_max", "volume_min_mult",
                     "conviction_min", "max_position_pct", "macd_fast",
                     "macd_slow", "ma_period", "rsi_period", "ceiling_pct",
                     "trailing_stop_k"):
                key_parts.append(f"{k}={v}")
        return ", ".join(key_parts[:5])


@dataclass
class VariantResult:
    """Output of running one config variant."""

    variant: ConfigVariant
    replay_result: ReplayResult
    caught_signals: List[DiscoveredSignal]
    missed_signals: List[DiscoveredSignal]
    total_signals: int
    caught_count: int
    false_positive_count: int

    # 2026-07-31: optional split-window replay results (first/second half
    # of the tick data by date midpoint), populated by the harness when
    # split-window Sharpe validation is enabled -- see overnight_scorer.py.
    # None for any caller not doing split-window testing (backward compat).
    first_half_replay_result: Optional[ReplayResult] = None
    second_half_replay_result: Optional[ReplayResult] = None

    def catch_rate(self) -> float:
        """Fraction of discovered signals that were caught."""
        if self.total_signals == 0:
            return 0.0
        return self.caught_count / self.total_signals

    def false_positive_rate(self) -> float:
        """Fraction of trades that were false positives."""
        total_trades = len(self.replay_result.trades)
        if total_trades == 0:
            return 0.0
        return self.false_positive_count / total_trades


# ── Replay variant engine ────────────────────────────────────────────────────


class ReplayVariantEngine:
    """Runs replay with variant parameters.

    For each ConfigVariant, the engine:
    1. Computes technical indicators from bar data
    2. Runs the entry gate using variant parameters
    3. Tracks positions, stops, exits
    4. Returns VariantResult with caught/missed signals

    Args:
        initial_balance: Starting cash for replay.
        default_params: Base parameters for dimensions not being swept.
    """

    def __init__(
        self,
        initial_balance: float = 100_000.0,
        default_params: Optional[Dict[str, Any]] = None,
    ):
        self.initial_balance = initial_balance
        self.default_params = dict(DEFAULT_PARAMS)
        if default_params:
            self.default_params.update(default_params)

    def run_variant(
        self,
        variant: ConfigVariant,
        ticks: List[Tick],
        discovered_signals: Optional[List[DiscoveredSignal]] = None,
    ) -> VariantResult:
        """Run one config variant against historical ticks.

        Args:
            variant: Config variant to test.
            ticks: Chronological historical tick data.
            discovered_signals: Ground truth signals (from discovery phase).

        Returns:
            VariantResult with replay output and signal comparison.
        """
        params = dict(self.default_params)
        params.update(variant.params)

        closes = np.array([t.close for t in ticks])
        volumes = np.array([t.volume for t in ticks], dtype=float)

        rsi_arr = self._compute_rsi(closes, int(params["rsi_period"]))
        macd_fast_arr = self._compute_sma(closes, int(params["macd_fast"]))
        macd_slow_arr = self._compute_sma(closes, int(params["macd_slow"]))
        ma_arr = self._compute_sma(closes, int(params["ma_period"]))
        volume_ma20 = self._compute_sma(volumes, 20)

        indicators: Dict[str, Any] = {
            "rsi": rsi_arr,
            "macd_fast": macd_fast_arr,
            "macd_slow": macd_slow_arr,
            "ma": ma_arr,
            "volume_ma20": volume_ma20,
        }

        def trader_fn(tick: Tick, portfolio: Portfolio) -> TraderDecision:
            """Entry gate + exit logic using variant params."""
            ticker = tick.ticker
            idx = next(
                (j for j, t in enumerate(ticks)
                 if t.timestamp == tick.timestamp and t.ticker == tick.ticker),
                -1,
            )
            if idx < 0 or idx >= len(ticks):
                return TraderDecision(ticker=ticker, decision="HOLD", conviction=0.0)

            p = params
            close = tick.close
            vol = tick.volume

            # ── CRITICAL: No look-ahead ──
            # Use indicators from the PREVIOUS bar (idx-1) to avoid look-ahead bias.
            # RSI and MACD at bar idx use close[idx] in their computation, so
            # using them for decisions on bar idx means the indicators "see"
            # the close price before deciding. Indicators from bar idx-1
            # represent what was actually known before this bar's close.
            prev_idx = max(0, idx - 1)
            rsi = indicators["rsi"][prev_idx]
            ma = indicators["ma"][prev_idx]
            macd_f = indicators["macd_fast"][prev_idx]
            macd_s = indicators["macd_slow"][prev_idx]
            vol_ma = indicators["volume_ma20"][prev_idx]

            if any(v is None for v in [rsi, ma, macd_f, macd_s, vol_ma]):
                return TraderDecision(ticker=ticker, decision="HOLD", conviction=0.0)

            volume_ratio = vol / vol_ma if vol_ma > 0 else 0.0
            price_above = close > ma
            macd_bullish = macd_f > macd_s

            # --- EXIT logic ---
            has_pos = ticker in portfolio.positions
            if has_pos:
                pos = portfolio.positions[ticker]
                stop_price = pos.entry_price * 0.95
                if close <= stop_price:
                    return TraderDecision(
                        ticker=ticker, decision="SELL", conviction=1.0,
                        rationale="Stop loss hit", shares=pos.shares,
                    )
                target_price = pos.entry_price * 1.15
                if close >= target_price:
                    return TraderDecision(
                        ticker=ticker, decision="SELL", conviction=1.0,
                        rationale="Take profit hit", shares=pos.shares,
                    )
                if hasattr(pos, "peak_price"):
                    peak = pos.peak_price
                else:
                    peak = close
                    pos.peak_price = peak
                if close > peak:
                    pos.peak_price = close
                trail_pct = 0.04
                if close < pos.peak_price * (1 - trail_pct):
                    return TraderDecision(
                        ticker=ticker, decision="SELL", conviction=0.9,
                        rationale=f"Trailing stop ({trail_pct*100:.0f}%)",
                        shares=pos.shares,
                    )
                return TraderDecision(ticker=ticker, decision="HOLD", conviction=0.5)

            # --- ENTRY gates ---
            rsi_ok = float(p["rsi_entry_min"]) <= rsi <= float(p["rsi_entry_max"])
            vol_ok = volume_ratio >= float(p["volume_min_mult"])
            ma_ok = price_above == p["price_above_ma"] if not p["price_above_ma"] else price_above
            macd_ok = macd_bullish
            catalyst_min = float(p["catalyst_min"])
            catalyst_ok = volume_ratio >= catalyst_min if catalyst_min > 0 else True

            conviction = 0.0
            if rsi_ok:
                conviction += 0.30
            if vol_ok:
                conviction += 0.25
            if ma_ok:
                conviction += 0.15
            if macd_ok:
                conviction += 0.15
            if catalyst_ok:
                conviction += 0.15
            conviction = min(conviction, 1.0)

            if conviction < float(p["conviction_min"]):
                return TraderDecision(ticker=ticker, decision="HOLD", conviction=conviction)
            if not (rsi_ok and vol_ok):
                return TraderDecision(ticker=ticker, decision="HOLD", conviction=conviction)

            max_pos_pct = float(p["max_position_pct"]) / 100.0
            max_cost = portfolio.total_equity * max_pos_pct * conviction
            shares = int(max_cost / close) if close > 0 else 0
            if shares <= 0:
                return TraderDecision(ticker=ticker, decision="HOLD", conviction=conviction)

            return TraderDecision(
                ticker=ticker, decision="BUY", conviction=round(conviction, 4),
                rationale=f"RSI={rsi:.1f} VolR={volume_ratio:.2f} MA={price_above} MACD={macd_bullish}",
                shares=shares,
                signal_override=catalyst_ok and conviction < float(p["conviction_min"]),
            )

        harness = ReplayHarness(
            initial_balance=self.initial_balance,
            max_position_pct=float(params["max_position_pct"]) / 100.0,
        )
        replay_result = harness.run(ticks, trader_fn)

        caught: List[DiscoveredSignal] = []
        missed: List[DiscoveredSignal] = []
        false_positive_count = 0

        if discovered_signals:
            caught_set: set = set()
            for trade in replay_result.trades:
                matched = False
                for signal in discovered_signals:
                    if signal.symbol == trade.ticker:
                        time_diff = abs(
                            (trade.entry_time - signal.timestamp).total_seconds()
                        )
                        if time_diff < 7200:
                            price_diff = abs(
                                (trade.entry_price - signal.price_at_signal)
                                / signal.price_at_signal
                            )
                            if price_diff < 0.05:
                                caught.append(signal)
                                caught_set.add(id(signal))
                                matched = True
                                break
                if not matched:
                    false_positive_count += 1
            missed = [s for s in discovered_signals if id(s) not in caught_set]

        return VariantResult(
            variant=variant,
            replay_result=replay_result,
            caught_signals=caught,
            missed_signals=missed,
            total_signals=len(discovered_signals) if discovered_signals else 0,
            caught_count=len(caught),
            false_positive_count=false_positive_count,
        )

    # ── Indicator helpers ──────────────────────────────────────────────

    @staticmethod
    def _compute_rsi(closes: np.ndarray, period: int = 14) -> List[Optional[float]]:
        """Wilder's RSI for the full array."""
        result: List[Optional[float]] = [None] * len(closes)
        if len(closes) < period + 1:
            return result
        for i in range(period, len(closes)):
            deltas = np.diff(closes[i - period:i + 1])
            gains = np.maximum(deltas, 0)
            losses = np.abs(np.minimum(deltas, 0))
            avg_gain = np.mean(gains)
            avg_loss = np.mean(losses)
            if avg_loss < 1e-10:
                result[i] = 100.0 if avg_gain > 0 else 50.0
            else:
                rs = avg_gain / avg_loss
                result[i] = 100.0 - (100.0 / (1.0 + rs))
        return result

    @staticmethod
    def _compute_sma(values: np.ndarray, period: int) -> List[Optional[float]]:
        """Simple moving average."""
        result: List[Optional[float]] = [None] * len(values)
        if len(values) < period:
            return result
        running_sum = float(np.sum(values[:period]))
        result[period - 1] = running_sum / period
        for i in range(period, len(values)):
            running_sum += values[i] - values[i - period]
            result[i] = running_sum / period
        return result


# ── Variant generators ────────────────────────────────────────────────────────


def _variant_id(params: Dict[str, Any]) -> str:
    """Generate a compact variant id from parameters."""
    parts = []
    for k in sorted(params.keys()):
        parts.append(f"{k}{str(params[k]).replace('.','_')}")
    return "_".join(parts)


def generate_variants(
    n_variants: int = 50,
    fixed_params: Optional[Dict[str, Any]] = None,
    seed: int = 42,
) -> List[ConfigVariant]:
    """Generate config variants using Latin hypercube + random sampling.

    Phase 1 strategy: sweep 3 dimensions at a time with others fixed.
    Uses random sampling for each group to ensure good coverage.

    Args:
        n_variants: Number of variants to generate (default 50).
        fixed_params: Override default params for any dimension.
        seed: Random seed for reproducibility.

    Returns:
        List of ConfigVariant instances.
    """
    rng = np.random.default_rng(seed)
    params_base = dict(DEFAULT_PARAMS)
    if fixed_params:
        params_base.update(fixed_params)

    sweep_groups = [
        ["rsi_period", "rsi_entry_min", "rsi_entry_max"],
        ["volume_min_mult", "conviction_min", "catalyst_min"],
        ["ma_period", "price_above_ma", "macd_fast"],
        ["macd_slow", "max_position_pct", "ceiling_pct"],
    ]

    variants: List[ConfigVariant] = []
    variants_per_group = max(3, n_variants // len(sweep_groups))

    for group in sweep_groups:
        n_samples = min(variants_per_group, 20)
        for _ in range(n_samples):
            params = dict(params_base)
            for dim in group:
                if dim not in SWEEP_DIMENSIONS:
                    continue
                _, sweep_vals, _ = SWEEP_DIMENSIONS[dim]
                idx = int(rng.uniform(0, len(sweep_vals)))
                idx = min(idx, len(sweep_vals) - 1)
                params[dim] = sweep_vals[idx]
            vid = _variant_id(params)
            variants.append(ConfigVariant(variant_id=vid, params=dict(params)))

    # Random full-space samples
    random_count = max(5, n_variants // 4)
    for _ in range(random_count):
        params = dict(params_base)
        for dim_name, (_, sweep_vals, _) in SWEEP_DIMENSIONS.items():
            idx = int(rng.uniform(0, len(sweep_vals)))
            idx = min(idx, len(sweep_vals) - 1)
            params[dim_name] = sweep_vals[idx]
        vid = _variant_id(params)
        variants.append(ConfigVariant(variant_id=vid, params=dict(params)))

    # Always include baseline
    base_vid = _variant_id(params_base)
    variants = [v for v in variants if v.variant_id != base_vid]
    variants.append(ConfigVariant(variant_id="baseline", params=dict(params_base)))

    rng.shuffle(variants)
    return variants[:n_variants]


def load_variant_from_dict(params: Dict[str, Any]) -> ConfigVariant:
    """Create a ConfigVariant from a parameter dict.

    Args:
        params: Dictionary of parameter overrides.

    Returns:
        ConfigVariant with merged parameters.
    """
    merged = dict(DEFAULT_PARAMS)
    merged.update(params)
    vid = _variant_id(merged)
    return ConfigVariant(variant_id=vid, params=merged)


def variant_from_current_live() -> ConfigVariant:
    """Create a variant representing the current live config."""
    return load_variant_from_dict({})
