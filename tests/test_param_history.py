"""Tests for src/param_history.py — Parameter History Tracking (#23)."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

from src.param_history import (
    ParamChange,
    ConvergenceResult,
    OscillationResult,
    ParameterReport,
    ParamHistory,
    record_gradient_step,
    record_prompt_sweep,
    get_nightly_summary,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Unit tests — ConvergenceResult
# ═══════════════════════════════════════════════════════════════════════════════


class TestConvergenceResult:
    def test_stability_score_perfect(self):
        """Zero variance → stability = 1.0."""
        r = ConvergenceResult(
            param_name="test", converging=True, stable_value=0.5,
            variance_last_half=0.0, trend_slope=0.0,
            recent_mean=0.5, recent_std=0.0, samples=10,
        )
        assert r.stability_score == 1.0

    def test_stability_score_moderate(self):
        """Moderate variance → score between 0 and 1."""
        r = ConvergenceResult(
            param_name="test", converging=True, stable_value=0.5,
            variance_last_half=0.005, trend_slope=0.001,
            recent_mean=0.5, recent_std=0.07, samples=10,
        )
        assert 0.4 < r.stability_score < 0.6

    def test_stability_score_unstable(self):
        """High variance → low stability."""
        r = ConvergenceResult(
            param_name="test", converging=False, stable_value=0.5,
            variance_last_half=0.02, trend_slope=0.05,
            recent_mean=0.5, recent_std=0.2, samples=10,
        )
        assert r.stability_score < 0.1


class TestOscillationResult:
    def test_oscillation_score_stable(self):
        """No direction changes → oscillation = 0."""
        r = OscillationResult(
            param_name="test", oscillating=False,
            cycles=0, amplitude=0.0, direction_changes=0, samples=10,
        )
        assert r.oscillation_score == 0.0

    def test_oscillation_score_high(self):
        """Many direction changes → high oscillation score."""
        r = OscillationResult(
            param_name="test", oscillating=True,
            cycles=5, amplitude=0.05, direction_changes=10, samples=10,
        )
        assert r.oscillation_score == 1.0

    def test_oscillation_score_low_samples(self):
        """Few samples → score clamped."""
        r = OscillationResult(
            param_name="test", oscillating=True,
            cycles=1, amplitude=0.02, direction_changes=2, samples=2,
        )
        assert r.oscillation_score == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Unit tests — ParamChange
# ═══════════════════════════════════════════════════════════════════════════════


class TestParamChange:
    def test_delta_positive(self):
        pc = ParamChange(
            id=1, agent_id="test", param_name="momentum_threshold",
            old_value=0.5, new_value=0.7, before_score=1.0, after_score=1.2,
            changed_at=datetime.now(), source="gradient_descent", reason="test",
            trader_id="kairos", score_metric="calmar",
        )
        assert pc.delta == pytest.approx(0.2)

    def test_delta_none_when_none_values(self):
        pc = ParamChange(
            id=1, agent_id="test", param_name="test_param",
            old_value=None, new_value=0.7, before_score=None, after_score=None,
            changed_at=datetime.now(), source="manual", reason="",
            trader_id="", score_metric="calmar",
        )
        assert pc.delta is None

    def test_score_delta(self):
        pc = ParamChange(
            id=1, agent_id="test", param_name="test_param",
            old_value=0.5, new_value=0.7, before_score=1.0, after_score=1.5,
            changed_at=datetime.now(), source="gradient_descent", reason="test",
            trader_id="kairos", score_metric="calmar",
        )
        assert pc.score_delta == 0.5

    def test_score_delta_none(self):
        pc = ParamChange(
            id=1, agent_id="test", param_name="test_param",
            old_value=0.5, new_value=0.7, before_score=None, after_score=1.5,
            changed_at=datetime.now(), source="manual", reason="",
            trader_id="", score_metric="calmar",
        )
        assert pc.score_delta is None


# ═══════════════════════════════════════════════════════════════════════════════
# Convergence detection
# ═══════════════════════════════════════════════════════════════════════════════


class TestConvergenceScore:
    def test_converging_params(self):
        """Parameters that settle toward a stable value are detected as converging."""
        ph = ParamHistory()

        # Values trending then settling: 0.50 → 0.51 → 0.52 → 0.52 → 0.52 → 0.52
        mock_records = []
        values = [0.50, 0.51, 0.52, 0.52, 0.52, 0.52, 0.52, 0.52, 0.52, 0.52]
        for i, v in enumerate(values):
            mock_records.append(ParamChange(
                id=i + 1, agent_id="default", param_name="momentum_threshold",
                old_value=v - 0.01 if i < 3 else v,
                new_value=v,
                before_score=1.0 + i * 0.01, after_score=None,
                changed_at=datetime.now() - timedelta(days=10 - i),
                source="gradient_descent", reason="", trader_id="kairos",
                score_metric="calmar",
            ))

        with patch.object(ph, "get_history", return_value=list(reversed(mock_records))):
            result = ph.convergence_score("momentum_threshold", window=10)

        assert result.converging is True
        assert result.samples == 10
        assert result.variance_last_half < 0.001
        assert result.stability_score > 0.8

    def test_diverging_params(self):
        """Parameters that trend strongly are NOT converging."""
        ph = ParamHistory()

        # Values trending strongly: 0.50 → 0.55 → 0.60 → ... → 0.95
        mock_records = []
        values = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
        for i, v in enumerate(values):
            mock_records.append(ParamChange(
                id=i + 1, agent_id="default", param_name="rsl_threshold",
                old_value=v - 0.05, new_value=v,
                before_score=1.0, after_score=None,
                changed_at=datetime.now() - timedelta(days=10 - i),
                source="gradient_descent", reason="", trader_id="kairos",
                score_metric="calmar",
            ))

        with patch.object(ph, "get_history", return_value=mock_records):
            result = ph.convergence_score("rsl_threshold", window=10)

        assert result.converging is False
        assert abs(result.trend_slope) > 0.01

    def test_too_few_samples(self):
        """Fewer than 4 samples → not converging."""
        ph = ParamHistory()
        mock_records = [
            ParamChange(id=1, agent_id="default", param_name="test",
                        old_value=0.5, new_value=0.6, before_score=1.0, after_score=None,
                        changed_at=datetime.now(), source="manual", reason="",
                        trader_id="", score_metric="calmar"),
        ]

        with patch.object(ph, "get_history", return_value=mock_records):
            result = ph.convergence_score("test", window=10)

        assert result.converging is False
        assert result.samples == 1

    def test_all_identical_values(self):
        """All values identical → converging with perfect stability."""
        ph = ParamHistory()
        mock_records = []
        for i in range(8):
            mock_records.append(ParamChange(
                id=i + 1, agent_id="default", param_name="stable_param",
                old_value=0.55, new_value=0.55,
                before_score=1.0, after_score=None,
                changed_at=datetime.now() - timedelta(days=8 - i),
                source="gradient_descent", reason="", trader_id="kairos",
                score_metric="calmar",
            ))

        with patch.object(ph, "get_history", return_value=mock_records):
            result = ph.convergence_score("stable_param", window=8)

        assert result.converging is True
        assert result.stability_score == 1.0
        assert result.stable_value == 0.55


# ═══════════════════════════════════════════════════════════════════════════════
# Oscillation detection
# ═══════════════════════════════════════════════════════════════════════════════


class TestOscillationCheck:
    def test_non_oscillating(self):
        """Monotonic trend → no oscillation."""
        ph = ParamHistory()
        mock_records = []
        values = [0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62, 0.64]
        for i, v in enumerate(values):
            mock_records.append(ParamChange(
                id=i + 1, agent_id="default", param_name="test",
                old_value=v - 0.02, new_value=v,
                before_score=1.0, after_score=None,
                changed_at=datetime.now() - timedelta(days=8 - i),
                source="gradient_descent", reason="", trader_id="kairos",
                score_metric="calmar",
            ))

        with patch.object(ph, "get_history", return_value=mock_records):
            result = ph.oscillation_check("test", window=8)

        assert result.oscillating is False
        assert result.direction_changes == 0

    def test_oscillating(self):
        """Values go up→down→up→down repeatedly → oscillating."""
        ph = ParamHistory()
        mock_records = []
        values = [0.50, 0.60, 0.50, 0.60, 0.50, 0.60, 0.50, 0.60, 0.50, 0.60]
        for i, v in enumerate(values):
            delta = 0.1 if i % 2 == 0 else -0.1
            mock_records.append(ParamChange(
                id=i + 1, agent_id="default", param_name="osc_param",
                old_value=v - delta, new_value=v,
                before_score=1.0, after_score=None,
                changed_at=datetime.now() - timedelta(days=10 - i),
                source="gradient_descent", reason="", trader_id="kairos",
                score_metric="calmar",
            ))

        with patch.object(ph, "get_history", return_value=mock_records):
            result = ph.oscillation_check("osc_param", window=10)

        assert result.oscillating is True
        assert result.direction_changes >= 4
        assert result.oscillation_score > 0.5

    def test_too_few_samples_no_oscillation(self):
        """Fewer than 4 samples → not oscillating."""
        ph = ParamHistory()
        mock_records = [
            ParamChange(id=1, agent_id="default", param_name="test",
                        old_value=0.5, new_value=0.6, before_score=1.0, after_score=None,
                        changed_at=datetime.now(), source="manual", reason="",
                        trader_id="", score_metric="calmar"),
            ParamChange(id=2, agent_id="default", param_name="test",
                        old_value=0.6, new_value=0.5, before_score=1.0, after_score=None,
                        changed_at=datetime.now(), source="manual", reason="",
                        trader_id="", score_metric="calmar"),
        ]

        with patch.object(ph, "get_history", return_value=mock_records):
            result = ph.oscillation_check("test", window=10)

        assert result.oscillating is False
        assert result.samples == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Query / get_history
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetHistory:
    def test_get_history_returns_records(self):
        """get_history returns list of ParamChange objects."""
        ph = ParamHistory()
        mock_rows = [(
            1, "default", "momentum_threshold", 0.55, 0.58, 1.0, 1.2,
            datetime.now(), "gradient_descent", "test", "kairos", "calmar",
        )]

        with patch("src.param_history.get_connection") as mock_conn:
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = mock_rows
            mock_conn.return_value.cursor.return_value = mock_cursor

            records = ph.get_history(param_name="momentum_threshold", days=30)

        assert len(records) == 1
        assert isinstance(records[0], ParamChange)
        assert records[0].param_name == "momentum_threshold"
        assert records[0].old_value == 0.55
        assert records[0].new_value == 0.58

    def test_get_history_empty(self):
        """Empty result set → empty list."""
        ph = ParamHistory()

        with patch("src.param_history.get_connection") as mock_conn:
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = []
            mock_conn.return_value.cursor.return_value = mock_cursor

            records = ph.get_history(days=1)

        assert records == []

    def test_get_history_db_error(self):
        """DB error → returns empty list gracefully."""
        ph = ParamHistory()

        with patch("src.param_history.get_connection") as mock_conn:
            mock_conn.side_effect = Exception("DB down")

            records = ph.get_history(days=1)

        assert records == []


# ═══════════════════════════════════════════════════════════════════════════════
# get_latest
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetLatest:
    def test_returns_latest_change(self):
        ph = ParamHistory()
        mock_row = (1, "default", "momentum_threshold", 0.55, 0.58, 1.0, 1.2,
                    datetime.now(), "gradient_descent", "test", "kairos", "calmar")

        with patch("src.param_history.get_connection") as mock_conn:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = mock_row
            mock_conn.return_value.cursor.return_value = mock_cursor

            result = ph.get_latest("momentum_threshold", trader_id="kairos")

        assert result is not None
        assert result.param_name == "momentum_threshold"
        assert result.new_value == 0.58

    def test_returns_none_when_no_record(self):
        ph = ParamHistory()

        with patch("src.param_history.get_connection") as mock_conn:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = None
            mock_conn.return_value.cursor.return_value = mock_cursor

            result = ph.get_latest("unknown_param")

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Generate report
# ═══════════════════════════════════════════════════════════════════════════════


class TestGenerateReport:
    def test_empty_report(self):
        """No records → empty report with one recommendation."""
        ph = ParamHistory()

        with patch.object(ph, "get_history", return_value=[]):
            report = ph.generate_report(trader_id="kairos", days=30)

        assert report.total_changes == 0
        assert report.params_analyzed == 0
        assert len(report.recommendations) == 1

    def test_report_with_converging_params(self):
        """Report includes converging parameter analysis."""
        ph = ParamHistory()

        mock_records = []
        for i in range(8):
            mock_records.append(ParamChange(
                id=i + 1, agent_id="default", param_name="momentum_threshold",
                old_value=0.55, new_value=0.55,
                before_score=1.0 + i * 0.001, after_score=None,
                changed_at=datetime.now() - timedelta(hours=8 - i),
                source="gradient_descent", reason="", trader_id="kairos",
                score_metric="calmar",
            ))

        with patch.object(ph, "get_history", return_value=mock_records):
            report = ph.generate_report(trader_id="kairos", days=1)

        assert report.total_changes == 8
        assert report.params_analyzed == 1
        assert len(report.converging_params) == 1
        assert report.converging_params[0].stability_score == 1.0

    def test_report_with_oscillating_params(self):
        """Report includes oscillating parameter analysis."""
        ph = ParamHistory()

        mock_records = []
        values = [0.50, 0.60, 0.50, 0.60, 0.50, 0.60, 0.50, 0.60]
        for i, v in enumerate(values):
            mock_records.append(ParamChange(
                id=i + 1, agent_id="default", param_name="unstable_param",
                old_value=v - 0.1 if i % 2 == 0 else v + 0.1,
                new_value=v,
                before_score=1.0, after_score=None,
                changed_at=datetime.now() - timedelta(hours=8 - i),
                source="gradient_descent", reason="", trader_id="kairos",
                score_metric="calmar",
            ))

        with patch.object(ph, "get_history", return_value=mock_records):
            report = ph.generate_report(trader_id="kairos", days=1)

        assert len(report.oscillating_params) == 1
        assert "oscill" in report.recommendations[0].lower()

    def test_report_score_improvements(self):
        """Report tracks per-parameter score improvements."""
        ph = ParamHistory()

        mock_records = [
            ParamChange(id=1, agent_id="default", param_name="a",
                        old_value=0.5, new_value=0.6, before_score=1.0, after_score=1.2,
                        changed_at=datetime.now(), source="gradient_descent", reason="",
                        trader_id="kairos", score_metric="calmar"),
            ParamChange(id=2, agent_id="default", param_name="a",
                        old_value=0.6, new_value=0.65, before_score=1.2, after_score=1.35,
                        changed_at=datetime.now(), source="gradient_descent", reason="",
                        trader_id="kairos", score_metric="calmar"),
        ]

        with patch.object(ph, "get_history", return_value=mock_records):
            report = ph.generate_report(trader_id="kairos", days=1)

        assert "a" in report.score_improvements
        assert report.score_improvements["a"] == pytest.approx(0.175)
        assert report.net_improvement == pytest.approx(0.35)

    def test_summary_str(self):
        """summary_str produces readable output."""
        ph = ParamHistory()

        mock_records = []
        for i in range(8):
            mock_records.append(ParamChange(
                id=i + 1, agent_id="default", param_name="stable",
                old_value=0.5, new_value=0.5,
                before_score=1.0, after_score=1.1,
                changed_at=datetime.now() - timedelta(hours=8 - i),
                source="gradient_descent", reason="", trader_id="kairos",
                score_metric="calmar",
            ))

        with patch.object(ph, "get_history", return_value=mock_records):
            report = ph.generate_report(trader_id="kairos", days=1)
            summary = ph.summary_str(report)

        assert "📊 Parameter History Report" in summary
        assert "kairos" in summary
        assert "stable" in summary


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience functions
# ═══════════════════════════════════════════════════════════════════════════════


class TestRecordGradientStep:
    def test_records_with_context(self):
        """record_gradient_step records with gradient descent metadata."""
        with patch("src.param_history.ParamHistory.record_change") as mock_record:
            mock_record.return_value = 42
            record_id = record_gradient_step(
                param_name="momentum_threshold",
                old_value=0.55, new_value=0.58,
                before_score=1.2, trader_id="kairos",
                learning_rate=0.01, gradient=0.5,
            )

        assert record_id == 42
        call_kwargs = mock_record.call_args.kwargs
        assert call_kwargs["source"] == "gradient_descent"
        assert "gradient" in call_kwargs["reason"]


class TestRecordPromptSweep:
    def test_records_prompt_sweep(self):
        """record_prompt_sweep records with prompt sweep metadata."""
        with patch("src.param_history.ParamHistory.record_change") as mock_record:
            mock_record.return_value = 99
            record_id = record_prompt_sweep(
                param_name="momentum_threshold",
                old_value=0.55, new_value=0.60,
                before_score=1.0, after_score=1.3,
                trader_id="kairos", variant_id="variant-047",
            )

        assert record_id == 99
        call_kwargs = mock_record.call_args.kwargs
        assert call_kwargs["source"] == "prompt_sweep"
        assert "variant-047" in call_kwargs["reason"]


class TestGetNightlySummary:
    def test_returns_summary_string(self):
        """get_nightly_summary returns a formatted string."""
        with patch("src.param_history.ParamHistory.generate_report") as mock_gen:
            mock_report = MagicMock()
            mock_report.trader_id = "kairos"
            mock_report.period_days = 1
            mock_report.total_changes = 5
            mock_report.params_analyzed = 3
            mock_report.net_improvement = 0.15
            mock_report.converging_params = []
            mock_report.oscillating_params = []
            mock_report.score_improvements = {"a": 0.1}
            mock_report.recommendations = ["All stable"]
            mock_gen.return_value = mock_report

            summary = get_nightly_summary(trader_id="kairos", days=1)

        assert "📊" in summary
        assert "kairos" in summary
        assert "5" in summary
        mock_gen.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_nan_values_dont_break_convergence(self):
        """NaN values are safely handled in convergence analysis."""
        ph = ParamHistory()
        mock_records = []
        values = [0.5, 0.6, None, 0.55, 0.55]  # None values filtered out
        for i, v in enumerate(values):
            mock_records.append(ParamChange(
                id=i + 1, agent_id="default", param_name="test",
                old_value=0.5, new_value=v,
                before_score=1.0, after_score=None,
                changed_at=datetime.now() - timedelta(days=5 - i),
                source="manual", reason="", trader_id="",
                score_metric="calmar",
            ))

        with patch.object(ph, "get_history", return_value=list(reversed(mock_records))):
            result = ph.convergence_score("test", window=5)

        assert result.samples == 4  # None filtered out
        assert result.converging is True  # stable after NaN

    def test_convergence_with_trader_filter(self):
        """Convergence checks respect trader_id filter."""
        ph = ParamHistory()
        mock_records = []
        values = [0.50, 0.52, 0.54, 0.56, 0.58, 0.58, 0.58, 0.58]
        for i, v in enumerate(values):
            mock_records.append(ParamChange(
                id=i + 1, agent_id="default", param_name="test",
                old_value=v - 0.02, new_value=v,
                before_score=1.0, after_score=None,
                changed_at=datetime.now() - timedelta(days=8 - i),
                source="gradient_descent", reason="", trader_id="kairos",
                score_metric="calmar",
            ))

        with patch.object(ph, "get_history", return_value=list(reversed(mock_records))):
            result = ph.convergence_score("test", window=8, trader_id="kairos")

        assert result.converging is True

    def test_oscillation_with_empty_values(self):
        """All-None values → no oscillation."""
        ph = ParamHistory()
        mock_records = [
            ParamChange(id=i + 1, agent_id="default", param_name="test",
                        old_value=None, new_value=None,
                        before_score=None, after_score=None,
                        changed_at=datetime.now(), source="manual", reason="",
                        trader_id="", score_metric="calmar")
            for i in range(5)
        ]

        with patch.object(ph, "get_history", return_value=mock_records):
            result = ph.oscillation_check("test", window=5)

        assert result.oscillating is False
        assert result.samples == 5

    def test_parameter_report_handles_mixed_convergence_oscillation(self):
        """Report can handle both converging and oscillating params simultaneously."""
        ph = ParamHistory()

        param_a = []
        for i in range(8):
            param_a.append(ParamChange(
                id=i + 1, agent_id="default", param_name="converging_a",
                old_value=0.5, new_value=0.5,
                before_score=1.0, after_score=1.01,
                changed_at=datetime.now() - timedelta(hours=8 - i),
                source="gradient_descent", reason="", trader_id="kairos",
                score_metric="calmar",
            ))

        param_b = []
        values_b = [0.50, 0.55, 0.50, 0.55, 0.50, 0.55, 0.50, 0.55]
        for i, v in enumerate(values_b):
            param_b.append(ParamChange(
                id=i + 9, agent_id="default", param_name="oscillating_b",
                old_value=v - 0.05, new_value=v,
                before_score=1.0, after_score=None,
                changed_at=datetime.now() - timedelta(hours=8 - i),
                source="gradient_descent", reason="", trader_id="kairos",
                score_metric="calmar",
            ))

        all_records = param_a + param_b

        def mock_get_history(param_name=None, trader_id=None, source=None,
                             days=30, limit=100):
            if param_name == "converging_a":
                return list(reversed(param_a))
            elif param_name == "oscillating_b":
                return list(reversed(param_b))
            return list(reversed(all_records))

        with patch.object(ph, "get_history", side_effect=mock_get_history):
            report = ph.generate_report(trader_id="kairos", days=1)

        assert report.total_changes == 16
        assert report.params_analyzed == 2
        assert len(report.converging_params) >= 1
        assert len(report.oscillating_params) >= 1
