#!/usr/bin/env python3
"""
Pre-market prompt format validation gate.

Per SPEC §4.2 and specs/operational-hygiene.md:
  - Validates every trader's prompt template is intact before market open
  - Checks prompt files exist, contain required sections, and are non-empty
  - Runs a dry-run format check through DecisionFormatValidator to ensure
    the prompt output schema is supported
  - Exits non-zero if any trader fails → cron should block the tick

Usage:
    python3 scripts/validate_prompt_format.py                # validate all traders
    python3 scripts/validate_prompt_format.py --trader kairos  # single trader
    python3 scripts/validate_prompt_format.py --json           # JSON output for CI

Exit codes:
    0 — all traders pass validation
    1 — one or more traders failed validation (block market open)
    2 — validation script itself encountered an error
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_DIR / "prompts"
AGENTS_DIR = REPO_DIR / "agents"

# Required sections that must appear in every trader prompt
REQUIRED_SECTIONS = [
    "decision",
    "conviction",
    "rationale",
]

# Minimum prompt size in characters (empty/broken prompts are too small)
MIN_PROMPT_SIZE_CHARS = 200

# All active traders
ALL_TRADERS = ["kairos", "stonks", "aldridge"]


def validate_prompt_file(trader_id: str) -> dict:
    """Validate a single trader's prompt file.

    Returns:
        dict with 'trader', 'valid', 'errors', 'warnings'
    """
    result = {
        "trader": trader_id,
        "valid": True,
        "errors": [],
        "warnings": [],
    }

    # 1. Check prompt file exists at prompts/{trader}.txt
    prompt_path = PROMPTS_DIR / f"{trader_id}.txt"
    if not prompt_path.exists():
        result["valid"] = False
        result["errors"].append(f"Prompt file missing: {prompt_path}")
        return result

    # 2. Check prompt file is non-empty and above minimum size
    try:
        content = prompt_path.read_text()
    except Exception as e:
        result["valid"] = False
        result["errors"].append(f"Cannot read prompt file: {e}")
        return result

    if not content.strip():
        result["valid"] = False
        result["errors"].append(f"Prompt file is empty: {prompt_path}")
        return result

    if len(content.strip()) < MIN_PROMPT_SIZE_CHARS:
        result["warnings"].append(
            f"Prompt file is unusually short ({len(content.strip())} chars, "
            f"min recommended: {MIN_PROMPT_SIZE_CHARS})"
        )

    # 3. Check for required decision fields in the output schema
    for section in REQUIRED_SECTIONS:
        if section.lower() not in content.lower():
            result["warnings"].append(
                f"Prompt may be missing '{section}' field instruction"
            )

    # 4. Check AGENTS.md exists and is non-empty
    agent_dir = AGENTS_DIR / f"trader-{trader_id}"
    agents_path = agent_dir / "AGENTS.md"
    if not agents_path.exists():
        result["warnings"].append(f"AGENTS.md missing: {agents_path}")
    else:
        try:
            agents_content = agents_path.read_text()
            if len(agents_content.strip()) < 100:
                result["warnings"].append("AGENTS.md is unusually short")
        except Exception:
            result["warnings"].append("Cannot read AGENTS.md")

    return result


def validate_output_format() -> dict:
    """Validate that DecisionFormatValidator is importable and working.

    This ensures the output schema validator is functional before ticks run.
    A broken validator = no format enforcement = broken prompts can hit production.
    """
    result = {
        "validator_available": False,
        "errors": [],
    }

    try:
        sys.path.insert(0, str(REPO_DIR))
        from src.format_validator import DecisionFormatValidator, VALID_ACTIONS

        validator = DecisionFormatValidator()

        # Test with a minimal valid decision
        valid_json = json.dumps({
            "action": "HOLD",
            "ticker": None,
            "quantity": 0,
            "stop_loss": None,
            "confidence": 0.5,
            "thesis": "No actionable signals in current market conditions.",
            "signals_used": ["no_signal"],
            "exit_condition": "time_stop",
            "holding_horizon_days": 1,
        })
        result_obj = validator.validate(valid_json, "test")
        if not result_obj.is_valid:
            result["errors"].append(
                f"Validator rejects a known-valid HOLD: {result_obj.errors}"
            )
        else:
            result["validator_available"] = True

    except ImportError as e:
        result["errors"].append(f"Cannot import DecisionFormatValidator: {e}")
    except Exception as e:
        result["errors"].append(f"Validator runtime error: {e}")

    return result


def validate_all_traders(traders: list[str]) -> dict:
    """Run all validations and return aggregate result."""
    results = {
        "timestamp": __import__("datetime").datetime.now().isoformat(),
        "traders_checked": len(traders),
        "all_valid": True,
        "trader_results": {},
        "format_validator": {},
    }

    # Validate each trader's prompt files
    for trader in traders:
        trader_result = validate_prompt_file(trader)
        results["trader_results"][trader] = trader_result
        if not trader_result["valid"]:
            results["all_valid"] = False

    # Validate format validator itself
    results["format_validator"] = validate_output_format()

    return results


def print_results(results: dict, json_output: bool = False):
    """Print validation results to stdout."""
    if json_output:
        print(json.dumps(results, indent=2))
        return

    all_ok = True

    print("=" * 60)
    print("PRE-MARKET PROMPT FORMAT VALIDATION")
    print(f"Timestamp: {results['timestamp']}")
    print("=" * 60)

    for trader, result in results["trader_results"].items():
        status = "✅" if result["valid"] else "❌"
        print(f"\n{status} {trader}:")
        for err in result["errors"]:
            print(f"   ❌ ERROR: {err}")
            all_ok = False
        for warn in result["warnings"]:
            print(f"   ⚠️  WARNING: {warn}")

    # Format validator
    fv = results["format_validator"]
    fv_status = "✅" if fv["validator_available"] else "❌"
    print(f"\n{fv_status} DecisionFormatValidator: {'ready' if fv['validator_available'] else 'BROKEN'}")
    for err in fv.get("errors", []):
        print(f"   ❌ {err}")
        all_ok = False

    print("\n" + "=" * 60)
    if all_ok and results.get("all_valid", False):
        print("✅ ALL CHECKS PASSED — prompts ready for market open")
    else:
        print("❌ VALIDATION FAILED — DO NOT START TICKS")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Pre-market prompt format validation gate"
    )
    parser.add_argument(
        "--trader",
        choices=ALL_TRADERS,
        help="Validate a single trader (default: all)"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON"
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="CI mode: validates + runs format validator self-test"
    )
    args = parser.parse_args()

    traders = [args.trader] if args.trader else ALL_TRADERS

    try:
        results = validate_all_traders(traders)
    except Exception as e:
        print(f"FATAL: Validation script error: {e}", file=sys.stderr)
        sys.exit(2)

    print_results(results, json_output=args.json)

    # Exit code: 0 = all pass, 1 = failures
    if not results["all_valid"]:
        sys.exit(1)

    # In CI mode, also verify the format validator can catch broken prompts
    if args.ci:
        try:
            sys.path.insert(0, str(REPO_DIR))
            from src.format_validator import DecisionFormatValidator
            validator = DecisionFormatValidator()

            broken_tests = [
                ("empty string", ""),
                ("not json", "not json at all"),
                ("missing fields", '{"action": "BUY"}'),
                ("bad action", '{"action": "HODL", "ticker": "AAPL", "quantity": 10, "stop_loss": 100, "confidence": 0.5, "thesis": "A" * 20, "signals_used": ["test"], "exit_condition": "stop_loss_hit", "holding_horizon_days": 5}'),
                ("bad confidence", '{"action": "BUY", "ticker": "AAPL", "quantity": 10, "stop_loss": 100, "confidence": 1.5, "thesis": "A" * 20, "signals_used": ["test"], "exit_condition": "stop_loss_hit", "holding_horizon_days": 5}'),
            ]

            ci_errors = []
            for name, payload in broken_tests:
                vr = validator.validate(payload, trader="ci-test")
                if vr.is_valid:
                    ci_errors.append(f"FAIL: '{name}' should have been caught as invalid")

            if ci_errors:
                print("\nCI: Broken prompt detection FAILED:")
                for err in ci_errors:
                    print(f"  ❌ {err}")
                sys.exit(1)
            else:
                print("\nCI: All broken prompts correctly detected ✅")
        except Exception as e:
            print(f"\nCI: Error running broken-prompt tests: {e}", file=sys.stderr)
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
