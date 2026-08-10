"""Offline backtest-validation harness for src.ml_signal's HMM regime
classifier (Phase B, 2026-08-10).

Replays historical 5-min bars through the classifier and scores its regime
calls against realized forward returns -- nobody had ever validated the
live classifier's actual calls against what price did afterward; the Sharpe
numbers historically cited for "regime-gated entries" test an unrelated
proxy (a CHOPPY_VOL_THRESHOLD daily-vol heuristic in a different repo),
never the real HMM.

Local inference by default, not per-window gRPC: ml_signal.get_regime()
does one network round-trip per call with no batch-many-windows RPC, so
replaying thousands of historical windows that way would hammer the remote
Mac worker. Confirmed neither training (gpu-compute worker/job_manager.py
_train_hmm) nor inference (_run_inference) touches GPU/MPS at all -- both
are plain hmmlearn/numpy CPU calls -- so running locally isn't taking
anything away from GPU-reserved work; the Mac was never using its GPU for
this model. local_infer_regime() reuses the real functions from ml_signal
(_extract_features, _scale, _sub_classify, _score_to_confidence) so the
local and live paths can't silently drift apart. Use parity_check() to
spot-verify that empirically against the live worker.

Usage:
    python3 scripts/backtest_regime.py --symbol SPY
"""
from __future__ import annotations

import json
import logging
import pickle
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_PROJECT_SRC = str(Path(__file__).resolve().parent)
if _PROJECT_SRC not in sys.path:
    sys.path.insert(0, _PROJECT_SRC)

import ml_signal  # noqa: E402
from db.connection import get_connection  # noqa: E402
from metrics import compute_win_rate  # noqa: E402
from validation import is_significant  # noqa: E402

logger = logging.getLogger("regime_backtest")

WARMUP_BARS = 30
DEFAULT_HORIZONS = (12, 78, 192)  # ~1hr, conventional 6.5hr session, measured 192-bars/day
DEFAULT_LOOKBACK_BARS = 780  # ~1 week -- production passes the whole cached history unbounded;
                              # this is a deliberate, documented divergence, noted in reports.
DEFAULT_STRIDE = 12

_REGIME_LABELS = ("SUSTAINABLE", "EXHAUSTED", "CHOPPY")
_CONFIDENCE_BUCKETS = ((0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.93))


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _compress_to_5min_bars(df: pd.DataFrame) -> pd.DataFrame:
    """market_data.bars_5min isn't actually uniformly 5-min-spaced -- during
    regular market hours it's written far more often than every 5 minutes
    (confirmed empirically against real SPY data: a steady ~12 rows/hour
    overnight vs 250-500+ rows/hour during market hours), so raw rows can't
    be treated as bars directly or the "N bars" horizons mean wildly
    different amounts of wall-clock time depending on time of day. Floors
    to a 5-min bucket, keeps the last snapshot per bucket, and drops
    consecutive duplicate OHLC rows (quiet-period polling repeats the same
    price). Single-symbol adaptation of the same bucket-and-dedupe approach
    already used by nightly_replay.compress_to_5min_bars() for this same
    table."""
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["bucket"] = df["timestamp"].dt.floor("5min")
    last = df.groupby("bucket", as_index=False).last()
    is_dup = (last["close"].diff().abs() < 1e-6) & (last["open"].diff().abs() < 1e-6)
    compressed = last[~is_dup].copy()
    compressed = compressed.drop(columns=["timestamp"]).rename(columns={"bucket": "timestamp"})
    return compressed.sort_values("timestamp").reset_index(drop=True)


def load_bars_from_pg(
    symbol: str,
    start=None,
    end=None,
    conn=None,
    min_horizon_bars: Optional[int] = None,
) -> pd.DataFrame:
    """Read-only load of market_data.bars_5min for one symbol, bucketed to
    genuine 5-min bars (see _compress_to_5min_bars) and sorted ascending by
    timestamp. `conn` is injectable (a real psycopg2 connection, or a fake
    with a matching cursor()/execute()/fetchall() surface) so tests never
    touch the real DB. Raises ValueError if there aren't enough bars left
    after compression to run even one walk-forward evaluation."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        cur = conn.cursor()
        sql = (
            "SELECT timestamp, open, high, low, close, volume "
            "FROM market_data.bars_5min WHERE symbol = %s"
        )
        params = [symbol]
        if start is not None:
            sql += " AND timestamp >= %s"
            params.append(start)
        if end is not None:
            sql += " AND timestamp <= %s"
            params.append(end)
        sql += " ORDER BY timestamp"
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
    finally:
        if owns_conn:
            conn.close()

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)

    df = _compress_to_5min_bars(df)

    min_horizon_bars = max(DEFAULT_HORIZONS) if min_horizon_bars is None else min_horizon_bars
    min_rows = WARMUP_BARS + min_horizon_bars + 1
    if len(df) < min_rows:
        raise ValueError(
            f"Not enough bars for {symbol} after 5-min compression: got {len(df)}, "
            f"need at least {min_rows} (warmup {WARMUP_BARS} + horizon {min_horizon_bars})"
        )
    return df


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def resolve_model_path(symbol: str, explicit_path: Optional[str] = None) -> Path:
    """explicit_path wins if given. Otherwise use the current-pointer's
    path (the model actually paired with today's scaler -- see module
    docstring on the scaler-versioning gap). Raises FileNotFoundError with
    an actionable message if no archived model exists yet."""
    if explicit_path is not None:
        p = Path(explicit_path)
        if not p.exists():
            raise FileNotFoundError(f"--model-path {p} does not exist")
        return p

    pointer_path = ml_signal._current_pointer_path(symbol)
    if pointer_path.exists():
        pointer = json.loads(pointer_path.read_text())
        p = Path(pointer["path"])
        if p.exists():
            return p

    versions = ml_signal.list_archived_models(symbol)
    if not versions:
        raise FileNotFoundError(
            f"No archived model for {symbol} -- run scripts/retrain_regime.py first"
        )
    return Path(versions[0]["path"])


@dataclass
class LoadedModel:
    model: object
    scaler: object
    sustainable_state: int
    path: Path
    is_current: bool


def load_local_model(symbol: str, model_path: Optional[str] = None) -> LoadedModel:
    """Unpickle the model at model_path (or the current pointer's path) and
    pair it with TODAY's scaler + sustainable_state (scalers aren't
    versioned, only models are). If model_path isn't the current pointer's
    path, that pairing is a known mismatch -- logged loudly, not silently
    accepted."""
    resolved_path = resolve_model_path(symbol, model_path)

    pointer_path = ml_signal._current_pointer_path(symbol)
    current_path = None
    if pointer_path.exists():
        current_path = Path(json.loads(pointer_path.read_text())["path"])
    is_current = current_path is not None and resolved_path == current_path
    if not is_current:
        logger.warning(
            "Loading non-current model %s for %s -- pairing it with TODAY's scaler, "
            "which may not be the scaler it was actually trained with.",
            resolved_path, symbol,
        )

    with open(resolved_path, "rb") as f:
        model = pickle.load(f)

    scaler_path = ml_signal._scaler_path(symbol)
    if not scaler_path.exists():
        raise FileNotFoundError(f"No scaler for {symbol} at {scaler_path}")
    with open(scaler_path, "rb") as f:
        scaler = pickle.load(f)

    sustainable_state = ml_signal._load_sustainable_state(symbol)

    return LoadedModel(
        model=model, scaler=scaler, sustainable_state=sustainable_state,
        path=resolved_path, is_current=is_current,
    )


# ---------------------------------------------------------------------------
# Local inference -- mirrors ml_signal.get_regime()'s post-processing exactly
# ---------------------------------------------------------------------------

def local_infer_regime(window_df: pd.DataFrame, model, scaler, sustainable_state: int) -> dict:
    """Local equivalent of ml_signal.get_regime(), with the gRPC round-trip
    (submit_infer/wait_for_job) replaced by a direct model.predict()/
    .score() call. Feature extraction, scaling, sub-classification, and the
    confidence transform are all imported from ml_signal verbatim -- not
    reimplemented -- so this can't silently drift from the live path."""
    X_raw, details = ml_signal._extract_features(window_df)
    if not X_raw:
        return {
            "regime": "CHOPPY", "confidence": 0.0, "details": {}, "source": "error",
            "error": "not enough bars after feature warmup",
        }

    X_scaled = ml_signal._scale(X_raw, scaler)
    X_arr = np.array(X_scaled)
    states = model.predict(X_arr)
    last_state = int(states[-1])
    log_score = float(model.score(X_arr))

    hmm_regime = "SUSTAINABLE" if last_state == sustainable_state else "NOT_SUSTAINABLE"
    confidence = ml_signal._score_to_confidence(log_score, len(X_scaled))

    if hmm_regime == "SUSTAINABLE":
        final_regime = "SUSTAINABLE"
    else:
        final_regime = ml_signal._sub_classify(details)
        confidence = round(confidence * 0.7, 3)

    return {
        "regime": final_regime,
        "confidence": confidence,
        "details": details,
        "source": "local",
        "hmm_state": last_state,
        "log_score": log_score,
    }


# ---------------------------------------------------------------------------
# Opt-in gRPC parity check
# ---------------------------------------------------------------------------

async def parity_check(
    symbol: str,
    bars_df: pd.DataFrame,
    model,
    scaler,
    sustainable_state: int,
    n_samples: int = 5,
    seed: int = 42,
    client=None,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
) -> list[dict]:
    """Spot-compare local_infer_regime() against a real
    ml_signal.get_regime() gRPC call on identical historical slices. Off by
    default -- the only part of this harness that touches the live worker,
    and it's just another consumer of the existing get_regime() path (same
    as data_bus.py already is), not a service restart or config change."""
    rng = np.random.default_rng(seed)
    valid_indices = list(range(WARMUP_BARS, len(bars_df)))
    if not valid_indices:
        return []
    n_samples = min(n_samples, len(valid_indices))
    chosen = sorted(rng.choice(valid_indices, size=n_samples, replace=False).tolist())

    results = []
    for idx in chosen:
        window = bars_df.iloc[max(0, idx - lookback_bars + 1): idx + 1]
        local = local_infer_regime(window, model, scaler, sustainable_state)
        remote = await ml_signal.get_regime(symbol, window, client=client)
        local_conf = local.get("confidence")
        remote_conf = remote.get("confidence")
        results.append({
            "timestamp": bars_df.iloc[idx]["timestamp"],
            "local_regime": local.get("regime"),
            "remote_regime": remote.get("regime"),
            "agree": local.get("regime") == remote.get("regime"),
            "local_confidence": local_conf,
            "remote_confidence": remote_conf,
            "confidence_diff": (
                None if local_conf is None or remote_conf is None
                else round(abs(local_conf - remote_conf), 3)
            ),
        })
    return results


# ---------------------------------------------------------------------------
# Forward-return outcomes
# ---------------------------------------------------------------------------

@dataclass
class ForwardOutcome:
    fwd_return_pct: float
    mfe_pct: float
    mae_pct: float


def compute_forward_outcome(bars_df: pd.DataFrame, idx: int, horizon_bars: int) -> Optional[ForwardOutcome]:
    """None if idx + horizon_bars runs past the end of bars_df. entry =
    close[idx], exit = close[idx + horizon_bars]. mfe/mae are the max
    favorable/adverse excursion vs entry over the bars in between -- two
    cheap extra numbers, not a stop-loss simulator; this tool measures, it
    doesn't trade."""
    end_idx = idx + horizon_bars
    if end_idx >= len(bars_df):
        return None
    entry = float(bars_df.iloc[idx]["close"])
    if entry == 0:
        return None
    exit_price = float(bars_df.iloc[end_idx]["close"])
    window = bars_df.iloc[idx + 1: end_idx + 1]
    return ForwardOutcome(
        fwd_return_pct=(exit_price / entry - 1.0) * 100.0,
        mfe_pct=(float(window["high"].max()) / entry - 1.0) * 100.0,
        mae_pct=(float(window["low"].min()) / entry - 1.0) * 100.0,
    )


# ---------------------------------------------------------------------------
# Walk-forward loop
# ---------------------------------------------------------------------------

def walk_forward(
    bars_df: pd.DataFrame,
    model,
    scaler,
    sustainable_state: int,
    horizons=DEFAULT_HORIZONS,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    warmup_bars: int = WARMUP_BARS,
    stride: Optional[int] = None,
) -> pd.DataFrame:
    """Slides an evaluation point across bars_df at `stride`-bar steps.
    At each point, runs local_infer_regime() on a trailing lookback window,
    then computes forward outcomes at each configured horizon. Produces one
    row per (evaluation point x horizon) -- the raw table everything else
    reads from."""
    stride = stride or min(horizons)
    max_horizon = max(horizons)
    rows = []

    for i in range(warmup_bars, len(bars_df) - max_horizon, stride):
        window = bars_df.iloc[max(0, i - lookback_bars + 1): i + 1]
        call = local_infer_regime(window, model, scaler, sustainable_state)
        if call["source"] == "error":
            continue
        for h in horizons:
            outcome = compute_forward_outcome(bars_df, i, h)
            if outcome is None:
                continue
            rows.append({
                "timestamp": bars_df.iloc[i]["timestamp"],
                "regime": call["regime"],
                "confidence": call["confidence"],
                "hmm_state": call.get("hmm_state"),
                "log_score": call.get("log_score"),
                "horizon_bars": h,
                "fwd_return_pct": outcome.fwd_return_pct,
                "mfe_pct": outcome.mfe_pct,
                "mae_pct": outcome.mae_pct,
            })

    return pd.DataFrame(rows, columns=[
        "timestamp", "regime", "confidence", "hmm_state", "log_score",
        "horizon_bars", "fwd_return_pct", "mfe_pct", "mae_pct",
    ])


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _direction_correct(regime: str, fwd_return_pct: float, choppy_threshold: float) -> Optional[bool]:
    if regime == "SUSTAINABLE":
        return fwd_return_pct > 0
    if regime == "EXHAUSTED":
        return fwd_return_pct < 0
    if regime == "CHOPPY":
        return abs(fwd_return_pct) <= choppy_threshold
    return None


def _regime_distribution(sub_df: pd.DataFrame) -> dict:
    stats = {}
    for label in _REGIME_LABELS:
        returns = sub_df.loc[sub_df["regime"] == label, "fwd_return_pct"].tolist()
        if not returns:
            stats[label] = {"n": 0}
            continue
        stats[label] = {
            "n": len(returns),
            "mean": float(np.mean(returns)),
            "median": float(np.median(returns)),
            "stdev": float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0,
            "win_rate": compute_win_rate(returns),
        }
    return stats


def _confidence_calibration(sub_df: pd.DataFrame) -> list[dict]:
    # Self-referential CHOPPY "correctness" proxy -- no ground-truth label
    # exists for CHOPPY, so this treats "return stayed inside the typical
    # range for this horizon" as correct. A judgment call, not a validated
    # definition; flagged in the report rather than presented as ground truth.
    choppy_threshold = float(sub_df["fwd_return_pct"].abs().median()) if len(sub_df) else 0.0
    out = []
    for lo, hi in _CONFIDENCE_BUCKETS:
        bucket = sub_df[(sub_df["confidence"] >= lo) & (sub_df["confidence"] < hi)]
        if bucket.empty:
            out.append({"range": [lo, hi], "n": 0, "directional_accuracy": None})
            continue
        correct = [
            _direction_correct(row.regime, row.fwd_return_pct, choppy_threshold)
            for row in bucket.itertuples()
        ]
        correct = [c for c in correct if c is not None]
        accuracy = (sum(correct) / len(correct)) if correct else None
        out.append({"range": [lo, hi], "n": len(bucket), "directional_accuracy": accuracy})
    return out


def _split_half_significance(sub_df: pd.DataFrame) -> dict:
    """Paired t-test (src.validation.is_significant) comparing the first
    half of history against the second half's fwd_return_pct, per regime --
    is any apparent edge stable across time or a fluke of one window."""
    out = {}
    if sub_df.empty:
        return out
    median_ts = sub_df["timestamp"].median()
    first = sub_df[sub_df["timestamp"] <= median_ts]
    second = sub_df[sub_df["timestamp"] > median_ts]
    for label in _REGIME_LABELS:
        a = first.loc[first["regime"] == label, "fwd_return_pct"].tolist()
        b = second.loc[second["regime"] == label, "fwd_return_pct"].tolist()
        n = min(len(a), len(b))
        if n < 5:
            out[label] = {
                "n_pairs": n, "significant": False, "p_value": None,
                "note": "fewer than 5 paired windows -- not enough data for a t-test",
            }
            continue
        is_sig, p_value = is_significant(a[:n], b[:n])
        out[label] = {"n_pairs": n, "significant": is_sig, "p_value": float(p_value)}
    return out


def summarize(results_df: pd.DataFrame, total_bars: int) -> dict:
    """Per-horizon: regime-bucketed forward-return distribution, confidence
    calibration, first/second-half significance check, and an explicit
    sample-size caveat (overlapping evaluation windows aren't independent
    draws)."""
    summary = {"horizons": {}}
    if results_df.empty:
        return summary

    for horizon in sorted(results_df["horizon_bars"].unique()):
        sub = results_df[results_df["horizon_bars"] == horizon]
        horizon = int(horizon)
        approx_independent_n = total_bars // horizon if horizon else 0
        summary["horizons"][horizon] = {
            "raw_row_count": len(sub),
            "approx_independent_window_count": approx_independent_n,
            "sample_size_note": (
                "adjacent evaluation windows overlap and are not independent draws -- "
                "treat raw_row_count as a diagnostic curve, approx_independent_window_count "
                "as the real order-of-magnitude sample size"
            ),
            "regime_distribution": _regime_distribution(sub),
            "confidence_calibration": _confidence_calibration(sub),
            "split_half_significance": _split_half_significance(sub),
        }
    return summary


# ---------------------------------------------------------------------------
# Report output
# ---------------------------------------------------------------------------

def write_report(
    results_df: pd.DataFrame,
    summary: dict,
    symbol: str,
    out_dir: Optional[Path] = None,
    run_date: Optional[str] = None,
    parity_results: Optional[list[dict]] = None,
) -> tuple[Path, Path]:
    out_dir = out_dir or (Path(__file__).resolve().parent.parent / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")

    csv_path = out_dir / f"regime_backtest_{symbol}_{run_date}.csv"
    md_path = out_dir / f"regime_backtest_{symbol}_{run_date}.md"

    results_df.to_csv(csv_path, index=False)

    lines = [
        f"# Regime Backtest — {symbol} ({run_date})",
        "",
        "Offline walk-forward scoring of src.ml_signal's HMM regime classifier "
        "against realized forward returns, using local inference against the "
        "current archived model. Re-run periodically as more "
        "market_data.bars_5min history accumulates -- this is a perishable "
        "read, not a final verdict (only ~7 weeks of 5-min-bar depth exists "
        "as of this writing).",
        "",
    ]
    for horizon, stats in summary.get("horizons", {}).items():
        lines.append(f"## Horizon: {horizon} bars")
        lines.append("")
        lines.append(f"- raw rows: {stats['raw_row_count']}")
        lines.append(f"- approx independent windows: {stats['approx_independent_window_count']}")
        lines.append(f"- {stats['sample_size_note']}")
        lines.append("")
        lines.append("### Forward-return distribution by regime")
        lines.append("")
        lines.append("| regime | n | mean % | median % | stdev % | win rate |")
        lines.append("|---|---|---|---|---|---|")
        for label, d in stats["regime_distribution"].items():
            if d["n"] == 0:
                lines.append(f"| {label} | 0 | - | - | - | - |")
            else:
                lines.append(
                    f"| {label} | {d['n']} | {d['mean']:.3f} | {d['median']:.3f} | "
                    f"{d['stdev']:.3f} | {d['win_rate']:.1%} |"
                )
        lines.append("")
        lines.append("### Confidence calibration")
        lines.append("")
        lines.append("| range | n | directional accuracy |")
        lines.append("|---|---|---|")
        for bucket in stats["confidence_calibration"]:
            lo, hi = bucket["range"]
            acc = bucket.get("directional_accuracy")
            acc_str = f"{acc:.1%}" if acc is not None else "-"
            lines.append(f"| [{lo}, {hi}) | {bucket['n']} | {acc_str} |")
        lines.append("")
        lines.append("### First-half vs second-half significance (paired t-test)")
        lines.append("")
        for label, sig in stats["split_half_significance"].items():
            note = sig.get("note", "")
            p_val = sig.get("p_value")
            p_str = f"{p_val:.3f}" if p_val is not None else "n/a"
            lines.append(
                f"- {label}: n_pairs={sig['n_pairs']}, significant={sig['significant']}, "
                f"p={p_str} {note}".rstrip()
            )
        lines.append("")

    if parity_results:
        lines.append("## gRPC parity spot-check")
        lines.append("")
        lines.append("| timestamp | local | remote | agree | conf diff |")
        lines.append("|---|---|---|---|---|")
        for r in parity_results:
            lines.append(
                f"| {r['timestamp']} | {r['local_regime']} | {r['remote_regime']} | "
                f"{r['agree']} | {r['confidence_diff']} |"
            )
        lines.append("")

    md_path.write_text("\n".join(lines))
    return csv_path, md_path
