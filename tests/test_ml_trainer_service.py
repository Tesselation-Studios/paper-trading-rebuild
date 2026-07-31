"""Tests for scripts/ml_trainer_service.py's pure logic (walk-forward split,
scoring, explanation). GPU submission and counterfactual sampling are
exercised manually against the real network/data, not mocked here --
same "logic only in CI, real integration checked by hand" split used
elsewhere in this repo (test_gpu_client.py, test_worker_registry_dispatcher.py
before it was removed).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ml_trainer_service as svc  # noqa: E402


class TestWalkForwardSplit:
    def test_splits_by_date_not_randomly(self):
        examples = [
            ({"f": 1.0}, 1, "2026-07-01"),
            ({"f": 2.0}, 0, "2026-07-05"),
            ({"f": 3.0}, 1, "2026-07-10"),
            ({"f": 4.0}, 0, "2026-07-15"),
            ({"f": 5.0}, 1, "2026-07-20"),
        ]
        train, val = svc.walk_forward_split(examples)
        # 70% of 5 = 3 (int truncation) -> first 3 dates train, last 2 validate
        assert [e[2] for e in train] == ["2026-07-01", "2026-07-05", "2026-07-10"]
        assert [e[2] for e in val] == ["2026-07-15", "2026-07-20"]

    def test_never_leaks_future_into_train(self):
        examples = [
            ({"f": float(i)}, i % 2, f"2026-07-{i:02d}") for i in range(1, 21)
        ]
        train, val = svc.walk_forward_split(examples)
        max_train_date = max(e[2] for e in train)
        min_val_date = min(e[2] for e in val)
        assert max_train_date < min_val_date

    def test_empty_input(self):
        train, val = svc.walk_forward_split([])
        assert train == []
        assert val == []


class TestScoreCandidate:
    def test_higher_feature_values_with_positive_coef_raise_p_win(self):
        coefs = {"rsi_14": 0.05, "volume_ratio": 0.0, "macd_hist": 0.0, "conviction": 0.0}
        low = svc.score_candidate({"rsi_14": 20.0, "volume_ratio": 1.0, "macd_hist": 0.0, "conviction": 0.5}, coefs, 0.0)
        high = svc.score_candidate({"rsi_14": 80.0, "volume_ratio": 1.0, "macd_hist": 0.0, "conviction": 0.5}, coefs, 0.0)
        assert high > low

    def test_zero_coefficients_and_intercept_gives_50_50(self):
        coefs = {f: 0.0 for f in svc.FEATURE_NAMES}
        p = svc.score_candidate({"rsi_14": 50.0, "volume_ratio": 1.0, "macd_hist": 0.0, "conviction": 0.5}, coefs, 0.0)
        assert abs(p - 0.5) < 1e-9

    def test_missing_feature_key_defaults_to_zero_not_crash(self):
        coefs = {"rsi_14": 1.0}
        p = svc.score_candidate({"rsi_14": 10.0}, coefs, 0.0)
        assert 0.0 <= p <= 1.0


class TestExplain:
    def test_cites_the_strongest_contributing_feature(self):
        feats = {"rsi_14": 90.0, "volume_ratio": 1.0, "macd_hist": 0.01, "conviction": 0.5}
        coefs = {"rsi_14": 0.9, "volume_ratio": 0.01, "macd_hist": 0.01, "conviction": 0.01}
        text = svc.explain(feats, coefs, p_win=0.9)
        assert "rsi_14" in text

    def test_leans_favorable_above_threshold(self):
        text = svc.explain({"rsi_14": 1.0, "volume_ratio": 0, "macd_hist": 0, "conviction": 0},
                            {"rsi_14": 1.0, "volume_ratio": 0, "macd_hist": 0, "conviction": 0}, p_win=0.8)
        assert "leans favorable" in text

    def test_leans_unfavorable_below_threshold(self):
        text = svc.explain({"rsi_14": 1.0, "volume_ratio": 0, "macd_hist": 0, "conviction": 0},
                            {"rsi_14": -1.0, "volume_ratio": 0, "macd_hist": 0, "conviction": 0}, p_win=0.2)
        assert "leans unfavorable" in text

    def test_roughly_even_near_50_percent(self):
        text = svc.explain({"rsi_14": 1.0, "volume_ratio": 0, "macd_hist": 0, "conviction": 0},
                            {"rsi_14": 0.01, "volume_ratio": 0, "macd_hist": 0, "conviction": 0}, p_win=0.5)
        assert "roughly even" in text


class TestWriteSignals:
    def test_writes_expected_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "SIGNALS_DIR", tmp_path)
        monkeypatch.setattr(svc, "SIGNALS_PATH", tmp_path / "latest.json")
        scores = {"AAPL": {"p_win": 0.65, "explanation": "test explanation"}}
        svc.write_signals(scores, "logistic_regression", val_accuracy=0.7, n_train=50)

        import json
        payload = json.loads((tmp_path / "latest.json").read_text())
        assert payload["AAPL"]["p_win"] == 0.65
        assert payload["AAPL"]["explanation"] == "test explanation"
        assert payload["AAPL"]["model_type"] == "logistic_regression"
        assert "trained_at" in payload["AAPL"]
        assert "70%" in payload["AAPL"]["confidence_note"]
        assert "50" in payload["AAPL"]["confidence_note"]
