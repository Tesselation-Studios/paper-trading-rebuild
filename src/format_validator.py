#!/usr/bin/env python3
"""
Decision Format Validator — validates trader outputs match the required JSON schema.

Per SPEC §4.2, every trader must output a JSON decision with specific fields.
The risk gate enforces format quality (invariant #12 — bootstrap mode downgrades
vetoes to warnings). This validator provides comprehensive format checking that
can be used in CI, nightly validation, and the risk gate itself.

Validates:
    - JSON parseability
    - Required fields present
    - Field types correct
    - Value ranges valid
    - Business rules (thesis ≥ 20 chars, signals_used non-empty, etc.)
"""

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

# ── Constants ──────────────────────────────────────────────────────────────────

VALID_ACTIONS = frozenset({"BUY", "SELL", "HOLD"})
VALID_EXIT_CONDITIONS = frozenset({
    "stop_loss_hit",
    "profit_target_hit",
    "thesis_broken",
    "time_stop",
    "signal_decay",
})
REQUIRED_FIELDS = [
    "action",
    "ticker",
    "quantity",
    "stop_loss",
    "confidence",
    "thesis",
    "signals_used",
    "exit_condition",
    "holding_horizon_days",
]
THESIS_MIN_CHARS = 20
CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0
MIN_HOLDING_DAYS = 1


# ── Data Structures ─────────────────────────────────────────────────────────────

@dataclass
class ValidationError:
    """A single format validation failure."""

    field: str
    message: str
    severity: str = "ERROR"  # ERROR or WARNING


@dataclass
class ValidationResult:
    """Complete format validation result."""

    is_valid: bool
    trader: str
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "trader": self.trader,
            "errors": [
                {"field": e.field, "message": e.message, "severity": e.severity}
                for e in self.errors
            ],
            "warnings": [
                {"field": w.field, "message": w.message, "severity": w.severity}
                for w in self.warnings
            ],
        }


# ── Validator ──────────────────────────────────────────────────────────────────


class DecisionFormatValidator:
    """Validate that a trader's JSON decision matches the required format.

    Usage:
        validator = DecisionFormatValidator()
        result = validator.validate(trader_json_string, trader_name="kairos")

    Per SPEC §4.2 output format:
        {
          "action": "BUY | SELL | HOLD",
          "ticker": "AAPL or null if HOLD",
          "quantity": int,
          "stop_loss": float/dollar,
          "confidence": 0.0-1.0,
          "thesis": "20+ char explanation",
          "signals_used": ["sig1", "sig2"],
          "exit_condition": "stop_loss_hit | profit_target_hit | ...",
          "holding_horizon_days": int
        }
    """

    def __init__(self, thesis_min_chars: int = THESIS_MIN_CHARS):
        self.thesis_min_chars = thesis_min_chars

    def validate(
        self, raw_output: str, trader: str = "unknown"
    ) -> ValidationResult:
        """Validate a raw trader output string.

        Args:
            raw_output: Raw JSON string from trader
            trader: Trader name for error context

        Returns:
            ValidationResult with pass/fail and specific error details
        """
        errors: List[ValidationError] = []
        warnings: List[ValidationError] = []

        # ── Step 1: Parse JSON ──────────────────────────────────────────────
        try:
            decision = json.loads(raw_output)
        except json.JSONDecodeError as e:
            errors.append(
                ValidationError(
                    field="(root)",
                    message=f"Invalid JSON: {e}",
                    severity="ERROR",
                )
            )
            return ValidationResult(
                is_valid=False, trader=trader, errors=errors, warnings=warnings
            )

        if not isinstance(decision, dict):
            errors.append(
                ValidationError(
                    field="(root)",
                    message="Output must be a JSON object, not array/primitive",
                    severity="ERROR",
                )
            )
            return ValidationResult(
                is_valid=False, trader=trader, errors=errors, warnings=warnings
            )

        # ── Step 2: Required fields ─────────────────────────────────────────
        for field in REQUIRED_FIELDS:
            if field not in decision:
                errors.append(
                    ValidationError(
                        field=field,
                        message=f"Missing required field '{field}'",
                        severity="ERROR",
                    )
                )

        # ── Step 3: Action validation ───────────────────────────────────────
        raw_action = decision.get("action", "")
        if isinstance(raw_action, str):
            action = raw_action.upper().strip()
        else:
            action = str(raw_action).upper().strip()

        if not action:
            errors.append(
                ValidationError(
                    field="action",
                    message="Action is empty or missing. Must be BUY, SELL, or HOLD",
                    severity="ERROR",
                )
            )
        elif action not in VALID_ACTIONS:
            errors.append(
                ValidationError(
                    field="action",
                    message=f"Invalid action '{action}'. Must be BUY, SELL, or HOLD",
                    severity="ERROR",
                )
            )

        is_trade = action in ("BUY", "SELL")

        # ── Step 4: Ticker validation ───────────────────────────────────────
        ticker = decision.get("ticker")
        if is_trade:
            if ticker is None or (isinstance(ticker, str) and not ticker.strip()):
                errors.append(
                    ValidationError(
                        field="ticker",
                        message=f"Ticker required for {action} but was null/empty",
                        severity="ERROR",
                    )
                )
            elif isinstance(ticker, str) and ticker.strip():
                # Warn on suspicious tickers
                if len(ticker.strip()) > 5:
                    warnings.append(
                        ValidationError(
                            field="ticker",
                            message=f"Ticker '{ticker.strip()}' is unusually long (>5 chars)",
                            severity="WARNING",
                        )
                    )

        # ── Step 5: Quantity validation ─────────────────────────────────────
        quantity = decision.get("quantity")
        if is_trade:
            if quantity is None:
                errors.append(
                    ValidationError(
                        field="quantity",
                        message=f"Quantity required for {action} but was null",
                        severity="ERROR",
                    )
                )
            else:
                try:
                    # Must be an actual int, not a float that looks like an int
                    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
                        raise ValueError("not numeric")
                    if isinstance(quantity, float) and quantity != int(quantity):
                        raise ValueError("float with decimals")
                    qty = int(quantity)
                    if qty <= 0:
                        errors.append(
                            ValidationError(
                                field="quantity",
                                message=f"Quantity must be positive, got {qty}",
                                severity="ERROR",
                            )
                        )
                    if qty > 10000:
                        warnings.append(
                            ValidationError(
                                field="quantity",
                                message=f"Quantity {qty} is unusually large",
                                severity="WARNING",
                            )
                        )
                except (ValueError, TypeError):
                    errors.append(
                        ValidationError(
                            field="quantity",
                            message=f"Quantity must be an integer, got {type(quantity).__name__}: {quantity}",
                            severity="ERROR",
                        )
                    )

        # ── Step 6: Stop loss validation ────────────────────────────────────
        stop_loss = decision.get("stop_loss")
        if is_trade and stop_loss is None:
            # During bootstrap, this is a warning; after bootstrap, it's an error
            # Per invariant #12 — bootstrap mode downgrades to WARNING
            warnings.append(
                ValidationError(
                    field="stop_loss",
                    message=f"Stop loss is null for {action}. Per spec, stop_loss is mandatory.",
                    severity="WARNING",
                )
            )

        # ── Step 7: Confidence validation ───────────────────────────────────
        confidence = decision.get("confidence")
        if confidence is not None:
            try:
                conf = float(confidence)
                if conf < CONFIDENCE_MIN or conf > CONFIDENCE_MAX:
                    errors.append(
                        ValidationError(
                            field="confidence",
                            message=f"Confidence {conf} outside valid range [{CONFIDENCE_MIN}, {CONFIDENCE_MAX}]",
                            severity="ERROR",
                        )
                    )
            except (ValueError, TypeError):
                errors.append(
                    ValidationError(
                        field="confidence",
                        message=f"Confidence must be a float, got {type(confidence).__name__}: {confidence}",
                        severity="ERROR",
                    )
                )

        # ── Step 8: Thesis validation ───────────────────────────────────────
        thesis = decision.get("thesis", "")
        if is_trade:
            if not thesis:
                errors.append(
                    ValidationError(
                        field="thesis",
                        message=f"Thesis is empty — required for {action}",
                        severity="ERROR",
                    )
                )
            elif isinstance(thesis, str) and len(thesis.strip()) < self.thesis_min_chars:
                errors.append(
                    ValidationError(
                        field="thesis",
                        message=(
                            f"Thesis too short ({len(thesis.strip())} chars). "
                            f"Minimum {self.thesis_min_chars} characters."
                        ),
                        severity="ERROR",
                    )
                )

        # ── Step 9: Signals used validation ─────────────────────────────────
        signals_used = decision.get("signals_used")
        if is_trade:
            if signals_used is None:
                errors.append(
                    ValidationError(
                        field="signals_used",
                        message=f"signals_used is null — required for {action}",
                        severity="ERROR",
                    )
                )
            elif not isinstance(signals_used, list):
                errors.append(
                    ValidationError(
                        field="signals_used",
                        message=(
                            f"signals_used must be a list, got "
                            f"{type(signals_used).__name__}"
                        ),
                        severity="ERROR",
                    )
                )
            elif len(signals_used) == 0:
                errors.append(
                    ValidationError(
                        field="signals_used",
                        message="signals_used list is empty — at least 1 signal required",
                        severity="ERROR",
                    )
                )
            else:
                # Validate each signal is a string
                non_strings = [
                    s for s in signals_used if not isinstance(s, str)
                ]
                if non_strings:
                    errors.append(
                        ValidationError(
                            field="signals_used",
                            message=(
                                f"All signals must be strings. Non-string entries: "
                                f"{non_strings}"
                            ),
                            severity="ERROR",
                        )
                    )

        # ── Step 10: Exit condition validation ──────────────────────────────
        exit_condition = decision.get("exit_condition", "")
        if is_trade:
            if not exit_condition:
                errors.append(
                    ValidationError(
                        field="exit_condition",
                        message=f"exit_condition is empty — required for {action}",
                        severity="ERROR",
                    )
                )
            elif isinstance(exit_condition, str) and exit_condition not in VALID_EXIT_CONDITIONS:
                errors.append(
                    ValidationError(
                        field="exit_condition",
                        message=(
                            f"Invalid exit_condition '{exit_condition}'. "
                            f"Must be one of: {sorted(VALID_EXIT_CONDITIONS)}"
                        ),
                        severity="ERROR",
                    )
                )

        # ── Step 11: Holding horizon validation ─────────────────────────────
        holding_horizon = decision.get("holding_horizon_days")
        if is_trade:
            if holding_horizon is None:
                errors.append(
                    ValidationError(
                        field="holding_horizon_days",
                        message=f"holding_horizon_days is null — required for {action}",
                        severity="ERROR",
                    )
                )
            else:
                try:
                    # Must be an actual int, not a float with decimals
                    if isinstance(holding_horizon, bool) or not isinstance(holding_horizon, (int, float)):
                        raise ValueError("not numeric")
                    if isinstance(holding_horizon, float) and holding_horizon != int(holding_horizon):
                        raise ValueError("float with decimals")
                    hh = int(holding_horizon)
                    if hh < MIN_HOLDING_DAYS:
                        errors.append(
                            ValidationError(
                                field="holding_horizon_days",
                                message=(
                                    f"holding_horizon_days={hh} is below minimum {MIN_HOLDING_DAYS}"
                                ),
                                severity="ERROR",
                            )
                        )
                    if hh > 365:
                        warnings.append(
                            ValidationError(
                                field="holding_horizon_days",
                                message=(
                                    f"holding_horizon_days={hh} is unusually long (>365 days)"
                                ),
                                severity="WARNING",
                            )
                        )
                except (ValueError, TypeError):
                    errors.append(
                        ValidationError(
                            field="holding_horizon_days",
                            message=(
                                f"holding_horizon_days must be an integer, got "
                                f"{type(holding_horizon).__name__}: {holding_horizon}"
                            ),
                            severity="ERROR",
                        )
                    )

        # ── Step 12: Reasoning/mood (optional but encouraged) ───────────────
        has_reasoning = bool(decision.get("reasoning", ""))
        has_mood = bool(decision.get("mood", ""))
        if not has_reasoning and not has_mood and is_trade:
            warnings.append(
                ValidationError(
                    field="reasoning/mood",
                    message=(
                        "Neither 'reasoning' nor 'mood' field present. "
                        "Encouraged for journal analysis."
                    ),
                    severity="WARNING",
                )
            )

        # ── Assemble result ──────────────────────────────────────────────────
        is_valid = len(errors) == 0
        return ValidationResult(
            is_valid=is_valid,
            trader=trader,
            errors=errors,
            warnings=warnings,
        )


def validate_batch(
    decisions: List[Tuple[str, str]],  # [(trader_name, raw_json), ...]
) -> List[ValidationResult]:
    """Validate multiple decisions at once.

    Args:
        decisions: List of (trader_name, raw_json_string) tuples

    Returns:
        List of ValidationResult, one per decision
    """
    validator = DecisionFormatValidator()
    return [
        validator.validate(raw, trader) for trader, raw in decisions
    ]


def validate_decision_dict(
    decision: Dict[str, Any], trader: str = "unknown"
) -> ValidationResult:
    """Validate an already-parsed decision dict.

    Useful for programmatic validation without JSON round-trip.
    """
    raw_json = json.dumps(decision)
    return DecisionFormatValidator().validate(raw_json, trader)
