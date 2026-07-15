#!/usr/bin/env python3
"""
Orchestrator — reads pending ticks from trading.tick_queue and dispatches
each tick to all live and virtual traders in parallel.

Architecture:
  For each pending tick in trading.tick_queue (WHERE status = 'pending'):
  1. Dispatch to 3 live traders (kairos, horizons, meridian)
  2. Simultaneously run all active virtual traders via virtual_runner.run_once()
  3. Results stored in trading.orchestrator_log table
  4. Individual trader failures do NOT crash the pipeline

  virtual_runner.run_once() fetches its own market data from the data bus
  and handles all virtual trader decision-making + result logging internally.
  The orchestrator only logs the dispatch event and marks ticks as processed.

Usage:
    python3 src/orchestrator.py              # process all pending ticks
    python3 src/orchestrator.py --dry-run    # print what would be processed
    python3 src/orchestrator.py --tick-id N  # process a specific tick only
    python3 src/orchestrator.py --skip-virtuals  # skip virtual trader dispatch
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

log = logging.getLogger("orchestrator")

# ── Defaults ──────────────────────────────────────────────────────────────

DB_DSN = os.getenv("ORCH_DB_URL", "postgresql://trader:@trading-db:5432/trading")

# Live traders — the 3 production-ish live traders.
LIVE_TRADERS = ["kairos", "horizons", "meridian"]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Orchestrator — dispatch ticks to live + virtual traders"
    )
    p.add_argument("--db-dsn", default=DB_DSN, help="Postgres DSN")
    p.add_argument(
        "--tick-id",
        type=int,
        default=None,
        help="Process a specific tick ID only",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print pending ticks without dispatching",
    )
    p.add_argument(
        "--skip-virtuals",
        action="store_true",
        help="Skip virtual trader dispatch (live traders only)",
    )
    p.add_argument(
        "--live-traders",
        default=",".join(LIVE_TRADERS),
        help="Comma-separated list of live trader names",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return p.parse_args(argv)


def ensure_orchestrator_log_table(conn) -> None:
    """Create trading.orchestrator_log table if it doesn't exist."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trading.orchestrator_log (
            id SERIAL PRIMARY KEY,
            tick_id INTEGER,
            trader TEXT NOT NULL,
            decision TEXT,
            status TEXT DEFAULT 'success',
            error TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()


def fetch_pending_ticks(conn, tick_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Fetch pending ticks from trading.tick_queue.

    If tick_id is given, fetches only that specific tick.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if tick_id is not None:
        cur.execute(
            "SELECT id, tick_data FROM trading.tick_queue WHERE id = %s",
            (tick_id,),
        )
    else:
        cur.execute(
            "SELECT id, tick_data FROM trading.tick_queue "
            "WHERE status = 'pending' ORDER BY id ASC"
        )
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


def mark_tick_processing(conn, tick_id: int) -> None:
    """Mark a tick as processing (prevent double-dispatch)."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE trading.tick_queue SET status = 'processing' WHERE id = %s",
        (tick_id,),
    )
    conn.commit()
    cur.close()


def mark_tick_done(conn, tick_id: int, error: Optional[str] = None) -> None:
    """Mark a tick as processed (done or error)."""
    cur = conn.cursor()
    if error:
        cur.execute(
            "UPDATE trading.tick_queue SET status = 'error', error = %s, "
            "processed_at = NOW() WHERE id = %s",
            (error, tick_id),
        )
    else:
        cur.execute(
            "UPDATE trading.tick_queue SET status = 'done', processed_at = NOW() "
            "WHERE id = %s",
            (tick_id,),
        )
    conn.commit()
    cur.close()


def log_trader_result(
    conn,
    tick_id: int,
    trader: str,
    decision: Optional[str],
    status: str = "success",
    error: Optional[str] = None,
) -> None:
    """Insert a row into trading.orchestrator_log."""
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO trading.orchestrator_log
           (tick_id, trader, decision, status, error)
           VALUES (%s, %s, %s, %s, %s)""",
        (tick_id, trader, decision, status, error),
    )
    conn.commit()
    cur.close()


def log_virtual_runner_event(
    conn, tick_id: int, summary: Dict[str, Any]
) -> None:
    """Log a summary row in orchestrator_log for the virtual runner cycle.

    The virtual runner handles its own detailed logging internally;
    this is just a dispatch marker for the orchestrator.
    """
    status = summary.get("status", "unknown")
    n_virtuals = summary.get("virtuals", 0)
    n_decisions = summary.get("decisions", 0)
    result_ok = "success" if status != "no_data" else "error"
    decision = (
        f"VIRTUAL_DISPATCH:virtuals={n_virtuals},decisions={n_decisions}"
    )
    error = None if result_ok == "success" else f"virtual_runner status={status}"
    log_trader_result(conn, tick_id, "virtual_runner", decision, result_ok, error)


def dispatch_to_live_traders(
    conn,
    tick_id: int,
    tick_data: Dict[str, Any],
    live_trader_names: List[str],
) -> List[Dict[str, Any]]:
    """Dispatch a tick to all live traders.

    Each live trader gets the tick and returns a decision.
    Individual failures are caught and logged; they don't crash others.

    Returns list of result dicts.
    """
    results: List[Dict[str, Any]] = []

    for name in live_trader_names:
        try:
            symbol = tick_data.get("symbol", "?")
            price = tick_data.get("price", 0.0)
            log.debug("Live trader %s processing %s @ %.2f", name, symbol, price)
            # TODO: Wire up actual trader agent invocation.
            # For now, a pass-through acknowledgement.
            decision = f"RECEIVED:{symbol}:{price:.2f}"
            results.append({"trader": name, "decision": decision, "status": "success"})
            log_trader_result(conn, tick_id, name, decision, "success")
        except Exception as exc:
            log.error("Live trader %s failed: %s", name, exc)
            results.append({
                "trader": name,
                "decision": None,
                "status": "error",
                "error": str(exc),
            })
            log_trader_result(conn, tick_id, name, None, "error", str(exc))

    return results


def run_virtual_traders_once() -> Dict[str, Any]:
    """Run one cycle of all virtual traders via virtual_runner.run_once().

    virtual_runner.run_once() handles:
      - Fetching market data from the data bus
      - Running all active virtual traders
      - Logging decisions to trading.trades

    The orchestrator just logs the dispatch event.

    Returns:
        Summary dict from virtual_runner.run_once(), or an error dict.
    """
    try:
        from src.virtual_runner import run_once as run_virtuals_once

        summary = run_virtuals_once()
        return summary
    except ImportError:
        log.warning("virtual_runner.run_once not available — skipping virtuals")
        return {"status": "import_error", "virtuals": 0, "decisions": 0}
    except Exception as exc:
        log.error("virtual_runner.run_once failed: %s", exc)
        return {"status": "error", "virtuals": 0, "decisions": 0, "error": str(exc)}


def main() -> None:
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    live_traders = [t.strip() for t in args.live_traders.split(",") if t.strip()]

    conn = psycopg2.connect(args.db_dsn)
    try:
        ensure_orchestrator_log_table(conn)

        # 1. Fetch pending ticks from tick_queue
        ticks = fetch_pending_ticks(conn, tick_id=args.tick_id)
        if not ticks:
            log.info("No pending ticks to process.")
            return

        log.info("Found %d pending tick(s)", len(ticks))

        if args.dry_run:
            print("── DRY RUN — would dispatch these ticks ──")
            for t in ticks:
                td = t["tick_data"]
                print(
                    f"  tick_id={t['id']} "
                    f"symbol={td.get('symbol', '?')} "
                    f"price={td.get('price', 0.0):.2f} "
                    f"volume={td.get('volume', 0)}"
                )
                print(f"  Would dispatch to: {', '.join(live_traders)} + virtuals")
            return

        # 2. Run virtual traders once in a background thread while processing ticks
        virtual_summary: Optional[Dict[str, Any]] = None
        with ThreadPoolExecutor(max_workers=2) as pool:
            # Start virtual runner in parallel if not skipped
            if not args.skip_virtuals:
                virtual_future = pool.submit(run_virtual_traders_once)
            else:
                virtual_future = None

            # 3. Process each tick — dispatch to live traders
            summary: Dict[str, Any] = {
                "ticks_processed": 0,
                "total_live_calls": 0,
                "total_errors": 0,
            }
            for tick in ticks:
                tick_id = tick["id"]
                tick_data = tick["tick_data"]

                mark_tick_processing(conn, tick_id)
                try:
                    live_results = dispatch_to_live_traders(
                        conn, tick_id, tick_data, live_traders
                    )
                    all_ok = all(
                        r.get("status") == "success" for r in live_results
                    )
                    if all_ok:
                        mark_tick_done(conn, tick_id)
                    else:
                        error_detail = "; ".join(
                            r.get("error", "unknown")
                            for r in live_results
                            if r.get("status") == "error"
                        )
                        mark_tick_done(conn, tick_id, error=error_detail)
                except Exception as exc:
                    log.error("Fatal error dispatching tick %d: %s", tick_id, exc)
                    mark_tick_done(conn, tick_id, error=str(exc))
                    continue

                summary["ticks_processed"] += 1
                summary["total_live_calls"] += len(live_results)
                summary["total_errors"] += sum(
                    1 for r in live_results if r.get("status") == "error"
                )

            # 4. Collect virtual runner results
            if virtual_future is not None:
                try:
                    virtual_summary = virtual_future.result()
                    # Log virtual runner event for all ticks
                    for tick in ticks:
                        log_virtual_runner_event(
                            conn, tick["id"], virtual_summary
                        )
                    log.info(
                        "Virtual runner: %d virtuals, %d decisions (status=%s)",
                        virtual_summary.get("virtuals", 0),
                        virtual_summary.get("decisions", 0),
                        virtual_summary.get("status", "?"),
                    )
                except Exception as exc:
                    log.error("Virtual runner unhandled exception: %s", exc)
                    for tick in ticks:
                        log_trader_result(
                            conn, tick["id"], "virtual_runner", None,
                            "error", str(exc),
                        )

        log.info(
            "Done. Processed %d tick(s) across %d live trader call(s) "
            "(%d error(s))",
            summary["ticks_processed"],
            summary["total_live_calls"],
            summary["total_errors"],
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()