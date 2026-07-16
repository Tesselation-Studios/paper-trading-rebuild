#!/usr/bin/env python3
"""
strategy_evolution — Weekly parameter optimization for paper trading agents.

Analyzes 7-day trade performance, identifies parameter drift, and suggests
data-driven parameter tweaks. Runs as a Saturday morning cron job (or on-demand).

Ties together:
  - src/param_history.py  (parameter change tracking)
  - src/signals.py        (SignalParams definitions)
  - src/db/connection.py  (Postgres access)

Usage:
    python3 -m src.strategy_evolution --all                          # All traders
    python3 -m src.strategy_evolution --agent trader-kairos          # Single trader
    python3 -m src.strategy_evolution --dry-run                      # Preview only
    python3 -m src.strategy_evolution --all --apply                  # Apply suggestions
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.param_history import ParamHistory
from src.signals import SignalParams
from src.db.connection import get_connection

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════

DB_DSN = os.getenv("PG_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")
BASE_TRADERS = ["kairos", "aldridge", "stonks"]
LOOKBACK_DAYS = 7
MIN_TRADES_FOR_ANALYSIS = 3
MAX_SUGGESTIONS_PER_TRADER = 5

# Parameter bounds — prevent wild swings
PARAM_BOUNDS: Dict[str, Tuple[float, float]] = {
    "momentum_threshold": (0.01, 0.30),
    "rsi_oversold": (15.0, 35.0),
    "rsi_overbought": (65.0, 85.0),
    "bollinger_std": (1.5, 3.0),
    "volume_threshold": (1.0, 3.0),
    "vol_regime_threshold": (0.5, 3.0),
    "vol_reduction_multiplier": (0.2, 1.0),
    "base_size_pct": (0.02, 0.25),
    "conviction_multiplier": (0.5, 2.0),
    "max_positions": (3, 15),
    "stop_loss_pct": (0.01, 0.10),
    "take_profit_pct": (0.02, 0.20),
    "trailing_stop_pct": (0.005, 0.05),
    "weight_trending_up": (0.0, 1.0),
    "weight_trending_down": (0.0, 1.0),
    "weight_mean_reverting": (0.0, 1.0),
    "weight_high_volatility": (0.0, 1.0),
}

# How much a parameter can move per week (fraction of current value)
MAX_WEEKLY_DELTA_FRACTION = 0.15

# ═══════════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class WeeklyStats:
    """Aggregated weekly stats for a trader."""
    agent_id: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_hold_hours: float = 0.0
    best_ticker: str = ""
    worst_ticker: str = ""
    ticker_performance: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    pnl_by_signal: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    pnl_by_sector: Dict[str, Tuple[int, float]] = field(default_factory=dict)


@dataclass
class ParamSuggestion:
    """A single parameter change suggestion."""
    agent_id: str
    param_name: str
    current_value: float
    suggested_value: float
    delta: float
    delta_pct: float
    reason: str
    confidence: float  # 0.0 — 1.0
    evidence: str  # supporting data point


# ═══════════════════════════════════════════════════════════════════════════════
# Database queries
# ═══════════════════════════════════════════════════════════════════════════════


def _query(conn, sql: str, params: tuple = ()) -> list:
    """Execute a query and return results as list of dicts."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def get_weekly_trades(conn, agent_id: str, lookback_days: int = LOOKBACK_DAYS) -> list:
    """Fetch closed trades from the past N days."""
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    sql = """
        SELECT ticker, action, entry_price, exit_price, quantity,
               pnl, pnl_pct, entry_time, exit_time, exit_reason, entry_reason
        FROM trading.executed_trades
        WHERE agent_id = %s
          AND status = 'closed'
          AND exit_time >= %s
          AND pnl IS NOT NULL
        ORDER BY exit_time DESC
    """
    return _query(conn, sql, (agent_id, since))


def get_current_params(conn, agent_id: str) -> Dict[str, float]:
    """Extract current parameter values from agent profile JSONB."""
    sql = """
        SELECT performance->>'params' as params_json
        FROM trading.agent_profile
        WHERE agent_id = %s
    """
    rows = _query(conn, sql, (agent_id,))
    if not rows or not rows[0].get("params_json"):
        return {}

    try:
        params = json.loads(rows[0]["params_json"])
        if isinstance(params, dict):
            return {k: float(v) for k, v in params.items()
                    if isinstance(v, (int, float))}
    except (json.JSONDecodeError, ValueError, TypeError):
        log.warning("Could not parse params for %s", agent_id)

    return {}


def get_agent_state(conn, agent_id: str) -> Optional[dict]:
    """Get current agent operational state."""
    sql = """
        SELECT is_active, cash, equity, pnl, pnl_pct, positions_count,
               last_heartbeat, last_trade
        FROM trading.agent_state
        WHERE agent_id = %s
    """
    rows = _query(conn, sql, (agent_id,))
    return rows[0] if rows else None


def get_equity_history(conn, agent_id: str, lookback_days: int = LOOKBACK_DAYS) -> list:
    """Get daily P&L history for trend analysis."""
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    sql = """
        SELECT date, equity, pnl, pnl_pct
        FROM trading.daily_pnl
        WHERE agent_id = %s AND date >= %s
        ORDER BY date ASC
    """
    return _query(conn, sql, (agent_id, since))


# ═══════════════════════════════════════════════════════════════════════════════
# Analysis
# ═══════════════════════════════════════════════════════════════════════════════


def compute_weekly_stats(trades: list, agent_id: str) -> WeeklyStats:
    """Compute aggregated stats from a list of trade records."""
    stats = WeeklyStats(agent_id=agent_id)
    stats.total_trades = len(trades)

    if not trades:
        return stats

    ticker_pnl: Dict[str, float] = defaultdict(float)
    ticker_count: Dict[str, int] = defaultdict(int)
    signal_pnl: Dict[str, float] = defaultdict(float)
    signal_count: Dict[str, int] = defaultdict(int)
    sector_pnl: Dict[str, float] = defaultdict(float)
    sector_count: Dict[str, int] = defaultdict(int)
    win_pcts: list = []
    loss_pcts: list = []
    total_wins_pnl: float = 0
    total_losses_pnl: float = 0

    for trade in trades:
        pnl = float(trade["pnl"] or 0)
        pnl_pct = float(trade["pnl_pct"] or 0)
        ticker = trade["ticker"]
        reason = (trade.get("entry_reason") or "").lower()

        stats.total_pnl += pnl
        ticker_pnl[ticker] += pnl
        ticker_count[ticker] += 1

        # Classify signal from entry_reason
        signal = _classify_signal(reason)
        signal_pnl[signal] += pnl
        signal_count[signal] += 1

        if pnl > 0:
            stats.wins += 1
            total_wins_pnl += pnl
            win_pcts.append(pnl_pct)
        else:
            stats.losses += 1
            total_losses_pnl += abs(pnl)
            loss_pcts.append(abs(pnl_pct))

        # Compute hold time
        if trade.get("entry_time") and trade.get("exit_time"):
            hold = trade["exit_time"] - trade["entry_time"]
            stats.avg_hold_hours += hold.total_seconds() / 3600

    # Win rate
    if stats.total_trades > 0:
        stats.win_rate = stats.wins / stats.total_trades

    # Average win/loss %
    stats.avg_win_pct = sum(win_pcts) / len(win_pcts) if win_pcts else 0
    stats.avg_loss_pct = sum(loss_pcts) / len(loss_pcts) if loss_pcts else 0

    # Profit factor
    stats.profit_factor = (
        total_wins_pnl / total_losses_pnl if total_losses_pnl > 0 else float("inf")
    )

    # Average hold hours
    if stats.total_trades > 0:
        stats.avg_hold_hours /= stats.total_trades

    # Best/worst ticker
    if ticker_pnl:
        stats.best_ticker = max(ticker_pnl, key=lambda k: ticker_pnl[k])
        stats.worst_ticker = min(ticker_pnl, key=lambda k: ticker_pnl[k])

    # Per-ticker stats
    for ticker in ticker_pnl:
        count = ticker_count[ticker]
        stats.ticker_performance[ticker] = (count, ticker_pnl[ticker])

    # Per-signal stats
    for sig in signal_pnl:
        count = signal_count[sig]
        stats.pnl_by_signal[sig] = (count, signal_pnl[sig])

    return stats


def _classify_signal(reason: str) -> str:
    """Classify entry reason into signal category."""
    reason = reason.lower()
    if "momentum" in reason or "trend" in reason:
        return "momentum"
    elif "rsi" in reason or "oversold" in reason or "overbought" in reason:
        return "mean_reversion"
    elif "bollinger" in reason or "bb" in reason:
        return "bollinger"
    elif "volume" in reason:
        return "volume"
    elif "regime" in reason:
        return "regime"
    elif "breakout" in reason:
        return "breakout"
    return "other"


def generate_suggestions(
    agent_id: str,
    stats: WeeklyStats,
    current_params: Dict[str, float],
    ph: ParamHistory,
) -> List[ParamSuggestion]:
    """Generate parameter change suggestions based on weekly performance."""
    suggestions: List[ParamSuggestion] = []

    if stats.total_trades < MIN_TRADES_FOR_ANALYSIS:
        log.info(
            "%s: only %d trades this week, skipping (need %d)",
            agent_id, stats.total_trades, MIN_TRADES_FOR_ANALYSIS,
        )
        return suggestions

    # ── Suggestion 1: Adjust position sizing based on win rate ──
    if current_params.get("base_size_pct"):
        cur = current_params["base_size_pct"]
        if stats.win_rate > 0.60 and stats.total_trades >= 5:
            # High conviction — increase size slightly
            suggested = min(cur * 1.05, PARAM_BOUNDS["base_size_pct"][1])
            if suggested != cur:
                suggestions.append(ParamSuggestion(
                    agent_id=agent_id,
                    param_name="base_size_pct",
                    current_value=cur,
                    suggested_value=round(suggested, 4),
                    delta=round(suggested - cur, 4),
                    delta_pct=round((suggested - cur) / cur * 100, 1),
                    reason=f"Win rate {stats.win_rate:.1%} > 60% — increase position size",
                    confidence=0.7,
                    evidence=f"{stats.wins}W/{stats.losses}L, profit factor {stats.profit_factor:.2f}",
                ))
        elif stats.win_rate < 0.35:
            suggested = max(cur * 0.90, PARAM_BOUNDS["base_size_pct"][0])
            if suggested != cur:
                suggestions.append(ParamSuggestion(
                    agent_id=agent_id,
                    param_name="base_size_pct",
                    current_value=cur,
                    suggested_value=round(suggested, 4),
                    delta=round(suggested - cur, 4),
                    delta_pct=round((suggested - cur) / cur * 100, 1),
                    reason=f"Win rate {stats.win_rate:.1%} < 35% — reduce position size",
                    confidence=0.8,
                    evidence=f"{stats.wins}W/{stats.losses}L, avg loss {stats.avg_loss_pct:.2%}",
                ))

    # ── Suggestion 2: Adjust stop loss if avg loss is too large ──
    if (current_params.get("stop_loss_pct") and
            stats.avg_loss_pct > 0 and stats.losses >= 3):
        cur = current_params["stop_loss_pct"]
        if stats.avg_loss_pct > cur * 1.5:
            # Losses are bigger than stop suggests — tighten stop
            suggested = max(cur * 0.85, PARAM_BOUNDS["stop_loss_pct"][0])
            if abs(suggested - cur) / cur > 0.02:
                suggestions.append(ParamSuggestion(
                    agent_id=agent_id,
                    param_name="stop_loss_pct",
                    current_value=cur,
                    suggested_value=round(suggested, 4),
                    delta=round(suggested - cur, 4),
                    delta_pct=round((suggested - cur) / cur * 100, 1),
                    reason=f"Avg loss {stats.avg_loss_pct:.2%} > stop {cur:.2%} — tighten stop",
                    confidence=0.75,
                    evidence=f"{stats.losses} losses averaging {stats.avg_loss_pct:.2%}",
                ))
        elif stats.avg_loss_pct < cur * 0.5 and stats.win_rate > 0.40:
            # Losses are much smaller than stop — might be too tight
            suggested = min(cur * 1.10, PARAM_BOUNDS["stop_loss_pct"][1])
            if abs(suggested - cur) / cur > 0.02:
                suggestions.append(ParamSuggestion(
                    agent_id=agent_id,
                    param_name="stop_loss_pct",
                    current_value=cur,
                    suggested_value=round(suggested, 4),
                    delta=round(suggested - cur, 4),
                    delta_pct=round((suggested - cur) / cur * 100, 1),
                    reason=f"Avg loss {stats.avg_loss_pct:.2%} is tight — loosen stop",
                    confidence=0.55,
                    evidence=f"Losses average {stats.avg_loss_pct:.2%} vs stop {cur:.2%}",
                ))

    # ── Suggestion 3: Adjust signal weights based on P&L by signal ──
    signals = stats.pnl_by_signal
    if len(signals) >= 2 and current_params:
        signal_weight_map = {
            "momentum": "weight_trending_up",
            "mean_reversion": "weight_mean_reverting",
            "bollinger": "weight_mean_reverting",
            "volume": "weight_high_volatility",
        }

        for sig, weight_key in signal_weight_map.items():
            if sig in signals and weight_key in current_params:
                sig_pnl = signals[sig][1]
                cur_w = current_params[weight_key]

                if sig_pnl > 100 and cur_w < 0.8:
                    suggested = min(cur_w * 1.10, PARAM_BOUNDS[weight_key][1])
                    if abs(suggested - cur_w) / max(cur_w, 0.01) > 0.05:
                        suggestions.append(ParamSuggestion(
                            agent_id=agent_id,
                            param_name=weight_key,
                            current_value=cur_w,
                            suggested_value=round(suggested, 4),
                            delta=round(suggested - cur_w, 4),
                            delta_pct=round((suggested - cur_w) / max(cur_w, 0.01) * 100, 1),
                            reason=f"{sig} signal profitable (${sig_pnl:.0f} P&L) — increase weight",
                            confidence=0.65,
                            evidence=f"{signals[sig][0]} {sig} trades: ${sig_pnl:.0f}",
                        ))
                elif sig_pnl < -50 and cur_w > 0.2:
                    suggested = max(cur_w * 0.85, PARAM_BOUNDS[weight_key][0])
                    if abs(suggested - cur_w) / max(cur_w, 0.01) > 0.05:
                        suggestions.append(ParamSuggestion(
                            agent_id=agent_id,
                            param_name=weight_key,
                            current_value=cur_w,
                            suggested_value=round(suggested, 4),
                            delta=round(suggested - cur_w, 4),
                            delta_pct=round((suggested - cur_w) / max(cur_w, 0.01) * 100, 1),
                            reason=f"{sig} signal unprofitable (${sig_pnl:.0f}) — reduce weight",
                            confidence=0.6,
                            evidence=f"{signals[sig][0]} {sig} trades: ${sig_pnl:.0f}",
                        ))

    # ── Suggestion 4: Param convergence check ──
    for param_name in ["momentum_threshold", "rsi_oversold", "rsi_overbought"]:
        if param_name in current_params:
            try:
                conv = ph.convergence_score(param_name, window=10)
                if conv and conv.get("converging") and conv.get("stable_value"):
                    current = current_params[param_name]
                    stable = conv["stable_value"]
                    delta_pct = abs(current - stable) / max(abs(stable), 0.01)
                    if delta_pct > 0.05:
                        # Parameter has drifted from stable value
                        suggestions.append(ParamSuggestion(
                            agent_id=agent_id,
                            param_name=param_name,
                            current_value=round(current, 4),
                            suggested_value=round(stable, 4),
                            delta=round(stable - current, 4),
                            delta_pct=round(delta_pct * 100, 1),
                            reason=f"Parameter drifted from stable value {stable:.4f}",
                            confidence=0.5,
                            evidence=f"Stable for {conv.get('variance_last_half', 0):.6f} window",
                        ))
            except Exception:
                pass  # No history yet

    # ── Trim to max suggestions ──
    suggestions.sort(key=lambda s: s.confidence, reverse=True)
    return suggestions[:MAX_SUGGESTIONS_PER_TRADER]


# ═══════════════════════════════════════════════════════════════════════════════
# Output
# ═══════════════════════════════════════════════════════════════════════════════


def format_report(
    agent_id: str,
    stats: WeeklyStats,
    suggestions: List[ParamSuggestion],
    state: Optional[dict],
) -> str:
    """Generate a markdown report for the weekly strategy evolution."""
    lines = [
        f"# Strategy Evolution — {agent_id}",
        f"**Week of {date.today().strftime('%Y-%m-%d')}**",
        "",
    ]

    # State summary
    if state:
        lines.append("## Portfolio State")
        lines.append(f"- Equity: ${float(state['equity']):,.2f}")
        lines.append(f"- P&L: ${float(state['pnl']):,.2f} ({float(state['pnl_pct']):.2%})")
        lines.append(f"- Positions: {state['positions_count']}")
        lines.append(f"- Active: {'✅' if state['is_active'] else '❌'}")
        lines.append("")

    # Weekly stats
    lines.append("## Weekly Performance")
    lines.append(f"- Trades: {stats.total_trades} ({stats.wins}W / {stats.losses}L)")
    lines.append(f"- Win Rate: {stats.win_rate:.1%}")
    lines.append(f"- Total P&L: ${stats.total_pnl:,.2f}")
    lines.append(f"- Profit Factor: {stats.profit_factor:.2f}")
    lines.append(f"- Avg Win: {stats.avg_win_pct:.2%} | Avg Loss: {stats.avg_loss_pct:.2%}")
    lines.append(f"- Avg Hold: {stats.avg_hold_hours:.1f}h")
    if stats.best_ticker:
        lines.append(f"- Best Ticker: {stats.best_ticker} (${stats.ticker_performance.get(stats.best_ticker, (0, 0))[1]:,.2f})")
    if stats.worst_ticker:
        lines.append(f"- Worst Ticker: {stats.worst_ticker} (${stats.ticker_performance.get(stats.worst_ticker, (0, 0))[1]:,.2f})")
    lines.append("")

    # Signal breakdown
    if stats.pnl_by_signal:
        lines.append("## P&L by Signal")
        lines.append("| Signal | Trades | P&L |")
        lines.append("|--------|--------|-----|")
        for sig, (count, pnl) in sorted(stats.pnl_by_signal.items(),
                                          key=lambda x: x[1][1], reverse=True):
            lines.append(f"| {sig} | {count} | ${pnl:,.2f} |")
        lines.append("")

    # Suggestions
    lines.append("## Parameter Suggestions")
    if not suggestions:
        lines.append("_No parameter changes recommended this week._")
    else:
        lines.append("| Parameter | Current | Suggested | Δ% | Confidence | Reason |")
        lines.append("|-----------|---------|-----------|----|------------|--------|")
        for s in suggestions:
            direction = "↑" if s.delta > 0 else "↓"
            lines.append(
                f"| {s.param_name} | {s.current_value:.4f} | "
                f"{s.suggested_value:.4f} {direction} | "
                f"{s.delta_pct:+.1f}% | {s.confidence:.0%} | {s.reason} |"
            )
        lines.append("")

        lines.append("### Details")
        for i, s in enumerate(suggestions, 1):
            lines.append(f"{i}. **{s.param_name}**: {s.reason}")
            lines.append(f"   - Evidence: {s.evidence}")
            lines.append(f"   - Change: {s.current_value:.4f} → {s.suggested_value:.4f} ({s.delta_pct:+.1f}%)")
            lines.append("")

    lines.append("---")
    lines.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_")

    return "\n".join(lines)


def write_report(agent_id: str, report: str, output_dir: str = ""):
    """Write report to state directory."""
    if not output_dir:
        output_dir = os.path.join(os.path.dirname(__file__), "..", "state")
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"strategy_evolution_{agent_id}.md")
    with open(path, "w") as f:
        f.write(report)
    log.info("Report saved: %s", path)
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════


def evolve_agent(
    agent_id: str,
    conn,
    ph: ParamHistory,
    apply: bool = False,
    dry_run: bool = False,
) -> Optional[str]:
    """Run strategy evolution for a single agent."""
    log.info("Analyzing %s...", agent_id)

    trades = get_weekly_trades(conn, agent_id)
    stats = compute_weekly_stats(trades, agent_id)
    current_params = get_current_params(conn, agent_id)
    state = get_agent_state(conn, agent_id)

    if not current_params:
        log.warning("%s: no current params found in agent_profile", agent_id)

    suggestions = generate_suggestions(agent_id, stats, current_params, ph)
    report = format_report(agent_id, stats, suggestions, state)

    if dry_run:
        print(report)
        return None

    path = write_report(agent_id, report)

    if apply and suggestions:
        log.info("Applying %d suggestions for %s...", len(suggestions), agent_id)
        for s in suggestions:
            try:
                ph.record_change(
                    param_name=s.param_name,
                    old=s.current_value,
                    new=s.suggested_value,
                    before_score=0.0,
                    after_score=0.0,
                    source="strategy_evolution",
                )
                log.info(
                    "  %s: %.4f → %.4f (%+.1f%%) [%s]",
                    s.param_name, s.current_value, s.suggested_value,
                    s.delta_pct, s.reason[:60],
                )
            except Exception as e:
                log.error("  Failed to record %s: %s", s.param_name, e)

    return path


def main():
    parser = argparse.ArgumentParser(
        description="Weekly strategy evolution — parameter optimization"
    )
    parser.add_argument(
        "--agent", type=str,
        help="Single agent ID (e.g. trader-kairos)"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Run for all base traders"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply suggested parameter changes"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print report without saving"
    )
    parser.add_argument(
        "--lookback", type=int, default=LOOKBACK_DAYS,
        help=f"Lookback days (default: {LOOKBACK_DAYS})"
    )
    args = parser.parse_args()

    if not args.agent and not args.all:
        parser.error("Must specify --agent or --all")

    agents = [args.agent] if args.agent else BASE_TRADERS

    conn = get_connection()
    ph = ParamHistory()

    try:
        for agent_id in agents:
            evolve_agent(
                agent_id=agent_id,
                conn=conn,
                ph=ph,
                apply=args.apply,
                dry_run=args.dry_run,
            )
    finally:
        conn.close()

    log.info("Strategy evolution complete.")


if __name__ == "__main__":
    main()
