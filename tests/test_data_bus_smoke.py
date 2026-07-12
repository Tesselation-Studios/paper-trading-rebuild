#!/usr/bin/env python3
"""
Integration smoke tests for the docker.klo:5000 data bus.

Covers every endpoint: health, quotes, crypto, sentiment, fear_greed, news,
fundamentals, signals, momentum, congress, insiders, flow, earnings, macro,
and source-quality.

Strategy:
- Hit endpoint → validate HTTP 200 (404 for /fundamentals when no data is OK)
- Validate response schema (keys, types)
- Validate non-empty data where applicable
- After-hours / no-data scenarios are handled gracefully
"""

import pytest
import requests
from datetime import datetime

DATA_BUS = "http://localhost:5000"
TIMEOUT = 10  # seconds


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get(path: str, params: dict | None = None, expect_status: int = 200):
    """GET a data-bus endpoint and return parsed JSON."""
    url = f"{DATA_BUS}{path}"
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    assert resp.status_code == expect_status, (
        f"Expected {expect_status}, got {resp.status_code}: {resp.text[:300]}"
    )
    return resp.json()


# ── /health ──────────────────────────────────────────────────────────────────


class TestHealth:
    """GET /health — liveness and scheduler state."""

    def test_status_ok(self):
        data = _get("/health")
        assert data["status"] == "ok"
        assert data["service"] == "data-bus"

    def test_has_schedulers(self):
        data = _get("/health")
        schedulers = data["schedulers"]
        assert isinstance(schedulers, list)
        assert len(schedulers) > 0
        names = {s["name"] for s in schedulers}
        expected = {"quotes", "crypto", "news", "congress", "signals_gc",
                    "momentum", "macro", "earnings", "fear_greed", "flow",
                    "insiders", "sentiment"}
        missing = expected - names
        assert not missing, f"Missing schedulers: {missing}"

    def test_scheduler_schema(self):
        data = _get("/health")
        for s in data["schedulers"]:
            assert "name" in s
            assert "interval" in s
            assert "mode" in s
            assert "last_run" in s
            assert "run_count" in s
            # mode should be a known value
            assert s["mode"] in ("off", "market", "always")

    def test_cache_stats(self):
        data = _get("/health")
        assert "cache_stats" in data
        assert "entries" in data["cache_stats"]
        assert "keys" in data["cache_stats"]
        assert isinstance(data["cache_stats"]["keys"], int)
        assert isinstance(data["cache_stats"]["entries"], list)
        assert data["cache_stats"]["keys"] > 0

    def test_uptime_positive(self):
        data = _get("/health")
        assert data["uptime_seconds"] > 0

    def test_tracked_symbols(self):
        data = _get("/health")
        assert isinstance(data["tracked_symbols"], int)
        assert data["tracked_symbols"] >= 0


# ── /quotes ──────────────────────────────────────────────────────────────────


class TestQuotes:
    """GET /quotes?symbols=AAPL,SPY,TSLA — real-time quote data."""

    SYMBOLS = "AAPL,SPY,TSLA"

    def test_status_200(self):
        _get("/quotes", {"symbols": self.SYMBOLS})

    def test_returns_all_symbols(self):
        data = _get("/quotes", {"symbols": self.SYMBOLS})
        quotes = data["quotes"]
        for sym in ["AAPL", "SPY", "TSLA"]:
            assert sym in quotes, f"Missing {sym} in quotes"

    def test_required_fields(self):
        data = _get("/quotes", {"symbols": self.SYMBOLS})
        for sym, q in data["quotes"].items():
            assert "price" in q, f"{sym}: missing price"
            assert isinstance(q["price"], (int, float)), f"{sym}: price not numeric"
            assert "change_pct" in q, f"{sym}: missing change_pct"
            assert "volume" in q, f"{sym}: missing volume"
            assert "rsi" in q, f"{sym}: missing rsi"
            assert "macd" in q, f"{sym}: missing macd"
            assert q["macd"] in ("bullish", "bearish", "neutral"), (
                f"{sym}: unexpected macd={q['macd']}"
            )

    def test_meta_fields(self):
        data = _get("/quotes", {"symbols": self.SYMBOLS})
        assert isinstance(data["cached"], int)
        assert isinstance(data["fetched_live"], int)

    def test_price_is_positive(self):
        data = _get("/quotes", {"symbols": self.SYMBOLS})
        for sym, q in data["quotes"].items():
            assert q["price"] > 0, f"{sym}: price={q['price']} not positive"


# ── /crypto ──────────────────────────────────────────────────────────────────


class TestCrypto:
    """GET /crypto?symbols=BTC/USD — cryptocurrency quotes."""

    def test_status_200(self):
        _get("/crypto", {"symbols": "BTC/USD"})

    def test_returns_symbol(self):
        data = _get("/crypto", {"symbols": "BTC/USD"})
        assert "BTC/USD" in data["crypto"]

    def test_required_fields(self):
        data = _get("/crypto", {"symbols": "BTC/USD"})
        btc = data["crypto"]["BTC/USD"]
        assert "price" in btc
        assert isinstance(btc["price"], (int, float))
        assert btc["price"] > 0
        assert "timestamp" in btc

    def test_meta_fields(self):
        data = _get("/crypto", {"symbols": "BTC/USD"})
        assert isinstance(data["cached"], int)
        assert isinstance(data["fetched_live"], int)


# ── /sentiment ───────────────────────────────────────────────────────────────


class TestSentiment:
    """GET /sentiment?symbol=AAPL — news/social sentiment scores."""

    def test_status_200(self):
        _get("/sentiment", {"symbol": "AAPL"})

    def test_required_score_fields(self):
        data = _get("/sentiment", {"symbol": "AAPL"})
        sentiment = data["sentiment"]
        for field in ("compound", "positive", "negative", "neutral"):
            assert field in sentiment, f"Missing {field}"
            assert isinstance(sentiment[field], (int, float)), (
                f"{field} not numeric: {sentiment[field]}"
            )
        # Scores should be in valid ranges
        assert -1.0 <= sentiment["compound"] <= 1.0
        assert 0.0 <= sentiment["positive"] <= 1.0
        assert 0.0 <= sentiment["negative"] <= 1.0
        assert 0.0 <= sentiment["neutral"] <= 1.0

    def test_has_symbol_and_source(self):
        data = _get("/sentiment", {"symbol": "AAPL"})
        assert "symbol" in data
        assert data["symbol"] == "AAPL"
        assert "source" in data


# ── /fear_greed ──────────────────────────────────────────────────────────────


class TestFearGreed:
    """GET /fear_greed — Fear & Greed Index."""

    def test_status_200(self):
        _get("/fear_greed")

    def test_required_fields(self):
        data = _get("/fear_greed")
        fg = data["fear_greed"]
        assert "value" in fg
        assert isinstance(fg["value"], (int, float))
        assert 0 <= fg["value"] <= 100, f"value={fg['value']} out of range"
        assert "classification" in fg
        assert isinstance(fg["classification"], str)
        assert len(fg["classification"]) > 0
        assert "source" in data

    def test_classification_known_value(self):
        data = _get("/fear_greed")
        classification = data["fear_greed"]["classification"]
        known = {"Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"}
        assert classification in known, f"Unknown classification: {classification}"


# ── /news ────────────────────────────────────────────────────────────────────


class TestNews:
    """GET /news?symbol=AAPL — news headlines."""

    def test_status_200(self):
        _get("/news", {"symbol": "AAPL"})

    def test_returns_headlines(self):
        data = _get("/news", {"symbol": "AAPL"})
        assert "news" in data
        assert isinstance(data["news"], list)
        assert len(data["news"]) > 0, "Expected non-empty news list"

    def test_headline_schema(self):
        data = _get("/news", {"symbol": "AAPL"})
        for article in data["news"]:
            assert "headline" in article
            assert isinstance(article["headline"], str)
            assert len(article["headline"]) > 0
            assert "source" in article
            assert "url" in article
            assert "created_at" in article


# ── /fundamentals ────────────────────────────────────────────────────────────


class TestFundamentals:
    """GET /fundamentals?symbol=AAPL — company fundamentals.

    Note: this endpoint may return 404 (with JSON body) when no data is
    available for a symbol.  Both responses are valid in this test suite.
    """

    def test_returns_json(self):
        """Fundamentals returns JSON — may be 200 with data or 404 with error."""
        resp = requests.get(
            f"{DATA_BUS}/fundamentals",
            params={"symbol": "AAPL"},
            timeout=TIMEOUT,
        )
        assert resp.status_code in (200, 404), (
            f"Unexpected status {resp.status_code}"
        )
        body = resp.json()
        assert "symbol" in body
        assert body["symbol"] == "AAPL"

    def test_error_schema_when_no_data(self):
        """When data is unavailable, response has error + null fundamentals."""
        data = _get("/fundamentals", {"symbol": "AAPL"}, expect_status=404)
        assert "error" in data
        assert "fundamentals" in data
        # fundamentals may be null when unavailable
        assert data["fundamentals"] is None or isinstance(data["fundamentals"], dict)

    def test_params_required(self):
        """Missing symbol param returns 400."""
        resp = requests.get(f"{DATA_BUS}/fundamentals", timeout=TIMEOUT)
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body


# ── /signals ─────────────────────────────────────────────────────────────────


class TestSignals:
    """GET /signals — all computed trading signals."""

    def test_status_200(self):
        _get("/signals")

    def test_signals_schema(self):
        data = _get("/signals")
        assert "signals" in data
        assert isinstance(data["signals"], list)
        assert "count" in data
        assert data["count"] == len(data["signals"])

    def test_signal_item_schema(self):
        data = _get("/signals")
        for sig in data["signals"]:
            # Signal items should have at minimum a type/name and value
            assert isinstance(sig, dict), f"Signal item not a dict: {sig}"


# ── /momentum ────────────────────────────────────────────────────────────────


class TestMomentum:
    """GET /momentum — cross-sectional momentum signal."""

    def test_status_200(self):
        _get("/momentum")

    def test_required_fields(self):
        data = _get("/momentum")
        assert "avg_composite_z" in data
        assert "signal" in data
        assert data["signal"] == "cross_sectional_momentum"
        assert "top_buys" in data
        assert isinstance(data["top_buys"], list)
        assert "top_avoids" in data
        assert isinstance(data["top_avoids"], list)
        assert "num_ranked" in data
        assert data["num_ranked"] > 0, "Expected non-zero ranked"
        assert "market_regime" in data

    def test_z_score_is_float(self):
        data = _get("/momentum")
        assert isinstance(data["avg_composite_z"], (int, float))

    def test_top_decile_present(self):
        data = _get("/momentum")
        # top_decile_avg_z may or may not be present; if it is, it's numeric
        if "top_decile_avg_z" in data:
            assert isinstance(data["top_decile_avg_z"], (int, float))


# ── /congress ────────────────────────────────────────────────────────────────


class TestCongress:
    """GET /congress — congressional trading disclosures."""

    def test_status_200(self):
        _get("/congress")

    def test_congress_trades_schema(self):
        data = _get("/congress")
        assert "congress_trades" in data
        ct = data["congress_trades"]
        # Schema fields (may be empty after-hours)
        for field in ("cluster_buys", "recent_buys", "recent_sells",
                      "tickers_with_congressional_activity"):
            assert field in ct, f"Missing {field} in congress_trades"
            assert isinstance(ct[field], list)
        assert "total_trades_found" in ct
        assert isinstance(ct["total_trades_found"], int)
        assert "source" in data


# ── /insiders ────────────────────────────────────────────────────────────────


class TestInsiders:
    """GET /insiders — insider trading filings.

    Note: this endpoint may return a pydantic validation error in the JSON
    body from the upstream Lonestar service.  We validate the envelope schema.
    """

    def test_status_200(self):
        _get("/insiders")

    def test_insiders_envelope(self):
        data = _get("/insiders")
        assert "insiders" in data
        assert isinstance(data["insiders"], dict)
        assert "source" in data
        # Should have fetched_at regardless of success/error
        assert "fetched_at" in data["insiders"]


# ── /flow ────────────────────────────────────────────────────────────────────


class TestFlow:
    """GET /flow — options / unusual flow data."""

    def test_status_200(self):
        _get("/flow")

    def test_returns_flows(self):
        data = _get("/flow")
        assert "flow" in data
        assert "flows" in data["flow"]
        assert isinstance(data["flow"]["flows"], list)
        assert len(data["flow"]["flows"]) > 0, "Expected non-empty flows"

    def test_flow_item_schema(self):
        data = _get("/flow")
        for item in data["flow"]["flows"]:
            assert isinstance(item, dict)
            assert "summary" in item or "title" in item
            # Items should have tickers list
            if "tickers" in item:
                assert isinstance(item["tickers"], list)


# ── /earnings ────────────────────────────────────────────────────────────────


class TestEarnings:
    """GET /earnings?symbol=AAPL — earnings calendar.

    Note: the upstream Lonestar service may return pydantic validation errors.
    We validate the envelope schema regardless.
    """

    def test_status_200(self):
        _get("/earnings", {"symbol": "AAPL"})

    def test_earnings_envelope(self):
        data = _get("/earnings", {"symbol": "AAPL"})
        assert "earnings" in data
        assert isinstance(data["earnings"], dict)
        assert "source" in data
        # fetched_at is present on lonestar error path;
        # on success the dict is keyed by symbol with earnings list
        earnings = data["earnings"]
        if "fetched_at" in earnings:
            # Error path from lonestar — validate error shape
            pass
        else:
            # Success path — symbol-keyed earnings entries
            assert "AAPL" in earnings or any(
                isinstance(v, list) for v in earnings.values()
            ), f"Unexpected earnings shape: {earnings}"


# ── /macro ───────────────────────────────────────────────────────────────────


class TestMacro:
    """GET /macro — FRED macroeconomic indicators."""

    def test_status_200(self):
        _get("/macro")

    def test_fred_indicators(self):
        data = _get("/macro")
        assert "macro" in data
        assert "fred" in data["macro"]
        fred = data["macro"]["fred"]
        assert isinstance(fred, dict)
        assert len(fred) > 0, "Expected non-empty FRED indicators"

    def test_required_indicators(self):
        data = _get("/macro")
        fred = data["macro"]["fred"]
        # Core indicators that should always be present
        required = {"CPI", "GDP", "DGS10", "DGS2"}
        missing = required - set(fred.keys())
        assert not missing, f"Missing core FRED indicators: {missing}"

    def test_indicator_schema(self):
        data = _get("/macro")
        for series_id, indicator in data["macro"]["fred"].items():
            assert "value" in indicator, f"{series_id}: missing value"
            assert "date" in indicator, f"{series_id}: missing date"
            assert "series_id" in indicator, f"{series_id}: missing series_id"

    def test_fomc_indicators(self):
        data = _get("/macro")
        fred = data["macro"]["fred"]
        # FOMC upper/lower should be present
        assert "FOMC_lower" in fred
        assert "FOMC_upper" in fred
        fomc_lower = float(fred["FOMC_lower"]["value"])
        fomc_upper = float(fred["FOMC_upper"]["value"])
        assert fomc_lower <= fomc_upper


# ── /source-quality ──────────────────────────────────────────────────────────


class TestSourceQuality:
    """GET /source-quality — data source health metrics.

    Note: this table may not exist yet in cache.db (returns empty with note).
    """

    def test_status_200(self):
        _get("/source-quality")

    def test_sources_schema(self):
        data = _get("/source-quality")
        assert "sources" in data
        assert isinstance(data["sources"], list)
        assert "count" in data
        assert data["count"] == len(data["sources"])
        # May have a note when table not yet created
        if "note" in data:
            assert isinstance(data["note"], str)

    def test_source_item_schema(self):
        data = _get("/source-quality")
        for src in data["sources"]:
            assert isinstance(src, dict)
            # Each source should have a name at minimum
            assert "name" in src or "source" in src


# ── Cross-endpoint consistency ───────────────────────────────────────────────


class TestCrossEndpoint:
    """Cross-cutting concerns across multiple endpoints."""

    def test_all_endpoints_return_json(self):
        """Every endpoint should return valid JSON with Content-Type json."""
        endpoints = [
            ("/health", None),
            ("/quotes", {"symbols": "AAPL,SPY"}),
            ("/crypto", {"symbols": "BTC/USD"}),
            ("/sentiment", {"symbol": "AAPL"}),
            ("/fear_greed", None),
            ("/news", {"symbol": "AAPL"}),
            ("/signals", None),
            ("/momentum", None),
            ("/congress", None),
            ("/insiders", None),
            ("/flow", None),
            ("/earnings", {"symbol": "AAPL"}),
            ("/macro", None),
            ("/source-quality", None),
        ]
        for path, params in endpoints:
            resp = requests.get(f"{DATA_BUS}{path}", params=params, timeout=TIMEOUT)
            ct = resp.headers.get("Content-Type", "")
            assert "application/json" in ct, (
                f"{path}: expected JSON Content-Type, got '{ct}'"
            )
            # Must parse as JSON
            resp.json()  # no raise

    def test_response_timing(self):
        """All endpoints should respond within TIMEOUT seconds."""
        endpoints = [
            "/health",
            "/quotes?symbols=AAPL,SPY",
            "/crypto?symbols=BTC/USD",
            "/sentiment?symbol=AAPL",
            "/fear_greed",
            "/news?symbol=AAPL",
            "/signals",
            "/momentum",
            "/congress",
            "/insiders",
            "/flow",
            "/earnings?symbol=AAPL",
            "/macro",
            "/source-quality",
        ]
        for path in endpoints:
            t0 = datetime.now()
            resp = requests.get(f"{DATA_BUS}{path}", timeout=TIMEOUT)
            elapsed = (datetime.now() - t0).total_seconds()
            assert resp.status_code in (200, 404), (
                f"{path}: unexpected status {resp.status_code}"
            )
            assert elapsed < TIMEOUT, (
                f"{path}: took {elapsed:.1f}s (limit {TIMEOUT}s)"
            )
