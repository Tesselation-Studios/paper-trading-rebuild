"""Data Fetcher — 24/7 market data collection for the trading pipeline.

Backfills 2 years of bars for Kairos (technical data), then runs as a
daemon collecting live data. Stores everything in Postgres.

Usage:
    python3 -m src.data_fetcher backfill --ticker AAPL --years 2
    python3 -m src.data_fetcher daemon          # run forever
    python3 -m src.data_fetcher fetch-news      # one-shot news fetch
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2

from src.db.connection import (
    get_connection,
    insert_bars_batch,
)

log = logging.getLogger("data_fetcher")


# Default tickers for Kairos (momentum trader — technical data)
KAIROS_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "SPY", "QQQ", "IWM", "DIA",
    "JPM", "V", "WMT", "JNJ", "XOM", "UNH", "MA", "HD", "PG",
]


# ── yfinance fallback ─────────────────────────────────────────────────────────


def fetch_bars_yfinance(
    ticker: str,
    start: str,
    end: str,
    interval: str = "5min",
) -> List[Dict[str, Any]]:
    """Fetch historical bars using yfinance (free, no API key needed).

    Args:
        ticker: Stock symbol.
        start: Start date YYYY-MM-DD.
        end: End date YYYY-MM-DD.
        interval: Bar interval (1m, 5m, 15m, 1h, 1d).

    Returns:
        List of dicts with keys: timestamp, open, high, low, close, volume.
    """
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed. Run: pip install yfinance")
        return []

    # yfinance interval mapping
    yf_interval = {"1min": "1m", "5min": "5m", "15min": "15m",
                   "1hour": "1h", "1day": "1d"}.get(interval, "5m")

    try:
        stock = yf.Ticker(ticker)
        df = stock.history(start=start, end=end, interval=yf_interval)
        if df.empty:
            log.warning("No data for %s %s-%s @ %s", ticker, start, end, interval)
            return []

        bars = []
        for ts, row in df.iterrows():
            bars.append({
                "timestamp": ts.isoformat(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": int(row["Volume"]),
            })
        log.info("Fetched %d bars for %s (%s-%s)", len(bars), ticker, start, end)
        return bars
    except Exception as e:
        log.error("yfinance fetch failed for %s: %s", ticker, e)
        return []


# ── Backfill ──────────────────────────────────────────────────────────────────


def backfill_ticker(
    ticker: str,
    years: int = 2,
    interval: str = "1day",
    conn: Optional[psycopg2.extensions.connection] = None,
) -> int:
    """Backfill historical bars for a ticker.

    Args:
        ticker: Stock symbol.
        years: How many years to backfill.
        interval: Bar interval.
        conn: Database connection (creates new if None).

    Returns:
        Number of bars inserted.
    """
    close_conn = conn is None
    if close_conn:
        conn = get_connection()

    end = datetime.now()
    start = end - timedelta(days=years * 365)

    bars = fetch_bars_yfinance(
        ticker,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        interval=interval,
    )

    if not bars:
        if close_conn:
            conn.close()
        return 0

    rows = []
    for b in bars:
        rows.append((
            ticker,
            b["timestamp"],
            interval,
            b["open"],
            b["high"],
            b["low"],
            b["close"],
            b["volume"],
            "yfinance",
        ))

    count = insert_bars_batch(conn, rows)
    log.info("Backfilled %d bars for %s (%d years, %s)", count, ticker, years, interval)

    if close_conn:
        conn.close()

    return count


def backfill_all(
    tickers: List[str],
    years: int = 2,
    interval: str = "1day",
) -> Dict[str, int]:
    """Backfill bars for all tickers. Returns {ticker: count}."""
    conn = get_connection()
    results = {}

    for i, ticker in enumerate(tickers):
        log.info("[%d/%d] Backfilling %s...", i + 1, len(tickers), ticker)
        count = backfill_ticker(ticker, years=years, interval=interval, conn=conn)
        results[ticker] = count
        time.sleep(0.5)  # rate limit

    conn.close()
    return results


# ── Daemon ────────────────────────────────────────────────────────────────────


def run_daemon(
    tickers: List[str] = None,
    interval_minutes: int = 5,
) -> None:
    """Run as a daemon, fetching live bars every N minutes.

    Args:
        tickers: Tickers to monitor.
        interval_minutes: Polling interval.
    """
    if tickers is None:
        tickers = KAIROS_TICKERS

    conn = get_connection()
    log.info("Data fetcher daemon started. %d tickers, every %d min",
             len(tickers), interval_minutes)

    while True:
        try:
            for ticker in tickers:
                # Fetch last hour of 5-min bars
                end = datetime.now()
                start = end - timedelta(hours=1)
                bars = fetch_bars_yfinance(
                    ticker,
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    interval="5min",
                )

                if bars:
                    rows = []
                    for b in bars[-12:]:  # last 12 bars (1 hour)
                        rows.append((
                            ticker, b["timestamp"], "5min",
                            b["open"], b["high"], b["low"], b["close"],
                            b["volume"], "yfinance",
                        ))
                    count = insert_bars_batch(conn, rows)
                    if count > 0:
                        log.debug("%s: %d new bars", ticker, count)

                time.sleep(0.2)  # rate limit between tickers

            log.debug("Sleeping %d min...", interval_minutes)
            time.sleep(interval_minutes * 60)

        except KeyboardInterrupt:
            log.info("Daemon stopped")
            break
        except Exception as e:
            log.error("Daemon error: %s", e)
            time.sleep(60)

    conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Data Fetcher — market data collection")
    sub = parser.add_subparsers(dest="command")

    backfill_p = sub.add_parser("backfill", help="Backfill historical bars")
    backfill_p.add_argument("--ticker", default="AAPL")
    backfill_p.add_argument("--all-kairos", action="store_true",
                            help="Backfill all Kairos tickers")
    backfill_p.add_argument("--years", type=int, default=2)
    backfill_p.add_argument("--interval", default="1day")

    daemon_p = sub.add_parser("daemon", help="Run live data collection daemon")
    daemon_p.add_argument("--interval", type=int, default=5,
                          help="Polling interval in minutes")

    fetch_news_p = sub.add_parser("fetch-news", help="One-shot news fetch")
    fetch_news_p.add_argument("--ticker", default="AAPL")

    status_p = sub.add_parser("status", help="Show DB stats")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    if args.command == "backfill":
        if getattr(args, "all_kairos", False):
            results = backfill_all(KAIROS_TICKERS, years=args.years, interval=args.interval)
            total = sum(results.values())
            print(f"Backfilled {total} total bars across {len(results)} tickers")
            for t, c in sorted(results.items(), key=lambda x: -x[1]):
                print(f"  {t}: {c} bars")
        else:
            count = backfill_ticker(args.ticker, years=args.years, interval=args.interval)
            print(f"Backfilled {count} bars for {args.ticker}")

    elif args.command == "daemon":
        run_daemon(interval_minutes=args.interval)

    elif args.command == "fetch-news":
        print("News fetching requires Finnhub/AlphaVantage API key. Not yet implemented.")
        print("Set FINNHUB_API_KEY or ALPHA_VANTAGE_API_KEY env var.")

    elif args.command == "status":
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM market_data.bars")
            bars = cur.fetchone()[0]
            cur.execute("SELECT count(DISTINCT ticker) FROM market_data.bars")
            tickers = cur.fetchone()[0]
            cur.execute("SELECT min(timestamp), max(timestamp) FROM market_data.bars")
            min_ts, max_ts = cur.fetchone()
        conn.close()
        print(f"market_data.bars: {bars} rows, {tickers} tickers")
        if min_ts:
            print(f"  Range: {min_ts} → {max_ts}")


if __name__ == "__main__":
    main()
