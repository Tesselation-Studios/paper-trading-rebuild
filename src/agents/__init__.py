"""Virtual trader agent stubs — multiple AI traders competing in the paper trading simulation.

This package defines:
- Decision schema (JSON contract between agents and orchestrator)
- Base VirtualTrader class with common lifecycle methods
- 3 persona implementations:
  - MomentumTrader — rides trends
  - ValueTrader — contrarian/value opportunities
  - MeanReversionScalper — captures small oscillations
- Persona registry for dynamic instantiation
- OpenClaw agent config files in configs/
- Prompt templates in prompts/
"""

from src.agents.decision_schema import (
    Action,
    OrderType,
    TimeInForce,
    TickContext,
    TickDecisions,
    VirtualDecision,
    PortfolioSnapshot,
    validate_decision,
    validate_decision_list,
    decision_to_trade_row,
    DECISION_JSON_SCHEMA,
    DECISION_JSON_EXAMPLE,
)
from src.agents.base import (
    VirtualTrader,
    TraderPortfolio,
    OpenPosition,
    DataBusClient,
)
from src.agents.registry import (
    list_personas,
    get_persona,
    create_trader,
    load_all_persona_configs,
    persona_base_trader,
)
from src.agents.momentum import MomentumTrader
from src.agents.value import ValueTrader
from src.agents.mean_reversion import MeanReversionScalper

__all__ = [
    # Decision schema
    "Action",
    "OrderType",
    "TimeInForce",
    "TickContext",
    "TickDecisions",
    "VirtualDecision",
    "PortfolioSnapshot",
    "validate_decision",
    "validate_decision_list",
    "decision_to_trade_row",
    "DECISION_JSON_SCHEMA",
    "DECISION_JSON_EXAMPLE",
    # Base class
    "VirtualTrader",
    "TraderPortfolio",
    "OpenPosition",
    "DataBusClient",
    # Registry
    "list_personas",
    "get_persona",
    "create_trader",
    "load_all_persona_configs",
    "persona_base_trader",
    # Personas
    "MomentumTrader",
    "ValueTrader",
    "MeanReversionScalper",
]
