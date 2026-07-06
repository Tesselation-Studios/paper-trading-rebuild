"""Aldridge Strategy — buy-and-hold value investing screen.

Implements the screening and rebalancing logic for Aldridge, a value-oriented
trader that selects stocks based on fundamental metrics.

Screening criteria (per SPEC §7.0):
  - P/E ratio: > 0 and < 20
  - Dividend yield: > 1%
  - Earnings growth: > 5%
  - Debt-to-equity: < 2.0

Usage:
    from src.aldridge_strategy import screen_candidates, weekly_rebalance
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.fundamentals import Fundamentals


# ── Screening parameters ─────────────────────────────────────────────────────


@dataclass
class ScreenParams:
    """Configurable screening thresholds for Aldridge."""

    pe_min: float = 0.0       # P/E must be positive
    pe_max: float = 20.0      # P/E upper bound
    div_min_pct: float = 1.0  # Dividend yield minimum (%)
    eg_min_pct: float = 5.0   # Earnings growth minimum (%)
    de_max: float = 2.0       # Debt-to-equity maximum


# ── Portfolio model ───────────────────────────────────────────────────────────


@dataclass
class Position:
    """A position in the Aldridge portfolio."""

    ticker: str
    shares: float = 0.0
    avg_cost: float = 0.0

    @property
    def value(self) -> float:
        return self.shares * self.avg_cost


@dataclass
class Portfolio:
    """Aldridge buy-and-hold portfolio."""

    cash: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)

    def tickers(self) -> List[str]:
        return list(self.positions.keys())


# ── Screener ──────────────────────────────────────────────────────────────────


def screen_candidates(
    fundamentals_list: List[Fundamentals],
    params: Optional[ScreenParams] = None,
) -> List[str]:
    """Screen a list of fundamentals and return tickers that pass all criteria.

    Criteria:
      - P/E > 0 and < 20
      - Dividend yield > 1%
      - Earnings growth > 5%
      - Debt-to-equity < 2.0

    Args:
        fundamentals_list: List of Fundamentals dataclasses to screen.
        params: Screening parameters (uses defaults if None).

    Returns:
        List of ticker symbols that pass all screening criteria.
    """
    if params is None:
        params = ScreenParams()

    candidates: List[str] = []

    for f in fundamentals_list:
        # P/E: must be positive and below max
        if f.pe_ratio is None or f.pe_ratio <= params.pe_min or f.pe_ratio >= params.pe_max:
            continue

        # Dividend yield: must be above minimum
        if f.dividend_yield is None or f.dividend_yield <= params.div_min_pct:
            continue

        # Earnings growth: must be above minimum
        if f.earnings_growth is None or f.earnings_growth <= params.eg_min_pct:
            continue

        # Debt-to-equity: must be below maximum
        if f.debt_to_equity is None or f.debt_to_equity >= params.de_max:
            continue

        candidates.append(f.ticker)

    return candidates


# ── Rebalancing ───────────────────────────────────────────────────────────────


def weekly_rebalance(
    portfolio: Portfolio,
    candidates: List[str],
) -> Dict[str, str]:
    """Determine which positions to hold or sell based on the current screen.

    Aldridge is buy-and-hold: tickers still in the screen are held,
    tickers that fell out of the screen are sold.

    Args:
        portfolio: Current portfolio state.
        candidates: List of tickers that currently pass the screen.

    Returns:
        Dict mapping ticker → action ('HOLD' or 'SELL').
    """
    candidate_set = set(candidates)
    actions: Dict[str, str] = {}

    for ticker in portfolio.tickers():
        if ticker in candidate_set:
            actions[ticker] = "HOLD"
        else:
            actions[ticker] = "SELL"

    return actions
