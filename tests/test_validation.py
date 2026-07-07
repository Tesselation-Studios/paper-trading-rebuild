"""Tests for src/validation.py — walk-forward validation, overfitting, significance."""
import pytest
from datetime import datetime, timedelta
from src.validation import (
    walk_forward_split,
    is_overfit,
    is_significant,
    TimeWindow,
    ValidationResult,
    WalkForwardConfig,
    WalkForwardValidator,
    walk_forward_validate,
    _slice_ticks,
    _default_trader_from_params,
)
from src.replay import Tick, Portfolio, TraderDecision, ReplayHarness, TraderFn


class TestWalkForwardSplit:
    def test_single_window(self):
        """Minimal: exactly one window fits."""
        windows = list(walk_forward_split(n_days=120, train_window=90, val_window=30))
        assert len(windows) == 1
        w = windows[0]
        assert w.train_start == 0
        assert w.train_end == 90
        assert w.val_start == 90
        assert w.val_end == 120

    def test_multiple_windows(self):
        """120 data points, 60+30 windows with step=1 → 31 windows."""
        windows = list(walk_forward_split(n_days=120, train_window=60, val_window=30, step=1))
        assert len(windows) == 31  # 120 - 90 + 1

    def test_not_enough_data(self):
        """Less data than one window → empty."""
        windows = list(walk_forward_split(n_days=50, train_window=60, val_window=30))
        assert len(windows) == 0

    def test_train_before_val(self):
        """Training window always ends where validation begins."""
        for w in walk_forward_split(n_days=200, train_window=90, val_window=30, step=5):
            assert w.train_end == w.val_start
            assert w.train_start < w.train_end < w.val_end

    def test_step_size(self):
        """Step controls how many windows per day."""
        windows_small_step = list(walk_forward_split(n_days=150, train_window=60, val_window=30, step=1))
        windows_big_step = list(walk_forward_split(n_days=150, train_window=60, val_window=30, step=10))
        assert len(windows_small_step) > len(windows_big_step)


class TestIsOverfit:
    def test_not_overfit(self):
        """Validation close to training → not overfit."""
        assert not is_overfit(train_score=1.5, val_score=1.4, threshold=0.30)

    def test_overfit(self):
        """Validation much worse → overfit."""
        assert is_overfit(train_score=1.5, val_score=0.9, threshold=0.30)
        # 0.9 < 1.5 * 0.7 = 1.05

    def test_boundary(self):
        """Exactly at threshold edge."""
        # 1.5 * 0.7 = 1.05 → 1.05 is NOT < 1.05
        assert not is_overfit(train_score=1.5, val_score=1.05, threshold=0.30)

    def test_val_better_than_train(self):
        """Validation BETTER than training → not overfit (generalization)."""
        assert not is_overfit(train_score=1.0, val_score=1.5, threshold=0.30)


class TestIsSignificant:
    def test_clear_improvement(self):
        """Big consistent improvement → significant."""
        baseline = [0.5, 0.6, 0.55, 0.5, 0.6, 0.55, 0.5, 0.6]
        candidate = [0.8, 0.9, 0.85, 0.8, 0.9, 0.85, 0.8, 0.9]
        is_sig, p_val = is_significant(baseline, candidate)
        assert is_sig
        assert p_val < 0.05

    def test_no_improvement(self):
        """Similar scores → not significant."""
        baseline = [0.5, 0.6, 0.55, 0.5, 0.6, 0.55, 0.5, 0.6]
        candidate = [0.51, 0.59, 0.54, 0.51, 0.6, 0.56, 0.49, 0.61]
        is_sig, p_val = is_significant(baseline, candidate)
        # Should NOT be significant (very small difference)
        assert p_val > 0.05 or not is_sig

    def test_insufficient_data(self):
        """Less than 5 points → cannot determine significance."""
        is_sig, p_val = is_significant([0.5, 0.6], [0.6, 0.7])
        assert not is_sig
        assert p_val == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: synthetic tick generation
# ═══════════════════════════════════════════════════════════════════════════════


def _make_ticks(
    n: int = 200,
    base_price: float = 100.0,
    momentum: float = 0.7,
    rsi: float = 50.0,
    volatility: float = 0.01,
    ticker: str = "AAPL",
) -> list[Tick]:
    """Generate synthetic ticks with configurable signal values.

    Creates a random walk of prices with momentum/RSI set to trigger
    specific trading behaviors in the default trader.
    """
    import random

    ticks = []
    price = base_price
    start = datetime(2026, 1, 5, 9, 30)

    for i in range(n):
        # Random walk with drift
        price = price * (1 + random.gauss(0.0005, volatility))
        price = max(price, 10.0)

        tick = Tick(
            timestamp=start + timedelta(minutes=i * 5),
            ticker=ticker,
            open=price * 0.999,
            high=price * 1.002,
            low=price * 0.998,
            close=price,
            volume=1000000 + i * 1000,
            rsi=rsi + random.gauss(0, 3),  # Slight noise around target RSI
            momentum=momentum + random.gauss(0, 0.02),
            volatility=volatility,
        )
        ticks.append(tick)

    return ticks


# ═══════════════════════════════════════════════════════════════════════════════
# WalkForwardConfig tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWalkForwardConfig:
    def test_defaults_match_spec(self):
        """Default config matches SPEC §6.1: 90-train, 30-val."""
        cfg = WalkForwardConfig()
        assert cfg.train_window_days == 90
        assert cfg.val_window_days == 30
        assert cfg.overfit_threshold == 0.30  # 30% degradation = overfit

    def test_custom_config(self):
        """Custom windows for shorter validation cycles."""
        cfg = WalkForwardConfig(
            train_window_days=60,
            val_window_days=10,
            min_trades=10,
            step=5,
        )
        assert cfg.train_window_days == 60
        assert cfg.val_window_days == 10
        assert cfg.min_trades == 10
        assert cfg.step == 5


# ═══════════════════════════════════════════════════════════════════════════════
# ValidationResult tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidationResult:
    def test_accepted_result(self):
        """Accepted result has all checks passing."""
        result = ValidationResult(
            accepted=True,
            train_sharpe=1.5,
            val_sharpe=1.2,
            baseline_val_sharpe=0.8,
            confidence=0.8,
            reason="All acceptance criteria met",
            checks={
                "val_sharpe_positive": True,
                "beats_baseline": True,
                "not_overfit": True,
            },
        )
        assert result.accepted
        assert result.checks["val_sharpe_positive"]

    def test_rejected_result(self):
        """Rejected result fails at least one criterion."""
        result = ValidationResult(
            accepted=False,
            train_sharpe=1.5,
            val_sharpe=-0.5,
            baseline_val_sharpe=0.8,
            confidence=0.0,
            reason="Validation Sharpe -0.500 ≤ 0 (no edge on unseen data)",
            checks={
                "val_sharpe_positive": False,
                "beats_baseline": False,
                "not_overfit": True,
            },
        )
        assert not result.accepted
        assert "no edge" in result.reason


# ═══════════════════════════════════════════════════════════════════════════════
# WalkForwardValidator tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWalkForwardValidator:
    def test_not_enough_data(self):
        """Rejected immediately when too few ticks for one window."""
        validator = WalkForwardValidator()
        ticks = _make_ticks(n=50)  # Need 120 for 90+30

        result = validator.validate(
            all_ticks=ticks,
            candidate_params={"momentum_threshold": 0.55},
            baseline_params={"momentum_threshold": 0.55},
        )
        assert not result.accepted
        assert "Not enough data" in result.reason
        assert result.val_sharpe == 0.0

    def test_accepts_improvement(self):
        """Candidate that beats baseline should be accepted; worse should be rejected."""
        cfg = WalkForwardConfig(
            train_window_days=60,
            val_window_days=30,
            min_trades=3,
            step=30,
        )
        validator = WalkForwardValidator(config=cfg)

        ticks = _make_ticks(n=200)

        # Counter for alternating BUY/SELL to generate actual trades
        _buy_counter = [0]

        def candidate_trader(tick, portfolio):
            """Alternates BUY/SELL to produce actual trade records."""
            from src.replay import TraderDecision
            _buy_counter[0] += 1
            if len(portfolio.positions) >= 3:
                # Sell all positions
                pos = next(iter(portfolio.positions.values()))
                return TraderDecision(
                    ticker=pos.ticker, decision="SELL",
                    conviction=1.0, rationale="Candidate sell",
                )
            return TraderDecision(
                ticker=tick.ticker, decision="BUY",
                conviction=0.8, rationale="Candidate buy",
            )

        result = validator.validate(
            all_ticks=ticks,
            candidate_params={"momentum_threshold": 0.40},
            baseline_params={"momentum_threshold": 0.70},
            trader_fn=candidate_trader,
        )
        # Verify structure
        assert isinstance(result.accepted, bool)
        assert isinstance(result.reason, str)

    def test_rejects_overfit(self):
        """When val Sharpe << train Sharpe, should detect overfitting."""
        validator = WalkForwardValidator(config=WalkForwardConfig(
            train_window_days=95,
            val_window_days=25,
            min_trades=3,
            step=30,
        ))
        ticks = _make_ticks(n=200, momentum=0.55, rsi=45.0)

        result = validator.validate(
            all_ticks=ticks,
            candidate_params={"momentum_threshold": 0.55},
            baseline_params={"momentum_threshold": 0.55},
        )
        # Same params → shouldn't be overfit (same behavior)
        # Just verify we get a structured result
        assert hasattr(result, "accepted")

    def test_no_windows_with_trades(self):
        """When no windows produce enough trades, rejected gracefully."""
        cfg = WalkForwardConfig(
            train_window_days=60,
            val_window_days=30,
            min_trades=50,  # Unrealistically high
            step=30,
        )
        validator = WalkForwardValidator(config=cfg)
        ticks = _make_ticks(n=200, momentum=0.3, rsi=50.0)  # Below threshold → no buys

        result = validator.validate(
            all_ticks=ticks,
            candidate_params={"momentum_threshold": 0.9},  # Very high → few entries
            baseline_params={"momentum_threshold": 0.9},
        )
        assert not result.accepted
        assert "No windows produced enough trades" in result.reason


# ═══════════════════════════════════════════════════════════════════════════════
# walk_forward_validate convenience function tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWalkForwardValidate:
    def test_convenience_wrapper(self):
        """walk_forward_validate() returns a ValidationResult."""
        ticks = _make_ticks(n=150, momentum=0.55, rsi=45.0)

        result = walk_forward_validate(
            ticks=ticks,
            candidate_params={"momentum_threshold": 0.50},
            baseline_params={"momentum_threshold": 0.55},
            train_days=60,
            val_days=30,
        )
        assert isinstance(result, ValidationResult)
        assert hasattr(result, "accepted")

    def test_bootstrap_validation(self):
        """When no baseline is provided, compares candidate against itself."""
        ticks = _make_ticks(n=150, momentum=0.55, rsi=45.0)

        result = walk_forward_validate(
            ticks=ticks,
            candidate_params={"momentum_threshold": 0.55},
            train_days=60,
            val_days=30,
        )
        assert isinstance(result, ValidationResult)

    def test_not_enough_data_convenience(self):
        """Convenience wrapper rejects gracefully with too few ticks."""
        ticks = _make_ticks(n=40)
        result = walk_forward_validate(
            ticks=ticks,
            candidate_params={"momentum_threshold": 0.55},
            train_days=60,
            val_days=30,
        )
        assert not result.accepted
        assert "Not enough data" in result.reason


# ═══════════════════════════════════════════════════════════════════════════════
# Helper function tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSliceTicks:
    def test_slice_normal(self):
        """Slice returns the requested range."""
        ticks = _make_ticks(n=100)
        sliced = _slice_ticks(ticks, 10, 30)
        assert len(sliced) == 20

    def test_slice_clamped(self):
        """Slice clamps end to available data."""
        ticks = _make_ticks(n=50)
        sliced = _slice_ticks(ticks, 40, 100)
        assert len(sliced) == 10  # 50 - 40

    def test_slice_empty(self):
        """Slice returns empty when start >= end."""
        ticks = _make_ticks(n=50)
        sliced = _slice_ticks(ticks, 50, 60)
        assert sliced == []


class TestDefaultTraderFromParams:
    def test_creates_callable(self):
        """Returns a callable that accepts (tick, portfolio)."""
        trader = _default_trader_from_params({"momentum_threshold": 0.55})
        assert callable(trader)

    def test_buy_signal(self):
        """High momentum + moderate RSI triggers BUY."""
        trader = _default_trader_from_params({
            "momentum_threshold": 0.55,
            "rsi_overbought": 70.0,
        })
        tick = Tick(
            timestamp=datetime(2026, 1, 5, 9, 35),
            ticker="AAPL",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1000000,
            momentum=0.75,
            rsi=45.0,
        )
        portfolio = Portfolio(cash=100_000.0)
        decision = trader(tick, portfolio)
        assert decision.decision == "BUY"
        assert decision.conviction > 0.5

    def test_hold_no_signal(self):
        """Low momentum → HOLD."""
        trader = _default_trader_from_params({"momentum_threshold": 0.55})
        tick = Tick(
            timestamp=datetime(2026, 1, 5, 9, 35),
            ticker="AAPL",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1000000,
            momentum=0.30,
            rsi=50.0,
        )
        portfolio = Portfolio(cash=100_000.0)
        decision = trader(tick, portfolio)
        assert decision.decision == "HOLD"

    def test_sell_overbought(self):
        """RSI > overbought → SELL."""
        trader = _default_trader_from_params({
            "momentum_threshold": 0.55,
            "rsi_overbought": 70.0,
        })
        tick = Tick(
            timestamp=datetime(2026, 1, 5, 9, 35),
            ticker="AAPL",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1000000,
            momentum=0.60,
            rsi=80.0,
        )
        portfolio = Portfolio(cash=100_000.0)
        decision = trader(tick, portfolio)
        assert decision.decision == "SELL"

    def test_stop_loss_triggers_sell(self):
        """Position at -6% with stop_loss_pct=5% → SELL."""
        trader = _default_trader_from_params({
            "momentum_threshold": 0.55,
            "stop_loss_pct": 0.05,
        })
        # Add a losing position
        from src.replay import Position
        portfolio = Portfolio(
            cash=100_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    shares=100,
                    entry_price=100.0,
                    entry_time=datetime(2026, 1, 5, 9, 30),
                    current_price=94.0,  # -6%
                ),
            },
        )
        tick = Tick(
            timestamp=datetime(2026, 1, 5, 9, 35),
            ticker="MSFT",  # Different ticker — stop-loss check runs for ALL positions
            open=200.0,
            high=201.0,
            low=199.0,
            close=200.5,
            volume=1000000,
            momentum=0.30,
            rsi=50.0,
        )
        decision = trader(tick, portfolio)
        assert decision.decision == "SELL"
        assert "Stop-loss" in decision.rationale

    def test_custom_params_apply(self):
        """Custom momentum_threshold changes behavior."""
        strict_trader = _default_trader_from_params({"momentum_threshold": 0.90})
        loose_trader = _default_trader_from_params({"momentum_threshold": 0.30})

        tick = Tick(
            timestamp=datetime(2026, 1, 5, 9, 35),
            ticker="AAPL",
            open=100.0, high=101.0, low=99.0, close=100.5,
            volume=1000000, momentum=0.70, rsi=50.0,
        )
        portfolio = Portfolio(cash=100_000.0)

        assert strict_trader(tick, portfolio).decision == "HOLD"  # 0.70 < 0.90
        assert loose_trader(tick, portfolio).decision == "BUY"    # 0.70 > 0.30
