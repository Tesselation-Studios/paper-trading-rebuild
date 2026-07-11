"""Simulation Engine — overnight sweep runner.

Runs prompt × parameter × regime scenarios on historical market data.
Calls OpenRouter directly (no OpenClaw agent dispatch).
Fast, parallelizable, writes results to Postgres.

Usage:
    python3 -m src.simulator sweep --trader kairos     # nightly
    python3 -m src.simulator sweep --all                # all traders
    python3 -m src.simulator deep --trader kairos       # top candidates, 30-day
    python3 -m src.simulator weekend                    # 90-day sweep
    python3 -m src.simulator analyze --trader kairos    # generate hypotheses
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.llm_engine import LLMEngine
from src.agent_files import AgentFiles
from src.metrics import objective_score
from src.prompt_builder import PromptBuilder
from src.reflection import Reflection, reflect_on_decision, format_reflections_for_prompt
from src.replay import (
    ReplayHarness,
    ReplayResult,
    Tick,
    Portfolio,
    TraderDecision,
    make_deterministic_uptrend_ticks,
    make_uptrend_ticks,
)
from src.signals import SignalEngine, SignalParams
from src.journal_analyzer import JournalAnalyzer, JournalInsight, analyze_journal
from src.synthesis import Synthesizer, NightlySummary, synthesize_nightly

log = logging.getLogger("simulator")

# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class VariantConfig:
    """One prompt variant to test."""
    id: str
    trader: str
    description: str
    agents_md_diff: str = ""       # diff against base AGENTS.md
    soul_diff: str = ""             # diff against base SOUL.md
    skills_add: List[str] = field(default_factory=list)
    skills_remove: List[str] = field(default_factory=list)
    param_overrides: Dict[str, float] = field(default_factory=dict)


@dataclass
class SweepConfig:
    """Configuration for a sweep run."""
    trader: str
    variants: List[VariantConfig]
    param_grid: Dict[str, List[float]]  # param_name → [values to test]
    model: str = "deepseek/deepseek-v4-flash"
    data_days: int = 5
    data_window_end: Optional[datetime] = None  # defaults to yesterday
    temperature: float = 0.3


@dataclass
class ScenarioResult:
    """Result of one scenario (one variant × one param config)."""
    trader: str
    variant_id: str
    params: Dict[str, float]
    replay_result: ReplayResult
    objective_score: float
    journal: List[str]
    reflections: List[Reflection] = field(default_factory=list)
    elapsed_s: float = 0.0
    model_used: str = ""


@dataclass
class SweepReport:
    """Complete sweep run results."""
    run_id: str
    trader: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    total_scenarios: int = 0
    completed: int = 0
    failed: int = 0
    best_score: float = -999
    best_variant_id: str = ""
    best_params: Dict[str, float] = field(default_factory=dict)
    results: List[ScenarioResult] = field(default_factory=list)


# ── Simulation Runner ─────────────────────────────────────────────────────────


class SimulationRunner:
    """Runs a sweep: variants × params on historical data.

    Args:
        builder: PromptBuilder for loading agent files.
        engine: LLMEngine for OpenRouter calls.
        harness: ReplayHarness for tick-by-tick simulation.
        signal_engine: SignalEngine for computing indicators.
        market_data: List of Tick objects (historical data).
    """

    def __init__(
        self,
        builder: PromptBuilder,
        engine: LLMEngine,
        harness: ReplayHarness,
        signal_engine: SignalEngine,
        market_data: List[Tick],
        reflection_engine: Optional[LLMEngine] = None,
    ):
        self.builder = builder
        self.engine = engine
        self.harness = harness
        self.signal_engine = signal_engine
        self.market_data = market_data
        # Use cheapest model for reflection to avoid doubling simulation cost
        self.reflection_engine = reflection_engine or LLMEngine(
            model="deepseek/deepseek-v4-flash",
        )

    def run_scenario(
        self,
        variant: VariantConfig,
        params_override: Dict[str, float],
        agent_files: AgentFiles,
    ) -> ScenarioResult:
        """Run one scenario: one variant × one param config on the full dataset.

        Args:
            variant: The prompt variant to test.
            params_override: Signal engine parameter overrides.
            agent_files: Base agent files (variant diff applied on top).

        Returns:
            ScenarioResult with score, trades, journal.
        """
        t0 = time.monotonic()

        # ── Reset signal engine state to prevent cross-scenario leaks ──
        # Price history and params accumulate across scenarios if not reset.
        self.signal_engine._price_history = {}
        # Snapshot params so we restore after this scenario
        saved_params = SignalParams(**{
            f.name: getattr(self.signal_engine.params, f.name)
            for f in fields(self.signal_engine.params)
            if f.name != "_BOUNDS"
        })

        # Apply variant diff to agent files
        af = AgentFiles(
            identity=agent_files.identity,
            agents_md=(agent_files.agents_md + "\n" + variant.agents_md_diff).strip(),
            soul=(agent_files.soul + "\n" + variant.soul_diff).strip(),
            tools=agent_files.tools,
            memory=agent_files.memory,
            skills=[s for s in agent_files.skills if s.split(":")[0] not in variant.skills_remove]
                   + variant.skills_add,
        )

        # Apply parameter overrides to signal engine
        for name, value in (params_override or {}).items():
            try:
                self.signal_engine.params.set(name, value)
            except Exception:
                pass

        # Run tick-by-tick simulation
        journal: List[str] = []
        reflections: List[Reflection] = []
        tickers_seen: List[str] = []
        prev_equity = self.harness.initial_balance

        self.harness._reset()
        equity_curve: List[float] = []
        returns: List[float] = []

        # ── Pre-warm: feed 30 ticks to establish price history ──────
        # SignalEngine needs 20+ ticks before indicators produce meaningful
        # values. Without pre-warming, the first ~20 ticks of every scenario
        # are dead air (momentum=0, RSI=50, composite≈0) — everything looks
        # like HOLD and no trades fire.
        pre_warm_count = min(30, len(self.market_data) - 1)
        for tick in self.market_data[:pre_warm_count]:
            self.signal_engine.process(tick)
        log.debug("Pre-warmed signal engine with %d ticks", pre_warm_count)

        for tick in self.market_data[pre_warm_count:]:
            # Update position prices
            for pos in self.harness._portfolio.positions.values():
                if pos.ticker == tick.ticker:
                    pos.current_price = tick.close

            if tick.ticker not in tickers_seen:
                tickers_seen.append(tick.ticker)

            # Compute signal
            signal = self.signal_engine.process(tick)

            # Format reflection context from previous ticks
            reflection_context = format_reflections_for_prompt(reflections, max_count=3)

            # Ask LLM for decision
            try:
                decision = self.engine.decide(
                    tick, signal, journal, self.harness._portfolio, af,
                    reflection_context=reflection_context,
                )
            except Exception as e:
                log.warning("LLM error at %s: %s", tick.timestamp, e)
                decision = TraderDecision(
                    ticker=tick.ticker, decision="HOLD", conviction=0.0,
                    rationale=f"ERROR: {e}",
                )

            # Reflect: ask 'what did I learn from this decision?'
            try:
                prev_refs = reflections[-3:] if reflections else []
                reflection = reflect_on_decision(
                    tick, decision, signal, self.reflection_engine, prev_refs,
                )
                reflections.append(reflection)
            except Exception as e:
                log.warning("Reflection failed at %s: %s — continuing", tick.timestamp, e)

            # Execute decision
            if decision.decision != "HOLD":
                self.harness._decision_count += 1
                self.harness._execute(tick, decision)

            # Record equity
            equity = self.harness._portfolio.total_equity
            equity_curve.append(equity)
            if prev_equity > 0:
                returns.append((equity - prev_equity) / prev_equity)
            else:
                returns.append(0.0)
            prev_equity = equity

            # Journal
            entry = (
                f"[{tick.timestamp.strftime('%H:%M')}] "
                f"{decision.decision} {tick.ticker} @ ${tick.close:.2f}: "
                f"{decision.rationale}"
            )
            journal.append(entry)

        # Build result
        replay_result = self.harness._build_result(len(self.market_data))

        # Score
        # Use net trade PnL if cost model was applied, fall back to gross
        trade_pnls = [getattr(t, "pnl_net", t.pnl) for t in replay_result.trades]
        score = objective_score(
            returns=np.array(returns, dtype=np.float64),
            equity=np.array(equity_curve, dtype=np.float64),
            trades=trade_pnls,
        )

        # Restore params so next scenario starts clean
        self.signal_engine.params = saved_params

        return ScenarioResult(
            trader=variant.trader,
            variant_id=variant.id,
            params=params_override or {},
            replay_result=replay_result,
            objective_score=score,
            journal=journal,
            reflections=reflections,
            elapsed_s=time.monotonic() - t0,
            model_used=self.engine.model,
        )

    def run_sweep(self, config: SweepConfig) -> SweepReport:
        """Run a full sweep: all variants × all param configs.

        After the sweep, if no trades were generated, auto-relaxes thresholds
        and re-runs (up to 3 iterations). This prevents the dead-air problem
        where conservative defaults produce 0 trades across all scenarios.

        Args:
            config: Sweep configuration (variants, params, data config).

        Returns:
            SweepReport with ranked results.
        """
        report = SweepReport(
            run_id=datetime.now().strftime("%Y%m%d-%H%M%S"),
            trader=config.trader,
            started_at=datetime.now(),
        )

        # Load base agent files once
        agent_files = self.builder.load_agent_files()
        log.info("Loaded agent files for %s", config.trader)

        # ── Auto-relax loop ──────────────────────────────────────────
        # If all scenarios produce 0 trades, thresholds are too conservative.
        # Relax and re-run up to 3 times before giving up.
        MAX_RELAX_ITERATIONS = 3
        original_params = SignalParams(**{
            f.name: getattr(self.signal_engine.params, f.name)
            for f in fields(self.signal_engine.params)
            if f.name != "_BOUNDS"
        })

        for relax_iter in range(MAX_RELAX_ITERATIONS + 1):
            # Build param configurations from grid
            param_keys = list(config.param_grid.keys())
            param_values = list(config.param_grid.values())
            param_configs = _grid_combinations(param_keys, param_values)

            total = len(config.variants) * len(param_configs)
            report.total_scenarios += total
            log.info("Starting sweep [relax=%d]: %d variants × %d param configs = %d scenarios",
                     relax_iter, len(config.variants), len(param_configs), total)

            iter_results: List[ScenarioResult] = []
            for i, variant in enumerate(config.variants):
                for j, params in enumerate(param_configs):
                    try:
                        result = self.run_scenario(variant, params, agent_files)
                        iter_results.append(result)
                        report.results.append(result)
                        report.completed += 1

                        if result.objective_score > report.best_score:
                            report.best_score = result.objective_score
                            report.best_variant_id = variant.id
                            report.best_params = params

                        log.info(
                            "[%d/%d] %s variant=%s score=%.3f pnl=$%.0f trades=%d %.1fs",
                            report.completed, report.total_scenarios,
                            config.trader, variant.id,
                            result.objective_score,
                            result.replay_result.total_pnl,
                            len(result.replay_result.trades),
                            result.elapsed_s,
                        )
                    except Exception as e:
                        log.error("Scenario %s/%s failed: %s", variant.id, params, e)
                        report.failed += 1

            # ── Check if we need to relax ───────────────────────────
            max_trades = max(
                (len(r.replay_result.trades) for r in iter_results),
                default=0,
            )

            if max_trades > 3:
                log.info("Sweep produced %d trades — thresholds working, no relaxation needed",
                         max_trades)
                break  # Good: trades are flowing

            if relax_iter >= MAX_RELAX_ITERATIONS - 1:
                log.warning("After %d relaxation iterations, max trades = %d — giving up",
                           relax_iter + 1, max_trades)
                break  # Don't relax on final iteration

            # Relax thresholds by 20% of range toward more permissive
            relax_factor = 0.2 if max_trades == 0 else 0.10
            self.signal_engine.params = self.signal_engine.params.relax_thresholds(relax_factor)
            log.info(
                "Auto-relax #%d (max_trades=%d, factor=%.0f%%): momentum_threshold %.3f -> %.3f, "
                "rsi_oversold %.0f -> %.0f, rsi_overbought %.0f -> %.0f",
                relax_iter + 1, max_trades, relax_factor * 100,
                original_params.momentum_threshold, self.signal_engine.params.momentum_threshold,
                original_params.rsi_oversold, self.signal_engine.params.rsi_oversold,
                original_params.rsi_overbought, self.signal_engine.params.rsi_overbought,
            )

            # Re-reset harness for next iteration
            self.harness._reset()
            # Re-pre-warm signal engine with current (relaxed) params
            pre_warm_count = min(30, len(self.market_data) - 1)
            for tick in self.market_data[:pre_warm_count]:
                self.signal_engine.process(tick)

        # Restore original params so caller gets clean state
        self.signal_engine.params = original_params

        report.finished_at = datetime.now()

        # Sort by score descending
        report.results.sort(key=lambda r: r.objective_score, reverse=True)

        return report

    def analyze_sweep(self, report: SweepReport) -> List[JournalInsight]:
        """Run journal analysis on all scenarios in a sweep report.

        Extracts trades from ReplayResult, combines with reflections and journal,
        and produces ranked insights.

        Args:
            report: Completed SweepReport.

        Returns:
            List of JournalInsight sorted by confidence descending.
        """
        analyzer = JournalAnalyzer()

        all_trades: List[Dict[str, Any]] = []
        all_reflections: List[Reflection] = []
        all_journal: List[str] = []

        for result in report.results:
            # Extract trades from replay result
            for trade in result.replay_result.trades:
                all_trades.append({
                    "ticker": trade.ticker,
                    "pnl": trade.pnl,
                    "regime": "TRENDING_UP" if trade.pnl > 0 else "TRENDING_DOWN",
                    "conviction": 0.5,
                    "shares": trade.shares,
                    "position_pct": abs(trade.shares * trade.entry_price / 100_000.0)
                    if trade.entry_price > 0 else 0,
                })
            all_reflections.extend(result.reflections)
            all_journal.extend(result.journal)

        insights = analyzer.analyze(
            journal=all_journal,
            reflections=all_reflections,
            trades=all_trades,
        )

        log.info("Journal analysis: %d insights from %d scenarios (%d trades, %d reflections)",
                 len(insights), len(report.results), len(all_trades), len(all_reflections))

        return insights


def _scenarios_to_trader_dict(scenarios: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Convert a flat scenarios dict to per-trader format for synthesis."""
    result: Dict[str, Dict[str, Any]] = {}
    trader = scenarios.get("trader", "kairos")
    result[trader] = {
        "n_scenarios": scenarios.get("n_scenarios", 0),
        "n_trades": scenarios.get("n_trades", 0),
        "best_score": scenarios.get("best_score", 0.0),
        "top_variant": scenarios.get("top_variant", ""),
    }
    return result


def run_nightly_synthesis(
    trader_insights: Dict[str, List[JournalInsight]],
    scenarios: Dict[str, Dict[str, Any]],
    date: Optional[datetime] = None,
) -> NightlySummary:
    """Run nightly synthesis across all traders' insights.

    Wraps synthesize_nightly with logging and produces formatted output.

    Args:
        trader_insights: Dict mapping trader_name → list of JournalInsight.
        scenarios: Dict mapping trader_name → scenario summary dict.
        date: Optional date for the summary.

    Returns:
        NightlySummary ready for markdown formatting.
    """
    summary = synthesize_nightly(
        trader_insights=trader_insights,
        scenarios=scenarios,
        date=date,
    )

    # Log summary
    log.info(
        "Nightly synthesis: %d traders, %d insights, %d auto-promoted, %d PR-ready, %d needs validation",
        summary.n_traders,
        len(summary.top_insights),
        summary.n_auto_promoted,
        summary.n_pr_ready,
        summary.n_validation,
    )

    for promo in summary.promotions:
        if promo["action"] == "AUTO_PROMOTE":
            log.info(
                "AUTO-PROMOTED: %s — %s",
                promo.get("trader", "?"),
                promo["insight"]["description"],
            )

    return summary


# ── Helpers ───────────────────────────────────────────────────────────────────


def _grid_combinations(
    keys: List[str],
    values: List[List[float]],
) -> List[Dict[str, float]]:
    """Cartesian product of parameter values."""
    if not keys:
        return [{}]
    if not values:
        return [{}]

    import itertools
    combos = []
    for combo in itertools.product(*values):
        combos.append(dict(zip(keys, combo)))
    return combos


def load_market_data(
    ticker: str = "AAPL",
    days: int = 5,
    end_date: Optional[datetime] = None,
) -> List[Tick]:
    """Load historical market data.

    For now: generates synthetic data. Production: reads from Postgres.

    Args:
        ticker: Stock symbol.
        days: Number of trading days to simulate.
        end_date: End date (defaults to yesterday).

    Returns:
        List of Tick objects in chronological order.
    """
    # Synthetic data — replace with Postgres query after Coder builds DB layer
    n_ticks = days * 80  # ~80 ticks per day (5-min bars over 6.5h)
    return make_uptrend_ticks(
        ticker=ticker,
        n=n_ticks,
        start_price=150.0,
        drift=0.25,
        noise=0.02,
        seed=42 + days,  # different seed per day count for variety
    )


def build_default_sweep_config(
    trader: str,
    data_days: int = 5,
) -> SweepConfig:
    """Build a sensible default sweep configuration for a trader."""
    variants = [
        VariantConfig(
            id="baseline",
            trader=trader,
            description="Current production prompt, no changes",
        ),
        VariantConfig(
            id="v1-aggressive",
            trader=trader,
            description="Aggressive sizing, higher conviction threshold",
            agents_md_diff="- Never exceed 20% of portfolio in one position\n+ Never exceed 25% of portfolio in one position\n- If uncertain (conviction < 0.3), default to HOLD\n+ If uncertain (conviction < 0.2), default to HOLD",
        ),
        VariantConfig(
            id="v2-defensive",
            trader=trader,
            description="Defensive posture, wider stops, smaller positions",
            agents_md_diff="- Never exceed 20% of portfolio in one position\n+ Never exceed 15% of portfolio in one position\n- If uncertain (conviction < 0.3), default to HOLD\n+ If uncertain (conviction < 0.4), default to HOLD",
        ),
        VariantConfig(
            id="v3-volume-check",
            trader=trader,
            description="Add explicit volume confirmation rule",
            agents_md_diff="+ - Volume must be > 1.5x 20-day average before buying\n+ - Skip any signal where volume is below average",
        ),
    ]

    param_grid = {
        "momentum_threshold": [0.15, 0.25, 0.35],  # was [0.50, 0.55, 0.60] — too conservative
        "base_size_pct": [0.08, 0.12, 0.15],
        "rsi_oversold": [30, 35, 40],
        "rsi_overbought": [60, 65, 70],
    }

    return SweepConfig(
        trader=trader,
        variants=variants,
        param_grid=param_grid,
        data_days=data_days,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Simulation Engine — overnight sweep runner")
    sub = parser.add_subparsers(dest="command")

    # sweep
    sweep_p = sub.add_parser("sweep", help="Run nightly sweep")
    sweep_p.add_argument("--trader", default="kairos")
    sweep_p.add_argument("--all", action="store_true")
    sweep_p.add_argument("--days", type=int, default=5)
    sweep_p.add_argument("--ticker", default="AAPL")
    sweep_p.add_argument("--model", default="deepseek/deepseek-v4-flash")
    sweep_p.add_argument("--json", action="store_true", help="Output JSON")

    # deep
    deep_p = sub.add_parser("deep", help="Deep validation (30-day)")
    deep_p.add_argument("--trader", default="kairos")
    deep_p.add_argument("--days", type=int, default=30)
    deep_p.add_argument("--model", default="deepseek/deepseek-v4-flash")
    deep_p.add_argument("--json", action="store_true")

    # weekend
    weekend_p = sub.add_parser("weekend", help="Weekend 90-day sweep")
    weekend_p.add_argument("--days", type=int, default=90)
    weekend_p.add_argument("--model", default="deepseek/deepseek-v4-pro")
    weekend_p.add_argument("--json", action="store_true")

    # analyze
    analyze_p = sub.add_parser("analyze", help="Analyze past results, generate hypotheses")
    analyze_p.add_argument("--trader", default="kairos")
    analyze_p.add_argument("--json", action="store_true")

    # test
    test_p = sub.add_parser("test", help="Quick test with synthetic data")
    test_p.add_argument("--trader", default="kairos")
    test_p.add_argument("--ticks", type=int, default=10)
    test_p.add_argument("--model", default="deepseek/deepseek-v4-flash")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    if args.command == "test":
        _cmd_test(args)
    elif args.command in ("sweep", "deep", "weekend"):
        _cmd_sweep(args)
    elif args.command == "analyze":
        _cmd_analyze(args)


def _cmd_test(args):
    """Quick test: 10 synthetic ticks through the LLM."""
    builder = PromptBuilder(trader=args.trader)
    engine = LLMEngine(model=args.model)
    harness = ReplayHarness(initial_balance=100_000)
    signal_engine = SignalEngine()

    ticks = make_deterministic_uptrend_ticks(
        ticker="AAPL", n=args.ticks, start_price=150.0, step_pct=0.005,
    )

    agent_files = builder.load_agent_files()
    journal: List[str] = []

    print(f"=== Test: {args.trader} | {args.ticks} ticks | {args.model} ===\n")

    for tick in ticks:
        signal = signal_engine.process(tick)
        decision = engine.decide(tick, signal, journal, harness._portfolio, agent_files)
        print(f"[{tick.timestamp}] {tick.ticker} ${tick.close:.2f} | "
              f"Signal: {signal.composite_signal:.2f} | "
              f"Decision: {decision.decision} (conv={decision.conviction:.2f})")
        print(f"  Rationale: {decision.rationale}")
        journal.append(
            f"[{tick.timestamp}] {decision.decision} {tick.ticker} "
            f"@ ${tick.close:.2f}: {decision.rationale}"
        )

    print(f"\n=== {len(ticks)} ticks complete ===")


def _cmd_sweep(args):
    """Run a sweep, followed by journal analysis and nightly synthesis."""
    traders = ["kairos", "aldridge", "stonks"] if getattr(args, 'all', False) else [args.trader]
    all_reports: List[SweepReport] = []
    all_insights: Dict[str, List[JournalInsight]] = {}
    all_scenarios: Dict[str, Dict[str, Any]] = {}

    for trader in traders:
        builder = PromptBuilder(trader=trader)
        engine = LLMEngine(model=args.model)
        harness = ReplayHarness(initial_balance=100_000)
        signal_engine = SignalEngine()
        market_data = load_market_data(ticker="AAPL", days=args.days)

        config = build_default_sweep_config(trader=trader, data_days=args.days)
        runner = SimulationRunner(builder, engine, harness, signal_engine, market_data)

        report = runner.run_sweep(config)
        all_reports.append(report)

        _print_report(report)

        # ── Journal analysis (Task 3) ───────────────────────────────
        total_trades = sum(len(r.replay_result.trades) for r in report.results)
        if total_trades > 0:
            insights = runner.analyze_sweep(report)
            all_insights[trader] = insights
            all_scenarios[trader] = {
                "trader": trader,
                "n_scenarios": report.total_scenarios,
                "n_trades": total_trades,
                "best_score": report.best_score,
                "top_variant": report.best_variant_id,
            }
            log.info("Trader %s: %d insights generated", trader, len(insights))
        else:
            log.info("Trader %s: no trades — skipping journal analysis", trader)
            all_insights[trader] = []
            all_scenarios[trader] = {
                "trader": trader,
                "n_scenarios": report.total_scenarios,
                "n_trades": 0,
                "best_score": 0.0,
                "top_variant": "",
            }

    # ── Nightly synthesis (Task 4) ─────────────────────────────────
    if all_insights:
        summary = run_nightly_synthesis(all_insights, all_scenarios)
        formatted = summary.format()
        print(formatted)

        # Write nightly summary to file
        _write_nightly_summary(formatted)

    if getattr(args, 'json', False):
        print(json.dumps([
            {
                "trader": r.trader,
                "scenarios": r.total_scenarios,
                "best_score": round(r.best_score, 4),
                "best_variant": r.best_variant_id,
                "best_params": r.best_params,
                "top5": [
                    {"variant": s.variant_id, "score": round(s.objective_score, 4),
                     "pnl": round(s.replay_result.total_pnl, 2),
                     "trades": len(s.replay_result.trades)}
                    for s in r.results[:5]
                ]
            }
            for r in all_reports
        ], indent=2))


def _cmd_analyze(args):
    """Analyze past results and generate hypotheses.

    Reads the most recent sweep results from Postgres, runs journal analysis
    and nightly synthesis, and outputs a markdown summary.
    """
    from src.db.connection import get_connection

    print(f"=== Learning Loop Analysis: {args.trader} ===\n")

    # Try to analyze from the most recent sweep in the DB
    trader_insights: Dict[str, List[JournalInsight]] = {}
    trader_scenarios: Dict[str, Dict[str, Any]] = {}

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Get most recent sweep for each trader
                traders_to_query = [args.trader] if args.trader != "all" else ["kairos", "aldridge", "stonks"]
                for trader in traders_to_query:
                    cur.execute(
                        """SELECT run_id, trader, total_scenarios, best_score, best_variant_id,
                                  best_params, started_at
                           FROM sweep_results
                           WHERE trader = %s
                           ORDER BY started_at DESC LIMIT 1""",
                        (trader,),
                    )
                    row = cur.fetchone()
                    if row:
                        trader_scenarios[trader] = {
                            "trader": trader,
                            "n_scenarios": row[2] or 0,
                            "n_trades": 0,  # Will be populated from trade data
                            "best_score": float(row[3] or 0),
                            "top_variant": row[4] or "",
                        }
                        print(f"  {trader}: {row[2]} scenarios, best score {row[3]:.4f} "
                              f"({row[4]})")
                    else:
                        print(f"  {trader}: no sweep data found")

    except Exception as e:
        print(f"  (DB not available: {e})")
        print(f"  Run with --trader kairos to run a test sweep + analysis instead")
        return

    if not trader_scenarios:
        print("\nNo sweep data found. Run 'python3 -m src.simulator sweep' first.")
        return

    # Synthesis — aggregate insights from journal analysis
    summary = synthesize_nightly(
        trader_insights=trader_insights,
        scenarios=trader_scenarios,
    )
    print(f"\n{summary.format()}")


def _write_nightly_summary(formatted: str, output_dir: Optional[str] = None) -> str:
    """Write the nightly summary to a markdown file.

    Args:
        formatted: Markdown-formatted summary string.
        output_dir: Optional output directory (default: .hermes/reports/).

    Returns:
        Path to the written file.
    """
    if output_dir is None:
        output_dir = str(Path(__file__).parent.parent / ".hermes" / "reports")
    os.makedirs(output_dir, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"nightly_summary_{date_str}.md"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w") as f:
        f.write(formatted)
        f.write(f"\n\n---\nGenerated at {datetime.now().isoformat()}\n")

    log.info("Nightly summary written to %s", filepath)
    return filepath


def _print_report(report: SweepReport):
    """Print a human-readable sweep report."""
    duration = (report.finished_at - report.started_at).total_seconds() if report.finished_at else 0
    print(f"\n{'='*60}")
    print(f"  SWEEP: {report.trader} | {report.run_id}")
    print(f"  Scenarios: {report.completed}/{report.total_scenarios} "
          f"({report.failed} failed) | {duration:.0f}s")
    print(f"  Best: {report.best_variant_id} (score={report.best_score:.4f})")
    if report.best_params:
        print(f"  Best params: {report.best_params}")
    print(f"  Top 5:")
    for r in report.results[:5]:
        pnl = r.replay_result.total_pnl
        trades = len(r.replay_result.trades)
        print(f"    {r.variant_id:20s} score={r.objective_score:.4f}  "
              f"pnl=${pnl:,.0f}  trades={trades}  {r.elapsed_s:.1f}s")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
