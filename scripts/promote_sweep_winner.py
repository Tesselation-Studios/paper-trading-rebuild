#!/usr/bin/env python3
"""
promote_sweep_winner.py — reads sweep results, picks the top-ranked variant,
creates a virtual trader from it.

The sweep pipeline runs overnight and produces results in the
trading.sweep_results table (and optionally results/sweep_results.json).
This script reads the most recent sweep run, identifies the winning variant
(by objective_score), and creates a virtual trader entry in
trading.virtual_traders with status='probation' and variant_type='from_sweep'.

Usage:
    python3 scripts/promote_sweep_winner.py                    # read from DB
    python3 scripts/promote_sweep_winner.py --json             # read from JSON file
    python3 scripts/promote_sweep_winner.py --run-id N         # specific sweep run
    python3 scripts/promote_sweep_winner.py --dry-run          # preview only
    python3 scripts/promote_sweep_winner.py --name my-variant  # force a specific variant
    python3 scripts/promote_sweep_winner.py --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

log = logging.getLogger("promote_sweep_winner")

# ── Defaults ──────────────────────────────────────────────────────────────

DB_DSN = os.getenv("SWEEP_WINNER_DB_URL", "postgresql://trader:@trading-db:5432/trading")
PROJECT_DIR = Path(__file__).resolve().parent.parent
SWEEP_RESULTS_JSON = PROJECT_DIR / "results" / "sweep_results.json"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Promote sweep winner to a probationary virtual trader"
    )
    p.add_argument("--db-dsn", default=DB_DSN, help="Postgres DSN")
    p.add_argument(
        "--json",
        action="store_true",
        help="Read from results/sweep_results.json instead of DB",
    )
    p.add_argument("--run-id", type=int, default=None, help="Specific sweep run ID")
    p.add_argument(
        "--name",
        type=str,
        default=None,
        help="Force a specific variant name instead of auto-picking top scorer",
    )
    p.add_argument("--dry-run", action="store_true", help="Preview mode, no inserts")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return p.parse_args(argv)


# ── DB access ─────────────────────────────────────────────────────────────


def ensure_virtual_traders_table(conn) -> None:
    """Create trading.virtual_traders table if it doesn't exist."""
    cur = conn.cursor()
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


def get_latest_sweep_run_id(conn) -> Optional[int]:
    """Get the most recent sweep run ID from trading.sweep_runs."""
    cur = conn.cursor()
    cur.execute("SELECT MAX(id) FROM trading.sweep_runs")
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def get_sweep_results_from_db(
    conn, run_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Fetch sweep results from trading.sweep_results, optionally for a specific run."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if run_id is not None:
        cur.execute(
            """SELECT sr.id AS result_id,
                      sr.run_id,
                      sr.trader_id,
                      sv.variant_id,
                      sv.name AS variant_name,
                      sr.objective_score,
                      sr.total_pnl,
                      sr.n_ticks,
                      sr.n_trades,
                      sr.win_rate,
                      sr.calmar,
                      sr.sortino,
                      sr.profit_factor,
                      sr.elapsed_s,
                      sr.model_used,
                      sr.created_at
               FROM trading.sweep_results sr
               LEFT JOIN trading.sweep_variants sv ON (
                   sv.run_id = sr.run_id AND sv.variant_id = sr.variant_id
               )
               WHERE sr.run_id = %s
               ORDER BY sr.objective_score DESC NULLS LAST""",
            (run_id,),
        )
    else:
        cur.execute(
            """SELECT sr.id AS result_id,
                      sr.run_id,
                      sr.trader_id,
                      sv.variant_id,
                      sv.name AS variant_name,
                      sr.objective_score,
                      sr.total_pnl,
                      sr.n_ticks,
                      sr.n_trades,
                      sr.win_rate,
                      sr.calmar,
                      sr.sortino,
                      sr.profit_factor,
                      sr.elapsed_s,
                      sr.model_used,
                      sr.created_at
               FROM trading.sweep_results sr
               JOIN (
                   SELECT MAX(run_id) AS max_run_id FROM trading.sweep_runs
               ) latest ON sr.run_id = latest.max_run_id
               LEFT JOIN trading.sweep_variants sv ON (
                   sv.run_id = sr.run_id AND sv.variant_id = sr.variant_id
               )
               ORDER BY sr.objective_score DESC NULLS LAST""",
        )

    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


def load_sweep_results_from_json(path: Path) -> List[Dict[str, Any]]:
    """Load sweep results from a JSON file.

    Expected format: list of dicts or dict with "results" key.
    Each dict should have: variant_name, objective_score, total_pnl, etc.
    """
    if not path.exists():
        log.error("Sweep results JSON not found: %s", path)
        return []

    with open(path, "r") as f:
        data = json.load(f)

    if isinstance(data, list):
        results = data
    elif isinstance(data, dict):
        results = data.get("results", data.get("variants", data.get("sweep", [])))
    else:
        log.error("Unexpected JSON format in %s: expected list or dict with 'results' key", path)
        return []

    # Normalise keys
    normalised: List[Dict[str, Any]] = []
    for r in results:
        normalised.append({
            "result_id": r.get("id", r.get("result_id", 0)),
            "run_id": r.get("run_id", 0),
            "trader_id": r.get("trader", r.get("trader_id", "kairos")),
            "variant_id": r.get("variant_id", 0),
            "variant_name": r.get(
                "name", r.get("variant_name", r.get("variant", "unknown"))
            ),
            "objective_score": float(r.get("score", r.get("objective_score", 0.0))),
            "total_pnl": float(r.get("pnl", r.get("total_pnl", 0.0))),
            "n_ticks": int(r.get("ticks", r.get("n_ticks", 0))),
            "n_trades": int(r.get("trades", r.get("n_trades", 0))),
            "win_rate": float(r.get("win_rate", 0.0)),
            "calmar": float(r.get("calmar", 0.0)),
            "sortino": float(r.get("sortino", 0.0)),
            "profit_factor": float(r.get("profit_factor", 0.0)),
            "model_used": r.get("model", r.get("model_used", "unknown")),
        })
    return normalised


def pick_winner(
    results: List[Dict[str, Any]], force_name: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Pick the winning variant from sweep results.

    If force_name is given, picks that specific variant.
    Otherwise picks the one with the highest objective_score.
    """
    if not results:
        log.warning("No sweep results to pick from")
        return None

    if force_name:
        for r in results:
            vname = r.get("variant_name", "")
            if vname == force_name:
                log.info(
                    "Forced winner: '%s' (score=%.4f)",
                    force_name, r.get("objective_score", 0.0),
                )
                return r
        log.warning("Forced variant '%s' not found in results", force_name)
        return None

    # Pick top by objective_score
    winner = max(results, key=lambda r: r.get("objective_score", 0.0) or 0.0)
    log.info(
        "Top variant: '%s' (score=%.4f, pnl=%.2f, win_rate=%.2f%%)",
        winner.get("variant_name", "?"),
        winner.get("objective_score", 0.0),
        winner.get("total_pnl", 0.0),
        winner.get("win_rate", 0.0) * 100,
    )
    return winner


def insert_virtual_trader(
    conn,
    name: str,
    base_trader: str,
    variant_id: Optional[int],
    score: float,
    params: Optional[Dict[str, Any]] = None,
) -> int:
    """Insert a new virtual trader row into trading.virtual_traders.

    Returns the new row ID.
    """
    cur = conn.cursor()
    notes = f"Promoted from sweep (score={score:.4f}) at {datetime.now(timezone.utc).isoformat()}"
    cur.execute(
        """INSERT INTO trading.virtual_traders
           (name, base_trader, variant_type, variant_id, params, status, score, notes)
           VALUES (%s, %s, 'from_sweep', %s, %s, 'probation', %s, %s)
           ON CONFLICT (name) DO UPDATE SET
               variant_type = 'from_sweep',
               variant_id = EXCLUDED.variant_id,
               params = EXCLUDED.params,
               status = 'probation',
               score = EXCLUDED.score,
               notes = EXCLUDED.notes
           RETURNING id""",
        (
            name,
            base_trader,
            variant_id,
            json.dumps(params or {}),
            score,
            notes,
        ),
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    return row[0]


def build_virtual_name(winner: Dict[str, Any]) -> str:
    """Build a unique virtual trader name from the sweep variant."""
    vname = winner.get("variant_name", "")
    run_id = winner.get("run_id", 0)
    prefix = vname.replace(" ", "_").lower()[:24] if vname else f"sweep-run{run_id}"
    suffix = datetime.now(timezone.utc).strftime("%m%d")
    return f"{prefix}-{suffix}"


def dry_run_output(winner: Dict[str, Any], virtual_name: str) -> None:
    """Print what would happen in dry-run mode."""
    print("── DRY RUN — would create virtual trader ──")
    print(f"  virtual_name:   {virtual_name}")
    print(f"  base_trader:    {winner.get('trader_id', 'kairos')}")
    print(f"  variant_name:   {winner.get('variant_name', '?')}")
    print(f"  variant_id:     {winner.get('variant_id', 0)}")
    print(f"  objective_score: {winner.get('objective_score', 0.0):.4f}")
    print(f"  total_pnl:      {winner.get('total_pnl', 0.0):.2f}")
    print(f"  win_rate:       {winner.get('win_rate', 0.0) * 100:.1f}%")
    print(f"  status:         probation")
    print(f"  variant_type:   from_sweep")
    print(f"  notes:          Promoted from sweep (score={winner.get('objective_score', 0.0):.4f})")


def main() -> None:
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Read sweep results
    if args.json:
        results = load_sweep_results_from_json(SWEEP_RESULTS_JSON)
    else:
        conn = psycopg2.connect(args.db_dsn)
        try:
            run_id = args.run_id
            if run_id is None:
                run_id = get_latest_sweep_run_id(conn)
                log.info("Latest sweep run ID: %s", run_id)
            results = get_sweep_results_from_db(conn, run_id=run_id)
        finally:
            conn.close()

    if not results:
        log.error("No sweep results found. Run a sweep first, or specify --json or --run-id.")
        sys.exit(1)

    log.info("Loaded %d sweep result(s)", len(results))

    # 2. Pick winner
    winner = pick_winner(results, force_name=args.name)
    if winner is None:
        log.error("No winning variant could be determined.")
        sys.exit(1)

    virtual_name = build_virtual_name(winner)

    if args.dry_run:
        dry_run_output(winner, virtual_name)
        return

    # 3. Insert virtual trader
    conn = psycopg2.connect(args.db_dsn)
    try:
        ensure_virtual_traders_table(conn)
        vid = insert_virtual_trader(
            conn,
            name=virtual_name,
            base_trader=winner.get("trader_id", "kairos"),
            variant_id=winner.get("variant_id"),
            score=float(winner.get("objective_score", 0.0)),
            params={
                "variant_name": winner.get("variant_name", ""),
                "run_id": winner.get("run_id"),
                "model_used": winner.get("model_used", ""),
            },
        )
        log.info(
            "Promoted '%s' → virtual trader id=%d (status=probation)",
            virtual_name, vid,
        )
        print(f"✓ Created virtual trader '{virtual_name}' (id={vid}, status=probation)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()