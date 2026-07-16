"""
Tests for scripts/nightly_trend_card.py — 7-night variant performance trends.

Tests the analysis functions (top-3 consistency, score trendlines, parameter
drift) with mocked sweep data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from scripts.nightly_trend_card import (
    compute_top3_consistency,
    compute_score_trendline,
    compute_param_drift,
    generate_markdown,
    _safe_float,
    _sparkline,
    _trend_arrow,
    get_7_night_data,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _make_row(
    trader_id: str = "kairos",
    variant_id: int = 1,
    params_hash: str = "abc123def456",
    objective_score: float = 0.5,
    calmar: float = 1.2,
    sortino: float = 1.5,
    profit_factor: float = 1.3,
    win_rate: float = 0.55,
    total_return_pct: float = 2.5,
    run_date_str: str = "2026-07-15T02:00:00+00:00",
) -> dict:
    """Create a mock sweep result row."""
    return {
        "trader_id": trader_id,
        "variant_id": variant_id,
        "params_hash": params_hash,
        "objective_score": objective_score,
        "calmar": calmar,
        "sortino": sortino,
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "total_return_pct": total_return_pct,
        "created_at": datetime.now(timezone.utc),
        "run_date": datetime.fromisoformat(run_date_str),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# _safe_float
# ═══════════════════════════════════════════════════════════════════════════════


class TestSafeFloat:
    def test_normal_float(self):
        assert _safe_float(3.14) == 3.14

    def test_int(self):
        assert _safe_float(42) == 42.0

    def test_decimal_string(self):
        assert _safe_float("3.14") == 3.14

    def test_none(self):
        assert _safe_float(None) is None

    def test_bad_string(self):
        assert _safe_float("nope") is None

    def test_zero(self):
        assert _safe_float(0) == 0.0
        assert _safe_float(0.0) == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# _sparkline
# ═══════════════════════════════════════════════════════════════════════════════


class TestSparkline:
    def test_empty(self):
        assert _sparkline([]) == "(no data)"

    def test_single_value(self):
        result = _sparkline([5.0], width=5)
        assert len(result) == 5
        # All same value → flat line (all ─)
        assert all(c == "─" for c in result)

    def test_all_same(self):
        result = _sparkline([3.0, 3.0, 3.0], width=5)
        assert all(c == "─" for c in result)

    def test_increasing(self):
        result = _sparkline([1.0, 2.0, 3.0], width=5)
        assert len(result) == 5
        # Should show increasing pattern (low chars → high chars)
        chars = "▁▂▃▄▅▆▇█"
        first_idx = chars.index(result[0])
        last_idx = chars.index(result[-1])
        assert first_idx <= last_idx

    def test_decreasing(self):
        result = _sparkline([3.0, 2.0, 1.0], width=5)
        chars = "▁▂▃▄▅▆▇█"
        first_idx = chars.index(result[0])
        last_idx = chars.index(result[-1])
        assert first_idx >= last_idx

    def test_many_values_sampled(self):
        # 100 values should be sampled down to width
        values = list(range(100))
        result = _sparkline(values, width=10)
        assert len(result) == 10

    def test_few_values_expanded(self):
        # 3 values expanded to width 20
        result = _sparkline([1.0, 5.0, 3.0], width=20)
        assert len(result) == 20

    def test_negative_values(self):
        result = _sparkline([-5.0, 0.0, 5.0], width=5)
        assert len(result) == 5
        chars = "▁▂▃▄▅▆▇█"
        first_idx = chars.index(result[0])
        last_idx = chars.index(result[-1])
        assert first_idx <= last_idx


# ═══════════════════════════════════════════════════════════════════════════════
# _trend_arrow
# ═══════════════════════════════════════════════════════════════════════════════


class TestTrendArrow:
    def test_strong_up(self):
        assert "↗" in _trend_arrow([1.0, 2.0, 3.0, 4.0])

    def test_strong_down(self):
        assert "↘" in _trend_arrow([4.0, 3.0, 2.0, 1.0])

    def test_flat(self):
        result = _trend_arrow([1.0, 1.0, 1.0, 1.0])
        assert "↘" in result or "↗" in result  # small change

    def test_single_value(self):
        assert _trend_arrow([5.0]) == "➖"

    def test_empty(self):
        assert _trend_arrow([]) == "➖"

    def test_mild_up(self):
        result = _trend_arrow([1.0, 1.1])
        assert "↗" in result

    def test_mild_down(self):
        result = _trend_arrow([1.1, 1.0])
        # ~9% down → red
        assert "↘" in result


# ═══════════════════════════════════════════════════════════════════════════════
# compute_top3_consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestTop3Consistency:
    def test_single_night_single_variant(self):
        rows = [_make_row(variant_id=1, objective_score=0.5)]
        result = compute_top3_consistency(rows)
        assert len(result) == 1
        assert result[0]["variant_id"] == 1
        assert result[0]["nights_in_top3"] == 1
        assert result[0]["night_count"] == 1
        assert result[0]["pct_in_top3"] == 100

    def test_single_night_many_variants_selects_top3(self):
        rows = [
            _make_row(variant_id=1, objective_score=0.9),
            _make_row(variant_id=2, objective_score=0.8),
            _make_row(variant_id=3, objective_score=0.7),
            _make_row(variant_id=4, objective_score=0.1),  # outside top 3
            _make_row(variant_id=5, objective_score=0.05),  # outside top 3
        ]
        result = compute_top3_consistency(rows)
        # Only variants 1, 2, 3 should be in result (top 3)
        variant_ids = {r["variant_id"] for r in result}
        assert variant_ids == {1, 2, 3}
        # All have 1 night in top-3
        for r in result:
            assert r["nights_in_top3"] == 1
            assert r["night_count"] == 1

    def test_multi_night_consistency(self):
        rows = [
            # Night 1: variant 1 wins
            _make_row(variant_id=1, objective_score=0.9, run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=2, objective_score=0.8, run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=3, objective_score=0.7, run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=4, objective_score=0.1, run_date_str="2026-07-15T02:00:00+00:00"),
            # Night 2: variant 1 wins again, 4 enters top-3
            _make_row(variant_id=1, objective_score=0.95, run_date_str="2026-07-14T02:00:00+00:00"),
            _make_row(variant_id=4, objective_score=0.85, run_date_str="2026-07-14T02:00:00+00:00"),
            _make_row(variant_id=2, objective_score=0.75, run_date_str="2026-07-14T02:00:00+00:00"),
            _make_row(variant_id=3, objective_score=0.6, run_date_str="2026-07-14T02:00:00+00:00"),
        ]
        result = compute_top3_consistency(rows)
        # variant 1 should be top-3 both nights
        v1 = next(r for r in result if r["variant_id"] == 1)
        assert v1["nights_in_top3"] == 2
        assert v1["night_count"] == 2
        assert v1["pct_in_top3"] == 100

        # variant 4 should be top-3 only second night
        v4 = next(r for r in result if r["variant_id"] == 4)
        assert v4["nights_in_top3"] == 1
        assert v4["pct_in_top3"] == 50

    def test_scores_aggregated_correctly(self):
        rows = [
            _make_row(variant_id=1, objective_score=0.5, run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=1, objective_score=0.7, run_date_str="2026-07-14T02:00:00+00:00"),
        ]
        result = compute_top3_consistency(rows)
        assert len(result) == 1
        assert result[0]["avg_score"] == 0.6
        assert result[0]["best_score"] == 0.7

    def test_empty_data(self):
        result = compute_top3_consistency([])
        assert result == []

    def test_null_scores_handled(self):
        rows = [
            _make_row(variant_id=1, objective_score=None, run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=2, objective_score=0.5, run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=3, objective_score=0.4, run_date_str="2026-07-15T02:00:00+00:00"),
        ]
        result = compute_top3_consistency(rows)
        # All 3 should be in result (only 3 variants, all top-3)
        assert len(result) == 3
        # variant 1 has no score → avg = 0.0
        v1 = next(r for r in result if r["variant_id"] == 1)
        assert v1["avg_score"] == 0.0
        assert v1["best_score"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# compute_score_trendline
# ═══════════════════════════════════════════════════════════════════════════════


class TestScoreTrendline:
    def test_single_night(self):
        rows = [
            _make_row(variant_id=1, objective_score=0.5, run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=2, objective_score=0.8, run_date_str="2026-07-15T02:00:00+00:00"),
        ]
        result = compute_score_trendline(rows)
        assert len(result) == 1
        assert result[0]["date"] == "2026-07-15"
        assert result[0]["best_score"] == 0.8
        assert result[0]["best_variant_id"] == 2
        assert result[0]["run_count"] == 2

    def test_multi_night_chronological(self):
        rows = [
            _make_row(variant_id=1, objective_score=0.5, run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=2, objective_score=0.6, run_date_str="2026-07-14T02:00:00+00:00"),
            _make_row(variant_id=3, objective_score=0.7, run_date_str="2026-07-13T02:00:00+00:00"),
        ]
        result = compute_score_trendline(rows)
        assert len(result) == 3
        # Sorted chronologically
        dates = [r["date"] for r in result]
        assert dates == sorted(dates)
        # Best scores per night
        assert result[0]["best_score"] == 0.7  # 07-13
        assert result[1]["best_score"] == 0.6  # 07-14
        assert result[2]["best_score"] == 0.5  # 07-15

    def test_same_date_multiple_runs(self):
        """Multiple runs on the same date should pick the best score."""
        rows = [
            _make_row(variant_id=1, objective_score=0.9, run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=2, objective_score=0.3, run_date_str="2026-07-15T04:00:00+00:00"),
        ]
        result = compute_score_trendline(rows)
        assert len(result) == 1  # one date
        assert result[0]["best_score"] == 0.9
        assert result[0]["best_variant_id"] == 1

    def test_empty_data(self):
        result = compute_score_trendline([])
        assert result == []

    def test_null_scores_ignored(self):
        rows = [
            _make_row(variant_id=1, objective_score=None, run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=2, objective_score=0.4, run_date_str="2026-07-15T02:00:00+00:00"),
        ]
        result = compute_score_trendline(rows)
        assert result[0]["best_score"] == 0.4


# ═══════════════════════════════════════════════════════════════════════════════
# compute_param_drift
# ═══════════════════════════════════════════════════════════════════════════════


class TestParamDrift:
    def test_no_drift_single_hash(self):
        rows = [
            _make_row(variant_id=1, params_hash="abc123", run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=1, params_hash="abc123", run_date_str="2026-07-14T02:00:00+00:00"),
        ]
        result = compute_param_drift(rows)
        assert len(result) == 1
        assert result[0]["variant_id"] == 1
        assert result[0]["hash_count"] == 1
        assert result[0]["drift"] is False
        assert result[0]["nights_present"] == 2

    def test_drift_detected(self):
        rows = [
            _make_row(variant_id=1, params_hash="abc123", run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=1, params_hash="def456", run_date_str="2026-07-14T02:00:00+00:00"),
        ]
        result = compute_param_drift(rows)
        assert result[0]["drift"] is True
        assert result[0]["hash_count"] == 2

    def test_drift_sorted_by_hash_count(self):
        rows = [
            # variant 1: 3 different hashes (most drift)
            _make_row(variant_id=1, params_hash="aaa", run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=1, params_hash="bbb", run_date_str="2026-07-14T02:00:00+00:00"),
            _make_row(variant_id=1, params_hash="ccc", run_date_str="2026-07-13T02:00:00+00:00"),
            # variant 2: 2 different hashes
            _make_row(variant_id=2, params_hash="xxx", run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=2, params_hash="yyy", run_date_str="2026-07-14T02:00:00+00:00"),
            # variant 3: stable
            _make_row(variant_id=3, params_hash="zzz", run_date_str="2026-07-15T02:00:00+00:00"),
            _make_row(variant_id=3, params_hash="zzz", run_date_str="2026-07-14T02:00:00+00:00"),
        ]
        result = compute_param_drift(rows)
        # Sorted by hash_count DESC
        assert result[0]["variant_id"] == 1
        assert result[1]["variant_id"] == 2
        assert result[2]["variant_id"] == 3

    def test_empty_data(self):
        result = compute_param_drift([])
        assert result == []

    def test_single_observation(self):
        """Single night, single variant — no drift possible."""
        rows = [_make_row(variant_id=1, params_hash="abc123")]
        result = compute_param_drift(rows)
        assert result[0]["drift"] is False
        assert result[0]["hash_count"] == 1
        assert result[0]["nights_present"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# generate_markdown
# ═══════════════════════════════════════════════════════════════════════════════


class TestGenerateMarkdown:
    def test_empty_data(self):
        content = generate_markdown({})
        assert "⚠️" in content
        assert "No sweep data found" in content

    def test_single_trader_minimal(self):
        data = {
            "kairos": [
                _make_row(variant_id=1, objective_score=0.5, run_date_str="2026-07-15T02:00:00+00:00"),
            ]
        }
        content = generate_markdown(data)
        assert "Kairos 🔮" in content
        assert "Top-3 Consistency" in content
        assert "Score Trendline" in content
        assert "Parameter Drift" in content
        assert "Cross-Trader Summary" in content
        # No drift detected
        assert "No parameter drift detected" in content

    def test_multi_trader(self):
        data = {
            "kairos": [
                _make_row(trader_id="kairos", variant_id=1, objective_score=0.5,
                          run_date_str="2026-07-15T02:00:00+00:00"),
            ],
            "aldridge": [
                _make_row(trader_id="aldridge", variant_id=2, objective_score=0.6,
                          run_date_str="2026-07-15T02:00:00+00:00"),
            ],
        }
        content = generate_markdown(data)
        assert "Kairos 🔮" in content
        assert "Aldridge 📊" in content

    def test_custom_title(self):
        data = {
            "kairos": [
                _make_row(variant_id=1, objective_score=0.5),
            ]
        }
        content = generate_markdown(data, title="## Custom Title")
        assert "Custom Title" in content

    def test_drift_shown(self):
        data = {
            "kairos": [
                _make_row(variant_id=1, params_hash="abc", run_date_str="2026-07-15T02:00:00+00:00"),
                _make_row(variant_id=1, params_hash="def", run_date_str="2026-07-14T02:00:00+00:00"),
            ]
        }
        content = generate_markdown(data)
        assert "Parameter Drift" in content
        assert "No parameter drift detected" not in content
        # Should show the drifting variants table
        assert "Variants with evolving parameters" in content

    def test_cross_trader_summary_table(self):
        data = {
            "kairos": [
                _make_row(trader_id="kairos", variant_id=1, objective_score=0.5,
                          run_date_str="2026-07-15T02:00:00+00:00"),
                _make_row(trader_id="kairos", variant_id=2, objective_score=0.3,
                          run_date_str="2026-07-15T02:00:00+00:00"),
            ],
        }
        content = generate_markdown(data)
        # Cross-trader summary table should have a row for each trader
        assert "🌐 Cross-Trader Summary" in content
        assert "Kairos 🔮" in content


# ═══════════════════════════════════════════════════════════════════════════════
# get_7_night_data (integration-ish with mocked DB)
# ═══════════════════════════════════════════════════════════════════════════════


class TestGet7NightData:
    def test_db_unavailable(self):
        """When UnifiedStore can't connect, return empty dict gracefully."""
        with patch("src.db.unified_store.UnifiedStore", side_effect=ImportError("no pg")):
            result = get_7_night_data()
            assert result == {}

    def test_db_returns_empty(self):
        """When DB has no sweep data, return empty dict."""
        mock_store = MagicMock()
        mock_store.query.return_value = []
        with patch("src.db.unified_store.UnifiedStore", return_value=mock_store):
            result = get_7_night_data()
            assert result == {}

    def test_db_returns_data_grouped_by_trader(self):
        """Rows should be grouped by trader_id."""
        mock_store = MagicMock()
        mock_store.query.return_value = [
            {
                "trader_id": "kairos",
                "variant_id": 1,
                "params_hash": "abc",
                "objective_score": 0.5,
                "calmar": 1.0,
                "sortino": 1.0,
                "profit_factor": 1.0,
                "win_rate": 0.5,
                "total_return_pct": 2.0,
                "created_at": datetime.now(timezone.utc),
                "run_date": datetime.now(timezone.utc),
            },
            {
                "trader_id": "aldridge",
                "variant_id": 2,
                "params_hash": "def",
                "objective_score": 0.6,
                "calmar": 1.2,
                "sortino": 1.3,
                "profit_factor": 1.1,
                "win_rate": 0.6,
                "total_return_pct": 3.0,
                "created_at": datetime.now(timezone.utc),
                "run_date": datetime.now(timezone.utc),
            },
        ]
        with patch("src.db.unified_store.UnifiedStore", return_value=mock_store):
            result = get_7_night_data()
            assert "kairos" in result
            assert "aldridge" in result
            assert len(result["kairos"]) == 1
            assert len(result["aldridge"]) == 1
            assert result["kairos"][0]["variant_id"] == 1
            assert result["aldridge"][0]["variant_id"] == 2

    def test_trader_filter(self):
        """When trader_id is specified, only that trader's data is returned."""
        mock_store = MagicMock()
        mock_store.query.return_value = [
            {
                "trader_id": "kairos",
                "variant_id": 1,
                "params_hash": "abc",
                "objective_score": 0.5,
                "calmar": 1.0,
                "sortino": 1.0,
                "profit_factor": 1.0,
                "win_rate": 0.5,
                "total_return_pct": 2.0,
                "created_at": datetime.now(timezone.utc),
                "run_date": datetime.now(timezone.utc),
            },
        ]
        with patch("src.db.unified_store.UnifiedStore", return_value=mock_store):
            result = get_7_night_data(trader_id="kairos")
            assert "kairos" in result
            mock_store.query.assert_called_once()
            # SQL should filter on trader_id
            call_sql = mock_store.query.call_args[0][0]
            assert "sr.trader_id = %s" in call_sql
            assert mock_store.query.call_args[0][1] == ("kairos",)
