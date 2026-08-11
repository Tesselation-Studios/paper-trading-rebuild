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
