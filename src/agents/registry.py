"""Virtual trader persona registry — maps persona IDs to their implementations.

Usage:
    from src.agents.registry import get_persona, list_personas, create_trader

    trader = create_trader("momentum", "my-momentum-trader", cash=10000.0)
    decisions = trader.run_tick(tick_context)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from src.agents.base import VirtualTrader
from src.agents.momentum import MomentumTrader
from src.agents.value import ValueTrader
from src.agents.mean_reversion import MeanReversionScalper

# ── Registry ──────────────────────────────────────────────────────────────────

_PERSONA_REGISTRY: Dict[str, Dict[str, Any]] = {
    "momentum": {
        "class": MomentumTrader,
        "name": "Momentum Trader",
        "description": "Rides trends with aggressive momentum capture",
        "base_trader": "kairos",
        "default_config": {
            "momentum_threshold_buy": 0.4,
            "momentum_threshold_sell": -0.3,
            "rsi_overbought": 80,
            "rsi_oversold": 30,
            "max_positions": 5,
            "max_portfolio_pct": 0.20,
            "min_conviction": 0.4,
        },
        "prompt_template": "identity-momentum.md",
    },
    "value_contrarian": {
        "class": ValueTrader,
        "name": "Value/Contrarian Trader",
        "description": "Buys oversold value opportunities, sells overbought",
        "base_trader": "aldridge",
        "default_config": {
            "max_positions": 7,
            "min_positions": 3,
            "max_portfolio_pct": 0.15,
            "target_position_pct": 0.12,
            "min_conviction": 0.5,
            "rsi_oversold": 35,
            "rsi_overbought": 65,
        },
        "prompt_template": "identity-value.md",
    },
    "mean_reversion_scalper": {
        "class": MeanReversionScalper,
        "name": "Mean-Reversion Scalper",
        "description": "Captures small bounces with tight stops and quick exits",
        "base_trader": None,  # new persona, not mapped to live trader
        "default_config": {
            "max_positions": 3,
            "max_portfolio_pct": 0.03,
            "min_conviction": 0.4,
            "rsi_oversold": 35,
            "rsi_overbought": 65,
            "target_pct": 0.015,
            "stop_loss_pct": 0.015,
            "max_holding_ticks": 3,
            "consecutive_loss_limit": 3,
            "cool_down_ticks": 10,
        },
        "prompt_template": "identity-scalper.md",
    },
}

# Alias short IDs
_PERSONA_ALIASES: Dict[str, str] = {
    "momentum": "momentum",
    "value": "value_contrarian",
    "value_contrarian": "value_contrarian",
    "contrarian": "value_contrarian",
    "scalper": "mean_reversion_scalper",
    "mean_reversion": "mean_reversion_scalper",
    "mean_reversion_scalper": "mean_reversion_scalper",
}


# ── Public API ────────────────────────────────────────────────────────────────


def list_personas() -> List[Dict[str, Any]]:
    """List all registered trader personas."""
    return [
        {
            "id": pid,
            "name": info["name"],
            "description": info["description"],
            "base_trader": info.get("base_trader"),
        }
        for pid, info in _PERSONA_REGISTRY.items()
    ]


def get_persona(persona_id: str) -> Optional[Dict[str, Any]]:
    """Get persona info by ID. Supports aliases like 'value' -> 'value_contrarian'."""
    resolved = _PERSONA_ALIASES.get(persona_id.lower(), persona_id.lower())
    info = _PERSONA_REGISTRY.get(resolved)
    if info is None:
        return None
    return {
        "id": resolved,
        "name": info["name"],
        "description": info["description"],
        "base_trader": info.get("base_trader"),
        "default_config": info.get("default_config", {}),
        "prompt_template": info.get("prompt_template", ""),
        "class": info["class"],
    }


def create_trader(
    persona_id: str,
    trader_name: str,
    starting_cash: float = 10_000.0,
    data_bus_url: str = "http://192.168.1.25:5000",
    config: Optional[Dict[str, Any]] = None,
) -> VirtualTrader:
    """Create a virtual trader instance by persona ID.

    Args:
        persona_id: One of 'momentum', 'value_contrarian', 'mean_reversion_scalper'
        trader_name: Unique name for this trader instance (e.g. 'momentum-001')
        starting_cash: Initial portfolio cash
        data_bus_url: URL for the data bus
        config: Optional config overrides for this specific instance

    Returns:
        A VirtualTrader subclass instance.

    Raises:
        ValueError: If persona_id is not registered.
    """
    info = get_persona(persona_id)
    if info is None:
        available = list(_PERSONA_REGISTRY.keys())
        raise ValueError(
            f"Unknown persona: {persona_id!r}. Available: {available}"
        )

    cls: Type[VirtualTrader] = info["class"]
    merged_config = {**info.get("default_config", {}), **(config or {})}

    trader = cls(
        trader_name=trader_name,
        starting_cash=starting_cash,
        data_bus_url=data_bus_url,
        config=merged_config,
    )
    return trader


def load_all_persona_configs() -> Dict[str, Dict[str, Any]]:
    """Load all persona default configs merged with any overrides.

    Returns a dict of {persona_id: {class, name, config, ...}} suitable
    for the virtual runner to iterate over.
    """
    result = {}
    for pid, info in _PERSONA_REGISTRY.items():
        result[pid] = {
            "id": pid,
            "name": info["name"],
            "base_trader": info.get("base_trader"),
            "default_config": info.get("default_config", {}).copy(),
            "prompt_template": info.get("prompt_template", ""),
        }
    return result


def persona_base_trader(persona_id: str) -> Optional[str]:
    """Get the base trader associated with a persona."""
    info = get_persona(persona_id)
    return info.get("base_trader") if info else None