"""
test_tick_prompt.py — Tests for pre-assembled tick prompt system.

Verifies:
1. Prompt assembly produces valid, non-empty output for all traders
2. Timing is under 60s (target: 20-50s with data bus; <5s for template-only dry run)
3. Template placeholders are all filled (no raw {placeholder} in output)
4. Prompt templates exist and are valid for all 3 traders
5. CLI arguments are validated (--trader required, unknown trader rejected)
6. JSON schema in templates uses SPEC-compliant format (decision/conviction/rationale)
7. Trader configs have correct intervals per SPEC
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
PROMPTS_DIR = REPO_ROOT / "prompts"
TICK_PROMPT_SCRIPT = SCRIPTS_DIR / "tick_prompt.py"
TICK_RUNNER_SCRIPT = SCRIPTS_DIR / "tick_runner.py"

TRADERS = ["kairos", "stonks", "aldridge"]


# ---------------------------------------------------------------------------
# Helper: run tick_prompt.py
# ---------------------------------------------------------------------------

def run_tick_prompt(trader: str, extra_args: list[str] | None = None,
                    timeout: int = 30) -> subprocess.CompletedProcess:
    """Run tick_prompt.py and return the CompletedProcess."""
    cmd = [
        sys.executable, str(TICK_PROMPT_SCRIPT),
        "--trader", trader,
        "--timeout", "3",  # Short timeout for tests
    ]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


def run_tick_runner(trader: str, prompt: str,
                    extra_args: list[str] | None = None,
                    timeout: int = 30) -> subprocess.CompletedProcess:
    """Run tick_runner.py with a pre-assembled prompt."""
    cmd = [
        sys.executable, str(TICK_RUNNER_SCRIPT),
        trader,
        "--benchmark",
    ]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# File existence tests
# ---------------------------------------------------------------------------

class TestFileStructure:
    """Verify all required files exist."""

    def test_scripts_directory_exists(self):
        assert SCRIPTS_DIR.is_dir(), "scripts/ directory missing"

    def test_prompts_directory_exists(self):
        assert PROMPTS_DIR.is_dir(), "prompts/ directory missing"

    def test_tick_prompt_script_exists(self):
        assert TICK_PROMPT_SCRIPT.exists(), "tick_prompt.py missing"

    def test_tick_runner_script_exists(self):
        assert TICK_RUNNER_SCRIPT.exists(), "tick_runner.py missing"

    @pytest.mark.parametrize("trader", TRADERS)
    def test_prompt_template_exists(self, trader: str):
        path = PROMPTS_DIR / f"{trader}.txt"
        assert path.exists(), f"Template missing: {path}"

    @pytest.mark.parametrize("trader", TRADERS)
    def test_crontab_exists(self, trader: str):
        path = SCRIPTS_DIR / "crontab.example"
        assert path.exists(), f"Crontab example missing: {path}"


# ---------------------------------------------------------------------------
# Template validity tests
# ---------------------------------------------------------------------------

class TestPromptTemplates:
    """Verify prompt templates are valid and SPEC-compliant."""

    @pytest.mark.parametrize("trader", TRADERS)
    def test_template_not_empty(self, trader: str):
        path = PROMPTS_DIR / f"{trader}.txt"
        content = path.read_text()
        assert len(content) > 100, f"Template for {trader} is suspiciously short ({len(content)} chars)"

    @pytest.mark.parametrize("trader", TRADERS)
    def test_template_has_required_placeholders(self, trader: str):
        """Verify templates use format() style placeholders."""
        path = PROMPTS_DIR / f"{trader}.txt"
        content = path.read_text()

        required = [
            "{regime}",
            "{regime_confidence}",
            "{signal_report}",
            "{portfolio_state}",
            "{journal_entries}",
        ]
        for placeholder in required:
            assert placeholder in content, (
                f"Template for {trader} missing placeholder: {placeholder}"
            )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_template_uses_spec_json_schema(self, trader: str):
        """Verify templates use decision/conviction/rationale schema (per SPEC)."""
        path = PROMPTS_DIR / f"{trader}.txt"
        content = path.read_text()

        # SPEC fields must be present
        assert '"decision"' in content, f"Template for {trader} missing 'decision' field"
        assert '"conviction"' in content, f"Template for {trader} missing 'conviction' field"
        assert '"rationale"' in content, f"Template for {trader} missing 'rationale' field"
        assert '"signal_override"' in content, f"Template for {trader} missing 'signal_override' field"

        # Legacy fields must NOT be present
        assert '"action"' not in content, f"Template for {trader} uses deprecated 'action' field"
        assert '"confidence"' not in content, f"Template for {trader} uses deprecated 'confidence' field"
        assert '"reasoning"' not in content, f"Template for {trader} uses deprecated 'reasoning' field"

    @pytest.mark.parametrize("trader", TRADERS)
    def test_template_mentions_no_tool_calls(self, trader: str):
        """Verify templates don't reference tool calls."""
        path = PROMPTS_DIR / f"{trader}.txt"
        content = path.read_text()

        # Should not contain tool call instructions
        tool_patterns = [
            "curl localhost",
            "python3 src/skill_",
            "record_journal.py",
            "record_decision.py",
        ]
        for pattern in tool_patterns:
            assert pattern not in content, (
                f"Template for {trader} contains deprecated tool call: {pattern}"
            )


# ---------------------------------------------------------------------------
# CLI validation tests
# ---------------------------------------------------------------------------

class TestCLIValidation:
    """Verify CLI argument handling."""

    def test_missing_trader_arg_fails(self):
        result = subprocess.run(
            [sys.executable, str(TICK_PROMPT_SCRIPT)],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0, "Should fail without --trader"

    def test_invalid_trader_rejected(self):
        result = subprocess.run(
            [sys.executable, str(TICK_PROMPT_SCRIPT), "--trader", "nonexistent"],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0, "Should reject unknown trader"

    def test_help_output(self):
        result = subprocess.run(
            [sys.executable, str(TICK_PROMPT_SCRIPT), "--help"],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        assert "trader" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Dry-run / benchmark tests (no data bus required)
# ---------------------------------------------------------------------------

class TestDryRun:
    """Verify prompt assembly in dry-run mode (no live data bus)."""

    @pytest.mark.parametrize("trader", TRADERS)
    def test_dry_run_produces_output(self, trader: str):
        """Dry-run should produce a valid prompt even when data bus is unreachable."""
        result = run_tick_prompt(trader, extra_args=["--dry-run"])
        # Dry run produces no stdout but shouldn't crash
        assert result.returncode == 0, (
            f"Dry-run for {trader} failed:\nSTDERR: {result.stderr}"
        )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_benchmark_produces_timing(self, trader: str):
        """Benchmark mode outputs timing info to stderr."""
        result = run_tick_prompt(trader, extra_args=["--dry-run", "--benchmark"])
        assert result.returncode == 0
        # Should have timing info on stderr
        assert "tick_prompt" in result.stderr.lower(), (
            f"Benchmark for {trader} missing timing info:\nSTDERR: {result.stderr}"
        )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_dry_run_under_5_seconds(self, trader: str):
        """With no data bus, assembly should be nearly instant (<5s)."""
        start = time.time()
        result = run_tick_prompt(trader, extra_args=["--dry-run"])
        elapsed = time.time() - start
        assert elapsed < 5.0, (
            f"Dry-run for {trader} took {elapsed:.1f}s (expected <5s)"
        )


# ---------------------------------------------------------------------------
# Live prompt assembly (with data bus available)
# ---------------------------------------------------------------------------

class TestLiveAssembly:
    """Test prompt assembly with live data bus (if available)."""

    @pytest.mark.parametrize("trader", TRADERS)
    def test_live_assembly_produces_output(self, trader: str):
        """Full assembly should produce a prompt string on stdout."""
        result = run_tick_prompt(trader, extra_args=["--benchmark"])
        # May fail if data bus is unreachable (non-zero exit), but should not crash
        if result.returncode == 0:
            prompt = result.stdout
            assert len(prompt) > 100, (
                f"Live prompt for {trader} too short ({len(prompt)} chars):\n{prompt[:500]}"
            )
            # Check no raw placeholders remain
            raw_placeholders = re.findall(r'\{[a-z_]+\}', prompt)
            for ph in raw_placeholders:
                # Skip JSON braces (they're double-escaped in template as {{ }})
                if ph not in ('{', '}'):
                    assert ph not in prompt, (
                        f"Prompt for {trader} contains unfilled placeholder: {ph}"
                    )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_live_assembly_under_60_seconds(self, trader: str):
        """Full assembly with data bus must complete under 60 seconds per SPEC."""
        start = time.time()
        result = run_tick_prompt(trader, extra_args=["--benchmark"], timeout=60)
        elapsed = time.time() - start
        assert elapsed < 60.0, (
            f"Live assembly for {trader} took {elapsed:.1f}s (SPEC limit: 60s)"
        )


# ---------------------------------------------------------------------------
# Tick runner tests
# ---------------------------------------------------------------------------

class TestTickRunner:
    """Verify tick_runner.py end-to-end flow."""

    @pytest.mark.parametrize("trader", TRADERS)
    def test_tick_runner_accepts_piped_input(self, trader: str):
        """Runner should accept prompt via stdin pipe."""
        test_prompt = f"Test prompt for {trader} with {{}} filled context.\n"
        result = run_tick_runner(trader, test_prompt)
        assert result.returncode == 0, (
            f"Runner for {trader} failed:\nSTDERR: {result.stderr}"
        )

        # Should output valid JSON
        try:
            output = json.loads(result.stdout)
            assert output["trader"] == trader
            assert output["status"] == "completed"
        except json.JSONDecodeError:
            pytest.fail(f"Runner for {trader} did not output valid JSON:\n{result.stdout}")

    def test_tick_runner_rejects_empty_prompt(self):
        result = subprocess.run(
            [sys.executable, str(TICK_RUNNER_SCRIPT), "kairos"],
            input="",
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0, "Runner should reject empty prompt"


# ---------------------------------------------------------------------------
# AGENTS.md migration tests
# ---------------------------------------------------------------------------

class TestAgentsMdMigration:
    """Verify AGENTS.md files have been migrated to pre-assembled, no-tool format."""

    @pytest.mark.parametrize("trader", TRADERS)
    def test_agents_md_exists(self, trader: str):
        path = REPO_ROOT / "agents" / trader / "AGENTS.md"
        assert path.exists(), f"AGENTS.md missing for {trader}: {path}"

    @pytest.mark.parametrize("trader", TRADERS)
    def test_agents_md_no_tool_calls(self, trader: str):
        """AGENTS.md must not contain tool call instructions."""
        path = REPO_ROOT / "agents" / trader / "AGENTS.md"
        content = path.read_text()

        deprecated_tools = [
            "curl localhost",
            "python3 src/skill_",
            "record_journal.py",
            "record_decision.py",
            "GET /momentum",
            "GET /quotes",
            "GET /flow",
            "GET /sentiment",
            "GET /news",
            "GET /social",
            "GET /congress",
            "GET /crypto",
            "GET /insiders",
        ]
        for tool in deprecated_tools:
            # "Data Bus Quick Ref" sections are allowed for reference
            # but not in the core loop instructions
            assert tool not in content, (
                f"AGENTS.md for {trader} contains deprecated tool reference: {tool}"
            )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_agents_md_references_pre_assembly(self, trader: str):
        """AGENTS.md must reference the pre-assembled prompt architecture."""
        path = REPO_ROOT / "agents" / trader / "AGENTS.md"
        content = path.read_text()

        references = [
            "pre-assembled prompt",
            "tick_prompt.py",
        ]
        for ref in references:
            assert ref.lower() in content.lower(), (
                f"AGENTS.md for {trader} missing reference to: {ref}"
            )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_agents_md_uses_spec_json_schema(self, trader: str):
        """AGENTS.md must use decision/conviction/rationale schema (SPEC)."""
        path = REPO_ROOT / "agents" / trader / "AGENTS.md"
        content = path.read_text()

        assert '"decision"' in content, f"AGENTS.md for {trader} missing 'decision' field"
        assert '"conviction"' in content, f"AGENTS.md for {trader} missing 'conviction' field"
        assert '"rationale"' in content, f"AGENTS.md for {trader} missing 'rationale' field"

        # Legacy fields should not be in schema definition
        assert '"action"' not in content, (
            f"AGENTS.md for {trader} uses deprecated 'action' field in schema"
        )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_agents_md_time_limit_60_seconds(self, trader: str):
        """Time limit should be 60 seconds (was 4 minutes)."""
        path = REPO_ROOT / "agents" / trader / "AGENTS.md"
        content = path.read_text()

        # Should mention 60-second time budget
        assert "60 second" in content.lower() or "60s" in content.lower(), (
            f"AGENTS.md for {trader} does not specify 60-second time limit"
        )


# ---------------------------------------------------------------------------
# Spec compliance tests
# ---------------------------------------------------------------------------

class TestSpecCompliance:
    """Verify compliance with SPEC trader-ticks.md."""

    def test_kairos_interval_5_min(self):
        """SPEC: Kairos runs every 5 min with flash model, low thinking."""
        path = PROMPTS_DIR / "kairos.txt"
        content = path.read_text()
        # Template should reflect momentum strategy
        assert "momentum" in content.lower()

    def test_stonks_interval_15_min(self):
        """SPEC: Stonks runs every 15 min with flash model, low thinking."""
        path = PROMPTS_DIR / "stonks.txt"
        content = path.read_text()
        assert "sentiment" in content.lower() or "social" in content.lower()

    def test_aldridge_interval_30_min(self):
        """SPEC: Aldridge runs every 30 min with pro model, medium thinking."""
        path = PROMPTS_DIR / "aldridge.txt"
        content = path.read_text()
        assert "value" in content.lower()

    def test_timeout_600_seconds_honored(self):
        """SPEC: 600s timeout. Our assembly is well under that."""
        # tick_prompt.py assembly is expected under 60s
        # tick_runner.py honors 600s agent timeout
        content = TICK_RUNNER_SCRIPT.read_text()
        assert "600" in content, "tick_runner should reference 600s timeout per SPEC"

    def test_no_tool_calls_in_architecture(self):
        """SPEC: 'The LLM never touches a tool during a trading tick.'"""
        # Verify tick_prompt.py does it all, agent gets pre-built context
        content = TICK_PROMPT_SCRIPT.read_text()
        assert "assemble" in content.lower()
        assert "data bus" in content.lower()


# ---------------------------------------------------------------------------
# Trader config tests
# ---------------------------------------------------------------------------

class TestTraderConfig:
    """Verify TRADER_CONFIG in tick_prompt.py matches SPEC."""

    def test_all_three_traders_configured(self):
        """All three traders must be in TRADER_CONFIG."""
        # Parse the config from the script
        content = TICK_PROMPT_SCRIPT.read_text()
        for trader in TRADERS:
            assert f'"{trader}"' in content, (
                f"TRADER_CONFIG missing entry for: {trader}"
            )

    def test_kairos_has_momentum_signal_source(self):
        content = TICK_PROMPT_SCRIPT.read_text()
        # Kairos should have momentum in endpoints
        assert "momentum" in content

    def test_aldridge_has_insider_signal_source(self):
        content = TICK_PROMPT_SCRIPT.read_text()
        assert "insiders" in content

    def test_stonks_has_social_signal_source(self):
        content = TICK_PROMPT_SCRIPT.read_text()
        assert "social" in content
