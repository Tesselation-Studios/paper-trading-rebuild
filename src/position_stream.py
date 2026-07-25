"""Alpaca websocket client for real-time exit protection on Stan's
(trader-stonks) held positions — event-driven, instead of waiting for the
next 5-min stonks-tick cron.

Scope: held positions only (not watchlist/universe) — protecting capital
already at risk, not sourcing new ideas. Two delivery paths:

  1. Mechanical fast path (hard-stop/trailing-stop/oversized breaches): no
     LLM in the loop at all. executor.py's check_stops()/check_order() are
     already fully deterministic — this just triggers them faster than the
     5-min cadence allows, and executes any real breach via executor.py's
     normal guardrail-gated CLI, exactly as a tick would.
  2. LLM-judgment path (profit-target *guide* proximity, a sharp-but-not-
     breaching adverse move worth a second look, or Stan's own custom watch
     conditions marked action=ALERT): shells out to
     `openclaw agent --agent trader-stonks --message ...`, same mechanism
     used reliably everywhere else this session. Deliberately NOT routed
     through OpenClaw's /hooks/agent webhook — confirmed empirically
     (2026-07-24) that it can silently drop a request under concurrent load
     with no documented delivery guarantee anywhere.

Stan's own custom watches (scripts/set_watch.py, in the trader-stonks repo)
are also evaluated here — structured, no-eval conditions Stan pre-authorizes
(SELL/BUY/ALERT), executed mechanically without a fresh LLM judgment at
trigger time, one-shot, always through executor.py's gated CLI.

Delivery is a single-threaded local worker (one queue, one thread) — no HTTP,
no separate dispatcher service. Serialized by construction: the worker pops
one item and runs its subprocess call to completion before popping the next,
so this daemon's own alerts can never race each other. It CANNOT prevent a
race against the separately-scheduled stonks-tick cron (a different process,
dispatching via its own internal sessions_spawn) — every execution path here
checks session_status/sessions_list for another active trader-stonks run
first and backs off (logs, doesn't double-act) if one is found.

Started as a background thread from data_bus.py's app init, matching its
existing daemon-thread pattern (RSS collector, cache warmup) — see
_HAS_POSITION_STREAM / start_position_stream() below.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("position_stream")

# ── Paths into the trader-stonks workspace (separate repo) ────────────────
# Always shell out to its scripts (executor.py, set_watch.py,
# record_decision.py) rather than importing their internals directly, except
# for set_watch's own read-only helpers (prune_expired/load_watches), which
# have no side effects worth avoiding a subprocess for — every WRITE to
# watches.json still goes through the CLI so its flock actually protects
# concurrent access from both this daemon and Stan's tick process.
STONKS_WORKSPACE = Path.home() / ".openclaw" / "workspace-trader-stonks"
STONKS_SCRIPTS_DIR = STONKS_WORKSPACE / "scripts"
EXECUTOR_SCRIPT = STONKS_SCRIPTS_DIR / "executor.py"
SET_WATCH_SCRIPT = STONKS_SCRIPTS_DIR / "set_watch.py"
RECORD_DECISION_SCRIPT = STONKS_SCRIPTS_DIR / "record_decision.py"
PARAMS_PATH = STONKS_WORKSPACE / "params.json"

sys.path.insert(0, str(STONKS_SCRIPTS_DIR))
try:
    import set_watch  # noqa: E402
    _HAS_SET_WATCH = True
except ImportError:
    set_watch = None  # type: ignore
    _HAS_SET_WATCH = False
    log.warning("set_watch module not importable — custom watch evaluation disabled")

ACCOUNT = "stonks"
AGENT_ID = "trader-stonks"

# ── Tunables — same convention as bankroll.py's SCALE_IN_* constants ──────
POSITION_POLL_INTERVAL_SECONDS = 45
# Only bother with a real executor.py round trip (network + subprocess cost)
# once local math says price is plausibly close to a fixed-stop threshold —
# this is a pre-filter, never the authoritative breach decision.
LOCAL_STOP_PROXIMITY_PCT = 1.5
# Per-ticker, per-check-kind cooldown so oscillation near a threshold can't
# spam repeated executor.py calls or repeated LLM alerts.
RECHECK_COOLDOWN_SECONDS = 30
# A move sharper than this within the window is worth flagging to Stan even
# if it doesn't breach a mechanical stop — thesis-breaking-news territory.
ADVERSE_MOVE_PCT = 3.0
ADVERSE_MOVE_WINDOW_SECONDS = 120
PRICE_HISTORY_MAXLEN = 200
# Within this % of the profit_target_pct *guide* (params.json, not a hard
# exit) is worth a judgment call, not a mechanical sell.
PROFIT_TARGET_PROXIMITY_PCT = 1.0


def load_params() -> Dict[str, Any]:
    try:
        return json.loads(PARAMS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def get_alpaca_credentials() -> tuple[Optional[str], Optional[str]]:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path.home() / ".openclaw" / ".env", override=True)
    except ImportError:
        pass
    return os.getenv("ALPACA_STONKS_KEY"), os.getenv("ALPACA_STONKS_SECRET")


def run_executor(*args: str, timeout: int = 30) -> Optional[Dict[str, Any]]:
    """Shell out to executor.py — never re-implement order placement or
    guardrail logic here. Same invocation shape Stan's own ticks use."""
    cmd = ["python3", str(EXECUTOR_SCRIPT), "--account", ACCOUNT, *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error("executor.py timed out: %s", args)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("executor.py non-JSON output for %s: stdout=%r stderr=%r",
                   args, result.stdout[:500], result.stderr[:500])
        return None


def fetch_held_positions() -> Dict[str, Dict[str, Any]]:
    """ticker -> position dict (avg_entry_price, qty, current_price, ...)."""
    status = run_executor("--action", "status")
    if not status or "positions" not in status:
        return {}
    return {p["symbol"].upper(): p for p in status["positions"]}


def load_active_watches() -> List[Dict[str, Any]]:
    if not _HAS_SET_WATCH:
        return []
    return set_watch.prune_expired(set_watch.load_watches())


def clear_watch(watch_id: str) -> None:
    """One-shot — always remove a watch through the CLI so its flock
    protects against Stan's tick process adding one at the same moment."""
    try:
        subprocess.run(["python3", str(SET_WATCH_SCRIPT), "clear", "--id", watch_id],
                        capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        log.error("set_watch.py clear timed out for watch %s", watch_id)


def record_decision(ticker: str, action: str, rationale: str, conviction: float = 0.0) -> None:
    try:
        subprocess.run(
            ["python3", str(RECORD_DECISION_SCRIPT), "decision",
             "--trader-id", ACCOUNT, "--ticker", ticker, "--action", action,
             "--rationale", rationale, "--conviction", str(conviction)],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        log.error("record_decision.py timed out for %s %s", ticker, action)


def another_trader_stonks_run_active() -> bool:
    """Cross-mechanism concurrency guard: this daemon's own queue serializes
    its own alerts against each other, but can't see the independently-
    scheduled stonks-tick cron, which dispatches via its own internal
    sessions_spawn (a different process entirely). Check before any
    execution so a mechanical action here never races a live tick's own
    SELL/BUY on the same position.

    `openclaw tasks list` has no --agent filter, so this pulls running tasks
    and inspects agentId/childSessionKey/requesterAgentId itself — same
    fields confirmed present in real `openclaw tasks list --json` output
    while diagnosing the /hooks/agent drop bug earlier this session."""
    try:
        result = subprocess.run(
            ["openclaw", "tasks", "list", "--status", "running", "--json"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        # Fail closed: if we can't check, don't risk a double-action.
        log.warning("could not check for an active trader-stonks run (%s) — treating as active, skipping", e)
        return True
    if result.returncode != 0:
        log.warning("tasks list check failed (rc=%s) — treating as active, skipping", result.returncode)
        return True
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return True
    tasks = data.get("tasks", data if isinstance(data, list) else [])
    return any(AGENT_ID in json.dumps(t) for t in tasks)


# ── Pure evaluation functions — no I/O, fully unit-testable ───────────────


def hard_stop_price(entry_price: float, params: Dict[str, Any]) -> float:
    pct = abs(float(params.get("risk", {}).get("stop_loss_pct", -10.0)))
    return entry_price * (1 - pct / 100)


def profit_target_guide_price(entry_price: float, params: Dict[str, Any]) -> float:
    pct = float(params.get("risk", {}).get("profit_target_pct", 8.0))
    return entry_price * (1 + pct / 100)


def within_local_stop_proximity(current_price: float, entry_price: float, params: Dict[str, Any],
                                 proximity_pct: float = LOCAL_STOP_PROXIMITY_PCT) -> bool:
    """Cheap local pre-filter — never the authoritative breach decision
    (that's always executor.py's real check_stops()). True if price has
    already crossed the hard stop, or is within proximity_pct above it —
    just decides whether it's worth spending a real round trip right now."""
    stop_price = hard_stop_price(entry_price, params)
    if stop_price <= 0:
        return False
    if current_price <= stop_price:
        return True
    distance_pct = (current_price - stop_price) / stop_price * 100
    return distance_pct <= proximity_pct


def within_profit_target_guide_proximity(current_price: float, entry_price: float, params: Dict[str, Any],
                                          proximity_pct: float = PROFIT_TARGET_PROXIMITY_PCT) -> bool:
    target_price = profit_target_guide_price(entry_price, params)
    if target_price <= 0:
        return False
    return current_price >= target_price * (1 - proximity_pct / 100)


def detect_adverse_move(price_history: collections.deque, window_seconds: float = ADVERSE_MOVE_WINDOW_SECONDS,
                         move_pct: float = ADVERSE_MOVE_PCT) -> Optional[Dict[str, Any]]:
    """price_history: deque of (timestamp, price) tuples, oldest first.
    Returns a dict describing the move if a sharp adverse move happened
    within the window, else None."""
    if len(price_history) < 2:
        return None
    now_ts, now_price = price_history[-1]
    cutoff = now_ts - window_seconds
    window_start_price = None
    for ts, price in price_history:
        if ts >= cutoff:
            window_start_price = price
            break
    if window_start_price is None or window_start_price <= 0:
        return None
    change_pct = (now_price - window_start_price) / window_start_price * 100
    if change_pct <= -move_pct:
        return {"change_pct": change_pct, "window_seconds": window_seconds,
                "from_price": window_start_price, "to_price": now_price}
    return None


def evaluate_watch(watch: Dict[str, Any], current_price: float, position: Optional[Dict[str, Any]]) -> bool:
    """True if this watch's condition is currently met. field is restricted
    to price/pnl_pct — a structured comparison, never eval'd code."""
    if watch["field"] == "price":
        observed = current_price
    elif watch["field"] == "pnl_pct":
        if not position or not position.get("avg_entry_price"):
            return False
        entry = float(position["avg_entry_price"])
        observed = (current_price - entry) / entry * 100 if entry else 0.0
    else:
        return False

    value = float(watch["value"])
    if watch["comparator"] == ">=":
        return observed >= value
    if watch["comparator"] == "<=":
        return observed <= value
    return False


# ── Delivery worker — single thread, one queue, strictly serialized ───────


class DeliveryWorker:
    """One background thread pops items and runs them to completion before
    popping the next — this daemon's own triggers can never race each
    other by construction. Each item type checks
    another_trader_stonks_run_active() before executing anything, guarding
    against the separate stonks-tick cron."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="position-stream-delivery")
        self._thread.start()

    def stop(self) -> None:
        """Drain-to-completion, not immediate cutoff — an alert that was
        already queued before stop() must still be delivered, not silently
        dropped because shutdown happened to race it. The sentinel is the
        only thing that ends the loop; nothing checks self._stop in the
        loop condition itself (a prior version did, and could exit before
        draining items pushed just before stop() — caught by
        test_items_handled_in_order)."""
        self._stop.set()
        self._queue.put({"kind": "_shutdown"})

    def push(self, item: Dict[str, Any]) -> None:
        self._queue.put(item)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item.get("kind") == "_shutdown":
                break
            try:
                self._handle(item)
            except Exception:
                log.exception("delivery worker failed on item: %s", item)

    def _handle(self, item: Dict[str, Any]) -> None:
        kind = item["kind"]
        if kind == "mechanical_check_stops":
            self._handle_mechanical_check_stops(item)
        elif kind == "watch_triggered":
            self._handle_watch_triggered(item)
        elif kind == "llm_alert":
            self._handle_llm_alert(item)
        else:
            log.warning("unknown delivery item kind: %s", kind)

    def _handle_mechanical_check_stops(self, item: Dict[str, Any]) -> None:
        if another_trader_stonks_run_active():
            log.info("skip mechanical check-stops for %s — another trader-stonks run is active", item.get("ticker"))
            return
        result = run_executor("--action", "check-stops")
        if not result:
            return
        for breach in result.get("breaches", []):
            ticker = breach["ticker"]
            qty = breach.get("shares_to_sell")
            if qty is None:
                positions = fetch_held_positions()
                pos = positions.get(ticker)
                if not pos:
                    continue
                qty = int(float(pos["qty"]))
            sell_result = run_executor("--action", "SELL", "--ticker", ticker, "--qty", str(qty))
            log.info("mechanical stop breach executed: %s — %s", breach["reason"], sell_result)
            record_decision(ticker, "SELL", f"[position_stream mechanical] {breach['reason']}")

    def _handle_watch_triggered(self, item: Dict[str, Any]) -> None:
        watch = item["watch"]
        if another_trader_stonks_run_active():
            log.info("skip watch %s — another trader-stonks run is active", watch["id"])
            return
        ticker = watch["ticker"]
        action = watch["action"]

        if action == "ALERT":
            self._handle_llm_alert({
                "ticker": ticker,
                "message": (f"[watch triggered] {ticker} {watch['field']} {watch['comparator']} "
                            f"{watch['value']} — {watch['reason']}. Current price: {item.get('current_price')}."),
            })
            clear_watch(watch["id"])
            return

        qty = watch["qty"]
        if action == "SELL" and qty == "all":
            positions = fetch_held_positions()
            pos = positions.get(ticker)
            if not pos:
                log.warning("watch %s: SELL 'all' but no held position for %s, dropping", watch["id"], ticker)
                clear_watch(watch["id"])
                return
            qty = int(float(pos["qty"]))

        args = ["--action", action, "--ticker", ticker, "--qty", str(qty),
                "--price", str(item.get("current_price", ""))]
        if action == "BUY":
            args += ["--conviction", str(watch["conviction"])]
            if watch.get("sector"):
                args += ["--sector", watch["sector"]]
        result = run_executor(*args)
        log.info("watch %s triggered: %s -> %s", watch["id"], watch["reason"], result)
        record_decision(ticker, action, f"[watch-triggered] {watch['reason']}", conviction=watch.get("conviction") or 0.0)
        clear_watch(watch["id"])

    def _handle_llm_alert(self, item: Dict[str, Any]) -> None:
        try:
            subprocess.run(
                ["openclaw", "agent", "--agent", AGENT_ID, "--message", item["message"]],
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            log.error("openclaw agent call timed out for alert: %s", item.get("ticker"))


# ── Manager — websocket subscription lifecycle + trade handling ───────────


class PositionStreamManager:
    def __init__(self, stream_factory: Optional[Callable[[str, str], Any]] = None) -> None:
        """stream_factory(api_key, secret_key) -> a StockDataStream-like
        object with subscribe_trades/unsubscribe_trades/run. Injectable for
        tests; defaults to the real alpaca-py client."""
        self._stream_factory = stream_factory or self._default_stream_factory
        self._stream: Any = None
        self._subscribed: set[str] = set()
        self._price_history: Dict[str, collections.deque] = {}
        self._last_recheck: Dict[str, float] = {}
        self._worker = DeliveryWorker()
        self._poll_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @staticmethod
    def _default_stream_factory(api_key: str, secret_key: str) -> Any:
        from alpaca.data.live import StockDataStream
        return StockDataStream(api_key, secret_key)

    def start(self) -> None:
        api_key, secret_key = get_alpaca_credentials()
        if not api_key or not secret_key:
            log.warning("no ALPACA_STONKS_KEY/SECRET — position_stream disabled")
            return
        self._stream = self._stream_factory(api_key, secret_key)
        self._worker.start()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="position-stream-poll")
        self._poll_thread.start()
        threading.Thread(target=self._stream.run, daemon=True, name="position-stream-ws").start()

    def stop(self) -> None:
        self._stop.set()
        self._worker.stop()
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                log.exception("error stopping alpaca stream")

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._resync_subscriptions()
            except Exception:
                log.exception("position poll failed")
            self._stop.wait(POSITION_POLL_INTERVAL_SECONDS)

    def _resync_subscriptions(self) -> None:
        positions = fetch_held_positions()
        held = set(positions.keys())
        new = held - self._subscribed
        gone = self._subscribed - held
        if new:
            self._stream.subscribe_trades(self._on_trade, *sorted(new))
            for ticker in new:
                self._price_history.setdefault(ticker, collections.deque(maxlen=PRICE_HISTORY_MAXLEN))
        if gone:
            self._stream.unsubscribe_trades(*sorted(gone))
            for ticker in gone:
                self._price_history.pop(ticker, None)
                self._last_recheck.pop(ticker, None)
        self._subscribed = held

    async def _on_trade(self, trade: Any) -> None:
        ticker = str(trade.symbol).upper()
        price = float(trade.price)
        now = time.time()
        history = self._price_history.setdefault(ticker, collections.deque(maxlen=PRICE_HISTORY_MAXLEN))
        history.append((now, price))
        self.handle_price_update(ticker, price, now)

    def handle_price_update(self, ticker: str, price: float, now: Optional[float] = None) -> None:
        """Split out from _on_trade so tests can drive it synchronously
        without a real websocket connection."""
        now = now if now is not None else time.time()
        params = load_params()
        positions = fetch_held_positions()
        position = positions.get(ticker)

        last = self._last_recheck.get(ticker, 0.0)
        cooldown_ok = (now - last) >= RECHECK_COOLDOWN_SECONDS

        if position and cooldown_ok:
            entry_price = float(position["avg_entry_price"])
            if within_local_stop_proximity(price, entry_price, params):
                self._last_recheck[ticker] = now
                self._worker.push({"kind": "mechanical_check_stops", "ticker": ticker})
            elif within_profit_target_guide_proximity(price, entry_price, params):
                self._last_recheck[ticker] = now
                self._worker.push({
                    "kind": "llm_alert", "ticker": ticker,
                    "message": (f"[position_stream] {ticker} at ${price:.2f} is near the profit-target guide "
                                f"(entry ${entry_price:.2f}). Worth reviewing whether to take profit or let it run."),
                })

        if cooldown_ok:
            history = self._price_history.get(ticker)
            if history:
                move = detect_adverse_move(history)
                if move:
                    self._last_recheck[ticker] = now
                    self._worker.push({
                        "kind": "llm_alert", "ticker": ticker,
                        "message": (f"[position_stream] {ticker} moved {move['change_pct']:.1f}% "
                                    f"(${move['from_price']:.2f} -> ${move['to_price']:.2f}) in the last "
                                    f"{move['window_seconds']:.0f}s. Worth checking for thesis-breaking news."),
                    })

        for watch in load_active_watches():
            if watch["ticker"] != ticker:
                continue
            if evaluate_watch(watch, price, position):
                self._worker.push({"kind": "watch_triggered", "watch": watch, "current_price": price})


_manager: Optional[PositionStreamManager] = None


def start_position_stream() -> None:
    global _manager
    if _manager is not None:
        return
    _manager = PositionStreamManager()
    _manager.start()
