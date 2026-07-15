#!/usr/bin/env python3
"""
Migrate live trading data from SQLite (OpenClaw) → Postgres (docker.klo).

SAFE: Read-only on SQLite. Uses ON CONFLICT (idempotent — can re-run).
Does NOT touch live traders. Run manually, not from cron.

Usage:
    python3 scripts/migrate_sqlite_to_pg.py --dry-run
    python3 scripts/migrate_sqlite_to_pg.py
    python3 scripts/migrate_sqlite_to_pg.py --table trades
    python3 scripts/migrate_sqlite_to_pg.py --pull     # auto-copy from OpenClaw
    SQLITE_PATH=/tmp/trader.db python3 scripts/migrate_sqlite_to_pg.py
"""

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import execute_values

# ── Config ────────────────────────────────────────────────────────────────────
SQLITE_PATH = os.environ.get(
    "SQLITE_PATH",
    "/home/openclaw/projects/paper-trading-teams/shared/trader.db",
)
OPENCLAW_HOST = "192.168.1.41"
OPENCLAW_USER = "openclaw"
PG_HOST = os.getenv("PG_HOST", "trading-db")
PG_PORT = 5433
PG_DB = "trading"
PG_USER = "trader"
PG_PASSWORD = "trader-dev-2026"

# Tables that exist in both SQLite and Postgres (with schema prefix)
# Format: (sqlite_table, pg_schema_table, column_mapping)
# column_mapping: None = auto-map matching column names
MIGRATION_TABLES: list[dict[str, Any]] = [
    # Core trading
    {
        "sqlite": "trades",
        "pg": "trading.trades",
        "map": {
            "trader_id": "agent_id",
            "trade_id": "id",
            "ticker": "ticker",
            "entry_time": "entry_timestamp",
            "exit_time": "exit_timestamp",
            "entry_price": "entry_price",
            "exit_price": "exit_price",
            "shares": "quantity",
            "pnl": "pnl",
            "return_pct": "pnl_pct",
            "buy_decision_id": "decision_id",
        },
        "on_conflict": "DO NOTHING",
    },
    {
        "sqlite": "decisions",
        "pg": "trading.decisions",
        "map": {
            "trader_id": "agent_id",
            "ticker": "ticker",
            "timestamp": "timestamp",
            "decision": "action",
            "conviction": "confidence",
            "rationale": "thesis",
        },
        "defaults": {
            "ticker": "",
            "conviction": 0,
        },
        "on_conflict": "DO NOTHING",
    },
    {
        "sqlite": "decisions",
        "pg": "trading.trader_decisions",
        "map": {
            "agent_id": "agent_id",
            "timestamp": "timestamp",
            "action": "action",
            "ticker": "ticker",
            "quantity": "quantity",
            "confidence": "confidence",
            "thesis": "thesis",
            "mood": "mood",
            "source": "source",
        },
        "on_conflict": "DO NOTHING",
    },
    {
        "sqlite": "journal",
        "pg": "trading.trader_journal",
        "map": {
            "agent_id": "agent_id",
            "timestamp": "timestamp",
            "mood": "mood",
            "entry": "entry",
            "confidence": "confidence",
            "source": "source",
        },
        "on_conflict": "DO NOTHING",
    },
    {
        "sqlite": "journal",
        "pg": "trading.journal",
        "map": {
            "trader_id": "agent_id",
            "timestamp": "timestamp",
            "ticker": "__default__",  # not in SQLite, use default
            "decision": "mood",
            "rationale": "entry",
            "equity": "__default__",
            "drawdown_pct": "__default__",
        },
        "defaults": {
            "ticker": "",
            "decision": "",
            "equity": 100000,
            "drawdown_pct": 0,
        },
        "on_conflict": "DO NOTHING",
    },
    # Agent state
    {
        "sqlite": "agent_state",
        "pg": "trading.agent_state",
        "map": {
            "agent_id": "agent_id",
            "name": "name",
            "current_portfolio_value": "current_portfolio_value",
            "unrealized_pnl": "unrealized_pnl",
            "ytd_pnl": "ytd_pnl",
            "win_rate": "win_rate",
            "wins": "wins",
            "losses": "losses",
            "total_trades": "total_trades",
            "updated_at": "updated_at",
        },
        "on_conflict": "(agent_id) DO UPDATE SET current_portfolio_value=EXCLUDED.current_portfolio_value, unrealized_pnl=EXCLUDED.unrealized_pnl, updated_at=EXCLUDED.updated_at",
    },
    {
        "sqlite": "agent_profile",
        "pg": "trading.agent_profile",
        "map": {
            "agent_id": "agent_id",
            "name": "name",
            "company": "company",
            "tagline": "tagline",
            "identity": "identity",
            "current_state": "current_state",
            "performance": "performance",
            "strategic_focus": "strategic_focus",
            "updated_at": "updated_at",
        },
        "on_conflict": "(agent_id) DO UPDATE SET name=EXCLUDED.name, current_state=EXCLUDED.current_state, updated_at=EXCLUDED.updated_at",
    },
    {
        "sqlite": "positions",
        "pg": "trading.trader_positions",
        "map": {
            "agent_id": "agent_id",
            "ticker": "ticker",
            "quantity": "quantity",
            "avg_entry_price": "avg_entry_price",
            "current_price": "current_price",
            "market_value": "market_value",
            "unrealized_pl": "unrealized_pl",
            "stop_loss": "stop_loss",
            "status": "status",
            "opened_at": "opened_at",
            "closed_at": "closed_at",
            "exit_condition": "exit_condition",
        },
        "on_conflict": "DO NOTHING",
    },
    {
        "sqlite": "positions",
        "pg": "trading.trader_positions",
        "map": {  # duplicate to catch both positions tables
            "agent_id": "agent_id",
            "ticker": "ticker",
            "quantity": "quantity",
            "avg_entry_price": "avg_entry_price",
            "current_price": "current_price",
            "market_value": "market_value",
            "unrealized_pl": "unrealized_pl",
            "stop_loss": "stop_loss",
            "status": "status",
            "opened_at": "opened_at",
            "closed_at": "closed_at",
            "exit_condition": "exit_condition",
        },
        "on_conflict": "DO NOTHING",
    },
    {
        "sqlite": "trader_positions",
        "pg": "trading.trader_positions",
        "map": {
            "agent_id": "agent_id",
            "trader_id": "trader_id",
            "ticker": "ticker",
            "quantity": "quantity",
            "avg_entry_price": "avg_entry_price",
            "current_price": "current_price",
            "market_value": "market_value",
            "unrealized_pl": "unrealized_pl",
            "stop_loss": "stop_loss",
            "status": "status",
            "opened_at": "opened_at",
            "closed_at": "closed_at",
            "exit_condition": "exit_condition",
        },
        "on_conflict": "DO NOTHING",
    },
    {
        "sqlite": "portfolio_snapshots",
        "pg": "trading.portfolio_snapshots",
        "map": {
            "agent_id": "agent_id",
            "trader_id": "agent_id",
            "timestamp": "timestamp",
            "portfolio_value": "portfolio_value",
            "cash": "cash",
            "unrealized_pl": "unrealized_pl",
            "daily_pnl": "daily_pnl",
            "open_positions": "open_positions",
            "source": "source",
        },
        "on_conflict": "DO NOTHING",
    },
    {
        "sqlite": "orders",
        "pg": "trading.orders",
        "map": {
            "decision_id": "decision_id",
            "agent_id": "agent_id",
            "timestamp": "timestamp",
            "order_id": "order_id",
            "action": "action",
            "ticker": "ticker",
            "quantity": "quantity",
            "stop_loss": "stop_loss",
            "status": "status",
            "filled_price": "filled_price",
            "error_reason": "error_reason",
            "stop_loss_submitted": "stop_loss_submitted",
        },
        "on_conflict": "(order_id) DO UPDATE SET status=EXCLUDED.status, filled_price=EXCLUDED.filled_price",
    },
    {
        "sqlite": "trader_watchlist",
        "pg": "trading.trader_watchlist",
        "map": {
            "agent_id": "agent_id",
            "ticker": "ticker",
            "reason": "reason",
            "conviction_level": "conviction_level",
            "added_at": "added_at",
            "trader_id": "trader_id",
        },
        "on_conflict": "(agent_id, ticker) DO UPDATE SET reason=EXCLUDED.reason, conviction_level=EXCLUDED.conviction_level",
    },
    {
        "sqlite": "daily_pnl",
        "pg": "trading.daily_pnl",
        "map": {
            "agent_id": "agent_id",
            "date": "date",
            "pnl": "daily_pnl",
            "pnl_pct": "daily_pnl_pct",
            "start_equity": "opening_portfolio_value",
            "end_equity": "closing_portfolio_value",
            "trades_count": "trades_count",
            "win_count": "wins_count",
        },
        "on_conflict": "(agent_id, date) DO UPDATE SET pnl=EXCLUDED.pnl, end_equity=EXCLUDED.end_equity, trades_count=EXCLUDED.trades_count",
    },
    {
        "sqlite": "orders",
        "pg": "trading.orders",
        "map": {
            "agent_id": "agent_id",
            "order_id": "order_id",
            "ticker": "ticker",
            "action": "action",
            "quantity": "quantity",
            "status": "status",
            "filled_avg_price": "filled_price",
        },
        "on_conflict": "(order_id) DO UPDATE SET status=EXCLUDED.status, filled_avg_price=EXCLUDED.filled_avg_price",
    },
    {
        "sqlite": "risk_state",
        "pg": "trading.risk_state",
        "map": {
            "agent_id": "agent_id",
            "is_paused": "paused",
            "paused_reason": "pause_reason",
            "paused_at": "pause_timestamp",
            "updated_at": "updated_at",
        },
        "on_conflict": "(agent_id) DO UPDATE SET is_paused=EXCLUDED.is_paused, paused_reason=EXCLUDED.paused_reason, updated_at=EXCLUDED.updated_at",
    },
    {
        "sqlite": "sentiment",
        "pg": "trading.sentiment",
        "map": {
            "ticker": "ticker",
            "score": "overall_sentiment",
            "articles_count": "mention_count",
            "source": "sources",
            "fetched_at": "fetched_at",
        },
        "on_conflict": "DO NOTHING",
    },
]


def _sanitize_value(val: Any, col_default: Any = None) -> Any:
    """Strip NUL bytes from string values (Postgres rejects them).
    Also replaces None with a column-specific default if provided."""
    if val is None and col_default is not None:
        return col_default
    if isinstance(val, str):
        return val.replace("\x00", "")
    return val


def migrate_table(
    sq_conn: sqlite3.Connection,
    pg_conn,
    table_def: dict,
    dry_run: bool = False,
) -> int:
    """Migrate one table from SQLite to Postgres."""
    sq_table = table_def["sqlite"]
    pg_table = table_def["pg"]
    col_map = table_def["map"]
    on_conflict = table_def.get("on_conflict", "DO NOTHING")

    # Check if SQLite table exists
    cur = sq_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (sq_table,),
    )
    if not cur.fetchone():
        print(f"  [SKIP] {sq_table} → table not in SQLite")
        return 0

    # Get rows from SQLite
    sq_rows = sq_conn.execute(f"SELECT * FROM {sq_table}").fetchall()
    if not sq_rows:
        print(f"  [SKIP] {sq_table} → 0 rows")
        return 0

    # Get column names from SQLite
    sq_cols = [d[1] for d in sq_conn.execute(f"PRAGMA table_info({sq_table})")]

    # Build PG column list and value list
    # sq_col == '__default__' means always use the default value (synthetic column)
    pg_cols = []
    sq_indices = []  # index in SQLite row, or -1 for synthetic __default__ columns
    for pg_col, sq_col in col_map.items():
        if sq_col == '__default__':
            pg_cols.append(pg_col)
            sq_indices.append(-1)  # synthetic: use default
        elif sq_col in sq_cols:
            pg_cols.append(pg_col)
            sq_indices.append(sq_cols.index(sq_col))

    if not pg_cols:
        print(f"  [SKIP] {sq_table} → no matching columns")
        return 0

    # Extract values with defaults for NOT NULL columns
    defaults = table_def.get("defaults", {})
    values = []
    for row in sq_rows:
        row_vals = []
        for idx, pg_col in zip(sq_indices, pg_cols):
            if idx == -1:
                # Synthetic column: use default value
                val = defaults.get(pg_col)
            else:
                val = row[idx]
            col_default = defaults.get(pg_col)
            row_vals.append(_sanitize_value(val, col_default))
        values.append(tuple(row_vals))

    col_str = ", ".join(pg_cols)

    if dry_run:
        print(f"  [DRY] {sq_table} → {pg_table}: {len(values)} rows ({col_str})")
        return len(values)

    # Insert into Postgres
    pg_cur = pg_conn.cursor()
    sql = f"INSERT INTO {pg_table} ({col_str}) VALUES %s ON CONFLICT {on_conflict}"
    try:
        execute_values(pg_cur, sql, values)
        pg_conn.commit()
    except Exception as e:
        pg_conn.rollback()
        print(f"  [ERROR] {sq_table} → {pg_table}: {e}")
        return 0

    print(f"  [OK] {sq_table} → {pg_table}: {len(values)} rows")
    return len(values)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate SQLite → Postgres (read-only on SQLite)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--table", type=str, help="Migrate a single table")
    parser.add_argument(
        "--pull",
        action="store_true",
        help="Auto-copy SQLite DB from OpenClaw before migrating",
    )
    args = parser.parse_args()

    # --pull: copy SQLite from OpenClaw to a local temp file
    if args.pull:
        if not Path(SQLITE_PATH).exists() or args.pull:
            local_path = "/tmp/trader.db"
            print(f"Pulling SQLite from {OPENCLAW_USER}@{OPENCLAW_HOST}...")
            cmd = [
                "ssh", f"{OPENCLAW_USER}@{OPENCLAW_HOST}",
                f"cat /home/openclaw/projects/paper-trading-teams/shared/trader.db",
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                if result.returncode != 0:
                    print(f"ERROR: SSH failed: {result.stderr.decode()}")
                    sys.exit(1)
                Path(local_path).write_bytes(result.stdout)
                print(f"  → {len(result.stdout)} bytes written to {local_path}")
                os.environ["SQLITE_PATH"] = local_path
            except subprocess.TimeoutExpired:
                print("ERROR: SSH timed out")
                sys.exit(1)

    sqlite_path = os.environ.get("SQLITE_PATH", SQLITE_PATH)
    if not Path(sqlite_path).exists():
        print(f"ERROR: SQLite DB not found at {sqlite_path}")
        print("Run with --pull to auto-copy from OpenClaw, or set SQLITE_PATH env var")
        sys.exit(1)

    sq_conn = sqlite3.connect(sqlite_path)
    sq_conn.row_factory = sqlite3.Row

    pg_conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASSWORD,
    )

    tables = MIGRATION_TABLES
    if args.table:
        tables = [t for t in MIGRATION_TABLES if t["sqlite"] == args.table]
        if not tables:
            print(f"ERROR: table '{args.table}' not in migration config")
            sys.exit(1)

    total = 0
    mode = "DRY RUN" if args.dry_run else "MIGRATE"
    print(f"=== {mode}: {len(tables)} tables ===\n")

    for td in tables:
        n = migrate_table(sq_conn, pg_conn, td, dry_run=args.dry_run)
        total += n

    print(f"\n=== {mode} complete: {total} total rows ===")

    sq_conn.close()
    pg_conn.close()


if __name__ == "__main__":
    main()
