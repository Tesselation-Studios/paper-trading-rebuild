#!/usr/bin/env python3
"""
tick_runner.py — Consumes a pre-assembled prompt and feeds it to the agent.

This is the bridge between tick_prompt.py output and the agent session.
In production, this would use the OpenClaw agent API to create a one-shot
session with the prompt as context.

Usage (from cron):
    scripts/tick_runner.py kairos "$(scripts/tick_prompt.py --trader kairos)"

Or to measure timing:
    scripts/tick_runner.py kairos --prompt-file /tmp/tick_prompt.txt
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_tick_via_agent_api(trader: str, prompt: str,
                           timeout: int = 600) -> dict:
    """
    Submit a pre-assembled prompt to the agent and wait for the response.

    In the OpenClaw deployment, this would use the agent API to:
    1. Create a one-shot session
    2. Send the prompt as the first (and only) message
    3. Wait for the agent to respond with JSON decision
    4. Parse and validate the response
    5. Discard the session

    Returns: {"status": "completed", "decision": {...}, "duration_ms": ...}
    """
    start = time.time()

    # TODO: Replace with actual OpenClaw agent API integration.
    # For now, write prompt to a temp file for testing.
    temp_file = Path(f"/tmp/tick_{trader}_{int(start)}.txt")
    temp_file.write_text(prompt)

    # Simulate agent processing for testing
    # In production, this would call the agent API
    try:
        # This is a placeholder. In production:
        #   result = openclaw_api.submit_oneshot(trader, prompt, timeout=timeout)
        #   response = result["response"]
        #   decision = parse_json_decision(response)

        # For testing: the tick_prompt.py output is the complete prompt.
        # The agent would process it and output JSON.
        duration_ms = (time.time() - start) * 1000

        return {
            "status": "completed",
            "trader": trader,
            "prompt_chars": len(prompt),
            "duration_ms": round(duration_ms, 1),
            "decision": None,  # Would contain the parsed JSON
            "note": "tick_runner: prompt assembled — awaiting agent API integration",
        }
    finally:
        # Clean up temp file
        if temp_file.exists():
            temp_file.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a trading tick with a pre-assembled prompt.",
    )
    parser.add_argument(
        "trader",
        choices=["kairos", "stonks", "aldridge"],
        help="Trader name",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="Pre-assembled prompt string (from tick_prompt.py stdout)",
    )
    parser.add_argument(
        "--prompt-file",
        help="Read prompt from file instead of stdin/args",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Agent timeout in seconds (default: 600)",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Output timing benchmark to stderr",
    )

    args = parser.parse_args()

    # Get prompt
    prompt: Optional[str] = None
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text()
    elif args.prompt:
        prompt = args.prompt
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        sys.stderr.write("tick_runner: No prompt provided. Use --prompt-file or pipe input.\n")
        sys.exit(1)

    if not prompt or not prompt.strip():
        sys.stderr.write("tick_runner: Empty prompt received.\n")
        sys.exit(1)

    # Run the tick
    result = run_tick_via_agent_api(args.trader, prompt, timeout=args.timeout)

    if args.benchmark:
        sys.stderr.write(
            f"[tick_runner] {args.trader}: tick completed in "
            f"{result['duration_ms']:.0f}ms, "
            f"prompt={result['prompt_chars']} chars\n"
        )

    # Output result as JSON
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
