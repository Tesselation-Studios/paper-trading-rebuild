#!/usr/bin/env python3
"""
sync_trades.py — Sync Alpaca positions → trader.db (positions-based, NOT order history)

v2 FIX: Previously synced from order history, which caused:
  - All trades force-closed with exit_reason='sync_sell' (paper auto-liquidations)
  - Aldridge & Stonks had zero trades in DB (BUY orders never captured correctly)

Now syncs from Alpaca's /positions endpoint — the source of truth for "what do
I currently hold?" If Alpaca says you have it, DB says open. If Alpaca says
you don't, DB says closed.

Usage:
    python3 src/sync_trades.py --agent kairos
    python3 src/sync_trades.py --all
    python3 src/sync_trades.py --agent kairos --dry-run
"""

import os
import sys
import json
import argparse
import sqlite3
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
SHARED_DB = PROJECT_DIR / "shared" / "trader.db"

load_dotenv(Path.home() / ".openclaw" / ".env", override=True)
local_env = PROJECT_DIR / ".env"
if local_env.exists():
    load_dotenv(local_env, override=True)

AGENT_CONFIG = {
    "kairos": {
        "agent_id": "trader-kairos",
        "key_vars": ["ALPACA_KAIROS_KEY", "KAIROS_API_KEY"],
        "secret_vars": ["ALPACA_KAIROS_SECRET", "KAIROS_SECRET_KEY"],
    },
    "stonks": {
        "agent_id": "trader-stonks",
        "key_vars": ["ALPACA_STONKS_KEY", "STONKS_API_KEY"],
        "secret_vars": ["ALPACA_STONKS_SECRET", "STONKS_SECRET_KEY"],
    },
    "aldridge": {
        "agent_id": "trader-aldridge",
        "key_vars": ["ALPACA_ALDRIDGE_KEY", "ALDRIDGE_API_KEY"],
        "secret_vars": ["ALPACA_ALDRIDGE_SECRET", "ALDRIDGE_SECRET_KEY"],
    },
}

for short_name in ["trader-kairos", "trader-stonks", "trader-aldridge"]:
    AGENT_CONFIG[short_name] = AGENT_CONFIG[short_name.split("-")[1]]


def get_agent_creds(agent_name: str):
    cfg = AGENT_CONFIG.get(agent_name, {})
    api_key = ""
    for kv in cfg.get("key_vars", []):
        api_key = os.environ.get(kv, "")
        if api_key:
            break
    secret_key = ""
    for sv in cfg.get("secret_vars", []):
        secret_key = os.environ.get(sv, "")
        if secret_key:
            break
    return cfg.get("agent_id", agent_name), api_key, secret_key


def get_alpaca_positions(api_key: str, secret_key: str) -> list[dict]:
    """Get current positions from Alpaca. Returns list of {symbol, qty, current_price, avg_entry_price, ...}."""
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(api_key, secret_key, paper=True)
        positions = client.get_all_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "current_price": float(p.current_price) if p.current_price else 0,
                "avg_entry_price": float(p.avg_entry_price) if p.avg_entry_price else 0,
                "market_value": float(p.market_value) if p.market_value else 0,
                "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else 0,
                "unrealized_plpc": float(p.unrealized_plpc) if p.unrealized_plpc else 0,
            }
            for p in positions
        ]
    except ImportError:
        print("[sync_trades] ERROR: alpaca-py not installed", file=sys.stderr)
        return []
    except Exception as e:
        print(f"[sync_trades] ERROR fetching positions: {e}", file=sys.stderr)
        return []


def sync_positions(conn, agent_id: str, api_key: str, secret_key: str, dry_run: bool = False):
    """Sync: Alpaca positions → trades table.

    For each Alpaca position:
      - If no matching open BUY trade exists, create one.
      - If a matching open BUY trade exists, update qty/entry_price if changed.

    For each DB trade marked 'open':
      - If NOT in Alpaca positions, close it (the position was sold/liquidated).
    """
    positions = get_alpaca_positions(api_key, secret_key)
    if not positions:
        print(f"[sync_trades] {agent_id}: no positions returned from Alpaca (may be empty portfolio)")
        return 0, 0

    cur = conn.cursor()

    # Get current open trades from DB
    cur.execute(
        "SELECT id, ticker, quantity, entry_price FROM trades "
        "WHERE agent_id = ? AND action = 'buy' AND status = 'open'",
        (agent_id,),
    )
    open_trades = {r[1].upper(): {"id": r[0], "qty": r[2], "entry_price": r[3]} for r in cur.fetchall()}

    created = 0
    updated = 0
    closed = 0
    now = datetime.now().isoformat()

    # Build set of Alpaca symbols for closing check
    alpaca_symbols = set()

    for pos in positions:
        sym = pos["symbol"].upper()
        alpaca_symbols.add(sym)
        qty = pos["qty"]
        entry = pos["avg_entry_price"]

        if sym in open_trades:
            trade = open_trades[sym]
            # Update if quantity or entry price changed
            if abs(trade["qty"] - qty) > 0.001 or abs(trade["entry_price"] - entry) > 0.01:
                if not dry_run:
                    cur.execute(
                        "UPDATE trades SET quantity = ?, entry_price = ?, updated_at = ? WHERE id = ?",
                        (qty, entry, now, trade["id"]),
                    )
                print(f"[sync_trades] {agent_id}: updated {sym} qty={trade['qty']}→{qty} entry={trade['entry_price']}→{entry}")
                updated += 1
            del open_trades[sym]  # Mark as seen
        else:
            # Check if a closed trade exists (position was previously held, sold, re-bought)
            cur.execute(
                "SELECT id FROM trades WHERE agent_id = ? AND ticker = ? AND action = 'buy' AND status = 'closed'",
                (agent_id, sym),
            )
            closed_trade = cur.fetchone()
            if closed_trade:
                # Reopen the closed trade instead of creating a duplicate
                if not dry_run:
                    cur.execute(
                        """UPDATE trades SET quantity = ?, entry_price = ?,
                           entry_timestamp = ?, status = 'open',
                           exit_price = NULL, exit_timestamp = NULL,
                           exit_reason = NULL, pnl = NULL, pnl_pct = NULL,
                           updated_at = ?
                           WHERE id = ?""",
                        (qty, entry, now, now, closed_trade[0]),
                    )
                print(f"[sync_trades] {agent_id}: reopened closed trade for {sym} x{qty} @ ${entry:.2f}")
                created += 1
            else:
                # New position — create open BUY trade
                if not dry_run:
                    cur.execute(
                        """INSERT INTO trades
                           (agent_id, timestamp, decision_id, ticker, action, quantity,
                            entry_price, entry_reason, entry_timestamp, status, updated_at)
                           VALUES (?, ?, 0, ?, 'buy', ?, ?, 'position_sync', ?, 'open', ?)""",
                        (agent_id, now, sym, qty, entry, now, now),
                    )
                print(f"[sync_trades] {agent_id}: created open trade for {sym} x{qty} @ ${entry:.2f}")
                created += 1

    # Close any DB trades that are no longer in Alpaca positions
    for sym, trade in open_trades.items():
        if sym not in alpaca_symbols:
            # Position was sold/liquidated — try to get exit price from recent SELL orders
            exit_price = _find_exit_price(api_key, secret_key, sym, trade["qty"])
            if not dry_run:
                cur.execute(
                    """UPDATE trades
                       SET status = 'closed', exit_price = ?, exit_timestamp = ?,
                           exit_reason = 'position_closed', quantity = ?
                       WHERE id = ?""",
                    (exit_price or 0, now, trade["qty"], trade["id"]),
                )
            print(f"[sync_trades] {agent_id}: closed {sym} — no longer on Alpaca (exit≈${exit_price or '?'})")
            closed += 1

    if not dry_run:
        conn.commit()

    return created, closed


def _find_exit_price(api_key: str, secret_key: str, symbol: str, qty: float) -> float | None:
    """Try to find the exit price from recent SELL orders for reconciliation."""
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import OrderSide, QueryOrderStatus

        client = TradingClient(api_key, secret_key, paper=True)
        req = GetOrdersRequest(side=OrderSide.SELL, status=QueryOrderStatus.CLOSED,
                               symbols=[symbol], limit=5, direction="desc")
        orders = client.get_orders(req)
        for o in orders:
            if o.filled_avg_price and abs(float(o.filled_qty) - qty) < 0.01:
                return float(o.filled_avg_price)
    except Exception:
        pass
    return None


def sync_agent(agent_name: str, dry_run: bool = False):
    if agent_name not in AGENT_CONFIG:
        print(f"[sync_trades] ERROR: Unknown agent '{agent_name}'", file=sys.stderr)
        return

    agent_id, api_key, secret_key = get_agent_creds(agent_name)
    if not api_key or not secret_key:
        print(f"[sync_trades] ERROR: Missing credentials for {agent_name}", file=sys.stderr)
        return

    mode = "DRY RUN" if dry_run else "SYNC"
    print(f"[sync_trades] {mode} {agent_id}...")

    conn = sqlite3.connect(str(SHARED_DB), timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        created, closed = sync_positions(conn, agent_id, api_key, secret_key, dry_run)
        print(f"  Created: {created}, Closed: {closed}")

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trades WHERE agent_id = ? AND status = 'open'", (agent_id,))
        open_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*), SUM(pnl) FROM trades WHERE agent_id = ? AND status = 'closed'",
                    (agent_id,))
        total_closed, total_pnl = cur.fetchone()
        print(f"  State: {open_count} open, {total_closed} closed (P&L ${total_pnl or 0:+.2f})")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Sync Alpaca positions → trader.db")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--agent", help="Agent: kairos, stonks, aldridge")
    group.add_argument("--all", action="store_true", help="Sync all three traders")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, don't write to DB")
    args = parser.parse_args()

    if args.all:
        for name in ["kairos", "stonks", "aldridge"]:
            sync_agent(name, dry_run=args.dry_run)
            print()
    else:
        sync_agent(args.agent, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
