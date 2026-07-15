#!/usr/bin/env python3
"""
Alpaca Trade Executor — place buy/sell orders and check portfolio.

Each agent has their own Alpaca paper trading account.
This script uses the data bus Alpaca client to execute trades.

Usage:
    python3 src/skill_alpaca.py --account stonks --portfolio
    python3 src/skill_alpaca.py --account stonks --buy FUBO --qty 3 --stop-loss 8.94
    python3 src/skill_alpaca.py --account kairos --sell SOFI --qty 2
    python3 src/skill_alpaca.py --account aldridge --buy AAPL --qty 1
    python3 src/skill_alpaca.py --account stonks --portfolio --json
"""

import argparse
import json
import logging
import os
import sys
from typing import Optional

log = logging.getLogger("skill_alpaca")

# Alpaca env var names per account
ACCOUNT_ENV = {
    "stonks": {
        "key": "STONKS_API_KEY",
        "secret": "STONKS_SECRET_KEY",
        "alt_key": "ALPACA_STONKS_KEY",
        "alt_secret": "ALPACA_STONKS_SECRET",
    },
    "kairos": {
        "key": "KAIROS_API_KEY",
        "secret": "KAIROS_SECRET_KEY",
        "alt_key": "ALPACA_KAIROS_KEY",
        "alt_secret": "ALPACA_KAIROS_SECRET",
    },
    "aldridge": {
        "key": "ALDRIDGE_API_KEY",
        "secret": "ALDRIDGE_SECRET_KEY",
        "alt_key": "ALPACA_ALDRIDGE_KEY",
        "alt_secret": "ALPACA_ALDRIDGE_SECRET",
    },
}


def get_alpaca_client(account: str):
    """Create an Alpaca trading client for the given account."""
    env = ACCOUNT_ENV[account]
    api_key = os.getenv(env["key"]) or os.getenv(env["alt_key"])
    secret_key = os.getenv(env["secret"]) or os.getenv(env["alt_secret"])

    if not api_key or not secret_key:
        print(f"ERROR: Alpaca credentials not found for {account}", file=sys.stderr)
        print(f"  Set {env['key']} and {env['secret']} env vars", file=sys.stderr)
        sys.exit(1)

    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
    except ImportError:
        print("ERROR: alpaca-py not installed. Run: pip install alpaca-py", file=sys.stderr)
        sys.exit(1)

    client = TradingClient(api_key, secret_key, paper=True)
    # Store imports for use in other functions
    client._OrderSide = OrderSide
    client._TimeInForce = TimeInForce
    client._OrderType = OrderType
    client._MarketOrderRequest = MarketOrderRequest
    client._StopLossRequest = StopLossRequest
    client._LimitOrderRequest = LimitOrderRequest

    # Validate by fetching account
    try:
        acc = client.get_account()
        if acc.status != "ACTIVE":
            print(f"WARNING: Account status is {acc.status}, not ACTIVE", file=sys.stderr)
    except Exception as e:
        print(f"ERROR: Failed to connect to Alpaca: {e}", file=sys.stderr)
        sys.exit(1)

    return client


def get_portfolio(client, account: str, json_output: bool = False) -> dict:
    """Fetch portfolio state from Alpaca."""
    acc = client.get_account()
    positions = client.get_all_positions()

    portfolio = {
        "account": account,
        "cash": float(acc.cash),
        "portfolio_value": float(acc.portfolio_value),
        "buying_power": float(acc.buying_power),
        "equity": float(acc.equity),
        "unrealized_pl": float(acc.unrealized_pl),
        "unrealized_plpc": float(acc.unrealized_plpc),
        "daytrade_count": int(acc.daytrade_count),
        "status": acc.status,
        "positions": [],
    }

    for p in positions:
        portfolio["positions"].append({
            "ticker": p.symbol,
            "qty": float(p.qty),
            "avg_entry": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc),
            "cost_basis": float(p.cost_basis),
        })

    if json_output:
        print(json.dumps(portfolio, indent=2))
    else:
        print(f"\n=== {account.upper()} Alpaca Portfolio ===")
        print(f"  Portfolio Value: ${portfolio['portfolio_value']:,.2f}")
        print(f"  Cash: ${portfolio['cash']:,.2f}")
        print(f"  Buying Power: ${portfolio['buying_power']:,.2f}")
        print(f"  Unrealized P&L: ${portfolio['unrealized_pl']:+,.2f}")
        print(f"  Positions: {len(portfolio['positions'])}")
        for p in portfolio["positions"]:
            pl = p["unrealized_plpc"] * 100
            print(f"    {p['ticker']}: {p['qty']} @ ${p['avg_entry']:.2f} "
                  f"(current: ${p['current_price']:.2f}, P&L: {pl:+.2f}%)")
        print()

    return portfolio


def buy(client, account: str, ticker: str, qty: int, stop_loss: Optional[float] = None,
         limit_price: Optional[float] = None, json_output: bool = False):
    """Place a BUY order on Alpaca with pre-trade validation."""
    ticker = ticker.upper()

    # Check market hours first
    clock = client.get_clock()
    if not clock.is_open:
        msg = f"Market is closed. Opens at {clock.next_open}"
        print(f"ERROR: {msg}", file=sys.stderr)
        if json_output:
            print(json.dumps({"status": "market_closed", "next_open": str(clock.next_open), "error": msg}))
        return None

    # Pre-trade cash validation
    try:
        acct = client.get_account()
        cash = float(acct.cash)
        buying_power = float(acct.buying_power)
        # Estimate cost with 5% buffer for slippage
        estimated_cost = qty * (limit_price if limit_price else 0)
        if estimated_cost <= 0:
            # Try to get current price for cost estimate
            estimated_cost = qty * 0  # Can't estimate, skip check
        estimated_cost_with_buffer = estimated_cost * 1.05

        if estimated_cost_with_buffer > buying_power:
            msg = (
                f"Insufficient buying power: estimated cost ${estimated_cost_with_buffer:,.2f} "
                f"exceeds buying power ${buying_power:,.2f} (cash ${cash:,.2f})"
            )
            print(f"ERROR: {msg}", file=sys.stderr)
            if json_output:
                print(json.dumps({
                    "status": "rejected",
                    "error": msg,
                    "cash": cash,
                    "buying_power": buying_power,
                    "estimated_cost": estimated_cost_with_buffer,
                }))
            return None
    except Exception as e:
        log.warning("Pre-trade cash check failed (non-fatal): %s", e)

    # Build order request
    try:
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        OrderSide = client._OrderSide
        TimeInForce = client._TimeInForce

        if limit_price and limit_price > 0:
            from alpaca.trading.requests import LimitOrderRequest
            from alpaca.trading.enums import OrderType
            order_req = LimitOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.BUY,
                limit_price=limit_price,
                time_in_force=TimeInForce.DAY,
            )
        else:
            order_req = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
    except ImportError as e:
        msg = f"Alpaca SDK import failed: {e}"
        print(f"ERROR: {msg}", file=sys.stderr)
        if json_output:
            print(json.dumps({"status": "error", "error": msg}))
        return None

    try:
        order = client.submit_order(order_req)
        result = {
            "status": "filled" if order.filled_at else "submitted",
            "order_id": order.id,
            "ticker": ticker,
            "qty": qty,
            "filled_qty": float(order.filled_qty or 0),
            "filled_avg_price": float(order.filled_avg_price or 0),
            "created_at": str(order.created_at),
        }

        # Place stop-loss order as a separate GTC sell stop order
        if stop_loss and float(order.filled_qty or 0) > 0:
            try:
                from alpaca.trading.requests import StopOrderRequest
                sl_order_req = StopOrderRequest(
                    symbol=ticker,
                    qty=float(order.filled_qty),
                    side=OrderSide.SELL,
                    stop_price=float(stop_loss),
                    time_in_force=TimeInForce.GTC,
                )
                sl_order = client.submit_order(sl_order_req)
                result["stop_loss_order_id"] = sl_order.id
                result["stop_loss_price"] = float(stop_loss)
            except Exception as e:
                log.warning("Stop-loss order placement failed: %s", e)
                result["stop_loss_error"] = str(e)

        if json_output:
            print(json.dumps(result, indent=2))
        else:
            print(f"\n✓ BUY {ticker} {qty} @ ${float(order.filled_avg_price or 0):.2f}")
            print(f"  Order ID: {order.id}")
            print(f"  Status: {order.status}")
            if stop_loss:
                print(f"  Stop-loss: ${stop_loss:.2f}")
        return result

    except Exception as e:
        err_msg = str(e)
        print(f"ERROR: Order failed: {err_msg}", file=sys.stderr)
        if json_output:
            print(json.dumps({"status": "rejected", "error": err_msg, "ticker": ticker, "qty": qty}))
        return None


def sell(client, account: str, ticker: str, qty: int,
         json_output: bool = False):
    """Place a SELL order on Alpaca."""
    ticker = ticker.upper()

    clock = client.get_clock()
    if not clock.is_open:
        msg = f"Market is closed. Opens at {clock.next_open}"
        print(f"ERROR: {msg}", file=sys.stderr)
        if json_output:
            print(json.dumps({"status": "market_closed", "next_open": str(clock.next_open), "error": msg}))
        return None

    order_req = client._MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=client._OrderSide.SELL,
        time_in_force=client._TimeInForce.DAY,
    )

    try:
        order = client.submit_order(order_req)
        result = {
            "status": "filled" if order.filled_at else "submitted",
            "order_id": order.id,
            "ticker": ticker,
            "qty": qty,
            "filled_qty": float(order.filled_qty or 0),
            "filled_avg_price": float(order.filled_avg_price or 0),
            "created_at": str(order.created_at),
        }

        if json_output:
            print(json.dumps(result, indent=2))
        else:
            print(f"\n✓ SELL {ticker} {qty} @ ${float(order.filled_avg_price or 0):.2f}")
            print(f"  Order ID: {order.id}")
            print(f"  Status: {order.status}")
        return result

    except Exception as e:
        err_msg = str(e)
        print(f"ERROR: Order failed: {err_msg}", file=sys.stderr)
        if json_output:
            print(json.dumps({"status": "rejected", "error": err_msg, "ticker": ticker, "qty": qty}))
        return None


def main():
    parser = argparse.ArgumentParser(description="Alpaca Trade Executor")
    parser.add_argument("--account", required=True, choices=["stonks", "kairos", "aldridge"],
                        help="Which trader account")
    parser.add_argument("--portfolio", action="store_true", help="Show portfolio")
    parser.add_argument("--buy", metavar="TICKER", help="Buy ticker")
    parser.add_argument("--sell", metavar="TICKER", help="Sell ticker")
    parser.add_argument("--qty", type=float, help="Quantity")
    parser.add_argument("--stop-loss", type=float, help="Stop-loss price (buy only)")
    parser.add_argument("--limit", type=float, help="Limit price")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    client = get_alpaca_client(args.account)

    if args.portfolio:
        get_portfolio(client, args.account, json_output=args.json)
    elif args.buy:
        if not args.qty:
            print("ERROR: --qty required for buy orders", file=sys.stderr)
            sys.exit(1)
        buy(client, args.account, args.buy, args.qty,
            stop_loss=args.stop_loss, limit_price=args.limit,
            json_output=args.json)
    elif args.sell:
        if not args.qty:
            print("ERROR: --qty required for sell orders", file=sys.stderr)
            sys.exit(1)
        sell(client, args.account, args.sell, args.qty, json_output=args.json)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()