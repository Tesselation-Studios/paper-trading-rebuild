#!/usr/bin/env python3
"""
Tests for DecisionFormatValidator — validates trader output format per SPEC schema.

Tests cover:
    - Valid BUY, SELL, HOLD outputs
    - Missing fields
    - Invalid field types
    - Business rule violations (rationale length, signal_override type, etc.)
    - Edge cases (json parse failures, null handling, type coercion)
"""

import pytest
from src.format_validator import (
    DecisionFormatValidator,
    ValidationError,
    ValidationResult,
    validate_batch,
    validate_decision_dict,
    VALID_ACTIONS,
    RATIONALE_MIN_CHARS,
)


# ── Fixtures ────────────────────────────────────────────────────────────────────


@pytest.fixture
def validator():
    return DecisionFormatValidator()


@pytest.fixture
def valid_buy():
    return {
        "decision": "BUY",
        "ticker": "AAPL",
        "conviction": 0.72,
        "rationale": "Strong momentum signal with RSI confirming uptrend and volume spike confirming breakout.",
        "signal_override": False,
        "override_reason": None,
    }


@pytest.fixture
def valid_sell():
    return {
        "decision": "SELL",
        "ticker": "MSFT",
        "conviction": 0.85,
        "rationale": "Profit target reached at 25% gain, momentum showing signs of exhaustion.",
        "signal_override": False,
        "override_reason": None,
    }


@pytest.fixture
def valid_hold():
    return {
        "decision": "HOLD",
        "ticker": None,
        "conviction": 0.3,
        "rationale": "No clear setup right now. Waiting for next signal.",
        "signal_override": False,
        "override_reason": None,
    }


# ── Valid outputs ──────────────────────────────────────────────────────────────


class TestValidOutputs:
    """Happy-path validation for all decision types."""

    def test_valid_buy_passes(self, validator, valid_buy):
        result = validate_decision_dict(valid_buy, trader="kairos")
        assert result.is_valid is True
        assert result.error_count == 0

    def test_valid_sell_passes(self, validator, valid_sell):
        result = validate_decision_dict(valid_sell, trader="kairos")
        assert result.is_valid is True
        assert result.error_count == 0

    def test_valid_hold_passes(self, validator, valid_hold):
        result = validate_decision_dict(valid_hold, trader="kairos")
        assert result.is_valid is True
        assert result.error_count == 0

    def test_hold_with_null_ticker_passes(self, validator):
        """HOLD allows null ticker."""
        decision = {
            "decision": "HOLD",
            "ticker": None,
            "conviction": 0.5,
            "rationale": "Waiting for better setup.",
            "signal_override": False,
        }
        result = validate_decision_dict(decision)
        assert result.is_valid is True

    def test_all_required_fields_present(self, validator, valid_buy):
        """Every required field must be present."""
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True


# ── JSON parsing errors ────────────────────────────────────────────────────────


class TestJsonParsing:
    """Raw JSON string parsing failures."""

    def test_invalid_json_rejected(self, validator):
        result = validator.validate("not valid json at all", trader="kairos")
        assert result.is_valid is False
        assert result.error_count >= 1
        assert any("Invalid JSON" in e.message for e in result.errors)

    def test_array_rejected(self, validator):
        result = validator.validate("[1, 2, 3]", trader="kairos")
        assert result.is_valid is False
        assert any("object" in e.message.lower() for e in result.errors)

    def test_primitive_rejected(self, validator):
        result = validator.validate('"just a string"', trader="kairos")
        assert result.is_valid is False
        assert any("object" in e.message.lower() for e in result.errors)

    def test_empty_string_rejected(self, validator):
        result = validator.validate("", trader="kairos")
        assert result.is_valid is False

    def test_trailing_comma_rejected(self, validator):
        result = validator.validate(
            '{"decision": "BUY", "ticker": "AAPL",}', trader="kairos"
        )
        assert result.is_valid is False


# ── Missing fields ─────────────────────────────────────────────────────────────


class TestMissingFields:
    """Each required field must be present."""

    def test_missing_decision_flagged(self, validator, valid_buy):
        del valid_buy["decision"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "decision" for e in result.errors)

    def test_missing_ticker_flagged_on_buy(self, validator, valid_buy):
        del valid_buy["ticker"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "ticker" for e in result.errors)

    def test_missing_conviction_flagged(self, validator, valid_buy):
        del valid_buy["conviction"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "conviction" for e in result.errors)

    def test_missing_rationale_flagged_on_buy(self, validator, valid_buy):
        del valid_buy["rationale"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "rationale" for e in result.errors)

    def test_missing_signal_override_flagged(self, validator, valid_buy):
        del valid_buy["signal_override"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "signal_override" for e in result.errors)

    def test_multiple_missing_fields_all_flagged(self, validator):
        result = validate_decision_dict({"some_random": "data"}, trader="kairos")
        assert result.is_valid is False
        assert result.error_count >= 5  # All required fields missing


# ── Decision validation ──────────────────────────────────────────────────────────


class TestDecisionValidation:
    """Decision must be BUY, SELL, or HOLD."""

    def test_lowercase_decision_accepted(self, validator, valid_buy):
        valid_buy["decision"] = "buy"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_whitespace_decision_accepted(self, validator, valid_buy):
        valid_buy["decision"] = "  BUY  "
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_invalid_decision_rejected(self, validator, valid_buy):
        valid_buy["decision"] = "SHORT"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "Invalid decision" in e.message for e in result.errors
        )

    def test_wait_decision_rejected(self, validator, valid_buy):
        valid_buy["decision"] = "WAIT"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_empty_decision_rejected(self, validator, valid_buy):
        valid_buy["decision"] = ""
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False


# ── Ticker validation ──────────────────────────────────────────────────────────


class TestTickerValidation:
    """Ticker must be non-null for BUY/SELL."""

    def test_null_ticker_on_buy_rejected(self, validator, valid_buy):
        valid_buy["ticker"] = None
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "ticker" for e in result.errors)

    def test_empty_ticker_on_buy_rejected(self, validator, valid_buy):
        valid_buy["ticker"] = "   "
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_null_ticker_on_sell_rejected(self, validator, valid_sell):
        valid_sell["ticker"] = None
        result = validate_decision_dict(valid_sell)
        assert result.is_valid is False

    def test_null_ticker_on_hold_accepted(self, validator, valid_hold):
        valid_hold["ticker"] = None
        result = validate_decision_dict(valid_hold)
        assert result.is_valid is True

    def test_long_ticker_warns(self, validator, valid_buy):
        valid_buy["ticker"] = "TOOLONG"
        result = validate_decision_dict(valid_buy)
        ticker_warnings = [
            w for w in result.warnings if w.field == "ticker"
        ]
        assert len(ticker_warnings) >= 1

    def test_hold_with_ticker_warns(self, validator):
        """HOLD with a ticker value should warn."""
        decision = {
            "decision": "HOLD",
            "ticker": "AAPL",
            "conviction": 0.5,
            "rationale": "Watching for entry.",
            "signal_override": False,
        }
        result = validate_decision_dict(decision)
        assert result.is_valid is True
        ticker_warnings = [
            w for w in result.warnings if w.field == "ticker"
        ]
        assert len(ticker_warnings) >= 1


# ── Conviction validation ──────────────────────────────────────────────────────


class TestConvictionValidation:
    """Conviction must be float in [0.0, 1.0]."""

    def test_conviction_below_zero_rejected(self, validator, valid_buy):
        valid_buy["conviction"] = -0.1
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "outside valid range" in e.message
            for e in result.errors
        )

    def test_conviction_above_one_rejected(self, validator, valid_buy):
        valid_buy["conviction"] = 1.5
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_conviction_exactly_zero_accepted(self, validator, valid_buy):
        valid_buy["conviction"] = 0.0
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_conviction_exactly_one_accepted(self, validator, valid_buy):
        valid_buy["conviction"] = 1.0
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_conviction_string_rejected(self, validator, valid_buy):
        valid_buy["conviction"] = "high"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_conviction_missing_rejected(self, validator, valid_buy):
        del valid_buy["conviction"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False


# ── Rationale validation ────────────────────────────────────────────────────────


class TestRationaleValidation:
    """Rationale must be >= 10 characters for BUY/SELL."""

    def test_empty_rationale_on_buy_rejected(self, validator, valid_buy):
        valid_buy["rationale"] = ""
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "Rationale is empty" in e.message
            for e in result.errors
        )

    def test_short_rationale_rejected(self, validator, valid_buy):
        valid_buy["rationale"] = "Too short"  # 9 chars
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "Rationale too short" in e.message
            for e in result.errors
        )

    def test_exactly_min_length_accepted(self, validator, valid_buy):
        rationale = "A" * RATIONALE_MIN_CHARS
        valid_buy["rationale"] = rationale
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_one_over_min_length_accepted(self, validator, valid_buy):
        rationale = "A" * (RATIONALE_MIN_CHARS + 1)
        valid_buy["rationale"] = rationale
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_whitespace_rationale_stripped(self, validator, valid_buy):
        """Whitespace-only rationale after stripping should be caught."""
        core = "AB"
        padded = "   " + core + "   "
        valid_buy["rationale"] = padded
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False  # 2 chars after strip

    def test_null_rationale_on_buy_rejected(self, validator, valid_buy):
        valid_buy["rationale"] = None
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_empty_rationale_on_hold_accepted(self, validator, valid_hold):
        valid_hold["rationale"] = ""
        result = validate_decision_dict(valid_hold)
        assert result.is_valid is True

    def test_long_rationale_accepted(self, validator, valid_buy):
        valid_buy["rationale"] = (
            "This is a comprehensive rationale covering market conditions, "
            "technical analysis results, fundamental factors, "
            "and risk considerations in detail."
        )
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True


# ── Signal override validation ──────────────────────────────────────────────────


class TestSignalOverride:
    """signal_override must be a boolean."""

    def test_null_signal_override_rejected(self, validator, valid_buy):
        valid_buy["signal_override"] = None
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "signal_override" in e.message
            for e in result.errors
        )

    def test_string_signal_override_rejected(self, validator, valid_buy):
        valid_buy["signal_override"] = "true"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "boolean" in e.message
            for e in result.errors
        )

    def test_integer_signal_override_rejected(self, validator, valid_buy):
        valid_buy["signal_override"] = 1
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "boolean" in e.message
            for e in result.errors
        )

    def test_false_signal_override_accepted(self, validator, valid_buy):
        valid_buy["signal_override"] = False
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_true_signal_override_accepted(self, validator, valid_buy):
        valid_buy["signal_override"] = True
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_missing_signal_override_rejected(self, validator, valid_buy):
        del valid_buy["signal_override"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False


# ── Batch validation ───────────────────────────────────────────────────────────


class TestBatchValidation:
    """Validate multiple decisions at once."""

    def test_batch_all_valid(self, validator, valid_buy, valid_sell, valid_hold):
        decisions = [
            ("kairos", valid_buy),
            ("aldridge", valid_sell),
            ("stonks", valid_hold),
        ]
        raw_decisions = [
            (name, __import__("json").dumps(d))
            for name, d in decisions
        ]
        results = validate_batch(raw_decisions)
        assert len(results) == 3
        assert all(r.is_valid for r in results)

    def test_batch_mixed_validity(self):
        decisions = [
            ("kairos", '{"decision": "HOLD", "ticker":null,"conviction":0.5,"rationale":"No clear setup","signal_override":false}'),
            ("bad", "not json"),
            ("broken", '{"decision": "BUY"}'),  # Missing ticker, conviction, rationale, signal_override
        ]
        results = validate_batch(decisions)
        assert len(results) == 3
        assert results[0].is_valid is True
        assert results[1].is_valid is False
        assert results[2].is_valid is False

    def test_empty_batch(self):
        results = validate_batch([])
        assert results == []


# ── Trader-specific scenarios ──────────────────────────────────────────────────


class TestTraderSpecific:
    """Trader-specific format scenarios matching SPEC schema."""

    def test_kairos_full_format(self, validator):
        """Kairos SPEC schema decision."""
        decision = {
            "decision": "BUY",
            "ticker": "KO",
            "conviction": 0.65,
            "rationale": "RSI crossing 45 from oversold with MACD bullish crossover on daily chart. Momentum signal confirmed.",
            "signal_override": False,
            "override_reason": None,
        }
        result = validate_decision_dict(decision, trader="kairos")
        assert result.is_valid is True

    def test_aldridge_full_format(self, validator):
        """Aldridge SPEC schema decision."""
        decision = {
            "decision": "BUY",
            "ticker": "INTC",
            "conviction": 0.55,
            "rationale": "Attractive P/E of 12 with 4.2% dividend yield and strong balance sheet supporting entry.",
            "signal_override": False,
            "override_reason": None,
        }
        result = validate_decision_dict(decision, trader="aldridge")
        assert result.is_valid is True

    def test_stonks_full_format(self, validator):
        """Stonks SPEC schema decision."""
        decision = {
            "decision": "BUY",
            "ticker": "F",
            "conviction": 0.7,
            "rationale": "F RSI pumping with MACD confirmation and volume spike. Momentum play aligning across signals.",
            "signal_override": False,
            "override_reason": None,
        }
        result = validate_decision_dict(decision, trader="stonks")
        assert result.is_valid is True

    def test_stonks_short_rationale_rejected(self, validator):
        """Short rationale should be rejected."""
        decision = {
            "decision": "BUY",
            "ticker": "F",
            "conviction": 0.7,
            "rationale": "LFG",
            "signal_override": False,
        }
        result = validate_decision_dict(decision, trader="stonks")
        assert result.is_valid is False
        assert any(
            "Rationale too short" in e.message
            for e in result.errors
        )


# ── Error info dataclass tests ─────────────────────────────────────────────────


class TestDataclasses:
    """ValidationResult and ValidationError dataclasses."""

    def test_validation_result_properties(self):
        result = ValidationResult(
            is_valid=True,
            trader="test",
            errors=[ValidationError(field="x", message="bad")],
            warnings=[ValidationError(field="y", message="meh")],
        )
        assert result.error_count == 1
        assert result.warning_count == 1

    def test_empty_result(self):
        result = ValidationResult(is_valid=True, trader="test")
        assert result.error_count == 0
        assert result.warning_count == 0

    def test_to_dict(self):
        result = ValidationResult(
            is_valid=False,
            trader="kairos",
            errors=[ValidationError(field="rationale", message="Too short")],
        )
        d = result.to_dict()
        assert d["is_valid"] is False
        assert d["trader"] == "kairos"
        assert len(d["errors"]) == 1
        assert d["errors"][0]["field"] == "rationale"

    def test_validation_error_defaults(self):
        e = ValidationError(field="test", message="it broke")
        assert e.severity == "ERROR"


# ── LLM common failure modes ──────────────────────────────────────────────────


class TestCommonLLMFailureModes:
    """Patterns that LLMs commonly produce that should be caught."""

    def test_json_wrapped_in_markdown(self, validator):
        """LLMs often output ```json ... ``` blocks."""
        raw = '```json\n{"decision": "BUY", "ticker": "AAPL"}\n```'
        result = validator.validate(raw, trader="kairos")
        assert result.is_valid is False
        assert any("Invalid JSON" in e.message for e in result.errors)

    def test_prose_before_json(self, validator):
        """LLMs might add explanatory text before the JSON."""
        raw = 'Here is my decision:\n\n{"decision": "HOLD", "ticker": null}'
        result = validator.validate(raw, trader="kairos")
        assert result.is_valid is False

    def test_single_quoted_json(self, validator):
        """LLMs sometimes use single quotes in JSON (invalid)."""
        raw = "{'decision': 'BUY', 'ticker': 'AAPL'}"
        result = validator.validate(raw, trader="kairos")
        assert result.is_valid is False

    def test_negative_conviction_from_llm(self, validator, valid_buy):
        """LLM might output conviction -1 (hallucination)."""
        valid_buy["conviction"] = -1
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_missing_rationale_with_backticks(self, validator):
        """LLM outputs empty rationale string."""
        decision = {
            "decision": "BUY",
            "ticker": "AAPL",
            "conviction": 0.6,
            "rationale": "",
            "signal_override": False,
        }
        result = validate_decision_dict(decision)
        assert result.is_valid is False


# ── Edge cases ─────────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Unusual but valid or invalid inputs."""

    def test_extra_unknown_fields_ignored(self, validator, valid_buy):
        """Extra fields beyond required shouldn't cause failures."""
        valid_buy["custom_field"] = "something extra"
        valid_buy["another_one"] = 42
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_unicode_in_rationale(self, validator, valid_buy):
        """Unicode characters in rationale should be fine."""
        valid_buy["rationale"] = "\u4fe1\u53f7\u786e\u8ba4 \u2014 RSI bullish + MACD crossover \u78ba\u8a8d \u2705" + "x" * 15
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_json_with_nulls_for_hold(self, validator):
        """JSON with Python None literals in a HOLD decision."""
        import json as _json
        decision = _json.dumps({
            "decision": "HOLD",
            "ticker": None,
            "conviction": 0.5,
            "rationale": "No clear setup. Waiting.",
            "signal_override": False,
            "override_reason": None,
        })
        result = validator.validate(decision, trader="kairos")
        assert result.is_valid is True

    def test_numeric_ticker(self, validator, valid_buy):
        """Ticker '12345' should pass but warn (unusual >5 chars)."""
        valid_buy["ticker"] = "123456"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True
        assert any(
            w.field == "ticker" for w in result.warnings
        )

    def test_string_conviction_accepted(self, validator, valid_buy):
        """Numeric string for conviction should pass (float conversion works)."""
        valid_buy["conviction"] = 0.5
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True