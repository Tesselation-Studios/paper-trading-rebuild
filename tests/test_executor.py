"""Tests for executor module — bankroll gate."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.executor import gate_bankroll, execute_trade


# ═══════════════════════════════════════════════════════════════════════════════
# gate_bankroll tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestGateBankroll:
    """BankrollGate: validate BUY against ceiling, fail-open on missing data."""

    def test_non_buy_skipped(self):
        """Non-BUY actions should always pass."""
        action = {"type": "SELL", "ticker": "AAPL", "quantity": 100, "price": 150.0}
        context = {"portfolio_value": 10000, "cash": 5000}
        granted, reason = gate_bankroll(action, context)
        assert granted is True
        assert "skipped" in reason

    def test_fail_open_no_portfolio_value(self):
        """Missing portfolio_value should fail-open (skip gate)."""
        action = {"type": "BUY", "ticker": "AAPL", "quantity": 100, "price": 150.0}
        context = {"cash": 5000}  # No portfolio_value
        granted, reason = gate_bankroll(action, context)
        assert granted is True
        assert "fail-open" in reason

    def test_fail_open_zero_portfolio_value(self):
        """Zero portfolio_value should fail-open."""
        action = {"type": "BUY", "ticker": "AAPL", "quantity": 100, "price": 150.0}
        context = {"portfolio_value": 0, "cash": 5000}
        granted, reason = gate_bankroll(action, context)
        assert granted is True
        assert "fail-open" in reason

    def test_fail_open_no_bankroll_state(self):
        """No bankroll_state should fail-open."""
        action = {"type": "BUY", "ticker": "AAPL", "quantity": 100, "price": 150.0}
        context = {"portfolio_value": 10000, "cash": 5000}
        granted, reason = gate_bankroll(action, context, bankroll_state=None)
        assert granted is True
        assert "fail-open" in reason

    def test_fail_open_zero_ceiling(self):
        """Zero ceiling should fail-open."""
        action = {"type": "BUY", "ticker": "AAPL", "quantity": 100, "price": 150.0}
        context = {"portfolio_value": 10000, "cash": 5000}
        state = {"ceiling": 0}
        granted, reason = gate_bankroll(action, context, bankroll_state=state)
        assert granted is True
        assert "fail-open" in reason

    def test_zero_cost_trade(self):
        """Zero-cost trade should pass."""
        action = {"type": "BUY", "ticker": "AAPL", "quantity": 0, "price": 150.0}
        context = {"portfolio_value": 10000, "cash": 5000}
        state = {"ceiling": 1000}
        granted, reason = gate_bankroll(action, context, bankroll_state=state)
        assert granted is True

    def test_buy_within_ceiling(self):
        """BUY within ceiling should pass."""
        action = {"type": "BUY", "ticker": "AAPL", "quantity": 5, "price": 150.0}
        context = {"portfolio_value": 10000, "cash": 5000, "positions": []}
        state = {"ceiling": 2000}  # 5 * 150 = 750 < 2000
        granted, reason = gate_bankroll(action, context, bankroll_state=state)
        assert granted is True
        assert "OK" in reason

    def test_buy_exceeds_ceiling(self):
        """BUY exceeding ceiling should be rejected."""
        action = {"type": "BUY", "ticker": "AAPL", "quantity": 100, "price": 150.0}
        context = {"portfolio_value": 10000, "cash": 5000}
        state = {"ceiling": 500}  # 100 * 150 = 15000 > 500
        granted, reason = gate_bankroll(action, context, bankroll_state=state)
        assert granted is False
        assert "exceeds" in reason.lower() or "ceiling" in reason.lower()

    def test_buy_exceeds_remaining(self):
        """BUY that exceeds remaining ceiling should be rejected."""
        action = {"type": "BUY", "ticker": "AAPL", "quantity": 10, "price": 150.0}
        context = {
            "portfolio_value": 10000,
            "cash": 5000,
            "positions": [
                {"ticker": "MSFT", "market_value": 800},
            ],
        }
        state = {"ceiling": 1000}  # 800 deployed, 200 remaining, need 1500
        granted, reason = gate_bankroll(action, context, bankroll_state=state)
        assert granted is False
        # Cost (1500) exceeds ceiling (1000) directly — check for either tells
        assert "ceiling" in reason.lower() or "exceeds" in reason.lower()

    def test_buy_fits_remaining(self):
        """BUY that fits remaining ceiling should pass."""
        action = {"type": "BUY", "ticker": "AAPL", "quantity": 2, "price": 100.0}
        context = {
            "portfolio_value": 10000,
            "cash": 8000,
            "positions": [
                {"ticker": "MSFT", "market_value": 300},
            ],
        }
        state = {"ceiling": 1000}  # 300 deployed, 700 remaining, need 200
        granted, reason = gate_bankroll(action, context, bankroll_state=state)
        assert granted is True

    def test_action_type_uppercased(self):
        """Action type should be case-insensitive."""
        action = {"type": "buy", "ticker": "AAPL", "quantity": 5, "price": 150.0}
        context = {"portfolio_value": 10000, "cash": 8000, "positions": []}
        state = {"ceiling": 2000}
        granted, reason = gate_bankroll(action, context, bankroll_state=state)
        assert granted is True


# ═══════════════════════════════════════════════════════════════════════════════
# execute_trade tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestExecuteTrade:
    def test_buy_success(self):
        """Successful buy should return result dict."""
        action = {"type": "BUY", "ticker": "AAPL", "quantity": 10, "price": 150.0}
        context = {"portfolio_value": 10000, "cash": 5000, "positions": []}
        state = {"ceiling": 2000}
        success, reason, result = execute_trade(action, context, state)
        assert success is True
        assert result["action"] == "BUY"
        assert result["ticker"] == "AAPL"
        assert result["cost"] == 1500.0

    def test_buy_blocked(self):
        """Blocked buy should return failure."""
        action = {"type": "BUY", "ticker": "AAPL", "quantity": 100, "price": 150.0}
        context = {"portfolio_value": 10000, "cash": 5000}
        state = {"ceiling": 500}
        success, reason, result = execute_trade(action, context, state)
        assert success is False
        assert result is None

    def test_sell_bypasses_gate(self):
        """SELL should bypass bankroll gate."""
        action = {"type": "SELL", "ticker": "AAPL", "quantity": 10, "price": 150.0}
        context = {"portfolio_value": 10000, "cash": 5000}
        success, reason, result = execute_trade(action, context)
        assert success is True
        assert result["action"] == "SELL"
