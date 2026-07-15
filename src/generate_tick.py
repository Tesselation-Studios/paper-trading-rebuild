#!/usr/bin/env python3
"""
generate_tick — Generate daily_tick.md for each virtual trader.

Reads market state from data bus and portfolio snapshot from Postgres (or
falls back to shared/trader.db SQLite), then writes a daily briefing markdown
file to each trader's prompt directory in ~/projects/trading-agent-prompts/.

Usage:
    python3 src/generate_tick.py --all
    python3 src/generate_tick.py --trader kairos
    python3 src/generate_tick.py --trader stonks --symbols AAPL,MSFT,SPY

Configuration:
    DATA_BUS_URL  Data bus URL (default: http://localhost:5000)
    PROMPTS_DIR   Path to trading-agent-prompts (default: ~/projects/trading-agent-prompts)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [tick] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("generate_tick")

# ── Defaults ─────────────────────────────────────────────────────────────────
DATA_BUS_URL = os.getenv("DATA_BUS_URL", "http://localhost:5000")
PROMPTS_DIR = Path(
    os.getenv("PROMPTS_DIR", os.path.expanduser("~/projects/trading-agent-prompts"))
)
SRC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SRC_DIR.parent

# ── Trader config: symbols, risk params ──────────────────────────────────────
TRADER_CONFIG: Dict[str, Dict[str, Any]] = {
    "kairos": {
        "symbols": ["AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "QQQ", "SPY", "TSLA"],
        "max_position_pct": 20.0,
        "stop_loss_pct": 7.0,
        "max_concurrent": 3,
        "label": "Kairos (Momentum + Signal)",
    },
    "stonks": {
        "symbols": ["AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "QQQ", "SPY", "TSLA"],
        "max_position_pct": 25.0,
        "stop_loss_pct": 8.0,
        "max_concurrent": 3,
        "label": "Stonks (Community Sentiment + Technicals)",
    },
    "aldridge": {
        "symbols": ["AAPL", "MSFT", "JPM", "BAC", "GS", "V", "MA", "WMT", "PG", "KO"],
        "max_position_pct": 25.0,
        "stop_loss_pct": 8.0,
        "max_concurrent": 5,
        "label": "Aldridge (Value + Fundamental)",
    },
}


def fetch_json(endpoint: str, params: dict | None = None,
               timeout: int = 15) -> Optional[dict]:
    """Fetch JSON from the data bus."""
    if requests is None:
        log.error("requests module not available")
        return None
    try:
        url = f"{DATA_BUS_URL}{endpoint}"
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        log.warning("Data bus %s request failed: %s", endpoint, e)
        return None


def get_market_context() -> str:
    """Build a market context summary from the data bus."""
    lines = []

    # Fear & Greed
    fg = fetch_json("/fear_greed")
    if fg and "fear_greed" in fg:
        v = fg["fear_greed"].get("value", "?")
        c = fg["fear_greed"].get("classification", "?")
        lines.append(f"- **Fear & Greed**: {c} ({v})")

    # Macro indicators
    macro = fetch_json("/macro")
    if macro and "macro" in macro:
        indicators = macro["macro"].get("indicators", {})
        for key in ("GDP", "CPI", "DGS10", "DGS2", "unemployment", "PCE", "NFP"):
            if key in indicators:
                ind = indicators[key]
                val = ind.get("value", "?")
                date = ind.get("date", "")
                lines.append(f"- **{key}**: {val} (as of {date})")
        # Yield curve
        yields = macro["macro"].get("yields", {})
        if yields:
            spread = yields.get("spread_10y2y")
            status = yields.get("curve_status", "")
            lines.append(f"- **Yield Curve**: {spread}")
            lines.append(f"- **Curve Status**: {status}")

    # Regime from momentum
    momentum = fetch_json("/momentum")
    if momentum:
        regime = momentum.get("market_regime", "")
        if regime:
            lines.append(f"- **Market Regime**: {regime}")
        avg_z = momentum.get("avg_composite_z")
        if avg_z is not None:
            lines.append(f"- **Momentum Z-score**: {avg_z:.3f}")

    # Tracked symbols count
    health = fetch_json("/health")
    if health:
        tracked = health.get("tracked_symbols", 0)
        uptime = health.get("uptime_seconds", 0)
        lines.append(f"- **Data Bus**: {tracked} symbols tracked, "
                     f"uptime {uptime // 3600}h")

    return "\n".join(lines) if lines else "Market data unavailable"


def get_portfolio_snapshot(trader: str) -> Dict[str, Any]:
    """Get portfolio state for a trader.

    Tries Postgres first, then SQLite fallback.
    """
    snapshot = {
        "cash": 10000.00,
        "open_positions": 0,
        "unrealized_pnl": 0.0,
        "day_pnl": 0.0,
        "portfolio_value": 10000.00,
        "positions": [],
    }

    # Try Postgres
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "trading-db"),
            port=int(os.getenv("DB_PORT", "5433")),
            dbname=os.getenv("DB_NAME", "trading"),
            user=os.getenv("DB_USER", "trader"),
            password=os.getenv("DB_PASSWORD", "trader"),
            connect_timeout=3,
        )
        cur = conn.cursor()

        # Get agent state
        cur.execute(
            "SELECT current_portfolio_value, unrealized_pnl, ytd_pnl "
            "FROM agent_state WHERE agent_id = %s ORDER BY fetched_at DESC LIMIT 1",
            (trader,),
        )
        row = cur.fetchone()
        if row:
            snapshot["portfolio_value"] = float(row[0] or 10000.0)
            snapshot["unrealized_pnl"] = float(row[1] or 0.0)
            snapshot["day_pnl"] = float(row[2] or 0.0)

        # Get latest portfolio_snapshot
        cur.execute(
            "SELECT cash, portfolio_value, daily_pnl, open_positions "
            "FROM portfolio_snapshot WHERE trader_name = %s "
            "ORDER BY fetched_at DESC LIMIT 1",
            (trader,),
        )
        row = cur.fetchone()
        if row:
            snapshot["cash"] = float(row[0] or snapshot["cash"])
            snapshot["portfolio_value"] = float(row[1] or snapshot["portfolio_value"])
            snapshot["day_pnl"] = float(row[2] or snapshot["day_pnl"])
            snapshot["open_positions"] = int(row[3] or 0)

        # Get open positions
        cur.execute(
            "SELECT ticker, quantity, entry_price, current_price, pnl "
            "FROM positions WHERE trader_name = %s AND status = 'open'",
            (trader,),
        )
        positions = cur.fetchall()
        if positions:
            snapshot["positions"] = [
                {"ticker": p[0], "qty": float(p[1]), "entry": float(p[2]),
                 "current": float(p[3]), "pnl": float(p[4])}
                for p in positions
            ]
            snapshot["open_positions"] = len(positions)

        conn.close()
    except Exception as e:
        log.warning("Postgres unavailable for %s: %s — using defaults", trader, e)

    return snapshot


def format_position_row(pos: Dict[str, Any]) -> str:
    """Format a single position for the tick."""
    ticker = pos.get("ticker", "?")
    qty = pos.get("qty", 0)
    entry = pos.get("entry", 0)
    current = pos.get("current", entry)
    pnl = pos.get("pnl", 0)
    pnl_sym = "🟢" if pnl >= 0 else "🔴"
    return f"  | {ticker} | {qty:.0f} | ${entry:.2f} | ${current:.2f} | {pnl_sym} ${pnl:.2f} |"


def get_quote_summary(symbols: List[str]) -> str:
    """Get quote summary for watchlist."""
    if not symbols:
        return "No symbols configured"
    data = fetch_json("/quotes", {"symbols": ",".join(symbols)})
    if not data or "quotes" not in data:
        return "Quotes unavailable"

    quotes = data["quotes"]
    lines = []
    lines.append("| Ticker | Close | Change | Volume | Source |")
    lines.append("|--------|-------|--------|--------|--------|")

    for sym in symbols:
        q = quotes.get(sym)
        if q is None:
            lines.append(f"| {sym} | N/A | N/A | N/A | N/A |")
            continue
        close = q.get("close", "?")
        change = q.get("change_pct", q.get("change", ""))
        vol = q.get("volume", "?")
        source = q.get("source", "?")

        # Format change
        if isinstance(change, (int, float)):
            change_str = f"{change:+.2f}%"
        elif change:
            change_str = str(change)
        else:
            change_str = "N/A"

        lines.append(f"  | {sym} | ${close} | {change_str} | {vol:,} | {source} |")

    return "\n".join(lines)


def generate_tick(trader: str, date_str: str) -> str:
    """Generate the daily_tick.md content for a trader."""
    cfg = TRADER_CONFIG.get(trader)
    if not cfg:
        log.error("Unknown trader: %s", trader)
        return ""

    log.info("Generating tick for %s on %s", trader, date_str)

    # Gather context
    market_context = get_market_context() or "Market data unavailable"
    portfolio = get_portfolio_snapshot(trader)
    watchlist = get_quote_summary(cfg["symbols"])

    # Format portfolio
    positions_str = ""
    if portfolio["positions"]:
        pos_lines = [format_position_row(p) for p in portfolio["positions"]]
        positions_str = "\n".join(pos_lines)

    # Sentiment
    sentiment_str = ""
    # Pick a key symbol for sentiment
    sentiment_sym = cfg["symbols"][0] if cfg["symbols"] else "SPY"
    sent_data = fetch_json("/sentiment", {"symbol": sentiment_sym})
    if sent_data and "sentiment" in sent_data:
        s = sent_data["sentiment"]
        compound = s.get("compound", 0)
        pos = s.get("positive", 0)
        neg = s.get("negative", 0)
        neu = s.get("neutral", 0)
        sent_sym = "🟢" if compound > 0.1 else "🔴" if compound < -0.1 else "🟡"
        sentiment_str = (
            f"{sent_sym} **{sentiment_sym} Sentiment**: "
            f"compound={compound:.3f} | "
            f"pos={pos:.2%} neg={neg:.2%} neu={neu:.2%}"
        )

    # Build the tick
    tick = f"""# Today's Trading — {date_str}

## Trader
**{cfg['label']}** ({trader})

## Market Context
{market_context}

## Portfolio Snapshot
| Metric | Value |
|--------|-------|
| Cash | ${portfolio['cash']:,.2f} |
| Open Positions | {portfolio['open_positions']} |
| Unrealized P&L | ${portfolio['unrealized_pnl']:+,.2f} |
| Day P&L | ${portfolio['day_pnl']:+,.2f} |
| Portfolio Value | ${portfolio['portfolio_value']:,.2f} |
"""

    if positions_str:
        tick += f"""
## Open Positions
| Ticker | Shares | Entry | Current | P&L |
|--------|--------|-------|---------|-----|
{positions_str}
"""

    if sentiment_str:
        tick += f"""
## Sentiment
{sentiment_str}
"""

    tick += f"""
## Watchlist
{watchlist}

## Risk Parameters
- Max position: {cfg['max_position_pct']:.0f}%
- Stop loss: {cfg['stop_loss_pct']:.0f}%
- Max concurrent positions: {cfg['max_concurrent']}

## Instructions
1. Review market context and portfolio snapshot
2. Check sentiment and watchlist data
3. Evaluate open positions or find new entries based on signals
4. Consider risk parameters before making decisions
5. Decide BUY/SELL/HOLD for each relevant position
"""

    return tick


def write_tick(trader: str, content: str):
    """Write daily_tick.md to trader prompt directory."""
    trader_dir = PROMPTS_DIR / trader
    trader_dir.mkdir(parents=True, exist_ok=True)
    tick_path = trader_dir / "daily_tick.md"

    tick_path.write_text(content)
    log.info("Written %s (%d bytes)", tick_path, len(content))


def main():
    parser = argparse.ArgumentParser(description="Generate daily tick for traders")
    parser.add_argument("--all", action="store_true", help="Generate for all traders")
    parser.add_argument("--trader", type=str, help="Specific trader (kairos/stonks/aldridge)")
    parser.add_argument("--date", type=str, default="",
                        help="Override date string (default: today)")
    parser.add_argument("--symbols", type=str, default="",
                        help="Override symbols (comma-separated)")
    parser.add_argument("--print", action="store_true",
                        help="Print to stdout instead of writing files")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be written without writing")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    traders: List[str] = []
    if args.all:
        traders = list(TRADER_CONFIG.keys())
    elif args.trader:
        traders = [args.trader]
    else:
        log.info("No trader specified. Use --all or --trader")
        parser.print_help()
        return 1

    # Override symbols if specified
    if args.symbols:
        sym_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        for t in traders:
            if t in TRADER_CONFIG:
                TRADER_CONFIG[t]["symbols"] = sym_list

    for trader in traders:
        if trader not in TRADER_CONFIG:
            log.error("Unknown trader: %s (known: %s)", trader, list(TRADER_CONFIG.keys()))
            continue

        content = generate_tick(trader, date_str)
        if not content:
            log.error("Failed to generate tick for %s", trader)
            continue

        if args.print:
            print(f"\n{'='*60}")
            print(f"  {trader.upper()} — {date_str}")
            print(f"{'='*60}")
            print(content)
        elif args.dry_run:
            trader_dir = PROMPTS_DIR / trader
            tick_path = trader_dir / "daily_tick.md"
            log.info("[DRY RUN] Would write %s (%d bytes)", tick_path, len(content))
        else:
            write_tick(trader, content)

    log.info("Done — generated ticks for %d trader(s): %s", len(traders), traders)
    return 0


if __name__ == "__main__":
    sys.exit(main())
