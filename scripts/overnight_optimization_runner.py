#!/usr/bin/env python3
"""
Overnight optimization runner — runs 10 iterations of OvernightHarness
with varying configurations, extracts results, saves YAML, and reports.
"""
import sys, os, json, yaml, time, tempfile, logging
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd
import pyarrow.parquet as pq

# ── Add project root ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.overnight_harness import OvernightHarness, DiscoveryConfig
from src.replay import Tick

log = logging.getLogger("overnight-optimization-runner")

RESULTS_DIR = Path(__file__).parent.parent / "shared" / "research" / "overnight-runs"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CACHE_DIR = Path(__file__).parent.parent / "shared" / "cache" / "bars"


def load_ticks(tickers: List[str], days: int = 20) -> List[Tick]:
    """Load real parquet bars as Tick objects.

    2026-07-31: this runner previously called run_with_logging() with no
    ticks at all, which silently falls back to OvernightHarness's
    synthetic-data generator (make_deterministic_uptrend_ticks -- a pure,
    noise-free per-symbol price ramp, not real market data). data_dir was
    set but never actually read anywhere in the harness. Every "12 runs,
    600 variants" sweep run this way was backtesting a synthetic walk, not
    the real cached bars sitting in shared/cache/bars/ (confirmed present:
    67 tickers). Ported from scripts/run_overnight.py's load_ticks(),
    which already does this correctly -- same function, not reinvented.
    """
    now = datetime.now()
    ticks = []
    for tkr in tickers:
        f = CACHE_DIR / f"{tkr}.parquet"
        if not f.exists():
            log.warning("No data for %s, skipping", tkr)
            continue
        try:
            table = pq.read_table(str(f))
            df = table.to_pandas()
            if df.empty:
                continue
            df = df.sort_values("timestamp")
            if days:
                cutoff = df["timestamp"].max() - pd.Timedelta(days=days)
                df = df[df["timestamp"] >= cutoff]
            for _, row in df.iterrows():
                ticks.append(Tick(
                    timestamp=row["timestamp"].to_pydatetime(),
                    ticker=tkr,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                ))
        except Exception as e:
            log.warning("Error loading %s: %s", tkr, e)
    log.info("Loaded %d ticks from %d tickers", len(ticks), len(tickers))
    return ticks

# ── Ticker universes ──
UNIVERSES = {
    "core": ["AAPL","MSFT","NVDA","TSLA","META","GOOGL","AMZN","SPY"],
    "stonks": ["NVDA","TSLA","COIN","PLTR","MSTR","GME","RIOT","MARA","HOOD","DJT"],
    "kairos": ["AMD","INTC","IBM","ORCL","CRM","ADBE","NFLX","DIS","BA","CAT"],
    "aldridge": ["JPM","GS","BAC","V","MA","PYPL","SQ","ARKK","XLF","QQQ"],
    "all": ["AAPL","MSFT","NVDA","TSLA","META","GOOGL","AMZN","SPY",
            "COIN","PLTR","MSTR","GME","RIOT","MARA","HOOD","DJT",
            "AMD","INTC","IBM","ORCL","CRM","ADBE","NFLX","DIS","BA","CAT",
            "JPM","GS","BAC","V","MA","PYPL","SQ","ARKK","XLF","QQQ"],
}

# ── 10 different configurations ──
ITERATIONS = [
    {
        "name": "core-default",
        "universe": "core",
        "desc": "Baseline: core tickers, default discovery params",
        "discovery": {},
        "counterfactual": True,
        "n_variants": 50,
    },
    {
        "name": "stonks-relaxed",
        "universe": "stonks",
        "desc": "Stonks universe, relaxed RSI (30/75), lower volume threshold",
        "discovery": {
            "rsi_oversold_threshold": 30.0,
            "rsi_overbought_short_threshold": 75.0,
            "volume_ratio_min": 1.2,
            "conviction_high_volume_bonus": 0.25,
        },
        "counterfactual": True,
        "n_variants": 50,
    },
    {
        "name": "all-tight",
        "universe": "all",
        "desc": "All tickers, tight RSI (38/65), high volume, high conviction",
        "discovery": {
            "rsi_oversold_threshold": 38.0,
            "rsi_overbought_short_threshold": 65.0,
            "volume_ratio_min": 2.0,
            "conviction_strong_threshold": 0.7,
            "conviction_moderate_threshold": 0.45,
        },
        "counterfactual": True,
        "n_variants": 50,
    },
    {
        "name": "kairos-macd",
        "universe": "kairos",
        "desc": "Kairos universe, MACD focus, longer MA, lower volume",
        "discovery": {
            "ma_period": 30,
            "ma_max_distance_pct": 3.0,
            "volume_ratio_min": 1.3,
            "momentum_lookback": 15,
            "momentum_threshold_bullish": 0.015,
            "conviction_ma_proximity_bonus": 0.2,
        },
        "counterfactual": True,
        "n_variants": 50,
    },
    {
        "name": "aldridge-aggressive",
        "universe": "aldridge",
        "desc": "Aldridge universe, aggressive entry, low conviction min",
        "discovery": {
            "rsi_oversold_threshold": 32.0,
            "rsi_overbought_short_threshold": 72.0,
            "conviction_strong_threshold": 0.55,
            "conviction_moderate_threshold": 0.3,
            "volume_ratio_min": 1.5,
            "conviction_rsi_bounce_bonus": 0.3,
        },
        "counterfactual": True,
        "n_variants": 50,
    },
    {
        "name": "stonks-catalyst",
        "universe": "stonks",
        "desc": "Stonks, catalyst-driven, high volume spike sensitivity",
        "discovery": {
            "catalyst_volume_threshold": 1.5,
            "catalyst_price_move": 0.02,
            "volume_spike_consecutive": 3,
            "volume_ratio_min": 1.8,
            "momentum_lookback": 5,
        },
        "counterfactual": True,
        "n_variants": 50,
    },
    {
        "name": "core-conservative",
        "universe": "core",
        "desc": "Core, conservative: long MA, high volume, tight RSI",
        "discovery": {
            "rsi_oversold_threshold": 40.0,
            "rsi_overbought_short_threshold": 60.0,
            "volume_ratio_min": 2.5,
            "ma_period": 40,
            "momentum_lookback": 20,
            "momentum_threshold_bullish": 0.03,
        },
        "counterfactual": True,
        "n_variants": 50,
    },
    {
        "name": "all-mean-reversion",
        "universe": "all",
        "desc": "All tickers, mean reversion: tight RSI bounce, short MA",
        "discovery": {
            "rsi_oversold_threshold": 30.0,
            "rsi_period": 7,
            "ma_period": 10,
            "conviction_rsi_bounce_bonus": 0.35,
            "conviction_ma_proximity_bonus": 0.25,
            "volume_ratio_min": 1.3,
            "momentum_threshold_bullish": 0.01,
        },
        "counterfactual": True,
        "n_variants": 50,
    },
    {
        "name": "core-momentum",
        "universe": "core",
        "desc": "Core, momentum play: long lookback, low volume req, MA proximity",
        "discovery": {
            "ma_period": 30,
            "ma_max_distance_pct": 2.0,
            "momentum_lookback": 30,
            "momentum_threshold_bullish": 0.04,
            "volume_ratio_min": 1.2,
            "conviction_ma_proximity_bonus": 0.3,
        },
        "counterfactual": True,
        "n_variants": 50,
    },
    {
        "name": "stonks-hybrid",
        "universe": "stonks",
        "desc": "Stonks, hybrid: moderate everything, balanced",
        "discovery": {
            "rsi_oversold_threshold": 35.0,
            "rsi_overbought_short_threshold": 68.0,
            "rsi_period": 10,
            "volume_ratio_min": 1.5,
            "ma_period": 15,
            "momentum_lookback": 12,
            "momentum_threshold_bullish": 0.025,
            "conviction_strong_threshold": 0.6,
            "conviction_moderate_threshold": 0.35,
        },
        "counterfactual": True,
        "n_variants": 50,
    },
    {
        "name": "aldridge-volume-spike",
        "universe": "aldridge",
        "desc": "Aldridge, volume spike: high consecutive, volume-driven",
        "discovery": {
            "volume_ratio_min": 2.0,
            "volume_spike_consecutive": 3,
            "catalyst_volume_threshold": 1.8,
            "conviction_high_volume_bonus": 0.3,
            "conviction_strong_threshold": 0.55,
            "ma_period": 20,
        },
        "counterfactual": True,
        "n_variants": 50,
    },
    {
        "name": "kairos-bounce",
        "universe": "kairos",
        "desc": "Kairos, RSI bounce focus: tight bounce bonus, short windows",
        "discovery": {
            "rsi_oversold_threshold": 33.0,
            "rsi_period": 9,
            "conviction_rsi_bounce_bonus": 0.4,
            "conviction_moderate_threshold": 0.35,
            "volume_ratio_min": 1.4,
            "momentum_lookback": 8,
            "ma_period": 15,
        },
        "counterfactual": True,
        "n_variants": 50,
    },
]

def extract_key_results(result, iteration):
    """Extract human-readable key results for Stan's journal."""
    report = result.report
    top_5 = report.top_5 if report and report.top_5 else []
    
    lines = []
    lines.append(f"## {iteration['name']}: {iteration['desc']}")
    lines.append(f"Universe: {iteration['universe']} ({len(UNIVERSES[iteration['universe']])} tickers)")
    lines.append(f"Duration: {result.duration_seconds:.1f}s")
    lines.append(f"Variants tested: {result.n_variants_tested}")
    lines.append(f"Signals discovered: {len(result.discovered_signals)}")
    lines.append("")
    
    if top_5:
        lines.append("### Top 5 Configs")
        for entry in top_5:
            score = entry.score
            metrics = entry.metrics
            catch_rate = metrics.get("catch_rate", 0)
            win_rate = metrics.get("win_rate", 0)
            total_return = metrics.get("total_return_pct", 0)
            n_trades = metrics.get("n_trades", 0)
            lines.append(f"  #{entry.rank} | Score: {score:.4f} | Catch: {catch_rate:.4f} | Win: {win_rate:.2%} | Return: {total_return:.2f}% | Trades: {n_trades}")
    else:
        lines.append("  No top configs found.")
    
    if result.errors:
        lines.append(f"\nErrors: {len(result.errors)}")
        for e in result.errors[:3]:
            lines.append(f"  - {e}")
    
    # Best and worst metrics across all scored variants
    if result.scored_variants:
        returns = [sv.actual_next_day_return for sv in result.scored_variants if hasattr(sv, 'actual_next_day_return')]
        catches = [sv.actual_next_day_return > 0 for sv in result.scored_variants if hasattr(sv, 'actual_next_day_return')]
        if returns:
            lines.append(f"\nBest return: {max(returns):.4f}")
            lines.append(f"Worst return: {min(returns):.4f}")
            lines.append(f"Avg return: {sum(returns)/len(returns):.4f}")
            lines.append(f"Win rate: {sum(catches)/len(catches):.2%}" if catches else "No trades")
    
    # Counterfactual results
    if result.counterfactual_results:
        lines.append(f"\nCounterfactual runs: {len(result.counterfactual_results)}")
    
    # Missed opportunities from improvement_potential text
    if report and report.improvement_potential:
        potential = report.improvement_potential[:500]  # truncate
        lines.append(f"\nImprovement potential:\n{potential}")
    
    return "\n".join(lines)


def run_iteration(it, seed_base):
    """Run a single iteration and save results."""
    name = it["name"]
    universe_tickers = UNIVERSES[it["universe"]]
    seed = seed_base + len(list(RESULTS_DIR.glob(f"*{name}*")))
    
    print(f"\n{'='*60}")
    print(f"ITERATION: {name}")
    print(f"  {it['desc']}")
    print(f"  Universe: {it['universe']} ({len(universe_tickers)} tickers)")
    print(f"  Seed: {seed}")
    print(f"{'='*60}")
    
    config = DiscoveryConfig(**it["discovery"]) if it.get("discovery") else DiscoveryConfig()

    ticks = load_ticks(universe_tickers, days=it.get("days", 20))
    if not ticks:
        print(f"  No real ticks loaded for {name}, skipping iteration")
        return None, f"FAILED: {name}\n  no ticks loaded from {CACHE_DIR}"

    harness = OvernightHarness(
        initial_balance=100000.0,
        data_dir=str(CACHE_DIR),
        n_variants=it["n_variants"],
        discovery_config=config,
        seed=seed,
        counterfactual=it.get("counterfactual", True),
    )

    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    leaderboard_path = str(RESULTS_DIR / f"{timestamp}-{name}.yaml")

    try:
        result = harness.run_with_logging(
            ticks=ticks,
            n_variants=it["n_variants"],
            leaderboard_path=leaderboard_path,
        )
        
        print(f"\n  Duration: {result.duration_seconds:.1f}s")
        print(f"  Variants: {result.n_variants_tested}")
        print(f"  Signals: {len(result.discovered_signals)}")
        
        text = extract_key_results(result, it)
        
        # Also save as JSON for easy parsing
        json_path = str(RESULTS_DIR / f"{timestamp}-{name}.json")
        summary = {
            "name": name,
            "desc": it["desc"],
            "universe": it["universe"],
            "timestamp": timestamp,
            "duration_seconds": result.duration_seconds,
            "n_variants_tested": result.n_variants_tested,
            "n_signals": len(result.discovered_signals),
            "n_errors": len(result.errors),
            "top_scores": [
                {
                    "rank": e.rank,
                    "score": e.score,
                    "metrics": e.metrics,
                }
                for e in (result.report.top_5 if result.report else [])
            ] if result.report else [],
        }
        with open(json_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        
        print(f"  Saved: {leaderboard_path}")
        print(f"  Saved: {json_path}")
        
        return result, text
        
    except Exception as e:
        import traceback
        error_text = f"FAILED: {name}\n  {e}\n{traceback.format_exc()}"
        print(f"\n  {error_text}")
        # Save error report
        error_path = str(RESULTS_DIR / f"{timestamp}-{name}-ERROR.txt")
        with open(error_path, "w") as f:
            f.write(error_text)
        
        # Return a minimal mock result
        return None, error_text


def main():
    start_time = time.time()
    max_runtime = 7 * 3600  # 7 hours
    run_results = []
    
    for i, it in enumerate(ITERATIONS):
        elapsed = time.time() - start_time
        if elapsed > max_runtime * 0.85:  # Stop if we've used >85% of budget
            print(f"\n⚠️  {elapsed/3600:.1f}h elapsed, stopping to leave time for summary.")
            break
        
        result, text = run_iteration(it, seed_base=42)
        run_results.append((it["name"], result, text))
        
        # Print Stan's journal prompt to stdout for capture
        timestamp = datetime.now().strftime("%H:%M")
        print(f"\n---JOURNAL_TRIGGER:{it['name']}---")
        print(text)
        print(f"---END_JOURNAL_TRIGGER:{it['name']}---")
    
    # ── Compile morning summary ──
    morning_summary_path = RESULTS_DIR / f"morning-summary-{datetime.now().strftime('%Y-%m-%d-%H%M')}.md"
    successful = [(n, r, t) for n, r, t in run_results if r is not None]
    failed = [(n, r, t) for n, r, t in run_results if r is None]
    
    summary_lines = [
        f"# Overnight Optimization Summary — {datetime.now().strftime('%Y-%m-%d')}",
        f"",
        f"**Total iterations:** {len(run_results)}",
        f"**Successful:** {len(successful)}",
        f"**Failed:** {len(failed)}",
        f"**Duration:** {(time.time() - start_time)/3600:.1f}h",
        f"",
    ]
    
    if failed:
        summary_lines.append("## Failed Runs")
        for n, r, t in failed:
            summary_lines.append(f"- {n}")
        summary_lines.append("")
    
    # Collect top 3 configs across all runs
    all_top_configs = []
    for n, r, t in successful:
        if r and r.report and r.report.top_5:
            for entry in r.report.top_5[:3]:
                all_top_configs.append({
                    "run": n,
                    "score": entry.score,
                    "metrics": entry.metrics,
                    "params": entry.params,
                })
    
    all_top_configs.sort(key=lambda x: x["score"], reverse=True)
    
    summary_lines.append("## 🏆 Top 3 Configs Across All Runs")
    for i, cfg in enumerate(all_top_configs[:3]):
        m = cfg["metrics"]
        summary_lines.append(f"### #{i+1}: {cfg['run']} (Score: {cfg['score']:.4f})")
        summary_lines.append(f"- Catch rate: {m.get('catch_rate', 0):.4f}")
        summary_lines.append(f"- Win rate: {m.get('win_rate', 0):.2%}")
        summary_lines.append(f"- Total return: {m.get('total_return_pct', 0):.2f}%")
        summary_lines.append(f"- Trades: {m.get('n_trades', 0)}")
        summary_lines.append(f"- Key params: {cfg['params']}")
        summary_lines.append("")
    
    summary_lines.append("## 📊 Per-Run Summary")
    for n, r, t in successful:
        if r and r.report and r.report.top_5:
            top = r.report.top_5[0]
            summary_lines.append(f"- **{n}**: top score {top.score:.4f}, catch {top.metrics.get('catch_rate',0):.4f}, win {top.metrics.get('win_rate',0):.2%}, {r.n_variants_tested} variants")
        else:
            summary_lines.append(f"- **{n}**: {r.n_variants_tested} variants tested, {len(r.discovered_signals)} signals")
    
    summary_lines.append("")
    summary_lines.append("## 💡 Opportunities & Ideas")
    summary_lines.append("Consolidated from Stan's journal entries across all runs.")
    
    with open(morning_summary_path, "w") as f:
        f.write("\n".join(summary_lines))
    
    print(f"\n{'='*60}")
    print(f"MORNING SUMMARY SAVED: {morning_summary_path}")
    print(f"\nSummary:")
    print("\n".join(summary_lines))
    print(f"{'='*60}")
    
    # ── Print machine-readable results JSON for the calling script ──
    telegram_data = {
        "runs_completed": len(successful),
        "runs_failed": len(failed),
        "total_runs": len(run_results),
        "top3": all_top_configs[:3],
        "summary_path": str(morning_summary_path),
    }
    print(f"\n---TELEGRAM_DATA_START---")
    print(json.dumps(telegram_data, indent=2))
    print(f"---TELEGRAM_DATA_END---")


if __name__ == "__main__":
    main()
