#!/usr/bin/env python3
"""Virtual Trader Rotation — nightly selection of tomorrow's live trader.

Runs at 20:00 ET daily after market close. For each base trader (kairos,
aldridge, stonks):

  1. Compute today's net P&L (realized + unrealized) for all virtuals + LIVE
  2. Rank by P&L → award daily WIN to #1
  3. Championship belt: IF challenger.wins > main.wins → ROTATE
  4. Apply guardrails: min trades, main tenure, 3-day lock, all-negative freeze
  5. Log decisions to rotation_log

Usage:
    python3 src/virtual_rotate.py                   # run once (for cron)
    python3 src/virtual_rotate.py --dry-run          # print what would happen
    python3 src/virtual_rotate.py --once             # run once (default behavior, for testing)
    python3 src/virtual_rotate.py --force-rotate kairos  # force rotation for testing
    python3 src/virtual_rotate.py --base kairos       # only rotate one base trader
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger("virtual_rotate")

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════

DB_DSN = os.getenv("VT_DB_DSN", "host=trading-db port=5432 dbname=trading user=trader")

BASE_TRADERS = ["kairos", "aldridge", "stonks"]

# Guardrails
MIN_CLOSED_TRADES = 10         # minimum closed trades before promotion
MAIN_MIN_DAYS = 3              # main must have tenure before challengers can contest
LOCK_STREAK_DAYS = 3           # consecutive daily wins → locked (belt holder immune)
MAX_LOCK_DAYS = 5              # maximum days lock can last
ALL_NEGATIVE_FREEZE_DAYS = 3   # consecutive all-negative P&L days → freeze system


# ═══════════════════════════════════════════════════════════════════════════════
# Database helpers
# ═══════════════════════════════════════════════════════════════════════════════

def get_db():
    """Return a psycopg2 connection with autocommit."""
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


def ensure_baseline_traders():
    """Ensure baseline entries exist in virtual_traders for each base trader.

    The baseline is the live trader itself (e.g., 'kairos' not in virtual_traders
    by default). We insert a placeholder so it can accumulate wins and
    participate in the championship belt model.
    """
    conn = get_db()
    cur = conn.cursor()
    for base in BASE_TRADERS:
        name = f"trader-{base}"
        cur.execute(
            "SELECT id FROM trading.virtual_traders WHERE name = %s",
            (name,),
        )
        if not cur.fetchone():
            cur.execute(
                """INSERT INTO trading.virtual_traders
                   (name, base_trader, variant_type, config, status, created_at, wins)
                   VALUES (%s, %s, %s, %s::jsonb, 'live', %s, 0)""",
                (name, base, "baseline", "{}", date.today()),
            )
            log.info("  Created baseline entry: %s", name)
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# P&L computation
# ═══════════════════════════════════════════════════════════════════════════════

def _latest_close_prices(tickers: List[str], as_of: date) -> Dict[str, float]:
    """Get the most recent closing price for each ticker from market_data.bars.

    Falls back to entry_price (no gain/loss) if no bars available.
    """
    if not tickers:
        return {}

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    prices: Dict[str, float] = {}
    for ticker in set(tickers):
        cur.execute(
            """SELECT close FROM market_data.bars
               WHERE ticker = %s AND timestamp <= %s::timestamp + interval '1 day'
               ORDER BY timestamp DESC LIMIT 1""",
            (ticker, as_of),
        )
        row = cur.fetchone()
        if row:
            prices[ticker] = float(row["close"])

    conn.close()
    return prices


def compute_daily_pnl(
    trader_ids: List[str], target_date: Optional[date] = None
) -> Dict[str, float]:
    """Compute net P&L for each trader: realized + mark-to-market unrealized.

    Args:
        trader_ids: list of trader names (includes virtuals + live baseline)
        target_date: date to compute P&L for (default: today)

    Returns:
        {trader_name: net_pnl}
    """
    if target_date is None:
        target_date = date.today()

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── 1. Realized P&L: trades closed today ──
    # Use parameterized IN clause
    placeholders = ",".join(["%s"] * len(trader_ids))
    cur.execute(
        f"""SELECT trader_id, COALESCE(SUM(pnl), 0) as realized_pnl
            FROM trading.trades
            WHERE trader_id IN ({placeholders})
              AND exit_time IS NOT NULL
              AND exit_time::date = %s
            GROUP BY trader_id""",
        (*trader_ids, target_date),
    )
    realized_map = {row["trader_id"]: float(row["realized_pnl"]) for row in cur.fetchall()}

    # ── 2. Unrealized P&L: open positions marked to latest close ──
    cur.execute(
        f"""SELECT trader_id, ticker, SUM(shares) as total_shares,
                   AVG(entry_price) as avg_entry
            FROM trading.trades
            WHERE trader_id IN ({placeholders})
              AND exit_time IS NULL
            GROUP BY trader_id, ticker
            HAVING SUM(shares) != 0""",
        (*trader_ids,),
    )
    open_positions = cur.fetchall()

    # Get latest close prices for all tickers with open positions
    open_tickers = list({p["ticker"] for p in open_positions})
    close_prices = _latest_close_prices(open_tickers, target_date)

    # Calculate unrealized P&L per trader
    unrealized_map: Dict[str, float] = defaultdict(float)
    for pos in open_positions:
        trader = pos["trader_id"]
        ticker = pos["ticker"]
        shares = int(pos["total_shares"])
        entry = float(pos["avg_entry"])
        close = close_prices.get(ticker, entry)  # fallback to entry price → 0 P&L
        unrealized_map[trader] += (close - entry) * shares

    conn.close()

    # ── 3. Combine ──
    pnl_map: Dict[str, float] = {}
    for tid in trader_ids:
        realized = realized_map.get(tid, 0.0)
        unrealized = unrealized_map.get(tid, 0.0)
        pnl_map[tid] = realized + unrealized

    return pnl_map


def count_closed_trades(trader_id: str, since_date: Optional[date] = None) -> int:
    """Count closed trades (with exit_time) for a trader since a given date."""
    if since_date is None:
        since_date = date.today() - timedelta(days=30)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT COUNT(*) FROM trading.trades
           WHERE trader_id = %s AND exit_time IS NOT NULL AND exit_time::date >= %s""",
        (trader_id, since_date),
    )
    count = cur.fetchone()[0]
    conn.close()
    return count


# ═══════════════════════════════════════════════════════════════════════════════
# Win/loss tracking
# ═══════════════════════════════════════════════════════════════════════════════

def get_win_counts(base_trader: str) -> Dict[str, int]:
    """Get cumulative win counts for all traders of a base type from virtual_traders."""
    conn = get_db()
    cur = conn.cursor()

    # Get wins from virtual_traders
    cur.execute(
        """SELECT name, wins FROM trading.virtual_traders
           WHERE base_trader = %s AND status IN ('active', 'live', 'probation')""",
        (base_trader,),
    )
    win_counts = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()
    return win_counts


def award_daily_win(winner_name: str):
    """Increment the daily win counter for the #1 P&L trader."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE trading.virtual_traders SET wins = wins + 1 WHERE name = %s",
        (winner_name,),
    )
    rows = cur.rowcount
    conn.close()
    if rows == 0:
        log.warning("  Could not award win: %s not in virtual_traders", winner_name)
    else:
        log.info("  🏆 Daily WIN awarded to %s", winner_name)


def get_main_consecutive_wins(base_trader: str, main_id: str) -> int:
    """Count consecutive days the current main has been ranked #1."""
    conn = get_db()
    cur = conn.cursor()
    today = date.today()

    cur.execute(
        """SELECT date, top_virtual FROM trading.rotation_log
           WHERE base_trader = %s
           ORDER BY date DESC""",
        (base_trader,),
    )
    rows = cur.fetchall()
    conn.close()

    streak = 0
    expected_date = today - timedelta(days=1)
    for row in rows:
        log_date, top_virtual = row
        if log_date != expected_date:
            break
        if top_virtual == main_id:
            streak += 1
            expected_date -= timedelta(days=1)
        else:
            break
    return streak


def get_main_tenure(base_trader: str, main_id: str) -> int:
    """Count consecutive days this trader has been the main (status='live')."""
    conn = get_db()
    cur = conn.cursor()

    # Look at rotation_log for last promotion of this trader
    cur.execute(
        """SELECT date FROM trading.rotation_log
           WHERE base_trader = %s AND live_virtual = %s AND promoted = true
           ORDER BY date DESC LIMIT 1""",
        (base_trader, main_id),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        # Not in rotation_log — likely the baseline. Check live_dates.
        cur2 = conn.cursor()
        cur2.execute(
            "SELECT live_dates FROM trading.virtual_traders WHERE name = %s",
            (main_id,),
        )
        vt_row = cur2.fetchone()
        conn.close()
        if vt_row and vt_row[0]:
            dates = vt_row[0]
            if dates:
                last_promoted = max(dates)
                return (date.today() - last_promoted).days
        return 0  # Baseline with no tenure tracking

    last_promoted = row[0]

    # Check if anyone else was promoted since
    cur2 = conn.cursor()
    cur2.execute(
        """SELECT COUNT(*) FROM trading.rotation_log
           WHERE base_trader = %s AND date > %s AND promoted = true AND live_virtual != %s""",
        (base_trader, last_promoted, main_id),
    )
    if cur2.fetchone()[0] > 0:
        conn.close()
        return 0  # Someone else took over since

    conn.close()
    return (date.today() - last_promoted).days


def check_all_negative_streak(base_trader: str) -> int:
    """Check consecutive days where live_virtual AND top_virtual P&L were negative."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT live_pnl, top_virtual_pnl FROM trading.rotation_log
           WHERE base_trader = %s
           ORDER BY date DESC LIMIT %s""",
        (base_trader, ALL_NEGATIVE_FREEZE_DAYS),
    )
    rows = cur.fetchall()
    conn.close()

    streak = 0
    for row in rows:
        lp = row[0] or 0
        tp = row[1] or 0
        if lp < 0 and tp < 0:
            streak += 1
        else:
            break
    return streak


# ═══════════════════════════════════════════════════════════════════════════════
# Promotion logic — championship belt model
# ═══════════════════════════════════════════════════════════════════════════════

def should_promote(
    base_trader: str,
    main_id: str,
    challenger_id: str,
    main_pnl: float,
    challenger_pnl: float,
) -> Tuple[bool, str]:
    """Championship belt model: determine if challenger takes the belt.

    Rules (evaluated in order):
      1. Minimum closed trades: challenger must have ≥ MIN_CLOSED_TRADES
      2. Main tenure: main must have been main for ≥ MAIN_MIN_DAYS
      3. All-negative freeze: all traders negative for N consecutive days → freeze
      4. 3-day lock: main on a win streak ≥ LOCK_STREAK_DAYS → can't be challenged
      5. Belt comparison: challenger.wins > main.wins → ROTATE

    Returns:
        (should_promote: bool, reason: str)
    """
    # Rule 1: Minimum closed trades
    challenger_trades = count_closed_trades(challenger_id)
    if challenger_trades < MIN_CLOSED_TRADES:
        return False, (
            f"Challenger has only {challenger_trades} closed trades "
            f"(need {MIN_CLOSED_TRADES})"
        )

    # Rule 2: Main tenure
    main_tenure = get_main_tenure(base_trader, main_id)
    if main_tenure < MAIN_MIN_DAYS:
        return False, (
            f"Main trader has only {main_tenure} days tenure "
            f"(need {MAIN_MIN_DAYS})"
        )

    # Rule 3: All-negative freeze
    neg_streak = check_all_negative_streak(base_trader)
    if neg_streak >= ALL_NEGATIVE_FREEZE_DAYS:
        return False, (
            f"🚨 FREEZE: all {base_trader} traders have negative P&L for "
            f"{neg_streak} consecutive days — system frozen, human review needed"
        )

    # Rule 4: 3-day lock (main on a win streak)
    main_streak = get_main_consecutive_wins(base_trader, main_id)
    if main_streak >= LOCK_STREAK_DAYS:
        return False, (
            f"🔒 LOCKED: main ({main_id}) has {main_streak}-day win streak "
            f"— can't be challenged until day {MAX_LOCK_DAYS}"
        )

    # Rule 5: Championship belt — challenger must have MORE cumulative wins
    win_counts = get_win_counts(base_trader)
    main_wins = win_counts.get(main_id, 0)
    challenger_wins = win_counts.get(challenger_id, 0)

    if challenger_wins <= main_wins:
        return False, (
            f"Challenger wins ({challenger_wins}) ≤ Main wins ({main_wins})"
        )

    return True, (
        f"PROMOTE: {challenger_id} ({challenger_wins}W) overtakes "
        f"{main_id} ({main_wins}W) — P&L: ${challenger_pnl:.2f} vs ${main_pnl:.2f}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Rotation execution
# ═══════════════════════════════════════════════════════════════════════════════

def find_current_main(base_trader: str) -> str:
    """Find the current main (live) trader for a base type.

    Looks in virtual_traders for status='live' first (previously promoted
    virtual), then falls back to the baseline trader-{base}.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT name FROM trading.virtual_traders
           WHERE base_trader = %s AND status = 'live'
           ORDER BY created_at DESC LIMIT 1""",
        (base_trader,),
    )
    row = cur.fetchone()
    conn.close()
    if row:
        return row[0]
    return f"trader-{base_trader}"


def get_active_virtuals(base_trader: str) -> List[str]:
    """Get list of active virtual trader names for a base type."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT name FROM trading.virtual_traders
           WHERE base_trader = %s AND status IN ('active', 'live', 'probation')
           ORDER BY name""",
        (base_trader,),
    )
    names = [row[0] for row in cur.fetchall()]
    conn.close()
    return names


def log_rotation(
    base_trader: str,
    live_virtual: str,
    live_pnl: float,
    top_virtual: str,
    top_virtual_pnl: float,
    promoted: bool,
    reason: str,
):
    """Record today's rotation decision."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO trading.rotation_log
           (date, base_trader, live_virtual, live_pnl, top_virtual, top_virtual_pnl,
            promoted, reason)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (date.today(), base_trader, live_virtual, live_pnl,
         top_virtual, top_virtual_pnl, promoted, reason),
    )
    conn.close()


def update_virtual_status(name: str, new_status: str, add_live_date: bool = False):
    """Update a virtual trader's status and optionally record a live date."""
    conn = get_db()
    cur = conn.cursor()
    if add_live_date:
        cur.execute(
            """UPDATE trading.virtual_traders
               SET status = %s,
                   live_dates = array_append(COALESCE(live_dates, '{}'::date[]), %s::date)
               WHERE name = %s""",
            (new_status, date.today(), name),
        )
    else:
        cur.execute(
            "UPDATE trading.virtual_traders SET status = %s WHERE name = %s",
            (new_status, name),
        )
    conn.close()


def execute_rotation(base_trader: str, old_main: str, new_main: str):
    """Swap the live trader: demote old main, promote challenger."""
    # Demote old main (if it's a virtual, not the baseline)
    update_virtual_status(old_main, "active")

    # Promote challenger
    update_virtual_status(new_main, "live", add_live_date=True)

    log.info("  ✅ ROTATED: %s → %s", old_main, new_main)


# ═══════════════════════════════════════════════════════════════════════════════
# Main rotation logic
# ═══════════════════════════════════════════════════════════════════════════════

def rotate_base_trader(
    base_trader: str,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """Run nightly rotation for one base trader.

    Returns a decision summary dict.
    """
    today = date.today()
    main_id = find_current_main(base_trader)

    log.info("── %s ───────────────────────────────────────────────", base_trader.upper())
    log.info("  Current main: %s  |  Date: %s", main_id, today)

    # Get all competing traders (virtuals + main)
    active_virtuals = get_active_virtuals(base_trader)
    all_traders = list(set(active_virtuals + [main_id]))

    if len(all_traders) < 2:
        log.warning("  Only 1 trader (%s) — nothing to rotate.", main_id)
        return {
            "base_trader": base_trader,
            "main": main_id,
            "status": "insufficient_traders",
        }

    # ── Step 1: Compute today's P&L ──
    pnl_map = compute_daily_pnl(all_traders, today)
    sorted_traders = sorted(pnl_map.items(), key=lambda x: x[1], reverse=True)

    top_name, top_pnl = sorted_traders[0]
    main_pnl = pnl_map.get(main_id, 0.0)

    # ── Step 2: Print ranking ──
    log.info("  Today's P&L ranking:")
    for rank, (name, pnl) in enumerate(sorted_traders, 1):
        badge = ""
        if name == main_id:
            badge = " ← MAIN"
        if name == top_name:
            badge = " ← #1"
        log.info("    %2d. %-26s $%+8.2f%s", rank, name, pnl, badge)

    # ── Step 3: Award daily win to #1 P&L ──
    if top_pnl > 0 or any(p > 0 for _, p in sorted_traders):
        # Award win to #1 (positive P&L or best among mixed)
        if not dry_run:
            award_daily_win(top_name)
    else:
        log.info("  ⚠️  All traders negative — no win awarded today.")

    # ── Step 4: Check if main stays #1 ──
    if top_name == main_id:
        reason = "Main is already #1 — belt stays"
        log.info("  🔒 %s", reason)
        if not dry_run:
            log_rotation(base_trader, main_id, main_pnl, top_name, top_pnl, False, reason)
        return {
            "base_trader": base_trader,
            "main": main_id,
            "top": top_name,
            "promoted": False,
            "reason": reason,
            "pnl_ranking": [{"name": n, "pnl": p} for n, p in sorted_traders],
        }

    # ── Step 5: Evaluate promotion ──
    if force:
        promote = True
        reason = "FORCED rotation (--force-rotate)"
    else:
        promote, reason = should_promote(base_trader, main_id, top_name, main_pnl, top_pnl)

    status_icon = "🔺 PROMOTE" if promote else "✖ KEEP"
    log.info("  %s  %s", status_icon, reason)

    # ── Step 6: Execute ──
    if (promote or force) and not dry_run:
        execute_rotation(base_trader, main_id, top_name)

    if not dry_run:
        log_rotation(base_trader, main_id, main_pnl, top_name, top_pnl, promote, reason)

    return {
        "base_trader": base_trader,
        "main": main_id,
        "top": top_name,
        "main_pnl": main_pnl,
        "top_pnl": top_pnl,
        "promoted": promote,
        "reason": reason,
        "pnl_ranking": [{"name": n, "pnl": p} for n, p in sorted_traders],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def print_summary(results: List[Dict[str, Any]]):
    """Print human-readable summary to stdout."""
    print()
    print("═" * 72)
    print(f"  VIRTUAL TRADER ROTATION — {date.today()}")
    print("═" * 72)

    for r in results:
        status = r.get("status", "")
        if status == "error":
            print(f"\n  ❌ {r['base_trader'].upper()}: ERROR — {r.get('error', 'unknown')}")
            continue
        if status == "insufficient_traders":
            print(f"\n  ⚠️  {r['base_trader'].upper()}: Only 1 trader — nothing to rotate")
            continue

        promoted = r.get("promoted", False)
        icon = "🔺" if promoted else "🔒"
        print(f"\n  {icon} {r['base_trader'].upper()}")
        print(f"     Main:   {r['main']}  (P&L: ${r.get('main_pnl', 0):.2f})")
        print(f"     #1:     {r['top']}  (P&L: ${r.get('top_pnl', 0):.2f})")
        print(f"     Action: {'PROMOTED ✅' if promoted else 'KEPT'}")
        print(f"     Reason: {r['reason']}")

        # Show top 5 ranking
        ranking = r.get("pnl_ranking", [])
        if ranking:
            print(f"\n     Top 5 P&L:")
            for rank, entry in enumerate(ranking[:5], 1):
                badge = ""
                if entry["name"] == r["main"]:
                    badge = " ← MAIN"
                if entry["name"] == r["top"]:
                    badge = " ← #1"
                print(f"       {rank}. {entry['name']:<26} ${entry['pnl']:+8.2f}{badge}")

    promoted_count = sum(1 for r in results if r.get("promoted"))
    print(f"\n{'═' * 72}")
    if promoted_count > 0:
        print(f"  ✅ {promoted_count} rotation(s) executed")
    else:
        print(f"  No rotations today — belts held.")
    print(f"{'═' * 72}\n")


def main():
    global DB_DSN, BASE_TRADERS

    parser = argparse.ArgumentParser(description="Virtual Trader Rotation — nightly selection")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing to DB")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit (default behavior; for script consistency)")
    parser.add_argument("--force-rotate", type=str, default=None,
                        help="Force rotation for a specific base trader (e.g., 'kairos')")
    parser.add_argument("--base", type=str, default=None,
                        help="Only rotate one base trader (default: all three)")
    parser.add_argument("--db-dsn", type=str, default=DB_DSN,
                        help="Postgres connection string")
    args = parser.parse_args()

    DB_DSN = args.db_dsn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("═" * 60)
    log.info("Virtual Trader Rotation — %s", "DRY RUN" if args.dry_run else "LIVE")
    log.info("Date: %s", date.today())

    # Determine which base traders to rotate
    if args.base:
        if args.base not in BASE_TRADERS:
            log.error("Unknown base trader: %s. Valid: %s", args.base, BASE_TRADERS)
            sys.exit(1)
        base_traders = [args.base]
    else:
        base_traders = list(BASE_TRADERS)

    # Ensure baseline entries exist (so they can accumulate wins)
    if not args.dry_run:
        ensure_baseline_traders()

    # Run rotation for each base trader
    results = []
    for bt in base_traders:
        try:
            result = rotate_base_trader(
                bt,
                dry_run=args.dry_run,
                force=(args.force_rotate == bt),
            )
            results.append(result)
        except Exception as e:
            log.error("Rotation failed for %s: %s", bt, e, exc_info=True)
            results.append({"base_trader": bt, "status": "error", "error": str(e)})

    # Print summary
    print_summary(results)

    if args.dry_run:
        log.info("DRY RUN — no changes were made.")


if __name__ == "__main__":
    main()
