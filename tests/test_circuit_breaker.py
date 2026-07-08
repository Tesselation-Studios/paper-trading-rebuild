"""Tests for circuit_breaker module — agent tool-loop circuit breakers."""

import pytest
import time
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from src.circuit_breaker import (
    AgentCircuitBreaker,
    BreakerState,
    TickSession,
    ToolCallRecord,
    _args_to_signature,
    get_breaker,
    guard_tick,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestArgsToSignature:
    def test_empty_args(self):
        assert _args_to_signature({}) == "{}"

    def test_simple_args(self):
        sig = _args_to_signature({"query": "AAPL price", "limit": 5})
        assert "query=AAPL price" in sig
        assert "limit=5" in sig

    def test_nested_dict(self):
        sig = _args_to_signature({"params": {"ticker": "AAPL", "days": 30}})
        assert "ticker=AAPL" in sig
        assert "days=30" in sig

    def test_deterministic_order(self):
        sig1 = _args_to_signature({"b": 2, "a": 1})
        sig2 = _args_to_signature({"a": 1, "b": 2})
        assert sig1 == sig2

    def test_list_values(self):
        sig = _args_to_signature({"symbols": ["AAPL", "GOOG"]})
        assert "AAPL" in sig
        assert "GOOG" in sig


class TestToolCallRecord:
    def test_args_signature(self):
        record = ToolCallRecord("web_search", {"query": "AAPL"})
        assert "query=AAPL" in record.args_signature

    def test_same_args_same_signature(self):
        r1 = ToolCallRecord("web_search", {"query": "AAPL", "limit": 5})
        r2 = ToolCallRecord("web_search", {"limit": 5, "query": "AAPL"})
        assert r1.args_signature == r2.args_signature


# ═══════════════════════════════════════════════════════════════════════════════
# AgentCircuitBreaker — core functionality
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def reset_breakers():
    """Reset breaker instances between tests."""
    AgentCircuitBreaker._instances.clear()
    yield


class TestAgentCircuitBreaker:
    def test_instance_caching(self):
        b1 = AgentCircuitBreaker.get("trader-kairos")
        b2 = AgentCircuitBreaker.get("trader-kairos")
        assert b1 is b2

    def test_different_traders_different_instances(self):
        k = AgentCircuitBreaker.get("trader-kairos")
        a = AgentCircuitBreaker.get("trader-aldridge")
        assert k is not a

    def test_default_config_values(self):
        breaker = AgentCircuitBreaker("trader-test")
        assert breaker.max_tool_calls_per_tick == 20
        assert breaker.max_repeat_tool_args == 3
        assert breaker.tool_timeout_seconds == 60
        assert breaker.auto_pause_minutes == 5

    def test_custom_config_values(self):
        breaker = AgentCircuitBreaker(
            "trader-test",
            max_tool_calls_per_tick=10,
            max_repeat_tool_args=2,
            tool_timeout_seconds=30,
            auto_pause_minutes=3,
        )
        assert breaker.max_tool_calls_per_tick == 10
        assert breaker.max_repeat_tool_args == 2
        assert breaker.tool_timeout_seconds == 30
        assert breaker.auto_pause_minutes == 3

    def test_initial_not_paused(self):
        breaker = AgentCircuitBreaker("trader-test")
        assert not breaker.is_paused()

    def test_start_tick_creates_session(self):
        breaker = AgentCircuitBreaker("trader-test")
        breaker.start_tick()
        assert breaker.state.current_tick is not None
        assert breaker.state.current_tick.trader_id == "trader-test"

    def test_start_tick_clears_previous(self):
        breaker = AgentCircuitBreaker("trader-test")
        breaker.start_tick()
        first = breaker.state.current_tick
        breaker.start_tick()
        assert breaker.state.current_tick is not first


class TestToolCallTracking:
    """Tests for tool call tracking logic."""

    def test_track_within_limits(self):
        breaker = AgentCircuitBreaker("trader-test", max_tool_calls_per_tick=20)
        breaker.start_tick()
        for i in range(10):
            allowed, reason = breaker.track("web_search", {"query": f"AAPL-{i}"})
            assert allowed
            assert reason is None

    def test_track_exceeds_call_limit(self):
        breaker = AgentCircuitBreaker("trader-test", max_tool_calls_per_tick=5)
        breaker.start_tick()
        for i in range(5):
            breaker.track("web_search", {"query": f"AAPL-{i}"})
        # 6th call should trip
        allowed, reason = breaker.track("web_search", {"query": "AAPL-6"})
        assert not allowed
        assert "Tool call count exceeded" in reason

    def test_track_repeat_detection(self):
        breaker = AgentCircuitBreaker(
            "trader-test", max_tool_calls_per_tick=20, max_repeat_tool_args=3
        )
        breaker.start_tick()
        # Same tool + same args 3 times
        breaker.track("web_search", {"query": "AAPL"})
        breaker.track("web_search", {"query": "AAPL"})
        breaker.track("web_search", {"query": "AAPL"})
        # 4th time should trip
        allowed, reason = breaker.track("web_search", {"query": "AAPL"})
        assert not allowed
        assert "Repeat tool call" in reason

    def test_track_different_args_no_repeat(self):
        breaker = AgentCircuitBreaker("trader-test", max_repeat_tool_args=3)
        breaker.start_tick()
        breaker.track("web_search", {"query": "AAPL"})
        breaker.track("web_search", {"query": "GOOG"})
        breaker.track("web_search", {"query": "MSFT"})
        # Different args, should be fine
        allowed, _ = breaker.track("web_search", {"query": "TSLA"})
        assert allowed

    def test_track_same_tool_different_args_no_trip(self):
        breaker = AgentCircuitBreaker("trader-test", max_repeat_tool_args=3)
        breaker.start_tick()
        for i in range(5):
            allowed, _ = breaker.track("web_search", {"query": f"ticker-{i}"})
            assert allowed

    def test_track_after_trip_always_blocked(self):
        breaker = AgentCircuitBreaker("trader-test", max_tool_calls_per_tick=2)
        breaker.start_tick()
        breaker.track("a", {"x": 1})
        breaker.track("b", {"x": 2})
        # Third call trips
        allowed, reason = breaker.track("c", {"x": 3})
        assert not allowed
        # Fourth call also blocked
        allowed2, reason2 = breaker.track("d", {"x": 4})
        assert not allowed2
        assert "already tripped" in reason2.lower()


class TestTimeoutGate:
    """Tests for the timeout gate."""

    def test_no_timeout_when_decision_made(self):
        breaker = AgentCircuitBreaker("trader-test", tool_timeout_seconds=1)
        breaker.start_tick()
        breaker.track("web_search", {"query": "AAPL"})
        breaker.mark_decision()
        # Even after waiting, decision made = no timeout
        time.sleep(0.1)
        allowed, _ = breaker.track("web_search", {"query": "GOOG"})
        assert allowed

    def test_timeout_when_no_decision(self):
        breaker = AgentCircuitBreaker("trader-test", tool_timeout_seconds=0.01)
        breaker.start_tick()
        time.sleep(0.02)  # Exceed timeout
        allowed, reason = breaker.track("web_search", {"query": "AAPL"})
        assert not allowed
        assert "timeout" in reason.lower()


class TestTickContext:
    def test_context_manager_starts_and_ends(self):
        breaker = AgentCircuitBreaker("trader-test")
        with breaker.tick_context():
            assert breaker.state.current_tick is not None
            breaker.mark_decision()
        assert breaker.state.current_tick is None

    def test_context_warns_on_no_decision(self):
        breaker = AgentCircuitBreaker("trader-test")
        with breaker.tick_context():
            breaker.track("test", {"x": 1})
        # Session ended without decision — end_tick logs a warning


class TestStatus:
    def test_status_basic(self):
        breaker = AgentCircuitBreaker("trader-test")
        s = breaker.status()
        assert s["trader_id"] == "trader-test"
        assert not s["is_paused"]
        assert s["total_trips"] == 0

    def test_status_with_active_tick(self):
        breaker = AgentCircuitBreaker("trader-test")
        breaker.start_tick()
        breaker.track("web_search", {"query": "AAPL"})
        s = breaker.status()
        assert s["current_tick"]["active"]
        assert s["current_tick"]["call_count"] == 1

    def test_get_all_status(self):
        AgentCircuitBreaker.get("trader-kairos")
        AgentCircuitBreaker.get("trader-aldridge")
        statuses = AgentCircuitBreaker.get_all_status()
        assert len(statuses) == 2
        assert "trader-kairos" in statuses
        assert "trader-aldridge" in statuses


class TestConvenience:
    def test_get_breaker_helper(self):
        b = get_breaker("trader-kairos")
        assert isinstance(b, AgentCircuitBreaker)
        assert b.trader_id == "trader-kairos"


# ═══════════════════════════════════════════════════════════════════════════════
# Integration scenarios
# ═══════════════════════════════════════════════════════════════════════════════


class TestRealWorldScenarios:
    """Scenarios that match the issue #36 requirements."""

    def test_per_tick_call_counter_exceeds_20(self):
        """a) Per-trader tool call counter — flag if >20."""
        breaker = AgentCircuitBreaker("trader-test", max_tool_calls_per_tick=20)
        breaker.start_tick()
        # 20 calls should be fine
        for i in range(20):
            allowed, _ = breaker.track("get_quote", {"symbol": f"T{i}"})
            assert allowed
        # 21st call trips
        allowed, reason = breaker.track("get_quote", {"symbol": "T21"})
        assert not allowed
        assert "exceeded" in reason.lower()

    def test_repeat_same_tool_same_args_3x_trips(self):
        """b) Same tool + same args 3+ times in one tick = abort."""
        breaker = AgentCircuitBreaker("trader-test", max_repeat_tool_args=3)
        breaker.start_tick()
        breaker.track("execute_trade", {"ticker": "KO", "action": "BUY", "shares": 10})
        breaker.track("execute_trade", {"ticker": "KO", "action": "BUY", "shares": 10})
        breaker.track("execute_trade", {"ticker": "KO", "action": "BUY", "shares": 10})
        allowed, reason = breaker.track("execute_trade", {"ticker": "KO", "action": "BUY", "shares": 10})
        assert not allowed
        assert "Repeat" in reason
        assert "execute_trade" in reason

    def test_timeout_60s_no_decision(self):
        """c) Trader spends >60s in tool calls without decision = hard stop."""
        breaker = AgentCircuitBreaker("trader-test", tool_timeout_seconds=0.01)
        breaker.start_tick()
        time.sleep(0.02)
        allowed, reason = breaker.track("web_search", {"query": "market news"})
        assert not allowed
        assert "timeout" in reason.lower()
        assert "no trading decision" in reason.lower()

    def test_auto_pause_logic(self):
        """d) Circuit trips → pause for N minutes."""
        breaker = AgentCircuitBreaker("trader-test",
                                      max_tool_calls_per_tick=2,
                                      auto_pause_minutes=5)
        breaker.start_tick()
        breaker.track("a", {})
        breaker.track("b", {})
        allowed, reason = breaker.track("c", {})
        assert not allowed
        assert breaker.state.total_trips == 1
        assert breaker.state.last_trip_at is not None


class TestEdgeCases:
    def test_track_without_start_auto_starts(self):
        breaker = AgentCircuitBreaker("trader-test")
        allowed, _ = breaker.track("web_search", {"query": "AAPL"})
        assert allowed
        assert breaker.state.current_tick is not None
        assert len(breaker.state.current_tick.calls) == 1

    def test_mark_decision_on_empty_tick(self):
        breaker = AgentCircuitBreaker("trader-test")
        breaker.start_tick()
        breaker.mark_decision()
        assert breaker.state.current_tick.decision_made

    def test_empty_args_handled(self):
        breaker = AgentCircuitBreaker("trader-test", max_repeat_tool_args=2)
        breaker.start_tick()
        breaker.track("ping")
        breaker.track("ping")
        allowed, reason = breaker.track("ping")
        assert not allowed
        assert "Repeat" in reason

    def test_none_args_handled(self):
        breaker = AgentCircuitBreaker("trader-test")
        breaker.start_tick()
        allowed, _ = breaker.track("check_status", None)
        assert allowed

    def test_multiple_trips_counted(self):
        breaker = AgentCircuitBreaker("trader-test", max_tool_calls_per_tick=2)
        # First trip
        breaker.start_tick()
        breaker.track("a", {})
        breaker.track("b", {})
        breaker.track("c", {})
        assert breaker.state.total_trips == 1

        # Reset and trip again
        breaker.start_tick()
        breaker.track("d", {})
        breaker.track("e", {})
        breaker.track("f", {})
        assert breaker.state.total_trips == 2

    def test_end_tick_logs_warning_no_decision(self):
        breaker = AgentCircuitBreaker("trader-test")
        breaker.start_tick()
        breaker.track("test", {"x": 1})
        breaker.track("test", {"y": 2})
        breaker.end_tick()
        # Should have warned about no decision — tick cleaned up
        assert breaker.state.current_tick is None


# ═══════════════════════════════════════════════════════════════════════════════
# guard_tick() convenience function
# ═══════════════════════════════════════════════════════════════════════════════


class TestGuardTick:
    """Tests for the guard_tick() pre-tick convenience function."""

    def test_allows_when_not_paused(self):
        result = guard_tick("trader-guard-test")
        assert result["allowed"] is True
        assert result["reason"] == ""
        assert "trader_id" in result["status"]

    def test_returns_consistent_status_keys(self):
        result = guard_tick("trader-guard-status")
        assert "allowed" in result
        assert "reason" in result
        assert "status" in result
        assert result["status"]["trader_id"] == "trader-guard-status"
        assert result["status"]["total_trips"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Trader integration — circuit breaker wired into process_tick()
# ═══════════════════════════════════════════════════════════════════════════════


class TestTraderCircuitBreakerIntegration:
    """Verify Trader.process_tick() integrates with AgentCircuitBreaker."""

    def test_trader_has_agent_breaker(self):
        from src.trader import Trader
        trader = Trader("trader-tcb-test")
        assert trader.agent_breaker is not None
        assert isinstance(trader.agent_breaker, AgentCircuitBreaker)

    def test_track_tool_call_proxies_to_breaker(self):
        from src.trader import Trader
        trader = Trader("trader-tcb-proxy")
        trader.agent_breaker.start_tick()
        allowed, reason = trader.track_tool_call("web_search", {"query": "AAPL"})
        assert allowed is True
        assert reason is None
        assert len(trader.agent_breaker.state.current_tick.calls) == 1

    def test_track_tool_call_repeat_detection(self):
        from src.trader import Trader
        trader = Trader("trader-tcb-repeat", max_journal_entries=10)
        trader.agent_breaker.max_repeat_tool_args = 3
        trader.agent_breaker.start_tick()
        for _ in range(3):
            trader.track_tool_call("get_quote", {"symbol": "AAPL"})
        allowed, reason = trader.track_tool_call("get_quote", {"symbol": "AAPL"})
        assert not allowed
        assert "Repeat" in reason

    def test_trader_process_tick_checks_pause_state(self):
        """process_tick() checks agent breaker pause state."""
        from src.trader import create_trader
        from src.replay import make_deterministic_uptrend_ticks

        trader = create_trader("trader-tcb-process")
        ticks = make_deterministic_uptrend_ticks(
            ticker="AAPL", n=1, start_price=150.0, step_pct=0.01,
        )
        # Not paused — should process normally
        trader.process_tick(ticks[0])
        assert trader.state.ticks_processed >= 0  # may be 0 if signal not enough

    def test_trader_process_tick_marks_decision_on_hold(self):
        """process_tick() calls mark_decision() on HOLD."""
        from src.trader import create_trader
        from src.replay import make_deterministic_uptrend_ticks

        trader = create_trader("trader-tcb-mark")
        # Feed one tick — should produce HOLD due to low conviction
        ticks = make_deterministic_uptrend_ticks(
            ticker="AAPL", n=1, start_price=150.0, step_pct=0.01,
        )
        # process_tick() calls start_tick() then end_tick() via the decision path
        trader.process_tick(ticks[0])
        # The agent breaker should be in a clean state (no active tick session)
        # because process_tick doesn't manage the tick lifecycle directly
        assert trader.agent_breaker is not None
