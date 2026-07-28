"""Tests for bankroll module — equity-scaled ceiling + graduated sizing."""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bankroll import (
    STARTING_CEILING_PCT,
    FLOOR_PCT,
    MAX_CEILING_PCT,
    MIN_SAMPLES,
    BASE_MAX_POSITION_PCT,
    PROVEN_MAX_POSITION_PCT,
    recalc_ceiling,
    effective_ceiling,
    read_bankroll,
    write_bankroll,
    graduated_max_position_pct,
    write_max_position_pct_to_params,
    competition_multiplier,
    universe_max_price_for_ceiling,
    UNIVERSE_MAX_PRICE_TIERS,
    DEFAULT_BANKROLL_STATE,
)


# ═══════════════════════════════════════════════════════════════════════════════
# recalc_ceiling tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRecalcCeiling:
    def test_streak_zero_no_change(self):
        """No streak should leave ceiling_pct unchanged."""
        state = {"ceiling_pct": STARTING_CEILING_PCT, "streak": 0,
                 "peak_pct": STARTING_CEILING_PCT}
        result = recalc_ceiling(state)
        assert result["ceiling_pct"] == pytest.approx(STARTING_CEILING_PCT)

    def test_win_streak_expands(self):
        """Win streak should increase ceiling_pct by 2%."""
        state = {"ceiling_pct": STARTING_CEILING_PCT, "streak": 3,
                 "peak_pct": STARTING_CEILING_PCT}
        result = recalc_ceiling(state)
        expected = STARTING_CEILING_PCT * 1.02
        assert result["ceiling_pct"] == pytest.approx(expected)

    def test_loss_streak_contracts(self):
        """Loss streak should decrease ceiling_pct by 1%."""
        state = {"ceiling_pct": STARTING_CEILING_PCT, "streak": -2,
                 "peak_pct": STARTING_CEILING_PCT}
        result = recalc_ceiling(state)
        expected = STARTING_CEILING_PCT * 0.99
        assert result["ceiling_pct"] == pytest.approx(expected)

    def test_clamps_to_floor(self):
        """Ceiling_pct should not go below FLOOR_PCT."""
        state = {"ceiling_pct": FLOOR_PCT, "streak": -10,
                 "peak_pct": FLOOR_PCT}
        result = recalc_ceiling(state)
        assert result["ceiling_pct"] >= FLOOR_PCT
        assert result["ceiling_pct"] == pytest.approx(FLOOR_PCT)

    def test_clamps_to_max(self):
        """Ceiling_pct should not exceed MAX_CEILING_PCT."""
        state = {"ceiling_pct": MAX_CEILING_PCT, "streak": 100,
                 "peak_pct": MAX_CEILING_PCT}
        result = recalc_ceiling(state)
        assert result["ceiling_pct"] <= MAX_CEILING_PCT
        assert result["ceiling_pct"] == pytest.approx(MAX_CEILING_PCT)

    def test_win_streak_applies_multiplier_once_per_call(self):
        """recalc_ceiling applies 1.02 once per call regardless of streak magnitude."""
        state = {"ceiling_pct": STARTING_CEILING_PCT, "streak": 5,
                 "peak_pct": STARTING_CEILING_PCT}
        result = recalc_ceiling(state)
        # Each call applies 1.02 once, regardless of streak magnitude
        expected = STARTING_CEILING_PCT * 1.02
        assert result["ceiling_pct"] == pytest.approx(expected)

    def test_derived_ceiling_updated(self):
        """Derived 'ceiling' field should be updated at 10K nominal."""
        state = {"ceiling_pct": STARTING_CEILING_PCT, "streak": 2,
                 "peak_pct": STARTING_CEILING_PCT}
        result = recalc_ceiling(state)
        expected_pct = STARTING_CEILING_PCT * 1.02
        assert result["ceiling"] == pytest.approx(expected_pct * 10_000)

    def test_peak_pct_tracks(self):
        """Peak pct should track highest ceiling_pct."""
        state = {"ceiling_pct": 0.05, "streak": 5,
                 "peak_pct": 0.10}  # Already higher
        result = recalc_ceiling(state)
        assert result["peak_pct"] == pytest.approx(0.10)  # Should not decrease

        state2 = {"ceiling_pct": 0.05, "streak": 10,
                   "peak_pct": 0.04}
        result2 = recalc_ceiling(state2)
        # After 10 wins: 0.05 * 1.02^10 ≈ 0.061, peak should be that
        assert result2["peak_pct"] > 0.05


# ═══════════════════════════════════════════════════════════════════════════════
# effective_ceiling tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEffectiveCeiling:
    def test_basic_scaling(self):
        """Effective ceiling = ceiling_pct * equity * competition_multiplier."""
        state = {"ceiling_pct": STARTING_CEILING_PCT}
        equity = 10000.0
        result = effective_ceiling(state, equity)
        expected = STARTING_CEILING_PCT * equity * competition_multiplier()
        assert result == pytest.approx(expected)

    def test_hard_cap(self):
        """Should not exceed MAX_CEILING_PCT * equity."""
        state = {"ceiling_pct": 0.50}  # Above max
        equity = 10000.0
        result = effective_ceiling(state, equity)
        cap = MAX_CEILING_PCT * equity
        assert result <= cap

    def test_competition_multiplier_ramp(self):
        """Competition multiplier should increase over time."""
        early = datetime(2026, 8, 1)
        late = datetime(2026, 12, 1)
        mult_early = competition_multiplier(early)
        mult_late = competition_multiplier(late)
        assert mult_late > mult_early
        assert mult_early >= 1.0
        assert mult_late <= 1.3

    def test_competition_before_start(self):
        """Before competition start, multiplier = 1.0."""
        date = datetime(2026, 7, 1)
        assert competition_multiplier(date) == pytest.approx(1.0)

    def test_competition_after_end(self):
        """After competition end, multiplier = 1.0."""
        date = datetime(2027, 1, 1)
        assert competition_multiplier(date) == pytest.approx(1.0)

    def test_equity_scaling_large(self):
        """Larger equity should yield higher ceiling."""
        state = {"ceiling_pct": STARTING_CEILING_PCT}
        small = effective_ceiling(state, 5000)
        large = effective_ceiling(state, 50000)
        assert large > small
        # Ratio should be roughly 10x
        assert large / small == pytest.approx(10.0, rel=0.02)


# ═══════════════════════════════════════════════════════════════════════════════
# graduated_max_position_pct tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestGraduatedSizing:
    def test_below_min_samples(self):
        """Below MIN_SAMPLES, should return BASE_MAX_POSITION_PCT."""
        trades = [{"pnl": 100}] * 15  # Only 15 trades
        result = graduated_max_position_pct(closed_trades=trades)
        assert result == pytest.approx(BASE_MAX_POSITION_PCT)

    def test_above_min_high_winrate(self):
        """Above MIN_SAMPLES with >55% WR, should return PROVEN_MAX."""
        trades = [{"pnl": 100}] * 14 + [{"pnl": -50}] * 6  # 20 trades, 70% WR
        result = graduated_max_position_pct(closed_trades=trades)
        assert result == pytest.approx(PROVEN_MAX_POSITION_PCT)

    def test_above_min_low_winrate(self):
        """Above MIN_SAMPLES with <45% WR, should return BASE."""
        trades = [{"pnl": 100}] * 7 + [{"pnl": -50}] * 13  # 20 trades, 35% WR
        result = graduated_max_position_pct(closed_trades=trades)
        assert result == pytest.approx(BASE_MAX_POSITION_PCT)

    def test_interpolation_mid(self):
        """45-55% WR should interpolate linearly."""
        # Exactly 50% WR → halfway between BASE and PROVEN
        wr = 0.50
        result = graduated_max_position_pct(win_rate=wr, n_trades=25)
        expected_mid = (BASE_MAX_POSITION_PCT + PROVEN_MAX_POSITION_PCT) / 2
        assert result == pytest.approx(expected_mid)

    def test_interpolation_near_upper(self):
        """52% WR → ~12.0% (60% of the way from 6 to 15)."""
        wr = 0.52
        result = graduated_max_position_pct(win_rate=wr, n_trades=25)
        expected = BASE_MAX_POSITION_PCT + 0.7 * (PROVEN_MAX_POSITION_PCT - BASE_MAX_POSITION_PCT)
        assert result == pytest.approx(expected)

    def test_empty_trades(self):
        """Empty trades list with n_trades=0 should return BASE."""
        result = graduated_max_position_pct(closed_trades=[], win_rate=0.0, n_trades=0)
        assert result == pytest.approx(BASE_MAX_POSITION_PCT)

    def test_exact_55_pct(self):
        """Exactly 55% should return PROVEN."""
        wr = 0.55
        result = graduated_max_position_pct(win_rate=wr, n_trades=25)
        assert result == pytest.approx(PROVEN_MAX_POSITION_PCT)

    def test_exact_45_pct(self):
        """Exactly 45% should return BASE."""
        wr = 0.45
        result = graduated_max_position_pct(win_rate=wr, n_trades=25)
        assert result == pytest.approx(BASE_MAX_POSITION_PCT)


# ═══════════════════════════════════════════════════════════════════════════════
# read_bankroll / write_bankroll tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBankrollIO:
    def test_read_nonexistent_returns_default(self):
        """Reading a nonexistent file should return default state."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        os.unlink(path)  # Remove it so it doesn't exist
        try:
            state = read_bankroll(path)
            assert state["ceiling_pct"] == pytest.approx(STARTING_CEILING_PCT)
            assert state["wins"] == 0
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_write_then_read(self):
        """Write then read should round-trip faithfully."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            state = {
                "ceiling_pct": 0.10,
                "ceiling": 1000.0,
                "wins": 5,
                "losses": 3,
                "streak": 2,
                "peak_pct": 0.12,
            }
            write_bankroll(path, state)
            loaded = read_bankroll(path)
            assert loaded["ceiling_pct"] == pytest.approx(0.10)
            assert loaded["wins"] == 5
            assert loaded["streak"] == 2
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_write_ensures_ceiling_pct(self):
        """Write should ensure ceiling_pct field is present."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            state = {"wins": 3}  # Missing ceiling_pct
            write_bankroll(path, state)
            loaded = read_bankroll(path)
            assert "ceiling_pct" in loaded
            assert loaded["ceiling_pct"] == pytest.approx(STARTING_CEILING_PCT)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_read_old_format(self):
        """Old format (ceiling-only, no ceiling_pct) should migrate."""
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"ceiling": 500.0, "wins": 2, "losses": 1}, f)
            path = f.name
        try:
            state = read_bankroll(path)
            assert "ceiling_pct" in state
            assert state["ceiling_pct"] == pytest.approx(500.0 / 10_000)
            assert state["wins"] == 2
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_creates_directory(self):
        """Write should create parent directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "subdir", "bankroll.json")
            state = {"ceiling_pct": 0.067}
            write_bankroll(path, state)
            assert os.path.exists(path)
            loaded = read_bankroll(path)
            assert loaded["ceiling_pct"] == pytest.approx(0.067)


# ═══════════════════════════════════════════════════════════════════════════════
# write_max_position_pct_to_params tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWriteMaxPositionPct:
    def test_writes_to_params(self):
        """Should write max_position_pct into params.json."""
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"risk": {"existing_key": 42}}, f)
            path = f.name
        try:
            write_max_position_pct_to_params(12.0, path)
            with open(path) as f:
                data = json.load(f)
            assert data["risk"]["max_position_pct"] == 12.0
            assert data["risk"]["existing_key"] == 42  # Preserved
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_creates_params_if_missing(self):
        """Should create params.json if it doesn't exist."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        os.unlink(path)  # Remove so it doesn't exist
        try:
            write_max_position_pct_to_params(15.0, path)
            with open(path) as f:
                data = json.load(f)
            assert data["risk"]["max_position_pct"] == 15.0
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_creates_risk_section(self):
        """Should create risk section if missing."""
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"other": "value"}, f)
            path = f.name
        try:
            write_max_position_pct_to_params(8.0, path)
            with open(path) as f:
                data = json.load(f)
            assert "risk" in data
            assert data["risk"]["max_position_pct"] == 8.0
            assert data["other"] == "value"
        finally:
            if os.path.exists(path):
                os.unlink(path)


# ═══════════════════════════════════════════════════════════════════════════════
# universe_max_price_for_ceiling tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestUniverseMaxPrice:
    def test_starting_tier(self):
        """At STARTING_CEILING_PCT, should return $100 * equity factor."""
        result = universe_max_price_for_ceiling(STARTING_CEILING_PCT, 10000)
        assert result == pytest.approx(100.0)

    def test_low_ceiling(self):
        """At low ceiling_pct, should return lower price."""
        result = universe_max_price_for_ceiling(0.02, 10000)
        assert result < 100.0

    def test_high_ceiling(self):
        """At high ceiling_pct, should return higher price."""
        result = universe_max_price_for_ceiling(0.15, 10000)
        assert result > 100.0

    def test_equity_scaling(self):
        """Higher equity should increase max price."""
        small = universe_max_price_for_ceiling(STARTING_CEILING_PCT, 10000)
        large = universe_max_price_for_ceiling(STARTING_CEILING_PCT, 100000)
        assert large > small

    def test_hard_cap(self):
        """Should never exceed 500.0."""
        result = universe_max_price_for_ceiling(0.19, 1000000)
        assert result <= 500.0
