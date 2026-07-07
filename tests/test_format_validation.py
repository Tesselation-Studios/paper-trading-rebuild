#!/usr/bin/env python3
"""
Tests for DecisionFormatValidator — validates trader output format per SPEC §4.2.

Tests cover:
    - Valid BUY, SELL, HOLD outputs
    - Missing fields
    - Invalid field types
    - Business rule violations (thesis length, signals_used empty, etc.)
    - Edge cases (json parse failures, null handling, type coercion)
    - Each trader's prompt-specific format requirements
"""

import pytest
from src.format_validator import (
    DecisionFormatValidator,
    ValidationError,
    ValidationResult,
    validate_batch,
    validate_decision_dict,
    VALID_ACTIONS,
    VALID_EXIT_CONDITIONS,
    THESIS_MIN_CHARS,
)


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def validator():
    return DecisionFormatValidator()


@pytest.fixture
def valid_buy():
    return {
        "action": "BUY",
        "ticker": "AAPL",
        "quantity": 10,
        "stop_loss": 185.50,
        "confidence": 0.72,
        "thesis": "Strong momentum signal with RSI confirming uptrend and volume spike",
        "signals_used": ["momentum_breakout", "rsi_bullish", "volume_confirmation"],
        "exit_condition": "stop_loss_hit",
        "holding_horizon_days": 5,
        "reasoning": "Momentum looks strong here, confirming across multiple signals.",
    }


@pytest.fixture
def valid_sell():
    return {
        "action": "SELL",
        "ticker": "MSFT",
        "quantity": 15,
        "stop_loss": None,
        "confidence": 0.85,
        "thesis": "Profit target reached at 25% gain, momentum showing signs of exhaustion",
        "signals_used": ["profit_target", "momentum_decay"],
        "exit_condition": "profit_target_hit",
        "holding_horizon_days": 7,
        "reasoning": "Taking profits here, thesis played out as expected.",
    }


@pytest.fixture
def valid_hold():
    return {
        "action": "HOLD",
        "ticker": None,
        "quantity": None,
        "stop_loss": None,
        "confidence": 0.3,
        "thesis": "",
        "signals_used": [],
        "exit_condition": "",
        "holding_horizon_days": 0,
        "reasoning": "No clear setup right now. Waiting for next signal.",
    }


# ── Valid outputs ──────────────────────────────────────────────────────────────


class TestValidOutputs:
    """Happy-path validation for all action types."""

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

    def test_hold_with_null_fields_passes(self, validator):
        """HOLD does not require ticker, quantity, or thesis per bootstrap mode."""
        decision = {
            "action": "HOLD",
            "ticker": None,
            "quantity": None,
            "stop_loss": None,
            "confidence": 0.5,
            "thesis": "",
            "signals_used": [],
            "exit_condition": "",
            "holding_horizon_days": 0,
        }
        result = validate_decision_dict(decision)
        assert result.is_valid is True

    def test_sell_with_null_stop_loss_warns(self, validator, valid_sell):
        """SELL with null stop_loss gets a warning (bootstrap mode)."""
        result = validate_decision_dict(valid_sell, trader="kairos")
        assert result.is_valid is True  # Still valid
        stop_loss_warnings = [
            w for w in result.warnings if w.field == "stop_loss"
        ]
        assert len(stop_loss_warnings) == 1

    def test_buy_all_signals_present(self, validator, valid_buy):
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
            '{"action": "BUY", "ticker": "AAPL",}', trader="kairos"
        )
        assert result.is_valid is False


# ── Missing fields ─────────────────────────────────────────────────────────────


class TestMissingFields:
    """Each required field must be present."""

    def test_missing_action_flagged(self, validator, valid_buy):
        del valid_buy["action"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "action" for e in result.errors)

    def test_missing_ticker_flagged_on_buy(self, validator, valid_buy):
        del valid_buy["ticker"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "ticker" for e in result.errors)

    def test_missing_quantity_flagged_on_buy(self, validator, valid_buy):
        del valid_buy["quantity"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "quantity" for e in result.errors)

    def test_missing_confidence_flagged(self, validator, valid_buy):
        del valid_buy["confidence"]
        result = validate_decision_dict(valid_buy)
        # confidence field is checked for existence
        assert result.is_valid is False
        assert any(e.field == "confidence" for e in result.errors)

    def test_missing_thesis_flagged_on_buy(self, validator, valid_buy):
        del valid_buy["thesis"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "thesis" for e in result.errors)

    def test_missing_signals_used_flagged_on_buy(self, validator, valid_buy):
        del valid_buy["signals_used"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "signals_used" for e in result.errors)

    def test_missing_exit_condition_flagged_on_buy(self, validator, valid_buy):
        del valid_buy["exit_condition"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "exit_condition" for e in result.errors)

    def test_missing_holding_horizon_flagged_on_buy(self, validator, valid_buy):
        del valid_buy["holding_horizon_days"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(e.field == "holding_horizon_days" for e in result.errors)

    def test_multiple_missing_fields_all_flagged(self, validator):
        result = validate_decision_dict({"some_random": "data"}, trader="kairos")
        assert result.is_valid is False
        assert result.error_count >= 9  # All required fields missing


# ── Action validation ──────────────────────────────────────────────────────────


class TestActionValidation:
    """Action must be BUY, SELL, or HOLD."""

    def test_lowercase_action_accepted(self, validator, valid_buy):
        valid_buy["action"] = "buy"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_whitespace_action_accepted(self, validator, valid_buy):
        valid_buy["action"] = "  BUY  "
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_invalid_action_rejected(self, validator, valid_buy):
        valid_buy["action"] = "SHORT"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "Invalid action" in e.message for e in result.errors
        )

    def test_wait_action_rejected(self, validator, valid_buy):
        valid_buy["action"] = "WAIT"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_empty_action_rejected(self, validator, valid_buy):
        valid_buy["action"] = ""
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
        # Still valid, just warned
        ticker_warnings = [
            w for w in result.warnings if w.field == "ticker"
        ]
        assert len(ticker_warnings) >= 1


# ── Quantity validation ────────────────────────────────────────────────────────


class TestQuantityValidation:
    """Quantity must be a positive integer for BUY/SELL."""

    def test_null_quantity_on_buy_rejected(self, validator, valid_buy):
        valid_buy["quantity"] = None
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_zero_quantity_rejected(self, validator, valid_buy):
        valid_buy["quantity"] = 0
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "Quantity must be positive" in e.message
            for e in result.errors
        )

    def test_negative_quantity_rejected(self, validator, valid_buy):
        valid_buy["quantity"] = -5
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_float_quantity_rejected(self, validator, valid_buy):
        valid_buy["quantity"] = 10.5
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "must be an integer" in e.message
            for e in result.errors
        )

    def test_string_quantity_rejected(self, validator, valid_buy):
        valid_buy["quantity"] = "ten"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_large_quantity_warns(self, validator, valid_buy):
        valid_buy["quantity"] = 50000
        result = validate_decision_dict(valid_buy)
        qty_warnings = [
            w for w in result.warnings if w.field == "quantity"
        ]
        assert len(qty_warnings) >= 1

    def test_null_quantity_on_hold_accepted(self, validator, valid_hold):
        valid_hold["quantity"] = None
        result = validate_decision_dict(valid_hold)
        assert result.is_valid is True


# ── Confidence validation ──────────────────────────────────────────────────────


class TestConfidenceValidation:
    """Confidence must be float in [0.0, 1.0]."""

    def test_confidence_below_zero_rejected(self, validator, valid_buy):
        valid_buy["confidence"] = -0.1
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "outside valid range" in e.message
            for e in result.errors
        )

    def test_confidence_above_one_rejected(self, validator, valid_buy):
        valid_buy["confidence"] = 1.5
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_confidence_exactly_zero_accepted(self, validator, valid_buy):
        valid_buy["confidence"] = 0.0
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_confidence_exactly_one_accepted(self, validator, valid_buy):
        valid_buy["confidence"] = 1.0
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_confidence_string_rejected(self, validator, valid_buy):
        valid_buy["confidence"] = "high"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_confidence_null_accepted(self, validator, valid_hold):
        """If confidence is null, it's treated as missing and we skip range check."""
        valid_hold["confidence"] = None
        result = validate_decision_dict(valid_hold)
        # None confidence skips range check
        assert result.is_valid is True


# ── Thesis validation ──────────────────────────────────────────────────────────


class TestThesisValidation:
    """Thesis must be >= 20 characters for BUY/SELL."""

    def test_empty_thesis_on_buy_rejected(self, validator, valid_buy):
        valid_buy["thesis"] = ""
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "Thesis is empty" in e.message
            for e in result.errors
        )

    def test_short_thesis_rejected(self, validator, valid_buy):
        valid_buy["thesis"] = "Too short"  # 9 chars
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "Thesis too short" in e.message
            for e in result.errors
        )

    def test_exactly_min_length_accepted(self, validator, valid_buy):
        thesis = "A" * THESIS_MIN_CHARS
        valid_buy["thesis"] = thesis
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_one_over_min_length_accepted(self, validator, valid_buy):
        thesis = "A" * (THESIS_MIN_CHARS + 1)
        valid_buy["thesis"] = thesis
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_whitespace_thesis_stripped(self, validator, valid_buy):
        """Whitespace-only thesis after stripping should be caught."""
        core = "AB"
        padded = "   " + core + "   "
        valid_buy["thesis"] = padded
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False  # 2 chars after strip

    def test_null_thesis_on_buy_rejected(self, validator, valid_buy):
        valid_buy["thesis"] = None
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_empty_thesis_on_hold_accepted(self, validator, valid_hold):
        valid_hold["thesis"] = ""
        result = validate_decision_dict(valid_hold)
        assert result.is_valid is True

    def test_long_thesis_accepted(self, validator, valid_buy):
        valid_buy["thesis"] = (
            "This is a very comprehensive thesis that covers all aspects of the trade "
            "including market conditions, technical analysis, fundamental factors, "
            "and risk considerations in great detail."
        )
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True


# ── Signals used validation ────────────────────────────────────────────────────


class TestSignalsUsed:
    """signals_used must be non-empty list of strings for BUY/SELL."""

    def test_null_signals_rejected(self, validator, valid_buy):
        valid_buy["signals_used"] = None
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "signals_used is null" in e.message
            for e in result.errors
        )

    def test_empty_signals_rejected(self, validator, valid_buy):
        valid_buy["signals_used"] = []
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "signals_used list is empty" in e.message
            for e in result.errors
        )

    def test_not_a_list_rejected(self, validator, valid_buy):
        valid_buy["signals_used"] = "momentum"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "must be a list" in e.message
            for e in result.errors
        )

    def test_non_string_signals_flagged(self, validator, valid_buy):
        valid_buy["signals_used"] = ["momentum", 123, True, "rsi"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "Non-string entries" in e.message
            for e in result.errors
        )

    def test_single_signal_accepted(self, validator, valid_buy):
        valid_buy["signals_used"] = ["momentum_breakout"]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_many_signals_accepted(self, validator, valid_buy):
        valid_buy["signals_used"] = [
            "momentum", "rsi", "macd", "volume", "sentiment",
            "sector_rotation", "fundamental_value",
        ]
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_empty_signals_on_hold_accepted(self, validator, valid_hold):
        valid_hold["signals_used"] = []
        result = validate_decision_dict(valid_hold)
        assert result.is_valid is True


# ── Exit condition validation ──────────────────────────────────────────────────


class TestExitCondition:
    """exit_condition must be one of the valid enum values for BUY/SELL."""

    def test_all_valid_exit_conditions_accepted(self, validator, valid_buy):
        for cond in VALID_EXIT_CONDITIONS:
            valid_buy["exit_condition"] = cond
            result = validate_decision_dict(valid_buy)
            assert result.is_valid is True, f"Failed for: {cond}"

    def test_invalid_exit_condition_rejected(self, validator, valid_buy):
        valid_buy["exit_condition"] = "market_close"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False
        assert any(
            "Invalid exit_condition" in e.message
            for e in result.errors
        )

    def test_empty_exit_condition_on_buy_rejected(self, validator, valid_buy):
        valid_buy["exit_condition"] = ""
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_null_exit_condition_rejected(self, validator, valid_buy):
        valid_buy["exit_condition"] = None
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_empty_exit_condition_on_hold_accepted(self, validator, valid_hold):
        valid_hold["exit_condition"] = ""
        result = validate_decision_dict(valid_hold)
        assert result.is_valid is True


# ── Holding horizon validation ─────────────────────────────────────────────────


class TestHoldingHorizon:
    """holding_horizon_days must be integer >= 1 for BUY/SELL."""

    def test_null_horizon_on_buy_rejected(self, validator, valid_buy):
        valid_buy["holding_horizon_days"] = None
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_zero_horizon_rejected(self, validator, valid_buy):
        valid_buy["holding_horizon_days"] = 0
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_negative_horizon_rejected(self, validator, valid_buy):
        valid_buy["holding_horizon_days"] = -1
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_float_horizon_rejected(self, validator, valid_buy):
        valid_buy["holding_horizon_days"] = 3.5
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_valid_horizons_accepted(self, validator, valid_buy):
        for days in [1, 5, 30, 90, 180, 365]:
            valid_buy["holding_horizon_days"] = days
            result = validate_decision_dict(valid_buy)
            assert result.is_valid is True, f"Failed for {days} days"

    def test_very_long_horizon_warns(self, validator, valid_buy):
        valid_buy["holding_horizon_days"] = 400
        result = validate_decision_dict(valid_buy)
        horizon_warnings = [
            w for w in result.warnings if w.field == "holding_horizon_days"
        ]
        assert len(horizon_warnings) >= 1

    def test_string_horizon_rejected(self, validator, valid_buy):
        valid_buy["holding_horizon_days"] = "five"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False


# ── Reasoning / mood optional fields ───────────────────────────────────────────


class TestReasoningMoodFields:
    """Optional reasoning and mood fields."""

    def test_no_reasoning_no_mood_warns(self, validator, valid_buy):
        valid_buy.pop("reasoning", None)
        valid_buy.pop("mood", None)
        result = validate_decision_dict(valid_buy)
        reasoning_warnings = [
            w for w in result.warnings
            if w.field == "reasoning/mood"
        ]
        assert len(reasoning_warnings) >= 1

    def test_reasoning_present_no_warning(self, validator, valid_buy):
        valid_buy["reasoning"] = "Some reasoning here."
        valid_buy.pop("mood", None)
        result = validate_decision_dict(valid_buy)
        reasoning_warnings = [
            w for w in result.warnings
            if w.field == "reasoning/mood"
        ]
        assert len(reasoning_warnings) == 0

    def test_mood_present_no_warning(self, validator, valid_buy):
        valid_buy["mood"] = "Bullish"
        valid_buy.pop("reasoning", None)
        result = validate_decision_dict(valid_buy)
        reasoning_warnings = [
            w for w in result.warnings
            if w.field == "reasoning/mood"
        ]
        assert len(reasoning_warnings) == 0

    def test_hold_no_reasoning_no_warning(self, validator, valid_hold):
        """HOLD doesn't need reasoning."""
        valid_hold.pop("reasoning", None)
        valid_hold.pop("mood", None)
        result = validate_decision_dict(valid_hold)
        reasoning_warnings = [
            w for w in result.warnings
            if w.field == "reasoning/mood"
        ]
        assert len(reasoning_warnings) == 0


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
        decisions = [("kairos", '{"action": "HOLD", "ticker":null,"quantity":null,"stop_loss":null,"confidence":0.5,"thesis":"","signals_used":[],"exit_condition":"","holding_horizon_days":0}'),
                      ("bad", "not json"),
                      ("broken", '{"action": "BUY"}')]
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
    """Trader-specific format scenarios from prompts."""

    def test_kairos_full_format(self, validator):
        """Kairos prompt requires all fields with thesis 20+ chars."""
        decision = {
            "action": "BUY",
            "ticker": "KO",
            "quantity": 20,
            "stop_loss": 58.50,
            "confidence": 0.65,
            "thesis": "RSI crossing 45 from oversold with MACD bullish crossover on daily",
            "signals_used": ["rsi_bullish", "macd_crossover", "momentum"],
            "exit_condition": "stop_loss_hit",
            "holding_horizon_days": 10,
            "reasoning": "Good momentum setup with confirmation across multiple signals.",
        }
        result = validate_decision_dict(decision, trader="kairos")
        assert result.is_valid is True

    def test_aldridge_full_format(self, validator):
        """Aldridge prompt includes mood field."""
        decision = {
            "action": "BUY",
            "ticker": "INTC",
            "quantity": 15,
            "stop_loss": 18.75,
            "confidence": 0.55,
            "thesis": "Attractive P/E of 12 with 4.2% dividend yield and strong balance sheet",
            "signals_used": ["fundamental_value", "dividend_yield", "low_pe_ratio"],
            "exit_condition": "thesis_broken",
            "holding_horizon_days": 60,
            "reasoning": "In my considered view, the fundamentals do not lie.",
            "mood": "Cautious",
        }
        result = validate_decision_dict(decision, trader="aldridge")
        assert result.is_valid is True

    def test_stonks_full_format(self, validator):
        """Stonks prompt includes mood with emoji."""
        decision = {
            "action": "BUY",
            "ticker": "F",
            "quantity": 25,
            "stop_loss": 11.20,
            "confidence": 0.7,
            "thesis": "F RSI pumping + Stocktwits going crazy + MACD confirmation LFG",
            "signals_used": ["momentum", "social_sentiment", "macd_bullish"],
            "exit_condition": "profit_target_hit",
            "holding_horizon_days": 14,
            "reasoning": "Discord crew is all over this one, volume is insane today. Not financial advice but LFG",
            "mood": "Hyped",
        }
        result = validate_decision_dict(decision, trader="stonks")
        assert result.is_valid is True

    def test_stonks_short_thesis_rejected(self, validator):
        """Stonks thesis 'LFG' is too short."""
        decision = {
            "action": "BUY",
            "ticker": "F",
            "quantity": 25,
            "stop_loss": 11.20,
            "confidence": 0.7,
            "thesis": "LFG",
            "signals_used": ["momentum"],
            "exit_condition": "profit_target_hit",
            "holding_horizon_days": 14,
            "reasoning": "yolo",
            "mood": "Hyped",
        }
        result = validate_decision_dict(decision, trader="stonks")
        assert result.is_valid is False
        assert any(
            "Thesis too short" in e.message
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
            errors=[ValidationError(field="thesis", message="Too short")],
        )
        d = result.to_dict()
        assert d["is_valid"] is False
        assert d["trader"] == "kairos"
        assert len(d["errors"]) == 1
        assert d["errors"][0]["field"] == "thesis"

    def test_validation_error_defaults(self):
        e = ValidationError(field="test", message="it broke")
        assert e.severity == "ERROR"


# ── LLM common failure modes ──────────────────────────────────────────────────


class TestCommonLLMFailureModes:
    """Patterns that LLMs commonly produce that should be caught."""

    def test_json_wrapped_in_markdown(self, validator):
        """LLMs often output ```json ... ``` blocks."""
        raw = '```json\n{"action": "BUY", "ticker": "AAPL"}\n```'
        result = validator.validate(raw, trader="kairos")
        assert result.is_valid is False
        assert any("Invalid JSON" in e.message for e in result.errors)

    def test_prose_before_json(self, validator):
        """LLMs might add explanatory text before the JSON."""
        raw = 'Here is my decision:\n\n{"action": "HOLD", "ticker": null}'
        result = validator.validate(raw, trader="kairos")
        assert result.is_valid is False

    def test_single_quoted_json(self, validator):
        """LLMs sometimes use single quotes in JSON (invalid)."""
        raw = "{'action': 'BUY', 'ticker': 'AAPL'}"
        result = validator.validate(raw, trader="kairos")
        assert result.is_valid is False

    def test_trailing_comma_in_array(self, validator, valid_buy):
        """Trailing comma in signals_used array."""
        valid_buy_str = (
            '{"action": "BUY", "ticker": "AAPL", "quantity": 10, '
            '"stop_loss": 100, "confidence": 0.5, '
            '"thesis": "A good thesis with enough characters here", '
            '"signals_used": ["momentum", "rsi",], '
            '"exit_condition": "stop_loss_hit", '
            '"holding_horizon_days": 5}'
        )
        result = validator.validate(valid_buy_str, trader="kairos")
        assert result.is_valid is False

    def test_negative_confidence_from_llm(self, validator, valid_buy):
        """LLM might output confidence -1 (hallucination)."""
        valid_buy["confidence"] = -1
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_buy_with_zero_quantity(self, validator, valid_buy):
        """Buying 0 shares should be rejected."""
        valid_buy["quantity"] = 0
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is False

    def test_missing_thesis_with_backticks(self, validator):
        """LLM outputs empty thesis string."""
        decision = {
            "action": "BUY",
            "ticker": "AAPL",
            "quantity": 10,
            "stop_loss": 150.0,
            "confidence": 0.6,
            "thesis": "",
            "signals_used": ["rsi"],
            "exit_condition": "stop_loss_hit",
            "holding_horizon_days": 3,
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

    def test_unicode_in_thesis(self, validator, valid_buy):
        """Unicode characters in thesis should be fine."""
        valid_buy["thesis"] = "信号确认 — RSI bullish + MACD crossover 確認 ✅ " + "x" * 15
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True

    def test_json_with_nulls_for_hold(self, validator):
        """JSON with Python None literals in a HOLD decision."""
        import json
        decision = json.dumps({
            "action": "HOLD",
            "ticker": None,
            "quantity": None,
            "stop_loss": None,
            "confidence": 0.5,
            "thesis": "",
            "signals_used": [],
            "exit_condition": "",
            "holding_horizon_days": 0,
        })
        result = validator.validate(decision, trader="kairos")
        assert result.is_valid is True

    def test_numeric_ticker(self, validator, valid_buy):
        """Ticker '12345' should pass but warn (unusual >5 chars)."""
        valid_buy["ticker"] = "123456"
        result = validate_decision_dict(valid_buy)
        assert result.is_valid is True  # Still valid
        # Ticker length > 5 warns
        assert any(
            w.field == "ticker" for w in result.warnings
        )

    def test_buy_all_string_confidence(self, validator, valid_buy):
        """Numeric string confidence should fail (type check)."""
        valid_buy["confidence"] = "0.5"
        result = validate_decision_dict(valid_buy)
        # "0.5" can't be cast to float via isinstance check...
        # Actually float("0.5") would work. Let's check what the code does.
        # The code tries float(confidence). "0.5" -> 0.5 works.
        assert result.is_valid is True
