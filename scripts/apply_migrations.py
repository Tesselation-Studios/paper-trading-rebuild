#!/usr/bin/env python3
"""
Simple migration runner — zero dependencies.

Tracks applied migrations in a `schema_migrations` table in Postgres.
Each migration is a pair of .sql files: NNN_name_up.sql and NNN_name_down.sql.

Usage:
    python3 scripts/apply_migrations.py              # apply pending (up)
    python3 scripts/apply_migrations.py --status     # show what's applied
    python3 scripts/apply_migrations.py --rollback N # rollback last N
"""

import argparse
import re
import sys
from pathlib import Path

import psycopg2

# ── Config ──────────────────────────────────────────────────────────────────
PG_HOST = "192.168.1.179"
PG_PORT = 5433
PG_DB = "trading"
PG_USER = "trader"
PG_PASSWORD = "trader-dev-2026"

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

MIGRATION_RE = re.compile(r"^(\d{3})_(.+)_(up|down)\.sql$")


def get_connection():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASSWORD,
    )


def ensure_migrations_table(conn):
    """Create the tracking table if it doesn't exist."""
    conn.cursor().execute("""
        CREATE TABLE IF NOT EXISTS trading.schema_migrations (
            id          BIGSERIAL PRIMARY KEY,
            version     INTEGER NOT NULL UNIQUE,
            name        TEXT NOT NULL,
            applied_at  TIMESTAMPTZ DEFAULT NOW(),
            direction   TEXT NOT NULL CHECK (direction IN ('up', 'down'))
        )
    """)
    conn.commit()


def get_applied(conn) -> dict[int, str]:
    """Return dict of {version: direction} for applied migrations."""
    cur = conn.cursor()
    cur.execute("""
        SELECT version, direction FROM trading.schema_migrations
        ORDER BY version
    """)
    applied = {}
    for version, direction in cur.fetchall():
        applied[version] = direction
    return applied


def discover_migrations() -> dict[int, dict]:
    """Scan migrations dir for up/down pairs. Returns {version: {name, up, down}}."""
    migrations = {}
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = MIGRATION_RE.match(f.name)
        if not m:
            continue
        version = int(m.group(1))
        name = m.group(2)
        direction = m.group(3)

        if version not in migrations:
            migrations[version] = {"name": name}
        migrations[version][direction] = f

    return dict(sorted(migrations.items()))


def apply_migration(conn, version: int, info: dict, direction: str):
    """Run a single migration file."""
    sql_file = info.get(direction)
    if not sql_file:
        print(f"  ⚠️  v{version:03d} ({info['name']}): no {direction} file — skipping")
        return

    sql = sql_file.read_text().strip()
    if not sql:
        print(f"  ⚠️  v{version:03d} ({info['name']}): empty {direction} file — skipping")
        return

    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()
    print(f"  ✅ v{version:03d} ({info['name']}): {direction}")


def record_migration(conn, version: int, name: str, direction: str):
    """Record migration in tracking table."""
    cur = conn.cursor()
    if direction == "up":
        cur.execute(
            "INSERT INTO trading.schema_migrations (version, name, direction) VALUES (%s, %s, %s)",
            (version, name, direction),
        )
    else:
        cur.execute(
            "DELETE FROM trading.schema_migrations WHERE version = %s",
            (version,),
        )
    conn.commit()


def cmd_status(conn):
    """Show migration status."""
    migrations = discover_migrations()
    applied = get_applied(conn)

    print(f"\n{'Version':>8}  {'Name':<35} {'Status':<12}")
    print("-" * 60)
    for version, info in migrations.items():
        if version in applied:
            print(f"  {version:03d}     {info['name']:<35} {'applied':<12}")
        else:
            print(f"  {version:03d}     {info['name']:<35} {'pending':<12}")


def cmd_up(conn):
    """Apply all pending migrations."""
    migrations = discover_migrations()
    applied = get_applied(conn)

    pending = {
        v: info for v, info in migrations.items()
        if v not in applied
    }

    if not pending:
        print("No pending migrations.")
        return

    print(f"\nApplying {len(pending)} migration(s):")
    for version, info in sorted(pending.items()):
        apply_migration(conn, version, info, "up")
        record_migration(conn, version, info["name"], "up")


def cmd_rollback(conn, count: int):
    """Rollback the last N applied migrations."""
    migrations = discover_migrations()
    cur = conn.cursor()
    cur.execute(
        "SELECT version, name FROM trading.schema_migrations WHERE direction='up' ORDER BY version DESC LIMIT %s",
        (count,),
    )
    to_rollback = cur.fetchall()

    if not to_rollback:
        print("No migrations to rollback.")
        return

    print(f"\nRolling back {len(to_rollback)} migration(s):")
    for version, name in reversed(to_rollback):
        info = migrations.get(version, {"name": name})
        apply_migration(conn, version, info, "down")
        record_migration(conn, version, name, "down")


def main():
    parser = argparse.ArgumentParser(description="Run DB migrations")
    parser.add_argument("--status", action="store_true", help="Show migration status")
    parser.add_argument("--rollback", type=int, metavar="N", help="Rollback last N migrations")
    args = parser.parse_args()

    conn = get_connection()
    try:
        ensure_migrations_table(conn)

        if args.status:
            cmd_status(conn)
        elif args.rollback:
            cmd_rollback(conn, args.rollback)
        else:
            cmd_up(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
