"""
Trade execution module with bankroll safety gates.

Provides:
  - gate_bankroll(): Pre-trade bankroll ceiling validation with fail-open
  - execute_trade(): Simulated trade execution with bankroll check

Usage:
    from src.executor import gate_bankroll

    granted, reason = gate_bankroll(action, context, bankroll_state)
    if not granted:
        log.warning("Bankroll gate blocked: %s", reason)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def gate_bankroll(
    action: Dict[str, Any],
    context: Dict[str, Any],
    bankroll_state: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Bankroll gate: validate trade against available ceiling.

    Checks that the proposed trade cost doesn't exceed the available
    bankroll ceiling (after accounting for already-deployed positions).

    Args:
        action: Trade action dict with 'type', 'ticker', 'quantity', 'price'.
        context: Portfolio context with 'portfolio_value', 'cash', 'positions'.
        bankroll_state: Optional bankroll state dict with 'ceiling' field.

    Returns:
        (granted: bool, reason: str) — True if trade is allowed.

    Fallback behavior:
        - Non-BUY actions: always pass (only BUY is gated)
        - Missing portfolio_value: fail-open (skip gate)
        - No bankroll_state provided: fail-open (skip gate)
        - Zero ceiling: fail-open (skip gate)
    """
    action_type = str(action.get("type", action.get("action", ""))).upper()
    if action_type != "BUY":
        return True, "BankrollGate: non-BUY action, skipped"

    # Fail-open when portfolio_value is missing (defensive)
    portfolio_value = context.get("portfolio_value")
    if portfolio_value is None or portfolio_value == 0:
        return True, "BankrollGate: no portfolio value, fail-open (skipped)"

    ticker = str(action.get("ticker", "")).upper()
    quantity = float(action.get("quantity", 0) or 0)
    price = float(action.get("price", action.get("current_price", 0)) or 0)
    cost = quantity * price

    if cost <= 0:
        return True, "BankrollGate: zero-cost trade, granted"

    # Fail-open if no bankroll state available
    if bankroll_state is None:
        return True, "BankrollGate: no bankroll state, fail-open (skipped)"

    ceiling = float(bankroll_state.get("ceiling", 0) or 0)
    if ceiling <= 0:
        return True, "BankrollGate: zero ceiling, fail-open (skipped)"

    # Check: does this single trade exceed the ceiling?
    if cost > ceiling:
        return False, (
            f"BankrollGate: BUY {ticker} costs ${cost:,.2f} "
            f"but ceiling is ${ceiling:,.2f}"
        )

    # Check: how much of ceiling is already deployed?
    positions: list[Dict[str, Any]] = context.get("positions", []) or []
    deployed = 0.0
    for p in positions:
        mv = p.get("market_value")
        if mv is not None:
            deployed += float(mv)
        else:
            qty = float(p.get("quantity", 0) or 0)
            entry = float(p.get("entry_price", 0) or 0)
            curr = float(p.get("current_price", 0) or 0)
            deployed += qty * max(curr, entry)

    remaining = ceiling - deployed

    if cost > remaining:
        return False, (
            f"BankrollGate: BUY {ticker} costs ${cost:,.2f} "
            f"but only ${remaining:,.2f} remaining on ceiling "
            f"(ceiling=${ceiling:,.2f}, deployed=${deployed:,.2f})"
        )

    return True, (
        f"BankrollGate: BUY {ticker} costs ${cost:,.2f}, "
        f"ceiling=${ceiling:,.2f}, deployed=${deployed:,.2f}, "
        f"remaining=${remaining:,.2f} — OK"
    )


def execute_trade(
    action: Dict[str, Any],
    context: Dict[str, Any],
    bankroll_state: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Execute a trade after bankroll gate check.

    Combines gate_bankroll with a simulated execution step.

    Args:
        action: Trade action dict.
        context: Portfolio context.
        bankroll_state: Optional bankroll state.

    Returns:
        (success, reason, result_or_none) tuple.
    """
    # First, check bankroll gate
    granted, reason = gate_bankroll(action, context, bankroll_state)
    if not granted:
        return False, reason, None

    action_type = str(action.get("type", action.get("action", ""))).upper()

    if action_type != "BUY":
        return True, f"Non-BUY action ({action_type}): simulated", {
            "action": action_type,
            "simulated": True,
        }

    # Simulate BUY execution
    ticker = str(action.get("ticker", "")).upper()
    quantity = float(action.get("quantity", 0) or 0)
    price = float(action.get("price", action.get("current_price", 0)) or 0)
    cost = quantity * price

    portfolio_value = float(context.get("portfolio_value", 0) or 0)
    post_trade_cash = float(context.get("cash", 0) or 0) - cost
    post_trade_equity = portfolio_value  # simplified: equity doesn't change on execution

    return True, f"BUY {ticker} {quantity} @ ${price:.2f} = ${cost:,.2f}", {
        "action": "BUY",
        "ticker": ticker,
        "quantity": quantity,
        "price": price,
        "cost": cost,
        "post_trade_cash": post_trade_cash,
        "post_trade_equity": post_trade_equity,
    }
