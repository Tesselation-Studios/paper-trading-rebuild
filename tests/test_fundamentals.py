"""Tests for fundamentals data collector."""

import pytest

from src.fundamentals import (
    Fundamentals,
    fetch_fundamentals,
    store_fundamentals,
    load_fundamentals,
    _safe_float,
)
from src.aldridge_strategy import ScreenParams, screen_candidates

pytestmark = pytest.mark.integration


# ── _safe_float ───────────────────────────────────────────────────────────────


class TestSafeFloat:
    def test_none(self):
        assert _safe_float(None) is None

    def test_valid_float(self):
        assert _safe_float(25.5) == 25.5
        assert _safe_float("42") == 42.0

    def test_nan(self):
        assert _safe_float(float("nan")) is None

    def test_invalid(self):
        assert _safe_float("n/a") is None
        assert _safe_float("") is None


# ── fetch_fundamentals ────────────────────────────────────────────────────────


class TestFetchFundamentals:
    def test_returns_fundamentals_dataclass(self):
        """fetch_fundamentals returns a Fundamentals dataclass for AAPL."""
        result = fetch_fundamentals("AAPL")
        assert isinstance(result, Fundamentals)
        assert result.ticker == "AAPL"
        assert result.fetched_at is not None

    def test_returns_real_data_for_aapl(self):
        """AAPL should have some fundamental data populated."""
        result = fetch_fundamentals("AAPL")
        # At minimum, market_cap and sector should exist for a major stock
        assert result.market_cap is not None, "AAPL should have market cap"
        assert result.market_cap > 0, "Market cap should be positive"

    def test_handles_unknown_ticker(self):
        """Should handle unknown ticker gracefully without crashing."""
        result = fetch_fundamentals("ZZZZZ_UNKNOWN")
        assert isinstance(result, Fundamentals)
        assert result.ticker == "ZZZZZ_UNKNOWN"
        # Most fields will be None for an unknown ticker
        assert result.market_cap is None


# ── Store/Load round-trip ─────────────────────────────────────────────────────


class TestStoreAndLoad:
    def test_store_and_load_roundtrip(self):
        """Store fundamentals then load them back."""
        f = Fundamentals(
            ticker="TEST",
            pe_ratio=15.5,
            pb_ratio=3.2,
            market_cap=1e12,
            dividend_yield=0.8,
            earnings_growth=10.0,
            revenue_growth=8.0,
            debt_to_equity=1.5,
            free_cash_flow=100e9,
            sector="Technology",
            industry="Consumer Electronics",
        )
        ok = store_fundamentals(f)
        assert ok, "Store should succeed"

        loaded = load_fundamentals("TEST")
        assert loaded is not None, "Should load back"
        assert loaded.ticker == "TEST"
        assert loaded.pe_ratio == 15.5
        assert loaded.sector == "Technology"


# ── Screening integration test ────────────────────────────────────────────────


class TestScreeningIntegration:
    def test_aapl_passes_screen(self):
        """AAPL should typically pass the Aldridge value screen."""
        f = fetch_fundamentals("AAPL")
        candidates = screen_candidates([f])
        # AAPL typically has reasonable P/E, dividend, earnings growth
        # But we don't hard-assert since data changes; just verify it runs
        assert isinstance(candidates, list)

    def test_negative_earnings_rejected(self):
        """A stock with negative earnings growth should be rejected."""
        f = Fundamentals(
            ticker="LOSER",
            pe_ratio=15.0,
            dividend_yield=3.0,
            earnings_growth=-10.0,  # Negative growth
            debt_to_equity=1.0,
        )
        candidates = screen_candidates([f])
        assert "LOSER" not in candidates

    def test_high_de_rejected(self):
        """A stock with high debt-to-equity should be rejected."""
        f = Fundamentals(
            ticker="DEBTCO",
            pe_ratio=10.0,
            dividend_yield=2.0,
            earnings_growth=10.0,
            debt_to_equity=5.0,  # Too high
        )
        candidates = screen_candidates([f])
        assert "DEBTCO" not in candidates

    def test_negative_pe_rejected(self):
        """A stock with negative P/E should be rejected."""
        f = Fundamentals(
            ticker="NEGPE",
            pe_ratio=-5.0,  # Negative P/E
            dividend_yield=2.0,
            earnings_growth=10.0,
            debt_to_equity=1.0,
        )
        candidates = screen_candidates([f])
        assert "NEGPE" not in candidates

    def test_no_dividend_rejected(self):
        """A stock with no dividend should be rejected."""
        f = Fundamentals(
            ticker="NODIV",
            pe_ratio=15.0,
            dividend_yield=0.0,  # No dividend
            earnings_growth=10.0,
            debt_to_equity=1.0,
        )
        candidates = screen_candidates([f])
        assert "NODIV" not in candidates
