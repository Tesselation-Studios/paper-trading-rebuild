"""
dual_writer — Bidirectional Postgres writer for data_bus.

Writes data to Postgres tables as a "mirror" from the data bus cache.
Used by data_bus.py to persist fetched data into the trading database.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import psycopg2
import psycopg2.extras

log = logging.getLogger("dual_writer")

_DSN: str | None = None


def _get_dsn() -> str:
    global _DSN
    if _DSN is None:
        import os as _os
        host = _os.getenv("PGHOST", "trading-db")
        port = _os.getenv("PGPORT", "5432")
        dbname = _os.getenv("PGDATABASE", "trading")
        user = _os.getenv("PGUSER", "trader")
        pw = _os.getenv("PGPASSWORD", "")
        _DSN = f"host={host} port={port} dbname={dbname} user={user}"
        if pw:
            _DSN += f" password={pw}"
    return _DSN


def write(table: str, row: Dict[str, Any]) -> bool:
    """Write a single row dict to the specified Postgres table (best-effort).

    Args:
        table: Destination table name (may include schema, e.g. 'trading.trades').
        row: Column → value mapping.

    Returns:
        True on success, False on any error (logged as warning).
    """
    if not row:
        return False

    try:
        columns = list(row.keys())
        placeholders = ", ".join(f"%({c})s" for c in columns)
        col_str = ", ".join(columns)

        sql = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders})"

        conn = psycopg2.connect(_get_dsn())
        try:
            with conn.cursor() as cur:
                cur.execute(sql, row)
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log.warning("dual_writer.write(%s) failed: %s", table, e)
        return False


def write_batch(table: str, rows: list[Dict[str, Any]]) -> bool:
    """Write multiple rows to the specified Postgres table (best-effort).

    Args:
        table: Destination table name.
        rows: List of column → value mappings. Must all share the same keys.

    Returns:
        True on success, False on any error.
    """
    if not rows:
        return False

    try:
        columns = list(rows[0].keys())
        col_str = ", ".join(columns)

        conn = psycopg2.connect(_get_dsn())
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO {table} ({col_str}) VALUES %s",
                    rows,
                    template=f"({', '.join(f'%({c})s' for c in columns)})",
                )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log.warning("dual_writer.write_batch(%s) failed: %s", table, e)
        return False
