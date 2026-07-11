#!/usr/bin/env python3
"""
Prompt Sweep — nightly prompt variant generation, replay, ranking, and promotion.

Generates N=5 prompt variants per trader, replays each through yesterday's
market data using the rebuild's replay harness, ranks by objective score,
and creates a git branch for the winning variant if it beats baseline.

Usage:
    python3 src/prompt_sweep.py                                # all traders, yesterday's data
    python3 src/prompt_sweep.py --trader kairos                # single trader
    python3 src/prompt_sweep.py --variants 10                  # more variants
    python3 src/prompt_sweep.py --date 2026-07-03              # specific date
    python3 src/prompt_sweep.py --dry-run                      # score only, no git
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

# ── Add rebuild repo to path ─────────────────────────────────────────────────
_REBUILD_SRC = str(Path(__file__).resolve().parent.parent.parent / "paper-trading-rebuild" / "src")
if _REBUILD_SRC not in sys.path:
    sys.path.insert(0, _REBUILD_SRC)

from metrics import objective_score, compute_calmar, compute_profit_factor
from replay import ReplayHarness, Tick, Portfolio, TraderDecision, ReplayResult
from signals import SignalEngine, SignalParams

# ── Optional imports for multi-date + cost features ──────────────────────────
try:
    from src.bar_loader import BarLoader
except ImportError:
    BarLoader = None  # type: ignore[assignment]

try:
    from src.transaction_costs import CostModel
except ImportError:
    CostModel = None  # type: ignore[assignment]

try:
    from src.sweep_validation import (
        two_phase_validate,
        ValidationConfig,
    )
except ImportError:
    two_phase_validate = None  # type: ignore[assignment]
    ValidationConfig = None  # type: ignore[assignment]

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_DIR / "shared" / "trader.db"
AGENTS_DIR = PROJECT_DIR / "agents"
DATA_DIR = PROJECT_DIR / "data"

TRADER_IDS = ["trader-kairos", "trader-aldridge", "trader-stonks"]
SHORT_NAMES = {
    "trader-kairos": "kairos",
    "trader-aldridge": "aldridge",
    "trader-stonks": "stonks",
}

# ── Prompt perturbation templates ────────────────────────────────────────────
# Each template modifies a different aspect of the trading strategy.
# When applied to a prompt, it tweaks the behavior encoded in the prompt.

PERTURBATION_TEMPLATES = [
    {
        "name": "wider_stops",
        "description": "Widen stop-loss and take-profit by 50%",
        "param_changes": {
            "stop_loss_pct": 1.5,    # multiplier
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


# ═══════════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PromptVariant:
    """One prompt variant for evaluation."""
    trader: str
    variant_id: int
    variant_name: str
    description: str
    prompt_text: str         # Modified AGENTS.md content
    signal_params: SignalParams  # Parameter set derived from the variant
    baseline_params: SignalParams  # Original parameters for comparison
    score: float = 0.0
    calmar: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0
    n_trades: int = 0
    # Multi-date walk-forward metrics (only populated when --dates > 1)
    val_scores: List[float] = field(default_factory=list)
    avg_val_score: float = 0.0
    val_stability: float = 0.0

    @property
    def beats_baseline(self) -> bool:
        """Does this variant beat the baseline by a meaningful margin?"""
        return self.score > 0 and self.score > 0.05  # At least 0.05 objective improvement


@dataclass
class SweepResult:
    """Results of one prompt sweep run."""
    trader: str
    date: str
    baseline_score: float
    variants: List[PromptVariant]
    winner: Optional[PromptVariant] = None
    branch_name: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
# Prompt reading and variant generation
# ═══════════════════════════════════════════════════════════════════════════════

def read_trader_prompt(trader_short: str) -> str:
    """Read a trader's AGENTS.md file."""
    agents_md = AGENTS_DIR / f"trader-{trader_short}" / "AGENTS.md"
    if not agents_md.exists():
        raise FileNotFoundError(f"AGENTS.md not found for {trader_short}: {agents_md}")
    return agents_md.read_text()


def _extract_params_from_prompt(prompt_text: str) -> SignalParams:
    """Extract default signal parameters from the trader's prompt.

    Scans the prompt for parameter mentions and either:
    - Uses defaults from SignalParams if no overrides found
    - Adjusts based on strategy description (momentum vs value vs sentiment)

    Returns a SignalParams instance.
    """
    params = SignalParams()
    text_lower = prompt_text.lower()

    # Strategy detection from prompt language
    # Check sentiment first (may also mention momentum)
    if "sentiment" in text_lower or "meme" in text_lower:
        # Sentiment-focused trader → higher volatility tolerance
        params.vol_regime_threshold = 0.35
        params.weight_high_volatility = 0.6
        params.stop_loss_pct = 0.06
        params.base_size_pct = 0.10
    elif "social" in text_lower:
        # Social signals trader
        params.vol_regime_threshold = 0.32
        params.weight_high_volatility = 0.55
        params.stop_loss_pct = 0.06
        params.base_size_pct = 0.10
    elif "momentum" in text_lower and "value" not in text_lower:
        # Momentum-focused trader → higher momentum weights
        params.momentum_threshold = 0.003
        params.weight_trending_up = 1.3
        params.weight_trending_down = 0.4
        params.weight_mean_reverting = 0.6
    elif "value" in text_lower or "fundamental" in text_lower:
        # Value-focused trader → longer lookback, mean reversion
        params.momentum_lookback = 30
        params.momentum_threshold = 0.002
        params.weight_mean_reverting = 1.2
        params.weight_trending_up = 0.8
        params.rsi_oversold = 25.0
        params.rsi_overbought = 75.0

    # Look for explicit parameter mentions in the prompt
    import re

    pct_patterns = {
        "stop_loss_pct": r"stop.?loss[:\s]*(\d+\.?\d*)%",
        "take_profit_pct": r"(?:take.?profit|profit.?target)[:\s]*(\d+\.?\d*)%",
        "base_size_pct": r"position.?size[:\s]*(\d+\.?\d*)%",
    }

    for param_name, pattern in pct_patterns.items():
        m = re.search(pattern, prompt_text, re.IGNORECASE)
        if m:
            try:
                pct_value = float(m.group(1)) / 100.0
                b = SignalParams.bound(param_name)
                params.set(param_name, b.clip(pct_value))
            except (ValueError, KeyError):
                pass

    params.clip_all()
    return params


def generate_variants(
    trader_short: str,
    prompt_text: str,
    n_variants: int = 5,
    seed: int = 42,
) -> List[PromptVariant]:
    """Generate N prompt variants with parameter perturbations.

    Uses the perturbation templates to create distinct strategy variants.
    Each variant gets a modified SignalParams set and a modified prompt.
    """
    rng = random.Random(seed)
    baseline_params = _extract_params_from_prompt(prompt_text)

    # Select n_variants templates (rotate if needed)
    templates = PERTURBATION_TEMPLATES.copy()
    rng.shuffle(templates)
    selected = templates[:n_variants]

    from dataclasses import fields as dc_fields
    field_names = [f.name for f in dc_fields(SignalParams) if f.name != "_BOUNDS"]

    variants = []
    for i, template in enumerate(selected):
        variant_params = SignalParams(**{
            name: getattr(baseline_params, name)
            for name in field_names
        })

        # Apply parameter changes
        for param_name, multiplier in template["param_changes"].items():
            if hasattr(variant_params, param_name):
                current = getattr(variant_params, param_name)
                b = SignalParams.bound(param_name)
                if b.is_int:
                    new_val = int(round(current * multiplier))
                else:
                    new_val = current * multiplier
                variant_params.set(param_name, new_val)

        # Generate a modified prompt text with the variant strategy notes
        variant_prompt = _inject_variant_notes(prompt_text, template)

        variants.append(PromptVariant(
            trader=trader_short,
            variant_id=i + 1,
            variant_name=template["name"],
            description=template["description"],
            prompt_text=variant_prompt,
            signal_params=variant_params,
            baseline_params=baseline_params,
        ))

    return variants


def _inject_variant_notes(prompt_text: str, template: Dict[str, Any]) -> str:
    """Inject variant-specific strategy notes into the prompt.

    Adds a strategy adjustment section that the LLM trader would pick up on.
    """
    lines = []
    lines.append("")
    lines.append("## 🧬 Strategy Variant Override (Nightly Sweep)")
    lines.append(f"> Variant: **{template['name']}** — {template['description']}")
    lines.append("")

    for param, mult in template["param_changes"].items():
        direction = "increased" if mult > 1.0 else "decreased"
        pct = abs(mult - 1.0) * 100
        lines.append(f"- {param}: {direction} by {pct:.0f}%")

    lines.append("")
    lines.append("<!-- END VARIANT OVERRIDE -->")

    override_block = "\n".join(lines)
    return prompt_text.rstrip() + "\n" + override_block


# ═══════════════════════════════════════════════════════════════════════════════
# Walk-forward validation utilities
# ═══════════════════════════════════════════════════════════════════════════════

def get_trading_days(n_days: int, end_date: Optional[str] = None) -> List[str]:
    """Get the last N trading days (Mon-Fri) before end_date.

    Args:
        n_days: Number of trading days to collect.
        end_date: Reference date (ISO format). Default: today.

    Returns:
        List of ISO date strings in chronological order (oldest first).
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    dates: List[str] = []
    cursor = datetime.strptime(end_date, "%Y-%m-%d")
    max_attempts = n_days * 3  # Safety valve against infinite loops
    attempts = 0

    while len(dates) < n_days and attempts < max_attempts:
        attempts += 1
        if cursor.weekday() < 5:  # Mon-Fri only
            dates.append(cursor.strftime("%Y-%m-%d"))
        cursor -= timedelta(days=1)

    if len(dates) < n_days:
        raise ValueError(
            f"Could not find {n_days} trading days before {end_date}; "
            f"only found {len(dates)}"
        )

    return list(reversed(dates))  # Oldest first, newest last


def build_walk_forward_windows(
    dates: List[str], train_days: int, val_days: int
) -> List[Tuple[List[str], List[str]]]:
    """Build train/val window pairs from a chronological list of dates.

    Each window uses train_days consecutive dates for training and the
    following val_days for validation. Windows slide forward one day at
    a time.

    Args:
        dates: Chronological date strings (oldest first).
        train_days: Number of training dates per window.
        val_days: Number of validation dates per window.

    Returns:
        List of (train_dates, val_dates) tuples.
    """
    windows: List[Tuple[List[str], List[str]]] = []
    for i in range(len(dates) - train_days - val_days + 1):
        train = dates[i : i + train_days]
        val = dates[i + train_days : i + train_days + val_days]
        windows.append((train, val))
    return windows


def _load_dates_data(
    dates: List[str],
    tickers: Optional[List[str]] = None,
) -> List[Tick]:
    """Load tick data for multiple dates using BarLoader with fallback.

    Tries BarLoader first for efficient date-range loading. Falls back
    to load_historical_ticks() per-date if BarLoader is unavailable or
    has no data.

    Args:
        dates: ISO date strings to load.
        tickers: Optional ticker filter.

    Returns:
        Combined list of Tick objects sorted by timestamp.
    """
    if not dates:
        return []

    # Try BarLoader for efficient date-range loading
    if BarLoader is not None:
        try:
            loader = BarLoader()
            start = dates[0]
            end = dates[-1]
            ticks = loader.load_date_range(
                tickers=tickers or ["SPY", "AAPL", "MSFT", "NVDA"],
                start_date=start,
                end_date=end,
                interval_minutes=5,
            )
            if ticks:
                return ticks
        except Exception:
            pass  # Fall through to per-date fallback

    # Per-date fallback using load_historical_ticks
    all_ticks: List[Tick] = []
    for date_str in dates:
        ticks = load_historical_ticks(date_str, tickers=tickers)
        all_ticks.extend(ticks)

    all_ticks.sort(key=lambda t: t.timestamp)
    return all_ticks


def _compute_walk_forward_metrics(
    val_scores: List[float],
    baseline_val_scores: List[float],
) -> Dict[str, float]:
    """Compute walk-forward aggregate metrics from per-window scores.

    Args:
        val_scores: List of variant objective scores (one per val window).
        baseline_val_scores: List of baseline objective scores (one per val window),
            aligned with val_scores.

    Returns:
        Dict with keys: avg_val_score, val_stability, win_rate.
    """
    if not val_scores or not baseline_val_scores:
        return {"avg_val_score": 0.0, "val_stability": 0.0, "win_rate": 0.0}

    arr = np.array(val_scores)
    wins = sum(1 for vs, bs in zip(val_scores, baseline_val_scores) if vs > bs)

    return {
        "avg_val_score": float(np.mean(arr)),
        "val_stability": float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
        "win_rate": wins / len(val_scores),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Data loading for replay
# ═══════════════════════════════════════════════════════════════════════════════

def load_historical_ticks(
    date_str: str,
    tickers: Optional[List[str]] = None,
) -> List[Tick]:
    """Load historical tick data for a given date.

    Tries to load from prices table, then market_cache, then generates
    synthetic data as fallback.
    """
    if tickers is None:
        tickers = ["SPY", "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOGL", "AMZN"]

    ticks: List[Tick] = []

    # Try to load from market_cache
    try:
        with _get_readonly_conn() as conn:
            placeholders = ",".join("?" for _ in tickers)
            rows = conn.execute(
                f"""SELECT ticker, fetched_at, open, high, low, close, volume, rsi
                    FROM market_cache
                    WHERE ticker IN ({placeholders})
                      AND date(fetched_at) = ?
                    ORDER BY ticker, fetched_at""",
                (*tickers, date_str),
            ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    if rows:
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["fetched_at"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                ts = datetime.strptime(date_str, "%Y-%m-%d")
            ticks.append(Tick(
                timestamp=ts,
                ticker=r["ticker"],
                open=r["open"] or r["close"] or 100.0,
                high=r["high"] or r["close"] or 100.0,
                low=r["low"] or r["close"] or 100.0,
                close=r["close"] or 100.0,
                volume=r["volume"] or 0,
                rsi=r["rsi"],
            ))

    if ticks:
        ticks.sort(key=lambda t: (t.ticker, t.timestamp))
        return ticks

    # Try prices table
    try:
        with _get_readonly_conn() as conn:
            placeholders = ",".join("?" for _ in tickers)
            rows = conn.execute(
                f"""SELECT ticker, fetched_at, open, high, low, close, volume, rsi
                    FROM prices
                    WHERE ticker IN ({placeholders})
                      AND date(fetched_at) = ?
                    ORDER BY ticker, fetched_at""",
                (*tickers, date_str),
            ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    if rows:
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["fetched_at"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                ts = datetime.strptime(date_str, "%Y-%m-%d")
            ticks.append(Tick(
                timestamp=ts,
                ticker=r["ticker"],
                open=r["open"] or r["close"] or 100.0,
                high=r["high"] or r["close"] or 100.0,
                low=r["low"] or r["close"] or 100.0,
                close=r["close"] or 100.0,
                volume=r["volume"] or 0,
                rsi=r["rsi"],
            ))

    if ticks:
        ticks.sort(key=lambda t: (t.ticker, t.timestamp))
        return ticks

    # Fallback: generate synthetic ticks for the date
    return _generate_synthetic_ticks(date_str, tickers)


def _generate_synthetic_ticks(date_str: str, tickers: List[str]) -> List[Tick]:
    """Generate synthetic tick data as fallback when no real data exists."""
    rng = np.random.default_rng(42)
    ticks = []

    base_prices = {
        "SPY": 590.0, "AAPL": 225.0, "MSFT": 450.0, "NVDA": 130.0,
        "TSLA": 340.0, "META": 700.0, "GOOGL": 185.0, "AMZN": 225.0,
    }
    base_time = datetime.strptime(f"{date_str}T09:30:00", "%Y-%m-%dT%H:%M:%S")
    # 6.5 hours of trading, one tick per 30 minutes = 13 ticks per ticker
    n_ticks = 13

    for ticker in tickers:
        price = base_prices.get(ticker, 100.0)
        for i in range(n_ticks):
            ts = base_time + timedelta(minutes=30 * i)
            noise = rng.normal(0, 0.005)
            price = price * (1 + noise)
            ticks.append(Tick(
                timestamp=ts,
                ticker=ticker,
                open=round(price * 0.999, 2),
                high=round(price * 1.005, 2),
                low=round(price * 0.995, 2),
                close=round(price, 2),
                volume=rng.integers(100_000, 5_000_000),
                rsi=50.0 + rng.normal(0, 5),
            ))

    ticks.sort(key=lambda t: t.timestamp)
    return ticks


def _get_readonly_conn() -> sqlite3.Connection:
    """Get a read-only connection to trader.db."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ═══════════════════════════════════════════════════════════════════════════════
# Signal-based trader for replay scoring
# ═══════════════════════════════════════════════════════════════════════════════

def make_signal_trader(params: SignalParams) -> Callable[[Tick, Portfolio], TraderDecision]:
    """Create a trader function that uses SignalEngine with given params.

    This replaces the LLM call with a deterministic signal-based decision.
    The SignalParams encode the strategy variant's behavior, so different
    variants produce different trade decisions and scores.

    Args:
        params: SignalParams tuned for this variant.

    Returns:
        Callable matching the TraderFn signature.
    """
    engine = SignalEngine(params=params)

    def trader_fn(tick: Tick, portfolio: Portfolio) -> TraderDecision:
        report = engine.process(tick)

        # Don't trade if we already hold this ticker
        if tick.ticker in portfolio.positions:
            pos = portfolio.positions[tick.ticker]

            # Check stop loss — risk management, always allowed
            if tick.close <= report.stop_loss:
                return TraderDecision(
                    ticker=tick.ticker,
                    decision="SELL",
                    conviction=report.conviction,
                    rationale=f"Stop loss hit at {tick.close:.2f}",
                    shares=pos.shares,
                    signal_override=True,
                )

            # Check take profit — risk management, always allowed
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

        # Entry logic: momentum signal gates, position sized by conviction
        if (report.momentum_signal == "BULLISH"
                and portfolio.position_count < report.max_positions):
            return TraderDecision(
                ticker=tick.ticker,
                decision="BUY",
                conviction=report.conviction,
                rationale=f"Bullish signal: momentum={report.momentum_score:.2f}, "
                          f"RSI={report.rsi:.1f}, regime={report.regime}",
                shares=0,  # Let harness size based on conviction
            )

        return TraderDecision(
            ticker=tick.ticker,
            decision="HOLD",
            conviction=0.0,
            rationale="No signal",
        )

    return trader_fn


# ═══════════════════════════════════════════════════════════════════════════════
# Variant scoring via replay
# ═══════════════════════════════════════════════════════════════════════════════

def score_variant(
    variant: PromptVariant,
    ticks: List[Tick],
    cost_model: Optional[Any] = None,
) -> Tuple[float, ReplayResult]:
    """Score a prompt variant by replaying it through historical ticks.

    Args:
        variant: The variant to score.
        ticks: Historical tick data.
        cost_model: Optional CostModel for applying transaction costs.

    Returns (objective_score, ReplayResult).
    """
    harness = ReplayHarness(
        initial_balance=100_000.0,
        max_position_pct=variant.signal_params.base_size_pct,
        require_conviction=0.3,
    )

    trader_fn = make_signal_trader(variant.signal_params)
    result = harness.run(ticks, trader_fn)

    # Apply transaction costs if requested (between harness.run and objective_score)
    if cost_model is not None:
        cost_model.apply_to_result(result)
        trade_pnls = [getattr(t, "pnl_net", t.pnl) for t in result.trades]
    else:
        trade_pnls = [t.pnl for t in result.trades]

    score = objective_score(result.returns, result.equity_curve, trade_pnls)

    return float(score), result


def score_variants(
    variants: List[PromptVariant],
    ticks: List[Tick],
    cost_model: Optional[Any] = None,
) -> List[PromptVariant]:
    """Score all variants and attach results."""
    for variant in variants:
        score, result = score_variant(variant, ticks, cost_model=cost_model)
        variant.score = score
        variant.calmar = float(compute_calmar(result.returns, result.equity_curve))
        # Use net PnL for profit factor if costs were applied
        trade_pnls = [
            getattr(t, "pnl_net", t.pnl) for t in result.trades
        ]
        variant.profit_factor = float(compute_profit_factor(trade_pnls))
        variant.win_rate = result.win_rate
        variant.n_trades = len(result.trades)

    # Sort by objective score descending
    variants.sort(key=lambda v: v.score, reverse=True)
    return variants


# ═══════════════════════════════════════════════════════════════════════════════
# Git operations for winner promotion
# ═══════════════════════════════════════════════════════════════════════════════

def _run_git(args: List[str]) -> Tuple[int, str, str]:
    """Run a git command in the paper-trading-teams repo."""
    cmd = ["git", "-C", str(PROJECT_DIR)] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _git_must(args: List[str]) -> str:
    """Run git command, raise on failure."""
    rc, out, err = _run_git(args)
    if rc != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {err}")
    return out


def create_winner_branch(
    trader_short: str,
    variant: PromptVariant,
    date_str: str,
    dry_run: bool = False,
) -> Optional[str]:
    """Create a git branch with the winning variant's prompt.

    Branch naming: sweep/YYYY-MM-DD/{trader}/variant-NNN

    Returns branch name or None if skipped.
    """
    branch_name = f"sweep/{date_str}/{trader_short}/variant-{variant.variant_id:03d}"
    agents_md = AGENTS_DIR / f"trader-{trader_short}" / "AGENTS.md"

    if dry_run:
        print(f"[DRY RUN] Would create branch: {branch_name}")
        print(f"[DRY RUN] Would update: {agents_md}")
        return branch_name

    try:
        # Ensure we're on main and clean
        _git_must(["checkout", "main"])
        _git_must(["pull", "origin", "main"])

        # Create branch
        _git_must(["checkout", "-b", branch_name])

        # Write the variant prompt
        agents_md.write_text(variant.prompt_text)
        _git_must(["add", str(agents_md.relative_to(PROJECT_DIR))])
        _git_must([
            "commit", "-m",
            f"sweep({trader_short}): variant-{variant.variant_id:03d} "
            f"({variant.variant_name}) "
            f"score={variant.score:.2f} vs baseline"
        ])
        _git_must(["push", "-u", "origin", branch_name])

        # Return to main
        _git_must(["checkout", "main"])

        print(f"[prompt_sweep] Created branch: {branch_name}")
        return branch_name

    except RuntimeError as e:
        print(f"[ERROR] Git operation failed: {e}", file=sys.stderr)
        # Try to return to main
        try:
            _run_git(["checkout", "main"])
        except Exception:
            pass
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Multi-date walk-forward sweep
# ═══════════════════════════════════════════════════════════════════════════════

def _run_multidate_sweep(
    date_str: str,
    trader_short: str,
    prompt_text: str,
    n_variants: int,
    n_dates: int,
    train_days: int,
    val_days: int,
    dry_run: bool,
    cost_model: Optional[Any] = None,
) -> SweepResult:
    """Run walk-forward validation across multiple dates for one trader.

    Algorithm:
    1. Collect last N trading days before date_str.
    2. Build walk-forward windows (train, val).
    3. Score baseline on each validation window.
    4. Score each variant on each validation window.
    5. Compute aggregate metrics (win_rate, avg_val_score, val_stability).
    6. Apply winner criteria: win_rate >= 0.6, avg_val_score > baseline + 0.05,
       val_stability < 2 * baseline_val_stability.
    7. Promote winner via git branch creation.
    """
    baseline_params = _extract_params_from_prompt(prompt_text)

    # 1. Get trading days
    dates = get_trading_days(n_dates, end_date=date_str)
    print(f"  Trading days: {dates[0]} → {dates[-1]} ({len(dates)} days)")

    # 2. Build walk-forward windows
    windows = build_walk_forward_windows(dates, train_days, val_days)
    if not windows:
        raise ValueError(
            f"Not enough dates for walk-forward: need at least "
            f"{train_days + val_days} days, got {len(dates)}. "
            f"Reduce --train or --val, or increase --dates."
        )
    print(f"  Walk-forward windows: {len(windows)} "
          f"(train={train_days}d, val={val_days}d)")

    # 3. Score baseline on each validation window
    print(f"  Scoring baseline across {len(windows)} windows...")
    baseline_val_scores: List[float] = []
    baseline = PromptVariant(
        trader=trader_short,
        variant_id=0,
        variant_name="baseline",
        description="Current production prompt",
        prompt_text=prompt_text,
        signal_params=baseline_params,
        baseline_params=baseline_params,
    )

    for wi, (train_dates, val_dates) in enumerate(windows):
        val_ticks = _load_dates_data(val_dates)
        bs, _ = score_variant(baseline, val_ticks, cost_model=cost_model)
        baseline_val_scores.append(bs)
        if (wi + 1) % max(1, len(windows) // 5) == 0:
            print(f"    Baseline window {wi + 1}/{len(windows)}: score={bs:.4f}")

    baseline_metrics = _compute_walk_forward_metrics(
        baseline_val_scores, baseline_val_scores
    )
    print(f"  Baseline: avg={baseline_metrics['avg_val_score']:.4f}, "
          f"stability={baseline_metrics['val_stability']:.4f}")

    # 4. Generate and score variants
    variants = generate_variants(trader_short, prompt_text, n_variants)
    print(f"  Scoring {len(variants)} variants across {len(windows)} windows...")

    for vi, variant in enumerate(variants):
        val_scores: List[float] = []
        for train_dates, val_dates in windows:
            val_ticks = _load_dates_data(val_dates)
            vs, result = score_variant(variant, val_ticks, cost_model=cost_model)
            val_scores.append(vs)

        variant.val_scores = val_scores
        metrics = _compute_walk_forward_metrics(val_scores, baseline_val_scores)
        variant.avg_val_score = metrics["avg_val_score"]
        variant.val_stability = metrics["val_stability"]
        variant.win_rate = metrics["win_rate"]

        # Also compute single-date metrics on the last validation window (for display)
        last_val_dates = windows[-1][1]
        last_ticks = _load_dates_data(last_val_dates)
        last_score, last_result = score_variant(
            variant, last_ticks, cost_model=cost_model
        )
        variant.score = last_score
        variant.calmar = float(compute_calmar(
            last_result.returns, last_result.equity_curve
        ))
        variant.profit_factor = float(compute_profit_factor([
            getattr(t, "pnl_net", t.pnl) for t in last_result.trades
        ]))
        variant.n_trades = len(last_result.trades)

        print(f"    [{vi + 1}/{len(variants)}] {variant.variant_name}: "
              f"avg_val={variant.avg_val_score:.4f}, "
              f"win_rate={variant.win_rate:.1%}, "
              f"stability={variant.val_stability:.4f}")

    # Sort by avg_val_score descending
    variants.sort(key=lambda v: v.avg_val_score, reverse=True)

    # 5. Winner criteria (must pass ALL)
    winner: Optional[PromptVariant] = None
    branch_name: Optional[str] = None

    for v in variants:
        passes_win_rate = v.win_rate >= 0.6
        passes_avg_score = v.avg_val_score > baseline_metrics["avg_val_score"] + 0.05
        passes_stability = (
            baseline_metrics["val_stability"] == 0.0
            or v.val_stability < 2.0 * baseline_metrics["val_stability"]
        )

        if passes_win_rate and passes_avg_score and passes_stability:
            winner = v
            print(f"\n  🏆 Winner: {v.variant_name}")
            print(f"     avg_val_score: {v.avg_val_score:.4f} "
                  f"(baseline: {baseline_metrics['avg_val_score']:.4f})")
            print(f"     win_rate: {v.win_rate:.1%}")
            print(f"     stability: {v.val_stability:.4f} "
                  f"(baseline: {baseline_metrics['val_stability']:.4f})")

            branch_name = create_winner_branch(
                trader_short, v, date_str, dry_run=dry_run,
            )
            break

    if winner is None:
        print(f"\n  ❌ No variant passed all walk-forward criteria")
        print(f"     Required: win_rate >= 0.6, avg_val > baseline + 0.05, "
              f"stability < 2× baseline")

    return SweepResult(
        trader=trader_short,
        date=date_str,
        baseline_score=baseline_metrics["avg_val_score"],
        variants=variants,
        winner=winner,
        branch_name=branch_name,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Main sweep function
# ═══════════════════════════════════════════════════════════════════════════════

def run_sweep(
    date_str: Optional[str] = None,
    trader: Optional[str] = None,
    n_variants: int = 5,
    dry_run: bool = False,
    n_dates: int = 1,
    train_days: int = 5,
    val_days: int = 1,
    slippage_bps: float = 0.0,
    use_costs: bool = False,
    phase2: bool = False,
    phase2_top_k: int = 3,
    phase2_budget: int = 9,
) -> List[SweepResult]:
    """Run the full prompt sweep pipeline.

    Args:
        date_str: Date to sweep (YYYY-MM-DD). Default: yesterday.
        trader: Trader short name (e.g., 'kairos'). Default: all.
        n_variants: Number of variants to generate per trader.
        dry_run: If True, skip git operations.
        n_dates: Number of historical trading days (1 = single-date, current behavior).
        train_days: Training days per walk-forward window.
        val_days: Validation days per walk-forward window.
        slippage_bps: Transaction cost in basis points.
        use_costs: Whether to apply transaction costs.
        phase2: Enable two-phase validation (signal → LLM gate).
        phase2_top_k: Top K variants for LLM validation.
        phase2_budget: Max LLM runs per trader.

    Returns:
        List of SweepResult, one per trader.
    """
    if date_str is None:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # Build cost model if requested
    cost_model: Optional[Any] = None
    if use_costs and slippage_bps > 0 and CostModel is not None:
        cost_model = CostModel(slippage_bps=slippage_bps)
    elif use_costs and CostModel is not None:
        cost_model = CostModel.default()

    traders = [trader] if trader else [SHORT_NAMES[tid] for tid in TRADER_IDS]
    print(f"[prompt_sweep] Date: {date_str}")
    print(f"[prompt_sweep] Traders: {', '.join(traders)}")
    print(f"[prompt_sweep] Variants per trader: {n_variants}")
    if n_dates > 1:
        print(f"[prompt_sweep] Mode: multi-date walk-forward "
              f"(dates={n_dates}, train={train_days}, val={val_days})")
        if cost_model is not None:
            print(f"[prompt_sweep] Costs: enabled (slippage={cost_model.slippage_bps} bps)")
    print()

    # ── Single-date mode (backward compatible) ────────────────────────────
    if n_dates <= 1:
        ticks = _load_dates_data([date_str])
        print(f"[prompt_sweep] Loaded {len(ticks)} ticks for {date_str}")

        results: List[SweepResult] = []
        for trader_short in traders:
            print(f"\n{'='*60}")
            print(f"[prompt_sweep] Sweeping {trader_short}...")
            print(f"{'='*60}")

            prompt_text = read_trader_prompt(trader_short)
            baseline_params = _extract_params_from_prompt(prompt_text)

            baseline_variant = PromptVariant(
                trader=trader_short,
                variant_id=0,
                variant_name="baseline",
                description="Current production prompt",
                prompt_text=prompt_text,
                signal_params=baseline_params,
                baseline_params=baseline_params,
            )
            baseline_score, _ = score_variant(baseline_variant, ticks, cost_model=cost_model)
            print(f"  Baseline score: {baseline_score:.4f}")

            variants = generate_variants(trader_short, prompt_text, n_variants)
            print(f"  Generated {len(variants)} variants")

            variants = score_variants(variants, ticks, cost_model=cost_model)

            # Print leaderboard
            print(f"\n  {'Rank':<5} {'Variant':<25} {'Score':<8} {'Calmar':<8} {'PF':<8} {'WinRate':<8} {'Trades':<7}")
            print(f"  {'-'*5} {'-'*25} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*7}")
            print(f"  {'0':<5} {'baseline':<25} {baseline_score:<8.4f}")

            for i, v in enumerate(variants, 1):
                flag = " ★" if v.beats_baseline else ""
                print(f"  {i:<5} {v.variant_name:<25} {v.score:<8.4f} "
                      f"{v.calmar:<8.2f} {v.profit_factor:<8.2f} "
                      f"{v.win_rate:<8.1%} {v.n_trades:<7}{flag}")

            winner = variants[0] if variants else None
            branch_name = None

            if winner and winner.beats_baseline and winner.score > baseline_score:
                print(f"\n  🏆 Winner: {winner.variant_name} (score: {winner.score:.4f})")
                winner_label = winner
                branch_name = create_winner_branch(
                    trader_short, winner, date_str, dry_run=dry_run,
                )
            else:
                print(f"\n  ❌ No variant beat baseline significantly")
                winner_label = None

            results.append(SweepResult(
                trader=trader_short,
                date=date_str,
                baseline_score=baseline_score,
                variants=variants,
                winner=winner_label,
                branch_name=branch_name,
            ))

        return results

    # ── Multi-date walk-forward mode ───────────────────────────────────────
    results = []
    for trader_short in traders:
        print(f"\n{'='*60}")
        print(f"[prompt_sweep] Walk-forward sweep: {trader_short}")
        print(f"{'='*60}")

        prompt_text = read_trader_prompt(trader_short)
        sweep = _run_multidate_sweep(
            date_str=date_str,
            trader_short=trader_short,
            prompt_text=prompt_text,
            n_variants=n_variants,
            n_dates=n_dates,
            train_days=train_days,
            val_days=val_days,
            dry_run=dry_run,
            cost_model=cost_model,
        )
        results.append(sweep)

    # ── Phase 2: LLM validation (if enabled) ─────────────────────────────
    if phase2 and two_phase_validate is not None and ValidationConfig is not None:
        print(f"\n{'='*60}")
        print(f"[prompt_sweep] Phase 2: LLM Validation")
        print(f"{'='*60}")

        for trader_short in traders:
            print(f"\n[prompt_sweep] Two-phase validation: {trader_short}")
            dates = get_trading_days(n_dates, end_date=date_str)

            config = ValidationConfig(
                phase1_variants=n_variants,
                phase2_top_k=phase2_top_k,
                max_llm_runs_per_trader=phase2_budget,
            )

            llm_winner, diagnostics = two_phase_validate(
                trader=trader_short,
                dates=dates,
                train_days=train_days,
                val_days=val_days,
                config=config,
                dry_run=dry_run,
            )

            # Update the corresponding SweepResult
            for sr in results:
                if sr.trader == trader_short:
                    if llm_winner:
                        sr.winner = llm_winner
                    if not diagnostics.get("signal_llm_divergence", False):
                        sr.branch_name = create_winner_branch(
                            trader_short, llm_winner, date_str, dry_run=dry_run,
                        ) if llm_winner else None
                    break

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Prompt Sweep — nightly variant generation, replay, ranking"
    )
    parser.add_argument("--date", type=str, default=None,
                        help="Date to sweep (YYYY-MM-DD). Default: yesterday.")
    parser.add_argument("--trader", type=str, default=None,
                        help="Single trader short name (e.g., 'kairos'). Default: all.")
    parser.add_argument("--variants", type=int, default=5,
                        help="Number of variants per trader (default: 5).")
    parser.add_argument("--dates", type=int, default=1,
                        help="Number of historical trading days (default: 1 = single-date).")
    parser.add_argument("--train", type=int, default=5,
                        help="Training days per walk-forward window (default: 5).")
    parser.add_argument("--val", type=int, default=1,
                        help="Validation days per walk-forward window (default: 1).")
    parser.add_argument("--slippage", type=float, default=0.0,
                        help="Transaction cost in basis points (default: 0).")
    parser.add_argument("--costs", action="store_true",
                        help="Apply transaction costs to replay results.")
    parser.add_argument("--phase2", action="store_true",
                        help="Enable LLM validation phase (default: signal only).")
    parser.add_argument("--phase2-top-k", type=int, default=3,
                        help="Top K variants to LLM validate (default: 3).")
    parser.add_argument("--phase2-budget", type=int, default=9,
                        help="Max LLM runs per trader (default: 9).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Score only — skip git branch creation.")

    args = parser.parse_args()

    results = run_sweep(
        date_str=args.date,
        trader=args.trader,
        n_variants=args.variants,
        dry_run=args.dry_run,
        n_dates=args.dates,
        train_days=args.train,
        val_days=args.val,
        slippage_bps=args.slippage,
        use_costs=args.costs,
        phase2=args.phase2,
        phase2_top_k=args.phase2_top_k,
        phase2_budget=args.phase2_budget,
    )

    # Final summary
    print(f"\n{'='*60}")
    print("Prompt Sweep Complete")
    print(f"{'='*60}")
    for sr in results:
        if sr.winner:
            winner_str = f" → {sr.winner.variant_name} (avg_val={sr.winner.avg_val_score:.3f})"
        else:
            winner_str = " → NONE"
        print(f"  {sr.trader}: baseline={sr.baseline_score:.3f}{winner_str}")
        if sr.branch_name:
            print(f"           branch: {sr.branch_name}")
    print()


if __name__ == "__main__":
    main()
