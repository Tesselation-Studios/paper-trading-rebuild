"""Tests for promotion_check — virtual trader promotion system (SPEC-v3 §1.2)."""

import pytest
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from scripts.promotion_check import (
    VirtualTrader,
    PromotionCheck,
    PromotionResult,
    check_promotion,
    TIER_INDEX,
    TIERS,
    PROMOTION_GATES,
    TIER_SLOTS,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def probation_trader() -> VirtualTrader:
    return VirtualTrader(
        id=1,
        name="kairos-pro-1",
        base_trader="kairos",
        variant_type="param",
        config={},
        status="active",
        tier="probation",
        composite_score=None,
        created_at=date(2026, 7, 10),
        promoted_at=None,
    )


@pytest.fixture
def rookie_trader() -> VirtualTrader:
    return VirtualTrader(
        id=2,
        name="kairos-rk-1",
        base_trader="kairos",
        variant_type="param",
        config={},
        status="active",
        tier="rookie",
        composite_score=0.5,
        created_at=date(2026, 7, 1),
        promoted_at=datetime(2026, 7, 5),
    )


@pytest.fixture
def veteran_trader() -> VirtualTrader:
    return VirtualTrader(
        id=3,
        name="kairos-vee-1",
        base_trader="kairos",
        variant_type="param",
        config={},
        status="active",
        tier="veteran",
        composite_score=0.75,
        created_at=date(2026, 6, 15),
        promoted_at=datetime(2026, 7, 1),
    )


@pytest.fixture
def live_trader() -> VirtualTrader:
    return VirtualTrader(
        id=4,
        name="kairos-live-1",
        base_trader="kairos",
        variant_type="base",
        config={},
        status="active",
        tier="live",
        composite_score=0.9,
        created_at=date(2026, 5, 1),
        promoted_at=datetime(2026, 6, 1),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTierSystem:
    def test_tier_order(self):
        """Tiers should be ordered correctly."""
        assert TIERS == ["probation", "rookie", "veteran", "expert", "elite", "live"]
        assert TIER_INDEX["probation"] == 0
        assert TIER_INDEX["live"] == 5

    def test_tier_slots(self):
        """Slot caps should be defined for all tiers."""
        assert TIER_SLOTS["probation"] == 999
        assert TIER_SLOTS["rookie"] == 12
        assert TIER_SLOTS["veteran"] == 8
        assert TIER_SLOTS["expert"] == 4
        assert TIER_SLOTS["elite"] == 2
        assert TIER_SLOTS["live"] == 1

    def test_promotion_gates_defined(self):
        """All tier transitions should have gates defined."""
        for i in range(len(TIERS) - 1):
            key = (TIERS[i], TIERS[i + 1])
            assert key in PROMOTION_GATES, f"Missing gate: {key}"


class TestCheckPromotion:
    def test_probation_to_rookie_good_metrics(self, probation_trader: VirtualTrader):
        metrics = {
            "n_trades": 10,
            "total_return_pct": 2.5,
            "max_drawdown": 5.0,
            "win_rate": 60.0,
            "sortino": 0.8,
            "calmar": 0.5,
            "profit_factor": 1.5,
            "composite_score": 0.3,
        }
        check = check_promotion(probation_trader, metrics, rank=1)
        assert check.eligible, f"Should be eligible: {check.failures}"
        assert check.from_tier == "probation"
        assert check.to_tier == "rookie"

    def test_probation_to_rookie_too_few_trades(self, probation_trader: VirtualTrader):
        metrics = {
            "n_trades": 1,
            "total_return_pct": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "sortino": 0.0,
            "calmar": 0.0,
            "profit_factor": 0.0,
            "composite_score": 0.0,
        }
        check = check_promotion(probation_trader, metrics, rank=5)
        assert not check.eligible
        assert any("trades" in f.lower() for f in check.failures)

    def test_rookie_to_veteran_good(self, rookie_trader: VirtualTrader):
        metrics = {
            "n_trades": 30,
            "total_return_pct": 5.0,
            "max_drawdown": 10.0,
            "win_rate": 50.0,
            "sortino": 0.8,
            "calmar": 0.5,
            "profit_factor": 1.5,
            "composite_score": 0.5,
        }
        check = check_promotion(rookie_trader, metrics, rank=2)
        assert check.eligible, f"Should be eligible: {check.failures}"

    def test_rookie_to_veteran_low_sortino(self, rookie_trader: VirtualTrader):
        metrics = {
            "n_trades": 30,
            "total_return_pct": 5.0,
            "max_drawdown": 10.0,
            "win_rate": 50.0,
            "sortino": 0.2,  # below 0.5 threshold
            "calmar": 0.5,
            "profit_factor": 1.5,
            "composite_score": 0.5,
        }
        check = check_promotion(rookie_trader, metrics, rank=2)
        assert not check.eligible
        assert any("sortino" in f.lower() for f in check.failures)

    def test_veteran_to_expert_good(self, veteran_trader: VirtualTrader):
        metrics = {
            "n_trades": 60,
            "total_return_pct": 8.0,
            "max_drawdown": 15.0,
            "win_rate": 55.0,
            "sortino": 1.2,
            "calmar": 0.6,
            "profit_factor": 2.0,
            "composite_score": 0.75,
        }
        check = check_promotion(veteran_trader, metrics, rank=2)
        assert check.eligible, f"Should be eligible: {check.failures}"

    def test_veteran_to_expert_low_rank(self, veteran_trader: VirtualTrader):
        metrics = {
            "n_trades": 60,
            "total_return_pct": 8.0,
            "max_drawdown": 15.0,
            "win_rate": 55.0,
            "sortino": 1.2,
            "calmar": 0.6,
            "profit_factor": 2.0,
            "composite_score": 0.75,
        }
        check = check_promotion(veteran_trader, metrics, rank=5)
        assert not check.eligible
        assert any("rank" in f.lower() for f in check.failures)

    def test_live_trader_not_promotable(self, live_trader: VirtualTrader):
        metrics = {
            "n_trades": 300,
            "total_return_pct": 30.0,
            "max_drawdown": 10.0,
            "win_rate": 60.0,
            "sortino": 2.5,
            "calmar": 1.5,
            "profit_factor": 3.0,
            "composite_score": 0.9,
        }
        check = check_promotion(live_trader, metrics, rank=1)
        assert not check.eligible
        assert "max tier" in " ".join(check.failures).lower()

    def test_young_trader_denied(self):
        trader = VirtualTrader(
            id=5, name="new-pro", base_trader="kairos",
            variant_type="param", config={}, status="active",
            tier="probation", composite_score=None,
            created_at=date.today(), promoted_at=None,
        )
        metrics = {
            "n_trades": 0, "total_return_pct": 0, "max_drawdown": 0,
            "win_rate": 0, "sortino": 0, "calmar": 0,
            "profit_factor": 0, "composite_score": 0,
        }
        check = check_promotion(trader, metrics, rank=5)
        assert not check.eligible
        assert any("age" in f.lower() for f in check.failures)


class TestVirtualTraderModel:
    def test_age_days(self, probation_trader: VirtualTrader):
        assert probation_trader.age_days > 0

    def test_age_days_no_created_at(self):
        trader = VirtualTrader(
            id=99, name="no-date", base_trader="kairos",
            variant_type="param", config={}, status="active",
            tier="probation", composite_score=None,
            created_at=None, promoted_at=None,
        )
        assert trader.age_days == 0

    def test_promotion_result_dataclass(self):
        result = PromotionResult(
            trader_name="test",
            from_tier="probation",
            to_tier="rookie",
            promoted=True,
            reason="Met criteria",
            metrics={"n_trades": 10},
        )
        assert result.promoted
        assert result.from_tier == "probation"

    def test_promotion_check_dataclass(self, probation_trader: VirtualTrader):
        check = PromotionCheck(
            trader=probation_trader,
            from_tier="probation",
            to_tier="rookie",
            eligible=True,
            reasons=["All good"],
            failures=[],
            metrics={"n_trades": 10},
        )
        assert check.eligible
        assert check.reasons == ["All good"]


class TestTierTransitions:
    def test_all_gates_defined(self):
        """Every tier pair should have a gate."""
        for i in range(len(TIERS) - 1):
            gate = PROMOTION_GATES.get((TIERS[i], TIERS[i + 1]))
            assert gate is not None, f"Missing gate: {TIERS[i]} → {TIERS[i + 1]}"

    def test_gate_values_are_reasonable(self):
        """Gates should have sensible minimum values."""
        for (from_tier, to_tier), gate in PROMOTION_GATES.items():
            assert gate.min_trades >= 0
            assert gate.min_age_days >= 0
            assert gate.min_sortino >= -10
            assert gate.min_calmar >= -10
            assert isinstance(gate.soft_gate, bool)


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-Trader Learning (Seed Descendants) — #203
# ═══════════════════════════════════════════════════════════════════════════════


class TestExtractSeedParams:
    def test_empty_config(self):
        from scripts.promotion_check import _extract_seed_params
        assert _extract_seed_params({}) == {}

    def test_scalar_params_extracted(self):
        from scripts.promotion_check import _extract_seed_params
        config = {
            "stop_loss": 0.05,
            "take_profit": 0.10,
            "max_positions": 5,
            "temperature": 0.3,
        }
        result = _extract_seed_params(config)
        assert result == config

    def test_meta_keys_excluded(self):
        from scripts.promotion_check import _extract_seed_params
        config = {
            "stop_loss": 0.05,
            "parent": "kairos-exp-1",
            "generation": 2,
            "parent_tier": "expert",
            "descendant_of": "kairos-exp-1",
        }
        result = _extract_seed_params(config)
        assert "stop_loss" in result
        assert "parent" not in result
        assert "generation" not in result
        assert "parent_tier" not in result
        assert "descendant_of" not in result

    def test_nested_dicts_excluded(self):
        from scripts.promotion_check import _extract_seed_params
        config = {
            "stop_loss": 0.05,
            "nested": {"key": "value"},
            "complex": [{"a": 1}],
        }
        result = _extract_seed_params(config)
        assert "stop_loss" in result
        assert "nested" not in result
        assert "complex" not in result  # list of dicts excluded

    def test_simple_lists_preserved(self):
        from scripts.promotion_check import _extract_seed_params
        config = {
            "symbols": ["AAPL", "GOOGL", "MSFT"],
            "weights": [0.3, 0.3, 0.4],
        }
        result = _extract_seed_params(config)
        assert result["symbols"] == ["AAPL", "GOOGL", "MSFT"]
        assert result["weights"] == [0.3, 0.3, 0.4]

    def test_none_config(self):
        from scripts.promotion_check import _extract_seed_params
        assert _extract_seed_params(None) == {}


class TestSeedDescendants:
    def test_no_seeding_below_expert(self):
        """Only Expert and Elite promotions trigger cross-trader seeding."""
        from scripts.promotion_check import seed_descendants, VirtualTrader
        trader = VirtualTrader(
            id=1, name="kairos-vee-1", base_trader="kairos",
            variant_type="param", config={}, status="active",
            tier="veteran", composite_score=None,
            created_at=date(2026, 7, 1), promoted_at=None,
        )
        # Rookie and Veteran promotions should not seed
        assert seed_descendants(None, trader, "rookie") == 0
        assert seed_descendants(None, trader, "veteran") == 0

    def test_no_seedable_params_skips(self):
        from scripts.promotion_check import seed_descendants, VirtualTrader
        trader = VirtualTrader(
            id=1, name="kairos-exp-1", base_trader="kairos",
            variant_type="param", config={}, status="active",
            tier="expert", composite_score=0.85,
            created_at=date(2026, 7, 1), promoted_at=None,
        )
        assert seed_descendants(None, trader, "expert") == 0