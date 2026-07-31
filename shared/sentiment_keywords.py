"""
Shared sentiment keywords — single source of truth for keyword-based sentiment scoring.

Used by:
  - src/social_sentiment.py (Bluesky + Stocktwits + Reddit search fallback)
  - src/social_reddit.py (Reddit PRAW + DuckDuckGo pipeline)

Previously these keyword lists were duplicated across both modules. This shared
module eliminates the duplication and ensures consistent scoring across all sources.

Scoring formula:
    score = (bullish_hits - bearish_hits) / (bullish_hits + bearish_hits)
    Returns 0.0 when no keywords match (neutral).

Usage:
    from shared.sentiment_keywords import BULLISH_KEYWORDS, BEARISH_KEYWORDS
"""

# ── Bullish Keywords ─────────────────────────────────────────────────────────
# Terms indicating positive/optimistic market sentiment.
# Includes VADER-like financial terms plus Reddit-specific meme vocabulary.

BULLISH_KEYWORDS: set[str] = {
    "bullish", "moon", "rocket", "🚀", "tendies", "yolo", "calls", "long",
    "breakout", "pump", "squeeze", "gamma", "rip", "send", "printing",
    "green", "buy", "accumulation", "diamond", "hodl", "support",
    "omega", "superstonk", "power hour", "gains", "profit", "value",
    "undervalued", "oversold", "dip", "buying opportunity", "rally",
}

# ── Bearish Keywords ─────────────────────────────────────────────────────────
# Terms indicating negative/pessimistic market sentiment.

BEARISH_KEYWORDS: set[str] = {
    "bearish", "dump", "crash", "red", "short", "puts", "bag", "rug",
    "rekt", "dead", "drill", "falling", "downtrend", "overbought",
    "correction", "distribution", "sell", "cover", "liquidate",
    "stop loss", "margin call", "collapse", "fraud", "investigation",
    "downgrade", "underperform", "selloff",
}
