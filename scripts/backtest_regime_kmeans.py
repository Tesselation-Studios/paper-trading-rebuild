#!/usr/bin/env python3
"""Offline backtest-validation harness CLI for the orphaned K-Means regime
candidate (src.regime_detector.RegimeDetector, commit bf93533 -- never
live-wired on v4; src.signals._classify_regime and src.ml_signal's HMM are
what's actually consumed elsewhere).

Trains a fresh, in-memory RegimeDetector on the first --train-frac of
market_data.bars_1d history and walk-forward-evaluates only on the held-out
remainder -- scoring the existing frozen /home/openclaw/data/regime_kmeans.pkl
artifact against its own training data would be in-sample and overstate any
edge (the same class of problem regime_backtest._split_half_significance
exists to catch for the HMM). No disk writes (model_path="" -- see
RegimeDetector._save()), no gRPC (K-Means is local sklearn). Produces a
report directly comparable to scripts/backtest_regime.py's HMM report,
using the same walk-forward/scoring/reporting core (see src/regime_backtest.py's
module docstring for the Phase C generalization).

market_data.bars_1d is currently ~3 weeks stale (only src.train_regime_detector
refreshes it, and nothing schedules that) -- not refreshed here. That's fine
for this offline comparison; freshness only matters if/when a daily-bar model
gets deployed live.

Measurement only -- does not retrain the live classifier, redeploy anything,
or touch decision_heuristics.md.

Usage:
    python3 scripts/backtest_regime_kmeans.py --symbol SPY
    python3 scripts/backtest_regime_kmeans.py --symbol SPY --k 4 --train-frac 0.7
"""
import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from src import regime_backtest  # noqa: E402
from src import regime_detector  # noqa: E402


def main(args: argparse.Namespace) -> int:
    horizons = tuple(int(h) for h in args.horizons.split(","))

    print(f"[1/4] Loading {args.symbol} daily bars from market_data.bars_1d...")
    try:
        bars_df = regime_backtest.load_daily_bars_from_pg(
            args.symbol, min_horizon_bars=max(horizons)
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"  Loaded {len(bars_df)} daily bars ({bars_df['timestamp'].min()} to {bars_df['timestamp'].max()})")

    split_idx = int(len(bars_df) * args.train_frac)
    train_slice = bars_df.iloc[:split_idx]
    test_bars = len(bars_df) - split_idx
    print(f"[2/4] Training K-Means (k={args.k}) on first {len(train_slice)} bars "
          f"({args.train_frac:.0%}), evaluating on the held-out {test_bars} bars...")
    detector = regime_detector.RegimeDetector(k=args.k, model_path="")
    records = regime_backtest._df_to_records(train_slice, args.symbol)
    try:
        detector.fit(records, symbols=[args.symbol])
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"[3/4] Running walk-forward (horizons={horizons}, "
          f"lookback={args.lookback_bars}, stride={args.stride})...")
    results_df = regime_backtest.walk_forward_kmeans(
        bars_df, detector, symbol=args.symbol, horizons=horizons,
        lookback_bars=args.lookback_bars, warmup_bars=split_idx, stride=args.stride,
    )
    print(f"  {len(results_df)} (evaluation point x horizon) rows")
    if results_df.empty:
        print("ERROR: walk_forward_kmeans produced no rows -- not enough held-out "
              "bars for the configured horizons/lookback", file=sys.stderr)
        return 1

    summary = regime_backtest.summarize(
        results_df, total_bars=test_bars,
        regime_labels=regime_backtest._KMEANS_REGIME_LABELS,
        direction_map=regime_backtest._KMEANS_DIRECTION_MAP,
        confidence_buckets=regime_backtest._KMEANS_CONFIDENCE_BUCKETS,
    )

    print("[4/4] Writing report...")
    description = (
        "Offline walk-forward scoring of the orphaned src.regime_detector K-Means "
        "regime candidate (never live-wired on v4) against realized forward returns. "
        f"Model trained fresh on the first {args.train_frac:.0%} of "
        "market_data.bars_1d history (k="
        f"{args.k}) and evaluated only on the held-out remainder -- avoids scoring "
        "the pre-existing, frozen /home/openclaw/data/regime_kmeans.pkl artifact "
        "in-sample. Directly comparable to the HMM's regime_backtest report from the "
        "same walk-forward/scoring methodology. market_data.bars_1d is not refreshed "
        "by this run and may be several weeks stale -- fine for this offline "
        "comparison; freshness only matters if/when a daily-bar model is deployed live."
    )
    csv_path, md_path = regime_backtest.write_report(
        results_df, summary, args.symbol, candidate_slug="kmeans", description=description,
    )
    print(f"\nReport written: {md_path}")
    print(f"Raw results:    {csv_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="SPY", help="Only symbol this data path is proven against")
    parser.add_argument("--k", type=int, default=4, help="K-Means cluster count (default matches the historical training run)")
    parser.add_argument("--train-frac", type=float, default=0.7,
                         help="Fraction of history used to fit the detector; the rest is held out for evaluation")
    parser.add_argument("--horizons", default=",".join(str(h) for h in regime_backtest.DEFAULT_DAILY_HORIZONS),
                         help="Comma-separated forward-return horizons in daily bars")
    parser.add_argument("--lookback-bars", type=int, default=regime_backtest.DEFAULT_DAILY_HORIZONS[-1] * 3)
    parser.add_argument("--stride", type=int, default=None,
                         help="Bars between evaluation points (default: min horizon)")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(main(parse_args()))
