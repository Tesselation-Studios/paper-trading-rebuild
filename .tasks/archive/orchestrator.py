#!/usr/bin/env python3
"""Overnight task orchestrator — works through prioritized tasks one at a time.

Each tick (30 min): load state → pick/continue task → do ONE concrete action →
save state → report. Survives compaction/restarts via state file.

State file: .tasks/orchestrator_state.json

Priority: P0 (100) > P1 (50) > P2 (10) > no-label (5), then FIFO by issue #.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_DIR / ".tasks" / "orchestrator_state.json"
TASKS_DIR = PROJECT_DIR / ".tasks"
REPO = "Tesselation-Studios/paper-trading-rebuild"

# Priority weights
PRIORITY_WEIGHT = {"priority:p0": 100, "priority:p1": 50, "priority:p2": 10}

# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class Task:
    """A single task from GitHub or .tasks."""

    id: str  # "issue#74" or "task:filename"
    source: str  # "github" or "local"
    title: str
    priority: int  # 100, 50, 10, 5
    number: Optional[int] = None  # issue number if github
    labels: List[str] = field(default_factory=list)


@dataclass
class OrchestratorState:
    """Persisted state between ticks."""

    current_task: Optional[Task] = None
    status: str = "idle"  # idle | working | blocked | done
    started_at: Optional[str] = None
    ticks: int = 0
    last_action: str = ""
    next_action: str = ""
    completed: List[str] = field(default_factory=list)  # task IDs
    blocked_reason: str = ""
    total_completed: int = 0
    last_report_at: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "current_task": {
                "id": self.current_task.id,
                "source": self.current_task.source,
                "title": self.current_task.title,
                "priority": self.current_task.priority,
                "number": self.current_task.number,
                "labels": self.current_task.labels,
            }
            if self.current_task
            else None,
            "status": self.status,
            "started_at": self.started_at,
            "ticks": self.ticks,
            "last_action": self.last_action,
            "next_action": self.next_action,
            "completed": self.completed,
            "total_completed": self.total_completed,
            "blocked_reason": self.blocked_reason,
            "last_report_at": self.last_report_at,
        }
        # Truncate long strings for state file
        for key in ("last_action", "next_action", "blocked_reason"):
            if d[key] and len(d[key]) > 500:
                d[key] = d[key][:497] + "..."
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "OrchestratorState":
        ct = d.get("current_task")
        task = None
        if ct:
            task = Task(
                id=ct["id"],
                source=ct["source"],
                title=ct["title"],
                priority=ct.get("priority", 5),
                number=ct.get("number"),
                labels=ct.get("labels", []),
            )
        return cls(
            current_task=task,
            status=d.get("status", "idle"),
            started_at=d.get("started_at"),
            ticks=d.get("ticks", 0),
            last_action=d.get("last_action", ""),
            next_action=d.get("next_action", ""),
            completed=d.get("completed", []),
            total_completed=d.get("total_completed", 0),
            blocked_reason=d.get("blocked_reason", ""),
            last_report_at=d.get("last_report_at"),
        )


# ── State persistence ─────────────────────────────────────────────────────────


def load_state() -> OrchestratorState:
    """Load state from disk, returning fresh state if file missing."""
    if STATE_FILE.exists():
        try:
            return OrchestratorState.from_dict(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, KeyError):
            pass
    return OrchestratorState()


def save_state(state: OrchestratorState):
    """Persist state atomically."""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2, default=str))
    tmp.rename(STATE_FILE)


# ── Task discovery ────────────────────────────────────────────────────────────


def fetch_github_issues() -> List[Task]:
    """Fetch open GitHub issues with priority labels."""
    try:
        result = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", REPO,
                "--state", "open",
                "--limit", "20",
                "--json", "number,title,labels",
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            print(f"  gh failed: {result.stderr.strip()}")
            return []
        issues = json.loads(result.stdout)
    except Exception as e:
        print(f"  gh error: {e}")
        return []

    tasks = []
    for i in issues:
        labels = [l["name"] for l in i.get("labels", [])]
        priority = 5  # default
        for lbl in labels:
            if lbl in PRIORITY_WEIGHT:
                priority = PRIORITY_WEIGHT[lbl]
                break
        tasks.append(Task(
            id=f"issue#{i['number']}",
            source="github",
            title=i["title"],
            priority=priority,
            number=i["number"],
            labels=labels,
        ))
    return tasks


def fetch_local_tasks() -> List[Task]:
    """Find .tasks/*.md files that aren't the orchestrator or state."""
    tasks = []
    for f in sorted(TASKS_DIR.glob("*.md")):
        name = f.stem
        # First line often has priority
        first_line = ""
        try:
            first_line = f.read_text().split("\n")[0].strip()
        except Exception:
            pass

        priority = 5
        if "P0" in first_line or "P0" in name:
            priority = 100
        elif "P1" in first_line or "P1" in name:
            priority = 50

        tasks.append(Task(
            id=f"task:{name}",
            source="local",
            title=name.replace("-", " ").replace("_", " "),
            priority=priority,
        ))
    return tasks


def discover_tasks(exclude_completed: List[str]) -> List[Task]:
    """Fetch all tasks, excluding already-completed ones."""
    all_tasks = fetch_github_issues() + fetch_local_tasks()
    active = [t for t in all_tasks if t.id not in exclude_completed]
    active.sort(key=lambda t: (-t.priority, t.number or 9999))
    return active


# ── Task execution ────────────────────────────────────────────────────────────


def execute_task_tick(state: OrchestratorState) -> OrchestratorState:
    """Do ONE concrete action on the current task. Save progress for next tick.

    This is the core loop. Each call does a single step, not the whole task.
    The agent (LLM) does the real work — we just provide context + tool access.
    """
    task = state.current_task
    if not task:
        return state

    state.ticks += 1
    now = datetime.now(timezone.utc).isoformat()

    # Build context for the agent
    context = f"""## Orchestrator Tick #{state.ticks}

**Task:** {task.title} ({task.id})
**Priority:** {task.priority}
**Started:** {state.started_at}
**Ticks spent:** {state.ticks}

**Last action:** {state.last_action or '(none — first tick)'}
**Next step:** {state.next_action or '(start work)'}

### Instructions
1. Read the task file or GitHub issue for full context
2. Do ONE concrete action toward completion (don't try to finish everything at once)
3. Save your progress by writing an update below

### Output format
After your action, end with:
---
STATUS: (in_progress|blocked|done)
ACTION: <one-line summary of what you just did>
NEXT: <what to do on next tick>
BLOCKED: <reason if blocked, else "none">
---
"""

    # The actual work happens in the agent's turn after receiving this context.
    # We write the context to stdout for the cron agent to read.
    print(context)

    # Mark the next action so the agent knows what to update
    state.last_action = f"Tick #{state.ticks} — see agent response"
    state.status = "working"
    state.last_report_at = now

    return state


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    os.chdir(PROJECT_DIR)

    state = load_state()
    print(f"=== Orchestrator Tick === (state: {state.status}, tasks done: {state.total_completed})")

    # If no current task, pick one
    if state.current_task is None or state.status in ("done", "blocked"):
        if state.current_task and state.status == "done":
            state.completed.append(state.current_task.id)
            state.total_completed += 1
            state.current_task = None

        tasks = discover_tasks(state.completed)
        if not tasks:
            print("✅ All tasks complete! Nothing to do.")
            state.status = "idle"
            state.last_action = "Queue empty — all tasks done"
            save_state(state)
            return

        task = tasks[0]
        state.current_task = task
        state.status = "working"
        state.started_at = datetime.now(timezone.utc).isoformat()
        state.ticks = 0
        state.last_action = f"Picked {task.id}: {task.title}"
        state.next_action = "Read task and begin work"
        print(f"\n📋 NEW TASK: [{task.id}] {task.title} (priority={task.priority})")
        print(f"   Queue: {len(tasks)} tasks remaining")

    # Execute one tick
    state = execute_task_tick(state)
    save_state(state)

    # Summary
    tasks_remaining = len(discover_tasks(state.completed))
    print(f"\n📊 State saved: {state.status} | {state.total_completed} done | {tasks_remaining} queued")
    print(f"   Current: {state.current_task.id if state.current_task else 'none'}")
    print(f"   Next: {state.next_action}")

    # Push health dashboard to Canvas after each tick batch
    push_health = PROJECT_DIR / "scripts" / "push_health.sh"
    if push_health.exists():
        try:
            subprocess.run(
                [str(push_health), "--board", "trading"],
                capture_output=True, text=True, timeout=30,
                cwd=str(PROJECT_DIR),
            )
            print("   ✅ Health dashboard pushed to Canvas")
        except Exception as e:
            print(f"   ⚠️  Health push failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
