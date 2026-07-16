"""
tests/test_unified_store.py — Unit tests for UnifiedStore.

Tests the interface without a real Postgres connection by mocking
psycopg2 at the connection/cursor level.
"""

import os
import sys
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db.unified_store import UnifiedStore, get_store


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_psycopg2():
    """Mock psycopg2.connect to return a mock connection/cursor."""
    with (
        patch("db.unified_store._psycopg2") as mock_pg,
        patch("db.unified_store._psycopg2_extras") as mock_extras,
    ):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # connection context manager: with self.conn() as cn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        # cursor context manager: with cn.cursor() as cur
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_pg.connect.return_value = mock_conn
        yield mock_pg, mock_extras, mock_conn, mock_cur


@pytest.fixture
def store(mock_psycopg2):
    """Create a UnifiedStore with mocked psycopg2."""
    mock_pg, mock_extras, mock_conn, mock_cur = mock_psycopg2
    return UnifiedStore(dsn="host=test port=5432 dbname=test user=test")


# ── Ping ────────────────────────────────────────────────────────────────────


class TestPing:
    def test_ping_success(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.description = [("?column?",)]
        mock_cur.fetchall.return_value = [(1,)]
        assert store.ping() is True

    def test_ping_failure(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.execute.side_effect = Exception("connection refused")
        assert store.ping() is False


# ── Query / Execute ──────────────────────────────────────────────────────────


class TestQuery:
    def test_query_returns_dicts(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.description = [("id",), ("name",)]
        mock_cur.fetchall.return_value = [(1, "test")]
        rows = store.query("SELECT id, name FROM test")
        assert rows == [{"id": 1, "name": "test"}]

    def test_query_with_params(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.description = [("id",)]
        mock_cur.fetchall.return_value = [(42,)]
        rows = store.query("SELECT id FROM test WHERE id = %s", (42,))
        assert rows == [{"id": 42}]
        mock_cur.execute.assert_called_once_with(
            "SELECT id FROM test WHERE id = %s", (42,)
        )

    def test_query_empty_result(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.description = [("id",)]
        mock_cur.fetchall.return_value = []
        rows = store.query("SELECT id FROM test WHERE id = 999")
        assert rows == []


class TestExecute:
    def test_execute_returns_rowcount(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.rowcount = 1
        count = store.execute("INSERT INTO test VALUES (%s)", ("val",))
        assert count == 1


# ── Replay Ticks ─────────────────────────────────────────────────────────────


class TestReplayTicks:
    def test_ensure_table(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        store.ensure_replay_ticks_table()
        # Should have been called at least once (CREATE TABLE + CREATE INDEX)
        assert mock_cur.execute.call_count >= 1

    def test_insert_replay_ticks(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.rowcount = 1
        rows = [
            {"ticker": "SPY", "timestamp": "2026-07-15T10:00:00Z", "open": 500.0,
             "high": 501.0, "low": 499.0, "close": 500.5, "volume": 1000000}
        ]
        count = store.insert_replay_ticks(rows)
        assert count == 1

    def test_insert_empty_ticks(self, store, mock_psycopg2):
        assert store.insert_replay_ticks([]) == 0

    def test_get_replay_ticks(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.description = [
            ("ticker",), ("timestamp",), ("open",), ("close",), ("volume",)
        ]
        mock_cur.fetchall.return_value = [
            ("SPY", "2026-07-15T10:00:00Z", 500.0, 500.5, 1000000)
        ]
        rows = store.get_replay_ticks(tickers=["SPY"], start_date="2026-07-15")
        assert len(rows) == 1
        assert rows[0]["ticker"] == "SPY"


# ── Trades ───────────────────────────────────────────────────────────────────


class TestTrades:
    def test_insert_trade(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        result = store.insert_trade({
            "trader_id": "kairos",
            "trade_id": "K-001",
            "ticker": "AAPL",
            "entry_time": "2026-07-15T10:00:00Z",
            "exit_time": "2026-07-15T15:00:00Z",
            "entry_price": 200.0,
            "exit_price": 205.0,
            "shares": 10,
            "pnl": 50.0,
            "return_pct": 2.5,
            "regime": "bullish",
        })
        assert result is True

    def test_insert_trade_handles_error(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.execute.side_effect = Exception("unique violation")
        result = store.insert_trade({"trader_id": "kairos", "trade_id": "K-001",
                                      "ticker": "AAPL", "entry_time": "2026-01-01",
                                      "entry_price": 200.0, "shares": 10})
        assert result is False

    def test_get_trade_found(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.description = [
            ("trader_id",), ("trade_id",), ("ticker",), ("pnl",)
        ]
        mock_cur.fetchall.return_value = [
            ("kairos", "K-001", "AAPL", 50.0)
        ]
        trade = store.get_trade("K-001")
        assert trade is not None
        assert trade["pnl"] == 50.0

    def test_get_trade_not_found(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.description = []
        mock_cur.fetchall.return_value = []
        trade = store.get_trade("NONEXISTENT")
        assert trade is None

    def test_get_trade_pnl_sum(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.description = [("total",)]
        mock_cur.fetchall.return_value = [(150.0,)]
        total = store.get_trade_pnl_sum("kairos")
        assert total == 150.0


# ── Params ───────────────────────────────────────────────────────────────────


class TestParams:
    def test_get_param_found(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.description = [("param_value",)]
        mock_cur.fetchall.return_value = [(0.75,)]
        val = store.get_param("kairos", "rsi_threshold")
        assert val == 0.75

    def test_get_param_not_found(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.description = [("param_value",)]
        mock_cur.fetchall.return_value = []
        val = store.get_param("kairos", "nonexistent")
        assert val is None

    def test_get_params_with_prefix(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.description = [("param_name",), ("param_value",)]
        mock_cur.fetchall.return_value = [
            ("rsi_threshold", 0.7), ("rsi_oversold", 0.3)
        ]
        params = store.get_params("kairos", prefix="rsi_")
        assert params == {"rsi_threshold": 0.7, "rsi_oversold": 0.3}

    def test_upsert_param(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        result = store.upsert_param("kairos", "rsi_threshold", 0.75)
        assert result is True

    def test_upsert_param_handles_error(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.execute.side_effect = Exception("constraint violation")
        result = store.upsert_param("kairos", "rsi_threshold", 0.75)
        assert result is False


# ── Sweep Results ────────────────────────────────────────────────────────────


class TestSweepResults:
    def test_create_sweep_run(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.fetchone.return_value = [42]
        run_id = store.create_sweep_run("kairos", 100)
        assert run_id == 42

    def test_finish_sweep_run(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        store.finish_sweep_run(42, best_score=0.85)
        # Should not raise

    def test_insert_sweep_results(self, store, mock_psycopg2):
        _, _, _, mock_cur = mock_psycopg2
        mock_cur.rowcount = 5
        rows = [{"run_id": 42, "trader_id": "kairos", "variant_id": 1,
                 "params_hash": "abc", "objective_score": 0.8}]
        count = store.insert_sweep_results(rows)
        assert count == 5

    def test_insert_empty_sweep_results(self, store, mock_psycopg2):
        assert store.insert_sweep_results([]) == 0


# ── Singleton ────────────────────────────────────────────────────────────────


class TestGetStore:
    def test_returns_same_instance(self):
        s1 = get_store(dsn="host=test")
        s2 = get_store()
        assert s1 is s2

    def test_new_dsn_replaces(self):
        s1 = get_store(dsn="host=test")
        s2 = get_store(dsn="host=other")
        # With a new DSN, should be a new instance
        assert s1 is not s2
