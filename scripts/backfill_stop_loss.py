#!/usr/bin/env python3
"""Backfill stop_loss for all open trades that have NULL stop_loss.

Bug: sync_trades.py (#42) never populated stop_loss when creating BUY trades.
All Aldridge (22) and Stonks (2) open positions have NULL stop_loss.

Computed as: entry_price * (1 - default_stop_loss_pct)
Default from config/risk.yaml: 0.05 (5%)

Usage:
    python3 scripts/backfill_stop_loss.py [--dry-run]
"""

import os
import sys
import argparse
import psycopg2
from decimal import Decimal
from pathlib import Path

# ── DB config ────────────────────────────────────────────────────────────────

DB_HOST = os.environ.get("DOCKER_HOST", "192.168.1.179")
DB_PORT = int(os.environ.get("POSTGRES_PORT", "5433"))
DB_NAME = "trading"
DB_USER = "trader"
DB_PASSWORD = os.environ.get("TRADER_DB_PASSWORD", "trader-dev-2026")

STOP_LOSS_PCT = Decimal("0.05")  # 5% from config/risk.yaml


def backfill(dry_run: bool = False) -> int:
    """Backfill stop_loss for all open trades with NULL stop_loss."""
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
    )
    cur = conn.cursor()

    # Find open trades without stop_loss
    cur.execute("""
        SELECT id, agent_id, ticker, entry_price
        FROM trading.executed_trades
        WHERE status = 'open' AND stop_loss IS NULL
        ORDER BY id
    """)
    rows = cur.fetchall()

    if not rows:
        print("[backfill_stop_loss] No open trades without stop_loss — nothing to do.")
        cur.close()
        conn.close()
        return 0

    print(f"[backfill_stop_loss] Found {len(rows)} open trades without stop_loss:")
    for r in rows:
        entry = Decimal(str(r[3]))
        stop = round(entry * (Decimal("1") - STOP_LOSS_PCT), 2)
        print(f"  {r[1]} | {r[2]} | entry=${entry} → stop=${stop}")

    if dry_run:
        print("\n[backfill_stop_loss] DRY RUN — no changes made.")
        cur.close()
        conn.close()
        return len(rows)

    # Update all in one batch
    updated = 0
    for r in rows:
        entry = Decimal(str(r[3]))
        stop = round(entry * (Decimal("1") - STOP_LOSS_PCT), 2)
        cur.execute(
            "UPDATE trading.executed_trades SET stop_loss = %s WHERE id = %s",
            (stop, r[0]),
        )
        updated += 1

    conn.commit()
    print(f"\n[backfill_stop_loss] Updated {updated} trades with stop_loss.")
    cur.close()
    conn.close()
    return updated


def main():
    parser = argparse.ArgumentParser(description="Backfill stop_loss for open trades")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()
    count = backfill(dry_run=args.dry_run)
    return 0 if count >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
