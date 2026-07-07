#!/usr/bin/env python3
"""Nightly Synthesis — aggregate journal insights, rank, promote.

Runs nightly (cron at e.g. 04:30 ET) to:
1. Query Postgres for journal entries, decisions, and executed trades
2. Run the JournalAnalyzer to extract insights
3. Feed insights into the Synthesizer for ranking + promotion evaluation
4. Produce a markdown report → stdout + reports/nightly_synthesis_YYYY-MM-DD.md
5. Push summary to Canvas

Usage:
    python3 scripts/nightly_synthesis.py --date 2026-07-07
    python3 scripts/nightly_synthesis.py --trader kairos --dry-run
    python3 scripts/nightly_synthesis.py --auto-promote

Cron:  30 4 * * 1-5  cd /home/raf/projects/paper-trading-rebuild && python3 scripts/nightly_synthesis.py >> logs/nightly_synthesis.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure we can import from src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.journal_analyzer import (
    JournalAnalyzer,
    JournalInsight,
    analyze_journal,
)
from src.synthesis import (
    Synthesizer,
    NightlySummary,
    synthesize_nightly,
)

log = logging.getLogger("nightly_synthesis")


def query_journal_entries(conn, trader_id: str, date_str: str) -> List[str]:
    """Query journal entries for a trader on a given date."""
    cur = conn.cursor()
    cur.execute(
        """SELECT rationale FROM trading.journal
           WHERE trader_id = %s
             AND timestamp::date = %s::date
           ORDER BY timestamp""",
        (trader_id, date_str),
    )
    rows = cur.fetchall()
    cur.close()
    return [r[0] for r in rows if r[0]]


def query_decisions(conn, trader_id: str, date_str: str) -> List[Dict[str, Any]]:
    """Query decisions for a trader on a given date."""
    cur = conn.cursor()
    cur.execute(
        """SELECT ticker, decision, conviction, rationale, created_at
           FROM trading.decisions
           WHERE trader_id = %s
             AND created_at::date = %s::date
           ORDER BY created_at""",
        (trader_id, date_str),
    )
    rows = cur.fetchall()
    cur.close()
    return [
        {
            "ticker": r[0] or "?",
            "decision": r[1] or "",
            "conviction": float(r[2] or 0),
            "rationale": r[3] or "",
            "created_at": str(r[4]) if r[4] else "",
        }
        for r in rows
    ]


def query_trades(conn, agent_id: str, date_str: str) -> List[Dict[str, Any]]:
    """Query executed trades for a trader on a given date."""
    cur = conn.cursor()
    cur.execute(
        """SELECT ticker, action, pnl, pnl_pct, entry_price, exit_price,
                  exit_reason, stop_loss, status
           FROM trading.executed_trades
           WHERE agent_id = %s
             AND entry_time::date = %s::date
           ORDER BY entry_time""",
        (agent_id, date_str),
    )
    rows = cur.fetchall()
    cur.close()
    return [
        {
            "ticker": r[0] or "?",
            "action": r[1] or "",
            "pnl": float(r[2] or 0),
            "pnl_pct": float(r[3] or 0) * 100 if r[3] else 0,
            "entry_price": float(r[4] or 0),
            "exit_price": float(r[5] or 0),
            "exit_reason": r[6] or "",
            "stop_loss": float(r[7] or 0),
            "status": r[8] or "unknown",
            "conviction": 0.5,  # Default — executed_trades doesn't store conviction
        }
        for r in rows
    ]


def query_sweep_results(conn, trader: str, date_str: str) -> Dict[str, Any]:
    """Query the most recent sweep results for a trader."""
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT COUNT(*) as n_scenarios,
                      COUNT(DISTINCT run_id) as n_runs,
                      COALESCE(MAX(objective_score), 0) as best_score,
                      COALESCE(SUM(n_trades), 0) as total_trades
               FROM trading.sweep_results
               WHERE trader_id = %s
                 AND run_id LIKE %s""",
            (trader, f"%{date_str}%"),
        )
        row = cur.fetchone()
        cur.close()
        if row and (row[0] > 0):
            return {
                "n_scenarios": int(row[0] or 0),
                "n_trades": int(row[3] or 0),
                "best_score": float(row[2] or 0),
                "top_variant": f"sweep_{date_str}",
            }
    except Exception:
        cur.close()

    # Fallback: query without date filter
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT COUNT(*) as n_scenarios,
                      COALESCE(MAX(objective_score), 0) as best_score,
                      COALESCE(SUM(n_trades), 0) as total_trades
               FROM trading.sweep_results
               WHERE trader_id = %s""",
            (trader,),
        )
        row = cur.fetchone()
        cur.close()
        if row and row[0] > 0:
            return {
                "n_scenarios": int(row[0] or 0),
                "n_trades": int(row[2] or 0),
                "best_score": float(row[1] or 0),
                "top_variant": "latest",
            }
    except Exception:
        cur.close()

    return {"n_scenarios": 0, "n_trades": 0, "best_score": 0.0, "top_variant": ""}


# ── Mappings ─────────────────────────────────────────────────────────────────

# Agent IDs in executed_trades vs trader_ids in journal/decisions
AGENT_TO_TRADER = {
    "trader-kairos": "kairos",
    "trader-aldridge": "aldridge",
    "trader-stonks": "stonks",
}

TRADER_TO_AGENT = {v: k for k, v in AGENT_TO_TRADER.items()}

# All active traders
ALL_TRADERS = ["kairos", "aldridge", "stonks"]


def run_synthesis(
    date_str: str,
    trader: Optional[str] = None,
    output_dir: Optional[str] = None,
    dry_run: bool = False,
    auto_promote: bool = False,
) -> NightlySummary:
    """Run the full nightly synthesis pipeline.

    Args:
        date_str: Date to analyze (YYYY-MM-DD).
        trader: Specific trader short name, or None for all.
        output_dir: Directory for report output.
        dry_run: Don't write report or push to Canvas.
        auto_promote: Create PR/branch for AUTO_PROMOTE insights.

    Returns:
        NightlySummary with all syntheses and promotions.
    """
    traders = [trader] if trader else ALL_TRADERS

    # Connect to Postgres
    from src.db.connection import get_connection
    conn = get_connection()

    # Collect journal data and run analysis per trader
    analyzer = JournalAnalyzer()
    trader_insights: Dict[str, List[JournalInsight]] = {}
    scenarios: Dict[str, Dict[str, Any]] = {}

    for t in traders:
        agent_id = TRADER_TO_AGENT.get(t, f"trader-{t}")

        # Query data
        journal = query_journal_entries(conn, agent_id, date_str)
        decisions = query_decisions(conn, agent_id, date_str)
        trades = query_trades(conn, agent_id, date_str)

        # Enrich trades with conviction from decisions
        decision_conviction: Dict[str, float] = {}
        for d in decisions:
            ticker = d.get("ticker", "")
            decision_conviction[ticker] = d.get("conviction", 0.5)

        for tr in trades:
            tr["conviction"] = decision_conviction.get(tr["ticker"], 0.5)

        print(f"[nightly_synthesis] {t}: {len(journal)} journal entries, "
              f"{len(decisions)} decisions, {len(trades)} trades")

        # Run journal analysis
        insights = analyzer.analyze(
            journal=journal,
            reflections=[],
            trades=trades,
            use_llm=False,
        )
        trader_insights[t] = insights

        # Get sweep scenarios
        trader_scenarios = query_sweep_results(conn, t, date_str)
        trader_scenarios["trader"] = t
        trader_scenarios["n_trades"] = max(trader_scenarios.get("n_trades", 0), len(trades))
        scenarios[t] = trader_scenarios

        print(f"  → {len(insights)} insights extracted")

    conn.close()

    # ── Parameter History Analysis (#23) ──────────────────────────────────
    param_report = ""
    try:
        from src.param_history import ParamHistory
        ph = ParamHistory()
        traders_to_analyze = [trader] if trader else ["kairos", "aldridge", "stonks"]
        for tid in traders_to_analyze:
            report = ph.generate_report(trader_id=tid, days=1)
            if report.total_changes > 0:
                param_report += ph.summary_str(report) + "\n\n"
        if param_report:
            print(f"\n[nightly_synthesis] Parameter history analysis appended")
    except Exception as e:
        print(f"\n[nightly_synthesis] Param history analysis failed: {e}")
        param_report = ""

    # Run synthesis
    synthesizer = Synthesizer()
    summary = synthesizer.synthesize(
        trader_insights=trader_insights,
        scenarios=scenarios,
        date=datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now(),
    )

    # Write report
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        report_path = os.path.join(output_dir, f"nightly_synthesis_{date_str}.md")
        formatted = summary.format()
        if param_report:
            formatted += "\n\n---\n\n" + param_report
        with open(report_path, "w") as f:
            f.write(formatted)
        print(f"\n[nightly_synthesis] Report written to {report_path}")

    # Auto-promote winners
    if auto_promote and not dry_run:
        auto = [p for p in summary.promotions if p["action"] == "AUTO_PROMOTE"]
        if auto:
            print(f"\n[nightly_synthesis] AUTO-PROMOTING {len(auto)} insight(s):")
            for p in auto:
                print(f"  - {p['trader'].capitalize()}: {p['insight']['description']}")
                print(f"    → {p['insight']['suggestion']}")
            # TODO: Create PR/branch for each promotion
            # For now, the report serves as the authoritative record
        else:
            print("\n[nightly_synthesis] No insights qualified for auto-promotion.")

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Nightly Synthesis — aggregate, rank, and promote journal insights"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Date to analyze (YYYY-MM-DD). Default: yesterday.",
    )
    parser.add_argument(
        "--trader", type=str, default=None,
        help="Single trader short name (e.g., 'kairos'). Default: all.",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output directory for summary report (default: reports/).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Analyze and print but don't write report or push to Canvas.",
    )
    parser.add_argument(
        "--auto-promote", action="store_true",
        help="Create PRs/branches for AUTO_PROMOTE insights.",
    )
    parser.add_argument(
        "--canvas", action="store_true",
        help="Push summary to Canvas (requires CANVAS_URL + CANVAS_TOKEN).",
    )

    args = parser.parse_args()

    # Determine date
    if args.date:
        date_str = args.date
    else:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # Determine output directory
    output_dir = args.output or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "reports"
    )

    print(f"=== Nightly Synthesis: {date_str} ===")
    print(f"Traders: {args.trader or 'all'}")
    print(f"Auto-promote: {'ON' if args.auto_promote else 'OFF'}")
    print()

    summary = run_synthesis(
        date_str=date_str,
        trader=args.trader,
        output_dir=output_dir if not args.dry_run else None,
        dry_run=args.dry_run,
        auto_promote=args.auto_promote,
    )

    # Print formatted summary to stdout
    print()
    print(summary.format())

    # Push to Canvas
    if args.canvas and not args.dry_run:
        try:
            canvas_content = summary.format()
            # Truncate for Canvas (keep it focused)
            if len(canvas_content) > 8000:
                canvas_content = canvas_content[:8000] + "\n\n... (truncated for Canvas)"
            push_to_canvas(
                title=f"Nightly Synthesis — {date_str}",
                content=canvas_content,
                agent="hermes",
                emoji="🪽",
            )
            print("\n[nightly_synthesis] Pushed to Canvas ✓")
        except Exception as e:
            print(f"\n[nightly_synthesis] Canvas push failed: {e}")


def push_to_canvas(
    title: str,
    content: str,
    board: str = "main",
    agent: str = "hermes",
    emoji: str = "🪽",
):
    """Push a markdown card to Canvas."""
    import urllib.request

    canvas_env = os.path.expanduser("~/canvas/.env")
    if not os.path.exists(canvas_env):
        raise FileNotFoundError(f"Canvas env file not found: {canvas_env}")

    # Load env vars
    env_vars = {}
    with open(canvas_env) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                env_vars[key.strip()] = val.strip().strip('"').strip("'")

    canvas_url = env_vars.get("CANVAS_URL", "")
    canvas_token = env_vars.get("CANVAS_TOKEN", "")

    if not canvas_url or not canvas_token:
        raise RuntimeError("CANVAS_URL or CANVAS_TOKEN not found in ~/canvas/.env")

    data = json.dumps({
        "type": "markdown",
        "title": title,
        "content": content,
        "board": board,
        "agent": agent,
        "agent_emoji": emoji,
    }).encode()

    req = urllib.request.Request(
        f"{canvas_url}/push",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {canvas_token}",
        },
    )
    result = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return result


if __name__ == "__main__":
    main()
