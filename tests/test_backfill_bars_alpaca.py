"""Tests for scripts/backfill_bars_alpaca.py.

This is the module actually wired into Stan's live daily bars-sync
(workspace-trader-stonks/scripts/sync_historical_bars.py -> stonks-bars-sync
cron), unlike the yfinance-based backfill_bars.py covered by
test_backfill_bars.py. It had zero test coverage before this file, which is
exactly how a real bug shipped and ran silently for days: fetch_bars_alpaca's
`end` date was parsed to midnight of the end date (the start of that day,
before market open), so the Alpaca request window excluded the ENTIRE end
date's trading session. Every ticker -- including SPY -- returned zero bars,
and main()'s error counting never treated "empty"/"invalid" statuses as
failures, so the cron kept exiting 0 and reporting "ok" the whole time.
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

if "pandas_ta" in sys.modules and isinstance(sys.modules["pandas_ta"], MagicMock):
    del sys.modules["pandas_ta"]

import backfill_bars_alpaca as bba


@pytest.fixture(autouse=True)
def _real_alpaca_data_historical():
    """test_leaderboard_api.py replaces sys.modules["alpaca"] with a bare
    MagicMock at MODULE level (no fixture, no teardown) to stub
    alpaca.trading.client for its own tests. Since pytest imports every test
    file during collection regardless of which tests get selected, that
    stub permanently shadows the real alpaca-py package for the rest of the
    session -- `from alpaca.data.historical import ...` then fails with
    "'alpaca' is not a package" for any test file collected afterward,
    independent of test order or selection. Save/restore around each test
    here so fetch_bars_alpaca's local import resolves to the real package."""
    saved = {k: v for k, v in sys.modules.items()
             if k == "alpaca" or k.startswith("alpaca.")}
    for k in list(saved):
        del sys.modules[k]
    import alpaca.data.historical  # noqa: F401 -- real import, repopulates sys.modules
    yield
    for k in [k for k in sys.modules if k == "alpaca" or k.startswith("alpaca.")]:
        del sys.modules[k]
    sys.modules.update(saved)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def temp_bars_dir():
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        orig = bba.BARS_DIR
        bba.BARS_DIR = Path(tmpdir)
        yield Path(tmpdir)
        bba.BARS_DIR = orig


def _mock_bar(ts, close=100.0):
    bar = MagicMock()
    bar.timestamp = ts
    bar.open = close - 0.1
    bar.high = close + 0.3
    bar.low = close - 0.3
    bar.close = close
    bar.volume = 10000.0
    return bar


def _install_mock_client(monkeypatch, bars_by_ticker):
    """Patch alpaca.data.historical.StockHistoricalDataClient (imported
    locally inside fetch_bars_alpaca) and capture the StockBarsRequest it
    receives, so tests can assert on the actual request window sent to
    Alpaca -- not just the string args passed into fetch_bars_alpaca."""
    captured_requests = []

    mock_response = MagicMock()
    mock_response.data = bars_by_ticker

    def fake_get_stock_bars(request_params):
        captured_requests.append(request_params)
        return mock_response

    mock_client_instance = MagicMock()
    mock_client_instance.get_stock_bars.side_effect = fake_get_stock_bars

    mock_client_cls = MagicMock(return_value=mock_client_instance)
    monkeypatch.setattr(
        "alpaca.data.historical.StockHistoricalDataClient", mock_client_cls
    )
    return captured_requests


# ══════════════════════════════════════════════════════════════════════════════
# fetch_bars_alpaca — end-date boundary regression (the actual bug)
# ══════════════════════════════════════════════════════════════════════════════

def test_fetch_bars_alpaca_end_date_fully_included(monkeypatch):
    """Regression test for the 2026-07-28 incident: requesting through
    end="2026-07-27" must cover all of 2026-07-27's trading session, not
    stop at that date's midnight. The request's `end` timestamp sent to
    Alpaca must be at or after the start of the NEXT calendar day."""
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    captured = _install_mock_client(monkeypatch, {"SPY": [_mock_bar(
        pd.Timestamp("2026-07-27 15:55:00", tz="America/New_York")
    )]})

    result = bba.fetch_bars_alpaca("SPY", "2026-07-25", "2026-07-27")

    assert result is not None
    assert len(captured) == 1
    end_sent = pd.Timestamp(captured[0].end)
    boundary = pd.Timestamp("2026-07-28", tz="America/New_York")
    assert end_sent >= boundary, (
        f"end={end_sent} does not cover all of 2026-07-27's trading session "
        f"(needs to be >= start of 2026-07-28)"
    )


def test_fetch_bars_alpaca_start_unchanged(monkeypatch):
    """The start boundary was never buggy -- only end needed the fix."""
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    captured = _install_mock_client(monkeypatch, {"SPY": [_mock_bar(
        pd.Timestamp("2026-07-25 10:00:00", tz="America/New_York")
    )]})

    bba.fetch_bars_alpaca("SPY", "2026-07-25", "2026-07-27")

    start_sent = pd.Timestamp(captured[0].start)
    assert start_sent == pd.Timestamp("2026-07-25", tz="America/New_York")


# ══════════════════════════════════════════════════════════════════════════════
# fetch_bars_alpaca — general behavior
# ══════════════════════════════════════════════════════════════════════════════

def test_fetch_bars_alpaca_returns_dataframe(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    ts1 = pd.Timestamp("2026-07-27 09:30:00", tz="America/New_York")
    ts2 = pd.Timestamp("2026-07-27 09:35:00", tz="America/New_York")
    _install_mock_client(monkeypatch, {
        "SPY": [_mock_bar(ts1, 500.0), _mock_bar(ts2, 501.0)],
    })

    result = bba.fetch_bars_alpaca("SPY", "2026-07-27", "2026-07-27")

    assert result is not None
    assert len(result) == 2
    assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert result["timestamp"].dt.tz is not None
    assert result["timestamp"].is_monotonic_increasing


def test_fetch_bars_alpaca_empty_returns_none(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    _install_mock_client(monkeypatch, {"SPY": []})
    result = bba.fetch_bars_alpaca("SPY", "2026-07-27", "2026-07-27")
    assert result is None


def test_fetch_bars_alpaca_missing_ticker_in_response_returns_none(monkeypatch):
    """bars.data has no entry at all for the requested ticker."""
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    _install_mock_client(monkeypatch, {})
    result = bba.fetch_bars_alpaca("SPY", "2026-07-27", "2026-07-27")
    assert result is None


def test_fetch_bars_alpaca_missing_credentials(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    result = bba.fetch_bars_alpaca("SPY", "2026-07-27", "2026-07-27")
    assert result is None


def test_fetch_bars_alpaca_client_exception_returns_none(monkeypatch):
    mock_client_cls = MagicMock(side_effect=RuntimeError("network error"))
    monkeypatch.setattr(
        "alpaca.data.historical.StockHistoricalDataClient", mock_client_cls
    )
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")

    result = bba.fetch_bars_alpaca("SPY", "2026-07-27", "2026-07-27")
    assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# backfill_ticker — integration through the real end-to-end path
# ══════════════════════════════════════════════════════════════════════════════

def test_backfill_ticker_end_to_end_with_realistic_window(monkeypatch, temp_bars_dir):
    """Full backfill_ticker() flow with a realistic multi-day window (the
    same shape missing_date_range() actually produces live) must yield real
    bars, not the silent zero-bars regression."""
    today = date.today()
    # Mirror missing_date_range's own end_date choice so this test tracks
    # real behavior regardless of what day it happens to run.
    end_date = today - timedelta(days=3 if today.weekday() == 0 else 1)
    bars_ts = pd.Timestamp(
        f"{end_date.isoformat()} 15:55:00", tz="America/New_York"
    )
    _install_mock_client(monkeypatch, {
        "SPY": [_mock_bar(bars_ts + pd.Timedelta(minutes=5 * i), 500.0 + i)
                for i in range(40)],
    })
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")

    ticker, status, count = bba.backfill_ticker("SPY", days=5, verbose=True)

    assert status == "ok"
    assert count == 40
    path = temp_bars_dir / "SPY.parquet"
    assert path.exists()


def test_backfill_ticker_empty_response_reports_empty_not_ok(monkeypatch, temp_bars_dir):
    _install_mock_client(monkeypatch, {"SPY": []})
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")

    ticker, status, count = bba.backfill_ticker("SPY", days=5, verbose=False)

    assert status == "empty"
    assert count == 0
    assert not (temp_bars_dir / "SPY.parquet").exists()


# ══════════════════════════════════════════════════════════════════════════════
# main() — exit-code / alerting regression
# ══════════════════════════════════════════════════════════════════════════════

def test_main_exits_nonzero_when_all_tickers_empty(monkeypatch, temp_bars_dir):
    """Regression test for the 2026-07-28 incident: main() must not exit 0
    when every attempted ticker came back empty -- that's exactly the
    silent-failure shape the cron reported as "ok" for days."""
    monkeypatch.setattr(sys, "argv", ["backfill_bars_alpaca.py", "--tickers", "SPY,AAPL", "--days", "2"])
    monkeypatch.setattr(bba.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        bba, "backfill_ticker",
        lambda ticker, days, **kw: (ticker, "empty", 0),
    )

    assert bba.main() == 1


def test_main_exits_zero_when_some_tickers_fetch_real_bars(monkeypatch, temp_bars_dir):
    monkeypatch.setattr(sys, "argv", ["backfill_bars_alpaca.py", "--tickers", "SPY,AAPL", "--days", "2"])
    monkeypatch.setattr(bba.time, "sleep", lambda *_: None)

    def fake_backfill(ticker, days, **kw):
        return (ticker, "ok", 40) if ticker == "SPY" else (ticker, "empty", 0)

    monkeypatch.setattr(bba, "backfill_ticker", fake_backfill)

    assert bba.main() == 0


def test_main_exits_zero_when_all_skipped(monkeypatch, temp_bars_dir):
    """Already-covered dates -> nothing to fetch is a legitimate no-op, not
    a failure -- must not be conflated with the all-empty failure case."""
    monkeypatch.setattr(sys, "argv", ["backfill_bars_alpaca.py", "--tickers", "SPY,AAPL", "--days", "2"])
    monkeypatch.setattr(bba.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        bba, "backfill_ticker",
        lambda ticker, days, **kw: (ticker, "skipped", 0),
    )

    assert bba.main() == 0


def test_main_exits_zero_when_all_tickers_quality_rejected(monkeypatch, temp_bars_dir):
    """Regression test for the 2026-08-01 false-positive: every non-skipped
    ticker legitimately failing the data-quality gate (thin small-caps, no
    real bars returned) is NOT the same failure shape as the 2026-07-28
    incident (Alpaca returning nothing at all) -- it must not trip the same
    all-zero safety net. Confirmed live: 21/21 attempted tickers "invalid",
    0 "empty", which previously exited 1 despite the fetch pipeline working
    correctly and simply having nothing quality-passing to report today."""
    monkeypatch.setattr(sys, "argv", ["backfill_bars_alpaca.py", "--tickers", "SPY,AAPL", "--days", "2"])
    monkeypatch.setattr(bba.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        bba, "backfill_ticker",
        lambda ticker, days, **kw: (ticker, "invalid", 0),
    )

    assert bba.main() == 0


def test_main_exits_nonzero_when_mixed_empty_and_invalid_but_zero_fetched(monkeypatch, temp_bars_dir):
    """A run with even one genuine "empty" (API returned nothing) result and
    zero real bars fetched overall must still trip the guard, regardless of
    how many other tickers were separately (and legitimately) quality-
    rejected -- "empty" is still the real upstream-break signature."""
    monkeypatch.setattr(sys, "argv", ["backfill_bars_alpaca.py", "--tickers", "SPY,AAPL,TSLA", "--days", "2"])
    monkeypatch.setattr(bba.time, "sleep", lambda *_: None)

    def fake_backfill(ticker, days, **kw):
        return (ticker, "empty", 0) if ticker == "SPY" else (ticker, "invalid", 0)

    monkeypatch.setattr(bba, "backfill_ticker", fake_backfill)

    assert bba.main() == 1
