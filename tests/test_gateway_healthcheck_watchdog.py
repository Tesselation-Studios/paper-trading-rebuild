"""Tests for Gateway Healthcheck Watchdog — detect gateway crash-loops."""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from scripts.gateway_healthcheck_watchdog import (
    GatewayStatus,
    FailureRecord,
    check_gateway,
    check_tcp,
    check_http,
    check_systemd,
    restart_gateway,
    update_heartbeat_state,
    load_gateway_state,
    save_gateway_state,
    format_report,
    FAILURE_THRESHOLD,
    FAILURE_WINDOW_SECONDS,
    MIN_RESTART_INTERVAL,
    GATEWAY_PORT,
    SERVICE_NAME,
)


# ── FailureRecord ────────────────────────────────────────────────────────


class TestFailureRecord:
    def test_initial_state(self):
        r = FailureRecord()
        assert r.consecutive_count == 0
        assert not r.is_crash_loop
        assert r.downtime_seconds == 0.0

    def test_records_failure(self):
        r = FailureRecord()
        r.record_failure()
        assert r.consecutive_count == 1
        assert not r.is_crash_loop  # Below threshold

    def test_crash_loop_detection(self):
        r = FailureRecord()
        for _ in range(FAILURE_THRESHOLD):
            r.record_failure()
        assert r.consecutive_count == FAILURE_THRESHOLD
        assert r.is_crash_loop

    def test_success_clears_failures(self):
        r = FailureRecord()
        for _ in range(FAILURE_THRESHOLD):
            r.record_failure()
        assert r.is_crash_loop
        r.record_success()
        assert r.consecutive_count == 0
        assert not r.is_crash_loop
        assert r.last_healthy is not None

    def test_failures_expire_outside_window(self):
        r = FailureRecord()
        # Add failures far in the past
        old = time.time() - (FAILURE_WINDOW_SECONDS + 60)
        r.failures = [old, old, old]
        # Record a new failure — old ones should be pruned
        r.record_failure()
        assert r.consecutive_count == 1  # Only the new one
        assert not r.is_crash_loop

    def test_downtime_calculation(self):
        r = FailureRecord()
        r.last_healthy = datetime.now(timezone.utc) - timedelta(seconds=120)
        assert 115 <= r.downtime_seconds <= 125  # ~120s

    def test_to_dict(self):
        r = FailureRecord()
        r.record_failure()
        r.record_success()
        d = r.to_dict()
        assert d["failure_count"] == 0
        assert not d["is_crash_loop"]
        assert d["last_healthy"] is not None


# ── GatewayStatus ────────────────────────────────────────────────────────


class TestGatewayStatus:
    def test_defaults(self):
        s = GatewayStatus()
        assert s.status == "unknown"
        assert not s.reachable
        assert not s.http_ok

    def test_to_dict(self):
        s = GatewayStatus(
            status="green",
            reachable=True,
            http_ok=True,
            http_status=200,
            http_latency_ms=12.5,
            service_active=True,
            service_substate="active/running",
            restart_count=0,
            consecutive_failures=0,
            last_checked=datetime.now(timezone.utc),
        )
        d = s.to_dict()
        assert d["status"] == "green"
        assert d["reachable"] is True
        assert d["http_ok"] is True
        assert d["http_status"] == 200
        assert d["http_latency_ms"] == 12.5
        assert d["service_active"] is True

    def test_to_dict_with_none_values(self):
        s = GatewayStatus()
        d = s.to_dict()
        assert d["last_healthy"] is None
        assert d["last_checked"] is None
        assert d["error"] is None
        assert d["restart_result"] is None


# ── TCP Check ─────────────────────────────────────────────────────────────


class TestCheckTcp:
    def test_successful_connection(self):
        with patch("scripts.gateway_healthcheck_watchdog.socket.create_connection") as mock_connect:
            mock_sock = MagicMock()
            mock_connect.return_value = mock_sock
            reachable, latency, error = check_tcp("127.0.0.1", 9999, timeout=2)
            assert reachable is True
            assert latency >= 0
            assert error is None

    def test_connection_refused(self):
        with patch("scripts.gateway_healthcheck_watchdog.socket.create_connection") as mock_connect:
            mock_connect.side_effect = ConnectionRefusedError()
            reachable, latency, error = check_tcp("127.0.0.1", 9999)
            assert reachable is False
            assert "Connection refused" in (error or "")

    def test_timeout(self):
        with patch("scripts.gateway_healthcheck_watchdog.socket.create_connection") as mock_connect:
            mock_connect.side_effect = socket_timeout()
            reachable, latency, error = check_tcp("127.0.0.1", 9999, timeout=1)
            assert reachable is False
            assert "timed out" in (error or "").lower()


def socket_timeout():
    """Create a socket.timeout exception without importing socket at module level."""
    import socket
    return socket.timeout("timed out")


# ── HTTP Check ────────────────────────────────────────────────────────────


class TestCheckHttp:
    def test_healthy(self):
        import urllib.request
        with patch.object(urllib.request, "urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            mock_resp.read.return_value = b'{"status":"ok"}'
            mock_urlopen.return_value = mock_resp

            ok, status, latency, error = check_http("127.0.0.1", 9999, "/health")
            assert ok is True
            assert status == 200
            assert latency >= 0
            assert error is None

    def test_http_500(self):
        import urllib.request
        import urllib.error
        with patch.object(urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "http://127.0.0.1:9999/health", 500, "Internal Error",
                {}, None  # type: ignore[arg-type]
            )
            ok, status, latency, error = check_http("127.0.0.1", 9999, "/health")
            assert ok is False
            assert status == 500
            assert "500" in (error or "")

    def test_connection_error(self):
        import urllib.request
        import urllib.error
        with patch.object(urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")
            ok, status, latency, error = check_http("127.0.0.1", 9999, "/health")
            assert ok is False
            assert status is None


# ── Systemd Check ─────────────────────────────────────────────────────────


class TestCheckSystemd:
    def test_active_running(self):
        with patch("scripts.gateway_healthcheck_watchdog.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="ActiveState=active\nSubState=running\nNRestarts=0\n",
                stderr="",
            )
            active, substate, restarts = check_systemd("openclaw")
            assert active is True
            assert "running" in substate
            assert restarts == 0

    def test_failed(self):
        with patch("scripts.gateway_healthcheck_watchdog.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="ActiveState=failed\nSubState=failed\nNRestarts=5\n",
                stderr="",
            )
            active, substate, restarts = check_systemd("openclaw")
            assert active is False
            assert "failed" in substate
            assert restarts == 5

    def test_systemctl_not_found(self):
        with patch("scripts.gateway_healthcheck_watchdog.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            active, substate, restarts = check_systemd("openclaw")
            assert active is False
            assert "no-systemctl" == substate
            assert restarts == 0


# ── Restart ───────────────────────────────────────────────────────────────


class TestRestartGateway:
    def test_successful_restart(self):
        with patch("scripts.gateway_healthcheck_watchdog.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            success, msg = restart_gateway("openclaw")
            assert success is True
            assert "succeeded" in msg

    def test_failed_restart(self):
        with patch("scripts.gateway_healthcheck_watchdog.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stderr="access denied"
            )
            success, msg = restart_gateway("openclaw")
            assert success is False
            assert "access denied" in msg

    def test_no_sudo(self):
        with patch("scripts.gateway_healthcheck_watchdog.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            success, msg = restart_gateway("openclaw")
            assert success is False
            assert "not available" in msg


# ── State Persistence ─────────────────────────────────────────────────────


class TestStatePersistence:
    def test_save_and_load(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            tf_path = tf.name

        try:
            with patch("scripts.gateway_healthcheck_watchdog.GATEWAY_STATE_FILE", tf_path):
                state = {"test": "value", "failures": {"timestamps": [1.0, 2.0]}}
                save_gateway_state(state)
                loaded = load_gateway_state()
                assert loaded["test"] == "value"
                assert loaded["failures"]["timestamps"] == [1.0, 2.0]
        finally:
            os.unlink(tf_path)

    def test_load_nonexistent_returns_empty(self):
        with patch("scripts.gateway_healthcheck_watchdog.GATEWAY_STATE_FILE",
                   "/tmp/nonexistent_gateway_state.json"):
            result = load_gateway_state()
            assert result == {}

    def test_load_corrupt_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            tf.write("not valid json{{{")
            tf_path = tf.name

        try:
            with patch("scripts.gateway_healthcheck_watchdog.GATEWAY_STATE_FILE", tf_path):
                result = load_gateway_state()
                assert result == {}
        finally:
            os.unlink(tf_path)


class TestUpdateHeartbeatState:
    def test_updates_heartbeat_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            json.dump({"last_kairos": "2026-01-01T00:00:00+00:00"}, tf)
            tf_path = tf.name

        try:
            with patch("scripts.gateway_healthcheck_watchdog.HEARTBEAT_STATE_FILE", tf_path):
                status = GatewayStatus(
                    status="green",
                    reachable=True,
                    http_ok=True,
                    service_active=True,
                    last_checked=datetime.now(timezone.utc),
                )
                update_heartbeat_state(status)

                with open(tf_path) as f:
                    hb = json.load(f)
                assert "gateway" in hb
                assert hb["gateway"]["status"] == "GREEN"
                assert hb["gateway"]["reachable"] is True
                # Existing keys preserved
                assert "last_kairos" in hb
        finally:
            os.unlink(tf_path)

    def test_creates_file_if_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hb_path = os.path.join(tmpdir, "heartbeat-state.json")
            with patch("scripts.gateway_healthcheck_watchdog.HEARTBEAT_STATE_FILE", hb_path):
                status = GatewayStatus(status="red", reachable=False)
                update_heartbeat_state(status)

                assert os.path.exists(hb_path)
                with open(hb_path) as f:
                    hb = json.load(f)
                assert hb["gateway"]["status"] == "RED"


# ── check_gateway (integration) ───────────────────────────────────────────


class TestCheckGateway:
    @pytest.fixture(autouse=True)
    def _isolate_state(self):
        """Ensure each check_gateway test uses a fresh state file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            tf.write("{}")
            tf_path = tf.name
        with patch("scripts.gateway_healthcheck_watchdog.GATEWAY_STATE_FILE", tf_path), \
             patch("scripts.gateway_healthcheck_watchdog.HEARTBEAT_STATE_FILE", tf_path):
            yield
        try:
            os.unlink(tf_path)
        except OSError:
            pass

    def test_healthy_gateway(self):
        """All checks pass → GREEN."""
        with patch("scripts.gateway_healthcheck_watchdog.check_tcp") as mock_tcp, \
             patch("scripts.gateway_healthcheck_watchdog.check_http") as mock_http, \
             patch("scripts.gateway_healthcheck_watchdog.check_systemd") as mock_systemd, \
             patch("scripts.gateway_healthcheck_watchdog.update_heartbeat_state") as mock_hb, \
             patch("scripts.gateway_healthcheck_watchdog.save_gateway_state") as mock_save:

            mock_tcp.return_value = (True, 5.0, None)
            mock_http.return_value = (True, 200, 12.0, None)
            mock_systemd.return_value = (True, "active/running", 0)

            status = check_gateway()
            assert status.status == "green"
            assert status.reachable is True
            assert status.http_ok is True
            assert not status.is_crash_loop
            mock_hb.assert_called_once()
            mock_save.assert_called_once()

    def test_unreachable_leads_to_red(self):
        """Multiple failures detected (simulate crash-loop) → RED."""
        with patch("scripts.gateway_healthcheck_watchdog.check_tcp") as mock_tcp, \
             patch("scripts.gateway_healthcheck_watchdog.check_http") as mock_http, \
             patch("scripts.gateway_healthcheck_watchdog.check_systemd") as mock_systemd, \
             patch("scripts.gateway_healthcheck_watchdog.update_heartbeat_state"):

            mock_tcp.return_value = (False, 5000.0, "timeout")
            mock_http.return_value = (False, None, 0, "unreachable")
            mock_systemd.return_value = (False, "failed/failed", 10)

            # First call — 1 failure
            status1 = check_gateway()
            assert status1.status == "yellow", f"Expected yellow, got {status1.status}"
            assert not status1.is_crash_loop

            # Second call — 2 failures
            status2 = check_gateway()
            assert status2.status == "yellow", f"Expected yellow, got {status2.status}"
            assert not status2.is_crash_loop

            # Third call — crash-loop threshold
            status3 = check_gateway()
            assert status3.status == "red", f"Expected red, got {status3.status}"
            assert status3.is_crash_loop
            assert status3.consecutive_failures >= FAILURE_THRESHOLD

    def test_recovery_after_failures(self):
        """After failures, a healthy check resets to GREEN."""
        with patch("scripts.gateway_healthcheck_watchdog.check_tcp") as mock_tcp, \
             patch("scripts.gateway_healthcheck_watchdog.check_http") as mock_http, \
             patch("scripts.gateway_healthcheck_watchdog.check_systemd") as mock_systemd, \
             patch("scripts.gateway_healthcheck_watchdog.update_heartbeat_state"), \
             patch("scripts.gateway_healthcheck_watchdog.save_gateway_state"):

            # Fail 3 times
            mock_tcp.return_value = (False, 5000.0, "timeout")
            mock_http.return_value = (False, None, 0, "unreachable")
            mock_systemd.return_value = (False, "failed/failed", 10)

            for _ in range(FAILURE_THRESHOLD):
                check_gateway()

            # Now healthy
            mock_tcp.return_value = (True, 5.0, None)
            mock_http.return_value = (True, 200, 12.0, None)
            mock_systemd.return_value = (True, "active/running", 0)

            status = check_gateway()
            assert status.status == "green"
            assert status.consecutive_failures == 0
            assert not status.is_crash_loop

    def test_tcp_ok_but_http_down(self):
        """TCP connects but HTTP fails → YELLOW."""
        with patch("scripts.gateway_healthcheck_watchdog.check_tcp") as mock_tcp, \
             patch("scripts.gateway_healthcheck_watchdog.check_http") as mock_http, \
             patch("scripts.gateway_healthcheck_watchdog.check_systemd") as mock_systemd, \
             patch("scripts.gateway_healthcheck_watchdog.update_heartbeat_state"), \
             patch("scripts.gateway_healthcheck_watchdog.save_gateway_state"):

            mock_tcp.return_value = (True, 5.0, None)
            mock_http.return_value = (False, 502, 50.0, "Bad Gateway")
            mock_systemd.return_value = (True, "active/running", 2)

            status = check_gateway()
            assert status.status == "yellow"
            assert status.reachable is True
            assert status.http_ok is False
            assert status.http_status == 502

    def test_restart_on_crash_loop(self):
        """--restart flag triggers restart when crash-loop detected."""
        with patch("scripts.gateway_healthcheck_watchdog.check_tcp") as mock_tcp, \
             patch("scripts.gateway_healthcheck_watchdog.check_http") as mock_http, \
             patch("scripts.gateway_healthcheck_watchdog.check_systemd") as mock_systemd, \
             patch("scripts.gateway_healthcheck_watchdog.update_heartbeat_state"), \
             patch("scripts.gateway_healthcheck_watchdog.restart_gateway") as mock_restart:

            mock_tcp.return_value = (False, 5000.0, "timeout")
            mock_http.return_value = (False, None, 0, "unreachable")
            mock_systemd.return_value = (False, "failed/failed", 10)
            mock_restart.return_value = (True, "restart command succeeded")

            # Build up to crash-loop
            for _ in range(FAILURE_THRESHOLD - 1):
                check_gateway(attempt_restart=False)

            # Now trigger restart
            status = check_gateway(attempt_restart=True)
            assert status.status == "red", f"Expected red, got {status.status}"
            assert status.is_crash_loop
            assert status.restart_attempted is True
            mock_restart.assert_called_once()

    def test_restart_cooldown(self):
        """Restart respects MIN_RESTART_INTERVAL cooldown."""
        with patch("scripts.gateway_healthcheck_watchdog.check_tcp") as mock_tcp, \
             patch("scripts.gateway_healthcheck_watchdog.check_http") as mock_http, \
             patch("scripts.gateway_healthcheck_watchdog.check_systemd") as mock_systemd, \
             patch("scripts.gateway_healthcheck_watchdog.update_heartbeat_state"), \
             patch("scripts.gateway_healthcheck_watchdog.restart_gateway") as mock_restart:

            mock_tcp.return_value = (False, 5000.0, "timeout")
            mock_http.return_value = (False, None, 0, "unreachable")
            mock_systemd.return_value = (False, "failed/failed", 10)
            mock_restart.return_value = (True, "restart command succeeded")

            # Build up to crash-loop
            for _ in range(FAILURE_THRESHOLD - 1):
                check_gateway(attempt_restart=False)

            # First restart succeeds
            status1 = check_gateway(attempt_restart=True)
            assert status1.restart_attempted is True
            mock_restart.assert_called_once()

            # Second restart within cooldown should be blocked
            status2 = check_gateway(attempt_restart=True)
            assert "cooldown" in (status2.restart_result or "")
            # restart_gateway should only have been called once
            assert mock_restart.call_count == 1


# ── Format Report ─────────────────────────────────────────────────────────


class TestFormatReport:
    def test_green_report(self):
        s = GatewayStatus(
            status="green",
            reachable=True,
            http_ok=True,
            http_status=200,
            http_latency_ms=5.2,
            service_active=True,
            service_substate="active/running",
            last_checked=datetime.now(timezone.utc),
        )
        report = format_report(s)
        assert "GREEN" in report
        assert "🟢" in report
        assert "HEALTHY" in report

    def test_red_report(self):
        s = GatewayStatus(
            status="red",
            reachable=False,
            http_ok=False,
            service_active=False,
            service_substate="failed/failed",
            is_crash_loop=True,
            consecutive_failures=5,
            last_checked=datetime.now(timezone.utc),
            error="Connection refused",
            downtime_seconds=300,
        )
        report = format_report(s)
        assert "RED" in report
        assert "🔴" in report
        assert "CRASH-LOOP" in report
        assert "CRITICAL" in report
        assert "Connection refused" in report

    def test_yellow_report(self):
        s = GatewayStatus(
            status="yellow",
            reachable=True,
            http_ok=False,
            http_status=502,
            service_active=True,
            service_substate="active/running",
            consecutive_failures=1,
            last_checked=datetime.now(timezone.utc),
            error="HTTP 502",
        )
        report = format_report(s)
        assert "YELLOW" in report
        assert "🟡" in report
        assert "DEGRADED" in report
        assert "502" in report


# ── CLI (main) ────────────────────────────────────────────────────────────


class TestMain:
    def test_main_green_exit_0(self):
        with patch("scripts.gateway_healthcheck_watchdog.check_gateway") as mock_check:
            mock_check.return_value = GatewayStatus(
                status="green",
                reachable=True,
                http_ok=True,
                service_active=True,
                last_checked=datetime.now(timezone.utc),
            )
            with patch.object(sys, "argv", ["gateway_healthcheck_watchdog.py", "--status-only"]):
                from scripts.gateway_healthcheck_watchdog import main
                exit_code = main()
                assert exit_code == 0

    def test_main_red_exit_2(self):
        with patch("scripts.gateway_healthcheck_watchdog.check_gateway") as mock_check:
            mock_check.return_value = GatewayStatus(
                status="red",
                reachable=False,
                is_crash_loop=True,
                last_checked=datetime.now(timezone.utc),
            )
            with patch.object(sys, "argv", ["gateway_healthcheck_watchdog.py", "--status-only"]):
                from scripts.gateway_healthcheck_watchdog import main
                exit_code = main()
                assert exit_code == 2

    def test_main_yellow_exit_1(self):
        with patch("scripts.gateway_healthcheck_watchdog.check_gateway") as mock_check:
            mock_check.return_value = GatewayStatus(
                status="yellow",
                reachable=True,
                http_ok=False,
                last_checked=datetime.now(timezone.utc),
            )
            with patch.object(sys, "argv", ["gateway_healthcheck_watchdog.py", "--status-only"]):
                from scripts.gateway_healthcheck_watchdog import main
                exit_code = main()
                assert exit_code == 1

    def test_main_json_output(self):
        with patch("scripts.gateway_healthcheck_watchdog.check_gateway") as mock_check:
            mock_check.return_value = GatewayStatus(
                status="green",
                reachable=True,
                http_ok=True,
                service_active=True,
                last_checked=datetime.now(timezone.utc),
            )
            with patch.object(sys, "argv", ["gateway_healthcheck_watchdog.py", "--json"]):
                from scripts.gateway_healthcheck_watchdog import main
                exit_code = main()
                assert exit_code == 0

    def test_main_exception_exit_3(self):
        with patch("scripts.gateway_healthcheck_watchdog.check_gateway") as mock_check:
            mock_check.side_effect = RuntimeError("test error")
            with patch.object(sys, "argv", ["gateway_healthcheck_watchdog.py", "--status-only"]):
                from scripts.gateway_healthcheck_watchdog import main
                exit_code = main()
                assert exit_code == 3
