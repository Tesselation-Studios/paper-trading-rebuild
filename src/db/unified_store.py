"""
unified_store — Postgres-backed unified data access layer.

Replaces SQLite reads/writes across the project with a single Postgres
connection pool. All data operations route through this module.

Tables (schema: market_data, trading):
  - market_data.bars          → bar_loader.py (OHLCV bars)
  - trading.trades            → sync_exits.py (completed trades)
  - trading.journal           → decision journal entries
  - trading.params            → config_loader.py (tuneable params)
  - trading.sweep_runs        → historical_sim.py (sweep metadata)
  - trading.sweep_results     → historical_sim.py (sweep variant scores)
  - trading.signals           → signal snapshots
  - trading.decisions         → trading decisions

Usage:
    from src.db.unified_store import get_store

    store = get_store()
    
    # Write a trade
    store.insert_trade(trader_id="kairos", trade_id="K-001", ...)
    
    # Read params
    params = store.get_params("kairos")
    
    # Read sweep results
    sweep = store.get_sweep_results("kairos", limit=50)
    
    # Raw query
    rows = store.query("SELECT * FROM trading.trades WHERE trader_id = %s", ("kairos",))
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("unified_store")


def _get_dsn() -> str:
    """Build Postgres DSN from environment variables."""
    host = os.getenv("PGHOST", "trading-db")
    port = os.getenv("PGPORT", "5432")
    dbname = os.getenv("PGDATABASE", "trading")
    user = os.getenv("PGUSER", "trader")
    password = os.getenv("PGPASSWORD", "")
    dsn = f"host={host} port={port} dbname={dbname} user={user}"
    if password:
        dsn += f" password={password}"
    return dsn


# Lazy import so the module can be imported without psycopg2
_psycopg2 = None
_psycopg2_extras = None


def _ensure_psycopg2():
    global _psycopg2, _psycopg2_extras
    if _psycopg2 is None:
        import psycopg2 as _psycopg2
        import psycopg2.extras as _psycopg2_extras
    return _psycopg2, _psycopg2_extras


# ── UnifiedStore ─────────────────────────────────────────────────────────────


class UnifiedStore:
    """Postgres-backed unified data access layer.

    Thread-safe; creates a new connection per operation.
    For high-throughput scenarios, use the connection context manager
    or inject a connection pool.
    """

    def __init__(self, dsn: Optional[str] = None):
        self._dsn = dsn or _get_dsn()

    @contextmanager
    def conn(self):
        """Context manager yielding a psycopg2 connection (auto-commit + close)."""
        psycopg2, _ = _ensure_psycopg2()
        cn = psycopg2.connect(self._dsn)
        try:
            yield cn
            cn.commit()
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.close()

    # ── Raw query ───────────────────────────────────────────────────────

    def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Execute a SELECT and return rows as dicts."""
        psycopg2, _ = _ensure_psycopg2()
        with self.conn() as cn:
            with cn.cursor() as cur:
                cur.execute(sql, params)
                columns = [desc[0] for desc in cur.description] if cur.description else []
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def execute(self, sql: str, params: tuple = ()) -> int:
        """Execute a write statement, return rowcount."""
        psycopg2, _ = _ensure_psycopg2()
        with self.conn() as cn:
            with cn.cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount

    def execute_values(self, sql: str, rows: List[tuple], template: str = "") -> int:
        """Batch insert using execute_values."""
        psycopg2, extras = _ensure_psycopg2()
        with self.conn() as cn:
            with cn.cursor() as cur:
                extras.execute_values(cur, sql, rows, template=template or None)
                return cur.rowcount

    # ── Replay Ticks (bar_loader.py) ─────────────────────────────────────

    def ensure_replay_ticks_table(self):
        """Create replay_ticks table if it doesn't exist (in market_data schema)."""
        self.execute("""
            CREATE TABLE IF NOT EXISTS market_data.replay_ticks (
                id          BIGSERIAL PRIMARY KEY,
                ticker      VARCHAR(10)  NOT NULL,
                timestamp   TIMESTAMPTZ  NOT NULL,
                open        DECIMAL      NOT NULL,
                high        DECIMAL      NOT NULL,
                low         DECIMAL      NOT NULL,
                close       DECIMAL      NOT NULL,
                volume      BIGINT       NOT NULL,
                rsi         DECIMAL,
                momentum    DECIMAL,
                volatility  DECIMAL,
                regime      VARCHAR(32),
                created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        """)
        self.execute("""
            CREATE INDEX IF NOT EXISTS idx_replay_ticks_ticker_ts
                ON market_data.replay_ticks (ticker, timestamp)
        """)

    def insert_replay_ticks(self, rows: List[Dict[str, Any]]) -> int:
        """Insert replay ticks batch. Returns row count."""
        if not rows:
            return 0
        self.ensure_replay_ticks_table()
        psycopg2, extras = _ensure_psycopg2()
        columns = list(rows[0].keys())
        col_str = ", ".join(columns)
        with self.conn() as cn:
            with cn.cursor() as cur:
                extras.execute_values(
                    cur,
                    f"INSERT INTO market_data.replay_ticks ({col_str}) VALUES %s "
                    "ON CONFLICT DO NOTHING",
                    rows,
                    template=f"({', '.join(f'%({c})s' for c in columns)})",
                )
                return cur.rowcount

    def get_replay_ticks(
        self,
        tickers: Optional[List[str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get replay ticks with optional filters."""
        conditions = []
        params: List[Any] = []

        if tickers:
            conditions.append(f"ticker IN ({', '.join('%s' for _ in tickers)})")
            params.extend(tickers)
        if start_date:
            conditions.append("timestamp >= %s")
            params.append(start_date)
        if end_date:
            conditions.append("timestamp <= %s")
            params.append(end_date + " 23:59:59")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return self.query(f"SELECT * FROM market_data.replay_ticks {where} ORDER BY timestamp", tuple(params))

    # ── Trades (sync_exits.py) ───────────────────────────────────────────

    def insert_trade(self, row: Dict[str, Any]) -> bool:
        """Insert a completed trade. Returns True on success."""
        try:
            self.execute(
                """INSERT INTO trading.trades
                   (trader_id, trade_id, ticker, entry_time, exit_time,
                    entry_price, exit_price, shares, pnl, return_pct, regime)
                   VALUES (%(trader_id)s, %(trade_id)s, %(ticker)s, %(entry_time)s,
                           %(exit_time)s, %(entry_price)s, %(exit_price)s,
                           %(shares)s, %(pnl)s, %(return_pct)s, %(regime)s)
                   ON CONFLICT (trade_id) DO UPDATE SET
                     exit_time = EXCLUDED.exit_time,
                     exit_price = EXCLUDED.exit_price,
                     pnl = EXCLUDED.pnl,
                     return_pct = EXCLUDED.return_pct""",
                row,
            )
            return True
        except Exception as e:
            log.warning("insert_trade failed: %s", e)
            return False

    def get_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        """Get a single trade by trade_id."""
        rows = self.query(
            "SELECT * FROM trading.trades WHERE trade_id = %s", (trade_id,)
        )
        return rows[0] if rows else None

    def get_trades(
        self,
        trader_id: Optional[str] = None,
        ticker: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get trades with optional filters."""
        conditions = []
        params: List[Any] = []
        if trader_id:
            conditions.append("trader_id = %s")
            params.append(trader_id)
        if ticker:
            conditions.append("ticker = %s")
            params.append(ticker)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return self.query(
            f"SELECT * FROM trading.trades {where} ORDER BY entry_time DESC LIMIT %s",
            tuple(params) + (limit,),
        )

    def get_trade_pnl_sum(self, trader_id: Optional[str] = None) -> float:
        """Sum of all trade P&L for a trader (or all)."""
        if trader_id:
            rows = self.query(
                "SELECT COALESCE(SUM(pnl), 0) as total FROM trading.trades WHERE trader_id = %s",
                (trader_id,),
            )
        else:
            rows = self.query("SELECT COALESCE(SUM(pnl), 0) as total FROM trading.trades")
        return float(rows[0]["total"]) if rows else 0.0

    # ── Agent Params (config_loader.py) ──────────────────────────────────

    def get_param(self, trader_id: str, param_name: str) -> Optional[float]:
        """Get a single parameter value."""
        rows = self.query(
            "SELECT param_value FROM trading.params WHERE trader_id = %s AND param_name = %s",
            (trader_id, param_name),
        )
        return float(rows[0]["param_value"]) if rows else None

    def get_params(self, trader_id: str, prefix: Optional[str] = None) -> Dict[str, float]:
        """Get all params for a trader, optionally filtered by prefix."""
        if prefix:
            rows = self.query(
                "SELECT param_name, param_value FROM trading.params "
                "WHERE trader_id = %s AND param_name LIKE %s ORDER BY param_name",
                (trader_id, f"{prefix}%"),
            )
        else:
            rows = self.query(
                "SELECT param_name, param_value FROM trading.params "
                "WHERE trader_id = %s ORDER BY param_name",
                (trader_id,),
            )
        return {r["param_name"]: float(r["param_value"]) for r in rows}

    def upsert_param(
        self,
        trader_id: str,
        param_name: str,
        param_value: float,
        min_val: float = 0.0,
        max_val: float = 1.0,
        updated_by: str = "system",
    ) -> bool:
        """Insert or update a parameter. Returns True on success."""
        try:
            self.execute(
                """INSERT INTO trading.params
                   (trader_id, param_name, param_value, min_val, max_val, updated_by)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (trader_id, param_name) DO UPDATE SET
                     param_value = EXCLUDED.param_value,
                     updated_by = EXCLUDED.updated_by,
                     created_at = NOW()""",
                (trader_id, param_name, param_value, min_val, max_val, updated_by),
            )
            return True
        except Exception as e:
            log.warning("upsert_param failed: %s", e)
            return False

    # ── Sweep Results (historical_sim.py) ────────────────────────────────

    def create_sweep_run(
        self, trader_id: str, n_scenarios: int, model_used: str = "", data_days: int = 0
    ) -> int:
        """Create a sweep_run and return its id."""
        psycopg2, _ = _ensure_psycopg2()
        with self.conn() as cn:
            with cn.cursor() as cur:
                cur.execute(
                    """INSERT INTO trading.sweep_runs
                       (trader_id, n_scenarios, model_used, data_days)
                       VALUES (%s, %s, %s, %s) RETURNING id""",
                    (trader_id, n_scenarios, model_used, data_days),
                )
                return cur.fetchone()[0]

    def finish_sweep_run(self, run_id: int, best_score: Optional[float] = None):
        """Mark a sweep run as finished."""
        self.execute(
            "UPDATE trading.sweep_runs SET finished_at = NOW(), best_score = COALESCE(%s, best_score) WHERE id = %s",
            (best_score, run_id),
        )

    def insert_sweep_results(self, rows: List[Dict[str, Any]]) -> int:
        """Insert sweep result rows. Returns row count."""
        if not rows:
            return 0
        psycopg2, extras = _ensure_psycopg2()
        columns = list(rows[0].keys())
        col_str = ", ".join(columns)
        with self.conn() as cn:
            with cn.cursor() as cur:
                extras.execute_values(
                    cur,
                    f"INSERT INTO trading.sweep_results ({col_str}) VALUES %s",
                    rows,
                    template=f"({', '.join(f'%({c})s' for c in columns)})",
                )
                return cur.rowcount

    def get_sweep_results(self, trader_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent sweep results."""
        if trader_id:
            return self.query(
                "SELECT * FROM trading.sweep_results WHERE trader_id = %s ORDER BY id DESC LIMIT %s",
                (trader_id, limit),
            )
        return self.query(
            "SELECT * FROM trading.sweep_results ORDER BY id DESC LIMIT %s",
            (limit,),
        )

    # ── Health ───────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """Check database connectivity."""
        try:
            rows = self.query("SELECT 1")
            return len(rows) == 1
        except Exception:
            return False


# ── Singleton ────────────────────────────────────────────────────────────────

_store: Optional[UnifiedStore] = None


def get_store(dsn: Optional[str] = None) -> UnifiedStore:
    """Get or create the singleton UnifiedStore instance."""
    global _store
    if _store is None or dsn is not None:
        _store = UnifiedStore(dsn=dsn)
    return _store
