#!/usr/bin/env python3
"""Unit tests for src/position_stream.py — the Alpaca websocket daemon that
gives Stan's held positions faster-than-5-min exit protection. No real
websocket connection or subprocess calls; everything network/subprocess-
shaped is monkeypatched, matching this repo's existing no-network convention
(see test_guardrails.py / conftest.py)."""
import collections
import json
import queue
import time

import pytest

from src import position_stream as ps


DEFAULT_PARAMS = {"risk": {"stop_loss_pct": -10.0, "profit_target_pct": 8.0}}


# ─────────────────────────────────────────────────────────────────────────
# Pure evaluation functions
# ─────────────────────────────────────────────────────────────────────────


class TestHardStopPrice:
    def test_ten_percent_stop(self):
        assert ps.hard_stop_price(100.0, DEFAULT_PARAMS) == pytest.approx(90.0)

    def test_defaults_when_params_missing(self):
        assert ps.hard_stop_price(100.0, {}) == pytest.approx(90.0)


class TestProfitTargetGuidePrice:
    def test_eight_percent_target(self):
        assert ps.profit_target_guide_price(100.0, DEFAULT_PARAMS) == pytest.approx(108.0)


class TestWithinLocalStopProximity:
    def test_already_breached(self):
        assert ps.within_local_stop_proximity(89.0, 100.0, DEFAULT_PARAMS) is True

    def test_exactly_at_stop(self):
        assert ps.within_local_stop_proximity(90.0, 100.0, DEFAULT_PARAMS) is True

    def test_within_proximity_band(self):
        # stop is 90.0, 1.5% proximity band -> up to 91.35 counts
        assert ps.within_local_stop_proximity(91.0, 100.0, DEFAULT_PARAMS, proximity_pct=1.5) is True

    def test_outside_proximity_band(self):
        assert ps.within_local_stop_proximity(95.0, 100.0, DEFAULT_PARAMS, proximity_pct=1.5) is False

    def test_zero_entry_price_is_safe(self):
        assert ps.within_local_stop_proximity(50.0, 0.0, DEFAULT_PARAMS) is False


class TestWithinProfitTargetGuideProximity:
    def test_at_target(self):
        assert ps.within_profit_target_guide_proximity(108.0, 100.0, DEFAULT_PARAMS) is True

    def test_just_below_proximity_band(self):
        # target 108.0, 1% band -> from 106.92 counts
        assert ps.within_profit_target_guide_proximity(107.0, 100.0, DEFAULT_PARAMS, proximity_pct=1.0) is True

    def test_well_below_target(self):
        assert ps.within_profit_target_guide_proximity(102.0, 100.0, DEFAULT_PARAMS) is False


class TestDetectAdverseMove:
    def test_no_move_returns_none(self):
        history = collections.deque([(0.0, 100.0), (60.0, 100.5)])
        assert ps.detect_adverse_move(history, window_seconds=120, move_pct=3.0) is None

    def test_sharp_drop_within_window_detected(self):
        history = collections.deque([(0.0, 100.0), (60.0, 96.0)])
        move = ps.detect_adverse_move(history, window_seconds=120, move_pct=3.0)
        assert move is not None
        assert move["change_pct"] == pytest.approx(-4.0)

    def test_drop_outside_window_ignored(self):
        # window_start_price is picked from the first sample >= cutoff; an
        # old sample outside the window shouldn't be used as the baseline.
        history = collections.deque([(0.0, 200.0), (100.0, 100.0), (110.0, 99.0)])
        move = ps.detect_adverse_move(history, window_seconds=20, move_pct=3.0)
        assert move is None  # 100.0 -> 99.0 is only -1%

    def test_single_sample_returns_none(self):
        history = collections.deque([(0.0, 100.0)])
        assert ps.detect_adverse_move(history) is None

    def test_upward_move_not_flagged(self):
        history = collections.deque([(0.0, 100.0), (60.0, 110.0)])
        assert ps.detect_adverse_move(history, window_seconds=120, move_pct=3.0) is None


class TestEvaluateWatch:
    def test_price_gte_triggers(self):
        watch = {"field": "price", "comparator": ">=", "value": 45.0}
        assert ps.evaluate_watch(watch, 45.5, None) is True

    def test_price_gte_not_yet(self):
        watch = {"field": "price", "comparator": ">=", "value": 45.0}
        assert ps.evaluate_watch(watch, 44.0, None) is False

    def test_price_lte_triggers(self):
        watch = {"field": "price", "comparator": "<=", "value": 28.50}
        assert ps.evaluate_watch(watch, 28.0, None) is True

    def test_pnl_pct_needs_position(self):
        watch = {"field": "pnl_pct", "comparator": "<=", "value": -5.0}
        assert ps.evaluate_watch(watch, 90.0, None) is False

    def test_pnl_pct_computed_from_entry(self):
        watch = {"field": "pnl_pct", "comparator": "<=", "value": -5.0}
        position = {"avg_entry_price": "100.0"}
        assert ps.evaluate_watch(watch, 94.0, position) is True  # -6%
        assert ps.evaluate_watch(watch, 96.0, position) is False  # -4%

    def test_unknown_field_never_triggers(self):
        watch = {"field": "macdh", "comparator": ">=", "value": 0.0}
        assert ps.evaluate_watch(watch, 100.0, None) is False


# ─────────────────────────────────────────────────────────────────────────
# another_trader_stonks_run_active — cross-mechanism concurrency guard
# ─────────────────────────────────────────────────────────────────────────


class FakeCompletedProcess:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class TestAnotherTraderStonksRunActive:
    def test_no_running_tasks_returns_false(self, monkeypatch):
        monkeypatch.setattr(ps.subprocess, "run",
                             lambda *a, **k: FakeCompletedProcess(json.dumps({"tasks": []})))
        assert ps.another_trader_stonks_run_active() is False

    def test_unrelated_running_task_returns_false(self, monkeypatch):
        tasks = {"tasks": [{"agentId": "trader-aldridge", "childSessionKey": "agent:trader-aldridge:main"}]}
        monkeypatch.setattr(ps.subprocess, "run", lambda *a, **k: FakeCompletedProcess(json.dumps(tasks)))
        assert ps.another_trader_stonks_run_active() is False

    def test_trader_stonks_running_task_returns_true(self, monkeypatch):
        tasks = {"tasks": [{"agentId": "trader-stonks", "childSessionKey": "agent:trader-stonks:cron:xyz"}]}
        monkeypatch.setattr(ps.subprocess, "run", lambda *a, **k: FakeCompletedProcess(json.dumps(tasks)))
        assert ps.another_trader_stonks_run_active() is True

    def test_subprocess_timeout_fails_closed(self, monkeypatch):
        def _raise(*a, **k):
            raise ps.subprocess.TimeoutExpired(cmd="openclaw", timeout=15)
        monkeypatch.setattr(ps.subprocess, "run", _raise)
        assert ps.another_trader_stonks_run_active() is True

    def test_nonzero_returncode_fails_closed(self, monkeypatch):
        monkeypatch.setattr(ps.subprocess, "run", lambda *a, **k: FakeCompletedProcess("", returncode=1))
        assert ps.another_trader_stonks_run_active() is True

    def test_malformed_json_fails_closed(self, monkeypatch):
        monkeypatch.setattr(ps.subprocess, "run", lambda *a, **k: FakeCompletedProcess("not json"))
        assert ps.another_trader_stonks_run_active() is True


# ─────────────────────────────────────────────────────────────────────────
# DeliveryWorker — strict serialization
# ─────────────────────────────────────────────────────────────────────────


class TestDeliveryWorker:
    def test_items_handled_in_order(self, monkeypatch):
        seen = []
        monkeypatch.setattr(ps.DeliveryWorker, "_handle", lambda self, item: seen.append(item["n"]))
        worker = ps.DeliveryWorker()
        worker.start()
        for n in range(5):
            worker.push({"kind": "noop", "n": n})
        worker.stop()
        worker._thread.join(timeout=5)
        assert seen == [0, 1, 2, 3, 4]

    def test_never_overlaps(self, monkeypatch):
        """Each handled item sleeps briefly; if two ever ran concurrently,
        an in-flight counter would exceed 1."""
        in_flight = []
        max_in_flight = []

        def slow_handle(self, item):
            in_flight.append(1)
            max_in_flight.append(len(in_flight))
            time.sleep(0.02)
            in_flight.pop()

        monkeypatch.setattr(ps.DeliveryWorker, "_handle", slow_handle)
        worker = ps.DeliveryWorker()
        worker.start()
        for n in range(4):
            worker.push({"kind": "noop", "n": n})
        worker.stop()
        worker._thread.join(timeout=5)
        assert max(max_in_flight) == 1

    def test_exception_in_handler_does_not_kill_worker(self, monkeypatch):
        calls = []

        def flaky_handle(self, item):
            if item["n"] == 0:
                raise RuntimeError("boom")
            calls.append(item["n"])

        monkeypatch.setattr(ps.DeliveryWorker, "_handle", flaky_handle)
        worker = ps.DeliveryWorker()
        worker.start()
        worker.push({"kind": "noop", "n": 0})
        worker.push({"kind": "noop", "n": 1})
        worker.stop()
        worker._thread.join(timeout=5)
        assert calls == [1]

    def test_unknown_kind_does_not_raise(self):
        worker = ps.DeliveryWorker()
        worker._handle({"kind": "totally_unknown"})  # should just log, not raise


class TestDeliveryWorkerHandlers:
    def test_mechanical_check_stops_skips_when_another_run_active(self, monkeypatch):
        monkeypatch.setattr(ps, "another_trader_stonks_run_active", lambda: True)
        called = []
        monkeypatch.setattr(ps, "run_executor", lambda *a, **k: called.append(a) or {})
        worker = ps.DeliveryWorker()
        worker._handle_mechanical_check_stops({"ticker": "IP"})
        assert called == []

    def test_mechanical_check_stops_executes_breach(self, monkeypatch):
        monkeypatch.setattr(ps, "another_trader_stonks_run_active", lambda: False)
        calls = []

        def fake_run_executor(*args, **kwargs):
            calls.append(args)
            if "check-stops" in args:
                return {"breaches": [{"ticker": "IP", "reason": "hard stop", "shares_to_sell": None}]}
            return {"ok": True}

        monkeypatch.setattr(ps, "run_executor", fake_run_executor)
        monkeypatch.setattr(ps, "fetch_held_positions", lambda: {"IP": {"qty": "3"}})
        recorded = []
        monkeypatch.setattr(ps, "record_decision", lambda *a, **k: recorded.append(a))

        worker = ps.DeliveryWorker()
        worker._handle_mechanical_check_stops({"ticker": "IP"})

        sell_call = [c for c in calls if "SELL" in c]
        assert sell_call, f"expected a SELL call, got {calls}"
        assert "3" in sell_call[0]
        assert recorded  # decision was logged

    def test_watch_triggered_alert_dispatches_and_clears(self, monkeypatch):
        monkeypatch.setattr(ps, "another_trader_stonks_run_active", lambda: False)
        cleared = []
        monkeypatch.setattr(ps, "clear_watch", lambda wid: cleared.append(wid))
        alerts = []
        monkeypatch.setattr(ps.subprocess, "run", lambda *a, **k: alerts.append(a) or FakeCompletedProcess())

        worker = ps.DeliveryWorker()
        watch = {"id": "abc123", "ticker": "F", "action": "ALERT", "field": "price",
                 "comparator": "<=", "value": 13.5, "reason": "watching for a dip"}
        worker._handle_watch_triggered({"watch": watch, "current_price": 13.4})

        assert cleared == ["abc123"]
        assert alerts  # openclaw agent call was made

    def test_watch_triggered_sell_all_resolves_qty(self, monkeypatch):
        monkeypatch.setattr(ps, "another_trader_stonks_run_active", lambda: False)
        monkeypatch.setattr(ps, "fetch_held_positions", lambda: {"IP": {"qty": "7"}})
        calls = []
        monkeypatch.setattr(ps, "run_executor", lambda *a, **k: calls.append(a) or {"ok": True})
        monkeypatch.setattr(ps, "record_decision", lambda *a, **k: None)
        monkeypatch.setattr(ps, "clear_watch", lambda wid: None)

        worker = ps.DeliveryWorker()
        watch = {"id": "w1", "ticker": "IP", "action": "SELL", "qty": "all",
                 "field": "price", "comparator": ">=", "value": 45.0, "reason": "target hit"}
        worker._handle_watch_triggered({"watch": watch, "current_price": 45.5})

        assert calls and "7" in calls[0]

    def test_watch_triggered_buy_passes_conviction_and_sector(self, monkeypatch):
        monkeypatch.setattr(ps, "another_trader_stonks_run_active", lambda: False)
        calls = []
        monkeypatch.setattr(ps, "run_executor", lambda *a, **k: calls.append(a) or {"ok": True})
        monkeypatch.setattr(ps, "record_decision", lambda *a, **k: None)
        monkeypatch.setattr(ps, "clear_watch", lambda wid: None)

        worker = ps.DeliveryWorker()
        watch = {"id": "w2", "ticker": "BFST", "action": "BUY", "qty": "3", "conviction": 0.65,
                  "sector": "Financials", "field": "price", "comparator": "<=", "value": 28.5,
                  "reason": "dip buy"}
        worker._handle_watch_triggered({"watch": watch, "current_price": 28.4})

        assert calls
        assert "--conviction" in calls[0] and "0.65" in calls[0]
        assert "Financials" in calls[0]


# ─────────────────────────────────────────────────────────────────────────
# PositionStreamManager
# ─────────────────────────────────────────────────────────────────────────


class FakeStream:
    def __init__(self):
        self.subscribed = set()
        self.subscribe_calls = []
        self.unsubscribe_calls = []

    def subscribe_trades(self, handler, *symbols):
        self.subscribe_calls.append(symbols)
        self.subscribed.update(symbols)

    def unsubscribe_trades(self, *symbols):
        self.unsubscribe_calls.append(symbols)
        self.subscribed.difference_update(symbols)

    def run(self):
        pass

    def stop(self):
        pass


class TestResyncSubscriptions:
    def test_subscribes_new_positions(self, monkeypatch):
        monkeypatch.setattr(ps, "fetch_held_positions", lambda: {"IP": {}, "F": {}})
        mgr = ps.PositionStreamManager(stream_factory=lambda k, s: FakeStream())
        mgr._stream = FakeStream()
        mgr._resync_subscriptions()
        assert mgr._subscribed == {"IP", "F"}
        assert mgr._stream.subscribe_calls == [("F", "IP")]

    def test_unsubscribes_closed_positions(self, monkeypatch):
        mgr = ps.PositionStreamManager(stream_factory=lambda k, s: FakeStream())
        mgr._stream = FakeStream()
        mgr._subscribed = {"IP", "F"}
        mgr._price_history["IP"] = collections.deque()
        monkeypatch.setattr(ps, "fetch_held_positions", lambda: {"F": {}})
        mgr._resync_subscriptions()
        assert mgr._subscribed == {"F"}
        assert mgr._stream.unsubscribe_calls == [("IP",)]
        assert "IP" not in mgr._price_history


class TestHandlePriceUpdate:
    def test_local_stop_proximity_pushes_mechanical_check(self, monkeypatch):
        monkeypatch.setattr(ps, "load_params", lambda: DEFAULT_PARAMS)
        monkeypatch.setattr(ps, "fetch_held_positions", lambda: {"IP": {"avg_entry_price": "100.0"}})
        monkeypatch.setattr(ps, "load_active_watches", lambda: [])

        mgr = ps.PositionStreamManager(stream_factory=lambda k, s: FakeStream())
        pushed = []
        mgr._worker.push = lambda item: pushed.append(item)

        mgr.handle_price_update("IP", 90.5, now=1000.0)

        kinds = [p["kind"] for p in pushed]
        assert "mechanical_check_stops" in kinds

    def test_profit_target_guide_proximity_pushes_llm_alert(self, monkeypatch):
        monkeypatch.setattr(ps, "load_params", lambda: DEFAULT_PARAMS)
        monkeypatch.setattr(ps, "fetch_held_positions", lambda: {"IP": {"avg_entry_price": "100.0"}})
        monkeypatch.setattr(ps, "load_active_watches", lambda: [])

        mgr = ps.PositionStreamManager(stream_factory=lambda k, s: FakeStream())
        pushed = []
        mgr._worker.push = lambda item: pushed.append(item)

        mgr.handle_price_update("IP", 108.0, now=1000.0)

        kinds = [p["kind"] for p in pushed]
        assert "llm_alert" in kinds

    def test_cooldown_prevents_repeat_push(self, monkeypatch):
        monkeypatch.setattr(ps, "load_params", lambda: DEFAULT_PARAMS)
        monkeypatch.setattr(ps, "fetch_held_positions", lambda: {"IP": {"avg_entry_price": "100.0"}})
        monkeypatch.setattr(ps, "load_active_watches", lambda: [])

        mgr = ps.PositionStreamManager(stream_factory=lambda k, s: FakeStream())
        pushed = []
        mgr._worker.push = lambda item: pushed.append(item)

        mgr.handle_price_update("IP", 90.5, now=1000.0)
        mgr.handle_price_update("IP", 90.5, now=1000.0 + ps.RECHECK_COOLDOWN_SECONDS / 2)
        assert len(pushed) == 1  # second call inside the cooldown window is suppressed

    def test_watch_match_pushes_watch_triggered(self, monkeypatch):
        monkeypatch.setattr(ps, "load_params", lambda: DEFAULT_PARAMS)
        monkeypatch.setattr(ps, "fetch_held_positions", lambda: {"IP": {"avg_entry_price": "100.0"}})
        watch = {"id": "w1", "ticker": "IP", "field": "price", "comparator": ">=", "value": 105.0,
                 "action": "SELL", "qty": "all", "reason": "test"}
        monkeypatch.setattr(ps, "load_active_watches", lambda: [watch])

        mgr = ps.PositionStreamManager(stream_factory=lambda k, s: FakeStream())
        pushed = []
        mgr._worker.push = lambda item: pushed.append(item)

        # price 106 is well clear of both stop and target-guide proximity, so
        # only the watch should fire.
        mgr.handle_price_update("IP", 106.0, now=1000.0)

        watch_pushes = [p for p in pushed if p["kind"] == "watch_triggered"]
        assert len(watch_pushes) == 1
        assert watch_pushes[0]["watch"]["id"] == "w1"

    def test_watch_for_different_ticker_ignored(self, monkeypatch):
        monkeypatch.setattr(ps, "load_params", lambda: DEFAULT_PARAMS)
        monkeypatch.setattr(ps, "fetch_held_positions", lambda: {"IP": {"avg_entry_price": "100.0"}})
        watch = {"id": "w1", "ticker": "F", "field": "price", "comparator": ">=", "value": 10.0,
                 "action": "ALERT", "reason": "test"}
        monkeypatch.setattr(ps, "load_active_watches", lambda: [watch])

        mgr = ps.PositionStreamManager(stream_factory=lambda k, s: FakeStream())
        pushed = []
        mgr._worker.push = lambda item: pushed.append(item)

        mgr.handle_price_update("IP", 106.0, now=1000.0)

        assert not [p for p in pushed if p["kind"] == "watch_triggered"]
