#!/usr/bin/env python3
"""
src/learning_loop.py — Unified entry point: grade → analyze → synthesize → promote.

Writes ALL data to Postgres (trading.decisions, trading.trades, trading.journal,
trading.agent_profile). No more shared/trader.db SQLite.

Ties together:
  - src/journal_analyzer.py  (heuristic analysis of trader decisions)
  - src/synthesis.py          (nightly synthesis + auto-promotion)
  - src/simulator.py          (analyze_sweep, run_nightly_synthesis)

Usage (CLI):
    python3 -m src.learning_loop --agent trader-kairos      # single trader
    python3 -m src.learning_loop --all                       # all traders
    python3 -m src.learning_loop --agent trader-kairos --inject-test-data  # inject + analyze
    python3 -m src.learning_loop --health                    # check system health
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

# ── Path setup ───────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent

# Postgres connection
PG_DSN = os.getenv("PG_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://trader:@192.168.1.179:5433/trading")

sys.path.insert(0, str(PROJECT_DIR))

from src.journal_analyzer import analyze_journal, JournalInsight
from src.synthesis import synthesize_nightly, Synthesizer, NightlySummary
from src.simulator import run_nightly_synthesis as sim_run_nightly_synthesis

# ═══════════════════════════════════════════════════════════════════════════════
# DB helpers — Postgres
# ═══════════════════════════════════════════════════════════════════════════════


def get_db() -> psycopg2.extensions.connection:
    """Return a sync psycopg2 Postgres connection (NOT SQLite)."""
    dsn = os.getenv("PG_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


def get_agents() -> List[str]:
    """Fetch unique trader_ids from trading.agent_profile in Postgres."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT agent_id FROM trading.agent_profile ORDER BY agent_id")
        agents = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    # Fallback: if no profiles exist yet, return known live traders
    if not agents:
        agents = ["trader-kairos", "trader-aldridge", "trader-stonks"]
    return agents


def get_decisions(agent_id: str, limit: int = 50) -> List[Dict]:
    """Fetch decisions from Postgres trading.decisions.

    Maps Postgres columns to the legacy SQLite column naming
    that the analysis code expects (agent_id, action, confidence, thesis, mood).
    """
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """SELECT trader_id AS agent_id,
                      timestamp,
                      decision AS action,
                      ticker,
                      conviction AS confidence,
                      rationale AS thesis,
                      created_at
               FROM trading.decisions
               WHERE trader_id = %s
               ORDER BY timestamp DESC
               LIMIT %s""",
            (agent_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        # Enhance with mood, source, signals_used fields that Postgres may not have
        # (they come from the agent's LLM output, stored as rationale)
        for row in rows:
            row.setdefault("mood", "analytical")
            row.setdefault("source", "live")
            row.setdefault("signals_used", json.dumps([]))
            # Parse quantity from rationale or default to 0
            row.setdefault("quantity", 0)
            # tick_number
            row.setdefault("tick_number", 0)
        return rows
    finally:
        conn.close()


def get_trades(agent_id: str, limit: int = 50) -> List[Dict]:
    """Fetch trades from Postgres trading.trades (or trading.executed_trades).

    Maps Postgres columns to the legacy SQLite naming (agent_id, entry_price,
    entry_timestamp, pnl, pnl_pct, status).
    """
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # Try trading.trades first (post-migration schema)
        cur.execute(
            """SELECT trader_id AS agent_id,
                      ticker,
                      entry_price,
                      entry_time AS entry_timestamp,
                      exit_price,
                      exit_time AS exit_timestamp,
                      shares AS quantity,
                      pnl,
                      return_pct AS pnl_pct,
                      entry_time AS timestamp,
                      CASE WHEN exit_time IS NULL THEN 'open' ELSE 'closed' END AS status
               FROM trading.trades
               WHERE trader_id = %s AND pnl IS NOT NULL
               ORDER BY entry_time DESC
               LIMIT %s""",
            (agent_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]

        if not rows:
            # Fallback to trading.executed_trades (older schema)
            cur.execute(
                """SELECT agent_id,
                          ticker,
                          action,
                          quantity,
                          entry_price,
                          exit_price,
                          entry_time AS entry_timestamp,
                          exit_time AS exit_timestamp,
                          pnl,
                          pnl_pct,
                          entry_reason,
                          exit_reason,
                          status,
                          entry_time AS timestamp
                   FROM trading.executed_trades
                   WHERE agent_id = %s
                   ORDER BY entry_time DESC
                   LIMIT %s""",
                (agent_id, limit),
            )
            rows = [dict(r) for r in cur.fetchall()]

        return rows
    finally:
        conn.close()


def get_journal(agent_id: str, limit: int = 50) -> List[str]:
    """Fetch journal entries from Postgres trading.journal.

    Returns the rationale (entry text) as a flat list, matching
    the legacy get_journal() contract.
    """
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """SELECT rationale, timestamp
               FROM trading.journal
               WHERE trader_id = %s
               ORDER BY timestamp DESC
               LIMIT %s""",
            (agent_id, limit),
        )
        return [dict(r)["rationale"] for r in cur.fetchall() if dict(r).get("rationale")]
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Test data injection — now writes to Postgres
# ═══════════════════════════════════════════════════════════════════════════════

TEST_DECISIONS = {
    "trader-kairos": [
        {
            "action": "BUY",
            "ticker": "NVDA",
            "quantity": 10,
            "confidence": 0.75,
            "thesis": "Momentum breakout on above-average volume — MACD bullish crossover, strong sector rotation into semiconductors",
            "mood": "excited",
            "signals_used": ["momentum_breakout", "volume_surge", "macd_bullish_crossover"],
        },
        {
            "action": "BUY",
            "ticker": "AMD",
            "quantity": 15,
            "confidence": 0.65,
            "thesis": "Relative strength against NVDA, catching up on AI chip demand narrative. RSI at 58 — room to run",
            "mood": "optimistic",
            "signals_used": ["relative_strength", "sector_rotation", "rsi_mid"],
        },
        {
            "action": "SELL",
            "ticker": "NVDA",
            "quantity": 10,
            "confidence": 0.70,
            "thesis": "Hit 8% profit target. Momentum stalling — volume declining, RSI hit 72. Lock in gains",
            "mood": "satisfied",
            "signals_used": ["profit_target", "rsi_overbought", "volume_decline"],
        },
        {
            "action": "BUY",
            "ticker": "AVGO",
            "quantity": 8,
            "confidence": 0.55,
            "thesis": "Momentum signal from data bus — unusual options flow detected. Heavy call buying before earnings",
            "mood": "curious",
            "signals_used": ["unusual_options_flow", "earnings_drift", "momentum"],
        },
        {
            "action": "HOLD",
            "ticker": "",
            "quantity": 0,
            "confidence": 0.40,
            "thesis": "No strong signals across watchlist. AMD position still developing. Waiting for clearer setup",
            "mood": "patient",
            "signals_used": ["no_candidates"],
        },
    ],
    "trader-aldridge": [
        {
            "action": "BUY",
            "ticker": "JPM",
            "quantity": 5,
            "confidence": 0.72,
            "thesis": "Banking sector oversold — P/E at 5-year low, strong insider buying reported. Dividend yield attractive at current levels",
            "mood": "thoughtful",
            "signals_used": ["fundamental_value", "rsi_oversold", "insider_buying"],
        },
        {
            "action": "BUY",
            "ticker": "KO",
            "quantity": 20,
            "confidence": 0.80,
            "thesis": "Core position. Coca-Cola at support levels with strong balance sheet. Defensive play in uncertain market",
            "mood": "confident",
            "signals_used": ["core_position", "support_level", "defensive_quality"],
        },
        {
            "action": "BUY",
            "ticker": "PG",
            "quantity": 15,
            "confidence": 0.78,
            "thesis": "Consumer staple at reasonable valuation. P&G has pricing power and consistent dividend growth. Patricia approved this pick",
            "mood": "measured",
            "signals_used": ["dividend_growth", "pricing_power", "sector_stability"],
        },
        {
            "action": "HOLD",
            "ticker": "",
            "quantity": 0,
            "confidence": 0.55,
            "thesis": "Core positions held. KO and PG performing as expected. Monitoring JPM for additional entry. No rush to deploy capital",
            "mood": "patient",
            "signals_used": ["portfolio_balance"],
        },
        {
            "action": "HOLD",
            "ticker": "",
            "quantity": 0,
            "confidence": 0.50,
            "thesis": "Market choppy. The Committee discussed increasing cash buffer to 15%. Waiting for clearer fundamental signals",
            "mood": "cautious",
            "signals_used": ["macro_uncertainty", "portfolio_defense"],
        },
    ],
    "trader-stonks": [
        {
            "action": "BUY",
            "ticker": "GME",
            "quantity": 25,
            "confidence": 0.60,
            "thesis": "Social media heating up — heavy call buying detected on unusual options flow. WSB mentions spiking. YOLO energy is real",
            "mood": "pumped",
            "signals_used": ["social_momentum", "unusual_options_flow", "wsb_mentions"],
        },
        {
            "action": "BUY",
            "ticker": "DJT",
            "quantity": 20,
            "confidence": 0.45,
            "thesis": "Bluesky chatter picking up. Low float + high short interest = squeeze potential. Risky but could moon",
            "mood": "reckless",
            "signals_used": ["social_sentiment", "short_squeeze_potential", "low_float"],
        },
        {
            "action": "SELL",
            "ticker": "GME",
            "quantity": 25,
            "confidence": 0.65,
            "thesis": "GME hit 12% gain in 2 hours. Volume fading, WSB mentions cooling. Taking profits before the dump",
            "mood": "giddy",
            "signals_used": ["profit_target", "volume_fade", "sentiment_peak"],
        },
        {
            "action": "HOLD",
            "ticker": "",
            "quantity": 0,
            "confidence": 0.30,
            "thesis": "Everything looks dead. No social pickup, no flow. Staying in cash until something interesting happens",
            "mood": "bored",
            "signals_used": ["no_signals"],
        },
        {
            "action": "SELL",
            "ticker": "DJT",
            "quantity": 20,
            "confidence": 0.55,
            "thesis": "Stop loss triggered at -8%. Thesis didn't play out — social hype never materialized into sustained volume. Cut losses",
            "mood": "disappointed",
            "signals_used": ["stop_loss", "thesis_failed"],
        },
    ],
}

TEST_TRADES = {
    "trader-kairos": [
        {"ticker": "NVDA", "action": "buy", "quantity": 10, "entry_price": 128.50, "status": "closed", "exit_price": 138.78, "pnl": 102.80, "pnl_pct": 8.0, "entry_reason": "momentum_breakout", "exit_reason": "profit_target"},
        {"ticker": "AMD", "action": "buy", "quantity": 15, "entry_price": 156.20, "status": "open", "pnl": 0, "pnl_pct": 0},
        {"ticker": "AVGO", "action": "buy", "quantity": 8, "entry_price": 182.40, "status": "closed", "exit_price": 175.10, "pnl": -58.40, "pnl_pct": -4.0, "entry_reason": "options_flow", "exit_reason": "stop_loss"},
    ],
    "trader-aldridge": [
        {"ticker": "JPM", "action": "buy", "quantity": 5, "entry_price": 215.30, "status": "open", "pnl": 0, "pnl_pct": 0},
        {"ticker": "KO", "action": "buy", "quantity": 20, "entry_price": 68.40, "status": "closed", "exit_price": 72.15, "pnl": 75.00, "pnl_pct": 5.48, "entry_reason": "fundamental_value", "exit_reason": "profit_target"},
        {"ticker": "PG", "action": "buy", "quantity": 15, "entry_price": 172.80, "status": "open", "pnl": 0, "pnl_pct": 0},
    ],
    "trader-stonks": [
        {"ticker": "GME", "action": "buy", "quantity": 25, "entry_price": 28.40, "status": "closed", "exit_price": 31.81, "pnl": 85.25, "pnl_pct": 12.0, "entry_reason": "social_momentum", "exit_reason": "profit_target"},
        {"ticker": "DJT", "action": "buy", "quantity": 20, "entry_price": 42.50, "status": "closed", "exit_price": 39.10, "pnl": -68.00, "pnl_pct": -8.0, "entry_reason": "social_sentiment", "exit_reason": "stop_loss"},
    ],
}


def inject_test_data(agent_id: str):
    """Inject realistic test decisions and trades into Postgres."""
    conn = get_db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    decisions = TEST_DECISIONS.get(agent_id, [])
    trades = TEST_TRADES.get(agent_id, [])

    if not decisions:
        print(f"  No test data defined for {agent_id}")
        conn.close()
        return

    decision_ids = []
    for i, dec in enumerate(decisions):
        ts = now
        signals_json = json.dumps(dec.get("signals_used", []))
        action = dec["action"]
        ticker = dec.get("ticker", "")
        conviction = float(dec.get("confidence", 0.5))
        rationale = dec.get("thesis", "")
        cur.execute(
            """INSERT INTO trading.decisions
               (trader_id, ticker, timestamp, decision, conviction, rationale, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (agent_id, ticker, ts, action, conviction, rationale, ts),
        )
        decision_ids.append(cur.fetchone()[0])

    for i, trade in enumerate(trades):
        ts = now
        trade_id = f"test-{uuid.uuid4().hex[:12]}"
        action = trade.get("action", "buy")
        if trade["status"] == "open":
            cur.execute(
                """INSERT INTO trading.trades
                   (trader_id, trade_id, ticker, entry_time, entry_price, shares,
                    pnl, return_pct, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    agent_id,
                    trade_id,
                    trade["ticker"],
                    ts,
                    trade["entry_price"],
                    trade["quantity"],
                    0.0,
                    0.0,
                    ts,
                ),
            )
        else:
            pnl = trade.get("pnl", 0)
            pnl_pct = trade.get("pnl_pct", 0)
            cur.execute(
                """INSERT INTO trading.trades
                   (trader_id, trade_id, ticker, entry_time, exit_time,
                    entry_price, exit_price, shares, pnl, return_pct, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    agent_id,
                    trade_id,
                    trade["ticker"],
                    ts,
                    ts,
                    trade["entry_price"],
                    trade.get("exit_price", 0),
                    trade["quantity"],
                    pnl,
                    pnl_pct,
                    ts,
                ),
            )

    # Add journal entries
    journal_entries = {
        "trader-kairos": [
            "NVDA breakout was textbook — MACD crossover on volume surge. Took profit at 8% per plan. AMD still developing.",
            "AVGO was a mistake. Options flow looked good but the thesis was thin. Stopped out at -4%. Note: don't chase earnings drift without fundamentals.",
            "Portfolio: 63% cash, 1 open position (AMD). Conviction moderate. Sector rotation favoring semis.",
        ],
        "trader-aldridge": [
            "JPM position opened at $215.30. P/E at 5-year low provides margin of safety. Monitoring for additional entry.",
            "KO exited at $72.15 for +5.48%. Thesis played out — defensive rotation into staples materialized as expected.",
            "Committee notes: cash position at 72%. Considering increasing to 5 positions with some mid-cap value names per Patricia's recommendation.",
        ],
        "trader-stonks": [
            "GME was a banger — +12% in 2 hours. Social sentiment peaked and I got out at the right time. WSB calls it a 'diamond hands moment' but I'm taking the money.",
            "DJT was a L. Entry thesis was thin — Bluesky chatter isn't enough. Social momentum needs to be backed by volume. -8% hit stop loss. Lesson learned.",
            "In cash now. Waiting for the next hype cycle. Flow is quiet across the board.",
        ],
    }

    entries = journal_entries.get(agent_id, [])
    for entry in entries:
        ts = now
        cur.execute(
            """INSERT INTO trading.journal
               (trader_id, timestamp, ticker, decision, rationale, equity, drawdown_pct, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (agent_id, ts, "", "REFLECTION", entry, 0.0, 0.0, ts),
        )

    conn.commit()
    conn.close()

    n_dec = len(decisions)
    n_trd = len(trades)
    n_jnl = len(entries)
    print(f"  Injected {n_dec} decisions, {n_trd} trades, {n_jnl} journal entries for {agent_id} into Postgres")


# ═══════════════════════════════════════════════════════════════════════════════
# Learning loop core
# ═══════════════════════════════════════════════════════════════════════════════


def run_for_agent(agent_id: str) -> Dict[str, Any]:
    """Full learning loop for one agent: grade → analyze → synthesize."""
    print(f"\n{'='*60}")
    print(f"📊 Learning Loop — {agent_id}")
    print(f"{'='*60}")

    decisions = get_decisions(agent_id)
    trades = get_trades(agent_id)
    journal = get_journal(agent_id)

    print(f"  Decisions: {len(decisions)}")
    print(f"  Trades:    {len(trades)}")
    print(f"  Journal:   {len(journal)}")

    # Initialize variables used across steps (avoid NameError when data is sparse)
    insights = []
    high_conviction = []
    low_conviction = []
    win_rate = 0
    total_pnl = 0.0

    # Step 1: Journal analysis
    print(f"\n  🔍 Journal Analysis...")
    if journal:
        insights = analyze_journal(journal, [], trades)
        if isinstance(insights, list):
            for ins in insights:
                if isinstance(ins, JournalInsight):
                    print(f"    • [{ins.category}] {ins.description[:120]}...")
                else:
                    print(f"    • {str(ins)[:120]}")
        elif isinstance(insights, dict):
            for k, v in insights.items():
                print(f"    • {k}: {str(v)[:120]}")
        else:
            print(f"    • {str(insights)[:120]}")
    else:
        print(f"    No journal entries found")

    # Step 2: Decision analysis
    print(f"\n  📈 Decision Analysis...")
    if decisions:
        buys = sum(1 for d in decisions if d["action"] == "BUY")
        sells = sum(1 for d in decisions if d["action"] == "SELL")
        holds = sum(1 for d in decisions if d["action"] == "HOLD")
        avg_conf = sum(d.get("confidence") or 0 for d in decisions) / len(decisions) if decisions else 0
        print(f"    BUY: {buys}  SELL: {sells}  HOLD: {holds}")
        print(f"    Avg confidence: {avg_conf:.2f}")

        # Find patterns
        high_conviction = [d for d in decisions if d.get("confidence") and d["confidence"] >= 0.7]
        low_conviction = [d for d in decisions if d.get("confidence") and d["confidence"] < 0.5]
        if high_conviction:
            print(f"    High-conviction trades: {len(high_conviction)}")
            for d in high_conviction[:3]:
                signals_raw = d.get("signals_used", "[]")
                signals = json.loads(signals_raw) if isinstance(signals_raw, str) else (signals_raw or [])
                print(f"      • {d['action']} {d['ticker']} (conf:{d['confidence']:.2f}) — {', '.join(signals[:3])}")
        if low_conviction:
            print(f"    Low-conviction trades: {len(low_conviction)}")

        # Check for regime weakness patterns
        losing_signals = {}
        for d in decisions:
            if d.get("confidence") and d["confidence"] < 0.5:
                signals_raw = d.get("signals_used", "[]")
                signals = json.loads(signals_raw) if isinstance(signals_raw, str) else (signals_raw or [])
                for s in signals:
                    losing_signals[s] = losing_signals.get(s, 0) + 1
        if losing_signals:
            worst_signals = sorted(losing_signals.items(), key=lambda x: x[1], reverse=True)[:3]
            print(f"    Worst-performing signal categories:")
            for signal, count in worst_signals:
                print(f"      • {signal}: appeared in {count} low-conviction decisions")

    # Step 3: Trade P&L analysis
    print(f"\n  💰 P&L Analysis...")
    if trades:
        total_pnl = sum(t.get("pnl", 0) or 0 for t in trades)
        wins = [t for t in trades if (t.get("pnl", 0) or 0) > 0]
        losses = [t for t in trades if (t.get("pnl", 0) or 0) < 0]
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        best = max(trades, key=lambda t: t.get("pnl", 0) or 0) if trades else {}
        worst = min(trades, key=lambda t: t.get("pnl", 0) or 0) if trades else {}
        print(f"    Total P&L: ${total_pnl:.2f}")
        print(f"    Win rate:  {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)")
        print(f"    Best:      {best.get('ticker','?')} ${best.get('pnl',0):.2f}")
        print(f"    Worst:     {worst.get('ticker','?')} ${worst.get('pnl',0):.2f}")
    else:
        print(f"    No trades found")
        total_pnl = 0.0
        win_rate = 0

    # Step 4: Synthesis
    print(f"\n  🧬 Synthesis...")
    try:
        insights_for_synth = insights if isinstance(insights, list) else []
        trader_insights_dict = {agent_id: [i for i in insights_for_synth if isinstance(i, JournalInsight)]}
        scenarios_dict = {agent_id: {'decisions': decisions, 'trades': trades}}
        synth = synthesize_nightly(trader_insights_dict, scenarios_dict) if insights_for_synth else None
        if synth:
            promos = getattr(synth, 'promotions', getattr(synth, 'promoted_insights', []))
            if promos:
                print(f"    Promotions: {len(promos)}")
                for p in promos[:3]:
                    print(f"      • {p}")
            else:
                print(f"    Synthesis: no promotions")
        else:
            print(f"    No synthesis results")
    except Exception as e:
        print(f"    Synthesis: {e}")

    # Step 5: Learning signals
    print(f"\n  📝 Learning Signals...")
    signals = []

    if trades:
        if win_rate < 40:
            signals.append("⚠️  LOW WIN RATE — Review entry criteria. Consider tightening signal filters.")
        elif win_rate > 60:
            signals.append("✅ GOOD WIN RATE — Current strategy generating positive results.")

    if high_conviction:
        hv_buys = [d for d in high_conviction if d["action"] == "BUY"]
        hv_profit = sum(
            1 for d in high_conviction
            if any(
                t.get("pnl", 0) or 0 > 0
                for t in trades
                if t.get("ticker") == d.get("ticker")
            )
        )
        if hv_buys and hv_profit < len(hv_buys) * 0.5:
            signals.append("⚠️  HIGH CONVICTION ≠ HIGH ACCURACY — Confidence calibration needs adjustment.")

    # Market-specific signals
    if decisions:
        recent = decisions[:3]
        hold_only = all(d["action"] == "HOLD" for d in recent)
        if hold_only:
            signals.append("ℹ️  CONSISTENT HOLD — Agent is cautious. Check if this is risk management or paralysis.")

    if not signals:
        signals.append("✅ No critical issues detected. Baseline performance within acceptable range.")

    for sig in signals:
        print(f"    {sig}")

    result = {
        "agent_id": agent_id,
        "decisions_count": len(decisions),
        "trades_count": len(trades),
        "journal_count": len(journal),
        "win_rate": win_rate if trades else 0,
        "total_pnl": sum(t.get("pnl", 0) or 0 for t in trades),
        "signals": signals,
        "status": "ok",
    }
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def health_check():
    """Check system health — Postgres connections, schema, profiles."""
    print(f"{'='*60}")
    print(f"🏥 Health Check (Postgres)")
    print(f"{'='*60}")

    # DB check
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        conn.close()
        print(f"  ✅ Postgres: connected")
    except Exception as e:
        print(f"  ❌ Postgres: {e}")
        return

    # Agent profiles
    agents = get_agents()
    print(f"  ✅ Agent profiles: {len(agents)} — {', '.join(agents)}")

    # Schema — check Postgres schemas
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT table_schema, table_name, pg_size_pretty(pg_total_relation_size(quote_ident(table_schema) || '.' || quote_ident(table_name))) as size,
                   (SELECT reltuples::bigint FROM pg_class WHERE oid = (quote_ident(table_schema) || '.' || quote_ident(table_name))::regclass) as rows_est
            FROM information_schema.tables
            WHERE table_schema IN ('market_data', 'trading')
            ORDER BY table_schema, table_name
        """)
        tables = cur.fetchall()
        print(f"  ✅ Tables: {len(tables)}")
        for schema, name, size, rows in tables:
            print(f"      {schema}.{name}: ~{rows or 0} rows ({size})")
    except Exception as e:
        print(f"  ⚠️  Schema check: {e}")
    finally:
        conn.close()

    # Check existing imports
    print(f"\n  📦 Module check:")
    for mod in ["journal_analyzer", "synthesis", "simulator"]:
        try:
            __import__(f"src.{mod}")
            print(f"    ✅ src.{mod}")
        except Exception as e:
            print(f"    ❌ src.{mod}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Learning Loop — grade → analyze → synthesize")
    parser.add_argument("--agent", help="Single agent id (e.g. trader-kairos)")
    parser.add_argument("--all", action="store_true", help="Run for all agents")
    parser.add_argument("--inject-test-data", action="store_true", help="Inject test decisions/trades before running")
    parser.add_argument("--health", action="store_true", help="System health check")

    args = parser.parse_args()

    start = time.time()

    if args.health:
        health_check()
        return

    # Determine agents
    if args.agent:
        agents = [args.agent]
    elif args.all:
        agents = get_agents()
    else:
        # Default: all agents
        agents = get_agents()

    if not agents:
        print("No agents found. Run with --health to check database state.")
        return

    # Optionally inject test data
    if args.inject_test_data:
        print("Injecting test data...")
        for agent in agents:
            inject_test_data(agent)
        print()

    # Run learning loop for each agent
    results = []
    for agent in agents:
        try:
            result = run_for_agent(agent)
            results.append(result)
        except Exception as e:
            print(f"\n  ❌ Error for {agent}: {e}")
            import traceback
            traceback.print_exc()

    # Summary
    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"✅ Learning loop complete ({elapsed:.1f}s)")
    for r in results:
        print(f"  {r['agent_id']}: {r['trades_count']} trades, ${r['total_pnl']:.2f} P&L, "
              f"{r['win_rate']:.0f}% win rate — {len(r['signals'])} learning signals")
    print()


if __name__ == "__main__":
    main()