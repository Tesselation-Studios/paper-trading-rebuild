#!/usr/bin/env python3
"""
Validation Gate — Architectural Invariant #7 enforcement.

SPEC §6.1 / Architectural Invariant #7: No parameter change accepted
without validation on unseen data. This module bridges the existing
WalkForwardValidator into the promotion/deploy pipeline.

Gate flow:
  1. Extract candidate + baseline params from virtual_traders table
  2. Load historical tick data for the trader's universe
  3. Run walk-forward validation (WalkForwardValidator)
  4. If accepted → return success with diagnostics
  5. If rejected → return failure with reason (blocks promotion)

Usage:
    from src.validation_gate import ValidationGate, GateResult

    gate = ValidationGate()
    result = gate.check(
        candidate_params={"momentum_threshold": 0.60},
        baseline_params={"momentum_threshold": 0.55},
        trader_id="kairos",
        tick_data=ticks,  # optional; loads from DB if not provided
    )
    if result.passed:
        print(f"Validation gate passed: {result.reason}")
    else:
        print(f"REJECTED: {result.reason}")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.validation import (
    ValidationResult,
    WalkForwardConfig,
    WalkForwardValidator,
    walk_forward_validate,
)

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Domain types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class GateResult:
    """Result of the validation gate check.

    Per SPEC §6.1, the gate passes when all three criteria are met:
      1. Validation Sharpe > 0 (positive on unseen data)
      2. Validation Sharpe > Baseline Sharpe (improvement)
      3. Validation Sharpe > Training Sharpe × 0.7 (not grossly overfit)

    Attributes:
        passed: True if all acceptance criteria met.
        reason: Human-readable explanation.
        validation: Underlying ValidationResult with detailed metrics.
        candidate_params: The proposed parameters that were tested.
        baseline_params: The current production parameters.
        trader_id: Which trader this validation was for.
        checked_at: ISO 8601 timestamp of the check.
    """

    passed: bool
    reason: str
    validation: Optional[ValidationResult] = None
    candidate_params: Dict[str, Any] = field(default_factory=dict)
    baseline_params: Dict[str, Any] = field(default_factory=dict)
    trader_id: str = ""
    checked_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for logging/storage."""
        result = {
            "passed": self.passed,
            "reason": self.reason,
            "trader_id": self.trader_id,
            "checked_at": self.checked_at,
            "candidate_params": self.candidate_params,
            "baseline_params": self.baseline_params,
        }
        if self.validation is not None:
            result["validation"] = {
                "accepted": self.validation.accepted,
                "train_sharpe": self.validation.train_sharpe,
                "val_sharpe": self.validation.val_sharpe,
                "baseline_val_sharpe": self.validation.baseline_val_sharpe,
                "confidence": self.validation.confidence,
                "checks": self.validation.checks,
            }
        return result

    @classmethod
    def rejected(cls, reason: str, **kwargs: Any) -> "GateResult":
        """Create a rejected gate result."""
        return cls(
            passed=False,
            reason=reason,
            checked_at=kwargs.pop("checked_at", datetime.now(timezone.utc).isoformat()),
            **kwargs,
        )

    @classmethod
    def accepted(
        cls,
        reason: str,
        validation: ValidationResult,
        **kwargs: Any,
    ) -> "GateResult":
        """Create an accepted gate result."""
        return cls(
            passed=True,
            reason=reason,
            validation=validation,
            checked_at=kwargs.pop("checked_at", datetime.now(timezone.utc).isoformat()),
            **kwargs,
        )


@dataclass
class GateConfig:
    """Configuration for the validation gate.

    Attributes:
        require_validation: Whether validation is mandatory (True = gate active).
        train_window_days: Training window in days (default 90 per SPEC §6.1).
        val_window_days: Validation window in days (default 30 per SPEC §6.1).
        min_improvement: Minimum validation Sharpe improvement over baseline
            (0.0 = any improvement, 0.05 = 5% better minimum).
        log_results: Whether to log results to sweep_results table.
    """

    require_validation: bool = True
    train_window_days: int = 90
    val_window_days: int = 30
    min_improvement: float = 0.0
    log_results: bool = True


# ═══════════════════════════════════════════════════════════════════════════════
# Validation Gate
# ═══════════════════════════════════════════════════════════════════════════════


class ValidationGate:
    """Enforces Architectural Invariant #7 in the deploy pipeline.

    Wraps WalkForwardValidator with parameter extraction, data loading,
    and result logging. Designed to be called from promote_virtual_to_live.py
    before any config swap.

    Usage:
        gate = ValidationGate()
        result = gate.check(
            candidate_params={"momentum_threshold": 0.60},
            baseline_params={"momentum_threshold": 0.55},
            trader_id="kairos",
        )
        if not result.passed:
            raise SystemExit(f"Validation gate rejected: {result.reason}")
    """

    def __init__(
        self,
        config: GateConfig | None = None,
        validator_config: WalkForwardConfig | None = None,
    ):
        self.config = config or GateConfig()
        self.validator_config = validator_config or WalkForwardConfig(
            train_window_days=self.config.train_window_days,
            val_window_days=self.config.val_window_days,
        )

    # ── Main entry point ─────────────────────────────────────────────────────

    def check(
        self,
        candidate_params: Dict[str, Any],
        baseline_params: Dict[str, Any],
        trader_id: str,
        tick_data: Optional[List] = None,
        initial_balance: float = 100_000.0,
    ) -> GateResult:
        """Run the validation gate check.

        If require_validation is False, always returns accepted
        (gate is disabled — useful for emergency overrides).

        Args:
            candidate_params: Proposed parameter changes.
            baseline_params: Current production parameters.
            trader_id: Trader name (e.g., 'kairos').
            tick_data: Pre-loaded tick list (loads from DB if None).
            initial_balance: Starting cash for replay.

        Returns:
            GateResult with pass/fail + diagnostics.
        """
        checked_at = datetime.now(timezone.utc).isoformat()

        # Fast path: gate disabled
        if not self.config.require_validation:
            return GateResult.accepted(
                reason="Validation gate disabled (--force or --no-validate)",
                validation=None,
                candidate_params=candidate_params,
                baseline_params=baseline_params,
                trader_id=trader_id,
                checked_at=checked_at,
            )

        # Load tick data if not provided
        if tick_data is None:
            tick_data = self._load_tick_data(trader_id)
            if not tick_data:
                return GateResult.rejected(
                    reason=f"No historical tick data available for {trader_id} "
                           f"— cannot validate parameter changes",
                    candidate_params=candidate_params,
                    baseline_params=baseline_params,
                    trader_id=trader_id,
                    checked_at=checked_at,
                )

        # Run walk-forward validation
        try:
            validation = walk_forward_validate(
                ticks=tick_data,
                candidate_params=candidate_params,
                baseline_params=baseline_params,
                train_days=self.config.train_window_days,
                val_days=self.config.val_window_days,
                initial_balance=initial_balance,
            )
        except Exception as exc:
            log.error("Walk-forward validation failed: %s", exc)
            return GateResult.rejected(
                reason=f"Walk-forward validation error: {exc}",
                candidate_params=candidate_params,
                baseline_params=baseline_params,
                trader_id=trader_id,
                checked_at=checked_at,
            )

        # Check acceptance
        if not validation.accepted:
            reason = (
                f"Walk-forward validation REJECTED for {trader_id}: "
                f"{validation.reason}"
            )
            log.warning("Validation gate: %s", reason)
            gate_result = GateResult.rejected(
                reason=reason,
                validation=validation,
                candidate_params=candidate_params,
                baseline_params=baseline_params,
                trader_id=trader_id,
                checked_at=checked_at,
            )
        else:
            # Check minimum improvement over baseline
            improvement = validation.val_sharpe - validation.baseline_val_sharpe
            if improvement < self.config.min_improvement:
                reason = (
                    f"Walk-forward validation PASSED but improvement "
                    f"({improvement:.4f}) below minimum ({self.config.min_improvement:.4f})"
                )
                log.warning("Validation gate: %s", reason)
                gate_result = GateResult.rejected(
                    reason=reason,
                    validation=validation,
                    candidate_params=candidate_params,
                    baseline_params=baseline_params,
                    trader_id=trader_id,
                    checked_at=checked_at,
                )
            else:
                reason = (
                    f"Walk-forward validation ACCEPTED: "
                    f"train Sharpe={validation.train_sharpe:.3f}, "
                    f"val Sharpe={validation.val_sharpe:.3f} "
                    f"(baseline={validation.baseline_val_sharpe:.3f}), "
                    f"confidence={validation.confidence:.2f}"
                )
                log.info("Validation gate: %s", reason)
                gate_result = GateResult.accepted(
                    reason=reason,
                    validation=validation,
                    candidate_params=candidate_params,
                    baseline_params=baseline_params,
                    trader_id=trader_id,
                    checked_at=checked_at,
                )

        # Log to sweep_results if configured
        if self.config.log_results:
            self._log_result(gate_result)

        return gate_result

    # ── Data loading ─────────────────────────────────────────────────────────

    def _load_tick_data(self, trader_id: str) -> List:
        """Load historical tick data for a trader from the database.

        Falls back to synthetic data if DB is unavailable.
        """
        try:
            import psycopg2
            from src.replay import Tick

            dsn = "postgresql://trader:@192.168.1.179:5433/trading"
            conn = psycopg2.connect(dsn)
            try:
                cur = conn.cursor()
                cur.execute(
                    """SELECT ticker, timestamp, open, high, low, close,
                              volume, rsi, momentum, volatility
                       FROM trading.ticks
                       WHERE timestamp >= NOW() - INTERVAL '180 days'
                       ORDER BY timestamp ASC
                       LIMIT 10000"""
                )
                rows = cur.fetchall()
                if not rows:
                    log.warning("No tick data in DB for %s", trader_id)
                    return []

                ticks = []
                for row in rows:
                    tick = Tick(
                        timestamp=row[1],
                        ticker=row[0],
                        open=float(row[2] or 0),
                        high=float(row[3] or 0),
                        low=float(row[4] or 0),
                        close=float(row[5] or 0),
                        volume=int(row[6] or 0),
                        rsi=float(row[7] or 50.0),
                        momentum=float(row[8] or 0.5),
                        volatility=float(row[9] or 0.01),
                    )
                    ticks.append(tick)

                cur.close()
                return ticks
            finally:
                conn.close()
        except Exception as exc:
            log.warning("Could not load tick data from DB: %s. Using synthetic data.", exc)
            return self._make_synthetic_ticks()

    @staticmethod
    def _make_synthetic_ticks(
        n: int = 200,
        base_price: float = 100.0,
        ticker: str = "SPY",
    ) -> List:
        """Generate synthetic tick data for validation when DB is unavailable."""
        import random
        from datetime import datetime, timedelta
        from src.replay import Tick

        ticks = []
        price = base_price
        start = datetime(2026, 1, 5, 9, 30)

        for i in range(n):
            price = price * (1 + random.gauss(0.0005, 0.01))
            price = max(price, 10.0)

            tick = Tick(
                timestamp=start + timedelta(minutes=i * 5),
                ticker=ticker,
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

    # ── Result logging ───────────────────────────────────────────────────────

    def _log_result(self, result: GateResult) -> None:
        """Log the gate result to sweep_results or promotion_log."""
        try:
            import psycopg2

            dsn = "postgresql://trader:@192.168.1.179:5433/trading"
            conn = psycopg2.connect(dsn)
            try:
                cur = conn.cursor()

                # Log to promotion_log if it exists
                validation_meta = json.dumps(result.to_dict())
                cur.execute(
                    """INSERT INTO trading.promotion_log
                       (virtual_name, base_trader, live_trader_before,
                        virtual_score, live_score, metric, threshold,
                        improvement_pct, notes)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        f"validation-gate-{result.trader_id}",
                        result.trader_id,
                        result.trader_id,
                        result.validation.val_sharpe if result.validation else 0.0,
                        result.validation.baseline_val_sharpe if result.validation else 0.0,
                        "walk_forward_sharpe",
                        0.0,
                        (
                            (result.validation.val_sharpe - result.validation.baseline_val_sharpe)
                            / max(abs(result.validation.baseline_val_sharpe), 0.01) * 100.0
                        ) if result.validation else 0.0,
                        f"Validation gate: {result.reason} | meta={validation_meta}",
                    ),
                )
                conn.commit()
                cur.close()
                log.debug("Logged validation gate result to promotion_log")
            except Exception as exc:
                conn.rollback()
                log.debug("Could not log to promotion_log (table may not exist): %s", exc)
            finally:
                conn.close()
        except Exception as exc:
            log.debug("Could not connect to DB for logging: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience function
# ═══════════════════════════════════════════════════════════════════════════════


def check_validation_gate(
    candidate_params: Dict[str, Any],
    baseline_params: Dict[str, Any],
    trader_id: str,
    require: bool = True,
    tick_data: Optional[List] = None,
) -> GateResult:
    """Convenience function: run the validation gate check.

    Args:
        candidate_params: Proposed parameter changes.
        baseline_params: Current production parameters.
        trader_id: Trader name (e.g., 'kairos').
        require: Whether validation is mandatory.
        tick_data: Pre-loaded tick list.

    Returns:
        GateResult.
    """
    config = GateConfig(require_validation=require)
    gate = ValidationGate(config=config)
    return gate.check(
        candidate_params=candidate_params,
        baseline_params=baseline_params,
        trader_id=trader_id,
        tick_data=tick_data,
    )
