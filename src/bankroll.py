"""
Bankroll management — equity-scaled risk ceiling with win/loss-reactive dynamics.

Provides:
  - Equity-scaled ceiling: ceiling_pct * current_equity instead of fixed dollars
  - Win/loss-reactive: recalc_ceiling() with 1.02 win / 0.99 loss multipliers
  - Graduated position sizing: scale max_position_pct based on proven win rate
  - Competition multiplier: time-based risk ramp for tournament progression

Usage:
    from src.bankroll import (
        recalc_ceiling,
        effective_ceiling,
        read_bankroll,
        write_bankroll,
        graduated_max_position_pct,
        write_max_position_pct_to_params,
    )

    state = read_bankroll("state/bankroll.json")
    state = recalc_ceiling(state)
    ceiling = effective_ceiling(state, current_equity=10500.0)
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

# Equity-scaled ceiling pct constants
STARTING_CEILING_PCT = 0.20   # 20% of live equity
FLOOR_PCT = 0.01             # 1% floor
MAX_CEILING_PCT = 0.40       # 40% max

# Graduated position sizing constants
MIN_SAMPLES = 20
BASE_MAX_POSITION_PCT = 10.0   # Default max position size % (10% of portfolio)
PROVEN_MAX_POSITION_PCT = 25.0  # Max when proven track record exists

# Win rate breakpoints for graduated sizing
WINRATE_PROVEN_THRESHOLD = 0.55   # Above: unlock PROVEN_MAX_POSITION_PCT
WINRATE_CONSERVATIVE_THRESHOLD = 0.45  # Below: stay at BASE_MAX_POSITION_PCT

# ═══════════════════════════════════════════════════════════════════════════════
# Competition Multiplier
# ═══════════════════════════════════════════════════════════════════════════════


def competition_multiplier(today: Optional[datetime] = None) -> float:
    """Competition time-based risk multiplier.

    As the tournament progresses (Jul 9 → Dec 31, 2026), the multiplier
    ramps linearly from 1.0 to 1.3, encouraging calculated risk-taking
    in the final stretch.

    Args:
        today: Date to compute multiplier for (defaults to now).

    Returns:
        Multiplier between 1.0 and 1.3.
    """
    if today is None:
        today = datetime.now()

    start = datetime(2026, 7, 9)
    end = datetime(2026, 12, 31)

    if today < start or today > end:
        return 1.0

    total_days = (end - start).days
    elapsed = (today - start).days
    progress = elapsed / total_days if total_days > 0 else 0.0

    # Linear ramp from 1.0 → 1.3
    return 1.0 + 0.3 * max(0.0, min(1.0, progress))


# ═══════════════════════════════════════════════════════════════════════════════
# UNIVERSE_MAX_PRICE_TIERS
# ═══════════════════════════════════════════════════════════════════════════════

# Re-keyed from fixed-dollar ceiling to ceiling_pct-based tiers.
# Each entry: (ceiling_pct_bracket, max_stock_price)
# Higher ceiling_pct = access to more expensive stocks.
UNIVERSE_MAX_PRICE_TIERS: List[Tuple[float, float]] = [
    (0.00, 30.0),    # < 0%: $30  (absolute floor)
    (0.01, 40.0),    # 1%:   $40
    (0.03, 60.0),    # 3%:   $60
    (0.05, 80.0),    # 5%:   $80
    (0.067, 100.0),  # 6.7%: $100 (starting)
    (0.10, 150.0),   # 10%:  $150
    (0.15, 200.0),   # 15%:  $200
    (0.19, 250.0),   # 19%:  $250 (max)
]


def universe_max_price_for_ceiling(ceiling_pct: float, equity: float) -> float:
    """Return max stock price the trader can buy based on ceiling_pct and equity.

    Uses the ceiling_pct bracket to determine base price tier, then
    scales by equity factor (higher equity = access to pricier stocks).

    Args:
        ceiling_pct: Current ceiling_pct (0.0-1.0 range, e.g. 0.067).
        equity: Current portfolio equity in dollars.

    Returns:
        Max stock price the trader can buy.
    """
    # Find base price from ceiling_pct tier
    # Walk tiers: use the price of the highest bracket where ceiling_pct >= bracket_pct
    base_price = 10.0  # absolute floor
    for bracket_pct, price in sorted(UNIVERSE_MAX_PRICE_TIERS):
        if ceiling_pct < bracket_pct:
            break
        base_price = price

    # Equity adjustment: $10K → 1.0x, $50K → 2.0x, $100K → 3.0x
    equity_factor = max(1.0, equity / 10_000.0)

    return min(base_price * equity_factor, 500.0)  # hard cap at $500


# ═══════════════════════════════════════════════════════════════════════════════
# Bankroll State
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_BANKROLL_STATE: Dict[str, Any] = {
    "ceiling_pct": STARTING_CEILING_PCT,
    "ceiling": STARTING_CEILING_PCT * 10_000,  # derived/logged value (~$670)
    "wins": 0,
    "losses": 0,
    "streak": 0,     # positive = win streak, negative = loss streak
    "peak_pct": STARTING_CEILING_PCT,
}


def recalc_ceiling(state: Dict[str, Any]) -> Dict[str, Any]:
    """Recalculate ceiling_pct based on win/loss streak.

    Win/loss-reactive dynamics (exact same multipliers):
      - Win streak (streak > 0): ceiling_pct *= 1.02
      - Loss streak (streak < 0): ceiling_pct *= 0.99
      - Streak == 0: no change

    Clamped to [FLOOR_PCT, MAX_CEILING_PCT].

    Operates on state["ceiling_pct"] instead of state["ceiling"].
    Updates the derived/logged "ceiling" field for backward compat.

    Args:
        state: Bankroll state dict with at least "ceiling_pct" and "streak".

    Returns:
        Updated state dict (same reference, modified in-place).
    """
    pct = state.get("ceiling_pct", STARTING_CEILING_PCT)
    streak = state.get("streak", 0)

    if streak > 0:
        # Win streak: expand risk ceiling
        pct *= 1.02
    elif streak < 0:
        # Loss streak: contract risk ceiling
        pct *= 0.99

    # Clamp to [FLOOR_PCT, MAX_CEILING_PCT]
    state["ceiling_pct"] = max(FLOOR_PCT, min(MAX_CEILING_PCT, pct))

    # Update derived ceiling field (at 10K nominal equity for backward compat)
    state["ceiling"] = state["ceiling_pct"] * 10_000

    # Track peak
    state["peak_pct"] = max(state.get("peak_pct", FLOOR_PCT), state["ceiling_pct"])

    return state


def effective_ceiling(
    state: Dict[str, Any],
    current_equity: float,
    today: Optional[datetime] = None,
) -> float:
    """Compute the effective dollar ceiling.

    Formula:
        ceiling_pct * current_equity * competition_multiplier(today)

    Hard-capped at MAX_CEILING_PCT * current_equity to prevent runaway.

    Args:
        state: Bankroll state dict with "ceiling_pct".
        current_equity: Current portfolio equity in dollars.
        today: Date for competition multiplier (defaults to now).

    Returns:
        Effective ceiling in dollars.
    """
    pct = state.get("ceiling_pct", STARTING_CEILING_PCT)
    mult = competition_multiplier(today)
    raw = pct * current_equity * mult
    cap = MAX_CEILING_PCT * current_equity
    return min(raw, cap)


# ═══════════════════════════════════════════════════════════════════════════════
# File-based I/O
# ═══════════════════════════════════════════════════════════════════════════════


def read_bankroll(path: str) -> Dict[str, Any]:
    """Read bankroll state from a JSON file.

    Falls back to DEFAULT_BANKROLL_STATE if the file doesn't exist
    or is corrupt.

    Uses ceiling_pct field, defaults to STARTING_CEILING_PCT.

    Args:
        path: Path to JSON bankroll file.

    Returns:
        Bankroll state dict.
    """
    if not os.path.exists(path):
        return dict(DEFAULT_BANKROLL_STATE)

    try:
        with open(path) as f:
            data: Dict[str, Any] = json.load(f)

        # Ensure ceiling_pct field exists — migrate from old ceiling-only format
        if "ceiling_pct" not in data:
            old_ceiling = data.get("ceiling", STARTING_CEILING_PCT * 10_000)
            data["ceiling_pct"] = old_ceiling / 10_000 if old_ceiling else STARTING_CEILING_PCT

        # Ensure all default keys exist
        for key, default in DEFAULT_BANKROLL_STATE.items():
            data.setdefault(key, default)

        return data
    except (json.JSONDecodeError, IOError, OSError):
        return dict(DEFAULT_BANKROLL_STATE)


def write_bankroll(path: str, state: Dict[str, Any]) -> None:
    """Write bankroll state to a JSON file.

    Ensures ceiling_pct field is always present.

    Args:
        path: Path to JSON bankroll file.
        state: Bankroll state dict to persist.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    out = dict(state)
    if "ceiling_pct" not in out:
        out["ceiling_pct"] = STARTING_CEILING_PCT

    # Keep derived ceiling field for log compatibility
    if "ceiling" not in out:
        out["ceiling"] = out["ceiling_pct"] * 10_000

    with open(path, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")


# ═══════════════════════════════════════════════════════════════════════════════
# Graduated Position Sizing
# ═══════════════════════════════════════════════════════════════════════════════


def graduated_max_position_pct(
    closed_trades: Optional[List[Dict[str, Any]]] = None,
    win_rate: Optional[float] = None,
    n_trades: Optional[int] = None,
) -> float:
    """Determine max position size % based on proven track record.

    Scales position size between BASE_MAX_POSITION_PCT (6.0%) and
    PROVEN_MAX_POSITION_PCT (15.0%) based on win rate.

    Logic:
        - Below MIN_SAMPLES (20 closed trades): return BASE_MAX_POSITION_PCT
        - win_rate > 55%: return PROVEN_MAX_POSITION_PCT (full confidence)
        - win_rate < 45%: return BASE_MAX_POSITION_PCT (conservative)
        - 45-55%: linear interpolation between 6.0% and 15.0%

    Args:
        closed_trades: List of closed trade dicts. Each must have a 'pnl'
                       field (positive = win). If None, uses win_rate/n_trades.
        win_rate: Override win rate (0.0-1.0). If None, computed from trades.
        n_trades: Override trade count. If None, computed from trades.

    Returns:
        Max position size as a percentage (e.g. 6.0 = 6% of portfolio).
    """
    # Determine n_trades and win_rate from available data
    if closed_trades is not None:
        n = len(closed_trades)
        if win_rate is None:
            if n > 0:
                wins = sum(1 for t in closed_trades if t.get("pnl", 0) is not None and t["pnl"] > 0)
                wr = wins / n
            else:
                wr = 0.0
        else:
            wr = win_rate
    else:
        n = n_trades or 0
        wr = win_rate or 0.0

    # Below minimum samples: stick with base
    if n < MIN_SAMPLES:
        return BASE_MAX_POSITION_PCT

    # Proven above 55% WR: unlock full position size
    if wr > WINRATE_PROVEN_THRESHOLD:
        return PROVEN_MAX_POSITION_PCT

    # Proven below 45% WR: stay conservative
    if wr < WINRATE_CONSERVATIVE_THRESHOLD:
        return BASE_MAX_POSITION_PCT

    # Between 45-55%: linear interpolation
    # 45% → BASE (6%), 55% → PROVEN (15%)
    t = (wr - WINRATE_CONSERVATIVE_THRESHOLD) / (
        WINRATE_PROVEN_THRESHOLD - WINRATE_CONSERVATIVE_THRESHOLD
    )
    return BASE_MAX_POSITION_PCT + t * (PROVEN_MAX_POSITION_PCT - BASE_MAX_POSITION_PCT)


def write_max_position_pct_to_params(
    value: float,
    params_path: str,
) -> None:
    """Write max_position_pct into params.json under risk.max_position_pct.

    Creates params.json if it doesn't exist, preserving all existing keys.

    Args:
        value: Max position size as a percentage (e.g. 6.0 = 6%).
        params_path: Path to params.json.
    """
    # Load existing or create empty
    if os.path.exists(params_path):
        try:
            with open(params_path) as f:
                params: Dict[str, Any] = json.load(f)
        except (json.JSONDecodeError, IOError):
            params = {}
    else:
        params = {}

    # Ensure risk section exists
    if "risk" not in params:
        params["risk"] = {}

    # Set the graduated value as max_position_pct (store as 0-100 % value)
    params["risk"]["max_position_pct"] = value

    # Write back
    os.makedirs(os.path.dirname(params_path) or ".", exist_ok=True)
    with open(params_path, "w") as f:
        json.dump(params, f, indent=2)
        f.write("\n")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    """CLI entry point for bankroll operations."""
    import argparse

    parser = argparse.ArgumentParser(description="Bankroll management")
    parser.add_argument("--state-path", default="state/bankroll.json",
                        help="Path to bankroll state JSON")

    sub = parser.add_subparsers(dest="command")

    # Show
    sub.add_parser("show", help="Show current bankroll state")

    # Recalibrate
    recal = sub.add_parser("recalibrate", help="Recalc ceiling based on streak")
    recal.add_argument("--streak", type=int, default=0,
                       help="Current win/loss streak")
    recal.add_argument("--wins", type=int, default=0, help="Total wins")
    recal.add_argument("--losses", type=int, default=0, help="Total losses")
    recal.add_argument("--equity", type=float, default=10_000.0,
                       help="Current portfolio equity")

    # Graduated sizing
    grad = sub.add_parser("graduated", help="Show graduated position sizing")
    grad.add_argument("--trades", type=int, default=20,
                      help="Number of closed trades")
    grad.add_argument("--win-rate", type=float, default=0.50,
                      help="Win rate (0.0-1.0)")

    args = parser.parse_args()

    if args.command == "show":
        state = read_bankroll(args.state_path)
        print(f"ceiling_pct: {state['ceiling_pct']:.3%}")
        print(f"ceiling (derived): ${state['ceiling']:,.2f}")
        print(f"wins: {state['wins']}")
        print(f"losses: {state['losses']}")
        print(f"streak: {state['streak']}")
        print(f"peak_pct: {state['peak_pct']:.3%}")

    elif args.command == "recalibrate":
        state = read_bankroll(args.state_path)
        state["streak"] = args.streak
        state["wins"] = args.wins
        state["losses"] = args.losses
        state = recalc_ceiling(state)
        print(f"New ceiling_pct: {state['ceiling_pct']:.3%}")
        effective = effective_ceiling(state, current_equity=args.equity)
        print(f"Effective ceiling at ${args.equity:,.2f} equity: ${effective:,.2f}")

    elif args.command == "graduated":
        pct = graduated_max_position_pct(
            win_rate=args.win_rate,
            n_trades=args.trades,
        )
        print(f"Graduated max_position_pct: {pct:.1f}%")
        print(f"(based on {args.trades} trades, {args.win_rate:.1%} win rate)")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
