"""Tests for D-State Watchdog — detect silent/stuck traders."""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from src.d_state_watchdog import (
    TraderStatus,
    WatchdogReport,
    _resolve_trader_id,
    _get_tick_interval,
    _load_monitoring_config,
    check_trader,
    check_all_traders,
    DEFAULT_TRADERS,
    TRADER_TICK_INTERVALS,
)


# ── ID Resolution ──────────────────────────────────────────────────────────


class TestResolveTraderId:
    def test_short_form(self):
        assert _resolve_trader_id("kairos") == "kairos"
        assert _resolve_trader_id("aldridge") == "aldridge"
        assert _resolve_trader_id("stonks") == "stonks"

    def test_trader_prefixed(self):
        assert _resolve_trader_id("trader-kairos") == "kairos"
        assert _resolve_trader_id("trader-aldridge") == "aldridge"
        assert _resolve_trader_id("trader-stonks") == "stonks"

    def test_case_insensitive(self):
        assert _resolve_trader_id("KAIROS") == "kairos"
        assert _resolve_trader_id("Trader-Aldridge") == "aldridge"

    def test_unknown(self):
        assert _resolve_trader_id("unknown") == "unknown"


# ── Tick Intervals ─────────────────────────────────────────────────────────


class TestGetTickInterval:
    def test_kairos(self):
        assert _get_tick_interval("kairos") == 5

    def test_aldridge(self):
        assert _get_tick_interval("aldridge") == 30

    def test_stonks(self):
        assert _get_tick_interval("stonks") == 15

    def test_trader_prefixed(self):
        assert _get_tick_interval("trader-kairos") == 5

    def test_unknown_defaults_to_15(self):
        assert _get_tick_interval("nonexistent") == 15


# ── Monitoring Config ──────────────────────────────────────────────────────


class TestLoadMonitoringConfig:
    def test_loads_from_config(self):
        stale, limit = _load_monitoring_config()
        assert isinstance(stale, int)
        assert isinstance(limit, int)
        assert stale > 0
        assert limit > 0

    def test_default_values_when_config_unavailable(self):
        with patch("src.d_state_watchdog._load_monitoring_config") as mock_load:
            mock_load.return_value = (900, 3)
            stale, limit = mock_load()
            assert stale == 900
            assert limit == 3


# ── TraderStatus ───────────────────────────────────────────────────────────


class TestTraderStatus:
    def test_last_activity_prefers_decision(self):
        now = datetime.now(timezone.utc)
        s = TraderStatus(
            trader_id="test",
            last_decision=now,
            last_trade=now - timedelta(minutes=5),
            last_heartbeat=now - timedelta(minutes=10),
        )
        assert s.last_activity == now

    def test_last_activity_falls_back_to_trade(self):
        now = datetime.now(timezone.utc)
        s = TraderStatus(
            trader_id="test",
            last_trade=now - timedelta(minutes=5),
            last_heartbeat=now - timedelta(minutes=10),
        )
        assert s.last_activity == now - timedelta(minutes=5)

    def test_last_activity_falls_back_to_heartbeat(self):
        now = datetime.now(timezone.utc)
        s = TraderStatus(
            trader_id="test",
            last_heartbeat=now - timedelta(minutes=10),
        )
        assert s.last_activity == now - timedelta(minutes=10)

    def test_last_activity_none_when_no_data(self):
        s = TraderStatus(trader_id="test")
        assert s.last_activity is None

    def test_to_dict(self):
        now = datetime.now(timezone.utc)
        s = TraderStatus(
            trader_id="kairos",
            is_active=True,
            last_decision=now,
            silence_seconds=120.0,
            tick_interval_minutes=5,
            ticks_silent=0,
        )
        d = s.to_dict()
        assert d["trader_id"] == "kairos"
        assert d["is_active"] is True
        assert d["silence_seconds"] == 120.0
        assert d["ticks_silent"] == 0


# ── check_trader ───────────────────────────────────────────────────────────


class TestCheckTrader:
    """Test check_trader with mocked DB queries."""

    def _make_state(self, trader_id="kairos", heartbeat_ago=None, trade_ago=None,
                    is_active=True, equity=10000.0, pnl=0.0, positions=0):
        """Build a mock agent_state row."""
        now = datetime.now(timezone.utc)
        return {
            "agent_id": trader_id,
            "is_active": is_active,
            "last_heartbeat": now - timedelta(seconds=heartbeat_ago) if heartbeat_ago else now,
            "last_trade": now - timedelta(seconds=trade_ago) if trade_ago else None,
            "cash": equity,
            "equity": equity,
            "pnl": pnl,
            "positions_count": positions,
        }

    def _make_decision(self, trader_id="kairos", seconds_ago=60, decision_type="BUY"):
        """Build a mock decisions row."""
        now = datetime.now(timezone.utc)
        return {
            "last_decision": now - timedelta(seconds=seconds_ago),
            "last_decision_type": decision_type,
        }

    def test_healthy_trader(self):
        """Trader with recent activity should be OK."""
        with patch("src.d_state_watchdog._query_agent_state") as mock_state, \
             patch("src.d_state_watchdog._query_decisions") as mock_decisions:
            mock_state.return_value = {
                "trader-kairos": self._make_state("trader-kairos", heartbeat_ago=60)
            }
            mock_decisions.return_value = {
                "kairos": self._make_decision("kairos", seconds_ago=120)
            }

            status = check_trader("kairos")
            assert not status.is_d_state
            assert status.severity == "ok"
            assert status.ticks_silent == 0
            assert status.last_decision is not None

    def test_silent_trader_d_state(self):
        """Trader silent for longer than alert threshold → D-state."""
        with patch("src.d_state_watchdog._query_agent_state") as mock_state, \
             patch("src.d_state_watchdog._query_decisions") as mock_decisions:
            # Last activity was 1 hour ago
            seconds_ago = 3600
            mock_state.return_value = {
                "trader-kairos": self._make_state(
                    "trader-kairos", heartbeat_ago=seconds_ago
                )
            }
            mock_decisions.return_value = {
                "kairos": self._make_decision("kairos", seconds_ago=seconds_ago)
            }

            status = check_trader("kairos", stale_threshold=300, missed_limit=3)
            # Threshold: 300 * 3 = 900s. 3600s > 900s → D-state
            assert status.is_d_state
            assert status.severity == "critical"
            assert status.ticks_silent > 0

    def test_warning_approaching_threshold(self):
        """Trader silent longer than stale_threshold but less than alert → warning."""
        with patch("src.d_state_watchdog._query_agent_state") as mock_state, \
             patch("src.d_state_watchdog._query_decisions") as mock_decisions:
            # Use Kairos (5-min ticks): 600s = 2 ticks, stale=300, alert=900 → warning
            seconds_ago = 600
            mock_state.return_value = {
                "trader-kairos": self._make_state(
                    "trader-kairos", heartbeat_ago=seconds_ago
                )
            }
            mock_decisions.return_value = {
                "kairos": self._make_decision("kairos", seconds_ago=seconds_ago)
            }

            status = check_trader("kairos", stale_threshold=300, missed_limit=3)
            assert not status.is_d_state
            assert status.severity == "warning"
            assert status.ticks_silent == 2  # 600s / 300s = 2 ticks

    def test_no_activity_ever(self):
        """Trader with no recorded activity at all → critical D-state."""
        with patch("src.d_state_watchdog._query_agent_state") as mock_state, \
             patch("src.d_state_watchdog._query_decisions") as mock_decisions:
            mock_state.return_value = {}
            mock_decisions.return_value = {}

            status = check_trader("aldridge")
            assert status.is_d_state
            assert status.severity == "critical"
            assert status.error is not None
            assert "No activity ever recorded" in status.error

    def test_uses_short_id_for_agent_state(self):
        """Should try both 'kairos' and 'trader-kairos' for agent_state."""
        with patch("src.d_state_watchdog._query_agent_state") as mock_state, \
             patch("src.d_state_watchdog._query_decisions") as mock_decisions:
            mock_state.return_value = {
                "kairos": self._make_state("kairos", heartbeat_ago=30)
            }
            mock_decisions.return_value = {}

            status = check_trader("kairos")
            # Should have found the state
            assert status.last_heartbeat is not None
            # agent_state is queried with both IDs
            call_args = mock_state.call_args
            assert "kairos" in str(call_args) or "kairos" in str(call_args[0])


# ── check_all_traders ──────────────────────────────────────────────────────


class TestCheckAllTraders:
    def test_all_healthy(self):
        """All traders healthy → report with 0 D-state."""
        with patch("src.d_state_watchdog._query_agent_state") as mock_state, \
             patch("src.d_state_watchdog._query_decisions") as mock_decisions, \
             patch("src.d_state_watchdog._load_monitoring_config") as mock_config:

            mock_config.return_value = (900, 3)
            now = datetime.now(timezone.utc)
            mock_state.return_value = {
                "trader-kairos": {"agent_id": "trader-kairos", "is_active": True,
                                   "last_heartbeat": now, "last_trade": None,
                                   "cash": 10000, "equity": 10000, "pnl": 0, "positions_count": 0},
                "trader-aldridge": {"agent_id": "trader-aldridge", "is_active": True,
                                     "last_heartbeat": now, "last_trade": None,
                                     "cash": 10000, "equity": 10000, "pnl": 0, "positions_count": 0},
                "trader-stonks": {"agent_id": "trader-stonks", "is_active": True,
                                   "last_heartbeat": now, "last_trade": None,
                                   "cash": 10000, "equity": 10000, "pnl": 0, "positions_count": 0},
            }
            mock_decisions.return_value = {}

            report = check_all_traders()
            assert report.checked_traders == 3
            assert report.d_state_traders == 0
            assert report.ok_traders == 3

    def test_one_d_state(self):
        """One trader in D-state → report identifies it."""
        with patch("src.d_state_watchdog._query_agent_state") as mock_state, \
             patch("src.d_state_watchdog._query_decisions") as mock_decisions, \
             patch("src.d_state_watchdog._load_monitoring_config") as mock_config:

            mock_config.return_value = (300, 2)  # alert at 600s
            now = datetime.now(timezone.utc)
            old = now - timedelta(hours=2)  # 2 hours ago → D-state

            mock_state.return_value = {
                "trader-kairos": {"agent_id": "trader-kairos", "is_active": True,
                                   "last_heartbeat": now, "last_trade": None,
                                   "cash": 10000, "equity": 10000, "pnl": 0, "positions_count": 0},
                "trader-aldridge": {"agent_id": "trader-aldridge", "is_active": True,
                                     "last_heartbeat": old, "last_trade": None,
                                     "cash": 10000, "equity": 10000, "pnl": 0, "positions_count": 0},
                "trader-stonks": {"agent_id": "trader-stonks", "is_active": True,
                                   "last_heartbeat": now, "last_trade": None,
                                   "cash": 10000, "equity": 10000, "pnl": 0, "positions_count": 0},
            }
            mock_decisions.return_value = {}

            report = check_all_traders()
            assert report.d_state_traders == 1
            assert report.ok_traders == 2

    def test_single_trader_filter(self):
        """Check a specific trader only."""
        with patch("src.d_state_watchdog._query_agent_state") as mock_state, \
             patch("src.d_state_watchdog._query_decisions") as mock_decisions, \
             patch("src.d_state_watchdog._load_monitoring_config") as mock_config:

            mock_config.return_value = (900, 3)
            now = datetime.now(timezone.utc)
            mock_state.return_value = {
                "trader-kairos": {"agent_id": "trader-kairos", "is_active": True,
                                   "last_heartbeat": now, "last_trade": None,
                                   "cash": 10000, "equity": 10000, "pnl": 0, "positions_count": 0},
            }
            mock_decisions.return_value = {}

            report = check_all_traders(trader_ids=["kairos"])
            assert report.checked_traders == 1
            assert len(report.traders) == 1
            assert report.traders[0].trader_id == "kairos"


# ── WatchdogReport ─────────────────────────────────────────────────────────


class TestWatchdogReport:
    def test_has_alerts_when_d_state(self):
        r = WatchdogReport(timestamp=datetime.now(timezone.utc))
        r.d_state_traders = 1
        assert r.has_alerts is True

    def test_has_alerts_when_warning(self):
        r = WatchdogReport(timestamp=datetime.now(timezone.utc))
        r.warning_traders = 2
        assert r.has_alerts is True

    def test_no_alerts_when_all_ok(self):
        r = WatchdogReport(timestamp=datetime.now(timezone.utc))
        r.ok_traders = 3
        assert r.has_alerts is False

    def test_to_dict(self):
        r = WatchdogReport(timestamp=datetime.now(timezone.utc))
        d = r.to_dict()
        assert "timestamp" in d
        assert "summary" in d
        assert "traders" in d
        assert d["summary"]["checked"] == 0
        assert d["summary"]["d_state"] == 0


# ── End-to-end (main function) ─────────────────────────────────────────────


class TestMain:
    def test_main_healthy(self):
        """main() returns 0 when all traders healthy."""
        with patch("src.d_state_watchdog.check_all_traders") as mock_check:
            now = datetime.now(timezone.utc)
            mock_report = WatchdogReport(timestamp=now)
            mock_report.ok_traders = 3
            mock_report.checked_traders = 3
            mock_report.traders = [
                TraderStatus(trader_id="kairos", is_active=True,
                             last_heartbeat=now, severity="ok"),
                TraderStatus(trader_id="aldridge", is_active=True,
                             last_heartbeat=now, severity="ok"),
                TraderStatus(trader_id="stonks", is_active=True,
                             last_heartbeat=now, severity="ok"),
            ]
            mock_check.return_value = mock_report

            from src.d_state_watchdog import main
            # Patch sys.argv
            with patch.object(sys, "argv", ["d_state_watchdog.py", "--quiet"]):
                exit_code = main()
                assert exit_code == 0

    def test_main_d_state(self):
        """main() returns 1 when trader in D-state."""
        with patch("src.d_state_watchdog.check_all_traders") as mock_check:
            now = datetime.now(timezone.utc)
            mock_report = WatchdogReport(timestamp=now)
            mock_report.ok_traders = 2
            mock_report.d_state_traders = 1
            mock_report.checked_traders = 3
            mock_report.traders = [
                TraderStatus(trader_id="kairos", is_active=True,
                             last_heartbeat=now, severity="ok"),
                TraderStatus(trader_id="aldridge", is_active=True,
                             severity="critical", is_d_state=True,
                             error="No activity"),
                TraderStatus(trader_id="stonks", is_active=True,
                             last_heartbeat=now, severity="ok"),
            ]
            mock_check.return_value = mock_report

            from src.d_state_watchdog import main
            with patch.object(sys, "argv", ["d_state_watchdog.py", "--quiet"]):
                exit_code = main()
                assert exit_code == 1


# ── Restart functionality ─────────────────────────────────────────────────

class TestRestartStalledTraders:
    """Tests for _restart_stalled_traders — auto-restart via SSH."""

    def _make_report(self, d_state=None, warnings=None, ok=None):
        """Build a WatchdogReport with given statuses."""
        now = datetime.now(timezone.utc)
        report = WatchdogReport(timestamp=now)
        traders = []
        if d_state:
            for tid in d_state:
                traders.append(TraderStatus(
                    trader_id=tid, is_active=True,
                    severity="critical", is_d_state=True,
                ))
        if warnings:
            for tid in warnings:
                traders.append(TraderStatus(
                    trader_id=tid, is_active=True,
                    last_heartbeat=now - timedelta(minutes=20),
                    severity="warning",
                ))
        if ok:
            for tid in ok:
                traders.append(TraderStatus(
                    trader_id=tid, is_active=True,
                    last_heartbeat=now, severity="ok",
                ))
        report.traders = traders
        report.checked_traders = len(traders)
        report.d_state_traders = len(d_state or [])
        report.warning_traders = len(warnings or [])
        report.ok_traders = len(ok or [])
        return report

    def test_no_alerts_no_restart(self, tmp_path):
        """All traders healthy → no restart attempted."""
        from src.d_state_watchdog import _restart_stalled_traders
        state_path = str(tmp_path / "crash-recovery.json")
        report = self._make_report(ok=["kairos", "aldridge", "stonks"])
        with patch("src.d_state_watchdog.CRASH_STATE_PATH", state_path):
            results = _restart_stalled_traders(report)
        assert results == {}

    def test_restarts_d_state(self, tmp_path):
        """D-state trader triggers gateway restart."""
        from src.d_state_watchdog import _restart_stalled_traders
        state_path = str(tmp_path / "crash-recovery.json")
        report = self._make_report(
            d_state=["stonks"],
            ok=["kairos", "aldridge"],
        )
        with patch("src.d_state_watchdog.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            with patch("src.d_state_watchdog.CRASH_STATE_PATH", state_path):
                results = _restart_stalled_traders(report)

            assert results["stonks"] == "restarted"
            assert "kairos" not in results
            assert "aldridge" not in results
            mock_run.assert_called_once()

    def test_restarts_warnings_too(self, tmp_path):
        """Warning traders also trigger restart."""
        from src.d_state_watchdog import _restart_stalled_traders
        state_path = str(tmp_path / "crash-recovery.json")
        report = self._make_report(
            warnings=["kairos"],
            ok=["aldridge", "stonks"],
        )
        with patch("src.d_state_watchdog.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            with patch("src.d_state_watchdog.CRASH_STATE_PATH", state_path):
                results = _restart_stalled_traders(report)

            assert results["kairos"] == "restarted"
            mock_run.assert_called_once()

    def test_restart_failure(self, tmp_path):
        """SSH failure returns error per trader."""
        from src.d_state_watchdog import _restart_stalled_traders
        state_path = str(tmp_path / "crash-recovery.json")
        report = self._make_report(
            d_state=["stonks"],
            ok=["kairos", "aldridge"],
        )
        with patch("src.d_state_watchdog.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stdout = ""
            mock_result.stderr = "Connection refused"
            mock_run.return_value = mock_result

            with patch("src.d_state_watchdog.CRASH_STATE_PATH", state_path):
                results = _restart_stalled_traders(report)

            assert "failed" in results["stonks"]
            assert "Connection refused" in results["stonks"]

    def test_restart_timeout(self, tmp_path):
        """SSH timeout returns error."""
        from src.d_state_watchdog import _restart_stalled_traders
        state_path = str(tmp_path / "crash-recovery.json")
        report = self._make_report(d_state=["stonks"], ok=["kairos", "aldridge"])
        with patch("src.d_state_watchdog.subprocess.run") as mock_run:
            import subprocess
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=30)

            with patch("src.d_state_watchdog.CRASH_STATE_PATH", state_path):
                results = _restart_stalled_traders(report)

            assert "SSH timeout" in results["stonks"]

    def test_ssh_not_found(self, tmp_path):
        """ssh binary not available."""
        from src.d_state_watchdog import _restart_stalled_traders
        state_path = str(tmp_path / "crash-recovery.json")
        report = self._make_report(d_state=["stonks"], ok=["kairos", "aldridge"])
        with patch("src.d_state_watchdog.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("ssh")

            with patch("src.d_state_watchdog.CRASH_STATE_PATH", state_path):
                results = _restart_stalled_traders(report)

            assert "ssh not found" in results["stonks"]

    def test_multiple_d_state_single_restart(self, tmp_path):
        """Multiple D-state traders → single gateway restart, all marked restarted."""
        from src.d_state_watchdog import _restart_stalled_traders
        state_path = str(tmp_path / "crash-recovery.json")
        report = self._make_report(
            d_state=["stonks", "kairos"],
            ok=["aldridge"],
        )
        with patch("src.d_state_watchdog.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            with patch("src.d_state_watchdog.CRASH_STATE_PATH", state_path):
                results = _restart_stalled_traders(report)

            assert results["stonks"] == "restarted"
            assert results["kairos"] == "restarted"
            assert "aldridge" not in results
            # Only one SSH call
            assert mock_run.call_count == 1

    def test_main_with_restart_flag(self):
        """main() with --restart triggers gateway restart when D-state."""
        from src.d_state_watchdog import main
        with patch("src.d_state_watchdog.check_all_traders") as mock_check, \
             patch("src.d_state_watchdog._restart_stalled_traders") as mock_restart:
            now = datetime.now(timezone.utc)
            mock_report = WatchdogReport(timestamp=now)
            mock_report.ok_traders = 2
            mock_report.d_state_traders = 1
            mock_report.checked_traders = 3
            mock_report.traders = [
                TraderStatus(trader_id="kairos", is_active=True,
                             last_heartbeat=now, severity="ok"),
                TraderStatus(trader_id="stonks", is_active=True,
                             severity="critical", is_d_state=True,
                             error="No activity"),
                TraderStatus(trader_id="aldridge", is_active=True,
                             last_heartbeat=now, severity="ok"),
            ]
            mock_check.return_value = mock_report
            mock_restart.return_value = {"stonks": "restarted"}

            with patch.object(sys, "argv", [
                "d_state_watchdog.py", "--restart", "--quiet"
            ]):
                exit_code = main()
                assert exit_code == 1  # still reports D-state
                mock_restart.assert_called_once()

    def test_main_with_restart_no_alerts_no_call(self):
        """main() with --restart but all healthy → no restart call."""
        from src.d_state_watchdog import main
        with patch("src.d_state_watchdog.check_all_traders") as mock_check, \
             patch("src.d_state_watchdog._restart_stalled_traders") as mock_restart:
            now = datetime.now(timezone.utc)
            mock_report = WatchdogReport(timestamp=now)
            mock_report.ok_traders = 3
            mock_report.checked_traders = 3
            mock_report.traders = [
                TraderStatus(trader_id="kairos", is_active=True,
                             last_heartbeat=now, severity="ok"),
                TraderStatus(trader_id="aldridge", is_active=True,
                             last_heartbeat=now, severity="ok"),
                TraderStatus(trader_id="stonks", is_active=True,
                             last_heartbeat=now, severity="ok"),
            ]
            mock_check.return_value = mock_report

            with patch.object(sys, "argv", [
                "d_state_watchdog.py", "--restart", "--quiet"
            ]):
                exit_code = main()
                assert exit_code == 0
                mock_restart.assert_not_called()


# ── Crash Counter ──────────────────────────────────────────────────────────


class TestCrashRecovery:
    """Tests for CrashRecovery — crash counter and pause threshold."""

    @pytest.fixture
    def temp_state(self, tmp_path):
        """Create a temporary crash state file path."""
        return str(tmp_path / "crash-recovery.json")

    def test_new_trader_starts_clean(self, temp_state):
        """A new trader has 0 crashes and is not paused."""
        from src.d_state_watchdog import CrashRecovery
        cr = CrashRecovery(state_path=temp_state)
        status = cr.get_status("kairos")
        assert status["crash_count"] == 0
        assert status["is_paused"] is False
        assert status["first_crash"] is None
        assert status["last_crash"] is None

    def test_record_crash_increments(self, temp_state):
        """record_crash increments the counter."""
        from src.d_state_watchdog import CrashRecovery
        cr = CrashRecovery(state_path=temp_state)

        cr.record_crash("kairos")
        assert cr.get_status("kairos")["crash_count"] == 1

        cr.record_crash("kairos")
        assert cr.get_status("kairos")["crash_count"] == 2

    def test_record_crash_sets_timestamps(self, temp_state):
        """record_crash sets first_crash and last_crash."""
        from src.d_state_watchdog import CrashRecovery
        cr = CrashRecovery(state_path=temp_state)

        cr.record_crash("kairos")
        status = cr.get_status("kairos")
        assert status["first_crash"] is not None
        assert status["last_crash"] is not None
        assert status["first_crash"] == status["last_crash"]  # same for first crash

    def test_pause_at_limit(self, temp_state):
        """Trader pauses when crash count reaches CRASH_LIMIT."""
        from src.d_state_watchdog import CrashRecovery, CRASH_LIMIT
        cr = CrashRecovery(state_path=temp_state)

        # Record crashes up to limit
        for i in range(CRASH_LIMIT - 1):
            cr.record_crash("kairos")
            assert cr.is_paused("kairos") is False

        # This one should hit the limit
        cr.record_crash("kairos")
        assert cr.is_paused("kairos") is True
        status = cr.get_status("kairos")
        assert status["crash_count"] == CRASH_LIMIT
        assert status["paused_at"] is not None

    def test_paused_trader_not_restarted(self, temp_state):
        """A paused trader is skipped during restart."""
        from src.d_state_watchdog import CrashRecovery, _restart_stalled_traders

        # Pre-set paused state
        with open(temp_state, "w") as f:
            json.dump({
                "kairos": {
                    "crash_count": 5,
                    "first_crash": datetime.now(timezone.utc).isoformat(),
                    "last_crash": datetime.now(timezone.utc).isoformat(),
                    "last_restart": None,
                    "is_paused": True,
                    "paused_at": datetime.now(timezone.utc).isoformat(),
                }
            }, f)

        from src.d_state_watchdog import CrashRecovery

        now = datetime.now(timezone.utc)
        report = WatchdogReport(timestamp=now)
        report.traders = [
            TraderStatus(trader_id="kairos", is_active=True,
                         severity="critical", is_d_state=True),
            TraderStatus(trader_id="aldridge", is_active=True,
                         last_heartbeat=now, severity="ok"),
        ]
        report.checked_traders = 2
        report.d_state_traders = 1
        report.ok_traders = 1

        # Patch the CrashRecovery to use our temp file
        with patch("src.d_state_watchdog.CRASH_STATE_PATH", temp_state):
            results = _restart_stalled_traders(report)

        assert results["kairos"] == "paused"
        assert "aldridge" not in results

    def test_unpause_resets_counter(self, temp_state):
        """Unpausing resets crash count to 0."""
        from src.d_state_watchdog import CrashRecovery
        cr = CrashRecovery(state_path=temp_state)

        # Crash to limit
        for _ in range(5):
            cr.record_crash("kairos")
        assert cr.is_paused("kairos") is True

        # Unpause
        was_paused = cr.unpause("kairos")
        assert was_paused is True
        assert cr.is_paused("kairos") is False
        status = cr.get_status("kairos")
        assert status["crash_count"] == 0
        assert status["first_crash"] is None

    def test_unpause_not_paused(self, temp_state):
        """Unpausing a never-paused trader returns False."""
        from src.d_state_watchdog import CrashRecovery
        cr = CrashRecovery(state_path=temp_state)
        was_paused = cr.unpause("kairos")
        assert was_paused is False

    def test_record_restart(self, temp_state):
        """record_restart sets last_restart timestamp."""
        from src.d_state_watchdog import CrashRecovery
        cr = CrashRecovery(state_path=temp_state)

        cr.record_crash("kairos")
        cr.record_restart("kairos")
        status = cr.get_status("kairos")
        assert status["last_restart"] is not None
        assert status["crash_count"] == 1  # doesn't reset crash count

    def test_window_reset(self, temp_state):
        """Crash counter resets when CRASH_WINDOW expires."""
        from src.d_state_watchdog import CrashRecovery, CRASH_WINDOW_SECONDS
        cr = CrashRecovery(state_path=temp_state)

        # Simulate a crash from long ago
        from datetime import timedelta
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=CRASH_WINDOW_SECONDS + 60)).isoformat()
        cr._state["kairos"] = {
            "crash_count": 3,
            "first_crash": old_time,
            "last_crash": old_time,
            "last_restart": None,
            "is_paused": False,
            "paused_at": None,
        }
        cr._save()

        # Next crash should reset the window first
        cr.record_crash("kairos")
        status = cr.get_status("kairos")
        # Counter should be 1 (reset + new crash), not 4
        assert status["crash_count"] == 1

    def test_get_all(self, temp_state):
        """get_all returns all traders."""
        from src.d_state_watchdog import CrashRecovery
        cr = CrashRecovery(state_path=temp_state)
        cr.record_crash("kairos")
        cr.record_crash("aldridge")

        all_status = cr.get_all()
        assert "kairos" in all_status
        assert "aldridge" in all_status
        assert all_status["kairos"]["crash_count"] == 1
        assert all_status["aldridge"]["crash_count"] == 1

    def test_persistence(self, temp_state):
        """Crash state persists to disk and survives re-read."""
        from src.d_state_watchdog import CrashRecovery
        cr1 = CrashRecovery(state_path=temp_state)
        cr1.record_crash("kairos")
        cr1.record_crash("kairos")

        # New instance reads from same file
        cr2 = CrashRecovery(state_path=temp_state)
        status = cr2.get_status("kairos")
        assert status["crash_count"] == 2

    def test_enrich_report_with_crash_state(self):
        """main() enriches report with crash state from file."""
        from src.d_state_watchdog import main
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "kairos": {
                    "crash_count": 3,
                    "first_crash": datetime.now(timezone.utc).isoformat(),
                    "last_crash": datetime.now(timezone.utc).isoformat(),
                    "last_restart": datetime.now(timezone.utc).isoformat(),
                    "is_paused": False,
                    "paused_at": None,
                }
            }, f)
            temp_path = f.name

        try:
            with patch("src.d_state_watchdog.check_all_traders") as mock_check:
                now = datetime.now(timezone.utc)
                mock_report = WatchdogReport(timestamp=now)
                mock_report.ok_traders = 3
                mock_report.checked_traders = 3
                mock_report.traders = [
                    TraderStatus(trader_id="kairos", is_active=True,
                                 last_heartbeat=now, severity="ok"),
                    TraderStatus(trader_id="aldridge", is_active=True,
                                 last_heartbeat=now, severity="ok"),
                    TraderStatus(trader_id="stonks", is_active=True,
                                 last_heartbeat=now, severity="ok"),
                ]
                mock_check.return_value = mock_report

                with patch.object(sys, "argv", [
                    "d_state_watchdog.py", "--quiet",
                    "--crash-state", temp_path
                ]):
                    exit_code = main()
                    assert exit_code == 0
                    # kairos should have crash_count=3 from file
                    kairos = [t for t in mock_report.traders if t.trader_id == "kairos"][0]
                    assert kairos.crash_count == 3
        finally:
            os.unlink(temp_path)

    def test_main_unpause(self):
        """main() with --unpause unpauses a trader."""
        from src.d_state_watchdog import main
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "kairos": {
                    "crash_count": 5,
                    "first_crash": datetime.now(timezone.utc).isoformat(),
                    "last_crash": datetime.now(timezone.utc).isoformat(),
                    "last_restart": None,
                    "is_paused": True,
                    "paused_at": datetime.now(timezone.utc).isoformat(),
                }
            }, f)
            temp_path = f.name

        try:
            with patch.object(sys, "argv", [
                "d_state_watchdog.py", "--unpause", "kairos",
                "--crash-state", temp_path
            ]):
                exit_code = main()
                assert exit_code == 0

            # Verify unpaused
            from src.d_state_watchdog import CrashRecovery
            cr = CrashRecovery(state_path=temp_path)
            assert cr.is_paused("kairos") is False
            assert cr.get_status("kairos")["crash_count"] == 0
        finally:
            os.unlink(temp_path)

    def test_main_unpause_all(self):
        """main() with --unpause (no arg) unpauses all traders."""
        from src.d_state_watchdog import main
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "kairos": {
                    "crash_count": 5,
                    "first_crash": datetime.now(timezone.utc).isoformat(),
                    "last_crash": datetime.now(timezone.utc).isoformat(),
                    "last_restart": None,
                    "is_paused": True,
                    "paused_at": datetime.now(timezone.utc).isoformat(),
                },
                "aldridge": {
                    "crash_count": 5,
                    "first_crash": datetime.now(timezone.utc).isoformat(),
                    "last_crash": datetime.now(timezone.utc).isoformat(),
                    "last_restart": None,
                    "is_paused": True,
                    "paused_at": datetime.now(timezone.utc).isoformat(),
                }
            }, f)
            temp_path = f.name

        try:
            with patch.object(sys, "argv", [
                "d_state_watchdog.py", "--unpause",
                "--crash-state", temp_path
            ]):
                exit_code = main()
                assert exit_code == 0

            # Verify both unpaused
            from src.d_state_watchdog import CrashRecovery
            cr = CrashRecovery(state_path=temp_path)
            assert cr.is_paused("kairos") is False
            assert cr.is_paused("aldridge") is False
        finally:
            os.unlink(temp_path)

    def test_restart_hits_limit_then_pauses(self):
        """After CRASH_LIMIT restarts, trader is paused and no further restarts happen."""
        from src.d_state_watchdog import _restart_stalled_traders, CRASH_LIMIT
        import tempfile
        import os

        # Create temp state file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            # Pre-crash to just below limit
            crash_data = {
                "kairos": {
                    "crash_count": CRASH_LIMIT - 1,
                    "first_crash": datetime.now(timezone.utc).isoformat(),
                    "last_crash": datetime.now(timezone.utc).isoformat(),
                    "last_restart": None,
                    "is_paused": False,
                    "paused_at": None,
                }
            }
            json.dump(crash_data, f)
            temp_path = f.name

        try:
            now = datetime.now(timezone.utc)
            report = WatchdogReport(timestamp=now)
            report.traders = [
                TraderStatus(trader_id="kairos", is_active=True,
                             severity="critical", is_d_state=True),
            ]
            report.checked_traders = 1
            report.d_state_traders = 1

            with patch("src.d_state_watchdog.CRASH_STATE_PATH", temp_path):
                results = _restart_stalled_traders(report)

            # Should be paused because record_crash pushed it to limit
            assert results["kairos"] == "paused"

            # Verify state file
            from src.d_state_watchdog import CrashRecovery
            cr = CrashRecovery(state_path=temp_path)
            status = cr.get_status("kairos")
            assert status["crash_count"] == CRASH_LIMIT
            assert status["is_paused"] is True
        finally:
            os.unlink(temp_path)


# ── TraderStatus crash fields ─────────────────────────────────────────────


class TestTraderStatusCrashFields:
    """TraderStatus includes crash recovery fields."""

    def test_default_crash_fields(self):
        """Default values for new crash fields."""
        ts = TraderStatus(trader_id="kairos")
        assert ts.crash_count == 0
        assert ts.last_restart is None
        assert ts.is_paused is False
        assert ts.paused_at is None

    def test_to_dict_includes_crash_fields(self):
        """to_dict includes crash recovery data."""
        now = datetime.now(timezone.utc)
        ts = TraderStatus(
            trader_id="kairos",
            crash_count=3,
            last_restart=now,
            is_paused=True,
            paused_at=now,
        )
        d = ts.to_dict()
        assert d["crash_count"] == 3
        assert d["last_restart"] is not None
        assert d["is_paused"] is True
        assert d["paused_at"] is not None
