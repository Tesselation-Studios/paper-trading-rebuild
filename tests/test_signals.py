"""Tests for signal engine — SPEC-v3 §3."""

import pytest
import numpy as np

from src.signals import (
    ParamBound,
    SignalParams,
    SignalReport,
    SignalEngine,
    compute_gradient,
    gradient_step,
)

pytestmark = pytest.mark.integration


# ── ParamBound tests ─────────────────────────────────────────────────────────


class TestParamBound:
    def test_clip_within_bounds(self):
        b = ParamBound(default=0.5, min_val=0.0, max_val=1.0)
        assert b.clip(0.3) == 0.3
        assert b.clip(0.0) == 0.0
        assert b.clip(1.0) == 1.0

    def test_clip_outside_bounds(self):
        b = ParamBound(default=0.5, min_val=0.1, max_val=0.9)
        assert b.clip(0.05) == 0.1
        assert b.clip(1.5) == 0.9

    def test_clip_int(self):
        b = ParamBound(default=10, min_val=1, max_val=100, is_int=True)
        assert b.clip(50) == 50
        assert b.clip(0) == 1
        assert b.clip(200) == 100
        assert b.clip(42.7) == 43  # rounds

    def test_epsilon(self):
        b = ParamBound(default=50, min_val=0, max_val=100)
        assert b.epsilon() == 1.0  # 1% of 100

        b2 = ParamBound(default=0.5, min_val=0.2, max_val=1.0)
        assert b2.epsilon() == 0.008  # 1% of 0.8


# ── SignalParams tests ───────────────────────────────────────────────────────


class TestSignalParams:
    def test_defaults(self):
        p = SignalParams()
        assert p.momentum_threshold == 0.55
        assert p.base_size_pct == 0.15
        assert p.max_positions == 5

    def test_bound_lookup(self):
        b = SignalParams.bound("momentum_threshold")
        assert b.min_val == 0.3
        assert b.max_val == 0.9

    def test_bound_unknown_param(self):
        with pytest.raises(KeyError):
            SignalParams.bound("nonexistent_param")

    def test_param_names(self):
        names = SignalParams.param_names()
        assert "momentum_threshold" in names
        assert "rsi_oversold" in names
        assert "stop_loss_pct" in names
        assert len(names) == 24  # 24 tunable params (incl K-Means + legacy regime weights)

    def test_get_set(self):
        p = SignalParams()
        p.set("momentum_threshold", 0.75)
        assert p.get("momentum_threshold") == 0.75

    def test_set_clips(self):
        p = SignalParams()
        p.set("momentum_threshold", 2.0)  # max is 0.9
        assert p.get("momentum_threshold") == 0.9

        p.set("rsi_oversold", 0.0)  # min is 15
        assert p.get("rsi_oversold") == 15.0

    def test_set_int_param(self):
        p = SignalParams()
        p.set("max_positions", 15)  # max is 10
        assert p.get("max_positions") == 10

    def test_clip_all(self):
        p = SignalParams()
        p.momentum_threshold = 5.0  # way out of bounds
        p.stop_loss_pct = -0.5
        p.clip_all()
        assert p.momentum_threshold == 0.9
        assert p.stop_loss_pct == 0.02

    def test_perturb(self):
        p = SignalParams()
        p.set("momentum_threshold", 0.55)
        perturbed = p.perturb("momentum_threshold")
        # Should be different from original
        assert perturbed.get("momentum_threshold") != 0.55
        # Original should be unchanged
        assert p.get("momentum_threshold") == 0.55

    def test_perturb_respects_bounds(self):
        p = SignalParams()
        p.set("weight_high_volatility", 0.4)
        b = SignalParams.bound("weight_high_volatility")
        # max is 1.0, epsilon is ~0.01
        perturbed = p.perturb("weight_high_volatility")
        assert 0.0 <= perturbed.get("weight_high_volatility") <= 1.0

    def test_perturb_custom_epsilon(self):
        p = SignalParams()
        p.set("momentum_threshold", 0.55)
        perturbed = p.perturb("momentum_threshold", epsilon=0.1)
        assert abs(perturbed.get("momentum_threshold") - 0.55) <= 0.15  # clipped

    def test_to_dict(self):
        p = SignalParams()
        d = p.to_dict()
        assert d["momentum_threshold"] == 0.55
        assert d["max_positions"] == 5
        assert len(d) == 24

    def test_from_dict(self):
        d = {"momentum_threshold": 0.8, "stop_loss_pct": 0.03, "max_positions": 3}
        p = SignalParams.from_dict(d)
        assert p.get("momentum_threshold") == 0.8
        assert p.get("stop_loss_pct") == 0.03
        assert p.get("max_positions") == 3
        # Unspecified params keep defaults
        assert p.get("base_size_pct") == 0.15

    def test_from_dict_clips(self):
        d = {"momentum_threshold": 5.0}  # max 0.9
        p = SignalParams.from_dict(d)
        assert p.get("momentum_threshold") == 0.9


# ── SignalEngine tests ───────────────────────────────────────────────────────


def make_price_series(start=100, n=50, noise=0.01, trend=0.001):
    """Helper: synthetic price series with optional trend."""
    rng = np.random.default_rng(42)
    returns = trend + noise * rng.standard_normal(n)
    prices = start * np.exp(np.cumsum(returns))
    return list(prices)


class DummyTick:
    """Minimal tick for testing."""
    def __init__(self, ticker, close, timestamp=None):
        self.ticker = ticker
        self.close = close
        self.timestamp = timestamp or "2024-01-02"


class TestSignalEngine:
    def test_empty_engine(self):
        engine = SignalEngine()
        report = engine.process(DummyTick("AAPL", 150.0))
        assert report.ticker == "AAPL"
        assert report.momentum_score == 0.0
        assert report.momentum_signal == "NEUTRAL"
        assert report.rsi == 50.0  # not enough data
        assert report.volatility == 0.0

    def test_uptrend_produces_bullish_momentum(self):
        params = SignalParams()
        params.momentum_threshold = 0.02  # bypass clip for testing
        engine = SignalEngine(params=params)
        prices = [100 * (1.01 ** i) for i in range(50)]
        for i, p in enumerate(prices):
            report = engine.process(DummyTick("AAPL", p, f"day-{i}"))
        assert report.momentum_signal == "BULLISH"
        assert report.momentum_score > 0.0

    def test_downtrend_produces_bearish_momentum(self):
        params = SignalParams()
        params.momentum_threshold = 0.02  # bypass clip for testing
        engine = SignalEngine(params=params)
        prices = [100 * (0.99 ** i) for i in range(50)]
        for i, p in enumerate(prices):
            report = engine.process(DummyTick("AAPL", p, f"day-{i}"))
        assert report.momentum_signal == "BEARISH"
        assert report.momentum_score < 0.0

    def test_rsi_oversold_below_threshold(self):
        engine = SignalEngine()
        # Strong sustained drop: -2% per day for 20 days after flat period
        prices = [100.0] * 30 + [100 * (0.98 ** i) for i in range(25)]
        for i, p in enumerate(prices):
            report = engine.process(DummyTick("AAPL", p, f"day-{i}"))
        # After a sustained sharp drop, RSI should be well below 50
        assert report.rsi < 50.0

    def test_high_volatility_detected(self):
        engine = SignalEngine()
        rng = np.random.default_rng(7)
        prices = 100 + rng.standard_normal(50) * 5  # high volatility
        for i, p in enumerate(prices):
            report = engine.process(DummyTick("AAPL", float(p), f"day-{i}"))
        assert report.volatility > 0.0

    def test_regime_classification(self):
        engine = SignalEngine()
        # Very strong uptrend: +1.5% per day
        prices = [100 * (1.015 ** i) for i in range(50)]
        for i, p in enumerate(prices):
            report = engine.process(DummyTick("AAPL", p, f"day-{i}"))
        assert report.regime == "TRENDING_UP"
        assert report.regime_confidence > 0.0

    def test_composite_signal_range(self):
        engine = SignalEngine()
        prices = make_price_series(n=60, trend=0.002)
        for i, p in enumerate(prices):
            report = engine.process(DummyTick("AAPL", p, f"day-{i}"))
        assert -1.0 <= report.composite_signal <= 1.0
        assert 0.0 <= report.conviction <= 1.0

    def test_stop_loss_below_price(self):
        engine = SignalEngine()
        report = engine.process(DummyTick("AAPL", 100.0))
        assert report.stop_loss < report.take_profit
        # stop_loss = 100 * (1 - 0.05) = 95
        assert report.stop_loss == 95.0

    def test_take_profit_above_price(self):
        engine = SignalEngine()
        report = engine.process(DummyTick("AAPL", 100.0))
        assert report.take_profit == 115.0  # 100 * 1.15

    def test_recommended_size(self):
        engine = SignalEngine()
        prices = make_price_series(n=60, trend=0.002)
        for i, p in enumerate(prices):
            report = engine.process(DummyTick("AAPL", p, f"day-{i}"))
        # Should be positive and capped
        assert 0.0 < report.recommended_size_pct <= 0.30

    def test_multi_ticker_independence(self):
        params = SignalParams()
        params.momentum_threshold = 0.02  # bypass clip for testing
        engine = SignalEngine(params=params)
        # Strong uptrend AAPL, strong downtrend GOOG
        for i in range(40):
            engine.process(DummyTick("AAPL", 100 * (1.015 ** i), f"a-{i}"))
        for i in range(40):
            engine.process(DummyTick("GOOG", 100 * (0.985 ** i), f"g-{i}"))

        # AAPL should be bullish, GOOG bearish
        aapl = engine.process(DummyTick("AAPL", 100 * (1.015 ** 40)))
        goog = engine.process(DummyTick("GOOG", 100 * (0.985 ** 40)))
        assert aapl.momentum_signal == "BULLISH"
        assert goog.momentum_signal == "BEARISH"

    def test_volume_ratio_computed_when_data_available(self):
        """Volume ratio is computed when tick has volume data."""
        engine = SignalEngine()
        # Feed 22 ticks with increasing volume
        for i in range(22):
            tick = DummyTick("AAPL", 100 + i, f"day-{i}")
            tick.volume = 1_000_000 + i * 100_000
            engine.process(tick)
        # Process with high volume
        tick = DummyTick("AAPL", 122.0, "day-end")
        tick.volume = 3_000_000  # 3x the average
        report = engine.process(tick)
        assert report.volume_ratio is not None
        assert report.volume_ratio > 1.0

    def test_volume_pass_defaults_true_without_volume(self):
        """volume_pass is True when tick has no volume attr (DummyTick)."""
        engine = SignalEngine()
        report = engine.process(DummyTick("AAPL", 100.0))
        assert report.volume_pass is True
        assert report.volume_ratio is None

    def test_volume_bypass_chop_extreme_fear(self):
        """volume_pass is True when MEAN_REVERTING + fear_greed ≤ 30, even with low volume."""
        engine = SignalEngine()
        # Feed flat prices + very low volume to create MEAN_REVERTING regime
        for i in range(22):
            tick = DummyTick("AAPL", 100 + 0.1 * i, f"day-{i}")
            tick.volume = 100_000  # consistently low
            engine.process(tick)
        # Last tick: low volume, CHOPPY regime, Extreme Fear
        tick = DummyTick("AAPL", 102.0, "chop-day")
        tick.volume = 50_000  # very low volume
        report = engine.process(tick, fear_greed=24.0)
        assert report.volume_pass is True  # bypassed due to CHOPPY + Extreme Fear
        assert report.volume_ratio is not None
        assert report.volume_ratio < 1.0  # volume is low, but pass is True

    def test_volume_not_bypassed_normal_fear_greed(self):
        """volume_pass follows actual volume when fear_greed > 30."""
        engine = SignalEngine()
        # Feed flat prices
        for i in range(22):
            tick = DummyTick("AAPL", 100 + 0.1 * i, f"day-{i}")
            tick.volume = 10_000_000  # high avg volume
            engine.process(tick)
        # Last tick: very low volume, fear_greed = 45 (not extreme)
        tick = DummyTick("AAPL", 102.0, "day")
        tick.volume = 1_000_000  # 0.1x avg volume
        report = engine.process(tick, fear_greed=45.0)
        assert report.volume_pass is False  # NOT bypassed, fails volume threshold
        assert report.volume_ratio is not None
        assert report.volume_ratio < 1.2  # below default 1.2 threshold


# ── Gradient descent tests ───────────────────────────────────────────────────


class TestGradientDescent:
    def test_compute_gradient_positive(self):
        """Score improves with higher momentum threshold — gradient positive."""
        params = SignalParams()
        params.set("momentum_threshold", 0.55)

        def scorer(p: SignalParams) -> float:
            # Higher threshold = better score in this fake world
            return p.get("momentum_threshold") * 10

        grad = compute_gradient(params, "momentum_threshold", baseline_score=5.5, scorer=scorer)
        assert grad > 0.0

    def test_compute_gradient_negative(self):
        """Score degrades with higher threshold — gradient negative."""
        params = SignalParams()
        params.set("momentum_threshold", 0.55)

        def scorer(p: SignalParams) -> float:
            return 10 - p.get("momentum_threshold") * 10

        grad = compute_gradient(params, "momentum_threshold", baseline_score=4.5, scorer=scorer)
        assert grad < 0.0

    def test_compute_gradient_scorer_failure(self):
        """When scorer fails, gradient falls back to 0."""
        params = SignalParams()
        params.set("momentum_threshold", 0.55)

        def failing_scorer(p: SignalParams) -> float:
            raise RuntimeError("scorer crashed")

        grad = compute_gradient(params, "momentum_threshold", baseline_score=5.0, scorer=failing_scorer)
        assert grad == 0.0

    def test_gradient_step_adjusts_params(self):
        params = SignalParams()
        params.set("momentum_threshold", 0.55)
        original = params.get("momentum_threshold")

        def scorer(p: SignalParams) -> float:
            return p.get("momentum_threshold") * 10

        new_params, gradients = gradient_step(
            params, scorer, learning_rate=0.1, param_names=["momentum_threshold"],
            record_history=False,
        )

        # Params should have moved
        assert new_params.get("momentum_threshold") != original
        assert "momentum_threshold" in gradients
        assert gradients["momentum_threshold"] != 0.0

    def test_gradient_step_respects_bounds(self):
        params = SignalParams()
        params.set("rsi_oversold", 39.0)  # close to max (40)

        def scorer(p: SignalParams) -> float:
            return p.get("rsi_oversold") * 10  # higher = better, pushes toward max

        new_params, _ = gradient_step(
            params, scorer, learning_rate=1.0,  # aggressive
            param_names=["rsi_oversold"],
            record_history=False,
        )
        # Should be clipped to max (40)
        assert new_params.get("rsi_oversold") <= 40.0

    def test_gradient_step_max_change_respected(self):
        params = SignalParams()
        params.set("momentum_threshold", 0.55)
        # Range is 0.6 (0.9-0.3), max_change_pct=0.05 → max step = 0.03

        def scorer(p: SignalParams) -> float:
            return p.get("momentum_threshold") * 100

        new_params, _ = gradient_step(
            params, scorer, learning_rate=100.0,  # absurdly aggressive
            max_change_pct=0.05,
            param_names=["momentum_threshold"],
            record_history=False,
        )
        change = abs(new_params.get("momentum_threshold") - 0.55)
        assert change <= 0.03 + 0.001  # max step + epsilon tolerance

    def test_gradient_step_scorer_baseline_failure(self):
        params = SignalParams()

        def bad_scorer(p: SignalParams) -> float:
            raise RuntimeError("always fails")

        new_params, gradients = gradient_step(params, bad_scorer, record_history=False)
        # Should return unchanged params, empty gradients
        assert gradients == {}
        assert new_params.get("momentum_threshold") == 0.55
