#!/usr/bin/env python3
"""Offline backtest-validation harness CLI for the SPY market-regime HMM.

Replays historical market_data.bars_5min through src.ml_signal's classifier
(local inference against the current archived model, not per-window gRPC --
see src/regime_backtest.py's module docstring) and scores its calls against
realized forward returns. Writes a dated report to reports/.

Measurement only -- does not retrain, redeploy, or touch decision_heuristics.md.

Usage:
    python3 scripts/backtest_regime.py --symbol SPY
    python3 scripts/backtest_regime.py --symbol SPY --parity-check 5
    python3 scripts/backtest_regime.py --symbol SPY --horizons 12,78,192 --lookback-bars 780
"""
import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from src import regime_backtest  # noqa: E402


async def main_async(args: argparse.Namespace) -> int:
    horizons = tuple(int(h) for h in args.horizons.split(","))

    print(f"[1/4] Loading {args.symbol} bars from market_data.bars_5min...")
    try:
        bars_df = regime_backtest.load_bars_from_pg(
            args.symbol, min_horizon_bars=max(horizons)
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"  Loaded {len(bars_df)} bars ({bars_df['timestamp'].min()} to {bars_df['timestamp'].max()})")

    print(f"[2/4] Loading archived model for {args.symbol}...")
    try:
        loaded = regime_backtest.load_local_model(args.symbol, model_path=args.model_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"  Model: {loaded.path} (current={loaded.is_current})")

    print(f"[3/4] Running walk-forward (horizons={horizons}, "
          f"lookback={args.lookback_bars}, stride={args.stride})...")
    results_df = regime_backtest.walk_forward(
        bars_df, loaded.model, loaded.scaler, loaded.sustainable_state,
        horizons=horizons, lookback_bars=args.lookback_bars, stride=args.stride,
    )
    print(f"  {len(results_df)} (evaluation point x horizon) rows")
    if results_df.empty:
        print("ERROR: walk_forward produced no rows -- not enough bars for the "
              "configured warmup/horizons/lookback", file=sys.stderr)
        return 1

    summary = regime_backtest.summarize(results_df, total_bars=len(bars_df))

    parity_results = None
    if args.parity_check > 0:
        print(f"[4/4] Running gRPC parity check ({args.parity_check} spot-checks)...")
        parity_results = await regime_backtest.parity_check(
            args.symbol, bars_df, loaded.model, loaded.scaler, loaded.sustainable_state,
            n_samples=args.parity_check, lookback_bars=args.lookback_bars,
        )
        agreed = sum(1 for r in parity_results if r["agree"])
        print(f"  {agreed}/{len(parity_results)} local/remote regime calls agreed")
        for r in parity_results:
            print(f"    {r['timestamp']}: local={r['local_regime']}({r['local_confidence']}) "
                  f"remote={r['remote_regime']}({r['remote_confidence']}) agree={r['agree']}")
    else:
        print("[4/4] Skipping gRPC parity check (--parity-check 0)")

    csv_path, md_path = regime_backtest.write_report(
        results_df, summary, args.symbol, parity_results=parity_results,
    )
    print(f"\nReport written: {md_path}")
    print(f"Raw results:    {csv_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--horizons", default=",".join(str(h) for h in regime_backtest.DEFAULT_HORIZONS),
                         help="Comma-separated forward-return horizons in bars")
    parser.add_argument("--lookback-bars", type=int, default=regime_backtest.DEFAULT_LOOKBACK_BARS)
    parser.add_argument("--stride", type=int, default=None,
                         help="Bars between evaluation points (default: min horizon)")
    parser.add_argument("--model-path", default=None,
                         help="Override the current archived model (loud warning: paired with today's scaler regardless)")
    parser.add_argument("--parity-check", type=int, default=0,
                         help="N spot-check windows to verify against live gRPC get_regime() (0 = off, default)")
    args = parser.parse_args()

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
