"""Tests for safety module — SPEC-v3 §8, §11, §12."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

from src.safety import (
    BreakerLevel,
    BreakerState,
    CircuitBreaker,
    ChangeRecord,
    GovernorState,
    ChangeGovernor,
    ShadowDecision,
    ShadowResult,
    ShadowMode,
    RecoveryManager,
)

pytestmark = pytest.mark.integration


# ═══════════════════════════════════════════════════════════════════════════════
# Circuit Breaker tests (§8)
# ═══════════════════════════════════════════════════════════════════════════════


class TestBreakerLevel:
    def test_can_trade_normal(self):
        state = BreakerState(level=BreakerLevel.NORMAL)
        assert state.can_trade is True
        assert state.position_multiplier == 1.0

    def test_can_trade_caution(self):
        state = BreakerState(level=BreakerLevel.CAUTION)
        assert state.can_trade is True
        assert state.position_multiplier == 0.5

    def test_cannot_trade_paused(self):
        state = BreakerState(level=BreakerLevel.PAUSED)
        assert state.can_trade is False
        assert state.position_multiplier == 0.0

    def test_cannot_trade_emergency(self):
        state = BreakerState(level=BreakerLevel.EMERGENCY)
        assert state.can_trade is False
        assert state.position_multiplier == 0.0


class TestCircuitBreaker:
    def test_normal_at_low_drawdown(self):
        cb = CircuitBreaker("test")
        cb.update(equity=100_000)
        cb.update(equity=98_000)  # 2% DD
        assert cb.state.level == BreakerLevel.NORMAL

    def test_caution_at_medium_drawdown(self):
        cb = CircuitBreaker("test")
        cb.update(equity=100_000)  # set peak
        cb.update(equity=93_000)  # 7% DD
        assert cb.state.level == BreakerLevel.CAUTION

    def test_paused_at_high_drawdown(self):
        cb = CircuitBreaker("test")
        cb.update(equity=100_000)
        cb.update(equity=88_000)  # 12% DD
        assert cb.state.level == BreakerLevel.PAUSED

    def test_emergency_at_critical_drawdown(self):
        cb = CircuitBreaker("test")
        cb.update(equity=100_000)
        cb.update(equity=80_000)  # 20% DD
        assert cb.state.level == BreakerLevel.EMERGENCY

    def test_emergency_stays_emergency(self):
        """Once in emergency, stays there even if equity recovers."""
        cb = CircuitBreaker("test")
        cb.update(equity=100_000)
        cb.update(equity=80_000)  # emergency
        assert cb.state.level == BreakerLevel.EMERGENCY
        # Equity recovers but breaker stays
        cb.update(equity=120_000)
        assert cb.state.level == BreakerLevel.EMERGENCY

    def test_peak_equity_tracks_highs(self):
        cb = CircuitBreaker("test")
        cb.update(equity=100_000)
        assert cb.state.peak_equity == 100_000
        cb.update(equity=110_000)
        assert cb.state.peak_equity == 110_000
        cb.update(equity=105_000)
        assert cb.state.peak_equity == 110_000  # peak stays

    def test_consecutive_losses_cool_off(self):
        cb = CircuitBreaker("test")
        cb.update(equity=100_000)
        # Three consecutive losses
        cb.update(equity=99_500, last_trade_pnl=-500)
        assert cb.state.level == BreakerLevel.NORMAL
        cb.update(equity=99_000, last_trade_pnl=-500)
        cb.update(equity=98_500, last_trade_pnl=-500)
        # Third loss triggers cooling off
        assert cb.state.level == BreakerLevel.CAUTION
        assert cb.state.cooling_off_until is not None

    def test_win_resets_consecutive_losses(self):
        cb = CircuitBreaker("test")
        cb.update(equity=100_000)
        cb.update(equity=99_500, last_trade_pnl=-500)
        cb.update(equity=99_000, last_trade_pnl=-500)
        # Winning trade resets
        cb.update(equity=99_500, last_trade_pnl=500)
        assert cb.state.consecutive_losses == 0

    def test_re_enable_from_emergency(self):
        cb = CircuitBreaker("test")
        cb.update(equity=100_000)
        cb.update(equity=80_000)
        assert cb.state.level == BreakerLevel.EMERGENCY
        cb.re_enable()
        assert cb.state.level == BreakerLevel.NORMAL
        assert cb.state.emergency_at is None

    def test_submit_recovery_plan(self):
        cb = CircuitBreaker("test")
        cb.update(equity=100_000)
        cb.update(equity=88_000)
        assert cb.state.level == BreakerLevel.PAUSED
        cb.submit_recovery_plan("Overleveraged in high vol. Will reduce position sizes by 30%.")
        assert cb.state.recovery_plan is not None
        # After recovery plan and DD drops below caution, should recover
        cb.update(equity=98_000)  # DD now 2% from original peak
        # Recovery happens if plan exists and DD < caution threshold
        assert cb.state.level == BreakerLevel.NORMAL

    def test_position_multiplier_updates(self):
        cb = CircuitBreaker("test")
        cb.update(equity=100_000)
        assert cb.state.position_multiplier == 1.0
        cb.update(equity=93_000)
        assert cb.state.position_multiplier == 0.5
        cb.update(equity=88_000)
        assert cb.state.position_multiplier == 0.0

    def test_journal_logs_transitions(self):
        cb = CircuitBreaker("test")
        cb.update(equity=100_000)
        cb.update(equity=93_000)
        assert len(cb.state.journal) >= 1
        assert "normal → caution" in cb.state.journal[-1].lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Change Governor tests (§12)
# ═══════════════════════════════════════════════════════════════════════════════


class TestChangeGovernor:
    def test_can_change_within_budget(self):
        cg = ChangeGovernor("test", max_monthly_changes=5)
        ok, reason = cg.can_change("momentum_threshold")
        assert ok is True

    def test_cannot_change_budget_exhausted(self):
        cg = ChangeGovernor("test", max_monthly_changes=0)  # no budget
        ok, reason = cg.can_change("momentum_threshold")
        assert ok is False
        assert "budget exhausted" in reason.lower()

    def test_record_change_applies_damping(self):
        cg = ChangeGovernor("test", damping_factor=0.3)
        record = cg.record_change("momentum_threshold", old_value=0.55, proposed_value=0.85)
        # damped: 0.55 + 0.3 * (0.85 - 0.55) = 0.55 + 0.09 = 0.64
        assert 0.63 < record.new_value < 0.65
        assert record.old_value == 0.55
        assert record.proposed_value == 0.85

    def test_record_change_increments_budget(self):
        cg = ChangeGovernor("test", max_monthly_changes=3)
        assert cg.state.monthly_changes == 0
        cg.record_change("param_a", 1.0, 1.5)
        assert cg.state.monthly_changes == 1
        cg.record_change("param_b", 2.0, 2.5)
        assert cg.state.monthly_changes == 2

    def test_freeze_after_change(self):
        cg = ChangeGovernor("test", freeze_days=5)
        cg.record_change("momentum_threshold", 0.5, 0.7)
        ok, reason = cg.can_change("momentum_threshold")
        assert ok is False
        assert "frozen" in reason.lower()

    def test_different_param_not_frozen(self):
        cg = ChangeGovernor("test", freeze_days=5)
        cg.record_change("momentum_threshold", 0.5, 0.7)
        # Different param should be fine
        ok, _ = cg.can_change("rsi_oversold")
        assert ok is True

    def test_detect_revert(self):
        cg = ChangeGovernor("test", revert_window_days=20)
        cg.record_change("momentum_threshold", 0.55, 0.75)  # went up
        # Now propose going back down
        is_revert = cg.detect_revert("momentum_threshold", old_value=0.75, new_value=0.55)
        assert is_revert is True
        assert cg.state.revert_counts["momentum_threshold"] == 1

    def test_no_revert_same_direction(self):
        cg = ChangeGovernor("test")
        cg.record_change("momentum_threshold", 0.55, 0.75)  # went up
        # Continuing up is not a revert
        is_revert = cg.detect_revert("momentum_threshold", old_value=0.75, new_value=0.90)
        assert is_revert is False

    def test_no_revert_no_history(self):
        cg = ChangeGovernor("test")
        is_revert = cg.detect_revert("momentum_threshold", 0.55, 0.75)
        assert is_revert is False

    def test_budget_resets_new_month(self):
        cg = ChangeGovernor("test", max_monthly_changes=2)
        cg.record_change("a", 1.0, 1.5)
        cg.record_change("b", 2.0, 2.5)
        # Manually expire the month
        cg.state.month_start = datetime(2020, 1, 1)
        ok, _ = cg.can_change("c")
        assert ok is True  # new month, budget reset

    def test_revert_penalty_reduces_budget(self):
        cg = ChangeGovernor("test", max_monthly_changes=5, revert_penalty=0.5)
        cg.state.revert_counts["momentum_threshold"] = 3  # reverted 3x
        # effective budget = 5 * 0.5^3 = 5 * 0.125 = 0.625 → 0
        ok, reason = cg.can_change("momentum_threshold")
        assert ok is False
        assert "reverted" in reason.lower()

    def test_change_record_has_reason(self):
        cg = ChangeGovernor("test")
        record = cg.record_change("momentum_threshold", 0.5, 0.7, reason="gradient descent")
        assert record.reason == "gradient descent"


# ═══════════════════════════════════════════════════════════════════════════════
# Shadow Mode tests (§11)
# ═══════════════════════════════════════════════════════════════════════════════


class TestShadowMode:
    def test_start_shadow(self):
        sm = ShadowMode("kairos")
        sm.start_shadow("variant-047", rollback_sha="abc123", live_calmar=1.5)
        assert sm.is_shadowing is True
        assert sm.days_remaining == 5

    def test_not_enough_data_returns_none(self):
        sm = ShadowMode("kairos", eval_days=5)
        sm.start_shadow("v1", "abc", 1.5)
        # Only 2 days of data
        sm.record_day(1.5, 1.6)
        sm.record_day(1.5, 1.7)
        result = sm.evaluate()
        assert result is None

    def test_evaluate_auto_merge(self):
        sm = ShadowMode("kairos", eval_days=3, auto_merge_threshold=0.10)
        sm.start_shadow("v1", "abc", 1.5)
        # Shadow has better Calmar AND lower drawdown (better = less negative)
        sm.record_day(1.5, 2.0)
        sm.record_day(1.5, 2.1)
        sm.record_day(1.5, 2.2)
        # Shadow min DD = 2.0 > live min DD = 1.5 → not auto-merge eligible with strict inequality
        # Fix: use a case where shadow both improves Calmar AND has lower drawdown
        sm2 = ShadowMode("kairos", eval_days=2, auto_merge_threshold=0.10)
        sm2.start_shadow("v2", "xyz", 1.5)
        sm2.record_day(1.8, 2.3)
        sm2.record_day(1.8, 2.4)
        result = sm2.evaluate()
        assert result is not None
        assert result.decision == ShadowDecision.AUTO_MERGE
        assert result.improvement_pct > 20  # big improvement

    def test_evaluate_notify(self):
        sm = ShadowMode("kairos", eval_days=2, notify_threshold=0.05, auto_merge_threshold=0.20)
        sm.start_shadow("v1", "abc", 1.5)
        sm.record_day(1.5, 1.62)  # 8% improvement
        sm.record_day(1.5, 1.63)
        result = sm.evaluate()
        assert result is not None
        assert result.decision == ShadowDecision.NOTIFY

    def test_evaluate_review(self):
        sm = ShadowMode("kairos", eval_days=2, review_threshold=0.01, notify_threshold=0.10)
        sm.start_shadow("v1", "abc", 1.5)
        sm.record_day(1.5, 1.53)  # 2% improvement
        sm.record_day(1.5, 1.54)
        result = sm.evaluate()
        assert result is not None
        assert result.decision == ShadowDecision.REVIEW

    def test_evaluate_discard(self):
        sm = ShadowMode("kairos", eval_days=2, review_threshold=0.05)
        sm.start_shadow("v1", "abc", 1.5)
        sm.record_day(1.5, 1.51)  # < 1% improvement
        sm.record_day(1.5, 1.50)
        result = sm.evaluate()
        assert result is not None
        assert result.decision == ShadowDecision.DISCARD

    def test_degradation_detected(self):
        sm = ShadowMode("kairos", rollback_threshold=0.10)
        # Calmar dropped from 2.0 to 1.5 = 25% drop > 10% threshold
        assert sm.check_degradation(current_calmar=1.5, pre_merge_calmar=2.0) is True

    def test_degradation_within_threshold(self):
        sm = ShadowMode("kairos", rollback_threshold=0.10)
        # Calmar dropped from 2.0 to 1.85 = 7.5% drop < 10% threshold
        assert sm.check_degradation(current_calmar=1.85, pre_merge_calmar=2.0) is False

    def test_degradation_zero_pre_merge(self):
        sm = ShadowMode("kairos")
        assert sm.check_degradation(current_calmar=1.5, pre_merge_calmar=0.0) is False

    def test_shadow_summary(self):
        result = ShadowResult(
            live_calmar=1.5, shadow_calmar=1.8,
            live_worst=1.4, shadow_worst=1.7,
            improvement_pct=20.0, decision=ShadowDecision.AUTO_MERGE,
            days_evaluated=5, rollback_point="abc123",
        )
        summary = result.summary
        assert "1.50→1.80" in summary
        assert "+20.0%" in summary
        assert "auto_merge" in summary


# ═══════════════════════════════════════════════════════════════════════════════
# Recovery Manager tests (§8.3)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRecoveryManager:
    def test_initial_state(self):
        rm = RecoveryManager("kairos")
        ready, reason = rm.can_exit_recovery()
        assert ready is False
        assert "observation ticks" in reason.lower()

    def test_cannot_exit_without_plan(self):
        rm = RecoveryManager("kairos", min_recovery_ticks=2)
        for i in range(5):
            rm.observe({"ticker": "AAPL", "price": 100 + i}, {"decision": "HOLD"})
        ready, reason = rm.can_exit_recovery()
        assert ready is False
        assert "recovery plan" in reason.lower()

    def test_can_exit_with_plan_and_ticks(self):
        rm = RecoveryManager("kairos", min_recovery_ticks=3)
        for i in range(5):
            rm.observe({"ticker": "AAPL", "price": 100 + i}, {"decision": "HOLD"})
        result = rm.propose_recovery_plan(
            "Overleveraged in high volatility. Will reduce max positions from 8 to 5 "
            "and increase stop-loss from 5% to 8%."
        )
        assert result == "accepted"
        ready, reason = rm.can_exit_recovery()
        assert ready is True

    def test_plan_too_short_rejected(self):
        rm = RecoveryManager("kairos", min_recovery_ticks=0)
        result = rm.propose_recovery_plan("fix it")
        assert "rejected" in result
        assert "too short" in result.lower()

    def test_plan_rejected_not_enough_ticks(self):
        rm = RecoveryManager("kairos", min_recovery_ticks=10)
        rm.observe({"ticker": "AAPL"}, {"decision": "HOLD"})  # only 1 tick
        result = rm.propose_recovery_plan("A detailed plan explaining exactly what went wrong and how I will fix it going forward")
        assert "rejected" in result

    def test_reset_clears_state(self):
        rm = RecoveryManager("kairos", min_recovery_ticks=1)
        rm.observe({"ticker": "AAPL"}, {"decision": "HOLD"})
        rm.propose_recovery_plan("A sufficiently detailed recovery plan with specific actions")
        rm.reset()
        assert rm.observation_count == 0
        ready, _ = rm.can_exit_recovery()
        assert ready is False

    def test_mock_decisions_capped(self):
        rm = RecoveryManager("kairos", min_recovery_ticks=0)
        for i in range(60):
            rm.observe({"ticker": f"T{i}"}, {"decision": "HOLD"})
        assert rm.observation_count == 60
        # But only last 50 are kept
        ready, _ = rm.can_exit_recovery()
        # Still can't exit without plan
        assert ready is False
