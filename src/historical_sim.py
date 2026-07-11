#!/usr/bin/env python3
"""
historical_sim — run historical simulations on cached bar data.

Usage:
    python3 -m src.historical_sim intraday --trader kairos --ticker AAPL
    python3 -m src.historical_sim intraday --trader kairos --ticker AAPL --interval 15 --days 5
    python3 -m src.historical_sim longterm --trader aldridge --ticker SPY --days 30
    python3 -m src.historical_sim longterm --trader aldridge --ticker SPY --days 365
    python3 -m src.historical_sim improve --trader kairos --ticker AAPL --days 5 --n-variants 4

Modes:
  intraday  — run sim on 15m/1h bars from cached Parquet data
  longterm  — run sim on daily bars from 3-year cache
  improve   — param/prompt optimization across multiple variants
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from src.bar_loader import BarLoader, DEFAULT_BARS_DIR, DEFAULT_DB_PATH
from src.llm_engine import LLMEngine
from src.agent_files import AgentFiles
from src.prompt_builder import PromptBuilder, DEFAULTS
from src.replay import (
    ReplayHarness,
    ReplayResult,
    Tick,
    Portfolio,
    TraderDecision,
)
from src.signals import SignalEngine, SignalParams
from src.metrics import objective_score

log = logging.getLogger("historical_sim")

# ═══════════════════════════════════════════════════════════════════════════════
#  Mode 1: Intraday — 15m / 1h bars
# ═══════════════════════════════════════════════════════════════════════════════


def run_intraday(
    trader: str = "kairos",
    ticker: str = "AAPL",
    interval_minutes: int = 15,
    days: int = 5,
    model: str = "deepseek/deepseek-v4-flash",
    initial_balance: float = 100_000.0,
    require_conviction: float = 0.0,
    json_output: bool = False,
) -> ReplayResult:
    """Run an intraday simulation on cached bar data.

    Args:
        trader: Trader persona (kairos, aldridge, stonks).
        ticker: Stock ticker symbol.
        interval_minutes: Bar interval (15 or 60 for intraday).
        days: Number of trading days to simulate.
        model: OpenRouter model string.
        initial_balance: Starting portfolio cash.
        require_conviction: Minimum conviction to execute.
        json_output: If True, print JSON result.

    Returns:
        ReplayResult with full equity curve, trades, and metrics.
    """
    # ── Load market data ───────────────────────────────────────────────
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_dt = datetime.now() - timedelta(days=days * 1.5)  # buffer for weekends
    start_date = start_dt.strftime("%Y-%m-%d")

    bl = BarLoader()
    ticks = bl.load_date_range(
        tickers=[ticker],
        start_date=start_date,
        end_date=end_date,
        interval_minutes=interval_minutes,
    )

    if not ticks:
        print(f"No data for {ticker} in [{start_date}, {end_date}]", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(ticks)} ticks ({interval_minutes}min bars, {days} trading days)")

    # ── Build components ───────────────────────────────────────────────
    builder = PromptBuilder(trader=trader, use_defaults=True)
    engine = LLMEngine(model=model)
    harness = ReplayHarness(
        initial_balance=initial_balance,
        require_conviction=require_conviction,
    )
    signal_engine = SignalEngine()

    agent_files = builder.load_agent_files()
    journal: List[str] = []
    tickers_seen: List[str] = []

    # ── Pre-warm signal engine ─────────────────────────────────────────
    pre_warm = min(30, len(ticks) - 1)
    for tick in ticks[:pre_warm]:
        signal_engine.process(tick)
    if pre_warm > 0:
        print(f"Pre-warmed signal engine with {pre_warm} ticks")

    # ── Simulate ───────────────────────────────────────────────────────
    t0 = time.monotonic()
    n_decisions = 0

    for tick in ticks[pre_warm:]:
        # Update position prices
        for pos in harness._portfolio.positions.values():
            if pos.ticker == tick.ticker:
                pos.current_price = tick.close

        if tick.ticker not in tickers_seen:
            tickers_seen.append(tick.ticker)

        # Compute signal
        signal = signal_engine.process(tick)

        # Ask LLM for decision
        try:
            decision = engine.decide(
                tick, signal, journal, harness._portfolio, agent_files,
            )
        except Exception as e:
            log.warning("LLM error at %s: %s", tick.timestamp, e)
            decision = TraderDecision(
                ticker=tick.ticker, decision="HOLD", conviction=0.0,
                rationale=f"ERROR: {e}",
            )

        # Execute
        if decision.decision != "HOLD":
            n_decisions += 1
            harness._decision_count += 1
            harness._execute(tick, decision)

        # Journal
        journal.append(
            f"[{tick.timestamp.strftime('%m/%d %H:%M')}] "
            f"{decision.decision} {tick.ticker} @ ${tick.close:.2f}: "
            f"{decision.rationale}"
        )

    # ── Build results ──────────────────────────────────────────────────
    elapsed = time.monotonic() - t0
    result = harness._build_result(len(ticks))

    # Score
    score = objective_score(
        returns=np.array(harness._returns, dtype=np.float64),
        equity=np.array(harness._equity, dtype=np.float64),
        trades=[getattr(t, "pnl_net", t.pnl) for t in result.trades],
    )

    _print_result(
        trader=trader, ticker=ticker, interval=f"{interval_minutes}min",
        mode="intraday", days=days, result=result, score=score,
        n_decisions=n_decisions, elapsed=elapsed, json_output=json_output,
    )

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Mode 2: Longterm — daily bars
# ═══════════════════════════════════════════════════════════════════════════════


def run_longterm(
    trader: str = "kairos",
    ticker: str = "SPY",
    days: int = 30,
    model: str = "deepseek/deepseek-v4-flash",
    initial_balance: float = 100_000.0,
    require_conviction: float = 0.0,
    json_output: bool = False,
) -> ReplayResult:
    """Run a long-term simulation on daily bars.

    If daily bars aren't cached in Parquet, we resample from intraday data.

    Args:
        trader: Trader persona.
        ticker: Stock ticker symbol.
        days: Number of calendar days to simulate.
        model: OpenRouter model.
        initial_balance: Starting cash.
        require_conviction: Minimum conviction.
        json_output: Print JSON results.

    Returns:
        ReplayResult.
    """
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_dt = datetime.now() - timedelta(days=days * 1.5)
    start_date = start_dt.strftime("%Y-%m-%d")

    bl = BarLoader()
    # Load 5-min bars, then aggregate to daily in bar_loader
    ticks = bl.load_date_range(
        tickers=[ticker],
        start_date=start_date,
        end_date=end_date,
        interval_minutes=390,  # 6.5h = ~daily bar
    )

    if not ticks:
        print(f"No data for {ticker} in [{start_date}, {end_date}]", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(ticks)} daily bars ({days} calendar days)")

    # Same sim loop as intraday
    builder = PromptBuilder(trader=trader, use_defaults=True)
    engine = LLMEngine(model=model)
    harness = ReplayHarness(
        initial_balance=initial_balance,
        require_conviction=require_conviction,
    )
    signal_engine = SignalEngine()
    agent_files = builder.load_agent_files()
    journal: List[str] = []

    pre_warm = min(20, len(ticks) - 1)
    for tick in ticks[:pre_warm]:
        signal_engine.process(tick)

    t0 = time.monotonic()
    n_decisions = 0

    for tick in ticks[pre_warm:]:
        for pos in harness._portfolio.positions.values():
            if pos.ticker == tick.ticker:
                pos.current_price = tick.close

        signal = signal_engine.process(tick)

        try:
            decision = engine.decide(
                tick, signal, journal, harness._portfolio, agent_files,
            )
        except Exception:
            decision = TraderDecision(
                ticker=tick.ticker, decision="HOLD", conviction=0.0,
                rationale="LLM error",
            )

        if decision.decision != "HOLD":
            n_decisions += 1
            harness._decision_count += 1
            harness._execute(tick, decision)

        journal.append(
            f"[{tick.timestamp.strftime('%m/%d')}] "
            f"{decision.decision} {tick.ticker} @ ${tick.close:.2f}: "
            f"{decision.rationale}"
        )

    elapsed = time.monotonic() - t0
    result = harness._build_result(len(ticks))

    score = objective_score(
        returns=np.array(harness._returns, dtype=np.float64),
        equity=np.array(harness._equity, dtype=np.float64),
        trades=[getattr(t, "pnl_net", t.pnl) for t in result.trades],
    )

    _print_result(
        trader=trader, ticker=ticker, interval="daily",
        mode="longterm", days=days, result=result, score=score,
        n_decisions=n_decisions, elapsed=elapsed, json_output=json_output,
    )

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Mode 3: Improve — param optimization
# ═══════════════════════════════════════════════════════════════════════════════


def run_improve(
    trader: str = "kairos",
    ticker: str = "AAPL",
    days: int = 5,
    n_variants: int = 4,
    model: str = "deepseek/deepseek-v4-flash",
    json_output: bool = False,
):
    """Run parameter optimization across multiple param configurations.

    Tests combinations of momentum_threshold, rsi thresholds, and conviction
    settings to find the best configuration for the given trader × ticker.

    Args:
        trader: Trader persona.
        ticker: Stock ticker symbol.
        days: Trading days.
        n_variants: Number of param configs to test (sqrt of total combos).
        model: OpenRouter model.
        json_output: Print JSON results.

    Returns:
        Dict with best params, best score, and all results.
    """
    # ── Load data once ─────────────────────────────────────────────────
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_dt = datetime.now() - timedelta(days=days * 1.5)
    start_date = start_dt.strftime("%Y-%m-%d")

    bl = BarLoader()
    ticks = bl.load_date_range(
        tickers=[ticker],
        start_date=start_date,
        end_date=end_date,
        interval_minutes=30,
    )

    if not ticks:
        print(f"No data for {ticker}", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(ticks)} ticks for optimization run\n")

    # ── Build stable components ────────────────────────────────────────
    builder = PromptBuilder(trader=trader, use_defaults=True)
    engine = LLMEngine(model=model)
    agent_files = builder.load_agent_files()

    pre_warm = min(30, len(ticks) - 1)

    # ── Param grid ─────────────────────────────────────────────────────
    from itertools import product

    param_grid = {
        "momentum_threshold": [0.15, 0.30],
        "rsi_oversold": [30, 35],
        "rsi_overbought": [65, 70],
        "conviction_multiplier": [1.0, 1.5],
    }

    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combos = list(product(*values))

    # Limit to n_variants if too many
    if len(combos) > n_variants:
        import random
        combos = random.sample(combos, n_variants)

    results = []
    t0 = time.monotonic()

    for i, combo in enumerate(combos):
        params_dict = dict(zip(keys, combo))
        harness = ReplayHarness(initial_balance=100_000)
        signal_engine = SignalEngine(SignalParams(**params_dict))
        journal: List[str] = []
        scenario_t0 = time.monotonic()

        # Pre-warm
        for tick in ticks[:pre_warm]:
            signal_engine.process(tick)

        n_dec = 0
        for tick in ticks[pre_warm:]:
            for pos in harness._portfolio.positions.values():
                if pos.ticker == tick.ticker:
                    pos.current_price = tick.close

            signal = signal_engine.process(tick)

            try:
                decision = engine.decide(
                    tick, signal, journal, harness._portfolio, agent_files,
                )
            except Exception:
                decision = TraderDecision(
                    ticker=tick.ticker, decision="HOLD", conviction=0.0,
                    rationale="LLM error",
                )

            if decision.decision != "HOLD":
                n_dec += 1
                harness._decision_count += 1
                harness._execute(tick, decision)

            journal.append(
                f"[{tick.timestamp.strftime('%m/%d %H:%M')}] "
                f"{decision.decision} {tick.ticker} @ ${tick.close:.2f}: "
                f"{decision.rationale}"
            )

        result = harness._build_result(len(ticks))
        score = objective_score(
            returns=np.array(harness._returns, dtype=np.float64),
            equity=np.array(harness._equity, dtype=np.float64),
            trades=[getattr(t, "pnl_net", t.pnl) for t in result.trades],
        )

        scenario_elapsed = time.monotonic() - scenario_t0

        results.append({
            "params": params_dict,
            "score": score,
            "pnl": result.total_pnl,
            "trades": len(result.trades),
            "decisions": n_dec,
            "elapsed_s": round(scenario_elapsed, 1),
        })

        print(
            f"  [{i + 1}/{len(combos)}] "
            f"params={params_dict} "
            f"score={score:.4f} pnl=${result.total_pnl:.2f} "
            f"trades={len(result.trades)} ({scenario_elapsed:.1f}s)"
        )

    total_elapsed = time.monotonic() - t0

    # ── Rank ───────────────────────────────────────────────────────────
    results.sort(key=lambda r: r["score"], reverse=True)
    best = results[0]

    print(f"\n{'=' * 60}")
    print(f"  OPTIMIZATION RESULTS — {trader} @ {ticker}")
    print(f"  Configs tested: {len(combos)} | Total time: {total_elapsed:.0f}s")
    print(f"")
    print(f"  🏆 Best config: {best['params']}")
    print(f"     Score: {best['score']:.4f} | P&L: ${best['pnl']:.2f} | "
          f"Trades: {best['trades']}")
    print(f"")
    print(f"  Top 5:")
    for r in results[:5]:
        print(f"    {str(r['params']):40s} score={r['score']:.4f}  "
              f"pnl=${r['pnl']:,.0f}  trades={r['trades']}")
    print(f"{'=' * 60}")

    if json_output:
        print(json.dumps({
            "trader": trader,
            "ticker": ticker,
            "n_configs": len(combos),
            "best_params": best["params"],
            "best_score": round(best["score"], 4),
            "best_pnl": round(best["pnl"], 2),
            "best_trades": best["trades"],
            "all": results[:10],
        }, indent=2))

    return best["params"]


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _print_result(
    trader: str,
    ticker: str,
    interval: str,
    mode: str,
    days: int,
    result: ReplayResult,
    score: float,
    n_decisions: int,
    elapsed: float,
    json_output: bool = False,
):
    """Print or emit JSON result summary."""
    if json_output:
        print(json.dumps({
            "mode": mode,
            "trader": trader,
            "ticker": ticker,
            "interval": interval,
            "days": days,
            "ticks": result.n_ticks,
            "decisions": n_decisions,
            "trades": len(result.trades),
            "score": round(score, 4),
            "pnl": round(result.total_pnl, 2),
            "gross_pnl": round(result.gross_pnl, 2),
            "total_cost": round(result.total_cost, 2),
            "final_equity": round(result.final_equity, 2),
            "return_pct": round(result.total_return_pct, 2),
            "win_rate": round(result.win_rate, 4),
            "elapsed_s": round(elapsed, 1),
            "initial_balance": result.initial_balance,
        }, indent=2))
        return

    print(f"\n{'=' * 60}")
    print(f"  📊 HISTORICAL SIM — {mode} | {trader} @ {ticker}")
    print(f"  {len(result.trades)} trades over {result.n_ticks} ticks "
          f"({interval}, {days} days)")
    print(f"  Elapsed: {elapsed:.1f}s")
    print(f"")

    # P&L
    pnl_sign = "+" if result.total_pnl >= 0 else ""
    print(f"  Initial equity: ${result.initial_balance:,.2f}")
    print(f"  Final equity:   ${result.final_equity:,.2f}")
    print(f"  P&L:            {pnl_sign}${result.total_pnl:,.2f}")
    if result.total_cost > 0:
        print(f"  Gross P&L:      ${result.gross_pnl:,.2f}")
        print(f"  Transaction costs: ${result.total_cost:,.2f}")
    print(f"  Return:         {pnl_sign}{result.total_return_pct:.2f}%")
    print(f"")

    # Trade stats
    print(f"  Trades:         {len(result.trades)} ({n_decisions} decisions)")
    print(f"  Win rate:       {result.win_rate:.1f}% "
          f"({len(result.positive_trades)}W / {len(result.negative_trades)}L)")
    if result.trades:
        avg_pnl = sum(t.pnl for t in result.trades) / len(result.trades)
        print(f"  Avg trade P&L:  ${avg_pnl:.2f}")
        print(f"  Best trade:     ${max(t.pnl for t in result.trades):.2f}")
        print(f"  Worst trade:    ${min(t.pnl for t in result.trades):.2f}")
    print(f"")

    # Score
    print(f"  Objective score: {score:.4f}")
    print(f"{'=' * 60}")


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="Historical Simulation — run sims on cached bar data",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # intraday
    intra = sub.add_parser("intraday", help="Run intraday sim (15m/1h bars)")
    intra.add_argument("--trader", default="kairos", choices=["kairos", "aldridge", "stonks"])
    intra.add_argument("--ticker", default="AAPL")
    intra.add_argument("--interval", type=int, default=15, help="Bar interval in minutes (15 or 60)")
    intra.add_argument("--days", type=int, default=5, help="Trading days")
    intra.add_argument("--model", default="deepseek/deepseek-v4-flash")
    intra.add_argument("--balance", type=float, default=100_000.0)
    intra.add_argument("--conviction", type=float, default=0.0)
    intra.add_argument("--json", action="store_true", help="JSON output")

    # longterm
    lt = sub.add_parser("longterm", help="Run longterm sim (daily bars)")
    lt.add_argument("--trader", default="kairos", choices=["kairos", "aldridge", "stonks"])
    lt.add_argument("--ticker", default="SPY")
    lt.add_argument("--days", type=int, default=30)
    lt.add_argument("--model", default="deepseek/deepseek-v4-flash")
    lt.add_argument("--balance", type=float, default=100_000.0)
    lt.add_argument("--conviction", type=float, default=0.0)
    lt.add_argument("--json", action="store_true")

    # improve
    imp = sub.add_parser("improve", help="Parameter optimization mode")
    imp.add_argument("--trader", default="kairos", choices=["kairos", "aldridge", "stonks"])
    imp.add_argument("--ticker", default="AAPL")
    imp.add_argument("--days", type=int, default=5)
    imp.add_argument("--n-variants", type=int, default=8, help="Number of param combos to test")
    imp.add_argument("--model", default="deepseek/deepseek-v4-flash")
    imp.add_argument("--json", action="store_true")

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.WARNING,  # Quiet by default
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    if args.mode == "intraday":
        run_intraday(
            trader=args.trader,
            ticker=args.ticker,
            interval_minutes=args.interval,
            days=args.days,
            model=args.model,
            initial_balance=args.balance,
            require_conviction=args.conviction,
            json_output=args.json,
        )
    elif args.mode == "longterm":
        run_longterm(
            trader=args.trader,
            ticker=args.ticker,
            days=args.days,
            model=args.model,
            initial_balance=args.balance,
            require_conviction=args.conviction,
            json_output=args.json,
        )
    elif args.mode == "improve":
        run_improve(
            trader=args.trader,
            ticker=args.ticker,
            days=args.days,
            n_variants=args.n_variants,
            model=args.model,
            json_output=args.json,
        )


if __name__ == "__main__":
    main()
