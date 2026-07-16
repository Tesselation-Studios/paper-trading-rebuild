"""Tests for Kairos ML Backtesting Toolkit — SPEC-v3 §15."""

import pytest
from src.kairos_backtest import (
    BacktestResult,
    FeatureEngineer,
    GridSearchResult,
    KairosBacktester,
    MLFeatures,
    _apply_param_overrides,
    _build_default_trader,
)
from src.replay import (
    ReplayResult,
    make_dummy_tick,
    make_uptrend_ticks,
)
from src.signals import SignalEngine, SignalParams
import numpy as np

pytestmark = pytest.mark.integration


class TestKairosBacktester:
    """Core backtesting functionality."""

    def test_run_backtest_produces_trades(self):
        """Backtest should produce at least one trade on uptrend data."""
        kbt = KairosBacktester()
        result = kbt.run_backtest("AAPL", n_ticks=80)
        assert result.total_trades > 0
        assert result.ticker == "AAPL"
        assert result.trader == "kairos"

    def test_run_backtest_returns_metrics(self):
        """Backtest should return full metrics."""
        kbt = KairosBacktester()
        result = kbt.run_backtest("AAPL", n_ticks=80)
        assert result.total_pnl != 0
        assert result.win_rate >= 0.0
        assert result.max_drawdown >= 0.0
        assert isinstance(result.calmar_ratio, float)
        assert isinstance(result.objective, float)

    def test_run_backtest_with_custom_params(self):
        """Custom SignalParams should be respected."""
        kbt = KairosBacktester()
        sp = SignalParams(momentum_threshold=0.15, rsi_oversold=20.0)
        result = kbt.run_backtest("AAPL", n_ticks=80, params=sp)
        assert result.params["momentum_threshold"] == 0.15

    def test_multi_ticker_scan(self):
        """Should scan multiple tickers."""
        kbt = KairosBacktester()
        results = kbt.multi_ticker_scan(["AAPL", "MSFT"], n_ticks=80)
        assert len(results) == 2
        assert results[0].ticker in ("AAPL", "MSFT")
        # Should be sorted by objective descending
        assert results[0].objective >= results[1].objective

    def test_grid_search_finds_best(self):
        """Grid search should rank and return best params."""
        kbt = KairosBacktester()
        grid = kbt.grid_search(
            "AAPL",
            n_ticks=30,
            param_grid={
                "momentum_threshold": [0.2, 0.5],
                "rsi_oversold": [25.0, 30.0],
            },
        )
        assert grid.n_combinations == 4
        assert len(grid.results) == 4
        assert grid.best_result is not None
        assert isinstance(grid.best_params, dict)
        assert "momentum_threshold" in grid.best_params

    def test_grid_search_summary(self):
        """Summary should produce readable output."""
        kbt = KairosBacktester()
        grid = kbt.grid_search("AAPL", n_ticks=30, param_grid={"momentum_threshold": [0.3]})
        summary = grid.summary()
        assert "Grid Search" in summary
        assert "BEST" in summary

    def test_backtest_summary(self):
        """Summary should produce readable output."""
        kbt = KairosBacktester()
        result = kbt.run_backtest("AAPL", n_ticks=80)
        summary = result.summary()
        assert "Backtest" in summary
        assert "AAPL" in summary
        assert "Trades" in summary

    def test_backtest_to_dict(self):
        """Should produce serializable dict."""
        kbt = KairosBacktester()
        result = kbt.run_backtest("AAPL", n_ticks=50)
        d = result.to_dict()
        assert d["ticker"] == "AAPL"
        assert "total_pnl" in d
        assert "objective" in d


class TestGridSearchResult:
    """GridSearchResult edge cases."""

    def test_empty_results(self):
        """Should handle no results gracefully."""
        grid = GridSearchResult(
            ticker="AAPL", trader="kairos", n_ticks=30, n_combinations=0
        )
        assert grid.best_result is None
        assert grid.best_params == {}
        assert "0 combos" in grid.summary()


class TestFeatureEngineer:
    """ML feature computation."""

    def test_computes_features(self):
        """Should compute features for tick sequence."""
        ticks = make_uptrend_ticks("AAPL", n=30)
        fe = FeatureEngineer()
        features = fe.compute_features(ticks)
        assert len(features) == 30
        assert isinstance(features[0], MLFeatures)

    def test_features_contain_required_fields(self):
        """All features should have core fields."""
        ticks = make_uptrend_ticks("AAPL", n=30)
        fe = FeatureEngineer()
        features = fe.compute_features(ticks)
        # Last few ticks should have non-zero values
        last = features[-1]
        assert isinstance(last.momentum_score, float)
        assert isinstance(last.rsi, float)
        assert isinstance(last.close, float)
        assert isinstance(last.regime, str)

    def test_reset_clears_state(self):
        """Reset should clear internal history."""
        ticks = make_uptrend_ticks("AAPL", n=20)
        fe = FeatureEngineer()
        fe.compute_features(ticks)
        fe.reset()
        # After reset, compute features from scratch
        fe2_features = fe.compute_features(ticks)
        assert len(fe2_features) == 20

    def test_mlfeatures_to_dict(self):
        """Should produce serializable dict."""
        ticks = make_uptrend_ticks("AAPL", n=20)
        fe = FeatureEngineer()
        features = fe.compute_features(ticks)
        d = features[-1].to_dict()
        assert "timestamp" in d
        assert "close" in d
        assert "momentum_score" in d

    def test_mlfeatures_to_array(self):
        """Should produce numpy array."""
        ticks = make_uptrend_ticks("AAPL", n=20)
        fe = FeatureEngineer()
        features = fe.compute_features(ticks)
        arr = features[-1].to_array()
        assert isinstance(arr, np.ndarray)
        assert arr.shape[0] == 13  # 13 features


class TestBacktestResultFromReplay:
    """BacktestResult.from_replay factory."""

    def test_from_replay_basic(self):
        """Should build a result from a ReplayResult."""
        ticks = make_uptrend_ticks("AAPL", n=50)
        engine = SignalEngine(SignalParams())
        for t in ticks:
            engine.process(t)
        from src.replay import ReplayHarness
        harness = ReplayHarness()
        trader_fn = _build_default_trader(engine)
        replay = harness.run(ticks, trader_fn)

        result = BacktestResult.from_replay(replay, ticker="TEST", n_ticks=50)
        assert result.ticker == "TEST"
        assert result.total_trades == len(replay.trades)
        assert isinstance(result.calmar_ratio, float)

    def test_from_replay_no_trades(self):
        """Should handle zero trades gracefully."""
        empty_replay = ReplayResult(
            equity_curve=np.array([10000.0]),
            returns=np.array([]),
            trades=[],
            initial_balance=10000.0,
            final_equity=10000.0,
            total_pnl=0.0,
            total_return_pct=0.0,
            n_ticks=10,
            n_decisions=0,
            tickers_seen=["AAPL"],
        )
        result = BacktestResult.from_replay(empty_replay, ticker="TEST", n_ticks=10)
        assert result.total_trades == 0
        assert result.win_rate == 0.0
        assert result.profit_factor == 0.0


class TestApplyParamOverrides:
    """Parameter override function."""

    def test_overrides_momentum_threshold(self):
        base = SignalParams()
        result = _apply_param_overrides(base, {"momentum_threshold": 0.15})
        assert result.momentum_threshold == 0.15
        assert base.momentum_threshold == 0.55  # original unchanged

    def test_overrides_rsi(self):
        base = SignalParams()
        result = _apply_param_overrides(base, {"rsi_oversold": 20.0, "rsi_overbought": 80.0})
        assert result.rsi_oversold == 20.0
        assert result.rsi_overbought == 80.0

    def test_overrides_unknown_key_is_passthrough(self):
        """Unknown keys should just be ignored (passed through)."""
        base = SignalParams()
        result = _apply_param_overrides(base, {"unknown_key": 999.0})
        assert result.momentum_threshold == base.momentum_threshold