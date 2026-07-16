#!/usr/bin/env python3
"""
7-Night Trend Card — weekly summary of variant performance trends.

Queries the last 7 nights of sweep_results and generates:
  1. Top-3 consistency: which variants appear most often in top-3 per trader
  2. Composite score trendline per trader (best nightly objective_score)
  3. Parameter drift: variants where params_hash changed over the period

Output: markdown report (stdout, file, or Canvas card)

Usage:
    python3 scripts/nightly_trend_card.py                          # stdout
    python3 scripts/nightly_trend_card.py --trader kairos          # single trader
    python3 scripts/nightly_trend_card.py --canvas                 # push to Canvas
    python3 scripts/nightly_trend_card.py --output trend.md        # write to file

Spec: specs/nightly-optimization-pipeline.md §2.2H
Issue: #205
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Path setup ───────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(PROJECT_DIR))

CANVAS_ENV = Path.home() / "canvas" / ".env"


# ═══════════════════════════════════════════════════════════════════════════════
# Data queries
# ═══════════════════════════════════════════════════════════════════════════════


def get_7_night_data(
    trader_id: Optional[str] = None,
    nights: int = 7,
) -> Dict[str, List[Dict[str, Any]]]:
    """Query sweep_results for the last N nights, grouped by trader.

    Each row: {run_date, variant_id, params_hash, objective_score, calmar,
               sortino, profit_factor, win_rate, total_return_pct}

    Returns dict: trader_id → list of rows sorted by run_date DESC, variant_id.
    """
    try:
        from src.db.unified_store import UnifiedStore

        store = UnifiedStore()

        # Get distinct run_dates (nights) for each trader within the window
        cutoff = datetime.now(timezone.utc) - timedelta(days=nights)

        trader_filter = ""
        params: tuple = ()
        if trader_id:
            trader_filter = "AND sr.trader_id = %s"
            params = (trader_id,)

        query = f"""
            SELECT
                sr.trader_id,
                sr.variant_id,
                sr.params_hash,
                sr.objective_score,
                sr.calmar,
                sr.sortino,
                sr.profit_factor,
                sr.win_rate,
                sr.total_return_pct,
                sr.created_at,
                srn.started_at AS run_date
            FROM trading.sweep_results sr
            JOIN trading.sweep_runs srn ON sr.run_id = srn.id
            WHERE srn.started_at >= NOW() - INTERVAL '{nights} days'
              {trader_filter}
            ORDER BY sr.trader_id, srn.started_at DESC, sr.objective_score DESC NULLS LAST
        """

        rows = store.query(query, params)
        store.close()

        # Group by trader
        by_trader: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_trader[row["trader_id"]].append(row)

        return dict(by_trader)
    except Exception as e:
        print(f"[trend_card] ⚠️  DB query failed: {e}", file=sys.stderr)
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# Analysis functions
# ═══════════════════════════════════════════════════════════════════════════════


def _safe_float(val: Any) -> Optional[float]:
    """Parse a value to float, returning None for non-numeric."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def compute_top3_consistency(
    data: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Which variant_ids appear most often in the top-3 across nights.

    Returns list of {variant_id, nights_in_top3, night_count, avg_score,
                      best_score, latest_params_hash} sorted by nights_in_top3 DESC.
    """
    # Group by run_date (night)
    nights: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in data:
        run_date = row.get("run_date")
        if hasattr(run_date, "strftime"):
            date_key = run_date.strftime("%Y-%m-%d")
        else:
            date_key = str(run_date)[:10]
        nights[date_key].append(row)

    # For each night, find top-3 variant_ids
    variant_top3: Dict[int, int] = defaultdict(int)  # variant_id → count
    variant_scores: Dict[int, List[float]] = defaultdict(list)
    variant_latest_hash: Dict[int, str] = {}
    variant_latest_date: Dict[int, str] = {}

    for date_key, rows in nights.items():
        # Sort by objective_score DESC
        sorted_rows = sorted(
            rows,
            key=lambda r: _safe_float(r.get("objective_score")) or -999.0,
            reverse=True,
        )
        top3_ids = {r["variant_id"] for r in sorted_rows[:3]}
        for vid in top3_ids:
            variant_top3[vid] += 1

        # Collect scores
        for r in rows:
            vid = r["variant_id"]
            score = _safe_float(r.get("objective_score"))
            if score is not None:
                variant_scores[vid].append(score)

            # Track latest params_hash
            if (vid not in variant_latest_date) or (date_key > variant_latest_date.get(vid, "")):
                variant_latest_date[vid] = date_key
                variant_latest_hash[vid] = r.get("params_hash", "unknown")

    total_nights = len(nights)
    result = []
    for vid, nights_in_top3 in variant_top3.items():
        scores = variant_scores.get(vid, [])
        avg_score = sum(scores) / len(scores) if scores else 0.0
        best_score = max(scores) if scores else 0.0
        result.append({
            "variant_id": vid,
            "nights_in_top3": nights_in_top3,
            "night_count": total_nights,
            "pct_in_top3": round(nights_in_top3 / max(total_nights, 1) * 100, 0),
            "avg_score": round(avg_score, 4),
            "best_score": round(best_score, 4),
            "latest_params_hash": variant_latest_hash.get(vid, "unknown")[:12],
        })

    result.sort(key=lambda x: x["nights_in_top3"], reverse=True)
    return result


def compute_score_trendline(
    data: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Compute the best nightly objective_score trend.

    Returns list of {date, best_score, best_variant_id, run_count} sorted by date.
    """
    nights: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "best_score": -999.0,
        "best_variant_id": None,
        "count": 0,
    })

    for row in data:
        run_date = row.get("run_date")
        if hasattr(run_date, "strftime"):
            date_key = run_date.strftime("%Y-%m-%d")
        else:
            date_key = str(run_date)[:10]

        nights[date_key]["count"] += 1
        score = _safe_float(row.get("objective_score")) or -999.0
        if score > nights[date_key]["best_score"]:
            nights[date_key]["best_score"] = score
            nights[date_key]["best_variant_id"] = row["variant_id"]

    result = []
    for date_key in sorted(nights.keys()):
        n = nights[date_key]
        result.append({
            "date": date_key,
            "best_score": round(n["best_score"], 4),
            "best_variant_id": n["best_variant_id"],
            "run_count": n["count"],
        })

    return result


def _sparkline(values: List[float], width: int = 20) -> str:
    """Generate an ASCII sparkline from a list of values."""
    if not values:
        return "(no data)"

    lo, hi = min(values), max(values)
    if hi == lo:
        # All same — draw flat line
        return "─" * width

    chars = "▁▂▃▄▅▆▇█"
    norm = [(v - lo) / (hi - lo) for v in values]
    # Map to character indices
    indices = [min(int(n * (len(chars) - 1)), len(chars) - 1) for n in norm]

    # Distribute across width
    if len(values) >= width:
        step = len(values) / width
        sampled = [indices[int(i * step)] for i in range(width)]
    else:
        # Repeat to fill
        sampled = []
        for i in range(width):
            idx = int(i * len(values) / width)
            sampled.append(indices[idx])

    return "".join(chars[i] for i in sampled)


def _trend_arrow(values: List[float]) -> str:
    """Return a trend direction indicator."""
    if len(values) < 2:
        return "➖"
    first_half = sum(values[: len(values) // 2]) / max(len(values) // 2, 1)
    second_half = sum(values[len(values) // 2 :]) / max(len(values) - len(values) // 2, 1)
    diff_pct = (second_half - first_half) / max(abs(first_half), 0.0001) * 100
    if diff_pct > 5:
        return "🟢 ↗"
    elif diff_pct > 0:
        return "🟡 ↗"
    elif diff_pct > -5:
        return "🟡 ↘"
    else:
        return "🔴 ↘"


def compute_param_drift(
    data: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Detect parameter drift: variants whose params_hash changed across nights.

    A variant is considered "drifting" if it appeared in multiple nights with
    different params_hash values (indicating the prompt/strategy was modified).

    Returns list of {variant_id, hash_count, hashes: [str], nights_present, drift}
        sorted by hash_count DESC (more hashes = more drift).
    """
    # variant_id → set of (params_hash, date)
    variant_hashes: Dict[int, Dict[str, set]] = defaultdict(lambda: {"hashes": set(), "nights": set()})

    for row in data:
        vid = row["variant_id"]
        variant_hashes[vid]["hashes"].add(row.get("params_hash", "unknown"))
        run_date = row.get("run_date")
        if hasattr(run_date, "strftime"):
            date_key = run_date.strftime("%Y-%m-%d")
        else:
            date_key = str(run_date)[:10]
        variant_hashes[vid]["nights"].add(date_key)

    result = []
    for vid, info in variant_hashes.items():
        hash_count = len(info["hashes"])
        nights_present = len(info["nights"])
        result.append({
            "variant_id": vid,
            "hash_count": hash_count,
            "hashes": sorted(info["hashes"]),
            "nights_present": nights_present,
            "drift": hash_count > 1,
        })

    result.sort(key=lambda x: x["hash_count"], reverse=True)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Report generation
# ═══════════════════════════════════════════════════════════════════════════════

TRADER_LABELS = {
    "kairos": "Kairos 🔮",
    "aldridge": "Aldridge 📊",
    "stonks": "Stonks 🚀",
}


def generate_markdown(
    data: Dict[str, List[Dict[str, Any]]],
    title: Optional[str] = None,
) -> str:
    """Generate a full markdown report from 7-night data.

    Args:
        data: trader_id → list of sweep result rows
        title: Optional report title override

    Returns:
        Markdown string
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    title = title or f"## 📈 7-Night Trend Card — {date_str}"

    lines = [
        title,
        "",
        f"**Generated:** {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Window:** Last 7 nights",
        f"**Traders:** {len(data)}",
        "",
    ]

    if not data:
        lines.append("⚠️ **No sweep data found for the last 7 nights.**")
        lines.append("")
        lines.append("Make sure the nightly pipeline has run at least once in the last week.")
        return "\n".join(lines)

    for trader_id in sorted(data.keys()):
        trader_rows = data[trader_id]
        label = TRADER_LABELS.get(trader_id, trader_id)

        lines.append(f"### {label}")
        lines.append("")

        # ── Top-3 Consistency ──
        top3 = compute_top3_consistency(trader_rows)
        if top3:
            lines.append("#### 🏆 Top-3 Consistency")
            lines.append("")
            lines.append("| Variant | Top-3 Nights | Pct | Avg Score | Best Score | Latest Hash |")
            lines.append("|---------|-------------|-----|-----------|------------|-------------|")
            for item in top3[:8]:  # Top 8 most consistent
                lines.append(
                    f"| {item['variant_id']} | "
                    f"{item['nights_in_top3']}/{item['night_count']} | "
                    f"{item['pct_in_top3']:.0f}% | "
                    f"{item['avg_score']:.4f} | "
                    f"{item['best_score']:.4f} | "
                    f"`{item['latest_params_hash']}` |"
                )
            lines.append("")

        # ── Score Trendline ──
        trend = compute_score_trendline(trader_rows)
        if trend:
            scores = [t["best_score"] for t in trend]
            spark = _sparkline(scores)
            arrow = _trend_arrow(scores)

            lines.append("#### 📊 Score Trendline")
            lines.append("")
            lines.append(f"**Trend:** {arrow} &nbsp; `{spark}`")
            lines.append("")
            lines.append("| Date | Best Score | Best Variant | Variants Run |")
            lines.append("|------|-----------|--------------|-------------|")
            for t in trend:
                lines.append(
                    f"| {t['date']} | "
                    f"{t['best_score']:.4f} | "
                    f"{t['best_variant_id']} | "
                    f"{t['run_count']} |"
                )
            lines.append("")

        # ── Parameter Drift ──
        drift = compute_param_drift(trader_rows)
        drifting = [d for d in drift if d["drift"]]
        if drifting:
            lines.append("#### 🔄 Parameter Drift")
            lines.append("")
            lines.append("Variants with evolving parameters (multiple `params_hash` values):")
            lines.append("")
            lines.append("| Variant | Nights | Hash Changes | Hashes |")
            lines.append("|---------|--------|-------------|--------|")
            for d in drifting[:10]:
                hash_preview = ", ".join(f"`{h[:10]}`" for h in d["hashes"][:5])
                if len(d["hashes"]) > 5:
                    hash_preview += f" (+{len(d['hashes']) - 5} more)"
                lines.append(
                    f"| {d['variant_id']} | "
                    f"{d['nights_present']} | "
                    f"{d['hash_count']} | "
                    f"{hash_preview} |"
                )
            lines.append("")
        else:
            lines.append("#### 🔄 Parameter Drift")
            lines.append("")
            lines.append("✅ No parameter drift detected — all variants are stable.")
            lines.append("")

        lines.append("---")
        lines.append("")

    # ── Cross-Trader Summary ──
    lines.append("### 🌐 Cross-Trader Summary")
    lines.append("")
    lines.append("| Trader | Nights | Top Variant | Best Score | Score Trend | Drifting? |")
    lines.append("|--------|--------|-------------|------------|-------------|-----------|")

    for trader_id in sorted(data.keys()):
        trader_rows = data[trader_id]
        top3 = compute_top3_consistency(trader_rows)
        trend = compute_score_trendline(trader_rows)
        drift = compute_param_drift(trader_rows)

        nights = len(set(
            (r.get("run_date").strftime("%Y-%m-%d") if hasattr(r.get("run_date"), "strftime") else str(r.get("run_date", ""))[:10])
            for r in trader_rows
        ))
        top_variant = top3[0]["variant_id"] if top3 else "—"
        best_score = top3[0]["best_score"] if top3 else 0.0

        scores = [t["best_score"] for t in trend]
        spark = _sparkline(scores, width=10) if scores else "—"
        has_drift = any(d["drift"] for d in drift)

        lines.append(
            f"| {TRADER_LABELS.get(trader_id, trader_id)} | "
            f"{nights} | "
            f"{top_variant} | "
            f"{best_score:.4f} | "
            f"`{spark}` | "
            f"{'⚠️' if has_drift else '✅'} |"
        )

    lines.append("")
    lines.append("---")
    lines.append(f"*Generated by nightly_trend_card.py — see #205*")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Canvas integration
# ═══════════════════════════════════════════════════════════════════════════════


def push_to_canvas(content: str, dry_run: bool = False) -> Optional[str]:
    """Push the trend card to Canvas.

    Args:
        content: Markdown content to push.
        dry_run: If True, print content without pushing.

    Returns:
        Card UUID if pushed, None otherwise.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if dry_run:
        print("\n[trend_card] Canvas card content (dry-run):")
        print(content)
        return None

    try:
        from src.canvas_dashboard import _push_to_canvas

        result = _push_to_canvas(
            title=f"📈 7-Night Trend Card — {date_str}",
            content=content,
            board="main",
            agent="orchestrator",
            emoji="📈",
            expires_days=7,
        )
        card_uuid = result.get("id")
        if card_uuid:
            print(f"[trend_card] ✅ Canvas card pushed: {card_uuid}")
        else:
            print(f"[trend_card] ✅ Canvas card pushed (no UUID in response)")
        return card_uuid
    except Exception as e:
        print(f"[trend_card] ⚠️  Failed to push canvas card: {e}", file=sys.stderr)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="7-Night Trend Card — weekly variant performance summary",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--trader", type=str, default=None,
        help="Single trader ID (kairos, aldridge, stonks). Omit for all.",
    )
    parser.add_argument(
        "--nights", type=int, default=7,
        help="Number of nights to analyze (default: 7)",
    )
    parser.add_argument(
        "--canvas", action="store_true",
        help="Push result to Canvas instead of stdout",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Write report to file",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview Canvas card without pushing",
    )
    args = parser.parse_args()

    # Fetch data
    data = get_7_night_data(trader_id=args.trader, nights=args.nights)

    if not data:
        print("[trend_card] ⚠️  No sweep data found for the specified window.")
        sys.exit(1)

    # Generate report
    title = None
    if args.trader:
        label = TRADER_LABELS.get(args.trader, args.trader)
        title = f"## 📈 7-Night Trend Card — {label}"

    content = generate_markdown(data, title=title)

    # Output
    if args.canvas or args.dry_run:
        push_to_canvas(content, dry_run=args.dry_run)
    elif args.output:
        output_path = Path(args.output)
        output_path.write_text(content)
        print(f"[trend_card] ✅ Report written to {output_path}")
    else:
        print(content)


if __name__ == "__main__":
    main()
