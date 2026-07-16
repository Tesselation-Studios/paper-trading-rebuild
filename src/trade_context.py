#!/usr/bin/env python3
"""
trade_context.py — Trade context injector for LLM agents.

Gathers positions, portfolio, market data, and recent trades from Alpaca
(or Postgres fallback), formats everything as clean readable text for the
LLM to read and make decisions from.

Usage:
    # CLI — output formatted context text
    python3 src/trade_context.py --trader kairos

    # CLI — output JSON for programmatic use
    python3 src/trade_context.py --trader kairos --json

    # Import
    from src.trade_context import build_trade_context
    context = build_trade_context("trader-kairos")
    print(context["text"])
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv(Path.home() / ".openclaw" / ".env")
load_dotenv(Path(".env"), override=False)

PG_DSN = os.getenv("PG_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")
STARTING_VALUE = 10_000.0

# Trader credentials mapping
_CRED_MAP = {
    "kairos":   ("KAIROS_API_KEY",   "KAIROS_SECRET_KEY"),
    "aldridge": ("ALDRIDGE_API_KEY", "ALDRIDGE_SECRET_KEY"),
    "stonks":   ("STONKS_API_KEY",   "STONKS_SECRET_KEY"),
}

TRADER_NAMES = {
    "trader-kairos": "Kairós Capital",
    "trader-aldridge": "Aldridge & Partners",
    "trader-stonks": "Stonks Capital",
}

TRADER_INTERVALS = {
    "trader-kairos": 5,
    "trader-aldridge": 30,
    "trader-stonks": 15,
}


# ── Database helpers ───────────────────────────────────────────────────────────

def _get_db():
    """Get a Postgres connection."""
    import psycopg2
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def _get_alpaca_client(agent_id: str):
    """Get Alpaca TradingClient, or None if credentials missing."""
    from alpaca.trading.client import TradingClient
    company = agent_id.replace("trader-", "")
    try:
        api_key_env, secret_env = _CRED_MAP[company]
        api_key = os.getenv(f"ALPACA_{company.upper()}_KEY") or os.getenv(api_key_env)
        secret = os.getenv(f"ALPACA_{company.upper()}_SECRET") or os.getenv(secret_env)
        if not api_key or not secret:
            return None
        return TradingClient(api_key, secret, paper=True)
    except Exception:
        return None


# ── Data gathering functions ──────────────────────────────────────────────────

def get_portfolio(agent_id: str) -> dict:
    """Get portfolio data — Alpaca first, Postgres fallback.

    Returns dict with cash, portfolio_value, buying_power, positions.
    """
    company = agent_id.replace("trader-", "")
    positions = []

    # Try Alpaca first
    client = _get_alpaca_client(agent_id)
    if client:
        try:
            acct = client.get_account()
            result = {
                "cash": float(acct.cash),
                "portfolio_value": float(acct.equity),
                "buying_power": float(acct.buying_power),
                "source": "alpaca_live",
            }
            # Positions
            try:
                for p in client.get_all_positions():
                    pl_pct = float(p.unrealized_plpc) * 100
                    positions.append({
                        "ticker": p.symbol,
                        "qty": float(p.qty),
                        "avg_entry": float(p.avg_entry_price),
                        "current_price": float(p.current_price),
                        "unrealized_pl": float(p.unrealized_pl),
                        "unrealized_plpc": round(pl_pct, 2),
                        "market_value": float(p.market_value),
                    })
            except Exception:
                pass
            result["positions"] = positions
            return result
        except Exception:
            pass

    # Fallback to Postgres
    try:
        conn = _get_db()
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Latest portfolio snapshot
        cur.execute(
            """SELECT cash, portfolio_value, daily_pnl, timestamp
               FROM trading.portfolio_snapshots
               WHERE trader_id = %s
               ORDER BY timestamp DESC LIMIT 1""",
            (agent_id,),
        )
        snap = cur.fetchone()

        # Open positions
        cur.execute(
            """SELECT ticker, quantity, avg_entry_price, current_price,
                      market_value, unrealized_pl, stop_loss, exit_condition
               FROM trading.trader_positions
               WHERE agent_id = %s AND status = 'open'""",
            (agent_id,),
        )
        for r in cur.fetchall():
            pl_pct = 0.0
            if r["avg_entry_price"] and r["avg_entry_price"] > 0:
                pl_pct = round(((r["current_price"] or 0) - r["avg_entry_price"])
                               / r["avg_entry_price"] * 100, 2)
            positions.append({
                "ticker": r["ticker"],
                "qty": r["quantity"],
                "avg_entry": r["avg_entry_price"],
                "current_price": r["current_price"] or 0,
                "unrealized_pl": r["unrealized_pl"] or 0,
                "unrealized_plpc": pl_pct,
                "market_value": r["market_value"] or 0,
                "stop_loss": r["stop_loss"],
                "exit_condition": r["exit_condition"] or "",
            })

        conn.close()

        if snap:
            return {
                "cash": float(snap["cash"]),
                "portfolio_value": float(snap["portfolio_value"]),
                "buying_power": None,
                "daily_pnl": float(snap["daily_pnl"]) if snap["daily_pnl"] else 0.0,
                "positions": positions,
                "source": "pg_snapshot",
                "snapshot_ts": str(snap["timestamp"]),
            }
        elif positions:
            return {
                "cash": 0.0,
                "portfolio_value": 0.0,
                "buying_power": None,
                "positions": positions,
                "source": "pg_positions_only",
            }
    except Exception:
        pass

    return {
        "cash": 0.0,
        "portfolio_value": 0.0,
        "buying_power": None,
        "positions": [],
        "source": "unavailable",
    }


def get_recent_trades(agent_id: str, limit: int = 10) -> list[dict]:
    """Get recent trades from executed_trades."""
    try:
        conn = _get_db()
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT ticker, action, quantity, entry_price, exit_price,
                      pnl, entry_time, exit_time, status
               FROM trading.executed_trades
               WHERE agent_id = %s
               ORDER BY entry_time DESC LIMIT %s""",
            (agent_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def get_recent_decisions(agent_id: str, limit: int = 5) -> list[dict]:
    """Get recent trading decisions."""
    try:
        conn = _get_db()
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT trader_id, ticker, decision, conviction, rationale, timestamp
               FROM trading.decisions
               WHERE trader_id = %s
               ORDER BY timestamp DESC LIMIT %s""",
            (agent_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def get_market_data(tickers: list[str]) -> dict:
    """Get market data (quotes, signals) for a list of tickers.

    Tries data bus first, then falls back to Postgres.
    """
    result = {"quotes": {}, "signals": {}}

    # Try data bus
    try:
        url = "http://localhost:5000/tick-snapshot"
        resp = urllib.request.urlopen(url, timeout=5)
        snapshot = json.loads(resp.read().decode())
        quotes = snapshot.get("quotes", {})
        for t in tickers:
            if t in quotes:
                result["quotes"][t] = quotes[t]
        # Fear & greed
        fg = snapshot.get("fear_greed", {})
        if fg:
            result["fear_greed"] = fg
        # Regime
        regime = snapshot.get("regime", {})
        if regime:
            result["regime"] = regime
    except Exception:
        pass

    # Try Postgres for signals
    try:
        conn = _get_db()
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        for t in tickers:
            cur.execute(
                """SELECT composite_signal, conviction, regime
                   FROM trading.signals
                   WHERE ticker = %s
                   ORDER BY timestamp DESC LIMIT 1""",
                (t,),
            )
            row = cur.fetchone()
            if row:
                result["signals"][t] = {
                    "signal": row["composite_signal"],
                    "confidence": float(row["conviction"]) if row["conviction"] else None,
                    "regime": row["regime"],
                }
        conn.close()
    except Exception:
        pass

    return result


def get_performance_stats(agent_id: str) -> dict:
    """Get performance statistics from trades table."""
    try:
        conn = _get_db()
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT pnl FROM trading.trades
               WHERE trader_id = %s AND pnl IS NOT NULL""",
            (agent_id,),
        )
        pnls = [float(r["pnl"]) for r in cur.fetchall()]
        conn.close()
        if not pnls:
            return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "realized_pnl": 0}
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p <= 0)
        return {
            "total_trades": len(pnls),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(pnls), 4) if pnls else 0,
            "realized_pnl": round(sum(pnls), 2),
        }
    except Exception:
        return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "realized_pnl": 0}


# ── Formatting ─────────────────────────────────────────────────────────────────

def format_portfolio_text(portfolio: dict) -> str:
    """Format portfolio data as readable text."""
    lines = []
    pv = portfolio.get("portfolio_value", 0)
    cash = portfolio.get("cash", 0)
    bp = portfolio.get("buying_power")
    source = portfolio.get("source", "unknown")
    positions = portfolio.get("positions", [])

    pct = round((pv - STARTING_VALUE) / STARTING_VALUE * 100, 2) if pv else 0

    lines.append(f"Portfolio Value: ${pv:,.2f} ({pct:+.2f}% from ${STARTING_VALUE:,.0f})")
    lines.append(f"Cash: ${cash:,.2f}")
    if bp is not None:
        lines.append(f"Buying Power: ${bp:,.2f}")
    lines.append(f"Source: {source}")
    lines.append(f"Open Positions: {len(positions)}")

    if positions:
        lines.append("")
        lines.append(f"{'Ticker':<8} {'Qty':<8} {'Entry':<10} {'Current':<10} {'Value':<12} {'uPNL':<12} {'uPNL%':<8}")
        lines.append("-" * 70)
        for p in positions:
            ticker = p.get("ticker", "?")
            qty = f"{p.get('qty', 0):.0f}"
            entry = f"${p.get('avg_entry', 0):.2f}"
            cur = f"${p.get('current_price', 0):.2f}"
            mkt = f"${p.get('market_value', 0):,.2f}"
            upl = f"${p.get('unrealized_pl', 0):+.2f}"
            upl_pct = f"{p.get('unrealized_plpc', 0):+.2f}%"
            lines.append(f"{ticker:<8} {qty:<8} {entry:<10} {cur:<10} {mkt:<12} {upl:<12} {upl_pct:<8}")

    return "\n".join(lines)


def format_trades_text(trades: list[dict]) -> str:
    """Format recent trades as readable text."""
    if not trades:
        return "No recent trades."

    lines = [
        "",
        "Recent Trades:",
        f"{'Ticker':<8} {'Action':<8} {'Qty':<8} {'Entry':<10} {'Exit':<10} {'PnL':<12} {'Date':<20}",
        "-" * 76,
    ]
    for t in trades:
        ticker = t.get("ticker", "?")
        action = t.get("action", "?")
        qty = str(t.get("quantity", 0))
        entry = f"${float(t.get('entry_price', 0)):.2f}" if t.get("entry_price") else "—"
        exit_p = f"${float(t.get('exit_price', 0)):.2f}" if t.get("exit_price") else "—"
        pnl = t.get("pnl")
        pnl_str = f"${float(pnl):+.2f}" if pnl is not None else "—"
        date = str(t.get("entry_time", ""))[:19] if t.get("entry_time") else ""
        lines.append(f"{ticker:<8} {action:<8} {qty:<8} {entry:<10} {exit_p:<10} {pnl_str:<12} {date:<20}")

    return "\n".join(lines)


def format_decisions_text(decisions: list[dict]) -> str:
    """Format recent decisions as readable text."""
    if not decisions:
        return "No recent decisions."

    lines = ["", "Recent Decisions:"]
    for d in decisions:
        ticker = d.get("ticker", "?")
        action = d.get("decision", "?")
        confidence = d.get("conviction")
        conf_str = f" (conf: {confidence:.1%})" if confidence else ""
        thesis = d.get("rationale", "")
        ts = str(d.get("timestamp", ""))[:19]
        lines.append(f"  [{ts}] {action} {ticker}{conf_str}: {thesis[:100]}")

    return "\n".join(lines)


def format_market_text(market: dict) -> str:
    """Format market data as readable text."""
    lines = []

    # Fear & Greed
    fg = market.get("fear_greed", {})
    if fg:
        fg_val = fg.get("value", "?")
        fg_class = fg.get("classification", "?")
        lines.append(f"Fear & Greed Index: {fg_val} ({fg_class})")

    # Regime
    regime = market.get("regime", {})
    if regime:
        label = regime.get("regime", regime.get("label", "?"))
        conf = regime.get("confidence", "?")
        lines.append(f"Market Regime: {label} (confidence: {conf})")

    # Quotes
    quotes = market.get("quotes", {})
    if quotes:
        lines.append("")
        lines.append(f"{'Ticker':<8} {'Price':<10} {'Change':<10} {'RSI':<8}")
        lines.append("-" * 36)
        for sym, q in sorted(quotes.items()):
            if not q or not q.get("close"):
                continue
            close = q.get("close", 0)
            prev_close = q.get("prev_close") or q.get("open") or close
            change = ((close - prev_close) / prev_close * 100) if prev_close and prev_close != 0 else 0
            rsi = f"{q.get('rsi', '—'):.0f}" if isinstance(q.get('rsi'), (int, float)) else "—"
            lines.append(f"{sym:<8} ${close:<7.2f} {change:<+9.1f}% {rsi:<8}")

    # Signals
    signals = market.get("signals", {})
    if signals:
        lines.append("")
        lines.append("ML Signals for Held Positions:")
        lines.append(f"{'Ticker':<8} {'Signal':<12} {'Confidence':<12} {'Regime':<12}")
        lines.append("-" * 44)
        for sym, sig in sorted(signals.items()):
            s = sig.get("signal", "—")
            c = f"{sig.get('confidence', 0):.0%}" if sig.get("confidence") else "—"
            r = sig.get("regime", "—")
            lines.append(f"{sym:<8} {s:<12} {c:<12} {r:<12}")

    return "\n".join(lines)


def format_performance_text(stats: dict) -> str:
    """Format performance stats as readable text."""
    if not stats or stats.get("total_trades", 0) == 0:
        return "No trade history yet."

    lines = [""]
    lines.append(f"Performance: {stats['wins']}W / {stats['losses']}L "
                 f"({stats['win_rate']*100:.1f}% win rate)")
    lines.append(f"Realized P&L: ${stats['realized_pnl']:+,.2f}")
    lines.append(f"Total Closed Trades: {stats['total_trades']}")
    return "\n".join(lines)


# ── Main builder ──────────────────────────────────────────────────────────────

def build_trade_context(agent_id: str, include_signals: bool = True) -> dict:
    """Build the complete trade context for an LLM agent.

    Args:
        agent_id: e.g. "trader-kairos", "trader-stonks", "trader-aldridge"
        include_signals: Whether to fetch and include ML signal data

    Returns:
        dict with:
            text: Formatted readable text for the LLM
            data: Raw structured data (for programmatic use)
            agent_id: The agent identifier
            timestamp: ISO timestamp of when context was built
    """
    company = agent_id.replace("trader-", "")
    trader_name = TRADER_NAMES.get(agent_id, company.title())

    # Gather data
    portfolio = get_portfolio(agent_id)
    positions = portfolio.get("positions", [])
    position_tickers = [p["ticker"] for p in positions]

    market = get_market_data(position_tickers) if include_signals else {"quotes": {}, "signals": {}}
    recent_trades = get_recent_trades(agent_id, limit=10)
    recent_decisions = get_recent_decisions(agent_id, limit=5)
    perf_stats = get_performance_stats(agent_id)

    # Format as text
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S ET")
    text_parts = [
        f"=== TRADE CONTEXT for {trader_name} ({agent_id}) ===",
        f"Generated: {now}",
        f"Trading Mode: LIVE (paper trading)",
        "",
        "--- PORTFOLIO ---",
        format_portfolio_text(portfolio),
    ]

    text_parts.append("--- MARKET DATA ---")
    text_parts.append(format_market_text(market))

    text_parts.append("--- PERFORMANCE ---")
    text_parts.append(format_performance_text(perf_stats))

    text_parts.append("--- RECENT TRADES ---")
    text_parts.append(format_trades_text(recent_trades))

    text_parts.append("--- RECENT DECISIONS ---")
    text_parts.append(format_decisions_text(recent_decisions))

    if position_tickers:
        text_parts.append("")
        text_parts.append(f"--- WATCHLIST ({len(position_tickers)} tickers) ---")
        text_parts.append(", ".join(position_tickers))

    return {
        "text": "\n".join(text_parts),
        "data": {
            "portfolio": portfolio,
            "market": market,
            "recent_trades": recent_trades,
            "recent_decisions": recent_decisions,
            "performance": perf_stats,
        },
        "agent_id": agent_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Trade context injector — gather portfolio, market, and performance data"
    )
    parser.add_argument("--trader", required=True,
                        choices=["trader-kairos", "trader-aldridge", "trader-stonks",
                                 "kairos", "aldridge", "stonks"],
                        help="Trader agent ID")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON instead of formatted text")
    parser.add_argument("--no-signals", action="store_true",
                        help="Skip ML signal data")
    args = parser.parse_args()

    # Normalize agent ID
    agent_id = args.trader if args.trader.startswith("trader-") else f"trader-{args.trader}"

    context = build_trade_context(agent_id, include_signals=not args.no_signals)

    if args.json:
        print(json.dumps(context, indent=2, default=str))
    else:
        print(context["text"])


if __name__ == "__main__":
    main()