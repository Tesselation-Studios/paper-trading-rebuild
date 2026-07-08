#!/usr/bin/env python3
"""
tick_prompt.py — Pre-assemble a complete trading prompt for one trader tick.

Reads the prompt template, hits the data bus for live state, templates
everything into a single prompt, and outputs it to stdout for the cron
to use as the agentTurn message.

Usage:
    python3 scripts/tick_prompt.py --trader kairos
    python3 scripts/tick_prompt.py --trader stonks --db-path shared/trader.db

Architecture:
    Cron fires → tick_prompt.py runs → outputs complete prompt → cron sends
    as agentTurn message → LLM receives fully-loaded context → outputs JSON.
    The LLM never reads files or queries APIs during a trading tick.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────

DATA_BUS_URL = os.getenv("DATA_BUS_URL", "http://localhost:5000")
REPO_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_DIR / "prompts"

# Default stock universe per trader (kept in sync with prompts/*.txt)
STOCK_UNIVERSES = {
    "kairos": ["KO", "F", "INTC", "PFE", "WBD", "VZ", "CSCO", "HPQ", "KHC", "WBA"],
    "stonks": ["KO", "F", "INTC", "PFE", "WBD", "VZ", "CSCO", "HPQ", "KHC", "WBA"],
    "aldridge": ["KO", "F", "INTC", "PFE", "WBD", "VZ", "CSCO", "HPQ", "KHC", "WBA"],
}

# ── Data Bus Fetch ───────────────────────────────────────────────────────────

def fetch_tick_snapshot() -> dict:
    """Hit /tick-snapshot once — returns quotes, regime, F&G, portfolio, signals."""
    url = f"{DATA_BUS_URL}/tick-snapshot"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"WARNING: data bus fetch failed: {e}", file=sys.stderr)
        return {}


def fetch_quotes_live(symbols: list[str]) -> dict:
    """Fetch live quotes for symbols that may not be tracked."""
    url = f"{DATA_BUS_URL}/quotes?symbols={",".join(symbols)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("quotes", data)
    except Exception as e:
        print(f"WARNING: live quotes fetch failed: {e}", file=sys.stderr)
        return {}
    

def fetch_technical_scan(symbol: str) -> dict:
    """Get multi-timeframe technical scan for one symbol."""
    url = f"{DATA_BUS_URL}/technical_scan?symbol={symbol}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def fetch_fear_greed() -> dict:
    """Fallback: fetch F&G directly if snapshot misses it."""
    url = f"{DATA_BUS_URL}/fear_greed"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return data.get("fear_greed", data)
    except Exception:
        return {}


def fetch_sentiment(symbol: str) -> dict:
    """Get FinBERT sentiment for one symbol."""
    url = f"{DATA_BUS_URL}/sentiment?symbol={symbol}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


# ── Journal ──────────────────────────────────────────────────────────────────

def get_journal_entries(db_path: str, trader_id: str, n: int = 5) -> list[str]:
    """Pull the last N journal entries for a trader."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT entry as content, timestamp as created_at FROM trader_journal "
            "WHERE agent_id = ? ORDER BY timestamp DESC LIMIT ?",
            (f"trader-{trader_id}", n),
        ).fetchall()
        conn.close()
        return [dict(r) for r in reversed(rows)]
    except Exception as e:
        print(f"WARNING: journal fetch failed: {e}", file=sys.stderr)
        return []


# ── Prompt Assembly ──────────────────────────────────────────────────────────

def build_quotes_table(snapshot: dict, symbols: list[str]) -> str:
    """Build a compact quotes table for the prompt."""
    quotes = snapshot.get("quotes", {})
    lines = ["| Ticker | Price | Change% | RSI | MACD | Volume vs Avg |"]
    lines.append("|--------|-------|---------|-----|------|---------------|")

    for sym in symbols:
        q = quotes.get(sym, {})
        if not q or not q.get("close"):
            lines.append(f"| {sym} | N/A | — | — | — | — |")
            continue

        close = q.get("close", 0)
        prev_close = q.get("prev_close") or q.get("open") or close
        change = ((close - prev_close) / prev_close * 100) if prev_close and prev_close != 0 else 0
        rsi = q.get("rsi", "—")
        macd = q.get("macd_line", "—")
        vol_ratio = q.get("vol_ratio", "—")

        rsi_str = f"{rsi:.0f}" if isinstance(rsi, (int, float)) else str(rsi)
        macd_str = f"{macd:+.3f}" if isinstance(macd, (int, float)) else str(macd)
        vol_str = f"{vol_ratio:.1f}x" if isinstance(vol_ratio, (int, float)) else str(vol_ratio)

        lines.append(
            f"| {sym} | ${close:.2f} | {change:+.1f}% | {rsi_str} | {macd_str} | {vol_str} |"
        )

    return "\n".join(lines)


def build_market_context(snapshot: dict) -> str:
    """Build market context section."""
    parts = []

    # Fear & Greed
    # Fear & Greed — snapshot may miss it, use fallback
    fg = snapshot.get("fear_greed") or fetch_fear_greed()
    if fg:
        fg_val = fg.get("value", "?")
        fg_class = fg.get("classification", "?")
        parts.append(f"Fear & Greed: {fg_val} ({fg_class})")

    # Regime
    regime = snapshot.get("regime", {})
    if regime and "error" not in regime:
        regime_label = regime.get("regime", regime.get("label", "?"))
        regime_conf = regime.get("confidence", "?")
        parts.append(f"Market Regime: {regime_label} (confidence: {regime_conf})")

    # VIX
    quotes = snapshot.get("quotes", {})
    vix = quotes.get("VIX", {})
    if vix and vix.get("close"):
        parts.append(f"VIX: {vix['close']:.2f}")

    return "\n".join(f"- {p}" for p in parts) if parts else "Market context unavailable"


def build_portfolio_section(snapshot: dict, trader_id: str) -> str:
    """Build portfolio summary section."""
    pf = snapshot.get("portfolio_state", {}).get(f"trader-{trader_id}", {})
    if not pf or "error" in pf:
        return "Portfolio: data unavailable"

    summary = pf.get("summary", "")
    positions = pf.get("open_positions", [])

    lines = [summary, ""]
    if positions:
        lines.append("Current Positions:")
        for p in positions:
            lines.append(
                f"  {p['ticker']}: {p['shares']} shares @ ${p['current']:.2f} "
                f"(entry ${p['entry']:.2f}, uPNL ${p['uPNL']:+.2f})"
            )
    else:
        lines.append("No open positions.")

    return "\n".join(lines)


def build_performance_section(snapshot: dict, trader_id: str) -> str:
    """Build performance brief section."""
    perf = snapshot.get("performance_brief", {})
    if not perf or f"trader-{trader_id}" not in perf:
        return ""
    return perf[f"trader-{trader_id}"].get("brief_markdown", "")


def build_signals_board(snapshot: dict) -> str:
    """Build inter-trader signals board."""
    signals = snapshot.get("signals", [])
    if not signals:
        return "No signals from other traders this tick."

    lines = []
    for s in signals[-5:]:  # last 5 signals
        trader = s.get("agent_id", s.get("trader", "?"))
        ticker = s.get("ticker", "?")
        action = s.get("action", s.get("decision", "?"))
        thesis = s.get("thesis", s.get("note", ""))
        lines.append(f"- **{trader}** {action} {ticker}: {thesis[:120]}")

    return "\n".join(lines) if lines else "No signals from other traders this tick."


def check_circuit_breaker(trader_id: str) -> dict | None:
    """Check if the trader's circuit breaker is tripped.

    Returns None if trading is allowed, or a dict with skip info if paused.
    Resilient: if the circuit breaker module or DB is unavailable, logs a warning
    and allows the tick through (fail-open).
    """
    agent_id = f"trader-{trader_id}"
    try:
        from src.circuit_breaker import get_breaker
        breaker = get_breaker(agent_id)
        paused, reason = breaker.check_paused()
        if paused:
            status = breaker.status()
            return {
                "agent": agent_id,
                "paused": True,
                "reason": reason or "Circuit breaker tripped",
                "total_trips": status.get("total_trips", 0),
                "last_trip_at": status.get("last_trip_at"),
            }
        return None
    except Exception as e:
        print(f"WARNING: circuit breaker check failed (fail-open): {e}", file=sys.stderr)
        return None


def assemble_prompt(trader_id: str, db_path: str | None = None) -> str:
    """Assemble the complete trading prompt for one tick."""
    # 0. Circuit breaker guard — skip if paused
    skip_info = check_circuit_breaker(trader_id)
    if skip_info:
        return json.dumps({
            "tick_skipped": True,
            "agent": skip_info["agent"],
            "reason": skip_info["reason"],
            "total_trips": skip_info["total_trips"],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S ET"),
        })

    # 1. Read the prompt template
    prompt_path = PROMPTS_DIR / f"{trader_id}.txt"
    if not prompt_path.exists():
        print(f"FATAL: prompt template not found: {prompt_path}", file=sys.stderr)
        sys.exit(1)
    template = prompt_path.read_text()

    # 2. Fetch live state from data bus
    snapshot = fetch_tick_snapshot()
    symbols = STOCK_UNIVERSES.get(trader_id, [])

    # Live fetch for untracked symbols (data bus only tracks 17 legacy symbols)
    live_quotes = fetch_quotes_live(symbols)
    if live_quotes:
        snapshot["quotes"] = {**snapshot.get("quotes", {}), **live_quotes}

    # 3. Build injected sections
    market_context = build_market_context(snapshot)
    quotes_table = build_quotes_table(snapshot, symbols)
    portfolio = build_portfolio_section(snapshot, trader_id)
    performance = build_performance_section(snapshot, trader_id)
    signals_board = build_signals_board(snapshot)

    # 4. Journal entries
    if db_path:
        journal = get_journal_entries(db_path, trader_id)
        journal_text = "\n\n".join(
            f"[{j['created_at']}] {j['content']}" for j in journal
        ) if journal else "No recent journal entries."
    else:
        journal_text = "Journal unavailable."

    # 5. Assemble the full prompt
    injected = f"""
## LIVE TRADING TICK — {time.strftime('%Y-%m-%d %H:%M:%S ET')}

### Market Context
{market_context}

### Watchlist Quotes
{quotes_table}

### Your Portfolio
{portfolio}

### Performance
{performance if performance else 'Performance data unavailable.'}

### Other Traders' Signals
{signals_board}

### Your Recent Journal
{journal_text}

---
## YOUR PROMPT (from prompts/{trader_id}.txt)
{template}

---
## TRADING TICK INSTRUCTIONS

1. Read the market context and your watchlist quotes above.
2. Read YOUR PROMPT for your strategy, persona, and rules.
3. Make ONE trading decision: BUY, SELL, or HOLD.
4. Use `python3 src/skill_alpaca.py --account {trader_id}` to execute.
5. Journal your reasoning.

REMEMBER: thesis MUST be 20+ chars, signals_used MUST have at least 1 entry,
confidence ≥ 0.3. A HOLD with idle cash is a missed learning opportunity.
""".strip()

    return injected


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pre-assemble a trading tick prompt for one trader."
    )
    parser.add_argument(
        "--trader", required=True,
        choices=["kairos", "stonks", "aldridge"],
        help="Trader ID"
    )
    parser.add_argument(
        "--db-path",
        default=os.path.join(os.path.dirname(__file__), "..", "..",
                             "paper-trading-teams", "shared", "trader.db"),
        help="Path to SQLite trader database (for journal entries)"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON (for programmatic use)"
    )
    args = parser.parse_args()

    db_path = args.db_path if os.path.exists(args.db_path) else None
    if not db_path:
        print(f"WARNING: DB not found at {args.db_path}, journal unavailable", file=sys.stderr)

    prompt = assemble_prompt(args.trader, db_path)

    # Check if tick was skipped by circuit breaker
    try:
        parsed = json.loads(prompt)
        if isinstance(parsed, dict) and parsed.get("tick_skipped"):
            # Tick skipped — output skip info and exit clean
            if args.json:
                print(json.dumps(parsed))
            else:
                print(f"SKIPPED: {parsed['agent']} is paused — {parsed['reason']}")
                print(f"         Trips: {parsed['total_trips']}, Last: {parsed.get('last_trip_at', 'N/A')}")
            sys.exit(0)
    except (json.JSONDecodeError, TypeError):
        pass  # Not JSON — normal prompt text

    if args.json:
        print(json.dumps({"prompt": prompt}))
    else:
        print(prompt)


if __name__ == "__main__":
    main()
