"""asyncpg connection pool for paper trading database."""
import asyncpg
from typing import Optional

DB_URL = os.getenv("DB_URL", "postgresql://trader:@trading-db:5432/trading")

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10)
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def execute(sql: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(sql, *args)


async def fetch(sql: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(sql, *args)


async def fetchrow(sql: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(sql, *args)


# ── Sync compatibility layer ──────────────────────────────────────────────
# The fundamentals module and tests use sync Postgres.
# Until fully migrated, provide get_connection() as a psycopg2-compatible
# wrapper that uses the same connection string.

import psycopg2
import psycopg2.extras

_SYNC_URL = os.getenv("SYNC_DB_URL", "postgresql://trader:@trading-db:5432/trading")


def get_connection():
    """Return a sync psycopg2 connection (compatibility wrapper)."""
    conn = psycopg2.connect(_SYNC_URL)
    conn.autocommit = False
    return conn


def insert_bars_batch(conn, rows: list) -> int:
    """Insert OHLCV bars batch (compatibility wrapper)."""
    cur = conn.cursor()
    psycopg2.extras.execute_values(
        cur,
        """INSERT INTO market_data.bars
           (ticker, timestamp, interval, open, high, low, close, volume, source)
           VALUES %s
           ON CONFLICT (ticker, timestamp, interval) DO NOTHING""",
        rows,
        template="(%s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s)",
    )
    n = cur.rowcount
    conn.commit()
    cur.close()
    return n


def insert_sweep_result(conn, run_id: str, trader: str, variant_id: str,
                        score: float, pnl: float, ticks: int, trades: int,
                        win_rate: float, elapsed_s: float, model_used: str):
    """Insert a sweep result (compatibility wrapper)."""
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO trading.sweep_results
           (run_id, trader, variant_id, objective_score, total_pnl,
            ticks_processed, trades_executed, win_rate, elapsed_seconds, model)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (run_id, trader, variant_id, score, pnl, ticks, trades,
         win_rate, elapsed_s, model_used),
    )
    conn.commit()
    cur.close()
