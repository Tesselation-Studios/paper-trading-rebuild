"""Virtual trader decision schema — JSON contract between virtual agents and orchestrator.

Every virtual trader must return decisions conforming to this schema.
The orchestrator tick loop validates decisions against this schema before
logging them to trading.trades.

Use SchemaValidator.validate() to enforce the contract at runtime.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

log = logging.getLogger("decision_schema")


# ── Enums ────────────────────────────────────────────────────────────────────


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    CLOSE = "close"  # close all positions in this symbol


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"  # good-till-cancelled
    IOC = "ioc"  # immediate-or-cancel
    FOK = "fok"  # fill-or-kill


# ── Core Decision Data Class ─────────────────────────────────────────────────


@dataclass
class VirtualDecision:
    """Single virtual trader decision for one tick.

    This is the canonical decision object. Every trader persona must return
    a list of these (one per symbol they evaluated).

    Fields:
        symbol: Ticker symbol (e.g. "AAPL")
        action: One of buy/sell/hold/close
        quantity: Number of shares (0 for hold)
        limit_price: Optional limit price for limit/stop orders (None = market)
        order_type: MARKET, LIMIT, STOP, or STOP_LIMIT
        time_in_force: DAY, GTC, IOC, or FOK
        conviction: 0.0–1.0 confidence in this decision
        reasoning: Free-text rationale for the decision
        strategy: Which strategy/persona generated this (e.g. "momentum")
        metadata: Optional extra debug info (regime snapshot, indicators, etc.)
        timestamp: When this decision was made (auto-set if None)
    """
    symbol: str
    action: Action
    quantity: int = 0
    limit_price: Optional[float] = None
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY
    conviction: float = 0.0
    reasoning: str = ""
    strategy: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[str] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        self.conviction = max(0.0, min(1.0, self.conviction))
        # Coerce strings to enum values (supports dict-init roundtrips)
        if isinstance(self.action, str):
            self.action = Action(self.action)
        if isinstance(self.order_type, str):
            self.order_type = OrderType(self.order_type)
        if isinstance(self.time_in_force, str):
            self.time_in_force = TimeInForce(self.time_in_force)
        if self.action != Action.HOLD and self.action != Action.CLOSE:
            self.quantity = max(0, int(self.quantity))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "action": self.action.value,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "order_type": self.order_type.value,
            "time_in_force": self.time_in_force.value,
            "conviction": self.conviction,
            "reasoning": self.reasoning,
            "strategy": self.strategy,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }

    @classmethod
    def hold(cls, symbol: str, reasoning: str = "", strategy: str = "") -> "VirtualDecision":
        """Factory: generate a HOLD decision for a symbol."""
        return cls(
            symbol=symbol,
            action=Action.HOLD,
            conviction=0.0,
            reasoning=reasoning or "No actionable signal at this tick.",
            strategy=strategy,
        )


# ── Portfolio Snapshot (what the trader sees) ────────────────────────────────


@dataclass
class PortfolioSnapshot:
    """Current portfolio state fed into the virtual trader's context."""
    cash: float
    total_equity: float
    positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    day_pnl: float = 0.0
    total_pnl: float = 0.0
    drawdown: float = 0.0
    position_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cash": self.cash,
            "total_equity": self.total_equity,
            "positions": self.positions,
            "day_pnl": self.day_pnl,
            "total_pnl": self.total_pnl,
            "drawdown": self.drawdown,
            "position_count": self.position_count,
        }


# ── Tick Context (what the trader receives) ──────────────────────────────────


@dataclass
class TickContext:
    """All market data + portfolio context for one tick decision cycle."""
    tick_id: str
    timestamp: str
    symbols: List[str]
    quotes: Dict[str, Dict[str, Any]]  # per-symbol OHLCV + derived data
    signals: Dict[str, Dict[str, Any]]  # per-symbol momentum/rsi/regime signals
    portfolio: PortfolioSnapshot
    journal: List[str] = field(default_factory=list)  # recent journal entries

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tick_id": self.tick_id,
            "timestamp": self.timestamp,
            "symbols": self.symbols,
            "quotes": self.quotes,
            "signals": self.signals,
            "portfolio": self.portfolio.to_dict(),
            "journal": self.journal[-10:],  # last 10 entries
        }


# ── Validation ────────────────────────────────────────────────────────────────


def validate_decision(raw: Dict[str, Any]) -> List[str]:
    """Validate a raw decision dict against the schema.

    Returns a list of error messages. Empty list = valid.
    """
    errors: List[str] = []

    if "symbol" not in raw:
        errors.append("Missing required field: symbol")
    symbol = raw.get("symbol", "")
    if not isinstance(symbol, str) or not symbol.strip():
        errors.append("symbol must be a non-empty string")

    action = raw.get("action", "").lower()
    valid_actions = {a.value for a in Action}
    if action not in valid_actions:
        errors.append(f"action must be one of {valid_actions}, got {action!r}")

    if action in ("buy", "sell", "close"):
        qty = raw.get("quantity", 0)
        if action in ("buy", "sell") and (not isinstance(qty, (int, float)) or qty < 0):
            errors.append(f"quantity must be a non-negative number for {action}, got {qty!r}")

    conviction = raw.get("conviction", 0.0)
    if not isinstance(conviction, (int, float)) or not (0.0 <= conviction <= 1.0):
        errors.append("conviction must be a float between 0.0 and 1.0")

    limit_price = raw.get("limit_price")
    if limit_price is not None and (not isinstance(limit_price, (int, float)) or limit_price < 0):
        errors.append("limit_price must be a positive number or null")

    order_type = raw.get("order_type", "market").lower()
    valid_types = {t.value for t in OrderType}
    if order_type not in valid_types:
        errors.append(f"order_type must be one of {valid_types}, got {order_type!r}")

    tif = raw.get("time_in_force", "day").lower()
    valid_tifs = {t.value for t in TimeInForce}
    if tif not in valid_tifs:
        errors.append(f"time_in_force must be one of {valid_tifs}, got {tif!r}")

    if "reasoning" in raw and not isinstance(raw["reasoning"], str):
        errors.append("reasoning must be a string")

    return errors


def validate_decision_list(decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Validate a list of decisions and return only valid ones with errors stripped.

    Logs warnings for each invalid decision.
    """
    validated: List[Dict[str, Any]] = []
    for i, d in enumerate(decisions):
        errors = validate_decision(d)
        if errors:
            log.warning("Decision #%d (%s) failed validation: %s",
                        i, d.get("symbol", "?"), "; ".join(errors))
            # Still include with a meta error flag so orchestrator can decide
            d["_validation_errors"] = errors
        validated.append(d)
    return validated


def decision_to_trade_row(decision: VirtualDecision, trader_name: str) -> Dict[str, Any]:
    """Convert a VirtualDecision to a row for trading.trades insertion.

    The orchestrator uses this to log each decision to the database.
    """
    return {
        "trader_id": trader_name,
        "ticker": decision.symbol,
        "action": decision.action.value,
        "shares": decision.quantity,
        "limit_price": decision.limit_price,
        "conviction": decision.conviction,
        "reasoning": decision.reasoning,
        "strategy": decision.strategy,
        "decision_time": decision.timestamp,
        "trade_source": "virtual",
    }


# ── JSON Schema (for LLM system prompts) ──────────────────────────────────────


DECISION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["buy", "sell", "hold", "close"],
                    "description": "Trading action: buy, sell, hold, or close (liquidate position)"
                },
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol (e.g. AAPL, NVDA)"
                },
                "quantity": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Number of shares. 0 for hold/close."
                },
                "limit_price": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Limit price for limit/stop orders. Omit for market orders."
                },
                "order_type": {
                    "type": "string",
                    "enum": ["market", "limit", "stop", "stop_limit"],
                    "description": "Order type. Use market for fast execution, limit for precision."
                },
                "time_in_force": {
                    "type": "string",
                    "enum": ["day", "gtc", "ioc", "fok"],
                    "description": "Time in force: day = expires at market close, gtc = good-till-cancelled"
                },
                "conviction": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Confidence in this decision (0.0 = uncertain, 1.0 = certain)"
                },
                "reasoning": {
                    "type": "string",
                    "maxLength": 500,
                    "description": "Brief rationale for the decision"
                }
            },
            "required": ["action", "symbol", "conviction", "reasoning"],
            "additionalProperties": False
        }
    },
    "required": ["decision"],
    "additionalProperties": False
}


DECISION_JSON_EXAMPLE = json.dumps({
    "decision": {
        "action": "buy",
        "symbol": "AAPL",
        "quantity": 100,
        "limit_price": 185.50,
        "order_type": "limit",
        "time_in_force": "day",
        "conviction": 0.78,
        "reasoning": "AAPL broke above 50-day MA on above-average volume with RSI at 58. "
                     "Momentum signal strong (0.65). Bullish regime confirmed. "
                     "Entering 100 shares at limit $185.50."
    }
}, indent=2)


# ── Aggregate Decision (per-tick batch) ──────────────────────────────────────


@dataclass
class TickDecisions:
    """All decisions made by one virtual trader for one tick across all symbols."""
    trader_name: str
    trader_persona: str
    tick_id: str
    decisions: List[VirtualDecision]
    portfolio_snapshot: PortfolioSnapshot
    generated_at: str = ""

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trader_name": self.trader_name,
            "trader_persona": self.trader_persona,
            "tick_id": self.tick_id,
            "decisions": [d.to_dict() for d in self.decisions],
            "portfolio": self.portfolio_snapshot.to_dict(),
            "generated_at": self.generated_at,
        }
