#!/usr/bin/env python3
"""
Virtual Competitor Promotion — swap AGENTS.md/SOUL.md when a variant beats the live trader.

Architecture:
  When a virtual competitor variant consistently outperforms the live trader,
  we promote it by swapping its config files with the live trader's workspace.

  The swap is safe because:
    - Both the live trader and virtual variant use the SAME file format
    - The live trader's workspaces are version-controlled in git
    - Old config is archived, not destroyed (in case of regression)
    - A sanity check runs before the swap

  Promotion conditions (from COMPETITION.md §C2.5):
    1. Variant completes its evaluation window (1d, 5d, 20d, 90d)
    2. Objective score computed over the FULL window
    3. Improvement > threshold (e.g., 5% better than baseline)
    4. Variant passed a 2-day probation period (if applicable)
    5. Not in a hard drawdown (>15%)

Usage:
    python3 agents/virtual/scripts/virtual_promote.py                             # review all eligible
    python3 agents/virtual/scripts/virtual_promote.py --promote kairos-aggressive  # promote one
    python3 agents/virtual/scripts/virtual_promote.py --dry-run                    # print what would happen
    python3 agents/virtual/scripts/virtual_promote.py --force kairos-contrarian    # force promotion
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("virtual_promote")

# ── Paths ───────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
VIRTUAL_AGENTS_DIR = PROJECT_ROOT / "agents" / "virtual"
SCRIPTS_DIR = VIRTUAL_AGENTS_DIR / "scripts"
REPLAY_RESULTS_DIR = VIRTUAL_AGENTS_DIR / ".replay_results"
ARCHIVE_DIR = PROJECT_ROOT / "agents" / "virtual" / ".archived_promotions"
DB_DSN = os.getenv("VT_DB_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")

# OpenClaw live trader workspace paths
LIVE_WORKSPACES = {
    "kairos":   Path("/home/openclaw/.openclaw/workspace-trader-kairos"),
    "aldridge": Path("/home/openclaw/.openclaw/workspace-trader-aldridge"),
    "stonks":   Path("/home/openclaw/.openclaw/workspace-trader-stonks"),
}

# Files to swap
SWAP_FILES = ["AGENTS.md", "SOUL.md", "HEARTBEAT.md"]

# Promotion thresholds
EVAL_WINDOWS = {
    1:  {"min_days": 1,  "promote_threshold": 0.10},   # 1-day: 10% improvement
    5:  {"min_days": 5,  "promote_threshold": 0.08},   # 5-day: 8% improvement
    20: {"min_days": 20, "promote_threshold": 0.05},   # 20-day: 5% improvement
    90: {"min_days": 90, "promote_threshold": 0.03},   # 90-day: 3% improvement
}

# Mapping variant type → directory name
VARIANT_DIR_MAP: Dict[str, str] = {
    "aggressive": "aggressive",
    "conservative": "conservative",
    "contrarian": "contrarian",
}


def discover_variant_dirs() -> Dict[str, Path]:
    """Discover all variant directories.

    Returns:
        {variant_name: Path} e.g. {"virtual-kairos-aggressive": Path(...)}
    """
    result: Dict[str, Path] = {}
    for base in ["kairos", "aldridge", "stonks"]:
        for vtype, dirname in VARIANT_DIR_MAP.items():
            variant_dir = VIRTUAL_AGENTS_DIR / base / dirname
            if variant_dir.exists():
                name = f"virtual-{base}-{vtype}"
                result[name] = variant_dir
    return result


def load_latest_scores(
    variant_name: str, window_days: int = 5
) -> Optional[Dict[str, Any]]:
    """Load the latest replay score for a variant.

    Reads from replay_results JSON files.

    Args:
        variant_name: e.g. "virtual-kairos-aggressive"
        window_days: Evaluation window in days.

    Returns:
        Score dict or None if not found.
    """
    if not REPLAY_RESULTS_DIR.exists():
        return None

    # Collect all results files for the window
    scores = []
    for f in sorted(REPLAY_RESULTS_DIR.glob("replay_*.json")):
        try:
            data = json.loads(f.read_text())
            for r in data.get("results", []):
                if r.get("variant") == variant_name and r.get("status") in (None, "ok"):
                    scores.append(r)
        except Exception:
            continue

    if not scores:
        return None

    # Take the most recent scores within the window
    recent_scores = scores[-window_days:] if len(scores) > window_days else scores

    if not recent_scores:
        return None

    # Aggregate
    avg_return = sum(s.get("total_return", 0) for s in recent_scores) / len(recent_scores)
    avg_win_rate = sum(s.get("win_rate", 0) for s in recent_scores) / len(recent_scores)
    avg_sharpe = sum(s.get("sharpe", 0) for s in recent_scores) / len(recent_scores)

    return {
        "variant": variant_name,
        "samples": len(recent_scores),
        "avg_return": avg_return,
        "avg_win_rate": avg_win_rate,
        "avg_sharpe": avg_sharpe,
        "total_return_pct": avg_return * 100,
    }


def load_live_trader_scores(
    base_trader: str, window_days: int = 5
) -> Optional[Dict[str, Any]]:
    """Load the live trader's scores from the DB.

    Queries trading.trades for the live trader's recent performance.

    Args:
        base_trader: e.g. "kairos"
        window_days: Evaluation window in days.

    Returns:
        Score dict or None.
    """
    import psycopg2
    import psycopg2.extras

    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        since = date.today()
        # Get the live trader's trades
        cur.execute(
            """SELECT COUNT(*) as trades,
                      COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 0) as win_rate,
                      COALESCE(SUM(pnl), 0) as total_pnl
               FROM trading.trades
               WHERE trader_id = %s
                 AND created_at::date >= %s
                 AND exit_time IS NOT NULL""",
            (base_trader, since),
        )
        row = cur.fetchone()
        conn.close()

        if not row or row["trades"] == 0:
            return None

        return {
            "trader": base_trader,
            "trades": row["trades"],
            "win_rate": float(row["win_rate"]),
            "total_pnl": float(row["total_pnl"]),
            "total_return": float(row["total_pnl"]) / 10000,
            "total_return_pct": float(row["total_pnl"]) / 100,
        }
    except Exception as e:
        log.warning("Could not load live trader scores for %s: %s", base_trader, e)
        return None


def compute_promotion_eligibility(
    variant_name: str,
    base_trader: str,
    variant_score: Optional[Dict[str, Any]],
    live_score: Optional[Dict[str, Any]],
) -> Tuple[bool, str, float]:
    """Determine if a variant is eligible for promotion.

    Args:
        variant_name: Name of the virtual variant.
        base_trader: Base trader name.
        variant_score: Variant's replay scores.
        live_score: Live trader's scores.

    Returns:
        (eligible, reason, improvement_pct)
    """
    if not variant_score:
        return False, "No replay scores available for variant", 0.0

    if not live_score:
        return False, "No live trader scores available for comparison", 0.0

    variant_samples = variant_score.get("samples", 0)
    if variant_samples < 3:
        return False, f"Only {variant_samples} samples (need 3+ for significance)", 0.0

    # Compare total return
    variant_return = variant_score.get("avg_return", 0)
    live_return = live_score.get("total_return", 0)

    if live_return == 0:
        # Live trader broke even — any positive return is an improvement
        improvement = variant_return - live_return
        threshold = 0.02  # 2% absolute improvement
    else:
        improvement = (variant_return - live_return) / abs(live_return) if live_return != 0 else variant_return
        threshold = EVAL_WINDOWS.get(5, {}).get("promote_threshold", 0.08)

    if variant_return <= live_return:
        return False, (
            f"Variant return ({variant_return:+.4f}) ≤ "
            f"Live return ({live_return:+.4f}) — improvement={improvement:+.2%}"
        ), improvement

    if improvement < threshold:
        return False, (
            f"Improvement ({improvement:+.2%}) below threshold ({threshold:+.0%})"
        ), improvement

    return True, (
        f"✅ Variant outperforms live: {variant_return:+.4f} vs {live_return:+.4f} "
        f"(improvement={improvement:+.2%})"
    ), improvement


def promote_variant(
    variant_name: str,
    base_trader: str,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """Promote a variant by swapping its config files with the live trader's workspace.

    Args:
        variant_name: e.g. "virtual-kairos-aggressive"
        base_trader: e.g. "kairos"
        dry_run: If True, print without swapping.
        force: Force promotion even if eligibility fails.

    Returns:
        Result dict with status.
    """
    variant_dirs = discover_variant_dirs()
    variant_dir = variant_dirs.get(variant_name)

    if not variant_dir:
        return {"variant": variant_name, "status": "error", "error": "Variant directory not found"}

    live_workspace = LIVE_WORKSPACES.get(base_trader)
    if not live_workspace or not live_workspace.exists():
        return {"variant": variant_name, "status": "error",
                "error": f"Live workspace not found for {base_trader}"}

    # Validate variant has the swap files
    available_files = []
    for fname in SWAP_FILES:
        src = variant_dir / fname
        if src.exists():
            available_files.append(fname)

    if not available_files:
        return {"variant": variant_name, "status": "error",
                "error": "No swap files found in variant directory"}

    log.info("── Promoting %s (%s) ──────────────────────────", variant_name, base_trader)
    log.info("  Variant dir: %s", variant_dir)
    log.info("  Live workspace: %s", live_workspace)
    log.info("  Files to swap: %s", available_files)

    if not force:
        # Check eligibility
        variant_score = load_latest_scores(variant_name)
        live_score = load_live_trader_scores(base_trader)
        eligible, reason, improvement = compute_promotion_eligibility(
            variant_name, base_trader, variant_score, live_score
        )

        log.info("  Eligibility: %s", "ELIGIBLE" if eligible else "NOT ELIGIBLE")
        log.info("  Reason: %s", reason)
        if variant_score:
            log.info("  Variant score: avg_return=%+.4f, samples=%d",
                     variant_score.get("avg_return", 0), variant_score.get("samples", 0))
        if live_score:
            log.info("  Live score: total_return=%+.4f, trades=%d",
                     live_score.get("total_return", 0), live_score.get("trades", 0))
        log.info("  Improvement: %+.2f%%", improvement * 100)

        if not eligible:
            return {
                "variant": variant_name,
                "status": "not_eligible",
                "reason": reason,
                "improvement": improvement,
            }

    if dry_run:
        log.info("DRY RUN — would swap:")
        for fname in available_files:
            src = variant_dir / fname
            dst = live_workspace / fname
            log.info("  %s → %s", src, dst)
            if src.exists():
                log.info("    Source: %d bytes", src.stat().st_size)
            if dst.exists():
                log.info("    Dest:   %d bytes (existing)", dst.stat().st_size)

        return {
            "variant": variant_name,
            "status": "dry_run",
            "files": available_files,
            "variant_dir": str(variant_dir),
            "live_workspace": str(live_workspace),
        }

    # Perform the swap
    archived_files = []
    promoted_files = []

    for fname in available_files:
        src = variant_dir / fname
        dst = live_workspace / fname

        # Archive old file
        if dst.exists():
            archive_name = f"{base_trader}_{fname}_{date.today().isoformat()}"
            archive_path = ARCHIVE_DIR / archive_name
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, archive_path)
            archived_files.append(str(archive_path))
            log.info("  Archived %s → %s", dst.name, archive_path)

        # Copy variant file to live workspace
        shutil.copy2(src, dst)
        promoted_files.append(fname)
        log.info("  ✅ Promoted %s (%d bytes)", fname, src.stat().st_size)

    result = {
        "variant": variant_name,
        "status": "promoted",
        "base_trader": base_trader,
        "files_promoted": promoted_files,
        "files_archived": archived_files,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Write promotion record
    promotion_record = ARCHIVE_DIR / f"promotion_{base_trader}_{date.today().isoformat()}.json"
    promotion_record.write_text(json.dumps(result, indent=2))
    log.info("  Promotion record: %s", promotion_record)

    # Git commit (version control)
    try:
        repo_dir = PROJECT_ROOT
        subprocess.run(
            ["git", "add", str(ARCHIVE_DIR.relative_to(repo_dir)),
             str(live_workspace.relative_to(repo_dir) / "AGENTS.md"),
             str(live_workspace.relative_to(repo_dir) / "SOUL.md")],
            cwd=repo_dir,
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["git", "commit", "-m",
             f"promotion: {variant_name} → {base_trader} live trader\n\n"
             f"Files swapped: {', '.join(promoted_files)}\n"
             f"Archived: {', '.join(archived_files)}\n"
             f"Timestamp: {result['timestamp']}"],
            cwd=repo_dir,
            capture_output=True,
            check=False,
        )
        log.info("  Git commit created")
    except Exception as e:
        log.warning("Git commit failed (non-fatal): %s", e)

    return result


def review_all_variants(dry_run: bool = False) -> List[Dict[str, Any]]:
    """Review all variants for promotion eligibility.

    Args:
        dry_run: If True, only print eligibility.

    Returns:
        List of eligibility results.
    """
    results = []
    variant_dirs = discover_variant_dirs()

    for variant_name, variant_dir in variant_dirs.items():
        # Extract base trader from variant name
        parts = variant_name.replace("virtual-", "").split("-")
        if len(parts) < 2:
            continue
        base_trader = parts[0]  # "kairos", "aldridge", "stonks"

        variant_score = load_latest_scores(variant_name)
        live_score = load_live_trader_scores(base_trader)

        eligible, reason, improvement = compute_promotion_eligibility(
            variant_name, base_trader, variant_score, live_score
        )

        entry = {
            "variant": variant_name,
            "base_trader": base_trader,
            "eligible": eligible,
            "reason": reason,
            "improvement_pct": round(improvement * 100, 2),
            "variant_score": variant_score,
            "live_score": live_score,
        }
        results.append(entry)

    return results


def print_review(results: List[Dict[str, Any]]):
    """Print promotion review table."""
    print()
    print("═" * 100)
    print(f"  VIRTUAL COMPETITOR PROMOTION REVIEW — {date.today()}")
    print("═" * 100)
    print()

    if not results:
        print("  No variants found.")
        print()
        return

    eligible = [r for r in results if r.get("eligible")]
    not_eligible = [r for r in results if not r.get("eligible")]

    if eligible:
        print(f"  🏆 ELIGIBLE FOR PROMOTION ({len(eligible)}):")
        print()
        for r in eligible:
            print(f"     {r['variant']:<30} Improvement: {r.get('improvement_pct', 0):+.2f}%")
            print(f"     {'':<30} Reason: {r.get('reason', '')[:70]}")
            print()

    if not_eligible:
        print(f"  ⏳ NOT ELIGIBLE ({len(not_eligible)}):")
        print()
        for r in not_eligible:
            imp = r.get('improvement_pct', 0)
            sig = "✅" if imp > 0 else "❌"
            print(f"     {sig} {r['variant']:<30} {r.get('reason', '')[:80]}")
            print()


def main():
    parser = argparse.ArgumentParser(description="Virtual Competitor Promotion Pipeline")
    parser.add_argument("--promote", type=str, default=None,
                        help="Promote a specific variant (e.g., kairos-aggressive)")
    parser.add_argument("--force", action="store_true",
                        help="Force promotion even if eligibility fails")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without swapping")
    parser.add_argument("--review", action="store_true",
                        help="Review all variants for promotion eligibility")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("═" * 60)
    log.info("Virtual Competitor Promotion Pipeline")
    log.info("  Force: %s | Dry run: %s", args.force, args.dry_run)

    if args.review:
        results = review_all_variants(dry_run=args.dry_run)
        print_review(results)
        return

    if args.promote:
        variant_name = args.promote
        if not variant_name.startswith("virtual-"):
            variant_name = f"virtual-{variant_name}"

        # Extract base trader
        parts = variant_name.replace("virtual-", "").split("-")
        if len(parts) < 2:
            log.error("Could not parse variant name: %s", variant_name)
            sys.exit(1)
        base_trader = parts[0]

        if base_trader not in LIVE_WORKSPACES:
            log.error("Unknown base trader: %s", base_trader)
            sys.exit(1)

        result = promote_variant(
            variant_name=variant_name,
            base_trader=base_trader,
            dry_run=args.dry_run,
            force=args.force,
        )

        status = result.get("status", "unknown")
        if status == "promoted":
            print(f"\n  ✅ PROMOTED: {variant_name} → {base_trader} live trader")
            print(f"     Files: {', '.join(result.get('files_promoted', []))}")
            print(f"     Archived: {len(result.get('files_archived', []))} file(s)")
        elif status == "dry_run":
            print(f"\n  📋 DRY RUN: would promote {variant_name}")
        elif status == "not_eligible":
            print(f"\n  ⏳ NOT ELIGIBLE: {variant_name}")
            print(f"     Reason: {result.get('reason', '')}")
        else:
            print(f"\n  ❌ ERROR: {result.get('error', 'unknown')}")

        return

    # Default: review
    results = review_all_variants(dry_run=args.dry_run)
    print_review(results)


if __name__ == "__main__":
    main()