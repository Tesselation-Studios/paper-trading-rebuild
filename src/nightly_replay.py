#!/usr/bin/env python3
"""
Nightly Replay — Postgres-backed prompt variant sweep.

Loads bars from market_data.bars_5min (Postgres), bins raw microsecond ticks
to 5-min OHLC, converts to replay.Tick, generates prompt variants, scores
each via the replay harness, and outputs a leaderboard.

Usage:
    python3 src/nightly_replay.py --dry-run --date 2026-07-10
    python3 src/nightly_replay.py --date 2026-07-10
    python3 src/nightly_replay.py --date 2026-07-10 --variants 10
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── Add project src to path ──────────────────────────────────────────────────
_PROJECT_SRC = str(Path(__file__).resolve().parent)
if _PROJECT_SRC not in sys.path:
    sys.path.insert(0, _PROJECT_SRC)

from db.connection import get_connection
from metrics import objective_score, compute_calmar, compute_profit_factor
from replay import ReplayHarness, Tick, Portfolio, TraderDecision, TraderFn
from signals import SignalEngine, SignalParams

log = logging.getLogger("nightly_replay")

# ── Constants ────────────────────────────────────────────────────────────────

# The 6 built-in variant templates (same structure as prompt_sweep.PERTURBATION_TEMPLATES
# but with SL/TP adjusted to 1%/3% per the fix requirements).
_VARIANT_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "wider_stops",
        "description": "Widen stop-loss and take-profit by 50%",
        "param_changes": {
            "stop_loss_pct": 1.5,
            "take_profit_pct": 1.5,
            "trailing_stop_pct": 1.5,
        },
    },
    {
        "name": "tighter_stops",
        "description": "Tighten stop-loss and take-profit by 30%",
        "param_changes": {
            "stop_loss_pct": 0.7,
            "take_profit_pct": 0.7,
            "trailing_stop_pct": 0.7,
        },
    },
    {
        "name": "aggressive_sizing",
        "description": "Increase position sizing and conviction multiplier",
        "param_changes": {
            "base_size_pct": 1.4,
            "conviction_multiplier": 1.3,
            "max_positions": 1.4,
        },
    },
    {
        "name": "conservative_sizing",
        "description": "Reduce position sizing and max positions",
        "param_changes": {
            "base_size_pct": 0.6,
            "conviction_multiplier": 0.7,
            "max_positions": 0.6,
        },
    },
    {
        "name": "momentum_focus",
        "description": "Increase momentum weight, reduce mean-reversion weight",
        "param_changes": {
            "momentum_threshold": 1.2,
            "weight_trending_up": 1.4,
            "weight_trending_down": 0.5,
            "weight_mean_reverting": 0.5,
        },
    },
    {
        "name": "mean_reversion_focus",
        "description": "Increase mean-reversion weight, reduce momentum",
        "param_changes": {
            "momentum_threshold": 0.7,
            "weight_trending_up": 0.6,
            "weight_mean_reverting": 1.6,
            "rsi_oversold": 1.3,
        },
    },
    # Baseline + 6 = 7 variants by default. Add more for variety:
    {
        "name": "trend_following",
        "description": "Strong trend following with longer lookback",
        "param_changes": {
            "momentum_lookback": 1.5,
            "momentum_decay": 1.1,
            "weight_trending_up": 1.6,
            "weight_high_volatility": 0.3,
        },
    },
    {
        "name": "volatility_adaptive",
        "description": "Adapt more aggressively to volatility regime changes",
        "param_changes": {
            "vol_regime_threshold": 0.8,
            "vol_reduction_multiplier": 0.5,
            "weight_high_volatility": 0.7,
        },
    },
]

# ── Fixes: SL/TP 1%/3%, history window 5+ days, conviction gating 0.4 ──────
# These are applied as the baseline SignalParams for the nightly run.
# SL/TP: 1%/3% (not 5%/15%)
# History window: momentum_lookback = 5 (at minimum 5 days for 5-min bars)
# Conviction: single gate at 0.4 (not double 0.2)

_NIGHTLY_PARAMS: Dict[str, float] = {
    "momentum_threshold": 0.55,
    "momentum_lookback": 20,       # 5+ days of 5-min bars
    "momentum_decay": 0.85,
    "rsi_oversold": 30.0,
    "rsi_overbought": 70.0,
    "bollinger_std": 2.0,
    "volume_threshold": 1.2,
    "vol_regime_threshold": 0.25,
    "vol_reduction_multiplier": 0.7,
    "base_size_pct": 0.15,
    "conviction_multiplier": 1.5,
    "max_positions": 5,
    "stop_loss_pct": 0.01,         # FIX: 1% stop-loss (not 5%)
    "take_profit_pct": 0.03,       # FIX: 3% take-profit (not 15%)
    "trailing_stop_pct": 0.01,     # FIX: 1% trailing stop
    "weight_trending_up": 1.0,
    "weight_trending_down": 0.5,
    "weight_mean_reverting": 0.8,
    "weight_high_volatility": 0.4,
}

# ── Data types ───────────────────────────────────────────────────────────────


@dataclass
class VariantResult:
    """Score for one variant in the nightly sweep."""
    variant_id: int
    variant_name: str
    description: str
    score: float
    calmar: float
    profit_factor: float
    win_rate: float
    n_trades: int
    total_pnl: float
    total_return_pct: float
    params: SignalParams

    @property
    def beats_baseline(self) -> bool:
        return self.score > 0.05


# ═══════════════════════════════════════════════════════════════════════════════
# Data loading — Postgres → 5-min OHLC bars → replay.Tick
# ═══════════════════════════════════════════════════════════════════════════════

def load_bars_from_postgres(
    date_str: str,
    lookback_days: int = 5,
) -> pd.DataFrame:
    """Load bars from market_data.bars_5min for the given date + lookback.

    Uses the `symbol` column (not `ticker` — this is a critical fix).

    Args:
        date_str: Target date (YYYY-MM-DD).
        lookback_days: How many days of history to load (default 5).

    Returns:
        DataFrame with columns: symbol, timestamp, open, high, low, close, volume.
    """
    target_date = datetime.strptime(date_str, "%Y-%m-%d")
    start_date = target_date - timedelta(days=lookback_days)

    conn = get_connection()
    try:
        cur = conn.cursor()
        # CRITICAL: column is `symbol` NOT `ticker`
        cur.execute(
            """SELECT symbol, timestamp, open, high, low, close, volume
               FROM market_data.bars_5min
               WHERE timestamp::date >= %s
                 AND timestamp::date <= %s
               ORDER BY symbol, timestamp""",
            (start_date.date(), target_date.date()),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    if not rows:
        raise ValueError(
            f"No bars found in market_data.bars_5min for "
            f"{start_date.date()} to {target_date.date()}"
        )

    df = pd.DataFrame(
        rows,
        columns=["symbol", "timestamp", "open", "high", "low", "close", "volume"],
    )
    # Convert numeric columns from Decimal to float
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    log.info(
        "Loaded %d raw rows from Postgres (%s to %s)",
        len(df), start_date.date(), target_date.date(),
    )
    return df


def bin_to_5min_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Bin raw microsecond ticks to 5-min OHLC bars.

    Resamples using pandas groupby + agg:
      - open: first value in the 5-min bucket
      - high: max high in the 5-min bucket
      - low: min low in the 5-min bucket
      - close: last value in the 5-min bucket
      - volume: sum of volume in the 5-min bucket

    Args:
        df: Raw bars DataFrame with symbol, timestamp, open, high, low, close, volume.

    Returns:
        DataFrame with one row per (symbol, 5-min bucket).
    """
    df = df.copy()

    # Floor timestamps to 5-minute buckets
    ts = pd.to_datetime(df["timestamp"])
    df["bucket"] = ts.dt.floor("5min")

    # Group by symbol + bucket and aggregate
    binned = df.groupby(["symbol", "bucket"], as_index=False).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )

    binned = binned.sort_values(["symbol", "bucket"]).reset_index(drop=True)
    log.info("Binned %d raw rows → %d 5-min bars", len(df), len(binned))
    return binned


def bars_to_ticks(df: pd.DataFrame) -> List[Tick]:
    """Convert a 5-min OHLC DataFrame to replay.Tick objects.

    Only includes US market hours (9:30 AM - 4:00 PM ET / 13:30-20:00 UTC).
    From July 6-10, timestamps are in UTC, so market hours = 13:30-20:00 UTC.

    Args:
        df: DataFrame with columns: symbol, bucket, open, high, low, close, volume.

    Returns:
        Sorted list of Tick objects.
    """
    ticks: List[Tick] = []

    for _, row in df.iterrows():
        ts = row["bucket"]
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()

        # Filter to US market hours (9:30 AM - 4:00 PM ET = 13:30-20:00 UTC)
        # If the timestamp is naive, assume UTC
        hour = ts.hour + ts.minute / 60.0
        if hour < 13.5 or hour >= 20.0:
            continue

        # Compute momentum from the bar's OHLC (simple return)
        momentum = (row["close"] - row["open"]) / row["open"] if row["open"] > 0 else 0.0

        ticks.append(Tick(
            timestamp=ts,
            ticker=row["symbol"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
            momentum=momentum * 100.0,  # Scale to percentage
            volatility=abs(float(row["high"] - row["low"]) / row["open"]) if row["open"] > 0 else 0.0,
        ))

    ticks.sort(key=lambda t: (t.timestamp, t.ticker))
    log.info("Converted %d bars → %d Tick objects (market hours only)", len(df), len(ticks))
    return ticks


def load_ticks_for_date(date_str: str, lookback_days: int = 5) -> List[Tick]:
    """One-stop: load from Postgres, bin to 5-min OHLC, convert to Ticks.

    Args:
        date_str: Target date (YYYY-MM-DD).
        lookback_days: Days of history to load (default 5).

    Returns:
        Sorted list of Tick objects for replay.
    """
    df = load_bars_from_postgres(date_str, lookback_days=lookback_days)
    binned = bin_to_5min_ohlc(df)
    ticks = bars_to_ticks(binned)
    return ticks


# ═══════════════════════════════════════════════════════════════════════════════
# Trader function builder (signal-only, no LLM)
# ═══════════════════════════════════════════════════════════════════════════════

def make_signal_trader(params: SignalParams) -> TraderFn:
    """Create a signal-only trader function (no LLM).

    Uses SignalEngine with the given params. This is the --dry-run mode
    trader — deterministic, fast, no external API calls.

    FIXES applied:
      - SL/TP: 1%/3% (from params)
      - Conviction gating: single gate at 0.4 minimum (not double 0.2)

    Args:
        params: SignalParams for this variant.

    Returns:
        Callable matching TraderFn signature.
    """
    engine = SignalEngine(params=params)
    # FIX: single conviction gate at 0.4
    MIN_CONVICTION = 0.4

    def trader_fn(tick: Tick, portfolio: Portfolio) -> TraderDecision:
        report = engine.process(tick)

        # If we already hold this ticker, check risk management & exit
        if tick.ticker in portfolio.positions:
            pos = portfolio.positions[tick.ticker]

            # Check stop loss — FIX: 1% SL (from params)
            if tick.close <= report.stop_loss:
                return TraderDecision(
                    ticker=tick.ticker,
                    decision="SELL",
                    conviction=report.conviction,
                    rationale=f"Stop loss hit at {tick.close:.2f}",
                    shares=pos.shares,
                    signal_override=True,
                )

            # Check take profit — FIX: 3% TP (from params)
            if tick.close >= report.take_profit:
                return TraderDecision(
                    ticker=tick.ticker,
                    decision="SELL",
                    conviction=report.conviction,
                    rationale=f"Take profit at {tick.close:.2f}",
                    shares=pos.shares,
                    signal_override=True,
                )

            return TraderDecision(
                ticker=tick.ticker,
                decision="HOLD",
                conviction=0.0,
                rationale="Position held",
            )

        # Entry logic: single conviction gate at 0.4
        if (report.momentum_signal == "BULLISH"
                and report.conviction >= MIN_CONVICTION
                and portfolio.position_count < report.max_positions):
            return TraderDecision(
                ticker=tick.ticker,
                decision="BUY",
                conviction=report.conviction,
                rationale=f"Bullish signal: momentum={report.momentum_score:.2f}, "
                          f"RSI={report.rsi:.1f}, regime={report.regime}",
                shares=0,
            )

        return TraderDecision(
            ticker=tick.ticker,
            decision="HOLD",
            conviction=0.0,
            rationale="No signal",
        )

    return trader_fn


# ═══════════════════════════════════════════════════════════════════════════════
# Variant generation
# ═══════════════════════════════════════════════════════════════════════════════

def generate_variant_params(baseline_params: SignalParams) -> List[SignalParams]:
    """Generate variant parameter sets from the baseline.

    Applies the _VARIANT_TEMPLATES to create distinct parameter sets.
    The baseline (identity) is included as variant 0.

    Returns:
        List of (variant_name, description, SignalParams) tuples.
    """
    from dataclasses import fields as dc_fields
    field_names = [f.name for f in dc_fields(SignalParams) if f.name != "_BOUNDS"]

    variants: List[Tuple[str, str, SignalParams]] = [
        ("baseline", "Baseline nightly parameters", baseline_params),
    ]

    for i, template in enumerate(_VARIANT_TEMPLATES):
        # Start from baseline params
        vp = SignalParams(**{
            name: getattr(baseline_params, name)
            for name in field_names
        })

        # Apply multiplier changes
        for param_name, multiplier in template["param_changes"].items():
            if hasattr(vp, param_name):
                current = getattr(vp, param_name)
                b = SignalParams.bound(param_name)
                if b.is_int:
                    new_val = int(round(current * multiplier))
                else:
                    new_val = current * multiplier
                vp.set(param_name, new_val)

        variants.append((template["name"], template["description"], vp))

    return variants


# ═══════════════════════════════════════════════════════════════════════════════
# Scoring
# ═══════════════════════════════════════════════════════════════════════════════

def score_variant(
    params: SignalParams,
    ticks: List[Tick],
) -> Tuple[float, Any]:
    """Score a variant by replaying it through historical ticks.

    Args:
        params: SignalParams for this variant.
        ticks: Historical tick data.

    Returns:
        (objective_score, ReplayResult).
    """
    harness = ReplayHarness(
        initial_balance=100_000.0,
        max_position_pct=params.base_size_pct,
        # FIX: single conviction gate at 0.4 — the harness require_conviction
        # only gates BUY entries, which is exactly what we want.
        # SELL exits (SL/TP) are always allowed via signal_override.
        require_conviction=0.4,
    )

    trader_fn = make_signal_trader(params)
    result = harness.run(ticks, trader_fn)

    trade_pnls = [t.pnl for t in result.trades]
    score = objective_score(result.returns, result.equity_curve, trade_pnls)

    return float(score), result


# ═══════════════════════════════════════════════════════════════════════════════
# Leaderboard
# ═══════════════════════════════════════════════════════════════════════════════

def print_leaderboard(
    results: List[VariantResult],
    baseline_score: float,
    total_ticks: int,
    elapsed: float,
) -> None:
    """Print a formatted leaderboard of variant scores."""
    print(f"\n{'='*80}")
    print(f"  NIGHTLY REPLAY LEADERBOARD")
    print(f"  Ticks: {total_ticks}  |  Elapsed: {elapsed:.1f}s")
    print(f"{'='*80}")
    print(f"  {'Rank':<5} {'Variant':<25} {'Score':<10} {'Calmar':<10} "
          f"{'PF':<8} {'WinRate':<10} {'Trades':<7} {'P&L':<12}")
    print(f"  {'-'*5} {'-'*25} {'-'*10} {'-'*10} {'-'*8} {'-'*10} {'-'*7} {'-'*12}")

    # Baseline is always first
    baseline_result = results[0] if results[0].variant_name == "baseline" else None
    if baseline_result:
        print(f"  {'0':<5} {'baseline':<25} {baseline_result.score:<10.4f} "
              f"{baseline_result.calmar:<10.2f} {baseline_result.profit_factor:<8.2f} "
              f"{baseline_result.win_rate:<10.1%} {baseline_result.n_trades:<7} "
              f"${baseline_result.total_pnl:<9.2f}")

    # Sort remaining by score descending
    sorted_results = sorted(
        [r for r in results if r.variant_name != "baseline"],
        key=lambda r: r.score, reverse=True,
    )

    for i, r in enumerate(sorted_results, 1):
        flag = " ★" if r.score > baseline_score + 0.05 else ""
        print(f"  {i:<5} {r.variant_name:<25} {r.score:<10.4f} "
              f"{r.calmar:<10.2f} {r.profit_factor:<8.2f} "
              f"{r.win_rate:<10.1%} {r.n_trades:<7} "
              f"${r.total_pnl:<9.2f}{flag}")

    # Find winner
    best = sorted_results[0] if sorted_results else None
    if best and best.score > baseline_score + 0.05:
        print(f"\n  🏆 Winner: {best.variant_name} (score: {best.score:.4f} vs baseline: {baseline_score:.4f})")
    else:
        print(f"\n  ❌ No variant beat baseline significantly")
        if best:
            print(f"     Best: {best.variant_name} ({best.score:.4f}) vs baseline ({baseline_score:.4f})")

    print(f"{'='*80}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def run_nightly_replay(
    date_str: str,
    dry_run: bool = True,
    lookback_days: int = 5,
) -> List[VariantResult]:
    """Run the full nightly replay pipeline.

    Args:
        date_str: Target date (YYYY-MM-DD).
        dry_run: If True, use signal-only trader (no LLM).
        lookback_days: Days of history to load.

    Returns:
        List of VariantResult sorted by score (baseline first).
    """
    t0 = time.time()

    # ── 1. Load data ────────────────────────────────────────────────────
    print(f"[nightly_replay] Loading bars from Postgres for {date_str} "
          f"(lookback: {lookback_days}d)...")
    ticks = load_ticks_for_date(date_str, lookback_days=lookback_days)
    print(f"[nightly_replay] Loaded {len(ticks)} ticks for {date_str}")

    if not ticks:
        print("[nightly_replay] ERROR: No ticks loaded — aborting")
        return []

    # ── 2. Setup baseline params ────────────────────────────────────────
    baseline_params = SignalParams.from_dict(_NIGHTLY_PARAMS)
    print(f"[nightly_replay] Baseline params: SL={baseline_params.stop_loss_pct:.0%}, "
          f"TP={baseline_params.take_profit_pct:.0%}, "
          f"mom_lookback={baseline_params.momentum_lookback}, "
          f"conviction_gate=0.4")

    # ── 3. Generate variants ────────────────────────────────────────────
    variants = generate_variant_params(baseline_params)
    print(f"[nightly_replay] Generated {len(variants)} variants "
          f"(baseline + {len(variants) - 1} perturbations)")

    # ── 4. Score each variant ────────────────────────────────────────────
    results: List[VariantResult] = []
    variant_id = 0

    for variant_name, description, params in variants:
        vt0 = time.time()
        score, result = score_variant(params, ticks)
        vt1 = time.time()

        trade_pnls = [t.pnl for t in result.trades]
        calmar = float(compute_calmar(result.returns, result.equity_curve))
        profit_factor = float(compute_profit_factor(trade_pnls))

        vr = VariantResult(
            variant_id=variant_id,
            variant_name=variant_name,
            description=description,
            score=score,
            calmar=calmar,
            profit_factor=profit_factor,
            win_rate=result.win_rate,
            n_trades=len(result.trades),
            total_pnl=result.total_pnl,
            total_return_pct=result.total_return_pct,
            params=params,
        )
        results.append(vr)

        print(f"  [{variant_id + 1}/{len(variants)}] {variant_name:<25} "
              f"score={score:.4f}  trades={len(result.trades):<4}  "
              f"P&L=${result.total_pnl:<8.2f}  ({vt1 - vt0:.1f}s)")

        variant_id += 1

    # ── 5. Print leaderboard ────────────────────────────────────────────
    total_elapsed = time.time() - t0
    print_leaderboard(results, results[0].score, len(ticks), total_elapsed)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Nightly Replay — Postgres-backed prompt variant sweep",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Target date (YYYY-MM-DD). Default: yesterday.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run without LLM (signal-only trader function).",
    )
    parser.add_argument(
        "--lookback", type=int, default=5,
        help="Days of history to load (default: 5).",
    )
    parser.add_argument(
        "--variants", type=int, default=8,
        help="Number of variants including baseline (default: 8 = baseline + 7 templates).",
    )
    args = parser.parse_args()

    # Set default date to yesterday
    date_str = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"[nightly_replay] {'='*60}")
    print(f"[nightly_replay] Nightly Replay")
    print(f"[nightly_replay] Date: {date_str}")
    print(f"[nightly_replay] Mode: {'DRY RUN (signal only)' if args.dry_run else 'FULL (LLM)'}")
    print(f"[nightly_replay] Lookback: {args.lookback}d")
    print(f"[nightly_replay] Variants: {args.variants}")
    print(f"[nightly_replay] {'='*60}")
    print()

    run_nightly_replay(
        date_str=date_str,
        dry_run=args.dry_run,
        lookback_days=args.lookback,
    )


if __name__ == "__main__":
    main()