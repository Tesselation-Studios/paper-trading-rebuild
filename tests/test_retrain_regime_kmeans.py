"""Tests for scripts/retrain_regime_kmeans.py. No real Alpaca/Postgres --
subprocess.run and regime_backtest.load_daily_bars_from_pg are mocked/
monkeypatched, and the archive dir is redirected to tmp_path (same pattern
as tests/test_kmeans_regime.py)."""
import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import retrain_regime_kmeans as rrk  # noqa: E402

# retrain_regime_kmeans.py imports kmeans_regime via `from src import
# kmeans_regime` (it inserts PROJECT_DIR, not src/, onto sys.path) -- reuse
# that exact module object (rrk.kmeans_regime) rather than a second bare
# `import kmeans_regime`, which would land as a *different* sys.modules
# entry ("kmeans_regime" vs "src.kmeans_regime") and silently defeat the
# _MODEL_ARCHIVE_DIR monkeypatch below.
kmeans_regime = rrk.kmeans_regime


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


def _args(symbol="SPY", k=4):
    return argparse.Namespace(symbol=symbol, k=k)


@pytest.fixture(autouse=True)
def _isolated_archive_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(kmeans_regime, "_MODEL_ARCHIVE_DIR", tmp_path / "regime_kmeans")


class TestRefreshBars:
    def test_shells_out_to_backfill_script_with_symbol(self):
        with patch.object(rrk.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            rc = rrk.refresh_bars("SPY")
        assert rc == 0
        cmd = mock_run.call_args[0][0]
        assert str(rrk.BACKFILL_SCRIPT) in cmd
        assert "--tickers" in cmd and "SPY" in cmd


class TestMain:
    def test_backfill_failure_returns_1(self):
        with patch.object(rrk, "refresh_bars", return_value=1):
            rc = rrk.main(_args())
        assert rc == 1

    def test_insufficient_bars_returns_1(self, monkeypatch):
        monkeypatch.setattr(rrk, "refresh_bars", lambda symbol: 0)

        def _raise(symbol, min_horizon_bars=None):
            raise ValueError("not enough daily bars")

        monkeypatch.setattr(rrk.regime_backtest, "load_daily_bars_from_pg", _raise)
        rc = rrk.main(_args())
        assert rc == 1

    def test_happy_path_archives_and_returns_0(self, monkeypatch):
        bars = _synthetic_daily_bars(n=120, seed=7)
        monkeypatch.setattr(rrk, "refresh_bars", lambda symbol: 0)
        monkeypatch.setattr(rrk.regime_backtest, "load_daily_bars_from_pg", lambda symbol, min_horizon_bars=None: bars)

        rc = rrk.main(_args(k=2))
        assert rc == 0
        versions = kmeans_regime.list_archived_models("SPY")
        assert len(versions) == 1

    def test_fit_failure_returns_1(self, monkeypatch):
        # k too large for the available synthetic rows -> RegimeDetector.fit raises ValueError
        bars = _synthetic_daily_bars(n=60, seed=8)
        monkeypatch.setattr(rrk, "refresh_bars", lambda symbol: 0)
        monkeypatch.setattr(rrk.regime_backtest, "load_daily_bars_from_pg", lambda symbol, min_horizon_bars=None: bars)

        rc = rrk.main(_args(k=50))
        assert rc == 1
