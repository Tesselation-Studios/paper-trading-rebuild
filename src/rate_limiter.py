"""Simple JSON-backed daily rate-limit counter.

Not for real datasets — this is a single small counter per source, resets
at UTC midnight. If a source ever needs more than that (per-minute windows,
multiple limit tiers), it's still small enough to extend here; it doesn't
need a database. For actual cached data (fundamentals, macro, ...) see the
SQLite cache in data_bus.py instead — this module only tracks call counts.

Usage:
    from rate_limiter import check_and_increment
    allowed, remaining = check_and_increment("alpha_vantage", limit=25)
    if not allowed:
        return {"error": "rate_limited", "resets_at": "<next UTC midnight>"}
"""
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

log = logging.getLogger("rate_limiter")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATE_PATH = _PROJECT_ROOT / "shared" / "rate_limits.json"
_LOCK = threading.Lock()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("rate_limits.json unreadable (%s), starting fresh", e)
        return {}


def _save(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def check_and_increment(source: str, limit: int) -> Tuple[bool, int]:
    """Increment today's call count for `source` and report whether it's
    still under `limit`. Always increments (even a call about to be
    rejected still "counts" as an attempt) — callers should check
    `allowed` BEFORE making the real API call, not after.

    Returns (allowed, remaining_after_this_call)."""
    with _LOCK:
        state = _load()
        today = _today_utc()
        entry = state.get(source)
        if not entry or entry.get("date") != today:
            entry = {"date": today, "count": 0, "limit": limit}
        entry["count"] += 1
        entry["limit"] = limit
        state[source] = entry
        _save(state)

        remaining = max(0, limit - entry["count"])
        allowed = entry["count"] <= limit
        return allowed, remaining


def resets_at() -> str:
    """ISO timestamp of the next UTC midnight (when counters reset)."""
    now = datetime.now(timezone.utc)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    tomorrow += timedelta(days=1)
    return tomorrow.isoformat()


def status(source: str) -> dict:
    """Read-only peek at today's count for `source`, without incrementing."""
    state = _load()
    entry = state.get(source)
    if not entry or entry.get("date") != _today_utc():
        return {"date": _today_utc(), "count": 0}
    return entry
