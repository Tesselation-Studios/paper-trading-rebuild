"""
test_prompt_paths.py — Verify canonical prompt templates exist and are valid.

Per SPEC: Prompt templates live in prompts/{trader}.txt within the project repo.
These are STATIC during market hours, edited during nightly sweeps, and filled
at tick time by scripts/tick_prompt.py using Python str.format().

Tests:
1. prompts/ directory exists
2. prompts/{trader}.txt exists for each trader (kairos, aldridge, stonks)
3. Templates are non-empty
4. Templates contain all required placeholders (format-compatible)
5. Templates are valid Python format strings (no KeyError on format())
6. Templates produce valid output when filled with sample data
7. Rendered output contains no raw placeholders
8. Templates contain required content sections
9. JSON schema uses decision/conviction/rationale format (per #147)
10. sync_prompts.sh exists and is executable
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
SCRIPTS_DIR = REPO_ROOT / "scripts"
SYNC_SCRIPT = SCRIPTS_DIR / "sync_prompts.sh"
TICK_PROMPT_SCRIPT = SCRIPTS_DIR / "tick_prompt.py"

TRADERS = ["kairos", "aldridge", "stonks"]

REQUIRED_PLACEHOLDERS = [
    "{regime}",
    "{regime_confidence}",
    "{signal_report}",
    "{portfolio_state}",
    "{journal_entries}",
]

SAMPLE_DATA = {
    "regime": "bull",
    "regime_confidence": "0.85",
    "signal_report": (
        "### momentum\nTop 5 by momentum score:\n"
        "  - AAPL: score=0.92 vol=12.5M\n"
        "  - TSLA: score=0.88 vol=45.2M\n"
    ),
    "portfolio_state": (
        "Account: kairos\nCash: $25,000.00\nBuying Power: $50,000.00\n"
        "\nPositions (2):\n  - AAPL: 100 shares, MV=$19,500.00, P&L=$1,200.00\n"
        "  - MSFT: 50 shares, MV=$21,000.00, P&L=$800.00"
    ),
    "journal_entries": (
        "1. [2026-07-15T14:30:00] [trade] AAPL momentum breakout — entered at 195\n"
        "2. [2026-07-15T14:25:00] [observation] MSFT showing relative strength vs SPY"
    ),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def template_paths():
    """Return dict of trader → template path."""
    return {trader: PROMPTS_DIR / f"{trader}.txt" for trader in TRADERS}


@pytest.fixture
def template_contents(template_paths):
    """Read all templates into memory."""
    contents = {}
    for trader, path in template_paths.items():
        if path.exists():
            contents[trader] = path.read_text()
    return contents


# ---------------------------------------------------------------------------
# Test: prompts/ directory structure
# ---------------------------------------------------------------------------

class TestPromptDirectory:
    """Verify prompts/ directory exists and contains templates."""

    def test_prompts_dir_exists(self):
        """prompts/ directory must exist in repo root."""
        assert PROMPTS_DIR.exists(), (
            f"prompts/ directory not found at {PROMPTS_DIR}. "
            "Per SPEC, prompt templates must live in prompts/{trader}.txt"
        )
        assert PROMPTS_DIR.is_dir(), f"{PROMPTS_DIR} is not a directory"

    @pytest.mark.parametrize("trader", TRADERS)
    def test_template_file_exists(self, trader):
        """Each trader must have prompts/{trader}.txt."""
        path = PROMPTS_DIR / f"{trader}.txt"
        assert path.exists(), (
            f"Missing prompt template: {path}. "
            "Each trader (kairos, aldridge, stonks) must have a template."
        )
        assert path.is_file(), f"{path} is not a regular file"

    @pytest.mark.parametrize("trader", TRADERS)
    def test_template_is_non_empty(self, trader):
        """Templates must contain meaningful content."""
        path = PROMPTS_DIR / f"{trader}.txt"
        if not path.exists():
            pytest.skip(f"{trader}.txt not found")
        content = path.read_text()
        assert len(content) > 500, (
            f"{trader}.txt is too short ({len(content)} chars). "
            "Prompt template must contain strategy, rules, and output schema."
        )

    def test_no_extra_files(self):
        """Only trader templates should be in prompts/."""
        valid_names = {f"{t}.txt" for t in TRADERS}
        for child in PROMPTS_DIR.iterdir():
            assert child.name in valid_names, (
                f"Unexpected file in prompts/: {child.name}. "
                f"Expected only: {sorted(valid_names)}"
            )


# ---------------------------------------------------------------------------
# Test: Placeholders
# ---------------------------------------------------------------------------

class TestPlaceholders:
    """Verify templates contain all required placeholders."""

    @pytest.mark.parametrize("trader", TRADERS)
    def test_all_required_placeholders_present(self, trader, template_contents):
        """Every template must contain all {placeholder} fields."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        for placeholder in REQUIRED_PLACEHOLDERS:
            assert placeholder in content, (
                f"{trader}.txt missing placeholder: {placeholder}. "
                "tick_prompt.py assembles these at runtime."
            )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_no_unfilled_placeholders_after_format(self, trader, template_contents):
        """After .format() with sample data, no raw placeholders remain."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        try:
            result = content.format(**SAMPLE_DATA)
        except KeyError as e:
            pytest.fail(f"{trader}.txt has unrecognized placeholder: {e}")
        # No raw placeholder braces remaining
        remaining = re.findall(r"\{[a-z_]+\}", result)
        assert not remaining, (
            f"{trader}.txt has unfilled placeholders after format: {remaining}"
        )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_format_is_valid_python(self, trader, template_contents):
        """str.format() must not raise errors with valid data."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        try:
            result = content.format(**SAMPLE_DATA)
        except KeyError as e:
            missing_key = str(e).strip("'")
            if missing_key in {
                "regime", "regime_confidence", "signal_report",
                "portfolio_state", "journal_entries",
            }:
                pytest.fail(
                    f"{trader}.txt: required placeholder {e} not in SAMPLE_DATA"
                )
            else:
                pytest.fail(
                    f"{trader}.txt: unexpected placeholder {e} — "
                    "only regime, regime_confidence, signal_report, "
                    "portfolio_state, journal_entries are valid"
                )
        except ValueError as e:
            pytest.fail(f"{trader}.txt format error: {e}")
        else:
            assert len(result) > len(content) * 0.5, (
                f"{trader}.txt: rendered output suspiciously short "
                f"({len(result)} chars vs {len(content)} template)"
            )


# ---------------------------------------------------------------------------
# Test: Content sections
# ---------------------------------------------------------------------------

class TestContentSections:
    """Verify templates contain required content sections."""

    @pytest.mark.parametrize("trader", TRADERS)
    def test_strategy_section_present(self, trader, template_contents):
        """Template must define the trading strategy."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        assert "**Strategy**" in content or "Strategy:" in content, (
            f"{trader}.txt missing strategy description"
        )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_rules_section_present(self, trader, template_contents):
        """Template must define trading rules."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        assert "## Rules" in content, (
            f"{trader}.txt missing Rules section"
        )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_output_section_present(self, trader, template_contents):
        """Template must define output format."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        assert "## Output" in content, (
            f"{trader}.txt missing Output section"
        )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_circuit_breaker_section_present(self, trader, template_contents):
        """Template must document circuit breaker rules."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        assert "Circuit Breaker" in content or "Drawdown" in content, (
            f"{trader}.txt missing circuit breaker / drawdown section"
        )


# ---------------------------------------------------------------------------
# Test: JSON schema (per #147 — decision/conviction/rationale)
# ---------------------------------------------------------------------------

class TestJsonSchema:
    """Verify the JSON schema in templates matches SPEC #147."""

    @pytest.mark.parametrize("trader", TRADERS)
    def test_decision_field_in_schema(self, trader, template_contents):
        """Schema must use 'decision' field (not 'action')."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        assert '"decision"' in content, (
            f"{trader}.txt: JSON schema must use 'decision' (per #147 schema)"
        )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_conviction_field_in_schema(self, trader, template_contents):
        """Schema must use 'conviction' field (not 'confidence')."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        assert '"conviction"' in content, (
            f"{trader}.txt: JSON schema must use 'conviction' (per #147 schema)"
        )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_rationale_field_in_schema(self, trader, template_contents):
        """Schema must use 'rationale' field (not 'reasoning')."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        assert '"rationale"' in content, (
            f"{trader}.txt: JSON schema must use 'rationale' (per #147 schema)"
        )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_signal_override_field_in_schema(self, trader, template_contents):
        """Schema must include signal_override field."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        assert '"signal_override"' in content, (
            f"{trader}.txt: JSON schema must include 'signal_override'"
        )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_no_legacy_fields(self, trader, template_contents):
        """Schema must NOT use deprecated fields (action, confidence, reasoning)."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        # Extract content between ```json and ```
        json_block_match = re.search(
            r"```json\s*\n(.*?)\n```", content, re.DOTALL
        )
        if json_block_match:
            json_block = json_block_match.group(1)
            assert '"action"' not in json_block, (
                f"{trader}.txt: deprecated 'action' field in JSON schema. "
                "Use 'decision' per #147."
            )
            assert '"confidence"' not in json_block, (
                f"{trader}.txt: deprecated 'confidence' field in JSON schema. "
                "Use 'conviction' per #147."
            )
            assert '"reasoning"' not in json_block, (
                f"{trader}.txt: deprecated 'reasoning' field in JSON schema. "
                "Use 'rationale' per #147."
            )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_rendered_template_has_valid_json_braces(self, trader, template_contents):
        """After .format(), the JSON block uses single braces (not doubled)."""
        content = template_contents.get(trader)
        if not content:
            pytest.skip(f"{trader}.txt not found")
        result = content.format(**SAMPLE_DATA)
        # Extract JSON block
        json_block_match = re.search(
            r"```json\s*\n(.*?)\n```", result, re.DOTALL
        )
        if json_block_match:
            json_block = json_block_match.group(1)
            assert "{{" not in json_block, (
                f"{trader}.txt: double braces ({{{{) not resolved in output."
            )
            assert "}}" not in json_block, (
                f"{trader}.txt: double braces (}}}}) not resolved in output."
            )


# ---------------------------------------------------------------------------
# Test: sync_prompts.sh
# ---------------------------------------------------------------------------

class TestSyncScript:
    """Verify the sync script for backward compatibility."""

    def test_sync_script_exists(self):
        """scripts/sync_prompts.sh must exist."""
        assert SYNC_SCRIPT.exists(), (
            f"Missing sync script: {SYNC_SCRIPT}. "
            "Required for backward compat with trading-agent-prompts repo."
        )

    def test_sync_script_executable(self):
        """sync_prompts.sh must be executable."""
        if not SYNC_SCRIPT.exists():
            pytest.skip("sync_prompts.sh not found")
        assert SYNC_SCRIPT.stat().st_mode & 0o111, (
            f"{SYNC_SCRIPT} is not executable"
        )

    def test_sync_script_has_help_flag(self):
        """sync_prompts.sh must support --help."""
        if not SYNC_SCRIPT.exists():
            pytest.skip("sync_prompts.sh not found")
        result = subprocess.run(
            ["bash", str(SYNC_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, (
            f"sync_prompts.sh --help failed: {result.stderr}"
        )
        assert "Usage:" in result.stdout, (
            "sync_prompts.sh --help did not show usage"
        )

    def test_sync_script_dry_run_works(self):
        """sync_prompts.sh --dry-run must not fail."""
        if not SYNC_SCRIPT.exists():
            pytest.skip("sync_prompts.sh not found")
        result = subprocess.run(
            ["bash", str(SYNC_SCRIPT), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # May fail if legacy repo doesn't exist — that's OK, just check it runs
        if result.returncode != 0:
            assert "ERROR" in result.stderr or "not found" in result.stderr, (
                f"sync_prompts.sh --dry-run failed unexpectedly: {result.stderr}"
            )


# ---------------------------------------------------------------------------
# Integration test: verify tick_prompt.py can assemble with these templates
# ---------------------------------------------------------------------------

class TestTickPromptIntegration:
    """Verify tick_prompt.py can read and assemble from the new templates."""

    @pytest.mark.parametrize("trader", TRADERS)
    def test_tick_prompt_template_path_resolves(self, trader):
        """tick_prompt.py PROMPTS_DIR should point to our templates."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "tick_prompt", TICK_PROMPT_SCRIPT
        )
        if spec is None or spec.loader is None:
            pytest.skip("Cannot load tick_prompt module")
        tick_prompt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tick_prompt)
        expected = PROMPTS_DIR / f"{trader}.txt"
        actual = tick_prompt.PROMPTS_DIR / f"{trader}.txt"
        assert actual == expected, (
            f"tick_prompt PROMPTS_DIR mismatch: expected {expected}, got {actual}"
        )

    @pytest.mark.parametrize("trader", TRADERS)
    def test_dry_run_produces_no_template_error(self, trader):
        """tick_prompt.py --trader {trader} --dry-run should not fail on template."""
        if not TICK_PROMPT_SCRIPT.exists():
            pytest.skip("tick_prompt.py not found")
        result = subprocess.run(
            [
                "python3", str(TICK_PROMPT_SCRIPT),
                "--trader", trader, "--dry-run", "--timeout", "2",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        stderr = result.stderr.lower()
        # Template errors would show "template not found" or "keyerror"
        assert "template not found" not in stderr, (
            f"tick_prompt.py could not find template for {trader}: {result.stderr}"
        )
        assert "keyerror" not in stderr, (
            f"tick_prompt.py template format error for {trader}: {result.stderr}"
        )
