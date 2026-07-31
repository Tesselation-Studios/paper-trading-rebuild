"""Tests for reflection_cron.py's stats computation and trader.db trade fetching.

Covers the fix for the NULL-pnl win/loss bug: `realized_pnl` (dollar) is NULL
for every trade in trader.db closed before 2026-07-30 (dollar-pnl recording
gap, never backfilled), while `realized_return_pct` is populated for all
closed trades. `_is_win()` must fall back to return_pct when pnl is missing,
and `_get_trades()` must read trader.db directly instead of the separately-
populated (and lossier) Postgres trading.trades table.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from src.reflection_cron import _is_win, _get_trades, compute_trade_stats, STONKS_DB_PATH


# ── _is_win ──────────────────────────────────────────────────────────────────


def test_is_win_prefers_positive_pnl():
    assert _is_win({"pnl": 5.0, "return_pct": -3.0}) is True


def test_is_win_prefers_negative_pnl_even_if_return_pct_positive():
    assert _is_win({"pnl": -5.0, "return_pct": 3.0}) is False


def test_is_win_zero_pnl_is_not_a_win():
    assert _is_win({"pnl": 0.0, "return_pct": 1.0}) is False


def test_is_win_falls_back_to_return_pct_when_pnl_none():
    assert _is_win({"pnl": None, "return_pct": 2.5}) is True
    assert _is_win({"pnl": None, "return_pct": -2.5}) is False


def test_is_win_false_when_both_missing():
    assert _is_win({"pnl": None, "return_pct": None}) is False
    assert _is_win({}) is False


# ── compute_trade_stats: NULL-pnl fallback ───────────────────────────────────


def _trade(ticker, pnl, return_pct, sector=None, exit_time=None, entry_time=None, conviction=None):
    return {
        "id": ticker,
        "ticker": ticker,
        "shares": 1,
        "entry_price": 10.0,
        "entry_time": entry_time or datetime(2026, 7, 20, 9, 30),
        "exit_time": exit_time or datetime(2026, 7, 20, 15, 0),
        "sector": sector,
        "pnl": pnl,
        "return_pct": return_pct,
        "entry_conviction": conviction,
    }


def test_compute_trade_stats_null_pnl_rows_use_return_pct_for_wins():
    """Reproduces the exact bug: old rows with pnl=None but positive return_pct
    must count as wins, not silently collapse into the loss bucket."""
    trades = [
        _trade("AAA", pnl=None, return_pct=5.0),   # old row, real win
        _trade("BBB", pnl=None, return_pct=-3.0),  # old row, real loss
        _trade("CCC", pnl=None, return_pct=2.0),   # old row, real win
        _trade("DDD", pnl=-1.5, return_pct=-4.0),  # new row, real loss
    ]
    stats = compute_trade_stats(trades, signals=None)
    assert stats["rolling_stats"]["overall"] == 0.5  # 2 wins / 4 trades
    assert stats["rolling_stats"]["total_closed"] == 4


def test_compute_trade_stats_by_sector_uses_return_pct_fallback():
    trades = [
        _trade("AAA", pnl=None, return_pct=5.0, sector=None),
        _trade("BBB", pnl=None, return_pct=3.0, sector=None),
        _trade("CCC", pnl=None, return_pct=-1.0, sector=None),
    ]
    stats = compute_trade_stats(trades, signals=None)
    unknown = stats["by_sector"]["Unknown"]
    assert unknown["wins"] == 2
    assert unknown["losses"] == 1
    assert unknown["total"] == 3


def test_compute_trade_stats_uses_real_sector_field_over_lookup():
    trades = [_trade("ZZZ", pnl=1.0, return_pct=1.0, sector="Real Estate")]
    stats = compute_trade_stats(trades, signals=None)
    assert "Real Estate" in stats["by_sector"]
    assert "Unknown" not in stats["by_sector"]


def test_compute_trade_stats_today_stats_uses_return_pct_fallback():
    today = datetime.combine(date.today(), datetime.min.time()).replace(hour=10)
    trades = [
        _trade("AAA", pnl=None, return_pct=5.0, exit_time=today, entry_time=today - timedelta(hours=1)),
        _trade("BBB", pnl=None, return_pct=-2.0, exit_time=today, entry_time=today - timedelta(hours=1)),
    ]
    stats = compute_trade_stats(trades, signals=None)
    ts = stats["today_stats"]
    assert ts["num_trades"] == 2
    assert ts["num_wins"] == 1
    assert ts["num_losses"] == 1


def test_compute_trade_stats_empty_trades():
    stats = compute_trade_stats([], signals=None)
    assert stats["total_trades"] == 0
    assert stats["by_sector"] == {}


# ── _get_trades: reads trader.db directly ────────────────────────────────────


@pytest.fixture
def fake_trader_db(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE positions (
            ticker TEXT PRIMARY KEY, shares REAL, entry_price REAL, entry_time TEXT,
            sector TEXT, thesis TEXT, status TEXT DEFAULT 'open',
            closed_at TEXT, close_reason TEXT, realized_pnl REAL, realized_return_pct REAL,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY, ticker TEXT, timestamp TEXT, decision TEXT,
            conviction REAL, rationale TEXT
        )
    """)
    conn.execute(
        "INSERT INTO positions (ticker, shares, entry_price, entry_time, sector, status, "
        "closed_at, realized_pnl, realized_return_pct, updated_at) VALUES "
        "('OLD', 1, 10.0, '2026-07-20T09:30:00-04:00', NULL, 'closed', "
        "'2026-07-21T15:00:00-04:00', NULL, 4.5, '2026-07-21T15:00:00-04:00')"
    )
    conn.execute(
        "INSERT INTO positions (ticker, shares, entry_price, entry_time, sector, status, "
        "closed_at, realized_pnl, realized_return_pct, updated_at) VALUES "
        "('NEW', 2, 20.0, '2026-07-30T09:30:00-04:00', 'Technology', 'closed', "
        "'2026-07-30T15:00:00-04:00', 3.2, 1.6, '2026-07-30T15:00:00-04:00')"
    )
    conn.execute(
        "INSERT INTO positions (ticker, shares, entry_price, entry_time, sector, status, "
        "updated_at) VALUES ('OPEN', 1, 5.0, '2026-07-31T09:30:00-04:00', NULL, 'open', "
        "'2026-07-31T09:30:00-04:00')"
    )
    conn.execute(
        "INSERT INTO decisions (ticker, timestamp, decision, conviction) VALUES "
        "('NEW', '2026-07-30T09:29:00-04:00', 'BUY', 0.82)"
    )
    conn.commit()
    conn.close()

    import src.reflection_cron as rc
    monkeypatch.setattr(rc, "STONKS_DB_PATH", db_path)
    return db_path


def test_get_trades_reads_only_closed_positions(fake_trader_db):
    trades = _get_trades("trader-stonks", limit=100)
    tickers = {t["ticker"] for t in trades}
    assert tickers == {"OLD", "NEW"}
    assert "OPEN" not in tickers


def test_get_trades_preserves_null_pnl_with_populated_return_pct(fake_trader_db):
    trades = _get_trades("trader-stonks", limit=100)
    old = next(t for t in trades if t["ticker"] == "OLD")
    assert old["pnl"] is None
    assert old["return_pct"] == 4.5


def test_get_trades_joins_entry_conviction_from_decisions(fake_trader_db):
    trades = _get_trades("trader-stonks", limit=100)
    new = next(t for t in trades if t["ticker"] == "NEW")
    assert new["entry_conviction"] == 0.82
    old = next(t for t in trades if t["ticker"] == "OLD")
    assert old["entry_conviction"] is None


def test_get_trades_parses_datetimes(fake_trader_db):
    trades = _get_trades("trader-stonks", limit=100)
    new = next(t for t in trades if t["ticker"] == "NEW")
    assert isinstance(new["exit_time"], datetime)
    assert isinstance(new["entry_time"], datetime)


def test_get_trades_missing_db_returns_empty(tmp_path, monkeypatch):
    import src.reflection_cron as rc
    monkeypatch.setattr(rc, "STONKS_DB_PATH", tmp_path / "does_not_exist.db")
    assert _get_trades("trader-stonks") == []


def test_get_trades_respects_limit(fake_trader_db):
    trades = _get_trades("trader-stonks", limit=1)
    assert len(trades) == 1
