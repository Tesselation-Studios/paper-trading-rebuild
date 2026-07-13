#!/usr/bin/env python3
# NOTE: Requires yfinance >= 1.0 (use .venv/bin/python3 if system yfinance is < 1.0)
"""
Backfill historical 5-min OHLCV bars for core tickers via Yahoo Finance (yfinance).

Writes to shared/cache/bars/<ticker>.parquet with technical indicators
(RSI, MACD, ATR) computed via pandas_ta.

Idempotent — checks existing dates in Parquet, only fetches missing dates.
Appends via temp file + atomic rename.

Usage:
    python3 scripts/backfill_bars.py --tickers core --days 20
    python3 scripts/backfill_bars.py --tickers AAPL,MSFT --days 30
    python3 scripts/backfill_bars.py --tickers core --days 20 --check   # dry-run
    python3 scripts/backfill_bars.py --tickers all --days 10 --force    # all tickers
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

# ── Dependencies ─────────────────────────────────────────────────────────────
import pandas as pd

# ── Ticker groups ────────────────────────────────────────────────────────────
CORE_TICKERS: List[str] = [
    "SPY", "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOGL", "AMZN",
]

# Extended ticker list from trader configurations (prepopulate_data.py)
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

# Interval for yfinance downloads
INTERVAL = "5m"
# Rate limit: seconds between ticker fetches
FETCH_DELAY = 0.5

# ── Technical indicator parameters ───────────────────────────────────────────
RSI_LENGTH = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ATR_LENGTH = 14


def resolve_tickers(spec: str) -> List[str]:
    """Resolve a ticker spec string to a list of ticker symbols."""
    if spec.lower() == "core":
        return sorted(CORE_TICKERS)
    if spec.lower() == "all":
        all_set: Set[str] = set()
        for tickers in TRADER_TICKERS.values():
            all_set.update(tickers)
        return sorted(all_set)
    return sorted([t.strip().upper() for t in spec.split(",") if t.strip()])


# ── Lazy pandas_ta / yfinance import (may not be available on older Python) ──
_has_pandas_ta: bool = False
try:
    import pandas_ta as ta  # noqa: E402
    _has_pandas_ta = True
except ImportError:
    ta = None  # type: ignore[assignment]


def existing_dates(ticker: str) -> Set[str]:
    """Return the set of date strings (YYYY-MM-DD) already in the Parquet file."""
    path = BARS_DIR / f"{ticker}.parquet"
    if not path.exists():
        return set()
    try:
        df = pd.read_parquet(path, columns=["timestamp"])
        # Convert to date strings
        dates = df["timestamp"].dt.strftime("%Y-%m-%d")
        return set(dates.unique())
    except Exception:
        return set()


def missing_date_range(
    ticker: str,
    days: int,
    existing: Optional[Set[str]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Determine the start/end date range for fetching.

    Returns (start_str, end_str) or (None, None) if fully covered.
    """
    if existing is None:
        existing = existing_dates(ticker)

    today = date.today()
    end_date = today
    start_date = today - timedelta(days=days + 1)

    # Generate all expected dates (calendar days — yfinance handles non-trading)
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
    """Add RSI(14), MACD(12,26,9), and ATR(14) columns to a bars DataFrame."""
    if not _has_pandas_ta:
        for col in ["rsi_14", "macd", "macd_signal", "macd_hist", "atr_14"]:
            df[col] = float("nan")
        return df
    closes = df["close"]
    highs = df["high"]
    lows = df["low"]

    # RSI(14)
    try:
        rsi_series = ta.rsi(closes, length=RSI_LENGTH)
        df["rsi_14"] = pd.to_numeric(rsi_series, errors="coerce").astype("float64")
    except Exception:
        df["rsi_14"] = pd.Series([float("nan")] * len(df), dtype="float64")

    # MACD(12, 26, 9)
    try:
        macd_df = ta.macd(closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
        if macd_df is not None and macd_df.shape[1] >= 3:
            df["macd"] = pd.to_numeric(macd_df.iloc[:, 0], errors="coerce").astype("float64")
            df["macd_signal"] = pd.to_numeric(macd_df.iloc[:, 1], errors="coerce").astype("float64")
            df["macd_hist"] = pd.to_numeric(macd_df.iloc[:, 2], errors="coerce").astype("float64")
        else:
            df["macd"] = pd.array([float("nan")] * len(df), dtype="float64")
            df["macd_signal"] = pd.array([float("nan")] * len(df), dtype="float64")
            df["macd_hist"] = pd.array([float("nan")] * len(df), dtype="float64")
    except Exception:
        df["macd"] = pd.array([float("nan")] * len(df), dtype="float64")
        df["macd_signal"] = pd.array([float("nan")] * len(df), dtype="float64")
        df["macd_hist"] = pd.array([float("nan")] * len(df), dtype="float64")

    # ATR(14)
    try:
        atr_series = ta.atr(highs, lows, closes, length=ATR_LENGTH)
        df["atr_14"] = pd.to_numeric(atr_series, errors="coerce").astype("float64")
    except Exception:
        df["atr_14"] = pd.Series([float("nan")] * len(df), dtype="float64")

    return df


def fetch_bars(ticker: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """Fetch 5-min OHLCV bars from Yahoo Finance."""
    import yfinance as yf  # lazy import
    try:
        df = yf.download(
            ticker,
            start=start,
            end=end,
            interval=INTERVAL,
            progress=False,
            auto_adjust=True,
        )

        if df is None or df.empty:
            return None

        # yfinance returns multi-level columns; flatten them
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)

        # Reset index to get timestamp as a column
        df = df.reset_index()

        # yfinance may use "Datetime" or "Date" as the column name
        ts_col = "Datetime" if "Datetime" in df.columns else "Date"
        if ts_col not in df.columns and "timestamp" not in df.columns:
            # Try the first column
            ts_col = df.columns[0]

        # Rename columns to match our schema
        rename_map = {}
        if ts_col in df.columns:
            rename_map[ts_col] = "timestamp"
        for col in df.columns:
            cl = col.lower()
            if cl in ("open", "high", "low", "close", "volume"):
                rename_map[col] = cl

        df = df.rename(columns=rename_map)

        # Ensure required columns exist
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        if not required.issubset(set(df.columns)):
            missing_cols = required - set(df.columns)
            print(f"  WARNING: {ticker} missing columns: {missing_cols}", file=sys.stderr)
            return None

        # Keep only required columns
        df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()

        # Ensure UTC timezone
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
        else:
            df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")

        # Drop rows with all-NaN OHLCV
        df = df.dropna(subset=["open", "high", "low", "close"])

        # Ensure dtypes
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].fillna(0).astype("float64")

        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    except Exception as e:
        print(f"  ERROR fetching {ticker}: {e}", file=sys.stderr)
        return None


def merge_and_dedup(
    existing_path: Path,
    new_df: pd.DataFrame,
) -> pd.DataFrame:
    """Merge new bars with existing data, deduplicating on timestamp.

    Newer timestamps overwrite older ones. Returns the merged DataFrame.
    """
    if existing_path.exists():
        existing_df = pd.read_parquet(existing_path)
        # Ensure same columns
        for col in new_df.columns:
            if col not in existing_df.columns:
                existing_df[col] = float("nan")
        for col in existing_df.columns:
            if col not in new_df.columns and col != "timestamp":
                new_df[col] = float("nan")

        combined = pd.concat([existing_df, new_df], ignore_index=True)
        # Deduplicate: keep last (newest) for each timestamp
        combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
        combined = combined.sort_values("timestamp").reset_index(drop=True)
        return combined
    else:
        return new_df.sort_values("timestamp").reset_index(drop=True)


def atomic_write(df: pd.DataFrame, final_path: Path) -> None:
    """Write DataFrame to Parquet via temp file + atomic rename."""
    fd, tmp_path = tempfile.mkstemp(
        suffix=".parquet",
        prefix=f"{final_path.stem}_",
        dir=str(final_path.parent),
    )
    os.close(fd)
    tmp = Path(tmp_path)

    try:
        df.to_parquet(tmp, index=False)

        # Verify the file can be read back
        _verify = pd.read_parquet(tmp)
        if _verify is None or _verify.empty:
            raise RuntimeError(f"Verification read of {tmp} returned empty DataFrame")

        # Atomic rename
        tmp.rename(final_path)
    except Exception:
        # Clean up temp file on error
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
    """Backfill a single ticker.

    Returns: (ticker, status, bar_count)
        status: "ok", "skipped", "empty", "error"
        bar_count: number of bars fetched (0 if skipped/empty/error)
    """
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
            # Show which dates are missing
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
        print(f"  {ticker}: fetching {start_str} → {end_str}...")

    new_df = fetch_bars(ticker, start_str, end_str)

    if new_df is None or new_df.empty:
        print(f"  {ticker}: no data returned", file=sys.stderr)
        return ticker, "empty", 0

    # Compute technical indicators on the new data
    new_df = compute_indicators(new_df)

    # Merge with existing data (dedup on timestamp)
    merged = merge_and_dedup(cache_path, new_df)

    # Recompute indicators on the full merged dataset so boundary rows
    # get proper values (indicators computed only on new data would leave
    # NaN gaps at boundaries).
    merged = compute_indicators(merged)

    # Atomic write
    atomic_write(merged, cache_path)

    new_count = len(new_df)
    total_count = len(merged)
    if verbose:
        print(f"  {ticker}: {new_count} new bars → {total_count} total "
              f"({len(merged['timestamp'].dt.date.unique())} dates)")

    return ticker, "ok", new_count


def main():
    parser = argparse.ArgumentParser(
        description="Backfill historical 5-min OHLCV bars via Yahoo Finance"
    )
    parser.add_argument(
        "--tickers",
        type=str,
        default="core",
        help="Ticker spec: 'core' (8 tickers), 'all' (30+), or comma-separated "
             "like 'AAPL,MSFT,NVDA' (default: core)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=20,
        help="Number of calendar days to backfill (default: 20)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry-run: show which (ticker, date) pairs are missing, do not fetch",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-fetch all dates (ignore existing data freshness)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output",
    )
    args = parser.parse_args()

    tickers = resolve_tickers(args.tickers)

    print(f"\n{'='*60}")
    mode = "CHECK (dry-run)" if args.check else "BACKFILL"
    print(f"  {mode} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Tickers: {', '.join(tickers)}")
    print(f"  Days:    {args.days}")
    print(f"  Force:   {args.force}")
    print(f"  Output:  {BARS_DIR}")
    print(f"{'='*60}\n")

    results: Dict[str, List[str]] = {"ok": [], "skipped": [], "empty": [], "error": [], "gaps": []}
    total_bars = 0
    total_start = time.time()

    for i, ticker in enumerate(tickers):
        ticker_name, status, bar_count = backfill_ticker(
            ticker,
            days=args.days,
            force=args.force,
            check_only=args.check,
            verbose=args.verbose or args.check,
        )
        results.setdefault(status, []).append(ticker_name)
        total_bars += bar_count

        # Rate limiting between ticker fetches (skip for check-only)
        if not args.check and i < len(tickers) - 1:
            time.sleep(FETCH_DELAY)

    elapsed = time.time() - total_start

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  SUMMARY — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Elapsed: {elapsed:.1f}s")
    print(f"{'='*60}")

    if args.check:
        total_gaps = sum(1 for s in results.get("gaps", []))
        ok_clean = len(results.get("ok", []))
        print(f"  ✓ No gaps:  {ok_clean} tickers")
        print(f"  ⚠ Has gaps: {total_gaps} tickers")
        for ticker in results.get("gaps", []):
            exist_dates = existing_dates(ticker)
            start_s, end_s = missing_date_range(ticker, args.days, existing=exist_dates)
            if start_s:
                print(f"      {ticker}: needs {start_s} → {end_s}")
    else:
        print(f"  ✓ Fetched: {len(results.get('ok', []))} tickers ({total_bars:,} new bars)")
        print(f"  ⏭ Skipped: {len(results.get('skipped', []))} tickers (fully covered)")
        if results.get("empty"):
            print(f"  ⚠ Empty:   {len(results['empty'])} tickers ({', '.join(results['empty'])})")
        if results.get("error"):
            print(f"  ✗ Errors:  {len(results['error'])} tickers ({', '.join(results['error'])})")

        # Show cache stats
        total_size = 0
        file_count = 0
        for f in BARS_DIR.glob("*.parquet"):
            total_size += f.stat().st_size
            file_count += 1
        if total_size > 1_000_000:
            size_str = f"{total_size / 1_000_000:.1f} MB"
        elif total_size > 1_000:
            size_str = f"{total_size / 1_000:.1f} KB"
        else:
            size_str = f"{total_size} B"
        print(f"\n  💾 Cache:  {file_count} parquet files, {size_str}")

    print()

    # Exit non-zero if any critical errors
    if results.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
