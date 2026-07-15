#!/usr/bin/env python3
"""
Seed test data into Postgres for CI integration tests.

Creates all tables the leaderboard_api needs (in the trading schema),
then populates them with realistic-but-synthetic data so the dashboard
renders with actual content for Playwright to verify.

Usage:
    PG_DSN="host=trading-db port=5432 dbname=trading user=trader" \
    python3 scripts/seed_test_data.py

This is designed to run as a Docker Compose one-shot service (depends_on: trading-db)
in docker-compose.test.yml.
"""

import os
import sys
import json
import random
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

PG_DSN = os.getenv("PG_DSN", "host=trading-db port=5432 dbname=trading user=trader")

NOW = datetime.now(timezone.utc)

TRADERS = [
    {"agent_id": "kairos",   "name": "Kairós Capital",     "manager": "Zara Chen"},
    {"agent_id": "aldridge", "name": "Aldridge & Partners", "manager": "Edmund Whitfield"},
    {"agent_id": "stonks",   "name": "Stonks Capital",      "manager": "Stan Hoolihan"},
]

TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "SPY", "QQQ", "BRK.B"]


def get_conn():
    """Connect to Postgres with retries."""
    import time
    for attempt in range(30):
        try:
            conn = psycopg2.connect(PG_DSN)
            conn.autocommit = True
            return conn
        except Exception as e:
            if attempt < 29:
                time.sleep(1)
            else:
                print(f"FATAL: Could not connect after 30s: {e}", file=sys.stderr)
                sys.exit(1)


def ensure_schema(conn):
    """Ensure the trading schema exists."""
    cur = conn.cursor()
    cur.execute("CREATE SCHEMA IF NOT EXISTS trading")
    cur.execute("CREATE SCHEMA IF NOT EXISTS market_data")
    cur.close()


def ensure_tables(conn):
    """Create all tables the leaderboard_api needs (idempotent)."""
    cur = conn.cursor()
    tables = """
        CREATE TABLE IF NOT EXISTS trading.agent_profile (
            id              BIGSERIAL PRIMARY KEY,
            agent_id        VARCHAR(32) NOT NULL,
            name            VARCHAR(128),
            company         VARCHAR(128),
            tagline         TEXT,
            identity        TEXT,
            current_state   JSONB,
            performance     JSONB,
            strategic_focus TEXT,
            market_observations JSONB,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_agent_profile_id UNIQUE (agent_id)
        );

        CREATE TABLE IF NOT EXISTS trading.agent_state (
            id              BIGSERIAL PRIMARY KEY,
            agent_id        VARCHAR(32) NOT NULL,
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            last_heartbeat  TIMESTAMPTZ,
            last_tick       TIMESTAMPTZ,
            cash            DECIMAL NOT NULL DEFAULT 10000.00,
            equity          DECIMAL NOT NULL DEFAULT 10000.00,
            pnl             DECIMAL NOT NULL DEFAULT 0,
            pnl_pct         DECIMAL NOT NULL DEFAULT 0,
            positions_count INTEGER NOT NULL DEFAULT 0,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_agent_state_id UNIQUE (agent_id)
        );

        CREATE TABLE IF NOT EXISTS trading.portfolio_snapshots (
            id              BIGSERIAL PRIMARY KEY,
            trader_id       VARCHAR(32) NOT NULL,
            timestamp       TIMESTAMPTZ NOT NULL,
            cash            DECIMAL NOT NULL,
            portfolio_value DECIMAL NOT NULL,
            unrealized_pl   DECIMAL NOT NULL DEFAULT 0,
            daily_pnl       DECIMAL NOT NULL DEFAULT 0,
            open_positions  INTEGER NOT NULL DEFAULT 0,
            source          VARCHAR(32) DEFAULT 'db_snapshot',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_psnap_trader_ts
            ON trading.portfolio_snapshots (trader_id, timestamp);

        CREATE TABLE IF NOT EXISTS trading.trader_positions (
            id              BIGSERIAL PRIMARY KEY,
            trader_id       VARCHAR(32) NOT NULL,
            ticker          VARCHAR(10) NOT NULL,
            quantity        DECIMAL NOT NULL,
            market_value    DECIMAL,
            unrealized_pl   DECIMAL,
            avg_entry_price DECIMAL,
            current_price   DECIMAL,
            status          VARCHAR(16) NOT NULL DEFAULT 'open',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_tpos_trader
            ON trading.trader_positions (trader_id);

        CREATE TABLE IF NOT EXISTS trading.trader_decisions (
            id              BIGSERIAL PRIMARY KEY,
            agent_id        VARCHAR(32) NOT NULL,
            timestamp       TIMESTAMPTZ NOT NULL,
            action          VARCHAR(16) NOT NULL,
            ticker          VARCHAR(10),
            quantity        DECIMAL,
            stop_loss       DECIMAL,
            confidence      DECIMAL,
            thesis          TEXT,
            source          VARCHAR(32),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_tdec_agent_ts
            ON trading.trader_decisions (agent_id, timestamp);

        CREATE TABLE IF NOT EXISTS trading.orders (
            id                BIGSERIAL PRIMARY KEY,
            agent_id          VARCHAR(32) NOT NULL,
            order_id          VARCHAR(64) NOT NULL,
            ticker            VARCHAR(10) NOT NULL,
            action            VARCHAR(8) NOT NULL,
            quantity          DECIMAL NOT NULL,
            status            VARCHAR(16) NOT NULL,
            decision_id       INTEGER,
            error_reason      TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_orders_oid UNIQUE (order_id)
        );

        CREATE TABLE IF NOT EXISTS trading.trader_journal (
            id              BIGSERIAL PRIMARY KEY,
            agent_id        VARCHAR(32) NOT NULL,
            timestamp       TIMESTAMPTZ NOT NULL,
            mood            VARCHAR(32),
            entry           TEXT,
            confidence      DECIMAL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_tjourn_agent_ts
            ON trading.trader_journal (agent_id, timestamp);

        CREATE TABLE IF NOT EXISTS trading.risk_events (
            id              BIGSERIAL PRIMARY KEY,
            trader_id       VARCHAR(32) NOT NULL,
            timestamp       TIMESTAMPTZ NOT NULL,
            vetoed          BOOLEAN NOT NULL DEFAULT FALSE,
            reason          TEXT,
            ticker          VARCHAR(10),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_risk_trader_ts
            ON trading.risk_events (trader_id, timestamp);

        CREATE TABLE IF NOT EXISTS trading.trader_watchlist (
            id              BIGSERIAL PRIMARY KEY,
            trader_id       VARCHAR(32) NOT NULL,
            ticker          VARCHAR(10) NOT NULL,
            added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS trading.system_params (
            id              BIGSERIAL PRIMARY KEY,
            trader_id       VARCHAR(32) NOT NULL,
            param_name      VARCHAR(64),
            param_value     DECIMAL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS trading.equity_snapshots (
            id              BIGSERIAL PRIMARY KEY,
            trader_id       VARCHAR(32) NOT NULL,
            date            DATE NOT NULL,
            equity          DECIMAL NOT NULL,
            cash            DECIMAL NOT NULL,
            pnl             DECIMAL NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_equity_trader_date UNIQUE (trader_id, date)
        );

        CREATE TABLE IF NOT EXISTS market_data.bars (
            id              BIGSERIAL PRIMARY KEY,
            ticker          VARCHAR(10) NOT NULL,
            timestamp       TIMESTAMPTZ NOT NULL,
            open            DECIMAL,
            high            DECIMAL,
            low             DECIMAL,
            close           DECIMAL,
            volume          BIGINT,
            interval        VARCHAR(8) NOT NULL DEFAULT '1d',
            source          VARCHAR(32) DEFAULT 'seed',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS trading.signals (
            id              BIGSERIAL PRIMARY KEY,
            trader_id       VARCHAR(32) NOT NULL,
            ticker          VARCHAR(10) NOT NULL,
            timestamp       TIMESTAMPTZ NOT NULL,
            composite_signal VARCHAR(16),
            conviction       DECIMAL,
            regime          VARCHAR(32),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """
    # Split and execute each statement
    for stmt in tables.split(";"):
        s = stmt.strip()
        if s:
            cur.execute(s + ";")
    cur.close()


def seed_agent_profiles(conn):
    """Insert trader personality profiles."""
    cur = conn.cursor()
    profiles = [
        {
            "agent_id": "kairos",
            "name": "Zara Chen",
            "company": "Kairos Capital",
            "tagline": "Precision at the turning point.",
            "identity": json.dumps({
                "core_belief": "Momentum reveals itself before the crowd sees it.",
                "approach": "Multi-timeframe momentum with ML conviction scoring.",
                "personality": "Sharp, decisive, data-obsessed.",
            }),
            "current_state": json.dumps({
                "confidence": 0.82,
                "excitement": 0.65,
                "frustration": 0.15,
                "market_appetite": "bullish",
            }),
            "performance": json.dumps({
                "wins": 18, "losses": 7, "total_trades": 25, "win_rate": 0.72,
            }),
        },
        {
            "agent_id": "aldridge",
            "name": "Edmund Whitfield",
            "company": "Aldridge & Partners",
            "tagline": "Measured. Deliberate. Enduring.",
            "identity": json.dumps({
                "core_belief": "Fundamentals drive long-term value.",
                "approach": "Value-oriented, bottom-up fundamental analysis.",
                "personality": "Measured, avuncular, occasionally pompous.",
            }),
            "current_state": json.dumps({
                "confidence": 0.71,
                "excitement": 0.32,
                "frustration": 0.22,
                "market_appetite": "neutral",
            }),
            "performance": json.dumps({
                "wins": 12, "losses": 5, "total_trades": 17, "win_rate": 0.71,
            }),
        },
        {
            "agent_id": "stonks",
            "name": "Stan Hoolihan",
            "company": "Stonks Capital",
            "tagline": "YOLO responsibly.",
            "identity": json.dumps({
                "core_belief": "Retail momentum is alpha. Follow the flow.",
                "approach": "High-conviction momentum swing trades.",
                "personality": "Loud, charismatic, high-risk-high-reward.",
            }),
            "current_state": json.dumps({
                "confidence": 0.91,
                "excitement": 0.85,
                "frustration": 0.08,
                "market_appetite": "aggressive",
            }),
            "performance": json.dumps({
                "wins": 24, "losses": 14, "total_trades": 38, "win_rate": 0.63,
            }),
        },
    ]
    for p in profiles:
        cur.execute("""
            INSERT INTO trading.agent_profile
                (agent_id, name, company, tagline, identity, current_state, performance)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (agent_id) DO UPDATE SET
                name = EXCLUDED.name, company = EXCLUDED.company,
                tagline = EXCLUDED.tagline, current_state = EXCLUDED.current_state::jsonb,
                performance = EXCLUDED.performance::jsonb,
                updated_at = NOW()
        """, (p["agent_id"], p["name"], p["company"], p["tagline"],
              p["identity"], p["current_state"], p["performance"]))
    cur.close()
    print("  ✓ agent_profiles seeded")


def seed_agent_states(conn):
    """Insert agent operational states."""
    cur = conn.cursor()
    for t in TRADERS:
        starting_cash = 10000.0
        # Random variation
        pnl = random.uniform(-500, 1500)
        equity = starting_cash + pnl
        pnl_pct = round((pnl / starting_cash) * 100, 2)
        cur.execute("""
            INSERT INTO trading.agent_state
                (agent_id, is_active, last_heartbeat, cash, equity, pnl, pnl_pct, positions_count)
            VALUES (%s, TRUE, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (agent_id) DO UPDATE SET
                is_active = TRUE, last_heartbeat = EXCLUDED.last_heartbeat,
                cash = EXCLUDED.cash, equity = EXCLUDED.equity,
                pnl = EXCLUDED.pnl, pnl_pct = EXCLUDED.pnl_pct,
                updated_at = NOW()
        """, (f"trader-{t['agent_id']}", NOW - timedelta(minutes=random.randint(1, 30)),
              max(0, starting_cash + random.uniform(-100, 200)),
              equity, round(pnl, 2), pnl_pct, random.randint(0, 3)))
    cur.close()
    print("  ✓ agent_states seeded")


def seed_portfolio_snapshots(conn):
    """Insert recent portfolio snapshots for each trader."""
    cur = conn.cursor()
    for t in TRADERS:
        trader_id = f"trader-{t['agent_id']}"
        pv = 10000.0 + random.uniform(-300, 1800)
        cash = pv * random.uniform(0.2, 0.6)
        unrel_pl = random.uniform(-200, 400)
        daily_pnl = random.uniform(-100, 300)
        positions = random.randint(1, 4)
        cur.execute("""
            INSERT INTO trading.portfolio_snapshots
                (trader_id, timestamp, cash, portfolio_value, unrealized_pl, daily_pnl, open_positions, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'db_snapshot')
        """, (trader_id, NOW - timedelta(seconds=random.randint(10, 120)),
              round(cash, 2), round(pv, 2), round(unrel_pl, 2),
              round(daily_pnl, 2), positions))
    cur.close()
    print("  ✓ portfolio_snapshots seeded")


def seed_positions(conn):
    """Insert open positions for each trader."""
    cur = conn.cursor()
    for t in TRADERS:
        positions = [
            (t["agent_id"], "AAPL", 10, 218.50, 223.75),
            (t["agent_id"], "MSFT", 5, 425.30, 432.10),
        ]
        if t["agent_id"] == "stonks":
            positions.append((t["agent_id"], "TSLA", 25, 245.00, 261.80))
            positions.append((t["agent_id"], "NVDA", 8, 130.20, 141.50))
        for pid, ticker, qty, entry, current in positions:
            mkt_val = round(qty * current, 2)
            u_pl = round(qty * (current - entry), 2)
            cur.execute("""
                INSERT INTO trading.trader_positions
                    (trader_id, ticker, quantity, market_value, unrealized_pl,
                     avg_entry_price, current_price, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'open')
            """, (pid.replace("trader-", ""), ticker, qty, mkt_val, u_pl,
                  round(entry, 2), round(current, 2)))
    cur.close()
    print("  ✓ trader_positions seeded")


def seed_decisions(conn):
    """Insert recent decisions (BUY/SELL/HOLD) for activity feed."""
    cur = conn.cursor()
    for t in TRADERS:
        actions = [
            {"action": "BUY", "ticker": "AAPL", "qty": 10, "conf": 0.85,
             "thesis": "Strong support at $215 with bullish MACD crossover on daily chart. Volume confirms accumulation pattern."},
            {"action": "HOLD", "ticker": "MSFT", "qty": 0, "conf": 0.72,
             "thesis": "Position is within target range. Waiting for breakout above $435 before adding."},
            {"action": "SELL", "ticker": "TSLA", "qty": 5, "conf": 0.63,
             "thesis": "Taking partial profits after 8% run-up. RSI above 70 suggests short-term overbought."},
        ]
        for i, a in enumerate(actions):
            cur.execute("""
                INSERT INTO trading.trader_decisions
                    (agent_id, timestamp, action, ticker, quantity, confidence, thesis, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'agent')
            """, (f"trader-{t['agent_id']}",
                  NOW - timedelta(hours=i * 3 + random.randint(0, 30)),
                  a["action"], a["ticker"], a["qty"], a["conf"], a["thesis"]))
    cur.close()
    print("  ✓ trader_decisions seeded")


def seed_orders(conn):
    """Insert order records for trade stats."""
    cur = conn.cursor()
    for t in TRADERS:
        for i in range(5):
            ticker = random.choice(TICKERS)
            action = random.choice(["buy", "sell"])
            qty = random.randint(1, 20)
            cur.execute("""
                INSERT INTO trading.orders
                    (agent_id, order_id, ticker, action, quantity, status)
                VALUES (%s, %s, %s, %s, %s, 'filled')
            """, (f"trader-{t['agent_id']}",
                  f"test-{t['agent_id']}-{i}-{random.randint(1000,9999)}",
                  ticker, action, qty))
    cur.close()
    print("  ✓ orders seeded")


def seed_journal(conn):
    """Insert journal entries for each trader."""
    cur = conn.cursor()
    entries = {
        "kairos": [
            ("Analytical", "AAPL showing strong momentum on daily timeframe. The RSI divergence we spotted yesterday is playing out. Adding to position on pullback to $218."),
            ("Confident", "Portfolio up 1.2% today. Market regime reading as bullish — VIX below 14, SPY above 50-day MA. Maintaining risk exposure at 85%."),
            ("Cautious", "NVDA running hot. Taking half position off the table. The gamma squeeze chatter on WSB is getting loud — better to be early than wrong."),
        ],
        "aldridge": [
            ("Contemplative", "AAPL's P/E of 28 feels rich but the services margin story is compelling. The installed base is a moat that's hard to replicate."),
            ("Satisfied", "BRK.B continues to compound. Treasury yields are finally stabilizing, which should support the insurance float thesis."),
            ("Wary", "Consumer staples getting hammered. PG down 3% on no real news — this feels like algorithmic selling. Might be a buying opportunity soon."),
        ],
        "stonks": [
            ("Hyped", "GME to the moon! Options chain shows heavy call buying at $30 strike for next week. Retail is back baby!"),
            ("Giddy", "Watching the order flow on TSLA — massive block trades right at close. Someone knows something. Riding the wave."),
            ("Anxious", "Took a hit on MSTR today. BTC dumping but I'm not selling. The 4-year cycle says we're still early. Diamond hands."),
        ],
    }
    for t in TRADERS:
        for i, (mood, entry) in enumerate(entries.get(t["agent_id"], [("Neutral", "No thoughts.")])):
            cur.execute("""
                INSERT INTO trading.trader_journal
                    (agent_id, timestamp, mood, entry, confidence)
                VALUES (%s, %s, %s, %s, %s)
            """, (f"trader-{t['agent_id']}",
                  NOW - timedelta(hours=i * 6 + random.randint(0, 60)),
                  mood, entry, round(random.uniform(0.5, 0.95), 2)))
    cur.close()
    print("  ✓ trader_journal seeded")


def seed_equity_snapshots(conn):
    """Insert equity history for score calculation."""
    cur = conn.cursor()
    for t in TRADERS:
        for day_offset in range(30):
            date = (NOW - timedelta(days=day_offset)).date()
            equity = 10000.0 + random.uniform(-500, 2000)
            cash = equity * random.uniform(0.3, 0.5)
            pnl = equity - 10000.0
            cur.execute("""
                INSERT INTO trading.equity_snapshots
                    (trader_id, date, equity, cash, pnl)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (trader_id, date) DO NOTHING
            """, (t["agent_id"], date, round(equity, 2),
                  round(cash, 2), round(pnl, 2)))
    cur.close()
    print("  ✓ equity_snapshots seeded")


def seed_market_data(conn):
    """Seed benchmark data (SPY/QQQ prices)."""
    cur = conn.cursor()
    benchmarks = [("SPY", 548.32), ("QQQ", 475.18)]
    for ticker, price in benchmarks:
        for day_offset in range(10):
            ts = NOW - timedelta(days=day_offset, hours=random.randint(0, 6))
            close_val = price + random.uniform(-10, 10)
            cur.execute("""
                INSERT INTO market_data.bars
                    (ticker, timestamp, open, high, low, close, volume, interval, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, '1d', 'seed')
            """, (ticker, ts, round(close_val - random.uniform(0, 2), 2),
                  round(close_val + random.uniform(0, 2), 2),
                  round(close_val - random.uniform(0, 2), 2),
                  round(close_val, 2), int(random.uniform(10e6, 50e6))))
    cur.close()
    print("  ✓ market_data.bars (benchmarks) seeded")


def seed_signals(conn):
    """Insert ML trading signals."""
    cur = conn.cursor()
    trading_signals = [
        ("AA", "bullish", 0.72, 0.65, "bull"),
        ("AAPL", "bullish", 0.68, 0.58, "bull"),
        ("MSFT", "neutral", 0.45, 0.50, "neutral"),
        ("TSLA", "bearish", 0.21, 0.38, "volatile"),
        ("NVDA", "bullish", 0.81, 0.72, "bull"),
        ("META", "neutral", 0.52, 0.48, "neutral"),
        ("GOOGL", "bullish", 0.63, 0.55, "bull"),
    ]
    for ticker, signal, conf, conv, regime in trading_signals:
        cur.execute("""
            INSERT INTO trading.signals
                (trader_id, ticker, timestamp, composite_signal, conviction, regime)
            VALUES ('kairos', %s, %s, %s, %s, %s)
        """, (ticker, NOW - timedelta(hours=random.randint(0, 12)),
              round(conf, 2), round(conv, 2), regime))
    cur.close()
    print("  ✓ trading.signals seeded")


def seed_watchlists(conn):
    """Insert watchlist tickers per trader."""
    cur = conn.cursor()
    watchlists = {
        "kairos": ["AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "AMZN", "META", "CRM", "NOW", "SNOW"],
        "aldridge": ["BRK.B", "JPM", "PG", "KO", "PEP", "WMT", "JNJ", "VZ", "HD", "CAT"],
        "stonks": ["GME", "AMC", "TSLA", "MSTR", "COIN", "RKLB", "PLTR", "SOFI", "HOOD", "DXYZ"],
    }
    for trader_id, tickers in watchlists.items():
        for ticker in tickers:
            cur.execute("""
                INSERT INTO trading.trader_watchlist (trader_id, ticker)
                VALUES (%s, %s)
            """, (trader_id, ticker))
    cur.close()
    print("  ✓ trader_watchlist seeded")


def seed_vetoes(conn):
    """Insert recent risk gate vetoes."""
    cur = conn.cursor()
    vetoes = [
        ("kairos", "BUY limit exceeded — max position size 10% of portfolio", "AAPL"),
        ("aldridge", "Daily loss limit reached at -2.3%", "MSFT"),
        ("stonks", "Drawdown exceeded 5% threshold — trade blocked", "TSLA"),
        ("kairos", "Sector concentration exceeded 40% in tech", "NVDA"),
        ("aldridge", "Volatility too high — VIX above 25", None),
    ]
    for i, (trader_id, reason, ticker) in enumerate(vetoes):
        cur.execute("""
            INSERT INTO trading.risk_events (trader_id, timestamp, vetoed, reason, ticker)
            VALUES (%s, %s, TRUE, %s, %s)
        """, (trader_id, NOW - timedelta(hours=i * 4 + 1), reason, ticker))
    cur.close()
    print("  ✓ risk_events (vetoes) seeded")


def main():
    print("Seeding test data...")
    conn = get_conn()
    ensure_schema(conn)
    ensure_tables(conn)

    seed_agent_profiles(conn)
    seed_agent_states(conn)
    seed_portfolio_snapshots(conn)
    seed_positions(conn)
    seed_decisions(conn)
    seed_orders(conn)
    seed_journal(conn)
    seed_equity_snapshots(conn)
    seed_market_data(conn)
    seed_signals(conn)
    seed_watchlists(conn)
    seed_vetoes(conn)

    conn.close()
    print("Done! Test data seeded successfully.")


if __name__ == "__main__":
    main()