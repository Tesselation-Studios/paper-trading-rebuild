"""Overnight optimization harness — main orchestrator.

Runs the full overnight pipeline:
  1. Load historical bars
  2. Run discovery phase to find ground truth signals
  3. Generate config variants
  4. Replay each variant against historical data
  5. Score each variant against discovered signals
  6. Build leaderboard and write YAML report

Usage:
    from src.overnight_harness import OvernightHarness

    harness = OvernightHarness(data_dir="shared/cache/bars")
    report = harness.run(n_variants=50)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from typing import TYPE_CHECKING

from src.replay import Tick, make_dummy_tick, make_deterministic_uptrend_ticks
from src.overnight_discovery import (
    SignalDiscoverer,
    DiscoveryConfig,
    DiscoveredSignal,
)
from src.overnight_replay import (
    ReplayVariantEngine,
    ConfigVariant,
    VariantResult,
    generate_variants,
    load_variant_from_dict,
)
from src.overnight_scorer import VariantScorer, ScoredVariant, split_ticks_by_midpoint
from src.overnight_leaderboard import LeaderboardBuilder, LeaderboardReport

if TYPE_CHECKING:
    from src.counterfactual import CounterfactualResult

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_N_VARIANTS = 50
DEFAULT_INITIAL_BALANCE = 100_000.0


@dataclass
class OvernightResult:
    """Complete output of one overnight optimization run."""

    report: LeaderboardReport
    scored_variants: List[ScoredVariant]
    discovered_signals: List[DiscoveredSignal]
    n_variants_tested: int
    duration_seconds: float
    errors: List[str] = field(default_factory=list)
    counterfactual_results: List[Any] = field(default_factory=list)


class OvernightHarness:
    """Full overnight optimization pipeline.

    Orchestrates the discovery, replay, scoring, and leaderboard phases.
    Optionally runs counterfactual analysis when --counterfactual is enabled.

    Args:
        initial_balance: Starting cash for replay.
        data_dir: Path to bar data files (parquet or SQLite).
        n_variants: Number of config variants to test.
        discovery_config: Optional config for the discovery phase.
        seed: Random seed for variant generation.
        counterfactual: Whether to run counterfactual analysis phase.
        counterfactual_config: Optional dict of counterfactual parameters.
    """

    def __init__(
        self,
        initial_balance: float = DEFAULT_INITIAL_BALANCE,
        data_dir: Optional[str] = None,
        n_variants: int = DEFAULT_N_VARIANTS,
        discovery_config: Optional[DiscoveryConfig] = None,
        seed: int = 42,
        counterfactual: bool = False,
        counterfactual_config: Optional[Dict[str, Any]] = None,
        enable_split_window: bool = True,
    ):
        self.initial_balance = initial_balance
        self.data_dir = Path(data_dir) if data_dir else None
        self.n_variants = n_variants
        self.discovery_config = discovery_config or DiscoveryConfig()
        # 2026-07-31: run each variant on first/second half of the period
        # (by date midpoint) in addition to the full window, so scoring can
        # require Sharpe positive in BOTH halves before calling a variant
        # "robust" -- see overnight_scorer.py. Default on since an
        # unvalidated leaderboard is the whole problem this fixes; the flag
        # exists for quick iteration that doesn't need the ~3x replay cost.
        self.enable_split_window = enable_split_window
        self.seed = seed
        self.counterfactual_enabled = counterfactual
        self.counterfactual_config = counterfactual_config or {}

        self._discoverer = SignalDiscoverer(config=self.discovery_config)
        self._replay_engine = ReplayVariantEngine(
            initial_balance=initial_balance,
        )
        self._scorer = VariantScorer()
        self._leaderboard = LeaderboardBuilder()

    def run(
        self,
        ticks: Optional[List[Tick]] = None,
        n_variants: Optional[int] = None,
    ) -> OvernightResult:
        """Run the full overnight optimization pipeline.

        When counterfactual is enabled, also runs counterfactual analysis
        phase after discovery + replay.

        Args:
            ticks: Historical tick data. If None, synthetic data is generated.
            n_variants: Override the number of variants for this run.

        Returns:
            OvernightResult with leaderboard, scored variants, errors,
            and optionally counterfactual_results.
        """
        t0 = time.time()
        errors: List[str] = []
        n_vars = n_variants or self.n_variants

        # ── Phase 0: Load/generate bar data ───────────────────────────
        log.info("Phase 0: Loading bar data...")
        if ticks is None:
            ticks = self._generate_synthetic_data()
            log.info("Generated %d synthetic ticks", len(ticks))
        else:
            log.info("Loaded %d ticks", len(ticks))

        # ── Phase 1: Discovery ────────────────────────────────────────
        log.info("Phase 1: Running discovery...")
        discovered_signals = self._run_discovery(ticks)
        log.info("Discovered %d signals", len(discovered_signals))

        # ── Phase 2: Generate variants ────────────────────────────────
        log.info("Phase 2: Generating %d variants...", n_vars)
        variants = self._generate_variants(n_vars)
        log.info("Generated %d variants", len(variants))

        # ── Phase 3: Replay + Score each variant ──────────────────────
        log.info("Phase 3: Running replay on %d variants...", len(variants))
        first_half: List[Tick] = []
        second_half: List[Tick] = []
        if self.enable_split_window:
            first_half, second_half = split_ticks_by_midpoint(ticks)
            log.info("Split-window enabled: %d ticks in first half, %d in second half",
                      len(first_half), len(second_half))
        results: List[VariantResult] = []
        for i, variant in enumerate(variants):
            try:
                result = self._replay_engine.run_variant(
                    variant, ticks, discovered_signals,
                )
                if self.enable_split_window and first_half and second_half:
                    # Same params, same trader logic, just a different
                    # tick slice -- reuses run_variant rather than
                    # duplicating the entry-gate/indicator logic. The
                    # catch/miss-signal comparison on each half is unused
                    # here (only .replay_result feeds Sharpe), harmless
                    # extra computation.
                    result.first_half_replay_result = self._replay_engine.run_variant(
                        variant, first_half, discovered_signals,
                    ).replay_result
                    result.second_half_replay_result = self._replay_engine.run_variant(
                        variant, second_half, discovered_signals,
                    ).replay_result
                results.append(result)
                if (i + 1) % 10 == 0:
                    log.info("  Completed %d/%d variants", i + 1, len(variants))
            except Exception as e:
                log.warning("Variant %s failed: %s", variant.variant_id, e)
                errors.append(f"{variant.variant_id}: {e}")

        if not results:
            return OvernightResult(
                report=LeaderboardReport(
                    session_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    duration_seconds=time.time() - t0,
                    variants_tested=0,
                    top_5=[],
                    current_live={},
                    improvement_potential="No variants completed successfully.",
                ),
                scored_variants=[],
                discovered_signals=discovered_signals,
                n_variants_tested=0,
                duration_seconds=time.time() - t0,
                errors=errors,
            )

        # ── Phase 4: Score ────────────────────────────────────────────
        log.info("Phase 4: Scoring %d variant results...", len(results))
        scored = self._scorer.score_all(results)
        log.info("Top score: %.4f (variant: %s)", scored[0].score, scored[0].variant.variant_id)

        # ── Phase 5: Leaderboard ──────────────────────────────────────
        log.info("Phase 5: Building leaderboard...")
        self._leaderboard.start_session()
        report = self._leaderboard.build(scored, duration_seconds=time.time() - t0)

        # ── Phase 6: Counterfactual (optional) ────────────────────────
        counterfactual_results: List[Any] = []
        if self.counterfactual_enabled:
            log.info("Phase 6: Running counterfactual analysis...")
            try:
                counterfactual_results = self._run_counterfactual(discovered_signals, ticks)
                log.info("Counterfactual: analyzed %d trades", len(counterfactual_results))
            except Exception as e:
                log.warning("Counterfactual phase failed: %s", e)
                errors.append(f"counterfactual: {e}")

        duration = time.time() - t0
        log.info("Overnight run complete: %d variants in %.1fs",
                 len(scored), duration)

        return OvernightResult(
            report=report,
            scored_variants=scored,
            discovered_signals=discovered_signals,
            n_variants_tested=len(results),
            duration_seconds=duration,
            errors=errors,
            counterfactual_results=counterfactual_results,
        )

    def run_with_logging(
        self,
        ticks: Optional[List[Tick]] = None,
        n_variants: Optional[int] = None,
        leaderboard_path: str = "leaderboard.yaml",
    ) -> OvernightResult:
        """Run the pipeline, log progress, and write the leaderboard.

        Args:
            ticks: Historical tick data.
            n_variants: Override the number of variants.
            leaderboard_path: Output path for leaderboard YAML.

        Returns:
            OvernightResult with all results.
        """
        result = self.run(ticks=ticks, n_variants=n_variants)

        # Write leaderboard
        if result.report.variants_tested > 0:
            self._leaderboard.write_to_file(result.report, path=leaderboard_path)
            log.info("Leaderboard written to %s", leaderboard_path)

        # Summary
        log.info("=" * 60)
        log.info("OVERNIGHT OPTIMIZATION SUMMARY")
        log.info("=" * 60)
        log.info("Variants tested: %d", result.n_variants_tested)
        log.info("Duration: %.1fs (%.2fh)", result.duration_seconds,
                 result.duration_seconds / 3600)
        if result.scored_variants:
            log.info("Top config: %s (score=%.4f)",
                     result.scored_variants[0].variant.variant_id,
                     result.scored_variants[0].score)
            # catch_rate/false_positive_rate are fractions of total discovered
            # signals (thousands of raw pattern matches), not of trades taken --
            # commonly < 0.005, which %.2f rounds to a display of "0.00",
            # indistinguishable from a true zero. 2026-08-11: this is exactly
            # what happened in the Aug 9/10 overnight cycle -- catch_rate=0.001
            # displayed as "0.00" and got written up as "zero caught," when the
            # stored value (and false_positive_rate=0.0) actually showed every
            # trade taken matched a discovered signal. %.3f%% (percentage,
            # 3 decimals) keeps small-but-real values visibly distinct from zero.
            log.info("Catch rate: %.3f%% | FP rate: %.3f%% | Return: %.2f%%",
                     result.scored_variants[0].catch_rate * 100,
                     result.scored_variants[0].false_positive_rate * 100,
                     result.scored_variants[0].total_return_pct)
        if result.errors:
            log.warning("Errors (%d):", len(result.errors))
            for err in result.errors[:5]:
                log.warning("  - %s", err)
        log.info("=" * 60)

        return result

    # ── Internal methods ──────────────────────────────────────────────

    def _run_discovery(self, ticks: List[Tick]) -> List[DiscoveredSignal]:
        """Run the discovery phase."""
        return self._discoverer.discover(ticks)

    def _generate_variants(self, n_variants: int) -> List[ConfigVariant]:
        """Generate config variants."""
        return generate_variants(n_variants=n_variants, seed=self.seed)

    def _run_counterfactual(
        self,
        discovered_signals: List[DiscoveredSignal],
        ticks: List[Tick],
    ) -> List[Any]:
        """Run counterfactual analysis on discovered signals.

        Treats each discovered signal as an "actual trade" and finds
        alternative stocks that would have been better buys.

        Args:
            discovered_signals: Signals from the discovery phase.
            ticks: Historical tick data (for extracting dates).

        Returns:
            List of CounterfactualResult objects.
        """
        from src.counterfactual import (
            UniverseSampler,
            CounterfactualReplay,
            run_counterfactual_batch,
        )

        # Extract trade-like dicts from discovered signals
        trades: List[Dict[str, Any]] = []
        seen: set = set()
        for sig in discovered_signals:
            # Deduplicate by symbol+date
            date_key = sig.timestamp.strftime("%Y-%m-%d")
            key = (sig.symbol, date_key)
            if key in seen:
                continue
            seen.add(key)
            trades.append({
                "symbol": sig.symbol,
                "date": date_key,
                "entry_price": sig.price_at_signal,
            })

        # Limit number of trades to avoid excessive runtime
        max_trades = self.counterfactual_config.get("max_trades", 10)
        if len(trades) > max_trades:
            trades = trades[:max_trades]

        n_alternatives = self.counterfactual_config.get(
            "n_alternatives", 50,
        )
        price_range_pct = self.counterfactual_config.get(
            "price_range_pct", 0.30,
        )

        return run_counterfactual_batch(
            trades=trades,
            n_alternatives=n_alternatives,
            price_range_pct=price_range_pct,
        )

    def _generate_synthetic_data(self) -> List[Tick]:
        """Generate synthetic tick data for testing when no real bars exist."""
        rng = np.random.default_rng(self.seed)
        all_ticks: List[Tick] = []

        symbols = ["SPY", "AAPL", "MSFT", "NVDA", "TSLA"]
        for symbol in symbols:
            ticks = make_deterministic_uptrend_ticks(
                ticker=symbol, n=100, start_price=100.0 + rng.uniform(-10, 10),
            )
            all_ticks.extend(ticks)

        # Sort by timestamp, then by ticker for determinism
        all_ticks.sort(key=lambda t: (t.timestamp, t.ticker))
        return all_ticks

    @staticmethod
    def get_defaults() -> Dict[str, Any]:
        """Get the default parameter values dictionary."""
        from src.overnight_replay import DEFAULT_PARAMS
        return dict(DEFAULT_PARAMS)


# ── Convenience function ─────────────────────────────────────────────────────


def run_overnight(
    n_variants: int = DEFAULT_N_VARIANTS,
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
    leaderboard_path: str = "leaderboard.yaml",
    ticks: Optional[List[Tick]] = None,
) -> OvernightResult:
    """One-line invocation of the overnight optimization harness.

    Args:
        n_variants: Number of config variants to test.
        initial_balance: Starting cash.
        leaderboard_path: Output path for YAML leaderboard.
        ticks: Optional historical tick data.

    Returns:
        OvernightResult with full results.
    """
    harness = OvernightHarness(
        initial_balance=initial_balance,
        n_variants=n_variants,
    )
    return harness.run_with_logging(
        ticks=ticks,
        leaderboard_path=leaderboard_path,
    )
