#!/usr/bin/env python3
"""
Paper Trading Dashboard — Flask API + static frontend server.

Serves the web dashboard at /  and JSON data at /api/*.
All three trader accounts are queried live from Alpaca + local state files.

Run:
    python3 src/leaderboard_api.py
    # Open http://<vm-ip>:5002 in a browser

Persistent (background):
    nohup python3 src/leaderboard_api.py > state/dashboard.log 2>&1 &
"""

import json
import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

# ── credentials ──────────────────────────────────────────────────────────────
# Load from ~/.openclaw/.env (paper trading keys), fall back to local .env
load_dotenv(Path.home() / ".openclaw" / ".env")
load_dotenv(Path(".env"), override=False)

# ── paths ─────────────────────────────────────────────────────────────────────
ROOT    = Path(__file__).parent.parent   # project root
STATE   = ROOT / "state"
PG_DSN = "host=192.168.1.179 port=5433 dbname=trading user=trader"
UI_DIR  = Path(__file__).parent / "leaderboard_ui"

app = Flask(__name__)

# ── constants ─────────────────────────────────────────────────────────────────
STARTING_VALUE = 10_000.0

TRADER_META = [
    {"id": "kairos",   "name": "Kairós Capital",     "manager": "Zara Chen"},
    {"id": "aldridge", "name": "Aldridge & Partners", "manager": "Edmund Whitfield"},
    {"id": "stonks",   "name": "Stonks Capital",      "manager": "Stan Hoolihan"},
]

# Env var names for each account's Alpaca credentials
_CRED_MAP = {
    "kairos":   ("KAIROS_API_KEY",   "KAIROS_SECRET_KEY"),
    "aldridge": ("ALDRIDGE_API_KEY", "ALDRIDGE_SECRET_KEY"),
    "stonks":   ("STONKS_API_KEY",   "STONKS_SECRET_KEY"),
}


# ── helpers ───────────────────────────────────────────────────────────────────

class _PgCursor:
    """Wrapper around connection providing conn.execute() that returns RealDictCursor."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def close(self):
        self._conn.close()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def __bool__(self):
        return True


@contextmanager
def _db():
    """Context manager for DB connections — Postgres on docker.klo.

    Returns a wrapper with execute() that returns RealDictCursor so
    existing SQLite-style conn.execute(sql, params).fetchone() calls work on pg.
    """
    conn = None
    try:
        conn = psycopg2.connect("host=192.168.1.179 port=5433 dbname=trading user=trader")
        conn.autocommit = True
        with conn.cursor() as c:
            c.execute("SET search_path TO trading, public")
        yield _PgCursor(conn)
    finally:
        if conn:
            conn.close()


def _load_json(path: Path) -> dict:
    """Load a JSON file, returning {} on any error."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _seconds_ago(ts: str) -> Optional[int]:
    """Return seconds since an ISO timestamp string (handles both naive and tz-aware)."""
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(ts)
        now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
        return max(0, int((now - dt).total_seconds()))
    except Exception:
        return None


def _is_option_symbol(symbol: str) -> bool:
    """Detect OCC option symbols: ROOT+YYMMDD+C/P+STRIKE*1000."""
    import re
    # OCC format: 1-6 char root + 6-digit date + C/P + 8-digit strike*1000
    return bool(re.match(r'^[A-Z]{1,6}\d{6}[CP]\d{8}$', symbol))


def _get_portfolio_from_db(company: str) -> Optional[dict]:
    """
    Read the latest portfolio snapshot from portfolio_snapshots table.
    Returns dict with cash, portfolio_value, unrealized_pl, open_positions, etc.
    Returns None if no snapshot exists or DB is missing.
    """
    try:
        with _db() as conn:
            row = conn.execute(
                """SELECT timestamp, cash, portfolio_value, unrealized_pl,
                          daily_pnl, open_positions, source
                   FROM portfolio_snapshots
                   WHERE trader_id = %s
                   ORDER BY timestamp DESC LIMIT 1""",
                (f"trader-{company}",),
            ).fetchone()
        if not row:
            return None
        return {
            "cash": row["cash"],
            "portfolio_value": row["portfolio_value"],
            "unrealized_pl": row["unrealized_pl"],
            "daily_pnl": row["daily_pnl"],
            "open_positions_count": row["open_positions"],
            "snapshot_ts": row["timestamp"],
            "source": "db_snapshot",
        }
    except Exception:
        return None


def _get_alpaca_portfolio(company: str) -> Optional[dict]:
    """
    Fetch portfolio data — DB snapshot first, live Alpaca fallback.
    
    1. If a recent DB snapshot exists (< 5 min old), use its values.
    2. If stale or missing, query Alpaca live.
    3. If Alpaca fails too, fall back to the stale snapshot (better than zeros).
    
    Positions are always fetched from Alpaca live when possible.
    Returns dict with cash, portfolio_value, buying_power, positions, _source.
    Returns None if no data is available from any source.
    """
    snap = _get_portfolio_from_db(company)
    snap_recent = (
        snap
        and snap.get("snapshot_ts")
        and _seconds_ago(snap["snapshot_ts"]) is not None
        and _seconds_ago(snap["snapshot_ts"]) < 300
    )

    # ── always try Alpaca for positions ──
    live_data = None
    positions = []
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from src.execute import AlpacaExecutor

        api_key_env, secret_env = _CRED_MAP[company]
        # Prefer ALPACA_*_KEY naming (from ~/.openclaw/.env) over old-style names
        api_key = (os.getenv(f"ALPACA_{company.upper()}_KEY")
                   or os.getenv(api_key_env))
        secret  = (os.getenv(f"ALPACA_{company.upper()}_SECRET")
                   or os.getenv(secret_env))
        if not api_key or not secret:
            return None

        executor = AlpacaExecutor(api_key, secret, company)
        data = executor.get_account_value()
        if data is None:
            return None

        # Fetch open positions so the UI can show what each trader holds
        positions = []
        try:
            for p in executor.client.get_all_positions():
                pl_pct = float(p.unrealized_plpc) * 100
                positions.append({
                    "ticker":          p.symbol,
                    "qty":             float(p.qty),
                    "avg_entry":       float(p.avg_entry_price),
                    "current_price":   float(p.current_price),
                    "unrealized_pl":   float(p.unrealized_pl),
                    "unrealized_plpc": round(pl_pct, 2),
                    "market_value":    float(p.market_value),
                })
        except Exception:
            pass

        # Merge exit conditions from local positions table
        if positions:
            try:
                agent_id = f"trader-{company}"
                with _db() as conn:
                    if conn:
                        for pos in positions:
                            row = conn.execute(
                                """SELECT exit_condition, holding_horizon_days, stop_loss
                                   FROM trader_positions
                                   WHERE agent_id = %s AND ticker = %s AND status = 'open'""",
                                (agent_id, pos["ticker"]),
                            ).fetchone()
                            if row:
                                pos["exit_condition"] = row["exit_condition"] or ""
                                pos["holding_horizon_days"] = row["holding_horizon_days"]
                                if row["stop_loss"] and not pos.get("stop_loss"):
                                    pos["stop_loss"] = float(row["stop_loss"])
            except Exception:
                pass

        data["positions"] = positions
        return data
    except Exception:
        pass

    # ── decide which values to return ──
    if snap_recent:
        # Recent snapshot available — prefer it for account values
        result = {
            "cash": snap["cash"],
            "portfolio_value": snap["portfolio_value"],
            "buying_power": live_data.get("buying_power") if live_data else None,
            "unrealized_pl": snap["unrealized_pl"],
            "daily_pnl": snap["daily_pnl"],
            "positions": positions,
            "_source": "db_snapshot",
        }
        return result

    if live_data:
        # Live Alpaca succeeded
        live_data["positions"] = positions
        live_data["_source"] = "alpaca_live"
        live_data["unrealized_pl"] = snap["unrealized_pl"] if snap else 0
        live_data["daily_pnl"] = snap["daily_pnl"] if snap else 0
        return live_data

    if snap:
        # Alpaca failed — use stale snapshot as fallback
        return {
            "cash": snap["cash"],
            "portfolio_value": snap["portfolio_value"],
            "buying_power": None,
            "unrealized_pl": snap["unrealized_pl"],
            "daily_pnl": snap["daily_pnl"],
            "positions": [],
            "_source": "stale_snapshot",
        }

    return None


def _parse_decisions(company: str) -> list:
    """
    Query decisions + orders from Postgres trading.decisions table.
    Returns a list of event dicts matching the old JSONL format.
    """
    events = []
    with _db() as conn:
        if not conn:
            return events
        try:
            rows = conn.execute(
                """SELECT d.agent_id, d.timestamp, d.action, d.ticker, d.quantity,
                          d.stop_loss, d.confidence, d.thesis,
                          o.status AS order_status, o.order_id, o.error_reason
                   FROM trader_decisions d
                   LEFT JOIN orders o ON d.id = o.decision_id
                   WHERE d.agent_id = %s
                   ORDER BY d.timestamp DESC""",
                (f"trader-{company}",),
            ).fetchall()
            for r in rows:
                events.append({
                    "timestamp":    r["timestamp"],
                    "trader":       r["agent_id"],
                    "decision": {
                        "action":   r["action"],
                        "ticker":   r["ticker"],
                        "quantity": r["quantity"],
                        "confidence": r["confidence"],
                        "thesis":   r["thesis"],
                        "stop_loss": r["stop_loss"],
                    },
                    "order": {
                        "status":   r["order_status"],
                        "order_id": r["order_id"],
                        "reason":   r["error_reason"],
                    },
                })
        except Exception:
            pass
    return events


# ── Benchmark helpers ────────────────────────────────────────────────────────

def _get_benchmark_data() -> dict:
    """Get current SPY/QQQ prices from market_data.bars.

    Returns:
        {
            "spy": {"price": 733.24},
            "qqq": {"price": 710.62},
            "comparisons": {}
        }
    """
    data = {"spy": None, "qqq": None, "comparisons": {}}
    try:
        with _db() as conn:
            if not conn:
                return data

            # Latest prices
            for ticker in ["SPY", "QQQ"]:
                row = conn.execute(
                    "SELECT close_price FROM benchmarks WHERE ticker = %s ORDER BY date DESC LIMIT 1",
                    (ticker,),
                ).fetchone()
                if row:
                    key = ticker.lower()
                    data[key] = {"price": round(float(row["close_price"]), 2)}

            # Agent benchmark comparisons
            for row in conn.execute(
                """SELECT * FROM agent_benchmark_comparison
                   ORDER BY agent_id, period_start DESC"""
            ).fetchall():
                aid = row["agent_id"]
                if aid not in data["comparisons"]:
                    data["comparisons"][aid] = {
                        "agent_return": row["agent_return"],
                        "spy_return": row["spy_return"],
                        "qqq_return": row["qqq_return"],
                        "spy_excess": row["spy_excess"],
                        "period_start": str(row["period_start"]) if row["period_start"] else None,
                        "period_end": str(row["period_end"]) if row["period_end"] else None,
                    }
    except Exception:
        pass
    return data


def _get_agent_benchmark(agent_id: str) -> Optional[dict]:
    """Get the latest benchmark comparison for a specific agent."""
    bm = _get_benchmark_data()
    return bm["comparisons"].get(agent_id)


def _get_agent_score(company: str) -> Optional[dict]:
    """Get the current score and score components for a trader.
    Returns the score dict from scoring module, or None if unavailable.
    """
    try:
        sys.path.insert(0, str(ROOT))
        from src.scoring import compute_score
        result = compute_score(f"trader-{company}")
        return {
            "score": result["score"],
            "ending_value": result["ending_value"],
            "drawdown_penalty": result["drawdown_penalty"],
            "violation_penalties": result["violation_penalties"],
        }
    except Exception:
        return None


def _get_paused_status(company: str) -> Optional[dict]:
    """Get kill-switch / paused status for a trader from risk_events."""
    try:
        with _db() as conn:
            if not conn:
                return None
            row = conn.execute(
                "SELECT trader_id, vetoed, timestamp, reason "
                "FROM risk_events WHERE trader_id = %s AND vetoed = true "
                "ORDER BY timestamp DESC LIMIT 1",
                (f"trader-{company}",),
            ).fetchone()
            if row:
                from datetime import datetime, timezone, timedelta
                evt_ts = row["timestamp"]
                is_recent = False
                try:
                    if isinstance(evt_ts, str):
                        evt_dt = datetime.fromisoformat(evt_ts)
                    else:
                        evt_dt = evt_ts
                    if evt_dt.tzinfo is None:
                        evt_dt = evt_dt.replace(tzinfo=timezone.utc)
                    is_recent = (datetime.now(timezone.utc) - evt_dt) < timedelta(hours=24)
                except Exception:
                    pass
                return {
                    "paused": is_recent,
                    "reason": row["reason"],
                    "timestamp": row["timestamp"],
                }
    except Exception:
        pass
    return None


# ── API: /api/traders ─────────────────────────────────────────────────────────


def _get_trade_stats(company: str) -> dict:
    """Compute trade stats from orders table + agent_profile performance metrics.

    Returns:
        total_trades: order count from orders table (buys + sells)
        buys: number of buy orders
        sells: number of sell orders
        wins: from agent_profile.performance.wins
        losses: from agent_profile.performance.losses
        win_rate: from agent_profile.performance.win_rate
    """
    result = {
        "total_trades": 0, "buys": 0, "sells": 0,
        "wins": 0, "losses": 0, "win_rate": 0,
    }
    try:
        with _db() as conn:
            # Order counts: buys and sells (not wins/losses)
            rows = conn.execute(
                """SELECT action, COUNT(*) as cnt
                   FROM orders
                   WHERE agent_id=%s AND status NOT IN ('error','rejected')
                   GROUP BY action""",
                (f"trader-{company}",),
            ).fetchall()
            for r in rows:
                action_lower = r["action"].lower()
                result[action_lower] = r["cnt"]
            # Total trades count from trades table (agent-level production data)
            total_row = conn.execute(
                """SELECT count(*) as cnt FROM trades
                   WHERE trader_id = %s AND pnl IS NOT NULL""",
                (f"trader-{company}",),
            ).fetchone()
            result["total_trades"] = total_row["cnt"] if total_row else 0

            # Also keep order counts
            result["buys"] = result.get("buy", 0)
            result["sells"] = result.get("sell", 0)

            # Win/loss: compute directly from trades with PnL (agent-level production data)
            pnl_rows = conn.execute(
                """SELECT pnl FROM trades
                   WHERE trader_id = %s AND pnl IS NOT NULL""",
                (f"trader-{company}",),
            ).fetchall()
            if pnl_rows:
                pnls = [r["pnl"] for r in pnl_rows]
                result["wins"] = sum(1 for p in pnls if p > 0)
                result["losses"] = sum(1 for p in pnls if p <= 0)
                result["win_rate"] = round(result["wins"] / len(pnls), 4) if pnls else 0.0

            # Also check agent_profile.performance as fallback
            if not pnl_rows:
                perf_row = conn.execute(
                    "SELECT performance FROM agent_profile WHERE agent_id=%s",
                    (f"trader-{company}",),
                ).fetchone()
                if perf_row and perf_row["performance"]:
                    try:
                        perf = json.loads(perf_row["performance"])
                        if isinstance(perf, str):
                            perf = json.loads(perf)
                    except (json.JSONDecodeError, TypeError):
                        perf = {}
                    result["wins"] = perf.get("wins", 0) or 0
                    result["losses"] = perf.get("losses", 0) or 0
                    result["win_rate"] = perf.get("win_rate", 0) or 0
    except Exception:
        pass
    return result

def _get_last_activity(company: str) -> str | None:
    """Get the most recent journal entry timestamp for a trader from the shared DB."""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT MAX(timestamp) as max_ts FROM trader_journal WHERE agent_id=%s",
                (f"trader-{company}",),
            ).fetchone()
        return row["max_ts"] if row else None
    except Exception:
        return None


def _get_recent_thought(company: str) -> str | None:
    """Get the most recent journal entry text for a trader."""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT entry, mood FROM trader_journal WHERE agent_id=%s ORDER BY timestamp DESC LIMIT 1",
                (f"trader-{company}",),
            ).fetchone()
        if row:
            mood = row["mood"] or ""
            text = row["entry"] or ""
            return f"{mood + ' — ' if mood else ''}{text}"
        return None
    except Exception:
        return None


def _get_profile_from_db(company: str) -> dict:
    """
    Read trader personality/profile from the Postgres agent_profile table.
    Returns a dict matching the fields expected by the /api/traders route.
    Returns empty dict on failure.
    """
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM agent_profile WHERE agent_id = %s",
                (f"trader-{company}",),
            ).fetchone()
        if not row:
            return {}

        # Parse JSON columns
        current_state = {}
        performance = {}
        identity = {}
        try:
            current_state = json.loads(row["current_state"]) if isinstance(row["current_state"], str) else row["current_state"] or {}
        except Exception:
            pass
        try:
            performance = json.loads(row["performance"]) if isinstance(row["performance"], str) else row["performance"] or {}
        except Exception:
            pass
        try:
            identity = json.loads(row["identity"]) if isinstance(row["identity"], str) else row["identity"] or {}
        except Exception:
            pass

        return {
            "tagline": row["tagline"] or "",
            "current_state": current_state,
            "performance_metrics": {
                "wins": performance.get("winning_trades", 0),
                "losses": performance.get("losing_trades", 0),
                "win_rate": performance.get("win_rate", 0),
                "weekly_pnl": performance.get("weekly_pnl", 0),
                "trades_this_week": performance.get("trades_this_week", 0),
                "max_drawdown": performance.get("max_drawdown", 0),
                "biggest_win": performance.get("biggest_win", 0),
                "biggest_loss": performance.get("biggest_loss", 0),
            },
            "market_observations": {},
            "recent_thoughts": [],
            "identity": identity,
            "updated_at": row["updated_at"],
        }
    except Exception:
        return {}


@app.route("/api/traders")
def api_traders():
    """
    Returns live portfolio data for all three traders.
    Queries Alpaca API for each account + reads profile JSON for personality state.
    """
    heartbeat = _load_json(STATE / "heartbeat-state.json")
    result = []

    for meta in TRADER_META:
        company  = meta["id"]
        profile  = _get_profile_from_db(company)
        portfolio = _get_alpaca_portfolio(company)

        pv  = portfolio["portfolio_value"] if portfolio else None
        pct = round((pv - STARTING_VALUE) / STARTING_VALUE * 100, 2) if pv else None

        # Tag positions as option or equity
        positions = portfolio.get("positions", []) if portfolio else []
        options_exposure = 0.0
        for pos in positions:
            pos["is_option"] = _is_option_symbol(pos.get("ticker", pos.get("symbol", "")))
            if pos["is_option"]:
                options_exposure += float(pos.get("market_value", 0) or 0)
        options_pct = round((options_exposure / pv * 100), 1) if pv and pv > 0 else 0

        # Use journal timestamp (most reliable), fall back to heartbeat-state.json, then profile
        last_hb   = _get_last_activity(company) or heartbeat.get(f"last_{company}") or profile.get("updated_at")
        cs        = profile.get("current_state", {})
        perf      = profile.get("performance_metrics", {})
        obs       = profile.get("market_observations", {})

        # Live trade stats: buys/sells from orders, wins/losses from agent_profile
        _ts = _get_trade_stats(company)
        wins = _ts["wins"]
        losses = _ts["losses"]
        total_trades = _ts["total_trades"]
        win_rate = _ts["win_rate"]

        result.append({
            "id":               company,
            "name":             meta["name"],
            "manager":          meta["manager"],
            "tagline":          profile.get("tagline", ""),
            "portfolio_value":  pv,
            "cash":             portfolio.get("cash") if portfolio else None,
            "buying_power":     portfolio.get("buying_power") if portfolio else None,
            "pnl_pct":          pct,
            "benchmark_comparison": _get_agent_benchmark(agent_id=f"trader-{company}"),
            # Personality state (from profile JSON, updated each heartbeat)
            "confidence":       cs.get("confidence"),
            "excitement":       cs.get("excitement"),
            "frustration":      cs.get("frustration"),
            "market_appetite":  cs.get("market_appetite", ""),
            # Win/loss stats from agent_profile.performance
            "wins":             wins,
            "losses":           losses,
            "total_trades":     total_trades,
            "win_rate":         win_rate,
            # Most recent thought from journal DB
            "recent_thought":   _get_recent_thought(company),
            # Sector bets they're focused on
            "sector_momentum":  obs.get("sector_momentum", {}),
            "market_trend":     obs.get("market_trend", ""),
            # Last heartbeat timing
            "last_heartbeat":   last_hb,
            "last_heartbeat_ago_s": _seconds_ago(last_hb) if last_hb else None,
            # Last tick = same as last heartbeat (ticks merged into heartbeat)
            "last_tick_ago_s": _seconds_ago(last_hb) if last_hb else None,
            # Open positions from Alpaca (tagged with is_option)
            "positions":        positions,
            # Options summary
            "options_exposure": options_exposure,
            "options_pct":      options_pct,
            # Adjusted score (risk-adjusted)
            "score":            _get_agent_score(company),
            # Kill-switch / paused status
            "paused":           _get_paused_status(company),
        })

    # Rank by portfolio_value descending; null values go last
    result.sort(key=lambda t: t["portfolio_value"] or 0, reverse=True)

    # ── Benchmark: SPY/QQQ comparison ───────────────────────────────────────
    benchmarks = _get_benchmark_data()

    return jsonify({
        "traders": result,
        "benchmarks": benchmarks,
        "updated_at": datetime.now().isoformat(),
    })


# ── API: /api/vetoes ─────────────────────────────────────────────────────────

@app.route("/api/vetoes")
def api_vetoes():
    """Returns last 10 risk gate vetoes (decisions with source='risk_gate')."""
    limit = int(request.args.get("limit", 10))
    vetoes = []
    with _db() as conn:
        if conn:
            try:
                rows = conn.execute(
                    """SELECT agent_id, timestamp, action, ticker, thesis, source
                       FROM trader_decisions
                       WHERE source = 'risk_gate'
                       ORDER BY timestamp DESC LIMIT %s""",
                    (limit,),
                ).fetchall()
                for r in rows:
                    vetoes.append({
                        "agent_id": r["agent_id"],
                        "timestamp": r["timestamp"],
                        "action": r["action"],
                        "ticker": r["ticker"],
                        "reason": r["thesis"],
                    })
            except Exception:
                pass
    return jsonify({"vetoes": vetoes})


# ── API: /api/positions ────────────────────────────────────────────────────────

@app.route("/api/positions")
def api_positions():
    """Returns open positions with exit conditions for all traders."""
    positions = []
    with _db() as conn:
        if conn:
            try:
                rows = conn.execute(
                    """SELECT p.agent_id, p.ticker, p.quantity, p.avg_entry_price,
                              p.current_price, p.stop_loss, p.exit_condition,
                              p.holding_horizon_days, p.opened_at, p.status
                       FROM trader_positions p
                       WHERE p.status = 'open'
                       ORDER BY p.agent_id, p.ticker""",
                ).fetchall()
                for r in rows:
                    positions.append(dict(r))
            except Exception:
                pass
    return jsonify({"positions": positions})


# ── API: /api/kill-switch ─────────────────────────────────────────────────────

@app.route("/api/kill-switch")
def api_kill_switch():
    """Returns kill-switch status for all traders.
    
    Paused status from risk_state table.
    """
    status = {}
    with _db() as conn:
        if conn:
            try:
                rows = conn.execute(
                    """SELECT trader_id, vetoed, timestamp, reason
                       FROM risk_events
                       WHERE vetoed = true
                       ORDER BY timestamp DESC""",
                ).fetchall()
                seen = set()
                for r in rows:
                    agent_id = r["trader_id"]
                    if agent_id in seen:
                        continue
                    seen.add(agent_id)
                    from datetime import datetime, timezone, timedelta
                    evt_ts = r["timestamp"]
                    is_recent = False
                    try:
                        if isinstance(evt_ts, str):
                            evt_dt = datetime.fromisoformat(evt_ts)
                        else:
                            evt_dt = evt_ts
                        if evt_dt.tzinfo is None:
                            evt_dt = evt_dt.replace(tzinfo=timezone.utc)
                        is_recent = (datetime.now(timezone.utc) - evt_dt) < timedelta(hours=24)
                    except Exception:
                        pass
                    status[agent_id] = {
                        "paused": is_recent,
                        "pause_reason": r["reason"],
                        "pause_timestamp": r["timestamp"],
                    }
            except Exception:
                pass
    return jsonify({"kill_switch": status})


# ── API: /api/activity ────────────────────────────────────────────────────────

@app.route("/api/activity")
def api_activity():
    """
    Returns recent trade decisions from all three traders, sorted newest first.
    Source: state/{company}-decisions.jsonl
    """
    limit = int(request.args.get("limit", 50))

    # Collect and merge all three traders' decision logs
    all_events = []
    for company in ["kairos", "aldridge", "stonks"]:
        all_events.extend(_parse_decisions(company))

    # Sort descending by timestamp string (ISO format sorts correctly as string)
    all_events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    out = []
    for e in all_events[:limit]:
        dec   = e.get("decision") or {}
        order = e.get("order") or {}
        out.append({
            "timestamp":    e.get("timestamp"),
            "trader":       e.get("trader"),
            "action":       dec.get("action"),
            "ticker":       dec.get("ticker"),
            "quantity":     dec.get("quantity"),
            "confidence":   dec.get("confidence"),
            "thesis":       dec.get("thesis"),
            "stop_loss":    dec.get("stop_loss"),
            "order_status": order.get("status"),
            "order_id":     order.get("order_id"),
            # Only include error reason if the order actually errored
            "order_error":  order.get("reason") if order.get("status") == "error" else None,
        })

    return jsonify({"events": out})


# ── API: /api/journal ─────────────────────────────────────────────────────────

@app.route("/api/journal")
def api_journal():
    """
    Returns recent journal entries.
    Primary source: shared/cache.db journal table.
    Fallback: parse state/kairos-daily-log.md if DB is empty.
    """
    limit = int(request.args.get("limit", 30))
    entries = []

    with _db() as conn:
        if conn:
            try:
                rows = conn.execute(
                    "SELECT agent_id, timestamp, mood, entry, confidence "
                    "FROM trader_journal ORDER BY timestamp DESC LIMIT %s",
                    (limit,)
                ).fetchall()
                entries = [dict(r) for r in rows]
            except Exception:
                pass

    # Fallback: parse kairos daily log if DB trader_journal is empty
    if not entries:
        try:
            sections = (STATE / "kairos-daily-log.md").read_text().split("---")
            for section in reversed(sections):
                section = section.strip()
                if "### Thoughts" not in section:
                    continue
                thoughts_raw = section.split("### Thoughts")[-1]
                thoughts = "\n".join(
                    line.lstrip("- ").strip()
                    for line in thoughts_raw.splitlines()
                    if line.strip().startswith("-")
                )
                if not thoughts:
                    continue
                # Try to grab the date from the section header
                header = section.splitlines()[0].strip("# ").strip()
                entries.append({
                    "agent_id":   "trader-momentum",
                    "timestamp":  header,
                    "mood":       "",
                    "entry":      thoughts[:600],
                    "confidence": None,
                })
                if len(entries) >= limit:
                    break
        except Exception:
            pass

    return jsonify({"entries": entries[:limit]})


# ── API: /api/signals ─────────────────────────────────────────────────────────

@app.route("/api/signals")
def api_signals():
    """Returns recent trader signals — proxies from data_bus live, falls back to DB."""
    # Try data_bus first (live trader thoughts)
    try:
        import requests as _requests
        r = _requests.get("http://localhost:5000/signals", timeout=3)
        if r.status_code == 200:
            data = r.json()
            return jsonify(data)
    except Exception:
        pass

    # Fallback: ML signals from local DB
    limit = int(request.args.get("limit", 20))
    signals = []

    with _db() as conn:
        if conn:
            try:
                rows = conn.execute(
                    "SELECT trader_id AS agent_id, timestamp, ticker, "
                    "composite_signal AS signal, conviction AS confidence, regime "
                    "FROM signals ORDER BY timestamp DESC LIMIT %s",
                    (limit,)
                ).fetchall()
                signals = [dict(r) for r in rows]
            except Exception:
                pass

    return jsonify({"signals": signals})


# ── API: /api/heartbeat ───────────────────────────────────────────────────────

@app.route("/api/heartbeat")
def api_heartbeat():
    """Returns last heartbeat timestamp and age (seconds) for each trader."""
    hb = _load_json(STATE / "heartbeat-state.json")
    return jsonify({
        key: {"timestamp": ts, "ago_s": _seconds_ago(ts)}
        for key, ts in hb.items()
    })


# ── API: /api/options-proxy ──────────────────────────────────────────────────

@app.route("/api/options-proxy")
def api_options_proxy():
    """
    Proxy options chain data from Alpaca for a given underlying symbol.
    Returns contract details: strike, expiration, type, premium (close_price), open interest.
    Fetches from Alpaca's paper trading API.
    """
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol parameter required"}), 400

    try:
        import requests

        company = request.args.get("trader", "stonks")
        api_key_env, secret_env = _CRED_MAP.get(company, _CRED_MAP["stonks"])
        api_key = (os.getenv(f"ALPACA_{company.upper()}_KEY") or os.getenv(api_key_env))
        secret  = (os.getenv(f"ALPACA_{company.upper()}_SECRET") or os.getenv(secret_env))

        if not api_key or not secret:
            return jsonify({"error": "no API credentials"}), 503

        headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}
        base = "https://paper-api.alpaca.markets/v2/options"

        # Fetch contracts near the money, next few expirations
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y-%m-%d")
        end = (datetime.now() + timedelta(days=45)).strftime("%Y-%m-%d")

        r = requests.get(f"{base}/contracts", params={"underlying_symbols": symbol, "status": "active", "expiration_date_gte": today, "expiration_date_lte": end, "limit": 100}, headers=headers, timeout=10)

        if r.status_code != 200:
            return jsonify({"error": f"Alpaca returned {r.status_code}", "detail": r.text[:500]}), 502

        contracts = r.json().get("option_contracts", [])

        # Also try to get current quotes for these symbols
        if contracts:
            syms = [c["symbol"] for c in contracts]
            try:
                qr = requests.get(f"{base}/snapshots", params={"symbols": ",".join(syms[:50])}, headers=headers, timeout=10)
                if qr.status_code == 200:
                    snapshots = qr.json().get("snapshots", {})
                    for c in contracts:
                        snap = snapshots.get(c["symbol"], {})
                        if "latestQuote" in snap:
                            c["latest_bid"] = snap["latestQuote"].get("bp")
                            c["latest_ask"] = snap["latestQuote"].get("ap")
            except Exception:
                pass

        return jsonify({
            "symbol": symbol,
            "contracts": contracts,
            "fetched_at": datetime.now().isoformat(),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: /api/options-positions ───────────────────────────────────────────────

@app.route("/api/options-positions")
def api_options_positions():
    """
    Return open option positions across all traders with greeks.

    For each option position (OCC symbol), fetches snapshot data from Alpaca
    which includes greeks (delta, gamma, theta, vega, rho) and IV.
    Falls back gracefully if snapshot API is unavailable.
    """
    import requests as req

    results = []
    for company in ["kairos", "aldridge", "stonks"]:
        portfolio = None
        try:
            portfolio = _get_alpaca_portfolio(company)
        except Exception:
            continue

        if not portfolio or not portfolio.get("positions"):
            continue

        option_positions = [
            p for p in portfolio["positions"]
            if _is_option_symbol(p.get("ticker", p.get("symbol", "")))
        ]

        if not option_positions:
            continue

        # Get API credentials for this trader
        api_key_env, secret_env = _CRED_MAP.get(company, _CRED_MAP.get("stonks", ("", "")))
        api_key = os.getenv(f"ALPACA_{company.upper()}_KEY") or os.getenv(api_key_env)
        secret = os.getenv(f"ALPACA_{company.upper()}_SECRET") or os.getenv(secret_env)

        # Fetch greeks via snapshot API (batch by ticker for efficiency)
        snapshots_by_symbol = {}
        if api_key and secret:
            try:
                symbols = [p["ticker"] for p in option_positions if p.get("ticker")]
                if symbols:
                    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}
                    base = "https://paper-api.alpaca.markets/v2/options"
                    # Batch up to 50 symbols per request
                    for i in range(0, len(symbols), 50):
                        batch = symbols[i:i+50]
                        try:
                            r = req.get(
                                f"{base}/snapshots",
                                params={"symbols": ",".join(batch)},
                                headers=headers,
                                timeout=10,
                            )
                            if r.status_code == 200:
                                snapshots_by_symbol.update(r.json().get("snapshots", {}))
                        except Exception:
                            pass
            except Exception:
                pass  # Greeks are nice-to-have; positions still show without them

        for pos in option_positions:
            ticker = pos.get("ticker", pos.get("symbol", ""))
            snap = snapshots_by_symbol.get(ticker, {})

            # Parse OCC symbol: ROOT+YYMMDD+C/P+STRIKE*1000
            # e.g. AAPL250718C00210000 → AAPL, 2025-07-18, call, 210.00
            try:
                import re
                match = re.match(r"^([A-Z]+)(\d{6})([CP])(\d+)$", ticker)
                if match:
                    root = match.group(1)
                    date_str = match.group(2)
                    opt_type = "call" if match.group(3) == "C" else "put"
                    strike_raw = match.group(4)
                    strike = float(strike_raw) / 1000.0
                    exp_date = f"20{date_str[0:2]}-{date_str[2:4]}-{date_str[4:6]}"
                else:
                    root = ticker
                    opt_type = "unknown"
                    strike = 0.0
                    exp_date = ""
            except Exception:
                root = ticker
                opt_type = "unknown"
                strike = 0.0
                exp_date = ""

            # Calculate days to expiration
            dte = None
            if exp_date:
                try:
                    from datetime import date as dt_date
                    dte = (dt_date.fromisoformat(exp_date) - dt_date.today()).days
                except Exception:
                    pass

            # Extract greeks from snapshot
            greeks = {}
            if snap:
                greeks["delta"] = snap.get("delta")
                greeks["gamma"] = snap.get("gamma")
                greeks["theta"] = snap.get("theta")
                greeks["vega"] = snap.get("vega")
                greeks["rho"] = snap.get("rho")
                greeks["implied_volatility"] = snap.get("implied_volatility")
                # Current pricing from snapshot
                quote = snap.get("latestQuote", {})
                if quote:
                    greeks["bid"] = quote.get("bp")
                    greeks["ask"] = quote.get("ap")
                    greeks["mid"] = ((quote.get("bp", 0) or 0) + (quote.get("ap", 0) or 0)) / 2 if quote.get("bp") and quote.get("ap") else None

            results.append({
                "trader": company,
                "contract": ticker,
                "underlying": root,
                "option_type": opt_type,
                "strike": round(strike, 2),
                "expiration": exp_date,
                "dte": dte,
                "quantity": pos.get("qty") or pos.get("quantity", 0),
                "market_value": pos.get("market_value"),
                "unrealized_pl": pos.get("unrealized_pl") or pos.get("unrealized_intraday_pl"),
                "cost_basis": pos.get("cost_basis") or pos.get("avg_entry_price"),
                "current_price": pos.get("current_price"),
                "greeks": greeks,
            })

    return jsonify({
        "positions": results,
        "total": len(results),
        "fetched_at": datetime.now().isoformat(),
    })


# ── API: /api/watchlists ──────────────────────────────────────────────────────

@app.route("/api/watchlists")
def api_watchlists():
    """Returns each trader's current watchlist tickers from the DB."""
    out = {}
    with _db() as conn:
        if conn:
            try:
                for company in ["kairos", "aldridge", "stonks"]:
                    rows = conn.execute(
                        "SELECT ticker FROM trader_watchlist WHERE trader_id = %s ORDER BY added_at DESC",
                        (company,),
                    ).fetchall()
                    out[company] = [r["ticker"] for r in rows]
            except Exception:
                pass
    return jsonify(out)


# ── API: /api/wiki ────────────────────────────────────────────────────────────

WIKI_DIR = Path.home() / ".openclaw" / "wiki" / "main"
WIKI_CACHE = WIKI_DIR / ".openclaw-wiki" / "cache" / "agent-digest.json"
WIKI_REPORTS = WIKI_DIR / "reports"

@app.route("/api/wiki")
def api_wiki():
    """Returns memory wiki health summary from agent-digest.json and report files."""
    digest = {}
    if WIKI_CACHE.exists():
        try:
            digest = json.loads(WIKI_CACHE.read_text())
        except Exception:
            pass

    # Extract key counts from stale-pages and open-questions reports
    stale_count = 0
    open_q_count = 0
    contradiction_count = len(digest.get("contradictionClusters", []))

    if WIKI_REPORTS.exists():
        for rpt in ["stale-pages.md", "open-questions.md"]:
            path = WIKI_REPORTS / rpt
            if path.exists():
                text = path.read_text()
                import re
                if rpt == "stale-pages.md":
                    m = re.search(r"Stale pages:\s*(\d+)", text)
                    if m:
                        stale_count = int(m.group(1))
                elif rpt == "open-questions.md":
                    items = re.findall(r"^- .+", text, re.MULTILINE)
                    open_q_count = sum(1 for x in items if "No open questions" not in x)

    # Recent syntheses list
    synth_dir = WIKI_DIR / "syntheses"
    syntheses = []
    if synth_dir.exists():
        for f in sorted(synth_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
            syntheses.append({"name": f.stem.replace("-", " ").title(), "path": str(f.name)})

    # Entity list
    entity_dir = WIKI_DIR / "entities"
    entities = []
    if entity_dir.exists():
        for f in sorted(entity_dir.glob("*.md"))[:10]:
            entities.append(f.stem.replace("-", " ").title())

    return jsonify({
        "page_counts": digest.get("pageCounts", {}),
        "claim_count": digest.get("claimCount", 0),
        "claim_health": digest.get("claimHealth", {}),
        "stale_count": stale_count,
        "open_questions": open_q_count,
        "contradictions": contradiction_count,
        "syntheses": syntheses,
        "entities": entities,
        "digest_updated": datetime.fromtimestamp(
            WIKI_CACHE.stat().st_mtime
        ).isoformat() if WIKI_CACHE.exists() else None,
    })


# ── debug dashboard (trader internals) ────────────────────────────────────────

@app.route("/trader-debug")
def debug_dashboard():
    """Diagnostic dashboard — trader configs, DB stats, API keys, health."""
    import requests

    # ── DB Stats ──────────────────────────────────────────────────────────
    db_size_mb = 0
    table_counts = {}
    total_rows = 0
    with _db() as conn:
        if conn:
            try:
                tables = conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='trading' ORDER BY table_name"
                ).fetchall()
                for tname_row in tables:
                    tname = tname_row["table_name"]
                    try:
                        cnt = conn.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()["count"]
                        table_counts[tname] = cnt
                        total_rows += cnt
                    except Exception:
                        table_counts[tname] = "?"
            except Exception as e:
                table_counts = {"error": str(e)}
    db_size_mb = 0  # Postgres — size tracked on server if DB_PATH.exists() else 0

    # ── Trader configs ────────────────────────────────────────────────────
    trader_rows_html = ""
    with _db() as conn:
        if conn:
            try:
                for meta in TRADER_META:
                    cid = meta["id"]
                    # Agent state
                    st = conn.execute(
                        "SELECT * FROM agent_state WHERE agent_id=%s", (cid,)
                    ).fetchone()
                    # Config (use system_params as closest match)
                    cfg = conn.execute(
                        "SELECT * FROM system_params WHERE trader_id=%s LIMIT 1", (cid,)
                    ).fetchone()
                    # Watchlist count
                    wl = conn.execute(
                        "SELECT COUNT(*) as cnt FROM trader_watchlist WHERE agent_id=%s", (cid,)
                    ).fetchone()
                    # Recent decisions
                    dec = conn.execute(
                        "SELECT COUNT(*) as cnt FROM trader_decisions WHERE agent_id=%s", (cid,)
                    ).fetchone()
                    # Open positions
                    pos = conn.execute(
                        "SELECT COUNT(*) as cnt FROM trader_positions WHERE agent_id=%s AND status='open'", (cid,)
                    ).fetchone()

                    pv  = st["current_portfolio_value"] if st else 0
                    wr  = st["win_rate"] if st else 0
                    wt  = st["total_trades"] if st else 0
                    wl_c = wl["cnt"] if wl else 0
                    dc_c = dec["cnt"] if dec else 0
                    po_c = pos["cnt"] if pos else 0
                    freq = dict(cfg).get("polling_freq_sec", "?") if cfg else "?"

                    pnl = round(pv - STARTING_VALUE, 2) if pv else 0
                    pnl_color = "var(--green)" if pnl >= 0 else "var(--red)"
                    pnl_sign = "+" if pnl >= 0 else ""

                    trader_rows_html += f"""
                    <tr>
                      <td style="color:var(--{cid if cid != 'kairos' else 'blue'})">{meta['name']}</td>
                      <td>{meta['manager']}</td>
                      <td style="color:{pnl_color}">${pv:,.2f}</td>
                      <td style="color:{pnl_color}">{pnl_sign}${pnl:,.2f}</td>
                      <td>{wr:.1%}</td>
                      <td>{wt}</td>
                      <td>{dc_c}</td>
                      <td>{po_c}</td>
                      <td>{wl_c}</td>
                      <td>{freq}s</td>
                    </tr>"""
            except Exception as e:
                trader_rows_html = f'<tr><td colspan="10" style="color:var(--red)">Error: {e}</td></tr>'

    # ── API Key Status ────────────────────────────────────────────────────
    def _mask(k):
        if not k: return "—"
        return k[:3] + "***" + k[-3:] if len(k) > 6 else k[:2] + "****"

    key_rows = ""
    for var in ["ALPACA_KAIROS_KEY", "ALPACA_ALDRIDGE_KEY", "ALPACA_STONKS_KEY",
                 "FINNHUB_API_KEY", "ALPHA_VANTAGE_API_KEY"]:
        val = os.getenv(var)
        st_color = "var(--green)" if val else "var(--red)"
        st_text = "✓ configured" if val else "✗ missing"
        key_rows += f"<tr><td style='font-size:11px'>{var}</td><td style='color:{st_color}'>{st_text}</td><td style='font-size:11px'>{_mask(val)}</td></tr>"

    # ── ML Worker Health ─────────────────────────────────────────────────
    ml_status = "unknown"
    ml_color = "var(--yellow)"
    try:
        r = requests.get("http://192.168.1.237:5005/health", timeout=3)
        if r.status_code == 200:
            d = r.json()
            ml_status = f"healthy ({d.get('models_loaded', '?')} models)"
            ml_color = "var(--green)"
        else:
            ml_status = f"HTTP {r.status_code}"
            ml_color = "var(--red)"
    except Exception:
        ml_status = "unreachable"
        ml_color = "var(--red)"

    # ── Data Bus Health ──────────────────────────────────────────────────
    dbus_status = "unknown"
    dbus_color = "var(--yellow)"
    dbus_uptime = "—"
    try:
        r = requests.get("http://localhost:5000/health", timeout=3)
        if r.status_code == 200:
            d = r.json()
            dbus_status = "ok"
            dbus_color = "var(--green)"
            dbus_uptime = f"{d.get('uptime_seconds', 0):.0f}s"
        else:
            dbus_status = f"HTTP {r.status_code}"
            dbus_color = "var(--red)"
    except Exception:
        dbus_status = "unreachable"
        dbus_color = "var(--red)"

    # ── Recent ML Signals ────────────────────────────────────────────────
    sig_rows = ""
    with _db() as conn:
        if conn:
            try:
                rows = conn.execute(
                    "SELECT ticker, composite_signal AS signal, conviction AS confidence, regime, timestamp "
                    "FROM signals ORDER BY timestamp DESC LIMIT 15"
                ).fetchall()
                for r in rows:
                    sc = {"bullish": "var(--green)", "bearish": "var(--red)", "neutral": "var(--muted)"}.get(r["signal"], "var(--muted)")
                    sig_rows += f"<tr><td>{r['ticker']}</td><td style='color:{sc}'>{r['signal'] or '—'}</td><td>{r['regime'] or '—'}</td><td>{r['confidence']:.0%}</td><td style='font-size:11px'>{r['timestamp'][:19] if r['timestamp'] else '—'}</td></tr>"
            except Exception:
                sig_rows = "<tr><td colspan='5' style='color:var(--muted)'>no data</td></tr>"

    if not sig_rows:
        sig_rows = "<tr><td colspan='5' style='color:var(--muted)'>no signals yet</td></tr>"

    # ── MCP Status ────────────────────────────────────────────────────────
    mcp_html = ""
    try:
        r = requests.get("http://localhost:5000/mcp-status", timeout=3)
        if r.status_code == 200:
            mcp = r.json()
            total = mcp.get("total", 0)
            conn = mcp.get("connected_count", 0)
            rows = ""
            for name, srv in sorted(mcp.get("servers", {}).items()):
                if srv.get("enabled"):
                    if srv.get("connected"):
                        st = "🟢"
                        detail = "connected"
                    elif srv.get("error_count", 0) > 0:
                        st = "🔴"
                        detail = f'{srv["error_count"]} errors'
                    else:
                        st = "🟡"
                        detail = "enabled, not connected"
                else:
                    st = "⏸️"
                    detail = "disabled"
                rows += f"<tr><td>{name}</td><td>{st}</td><td style='font-size:11px'>{detail}</td><td style='font-size:11px;color:var(--muted)'>{srv.get('transport', '')}</td></tr>"
            mcp_html = f'''
<h2>🔌 MCP Servers [{conn}/{total} connected]</h2>
<table>
<tr><th>Server</th><th>Status</th><th>Detail</th><th>Transport</th></tr>
{rows}
</table>'''
    except Exception:
        mcp_html = "<h2>🔌 MCP Servers</h2><p style='color:var(--red)'>unreachable</p>"

    # ── Calendar ────────────────────────────────────────────────────────────
    calendar_html = ""
    try:
        r = requests.get("http://localhost:5000/calendar", timeout=3)
        if r.status_code == 200:
            cal = r.json()
            mk = "open" if cal.get("market_open") else "closed"
            mk_color = "var(--green)" if cal.get("market_open") else "var(--red)"
            events = ", ".join(cal.get("today_events", [])) or "none"
            calendar_html = f'''
<h2>📅 Calendar</h2>
<table>
<tr><th>Date</th><td style="color:{mk_color}">{cal['today']} ({cal['time_et']} ET)</td></tr>
<tr><th>Market</th><td style="color:{mk_color}">{mk}</td></tr>
<tr><th>Today's Events</th><td>{events}</td></tr>
<tr><th>Next FOMC</th><td>{cal.get('next_fomc', '—')}</td></tr>
<tr><th>Next CPI</th><td>{cal.get('next_cpi', '—')}</td></tr>
<tr><th>Next NFP</th><td>{cal.get('next_nfp', '—')}</td></tr>
<tr><th>Next Holiday</th><td>{cal.get('next_holiday', '—')}</td></tr>
</table>'''
    except Exception:
        pass

    # ── Render ────────────────────────────────────────────────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Trader Diagnostics</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#0d1117;--surface:#161b22;--surface2:#1c2128;--border:#30363d;
  --text:#e6edf3;--muted:#8b949e;--green:#3fb950;--red:#f85149;
  --yellow:#d29922;--blue:#58a6ff;--orange:#f0883e;--purple:#bc8cff;
}}
body{{background:var(--bg);color:var(--text);font-family:'JetBrains Mono','Consolas',monospace;font-size:13px;line-height:1.5;padding:16px}}
h1{{color:var(--blue);font-size:18px;margin-bottom:4px}}
h2{{color:var(--muted);font-size:14px;border-bottom:1px solid var(--border);padding-bottom:4px;margin:16px 0 8px}}
table{{width:100%;border-collapse:collapse;margin-bottom:12px}}
th,td{{text-align:left;padding:4px 8px;font-size:13px}}
th{{color:var(--muted);font-weight:bold;border-bottom:1px solid var(--border)}}
td{{border-bottom:1px solid var(--surface2)}}
.badge{{display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:bold}}
.footer{{color:var(--muted);font-size:11px;margin-top:16px;border-top:1px solid var(--border);padding-top:8px}}
</style>
</head>
<body>
<h1>🔍 Trader Diagnostics</h1>
<p style="color:var(--muted);font-size:12px;margin-bottom:12px">
  <a href="/" style="color:var(--blue)">← back to dashboard</a> &nbsp;|&nbsp;
  <a href="https://trading.wodinga.studio/dashboard" style="color:var(--blue)">data bus dash</a> &nbsp;|&nbsp;
  <a href="https://trading.wodinga.studio/debug" style="color:var(--blue)">data bus debug</a>
</p>

<h2>👥 Traders</h2>
<table>
<tr><th>Firm</th><th>Manager</th><th>Portfolio</th><th>P&amp;L</th><th>Win Rate</th><th>Trades</th><th>Positions</th><th>Invested</th></tr>
{trader_rows_html}
</table>

<h2>📊 Database ({db_size_mb} MB · {total_rows} rows · {len(table_counts)} tables)</h2>
<table>
<tr><th>Table</th><th>Rows</th></tr>
{"".join(f"<tr><td>{t}</td><td>{c}</td></tr>" for t, c in sorted(table_counts.items()))}
</table>

<h2>🔑 API Keys</h2>
<table>
<tr><th>Variable</th><th>Status</th><th>Value</th></tr>
{key_rows}
</table>

<h2>🩺 Health</h2>
<table>
<tr><th>Service</th><th>Status</th><th>Detail</th></tr>
<tr><td>ML Worker</td><td style="color:{ml_color}">{ml_status}</td><td>192.168.1.237:5005</td></tr>
<tr><td>Data Bus</td><td style="color:{dbus_color}">{dbus_status}</td><td>uptime {dbus_uptime}</td></tr>
<tr><td>Leaderboard</td><td style="color:var(--green)">running</td><td>port 5002</td></tr>
</table>

{mcp_html}

{calendar_html}

<h2>📶 Recent ML Signals</h2>
<table>
<tr><th>Ticker</th><th>Signal</th><th>Regime</th><th>Confidence</th><th>Timestamp</th></tr>
{sig_rows}
</table>

<div class="footer">Trader Diagnostics · {now} · auto-refresh 30s · LAN-only</div>
</body>
</html>"""


# ── API: /api/findings ────────────────────────────────────────────────────

@app.route("/api/findings")
def api_findings():
    """Returns historical sim findings — sweep results from shared/trader.db.

    Query parameters:
        trader: Filter by trader name (optional).
        limit: Max results (default 20).

    Returns:
        JSON list of sweep result dicts with performance metrics.
    """
    trader = request.args.get("trader", "")
    limit = int(request.args.get("limit", 20))

    results = []
    try:
        import sqlite3
        db_path = ROOT / "shared" / "trader.db"
        if not db_path.exists():
            return jsonify({"findings": [], "count": 0, "db_path": str(db_path), "error": "no db file"})

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Check if sweep_results table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sweep_results'")
        if not cur.fetchone():
            conn.close()
            return jsonify({"findings": [], "count": 0, "status": "no_sweep_results_table"})

        query = "SELECT * FROM sweep_results"
        params = []
        if trader:
            query += " WHERE trader = ?"
            params.append(trader)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = cur.execute(query, params).fetchall()
        for r in rows:
            d = dict(r)
            # Parse params JSON if present
            if d.get("params"):
                try:
                    d["params"] = json.loads(d["params"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        conn.close()
    except Exception as e:
        return jsonify({"findings": [], "count": 0, "error": str(e)})

    return jsonify({
        "findings": results,
        "count": len(results),
        "trader_filter": trader or "all",
    })


# ── API: /api/virtual-traders ────────────────────────────────────────────

@app.route("/api/virtual-traders")
def api_virtual_traders():
    """
    Return all virtual traders with their params, P&L, configs.
    Aggregates trade P&L from executed_trades/virtual_traders/trades tables.
    """
    result = []
    with _db() as conn:
        if not conn:
            return jsonify({"virtual_traders": [], "updated_at": datetime.now().isoformat()})
        try:
            rows = conn.execute(
                """SELECT v.id, v.name, v.base_trader, v.variant_type, v.config, v.status,
                          v.created_at, v.culled_at, v.wins
                   FROM trading.virtual_traders v
                   ORDER BY v.base_trader, v.created_at DESC"""
            ).fetchall()
            for r in rows:
                config = r["config"]
                if isinstance(config, str):
                    try:
                        config = json.loads(config)
                    except Exception:
                        config = {}
                # Compute total P&L from trades with this virtual trader name
                total_pnl = None
                total_trades = 0
                try:
                    pnl_cur = conn.execute(
                        """SELECT COUNT(*) as cnt, COALESCE(SUM(t.pnl),0) as total_pnl
                           FROM trading.trades t
                           WHERE t.trade_source = 'virtual'
                             AND t.strategy = %s
                             AND t.agent_id = %s""",
                        (r["name"], f"trader-{r['base_trader']}"),
                    )
                    pnl_row = pnl_cur.fetchone()
                    if pnl_row:
                        total_trades = pnl_row["cnt"] or 0
                        total_pnl = round(float(pnl_row["total_pnl"] or 0), 2)
                except Exception:
                    pass

                result.append({
                    "id": r["id"],
                    "name": r["name"],
                    "base_trader": r["base_trader"],
                    "variant_type": r["variant_type"],
                    "config": config,
                    "status": r["status"],
                    "wins": r["wins"] or 0,
                    "total_trades": total_trades,
                    "total_pnl": total_pnl,
                    "created_at": str(r["created_at"]) if r["created_at"] else None,
                    "culled_at": str(r["culled_at"]) if r["culled_at"] else None,
                })
        except Exception as e:
            return jsonify({"error": str(e), "virtual_traders": []}), 500

    return jsonify({
        "virtual_traders": result,
        "count": len(result),
        "updated_at": datetime.now().isoformat(),
    })


# ── API: /api/virtual-trader/<name> ─────────────────────────────────

@app.route("/api/virtual-trader/<path:name>")
def api_virtual_trader_detail(name):
    """Return full detail for a single virtual trader including recent trades."""
    result = None
    trades = []
    with _db() as conn:
        if not conn:
            return jsonify({"error": "DB unavailable"}), 503
        try:
            row = conn.execute(
                """SELECT v.id, v.name, v.base_trader, v.variant_type, v.config,
                          v.status, v.created_at, v.culled_at, v.wins
                   FROM trading.virtual_traders v WHERE v.name = %s""",
                (name,),
            ).fetchone()
            if row:
                config = row["config"]
                if isinstance(config, str):
                    try:
                        config = json.loads(config)
                    except Exception:
                        config = {}
                result = {
                    "id": row["id"],
                    "name": row["name"],
                    "base_trader": row["base_trader"],
                    "variant_type": row["variant_type"],
                    "config": config,
                    "status": row["status"],
                    "wins": row["wins"] or 0,
                    "created_at": str(row["created_at"]) if row["created_at"] else None,
                    "culled_at": str(row["culled_at"]) if row["culled_at"] else None,
                }
                # Recent trades
                tcur = conn.execute(
                    """SELECT t.id, t.timestamp, t.ticker, t.action, t.quantity,
                              t.price, t.pnl, t.status
                       FROM trading.trades t
                       WHERE t.trade_source = 'virtual'
                         AND t.strategy = %s
                       ORDER BY t.timestamp DESC LIMIT 50""",
                    (name,),
                )
                for t in tcur.fetchall():
                    trades.append({
                        "id": t["id"],
                        "timestamp": str(t["timestamp"]) if t["timestamp"] else None,
                        "ticker": t["ticker"],
                        "action": t["action"],
                        "quantity": t["quantity"],
                        "price": float(t["price"]) if t["price"] else None,
                        "pnl": float(t["pnl"]) if t["pnl"] else None,
                        "status": t["status"],
                    })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    if not result:
        return jsonify({"error": "Virtual trader not found"}), 404
    return jsonify({"virtual_trader": result, "trades": trades})


# ── API: /api/trader-files/<trader_id> ──────────────────────────────

@app.route("/api/trader-files/<trader_id>")
def api_trader_files(trader_id):
    """
    Return AGENTS.md, SOUL.md, TOOLS.md, HEARTBEAT.md for a base trader.
    Looks in both agents/ and trading-agent-prompts/ directories.
    Returns file contents keyed by filename.
    """
    files_map = {}
    # Normalize trader_id: strip 'trader-' prefix if present
    tid = trader_id.replace("trader-", "")

    # Search paths in priority order
    search_paths = [
        ROOT / "agents" / f"trader-{tid}",
        Path.home() / "projects" / "trading-agent-prompts" / tid,
        Path.home() / "projects" / "trading-agent-prompts" / f"trader-{tid}",
        ROOT / "agents" / tid,
    ]

    filenames = ["AGENTS.md", "SOUL.md", "TOOLS.md", "HEARTBEAT.md", "IDENTITY.md", "MEMORY.md"]

    for base in search_paths:
        if not base.exists():
            continue
        for fn in filenames:
            fp = base / fn
            if fp.exists() and fn not in files_map:
                try:
                    content = fp.read_text()
                    files_map[fn] = {
                        "filename": fn,
                        "content": content,
                        "size": len(content),
                        "path": str(fp.relative_to(base.parent) if fp.is_relative_to(base.parent) else fp),
                        "mtime": datetime.fromtimestamp(fp.stat().st_mtime).isoformat(),
                    }
                except Exception:
                    pass

    return jsonify({
        "trader_id": trader_id,
        "files": files_map,
        "count": len(files_map),
    })


# ── API: /api/eval-results ──────────────────────────────────────────

@app.route("/api/eval-results")
def api_eval_results():
    """
    Return multi-timeframe evaluation results for all traders.
    Timeframes: 1d, 5d, 20d, 90d performance windows.
    Sources: agent_benchmark_comparison, portfolio_snapshots, agent_profile.
    """
    from datetime import timedelta

    timeframes = {
        "1d": timedelta(days=1),
        "5d": timedelta(days=5),
        "20d": timedelta(days=20),
        "90d": timedelta(days=90),
    }

    result = {}
    with _db() as conn:
        if not conn:
            return jsonify({"error": "DB unavailable"}), 503
        try:
            # Get all available agent IDs from our trader meta
            for meta in TRADER_META:
                agent_id = f"trader-{meta['id']}"
                tf_data = {}
                for tf_name, delta in timeframes.items():
                    try:
                        s_cur = conn.execute(
                            """SELECT timestamp, portfolio_value, cash
                               FROM portfolio_snapshots
                               WHERE trader_id = %s
                                 AND timestamp >= NOW() - %s::interval
                               ORDER BY timestamp ASC""",
                            (agent_id, tf_name),
                        )
                        snapshots = s_cur.fetchall()
                        if snapshots:
                            start_val = float(snapshots[0]["portfolio_value"])
                            end_val = float(snapshots[-1]["portfolio_value"])
                            ret = round((end_val - start_val) / start_val * 100, 2) if start_val else 0
                            tf_data[tf_name] = {
                                "return_pct": ret,
                                "start_value": round(start_val, 2),
                                "end_value": round(end_val, 2),
                                "start_date": str(snapshots[0]["timestamp"]),
                                "end_date": str(snapshots[-1]["timestamp"]),
                                "snapshots_count": len(snapshots),
                            }
                        else:
                            tf_data[tf_name] = {"return_pct": None, "reason": "No data"}
                    except Exception as e:
                        tf_data[tf_name] = {"return_pct": None, "error": str(e)}

                # Win rates per timeframe from trades
                try:
                    wr_cur = conn.execute(
                        """SELECT COUNT(*) as total,
                                  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
                           FROM trading.trades
                           WHERE agent_id = %s
                             AND timestamp >= NOW() - INTERVAL '90 days'
                             AND pnl IS NOT NULL""",
                        (agent_id,),
                    )
                    wr_row = wr_cur.fetchone()
                    total_90d = wr_row["total"] or 0
                    wins_90d = wr_row["wins"] or 0
                    win_rate_90d = round(wins_90d / total_90d, 4) if total_90d > 0 else None
                except Exception:
                    total_90d = 0
                    wins_90d = 0
                    win_rate_90d = None

                result[meta["id"]] = {
                    "name": meta["name"],
                    "manager": meta["manager"],
                    "timeframes": tf_data,
                    "90d_wins": wins_90d,
                    "90d_total_trades": total_90d,
                    "90d_win_rate": win_rate_90d,
                }
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"results": result, "timeframes": list(timeframes.keys())})


# ── API: /api/git-branches ──────────────────────────────────────────

GIT_REPOS = [
    ("paper-trading-rebuild", ROOT),
    ("trading-agent-prompts", Path.home() / "projects" / "trading-agent-prompts"),
]

@app.route("/api/git-branches")
def api_git_branches():
    """
    Return git branches for tracked repos, with recent commit metrics.
    Shows which branches are running and their activity.
    """
    repos = []
    for name, repo_path in GIT_REPOS:
        if not repo_path.exists() or not (repo_path / ".git").exists():
            continue
        try:
            import subprocess
            # Get branches and their last commit info
            branches = []
            result = subprocess.run(
                ["git", "branch", "-a"],
                capture_output=True, text=True, timeout=5,
                cwd=str(repo_path),
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                is_current = line.startswith("* ")
                branch_name = line.replace("* ", "").strip()
                if not branch_name:
                    continue
                # Get last commit info for this branch
                try:
                    log_res = subprocess.run(
                        ["git", "log", branch_name, "-1",
                         "--format=%H|%ai|%s"],
                        capture_output=True, text=True, timeout=5,
                        cwd=str(repo_path),
                    )
                    commit_info = log_res.stdout.strip().split("|") if log_res.stdout else []
                    hash_val = commit_info[0][:12] if len(commit_info) > 0 else ""
                    date_val = commit_info[1] if len(commit_info) > 1 else ""
                    msg_val = commit_info[2][:80] if len(commit_info) > 2 else ""
                except Exception:
                    hash_val = ""
                    date_val = ""
                    msg_val = ""

                branches.append({
                    "name": branch_name,
                    "current": is_current,
                    "last_commit_hash": hash_val,
                    "last_commit_date": date_val,
                    "last_commit_msg": msg_val,
                })

            # Get current branch
            try:
                cur_res = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                    cwd=str(repo_path),
                )
                current_branch = cur_res.stdout.strip()
            except Exception:
                current_branch = ""

            # Get recent commits on main/master
            recent_commits = []
            try:
                log_res = subprocess.run(
                    ["git", "log", "--oneline", "-10", "--no-decorate"],
                    capture_output=True, text=True, timeout=5,
                    cwd=str(repo_path),
                )
                for line in log_res.stdout.splitlines():
                    parts = line.strip().split(" ", 1)
                    if len(parts) == 2:
                        recent_commits.append({
                            "hash": parts[0],
                            "message": parts[1],
                        })
            except Exception:
                pass

            repos.append({
                "name": name,
                "path": str(repo_path),
                "current_branch": current_branch,
                "branches": branches,
                "branch_count": len(branches),
                "recent_commits": recent_commits,
            })
        except Exception as e:
            repos.append({"name": name, "error": str(e)})

    return jsonify({"repos": repos})


# ── API: /api/correlations ──────────────────────────────────────────

@app.route("/api/correlations")
def api_correlations():
    """
    Return prompt changes ↔ performance outcome correlations.
    Sources: prompt_sweep results, prompt_versioning table, prompt_tiering changes.
    Shows which prompt changes led to positive/negative performance shifts.
    """
    correlations = []
    sweep_results = []
    version_history = []

    with _db() as conn:
        if not conn:
            return jsonify({"correlations": [], "sweep_results": []})
        try:
            # 1. Sweep results from prompt_sweep runs
            try:
                cur = conn.execute(
                    """SELECT id, prompt_version, variant_name, trader_id,
                              pnl_change_pct, win_rate_change, sharpe_change,
                              timestamp, sweep_run_id
                       FROM trading.prompt_sweep_results
                       ORDER BY timestamp DESC LIMIT 50"""
                )
                for r in cur.fetchall():
                    sweep_results.append({
                        "id": r["id"],
                        "prompt_version": r["prompt_version"],
                        "variant_name": r["variant_name"],
                        "trader_id": r["trader_id"],
                        "pnl_change_pct": round(float(r["pnl_change_pct"]), 2) if r["pnl_change_pct"] else None,
                        "win_rate_change": round(float(r["win_rate_change"]), 4) if r["win_rate_change"] else None,
                        "sharpe_change": round(float(r["sharpe_change"]), 2) if r["sharpe_change"] else None,
                        "timestamp": str(r["timestamp"]) if r["timestamp"] else None,
                        "sweep_run_id": r["sweep_run_id"],
                    })
            except Exception:
                pass

            # 2. Prompt version history
            try:
                cur = conn.execute(
                    """SELECT id, trader_id, version, diff_summary, performance_before,
                              performance_after, applied_at
                       FROM trading.prompt_versions
                       ORDER BY applied_at DESC LIMIT 50"""
                )
                for r in cur.fetchall():
                    version_history.append({
                        "id": r["id"],
                        "trader_id": r["trader_id"],
                        "version": r["version"],
                        "diff_summary": r["diff_summary"],
                        "performance_before": r["performance_before"],
                        "performance_after": r["performance_after"],
                        "applied_at": str(r["applied_at"]) if r["applied_at"] else None,
                    })
            except Exception:
                pass

            # 3. Build correlation insights from sweep data
            if sweep_results:
                # Group by prompt_version
                version_pnl = {}
                for r in sweep_results:
                    ver = r["prompt_version"] or "unknown"
                    if ver not in version_pnl:
                        version_pnl[ver] = {"pnl_changes": [], "win_rate_changes": [], "count": 0}
                    if r["pnl_change_pct"] is not None:
                        version_pnl[ver]["pnl_changes"].append(r["pnl_change_pct"])
                    if r["win_rate_change"] is not None:
                        version_pnl[ver]["win_rate_changes"].append(r["win_rate_change"])
                    version_pnl[ver]["count"] += 1

                for ver, data in sorted(version_pnl.items(), key=lambda x: x[1]["count"], reverse=True):
                    avg_pnl = round(sum(data["pnl_changes"]) / len(data["pnl_changes"]), 2) if data["pnl_changes"] else None
                    avg_wr = round(sum(data["win_rate_changes"]) / len(data["win_rate_changes"]) * 100, 2) if data["win_rate_changes"] else None
                    correlations.append({
                        "prompt_version": ver,
                        "test_count": data["count"],
                        "avg_pnl_change_pct": avg_pnl,
                        "avg_win_rate_change_pct": avg_wr,
                        "direction": "improvement" if (avg_pnl or 0) > 0 else ("regression" if (avg_pnl or 0) < 0 else "neutral"),
                        "samples": data["pnl_changes"][:5],  # first 5 raw samples
                    })

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({
        "correlations": correlations,
        "sweep_results": sweep_results,
        "version_history": version_history,
    })


# ── API: /api/promote-virtual ────────────────

@app.route("/api/promote-virtual/<int:virtual_id>", methods=["POST"])
def api_promote_virtual(virtual_id):
    """Promote a virtual trader to live: copy its config to the base trader."""
    with _db() as conn:
        if not conn:
            return jsonify({"error": "DB unavailable"}), 503
        try:
            row = conn.execute(
                "SELECT name, base_trader, variant_type, config FROM trading.virtual_traders WHERE id = %s",
                (virtual_id,),
            ).fetchone()
            if not row:
                return jsonify({"error": "Virtual trader not found"}), 404

            config = row["config"]
            if isinstance(config, str):
                config = json.loads(config)

            trader_id = f"trader-{row['base_trader']}"
            # Log promotion to rotation_log
            conn.execute(
                """INSERT INTO trading.rotation_log
                   (date, base_trader, live_virtual, reason)
                   VALUES (CURRENT_DATE, %s, %s, 'promoted via dashboard')""",
                (row["base_trader"], row["name"]),
            )
            # Mark virtual as promoted
            conn.execute(
                "UPDATE trading.virtual_traders SET status = 'promoted' WHERE id = %s",
                (virtual_id,),
            )
            return jsonify({"success": True, "message": f"Promoted {row['name']} to live trader {trader_id}"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


# ── static frontend ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the dashboard frontend."""
    return send_from_directory(str(UI_DIR), "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(str(UI_DIR), filename)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Paper Trading Dashboard")
    p.add_argument("--port", type=int, default=5002, help="Port to listen on")
    p.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    args = p.parse_args()
    print(f"[Dashboard] Serving at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
