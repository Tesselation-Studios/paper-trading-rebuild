#!/usr/bin/env python3
"""
Shared pytest fixtures for paper-trading-teams tests.

All tests import from conftest.py by default — no manual import needed.
"""

import sys
import os
import contextlib
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root is on sys.path for all tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Mock heavy deps that aren't installed in this venv ──────────────────────
# pandas_ta is used by fetch.py but not available everywhere.
# pandas is NOT mocked — it's needed by synthetic_data, data_feeder, etc.
_MOCK_PANDAS_TA = MagicMock()
sys.modules.setdefault("pandas_ta", _MOCK_PANDAS_TA)


def pytest_configure(config):
    """Register custom markers to suppress pytest warnings."""
    config.addinivalue_line("markers", "integration: env-dependent tests that need network/external services.")
    config.addinivalue_line("markers", "smoke: quick smoke tests for data-bus endpoints.")


@pytest.fixture(autouse=True)
def mock_openclaw_env():
    """Suppress real env loading in all tests to avoid credential leakage."""
    with patch.dict(os.environ, {
        "PAPER": "true",
        "ALPACA_KAIROS_KEY": "test-kairos-key",
        "ALPACA_KAIROS_SECRET": "test-kairos-secret",
        "ALPACA_ALDRIDGE_KEY": "test-aldridge-key",
        "ALPACA_ALDRIDGE_SECRET": "test-aldridge-secret",
        "ALPACA_STONKS_KEY": "test-stonks-key",
        "ALPACA_STONKS_SECRET": "test-stonks-secret",
        "ALPHA_VANTAGE_API_KEY": "test-av-key",
        "FINNHUB_API_KEY": "test-finnhub-key",
        "RISK_MANAGER_LIVE": "0",
    }, clear=False):
        yield


@pytest.fixture(autouse=True)
def suppress_load_dotenv():
    """Prevent dotenv from loading real .env files during tests."""
    with patch("dotenv.load_dotenv", return_value=True):
        yield


# ── Spec-driven rebuild fixtures ────────────────────────────────────────────

@pytest.fixture
def temp_db():
    """In-memory SQLite with fresh schema."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE trades (id INTEGER PRIMARY KEY, agent_id TEXT, ticker TEXT, action TEXT, qty REAL, price REAL, entry_time TEXT, exit_time TEXT, pnl REAL, decision_id INTEGER);
        CREATE TABLE positions (id INTEGER PRIMARY KEY, agent_id TEXT, ticker TEXT, qty REAL, entry_price REAL, UNIQUE(agent_id, ticker));
        CREATE TABLE journal (id INTEGER PRIMARY KEY, agent_id TEXT, entry TEXT, created_at TEXT);
        CREATE TABLE agent_params (id INTEGER PRIMARY KEY, agent_id TEXT, key TEXT, value TEXT);
        CREATE TABLE agent_profile (agent_id TEXT PRIMARY KEY, agent_name TEXT);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY, agent_id TEXT, timestamp TEXT, action TEXT, ticker TEXT, quantity REAL, stop_loss REAL, confidence REAL, thesis TEXT, mood TEXT, source TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER, agent_id TEXT, timestamp TEXT, order_id TEXT UNIQUE, action TEXT, ticker TEXT, quantity REAL, stop_loss REAL, status TEXT DEFAULT 'submitted', filled_price REAL, error_reason TEXT, fill_price REAL);
        CREATE TABLE risk_state (agent_id TEXT PRIMARY KEY, peak_portfolio_value REAL, daily_start_value REAL, daily_start_date TEXT, paused INTEGER DEFAULT 0, pause_reason TEXT, pause_timestamp TEXT, updated_at TEXT);
    """)
    yield conn
    conn.close()


@pytest.fixture
def sample_quotes():
    """Realistic quote data matching data bus /quotes format."""
    return {
        "AAPL": {"price": 193.45, "rsi": 58.2, "macd": "bullish", "change_pct": 0.45},
        "TSLA": {"price": 245.10, "rsi": 42.1, "macd": "bearish", "change_pct": -1.20},
    }


@pytest.fixture
def sample_positions():
    return [
        {"ticker": "AAPL", "qty": 2, "entry": 281.12, "current": 288.50, "unrealized_plpc": 2.63},
    ]


@pytest.fixture
def sample_context_blob():
    return {
        "agent": {"id": "trader-kairos", "cash": 9505.37, "portfolio_value": 10587.50, "pnl_pct": 5.88},
        "positions": [{"ticker": "AAPL", "qty": 2, "entry": 281.12, "current": 288.50}],
        "signals": {"social_sentiment": {"AAPL": 0.72}, "fear_greed": 45, "regime": "TRENDING"},
        "market": {"SPY_change_pct": 0.34, "VIX": 18.2},
        "recent_decisions": [],
    }
