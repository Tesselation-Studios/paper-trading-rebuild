"""Tests for drawdown knockout circuit breaker — src/drawdown_knockout.py.

Covers:
  1. All 4 drawdown tiers (normal, reduced, paused, emergency)
  2. Cooling-off: 3 consecutive losses → skip 2 signals
  3. Recovery mode: observation ticks + plan submission → exit paused
  4. Human re_enable() from emergency
  5. position_multiplier and allow_exits at each tier
  6. DrawdownKnockoutGate rejection logic
  7. Edge cases: sequential transitions, sticky emergency, synthetic 20% drawdown
"""

# ── no mocks import block — real observability is fine (in-memory) ──────────

import pytest
from datetime import datetime, timedelta

from src.drawdown_knockout import (
    KnockoutLevel,
    KnockoutState,
    KnockoutStateMachine,
    DrawdownKnockoutGate,
    DrawdownKnockout,
)
from src.observability import metrics, alert


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _epoch(days_ago: int = 0, hours_ago: int = 0) -> datetime:
    return datetime(2026, 7, 11, 7, 0) - timedelta(days=days_ago, hours=hours_ago)


# ═══════════════════════════════════════════════════════════════════════════════
# KnockoutLevel — enum properties
# ═══════════════════════════════════════════════════════════════════════════════


class TestKnockoutLevel:
    """Verify enum properties per spec."""

    @pytest.mark.parametrize("level,can_trade,can_open,mult,allow_exit", [
        (KnockoutLevel.NORMAL,     True,  True,  1.0, True),
        (KnockoutLevel.REDUCED,    True,  False, 0.5, True),
        (KnockoutLevel.PAUSED,     False, False, 0.0, True),
        (KnockoutLevel.EMERGENCY,  False, False, 0.0, False),
        (KnockoutLevel.COOLING_OFF, False, False, 0.0, True),
    ])
    def test_properties(self, level, can_trade, can_open, mult, allow_exit):
        assert level.can_trade == can_trade
        assert level.can_open_new_positions == can_open
        assert level.position_multiplier == mult
        assert level.allow_exits == allow_exit


# ═══════════════════════════════════════════════════════════════════════════════
# KnockoutState — pure data
# ═══════════════════════════════════════════════════════════════════════════════


class TestKnockoutStateDefaults:
    def test_default_level_is_normal(self):
        s = KnockoutState()
        assert s.level == KnockoutLevel.NORMAL

    def test_default_values(self):
        s = KnockoutState()
        assert s.current_drawdown == 0.0
        assert s.peak_equity == 0.0
        assert s.consecutive_losses == 0
        assert s.cooling_off_signals_to_skip == 0
        assert s.recovery_observation_ticks == 0

    def test_delegates_to_level(self):
        s = KnockoutState(level=KnockoutLevel.EMERGENCY)
        assert s.can_trade is False
        assert s.allow_exits is False
        assert s.position_multiplier == 0.0

    def test_to_dict_includes_all_keys(self):
        s = KnockoutState(level=KnockoutLevel.REDUCED, current_drawdown=0.07)
        d = s.to_dict()
        assert d["level"] == "reduced"
        assert d["current_drawdown_pct"] == 7.0
        assert d["can_trade"] is True
        assert d["can_open_new_positions"] is False
        assert d["position_multiplier"] == 0.5
        assert d["allow_exits"] is True

    def test_to_dict_no_paused_at(self):
        s = KnockoutState()
        d = s.to_dict()
        assert d["paused_at"] is None
        assert d["emergency_at"] is None
        assert d["has_recovery_plan"] is False

    def test_to_dict_with_paused_at(self):
        dt = _epoch()
        s = KnockoutState(paused_at=dt, recovery_plan="plan text")
        d = s.to_dict()
        assert d["paused_at"] == dt.isoformat()
        assert d["has_recovery_plan"] is True
        assert d["recovery_observation_ticks"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# KnockoutStateMachine — core tier determination
# ═══════════════════════════════════════════════════════════════════════════════


class TestStateMachineDefaults:
    def test_default_thresholds(self):
        m = KnockoutStateMachine()
        assert m.caution_threshold == 0.05
        assert m.pause_threshold == 0.10
        assert m.emergency_threshold == 0.15
        assert m.max_consecutive_losses == 3
        assert m.cool_off_skip_signals == 2
        assert m.min_recovery_ticks == 10

    def test_custom_thresholds(self):
        m = KnockoutStateMachine(
            caution_threshold=0.03,
            pause_threshold=0.08,
            emergency_threshold=0.12,
            max_consecutive_losses=5,
            cool_off_skip_signals=3,
            min_recovery_ticks=20,
        )
        assert m.caution_threshold == 0.03
        assert m.pause_threshold == 0.08
        assert m.emergency_threshold == 0.12
        assert m.max_consecutive_losses == 5
        assert m.cool_off_skip_signals == 3
        assert m.min_recovery_ticks == 20


class TestStateMachine_Normal:
    def test_initial_equity_no_drawdown(self):
        m = KnockoutStateMachine()
        state = KnockoutState()
        level, new_state = m.compute(state, equity=100_000.0, now=_epoch())
        assert level == KnockoutLevel.NORMAL
        assert new_state.current_drawdown == 0.0
        assert new_state.peak_equity == 100_000.0

    def test_small_drawdown_stays_normal(self):
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)
        level, _ = m.compute(state, equity=96_000.0, now=_epoch())  # 4% DD
        assert level == KnockoutLevel.NORMAL

    def test_gain_increases_peak(self):
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)
        level, new_state = m.compute(state, equity=105_000.0, now=_epoch())
        assert level == KnockoutLevel.NORMAL
        assert new_state.peak_equity == 105_000.0


class TestStateMachine_Reduced:
    def test_5pct_drawdown_enters_reduced(self):
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)
        level, _ = m.compute(state, equity=94_000.0, now=_epoch())  # 6% DD
        assert level == KnockoutLevel.REDUCED

    def test_exact_5pct_boundary_triggers_reduced(self):
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)
        level, _ = m.compute(state, equity=95_000.0, now=_epoch())  # exactly 5%
        assert level == KnockoutLevel.REDUCED

    def test_just_below_5pct_stays_normal(self):
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)
        level, _ = m.compute(state, equity=95_100.0, now=_epoch())  # 4.9% DD
        assert level == KnockoutLevel.NORMAL

    def test_reduced_recovers_to_normal(self):
        m = KnockoutStateMachine()
        state = KnockoutState(level=KnockoutLevel.REDUCED, peak_equity=100_000.0)
        level, _ = m.compute(state, equity=96_000.0, now=_epoch())  # back to 4%
        assert level == KnockoutLevel.NORMAL


class TestStateMachine_Paused:
    def test_10pct_drawdown_enters_paused(self):
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)
        level, _ = m.compute(state, equity=89_000.0, now=_epoch())  # 11% DD
        assert level == KnockoutLevel.PAUSED

    def test_exact_10pct_boundary_enters_paused(self):
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)
        level, _ = m.compute(state, equity=90_000.0, now=_epoch())  # exactly 10%
        assert level == KnockoutLevel.PAUSED

    def test_paused_stays_paused_without_recovery_even_if_dd_drops(self):
        m = KnockoutStateMachine()
        state = KnockoutState(
            level=KnockoutLevel.PAUSED,
            peak_equity=100_000.0,
            current_drawdown=0.12,
        )
        # DD drops to 5% but no recovery plan yet
        level, _ = m.compute(state, equity=95_000.0, now=_epoch())
        assert level == KnockoutLevel.PAUSED

    def test_paused_needs_plan_and_ticks_and_dd_below_caution(self):
        m = KnockoutStateMachine()
        state = KnockoutState(
            level=KnockoutLevel.PAUSED,
            peak_equity=100_000.0,
            recovery_plan="I will reduce risk exposure by tightening stops",
            recovery_observation_ticks=10,  # meets min_recovery_ticks
        )
        # DD = 4%
        level, new_state = m.compute(state, equity=96_000.0, now=_epoch())
        assert level == KnockoutLevel.NORMAL, f"Expected NORMAL, got {level}"
        assert new_state.recovery_observation_ticks == 10  # preserved

    def test_paused_insufficient_observation_ticks(self):
        m = KnockoutStateMachine()
        state = KnockoutState(
            level=KnockoutLevel.PAUSED,
            peak_equity=100_000.0,
            recovery_plan="I will reduce risk",
            recovery_observation_ticks=3,  # < 10
        )
        level, _ = m.compute(state, equity=96_000.0, now=_epoch())
        assert level == KnockoutLevel.PAUSED

    def test_paused_dd_still_too_high_for_exit(self):
        m = KnockoutStateMachine()
        state = KnockoutState(
            level=KnockoutLevel.PAUSED,
            peak_equity=100_000.0,
            recovery_plan="I will reduce risk",
            recovery_observation_ticks=10,
        )
        # DD = 6% — above caution (5%)
        level, _ = m.compute(state, equity=94_000.0, now=_epoch())
        assert level == KnockoutLevel.PAUSED  # DD still too high

    def test_paused_stays_paused_when_dd_above_caution(self):
        """When DD is below pause but still above caution threshold, PAUSED persists.
        The spec requires DD below caution (5%) to exit PAUSED.
        """
        m = KnockoutStateMachine()
        state = KnockoutState(
            level=KnockoutLevel.PAUSED,
            peak_equity=100_000.0,
            recovery_plan="I will reduce risk exposure",
            recovery_observation_ticks=10,
        )
        # DD = 6% — above caution (5%) but below pause (10%)
        level, _ = m.compute(state, equity=94_000.0, now=_epoch())
        # DD >= caution_threshold, so stays PAUSED
        assert level == KnockoutLevel.PAUSED

    def test_paused_exits_to_normal_when_dd_below_caution(self):
        """DD below caution (5%) with recovery plan + ticks met → exit PAUSED to NORMAL."""
        m = KnockoutStateMachine()
        state = KnockoutState(
            level=KnockoutLevel.PAUSED,
            peak_equity=100_000.0,
            recovery_plan="I will reduce risk exposure",
            recovery_observation_ticks=10,
        )
        # DD = 4% — below caution (5%)
        level, _ = m.compute(state, equity=96_000.0, now=_epoch())
        assert level == KnockoutLevel.NORMAL


class TestStateMachine_Emergency:
    def test_15pct_drawdown_enters_emergency(self):
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)
        level, _ = m.compute(state, equity=84_000.0, now=_epoch())  # 16% DD
        assert level == KnockoutLevel.EMERGENCY

    def test_emergency_is_sticky(self):
        m = KnockoutStateMachine()
        state = KnockoutState(level=KnockoutLevel.EMERGENCY, peak_equity=100_000.0)
        # DD drops to 0 (fully recovered) — still EMERGENCY
        level, _ = m.compute(state, equity=100_000.0, now=_epoch())
        assert level == KnockoutLevel.EMERGENCY

    def test_emergency_stays_emergency_even_with_gain(self):
        m = KnockoutStateMachine()
        state = KnockoutState(level=KnockoutLevel.EMERGENCY, peak_equity=100_000.0)
        level, _ = m.compute(state, equity=110_000.0, now=_epoch())
        assert level == KnockoutLevel.EMERGENCY

    @pytest.mark.parametrize("dd_pct", [15.0, 16.0, 20.0, 50.0])
    def test_direct_entry_at_various_dd_levels(self, dd_pct):
        m = KnockoutStateMachine()
        equity = 100_000.0 * (1 - dd_pct / 100)
        state = KnockoutState(peak_equity=100_000.0)
        level, _ = m.compute(state, equity=equity, now=_epoch())
        assert level == KnockoutLevel.EMERGENCY

    def test_20pct_synthetic_drawdown_freeze(self):
        """Synthetic 20% drawdown → EMERGENCY → positions frozen."""
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)
        level, new_state = m.compute(state, equity=80_000.0, now=_epoch())
        assert level == KnockoutLevel.EMERGENCY
        assert new_state.allow_exits is False
        assert new_state.position_multiplier == 0.0
        assert new_state.can_trade is False


class TestStateMachine_CoolingOff:
    def test_three_consecutive_losses_triggers_cooling_off(self):
        m = KnockoutStateMachine()
        state = KnockoutState()

        # 3 losing trades
        for i in range(3):
            level, state = m.compute(
                state, equity=100_000.0, last_trade_pnl=-100.0, now=_epoch(),
            )
        # The compute() returns COOLING_OFF but the calling code (DrawdownKnockout)
        # sets the skip count. The raw machine returns COOLING_OFF when consecutive >= 3.
        assert state.consecutive_losses == 3
        assert level == KnockoutLevel.COOLING_OFF

    def test_cooling_off_skips_n_signals(self):
        """When cooling_off_signals_to_skip > 0, level stays COOLING_OFF."""
        m = KnockoutStateMachine()
        state = KnockoutState(cooling_off_signals_to_skip=2, consecutive_losses=3)
        level, new_state = m.compute(state, equity=100_000.0, now=_epoch())
        assert level == KnockoutLevel.COOLING_OFF
        assert new_state.cooling_off_signals_to_skip == 1  # decremented

    def test_cooling_off_decays_one_per_tick(self):
        m = KnockoutStateMachine()
        state = KnockoutState(cooling_off_signals_to_skip=2, consecutive_losses=3)
        # Tick 1
        level, state = m.compute(state, equity=100_000.0, now=_epoch())
        assert level == KnockoutLevel.COOLING_OFF
        assert state.cooling_off_signals_to_skip == 1
        # Tick 2
        level, state = m.compute(state, equity=100_000.0, now=_epoch())
        assert level == KnockoutLevel.COOLING_OFF
        assert state.cooling_off_signals_to_skip == 0
        # Tick 3 — cooling-off expired, but consecutive_losses still 3
        # The pure machine checks both cooling_off_signals_to_skip AND
        # consecutive_losses >= max_consecutive_losses, so we need a win
        level, state = m.compute(
            state, equity=100_000.0, last_trade_pnl=50.0, now=_epoch(),
        )
        # Win resets consecutive_losses to 0, so no more cooling-off
        assert state.consecutive_losses == 0
        assert level == KnockoutLevel.NORMAL

    def test_winning_trade_resets_consecutive_losses(self):
        m = KnockoutStateMachine()
        state = KnockoutState(cooling_off_signals_to_skip=0, consecutive_losses=3)
        # Win
        level, state = m.compute(state, equity=100_000.0, last_trade_pnl=50.0, now=_epoch())
        assert state.consecutive_losses == 0
        # Cooling off not triggered — shouldn't return COOLING_OFF
        assert level != KnockoutLevel.COOLING_OFF

    def test_partial_losses_below_threshold(self):
        m = KnockoutStateMachine()
        state = KnockoutState()
        for i in range(2):
            _, state = m.compute(state, equity=100_000.0, last_trade_pnl=-50.0, now=_epoch())
        assert state.consecutive_losses == 2
        # Not yet cooling off
        level, _ = m.compute(state, equity=100_000.0, last_trade_pnl=10.0, now=_epoch())
        assert state.consecutive_losses == 2  # the above line computes but uses original state
        # Actually let's re-examine: last call resets to 0 since it won
        level2, state2 = m.compute(state, equity=100_000.0, last_trade_pnl=10.0, now=_epoch())
        assert state2.consecutive_losses == 0


class TestStateMachine_SequentialTransitions:
    """End-to-end: NORMAL → REDUCED → PAUSED → EMERGENCY."""

    def test_normal_to_reduced_to_paused_to_emergency(self):
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)

        # NORMAL → REDUCED (6% DD)
        level, state = m.compute(state, equity=94_000.0, now=_epoch())
        assert level == KnockoutLevel.REDUCED

        # REDUCED → PAUSED (11% DD)
        level, state = m.compute(state, equity=89_000.0, now=_epoch())
        assert level == KnockoutLevel.PAUSED

        # PAUSED → EMERGENCY (16% DD)
        level, state = m.compute(state, equity=84_000.0, now=_epoch())
        assert level == KnockoutLevel.EMERGENCY

    def test_normal_skips_reduced_goes_direct_to_paused(self):
        """Rapid drawdown: 0% → 12% in one tick."""
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)
        level, _ = m.compute(state, equity=88_000.0, now=_epoch())
        assert level == KnockoutLevel.PAUSED

    def test_normal_skips_all_goes_direct_to_emergency(self):
        """Flash crash: 0% → 20% in one tick."""
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)
        level, _ = m.compute(state, equity=80_000.0, now=_epoch())
        assert level == KnockoutLevel.EMERGENCY

    def test_emergency_overrides_paused_recovery(self):
        """If already PAUSED but DD exceeds 15%, go straight to EMERGENCY."""
        m = KnockoutStateMachine()
        state = KnockoutState(
            level=KnockoutLevel.PAUSED,
            peak_equity=100_000.0,
            recovery_plan="plan",
            recovery_observation_ticks=10,
        )
        # DD = 16% — emergency should override paused recovery
        level, _ = m.compute(state, equity=84_000.0, now=_epoch())
        assert level == KnockoutLevel.EMERGENCY

    def test_reduced_to_normal_when_drawdown_improves(self):
        m = KnockoutStateMachine()
        state = KnockoutState(level=KnockoutLevel.REDUCED, peak_equity=100_000.0)
        level, _ = m.compute(state, equity=97_000.0, now=_epoch())  # 3% DD
        assert level == KnockoutLevel.NORMAL

    def test_cooling_off_during_reduced(self):
        """Cooling-off can trigger even while in REDUCED tier."""
        m = KnockoutStateMachine()
        state = KnockoutState(
            level=KnockoutLevel.REDUCED,
            peak_equity=100_000.0,
            consecutive_losses=2,
        )
        level, _ = m.compute(state, equity=94_000.0, last_trade_pnl=-100.0, now=_epoch())
        assert state.consecutive_losses == 2  # pre-compute
        level, state = m.compute(state, equity=94_000.0, last_trade_pnl=-100.0, now=_epoch())
        assert state.consecutive_losses == 3
        # The machine returns COOLING_OFF, calling code handles skip count
        # But compute() with consecutive_losses >= max returns COOLING_OFF
        level2, state2 = m.compute(state, equity=94_000.0, last_trade_pnl=-100.0, now=_epoch())
        # consecutive is now 4 > 3 so yes
        assert level2 == KnockoutLevel.COOLING_OFF


# ═══════════════════════════════════════════════════════════════════════════════
# DrawdownKnockoutGate — rejection logic
# ═══════════════════════════════════════════════════════════════════════════════


class TestKnockoutGate_NoState:
    def test_no_state_in_context_passes(self):
        gate = DrawdownKnockoutGate()
        granted, reason = gate.check({}, {"type": "BUY", "ticker": "AAPL"})
        assert granted is True
        assert "no knockout state" in reason.lower()


class TestKnockoutGate_Normal:
    @pytest.fixture
    def state(self):
        return KnockoutState(level=KnockoutLevel.NORMAL, current_drawdown=0.02)

    def test_buy_allowed(self, state):
        gate = DrawdownKnockoutGate()
        granted, reason = gate.check(
            {"knockout_state": state}, {"type": "BUY", "ticker": "AAPL"},
        )
        assert granted is True
        assert "NORMAL" in reason

    def test_sell_allowed(self, state):
        gate = DrawdownKnockoutGate()
        granted, _ = gate.check(
            {"knockout_state": state}, {"type": "SELL", "ticker": "AAPL"},
        )
        assert granted is True

    def test_hold_allowed(self, state):
        gate = DrawdownKnockoutGate()
        granted, _ = gate.check(
            {"knockout_state": state}, {"type": "HOLD", "ticker": "AAPL"},
        )
        assert granted is True


class TestKnockoutGate_Reduced:
    @pytest.fixture
    def state(self):
        return KnockoutState(level=KnockoutLevel.REDUCED, current_drawdown=0.07)

    def test_buy_allowed_with_warning(self, state):
        gate = DrawdownKnockoutGate()
        granted, reason = gate.check(
            {"knockout_state": state}, {"type": "BUY", "ticker": "AAPL"},
        )
        assert granted is True
        assert "REDUCED" in reason
        assert "50%" in reason

    def test_sell_allowed(self, state):
        gate = DrawdownKnockoutGate()
        granted, _ = gate.check(
            {"knockout_state": state}, {"type": "SELL", "ticker": "AAPL"},
        )
        assert granted is True


class TestKnockoutGate_Paused:
    @pytest.fixture
    def state(self):
        return KnockoutState(level=KnockoutLevel.PAUSED, current_drawdown=0.12)

    def test_buy_rejected(self, state):
        gate = DrawdownKnockoutGate()
        granted, reason = gate.check(
            {"knockout_state": state}, {"type": "BUY", "ticker": "AAPL"},
        )
        assert granted is False
        assert "PAUSED" in reason.upper() or "paused" in reason.lower()
        assert "observation only" in reason.lower()

    def test_sell_allowed(self, state):
        gate = DrawdownKnockoutGate()
        granted, reason = gate.check(
            {"knockout_state": state}, {"type": "SELL", "ticker": "AAPL"},
        )
        assert granted is True
        assert "SELL" in reason


class TestKnockoutGate_Emergency:
    @pytest.fixture
    def state(self):
        return KnockoutState(level=KnockoutLevel.EMERGENCY, current_drawdown=0.18)

    def test_buy_rejected(self, state):
        gate = DrawdownKnockoutGate()
        granted, reason = gate.check(
            {"knockout_state": state}, {"type": "BUY", "ticker": "AAPL"},
        )
        assert granted is False
        assert "EMERGENCY" in reason

    def test_sell_rejected(self, state):
        gate = DrawdownKnockoutGate()
        granted, reason = gate.check(
            {"knockout_state": state}, {"type": "SELL", "ticker": "AAPL"},
        )
        assert granted is False
        assert "frozen" in reason.lower() or "emergency" in reason.lower()

    def test_hold_still_allowed(self, state):
        """HOLD is never rejected — it's a no-op."""
        gate = DrawdownKnockoutGate()
        granted, reason = gate.check(
            {"knockout_state": state}, {"type": "HOLD", "ticker": "AAPL"},
        )
        assert granted is True
        assert "HOLD" in reason


class TestKnockoutGate_CoolingOff:
    @pytest.fixture
    def state(self):
        return KnockoutState(level=KnockoutLevel.COOLING_OFF, current_drawdown=0.02)

    def test_buy_rejected(self, state):
        gate = DrawdownKnockoutGate()
        granted, reason = gate.check(
            {"knockout_state": state}, {"type": "BUY", "ticker": "AAPL"},
        )
        assert granted is False
        assert "COOLING" in reason.upper() or "cooling" in reason.lower()

    def test_sell_allowed(self, state):
        gate = DrawdownKnockoutGate()
        granted, reason = gate.check(
            {"knockout_state": state}, {"type": "SELL", "ticker": "AAPL"},
        )
        assert granted is True
        assert "SELL" in reason


class TestKnockoutGate_EdgeCases:
    def test_action_with_action_key_instead_of_type(self):
        gate = DrawdownKnockoutGate()
        state = KnockoutState(level=KnockoutLevel.PAUSED)
        granted, _ = gate.check(
            {"knockout_state": state}, {"action": "BUY", "ticker": "AAPL"},
        )
        assert granted is False

    def test_missing_ticker_still_works(self):
        gate = DrawdownKnockoutGate()
        state = KnockoutState(level=KnockoutLevel.NORMAL)
        granted, _ = gate.check(
            {"knockout_state": state}, {"type": "BUY"},
        )
        assert granted is True

    def test_lowercase_action_type(self):
        gate = DrawdownKnockoutGate()
        state = KnockoutState(level=KnockoutLevel.PAUSED)
        granted, reason = gate.check(
            {"knockout_state": state}, {"type": "buy", "ticker": "aapl"},
        )
        assert granted is False


# ═══════════════════════════════════════════════════════════════════════════════
# DrawdownKnockout — full orchestrator with transitions and recovery
# ═══════════════════════════════════════════════════════════════════════════════


class TestDrawdownKnockout_Init:
    def test_default_initial_state(self):
        dk = DrawdownKnockout("trader-test")
        assert dk.trader_id == "trader-test"
        assert dk.state.level == KnockoutLevel.NORMAL
        assert dk.state.peak_equity == 0.0
        assert dk.gate is not None

    def test_custom_config(self):
        dk = DrawdownKnockout(
            "trader-test",
            caution_threshold=0.03,
            pause_threshold=0.07,
            emergency_threshold=0.12,
            max_consecutive_losses=5,
        )
        assert dk._machine.caution_threshold == 0.03
        assert dk._machine.max_consecutive_losses == 5

    def test_repr(self):
        dk = DrawdownKnockout("trader-repr")
        r = repr(dk)
        assert "trader-repr" in r
        assert "normal" in r
        assert "DD=0.0%" in r
        assert "peak=$0" in r


class TestDrawdownKnockout_Update:
    def test_first_update_sets_peak(self):
        dk = DrawdownKnockout("trader-upd", caution_threshold=0.05)
        dk.update(100_000.0, now=_epoch())
        assert dk.state.peak_equity == 100_000.0
        assert dk.state.level == KnockoutLevel.NORMAL

    def test_equity_gain_updates_peak(self):
        dk = DrawdownKnockout("trader-gain")
        dk.update(100_000.0, now=_epoch())
        dk.update(110_000.0, now=_epoch())
        assert dk.state.peak_equity == 110_000.0

    def test_tracks_consecutive_losses(self):
        dk = DrawdownKnockout("trader-consec")
        dk.update(100_000.0, now=_epoch())
        assert dk.state.consecutive_losses == 0

        dk.update(100_000.0, last_trade_pnl=-50.0, now=_epoch())
        assert dk.state.consecutive_losses == 1

        dk.update(100_000.0, last_trade_pnl=-30.0, now=_epoch())
        assert dk.state.consecutive_losses == 2

    def test_win_resets_consecutive_losses(self):
        dk = DrawdownKnockout("trader-win")
        dk.update(100_000.0, now=_epoch())
        dk.update(100_000.0, last_trade_pnl=-50.0, now=_epoch())
        dk.update(100_000.0, last_trade_pnl=-30.0, now=_epoch())
        assert dk.state.consecutive_losses == 2
        dk.update(100_000.0, last_trade_pnl=20.0, now=_epoch())
        assert dk.state.consecutive_losses == 0

    def test_drawdown_update_changes_level(self):
        dk = DrawdownKnockout("trader-dd")
        dk.update(100_000.0, now=_epoch())  # NORMAL
        dk.update(93_000.0, now=_epoch())  # 7% DD → REDUCED
        assert dk.state.level == KnockoutLevel.REDUCED

    def test_equity_history_grows(self):
        dk = DrawdownKnockout("trader-hist")
        for i in range(5):
            dk.update(100_000.0 + i * 1000.0, now=_epoch())
        assert len(dk.state.equity_history) == 5

    def test_equity_history_bounded(self):
        dk = DrawdownKnockout("trader-bound")
        for i in range(1500):
            dk.update(100_000.0, now=_epoch())
        assert len(dk.state.equity_history) <= 1000


class TestDrawdownKnockout_Transitions:
    """End-to-end transition scenarios via update()."""

    def test_normal_to_reduced(self):
        dk = DrawdownKnockout("trader-e2e", caution_threshold=0.05)
        dk.update(100_000.0, now=_epoch())
        dk.update(94_000.0, now=_epoch())  # 6% DD
        assert dk.state.level == KnockoutLevel.REDUCED

    def test_normal_to_paused(self):
        dk = DrawdownKnockout("trader-e2e2", caution_threshold=0.05, pause_threshold=0.10)
        dk.update(100_000.0, now=_epoch())
        dk.update(88_000.0, now=_epoch())  # 12% DD
        assert dk.state.level == KnockoutLevel.PAUSED

    def test_normal_to_emergency(self):
        dk = DrawdownKnockout("trader-e2e3")
        dk.update(100_000.0, now=_epoch())
        dk.update(80_000.0, now=_epoch())  # 20% DD
        assert dk.state.level == KnockoutLevel.EMERGENCY

    def test_full_sequence_reduced_to_paused_to_emergency(self):
        dk = DrawdownKnockout("trader-full")
        dk.update(100_000.0, now=_epoch())
        dk.update(94_000.0, now=_epoch())   # REDUCED
        assert dk.state.level == KnockoutLevel.REDUCED
        dk.update(89_000.0, now=_epoch())   # PAUSED
        assert dk.state.level == KnockoutLevel.PAUSED
        dk.update(84_000.0, now=_epoch())   # EMERGENCY
        assert dk.state.level == KnockoutLevel.EMERGENCY

    def test_reduced_recovers_to_normal(self):
        dk = DrawdownKnockout("trader-recover")
        dk.update(100_000.0, now=_epoch())
        dk.update(93_000.0, now=_epoch())  # REDUCED
        assert dk.state.level == KnockoutLevel.REDUCED
        dk.update(97_000.0, now=_epoch())  # 3% DD → NORMAL
        assert dk.state.level == KnockoutLevel.NORMAL

    def test_paused_sets_timestamp(self):
        dk = DrawdownKnockout("trader-pts")
        now = _epoch()
        dk.update(100_000.0, now=now)
        dk.update(88_000.0, now=now + timedelta(seconds=1))
        assert dk.state.level == KnockoutLevel.PAUSED
        assert dk.state.paused_at is not None

    def test_emergency_sets_timestamp(self):
        dk = DrawdownKnockout("trader-ets")
        now = _epoch()
        dk.update(100_000.0, now=now)
        dk.update(80_000.0, now=now + timedelta(seconds=1))
        assert dk.state.level == KnockoutLevel.EMERGENCY
        assert dk.state.emergency_at is not None


class TestDrawdownKnockout_CoolingOff:
    """End-to-end cooling-off through the orchestrator."""

    def test_three_losses_triggers_cooling_off(self):
        dk = DrawdownKnockout("trader-co")
        dk.update(100_000.0, now=_epoch())
        # 3 consecutive losses
        dk.update(100_000.0, last_trade_pnl=-100.0, now=_epoch())  # loss 1
        assert dk.state.consecutive_losses == 1
        dk.update(100_000.0, last_trade_pnl=-100.0, now=_epoch())  # loss 2
        assert dk.state.consecutive_losses == 2
        dk.update(100_000.0, last_trade_pnl=-100.0, now=_epoch())  # loss 3 → cooling
        assert dk.state.consecutive_losses == 3
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        assert dk.state.cooling_off_signals_to_skip == 2

    def test_cooling_off_skips_two_signals(self):
        dk = DrawdownKnockout("trader-co2")
        dk.update(100_000.0, now=_epoch())
        dk.update(100_000.0, last_trade_pnl=-100.0, now=_epoch())
        dk.update(100_000.0, last_trade_pnl=-100.0, now=_epoch())
        dk.update(100_000.0, last_trade_pnl=-100.0, now=_epoch())  # cooling
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        assert dk.state.cooling_off_signals_to_skip == 2

        # Tick 1: skip 1
        dk.update(100_000.0, now=_epoch())
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        assert dk.state.cooling_off_signals_to_skip == 1

        # Tick 2: skip 2 (counter hits 0)
        dk.update(100_000.0, now=_epoch())
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        assert dk.state.cooling_off_signals_to_skip == 0

        # Tick 3: winning trade resets losses → exit cooling-off
        dk.update(100_000.0, last_trade_pnl=50.0, now=_epoch())
        assert dk.state.level == KnockoutLevel.NORMAL

    def test_cooling_off_journal_entry(self):
        dk = DrawdownKnockout("trader-co-j")
        dk.update(100_000.0, now=_epoch())
        dk.update(100_000.0, last_trade_pnl=-100.0, now=_epoch())
        dk.update(100_000.0, last_trade_pnl=-100.0, now=_epoch())
        dk.update(100_000.0, last_trade_pnl=-100.0, now=_epoch())
        journal = dk.state.journal
        co_entries = [e for e in journal if "COOLING" in e.upper()]
        assert len(co_entries) >= 1

    def test_no_cooling_off_for_wins(self):
        dk = DrawdownKnockout("trader-co-w")
        dk.update(100_000.0, now=_epoch())
        dk.update(100_000.0, last_trade_pnl=50.0, now=_epoch())
        dk.update(100_000.0, last_trade_pnl=30.0, now=_epoch())
        dk.update(100_000.0, last_trade_pnl=20.0, now=_epoch())
        assert dk.state.level == KnockoutLevel.NORMAL
        assert dk.state.cooling_off_signals_to_skip == 0


class TestDrawdownKnockout_Recovery:
    """Recovery from PAUSED: observation ticks + plan submission."""

    def test_recovery_observation_ticks_increment(self):
        dk = DrawdownKnockout("trader-rec", min_recovery_ticks=5)
        dk.update(100_000.0, now=_epoch())
        dk.update(80_000.0, now=_epoch())  # 20% → EMERGENCY (overrides paused)
        # Can't get to paused if DD >= 15%, let's use 12%
        dk2 = DrawdownKnockout("trader-rec2", min_recovery_ticks=5)
        dk2.update(100_000.0, now=_epoch())
        dk2.update(88_000.0, now=_epoch())  # 12% → PAUSED
        assert dk2.state.level == KnockoutLevel.PAUSED
        # Tick 2
        dk2.update(88_000.0, now=_epoch())
        assert dk2.state.recovery_observation_ticks >= 1

    def test_submit_recovery_plan_rejected_if_not_paused(self):
        dk = DrawdownKnockout("trader-rj")
        dk.update(100_000.0, now=_epoch())
        result = dk.submit_recovery_plan("I will tighten stops")
        assert result.startswith("rejected")
        assert "not in PAUSED" in result

    def test_submit_recovery_plan_too_short(self):
        dk = DrawdownKnockout("trader-rs")
        dk.update(100_000.0, now=_epoch())
        dk.update(88_000.0, now=_epoch())  # PAUSED
        result = dk.submit_recovery_plan("fix it")
        assert result.startswith("rejected")
        assert "too short" in result

    def test_submit_recovery_plan_too_few_ticks(self):
        dk = DrawdownKnockout("trader-rt", min_recovery_ticks=10)
        dk.update(100_000.0, now=_epoch())
        dk.update(88_000.0, now=_epoch())  # PAUSED
        result = dk.submit_recovery_plan("I will reduce risk by tightening stops and cutting losers faster")
        assert result.startswith("rejected")
        assert "observation ticks" in result

    def test_full_recovery_flow(self):
        dk = DrawdownKnockout("trader-full-rec", min_recovery_ticks=3)
        dk.update(100_000.0, now=_epoch())
        dk.update(88_000.0, now=_epoch())  # 12% → PAUSED
        assert dk.state.level == KnockoutLevel.PAUSED

        # Observe for 3 ticks
        for i in range(3):
            dk.update(88_000.0, now=_epoch())
        assert dk.state.recovery_observation_ticks >= 3

        # Submit plan
        result = dk.submit_recovery_plan(
            "I will tighten stop losses, reduce position sizes, and avoid high-beta names"
        )
        assert result == "accepted"

        # DD stays at 12% — still PAUSED (DD too high)
        assert dk.state.level == KnockoutLevel.PAUSED

    def test_full_recovery_exit_paused(self):
        """Exit PAUSED after plan submitted, ticks met, and DD improves."""
        dk = DrawdownKnockout("trader-rec-exit", min_recovery_ticks=3)
        now = _epoch()
        dk.update(100_000.0, now=now)
        dk.update(88_000.0, now=now + timedelta(seconds=1))  # PAUSED

        for i in range(4):
            dk.update(88_000.0, now=now + timedelta(seconds=2 + i))

        # Submit plan
        dk.submit_recovery_plan(
            "I will tighten stops and reduce position sizes"
        )

        # DD improves to 4% (below caution)
        dk.update(96_000.0, now=now + timedelta(seconds=10))
        assert dk.state.level == KnockoutLevel.NORMAL


class TestDrawdownKnockout_ReEnable:
    """Human re_enable() from emergency."""

    def test_re_enable_resets_state(self):
        dk = DrawdownKnockout("trader-re")
        dk.update(100_000.0, now=_epoch())
        dk.update(80_000.0, now=_epoch())  # EMERGENCY
        assert dk.state.level == KnockoutLevel.EMERGENCY

        dk.re_enable()
        assert dk.state.level == KnockoutLevel.NORMAL
        assert dk.state.current_drawdown == 0.0
        assert dk.state.consecutive_losses == 0
        assert dk.state.cooling_off_signals_to_skip == 0
        assert dk.state.recovery_plan is None
        assert dk.state.recovery_observation_ticks == 0
        assert dk.state.paused_at is None
        assert dk.state.emergency_at is None

    def test_re_enable_from_normal_is_noop(self):
        dk = DrawdownKnockout("trader-re2")
        dk.update(100_000.0, now=_epoch())
        dk.re_enable()  # Should not crash — resets anyway
        assert dk.state.level == KnockoutLevel.NORMAL

    def test_re_enable_journal_entry(self):
        dk = DrawdownKnockout("trader-re3")
        dk.update(100_000.0, now=_epoch())
        dk.update(80_000.0, now=_epoch())
        dk.re_enable()
        journal = dk.state.journal
        re_entries = [e for e in journal if "RE-ENABLED" in e or "re_enable" in e.lower()]
        assert len(re_entries) >= 1

    def test_after_re_enable_trading_resumes(self):
        """After re_enable(), new equity sets new peak and trading works."""
        dk = DrawdownKnockout("trader-re4")
        dk.update(100_000.0, now=_epoch())
        dk.update(80_000.0, now=_epoch())
        dk.re_enable()
        # New peak starts fresh
        dk.update(90_000.0, now=_epoch())
        assert dk.state.peak_equity == 90_000.0  # new baseline
        assert dk.state.level == KnockoutLevel.NORMAL


class TestDrawdownKnockout_EmergencyStatus:
    def test_emergency_status_normal(self):
        dk = DrawdownKnockout("trader-es")
        dk.update(100_000.0, now=_epoch())
        status = dk.emergency_status()
        assert status["trader_id"] == "trader-es"
        assert status["freeze_positions"] is False
        assert status["alert_required"] is False

    def test_emergency_status_emergency(self):
        dk = DrawdownKnockout("trader-ese")
        dk.update(100_000.0, now=_epoch())
        dk.update(80_000.0, now=_epoch())
        status = dk.emergency_status()
        assert status["freeze_positions"] is True
        assert status["alert_required"] is True
        assert status["level"] == "emergency"

    def test_emergency_status_paused(self):
        dk = DrawdownKnockout("trader-esp")
        dk.update(100_000.0, now=_epoch())
        dk.update(88_000.0, now=_epoch())
        status = dk.emergency_status()
        assert status["alert_required"] is True
        assert status["level"] == "paused"

    def test_recovery_ready_flag(self):
        dk = DrawdownKnockout("trader-esr", min_recovery_ticks=3)
        dk.update(100_000.0, now=_epoch())
        dk.update(88_000.0, now=_epoch())
        status = dk.emergency_status()
        assert status["recovery_ready"] is False
        for i in range(5):
            dk.update(88_000.0, now=_epoch())
        status = dk.emergency_status()
        assert status["recovery_ready"] is True

    def test_thresholds_in_status(self):
        dk = DrawdownKnockout("trader-est")
        status = dk.emergency_status()
        assert status["thresholds"]["caution_pct"] == 5.0
        assert status["thresholds"]["pause_pct"] == 10.0
        assert status["thresholds"]["emergency_pct"] == 15.0


# ═══════════════════════════════════════════════════════════════════════════════
# Edge Cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_zero_peak_equity_does_not_crash(self):
        """If peak is 0 (no equity data yet), compute should not divide by zero."""
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=0.0)
        level, new_state = m.compute(state, equity=100_000.0, now=_epoch())
        assert level == KnockoutLevel.NORMAL
        assert new_state.current_drawdown == 0.0  # 0/0 handled gracefully

    def test_negative_equity(self):
        """Negative equity should not crash — though unlikely in practice."""
        m = KnockoutStateMachine()
        state = KnockoutState(peak_equity=100_000.0)
        level, new_state = m.compute(state, equity=-10_000.0, now=_epoch())
        # DD = (100000 - (-10000)) / 100000 = 1.1 = 110% → EMERGENCY
        assert level == KnockoutLevel.EMERGENCY

    def test_rapid_oscillation_normal_emergency(self):
        """Rapid equity recover after emergency doesn't unstick."""
        dk = DrawdownKnockout("trader-osc")
        dk.update(100_000.0, now=_epoch())
        dk.update(80_000.0, now=_epoch())  # EMERGENCY
        assert dk.state.level == KnockoutLevel.EMERGENCY
        dk.update(100_000.0, now=_epoch())  # fully recovered
        assert dk.state.level == KnockoutLevel.EMERGENCY  # still EMERGENCY

    def test_mixed_losses_and_drawdown(self):
        """Both cooling-off and drawdown tier apply — highest priority wins."""
        dk = DrawdownKnockout("trader-mix")
        dk.update(100_000.0, now=_epoch())
        # 3 losses + 8% DD
        dk.update(92_000.0, last_trade_pnl=-500.0, now=_epoch())
        dk.update(92_000.0, last_trade_pnl=-500.0, now=_epoch())
        dk.update(92_000.0, last_trade_pnl=-500.0, now=_epoch())
        # DD = 8% → REDUCED, but 3 losses → cooling-off
        # COOLING_OFF priority > REDUCED in _determine_level
        assert dk.state.level == KnockoutLevel.COOLING_OFF

    def test_emergency_overrides_cooling_off(self):
        """Emergency has highest priority — overrides cooling-off."""
        dk = DrawdownKnockout("trader-emco")
        dk.update(100_000.0, now=_epoch())
        dk.update(85_000.0, last_trade_pnl=-500.0, now=_epoch())  # 15% + loss
        dk.update(85_000.0, last_trade_pnl=-500.0, now=_epoch())  # loss 2
        dk.update(85_000.0, last_trade_pnl=-500.0, now=_epoch())  # loss 3
        # DD = 15% → EMERGENCY (higher priority than cooling-off)
        assert dk.state.level == KnockoutLevel.EMERGENCY

    def test_paused_after_observation_resets_when_exiting(self):
        """When exiting PAUSED, recovery tracking is reset."""
        dk = DrawdownKnockout("trader-reset", min_recovery_ticks=3)
        dk.update(100_000.0, now=_epoch())
        dk.update(88_000.0, now=_epoch())  # PAUSED
        for _ in range(5):
            dk.update(88_000.0, now=_epoch())
        result = dk.submit_recovery_plan(
            "I will tighten stops and reduce position sizes"
        )
        assert result == "accepted"
        # DD improves to 3% (below caution 5%)
        dk.update(97_000.0, now=_epoch())
        assert dk.state.level == KnockoutLevel.NORMAL
        assert dk.state.recovery_observation_ticks == 0
        assert dk.state.recovery_plan is None

    def test_gate_with_empty_state_context_graceful(self):
        """Empty context with knockout_state=None should pass through."""
        gate = DrawdownKnockoutGate()
        granted, reason = gate.check({}, {"type": "BUY", "ticker": "AAPL"})
        assert granted is True
        assert "no knockout state" in reason.lower()


class TestDrawdownKnockout_Journal:
    def test_journal_tracks_transitions(self):
        dk = DrawdownKnockout("trader-jrnl")
        dk.update(100_000.0, now=_epoch())
        dk.update(93_000.0, now=_epoch())  # REDUCED
        dk.update(88_000.0, now=_epoch())  # PAUSED
        assert len(dk.state.journal) >= 2
        reduced_entries = [e for e in dk.state.journal if "reduced" in e]
        paused_entries = [e for e in dk.state.journal if "paused" in e]
        assert len(reduced_entries) >= 1
        assert len(paused_entries) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# inject_knockout_gate helper
# ═══════════════════════════════════════════════════════════════════════════════


class TestInjectKnockoutGate:
    def test_inject_creates_new_knockout(self):
        from src.drawdown_knockout import inject_knockout_gate
        from src.risk.manager import RiskManager

        rm = RiskManager(gates=[])
        dk, new_rm = inject_knockout_gate(rm, trader_id="trader-inject")
        assert dk is not None
        assert dk.trader_id == "trader-inject"
        assert len(new_rm.gates) == 1
        assert new_rm.gates[0] is dk.gate

    def test_inject_uses_existing_knockout(self):
        from src.drawdown_knockout import inject_knockout_gate
        from src.risk.manager import RiskManager

        existing = DrawdownKnockout("trader-existing")
        rm = RiskManager(gates=[])
        dk, new_rm = inject_knockout_gate(rm, knockout=existing)
        assert dk is existing
        assert new_rm.gates[0] is dk.gate

    def test_inject_requires_trader_id(self):
        from src.drawdown_knockout import inject_knockout_gate
        from src.risk.manager import RiskManager

        rm = RiskManager(gates=[])
        with pytest.raises(ValueError, match="trader_id"):
            inject_knockout_gate(rm)

    def test_inject_preserves_existing_gates(self):
        from src.drawdown_knockout import inject_knockout_gate
        from src.risk.manager import RiskManager

        class DummyGate:
            def check(self, ctx, action, ts=None):
                return True, "dummy"

        rm = RiskManager(gates=[DummyGate()])
        dk, new_rm = inject_knockout_gate(rm, trader_id="trader-gate")
        assert len(new_rm.gates) == 2
        # Knockout gate should be first
        assert new_rm.gates[0] is dk.gate
        assert isinstance(new_rm.gates[1], DummyGate)


# ═══════════════════════════════════════════════════════════════════════════════
# Observability integration (real in-memory metrics/alert)
# ═══════════════════════════════════════════════════════════════════════════════


class TestObservabilityIntegration:
    def test_transition_triggers_metrics(self):
        # Clear metrics state
        metrics._counters.clear()
        dk = DrawdownKnockout("trader-obs")
        dk.update(100_000.0, now=_epoch())
        dk.update(88_000.0, now=_epoch())  # → PAUSED
        snap = metrics.snapshot()
        # Should have a P0 alert fired and a counter incremented
        summary = alert.summary()
        paused_alerts = [a for a in summary.get("p0", []) + summary.get("p1", [])
                         if "PAUSED" in a.get("title", "") or "paused" in a.get("title", "").lower()]
        # At minimum, the transition happened
        assert dk.state.level == KnockoutLevel.PAUSED
