"""Tests for Aldridge buy-and-hold value strategy."""

import pytest

from src.fundamentals import Fundamentals
from src.aldridge_strategy import (
    ScreenParams,
    Portfolio,
    Position,
    screen_candidates,
    weekly_rebalance,
)


# ── screen_candidates ─────────────────────────────────────────────────────────


class TestScreenCandidates:
    def test_passing_stock(self):
        """A stock meeting all criteria should pass."""
        f = Fundamentals(
            ticker="GOOD",
            pe_ratio=15.0,
            dividend_yield=2.5,
            earnings_growth=8.0,
            debt_to_equity=1.2,
        )
        candidates = screen_candidates([f])
        assert candidates == ["GOOD"]

    def test_multiple_candidates(self):
        """Multiple passing stocks should all be returned."""
        stocks = [
            Fundamentals(ticker="A", pe_ratio=10.0, dividend_yield=2.0,
                         earnings_growth=10.0, debt_to_equity=1.0),
            Fundamentals(ticker="B", pe_ratio=15.0, dividend_yield=3.0,
                         earnings_growth=7.0, debt_to_equity=0.5),
        ]
        candidates = screen_candidates(stocks)
        assert set(candidates) == {"A", "B"}

    def test_mixed_candidates(self):
        """Only passing stocks are returned from a mixed list."""
        stocks = [
            Fundamentals(ticker="GOOD", pe_ratio=15.0, dividend_yield=2.0,
                         earnings_growth=10.0, debt_to_equity=1.0),
            Fundamentals(ticker="BAD_PE", pe_ratio=30.0, dividend_yield=2.0,
                         earnings_growth=10.0, debt_to_equity=1.0),
            Fundamentals(ticker="BAD_DIV", pe_ratio=15.0, dividend_yield=0.5,
                         earnings_growth=10.0, debt_to_equity=1.0),
            Fundamentals(ticker="BAD_EG", pe_ratio=15.0, dividend_yield=2.0,
                         earnings_growth=2.0, debt_to_equity=1.0),
            Fundamentals(ticker="BAD_DE", pe_ratio=15.0, dividend_yield=2.0,
                         earnings_growth=10.0, debt_to_equity=3.0),
            Fundamentals(ticker="NEG_PE", pe_ratio=-1.0, dividend_yield=2.0,
                         earnings_growth=10.0, debt_to_equity=1.0),
        ]
        candidates = screen_candidates(stocks)
        assert candidates == ["GOOD"]

    def test_none_values_rejected(self):
        """Stocks with None for required fields should be rejected."""
        f = Fundamentals(ticker="NONEY", pe_ratio=None, dividend_yield=None,
                         earnings_growth=None, debt_to_equity=None)
        candidates = screen_candidates([f])
        assert candidates == []

    def test_empty_list(self):
        """Empty input returns empty output."""
        assert screen_candidates([]) == []

    def test_custom_params(self):
        """Custom screening parameters should be respected."""
        params = ScreenParams(pe_max=10.0, div_min_pct=3.0)
        f = Fundamentals(ticker="OK", pe_ratio=12.0, dividend_yield=2.0,
                         earnings_growth=10.0, debt_to_equity=1.0)
        # P/E 12 > custom max 10, and div 2% < custom min 3%
        candidates = screen_candidates([f], params=params)
        assert candidates == []


class TestScreenParamsDefaults:
    def test_default_params(self):
        p = ScreenParams()
        assert p.pe_min == 0.0
        assert p.pe_max == 20.0
        assert p.div_min_pct == 1.0
        assert p.eg_min_pct == 5.0
        assert p.de_max == 2.0


# ── weekly_rebalance ──────────────────────────────────────────────────────────


class TestWeeklyRebalance:
    def test_hold_candidates(self):
        """Tickers still in screen should be held."""
        portfolio = Portfolio(
            cash=100000,
            positions={
                "AAPL": Position(ticker="AAPL", shares=100, avg_cost=150.0),
                "MSFT": Position(ticker="MSFT", shares=50, avg_cost=300.0),
            },
        )
        candidates = ["AAPL", "MSFT", "GOOGL"]
        actions = weekly_rebalance(portfolio, candidates)
        assert actions == {"AAPL": "HOLD", "MSFT": "HOLD"}

    def test_sell_fallen_out(self):
        """Tickers that fall out of the screen should be sold."""
        portfolio = Portfolio(
            cash=100000,
            positions={
                "AAPL": Position(ticker="AAPL", shares=100, avg_cost=150.0),
                "FALLEN": Position(ticker="FALLEN", shares=50, avg_cost=50.0),
            },
        )
        candidates = ["AAPL", "MSFT"]  # FALLEN is no longer a candidate
        actions = weekly_rebalance(portfolio, candidates)
        assert actions == {"AAPL": "HOLD", "FALLEN": "SELL"}

    def test_sell_all_when_no_candidates(self):
        """When no candidates pass, all positions should be sold."""
        portfolio = Portfolio(
            cash=100000,
            positions={
                "AAPL": Position(ticker="AAPL", shares=100, avg_cost=150.0),
            },
        )
        candidates: list = []
        actions = weekly_rebalance(portfolio, candidates)
        assert actions == {"AAPL": "SELL"}

    def test_empty_portfolio(self):
        """Empty portfolio should get empty actions."""
        portfolio = Portfolio(cash=100000)
        candidates = ["AAPL", "MSFT"]
        actions = weekly_rebalance(portfolio, candidates)
        assert actions == {}

    def test_mixed_hold_sell(self):
        """Complex scenario with multiple holds and sells."""
        portfolio = Portfolio(
            cash=100000,
            positions={
                "AAPL": Position(ticker="AAPL", shares=100, avg_cost=150.0),
                "GOOGL": Position(ticker="GOOGL", shares=30, avg_cost=140.0),
                "TSLA": Position(ticker="TSLA", shares=20, avg_cost=250.0),
                "F": Position(ticker="F", shares=200, avg_cost=12.0),
            },
        )
        candidates = ["AAPL", "GOOGL"]  # TSLA and F fell out
        actions = weekly_rebalance(portfolio, candidates)
        assert actions == {
            "AAPL": "HOLD",
            "GOOGL": "HOLD",
            "TSLA": "SELL",
            "F": "SELL",
        }


# ── Portfolio model ───────────────────────────────────────────────────────────


class TestPortfolio:
    def test_empty_portfolio(self):
        p = Portfolio(cash=50000)
        assert p.cash == 50000
        assert p.tickers() == []

    def test_portfolio_tickers(self):
        p = Portfolio(
            cash=100000,
            positions={
                "AAPL": Position(ticker="AAPL", shares=100, avg_cost=150.0),
                "MSFT": Position(ticker="MSFT", shares=50, avg_cost=300.0),
            },
        )
        assert set(p.tickers()) == {"AAPL", "MSFT"}


class TestPosition:
    def test_position_value(self):
        pos = Position(ticker="AAPL", shares=10, avg_cost=150.0)
        assert pos.value == 1500.0
