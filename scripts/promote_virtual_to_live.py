#!/usr/bin/env python3
"""
promote_virtual_to_live.py — Full promotion mechanism: compare a virtual
trader's performance against its live counterpart, then swap configs.

Flow:
  1. Load virtual trader + live base trader records from DB
  2. Compute performance scores (P&L sum or Sharpe) over configurable window
  3. If virtual outperforms live by >= threshold, promote:
     a. Archive live trader's config files
     b. Copy virtual's config overrides → live trader's config files
     c. Demote ex-live trader to a new virtual with status='probation'
     d. Log to trading.promotion_log
  4. --rollback reverses the last promotion

MEMORY.md files are intentionally excluded from config swaps — traders don't
notice they've been swapped.

Usage:
    # Promote a specific virtual trader against its base live trader
    python3 scripts/promote_virtual_to_live.py --name kairos-tighter-0712 --base kairos

    # Preview without making changes
    python3 scripts/promote_virtual_to_live.py --name kairos-tighter-0712 --base kairos --dry-run

    # Force promotion (skip score check)
    python3 scripts/promote_virtual_to_live.py --name kairos-tighter-0712 --base kairos --force

    # Rollback last promotion
    python3 scripts/promote_virtual_to_live.py --rollback --name kairos-tighter-0712

    # Custom scoring window and threshold
    python3 scripts/promote_virtual_to_live.py --name kairos-tighter-0712 --base kairos \\
        --window 14 --threshold 15.0 --metric sharpe
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import yaml

log = logging.getLogger("promote_virtual_to_live")

# ── Defaults ──────────────────────────────────────────────────────────────

DB_DSN = os.getenv("PROMOTE_LIVE_DB_URL", "postgresql://trader:@trading-db:5432/trading")
PROJECT_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = PROJECT_DIR / "agents"
ARCHIVE_DIR = PROJECT_DIR / "archive" / "trader_configs"
LIVE_TRADER_PREFIX = "trader-"  # e.g. trader-kairos

# Files excluded from config swaps (traders shouldn't notice the swap)
SWAP_EXCLUDE_FILES: set = {"MEMORY.md", "HEARTBEAT.md"}

# Default scoring
DEFAULT_WINDOW_DAYS = 7
DEFAULT_THRESHOLD_PCT = 10.0
DEFAULT_METRIC = "pnl"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Promote a virtual trader to live, demoting the current live trader"
    )
    p.add_argument(
        "--name", type=str, required=True,
        help="Virtual trader name to promote (from trading.virtual_traders)",
    )
    p.add_argument(
        "--base", type=str, default=None,
        help="Live base trader name (e.g. kairos). Defaults to virtual's base_trader field.",
    )
    p.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW_DAYS,
        help=f"Scoring window in days (default: {DEFAULT_WINDOW_DAYS})",
    )
    p.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD_PCT,
        help=f"Improvement threshold %% (default: {DEFAULT_THRESHOLD_PCT})",
    )
    p.add_argument(
        "--metric", choices=["pnl", "sharpe"], default=DEFAULT_METRIC,
        help=f"Scoring metric (default: {DEFAULT_METRIC})",
    )
    p.add_argument("--force", action="store_true", help="Skip score check, force promotion")
    p.add_argument(
        "--rollback", action="store_true",
        help="Reverse the last promotion for this virtual trader",
    )
    p.add_argument("--dry-run", action="store_true", help="Preview mode, no changes")
    p.add_argument("--db-dsn", default=DB_DSN, help="Postgres DSN")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return p.parse_args(argv)


# ══════════════════════════════════════════════════════════════════════════
# DB helpers
# ══════════════════════════════════════════════════════════════════════════


def ensure_tables(conn) -> None:
    """Create required tables if they don't exist."""
    cur = conn.cursor()
    # promotion_log
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trading.promotion_log (
            id SERIAL PRIMARY KEY,
            virtual_name TEXT NOT NULL,
            base_trader TEXT NOT NULL,
            live_trader_before TEXT NOT NULL,
            virtual_score REAL,
            live_score REAL,
            metric TEXT DEFAULT 'pnl',
            threshold REAL DEFAULT 10.0,
            improvement_pct REAL,
            was_rolled_back BOOLEAN DEFAULT FALSE,
            rollback_at TIMESTAMPTZ,
            notes TEXT,
            promoted_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # virtual_traders
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trading.virtual_traders (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            base_trader TEXT NOT NULL DEFAULT 'kairos',
            variant_type TEXT NOT NULL DEFAULT 'manual',
            variant_id INTEGER,
            params JSONB DEFAULT '{}'::jsonb,
            status TEXT DEFAULT 'probation',
            score REAL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            activated_at TIMESTAMPTZ,
            notes TEXT
        )
    """)
    conn.commit()
    cur.close()


def fetch_virtual_trader(conn, name: str) -> Optional[Dict[str, Any]]:
    """Fetch a virtual trader record from trading.virtual_traders by name."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM trading.virtual_traders WHERE name = %s",
        (name,),
    )
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def fetch_latest_promotion_log(
    conn, virtual_name: str
) -> Optional[Dict[str, Any]]:
    """Fetch the most recent promotion log entry for a virtual trader."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT * FROM trading.promotion_log
           WHERE virtual_name = %s AND was_rolled_back = FALSE
           ORDER BY promoted_at DESC LIMIT 1""",
        (virtual_name,),
    )
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def fetch_pnl_data(
    conn, agent_id: str, window_days: int
) -> List[Dict[str, Any]]:
    """Fetch daily P&L data for an agent over the scoring window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).date()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT date, pnl, pnl_pct, start_equity, end_equity, trades_count,
                  win_count, loss_count
           FROM trading.daily_pnl
           WHERE agent_id = %s AND date >= %s
           ORDER BY date ASC""",
        (agent_id, cutoff),
    )
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


def fetch_trader_id_from_virtual(conn, virtual_name: str) -> Optional[str]:
    """Map a virtual trader name to its trader_id in the trading system.

    Virtual traders typically log their trades with trade_source='virtual'
    and trader_id as the virtual name. Daily PnL uses the same agent_id.
    """
    # The virtual trader's name is its agent_id in daily_pnl
    return virtual_name


def compute_pnl_score(
    pnl_rows: List[Dict[str, Any]], metric: str
) -> float:
    """Compute performance score from daily P&L data.

    For 'pnl': sum of daily P&L.
    For 'sharpe': annualised Sharpe from daily returns (risk-free = 0).
    Returns 0.0 if insufficient data.
    """
    if not pnl_rows:
        return 0.0

    if metric == "pnl":
        return sum(r.get("pnl", 0.0) or 0.0 for r in pnl_rows)

    if metric == "sharpe":
        # Compute daily returns
        daily_returns = []
        for r in pnl_rows:
            pnl_pct = r.get("pnl_pct", 0.0) or 0.0
            daily_returns.append(pnl_pct)

        if len(daily_returns) < 2:
            return 0.0

        import numpy as np
        returns_arr = np.array(daily_returns, dtype=float)
        mean_ret = float(np.mean(returns_arr))
        std_ret = float(np.std(returns_arr, ddof=1))
        if std_ret == 0:
            return 0.0
        # Annualised Sharpe (252 trading days)
        sharpe = (mean_ret / std_ret) * np.sqrt(252.0)
        return sharpe

    return 0.0


# ══════════════════════════════════════════════════════════════════════════
# Config file management
# ══════════════════════════════════════════════════════════════════════════


def live_trader_dir(trader_name: str) -> Path:
    """Get the filesystem directory for a live trader."""
    return AGENTS_DIR / f"{LIVE_TRADER_PREFIX}{trader_name}"


def archive_dir_for(trader_name: str, timestamp: str) -> Path:
    """Get the archive directory path for a given backup."""
    return ARCHIVE_DIR / f"{trader_name}-{timestamp}"


def read_trader_config(trader_dir: Path) -> Optional[Dict[str, Any]]:
    """Read a trader's config.yaml file and return parsed contents."""
    config_path = trader_dir / "config.yaml"
    if not config_path.exists():
        return None
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def write_trader_config(trader_dir: Path, config: Dict[str, Any]) -> None:
    """Write a trader's config.yaml file."""
    config_path = trader_dir / "config.yaml"
    trader_dir.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def read_file(path: Path) -> str:
    """Read file contents as string."""
    if path.exists():
        return path.read_text()
    return ""


def write_file(path: Path, content: str) -> None:
    """Write string content to a file, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def archive_trader_config(
    trader_name: str, trader_dir: Path, timestamp: str
) -> Path:
    """Archive a live trader's entire config directory.

    Copies all files (including MEMORY.md — the archive keeps everything)
    to a timestamped directory under archive/.

    Returns the archive path.
    """
    archive_path = archive_dir_for(trader_name, timestamp)
    if archive_path.exists():
        log.warning("Archive dir already exists, overwriting: %s", archive_path)
        shutil.rmtree(archive_path)

    shutil.copytree(trader_dir, archive_path)
    log.info("Archived %s → %s (%d files)", trader_dir, archive_path,
             len(list(archive_path.rglob("*"))))
    return archive_path


def apply_virtual_overrides(
    trader_dir: Path,
    params: Dict[str, Any],
    dry_run: bool = False,
) -> None:
    """Apply virtual trader's config overrides to the live trader directory.

    Specifically:
      1. Update config.yaml with merged params (base config overlaid with
         variant params from the sweep/promotion).
      2. If the virtual has a prompt.txt, replace the live one.
      3. Copy AGENTS.md, SOUL.md, and skills/ from virtual if present.
      4. MEMORY.md and HEARTBEAT.md are NEVER copied — excluded from swap.

    The virtual trader typically stores its config overrides in the
    params JSONB column. For sweep-derived virtuals, these are the
    signal engine parameter changes (e.g. stop_loss_pct, momentum_weight).

    For virtuals with their own filesystem directory (uncommon for
    sweep variants), we check for a corresponding agents/trader-<name>/
    directory and copy files from there.
    """
    virtual_dir = live_trader_dir(params.get("name", "")) if params.get("name") else None
    if virtual_dir and virtual_dir.exists() and virtual_dir != trader_dir:
        # This virtual has its own filesystem directory — copy relevant files
        for fname in os.listdir(virtual_dir):
            if fname in SWAP_EXCLUDE_FILES:
                log.debug("Excluding %s from swap (traders shouldn't notice)", fname)
                continue
            src = virtual_dir / fname
            if src.is_file():
                dst = trader_dir / fname
                if not dry_run:
                    write_file(dst, read_file(src))
                    log.debug("Copied %s → %s", fname, dst)
            elif src.is_dir():
                dst = trader_dir / fname
                if not dry_run:
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                    log.debug("Copied dir %s → %s", fname, dst)

    # Update config.yaml with merged params
    current_config = read_trader_config(trader_dir) or {}
    # Apply overrides from params (sweep variants store signal changes here)
    for key, val in params.items():
        if key in ("name", "base_trader", "variant_type", "variant_id",
                    "run_id", "model_used", "variant_name", "notes"):
            continue  # metadata, not config
        if isinstance(val, dict) and isinstance(current_config.get(key), dict):
            current_config[key].update(val)
        else:
            current_config[key] = val

    if not dry_run:
        write_trader_config(trader_dir, current_config)
        log.info("Updated config.yaml with %d param overrides", len(params))
    else:
        log.info("DRY RUN: would update config.yaml with %d param overrides", len(params))


def demote_live_to_virtual(
    conn,
    old_live_name: str,
    base_trader: str,
    archived_config: Dict[str, Any],
    dry_run: bool = False,
) -> Optional[int]:
    """Demote a former live trader to a virtual trader with status='probation'.

    The demoted trader keeps a snapshot of its params so it can be
    compared later. Creates a new entry in trading.virtual_traders.

    Returns the new virtual trader id, or None.
    """
    demoted_name = f"ex-{old_live_name}-{datetime.now(timezone.utc).strftime('%m%d')}"

    if dry_run:
        log.info(
            "DRY RUN: would demote '%s' → virtual '%s' (status=probation)",
            old_live_name, demoted_name,
        )
        return None

    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO trading.virtual_traders
               (name, base_trader, variant_type, params, status, notes)
               VALUES (%s, %s, 'demoted', %s, 'probation', %s)
               ON CONFLICT (name) DO UPDATE SET
                   base_trader = EXCLUDED.base_trader,
                   variant_type = 'demoted',
                   params = EXCLUDED.params,
                   status = 'probation',
                   notes = EXCLUDED.notes
               RETURNING id""",
            (
                demoted_name,
                base_trader,
                json.dumps(archived_config or {}),
                f"Demoted from live trader '{old_live_name}' at "
                f"{datetime.now(timezone.utc).isoformat()}",
            ),
        )
        row = cur.fetchone()
        conn.commit()
        vid = row[0]
        log.info("Demoted '%s' → virtual '%s' (id=%d)", old_live_name, demoted_name, vid)
        return vid
    except Exception as exc:
        conn.rollback()
        log.error("Failed to demote live trader '%s': %s", old_live_name, exc)
        return None
    finally:
        cur.close()


# ══════════════════════════════════════════════════════════════════════════
# Promotion logic
# ══════════════════════════════════════════════════════════════════════════


def check_scores(
    virtual_score: float,
    live_score: float,
    threshold_pct: float,
) -> Tuple[bool, float]:
    """Check if virtual outperforms live by the threshold.

    Returns (passes_threshold, improvement_pct).
    """
    if live_score == 0:
        improvement = 100.0 if virtual_score > 0 else 0.0
    else:
        improvement = ((virtual_score - live_score) / abs(live_score)) * 100.0

    passes = improvement >= threshold_pct
    log.info(
        "Score comparison: virtual=%.4f vs live=%.4f → improvement=%.2f%% "
        "(threshold=%.1f%%, %s)",
        virtual_score, live_score, improvement, threshold_pct,
        "PASSES ✓" if passes else "FAILS ✗",
    )
    return passes, improvement


def perform_promotion(
    conn,
    virtual: Dict[str, Any],
    base_trader: str,
    virtual_score: float,
    live_score: float,
    metric: str,
    threshold: float,
    improvement_pct: float,
    dry_run: bool = False,
) -> bool:
    """Execute the actual promotion: config swap, demotion, logging.

    Returns True on success.
    """
    virtual_name = virtual["name"]
    params = virtual.get("params", {}) or {}
    if isinstance(params, str):
        params = json.loads(params)

    trader_dir = live_trader_dir(base_trader)

    if not trader_dir.exists():
        log.error("Live trader directory not found: %s", trader_dir)
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if dry_run:
        log.info("DRY RUN: would promote '%s' → '%s'", virtual_name, base_trader)
        log.info("DRY RUN:   archive %s → %s", trader_dir,
                 archive_dir_for(base_trader, timestamp))
        log.info("DRY RUN:   apply virtual overrides to %s", trader_dir)
        log.info("DRY RUN:   demote '%s' → virtual (status=probation)", base_trader)
        log.info("DRY RUN:   update virtual '%s' status → 'promoted'", virtual_name)
        log.info("DRY RUN:   insert promotion_log entry")
        return True

    # Step 1: Archive current live trader config
    log.info("Archiving live trader config: %s", base_trader)
    archive_path = archive_trader_config(base_trader, trader_dir, timestamp)
    archived_config = read_trader_config(trader_dir) or {}

    # Step 2: Copy virtual config overrides to live trader directory
    log.info("Applying virtual overrides to %s", trader_dir)
    apply_virtual_overrides(trader_dir, params, dry_run=False)

    # Step 3: Demote former live trader to virtual
    log.info("Demoting former live trader '%s'", base_trader)
    demoted_id = demote_live_to_virtual(
        conn, base_trader, base_trader, archived_config, dry_run=False,
    )

    # Step 4: Update virtual trader status to 'promoted'
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE trading.virtual_traders SET status = 'promoted' WHERE name = %s",
            (virtual_name,),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log.error("Failed to update virtual trader status: %s", exc)
    finally:
        cur.close()

    # Step 5: Log to promotion_log
    notes = (
        f"Promoted virtual '{virtual_name}' → live '{base_trader}'. "
        f"Archived old config to {archive_path}. "
        f"Demoted '{base_trader}' to virtual id={demoted_id}."
    )

    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO trading.promotion_log
               (virtual_name, base_trader, live_trader_before,
                virtual_score, live_score, metric, threshold,
                improvement_pct, notes)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                virtual_name,
                base_trader,
                base_trader,
                virtual_score,
                live_score,
                metric,
                threshold,
                improvement_pct,
                notes,
            ),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log.error("Failed to insert promotion_log entry: %s", exc)
    finally:
        cur.close()

    log.info("✓ Promotion complete: '%s' → '%s'", virtual_name, base_trader)
    return True


def perform_rollback(
    conn,
    virtual_name: str,
    log_entry: Dict[str, Any],
    dry_run: bool = False,
) -> bool:
    """Reverse a promotion: restore archived config, undo virtual status.

    Args:
        conn: DB connection
        virtual_name: Name of the virtual trader that was promoted
        log_entry: The promotion_log entry from the promotion that was done
        dry_run: Preview mode

    Returns True on success.
    """
    base_trader = log_entry["base_trader"]

    # Find the most recent archive for this base trader
    archive_candidates = sorted(
        ARCHIVE_DIR.glob(f"{base_trader}-*"),
        key=os.path.getmtime,
        reverse=True,
    )

    if not archive_candidates:
        log.error("No archive dirs found for '%s'. Cannot rollback.", base_trader)
        return False

    archive_path = archive_candidates[0]
    trader_dir = live_trader_dir(base_trader)

    if dry_run:
        log.info("DRY RUN: would rollback promotion of '%s'", virtual_name)
        log.info("DRY RUN:   restore %s → %s", archive_path, trader_dir)
        log.info("DRY RUN:   revert demotion (delete ex-* virtual)")
        log.info("DRY RUN:   revert virtual '%s' status → 'probation'", virtual_name)
        log.info("DRY RUN:   mark promotion_log entry as rolled_back")
        return True

    # Step 1: Restore archived config to live trader directory
    if trader_dir.exists():
        shutil.rmtree(trader_dir)
    shutil.copytree(archive_path, trader_dir)
    log.info("Restored %s → %s", archive_path, trader_dir)

    # Step 2: Delete or demote the ex-live virtual (the demoted one)
    ex_name = f"ex-{base_trader}-*"
    cur = conn.cursor()
    try:
        # Find and delete the demoted virtual entry created during promotion
        cur.execute(
            "DELETE FROM trading.virtual_traders "
            "WHERE name LIKE %s AND variant_type = 'demoted'",
            (f"ex-{base_trader}-%",),
        )
        deleted = cur.rowcount
        conn.commit()
        log.info("Deleted %d demoted virtual(s) for '%s'", deleted, base_trader)
    except Exception as exc:
        conn.rollback()
        log.warning("Could not delete demoted virtuals: %s", exc)
    finally:
        cur.close()

    # Step 3: Revert promoted virtual's status back to 'probation'
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE trading.virtual_traders SET status = 'probation' WHERE name = %s",
            (virtual_name,),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log.warning("Could not revert virtual '%s' status: %s", virtual_name, exc)
    finally:
        cur.close()

    # Step 4: Mark promotion_log as rolled back
    cur = conn.cursor()
    try:
        cur.execute(
            """UPDATE trading.promotion_log
               SET was_rolled_back = TRUE,
                   rollback_at = NOW(),
                   notes = CONCAT(notes, ' | ROLLED BACK at ', NOW()::text)
               WHERE id = %s""",
            (log_entry["id"],),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log.warning("Could not mark promotion_log as rolled back: %s", exc)
    finally:
        cur.close()

    log.info("✓ Rollback complete: '%s' → restored '%s'", virtual_name, base_trader)
    return True


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════


def main() -> None:
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    conn = psycopg2.connect(args.db_dsn)
    try:
        ensure_tables(conn)

        # ── Rollback mode ──────────────────────────────────────────────
        if args.rollback:
            log_entry = fetch_latest_promotion_log(conn, args.name)
            if not log_entry:
                log.error(
                    "No promotion log entry found for '%s'. Nothing to rollback.",
                    args.name,
                )
                sys.exit(1)

            if log_entry["was_rolled_back"]:
                log.warning(
                    "Last promotion for '%s' was already rolled back at %s.",
                    args.name, log_entry["rollback_at"],
                )
                print(f"⚠ Last promotion for '{args.name}' was already rolled back.")
                sys.exit(0)

            ok = perform_rollback(conn, args.name, log_entry, dry_run=args.dry_run)
            if ok and not args.dry_run:
                log.info("Rollback successful.")
            elif ok and args.dry_run:
                log.info("Dry-run rollback complete.")
            else:
                log.error("Rollback failed.")
                sys.exit(1)
            return

        # ── Promotion mode ─────────────────────────────────────────────

        # Step 1: Load virtual trader
        virtual = fetch_virtual_trader(conn, args.name)
        if not virtual:
            log.error("Virtual trader '%s' not found in trading.virtual_traders", args.name)
            sys.exit(1)

        if virtual["status"] == "promoted":
            log.warning(
                "Virtual trader '%s' already promoted (status='promoted'). "
                "Use --rollback to reverse.",
                args.name,
            )
            print(f"⚠ '{args.name}' was already promoted. Use --rollback to reverse.")
            sys.exit(0)

        base_trader = args.base or virtual.get("base_trader", "kairos")
        log.info(
            "Candidate: virtual='%s' base='%s' score=%.4f",
            args.name, base_trader, virtual.get("score", 0.0) or 0.0,
        )

        # Step 2: Compute scores
        virtual_agent_id = args.name  # virtuals log as their name
        live_agent_id = base_trader   # live traders log as their agent_id

        virtual_pnl = fetch_pnl_data(conn, virtual_agent_id, args.window)
        live_pnl = fetch_pnl_data(conn, live_agent_id, args.window)

        virtual_score = compute_pnl_score(virtual_pnl, args.metric)
        live_score = compute_pnl_score(live_pnl, args.metric)

        log.info(
            "Scores (%d-day window, metric=%s): virtual=%.4f (%d rows), "
            "live=%.4f (%d rows)",
            args.window, args.metric,
            virtual_score, len(virtual_pnl),
            live_score, len(live_pnl),
        )

        # Step 3: Check threshold (unless --force)
        if not args.force:
            if len(virtual_pnl) < 1:
                log.warning(
                    "Insufficient virtual P&L data (%d rows in %d-day window). "
                    "Use --force to override.",
                    len(virtual_pnl), args.window,
                )
                print("⚠ Insufficient virtual P&L data. Use --force to promote anyway.")
                sys.exit(1)

            passes, improvement = check_scores(
                virtual_score, live_score, args.threshold
            )
            if not passes:
                log.warning(
                    "Improvement %.2f%% below threshold %.1f%%. "
                    "Use --force to override.",
                    improvement, args.threshold,
                )
                print(
                    f"⚠ Improvement {improvement:.2f}% < threshold "
                    f"{args.threshold:.1f}%. Use --force to override."
                )
                sys.exit(0)

            if live_score == 0 and virtual_score <= 0:
                log.warning(
                    "Both scores are zero/non-positive. "
                    "Use --force to promote anyway."
                )
                sys.exit(0)
        else:
            improvement = 0.0  # Not applicable with --force
            passes = True

        # Step 4: Perform the promotion
        ok = perform_promotion(
            conn,
            virtual,
            base_trader,
            virtual_score,
            live_score,
            args.metric,
            args.threshold,
            improvement,
            dry_run=args.dry_run,
        )

        if ok and not args.dry_run:
            print(f"✓ Promoted '{args.name}' → live trader '{base_trader}'")
        elif ok and args.dry_run:
            print(f"✓ DRY RUN: would promote '{args.name}' → '{base_trader}'")
        else:
            log.error("Promotion failed.")
            sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    main()