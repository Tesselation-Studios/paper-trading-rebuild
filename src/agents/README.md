# Virtual Trader Agent System

Multiple AI virtual traders competing in the paper trading simulation.
Each virtual trader runs as an independent agent with its own strategy/personality.

## Architecture

```
src/agents/
├── __init__.py           # Package exports
├── base.py               # VirtualTrader base class
├── momentum.py           # Momentum trader persona
├── value.py              # Value/contrarian trader persona
├── mean_reversion.py     # Mean-reversion scalper persona
├── decision_schema.py    # JSON decision schema + validation
├── registry.py           # Persona registry (factory)
├── configs/              # OpenClaw agent config JSON files
│   ├── virtual-trader-momentum.json
│   ├── virtual-trader-value.json
│   └── virtual-trader-scalper.json
└── prompts/              # System prompt / identity templates
    ├── identity-momentum.md
    ├── identity-value.md
    └── identity-scalper.md
```

## Personas

| Persona ID              | Name                   | Strategy                          | Base Trader | LLM-Dependent |
|-------------------------|------------------------|-----------------------------------|-------------|---------------|
| `momentum`              | Pulse Capital          | Ride trends, momentum > 0.4       | kairos      | No (rule-based) |
| `value_contrarian`      | Sterling Capital       | Buy oversold, sell overbought     | aldridge    | No (rule-based) |
| `mean_reversion_scalper`| Volta Trading          | Capture bounces, tight stops      | (new)       | No (rule-based) |

## Usage

```python
from src.agents.registry import create_trader, list_personas

# List available personas
print(list_personas())

# Create a trader instance
trader = create_trader("momentum", "pulse-001", starting_cash=10000.0)

# Connect (initialize portfolio)
trader.connect()

# Run a tick
from src.agents.decision_schema import TickContext, PortfolioSnapshot
decisions = trader.run_tick(tick_context)

# Disconnect
trader.disconnect()
```

## Decision Schema

Every virtual trader returns decisions conforming to `decision_schema.VirtualDecision`:

```json
{
  "symbol": "AAPL",
  "action": "buy",
  "quantity": 100,
  "limit_price": 185.50,
  "order_type": "limit",
  "time_in_force": "day",
  "conviction": 0.78,
  "reasoning": "Momentum at 0.65, RSI at 58. Buying 100 shares.",
  "strategy": "momentum",
  "metadata": {}
}
```

## OpenClaw Agent Configs

Config files in `configs/` can be registered in `openclaw.json` under `agents[]`.
Each config includes:
- Agent ID and display name
- Model assignment (flash with fallbacks)
- Workspace directory
- System prompt reference
- Tool allowlist (restricted to fetch/exec)
- Skills: trade-execution, market-data, strategy-specific
- Heartbeat: every 5 min during market hours

## Integration with virtual_runner.py

Virtual trader personas are automatically loaded in mock mode:
```bash
VT_MOCK=1 python3 src/virtual_runner.py --once
```

This will create one instance of each persona alongside legacy mock traders.

## Next Steps (Phase 2)

1. Add DB persistence for persona-based virtual traders in `trading.virtual_traders`
2. Wire persona instances into the orchestrator dispatch loop
3. Add LLM fallback for each persona (use LLM when rule engine is uncertain)
4. Add performance tracking per persona
5. Run championship belt rotation across personas