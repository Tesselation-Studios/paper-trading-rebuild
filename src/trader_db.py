"""
SQLite database for trader bankroll state and closed trade access.

Provides a local SQLite store with:
  - bankroll_state table (ceiling_pct as REAL)
  - CRUD helpers for bankroll state persistence
  - Read-only access to closed trades for graduated position sizing

Usage:
    from src.trader_db import (
        upsert_bankroll_state,
        get_bankroll_state,
        get_or_create_bankroll_state,
        get_closed_trades,
    )

    state = get_or_create_bankroll_state("trading.db", "trader-kairos")
    trades = get_closed_trades("trading.db", "trader-kairos", limit=50)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════════════════════════

BANKROLL_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bankroll_state (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_id       TEXT NOT NULL UNIQUE,
    ceiling_pct     REAL NOT NULL DEFAULT 0.067,
    ceiling         REAL DEFAULT NULL,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    streak          INTEGER NOT NULL DEFAULT 0,
    peak_pct        REAL NOT NULL DEFAULT 0.067,
    updated_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Connection Management
# ═══════════════════════════════════════════════════════════════════════════════


def get_connection(db_path: str) -> sqlite3.Connection:
    """Get a SQLite connection, ensuring the schema exists.

    Args:
        db_path: Path to SQLite database file.

    Returns:
        sqlite3.Connection with row_factory set and WAL mode enabled.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create bankroll_state table if it doesn't exist.

    The ceiling column is retained as a derived/logged value for backward
    compatibility — ceiling_pct is the primary field now.
    """
    conn.execute(BANKROLL_STATE_SCHEMA)


# ═══════════════════════════════════════════════════════════════════════════════
# Bankroll State CRUD
# ═══════════════════════════════════════════════════════════════════════════════


def upsert_bankroll_state(
    db_path: str,
    trader_id: str,
    ceiling_pct: float,
    ceiling: Optional[float] = None,
    wins: int = 0,
    losses: int = 0,
    streak: int = 0,
    peak_pct: Optional[float] = None,
) -> None:
    """Insert or update bankroll state for a trader.

    Args:
        db_path: Path to SQLite database.
        trader_id: Trader identifier (e.g. "trader-kairos").
        ceiling_pct: Current ceiling as fraction of equity (0.0-1.0).
        ceiling: Derived/logged dollar ceiling value (optional).
        wins: Total winning trades.
        losses: Total losing trades.
        streak: Current win/loss streak (positive = wins, negative = losses).
        peak_pct: Highest ceiling_pct achieved.
    """
    conn = get_connection(db_path)
    try:
        now = datetime.now(timezone.utc).isoformat()
        if ceiling is None:
            ceiling = ceiling_pct * 10_000
        if peak_pct is None:
            peak_pct = ceiling_pct

        conn.execute(
            """
            INSERT INTO bankroll_state
                (trader_id, ceiling_pct, ceiling, wins, losses, streak, peak_pct, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trader_id) DO UPDATE SET
                ceiling_pct = EXCLUDED.ceiling_pct,
                ceiling     = EXCLUDED.ceiling,
                wins        = EXCLUDED.wins,
                losses      = EXCLUDED.losses,
                streak      = EXCLUDED.streak,
                peak_pct    = MAX(bankroll_state.peak_pct, EXCLUDED.peak_pct),
                updated_at  = EXCLUDED.updated_at
            """,
            (trader_id, ceiling_pct, ceiling, wins, losses, streak, peak_pct, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_bankroll_state(
    db_path: str,
    trader_id: str,
) -> Optional[Dict[str, Any]]:
    """Read bankroll state for a trader.

    Args:
        db_path: Path to SQLite database.
        trader_id: Trader identifier.

    Returns:
        Bankroll state dict, or None if not found.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM bankroll_state WHERE trader_id = ?",
            (trader_id,),
        ).fetchone()

        if row is None:
            return None

        return dict(row)
    finally:
        conn.close()


def get_or_create_bankroll_state(
    db_path: str,
    trader_id: str,
    default_ceiling_pct: float = 0.067,
) -> Dict[str, Any]:
    """Get existing bankroll state or create a default one.

    Args:
        db_path: Path to SQLite database.
        trader_id: Trader identifier.
        default_ceiling_pct: Default ceiling_pct for new entries.

    Returns:
        Bankroll state dict (always present after call).
    """
    # Check existing
    state = get_bankroll_state(db_path, trader_id)
    if state is not None:
        return state

    # Create new
    conn = get_connection(db_path)
    try:
        now = datetime.now(timezone.utc).isoformat()
        default_ceiling = default_ceiling_pct * 10_000
        conn.execute(
            """
            INSERT INTO bankroll_state
                (trader_id, ceiling_pct, ceiling, wins, losses, streak, peak_pct, updated_at)
            VALUES (?, ?, ?, 0, 0, 0, ?, ?)
            """,
            (trader_id, default_ceiling_pct, default_ceiling, default_ceiling_pct, now),
        )
        conn.commit()

        return {
            "trader_id": trader_id,
            "ceiling_pct": default_ceiling_pct,
            "ceiling": default_ceiling,
            "wins": 0,
            "losses": 0,
            "streak": 0,
            "peak_pct": default_ceiling_pct,
            "updated_at": now,
        }
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Closed Trade Access (for Graduated Sizing)
# ═══════════════════════════════════════════════════════════════════════════════


def get_closed_trades(
    db_path: str,
    trader_id: str,
    since: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Read closed trades for graduated position sizing.

    Attempts to read from both 'trades' and 'executed_trades' tables
    (the schema may vary depending on which migration stage the DB is at).

    Args:
        db_path: Path to SQLite database.
        trader_id: Trader identifier.
        since: Optional ISO timestamp string — only return trades after this.
        limit: Maximum number of trades to return.

    Returns:
        List of trade dicts, each with at least 'pnl' field.
    """
    conn = get_connection(db_path)
    try:
        # Try 'trades' table first (original schema)
        try:
            if since:
                rows = conn.execute(
                    """
                    SELECT * FROM trades
                    WHERE agent_id = ? AND exit_time IS NOT NULL AND exit_time >= ?
                    ORDER BY exit_time DESC
                    LIMIT ?
                    """,
                    (trader_id, since, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM trades
                    WHERE agent_id = ? AND exit_time IS NOT NULL
                    ORDER BY exit_time DESC
                    LIMIT ?
                    """,
                    (trader_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            pass

        # Try 'executed_trades' table next (live schema)
        try:
            if since:
                rows = conn.execute(
                    """
                    SELECT * FROM executed_trades
                    WHERE agent_id = ? AND status = 'closed' AND exit_time >= ?
                    ORDER BY exit_time DESC
                    LIMIT ?
                    """,
                    (trader_id, since, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM executed_trades
                    WHERE agent_id = ? AND status = 'closed'
                    ORDER BY exit_time DESC
                    LIMIT ?
                    """,
                    (trader_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            pass

        return []
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Maintenance: Apply Graduated Sizing
# ═══════════════════════════════════════════════════════════════════════════════


def apply_graduated_sizing(
    db_path: str,
    trader_id: str,
    params_path: str,
) -> Dict[str, Any]:
    """Run graduated position sizing and persist the result.

    Reads closed trades from DB, computes graduated max_position_pct,
    and writes it into params.json.

    Intended to run once per nightly-maintenance cycle.

    Args:
        db_path: Path to SQLite database (trades).
        trader_id: Trader identifier.
        params_path: Path to params.json.

    Returns:
        Dict with 'max_position_pct', 'win_rate', 'n_trades' keys.
    """
    from src.bankroll import graduated_max_position_pct, write_max_position_pct_to_params

    closed_trades = get_closed_trades(db_path, trader_id)
    n_trades = len(closed_trades)

    if n_trades > 0:
        wins = sum(1 for t in closed_trades if t.get("pnl", 0) is not None and t["pnl"] > 0)
        win_rate = wins / n_trades
    else:
        win_rate = 0.0

    max_pct = graduated_max_position_pct(
        closed_trades=closed_trades,
        win_rate=win_rate,
        n_trades=n_trades,
    )

    write_max_position_pct_to_params(max_pct, params_path)

    return {
        "max_position_pct": max_pct,
        "win_rate": round(win_rate, 4),
        "n_trades": n_trades,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    """CLI entry point for trader_db operations."""
    import argparse

    parser = argparse.ArgumentParser(description="Trader database management")
    parser.add_argument("--db", default="trading.db", help="SQLite database path")
    parser.add_argument("--trader", default="trader-kairos", help="Trader ID")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("show", help="Show bankroll state")

    grad = sub.add_parser("graduate", help="Apply graduated sizing")
    grad.add_argument("--params", default="params.json", help="Path to params.json")

    args = parser.parse_args()

    if args.command == "show":
        state = get_or_create_bankroll_state(args.db, args.trader)
        print(f"Trader:       {state['trader_id']}")
        print(f"ceiling_pct:  {state['ceiling_pct']:.3%}")
        print(f"ceiling:      ${state['ceiling']:,.2f}")
        print(f"wins:         {state['wins']}")
        print(f"losses:       {state['losses']}")
        print(f"streak:       {state['streak']}")
        print(f"peak_pct:     {state['peak_pct']:.3%}")

    elif args.command == "graduate":
        result = apply_graduated_sizing(args.db, args.trader, args.params)
        print(f"Applied graduated sizing:")
        print(f"  n_trades:        {result['n_trades']}")
        print(f"  win_rate:        {result['win_rate']:.1%}")
        print(f"  max_position_pct: {result['max_position_pct']:.1f}%")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
