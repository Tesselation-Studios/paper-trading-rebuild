"""Agent Circuit Breakers — prevent tool-call loops in paper trading agents.

This module sits on the paper-trading side of the bridge. It does NOT replace
OpenClaw's built-in loopDetection (openclaw.json → tools.loopDetection) — it
complements it by:

  1. Tracking tool calls per tick (per-trader counter)
  2. Detecting repeated tool+args calls in a single tick
  3. Enforcing a timeout gate (trader spends >60s in tool calls)
  4. Auto-pausing traders that trip a breaker (writes to trading.risk_state)
  5. Respecting paused state when the tick flow checks before dispatching

Architecture:
    tick arrives → check_paused(trader_id) → if paused, skip tick
    → TraderAgent processes tick → each tool call passes through track()
    → if limits exceeded → trip() → is_paused=True in risk_state
    → next tick checks paused → skipped → auto-resumes after cooldown

Config keys (from config/risk.yaml, section risk.circuit_breaker):
    risk.circuit_breaker.max_tool_calls_per_tick   (default: 20)
    risk.circuit_breaker.max_repeat_tool_args       (default: 3)
    risk.circuit_breaker.tool_timeout_seconds       (default: 60)
    risk.circuit_breaker.auto_pause_minutes         (default: 5)

Usage:
    from src.circuit_breaker import AgentCircuitBreaker, get_breaker

    breaker = get_breaker("trader-kairos")
    if breaker.is_paused():
        return  # skip this tick

    with breaker.tick_context():
        breaker.track("web_search", {"query": "AAPL price"})
        # ... more tool calls
        breaker.track("web_search", {"query": "AAPL price"})  # repeat!
        # This would trip the breaker if done 3x
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from src.config_loader import get_config

log = logging.getLogger("circuit_breaker")


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> Dict[str, Any]:
    """Load circuit breaker config from config/risk.yaml."""
    try:
        config = get_config()
        return config.get("risk", {}).get("circuit_breaker", {})
    except Exception:
        return {}


# ── Data Structures ───────────────────────────────────────────────────────────


@dataclass
class ToolCallRecord:
    """One tool call within a tick."""
    tool_name: str
    args: Dict[str, Any]
    timestamp: float = field(default_factory=time.monotonic)

    @property
    def args_signature(self) -> str:
        """Deterministic string for args comparison."""
        return _args_to_signature(self.args)


@dataclass
class TickSession:
    """Tracks all tool calls within one tick for one trader."""
    trader_id: str
    started_at: float = field(default_factory=time.monotonic)
    calls: List[ToolCallRecord] = field(default_factory=list)
    tripped: bool = False
    trip_reason: str = ""
    decision_made: bool = False


@dataclass
class BreakerState:
    """Volatile breaker state for a trader (not persisted — risk_state is the DB)."""
    current_tick: Optional[TickSession] = None
    total_trips: int = 0
    last_trip_at: Optional[datetime] = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _args_to_signature(args: Dict[str, Any]) -> str:
    """Convert tool args dict to a comparable signature string."""
    if not args:
        return "{}"
    # Sort keys for deterministic comparison
    parts = []
    for k in sorted(args.keys()):
        v = args[k]
        if isinstance(v, dict):
            v = _args_to_signature(v)
        elif isinstance(v, list):
            v = "[" + ",".join(str(_args_to_signature(x) if isinstance(x, dict) else x) for x in v) + "]"
        parts.append(f"{k}={v}")
    return "{" + ",".join(parts) + "}"


# ── Agent Circuit Breaker ─────────────────────────────────────────────────────


class AgentCircuitBreaker:
    """Per-trader circuit breaker for tool-call loop detection.

    Each trader gets one breaker instance. It tracks tool calls within
    a tick, detects repeats and timeouts, and pauses the trader when
    limits are exceeded.

    Args:
        trader_id: Which trader (trader-kairos, trader-aldridge, trader-stonks).
        max_tool_calls_per_tick: Max tool calls before flagging.
        max_repeat_tool_args: Same tool+args N times = abort.
        tool_timeout_seconds: Max seconds in tool calls without a decision.
        auto_pause_minutes: Minutes to pause after a trip.
    """

    # Cache breaker instances per trader_id
    _instances: Dict[str, "AgentCircuitBreaker"] = {}

    def __init__(
        self,
        trader_id: str,
        max_tool_calls_per_tick: Optional[int] = None,
        max_repeat_tool_args: Optional[int] = None,
        tool_timeout_seconds: Optional[int] = None,
        auto_pause_minutes: Optional[int] = None,
    ):
        self.trader_id = trader_id

        # Load from config with defaults
        cfg = _load_config()
        self.max_tool_calls_per_tick = (
            max_tool_calls_per_tick
            or int(cfg.get("max_tool_calls_per_tick", 20))
        )
        self.max_repeat_tool_args = (
            max_repeat_tool_args
            or int(cfg.get("max_repeat_tool_args", 3))
        )
        self.tool_timeout_seconds = (
            tool_timeout_seconds
            or int(cfg.get("tool_timeout_seconds", 60))
        )
        self.auto_pause_minutes = (
            auto_pause_minutes
            or int(cfg.get("auto_pause_minutes", 5))
        )

        self.state = BreakerState()

    # ── Public API ────────────────────────────────────────────────────────

    def is_paused(self) -> bool:
        """Check if this trader is currently paused (reads risk_state).

        Returns True if paused AND the cooldown hasn't expired.
        """
        try:
            rs = _get_risk_state_sync(self.trader_id)
            if not rs:
                return False
            is_paused = rs.get("is_paused", False)
            if not is_paused:
                return False
            paused_at = rs.get("paused_at")
            if paused_at:
                # Auto-resume after cooldown
                if isinstance(paused_at, str):
                    paused_at = datetime.fromisoformat(paused_at.replace("Z", "+00:00"))
                cooldown_end = paused_at + timedelta(minutes=self.auto_pause_minutes)
                if datetime.now(paused_at.tzinfo) >= cooldown_end:
                    # Auto-resume: clear pause
                    _clear_pause_sync(self.trader_id)
                    log.info(
                        "[%s] Auto-resumed after %dmin cooldown (paused at %s)",
                        self.trader_id, self.auto_pause_minutes, paused_at,
                    )
                    return False
            return True
        except Exception:
            return False

    def check_paused(self) -> Tuple[bool, Optional[str]]:
        """Check if paused. Returns (is_paused, reason_or_None)."""
        try:
            rs = _get_risk_state_sync(self.trader_id)
            if not rs:
                return False, None
            is_paused = rs.get("is_paused", False)
            if not is_paused:
                return False, None
            paused_at = rs.get("paused_at")
            if paused_at:
                if isinstance(paused_at, str):
                    paused_at = datetime.fromisoformat(paused_at.replace("Z", "+00:00"))
                cooldown_end = paused_at + timedelta(minutes=self.auto_pause_minutes)
                if datetime.now(paused_at.tzinfo) >= cooldown_end:
                    _clear_pause_sync(self.trader_id)
                    log.info("[%s] Auto-resumed after cooldown", self.trader_id)
                    return False, None

            reason = rs.get("paused_reason", "Circuit breaker tripped")
            return True, reason
        except Exception:
            return False, None

    def start_tick(self) -> None:
        """Begin tracking a new tick for this trader."""
        self.state.current_tick = TickSession(trader_id=self.trader_id)
        log.debug("[%s] Tick started — tracking tool calls", self.trader_id)

    def end_tick(self) -> None:
        """End the current tick session (cleanup)."""
        if self.state.current_tick and not self.state.current_tick.decision_made:
            log.warning(
                "[%s] Tick ended without a trading decision — %d tool calls made",
                self.trader_id,
                len(self.state.current_tick.calls),
            )
        self.state.current_tick = None

    def mark_decision(self) -> None:
        """Mark that a trading decision was made (BUY/SELL/HOLD)."""
        if self.state.current_tick:
            self.state.current_tick.decision_made = True

    @contextmanager
    def tick_context(self):
        """Context manager for a single tick:
            with breaker.tick_context():
                breaker.track("web_search", args)
                breaker.mark_decision()
        """
        self.start_tick()
        try:
            yield self
        finally:
            self.end_tick()

    def track(self, tool_name: str, args: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[str]]:
        """Track one tool call. Returns (allowed, reason_if_blocked).

        Checks:
          1. Call count > max_tool_calls_per_tick?
          2. Same tool+args repeated > max_repeat_tool_args?
          3. Total time in tool calls > tool_timeout_seconds?

        Returns:
            (True, None) if call is allowed.
            (False, reason) if the circuit trips.
        """
        if self.state.current_tick is None:
            self.start_tick()

        tick = self.state.current_tick
        if tick is None:
            return True, None  # shouldn't happen

        if tick.tripped:
            return False, f"Circuit already tripped: {tick.trip_reason}"

        args = args or {}
        record = ToolCallRecord(tool_name=tool_name, args=args)
        tick.calls.append(record)

        # 1. Per-tick call count check
        if len(tick.calls) > self.max_tool_calls_per_tick:
            reason = (
                f"Tool call count exceeded: {len(tick.calls)} > "
                f"{self.max_tool_calls_per_tick} in one tick"
            )
            self._trip(reason)
            return False, reason

        # 2. Repeat detection: same tool + same args
        repeat_count = sum(
            1 for c in tick.calls
            if c.tool_name == tool_name and c.args_signature == record.args_signature
        )
        if repeat_count >= self.max_repeat_tool_args:
            reason = (
                f"Repeat tool call: {tool_name}({_args_preview(args)}) called "
                f"{repeat_count}x in one tick (limit: {self.max_repeat_tool_args})"
            )
            self._trip(reason)
            return False, reason

        # 3. Timeout gate: no decision after timeout
        elapsed = time.monotonic() - tick.started_at
        if elapsed > self.tool_timeout_seconds and not tick.decision_made:
            reason = (
                f"Tool timeout: {elapsed:.1f}s elapsed with {len(tick.calls)} "
                f"tool calls and no trading decision"
            )
            self._trip(reason)
            return False, reason

        return True, None

    def _trip(self, reason: str) -> None:
        """Trip the circuit breaker — pause trader, write to risk_state."""
        tick = self.state.current_tick
        if tick:
            tick.tripped = True
            tick.trip_reason = reason

        self.state.total_trips += 1
        self.state.last_trip_at = datetime.now()

        log.warning(
            "[%s] CIRCUIT BREAKER TRIP (trip #%d): %s",
            self.trader_id, self.state.total_trips, reason,
        )

        # Persist to risk_state
        try:
            _upsert_risk_state_sync(
                agent_id=self.trader_id,
                is_paused=True,
                paused_reason=reason,
                paused_at=datetime.now(),
            )
        except Exception as e:
            log.error("[%s] Failed to write risk_state on trip: %s", self.trader_id, e)

    def reset(self) -> bool:
        """Manually reset the circuit breaker (unpause trader).

        Returns True if successful.
        """
        try:
            _clear_pause_sync(self.trader_id)
            self.state.current_tick = None
            log.info("[%s] Circuit breaker manually reset", self.trader_id)
            return True
        except Exception as e:
            log.error("[%s] Failed to reset circuit breaker: %s", self.trader_id, e)
            return False

    def status(self) -> Dict[str, Any]:
        """Get current breaker status."""
        paused, reason = self.check_paused()
        tick = self.state.current_tick
        return {
            "trader_id": self.trader_id,
            "is_paused": paused,
            "paused_reason": reason,
            "total_trips": self.state.total_trips,
            "last_trip_at": self.state.last_trip_at.isoformat() if self.state.last_trip_at else None,
            "current_tick": {
                "active": tick is not None,
                "call_count": len(tick.calls) if tick else 0,
                "elapsed_s": round(time.monotonic() - tick.started_at, 1) if tick else 0,
                "tripped": tick.tripped if tick else False,
                "decision_made": tick.decision_made if tick else False,
            } if tick else None,
        }

    @classmethod
    def get(cls, trader_id: str) -> "AgentCircuitBreaker":
        """Get or create a breaker instance for a trader."""
        if trader_id not in cls._instances:
            cls._instances[trader_id] = cls(trader_id=trader_id)
        return cls._instances[trader_id]

    @classmethod
    def get_all_status(cls) -> Dict[str, Dict[str, Any]]:
        """Get status for all tracked traders."""
        return {
            tid: breaker.status()
            for tid, breaker in cls._instances.items()
        }


def get_breaker(trader_id: str) -> AgentCircuitBreaker:
    """Convenience: get circuit breaker for a trader."""
    return AgentCircuitBreaker.get(trader_id)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _args_preview(args: Dict[str, Any], max_len: int = 80) -> str:
    """Compact args preview for logging."""
    s = str(args)
    if len(s) > max_len:
        return s[:max_len - 3] + "..."
    return s


# ── DB helpers (sync — psycopg2) ──────────────────────────────────────────────
# These use the same connection as leaderboard_api to stay consistent.


def _get_connection():
    """Get a sync psycopg2 connection for risk_state operations."""
    from src.db.connection import get_connection as _gc
    return _gc()


def _get_risk_state_sync(agent_id: str) -> Optional[Dict[str, Any]]:
    """Read risk_state row for an agent (sync, psycopg2)."""
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM trading.risk_state WHERE agent_id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    except Exception as e:
        log.error("Failed to read risk_state for %s: %s", agent_id, e)
        return None


def _upsert_risk_state_sync(
    agent_id: str,
    is_paused: bool = False,
    paused_reason: Optional[str] = None,
    paused_at: Optional[datetime] = None,
) -> None:
    """Upsert risk_state row (sync, psycopg2)."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO trading.risk_state (agent_id, is_paused, paused_reason, paused_at, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (agent_id) DO UPDATE SET
                is_paused     = EXCLUDED.is_paused,
                paused_reason = EXCLUDED.paused_reason,
                paused_at     = EXCLUDED.paused_at,
                updated_at    = NOW()
            """,
            (agent_id, is_paused, paused_reason, paused_at),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _clear_pause_sync(agent_id: str) -> None:
    """Clear pause state for an agent (sync, psycopg2)."""
    _upsert_risk_state_sync(
        agent_id=agent_id,
        is_paused=False,
        paused_reason=None,
        paused_at=None,
    )
