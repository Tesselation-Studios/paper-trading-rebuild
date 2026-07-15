#!/usr/bin/env python3
"""
Sync Bars to Postgres — Copy-on-Write Hook

Polls the data bus /quotes endpoint every 60 seconds and inserts new
5-minute OHLCV bars into market_data.bars_5min.

Only inserts rows with new timestamps (checks before insert) to avoid
duplicate work. Can run as a cron job or long-running background process.

Usage:
    # Run as a background daemon
    python3 scripts/sync_bars_to_pg.py

    # Run once (for cron)
    python3 scripts/sync_bars_to_pg.py --once

    # Custom interval
    python3 scripts/sync_bars_to_pg.py --interval 30

    # Custom tickers
    python3 scripts/sync_bars_to_pg.py --tickers AAPL,MSFT,NVDA

Environment:
    DATA_BUS_URL   Data bus base URL (default: http://trading-db:5000)
    PG_HOST        Postgres host (default: trading-db)
    PG_PORT        Postgres port (default: 5432)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

import psycopg2
import psycopg2.extras
import urllib.request
import json

# ── Config ────────────────────────────────────────────────────────────────────

DATA_BUS_URL = os.environ.get("DATA_BUS_URL", "http://trading-db:5000")

PG_HOST = os.environ.get("PG_HOST", "trading-db")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB = os.environ.get("PG_DB", "trading")
PG_USER = os.environ.get("PG_USER", "trader")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
PG_DSN = f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} user={PG_USER}"
if PG_PASSWORD:
    PG_DSN += f" password={PG_PASSWORD}"

# Core tracked tickers
CORE_TICKERS: List[str] = [
    "AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "QQQ", "SPY", "TSLA",
]

# Polling interval (seconds)
DEFAULT_INTERVAL = 60

# Request timeout (seconds)
REQUEST_TIMEOUT = 10

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sync-bars] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sync_bars")


# ═══════════════════════════════════════════════════════════════════════════════
# Data Bus Integration
# ═══════════════════════════════════════════════════════════════════════════════


def fetch_quotes_from_data_bus(symbols: List[str]) -> Dict[str, dict]:
    """Fetch latest quotes from the data bus /quotes endpoint.

    Returns:
        Dict mapping symbol → quote data dict, or empty dict on failure.
        Quote data includes: price, open, high, low, volume, timestamp, rsi, etc.
    """
    symbols_param = ",".join(symbols)
    url = f"{DATA_BUS_URL}/quotes?symbols={symbols_param}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        quotes = data.get("quotes", {})
        if not quotes:
            log.warning("Data bus returned empty quotes for %d symbols", len(symbols))
        return quotes
    except urllib.error.URLError as e:
        log.warning("Data bus unreachable: %s", e)
        return {}
    except json.JSONDecodeError as e:
        log.warning("Data bus returned invalid JSON: %s", e)
        return {}
    except Exception as e:
        log.warning("Data bus fetch failed: %s", e)
        return {}


def extract_bar_from_quote(symbol: str, quote: dict) -> Optional[dict]:
    """Extract a 5-min bar row from a data bus quote response.

    The data bus returns per-symbol quote data including:
      - price (last trade price → close)
      - open, high, low (from quote snapshot)
      - volume (daily cumulative)
      - cached_at (ISO timestamp)

    We use this to construct a 5-min OHLCV bar for insertion.
    Since the data bus doesn't provide true historical 5-min bars,
    we snapshot the current quote and treat it as a synthetic 5-min bar.

    Args:
        symbol: Ticker symbol.
        quote: Quote data dict from data bus.

    Returns:
        Dict with keys: symbol, timestamp, open, high, low, close, volume,
        or None if the quote is missing required fields.
    """
    try:
        timestamp_str = quote.get("cached_at")
        if not timestamp_str:
            return None
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        # Round down to nearest 5-minute boundary
        minute = ts.minute
        rounded_minute = (minute // 5) * 5
        ts = ts.replace(minute=rounded_minute, second=0, microsecond=0)

        open_price = quote.get("open")
        high_price = quote.get("high")
        low_price = quote.get("low")
        close_price = quote.get("price")  # current price → close
        volume = quote.get("volume", 0)

        # If we don't have OHLC data, try to derive from price
        if open_price is None:
            open_price = close_price
        if high_price is None:
            high_price = close_price
        if low_price is None:
            low_price = close_price

        # Validate all required fields
        for field_name, field_val in [
            ("open", open_price), ("high", high_price),
            ("low", low_price), ("close", close_price),
        ]:
            if field_val is None:
                return None
            try:
                float(field_val)
            except (TypeError, ValueError):
                return None

        return {
            "symbol": symbol,
            "timestamp": ts,
            "open": round(float(open_price), 4),
            "high": round(float(high_price), 4),
            "low": round(float(low_price), 4),
            "close": round(float(close_price), 4),
            "volume": int(volume),
        }
    except Exception as e:
        log.debug("  %s: failed to extract bar from quote: %s", symbol, e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Database Operations
# ═══════════════════════════════════════════════════════════════════════════════


def get_pg_connection() -> psycopg2.extensions.connection:
    """Get a psycopg2 connection to Postgres."""
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = False
    return conn


def get_existing_timestamps(
    conn: psycopg2.extensions.connection,
    symbol: str,
    since: datetime,
) -> Set[datetime]:
    """Get existing timestamps for a symbol since a cutoff to avoid re-insertion.

    Returns a set of datetime objects (rounded to 5-min boundaries).
    """
    cur = conn.cursor()
    cur.execute(
        """SELECT timestamp FROM market_data.bars_5min
           WHERE symbol = %s AND timestamp >= %s""",
        (symbol, since),
    )
    existing = {row[0] for row in cur.fetchall()}
    cur.close()
    return existing


def insert_5min_bar(
    conn: psycopg2.extensions.connection,
    bar: dict,
) -> bool:
    """Insert a single 5-min bar. Returns True if inserted, False if skipped."""
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO market_data.bars_5min
               (symbol, timestamp, open, high, low, close, volume)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (symbol, timestamp) DO NOTHING""",
            (
                bar["symbol"],
                bar["timestamp"],
                bar["open"],
                bar["high"],
                bar["low"],
                bar["close"],
                bar["volume"],
            ),
        )
        inserted = cur.rowcount > 0
        conn.commit()
        cur.close()
        return inserted
    except Exception as e:
        log.warning("  %s: insert failed: %s", bar.get("symbol"), e)
        conn.rollback()
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Sync Logic
# ═══════════════════════════════════════════════════════════════════════════════


def sync_once(tickers: List[str]) -> Dict[str, int]:
    """Fetch quotes from data bus and insert new 5-min bars. Returns {symbol: inserted}."""
    results: Dict[str, int] = {}

    # Fetch quotes from data bus
    log.debug("Fetching quotes for %d symbols from data bus...", len(tickers))
    quotes = fetch_quotes_from_data_bus(tickers)

    if not quotes:
        log.warning("No quotes returned from data bus")
        return results

    # Connect to Postgres
    try:
        conn = get_pg_connection()
    except Exception as e:
        log.error("Failed to connect to Postgres: %s", e)
        return results

    # Look back 1 hour for existing timestamps
    since = datetime.now(timezone.utc) - timedelta(hours=1)

    for symbol in tickers:
        quote = quotes.get(symbol)
        if not quote:
            log.debug("  %s: no quote in response", symbol)
            results[symbol] = 0
            continue

        bar = extract_bar_from_quote(symbol, quote)
        if not bar:
            log.debug("  %s: could not extract bar", symbol)
            results[symbol] = 0
            continue

        # Check if this timestamp already exists
        existing = get_existing_timestamps(conn, symbol, since)
        if bar["timestamp"] in existing:
            log.debug("  %s: bar at %s already exists, skipping", symbol, bar["timestamp"])
            results[symbol] = 0
            continue

        inserted = insert_5min_bar(conn, bar)
        results[symbol] = 1 if inserted else 0

    # Also snapshot cache to market_data.cache_snapshots
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO market_data.cache_snapshots (endpoint, data)
               VALUES (%s, %s)""",
            ("quotes", json.dumps(quotes)),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        log.debug("Cache snapshot insert failed (non-critical): %s", e)

    conn.close()
    return results


def run_daemon(tickers: List[str], interval: int):
    """Run sync in a loop with graceful shutdown."""
    shutdown = threading.Event()

    def _on_signal(signum, frame):
        log.info("Received signal %s, shutting down...", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    log.info("Sync daemon started. Pollling %d tickers every %ds from %s",
             len(tickers), interval, DATA_BUS_URL)
    log.info("Target: %s:%s/%s → market_data.bars_5min", PG_HOST, PG_PORT, PG_DB)

    cycle = 0
    while not shutdown.is_set():
        cycle += 1
        start = time.time()

        try:
            results = sync_once(tickers)
            total = sum(results.values())
            if total > 0:
                symbols = [s for s, n in results.items() if n > 0]
                log.info("Cycle #%d: inserted %d new bars (%s)",
                         cycle, total, ", ".join(symbols))
            else:
                log.debug("Cycle #%d: no new bars", cycle)
        except Exception as e:
            log.error("Cycle #%d failed: %s", cycle, e)

        elapsed = time.time() - start
        sleep_time = max(0, interval - elapsed)
        if sleep_time > 0:
            shutdown.wait(sleep_time)

    log.info("Sync daemon stopped after %d cycles", cycle)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="Sync 5-min bars from data bus to Postgres"
    )
    parser.add_argument(
        "--tickers",
        type=str,
        default=",".join(CORE_TICKERS),
        help="Comma-separated ticker symbols (default: 9 core tickers)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"Polling interval in seconds (default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (for cron usage)",
    )
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        print("ERROR: No tickers specified", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  SYNC BARS TO PG — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Tickers:  {', '.join(tickers)}")
    print(f"  Source:   {DATA_BUS_URL}/quotes")
    print(f"  Target:   {PG_HOST}:{PG_PORT}/{PG_DB} → market_data.bars_5min")
    print(f"  Mode:     {'once' if args.once else f'daemon ({args.interval}s interval)'}")
    print(f"{'='*60}\n")

    if args.once:
        results = sync_once(tickers)
        total = sum(results.values())
        print(f"Inserted {total} new bars")
        for symbol, n in sorted(results.items(), key=lambda x: -x[1]):
            if n > 0:
                print(f"  {symbol}: {n}")
    else:
        run_daemon(tickers, args.interval)


if __name__ == "__main__":
    main()
