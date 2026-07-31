#!/usr/bin/env python3
"""
ML trainer service — a standalone, long-running process (not an OpenClaw
cron, matching discovery_daemon.py's pattern for real background work)
that trains a real, interpretable win/loss classifier on the GPU worker
and serves its output as a signal Stan reads alongside everything else
(fundamentals, congress trades, social sentiment, wiki narratives) --
not a gate, not a black box.

Model: logistic regression on a small, interpretable feature set (RSI,
volume ratio, MACD histogram, conviction) -- deliberately not a black
box, chosen so its coefficients are directly explainable in the served
text output. See src/gpu_client.py's WorkerPool for GPU dispatch
(least-loaded healthy worker, graceful skip if none are online) -- this
service does NOT implement its own worker selection.

Training set: Stan's real closed trades (workspace-trader-stonks's
state/trader.db) PLUS counterfactual alternatives (src/counterfactual.py
-- for each real trade, what else would Stan's entry logic have
triggered on, and how did those alternatives actually do) as additional
labeled examples. A handful of real trades alone isn't enough data;
peer-comparison multiplies it without fabricating anything -- each
alternative is a real, replayed outcome, not a synthetic one.

Validation: walk-forward only (train on the earlier portion of the
labeled set by date, validate on the later portion) -- same discipline
as the overnight harness's split-window Sharpe fix, for the same reason
(a random train/test split leaks future information into training on
time-series data).

Usage:
    python3 scripts/ml_trainer_service.py --once      # single cycle, for testing
    python3 scripts/ml_trainer_service.py              # loop forever (real service)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from src.gpu_client import WorkerPool  # noqa: E402
from src.counterfactual import UniverseSampler, CounterfactualReplay  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ml-trainer] %(levelname)s %(message)s")
log = logging.getLogger("ml-trainer")

CACHE_DIR = PROJECT_DIR / "shared" / "cache" / "bars"
SIGNALS_DIR = PROJECT_DIR / "shared" / "ml_signals"
SIGNALS_PATH = SIGNALS_DIR / "latest.json"
STONKS_DB_PATH = Path("/home/openclaw/.openclaw/workspace-trader-stonks/state/trader.db")

# Small, bounded subset per cycle -- not the whole market at once, matching
# the overnight harness's per-iteration universe pattern.
DEFAULT_UNIVERSE = ["AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOGL", "AMZN",
                    "JPM", "GS", "BAC", "V", "MA", "AMD", "NFLX"]

FEATURE_NAMES = ["rsi_14", "volume_ratio", "macd_hist", "conviction"]
WALK_FORWARD_TRAIN_FRACTION = 0.7
CYCLE_SECONDS = 3600  # hourly -- retraining every cycle is cheap once data is cached


# ── Feature extraction ───────────────────────────────────────────────────────

def _features_at(symbol: str, as_of: datetime) -> Optional[Dict[str, float]]:
    """RSI/volume-ratio/MACD-hist from cached bars at (or just before) as_of.
    None if no cached data covers that date -- caller skips the example
    rather than fabricating a feature vector."""
    path = CACHE_DIR / f"{symbol}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        return None
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    as_of_utc = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    prior = df[df["timestamp"] <= as_of_utc]
    if prior.empty:
        return None
    row = prior.iloc[-1]
    vol_window = prior["volume"].tail(20)
    vol_avg = vol_window.mean() if len(vol_window) > 0 else row["volume"]
    volume_ratio = float(row["volume"] / vol_avg) if vol_avg else 1.0
    if any(pd.isna(row.get(c)) for c in ("rsi_14", "macd_hist")):
        return None
    return {
        "rsi_14": float(row["rsi_14"]),
        "volume_ratio": volume_ratio,
        "macd_hist": float(row["macd_hist"]),
    }


# ── Training set construction ────────────────────────────────────────────────

def _stan_closed_trades(limit: int = 200) -> List[dict]:
    """Real, labeled examples from Stan's own trading history."""
    if not STONKS_DB_PATH.exists():
        log.warning("trader.db not found at %s -- no real trades to train on", STONKS_DB_PATH)
        return []
    conn = sqlite3.connect(f"file:{STONKS_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT ticker, entry_time, closed_at, realized_return_pct
               FROM positions
               WHERE status = 'closed' AND realized_return_pct IS NOT NULL
                 AND entry_time != closed_at
               ORDER BY closed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    # entry_time != closed_at excludes the pre-2026-07-28 migration rows
    # with fabricated timestamps (see memory: stonks-trading-architecture) --
    # a walk-forward split needs real dates, not placeholder ones.
    return [dict(r) for r in rows]


def build_training_examples() -> List[Tuple[Dict[str, float], int, str]]:
    """Returns [(features, label, date_str), ...] -- label is 1 for a win,
    0 otherwise. Combines Stan's real trades with counterfactual
    alternatives for each (more labeled examples from real, replayed
    outcomes, not fabricated data)."""
    examples: List[Tuple[Dict[str, float], int, str]] = []

    trades = _stan_closed_trades()
    log.info("Building training set from %d real closed trades", len(trades))

    sampler = UniverseSampler()
    replay = CounterfactualReplay()

    for t in trades:
        try:
            entry_dt = datetime.fromisoformat(t["entry_time"])
        except (ValueError, TypeError):
            continue
        date_str = entry_dt.strftime("%Y-%m-%d")
        feats = _features_at(t["ticker"], entry_dt)
        if feats is not None:
            feats_full = {**feats, "conviction": 0.5}  # real trade's own conviction isn't
            # persisted on the positions row -- 0.5 (neutral) rather than fabricating one.
            label = 1 if t["realized_return_pct"] > 0 else 0
            examples.append((feats_full, label, date_str))

        # Counterfactual alternatives: real, replayed outcomes for peer
        # tickers in the same price band, not synthetic data.
        try:
            alts = sampler.sample_alternatives(t["ticker"], date_str, n=8)
            if not alts:
                continue
            cf_result = replay.run(t["ticker"], date_str, entry_price=0.0, alternatives=alts)
            for alt in cf_result.triggered_results:
                alt_feats = _features_at(alt.symbol, alt.entry_time)
                if alt_feats is None:
                    continue
                alt_feats_full = {**alt_feats, "conviction": alt.conviction}
                alt_label = 1 if alt.next_day_return_pct > 0 else 0
                examples.append((alt_feats_full, alt_label, date_str))
        except Exception as e:
            log.debug("Counterfactual sampling failed for %s on %s: %s", t["ticker"], date_str, e)

    log.info("Training set: %d examples (%d real trades + counterfactual alternatives)",
              len(examples), len(trades))
    return examples


def walk_forward_split(
    examples: List[Tuple[Dict[str, float], int, str]],
) -> Tuple[List[Tuple[Dict[str, float], int, str]], List[Tuple[Dict[str, float], int, str]]]:
    """Train on the earlier portion by date, validate on the later portion --
    never a random split. See module docstring for why."""
    ordered = sorted(examples, key=lambda e: e[2])
    split_idx = int(len(ordered) * WALK_FORWARD_TRAIN_FRACTION)
    return ordered[:split_idx], ordered[split_idx:]


def _to_xy(examples: List[Tuple[Dict[str, float], int, str]]) -> Tuple[List[List[float]], List[int]]:
    X = [[e[0][f] for f in FEATURE_NAMES] for e in examples]
    y = [e[1] for e in examples]
    return X, y


# ── GPU training ──────────────────────────────────────────────────────────────

async def train_on_gpu(train_examples: List[Tuple[Dict[str, float], int, str]]) -> Optional[dict]:
    """Submit a logistic_regression TrainJob to the least-loaded healthy
    worker, wait for completion. Returns the worker's result dict (expects
    fitted coefficients so new candidates can be scored locally, no second
    infer round-trip needed) or None if no worker was available -- caller
    treats that as "skip this cycle", not a crash."""
    X, y = _to_xy(train_examples)
    if len(X) < 10:
        log.warning("Only %d training examples, skipping GPU training this cycle "
                    "(walk-forward validation isn't meaningful yet)", len(X))
        return None

    pool = WorkerPool.from_env()
    client = await pool.pick()
    if client is None:
        log.warning("No GPU workers reachable this cycle -- skipping training, not crashing")
        await pool.close()
        return None

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump({"features": X, "labels": y}, tmp)
        tmp_path = tmp.name

    try:
        up = await client.upload_file(tmp_path, staging_subdir="train")
        if up is None or not up.ok:
            log.error("Upload to GPU worker failed: %s", getattr(up, "error", "no response"))
            return None
        job_id = await client.submit_train(
            model_type="logistic_regression",
            symbol="",  # cross-ticker classifier, not per-symbol like the regime HMMs
            data_path=up.stored_path,
        )
        if not job_id:
            log.error("submit_train returned no job_id")
            return None
        result = await client.wait_for_job(job_id, timeout=300.0)
        if result is None or result.error:
            log.error("Training job failed: %s", getattr(result, "error", "no result"))
            return None
        return json.loads(result.result_json.decode())
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        await pool.close()


# ── Local scoring + explanation (no second infer round-trip for v1) ─────────

def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def score_candidate(features: Dict[str, float], coefficients: Dict[str, float], intercept: float) -> float:
    z = intercept + sum(coefficients.get(f, 0.0) * features.get(f, 0.0) for f in FEATURE_NAMES)
    return _sigmoid(z)


def explain(features: Dict[str, float], coefficients: Dict[str, float], p_win: float) -> str:
    """Plain-language explanation from the fitted coefficients -- the whole
    point of logistic regression here over a black-box model. Cites the
    feature(s) actually pulling the prediction, not a canned sentence."""
    contributions = sorted(
        ((f, coefficients.get(f, 0.0) * features.get(f, 0.0)) for f in FEATURE_NAMES),
        key=lambda fc: abs(fc[1]), reverse=True,
    )
    top = contributions[0]
    direction = "supporting" if top[1] > 0 else "working against"
    lean = "leans favorable" if p_win > 0.55 else "leans unfavorable" if p_win < 0.45 else "is roughly even"
    return (
        f"Model {lean} ({p_win:.0%} estimated win probability); "
        f"{top[0]}={features.get(top[0], 0.0):.2f} is the strongest factor {direction} it."
    )


# ── Serve ─────────────────────────────────────────────────────────────────────

def write_signals(scores: Dict[str, dict], model_type: str, val_accuracy: Optional[float], n_train: int) -> None:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {}
    trained_at = datetime.now(timezone.utc).isoformat()
    confidence_note = (
        f"walk-forward validated on {n_train} training examples"
        + (f", {val_accuracy:.0%} holdout accuracy" if val_accuracy is not None else "")
    )
    for symbol, s in scores.items():
        payload[symbol] = {
            "p_win": round(s["p_win"], 4),
            "explanation": s["explanation"],
            "model_type": model_type,
            "trained_at": trained_at,
            "confidence_note": confidence_note,
        }
    SIGNALS_PATH.write_text(json.dumps(payload, indent=2))
    log.info("Wrote %d symbol signals to %s", len(payload), SIGNALS_PATH)


# ── One full cycle ────────────────────────────────────────────────────────────

async def run_cycle(universe: List[str] = None) -> None:
    universe = universe or DEFAULT_UNIVERSE
    examples = build_training_examples()
    if len(examples) < 10:
        log.warning("Not enough labeled examples yet (%d) -- skipping this cycle. "
                    "Needs more of Stan's real closed trades to accumulate.", len(examples))
        return

    train_examples, val_examples = walk_forward_split(examples)
    result = await train_on_gpu(train_examples)
    if result is None:
        return

    coefficients = dict(zip(FEATURE_NAMES, result.get("coefficients", [0.0] * len(FEATURE_NAMES))))
    intercept = result.get("intercept", 0.0)

    val_accuracy = None
    if val_examples:
        correct = sum(
            1 for feats, label, _ in val_examples
            if (score_candidate(feats, coefficients, intercept) > 0.5) == bool(label)
        )
        val_accuracy = correct / len(val_examples)
        log.info("Walk-forward holdout accuracy: %.1f%% (%d examples)", val_accuracy * 100, len(val_examples))

    now = datetime.now(timezone.utc)
    scores = {}
    for symbol in universe:
        feats = _features_at(symbol, now)
        if feats is None:
            continue
        feats_full = {**feats, "conviction": 0.5}
        p_win = score_candidate(feats_full, coefficients, intercept)
        scores[symbol] = {"p_win": p_win, "explanation": explain(feats_full, coefficients, p_win)}

    write_signals(scores, "logistic_regression", val_accuracy, len(train_examples))


async def main_async(once: bool) -> None:
    if once:
        await run_cycle()
        return
    while True:
        try:
            await run_cycle()
        except Exception as e:
            log.error("Cycle failed, will retry next interval: %s", e, exc_info=True)
        await asyncio.sleep(CYCLE_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit (for testing)")
    args = parser.parse_args()
    asyncio.run(main_async(args.once))
    return 0


if __name__ == "__main__":
    sys.exit(main())
