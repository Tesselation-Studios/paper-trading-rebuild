#!/usr/bin/env python3
"""
Event Bus — Push/subscribe model for the Data Bus.

Provides SSE (Server-Sent Events) streaming so agents receive real-time
data pushes instead of polling HTTP endpoints.

Architecture:
  EventBus (in-process pub/sub):
    ├── subscribe(topic) → queue.Queue
    ├── publish(topic, event) → fans out to all subscribers
    └── unsubscribe(topic, queue) → cleanup

  Flask SSE endpoints:
    GET /stream/quotes?symbols=AAPL,TSLA    — quote updates
    GET /stream/signals                      — trader signal broadcasts
    GET /stream/all                          — all events (firehose)

Usage (in data_bus.py):
    from src.event_bus import event_bus
    event_bus.publish("quotes", {"AAPL": {"close": 150.25, ...}})
    event_bus.publish("signals", {"agent": "kairos", "bias": "bullish", ...})

Usage (client agent):
    curl -N http://databus:5000/stream/quotes?symbols=AAPL,TSLA
    # Receives SSE events as they arrive
"""

import json
import queue
import threading
import time
import logging
from typing import Dict, Optional, Set, Any
from collections import defaultdict

log = logging.getLogger("event_bus")


class EventBus:
    """Thread-safe in-process pub/sub with per-topic subscriber queues.

    Subscribers get a queue.Queue. Publishers push events to all subscriber
    queues for that topic. Subscribers that fall behind (queue full) get
    dropped silently.
    """

    MAX_QUEUE_SIZE = 256  # events per subscriber queue
    MAX_SUBSCRIBERS_PER_TOPIC = 50
    GC_STALE_SUBSCRIBERS_AFTER = 300  # seconds (5 min without unsubscribe)

    def __init__(self):
        self._lock = threading.Lock()
        # topic → list of (queue, created_at, last_poll)
        self._subscribers: Dict[str, list] = defaultdict(list)

    def subscribe(self, topic: str) -> queue.Queue:
        """Register a subscriber for a topic. Returns a queue that receives events.

        The queue will receive dicts suitable for JSON serialization.
        """
        q = queue.Queue(maxsize=self.MAX_QUEUE_SIZE)
        now = time.time()
        with self._lock:
            subs = self._subscribers[topic]
            if len(subs) >= self.MAX_SUBSCRIBERS_PER_TOPIC:
                # Drop oldest subscriber
                oldest = subs.pop(0)
                try:
                    while not oldest[0].empty():
                        oldest[0].get_nowait()
                except Exception:
                    pass
            subs.append((q, now, now))
        log.debug("EventBus: +1 subscriber on '%s' (total: %d)", topic, len(subs))
        return q

    def unsubscribe(self, topic: str, q: queue.Queue):
        """Remove a subscriber."""
        with self._lock:
            before = len(self._subscribers[topic])
            self._subscribers[topic] = [
                (sq, created, last_poll) for sq, created, last_poll in self._subscribers[topic]
                if sq is not q
            ]
            after = len(self._subscribers[topic])
            if before > after:
                log.debug("EventBus: -1 subscriber on '%s' (total: %d)", topic, after)

    def publish(self, topic: str, event: dict):
        """Publish an event to all subscribers of a topic.

        Non-blocking: if a subscriber queue is full, the event is dropped
        for that subscriber.
        """
        with self._lock:
            subs = list(self._subscribers[topic])

        if not subs:
            return

        # Add server timestamp
        event["_published_at"] = time.time()
        payload = json.dumps(event)

        dropped = 0
        for q, created, last_poll in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dropped += 1

        if dropped:
            log.debug("EventBus: dropped %d/%d subscribers on '%s' (queue full)",
                      dropped, len(subs), topic)

    def publish_multi(self, topics_and_events: list):
        """Publish multiple (topic, event) pairs atomically."""
        for topic, event in topics_and_events:
            self.publish(topic, event)

    def gc_stale(self):
        """Remove subscribers that haven't been polled recently."""
        now = time.time()
        with self._lock:
            for topic in list(self._subscribers.keys()):
                before = len(self._subscribers[topic])
                self._subscribers[topic] = [
                    (q, created, last_poll) for q, created, last_poll in self._subscribers[topic]
                    if now - last_poll < self.GC_STALE_SUBSCRIBERS_AFTER
                ]
                after = len(self._subscribers[topic])
                if before > after:
                    log.info("EventBus: GC removed %d stale subscribers from '%s'",
                             before - after, topic)

    def subscriber_count(self, topic: str = None) -> int:
        """Count subscribers. If topic is None, return total across all topics."""
        with self._lock:
            if topic:
                return len(self._subscribers.get(topic, []))
            return sum(len(subs) for subs in self._subscribers.values())

    def status(self) -> dict:
        """Return status snapshot for debug endpoint."""
        with self._lock:
            topics = {}
            for topic, subs in sorted(self._subscribers.items()):
                now = time.time()
                topics[topic] = {
                    "subscribers": len(subs),
                    "oldest_age": round(now - min(c for _, c, _ in subs), 0) if subs else 0,
                }
            return {
                "total_subscribers": sum(len(subs) for subs in self._subscribers.values()),
                "topics": topics,
            }


# ── SSE Helpers ───────────────────────────────────────────────────────────────

def sse_event(event_type: str, data: dict, event_id: int = None) -> str:
    """Format an SSE message string.

    Args:
        event_type: The SSE event type (e.g. "quote", "signal")
        data: Dict to JSON-serialize as the event data
        event_id: Optional integer event ID

    Returns:
        An SSE-formatted string ready to yield.
    """
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(data)}")
    lines.append("")  # blank line terminates the event
    return "\n".join(lines) + "\n"


def sse_keepalive(comment: str = "keepalive") -> str:
    """Generate an SSE keepalive comment (prevents proxy timeout)."""
    return f": {comment}\n\n"


def sse_subscriber_generator(
    event_bus: "EventBus",
    topic: str,
    keepalive_interval: float = 15.0,
    filter_fn=None,
) -> str:
    """Generator that yields SSE events for a topic subscription.

    Args:
        event_bus: The EventBus instance
        topic: Topic to subscribe to
        keepalive_interval: Seconds between keepalive comments
        filter_fn: Optional callable(event_dict) → bool to filter events

    Yields:
        SSE-formatted strings
    """
    q = event_bus.subscribe(topic)
    last_keepalive = time.time()
    try:
        while True:
            try:
                payload = q.get(timeout=1.0)
                event = json.loads(payload)
                if filter_fn is None or filter_fn(event):
                    yield sse_event(topic.rstrip("s"), event)
                last_keepalive = time.time()
            except queue.Empty:
                pass

            # Keepalive to prevent proxy/load balancer timeout
            now = time.time()
            if now - last_keepalive >= keepalive_interval:
                yield sse_keepalive()
                last_keepalive = now
    except GeneratorExit:
        pass
    finally:
        event_bus.unsubscribe(topic, q)


# ── Global singleton ──────────────────────────────────────────────────────────

event_bus = EventBus()
