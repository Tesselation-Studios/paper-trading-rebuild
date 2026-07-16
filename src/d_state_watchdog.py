#!/usr/bin/env python3
"""
D-State Watchdog — detect when traders are stuck in a dead state.

A trader is in D-state when it hasn't produced a decision or heartbeat for
longer than the configured threshold. This watchdog:

1. Queries Postgres for last trader activity (agent_state + decisions)
2. Compares against per-trader silence thresholds
3. Alerts if a trader is silent for > stale_threshold × missed_heartbeat_limit
4. Exits with code 1 if any trader is in D-state (for cron alerting)

Usage:
    python3 src/d_state_watchdog.py              # Check all traders
    python3 src/d_state_watchdog.py --json       # JSON output
    python3 src/d_state_watchdog.py --trader kairos  # Check single trader

Config (from config/traders.yaml):
    monitoring:
      stale_threshold: 900       # seconds before trader considered stale
      missed_heartbeat_limit: 3  # consecutive missed before alert
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("d_state_watchdog")

# Default DB URL (can be overridden via env)
DB_URL = os.environ.get(
    "PAPER_TRADING_DB_URL",
    "postgresql://trader:***@192.168.1.179:5433/trading",
)

# Trader tick intervals in minutes (from SPEC §4.1)
TRADER_TICK_INTERVALS: Dict[str, int] = {
    "kairos": 5,
    "aldridge": 30,
    "stonks": 15,
    "trader-kairos": 5,
    "trader-aldridge": 30,
    "trader-stonks": 15,
}

# Normalize trader IDs to short form
TRADER_ID_MAP: Dict[str, str] = {
    "trader-kairos": "kairos",
    "trader-aldridge": "aldridge",
    "trader-stonks": "stonks",
    "kairos": "kairos",
    "aldridge": "aldridge",
    "stonks": "stonks",
    "trader-momentum": "momentum",
    "trader-value": "value",
}

# Which traders to monitor (from SPEC: the three core traders)
DEFAULT_TRADERS = ["kairos", "aldridge", "stonks"]


@dataclass
class TraderStatus:
    """Health status for a single trader."""
    trader_id: str
    is_active: bool = False
    last_heartbeat: Optional[datetime] = None
    last_trade: Optional[datetime] = None
    last_decision: Optional[datetime] = None
    last_decision_type: Optional[str] = None
    positions_count: int = 0
    equity: float = 0.0
    pnl: float = 0.0
    silence_seconds: float = 0.0
    tick_interval_minutes: int = 15
    ticks_silent: int = 0
    is_d_state: bool = False
    severity: str = "ok"  # ok, warning, critical
    error: Optional[str] = None

    @property
    def last_activity(self) -> Optional[datetime]:
        """Most recent activity timestamp (any source)."""
        candidates = [
            t for t in [self.last_decision, self.last_trade, self.last_heartbeat]
            if t is not None
        ]
        return max(candidates) if candidates else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trader_id": self.trader_id,
            "is_active": self.is_active,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "last_trade": self.last_trade.isoformat() if self.last_trade else None,
            "last_decision": self.last_decision.isoformat() if self.last_decision else None,
            "last_decision_type": self.last_decision_type,
            "positions_count": self.positions_count,
            "equity": round(self.equity, 2),
            "pnl": round(self.pnl, 2),
            "silence_seconds": round(self.silence_seconds, 1),
            "tick_interval_minutes": self.tick_interval_minutes,
            "ticks_silent": self.ticks_silent,
            "is_d_state": self.is_d_state,
            "severity": self.severity,
            "error": self.error,
        }


@dataclass
class WatchdogReport:
    """Full watchdog report."""
    timestamp: datetime
    checked_traders: int = 0
    d_state_traders: int = 0
    warning_traders: int = 0
    ok_traders: int = 0
    traders: List[TraderStatus] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def has_alerts(self) -> bool:
        return self.d_state_traders > 0 or self.warning_traders > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "summary": {
                "checked": self.checked_traders,
                "ok": self.ok_traders,
                "warning": self.warning_traders,
                "d_state": self.d_state_traders,
            },
            "traders": [t.to_dict() for t in self.traders],
            "errors": self.errors,
        }


def _resolve_trader_id(raw_id: str) -> str:
    """Normalize trader ID (e.g., 'trader-kairos' → 'kairos')."""
    return TRADER_ID_MAP.get(raw_id.lower(), raw_id.lower())


def _get_tick_interval(trader_id: str) -> int:
    """Get tick interval in minutes for a trader."""
    # Try both forms
    interval = TRADER_TICK_INTERVALS.get(trader_id)
    if interval is not None:
        return interval
    # Try with 'trader-' prefix
    interval = TRADER_TICK_INTERVALS.get(f"trader-{trader_id}")
    if interval is not None:
        return interval
    return 15  # default


def _load_monitoring_config() -> Tuple[int, int]:
    """Load monitoring thresholds from config/traders.yaml.

    Returns:
        (stale_threshold_seconds, missed_heartbeat_limit)
    """
    try:
        from src.config_loader import Config
        config = Config()
        config.load_all()

        stale = config.get("traders.monitoring.stale_threshold", 900)
        limit = config.get("traders.monitoring.missed_heartbeat_limit", 3)

        return int(stale), int(limit)
    except Exception as e:
        log.warning("Failed to load monitoring config: %s. Using defaults.", e)
        return 900, 3


def _query_agent_state(db_url: str, trader_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Query trading.agent_state for last heartbeat/trade per trader.

    Returns dict keyed by agent_id (e.g., 'trader-kairos').
    """
    try:
        import psycopg2
    except ImportError:
        log.error("psycopg2 not installed. Install with: pip install psycopg2-binary")
        return {}

    state_map: Dict[str, Dict[str, Any]] = {}
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        cur = conn.cursor()

        # Build query for all known agent_id variants
        placeholders = ",".join(["%s"] * len(trader_ids))
        cur.execute(
            f"""SELECT agent_id, is_active, last_heartbeat, last_trade,
                       cash, equity, pnl, positions_count
                FROM trading.agent_state
                WHERE agent_id IN ({placeholders})""",
            trader_ids,
        )
        for row in cur.fetchall():
            state_map[row[0]] = {
                "agent_id": row[0],
                "is_active": row[1],
                "last_heartbeat": row[2],
                "last_trade": row[3],
                "cash": float(row[4]) if row[4] else 0.0,
                "equity": float(row[5]) if row[5] else 0.0,
                "pnl": float(row[6]) if row[6] else 0.0,
                "positions_count": int(row[7]) if row[7] else 0,
            }
        cur.close()
        conn.close()
    except Exception as e:
        log.warning("Failed to query agent_state: %s", e)

    return state_map


def _query_decisions(db_url: str, trader_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Query trading.decisions for last decision per trader.

    Returns dict keyed by trader_id (short form like 'kairos').
    """
    try:
        import psycopg2
    except ImportError:
        return {}

    decision_map: Dict[str, Dict[str, Any]] = {}
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        cur = conn.cursor()

        # Query for both short-form and trader- prefixed IDs
        all_ids = list(trader_ids) + [f"trader-{t}" for t in trader_ids]
        placeholders = ",".join(["%s"] * len(all_ids))
        cur.execute(
            f"""SELECT trader_id, MAX(timestamp) as last_ts,
                       (SELECT decision FROM trading.decisions d2
                        WHERE d2.trader_id = trading.decisions.trader_id
                        ORDER BY timestamp DESC LIMIT 1) as last_decision
                FROM trading.decisions
                WHERE trader_id IN ({placeholders})
                GROUP BY trader_id""",
            all_ids,
        )
        for row in cur.fetchall():
            decision_map[row[0]] = {
                "last_decision": row[1],
                "last_decision_type": row[2],
            }
        cur.close()
        conn.close()
    except Exception as e:
        log.warning("Failed to query decisions: %s", e)

    return decision_map


def check_trader(
    trader_id: str,
    db_url: str = DB_URL,
    stale_threshold: int = 900,
    missed_limit: int = 3,
) -> TraderStatus:
    """Check a single trader's health status.

    Args:
        trader_id: Short trader ID (e.g., 'kairos').
        db_url: Postgres connection URL.
        stale_threshold: Seconds before a trader is considered stale.
        missed_limit: Number of consecutive missed intervals before alert.

    Returns:
        TraderStatus with full health assessment.
    """
    status = TraderStatus(trader_id=trader_id)
    status.tick_interval_minutes = _get_tick_interval(trader_id)

    # Try both ID forms for querying
    agent_id = f"trader-{trader_id}"

    # Query agent_state
    state_map = _query_agent_state(db_url, [agent_id, trader_id])
    state = state_map.get(agent_id) or state_map.get(trader_id)

    if state:
        status.is_active = bool(state.get("is_active", False))
        status.last_heartbeat = state.get("last_heartbeat")
        status.last_trade = state.get("last_trade")
        status.equity = state.get("equity", 0.0)
        status.pnl = state.get("pnl", 0.0)
        status.positions_count = state.get("positions_count", 0)

    # Query decisions (fallback if agent_state has no data)
    decision_map = _query_decisions(db_url, [trader_id])
    decision = decision_map.get(trader_id) or decision_map.get(agent_id)

    if decision:
        status.last_decision = decision.get("last_decision")
        status.last_decision_type = decision.get("last_decision_type")

    # Compute silence duration from most recent activity
    last_activity = status.last_activity
    now = datetime.now(timezone.utc)

    if last_activity is not None:
        # Ensure timezone-aware
        if last_activity.tzinfo is None:
            from datetime import timezone as tz
            last_activity = last_activity.replace(tzinfo=tz.utc)
        delta = now - last_activity
        status.silence_seconds = delta.total_seconds()
    else:
        # No activity ever recorded — mark as D-state
        status.silence_seconds = float("inf")
        status.ticks_silent = 999
        status.is_d_state = True
        status.severity = "critical"
        status.error = f"No activity ever recorded for {trader_id}"
        return status

    # Calculate ticks of silence
    tick_seconds = status.tick_interval_minutes * 60
    status.ticks_silent = int(status.silence_seconds / tick_seconds) if tick_seconds > 0 else 0

    # Determine D-state
    # A trader is in D-state if silence exceeds the alert threshold
    alert_threshold = stale_threshold * missed_limit

    if status.silence_seconds > alert_threshold:
        status.is_d_state = True
        status.severity = "critical"
    elif status.silence_seconds > stale_threshold:
        status.severity = "warning"
    else:
        status.severity = "ok"

    return status


def check_all_traders(
    trader_ids: Optional[List[str]] = None,
    db_url: str = DB_URL,
) -> WatchdogReport:
    """Check all specified traders and produce a full report.

    Args:
        trader_ids: List of trader IDs to check. Default: DEFAULT_TRADERS.
        db_url: Postgres connection URL.

    Returns:
        WatchdogReport with status for all traders.
    """
    if trader_ids is None:
        trader_ids = list(DEFAULT_TRADERS)

    stale_threshold, missed_limit = _load_monitoring_config()

    report = WatchdogReport(timestamp=datetime.now(timezone.utc))
    report.checked_traders = len(trader_ids)

    for tid in trader_ids:
        try:
            status = check_trader(
                tid,
                db_url=db_url,
                stale_threshold=stale_threshold,
                missed_limit=missed_limit,
            )
            report.traders.append(status)

            if status.is_d_state:
                report.d_state_traders += 1
            elif status.severity == "warning":
                report.warning_traders += 1
            else:
                report.ok_traders += 1
        except Exception as e:
            log.error("Error checking trader %s: %s", tid, e)
            report.errors.append(f"{tid}: {e}")

    return report


def _restart_stalled_traders(
    report: WatchdogReport,
    gateway_host: str = "192.168.1.41",
    gateway_user: str = "raf",
) -> Dict[str, str]:
    """Attempt to restart stalled/crashed traders by restarting the OpenClaw gateway.

    SSHs to the gateway host and runs 'openclaw gateway restart'.
    This restarts all cron jobs including the stalled trader.

    Returns dict of trader_id -> result ("restarted", "failed: <reason>", or "skipped").
    """
    results: Dict[str, str] = {}
    d_state_ids = [t.trader_id for t in report.traders if t.is_d_state]
    warning_ids = [t.trader_id for t in report.traders if t.severity == "warning"]

    restart_ids = d_state_ids + warning_ids
    if not restart_ids:
        return results

    log.info(
        "Auto-restart triggered for: %s (d-state: %s, warning: %s)",
        restart_ids, d_state_ids, warning_ids,
    )

    # Single gateway restart covers all traders
    cmd = [
        "ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
        f"{gateway_user}@{gateway_host}",
        "openclaw gateway restart",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            for tid in restart_ids:
                results[tid] = "restarted"
            log.info("Gateway restart succeeded for %d traders", len(restart_ids))
        else:
            err = result.stderr.strip() or "exit code {}".format(result.returncode)
            for tid in restart_ids:
                results[tid] = f"failed: {err[:120]}"
            log.error("Gateway restart failed: %s", err)
    except subprocess.TimeoutExpired:
        for tid in restart_ids:
            results[tid] = "failed: SSH timeout"
        log.error("Gateway restart timed out")
    except FileNotFoundError:
        for tid in restart_ids:
            results[tid] = "failed: ssh not found"
        log.error("ssh command not found")
    except Exception as e:
        for tid in restart_ids:
            results[tid] = f"failed: {e}"
        log.error("Gateway restart error: %s", e)

    return results


def _format_report(report: WatchdogReport) -> str:
    """Format a watchdog report for human consumption."""
    lines = []
    lines.append("=" * 60)
    lines.append(f"  D-State Watchdog Report — {report.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("=" * 60)

    # Summary
    lines.append(f"\n  Summary: {report.ok_traders} OK, {report.warning_traders} warning, "
                 f"{report.d_state_traders} D-STATE")

    if report.errors:
        lines.append(f"  Errors: {len(report.errors)}")

    # Per-trader details
    for t in report.traders:
        icon = {"ok": "✅", "warning": "⚠️", "critical": "🔴"}.get(t.severity, "❓")
        lines.append(f"\n  {icon} {t.trader_id.upper()} [{t.severity.upper()}]")
        lines.append(f"     Active: {t.is_active}")
        lines.append(f"     Last heartbeat: {t.last_heartbeat or 'never'}")
        lines.append(f"     Last trade:     {t.last_trade or 'never'}")
        lines.append(f"     Last decision:  {t.last_decision or 'never'}")
        if t.last_decision_type:
            lines.append(f"     Decision type:  {t.last_decision_type}")
        lines.append(f"     Silence:        {t.silence_seconds:.0f}s "
                     f"({t.ticks_silent} ticks @ {t.tick_interval_minutes}m/tick)")
        lines.append(f"     Positions:      {t.positions_count}")
        lines.append(f"     Equity:         ${t.equity:,.2f}")
        lines.append(f"     P&L:            ${t.pnl:,.2f}")
        if t.error:
            lines.append(f"     Error:          {t.error}")

    if report.errors:
        lines.append("\n  Errors:")
        for e in report.errors:
            lines.append(f"    - {e}")

    lines.append("\n" + "=" * 60)
    alert_count = report.d_state_traders
    if alert_count > 0:
        lines.append(f"  🚨 ALERT: {alert_count} trader(s) in D-STATE — "
                     f"no decisions for > threshold")
    elif report.warning_traders > 0:
        lines.append(f"  ⚠️  WARNING: {report.warning_traders} trader(s) approaching silence threshold")
    else:
        lines.append("  ✅ All traders healthy")

    lines.append("=" * 60)
    return "\n".join(lines)


def main() -> int:
    """Run the watchdog from the command line.

    Returns:
        0 if all healthy, 1 if any trader in D-state, 2 on error.
    """
    parser = argparse.ArgumentParser(
        description="D-State Watchdog — detect silent/stuck traders"
    )
    parser.add_argument(
        "--trader", "-t",
        help="Check a single trader (e.g., kairos, aldridge, stonks)",
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        default=True,
        help="Check all traders (default)",
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output JSON instead of human-readable format",
    )
    parser.add_argument(
        "--db-url",
        default=DB_URL,
        help="Postgres connection URL",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress output unless there's an alert",
    )
    parser.add_argument(
        "--restart", "-r",
        action="store_true",
        help="Auto-restart stalled/crashed traders via gateway restart",
    )
    parser.add_argument(
        "--gateway-host",
        default="192.168.1.41",
        help="OpenClaw gateway host for restart (default: 192.168.1.41)",
    )
    parser.add_argument(
        "--gateway-user",
        default="raf",
        help="SSH user for gateway host (default: raf)",
    )
    args = parser.parse_args()

    # Determine which traders to check
    if args.trader:
        trader_ids = [_resolve_trader_id(args.trader)]
    else:
        trader_ids = list(DEFAULT_TRADERS)

    try:
        report = check_all_traders(trader_ids=trader_ids, db_url=args.db_url)
    except Exception as e:
        log.error("Watchdog failed: %s", e)
        if args.json:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"ERROR: Watchdog failed: {e}")
        return 2

    # Auto-restart if requested
    restart_results = {}
    if args.restart and report.has_alerts:
        restart_results = _restart_stalled_traders(
            report,
            gateway_host=args.gateway_host,
            gateway_user=args.gateway_user,
        )

    # Output
    if args.json:
        output = report.to_dict()
        if restart_results:
            output["restart_results"] = restart_results
        print(json.dumps(output, indent=2, default=str))
    elif not args.quiet or report.has_alerts:
        print(_format_report(report))
        if restart_results:
            print("\n  🔄 Restart results:")
            for tid, result in restart_results.items():
                icon = "✅" if result == "restarted" else "❌"
                print(f"     {icon} {tid}: {result}")

    # Exit code: 0 = all ok, 1 = D-state alert, 2 = error
    if report.errors and not report.traders:
        return 2
    if report.d_state_traders > 0:
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    sys.exit(main())
