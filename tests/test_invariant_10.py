"""Tests for scripts/check_risk_prompt_consistency.py — Invariant #10 CI enforcement."""

import pytest
import tempfile
import yaml
from pathlib import Path

from scripts.check_risk_prompt_consistency import (
    parse_prompt,
    check_consistency,
    PromptParams,
    RuleCheck,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Prompt parsing tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestParsePrompt:
    def test_parse_kairos_style(self):
        text = """
        Strategy: momentum on liquid stocks.
        - Position size: 2% of equity per trade
        - Stop loss: 3% — give trades room to breathe
        - Confidence threshold: 0.3 — you're here to generate data
        thesis MUST be 20+ chars.
        signals_used must have at least 1 entry.
        """
        pp = parse_prompt(text, "kairos")
        assert pp.position_size_pct == 0.02
        assert pp.stop_loss_pct == 0.03
        assert pp.conviction_threshold == 0.3
        assert pp.requires_thesis is True
        assert pp.requires_signals is True

    def test_parse_aldridge_style(self):
        text = """
        - Max risk per trade: 1-2% of portfolio value.
        - Stop loss: required on every trade. Firm policy.
        THESIS MUST be 20+ chars.
        signals_used MUST have at least 1 entry.
        """
        pp = parse_prompt(text, "aldridge")
        assert pp.position_size_pct == 0.02  # takes max of range
        assert pp.stop_loss_pct is None  # no specific %
        assert pp.requires_stop_loss is True  # "required on every trade"
        assert pp.conviction_threshold is None
        assert pp.requires_thesis is True
        assert pp.requires_signals is True

    def test_parse_stonks_style(self):
        text = """
        - Sizing: 2-3% of equity per trade. Aggressive but smart.
        - Confidence threshold: 0.3 — you're confident by nature
        - Stop loss: Mandatory.
        """
        pp = parse_prompt(text, "stonks")
        assert pp.position_size_pct == 0.03  # max of 2-3% range
        assert pp.stop_loss_pct is None
        assert pp.conviction_threshold == 0.3

    def test_parse_no_matches(self):
        """Prompt with no recognizable parameters → empty values."""
        text = "Just a random strategy with no structured params."
        pp = parse_prompt(text, "test")
        assert pp.position_size_pct is None
        assert pp.conviction_threshold is None
        assert pp.stop_loss_pct is None
        assert pp.requires_stop_loss is False

    def test_parse_position_size_single(self):
        pp = parse_prompt("Position size: 5% of equity per trade", "test")
        assert pp.position_size_pct == 0.05

    def test_parse_conviction_variations(self):
        pp = parse_prompt("Confidence threshold: 0.35", "test")
        assert pp.conviction_threshold == 0.35

        pp2 = parse_prompt("Conviction: 0.25 — generate data", "test")
        assert pp2.conviction_threshold == 0.25

    def test_parse_stop_loss_required_phrases(self):
        for phrase in [
            "Stop loss: required on every trade",
            "stop loss: mandatory",
            "Stop loss: must be set",
        ]:
            pp = parse_prompt(phrase, "test")
            assert pp.requires_stop_loss is True, f"Failed for: {phrase}"


# ═══════════════════════════════════════════════════════════════════════════════
# Consistency check tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckConsistency:
    @pytest.fixture
    def risk_config(self):
        return {
            "gates": {"require_conviction": 0.3},
            "sizing": {"risk_per_trade_pct": 0.03, "min_position_value": 500, "max_position_value": 5000},
            "position": {"max_position_pct": 0.25, "max_sector_pct": 0.30},
            "stop_loss": {"default_pct": 0.05},
        }

    def test_all_match(self, risk_config):
        """All traders have params within risk limits."""
        prompts = {
            "kairos": PromptParams(
                trader="kairos", position_size_pct=0.02,
                conviction_threshold=0.3, stop_loss_pct=0.03,
            ),
            "stonks": PromptParams(
                trader="stonks", position_size_pct=0.03,
                conviction_threshold=0.3, stop_loss_pct=0.05,
            ),
        }
        checks = check_consistency(risk_config, prompts)
        assert all(c.passed for c in checks), [
            c.detail for c in checks if not c.passed
        ]

    def test_conviction_too_low_fails(self, risk_config):
        """Prompt conviction below gate minimum → failure."""
        prompts = {
            "kairos": PromptParams(
                trader="kairos", conviction_threshold=0.2,
            ),
        }
        checks = check_consistency(risk_config, prompts)
        assert not checks[0].passed
        assert "0.2" in checks[0].detail

    def test_position_above_cap_fails(self, risk_config):
        """Prompt sizing above gate cap → failure."""
        prompts = {
            "stonks": PromptParams(
                trader="stonks", position_size_pct=0.05,  # 5% > 3% cap
            ),
        }
        checks = check_consistency(risk_config, prompts)
        assert not checks[0].passed
        assert "5%" in checks[0].detail

    def test_stop_loss_looser_than_default_warns(self, risk_config):
        """Prompt stop loss looser than risk default → failure."""
        prompts = {
            "kairos": PromptParams(
                trader="kairos", stop_loss_pct=0.10,  # 10% > 5% default
            ),
        }
        checks = check_consistency(risk_config, prompts)
        assert not checks[0].passed

    def test_empty_prompt_skips_checks(self, risk_config):
        """Empty prompt (no parsed params) → no checks generated."""
        prompts = {"empty": PromptParams(trader="empty")}
        checks = check_consistency(risk_config, prompts)
        assert len(checks) == 0

    def test_different_trader_separate_checks(self, risk_config):
        """Each trader gets independent checks."""
        prompts = {
            "kairos": PromptParams(
                trader="kairos", position_size_pct=0.02,
                conviction_threshold=0.3, stop_loss_pct=0.03,
            ),
            "stonks": PromptParams(
                trader="stonks", position_size_pct=0.03,
                conviction_threshold=0.3, stop_loss_pct=0.05,
            ),
        }
        checks = check_consistency(risk_config, prompts)
        traders_checked = set(c.trader for c in checks)
        assert "kairos" in traders_checked
        assert "stonks" in traders_checked
        # Kairos: 3 checks (conviction, position, stop); Stonks: 3 checks = 6
        assert len(checks) == 6

    def test_risk_config_missing_keys_graceful(self):
        """Missing risk.yaml keys → treated as 0, skips checks gracefully."""
        risk = {}  # empty config
        prompts = {
            "kairos": PromptParams(
                trader="kairos", position_size_pct=0.02,
                conviction_threshold=0.3,
            ),
        }
        checks = check_consistency(risk, prompts)
        # With no risk values, checks should still complete (effective_cap = 0 → skipped)
        for c in checks:
            assert c.passed or c.risk_value == 0
