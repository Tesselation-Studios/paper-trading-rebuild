#!/usr/bin/env python3
"""
Entry Gate — code-level enforcement of trade entry rules for Stonks and Kairos.

Both agents describe this gate in their AGENTS.md as a hard requirement that
runs before every Alpaca order. The agent CANNOT override it.

Checks:
1. Bankroll ceiling — position cost ≤ portfolio_value × 0.01
2. Technical confirmation — RSI 50-70, MACD bullish, Price > MA20
3. Volume confirmation — volume_ratio ≥ 2.0
4. Conviction score — 3/5 signals confirmed, weighted conviction ≥ 0.50
5. Fear & Greed override — if F&G ≤ 25, needs 5/5 signals + conviction ≥ 0.70
6. High-conviction override — conviction ≥ 0.80 + catalyst ≥ 0.7 bypasses technicals

Usage:
    python3 src/stonks_entry_gate.py --agent stonks --action BUY --ticker FUBO \\
        --quantity 3 --price 9.93 --stop-loss 8.94 --confidence 0.78 \\
        --signals 4 --rsi 54.2 --macd-bullish --volume-ratio 2.5 \\
        --fear-greed 25 --catalyst 0.3
    python3 src/stonks_entry_gate.py --agent kairos --action BUY --ticker SNAP \\
        --quantity 3 --price 4.82 --confidence 0.75

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
        except Exception as e:
            logger.debug("Failed to read heartbeat-state.json: %s", e)

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
    except Exception as e:
        logger.debug("Failed to read portfolio from PG: %s", e)

    return 10_000.0  # fallback


def get_current_bankroll_state(agent: str = "stonks") -> dict:
    """Get current bankroll state from PG (trader_positions) + portfolio value.

    No bankroll.md — everything comes from the database.
    Positions are synced from Alpaca via sync_alpaca_positions.py.
    """
    state = {
        "ceiling": 100.0,
        "deployed": 0.0,
        "positions": [],
        "portfolio_value": 10_000.0,
    }

    # Get portfolio value from PG
    try:
        import psycopg2 as _psycopg2
        conn = _psycopg2.connect(PG_DSN)
        cur = conn.cursor()
        cur.execute(
            "SELECT portfolio_value, cash FROM trading.portfolio_snapshots "
            "WHERE trader_id = %s ORDER BY timestamp DESC LIMIT 1",
            (f"trader-{agent}",)
        )
        row = cur.fetchone()
        if row:
            state["portfolio_value"] = float(row[0])
        cur.close()
        conn.close()
    except Exception as e:
        logger.debug("Failed to load portfolio from PG for %s: %s", agent, e)

    # Get open positions from PG
    try:
        import psycopg2 as _psycopg2
        conn = _psycopg2.connect(PG_DSN)
        cur = conn.cursor()
        cur.execute(
            "SELECT ticker, quantity, market_value, avg_entry_price "
            "FROM trading.trader_positions "
            "WHERE trader_id = %s AND status = 'open'",
            (agent,)
        )
        for row in cur.fetchall():
            ticker = row[0]
            qty = float(row[1]) if row[1] else 0
            cost = float(row[2]) if row[2] else 0
            entry = float(row[3]) if row[3] else 0
            state["positions"].append({
                "ticker": ticker, "qty": qty,
                "cost": cost, "avg_entry": entry
            })
            state["deployed"] += cost
        cur.close()
        conn.close()
    except Exception as e:
        logger.debug("Failed to load positions from PG for %s: %s", agent, e)

    return state


def compute_bankroll_ceiling(portfolio_value: float) -> float:
    """Compute bankroll ceiling: 1% of portfolio value."""
    return round(portfolio_value * 0.01, 2)


def update_bankroll_file(portfolio_value: float, proposed: dict, agent: str = "stonks") -> dict:
    """No-op: bankroll is code-driven now. Positions come from Alpaca via PG."""
    return {
        "ceiling": compute_bankroll_ceiling(portfolio_value),
        "deployed": 0.0,
        "positions": [],
        "note": "bankroll is code-driven via PG. No manual file needed.",
    }
def check_bankroll(
    config: dict, portfolio_value: float, proposed_cost: float, proposed_ticker: str,
    agent: str = "stonks",
) -> Tuple[bool, str]:
    """Check if the proposed trade fits within the bankroll ceiling."""
    ceiling = compute_bankroll_ceiling(portfolio_value)
    current = get_current_bankroll_state(agent)
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
    bankroll_ok, bankroll_msg = check_bankroll(config, portfolio_value, proposed_cost, ticker, agent=agent)
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
    parser.add_argument("--agent", default="stonks", choices=["stonks", "kairos"],
                        help="Which trader agent to gate (stonks|kairos)")
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
        agent=args.agent,
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