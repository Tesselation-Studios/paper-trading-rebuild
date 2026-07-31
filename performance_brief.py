#!/usr/bin/env python3
"""
Performance Brief — Structured markdown stats per agent.

Reads trader.db and produces a concise, LLM-readable performance brief.

Usage:
    python3 src/performance_brief.py --agent kairos --days 14
    python3 src/performance_brief.py --all
    python3 src/performance_brief.py --agent kairos --days 30 --compact

Output: clean markdown with 📊 🎯 📈 🔄 emoji headers.
If agent has < 10 resolved predictions, returns null.
"""

import sys
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
from collections import defaultdict

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "shared" / "trader.db"
AGENT_IDS = ["kairos", "aldridge", "stonks"]
MIN_PREDICTIONS = 10


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _sharpe_ish(returns: List[float]) -> Optional[float]:
    """Compute a simplified Sharpe-like score.
    Uses mean / std of returns, annualized by sqrt(252).
    Returns None if insufficient data (fewer than 2 values, or std ~ 0).
    """
    if len(returns) < 2:
        return None
    import statistics
    mean_r = statistics.mean(returns)
    std_r = statistics.stdev(returns)
    if std_r < 1e-9:
        return None
    return round((mean_r / std_r) * (252 ** 0.5), 3)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Rolling Stats
# ═════════════════════════════════════════════════════════════════════════════

def _rolling_stats(agent_id: str, days: int) -> Dict[str, Any]:
    """Compute rolling win rate, avg return, Sharpe-like score."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    with _connect() as conn:
        rows = conn.execute(
            """SELECT outcome, actual_return FROM predictions
               WHERE agent_id = ? AND outcome != 'pending'
               AND timestamp >= ?
               ORDER BY timestamp DESC""",
            (f"trader-{agent_id}", cutoff)
        ).fetchall()

    if not rows:
        return {"resolved": 0}

    resolved = len(rows)
    wins = sum(1 for r in rows if r["outcome"] == "win")
    losses = sum(1 for r in rows if r["outcome"] == "loss")
    returns = [r["actual_return"] for r in rows if r["actual_return"] is not None]

    win_rate = round(wins / resolved, 3) if resolved > 0 else 0.0
    avg_return = round(sum(returns) / len(returns), 4) if returns else 0.0
    sharpe = _sharpe_ish(returns)

    return {
        "resolved": resolved,
        "wins": wins,
        "losses": losses,
        "breakeven": resolved - wins - losses,
        "win_rate": win_rate,
        "avg_return": avg_return,
        "sharpe": sharpe,
    }


# ═════════════════════════════════════════════════════════════════════════════
# 2. Signal Effectiveness
# ═════════════════════════════════════════════════════════════════════════════

def _signal_effectiveness(agent_id: str) -> List[Dict[str, Any]]:
    """For each signal in signals_used, compute win rate when active.
    Reads signals_used from decisions table (JSON array).
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT d.signals_used, t.outcome
               FROM decisions d
               LEFT JOIN trades t ON d.id = t.decision_id AND t.status = 'closed'
               WHERE d.agent_id = ? AND d.signals_used IS NOT NULL
               AND d.signals_used != '[]' AND t.outcome IS NOT NULL
               ORDER BY d.timestamp DESC""",
            (f"trader-{agent_id}",)
        ).fetchall()

    signal_stats: Dict[str, List[str]] = defaultdict(list)
    for row in rows:
        try:
            signals = json.loads(row["signals_used"])
        except (json.JSONDecodeError, TypeError):
            continue
        outcome = row["outcome"]
        for sig in signals:
            signal_stats[sig].append(outcome)

    results = []
    for signal, outcomes in signal_stats.items():
        total = len(outcomes)
        wins = sum(1 for o in outcomes if o == "win")
        wr = round(wins / total, 3) if total > 0 else 0.0
        results.append({
            "signal": signal,
            "trades": total,
            "win_rate": wr,
        })

    results.sort(key=lambda x: x["win_rate"], reverse=True)
    return results


# ═════════════════════════════════════════════════════════════════════════════
# 3. Conviction Calibration
# ═════════════════════════════════════════════════════════════════════════════

def _conviction_calibration(agent_id: str) -> Dict[str, Any]:
    """Group decisions by confidence quartiles, compare vs actual win rate.
    Flags over/under confidence.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT d.confidence, t.outcome
               FROM decisions d
               JOIN trades t ON d.id = t.decision_id AND t.status = 'closed'
               WHERE d.agent_id = ? AND d.confidence IS NOT NULL
               AND t.outcome IS NOT NULL""",
            (f"trader-{agent_id}",)
        ).fetchall()

    if not rows:
        return {"bins": [], "verdict": "no data"}

    # Bin by confidence ranges
    bins_def = [(0.0, 0.25, "0-25%"), (0.25, 0.50, "25-50%"),
                (0.50, 0.75, "50-75%"), (0.75, 0.90, "75-90%"), (0.90, 1.01, "90-100%")]
    bins = []
    for lo, hi, label in bins_def:
        bin_rows = [r for r in rows if lo <= (r["confidence"] or 0) < hi]
        if not bin_rows:
            continue
        total = len(bin_rows)
        wins = sum(1 for r in bin_rows if r["outcome"] == "win")
        wr = round(wins / total, 3)
        avg_conf = round(sum(r["confidence"] for r in bin_rows) / total, 3)
        bins.append({
            "bin": label,
            "count": total,
            "avg_confidence": avg_conf,
            "win_rate": wr,
            "calibration_error": round(abs(avg_conf - wr), 3),
        })

    # Verdict
    overconfident = [b for b in bins if b["avg_confidence"] - b["win_rate"] > 0.15 and b["count"] >= 3]
    underconfident = [b for b in bins if b["win_rate"] - b["avg_confidence"] > 0.15 and b["count"] >= 3]
    verdict = "✅ well-calibrated"
    if overconfident:
        bins_str = ", ".join(b["bin"] for b in overconfident)
        verdict = f"⚠️ overconfident in {bins_str}"
    if underconfident:
        bins_str = ", ".join(b["bin"] for b in underconfident)
        addendum = f" + underconfident in {bins_str}" if "⚠️" in verdict else f"⚠️ underconfident in {bins_str}"
        if "⚠️" in verdict:
            verdict = verdict + addendum

    return {"bins": bins, "verdict": verdict}


# ═════════════════════════════════════════════════════════════════════════════
# 4. Pattern Detection
# ═════════════════════════════════════════════════════════════════════════════

def _pattern_detection(agent_id: str) -> Dict[str, Any]:
    """Analyze thesis length vs win rate, day-of-week vs win rate,
    holding horizon vs win rate."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT d.thesis, d.timestamp, d.exit_condition, d.holding_horizon_days,
                      t.outcome, t.pnl_pct
               FROM decisions d
               JOIN trades t ON d.id = t.decision_id AND t.status = 'closed'
               WHERE d.agent_id = ? AND t.outcome IS NOT NULL""",
            (f"trader-{agent_id}",)
        ).fetchall()

    if not rows:
        return {}

    # Thesis length analysis
    short = [r for r in rows if len((r["thesis"] or "")) < 100]
    medium = [r for r in rows if 100 <= len((r["thesis"] or "")) < 300]
    long = [r for r in rows if len((r["thesis"] or "")) >= 300]

    def _wr(rows_list):
        if not rows_list:
            return None
        w = sum(1 for r in rows_list if r["outcome"] == "win")
        return round(w / len(rows_list), 3)

    thesis_wr = {
        "short_thesis_wr": _wr(short),
        "medium_thesis_wr": _wr(medium),
        "long_thesis_wr": _wr(long),
    }

    # Day of week analysis
    dow_stats: Dict[str, List[str]] = defaultdict(list)
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["timestamp"])
            day = dt.strftime("%a")
            dow_stats[day].append(r["outcome"])
        except (ValueError, TypeError):
            continue

    dow_wr = {}
    for day, outcomes in sorted(dow_stats.items()):
        if outcomes:
            w = sum(1 for o in outcomes if o == "win")
            dow_wr[day] = {
                "count": len(outcomes),
                "win_rate": round(w / len(outcomes), 3),
            }

    # Holding horizon vs win rate
    horizon_buckets = {"<3d": [], "3-7d": [], "7-14d": [], ">14d": []}
    for r in rows:
        hd = r["holding_horizon_days"]
        if hd is None:
            continue
        if hd < 3:
            bucket = "<3d"
        elif hd < 7:
            bucket = "3-7d"
        elif hd < 14:
            bucket = "7-14d"
        else:
            bucket = ">14d"
        horizon_buckets[bucket].append(r["outcome"])

    horizon_wr = {}
    for label, outcomes in horizon_buckets.items():
        if outcomes:
            w = sum(1 for o in outcomes if o == "win")
            horizon_wr[label] = {
                "count": len(outcomes),
                "win_rate": round(w / len(outcomes), 3),
            }

    # Exit condition rate
    with_exit = [r for r in rows if r.get("exit_condition")]
    exit_rate = round(len(with_exit) / len(rows), 3) if rows else 0.0

    return {
        "thesis_length": thesis_wr,
        "day_of_week": dow_wr,
        "holding_horizon": horizon_wr,
        "exit_condition_rate": exit_rate,
    }


# ═════════════════════════════════════════════════════════════════════════════
# 5. Strategy Drift
# ═════════════════════════════════════════════════════════════════════════════

def _strategy_drift(agent_id: str, days: int) -> Dict[str, Any]:
    """Compare recent behavior vs historical averages."""
    recent_cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    old_cutoff = (datetime.now() - timedelta(days=days * 3)).isoformat()

    with _connect() as conn:
        # Recent data
        recent = conn.execute(
            """SELECT thesis, exit_condition, holding_horizon_days
               FROM decisions WHERE agent_id = ?
               AND timestamp >= ?""",
            (f"trader-{agent_id}", recent_cutoff)
        ).fetchall()

        # Historical data (before recent window)
        old = conn.execute(
            """SELECT thesis, exit_condition, holding_horizon_days
               FROM decisions WHERE agent_id = ?
               AND timestamp < ? AND timestamp >= ?""",
            (f"trader-{agent_id}", recent_cutoff, old_cutoff)
        ).fetchall()

    def _avg_thesis_len(rows):
        if not rows:
            return None
        lengths = [len(r["thesis"] or "") for r in rows]
        return round(sum(lengths) / len(lengths), 1)

    def _exit_rate(rows):
        if not rows:
            return None
        with_exit = sum(1 for r in rows if r.get("exit_condition"))
        return round(with_exit / len(rows), 3)

    def _avg_horizon(rows):
        if not rows:
            return None
        horizons = [r["holding_horizon_days"] for r in rows if r["holding_horizon_days"] is not None]
        if not horizons:
            return None
        return round(sum(horizons) / len(horizons), 1)

    drift = {
        "recent": {
            "count": len(recent),
            "avg_thesis_len": _avg_thesis_len(recent),
            "exit_condition_rate": _exit_rate(recent),
            "avg_horizon_days": _avg_horizon(recent),
        },
        "historical": {
            "count": len(old),
            "avg_thesis_len": _avg_thesis_len(old),
            "exit_condition_rate": _exit_rate(old),
            "avg_horizon_days": _avg_horizon(old),
        },
    }

    # Flag significant drift
    flags = []
    r = drift["recent"]
    h = drift["historical"]
    if r["avg_thesis_len"] and h["avg_thesis_len"]:
        change = r["avg_thesis_len"] - h["avg_thesis_len"]
        if abs(change) > 50:
            flags.append(f"thesis length shifted by {change:+.0f} chars")
    if r["exit_condition_rate"] and h["exit_condition_rate"]:
        change = r["exit_condition_rate"] - h["exit_condition_rate"]
        if abs(change) > 0.15:
            flags.append(f"exit condition rate shifted by {change:+.0%}")
    if r["avg_horizon_days"] and h["avg_horizon_days"]:
        change = r["avg_horizon_days"] - h["avg_horizon_days"]
        if abs(change) > 3:
            flags.append(f"horizon shifted by {change:+.1f} days")

    drift["drift_flags"] = flags
    return drift


# ═════════════════════════════════════════════════════════════════════════════
# Brief Generation
# ═════════════════════════════════════════════════════════════════════════════

def generate_brief(agent_id: str, days: int = 14, compact: bool = False) -> Optional[Dict[str, Any]]:
    """Generate a structured performance brief for one agent.

    Returns None if fewer than MIN_PREDICTIONS resolved predictions.
    """
    rolling = _rolling_stats(agent_id, days)

    if rolling["resolved"] < MIN_PREDICTIONS:
        return None

    signals_eff = _signal_effectiveness(agent_id)
    calibration = _conviction_calibration(agent_id)
    patterns = _pattern_detection(agent_id)
    drift = _strategy_drift(agent_id, days)

    # ── Build markdown ─────────────────────────────────────────────────────
    lines = []
    name_map = {"kairos": "Kairós", "aldridge": "Aldridge", "stonks": "Stonks"}
    display_name = name_map.get(agent_id, agent_id.capitalize())

    lines.append(f"📊 **{display_name} — {days}d Brief**")
    lines.append(f"`{rolling['resolved']} resolved, {rolling['wins']}W/{rolling['losses']}L`")
    lines.append("")

    # Rolling stats
    sharp_str = f" Sharpe={rolling['sharpe']}" if rolling.get("sharpe") else ""
    lines.append(f"🎯 **Stats** | WR={rolling['win_rate']:.0%} | AvgR={rolling['avg_return']:+.2%}{sharp_str}")
    lines.append("")

    # Signal effectiveness (top 3)
    if signals_eff:
        top_signals = signals_eff[:3]
        sig_parts = [f"{s['signal']}: {s['win_rate']:.0%} ({s['trades']}t)" for s in top_signals]
        lines.append(f"📈 **Signals** | {' | '.join(sig_parts)}")
        lines.append("")

    # Calibration
    cal_line = f"🎯 **Calibration** | {calibration['verdict']}"
    if calibration.get("bins"):
        worst = max(calibration["bins"], key=lambda b: b["calibration_error"])
        cal_line += f" | worst bin: {worst['bin']} (err={worst['calibration_error']:.0%})"
    lines.append(cal_line)
    lines.append("")

    # Patterns (compact)
    if patterns:
        pat_parts = []
        tl = patterns.get("thesis_length", {})
        if tl.get("short_thesis_wr") is not None:
            pat_parts.append(f"short theses WR={tl['short_thesis_wr']:.0%}")
        if tl.get("long_thesis_wr") is not None:
            pat_parts.append(f"long theses WR={tl['long_thesis_wr']:.0%}")
        exit_r = patterns.get("exit_condition_rate", 0)
        pat_parts.append(f"exit condition rate={exit_r:.0%}")
        lines.append(f"🔍 **Patterns** | {' | '.join(pat_parts)}")
        lines.append("")

    # Drift
    if drift.get("drift_flags"):
        for flag in drift["drift_flags"][:3]:
            lines.append(f"🔄 **Drift** | {flag}")
        lines.append("")

    brief_markdown = "\n".join(lines)

    # If compact, trim > 2000 chars
    if compact and len(brief_markdown) > 2000:
        brief_markdown = brief_markdown[:1997] + "..."

    return {
        "agent": agent_id,
        "brief_markdown": brief_markdown,
        "computed_at": datetime.now().isoformat(),
        "stats": rolling,
    }


def generate_all_briefs(days: int = 14, compact: bool = False) -> Dict[str, Any]:
    """Generate briefs for all agents."""
    briefs = {}
    for agent in AGENT_IDS:
        b = generate_brief(agent, days=days, compact=compact)
        if b:
            briefs[agent] = b
    return {
        "briefs": briefs,
        "computed_at": datetime.now().isoformat(),
        "days": days,
        "compact": compact,
    }


def main():
    parser = argparse.ArgumentParser(description="Performance Brief — structured markdown stats per agent")
    parser.add_argument("--agent", type=str, help="Agent name (kairos, aldridge, stonks)")
    parser.add_argument("--all", action="store_true", help="Generate briefs for all agents")
    parser.add_argument("--days", type=int, default=14, help="Lookback days (default: 14)")
    parser.add_argument("--compact", action="store_true", help="Trim to ~2000 chars")
    args = parser.parse_args()

    if args.all:
        result = generate_all_briefs(days=args.days, compact=args.compact)
        for agent, brief in result["briefs"].items():
            print(brief["brief_markdown"])
            print("---")
    elif args.agent:
        result = generate_brief(args.agent, days=args.days, compact=args.compact)
        if result is None:
            print(f"null — fewer than {MIN_PREDICTIONS} resolved predictions for {args.agent}")
            sys.exit(0)
        if args.compact:
            print(result["brief_markdown"][:2000])
        else:
            print(result["brief_markdown"])
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()