#!/usr/bin/env python3
"""
Pre-market gate — runs prompt format validation and manages the blocking sentinel.

Called by cron at 9:15 AM ET (before market open at 9:30 AM).
- Runs scripts/validate_prompt_format.py
- On success: clears state/.pre_market_blocked sentinel
- On failure: creates state/.pre_market_blocked to block tick_producer.py

Usage:
    python3 scripts/pre_market_gate.py                   # run validation
    python3 scripts/pre_market_gate.py --clear            # manually clear the gate
    python3 scripts/pre_market_gate.py --status           # check gate status

Exit codes:
    0 — validation passed, gate clear
    1 — validation failed, gate blocked
    2 — internal error running the script
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_DIR / "state"
SENTINEL_FILE = STATE_DIR / ".pre_market_blocked"
LOGS_DIR = REPO_DIR / "logs"
VALIDATE_SCRIPT = REPO_DIR / "scripts" / "validate_prompt_format.py"


def ensure_dirs():
    """Ensure state and logs directories exist."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def run_validation() -> tuple[bool, str]:
    """Run validate_prompt_format.py and return (passed, output).

    Returns:
        (True, output) if all traders pass validation
        (False, output) if one or more traders fail
    """
    try:
        result = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO_DIR),
        )
        output = result.stdout.strip()
        if result.returncode == 0:
            return True, output
        else:
            # Collect stderr for error details
            error_detail = result.stderr.strip()
            full_output = output
            if error_detail:
                full_output += "\n" + error_detail
            return False, full_output
    except subprocess.TimeoutExpired:
        return False, "Validation script timed out after 30s"
    except FileNotFoundError:
        return False, f"Validation script not found: {VALIDATE_SCRIPT}"
    except Exception as e:
        return False, f"Internal error running validation: {e}"


def block_gate(reason: str):
    """Create the sentinel file to block tick production."""
    ensure_dirs()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S ET")
    SENTINEL_FILE.write_text(f"Blocked at {timestamp}: {reason}")


def clear_gate():
    """Remove the sentinel file to allow tick production."""
    if SENTINEL_FILE.exists():
        SENTINEL_FILE.unlink()


def gate_status() -> str:
    """Return current gate status."""
    if SENTINEL_FILE.exists():
        try:
            return f"BLOCKED: {SENTINEL_FILE.read_text().strip()}"
        except Exception:
            return "BLOCKED: (cannot read sentinel)"
    return "CLEAR — tick production allowed"


def main():
    parser = argparse.ArgumentParser(
        description="Pre-market prompt format validation gate"
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Manually clear the pre-market gate (removes sentinel)"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print current gate status and exit"
    )
    args = parser.parse_args()

    ensure_dirs()

    # --status: just report current state
    if args.status:
        print(gate_status())
        sys.exit(0)

    # --clear: manually unblock
    if args.clear:
        clear_gate()
        print("✅ Pre-market gate cleared. Tick production allowed.")
        sys.exit(0)

    # Default: run validation and set/clear the gate
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S ET")
    print(f"=== Pre-market gate check: {timestamp} ===")

    passed, output = run_validation()

    if passed:
        clear_gate()
        print("✅ Validation PASSED — gate clear, ticks allowed")
        if output:
            # Parse key summary lines from JSON output
            import json
            try:
                data = json.loads(output)
                all_valid = data.get("all_valid", True)
                traders = data.get("trader_results", {})
                for t, r in traders.items():
                    status = "✅" if r.get("valid") else "❌"
                    print(f"  {status} {t}")
            except (json.JSONDecodeError, KeyError):
                print(output[:500])
        sys.exit(0)
    else:
        # Collect failure reasons for the sentinel
        failure_reasons = []
        import json as _json
        try:
            data = _json.loads(output)
            traders = data.get("trader_results", {})
            for t, r in traders.items():
                if not r.get("valid"):
                    for err in r.get("errors", []):
                        failure_reasons.append(f"{t}: {err}")
        except (_json.JSONDecodeError, KeyError):
            failure_reasons.append(output[:200])

        reason = "; ".join(failure_reasons) if failure_reasons else "Validation failed"
        block_gate(reason)

        print(f"❌ Validation FAILED — gate BLOCKED")
        for r in failure_reasons:
            print(f"  ❌ {r}")
        print(f"\nSentinel written: {SENTINEL_FILE}")
        print("Tick production blocked until validation passes.")
        print("Run 'python3 scripts/pre_market_gate.py --clear' to override.")
        sys.exit(1)


if __name__ == "__main__":
    main()
