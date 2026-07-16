# Data Bus API Reference

> REST API on `http://192.168.1.41:5000` — 42 endpoints for quotes, sentiment, technicals, fundamentals, and more.

## Base URL

```
http://192.168.1.41:5000
```

All responses are JSON. Content-Type: `application/json`.

## Health & Metadata

### `GET /health`

System health and scheduler status.

**Response:**
```json
{
  "status": "ok",
  "service": "data-bus",
  "uptime_seconds": 12345,
  "tracked_symbols": 17,
  "schedulers": [
    {
      "name": "quotes",
      "interval": 60,
      "mode": "market",
      "last_run": "2026-07-15T14:30:00Z",
      "run_count": 450
    }
  ],
  "cache_stats": {
    "keys": 42,
    "entries": [...]
  }
}
```

Scheduler modes: `market` (9:30-16:00 ET), `always`, `off`.

### `GET /metrics`

Prometheus-style metrics endpoint. Returns system metrics in text format.

### `GET /source-quality`

Data source health metrics — freshness, error rates, latency per source.

**Response:**
```json
{
  "sources": [
    {"name": "alpaca", "status": "healthy", "last_success": "..."},
    {"name": "lonestar", "status": "degraded", "last_error": "..."}
  ],
  "count": 5
}
```

### `GET /mcp-status`

MCP tool suite status. Returns available tools and their health.

---

## Market Data

### `GET /quotes`

Real-time stock quotes with OHLCV + RSI.

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `symbols` | string | Yes | Comma-separated ticker symbols (e.g., `AAPL,SPY,TSLA`) |

**Response:**
```json
{
  "quotes": {
    "AAPL": {
      "open": 185.50,
      "high": 187.20,
      "low": 184.80,
      "close": 186.75,
      "volume": 52400000,
      "rsi": 58.3,
      "source": "alpaca",
      "stale": false
    }
  },
  "cached": 1,
  "fetched_live": 0
}
```

### `GET /bars`

Historical OHLCV bars.

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `symbols` | string | Yes | Comma-separated tickers |
| `timeframe` | string | No | `1Min`, `5Min`, `15Min`, `1Hour`, `1Day` (default: `1Day`) |
| `limit` | int | No | Number of bars (default: 100) |

### `GET /crypto`

Cryptocurrency quotes.

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `symbols` | string | Yes | e.g., `BTC/USD,ETH/USD` |

**Response:**
```json
{
  "crypto": {
    "BTC/USD": {"price": 67234.50, "timestamp": "2026-07-15T14:30:00Z"}
  },
  "cached": 1,
  "fetched_live": 0
}
```

---

## Technical Analysis

### `GET /technical-scan`

Multi-timeframe technical scan (15m/1h/4h/1d) with RSI/MACD/BB.

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `symbol` | string | Yes | Single ticker symbol |

**Response:**
```json
{
  "symbol": "AAPL",
  "scans": {
    "15m": {"rsi": 62.1, "macd_signal": "bullish", "bb_position": "middle"},
    "1h": {"rsi": 58.3, "macd_signal": "neutral", "bb_position": "upper"},
    "4h": {"rsi": 55.0, "macd_signal": "bullish", "bb_position": "middle"},
    "1d": {"rsi": 52.4, "macd_signal": "bearish", "bb_position": "lower"}
  },
  "source": "data-bus"
}
```

### `GET /ml-signal`

Machine learning market regime signal.

**Response:**
```json
{
  "regime": "TRENDING_UP",
  "confidence": 0.72,
  "features": {"rsi": 62, "volatility": 0.015, "trend_strength": 0.8},
  "classifier": "rule-based",
  "kmeans_cluster": null
}
```

Regime values: `TRENDING_UP`, `TRENDING_DOWN`, `HIGH_VOL`, `MEAN_REVERTING`, `CHOPPY`.

### `GET /signals`

All computed trading signals (composite).

**Response:**
```json
{
  "signals": [
    {"name": "momentum_zscore", "value": 1.2, "ticker": "AAPL"},
    {"name": "rsi_signal", "value": "neutral", "ticker": "AAPL"}
  ],
  "count": 34
}
```

### `GET /signal`

Single signal lookup.

**Parameters:** `name` (signal name), `ticker` (symbol)

### `GET /momentum`

Cross-sectional momentum signal. Ranks all tickers by composite Z-score.

**Response:**
```json
{
  "signal": "cross_sectional_momentum",
  "avg_composite_z": 0.15,
  "top_buys": ["NVDA", "META", "AVGO"],
  "top_avoids": ["JNJ", "PG", "KO"],
  "num_ranked": 17,
  "market_regime": "TRENDING_UP",
  "top_decile_avg_z": 1.45
}
```

### `GET /percentile`

Percentile-based ranking of a ticker against its peers.

---

## Sentiment & News

### `GET /sentiment`

FinBERT + Praesentire sentiment for a ticker.

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `symbol` | string | Yes | Single ticker symbol |

**Response:**
```json
{
  "symbol": "AAPL",
  "sentiment": {
    "compound": 0.42,
    "positive": 0.35,
    "negative": 0.12,
    "neutral": 0.53
  },
  "source": "finbert"
}
```

### `POST /sentiment`

Batch sentiment analysis. Body: `{"symbols": ["AAPL", "TSLA"]}`

### `GET /sentiment-divergence`

Cross-language sentiment divergence (English vs Chinese) from Praesentire.

**Parameters:** `symbol` (ticker)

### `GET /news`

News headlines for a ticker.

**Parameters:** `symbol` (ticker)

**Response:**
```json
{
  "news": [
    {
      "headline": "Apple Reports Record Q3 Earnings",
      "source": "Bloomberg",
      "url": "https://...",
      "created_at": "2026-07-15T14:00:00Z"
    }
  ]
}
```

### `GET /news/search`

Full-text news search.

**Parameters:** `q` (query string), `limit` (optional)

### `GET /news-cache`

Cached news — lower latency, may be stale.

### `GET /social`

Social media sentiment (Reddit, Twitter).

**Parameters:** `symbol` (ticker)

### `GET /overnight-sentiment`

Overnight sentiment aggregation — after-hours news, futures, international markets.

---

## Fundamentals & Analysis

### `GET /fundamentals`

Company fundamentals (market cap, P/E, EPS, revenue, etc.).

**Parameters:** `symbol` (ticker)

**Response:**
```json
{
  "symbol": "AAPL",
  "fundamentals": {
    "market_cap": 2850000000000,
    "pe_ratio": 32.5,
    "eps": 6.15,
    "revenue": 383000000000,
    "sector": "Technology"
  },
  "source": "alpaca"
}
```

Returns 404 with `{"symbol": "AAPL", "error": "...", "fundamentals": null}` when no data available.

### `GET /equity-analysis`

Comprehensive equity analysis combining fundamentals, technicals, and sentiment.

### `GET /earnings`

Earnings calendar and history.

**Parameters:** `symbol` (ticker)

**Response:**
```json
{
  "earnings": {
    "AAPL": [
      {"report_date": "2026-04-30", "eps_estimate": 1.52, "eps_actual": 1.55}
    ]
  },
  "source": "lonestar"
}
```

### `GET /earnings_today`

Earnings reports scheduled for today.

### `GET /options`

Options chain and Greeks.

**Parameters:** `symbol` (ticker), `expiration` (optional), `strike` (optional)

---

## Flow & Insider Activity

### `GET /flow`

Unusual options flow (sweeps, dark pool, blocks).

**Response:**
```json
{
  "flow": {
    "flows": [
      {
        "tickers": ["AAPL"],
        "summary": "Large call sweep — $2.3M notional",
        "sentiment": "bullish"
      }
    ]
  }
}
```

### `GET /insiders`

Insider trading filings (SEC Form 4).

**Response:**
```json
{
  "insiders": {
    "transactions": [...],
    "fetched_at": "2026-07-15T14:00:00Z"
  },
  "source": "lonestar"
}
```

### `GET /congress`

Congressional trading disclosures.

---

## Macro & Risk

### `GET /macro`

FRED macroeconomic indicators.

**Response:**
```json
{
  "macro": {
    "indicators": {
      "CPI": {"value": 3.2, "date": "2026-06-30", "series_id": "CPIAUCSL"},
      "GDP": {"value": 28700, "date": "2026-03-31", "series_id": "GDP"},
      "DGS10": {"value": 4.25, "date": "2026-07-14", "series_id": "DGS10"},
      "DGS2": {"value": 4.05, "date": "2026-07-14", "series_id": "DGS2"},
      "FOMC_lower": {"value": 4.25, "date": "2026-06-15"},
      "FOMC_upper": {"value": 4.50, "date": "2026-06-15"}
    }
  }
}
```

### `GET /fear_greed`

Fear & Greed Index (0-100).

**Response:**
```json
{
  "fear_greed": {
    "value": 45,
    "classification": "Neutral"
  },
  "source": "alternative.me"
}
```

Classifications: `Extreme Fear`, `Fear`, `Neutral`, `Greed`, `Extreme Greed`.

### `GET /risk`

Portfolio risk scoring (concentration, VaR, correlation).

**Parameters:** `symbol` (ticker)

---

## Portfolio & Agent State

### `GET /self/stats`

Agent performance stats: today's P&L, win rates by signal/sector, confidence calibration.

**Parameters:** `agent_id` (e.g., `kairos`, `aldridge`, `stonks`)

### `GET /portfolio`

Live portfolio data from Alpaca: portfolio_value, cash, buying_power, positions, P&L.

**Parameters:** `trader_id` (optional — omit for all)

### `GET /dashboard`

Aggregated dashboard data (all traders, leaderboard, activity feed).

### `GET /tick-snapshot`

Current tick state for all traders — positions, signals, regime.

---

## Virtual Traders

### `GET /virtual-traders`

List all virtual traders with status, tier, and performance.

**Response:**
```json
{
  "virtual_traders": [
    {
      "name": "kairos_variant_001",
      "base_trader": "kairos",
      "tier": "shadow",
      "status": "active",
      "sharpe": 0.8,
      "created_at": "..."
    }
  ],
  "count": 12
}
```

### `GET /virtual-traders/leaderboard`

Virtual trader leaderboard ranked by objective score.

### `POST /virtual-traders/register`

Register a new virtual trader. Body: `{"base_trader": "kairos", "variant_type": "prompt", ...}`

---

## Config & Admin

### `GET /trader/<agent>/config`

Get trader configuration.

### `PATCH /trader/<agent>/config`

Update trader configuration.

### `GET /params`

System parameters (from `trading.system_params`).

### `GET /briefing`

Daily market briefing — summary of overnight moves, upcoming events, key levels.

### `GET /calendar`

Economic calendar — FOMC, CPI, jobs reports, etc.

### `GET /discover`

Symbol discovery — suggests tickers based on momentum, volume, and news.

### `POST /retrain-hmm`

Trigger HMM regime model retraining (admin).

### `GET /retrain-status`

Check retraining job status.

### `GET /debug`

Debug endpoint — internal state, cache contents, scheduler details. Admin only.

---

## Rate Limits & Caching

- **Market hours (9:30-16:00 ET):** Schedulers run aggressively (5-60s intervals)
- **After hours:** Most endpoints serve cached data; some schedulers continue
- **TTL by endpoint:** Configurable in `config/data_bus.yaml` (defaults: quotes 60s, sentiment 300s, macro 3600s)
- **Fetch queue:** Rate-limit-aware batching via `src/fetch_queue.py` — prevents Alpaca API throttling

## Error Responses

All errors return JSON:
```json
{
  "error": "Symbol not found",
  "symbol": "INVALID",
  "status": 404
}
```

Common status codes: 200 (OK), 400 (bad request), 404 (not found), 503 (service unavailable — e.g., momentum module not installed).

## Testing

```bash
# Smoke test all endpoints (requires running data bus)
pytest tests/test_data_bus_smoke.py -v

# The test suite covers all 42 endpoints with schema validation
# and cross-endpoint consistency checks
```
