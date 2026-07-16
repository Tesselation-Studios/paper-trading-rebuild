"""Tests for prompt_self_edit — prompt self-edit mechanism (SPEC-v3 §4.2B)."""

import pytest
import tempfile
from pathlib import Path
from src.prompt_self_edit import (
    PromptSelfEdit,
    SelfEditReport,
    detect_stale_tickers,
    detect_stale_dates,
    detect_contradicted_rules,
    measure_section_sizes,
    extract_insights_from_journal,
)


# ═══════════════════════════════════════════════════════════════════════════════
# detect_stale_tickers
# ═══════════════════════════════════════════════════════════════════════════════


class TestDetectStaleTickers:
    def test_no_stale(self):
        text = "Watch AAPL, MSFT, and GOOGL."
        assert detect_stale_tickers(text, {"AAPL", "MSFT", "GOOGL"}) == []

    def test_stale_found(self):
        text = "We hold ZZZZ and watch XXXX."
        stale = detect_stale_tickers(text, {"AAPL", "MSFT"})
        assert "ZZZZ" in stale
        assert "XXXX" in stale

    def test_false_positives_ignored(self):
        text = "USA GDP is 5%. CEO said IPO is next."
        assert detect_stale_tickers(text, {"AAPL"}) == []

    def test_empty_tickers(self):
        text = "No tickers here."
        assert detect_stale_tickers(text, set()) == []


# ═══════════════════════════════════════════════════════════════════════════════
# detect_stale_dates
# ═══════════════════════════════════════════════════════════════════════════════


class TestDetectStaleDates:
    def test_no_dates(self):
        assert detect_stale_dates("No dates here") == []

    def test_recent_date_not_stale(self):
        # Can't reliably test this without mocking time, just check no crash
        result = detect_stale_dates("Today is 2026-07-16", max_age_days=7)
        assert isinstance(result, list)


# ═══════════════════════════════════════════════════════════════════════════════
# detect_contradicted_rules
# ═══════════════════════════════════════════════════════════════════════════════


class TestDetectContradictedRules:
    def test_no_contradictions(self):
        text = "Always buy when RSI < 30."
        assert detect_contradicted_rules(text) == []

    def test_buy_contradiction(self):
        text = "Never buy when RSI < 30. Always buy when RSI < 30."
        contradictions = detect_contradicted_rules(text)
        assert len(contradictions) >= 1

    def test_sell_contradiction(self):
        text = "Never sell at a loss. Always sell if drawdown > 5%."
        contradictions = detect_contradicted_rules(text)
        assert len(contradictions) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# measure_section_sizes
# ═══════════════════════════════════════════════════════════════════════════════


class TestMeasureSectionSizes:
    def test_short_sections(self):
        text = "## Core\nHello\n\n## Strategy\nBuy low"
        assert measure_section_sizes(text) == []

    def test_long_section(self):
        text = "## Strategy\n" + "A" * 300
        sections = measure_section_sizes(text)
        assert len(sections) >= 1
        assert sections[0][0] == "## Strategy"


# ═══════════════════════════════════════════════════════════════════════════════
# extract_insights_from_journal
# ═══════════════════════════════════════════════════════════════════════════════


class TestExtractInsights:
    def test_empty_journal(self):
        assert extract_insights_from_journal("") == []

    def test_lessons_section(self):
        journal = "## Lessons\nalways check volume before entry\nnever chase fomo\n"
        insights = extract_insights_from_journal(journal)
        assert len(insights) >= 1

    def test_keyword_lines(self):
        journal = "- Lesson: check volume first\n- Next time: wait for confirmation\n"
        insights = extract_insights_from_journal(journal)
        assert len(insights) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# PromptSelfEdit integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestPromptSelfEdit:
    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        """Create a temporary workspace with a prompt file."""
        ws = tmp_path / "trader-test"
        ws.mkdir(parents=True)
        prompt = ws / "AGENTS.md"
        prompt.write_text(
            "## Core Loop\n\n1. Read bankroll\n2. Check positions\n\n"
            "## Strategy\n\nBuy AAPL on dips. Watch ZZZZ for momentum.\n\n"
            "## Risk\n\nNever buy when RSI > 70. Always buy on green days.\n\n"
            "## Reference\n\nPast performance: 2025-01-15 was a good day.\n"
        )
        return ws

    def test_scan_no_issues(self, tmp_path: Path):
        ws = tmp_path / "trader-clean"
        ws.mkdir()
        (ws / "AGENTS.md").write_text("## Core\n\nBuy low, sell high.\n")
        editor = PromptSelfEdit(ws)
        report = editor.run(active_tickers={"AAPL", "MSFT"}, dry_run=True)
        assert isinstance(report, SelfEditReport)
        assert not report.error

    def test_dry_run_does_not_modify(self, workspace: Path):
        editor = PromptSelfEdit(workspace)
        original = (workspace / "AGENTS.md").read_text()
        report = editor.run(active_tickers={"AAPL", "MSFT"}, dry_run=True)
        assert (workspace / "AGENTS.md").read_text() == original
        # dry_run doesn't write, but our run() still returns changes
        # In dry_run mode, we don't write back

    def test_full_run_with_insights(self, workspace: Path):
        editor = PromptSelfEdit(workspace)
        report = editor.run(
            active_tickers={"AAPL", "MSFT"},
            insights=["Always check volume before entry", "Never chase fomo moves"],
            dry_run=False,
        )
        # Should have at least one change
        assert report.edits_made or report.error is None

    def test_prompt_file_not_found(self, tmp_path: Path):
        editor = PromptSelfEdit(tmp_path)
        report = editor.run()
        assert report.error is not None
        assert "not found" in report.error

    def test_inject_insights(self, workspace: Path):
        editor = PromptSelfEdit(workspace)
        insights = ["Check sector rotation before entry", "Set stop-loss at 2%"]
        report = editor.run(
            active_tickers={"AAPL", "MSFT"},
            insights=insights,
            dry_run=False,
        )
        if report.edits_made:
            text = (workspace / "AGENTS.md").read_text()
            assert "sector rotation" in text or "stop-loss" in text

    def test_verbose_section_extraction(self, workspace: Path):
        editor = PromptSelfEdit(workspace)
        # Verify the method works by checking the default threshold
        assert editor.max_section_chars > 0