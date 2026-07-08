#!/usr/bin/env python3
"""
Seed the virtual_traders table with 24 variant configs (8 per base trader).

Idempotent: deletes existing entries for kairos/aldridge/stonks then inserts fresh.
Run: python3 scripts/seed_virtual_traders.py
"""
import json
import sys

import psycopg2
import psycopg2.extras

DB_URL = "postgresql://trader:@192.168.1.179:5433/trading"

# ── All 24 variants ──────────────────────────────────────────────────────
VARIANTS = [
    # ── Kairos (momentum) ──────────────────────────────────────────────
    {
        "name": "kairos-looser",
        "base_trader": "kairos",
        "variant_type": "params",
        "config": {
            "signal_params.momentum.threshold": 0.35,
            "signal_params.mean_reversion.rsi_oversold": 25,
        },
    },
    {
        "name": "kairos-tighter",
        "base_trader": "kairos",
        "variant_type": "params",
        "config": {
            "signal_params.mean_reversion.rsi_oversold": 35,
            "signal_params.momentum.threshold": 0.65,
        },
    },
    {
        "name": "kairos-aggro",
        "base_trader": "kairos",
        "variant_type": "params",
        "config": {
            "signal_params.momentum.threshold": 0.30,
            "signal_params.position_sizing.base_size_pct": 0.20,
        },
    },
    {
        "name": "kairos-patient",
        "base_trader": "kairos",
        "variant_type": "params",
        "config": {
            "signal_params.momentum.threshold": 0.70,
            "signal_params.position_sizing.base_size_pct": 0.10,
        },
    },
    {
        "name": "kairos-wide",
        "base_trader": "kairos",
        "variant_type": "params",
        "config": {
            "signal_params.risk.stop_loss_pct": 0.08,
            "signal_params.risk.take_profit_pct": 0.20,
        },
    },
    {
        "name": "kairos-tight",
        "base_trader": "kairos",
        "variant_type": "params",
        "config": {
            "signal_params.risk.stop_loss_pct": 0.03,
            "signal_params.risk.take_profit_pct": 0.10,
        },
    },
    {
        "name": "kairos-big",
        "base_trader": "kairos",
        "variant_type": "params",
        "config": {
            "signal_params.position_sizing.base_size_pct": 0.25,
            "signal_params.position_sizing.max_positions": 8,
        },
    },
    {
        "name": "kairos-small",
        "base_trader": "kairos",
        "variant_type": "params",
        "config": {
            "signal_params.position_sizing.base_size_pct": 0.08,
            "signal_params.position_sizing.max_positions": 3,
        },
    },
    # ── Aldridge (value) ───────────────────────────────────────────────
    {
        "name": "aldridge-looser",
        "base_trader": "aldridge",
        "variant_type": "params",
        "config": {
            "signal_params.momentum.threshold": 0.35,
            "signal_params.mean_reversion.rsi_oversold": 25,
        },
    },
    {
        "name": "aldridge-tighter",
        "base_trader": "aldridge",
        "variant_type": "params",
        "config": {
            "signal_params.mean_reversion.rsi_oversold": 35,
            "signal_params.momentum.threshold": 0.65,
        },
    },
    {
        "name": "aldridge-aggro",
        "base_trader": "aldridge",
        "variant_type": "params",
        "config": {
            "signal_params.momentum.threshold": 0.30,
            "signal_params.position_sizing.base_size_pct": 0.20,
        },
    },
    {
        "name": "aldridge-patient",
        "base_trader": "aldridge",
        "variant_type": "params",
        "config": {
            "signal_params.momentum.threshold": 0.70,
            "signal_params.position_sizing.base_size_pct": 0.10,
        },
    },
    {
        "name": "aldridge-wide",
        "base_trader": "aldridge",
        "variant_type": "params",
        "config": {
            "signal_params.risk.stop_loss_pct": 0.08,
            "signal_params.risk.take_profit_pct": 0.25,
        },
    },
    {
        "name": "aldridge-tight",
        "base_trader": "aldridge",
        "variant_type": "params",
        "config": {
            "signal_params.risk.stop_loss_pct": 0.03,
            "signal_params.risk.take_profit_pct": 0.12,
        },
    },
    {
        "name": "aldridge-big",
        "base_trader": "aldridge",
        "variant_type": "params",
        "config": {
            "signal_params.position_sizing.base_size_pct": 0.25,
            "signal_params.position_sizing.max_positions": 6,
        },
    },
    {
        "name": "aldridge-small",
        "base_trader": "aldridge",
        "variant_type": "params",
        "config": {
            "signal_params.position_sizing.base_size_pct": 0.08,
            "signal_params.position_sizing.max_positions": 2,
        },
    },
    # ── Stonks (sentiment) ─────────────────────────────────────────────
    {
        "name": "stonks-looser",
        "base_trader": "stonks",
        "variant_type": "params",
        "config": {
            "signal_params.momentum.threshold": 0.35,
            "signal_params.mean_reversion.rsi_oversold": 25,
        },
    },
    {
        "name": "stonks-tighter",
        "base_trader": "stonks",
        "variant_type": "params",
        "config": {
            "signal_params.mean_reversion.rsi_oversold": 35,
            "signal_params.momentum.threshold": 0.65,
        },
    },
    {
        "name": "stonks-aggro",
        "base_trader": "stonks",
        "variant_type": "params",
        "config": {
            "signal_params.momentum.threshold": 0.25,
            "signal_params.position_sizing.base_size_pct": 0.22,
        },
    },
    {
        "name": "stonks-patient",
        "base_trader": "stonks",
        "variant_type": "params",
        "config": {
            "signal_params.momentum.threshold": 0.75,
            "signal_params.position_sizing.base_size_pct": 0.08,
        },
    },
    {
        "name": "stonks-wide",
        "base_trader": "stonks",
        "variant_type": "params",
        "config": {
            "signal_params.risk.stop_loss_pct": 0.10,
            "signal_params.risk.take_profit_pct": 0.25,
        },
    },
    {
        "name": "stonks-tight",
        "base_trader": "stonks",
        "variant_type": "params",
        "config": {
            "signal_params.risk.stop_loss_pct": 0.02,
            "signal_params.risk.take_profit_pct": 0.08,
        },
    },
    {
        "name": "stonks-big",
        "base_trader": "stonks",
        "variant_type": "params",
        "config": {
            "signal_params.position_sizing.base_size_pct": 0.28,
            "signal_params.position_sizing.max_positions": 3,
        },
    },
    {
        "name": "stonks-small",
        "base_trader": "stonks",
        "variant_type": "params",
        "config": {
            "signal_params.position_sizing.base_size_pct": 0.06,
            "signal_params.position_sizing.max_positions": 2,
        },
    },
]


def main():
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                # Delete existing variants for these three base traders
                cur.execute(
                    "DELETE FROM trading.virtual_traders "
                    "WHERE base_trader IN ('kairos', 'aldridge', 'stonks')"
                )
                deleted = cur.rowcount
                print(f"Deleted {deleted} existing virtual trader(s)")

                # Insert all 24 variants
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO trading.virtual_traders
                       (name, base_trader, variant_type, config, status)
                       VALUES %s""",
                    [
                        (v["name"], v["base_trader"], v["variant_type"],
                         json.dumps(v["config"]), "active")
                        for v in VARIANTS
                    ],
                    template="(%s, %s, %s, %s::jsonb, %s)",
                )
                inserted = cur.rowcount
                print(f"Inserted {inserted} virtual traders")

        # Verify
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, base_trader, variant_type, status, config "
                "FROM trading.virtual_traders "
                "ORDER BY base_trader, name"
            )
            rows = cur.fetchall()
            print(f"\n─── Verification: {len(rows)} virtual traders ───")
            for row in rows:
                name, base, vtype, status, config = row
                print(f"  {name:22s} | {base:10s} | {vtype:8s} | {status:8s} | {json.dumps(config)}")

    finally:
        conn.close()

    if len([v for v in VARIANTS]) == inserted:
        print(f"\n✅ All {inserted} virtual traders seeded successfully.")
    else:
        print(f"\n⚠️  Expected {len(VARIANTS)} but inserted {inserted}.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
