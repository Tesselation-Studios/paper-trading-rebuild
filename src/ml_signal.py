"""
ml_signal.py — gRPC-backed HMM regime signal for trader agents.

Ported 2026-07-24 from github.com/casper-bot-wodinga/gpu-compute
(orchestrator/ml_signal.py) — real, working infrastructure that was never
wired into the v4 rebuild. Wraps GpuClient with feature extraction (same
7-feature set as MomentumRegimeDetector), scaler management (local), and
3-state output post-processing.

Usage:
    from src.ml_signal import retrain_hmm, get_regime

    # Train (once, or on fresh market data)
    result = await retrain_hmm("SPY", ohlcv_df)

    # Infer (each tick)
    signal = await get_regime("SPY", ohlcv_df)
    # {"regime": "SUSTAINABLE", "confidence": 0.71, "details": {...}, "source": "grpc"}
"""

from __future__ import annotations

import json
import logging
import math
import os
import pickle
import tempfile
from pathlib import Path

logger = logging.getLogger("ml_signal")

# Scalers live locally so inference doesn't need a round-trip model download
_SCALER_DIR = Path.home() / ".openclaw" / "gpu-models" / "scalers"


# ------------------------------------------------------------------
# Feature extraction  (mirrors MomentumRegimeDetector.calculate_features)
# ------------------------------------------------------------------

def _extract_features(df) -> "tuple[list[list[float]], dict]":
    """
    Extract 7 technical features from an OHLCV DataFrame.
    Expects columns: open, high, low, close, volume.
    Returns (feature_rows, last_bar_details).
    """
    df = df.copy()

    # RSI-14
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss))
    df["rsi_trend"] = df["rsi"].diff()

    # MACD 12/26/9
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    df["macd_diff"] = macd - macd.ewm(span=9, adjust=False).mean()

    # Volume trend
    vol_ma = df["volume"].rolling(20).mean()
    df["volume_trend"] = (df["volume"] - vol_ma) / vol_ma

    # Price velocity & returns
    df["returns"] = df["close"].pct_change() * 100
    df["price_velocity"] = df["close"].pct_change(5) * 100

    # Volatility
    df["volatility"] = df["returns"].rolling(20).std()

    df = df.dropna()

    cols = ["rsi", "rsi_trend", "macd_diff", "volume_trend", "price_velocity", "returns", "volatility"]
    if df.empty:
        return [], {}

    X = df[cols].values.tolist()
    last = df.iloc[-1]
    details = {c: float(last[c]) for c in cols}

    return X, details


def _scale(X: list, scaler) -> list:
    import numpy as np
    return scaler.transform(np.array(X)).tolist()


def _sub_classify(details: dict) -> str:
    """CHOPPY vs EXHAUSTED heuristic (same as MomentumRegimeDetector)."""
    if details.get("rsi", 50) > 60 and details.get("rsi_trend", 0) < -0.3 and details.get("returns", 0) < 0:
        return "EXHAUSTED"
    return "CHOPPY"


def _scaler_path(symbol: str) -> Path:
    _SCALER_DIR.mkdir(parents=True, exist_ok=True)
    return _SCALER_DIR / f"hmm_{symbol}_scaler.pkl"


def _meta_path(symbol: str) -> Path:
    _SCALER_DIR.mkdir(parents=True, exist_ok=True)
    return _SCALER_DIR / f"hmm_{symbol}_meta.json"


def _load_sustainable_state(symbol: str) -> int:
    """Which HMM state index means SUSTAINABLE for this symbol's model.

    GaussianHMM.fit() assigns state indices arbitrarily based on the
    training data, not by semantic meaning — there's no guarantee state 0
    is the "good" state. retrain_regime.py determines this via a local
    shadow-fit after training and writes it here; default to 0 (old
    behavior) if the meta file is missing so this degrades safely rather
    than erroring.
    """
    mp = _meta_path(symbol)
    if not mp.exists():
        return 0
    try:
        return int(json.loads(mp.read_text()).get("sustainable_state", 0))
    except Exception:
        return 0


# ------------------------------------------------------------------
# Local model archive (2026-08-10)
# ------------------------------------------------------------------
# retrain_regime.py's own docstring: "the real fitted model stays on the
# Mac / never comes back over the wire" -- true of the SERVING copy, but
# the gRPC protocol already has a working DownloadFile RPC (used nowhere
# in this retrain flow until now) that can pull the trained pickle back
# for a local, versioned audit trail. This does NOT make the worker's
# serving copy swappable -- that needs UploadModel, still an
# UNIMPLEMENTED stub on the worker (gpu-compute/worker/
# grpc_worker_service.py) as of this writing -- but it closes the "zero
# history, zero audit trail, a bad weekly retrain has no safety net"
# gap: every retrain is now inspectable and diffable against prior weeks
# even before real rollback exists.

_MODEL_ARCHIVE_DIR = Path.home() / ".openclaw" / "gpu-models" / "regime"
_MODEL_ARCHIVE_RETENTION = 8  # ~2 months of weekly retrains


def _archive_dir(symbol: str) -> Path:
    d = _MODEL_ARCHIVE_DIR / symbol
    d.mkdir(parents=True, exist_ok=True)
    return d


def _current_pointer_path(symbol: str) -> Path:
    return _archive_dir(symbol) / "current.json"


def _prune_old_archives(symbol: str, keep: int = None) -> None:
    # keep read from the module global at call time, not bound as a
    # default at def time -- so monkeypatching _MODEL_ARCHIVE_RETENTION
    # (tests, or an operator tuning retention) actually takes effect.
    if keep is None:
        keep = _MODEL_ARCHIVE_RETENTION
    versions = sorted(_archive_dir(symbol).glob(f"hmm_{symbol}_*.pkl"))
    if len(versions) > keep:
        for old in versions[: len(versions) - keep]:
            old.unlink(missing_ok=True)


async def archive_trained_model(symbol: str, remote_artifact_path: str, client) -> dict:
    """Download the just-trained model from the worker and keep a
    timestamped local copy. Sanity-checks the download (unpickles, looks
    HMM-shaped) before trusting it as "current" -- a corrupt/truncated
    transfer shouldn't silently become the reference copy. Prunes old
    versions beyond _MODEL_ARCHIVE_RETENTION.

    Returns {"archived": bool, "path": str|None, "error": str|None}.
    """
    from datetime import datetime, timezone

    # Microsecond precision, not just seconds -- a bare-second timestamp
    # collides (silently overwrites the same filename) if this is ever
    # called twice in quick succession, which a real weekly cron won't
    # hit but is trivial to trigger accidentally and shouldn't corrupt
    # the archive if it ever does.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    local_path = _archive_dir(symbol) / f"hmm_{symbol}_{timestamp}.pkl"

    try:
        ok = await client.download_file(remote_path=remote_artifact_path, local_path=str(local_path))
    except Exception as e:
        return {"archived": False, "path": None, "error": f"download_file raised: {e}"}
    if not ok:
        return {"archived": False, "path": None, "error": "download_file returned False (see worker/client logs)"}

    try:
        with open(local_path, "rb") as f:
            obj = pickle.load(f)
        if not hasattr(obj, "predict") or not hasattr(obj, "means_"):
            raise ValueError(f"downloaded artifact doesn't look like a fitted HMM (type={type(obj)})")
    except Exception as e:
        local_path.unlink(missing_ok=True)
        return {"archived": False, "path": None, "error": f"sanity check failed: {e}"}

    _current_pointer_path(symbol).write_text(json.dumps({
        "path": str(local_path),
        "archived_at": timestamp,
        "remote_artifact_path": remote_artifact_path,
    }, indent=2))
    _prune_old_archives(symbol)

    return {"archived": True, "path": str(local_path), "error": None}


def list_archived_models(symbol: str) -> list[dict]:
    """Local version history for a symbol's regime model, newest first --
    diagnostic/audit use (compare a new retrain's parameters against
    prior weeks), and the candidate list a future rollback would choose
    from once UploadModel exists on the worker."""
    versions = sorted(_archive_dir(symbol).glob(f"hmm_{symbol}_*.pkl"), reverse=True)
    return [{"path": str(v), "filename": v.name} for v in versions]


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

async def retrain_hmm(
    symbol: str,
    ohlcv_df,
    n_components: int = 2,
    n_iter: int = 1500,
    client=None,
) -> dict:
    """
    Retrain the HMM for a given symbol on fresh OHLCV data.

    Steps:
      1. Extract 7 features from ohlcv_df
      2. Fit a new StandardScaler, save locally
      3. Scale features, upload JSON to the GPU worker via gRPC
      4. Submit train job, wait for completion
      5. Return result dict

    Args:
        symbol:       Ticker symbol (e.g. "SPY")
        ohlcv_df:     pandas DataFrame with columns: open, high, low, close, volume
        n_components: HMM hidden states (default 2)
        n_iter:       HMM training iterations (default 1500)
        client:       Optional pre-connected GpuClient

    Returns:
        {"symbol": ..., "converged": bool, "n_components": int, "artifact_path": str}
    """
    from sklearn.preprocessing import StandardScaler
    import numpy as np

    _pool = None
    _close = client is None
    if _close:
        from src.gpu_client import WorkerPool
        _pool = WorkerPool.from_env()
        client = await _pool.pick()  # pin to one worker for the whole train sequence
        if client is None:
            return {"error": "no healthy workers available"}

    try:
        X_raw, _ = _extract_features(ohlcv_df)

        # Fit scaler and save locally
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(np.array(X_raw)).tolist()
        sp = _scaler_path(symbol)
        with open(sp, "wb") as f:
            pickle.dump(scaler, f)
        logger.info("Scaler saved to %s", sp)

        # Write scaled training data to temp file
        data = {"features": X_scaled, "lengths": [len(X_scaled)]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump(data, tmp)
            tmp_path = tmp.name

        # Upload to the worker (use the pinned client, not the pool)
        up_resp = await client.upload_file(tmp_path, staging_subdir="train")
        os.unlink(tmp_path)
        if up_resp is None or not up_resp.ok:
            return {"error": f"upload failed: {getattr(up_resp, 'error', 'no response')}"}
        remote_path = up_resp.stored_path

        # Submit training job
        job_id = await client.submit_train(
            model_type="hmm",
            symbol=symbol,
            data_path=remote_path,
            n_components=n_components,
            n_iter=n_iter,
            write_reload_flag=True,
        )
        if job_id is None:
            return {"error": "submit_train returned None"}

        logger.info("Train job %s submitted (symbol=%s)", job_id, symbol)
        result = await client.wait_for_job(job_id, timeout=600.0)

        if result is None:
            return {"error": "train job timed out"}
        if result.phase == 3:  # FAILED
            return {"error": result.error}

        out = json.loads(result.result_json) if result.result_json else {}
        out["scaler_path"] = str(sp)

        if out.get("artifact_path"):
            archive_result = await archive_trained_model(symbol, out["artifact_path"], client)
            out["local_archive"] = archive_result
            if not archive_result["archived"]:
                logger.warning("Model archive failed for %s: %s", symbol, archive_result["error"])

        logger.info("Training complete: %s", out)
        return out

    finally:
        if _pool is not None:
            await _pool.close()


# 2026-08-10: log_score from hmmlearn's GaussianHMM.score() is the summed
# log-likelihood over ALL timesteps passed for inference, not a bounded
# per-call confidence -- its magnitude scales directly with how many bars
# are in the window (confirmed empirically: -102 for a 10-bar window vs
# -20,670 for the full ~2,400-bar history, same fitted model), which is
# why the old `min(0.92, abs(log_score)/(abs(log_score)+1))` transform
# saturated at its 0.92 cap for every window size tested -- confidence was
# effectively always exactly 0.92 (SUSTAINABLE) or 0.644 (0.92*0.7, the
# sub-classification haircut below), never anything in between. Normalizing
# by window length first gives a per-step average log-likelihood that
# stays in a consistent, meaningful range regardless of how many bars a
# given inference call happens to include.
#
# _SCORE_MIDPOINT/_SCORE_SCALE were calibrated empirically (2026-08-10)
# against real cached SPY 5-min bars (shared/cache/bars/SPY.parquet):
# realistic inference-sized windows (12-300 bars) scored -6.7 (best) to
# -10.2 (worst) per-step log-likelihood on this feature set; shuffled
# (temporally-incoherent) data scored similarly (-6.8 to -8.0 -- this
# HMM's per-step score is emission-distribution-dominated, not primarily a
# sequence-coherence signal, so don't over-read it as detecting regime
# persistence specifically); pure random noise scored -27.6, far outside
# the real-data range. The sigmoid below maps the realistic range to
# roughly confidence 0.3-0.9 (so real day-to-day variation is actually
# visible, unlike the old always-0.92/0.644 behavior) and drops sharply
# toward 0 for genuinely anomalous/noise-like data. Revisit these
# constants if the feature set or HMM n_components changes -- they're
# tuned to the current 7-feature, 2-state model.
_SCORE_MIDPOINT = -8.5
_SCORE_SCALE = 2.0


def _score_to_confidence(log_score: float, n_steps: int) -> float:
    """Per-step-normalized, sigmoid-bounded confidence from an HMM's raw
    (unbounded, window-length-dependent) log-likelihood. See the
    module-level comment above _SCORE_MIDPOINT for the empirical
    calibration this is based on."""
    n_steps = max(1, n_steps)
    avg_log_score = log_score / n_steps
    raw_conf = 1.0 / (1.0 + math.exp(-(avg_log_score - _SCORE_MIDPOINT) / _SCORE_SCALE))
    return round(min(0.92, raw_conf), 3)


async def get_regime(
    symbol: str,
    ohlcv_df,
    client=None,
) -> dict:
    """
    Get the current momentum regime for a symbol via gRPC inference.

    Requires retrain_hmm() to have been called at least once for this symbol
    (scaler must exist locally, HMM model must exist on the worker).

    Returns:
        {
            "regime": "SUSTAINABLE" | "EXHAUSTED" | "CHOPPY",
            "confidence": 0.0-1.0,
            "details": {rsi, rsi_trend, macd_diff, ...},
            "source": "grpc",
        }
    """
    sp = _scaler_path(symbol)
    if not sp.exists():
        return {
            "regime": "CHOPPY",
            "confidence": 0.0,
            "details": {},
            "source": "error",
            "error": f"No scaler for {symbol} — run retrain_hmm first",
        }

    with open(sp, "rb") as f:
        scaler = pickle.load(f)

    X_raw, details = _extract_features(ohlcv_df)
    X_scaled = _scale(X_raw, scaler)
    sustainable_state = _load_sustainable_state(symbol)

    _pool = None
    _close = client is None
    if _close:
        from src.gpu_client import WorkerPool
        _pool = WorkerPool.from_env()
        client = await _pool.pick()
        if client is None:
            return {"regime": "CHOPPY", "confidence": 0.0, "details": details, "source": "error", "error": "no healthy workers"}

    try:
        job_id = await client.submit_infer(
            model_name=f"hmm_{symbol}",
            features=X_scaled,
        )
        if job_id is None:
            return {"regime": "CHOPPY", "confidence": 0.0, "details": details, "source": "error", "error": "submit_infer returned None"}

        result = await client.wait_for_job(job_id, timeout=30.0)
        if result is None or result.phase == 3:
            err = getattr(result, "error", "timeout") if result else "timeout"
            return {"regime": "CHOPPY", "confidence": 0.0, "details": details, "source": "error", "error": err}

        out = json.loads(result.result_json)
        last_state = out.get("state", -1)
        log_score = out.get("log_score", 0.0)

        hmm_regime = "SUSTAINABLE" if last_state == sustainable_state else "NOT_SUSTAINABLE"

        confidence = _score_to_confidence(log_score, len(X_scaled))

        if hmm_regime == "SUSTAINABLE":
            final_regime = "SUSTAINABLE"
        else:
            final_regime = _sub_classify(details)
            confidence = round(confidence * 0.7, 3)  # heuristic sub-classification haircut

        return {
            "regime": final_regime,
            "confidence": confidence,
            "details": details,
            "source": "grpc",
            "hmm_state": last_state,
            "log_score": log_score,
        }

    finally:
        if _pool is not None:
            await _pool.close()
