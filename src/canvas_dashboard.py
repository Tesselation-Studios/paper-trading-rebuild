"""
Canvas Dashboard — push observability metrics to Canvas as visual cards.

Provides:
  - CanvasDashboard: collects metrics, alerts, circuit breaker status
  - push_health_dashboard(): one-shot push of a system health card
  - push_metrics_snapshot(): push the metrics snapshot as a markdown table
  - start_periodic_push(): background thread that pushes every N seconds

Usage:
    from src.canvas_dashboard import push_health_dashboard

    push_health_dashboard(board="trading")

    # Or for periodic pushes:
    from src.canvas_dashboard import start_periodic_push
    start_periodic_push(interval=300, board="trading")

Ref: SPEC-v3 observability requirements, issue#75
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Allow both `python -m src.canvas_dashboard` and `python3 src/canvas_dashboard.py`
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.observability import metrics, alert


def _load_canvas_credentials() -> tuple[str, str]:
    """Load Canvas URL and token from ~/canvas/.env."""
    canvas_env = os.path.expanduser("~/canvas/.env")
    if not os.path.exists(canvas_env):
        raise FileNotFoundError(f"Canvas env file not found: {canvas_env}")

    env_vars: Dict[str, str] = {}
    with open(canvas_env) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                env_vars[key.strip()] = val.strip().strip('"').strip("'")

    canvas_url = env_vars.get("CANVAS_URL", "")
    canvas_token = env_vars.get("CANVAS_TOKEN", "")

    if not canvas_url or not canvas_token:
        raise RuntimeError("CANVAS_URL or CANVAS_TOKEN not found in ~/canvas/.env")

    return canvas_url, canvas_token


def _push_to_canvas(
    title: str,
    content: str,
    board: str = "main",
    agent: str = "hermes",
    emoji: str = "🪽",
    card_id: Optional[str] = None,
    expires_days: int = 7,
) -> dict:
    """Push a markdown card to Canvas.

    Returns the response dict (includes 'id' for card permalink).
    """
    canvas_url, canvas_token = _load_canvas_credentials()

    from datetime import timedelta

    expires = (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat()

    payload: Dict[str, Any] = {
        "type": "markdown",
        "title": title,
        "content": content,
        "board": board,
        "agent": agent,
        "agent_emoji": emoji,
        "expires_at": expires,
    }
    if card_id:
        payload["card_id"] = card_id

    data = json.dumps(payload).encode()

    req = urllib.request.Request(
        f"{canvas_url}/push",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {canvas_token}",
        },
    )
    result = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return result


def _read_circuit_breaker_state() -> dict:
    """Read circuit breaker state for all tracked traders.

    Returns a dict with breaker status per trader.
    """
    try:
        from src.circuit_breaker import AgentCircuitBreaker

        all_status = AgentCircuitBreaker.get_all_status()

        trader_states = {}
        for trader_id, status in all_status.items():
            trader_states[trader_id] = {
                "is_paused": status.get("is_paused", False),
                "paused_reason": status.get("paused_reason", ""),
                "total_trips": status.get("total_trips", 0),
                "last_trip_at": status.get("last_trip_at"),
                "current_tick": status.get("current_tick"),
            }

        return {
            "status": "active" if all_status else "no_traders",
            "traders": trader_states,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _read_junit_xml(path: str) -> dict:
    """Parse a JUnit XML report and return test stats.

    Returns a dict with totals: tests, passed, failed, errors, skipped, time,
    plus per-suite breakdown.
    Returns error dict if the file can't be read/parsed.
    """
    try:
        tree = ET.parse(path)
        root = tree.getroot()

        totals = {
            "tests": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "time": 0.0,
        }
        suites = []

        for suite in root.iter("testsuite"):
            name = suite.get("name", "unknown")
            tests = int(suite.get("tests", 0))
            failures = int(suite.get("failures", 0))
            errors = int(suite.get("errors", 0))
            skipped = int(suite.get("skipped", 0))
            time_s = float(suite.get("time", 0))

            suite_data = {
                "name": name,
                "tests": tests,
                "passed": tests - failures - errors - skipped,
                "failed": failures,
                "errors": errors,
                "skipped": skipped,
                "time": time_s,
            }
            suites.append(suite_data)

            totals["tests"] += tests
            totals["failed"] += failures
            totals["errors"] += errors
            totals["skipped"] += skipped
            totals["time"] += time_s

        totals["passed"] = totals["tests"] - totals["failed"] - totals["errors"] - totals["skipped"]

        return {
            "status": "ok",
            "totals": totals,
            "suites": suites,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _build_health_markdown(
    metrics_snapshot: dict,
    alert_summary: dict,
    breaker_state: dict,
    test_results: Optional[dict] = None,
) -> str:
    """Build a markdown dashboard card from observability data."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"## 🩺 System Health — {ts}",
        "",
    ]

    # ── Circuit Breakers ──
    lines.append("### ⚡ Circuit Breakers")
    lines.append("")
    if breaker_state.get("status") == "error":
        lines.append(f"> ❌ Error reading breaker state: {breaker_state.get('error')}")
    elif breaker_state.get("status") == "no_traders":
        lines.append("> ⚠️ No traders registered yet")
    else:
        traders = breaker_state.get("traders", {})
        total_trips = sum(t.get("total_trips", 0) for t in traders.values())
        lines.append(f"**Total trips:** {total_trips}")
        lines.append("")
        for trader_id, state in traders.items():
            icon = "🚫" if state.get("is_paused") else "✅"
            tick = state.get("current_tick") or {}
            details = f"trips={state.get('total_trips', 0)}"
            if state.get("is_paused"):
                details += f", reason: {state.get('paused_reason', 'unknown')}"
            if tick.get("active"):
                details += f", calls={tick.get('call_count', 0)}, elapsed={tick.get('elapsed_s', 0)}s"
            lines.append(f"- {icon} **{trader_id}**: {details}")
    lines.append("")

    # ── Alerts ──
    lines.append("### 🚨 Alerts")
    lines.append("")
    lines.append(f"| Severity | Count |")
    lines.append(f"|----------|-------|")
    lines.append(f"| P0 (Critical) | {alert_summary.get('p0_count', 0)} |")
    lines.append(f"| P1 (High) | {alert_summary.get('p1_count', 0)} |")
    lines.append("")

    # ── Metrics ──
    lines.append("### 📊 Key Metrics")
    lines.append("")
    counters = metrics_snapshot.get("counters", {})
    gauges = metrics_snapshot.get("gauges", {})

    # Show top counters
    if counters:
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        # Sort by value descending, show top 10
        top_counters = sorted(counters.items(), key=lambda x: x[1], reverse=True)[:10]
        for name, val in top_counters:
            lines.append(f"| {name} | {val:.0f} |")
        lines.append("")

    # Show gauges
    if gauges:
        lines.append("| Gauge | Value |")
        lines.append("|-------|-------|")
        for name, val in sorted(gauges.items()):
            lines.append(f"| {name} | {val:.2f} |")
        lines.append("")

    # ── Histograms ──
    histograms = metrics_snapshot.get("histograms", {})
    if histograms:
        lines.append("### 📈 Histograms")
        lines.append("")
        lines.append("| Metric | Count | Min | Avg | Max |")
        lines.append("|--------|-------|-----|-----|-----|")
        for name, stats in sorted(histograms.items()):
            lines.append(
                f"| {name} | {stats['count']} | "
                f"{stats['min']:.2f} | {stats['avg']:.2f} | {stats['max']:.2f} |"
            )
        lines.append("")

    # ── Test Results ──
    if test_results and test_results.get("status") == "ok":
        totals = test_results["totals"]
        pass_pct = (totals["passed"] / totals["tests"] * 100) if totals["tests"] > 0 else 0
        pass_icon = "✅" if pass_pct >= 95 else ("⚠️" if pass_pct >= 80 else "❌")

        lines.append("### 🧪 Test Results")
        lines.append("")
        lines.append(f"{pass_icon} **{totals['passed']}/{totals['tests']} passed ({pass_pct:.1f}%)**")
        if totals["failed"]:
            lines.append(f"- Failed: {totals['failed']}")
        if totals["errors"]:
            lines.append(f"- Errors: {totals['errors']}")
        if totals["skipped"]:
            lines.append(f"- Skipped: {totals['skipped']}")
        lines.append(f"- Duration: {totals['time']:.1f}s")

        # Per-suite breakdown if 2+ suites
        suites = test_results.get("suites", [])
        if len(suites) > 1:
            lines.append("")
            lines.append("| Suite | Tests | Passed | Failed | Time |")
            lines.append("|-------|-------|--------|--------|------|")
            for s in suites[:15]:  # Cap at 15 suites
                lines.append(
                    f"| {s['name']} | {s['tests']} | {s['passed']} | "
                    f"{s['failed'] + s['errors']} | {s['time']:.1f}s |"
                )
        lines.append("")

    return "\n".join(lines)


def push_health_dashboard(
    board: str = "main",
    card_id: Optional[str] = None,
    expires_days: int = 1,
    junit_path: Optional[str] = None,
) -> Optional[str]:
    """Push a system health dashboard card to Canvas.

    Args:
        board: Canvas board name.
        card_id: Update existing card instead of creating new.
        expires_days: Days until card expires.
        junit_path: Optional path to a JUnit XML report for test pass rate tracking.

    Returns the card UUID on success, None on failure.
    """
    try:
        snap = metrics.snapshot()
        alert_summary = alert.summary()
        breaker_state = _read_circuit_breaker_state()

        test_results = None
        if junit_path:
            test_results = _read_junit_xml(junit_path)

        content = _build_health_markdown(snap, alert_summary, breaker_state, test_results)

        result = _push_to_canvas(
            title="System Health",
            content=content,
            board=board,
            agent="hermes",
            emoji="🩺",
            card_id=card_id,
            expires_days=expires_days,
        )

        card_uuid = result.get("id")
        if card_uuid:
            metrics.increment("canvas.health_pushes")
            print(f"[canvas_dashboard] Health dashboard pushed: {card_uuid}")
        return card_uuid

    except Exception as e:
        alert.info("Canvas health push failed", {"error": str(e)})
        print(f"[canvas_dashboard] Health push failed: {e}")
        return None


def start_periodic_push(interval: int = 300, board: str = "trading", daemon: bool = True) -> threading.Thread:
    """Start a background thread that periodically pushes health dashboard to Canvas.

    Args:
        interval: Seconds between pushes (default: 300 = 5 min).
        board: Canvas board name.
        daemon: Whether the thread is a daemon (default: True).

    Returns:
        The background thread (already started).
    """
    import time as _time
    from src.observability import get_logger

    log = get_logger("canvas_dashboard")
    card_id: Optional[str] = None
    running = True

    def _loop():
        nonlocal card_id
        log.info("Periodic Canvas push started (every %ds on board '%s')", interval, board)
        while running:
            try:
                result = push_health_dashboard(board=board, card_id=card_id, expires_days=1)
                if result:
                    card_id = result
                log.debug("Canvas dashboard push complete")
            except Exception as e:
                log.warning("Periodic Canvas push failed: %s", e)
            _time.sleep(interval)
        log.info("Periodic Canvas push stopped")

    thread = threading.Thread(target=_loop, daemon=daemon, name="canvas-periodic-push")
    thread.start()
    return thread


# ── CLI entry point ──
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Push observability health dashboard to Canvas"
    )
    parser.add_argument(
        "--board", default="trading", help="Canvas board name (default: trading)"
    )
    parser.add_argument(
        "--card-id", default=None, help="Update existing card instead of creating new"
    )
    parser.add_argument(
        "--expires",
        type=int,
        default=1,
        help="Days until card expires (default: 1)",
    )
    parser.add_argument(
        "--junit",
        default=None,
        help="Path to JUnit XML for test pass rate tracking",
    )
    args = parser.parse_args()

    uuid = push_health_dashboard(
        board=args.board,
        card_id=args.card_id,
        expires_days=args.expires,
        junit_path=args.junit,
    )
    if uuid:
        print(f"https://canvas.wodinga.studio/?board={args.board}#card-{uuid}")
    else:
        print("Failed to push dashboard — check ~/canvas/.env and Canvas connectivity.")
        sys.exit(1)