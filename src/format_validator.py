#!/usr/bin/env python3
"""
Decision Format Validator — validates trader outputs match the SPEC JSON schema.

Per SPEC §4.2 (trader-ticks.md), every trader must output a JSON decision with
specific fields. The risk gate enforces format quality (invariant #12 — bootstrap
mode downgrades vetoes to warnings). This validator provides comprehensive format
checking that can be used in CI, nightly validation, and the risk gate itself.

SPEC schema (from specs/trader-ticks.md):
    {
      "decision": "BUY | SELL | HOLD",
      "ticker": "AAPL",
      "conviction": 0.72,
      "rationale": "Momentum signal 0.81, RSI at 42, SPY trending up.",
      "signal_override": false,
      "override_reason": null
    }
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ── Constants ──────────────────────────────────────────────────────────────────

VALID_ACTIONS = frozenset({"BUY", "SELL", "HOLD"})
REQUIRED_FIELDS = [
    "decision",
    "ticker",
    "conviction",
    "rationale",
    "signal_override",
]
CONVICTION_MIN = 0.0
CONVICTION_MAX = 1.0
RATIONALE_MIN_CHARS = 10


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
    """Validate that a trader's JSON decision matches the SPEC schema.

    Usage:
        validator = DecisionFormatValidator()
        result = validator.validate(trader_json_string, trader_name="kairos")

    Per SPEC (specs/trader-ticks.md) output format:
        {
          "decision": "BUY | SELL | HOLD",
          "ticker": "AAPL",
          "conviction": 0.0-1.0,
          "rationale": "explanation string",
          "signal_override": false,
          "override_reason": null
        }
    """

    def __init__(self, rationale_min_chars: int = RATIONALE_MIN_CHARS):
        self.rationale_min_chars = rationale_min_chars

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

        # Early exit if missing required fields (avoids cascading errors)
        if any(e.severity == "ERROR" for e in errors
               if e.field in REQUIRED_FIELDS):
            return ValidationResult(
                is_valid=False, trader=trader, errors=errors, warnings=warnings
            )

        # ── Step 3: Decision validation ─────────────────────────────────────
        raw_decision = decision.get("decision", "")
        if isinstance(raw_decision, str):
            dec = raw_decision.upper().strip()
        else:
            dec = str(raw_decision).upper().strip()

        if not dec:
            errors.append(
                ValidationError(
                    field="decision",
                    message="Decision is empty or missing. Must be BUY, SELL, or HOLD",
                    severity="ERROR",
                )
            )
        elif dec not in VALID_ACTIONS:
            errors.append(
                ValidationError(
                    field="decision",
                    message=f"Invalid decision '{dec}'. Must be BUY, SELL, or HOLD",
                    severity="ERROR",
                )
            )

        is_trade = dec in ("BUY", "SELL")

        # ── Step 4: Ticker validation ───────────────────────────────────────
        ticker = decision.get("ticker")
        if is_trade:
            if ticker is None or (isinstance(ticker, str) and not ticker.strip()):
                errors.append(
                    ValidationError(
                        field="ticker",
                        message=f"Ticker required for {dec} but was null/empty",
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

        # ── Step 5: Conviction validation ───────────────────────────────────
        conviction = decision.get("conviction")
        if conviction is not None:
            try:
                conf = float(conviction)
                if conf < CONVICTION_MIN or conf > CONVICTION_MAX:
                    errors.append(
                        ValidationError(
                            field="conviction",
                            message=f"Conviction {conf} outside valid range [{CONVICTION_MIN}, {CONVICTION_MAX}]",
                            severity="ERROR",
                        )
                    )
            except (ValueError, TypeError):
                errors.append(
                    ValidationError(
                        field="conviction",
                        message=f"Conviction must be a float, got {type(conviction).__name__}: {conviction}",
                        severity="ERROR",
                    )
                )
        else:
            errors.append(
                ValidationError(
                    field="conviction",
                    message="Conviction is required and must be a float",
                    severity="ERROR",
                )
            )

        # ── Step 6: Rationale validation ────────────────────────────────────
        rationale = decision.get("rationale", "")
        if is_trade:
            if not rationale or not isinstance(rationale, str) or not rationale.strip():
                errors.append(
                    ValidationError(
                        field="rationale",
                        message=f"Rationale is empty — required for {dec}",
                        severity="ERROR",
                    )
                )
            elif isinstance(rationale, str) and len(rationale.strip()) < self.rationale_min_chars:
                errors.append(
                    ValidationError(
                        field="rationale",
                        message=(
                            f"Rationale too short ({len(rationale.strip())} chars). "
                            f"Minimum {self.rationale_min_chars} characters."
                        ),
                        severity="ERROR",
                    )
                )

        # ── Step 7: Signal override validation ──────────────────────────────
        signal_override = decision.get("signal_override")
        if signal_override is not None:
            if not isinstance(signal_override, bool):
                errors.append(
                    ValidationError(
                        field="signal_override",
                        message=f"signal_override must be a boolean, got {type(signal_override).__name__}: {signal_override}",
                        severity="ERROR",
                    )
                )
        else:
            errors.append(
                ValidationError(
                    field="signal_override",
                    message="signal_override is required and must be a boolean",
                    severity="ERROR",
                )
            )

        # ── Step 8: HOLD with missing ticker ────────────────────────────────
        if not is_trade and ticker is not None and ticker != "":
            warnings.append(
                ValidationError(
                    field="ticker",
                    message=f"Ticker '{ticker}' provided but decision is {dec}. Should be null for HOLD.",
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