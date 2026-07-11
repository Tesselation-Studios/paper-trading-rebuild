#!/usr/bin/env python3
"""Create a virtual trader agent directory with config files.

Usage:
    python3 scripts/create_virtual_trader.py --name test-vt-1 --base aldridge
    python3 scripts/create_virtual_trader.py --name test-vt-2 --base kairos --register
    python3 scripts/create_virtual_trader.py --name test-vt-3 --base stonks --status active

Creates:
    ~/.openclaw/agents/virtual-{name}/
        openclaw.json    — agentId: "virtual-{name}", skills mirroring base trader
        AGENTS.md        — operating manual noting virtual variant
        HEARTBEAT.md     — heartbeat config
        TOOLS.md         — tool access profile

Optional --register flag also registers the virtual trader in the DB.
"""

import argparse
import json
import os
import sys
import subprocess
from pathlib import Path

# ── Base trader skill mappings ────────────────────────────────────────────────

BASE_TRADER_SKILLS = {
    "aldridge": [
        "stock-analysis",
        "sell-the-news",
        "skill-aldridge-strategy",
        "skill-alpaca-aldridge",
        "skill-trade-execution",
        "self-improvement",
    ],
    "kairos": [
        "stock-analysis",
        "sell-the-news",
        "skill-kairos-strategy",
        "skill-alpaca-kairos",
        "skill-trade-execution",
        "self-improvement",
    ],
    "stonks": [
        "stock-analysis",
        "sell-the-news",
        "skill-stonks-strategy",
        "skill-alpaca-stonks",
        "skill-trade-execution",
        "self-improvement",
    ],
}

BASE_TRADER_MODELS = {
    "aldridge": "openrouter/deepseek/deepseek-v4-pro",
    "kairos": "openrouter/deepseek/deepseek-v4-flash",
    "stonks": "openrouter/deepseek/deepseek-v4-flash",
}

BASE_TRADER_THINKING = {
    "aldridge": "medium",
    "kairos": "low",
    "stonks": "low",
}

AGENTS_MD_TEMPLATE = """# Virtual Trader: {name}

**Type:** Virtual variant (lightweight agent)
**Base Trader:** {base_trader}
**Created:** {created_at}

This is a virtual trading agent — a lightweight variant of the live {base_trader}
trader. It does NOT run as a live OpenClaw agent. Instead, it is managed by the
virtual runner (`src/virtual_runner.py`) which orchestrates all virtual traders
during market hours.

## Strategy
Inherits {base_trader}'s strategy with the following modifications/overrides:

{variant_description}

## Operational Model
- Triggered by MARKET TICK events from the data bus
- Runs as a Python subprocess within virtual_runner.py
- Logs decisions to trading.virtual_traders_log and trading.trades
- Portfolio is in-memory (VirtualPortfolio) — no Alpaca account
- Starting equity mirrors {base_trader}'s current Alpaca account equity

## Rules
- BUY/SELL/HOLD decisions parsed from LLM output
- Position sizing, stop-loss, take-profit follow {base_trader} defaults unless overridden
- No journal writing to filesystem — decisions logged to DB only
- No Alpaca trade execution — decisions are paper only
"""

HEARTBEAT_TEMPLATE = """# Heartbeat Config — {name} (virtual)

disabled: true
interval: 300
max_cycles: 0
"""

TOOLS_TEMPLATE = """# Virtual Trader Tools — {name}

This virtual trader inherits the same tool access as the base {base_trader} trader.
Tool calls are proxied through the virtual_runner module.

## Capabilities
- Stock analysis (RSI, MACD, momentum indicators)
- Trade execution logging (to trading.virtual_traders_log)
- LLM inference via OpenRouter
- No Alpaca API access (all trades are paper/simulated)

## Runtime
- Managed by virtual_runner.py
- No SSH, no filesystem persistence beyond DB
- Graceful error handling — one virtual failing won't affect others
"""


def build_openclaw_config(name: str, base_trader: str) -> dict:
    """Build the openclaw.json config for a virtual trader."""
    skills = BASE_TRADER_SKILLS.get(base_trader, BASE_TRADER_SKILLS["kairos"])
    model = BASE_TRADER_MODELS.get(base_trader, BASE_TRADER_MODELS["kairos"])
    thinking = BASE_TRADER_THINKING.get(base_trader, BASE_TRADER_THINKING["kairos"])

    return {
        "agentId": f"virtual-{name}",
        "model": model,
        "skills": skills,
        "tools": {
            "profile": "minimal",
            "alsoAllow": [
                "group:runtime",
                "group:web",
                "group:memory",
                "group:fs"
            ]
        },
        "thinking": thinking,
    }


def create_agent_config(name: str, base_trader: str, variant_desc: str = "") -> Path:
    """Create the virtual trader agent directory with all config files.

    Args:
        name: Virtual trader name (e.g., 'test-aldridge-1')
        base_trader: Base trader to inherit from (aldridge/kairos/stonks)
        variant_desc: Description of this variant's modifications

    Returns:
        Path to the created agent directory.
    """
    from datetime import datetime

    agents_dir = Path.home() / ".openclaw" / "agents"
    agent_dir = agents_dir / f"virtual-{name}"
    agent_dir.mkdir(parents=True, exist_ok=True)

    # 1. openclaw.json
    config = build_openclaw_config(name, base_trader)
    with open(agent_dir / "openclaw.json", "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    # 2. AGENTS.md
    agents_md = AGENTS_MD_TEMPLATE.format(
        name=name,
        base_trader=base_trader,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        variant_description=variant_desc or "Standard virtual variant with default parameters.",
    )
    with open(agent_dir / "AGENTS.md", "w") as f:
        f.write(agents_md)

    # 3. HEARTBEAT.md
    heartbeat = HEARTBEAT_TEMPLATE.format(name=name)
    with open(agent_dir / "HEARTBEAT.md", "w") as f:
        f.write(heartbeat)

    # 4. TOOLS.md
    tools = TOOLS_TEMPLATE.format(name=name, base_trader=base_trader)
    with open(agent_dir / "TOOLS.md", "w") as f:
        f.write(tools)

    print(f"✅ Created virtual trader agent directory: {agent_dir}")
    print(f"   - openclaw.json (agentId: virtual-{name})")
    print(f"   - AGENTS.md")
    print(f"   - HEARTBEAT.md")
    print(f"   - TOOLS.md")

    return agent_dir


def register_in_db(name: str, base_trader: str, variant_type: str,
                   config_json: str, status: str) -> bool:
    """Call the register_virtual.py script to register in the DB."""
    try:
        script = Path(__file__).resolve().parent / "register_virtual.py"
        if not script.exists():
            print(f"⚠️  register_virtual.py not found at {script} — skipping DB registration")
            return False

        cmd = [
            sys.executable, str(script),
            "--name", name,
            "--base", base_trader,
            "--variant-type", variant_type,
            "--config", config_json,
            "--status", status,
        ]
        print(f"   Registering in DB: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=script.parent.parent)
        if result.returncode == 0:
            print(f"   {result.stdout.strip()}")
            return True
        else:
            print(f"   ⚠️  DB registration failed: {result.stderr.strip()}")
            return False
    except Exception as e:
        print(f"   ⚠️  DB registration error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Create virtual trader agent config and optionally register in DB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--name", required=True,
        help="Virtual trader name (e.g., test-aldridge-1)"
    )
    parser.add_argument(
        "--base", required=True, choices=["aldridge", "kairos", "stonks"],
        help="Base trader to inherit from"
    )
    parser.add_argument(
        "--variant-desc", default="",
        help="Description of this variant's modifications (appears in AGENTS.md)"
    )
    parser.add_argument(
        "--register", action="store_true",
        help="Also register the virtual trader in the database"
    )
    parser.add_argument(
        "--variant-type", default="params",
        choices=["params", "prompt", "model", "risk", "manual"],
        help="Variant type (default: params)"
    )
    parser.add_argument(
        "--config", default="{}",
        help="JSON config overrides (default: {})"
    )
    parser.add_argument(
        "--status", default="probation",
        choices=["probation", "active", "disabled"],
        help="Initial status in DB (default: probation)"
    )

    args = parser.parse_args()

    # Validate name
    if not args.name.replace("-", "").replace("_", "").isalnum():
        parser.error("Name must be alphanumeric with hyphens/underscores only")

    # Validate config JSON
    try:
        config_obj = json.loads(args.config)
    except json.JSONDecodeError as e:
        parser.error(f"Invalid --config JSON: {e}")

    # Create agent directory
    agent_dir = create_agent_config(args.name, args.base, args.variant_desc)

    # Optionally register in DB
    if args.register:
        config_json_str = json.dumps(config_obj)
        register_in_db(args.name, args.base, args.variant_type, config_json_str, args.status)
    else:
        print(f"ℹ️  Skipping DB registration (use --register to register)")
        print(f"   Or run: python3 scripts/register_virtual.py --name {args.name} --base {args.base} --status {args.status}")


if __name__ == "__main__":
    main()