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
    prices = [100.0]
    for i in range(1, 40):
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
    for i in range(7):
        d = today - timedelta(days=i)
        existing.add(d.strftime("%Y-%m-%d"))

    start, end = bb.missing_date_range("TICKER", days=5, existing=existing)
    assert start is None
    assert end is None


def test_missing_date_range_partial():
    """Some dates missing → returns appropriate range."""
    today = date.today()
    existing = {today.strftime("%Y-%m-%d")}

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
    """Early rows have NaN for indicators that need warmup."""
    result = bb.compute_indicators(sample_bars_df.copy())

    assert result["rsi_14"].iloc[0] is pd.NA or pd.isna(result["rsi_14"].iloc[0])
    non_nan = result["rsi_14"].iloc[14:].dropna()
    assert len(non_nan) > 0, f"RSI should have non-NaN values after warmup, got {len(non_nan)}"

    assert result["atr_14"].iloc[0] is pd.NA or pd.isna(result["atr_14"].iloc[0])
    non_nan_atr = result["atr_14"].iloc[14:].dropna()
    assert len(non_nan_atr) > 0, f"ATR should have non-NaN values after warmup, got {len(non_nan_atr)}"

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
    assert result["timestamp"].is_monotonic_increasing


def test_merge_dedup_keeps_newer(temp_bars_dir):
    """When merging, duplicate timestamps keep the newer row."""
    ts = pd.Timestamp("2026-07-01 14:30:00+00:00")

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
    assert result["open"].iloc[0] == 200.0


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
    small = sample_bars_df.iloc[:5].copy()
    bb.atomic_write(small, path)
    assert len(pd.read_parquet(path)) == 5
    bb.atomic_write(sample_bars_df, path)
    assert len(pd.read_parquet(path)) == len(sample_bars_df)


# ══════════════════════════════════════════════════════════════════════════════
# fetch_bars_alpaca — mocked Alpaca client
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_bar():
    bar = MagicMock()
    bar.timestamp = pd.Timestamp("2026-07-01 14:30:00", tz="UTC")
    bar.open = 100.0
    bar.high = 102.0
    bar.low = 99.0
    bar.close = 101.0
    bar.volume = 10000
    return bar


@pytest.fixture
def mock_alpaca_client(mock_bar):
    bar2 = MagicMock()
    bar2.timestamp = pd.Timestamp("2026-07-01 14:35:00", tz="UTC")
    bar2.open = 101.0
    bar2.high = 103.0
    bar2.low = 100.0
    bar2.close = 102.0
    bar2.volume = 12000

    client = MagicMock(spec=bb.StockHistoricalDataClient)
    resp = MagicMock()
    resp.data = {"AAPL": [mock_bar, bar2]}
    client.get_stock_bars.return_value = resp
    return client


@pytest.fixture
def mock_empty_client():
    client = MagicMock(spec=bb.StockHistoricalDataClient)
    resp = MagicMock()
    resp.data = {"AAPL": []}
    client.get_stock_bars.return_value = resp
    return client


@pytest.fixture
def mock_error_client():
    client = MagicMock(spec=bb.StockHistoricalDataClient)
    client.get_stock_bars.side_effect = Exception("Alpaca API error")
    return client


@pytest.fixture
def mock_many_bars():
    bars = []
    for i in range(14):
        h = 9 + (i // 2)
        m = 0 if i % 2 == 0 else 30
        bar = MagicMock()
        bar.timestamp = pd.Timestamp(f"2026-07-01 {h:02d}:{m:02d}:00", tz="UTC")
        bar.open = 100.0 + i
        bar.high = 102.0 + i
        bar.low = 99.0 + i
        bar.close = 101.0 + i
        bar.volume = 10000 + i * 100
        bars.append(bar)
    return bars


@pytest.fixture
def mock_alpaca_client_14bars(mock_many_bars):
    client = MagicMock(spec=bb.StockHistoricalDataClient)
    resp = MagicMock()
    resp.data = {"SPY": mock_many_bars}
    client.get_stock_bars.return_value = resp
    return client


def test_fetch_bars_alpaca_returns_dataframe(mock_alpaca_client):
    """fetch_bars_alpaca returns proper DataFrame."""
    result = bb.fetch_bars_alpaca(mock_alpaca_client, "AAPL", "2026-07-01", "2026-07-02")

    assert result is not None
    assert len(result) == 2
    assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert result["timestamp"].dt.tz is not None
    assert result["close"].iloc[0] == 101.0
    assert result["close"].iloc[1] == 102.0


def test_fetch_bars_alpaca_empty(mock_empty_client):
    """Empty data returns None."""
    result = bb.fetch_bars_alpaca(mock_empty_client, "AAPL", "2026-01-01", "2026-01-02")
    assert result is None


def test_fetch_bars_alpaca_missing_ticker():
    """Ticker not in response returns None."""
    client = MagicMock(spec=bb.StockHistoricalDataClient)
    resp = MagicMock()
    resp.data = {}
    client.get_stock_bars.return_value = resp
    result = bb.fetch_bars_alpaca(client, "AAPL", "2026-01-01", "2026-01-02")
    assert result is None


def test_fetch_bars_alpaca_error(mock_error_client):
    """Exception returns None."""
    result = bb.fetch_bars_alpaca(mock_error_client, "AAPL", "2026-01-01", "2026-01-02")
    assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# backfill_ticker — integration with mocked Alpaca client
# ══════════════════════════════════════════════════════════════════════════════

def test_backfill_ticker_creates_parquet(temp_bars_dir, mock_alpaca_client_14bars):
    """Full backfill flow creates a valid Parquet with indicators."""
    ticker, status, count = bb.backfill_ticker(
        mock_alpaca_client_14bars, "SPY", days=5, force=True, verbose=False,
    )

    assert status == "ok"
    assert count == 14

    path = temp_bars_dir / "SPY.parquet"
    assert path.exists()

    df = pd.read_parquet(path)
    assert len(df) == 14
    expected_cols = {"timestamp", "open", "high", "low", "close", "volume",
                     "rsi_14", "macd", "macd_signal", "macd_hist", "atr_14"}
    for col in expected_cols:
        assert col in df.columns, f"Missing: {col}"

    assert df["timestamp"].dtype.kind == "M"
    assert df["close"].dtype == "float64"


def test_backfill_ticker_idempotent(temp_bars_dir):
    """Running backfill twice doesn't create duplicates."""
    bar1 = MagicMock()
    bar1.timestamp = pd.Timestamp("2026-07-01 14:30:00", tz="UTC")
    bar1.open = 100.0
    bar1.high = 102.0
    bar1.low = 99.0
    bar1.close = 101.0
    bar1.volume = 10000

    bar2 = MagicMock()
    bar2.timestamp = pd.Timestamp("2026-07-01 14:35:00", tz="UTC")
    bar2.open = 101.0
    bar2.high = 103.0
    bar2.low = 100.0
    bar2.close = 102.0
    bar2.volume = 12000

    client = MagicMock(spec=bb.StockHistoricalDataClient)
    resp = MagicMock()
    resp.data = {"AAPL": [bar1, bar2]}
    client.get_stock_bars.return_value = resp

    bb.backfill_ticker(client, "AAPL", days=5, force=True, verbose=False)
    ticker, status, count = bb.backfill_ticker(
        client, "AAPL", days=5, force=True, verbose=False,
    )
    assert status == "ok"

    df = pd.read_parquet(temp_bars_dir / "AAPL.parquet")
    assert len(df) == 2
    assert df["timestamp"].nunique() == 2


def test_backfill_ticker_check_only(temp_bars_dir, capsys):
    """--check mode prints gaps but doesn't call get_stock_bars."""
    client = MagicMock(spec=bb.StockHistoricalDataClient)
    ticker, status, count = bb.backfill_ticker(
        client, "MSFT", days=5, force=False, check_only=True, verbose=True,
    )
    client.get_stock_bars.assert_not_called()

    assert status in ("ok", "gaps")
    captured = capsys.readouterr()
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

    assert df["timestamp"].dtype.kind == "M"
    ts = df["timestamp"].iloc[0]
    assert ts.tzinfo is not None

    for col in ["open", "high", "low", "close"]:
        assert col in df.columns
        assert df[col].dtype == "float64", f"{col} is {df[col].dtype}"
    assert "volume" in df.columns
    assert df["volume"].dtype.kind in ("f", "i"), f"volume is {df['volume'].dtype}"

    for col in ["rsi_14", "macd", "macd_signal", "macd_hist", "atr_14"]:
        assert col in df.columns
        assert df[col].dtype == "float64", f"{col} is {df[col].dtype}"