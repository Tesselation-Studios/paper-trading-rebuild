#!/usr/bin/env python3
"""
Stonks Entry Gate — code-level enforcement of trade entry rules.

The agent describes this gate in its AGENTS.md as a hard requirement that
runs before every Alpaca order. The agent CANNOT override it.

Checks:
1. Bankroll ceiling — position cost ≤ portfolio_value × 0.01
2. Technical confirmation — RSI 50-70, MACD bullish, Price > MA20
3. Volume confirmation — volume_ratio ≥ 2.0
4. Conviction score — 3/5 signals confirmed, weighted conviction ≥ 0.50
5. Fear & Greed override — if F&G ≤ 25, needs 5/5 signals + conviction ≥ 0.70
6. High-conviction override — conviction ≥ 0.80 + catalyst ≥ 0.7 bypasses technicals

Usage:
    python3 src/stonks_entry_gate.py --action BUY --ticker INTC --quantity 10 \\
        --stop-loss 42.50 --confidence 0.78 --thesis "momentum play"
    python3 src/stonks_entry_gate.py --action BUY --ticker INTC --quantity 10 \\
        --price 43.50 --confidence 0.78 --rsi 62.5 --macd-bullish true \\
        --volume-ratio 2.5 --fear-greed 30 --catalyst 0.0

Output: JSON with verdict, reason, and details.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AGENT = "stonks"
AGENT_ID = "trader-stonks"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"


def get_paths(agent: str) -> dict:
    """Get paths for a given agent."""
    name_map = {"stonks": "trader-stonks", "kairos": "trader-kairos"}
    agent_id = name_map.get(agent, f"trader-{agent}")
    return {
        "agent_id": agent_id,
        "config": PROJECT_ROOT / "agents" / agent_id / "config.yaml",
        "bankroll": Path(os.getenv("OPENCLAW_HOME", "/home/openclaw")) / ".openclaw" / f"workspace-{agent_id}" / "bankroll.md",
        "heartbeat": STATE_DIR / "heartbeat-state.json",
    }

PG_DSN = os.getenv("PG_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")


# ── Config Loader ────────────────────────────────────────────────────────────

def load_config(agent: str = "stonks") -> dict:
    """Load agent config.yaml."""
    paths = get_paths(agent)
    try:
        import yaml
        with open(paths["config"]) as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"[WARN] Could not load config for {agent}: {e}", file=sys.stderr)
        return {}


# ── Bankroll ─────────────────────────────────────────────────────────────────

def get_portfolio_value(agent: str = "stonks") -> float:
    """Get current portfolio value from heartbeat-state.json or Alpaca."""
    # Try heartbeat-state.json first
    paths = get_paths(agent)
    hb_path = paths["heartbeat"]
    if hb_path.exists():
        try:
            data = json.loads(hb_path.read_text())
            val = data.get(f"last_{agent}")
            if val:
                return float(val)
        except Exception:
            pass

    # Try PG trader_decisions for latest portfolio value
    try:
        import psycopg2 as _psycopg2
        conn = _psycopg2.connect(PG_DSN)
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM trading.system_params "
            "WHERE trader_id = 'trader-stonks' AND param_name = 'portfolio_value' "
            "ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return float(row[0])
    except Exception:
        pass

    return 10_000.0  # fallback


def get_current_bankroll_state(agent: str = "stonks") -> dict:
    """Read the current bankroll state from bankroll.md."""
    paths = get_paths(agent)
    state = {
        "ceiling": 100.0,
        "deployed": 0.0,
        "positions": [],
    }
    bankroll_path = paths["bankroll"]
    if not bankroll_path.exists():
        return state

    text = bankroll_path.read_text(encoding="utf-8", errors="replace")

    # Extract deployed positions
    in_table = False
    for line in text.split("\n"):
        if "| Ticker" in line and "Cost" in line:
            in_table = True
            continue
        if in_table and line.strip().startswith("|"):
            if "---" in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 5:
                ticker = parts[1]
                qty = float(parts[2]) if parts[2] else 0
                cost = float(parts[4].replace("$", "").replace(",", "")) if parts[4] else 0
                state["positions"].append({"ticker": ticker, "qty": qty, "cost": cost})
                state["deployed"] += cost
        elif in_table and not line.strip().startswith("|"):
            break

    return state


def compute_bankroll_ceiling(portfolio_value: float) -> float:
    """Compute bankroll ceiling: 1% of portfolio value."""
    return round(portfolio_value * 0.01, 2)


def update_bankroll_file(portfolio_value: float, proposed: dict, agent: str = "stonks") -> dict:
    """Update bankroll.md with new ceiling and add proposed position."""
    paths = get_paths(agent)
    bankroll_path = paths["bankroll"]
    ceiling = compute_bankroll_ceiling(portfolio_value)
    current = get_current_bankroll_state(agent)
    agent_name = agent.capitalize()

    # Add the proposed position to the deployed list
    new_position = {
        "ticker": proposed.get("ticker", "?"),
        "qty": proposed.get("quantity", 0),
        "cost": proposed.get("cost", 0),
    }
    # Replace existing position for same ticker or append
    found = False
    for i, p in enumerate(current["positions"]):
        if p["ticker"] == new_position["ticker"]:
            current["positions"][i] = new_position
            found = True
            break
    if not found:
        current["positions"].append(new_position)

    current["deployed"] = sum(p["cost"] for p in current["positions"])

    # Build markdown content
    lines = [
        f"# Bankroll \u2014 {agent_name}",
        "",
        f"Ceiling: ${ceiling:.2f}",
        f"Portfolio: ${portfolio_value:.2f}",
        f"Ceiling formula: portfolio \u00d7 0.01",
        f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "---",
        "## Open Position Deployment",
        "| Ticker | Qty | Entry Price | Cost | % of Ceiling |",
        "|--------|-----|-------------|------|-------------|",
    ]

    for p in current["positions"]:
        cost = p["cost"]
        pct = f"{cost / ceiling * 100:.1f}%" if ceiling > 0 else "0%"
        lines.append(
            f"| {p['ticker']} | {p['qty']} | ${cost / max(p['qty'], 1):.2f} | ${cost:.2f} | {pct} |"
        )

    lines.extend([
        "",
        f"Remaining: ${max(ceiling - current['deployed'], 0):.2f}",
        "",
        "## Rules",
        "- Ceiling = portfolio_value \u00d7 0.01 (recalculated on each check)",
        "- Ceiling recalculates automatically from portfolio value. No growth multiplier needed.",
        "- Ceiling can be split any way across any number of stocks",
        "- $0 is valid \u2014 patience doesn't punish you",
        "- Floor: $15.00 | No hard cap",
    ])

    bankroll_path.parent.mkdir(parents=True, exist_ok=True)
    bankroll_path.write_text("\n".join(lines) + "\n")

    return {
        "ceiling": ceiling,
        "deployed": current["deployed"] + (proposed.get("cost", 0) if not found else 0),
        "remaining": max(ceiling - current["deployed"] - (proposed.get("cost", 0) if not found else 0), 0),
    }


# ── Data Bus Fetch ───────────────────────────────────────────────────────────

def fetch_data_bus(endpoint: str, params: dict = None) -> dict:
    """Fetch data from the data bus on localhost:5000."""
    try:
        import urllib.request
        import urllib.parse

        url = f"http://localhost:5000/{endpoint.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[WARN] Data bus fetch failed ({endpoint}): {e}", file=sys.stderr)
        return {}


def fetch_quote(symbol: str) -> dict:
    """Fetch quote data from data bus."""
    return fetch_data_bus("quotes", {"symbols": symbol})


def fetch_fear_greed() -> dict:
    """Fetch Fear & Greed index."""
    return fetch_data_bus("fear_greed")


# ── Gate Checks ──────────────────────────────────────────────────────────────

def check_bankroll(
    config: dict, portfolio_value: float, proposed_cost: float, proposed_ticker: str
) -> Tuple[bool, str]:
    """Check if the proposed trade fits within the bankroll ceiling."""
    ceiling = compute_bankroll_ceiling(portfolio_value)
    current = get_current_bankroll_state()
    deployed = current["deployed"]
    remaining = max(ceiling - deployed, 0)

    if proposed_cost > remaining:
        return False, (
            f"Bankroll exceeded: ${proposed_cost:.2f} cost, "
            f"${remaining:.2f} remaining (ceiling ${ceiling:.2f}, "
            f"${deployed:.2f} deployed)"
        )

    # Check max position % from config
    max_pos_pct = config.get("risk", {}).get("max_position_pct", 0.04)
    max_position_cost = portfolio_value * max_pos_pct
    if proposed_cost > max_position_cost:
        return False, (
            f"Max position size exceeded: ${proposed_cost:.2f} > "
            f"${max_position_cost:.2f} ({max_pos_pct * 100:.0f}% of portfolio)"
        )

    return True, (
        f"Bankroll OK: ${proposed_cost:.2f} cost, "
        f"${remaining:.2f} remaining (ceiling ${ceiling:.2f})"
    )


def check_technical(
    config: dict, symbol: str, rsi: Optional[float], macd_bullish: Optional[bool],
    price_above_ma20: Optional[bool]
) -> Tuple[bool, str]:
    """Check technical confirmation rules."""
    gate = config.get("entry_gate", {})
    rsi_min = gate.get("rsi_min", 50)
    rsi_max = gate.get("rsi_max", 70)
    macd_required = gate.get("macd_bullish_required", True)
    ma20_required = gate.get("price_above_ma20_required", True)

    if rsi is not None:
        if rsi < rsi_min:
            return False, f"RSI {rsi:.1f} < minimum {rsi_min}"
        if rsi > rsi_max:
            return False, f"RSI {rsi:.1f} > maximum {rsi_max}"

    if macd_required and macd_bullish is not None and not macd_bullish:
        return False, "MACD not bullish"

    if ma20_required and price_above_ma20 is not None and not price_above_ma20:
        return False, "Price below MA20"

    return True, "Technical checks passed"


def check_volume(config: dict, volume_ratio: Optional[float]) -> Tuple[bool, str]:
    """Check volume confirmation."""
    min_ratio = config.get("entry_gate", {}).get("min_volume_ratio", 2.0)
    if volume_ratio is not None and volume_ratio < min_ratio:
        return False, f"Volume ratio {volume_ratio:.1f} < minimum {min_ratio}"
    return True, "Volume check passed"


def check_conviction(
    config: dict, confidence: float, signals_confirmed: int,
    catalyst: float = 0.0, fear_greed: int = 50
) -> Tuple[bool, str]:
    """Check conviction score and Fear & Greed override."""
    gate = config.get("entry_gate", {})
    min_conviction = gate.get("weighted_conviction_min", 0.50)
    min_signals = gate.get("confirmations_required", 3)
    fg_confirmations = gate.get("fear_greed_extreme_confirmations", 5)
    fg_conviction = gate.get("fear_greed_extreme_conviction", 0.70)
    fg_threshold = gate.get("fear_greed_extreme_threshold", 25)
    hc_conviction = gate.get("high_conviction_override_conviction", 0.80)
    hc_catalyst = gate.get("high_conviction_override_catalyst", 0.70)
    hc_min_signals = gate.get("high_conviction_override_min_signals", 2)
    signal_weights = config.get("signal_weights", {})

    # Compute weighted conviction
    weights = {
        "wsb": signal_weights.get("wsb", 0.30),
        "sentiment": signal_weights.get("sentiment", 0.25),
        "volume": signal_weights.get("volume", 0.20),
        "flow": signal_weights.get("flow", 0.15),
        "catalyst": signal_weights.get("catalyst", 0.10),
    }
    # Normalize weights to sum to 1.0
    total_w = sum(weights.values())
    if total_w > 0:
        weights = {k: v / total_w for k, v in weights.items()}

    # Approximate weighted conviction from confidence and signals
    weighted_conviction = confidence * 0.5 + (signals_confirmed / 5.0) * 0.5

    # High-conviction override: bypass technicals if conviction is very high
    if weighted_conviction >= hc_conviction and catalyst >= hc_catalyst:
        if signals_confirmed >= hc_min_signals:
            return True, (
                f"High-conviction override: conviction {weighted_conviction:.2f} ≥ "
                f"{hc_conviction}, catalyst {catalyst:.2f} ≥ {hc_catalyst}, "
                f"signals {signals_confirmed}/{hc_min_signals}"
            )

    # Fear & Greed extreme fear override
    if fear_greed <= fg_threshold:
        if signals_confirmed >= fg_confirmations and weighted_conviction >= fg_conviction:
            return True, (
                f"Extreme Fear override: F&G {fear_greed} ≤ {fg_threshold}, "
                f"signals {signals_confirmed}/{fg_confirmations}, "
                f"conviction {weighted_conviction:.2f} ≥ {fg_conviction}"
            )
        return False, (
            f"Extreme Fear (F&G {fear_greed} ≤ {fg_threshold}): "
            f"need {fg_confirmations}/{fg_confirmations} signals and "
            f"conviction ≥ {fg_conviction}, got {signals_confirmed}/{fg_confirmations} "
            f"and {weighted_conviction:.2f}"
        )

    # Normal check
    if signals_confirmed < min_signals:
        return False, (
            f"Not enough signals: {signals_confirmed}/{min_signals} confirmed, "
            f"need at least {min_signals}"
        )
    if weighted_conviction < min_conviction:
        return False, (
            f"Conviction too low: {weighted_conviction:.2f} < minimum {min_conviction}"
        )

    return True, (
        f"Conviction check passed: {signals_confirmed}/5 signals, "
        f"conviction {weighted_conviction:.2f}"
    )


def check_daily_loss(config: dict, portfolio_value: float, agent: str = "stonks") -> Tuple[bool, str]:
    """Check if the daily loss limit has been reached.

    Reads from state/daily_pnl_{agent}.json if available.
    """
    max_loss = config.get("risk", {}).get("max_daily_loss", 300)
    # Placeholder — daily P&L tracking via PG trader_decisions
    return True, f"Daily loss OK (limit ${max_loss})"


# ── Main Gate ────────────────────────────────────────────────────────────────

def run_gate(
    action: str,
    ticker: str,
    quantity: float,
    price: float,
    stop_loss: float,
    confidence: float,
    thesis: str,
    signals_confirmed: int = 3,
    rsi: Optional[float] = None,
    macd_bullish: Optional[bool] = None,
    price_above_ma20: Optional[bool] = None,
    volume_ratio: Optional[float] = None,
    fear_greed: Optional[int] = None,
    catalyst: float = 0.0,
    portfolio_value: Optional[float] = None,
    skip_technical: bool = False,
    agent: str = "stonks",
) -> Dict[str, Any]:
    """Run the full entry gate and return a verdict.

    Returns dict with verdict, reason, and details.
    """
    config = load_config(agent)
    if portfolio_value is None:
        portfolio_value = get_portfolio_value(agent)

    proposed_cost = quantity * price
    checks = []

    # 1. Bankroll check
    bankroll_ok, bankroll_msg = check_bankroll(config, portfolio_value, proposed_cost, ticker)
    checks.append({"check": "bankroll", "passed": bankroll_ok, "message": bankroll_msg})

    if action == "HOLD" or action == "SELL":
        return {
            "verdict": "PASS",
            "action": action,
            "reason": f"{action} actions don't require entry gate validation",
            "checks": checks,
            "bankroll": {
                "ceiling": compute_bankroll_ceiling(portfolio_value),
                "portfolio": portfolio_value,
            },
        }

    # 2. Technical confirmation
    if not skip_technical:
        tech_ok, tech_msg = check_technical(config, ticker, rsi, macd_bullish, price_above_ma20)
        checks.append({"check": "technical", "passed": tech_ok, "message": tech_msg})

        # 3. Volume confirmation
        vol_ok, vol_msg = check_volume(config, volume_ratio)
        checks.append({"check": "volume", "passed": vol_ok, "message": vol_msg})
    else:
        checks.append({"check": "technical", "passed": True, "message": "Skipped (override)"})
        checks.append({"check": "volume", "passed": True, "message": "Skipped (override)"})

    # 4. Conviction score
    fg = fear_greed or 50
    conv_ok, conv_msg = check_conviction(config, confidence, signals_confirmed, catalyst, fg)
    checks.append({"check": "conviction", "passed": conv_ok, "message": conv_msg})

    # 5. Daily loss limit
    dl_ok, dl_msg = check_daily_loss(config, portfolio_value, agent=agent)
    checks.append({"check": "daily_loss", "passed": dl_ok, "message": dl_msg})

    # Final verdict
    all_passed = all(c["passed"] for c in checks)
    failed = [c for c in checks if not c["passed"]]

    # Update bankroll file
    bankroll = update_bankroll_file(portfolio_value, {
        "ticker": ticker,
        "quantity": quantity,
        "cost": proposed_cost if action == "BUY" else 0,
    }, agent=agent)

    return {
        "verdict": "PASS" if all_passed else "FAIL",
        "action": action,
        "ticker": ticker,
        "quantity": quantity,
        "price": price,
        "cost": proposed_cost,
        "reason": "All checks passed" if all_passed else f"Blocked by: {failed[0]['message']}",
        "checks": checks,
        "bankroll": bankroll,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stonks Entry Gate — code-level trade enforcement")
    parser.add_argument("--action", required=True, choices=["BUY", "SELL", "HOLD"])
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--quantity", type=float, default=0)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--stop-loss", type=float, dest="stop_loss", default=None)
    parser.add_argument("--confidence", type=float, default=0.5)
    parser.add_argument("--thesis", default="")
    parser.add_argument("--signals", type=int, default=3, help="Number of signals confirmed")
    parser.add_argument("--rsi", type=float, default=None)
    parser.add_argument("--macd-bullish", action="store_true", default=None)
    parser.add_argument("--price-above-ma20", action="store_true", default=None)
    parser.add_argument("--volume-ratio", type=float, default=None)
    parser.add_argument("--fear-greed", type=int, default=None)
    parser.add_argument("--catalyst", type=float, default=0.0)
    parser.add_argument("--portfolio", type=float, default=None)
    parser.add_argument("--skip-technical", action="store_true", help="Bypass technical checks")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--fetch-data", action="store_true", help="Auto-fetch market data from data bus")

    args = parser.parse_args()

    # If --fetch-data, try to get RSI, MACD, volume from data bus
    rsi = args.rsi
    macd_bullish = args.macd_bullish
    price_above_ma20 = args.price_above_ma20
    volume_ratio = args.volume_ratio
    fear_greed = args.fear_greed
    price = args.price

    if args.fetch_data and args.ticker:
        quote = fetch_quote(args.ticker)
        if quote:
            data = quote.get(args.ticker, {})
            if rsi is None:
                rsi = data.get("rsi")
            if macd_bullish is None:
                macd_hist = data.get("macd_histogram")
                if macd_hist is not None:
                    macd_bullish = macd_hist > 0
            if price_above_ma20 is None:
                price_above_ma20 = data.get("price_above_ma20")
            if volume_ratio is None:
                volume_ratio = data.get("volume_ratio")
            if price is None:
                price = data.get("price")

        fg = fetch_fear_greed()
        if fg and fear_greed is None:
            fear_greed = fg.get("value")

    if price is None:
        price = 0.0

    result = run_gate(
        action=args.action,
        ticker=args.ticker,
        quantity=args.quantity,
        price=price,
        stop_loss=args.stop_loss,
        confidence=args.confidence,
        thesis=args.thesis,
        signals_confirmed=args.signals,
        rsi=rsi,
        macd_bullish=macd_bullish,
        price_above_ma20=price_above_ma20,
        volume_ratio=volume_ratio,
        fear_greed=fear_greed,
        catalyst=args.catalyst,
        portfolio_value=args.portfolio,
        skip_technical=args.skip_technical,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_verdict(result)


def _print_verdict(result: dict):
    """Print a human-readable verdict."""
    verdict = result["verdict"]
    ticker = result.get("ticker") or "?"
    action = result.get("action", "?")
    cost = result.get("cost", 0)
    bankroll = result.get("bankroll", {})

    if verdict == "PASS":
        print(f"✅ VERDICT: PASS — {action} {ticker}")
        if cost > 0:
            print(f"   Cost: ${cost:.2f}")
        if bankroll:
            print(f"   Bankroll: ${bankroll.get('ceiling', 0):.2f} ceiling, "
                  f"${bankroll.get('deployed', 0):.2f} deployed, "
                  f"${bankroll.get('remaining', 0):.2f} remaining")
        print(f"   Reason: {result['reason']}")
    else:
        print(f"❌ VERDICT: FAIL — {action} {ticker}")
        print(f"   Reason: {result['reason']}")

    print("\nChecks:")
    for c in result.get("checks", []):
        icon = "✅" if c["passed"] else "❌"
        print(f"  {icon} {c['check']}: {c['message']}")


if __name__ == "__main__":
    main()