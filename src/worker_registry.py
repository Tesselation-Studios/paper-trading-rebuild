"""GPU worker registry — health-polls configured workers, tracks
online/offline state and queue depth.

Documented in Homelab-Notes/Services/GPU-Compute.md (2026-06-15,
"status: phase-0-complete") but never actually built anywhere -- that doc
describes worker_registry.py/dispatcher.py as if they existed; they
didn't (confirmed 2026-07-31, only gpu_client.py and config/workers.yaml
were real). This is the real implementation.

Workers are configured by hostname where possible, not a static IP --
gpu_client.py's own _resolve_hostname() comment already documents why
(this network's IPs drift; legend-of-macs.local:5002 resolved to a
different IP than the one hardcoded in workers.yaml when checked
2026-07-31). A worker with an unconfirmed/wrong address just health-checks
as offline and gets skipped by the dispatcher -- not a hard failure.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.gpu_client import GpuClient
from generated import gpu_compute_pb2 as pb

logger = logging.getLogger("worker_registry")

# id -> address. mac-a confirmed reachable 2026-07-31 via its mDNS hostname
# (not the stale 192.168.1.237 in the original config/workers.yaml doc).
# mac-b (M4 MacBook) is only online when Raf isn't using it for work --
# offline is expected, not a bug. Update its address here once a real
# hostname is known; until then the last-known IP just health-checks
# offline and the dispatcher skips it.
DEFAULT_WORKERS: Dict[str, str] = {
    "mac-a": "legend-of-macs.local:5002",
    "mac-b": "192.168.1.238:5002",
}

HEALTH_TIMEOUT_SECONDS = 5.0


@dataclass
class WorkerStatus:
    worker_id: str
    address: str
    online: bool = False
    state: Optional[int] = None  # pb.WorkerState enum value when online
    queue_depth: int = 0
    gpu_mem_free_mb: float = 0.0
    last_checked: float = field(default_factory=time.time)
    last_error: Optional[str] = None


class WorkerRegistry:
    """Holds configured workers and their last-polled health state."""

    def __init__(self, workers: Optional[Dict[str, str]] = None):
        self.workers = workers or dict(DEFAULT_WORKERS)
        self._status: Dict[str, WorkerStatus] = {
            wid: WorkerStatus(worker_id=wid, address=addr)
            for wid, addr in self.workers.items()
        }

    async def _poll_one(self, worker_id: str, address: str) -> WorkerStatus:
        client = GpuClient(address=address)
        try:
            health = await asyncio.wait_for(
                client.stub.Health(pb.HealthRequest()), timeout=HEALTH_TIMEOUT_SECONDS
            )
            return WorkerStatus(
                worker_id=worker_id, address=address, online=True,
                state=health.state, queue_depth=health.queue_depth,
                gpu_mem_free_mb=health.gpu_mem_free_mb, last_checked=time.time(),
            )
        except Exception as e:
            logger.debug("Worker %s (%s) offline: %s", worker_id, address, e)
            return WorkerStatus(
                worker_id=worker_id, address=address, online=False,
                last_checked=time.time(), last_error=str(e),
            )
        finally:
            await client.close()

    async def poll_all(self) -> Dict[str, WorkerStatus]:
        """Health-poll every configured worker in parallel. Always
        completes -- an unreachable worker just comes back online=False,
        never raises."""
        results = await asyncio.gather(
            *(self._poll_one(wid, addr) for wid, addr in self.workers.items())
        )
        self._status = {r.worker_id: r for r in results}
        return self._status

    def online_workers(self) -> List[WorkerStatus]:
        """Workers marked online as of the last poll_all() call. Does not
        poll -- call poll_all() first."""
        return [s for s in self._status.values() if s.online]

    def status(self) -> Dict[str, WorkerStatus]:
        return dict(self._status)
