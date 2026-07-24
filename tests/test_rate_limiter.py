"""Tests for src/rate_limiter.py — JSON-backed daily call counter."""
import json

import pytest

from src import rate_limiter


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point the module at a throwaway file so tests never touch the real
    shared/rate_limits.json."""
    monkeypatch.setattr(rate_limiter, "_STATE_PATH", tmp_path / "rate_limits.json")


class TestCheckAndIncrement:
    def test_first_call_allowed(self):
        allowed, remaining = rate_limiter.check_and_increment("source_a", limit=5)
        assert allowed is True
        assert remaining == 4

    def test_exhausts_at_limit(self):
        for _ in range(3):
            allowed, _ = rate_limiter.check_and_increment("source_a", limit=3)
        assert allowed is True  # the 3rd call is still within limit

        allowed, remaining = rate_limiter.check_and_increment("source_a", limit=3)
        assert allowed is False
        assert remaining == 0

    def test_sources_tracked_independently(self):
        for _ in range(3):
            rate_limiter.check_and_increment("source_a", limit=3)
        allowed, _ = rate_limiter.check_and_increment("source_b", limit=3)
        assert allowed is True

    def test_state_persists_to_file(self, tmp_path):
        rate_limiter.check_and_increment("source_a", limit=10)
        rate_limiter.check_and_increment("source_a", limit=10)
        state = json.loads(rate_limiter._STATE_PATH.read_text())
        assert state["source_a"]["count"] == 2

    def test_new_day_resets_counter(self, tmp_path):
        rate_limiter._STATE_PATH.write_text(json.dumps({
            "source_a": {"date": "2020-01-01", "count": 999, "limit": 5}
        }))
        allowed, remaining = rate_limiter.check_and_increment("source_a", limit=5)
        assert allowed is True
        assert remaining == 4  # not still exhausted from the stale date

    def test_corrupt_file_starts_fresh(self, tmp_path):
        rate_limiter._STATE_PATH.write_text("not valid json{{{")
        allowed, remaining = rate_limiter.check_and_increment("source_a", limit=5)
        assert allowed is True
        assert remaining == 4


class TestStatus:
    def test_status_before_any_calls(self):
        assert rate_limiter.status("never_called")["count"] == 0

    def test_status_reflects_calls_without_incrementing(self):
        rate_limiter.check_and_increment("source_a", limit=10)
        rate_limiter.check_and_increment("source_a", limit=10)
        s1 = rate_limiter.status("source_a")
        s2 = rate_limiter.status("source_a")
        assert s1["count"] == s2["count"] == 2


class TestResetsAt:
    def test_returns_iso_midnight_utc(self):
        ts = rate_limiter.resets_at()
        assert ts.endswith("T00:00:00+00:00")
