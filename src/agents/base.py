"""Base VirtualTrader class — common interface for all AI virtual trading agents.

Each virtual trader:
1. Receives a TickContext (market data + portfolio snapshot)
2. Evaluates signals according to its strategy/persona
3. Returns a list of VirtualDecision objects

This base class provides:
- Data bus connection helpers (shared with virtual_runner)
- Portfolio state tracking (cash, positions, P&L)
- Journal management
- Common utility methods (position sizing, risk checks)

Subclass for each persona: define `evaluate()` and `system_prompt_template()`.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.agents.decision_schema import (
    Action,
    OrderType,
    PortfolioSnapshot,
    TimeInForce,
    TickContext,
    TickDecisions,
    VirtualDecision,
)

log = logging.getLogger("virtual_trader")


# ── Position Tracking ────────────────────────────────────────────────────────


@dataclass
class OpenPosition:
    """An open position held by a virtual trader."""
    symbol: str
    shares: int
    entry_price: float
    entry_time: str
    current_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.shares * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return self.shares * (self.current_price - self.entry_price)

    @property
    def return_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price


@dataclass
class TraderPortfolio:
    """In-memory portfolio for one virtual trader."""
    trader_name: str = ""
    cash: float = 0.0
    positions: Dict[str, OpenPosition] = field(default_factory=dict)
    initial_equity: float = 0.0

    @property
    def total_equity(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    @property
    def position_count(self) -> int:
        return len(self.positions)

    @property
    def total_pnl(self) -> float:
        return self.total_equity - self.initial_equity

    @property
    def drawdown(self) -> float:
        if self.initial_equity <= 0:
            return 0.0
        return max(0.0, (self.initial_equity - self.total_equity) / self.initial_equity)

    def to_snapshot(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            cash=self.cash,
            total_equity=self.total_equity,
            positions={
                sym: {
                    "shares": pos.shares,
                    "entry_price": pos.entry_price,
                    "current_price": pos.current_price,
                    "unrealized_pnl": pos.unrealized_pnl,
                    "return_pct": pos.return_pct,
                }
                for sym, pos in self.positions.items()
            },
            day_pnl=0.0,
            total_pnl=self.total_pnl,
            drawdown=self.drawdown,
            position_count=self.position_count,
        )


# ── Data Bus Helpers ─────────────────────────────────────────────────────────


class DataBusClient:
    """Lightweight HTTP client for the paper trading data bus.

    Reuses the same fetch helpers from virtual_runner.py so we don't
    duplicate connection logic here. If the data bus isn't available,
    fall back to TickContext data provided by the orchestrator.
    """

    def __init__(self, base_url: str = "http://192.168.1.25:5000"):
        self.base_url = base_url.rstrip("/")

    def get_quotes(self, symbols: List[str]) -> Dict[str, dict]:
        """Fetch live quotes from /quotes endpoint."""
        import urllib.request
        url = f"{self.base_url}/quotes?symbols={','.join(symbols)}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                return data.get("quotes", {})
        except Exception as e:
            log.debug("Data bus /quotes unavailable: %s", e)
            return {}

    def get_signals(self, symbols: List[str]) -> Dict[str, dict]:
        """Fetch pre-computed momentum signals from /signals/momentum."""
        import urllib.request
        url = f"{self.base_url}/signals/momentum?symbols={','.join(symbols)}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                return data.get("signals", {})
        except Exception as e:
            log.debug("Data bus /signals/momentum unavailable: %s", e)
            return {}


# ── Base VirtualTrader ────────────────────────────────────────────────────────


class VirtualTrader(ABC):
    """Abstract base for all virtual trader personas.

    Subclasses must implement:
        evaluate(tick_context) -> List[VirtualDecision]

    Subclasses should implement:
        system_prompt_template() -> str  (for LLM-based decisions)
        persona_id() -> str              (unique persona name)

    Lifecycle:
        1. Orchestrator calls connect() to initialize portfolio
        2. For each tick: prepare() -> evaluate() -> submit()
        3. Orchestrator logs decisions to DB
    """

    def __init__(
        self,
        trader_name: str,
        starting_cash: float = 10_000.0,
        data_bus_url: str = "http://192.168.1.25:5000",
        config: Optional[Dict[str, Any]] = None,
    ):
        self.trader_name = trader_name
        self.config = config or {}
        self.starting_cash = starting_cash
        self.portfolio = TraderPortfolio(
            trader_name=trader_name,
            cash=starting_cash,
            initial_equity=starting_cash,
        )
        self.data_bus = DataBusClient(base_url=data_bus_url)
        self.journal: List[str] = []
        self._tick_count = 0
        self._last_tick_id: Optional[str] = None

    # ── Abstract methods (must override) ────────────────────────────────────

    @abstractmethod
    def evaluate(self, tick_context: TickContext) -> List[VirtualDecision]:
        """Evaluate the current tick and return decisions.

        This is the core strategy method. Each persona implements
        its own signal processing and decision logic here.
        """
        ...

    @abstractmethod
    def persona_id(self) -> str:
        """Return a unique identifier for this trader persona."""
        ...

    @abstractmethod
    def system_prompt_template(self) -> str:
        """Return the system prompt template for this persona.

        Used when this trader delegates decisions to an LLM.
        """
        ...

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Initialize the trader — called once before the first tick.

        Subclasses can override for setup (loading models, etc.).
        """
        log.info("VirtualTrader %s connected (cash: $%.2f)",
                 self.trader_name, self.portfolio.cash)

    def prepare(self, tick_id: str, timestamp: str) -> None:
        """Prepare for a new tick cycle. Called before each evaluate()."""
        self._tick_count += 1
        self._last_tick_id = tick_id

    def submit(self, decisions: List[VirtualDecision]) -> TickDecisions:
        """Package decisions into a TickDecisions for the orchestrator.

        Updates portfolio state based on decisions.
        """
        # Apply decisions to in-memory portfolio
        for d in decisions:
            self._apply_decision(d)

        return TickDecisions(
            trader_name=self.trader_name,
            trader_persona=self.persona_id(),
            tick_id=self._last_tick_id or "unknown",
            decisions=decisions,
            portfolio_snapshot=self.portfolio.to_snapshot(),
        )

    def run_tick(self, tick_context: TickContext) -> TickDecisions:
        """Full tick cycle: prepare -> evaluate -> submit.

        This is the main entry point called by the orchestrator or runner.
        """
        self.prepare(tick_context.tick_id, tick_context.timestamp)
        decisions = self.evaluate(tick_context)
        return self.submit(decisions)

    def disconnect(self) -> None:
        """Cleanup — called once when the trader is removed or the session ends."""
        log.info("VirtualTrader %s disconnected after %d ticks. Final equity: $%.2f",
                 self.trader_name, self._tick_count, self.portfolio.total_equity)

    # ── Portfolio management ────────────────────────────────────────────────

    def update_positions(self, quotes: Dict[str, Dict[str, Any]]) -> None:
        """Update current prices for all open positions."""
        for sym, pos in self.portfolio.positions.items():
            quote = quotes.get(sym, {})
            pos.current_price = quote.get("price", 0.0) or quote.get("close", pos.current_price)

    def journal_entry(self, message: str) -> None:
        """Add a journal entry with timestamp."""
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.journal.append(f"[{ts}] {message}")

    def get_recent_journal(self, limit: int = 10) -> List[str]:
        """Get the most recent journal entries."""
        return self.journal[-limit:]

    # ── Position sizing heuristics ──────────────────────────────────────────

    def position_size(
        self,
        price: float,
        conviction: float,
        max_pct_portfolio: float = 0.20,
    ) -> int:
        """Calculate position size in shares based on risk and conviction.

        Args:
            price: Current price per share.
            conviction: Trader conviction 0.0–1.0.
            max_pct_portfolio: Max % of portfolio to allocate to one position.

        Returns:
            Number of shares to buy (integer).
        """
        max_allocation = self.portfolio.total_equity * max_pct_portfolio
        risk_adjusted = max_allocation * conviction
        shares = int(risk_adjusted / price) if price > 0 else 0
        return max(0, shares)

    def max_risk_per_trade(self, conviction: float) -> float:
        """Calculate max risk $ for one trade based on conviction."""
        base_risk = self.portfolio.total_equity * 0.02  # 2% base risk
        return base_risk * conviction

    # ── Position limits ─────────────────────────────────────────────────────

    def has_position(self, symbol: str) -> bool:
        """Check if we have an open position in this symbol."""
        return symbol in self.portfolio.positions

    def position_count_for_base(self, base_trader: str) -> int:
        """Count positions grouped by base trader strategy label."""
        return self.portfolio.position_count

    def is_overconcentrated(self, symbol: str, max_pct: float = 0.25) -> bool:
        """Check if adding to this symbol would over-concentrate."""
        pos = self.portfolio.positions.get(symbol)
        if not pos:
            return False
        current_pct = pos.market_value / self.portfolio.total_equity
        return current_pct >= max_pct

    # ── Internal helpers ────────────────────────────────────────────────────

    def _apply_decision(self, decision: VirtualDecision) -> None:
        """Apply a decision to the in-memory portfolio.

        Does NOT execute real trades — the orchestrator handles DB logging.
        This keeps the in-memory state consistent for the next tick.
        """
        p = self.portfolio
        sym = decision.symbol

        if decision.action == Action.BUY:
            cost = decision.quantity * (decision.limit_price or p.positions.get(sym, OpenPosition(sym, 0, 0, "")).current_price or 0.0)
            if cost <= p.cash:
                p.cash -= cost
                if sym in p.positions:
                    # Average into existing position
                    existing = p.positions[sym]
                    total_shares = existing.shares + decision.quantity
                    total_cost = existing.shares * existing.entry_price + cost
                    existing.shares = total_shares
                    existing.entry_price = total_cost / total_shares if total_shares > 0 else 0.0
                else:
                    p.positions[sym] = OpenPosition(
                        symbol=sym,
                        shares=decision.quantity,
                        entry_price=decision.limit_price or 0.0,
                        entry_time=decision.timestamp or datetime.now(timezone.utc).isoformat(),
                    )
            else:
                log.debug("Trader %s: insufficient cash to BUY %d %s ($%.2f needed, $%.2f available)",
                          self.trader_name, decision.quantity, sym, cost, p.cash)

        elif decision.action == Action.SELL:
            pos = p.positions.get(sym)
            if pos:
                close_qty = min(decision.quantity, pos.shares)
                price = decision.limit_price or pos.current_price
                proceeds = close_qty * price
                p.cash += proceeds
                pos.shares -= close_qty
                if pos.shares <= 0:
                    del p.positions[sym]

        elif decision.action == Action.CLOSE:
            pos = p.positions.pop(sym, None)
            if pos:
                price = decision.limit_price or pos.current_price
                p.cash += pos.shares * price

        # HOLD — no action needed