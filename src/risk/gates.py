#!/usr/bin/env python3
"""
Composable risk gates — pure functions for pre-trade validation.

Each gate is a class implementing:
    check(context, action, timestamp=None) -> (granted: bool, reason: str)

Where:
    - context: dict with portfolio state (portfolio_value, cash, positions, etc.)
    - action: dict with trade details (type, ticker, quantity, price)
    - timestamp: optional datetime for historical replay

Returns True=granted (trade is allowed) + human-readable reason.
All gates are stateless — no DB reads, no side effects, no network calls.
"""

from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, Optional, List


class CashGate:
    """Reject trades that would spend more than available cash.

    Config key: None (uses context["cash"] directly)
    """

    def check(
        self,
        context: Dict[str, Any],
        action: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """BUY actions must not exceed available cash."""

        action_type = str(action.get("type", action.get("action", ""))).upper()
        if action_type != "BUY":
            return True, "CashGate: non-BUY action, skipped"

        ticker = str(action.get("ticker", "")).upper()
        quantity = float(action.get("quantity", 0) or 0)
        price = float(action.get("price", action.get("current_price", 0)) or 0)
        cost = quantity * price

        if cost <= 0:
            return True, "CashGate: zero-cost trade, granted"

        cash = float(context.get("cash", 0) or 0)

        if cost > cash:
            return False, (
                f"CashGate: BUY {ticker} costs ${cost:,.2f} "
                f"but only ${cash:,.2f} cash available"
            )

        return True, (
            f"CashGate: BUY {ticker} costs ${cost:,.2f}, "
            f"cash ${cash:,.2f} sufficient"
        )


class PositionGate:
    """Reject trades that would make a single position exceed max_position_pct
    of the total portfolio value.

    Config keys:
        max_position_pct: float (e.g., 0.20 for 20%)
    """

    def __init__(self, max_position_pct: float = 0.20):
        self.max_position_pct = max_position_pct

    def check(
        self,
        context: Dict[str, Any],
        action: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """Ensure no single position exceeds max_position_pct of portfolio."""

        action_type = str(action.get("type", action.get("action", ""))).upper()
        if action_type != "BUY":
            return True, "PositionGate: non-BUY action, skipped"

        ticker = str(action.get("ticker", "")).upper()
        quantity = float(action.get("quantity", 0) or 0)
        price = float(action.get("price", action.get("current_price", 0)) or 0)
        proposed_value = quantity * price

        if proposed_value <= 0:
            return True, "PositionGate: zero-value trade, granted"

        portfolio_value = float(context.get("portfolio_value", 0) or 0)
        if portfolio_value <= 0:
            return True, "PositionGate: no portfolio value, granted"

        # Calculate existing position value for this ticker
        existing_value = 0.0
        positions = context.get("positions", []) or []
        for pos in positions:
            pos_ticker = str(pos.get("ticker", "")).upper()
            if pos_ticker == ticker:
                existing_value += float(
                    pos.get("market_value", 0)
                    or (float(pos.get("quantity", 0) or 0) * price)
                )

        total_position_value = existing_value + proposed_value
        position_pct = total_position_value / portfolio_value

        if position_pct > self.max_position_pct:
            return False, (
                f"PositionGate: {ticker} would be {position_pct:.1%} of portfolio "
                f"(${total_position_value:,.2f} / ${portfolio_value:,.2f}), "
                f"exceeds {self.max_position_pct:.0%} cap "
                f"(existing: ${existing_value:,.2f}, proposed: ${proposed_value:,.2f})"
            )

        return True, (
            f"PositionGate: {ticker} at {position_pct:.1%} of portfolio, "
            f"within {self.max_position_pct:.0%} cap"
        )


class ExposureGate:
    """Reject trades that would push total exposure over max_exposure_pct
    of the portfolio value.

    Config keys:
        max_exposure_pct: float (e.g., 1.00 for 100%)
    """

    def __init__(self, max_exposure_pct: float = 1.00):
        self.max_exposure_pct = max_exposure_pct

    def check(
        self,
        context: Dict[str, Any],
        action: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """Ensure total exposure (all positions + proposed) <= max_exposure_pct."""

        action_type = str(action.get("type", action.get("action", ""))).upper()
        ticker = str(action.get("ticker", "")).upper()

        portfolio_value = float(context.get("portfolio_value", 0) or 0)
        if portfolio_value <= 0:
            return True, "ExposureGate: no portfolio value, granted"

        # Sum existing position values
        positions = context.get("positions", []) or []
        existing_exposure = 0.0
        for pos in positions:
            existing_exposure += float(
                pos.get("market_value", 0)
                or (float(pos.get("quantity", 0) or 0) * float(pos.get("avg_entry_price", 0) or 0))
            )

        # Calculate proposed value
        proposed_value = 0.0
        if action_type == "BUY":
            quantity = float(action.get("quantity", 0) or 0)
            price = float(action.get("price", action.get("current_price", 0)) or 0)
            proposed_value = quantity * price
        elif action_type == "SELL":
            # SELL reduces exposure — always allowed from exposure perspective
            return True, (
                f"ExposureGate: SELL {ticker} reduces exposure, granted"
            )

        total_exposure = existing_exposure + proposed_value
        exposure_pct = total_exposure / portfolio_value

        if exposure_pct > self.max_exposure_pct:
            return False, (
                f"ExposureGate: total exposure would be {exposure_pct:.1%} "
                f"(${total_exposure:,.2f} / ${portfolio_value:,.2f}), "
                f"exceeds {self.max_exposure_pct:.0%} cap "
                f"(existing: ${existing_exposure:,.2f}, proposed: ${proposed_value:,.2f})"
            )

        return True, (
            f"ExposureGate: total exposure {exposure_pct:.1%}, "
            f"within {self.max_exposure_pct:.0%} cap"
        )


class PDTGate:
    """Pattern Day Trader rule: ≤ N day trades in a rolling 5-trading-day window.

    A day trade is defined as buying and selling (or selling short and buying
    to cover) the same security on the same trading day.

    Config keys:
        pdt_day_trade_limit: int (default 3)
        pdt_window_days: int (default 5)
    """

    def __init__(self, pdt_day_trade_limit: int = 3, pdt_window_days: int = 5):
        self.pdt_day_trade_limit = pdt_day_trade_limit
        self.pdt_window_days = pdt_window_days

    def _count_day_trades_in_window(
        self,
        day_trades: List[Dict[str, Any]],
        now: datetime,
    ) -> int:
        """Count day trades within the rolling window ending at `now`.

        Each day_trade entry should have at minimum a 'timestamp' field
        (ISO string or datetime).
        """
        cutoff = now - timedelta(days=self.pdt_window_days)
        count = 0

        for dt_entry in (day_trades or []):
            ts = dt_entry.get("timestamp")
            if ts is None:
                continue

            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts)
                except (ValueError, TypeError):
                    continue

            if isinstance(ts, datetime) and ts >= cutoff and ts <= now:
                count += 1

        return count

    def check(
        self,
        context: Dict[str, Any],
        action: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """Reject BUY if it would cause a day trade exceeding the PDT limit."""

        action_type = str(action.get("type", action.get("action", ""))).upper()
        if action_type != "BUY":
            return True, "PDTGate: non-BUY action, skipped"

        now = timestamp or datetime.now()

        day_trades = context.get("day_trades", []) or []
        current_count = self._count_day_trades_in_window(day_trades, now)

        # Check if this BUY would close an intraday round-trip (day trade)
        ticker = str(action.get("ticker", "")).upper()
        would_be_day_trade = False

        # Look for a SELL of the same ticker today that this BUY completes
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for dt_entry in day_trades:
            entry_ts = dt_entry.get("timestamp")
            if isinstance(entry_ts, str):
                try:
                    entry_ts = datetime.fromisoformat(entry_ts)
                except (ValueError, TypeError):
                    continue
            if isinstance(entry_ts, datetime) and entry_ts >= today_start:
                if str(dt_entry.get("ticker", "")).upper() == ticker:
                    would_be_day_trade = True
                    break

        new_count = current_count + (1 if would_be_day_trade else 0)

        # The BUY itself doesn't create a day trade (need both buy and sell).
        # A day trade is triggered on the closing SELL, not the opening BUY.
        # However, if the action marks itself as completing a day trade,
        # we count it.
        completes_day_trade = bool(action.get("completes_day_trade", False))

        if completes_day_trade:
            new_count = current_count + 1

        if new_count > self.pdt_day_trade_limit:
            return False, (
                f"PDTGate: {new_count} day trade(s) would exceed "
                f"{self.pdt_day_trade_limit} limit in {self.pdt_window_days}-day window "
                f"(current: {current_count})"
            )

        return True, (
            f"PDTGate: {current_count}/{self.pdt_day_trade_limit} day trades "
            f"in {self.pdt_window_days}-day window"
        )


class ConvictionGate:
    """Validate BUY entries against minimum conviction threshold.

    SELL and HOLD actions are always allowed — this gate only validates
    entry quality. Per SPEC §3 and risk gate invariants, exiting a position
    must not be blocked by entry-quality checks.

    This is the upstream fix for the signal_scores gate that was blocking
    sell orders on the OpenClaw side. BUY-only gate, composable into the
    RiskManager chain.

    Config keys:
        min_conviction: float (default 0.3)
    """

    def __init__(self, min_conviction: float = 0.3):
        self.min_conviction = min_conviction

    def check(
        self,
        context: Dict[str, Any],
        action: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """Only gate BUY. SELL and HOLD always pass."""
        action_type = str(action.get("type", action.get("action", ""))).upper()
        if action_type != "BUY":
            return True, f"ConvictionGate: non-BUY ({action_type}), skipped"

        # If conviction not in action, pass through — this gate validates
        # conviction quality, not data pipeline completeness
        if "conviction" not in action:
            return True, "ConvictionGate: no conviction data, passed"

        conviction = float(action.get("conviction", 0) or 0)
        ticker = str(action.get("ticker", "")).upper()

        if conviction < self.min_conviction:
            return False, (
                f"ConvictionGate: BUY {ticker} conviction {conviction:.2f} "
                f"below minimum {self.min_conviction}"
            )

        return True, (
            f"ConvictionGate: BUY {ticker} conviction {conviction:.2f} "
            f">= {self.min_conviction}"
        )


class HoursGate:
    """Only allow trading during regular market hours: 09:30–16:00 Eastern Time,
    Monday through Friday.

    No config keys needed — uses timestamp parameter or current time.

    Accepts optional `timestamp` parameter for historical replay/testing.
    If no timestamp provided, uses datetime.now().
    """

    # Market hours in Eastern Time (ET)
    MARKET_OPEN_HOUR = 9
    MARKET_OPEN_MINUTE = 30
    MARKET_CLOSE_HOUR = 16
    MARKET_CLOSE_MINUTE = 0

    @staticmethod
    def _is_market_open(now: datetime) -> Tuple[bool, str]:
        """Check if the given datetime falls within market hours (09:30–16:00 ET,
        Monday–Friday).

        NOTE: The timestamp is assumed to be in Eastern Time. The caller
        is responsible for timezone conversion if needed.
        """
        # Check weekday (Monday=0, Sunday=6)
        if now.weekday() >= 5:
            day_name = now.strftime("%A")
            return False, f"HoursGate: {day_name} — market closed on weekends"

        # Check hours
        hour = now.hour
        minute = now.minute

        # Before market open
        if hour < HoursGate.MARKET_OPEN_HOUR or (
            hour == HoursGate.MARKET_OPEN_HOUR
            and minute < HoursGate.MARKET_OPEN_MINUTE
        ):
            return False, (
                f"HoursGate: {now.strftime('%H:%M')} ET — "
                f"market opens at 09:30 ET"
            )

        # After market close
        if hour > HoursGate.MARKET_CLOSE_HOUR or (
            hour == HoursGate.MARKET_CLOSE_HOUR
            and minute > HoursGate.MARKET_CLOSE_MINUTE
        ):
            return False, (
                f"HoursGate: {now.strftime('%H:%M')} ET — "
                f"market closed at 16:00 ET"
            )

        return True, f"HoursGate: {now.strftime('%H:%M')} ET — market open"

    def check(
        self,
        context: Dict[str, Any],
        action: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """Reject any action outside regular market hours."""

        now = timestamp or datetime.now()
        is_open, reason = self._is_market_open(now)
        return is_open, reason
