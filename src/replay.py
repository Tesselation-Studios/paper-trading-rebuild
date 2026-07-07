"""Replay harness — SPEC-v3 §2, §3, §4.3.

Replays historical market data through a trader, tick by tick, tracking
the full portfolio state and trade history. The output feeds directly
into objective_score() for the learning loop.

This is the core engine behind:
  - Gradient descent (intraday): perturb params, re-replay last N ticks
  - Prompt sweeps (nightly): 100 variants replayed on yesterday's data
  - Walk-forward validation (§6): expanding windows with holdout folds
  - CI verification: replay known data, assert score meets threshold

Usage:
    from src.replay import ReplayHarness, replay_trader

    harness = ReplayHarness(initial_balance=100_000)
    result = harness.run(market_data, trader_fn)
    # result.equity_curve, result.trades, result.metrics
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from numpy.typing import NDArray

log = logging.getLogger("replay")

# ── Data types ───────────────────────────────────────────────────────────────


@dataclass
class Tick:
    """One moment in market time. Feeds into the trader at each step."""

    timestamp: datetime
    ticker: str
    open: float
    high: float
    low: float
    close: float
    volume: int

    # Optional derived data that signal engines may compute
    rsi: Optional[float] = None
    momentum: Optional[float] = None
    volatility: Optional[float] = None
    regime: Optional[str] = None


@dataclass
class Position:
    """An open position during replay."""

    ticker: str
    shares: int
    entry_price: float
    entry_time: datetime
    current_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.shares * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return self.shares * (self.current_price - self.entry_price)


@dataclass
class Trade:
    """A completed (closed) trade."""

    ticker: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    return_pct: float
    decision: str  # "BUY" | "SELL"


@dataclass
class Portfolio:
    """Current portfolio state during replay."""

    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)

    @property
    def total_equity(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    @property
    def position_count(self) -> int:
        return len(self.positions)


@dataclass
class TraderDecision:
    """What the trader decided at this tick."""

    ticker: str
    decision: str  # "BUY" | "SELL" | "HOLD"
    conviction: float  # 0.0 - 1.0
    rationale: str = ""
    shares: int = 0
    signal_override: bool = False


@dataclass
class ReplayResult:
    """Complete output of a replay run — feeds into metrics + learning loop."""

    equity_curve: NDArray[np.float64]
    returns: NDArray[np.float64]
    trades: List[Trade]
    initial_balance: float
    final_equity: float
    total_pnl: float          # net P&L when cost model is active, gross otherwise
    total_return_pct: float
    n_ticks: int
    n_decisions: int  # how many non-HOLD decisions were made
    tickers_seen: List[str]

    # Cost-adjusted fields (populated when cost_model is active)
    gross_pnl: float = 0.0     # P&L before transaction costs
    total_cost: float = 0.0    # total transaction cost deducted

    @property
    def positive_trades(self) -> List[Trade]:
        return [t for t in self.trades if t.pnl > 0]

    @property
    def negative_trades(self) -> List[Trade]:
        return [t for t in self.trades if t.pnl < 0]

    @property
    def trade_pnls(self) -> List[float]:
        return [t.pnl for t in self.trades]

    @property
    def net_trade_pnls(self) -> List[float]:
        """Trade PnLs net of transaction costs (falls back to gross if no costs)."""
        return [getattr(t, "pnl_net", t.pnl) for t in self.trades]

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return len(self.positive_trades) / len(self.trades)

    @property
    def net_win_rate(self) -> float:
        """Win rate computed on net (cost-adjusted) PnL."""
        if not self.trades:
            return 0.0
        net_wins = [t for t in self.trades if getattr(t, "pnl_net", t.pnl) > 0]
        return len(net_wins) / len(self.trades)


# ── Trader function type ─────────────────────────────────────────────────────

# A trader callable receives (tick, portfolio) and returns a TraderDecision.
TraderFn = Callable[[Tick, Portfolio], TraderDecision]


# ── Replay Harness ───────────────────────────────────────────────────────────


class ReplayHarness:
    """Tick-by-tick replay engine.

    Iterates through historical market data, calls the trader at each step,
    simulates fills at the close price, tracks the full portfolio and trade log.

    Args:
        initial_balance: Starting cash in dollars.
        commission_per_share: Per-share commission (default 0.0 for paper).
        max_position_pct: Max fraction of equity per position (default 20%).
        require_conviction: Minimum conviction to execute a trade (default 0.0).
        cost_model: Optional CostModel for transaction cost adjustment.
            When set, ReplayResult.total_pnl reflects net P&L (gross - costs)
            and each Trade gets a pnl_net attribute.
    """

    def __init__(
        self,
        initial_balance: float = 100_000.0,
        commission_per_share: float = 0.0,
        max_position_pct: float = 0.20,
        require_conviction: float = 0.0,
        cost_model: Any = None,
    ):
        self.initial_balance = initial_balance
        self.commission_per_share = commission_per_share
        self.max_position_pct = max_position_pct
        self.require_conviction = require_conviction
        self.cost_model = cost_model

        # Reset per run
        self._portfolio: Portfolio = Portfolio(cash=initial_balance)
        self._trades: List[Trade] = []
        self._equity: List[float] = []
        self._returns: List[float] = []
        self._decision_count: int = 0
        self._tickers_seen: List[str] = []

    def run(
        self,
        market_data: List[Tick],
        trader: TraderFn,
    ) -> ReplayResult:
        """Run a full replay.

        Args:
            market_data: Chronological list of ticks (earliest first).
            trader: Callable that takes (Tick, Portfolio) → TraderDecision.

        Returns:
            ReplayResult with equity curve, returns, trades, and summary stats.
        """
        self._reset()
        prev_equity = self.initial_balance

        for tick in market_data:
            if tick.ticker not in self._tickers_seen:
                self._tickers_seen.append(tick.ticker)

            # Update any open positions' current prices
            for pos in self._portfolio.positions.values():
                if pos.ticker == tick.ticker:
                    pos.current_price = tick.close

            # Call the trader
            try:
                decision = trader(tick, self._portfolio)
            except Exception as e:
                log.warning("Trader raised at %s: %s — treating as HOLD", tick.timestamp, e)
                decision = TraderDecision(
                    ticker=tick.ticker,
                    decision="HOLD",
                    conviction=0.0,
                    rationale=f"ERROR: {e}",
                )

            if decision.decision != "HOLD":
                self._decision_count += 1

            # Execute the decision
            self._execute(tick, decision)

            # Record equity snapshot
            current_equity = self._portfolio.total_equity
            self._equity.append(current_equity)
            if prev_equity > 0:
                self._returns.append((current_equity - prev_equity) / prev_equity)
            else:
                self._returns.append(0.0)
            prev_equity = current_equity

        return self._build_result(len(market_data))

    # ── Internal methods ─────────────────────────────────────────────────

    def _reset(self) -> None:
        self._portfolio = Portfolio(cash=self.initial_balance)
        self._trades = []
        self._equity = []
        self._returns = []
        self._decision_count = 0
        self._tickers_seen = []

    def _execute(self, tick: Tick, decision: TraderDecision) -> None:
        """Simulate fill at close price.

        Conviction gating only applies to BUY (entry) decisions.
        SELL orders are risk management / exit decisions and always
        pass through — stop-loss and take-profit must never be blocked.
        """
        if decision.decision == "HOLD":
            return

        if decision.decision == "BUY" and decision.conviction < self.require_conviction:
            log.debug("BUY blocked: conviction %.2f < required %.2f",
                      decision.conviction, self.require_conviction)
            return

        if decision.signal_override and decision.conviction < self.require_conviction:
            log.debug(
                "Decision passed via signal_override (conviction %.2f < required %.2f)",
                decision.conviction,
                self.require_conviction,
            )

        price = tick.close

        if decision.decision == "BUY":
            self._execute_buy(tick, decision, price)
        elif decision.decision == "SELL":
            self._execute_sell(tick, decision, price)

    def _execute_buy(self, tick: Tick, decision: TraderDecision, price: float) -> None:
        """Open a new long position or add to existing."""
        # Determine shares: use decision.shares if specified, else size by conviction
        if decision.shares > 0:
            shares = decision.shares
        else:
            max_cost = self._portfolio.total_equity * self.max_position_pct * decision.conviction
            shares = int(max_cost / price) if price > 0 else 0

        if shares <= 0:
            return

        cost = shares * price + shares * self.commission_per_share
        if cost > self._portfolio.cash:
            # Scale down to available cash
            affordable_shares = int(self._portfolio.cash / (price + self.commission_per_share))
            if affordable_shares <= 0:
                return
            shares = affordable_shares
            cost = shares * price + shares * self.commission_per_share

        self._portfolio.cash -= cost

        ticker = decision.ticker or tick.ticker
        if ticker in self._portfolio.positions:
            # Average down/up
            pos = self._portfolio.positions[ticker]
            total_shares = pos.shares + shares
            total_cost = (pos.shares * pos.entry_price) + (shares * price)
            pos.shares = total_shares
            pos.entry_price = total_cost / total_shares if total_shares > 0 else 0.0
            pos.current_price = price
        else:
            self._portfolio.positions[ticker] = Position(
                ticker=ticker,
                shares=shares,
                entry_price=price,
                entry_time=tick.timestamp,
                current_price=price,
            )

    def _execute_sell(self, tick: Tick, decision: TraderDecision, price: float) -> None:
        """Close an existing position (or reduce it)."""
        ticker = decision.ticker or tick.ticker
        if ticker not in self._portfolio.positions:
            return

        pos = self._portfolio.positions[ticker]
        shares_to_sell = decision.shares if decision.shares > 0 else pos.shares
        shares_to_sell = min(shares_to_sell, pos.shares)

        if shares_to_sell <= 0:
            return

        proceeds = shares_to_sell * price - shares_to_sell * self.commission_per_share
        self._portfolio.cash += proceeds

        pnl = shares_to_sell * (price - pos.entry_price)
        return_pct = (price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.0

        self._trades.append(Trade(
            ticker=ticker,
            entry_time=pos.entry_time,
            exit_time=tick.timestamp,
            entry_price=pos.entry_price,
            exit_price=price,
            shares=shares_to_sell,
            pnl=pnl,
            return_pct=return_pct,
            decision=decision.decision,
        ))

        # Reduce or remove position
        if shares_to_sell >= pos.shares:
            del self._portfolio.positions[ticker]
        else:
            pos.shares -= shares_to_sell

    def _build_result(self, n_ticks: int) -> ReplayResult:
        final_equity = self._portfolio.total_equity

        # Close any remaining open positions at last price to compute final P&L
        unrealized_pnl = sum(p.unrealized_pnl for p in self._portfolio.positions.values())
        gross_pnl = final_equity - self.initial_balance + unrealized_pnl

        total_cost = 0.0
        net_pnl = gross_pnl

        # Apply transaction cost model if configured
        if self.cost_model is not None:
            # Build a temporary result for the cost model
            temp = ReplayResult(
                equity_curve=np.array(self._equity, dtype=np.float64),
                returns=np.array([], dtype=np.float64),
                trades=list(self._trades),
                initial_balance=self.initial_balance,
                final_equity=final_equity,
                total_pnl=gross_pnl,
                total_return_pct=(final_equity - self.initial_balance) / self.initial_balance * 100,
                n_ticks=n_ticks,
                n_decisions=self._decision_count,
                tickers_seen=list(dict.fromkeys(self._tickers_seen)),
            )
            total_cost = self.cost_model.apply_to_result(temp)
            net_pnl = gross_pnl - total_cost
            # copy the updated trades back (they now have pnl_net)
            self._trades = list(temp.trades)

        return ReplayResult(
            equity_curve=np.array(self._equity, dtype=np.float64),
            returns=np.array(self._returns, dtype=np.float64),
            trades=list(self._trades),
            initial_balance=self.initial_balance,
            final_equity=final_equity,
            total_pnl=net_pnl,
            total_return_pct=(final_equity - self.initial_balance) / self.initial_balance * 100,
            n_ticks=n_ticks,
            n_decisions=self._decision_count,
            tickers_seen=list(dict.fromkeys(self._tickers_seen)),
            gross_pnl=gross_pnl,
            total_cost=total_cost,
        )


# ── Convenience function ─────────────────────────────────────────────────────


def replay_trader(
    market_data: List[Tick],
    trader: TraderFn,
    initial_balance: float = 100_000.0,
    **harness_kwargs: Any,
) -> ReplayResult:
    """One-line replay: feed market data through a trader, get results.

    Args:
        market_data: Chronological tick data.
        trader: Callable (Tick, Portfolio) → TraderDecision.
        initial_balance: Starting cash.
        **harness_kwargs: Passed to ReplayHarness (max_position_pct, etc.).

    Returns:
        ReplayResult with full equity curve, trades, and metrics.
    """
    harness = ReplayHarness(initial_balance=initial_balance, **harness_kwargs)
    return harness.run(market_data, trader)


# ── Test helpers ─────────────────────────────────────────────────────────────


def make_dummy_tick(
    ticker: str = "AAPL",
    price: float = 150.0,
    timestamp: Optional[datetime] = None,
    **overrides: Any,
) -> Tick:
    """Create a realistic Tick for testing.

    Args:
        ticker: Stock symbol.
        price: Close price (open/high/low derived from this).
        timestamp: Datetime (defaults to now).
        **overrides: Any Tick field overrides.

    Returns:
        A Tick dataclass instance.
    """
    ts = timestamp or datetime(2024, 1, 2, 10, 0, 0)
    spread = price * 0.005  # 0.5% spread
    kwargs = {
        "timestamp": ts,
        "ticker": ticker,
        "open": price - spread / 2,
        "high": price + spread,
        "low": price - spread,
        "close": price,
        "volume": 1_000_000,
        "rsi": 50.0,
        "momentum": 0.0,
        "volatility": 0.15,
        "regime": "TRENDING_UP",
    }
    kwargs.update(overrides)
    return Tick(**kwargs)


def make_uptrend_ticks(
    ticker: str = "AAPL",
    n: int = 50,
    start_price: float = 100.0,
    drift: float = 0.15,
    noise: float = 0.02,
    seed: int = 42,
) -> List[Tick]:
    """Generate a synthetic uptrend tick series for testing.

    Geometric Brownian motion with positive drift.

    Args:
        ticker: Stock symbol.
        n: Number of ticks.
        start_price: Initial price.
        drift: Daily drift rate (0.15 = 15% annual).
        noise: Daily volatility (0.02 = 2%).
        seed: Random seed.

    Returns:
        List of Tick objects in chronological order.
    """
    rng = np.random.default_rng(seed)
    returns = drift / 252 + noise * rng.standard_normal(n)
    prices = start_price * np.exp(np.cumsum(returns))

    ticks = []
    base_time = datetime(2024, 1, 2, 9, 30, 0)
    for i, price in enumerate(prices):
        ts = base_time + np.timedelta64(i, "D").astype("timedelta64[s]").item()
        ticks.append(make_dummy_tick(
            ticker=ticker,
            price=float(price),
            timestamp=ts,
            momentum=float(returns[i]) * 100,
            regime="TRENDING_UP",
        ))
    return ticks


def make_deterministic_uptrend_ticks(
    ticker: str = "AAPL",
    n: int = 50,
    start_price: float = 100.0,
    step_pct: float = 0.005,  # 0.5% per tick
) -> List[Tick]:
    """Generate guaranteed-uptrend ticks for reliable testing.

    Each tick's close is step_pct higher than the previous. No randomness.
    Use when you need a known outcome (buy-and-hold MUST profit).

    Args:
        ticker: Stock symbol.
        n: Number of ticks.
        start_price: Initial price.
        step_pct: Price increase per tick (0.005 = 0.5%).

    Returns:
        List of Tick objects in chronological order with monotonically rising prices.
    """
    ticks = []
    base_time = datetime(2024, 1, 2, 9, 30, 0)
    price = start_price
    for i in range(n):
        ts = base_time + np.timedelta64(i, "D").astype("timedelta64[s]").item()
        ticks.append(make_dummy_tick(
            ticker=ticker,
            price=round(price, 2),
            timestamp=ts,
            momentum=step_pct * 252 * 10,  # strong momentum signal
            regime="TRENDING_UP",
        ))
        price *= (1 + step_pct)
    return ticks


def make_random_walk_ticks(
    ticker: str = "SPY",
    n: int = 50,
    start_price: float = 450.0,
    noise: float = 0.015,
    seed: int = 42,
) -> List[Tick]:
    """Generate synthetic random-walk ticks (no drift) for testing.

    Args:
        ticker: Stock symbol.
        n: Number of ticks.
        start_price: Initial price.
        noise: Daily volatility.
        seed: Random seed.

    Returns:
        List of Tick objects.
    """
    rng = np.random.default_rng(seed)
    returns = noise * rng.standard_normal(n)
    prices = start_price * np.exp(np.cumsum(returns))

    ticks = []
    base_time = datetime(2024, 1, 2, 9, 30, 0)
    for i, price in enumerate(prices):
        ts = base_time + np.timedelta64(i, "D").astype("timedelta64[s]").item()
        ticks.append(make_dummy_tick(
            ticker=ticker,
            price=float(price),
            timestamp=ts,
            volatility=noise,
            regime="SIDEWAYS",
        ))
    return ticks
