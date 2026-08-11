#!/usr/bin/env python3
"""Retrain the SPY K-Means regime detector used by get_market_regime.

Steps:
  1. Refresh SPY daily bars via backfill_market_data.py --tickers SPY
     (writes to market_data.bars_1d, ON CONFLICT DO NOTHING -- idempotent).
     Hits the live Alpaca API and writes to the shared prod Postgres DB.
  2. Load full SPY market_data.bars_1d history.
  3. Fit a fresh RegimeDetector(k=4) on all of it -- unlike Phase C's
     evaluation harness (scripts/backtest_regime_kmeans.py), which holds out
     30% for honest offline scoring, production training uses all available
     history.
  4. Archive it via src.kmeans_regime.archive_detector (versioned, local --
     no remote worker involved, unlike scripts/retrain_regime.py's HMM).

Meant to run daily on weekdays after market close (stonks-regime-kmeans-retrain
cron) -- unlike the HMM's weekly retrain, this is a cheap, local, in-process
fit (no GPU worker round-trip), so keeping the model current daily is easy.

Usage:
    python3 scripts/retrain_regime_kmeans.py
    python3 scripts/retrain_regime_kmeans.py --symbol SPY --k 4
"""
import argparse
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
BACKFILL_SCRIPT = PROJECT_DIR / "scripts" / "backfill_market_data.py"

sys.path.insert(0, str(PROJECT_DIR))
load_dotenv(PROJECT_DIR / ".env")

from src import kmeans_regime  # noqa: E402
from src import regime_backtest  # noqa: E402
from src import regime_detector  # noqa: E402


def refresh_bars(symbol: str) -> int:
    """Shell out to the existing Alpaca backfill script (same subprocess
    pattern scripts/retrain_regime.py already uses for the HMM's 5-min
    backfill). Idempotent -- ON CONFLICT DO NOTHING on market_data.bars_1d."""
    cmd = [sys.executable, str(BACKFILL_SCRIPT), "--tickers", symbol]
    result = subprocess.run(cmd, cwd=str(PROJECT_DIR))
    return result.returncode


def main(args: argparse.Namespace) -> int:
    print(f"[1/3] Refreshing {args.symbol} daily bars via backfill_market_data.py...")
    rc = refresh_bars(args.symbol)
    if rc != 0:
        print(f"ERROR: bar refresh failed (exit {rc})", file=sys.stderr)
        return 1

    print(f"[2/3] Loading full {args.symbol} history from market_data.bars_1d...")
    try:
        bars_df = regime_backtest.load_daily_bars_from_pg(args.symbol, min_horizon_bars=0)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"  Loaded {len(bars_df)} daily bars ({bars_df['timestamp'].min()} to {bars_df['timestamp'].max()})")

    print(f"[3/3] Fitting K-Means (k={args.k}) on full history and archiving...")
    detector = regime_detector.RegimeDetector(k=args.k, model_path="")
    records = regime_backtest._df_to_records(bars_df, args.symbol)
    try:
        detector.fit(records, symbols=[args.symbol])
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    result = kmeans_regime.archive_detector(detector, args.symbol)
    print(f"  Archived: {result['path']}")
    print("Done.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--k", type=int, default=4, help="K-Means cluster count (matches the historical training run)")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(main(parse_args()))
