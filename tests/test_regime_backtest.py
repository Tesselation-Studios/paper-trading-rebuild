"""Tests for src/regime_backtest.py's pure logic: forward-outcome math,
walk-forward windowing, local-inference/live-confidence parity, and
scoring. No real Postgres or gRPC worker here -- injectable fake DB
connection and monkeypatched ml_signal.get_regime(), same style as
tests/test_ml_signal.py."""
import asyncio
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

import ml_signal  # noqa: E402
import regime_backtest  # noqa: E402


def _synthetic_ohlcv(n=60, seed=0):
    """A plausible-looking OHLCV series -- enough rows to survive the
    rolling(20)/pct_change(5) warmup in ml_signal._extract_features."""
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0, 0.5, n))
    opens = closes + rng.normal(0, 0.1, n)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.2, n))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.2, n))
    volumes = rng.integers(1000, 5000, n).astype(float)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes})


def _synthetic_bars(n=200, seed=0):
    """_synthetic_ohlcv plus a timestamp column -- walk_forward/summarize
    need one, but ml_signal._extract_features doesn't care about extra
    columns."""
    df = _synthetic_ohlcv(n=n, seed=seed)
    df["timestamp"] = pd.date_range("2026-06-01", periods=n, freq="5min")
    return df


class _IdentityScaler:
    """Stands in for a fitted StandardScaler -- ml_signal._scale() calls
    scaler.transform(X).tolist(), and we don't need real scaling to
    exercise the surrounding logic."""
    def transform(self, X):
        return X


class _FakeHMM:
    """Stands in for a real GaussianHMM (predict/score only, no hmmlearn
    dependency in this unit test -- same pattern as test_ml_signal.py's
    _FakeHMM)."""
    def __init__(self, state=0, log_score=-100.0):
        self.state = state
        self.log_score = log_score

    def predict(self, X):
        return np.array([self.state] * len(X))

    def score(self, X):
        return self.log_score


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        self.closed = True


class TestCompressTo5MinBars:
    def test_bucket_dense_market_hours_data_down_to_one_row_per_5min(self):
        # 3 raw rows landing in the same 5-min bucket (simulates the
        # empirically-observed dense market-hours write pattern), each with
        # a different close so the "keep last" rule is actually exercised.
        rows = [
            {"timestamp": pd.Timestamp("2026-06-01 13:30:01"), "open": 10, "high": 11, "low": 9, "close": 10.1, "volume": 100},
            {"timestamp": pd.Timestamp("2026-06-01 13:32:30"), "open": 10.1, "high": 11.2, "low": 9.5, "close": 10.4, "volume": 100},
            {"timestamp": pd.Timestamp("2026-06-01 13:34:59"), "open": 10.4, "high": 11.5, "low": 9.8, "close": 10.6, "volume": 100},
        ]
        df = pd.DataFrame(rows)
        compressed = regime_backtest._compress_to_5min_bars(df)
        assert len(compressed) == 1
        assert compressed.iloc[0]["timestamp"] == pd.Timestamp("2026-06-01 13:30:00")
        assert compressed.iloc[0]["close"] == 10.6  # last snapshot in the bucket wins

    def test_drops_consecutive_duplicate_ohlc(self):
        rows = [
            {"timestamp": pd.Timestamp("2026-06-01 00:00:00"), "open": 10, "high": 10, "low": 10, "close": 10, "volume": 0},
            {"timestamp": pd.Timestamp("2026-06-01 00:05:00"), "open": 10, "high": 10, "low": 10, "close": 10, "volume": 0},
            {"timestamp": pd.Timestamp("2026-06-01 00:10:00"), "open": 11, "high": 12, "low": 10, "close": 11.5, "volume": 100},
        ]
        df = pd.DataFrame(rows)
        compressed = regime_backtest._compress_to_5min_bars(df)
        # second row is a consecutive duplicate of the first -> dropped
        assert len(compressed) == 2
        assert list(compressed["close"]) == [10, 11.5]


class TestLoadBarsFromPg:
    def test_uses_injected_connection_no_real_db(self):
        rows = [
            (pd.Timestamp("2026-06-01") + pd.Timedelta(minutes=5 * i),
             100 + i, 101 + i, 99 + i, 100.5 + i, 1000 + i)
            for i in range(50)
        ]
        conn = _FakeConn(rows)
        df = regime_backtest.load_bars_from_pg("SPY", conn=conn, min_horizon_bars=5)
        assert len(df) == 50
        assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
        assert df["open"].dtype == float
        # caller-owned connection -- load_bars_from_pg must not close it
        assert conn.closed is False

    def test_raises_on_insufficient_rows(self):
        rows = [(pd.Timestamp("2026-06-01"), 100, 101, 99, 100.5, 1000)]
        conn = _FakeConn(rows)
        with pytest.raises(ValueError):
            regime_backtest.load_bars_from_pg("SPY", conn=conn, min_horizon_bars=5)


class TestResolveModelPath:
    def test_raises_actionable_error_when_archive_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_MODEL_ARCHIVE_DIR", tmp_path)
        with pytest.raises(FileNotFoundError) as exc_info:
            regime_backtest.resolve_model_path("SPY")
        assert "scripts/retrain_regime.py" in str(exc_info.value)

    def test_explicit_path_overrides_archive(self, tmp_path):
        p = tmp_path / "custom.pkl"
        p.write_bytes(pickle.dumps(_FakeHMM()))
        resolved = regime_backtest.resolve_model_path("SPY", explicit_path=str(p))
        assert resolved == p

    def test_explicit_missing_path_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            regime_backtest.resolve_model_path("SPY", explicit_path=str(tmp_path / "nope.pkl"))


class TestLoadLocalModel:
    def test_uses_current_pointer_and_scaler(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_MODEL_ARCHIVE_DIR", tmp_path / "archive")
        monkeypatch.setattr(ml_signal, "_SCALER_DIR", tmp_path / "scalers")

        archive_dir = ml_signal._archive_dir("SPY")
        model_path = archive_dir / "hmm_SPY_20260101T000000000000Z.pkl"
        with open(model_path, "wb") as f:
            pickle.dump(_FakeHMM(), f)
        ml_signal._current_pointer_path("SPY").write_text(json.dumps({"path": str(model_path)}))

        scaler_path = ml_signal._scaler_path("SPY")
        with open(scaler_path, "wb") as f:
            pickle.dump(_IdentityScaler(), f)

        loaded = regime_backtest.load_local_model("SPY")
        assert loaded.path == model_path
        assert loaded.is_current is True

    def test_missing_scaler_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_MODEL_ARCHIVE_DIR", tmp_path / "archive")
        monkeypatch.setattr(ml_signal, "_SCALER_DIR", tmp_path / "scalers_never_written")

        archive_dir = ml_signal._archive_dir("SPY")
        model_path = archive_dir / "hmm_SPY_20260101T000000000000Z.pkl"
        with open(model_path, "wb") as f:
            pickle.dump(_FakeHMM(), f)
        ml_signal._current_pointer_path("SPY").write_text(json.dumps({"path": str(model_path)}))

        with pytest.raises(FileNotFoundError):
            regime_backtest.load_local_model("SPY")

    def test_non_current_path_flagged_not_current(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_MODEL_ARCHIVE_DIR", tmp_path / "archive")
        monkeypatch.setattr(ml_signal, "_SCALER_DIR", tmp_path / "scalers")

        archive_dir = ml_signal._archive_dir("SPY")
        current_path = archive_dir / "hmm_SPY_20260102T000000000000Z.pkl"
        old_path = archive_dir / "hmm_SPY_20260101T000000000000Z.pkl"
        for p in (current_path, old_path):
            with open(p, "wb") as f:
                pickle.dump(_FakeHMM(), f)
        ml_signal._current_pointer_path("SPY").write_text(json.dumps({"path": str(current_path)}))

        scaler_path = ml_signal._scaler_path("SPY")
        with open(scaler_path, "wb") as f:
            pickle.dump(_IdentityScaler(), f)

        loaded = regime_backtest.load_local_model("SPY", model_path=str(old_path))
        assert loaded.path == old_path
        assert loaded.is_current is False


class TestLocalInferRegime:
    def test_confidence_matches_score_to_confidence_directly_when_sustainable(self):
        df = _synthetic_ohlcv(n=60, seed=1)
        result = regime_backtest.local_infer_regime(df, _FakeHMM(state=0, log_score=-500.0), _IdentityScaler(), sustainable_state=0)
        X_raw, _ = ml_signal._extract_features(df)
        expected = ml_signal._score_to_confidence(-500.0, len(X_raw))
        assert result["confidence"] == expected
        assert result["regime"] == "SUSTAINABLE"
        assert result["source"] == "local"

    def test_non_sustainable_applies_same_haircut_as_get_regime(self):
        df = _synthetic_ohlcv(n=60, seed=1)
        result = regime_backtest.local_infer_regime(df, _FakeHMM(state=1, log_score=-500.0), _IdentityScaler(), sustainable_state=0)
        X_raw, details = ml_signal._extract_features(df)
        base_conf = ml_signal._score_to_confidence(-500.0, len(X_raw))
        assert result["confidence"] == round(base_conf * 0.7, 3)
        assert result["regime"] == ml_signal._sub_classify(details)

    def test_output_shape_matches_get_regime_contract(self):
        df = _synthetic_ohlcv(n=60, seed=2)
        result = regime_backtest.local_infer_regime(df, _FakeHMM(state=0, log_score=-300.0), _IdentityScaler(), sustainable_state=0)
        assert {"regime", "confidence", "details", "source", "hmm_state", "log_score"} <= set(result.keys())
        assert result["source"] == "local"

    def test_too_few_bars_returns_error_source(self):
        df = _synthetic_ohlcv(n=5, seed=3)
        result = regime_backtest.local_infer_regime(df, _FakeHMM(), _IdentityScaler(), sustainable_state=0)
        assert result["source"] == "error"


class TestComputeForwardOutcome:
    def test_known_values(self):
        df = pd.DataFrame({
            "open":   [10, 10, 10, 10, 10],
            "high":   [10, 12, 15, 11, 10],
            "low":    [10, 9, 8, 9, 10],
            "close":  [10, 11, 14, 10, 9],
            "volume": [100] * 5,
        })
        outcome = regime_backtest.compute_forward_outcome(df, idx=0, horizon_bars=3)
        assert outcome.fwd_return_pct == pytest.approx(0.0)   # close[0]=10 -> close[3]=10
        assert outcome.mfe_pct == pytest.approx(50.0)          # max high in (1..3] = 15 -> +50%
        assert outcome.mae_pct == pytest.approx(-20.0)         # min low in (1..3] = 8 -> -20%

    def test_out_of_range_returns_none(self):
        df = _synthetic_ohlcv(n=10)
        assert regime_backtest.compute_forward_outcome(df, idx=8, horizon_bars=5) is None


class TestWalkForward:
    def test_respects_warmup_and_stride(self):
        df = _synthetic_bars(n=200, seed=5)
        results = regime_backtest.walk_forward(
            df, _FakeHMM(state=0, log_score=-50.0), _IdentityScaler(), sustainable_state=0,
            horizons=(5, 10), lookback_bars=50, warmup_bars=30, stride=10,
        )
        assert not results.empty
        expected_positions = list(range(30, 200 - 10, 10))
        expected_timestamps = sorted(df.iloc[p]["timestamp"] for p in expected_positions)

        for horizon in (5, 10):
            actual = sorted(results.loc[results["horizon_bars"] == horizon, "timestamp"].unique())
            assert list(actual) == [pd.Timestamp(t) for t in expected_timestamps]

    def test_window_respects_lookback_bars_cap(self, monkeypatch):
        df = _synthetic_bars(n=300, seed=7)
        captured_lengths = []

        def spy_infer(window_df, model, scaler, sustainable_state):
            captured_lengths.append(len(window_df))
            return {"regime": "CHOPPY", "confidence": 0.1, "details": {}, "source": "local",
                    "hmm_state": 0, "log_score": -10.0}

        monkeypatch.setattr(regime_backtest, "local_infer_regime", spy_infer)
        regime_backtest.walk_forward(
            df, None, None, sustainable_state=0,
            horizons=(5,), lookback_bars=50, warmup_bars=30, stride=50,
        )
        assert captured_lengths[0] <= 50  # early point: not enough history yet, clamped to 0
        assert max(captured_lengths) == 50  # later points: capped at lookback_bars, never more


class TestSummarize:
    def _results_df(self, **overrides):
        base = {
            "timestamp": pd.date_range("2026-06-01", periods=4, freq="5min"),
            "regime": ["SUSTAINABLE"] * 4,
            "confidence": [0.5, 0.6, 0.55, 0.7],
            "hmm_state": [0, 0, 0, 0],
            "log_score": [-10, -11, -9, -12],
            "horizon_bars": [12, 12, 12, 12],
            "fwd_return_pct": [1.0, -0.5, 0.2, 0.8],
            "mfe_pct": [1.5, 0.1, 0.3, 1.0],
            "mae_pct": [-0.2, -1.0, -0.1, -0.3],
        }
        base.update(overrides)
        return pd.DataFrame(base)

    def test_handles_regime_missing_from_data(self):
        summary = regime_backtest.summarize(self._results_df(), total_bars=1000)
        dist = summary["horizons"][12]["regime_distribution"]
        assert dist["SUSTAINABLE"]["n"] == 4
        assert dist["EXHAUSTED"]["n"] == 0
        assert dist["CHOPPY"]["n"] == 0

    def test_reports_both_raw_and_approx_independent_n(self):
        df = self._results_df(
            regime=["CHOPPY"] * 3 + ["CHOPPY"],
            timestamp=pd.date_range("2026-06-01", periods=4, freq="5min"),
            confidence=[0.4, 0.4, 0.4, 0.4],
            hmm_state=[1, 1, 1, 1],
            log_score=[-20, -20, -20, -20],
            horizon_bars=[100, 100, 100, 100],
            fwd_return_pct=[0.1, -0.1, 0.05, 0.0],
            mfe_pct=[0.2, 0.1, 0.2, 0.1],
            mae_pct=[-0.1, -0.2, -0.1, -0.1],
        )
        summary = regime_backtest.summarize(df, total_bars=1000)
        stats = summary["horizons"][100]
        assert stats["raw_row_count"] == 4
        assert stats["approx_independent_window_count"] == 10  # 1000 // 100

    def test_empty_results_df_returns_empty_summary(self):
        empty = pd.DataFrame(columns=[
            "timestamp", "regime", "confidence", "hmm_state", "log_score",
            "horizon_bars", "fwd_return_pct", "mfe_pct", "mae_pct",
        ])
        summary = regime_backtest.summarize(empty, total_bars=1000)
        assert summary == {"horizons": {}}


class TestParityCheck:
    def test_reports_agreement(self, monkeypatch):
        df = _synthetic_bars(n=100, seed=9)
        model = _FakeHMM(state=0, log_score=-50.0)
        scaler = _IdentityScaler()

        async def fake_get_regime(symbol, window_df, client=None):
            return {"regime": "SUSTAINABLE", "confidence": 0.55, "source": "grpc"}

        monkeypatch.setattr(ml_signal, "get_regime", fake_get_regime)

        results = asyncio.run(regime_backtest.parity_check(
            "SPY", df, model, scaler, sustainable_state=0, n_samples=3,
        ))
        assert len(results) == 3
        for r in results:
            assert r["local_regime"] == "SUSTAINABLE"
            assert r["remote_regime"] == "SUSTAINABLE"
            assert r["agree"] is True
            assert r["confidence_diff"] is not None

    def test_disagreement_detected(self, monkeypatch):
        df = _synthetic_bars(n=100, seed=10)
        model = _FakeHMM(state=1, log_score=-50.0)  # not sustainable_state -> EXHAUSTED/CHOPPY locally
        scaler = _IdentityScaler()

        async def fake_get_regime(symbol, window_df, client=None):
            return {"regime": "SUSTAINABLE", "confidence": 0.55, "source": "grpc"}

        monkeypatch.setattr(ml_signal, "get_regime", fake_get_regime)

        results = asyncio.run(regime_backtest.parity_check(
            "SPY", df, model, scaler, sustainable_state=0, n_samples=2,
        ))
        assert all(r["agree"] is False for r in results)
