#!/usr/bin/env python3
"""
Nightly Pipeline — unified cron entry point for the optimization pipeline.

Chains:
  1. Backfill missing bars (Parquet store)
  2. BarLoader → hot-cache into SQLite
  3. Phase 1: Signal sweep across N variants (cheap filter)
  4. Phase 2: LLM validation on top K candidates (expensive)
  5. Promote winner (git branch + sweep_results table)
  6. Push summary card to Canvas
  7. (Optional) 7-Night Trend Card — weekly variant performance trends

Usage:
    python3 scripts/nightly_pipeline.py                                          # defaults
    python3 scripts/nightly_pipeline.py --dates 20 --train 15 --val 5
    python3 scripts/nightly_pipeline.py --trader kairos --dry-run
    python3 scripts/nightly_pipeline.py --skip-backfill --skip-llm

Cron (recommended):
    0 22 * * 1-5 cd ~/projects/paper-trading-rebuild && \
        python3 scripts/nightly_pipeline.py >> logs/nightly_pipeline.log 2>&1

Spec: specs/nightly-optimization-pipeline.md
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Path setup ───────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
SCRIPTS_DIR = PROJECT_DIR / "scripts"
SHARED_DIR = PROJECT_DIR / "shared"
BARS_DIR = SHARED_DIR / "cache" / "bars"
DB_PATH = SHARED_DIR / "trader.db"

sys.path.insert(0, str(PROJECT_DIR))

# ── Imports from the project ─────────────────────────────────────────────────
from src.bar_loader import BarLoader
from src.prompt_sweep import (
    PromptVariant,
    SweepResult,
    SHORT_NAMES,
    TRADER_IDS,
    get_trading_days,
    run_sweep,
)
from src.sweep_validation import (
    two_phase_validate,
    ValidationConfig,
    log_sweep_result,
)
from src.transaction_costs import CostModel

# ── Canvas credentials ───────────────────────────────────────────────────────
CANVAS_ENV = Path.home() / "canvas" / ".env"


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline Step 1: Backfill
# ═══════════════════════════════════════════════════════════════════════════════


def step_backfill(
    tickers: str = "core",
    days: int = 20,
    force: bool = False,
    dry_run: bool = False,
) -> Tuple[int, int]:
    """Backfill missing bars into the Parquet store.

    Calls scripts/backfill_bars.py as a subprocess for its existing logic.

    Returns:
        (n_tickers_fetched, n_total_bars) — or (0, 0) if skipped.
    """
    if dry_run:
        print(f"[nightly] DRY RUN: would backfill tickers={tickers}, days={days}")
        return 0, 0

    backfill_script = SCRIPTS_DIR / "backfill_bars_alpaca.py"
    if not backfill_script.exists():
        # Fallback to yfinance version
        backfill_script = SCRIPTS_DIR / "backfill_bars.py"

    print(f"\n{'='*60}")
    print("  STEP 1/5: Backfill missing bars")
    print(f"  Tickers: {tickers} | Days: {days}")
    print(f"{'='*60}")

    cmd = [
        sys.executable, str(backfill_script),
        "--tickers", tickers,
        "--days", str(days),
        "--verbose",
    ]
    if force:
        cmd.append("--force")

    start = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    elapsed = time.time() - start

    # Print output
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if result.returncode != 0:
        print(f"[nightly] ⚠️  Backfill exited with code {result.returncode}")
        # Non-fatal — continue even if backfill partially fails
    else:
        print(f"[nightly] Backfill complete ({elapsed:.1f}s)")

    return result.returncode, elapsed


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline Step 2: BarLoader → SQLite Cache
# ═══════════════════════════════════════════════════════════════════════════════


def step_cache_bars(
    tickers: List[str],
    start_date: str,
    end_date: str,
    dry_run: bool = False,
) -> int:
    """Hot-cache bars into SQLite via BarLoader.to_sqlite_cache().

    Returns:
        Number of tick rows cached (0 if dry-run).
    """
    if dry_run:
        print(f"[nightly] DRY RUN: would cache bars for {len(tickers)} tickers "
              f"[{start_date} → {end_date}]")
        return 0

    print(f"\n{'='*60}")
    print("  STEP 2/5: BarLoader → SQLite cache")
    print(f"  Tickers: {len(tickers)} | Range: {start_date} → {end_date}")
    print(f"{'='*60}")

    start = time.time()
    loader = BarLoader(bars_dir=BARS_DIR, db_path=DB_PATH)

    # Check what's available
    available = 0
    missing: List[Tuple[str, str]] = []
    for ticker in tickers:
        ad = loader.available_dates(ticker)
        if ad:
            available += 1
        # Check for missing dates
        m = loader.missing_dates([ticker], start_date, end_date)
        missing.extend(m)

    print(f"  Available tickers: {available}/{len(tickers)}")
    if missing:
        print(f"  Missing date-ticker pairs: {len(missing)} "
              f"(will be skipped — backfill them first)")

    # Cache into Postgres (falls back to SQLite if unavailable)
    n_rows = loader.to_cache(
        tickers=tickers,
        start_date=start_date,
        end_date=end_date,
        interval_minutes=30,
    )
    elapsed = time.time() - start
    print(f"  Cached {n_rows:,} tick rows ({elapsed:.1f}s)")
    return n_rows


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline Step 3: Phase 1 — Signal Sweep
# ═══════════════════════════════════════════════════════════════════════════════


def step_signal_sweep(
    trader: Optional[str] = None,
    date_str: Optional[str] = None,
    n_dates: int = 20,
    train_days: int = 7,
    val_days: int = 3,
    n_variants: int = 5,
    use_costs: bool = True,
    slippage_bps: float = 10.0,
    dry_run: bool = False,
) -> List[SweepResult]:
    """Run Phase 1: Signal engine sweep over all variants.

    Returns:
        List of SweepResult, one per trader. Each result has .variants sorted
        by score, and .winner if one passed the criteria.
    """
    if date_str is None:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    as_of = "signal-only" if not dry_run else "DRY RUN signal-only"
    print(f"  STEP 3/5: Phase 1 — Signal Sweep ({as_of})")
    print(f"  Date: {date_str} | N_dates: {n_dates} | "
          f"Train: {train_days}d | Val: {val_days}d")
    print(f"{'='*60}")

    start = time.time()

    try:
        results = run_sweep(
            date_str=date_str,
            trader=trader,
            n_variants=n_variants,
            dry_run=dry_run,
            n_dates=n_dates,
            train_days=train_days,
            val_days=val_days,
            slippage_bps=slippage_bps,
            use_costs=use_costs,
            # Phase 2 is handled separately by step_llm_validation
            phase2=False,
        )
    except ValueError as e:
        print(f"[nightly] ⚠️  Signal sweep skipped: {e}")
        return []

    elapsed = time.time() - start
    print(f"\n[nightly] Phase 1 complete ({elapsed:.1f}s)")

    # Summarize per-trader
    for result in results:
        status = "✅" if result.winner else "❌"
        print(f"  {status} {result.trader}: baseline={result.baseline_score:.4f}, "
              f"winner={result.winner.variant_name if result.winner else 'none'}")

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline Step 4: Phase 2 — LLM Validation
# ═══════════════════════════════════════════════════════════════════════════════


def step_llm_validation(
    trader: str,
    dates: List[str],
    train_days: int = 7,
    val_days: int = 3,
    phase1_variants: int = 5,
    phase2_top_k: int = 3,
    max_llm_runs: int = 9,
    dry_run: bool = False,
) -> Tuple[Optional[PromptVariant], Dict[str, Any]]:
    """Run Phase 2: LLM validation on top K candidates.

    Calls two_phase_validate() which runs the full signal→LLM pipeline.

    Returns:
        (winner, diagnostics) — winner is None if LLM validation didn't
        confirm the signal winner.
    """
    print(f"\n{'='*60}")
    action = "DRY RUN" if dry_run else "live"
    print(f"  STEP 4/5: Phase 2 — LLM Validation ({action})")
    print(f"  Trader: {trader} | Dates: {dates[0]} → {dates[-1]} ({len(dates)} days)")
    print(f"  Train: {train_days}d | Val: {val_days}d")
    print(f"  Top K: {phase2_top_k} | Max runs: {max_llm_runs}")
    print(f"{'='*60}")

    start = time.time()

    config = ValidationConfig(
        phase1_variants=phase1_variants,
        phase2_top_k=phase2_top_k,
        max_llm_runs_per_trader=max_llm_runs,
    )

    try:
        winner, diagnostics = two_phase_validate(
            trader=trader,
            dates=dates,
            train_days=train_days,
            val_days=val_days,
            config=config,
            dry_run=dry_run,
        )
    except ValueError as e:
        print(f"[nightly] ⚠️  LLM validation skipped: {e}")
        return None, {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "trader": trader,
            "error": str(e),
            "phase1_winner": None,
            "phase2_winner": None,
            "winner": None,
            "signal_llm_divergence": False,
        }

    elapsed = time.time() - start

    status = "✅" if winner else "❌"
    print(f"\n[nightly] Phase 2 complete ({elapsed:.1f}s)")
    print(f"  {status} {trader}: winner={winner.variant_name if winner else 'none'}")
    if diagnostics.get("signal_llm_divergence"):
        print(f"  ⚠️  Signal/LLM divergence detected — no promotion")

    return winner, diagnostics


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline Step 5: Promote Winner
# ═══════════════════════════════════════════════════════════════════════════════


def step_promote(
    winner: Optional[PromptVariant],
    trader: str,
    date_str: str,
    dry_run: bool = False,
) -> Optional[str]:
    """Promote a winning variant: create git branch and log to DB.

    Args:
        winner: The winning variant (or None to skip).
        trader: Trader short name (e.g. 'kairos').
        date_str: Date string for branch naming.
        dry_run: If True, skip git operations.

    Returns:
        Branch name, or None if no winner or skipped.
    """
    if winner is None:
        print(f"\n[nightly] STEP 5/5: Promote — No winner to promote for {trader}")
        return None

    print(f"\n{'='*60}")
    action = "DRY RUN" if dry_run else "live"
    print(f"  STEP 5/5: Promote Winner ({action})")
    print(f"  Trader: {trader} | Variant: {winner.variant_name}")
    print(f"  Score: {winner.score:.4f} | "
          f"Avg val: {winner.avg_val_score:.4f} | "
          f"Win rate: {winner.win_rate:.1%}")
    print(f"{'='*60}")

    # ── Create git branch ────────────────────────────────────────────────
    # The actual branch creation logic lives in prompt_sweep.create_winner_branch.
    # We import and call it here to stay DRY.
    from src.prompt_sweep import create_winner_branch

    branch_name = create_winner_branch(
        trader_short=trader,
        variant=winner,
        date_str=date_str,
        dry_run=dry_run,
    )

    if branch_name:
        print(f"[nightly] ✅ Branch created: {branch_name}")
    else:
        print(f"[nightly] ⚠️  Branch creation skipped or failed")

    # ── Log to sweep_results table ───────────────────────────────────────
    if not dry_run:
        try:
            log_sweep_result(
                {
                    "run_at": datetime.now(timezone.utc).isoformat(),
                    "trader": trader,
                    "variant_name": winner.variant_name,
                    "variant_description": winner.description,
                    "train_date_range": "",
                    "val_date_range": date_str,
                    "baseline_score": 0.0,
                    "variant_score": winner.avg_val_score,
                    "variant_llm_score": 0.0,
                    "calmar": winner.calmar,
                    "profit_factor": winner.profit_factor,
                    "win_rate": winner.win_rate,
                    "n_trades": winner.n_trades,
                    "cost_adjusted_pnl": 0.0,
                    "promoted": True,
                    "signal_params_json": "",
                    "phase1_winner": True,
                    "phase2_winner": True,
                    "signal_llm_divergence": False,
                    "notes": f"Promoted via nightly_pipeline.py on {date_str}",
                },
                dry_run=False,
            )
            print(f"[nightly] ✅ Sweep result logged to DB")
        except Exception as e:
            print(f"[nightly] ⚠️  Failed to log sweep result: {e}")

    return branch_name


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline Step 6: Push Canvas Card
# ═══════════════════════════════════════════════════════════════════════════════


def step_canvas_card(
    results: Dict[str, Any],
    elapsed: float,
    dry_run: bool = False,
) -> Optional[str]:
    """Push a summary card to Canvas.

    Args:
        results: Dict with pipeline results per trader.
        elapsed: Total pipeline wall time in seconds.
        dry_run: If True, print card content without pushing.

    Returns:
        Card UUID if pushed, None otherwise.
    """
    if dry_run:
        print(f"\n[nightly] STEP 6/6: Canvas Card — DRY RUN (skipped)")
        return None

    print(f"\n{'='*60}")
    print("  STEP 6/6: Push Canvas Card")
    print(f"{'='*60}")

    # Build markdown summary
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"## Nightly Pipeline — {date_str}",
        f"",
        f"**Duration:** {elapsed:.1f}s",
        f"**Mode:** {'DRY RUN' if dry_run else 'Live'}",
        f"",
        f"### Results",
        f"",
        f"| Trader | Phase 1 Winner | Phase 2 Winner | Promoted | Branch |",
        f"|--------|----------------|----------------|----------|--------|",
    ]

    for trader_key, trader_result in results.get("traders", {}).items():
        p1 = trader_result.get("phase1_winner", "none")
        p2 = trader_result.get("phase2_winner", "none")
        promoted = trader_result.get("promoted", False)
        branch = trader_result.get("branch_name", "—")
        lines.append(
            f"| {trader_key} | {p1} | {p2} | "
            f"{'✅' if promoted else '❌'} | {branch or '—'} |"
        )

    lines.extend([
        f"",
        f"### Backfill",
        f"- Tickers: {results.get('backfill_tickers', 'core')}",
        f"- Days: {results.get('backfill_days', 20)}",
        f"- Cached rows: {results.get('cache_rows', 0):,}",
        f"",
        f"### Divergence Events",
    ])

    divergences = [
        (k, v) for k, v in results.get("traders", {}).items()
        if v.get("signal_llm_divergence")
    ]
    if divergences:
        for trader_key, _ in divergences:
            lines.append(f"- ⚠️ **{trader_key}**: Signal/LLM divergence detected")
    else:
        lines.append(f"- None (all clear)")

    content = "\n".join(lines)

    if dry_run:
        print(f"\n[nightly] Canvas card content (dry-run):")
        print(content)
        return None

    # Push via canvas_dashboard module
    try:
        from src.canvas_dashboard import _push_to_canvas

        result = _push_to_canvas(
            title=f"🌙 Nightly Pipeline — {date_str}",
            content=content,
            board="main",
            agent="coder",
            emoji="🌙",
            expires_days=3,
        )
        card_uuid = result.get("id")
        if card_uuid:
            print(f"[nightly] ✅ Canvas card pushed: {card_uuid}")
        else:
            print(f"[nightly] ✅ Canvas card pushed (no UUID in response)")
        return card_uuid
    except Exception as e:
        print(f"[nightly] ⚠️  Failed to push canvas card: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Ticker resolution
# ═══════════════════════════════════════════════════════════════════════════════


def resolve_tickers_for_trader(trader_short: str) -> List[str]:
    """Resolve tickers for a given trader from the backfill script's config."""
    try:
        # Import the ticker maps from backfill_bars
        sys.path.insert(0, str(SCRIPTS_DIR))
        from backfill_bars import TRADER_TICKERS, CORE_TICKERS
        trader_tickers = TRADER_TICKERS.get(trader_short, [])
        # Combine core + trader-specific, deduplicate
        all_tickers = list(dict.fromkeys(CORE_TICKERS + trader_tickers))
        return all_tickers
    except Exception:
        return ["SPY", "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOGL", "AMZN"]


def resolve_tickers(ticker_spec: str) -> List[str]:
    """Resolve a ticker spec to a list, using backfill_bars logic."""
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        from backfill_bars import resolve_tickers as _resolve
        return _resolve(ticker_spec)
    except Exception:
        return ["SPY", "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOGL", "AMZN"]


# ═══════════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ═══════════════════════════════════════════════════════════════════════════════


def nightly_pipeline(
    # Backfill
    ticker_spec: str = "core",
    backfill_days: int = 20,
    force_backfill: bool = False,
    skip_backfill: bool = False,
    # Sweep
    trader: Optional[str] = None,
    date_str: Optional[str] = None,
    n_dates: int = 20,
    train_days: int = 7,
    val_days: int = 3,
    n_variants: int = 5,
    use_costs: bool = True,
    slippage_bps: float = 10.0,
    # Phase 2
    phase2: bool = True,
    phase2_top_k: int = 3,
    max_llm_runs: int = 9,
    skip_llm: bool = False,
    # General
    dry_run: bool = False,
    # Trend card
    trend_card: bool = False,
) -> Dict[str, Any]:
    """Run the full nightly optimization pipeline.

    Args:
        ticker_spec: Ticker spec for backfill ('core', 'all', or comma-separated).
        backfill_days: Number of calendar days to backfill.
        force_backfill: Force re-fetch even if cached.
        skip_backfill: Skip the backfill step entirely.
        trader: Trader short name (e.g. 'kairos'). None = all traders.
        date_str: Reference date (YYYY-MM-DD). Default: yesterday.
        n_dates: Number of trading days for walk-forward validation.
        train_days: Training days per walk-forward window.
        val_days: Validation days per walk-forward window.
        n_variants: Number of variants to generate per trader.
        use_costs: Apply transaction costs to replay.
        slippage_bps: Slippage in basis points.
        phase2: Enable two-phase validation (signal → LLM gate).
        phase2_top_k: Top K variants for LLM validation.
        max_llm_runs: Max LLM runs per trader.
        skip_llm: Skip LLM phase entirely (signal-only).
        dry_run: If True, skip git operations and DB writes.
        trend_card: If True, push a 7-night trend card to Canvas (weekly).

    Returns:
        Dict with pipeline results for reporting.
    """
    if date_str is None:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    if trader:
        traders = [trader]
    else:
        traders = [SHORT_NAMES[tid] for tid in TRADER_IDS]

    pipeline_start = time.time()

    # ── Summary result accumulator ───────────────────────────────────────
    result: Dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "date_str": date_str,
        "traders": {},
        "backfill_tickers": ticker_spec,
        "backfill_days": backfill_days,
        "cache_rows": 0,
        "duration_seconds": 0.0,
        "errors": [],
        "warnings": [],
    }

    # ═══════════════════════════════════════════════════════════════════
    # STEP 1: Backfill
    # ═══════════════════════════════════════════════════════════════════
    if skip_backfill:
        print(f"\n[nightly] ⏭ Skipping backfill (--skip-backfill)")
    else:
        backfill_rc, backfill_elapsed = step_backfill(
            tickers=ticker_spec,
            days=backfill_days,
            force=force_backfill,
            dry_run=dry_run,
        )
        result["backfill_rc"] = backfill_rc
        result["backfill_elapsed"] = backfill_elapsed

        if backfill_rc != 0:
            result["warnings"].append(
                f"Backfill exited with code {backfill_rc}"
            )

    # ═══════════════════════════════════════════════════════════════════
    # STEP 2: Cache bars into SQLite
    # ═══════════════════════════════════════════════════════════════════
    # Resolve tickers for caching
    all_tickers = resolve_tickers(ticker_spec)
    if not all_tickers:
        all_tickers = resolve_tickers_for_trader(traders[0]) if traders else []
        result["warnings"].append("No tickers resolved, falling back to defaults")

    # Compute date range for caching
    from datetime import date as date_type
    ref_date = date_type.fromisoformat(date_str)
    cache_start = (ref_date - timedelta(days=n_dates + 5)).isoformat()

    n_cached = step_cache_bars(
        tickers=all_tickers,
        start_date=cache_start,
        end_date=date_str,
        dry_run=dry_run,
    )
    result["cache_rows"] = n_cached

    # ═══════════════════════════════════════════════════════════════════
    # STEP 3: Phase 1 — Signal Sweep (or combined Phase 1+2)
    # ═══════════════════════════════════════════════════════════════════
    for trader_short in traders:
        print(f"\n{'─'*60}")
        print(f"  Processing trader: {trader_short}")
        print(f"{'─'*60}")

        trader_result: Dict[str, Any] = {
            "phase1_winner": None,
            "phase2_winner": None,
            "promoted": False,
            "branch_name": None,
            "signal_llm_divergence": False,
            "phase2_score": 0.0,
        }

        if phase2 and not skip_llm:
            # ── Full two-phase validation ────────────────────────────
            # Get trading dates for this run
            dates = get_trading_days(n_dates, end_date=date_str)
            if len(dates) < train_days + val_days:
                msg = (
                    f"Not enough dates for {trader_short}: "
                    f"need {train_days + val_days}, got {len(dates)}. "
                    f"Falling back to signal-only sweep."
                )
                print(f"[nightly] ⚠️  {msg}")
                result["warnings"].append(msg)

                # Fall back to signal-only
                signal_results = step_signal_sweep(
                    trader=trader_short,
                    date_str=date_str,
                    n_dates=n_dates,
                    train_days=train_days,
                    val_days=val_days,
                    n_variants=n_variants,
                    use_costs=use_costs,
                    slippage_bps=slippage_bps,
                    dry_run=dry_run,
                )

                if signal_results:
                    sr = signal_results[0]
                    trader_result["phase1_winner"] = (
                        sr.winner.variant_name if sr.winner else None
                    )
                    trader_result["branch_name"] = sr.branch_name

                    # Promote if we have a winner
                    if sr.winner:
                        branch = step_promote(
                            sr.winner, trader_short, date_str, dry_run=dry_run
                        )
                        trader_result["promoted"] = branch is not None
                        trader_result["branch_name"] = branch or sr.branch_name
            else:
                # Full two-phase validation
                winner, diagnostics = step_llm_validation(
                    trader=trader_short,
                    dates=dates,
                    train_days=train_days,
                    val_days=val_days,
                    phase1_variants=n_variants,
                    phase2_top_k=phase2_top_k,
                    max_llm_runs=max_llm_runs,
                    dry_run=dry_run,
                )

                trader_result["phase1_winner"] = diagnostics.get("phase1_winner")
                trader_result["phase2_winner"] = diagnostics.get("phase2_winner")
                trader_result["signal_llm_divergence"] = diagnostics.get(
                    "signal_llm_divergence", False
                )
                trader_result["phase2_score"] = diagnostics.get(
                    "baseline_llm_score", 0.0
                )

                # Promote if winner passed both phases
                if winner:
                    branch = step_promote(
                        winner, trader_short, date_str, dry_run=dry_run
                    )
                    trader_result["promoted"] = branch is not None
                    trader_result["branch_name"] = branch

        else:
            # ── Signal-only sweep (Phase 1 only) ─────────────────────
            signal_results = step_signal_sweep(
                trader=trader_short,
                date_str=date_str,
                n_dates=n_dates,
                train_days=train_days,
                val_days=val_days,
                n_variants=n_variants,
                use_costs=use_costs,
                slippage_bps=slippage_bps,
                dry_run=dry_run,
            )

            if signal_results:
                sr = signal_results[0]
                trader_result["phase1_winner"] = (
                    sr.winner.variant_name if sr.winner else None
                )
                trader_result["branch_name"] = sr.branch_name

                # Promote if we have a winner
                if sr.winner:
                    branch = step_promote(
                        sr.winner, trader_short, date_str, dry_run=dry_run
                    )
                    trader_result["promoted"] = branch is not None
                    trader_result["branch_name"] = branch or sr.branch_name

        result["traders"][trader_short] = trader_result

    # ═══════════════════════════════════════════════════════════════════
    # STEP 6: Canvas Card
    # ═══════════════════════════════════════════════════════════════════
    pipeline_elapsed = time.time() - pipeline_start
    result["duration_seconds"] = pipeline_elapsed

    step_canvas_card(result, pipeline_elapsed, dry_run=dry_run)

    # ═══════════════════════════════════════════════════════════════════
    # STEP 7: 7-Night Trend Card (weekly — Mondays only)
    # ═══════════════════════════════════════════════════════════════════
    if trend_card:
        try:
            from scripts.nightly_trend_card import (
                get_7_night_data,
                generate_markdown,
                push_to_canvas as push_trend,
            )

            print(f"\n{'='*60}")
            print("  STEP 7/7: 7-Night Trend Card")
            print(f"{'='*60}")

            trend_data = get_7_night_data(trader_id=None, nights=7)
            if trend_data:
                trend_content = generate_markdown(trend_data)
                push_trend(trend_content, dry_run=dry_run)
                result["trend_card"] = "pushed"
            else:
                print("[nightly] ⚠️  No trend data — skipping trend card")
                result["trend_card"] = "no_data"
        except Exception as e:
            print(f"[nightly] ⚠️  Trend card failed: {e}")
            result["trend_card"] = f"error: {e}"

    # ── Final summary ─────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  NIGHTLY PIPELINE COMPLETE")
    print(f"  Duration: {pipeline_elapsed:.1f}s")
    print(f"{'='*60}")

    for trader_short, tr in result["traders"].items():
        promoted = tr.get("promoted", False)
        divergence = tr.get("signal_llm_divergence", False)
        p1 = tr.get("phase1_winner", "none")
        p2 = tr.get("phase2_winner", "none")
        branch = tr.get("branch_name", "—")

        status = "✅" if promoted else ("⚠️" if divergence else "❌")
        print(f"  {status} {trader_short}: "
              f"P1={p1}, P2={p2}, "
              f"{'promoted → ' + branch if promoted else 'not promoted'}")

    if result["warnings"]:
        print(f"\n  ⚠️  Warnings ({len(result['warnings'])}):")
        for w in result["warnings"]:
            print(f"    - {w}")

    if result["errors"]:
        print(f"\n  ✗ Errors ({len(result['errors'])}):")
        for e in result["errors"]:
            print(f"    - {e}")

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="Nightly Optimization Pipeline — unified cron entry point",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                          # defaults
  %(prog)s --dates 20 --train 15 --val 5            # walk-forward params
  %(prog)s --trader kairos --dry-run                # single trader, dry-run
  %(prog)s --skip-backfill --skip-llm               # signal-only, no backfill
  %(prog)s --tickers all --days 30                  # full universe, 30 days
        """,
    )

    # Backfill options
    backfill = parser.add_argument_group("Backfill options")
    backfill.add_argument(
        "--tickers", type=str, default="core",
        help="Ticker spec: 'core', 'all', or comma-separated (default: core)",
    )
    backfill.add_argument(
        "--days", type=int, default=20,
        help="Calendar days to backfill (default: 20)",
    )
    backfill.add_argument(
        "--force-backfill", action="store_true",
        help="Force re-fetch all backfill data",
    )
    backfill.add_argument(
        "--skip-backfill", action="store_true",
        help="Skip the backfill step",
    )

    # Sweep options
    sweep = parser.add_argument_group("Sweep options")
    sweep.add_argument(
        "--trader", type=str, default=None,
        help="Trader short name (e.g. 'kairos'). Default: all traders.",
    )
    sweep.add_argument(
        "--date", type=str, default=None,
        help="Reference date YYYY-MM-DD (default: yesterday)",
    )
    sweep.add_argument(
        "--dates", type=int, default=20,
        help="Number of trading days for walk-forward (default: 20)",
    )
    sweep.add_argument(
        "--train", type=int, default=7,
        help="Training days per walk-forward window (default: 7)",
    )
    sweep.add_argument(
        "--val", type=int, default=3,
        help="Validation days per walk-forward window (default: 3)",
    )
    sweep.add_argument(
        "--variants", type=int, default=5,
        help="Number of prompt variants to generate per trader (default: 5)",
    )
    sweep.add_argument(
        "--no-costs", action="store_true",
        help="Disable transaction costs",
    )

    # Phase 2 (LLM) options
    llm = parser.add_argument_group("LLM validation options")
    llm.add_argument(
        "--skip-llm", action="store_true",
        help="Skip LLM validation phase (signal-only)",
    )
    llm.add_argument(
        "--phase2-top-k", type=int, default=3,
        help="Top K variants for LLM validation (default: 3)",
    )
    llm.add_argument(
        "--max-llm-runs", type=int, default=9,
        help="Max LLM runs per trader (default: 9)",
    )

    # General
    general = parser.add_argument_group("General options")
    general.add_argument(
        "--dry-run", action="store_true",
        help="Skip git operations, DB writes, and canvas pushes",
    )
    general.add_argument(
        "--slippage", type=float, default=10.0,
        help="Slippage in basis points (default: 10.0)",
    )
    general.add_argument(
        "--trend-card", action="store_true",
        help="Push a 7-night trend card to Canvas after pipeline (weekly)",
    )

    args = parser.parse_args()

    # ── Run pipeline ────────────────────────────────────────────────────
    result = nightly_pipeline(
        ticker_spec=args.tickers,
        backfill_days=args.days,
        force_backfill=args.force_backfill,
        skip_backfill=args.skip_backfill,
        trader=args.trader,
        date_str=args.date,
        n_dates=args.dates,
        train_days=args.train,
        val_days=args.val,
        n_variants=args.variants,
        use_costs=not args.no_costs,
        slippage_bps=args.slippage,
        phase2=not args.skip_llm,
        phase2_top_k=args.phase2_top_k,
        max_llm_runs=args.max_llm_runs,
        skip_llm=args.skip_llm,
        dry_run=args.dry_run,
        trend_card=args.trend_card,
    )

    # Exit non-zero if no winners
    any_promoted = any(
        tr.get("promoted") for tr in result["traders"].values()
    )
    if not any_promoted and not args.dry_run:
        print("\n[nightly] No winners promoted. Pipeline completed without promotions.")
        # Don't exit non-zero — this is a normal outcome, not a failure
        sys.exit(0)


if __name__ == "__main__":
    main()