#!/usr/bin/env python3
"""
After-hours prompt format validator.

Runs each trader's prompt through OpenRouter and validates
that the output contains all required fields for trading.

Usage:
    python3 scripts/format_test.py              # test all traders
    python3 scripts/format_test.py --trader kairos  # test one

Required fields (per SPEC §30):
    - thesis: >= 20 characters
    - signals_used: non-empty list
    - exit_condition: non-empty string
    - holding_horizon_days: positive integer
    - action: BUY or SELL or HOLD
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.5-flash-preview-08-20"  # cheap for format testing
MAX_TOKENS = 800  # enough for a full JSON decision

PROMPT_DIR = Path(__file__).parent.parent / "prompts"
TRADERS = ["kairos", "aldridge", "stonks"]

REQUIRED_FIELDS = {
    "thesis": lambda v: isinstance(v, str) and len(v.strip()) >= 20,
    "signals_used": lambda v: v and v != [] and v != "" and v != "[]",
    "exit_condition": lambda v: isinstance(v, str) and len(v.strip()) > 0,
    "holding_horizon_days": lambda v: isinstance(v, (int, float)) and v > 0,
    "action": lambda v: v in ("BUY", "SELL", "HOLD"),
}


def load_prompt(trader: str) -> str:
    """Load a trader's prompt file."""
    prompt_path = PROMPT_DIR / f"{trader}.txt"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt not found: {prompt_path}")
    return prompt_path.read_text().strip()


def call_openrouter(prompt: str) -> dict:
    """Send prompt to OpenRouter, return parsed JSON response."""
    import urllib.request

    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a paper trading agent. Respond with ONLY valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3,
    }).encode()

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        },
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())

    content = result["choices"][0]["message"]["content"]

    # Try to extract JSON from the response
    # Models sometimes wrap JSON in markdown code blocks
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    return json.loads(content)


def validate_output(trader: str, output: dict) -> list[str]:
    """Validate trader output against required fields. Returns list of failures."""
    failures = []

    for field, check in REQUIRED_FIELDS.items():
        value = output.get(field)
        if not check(value):
            if value is None:
                failures.append(f"{field}: MISSING")
            elif isinstance(value, str):
                preview = value[:50].replace("\n", " ")
                failures.append(f"{field}: FAILED (got: '{preview}...')")
            else:
                failures.append(f"{field}: FAILED (got: {value})")

    return failures


def main():
    parser = argparse.ArgumentParser(
        description="Validate trader prompt outputs after hours"
    )
    parser.add_argument(
        "--trader", type=str, choices=TRADERS,
        help="Test a single trader (default: all)"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON"
    )
    args = parser.parse_args()

    if not OPENROUTER_API_KEY:
        print("ERROR: OPENROUTER_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    traders = [args.trader] if args.trader else TRADERS
    results = {}
    all_pass = True

    for trader in traders:
        print(f"\n{'='*60}")
        print(f"Testing: {trader}")
        print(f"{'='*60}")

        try:
            prompt = load_prompt(trader)
            print(f"Prompt: {len(prompt)} chars")

            output = call_openrouter(prompt)
            failures = validate_output(trader, output)

            if failures:
                all_pass = False
                results[trader] = {"status": "FAIL", "failures": failures, "output": output}
                print(f"❌ FAILED ({len(failures)} issues):")
                for f in failures:
                    print(f"  - {f}")
            else:
                results[trader] = {"status": "PASS", "output": output}
                print("✅ PASS")

        except FileNotFoundError as e:
            results[trader] = {"status": "ERROR", "error": str(e)}
            all_pass = False
            print(f"❌ ERROR: {e}")
        except json.JSONDecodeError as e:
            results[trader] = {"status": "ERROR", "error": f"JSON parse failed: {e}"}
            all_pass = False
            print(f"❌ ERROR: JSON parse failed")
        except Exception as e:
            results[trader] = {"status": "ERROR", "error": str(e)}
            all_pass = False
            print(f"❌ ERROR: {e}")

    # Summary
    print(f"\n{'='*60}")
    print(f"SUMMARY: {datetime.now().isoformat()}")
    print(f"{'='*60}")
    for trader, result in results.items():
        status_emoji = "✅" if result["status"] == "PASS" else "❌"
        print(f"{status_emoji} {trader}: {result['status']}")
        if result["status"] == "FAIL":
            for f in result["failures"]:
                print(f"     - {f}")

    if args.json:
        print(json.dumps(results, indent=2, default=str))

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()