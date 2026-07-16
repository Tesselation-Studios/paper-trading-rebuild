"""Tests for src/bar_loader.py — Parquet → Tick bridge."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.replay import Tick
from src.bar_loader import BarLoader


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_bars_dir():
    """Create a temporary directory with sample Parquet files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bars_dir = Path(tmpdir)

        # Build sample SPY data: 2 days of 5-min bars with indicators
        times_spy = pd.date_range(
            "2026-07-01 09:30", "2026-07-02 16:00", freq="5min", tz="UTC"
        )
        n = len(times_spy)
        rng = np.random.default_rng(42)
        base_price = 450.0

        spy_df = pd.DataFrame(
            {
                "timestamp": times_spy,
                "open": base_price + rng.normal(0, 0.5, n).cumsum(),
                "high": base_price + rng.normal(0, 0.5, n).cumsum() + rng.uniform(0, 0.3, n),
                "low": base_price + rng.normal(0, 0.5, n).cumsum() - rng.uniform(0, 0.3, n),
                "close": base_price + rng.normal(0, 0.5, n).cumsum(),
                "volume": rng.integers(1000, 50000, n),
                "rsi_14": 50.0 + rng.normal(0, 5, n),
                "macd_hist": rng.normal(0, 0.1, n),
                "atr_14": rng.uniform(0.5, 2.0, n),
            }
        )
        spy_df["high"] = spy_df[["open", "high", "close"]].max(axis=1)
        spy_df["low"] = spy_df[["open", "low", "close"]].min(axis=1)
        spy_df.to_parquet(bars_dir / "SPY.parquet", index=False)

        # Build sample AAPL data: 1 day with no indicators
        times_aapl = pd.date_range(
            "2026-07-01 09:30", "2026-07-01 16:00", freq="5min", tz="UTC"
        )
        aapl_df = pd.DataFrame(
            {
                "timestamp": times_aapl,
                "open": 200.0 + rng.normal(0, 0.2, len(times_aapl)).cumsum(),
                "high": 200.0 + rng.normal(0, 0.2, len(times_aapl)).cumsum() + 0.1,
                "low": 200.0 + rng.normal(0, 0.2, len(times_aapl)).cumsum() - 0.1,
                "close": 200.0 + rng.normal(0, 0.2, len(times_aapl)).cumsum(),
                "volume": rng.integers(5000, 100000, len(times_aapl)),
            }
        )
        aapl_df["high"] = aapl_df[["open", "high", "close"]].max(axis=1)
        aapl_df["low"] = aapl_df[["open", "low", "close"]].min(axis=1)
        aapl_df.to_parquet(bars_dir / "AAPL.parquet", index=False)

        # Create a temp database
        db_path = Path(tmpdir) / "test.db"

        yield bars_dir, db_path


@pytest.fixture
def loader(sample_bars_dir):
    """Return a BarLoader pointed at the sample data."""
    bars_dir, db_path = sample_bars_dir
    return BarLoader(bars_dir=bars_dir, db_path=db_path)


# ── Tests: available_dates ───────────────────────────────────────────────────


class TestAvailableDates:
    def test_returns_sorted_unique_dates(self, loader):
        dates = loader.available_dates("SPY")
        assert dates == ["2026-07-01", "2026-07-02"]

    def test_single_date_ticker(self, loader):
        dates = loader.available_dates("AAPL")
        assert dates == ["2026-07-01"]

    def test_nonexistent_ticker_returns_empty(self, loader):
        dates = loader.available_dates("NONEXIST")
        assert dates == []

    def test_corrupt_parquet_returns_empty(self, loader):
        # Write a corrupt file
        bad_path = loader.bars_dir / "BAD.parquet"
        bad_path.write_text("not a parquet file")
        dates = loader.available_dates("BAD")
        assert dates == []


# ── Tests: load_date_range ───────────────────────────────────────────────────


class TestLoadDateRange:
    def test_loads_ticks_for_one_ticker(self, loader):
        ticks = loader.load_date_range(["SPY"], "2026-07-01", "2026-07-01",
                                        interval_minutes=1)
        assert len(ticks) > 0
        assert all(isinstance(t, Tick) for t in ticks)
        assert all(t.ticker == "SPY" for t in ticks)

    def test_loads_ticks_for_multiple_tickers(self, loader):
        ticks = loader.load_date_range(
            ["SPY", "AAPL"], "2026-07-01", "2026-07-01", interval_minutes=1
        )
        tickers_seen = {t.ticker for t in ticks}
        assert tickers_seen == {"SPY", "AAPL"}

    def test_chronological_order(self, loader):
        ticks = loader.load_date_range(
            ["SPY", "AAPL"], "2026-07-01", "2026-07-02", interval_minutes=1
        )
        for i in range(1, len(ticks)):
            assert ticks[i].timestamp >= ticks[i - 1].timestamp, (
                f"Out of order at index {i}"
            )

    def test_date_range_filtering(self, loader):
        # Load only July 1 (AAPL only has July 1)
        ticks = loader.load_date_range(["AAPL"], "2026-07-01", "2026-07-01",
                                        interval_minutes=1)
        dates_seen = {t.timestamp.date().isoformat() for t in ticks}
        assert dates_seen == {"2026-07-01"}

        # Load only July 2 (AAPL has none)
        ticks = loader.load_date_range(["AAPL"], "2026-07-02", "2026-07-02",
                                        interval_minutes=1)
        assert len(ticks) == 0

    def test_nonexistent_ticker_skipped_gracefully(self, loader):
        ticks = loader.load_date_range(
            ["SPY", "NONEXIST"], "2026-07-01", "2026-07-02", interval_minutes=1
        )
        # Should have SPY ticks but no crash
        tickers = {t.ticker for t in ticks}
        assert "SPY" in tickers
        assert "NONEXIST" not in tickers

    def test_all_nonexistent_returns_empty(self, loader):
        ticks = loader.load_date_range(
            ["NONEXIST"], "2026-07-01", "2026-07-02", interval_minutes=1
        )
        assert ticks == []

    def test_tick_fields_populated(self, loader):
        ticks = loader.load_date_range(["SPY"], "2026-07-01", "2026-07-01",
                                        interval_minutes=1)
        t = ticks[0]
        assert isinstance(t.timestamp, datetime)
        assert t.ticker == "SPY"
        assert t.open > 0
        assert t.high >= t.low
        assert t.high >= t.open
        assert t.high >= t.close
        assert t.low <= t.open
        assert t.low <= t.close
        assert t.volume >= 0

    def test_indicators_mapped(self, loader):
        ticks = loader.load_date_range(["SPY"], "2026-07-01", "2026-07-01",
                                        interval_minutes=1)
        # SPY has indicators (rsi_14, macd_hist, atr_14)
        has_rsi = any(t.rsi is not None for t in ticks)
        has_momentum = any(t.momentum is not None for t in ticks)
        has_volatility = any(t.volatility is not None for t in ticks)
        assert has_rsi, "rsi should be mapped from rsi_14"
        assert has_momentum, "momentum should be mapped from macd_hist"
        assert has_volatility, "volatility should be mapped from atr_14"

    def test_no_indicators_for_aapl(self, loader):
        ticks = loader.load_date_range(["AAPL"], "2026-07-01", "2026-07-01",
                                        interval_minutes=1)
        # AAPL has no indicator columns
        assert all(t.rsi is None for t in ticks)
        assert all(t.momentum is None for t in ticks)
        assert all(t.volatility is None for t in ticks)


# ── Tests: downsampling ──────────────────────────────────────────────────────


class TestDownsampling:
    def test_30min_downsampling_reduces_count(self, loader):
        raw = loader.load_date_range(["SPY"], "2026-07-01", "2026-07-01",
                                      interval_minutes=1)
        downsampled = loader.load_date_range(
            ["SPY"], "2026-07-01", "2026-07-01", interval_minutes=30
        )
        assert len(downsampled) < len(raw)
        assert len(downsampled) > 0

    def test_downsampled_ticks_preserve_ohlcv_semantics(self, loader):
        ticks = loader.load_date_range(
            ["SPY"], "2026-07-01", "2026-07-01", interval_minutes=30
        )
        for t in ticks:
            assert t.open > 0
            assert t.high >= t.low
            assert t.high >= t.open
            assert t.high >= t.close
            assert t.low <= t.open
            assert t.low <= t.close
            assert t.volume >= 0

    def test_interval_1_is_passthrough(self, loader):
        ticks = loader.load_date_range(
            ["SPY"], "2026-07-01", "2026-07-01", interval_minutes=1
        )
        # With interval=1, we should get exact bar count
        df = pd.read_parquet(loader.bars_dir / "SPY.parquet")
        july1_mask = df["timestamp"].dt.date == pd.Timestamp("2026-07-01").date()
        expected_count = july1_mask.sum()
        assert len(ticks) == expected_count


# ── Tests: missing_dates ─────────────────────────────────────────────────────


class TestMissingDates:
    def test_detects_missing_dates(self, loader):
        missing = loader.missing_dates(
            ["SPY", "AAPL", "NONEXIST"], "2026-06-25", "2026-07-02"
        )
        assert len(missing) > 0
        # Each entry should be a (ticker, date_string) tuple
        for pair in missing:
            assert isinstance(pair, tuple)
            assert len(pair) == 2
            assert isinstance(pair[0], str)
            assert isinstance(pair[1], str)

    def test_full_range_present_returns_empty(self, loader):
        missing = loader.missing_dates(["SPY"], "2026-07-01", "2026-07-02")
        assert missing == []

    def test_weekends_excluded(self, loader):
        # 2026-07-04 is a Saturday, 2026-07-05 is a Sunday
        missing = loader.missing_dates(["SPY"], "2026-07-04", "2026-07-05")
        # These are weekends, so no trading dates are expected
        assert missing == []

    def test_nonexistent_ticker_all_missing(self, loader):
        missing = loader.missing_dates(["NONEXIST"], "2026-07-01", "2026-07-02")
        assert len(missing) == 2  # Both days are missing
        assert all(t == "NONEXIST" for t, d in missing)


# ── Tests: to_sqlite_cache ───────────────────────────────────────────────────


class TestSqliteCache:
    def test_caches_and_returns_count(self, loader):
        count = loader.to_sqlite_cache(["SPY", "AAPL"], "2026-07-01", "2026-07-02")
        assert count > 0

    def test_load_from_cache_returns_ticks(self, loader):
        loader.to_sqlite_cache(["SPY"], "2026-07-01", "2026-07-02")
        cached = loader.load_from_cache(tickers=["SPY"])
        assert len(cached) > 0
        assert all(isinstance(t, Tick) for t in cached)
        assert all(t.ticker == "SPY" for t in cached)

    def test_cache_date_filter(self, loader):
        loader.to_sqlite_cache(["SPY"], "2026-07-01", "2026-07-02")
        cached_july1 = loader.load_from_cache(
            tickers=["SPY"], start_date="2026-07-01", end_date="2026-07-01"
        )
        cached_all = loader.load_from_cache(tickers=["SPY"])
        assert len(cached_july1) <= len(cached_all)

    def test_cache_ticker_filter(self, loader):
        loader.to_sqlite_cache(["SPY", "AAPL"], "2026-07-01", "2026-07-02")
        spy_only = loader.load_from_cache(tickers=["SPY"])
        assert all(t.ticker == "SPY" for t in spy_only)

    def test_empty_cache_returns_empty(self, loader):
        # Load from cache without populating it first
        # Use a fresh loader to ensure empty cache
        bars_dir = loader.bars_dir
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            fresh_db = Path(f.name)
        try:
            bl = BarLoader(bars_dir=bars_dir, db_path=fresh_db)
            cached = bl.load_from_cache()
            assert cached == []
        finally:
            fresh_db.unlink(missing_ok=True)

    def test_cache_is_reproducible(self, loader):
        """Loading from Parquet and from cache should produce identical ticks."""
        # Use interval_minutes=1 so cache matches raw ticks
        from_parquet = loader.load_date_range(
            ["SPY"], "2026-07-01", "2026-07-01", interval_minutes=1
        )

        loader.to_sqlite_cache(["SPY"], "2026-07-01", "2026-07-01", interval_minutes=1)
        from_cache = loader.load_from_cache(
            tickers=["SPY"], start_date="2026-07-01", end_date="2026-07-01"
        )

        assert len(from_cache) == len(from_parquet)
        for pt, ct in zip(from_parquet, from_cache):
            assert ct.ticker == pt.ticker
            assert ct.open == pt.open
            assert ct.high == pt.high
            assert ct.low == pt.low
            assert ct.close == pt.close
            assert ct.volume == pt.volume


# ── Tests: validate_distribution ─────────────────────────────────────────────


class TestValidateDistribution:
    def test_balanced_data_passes(self, loader):
        """AAPL sample data (1 day, market hours only) should be balanced."""
        # SPY fixture spans across non-market hours (24h range),
        # so it has >100 bars per date. Use AAPL which is single-day.
        balanced, sparse, dense = loader.validate_distribution("AAPL")
        # AAPL fixture: 1 day (July 1 only) of 5-min bars, 9:30-16:00 = ~78
        # With the standard range, this should be balanced
        assert len(balanced) == 1, f"Expected 1 balanced date for AAPL, got {len(balanced)}: balanced={balanced}, sparse={sparse}, dense={dense}"
        assert len(sparse) == 0
        assert len(dense) == 0

    def test_nonexistent_ticker(self, loader):
        """Nonexistent ticker returns empty lists."""
        balanced, sparse, dense = loader.validate_distribution("NONEXIST")
        assert balanced == []
        assert sparse == []
        assert dense == []

    def test_all_distributions_balanced(self, loader):
        """AAPL alone should be balanced."""
        # SPY fixture spans non-market hours, so not balanced.
        # AAPL is single-day market-hours-only.
        assert loader.all_distributions_balanced(["AAPL"])

    def test_all_distributions_balanced_with_missing(self, loader):
        """Returns True when only valid data exists (NONEXIST has no dense dates)."""
        assert loader.all_distributions_balanced(["AAPL", "NONEXIST"])


# ── Integration: verify with real data ───────────────────────────────────────


class TestRealData:
    """Smoke test using actual bar files if they exist."""

    def test_real_spy_data_loads(self):
        """Verify BarLoader works with the real shared/cache/bars/ directory."""
        bl = BarLoader()
        dates = bl.available_dates("SPY")
        if not dates:
            pytest.skip("No real SPY data available")

        ticks = bl.load_date_range(["SPY"], dates[0], dates[-1],
                                    interval_minutes=30)
        assert len(ticks) > 0
        assert all(t.ticker == "SPY" for t in ticks)
