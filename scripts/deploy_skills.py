#!/usr/bin/env python3
"""Deploy skills from repo to OpenClaw skill directories.

Shared skills → ~/.openclaw/skills/
Per-trader strategy → ~/.openclaw/workspace-trader-<trader>/skills/

Run from repo root: python3 scripts/deploy_skills.py
"""

import shutil
import os
from pathlib import Path

OPENCLAW_SKILLS = Path.home() / ".openclaw" / "skills"
WORKSPACE_BASE = Path.home() / ".openclaw"
REPO_ROOT = Path(__file__).resolve().parent.parent

SHARED_SKILLS = [
    "trade-execution",
    "market-data",
    "fundamentals",
    "social-sentiment",
    "risk-management",
    "trading-hours",
]

TRADERS = ["kairos", "aldridge", "stonks"]


def deploy_shared():
    repo_skills = REPO_ROOT / "skills"
    OPENCLAW_SKILLS.mkdir(parents=True, exist_ok=True)
    for skill in SHARED_SKILLS:
        src = repo_skills / skill
        dst = OPENCLAW_SKILLS / skill
        if src.exists():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print(f"  ✓ {skill} → {dst}")
        else:
            print(f"  ✗ {skill} not found at {src}")


def deploy_trader(trader: str):
    repo_agents = REPO_ROOT / "agents"
    ws_dir = WORKSPACE_BASE / f"workspace-trader-{trader}"
    ws_skills = ws_dir / "skills"
    ws_skills.mkdir(parents=True, exist_ok=True)

    src = repo_agents / f"trader-{trader}" / "skills"
    if src.exists():
        for item in src.iterdir():
            dst = ws_skills / item.name
            if item.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)
        print(f"  ✓ trader-{trader} strategy → {ws_skills}")
    else:
        print(f"  ✗ trader-{trader}: no skills/ dir at {src}")


if __name__ == "__main__":
    print("Deploying shared skills → ~/.openclaw/skills/")
    deploy_shared()
    print()
    print("Deploying per-trader strategies")
    for trader in TRADERS:
        deploy_trader(trader)
    print()
    print("Done. Verify with: ls ~/.openclaw/skills/")
