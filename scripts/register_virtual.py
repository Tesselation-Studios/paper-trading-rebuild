#!/usr/bin/env python3
"""Register a virtual trader in the database.

Inserts a row into trading.virtual_traders with:
  - name, base_trader, variant_type, config (JSONB), status
  - wins=0, equity=starting_cash, is_champion=false

Also creates the trading.virtual_traders_log table if it does not exist.

Usage:
    python3 scripts/register_virtual.py --name test-vt-1 --base aldridge
    python3 scripts/register_virtual.py --name test-vt-2 --base kairos --config '{"rsi_period": 10}'
    python3 scripts/register_virtual.py --name test-vt-3 --base stonks --status active
    python3 scripts/register_virtual.py --list                           # list all virtual traders
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── DB connection (same pattern as src/db/connection.py) ─────────────────────

DB_DSN = os.getenv("VT_DB_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")


def get_conn():
    """Get a sync psycopg2 connection."""
    import psycopg2
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


def ensure_virtual_traders_log_table():
    """Create virtual_traders_log and virtual_traders tables if they don't exist.

    This ensures the logging table exists before any virtual trader runs.
    Returns True if created, False if already existed.
    """
    conn = get_conn()
    cur = conn.cursor()

    # First ensure virtual_traders exists (migration might not have run)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trading.virtual_traders (
            id SERIAL PRIMARY KEY,
            name VARCHAR(64) NOT NULL,
            base_trader VARCHAR(32) NOT NULL,
            variant_type VARCHAR(16) NOT NULL,
            config JSONB NOT NULL DEFAULT '{}'::jsonb,
            status VARCHAR(16) DEFAULT 'probation',
            live_dates DATE[],
            created_at DATE DEFAULT CURRENT_DATE,
            culled_at DATE,
            wins INTEGER DEFAULT 0,
            equity NUMERIC DEFAULT 10000.00,
            is_champion BOOLEAN DEFAULT false,
            champion_since DATE
        )
    """)

    # Ensure the GC-collected columns exist (added by 002_virtual_wins.sql)
    for col, dtype in [("wins", "INTEGER DEFAULT 0"),
                       ("equity", "NUMERIC DEFAULT 10000.00"),
                       ("is_champion", "BOOLEAN DEFAULT false"),
                       ("champion_since", "DATE")]:
        cur.execute(f"""
            ALTER TABLE trading.virtual_traders
            ADD COLUMN IF NOT EXISTS {col} {dtype}
        """)

    # Create the log table if it doesn't exist
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trading.virtual_traders_log (
            id SERIAL PRIMARY KEY,
            trader_name VARCHAR(64) NOT NULL,
            run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ticker VARCHAR(16),
            decision VARCHAR(8),
            conviction NUMERIC,
            rationale TEXT,
            price NUMERIC,
            regime VARCHAR(32),
            composite_signal NUMERIC,
            equity_before NUMERIC,
            equity_after NUMERIC,
            pnl NUMERIC,
            error TEXT,
            duration_ms INTEGER
        )
    """)

    # Create index on trader_name and run_at for performance
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_virtual_traders_log_trader_run
        ON trading.virtual_traders_log (trader_name, run_at DESC)
    """)

    conn.close()
    print("✅ Trading tables verified/created: virtual_traders, virtual_traders_log")


def register_virtual_trader(name: str, base_trader: str, variant_type: str,
                            config: dict, status: str = "probation",
                            starting_cash: float = 10000.0) -> int:
    """Insert a virtual trader row.

    Args:
        name: Virtual trader name
        base_trader: One of 'aldridge', 'kairos', 'stonks'
        variant_type: Type of variant (params, prompt, model, risk, manual)
        config: Dict of variant params (will be JSONB)
        status: 'probation', 'active', or 'disabled'
        starting_cash: Starting equity amount

    Returns:
        The new virtual_traders.id
    """
    import psycopg2
    import psycopg2.extras

    conn = get_conn()
    cur = conn.cursor()

    # Check if name already exists
    cur.execute(
        "SELECT id, status FROM trading.virtual_traders WHERE name = %s",
        (name,)
    )
    existing = cur.fetchone()
    if existing:
        print(f"⚠️  Virtual trader '{name}' already exists (id={existing[0]}, status={existing[1]})")
        print(f"   Update with --update-status if needed")
        conn.close()
        return existing[0]

    config_json = json.dumps(config)

    cur.execute(
        """INSERT INTO trading.virtual_traders
           (name, base_trader, variant_type, config, status, equity)
           VALUES (%s, %s, %s, %s::jsonb, %s, %s)
           RETURNING id""",
        (name, base_trader, variant_type, config_json, status, starting_cash)
    )
    new_id = cur.fetchone()[0]
    conn.close()

    print(f"✅ Registered virtual trader '{name}' (id={new_id})")
    print(f"   base_trader={base_trader}  variant_type={variant_type}  status={status}")
    print(f"   config={config}")

    return new_id


def list_virtual_traders(show_all: bool = False):
    """List all virtual traders in the DB."""
    conn = get_conn()
    cur = conn.cursor()

    if show_all:
        cur.execute(
            "SELECT id, name, base_trader, variant_type, status, equity, wins, "
            "       is_champion, created_at "
            "FROM trading.virtual_traders "
            "ORDER BY id"
        )
    else:
        cur.execute(
            "SELECT id, name, base_trader, variant_type, status, equity, wins, "
            "       is_champion, created_at "
            "FROM trading.virtual_traders "
            "WHERE status IN ('active', 'probation') "
            "ORDER BY id"
        )

    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No virtual traders found.")
        return

    print(f"\n{'ID':>4} {'Name':<28} {'Base':<10} {'Type':<10} {'Status':<12} "
          f"{'Equity':>10} {'Wins':>5} {'Champ':<6} {'Created'}")
    print("-" * 100)
    for row in rows:
        champ = "🏆" if row[7] else ""
        print(f"{row[0]:>4} {row[1]:<28} {row[2]:<10} {row[3]:<10} "
              f"{row[4]:<12} {float(row[5]):>8.2f}  {row[6]:>3}  {champ:<6} {row[8]}")
    print()


def update_status(name: str, new_status: str):
    """Update a virtual trader's status."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "UPDATE trading.virtual_traders SET status = %s WHERE name = %s",
        (new_status, name)
    )
    if cur.rowcount == 0:
        print(f"⚠️  No virtual trader found with name '{name}'")
    else:
        print(f"✅ Updated '{name}' status → {new_status}")
    conn.close()


def delete_virtual_trader(name: str):
    """Delete a virtual trader from the DB."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM trading.virtual_traders WHERE name = %s RETURNING id",
        (name,)
    )
    row = cur.fetchone()
    conn.close()

    if row:
        print(f"🗑️  Deleted virtual trader '{name}' (id={row[0]})")
    else:
        print(f"⚠️  No virtual trader found with name '{name}'")


def main():
    parser = argparse.ArgumentParser(
        description="Register and manage virtual traders in the database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--name", help="Virtual trader name")
    parser.add_argument("--base", choices=["aldridge", "kairos", "stonks"],
                        help="Base trader")
    parser.add_argument("--variant-type", default="params",
                        choices=["params", "prompt", "model", "risk", "manual"],
                        help="Variant type (default: params)")
    parser.add_argument("--config", default="{}",
                        help="JSON config overrides")
    parser.add_argument("--status", default="probation",
                        choices=["probation", "active", "disabled", "culled"],
                        help="Initial status (default: probation)")
    parser.add_argument("--starting-cash", type=float, default=10000.0,
                        help="Starting equity (default: 10000)")
    parser.add_argument("--list", action="store_true",
                        help="List all active/probation virtual traders")
    parser.add_argument("--list-all", action="store_true",
                        help="List ALL virtual traders (including disabled/culled)")
    parser.add_argument("--update-status", choices=["probation", "active", "disabled", "culled"],
                        help="Update status for an existing virtual trader")
    parser.add_argument("--delete", action="store_true",
                        help="Delete a virtual trader by name")
    parser.add_argument("--ensure-tables", action="store_true",
                        help="Create/verify the necessary DB tables and exit")

    args = parser.parse_args()

    # Ensure tables first
    ensure_virtual_traders_log_table()

    # Handle special actions
    if args.ensure_tables:
        return

    if args.list:
        list_virtual_traders(show_all=False)
        return

    if args.list_all:
        list_virtual_traders(show_all=True)
        return

    if args.update_status:
        if not args.name:
            parser.error("--name required for --update-status")
        update_status(args.name, args.update_status)
        return

    if args.delete:
        if not args.name:
            parser.error("--name required for --delete")
        delete_virtual_trader(args.name)
        return

    # Registration
    if not args.name:
        parser.error("--name is required for registration")
    if not args.base:
        parser.error("--base is required for registration")

    try:
        config_obj = json.loads(args.config)
    except json.JSONDecodeError as e:
        parser.error(f"Invalid --config JSON: {e}")

    register_virtual_trader(
        name=args.name,
        base_trader=args.base,
        variant_type=args.variant_type,
        config=config_obj,
        status=args.status,
        starting_cash=args.starting_cash,
    )


if __name__ == "__main__":
    main()