#!/usr/bin/env python3
"""
Unit tests for Virtual Trader Runner.
"""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.signals import SignalParams
import src.virtual_runner as vr


def test_is_market_hours():
    # Verify is_market_hours returns a boolean
    res = vr.is_market_hours()
    assert isinstance(res, bool)


def test_get_tracked_symbols():
    # Test fallback symbols when there is a connection error
    with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
        symbols = vr.get_tracked_symbols()
        assert len(symbols) > 0
        assert "SPY" in symbols
        assert "AAPL" in symbols


def test_offline_mode_behavior():
    # Set offline dynamic config override
    vr._config["mock"] = True

    # 1. Tracked symbols
    symbols = vr.get_tracked_symbols()
    assert symbols == ["SPY", "AAPL", "NVDA", "MSFT"]

    # 2. Fetch quotes
    quotes = vr.fetch_quotes(["SPY"])
    assert "SPY" in quotes
    assert quotes["SPY"]["price"] == 151.0

    # 3. Fetch momentum signals
    signals = vr.fetch_momentum_signals(["SPY"])
    assert "SPY" in signals
    assert signals["SPY"]["rsi"] == 52.0

    # 4. Load virtual traders
    vts = vr.load_virtual_traders()
    assert len(vts) == 2
    assert vts[0]["name"] == "kairos-looser"

    vts_filter = vr.load_virtual_traders(names=["trader-kairos"])
    assert len(vts_filter) == 1
    assert vts_filter[0]["name"] == "trader-kairos"

    # 5. Insert trade
    trade_id = vr.insert_trade(
        trader_id="test-trader",
        ticker="SPY",
        decision="BUY",
        conviction=0.8,
        rationale="Offline Test",
        price=151.0,
        trade_source="virtual"
    )
    assert trade_id.startswith("vt-mock-")

    # Reset config for other tests
    vr._config["mock"] = False
