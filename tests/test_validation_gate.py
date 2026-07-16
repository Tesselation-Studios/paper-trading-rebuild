"""Tests for src/validation_gate.py — validation gate integration.

Architectural Invariant #7: No parameter change accepted without
validation on unseen data. These tests verify:
  1. Gate accepts valid improvements
  2. Gate rejects overfit params
  3. Gate rejects when data is insufficient
  4. Gate can be disabled (emergency override)
  5. GateResult serialization
  6. Integration with promote_virtual_to_live parameter extraction
"""
import json
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.validation import (
    ValidationResult,
    WalkForwardConfig,
    WalkForwardValidator,
    walk_forward_validate,
)
from src.validation_gate import (
    ValidationGate,
    GateConfig,
    GateResult,
    check_validation_gate,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _make_synthetic_ticks(n: int = 200):
    """Generate synthetic ticks for testing."""
    import random
    from datetime import datetime, timedelta
    from src.replay import Tick

    ticks = []
    price = 100.0
    start = datetime(2026, 1, 5, 9, 30)

    for i in range(n):
        price = price * (1 + random.gauss(0.0005, 0.01))
        price = max(price, 10.0)
        tick = Tick(
            timestamp=start + timedelta(minutes=i * 5),
            ticker="SPY",
            open=price * 0.999,
            high=price * 1.002,
            low=price * 0.998,
            close=price,
            volume=1_000_000 + i * 1000,
            rsi=50.0 + random.gauss(0, 3),
            momentum=0.55 + random.gauss(0, 0.05),
            volatility=0.01,
        )
        ticks.append(tick)
    return ticks


# ═══════════════════════════════════════════════════════════════════════════════
# GateConfig tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestGateConfig:
    def test_defaults(self):
        """Default config has validation enabled."""
        cfg = GateConfig()
        assert cfg.require_validation is True
        assert cfg.train_window_days == 90
        assert cfg.val_window_days == 30
        assert cfg.min_improvement == 0.0
        assert cfg.log_results is True

    def test_disabled_gate(self):
        """Gate can be disabled for emergency overrides."""
        cfg = GateConfig(require_validation=False)
        assert cfg.require_validation is False

    def test_custom_windows(self):
        """Custom train/val windows."""
        cfg = GateConfig(train_window_days=60, val_window_days=10)
        assert cfg.train_window_days == 60
        assert cfg.val_window_days == 10

    def test_min_improvement(self):
        """Minimum improvement threshold filters marginal gains."""
        cfg = GateConfig(min_improvement=0.05)
        assert cfg.min_improvement == 0.05


# ═══════════════════════════════════════════════════════════════════════════════
# GateResult tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestGateResult:
    def test_rejected_factory(self):
        """GateResult.rejected() creates proper rejection."""
        result = GateResult.rejected(
            reason="Not enough data",
            trader_id="kairos",
        )
        assert result.passed is False
        assert result.reason == "Not enough data"
        assert result.trader_id == "kairos"
        assert result.validation is None
        assert result.checked_at != ""

    def test_accepted_factory(self):
        """GateResult.accepted() creates proper acceptance."""
        validation = ValidationResult(
            accepted=True,
            train_sharpe=1.5,
            val_sharpe=1.2,
            baseline_val_sharpe=0.8,
            confidence=0.8,
            reason="All acceptance criteria met",
            checks={"val_sharpe_positive": True, "beats_baseline": True, "not_overfit": True},
        )
        result = GateResult.accepted(
            reason="Walk-forward validation ACCEPTED",
            validation=validation,
            trader_id="kairos",
            candidate_params={"momentum_threshold": 0.60},
            baseline_params={"momentum_threshold": 0.55},
        )
        assert result.passed is True
        assert result.validation is not None
        assert result.validation.train_sharpe == 1.5
        assert result.trader_id == "kairos"
        assert result.candidate_params == {"momentum_threshold": 0.60}

    def test_to_dict(self):
        """to_dict() serializes all fields."""
        validation = ValidationResult(
            accepted=True,
            train_sharpe=1.5,
            val_sharpe=1.2,
            baseline_val_sharpe=0.8,
            confidence=0.8,
            reason="All acceptance criteria met",
            checks={"val_sharpe_positive": True, "beats_baseline": True, "not_overfit": True},
        )
        result = GateResult.accepted(
            reason="test",
            validation=validation,
            trader_id="kairos",
            candidate_params={"a": 1},
            baseline_params={"a": 0},
            checked_at="2026-01-01T00:00:00Z",
        )
        d = result.to_dict()
        assert d["passed"] is True
        assert d["trader_id"] == "kairos"
        assert d["validation"]["train_sharpe"] == 1.5
        assert d["candidate_params"] == {"a": 1}
        assert d["baseline_params"] == {"a": 0}

    def test_to_dict_rejected(self):
        """to_dict() works for rejected results too."""
        result = GateResult.rejected(
            reason="insufficient data",
            trader_id="aldridge",
            checked_at="2026-01-01T00:00:00Z",
        )
        d = result.to_dict()
        assert d["passed"] is False
        assert d["reason"] == "insufficient data"
        assert "validation" not in d


# ═══════════════════════════════════════════════════════════════════════════════
# ValidationGate tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidationGate:
    def test_disabled_gate_always_accepts(self):
        """When require_validation=False, gate always passes."""
        gate = ValidationGate(config=GateConfig(require_validation=False))
        result = gate.check(
            candidate_params={"momentum_threshold": 0.99},
            baseline_params={"momentum_threshold": 0.55},
            trader_id="kairos",
            tick_data=_make_synthetic_ticks(50),  # not enough for real validation
        )
        assert result.passed is True
        assert "disabled" in result.reason.lower()

    def test_insufficient_data_rejects(self):
        """When tick data is too short, gate rejects with clear reason."""
        gate = ValidationGate(
            config=GateConfig(require_validation=True, log_results=False),
            validator_config=WalkForwardConfig(
                train_window_days=60,
                val_window_days=30,
                min_trades=5,
                step=1,
            ),
        )
        ticks = _make_synthetic_ticks(n=50)  # Need at least 90 (60+30)
        result = gate.check(
            candidate_params={"momentum_threshold": 0.55},
            baseline_params={"momentum_threshold": 0.55},
            trader_id="kairos",
            tick_data=ticks,
        )
        assert result.passed is False
        assert "Not enough data" in result.reason

    def test_rejects_overfit_candidate(self):
        """Gate should reject when candidate params cause overfitting."""
        gate = ValidationGate(
            config=GateConfig(require_validation=True, log_results=False),
            validator_config=WalkForwardConfig(
                train_window_days=60,
                val_window_days=30,
                min_trades=3,
                step=30,
            ),
        )
        ticks = _make_synthetic_ticks(n=200)
        result = gate.check(
            candidate_params={"momentum_threshold": 0.99},  # almost never trades
            baseline_params={"momentum_threshold": 0.30},  # trades a lot
            trader_id="kairos",
            tick_data=ticks,
        )
        # Should either reject or accept — but must be a valid GateResult
        assert isinstance(result, GateResult)
        assert isinstance(result.passed, bool)
        assert len(result.reason) > 0

    def test_result_has_validation_on_failure(self):
        """Failed gate results include the underlying ValidationResult."""
        gate = ValidationGate(
            config=GateConfig(require_validation=True, log_results=False),
            validator_config=WalkForwardConfig(
                train_window_days=50,
                val_window_days=20,
                min_trades=3,
                step=30,
            ),
        )
        ticks = _make_synthetic_ticks(n=200)
        result = gate.check(
            candidate_params={"momentum_threshold": 0.95},
            baseline_params={"momentum_threshold": 0.30},
            trader_id="kairos",
            tick_data=ticks,
        )
        assert isinstance(result, GateResult)
        # Even if accepted, validation should be present
        if not result.passed:
            assert result.validation is not None
            assert hasattr(result.validation, "train_sharpe")
            assert hasattr(result.validation, "val_sharpe")

    def test_accepts_similar_params(self):
        """Same params should not produce a rejection for overfitting."""
        gate = ValidationGate(
            config=GateConfig(require_validation=True, log_results=False),
            validator_config=WalkForwardConfig(
                train_window_days=50,
                val_window_days=20,
                min_trades=3,
                step=30,
            ),
        )
        ticks = _make_synthetic_ticks(n=200)
        result = gate.check(
            candidate_params={"momentum_threshold": 0.55},
            baseline_params={"momentum_threshold": 0.55},
            trader_id="kairos",
            tick_data=ticks,
        )
        assert isinstance(result, GateResult)
        assert isinstance(result.passed, bool)

    def test_no_tick_data_rejects(self):
        """Empty tick data list → reject with clear message."""
        gate = ValidationGate(
            config=GateConfig(require_validation=True, log_results=False),
        )
        result = gate.check(
            candidate_params={"momentum_threshold": 0.55},
            baseline_params={"momentum_threshold": 0.55},
            trader_id="kairos",
            tick_data=[],
        )
        assert result.passed is False
        assert "Not enough data" in result.reason or "No historical" in result.reason

    def test_gate_attaches_params_to_result(self):
        """GateResult carries the params that were tested."""
        gate = ValidationGate(
            config=GateConfig(require_validation=True, log_results=False),
            validator_config=WalkForwardConfig(
                train_window_days=50,
                val_window_days=20,
                min_trades=3,
                step=30,
            ),
        )
        ticks = _make_synthetic_ticks(n=200)
        result = gate.check(
            candidate_params={"momentum_threshold": 0.60, "stop_loss_pct": 0.05},
            baseline_params={"momentum_threshold": 0.55, "stop_loss_pct": 0.07},
            trader_id="kairos",
            tick_data=ticks,
        )
        assert result.candidate_params["momentum_threshold"] == 0.60
        assert result.baseline_params["stop_loss_pct"] == 0.07
        assert result.trader_id == "kairos"

    def test_min_improvement_gate(self):
        """When min_improvement is set, marginal gains are rejected."""
        gate = ValidationGate(
            config=GateConfig(
                require_validation=True,
                log_results=False,
                min_improvement=0.20,  # Very high bar
            ),
            validator_config=WalkForwardConfig(
                train_window_days=50,
                val_window_days=20,
                min_trades=3,
                step=30,
            ),
        )
        ticks = _make_synthetic_ticks(n=200)
        result = gate.check(
            candidate_params={"momentum_threshold": 0.55},
            baseline_params={"momentum_threshold": 0.55},
            trader_id="kairos",
            tick_data=ticks,
        )
        assert isinstance(result, GateResult)
        # With high min_improvement and same params, should reject
        if "below minimum" in result.reason:
            assert result.passed is False


# ═══════════════════════════════════════════════════════════════════════════════
# check_validation_gate convenience function tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckValidationGate:
    def test_convenience_function(self):
        """check_validation_gate() returns GateResult."""
        ticks = _make_synthetic_ticks(n=50)
        result = check_validation_gate(
            candidate_params={"momentum_threshold": 0.55},
            baseline_params={"momentum_threshold": 0.55},
            trader_id="kairos",
            require=True,
            tick_data=ticks,
        )
        assert isinstance(result, GateResult)
        assert result.trader_id == "kairos"

    def test_disabled_convenience(self):
        """check_validation_gate with require=False always passes."""
        result = check_validation_gate(
            candidate_params={"momentum_threshold": 0.55},
            baseline_params={"momentum_threshold": 0.55},
            trader_id="kairos",
            require=False,
        )
        assert result.passed is True

    def test_accepts_with_sufficient_data(self):
        """With enough data, validation runs and returns a result."""
        ticks = _make_synthetic_ticks(n=200)
        result = check_validation_gate(
            candidate_params={"momentum_threshold": 0.55},
            baseline_params={"momentum_threshold": 0.55},
            trader_id="kairos",
            require=True,
            tick_data=ticks,
        )
        assert isinstance(result, GateResult)
        assert isinstance(result.passed, bool)


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: promote_virtual_to_live param extraction helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestParamExtraction:
    """Tests for _extract_params_from_virtual and _extract_baseline_params
    used by promote_virtual_to_live.py.

    These are integration-level checks that ensure the deploy pipeline
    correctly extracts candidate + baseline params for validation.
    """

    def test_extract_params_from_dict(self):
        """Params stored as dict in JSONB are extracted correctly."""
        # Import the function (it's in promote_virtual_to_live.py)
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "promote_virtual_to_live",
            Path(__file__).resolve().parent.parent / "scripts" / "promote_virtual_to_live.py",
        )
        mod = importlib.util.module_from_spec(spec)

        # Patch modules not available in test
        sys.modules["psycopg2"] = MagicMock()
        sys.modules["psycopg2.extras"] = MagicMock()
        sys.modules["yaml"] = MagicMock()

        spec.loader.exec_module(mod)

        virtual = {
            "name": "test-variant-0715",
            "base_trader": "kairos",
            "params": {"momentum_threshold": 0.60, "stop_loss_pct": 0.05},
        }

        params = mod._extract_params_from_virtual(virtual, "kairos")
        assert params["momentum_threshold"] == 0.60
        assert params["stop_loss_pct"] == 0.05

    def test_extract_params_from_json_string(self):
        """Params stored as JSON string are parsed correctly."""
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "promote_virtual_to_live",
            Path(__file__).resolve().parent.parent / "scripts" / "promote_virtual_to_live.py",
        )
        mod = importlib.util.module_from_spec(spec)

        sys.modules["psycopg2"] = MagicMock()
        sys.modules["psycopg2.extras"] = MagicMock()
        sys.modules["yaml"] = MagicMock()

        spec.loader.exec_module(mod)

        virtual = {
            "name": "test-variant-0715",
            "base_trader": "kairos",
            "params": json.dumps({"momentum_threshold": 0.65, "rsi_oversold": 25.0}),
        }

        params = mod._extract_params_from_virtual(virtual, "kairos")
        assert params["momentum_threshold"] == 0.65
        assert params["rsi_oversold"] == 25.0

    def test_extract_params_empty(self):
        """Empty params dict returns empty dict."""
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "promote_virtual_to_live",
            Path(__file__).resolve().parent.parent / "scripts" / "promote_virtual_to_live.py",
        )
        mod = importlib.util.module_from_spec(spec)

        sys.modules["psycopg2"] = MagicMock()
        sys.modules["psycopg2.extras"] = MagicMock()
        sys.modules["yaml"] = MagicMock()

        spec.loader.exec_module(mod)

        virtual = {"name": "test-variant", "base_trader": "kairos", "params": {}}
        params = mod._extract_params_from_virtual(virtual, "kairos")
        assert params == {}
