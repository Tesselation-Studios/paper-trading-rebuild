#!/usr/bin/env python3
"""Circuit breaker management CLI — check status, reset, or list paused traders.

Usage:
    python3 scripts/check_circuit_breakers.py status          # All traders
    python3 scripts/check_circuit_breakers.py status --trader kairos
    python3 scripts/check_circuit_breakers.py reset --trader kairos
    python3 scripts/check_circuit_breakers.py reset-all
    python3 scripts/check_circuit_breakers.py is-paused --trader kairos  # exit 1 if paused

Hook into cron:
    # Check and auto-recover paused traders every 2 min
    */2 * * * * cd ~/projects/paper-trading-rebuild && python3 scripts/check_circuit_breakers.py auto-recover >> logs/circuit_breaker.log 2>&1
"""

import argparse
import json
import sys
from datetime import datetime

# Ensure we can import from src/
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def get_all_traders():
    return ["trader-kairos", "trader-aldridge", "trader-stonks"]


def cmd_status(args):
    from src.circuit_breaker import AgentCircuitBreaker

    traders = [args.trader] if args.trader else get_all_traders()
    results = {}

    for tid in traders:
        breaker = AgentCircuitBreaker.get(tid)
        status = breaker.status()
        results[tid] = status

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for tid, s in results.items():
            paused = "🛑 PAUSED" if s["is_paused"] else "✅ ACTIVE"
            print(f"{paused}  {tid}")
            if s["is_paused"]:
                print(f"         Reason: {s['paused_reason']}")
            if s["total_trips"] > 0:
                print(f"         Trips: {s['total_trips']}, Last: {s['last_trip_at']}")
            tick = s.get("current_tick")
            if tick and tick["active"]:
                print(f"         In tick: {tick['call_count']} calls, {tick['elapsed_s']}s elapsed")
            print()


def cmd_reset(args):
    from src.circuit_breaker import AgentCircuitBreaker

    if not args.trader:
        print("ERROR: --trader required for reset", file=sys.stderr)
        sys.exit(1)

    breaker = AgentCircuitBreaker.get(args.trader)
    if breaker.reset():
        print(f"[{datetime.now()}] {args.trader}: Circuit breaker reset — trader unpaused")
    else:
        print(f"[{datetime.now()}] {args.trader}: Reset failed", file=sys.stderr)
        sys.exit(1)


def cmd_reset_all(args):
    from src.circuit_breaker import AgentCircuitBreaker

    for tid in get_all_traders():
        breaker = AgentCircuitBreaker.get(tid)
        if breaker.is_paused():
            breaker.reset()
            print(f"[{datetime.now()}] {tid}: Force-reset (unpaused)")


def cmd_is_paused(args):
    from src.circuit_breaker import AgentCircuitBreaker

    if not args.trader:
        print("ERROR: --trader required for is-paused", file=sys.stderr)
        sys.exit(2)

    breaker = AgentCircuitBreaker.get(args.trader)
    if breaker.is_paused():
        print(f"{args.trader}: PAUSED")
        sys.exit(1)
    else:
        print(f"{args.trader}: ACTIVE")
        sys.exit(0)


def cmd_auto_recover(args):
    """Check all traders and auto-unpause those whose cooldown has expired.

    This is safe to call from cron. It respects the auto_pause_minutes
    cooldown — no trader gets unpaused before their cooldown ends.
    """
    from src.circuit_breaker import AgentCircuitBreaker

    recovered = 0
    still_paused = 0

    for tid in get_all_traders():
        breaker = AgentCircuitBreaker.get(tid)
        paused, reason = breaker.check_paused()

        if paused:
            still_paused += 1
            print(f"[{datetime.now()}] {tid}: STILL PAUSED — {reason}")
        elif breaker.state.total_trips > 0:
            # Was previously paused but cooldown expired (check_paused auto-clears)
            recovered += 1
            print(f"[{datetime.now()}] {tid}: RECOVERED — cooldown expired")

    print(f"[{datetime.now()}] Summary: {recovered} recovered, {still_paused} still paused")


def main():
    parser = argparse.ArgumentParser(
        description="Circuit breaker management for paper trading agents"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show breaker status")
    p_status.add_argument("--trader", choices=["kairos", "aldridge", "stonks"])
    p_status.add_argument("--json", action="store_true")

    p_reset = sub.add_parser("reset", help="Reset breaker for one trader")
    p_reset.add_argument("--trader", required=True, choices=["kairos", "aldridge", "stonks"])

    p_reset_all = sub.add_parser("reset-all", help="Force-reset all traders")

    p_paused = sub.add_parser("is-paused", help="Exit 1 if trader is paused")
    p_paused.add_argument("--trader", required=True, choices=["kairos", "aldridge", "stonks"])

    p_recover = sub.add_parser("auto-recover", help="Auto-recover traders whose cooldown expired")

    args = parser.parse_args()

    commands = {
        "status": cmd_status,
        "reset": cmd_reset,
        "reset-all": cmd_reset_all,
        "is-paused": cmd_is_paused,
        "auto-recover": cmd_auto_recover,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
