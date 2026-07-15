#!/usr/bin/env python3
"""
reflection_cron — End-of-day reflection generator for paper trading agents.

Generates structured markdown reflections for each agent, analyzing:
  - Today's P&L, win rate, avg hold time, avg position size
  - Win rate by signal (from trade_signals table)
  - Win rate by sector (via fundamentals or ticker→sector mapping)
  - Rolling win rate (last 10/50/100 trades)
  - Confidence calibration curve
  - Strategy suggestions

CLI:
  python3 src/reflection_cron.py --dry-run --agent trader-kairos
  python3 src/reflection_cron.py --agent trader-kairos     # actually writes to DB
  python3 src/reflection_cron.py --all                      # all active agents

Scheduler (imported by data_bus.py):
  schedule_reflection_cron() — daemon thread that checks every minute
  if it's 16:30 ET, then runs reflection for all active traders.
"""

import os
import sys
import json
import time
import argparse
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict
from pathlib import Path

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [reflection] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("reflection")

# ── Postgres connection ──────────────────────────────────────────────────────
_DB_DSN = os.getenv(
    "REFLECTION_DB_DSN",
    "host=trading-db port=5432 dbname=trading user=trader",
)

_KNOWN_SECTORS: Dict[str, str] = {
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology", "GOOG": "Technology",
    "META": "Technology", "NVDA": "Technology", "AMD": "Technology", "AMZN": "Technology",
    "NFLX": "Technology", "CRM": "Technology", "ADBE": "Technology", "INTC": "Technology",
    "IBM": "Technology", "CSCO": "Technology", "ORCL": "Technology", "QCOM": "Technology",
    "AVGO": "Technology", "TXN": "Technology", "NOW": "Technology", "SHOP": "Technology",
    "PLTR": "Technology", "SNAP": "Technology", "UBER": "Technology", "LYFT": "Technology",
    "PINS": "Technology", "DASH": "Technology", "SQ": "Technology", "PYPL": "Technology",
    "MSTR": "Technology", "COIN": "Technology", "RIVN": "Technology", "TSLA": "Consumer_Cyclical",
    "JPM": "Financial", "BAC": "Financial", "WFC": "Financial", "GS": "Financial",
    "MS": "Financial", "C": "Financial", "SCHW": "Financial", "AXP": "Financial",
    "V": "Financial", "MA": "Financial", "BLK": "Financial", "JPM": "Financial",
    "PFE": "Healthcare", "MRNA": "Healthcare", "JNJ": "Healthcare", "LLY": "Healthcare",
    "ABBV": "Healthcare", "BMY": "Healthcare", "GILD": "Healthcare", "UNH": "Healthcare",
    "AMGN": "Healthcare", "NVO": "Healthcare", "XOM": "Energy", "CVX": "Energy",
    "SHEL": "Energy", "BP": "Energy", "COP": "Energy", "SLB": "Energy",
    "WMT": "Consumer_Defensive", "COST": "Consumer_Defensive", "KO": "Consumer_Defensive",
    "PEP": "Consumer_Defensive", "PG": "Consumer_Defensive", "MCD": "Consumer_Cyclical",
    "SBUX": "Consumer_Cyclical", "DIS": "Consumer_Cyclical", "NKE": "Consumer_Cyclical",
    "HD": "Consumer_Cyclical", "LOW": "Consumer_Cyclical", "TGT": "Consumer_Cyclical",
    "BA": "Industrial", "CAT": "Industrial", "GE": "Industrial", "RTX": "Industrial",
    "LMT": "Industrial", "NOC": "Industrial", "GD": "Industrial", "HON": "Industrial",
    "UPS": "Industrial", "FDX": "Industrial", "DE": "Industrial", "SPY": "ETF", "QQQ": "ETF",
    "IWM": "ETF", "DIA": "ETF", "TLT": "Fixed_Income", "GLD": "Commodities",
    "SLV": "Commodities", "USO": "Commodities", "VTI": "ETF", "VOO": "ETF",
    "BND": "Fixed_Income", "VNQ": "Real_Estate", "XLF": "ETF", "XLK": "ETF",
    "XLE": "ETF", "XLV": "ETF", "XLI": "ETF", "ARKK": "ETF", "GME": "Consumer_Cyclical",
    "AMC": "Consumer_Cyclical", "SMCI": "Technology", "HOOD": "Financial",
    "MAR": "Consumer_Cyclical", "ABNB": "Consumer_Cyclical", "ROKU": "Technology",
    "ZM": "Technology", "DOCU": "Technology", "MELI": "Technology", "SE": "Technology",
    "F": "Consumer_Cyclical", "GM": "Consumer_Cyclical", "LCID": "Consumer_Cyclical",
    "CHWY": "Consumer_Cyclical", "DDOG": "Technology", "CRWD": "Technology",
    "PANW": "Technology", "ZS": "Technology", "NET": "Technology", "DASH": "Technology",
    "AI": "Technology", "PLTR": "Technology", "SOFI": "Financial", "ASTS": "Technology",
    "RKLB": "Technology", "WDC": "Technology", "MU": "Technology", "MARA": "Technology",
    "SPCE": "Industrial", "WBD": "Communication", "PARA": "Communication",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _get_pg_conn():
    """Get a psycopg2 connection (sync) to the trading database.

    Returns None on failure (graceful fallback).
    """
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(_DB_DSN, connect_timeout=5)
        conn.set_session(readonly=True, autocommit=True)
        return conn
    except Exception as e:
        log.warning("Postgres connection failed: %s", e)
        return None


def _get_pg_writer():
    """Get a writable psycopg2 connection to the trading database.

    Returns None on failure (graceful fallback).
    """
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(_DB_DSN, connect_timeout=5)
        conn.autocommit = True
        return conn
    except Exception as e:
        log.warning("Postgres writer connection failed: %s", e)
        return None


def _ticker_sector(ticker: str) -> str:
    """Look up sector for a ticker using known mapping, fallback to DB, then Unknown."""
    t = ticker.upper()
    if t in _KNOWN_SECTORS:
        return _KNOWN_SECTORS[t]
    # Fallback: try fundamentals table
    try:
        conn = _get_pg_conn()
        if conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT sector FROM market_data.fundamentals
                       WHERE ticker = %s AND sector IS NOT NULL
                       ORDER BY fetched_at DESC LIMIT 1""",
                    (t,),
                )
                row = cur.fetchone()
            conn.close()
            if row and row["sector"]:
                _KNOWN_SECTORS[t] = row["sector"]
                return row["sector"]
    except Exception:
        pass
    return "Unknown"


def _get_trades(agent_id: str, limit: int = 100, since: Optional[date] = None) -> List[dict]:
    """Fetch trades for an agent from Postgres.

    Returns list of dicts with trade fields.
    """
    conn = _get_pg_conn()
    if not conn:
        return []

    from psycopg2.extras import RealDictCursor
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if since:
                cur.execute(
                    """SELECT t.*, d.conviction as entry_conviction,
                              d2.conviction as exit_conviction
                       FROM trading.trades t
                       LEFT JOIN trading.decisions d ON d.id = t.buy_decision_id
                       LEFT JOIN trading.decisions d2 ON d2.id = t.sell_decision_id
                       WHERE t.trader_id = %s
                         AND t.exit_time IS NOT NULL
                         AND t.exit_time >= %s::date
                       ORDER BY t.exit_time DESC
                       LIMIT %s""",
                    (agent_id, since.isoformat(), limit),
                )
            else:
                cur.execute(
                    """SELECT t.*, d.conviction as entry_conviction,
                              d2.conviction as exit_conviction
                       FROM trading.trades t
                       LEFT JOIN trading.decisions d ON d.id = t.buy_decision_id
                       LEFT JOIN trading.decisions d2 ON d2.id = t.sell_decision_id
                       WHERE t.trader_id = %s
                         AND t.exit_time IS NOT NULL
                       ORDER BY t.exit_time DESC
                       LIMIT %s""",
                    (agent_id, limit),
                )
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("Failed to fetch trades for %s: %s", agent_id, e)
        try:
            conn.close()
        except Exception:
            pass
        return []


def _get_trade_signals(trade_ids: List[int]) -> Dict[int, List[dict]]:
    """Fetch signals associated with a batch of trade IDs.

    Returns {trade_id: [signal_dict, ...]}
    """
    if not trade_ids:
        return {}

    conn = _get_pg_conn()
    if not conn:
        return {}

    from psycopg2.extras import RealDictCursor
    try:
        placeholders = ",".join(["%s"] * len(trade_ids))
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""SELECT trade_id, signal_name, signal_value, confidence_at_time
                    FROM trading.trade_signals
                    WHERE trade_id IN ({placeholders})
                    ORDER BY trade_id, signal_name""",
                trade_ids,
            )
            rows = cur.fetchall()
        conn.close()

        result: Dict[int, List[dict]] = defaultdict(list)
        for r in rows:
            result[r["trade_id"]].append(dict(r))
        return dict(result)
    except Exception as e:
        log.warning("Failed to fetch trade signals: %s", e)
        try:
            conn.close()
        except Exception:
            pass
        return {}


def _get_agents() -> List[str]:
    """Get list of active agent IDs from virtual_traders table, fallback to distinct trade trader_ids."""
    conn = _get_pg_conn()
    if not conn:
        return []

    from psycopg2.extras import RealDictCursor
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Try virtual_traders first
            cur.execute(
                """SELECT DISTINCT name FROM trading.virtual_traders
                   WHERE status IN ('active', 'live', 'probation')
                   ORDER BY name"""
            )
            rows = cur.fetchall()
            if rows:
                conn.close()
                return [r["name"] for r in rows]

            # Fallback: distinct trader_ids from trades table
            cur.execute(
                """SELECT DISTINCT trader_id FROM trading.trades
                   ORDER BY trader_id"""
            )
            rows = cur.fetchall()
        conn.close()
        return [r["trader_id"] for r in rows]
    except Exception as e:
        log.warning("Failed to fetch agents: %s", e)
        try:
            conn.close()
        except Exception:
            pass
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Stats Computation
# ═══════════════════════════════════════════════════════════════════════════════

def compute_trade_stats(trades: List[dict], signals: Optional[Dict[int, List[dict]]] = None) -> dict:
    """Compute comprehensive trading stats from a list of trade dicts.

    Args:
        trades: List of trade dicts (from Postgres trading.trades).
        signals: Optional {trade_id: [signal_dict]} mapping.

    Returns dict with:
        - today_stats: P&L, win rate, avg hold time, avg position size
        - rolling_stats: last 10/50/100 win rate
        - by_signal: win rate by signal name
        - by_sector: win rate by sector
        - confidence_calibration: confidence buckets → win rate
        - suggestions: list of strategy suggestions
    """
    if not trades:
        return {
            "today_stats": None,
            "rolling_stats": None,
            "by_signal": {},
            "by_sector": {},
            "confidence_calibration": {},
            "suggestions": ["No closed trades to analyze yet."],
            "total_trades": 0,
        }

    today = date.today()
    today_trades = [t for t in trades if t.get("exit_time") and hasattr(t["exit_time"], "date") and t["exit_time"].date() == today]

    # ── Today stats ────────────────────────────────────────────────────
    today_pnl = sum(float(t.get("pnl", 0) or 0) for t in today_trades)
    today_wins = [t for t in today_trades if (t.get("pnl") or 0) > 0]
    today_losses = [t for t in today_trades if (t.get("pnl") or 0) <= 0]
    today_win_rate = round(len(today_wins) / max(len(today_trades), 1), 4)

    today_hold_times = []
    for t in today_trades:
        if t.get("entry_time") and t.get("exit_time"):
            diff = t["exit_time"] - t["entry_time"]
            today_hold_times.append(diff.total_seconds() / 3600)
    avg_hold_time = round(sum(today_hold_times) / max(len(today_hold_times), 1), 2) if today_hold_times else 0.0

    avg_position_size = round(
        sum(abs(float(t.get("shares", 0) or 0) * float(t.get("entry_price", 0) or 0)) for t in today_trades)
        / max(len(today_trades), 1), 2,
    ) if today_trades else 0.0

    today_stats = {
        "num_trades": len(today_trades),
        "num_wins": len(today_wins),
        "num_losses": len(today_losses),
        "win_rate": today_win_rate,
        "total_pnl": round(today_pnl, 2),
        "avg_hold_time_hours": avg_hold_time,
        "avg_position_size": avg_position_size,
        "total_volume_shares": sum(int(t.get("shares", 0) or 0) for t in today_trades),
    }

    # ── Rolling stats (all closed trades) ──────────────────────────────
    closed_trades = [t for t in trades if t.get("exit_time") is not None and t.get("pnl") is not None]
    closed_trades.sort(key=lambda t: t.get("exit_time", datetime.min))

    def _rolling_wr(trades_slice: List[dict]) -> Optional[float]:
        if not trades_slice:
            return None
        wins = sum(1 for t in trades_slice if (t.get("pnl") or 0) > 0)
        return round(wins / len(trades_slice), 4)

    rolling_stats = {
        "last_10": _rolling_wr(closed_trades[-10:]) if len(closed_trades) >= 10 else None,
        "last_50": _rolling_wr(closed_trades[-50:]) if len(closed_trades) >= 50 else None,
        "last_100": _rolling_wr(closed_trades[-100:]) if len(closed_trades) >= 100 else None,
        "overall": _rolling_wr(closed_trades),
        "total_closed": len(closed_trades),
    }

    # ── By signal ──────────────────────────────────────────────────────
    by_signal: Dict[str, Dict[str, float]] = {}
    if signals:
        for t in trades:
            tid = t.get("id")
            if not tid:
                continue
            trade_signals = signals.get(tid, [])
            is_win = (t.get("pnl") or 0) > 0
            for sig in trade_signals:
                sname = sig.get("signal_name", "unknown")
                if sname not in by_signal:
                    by_signal[sname] = {"wins": 0, "losses": 0, "total": 0}
                by_signal[sname]["total"] += 1
                if is_win:
                    by_signal[sname]["wins"] += 1
                else:
                    by_signal[sname]["losses"] += 1

    by_signal_rates = {}
    for sname, counts in by_signal.items():
        total = counts["total"]
        wr = round(counts["wins"] / total, 4) if total > 0 else 0.0
        by_signal_rates[sname] = {
            "win_rate": wr,
            "wins": counts["wins"],
            "losses": counts["losses"],
            "total": total,
        }

    # ── By sector ──────────────────────────────────────────────────────
    by_sector: Dict[str, Dict[str, float]] = {}
    for t in trades:
        ticker = t.get("ticker", "")
        sector = _ticker_sector(ticker)
        is_win = (t.get("pnl") or 0) > 0
        if sector not in by_sector:
            by_sector[sector] = {"wins": 0, "losses": 0, "total": 0}
        by_sector[sector]["total"] += 1
        if is_win:
            by_sector[sector]["wins"] += 1
        else:
            by_sector[sector]["losses"] += 1

    by_sector_rates = {}
    for sector, counts in by_sector.items():
        total = counts["total"]
        wr = round(counts["wins"] / total, 4) if total > 0 else 0.0
        by_sector_rates[sector] = {
            "win_rate": wr,
            "wins": counts["wins"],
            "losses": counts["losses"],
            "total": total,
        }

    # ── Confidence calibration ─────────────────────────────────────────
    # Bucket trades by entry_conviction
    confidence_buckets: Dict[str, List[bool]] = {
        "very_low_<0.3": [],
        "low_0.3-0.5": [],
        "medium_0.5-0.7": [],
        "high_0.7-0.9": [],
        "very_high_>=0.9": [],
    }

    for t in trades:
        conv = t.get("entry_conviction")
        if conv is None:
            continue
        try:
            cv = float(conv)
        except (TypeError, ValueError):
            continue
        is_win = (t.get("pnl") or 0) > 0
        if cv < 0.3:
            confidence_buckets["very_low_<0.3"].append(is_win)
        elif cv < 0.5:
            confidence_buckets["low_0.3-0.5"].append(is_win)
        elif cv < 0.7:
            confidence_buckets["medium_0.5-0.7"].append(is_win)
        elif cv < 0.9:
            confidence_buckets["high_0.7-0.9"].append(is_win)
        else:
            confidence_buckets["very_high_>=0.9"].append(is_win)

    confidence_calibration = {}
    for bucket_name, outcomes in confidence_buckets.items():
        if not outcomes:
            continue
        wr_bucket = round(sum(1 for w in outcomes if w) / len(outcomes), 4)
        confidence_calibration[bucket_name] = {
            "win_rate": wr_bucket,
            "num_trades": len(outcomes),
        }

    # ── Suggestions ────────────────────────────────────────────────────
    suggestions = _generate_suggestions(today_stats, rolling_stats, by_signal_rates, by_sector_rates, confidence_calibration)

    return {
        "today_stats": today_stats,
        "rolling_stats": rolling_stats,
        "by_signal": by_signal_rates,
        "by_sector": by_sector_rates,
        "confidence_calibration": confidence_calibration,
        "suggestions": suggestions,
        "total_trades": len(closed_trades),
    }


def _generate_suggestions(
    today_stats: dict,
    rolling_stats: dict,
    by_signal: Dict[str, dict],
    by_sector: Dict[str, dict],
    confidence_calibration: Dict[str, dict],
) -> List[str]:
    """Generate actionable strategy suggestions based on stats."""
    suggestions = []

    # Win rate trending down
    if rolling_stats.get("last_10") is not None and rolling_stats.get("last_50") is not None:
        if rolling_stats["last_10"] < rolling_stats["last_50"]:
            suggestions.append(
                f"⚠️ Win rate declining: last 10 trades ({rolling_stats['last_10']:.0%}) "
                f"vs last 50 ({rolling_stats['last_50']:.0%}). Consider reviewing recent signals."
            )

    # Poor sector performance
    for sector, stats in sorted(by_sector.items(), key=lambda x: x[1]["win_rate"]):
        if stats["total"] >= 3 and stats["win_rate"] < 0.3:
            suggestions.append(
                f"⚠️ {sector}: only {stats['win_rate']:.0%} win rate ({stats['wins']}W/{stats['losses']}L). "
                f"Consider reducing exposure or tightening entries in this sector."
            )

    # Strong sector performance
    for sector, stats in sorted(by_sector.items(), key=lambda x: -x[1]["win_rate"]):
        if stats["total"] >= 3 and stats["win_rate"] > 0.7:
            suggestions.append(
                f"✅ {sector}: strong {stats['win_rate']:.0%} win rate ({stats['wins']}W/{stats['losses']}L). "
                f"Consider increasing allocation."
            )

    # Signal performance
    for sname, stats in sorted(by_signal.items(), key=lambda x: x[1]["win_rate"]):
        if stats["total"] >= 3 and stats["win_rate"] < 0.3:
            suggestions.append(
                f"⚠️ Signal '{sname}': only {stats['win_rate']:.0%} win rate. "
                f"Consider deprioritizing or re-tuning this signal."
            )
        elif stats["total"] >= 3 and stats["win_rate"] > 0.7:
            suggestions.append(
                f"✅ Signal '{sname}': strong {stats['win_rate']:.0%} win rate. "
                f"Consider weighting this signal more heavily."
            )

    # Confidence calibration
    if confidence_calibration:
        high_conf_keys = [k for k in confidence_calibration if "0.7" in k or "0.9" in k]
        low_conf_keys = [k for k in confidence_calibration if "0.3" in k or "low" in k]
        if high_conf_keys:
            avg_high_wr = sum(confidence_calibration[k]["win_rate"] for k in high_conf_keys) / len(high_conf_keys)
            if avg_high_wr < 0.5:
                suggestions.append(
                    f"⚠️ High-confidence trades (>{', '.join(high_conf_keys)}) average only "
                    f"{avg_high_wr:.0%} win rate. Confidence scoring may need recalibration."
                )
        if low_conf_keys:
            avg_low_wr = sum(confidence_calibration[k]["win_rate"] for k in low_conf_keys) / len(low_conf_keys)
            if avg_low_wr > 0.6:
                suggestions.append(
                    f"💡 Low-confidence trades average {avg_low_wr:.0%} win rate. "
                    f"The model might be under-confident in good setups."
                )

    # Low trade volume
    if today_stats and today_stats["num_trades"] == 0:
        suggestions.append("📭 No trades today. Check if agent is active and market conditions are favorable.")
    elif today_stats and today_stats["num_trades"] < 3:
        suggestions.append(f"📭 Only {today_stats['num_trades']} trades today. Consider expanding watchlist or lowering conviction threshold to increase activity.")

    # P&L direction
    if today_stats and today_stats["total_pnl"] < -50:
        suggestions.append(f"📉 Today's P&L: ${today_stats['total_pnl']:.2f}. Review stop-loss placement and position sizing.")

    if not suggestions:
        suggestions.append("✅ No significant issues detected. Keep executing!")

    return suggestions


# ═══════════════════════════════════════════════════════════════════════════════
# Reflection Generation
# ═══════════════════════════════════════════════════════════════════════════════

def generate_reflection(agent_id: str, trades: List[dict] = None) -> str:
    """Generate a structured markdown reflection for an agent.

    If trades is None, fetches from Postgres.

    Returns:
        Markdown string with today's reflection.
    """
    if trades is None:
        trades = _get_trades(agent_id, limit=100, since=date.today())

    # Fetch signals for trade IDs
    trade_ids = [t.get("id") for t in trades if t.get("id")]
    signals = _get_trade_signals(trade_ids) if trade_ids else {}

    stats = compute_trade_stats(trades, signals)

    today_str = date.today().isoformat()
    ts = stats["today_stats"]
    rs = stats["rolling_stats"]
    lines = []

    # ── Header ─────────────────────────────────────────────────────────
    lines.append(f"# 📊 Daily Reflection: {agent_id}")
    lines.append(f"**Date:** {today_str}")
    lines.append(f"**Total Trades Analyzed:** {stats['total_trades']}")
    lines.append("")

    # ── Today's Performance ─────────────────────────────────────────────
    lines.append("## Today's Performance")
    if ts and ts["num_trades"] > 0:
        lines.append(f"- **Trades:** {ts['num_trades']} ({ts['num_wins']}W / {ts['num_losses']}L)")
        lines.append(f"- **Win Rate:** {ts['win_rate']:.1%}")
        lines.append(f"- **P&L:** ${ts['total_pnl']:.2f}")
        lines.append(f"- **Avg Hold Time:** {ts['avg_hold_time_hours']:.1f}h")
        lines.append(f"- **Avg Position Size:** ${ts['avg_position_size']:.2f}")
        lines.append(f"- **Total Volume:** {ts['total_volume_shares']:,} shares")
    else:
        lines.append("- No trades closed today.")
    lines.append("")

    # ── Rolling Win Rates ──────────────────────────────────────────────
    lines.append("## Rolling Win Rates")
    if rs:
        for label in ["last_10", "last_50", "last_100"]:
            val = rs.get(label)
            if val is not None:
                lines.append(f"- **{label.replace('_', ' ').title()}:** {val:.1%}")
        if rs.get("overall") is not None:
            lines.append(f"- **Overall:** {rs['overall']:.1%} ({rs['total_closed']} trades)")
    lines.append("")

    # ── By Signal ──────────────────────────────────────────────────────
    if stats["by_signal"]:
        lines.append("## Win Rate by Signal")
        for sname, sstats in sorted(stats["by_signal"].items(), key=lambda x: -x[1]["win_rate"]):
            lines.append(f"- **{sname}:** {sstats['win_rate']:.1%} ({sstats['wins']}W/{sstats['losses']}L, {sstats['total']} trades)")
        lines.append("")

    # ── By Sector ──────────────────────────────────────────────────────
    if stats["by_sector"]:
        lines.append("## Win Rate by Sector")
        for sector, sstats in sorted(stats["by_sector"].items(), key=lambda x: -x[1]["win_rate"]):
            lines.append(f"- **{sector}:** {sstats['win_rate']:.1%} ({sstats['wins']}W/{sstats['losses']}L, {sstats['total']} trades)")
        lines.append("")

    # ── Confidence Calibration ─────────────────────────────────────────
    if stats["confidence_calibration"]:
        lines.append("## Confidence Calibration")
        for bucket, cstats in sorted(stats["confidence_calibration"].items()):
            display_key = bucket.replace("_", " ").replace(">=", "≥")
            lines.append(f"- **{display_key}:** {cstats['win_rate']:.1%} win rate ({cstats['num_trades']} trades)")
        lines.append("")

    # ── Suggestions ────────────────────────────────────────────────────
    lines.append("## Strategy Suggestions")
    for s in stats["suggestions"]:
        lines.append(f"- {s}")
    lines.append("")

    # ── Raw Stats (JSON block for programmatic consumption) ────────────
    lines.append("## Raw Stats")
    lines.append("```json")
    # Build a clean serializable version
    serializable = {
        "agent_id": agent_id,
        "date": today_str,
        "today_pnl": round(ts["total_pnl"], 2) if ts else None,
        "today_win_rate": ts["win_rate"] if ts else None,
        "today_trades": ts["num_trades"] if ts else None,
        "overall_win_rate": rs.get("overall") if rs else None,
        "total_closed_trades": rs.get("total_closed") if rs else None,
        "n_suggestions": len(stats["suggestions"]),
    }
    lines.append(json.dumps(serializable, indent=2, default=str))
    lines.append("```")

    return "\n".join(lines)


def generate_reflection_json(agent_id: str, trades: List[dict] = None) -> dict:
    """Generate reflection stats as JSON (for GET /self/stats endpoint).

    If trades is None, fetches from Postgres.
    """
    if trades is None:
        trades = _get_trades(agent_id, limit=100, since=date.today())

    trade_ids = [t.get("id") for t in trades if t.get("id")]
    signals = _get_trade_signals(trade_ids) if trade_ids else {}

    stats = compute_trade_stats(trades, signals)

    stats["agent_id"] = agent_id
    stats["date"] = date.today().isoformat()
    stats["market_condition"] = "unknown"  # filled by caller if available

    return stats


# ═══════════════════════════════════════════════════════════════════════════════
# Persistence
# ═══════════════════════════════════════════════════════════════════════════════

def write_reflection(agent_id: str, reflection_text: str, stats: dict) -> bool:
    """Write a day's reflection to the daily_reflections table.

    Uses INSERT … ON CONFLICT DO UPDATE so the same agent+date won't
    create duplicate rows.

    Returns True on success, False on error.
    """
    conn = _get_pg_writer()
    if not conn:
        return False

    today = date.today()
    ts = stats.get("today_stats") or {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO trading.daily_reflections
                       (agent_id, date, reflection_text, suggestions, win_rate, total_pnl, num_trades)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (agent_id, date)
                   DO UPDATE SET
                       reflection_text = EXCLUDED.reflection_text,
                       suggestions = EXCLUDED.suggestions,
                       win_rate = EXCLUDED.win_rate,
                       total_pnl = EXCLUDED.total_pnl,
                       num_trades = EXCLUDED.num_trades,
                       created_at = NOW()""",
                (
                    agent_id,
                    today,
                    reflection_text,
                    json.dumps(stats.get("suggestions", [])),
                    ts.get("win_rate"),
                    ts.get("total_pnl"),
                    ts.get("num_trades"),
                ),
            )
        conn.close()
        log.info("Wrote reflection for %s (%s)", agent_id, today)
        return True
    except Exception as e:
        log.warning("Failed to write reflection for %s: %s", agent_id, e)
        try:
            conn.close()
        except Exception:
            pass
        return False


def get_last_reflection(agent_id: str) -> Optional[dict]:
    """Fetch the most recent reflection for an agent.

    Returns dict or None.
    """
    conn = _get_pg_conn()
    if not conn:
        return None

    from psycopg2.extras import RealDictCursor
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM trading.daily_reflections
                   WHERE agent_id = %s
                   ORDER BY date DESC
                   LIMIT 1""",
                (agent_id,),
            )
            row = cur.fetchone()
        conn.close()
        if row:
            r = dict(row)
            if "created_at" in r and hasattr(r["created_at"], "isoformat"):
                r["created_at"] = r["created_at"].isoformat()
            if "date" in r and hasattr(r["date"], "isoformat"):
                r["date"] = r["date"].isoformat()
            return r
        return None
    except Exception as e:
        log.warning("Failed to fetch last reflection for %s: %s", agent_id, e)
        try:
            conn.close()
        except Exception:
            pass
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Scheduler
# ═══════════════════════════════════════════════════════════════════════════════

def _run_all_reflections(dry_run: bool = False) -> List[str]:
    """Run reflections for all active agents.

    Returns list of agent IDs that were processed.
    """
    agents = _get_agents()
    log.info("Running daily reflection for %d agents (dry_run=%s)", len(agents), dry_run)
    processed = []

    for agent_id in agents:
        try:
            trades = _get_trades(agent_id, limit=100, since=date.today())
            if not trades and not dry_run:
                log.info("Skipping %s: no trades found", agent_id)
                continue

            reflection_md = generate_reflection(agent_id, trades)
            stats = generate_reflection_json(agent_id, trades)

            if dry_run:
                log.info("=== REFLECTION for %s (DRY RUN) ===", agent_id)
                for line in reflection_md.splitlines():
                    log.info("  %s", line)
                log.info("=== END REFLECTION %s ===", agent_id)
            else:
                write_reflection(agent_id, reflection_md, stats)

            processed.append(agent_id)
        except Exception as e:
            log.error("Failed to generate reflection for %s: %s", agent_id, e)

    return processed


def schedule_reflection_cron():
    """Spawn a daemon thread that checks every minute if it's 16:30 ET.

    When the time matches, runs reflections for all active agents.
    Only fires once per day (tracks last_run_date).
    """
    def _loop():
        last_run_date = None
        log.info("Reflection cron scheduler started (checking every 60s for 16:30 ET)")

        while True:
            try:
                from zoneinfo import ZoneInfo
                now_et = datetime.now(ZoneInfo("America/New_York"))
                today = now_et.date()

                # Check if it's 16:30 or later, but haven't run today yet
                if last_run_date != today and now_et.hour == 16 and now_et.minute >= 30:
                    log.info("Triggering daily reflection at %s ET", now_et.strftime("%H:%M"))
                    _run_all_reflections(dry_run=False)
                    last_run_date = today
                    log.info("Daily reflection complete for %s", today)
            except Exception as e:
                log.error("Reflection cron loop error: %s", e)

            time.sleep(60)

    thread = threading.Thread(target=_loop, daemon=True, name="reflection-cron")
    thread.start()
    log.info("Reflection cron scheduler daemon launched")
    return thread


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="End-of-day reflection generator for paper trading agents."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print reflection without writing")
    parser.add_argument("--agent", type=str, default=None, help="Single agent ID to reflect on")
    parser.add_argument("--all", action="store_true", help="Run reflection for all active agents")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of markdown")
    args = parser.parse_args()

    if args.agent and args.all:
        log.error("Cannot specify both --agent and --all")
        sys.exit(1)

    if args.agent:
        # Single agent
        trades = _get_trades(args.agent, limit=100)
        if args.json:
            stats = generate_reflection_json(args.agent, trades)
            print(json.dumps(stats, indent=2, default=str))
        else:
            reflection = generate_reflection(args.agent, trades)
            print(reflection)

        if not args.dry_run and not args.json:
            trade_ids = [t.get("id") for t in trades if t.get("id")]
            signals = _get_trade_signals(trade_ids) if trade_ids else {}
            stats = compute_trade_stats(trades, signals)
            write_reflection(args.agent, reflection, stats)
            log.info("Reflection written for %s", args.agent)

    elif args.all:
        processed = _run_all_reflections(dry_run=args.dry_run)
        log.info("Processed %d agents", len(processed))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()