"""Tests for src/worker_registry.py and src/dispatcher.py.

No real gRPC connections — mocks GpuClient.stub directly, same "logic only,
exercise the real network manually" split as test_gpu_client.py. No
pytest-asyncio in this repo (anyio is present but unconfigured for test
markers) -- async pieces are driven via a plain asyncio.run() inside
ordinary sync test functions instead of pulling in new test infra for it.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.worker_registry import WorkerRegistry, WorkerStatus
from src.dispatcher import Dispatcher, NoWorkerAvailable


class TestWorkerRegistry:
    def test_poll_all_marks_reachable_worker_online(self, monkeypatch):
        registry = WorkerRegistry(workers={"mac-a": "fake-a:5002"})

        async def fake_poll_one(self, worker_id, address):
            return WorkerStatus(worker_id=worker_id, address=address, online=True, queue_depth=0)
        monkeypatch.setattr(WorkerRegistry, "_poll_one", fake_poll_one)

        statuses = asyncio.run(registry.poll_all())
        assert statuses["mac-a"].online is True

    def test_poll_all_marks_unreachable_worker_offline_not_raise(self, monkeypatch):
        registry = WorkerRegistry(workers={"mac-b": "fake-b:5002"})

        async def fake_poll_one(self, worker_id, address):
            return WorkerStatus(worker_id=worker_id, address=address, online=False, last_error="no route")
        monkeypatch.setattr(WorkerRegistry, "_poll_one", fake_poll_one)

        statuses = asyncio.run(registry.poll_all())
        assert statuses["mac-b"].online is False
        assert "no route" in statuses["mac-b"].last_error

    def test_poll_one_catches_exception_and_returns_offline(self, monkeypatch):
        registry = WorkerRegistry(workers={"mac-a": "fake-a:5002"})

        class BrokenClient:
            def __init__(self, address):
                pass

            async def close(self):
                pass

            class stub:
                @staticmethod
                async def Health(req):
                    raise RuntimeError("connection refused")

        monkeypatch.setattr("src.worker_registry.GpuClient", BrokenClient)
        status = asyncio.run(registry._poll_one("mac-a", "fake-a:5002"))
        assert status.online is False
        assert "connection refused" in status.last_error

    def test_online_workers_filters_by_last_poll(self):
        registry = WorkerRegistry(workers={"mac-a": "a:5002", "mac-b": "b:5002"})
        registry._status = {
            "mac-a": WorkerStatus(worker_id="mac-a", address="a:5002", online=True),
            "mac-b": WorkerStatus(worker_id="mac-b", address="b:5002", online=False),
        }
        online = registry.online_workers()
        assert len(online) == 1
        assert online[0].worker_id == "mac-a"


class TestDispatcher:
    def test_pick_returns_least_loaded_online_worker(self):
        registry = WorkerRegistry(workers={"mac-a": "a:5002", "mac-b": "b:5002"})
        registry._status = {
            "mac-a": WorkerStatus(worker_id="mac-a", address="a:5002", online=True, queue_depth=3),
            "mac-b": WorkerStatus(worker_id="mac-b", address="b:5002", online=True, queue_depth=0),
        }
        dispatcher = Dispatcher(registry=registry)
        picked = dispatcher.pick()
        assert picked.worker_id == "mac-b"

    def test_pick_skips_offline_workers(self):
        registry = WorkerRegistry(workers={"mac-a": "a:5002", "mac-b": "b:5002"})
        registry._status = {
            "mac-a": WorkerStatus(worker_id="mac-a", address="a:5002", online=False, queue_depth=0),
            "mac-b": WorkerStatus(worker_id="mac-b", address="b:5002", online=True, queue_depth=5),
        }
        dispatcher = Dispatcher(registry=registry)
        picked = dispatcher.pick()
        assert picked.worker_id == "mac-b"

    def test_pick_raises_when_none_online(self):
        registry = WorkerRegistry(workers={"mac-a": "a:5002"})
        registry._status = {
            "mac-a": WorkerStatus(worker_id="mac-a", address="a:5002", online=False, last_error="offline"),
        }
        dispatcher = Dispatcher(registry=registry)
        with pytest.raises(NoWorkerAvailable, match="offline"):
            dispatcher.pick()

    def test_refresh_delegates_to_registry_poll_all(self, monkeypatch):
        registry = WorkerRegistry(workers={"mac-a": "a:5002"})
        called = {}

        async def fake_poll_all(self):
            called["yes"] = True
            return {}
        monkeypatch.setattr(WorkerRegistry, "poll_all", fake_poll_all)

        dispatcher = Dispatcher(registry=registry)
        asyncio.run(dispatcher.refresh())
        assert called.get("yes") is True
