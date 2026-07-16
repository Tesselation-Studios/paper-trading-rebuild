#!/usr/bin/env python3
"""
Tick Producer — fetches market data from the data bus, enqueues ticks
to trading.tick_queue in Postgres.

Tick format: {tick_id, symbol, price, volume, timestamp, source}

Usage:
    python3 src/tick_producer.py                    # runs once
    python3 src/tick_producer.py --dry-run           # print ticks without enqueuing
    python3 src/tick_producer.py --data-bus URL      # override data bus URL

Every run creates a trading.tick_queue table if it doesn't exist.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

log = logging.getLogger("tick_producer")

# ── Defaults ──────────────────────────────────────────────────────────────

DB_DSN = "postgresql://trader:@192.168.1.179:5433/trading"
DATA_BUS_URL = "http://docker.klo:5000/quotes"  # data bus endpoint for current quotes

# Pre-market validation sentinel
PROJECT_DIR = Path(__file__).resolve().parent.parent
PRE_MARKET_SENTINEL = PROJECT_DIR / "state" / ".pre_market_blocked"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tick producer — enqueue market ticks")
    p.add_argument("--db-dsn", default=DB_DSN, help="Postgres DSN")
    p.add_argument(
        "--data-bus", default=DATA_BUS_URL, help="Data bus URL for market quotes"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print ticks without enqueuing to DB",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return p.parse_args(argv)


def fetch_quotes(url: str) -> List[Dict[str, Any]]:
    """Fetch current quotes from the data bus.

    Returns a list of dicts with keys: symbol, price, volume, timestamp, source.
    The data bus /quotes endpoint returns a JSON object like:
        {"quotes": [{"symbol": "SPY", "price": 550.12, "volume": 1000, ...}, ...]}
    """
    log.info("Fetching quotes from %s", url)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:
        log.error("Failed to fetch quotes from %s: %s", url, exc)
        return []

    raw = body if isinstance(body, list) else body.get("quotes", body.get("data", []))
    now = datetime.now(timezone.utc)
    quotes: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        symbol = entry.get("symbol") or entry.get("ticker") or entry.get("s", "")
        if not symbol:
            continue
        quotes.append({
            "tick_id": str(uuid.uuid4()),
            "symbol": symbol,
            "price": float(entry.get("price", entry.get("p", 0.0))),
            "volume": int(entry.get("volume", entry.get("v", 0))),
            "timestamp": entry.get("timestamp", now.isoformat()),
            "source": "data_bus",
        })
    return quotes


def ensure_tick_queue_table(conn) -> None:
    """Create trading.tick_queue table if it doesn't exist."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trading.tick_queue (
            id SERIAL PRIMARY KEY,
            tick_data JSONB NOT NULL,
            status TEXT DEFAULT 'pending',
            error TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            processed_at TIMESTAMPTZ
        )
    """)
    conn.commit()
    cur.close()


def insert_ticks(conn, ticks: List[Dict[str, Any]]) -> int:
    """Insert tick records into trading.tick_queue.

    Returns number of rows inserted.
    """
    cur = conn.cursor()
    rows = [(json.dumps(t),) for t in ticks]
    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO trading.tick_queue (tick_data) VALUES %s",
        rows,
        template="(%s::jsonb)",
    )
    n = cur.rowcount
    conn.commit()
    cur.close()
    return n


def check_pre_market_gate() -> tuple[bool, str]:
    """Check if pre-market format validation has passed.

    Returns (ok, reason). If the sentinel file exists, the gate is blocked
    and ticks must not be enqueued.

    The sentinel is created by scripts/pre_market_gate.py (run via cron at
    9:15 AM ET) and cleared on the next successful validation run.
    """
    if PRE_MARKET_SENTINEL.exists():
        try:
            reason = PRE_MARKET_SENTINEL.read_text().strip()
        except Exception:
            reason = "Unknown validation failure"
        return False, reason
    return True, ""


def main() -> None:
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 0. Pre-market validation gate — block ticks if prompts are broken
    if not args.dry_run:
        gate_ok, gate_reason = check_pre_market_gate()
        if not gate_ok:
            log.error(
                "PRE-MARKET GATE BLOCKED — refusing to enqueue ticks: %s",
                gate_reason,
            )
            log.error(
                "Run 'python3 scripts/validate_prompt_format.py' to diagnose. "
                "Remove state/.pre_market_blocked to override."
            )
            sys.exit(1)

    # 1. Fetch quotes from data bus
    ticks = fetch_quotes(args.data_bus)
    if not ticks:
        log.warning("No ticks fetched from data bus; nothing to enqueue.")
        return

    log.info("Fetched %d tick(s) from data bus", len(ticks))

    if args.dry_run:
        print("── DRY RUN — would insert these ticks ──")
        for t in ticks:
            print(json.dumps(t, indent=2, default=str))
        return

    # 2. Connect to Postgres and enqueue
    conn = psycopg2.connect(args.db_dsn)
    try:
        ensure_tick_queue_table(conn)
        n = insert_ticks(conn, ticks)
        log.info("Enqueued %d tick(s) into trading.tick_queue", n)
    finally:
        conn.close()


if __name__ == "__main__":
    main()