"""Tests for scripts/backfill_bars.py."""

import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# Ensure scripts/ is importable
PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# conftest.py mocks pandas_ta globally — replace with the real module
if "pandas_ta" in sys.modules and isinstance(sys.modules["pandas_ta"], MagicMock):
    del sys.modules["pandas_ta"]

try:
    import backfill_bars as bb
except ImportError as exc:
    pytest.skip(f"backfill_bars requires pandas_ta (not installed): {exc}", allow_module_level=True)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sample_bars_df():
    """Create a sample DataFrame with enough rows for all indicators.
    MACD(12,26,9) needs 26+ periods; use 40 rows with realistic price movements."""
    now = datetime(2026, 7, 1, 14, 30, tzinfo=timezone.utc)
    rows = []
    # Oscillating prices to produce meaningful RSI and MACD values
    prices = [100.0]
    for i in range(1, 40):
        # Alternate up and down with some noise
        if i % 3 == 0:
            prices.append(prices[-1] - 0.3)
        elif i % 5 == 0:
            prices.append(prices[-1] - 0.1)
        else:
            prices.append(prices[-1] + 0.5)

    for i in range(40):
        ts = now + timedelta(minutes=i * 5)
        close = prices[i]
        rows.append({
            "timestamp": ts,
            "open": close - 0.1,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": float(10000 + i * 100),
        })
    return pd.DataFrame(rows)


@pytest.fixture
def temp_bars_dir():
    """Temporary bars directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orig = bb.BARS_DIR
        bb.BARS_DIR = Path(tmpdir)
        yield Path(tmpdir)
        bb.BARS_DIR = orig


# ══════════════════════════════════════════════════════════════════════════════
# resolve_tickers
# ══════════════════════════════════════════════════════════════════════════════

def test_resolve_core():
    """--tickers core resolves to 8 core tickers."""
    tickers = bb.resolve_tickers("core")
    assert len(tickers) == 8
    assert "SPY" in tickers
    assert "AAPL" in tickers
    assert "MSFT" in tickers


def test_resolve_all():
    """--tickers all resolves to the union of all trader tickers."""
    tickers = bb.resolve_tickers("all")
    assert len(tickers) >= 30
    # Should include tickers from all three trader groups
    assert "SPY" in tickers
    assert "COIN" in tickers
    assert "JPM" in tickers


def test_resolve_comma_list():
    """Comma-separated list is parsed correctly."""
    tickers = bb.resolve_tickers("AAPL,MSFT,NVDA")
    assert tickers == ["AAPL", "MSFT", "NVDA"]


def test_resolve_comma_list_whitespace():
    tickers = bb.resolve_tickers(" AAPL , MSFT , NVDA ")
    assert tickers == ["AAPL", "MSFT", "NVDA"]


# ══════════════════════════════════════════════════════════════════════════════
# existing_dates
# ══════════════════════════════════════════════════════════════════════════════

def test_existing_dates_empty(temp_bars_dir):
    """No file → empty set."""
    dates = bb.existing_dates("UNKNOWN")
    assert dates == set()


def test_existing_dates_reads_from_parquet(temp_bars_dir):
    """Reads dates from existing Parquet file."""
    df = pd.DataFrame({
        "timestamp": [
            pd.Timestamp("2026-07-01 10:00:00", tz="UTC"),
            pd.Timestamp("2026-07-01 14:00:00", tz="UTC"),
            pd.Timestamp("2026-07-02 09:30:00", tz="UTC"),
        ],
    })
    df.to_parquet(temp_bars_dir / "TEST.parquet")
    dates = bb.existing_dates("TEST")
    assert dates == {"2026-07-01", "2026-07-02"}


# ══════════════════════════════════════════════════════════════════════════════
# missing_date_range
# ══════════════════════════════════════════════════════════════════════════════

def test_missing_date_range_fully_covered():
    """All expected dates exist → returns (None, None)."""
    today = date.today()
    existing = set()
    # days=5 → start = today - 6, end = today. Need 7 dates (inclusive).
    for i in range(7):
        d = today - timedelta(days=i)
        existing.add(d.strftime("%Y-%m-%d"))

    start, end = bb.missing_date_range("TICKER", days=5, existing=existing)
    assert start is None
    assert end is None


def test_missing_date_range_partial():
    """Some dates missing → returns appropriate range."""
    today = date.today()
    existing = {today.strftime("%Y-%m-%d")}  # only today

    start, end = bb.missing_date_range("TICKER", days=5, existing=existing)
    assert start is not None
    assert end is not None


# ══════════════════════════════════════════════════════════════════════════════
# compute_indicators
# ══════════════════════════════════════════════════════════════════════════════

def test_compute_indicators_adds_columns(sample_bars_df):
    """Technical indicator columns are added."""
    result = bb.compute_indicators(sample_bars_df.copy())
    required_cols = {"rsi_14", "macd", "macd_signal", "macd_hist", "atr_14"}
    for col in required_cols:
        assert col in result.columns, f"Missing column: {col}"


def test_compute_indicators_float64(sample_bars_df):
    """Indicator columns are float64."""
    result = bb.compute_indicators(sample_bars_df.copy())
    for col in ["rsi_14", "macd", "macd_signal", "macd_hist", "atr_14"]:
        assert result[col].dtype == "float64", f"{col} is {result[col].dtype}"


def test_compute_indicators_nan_for_early_rows(sample_bars_df):
    """Early rows have NaN for indicators that need warmup.

    Uses the sample_bars_df fixture which has 40 rows with oscillating prices.
    """
    result = bb.compute_indicators(sample_bars_df.copy())

    # RSI(14): at least the first row should be NaN (warmup)
    assert result["rsi_14"].iloc[0] is pd.NA or pd.isna(result["rsi_14"].iloc[0])
    # Later rows should have valid values
    non_nan = result["rsi_14"].iloc[14:].dropna()
    assert len(non_nan) > 0, f"RSI should have non-NaN values after warmup, got {len(non_nan)}"

    # ATR(14): early rows NaN, later rows valid
    assert result["atr_14"].iloc[0] is pd.NA or pd.isna(result["atr_14"].iloc[0])
    non_nan_atr = result["atr_14"].iloc[14:].dropna()
    assert len(non_nan_atr) > 0, f"ATR should have non-NaN values after warmup, got {len(non_nan_atr)}"

    # MACD(12,26,9): early rows NaN, later rows valid
    assert result["macd"].iloc[0] is pd.NA or pd.isna(result["macd"].iloc[0])
    non_nan_macd = result["macd"].iloc[26:].dropna()
    assert len(non_nan_macd) > 0, f"MACD should have non-NaN values after warmup, got {len(non_nan_macd)}"


# ══════════════════════════════════════════════════════════════════════════════
# merge_and_dedup
# ══════════════════════════════════════════════════════════════════════════════

def test_merge_no_existing(sample_bars_df, temp_bars_dir):
    """No existing file → returns new data as-is."""
    path = temp_bars_dir / "NEW.parquet"
    result = bb.merge_and_dedup(path, sample_bars_df)
    assert len(result) == len(sample_bars_df)
    # result should be sorted by timestamp
    assert result["timestamp"].is_monotonic_increasing


def test_merge_dedup_keeps_newer(temp_bars_dir):
    """When merging, duplicate timestamps keep the newer row."""
    ts = pd.Timestamp("2026-07-01 14:30:00+00:00")

    # Existing: old value
    existing = pd.DataFrame({
        "timestamp": [ts],
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.5],
        "volume": [10000.0],
    })
    path = temp_bars_dir / "DEDUP.parquet"
    existing.to_parquet(path, index=False)

    # New: same timestamp, different values
    new = pd.DataFrame({
        "timestamp": [ts],
        "open": [200.0],
        "high": [201.0],
        "low": [199.0],
        "close": [200.5],
        "volume": [20000.0],
    })

    result = bb.merge_and_dedup(path, new)
    assert len(result) == 1
    assert result["open"].iloc[0] == 200.0  # newer value wins


def test_merge_appends_new_timestamps(temp_bars_dir):
    """New timestamps are appended, not overwritten."""
    ts1 = pd.Timestamp("2026-07-01 14:30:00+00:00")
    ts2 = pd.Timestamp("2026-07-01 14:35:00+00:00")

    existing = pd.DataFrame({
        "timestamp": [ts1],
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.5],
        "volume": [10000.0],
    })
    path = temp_bars_dir / "APPEND.parquet"
    existing.to_parquet(path, index=False)

    new = pd.DataFrame({
        "timestamp": [ts2],
        "open": [200.0],
        "high": [201.0],
        "low": [199.0],
        "close": [200.5],
        "volume": [20000.0],
    })

    result = bb.merge_and_dedup(path, new)
    assert len(result) == 2
    assert result["timestamp"].is_monotonic_increasing


# ══════════════════════════════════════════════════════════════════════════════
# atomic_write
# ══════════════════════════════════════════════════════════════════════════════

def test_atomic_write_creates_file(sample_bars_df, temp_bars_dir):
    """Atomic write creates a valid Parquet file."""
    path = temp_bars_dir / "ATOMIC.parquet"
    bb.atomic_write(sample_bars_df, path)

    assert path.exists()
    df = pd.read_parquet(path)
    assert len(df) == len(sample_bars_df)


def test_atomic_write_overwrites(sample_bars_df, temp_bars_dir):
    """Atomic write overwrites existing file."""
    path = temp_bars_dir / "OVERWRITE.parquet"

    # Write once
    small = sample_bars_df.iloc[:5].copy()
    bb.atomic_write(small, path)
    assert len(pd.read_parquet(path)) == 5

    # Overwrite with full
    bb.atomic_write(sample_bars_df, path)
    assert len(pd.read_parquet(path)) == len(sample_bars_df)


# ══════════════════════════════════════════════════════════════════════════════
# fetch_bars — mocked
# ══════════════════════════════════════════════════════════════════════════════

def test_fetch_bars_returns_dataframe():
    """fetch_bars returns proper DataFrame with mocked yfinance."""
    mock_df = pd.DataFrame({
        "Datetime": [
            pd.Timestamp("2026-07-01 14:30:00", tz="UTC"),
            pd.Timestamp("2026-07-01 14:35:00", tz="UTC"),
        ],
        "Open": [100.0, 101.0],
        "High": [102.0, 103.0],
        "Low": [99.0, 100.0],
        "Close": [101.0, 102.0],
        "Volume": [10000, 12000],
    })

    with patch("backfill_bars.yf.download", return_value=mock_df):
        result = bb.fetch_bars("AAPL", "2026-07-01", "2026-07-02")

    assert result is not None
    assert len(result) == 2
    assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert result["timestamp"].dt.tz is not None  # has timezone


def test_fetch_bars_empty():
    """Empty DataFrame → None."""
    with patch("backfill_bars.yf.download", return_value=pd.DataFrame()):
        result = bb.fetch_bars("AAPL", "2026-01-01", "2026-01-02")
    assert result is None


def test_fetch_bars_error():
    """Exception → None."""
    with patch("backfill_bars.yf.download", side_effect=Exception("Network error")):
        result = bb.fetch_bars("AAPL", "2026-01-01", "2026-01-02")
    assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# backfill_ticker — integration with mocked yfinance
# ══════════════════════════════════════════════════════════════════════════════

def test_backfill_ticker_creates_parquet(temp_bars_dir):
    """Full backfill flow creates a valid Parquet with indicators."""
    mock_df = pd.DataFrame({
        "Datetime": [
            pd.Timestamp(f"2026-07-01 {h:02d}:{m:02d}:00", tz="UTC")
            for h in range(9, 16) for m in (0, 30)
        ],
        "Open":  [100.0 + i for i in range(14)],
        "High":  [102.0 + i for i in range(14)],
        "Low":   [99.0 + i for i in range(14)],
        "Close": [101.0 + i for i in range(14)],
        "Volume": [10000 + i * 100 for i in range(14)],
    })

    with patch("backfill_bars.yf.download", return_value=mock_df):
        ticker, status, count = bb.backfill_ticker(
            "SPY", days=5, force=True, verbose=False,
        )

    assert status == "ok"
    assert count == 14

    # Verify Parquet file exists
    path = temp_bars_dir / "SPY.parquet"
    assert path.exists()

    df = pd.read_parquet(path)
    assert len(df) == 14
    expected_cols = {"timestamp", "open", "high", "low", "close", "volume",
                     "rsi_14", "macd", "macd_signal", "macd_hist", "atr_14"}
    for col in expected_cols:
        assert col in df.columns, f"Missing: {col}"

    # Verify types
    assert df["timestamp"].dtype.kind == "M"  # datetime
    assert df["close"].dtype == "float64"
    # RSI should have some non-NaN values since we have exactly 14 rows
    # (last row should have RSI computed)


def test_backfill_ticker_idempotent(temp_bars_dir):
    """Running backfill twice doesn't create duplicates."""
    ts = [
        pd.Timestamp("2026-07-01 14:30:00", tz="UTC"),
        pd.Timestamp("2026-07-01 14:35:00", tz="UTC"),
    ]

    mock_df = pd.DataFrame({
        "Datetime": ts,
        "Open": [100.0, 101.0],
        "High": [102.0, 103.0],
        "Low": [99.0, 100.0],
        "Close": [101.0, 102.0],
        "Volume": [10000, 12000],
    })

    # First run with force=True
    with patch("backfill_bars.yf.download", return_value=mock_df):
        bb.backfill_ticker("AAPL", days=5, force=True, verbose=False)

    # Second run with force=True and same data — should produce 2 unique rows
    with patch("backfill_bars.yf.download", return_value=mock_df):
        ticker, status, count = bb.backfill_ticker(
            "AAPL", days=5, force=True, verbose=False,
        )
        assert status == "ok"

    # Verify no duplicates (merge_and_dedup should have eliminated them)
    df = pd.read_parquet(temp_bars_dir / "AAPL.parquet")
    assert len(df) == 2
    assert df["timestamp"].nunique() == 2


def test_backfill_ticker_check_only(temp_bars_dir, capsys):
    """--check mode prints gaps but doesn't write."""
    with patch("backfill_bars.yf.download") as mock_download:
        ticker, status, count = bb.backfill_ticker(
            "MSFT", days=5, force=False, check_only=True, verbose=True,
        )
        mock_download.assert_not_called()

    assert status in ("ok", "gaps")
    captured = capsys.readouterr()
    # Should mention the ticker
    assert "MSFT" in captured.out or "MSFT" in captured.err


# ══════════════════════════════════════════════════════════════════════════════
# Schema validation
# ══════════════════════════════════════════════════════════════════════════════

def test_parquet_schema_matches_spec(sample_bars_df, temp_bars_dir):
    """The parquet output matches the spec schema."""
    df_with_indicators = bb.compute_indicators(sample_bars_df.copy())
    path = temp_bars_dir / "SCHEMA.parquet"
    bb.atomic_write(df_with_indicators, path)

    df = pd.read_parquet(path)

    # Spec: timestamp (datetime64[ns, UTC])
    assert df["timestamp"].dtype.kind == "M"
    # Check timezone
    ts = df["timestamp"].iloc[0]
    assert ts.tzinfo is not None

    # Spec: open, high, low, close (float64)
    for col in ["open", "high", "low", "close"]:
        assert col in df.columns
        assert df[col].dtype == "float64", f"{col} is {df[col].dtype}"
    # volume: float64 or int64 are both acceptable
    assert "volume" in df.columns
    assert df["volume"].dtype.kind in ("f", "i"), f"volume is {df['volume'].dtype}"

    # Spec: rsi_14, macd, macd_signal, macd_hist, atr_14 (float64)
    for col in ["rsi_14", "macd", "macd_signal", "macd_hist", "atr_14"]:
        assert col in df.columns
        assert df[col].dtype == "float64", f"{col} is {df[col].dtype}"
