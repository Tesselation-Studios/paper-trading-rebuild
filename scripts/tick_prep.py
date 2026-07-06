#!/usr/bin/env python3
"""Tick preparation — pull current numbers for cron prompt injection.

Usage: python3 scripts/tick_prep.py --agent kairos
Output: JSON blob with positions, P&L, watchlist quotes, regime, and params.

This script is called as the first line of every trading tick cron.
Its output is prepended to the strategy prompt.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "shared" / "trader.db"
PARAMS_DIR = Path(__file__).resolve().parent.parent / "state"


def get_positions(agent_id: str) -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT ticker, quantity, avg_entry_price, current_price, unrealized_pl
           FROM positions
           WHERE agent_id = ? AND status = 'open'
           ORDER BY ticker""",
        (agent_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_last_decision(agent_id: str) -> dict | None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT timestamp, action, ticker, quantity
           FROM decisions
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


def main():
    parser = argparse.ArgumentParser(description="Tick prep for trader cron")
    parser.add_argument("--agent", required=True, help="Agent ID (trader-kairos, trader-stonks, trader-aldridge)")
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


if __name__ == "__main__":
    main()
