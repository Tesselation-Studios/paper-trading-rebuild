"""Tests for src/kmeans_regime.py -- the local K-Means archive + live-serving
module (Phase D). No real Postgres/gRPC here -- _MODEL_ARCHIVE_DIR is
monkeypatched to a tmp_path, and regime_backtest.load_daily_bars_from_pg is
monkeypatched to return synthetic data, same spirit as test_regime_backtest.py."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

import kmeans_regime  # noqa: E402
import regime_backtest  # noqa: E402
import regime_detector  # noqa: E402


def _synthetic_daily_bars(n=120, seed=0):
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0, 0.5, n))
    opens = closes + rng.normal(0, 0.1, n)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.2, n))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.2, n))
    volumes = rng.integers(1000, 5000, n).astype(float)
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes})
    df["timestamp"] = pd.date_range("2026-01-01", periods=n, freq="D")
    return df


def _fitted_detector(n=120, seed=0, k=2):
    df = _synthetic_daily_bars(n=n, seed=seed)
    detector = regime_detector.RegimeDetector(k=k, model_path="")
    detector.fit(regime_backtest._df_to_records(df, "SPY"), symbols=["SPY"])
    return detector


@pytest.fixture(autouse=True)
def _isolated_archive_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(kmeans_regime, "_MODEL_ARCHIVE_DIR", tmp_path / "regime_kmeans")


class TestArchiveDetector:
    def test_writes_pickle_and_pointer(self):
        detector = _fitted_detector()
        result = kmeans_regime.archive_detector(detector, "SPY")
        assert result["archived"] is True
        assert Path(result["path"]).exists()

        pointer = kmeans_regime._current_pointer_path("SPY")
        assert pointer.exists()
        assert result["path"] in pointer.read_text()

    def test_archived_model_reloads_and_predicts(self):
        detector = _fitted_detector(seed=3)
        result = kmeans_regime.archive_detector(detector, "SPY")

        reloaded = regime_detector.RegimeDetector(model_path=result["path"])
        assert reloaded._kmeans is not None
        # Same feature names as the original fit -- confirms a real round trip,
        # not just a file existing on disk.
        assert reloaded._feature_names == detector._feature_names


class TestListArchivedModels:
    def test_newest_first(self):
        kmeans_regime.archive_detector(_fitted_detector(seed=1), "SPY")
        kmeans_regime.archive_detector(_fitted_detector(seed=2), "SPY")
        versions = kmeans_regime.list_archived_models("SPY")
        assert len(versions) == 2
        assert versions[0]["filename"] > versions[1]["filename"]  # timestamp-sortable filenames


class TestPruneOldArchives:
    def test_prunes_beyond_retention(self, monkeypatch):
        monkeypatch.setattr(kmeans_regime, "_MODEL_ARCHIVE_RETENTION", 2)
        for i in range(4):
            kmeans_regime.archive_detector(_fitted_detector(seed=i), "SPY")
        versions = kmeans_regime.list_archived_models("SPY")
        assert len(versions) == 2


class TestGetKmeansRegime:
    def test_no_archive_returns_actionable_error(self):
        result = kmeans_regime.get_kmeans_regime("SPY")
        assert result["source"] == "error"
        assert result["regime"] is None
        assert "run scripts/retrain_regime_kmeans.py" in result["error"]

    def test_corrupt_archive_returns_error_not_crash(self):
        archive_dir = kmeans_regime._archive_dir("SPY")
        bad_path = archive_dir / "kmeans_SPY_bad.pkl"
        bad_path.write_bytes(b"not a pickle")
        kmeans_regime._current_pointer_path("SPY").write_text(
            '{"path": "%s", "archived_at": "x"}' % bad_path
        )
        result = kmeans_regime.get_kmeans_regime("SPY")
        assert result["source"] == "error"
        assert "failed to load" in result["error"]

    def test_happy_path_returns_local_regime(self, monkeypatch):
        detector = _fitted_detector(seed=4)
        kmeans_regime.archive_detector(detector, "SPY")

        window = _synthetic_daily_bars(n=90, seed=4)
        monkeypatch.setattr(
            kmeans_regime.regime_backtest, "load_daily_bars_from_pg",
            lambda symbol, min_horizon_bars=None: window,
        )

        result = kmeans_regime.get_kmeans_regime("SPY")
        assert result["source"] == "local"
        assert result["regime"] in regime_detector.REGIME_LABELS.values()
        assert 0.0 <= result["confidence"] <= 1.0

    def test_bars_load_failure_returns_error(self, monkeypatch):
        kmeans_regime.archive_detector(_fitted_detector(seed=5), "SPY")

        def _raise(symbol, min_horizon_bars=None):
            raise ValueError("not enough daily bars")

        monkeypatch.setattr(kmeans_regime.regime_backtest, "load_daily_bars_from_pg", _raise)

        result = kmeans_regime.get_kmeans_regime("SPY")
        assert result["source"] == "error"
        assert "not enough daily bars" in result["error"]


class _FakeKMeansModel:
    """Stands in for a fitted sklearn KMeans -- _assign_labels() only ever
    reads .cluster_centers_, so this is the minimal fake needed to exercise
    it directly without going through a real .fit()."""
    def __init__(self, cluster_centers):
        self.cluster_centers_ = np.array(cluster_centers)


class TestAssignLabelsReachability:
    """Regression test for the k=4 bug: _assign_labels() gives momentum_bull/
    momentum_bear/volatility_spike one cluster each, then splits whatever's
    left between mean_reversion/low_vol_drift. At k=4 there's exactly one
    cluster left over, so those two labels are structurally mutually
    exclusive -- confirmed against the live k=4 model, which never had a
    mean_reversion cluster. At k=5 there are two clusters left over, each
    independently eligible for either label -- this doesn't guarantee both
    appear on real data, but confirms it's no longer structurally
    impossible, using hand-picked, well-separated centroids."""

    def test_k5_can_reach_all_five_labels(self):
        detector = regime_detector.RegimeDetector(k=5, model_path="")
        detector._feature_names = ["SPY_mom_20d", "SPY_rsi_14", "SPY_atr_pct", "SPY_vol_trend"]
        # columns: [mom_20d, rsi_14, atr_pct, vol_trend]
        detector._kmeans = _FakeKMeansModel([
            [0.10, 70, 0.010, 0.0],   # highest momentum -> momentum_bull
            [-0.10, 30, 0.010, 0.0],  # lowest momentum -> momentum_bear
            [0.00, 50, 0.050, 0.0],   # highest ATR -> volatility_spike
            [0.001, 50, 0.001, 0.0],  # near-zero momentum + lowest remaining ATR -> low_vol_drift
            [0.03, 55, 0.020, 0.0],   # moderate momentum/ATR, fails the low-vol threshold -> mean_reversion
        ])
        detector._assign_labels()

        assert len(detector._centroid_labels) == 5
        assert set(detector._centroid_labels.values()) == set(regime_detector.REGIME_LABELS.values())

    def test_k4_structurally_cannot_reach_both_mean_reversion_and_low_vol_drift(self):
        """Documents the bug this session fixed (k=4 -> k=5 as the live
        default) -- kept as a regression guard in case k=4 is ever
        reintroduced as a default without revisiting _assign_labels."""
        detector = regime_detector.RegimeDetector(k=4, model_path="")
        detector._feature_names = ["SPY_mom_20d", "SPY_rsi_14", "SPY_atr_pct", "SPY_vol_trend"]
        detector._kmeans = _FakeKMeansModel([
            [0.10, 70, 0.010, 0.0],   # momentum_bull
            [-0.10, 30, 0.010, 0.0],  # momentum_bear
            [0.00, 50, 0.050, 0.0],   # volatility_spike
            [0.001, 50, 0.001, 0.0],  # the one remaining cluster
        ])
        detector._assign_labels()

        assert len(detector._centroid_labels) == 4
        labels = set(detector._centroid_labels.values())
        assert not {"mean_reversion", "low_vol_drift"}.issubset(labels)
