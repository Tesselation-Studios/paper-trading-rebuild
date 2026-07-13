#!/usr/bin/env python3
"""Tick preparation — pull current numbers for cron prompt injection.

Usage: python3 scripts/tick_prep.py --agent kairos
Output: JSON blob with positions, P&L, watchlist quotes, regime, and params.

This script is called as the first line of every trading tick cron.
Its output is prepended to the strategy prompt.

Reads from Postgres (primary) with SQLite fallback.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PARAMS_DIR = Path(__file__).resolve().parent.parent / "state"

# Postgres connection (same as paper-trading-teams/src/db.py)
PG_HOST = os.getenv("PGHOST", "docker.klo")
PG_PORT = os.getenv("PGPORT", "5433")
PG_DB = os.getenv("PGDATABASE", "trading")
PG_USER = os.getenv("PGUSER", "trader")
PG_PASSWORD = os.getenv("PGPASSWORD", "trade123")
PG_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}",
)

# SQLite fallback path
SQLITE_PATH = Path("/home/openclaw/projects/paper-trading-teams/shared/trader.db")


def _get_pg_conn():
    """Lazy Postgres connection."""
    import psycopg2
    conn = psycopg2.connect(PG_URL)
    conn.autocommit = True
    return conn


def _get_sl_conn():
    """Read-only SQLite connection for fallback."""
    import sqlite3
    conn = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_positions(agent_id: str) -> list[dict]:
    """Get open positions from Postgres (trader_positions), fallback to SQLite."""
    try:
        pg = _get_pg_conn()
        cur = pg.cursor()
        cur.execute(
            """SELECT DISTINCT ON (ticker) ticker, quantity, avg_entry_price,
                      current_price, unrealized_pl
               FROM trading.trader_positions
               WHERE agent_id = %s AND status = 'open'
               ORDER BY ticker, id DESC""",
            (agent_id,),
        )
        rows = cur.fetchall()
        pg.close()
        return [{"ticker": r[0], "quantity": r[1], "avg_entry_price": r[2],
                 "current_price": r[3], "unrealized_pl": r[4]} for r in rows]
    except Exception as e:
        print(f"[tick_prep] PG positions query failed: {e}, falling back to SQLite", file=sys.stderr)
        conn = _get_sl_conn()
        rows = conn.execute(
            """SELECT ticker, quantity, avg_entry_price, current_price, unrealized_pl
               FROM trader_positions
               WHERE agent_id = ? AND status = 'open'
               ORDER BY ticker""",
            (agent_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]


def get_last_decision(agent_id: str) -> dict | None:
    """Get last decision from Postgres (trader_decisions), fallback to SQLite."""
    try:
        pg = _get_pg_conn()
        cur = pg.cursor()
        cur.execute(
            """SELECT timestamp, action, ticker, quantity
               FROM trading.trader_decisions
               WHERE agent_id = %s
               ORDER BY id DESC LIMIT 1""",
            (agent_id,),
        )
        row = cur.fetchone()
        pg.close()
        if row:
            return {"timestamp": str(row[0]), "action": row[1],
                    "ticker": row[2], "quantity": row[3]}
        return None
    except Exception as e:
        print(f"[tick_prep] PG decisions query failed: {e}, falling back to SQLite", file=sys.stderr)
        conn = _get_sl_conn()
        row = conn.execute(
            """SELECT timestamp, action, ticker, quantity
               FROM trader_decisions
               WHERE agent_id = ?
               ORDER BY id DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None


def get_params(agent_id: str) -> dict:
    params_file = PARAMS_DIR / f"{agent_id}-params.json"
    if params_file.exists():
        return json.loads(params_file.read_text())
    return {}


def compute_summary(positions: list[dict]) -> dict:
    total_value = sum(p["current_price"] * p["quantity"] for p in positions if p["current_price"])
    total_upl = sum(p["unrealized_pl"] or 0 for p in positions)
    return {
        "positions": len(positions),
        "portfolio_value": round(total_value, 2),
        "unrealized_pnl": round(total_upl, 2),
    }


def run_learning_loop(agent_id: str) -> None:
    """Run the learning loop for the given agent after tick prep."""
    import subprocess
    print(f"\n[tick_prep] Running learning loop for {agent_id}...", file=sys.stderr)
    result = subprocess.run(
        [sys.executable, "-m", "src.learning_loop", "--agent", agent_id],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=120,
    )
    if result.returncode == 0:
        print(f"[tick_prep] Learning loop OK for {agent_id}", file=sys.stderr)
    else:
        print(f"[tick_prep] Learning loop FAILED for {agent_id}: {result.stderr[-200:]}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Tick prep for trader cron")
    parser.add_argument("--agent", required=True, help="Agent ID (trader-kairos, trader-stonks, trader-aldridge)")
    parser.add_argument(
        "--run-learning-loop", action="store_true",
        help="Run learning loop analysis after tick prep (post-tick grade/analyze/synthesize)",
    )
    args = parser.parse_args()

    positions = get_positions(args.agent)
    summary = compute_summary(positions)
    last_decision = get_last_decision(args.agent)
    params = get_params(args.agent)

    output = {
        "agent": args.agent,
        "timestamp": datetime.now().isoformat(),
        "summary": summary,
        "positions": positions,
        "last_decision": last_decision,
        "params": params,
    }

    print(json.dumps(output, indent=2, default=str))

    # Post-tick: run learning loop to grade and analyze results
    if args.run_learning_loop:
        run_learning_loop(args.agent)


if __name__ == "__main__":
    main()
