#!/usr/bin/env python3
"""Quick targeted migration from SQLite → Postgres using correct schema mappings.

Tables migrated: trades, decisions, journal, equity_snapshots
"""
import sqlite3, psycopg2, psycopg2.extras

SQLITE = "/home/openclaw/projects/paper-trading-teams/shared/trader.db"
PG_DSN = os.getenv("QUICK_MIGRATE_DB_URL", "postgresql://trader:***@trading-db:5432/trading")

sl = sqlite3.connect(SQLITE)
sl.row_factory = sqlite3.Row
pg = psycopg2.connect(PG_DSN)
pg.autocommit = True
cur = pg.cursor()
cur.execute("SET search_path TO trading, public")

def safe(v, default=None):
    """Handle sqlite3.Row .get() not existing."""
    try:
        return v if v is not None else default
    except:
        return default

counts = {}

# ── trades ──────────────────────────────────────────
try:
    rows = list(sl.execute("""
        SELECT id, agent_id, ticker, action, quantity, entry_price, entry_timestamp,
               exit_price, exit_timestamp, pnl, pnl_pct, decision_id, entry_reason, exit_reason
        FROM trades WHERE status IN ('closed', 'open')"""))

    for r in rows:
        cur.execute("""
            INSERT INTO trading.trades
                (trader_id, ticker, entry_time, exit_time, entry_price, exit_price,
                 shares, pnl, return_pct, buy_decision_id, regime, trade_source, trade_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (
            r["agent_id"], r["ticker"][:10],
            r["entry_timestamp"], r["exit_timestamp"],
            safe(r["entry_price"], 0), safe(r["exit_price"], 0),
            safe(r["quantity"], 0), safe(r["pnl"], 0), safe(r["pnl_pct"], 0),
            r["decision_id"] if r["action"] == "buy" else None,
            r["entry_reason"][:32] if r["entry_reason"] else "migration",
            "migration",
            f"mig-{r['id']}"
        ))
    pg.commit()
    counts["trades"] = len(rows)
    print(f"  trades: {len(rows)} rows")
except Exception as e:
    print(f"  trades: SKIP — {e}")
    pg.rollback()

# ── decisions ───────────────────────────────────────
try:
    rows = list(sl.execute("""
        SELECT agent_id, timestamp, COALESCE(action,'HOLD') as action, ticker,
               COALESCE(confidence,0) as confidence, COALESCE(thesis,'') as thesis,
               COALESCE(reasoning,'') as reasoning
        FROM decisions ORDER BY id"""))

    for r in rows:
        decision = (r["action"] or "HOLD")[:16]
        tkr = (r["ticker"] or "?")[:10]
        cur.execute("""
            INSERT INTO trading.decisions (trader_id, ticker, timestamp, decision, conviction, rationale)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (r["agent_id"], tkr, r["timestamp"], decision,
              safe(r["confidence"], 0),
              ((r["thesis"] or "") + " " + (r["reasoning"] or "")).strip()[:500] or "migrated"))
    pg.commit()
    counts["decisions"] = len(rows)
    print(f"  decisions: {len(rows)} rows")
except Exception as e:
    print(f"  decisions: SKIP — {e}")
    pg.rollback()

# ── journal ─────────────────────────────────────────
try:
    rows = list(sl.execute("""
        SELECT agent_id, timestamp, COALESCE(mood,'NOTE') as mood, COALESCE(entry,'') as entry
        FROM journal ORDER BY id"""))

    for r in rows:
        cur.execute("""
            INSERT INTO trading.journal (trader_id, timestamp, ticker, decision, rationale, equity, drawdown_pct)
            VALUES (%s, %s, %s, %s, %s, 0, 0)
            ON CONFLICT (id) DO NOTHING
        """, (r["agent_id"], r["timestamp"], "ALL",
              safe(r["mood"], "NOTE")[:16], safe(r["entry"], "") or "migrated"))
    pg.commit()
    counts["journal"] = len(rows)
    print(f"  journal: {len(rows)} rows")
except Exception as e:
    print(f"  journal: SKIP — {e}")
    pg.rollback()

# ── equity_snapshots (from portfolio_snapshots) ─────
try:
    rows = list(sl.execute("""
        SELECT agent_id, timestamp, cash, portfolio_value, unrealized_pl, daily_pnl
        FROM portfolio_snapshots ORDER BY id"""))

    for r in rows:
        cur.execute("""
            INSERT INTO trading.equity_snapshots (trader_id, date, equity, cash, pnl)
            VALUES (%s, %s::date, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (r["agent_id"], r["timestamp"][:10] if r["timestamp"] else "2025-01-01",
              safe(r["portfolio_value"], 0), safe(r["cash"], 0), safe(r["daily_pnl"], 0)))
    pg.commit()
    counts["equity_snapshots"] = len(rows)
    print(f"  equity_snapshots: {len(rows)} rows")
except Exception as e:
    print(f"  equity_snapshots: SKIP — {e}")
    pg.rollback()

pg.close()
sl.close()

print(f"\n=== MIGRATION COMPLETE: {sum(counts.values())} total rows ===")
for t, n in counts.items():
    print(f"  {t}: {n}")
