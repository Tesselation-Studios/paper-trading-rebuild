#!/usr/bin/env python3
"""
Cross-sectional momentum ranking for paper trading agents.

Provides momentum-weighted ticker universe ranking via the data bus bars API.
Cached server-side to reduce API calls.

Usage:
    from src.skill_cross_sectional_momentum import get_cached_momentum_signal
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests

log = logging.getLogger("momentum")

# ── Config ────────────────────────────────────────────────────────────────────
DATA_BUS_URL = os.getenv("DATA_BUS_URL", "http://localhost:5000")

# Old Kairos-era mega-cap list (Kairos/Aldridge retired 2026-07-25). Kept only
# as a last-resort fallback for get_stonks_universe() below — Stan (the sole
# remaining trader) trades small/mid-cap, none of these overlap his universe.
TRACKED_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "SPY", "QQQ",
    "JPM", "V", "WMT", "JNJ", "XOM", "BAC", "DIS", "KO",
]

STONKS_WORKSPACE = Path(
    os.getenv("STONKS_WORKSPACE", "/home/openclaw/.openclaw/workspace-trader-stonks")
)

# Cache
_signal_cache: Dict[str, Any] = {}
_cache_ttl = 300  # 5 minutes


def read_stonks_universe_from_workspace() -> List[str]:
    """Stan's actual tracked universe: open positions + active watchlist candidates.

    Reads his workspace files directly — his real source of truth (he writes
    strategies/watchlist.md + positions/*.md, not any SQL watchlist table).
    Returns [] if the workspace is unreadable/empty; callers decide their own
    fallback rather than this function silently substituting one.
    """
    tickers: set = set()
    try:
        positions_dir = STONKS_WORKSPACE / "positions"
        if positions_dir.is_dir():
            tickers.update(f.stem.upper() for f in positions_dir.glob("*.md"))

        watchlist_path = STONKS_WORKSPACE / "strategies" / "watchlist.md"
        if watchlist_path.is_file():
            for line in watchlist_path.read_text().splitlines():
                line = line.strip()
                if not line.startswith("- ") or "~~" in line:
                    continue
                m = re.match(r"^- ([A-Z]{1,6}(?:\.[A-Z])?)\s+—", line)
                if m:
                    tickers.add(m.group(1))
    except Exception as e:
        log.warning("Could not read Stan's workspace for dynamic universe: %s", e)

    return sorted(tickers)


def get_stonks_universe() -> List[str]:
    """Stan's tracked universe, falling back to the old Kairos mega-cap list
    (TRACKED_SYMBOLS) only if his workspace is unreadable or empty."""
    universe = read_stonks_universe_from_workspace()
    return universe if universe else TRACKED_SYMBOLS

# ── Momentum computation ─────────────────────────────────────────────────────

def compute_momentum(symbols: List[str], days: int = 21) -> List[Dict[str, Any]]:
    """Compute momentum scores from data bus /bars.

    Uses rate-of-change over N days for cross-sectional ranking.
    Falls back to shorter lookback if insufficient data.
    """
    ranked = []
    end_date = datetime.now().strftime("%Y-%m-%d")
    # Fetch a wider window to ensure enough trading days
    start_date = (datetime.now() - timedelta(days=days + 20)).strftime("%Y-%m-%d")

    params = {
        "symbols": ",".join(symbols),
        "interval": "daily",
        "start_date": start_date,
        "end_date": end_date,
    }

    try:
        resp = requests.get(f"{DATA_BUS_URL}/bars", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        log.warning("Momentum /bars request failed: %s", e)
        return []

    bars_by_symbol = data.get("symbols", {})

    for sym in symbols:
        bars = bars_by_symbol.get(sym, [])
        if len(bars) < 5:
            continue

        closes = np.array([b["close"] for b in bars], dtype=np.float64)

        # Use available bars for ROC, with at most days lookback
        lookback = min(len(closes) - 1, days)
        if lookback < 1:
            continue
        roc = (closes[-1] - closes[-lookback - 1]) / closes[-lookback - 1] * 100

        # Simple volatility (standard deviation of daily returns)
        returns = np.diff(closes) / closes[:-1]
        vol_window = min(len(returns), 20)
        volatility = float(np.std(returns[-vol_window:])) * 100 if len(returns) > 1 else 0

        # RSI (14-day, fallback to shorter window)
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        rsi_window = min(len(gains), 14)
        avg_gain = np.mean(gains[-rsi_window:])
        avg_loss = np.mean(losses[-rsi_window:])
        rsi = 50.0
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        elif avg_gain > 0:
            rsi = 100.0

        ranked.append({
            "symbol": sym,
            "roc_pct": round(roc, 2),
            "rsi": round(rsi, 1),
            "volatility_pct": round(volatility, 2),
            "close": float(closes[-1]),
        })

    # Sort by ROC descending (momentum ranking)
    ranked.sort(key=lambda x: x["roc_pct"], reverse=True)

    # Z-score normalize
    if ranked:
        roc_values = np.array([r["roc_pct"] for r in ranked], dtype=np.float64)
        mean_roc = np.mean(roc_values)
        std_roc = np.std(roc_values) if np.std(roc_values) > 0 else 1.0
        for r in ranked:
            r["z_score"] = round((r["roc_pct"] - mean_roc) / std_roc, 2)

    return ranked


def determine_market_regime(ranked: List[Dict]) -> str:
    """Determine market regime from momentum distribution."""
    if not ranked:
        return "unknown"

    top_q = ranked[:max(1, len(ranked) // 4)]
    avg_top_z = np.mean([r.get("z_score", 0) for r in top_q])
    avg_rsi = np.mean([r.get("rsi", 50) for r in ranked])
    total_positive = sum(1 for r in ranked if r["roc_pct"] > 0)
    pct_positive = total_positive / len(ranked) * 100

    if avg_top_z > 1.0 and pct_positive > 70:
        return "strong_bull"
    elif avg_top_z > 0.5 and pct_positive > 50:
        return "bullish"
    elif avg_top_z < -0.5 and pct_positive < 30:
        return "bearish"
    elif avg_top_z < -1.0 and pct_positive < 20:
        return "strong_bear"
    elif avg_rsi > 65:
        return "overbought"
    elif avg_rsi < 35:
        return "oversold"
    elif pct_positive >= 40:
        return "neutral_bullish"
    elif pct_positive < 40:
        return "neutral_bearish"
    else:
        return "neutral"


def get_cached_momentum_signal(top_n: int = 10) -> Optional[Dict[str, Any]]:
    """Get momentum signal, cached for _cache_ttl seconds."""
    now = time.time()
    cache_key = "momentum_signal"

    if cache_key in _signal_cache:
        cached = _signal_cache[cache_key]
        if now - cached.get("_fetched_at", 0) < _cache_ttl:
            return cached

    ranked = compute_momentum(get_stonks_universe())
    if not ranked:
        return None

    market_regime = determine_market_regime(ranked)
    avg_composite_z = round(
        np.mean([r.get("z_score", 0) for r in ranked]), 2
    ) if ranked else 0.0

    signal = {
        "market_regime": market_regime,
        "num_ranked": len(ranked),
        "avg_composite_z": avg_composite_z,
        "ranked": ranked[:top_n],
        "_fetched_at": now,
    }

    _signal_cache[cache_key] = signal
    return signal


def clear_cache():
    """Clear the cached momentum signal."""
    _signal_cache.clear()


if __name__ == "__main__":
    signal = get_cached_momentum_signal(top_n=5)
    if signal:
        print(json.dumps(signal, indent=2, default=str))
    else:
        print("No momentum signal available")