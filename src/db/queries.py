"""Typed database query helpers for paper trading.

All functions use the async connection pool from `connection.py`
and return asyncpg Record / list[Record] objects.
"""
from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence

import asyncpg

from .connection import execute, fetch, fetchrow

# ---------------------------------------------------------------------------
# market_data.bars
# ---------------------------------------------------------------------------


async def insert_bar(
    ticker: str,
    timestamp: datetime,
    open: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: int,
    interval: str = "1d",
) -> None:
    """Insert a single OHLCV bar."""
    await execute(
        """
        INSERT INTO market_data.bars
            (ticker, timestamp, open, high, low, close, volume, interval)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        ticker, timestamp, open, high, low, close, volume, interval,
    )


async def insert_bars_batch(rows: Sequence[tuple]) -> None:
    """Bulk-insert bars via executemany."""
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO market_data.bars
                (ticker, timestamp, open, high, low, close, volume, interval)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            rows,
        )


async def get_bars(
    ticker: str,
    start: datetime,
    end: datetime,
    interval: Optional[str] = None,
) -> list[asyncpg.Record]:
    """Fetch bars for a ticker within a time window, optionally filtering by interval."""
    if interval:
        return await fetch(
            """
            SELECT * FROM market_data.bars
            WHERE ticker = $1 AND timestamp >= $2 AND timestamp <= $3 AND interval = $4
            ORDER BY timestamp
            """,
            ticker, start, end, interval,
        )
    return await fetch(
        """
        SELECT * FROM market_data.bars
        WHERE ticker = $1 AND timestamp >= $2 AND timestamp <= $3
        ORDER BY timestamp
        """,
        ticker, start, end,
    )


async def get_latest_bar(ticker: str) -> Optional[asyncpg.Record]:
    """Return the most recent bar for a ticker."""
    return await fetchrow(
        """
        SELECT * FROM market_data.bars
        WHERE ticker = $1
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        ticker,
    )


# ---------------------------------------------------------------------------
# market_data.news
# ---------------------------------------------------------------------------


async def insert_news(
    url_hash: str,
    ticker: str,
    title: str,
    published_at: datetime,
    body: Optional[str] = None,
    sentiment: Optional[Decimal] = None,
) -> None:
    """Insert a news article."""
    await execute(
        """
        INSERT INTO market_data.news
            (url_hash, ticker, title, body, sentiment, published_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        url_hash, ticker, title, body, sentiment, published_at,
    )


async def get_news(
    ticker: str,
    start: datetime,
    end: datetime,
    limit: int = 50,
) -> list[asyncpg.Record]:
    """Fetch recent news for a ticker."""
    return await fetch(
        """
        SELECT * FROM market_data.news
        WHERE ticker = $1 AND published_at >= $2 AND published_at <= $3
        ORDER BY published_at DESC
        LIMIT $4
        """,
        ticker, start, end, limit,
    )


# ---------------------------------------------------------------------------
# market_data.regimes
# ---------------------------------------------------------------------------


async def insert_regime(
    date: Date,
    regime: str,
    confidence: Decimal,
    features_jsonb: Optional[dict] = None,
) -> None:
    """Upsert a market regime snapshot for a date."""
    import json
    await execute(
        """
        INSERT INTO market_data.regimes (date, regime, confidence, features_jsonb)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (date) DO UPDATE SET
            regime     = EXCLUDED.regime,
            confidence = EXCLUDED.confidence,
            features_jsonb = EXCLUDED.features_jsonb
        """,
        date, regime, confidence, json.dumps(features_jsonb) if features_jsonb else None,
    )


async def get_regime(date: Date) -> Optional[asyncpg.Record]:
    """Get market regime for a specific date."""
    return await fetchrow(
        "SELECT * FROM market_data.regimes WHERE date = $1", date,
    )


# ---------------------------------------------------------------------------
# trading.signals
# ---------------------------------------------------------------------------


async def insert_signal(
    trader_id: str,
    ticker: str,
    timestamp: datetime,
    composite_signal: Decimal,
    conviction: Decimal,
    momentum: Optional[Decimal] = None,
    rsi: Optional[Decimal] = None,
    regime: Optional[str] = None,
) -> None:
    """Insert a composite signal row."""
    await execute(
        """
        INSERT INTO trading.signals
            (trader_id, ticker, timestamp, composite_signal, conviction,
             momentum, rsi, regime)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        trader_id, ticker, timestamp, composite_signal, conviction,
        momentum, rsi, regime,
    )


async def get_recent_signals(
    trader_id: str,
    since: Optional[datetime] = None,
    limit: int = 100,
) -> list[asyncpg.Record]:
    """Get recent signals for a trader, optionally since a timestamp."""
    if since:
        return await fetch(
            """
            SELECT * FROM trading.signals
            WHERE trader_id = $1 AND timestamp >= $2
            ORDER BY timestamp DESC
            LIMIT $3
            """,
            trader_id, since, limit,
        )
    return await fetch(
        """
        SELECT * FROM trading.signals
        WHERE trader_id = $1
        ORDER BY timestamp DESC
        LIMIT $2
        """,
        trader_id, limit,
    )


# ---------------------------------------------------------------------------
# trading.decisions
# ---------------------------------------------------------------------------


async def insert_decision(
    trader_id: str,
    ticker: str,
    timestamp: datetime,
    decision: str,
    conviction: Decimal,
    rationale: Optional[str] = None,
    prompt_variant_id: Optional[int] = None,
    params_hash: Optional[str] = None,
) -> None:
    """Insert a trading decision record."""
    await execute(
        """
        INSERT INTO trading.decisions
            (trader_id, ticker, timestamp, decision, conviction,
             rationale, prompt_variant_id, params_hash)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        trader_id, ticker, timestamp, decision, conviction,
        rationale, prompt_variant_id, params_hash,
    )


async def get_recent_decisions(
    trader_id: str,
    ticker: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = 50,
) -> list[asyncpg.Record]:
    """Get recent decisions for a trader, optionally filtered by ticker/time."""
    if ticker and since:
        return await fetch(
            """
            SELECT * FROM trading.decisions
            WHERE trader_id = $1 AND ticker = $2 AND timestamp >= $3
            ORDER BY timestamp DESC
            LIMIT $4
            """,
            trader_id, ticker, since, limit,
        )
    if ticker:
        return await fetch(
            """
            SELECT * FROM trading.decisions
            WHERE trader_id = $1 AND ticker = $2
            ORDER BY timestamp DESC
            LIMIT $3
            """,
            trader_id, ticker, limit,
        )
    if since:
        return await fetch(
            """
            SELECT * FROM trading.decisions
            WHERE trader_id = $1 AND timestamp >= $2
            ORDER BY timestamp DESC
            LIMIT $3
            """,
            trader_id, since, limit,
        )
    return await fetch(
        """
        SELECT * FROM trading.decisions
        WHERE trader_id = $1
        ORDER BY timestamp DESC
        LIMIT $2
        """,
        trader_id, limit,
    )


# ---------------------------------------------------------------------------
# trading.trades
# ---------------------------------------------------------------------------


async def insert_trade(
    trader_id: str,
    trade_id: str,
    ticker: str,
    entry_time: datetime,
    entry_price: Decimal,
    shares: int,
    exit_time: Optional[datetime] = None,
    exit_price: Optional[Decimal] = None,
    pnl: Optional[Decimal] = None,
    return_pct: Optional[Decimal] = None,
) -> None:
    """Insert a trade (initially open; later closed via close_trade)."""
    await execute(
        """
        INSERT INTO trading.trades
            (trader_id, trade_id, ticker, entry_time, exit_time,
             entry_price, exit_price, shares, pnl, return_pct)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (trade_id) DO NOTHING
        """,
        trader_id, trade_id, ticker, entry_time, exit_time,
        entry_price, exit_price, shares, pnl, return_pct,
    )


async def get_open_trades(
    trader_id: str,
    ticker: Optional[str] = None,
) -> list[asyncpg.Record]:
    """Get all trades that are still open (exit_time IS NULL)."""
    if ticker:
        return await fetch(
            """
            SELECT * FROM trading.trades
            WHERE trader_id = $1 AND ticker = $2 AND exit_time IS NULL
            ORDER BY entry_time
            """,
            trader_id, ticker,
        )
    return await fetch(
        """
        SELECT * FROM trading.trades
        WHERE trader_id = $1 AND exit_time IS NULL
        ORDER BY entry_time
        """,
        trader_id,
    )


async def close_trade(
    trade_id: str,
    exit_time: datetime,
    exit_price: Decimal,
    pnl: Decimal,
    return_pct: Decimal,
) -> None:
    """Close an open trade with exit details."""
    await execute(
        """
        UPDATE trading.trades
        SET exit_time  = $2,
            exit_price = $3,
            pnl        = $4,
            return_pct = $5
        WHERE trade_id = $1 AND exit_time IS NULL
        """,
        trade_id, exit_time, exit_price, pnl, return_pct,
    )


async def get_trades(
    trader_id: str,
    ticker: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = 100,
) -> list[asyncpg.Record]:
    """Get completed trades for a trader."""
    clause = "trader_id = $1"
    params: list = [trader_id]
    if ticker:
        clause += " AND ticker = $2"
        params.append(ticker)
    if since:
        idx = len(params) + 1
        clause += f" AND entry_time >= ${idx}"
        params.append(since)
    idx = len(params) + 1
    return await fetch(
        f"""
        SELECT * FROM trading.trades
        WHERE {clause} AND exit_time IS NOT NULL
        ORDER BY entry_time DESC
        LIMIT ${idx}
        """,
        *params, limit,
    )


# ---------------------------------------------------------------------------
# trading.journal
# ---------------------------------------------------------------------------


async def insert_journal_entry(
    trader_id: str,
    timestamp: datetime,
    ticker: str,
    decision: str,
    equity: Decimal,
    drawdown_pct: Decimal,
    rationale: Optional[str] = None,
) -> None:
    """Insert a journal entry."""
    await execute(
        """
        INSERT INTO trading.journal
            (trader_id, timestamp, ticker, decision, rationale, equity, drawdown_pct)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        trader_id, timestamp, ticker, decision, rationale, equity, drawdown_pct,
    )


# ---------------------------------------------------------------------------
# trading.params
# ---------------------------------------------------------------------------


async def insert_param(
    trader_id: str,
    param_name: str,
    param_value: Decimal,
    min_val: Decimal,
    max_val: Decimal,
    updated_by: str = "system",
) -> None:
    """Upsert a parameter for a trader."""
    await execute(
        """
        INSERT INTO trading.params
            (trader_id, param_name, param_value, min_val, max_val, updated_by, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT (trader_id, param_name) DO UPDATE SET
            param_value = EXCLUDED.param_value,
            min_val     = EXCLUDED.min_val,
            max_val     = EXCLUDED.max_val,
            updated_by  = EXCLUDED.updated_by,
            updated_at  = NOW()
        """,
        trader_id, param_name, param_value, min_val, max_val, updated_by,
    )


async def get_params(trader_id: str) -> list[asyncpg.Record]:
    """Get all parameters for a trader."""
    return await fetch(
        "SELECT * FROM trading.params WHERE trader_id = $1 ORDER BY param_name",
        trader_id,
    )


# ---------------------------------------------------------------------------
# trading.sweep_runs
# ---------------------------------------------------------------------------


async def insert_sweep_run(
    trader_id: str,
    n_scenarios: int = 0,
) -> int:
    """Create a new sweep run, returning the run_id."""
    row = await fetchrow(
        """
        INSERT INTO trading.sweep_runs (trader_id, n_scenarios, started_at)
        VALUES ($1, $2, NOW())
        RETURNING run_id
        """,
        trader_id, n_scenarios,
    )
    assert row is not None
    return row["run_id"]


async def complete_sweep_run(
    run_id: int,
    best_score: Decimal,
    best_variant_id: int,
    best_params_hash: str,
    n_scenarios: int,
) -> None:
    """Mark a sweep run as finished with results."""
    await execute(
        """
        UPDATE trading.sweep_runs
        SET finished_at      = NOW(),
            best_score       = $2,
            best_variant_id  = $3,
            best_params_hash = $4,
            n_scenarios      = $5
        WHERE run_id = $1
        """,
        run_id, best_score, best_variant_id, best_params_hash, n_scenarios,
    )


async def get_sweep_runs(
    trader_id: str,
    limit: int = 10,
) -> list[asyncpg.Record]:
    """Get recent sweep runs for a trader."""
    return await fetch(
        """
        SELECT * FROM trading.sweep_runs
        WHERE trader_id = $1
        ORDER BY started_at DESC
        LIMIT $2
        """,
        trader_id, limit,
    )


# ---------------------------------------------------------------------------
# trading.sweep_results
# ---------------------------------------------------------------------------


async def insert_sweep_result(
    run_id: int,
    trader_id: str,
    variant_id: int,
    params_hash: str,
    calmar: Optional[Decimal] = None,
    sortino: Optional[Decimal] = None,
    profit_factor: Optional[Decimal] = None,
    expectancy: Optional[Decimal] = None,
    total_pnl: Optional[Decimal] = None,
    n_ticks: int = 0,
    n_trades: int = 0,
    win_rate: Optional[Decimal] = None,
) -> None:
    """Insert a sweep result row."""
    await execute(
        """
        INSERT INTO trading.sweep_results
            (run_id, trader_id, variant_id, params_hash,
             calmar, sortino, profit_factor, expectancy, total_pnl,
             n_ticks, n_trades, win_rate)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (run_id, variant_id) DO UPDATE SET
            params_hash   = EXCLUDED.params_hash,
            calmar        = EXCLUDED.calmar,
            sortino       = EXCLUDED.sortino,
            profit_factor = EXCLUDED.profit_factor,
            expectancy    = EXCLUDED.expectancy,
            total_pnl     = EXCLUDED.total_pnl,
            n_ticks       = EXCLUDED.n_ticks,
            n_trades      = EXCLUDED.n_trades,
            win_rate      = EXCLUDED.win_rate
        """,
        run_id, trader_id, variant_id, params_hash,
        calmar, sortino, profit_factor, expectancy, total_pnl,
        n_ticks, n_trades, win_rate,
    )


# ---------------------------------------------------------------------------
# trading.equity_snapshots
# ---------------------------------------------------------------------------


async def insert_equity_snapshot(
    trader_id: str,
    date: Date,
    equity: Decimal,
    cash: Decimal,
    pnl: Decimal = Decimal("0"),
    calmar_30d: Optional[Decimal] = None,
    calmar_90d: Optional[Decimal] = None,
    sharpe_30d: Optional[Decimal] = None,
    profit_factor: Optional[Decimal] = None,
    win_rate: Optional[Decimal] = None,
    max_drawdown: Optional[Decimal] = None,
    trades_closed: int = 0,
    trades_won: int = 0,
) -> None:
    """Upsert a daily equity snapshot."""
    await execute(
        """
        INSERT INTO trading.equity_snapshots
            (trader_id, date, equity, cash, pnl,
             calmar_30d, calmar_90d, sharpe_30d,
             profit_factor, win_rate, max_drawdown,
             trades_closed, trades_won)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        ON CONFLICT (trader_id, date) DO UPDATE SET
            equity        = EXCLUDED.equity,
            cash          = EXCLUDED.cash,
            pnl           = EXCLUDED.pnl,
            calmar_30d    = EXCLUDED.calmar_30d,
            calmar_90d    = EXCLUDED.calmar_90d,
            sharpe_30d    = EXCLUDED.sharpe_30d,
            profit_factor = EXCLUDED.profit_factor,
            win_rate      = EXCLUDED.win_rate,
            max_drawdown  = EXCLUDED.max_drawdown,
            trades_closed = EXCLUDED.trades_closed,
            trades_won    = EXCLUDED.trades_won
        """,
        trader_id, date, equity, cash, pnl,
        calmar_30d, calmar_90d, sharpe_30d,
        profit_factor, win_rate, max_drawdown,
        trades_closed, trades_won,
    )


async def get_equity_snapshot(
    trader_id: str,
    date: Date,
) -> Optional[asyncpg.Record]:
    """Get equity snapshot for a single date."""
    return await fetchrow(
        """
        SELECT * FROM trading.equity_snapshots
        WHERE trader_id = $1 AND date = $2
        """,
        trader_id, date,
    )


async def get_equity_history(
    trader_id: str,
    start: Date,
    end: Date,
) -> list[asyncpg.Record]:
    """Get equity snapshots in a date range."""
    return await fetch(
        """
        SELECT * FROM trading.equity_snapshots
        WHERE trader_id = $1 AND date >= $2 AND date <= $3
        ORDER BY date
        """,
        trader_id, start, end,
    )


# ---------------------------------------------------------------------------
# trading.risk_state — circuit breaker pause management
# ---------------------------------------------------------------------------


async def upsert_risk_state(
    agent_id: str,
    is_paused: bool = False,
    paused_reason: Optional[str] = None,
    paused_at: Optional[datetime] = None,
) -> None:
    """Upsert risk_state row for circuit breaker pause management.

    Args:
        agent_id: Trader agent ID (e.g. trader-kairos).
        is_paused: Whether the trader is paused.
        paused_reason: Human-readable reason for the pause.
        paused_at: When the pause started (None to clear).
    """
    await execute(
        """
        INSERT INTO trading.risk_state (agent_id, is_paused, paused_reason, paused_at, updated_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (agent_id) DO UPDATE SET
            is_paused     = EXCLUDED.is_paused,
            paused_reason = EXCLUDED.paused_reason,
            paused_at     = EXCLUDED.paused_at,
            updated_at    = NOW()
        """,
        agent_id, is_paused, paused_reason, paused_at,
    )


async def get_risk_state(agent_id: str) -> Optional[asyncpg.Record]:
    """Get risk_state row for an agent."""
    return await fetchrow(
        "SELECT * FROM trading.risk_state WHERE agent_id = $1",
        agent_id,
    )


async def clear_pause(agent_id: str) -> None:
    """Clear pause state for an agent."""
    await upsert_risk_state(
        agent_id=agent_id,
        is_paused=False,
        paused_reason=None,
        paused_at=None,
    )


async def is_trader_paused(agent_id: str) -> bool:
    """Check if a trader is currently paused."""
    row = await get_risk_state(agent_id)
    if not row:
        return False
    return bool(row.get("is_paused", False))


async def get_all_risk_states() -> list[asyncpg.Record]:
    """Get risk_state for all agents."""
    return await fetch(
        "SELECT * FROM trading.risk_state ORDER BY agent_id"
    )


# ---------------------------------------------------------------------------
# Schema initialisation helper
# ---------------------------------------------------------------------------


async def init_schema() -> None:
    """Run schema.sql to create tables and indexes (idempotent via IF NOT EXISTS)."""
    import os
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        sql = f.read()
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(sql)


async def _pool() -> asyncpg.Pool:
    """Internal helper so queries.py is self-contained."""
    from .connection import get_pool
    return await get_pool()
