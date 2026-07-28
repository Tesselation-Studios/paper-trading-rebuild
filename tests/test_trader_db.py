"""Tests for trader_db module — SQLite bankroll state."""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.trader_db import (
    get_connection,
    upsert_bankroll_state,
    get_bankroll_state,
    get_or_create_bankroll_state,
    get_closed_trades,
    apply_graduated_sizing,
    BANKROLL_STATE_SCHEMA,
)


@pytest.fixture
def db_path():
    """Create a temporary SQLite database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.unlink(path)


# ═══════════════════════════════════════════════════════════════════════════════
# Schema tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSchema:
    def test_creates_table(self, db_path):
        """get_connection should create the bankroll_state table."""
        conn = get_connection(db_path)
        try:
            # Check table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bankroll_state'"
            )
            assert cursor.fetchone() is not None
        finally:
            conn.close()

    def test_ceiling_pct_column_type(self, db_path):
        """ceiling_pct should be REAL NOT NULL DEFAULT 0.067."""
        conn = get_connection(db_path)
        try:
            # Check column type
            cursor = conn.execute("PRAGMA table_info(bankroll_state)")
            columns = {row["name"]: row for row in cursor.fetchall()}
            assert "ceiling_pct" in columns
            col = columns["ceiling_pct"]
            assert "REAL" in col["type"].upper() or "FLOAT" in col["type"].upper()
        finally:
            conn.close()

    def test_ceiling_column_exists(self, db_path):
        """Old ceiling column should still exist."""
        conn = get_connection(db_path)
        try:
            cursor = conn.execute("PRAGMA table_info(bankroll_state)")
            columns = {row["name"]: row for row in cursor.fetchall()}
            assert "ceiling" in columns  # Derived/logged value
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# CRUD tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCRUD:
    def test_upsert_and_get(self, db_path):
        """Should insert and retrieve bankroll state."""
        upsert_bankroll_state(
            db_path, "trader-kairos",
            ceiling_pct=0.10, ceiling=1000.0,
            wins=5, losses=3, streak=2,
        )
        state = get_bankroll_state(db_path, "trader-kairos")
        assert state is not None
        assert state["ceiling_pct"] == pytest.approx(0.10)
        assert state["wins"] == 5
        assert state["streak"] == 2

    def test_upsert_updates(self, db_path):
        """Second upsert should update existing row."""
        upsert_bankroll_state(db_path, "trader-kairos", ceiling_pct=0.10)
        upsert_bankroll_state(db_path, "trader-kairos", ceiling_pct=0.12,
                              wins=10, streak=5)
        state = get_bankroll_state(db_path, "trader-kairos")
        assert state["ceiling_pct"] == pytest.approx(0.12)
        assert state["wins"] == 10

    def test_get_nonexistent(self, db_path):
        """Getting nonexistent trader should return None."""
        state = get_bankroll_state(db_path, "nonexistent")
        assert state is None

    def test_get_or_create_existing(self, db_path):
        """get_or_create should return existing state."""
        upsert_bankroll_state(db_path, "trader-kairos", ceiling_pct=0.10)
        state = get_or_create_bankroll_state(db_path, "trader-kairos")
        assert state["ceiling_pct"] == pytest.approx(0.10)

    def test_get_or_create_new(self, db_path):
        """get_or_create should create default state for new trader."""
        state = get_or_create_bankroll_state(db_path, "trader-new")
        assert state["ceiling_pct"] == pytest.approx(0.067)
        assert state["wins"] == 0
        assert "trader_id" in state

    def test_multiple_traders(self, db_path):
        """Should support multiple traders independently."""
        upsert_bankroll_state(db_path, "trader-kairos", ceiling_pct=0.10,
                              wins=5)
        upsert_bankroll_state(db_path, "trader-stonks", ceiling_pct=0.15,
                              wins=8)
        kairos = get_bankroll_state(db_path, "trader-kairos")
        stonks = get_bankroll_state(db_path, "trader-stonks")
        assert kairos["ceiling_pct"] == pytest.approx(0.10)
        assert stonks["ceiling_pct"] == pytest.approx(0.15)

    def test_default_ceiling_value(self, db_path):
        """Default ceiling_pct should be 0.067."""
        state = get_or_create_bankroll_state(db_path, "trader-default")
        assert state["ceiling_pct"] == pytest.approx(0.067)

    def test_peak_pct_tracking(self, db_path):
        """peak_pct should track highest value, not decrease."""
        upsert_bankroll_state(db_path, "trader-kairos", ceiling_pct=0.10,
                              peak_pct=0.10)
        upsert_bankroll_state(db_path, "trader-kairos", ceiling_pct=0.08,
                              peak_pct=0.08)
        state = get_bankroll_state(db_path, "trader-kairos")
        assert state["peak_pct"] >= 0.10  # Should be MAX of stored vs new


# ═══════════════════════════════════════════════════════════════════════════════
# get_closed_trades tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetClosedTrades:
    def test_no_trades_table(self, db_path):
        """If trades table doesn't exist, should return empty list."""
        conn = get_connection(db_path)
        conn.close()  # Just ensures schema exists
        trades = get_closed_trades(db_path, "trader-kairos")
        assert trades == []

    def test_reads_from_trades_table(self, db_path):
        """Should read from trades table if it exists."""
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                agent_id TEXT,
                ticker TEXT,
                pnl REAL,
                exit_time TEXT
            )
        """)
        conn.execute(
            "INSERT INTO trades (agent_id, ticker, pnl, exit_time) VALUES (?, ?, ?, ?)",
            ("trader-kairos", "AAPL", 100.0, "2026-07-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO trades (agent_id, ticker, pnl, exit_time) VALUES (?, ?, ?, ?)",
            ("trader-kairos", "TSLA", -50.0, "2026-07-02T00:00:00"),
        )
        conn.commit()
        conn.close()

        trades = get_closed_trades(db_path, "trader-kairos")
        assert len(trades) == 2
        pnls = [t["pnl"] for t in trades]
        assert 100.0 in pnls
        assert -50.0 in pnls

    def test_reads_from_executed_trades(self, db_path):
        """Should fall back to executed_trades table."""
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE executed_trades (
                id INTEGER PRIMARY KEY,
                agent_id TEXT,
                ticker TEXT,
                pnl REAL,
                exit_time TEXT,
                status TEXT
            )
        """)
        conn.execute(
            "INSERT INTO executed_trades (agent_id, ticker, pnl, exit_time, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trader-kairos", "AAPL", 75.0, "2026-07-01T00:00:00", "closed"),
        )
        conn.commit()
        conn.close()

        trades = get_closed_trades(db_path, "trader-kairos")
        assert len(trades) == 1
        assert trades[0]["pnl"] == 75.0

    def test_filters_by_trader(self, db_path):
        """Should filter by trader_id."""
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                agent_id TEXT,
                ticker TEXT,
                pnl REAL,
                exit_time TEXT
            )
        """)
        conn.execute(
            "INSERT INTO trades (agent_id, ticker, pnl, exit_time) VALUES (?, ?, ?, ?)",
            ("trader-kairos", "AAPL", 100.0, "2026-07-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO trades (agent_id, ticker, pnl, exit_time) VALUES (?, ?, ?, ?)",
            ("trader-other", "AAPL", 50.0, "2026-07-01T00:00:00"),
        )
        conn.commit()
        conn.close()

        trades = get_closed_trades(db_path, "trader-kairos")
        assert len(trades) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# apply_graduated_sizing tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestApplyGraduatedSizing:
    def test_applies_sizing_with_trades(self, db_path):
        """Should compute and return graduated styling result."""
        # Create trades table with trades
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                agent_id TEXT,
                ticker TEXT,
                pnl REAL,
                exit_time TEXT
            )
        """)
        # 15 winning + 5 losing = 20 trades, 75% WR → should get PROVEN (15%)
        for i in range(15):
            conn.execute(
                "INSERT INTO trades (agent_id, ticker, pnl, exit_time) VALUES (?, ?, ?, ?)",
                ("trader-kairos", "AAPL", 100.0, "2026-07-01T00:00:00"),
            )
        for i in range(5):
            conn.execute(
                "INSERT INTO trades (agent_id, ticker, pnl, exit_time) VALUES (?, ?, ?, ?)",
                ("trader-kairos", "TSLA", -50.0, "2026-07-02T00:00:00"),
            )
        conn.commit()
        conn.close()

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            import json
            json.dump({}, f)
            params_path = f.name

        try:
            result = apply_graduated_sizing(db_path, "trader-kairos", params_path)

            assert result["n_trades"] == 20
            assert result["win_rate"] == pytest.approx(0.75, abs=0.01)
            assert result["max_position_pct"] == 15.0  # PROVEN_MAX

            # Verify written to params
            with open(params_path) as f:
                params = json.load(f)
            assert params["risk"]["max_position_pct"] == 15.0
        finally:
            if os.path.exists(params_path):
                os.unlink(params_path)

    def test_few_trades(self, db_path):
        """Fewer than MIN_SAMPLES trades should return BASE."""
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                agent_id TEXT,
                ticker TEXT,
                pnl REAL,
                exit_time TEXT
            )
        """)
        for i in range(10):
            conn.execute(
                "INSERT INTO trades (agent_id, ticker, pnl, exit_time) VALUES (?, ?, ?, ?)",
                ("trader-kairos", "AAPL", 100.0, "2026-07-01T00:00:00"),
            )
        conn.commit()
        conn.close()

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            import json
            json.dump({}, f)
            params_path = f.name

        try:
            result = apply_graduated_sizing(db_path, "trader-kairos", params_path)
            assert result["n_trades"] == 10
            assert result["max_position_pct"] == 6.0  # BASE
        finally:
            if os.path.exists(params_path):
                os.unlink(params_path)
