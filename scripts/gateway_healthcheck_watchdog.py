#!/usr/bin/env python3
"""
Gateway Healthcheck Watchdog — detect OpenClaw gateway crash-loops.

When the gateway crash-loops (systemd restarts it but it fails immediately),
the service looks "active" but is unreachable. This watchdog:

1. Pings the gateway HTTP endpoint
2. Checks systemd service state (detects restart storms)
3. Tracks consecutive failures with timestamps
4. Writes gateway status (GREEN/YELLOW/RED) to heartbeat-state.json
5. Logs alerts for cron-based Telegram notification
6. Optionally force-restarts the gateway on detected crash-loop

Usage:
    python3 scripts/gateway_healthcheck_watchdog.py              # check + alert
    python3 scripts/gateway_healthcheck_watchdog.py --json       # JSON output
    python3 scripts/gateway_healthcheck_watchdog.py --restart    # attempt restart if down
    python3 scripts/gateway_healthcheck_watchdog.py --status-only  # just update state file, no alert

Exit codes:
    0 — gateway healthy (GREEN)
    1 — gateway degraded (YELLOW — intermittent failures)
    2 — gateway down (RED — crash-loop detected)
    3 — script error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("gateway_healthcheck")

# ── Configuration ──────────────────────────────────────────────────────────

GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "18789"))
GATEWAY_HEALTH_PATH = os.environ.get("GATEWAY_HEALTH_PATH", "/health")
CONNECT_TIMEOUT = int(os.environ.get("GATEWAY_CONNECT_TIMEOUT", "5"))
HTTP_TIMEOUT = int(os.environ.get("GATEWAY_HTTP_TIMEOUT", "8"))
SERVICE_NAME = os.environ.get("GATEWAY_SERVICE_NAME", "openclaw")

# Crash-loop detection
FAILURE_WINDOW_SECONDS = int(os.environ.get("CRASH_LOOP_WINDOW", "300"))  # 5 min
FAILURE_THRESHOLD = int(os.environ.get("CRASH_LOOP_THRESHOLD", "3"))  # 3 failures in window
MIN_RESTART_INTERVAL = int(os.environ.get("MIN_RESTART_INTERVAL", "30"))  # seconds between restarts

# State file
REPO_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_DIR / "state"
HEARTBEAT_STATE_FILE = os.environ.get(
    "HEARTBEAT_STATE_FILE",
    str(STATE_DIR / "heartbeat-state.json"),
)
GATEWAY_STATE_FILE = os.environ.get(
    "GATEWAY_STATE_FILE",
    str(STATE_DIR / "gateway-health.json"),
)
LOGS_DIR = REPO_DIR / "logs"


# ── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class GatewayStatus:
    """Health status for the OpenClaw gateway."""
    status: str = "unknown"  # green, yellow, red
    reachable: bool = False
    http_ok: bool = False
    http_status: Optional[int] = None
    http_latency_ms: float = 0.0
    service_active: bool = False
    service_substate: str = ""
    restart_count: int = 0
    consecutive_failures: int = 0
    last_healthy: Optional[datetime] = None
    last_checked: Optional[datetime] = None
    is_crash_loop: bool = False
    downtime_seconds: float = 0.0
    error: Optional[str] = None
    restart_attempted: bool = False
    restart_result: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reachable": self.reachable,
            "http_ok": self.http_ok,
            "http_status": self.http_status,
            "http_latency_ms": round(self.http_latency_ms, 1),
            "service_active": self.service_active,
            "service_substate": self.service_substate,
            "restart_count": self.restart_count,
            "consecutive_failures": self.consecutive_failures,
            "last_healthy": self.last_healthy.isoformat() if self.last_healthy else None,
            "last_checked": self.last_checked.isoformat() if self.last_checked else None,
            "is_crash_loop": self.is_crash_loop,
            "downtime_seconds": round(self.downtime_seconds, 1),
            "error": self.error,
            "restart_attempted": self.restart_attempted,
            "restart_result": self.restart_result,
        }


@dataclass
class FailureRecord:
    """Tracked failure history for crash-loop detection."""
    failures: List[float] = field(default_factory=list)  # timestamps
    last_healthy: Optional[datetime] = None

    def record_failure(self) -> None:
        now = time.time()
        self.failures.append(now)
        # Prune old failures outside the window
        cutoff = now - FAILURE_WINDOW_SECONDS
        self.failures = [t for t in self.failures if t > cutoff]

    def record_success(self) -> None:
        self.failures.clear()
        self.last_healthy = datetime.now(timezone.utc)

    @property
    def consecutive_count(self) -> int:
        """Number of failures in the current window."""
        return len(self.failures)

    @property
    def is_crash_loop(self) -> bool:
        """True if failures exceed threshold within the window."""
        return len(self.failures) >= FAILURE_THRESHOLD

    @property
    def downtime_seconds(self) -> float:
        """Seconds since last healthy check, or 0 if never healthy."""
        if self.last_healthy is None:
            return 0.0
        return (datetime.now(timezone.utc) - self.last_healthy).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "failure_count": len(self.failures),
            "is_crash_loop": self.is_crash_loop,
            "last_healthy": self.last_healthy.isoformat() if self.last_healthy else None,
            "downtime_seconds": round(self.downtime_seconds, 1),
        }


# ── Core Checks ────────────────────────────────────────────────────────────


def check_tcp(host: str = GATEWAY_HOST, port: int = GATEWAY_PORT,
              timeout: int = CONNECT_TIMEOUT) -> Tuple[bool, float, Optional[str]]:
    """Check if gateway port is accepting TCP connections.

    Returns:
        (reachable, latency_ms, error)
    """
    start = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        latency = (time.monotonic() - start) * 1000
        return True, latency, None
    except socket.timeout:
        latency = (time.monotonic() - start) * 1000
        return False, latency, f"Connection to {host}:{port} timed out ({timeout}s)"
    except ConnectionRefusedError:
        latency = (time.monotonic() - start) * 1000
        return False, latency, f"Connection refused at {host}:{port}"
    except OSError as e:
        latency = (time.monotonic() - start) * 1000
        return False, latency, f"OS error: {e}"


def check_http(host: str = GATEWAY_HOST, port: int = GATEWAY_PORT,
               path: str = GATEWAY_HEALTH_PATH,
               timeout: int = HTTP_TIMEOUT) -> Tuple[bool, Optional[int], float, Optional[str]]:
    """Check if the gateway HTTP health endpoint responds.

    Returns:
        (ok, http_status, latency_ms, error)
    """
    try:
        import urllib.request
        url = f"http://{host}:{port}{path}"
        start = time.monotonic()
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=timeout)
        latency = (time.monotonic() - start) * 1000
        status = resp.getcode()
        # Read a bit to confirm body
        body = resp.read(1024)
        # 2xx = healthy
        return 200 <= status < 300, status, latency, None
    except urllib.error.HTTPError as e:
        latency = (time.monotonic() - start) * 1000
        return False, e.code, latency, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        latency = (time.monotonic() - start) * 1000
        return False, None, latency, f"HTTP error: {e.reason}"
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return False, None, latency, f"HTTP check failed: {e}"


def check_systemd(service_name: str = SERVICE_NAME) -> Tuple[bool, str, int]:
    """Check systemd service state.

    Returns:
        (active, substate, restart_count_in_window)
    """
    try:
        # Check active state
        result = subprocess.run(
            ["systemctl", "show", "--no-page", service_name,
             "--property=ActiveState,SubState,ActiveEnterTimestamp,NRestarts"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log.warning("systemctl show failed: %s", result.stderr.strip())
            return False, "unknown", 0

        props = {}
        for line in result.stdout.strip().split("\n"):
            if "=" in line:
                key, val = line.split("=", 1)
                props[key] = val

        active = props.get("ActiveState", "inactive")
        substate = props.get("SubState", "unknown")
        n_restarts = int(props.get("NRestarts", "0"))

        is_active = active == "active"
        return is_active, f"{active}/{substate}", n_restarts
    except FileNotFoundError:
        log.warning("systemctl not found — can't check service state")
        return False, "no-systemctl", 0
    except Exception as e:
        log.error("systemd check failed: %s", e)
        return False, f"error: {e}", 0


# ── State Persistence ──────────────────────────────────────────────────────


def load_gateway_state() -> Dict[str, Any]:
    """Load persisted gateway state from JSON file."""
    try:
        with open(GATEWAY_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_gateway_state(state: Dict[str, Any]) -> None:
    """Persist gateway state to JSON file."""
    os.makedirs(os.path.dirname(GATEWAY_STATE_FILE), exist_ok=True)
    with open(GATEWAY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def update_heartbeat_state(status: GatewayStatus) -> None:
    """Update heartbeat-state.json with gateway status for dashboard."""
    try:
        # Read existing
        try:
            with open(HEARTBEAT_STATE_FILE) as f:
                hb = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            hb = {}

        # Update gateway section
        hb["gateway"] = {
            "status": status.status.upper(),
            "reachable": status.reachable,
            "http_ok": status.http_ok,
            "service_active": status.service_active,
            "last_checked": status.last_checked.isoformat() if status.last_checked else None,
            "last_healthy": status.last_healthy.isoformat() if status.last_healthy else None,
            "is_crash_loop": status.is_crash_loop,
            "consecutive_failures": status.consecutive_failures,
            "downtime_seconds": round(status.downtime_seconds, 1),
        }

        os.makedirs(os.path.dirname(HEARTBEAT_STATE_FILE), exist_ok=True)
        with open(HEARTBEAT_STATE_FILE, "w") as f:
            json.dump(hb, f, indent=2, default=str)
    except Exception as e:
        log.error("Failed to update heartbeat state: %s", e)


def _restore_failure_record(state: Dict[str, Any]) -> FailureRecord:
    """Restore FailureRecord from persisted state."""
    record = FailureRecord()
    failures_data = state.get("failures", {})
    record.failures = failures_data.get("timestamps", [])

    last_healthy_str = failures_data.get("last_healthy")
    if last_healthy_str:
        try:
            record.last_healthy = datetime.fromisoformat(last_healthy_str)
        except (ValueError, TypeError):
            pass
    return record


def _persist_failure_record(state: Dict[str, Any], record: FailureRecord) -> None:
    """Write FailureRecord into persisted state dict."""
    state["failures"] = {
        "timestamps": record.failures,
        "last_healthy": record.last_healthy.isoformat() if record.last_healthy else None,
    }


# ── Restart ─────────────────────────────────────────────────────────────────


def restart_gateway(service_name: str = SERVICE_NAME) -> Tuple[bool, str]:
    """Attempt to restart the gateway via systemd.

    Returns:
        (success, result_message)
    """
    log.warning("🔄 Attempting gateway restart: %s", service_name)
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", service_name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            log.info("✅ Gateway restart command succeeded")
            return True, "restart command succeeded"
        else:
            log.error("❌ Gateway restart failed: %s", result.stderr.strip())
            return False, f"restart failed: {result.stderr.strip()}"
    except FileNotFoundError:
        return False, "sudo or systemctl not available"
    except subprocess.TimeoutExpired:
        return False, "restart timed out after 30s"
    except Exception as e:
        return False, f"restart error: {e}"


# ── Main Check ─────────────────────────────────────────────────────────────


def check_gateway(
    host: str = GATEWAY_HOST,
    port: int = GATEWAY_PORT,
    attempt_restart: bool = False,
) -> GatewayStatus:
    """Run full gateway health check.

    Args:
        host: Gateway host to check.
        port: Gateway port to check.
        attempt_restart: If True and crash-loop detected, attempt service restart.

    Returns:
        GatewayStatus with full assessment.
    """
    status = GatewayStatus()
    status.last_checked = datetime.now(timezone.utc)

    # Load persisted state for failure tracking
    persisted = load_gateway_state()
    failure_record = _restore_failure_record(persisted)

    # 1. TCP check
    reachable, tcp_latency, tcp_error = check_tcp(host=host, port=port)
    status.reachable = reachable
    status.http_latency_ms = tcp_latency

    # 2. HTTP health check (only if TCP reachable)
    if reachable:
        http_ok, http_status, http_latency, http_error = check_http(host=host, port=port)
        status.http_ok = http_ok
        status.http_status = http_status
        status.http_latency_ms = http_latency
        if not http_ok:
            status.error = http_error
    else:
        status.error = tcp_error
        status.http_ok = False

    # 3. Systemd service check
    active, substate, restart_count = check_systemd()
    status.service_active = active
    status.service_substate = substate
    status.restart_count = restart_count

    # 4. Update failure tracking
    if status.reachable and status.http_ok:
        failure_record.record_success()
        status.last_healthy = failure_record.last_healthy
        status.consecutive_failures = 0
        status.is_crash_loop = False
        status.status = "green"
    else:
        failure_record.record_failure()
        status.last_healthy = failure_record.last_healthy
        status.consecutive_failures = failure_record.consecutive_count
        status.downtime_seconds = failure_record.downtime_seconds
        status.is_crash_loop = failure_record.is_crash_loop

        if status.is_crash_loop:
            status.status = "red"
        elif status.consecutive_failures >= 1:
            status.status = "yellow"
        else:
            status.status = "green"

    # 5. Auto-restart on crash-loop detection
    if status.is_crash_loop and attempt_restart:
        # Only restart if we haven't restarted too recently
        last_restart = persisted.get("last_restart_attempt", 0)
        now_ts = time.time()
        if now_ts - last_restart > MIN_RESTART_INTERVAL:
            success, msg = restart_gateway()
            status.restart_attempted = True
            status.restart_result = msg
            persisted["last_restart_attempt"] = now_ts
        else:
            cooldown = int(MIN_RESTART_INTERVAL - (now_ts - last_restart))
            status.restart_result = f"restart cooldown: {cooldown}s remaining"

    # 6. Persist state
    _persist_failure_record(persisted, failure_record)
    persisted["last_check"] = status.last_checked.isoformat()
    persisted["last_status"] = status.status
    save_gateway_state(persisted)

    # 7. Update heartbeat-state.json for dashboard
    update_heartbeat_state(status)

    return status


# ── Output Formatting ──────────────────────────────────────────────────────


def format_report(status: GatewayStatus) -> str:
    """Format a human-readable report."""
    emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(status.status, "⚪")

    lines = []
    lines.append("=" * 60)
    lines.append(f"  Gateway Healthcheck — {status.last_checked.strftime('%Y-%m-%d %H:%M:%S %Z') if status.last_checked else 'unknown'}")
    lines.append("=" * 60)
    lines.append(f"\n  Status:    {emoji} {status.status.upper()}")

    if status.reachable:
        lines.append(f"  TCP:       ✅ reachable ({status.http_latency_ms:.0f}ms)")
    else:
        lines.append(f"  TCP:       ❌ unreachable")

    if status.http_ok:
        lines.append(f"  HTTP:      ✅ {status.http_status} ({status.http_latency_ms:.0f}ms)")
    elif status.http_status:
        lines.append(f"  HTTP:      ❌ {status.http_status}")
    else:
        lines.append(f"  HTTP:      ❌ no response")

    lines.append(f"  Service:   {'✅ active' if status.service_active else '❌ inactive'} "
                 f"({status.service_substate})")
    lines.append(f"  Restarts:  {status.restart_count}")
    lines.append(f"  Failures:  {status.consecutive_failures} consecutive "
                 f"(threshold: {FAILURE_THRESHOLD} in {FAILURE_WINDOW_SECONDS}s)")

    if status.is_crash_loop:
        lines.append(f"  🚨 CRASH-LOOP DETECTED")

    if status.last_healthy:
        lines.append(f"  Last OK:   {status.last_healthy.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    if status.downtime_seconds > 0:
        mins = int(status.downtime_seconds / 60)
        secs = int(status.downtime_seconds % 60)
        lines.append(f"  Down:      {mins}m {secs}s")

    if status.error:
        lines.append(f"  Error:     {status.error}")

    if status.restart_attempted:
        lines.append(f"  Restart:   {'✅' if 'succeeded' in (status.restart_result or '') else '❌'} "
                     f"{status.restart_result}")

    lines.append("\n" + "=" * 60)

    if status.status == "red":
        lines.append("  🚨 CRITICAL: Gateway is DOWN. Crash-loop detected!")
    elif status.status == "yellow":
        lines.append("  ⚠️  WARNING: Gateway is DEGRADED.")
    else:
        lines.append("  ✅ Gateway is HEALTHY.")

    lines.append("=" * 60)
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gateway Healthcheck Watchdog — detect crash-loops"
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output JSON instead of human-readable format",
    )
    parser.add_argument(
        "--restart", "-r",
        action="store_true",
        help="Attempt restart if crash-loop detected",
    )
    parser.add_argument(
        "--status-only", "-s",
        action="store_true",
        help="Just update state file, no output unless error",
    )
    parser.add_argument(
        "--host",
        default=GATEWAY_HOST,
        help=f"Gateway host (default: {GATEWAY_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=GATEWAY_PORT,
        help=f"Gateway port (default: {GATEWAY_PORT})",
    )
    args = parser.parse_args()

    try:
        status = check_gateway(
            host=args.host, port=args.port, attempt_restart=args.restart
        )
    except Exception as e:
        log.exception("Healthcheck failed with exception")
        if args.json:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"ERROR: Healthcheck failed: {e}")
        return 3

    # Output
    if args.json:
        print(json.dumps(status.to_dict(), indent=2, default=str))
    elif not args.status_only or status.status != "green":
        print(format_report(status))

    # Exit code
    if status.status == "red":
        return 2
    elif status.status == "yellow":
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [gateway-healthcheck] %(levelname)s: %(message)s",
    )
    sys.exit(main())
