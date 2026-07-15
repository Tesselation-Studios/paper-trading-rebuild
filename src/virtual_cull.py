#!/usr/bin/env python3
"""Virtual Trader Culling — weekly cleanup and regeneration.

Runs Sunday 23:00 ET.
  1. Rank all virtuals by 7-day rolling P&L (realized + unrealized)
  2. Cull bottom 3 per base trader (mark status='culled', set culled_at)
  3. Generate 3 new variants:
     a. 1 param variant: random perturbation of the #1 virtual's params
     b. 1 prompt variant: placeholder (weight adjustment to simulate prompt change)
     c. 1 wildcard: random new config within safe bounds
  4. New virtuals get status='probation' (2-day wait before promotion eligible)

Usage:
    python3 src/virtual_cull.py                    # run once (for cron)
    python3 src/virtual_cull.py --dry-run           # print what would happen
    python3 src/virtual_cull.py --once              # run once (default, for testing)
    python3 src/virtual_cull.py --cull-count 2      # cull only 2 instead of 3
    python3 src/virtual_cull.py --base kairos       # only cull one base trader
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.signals import SignalParams

log = logging.getLogger("virtual_cull")

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════

DB_DSN = os.getenv("VT_DB_DSN", "host=trading-db port=5432 dbname=trading user=trader")
BASE_TRADERS = ["kairos", "aldridge", "stonks"]

DEFAULT_CULL_COUNT = 3
P7D_LOOKBACK_DAYS = 7
PROBATION_DAYS = 2

# Mapping from seed-style dot-notation config keys to flat SignalParams names
_CONFIG_KEY_MAP: Dict[str, str] = {
    "signal_params.momentum.threshold": "momentum_threshold",
    "signal_params.mean_reversion.rsi_oversold": "rsi_oversold",
    "signal_params.mean_reversion.rsi_overbought": "rsi_overbought",
    "signal_params.mean_reversion.bollinger_std": "bollinger_std",
    "signal_params.volume.threshold": "volume_threshold",
    "signal_params.volatility.regime_threshold": "vol_regime_threshold",
    "signal_params.volatility.reduction_multiplier": "vol_reduction_multiplier",
    "signal_params.position_sizing.base_size_pct": "base_size_pct",
    "signal_params.position_sizing.conviction_multiplier": "conviction_multiplier",
    "signal_params.position_sizing.max_positions": "max_positions",
    "signal_params.risk.stop_loss_pct": "stop_loss_pct",
    "signal_params.risk.take_profit_pct": "take_profit_pct",
    "signal_params.risk.trailing_stop_pct": "trailing_stop_pct",
    "signal_params.regime_weights.trending_up": "weight_trending_up",
    "signal_params.regime_weights.trending_down": "weight_trending_down",
    "signal_params.regime_weights.mean_reverting": "weight_mean_reverting",
    "signal_params.regime_weights.high_volatility": "weight_high_volatility",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Database helpers
# ═══════════════════════════════════════════════════════════════════════════════

def get_db():
    """Return a psycopg2 connection with autocommit."""
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


# ═══════════════════════════════════════════════════════════════════════════════
# P&L computation (consistent with virtual_rotate.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _latest_close_prices(tickers: List[str], as_of: date) -> Dict[str, float]:
    """Get the most recent closing price for each ticker from market_data.bars."""
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


def compute_7day_pnl(trader_ids: List[str]) -> Dict[str, float]:
    """Compute 7-day rolling net P&L: realized + mark-to-market unrealized."""
    since = date.today() - timedelta(days=P7D_LOOKBACK_DAYS)
    today = date.today()

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── Realized P&L: closed within the window ──
    placeholders = ",".join(["%s"] * len(trader_ids))
    cur.execute(
        f"""SELECT trader_id, COALESCE(SUM(pnl), 0) as realized_pnl
            FROM trading.trades
            WHERE trader_id IN ({placeholders})
              AND exit_time IS NOT NULL
              AND exit_time::date >= %s
            GROUP BY trader_id""",
        (*trader_ids, since),
    )
    realized_map = {row["trader_id"]: float(row["realized_pnl"]) for row in cur.fetchall()}

    # ── Unrealized P&L: open positions marked to market ──
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

    open_tickers = list({p["ticker"] for p in open_positions})
    close_prices = _latest_close_prices(open_tickers, today)

    unrealized_map: Dict[str, float] = defaultdict(float)
    for pos in open_positions:
        trader = pos["trader_id"]
        ticker = pos["ticker"]
        shares = int(pos["total_shares"])
        entry = float(pos["avg_entry"])
        close = close_prices.get(ticker, entry)
        unrealized_map[trader] += (close - entry) * shares

    conn.close()

    # Combine
    pnl_map: Dict[str, float] = {}
    for tid in trader_ids:
        realized = realized_map.get(tid, 0.0)
        unrealized = unrealized_map.get(tid, 0.0)
        pnl_map[tid] = realized + unrealized

    return pnl_map


# ═══════════════════════════════════════════════════════════════════════════════
# Config normalization — handles both seed dot-notation and flat param names
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_config(raw_config: Dict[str, Any]) -> Dict[str, float]:
    """Convert a stored config (seed dot-notation or flat names) to flat param dict.

    Supports both formats:
      - Seed format:  {"signal_params.momentum.threshold": 0.35, ...}
      - Flat format:  {"momentum_threshold": 0.35, ...}

    Only returns keys that match known SignalParams names.
    """
    result: Dict[str, float] = {}
    for key, value in raw_config.items():
        try:
            fval = float(value)
        except (TypeError, ValueError):
            continue

        # Try flat name first
        if key in SignalParams.param_names():
            result[key] = fval
        # Try dot-notation mapping
        elif key in _CONFIG_KEY_MAP:
            result[_CONFIG_KEY_MAP[key]] = fval

    return result


def get_param_value(config: Dict[str, Any], param_name: str) -> Optional[float]:
    """Extract a single SignalParams value from config dict, handling both formats."""
    # Flat name
    if param_name in config:
        try:
            return float(config[param_name])
        except (TypeError, ValueError):
            pass

    # Dot notation — find the reverse mapping
    for dot_key, flat_name in _CONFIG_KEY_MAP.items():
        if flat_name == param_name and dot_key in config:
            try:
                return float(config[dot_key])
            except (TypeError, ValueError):
                pass

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Variant generation
# ═══════════════════════════════════════════════════════════════════════════════

def _random_suffix() -> str:
    """Short random suffix for variant names."""
    import uuid
    return uuid.uuid4().hex[:6]


def generate_param_variant(
    base_trader: str, top_config: Dict[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Generate a parameter perturbation of the #1 trader's config.

    Picks one known SignalParams parameter from the top config and perturbs
    within ±15% of its valid range. Falls back to picking a random param
    if the top config has no recognizable keys.

    Returns:
        (name, flat_config_dict)
    """
    normalized = normalize_config(top_config)
    params = SignalParams()

    # Find parameters that are both in the top config AND known to SignalParams
    tunable = [k for k in normalized if k in params.param_names()]

    if not tunable:
        # Top config has no recognizable params — pick any SignalParams param
        tunable = params.param_names()

    param_name = random.choice(tunable)
    bound = SignalParams.bound(param_name)

    # Get current value (from top config, or default)
    if param_name in normalized:
        current = normalized[param_name]
    else:
        current = bound.default

    # Perturb within ±15% of the full range
    delta = (bound.max_val - bound.min_val) * random.uniform(-0.15, 0.15)
    new_val = bound.clip(current + delta)

    # Build the new config dict (flat names)
    new_config: Dict[str, Any] = {param_name: new_val}

    # Copy other params from top config for consistency
    for name, val in normalized.items():
        if name != param_name and name in params.param_names():
            new_config[name] = val

    name = f"{base_trader}-param-{_random_suffix()}"
    return name, new_config


def generate_prompt_variant(base_trader: str) -> Tuple[str, Dict[str, Any]]:
    """Generate a prompt variant — placeholder for future sweep integration.

    Currently simulates a prompt change by adjusting signal weights and
    confidence thresholds. Real prompt variants will come from the prompt_sweep
    pipeline.

    Returns:
        (name, flat_config_dict)
    """
    params = SignalParams()

    # Adjust signal emphasis to simulate different prompt priorities
    new_config: Dict[str, Any] = {
        "momentum_threshold": round(random.uniform(0.35, 0.75), 2),
        "volume_threshold": round(random.uniform(0.8, 2.0), 2),
        "weight_trending_up": round(random.uniform(0.5, 1.8), 2),
        "weight_trending_down": round(random.uniform(0.0, 1.0), 2),
        "weight_mean_reverting": round(random.uniform(0.3, 1.5), 2),
        "conviction_multiplier": round(random.uniform(1.0, 2.5), 2),
    }

    # Clip to bounds
    for pname, val in list(new_config.items()):
        bound = SignalParams.bound(pname)
        new_config[pname] = bound.clip(val)

    name = f"{base_trader}-prompt-{_random_suffix()}"
    return name, new_config


def generate_wildcard(base_trader: str) -> Tuple[str, Dict[str, Any]]:
    """Generate a completely random config within safe SignalParams bounds.

    Wildcard — picks a random subset (3–5) of parameters and randomizes them.
    This explores the parameter space broadly.

    Returns:
        (name, flat_config_dict)
    """
    params = SignalParams()
    all_names = params.param_names()

    # Pick 3–5 random params to randomize
    n_pick = random.randint(3, min(5, len(all_names)))
    picked = random.sample(all_names, n_pick)

    new_config: Dict[str, Any] = {}
    for pname in picked:
        bound = SignalParams.bound(pname)
        if bound.is_int:
            new_config[pname] = random.randint(int(bound.min_val), int(bound.max_val))
        else:
            new_config[pname] = round(random.uniform(bound.min_val, bound.max_val), 3)

    name = f"{base_trader}-wild-{_random_suffix()}"
    return name, new_config


# Ordered generators: param, prompt, wildcard
GENERATORS: List[Tuple[str, Any]] = [
    ("params", generate_param_variant),
    ("prompt", generate_prompt_variant),
    ("wildcard", generate_wildcard),
]


# ═══════════════════════════════════════════════════════════════════════════════
# Database operations
# ═══════════════════════════════════════════════════════════════════════════════

def insert_virtual(
    name: str,
    base_trader: str,
    variant_type: str,
    config: Dict[str, Any],
    status: str = "probation",
):
    """Insert a new virtual trader into trading.virtual_traders.

    Skips duplicates (name already taken).
    """
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT id FROM trading.virtual_traders WHERE name = %s", (name,))
    if cur.fetchone():
        log.warning("  Virtual %s already exists — skipping", name)
        conn.close()
        return

    cur.execute(
        """INSERT INTO trading.virtual_traders
           (name, base_trader, variant_type, config, status, created_at, wins)
           VALUES (%s, %s, %s, %s::jsonb, %s, %s, 0)""",
        (name, base_trader, variant_type, json.dumps(config), status, date.today()),
    )
    conn.close()
    log.info("  ✅ Created %s (type=%s, status=%s)", name, variant_type, status)


def cull_virtual(name: str):
    """Mark a virtual trader as culled with timestamp."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """UPDATE trading.virtual_traders
           SET status = 'culled', culled_at = %s
           WHERE name = %s""",
        (date.today(), name),
    )
    conn.close()
    log.info("  🗑️  Culled %s", name)


def get_virtuals_for_base(base_trader: str) -> List[Dict[str, Any]]:
    """Get active virtual traders for a base type from the DB."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT name, config, status, variant_type
           FROM trading.virtual_traders
           WHERE base_trader = %s
             AND status IN ('active', 'probation')
           ORDER BY name""",
        (base_trader,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# Main culling logic
# ═══════════════════════════════════════════════════════════════════════════════

def cull_base_trader(
    base_trader: str,
    cull_count: int = DEFAULT_CULL_COUNT,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run culling and regeneration for one base trader.

    Returns summary dict.
    """
    log.info("── %s ───────────────────────────────────────────────", base_trader.upper())

    # Get active virtuals
    virtuals = get_virtuals_for_base(base_trader)

    if len(virtuals) <= cull_count:
        log.warning(
            "  Only %d active virtuals — need more than %d to cull. Skipping.",
            len(virtuals), cull_count,
        )
        return {
            "base_trader": base_trader,
            "status": "too_few",
            "count": len(virtuals),
        }

    # ── Step 1: Compute 7-day P&L ──
    names = [v["name"] for v in virtuals]
    pnl_map = compute_7day_pnl(names)

    # ── Step 2: Rank and identify bottom performers ──
    # Sort ascending (worst first) for culling
    ranked = sorted(pnl_map.items(), key=lambda x: x[1])
    bottom = ranked[:cull_count]
    top_name, top_pnl = ranked[-1] if ranked else (None, 0.0)

    log.info("  7-day P&L ranking (%d virtuals):", len(ranked))
    for i, (name, pnl) in enumerate(ranked):
        marker = ""
        if (name, pnl) in bottom:
            marker = " ← CULL"
        elif name == top_name:
            marker = " ← TOP"
        log.info("    %2d. %-28s $%+8.2f%s", i + 1, name, pnl, marker)

    # ── Step 3: Get top trader's config for variant seeding ──
    top_config: Dict[str, Any] = {}
    for v in virtuals:
        if v["name"] == top_name:
            raw = v.get("config", {})
            top_config = raw if isinstance(raw, dict) else {}
            break

    # ── Step 4: Cull bottom performers ──
    for name, pnl in bottom:
        if not dry_run:
            cull_virtual(name)

    # ── Step 5: Generate replacements ──
    log.info("  Generating %d new variants:", len(bottom))
    new_virtuals = []
    for i, (gen_type, gen_fn) in enumerate(GENERATORS[:len(bottom)]):
        if gen_type == "params":
            new_name, new_config = gen_fn(base_trader, top_config)
        else:
            new_name, new_config = gen_fn(base_trader)

        new_virtuals.append((new_name, gen_type, new_config))
        log.info("    %d. %-30s (type=%s)", i + 1, new_name, gen_type)

        if not dry_run:
            insert_virtual(new_name, base_trader, gen_type, new_config, status="probation")

    return {
        "base_trader": base_trader,
        "culled": [name for name, _ in bottom],
        "generated": [name for name, _, _ in new_virtuals],
        "top_trader": top_name,
        "top_pnl": top_pnl,
        "status": "ok",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def print_summary(results: List[Dict[str, Any]]):
    """Print human-readable summary to stdout."""
    total_culled = sum(len(r.get("culled", [])) for r in results)
    total_generated = sum(len(r.get("generated", [])) for r in results)

    print()
    print("═" * 72)
    print(f"  VIRTUAL TRADER CULLING — {date.today()}")
    print("═" * 72)

    for r in results:
        bt = r["base_trader"].upper()
        status = r.get("status", "")

        if status == "error":
            print(f"\n  ❌ {bt}: ERROR — {r.get('error', 'unknown')}")
            continue
        if status == "too_few":
            print(f"\n  ⚠️  {bt}: Only {r['count']} virtuals — skipped")
            continue

        culled = r.get("culled", [])
        generated = r.get("generated", [])
        top = r.get("top_trader", "?")
        top_pnl = r.get("top_pnl", 0)

        print(f"\n  📊 {bt}")
        print(f"     Top:       {top}  (7d P&L: ${top_pnl:.2f})")
        print(f"     Culled:    {', '.join(culled) if culled else 'none'}")
        print(f"     Generated: {', '.join(generated) if generated else 'none'}")

    print(f"\n{'═' * 72}")
    print(f"  Summary: {total_culled} culled, {total_generated} generated")
    print(f"{'═' * 72}\n")


def main():
    global DB_DSN, BASE_TRADERS

    parser = argparse.ArgumentParser(description="Virtual Trader Culling — weekly cleanup")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing to DB")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit (default behavior; for script consistency)")
    parser.add_argument("--cull-count", type=int, default=DEFAULT_CULL_COUNT,
                        help=f"Cull this many virtuals per base (default: {DEFAULT_CULL_COUNT})")
    parser.add_argument("--base", type=str, default=None,
                        help="Only cull one base trader (default: all three)")
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
    log.info("Virtual Trader Culling — %s", "DRY RUN" if args.dry_run else "LIVE")
    log.info("Date: %s | Cull count: %d", date.today(), args.cull_count)

    # Determine which base traders to cull
    if args.base:
        if args.base not in BASE_TRADERS:
            log.error("Unknown base trader: %s. Valid: %s", args.base, BASE_TRADERS)
            sys.exit(1)
        base_traders = [args.base]
    else:
        base_traders = list(BASE_TRADERS)

    # Run culling for each base trader
    results = []
    for bt in base_traders:
        try:
            result = cull_base_trader(
                bt,
                cull_count=args.cull_count,
                dry_run=args.dry_run,
            )
            results.append(result)
        except Exception as e:
            log.error("Culling failed for %s: %s", bt, e, exc_info=True)
            results.append({"base_trader": bt, "status": "error", "error": str(e)})

    # Summary
    print_summary(results)

    if args.dry_run:
        log.info("DRY RUN — no changes were made.")


if __name__ == "__main__":
    main()
