"""Tests for src/ml_signal.py's pure feature-extraction/labeling logic.

No real gRPC connection or GPU worker here — that's exercised manually
against the live worker (see scripts/retrain_regime.py), not in CI. Just
the deterministic feature math and state-labeling helpers, same style as
tests/test_gpu_client.py."""
import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ml_signal  # noqa: E402


def _synthetic_ohlcv(n=60, seed=0):
    """A plausible-looking OHLCV series — enough rows to survive the
    rolling(20)/pct_change(5) warmup in _extract_features."""
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0, 0.5, n))
    opens = closes + rng.normal(0, 0.1, n)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.2, n))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.2, n))
    volumes = rng.integers(1000, 5000, n).astype(float)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes})


class TestExtractFeatures:
    def test_returns_expected_columns_and_shapes(self):
        df = _synthetic_ohlcv()
        X, details = ml_signal._extract_features(df)
        assert len(X) > 0
        assert all(len(row) == 7 for row in X)
        expected_cols = {"rsi", "rsi_trend", "macd_diff", "volume_trend", "price_velocity", "returns", "volatility"}
        assert set(details.keys()) == expected_cols

    def test_drops_warmup_rows_with_nans(self):
        df = _synthetic_ohlcv(n=60)
        X, _ = ml_signal._extract_features(df)
        # rolling(20) + diff warmup means we lose at least ~20 rows
        assert len(X) < len(df)
        assert all(all(np.isfinite(v) for v in row) for row in X)

    def test_too_few_rows_yields_no_features(self):
        df = _synthetic_ohlcv(n=5)
        X, _ = ml_signal._extract_features(df)
        assert X == []


class TestScale:
    def test_scale_matches_sklearn_transform(self):
        from sklearn.preprocessing import StandardScaler
        X = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
        scaler = StandardScaler().fit(X)
        scaled = ml_signal._scale(X, scaler)
        assert np.allclose(scaled, scaler.transform(np.array(X)))


class TestSubClassify:
    def test_exhausted_when_rsi_high_and_falling_and_returns_negative(self):
        details = {"rsi": 75, "rsi_trend": -1.0, "returns": -0.5}
        assert ml_signal._sub_classify(details) == "EXHAUSTED"

    def test_choppy_when_rsi_not_overbought(self):
        details = {"rsi": 45, "rsi_trend": -1.0, "returns": -0.5}
        assert ml_signal._sub_classify(details) == "CHOPPY"

    def test_choppy_when_rsi_trend_not_falling(self):
        details = {"rsi": 75, "rsi_trend": 0.2, "returns": -0.5}
        assert ml_signal._sub_classify(details) == "CHOPPY"

    def test_choppy_on_missing_details(self):
        assert ml_signal._sub_classify({}) == "CHOPPY"


class TestSustainableStateMeta:
    def test_defaults_to_zero_when_meta_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_SCALER_DIR", tmp_path)
        assert ml_signal._load_sustainable_state("NOPE") == 0

    def test_reads_persisted_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_SCALER_DIR", tmp_path)
        meta_path = ml_signal._meta_path("SPY")
        meta_path.write_text(json.dumps({"sustainable_state": 1}))
        assert ml_signal._load_sustainable_state("SPY") == 1

    def test_falls_back_to_zero_on_corrupt_meta(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_SCALER_DIR", tmp_path)
        meta_path = ml_signal._meta_path("SPY")
        meta_path.write_text("not json")
        assert ml_signal._load_sustainable_state("SPY") == 0


class TestScoreToConfidence:
    """2026-08-10: the old `min(0.92, abs(log_score)/(abs(log_score)+1))`
    transform saturated at 0.92 for essentially any real log_score
    magnitude (confirmed live: confidence was always exactly 0.92 or
    0.644, never anything in between). Fixed by normalizing per-timestep
    before the bounded transform, so the result stops being dominated by
    how many bars happened to be in the inference window."""

    def test_varies_with_window_length_for_same_average_quality(self):
        """Old bug: same per-step fit quality, different window sizes ->
        different (wrongly saturated) confidence. New: normalizing by
        n_steps first means the SAME average per-step log_score gives the
        SAME confidence regardless of window length."""
        avg = -7.5
        short = ml_signal._score_to_confidence(avg * 20, 20)
        long = ml_signal._score_to_confidence(avg * 500, 500)
        assert short == long

    def test_does_not_saturate_at_the_old_fixed_cap(self):
        """The old formula returned exactly 0.92 for any log_score with
        |log_score| > ~11.5 -- i.e. almost always. Confirm a realistic
        multi-bar inference call (large |log_score|) no longer collapses
        to that fixed value."""
        conf = ml_signal._score_to_confidence(-768.14, 100)  # real empirical value, 100-bar window
        assert conf != 0.92
        assert 0.0 < conf < 0.92

    def test_higher_average_log_score_gives_higher_confidence(self):
        """Monotonic: a better per-step fit (higher/less-negative average
        log-likelihood) should never produce lower confidence."""
        better = ml_signal._score_to_confidence(-6.7 * 50, 50)
        worse = ml_signal._score_to_confidence(-10.2 * 50, 50)
        assert better > worse

    def test_anomalous_data_drops_toward_zero(self):
        """Empirically, pure-noise input scores far outside the real-data
        range (~-27/step vs ~-7/step for real SPY bars) -- confidence
        should reflect that clearly, not still read as moderate/high."""
        conf = ml_signal._score_to_confidence(-27.0 * 78, 78)
        assert conf < 0.1

    def test_zero_steps_does_not_raise(self):
        # Guards a hypothetical empty-window call rather than crashing get_regime.
        assert ml_signal._score_to_confidence(-100.0, 0) == ml_signal._score_to_confidence(-100.0, 1)

    def test_result_is_bounded_zero_to_cap(self):
        assert 0.0 <= ml_signal._score_to_confidence(-1000.0, 1) <= 0.92
        assert 0.0 <= ml_signal._score_to_confidence(0.0, 1) <= 0.92


class FakeDownloadClient:
    """Minimal fake for the download_file() surface archive_trained_model()
    needs -- writes a real pickled object to local_path so the sanity
    check has something real to unpickle, same pattern test files
    elsewhere in this suite use for other gRPC clients."""

    def __init__(self, succeed=True, write_obj="hmm-shaped"):
        self.succeed = succeed
        self.write_obj = write_obj
        self.calls = []

    async def download_file(self, remote_path, local_path, job_id="", staging_subdir=""):
        self.calls.append((remote_path, local_path))
        if not self.succeed:
            return False
        import pickle as _pickle
        with open(local_path, "wb") as f:
            _pickle.dump(self.write_obj, f)
        return True


class _FakeHMM:
    """Stands in for a real GaussianHMM for the "looks HMM-shaped" check
    (has predict + means_) without needing hmmlearn in this unit test."""
    def predict(self, X):
        return [0]
    means_ = [[0.0]]


class TestArchiveTrainedModel:
    def test_successful_archive_writes_versioned_file_and_pointer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_MODEL_ARCHIVE_DIR", tmp_path)
        client = FakeDownloadClient(write_obj=_FakeHMM())
        result = asyncio.run(ml_signal.archive_trained_model("SPY", "/remote/hmm_SPY.pkl", client))
        assert result["archived"] is True
        assert Path(result["path"]).exists()
        assert result["path"].endswith(".pkl")

        pointer = json.loads(ml_signal._current_pointer_path("SPY").read_text())
        assert pointer["path"] == result["path"]
        assert pointer["remote_artifact_path"] == "/remote/hmm_SPY.pkl"

    def test_download_failure_does_not_write_pointer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_MODEL_ARCHIVE_DIR", tmp_path)
        client = FakeDownloadClient(succeed=False)
        result = asyncio.run(ml_signal.archive_trained_model("SPY", "/remote/hmm_SPY.pkl", client))
        assert result["archived"] is False
        assert result["error"] is not None
        assert not ml_signal._current_pointer_path("SPY").exists()

    def test_download_raising_is_caught_not_propagated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_MODEL_ARCHIVE_DIR", tmp_path)

        class RaisingClient:
            async def download_file(self, **kwargs):
                raise ConnectionError("simulated worker outage")

        result = asyncio.run(ml_signal.archive_trained_model("SPY", "/remote/hmm_SPY.pkl", RaisingClient()))
        assert result["archived"] is False
        assert "simulated worker outage" in result["error"]

    def test_non_hmm_shaped_download_fails_sanity_check_and_is_removed(self, tmp_path, monkeypatch):
        """A corrupt/truncated transfer, or a file that downloaded fine
        but isn't actually a fitted HMM, must not become "current"."""
        monkeypatch.setattr(ml_signal, "_MODEL_ARCHIVE_DIR", tmp_path)
        client = FakeDownloadClient(write_obj={"not": "an hmm"})
        result = asyncio.run(ml_signal.archive_trained_model("SPY", "/remote/hmm_SPY.pkl", client))
        assert result["archived"] is False
        assert "doesn't look like a fitted HMM" in result["error"]
        # the bad file should not be left behind
        assert list(tmp_path.rglob("*.pkl")) == []

    def test_prunes_beyond_retention(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_MODEL_ARCHIVE_DIR", tmp_path)
        monkeypatch.setattr(ml_signal, "_MODEL_ARCHIVE_RETENTION", 2)
        client = FakeDownloadClient(write_obj=_FakeHMM())
        for _ in range(4):
            asyncio.run(ml_signal.archive_trained_model("SPY", "/remote/hmm_SPY.pkl", client))
        assert len(ml_signal.list_archived_models("SPY")) == 2

    def test_list_archived_models_newest_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_MODEL_ARCHIVE_DIR", tmp_path)
        client = FakeDownloadClient(write_obj=_FakeHMM())
        paths = []
        for _ in range(3):
            r = asyncio.run(ml_signal.archive_trained_model("SPY", "/remote/hmm_SPY.pkl", client))
            paths.append(r["path"])
        listed = [v["path"] for v in ml_signal.list_archived_models("SPY")]
        assert listed == sorted(paths, reverse=True)

    def test_symbols_archived_independently(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_MODEL_ARCHIVE_DIR", tmp_path)
        client = FakeDownloadClient(write_obj=_FakeHMM())
        asyncio.run(ml_signal.archive_trained_model("SPY", "/remote/hmm_SPY.pkl", client))
        asyncio.run(ml_signal.archive_trained_model("QQQ", "/remote/hmm_QQQ.pkl", client))
        assert len(ml_signal.list_archived_models("SPY")) == 1
        assert len(ml_signal.list_archived_models("QQQ")) == 1


class TestGetRegimeNoScaler:
    def test_returns_error_when_scaler_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_SCALER_DIR", tmp_path)
        df = _synthetic_ohlcv()
        result = asyncio.run(ml_signal.get_regime("NOPE", df))
        assert result["source"] == "error"
        assert "No scaler" in result["error"]
