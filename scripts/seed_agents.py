#!/usr/bin/env python3
"""Seed agent profiles into Postgres (trading.agent_profile) so the learning loop
has entities to analyze. Falls back to SQLite shared/trader.db for legacy compat."""

import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

# Postgres connection
PG_DSN = os.getenv("PG_DSN", "host=trading-db port=5432 dbname=trading user=trader")

# SQLite fallback path
SQLITE_DB = Path(__file__).resolve().parent.parent / "shared" / "trader.db"

PROFILES = {
    "trader-aldridge": {
        "name": "Edmund Whitfield",
        "company": "Aldridge & Partners",
        "tagline": "Measured. Deliberate. Enduring.",
        "identity": {
            "core_belief": "Fundamentals drive long-term value. Technicals confirm timing, not conviction.",
            "approach": "Value-oriented, bottom-up fundamental analysis. Long-only. Weeks to months horizon.",
            "personality": "Measured, avuncular, occasionally pompous. Thinks out loud.",
            "age": 62,
            "style": "Conservative, patient, skeptical of momentum",
        },
        "current_state": {
            "confidence": 0.7,
            "frustration": 0.2,
            "excitement": 0.3,
        },
        "performance": {
            "trades_this_week": 0,
            "win_rate": 0.0,
            "weekly_pnl": 0.0,
            "total_pnl": 0.0,
        },
        "strategic_focus": {
            "primary_obsession": "Finding mispriced quality — oversold blue chips with strong balance sheets",
            "sector_bets": ["defensive", "healthcare", "consumer staples"],
            "max_positions": 5,
            "position_size_pct": 10,
        },
    },
    "trader-kairos": {
        "name": "Zara Chen",
        "company": "Kairos Capital",
        "tagline": "Precision at the turning point.",
        "identity": {
            "core_belief": "Momentum reveals itself before the crowd sees it. Patterns + conviction = edge.",
            "approach": "Multi-timeframe momentum with ML conviction scoring. Technicals first, fundamentals as filter.",
            "personality": "Sharp, decisive, data-obsessed. Runs hot but backs it with analytics.",
            "age": 34,
            "style": "Aggressive trend-following with risk gates. ML-enhanced conviction scoring.",
        },
        "current_state": {
            "confidence": 0.65,
            "frustration": 0.3,
            "excitement": 0.6,
        },
        "performance": {
            "trades_this_week": 0,
            "win_rate": 0.0,
            "weekly_pnl": 0.0,
            "total_pnl": 0.0,
        },
        "strategic_focus": {
            "primary_obsession": "Riding momentum breakouts with ML-confirmed conviction",
            "sector_bets": ["technology", "semiconductors", "growth"],
            "max_positions": 4,
            "position_size_pct": 20,
        },
    },
    "trader-stonks": {
        "name": "Stan 'the Man' Hoolihan",
        "company": "Stonks Capital",
        "tagline": "Follow the noise, find the signal.",
        "identity": {
            "core_belief": "Social sentiment leads price. The crowd finds the next big move before analysts do.",
            "approach": "Social sentiment + unusual options flow + volume spikes. High-risk, high-reward.",
            "personality": "Loud, confident, internet-native. Lives on Reddit, Stocktwits, and Bluesky.",
            "age": 28,
            "style": "Aggressive, community-driven. Chases momentum and social hype.",
        },
        "current_state": {
            "confidence": 0.6,
            "frustration": 0.2,
            "excitement": 0.8,
        },
        "performance": {
            "trades_this_week": 0,
            "win_rate": 0.0,
            "weekly_pnl": 0.0,
            "total_pnl": 0.0,
        },
        "strategic_focus": {
            "primary_obsession": "Scanning social feeds for the next gamma squeeze or narrative breakout",
            "sector_bets": ["meme", "crypto-adjacent", "high-beta"],
            "max_positions": 3,
            "position_size_pct": 25,
        },
    },
}


def seed_postgres():
    """Seed agent profiles into Postgres trading.agent_profile."""
    try:
        dsn = os.getenv("PG_DSN", "host=trading-db port=5432 dbname=trading user=trader")
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()

        # Ensure trading schema has the agent_profile table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trading.agent_profile (
                id              BIGSERIAL PRIMARY KEY,
                agent_id        VARCHAR(32)     NOT NULL,
                name            VARCHAR(128),
                company         VARCHAR(128),
                tagline         TEXT,
                identity        JSONB,
                current_state   JSONB,
                performance     JSONB,
                strategic_focus TEXT,
                updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
                created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_agent_profile_id UNIQUE (agent_id)
            )
        """)

        for agent_id, profile in PROFILES.items():
            cur.execute(
                """INSERT INTO trading.agent_profile
                   (agent_id, name, company, tagline, identity, current_state,
                    performance, strategic_focus, updated_at)
                   VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, NOW())
                   ON CONFLICT (agent_id) DO UPDATE SET
                       name = EXCLUDED.name,
                       company = EXCLUDED.company,
                       tagline = EXCLUDED.tagline,
                       identity = EXCLUDED.identity,
                       current_state = EXCLUDED.current_state,
                       performance = EXCLUDED.performance,
                       strategic_focus = EXCLUDED.strategic_focus,
                       updated_at = NOW()""",
                (
                    agent_id,
                    profile["name"],
                    profile["company"],
                    profile["tagline"],
                    json.dumps(profile["identity"]),
                    json.dumps(profile["current_state"]),
                    json.dumps(profile["performance"]),
                    json.dumps(profile["strategic_focus"]),
                ),
            )
            print(f"✅ Postgres: Seeded {agent_id} — {profile['name']}")

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"⚠️  Postgres seed failed: {e} — falling back to SQLite")
        return False


def seed_sqlite():
    """Seed agent profiles into SQLite shared/trader.db (legacy fallback)."""
    import sqlite3

    db = sqlite3.connect(str(SQLITE_DB))
    db.execute("PRAGMA journal_mode=WAL")

    for agent_id, profile in PROFILES.items():
        db.execute(
            """INSERT OR REPLACE INTO agent_profile
               (agent_id, name, company, tagline, identity, current_state,
                performance, strategic_focus)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_id,
                profile["name"],
                profile["company"],
                profile["tagline"],
                json.dumps(profile["identity"]),
                json.dumps(profile["current_state"]),
                json.dumps(profile["performance"]),
                json.dumps(profile["strategic_focus"]),
            ),
        )
        print(f"✅ SQLite: Seeded {agent_id} — {profile['name']}")

    db.commit()
    db.close()


def seed():
    """Seed all agent profiles — Postgres first, SQLite fallback."""
    print("🌱 Seeding agent profiles...\n")

    if seed_postgres():
        print(f"\n✅ Agent profiles seeded to Postgres ({len(PROFILES)} profiles)")
    else:
        seed_sqlite()
        print(f"\n✅ Agent profiles seeded to SQLite ({len(PROFILES)} profiles)")


if __name__ == "__main__":
    seed()