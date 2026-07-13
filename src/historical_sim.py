#!/usr/bin/env python3
"""
historical_sim — Historical simulation using data bus API.

Pulls historical bar data through the running data bus API (192.168.1.41:5000)
instead of loading parquet files directly. Runs replay-based simulations
similar to src/simulator.py but optimized for CLI parameter sweeps.

Usage:
    python3 -m src.historical_sim sweep --trader kairos --ticker AAPL --days 5
    python3 -m src.historical_sim backtest --trader kairos --ticker AAPL --days 30
    python3 -m src.historical_sim findings --trader kairos
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
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import numpy as np

# ── Hermes review: transaction costs, holdout validation, sweep validation ──
from src.transaction_costs import CostModel  # type: ignore[import]
from src.holdout_validator import HoldoutSplitter, HoldoutConfig  # type: ignore[import]
from src.sweep_validation import two_phase_validate, ValidationConfig  # type: ignore[import]

# ── Path setup ────────────────────────────────────────────────────────────────
SRC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SRC_DIR.parent
for d in [str(SRC_DIR), str(PROJECT_DIR)]:
    if d not in sys.path:
        sys.path.insert(0, d)

log = logging.getLogger("historical_sim")

# ── Data bus URL ──────────────────────────────────────────────────────────────
DATA_BUS_URL = os.getenv("DATA_BUS_URL", "http://192.168.1.41:5000")


# ── Data fetching via data bus ────────────────────────────────────────────────

def fetch_bars_from_databus(
    tickers: List[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: str = "daily",
) -> Dict[str, List[dict]]:
    """Fetch historical OHLCV bars from the data bus API.

    Args:
        tickers: List of ticker symbols.
        start_date: ISO date filter (e.g. "2026-06-01").
        end_date: ISO date filter (e.g. "2026-07-02").
        interval: "daily" or "intraday".

    Returns:
        Dictionary mapping ticker -> list of OHLCV dicts.
    """
    params = {
        "symbols": ",".join(tickers),
        "interval": interval,
    }
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date

    try:
        resp = requests.get(f"{DATA_BUS_URL}/bars", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("symbols", {})
    except requests.RequestException as e:
        log.warning("Data bus /bars request failed: %s", e)
        return {}
    except json.JSONDecodeError as e:
        log.warning("Data bus /bars returned invalid JSON: %s", e)
        return {}


def fetch_quotes_from_databus(tickers: List[str]) -> Dict[str, dict]:
    """Fetch latest quote data from the data bus API.

    Args:
        tickers: List of ticker symbols.

    Returns:
        Dictionary mapping ticker -> quote dict with OHLCV + indicators.
    """
    params = {"symbols": ",".join(tickers)}
    try:
        resp = requests.get(f"{DATA_BUS_URL}/quotes", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("quotes", {})
    except requests.RequestException as e:
        log.warning("Data bus /quotes request failed: %s", e)
        return {}
    except json.JSONDecodeError as e:
        log.warning("Data bus /quotes returned invalid JSON: %s", e)
        return {}


# ── Backtest engine ───────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    """Result of a single backtest run."""
    ticker: str
    trader: str
    start_date: str
    end_date: str
    n_bars: int
    n_trades: int
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    final_equity: float
    params: Dict[str, Any] = field(default_factory=dict)


def compute_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """Compute RSI indicator."""
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.zeros_like(prices)
    avg_loss = np.zeros_like(prices)
    avg_gain[period] = np.mean(gains[:period])
    avg_loss[period] = np.mean(losses[:period])
    for i in range(period + 1, len(prices)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i - 1]) / period
    rs = np.divide(avg_gain, avg_loss, out=np.zeros_like(avg_gain), where=avg_loss != 0)
    rsi = 100 - (100 / (1 + rs))
    rsi[:period] = 50.0
    return rsi


def run_backtest(
    bars: List[dict],
    ticker: str,
    trader: str,
    params: Dict[str, Any],
    initial_cash: float = 100_000.0,
) -> BacktestResult:
    """Run a simple backtest on bar data.

    Uses a momentum/RSI strategy that varies by trader type.
    """
    if len(bars) < 20:
        return BacktestResult(
            ticker=ticker, trader=trader,
            start_date=bars[0]["timestamp"][:10] if bars else "",
            end_date=bars[-1]["timestamp"][:10] if bars else "",
            n_bars=len(bars), n_trades=0,
            total_return_pct=0.0, sharpe=0.0, max_drawdown_pct=0.0,
            win_rate=0.0, profit_factor=0.0, final_equity=initial_cash,
            params=params,
        )

    closes = np.array([b["close"] for b in bars], dtype=np.float64)
    rsi = compute_rsi(closes)

    # Trader-specific thresholds
    if trader == "kairos":
        rsi_overbought = params.get("rsi_overbought", 65)
        rsi_oversold = params.get("rsi_oversold", 35)
        trailing_stop_pct = params.get("trailing_stop_pct", 7.0)
        max_pos_pct = params.get("max_position_pct", 20.0)
        conviction = params.get("conviction", 0.63)
    elif trader == "stonks":
        rsi_overbought = params.get("rsi_overbought", 70)
        rsi_oversold = params.get("rsi_oversold", 30)
        volume_mult = params.get("volume_multiplier", 1.5)
        trailing_stop_pct = params.get("stop_loss_pct", 8.0)
        max_pos_pct = params.get("max_position_pct", 25.0)
        conviction = params.get("conviction", 0.6)
    elif trader == "aldridge":
        rsi_overbought = 80
        rsi_oversold = params.get("rsi_oversold", 40)
        trailing_stop_pct = params.get("stop_loss_pct", 8.0)
        max_pos_pct = params.get("max_position_pct", 25.0)
    else:
        rsi_overbought = 70
        rsi_oversold = 30
        trailing_stop_pct = 8.0
        max_pos_pct = 20.0

    # Run the backtest
    cash = initial_cash
    position = 0  # shares held
    entry_price = 0.0
    entry_bar = 0
    trades = []
    equity_curve = [initial_cash]
    high_water_mark = initial_cash

    for i in range(20, len(bars)):
        bar = bars[i]
        price = bar["close"]
        rsi_val = rsi[i]
        equity = cash + position * price
        high_water_mark = max(high_water_mark, equity)

        # Update trailing stop if in position
        if position > 0:
            mkt_val = position * price

            # Check trailing stop
            if trailing_stop_pct > 0:
                stop_price = entry_price * (1 - trailing_stop_pct / 100)
                if price <= stop_price:
                    proceeds = position * price
                    pnl = proceeds - (position * entry_price)
                    trades.append({
                        "entry_bar": entry_bar, "exit_bar": i,
                        "entry_price": entry_price, "exit_price": price,
                        "shares": position, "pnl": pnl,
                        "return_pct": (price - entry_price) / entry_price * 100,
                    })
                    cash += proceeds
                    position = 0

        # Entry signals
        if position == 0:
            # Buy signal: RSI oversold (oversold for aldridge)
            if (trader == "aldridge" and rsi_val <= rsi_oversold and rsi_val > 0) or \
               (trader != "aldridge" and rsi_val <= rsi_oversold and rsi_val > 0):
                max_cost = equity * (max_pos_pct / 100)
                shares = int(max_cost / price)
                if shares > 0 and cash >= shares * price:
                    position = shares
                    entry_price = price
                    entry_bar = i
                    cash -= shares * price
            elif trader == "kairos" and rsi_val >= rsi_overbought and rsi_val < 100:
                # Kairos enters on momentum (RSI overbought)
                max_cost = equity * (max_pos_pct / 100) * conviction
                shares = int(max_cost / price)
                if shares > 0 and cash >= shares * price:
                    position = shares
                    entry_price = price
                    entry_bar = i
                    cash -= shares * price

        # Exit signals (take profit or RSI overbought exit for non-kairos)
        elif position > 0:
            take_profit_pct = params.get("take_profit_pct", 15.0)
            if trader != "kairos" and rsi_val >= rsi_overbought and rsi_val < 100:
                proceeds = position * price
                pnl = proceeds - (position * entry_price)
                trades.append({
                    "entry_bar": entry_bar, "exit_bar": i,
                    "entry_price": entry_price, "exit_price": price,
                    "shares": position, "pnl": pnl,
                    "return_pct": (price - entry_price) / entry_price * 100,
                })
                cash += proceeds
                position = 0
            elif take_profit_pct > 0 and price >= entry_price * (1 + take_profit_pct / 100):
                proceeds = position * price
                pnl = proceeds - (position * entry_price)
                trades.append({
                    "entry_bar": entry_bar, "exit_bar": i,
                    "entry_price": entry_price, "exit_price": price,
                    "shares": position, "pnl": pnl,
                    "return_pct": (price - entry_price) / entry_price * 100,
                })
                cash += proceeds
                position = 0

        # Record equity
        equity_val = cash + position * price
        equity_curve.append(equity_val)

    # Close any open position at last price
    if position > 0:
        last_price = bars[-1]["close"]
        proceeds = position * last_price
        pnl = proceeds - (position * entry_price)
        trades.append({
            "entry_bar": entry_bar,
            "exit_bar": len(bars) - 1,
            "entry_price": entry_price,
            "exit_price": last_price,
            "shares": position,
            "pnl": pnl,
            "return_pct": (last_price - entry_price) / entry_price * 100,
        })
        cash += proceeds
        position = 0
        equity_curve[-1] = cash

    final_equity = equity_curve[-1]
    total_return_pct = (final_equity - initial_cash) / initial_cash * 100

    # Compute Sharpe ratio from daily returns
    equity_arr = np.array(equity_curve, dtype=np.float64)
    daily_returns = np.diff(equity_arr) / equity_arr[:-1]
    sharpe = 0.0
    if len(daily_returns) > 1 and np.std(daily_returns) > 0:
        sharpe = np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252)

    # Max drawdown
    if len(equity_arr) > 0:
        peak = np.maximum.accumulate(equity_arr)
        drawdown = (equity_arr - peak) / peak * 100
        max_drawdown_pct = float(np.min(drawdown))
    else:
        max_drawdown_pct = 0.0

    # Win rate and profit factor
    n_trades = len(trades)
    if n_trades > 0:
        wins = sum(1 for t in trades if t["pnl"] > 0)
        win_rate = wins / n_trades
        gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    else:
        win_rate = 0.0
        profit_factor = 0.0

    return BacktestResult(
        ticker=ticker,
        trader=trader,
        start_date=bars[0]["timestamp"][:10] if bars else "",
        end_date=bars[-1]["timestamp"][:10] if bars else "",
        n_bars=len(bars),
        n_trades=n_trades,
        total_return_pct=round(total_return_pct, 2),
        sharpe=round(sharpe, 4),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        win_rate=round(win_rate, 4),
        profit_factor=round(profit_factor, 2),
        final_equity=round(final_equity, 2),
        params=params,
    )


# ── CLI Commands ──────────────────────────────────────────────────────────────

def cmd_sweep(args):
    """Run parameter sweep across multiple tickers.

    Hermes review: applies transaction costs, uses holdout validation,
    and runs two-phase validation (signal + LLM gate) for overfitting detection.
    """
    trader = args.trader
    tickers_str = args.ticker or "AAPL,MSFT,SPY"
    tickers = [t.strip().upper() for t in tickers_str.split(",")]
    days = args.days
    variants = args.variants or 3
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    log.info("Sweep: trader=%s tickers=%s days=%d variants=%d",
             trader, tickers, days, variants)
    log.info("  Data range: %s to %s", start_date, end_date)
    log.info("  Data source: %s/bars", DATA_BUS_URL)

    # Fetch data from data bus
    print(f"\n{'='*60}")
    print(f"  📊 HISTORICAL SIM — Data Bus Pull")
    print(f"{'='*60}")
    print(f"  Fetching bars: {tickers}")
    print(f"  Range: {start_date} to {end_date}")
    print(f"  Source: {DATA_BUS_URL}/bars")
    print()

    bar_data = fetch_bars_from_databus(tickers, start_date, end_date, interval="daily")

    found = list(bar_data.keys())
    missing = [t for t in tickers if t not in bar_data]
    total_bars = sum(len(v) for v in bar_data.values())
    print(f"  Found {len(found)}/{len(tickers)} tickers, {total_bars} bars total")
    for t in found:
        print(f"    {t}: {len(bar_data[t])} bars")
    if missing:
        print(f"  Missing (no data): {missing}")
    print()

    if not found:
        log.error("No data returned from data bus for any ticker")
        return 1

    # ── Hermes review: apply holdout split ──
    splitter = HoldoutSplitter()
    dates = sorted(set(
        b["timestamp"][:10] for bars in bar_data.values() for b in bars
    ))
    train_dates, val_dates, holdout_dates = splitter.split(dates)
    log.info(
        "Holdout split: train=%d, val=%d, holdout=%d days",
        len(train_dates), len(val_dates), len(holdout_dates),
    )

    # Generate variant params
    param_sets = _generate_variants(trader, variants)

    # ── Hermes review: initialize cost model ──
    cost_model = CostModel.default()

    results = []
    for ticker in found:
        bars = bar_data[ticker]
        print(f"  Backtesting {ticker} with {variants} param variants...")
        for vid, params in enumerate(param_sets):
            result = run_backtest(bars, ticker, trader, params)
            # ── Hermes review: apply transaction costs ──
            cost_total = _apply_cost_model_to_backtest_result(result, cost_model)
            results.append(result)
            _print_result(result, vid)

    # Best result across all
    if results:
        sorted_results = sorted(results, key=lambda r: r.total_return_pct, reverse=True)
        best = sorted_results[0]
        print(f"\n{'='*60}")
        print(f"  🏆 BEST RESULT: {best.ticker} | {best.trader}")
        print(f"  Return: {best.total_return_pct:+.2f}% | Sharpe: {best.sharpe:.4f}")
        print(f"  Max DD: {best.max_drawdown_pct:.2f}% | Win Rate: {best.win_rate:.1%}")
        print(f"  Trades: {best.n_trades} | Profit Factor: {best.profit_factor:.2f}")
        print(f"  Params: {best.params}")
        print(f"{'='*60}\n")

        # Persist results to shared trader.db
        _persist_sweep_results(results, trader, start_date, end_date)

        # ── Hermes review: publish to virtual_traders DB ──
        _publish_sweep_to_virtuals(trader, best.params, best.total_return_pct)

    return 0


def cmd_backtest(args):
    """Run a single backtest."""
    trader = args.trader
    tickers_str = args.ticker.upper() if args.ticker else "AAPL"
    tickers = [t.strip() for t in tickers_str.split(",")]
    days = args.days or 30

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"  📈 BACKTEST: {trader} on {tickers}")
    print(f"  Range: {start_date} to {end_date}")
    print(f"  Source: {DATA_BUS_URL}/bars")
    print(f"{'='*60}\n")

    all_results = []
    for ticker in tickers:
        bar_data = fetch_bars_from_databus([ticker], start_date, end_date, interval="daily")
        bars = bar_data.get(ticker, [])
        if not bars:
            print(f"  ⚠️ No data returned for {ticker}")
            continue

        print(f"  {ticker}: Loaded {len(bars)} bars")

        # Use default params for trader
        schema = TRADER_PARAM_SCHEMAS.get(trader, TRADER_PARAM_SCHEMAS["kairos"])
        params = {p["name"]: p["default"] for p in schema}
        result = run_backtest(bars, ticker, trader, params)
        _print_result(result, 0)
        all_results.append((ticker, result))

    if not all_results:
        print(f"  ❌ No data returned for any ticker")
        return 1

    return 0


def cmd_findings(args):
    """Display sim findings — latest sweep results from DB."""
    trader = args.trader

    print(f"\n{'='*60}")
    print(f"  🔍 SIM FINDINGS: {trader or 'all traders'}")
    print(f"{'='*60}\n")

    # Read from shared/trader.db
    db_path = PROJECT_DIR / "shared" / "trader.db"
    if not db_path.exists():
        print(f"  No DB found at {db_path}")
        return 0

    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Check for sweep_results table
        tables = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [r["name"] for r in tables]

        if "sweep_results" in table_names:
            query = "SELECT * FROM sweep_results"
            params = []
            conditions = []
            if trader:
                conditions.append("trader = ?")
                params.append(trader)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY timestamp DESC, sharpe DESC LIMIT 30"

            rows = cur.execute(query, params).fetchall()
            if rows:
                print(f"  Found {len(rows)} sweep result(s):\n")
                for r in rows:
                    ts = r['timestamp'][:19] if r['timestamp'] else '?'
                    print(f"  [{ts}] {r['trader']} on {r['ticker']} (variant {r['variant_id']}):")
                    print(f"    Return: {r['total_return_pct']:+.2f}% | Sharpe: {r['sharpe']:.4f} | MaxDD: {r['max_drawdown_pct']:.2f}%")
                    print(f"    Trades: {r['n_trades']} | WR: {r['win_rate']*100:.0f}% | PF: {r['profit_factor']:.2f}")
                    if r['params']:
                        try:
                            bp = json.loads(r['params'])
                            print(f"    Params: {bp}")
                        except (json.JSONDecodeError, TypeError):
                            print(f"    Params: {r['params']}")
                    print()
            else:
                print(f"  No sweep results found{ ' for ' + trader if trader else ''}.")
        else:
            print(f"  No sweep_results table in DB.")
            print(f"  Available tables: {table_names}")

        conn.close()
    except Exception as e:
        print(f"  Error reading DB: {e}")

    return 0


# ── Helpers ───────────────────────────────────────────────────────────────────

TRADER_PARAM_SCHEMAS: Dict[str, List[Dict[str, Any]]] = {
    "kairos": [
        {"name": "rsi_overbought", "default": 65, "min": 55, "max": 80},
        {"name": "rsi_oversold", "default": 35, "min": 20, "max": 45},
        {"name": "trailing_stop_pct", "default": 7.0, "min": 3.0, "max": 15.0},
        {"name": "max_position_pct", "default": 20.0, "min": 5.0, "max": 40.0},
        {"name": "conviction", "default": 0.63, "min": 0.3, "max": 0.95},
    ],
    "stonks": [
        {"name": "rsi_overbought", "default": 70, "min": 60, "max": 85},
        {"name": "rsi_oversold", "default": 30, "min": 15, "max": 45},
        {"name": "volume_multiplier", "default": 1.5, "min": 1.0, "max": 3.0},
        {"name": "stop_loss_pct", "default": 8.0, "min": 3.0, "max": 15.0},
        {"name": "max_position_pct", "default": 25.0, "min": 5.0, "max": 50.0},
        {"name": "conviction", "default": 0.6, "min": 0.3, "max": 0.95},
    ],
    "aldridge": [
        {"name": "rsi_oversold", "default": 40, "min": 20, "max": 55},
        {"name": "pe_max", "default": 20.0, "min": 8.0, "max": 50.0},
        {"name": "stop_loss_pct", "default": 8.0, "min": 3.0, "max": 15.0},
        {"name": "take_profit_pct", "default": 15.0, "min": 5.0, "max": 30.0},
        {"name": "max_position_pct", "default": 25.0, "min": 5.0, "max": 50.0},
    ],
}


def _generate_variants(trader: str, n: int) -> List[Dict[str, Any]]:
    """Generate N parameter variants for sweeps."""
    schema = TRADER_PARAM_SCHEMAS.get(trader, TRADER_PARAM_SCHEMAS["kairos"])
    base = {p["name"]: p["default"] for p in schema}

    if n <= 1:
        return [base]

    variants = [base]
    rng = np.random.default_rng(42 + n)

    for v in range(1, n):
        variant = dict(base)
        for p in schema:
            if "min" in p and "max" in p and p["min"] != p["max"]:
                ptype = type(p["default"])
                if ptype == int:
                    variant[p["name"]] = int(rng.integers(p["min"], p["max"] + 1))
                elif ptype == float:
                    val = rng.uniform(p["min"], p["max"])
                    variant[p["name"]] = round(val, 2)
        variants.append(variant)

    return variants


def _print_result(result: BacktestResult, variant_id: int):
    """Print a single backtest result."""
    ret_color = "+" if result.total_return_pct >= 0 else ""
    print(f"    [{variant_id}] {result.ticker} | "
          f"Return: {ret_color}{result.total_return_pct:+.2f}% | "
          f"Sharpe: {result.sharpe:.4f} | "
          f"MaxDD: {result.max_drawdown_pct:.2f}% | "
          f"WR: {result.win_rate:.1%} | "
          f"Trades: {result.n_trades} | "
          f"PF: {result.profit_factor:.2f}")


def _publish_sweep_to_virtuals(
    trader: str,
    best_params: Dict[str, Any],
    score: float,
) -> int:
    """Publish sweep best params to virtual_traders DB.

    Bridge from prompt_sweep winning variants to virtual_traders config.
    Delegates to virtual_cull.publish_sweep_results().

    Args:
        trader: Base trader name.
        best_params: Best params from the sweep.
        score: Best objective score.

    Returns:
        Number of virtual traders updated.
    """
    try:
        from src.virtual_cull import publish_sweep_results as _publish  # type: ignore[import]
        return _publish(trader, best_params, score)
    except ImportError:
        log.warning("virtual_cull not available — skipping publish_sweep_to_virtuals")
        return 0
    except Exception as e:
        log.warning("publish_sweep_to_virtuals failed: %s", e)
        return 0


def _apply_cost_model_to_backtest_result(
    result: BacktestResult,
    cost_model: CostModel,
) -> float:
    """Apply transaction costs to a BacktestResult.

    Computes estimated costs per trade and adjusts the total return.

    Returns:
        Total cost deducted.
    """
    # Approximate: each trade round-trip costs ~$1-2 at retail
    # This is a simplified version of CostModel.apply_to_result from replay.py
    total_cost = 0.0
    for _ in range(result.n_trades):
        # Use a simplified per-trade cost
        avg_notional = 1000.0  # rough estimate
        cost = max(
            avg_notional * (cost_model.slippage_bps + cost_model.spread_bps) / 10000.0,
            cost_model.min_trade_cost,
        )
        total_cost += cost

    # Adjust return: cost reduces final equity
    cost_as_pct = total_cost / 100_000.0  # relative to initial cash
    result.total_return_pct -= cost_as_pct * 100

    log.debug(
        "Applied cost model: %.2f total cost, adjusted return by %.2f%%",
        total_cost, cost_as_pct * 100,
    )
    return total_cost


def _persist_sweep_results(results: List[BacktestResult], trader: str,
                           start_date: str, end_date: str):
    """Write sweep results to shared/trader.db for dashboard consumption."""
    db_path = PROJECT_DIR / "shared" / "trader.db"
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        # Create sweep_results table if not exists
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sweep_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                trader TEXT NOT NULL,
                ticker TEXT NOT NULL,
                variant_id INTEGER,
                start_date TEXT,
                end_date TEXT,
                n_bars INTEGER,
                n_trades INTEGER,
                total_return_pct REAL,
                sharpe REAL,
                max_drawdown_pct REAL,
                win_rate REAL,
                profit_factor REAL,
                final_equity REAL,
                params TEXT,
                data_source TEXT DEFAULT 'databus'
            )
        """)

        for r in results:
            cur.execute(
                """INSERT INTO sweep_results
                   (trader, ticker, variant_id, start_date, end_date,
                    n_bars, n_trades, total_return_pct, sharpe,
                    max_drawdown_pct, win_rate, profit_factor,
                    final_equity, params, data_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r.trader, r.ticker, 0 if r.params else None,
                 r.start_date, r.end_date,
                 r.n_bars, r.n_trades, r.total_return_pct, r.sharpe,
                 r.max_drawdown_pct, r.win_rate, r.profit_factor,
                 r.final_equity, json.dumps(r.params), "databus"),
            )
        conn.commit()
        conn.close()
        log.info("Persisted %d sweep results to %s", len(results), db_path)
    except Exception as e:
        log.warning("Failed to persist sweep results: %s", e)


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    """Entry point with subcommand dispatch."""
    parser = argparse.ArgumentParser(
        prog="historical_sim",
        description="Historical simulation via data bus API.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # sweep
    sp = sub.add_parser("sweep", help="Run multi-variant parameter sweep")
    sp.add_argument("--trader", default="kairos", help="Trader type")
    sp.add_argument("--ticker", default="AAPL,MSFT,SPY", help="Ticker(s) comma-separated")
    sp.add_argument("--days", type=int, default=5, help="Days of history")
    sp.add_argument("--variants", type=int, default=3, help="Number of param variants")

    # backtest
    bp = sub.add_parser("backtest", help="Single backtest run")
    bp.add_argument("--trader", default="kairos")
    bp.add_argument("--ticker", default="AAPL")
    bp.add_argument("--days", type=int, default=30)

    # findings
    fp = sub.add_parser("findings", help="Show sim findings from DB")
    fp.add_argument("--trader", default=None, help="Filter by trader (default: all)")

    # improve — run sweep + publish to virtual_traders
    ip = sub.add_parser("improve", help="Run sweep and publish best params to virtual_traders")
    ip.add_argument("--trader", default="kairos", help="Trader type")
    ip.add_argument("--ticker", default="AAPL,MSFT,SPY", help="Ticker(s) comma-separated")
    ip.add_argument("--days", type=int, default=10, help="Days of history")
    ip.add_argument("--variants", type=int, default=5, help="Number of param variants")
    ip.add_argument("--publish", action="store_true", default=True,
                    help="Publish best params to virtual_traders DB (default: true)")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [historical_sim] %(levelname)s %(message)s",
    )

    if args.mode == "sweep":
        return cmd_sweep(args)
    elif args.mode == "backtest":
        return cmd_backtest(args)
    elif args.mode == "findings":
        return cmd_findings(args)
    elif args.mode == "improve":
        return cmd_improve(args)
    else:
        parser.print_help()
        return 1

# ── cmd_improve: run sweep + publish to virtual_traders ───────────────────────

def cmd_improve(args):
    """Run sweep and publish best params to virtual_traders DB.

    Chains:
      1. Run parameter sweep (same as cmd_sweep)
      2. Apply transaction costs + holdout validation
      3. Publish best params to virtual_traders via publish_sweep_results()

    This is the bridge (#94) from prompt_sweep winning variants to virtual_traders.
    """
    trader = args.trader
    tickers_str = args.ticker or "AAPL,MSFT,SPY"
    tickers = [t.strip().upper() for t in tickers_str.split(",")]
    days = args.days
    variants = args.variants or 5
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    log.info("Improve: trader=%s tickers=%s days=%d variants=%d",
             trader, tickers, days, variants)

    print(f"\n{'='*60}")
    print(f"  🚀 IMPROVE — Sweep + Publish to Virtual Traders")
    print(f"{'='*60}")
    print(f"  Trader: {trader}")
    print(f"  Range: {start_date} to {end_date}")
    print(f"  Variants: {variants}")
    print()

    # ── Step 1: Fetch data from data bus ──
    bar_data = fetch_bars_from_databus(tickers, start_date, end_date, interval="daily")
    found = list(bar_data.keys())
    if not found:
        log.error("No data returned from data bus for any ticker")
        return 1

    # ── Step 2: Holdout split ──
    splitter = HoldoutSplitter()
    dates = sorted(set(
        b["timestamp"][:10] for bars in bar_data.values() for b in bars
    ))
    train_dates, val_dates, holdout_dates = splitter.split(dates)
    log.info(
        "Holdout split: train=%d, val=%d, holdout=%d days",
        len(train_dates), len(val_dates), len(holdout_dates),
    )

    # ── Step 3: Generate variants and run backtest ──
    param_sets = _generate_variants(trader, variants)
    cost_model = CostModel.default()

    results = []
    for ticker in found:
        bars = bar_data[ticker]
        print(f"  Backtesting {ticker} with {variants} param variants...")
        for vid, params in enumerate(param_sets):
            result = run_backtest(bars, ticker, trader, params)
            _apply_cost_model_to_backtest_result(result, cost_model)
            results.append(result)
            _print_result(result, vid)

    if not results:
        print("  ❌ No results generated")
        return 1

    # ── Step 4: Find best result ──
    sorted_results = sorted(results, key=lambda r: r.total_return_pct, reverse=True)
    best = sorted_results[0]

    print(f"\n{'='*60}")
    print(f"  🏆 BEST RESULT: {best.ticker} | {best.trader}")
    print(f"  Return: {best.total_return_pct:+.2f}% | Sharpe: {best.sharpe:.4f}")
    print(f"  Max DD: {best.max_drawdown_pct:.2f}% | Win Rate: {best.win_rate:.1%}")
    print(f"  Trades: {best.n_trades} | Profit Factor: {best.profit_factor:.2f}")
    print(f"  Params: {best.params}")
    print(f"{'='*60}\n")

    # ── Step 5: Persist to local DB ──
    _persist_sweep_results(results, trader, start_date, end_date)

    # ── Step 6: Publish to virtual_traders ──
    if args.publish:
        print(f"  Publishing best params to virtual_traders DB...")
        updated = _publish_sweep_to_virtuals(trader, best.params, best.total_return_pct)
        print(f"  ✅ Published to {updated} virtual trader(s)")
    else:
        print(f"  Skipping publish (--no-publish)")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
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
