"""Least-loaded GPU worker dispatch, with graceful fallback when a worker
is offline. Built on worker_registry.py's health-poll -- see that file's
docstring for why this exists as real code now instead of just a design
doc.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.worker_registry import WorkerRegistry, WorkerStatus

logger = logging.getLogger("dispatcher")


class NoWorkerAvailable(RuntimeError):
    """Raised when every configured worker is offline."""


class Dispatcher:
    """Wraps a WorkerRegistry with least-loaded selection. Callers should
    `await dispatcher.refresh()` before `pick()` if the health state might
    be stale (e.g. at the start of a training cycle) -- pick() itself never
    polls, so repeated picks within one cycle don't all pay the health-check
    cost."""

    def __init__(self, registry: Optional[WorkerRegistry] = None):
        self.registry = registry or WorkerRegistry()

    async def refresh(self) -> None:
        await self.registry.poll_all()

    def pick(self) -> WorkerStatus:
        """Return the online worker with the shortest queue. Raises
        NoWorkerAvailable if none are online -- callers decide whether
        that's fatal or a reason to skip this cycle, not this function."""
        online = self.registry.online_workers()
        if not online:
            statuses = self.registry.status()
            detail = ", ".join(f"{s.worker_id} ({s.last_error})" for s in statuses.values())
            raise NoWorkerAvailable(f"no GPU workers reachable: {detail}")
        return min(online, key=lambda s: s.queue_depth)

    def address_for(self, worker: WorkerStatus) -> str:
        return worker.address
