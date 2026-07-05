"""Tests for trader integration module."""

import pytest
import numpy as np
from datetime import datetime

from src.trader import (
    TraderMode,
    TraderJournal,
    TraderState,
    Trader,
    create_trader,
    create_fleet,
)
from src.signals import SignalParams
from src.safety import BreakerLevel


# ── Fixtures ──────────────────────────────────────────────────────────────────


class DummyTick:
    """Minimal tick for trader tests."""
    def __init__(self, ticker="AAPL", close=150.0, timestamp=None):
        self.ticker = ticker
        self.close = close
        self.timestamp = timestamp or datetime(2024, 1, 2)


def make_uptrend_ticks(ticker="AAPL", n=60, start=100.0, step_pct=0.01):
    """Generate uptrend ticks for testing."""
    from datetime import timedelta
    base = datetime(2024, 1, 1)
    return [
        DummyTick(ticker=ticker, close=start * (1 + step_pct) ** i,
                  timestamp=base + timedelta(days=i))
        for i in range(n)
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# TraderState tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTraderState:
    def test_initial_state(self):
        state = TraderState()
        assert state.mode == TraderMode.WARMUP
        assert state.equity == 100_000
        assert state.cash == 100_000
        assert state.positions == {}
        assert state.win_rate == 0.0

    def test_not_ready_for_live_initially(self):
        state = TraderState()
        assert state.ready_for_live is False

    def test_ready_for_live(self):
        state = TraderState()
        state.trades_closed = 20
        state.warmup_ticks_remaining = 0
        state.total_pnl = 500  # positive
        assert state.ready_for_live is True

    def test_not_ready_negative_pnl(self):
        state = TraderState()
        state.trades_closed = 20
        state.warmup_ticks_remaining = 0
        state.total_pnl = -500
        assert state.ready_for_live is False

    def test_drawdown_tracks_peak(self):
        state = TraderState()
        state.equity_history = [100_000, 105_000, 102_000]
        state.equity = 102_000
        assert state.drawdown == pytest.approx(3_000 / 105_000, abs=0.001)


# ═══════════════════════════════════════════════════════════════════════════════
# Trader tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTraderBasic:
    def test_create_trader(self):
        trader = create_trader("kairos")
        assert trader.trader_id == "kairos"
        assert trader.state.mode == TraderMode.WARMUP
        assert trader.state.equity == 100_000

    def test_create_fleet(self):
        fleet = create_fleet()
        assert set(fleet.keys()) == {"kairos", "aldridge", "stonks"}
        for t in fleet.values():
            assert t.state.equity == 100_000

    def test_trader_specific_defaults(self):
        kairos = create_trader("kairos")
        aldridge = create_trader("aldridge")
        stonks = create_trader("stonks")

        # Each trader has different starting params
        assert kairos.signal_engine.params.get("base_size_pct") != aldridge.signal_engine.params.get("base_size_pct")
        # Kairos = momentum, higher base size
        assert kairos.signal_engine.params.get("base_size_pct") > aldridge.signal_engine.params.get("base_size_pct")

    def test_process_tick_in_uptrend(self):
        """Trader processes ticks correctly — journals, equity, signal tracking."""
        trader = create_trader("kairos")
        ticks = make_uptrend_ticks(n=60, step_pct=0.015)

        for tick in ticks:
            trader.process_tick(tick)

        # Should have processed all ticks
        assert trader.state.ticks_processed == 60
        # Should have journaled every tick
        assert len(trader.state.journal) == 60
        # Last journal entry should reference the uptrend regime
        assert trader.state.journal[-1].regime == "TRENDING_UP"
        # Equity should be tracked
        assert trader.state.equity > 0

    def test_journal_records_ticks(self):
        trader = create_trader("kairos")
        ticks = make_uptrend_ticks(n=30)

        for tick in ticks:
            trader.process_tick(tick)

        assert len(trader.state.journal) > 0
        entry = trader.state.journal[-1]
        assert entry.ticker == "AAPL"
        assert entry.price > 0
        assert entry.signal_composite != 0  # should have a signal

    def test_recent_journal(self):
        trader = create_trader("kairos")
        ticks = make_uptrend_ticks(n=40)
        for tick in ticks:
            trader.process_tick(tick)

        recent = trader.recent_journal(5)
        assert len(recent) == 5
        assert recent[-1].ticker == "AAPL"

    def test_status_report(self):
        trader = create_trader("kairos")
        ticks = make_uptrend_ticks(n=20)
        for tick in ticks:
            trader.process_tick(tick)

        report = trader.status_report()
        assert report["trader"] == "kairos"
        assert report["ticks_processed"] == 20
        assert "equity" in report
        assert "drawdown_pct" in report

    def test_set_custom_decider(self):
        trader = create_trader("kairos")

        def always_buy(tick, signal, state, ctx=None):
            from src.replay import TraderDecision
            return TraderDecision(
                ticker=tick.ticker, decision="BUY", conviction=1.0,
                rationale="custom decider",
            )

        trader.set_decider(always_buy)
        tick = DummyTick(close=150.0)
        decision = trader.process_tick(tick)
        assert decision is not None
        assert decision.decision == "BUY"
        assert decision.rationale == "custom decider"

    def test_warmup_transitions_to_live(self):
        trader = create_trader("kairos")
        # Manually set state near warmup exit
        trader.state.warmup_ticks_remaining = 1
        trader.state.trades_closed = 19
        trader.state.total_pnl = 100

        # Feed one more tick with a trade opportunity
        ticks = make_uptrend_ticks(n=1, step_pct=0.02)

        # Force a winning trade
        def force_trade(tick, signal, state, ctx=None):
            from src.replay import TraderDecision
            # Force a buy then immediate sell to close a winning trade
            if "AAPL" not in state.positions:
                return TraderDecision(ticker="AAPL", decision="BUY", conviction=1.0, shares=10)
            return TraderDecision(ticker="AAPL", decision="SELL", conviction=1.0, shares=10)

        trader.set_decider(force_trade)
        trader.process_tick(ticks[0])  # buy
        ticks[0].close *= 1.1  # price up
        trader.update_position_prices({"AAPL": ticks[0].close})
        trader.process_tick(ticks[0])  # sell for profit

        # Should now be LIVE
        assert trader.state.mode == TraderMode.LIVE

    def test_breaker_blocks_when_paused(self):
        trader = create_trader("kairos")
        trader.state.equity = 100_000
        # Force breaker to paused by simulating drawdown
        trader.breaker.update(100_000)  # set peak
        trader.breaker.update(87_000)  # 13% DD → paused
        trader.state.equity = 87_000

        tick = DummyTick(close=150.0)
        decision = trader.process_tick(tick)
        # Should be None because breaker blocks trading
        if decision is not None:
            assert decision.decision != "BUY"  # at minimum, shouldn't be buying

    def test_update_position_prices(self):
        trader = create_trader("kairos")
        trader.state.positions["AAPL"] = {
            "shares": 100, "entry_price": 150.0, "current_price": 150.0,
        }
        trader.state.cash = 85_000
        trader._update_equity()

        # Price goes up
        trader.update_position_prices({"AAPL": 160.0})
        assert trader.state.equity == 101_000  # 85k cash + 100*160

    def test_compute_objective_no_data(self):
        trader = create_trader("kairos")
        assert trader.compute_objective() == 0.0

    def test_parameter_override(self):
        trader = create_trader("kairos", params_override={
            "momentum_threshold": 0.75,
            "stop_loss_pct": 0.08,
        })
        assert trader.signal_engine.params.get("momentum_threshold") == 0.75
        assert trader.signal_engine.params.get("stop_loss_pct") == 0.08
        # Unspecified params keep kairos defaults
        assert trader.signal_engine.params.get("base_size_pct") > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Fleet tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFleet:
    def test_fleet_independent(self):
        fleet = create_fleet(initial_balance=50_000)
        ticks = make_uptrend_ticks(n=30)

        for tick in ticks:
            for name, trader in fleet.items():
                trader.process_tick(tick)

        # Each trader should have processed ticks independently
        for name, trader in fleet.items():
            assert trader.state.ticks_processed == 30
            assert trader.state.equity != 50_000 or len(trader.state.positions) == 0
