#!/usr/bin/env python3
"""
Migrate live trading data from SQLite (OpenClaw) → Postgres (docker.klo).

SAFE: Read-only on SQLite. Uses ON CONFLICT (idempotent — can re-run).
Does NOT touch live traders. Run manually, not from cron.

Usage:
    python3 scripts/migrate_sqlite_to_pg.py --dry-run
    python3 scripts/migrate_sqlite_to_pg.py
    python3 scripts/migrate_sqlite_to_pg.py --table trades
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import execute_values

# ── Config ────────────────────────────────────────────────────────────────────
SQLITE_PATH = "/home/openclaw/projects/paper-trading-teams/shared/trader.db"
PG_HOST = "192.168.1.179"
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
        "pg": "trading.executed_trades",
        "map": {
            "agent_id": "agent_id",
            "ticker": "ticker",
            "action": "action",
            "quantity": "quantity",
            "entry_price": "entry_price",
            "exit_price": "exit_price",
            "entry_timestamp": "entry_time",
            "exit_timestamp": "exit_time",
            "exit_reason": "exit_reason",
            "pnl": "pnl",
            "pnl_pct": "pnl_pct",
            "status": "status",
            "decision_id": "decision_id",
            "entry_reason": "entry_reason",
            "updated_at": "updated_at",
        },
        "on_conflict": "(agent_id, ticker, entry_time) DO NOTHING",
    },
    {
        "sqlite": "decisions",
        "pg": "trading.decisions",
        "map": {
            "agent_id": "trader_id",
            "ticker": "ticker",
            "timestamp": "timestamp",
            "decision": "decision",
            "confidence": "conviction",
            "thesis": "rationale",
        },
        "on_conflict": "DO NOTHING",
    },
    {
        "sqlite": "journal",
        "pg": "trading.journal",
        "map": {
            "agent_id": "trader_id",
            "timestamp": "timestamp",
            "ticker": "ticker",
            "entry_type": "decision",
            "content": "rationale",
        },
        "on_conflict": "DO NOTHING",
    },
    # Agent state
    {
        "sqlite": "agent_state",
        "pg": "trading.agent_state",
        "map": {
            "agent_id": "agent_id",
            "is_active": "is_active",
            "last_heartbeat": "last_heartbeat",
            "last_trade": "last_trade",
            "cash": "cash",
            "equity": "equity",
            "pnl": "pnl",
            "pnl_pct": "pnl_pct",
            "positions_count": "positions_count",
            "updated_at": "updated_at",
        },
        "on_conflict": "(agent_id) DO UPDATE SET equity=EXCLUDED.equity, cash=EXCLUDED.cash, pnl=EXCLUDED.pnl, updated_at=EXCLUDED.updated_at",
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
            "updated_at": "updated_at",
        },
        "on_conflict": "(agent_id) DO UPDATE SET name=EXCLUDED.name, current_state=EXCLUDED.current_state, updated_at=EXCLUDED.updated_at",
    },
    {
        "sqlite": "positions",
        "pg": "trading.positions",
        "map": {
            "agent_id": "agent_id",
            "ticker": "ticker",
            "quantity": "quantity",
            "entry_price": "entry_price",
            "current_price": "current_price",
            "market_value": "market_value",
            "unrealized_pl": "unrealized_pl",
            "unrealized_plpc": "unrealized_plpc",
            "opened_at": "opened_at",
            "updated_at": "updated_at",
        },
        "on_conflict": "(agent_id, ticker) DO UPDATE SET quantity=EXCLUDED.quantity, current_price=EXCLUDED.current_price, market_value=EXCLUDED.market_value, updated_at=EXCLUDED.updated_at",
    },
    {
        "sqlite": "executed_trades",
        "pg": "trading.executed_trades",
        "map": {
            "agent_id": "agent_id",
            "ticker": "ticker",
            "action": "action",
            "quantity": "quantity",
            "entry_price": "entry_price",
            "exit_price": "exit_price",
            "entry_time": "entry_time",
            "exit_time": "exit_time",
            "exit_reason": "exit_reason",
            "pnl": "pnl",
            "pnl_pct": "pnl_pct",
            "status": "status",
        },
        "on_conflict": "DO NOTHING",
    },
    {
        "sqlite": "portfolio_snapshots",
        "pg": "trading.portfolio_snapshots",
        "map": {
            "agent_id": "agent_id",
            "timestamp": "timestamp",
            "equity": "equity",
            "cash": "cash",
            "invested": "invested",
            "pnl": "pnl",
            "pnl_pct": "pnl_pct",
        },
        "on_conflict": "DO NOTHING",
    },
    {
        "sqlite": "daily_pnl",
        "pg": "trading.daily_pnl",
        "map": {
            "agent_id": "agent_id",
            "date": "date",
            "pnl": "pnl",
            "pnl_pct": "pnl_pct",
            "start_equity": "start_equity",
            "end_equity": "end_equity",
            "trades_count": "trades_count",
            "win_count": "win_count",
            "loss_count": "loss_count",
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
            "order_type": "order_type",
            "limit_price": "limit_price",
            "status": "status",
            "filled_qty": "filled_qty",
            "filled_avg_price": "filled_avg_price",
            "submitted_at": "submitted_at",
            "filled_at": "filled_at",
        },
        "on_conflict": "(order_id) DO NOTHING",
    },
    {
        "sqlite": "risk_state",
        "pg": "trading.risk_state",
        "map": {
            "agent_id": "agent_id",
            "is_paused": "is_paused",
            "paused_reason": "paused_reason",
            "paused_at": "paused_at",
            "max_drawdown": "max_drawdown",
            "updated_at": "updated_at",
        },
        "on_conflict": "(agent_id) DO UPDATE SET is_paused=EXCLUDED.is_paused, updated_at=EXCLUDED.updated_at",
    },
    {
        "sqlite": "sentiment",
        "pg": "trading.sentiment",
        "map": {
            "source": "source",
            "ticker": "ticker",
            "score": "score",
            "magnitude": "magnitude",
            "fetched_at": "fetched_at",
        },
        "on_conflict": "DO NOTHING",
    },
]


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
    sq_cols = [d[0] for d in sq_conn.execute(f"PRAGMA table_info({sq_table})")]

    # Build PG column list and value list
    pg_cols = []
    sq_indices = []
    for pg_col, sq_col in col_map.items():
        if sq_col in sq_cols:
            pg_cols.append(pg_col)
            sq_indices.append(sq_cols.index(sq_col))

    if not pg_cols:
        print(f"  [SKIP] {sq_table} → no matching columns")
        return 0

    # Extract values
    values = []
    for row in sq_rows:
        values.append(tuple(row[i] for i in sq_indices))

    col_str = ", ".join(pg_cols)

    if dry_run:
        print(f"  [DRY] {sq_table} → {pg_table}: {len(values)} rows ({col_str})")
        return len(values)

    # Insert into Postgres
    pg_cur = pg_conn.cursor()
    sql = f"INSERT INTO {pg_table} ({col_str}) VALUES %s ON CONFLICT {on_conflict}"
    execute_values(pg_cur, sql, values)
    pg_conn.commit()

    print(f"  [OK] {sq_table} → {pg_table}: {len(values)} rows")
    return len(values)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate SQLite → Postgres (read-only on SQLite)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--table", type=str, help="Migrate a single table")
    args = parser.parse_args()

    if not Path(SQLITE_PATH).exists():
        print(f"ERROR: SQLite DB not found at {SQLITE_PATH}")
        print("Run from OpenClaw or update SQLITE_PATH")
        sys.exit(1)

    sq_conn = sqlite3.connect(SQLITE_PATH)
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
