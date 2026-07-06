"""asyncpg connection pool for paper trading database."""
import asyncpg
from typing import Optional

DB_URL = "postgresql://trader:trader-dev-2026@192.168.1.179:5433/trading"

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
