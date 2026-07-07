"""Tests for prompt_tiering.py — #27 prod/candidate registry with validation gates."""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.prompt_tiering import (
    PromptRegistry,
    PromptEntry,
    PromptTier,
    PromotionGateResult,
    create_registry,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _tmp_registry(min_candidate_days: int = 5) -> PromptRegistry:
    """Create a registry backed by a temp file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    path = Path(tmp.name)
    tmp.close()
    return PromptRegistry(registry_path=path, min_candidate_days=min_candidate_days)


def _accepted_wf() -> dict:
    """Synthetic accepted walk-forward result."""
    return {
        "accepted": True,
        "train_sharpe": 1.5,
        "val_sharpe": 1.2,
        "baseline_val_sharpe": 0.8,
        "confidence": 0.8,
        "reason": "All acceptance criteria met",
        "checks": {
            "val_sharpe_positive": True,
            "beats_baseline": True,
            "not_overfit": True,
            "significant": True,
        },
    }


def _rejected_wf() -> dict:
    """Synthetic rejected walk-forward result."""
    return {
        "accepted": False,
        "train_sharpe": 1.5,
        "val_sharpe": -0.5,
        "baseline_val_sharpe": 0.8,
        "confidence": 0.0,
        "reason": "Validation Sharpe -0.500 ≤ 0 (no edge on unseen data)",
        "checks": {
            "val_sharpe_positive": False,
            "beats_baseline": False,
            "not_overfit": True,
        },
    }


def _agreed_tp() -> dict:
    """Synthetic two-phase validation with agreement."""
    return {
        "trader": "kairos",
        "phase1_winner": "variant-047",
        "phase2_winner": "variant-047",
        "winner": "variant-047",
        "signal_llm_divergence": False,
        "baseline_llm_score": 1.0,
        "phase2_scores": {"variant-047": 2.5, "baseline": 1.0},
    }


def _divergent_tp() -> dict:
    """Synthetic two-phase validation with divergence."""
    return {
        "trader": "kairos",
        "phase1_winner": "variant-047",
        "phase2_winner": "variant-001",
        "winner": None,
        "signal_llm_divergence": True,
        "phase2_scores": {"variant-001": 2.5, "variant-047": 0.8, "baseline": 1.0},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Registry CRUD
# ═══════════════════════════════════════════════════════════════════════════════


class TestRegisterCandidate:
    def test_creates_candidate_entry(self):
        registry = _tmp_registry()
        entry = registry.register_candidate(
            trader="kairos",
            prompt_text="You are Kairos, a momentum trader...",
            description="Lowered momentum threshold from 0.55 to 0.40",
            source_branch="sweep/2026-07-05/kairos/variant-047",
        )
        assert entry.tier == PromptTier.CANDIDATE
        assert entry.trader == "kairos"
        assert entry.description == "Lowered momentum threshold from 0.55 to 0.40"
        assert entry.id

    def test_persists_across_reloads(self):
        path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        r1 = PromptRegistry(registry_path=path)
        e1 = r1.register_candidate("kairos", "Test prompt", description="First")

        r2 = PromptRegistry(registry_path=path)
        e2 = r2.get(e1.id)
        assert e2 is not None
        assert e2.trader == "kairos"
        assert e2.tier == PromptTier.CANDIDATE

    def test_metadata_stored(self):
        registry = _tmp_registry()
        entry = registry.register_candidate(
            trader="stonks",
            prompt_text="Sentiment-heavy strategy",
            metadata={"sentiment_model": "v3", "calmar_delta": 0.15},
        )
        assert entry.metadata["sentiment_model"] == "v3"
        assert entry.metadata["calmar_delta"] == 0.15


class TestValidationAttachment:
    def test_attaches_walk_forward(self):
        registry = _tmp_registry()
        entry = registry.register_candidate("kairos", "test")

        updated = registry.attach_validation(
            entry.id,
            walk_forward_result=_accepted_wf(),
        )
        assert updated.walk_forward_result is not None
        assert updated.walk_forward_result["accepted"] is True

    def test_attaches_two_phase(self):
        registry = _tmp_registry()
        entry = registry.register_candidate("kairos", "test")

        updated = registry.attach_validation(
            entry.id,
            two_phase_result=_agreed_tp(),
        )
        assert updated.two_phase_result is not None
        assert updated.two_phase_result["winner"] == "variant-047"

    def test_attaches_both(self):
        registry = _tmp_registry()
        entry = registry.register_candidate("kairos", "test")

        updated = registry.attach_validation(
            entry.id,
            walk_forward_result=_accepted_wf(),
            two_phase_result=_agreed_tp(),
        )
        assert updated.walk_forward_result is not None
        assert updated.two_phase_result is not None

    def test_unknown_entry_raises(self):
        registry = _tmp_registry()
        with pytest.raises(KeyError):
            registry.attach_validation("nonexistent", walk_forward_result=_accepted_wf())


class TestPromotionAndRetirement:
    def test_promote_success(self):
        registry = _tmp_registry()
        entry = registry.register_candidate("kairos", "You are Kairos...")
        registry.attach_validation(
            entry.id,
            walk_forward_result=_accepted_wf(),
            two_phase_result=_agreed_tp(),
        )

        promoted = registry.promote(entry.id)
        assert promoted.tier == PromptTier.PROD
        assert promoted.promoted_at is not None
        assert promoted.version != "v0.0.0"

    def test_retire_current_prod_on_promotion(self):
        registry = _tmp_registry()
        # Create and promote a first prompt
        e1 = registry.register_candidate("kairos", "Prompt v1")
        registry.promote(e1.id)

        # Create and promote a second prompt
        e2 = registry.register_candidate("kairos", "Prompt v2", description="Better")
        registry.promote(e2.id)

        # First prompt should now be retired
        retired = registry.get(e1.id)
        assert retired.tier == PromptTier.RETIRED
        assert retired.retired_at is not None

    def test_promote_non_candidate_raises(self):
        registry = _tmp_registry()
        entry = registry.register_candidate("kairos", "test")
        registry.promote(entry.id)  # Now PROD

        with pytest.raises(ValueError, match="not in CANDIDATE"):
            registry.promote(entry.id)

    def test_unknown_entry_raises(self):
        registry = _tmp_registry()
        with pytest.raises(ValueError, match="not found"):
            registry.promote("nonexistent")

    def test_retire(self):
        registry = _tmp_registry()
        entry = registry.register_candidate("kairos", "test")
        registry.promote(entry.id)

        retired = registry.retire(entry.id)
        assert retired.tier == PromptTier.RETIRED
        assert retired.retired_at is not None

    def test_version_bump(self):
        registry = _tmp_registry()
        e1 = registry.register_candidate("kairos", "Prompt v1")
        registry.promote(e1.id)
        assert e1.version == "kairos/v1.0.0"

        e2 = registry.register_candidate("kairos", "Prompt v2")
        registry.promote(e2.id)
        assert e2.version == "kairos/v1.0.1"
        assert e2.prev_version == "kairos/v1.0.0"


class TestQueries:
    def test_list_prod(self):
        registry = _tmp_registry()
        entry = registry.register_candidate("kairos", "Test")
        registry.promote(entry.id)

        prod = registry.list_prod("kairos")
        assert prod is not None
        assert prod.id == entry.id

    def test_list_prod_returns_none_when_empty(self):
        registry = _tmp_registry()
        assert registry.list_prod("kairos") is None

    def test_list_all_prod(self):
        registry = _tmp_registry()
        e1 = registry.register_candidate("kairos", "K")
        e2 = registry.register_candidate("aldridge", "A")
        registry.promote(e1.id)
        registry.promote(e2.id)

        prods = registry.list_all_prod()
        assert len(prods) == 2
        assert prods[0].trader == "aldridge"

    def test_list_candidates(self):
        registry = _tmp_registry()
        registry.register_candidate("kairos", "C1", description="First")
        registry.register_candidate("kairos", "C2", description="Second")
        registry.register_candidate("aldridge", "C3", description="Other")

        k_candidates = registry.list_candidates("kairos")
        assert len(k_candidates) == 2

        all_candidates = registry.list_candidates()
        assert len(all_candidates) == 3

    def test_list_retired(self):
        registry = _tmp_registry()
        e = registry.register_candidate("kairos", "test")
        registry.promote(e.id)
        registry.retire(e.id)

        retired = registry.list_retired()
        assert len(retired) == 1
        assert retired[0].id == e.id

    def test_count_by_tier(self):
        registry = _tmp_registry()
        e1 = registry.register_candidate("kairos", "prod-ready")
        e2 = registry.register_candidate("aldridge", "pending")
        registry.promote(e1.id)

        counts = registry.count_by_tier()
        assert counts["candidate"] == 1
        assert counts["prod"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Promotion gates
# ═══════════════════════════════════════════════════════════════════════════════


class TestPromotionGates:
    def test_all_gates_pass(self):
        """When all validation passes, gates open."""
        registry = _tmp_registry(min_candidate_days=0)  # Skip time gate for test
        entry = registry.register_candidate("kairos", "test")

        # Set creation date to 6 days ago so time gate passes
        entry.created_at = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()

        registry.attach_validation(
            entry.id,
            walk_forward_result=_accepted_wf(),
            two_phase_result=_agreed_tp(),
        )

        result = registry.check_promotion_gates(entry.id)
        assert result.passed, f"Gates should pass: {result.failures}"
        assert len(result.reasons) >= 3

    def test_fails_missing_wf(self):
        """Missing walk-forward → gate closed."""
        registry = _tmp_registry(min_candidate_days=0)
        entry = registry.register_candidate("kairos", "test")
        entry.created_at = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()

        registry.attach_validation(
            entry.id,
            two_phase_result=_agreed_tp(),
        )

        result = registry.check_promotion_gates(entry.id)
        assert not result.passed
        assert any("walk-forward" in f.lower() for f in result.failures)

    def test_fails_rejected_wf(self):
        """Rejected walk-forward → gate closed."""
        registry = _tmp_registry(min_candidate_days=0)
        entry = registry.register_candidate("kairos", "test")
        entry.created_at = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()

        registry.attach_validation(
            entry.id,
            walk_forward_result=_rejected_wf(),
            two_phase_result=_agreed_tp(),
        )

        result = registry.check_promotion_gates(entry.id)
        assert not result.passed
        assert any("walk-forward" in f.lower() for f in result.failures)

    def test_fails_signal_llm_divergence(self):
        """Two-phase divergence → gate closed."""
        registry = _tmp_registry(min_candidate_days=0)
        entry = registry.register_candidate("kairos", "test")
        entry.created_at = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()

        registry.attach_validation(
            entry.id,
            walk_forward_result=_accepted_wf(),
            two_phase_result=_divergent_tp(),
        )

        result = registry.check_promotion_gates(entry.id)
        assert not result.passed
        assert any("divergence" in f.lower() for f in result.failures)

    def test_fails_no_two_phase_winner(self):
        """Two-phase with no winner at all → gate closed."""
        registry = _tmp_registry(min_candidate_days=0)
        entry = registry.register_candidate("kairos", "test")
        entry.created_at = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()

        tp_no_winner = dict(_agreed_tp())
        tp_no_winner["winner"] = None
        tp_no_winner["phase2_winner"] = None
        tp_no_winner["phase2_scores"] = {"baseline": 1.0}

        registry.attach_validation(
            entry.id,
            walk_forward_result=_accepted_wf(),
            two_phase_result=tp_no_winner,
        )

        result = registry.check_promotion_gates(entry.id)
        assert not result.passed
        assert any("no winner" in f.lower() for f in result.failures)

    def test_fails_insufficient_time(self):
        """Not enough time in candidate → gate closed."""
        registry = _tmp_registry(min_candidate_days=5)
        entry = registry.register_candidate("kairos", "test")
        # created_at is just now — less than 5 days

        registry.attach_validation(
            entry.id,
            walk_forward_result=_accepted_wf(),
            two_phase_result=_agreed_tp(),
        )

        result = registry.check_promotion_gates(entry.id)
        assert not result.passed
        assert any("evaluation period" in f.lower() for f in result.failures)

    def test_fails_already_prod(self):
        """Already PROD → cannot promote again."""
        registry = _tmp_registry(min_candidate_days=0)
        entry = registry.register_candidate("kairos", "test")
        entry.created_at = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
        registry.promote(entry.id)  # Now PROD

        result = registry.check_promotion_gates(entry.id)
        assert not result.passed
        assert any("already in prod" in f.lower() for f in result.failures)

    def test_fails_missing_two_phase(self):
        """Missing two-phase → gate closed."""
        registry = _tmp_registry(min_candidate_days=0)
        entry = registry.register_candidate("kairos", "test")
        entry.created_at = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()

        registry.attach_validation(
            entry.id,
            walk_forward_result=_accepted_wf(),
        )

        result = registry.check_promotion_gates(entry.id)
        assert not result.passed
        assert any("two-phase" in f.lower() for f in result.failures)


class TestPromotionGateResult:
    def test_success(self):
        r = PromotionGateResult.success(["Gate 1 passed", "Gate 2 passed"])
        assert r.passed
        assert len(r.reasons) == 2
        assert len(r.failures) == 0

    def test_failure(self):
        r = PromotionGateResult.failure(["Gate 1 failed"], ["Gate 2 passed"])
        assert not r.passed
        assert len(r.failures) == 1
        assert len(r.reasons) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_empty_registry(self):
        registry = _tmp_registry()
        assert registry.list_all() == []
        assert registry.count_by_tier() == {}
        assert registry.list_prod("kairos") is None

    def test_create_registry_factory(self):
        path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        r = create_registry(registry_path=path)
        assert isinstance(r, PromptRegistry)

    def test_multiple_candidates_same_trader(self):
        registry = _tmp_registry()
        c1 = registry.register_candidate("kairos", "C1")
        c2 = registry.register_candidate("kairos", "C2")
        c3 = registry.register_candidate("kairos", "C3")

        candidates = registry.list_candidates("kairos")
        assert len(candidates) == 3
        assert candidates[0].id == c3.id  # Most recent first

    def test_atomic_save_no_partial_writes(self):
        """Registry survives a write with no corruption."""
        path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        r = PromptRegistry(registry_path=path)
        for i in range(10):
            r.register_candidate("kairos", f"Prompt {i}")

        r2 = PromptRegistry(registry_path=path)
        assert len(r2.list_all()) == 10

    def test_prompt_text_preserved(self):
        long_prompt = "You are Kairos.\n" * 100
        registry = _tmp_registry()
        entry = registry.register_candidate("kairos", long_prompt)
        assert registry.get(entry.id).prompt_text == long_prompt
