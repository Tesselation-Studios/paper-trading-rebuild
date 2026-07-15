#!/usr/bin/env python3
"""
Backfill market data into Postgres for historical testing and replay.

Uses Alpaca API (paper trading keys) to pull 2+ years of daily bars and
3 days of 5-min bars for core tickers. Stores in dedicated market_data
tables (bars_1d, bars_5min).

Usage:
    python3 scripts/backfill_market_data.py
    python3 scripts/backfill_market_data.py --tickers AAPL,MSFT,NVDA
    python3 scripts/backfill_market_data.py --days-5min 7
    python3 scripts/backfill_market_data.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# ── Config ────────────────────────────────────────────────────────────────────

# Postgres connection (docker.klo external port)
PG_HOST = os.environ.get("PG_HOST", "trading-db")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB = os.environ.get("PG_DB", "trading")
PG_USER = os.environ.get("PG_USER", "trader")
PG_DSN = f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} user={PG_USER}"

# Core tracked tickers
CORE_TICKERS: List[str] = [
    "AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "QQQ", "SPY", "TSLA",
]

# Rate limit between ticker fetches (seconds)
FETCH_DELAY = 0.3

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [backfill] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backfill")


# ═══════════════════════════════════════════════════════════════════════════════
# Alpaca Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def get_alpaca_credentials() -> Tuple[Optional[str], Optional[str]]:
    """Get Alpaca API credentials from environment."""
    candidates = [
        ("ALPACA_KAIROS_KEY", "ALPACA_KAIROS_SECRET"),
        ("KAIROS_API_KEY", "KAIROS_SECRET_KEY"),
        ("ALPACA_ALDRIDGE_KEY", "ALPACA_ALDRIDGE_SECRET"),
        ("ALDRIDGE_API_KEY", "ALDRIDGE_SECRET_KEY"),
        ("ALPACA_STONKS_KEY", "ALPACA_STONKS_SECRET"),
        ("STONKS_API_KEY", "STONKS_SECRET_KEY"),
    ]
    for key_var, sec_var in candidates:
        k = os.getenv(key_var)
        s = os.getenv(sec_var)
        if k and s:
            return k, s
    return None, None


def fetch_bars_alpaca(
    client: StockHistoricalDataClient,
    ticker: str,
    start: str,
    end: str,
    interval: str = "1d",
) -> List[Dict]:
    """Fetch OHLCV bars from Alpaca.

    Args:
        client: StockHistoricalDataClient instance.
        ticker: Stock symbol.
        start: Start date YYYY-MM-DD.
        end: End date YYYY-MM-DD.
        interval: '1d' or '5m'.

    Returns:
        List of dicts with keys: timestamp, open, high, low, close, volume.
    """
    if interval == "5m":
        tf = TimeFrame(5, TimeFrameUnit.Minute)
    elif interval == "1d":
        tf = TimeFrame.Day
    elif interval == "1h":
        tf = TimeFrame.Hour
    else:
        tf = TimeFrame.Day

    try:
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=tf,
            start=start,
            end=end,
        )
        resp = client.get_stock_bars(req)

        bars = []
        if ticker in resp.data:
            for bar in resp.data[ticker]:
                bars.append({
                    "timestamp": bar.timestamp,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": int(bar.volume),
                })
        log.info("  %s: fetched %d bars (%s-%s @ %s)", ticker, len(bars), start, end, interval)
        return bars
    except Exception as e:
        log.error("  %s: Alpaca fetch failed: %s", ticker, e)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Database Operations
# ═══════════════════════════════════════════════════════════════════════════════


def get_pg_connection() -> psycopg2.extensions.connection:
    """Get a psycopg2 connection to Postgres."""
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = False
    return conn


def insert_daily_bars(
    conn: psycopg2.extensions.connection,
    symbol: str,
    bars: List[Dict],
) -> int:
    """Insert daily bars into market_data.bars_1d with ON CONFLICT DO NOTHING."""
    if not bars:
        return 0

    rows = [
        (symbol, b["timestamp"].date(), b["open"], b["high"], b["low"], b["close"], b["volume"])
        for b in bars
    ]

    cur = conn.cursor()
    psycopg2.extras.execute_values(
        cur,
        """INSERT INTO market_data.bars_1d
           (symbol, date, open, high, low, close, volume)
           VALUES %s
           ON CONFLICT (symbol, date) DO NOTHING""",
        rows,
        template="(%s, %s, %s, %s, %s, %s, %s)",
    )
    n = cur.rowcount
    conn.commit()
    cur.close()
    return n


def insert_5min_bars(
    conn: psycopg2.extensions.connection,
    symbol: str,
    bars: List[Dict],
) -> int:
    """Insert 5-min bars into market_data.bars_5min with ON CONFLICT DO NOTHING."""
    if not bars:
        return 0

    rows = [
        (symbol, b["timestamp"], b["open"], b["high"], b["low"], b["close"], b["volume"])
        for b in bars
    ]

    cur = conn.cursor()
    psycopg2.extras.execute_values(
        cur,
        """INSERT INTO market_data.bars_5min
           (symbol, timestamp, open, high, low, close, volume)
           VALUES %s
           ON CONFLICT (symbol, timestamp) DO NOTHING""",
        rows,
        template="(%s, %s::timestamptz, %s, %s, %s, %s, %s)",
    )
    n = cur.rowcount
    conn.commit()
    cur.close()
    return n


# ═══════════════════════════════════════════════════════════════════════════════
# Backfill Orchestration
# ═══════════════════════════════════════════════════════════════════════════════


def backfill_ticker(
    ticker: str,
    client: StockHistoricalDataClient,
    conn: psycopg2.extensions.connection,
    years_daily: int = 2,
    days_5min: int = 3,
) -> Tuple[int, int]:
    """Backfill one ticker. Returns (daily_inserted, 5min_inserted)."""
    end = datetime.now(timezone.utc)
    daily_start = end - timedelta(days=years_daily * 365 + 10)
    min5_start = end - timedelta(days=days_5min + 2)

    end_str = end.strftime("%Y-%m-%d")
    daily_start_str = daily_start.strftime("%Y-%m-%d")
    min5_start_str = min5_start.strftime("%Y-%m-%d")

    # Fetch and insert daily bars
    daily_bars = fetch_bars_alpaca(client, ticker, daily_start_str, end_str, "1d")
    daily_inserted = insert_daily_bars(conn, ticker, daily_bars)

    # Fetch and insert 5-min bars
    min5_bars = fetch_bars_alpaca(client, ticker, min5_start_str, end_str, "5m")
    min5_inserted = insert_5min_bars(conn, ticker, min5_bars)

    return daily_inserted, min5_inserted


def main():
    parser = argparse.ArgumentParser(
        description="Backfill market data into Postgres (bars_1d + bars_5min) via Alpaca"
    )
    parser.add_argument(
        "--tickers",
        type=str,
        default=",".join(CORE_TICKERS),
        help="Comma-separated ticker symbols (default: 9 core tickers)",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=2,
        help="Years of daily bars to backfill (default: 2)",
    )
    parser.add_argument(
        "--days-5min",
        type=int,
        default=3,
        help="Days of 5-min bars to backfill (default: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done, do not insert",
    )
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        print("ERROR: No tickers specified", file=sys.stderr)
        sys.exit(1)

    # ── Alpaca credentials ────────────────────────────────────────────────
    api_key, secret_key = get_alpaca_credentials()
    if not api_key or not secret_key:
        print("ERROR: No Alpaca credentials found.", file=sys.stderr)
        print("Set ALPACA_KAIROS_KEY/ALPACA_KAIROS_SECRET or similar env vars.", file=sys.stderr)
        sys.exit(1)

    client = StockHistoricalDataClient(api_key, secret_key)
    log.info("Alpaca client initialized")

    print(f"\n{'='*60}")
    print(f"  MARKET DATA BACKFILL — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Tickers:  {', '.join(tickers)}")
    print(f"  Daily:    {args.years} years → market_data.bars_1d")
    print(f"  5-min:    {args.days_5min} days → market_data.bars_5min")
    print(f"  Source:   Alpaca API (paper trading)")
    print(f"  Postgres: {PG_HOST}:{PG_PORT}/{PG_DB}")
    print(f"  Dry run:  {args.dry_run}")
    print(f"{'='*60}\n")

    if args.dry_run:
        print("[DRY RUN] Checking data availability...\n")
        for ticker in tickers:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=args.years * 365 + 10)
            bars = fetch_bars_alpaca(
                client, ticker,
                start.strftime("%Y-%m-%d"),
                end.strftime("%Y-%m-%d"),
                "1d",
            )
            print(f"  {ticker}: {len(bars)} daily bars available ({start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')})")
        print("\n[Dry run complete — no data inserted]")
        return

    # ── Connect to Postgres ───────────────────────────────────────────────
    try:
        conn = get_pg_connection()
        log.info("Connected to Postgres at %s:%s/%s", PG_HOST, PG_PORT, PG_DB)
    except Exception as e:
        log.error("Failed to connect to Postgres: %s", e)
        sys.exit(1)

    # ── Backfill each ticker ──────────────────────────────────────────────
    results: Dict[str, Dict[str, int]] = {}
    total_daily = 0
    total_5min = 0
    start_time = time.time()

    for i, ticker in enumerate(tickers):
        print(f"[{i+1}/{len(tickers)}] {ticker} ...", end=" ", flush=True)
        try:
            daily_n, min5_n = backfill_ticker(
                ticker, client, conn,
                years_daily=args.years,
                days_5min=args.days_5min,
            )
            results[ticker] = {"daily": daily_n, "5min": min5_n}
            total_daily += daily_n
            total_5min += min5_n
            print(f"✓ daily={daily_n} 5min={min5_n}")
        except Exception as e:
            log.error("✗ %s failed: %s", ticker, e)
            results[ticker] = {"daily": 0, "5min": 0, "error": str(e)}
            print("✗ FAILED")

        # Rate limit
        if i < len(tickers) - 1:
            time.sleep(FETCH_DELAY)

    conn.close()
    elapsed = time.time() - start_time

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  BACKFILL SUMMARY — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Elapsed: {elapsed:.1f}s")
    print(f"  Total daily bars inserted:  {total_daily}")
    print(f"  Total 5-min bars inserted:  {total_5min}")
    print(f"{'='*60}")
    print(f"  {'Symbol':<8} {'Daily':>8} {'5-min':>8}")
    print(f"  {'-'*8} {'-'*8} {'-'*8}")
    for ticker in tickers:
        r = results.get(ticker, {"daily": 0, "5min": 0})
        daily_str = f"{r['daily']:,}" if r.get('daily', 0) > 0 else "ERROR" if 'error' in r else "0"
        min5_str = f"{r['5min']:,}" if r.get('5min', 0) > 0 else "ERROR" if 'error' in r else "0"
        print(f"  {ticker:<8} {daily_str:>8} {min5_str:>8}")
    print()

    # Exit non-zero if any ticker had errors
    if any("error" in r for r in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
