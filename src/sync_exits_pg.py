#!/usr/bin/env python3
"""
sync_exits_pg.py — Poll Alpaca for closed positions and sync exit data to Postgres.

Postgres-native version of sync_exits.py. Writes exclusively to Postgres
on docker.klo:5433 instead of the old shared/trader.db SQLite.

Usage:
    python3 src/sync_exits_pg.py --agent trader-kairos
    python3 src/sync_exits_pg.py --agent kairos
    python3 src/sync_exits_pg.py --all
    python3 src/sync_exits_pg.py --backfill kairos

This is the P0 replacement for sync_exits.py.
"""

import os, sys, json, argparse, uuid
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

import psycopg2
import psycopg2.extras

PROJECT_DIR = Path(__file__).resolve().parent.parent
PG_DSN = os.getenv("PG_DSN", "host=trading-db port=5432 dbname=trading user=trader")

load_dotenv(Path.home() / ".openclaw" / ".env", override=True)
local_env = PROJECT_DIR / ".env"
if local_env.exists():
    load_dotenv(local_env, override=True)

AGENT_CONFIG = {
    "trader-kairos": {
        "trader_id": "kairos",
        "agent_id": "trader-kairos",
        "key_vars": ["ALPACA_KAIROS_KEY", "KAIROS_API_KEY"],
        "secret_vars": ["ALPACA_KAIROS_SECRET", "KAIROS_SECRET_KEY"],
    },
    "trader-stonks": {
        "trader_id": "stonks",
        "agent_id": "trader-stonks",
        "key_vars": ["ALPACA_STONKS_KEY", "STONKS_API_KEY"],
        "secret_vars": ["ALPACA_STONKS_SECRET", "STONKS_SECRET_KEY"],
    },
    "trader-aldridge": {
        "trader_id": "aldridge",
        "agent_id": "trader-aldridge",
        "key_vars": ["ALPACA_ALDRIDGE_KEY", "ALDRIDGE_API_KEY"],
        "secret_vars": ["ALPACA_ALDRIDGE_SECRET", "ALDRIDGE_SECRET_KEY"],
    },
    "kairos": {"trader_id": "kairos", "agent_id": "trader-kairos",
               "key_vars": ["ALPACA_KAIROS_KEY", "KAIROS_API_KEY"],
               "secret_vars": ["ALPACA_KAIROS_SECRET", "KAIROS_SECRET_KEY"]},
    "stonks": {"trader_id": "stonks", "agent_id": "trader-stonks",
               "key_vars": ["ALPACA_STONKS_KEY", "STONKS_API_KEY"],
               "secret_vars": ["ALPACA_STONKS_SECRET", "STONKS_SECRET_KEY"]},
    "aldridge": {"trader_id": "aldridge", "agent_id": "trader-aldridge",
                 "key_vars": ["ALPACA_ALDRIDGE_KEY", "ALDRIDGE_API_KEY"],
                 "secret_vars": ["ALPACA_ALDRIDGE_SECRET", "ALDRIDGE_SECRET_KEY"]},
}


def get_db():
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def get_agent_creds(agent_name: str):
    cfg = AGENT_CONFIG[agent_name]
    api_key = secret_key = None
    for v in cfg["key_vars"]:
        api_key = os.getenv(v)
        if api_key: break
    for v in cfg["secret_vars"]:
        secret_key = os.getenv(v)
        if secret_key: break
    return cfg["agent_id"], cfg["trader_id"], api_key, secret_key


def get_filled_sell_orders(api_key: str, secret_key: str) -> list:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOrdersRequest
    client = TradingClient(api_key, secret_key, paper=True)
    sells = []
    after_ts = None
    all_orders = []
    for _ in range(5):
        params = {"status": "closed", "limit": 500, "direction": "desc"}
        if after_ts:
            params["after"] = after_ts
        try:
            page = client.get_orders(filter=GetOrdersRequest(**params))
            if not page: break
            all_orders.extend(page)
            if len(page) < 500: break
            after_ts = page[-1].submitted_at
        except Exception as e:
            print(f"[sync_exits_pg] WARNING: fetch page failed: {e}", file=sys.stderr)
            break
    for order in all_orders:
        side = order.side.value if hasattr(order.side, 'value') else str(order.side)
        status = order.status.value if hasattr(order.status, 'value') else str(order.status)
        if side.upper() == 'SELL' and status.upper() == 'FILLED':
            fp = float(order.filled_avg_price) if order.filled_avg_price else None
            if fp and fp > 0:
                sells.append({
                    "symbol": order.symbol,
                    "qty": float(order.qty),
                    "filled_price": fp,
                    "filled_qty": float(order.filled_qty) if order.filled_qty else float(order.qty),
                    "filled_at": str(order.filled_at) if order.filled_at else str(order.submitted_at),
                    "order_id": str(order.id),
                })
    return sells


def get_open_buy_trades(agent_id: str) -> list:
    """Get open BUY trades from Postgres trading.trades."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """SELECT id, trader_id, ticker, shares AS quantity, entry_price,
                      entry_time AS entry_timestamp
               FROM trading.trades
               WHERE trader_id = %s AND exit_time IS NULL
               ORDER BY entry_time ASC""",
            (agent_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def close_trade(trade_id: int, exit_price: float, exit_timestamp: str, exit_reason: str):
    """Close a trade in Postgres with P&L calculation."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT trader_id, ticker, entry_price, shares AS quantity FROM trading.trades WHERE id = %s",
            (trade_id,),
        )
        trade = cur.fetchone()
        if not trade:
            print(f"[sync_exits_pg] WARNING: Trade {trade_id} not found in Postgres", file=sys.stderr)
            return False
        entry_price = float(trade["entry_price"])
        quantity = int(trade["quantity"])
        pnl = round((exit_price - entry_price) * quantity, 4)
        pnl_pct = round(((exit_price - entry_price) / entry_price * 100), 4) if entry_price != 0 else 0
        now_utc = datetime.now(timezone.utc)
        cur.execute(
            """UPDATE trading.trades
               SET exit_price = %s, exit_time = %s, pnl = %s, return_pct = %s
               WHERE id = %s AND exit_time IS NULL""",
            (exit_price, exit_timestamp, pnl, pnl_pct, trade_id),
        )
        conn.commit()
        print(f"[sync_exits_pg] Closed trade #{trade_id}: {trade['trader_id']} {trade['ticker']} "
              f"entry=${entry_price:.2f} exit=${exit_price:.2f} pnl=${pnl:.2f} ({pnl_pct:.1f}%)")
        return True
    except Exception as e:
        print(f"[sync_exits_pg] ERROR closing trade #{trade_id}: {e}", file=sys.stderr)
        return False
    finally:
        conn.close()


def _write_learning_entry(agent_id: str, ticker: str, trade: dict,
                          exit_price: float, exit_reason: str):
    entry_price = float(trade.get("entry_price", 0))
    quantity = int(trade.get("quantity", 0))
    pnl = round((exit_price - entry_price) * quantity, 2)
    pnl_pct = round((exit_price - entry_price) / entry_price * 100, 1) if entry_price else 0
    outcome = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN"
    entry = (
        f"[LEARNING] Closed {ticker}: bought at ${entry_price:.2f}, "
        f"sold at ${exit_price:.2f} ({exit_reason}). "
        f"P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%). Outcome: {outcome}."
    )
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO trading.journal (trader_id, timestamp, ticker, decision, rationale, equity, drawdown_pct) "
            "VALUES (%s, NOW(), %s, 'EXIT', %s, 0, 0)",
            (agent_id, ticker, entry),
        )
        conn.commit()
        conn.close()
        print(f"[sync_exits_pg] Learning entry for {agent_id}: {outcome} on {ticker} (${pnl:+.2f})")
    except Exception as e:
        print(f"[sync_exits_pg] WARNING: Failed to write learning entry: {e}", file=sys.stderr)


def update_performance(agent_id: str):
    """Compute win/loss from closed trades and update trading.agent_profile in Postgres."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT pnl FROM trading.trades WHERE trader_id = %s AND pnl IS NOT NULL",
            (agent_id,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        print(f"[sync_exits_pg] No closed trades for {agent_id}, skipping performance update")
        return
    pnls = [float(r["pnl"]) for r in rows]
    total = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    win_rate = round(wins / total, 4) if total > 0 else 0.0
    realized = round(sum(pnls), 2)
    avg_win = round(sum(p for p in pnls if p > 0) / wins, 2) if wins else 0.0
    avg_loss = round(sum(p for p in pnls if p <= 0) / losses, 2) if losses else 0.0
    perf = json.dumps({
        "wins": wins, "losses": losses, "win_rate": win_rate,
        "total_closed_trades": total, "total_realized_pnl": realized,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "updated_at": datetime.now().strftime("%Y-%m-%d"),
    })
    try:
        conn = get_db()
        cur = conn.cursor()
        # Upsert into agent_profile
        cur.execute(
            "INSERT INTO trading.agent_profile (agent_id, performance, updated_at) "
            "VALUES (%s, %s::jsonb, NOW()) "
            "ON CONFLICT (agent_id) DO UPDATE SET performance = %s::jsonb, updated_at = NOW()",
            (agent_id, perf, perf),
        )
        conn.commit()
        conn.close()
        print(f"[sync_exits_pg] Performance for {agent_id}: {wins}W/{losses}L ({win_rate*100:.0f}%), PnL=${realized:.2f}")
    except Exception as e:
        print(f"[sync_exits_pg] WARNING: Failed to update performance: {e}", file=sys.stderr)


def match_and_close(agent_id: str, sells: list):
    open_trades = get_open_buy_trades(agent_id)
    if not open_trades:
        print(f"[sync_exits_pg] No open BUY trades for {agent_id}")
        return
    closed_count = 0
    matched = set()
    for sell in sells:
        ticker = sell["symbol"]
        qty = sell["filled_qty"]
        price = sell["filled_price"]
        filled_at = sell["filled_at"]
        matching = [
            t for t in open_trades
            if t["ticker"].upper() == ticker.upper()
            and abs(t["quantity"] - qty) < 0.001
            and t["id"] not in matched
        ]
        if not matching: continue
        trade = matching[0]
        matched.add(trade["id"])
        exit_reason = "stop_loss"
        if close_trade(trade["id"], price, filled_at, exit_reason):
            update_performance(agent_id)
            _write_learning_entry(agent_id, ticker, trade, price, exit_reason)
            closed_count += 1
    print(f"[sync_exits_pg] {agent_id}: closed {closed_count} trade(s) — {len(open_trades) - closed_count} remain open")


def sync_agent(agent_name: str):
    if agent_name not in AGENT_CONFIG:
        print(f"[sync_exits_pg] ERROR: Unknown agent '{agent_name}'", file=sys.stderr)
        sys.exit(1)
    agent_id, trader_id, api_key, secret_key = get_agent_creds(agent_name)
    if not api_key or not secret_key:
        print(f"[sync_exits_pg] ERROR: Missing credentials for {agent_name}", file=sys.stderr)
        sys.exit(1)
    print(f"[sync_exits_pg] Syncing exits for {agent_id} ({trader_id})...")
    sells = get_filled_sell_orders(api_key, secret_key)
    print(f"[sync_exits_pg] {agent_id}: found {len(sells)} filled SELL orders on Alpaca")
    match_and_close(agent_id, sells)


def backfill_agent(agent_name: str):
    if agent_name not in AGENT_CONFIG:
        print(f"[sync_exits_pg] ERROR: Unknown agent '{agent_name}'", file=sys.stderr)
        sys.exit(1)
    cfg = AGENT_CONFIG[agent_name]
    print(f"[sync_exits_pg] Backfilling performance for {cfg['agent_id']}...")
    update_performance(cfg["agent_id"])
    print(f"[sync_exits_pg] Backfill complete")


def main():
    parser = argparse.ArgumentParser(description="Sync closed Alpaca positions to Postgres")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--agent", help="Agent name: trader-kairos, kairos, trader-stonks, stonks, trader-aldridge, aldridge")
    group.add_argument("--all", action="store_true", help="Sync all three traders")
    group.add_argument("--backfill", help="Backfill performance stats for a specific agent", metavar="AGENT")
    args = parser.parse_args()
    if args.all:
        for name in ["trader-kairos", "trader-stonks", "trader-aldridge"]:
            sync_agent(name)
    elif args.backfill:
        backfill_agent(args.backfill)
    else:
        sync_agent(args.agent)


if __name__ == "__main__":
    main()