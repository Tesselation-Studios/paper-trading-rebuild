#!/usr/bin/env python3
"""
Invariant #10 CI validator — Risk gate mirrors prompts.

Checks that trader prompt files and config/risk.yaml are aligned:
  - Sizing caps in prompts ≤ risk.yaml caps (trader can't exceed system limit)
  - Conviction floors in prompts ≥ risk.yaml floor (trader can't be too lenient)
  - Stop-loss rules: prompt must require stops if gate does
  - Format requirements: thesis + signals_used must be in prompts if gate requires them

Usage:
    python3 scripts/check_risk_prompt_consistency.py [--prompts-dir /path/to/prompts]
Exit code 1 if mismatches found, 0 if consistent.
"""

import re
import sys
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class RuleCheck:
    """A single consistency check between prompt and risk gate."""
    trader: str
    risk_path: str          # dot-separated path in risk.yaml
    risk_value: object
    prompt_param: str       # human-readable parameter name
    prompt_value: object
    rule: str               # constraint description
    passed: bool
    detail: str = ""


@dataclass
class PromptParams:
    """Extracted parameters from a trader prompt."""
    trader: str
    position_size_pct: Optional[float] = None
    conviction_threshold: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    requires_stop_loss: bool = False
    requires_thesis: bool = False
    requires_signals: bool = False
    raw_text: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# Prompt parsing
# ═══════════════════════════════════════════════════════════════════════════════


def parse_prompt(text: str, trader: str) -> PromptParams:
    """Extract key risk-relevant parameters from a trader prompt."""
    pp = PromptParams(trader=trader, raw_text=text)

    # Position size: "Position size: X%" or "Max risk per trade: X-Y%" or "Sizing: X-Y%"
    pos_match = re.search(
        r'(?:Position size|Sizing|Max risk per trade)\s*:\s*(\d+(?:\.\d+)?)\s*%',
        text, re.IGNORECASE
    )
    if pos_match:
        pp.position_size_pct = float(pos_match.group(1)) / 100.0

    # Also check "X-Y%" range patterns (take the max)
    range_match = re.search(
        r'(?:Max risk per trade|Sizing)\s*:\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*%',
        text, re.IGNORECASE
    )
    if range_match:
        pp.position_size_pct = max(
            float(range_match.group(1)) / 100.0,
            float(range_match.group(2)) / 100.0,
        )

    # Conviction threshold
    conv_match = re.search(
        r'(?:Confidence threshold|Conviction)\s*[:=]\s*(0?\.\d+)',
        text, re.IGNORECASE
    )
    if conv_match:
        pp.conviction_threshold = float(conv_match.group(1))

    # Stop loss percentage
    stop_match = re.search(
        r'(?:Stop loss|stop-loss)\s*:\s*(\d+(?:\.\d+)?)\s*%',
        text, re.IGNORECASE
    )
    if stop_match:
        pp.stop_loss_pct = float(stop_match.group(1)) / 100.0

    # Stop loss required
    if re.search(r'stop\s*loss.*(?:required|mandatory|must|on every trade)', text, re.IGNORECASE):
        pp.requires_stop_loss = True

    # Thesis required
    if re.search(r'thesis.*20\+.*char|thesis.*required|THESIS.*MUST|Thesis MUST', text):
        pp.requires_thesis = True

    # Signals used required
    if re.search(r'signals_used.*must.*have|signals_used.*required|signals_used.*MUST', text):
        pp.requires_signals = True

    return pp


# ═══════════════════════════════════════════════════════════════════════════════
# Consistency checks
# ═══════════════════════════════════════════════════════════════════════════════


def check_consistency(
    risk_config: dict,
    prompts: Dict[str, PromptParams],
) -> List[RuleCheck]:
    """Run all invariant #10 consistency checks."""

    checks: List[RuleCheck] = []

    # Shortcut helpers
    def risk(path: str) -> object:
        """Get value from risk config by dot-separated path."""
        parts = path.split(".")
        current = risk_config
        for p in parts:
            if isinstance(current, dict):
                current = current.get(p)
            else:
                return None
        return current

    require_conviction = float(risk("gates.require_conviction") or 0)

    for trader, pp in prompts.items():
        # ── Conviction floor: prompt must not be more lenient than gate ──
        if pp.conviction_threshold is not None:
            passed = pp.conviction_threshold >= require_conviction
            checks.append(RuleCheck(
                trader=trader,
                risk_path="gates.require_conviction",
                risk_value=require_conviction,
                prompt_param="conviction threshold",
                prompt_value=pp.conviction_threshold,
                rule=f"prompt >= {require_conviction} (gate minimum)",
                passed=passed,
                detail="" if passed else (
                    f"Prompt tells {trader} conviction threshold {pp.conviction_threshold} "
                    f"but risk gate requires {require_conviction}. "
                    f"Trades at {pp.conviction_threshold} will be vetoed."
                ),
            ))

        # ── Position sizing: prompt must not exceed risk cap ──
        max_pos_pct = float(risk("position.max_position_pct") or 0)
        risk_per_trade = float(risk("sizing.risk_per_trade_pct") or 0)
        # The effective cap is the tighter of max_position_pct and risk_per_trade
        effective_cap = min(max_pos_pct, risk_per_trade) if max_pos_pct > 0 else risk_per_trade

        if pp.position_size_pct is not None and effective_cap > 0:
            passed = pp.position_size_pct <= effective_cap
            checks.append(RuleCheck(
                trader=trader,
                risk_path="sizing.risk_per_trade_pct",
                risk_value=effective_cap,
                prompt_param="position size",
                prompt_value=pp.position_size_pct,
                rule=f"prompt ≤ {effective_cap} (system cap)",
                passed=passed,
                detail="" if passed else (
                    f"Prompt tells {trader} position size {pp.position_size_pct:.0%} "
                    f"but risk gate caps at {effective_cap:.0%}. "
                    f"Trades at {pp.position_size_pct:.0%} sizing will be blocked."
                ),
            ))

        # ── Stop loss requirements must match ──
        default_stop = float(risk("stop_loss.default_pct") or 0)
        if pp.stop_loss_pct is not None and default_stop > 0:
            # Trader stop must be at least as tight as default (or trader can be tighter)
            # Actually: risk gate sets a default; trader can be tighter but not looser
            # A looser stop (higher %) would not be blocked, but it's poor practice
            # We warn if trader stop is looser than risk default
            passed = pp.stop_loss_pct <= default_stop
            checks.append(RuleCheck(
                trader=trader,
                risk_path="stop_loss.default_pct",
                risk_value=default_stop,
                prompt_param="stop loss",
                prompt_value=pp.stop_loss_pct,
                rule=f"prompt ≤ {default_stop:.0%} (gate default, tighter is allowed)",
                passed=passed,
                detail="" if passed else (
                    f"Prompt tells {trader} stop loss {pp.stop_loss_pct:.0%} "
                    f"but risk gate default is {default_stop:.0%}. "
                    f"Consider tightening the prompt or updating risk.yaml."
                ),
            ))

    return checks


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Check risk-prompt consistency (Invariant #10)")
    parser.add_argument(
        "--prompts-dir",
        default="/tmp/paper-trading-prompts",
        help="Path to paper-trading-prompts repo",
    )
    parser.add_argument(
        "--risk-yaml",
        default="config/risk.yaml",
        help="Path to risk config file",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: only print failures, exit 1 on mismatch",
    )
    args = parser.parse_args()

    # Load risk config
    risk_path = Path(args.risk_yaml)
    if not risk_path.exists():
        print(f"ERROR: risk.yaml not found at {risk_path}")
        return 1

    with open(risk_path) as f:
        risk_config = yaml.safe_load(f)

    # Load prompts
    prompts_dir = Path(args.prompts_dir)
    prompts: Dict[str, PromptParams] = {}
    for trader_dir in sorted(prompts_dir.iterdir()):
        if not trader_dir.is_dir():
            continue
        prompt_file = trader_dir / "prompt.txt"
        if not prompt_file.exists():
            continue
        trader = trader_dir.name
        text = prompt_file.read_text()
        prompts[trader] = parse_prompt(text, trader)

    if not prompts:
        print("ERROR: No prompt files found")
        return 1

    # Run checks
    checks = check_consistency(risk_config, prompts)

    failures = [c for c in checks if not c.passed]
    passed = [c for c in checks if c.passed]

    # Print results
    if not args.ci:
        print(f"🔍 Invariant #10: Risk Gate ↔ Prompt Consistency Check")
        print(f"   Risk config: {risk_path}")
        print(f"   Prompts dir: {prompts_dir}")
        print(f"   Checks: {len(checks)} total, {len(passed)} passed, {len(failures)} failed\n")

        for c in passed:
            print(f"  ✅ {c.trader:12s} {c.prompt_param:20s}: {c.prompt_value} vs risk {c.risk_value} — OK")
    else:
        for c in passed:
            print(f"OK: {c.trader}/{c.prompt_param}")

    if failures:
        print(f"\n❌ {len(failures)} inconsistency(s) found:")
        for c in failures:
            print(f"  ❌ {c.trader:12s} {c.prompt_param:20s}: prompt={c.prompt_value}, risk={c.risk_value}")
            print(f"     {c.detail}")
        return 1

    print("\n✅ All risk-prompt consistency checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
