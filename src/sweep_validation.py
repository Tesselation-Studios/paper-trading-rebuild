#!/usr/bin/env python3
"""
Two-Phase Validation — Signal sweep → LLM validation gate for prompt variants.

Phase 1 (cheap): SignalEngine-based sweep across all N variants.
Phase 2 (expensive): LLM replay on top K candidates only.
Gate: both phases must agree for winner promotion.

Usage:
    from src.sweep_validation import two_phase_validate, ValidationConfig

    winner = two_phase_validate("kairos", dates, train_days=5, val_days=3)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

# ── Project paths ────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = PROJECT_DIR / "agents"
DB_DSN = "postgresql://trader:***@192.168.1.179:5433/trading"

# ── Import from prompt_sweep ─────────────────────────────────────────────────
_SRC_DIR = str(Path(__file__).resolve().parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from prompt_sweep import (  # type: ignore[import]
    PromptVariant,
    SHORT_NAMES,
    TRADER_IDS,
    _extract_params_from_prompt,
    generate_variants,
    get_trading_days,
    build_walk_forward_windows,
    read_trader_prompt,
    score_variant,
    score_variants,
    _load_dates_data,
    _compute_walk_forward_metrics,
    run_sweep,
)

# Metrics from the rebuild repo (local — we're inside it)
from metrics import compute_calmar, compute_profit_factor  # type: ignore[import]

# ── Default initial cash matches replay_controller.py ─────────────────────────
DEFAULT_INITIAL_CASH = 10000.0

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ValidationConfig:
    """Configuration for the two-phase validation pipeline.

    Attributes:
        phase1_variants: Total variants to test in signal phase.
        phase2_top_k: Top K variants from Phase 1 to validate with LLM.
        max_llm_runs_per_trader: Hard budget cap on LLM replay runs.
        llm_cost_estimate_per_run: Estimated token cost per LLM replay (~$0.15).
    """

    phase1_variants: int = 5
    phase2_top_k: int = 3
    max_llm_runs_per_trader: int = 9
    llm_cost_estimate_per_run: float = 0.15


@dataclass
class Phase2Result:
    """Result from Phase 2 LLM validation for one variant+window pair."""

    variant_name: str
    val_date: str
    model_id: str
    total_pnl: float
    win_rate: float
    n_trades: int
    errors: int
    wall_time_seconds: float = 0.0
    llm_score: float = 0.0

    @classmethod
    def from_replay_report(
        cls,
        variant_name: str,
        val_date: str,
        model_id: str,
        report: dict,
    ) -> "Phase2Result":
        """Parse a replay_controller.py JSON report into a Phase2Result."""
        trader_stats = report.get("traders", {}).get(model_id, {})
        if not trader_stats:
            return cls(
                variant_name=variant_name,
                val_date=val_date,
                model_id=model_id,
                total_pnl=0.0,
                win_rate=0.0,
                n_trades=0,
                errors=1,
                llm_score=0.0,
            )

        total_pnl = float(trader_stats.get("total_pnl", 0.0))
        n_trades = len(trader_stats.get("trades", []))
        win_rate = float(trader_stats.get("win_rate", 0.0))
        errors = int(trader_stats.get("errors", 0))
        wall_time = float(report.get("wall_time_seconds", 0.0))

        # Compute LLM score: normalize PnL as percentage return
        # Positive PnL → positive score, negative → negative
        llm_score = total_pnl / DEFAULT_INITIAL_CASH * 100.0

        return cls(
            variant_name=variant_name,
            val_date=val_date,
            model_id=model_id,
            total_pnl=total_pnl,
            win_rate=win_rate,
            n_trades=n_trades,
            errors=errors,
            wall_time_seconds=wall_time,
            llm_score=llm_score,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Database: sweep_results table (Postgres)
# ═══════════════════════════════════════════════════════════════════════════════


def _get_pg_conn() -> psycopg2.extensions.connection:
    """Get a sync Postgres connection to the trading database."""
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False
    return conn


def _ensure_sweep_table() -> None:
    """No-op: sweep_results table is managed by schema.sql migrations."""
    pass


def log_sweep_result(
    result: Dict[str, Any],
    dry_run: bool = False,
) -> Optional[int]:
    """Log a two-phase validation result to trading.sweep_results.

    Creates a sweep_run entry if needed, then inserts the result with
    two-phase metadata stored in the validation_meta JSONB column.

    Args:
        result: Dict with keys matching sweep_results + two-phase columns.
        dry_run: If True, skip DB write.

    Returns:
        Row id or None.
    """
    if dry_run:
        return None

    conn = _get_pg_conn()
    try:
        cur = conn.cursor()

        # Ensure a sweep_run exists for this trader+time combination
        cur.execute(
            """INSERT INTO trading.sweep_runs
               (trader_id, n_scenarios, started_at)
               VALUES (%s, %s, %s)
               ON CONFLICT DO NOTHING
               RETURNING id""",
            (
                result.get("trader", ""),
                result.get("n_trades", 0),
                result.get("run_at", datetime.now().isoformat()),
            ),
        )
        run_row = cur.fetchone()
        if run_row:
            run_id = run_row[0]
        else:
            # Get existing run_id
            cur.execute(
                """SELECT id FROM trading.sweep_runs
                   WHERE trader_id = %s
                   ORDER BY started_at DESC LIMIT 1""",
                (result.get("trader", ""),),
            )
            run_row = cur.fetchone()
            run_id = run_row[0] if run_row else 1

        # Build validation_meta JSONB with two-phase specific data
        validation_meta = json.dumps({
            "variant_name": result.get("variant_name", ""),
            "variant_description": result.get("variant_description", ""),
            "train_date_range": result.get("train_date_range", ""),
            "val_date_range": result.get("val_date_range", ""),
            "baseline_score": result.get("baseline_score", 0.0),
            "variant_score": result.get("variant_score", 0.0),
            "variant_llm_score": result.get("variant_llm_score", 0.0),
            "cost_adjusted_pnl": result.get("cost_adjusted_pnl", 0.0),
            "promoted": bool(result.get("promoted", False)),
            "branch_name": result.get("branch_name", ""),
            "signal_params_json": result.get("signal_params_json", ""),
            "phase1_winner": bool(result.get("phase1_winner", False)),
            "phase2_winner": bool(result.get("phase2_winner", False)),
            "signal_llm_divergence": bool(result.get("signal_llm_divergence", False)),
            "notes": result.get("notes", ""),
        })

        cur.execute(
            """INSERT INTO trading.sweep_results
               (run_id, trader_id, variant_id, params_hash,
                objective_score, calmar, profit_factor, total_pnl,
                n_trades, win_rate, validation_meta)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
               ON CONFLICT (run_id, variant_id) DO UPDATE SET
                objective_score = EXCLUDED.objective_score,
                calmar          = EXCLUDED.calmar,
                profit_factor   = EXCLUDED.profit_factor,
                total_pnl       = EXCLUDED.total_pnl,
                n_trades        = EXCLUDED.n_trades,
                win_rate        = EXCLUDED.win_rate,
                validation_meta = EXCLUDED.validation_meta
               RETURNING id""",
            (
                run_id,
                result.get("trader", ""),
                abs(hash(result.get("variant_name", ""))) % 100000,  # variant_id as int
                "",  # params_hash — unused for two-phase
                result.get("variant_score", 0.0),  # objective_score
                result.get("calmar", 0.0),
                result.get("profit_factor", 0.0),
                result.get("cost_adjusted_pnl", 0.0),  # total_pnl
                result.get("n_trades", 0),
                result.get("win_rate", 0.0),
                validation_meta,
            ),
        )
        conn.commit()
        result_row = cur.fetchone()
        row_id = result_row[0] if result_row else None
        cur.close()
        return row_id
    except Exception as e:
        conn.rollback()
        print(f"[sweep_validation] DB error logging result: {e}", file=sys.stderr)
        return None
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1: Signal-based sweep
# ═══════════════════════════════════════════════════════════════════════════════


def run_phase1_signal_sweep(
    trader: str,
    dates: List[str],
    train_days: int,
    val_days: int,
    n_variants: int = 5,
    seed: int = 42,
) -> Tuple[List[PromptVariant], PromptVariant, PromptVariant, List[Tuple[List[str], List[str]]]]:
    """Phase 1: Run the cheap signal-based sweep.

    Generates N variants, scores each across walk-forward validation windows
    using SignalEngine (deterministic, no LLM), and returns variants sorted
    by average validation score.

    Args:
        trader: Trader short name (e.g., 'kairos').
        dates: Chronological trading dates (oldest first).
        train_days: Training days per walk-forward window.
        val_days: Validation days per walk-forward window.
        n_variants: Number of variants to generate and test.
        seed: Random seed for reproducible variant generation.

    Returns:
        Tuple of:
        - variants: List of PromptVariant sorted by avg_val_score descending.
        - baseline: PromptVariant for the current production prompt.
        - phase1_winner: The top variant (or None if none beats baseline).
        - windows: List of (train_dates, val_dates) tuples.
    """
    prompt_text = read_trader_prompt(trader)
    baseline_params = _extract_params_from_prompt(prompt_text)

    # Build walk-forward windows
    windows = build_walk_forward_windows(dates, train_days, val_days)
    if not windows:
        raise ValueError(
            f"Not enough dates for walk-forward: need at least "
            f"{train_days + val_days} days, got {len(dates)}."
        )

    # Create baseline variant
    baseline = PromptVariant(
        trader=trader,
        variant_id=0,
        variant_name="baseline",
        description="Current production prompt",
        prompt_text=prompt_text,
        signal_params=baseline_params,
        baseline_params=baseline_params,
    )

    # Score baseline on each validation window
    baseline_val_scores: List[float] = []
    for _train_dates, val_dates in windows:
        val_ticks = _load_dates_data(val_dates)
        bs, _ = score_variant(baseline, val_ticks)
        baseline_val_scores.append(bs)

    # Generate and score variants
    variants = generate_variants(trader, prompt_text, n_variants, seed=seed)

    for variant in variants:
        val_scores: List[float] = []
        for _train_dates, val_dates in windows:
            val_ticks = _load_dates_data(val_dates)
            vs, _ = score_variant(variant, val_ticks)
            val_scores.append(vs)

        variant.val_scores = val_scores
        metrics = _compute_walk_forward_metrics(val_scores, baseline_val_scores)
        variant.avg_val_score = metrics["avg_val_score"]
        variant.val_stability = metrics["val_stability"]
        variant.win_rate = metrics["win_rate"]

        # Single-date score on the last validation window for compatibility
        last_dates = windows[-1][1]
        last_ticks = _load_dates_data(last_dates)
        ls, lr = score_variant(variant, last_ticks)
        variant.score = ls
        variant.calmar = float(compute_calmar(lr.returns, lr.equity_curve))
        variant.profit_factor = float(
            compute_profit_factor([t.pnl for t in lr.trades])
        )
        variant.n_trades = len(lr.trades)

    # Sort by avg_val_score descending
    variants.sort(key=lambda v: v.avg_val_score, reverse=True)

    # Determine Phase 1 winner
    phase1_winner: Optional[PromptVariant] = None
    baseline_metrics = _compute_walk_forward_metrics(
        baseline_val_scores, baseline_val_scores
    )

    for v in variants:
        passes_win_rate = v.win_rate >= 0.6
        passes_avg = v.avg_val_score > baseline_metrics["avg_val_score"] + 0.05
        passes_stability = (
            baseline_metrics["val_stability"] == 0.0
            or v.val_stability < 2.0 * baseline_metrics["val_stability"]
        )
        if passes_win_rate and passes_avg and passes_stability:
            phase1_winner = v
            break

    return variants, baseline, phase1_winner, windows


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2: LLM validation
# ═══════════════════════════════════════════════════════════════════════════════


def _run_replay_for_date(
    trader_short: str,
    date_str: str,
    initial_cash: float = DEFAULT_INITIAL_CASH,
    timeout: int = 180,
) -> Optional[dict]:
    """Run replay_controller.py for a single date and return the parsed report.

    Args:
        trader_short: Trader short name (e.g., 'kairos').
        date_str: Trading date in YYYY-MM-DD format.
        initial_cash: Starting cash for the replay.
        timeout: Seconds before killing the subprocess.

    Returns:
        Parsed JSON report dict, or None on failure.
    """
    model_id = f"trader-{trader_short}"

    cmd = [
        sys.executable,
        str(PROJECT_DIR / "src" / "replay_controller.py"),
        "--date", date_str,
        "--traders", model_id,
        "--cash", str(initial_cash),
        "--interval", "15",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_DIR),
        )

        if result.returncode != 0:
            print(
                f"[sweep_validation] replay_controller failed for {date_str}: "
                f"rc={result.returncode} stderr={result.stderr[:200]}",
                file=sys.stderr,
            )
            return None

        # Parse JSON from stdout — the report comes after any log lines
        # replay_controller.py prints logs to stdout, then a JSON block
        # The report always starts with '{"date":' — find that marker
        stdout = result.stdout
        json_marker = '{"date"'
        json_start = stdout.rfind(json_marker)
        if json_start == -1:
            print(
                f"[sweep_validation] No JSON found in replay output for {date_str}",
                file=sys.stderr,
            )
            return None

        json_str = stdout[json_start:].strip()
        report = json.loads(json_str)
        return report

    except subprocess.TimeoutExpired:
        print(
            f"[sweep_validation] replay_controller timed out after {timeout}s for {date_str}",
            file=sys.stderr,
        )
        return None
    except (json.JSONDecodeError, ValueError) as e:
        print(
            f"[sweep_validation] Failed to parse replay report for {date_str}: {e}",
            file=sys.stderr,
        )
        return None


def _swap_trader_prompt(trader_short: str, prompt_text: str) -> str:
    """Replace the trader's AGENTS.md with the variant prompt.

    Returns the original prompt text so it can be restored later.
    """
    agents_md = AGENTS_DIR / f"trader-{trader_short}" / "AGENTS.md"
    original = agents_md.read_text()
    agents_md.write_text(prompt_text)
    return original


def _restore_trader_prompt(trader_short: str, original_text: str) -> None:
    """Restore the trader's AGENTS.md to its original content."""
    agents_md = AGENTS_DIR / f"trader-{trader_short}" / "AGENTS.md"
    agents_md.write_text(original_text)


def run_phase2_llm_validation(
    trader: str,
    top_variants: List[PromptVariant],
    baseline: PromptVariant,
    val_dates: List[str],
    max_runs: int = 9,
    dry_run: bool = False,
) -> Tuple[Dict[str, float], Dict[str, List[Phase2Result]]]:
    """Phase 2: Run LLM replays on top K variants.

    For each variant, swaps the trader's AGENTS.md, runs replay_controller.py
    on each validation date, parses the total PnL, and restores the original prompt.

    Args:
        trader: Trader short name (e.g., 'kairos').
        top_variants: Top K variants from Phase 1 (sorted by score).
        baseline: The baseline PromptVariant.
        val_dates: List of validation date strings.
        max_runs: Maximum number of LLM replay runs (budget cap).
        dry_run: If True, simulate results without running replay_controller.

    Returns:
        Tuple of:
        - llm_scores: Dict mapping variant_name → average LLM score.
        - all_results: Dict mapping variant_name → list of Phase2Result.
    """
    # Compute how many runs we can afford
    # Each (variant, date) pair is one run
    n_variants = min(len(top_variants), max_runs // max(len(val_dates), 1))
    n_variants = max(n_variants, 1) if top_variants else 0
    runs_needed = n_variants * len(val_dates) + len(val_dates)  # +1 for baseline

    if runs_needed > max_runs:
        # Reduce variants to fit budget
        n_variants = max((max_runs - len(val_dates)) // max(len(val_dates), 1), 0)

    variants_to_test = top_variants[:n_variants]
    print(
        f"[sweep_validation] Phase 2: {len(variants_to_test)} variants × "
        f"{len(val_dates)} dates + baseline = "
        f"{len(variants_to_test) * len(val_dates) + len(val_dates)} LLM runs "
        f"(budget: {max_runs})"
    )

    llm_scores: Dict[str, float] = {}
    all_results: Dict[str, List[Phase2Result]] = {}

    model_id = f"trader-{trader}"

    # Save original prompt so we can restore it
    original_prompt = read_trader_prompt(trader)

    try:
        # First: score baseline
        baseline_results: List[Phase2Result] = []
        for date_str in val_dates:
            if dry_run:
                result = Phase2Result(
                    variant_name="baseline",
                    val_date=date_str,
                    model_id=model_id,
                    total_pnl=50.0,
                    win_rate=0.5,
                    n_trades=5,
                    errors=0,
                    llm_score=50.0 / DEFAULT_INITIAL_CASH * 100.0,
                )
            else:
                _swap_trader_prompt(trader, baseline.prompt_text)
                report = _run_replay_for_date(trader, date_str)
                _restore_trader_prompt(trader, original_prompt)
                if report is None:
                    result = Phase2Result(
                        variant_name="baseline",
                        val_date=date_str,
                        model_id=model_id,
                        total_pnl=0.0,
                        win_rate=0.0,
                        n_trades=0,
                        errors=1,
                        llm_score=0.0,
                    )
                else:
                    result = Phase2Result.from_replay_report(
                        "baseline", date_str, model_id, report
                    )
            baseline_results.append(result)

        baseline_avg_score = (
            sum(r.llm_score for r in baseline_results) / len(baseline_results)
            if baseline_results else 0.0
        )
        llm_scores["baseline"] = baseline_avg_score
        all_results["baseline"] = baseline_results

        # Then: score each variant
        for variant in variants_to_test:
            var_results: List[Phase2Result] = []
            for date_str in val_dates:
                if dry_run:
                    # Synthetic: +10% over baseline for winners, -5% for others
                    synth_pnl = baseline_avg_score * (
                        1.1 if variant.variant_name == top_variants[0].variant_name
                        else 0.95
                    )
                    result = Phase2Result(
                        variant_name=variant.variant_name,
                        val_date=date_str,
                        model_id=model_id,
                        total_pnl=synth_pnl,
                        win_rate=0.5,
                        n_trades=5,
                        errors=0,
                        llm_score=synth_pnl,
                    )
                else:
                    _swap_trader_prompt(trader, variant.prompt_text)
                    report = _run_replay_for_date(trader, date_str)
                    _restore_trader_prompt(trader, original_prompt)
                    if report is None:
                        result = Phase2Result(
                            variant_name=variant.variant_name,
                            val_date=date_str,
                            model_id=model_id,
                            total_pnl=0.0,
                            win_rate=0.0,
                            n_trades=0,
                            errors=1,
                            llm_score=0.0,
                        )
                    else:
                        result = Phase2Result.from_replay_report(
                            variant.variant_name, date_str, model_id, report
                        )
                var_results.append(result)

            avg_score = (
                sum(r.llm_score for r in var_results) / len(var_results)
                if var_results else 0.0
            )
            llm_scores[variant.variant_name] = avg_score
            all_results[variant.variant_name] = var_results

            print(
                f"  {variant.variant_name}: LLM score = {avg_score:.2f}% "
                f"(baseline: {baseline_avg_score:.2f}%)"
            )

    finally:
        # Always restore original prompt
        if not dry_run:
            _restore_trader_prompt(trader, original_prompt)

    return llm_scores, all_results


# ═══════════════════════════════════════════════════════════════════════════════
# Two-Phase Orchestrator
# ═══════════════════════════════════════════════════════════════════════════════


def two_phase_validate(
    trader: str,
    dates: List[str],
    train_days: int,
    val_days: int,
    config: Optional[ValidationConfig] = None,
    dry_run: bool = False,
) -> Tuple[Optional[PromptVariant], Dict[str, Any]]:
    """Full two-phase validation pipeline.

    1. Phase 1: Signal sweep all N variants → sort by score.
    2. Filter to top K variants.
    3. Phase 2: LLM validate top K on validation windows.
    4. Compare LLM scores against baseline LLM score.
    5. If winner beats baseline in BOTH phases → promote.
       If winner only wins signal phase → log divergence, don't promote.

    Args:
        trader: Trader short name (e.g., 'kairos').
        dates: Chronological trading dates (oldest first).
        train_days: Training days per walk-forward window.
        val_days: Validation days per walk-forward window.
        config: ValidationConfig, or defaults if None.
        dry_run: If True, simulate LLM phase without real replay calls.

    Returns:
        Tuple of:
        - winner: PromptVariant that passed both phases, or None.
        - diagnostics: Dict with full results for logging.
    """
    if config is None:
        config = ValidationConfig()

    run_at = datetime.now().isoformat()
    diagnostics: Dict[str, Any] = {
        "run_at": run_at,
        "trader": trader,
        "config": {
            "phase1_variants": config.phase1_variants,
            "phase2_top_k": config.phase2_top_k,
            "max_llm_runs": config.max_llm_runs_per_trader,
        },
        "phase1_winner": None,
        "phase2_scores": {},
        "signal_llm_divergence": False,
        "winner": None,
    }

    print(f"\n[two_phase_validate] {trader} — Phase 1: Signal sweep")
    print(f"[two_phase_validate] Dates: {dates[0]} → {dates[-1]} ({len(dates)} days)")
    print(f"[two_phase_validate] Windows: train={train_days}d, val={val_days}d")

    # ── Phase 1: Signal sweep ────────────────────────────────────────────
    variants, baseline, phase1_winner, windows = run_phase1_signal_sweep(
        trader=trader,
        dates=dates,
        train_days=train_days,
        val_days=val_days,
        n_variants=config.phase1_variants,
    )

    print(f"[two_phase_validate] Phase 1 complete: {len(variants)} variants scored")
    if phase1_winner:
        print(
            f"[two_phase_validate] Phase 1 winner: {phase1_winner.variant_name} "
            f"(avg_val={phase1_winner.avg_val_score:.4f})"
        )
    else:
        print("[two_phase_validate] No Phase 1 winner (none passed walk-forward criteria)")

    diagnostics["phase1_winner"] = phase1_winner.variant_name if phase1_winner else None
    diagnostics["phase1_variants"] = [
        {
            "name": v.variant_name,
            "avg_val_score": v.avg_val_score,
            "win_rate": v.win_rate,
            "val_stability": v.val_stability,
        }
        for v in variants
    ]

    # ── Gather validation dates from windows ─────────────────────────────
    # Use the last validation date of each window as the representative
    val_dates: List[str] = []
    for _train_dates, window_val_dates in windows:
        if window_val_dates:
            val_dates.append(window_val_dates[-1])

    if not val_dates:
        print("[two_phase_validate] No validation dates available, stopping")
        diagnostics["notes"] = "No validation dates available"
        return None, diagnostics

    print(
        f"[two_phase_validate] Validation dates for Phase 2: "
        f"{val_dates[0]} → {val_dates[-1]} ({len(val_dates)} dates)"
    )

    # ── Select top K variants for Phase 2 ────────────────────────────────
    top_k = min(config.phase2_top_k, len(variants))
    top_variants = variants[:top_k]
    print(
        f"[two_phase_validate] Phase 2 candidates: "
        f"{', '.join(v.variant_name for v in top_variants)}"
    )

    # ── Phase 2: LLM validation ──────────────────────────────────────────
    print(f"\n[two_phase_validate] {trader} — Phase 2: LLM validation")

    llm_scores, all_results = run_phase2_llm_validation(
        trader=trader,
        top_variants=top_variants,
        baseline=baseline,
        val_dates=val_dates,
        max_runs=config.max_llm_runs_per_trader,
        dry_run=dry_run,
    )

    diagnostics["phase2_scores"] = llm_scores
    diagnostics["phase2_results"] = {
        name: [
            {
                "date": r.val_date,
                "pnl": r.total_pnl,
                "score": r.llm_score,
                "win_rate": r.win_rate,
                "trades": r.n_trades,
            }
            for r in results
        ]
        for name, results in all_results.items()
    }

    baseline_llm_score = llm_scores.get("baseline", 0.0)

    # ── Compare Phase 1 winner with Phase 2 winner ───────────────────────
    # Find the variant with the highest LLM score
    best_variant_name = None
    best_llm_score = float("-inf")
    for name, score in llm_scores.items():
        if name == "baseline":
            continue
        if score > best_llm_score:
            best_llm_score = score
            best_variant_name = name

    # Determine Phase 2 winner
    phase2_winner: Optional[PromptVariant] = None
    if best_variant_name and best_llm_score > baseline_llm_score:
        for v in variants:
            if v.variant_name == best_variant_name:
                phase2_winner = v
                break
        print(
            f"[two_phase_validate] Phase 2 winner: {best_variant_name} "
            f"({best_llm_score:.2f}% vs baseline {baseline_llm_score:.2f}%)"
        )
    else:
        print(
            f"[two_phase_validate] No Phase 2 winner "
            f"(best={best_variant_name}: {best_llm_score:.2f}%, "
            f"baseline={baseline_llm_score:.2f}%)"
        )

    # ── Gate: both phases must agree ─────────────────────────────────────
    final_winner: Optional[PromptVariant] = None
    signal_llm_divergence = False

    if phase1_winner and phase2_winner:
        if phase1_winner.variant_name == phase2_winner.variant_name:
            # BOTH phases agree on the same variant → promote!
            final_winner = phase1_winner
            print(
                f"\n[two_phase_validate] ✅ AGREEMENT: {final_winner.variant_name} "
                f"wins both phases → PROMOTE"
            )
        else:
            # Phase 1 winner ≠ Phase 2 winner → divergence
            signal_llm_divergence = True
            print(
                f"\n[two_phase_validate] ⚠️  DIVERGENCE: "
                f"Signal winner ({phase1_winner.variant_name}) ≠ "
                f"LLM winner ({phase2_winner.variant_name}) → NO PROMOTION"
            )
    elif phase1_winner and not phase2_winner:
        signal_llm_divergence = True
        print(
            f"\n[two_phase_validate] ⚠️  DIVERGENCE: "
            f"Signal winner ({phase1_winner.variant_name}) "
            f"did not beat baseline in LLM → NO PROMOTION"
        )

    diagnostics["signal_llm_divergence"] = signal_llm_divergence
    diagnostics["phase2_winner"] = phase2_winner.variant_name if phase2_winner else None
    diagnostics["winner"] = final_winner.variant_name if final_winner else None
    diagnostics["baseline_llm_score"] = baseline_llm_score

    # ── Log results to sweep_results table ───────────────────────────────
    train_range = f"{dates[0]}:{dates[-1]}" if dates else ""
    val_range = f"{val_dates[0]}:{val_dates[-1]}" if val_dates else ""
    original_prompt_text = read_trader_prompt(trader)

    # Log baseline
    log_sweep_result(
        {
            "run_at": run_at,
            "trader": trader,
            "variant_name": "baseline",
            "variant_description": "Current production prompt",
            "train_date_range": train_range,
            "val_date_range": val_range,
            "baseline_score": 0.0,
            "variant_score": 0.0,
            "variant_llm_score": baseline_llm_score,
            "calmar": 0.0,
            "profit_factor": 0.0,
            "win_rate": 0.0,
            "n_trades": 0,
            "cost_adjusted_pnl": 0.0,
            "promoted": False,
            "signal_params_json": "",
            "phase1_winner": False,
            "phase2_winner": phase2_winner is None,
            "signal_llm_divergence": signal_llm_divergence,
            "notes": "",
        },
        dry_run=dry_run,
    )

    # Log each variant
    import dataclasses as _dc
    for v in variants:
        vs_llm = llm_scores.get(v.variant_name, 0.0)
        is_phase1_win = phase1_winner is not None and v.variant_name == phase1_winner.variant_name
        is_phase2_win = phase2_winner is not None and v.variant_name == phase2_winner.variant_name
        is_final_win = final_winner is not None and v.variant_name == final_winner.variant_name
        is_divergent = signal_llm_divergence and is_phase1_win

        signal_params_dict = {
            f.name: getattr(v.signal_params, f.name)
            for f in _dc.fields(type(v.signal_params))
            if f.name != "_BOUNDS"
        }

        log_sweep_result(
            {
                "run_at": run_at,
                "trader": trader,
                "variant_name": v.variant_name,
                "variant_description": v.description,
                "train_date_range": train_range,
                "val_date_range": val_range,
                "baseline_score": 0.0,
                "variant_score": v.avg_val_score,
                "variant_llm_score": vs_llm,
                "calmar": v.calmar,
                "profit_factor": v.profit_factor,
                "win_rate": v.win_rate,
                "n_trades": v.n_trades,
                "cost_adjusted_pnl": 0.0,
                "promoted": is_final_win,
                "signal_params_json": json.dumps(signal_params_dict),
                "phase1_winner": is_phase1_win,
                "phase2_winner": is_phase2_win,
                "signal_llm_divergence": is_divergent,
                "notes": "signal/LLM divergence" if is_divergent else "",
            },
            dry_run=dry_run,
        )

    return final_winner, diagnostics


# ═══════════════════════════════════════════════════════════════════════════════
# CLI entry point (for standalone testing)
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    """Standalone CLI for running two-phase validation."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Two-Phase Validation: Signal sweep → LLM validation gate"
    )
    parser.add_argument(
        "--trader", type=str, default="kairos",
        help="Trader short name (default: kairos)",
    )
    parser.add_argument(
        "--dates", type=int, default=20,
        help="Number of trading days (default: 20)",
    )
    parser.add_argument(
        "--train", type=int, default=7,
        help="Training days per window (default: 7)",
    )
    parser.add_argument(
        "--val", type=int, default=3,
        help="Validation days per window (default: 3)",
    )
    parser.add_argument(
        "--phase1-variants", type=int, default=5,
        help="Variants in Phase 1 (default: 5)",
    )
    parser.add_argument(
        "--phase2-top-k", type=int, default=3,
        help="Top K for Phase 2 LLM validation (default: 3)",
    )
    parser.add_argument(
        "--phase2-budget", type=int, default=9,
        help="Max LLM runs per trader (default: 9)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate Phase 2 without actual LLM calls",
    )
    parser.add_argument(
        "--phase1-only", action="store_true",
        help="Run Phase 1 only, skip LLM validation",
    )

    args = parser.parse_args()

    config = ValidationConfig(
        phase1_variants=args.phase1_variants,
        phase2_top_k=args.phase2_top_k,
        max_llm_runs_per_trader=args.phase2_budget,
    )

    dates = get_trading_days(args.dates)

    if args.phase1_only:
        print("[sweep_validation] Phase 1 only mode")
        variants, baseline, winner, windows = run_phase1_signal_sweep(
            trader=args.trader,
            dates=dates,
            train_days=args.train,
            val_days=args.val,
            n_variants=config.phase1_variants,
        )
        print(f"\nPhase 1 winner: {winner.variant_name if winner else 'NONE'}")
        print("\nLeaderboard:")
        print(f"{'Rank':<5} {'Variant':<25} {'AvgVal':<10} {'WinRate':<10}")
        print(f"{'-'*5} {'-'*25} {'-'*10} {'-'*10}")
        for i, v in enumerate(variants, 1):
            flag = " ★" if winner and v.variant_name == winner.variant_name else ""
            print(
                f"  {i:<3} {v.variant_name:<25} {v.avg_val_score:<10.4f} "
                f"{v.win_rate:<10.1%}{flag}"
            )
        return

    winner, diagnostics = two_phase_validate(
        trader=args.trader,
        dates=dates,
        train_days=args.train,
        val_days=args.val,
        config=config,
        dry_run=args.dry_run,
    )

    print(f"\n{'='*60}")
    print("Two-Phase Validation Complete")
    print(f"{'='*60}")
    print(f"Trader: {args.trader}")
    print(f"Winner: {winner.variant_name if winner else 'NONE'}")
    print(f"Divergence: {diagnostics['signal_llm_divergence']}")
    print(f"Phase 1 winner: {diagnostics['phase1_winner']}")
    print(f"Phase 2 winner: {diagnostics['phase2_winner']}")
    print(f"Baseline LLM score: {diagnostics.get('baseline_llm_score', 'N/A')}")
    print()


if __name__ == "__main__":
    main()
