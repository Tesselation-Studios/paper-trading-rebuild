"""
Tests for trade_context.py — trade context injector for LLM agents.

Tests cover:
- build_trade_context() with mocked data sources
- All format_* functions with edge cases (empty, missing fields, etc.)
- CLI entry point (--trader, --json, --no-signals)
- Data gathering functions (get_portfolio, get_recent_trades, etc.)
"""

import json
import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from src.trade_context import (
    build_trade_context,
    format_decisions_text,
    format_market_text,
    format_performance_text,
    format_portfolio_text,
    format_trades_text,
    get_market_data,
    get_performance_stats,
    get_portfolio,
    get_recent_decisions,
    get_recent_trades,
    main,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_portfolio():
    return {
        "cash": 2500.0,
        "portfolio_value": 10250.0,
        "buying_power": 5000.0,
        "source": "alpaca_live",
        "positions": [
            {
                "ticker": "AAPL",
                "qty": 10,
                "avg_entry": 185.0,
                "current_price": 190.5,
                "unrealized_pl": 55.0,
                "unrealized_plpc": 2.97,
                "market_value": 1905.0,
            },
            {
                "ticker": "NVDA",
                "qty": 5,
                "avg_entry": 800.0,
                "current_price": 785.0,
                "unrealized_pl": -75.0,
                "unrealized_plpc": -1.88,
                "market_value": 3925.0,
            },
        ],
    }


@pytest.fixture
def sample_trades():
    return [
        {
            "ticker": "MSFT",
            "action": "BUY",
            "quantity": 10,
            "entry_price": 420.0,
            "exit_price": 445.0,
            "pnl": 250.0,
            "entry_time": "2026-07-15 10:30:00",
            "exit_time": "2026-07-15 15:45:00",
            "status": "closed",
        },
        {
            "ticker": "META",
            "action": "SELL",
            "quantity": 5,
            "entry_price": 500.0,
            "exit_price": 480.0,
            "pnl": -100.0,
            "entry_time": "2026-07-14 11:00:00",
            "exit_time": "2026-07-14 14:00:00",
            "status": "closed",
        },
    ]


@pytest.fixture
def sample_decisions():
    return [
        {
            "trader_id": "trader-kairos",
            "ticker": "AAPL",
            "decision": "BUY",
            "conviction": 0.75,
            "rationale": "Strong momentum breakout above 190 with volume confirmation",
            "timestamp": "2026-07-16 09:45:00",
        },
        {
            "trader_id": "trader-kairos",
            "ticker": "NVDA",
            "decision": "HOLD",
            "conviction": 0.30,
            "rationale": "RSI neutral, waiting for pullback to 780 support",
            "timestamp": "2026-07-16 09:30:00",
        },
    ]


@pytest.fixture
def sample_market():
    return {
        "quotes": {
            "AAPL": {"close": 190.5, "prev_close": 188.0, "rsi": 62.0, "volume": 50000000},
            "NVDA": {"close": 785.0, "prev_close": 792.0, "rsi": 45.0, "volume": 35000000},
        },
        "signals": {
            "AAPL": {"signal": "STRONG_BUY", "confidence": 0.75, "regime": "bullish"},
            "NVDA": {"signal": "NEUTRAL", "confidence": 0.30, "regime": "choppy"},
        },
        "fear_greed": {"value": 55, "classification": "Neutral"},
        "regime": {"regime": "bullish", "confidence": 0.65},
    }


# ── format_portfolio_text ─────────────────────────────────────────────────────


class TestFormatPortfolioText:
    def test_full_portfolio(self, sample_portfolio):
        text = format_portfolio_text(sample_portfolio)
        assert "Portfolio Value: $10,250.00" in text
        assert "Cash: $2,500.00" in text
        assert "Buying Power: $5,000.00" in text
        assert "Source: alpaca_live" in text
        assert "Open Positions: 2" in text
        assert "AAPL" in text
        assert "NVDA" in text
        assert "$+55.00" in text
        assert "$-75.00" in text

    def test_empty_portfolio(self):
        text = format_portfolio_text({
            "cash": 10000.0,
            "portfolio_value": 10000.0,
            "buying_power": None,
            "source": "pg_snapshot",
            "positions": [],
        })
        assert "Portfolio Value: $10,000.00" in text
        assert "Cash: $10,000.00" in text
        assert "Buying Power" not in text  # None should be omitted
        assert "Open Positions: 0" in text

    def test_portfolio_no_positions_key(self):
        text = format_portfolio_text({
            "cash": 5000.0,
            "portfolio_value": 5000.0,
            "source": "unavailable",
            "positions": [],
        })
        assert "Portfolio Value: $5,000.00" in text
        assert "Open Positions: 0" in text

    def test_portfolio_pct_from_starting(self):
        text = format_portfolio_text({
            "cash": 2000.0,
            "portfolio_value": 11000.0,
            "source": "alpaca_live",
            "positions": [],
        })
        assert "(+10.00% from $10,000)" in text

    def test_portfolio_negative_pct(self):
        text = format_portfolio_text({
            "cash": 1000.0,
            "portfolio_value": 9500.0,
            "source": "alpaca_live",
            "positions": [],
        })
        assert "(-5.00% from $10,000)" in text

    def test_pg_snapshot_source(self):
        text = format_portfolio_text({
            "cash": 3000.0,
            "portfolio_value": 9000.0,
            "source": "pg_snapshot",
            "positions": [],
        })
        assert "Source: pg_snapshot" in text
        assert "Buying Power" not in text


# ── format_trades_text ────────────────────────────────────────────────────────


class TestFormatTradesText:
    def test_with_trades(self, sample_trades):
        text = format_trades_text(sample_trades)
        assert "Recent Trades:" in text
        assert "MSFT" in text
        assert "META" in text
        assert "$+250.00" in text
        assert "$-100.00" in text
        assert "BUY" in text
        assert "SELL" in text

    def test_empty_trades(self):
        text = format_trades_text([])
        assert text == "No recent trades."

    def test_trades_with_missing_fields(self):
        trades = [
            {"ticker": "GOOG", "action": "BUY", "quantity": 3},
        ]
        text = format_trades_text(trades)
        assert "GOOG" in text
        assert "BUY" in text
        assert "—" in text  # missing prices rendered as em dash

    def test_trades_with_none_pnl(self):
        trades = [
            {
                "ticker": "AMZN",
                "action": "BUY",
                "quantity": 2,
                "entry_price": "200.00",
                "exit_price": None,
                "pnl": None,
                "entry_time": None,
            },
        ]
        text = format_trades_text(trades)
        assert "AMZN" in text
        # Should not crash with None values


# ── format_decisions_text ─────────────────────────────────────────────────────


class TestFormatDecisionsText:
    def test_with_decisions(self, sample_decisions):
        text = format_decisions_text(sample_decisions)
        assert "Recent Decisions:" in text
        assert "AAPL" in text
        assert "BUY" in text
        assert "75.0%" in text
        assert "momentum breakout" in text

    def test_empty_decisions(self):
        text = format_decisions_text([])
        assert text == "No recent decisions."

    def test_decision_no_confidence(self):
        decisions = [
            {
                "trader_id": "trader-kairos",
                "ticker": "TSLA",
                "decision": "SELL",
                "conviction": None,
                "rationale": "Breaking support",
                "timestamp": "2026-07-16 10:00:00",
            },
        ]
        text = format_decisions_text(decisions)
        assert "(conf:" not in text  # No confidence label when None
        assert "Breaking support" in text

    def test_truncated_rationale(self):
        long_rationale = "A" * 200
        decisions = [
            {
                "ticker": "XYZ",
                "decision": "HOLD",
                "conviction": 0.5,
                "rationale": long_rationale,
                "timestamp": "2026-07-16",
            },
        ]
        text = format_decisions_text(decisions)
        assert len(text.split(": ")[-1].strip()) <= 100


# ── format_market_text ─────────────────────────────────────────────────────────


class TestFormatMarketText:
    def test_full_market(self, sample_market):
        text = format_market_text(sample_market)
        assert "Fear & Greed Index: 55 (Neutral)" in text
        assert "Market Regime: bullish (confidence: 0.65)" in text
        assert "AAPL" in text
        assert "NVDA" in text
        assert "STRONG_BUY" in text
        assert "NEUTRAL" in text

    def test_empty_market(self):
        text = format_market_text({})
        assert text == ""

    def test_market_quotes_only(self):
        market = {
            "quotes": {
                "SPY": {"close": 550.0, "prev_close": 545.0, "rsi": 55.0},
            },
        }
        text = format_market_text(market)
        assert "SPY" in text
        assert "$550.00" in text
        assert "Fear & Greed" not in text
        assert "Market Regime" not in text

    def test_market_signals_no_quotes(self):
        market = {
            "signals": {
                "IBM": {"signal": "WEAK_SELL", "confidence": 0.20, "regime": "bearish"},
            },
        }
        text = format_market_text(market)
        assert "ML Signals" in text
        assert "WEAK_SELL" in text
        assert "IBM" in text

    def test_rsi_formatted_as_int(self):
        market = {
            "quotes": {
                "TEST": {"close": 100.0, "prev_close": 99.0, "rsi": 72.3},
            },
        }
        text = format_market_text(market)
        assert "72" in text  # RSI formatted as integer

    def test_rsi_missing(self):
        market = {
            "quotes": {
                "TEST": {"close": 100.0, "prev_close": 99.0},
            },
        }
        text = format_market_text(market)
        assert "—" in text  # Missing RSI shows em dash

    def test_skip_quote_without_close(self):
        market = {
            "quotes": {
                "BAD": {"volume": 1000},
                "GOOD": {"close": 50.0, "prev_close": 49.0},
            },
        }
        text = format_market_text(market)
        assert "GOOD" in text
        assert "BAD" not in text

    def test_fear_greed_only(self):
        market = {"fear_greed": {"value": 25, "classification": "Fear"}}
        text = format_market_text(market)
        assert "Fear & Greed Index: 25 (Fear)" in text

    def test_regime_with_label_key(self):
        market = {"regime": {"label": "choppy", "confidence": 0.3}}
        text = format_market_text(market)
        assert "Market Regime: choppy" in text


# ── format_performance_text ────────────────────────────────────────────────────


class TestFormatPerformanceText:
    def test_with_stats(self):
        text = format_performance_text({
            "total_trades": 20,
            "wins": 12,
            "losses": 8,
            "win_rate": 0.60,
            "realized_pnl": 1250.50,
        })
        assert "12W / 8L" in text
        assert "60.0% win rate" in text
        assert "$+1,250.50" in text
        assert "Total Closed Trades: 20" in text

    def test_empty_stats(self):
        text = format_performance_text({"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "realized_pnl": 0})
        assert text == "No trade history yet."

    def test_negative_pnl(self):
        text = format_performance_text({
            "total_trades": 10,
            "wins": 3,
            "losses": 7,
            "win_rate": 0.30,
            "realized_pnl": -500.00,
        })
        assert "$-500.00" in text


# ── get_portfolio (with mocked deps) ───────────────────────────────────────────


class TestGetPortfolio:
    @patch("src.trade_context._get_alpaca_client", return_value=None)
    @patch("src.trade_context._get_db")
    def test_pg_fallback(self, mock_get_db, mock_alpaca):
        """When Alpaca unavailable, falls back to Postgres."""
        mock_conn = MagicMock()
        mock_cur = mock_conn.cursor.return_value
        mock_cur.fetchone.return_value = {
            "cash": 3000.0,
            "portfolio_value": 10500.0,
            "daily_pnl": 150.0,
            "timestamp": "2026-07-16 08:00:00",
        }
        mock_cur.fetchall.return_value = []
        mock_get_db.return_value = mock_conn

        result = get_portfolio("trader-kairos")
        assert result["source"] == "pg_snapshot"
        assert result["cash"] == 3000.0
        assert result["portfolio_value"] == 10500.0
        assert result["daily_pnl"] == 150.0
        assert result["positions"] == []

    @patch("src.trade_context._get_alpaca_client", return_value=None)
    @patch("src.trade_context._get_db")
    def test_pg_fallback_with_positions(self, mock_get_db, mock_alpaca):
        """Postgres fallback with open positions."""
        mock_conn = MagicMock()
        mock_cur = mock_conn.cursor.return_value
        mock_cur.fetchone.return_value = {
            "cash": 2000.0,
            "portfolio_value": 11000.0,
            "daily_pnl": 200.0,
            "timestamp": "2026-07-16 08:00:00",
        }
        mock_cur.fetchall.return_value = [
            {
                "ticker": "AAPL",
                "quantity": 10,
                "avg_entry_price": 185.0,
                "current_price": 190.0,
                "market_value": 1900.0,
                "unrealized_pl": 50.0,
                "stop_loss": 180.0,
                "exit_condition": "take-profit at 200",
            },
        ]
        mock_get_db.return_value = mock_conn

        result = get_portfolio("trader-aldridge")
        assert len(result["positions"]) == 1
        assert result["positions"][0]["ticker"] == "AAPL"
        assert result["positions"][0]["stop_loss"] == 180.0
        assert result["positions"][0]["unrealized_plpc"] == pytest.approx(2.70, abs=0.1)

    @patch("src.trade_context._get_alpaca_client", return_value=None)
    @patch("src.trade_context._get_db")
    def test_pg_fallback_with_none_prices(self, mock_get_db, mock_alpaca):
        """Handles None current_price from Postgres."""
        mock_conn = MagicMock()
        mock_cur = mock_conn.cursor.return_value
        mock_cur.fetchone.return_value = None  # No snapshot
        mock_cur.fetchall.return_value = [
            {
                "ticker": "GOOGL",
                "quantity": 5,
                "avg_entry_price": 140.0,
                "current_price": None,
                "market_value": None,
                "unrealized_pl": None,
                "stop_loss": None,
                "exit_condition": "",
            },
        ]
        mock_get_db.return_value = mock_conn

        result = get_portfolio("trader-stonks")
        assert len(result["positions"]) == 1
        assert result["positions"][0]["current_price"] == 0
        assert result["positions"][0]["market_value"] == 0

    @patch("src.trade_context._get_alpaca_client", return_value=None)
    @patch("src.trade_context._get_db")
    def test_both_unavailable(self, mock_get_db, mock_alpaca):
        """When PG also fails, returns empty placeholder."""
        mock_get_db.side_effect = Exception("PG down")
        result = get_portfolio("trader-kairos")
        assert result["source"] == "unavailable"
        assert result["positions"] == []

    @patch("src.trade_context._get_alpaca_client")
    def test_alpaca_success(self, mock_get_client):
        """When Alpaca is available, uses it directly."""
        mock_client = MagicMock()
        mock_account = MagicMock()
        mock_account.cash = "5000.00"
        mock_account.equity = "12000.00"
        mock_account.buying_power = "10000.00"
        mock_client.get_account.return_value = mock_account
        mock_client.get_all_positions.return_value = []
        mock_get_client.return_value = mock_client

        result = get_portfolio("trader-kairos")
        assert result["source"] == "alpaca_live"
        assert result["cash"] == 5000.0
        assert result["portfolio_value"] == 12000.0
        assert result["buying_power"] == 10000.0

    @patch("src.trade_context._get_alpaca_client")
    def test_alpaca_with_positions(self, mock_get_client):
        """Alpaca returns positions correctly."""
        mock_client = MagicMock()
        mock_account = MagicMock()
        mock_account.cash = "3000.00"
        mock_account.equity = "10500.00"
        mock_account.buying_power = "6000.00"
        mock_client.get_account.return_value = mock_account

        mock_pos = MagicMock()
        mock_pos.symbol = "AAPL"
        mock_pos.qty = "10"
        mock_pos.avg_entry_price = "185.0"
        mock_pos.current_price = "190.5"
        mock_pos.unrealized_pl = "55.0"
        mock_pos.unrealized_plpc = "0.0297"
        mock_pos.market_value = "1905.0"
        mock_client.get_all_positions.return_value = [mock_pos]
        mock_get_client.return_value = mock_client

        result = get_portfolio("trader-kairos")
        assert len(result["positions"]) == 1
        assert result["positions"][0]["ticker"] == "AAPL"
        assert result["positions"][0]["qty"] == 10.0
        assert result["positions"][0]["unrealized_plpc"] == 2.97

    @patch("src.trade_context._get_alpaca_client")
    def test_alpaca_fallback_on_error(self, mock_get_client):
        """When Alpaca throws, falls back to Postgres."""
        mock_client = MagicMock()
        mock_client.get_account.side_effect = Exception("API error")
        mock_get_client.return_value = mock_client

        # PG also fails → should get fallback
        with patch("src.trade_context._get_db", side_effect=Exception("no PG")):
            result = get_portfolio("trader-kairos")
            assert result["source"] == "unavailable"

    def test_credential_shortcuts(self):
        """trader-{kairos,aldridge,stonks} all resolve to correct credential keys."""
        for agent_id in ["trader-kairos", "trader-aldridge", "trader-stonks"]:
            with patch("src.trade_context._get_alpaca_client") as mock:
                mock.return_value = None
                with patch("src.trade_context._get_db", side_effect=Exception("db offline")):
                    result = get_portfolio(agent_id)
                    assert result["source"] == "unavailable"


# ── get_recent_trades ─────────────────────────────────────────────────────────


class TestGetRecentTrades:
    @patch("src.trade_context._get_db")
    def test_returns_trades(self, mock_get_db):
        mock_conn = MagicMock()
        mock_cur = mock_conn.cursor.return_value
        mock_cur.fetchall.return_value = [
            {"ticker": "AAPL", "action": "BUY", "quantity": 10, "entry_price": "185.0",
             "exit_price": None, "pnl": None, "entry_time": "2026-07-15", "exit_time": None, "status": "open"},
        ]
        mock_get_db.return_value = mock_conn

        trades = get_recent_trades("trader-kairos", limit=5)
        assert len(trades) == 1
        assert trades[0]["ticker"] == "AAPL"

    @patch("src.trade_context._get_db")
    def test_db_error(self, mock_get_db):
        mock_get_db.side_effect = Exception("connection refused")
        trades = get_recent_trades("trader-kairos")
        assert trades == []


# ── get_recent_decisions ──────────────────────────────────────────────────────


class TestGetRecentDecisions:
    @patch("src.trade_context._get_db")
    def test_returns_decisions(self, mock_get_db):
        mock_conn = MagicMock()
        mock_cur = mock_conn.cursor.return_value
        mock_cur.fetchall.return_value = [
            {"trader_id": "trader-kairos", "ticker": "AAPL", "decision": "BUY",
             "conviction": 0.75, "rationale": "Momentum", "timestamp": "2026-07-16"},
        ]
        mock_get_db.return_value = mock_conn

        decisions = get_recent_decisions("trader-kairos", limit=3)
        assert len(decisions) == 1
        assert decisions[0]["decision"] == "BUY"

    @patch("src.trade_context._get_db")
    def test_db_error(self, mock_get_db):
        mock_get_db.side_effect = Exception("connection refused")
        decisions = get_recent_decisions("trader-kairos")
        assert decisions == []


# ── get_performance_stats ─────────────────────────────────────────────────────


class TestGetPerformanceStats:
    @patch("src.trade_context._get_db")
    def test_with_pnls(self, mock_get_db):
        mock_conn = MagicMock()
        mock_cur = mock_conn.cursor.return_value
        mock_cur.fetchall.return_value = [
            {"pnl": "250.0"},
            {"pnl": "-100.0"},
            {"pnl": "50.0"},
        ]
        mock_get_db.return_value = mock_conn

        stats = get_performance_stats("trader-kairos")
        assert stats["total_trades"] == 3
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert stats["win_rate"] == pytest.approx(0.6667, abs=0.01)
        assert stats["realized_pnl"] == 200.0

    @patch("src.trade_context._get_db")
    def test_no_trades(self, mock_get_db):
        mock_conn = MagicMock()
        mock_cur = mock_conn.cursor.return_value
        mock_cur.fetchall.return_value = []
        mock_get_db.return_value = mock_conn

        stats = get_performance_stats("trader-kairos")
        assert stats["total_trades"] == 0
        assert stats["wins"] == 0
        assert stats["win_rate"] == 0

    @patch("src.trade_context._get_db")
    def test_db_error(self, mock_get_db):
        mock_get_db.side_effect = Exception("no db")
        stats = get_performance_stats("trader-kairos")
        assert stats["wins"] == 0
        assert stats["total_trades"] == 0


# ── get_market_data ────────────────────────────────────────────────────────────


class TestGetMarketData:
    @patch("src.trade_context.urllib.request.urlopen")
    @patch("src.trade_context._get_db")
    def test_data_bus_success(self, mock_get_db, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "quotes": {"AAPL": {"close": 190.5, "rsi": 62.0}},
            "fear_greed": {"value": 55, "classification": "Neutral"},
            "regime": {"regime": "bullish", "confidence": 0.65},
        }).encode()
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        # DB returns signals
        mock_conn = MagicMock()
        mock_cur = mock_conn.cursor.return_value
        mock_cur.fetchone.return_value = {"composite_signal": "BUY", "conviction": "0.75", "regime": "bullish"}
        mock_get_db.return_value = mock_conn

        result = get_market_data(["AAPL"])
        assert "AAPL" in result["quotes"]
        assert result["fear_greed"]["value"] == 55
        assert result["regime"]["regime"] == "bullish"
        assert result["signals"]["AAPL"]["signal"] == "BUY"

    def test_no_data_sources(self):
        """When both data bus and DB are unavailable, returns empty."""
        with patch("src.trade_context.urllib.request.urlopen", side_effect=Exception("no bus")):
            with patch("src.trade_context._get_db", side_effect=Exception("no db")):
                result = get_market_data(["SPY"])
                assert result["quotes"] == {}
                assert result["signals"] == {}


# ── build_trade_context ───────────────────────────────────────────────────────


class TestBuildTradeContext:
    @patch("src.trade_context.get_performance_stats")
    @patch("src.trade_context.get_recent_decisions")
    @patch("src.trade_context.get_recent_trades")
    @patch("src.trade_context.get_market_data")
    @patch("src.trade_context.get_portfolio")
    def test_full_build(
        self, mock_portfolio, mock_market, mock_trades, mock_decisions, mock_perf,
        sample_portfolio, sample_market, sample_trades, sample_decisions,
    ):
        mock_portfolio.return_value = sample_portfolio
        mock_market.return_value = sample_market
        mock_trades.return_value = sample_trades
        mock_decisions.return_value = sample_decisions
        mock_perf.return_value = {
            "total_trades": 15, "wins": 9, "losses": 6,
            "win_rate": 0.60, "realized_pnl": 500.0,
        }

        context = build_trade_context("trader-kairos")

        assert "agent_id" in context
        assert "timestamp" in context
        assert "text" in context
        assert "data" in context
        assert context["agent_id"] == "trader-kairos"

        text = context["text"]
        assert "Kairós Capital" in text
        assert "PORTFOLIO" in text
        assert "MARKET DATA" in text
        assert "PERFORMANCE" in text
        assert "RECENT TRADES" in text
        assert "RECENT DECISIONS" in text
        assert "WATCHLIST" in text
        assert "AAPL" in text
        assert "NVDA" in text

        data = context["data"]
        assert "portfolio" in data
        assert "market" in data
        assert "recent_trades" in data
        assert "recent_decisions" in data
        assert "performance" in data

    @patch("src.trade_context.get_performance_stats")
    @patch("src.trade_context.get_recent_decisions")
    @patch("src.trade_context.get_recent_trades")
    @patch("src.trade_context.get_market_data")
    @patch("src.trade_context.get_portfolio")
    def test_no_signals_flag(
        self, mock_portfolio, mock_market, mock_trades, mock_decisions, mock_perf,
        sample_portfolio,
    ):
        mock_portfolio.return_value = sample_portfolio
        mock_market.return_value = {"quotes": {}, "signals": {}}
        mock_trades.return_value = []
        mock_decisions.return_value = []
        mock_perf.return_value = {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "realized_pnl": 0}

        context = build_trade_context("trader-aldridge", include_signals=False)
        # Should not crash, should still build text
        assert "TRADE CONTEXT" in context["text"]

    @patch("src.trade_context.get_performance_stats")
    @patch("src.trade_context.get_recent_decisions")
    @patch("src.trade_context.get_recent_trades")
    @patch("src.trade_context.get_market_data")
    @patch("src.trade_context.get_portfolio")
    def test_empty_portfolio(
        self, mock_portfolio, mock_market, mock_trades, mock_decisions, mock_perf,
    ):
        mock_portfolio.return_value = {
            "cash": 10000.0, "portfolio_value": 10000.0,
            "source": "alpaca_live", "positions": [],
        }
        mock_market.return_value = {}
        mock_trades.return_value = []
        mock_decisions.return_value = []
        mock_perf.return_value = {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "realized_pnl": 0}

        context = build_trade_context("trader-stonks")
        assert "No recent trades." in context["text"]
        assert "No recent decisions." in context["text"]

    def test_trader_name_fallback(self):
        """Unknown trader IDs still work (fallback to capitalized company name)."""
        with patch("src.trade_context.get_portfolio") as mock_pf:
            mock_pf.return_value = {
                "cash": 5000.0, "portfolio_value": 5000.0,
                "source": "unavailable", "positions": [],
            }
            with patch("src.trade_context.get_market_data", return_value={"quotes": {}, "signals": {}}):
                with patch("src.trade_context.get_recent_trades", return_value=[]):
                    with patch("src.trade_context.get_recent_decisions", return_value=[]):
                        with patch("src.trade_context.get_performance_stats",
                                   return_value={"total_trades": 0, "wins": 0, "losses": 0,
                                                "win_rate": 0, "realized_pnl": 0}):
                            context = build_trade_context("trader-unknown")
                            assert "Unknown" in context["text"]

    def test_trader_name_lookup(self):
        """Known traders get their display names."""
        names = {
            "trader-kairos": "Kairós Capital",
            "trader-aldridge": "Aldridge & Partners",
            "trader-stonks": "Stonks Capital",
        }
        with patch("src.trade_context.get_portfolio") as mock_pf:
            mock_pf.return_value = {
                "cash": 5000.0, "portfolio_value": 5000.0,
                "source": "unavailable", "positions": [],
            }
            with patch("src.trade_context.get_market_data", return_value={"quotes": {}, "signals": {}}):
                with patch("src.trade_context.get_recent_trades", return_value=[]):
                    with patch("src.trade_context.get_recent_decisions", return_value=[]):
                        with patch("src.trade_context.get_performance_stats",
                                   return_value={"total_trades": 0, "wins": 0, "losses": 0,
                                                "win_rate": 0, "realized_pnl": 0}):
                            for agent_id, expected_name in names.items():
                                context = build_trade_context(agent_id)
                                assert expected_name in context["text"], f"{agent_id} → {expected_name}"


# ── CLI ───────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_cli_text_output(self):
        """CLI --trader outputs formatted text."""
        with patch("src.trade_context.build_trade_context") as mock_build:
            mock_build.return_value = {
                "text": "=== TRADE CONTEXT for Test ===",
                "data": {},
                "agent_id": "trader-kairos",
                "timestamp": "2026-07-16T12:00:00Z",
            }
            with patch.object(sys, "argv", ["trade_context.py", "--trader", "kairos"]):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    main()
                assert "TRADE CONTEXT" in captured.getvalue()

    def test_cli_json_output(self):
        """CLI --json outputs JSON."""
        with patch("src.trade_context.build_trade_context") as mock_build:
            mock_build.return_value = {
                "text": "some text",
                "data": {"test": "value"},
                "agent_id": "trader-aldridge",
                "timestamp": "2026-07-16T12:00:00Z",
            }
            with patch.object(sys, "argv", ["trade_context.py", "--trader", "aldridge", "--json"]):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    main()
                parsed = json.loads(captured.getvalue())
                assert parsed["agent_id"] == "trader-aldridge"
                assert parsed["data"]["test"] == "value"

    def test_cli_no_signals_flag(self):
        """CLI --no-signals passes through to build_trade_context."""
        with patch("src.trade_context.build_trade_context") as mock_build:
            mock_build.return_value = {
                "text": "context without signals",
                "data": {},
                "agent_id": "trader-stonks",
                "timestamp": "2026-07-16T12:00:00Z",
            }
            with patch.object(sys, "argv", ["trade_context.py", "--trader", "stonks", "--no-signals"]):
                with patch("sys.stdout", StringIO()):
                    main()
                mock_build.assert_called_once_with("trader-stonks", include_signals=False)

    def test_cli_normalizes_trader_id(self):
        """--trader kairos becomes trader-kairos."""
        with patch("src.trade_context.build_trade_context") as mock_build:
            mock_build.return_value = {
                "text": "ok",
                "data": {},
                "agent_id": "trader-kairos",
                "timestamp": "2026-07-16T12:00:00Z",
            }
            with patch.object(sys, "argv", ["trade_context.py", "--trader", "kairos"]):
                with patch("sys.stdout", StringIO()):
                    main()
                mock_build.assert_called_once_with("trader-kairos", include_signals=True)

    def test_cli_already_prefixed(self):
        """--trader trader-kairos is passed as-is."""
        with patch("src.trade_context.build_trade_context") as mock_build:
            mock_build.return_value = {
                "text": "ok",
                "data": {},
                "agent_id": "trader-alderidge",
                "timestamp": "2026-07-16T12:00:00Z",
            }
            with patch.object(sys, "argv", ["trade_context.py", "--trader", "trader-aldridge"]):
                with patch("sys.stdout", StringIO()):
                    main()
                mock_build.assert_called_once_with("trader-aldridge", include_signals=True)
