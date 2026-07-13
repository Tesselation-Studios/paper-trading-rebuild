#!/usr/bin/env python3
"""
Tests for social_sentiment and social_reddit modules.

Unit tests for the keyword sentiment analyzer, and integration tests
for the Bluesky and StockTwits API wrappers (these hit live APIs and
are marked with @pytest.mark.integration).

Run:
    pytest tests/test_social_sentiment.py -v
    pytest tests/test_social_sentiment.py -v -m integration   # live API tests
"""

import pytest
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


# ═══════════════════════════════════════════════════════════════════════════════
# _simple_sentiment tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSimpleSentiment:
    """Unit tests for the keyword-based sentiment analyzer."""

    def setup_method(self):
        from social_sentiment import _simple_sentiment
        self._ss = _simple_sentiment

    # ── Positive cases ──────────────────────────────────────────────────

    def test_strong_bullish(self):
        score = self._ss("Strong buy, mooning, rocket ship incoming!")
        assert score > 0.1, f"Expected positive, got {score}"

    def test_bullish_amplified(self):
        score = self._ss("Extremely bullish on this stock, huge upside potential")
        assert score > 0.15, f"Expected strong positive, got {score}"

    def test_negated_bearish_is_positive(self):
        """'Not bearish' should yield a positive (or at least non-negative) score."""
        score = self._ss("Not bearish, actually quite positive on this")
        assert score >= 0.0, f"Expected non-negative, got {score}"

    def test_mildly_positive(self):
        score = self._ss("This stock has good growth potential")
        assert score > 0.0, f"Expected positive, got {score}"

    def test_recovery_language(self):
        score = self._ss("Strong recovery underway, solid rebound in progress")
        assert score > 0.05, f"Expected positive, got {score}"

    # ── Negative cases ──────────────────────────────────────────────────

    def test_strong_bearish(self):
        score = self._ss("Terrible crash, dumping everything, total disaster")
        assert score < -0.1, f"Expected negative, got {score}"

    def test_bearish_amplified(self):
        score = self._ss("Extremely bearish on this position, shorting hard")
        assert score < -0.1, f"Expected strong negative, got {score}"

    def test_negated_bullish_is_negative(self):
        score = self._ss("Not bullish, looking quite weak here")
        assert score <= 0.0, f"Expected non-positive, got {score}"

    def test_loss_language(self):
        score = self._ss("Big losses, terrible quarter, selling at a loss")
        assert score < -0.05, f"Expected negative, got {score}"

    # ── Neutral cases ───────────────────────────────────────────────────

    def test_empty_string(self):
        assert self._ss("") == 0.0

    def test_whitespace_only(self):
        assert self._ss("   \n  ") == 0.0

    def test_neutral_phrase(self):
        score = self._ss("The stock price is $150 today")
        assert -0.05 <= score <= 0.05, f"Expected near-neutral, got {score}"

    def test_ticker_only(self):
        score = self._ss("AAPL")
        assert -0.05 <= score <= 0.05, f"Expected near-neutral, got {score}"

    # ── Edge cases ──────────────────────────────────────────────────────

    def test_score_range(self):
        """All scores should be in [-1, 1]."""
        texts = [
            "Best day ever, mooning like crazy, rocket ship, huge gains, massive profits!",
            "Worst day ever, crashing hard, total disaster, huge losses, terrible!",
            "",
            "Mixed feelings: some good news but also some bad signals",
        ]
        for text in texts:
            score = self._ss(text)
            assert -1.0 <= score <= 1.0, f"Score {score} out of range for: {text[:50]}"

    def test_pronounced_negation_edge_case(self):
        """'Not bad' should be positive."""
        score = self._ss("Not bad at all, actually quite positive on this")
        assert score > 0.0, f"Expected positive, got {score}"

    def test_mixed_sentiment_noise(self):
        """Mixed sentiment should be near-zero but not exactly 0."""
        score = self._ss("Up and down, good and bad, bullish but also bearish")
        # Should be near-neutral (both signals cancel)
        assert -0.1 <= score <= 0.1, f"Expected near-neutral, got {score}"


# ═══════════════════════════════════════════════════════════════════════════════
# Integration tests (live API calls)
# ═══════════════════════════════════════════════════════════════════════════════

class TestBlueskyIntegration:
    """Integration tests for fetch_bluesky_sentiment — hits live API.

    Marked as @pytest.mark.integration — run with:
        pytest tests/test_social_sentiment.py -m integration
    """

    @pytest.mark.integration
    def test_returns_valid_schema(self):
        from social_sentiment import fetch_bluesky_sentiment
        result = fetch_bluesky_sentiment("AAPL")
        assert "posts" in result
        assert "top_posts" in result
        assert "sentiment_score" in result
        assert isinstance(result["posts"], int)
        assert isinstance(result["top_posts"], list)
        assert isinstance(result["sentiment_score"], float)
        assert -1.0 <= result["sentiment_score"] <= 1.0

    @pytest.mark.integration
    def test_top_post_schema(self):
        from social_sentiment import fetch_bluesky_sentiment
        result = fetch_bluesky_sentiment("AAPL")
        if result["top_posts"]:
            post = result["top_posts"][0]
            for key in ("handle", "text", "sentiment_score", "likes", "reposts", "created_at"):
                assert key in post, f"Missing key: {key}"

    @pytest.mark.integration
    def test_returns_data_for_major_ticker(self):
        from social_sentiment import fetch_bluesky_sentiment
        result = fetch_bluesky_sentiment("TSLA")
        # Expect at least some posts for a major ticker
        assert result["posts"] > 0, "Expected posts for TSLA"


class TestStockTwitsIntegration:
    """Integration tests for fetch_stocktwits_sentiment — hits live API."""

    @pytest.mark.integration
    def test_returns_valid_schema(self):
        from social_sentiment import fetch_stocktwits_sentiment
        result = fetch_stocktwits_sentiment("AAPL")
        assert "messages" in result
        assert "top_messages" in result
        assert "sentiment_score" in result
        assert isinstance(result["messages"], int)
        assert isinstance(result["top_messages"], list)
        assert isinstance(result["sentiment_score"], float)
        assert -1.0 <= result["sentiment_score"] <= 1.0

    @pytest.mark.integration
    def test_top_message_schema(self):
        from social_sentiment import fetch_stocktwits_sentiment
        result = fetch_stocktwits_sentiment("AAPL")
        if result["top_messages"]:
            msg = result["top_messages"][0]
            for key in ("user", "body", "sentiment", "created_at"):
                assert key in msg, f"Missing key: {key}"

    @pytest.mark.integration
    def test_returns_data_for_major_ticker(self):
        from social_sentiment import fetch_stocktwits_sentiment
        result = fetch_stocktwits_sentiment("NVDA")
        assert result["messages"] > 0, "Expected messages for NVDA"


# ═══════════════════════════════════════════════════════════════════════════════
# SocialRedditPipeline tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSocialRedditPipeline:
    """Tests for the RSS-based Reddit sentiment pipeline."""

    def test_import_and_instantiate(self):
        from social_reddit import SocialRedditPipeline
        pipeline = SocialRedditPipeline()
        assert pipeline is not None

    def test_fetch_all_posts_returns_list(self):
        from social_reddit import SocialRedditPipeline
        pipeline = SocialRedditPipeline()
        posts = pipeline.fetch_all_posts(max_per_feed=2)
        assert isinstance(posts, list)
        if posts:
            post = posts[0]
            for key in ("post_title", "subreddit", "tickers", "sentiment_score", "signal_strength"):
                assert key in post, f"Missing key: {key}"

    def test_ticker_sentiment_schema(self):
        from social_reddit import SocialRedditPipeline
        pipeline = SocialRedditPipeline()
        result = pipeline.fetch_ticker_sentiment("AAPL")
        assert "posts" in result
        assert "top_posts" in result
        assert "sentiment_score" in result
        assert isinstance(result["posts"], int)
        assert isinstance(result["top_posts"], list)
        assert isinstance(result["sentiment_score"], float)