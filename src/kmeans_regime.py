"""kmeans_regime.py — local K-Means regime classifier archive + live serving.

Phase D of the regime-classifier work (2026-08-10): replaces ml_signal.py's
HMM as get_market_regime's live source, per the offline evaluation in
reports/regime_backtest_SPY_kmeans_2026-08-10.md (Phase C) showing the
K-Means candidate has meaningfully more split-half-significant edge than
the HMM did in Phase B.

Unlike ml_signal.py's HMM (fit/predict on a remote gRPC worker on a Mac),
regime_detector.RegimeDetector is plain local sklearn -- so there's no
download/sanity-check step here like ml_signal.archive_trained_model()'s,
just pickle.dump/pickle.load of an already-in-process-fit model. Mirrors
ml_signal.py's _archive_dir/_current_pointer_path/_prune_old_archives shape
(ml_signal.py:127-219) for a consistent versioned-artifact convention
across both regime models, simplified accordingly.

Usage:
    from src.kmeans_regime import archive_detector, get_kmeans_regime

    # After fitting a fresh RegimeDetector (scripts/retrain_regime_kmeans.py):
    archive_detector(detector, "SPY")

    # Live serving (each tick, via data_bus.py's get_market_regime):
    result = get_kmeans_regime("SPY")
    # {"regime": "momentum_bull", "confidence": 0.71, "details": {...}, "source": "local"}
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PROJECT_SRC = str(Path(__file__).resolve().parent)
if _PROJECT_SRC not in sys.path:
    sys.path.insert(0, _PROJECT_SRC)

import regime_backtest  # noqa: E402
import regime_detector  # noqa: E402

# Daily retrain cadence (unlike the HMM's weekly one -- K-Means is local and
# cheap, see scripts/retrain_regime_kmeans.py), so retention is higher to
# cover a comparable span of history: ~30 versions ~= 6 weeks of weekdays.
_MODEL_ARCHIVE_DIR = Path.home() / ".openclaw" / "gpu-models" / "regime_kmeans"
_MODEL_ARCHIVE_RETENTION = 30


def _archive_dir(symbol: str) -> Path:
    d = _MODEL_ARCHIVE_DIR / symbol
    d.mkdir(parents=True, exist_ok=True)
    return d


def _current_pointer_path(symbol: str) -> Path:
    return _archive_dir(symbol) / "current.json"


def _prune_old_archives(symbol: str, keep: int = None) -> None:
    # keep read from the module global at call time, not bound as a default
    # at def time -- same reasoning as ml_signal._prune_old_archives: lets
    # monkeypatching _MODEL_ARCHIVE_RETENTION (tests, or retention tuning)
    # actually take effect.
    if keep is None:
        keep = _MODEL_ARCHIVE_RETENTION
    versions = sorted(_archive_dir(symbol).glob(f"kmeans_{symbol}_*.pkl"))
    if len(versions) > keep:
        for old in versions[: len(versions) - keep]:
            old.unlink(missing_ok=True)


def archive_detector(detector: "regime_detector.RegimeDetector", symbol: str) -> dict:
    """Persist an already-fit RegimeDetector as the new "current" version
    and prune old ones. No remote download/sanity-check step needed (unlike
    ml_signal.archive_trained_model) -- the model is already fit in-process,
    this is just save + pointer update + prune.

    Returns {"archived": bool, "path": str|None}.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    local_path = _archive_dir(symbol) / f"kmeans_{symbol}_{timestamp}.pkl"

    detector.model_path = str(local_path)
    detector._save()

    _current_pointer_path(symbol).write_text(json.dumps({
        "path": str(local_path),
        "archived_at": timestamp,
    }, indent=2))
    _prune_old_archives(symbol)

    return {"archived": True, "path": str(local_path)}


def list_archived_models(symbol: str) -> list[dict]:
    """Local version history for a symbol's K-Means model, newest first --
    diagnostic/audit use, same spirit as ml_signal.list_archived_models."""
    versions = sorted(_archive_dir(symbol).glob(f"kmeans_{symbol}_*.pkl"), reverse=True)
    return [{"path": str(v), "filename": v.name} for v in versions]


def _current_model_path(symbol: str) -> Optional[str]:
    pointer = _current_pointer_path(symbol)
    if not pointer.exists():
        return None
    try:
        data = json.loads(pointer.read_text())
    except Exception:
        return None
    path = data.get("path")
    if not path or not Path(path).exists():
        return None
    return path


def get_kmeans_regime(symbol: str = "SPY", lookback_bars: int = 90) -> dict:
    """Live-serving equivalent of ml_signal.get_regime(), but local K-Means
    instead of a remote HMM. Loads the current archived RegimeDetector and
    the most recent daily bars from market_data.bars_1d, classifies the
    latest bar via regime_backtest.kmeans_infer_regime (reused, not
    reimplemented -- same function Phase C already built and tested).

    Returns:
        {"regime": "momentum_bull"|"momentum_bear"|"mean_reversion"|
                    "volatility_spike"|"low_vol_drift"|None,
         "confidence": 0.0-1.0, "details": {...}, "source": "local"|"error"}
    """
    model_path = _current_model_path(symbol)
    if model_path is None:
        return {
            "regime": None, "confidence": 0.0, "details": {}, "source": "error",
            "error": f"no archived K-Means model for {symbol} — run scripts/retrain_regime_kmeans.py first",
        }

    detector = regime_detector.RegimeDetector(model_path=model_path)
    # __post_init__ calls self._load() but swallows a failed load (logs a
    # warning, leaves _kmeans None) rather than raising -- check explicitly
    # so a corrupt/truncated archive fails loud here instead of surfacing
    # as an opaque RuntimeError deeper in kmeans_infer_regime/predict().
    if detector._kmeans is None:
        return {
            "regime": None, "confidence": 0.0, "details": {}, "source": "error",
            "error": f"archived K-Means model for {symbol} failed to load ({model_path})",
        }

    try:
        bars_df = regime_backtest.load_daily_bars_from_pg(symbol, min_horizon_bars=0)
    except ValueError as e:
        return {"regime": None, "confidence": 0.0, "details": {}, "source": "error", "error": str(e)}

    window_df = bars_df.tail(lookback_bars)
    return regime_backtest.kmeans_infer_regime(window_df, detector, symbol)
