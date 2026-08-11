"""Tests for src/data_bus.py's DbWriteQueue.flush() -- specifically the
heterogeneous-row-shape column bug (2026-08-11): rows of genuinely different
shapes (full stock quotes vs. bar-only vs. minimal crypto rows) land in the
same "prices" table flush batch every 15s, and the INSERT's column list used
to come from rows[0].keys() only -- whichever row happened to sort first
silently dropped every other row's extra columns, no error, no log.

Uses the real shared/cache.db schema (via _ensure_cache_tables), pointed at
a tmp_path copy so this never touches the live cache.db."""
import shutil
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

import data_bus  # noqa: E402


@pytest.fixture
def isolated_queue(tmp_path, monkeypatch):
    """Points data_bus.SHARED_DIR at a scratch dir so DbWriteQueue.flush()
    creates/writes a throwaway cache.db, never the real one, and stubs out
    the Postgres mirror (dual_writer.write) so this stays a pure-sqlite,
    no-network test."""
    scratch = tmp_path / "shared"
    scratch.mkdir()
    monkeypatch.setattr(data_bus, "SHARED_DIR", scratch)
    monkeypatch.setattr(data_bus.dual_writer, "write", lambda table, row: None)
    return data_bus.DbWriteQueue()


class TestFlushHeterogeneousRows:
    def test_narrow_row_first_does_not_drop_wide_row_columns(self, isolated_queue):
        # Narrow (crypto-shaped) row enqueued first, wide (full stock-quote
        # shaped) row second -- this ordering is exactly what used to trigger
        # the bug (columns taken from rows[0] = the narrow row).
        isolated_queue.enqueue("prices", {"ticker": "BTC/USD", "close": 64000.0, "fetched_at": "2026-08-11T00:00:00"})
        isolated_queue.enqueue("prices", {
            "ticker": "AAPL", "close": 230.5, "high": 231.0, "low": 229.0, "open": 230.0,
            "volume": 1000, "rsi": 55.2, "macd_line": 0.5, "macd_signal": 0.3,
            "macd_histogram": 0.2, "ma20": 228.0, "fetched_at": "2026-08-11T00:00:01",
        })
        isolated_queue.flush()

        conn = data_bus._get_cache_db_connection(readonly=False)
        rows = {r["ticker"]: dict(r) for r in conn.execute("SELECT * FROM prices").fetchall()}
        conn.close()

        assert rows["BTC/USD"]["close"] == 64000.0
        aapl = rows["AAPL"]
        assert aapl["high"] == 231.0
        assert aapl["rsi"] == pytest.approx(55.2)
        assert aapl["macd_histogram"] == pytest.approx(0.2)
        assert aapl["ma20"] == pytest.approx(228.0)

    def test_wide_row_first_still_does_not_drop_its_own_columns(self, isolated_queue):
        # Sanity check the other ordering too -- this direction happened to
        # work before the fix (rows[0] was already the wide row), confirming
        # the fix doesn't regress the case that was already fine.
        isolated_queue.enqueue("prices", {
            "ticker": "MSFT", "close": 410.0, "high": 412.0, "low": 408.0, "open": 409.0,
            "volume": 2000, "rsi": 60.1, "macd_line": 1.1, "macd_signal": 0.9,
            "macd_histogram": 0.2, "ma20": 405.0, "fetched_at": "2026-08-11T00:00:02",
        })
        isolated_queue.enqueue("prices", {"ticker": "ETH/USD", "close": 3200.0, "fetched_at": "2026-08-11T00:00:03"})
        isolated_queue.flush()

        conn = data_bus._get_cache_db_connection(readonly=False)
        rows = {r["ticker"]: dict(r) for r in conn.execute("SELECT * FROM prices").fetchall()}
        conn.close()

        assert rows["MSFT"]["rsi"] == pytest.approx(60.1)
        assert rows["ETH/USD"]["close"] == 3200.0
