#!/usr/bin/env python3
"""Separate synthesis output from AGENTS.md files — prompt bloat guard.

The prompt bloat risk (#175): Nightly synthesis appends ~80 lines/night into
AGENTS.md files, pushing them toward OpenClaw's 12K hard limit. Mid-file
instructions silently get truncated when the limit is exceeded.

This script:
1. Scans all trader AGENTS.md files for synthesis output that may have been
   appended (by looking for "Nightly" or "synthesis" markers in the tail).
2. If found, moves the synthesis section to reports/nightly_synthesis_*.md and
   removes it from AGENTS.md.
3. Reports file sizes and warns if any approach the 12K hard limit.

Usage:
    python3 scripts/separate_synthesis_output.py                    # check & clean all
    python3 scripts/separate_synthesis_output.py --trader kairos    # single trader
    python3 scripts/separate_synthesis_output.py --check-only       # just report, no write
    python3 scripts/separate_synthesis_output.py --guard            # exit 1 if any > 10K

Cron (run before nightly synthesis):
    30 3 * * 1-5  cd /home/raf/projects/paper-trading-rebuild && \\
        python3 scripts/separate_synthesis_output.py >> logs/separate_synthesis.log 2>&1
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger("separate_synthesis")

# Paths
REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
REPORTS_DIR = REPO_ROOT / "reports"

# All trader directories
TRADER_DIRS: List[str] = ["trader-aldridge", "trader-kairos", "trader-stonks"]

# Sentinel markers that indicate synthesis output has been appended
# (These match the headings produced by NightlySummary.format())
SYNTHESIS_HEADINGS = [
    r"^=== Nightly Learning Summary: ",
    r"^### ⚠️ Active Risk: Prompt Bloat",
    r"^## Nightly Synthesis",
    r"^## Nightly Learning Summary",
    r"^## Promotion Summary",
    r"^## Auto-Promoted",
    r"^## PR-Ready",
    r"^## Needs Validation",
]

# Hard limit from OpenClaw
HARD_LIMIT = 12_000
WARN_LIMIT = 10_000  # Warn above this threshold
SOFT_TARGET = 2_000  # Ideal target per AGENTS.md


def find_synthesis_section(lines: List[str]) -> Optional[int]:
    """Find the first line index where a synthesis section begins.

    Scans the TAIL of the file (last 50 lines or so) for synthesis markers.
    Returns the line index of the section start, or None.
    """
    # Only scan the last portion — synthesis is always appended at the end
    scan_start = max(0, len(lines) - 80)
    for i in range(scan_start, len(lines)):
        for pattern in SYNTHESIS_HEADINGS:
            if re.match(pattern, lines[i].strip()):
                return i
    return None


def extract_synthesis_from_agents(
    trader_dir: str,
    check_only: bool = False,
    agents_base: Optional[Path] = None,
) -> Tuple[bool, float, Optional[str]]:
    """Check a trader's AGENTS.md for embedded synthesis output.

    Args:
        trader_dir: Directory name (e.g., 'trader-kairos')
        check_only: If True, don't modify files.
        agents_base: Base directory for agents (default: AGENTS_DIR).

    Returns:
        Tuple of (had_synthesis, file_size_kb, synthesis_content_or_none).
    """
    if agents_base is None:
        agents_base = AGENTS_DIR
    agents_path = agents_base / trader_dir / "AGENTS.md"
    if not agents_path.exists():
        return False, 0, None

    content = agents_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    file_size = len(content)
    file_size_kb = file_size / 1024

    # Check for synthesis section
    synth_start = find_synthesis_section(lines)
    if synth_start is None:
        return False, file_size_kb, None

    # Extract synthesis content (from synth_start to end of file)
    synth_content = "\n".join(lines[synth_start:])
    # Also strip any trailing whitespace
    synth_content = synth_content.strip()

    if not check_only:
        # Remove synthesis section from AGENTS.md
        cleaned_lines = lines[:synth_start]
        # Strip trailing blank lines
        while cleaned_lines and cleaned_lines[-1].strip() == "":
            cleaned_lines.pop()
        cleaned_content = "\n".join(cleaned_lines) + "\n"
        agents_path.write_text(cleaned_content, encoding="utf-8")

    return True, file_size_kb, synth_content


def save_synthesis_report(
    trader_short: str,
    content: str,
    date_str: str,
) -> Path:
    """Save extracted synthesis content to a report file.

    Returns the path to the saved report.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"nightly_synthesis_{trader_short}_{date_str}.md"
    # Avoid overwriting — append a suffix if the file already exists
    counter = 1
    while report_path.exists():
        report_path = (
            REPORTS_DIR / f"nightly_synthesis_{trader_short}_{date_str}_{counter}.md"
        )
        counter += 1
    report_path.write_text(content, encoding="utf-8")
    log.info("Saved synthesis report to %s", report_path)
    return report_path


def check_file_size(
    trader_dir: str,
    file_size_kb: float,
) -> int:
    """Check a single AGENTS.md file size and report status.

    Returns 1 if over WARN_LIMIT, 2 if over HARD_LIMIT, 0 otherwise.
    """
    agents_path = AGENTS_DIR / trader_dir / "AGENTS.md"
    if not agents_path.exists():
        return 0

    file_size = len(agents_path.read_text(encoding="utf-8"))
    exit_code = 0

    status = "✓"
    if file_size >= HARD_LIMIT:
        status = "🔴"
        exit_code = 2
    elif file_size >= WARN_LIMIT:
        status = "🟡"
        exit_code = 1
    elif file_size <= SOFT_TARGET:
        status = "✅"

    print(
        f"  {status} {trader_dir}/AGENTS.md: {file_size:,} chars "
        f"({file_size/1024:.1f} KB) "
        f"{'⚠️ OVER HARD LIMIT!' if file_size >= HARD_LIMIT else ''}"
        f"{'⚠️ Approaching limit' if file_size >= WARN_LIMIT and file_size < HARD_LIMIT else ''}"
    )
    return exit_code


def run(
    trader: Optional[str] = None,
    check_only: bool = False,
    guard: bool = False,
) -> int:
    """Run the synthesis separation check.

    Args:
        trader: Optional specific trader short name (e.g., 'kairos').
        check_only: If True, only report — don't modify files.
        guard: If True, exit with code 1 if any file is over WARN_LIMIT.

    Returns:
        Exit code (0 = clean, 1 = warnings, 2 = errors).
    """
    traders_to_check: List[str] = []
    if trader:
        trader_dir = f"trader-{trader}"
        if (AGENTS_DIR / trader_dir).exists():
            traders_to_check = [trader_dir]
        else:
            print(f"❌ Unknown trader: {trader} (no directory {trader_dir})")
            return 2
    else:
        traders_to_check = TRADER_DIRS

    print(f"{'Checking' if check_only else 'Cleaning'} AGENTS.md files for synthesis bloat")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    had_issues = 0
    total_extracted = 0

    for trader_dir in traders_to_check:
        agents_path = AGENTS_DIR / trader_dir / "AGENTS.md"
        if not agents_path.exists():
            print(f"  - {trader_dir}/AGENTS.md: not found, skipping")
            continue

        had_synth, file_size_kb, synth_content = extract_synthesis_from_agents(
            trader_dir, check_only=check_only
        )

        # Always check file size
        exit_code = check_file_size(trader_dir, file_size_kb)
        had_issues = max(had_issues, exit_code)

        if had_synth and synth_content:
            total_extracted += 1
            trader_short = trader_dir.replace("trader-", "")
            date_str = datetime.now().strftime("%Y-%m-%d")

            if check_only:
                print(
                    f"  ⚠️  Found synthesis output ({len(synth_content)} chars) "
                    f"embedded in {trader_dir}/AGENTS.md"
                )
                print(f"      First line: {synth_content.split(chr(10))[0][:80]}")
            else:
                report_path = save_synthesis_report(trader_short, synth_content, date_str)
                new_size = len(agents_path.read_text(encoding="utf-8"))
                print(
                    f"  ✅ Extracted {len(synth_content):,} chars of synthesis "
                    f"from {trader_dir}/AGENTS.md"
                )
                print(f"     → Saved to {report_path}")
                print(f"     → AGENTS.md now {new_size:,} chars ({new_size/1024:.1f} KB)")

    print()
    if total_extracted > 0:
        print(f"✅ Extracted synthesis from {total_extracted} trader(s)")
    else:
        print("✅ No synthesis bloat found in any AGENTS.md file")

    if guard and had_issues >= 1:
        print("\n⚠️  GUARD FAILED: One or more AGENTS.md files exceed the warning threshold")
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Separate synthesis output from AGENTS.md — prompt bloat guard"
    )
    parser.add_argument(
        "--trader", type=str, default=None,
        help="Single trader short name (e.g., 'kairos'). Default: all.",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="Only check for bloat, don't modify files.",
    )
    parser.add_argument(
        "--guard", action="store_true",
        help="Exit with code 1 if any AGENTS.md exceeds 10K chars (for CI/cron).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose logging.",
    )

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    return run(
        trader=args.trader,
        check_only=args.check_only,
        guard=args.guard,
    )


if __name__ == "__main__":
    sys.exit(main())