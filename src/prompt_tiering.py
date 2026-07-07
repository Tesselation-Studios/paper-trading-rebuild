"""Prompt tiering registry — prod/candidate separation with validation gates.

#27: Formally separate prod vs candidate prompts. Gate promotions.

Tiers:
  - PROD:        Live on traders. One per trader at a time.
  - CANDIDATE:   Proposed via sweep/experiment. Awaiting validation.
  - RETIRED:     Former PROD prompts, archived for audit trail.

Promotion gates (all must pass):
  1. Walk-forward validation (SPEC §6.1) — Val Sharpe > 0, > baseline, > train × 0.7
  2. Two-phase validation agreement (signal + LLM both pick same winner)
  3. Minimum evaluation period — at least 5 trading days in candidate tier
  4. No divergence — signal winner must match LLM winner

Usage:
    registry = PromptRegistry()
    registry.register_candidate(trader="kairos", prompt_text="...", metadata={...})
    registry.list_prod("kairos")   # → current prod prompt
    registry.list_candidates()     # → all candidates pending
    registry.promote("candidate-id")  # → move to PROD after gate check
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Domain types
# ═══════════════════════════════════════════════════════════════════════════════


class PromptTier(str, Enum):
    PROD = "prod"
    CANDIDATE = "candidate"
    RETIRED = "retired"


@dataclass
class PromptEntry:
    """A single prompt in the registry."""

    id: str                          # UUID
    trader: str                      # "kairos" | "aldridge" | "stonks"
    tier: PromptTier
    prompt_text: str
    created_at: str                  # ISO 8601
    promoted_at: Optional[str] = None  # ISO 8601 when moved to PROD
    retired_at: Optional[str] = None   # ISO 8601 when retired

    # Version tracking
    version: str = "v0.0.0"
    prev_version: Optional[str] = None

    # Validation results
    walk_forward_result: Optional[Dict[str, Any]] = None
    two_phase_result: Optional[Dict[str, Any]] = None

    # Metadata
    description: str = ""
    source_branch: str = ""          # sweep branch or experiment branch
    commit_sha: str = ""             # git SHA of the prompt
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PromotionGateResult:
    """Result of checking promotion gates."""

    passed: bool
    reasons: List[str]  # Pass reasons
    failures: List[str]  # Fail reasons

    @classmethod
    def success(cls, reasons: List[str] | None = None) -> "PromotionGateResult":
        return cls(passed=True, reasons=reasons or [], failures=[])

    @classmethod
    def failure(cls, failures: List[str], reasons: List[str] | None = None) -> "PromotionGateResult":
        return cls(passed=False, reasons=reasons or [], failures=failures)


# ═══════════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════════


class PromptRegistry:
    """Manages the prompt tiering registry.

    Backed by a JSON file for simplicity. All mutations are atomic
    (write to temp file, rename) to prevent corruption.

    Args:
        registry_path: Path to the registry JSON file.
        min_candidate_days: Minimum days a prompt must be in CANDIDATE
            before it can be promoted (default 5 per SPEC §6.3).
    """

    def __init__(
        self,
        registry_path: Path | str | None = None,
        min_candidate_days: int = 5,
    ):
        if registry_path is None:
            registry_path = Path(__file__).resolve().parent.parent / "prompts" / "registry.json"
        self.registry_path = Path(registry_path)
        self.min_candidate_days = min_candidate_days
        self._entries: Dict[str, PromptEntry] = {}

        if self.registry_path.exists():
            self._load()
        else:
            self._save()

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def register_candidate(
        self,
        trader: str,
        prompt_text: str,
        description: str = "",
        source_branch: str = "",
        commit_sha: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> PromptEntry:
        """Register a new candidate prompt.

        Args:
            trader: Trader name (e.g., 'kairos').
            prompt_text: Full prompt content.
            description: Human-readable description of what changed.
            source_branch: Git branch name this candidate came from.
            commit_sha: Git SHA of the prompt.
            metadata: Arbitrary metadata dict.

        Returns:
            The created PromptEntry.
        """
        entry = PromptEntry(
            id=str(uuid4()),
            trader=trader,
            tier=PromptTier.CANDIDATE,
            prompt_text=prompt_text,
            created_at=datetime.now(timezone.utc).isoformat(),
            description=description,
            source_branch=source_branch,
            commit_sha=commit_sha,
            metadata=metadata or {},
        )
        self._entries[entry.id] = entry
        self._save()
        log.info("Registered candidate %s for %s: %s", entry.id, trader, description)
        return entry

    def attach_validation(
        self,
        entry_id: str,
        walk_forward_result: Dict[str, Any] | None = None,
        two_phase_result: Dict[str, Any] | None = None,
    ) -> PromptEntry:
        """Attach validation results to a candidate entry.

        Args:
            entry_id: ID of the candidate entry.
            walk_forward_result: Result from WalkForwardValidator.validate().
            two_phase_result: Diagnostics from two_phase_validate().

        Returns:
            Updated PromptEntry.

        Raises:
            KeyError: If entry_id not found.
        """
        entry = self._entries[entry_id]
        if walk_forward_result is not None:
            entry.walk_forward_result = walk_forward_result
        if two_phase_result is not None:
            entry.two_phase_result = two_phase_result
        self._save()
        log.info("Attached validation to candidate %s", entry_id)
        return entry

    def check_promotion_gates(self, entry_id: str) -> PromotionGateResult:
        """Check if a candidate passes all promotion gates.

        Gates (per SPEC §6 and ROADMAP #27):
          1. Walk-forward validation must be present and accepted
          2. Two-phase validation must agree (signal + LLM same winner)
          3. Minimum 5 trading days in CANDIDATE tier
          4. No validation divergence

        Args:
            entry_id: ID of the candidate entry.

        Returns:
            PromotionGateResult with pass/fail details.
        """
        entry = self._entries.get(entry_id)
        if entry is None:
            return PromotionGateResult.failure([f"Entry {entry_id} not found"])

        failures: List[str] = []
        reasons: List[str] = []

        # Gate 1: Walk-forward validation
        wf = entry.walk_forward_result
        if wf is None:
            failures.append("No walk-forward validation results attached")
        elif not wf.get("accepted", False):
            failures.append(
                f"Walk-forward validation rejected: {wf.get('reason', 'unknown')}"
            )
        else:
            reasons.append(
                f"Walk-forward validation passed "
                f"(train Sharpe {wf.get('train_sharpe', '?'):.3f}, "
                f"val Sharpe {wf.get('val_sharpe', '?'):.3f})"
            )

        # Gate 2: Two-phase validation agreement
        tp = entry.two_phase_result
        if tp is None:
            failures.append("No two-phase validation results attached")
        elif tp.get("signal_llm_divergence", True):
            failures.append(
                "Two-phase validation: signal/LLM divergence — "
                f"signal winner={tp.get('phase1_winner')}, "
                f"LLM winner={tp.get('phase2_winner')}"
            )
        elif tp.get("winner") is None:
            failures.append(
                "Two-phase validation: no winner — all variants failed to beat baseline"
            )
        else:
            reasons.append(
                f"Two-phase validation passed: winner={tp.get('winner')}"
            )

        # Gate 3: Minimum evaluation period
        created = datetime.fromisoformat(entry.created_at.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - created
        if age < timedelta(days=self.min_candidate_days):
            failures.append(
                f"Insufficient evaluation period: "
                f"{age.days}d < {self.min_candidate_days}d minimum"
            )
        else:
            reasons.append(f"Evaluation period: {age.days} days (≥ {self.min_candidate_days})")

        # Gate 4: Must not be already PROD or RETIRED
        if entry.tier == PromptTier.PROD:
            failures.append("Already in PROD tier")
        elif entry.tier == PromptTier.RETIRED:
            failures.append("Cannot promote from RETIRED tier")

        if failures:
            return PromotionGateResult.failure(failures, reasons)
        return PromotionGateResult.success(reasons)

    def promote(self, entry_id: str, version: str = "") -> PromptEntry:
        """Promote a candidate to PROD tier.

        This automatically retires the current PROD entry for the same trader.
        The promotion gates MUST be checked first — call check_promotion_gates()
        to verify readiness before calling promote().

        Args:
            entry_id: ID of the candidate entry.
            version: Version string (e.g., 'kairos/v1.0.4'). Auto-computed if empty.

        Returns:
            The promoted PromptEntry.

        Raises:
            ValueError: If entry not found or not in CANDIDATE tier.
        """
        entry = self._entries.get(entry_id)
        if entry is None:
            raise ValueError(f"Entry {entry_id} not found")
        if entry.tier != PromptTier.CANDIDATE:
            raise ValueError(f"Entry {entry_id} is not in CANDIDATE tier (current: {entry.tier})")

        # Retire the current prod entry for this trader
        current_prod = self.list_prod(entry.trader)

        # Compute version before retiring (needs current prod's version)
        if version:
            entry.version = version
        else:
            entry.version = self._next_version(entry.trader)

        if current_prod is not None:
            entry.prev_version = current_prod.version
            current_prod.tier = PromptTier.RETIRED
            current_prod.retired_at = datetime.now(timezone.utc).isoformat()
            log.info("Retired prod %s (%s) for %s", current_prod.id, current_prod.version, entry.trader)

        # Promote
        entry.tier = PromptTier.PROD
        entry.promoted_at = datetime.now(timezone.utc).isoformat()
        self._save()
        log.info("Promoted %s to PROD for %s (version %s)", entry_id, entry.trader, entry.version)
        return entry

    def retire(self, entry_id: str) -> PromptEntry:
        """Manually retire a prompt (move to RETIRED tier).

        Args:
            entry_id: ID of the entry.

        Returns:
            The retired PromptEntry.
        """
        entry = self._entries[entry_id]
        entry.tier = PromptTier.RETIRED
        entry.retired_at = datetime.now(timezone.utc).isoformat()
        self._save()
        log.info("Retired %s (%s)", entry_id, entry.version)
        return entry

    # ── Queries ──────────────────────────────────────────────────────────────

    def get(self, entry_id: str) -> Optional[PromptEntry]:
        """Get an entry by ID."""
        return self._entries.get(entry_id)

    def list_prod(self, trader: str | None = None) -> Optional[PromptEntry]:
        """Get the current PROD prompt for a trader.

        Returns the single PROD entry for the trader, or None if none exists.
        """
        for entry in self._entries.values():
            if entry.tier == PromptTier.PROD and (trader is None or entry.trader == trader):
                return entry
        return None

    def list_all_prod(self) -> List[PromptEntry]:
        """List all PROD prompts across all traders."""
        return sorted(
            [e for e in self._entries.values() if e.tier == PromptTier.PROD],
            key=lambda e: e.trader,
        )

    def list_candidates(self, trader: str | None = None) -> List[PromptEntry]:
        """List all CANDIDATE prompts, optionally filtered by trader."""
        entries = [
            e for e in self._entries.values()
            if e.tier == PromptTier.CANDIDATE
            and (trader is None or e.trader == trader)
        ]
        return sorted(entries, key=lambda e: e.created_at, reverse=True)

    def list_retired(self, trader: str | None = None) -> List[PromptEntry]:
        """List all RETIRED prompts, optionally filtered by trader."""
        entries = [
            e for e in self._entries.values()
            if e.tier == PromptTier.RETIRED
            and (trader is None or e.trader == trader)
        ]
        return sorted(entries, key=lambda e: e.retired_at or "", reverse=True)

    def list_all(self) -> List[PromptEntry]:
        """List all entries in the registry."""
        return sorted(self._entries.values(), key=lambda e: e.created_at, reverse=True)

    def count_by_tier(self) -> Dict[str, int]:
        """Count entries by tier."""
        counts: Dict[str, int] = {}
        for entry in self._entries.values():
            counts[entry.tier.value] = counts.get(entry.tier.value, 0) + 1
        return counts

    # ── Internal ─────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load registry from JSON file."""
        try:
            with open(self.registry_path) as f:
                content = f.read().strip()
                if not content:
                    self._entries = {}
                    return
                data = json.loads(content)
        except (FileNotFoundError, json.JSONDecodeError):
            self._entries = {}
            return

        self._entries = {}
        for item in data.get("entries", []):
            entry = PromptEntry(
                id=item["id"],
                trader=item["trader"],
                tier=PromptTier(item["tier"]),
                prompt_text=item["prompt_text"],
                created_at=item["created_at"],
                promoted_at=item.get("promoted_at"),
                retired_at=item.get("retired_at"),
                version=item.get("version", "v0.0.0"),
                prev_version=item.get("prev_version"),
                walk_forward_result=item.get("walk_forward_result"),
                two_phase_result=item.get("two_phase_result"),
                description=item.get("description", ""),
                source_branch=item.get("source_branch", ""),
                commit_sha=item.get("commit_sha", ""),
                metadata=item.get("metadata", {}),
            )
            self._entries[entry.id] = entry

    def _save(self) -> None:
        """Save registry to JSON file (atomic write)."""
        data = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "entries": [],
        }
        for entry in self._entries.values():
            item = {
                "id": entry.id,
                "trader": entry.trader,
                "tier": entry.tier.value,
                "prompt_text": entry.prompt_text,
                "created_at": entry.created_at,
                "promoted_at": entry.promoted_at,
                "retired_at": entry.retired_at,
                "version": entry.version,
                "prev_version": entry.prev_version,
                "walk_forward_result": entry.walk_forward_result,
                "two_phase_result": entry.two_phase_result,
                "description": entry.description,
                "source_branch": entry.source_branch,
                "commit_sha": entry.commit_sha,
                "metadata": entry.metadata,
            }
            data["entries"].append(item)

        # Atomic write
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.rename(self.registry_path)

    def _next_version(self, trader: str) -> str:
        """Auto-compute next patch version for a trader."""
        prod = self.list_prod(trader)
        if prod and prod.version:
            parts = prod.version.split("/v")
            if len(parts) == 2:
                try:
                    v_parts = parts[1].split(".")
                    major, minor, patch = int(v_parts[0]), int(v_parts[1]), int(v_parts[2])
                    return f"{trader}/v{major}.{minor}.{patch + 1}"
                except (ValueError, IndexError):
                    pass
        return f"{trader}/v1.0.0"


# ═══════════════════════════════════════════════════════════════════════════════
# Factory / convenience
# ═══════════════════════════════════════════════════════════════════════════════


def create_registry(registry_path: Path | str | None = None) -> PromptRegistry:
    """Create or open a prompt registry.

    Args:
        registry_path: Path to registry JSON. Defaults to prompts/registry.json.

    Returns:
        PromptRegistry instance.
    """
    return PromptRegistry(registry_path=registry_path)
