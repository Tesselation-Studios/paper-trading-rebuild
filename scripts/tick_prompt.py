#!/usr/bin/env python3
"""
tick_prompt.py — Pre-assembles complete trading tick context from data bus.

The LLM never touches a tool during a trading tick. All context arrives
pre-assembled. This script is the bridge between the data bus and the agent.

Usage:
    scripts/tick_prompt.py --trader kairos
    scripts/tick_prompt.py --trader stonks
    scripts/tick_prompt.py --trader aldridge

Output: A single prompt string on stdout, ready to pipe into an agent session.

Architecture (per SPEC):
    Cron fires
      -> scripts/tick_prompt.py --trader kairos
        -> reads prompt template
        -> hits data bus for live state (portfolio, quotes, signals)
        -> renders everything into one prompt string
      -> Agent receives fully-loaded context
      -> First thought is about trading
      -> Outputs JSON: BUY/SELL/HOLD with thesis + signals
      -> Tick done. Session discarded.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
DATA_BUS_URL = os.environ.get("DATA_BUS_URL", "http://localhost:5000")
TRADER_DB_PATH = REPO_ROOT / "shared" / "trader.db"

# Default request timeout for data bus calls
DATA_BUS_TIMEOUT = int(os.environ.get("DATA_BUS_TIMEOUT", "10"))

# Traders and their data bus endpoints
TRADER_CONFIG: dict[str, dict[str, Any]] = {
    "kairos": {
        "name": "Kairos Capital",
        "persona": "Zara Chen",
        "interval_min": 5,
        "model": "flash",
        "endpoints": [
            "momentum",
        ],
        "extra_endpoints": ["flow", "sentiment"],
        "signal_sources": ["momentum", "flow", "sentiment"],
    },
    "stonks": {
        "name": "Stonks Capital",
        "persona": "Stan Hoolihan",
        "interval_min": 15,
        "model": "flash",
        "endpoints": [
            "momentum",
            "social?source=all",
        ],
        "extra_endpoints": ["flow", "sentiment", "congress", "crypto"],
        "signal_sources": ["momentum", "social", "flow", "congress", "crypto"],
    },
    "aldridge": {
        "name": "Aldridge Capital",
        "persona": "Aldridge",
        "interval_min": 30,
        "model": "pro",
        "endpoints": [
            "momentum",
        ],
        "extra_endpoints": ["insiders", "sentiment", "flow"],
        "signal_sources": ["momentum", "insiders", "sentiment", "flow"],
    },
}


# ---------------------------------------------------------------------------
# Data bus client
# ---------------------------------------------------------------------------

def fetch_json(endpoint: str, timeout: int = DATA_BUS_TIMEOUT) -> dict[str, Any]:
    """Fetch JSON from the data bus. Returns empty dict on failure."""
    url = f"{DATA_BUS_URL}/{endpoint.lstrip('/')}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except (urllib.error.URLError, urllib.error.HTTPError,
            ConnectionError, TimeoutError, json.JSONDecodeError,
            OSError) as e:
        sys.stderr.write(f"[tick_prompt] WARNING: Failed to fetch {url}: {e}\n")
        return {"error": str(e), "endpoint": endpoint}


def fetch_text(endpoint: str, timeout: int = DATA_BUS_TIMEOUT) -> str:
    """Fetch plain text from the data bus. Returns empty string on failure."""
    url = f"{DATA_BUS_URL}/{endpoint.lstrip('/')}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError,
            ConnectionError, TimeoutError, OSError) as e:
        sys.stderr.write(f"[tick_prompt] WARNING: Failed to fetch {url}: {e}\n")
        return f"[Error fetching {endpoint}: {e}]"


# ---------------------------------------------------------------------------
# Signal report assembly
# ---------------------------------------------------------------------------

def build_signal_report(trader: str, top_n: int = 5) -> str:
    """Hit data bus endpoints and assemble a structured signal report."""
    config = TRADER_CONFIG[trader]
    lines: list[str] = []

    # Fetch primary endpoints
    for endpoint in config["endpoints"]:
        data = fetch_json(endpoint)
        if "error" in data:
            lines.append(f"### {endpoint}\n[Unavailable: {data['error']}]")
        else:
            lines.append(f"### {endpoint}")
            # Format momentum data
            if endpoint == "momentum":
                rankings = data.get("rankings", data.get("data", []))
                if isinstance(rankings, list):
                    lines.append(f"Top {min(top_n, len(rankings))} by momentum score:")
                    for item in rankings[:top_n]:
                        if isinstance(item, dict):
                            sym = item.get("symbol", item.get("ticker", "?"))
                            score = item.get("score", item.get("momentum", "?"))
                            vol = item.get("volume", "")
                            lines.append(f"  - {sym}: score={score}" +
                                         (f" vol={vol}" if vol else ""))
                        elif isinstance(item, (list, tuple)):
                            lines.append(f"  - {item[0]}: {item[1]}")
                        else:
                            lines.append(f"  - {item}")
            elif endpoint.startswith("social"):
                items = data.get("trending", data.get("data", []))
                if isinstance(items, list):
                    for item in items[:top_n]:
                        if isinstance(item, dict):
                            lines.append(f"  - {item.get('symbol', '?')}: "
                                         f"score={item.get('score', item.get('sentiment', '?'))}")
                        else:
                            lines.append(f"  - {item}")
            else:
                # Generic formatting
                if isinstance(data, dict):
                    for key, val in data.items():
                        if key not in ("status", "timestamp"):
                            lines.append(f"  {key}: {val}")
                elif isinstance(data, list):
                    for item in data[:top_n]:
                        lines.append(f"  - {item}")
                else:
                    lines.append(f"  {data}")
        lines.append("")

    # Fetch extra endpoints for top symbols (derived from momentum if available)
    # For simplicity, we fetch each extra endpoint for context
    for endpoint in config.get("extra_endpoints", []):
        data = fetch_json(endpoint)
        if "error" in data:
            lines.append(f"### {endpoint}\n[Unavailable: {data['error']}]")
        else:
            lines.append(f"### {endpoint}")
            if isinstance(data, dict):
                # Try common key names
                items = (data.get("data", data.get("results",
                               data.get("items", data.get("entries", [])))))
                if isinstance(items, list) and items:
                    for item in items[:top_n]:
                        if isinstance(item, dict):
                            lines.append(f"  - {json.dumps(item)[:200]}")
                        else:
                            lines.append(f"  - {item}")
                else:
                    for key, val in data.items():
                        if key not in ("status", "timestamp"):
                            lines.append(f"  {key}: {val}")
            elif isinstance(data, list):
                for item in data[:top_n]:
                    lines.append(f"  - {item}")
            else:
                lines.append(f"  {data}")
        lines.append("")

    return "\n".join(lines) if lines else "[No signal data available]"


# ---------------------------------------------------------------------------
# Portfolio state
# ---------------------------------------------------------------------------

def build_portfolio_state(trader: str) -> str:
    """Fetch portfolio state from the data bus."""
    data = fetch_json(f"portfolio/{trader}")
    if "error" in data and "endpoint" in data:
        # Try alternative endpoint
        data = fetch_json(f"portfolio?account={trader}")

    if not data or "error" in data:
        # Fall back to DB
        return _build_portfolio_from_db(trader)

    lines = [f"Account: {trader}"]
    if "cash" in data:
        lines.append(f"Cash: ${data['cash']:,.2f}")
    if "buying_power" in data:
        lines.append(f"Buying Power: ${data['buying_power']:,.2f}")
    if "equity" in data:
        lines.append(f"Equity: ${data['equity']:,.2f}")
    if "portfolio_value" in data:
        lines.append(f"Portfolio Value: ${data['portfolio_value']:,.2f}")

    positions = data.get("positions", data.get("holdings", []))
    if positions:
        lines.append(f"\nPositions ({len(positions)}):")
        for pos in positions:
            if isinstance(pos, dict):
                sym = pos.get("symbol", pos.get("ticker", "?"))
                qty = pos.get("qty", pos.get("quantity", "?"))
                mv = pos.get("market_value", pos.get("value", "?"))
                pnl = pos.get("unrealized_pl", pos.get("pnl", ""))
                lines.append(
                    f"  - {sym}: {qty} shares, MV=${mv}"
                    + (f", P&L=${pnl}" if pnl else "")
                )

    return "\n".join(lines)


def _build_portfolio_from_db(trader: str) -> str:
    """Fallback: read portfolio snapshot from trader.db."""
    try:
        conn = sqlite3.connect(str(TRADER_DB_PATH))
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM portfolio_snapshots WHERE agent_id = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (trader,),
        )
        row = cursor.fetchone()
        conn.close()

        if row:
            lines = [f"Account: {trader}"]
            row_dict = dict(row)
            for key in ("cash", "equity", "buying_power", "portfolio_value"):
                if key in row_dict and row_dict[key] is not None:
                    lines.append(f"{key.replace('_', ' ').title()}: ${row_dict[key]:,.2f}")
            if "positions" in row_dict and row_dict["positions"]:
                lines.append(f"\nPositions: {row_dict['positions']}")
            return "\n".join(lines)
        return f"[No portfolio data available for {trader}]"
    except (sqlite3.Error, OSError) as e:
        return f"[Portfolio DB error: {e}]"


# ---------------------------------------------------------------------------
# Journal context
# ---------------------------------------------------------------------------

def build_journal_context(trader: str, count: int = 5) -> str:
    """Read last N journal entries from the database for context."""
    try:
        conn = sqlite3.connect(str(TRADER_DB_PATH))
        conn.row_factory = sqlite3.Row

        # Query strategy_notes ordered by timestamp
        cursor = conn.execute(
            "SELECT timestamp, note, category FROM strategy_notes "
            "WHERE agent_id IN (?, ?) "
            "ORDER BY timestamp DESC LIMIT ?",
            (trader, f"trader-{trader}", count),
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return "[No journal entries yet]"

        lines = []
        for i, row in enumerate(rows, 1):
            ts = row["timestamp"]
            note = row["note"] if row["note"] else ""
            category = row["category"] if row["category"] else ""
            # Truncate long entries
            if len(note) > 300:
                note = note[:297] + "..."
            lines.append(f"{i}. [{ts}] [{category}] {note}")

        return "\n".join(lines)
    except (sqlite3.Error, OSError) as e:
        return f"[Journal DB error: {e}]"


# ---------------------------------------------------------------------------
# Regime detection
# ---------------------------------------------------------------------------

def build_regime_context(trader: str) -> tuple[str, float]:
    """Determine current market regime from data bus."""
    data = fetch_json("regime")
    if "error" in data:
        # Default regime
        return ("unknown", 0.5)

    regime = data.get("regime", data.get("name", "unknown"))
    confidence = data.get("confidence", 0.5)
    return (regime, float(confidence))


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def assemble_prompt(trader: str) -> str:
    """Assemble the complete tick prompt for a trader."""
    config = TRADER_CONFIG[trader]

    # 1. Read the prompt template
    template_path = PROMPTS_DIR / f"{trader}.txt"
    if not template_path.exists():
        sys.stderr.write(
            f"[tick_prompt] ERROR: Template not found: {template_path}\n"
        )
        sys.exit(1)

    template = template_path.read_text()

    # 2. Gather live context
    regime, regime_confidence = build_regime_context(trader)
    signal_report = build_signal_report(trader)
    portfolio_state = build_portfolio_state(trader)
    journal_entries = build_journal_context(trader)

    # 3. Fill in template placeholders
    # The template uses Python str.format() syntax with double-brace escaping
    # for the JSON schema braces in the template.
    prompt = template.format(
        regime=regime,
        regime_confidence=f"{regime_confidence:.2f}",
        signal_report=signal_report,
        portfolio_state=portfolio_state,
        journal_entries=journal_entries,
    )

    return prompt


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    global DATA_BUS_URL, DATA_BUS_TIMEOUT

    parser = argparse.ArgumentParser(
        description="Pre-assemble tick context for paper trading agents.",
        epilog=(
            "Outputs the complete prompt to stdout. Pipe into the agent session. "
            "The LLM receives fully-loaded context and never touches a tool "
            "during a trading tick."
        ),
    )
    parser.add_argument(
        "--trader",
        required=True,
        choices=sorted(TRADER_CONFIG.keys()),
        help="Trader name (kairos, stonks, aldridge)",
    )
    parser.add_argument(
        "--data-bus",
        default=DATA_BUS_URL,
        help=f"Data bus URL (default: {DATA_BUS_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DATA_BUS_TIMEOUT,
        help=f"Per-endpoint timeout seconds (default: {DATA_BUS_TIMEOUT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Assemble but don't output prompt (measure timing)",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Output timing info to stderr",
    )

    args = parser.parse_args()

    # Apply overrides
    DATA_BUS_URL = args.data_bus
    DATA_BUS_TIMEOUT = args.timeout

    # Measure timing
    start = time.time()

    # Check template exists
    template_path = PROMPTS_DIR / f"{args.trader}.txt"
    if not template_path.exists():
        sys.stderr.write(
            f"[tick_prompt] ERROR: Template not found: {template_path}\n"
        )
        sys.exit(1)

    # Check market hours constraint (per SPEC: prompt locked during market hours)
    # The template itself is static during market hours, but we still assemble
    # fresh data. The "locked" constraint refers to the template text, not the
    # running script.

    # Assemble
    try:
        prompt = assemble_prompt(args.trader)
    except Exception as e:
        sys.stderr.write(f"[tick_prompt] FATAL: Prompt assembly failed: {e}\n")
        sys.exit(1)

    elapsed = time.time() - start

    if args.benchmark:
        sys.stderr.write(
            f"[tick_prompt] {args.trader}: assembled {len(prompt)}-char prompt "
            f"in {elapsed:.2f}s\n"
        )

    if not args.dry_run:
        sys.stdout.write(prompt)

    # Warn if assembly took too long
    if elapsed > 15:
        sys.stderr.write(
            f"[tick_prompt] WARNING: Prompt assembly took {elapsed:.1f}s "
            f"(expected <15s). Check data bus health.\n"
        )


if __name__ == "__main__":
    main()
