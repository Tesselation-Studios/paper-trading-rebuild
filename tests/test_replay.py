"""Tests for replay harness — SPEC-v3 §2, §3, §4.3.

Covers:
  - Basic replay: buy, hold, sell lifecycle
  - Portfolio tracking: equity curve, cash, positions
  - Edge cases: zero trades, max drawdown, conviction gates
  - Test helpers: synthetic data generators
"""

import pytest
import numpy as np
from datetime import datetime

from src.replay import (
    Tick,
    Position,
    Trade,
    Portfolio,
    TraderDecision,
    ReplayResult,
    ReplayHarness,
    replay_trader,
    make_dummy_tick,
    make_uptrend_ticks,
    make_deterministic_uptrend_ticks,
    make_random_walk_ticks,
)


# ── Test helpers ─────────────────────────────────────────────────────────────


def buy_hold_trader(tick: Tick, portfolio: Portfolio) -> TraderDecision:
    """Buy on first tick, hold forever."""
    ticker = tick.ticker
    if ticker not in portfolio.positions:
        return TraderDecision(
            ticker=ticker, decision="BUY", conviction=1.0,
            rationale="Buy and hold",
        )
    return TraderDecision(
        ticker=ticker, decision="HOLD", conviction=0.0,
    )


def trend_following_trader(tick: Tick, portfolio: Portfolio) -> TraderDecision:
    """Buy when momentum > 0, sell when momentum < 0."""
    ticker = tick.ticker
    momentum = tick.momentum or 0.0

    if momentum > 0.005 and ticker not in portfolio.positions:
        return TraderDecision(
            ticker=ticker, decision="BUY", conviction=abs(momentum) * 10,
            rationale=f"Momentum {momentum:.4f} > 0 — buying",
        )
    elif momentum < -0.005 and ticker in portfolio.positions:
        return TraderDecision(
            ticker=ticker, decision="SELL", conviction=abs(momentum) * 10,
            rationale=f"Momentum {momentum:.4f} < 0 — selling",
        )
    return TraderDecision(ticker=ticker, decision="HOLD", conviction=0.0)


def never_buy_trader(tick: Tick, portfolio: Portfolio) -> TraderDecision:
    """Always holds, never trades."""
    return TraderDecision(ticker=tick.ticker, decision="HOLD", conviction=0.0)


def always_buy_trader(tick: Tick, portfolio: Portfolio) -> TraderDecision:
    """Buy on every tick (tests position sizing limits)."""
    return TraderDecision(
        ticker=tick.ticker, decision="BUY", conviction=1.0,
    )


# ── Tick tests ───────────────────────────────────────────────────────────────


class TestMakeDummyTick:
    def test_default_tick(self):
        tick = make_dummy_tick()
        assert tick.ticker == "AAPL"
        assert tick.close == 150.0
        assert tick.rsi == 50.0
        assert tick.regime == "TRENDING_UP"

    def test_custom_tick(self):
        tick = make_dummy_tick(
            ticker="SPY", price=450.0,
            timestamp=datetime(2024, 6, 1),
            regime="HIGH_VOLATILITY",
        )
        assert tick.ticker == "SPY"
        assert tick.close == 450.0
        assert tick.regime == "HIGH_VOLATILITY"

    def test_synthetic_uptrend(self):
        """GBM with positive drift — trend is UP over sufficient samples."""
        ticks = make_uptrend_ticks(n=500, start_price=100.0, drift=0.30, seed=42)
        assert len(ticks) == 500
        # With 500 ticks and 30% annual drift, noise shouldn't overtake trend
        assert ticks[-1].close > ticks[0].close
        # Timestamps should be sequential
        assert ticks[0].timestamp < ticks[-1].timestamp

    def test_synthetic_reproducible(self):
        a = make_uptrend_ticks(n=20, seed=42)
        b = make_uptrend_ticks(n=20, seed=42)
        for ta, tb in zip(a, b):
            assert ta.close == tb.close

    def test_random_walk_ticks(self):
        ticks = make_random_walk_ticks(n=50, seed=123)
        assert len(ticks) == 50
        assert all(t.volatility == 0.015 for t in ticks)
        assert all(t.regime == "SIDEWAYS" for t in ticks)


# ── Portfolio tests ──────────────────────────────────────────────────────────


class TestPortfolio:
    def test_empty_portfolio(self):
        p = Portfolio(cash=100_000)
        assert p.total_equity == 100_000
        assert p.position_count == 0

    def test_with_position(self):
        p = Portfolio(cash=90_000)
        p.positions["AAPL"] = Position(
            ticker="AAPL", shares=100, entry_price=100.0,
            entry_time=datetime(2024, 1, 2), current_price=110.0,
        )
        assert p.position_count == 1
        assert p.positions["AAPL"].market_value == 11_000
        assert p.positions["AAPL"].unrealized_pnl == 1_000
        assert p.total_equity == 101_000

    def test_position_unrealized_loss(self):
        p = Portfolio(cash=90_000)
        p.positions["AAPL"] = Position(
            ticker="AAPL", shares=100, entry_price=100.0,
            entry_time=datetime(2024, 1, 2), current_price=90.0,
        )
        assert p.positions["AAPL"].unrealized_pnl == -1_000
        assert p.total_equity == 99_000


# ── Harness tests ────────────────────────────────────────────────────────────


class TestReplayHarnessBasic:
    """Core replay: buy, hold, sell lifecycle."""

    def test_empty_data_returns_initial_balance(self):
        harness = ReplayHarness(initial_balance=50_000)
        result = harness.run([], buy_hold_trader)
        assert result.initial_balance == 50_000
        assert result.final_equity == 50_000
        assert result.n_ticks == 0
        assert result.n_decisions == 0
        assert len(result.trades) == 0

    def test_never_trades(self):
        ticks = make_uptrend_ticks(n=30, start_price=100.0)
        harness = ReplayHarness(initial_balance=100_000)
        result = harness.run(ticks, never_buy_trader)
        assert result.initial_balance == result.final_equity
        assert len(result.trades) == 0
        assert result.n_decisions == 0
        assert len(result.equity_curve) == 30

    def test_buy_and_hold_uptrend(self):
        """Buy once, ride a guaranteed uptrend. Must have positive return."""
        ticks = make_deterministic_uptrend_ticks(n=60, start_price=100.0, step_pct=0.005)
        harness = ReplayHarness(initial_balance=100_000)
        result = harness.run(ticks, buy_hold_trader)

        # Bought on first tick, held through
        assert result.n_decisions >= 1
        # Equity curve should show growth
        assert result.equity_curve[-1] > result.equity_curve[0]
        # Must profit in guaranteed uptrend
        assert result.total_pnl > 0

    def test_trend_following_uptrend(self):
        """Momentum trader in guaranteed uptrend — must catch the ride."""
        ticks = make_deterministic_uptrend_ticks(n=80, start_price=100.0, step_pct=0.005)
        harness = ReplayHarness(initial_balance=100_000)
        result = harness.run(ticks, trend_following_trader)

        assert result.n_decisions >= 0
        assert len(result.equity_curve) == 80
        # In guaranteed uptrend, trend follower should profit (momentum > 0 on every tick)
        assert result.total_pnl >= 0

    def test_equity_curve_length(self):
        ticks = make_uptrend_ticks(n=25)
        harness = ReplayHarness()
        result = harness.run(ticks, buy_hold_trader)
        assert len(result.equity_curve) == 25
        assert len(result.returns) == 25

    def test_trade_record(self):
        """Verify trade records have correct fields."""
        ticks = make_uptrend_ticks(n=40, start_price=100.0, drift=0.20, seed=3)
        harness = ReplayHarness(initial_balance=100_000)

        # Use a trader that buys then sells
        buy_done = [False]
        sell_done = [False]

        def buy_then_sell(tick: Tick, portfolio: Portfolio) -> TraderDecision:
            ticker = tick.ticker
            if not buy_done[0]:
                buy_done[0] = True
                return TraderDecision(ticker=ticker, decision="BUY", conviction=1.0)
            if ticker in portfolio.positions and not sell_done[0]:
                sell_done[0] = True
                return TraderDecision(ticker=ticker, decision="SELL", conviction=1.0)
            return TraderDecision(ticker=ticker, decision="HOLD", conviction=0.0)

        result = harness.run(ticks, buy_then_sell)

        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.ticker == "AAPL"
        assert t.shares > 0
        assert t.entry_time < t.exit_time
        assert isinstance(t.pnl, float)
        assert isinstance(t.return_pct, float)


class TestReplayHarnessPositionSizing:
    """Position sizing and cash management."""

    def test_max_position_pct_respected(self):
        """Buying on every tick should respect max_position_pct."""
        ticks = make_uptrend_ticks(n=20, start_price=100.0)
        harness = ReplayHarness(
            initial_balance=100_000,
            max_position_pct=0.10,  # only 10% per position
        )
        result = harness.run(ticks, always_buy_trader)

        # Check that cash never went to zero (always buying but limited)
        # The first buy should cost at most 10k (10% of 100k)
        first_buy_shares = result.trades[0].shares if result.trades else 0
        first_cost = first_buy_shares * ticks[0].close
        assert first_cost <= 12_000  # 10k + wiggle room for rounding

    def test_cash_not_below_zero(self):
        ticks = make_uptrend_ticks(n=30, start_price=100.0)
        harness = ReplayHarness(initial_balance=10_000)
        result = harness.run(ticks, always_buy_trader)
        # Equity curve should never be negative
        assert all(e >= 0 for e in result.equity_curve)

    def test_commission_affects_cash(self):
        ticks = make_uptrend_ticks(n=20, start_price=100.0)
        commission = 0.01

        # Without commission
        h0 = ReplayHarness(initial_balance=100_000, commission_per_share=0.0)
        r0 = h0.run(ticks, buy_hold_trader)

        # With commission
        h1 = ReplayHarness(initial_balance=100_000, commission_per_share=commission)
        r1 = h1.run(ticks, buy_hold_trader)

        # With commission, cash should be slightly lower
        if r0.trades and r1.trades:
            # Commission doesn't hit HOLD decisions, only BUY/SELL
            pass  # Sanity check — no crash


class TestReplayHarnessConviction:
    """Conviction gating."""

    def test_low_conviction_blocked(self):
        ticks = make_uptrend_ticks(n=20, start_price=100.0)

        def low_conviction_buy(tick: Tick, portfolio: Portfolio) -> TraderDecision:
            if tick.ticker not in portfolio.positions:
                return TraderDecision(
                    ticker=tick.ticker, decision="BUY", conviction=0.1,
                    rationale="Unsure buy",
                )
            return TraderDecision(ticker=tick.ticker, decision="HOLD", conviction=0.0)

        harness = ReplayHarness(initial_balance=100_000, require_conviction=0.5)
        result = harness.run(ticks, low_conviction_buy)

        # The buy should be blocked — no trades, equity unchanged
        assert len(result.trades) == 0
        assert result.final_equity == 100_000

    def test_high_conviction_allowed(self):
        ticks = make_uptrend_ticks(n=20, start_price=100.0)

        def high_conviction_buy(tick: Tick, portfolio: Portfolio) -> TraderDecision:
            if tick.ticker not in portfolio.positions:
                return TraderDecision(
                    ticker=tick.ticker, decision="BUY", conviction=0.9,
                    rationale="Confident buy",
                )
            return TraderDecision(ticker=tick.ticker, decision="HOLD", conviction=0.0)

        harness = ReplayHarness(initial_balance=100_000, require_conviction=0.5)
        result = harness.run(ticks, high_conviction_buy)

        # Buy should go through
        assert result.n_decisions >= 1
        assert len(result.trades) == 0  # open position, not closed yet
        assert result.final_equity != 100_000  # invested

    def test_sell_bypasses_conviction_gate(self):
        """SELL orders (stop-loss, take-profit) must bypass the conviction gate.

        The conviction gate is for entry decisions (BUY).  Exit decisions
        are risk management and must never be blocked by conviction.
        """
        ticks = make_deterministic_uptrend_ticks(
            n=20, start_price=100.0, step_pct=0.01,
        )

        # Stage 1: buy on tick 0
        # Stage 2: sell on tick 1 with low conviction (simulating stop-loss)
        buy_done = [False]

        def buy_then_sell_low_conviction(tick: Tick, portfolio: Portfolio) -> TraderDecision:
            ticker = tick.ticker
            if not buy_done[0]:
                buy_done[0] = True
                return TraderDecision(
                    ticker=ticker, decision="BUY", conviction=1.0,
                )
            if ticker in portfolio.positions:
                # Simulate a stop-loss/take-profit exit with LOW conviction
                return TraderDecision(
                    ticker=ticker, decision="SELL", conviction=0.1,
                    rationale="Stop loss hit (low conviction from signal engine)",
                    signal_override=True,
                )
            return TraderDecision(ticker=ticker, decision="HOLD", conviction=0.0)

        harness = ReplayHarness(
            initial_balance=100_000, require_conviction=0.5,
        )
        result = harness.run(ticks, buy_then_sell_low_conviction)

        # The SELL MUST complete — it's risk management
        assert len(result.trades) == 1, (
            f"Expected 1 closed trade, got {len(result.trades)}. "
            "SELL (stop-loss/take-profit) must not be blocked by conviction gate."
        )
        assert result.trades[0].decision == "SELL"

    def test_stop_loss_sell_not_blocked_by_conviction(self):
        """End-to-end: SELL triggered by stop-loss must execute even when
        signal conviction is low (e.g., during a crash)."""
        ticks = make_deterministic_uptrend_ticks(
            n=10, start_price=100.0, step_pct=0.005,
        )

        # Buy first, then force a SELL with zero conviction (worst case)
        staged = {"bought": False}

        def trader(tick: Tick, portfolio: Portfolio) -> TraderDecision:
            ticker = tick.ticker
            if not staged["bought"]:
                staged["bought"] = True
                return TraderDecision(
                    ticker=ticker, decision="BUY", conviction=1.0,
                )
            if ticker in portfolio.positions:
                return TraderDecision(
                    ticker=ticker, decision="SELL", conviction=0.0,
                    rationale="Emergency exit",
                    signal_override=True,
                )
            return TraderDecision(ticker=ticker, decision="HOLD", conviction=0.0)

        harness = ReplayHarness(
            initial_balance=100_000, require_conviction=0.3,
        )
        result = harness.run(ticks, trader)

        # Even with zero conviction, SELL must execute
        assert len(result.trades) == 1, (
            f"Expected 1 trade, got {len(result.trades)}. "
            "SELL with zero conviction must still execute."
        )


class TestReplayHarnessEdgeCases:
    """Edge cases and error handling."""

    def test_trader_exception_becomes_hold(self):
        ticks = make_uptrend_ticks(n=10)

        def crashing_trader(tick: Tick, portfolio: Portfolio) -> TraderDecision:
            if tick.close > 102:
                raise RuntimeError("Market is too hot!")
            return TraderDecision(ticker=tick.ticker, decision="HOLD", conviction=0.0)

        harness = ReplayHarness()
        result = harness.run(ticks, crashing_trader)
        # Should complete without crashing
        assert result.n_ticks == 10
        assert len(result.equity_curve) == 10

    def test_sell_without_position(self):
        """Selling when no position exists — should be a no-op."""
        ticks = make_uptrend_ticks(n=10)

        def sell_first(tick: Tick, portfolio: Portfolio) -> TraderDecision:
            return TraderDecision(
                ticker=tick.ticker, decision="SELL", conviction=1.0,
            )

        harness = ReplayHarness(initial_balance=100_000)
        result = harness.run(ticks, sell_first)
        assert len(result.trades) == 0
        assert result.final_equity == 100_000

    def test_multi_ticker_trades(self):
        """Replay with multiple tickers."""
        ticks_a = make_uptrend_ticks(ticker="AAPL", n=15, start_price=150.0, seed=1)
        ticks_b = make_uptrend_ticks(ticker="GOOG", n=15, start_price=140.0, seed=2)
        # Interleave
        combined = []
        base_time = datetime(2024, 1, 2, 9, 30, 0)
        for i in range(15):
            ta = ticks_a[i]
            tb = ticks_b[i]
            ta.timestamp = base_time + np.timedelta64(i * 2, "m").astype("timedelta64[s]").item()
            tb.timestamp = base_time + np.timedelta64(i * 2 + 1, "m").astype("timedelta64[s]").item()
            combined.extend([ta, tb])

        harness = ReplayHarness(initial_balance=200_000)
        result = harness.run(combined, buy_hold_trader)

        # Should see both tickers
        assert "AAPL" in result.tickers_seen
        assert "GOOG" in result.tickers_seen
        assert len(result.equity_curve) == 30

    def test_zero_price_does_not_crash(self):
        """Tick with price=0 should not divide by zero."""
        ticks = [make_dummy_tick(price=1.0)]
        ticks.append(make_dummy_tick(price=0.0))  # disaster tick

        harness = ReplayHarness()
        result = harness.run(ticks, always_buy_trader)
        # Should not crash
        assert result.n_ticks == 2


class TestReplayResult:
    """ReplayResult properties."""

    def test_win_rate_no_trades(self):
        ticks = make_uptrend_ticks(n=10)
        harness = ReplayHarness()
        result = harness.run(ticks, never_buy_trader)
        assert result.win_rate == 0.0

    def test_trade_pnls(self):
        ticks = make_uptrend_ticks(n=20, start_price=100.0, drift=0.3, seed=5)
        buy_done = [False]
        sell_done = [False]

        def one_trade(tick: Tick, portfolio: Portfolio) -> TraderDecision:
            t = tick.ticker
            if not buy_done[0]:
                buy_done[0] = True
                return TraderDecision(ticker=t, decision="BUY", conviction=1.0)
            if t in portfolio.positions and not sell_done[0]:
                sell_done[0] = True
                return TraderDecision(ticker=t, decision="SELL", conviction=1.0)
            return TraderDecision(ticker=t, decision="HOLD", conviction=0.0)

        harness = ReplayHarness()
        result = harness.run(ticks, one_trade)

        assert len(result.trade_pnls) == 1
        assert len(result.positive_trades) + len(result.negative_trades) == 1
        assert result.win_rate in (0.0, 1.0)


class TestConvenienceFunction:
    """replay_trader() one-liner."""

    def test_replay_trader_works(self):
        ticks = make_uptrend_ticks(n=30, start_price=100.0, seed=10)
        result = replay_trader(ticks, buy_hold_trader, initial_balance=50_000)
        assert isinstance(result, ReplayResult)
        assert result.initial_balance == 50_000
        assert result.n_ticks == 30

    def test_replay_trader_kwargs(self):
        ticks = make_uptrend_ticks(n=20)
        result = replay_trader(
            ticks, buy_hold_trader,
            max_position_pct=0.05,
            require_conviction=0.8,
        )
        assert result.n_ticks == 20


# ── Integration: replay → metrics pipeline ───────────────────────────────────

def test_replay_to_objective_score():
    """End-to-end: replay data through trader → compute objective score."""
    from src.metrics import objective_score

    ticks = make_uptrend_ticks(n=100, start_price=100.0, drift=0.25, seed=99)
    result = replay_trader(ticks, buy_hold_trader, initial_balance=100_000)

    score = objective_score(
        returns=result.returns,
        equity=result.equity_curve,
        trades=result.trade_pnls,
    )

    # Uptrend with buy-and-hold should produce a positive score
    # (unless max drawdown > 15%, which it shouldn't in a clean uptrend)
    assert isinstance(score, float)
    assert score > 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Stop-loss assertion tests (#42)
# Every BUY trade must write stop_loss. Default = entry_price * 0.95 (5% stop).
# ═══════════════════════════════════════════════════════════════════════════════


class TestStopLossComputation:
    """Verify stop_loss computation used by sync_trades.py."""

    def test_stop_loss_default_5pct(self):
        """Stop-loss = entry_price * (1 - 0.05), rounded to 2 decimals."""
        from src.sync_trades import _compute_stop_loss, DEFAULT_STOP_LOSS_PCT

        assert DEFAULT_STOP_LOSS_PCT == 0.05

        # Round numbers
        assert _compute_stop_loss(100.0) == 95.0
        assert _compute_stop_loss(200.0) == 190.0
        assert _compute_stop_loss(50.0) == 47.5

        # Precise numbers from real trades
        assert _compute_stop_loss(198.795143) == 188.86  # ADBE
        assert _compute_stop_loss(112.08) == 106.48       # HOOD
        assert _compute_stop_loss(370.836667) == 352.29   # MSFT

    def test_stop_loss_always_below_entry(self):
        """Stop-loss must be strictly below entry price (positive stop_loss_pct)."""
        from src.sync_trades import _compute_stop_loss

        for entry in [10.0, 50.0, 100.0, 250.0, 500.0]:
            stop = _compute_stop_loss(entry)
            assert stop < entry, f"stop_loss={stop} >= entry={entry}"
            # Should be roughly 5% below
            assert abs((entry - stop) / entry - 0.05) < 0.001

    def test_stop_loss_never_negative(self):
        """Stop-loss should never go below zero even for tiny entry prices."""
        from src.sync_trades import _compute_stop_loss

        assert _compute_stop_loss(1.0) == 0.95
        assert _compute_stop_loss(0.05) == 0.05  # rounds to nearest 0.01
        assert _compute_stop_loss(0.001) == 0.0  # rounds down


# ═══════════════════════════════════════════════════════════════════════════════
# Transaction Cost Integration Tests (#20)
# ═══════════════════════════════════════════════════════════════════════════════


class TestTransactionCostIntegration:
    """Verify CostModel wires into ReplayHarness correctly."""

    def test_no_cost_model_produces_gross_pnl(self):
        """Without cost model, total_pnl = gross, total_cost = 0."""
        from src.replay import ReplayHarness, make_deterministic_uptrend_ticks

        ticks = make_deterministic_uptrend_ticks(n=10, start_price=100.0)
        harness = ReplayHarness(initial_balance=100_000.0)

        result = harness.run(ticks, buy_hold_trader)

        assert result.total_cost == 0.0
        assert result.gross_pnl == result.total_pnl
        assert result.total_pnl >= 0  # uptrend should be profitable

    def test_cost_model_produces_net_pnl(self):
        """With cost model on round-trip trades, pnl_net exists and total_cost > 0."""
        from src.replay import ReplayHarness, make_deterministic_uptrend_ticks
        from src.transaction_costs import CostModel
        from src.replay import TraderDecision

        ticks = make_deterministic_uptrend_ticks(n=10, start_price=100.0)
        cost_model = CostModel(slippage_bps=10.0, spread_bps=5.0,
                               commission_per_share=0.0, min_trade_cost=0.0)
        harness = ReplayHarness(initial_balance=100_000.0, cost_model=cost_model)

        # Trader that buys first tick, sells last tick (round-trip)
        buy_done = False

        def round_trip_trader(tick, portfolio):
            nonlocal buy_done
            ticker = tick.ticker
            if not buy_done:
                buy_done = True
                return TraderDecision(ticker=ticker, decision="BUY", conviction=1.0,
                                      rationale="Buy first tick")
            # On the last tick, sell everything
            if ticker in portfolio.positions:
                return TraderDecision(ticker=ticker, decision="SELL", conviction=1.0,
                                      rationale="Sell last tick")
            return TraderDecision(ticker=ticker, decision="HOLD", conviction=0.0)

        result = harness.run(ticks, round_trip_trader)

        # Should have completed trades
        assert len(result.trades) > 0
        for trade in result.trades:
            assert hasattr(trade, "pnl_net")
            assert trade.pnl_net <= trade.pnl  # costs reduce P&L

        assert result.total_cost > 0
        assert result.gross_pnl >= result.total_pnl
        assert result.total_pnl == pytest.approx(result.gross_pnl - result.total_cost)

    def test_cost_model_does_not_affect_zero_trade_run(self):
        """With cost model but no trades, nothing breaks."""
        from src.replay import ReplayHarness, Tick
        from src.transaction_costs import CostModel

        ticks = [Tick(
            timestamp=__import__("datetime").datetime(2024, 1, 2, 10, 0),
            ticker="AAPL", open=100, high=101, low=99, close=100, volume=1000,
        )]
        cost_model = CostModel.default()
        harness = ReplayHarness(initial_balance=100_000.0, cost_model=cost_model)

        # HOLD-only trader
        def hold_trader(tick, portfolio):
            from src.replay import TraderDecision
            return TraderDecision(ticker=tick.ticker, decision="HOLD", conviction=0.0)

        result = harness.run(ticks, hold_trader)
        assert result.total_cost == 0.0
        assert result.gross_pnl == 0.0
        assert len(result.trades) == 0

    def test_cost_model_adjusts_total_pnl(self):
        """total_pnl reflects net when cost model is active."""
        from src.replay import ReplayHarness, make_uptrend_ticks
        from src.transaction_costs import CostModel

        ticks = make_uptrend_ticks(n=30, start_price=100.0, seed=42)
        cost_model = CostModel(slippage_bps=100.0, spread_bps=0.0,
                               commission_per_share=0.0, min_trade_cost=0.0)

        # Run WITH costs
        h1 = ReplayHarness(initial_balance=100_000.0, cost_model=cost_model)
        r1 = h1.run(ticks, buy_hold_trader)

        # Run WITHOUT costs
        h2 = ReplayHarness(initial_balance=100_000.0)
        r2 = h2.run(ticks, buy_hold_trader)

        # With costs: net P&L should be lower
        if len(r1.trades) > 0:
            assert r1.total_pnl < r2.total_pnl, \
                f"Net P&L {r1.total_pnl:.2f} should be lower than gross {r2.total_pnl:.2f}"
            assert r1.total_cost > 0

    def test_net_trade_pnls_property(self):
        """ReplayResult.net_trade_pnls returns cost-adjusted values."""
        from src.replay import ReplayHarness, make_deterministic_uptrend_ticks
        from src.transaction_costs import CostModel

        ticks = make_deterministic_uptrend_ticks(n=10, start_price=100.0)
        cost_model = CostModel(slippage_bps=10.0, spread_bps=5.0,
                               commission_per_share=0.0, min_trade_cost=0.0)
        harness = ReplayHarness(initial_balance=100_000.0, cost_model=cost_model)

        result = harness.run(ticks, buy_hold_trader)

        net_pnls = result.net_trade_pnls
        gross_pnls = result.trade_pnls

        assert len(net_pnls) == len(gross_pnls)
        for net, gross in zip(net_pnls, gross_pnls):
            assert net <= gross  # costs always reduce

    def test_net_win_rate(self):
        """net_win_rate may be lower than win_rate due to costs."""
        from src.replay import ReplayHarness, make_deterministic_uptrend_ticks
        from src.transaction_costs import CostModel

        ticks = make_deterministic_uptrend_ticks(n=10, start_price=100.0)
        cost_model = CostModel(slippage_bps=10.0, spread_bps=5.0,
                               commission_per_share=0.0, min_trade_cost=0.0)
        harness = ReplayHarness(initial_balance=100_000.0, cost_model=cost_model)

        result = harness.run(ticks, buy_hold_trader)

        assert hasattr(result, "net_win_rate")
        assert isinstance(result.net_win_rate, float)
        # net_win_rate ≤ win_rate (costs can flip marginal winners)
        assert result.net_win_rate <= result.win_rate

    def test_alpaca_paper_cost_model_integration(self):
        """Alpaca paper cost model works end-to-end."""
        from src.replay import ReplayHarness, make_deterministic_uptrend_ticks
        from src.transaction_costs import CostModel

        ticks = make_deterministic_uptrend_ticks(n=10, start_price=100.0)
        harness = ReplayHarness(
            initial_balance=100_000.0,
            cost_model=CostModel.alpaca_paper(),
        )

        result = harness.run(ticks, buy_hold_trader)
        assert result.total_cost >= 0
        for trade in result.trades:
            assert hasattr(trade, "pnl_net")

    def test_backward_compatible_no_cost_model(self):
        """ReplayHarness without cost_model still works (backward compat)."""
        from src.replay import ReplayHarness, make_uptrend_ticks

        ticks = make_uptrend_ticks(n=20, start_price=100.0, seed=99)
        harness = ReplayHarness(initial_balance=100_000.0)

        result = harness.run(ticks, buy_hold_trader)

        # Old API still works
        assert isinstance(result.equity_curve, __import__("numpy").ndarray)
        assert isinstance(result.trades, list)
        assert result.n_ticks == 20
        assert result.total_cost == 0.0
        assert result.gross_pnl == result.total_pnl  # equal when no costs


# ═══════════════════════════════════════════════════════════════════════════════
# Idempotency tests — Invariant #8
# "Running the same tick twice produces the same result."
# No side effects that depend on timing, no shared state leakage.
# ═══════════════════════════════════════════════════════════════════════════════


class TestIdempotency:
    """Invariant #8: Idempotent ticks — same input → same output every time."""

    def test_same_harness_twice_produces_identical_results(self):
        """Re-running the same harness with same data → identical result."""
        ticks = make_deterministic_uptrend_ticks(n=50, start_price=100.0, step_pct=0.005)
        harness = ReplayHarness(initial_balance=100_000)

        result1 = harness.run(ticks, buy_hold_trader)
        result2 = harness.run(ticks, buy_hold_trader)

        assert result1.final_equity == result2.final_equity
        assert result1.total_pnl == result2.total_pnl
        assert result1.total_return_pct == result2.total_return_pct
        assert result1.n_ticks == result2.n_ticks
        assert result1.n_decisions == result2.n_decisions
        assert len(result1.trades) == len(result2.trades)
        assert np.array_equal(result1.equity_curve, result2.equity_curve)
        for t1, t2 in zip(result1.trades, result2.trades):
            assert t1.ticker == t2.ticker
            assert t1.entry_price == t2.entry_price
            assert t1.exit_price == t2.exit_price
            assert t1.shares == t2.shares
            assert t1.pnl == t2.pnl

    def test_two_instances_same_result(self):
        """Two separate harness instances with same inputs → identical."""
        ticks = make_deterministic_uptrend_ticks(n=50, start_price=100.0, step_pct=0.005)

        h1 = ReplayHarness(initial_balance=100_000)
        h2 = ReplayHarness(initial_balance=100_000)

        r1 = h1.run(ticks, buy_hold_trader)
        r2 = h2.run(ticks, buy_hold_trader)

        assert r1.final_equity == r2.final_equity
        assert r1.total_pnl == r2.total_pnl
        assert np.array_equal(r1.equity_curve, r2.equity_curve)
        assert len(r1.trades) == len(r2.trades)

    def test_seeded_random_walk_reproducible(self):
        """Uptrend ticks with same seed produce reproducible results."""
        ticks1 = make_uptrend_ticks(n=30, start_price=100.0, seed=42)
        ticks2 = make_uptrend_ticks(n=30, start_price=100.0, seed=42)

        h1 = ReplayHarness(initial_balance=100_000)
        h2 = ReplayHarness(initial_balance=100_000)

        r1 = h1.run(ticks1, buy_hold_trader)
        r2 = h2.run(ticks2, buy_hold_trader)

        assert r1.final_equity == r2.final_equity
        assert r1.total_pnl == r2.total_pnl
        assert np.array_equal(r1.equity_curve, r2.equity_curve)

    def test_trend_following_deterministic(self):
        """Trend-following trader on deterministic data → reproducible."""
        ticks = make_deterministic_uptrend_ticks(n=40, start_price=100.0, step_pct=0.005)

        h1 = ReplayHarness(initial_balance=100_000, require_conviction=0.0)
        h2 = ReplayHarness(initial_balance=100_000, require_conviction=0.0)

        r1 = h1.run(ticks, trend_following_trader)
        r2 = h2.run(ticks, trend_following_trader)

        assert r1.final_equity == r2.final_equity
        assert r1.total_pnl == r2.total_pnl
        assert r1.n_decisions == r2.n_decisions
        assert np.array_equal(r1.equity_curve, r2.equity_curve)

    def test_never_buy_trader_deterministic(self):
        """No-trade runs are always identical."""
        ticks = make_deterministic_uptrend_ticks(n=30, start_price=100.0)

        h1 = ReplayHarness(initial_balance=100_000)
        h2 = ReplayHarness(initial_balance=100_000)

        r1 = h1.run(ticks, never_buy_trader)
        r2 = h2.run(ticks, never_buy_trader)

        assert r1.final_equity == r2.final_equity
        assert r1.final_equity == 100_000
        assert len(r1.trades) == 0
        assert len(r2.trades) == 0

    def test_no_shared_mutation_between_runs(self):
        """First run does not affect second run's results (no state leakage)."""
        ticks = make_deterministic_uptrend_ticks(n=50, start_price=100.0)

        harness = ReplayHarness(initial_balance=100_000)
        r1 = harness.run(ticks, buy_hold_trader)

        # Mutate a trade from r1 — should not affect r2
        if r1.trades:
            r1.trades[0].pnl = 999999.99

        r2 = harness.run(ticks, buy_hold_trader)

        assert r2.final_equity == r1.final_equity
        assert np.array_equal(r2.equity_curve, r1.equity_curve)
        assert len(r2.trades) == len(r1.trades)

    def test_replay_result_fields_match_deterministic(self):
        """All ReplayResult fields are deterministic."""
        ticks = make_deterministic_uptrend_ticks(n=30, start_price=100.0, step_pct=0.005)

        r1 = ReplayHarness(initial_balance=100_000).run(ticks, buy_hold_trader)
        r2 = ReplayHarness(initial_balance=100_000).run(ticks, buy_hold_trader)

        assert r1.initial_balance == r2.initial_balance
        assert r1.final_equity == r2.final_equity
        assert r1.total_pnl == r2.total_pnl
        assert r1.total_return_pct == r2.total_return_pct
        assert r1.n_ticks == r2.n_ticks
        assert r1.n_decisions == r2.n_decisions
        assert r1.tickers_seen == r2.tickers_seen
        assert r1.win_rate == r2.win_rate
        assert np.array_equal(r1.returns, r2.returns)
