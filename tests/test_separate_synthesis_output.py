"""Tests for scripts/separate_synthesis_output.py — prompt bloat guard.

REF: Issue #175 — Fix prompt bloat caused by nightly synthesis appending
~80 lines/night into AGENTS.md files.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from scripts.separate_synthesis_output import (
    find_synthesis_section,
    extract_synthesis_from_agents,
    SYNTHESIS_HEADINGS,
)


class TestFindSynthesisSection:
    """Test the pattern detection for synthesis sections embedded in AGENTS.md."""

    def test_no_synthesis_returns_none(self):
        lines = [
            "# AGENTS.md — Test Trader",
            "",
            "## Strategy",
            "Buy low, sell high.",
            "",
            "## Output Format",
            '{"action": "HOLD"}',
        ]
        assert find_synthesis_section(lines) is None

    def test_nightly_learning_summary_detected(self):
        lines = [
            "# AGENTS.md — Test Trader",
            "## Core Loop",
            "1. Read data",
            "2. Decide",
            "",
            "=== Nightly Learning Summary: 2026-07-14 ===",
            "## Kairos — 5 scenarios...",
            "  Learned: \"some insight\"",
        ]
        idx = find_synthesis_section(lines)
        assert idx is not None
        assert idx == 5  # 0-indexed line of the heading

    def test_promotion_summary_detected(self):
        lines = [
            "# AGENTS.md — Test Trader",
            "## Rules",
            "- No leverage",
            "",
            "## Promotion Summary",
            "### AUTO-PROMOTED (1)",
        ]
        idx = find_synthesis_section(lines)
        assert idx is not None
        assert idx == 4  # 0-indexed line of the heading

    def test_only_scans_tail_for_efficiency(self):
        """Synthesis is always at the end; don't scan the whole file."""
        lines = (
            ["# Header"] * 100
            + [""]
            + ["=== Nightly Learning Summary: 2026-07-14 ==="]
            + ["  Insight: test"] * 20
        )
        idx = find_synthesis_section(lines)
        assert idx is not None
        # The whole file is ~122 lines, so the marker is well within last 80
        assert idx >= 100


class TestExtractSynthesisFromAgents:
    """Test the extraction logic against real-ish AGENTS.md content."""

    CLEAN_AGENTS_MD = """# AGENTS.md — Kairos (Momentum Trader)

## Core Loop (every tick)
1. Read playbook
2. Read bankroll
3. Market snapshot
4. Decide BUY/SELL/HOLD

## Output Format
{"action": "HOLD"}

## Non-Negotiable Rules
- Max risk per trade: 2%
- Stop loss required
"""

    BLOATED_AGENTS_MD = CLEAN_AGENTS_MD + """
=== Nightly Learning Summary: 2026-07-14 ===

## Kairos — 10 scenarios, 15 trades (best score: 0.82)

  Learned: "momentum signals work best with volume confirmation"
  Suggestion: "increase volume threshold from 1.5x to 2.0x"
  Confidence: 0.78 (nights: 3) → AUTO-PROMOTED ✓

---

Generated at 2026-07-15T04:30:00
"""

    @pytest.fixture
    def agents_tmp_dir(self):
        """Create a temporary directory with a trader-test/AGENTS.md subdir."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Create the agents base dir with a subdirectory
            agents_base = base / "agents"
            trader_dir = agents_base / "trader-test"
            trader_dir.mkdir(parents=True, exist_ok=True)
            yield agents_base, trader_dir

    def test_extract_clean_file_returns_false(self, agents_tmp_dir):
        agents_base, trader_dir = agents_tmp_dir
        agents_md = trader_dir / "AGENTS.md"
        agents_md.write_text(self.CLEAN_AGENTS_MD)

        had_synth, size_kb, content = extract_synthesis_from_agents(
            "trader-test",
            check_only=True,
            agents_base=agents_base,
        )
        assert had_synth is False
        assert size_kb > 0

    def test_extract_bloated_removes_synthesis(self, agents_tmp_dir):
        agents_base, trader_dir = agents_tmp_dir
        agents_md = trader_dir / "AGENTS.md"
        agents_md.write_text(self.BLOATED_AGENTS_MD)

        had_synth, _, content = extract_synthesis_from_agents(
            "trader-test",
            check_only=False,
            agents_base=agents_base,
        )
        assert had_synth is True
        assert content is not None
        assert "Nightly Learning Summary" in content

        # Verify AGENTS.md is clean now
        remaining = agents_md.read_text()
        assert "Nightly Learning Summary" not in remaining
        assert "Core Loop" in remaining
        assert "Output Format" in remaining

    def test_check_only_does_not_modify(self, agents_tmp_dir):
        agents_base, trader_dir = agents_tmp_dir
        agents_md = trader_dir / "AGENTS.md"
        original = self.BLOATED_AGENTS_MD
        agents_md.write_text(original)

        had_synth, _, content = extract_synthesis_from_agents(
            "trader-test",
            check_only=True,
            agents_base=agents_base,
        )
        assert had_synth is True

        # File should be unchanged
        assert agents_md.read_text() == original


class TestSynthesisHeadings:
    """Verify all patterns match expected headings."""

    def test_patterns_match_real_headings(self):
        import re

        test_cases = [
            ("=== Nightly Learning Summary: 2026-07-14 ===", True),
            ("### ⚠️ Active Risk: Prompt Bloat", True),
            ("## Nightly Synthesis", True),
            ("## Nightly Learning Summary", True),
            ("## Promotion Summary", True),
            ("## Auto-Promoted", True),
            ("## PR-Ready", True),
            ("## Needs Validation", True),
            # Negative cases
            ("# AGENTS.md — Kairos", False),
            ("## Core Loop", False),
            ("## Output Format", False),
            ("## Non-Negotiable Rules", False),
        ]

        for heading, should_match in test_cases:
            matched = any(re.match(p, heading) for p in SYNTHESIS_HEADINGS)
            assert matched == should_match, (
                f"Heading '{heading}' should_match={should_match} but got {matched}"
            )


if __name__ == "__main__":
    pytest.main([__file__])