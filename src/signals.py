"""Signal engine — the numerical half of the two-speed architecture (SPEC-v3 §3).

Produces structured signal reports from market data. All parameters are
bounded, tunable floats — this is what gradient descent optimizes intraday.

The signal engine does NOT make trading decisions. It computes indicators
and recommendations. The LLM trader reads those and decides.

Key components:
  - SignalParams: Bounded parameter set with defaults and validation
  - SignalEngine: Computes momentum, RSI, volatility signals from Tick data
  - Gradient descent: finite-difference perturbation + replay scoring
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger("signals")

# ── Parameter bounds ─────────────────────────────────────────────────────────


@dataclass
class ParamBound:
    """Bounded parameter with min/max and default."""
    default: float
    min_val: float
    max_val: float
    is_int: bool = False  # If True, values are integers

    def clip(self, value: float) -> float:
        clipped = max(self.min_val, min(self.max_val, value))
        return round(clipped) if self.is_int else clipped

    def epsilon(self) -> float:
        """Perturbation size for finite-difference gradient (1% of range)."""
        return (self.max_val - self.min_val) * 0.01


# ── Signal parameters ────────────────────────────────────────────────────────


@dataclass
class SignalParams:
    """All tunable signal engine parameters with bounds.

    Each field maps to a ParamBound. The defaults are conservative starting
    points — backtested against 2 years of data but NOT optimized.
    """

    # Momentum
    momentum_threshold: float = 0.55       # [0.3, 0.9]
    momentum_lookback: int = 20            # [5, 60]
    momentum_decay: float = 0.85           # [0.5, 0.99]

    # Mean reversion
    rsi_oversold: float = 30.0             # [15, 40]
    rsi_overbought: float = 70.0           # [60, 85]
    bollinger_std: float = 2.0             # [1.0, 3.0]

    # Volatility
    vol_regime_threshold: float = 0.25     # [0.1, 0.5]
    vol_reduction_multiplier: float = 0.7  # [0.3, 1.0]

    # Position sizing
    base_size_pct: float = 0.15            # [0.05, 0.30]
    conviction_multiplier: float = 1.5     # [1.0, 3.0]
    max_positions: int = 5                 # [1, 10]

    # Risk
    stop_loss_pct: float = 0.05            # [0.02, 0.10]
    take_profit_pct: float = 0.15          # [0.05, 0.30]
    trailing_stop_pct: float = 0.03        # [0.01, 0.08]

    # Regime weights
    weight_trending_up: float = 1.0        # [0.2, 2.0]
    weight_trending_down: float = 0.5      # [0.0, 1.5]
    weight_mean_reverting: float = 0.8     # [0.2, 2.0]
    weight_high_volatility: float = 0.4    # [0.0, 1.0]

    # ── Bounds registry (class-level, not a field) ─────────────────────

    _BOUNDS: ClassVar[Dict[str, ParamBound]] = {
        "momentum_threshold": ParamBound(0.55, 0.3, 0.9),
        "momentum_lookback": ParamBound(20, 5, 60, is_int=True),
        "momentum_decay": ParamBound(0.85, 0.5, 0.99),
        "rsi_oversold": ParamBound(30.0, 15.0, 40.0),
        "rsi_overbought": ParamBound(70.0, 60.0, 85.0),
        "bollinger_std": ParamBound(2.0, 1.0, 3.0),
        "vol_regime_threshold": ParamBound(0.25, 0.1, 0.5),
        "vol_reduction_multiplier": ParamBound(0.7, 0.3, 1.0),
        "base_size_pct": ParamBound(0.15, 0.05, 0.30),
        "conviction_multiplier": ParamBound(1.5, 1.0, 3.0),
        "max_positions": ParamBound(5, 1, 10, is_int=True),
        "stop_loss_pct": ParamBound(0.05, 0.02, 0.10),
        "take_profit_pct": ParamBound(0.15, 0.05, 0.30),
        "trailing_stop_pct": ParamBound(0.03, 0.01, 0.08),
        "weight_trending_up": ParamBound(1.0, 0.2, 2.0),
        "weight_trending_down": ParamBound(0.5, 0.0, 1.5),
        "weight_mean_reverting": ParamBound(0.8, 0.2, 2.0),
        "weight_high_volatility": ParamBound(0.4, 0.0, 1.0),
    }

    @classmethod
    def bound(cls, param_name: str) -> ParamBound:
        """Get the bounds for a parameter by name."""
        if param_name not in cls._BOUNDS:
            raise KeyError(f"Unknown parameter: {param_name}")
        return cls._BOUNDS[param_name]

    @classmethod
    def param_names(cls) -> List[str]:
        """List all tunable parameter names."""
        return list(cls._BOUNDS.keys())

    def get(self, param_name: str) -> float:
        """Get a parameter value by name."""
        if not hasattr(self, param_name):
            raise KeyError(f"Unknown parameter: {param_name}")
        return getattr(self, param_name)

    def set(self, param_name: str, value: float) -> None:
        """Set a parameter value, clipped to its bounds."""
        b = SignalParams.bound(param_name)
        setattr(self, param_name, b.clip(value))

    def clip_all(self) -> None:
        """Clip all parameters to their bounds."""
        for name in self.param_names():
            self.set(name, self.get(name))

    def perturb(self, param_name: str, epsilon: Optional[float] = None) -> SignalParams:
        """Return a copy with one parameter perturbed by epsilon (or 1% of range).

        Args:
            param_name: Which parameter to perturb.
            epsilon: Amount to add. Defaults to 1% of the parameter's range.

        Returns:
            New SignalParams instance with the perturbed value (clipped to bounds).
        """
        b = SignalParams.bound(param_name)
        eps = epsilon if epsilon is not None else b.epsilon()
        new_val = b.clip(self.get(param_name) + eps)
        new_params = SignalParams(**{
            f.name: getattr(self, f.name) for f in fields(self)
            if f.name != "_BOUNDS"
        })
        new_params.set(param_name, new_val)
        return new_params

    def to_dict(self) -> Dict[str, float]:
        """Export all parameters as a dict."""
        return {
            name: self.get(name)
            for name in self.param_names()
        }

    @classmethod
    def from_dict(cls, d: Dict[str, float]) -> "SignalParams":
        """Create SignalParams from a dict, clipping to bounds."""
        params = cls()
        for name, value in d.items():
            if name in cls.param_names():
                params.set(name, value)
        return params

    @classmethod
    def relaxed_sweep(cls) -> "SignalParams":
        """Relaxed preset — lower thresholds so sweeps actually produce trades.

        This is the recommended starting point for overnight sweep runs.
        Once trades flow, optimization naturally tightens thresholds toward
        the sweet spot.
        """
        return cls.from_dict({
            "momentum_threshold": 0.25,
            "momentum_lookback": 14,
            "momentum_decay": 0.80,
            "rsi_oversold": 35.0,
            "rsi_overbought": 65.0,
            "bollinger_std": 2.0,
            "vol_regime_threshold": 0.20,
            "vol_reduction_multiplier": 0.6,
            "base_size_pct": 0.12,
            "conviction_multiplier": 1.5,
            "max_positions": 5,
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.15,
            "trailing_stop_pct": 0.03,
            "weight_trending_up": 1.2,
            "weight_trending_down": 0.5,
            "weight_mean_reverting": 1.0,
            "weight_high_volatility": 0.4,
        })

    @classmethod
    def aggressive(cls) -> "SignalParams":
        """Maximum sensitivity — for discovering the edge of overtrading.

        Only use in deep weekend sweeps. This WILL produce trades; the
        question is whether they're profitable.
        """
        return cls.from_dict({
            "momentum_threshold": 0.15,
            "momentum_lookback": 10,
            "momentum_decay": 0.75,
            "rsi_oversold": 40.0,
            "rsi_overbought": 60.0,
            "bollinger_std": 1.5,
            "vol_regime_threshold": 0.15,
            "vol_reduction_multiplier": 0.5,
            "base_size_pct": 0.10,
            "conviction_multiplier": 2.0,
            "max_positions": 5,
            "stop_loss_pct": 0.04,
            "take_profit_pct": 0.12,
            "trailing_stop_pct": 0.02,
            "weight_trending_up": 1.5,
            "weight_trending_down": 0.3,
            "weight_mean_reverting": 1.2,
            "weight_high_volatility": 0.3,
        })

    def relax_thresholds(self, factor: float = 0.2) -> "SignalParams":
        """Return a copy with thresholds relaxed by `factor` of their range.

        A positive factor moves thresholds toward more permissive:
          - momentum_threshold LOWER (easier to trigger)
          - rsi_oversold HIGHER (wider oversold band)
          - rsi_overbought LOWER (wider overbought band)
          - vol_regime_threshold HIGHER (less likely to trigger vol clamp)

        Args:
            factor: Fraction of each param's range to shift (0.2 = 20%).

        Returns:
            New SignalParams with relaxed thresholds.
        """
        p = SignalParams(**{
            f.name: getattr(self, f.name) for f in fields(self)
            if f.name != "_BOUNDS"
        })
        # Relax these specific params toward more permissive
        relax_lower = ["momentum_threshold", "vol_reduction_multiplier",
                        "weight_high_volatility", "weight_trending_down"]
        relax_higher = ["rsi_oversold", "weight_trending_up",
                         "weight_mean_reverting", "vol_regime_threshold"]

        for name in relax_lower:
            if name in self.param_names():
                b = self.bound(name)
                shift = (b.max_val - b.min_val) * factor
                p.set(name, self.get(name) - shift)  # lower = more permissive

        for name in relax_higher:
            if name in self.param_names():
                b = self.bound(name)
                shift = (b.max_val - b.min_val) * factor
                p.set(name, self.get(name) + shift)  # higher = more permissive

        # rsi_overbought: lower = more permissive (we want to detect overbought sooner)
        b = self.bound("rsi_overbought")
        shift = (b.max_val - b.min_val) * factor
        p.set("rsi_overbought", self.get("rsi_overbought") - shift)

        p.clip_all()
        return p


# ── Signal computation ───────────────────────────────────────────────────────


@dataclass
class SignalReport:
    """Structured signal output — what the LLM trader reads each tick."""

    ticker: str
    timestamp: Any  # datetime or str

    # Momentum
    momentum_score: float         # -1.0 to 1.0 (negative = bearish)
    momentum_signal: str          # "BULLISH" | "BEARISH" | "NEUTRAL"

    # Mean reversion
    rsi: float                    # 0-100
    rsi_signal: str               # "OVERSOLD" | "OVERBOUGHT" | "NEUTRAL"

    # Volatility
    volatility: float             # annualized
    volatility_regime: str        # "LOW" | "NORMAL" | "HIGH"

    # Regime
    regime: str                   # "TRENDING_UP" | "TRENDING_DOWN" | "MEAN_REVERTING" | "HIGH_VOLATILITY"
    regime_confidence: float      # 0.0 - 1.0
    regime_weight: float          # Position sizing weight for current regime

    # Position sizing recommendation
    recommended_size_pct: float   # % of equity (already adjusted for regime + conviction)
    max_positions: int

    # Risk
    stop_loss: float              # Stop-loss price
    take_profit: float            # Take-profit price

    # Composite
    composite_signal: float       # -1.0 to 1.0, weighted summary
    conviction: float             # 0.0 - 1.0, how strongly the engine believes in the signal


class SignalEngine:
    """Computes structured signal reports from Tick data.

    The engine maintains a rolling price history to compute indicators.
    It does NOT maintain portfolio state — that's the trader's job.

    Args:
        params: SignalParams with tunable parameters.
        max_history: Max ticks of price history to keep (default 252).
    """

    def __init__(self, params: Optional[SignalParams] = None, max_history: int = 252):
        self.params = params or SignalParams()
        self.max_history = max_history
        self._price_history: Dict[str, List[float]] = {}  # ticker → [closes...]

    def process(self, tick: Any) -> SignalReport:
        """Process a tick and return a signal report.

        Args:
            tick: A Tick-like object with .ticker, .close, .timestamp, etc.

        Returns:
            SignalReport with all computed signals.
        """
        ticker = tick.ticker
        price = tick.close

        # Update price history
        if ticker not in self._price_history:
            self._price_history[ticker] = []
        self._price_history[ticker].append(price)
        if len(self._price_history[ticker]) > self.max_history:
            self._price_history[ticker].pop(0)

        prices = self._price_history[ticker]
        p = self.params

        # ── Momentum ──────────────────────────────────────────────────
        mom_score, mom_signal = self._compute_momentum(prices, p)

        # ── RSI ───────────────────────────────────────────────────────
        rsi = self._compute_rsi(prices, lookback=14)
        rsi_signal = (
            "OVERSOLD" if rsi < p.rsi_oversold
            else "OVERBOUGHT" if rsi > p.rsi_overbought
            else "NEUTRAL"
        )

        # ── Volatility ────────────────────────────────────────────────
        vol = self._compute_volatility(prices)
        vol_regime = (
            "HIGH" if vol > p.vol_regime_threshold
            else "LOW" if vol < p.vol_regime_threshold * 0.5
            else "NORMAL"
        )

        # ── Regime ────────────────────────────────────────────────────
        regime, regime_conf, regime_weight = self._classify_regime(
            prices, mom_score, vol, p
        )

        # ── Position sizing ───────────────────────────────────────────
        # Base size, scaled by regime weight and conviction
        recommended_size = p.base_size_pct * regime_weight
        recommended_size = min(recommended_size, p.base_size_pct * p.conviction_multiplier)

        # ── Risk ──────────────────────────────────────────────────────
        stop_loss = price * (1 - p.stop_loss_pct)
        take_profit = price * (1 + p.take_profit_pct)

        # ── Composite ─────────────────────────────────────────────────
        # Weighted blend of momentum (0.4) + RSI signal (0.3) + regime (0.3)
        rsi_z = (rsi - 50) / 25  # normalize RSI to roughly [-2, 2]
        rsi_component = -np.clip(rsi_z, -1, 1)  # negate: high RSI = bearish bias
        regime_bias = {
            "TRENDING_UP": 0.7,
            "TRENDING_DOWN": -0.7,
            "MEAN_REVERTING": 0.0,
            "HIGH_VOLATILITY": -0.3,
        }.get(regime, 0.0)

        composite = (
            0.40 * mom_score
            + 0.30 * rsi_component
            + 0.30 * regime_bias
        )
        composite = float(np.clip(composite, -1.0, 1.0))

        conviction = abs(composite)

        return SignalReport(
            ticker=ticker,
            timestamp=tick.timestamp,
            momentum_score=round(mom_score, 4),
            momentum_signal=mom_signal,
            rsi=round(rsi, 2),
            rsi_signal=rsi_signal,
            volatility=round(vol, 4),
            volatility_regime=vol_regime,
            regime=regime,
            regime_confidence=round(regime_conf, 4),
            regime_weight=round(regime_weight, 4),
            recommended_size_pct=round(recommended_size, 4),
            max_positions=p.max_positions,
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            composite_signal=round(composite, 4),
            conviction=round(conviction, 4),
        )

    # ── Indicator computations ──────────────────────────────────────────────

    @staticmethod
    def _compute_momentum(prices: List[float], p: SignalParams) -> Tuple[float, str]:
        """Exponential weighted momentum score."""
        if len(prices) < p.momentum_lookback:
            return 0.0, "NEUTRAL"

        recent = np.array(prices[-p.momentum_lookback:])
        if len(recent) < 2:
            return 0.0, "NEUTRAL"

        # Exponential weighted returns
        weights = np.exp(-p.momentum_decay * np.arange(len(recent) - 1, -1, -1))
        weighted_avg = np.average(recent, weights=weights)

        # Score = normalized position relative to weighted average
        current = recent[-1]
        if weighted_avg > 0:
            score = (current - weighted_avg) / weighted_avg
        else:
            score = 0.0

        # Scale to [-1, 1] via threshold
        threshold = p.momentum_threshold
        scaled = float(np.clip(score / threshold, -1.0, 1.0))

        signal = "BULLISH" if scaled > 0.2 else "BEARISH" if scaled < -0.2 else "NEUTRAL"
        return scaled, signal

    @staticmethod
    def _compute_rsi(prices: List[float], lookback: int = 14) -> float:
        """Wilder's RSI (0-100)."""
        if len(prices) < lookback + 1:
            return 50.0

        deltas = np.diff(prices[-lookback - 1:])
        gains = np.maximum(deltas, 0)
        losses = np.abs(np.minimum(deltas, 0))

        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)

        if avg_loss < 1e-10:
            return 100.0 if avg_gain > 0 else 50.0

        rs = avg_gain / avg_loss
        return float(100.0 - (100.0 / (1.0 + rs)))

    @staticmethod
    def _compute_volatility(prices: List[float]) -> float:
        """Annualized volatility from daily returns."""
        if len(prices) < 5:
            return 0.0
        returns = np.diff(np.log(prices))
        return float(np.std(returns, ddof=1) * np.sqrt(252))

    @staticmethod
    def _classify_regime(
        prices: List[float],
        momentum: float,
        volatility: float,
        p: SignalParams,
    ) -> Tuple[str, float, float]:
        """Classify current regime and return weight.

        Returns:
            (regime_name, confidence, position_weight)
        """
        # Trend detection: linear regression slope over recent window
        if len(prices) >= 20:
            recent = np.array(prices[-20:])
            x = np.arange(len(recent))
            slope, _ = np.polyfit(x, recent, 1)
            # Normalize slope to a "trendiness" score
            trend_strength = slope / (np.mean(recent) + 1e-10) * 100
        else:
            trend_strength = 0.0

        if volatility > p.vol_regime_threshold:
            regime = "HIGH_VOLATILITY"
            weight = p.weight_high_volatility
            confidence = min(volatility / (p.vol_regime_threshold * 2), 1.0)

        elif abs(trend_strength) > 0.3:  # stronger trend
            if trend_strength > 0:
                regime = "TRENDING_UP"
                weight = p.weight_trending_up
            else:
                regime = "TRENDING_DOWN"
                weight = p.weight_trending_down
            confidence = min(abs(trend_strength) / 2.0, 1.0)

        else:
            regime = "MEAN_REVERTING"
            weight = p.weight_mean_reverting
            confidence = 0.5 + abs(momentum) * 0.5  # higher momentum = less confident in mean reversion

        return regime, confidence, weight


# ── Gradient descent ─────────────────────────────────────────────────────────


def compute_gradient(
    params: SignalParams,
    param_name: str,
    baseline_score: float,
    scorer: Any,  # Callable[[SignalParams], float] — replays with params, returns score
    epsilon: Optional[float] = None,
) -> float:
    """Finite-difference gradient for one parameter.

    Args:
        params: Current signal parameters.
        param_name: Which parameter to differentiate.
        baseline_score: Score at current parameter values.
        scorer: Function that takes SignalParams → float score.
        epsilon: Perturbation size (default: 1% of param range).

    Returns:
        Approximate gradient: (score_up - score_down) / (2 * epsilon)
    """
    b = SignalParams.bound(param_name)
    eps = epsilon if epsilon is not None else b.epsilon()

    up_params = params.perturb(param_name, eps)
    down_params = params.perturb(param_name, -eps)

    try:
        score_up = scorer(up_params)
    except Exception as e:
        log.warning("Scorer failed for %s +eps: %s — using baseline", param_name, e)
        score_up = baseline_score

    try:
        score_down = scorer(down_params)
    except Exception as e:
        log.warning("Scorer failed for %s -eps: %s — using baseline", param_name, e)
        score_down = baseline_score

    if abs(eps) < 1e-12:
        return 0.0

    return float((score_up - score_down) / (2 * eps))


def gradient_step(
    params: SignalParams,
    scorer: Any,
    learning_rate: float = 0.01,
    max_change_pct: float = 0.05,
    param_names: Optional[List[str]] = None,
) -> Tuple[SignalParams, Dict[str, float]]:
    """Run one gradient descent step across all parameters.

    Args:
        params: Current parameters (modified in place).
        scorer: SignalParams → float score function.
        learning_rate: Step size (default 0.01).
        max_change_pct: Max parameter change as fraction of its range.
        param_names: Which params to optimize (default: all).

    Returns:
        (updated_params, gradients_dict)
    """
    names = param_names or SignalParams.param_names()
    gradients: Dict[str, float] = {}

    try:
        baseline = scorer(params)
    except Exception as e:
        log.warning("Scorer failed for baseline: %s — skipping gradient step", e)
        return params, {}

    for name in names:
        grad = compute_gradient(params, name, baseline, scorer)
        gradients[name] = grad

        # Apply gradient
        b = SignalParams.bound(name)
        max_step = (b.max_val - b.min_val) * max_change_pct
        step = learning_rate * grad
        step = float(np.clip(step, -max_step, max_step))

        new_val = b.clip(params.get(name) + step)
        params.set(name, new_val)

    return params, gradients
