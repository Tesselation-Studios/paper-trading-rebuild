"""
TraderLogger — structured JSONL logging per agent, per day.

Provides:
  - TraderLogger: writes trades, reflections, and errors to per-agent daily log files
  - get_trader_logger(): singleton cache by agent_id

Log format: one JSON object per line in logs/{agent_id}/YYYY-MM-DD.jsonl

Usage:
    from src.observability.trader_logger import TraderLogger

    logger = TraderLogger("kairos")
    logger.log_trade("AAPL", "BUY", 100, 185.50, 0.0, {"rsi": 65}, 0.8)
    logger.log_reflection("2026-07-13", 0.55, 142.50, ["tighten stops"])
    logger.log_error("DB_WRITE_FAIL", "connection timeout", "traceback...")
    recent = logger.get_recent_trades(5)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class TraderLogger:
    """Structured JSONL logger for a single trading agent.

    Writes one JSON object per line to logs/{agent_id}/YYYY-MM-DD.jsonl.
    Each file covers one calendar day. Thread-safe writes.
    """

    def __init__(self, agent_id: str, log_dir: str = "logs/") -> None:
        self.agent_id = agent_id
        self._log_dir = Path(log_dir)
        self._agent_dir = self._log_dir / agent_id
        self._agent_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._day: Optional[str] = None
        self._file: Optional[object] = None  # type: ignore[type-arg]

    def _date_str(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _ensure_file(self) -> object:
        """Open the log file for today, creating it if needed."""
        today = self._date_str()
        if self._day != today or self._file is None:
            # Close previous day's file
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
            path = self._agent_dir / f"{today}.jsonl"
            self._file = open(path, "a", encoding="utf-8")
            self._day = today
        return self._file

    def _write(self, entry: Dict[str, Any]) -> None:
        """Thread-safe write of one JSON line."""
        with self._lock:
            f = self._ensure_file()
            f.write(json.dumps(entry, default=str) + "\n")
            f.flush()

    def log_trade(
        self,
        ticker: str,
        action: str,
        qty: int,
        price: float,
        pnl: float,
        signals: Optional[Dict[str, Any]] = None,
        confidence: float = 0.0,
    ) -> None:
        """Log a trade execution event.

        Args:
            ticker: Stock symbol (e.g. "AAPL").
            action: Trade action (e.g. "BUY", "SELL", "CLOSE").
            qty: Number of shares.
            price: Execution price.
            pnl: P&L for this trade (0.0 for opens).
            signals: Dict of signal values (e.g. {"rsi": 65, "momentum": 0.3}).
            confidence: Trader confidence score (0.0 to 1.0).
        """
        entry = {
            "type": "trade",
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": self.agent_id,
            "ticker": ticker.upper(),
            "action": action.upper(),
            "qty": qty,
            "price": price,
            "pnl": pnl,
            "signals": signals or {},
            "confidence": round(confidence, 4),
        }
        self._write(entry)

    def log_reflection(
        self,
        date: str,
        win_rate: float,
        pnl: float,
        suggestions: Optional[List[str]] = None,
    ) -> None:
        """Log an end-of-day reflection.

        Args:
            date: Date string (e.g. "2026-07-13").
            win_rate: Win rate for the period (0.0 to 1.0).
            pnl: P&L for the period.
            suggestions: List of improvement suggestions.
        """
        entry = {
            "type": "reflection",
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": self.agent_id,
            "date": date,
            "win_rate": round(win_rate, 4),
            "pnl": round(pnl, 2),
            "suggestions": suggestions or [],
        }
        self._write(entry)

    def log_error(
        self,
        error_type: str,
        message: str,
        traceback: Optional[str] = None,
    ) -> None:
        """Log an error event.

        Args:
            error_type: Error category (e.g. "DB_WRITE_FAIL", "API_TIMEOUT").
            message: Human-readable error message.
            traceback: Optional stack trace string.
        """
        entry = {
            "type": "error",
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": self.agent_id,
            "error_type": error_type,
            "message": message,
            "traceback": traceback or "",
        }
        self._write(entry)

    def get_recent_trades(self, n: int = 10) -> List[Dict[str, Any]]:
        """Return the last N trade entries from today's log.

        Reads the current day's JSONL file and returns the most recent
        trade-type entries. Only returns trades from the current day.

        Args:
            n: Number of recent trades to return.

        Returns:
            List of trade dicts, most recent first.
        """
        today = self._date_str()
        path = self._agent_dir / f"{today}.jsonl"
        if not path.exists():
            return []

        trades: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "trade":
                        trades.append(entry)
                except json.JSONDecodeError:
                    continue

        # Most recent first
        trades.reverse()
        return trades[:n]

    def close(self) -> None:
        """Close the current log file."""
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
            self._day = None


# ── Singleton cache ──────────────────────────────────────────────────────────

_loggers: Dict[str, TraderLogger] = {}
_loggers_lock = threading.Lock()


def get_trader_logger(agent_id: str, log_dir: str = "logs/") -> TraderLogger:
    """Get or create a TraderLogger for the given agent_id.

    Thread-safe singleton cache.
    """
    with _loggers_lock:
        if agent_id not in _loggers:
            _loggers[agent_id] = TraderLogger(agent_id, log_dir)
        return _loggers[agent_id]