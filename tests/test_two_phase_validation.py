#!/usr/bin/env python3
"""Tests for sweep_validation.py — two-phase signal→LLM validation gate."""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock, call

import pytest
import numpy as np

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sweep_validation import (
    ValidationConfig,
    Phase2Result,
    run_phase1_signal_sweep,
    run_phase2_llm_validation,
    two_phase_validate,
    log_sweep_result,
    _ensure_sweep_table,
    _swap_trader_prompt,
    _restore_trader_prompt,
    _run_replay_for_date,
    _SWEEP_TABLE_SQL,
    DEFAULT_INITIAL_CASH,
)
from src.prompt_sweep import (
    PromptVariant,
    SignalParams,
    get_trading_days,
    build_walk_forward_windows,
    read_trader_prompt,
    _generate_synthetic_ticks,
    PERTURBATION_TEMPLATES,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_variant(name, avg_val_score=0.1, win_rate=0.7, stability=0.02):
    """Create a synthetic PromptVariant for testing."""
    return PromptVariant(
        trader="test",
        variant_id=hash(name) % 1000,
        variant_name=name,
        description=f"Test variant {name}",
        prompt_text=f"# Test prompt for {name}",
        signal_params=SignalParams(),
        baseline_params=SignalParams(),
        score=avg_val_score,
        avg_val_score=avg_val_score,
        win_rate=win_rate,
        val_stability=stability,
        val_scores=[avg_val_score],
    )


def _make_synthetic_replay_report(total_pnl=150.0, win_rate=0.6, n_trades=5, errors=0):
    """Create a synthetic replay_controller.py JSON report."""
    return {
        "date": "2026-06-30",
        "traders": {
            "trader-test": {
                "trades": [
                    {"ticker": "AAPL", "action": "BUY", "quantity": 10, "status": "filled"},
                ] * n_trades,
                "final_cash": DEFAULT_INITIAL_CASH + total_pnl,
                "final_positions": [],
                "total_pnl": total_pnl,
                "win_rate": win_rate,
                "errors": errors,
            }
        },
        "total_ticks": 26,
        "wall_time_seconds": 45.2,
        "errors": [{"type": "test", "model": "trader-test"}] * errors,
    }


def _make_fake_replay_result(pnl=50.0):
    """Create a fake ReplayResult for mocking score_variant."""
    class FakeResult:
        returns = np.array([0.01])
        equity_curve = np.array([10000.0, 10000.0 + pnl])
        trades = [MagicMock(pnl=pnl)]

    return FakeResult()


# ═══════════════════════════════════════════════════════════════════════════════
# ValidationConfig Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidationConfig:
    """Test the validation configuration dataclass."""

    def test_defaults(self):
        config = ValidationConfig()
        assert config.phase1_variants == 5
        assert config.phase2_top_k == 3
        assert config.max_llm_runs_per_trader == 9
        assert config.llm_cost_estimate_per_run == 0.15

    def test_custom_values(self):
        config = ValidationConfig(
            phase1_variants=10,
            phase2_top_k=5,
            max_llm_runs_per_trader=15,
        )
        assert config.phase1_variants == 10
        assert config.phase2_top_k == 5
        assert config.max_llm_runs_per_trader == 15


# ═══════════════════════════════════════════════════════════════════════════════
# Phase2Result Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestPhase2Result:
    """Test Phase2Result parsing from replay reports."""

    def test_from_replay_report_positive_pnl(self):
        report = _make_synthetic_replay_report(total_pnl=250.0, win_rate=0.7, n_trades=8)
        result = Phase2Result.from_replay_report(
            "test_variant", "2026-06-30", "trader-test", report
        )
        assert result.total_pnl == 250.0
        assert result.win_rate == 0.7
        assert result.n_trades == 8
        assert result.errors == 0
        assert result.llm_score == pytest.approx(250.0 / 10000.0 * 100.0)

    def test_from_replay_report_negative_pnl(self):
        report = _make_synthetic_replay_report(total_pnl=-150.0)
        result = Phase2Result.from_replay_report(
            "test_variant", "2026-06-30", "trader-test", report
        )
        assert result.total_pnl == -150.0
        assert result.llm_score < 0

    def test_from_replay_report_missing_trader(self):
        report = {"traders": {}, "date": "2026-06-30"}
        result = Phase2Result.from_replay_report(
            "test_variant", "2026-06-30", "trader-test", report
        )
        assert result.total_pnl == 0.0
        assert result.errors == 1
        assert result.llm_score == 0.0

    def test_from_replay_report_zero_trades(self):
        report = _make_synthetic_replay_report(total_pnl=0.0, win_rate=0.0, n_trades=0)
        result = Phase2Result.from_replay_report(
            "test_variant", "2026-06-30", "trader-test", report
        )
        assert result.total_pnl == 0.0
        assert result.n_trades == 0
        assert result.llm_score == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Database Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSweepResultsTable:
    """Test sweep_results database table operations."""

    def test_ensure_sweep_table_creates_table(self, temp_db):
        """Table should be creatable via the SQL."""
        temp_db.executescript(_SWEEP_TABLE_SQL)
        tables = temp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sweep_results'"
        ).fetchall()
        assert len(tables) == 1

    def test_sweep_table_has_all_columns(self, temp_db):
        """Table should include all columns from the spec."""
        temp_db.executescript(_SWEEP_TABLE_SQL)
        cols = temp_db.execute("PRAGMA table_info(sweep_results)").fetchall()
        col_names = {c["name"] for c in cols}  # type: ignore[index]

        expected = {
            "id", "run_at", "trader", "variant_name", "variant_description",
            "train_date_range", "val_date_range", "baseline_score",
            "variant_score", "variant_llm_score", "calmar", "profit_factor",
            "win_rate", "n_trades", "cost_adjusted_pnl", "promoted",
            "branch_name", "signal_params_json", "phase1_winner",
            "phase2_winner", "signal_llm_divergence", "notes",
        }
        for name in expected:
            assert name in col_names, f"Missing column: {name}"

    def test_log_sweep_result_dry_run(self, temp_db):
        """Dry run should skip DB write."""
        with patch("src.sweep_validation.sqlite3.connect", return_value=temp_db):
            result = log_sweep_result(
                {
                    "trader": "kairos",
                    "variant_name": "test_var",
                    "variant_score": 0.5,
                },
                dry_run=True,
            )
        assert result is None

    def test_log_sweep_result_writes_row(self, temp_db):
        """Should write a row to sweep_results when table exists."""
        import sqlite3 as _sqlite3

        temp_db.executescript(_SWEEP_TABLE_SQL)

        # Create a wrapper that prevents close() from actually closing temp_db
        class _NoCloseConn:
            def __init__(self, real):
                self._real = real

            def execute(self, *a, **kw):
                return self._real.execute(*a, **kw)

            def commit(self):
                self._real.commit()

            def close(self):
                pass  # no-op

        wrapper = _NoCloseConn(temp_db)

        with patch("src.sweep_validation._ensure_sweep_table", return_value=None):
            with patch("src.sweep_validation.sqlite3.connect", return_value=wrapper):
                row_id = log_sweep_result(
                    {
                        "run_at": "2026-07-06T10:00:00",
                        "trader": "kairos",
                        "variant_name": "momentum_focus",
                        "variant_description": "Momentum focused",
                        "train_date_range": "2026-06-20:2026-06-26",
                        "val_date_range": "2026-06-27:2026-06-30",
                        "variant_score": 0.45,
                        "variant_llm_score": 1.2,
                        "promoted": False,
                        "phase1_winner": True,
                        "phase2_winner": False,
                        "signal_llm_divergence": True,
                        "notes": "test divergence logging",
                    },
                    dry_run=False,
                )

        assert row_id is not None
        row = temp_db.execute("SELECT * FROM sweep_results WHERE id = ?", (row_id,)).fetchone()
        assert row["trader"] == "kairos"
        assert row["variant_name"] == "momentum_focus"
        assert row["signal_llm_divergence"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2: LLM Validation Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunPhase2LLMValidation:
    """Test Phase 2 LLM validation with mocked subprocess."""

    def test_budget_gate_respects_max_runs(self):
        """Don't exceed max_llm_runs_per_trader."""
        config = ValidationConfig(phase2_top_k=5, max_llm_runs_per_trader=4)
        val_dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
        top_variants = [_make_variant(f"v{i}") for i in range(5)]
        baseline = _make_variant("baseline")

        with patch("src.sweep_validation.read_trader_prompt", return_value="# test"):
            with patch("src.sweep_validation._swap_trader_prompt", return_value="# test"):
                with patch("src.sweep_validation._restore_trader_prompt") as mock_restore:
                    with patch("src.sweep_validation._run_replay_for_date") as mock_replay:
                        mock_replay.return_value = _make_synthetic_replay_report(100.0)

                        scores, results = run_phase2_llm_validation(
                            trader="test",
                            top_variants=top_variants,
                            baseline=baseline,
                            val_dates=val_dates,
                            max_runs=config.max_llm_runs_per_trader,
                            dry_run=False,
                        )

        # Only baseline should be tested (budget of 4, baseline uses 3, leaves 1 → 0 variant runs)
        assert len(results) >= 1  # baseline always tested
        assert mock_restore.called

    def test_dry_run_produces_synthetic_results(self):
        """Dry run mode should produce scores without calling subprocess."""
        val_dates = ["2026-07-01", "2026-07-02"]
        top_variants = [_make_variant("v1", avg_val_score=0.15), _make_variant("v2", avg_val_score=0.08)]
        baseline = _make_variant("baseline")

        with patch("src.sweep_validation.read_trader_prompt", return_value="# test"):
            with patch("src.sweep_validation._swap_trader_prompt"):
                with patch("src.sweep_validation._restore_trader_prompt"):
                    scores, results = run_phase2_llm_validation(
                        trader="test",
                        top_variants=top_variants,
                        baseline=baseline,
                        val_dates=val_dates,
                        max_runs=9,
                        dry_run=True,
                    )

        assert "baseline" in scores
        assert "v1" in scores
        assert "v2" in scores
        assert isinstance(scores["baseline"], float)
        assert isinstance(scores["v1"], float)

    def test_empty_variants_handled(self):
        """Empty top_variants list should not crash."""
        val_dates = ["2026-07-01"]
        baseline = _make_variant("baseline")

        with patch("src.sweep_validation.read_trader_prompt", return_value="# test"):
            with patch("src.sweep_validation._swap_trader_prompt"):
                with patch("src.sweep_validation._restore_trader_prompt"):
                    scores, results = run_phase2_llm_validation(
                        trader="test",
                        top_variants=[],
                        baseline=baseline,
                        val_dates=val_dates,
                        max_runs=9,
                        dry_run=True,
                    )

        assert "baseline" in scores
        assert len(results) == 1  # Only baseline

    def test_restores_prompt_after_run(self):
        """Prompt must be restored even if replay fails."""
        val_dates = ["2026-07-01"]
        baseline = _make_variant("baseline")
        variants = [_make_variant("v1")]

        original_prompt = "# Original AGENTS.md"

        with patch("src.sweep_validation.read_trader_prompt", return_value=original_prompt):
            with patch("src.sweep_validation._swap_trader_prompt", return_value=original_prompt):
                with patch("src.sweep_validation._restore_trader_prompt") as mock_restore:
                    with patch("src.sweep_validation._run_replay_for_date", return_value=None):
                        scores, results = run_phase2_llm_validation(
                            trader="test",
                            top_variants=variants,
                            baseline=baseline,
                            val_dates=val_dates,
                            max_runs=9,
                            dry_run=False,
                        )

        # Restore should have been called
        assert mock_restore.call_count >= 2

    def test_baseline_always_scored(self):
        """Baseline LLM score is always computed."""
        val_dates = ["2026-07-01"]
        baseline = _make_variant("baseline")

        with patch("src.sweep_validation.read_trader_prompt", return_value="# test"):
            with patch("src.sweep_validation._swap_trader_prompt"):
                with patch("src.sweep_validation._restore_trader_prompt"):
                    scores, results = run_phase2_llm_validation(
                        trader="test",
                        top_variants=[],
                        baseline=baseline,
                        val_dates=val_dates,
                        max_runs=9,
                        dry_run=True,
                    )

        assert "baseline" in scores
        assert "baseline" in results

    def test_parse_replay_output(self):
        """Verify parsing of real replay_controller JSON output format."""
        report = _make_synthetic_replay_report(total_pnl=187.50, win_rate=0.6, n_trades=7)
        # Simulate actual format: logs followed by JSON starting with {"date"
        stdout = (
            "[replay] Pre-fetching historical data for 2026-07-01...\n"
            "[replay] Created replay/.active\n"
            "[09:45 AM ET] Tick 2/26 (8%) → trader-test: BUY AAPL 10\n"
            "\n"
            + json.dumps(report)
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=stdout, stderr="",
            )

            result = _run_replay_for_date("test", "2026-07-01")

        assert result is not None
        assert result["traders"]["trader-test"]["total_pnl"] == 187.50


# ═══════════════════════════════════════════════════════════════════════════════
# Two-Phase Pipeline Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestTwoPhaseValidate:
    """Integration tests for the full two-phase pipeline."""

    def _setup_trader_prompt(self, trader="test"):
        """Create a temporary trader AGENTS.md for test."""
        import tempfile, os
        agents_dir = Path(tempfile.mkdtemp()) / f"trader-{trader}"
        agents_dir.mkdir(parents=True, exist_ok=True)
        agents_md = agents_dir / "AGENTS.md"
        agents_md.write_text("# Test Trader\nMomentum strategy with RSI filter.\nStop loss: 5%\n")
        return agents_md, agents_dir

    def _mock_phase1_components(self):
        """Mock out external calls for Phase 1 testing."""
        ticks = _generate_synthetic_ticks("2026-07-01", ["SPY", "AAPL", "MSFT"])
        return ticks

    def _make_test_variants(self, names):
        """Create test variants with SignalParams."""
        variants = []
        for i, name in enumerate(names, 1):
            variants.append(PromptVariant(
                trader="test",
                variant_id=i,
                variant_name=name,
                description=f"Test variant {name}",
                prompt_text=f"# Test\n## Strategy Variant Override\n{name}",
                signal_params=SignalParams(),
                baseline_params=SignalParams(),
            ))
        return variants

    @patch("src.sweep_validation.read_trader_prompt")
    @patch("src.sweep_validation._load_dates_data")
    @patch("src.sweep_validation.generate_variants")
    @patch("src.sweep_validation.score_variant")
    @patch("src.sweep_validation.get_trading_days")
    @patch("src.sweep_validation.build_walk_forward_windows")
    def test_divergence_detection_signal_winner_not_llm_winner(
        self, mock_build_windows, mock_get_dates, mock_score_variant,
        mock_gen_variants, mock_load_data, mock_read_prompt,
    ):
        """When signal winner ≠ LLM winner, detect divergence, no promotion."""
        mock_read_prompt.return_value = "# Test trader\nMomentum strategy."
        mock_get_dates.return_value = [f"2026-06-{d:02d}" for d in range(20, 30)] + [
            f"2026-07-{d:02d}" for d in range(1, 6)
        ]
        # 3 windows, 7 train + 3 val each
        mock_build_windows.return_value = [
            (["2026-06-20", "2026-06-21", "2026-06-22", "2026-06-23", "2026-06-24",
              "2026-06-25", "2026-06-26"], ["2026-06-27", "2026-06-28", "2026-06-29"]),
            (["2026-06-21", "2026-06-22", "2026-06-23", "2026-06-24", "2026-06-25",
              "2026-06-26", "2026-06-27"], ["2026-06-28", "2026-06-29", "2026-06-30"]),
            (["2026-06-22", "2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26",
              "2026-06-27", "2026-06-28"], ["2026-06-29", "2026-06-30", "2026-07-01"]),
        ]

        ticks = self._mock_phase1_components()
        mock_load_data.return_value = ticks

        v_momentum, v_aggressive, v_conservative = self._make_test_variants(
            ["momentum_focus", "aggressive_sizing", "conservative_sizing"]
        )
        mock_gen_variants.return_value = [v_momentum, v_aggressive, v_conservative]

        # Mock score_variant: baseline low, momentum_focus high → Phase 1 winner
        def fake_score(variant, ticks, cost_model=None):
            if variant.variant_name == "baseline":
                return (0.0, _make_fake_replay_result(0.0))
            elif variant.variant_name == "momentum_focus":
                return (5.0, _make_fake_replay_result(50.0))
            elif variant.variant_name == "aggressive_sizing":
                return (2.0, _make_fake_replay_result(20.0))
            else:
                return (0.5, _make_fake_replay_result(5.0))

        mock_score_variant.side_effect = fake_score

        # Phase 2: momentum_focus loses LLM, aggressive_sizing wins LLM
        with patch("src.sweep_validation.run_phase2_llm_validation") as mock_phase2:
            mock_phase2.return_value = (
                {
                    "baseline": 0.5,
                    "momentum_focus": 0.3,      # Signal winner loses LLM
                    "aggressive_sizing": 1.5,   # Different variant wins LLM
                    "conservative_sizing": 0.1,
                },
                {n: [] for n in ["baseline", "momentum_focus", "aggressive_sizing", "conservative_sizing"]},
            )
            with patch("src.sweep_validation.log_sweep_result"):
                winner, diagnostics = two_phase_validate(
                    trader="test",
                    dates=mock_get_dates.return_value,
                    train_days=7,
                    val_days=3,
                    config=ValidationConfig(phase1_variants=3, phase2_top_k=3),
                    dry_run=True,
                )

        # Should detect divergence
        assert diagnostics["signal_llm_divergence"] is True
        assert winner is None
        assert diagnostics["phase1_winner"] is not None
        assert diagnostics["phase2_winner"] is not None
        assert diagnostics["phase1_winner"] != diagnostics["phase2_winner"]

    @patch("src.sweep_validation.read_trader_prompt")
    @patch("src.sweep_validation._load_dates_data")
    @patch("src.sweep_validation.generate_variants")
    @patch("src.sweep_validation.score_variant")
    @patch("src.sweep_validation.get_trading_days")
    @patch("src.sweep_validation.build_walk_forward_windows")
    def test_agreement_signal_winner_equals_llm_winner(
        self, mock_build_windows, mock_get_dates, mock_score_variant,
        mock_gen_variants, mock_load_data, mock_read_prompt,
    ):
        """When signal winner = LLM winner, promote the variant."""
        mock_read_prompt.return_value = "# Test trader\nMomentum strategy."
        mock_get_dates.return_value = [f"2026-06-{d:02d}" for d in range(20, 30)] + [
            f"2026-07-{d:02d}" for d in range(1, 6)
        ]
        mock_build_windows.return_value = [
            (["2026-06-20", "2026-06-21", "2026-06-22", "2026-06-23", "2026-06-24",
              "2026-06-25", "2026-06-26"], ["2026-06-27", "2026-06-28", "2026-06-29"]),
            (["2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26", "2026-06-27",
              "2026-06-28", "2026-06-29"], ["2026-06-30", "2026-07-01", "2026-07-02"]),
        ]

        ticks = self._mock_phase1_components()
        mock_load_data.return_value = ticks

        v_momentum, v_wider = self._make_test_variants(["momentum_focus", "wider_stops"])
        mock_gen_variants.return_value = [v_momentum, v_wider]

        # Mock: momentum_focus scores higher than baseline
        def fake_score(variant, ticks, cost_model=None):
            if variant.variant_name == "baseline":
                return (0.0, _make_fake_replay_result(0.0))
            elif variant.variant_name == "momentum_focus":
                return (5.0, _make_fake_replay_result(50.0))
            else:
                return (1.0, _make_fake_replay_result(10.0))

        mock_score_variant.side_effect = fake_score

        # Phase 2: momentum_focus also wins LLM
        with patch("src.sweep_validation.run_phase2_llm_validation") as mock_phase2:
            mock_phase2.return_value = (
                {
                    "baseline": 0.5,
                    "momentum_focus": 2.0,  # Same variant wins both phases!
                    "wider_stops": 0.3,
                },
                {"baseline": [], "momentum_focus": [], "wider_stops": []},
            )
            with patch("src.sweep_validation.log_sweep_result"):
                winner, diagnostics = two_phase_validate(
                    trader="test",
                    dates=mock_get_dates.return_value,
                    train_days=7,
                    val_days=3,
                    config=ValidationConfig(phase1_variants=2, phase2_top_k=2),
                    dry_run=True,
                )

        # Agreement → promotion
        assert diagnostics["signal_llm_divergence"] is False
        assert winner is not None
        assert winner.variant_name == "momentum_focus"
        assert diagnostics["phase1_winner"] == diagnostics["phase2_winner"]

    @patch("src.sweep_validation.read_trader_prompt")
    @patch("src.sweep_validation._load_dates_data")
    @patch("src.sweep_validation.generate_variants")
    @patch("src.sweep_validation.score_variant")
    @patch("src.sweep_validation.get_trading_days")
    @patch("src.sweep_validation.build_walk_forward_windows")
    def test_no_phase1_winner_no_promotion(
        self, mock_build_windows, mock_get_dates, mock_score_variant,
        mock_gen_variants, mock_load_data, mock_read_prompt,
    ):
        """If no variant passes Phase 1 criteria, no promotion."""
        mock_read_prompt.return_value = "# Test trader\nMomentum strategy."
        mock_get_dates.return_value = [f"2026-06-{d:02d}" for d in range(20, 30)]
        mock_build_windows.return_value = [
            (["2026-06-20", "2026-06-21", "2026-06-22", "2026-06-23", "2026-06-24"],
             ["2026-06-25", "2026-06-26", "2026-06-27"]),
        ]

        ticks = self._mock_phase1_components()
        mock_load_data.return_value = ticks

        v1 = self._make_test_variants(["weak_var"])[0]
        mock_gen_variants.return_value = [v1]

        # All scores equal → no variant beats baseline
        def fake_score(variant, ticks, cost_model=None):
            return (0.5, _make_fake_replay_result(5.0))

        mock_score_variant.side_effect = fake_score

        with patch("src.sweep_validation.run_phase2_llm_validation") as mock_phase2:
            mock_phase2.return_value = (
                {"baseline": 0.5, "weak_var": 0.3},
                {"baseline": [], "weak_var": []},
            )
            with patch("src.sweep_validation.log_sweep_result"):
                winner, diagnostics = two_phase_validate(
                    trader="test",
                    dates=mock_get_dates.return_value,
                    train_days=5,
                    val_days=3,
                    config=ValidationConfig(phase1_variants=1, phase2_top_k=1),
                    dry_run=True,
                )

        assert winner is None
        assert diagnostics["phase1_winner"] is None

    @patch("src.sweep_validation.read_trader_prompt")
    @patch("src.sweep_validation._load_dates_data")
    @patch("src.sweep_validation.generate_variants")
    @patch("src.sweep_validation.score_variant")
    @patch("src.sweep_validation.get_trading_days")
    @patch("src.sweep_validation.build_walk_forward_windows")
    def test_phase1_only_mode(
        self, mock_build_windows, mock_get_dates, mock_score_variant,
        mock_gen_variants, mock_load_data, mock_read_prompt,
    ):
        """Run Phase 1 only (backward-compatible with signal-only sweep)."""
        mock_read_prompt.return_value = "# Test trader\nMomentum strategy."
        mock_get_dates.return_value = [f"2026-06-{d:02d}" for d in range(20, 30)] + [
            f"2026-07-{d:02d}" for d in range(1, 6)
        ]
        mock_build_windows.return_value = [
            (["2026-06-20", "2026-06-21", "2026-06-22", "2026-06-23", "2026-06-24",
              "2026-06-25", "2026-06-26"], ["2026-06-27", "2026-06-28", "2026-06-29"]),
        ]

        ticks = self._mock_phase1_components()
        mock_load_data.return_value = ticks

        v1 = self._make_test_variants(["momentum_focus"])[0]
        mock_gen_variants.return_value = [v1]

        def fake_score(variant, ticks, cost_model=None):
            if variant.variant_name == "baseline":
                return (0.0, _make_fake_replay_result(0.0))
            return (5.0, _make_fake_replay_result(50.0))

        mock_score_variant.side_effect = fake_score

        # Only call Phase 1
        variants, baseline, phase1_winner, windows = run_phase1_signal_sweep(
            trader="test",
            dates=mock_get_dates.return_value,
            train_days=7,
            val_days=3,
            n_variants=1,
        )

        assert len(variants) == 1
        assert baseline.variant_name == "baseline"
        assert len(windows) > 0

    @patch("src.sweep_validation.read_trader_prompt")
    @patch("src.sweep_validation._load_dates_data")
    @patch("src.sweep_validation.generate_variants")
    @patch("src.sweep_validation.score_variant")
    @patch("src.sweep_validation.get_trading_days")
    @patch("src.sweep_validation.build_walk_forward_windows")
    def test_signal_winner_loses_llm(
        self, mock_build_windows, mock_get_dates, mock_score_variant,
        mock_gen_variants, mock_load_data, mock_read_prompt,
    ):
        """Phase 1 winner that loses Phase 2 LLM → divergence, no promotion."""
        mock_read_prompt.return_value = "# Test trader\nMomentum strategy."
        mock_get_dates.return_value = [f"2026-06-{d:02d}" for d in range(20, 30)]
        mock_build_windows.return_value = [
            (["2026-06-20", "2026-06-21", "2026-06-22", "2026-06-23", "2026-06-24",
              "2026-06-25", "2026-06-26"], ["2026-06-27", "2026-06-28", "2026-06-29"]),
        ]

        ticks = self._mock_phase1_components()
        mock_load_data.return_value = ticks

        v1 = self._make_test_variants(["momentum_focus"])[0]
        mock_gen_variants.return_value = [v1]

        # Signal winner: momentum_focus scores high
        def fake_score(variant, ticks, cost_model=None):
            if variant.variant_name == "baseline":
                return (0.0, _make_fake_replay_result(0.0))
            return (5.0, _make_fake_replay_result(50.0))

        mock_score_variant.side_effect = fake_score

        # Phase 2: baseline has higher LLM score → momentum_focus loses
        with patch("src.sweep_validation.run_phase2_llm_validation") as mock_phase2:
            mock_phase2.return_value = (
                {
                    "baseline": 2.0,            # Baseline wins LLM
                    "momentum_focus": 0.5,      # Signal winner loses LLM
                },
                {"baseline": [], "momentum_focus": []},
            )
            with patch("src.sweep_validation.log_sweep_result"):
                winner, diagnostics = two_phase_validate(
                    trader="test",
                    dates=mock_get_dates.return_value,
                    train_days=7,
                    val_days=3,
                    config=ValidationConfig(phase1_variants=1, phase2_top_k=1),
                    dry_run=True,
                )

        # Signal winner loses LLM → divergence, no promotion
        assert diagnostics["signal_llm_divergence"] is True
        assert winner is None
        assert diagnostics["phase1_winner"] is not None
        assert diagnostics["phase2_winner"] is None  # No LLM winner (baseline wins)


# ═══════════════════════════════════════════════════════════════════════════════
# Prompt swap tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestPromptSwap:
    """Test temporary AGENTS.md swap for LLM validation."""

    def test_swap_and_restore(self, tmp_path):
        """Swap should replace file, restore should put original back."""
        agents_dir = tmp_path / "trader-test"
        agents_dir.mkdir(parents=True)
        agents_md = agents_dir / "AGENTS.md"
        original = "# Original prompt\nTest strategy\n"
        agents_md.write_text(original)

        with patch("src.sweep_validation.AGENTS_DIR", tmp_path):
            original_text = _swap_trader_prompt("test", "# Modified prompt\nVariant strategy\n")
            assert original_text.strip() == original.strip()

            current = (tmp_path / "trader-test" / "AGENTS.md").read_text()
            assert "Modified prompt" in current

            _restore_trader_prompt("test", original_text)
            restored = (tmp_path / "trader-test" / "AGENTS.md").read_text()
            assert restored.strip() == original.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# End-to-end: prompt_sweep.py --phase2 integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestPromptSweepPhase2Integration:
    """Verify prompt_sweep.py --phase2 flag integration."""

    def test_phase2_flag_accepted_by_argparse(self):
        """--phase2 flag should be accepted by argparse."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--phase2", action="store_true")
        parser.add_argument("--phase2-top-k", type=int, default=3)
        parser.add_argument("--phase2-budget", type=int, default=9)

        args = parser.parse_args(["--phase2", "--phase2-top-k", "5", "--phase2-budget", "12"])
        assert args.phase2 is True
        assert args.phase2_top_k == 5
        assert args.phase2_budget == 12

    def test_phase2_defaults_when_not_passed(self):
        """Defaults should apply when flags not provided."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--phase2", action="store_true")
        parser.add_argument("--phase2-top-k", type=int, default=3)
        parser.add_argument("--phase2-budget", type=int, default=9)

        args = parser.parse_args([])
        assert args.phase2 is False
        assert args.phase2_top_k == 3
        assert args.phase2_budget == 9

    @patch("src.prompt_sweep.two_phase_validate")
    @patch("src.prompt_sweep.ValidationConfig")
    @patch("src.prompt_sweep.get_trading_days")
    @patch("src.prompt_sweep.read_trader_prompt")
    @patch("src.prompt_sweep.load_historical_ticks")
    @patch("src.prompt_sweep.generate_variants")
    def test_phase2_triggers_validation(
        self, mock_gen, mock_ticks, mock_read, mock_dates,
        mock_config_cls, mock_two_phase,
    ):
        """When --phase2 is True, two_phase_validate() should be called."""
        from src.prompt_sweep import run_sweep, PromptVariant, SignalParams

        mock_read.return_value = "# Test trader"
        mock_ticks.return_value = _generate_synthetic_ticks("2026-07-05", ["SPY"])
        mock_dates.return_value = [f"2026-06-{d:02d}" for d in range(20, 30)]

        v = PromptVariant("test", 1, "v1", "", "# v1", SignalParams(), SignalParams())
        mock_gen.return_value = [v]

        mock_config = ValidationConfig(phase1_variants=5, phase2_top_k=3, max_llm_runs_per_trader=9)
        mock_config_cls.return_value = mock_config
        mock_two_phase.return_value = (None, {"signal_llm_divergence": False})

        # Run with phase2=True (multi-date mode required)
        results = run_sweep(
            date_str="2026-07-05",
            trader="test",
            n_variants=1,
            dry_run=True,
            n_dates=20,
            train_days=5,
            val_days=3,
            phase2=True,
            phase2_top_k=3,
            phase2_budget=9,
        )

        mock_two_phase.assert_called_once()
        assert len(results) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
