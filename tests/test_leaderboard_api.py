#!/usr/bin/env python3
"""
Regression tests for the leaderboard dashboard API.

Covers:
- _seconds_ago: ISO timestamp parsing (naive, tz-aware, None, malformed)
- _is_option_symbol: OCC option symbol detection
- _get_last_activity: journal timestamp normalization (handles journalT prefix)
- _get_benchmark_data: benchmark comparison computation
- _get_alpaca_portfolio: Alpaca direct connection + fallback
- _parse_decisions: decision query formatting
- API endpoint response schemas (via mocked DB)
"""

import sys
import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Mock psycopg2 before importing leaderboard_api ─────────────────────
# Prevent real DB connections in tests
mock_psycopg2 = MagicMock()
mock_connect = MagicMock()
mock_conn = MagicMock()
mock_cursor = MagicMock()
mock_psycopg2.connect.return_value = mock_conn
mock_psycopg2.extras = MagicMock()
mock_psycopg2.extras.RealDictCursor = MagicMock()
mock_psycopg2.extras.RealDictRow = dict

# Install mock before importing leaderboard_api
sys.modules["psycopg2"] = mock_psycopg2
sys.modules["psycopg2.extras"] = mock_psycopg2.extras

# Mock alpaca-py
mock_alpaca = MagicMock()
mock_trading_client = MagicMock()
mock_account = MagicMock()
mock_alpaca.trading = MagicMock()
mock_alpaca.trading.client = MagicMock()
mock_alpaca.trading.client.TradingClient = MagicMock(return_value=mock_trading_client)
sys.modules["alpaca"] = mock_alpaca
sys.modules["alpaca.trading"] = mock_alpaca.trading
sys.modules["alpaca.trading.client"] = mock_alpaca.trading.client

# Now import the module under test
from src import leaderboard_api as lb


# ═══════════════════════════════════════════════════════════════════════
#  _seconds_ago
# ═══════════════════════════════════════════════════════════════════════

class TestSecondsAgo:
    def test_valid_iso_naive(self):
        """Naive ISO timestamp (no timezone) works."""
        now = datetime.now()
        ts = (now - timedelta(seconds=65)).isoformat()
        result = lb._seconds_ago(ts)
        assert result is not None
        assert 60 <= result <= 70

    def test_valid_iso_tz_aware(self):
        """UTC-tagged ISO timestamp works."""
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(seconds=120)).isoformat()
        result = lb._seconds_ago(ts)
        assert result is not None
        assert 115 <= result <= 125

    def test_none_input(self):
        """None returns None, doesn't crash."""
        assert lb._seconds_ago(None) is None

    def test_empty_string(self):
        """Empty string returns None."""
        assert lb._seconds_ago("") is None

    def test_malformed_string(self):
        """Garbage string returns None."""
        assert lb._seconds_ago("not-a-timestamp") is None
        assert lb._seconds_ago("journalT12:29:00") is None

    def test_future_timestamp(self):
        """Future timestamp returns 0, not negative."""
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        result = lb._seconds_ago(future)
        assert result == 0


# ═══════════════════════════════════════════════════════════════════════
#  _is_option_symbol
# ═══════════════════════════════════════════════════════════════════════

class TestIsOptionSymbol:
    def test_valid_occ_call(self):
        """Valid OCC call option symbol."""
        assert lb._is_option_symbol("AAPL250117C00200000") is True

    def test_valid_occ_put(self):
        """Valid OCC put option symbol."""
        assert lb._is_option_symbol("SPY250117P00550000") is True

    def test_equity_symbol(self):
        """Regular equity ticker returns False."""
        assert lb._is_option_symbol("AAPL") is False
        assert lb._is_option_symbol("SPY") is False
        assert lb._is_option_symbol("QQQ") is False

    def test_empty_string(self):
        """Empty string returns False."""
        assert lb._is_option_symbol("") is False

    def test_none(self):
        """None returns False (coerced to empty string in regex)."""
        assert lb._is_option_symbol(None) is False

    def test_long_root(self):
        """6-char root with OCC format."""
        assert lb._is_option_symbol("BRKB250117C00050000") is True


# ═══════════════════════════════════════════════════════════════════════
#  _get_last_activity — journal timestamp normalization
# ═══════════════════════════════════════════════════════════════════════

class TestGetLastActivity:
    """Tests the journal timestamp normalization."""

    def test_valid_iso_timestamp(self, monkeypatch):
        """Valid ISO timestamp returned as-is."""
        def mock_db():
            class FakeRow:
                def __getitem__(self, key):
                    return "2026-07-15T14:30:00"
            class FakeConn:
                def execute(self, sql, params=None):
                    class FakeCur:
                        def fetchone(self):
                            return FakeRow()
                    return FakeCur()
                def close(self):
                    pass
            return MagicMock(__enter__=MagicMock(return_value=FakeConn()), __exit__=MagicMock())

        monkeypatch.setattr(lb, "_db", mock_db)
        result = lb._get_last_activity("kairos")
        assert result == "2026-07-15T14:30:00"

    def test_journalT_prefix(self, monkeypatch):
        """journalT12:29:00 → coerced to 2026-07-15T12:29:00."""
        def mock_db():
            class FakeRow:
                def __getitem__(self, key):
                    return "journalT12:29:00"
            class FakeConn:
                def execute(self, sql, params=None):
                    class FakeCur:
                        def fetchone(self):
                            return FakeRow()
                    return FakeCur()
                def close(self):
                    pass
            return MagicMock(__enter__=MagicMock(return_value=FakeConn()), __exit__=MagicMock())

        monkeypatch.setattr(lb, "_db", mock_db)
        result = lb._get_last_activity("kairos")
        # Should return a valid ISO-like date with the time part
        assert result is not None
        assert "T12:29:00" in result

    def test_garbage_timestamp(self, monkeypatch):
        """Completely garbage timestamp returns None."""
        def mock_db():
            class FakeRow:
                def __getitem__(self, key):
                    return "completely broken"
            class FakeConn:
                def execute(self, sql, params=None):
                    class FakeCur:
                        def fetchone(self):
                            return FakeRow()
                    return FakeCur()
                def close(self):
                    pass
            return MagicMock(__enter__=MagicMock(return_value=FakeConn()), __exit__=MagicMock())

        monkeypatch.setattr(lb, "_db", mock_db)
        result = lb._get_last_activity("kairos")
        assert result is None

    def test_none_timestamp(self, monkeypatch):
        """None from DB returns None."""
        def mock_db():
            class FakeRow:
                def __getitem__(self, key):
                    return None
            class FakeConn:
                def execute(self, sql, params=None):
                    class FakeCur:
                        def fetchone(self):
                            return FakeRow()
                    return FakeCur()
                def close(self):
                    pass
            return MagicMock(__enter__=MagicMock(return_value=FakeConn()), __exit__=MagicMock())

        monkeypatch.setattr(lb, "_db", mock_db)
        result = lb._get_last_activity("kairos")
        assert result is None

    def test_just_time_prefix(self, monkeypatch):
        """T14:30:00 format (no journal prefix) works."""
        def mock_db():
            class FakeRow:
                def __getitem__(self, key):
                    return "T14:30:00"
            class FakeConn:
                def execute(self, sql, params=None):
                    class FakeCur:
                        def fetchone(self):
                            return FakeRow()
                    return FakeCur()
                def close(self):
                    pass
            return MagicMock(__enter__=MagicMock(return_value=FakeConn()), __exit__=MagicMock())

        monkeypatch.setattr(lb, "_db", mock_db)
        result = lb._get_last_activity("kairos")
        assert result is not None
        assert "T14:30:00" in result


# ═══════════════════════════════════════════════════════════════════════
#  _get_benchmark_data — comparison logic
# ═══════════════════════════════════════════════════════════════════════

class TestGetBenchmarkData:
    """Tests the benchmark comparison computation."""

    def test_full_comparison(self, monkeypatch):
        """Happy path: SPY data + portfolio_value → correct excess returns."""
        class FakeConn:
            def __init__(self):
                self.call_count = 0
            def execute(self, sql, params=None):
                self.call_count += 1
                class FakeCur:
                    call_count = self.call_count
                    def fetchone(self):
                        if self.call_count == 1:  # SPY first
                            return {"close": 737.40, "timestamp": "2026-05-11T04:00:00+00"}
                        elif self.call_count == 2:  # SPY last
                            return {"close": 751.94}
                        elif self.call_count == 3:  # QQQ first
                            return {"close": 712.50, "timestamp": "2026-05-11T04:00:00+00"}
                        elif self.call_count == 4:  # QQQ last
                            return {"close": 719.71}
                        elif self.call_count == 5:  # kairos snapshot (first trader in loop)
                            return {"portfolio_value": 9300.56, "timestamp": "2026-07-15T17:01:41"}
                        elif self.call_count == 6:  # aldridge snapshot
                            return {"portfolio_value": 10225.49, "timestamp": "2026-07-15T17:01:41"}
                        elif self.call_count == 7:  # stonks snapshot
                            return {"portfolio_value": 10600.92, "timestamp": "2026-07-15T17:01:41"}
                        return None
                    def fetchall(self):
                        return []
                return FakeCur()
            def close(self):
                pass
            def cursor(self, *args, **kwargs):
                return MagicMock()

        class FakeConnCtx:
            def __enter__(self, *args):
                return FakeConn()
            def __exit__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(lb, "_db", lambda: FakeConnCtx())
        result = lb._get_benchmark_data()

        # SPY prices
        assert result["spy"]["price"] == 751.94
        assert result["qqq"]["price"] == 719.71

        # Stonks: pv=10600.92, return=6.01%, SPY return=(751.94/737.40-1)=1.97%
        s = result["comparisons"]["trader-stonks"]
        assert s["agent_return"] == pytest.approx(0.0601, abs=0.001)
        assert s["spy_return"] == pytest.approx(0.0197, abs=0.001)
        assert s["spy_excess"] == pytest.approx(0.0404, abs=0.001)
        assert s["spy_value"] == pytest.approx(10197.18, abs=0.01)
        assert s["agent_value"] == 10600.92
        assert s["period_start"] == "2026-05-11"
        assert s["period_end"] == "2026-07-15"

        # Kairos: pv=9300.56, return=-6.99%, SPY excess = -6.99% - 1.97% = -8.97%
        k = result["comparisons"]["trader-kairos"]
        assert k["agent_return"] == pytest.approx(-0.0699, abs=0.001)
        assert k["spy_excess"] == pytest.approx(-0.0897, abs=0.001)

        # Aldridge: pv=10225.49, return=2.25%, SPY excess = 2.25% - 1.97% = 0.28%
        a = result["comparisons"]["trader-aldridge"]
        assert a["agent_return"] == pytest.approx(0.0225, abs=0.001)
        assert a["spy_excess"] == pytest.approx(0.0028, abs=0.001)

    def test_no_spy_data(self, monkeypatch):
        """No SPY/QQQ bars → comparisons still return agent_return, but index returns are None."""
        class FakeConn:
            def execute(self, sql, params=None):
                class FakeCur:
                    def fetchone(self):
                        return None
                    def fetchall(self):
                        return []
                return FakeCur()
            def close(self):
                pass
            def cursor(self, *args, **kwargs):
                return MagicMock()

        class FakeConnCtx:
            def __enter__(self, *args):
                return FakeConn()
            def __exit__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(lb, "_db", lambda: FakeConnCtx())
        result = lb._get_benchmark_data()
        assert result["comparisons"] == {}  # No portfolio values either
        assert result["spy"] is None
        assert result["qqq"] is None


# ═══════════════════════════════════════════════════════════════════════
#  _get_alpaca_portfolio — Alpaca connection + fallback
# ═══════════════════════════════════════════════════════════════════════

class TestGetAlpacaPortfolio:
    """Tests the Alpaca direct connection + DB fallback."""

    def test_uses_direct_tradingclient(self, monkeypatch):
        """Uses alpaca-py TradingClient directly, not AlpacaExecutor."""
        # Mock the TradingClient
        mock_client = MagicMock()
        mock_acct = MagicMock()
        type(mock_acct).cash = PropertyMock(return_value="7545.56")
        type(mock_acct).equity = PropertyMock(return_value="10664.17")
        type(mock_acct).buying_power = PropertyMock(return_value="20000.00")
        mock_client.get_account.return_value = mock_acct

        # Mock positions
        mock_pos = MagicMock()
        mock_pos.symbol = "AAPL"
        mock_pos.qty = "10"
        mock_pos.avg_entry_price = "150.00"
        mock_pos.current_price = "155.00"
        mock_pos.unrealized_pl = "50.00"
        mock_pos.unrealized_plpc = "0.033"
        mock_pos.market_value = "1550.00"
        mock_client.get_all_positions.return_value = [mock_pos]

        mock_trading_client_cls = MagicMock(return_value=mock_client)
        monkeypatch.setattr("alpaca.trading.client.TradingClient", mock_trading_client_cls)

        # Mock DB for exit conditions merge
        class FakeRow:
            def __getitem__(self, key):
                if key == "exit_condition": return "trailing_stop"
                if key == "holding_horizon_days": return 5
                if key == "stop_loss": return "145.00"
                return None
            def __bool__(self): return True

        class FakeConn:
            def execute(self, sql, params=None):
                class FakeCur:
                    def fetchone(self):
                        return FakeRow()
                return FakeCur()
            def close(self):
                pass
            def cursor(self, *args, **kwargs):
                return MagicMock()

        class FakeConnCtx:
            def __enter__(self, *args):
                return FakeConn()
            def __exit__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(lb, "_db", lambda: FakeConnCtx())

        # Set env vars
        monkeypatch.setenv("ALPACA_STONKS_KEY", "test-key")
        monkeypatch.setenv("ALPACA_STONKS_SECRET", "test-secret")

        result = lb._get_alpaca_portfolio("stonks")

        assert result is not None
        assert result["_source"] == "alpaca_live"
        assert result["cash"] == 7545.56
        assert result["portfolio_value"] == 10664.17
        assert result["buying_power"] == 20000.00
        assert len(result["positions"]) == 1
        assert result["positions"][0]["ticker"] == "AAPL"
        assert result["positions"][0]["exit_condition"] == "trailing_stop"

    def test_missing_credentials(self, monkeypatch):
        """No credentials returns None (no crash)."""
        monkeypatch.delenv("ALPACA_KAIROS_KEY", raising=False)
        monkeypatch.delenv("ALPACA_KAIROS_SECRET", raising=False)
        monkeypatch.delenv("KAIROS_API_KEY", raising=False)
        monkeypatch.delenv("KAIROS_SECRET_KEY", raising=False)

        # Mock DB to return None (no snapshot fallback)
        class FakeRow:
            def __getitem__(self, key): return None
            def __bool__(self): return False

        class FakeConn:
            def execute(self, sql, params=None):
                class FakeCur:
                    def fetchone(self): return None
                return FakeCur()
            def close(self): pass
            def cursor(self, *args, **kwargs): return MagicMock()

        class FakeConnCtx:
            def __enter__(self, *args): return FakeConn()
            def __exit__(self, *args, **kwargs): pass

        monkeypatch.setattr(lb, "_get_portfolio_from_db", lambda c: None)
        result = lb._get_alpaca_portfolio("kairos")
        assert result is None

    def test_fallback_to_db_snapshot(self, monkeypatch):
        """Alpaca fails → falls back to stale snapshot."""
        # Make TradingClient raise
        def raise_error(*args, **kwargs):
            raise Exception("Alpaca unavailable")
        monkeypatch.setattr("alpaca.trading.client.TradingClient", raise_error)

        # Mock DB snapshot
        monkeypatch.setattr(lb, "_get_portfolio_from_db", lambda c: {
            "cash": 5000.0,
            "portfolio_value": 10500.0,
            "unrealized_pl": 100.0,
            "daily_pnl": 50.0,
            "open_positions_count": 5,
            "snapshot_ts": "2026-07-15T12:00:00",
            "source": "db_snapshot",
        })

        result = lb._get_alpaca_portfolio("stonks")
        assert result is not None
        assert result["_source"] == "stale_snapshot"
        assert result["cash"] == 5000.0
        assert result["portfolio_value"] == 10500.0
        assert result["positions"] == []  # empty because Alpaca failed


# ═══════════════════════════════════════════════════════════════════════
#  _parse_decisions
# ═══════════════════════════════════════════════════════════════════════

class TestParseDecisions:
    def test_returns_events_with_correct_schema(self, monkeypatch):
        """Decision rows from trading.decisions map to correct event schema."""
        class FakeRow:
            data = {
                "trader_id": "trader-kairos",
                "timestamp": "2026-07-15T14:30:00",
                "action": "BUY",
                "ticker": "AAPL",
                "confidence": 0.75,
                "thesis": "Strong momentum",
            }
            def __getitem__(self, key):
                return self.data.get(key)
            def __bool__(self): return True

        class FakeCur:
            def fetchall(self):
                return [FakeRow()]

        class FakeConn:
            def execute(self, sql, params=None):
                return FakeCur()
            def close(self):
                pass
            def cursor(self, *args, **kwargs):
                return MagicMock()

        class FakeConnCtx:
            def __enter__(self, *args):
                return FakeConn()
            def __exit__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(lb, "_db", lambda: FakeConnCtx())
        events = lb._parse_decisions("kairos")

        assert len(events) == 1
        e = events[0]
        assert e["timestamp"] == "2026-07-15T14:30:00"
        assert e["trader"] == "trader-kairos"
        assert e["decision"]["action"] == "BUY"
        assert e["decision"]["ticker"] == "AAPL"
        assert e["decision"]["confidence"] == 0.75
        assert e["decision"]["thesis"] == "Strong momentum"

    def test_empty_db_returns_empty_list(self, monkeypatch):
        """No decisions → empty list."""
        class FakeCur:
            def fetchall(self):
                return []

        class FakeConn:
            def execute(self, sql, params=None):
                return FakeCur()
            def close(self):
                pass
            def cursor(self, *args, **kwargs):
                return MagicMock()

        class FakeConnCtx:
            def __enter__(self, *args):
                return FakeConn()
            def __exit__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(lb, "_db", lambda: FakeConnCtx())
        events = lb._parse_decisions("kairos")
        assert events == []


# ═══════════════════════════════════════════════════════════════════════
#  API endpoint schemas (via mocked app)
# ═══════════════════════════════════════════════════════════════════════

class TestApiEndpoints:
    """Test that API endpoints return correct response schemas."""

    def test_index_serves_html(self, monkeypatch):
        """Root / serves the dashboard HTML."""
        monkeypatch.setattr(lb, "UI_DIR", Path(__file__).parent.parent / "src" / "leaderboard_ui")
        with lb.app.test_client() as c:
            r = c.get("/")
            assert r.status_code == 200
            assert b"PAPER TRADING COMMAND CENTER" in r.data

    def test_heartbeat_endpoint(self, monkeypatch):
        """/api/heartbeat returns JSON with timestamp and ago_s."""
        # Write a mock heartbeat state file
        state_dir = Path("/tmp/test_leaderboard_state")
        state_dir.mkdir(exist_ok=True)
        (state_dir / "heartbeat-state.json").write_text(
            json.dumps({"last_kairos": "2026-07-15T14:30:00"})
        )

        monkeypatch.setattr(lb, "STATE", state_dir)
        with lb.app.test_client() as c:
            r = c.get("/api/heartbeat")
            assert r.status_code == 200
            data = r.get_json()
            assert "last_kairos" in data
            assert "timestamp" in data["last_kairos"]
            assert "ago_s" in data["last_kairos"]

    def test_tick_endpoint(self, monkeypatch):
        """POST /api/tick/<trader> returns 200 and writes heartbeat state."""
        state_dir = Path("/tmp/test_leaderboard_state")
        state_dir.mkdir(exist_ok=True)
        # Remove existing state
        (state_dir / "heartbeat-state.json").write_text("{}")
        monkeypatch.setattr(lb, "STATE", state_dir)

        with lb.app.test_client() as c:
            r = c.post("/api/tick/stonks")
            assert r.status_code == 200
            data = r.get_json()
            assert data["trader"] == "stonks"
            assert data["status"] == "ok"
            assert data["ago_s"] == 0

            # Verify state file was written
            hb = json.loads((state_dir / "heartbeat-state.json").read_text())
            assert "last_stonks" in hb
            assert "ts_stonks" in hb

    def test_tick_unknown_trader(self, monkeypatch):
        """POST /api/tick/<unknown> returns 404."""
        with lb.app.test_client() as c:
            r = c.post("/api/tick/unknown")
            assert r.status_code == 404

    def test_tick_equity_cash_params(self, monkeypatch):
        """Tick with equity/cash params stores them in heartbeat state."""
        state_dir = Path("/tmp/test_leaderboard_state")
        state_dir.mkdir(exist_ok=True)
        (state_dir / "heartbeat-state.json").write_text("{}")
        monkeypatch.setattr(lb, "STATE", state_dir)

        with lb.app.test_client() as c:
            r = c.post("/api/tick/kairos?equity=10500.50&cash=8000")
            assert r.status_code == 200
            hb = json.loads((state_dir / "heartbeat-state.json").read_text())
            assert hb["equity_kairos"] == 10500.50
            assert hb["cash_kairos"] == 8000.0


# ═══════════════════════════════════════════════════════════════════════
#  _normalize_timestamp
# ═══════════════════════════════════════════════════════════════════════

class TestNormalizeTimestamp:
    def test_valid_iso(self):
        """Valid ISO timestamp returned as parsed ISO format."""
        result = lb._normalize_timestamp("2026-07-15T14:30:00")
        assert result is not None
        assert "2026-07-15" in result
        assert "14:30:00" in result

    def test_journalT_prefix(self):
        """journalT12:29:00 → normalized to 2026-07-15T12:29:00."""
        result = lb._normalize_timestamp("journalT12:29:00")
        assert result is not None
        assert result == "2026-07-15T12:29:00"

    def test_just_T_time(self):
        """T14:30:00 format → normalized."""
        result = lb._normalize_timestamp("T14:30:00")
        assert result is not None
        assert result == "2026-07-15T14:30:00"

    def test_none_returns_none(self):
        """None input returns None."""
        assert lb._normalize_timestamp(None) is None

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        assert lb._normalize_timestamp("") is None

    def test_garbage_returns_none(self):
        """Garbage input returns None."""
        result = lb._normalize_timestamp("garbage")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  API /api/journal endpoint — timestamp normalization
# ═══════════════════════════════════════════════════════════════════════

class TestApiJournalEndpoint:
    """Test that /api/journal normalizes timestamps correctly."""

    def test_journal_timestamps_normalized(self, monkeypatch):
        """journalT timestamps in DB are normalized in the response."""
        from datetime import datetime

        class FakeRow(dict):
            def __getitem__(self, key):
                return self.get(key)

        rows = [
            FakeRow({"agent_id": "trader-kairos", "timestamp": "journalT16:23:00", "mood": "", "entry": "Test entry 1", "confidence": None}),
            FakeRow({"agent_id": "trader-stonks", "timestamp": "journalT12:29:00", "mood": "", "entry": "Test entry 2", "confidence": None}),
            FakeRow({"agent_id": "trader-aldridge", "timestamp": "2026-07-15T17:20:00", "mood": "", "entry": "Test entry 3", "confidence": None}),
        ]

        class FakeCur:
            def fetchall(self):
                return rows

        class FakeConn:
            def execute(self, sql, params=None):
                return FakeCur()
            def close(self):
                pass
            def cursor(self, *args, **kwargs):
                return MagicMock()

        class FakeConnCtx:
            def __enter__(self, *args):
                return FakeConn()
            def __exit__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(lb, "_db", lambda: FakeConnCtx())

        with lb.app.test_client() as c:
            r = c.get("/api/journal?limit=10")
            assert r.status_code == 200
            data = r.get_json()
            entries = data.get("entries", [])

            # Should have at least 3 entries
            assert len(entries) >= 3

            # journalT timestamps should be normalized
            for e in entries:
                ts = e.get("timestamp", "")
                # No raw journalT prefix should leak through
                assert "journalT" not in str(ts).lower(), f"Raw journalT leaked: {ts}"
                # Should contain a time portion
                assert ":" in ts, f"Timestamp has no time portion: {ts}"

    def test_journal_returns_entries(self, monkeypatch):
        """Basic journal endpoint returns entries array."""
        class FakeRow(dict):
            def __getitem__(self, key):
                return self.get(key)

        class FakeCur:
            def fetchall(self):
                return [
                    FakeRow({"agent_id": "trader-kairos", "timestamp": "2026-07-15T14:30:00", "mood": "happy", "entry": "Test", "confidence": 0.8}),
                ]

        class FakeConn:
            def execute(self, sql, params=None):
                return FakeCur()
            def close(self):
                pass
            def cursor(self, *args, **kwargs):
                return MagicMock()

        class FakeConnCtx:
            def __enter__(self, *args):
                return FakeConn()
            def __exit__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(lb, "_db", lambda: FakeConnCtx())

        with lb.app.test_client() as c:
            r = c.get("/api/journal?limit=5")
            assert r.status_code == 200
            data = r.get_json()
            assert "entries" in data
            assert len(data["entries"]) == 1
            assert data["entries"][0]["agent_id"] == "trader-kairos"


# ═══════════════════════════════════════════════════════════════════════
#  API /api/decisions endpoint — returns data from trading.decisions
# ═══════════════════════════════════════════════════════════════════════

class TestApiDecisionsEndpoint:
    """Test that /api/decisions returns properly structured data."""

    def test_decisions_returns_events(self, monkeypatch):
        """Decisions endpoint returns events with trader/decision keys."""
        class FakeRow(dict):
            def __getitem__(self, key):
                return self.get(key)

        class FakeCur:
            def fetchall(self):
                return [
                    FakeRow({"trader_id": "trader-kairos", "timestamp": "2026-07-15T14:30:00", "action": "BUY", "ticker": "AAPL", "confidence": 0.75, "thesis": "Strong momentum"}),
                ]

        class FakeConn:
            def execute(self, sql, params=None):
                return FakeCur()
            def close(self):
                pass
            def cursor(self, *args, **kwargs):
                return MagicMock()

        class FakeConnCtx:
            def __enter__(self, *args):
                return FakeConn()
            def __exit__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(lb, "_db", lambda: FakeConnCtx())

        with lb.app.test_client() as c:
            r = c.get("/api/decisions?limit=10")
            assert r.status_code == 200
            events = r.get_json()
            assert isinstance(events, list)
            assert len(events) >= 1
            e = events[0]
            assert "trader" in e
            assert "decision" in e
            assert e["decision"]["action"] == "BUY"
            assert e["decision"]["ticker"] == "AAPL"
            assert e["decision"]["confidence"] == 0.75


# ═══════════════════════════════════════════════════════════════════════
#  API /api/traders — benchmark_comparison in response
# ═══════════════════════════════════════════════════════════════════════

class TestApiTradersBenchmarks:
    """Test that /api/traders includes benchmark_comparison per trader."""

    def test_traders_include_benchmark(self, monkeypatch):
        """Each trader card has benchmark_comparison with agent/spy/qoq values."""
        # Mock _get_benchmark_data to return known values
        def mock_get_benchmark_data():
            return {
                "spy": {"price": 751.94},
                "qqq": {"price": 719.71},
                "comparisons": {
                    "trader-stonks": {
                        "agent_return": 0.0601,
                        "spy_return": 0.0197,
                        "qqq_return": 0.0101,
                        "spy_excess": 0.0404,
                        "qqq_excess": 0.05,
                        "agent_value": 10600.92,
                        "spy_value": 10197.18,
                        "period_start": "2026-05-11",
                        "period_end": "2026-07-15",
                    },
                    "trader-aldridge": {
                        "agent_return": 0.0225,
                        "spy_return": 0.0197,
                        "spy_excess": 0.0028,
                        "agent_value": 10225.49,
                        "spy_value": 10197.18,
                    },
                    "trader-kairos": {
                        "agent_return": -0.0699,
                        "spy_return": 0.0197,
                        "spy_excess": -0.0897,
                        "agent_value": 9300.56,
                        "spy_value": 10197.18,
                    },
                },
            }

        monkeypatch.setattr(lb, "_get_benchmark_data", mock_get_benchmark_data)
        monkeypatch.setattr(lb, "_get_agent_benchmark", lambda agent_id=None: mock_get_benchmark_data()["comparisons"].get(agent_id))

        # Mock other heavy deps to return simple values
        monkeypatch.setattr(lb, "_get_alpaca_portfolio", lambda c: None)
        monkeypatch.setattr(lb, "_get_profile_from_db", lambda c: {})
        monkeypatch.setattr(lb, "_get_trade_stats", lambda c: {"wins": 0, "losses": 0, "total_trades": 0, "win_rate": 0})
        monkeypatch.setattr(lb, "_get_last_activity", lambda c: "2026-07-15T14:30:00")
        monkeypatch.setattr(lb, "_get_recent_thought", lambda c: None)
        monkeypatch.setattr(lb, "_get_agent_score", lambda c: None)
        monkeypatch.setattr(lb, "_get_paused_status", lambda c: None)
        monkeypatch.setattr(lb, "_is_option_symbol", lambda s: False)
        monkeypatch.setattr(lb, "_load_json", lambda p: {})

        with lb.app.test_client() as c:
            r = c.get("/api/traders")
            assert r.status_code == 200
            data = r.get_json()
            assert "traders" in data
            assert "benchmarks" in data
            bm = data["benchmarks"]
            assert "comparisons" in bm
            assert len(bm["comparisons"]) >= 2

            for trader in data["traders"]:
                aid = f"trader-{trader['id']}"
                bc = trader.get("benchmark_comparison")
                assert bc is not None, f"{trader['id']} missing benchmark_comparison"
                assert "agent_return" in bc
                assert "spy_excess" in bc or "spy_return" in bc
                if aid == "trader-stonks":
                    assert bc["agent_return"] == 0.0601
                    assert bc["spy_excess"] == 0.0404
                    assert bc["agent_value"] == 10600.92
                    assert bc["spy_value"] == 10197.18