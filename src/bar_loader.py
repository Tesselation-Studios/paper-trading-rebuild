"""
BarLoader — Parquet → Tick bridge for the nightly optimization pipeline.

Loads historical OHLCV bars from Parquet files and converts them
to the Tick format used by the replay harness (replay.Tick).
Can also load from the data bus HTTP API (/bars endpoint) when
remote_mode is enabled.

Usage:
    from src.bar_loader import BarLoader

    bl = BarLoader()
    ticks = bl.load_date_range(["SPY", "AAPL"], "2026-06-30", "2026-07-02")
    dates = bl.available_dates("SPY")
    missing = bl.missing_dates(["SPY", "AAPL"], "2026-06-20", "2026-07-05")
    count = bl.to_sqlite_cache(["SPY", "AAPL"], "2026-06-30", "2026-07-02")
    
    # Remote mode (data bus API):
    bl_remote = BarLoader(data_bus_url="http://192.168.1.41:5000")
    ticks = bl_remote.load_date_range(["SPY", "AAPL"], "2026-06-30", "2026-07-02")
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.replay import Tick

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

log = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
_BARS_CANDIDATES = [
    PROJECT_DIR / "shared" / "cache" / "bars",
    PROJECT_DIR / "shared" / "cache" / "daily_bars",
    PROJECT_DIR / "data" / "bars",
    Path.home() / ".openclaw" / "workspace-coder" / "paper-trading-teams" / "shared" / "cache" / "bars",
    Path.home() / ".openclaw" / "workspace-coder" / "paper-trading-teams" / "shared" / "cache" / "daily_bars",
]
_DB_CANDIDATES = [
    PROJECT_DIR / "shared" / "trader.db",
    PROJECT_DIR / "trader.db",
]


def _first_existing(paths: List[Path]) -> Path:
    """Return the first path that exists, falling back to the first one.

    Gracefully handles PermissionError on broken symlinks or inaccessible dirs.
    """
    for p in paths:
        try:
            if p.is_dir() or p.exists():
                return p
        except PermissionError:
            continue
    # Return first candidate even if it doesn't exist yet
    return paths[0]


DEFAULT_BARS_DIR: Path = _first_existing(_BARS_CANDIDATES)
DEFAULT_DB_PATH: Path = _first_existing(_DB_CANDIDATES)
DEFAULT_DATA_BUS_URL: str = "http://192.168.1.41:5000"


# ── BarLoader ────────────────────────────────────────────────────────────────


class BarLoader:
    """Load OHLCV bars from Parquet store, output Ticks for replay.

    Supports two modes:
      1. Local parquet (default): loads from bars_dir/*.parquet
      2. Remote data bus: loads from the data bus HTTP API /bars endpoint

    To use remote mode, pass data_bus_url or set the DATA_BUS_URL env var:
        bl = BarLoader(data_bus_url="http://192.168.1.41:5000")

    In remote mode, the load_date_range() method fetches bars from the
    data bus API and converts them to Tick objects, just like local mode.

    Args:
        bars_dir: Directory containing <ticker>.parquet files (local mode only).
        db_path: SQLite database for caching replay ticks.
        data_bus_url: Data bus URL for remote mode. If set, uses HTTP API
            instead of local parquet files. Reads from env DATA_BUS_URL if
            not explicitly provided.
    """

    def __init__(
        self,
        bars_dir: Optional[Path] = None,
        db_path: Optional[Path] = None,
        data_bus_url: Optional[str] = None,
    ):
        self.bars_dir = Path(bars_dir) if bars_dir else DEFAULT_BARS_DIR
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.data_bus_url = data_bus_url or os.environ.get("DATA_BUS_URL", "")
        self._remote_mode = bool(self.data_bus_url)
        if self._remote_mode:
            log.info("BarLoader in remote mode: data_bus=%s", self.data_bus_url)

    # ── Public API ────────────────────────────────────────────────────────

    def load_date_range(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
        interval_minutes: int = 30,
    ) -> List[Tick]:
        """Load ticks for a date range across multiple tickers.

        In remote mode, fetches from the data bus /bars endpoint.
        In local mode, loads from parquet files.

        Args:
            tickers: List of ticker symbols.
            start_date: ISO date string (e.g. "2026-06-30").
            end_date: ISO date string (inclusive).
            interval_minutes: Downsample to this interval (e.g. 30 = 30-min bars).

        Returns:
            Chronological list of Tick objects, sorted by timestamp.
        """
        if self._remote_mode:
            return self._load_from_databus(tickers, start_date, end_date, interval_minutes)

        return self._load_from_parquet(tickers, start_date, end_date, interval_minutes)

    def available_dates(self, ticker: str) -> List[str]:
        """Which dates have bars for this ticker?

        Note: In remote mode, this is not supported (returns empty list).
        Use the /bars endpoint directly for date discovery.

        Args:
            ticker: Stock symbol.

        Returns:
            Sorted list of ISO date strings.
        """
        if self._remote_mode:
            log.warning("available_dates() not supported in remote mode")
            return []

        df = self._read_parquet(ticker)
        if df is None or df.empty:
            return []

        dates = df["timestamp"].dt.date.unique()
        return sorted(d.isoformat() for d in dates)

    def missing_dates(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
    ) -> List[Tuple[str, str]]:
        """Return (ticker, date) pairs that need backfilling.

        Args:
            tickers: List of ticker symbols to check.
            start_date: ISO date string.
            end_date: ISO date string (inclusive).

        Returns:
            List of (ticker, date_string) pairs for dates without data.
        """
        if self._remote_mode:
            log.warning("missing_dates() not supported in remote mode")
            return []

        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        # Generate the list of expected trading dates (Mon-Fri)
        expected_dates = [
            d
            for d in _date_range(start, end)
            if d.weekday() < 5  # Mon-Fri only
        ]
        missing: List[Tuple[str, str]] = []

        for ticker in tickers:
            available = set(self.available_dates(ticker))
            for d in expected_dates:
                if d.isoformat() not in available:
                    missing.append((ticker, d.isoformat()))

        return missing

    def to_sqlite_cache(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
        interval_minutes: int = 30,
    ) -> int:
        """Pre-load bars into SQLite for faster repeated queries.

        Creates (or recreates) a ``replay_ticks`` table in the database.

        Args:
            tickers: List of ticker symbols.
            start_date: ISO date string.
            end_date: ISO date string (inclusive).
            interval_minutes: Downsample interval (default 30).

        Returns:
            Number of rows inserted.
        """
        ticks = self.load_date_range(tickers, start_date, end_date, interval_minutes=interval_minutes)

        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("DROP TABLE IF EXISTS replay_ticks")
            conn.execute(
                """
                CREATE TABLE replay_ticks (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp  TEXT    NOT NULL,
                    ticker     TEXT    NOT NULL,
                    open       REAL    NOT NULL,
                    high       REAL    NOT NULL,
                    low        REAL    NOT NULL,
                    close      REAL    NOT NULL,
                    volume     INTEGER NOT NULL DEFAULT 0,
                    rsi        REAL,
                    momentum   REAL,
                    volatility REAL,
                    regime     TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_replay_ticks_ts "
                "ON replay_ticks(timestamp, ticker)"
            )

            rows = [
                (
                    t.timestamp.isoformat(),
                    t.ticker,
                    t.open,
                    t.high,
                    t.low,
                    t.close,
                    t.volume,
                    t.rsi,
                    t.momentum,
                    t.volatility,
                    t.regime,
                )
                for t in ticks
            ]

            conn.executemany(
                """
                INSERT INTO replay_ticks
                    (timestamp, ticker, open, high, low, close, volume,
                     rsi, momentum, volatility, regime)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    def load_from_cache(
        self,
        tickers: Optional[List[str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Tick]:
        """Load ticks from the SQLite cache instead of Parquet.

        Args:
            tickers: Filter by tickers (None = all).
            start_date: ISO date string filter.
            end_date: ISO date string filter (inclusive).

        Returns:
            List of Tick objects.
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            # Check if table exists
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='replay_ticks'"
            ).fetchone()
            if not table_check:
                return []

            query = "SELECT timestamp, ticker, open, high, low, close, volume, rsi, momentum, volatility, regime FROM replay_ticks WHERE 1=1"
            params: list = []

            if tickers:
                placeholders = ",".join(["?"] * len(tickers))
                query += f" AND ticker IN ({placeholders})"
                params.extend(tickers)

            if start_date:
                query += " AND timestamp >= ?"
                params.append(start_date)
            if end_date:
                query += " AND timestamp <= ?"
                # Make end_date inclusive for the full day
                params.append(end_date + "T23:59:59")

            query += " ORDER BY timestamp ASC"

            rows = conn.execute(query, params).fetchall()
            ticks: List[Tick] = []
            for row in rows:
                ticks.append(
                    Tick(
                        timestamp=datetime.fromisoformat(row[0]),
                        ticker=row[1],
                        open=row[2],
                        high=row[3],
                        low=row[4],
                        close=row[5],
                        volume=row[6],
                        rsi=row[7],
                        momentum=row[8],
                        volatility=row[9],
                        regime=row[10],
                    )
                )
            return ticks
        finally:
            conn.close()

    # ── Remote mode (data bus HTTP API) ───────────────────────────────────

    def _load_from_databus(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
        interval_minutes: int = 30,
    ) -> List[Tick]:
        """Load ticks from the data bus HTTP API instead of parquet files.

        Fetches from DATA_BUS_URL/bars endpoint, converts to Tick objects.
        Falls back to intraday bars when interval_minutes < 24*60, daily otherwise.
        """
        if not _HAS_REQUESTS:
            log.error("requests module not available for data bus remote mode")
            return []

        # Determine interval type
        interval = "intraday" if interval_minutes < 24 * 60 else "daily"

        params = {
            "symbols": ",".join(tickers),
            "interval": interval,
        }
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        try:
            resp = requests.get(
                f"{self.data_bus_url}/bars",
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("Data bus /bars request failed: %s — falling back to parquet", e)
            return self._load_from_parquet(tickers, start_date, end_date, interval_minutes)

        symbols_data = data.get("symbols", {})
        if not symbols_data:
            log.warning("Data bus returned no symbols for %s", tickers)
            return []

        all_ticks: List[Tick] = []
        for ticker, bars in symbols_data.items():
            for bar in bars:
                ts_str = bar.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str)
                except (ValueError, TypeError):
                    continue

                all_ticks.append(Tick(
                    timestamp=ts,
                    ticker=ticker,
                    open=float(bar.get("open", 0)),
                    high=float(bar.get("high", 0)),
                    low=float(bar.get("low", 0)),
                    close=float(bar.get("close", 0)),
                    volume=int(bar.get("volume", 0)),
                ))

        # Sort chronologically across tickers
        all_ticks.sort(key=lambda t: t.timestamp)
        log.info("Loaded %d ticks from data bus (%s, %s-%s, interval=%s)",
                 len(all_ticks), tickers, start_date, end_date, interval)
        return all_ticks

    # ── Internal helpers ───────────────────────────────────────────────────

    def _load_from_parquet(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
        interval_minutes: int = 30,
    ) -> List[Tick]:
        """Load ticks from local parquet files (original implementation)."""
        start_dt = pd.Timestamp(start_date, tz="UTC")
        end_dt = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)  # inclusive

        all_ticks: List[Tick] = []

        for ticker in tickers:
            df = self._read_parquet(ticker)
            if df is None or df.empty:
                log.debug("No data for %s — skipping", ticker)
                continue

            # Filter to date range
            mask = (df["timestamp"] >= start_dt) & (df["timestamp"] < end_dt)
            df = df[mask].copy()
            if df.empty:
                log.debug("No data for %s in [%s, %s]", ticker, start_date, end_date)
                continue

            # Downsample if interval_minutes > native resolution
            if interval_minutes and interval_minutes > 1:
                df = self._downsample(df, interval_minutes)

            # Convert to Tick objects
            for _, row in df.iterrows():
                tick = self._row_to_tick(ticker, row)
                if tick is not None:
                    all_ticks.append(tick)

        # Sort chronologically across tickers
        all_ticks.sort(key=lambda t: t.timestamp)
        return all_ticks

    def _read_parquet(self, ticker: str) -> Optional[pd.DataFrame]:
        """Read a ticker's Parquet file, returning None if it doesn't exist."""
        path = self.bars_dir / f"{ticker}.parquet"
        if not path.exists():
            return None
        try:
            return pd.read_parquet(path)
        except Exception as e:
            log.warning("Failed to read %s: %s", path, e)
            return None

    def _downsample(
        self, df: pd.DataFrame, interval_minutes: int
    ) -> pd.DataFrame:
        """Resample OHLCV data to a coarser interval.

        Standard OHLCV aggregation: open=first, high=max, low=min, close=last,
        volume=sum. Optional indicator columns take the last value in the bucket.
        """
        if "timestamp" not in df.columns:
            return df

        df = df.copy()
        df = df.set_index("timestamp")

        freq = f"{interval_minutes}min"

        ohlcv_agg: Dict[str, str] = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }

        # Collect all columns present
        ohlcv_cols = [c for c in ohlcv_agg if c in df.columns]
        indicator_cols = [c for c in df.columns if c not in ohlcv_agg]

        # Aggregate OHLCV
        result = df[ohlcv_cols].resample(freq).agg(
            {c: ohlcv_agg[c] for c in ohlcv_cols}
        )

        # For indicator columns, take the last (latest known value in the bucket)
        for col in indicator_cols:
            result[col] = df[col].resample(freq).last()

        result = result.dropna(subset=["open"])  # remove empty buckets
        result = result.reset_index()
        return result

    def _row_to_tick(self, ticker: str, row: pd.Series) -> Optional[Tick]:
        """Convert a single DataFrame row to a Tick dataclass.

        Maps Parquet columns to Tick fields:
          - Direct: open, high, low, close, volume
          - Optional indicators: rsi_14 → rsi, atr_14 → volatility, etc.
        """
        ts = row.get("timestamp")
        if ts is None or pd.isna(ts):
            return None

        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()
        elif isinstance(ts, np.datetime64):
            ts = ts.astype("datetime64[us]").item()

        return Tick(
            timestamp=ts,
            ticker=ticker,
            open=float(row.get("open", 0.0)),
            high=float(row.get("high", 0.0)),
            low=float(row.get("low", 0.0)),
            close=float(row.get("close", 0.0)),
            volume=int(row.get("volume", 0)),
            # Map optional indicator columns to Tick fields
            rsi=self._safe_float(row.get("rsi_14", row.get("rsi"))),
            momentum=self._safe_float(row.get("macd_hist", row.get("momentum"))),
            volatility=self._safe_float(row.get("atr_14", row.get("volatility"))),
            regime=self._safe_str(row.get("regime")),
        )

    @staticmethod
    def _safe_float(val) -> Optional[float]:
        """Return float or None for nan/null values."""
        if val is None:
            return None
        try:
            f = float(val)
            if np.isnan(f):
                return None
            return f
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _safe_str(val) -> Optional[str]:
        """Return string or None for nan/null values."""
        if val is None:
            return None
        try:
            if isinstance(val, float) and np.isnan(val):
                return None
            return str(val)
        except (ValueError, TypeError):
            return None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _date_range(start: date, end: date) -> List[date]:
    """Generate a list of dates from start to end inclusive."""
    days = (end - start).days
    return [start + pd.Timedelta(days=i).to_pytimedelta() for i in range(days + 1)]