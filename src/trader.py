"""Trader integration — wires signal engine, safety, replay, and decisions together.

This is the runtime trader loop that will run on Monday. It composes:
  - SignalEngine (§3): computes indicators from market data
  - CircuitBreaker (§8): enforces drawdown limits
  - ChangeGovernor (§12): controls parameter evolution
  - ReplayHarness (§2): backtests parameter changes
  - SignalBoard (§7): publishes observations

Lifecycle:
  tick arrives → signal engine → safety check → decision → execution → journal
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from src.replay import ReplayResult, Tick, Portfolio, TraderDecision
from src.safety import BreakerLevel, CircuitBreaker, ChangeGovernor
from src.signals import SignalEngine, SignalParams, SignalReport
from src.metrics import objective_score
from src.risk.manager import RiskManager
from src.risk.stop_loss import StopLossManager
from src.circuit_breaker import AgentCircuitBreaker, get_breaker
from src.observability import metrics, alert

log = logging.getLogger("trader")


# ═══════════════════════════════════════════════════════════════════════════════
# Trader State
# ═══════════════════════════════════════════════════════════════════════════════


class TraderMode(Enum):
    WARMUP = "warmup"       # First 10 days: half positions, no tuning
    LIVE = "live"            # Normal trading
    SHADOW = "shadow"        # A/B testing, no real orders
    PAUSED = "paused"        # Drawdown triggered pause
    EMERGENCY = "emergency"  # Human must re-enable


@dataclass
class TraderJournal:
    """Per-tick journal entry — what the trader was thinking."""
    timestamp: datetime
    ticker: str
    price: float
    regime: str
    signal_composite: float
    signal_conviction: float
    breaker_level: str
    decision: str
    rationale: str
    position_count: int
    equity: float
    drawdown: float


@dataclass
class TraderState:
    """Full trader state at any point in time."""
    mode: TraderMode = TraderMode.WARMUP
    equity: float = 100_000.0
    cash: float = 100_000.0
    positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    trades_closed: int = 0
    trades_won: int = 0
    total_pnl: float = 0.0
    ticks_processed: int = 0
    warmup_ticks_remaining: int = 10  # 10 trading days
    journal: List[TraderJournal] = field(default_factory=list)
    equity_history: List[float] = field(default_factory=list)
    return_history: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.trades_closed == 0:
            return 0.0
        return self.trades_won / self.trades_closed

    @property
    def drawdown(self) -> float:
        if not self.equity_history:
            return 0.0
        peak = max(self.equity_history)
        if peak <= 0:
            return 0.0
        return (peak - self.equity) / peak

    @property
    def ready_for_live(self) -> bool:
        """Can this trader exit warmup and go live?"""
        return (
            self.trades_closed >= 20
            and self.warmup_ticks_remaining <= 0
            and self.total_pnl > 0  # positive expectancy
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Trader Runner
# ═══════════════════════════════════════════════════════════════════════════════


class Trader:
    """A self-improving paper trading agent.

    Composes signal engine, circuit breaker, and change governor into
    a complete trading loop. Designed to run autonomously on OpenClaw
    as an agent, receiving ticks from the data bus.

    Args:
        trader_id: Name (kairos, aldridge, stonks).
        params: Signal engine parameters.
        initial_balance: Starting cash.
        max_journal_entries: Keep last N journal entries.
    """

    def __init__(
        self,
        trader_id: str,
        params: Optional[SignalParams] = None,
        initial_balance: float = 100_000.0,
        max_journal_entries: int = 100,
    ):
        self.trader_id = trader_id
        self.signal_engine = SignalEngine(params=params)
        self.breaker = CircuitBreaker(trader_id)
        self.governor = ChangeGovernor(trader_id)
        self.max_journal_entries = max_journal_entries

        # Agent tool-loop circuit breaker — prevents runaway tool calls
        self.agent_breaker = get_breaker(trader_id)

        # Build sector lookup from fundamentals DB (if available)
        self.risk_manager = RiskManager()
        self.stop_loss = StopLossManager()

        self.state = TraderState(
            equity=initial_balance,
            cash=initial_balance,
        )

        # Decision function — can be swapped for LLM agent or virtual trader variant
        self._decider: Optional[Callable] = None

    def _build_sector_lookup(self) -> Optional[Callable[[str], Optional[str]]]:
        """Build a sector-lookup function from the fundamentals database.

        Returns None if fundamentals DB is unavailable, causing SectorGate
        to classify all tickers as 'Unknown'.
        """
        try:
            from src.fundamentals import Fundamentals
            # Load all tickers with known sectors into a cache
            try:
                fundamentals_list = Fundamentals.load_all()
                sector_map: Dict[str, str] = {}
                for f in fundamentals_list:
                    if f.ticker and f.sector:
                        sector_map[f.ticker.upper()] = f.sector
                if sector_map:
                    log.info("RiskManager: loaded sectors for %d tickers", len(sector_map))
                    return lambda t: sector_map.get(t.upper())
            except Exception as e:
                log.warning("trader: %s", e)
        except ImportError:
            pass
        return None

    def _lookup_sector(self, ticker: str) -> Optional[str]:
        """Look up sector for a ticker using the sector lookup."""
        lookup = self._build_sector_lookup()
        if lookup:
            try:
                return lookup(ticker)
            except Exception as e:
                log.warning("trader: %s", e)
        return None

    def _build_risk_context(self) -> Dict[str, Any]:
        """Build the portfolio context dict for RiskManager.evaluate()."""
        positions = []
        for ticker, pos in self.state.positions.items():
            price = pos.get("current_price", pos.get("entry_price", 0))
            positions.append({
                "ticker": ticker,
                "quantity": pos.get("shares", 0),
                "market_value": pos.get("shares", 0) * price,
                "sector": pos.get("sector"),
            })
        return {
            "portfolio_value": self.state.equity,
            "cash": self.state.cash,
            "positions": positions,
            "day_trades": getattr(self.state, "day_trades", []),
        }

    def _decision_to_action(
        self, decision: TraderDecision, tick: Any
    ) -> Dict[str, Any]:
        """Convert a TraderDecision to RiskManager action dict."""
        ticker = decision.ticker or tick.ticker
        price = tick.close
        return {
            "type": decision.decision,
            "ticker": ticker,
            "quantity": decision.shares,
            "price": price,
            "conviction": decision.conviction,
        }

    # ── Tick processing ─────────────────────────────────────────────────────

    def process_tick(
        self,
        tick: Any,  # Tick-like: has .ticker, .close, .timestamp
        market_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[TraderDecision]:
        """Process one market tick — the main trading loop.

        Args:
            tick: Market data for this moment.
            market_context: Optional SPY trend, VIX, sector data.

        Returns:
            TraderDecision if action taken, None if HOLD or blocked.
        """
        ticker = tick.ticker
        price = tick.close

        # 0. Agent circuit breaker guard — skip if paused
        if self.agent_breaker.is_paused():
            _, reason = self.agent_breaker.check_paused()
            # Build a minimal signal report for journaling
            from datetime import datetime as dt
            ts = getattr(tick, "timestamp", dt.now())
            zero_signal = SignalReport(
                ticker=ticker, timestamp=ts,
                momentum_score=0.0, momentum_signal="neutral",
                rsi=50.0, rsi_signal="neutral",
                volatility=0.0, volatility_regime="unknown",
                regime="PAUSED", regime_confidence=0.0, regime_weight=0.0,
                recommended_size_pct=0.0, max_positions=0,
                stop_loss=0.0, take_profit=0.0,
                composite_signal=0.0, conviction=0.0,
            )
            self._journal(tick, zero_signal, "SKIPPED",
                          f"Circuit breaker paused: {reason}")
            log.warning("[%s] Tick SKIPPED — circuit breaker paused: %s",
                        self.trader_id, reason)
            metrics.increment("trader.tick.skipped",
                              tags={"trader": self.trader_id, "reason": reason})
            return None

        self.state.ticks_processed += 1
        metrics.increment("trader.tick.received",
                          tags={"trader": self.trader_id})

        # 1. Signal engine: compute indicators
        try:
            # Pass bootstrap=True when no positions open — this bypasses
            # volume filter so traders can build initial positions.
            bootstrap = len(self.state.positions) == 0
            signal: SignalReport = self.signal_engine.process(tick, bootstrap=bootstrap)
        except Exception as exc:
            log.error("[%s] Signal engine failed for %s: %s", self.trader_id, ticker, exc)
            alert.p1(
                f"Signal engine error: {self.trader_id}",
                {"trader_id": self.trader_id, "ticker": ticker, "error": str(exc)},
            )
            metrics.increment("trader.error.signal_engine",
                              tags={"trader": self.trader_id, "ticker": ticker})
            return None

        try:
            self.breaker.update(self.state.equity)
        except Exception as exc:
            log.error("[%s] Drawdown breaker update failed: %s", self.trader_id, exc)
            metrics.increment("trader.error.breaker_update",
                              tags={"trader": self.trader_id, "ticker": ticker})
            # Cannot assess risk — block this tick
            self._journal(tick, signal, "BLOCKED", f"Breaker update error: {exc}")
            return None

        if not self.breaker.state.can_trade:
            self._journal(tick, signal, "BLOCKED", f"Breaker: {self.breaker.state.level.value}")
            self.agent_breaker.mark_decision()  # drawdown breaker is a valid stop
            metrics.increment("trader.decision.blocked",
                              tags={"trader": self.trader_id, "gate": "circuit_breaker"})
            return None

        # 2. STOP-LOSS CHECK — enforce before any new trade decisions
        try:
            breached = self.stop_loss.check_all(
                positions=self.state.positions,
                current_prices={tick.ticker: price},
            )
        except Exception as exc:
            log.error("[%s] Stop-loss check_all failed: %s", self.trader_id, exc)
            alert.p1(
                f"Stop-loss check failed: {self.trader_id}",
                {"trader_id": self.trader_id, "ticker": tick.ticker, "error": str(exc)},
            )
            metrics.increment("trader.error.stop_loss_check",
                              tags={"trader": self.trader_id})
            breached = []

        for breach in breached:
            bt = breach["ticker"]
            if bt in self.state.positions:
                pos = self.state.positions[bt]
                try:
                    sell_decision = TraderDecision(
                        ticker=bt,
                        decision="SELL",
                        shares=pos["shares"],
                        conviction=1.0,
                        rationale=f"Stop-loss ({breach['stop_type']}): {breach['reason']}",
                    )
                    self._simulate_fill(tick, sell_decision)
                    self._journal(tick, signal, "SELL", sell_decision.rationale)
                    self.stop_loss.record_exit(bt)
                    metrics.increment("trader.stop_loss.triggered",
                                      tags={"trader": self.trader_id, "ticker": bt,
                                            "stop_type": breach.get("stop_type", "unknown")})
                    log.warning(
                        "[%s] STOP-LOSS triggered: %s",
                        self.trader_id, sell_decision.rationale,
                    )
                except Exception as exc:
                    log.error(
                        "[%s] Stop-loss execution failed for %s: %s",
                        self.trader_id, bt, exc,
                    )
                    alert.p1(
                        f"Stop-loss execution failed: {self.trader_id}",
                        {"trader_id": self.trader_id, "ticker": bt, "error": str(exc)},
                    )
                    metrics.increment("trader.error.stop_loss_exec",
                                      tags={"trader": self.trader_id, "ticker": bt})

        # 3. Position sizing: apply breaker multiplier + warmup reduction
        size_mult = self.breaker.state.position_multiplier
        if self.state.mode == TraderMode.WARMUP:
            size_mult *= 0.5  # half size during warmup

        # 4. Make decision
        try:
            if self._decider:
                decision = self._decider(tick, signal, self.state, market_context)
            else:
                decision = self._default_decider(tick, signal)
        except Exception as exc:
            log.error("[%s] Decider failed for %s: %s", self.trader_id, ticker, exc)
            alert.p1(
                f"Decider error: {self.trader_id}",
                {"trader_id": self.trader_id, "ticker": ticker, "error": str(exc)},
            )
            metrics.increment("trader.error.decider",
                              tags={"trader": self.trader_id, "ticker": ticker})
            return None

        if decision is None or decision.decision == "HOLD":
            self._journal(tick, signal, "HOLD", "No signal or conviction too low")
            self.agent_breaker.mark_decision()  # HOLD is a valid decision
            metrics.increment("trader.decision.hold",
                              tags={"trader": self.trader_id})
            return None

        # 5. Apply position sizing
        try:
            if decision.decision == "BUY":
                max_cost = self.state.equity * signal.recommended_size_pct * size_mult
                decision.shares = int(max_cost / price) if price > 0 else 0
        except Exception as exc:
            log.error("[%s] Position sizing failed for %s: %s", self.trader_id, ticker, exc)
            metrics.increment("trader.error.position_sizing",
                              tags={"trader": self.trader_id, "ticker": ticker})
            decision.shares = 0

        # 5a. Risk gate evaluation (concentration, sector, exposure, cash, PDT)
        if decision.decision == "BUY":
            try:
                risk_action = self._decision_to_action(decision, tick)
                risk_context = self._build_risk_context()
                granted, reason, gate_results = self.risk_manager.evaluate(
                    action=risk_action,
                    portfolio=risk_context,
                    positions=list(risk_context.get("positions", [])),
                    timestamp=getattr(tick, "timestamp", None),
                )
                if not granted:
                    log.warning("[%s] Risk gate blocked %s: %s", self.trader_id, ticker, reason)
                    self._journal(tick, signal, "BLOCKED", reason)
                    self.agent_breaker.mark_decision()  # blocked is a valid outcome
                    metrics.increment("trader.decision.blocked",
                                      tags={"trader": self.trader_id, "gate": "risk_manager",
                                            "ticker": ticker})
                    return None
            except Exception as exc:
                log.error("[%s] Risk gate evaluation failed for %s: %s", self.trader_id, ticker, exc)
                metrics.increment("trader.error.risk_gate",
                                  tags={"trader": self.trader_id, "ticker": ticker})
                decision.shares = 0
                return None

        # 6. Execute (simulated in paper; real would call Alpaca) + post-fill steps
        try:
            self._simulate_fill(tick, decision)

            # 7. Metrics on trade decision
            decision_type = decision.decision.lower()
            metrics.increment(
                f"trader.decision.{decision_type}",
                tags={"trader": self.trader_id, "ticker": ticker},
            )
            metrics.increment("trader.tick.processed",
                              tags={"trader": self.trader_id})

            # 8. Journal
            self._journal(tick, signal, decision.decision, decision.rationale)

            # 9. Warmup check
            if self.state.mode == TraderMode.WARMUP:
                self.state.warmup_ticks_remaining -= 1
                if self.state.ready_for_live:
                    self.state.mode = TraderMode.LIVE
                    log.info("[%s] Exiting warmup — going LIVE", self.trader_id)

            # 10. Mark decision made — prevents timeout gate from tripping
            self.agent_breaker.mark_decision()
            return decision

        except Exception as exc:
            log.error("[%s] Trade execution failed for %s: %s", self.trader_id, ticker, exc)
            alert.p1(
                f"Trade execution error: {self.trader_id}",
                {
                    "trader_id": self.trader_id,
                    "ticker": ticker,
                    "decision": decision.decision,
                    "error": str(exc),
                },
            )
            metrics.increment("trader.error.execution",
                              tags={"trader": self.trader_id, "ticker": ticker})
            return None

    def _default_decider(
        self, tick: Any, signal: SignalReport
    ) -> Optional[TraderDecision]:
        """Default rule-based decider. Replace with LLM agent or virtual trader variant.

        Simple rules:
          - Composite > 0.3 AND conviction > 0.3 → BUY
          - Composite < -0.3 AND have position → SELL
          - Otherwise → HOLD
        """
        ticker = tick.ticker
        has_position = ticker in self.state.positions

        if signal.conviction < 0.3:
            return TraderDecision(
                ticker=ticker, decision="HOLD", conviction=signal.conviction,
                rationale=f"Conviction too low ({signal.conviction:.2f})",
            )

        if signal.composite_signal > 0.3 and not has_position:
            return TraderDecision(
                ticker=ticker, decision="BUY", conviction=signal.conviction,
                rationale=f"Bullish signal ({signal.composite_signal:.2f}), regime={signal.regime}",
            )

        if signal.composite_signal < -0.3 and has_position:
            return TraderDecision(
                ticker=ticker, decision="SELL", conviction=signal.conviction,
                rationale=f"Bearish signal ({signal.composite_signal:.2f}), taking profit/cutting loss",
            )

        return TraderDecision(
            ticker=ticker, decision="HOLD", conviction=signal.conviction,
        )

    def set_decider(self, fn: Callable) -> None:
        """Replace the decision function (e.g., with LLM agent or virtual trader variant)."""
        self._decider = fn

    def track_tool_call(self, tool_name: str, args: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[str]]:
        """Track one tool call through the agent circuit breaker.

        Call this from the OpenClaw agent side when making tool calls
        (web_search, get_quotes, etc.) during tick processing.

        Returns:
            (True, None) if the call is allowed.
            (False, reason) if the circuit breaker tripped.
        """
        return self.agent_breaker.track(tool_name, args)

    # ── Execution ────────────────────────────────────────────────────────────

    def _simulate_fill(self, tick: Any, decision: TraderDecision) -> None:
        """Simulate order fill at close price (paper trading)."""
        ticker = decision.ticker or tick.ticker
        price = tick.close

        if decision.decision == "BUY":
            cost = decision.shares * price
            if cost > self.state.cash:
                decision.shares = int(self.state.cash / price)
                cost = decision.shares * price
            if decision.shares <= 0:
                return

            self.state.cash -= cost

            if ticker in self.state.positions:
                # Average in
                pos = self.state.positions[ticker]
                total_shares = pos["shares"] + decision.shares
                total_cost = pos["shares"] * pos["entry_price"] + cost
                pos["shares"] = total_shares
                pos["entry_price"] = total_cost / total_shares
            else:
                self.state.positions[ticker] = {
                    "shares": decision.shares,
                    "entry_price": price,
                    "entry_time": tick.timestamp,
                    "sector": self._lookup_sector(ticker),
                }
                # Register with stop-loss manager
                self.stop_loss.set_entry(ticker, price)

        elif decision.decision == "SELL":
            if ticker not in self.state.positions:
                return

            pos = self.state.positions[ticker]
            shares = min(decision.shares, pos["shares"]) if decision.shares > 0 else pos["shares"]
            proceeds = shares * price
            pnl = shares * (price - pos["entry_price"])

            self.state.cash += proceeds
            self.state.total_pnl += pnl
            self.state.trades_closed += 1
            if pnl > 0:
                self.state.trades_won += 1

            # Update breaker with trade result
            self.breaker.update(self.state.equity, last_trade_pnl=pnl)

            if shares >= pos["shares"]:
                del self.state.positions[ticker]
                # Clean up stop-loss tracking on full exit
                self.stop_loss.record_exit(ticker)
            else:
                pos["shares"] -= shares

        # Update equity
        self._update_equity()

    def _update_equity(self) -> None:
        """Recalculate equity from cash + positions."""
        position_value = sum(
            pos["shares"] * pos.get("current_price", pos["entry_price"])
            for pos in self.state.positions.values()
        )
        prev = self.state.equity
        self.state.equity = self.state.cash + position_value
        self.state.equity_history.append(self.state.equity)
        if prev > 0:
            self.state.return_history.append((self.state.equity - prev) / prev)

        # Emit portfolio gauges for observability dashboards
        trader_tag = {"trader": self.trader_id}
        metrics.gauge("trader.portfolio.equity", self.state.equity)
        metrics.gauge("trader.portfolio.cash", self.state.cash)
        metrics.gauge("trader.portfolio.position_count", float(len(self.state.positions)))
        metrics.gauge("trader.portfolio.drawdown_pct", float(self.state.drawdown * 100))

    def update_position_prices(self, prices: Dict[str, float]) -> None:
        """Update mark-to-market prices for open positions."""
        for ticker, price in prices.items():
            if ticker in self.state.positions:
                self.state.positions[ticker]["current_price"] = price
        self._update_equity()

    # ── Journal ──────────────────────────────────────────────────────────────

    def _journal(
        self, tick: Any, signal: SignalReport,
        decision: str, rationale: str,
    ) -> None:
        try:
            entry = TraderJournal(
                timestamp=tick.timestamp if hasattr(tick, "timestamp") else datetime.now(),
                ticker=tick.ticker,
                price=tick.close,
                regime=signal.regime,
                signal_composite=signal.composite_signal,
                signal_conviction=signal.conviction,
                breaker_level=self.breaker.state.level.value,
                decision=decision,
                rationale=rationale,
                position_count=len(self.state.positions),
                equity=self.state.equity,
                drawdown=self.state.drawdown,
            )
            self.state.journal.append(entry)
            if len(self.state.journal) > self.max_journal_entries:
                self.state.journal.pop(0)
        except Exception as exc:
            log.error("[%s] Journal write failed: %s", self.trader_id, exc)
            metrics.increment("trader.error.journal",
                              tags={"trader": self.trader_id})

    def recent_journal(self, n: int = 10) -> List[TraderJournal]:
        """Get last N journal entries."""
        return self.state.journal[-n:]

    # ── Metrics ──────────────────────────────────────────────────────────────

    def compute_objective(self) -> float:
        """Compute current objective score from equity/trade history."""
        if len(self.state.return_history) < 2:
            return 0.0

        returns = np.array(self.state.return_history, dtype=np.float64)
        equity = np.array(self.state.equity_history, dtype=np.float64)

        # Build trade PnL list (approximate from position delta + closed trades)
        trade_pnls = []
        # For now, use the trade history from breaker/state
        # In production this would come from a trade DB

        return objective_score(returns, equity, trade_pnls)

    # ── Status ───────────────────────────────────────────────────────────────

    def status_report(self) -> Dict[str, Any]:
        """One-shot status report for dashboard/canvas."""
        return {
            "trader": self.trader_id,
            "mode": self.state.mode.value,
            "equity": round(self.state.equity, 2),
            "cash": round(self.state.cash, 2),
            "total_pnl": round(self.state.total_pnl, 2),
            "drawdown_pct": round(self.state.drawdown * 100, 2),
            "positions": len(self.state.positions),
            "trades_closed": self.state.trades_closed,
            "win_rate": round(self.state.win_rate, 3),
            "ticks_processed": self.state.ticks_processed,
            "breaker_level": self.breaker.state.level.value,
            "monthly_changes": self.governor.state.monthly_changes,
            "max_monthly_changes": self.governor.max_monthly_changes,
            "signal_params": self.signal_engine.params.to_dict(),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Trader Factory
# ═══════════════════════════════════════════════════════════════════════════════


def create_trader(
    trader_id: str,
    initial_balance: float = 100_000.0,
    params_override: Optional[Dict[str, float]] = None,
) -> Trader:
    """Create a trader with sensible defaults.

    Args:
        trader_id: One of kairos, aldridge, stonks.
        initial_balance: Starting paper balance.
        params_override: Optional parameter overrides.

    Returns:
        Configured Trader ready to process ticks.
    """
    params = SignalParams()

    # Trader-specific defaults (conservative starting points)
    if trader_id == "kairos":
        # Momentum trader — higher momentum sensitivity
        params.set("momentum_threshold", 0.50)
        params.set("base_size_pct", 0.18)
    elif trader_id == "aldridge":
        # Value trader — more positions, smaller sizes
        params.set("base_size_pct", 0.12)
        params.set("max_positions", 7)
        params.set("weight_mean_reverting", 1.0)
    elif trader_id == "stonks":
        # Sentiment trader — responsive to volatility
        params.set("vol_reduction_multiplier", 0.5)
        params.set("weight_high_volatility", 0.6)

    # Apply user overrides last (they take precedence)
    if params_override:
        for name, value in params_override.items():
            params.set(name, value)

    return Trader(trader_id=trader_id, params=params, initial_balance=initial_balance)


def create_fleet(
    initial_balance: float = 100_000.0,
) -> Dict[str, Trader]:
    """Create all three traders (kairos, aldridge, stonks).

    Returns:
        Dict of trader_id → Trader.
    """
    return {
        name: create_trader(name, initial_balance=initial_balance)
        for name in ("kairos", "aldridge", "stonks")
    }
