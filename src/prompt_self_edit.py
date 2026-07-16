#!/usr/bin/env python3
"""
Prompt Self-Edit Mechanism — SPEC-v3 §4.2B.

Allows trader agents to detect stale info, prune bloated prompts,
move verbose sections to reference files, and add new insights
from journal distillation during the heartbeat cycle.

Architecture:
    PromptSelfEdit orchestrates three operations:
      ┌─────────────────────────────────────────────┐
      │  PromptSelfEdit.scan_and_prune()            │
      │    ├─ detect stale tickers (not in watchlist)│
      │    ├─ detect stale dates (> 7 days old)     │
      │    ├─ detect contradicted rules             │
      │    └─ prune detected stale content          │
      ├─────────────────────────────────────────────┤
      │  PromptSelfEdit.refactor_verbose()          │
      │    ├─ measure sections > 200 chars          │
      │    ├─ extract to reference/*.md files       │
      │    └─ replace with "See reference/..." link │
      ├─────────────────────────────────────────────┤
      │  PromptSelfEdit.inject_insights()           │
      │    ├─ read journal/YYYY-MM-DD.md            │
      │    ├─ extract learnings & suggestions       │
      │    └─ append "Lessons" section              │
      └─────────────────────────────────────────────┘

Usage:
    from src.prompt_self_edit import PromptSelfEdit

    editor = PromptSelfEdit(workspace="/path/to/trader-workspace")
    report = editor.run(insights=["...", "..."])
    # report.edits_made: bool
    # report.changes: list[str] — what changed
    # report.new_length: int

Integration (tick_prep.py or heartbeat script):
    editor = PromptSelfEdit(workspace)
    report = editor.run()
    if report.edits_made:
        journal_entry = f"[prompt-edit] {', '.join(report.changes)}"
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

log = logging.getLogger("prompt_self_edit")


# ── Data Types ────────────────────────────────────────────────────────────────


@dataclass
class SelfEditReport:
    """Report of what the self-edit mechanism changed."""
    edits_made: bool = False
    changes: List[str] = field(default_factory=list)
    sections_pruned: int = 0
    sections_extracted: int = 0
    insights_injected: int = 0
    new_length: int = 0
    old_length: int = 0
    error: Optional[str] = None


# ── Staleness Detectors ──────────────────────────────────────────────────────


def detect_stale_tickers(
    text: str,
    active_tickers: Set[str],
) -> List[str]:
    """Find tickers mentioned in the prompt that are no longer active.

    Scans for uppercase 1-5 letter stock symbols (e.g. AAPL, TSLA).
    Ignores common non-ticker words that match the pattern (USA, CEO, etc.).
    """
    # Common words that look like tickers but aren't
    FALSE_POSITIVES: Set[str] = {
        "A", "I", "USA", "CEO", "CFO", "CTO", "CIO", "IPO", "ROI", "YTD",
        "Q1", "Q2", "Q3", "Q4", "API", "URL", "URI", "PDF", "HTML", "JSON",
        "UTC", "GMT", "EST", "EDT", "PST", "PDT", "NYSE", "NASDAQ", "AMEX",
        "SEC", "FED", "GDP", "CPI", "PPI", "EPS", "PE", "PB", "ROE", "ROA",
        "DD", "PNL", "SL", "TP", "SMA", "EMA", "RSI", "MACD", "ADX", "BB",
        "VIX", "SPY", "QQQ", "DIA", "IWM", "TLT", "GLD", "SLV", "USO", "UNG",
        "EEM", "EFA", "XLF", "XLK", "XLE", "XLI", "XLV", "XLY", "XLP", "XLB",
        "XLU", "XLRE", "XLC", "VT", "BND",
    }
    matches = set(re.findall(r'\b[A-Z]{1,5}\b', text))
    stale = [m for m in matches if m not in active_tickers and m not in FALSE_POSITIVES]
    # Heuristic: single chars + year-like words are unlikely tickers
    stale = [s for s in stale if len(s) > 1 and not s.isdigit() and not s.startswith("20")]
    return stale


def detect_stale_dates(text: str, max_age_days: int = 7) -> List[str]:
    """Find date references older than max_age_days.

    Matches ISO 8601 dates (YYYY-MM-DD) and common short dates.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    stale_lines: List[str] = []

    iso_matches = re.finditer(r'(\d{4}-\d{2}-\d{2})', text)
    for m in iso_matches:
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if dt < cutoff:
                # Find the line containing this date
                line_start = max(0, m.start() - 80)
                line_end = min(len(text), m.end() + 40)
                context = text[line_start:line_end].replace("\n", " ↵ ")
                stale_lines.append(context.strip())
        except ValueError:
            continue

    return stale_lines


def detect_contradicted_rules(text: str) -> List[str]:
    """Find pairs of contradictory rules in the prompt.

    Simple heuristic: detect "DO X" and "AVOID X" or "never X" and "always X".
    Returns descriptions of the contradictions found.
    """
    contradictions: List[str] = []

    # Common contradictory patterns
    patterns = [
        (r'\b(?:never|avoid|don\'t)\s+(?:buy|purchase|enter)\b',
         r'\b(?:always|must|should)\s+(?:buy|purchase|enter)\b'),
        (r'\b(?:never|avoid|don\'t)\s+(?:sell|exit)\b',
         r'\b(?:always|must|should)\s+(?:sell|exit)\b'),
        (r'\b(?:max|limit|ceiling)\s+(?:position|size|risk).*?(\d+)',
         r'\b(?:min|minimum)\s+(?:position|size|risk).*?(\d+)'),
    ]

    for i, (pat_a, pat_b) in enumerate(patterns):
        matches_a = list(re.finditer(pat_a, text, re.IGNORECASE))
        matches_b = list(re.finditer(pat_b, text, re.IGNORECASE))
        if matches_a and matches_b:
            contradictions.append(
                f"Pattern #{i + 1}: conflicting directives "
                f"({len(matches_a)} 'avoid/never' vs {len(matches_b)} 'always/must')"
            )

    return contradictions


# ── Verbosity Refactoring ────────────────────────────────────────────────────


def measure_section_sizes(text: str) -> List[Tuple[str, int, int]]:
    """Find sections > 200 chars by ## heading.

    Returns list of (heading_name, start_pos, length).
    """
    sections: List[Tuple[str, int, int]] = []
    lines = text.split("\n")
    current_heading = "(preamble)"
    current_start = 0
    current_chars = 0
    line_pos = 0

    for line in lines:
        if line.startswith("## "):
            if current_chars > 200:
                sections.append((current_heading, current_start, current_chars))
            current_heading = line.strip()
            current_start = line_pos
            current_chars = len(line)
        else:
            current_chars += len(line) + 1  # +1 for the newline
        line_pos += len(line) + 1

    # Last section
    if current_chars > 200:
        sections.append((current_heading, current_start, current_chars))

    return sections


# ── Insight Injection ────────────────────────────────────────────────────────


def extract_insights_from_journal(journal_text: str) -> List[str]:
    """Extract learning/insight lines from a journal entry.

    Looks for lines with keywords: "lesson", "learn", "insight",
    "next time", "pattern", "improve", or lines after "## Lessons".
    """
    insights: List[str] = []
    in_lessons_section = False

    for line in journal_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## Lessons"):
            in_lessons_section = True
            continue
        if stripped.startswith("## "):
            in_lessons_section = False
            continue

        if in_lessons_section and stripped and not stripped.startswith("-"):
            insights.append(stripped)

        # Also match bullet points with learning keywords
        keywords = ["lesson", "learn", "insight", "next time", "pattern", "improve"]
        if any(kw in stripped.lower() for kw in keywords) and stripped.startswith("-"):
            insights.append(stripped.lstrip("- "))

    return insights


# ── Main Editor ──────────────────────────────────────────────────────────────


class PromptSelfEdit:
    """Orchestrates prompt self-editing for a trader workspace.

    Args:
        workspace: Path to the trader workspace directory containing AGENTS.md
        prompt_file: Name of the prompt file (default: AGENTS.md)
        reference_dir: Name of the references directory (default: references)
    """

    def __init__(
        self,
        workspace: str | Path,
        prompt_file: str = "AGENTS.md",
        reference_dir: str = "references",
    ) -> None:
        self.workspace = Path(workspace)
        self.prompt_path = self.workspace / prompt_file
        self.reference_dir = self.workspace / reference_dir
        self.max_section_chars = 200

    # ── Public API ────────────────────────────────────────────────────────

    def run(
        self,
        active_tickers: Optional[Set[str]] = None,
        insights: Optional[List[str]] = None,
        dry_run: bool = False,
    ) -> SelfEditReport:
        """Run the full self-edit pipeline.

        Steps:
          1. Detect stale tickers, dates, and contradictions
          2. Prune stale content
          3. Refactor verbose sections to reference files
          4. Inject insights from journal distillation

        Args:
            active_tickers: Set of currently watched tickers
            insights: Pre-extracted insights to inject
            dry_run: If True, report changes without modifying files

        Returns:
            SelfEditReport describing all changes
        """
        report = SelfEditReport()

        if not self.prompt_path.exists():
            report.error = f"Prompt file not found: {self.prompt_path}"
            log.warning(report.error)
            return report

        try:
            original_text = self.prompt_path.read_text(encoding="utf-8")
        except Exception as e:
            report.error = f"Failed to read prompt: {e}"
            log.error(report.error)
            return report

        report.old_length = len(original_text)
        text = original_text
        changes: List[str] = []

        # Step 1: Detect stale tickers
        if active_tickers:
            stale_tickers = detect_stale_tickers(text, active_tickers)
            if stale_tickers:
                # Remove lines referencing stale tickers
                for ticker in stale_tickers:
                    # Only prune if it's an inline reference, not a heading
                    old_len = len(text)
                    text = re.sub(
                        rf'\b{ticker}\b[\s\S]{{0,80}}(?:\n|$)',
                        lambda m: m.group(0) if m.group(0).startswith("##") else m.group(0).replace(ticker, "[stale]"),
                        text,
                    )
                    if len(text) < old_len:
                        changes.append(f"Pruned stale ticker: {ticker}")

                report.sections_pruned = len(stale_tickers)

        # Step 2: Detect stale dates
        stale_dates = detect_stale_dates(text)
        for date_context in stale_dates:
            text = text.replace(date_context.replace(" ↵ ", "\n"), "")
            changes.append(f"Pruned stale date reference: {date_context[:50]}...")
            report.sections_pruned += 1

        # Step 3: Detect contradictions
        contradictions = detect_contradicted_rules(text)
        for c in contradictions:
            changes.append(f"Marked contradiction: {c}")
            # Flag contradictions rather than auto-removing
            text += f"\n\n> ⚠️ Contradiction detected: {c} — resolve in next heartbeat."

        # Step 4: Refactor verbose sections
        verbose_sections = measure_section_sizes(text)
        for heading, start, length in verbose_sections:
            if heading == "(preamble)":
                continue  # Don't extract the preamble

            # Extract section content
            section_end = start + length
            section_text = text[start:section_end]

            # Create reference file
            safe_name = heading.lower().replace("## ", "").replace(" ", "_").replace("/", "_")
            ref_path = self.reference_dir / f"{safe_name}.md"
            ref_content = f"# {heading.lstrip('# ')}\n\n{section_text.split(chr(10), 1)[1] if chr(10) in section_text else section_text[len(heading):]}"

            if not dry_run:
                self.reference_dir.mkdir(parents=True, exist_ok=True)
                ref_path.write_text(ref_content.strip() + "\n")

            # Replace in prompt with a reference
            replacement = f"{heading}\n\nSee `references/{ref_path.name}` for details.\n"
            text = text[:start] + replacement + text[start + length:]
            changes.append(f"Extracted {heading.strip()} → references/{ref_path.name}")
            report.sections_extracted += 1

        # Step 5: Inject insights
        if insights:
            injected = []
            for insight in insights:
                if insight.strip() and insight not in text:
                    injected.append(insight)

            if injected:
                if not dry_run:
                    text += "\n\n## Lessons\n\n"
                    for insight in injected:
                        text += f"- {insight}\n"
                changes.append(f"Injected {len(injected)} insights from journal distillation")
                report.insights_injected = len(injected)

        report.new_length = len(text)
        report.edits_made = bool(changes)
        report.changes = changes

        # Write back the modified prompt
        if not dry_run and report.edits_made:
            try:
                self.prompt_path.write_text(text, encoding="utf-8")
            except Exception as e:
                report.error = f"Failed to write prompt: {e}"
                log.error(report.error)
                return report

        return report

    # ── Convenience: scan without modifying ───────────────────────────────

    def scan(self, active_tickers: Optional[Set[str]] = None) -> SelfEditReport:
        """Read-only scan: detect issues without making changes."""
        return self.run(active_tickers=active_tickers, dry_run=True)