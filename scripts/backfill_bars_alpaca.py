#!/usr/bin/env python3
"""
Backfill historical 5-min OHLCV bars via Alpaca Markets API.

Replaces backfill_bars.py (yfinance) since Yahoo Finance is unreachable
from the homelab. Same output format: Parquet files with technical indicators
(RSI, MACD, ATR) in shared/cache/bars/<ticker>.parquet.

Idempotent — checks existing dates, only fetches missing ones.

Usage:
    python3 scripts/backfill_bars_alpaca.py --tickers core --days 20
    python3 scripts/backfill_bars_alpaca.py --tickers AAPL,MSFT --days 30
    python3 scripts/backfill_bars_alpaca.py --tickers core --days 20 --check
    python3 scripts/backfill_bars_alpaca.py --tickers all --days 10 --force

Requires: APCA_API_KEY_ID and APCA_API_SECRET_KEY env vars.
"""

import argparse
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ── Path setup ───────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
SHARED_DIR = PROJECT_DIR / "shared"
BARS_DIR = SHARED_DIR / "cache" / "bars"

BARS_DIR.mkdir(parents=True, exist_ok=True)

import pandas as pd

# ── Ticker groups ────────────────────────────────────────────────────────────
CORE_TICKERS: List[str] = [
    "SPY", "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOGL", "AMZN",
]

TRADER_TICKERS: Dict[str, List[str]] = {
    "kairos": [
        "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
        "SPY", "QQQ", "IWM", "SMH", "SOXL", "TQQQ",
        "PLTR", "SOFI", "HOOD",
    ],
    "aldridge": [
        "JPM", "MSFT", "AMZN", "GOOGL", "BRK.B", "WMT", "JNJ",
        "PG", "XOM", "BAC", "SPY", "DIA", "SCHD", "VYM",
    ],
    "stonks": [
        "NVDA", "TSLA", "COIN", "PLTR", "MSTR", "GME",
        "AMC", "RIOT", "MARA", "HOOD", "DJT", "SNAP",
    ],
}

# Interval for Alpaca — 5-min bars
INTERVAL = "5Min"
FETCH_DELAY = 0.25  # Alpaca allows generous rate limits

# Technical indicator params
RSI_LENGTH = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ATR_LENGTH = 14


def resolve_tickers(spec: str) -> List[str]:
    if spec.lower() == "core":
        return sorted(CORE_TICKERS)
    if spec.lower() == "all":
        all_set: Set[str] = set()
        for tickers in TRADER_TICKERS.values():
            all_set.update(tickers)
        return sorted(all_set)
    return sorted([t.strip().upper() for t in spec.split(",") if t.strip()])


# ── Lazy pandas_ta import (optional) ─────────────────────────────────────────
_has_pandas_ta: bool = False
try:
    import pandas_ta as ta
    _has_pandas_ta = True
except ImportError:
    ta = None


def existing_dates(ticker: str) -> Set[str]:
    path = BARS_DIR / f"{ticker}.parquet"
    if not path.exists():
        return set()
    try:
        df = pd.read_parquet(path, columns=["timestamp"])
        dates = df["timestamp"].dt.strftime("%Y-%m-%d")
        return set(dates.unique())
    except Exception:
        return set()


def missing_date_range(
    ticker: str,
    days: int,
    existing: Optional[Set[str]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    if existing is None:
        existing = existing_dates(ticker)

    today = date.today()
    end_date = today
    start_date = today - timedelta(days=days + 1)

    expected = set()
    d = start_date
    while d <= end_date:
        expected.add(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    missing = expected - existing
    if not missing:
        return None, None

    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if not _has_pandas_ta:
        for col in ["rsi_14", "macd", "macd_signal", "macd_hist", "atr_14"]:
            df[col] = float("nan")
        return df

    closes = df["close"]
    highs = df["high"]
    lows = df["low"]

    try:
        rsi_series = ta.rsi(closes, length=RSI_LENGTH)
        df["rsi_14"] = pd.to_numeric(rsi_series, errors="coerce").astype("float64")
    except Exception:
        df["rsi_14"] = pd.Series([float("nan")] * len(df), dtype="float64")

    try:
        macd_df = ta.macd(closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
        if macd_df is not None and macd_df.shape[1] >= 3:
            df["macd"] = pd.to_numeric(macd_df.iloc[:, 0], errors="coerce").astype("float64")
            df["macd_signal"] = pd.to_numeric(macd_df.iloc[:, 1], errors="coerce").astype("float64")
            df["macd_hist"] = pd.to_numeric(macd_df.iloc[:, 2], errors="coerce").astype("float64")
        else:
            for c in ["macd", "macd_signal", "macd_hist"]:
                df[c] = pd.array([float("nan")] * len(df), dtype="float64")
    except Exception:
        for c in ["macd", "macd_signal", "macd_hist"]:
            df[c] = pd.array([float("nan")] * len(df), dtype="float64")

    try:
        atr_series = ta.atr(highs, lows, closes, length=ATR_LENGTH)
        df["atr_14"] = pd.to_numeric(atr_series, errors="coerce").astype("float64")
    except Exception:
        df["atr_14"] = pd.Series([float("nan")] * len(df), dtype="float64")

    return df


def fetch_bars_alpaca(ticker: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """Fetch 5-min OHLCV bars from Alpaca Markets."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from alpaca.data.enums import DataFeed

        api_key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            print(f"  ERROR: Alpaca credentials not found in env (APCA_API_KEY_ID)", file=sys.stderr)
            return None

        client = StockHistoricalDataClient(api_key, secret_key)

        start_ts = pd.Timestamp(start, tz="America/New_York")
        end_ts = pd.Timestamp(end, tz="America/New_York") + pd.Timedelta(days=1)

        request_params = StockBarsRequest(
            symbol_or_symbols=[ticker],
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start_ts.isoformat(),
            end=end_ts.isoformat(),
            feed=DataFeed.IEX,
        )

        bars = client.get_stock_bars(request_params)
        sym_bars = bars.data.get(ticker, [])

        if not sym_bars:
            return None

        records = []
        for b in sym_bars:
            records.append({
                "timestamp": b.timestamp,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume),
            })

        if not records:
            return None

        df = pd.DataFrame(records)
        df = df.sort_values("timestamp").reset_index(drop=True)

        # Ensure timezone-aware UTC
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize("America/New_York")
        df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")

        return df

    except Exception as e:
        print(f"  ERROR fetching {ticker} from Alpaca: {e}", file=sys.stderr)
        return None


def merge_and_dedup(
    existing_path: Path,
    new_df: pd.DataFrame,
) -> pd.DataFrame:
    if existing_path.exists():
        existing_df = pd.read_parquet(existing_path)
        for col in new_df.columns:
            if col not in existing_df.columns:
                existing_df[col] = float("nan")
        for col in existing_df.columns:
            if col not in new_df.columns and col != "timestamp":
                new_df[col] = float("nan")

        combined = pd.concat([existing_df, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
        combined = combined.sort_values("timestamp").reset_index(drop=True)
        return combined
    else:
        return new_df.sort_values("timestamp").reset_index(drop=True)


def atomic_write(df: pd.DataFrame, final_path: Path) -> None:
    fd, tmp_path = tempfile.mkstemp(
        suffix=".parquet",
        prefix=f"{final_path.stem}_",
        dir=str(final_path.parent),
    )
    os.close(fd)
    tmp = Path(tmp_path)

    try:
        df.to_parquet(tmp, index=False)
        _verify = pd.read_parquet(tmp)
        if _verify is None or _verify.empty:
            raise RuntimeError(f"Verification read of {tmp} returned empty DataFrame")
        tmp.rename(final_path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def backfill_ticker(
    ticker: str,
    days: int,
    force: bool = False,
    check_only: bool = False,
    verbose: bool = False,
) -> Tuple[str, str, int]:
    cache_path = BARS_DIR / f"{ticker}.parquet"
    exist_dates = existing_dates(ticker)

    if not force:
        start_str, end_str = missing_date_range(ticker, days, existing=exist_dates)
        if start_str is None:
            if verbose:
                print(f"  {ticker}: fully covered ({len(exist_dates)} dates), skipping")
            return ticker, "skipped", 0
    else:
        today = date.today()
        start_str = (today - timedelta(days=days + 1)).strftime("%Y-%m-%d")
        end_str = today.strftime("%Y-%m-%d")

    if check_only:
        start_str, end_str = missing_date_range(ticker, days, existing=exist_dates)
        if start_str is None:
            if verbose:
                print(f"  {ticker}: no gaps")
            return ticker, "ok", 0
        else:
            today = date.today()
            start_d = date.fromisoformat(start_str)
            end_d = date.fromisoformat(end_str)
            missing = []
            d = start_d
            while d <= end_d:
                ds = d.strftime("%Y-%m-%d")
                if ds not in exist_dates:
                    missing.append(ds)
                d += timedelta(days=1)
            print(f"  {ticker}: missing {len(missing)} dates: {', '.join(missing[:5])}"
                  f"{'...' if len(missing) > 5 else ''}")
            return ticker, "gaps", len(missing)

    if verbose:
        print(f"  {ticker}: fetching {start_str} → {end_str} from Alpaca...")

    new_df = fetch_bars_alpaca(ticker, start_str, end_str)

    if new_df is None or new_df.empty:
        print(f"  {ticker}: no data returned", file=sys.stderr)
        return ticker, "empty", 0

    new_df = compute_indicators(new_df)
    merged = merge_and_dedup(cache_path, new_df)
    merged = compute_indicators(merged)
    atomic_write(merged, cache_path)

    new_count = len(new_df)
    total_count = len(merged)
    if verbose:
        print(f"  {ticker}: {new_count} new bars → {total_count} total "
              f"({len(merged['timestamp'].dt.date.unique())} dates)")

    return ticker, "ok", new_count


def main():
    parser = argparse.ArgumentParser(
        description="Backfill historical 5-min OHLCV bars via Alpaca Markets"
    )
    parser.add_argument(
        "--tickers", type=str, default="core",
        help="Ticker spec: 'core', 'all', or comma-separated (default: core)",
    )
    parser.add_argument(
        "--days", type=int, default=20,
        help="Number of days to look back (default: 20)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force re-fetch even if data exists",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Check-only mode: show gaps without fetching",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Verbose output",
    )

    args = parser.parse_args()
    tickers = resolve_tickers(args.tickers)

    if not tickers:
        print("ERROR: no tickers resolved from spec: " + args.tickers, file=sys.stderr)
        return 1

    print(f"Backfill: {len(tickers)} tickers, {args.days} days, "
          f"source=Alpaca, {'force' if args.force else 'incremental'}")
    if args.check:
        print(f"CHECK MODE (no data will be fetched)\n")

    total_fetched = 0
    total_errors = 0
    total_skipped = 0

    for ticker in tickers:
        status, label, count = backfill_ticker(
            ticker, args.days, force=args.force,
            check_only=args.check, verbose=args.verbose,
        )
        if status == "ok":
            total_fetched += count
        elif status == "error":
            total_errors += 1
        elif status == "skipped":
            total_skipped += 1

        time.sleep(FETCH_DELAY)

    print(f"\nSummary: {total_fetched} bars fetched, "
          f"{total_skipped} skipped, {total_errors} errors")

    # Exit with error if any ticker failed
    if total_errors > 0:
        return 1
    # Exit with 99 if all skipped (no work to do — same as yfinance version)
    if total_fetched == 0 and total_skipped > 0:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())