#!/usr/bin/env python3
"""
Tests for src/event_bus.py — PubSub + SSE streaming infrastructure.
"""
import json
import queue
import threading
import time
import pytest
from src.event_bus import EventBus, sse_event, sse_keepalive, sse_subscriber_generator


class TestEventBus:
    """Unit tests for the EventBus pub/sub."""

    def test_subscribe_returns_queue(self):
        bus = EventBus()
        q = bus.subscribe("quotes")
        assert isinstance(q, queue.Queue)

    def test_publish_single_subscriber(self):
        bus = EventBus()
        q = bus.subscribe("quotes")
        bus.publish("quotes", {"AAPL": 150.0})
        payload = q.get(timeout=1)
        event = json.loads(payload)
        assert event["AAPL"] == 150.0
        assert "_published_at" in event

    def test_publish_multi_subscriber(self):
        bus = EventBus()
        q1 = bus.subscribe("quotes")
        q2 = bus.subscribe("quotes")
        bus.publish("quotes", {"TSLA": 250.0})
        e1 = json.loads(q1.get(timeout=1))
        e2 = json.loads(q2.get(timeout=1))
        assert e1["TSLA"] == 250.0
        assert e2["TSLA"] == 250.0

    def test_different_topics_isolated(self):
        bus = EventBus()
        q_quotes = bus.subscribe("quotes")
        q_signals = bus.subscribe("signals")
        bus.publish("quotes", {"AAPL": 150.0})
        got = json.loads(q_quotes.get(timeout=1))
        assert got["AAPL"] == 150.0
        # signals queue should still be empty
        with pytest.raises(queue.Empty):
            q_signals.get(timeout=0.1)

    def test_unsubscribe(self):
        bus = EventBus()
        q = bus.subscribe("quotes")
        bus.unsubscribe("quotes", q)
        bus.publish("quotes", {"AAPL": 150.0})
        with pytest.raises(queue.Empty):
            q.get(timeout=0.1)

    def test_queue_full_drops_event(self):
        bus = EventBus()
        # Create a queue with maxsize=1 and immediately fill it
        # Override the MAX_QUEUE_SIZE for this test by patching the queue
        bus.MAX_QUEUE_SIZE = 1
        q = bus.subscribe("quotes")
        bus.publish("quotes", {"first": 1})
        # Queue should be full now (size 1)
        bus.publish("quotes", {"second": 2})
        # Should not block, and first event should still be in queue
        e1 = json.loads(q.get(timeout=1))
        assert e1["first"] == 1
        # Second was dropped
        with pytest.raises(queue.Empty):
            q.get(timeout=0.1)

    def test_gc_stale_subscribers(self):
        bus = EventBus()
        bus.GC_STALE_SUBSCRIBERS_AFTER = -1  # immediately expire
        q = bus.subscribe("quotes")
        assert bus.subscriber_count("quotes") == 1
        bus.gc_stale()
        assert bus.subscriber_count("quotes") == 0

    def test_subscriber_count(self):
        bus = EventBus()
        bus.subscribe("quotes")
        bus.subscribe("quotes")
        bus.subscribe("signals")
        assert bus.subscriber_count("quotes") == 2
        assert bus.subscriber_count("signals") == 1
        assert bus.subscriber_count() == 3

    def test_status(self):
        bus = EventBus()
        bus.subscribe("quotes")
        s = bus.status()
        assert s["total_subscribers"] == 1
        assert "quotes" in s["topics"]

    def test_max_subscribers_per_topic_drops_oldest(self):
        bus = EventBus()
        bus.MAX_SUBSCRIBERS_PER_TOPIC = 2
        q1 = bus.subscribe("quotes")
        q2 = bus.subscribe("quotes")
        assert bus.subscriber_count("quotes") == 2
        q3 = bus.subscribe("quotes")
        assert bus.subscriber_count("quotes") == 2  # q1 dropped
        # q1 should no longer get events
        bus.publish("quotes", {"AAPL": 150.0})
        # q2 and q3 get it
        json.loads(q2.get(timeout=1))
        json.loads(q3.get(timeout=1))
        with pytest.raises(queue.Empty):
            q1.get(timeout=0.1)

    def test_publish_multi(self):
        bus = EventBus()
        q1 = bus.subscribe("quotes")
        q2 = bus.subscribe("signals")
        bus.publish_multi([
            ("quotes", {"AAPL": 150.0}),
            ("signals", {"agent": "kairos", "bias": "bullish"}),
        ])
        e1 = json.loads(q1.get(timeout=1))
        e2 = json.loads(q2.get(timeout=1))
        assert e1["AAPL"] == 150.0
        assert e2["agent"] == "kairos"

    def test_thread_safety(self):
        """Concurrent subscribe/unsubscribe/publish should not crash."""
        bus = EventBus()
        errors = []

        def worker():
            try:
                for _ in range(100):
                    q = bus.subscribe("quotes")
                    bus.publish("quotes", {"test": True})
                    bus.unsubscribe("quotes", q)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0


class TestSSEHelpers:
    """Unit tests for SSE formatting helpers."""

    def test_sse_event_basic(self):
        result = sse_event("quote", {"AAPL": 150.0})
        assert "event: quote" in result
        assert 'data: {"AAPL": 150.0}' in result
        assert result.endswith("\n")  # blank line terminator
        assert "\n\n" in result

    def test_sse_event_with_id(self):
        result = sse_event("signal", {"agent": "kairos"}, event_id=42)
        assert "id: 42" in result
        assert "event: signal" in result

    def test_sse_keepalive(self):
        result = sse_keepalive()
        assert result.startswith(":")
        assert "keepalive" in result
        assert result.endswith("\n\n")

    def test_sse_keepalive_custom(self):
        result = sse_keepalive("ping")
        assert result == ": ping\n\n"


class TestSSEGenerator:
    """Integration tests for the SSE subscriber generator."""

    def test_generator_yields_keepalives(self):
        bus = EventBus()
        # Subscribe with very short keepalive
        gen = sse_subscriber_generator(bus, "quotes", keepalive_interval=0.01)
        # First yield should be a keepalive (no events published)
        first = next(gen)
        assert first.startswith(":")

    def test_generator_yields_published_event(self):
        bus = EventBus()
        # Use short keepalive so next() unblocks quickly
        gen = sse_subscriber_generator(bus, "quotes", keepalive_interval=0.01)
        first_yield = next(gen)  # keepalive (establishes subscription)
        bus.publish("quotes", {"AAPL": 150.0})
        result = next(gen)
        assert "event: quote" in result
        assert "AAPL" in result
        gen.close()

    def test_filter_fn_skips_non_matching(self):
        bus = EventBus()
        gen = sse_subscriber_generator(
            bus, "quotes", keepalive_interval=0.01,
            filter_fn=lambda e: "AAPL" in e,
        )
        # Establish subscription first
        first_yield = next(gen)  # keepalive
        bus.publish("quotes", {"TSLA": 250.0})
        bus.publish("quotes", {"AAPL": 150.0})
        # TSLA should be consumed but not yielded (filtered out)
        # AAPL should be yielded
        result = next(gen)
        assert "event: quote" in result
        assert "AAPL" in result
        gen.close()

    def test_cleanup_on_generator_exit(self):
        bus = EventBus()
        gen = sse_subscriber_generator(bus, "quotes", keepalive_interval=0.01)
        # Advance generator once to trigger subscription
        next(gen)  # keepalive (establishes subscription)
        assert bus.subscriber_count("quotes") == 1
        gen.close()
        time.sleep(0.05)
        assert bus.subscriber_count("quotes") == 0


def test_event_bus_singleton():
    """Global event_bus singleton exists and works."""
    from src.event_bus import event_bus as eb1
    from src.event_bus import event_bus as eb2
    assert eb1 is eb2
    q = eb1.subscribe("test_singleton")
    eb1.publish("test_singleton", {"hello": "world"})
    event = json.loads(q.get(timeout=1))
    assert event["hello"] == "world"
    eb1.unsubscribe("test_singleton", q)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
