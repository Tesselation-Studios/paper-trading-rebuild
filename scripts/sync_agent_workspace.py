#!/usr/bin/env python3
"""
Sync agent workspace files from the repo to OpenClaw agent workspaces.

Reads agent configs from agents/trader-*/ and copies:
  - HEARTBEAT.md  → ~/.openclaw/agents/{trader}/agent/HEARTBEAT.md
  - AGENTS.md      → ~/.openclaw/agents/{trader}/agent/AGENTS.md
  - MEMORY.md      → ~/.openclaw/agents/{trader}/agent/MEMORY.md
  - prompt.txt     → ~/.openclaw/agents/{trader}/prompt.txt

This ensures the OpenClaw heartbeat can read the correct HEARTBEAT.md
and AGENTS.md from the agent workspace during persistent sessions.

Usage:
    python3 scripts/sync_agent_workspace.py              # sync all traders
    python3 scripts/sync_agent_workspace.py --trader kairos  # single trader
    python3 scripts/sync_agent_workspace.py --dry-run    # preview only
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path
from typing import List

log = logging.getLogger("sync_agent_workspace")

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
OPENCLAW_HOME = Path.home() / ".openclaw"

TRADERS = ["trader-kairos", "trader-aldridge", "trader-stonks"]

# Files to sync: (repo_relative_path, workspace_relative_path)
SYNC_FILES = [
    ("HEARTBEAT.md", "agent/HEARTBEAT.md"),
    ("AGENTS.md", "agent/AGENTS.md"),
    ("MEMORY.md", "agent/MEMORY.md"),
    ("prompt.txt", "prompt.txt"),
]


def sync_trader(trader_id: str, dry_run: bool = False) -> List[str]:
    """Sync files for one trader. Returns list of synced files."""
    src_dir = AGENTS_DIR / trader_id
    dest_dir = OPENCLAW_HOME / "agents" / trader_id

    if not src_dir.is_dir():
        log.warning("Source directory not found: %s", src_dir)
        return []

    synced = []
    for src_rel, dest_rel in SYNC_FILES:
        src = src_dir / src_rel
        dest = dest_dir / dest_rel

        if not src.is_file():
            log.debug("Source file not found, skipping: %s", src_rel)
            continue

        if dry_run:
            log.info("DRY-RUN: would copy %s -> %s", src_rel, dest)
            synced.append(src_rel)
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        log.info("Synced: %s/%s", trader_id, src_rel)
        synced.append(src_rel)

    return synced


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync agent workspace files for persistent heartbeat sessions"
    )
    parser.add_argument(
        "--trader",
        choices=[t.replace("trader-", "") for t in TRADERS] + ["all"],
        default="all",
        help="Trader to sync (default: all)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without copying"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    traders = (
        [f"trader-{args.trader}"]
        if args.trader != "all"
        else TRADERS
    )

    total = 0
    for trader in traders:
        synced = sync_trader(trader, dry_run=args.dry_run)
        total += len(synced)

    action = "Would sync" if args.dry_run else "Synced"
    log.info("%s %d file(s) across %d trader(s)", action, total, len(traders))

    if not args.dry_run:
        log.info(
            "Done. Restart OpenClaw gateway or wait for config hot-reload "
            "for changes to take effect."
        )


if __name__ == "__main__":
    main()
