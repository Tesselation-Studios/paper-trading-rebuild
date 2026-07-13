# Paper Trading Data Bus API

> **Service**: `http://docker.klo:5000` (internal) or `http://192.168.1.25:5000` (LAN)
> **Status**: Active | **Version**: 2.0
> **Auth**: None required (internal network only)

The Data Bus is a centralized market data service that fetches and caches all
market data at per-source frequencies and serves it to all trading agents
(Kairos, Aldridge, Stonks) and external consumers (e.g. Hermes).

---

## Quick Start

```bash
# Health check
curl http://docker.klo:5000/health

# Discover all endpoints
curl http://docker.klo:5000/discover

# Get quotes for multiple symbols
curl "http://docker.klo:5000/quotes?symbols=AAPL,TSLA,NVDA"

# Register an external virtual trader
curl -X POST http://docker.klo:5000/virtual-traders/register \
  -H "Content-Type: application/json" \
  -d '{"name":"hermes-trader-1","base_strategy":"momentum"}'

# List all virtual traders
curl http://docker.klo:5000/virtual-traders

# Get a trader's config
curl http://docker.klo:5000/trader/kairos/config

# Enable exploration mode
curl -X PATCH http://docker.klo:5000/trader/hermes-trader-1/config \
  -H "Content-Type: application/json" \
  -d '{"exploration_mode": true, "max_position_pct": 5.0}'
```

---

## Table of Contents

1. [Health & Discovery](#1-health--discovery)
2. [Market Data](#2-market-data)
3. [Fundamentals & Sentiment](#3-fundamentals--sentiment)
4. [News & Social](#4-news--social)
5. [Macro & Indices](#5-macro--indices)
6. [Trader Intercom](#6-trader-intercom)
7. [Virtual Trader Management](#7-virtual-trader-management)
8. [Trader Config (Exploration Mode)](#8-trader-config)
9. [Risk & Analytics](#9-risk--analytics)
10. [Dashboard & Debug](#10-dashboard--debug)

---

## 1. Health & Discovery

### `GET /health`

Service health check with uptime, cache stats, and scheduler status.

```bash
curl http://docker.klo:5000/health
```

**Response:**
```json
{
  "status": "ok",
  "service": "data-bus",
  "started_at": "2026-07-12T10:00:00",
  "uptime_seconds": 3600.0,
  "cache_stats": { "keys": 42, "entries": ["quote:AAPL", "quote:MSFT", "..."] },
  "signal_count": 3,
  "tracked_symbols": 50,
  "schedulers": [
    { "name": "quotes", "running": true, "run_count": 720, "last_run": 100.0, "interval": 5 }
  ]
}
```

### `GET /discover`

Lists every available endpoint with its method, description, and parameters.

```bash
curl http://docker.klo:5000/discover
```

**Response:** A JSON object where each key is a route path with details.

---

## 2. Market Data

### `GET /quotes?symbols=AAPL,TSLA,NVDA`

Latest quote data (OHLCV) for stock symbols. Uses Alpaca Data API.

| Param     | Type   | Required | Description                  |
|-----------|--------|----------|------------------------------|
| `symbols` | string | Yes      | Comma-separated ticker list  |

```bash
curl "http://docker.klo:5000/quotes?symbols=AAPL,TSLA,NVDA"
```

**Response:**
```json
{
  "AAPL": { "close": 198.50, "open": 197.80, "high": 199.20, "low": 197.50, "volume": 52000000, "timestamp": "2026-07-12T15:30:00", "source": "alpaca_direct" },
  "TSLA": { "close": 265.30, "open": 264.10, "high": 267.00, "low": 263.50, "volume": 35000000, "timestamp": "2026-07-12T15:30:00", "source": "alpaca_direct" }
}
```

### `GET /crypto?symbols=BTC/USD,ETH/USD`

Latest crypto quotes. 24/7 market.

| Param     | Type   | Required | Description                      |
|-----------|--------|----------|----------------------------------|
| `symbols` | string | Yes      | Comma-separated pairs (e.g. BTC/USD,ETH/USD) |

```bash
curl "http://docker.klo:5000/crypto?symbols=BTC/USD,ETH/USD"
```

### `GET /bars?symbols=AAPL,MSFT&start_date=2026-07-01&end_date=2026-07-12&interval=daily`

Historical OHLCV bars for backtesting and analysis.

| Param        | Type   | Required | Default    | Description                    |
|--------------|--------|----------|------------|--------------------------------|
| `symbols`    | string | Yes      | —          | Comma-separated ticker list     |
| `start_date` | string | Yes      | —          | ISO start date (2026-06-01)    |
| `end_date`   | string | Yes      | —          | ISO end date (2026-07-02)      |
| `interval`   | string | No       | `daily`    | `daily` or `intraday`          |

### `GET /ml-signal?symbol=AAPL`

ML-driven trading signal for a specific symbol.

| Param    | Type   | Required | Description          |
|----------|--------|----------|----------------------|
| `symbol` | string | Yes      | Single ticker symbol |

---

## 3. Fundamentals & Sentiment

### `GET /fundamentals?symbol=AAPL`

Fundamental data: P/E ratio, EPS, market cap, dividend yield, analyst targets.

| Param    | Type   | Required | Description          |
|----------|--------|----------|----------------------|
| `symbol` | string | Yes      | Single ticker symbol |

```bash
curl "http://docker.klo:5000/fundamentals?symbol=AAPL"
```

**Response:**
```json
{
  "symbol": "AAPL",
  "fundamentals": {
    "pe_ratio": 28.5,
    "eps": 6.94,
    "dividend_yield": 0.005,
    "analyst_target": 215.0,
    "market_cap": 2900000000000,
    "description": "Apple Inc. designs, manufactures, and markets smartphones..."
  },
  "source": "live"
}
```

### `GET /sentiment?symbol=AAPL`

News/social sentiment analysis via FinBERT (Mac GPU) or keyword fallback.

| Param    | Type   | Required | Description          |
|----------|--------|----------|----------------------|
| `symbol` | string | Yes      | Single ticker symbol |

### `POST /sentiment`

Submit text for sentiment analysis.

```bash
curl -X POST http://docker.klo:5000/sentiment \
  -H "Content-Type: application/json" \
  -d '{"text": "AAPL reported record revenue this quarter", "ticker": "AAPL"}'
```

### `GET /sentiment-divergence?symbol=TSM`

Cross-language sentiment divergence comparing English vs Traditional Chinese analysis.

| Param    | Type   | Required | Description              |
|----------|--------|----------|--------------------------|
| `symbol` | string | Yes      | Single ticker symbol     |

### `GET /options?symbol=AAPL`

Options chain / stock snapshot from Alpaca.

| Param    | Type   | Required | Description          |
|----------|--------|----------|----------------------|
| `symbol` | string | Yes      | Single ticker symbol |

---

## 4. News & Social

### `GET /news?symbol=AAPL&limit=10`

News headlines from Alpaca API. Omitting `symbol` returns general market news.

| Param    | Type   | Required | Default | Description              |
|----------|--------|----------|---------|--------------------------|
| `symbol` | string | No       | —       | Filter by ticker symbol  |
| `limit`  | int    | No       | 10      | Max number of articles   |

```bash
curl "http://docker.klo:5000/news?symbol=AAPL&limit=5"
```

### `GET /news-cache?limit=30&source=marketwatch&days=1`

RSS news feed from Postgres `news_cache` table. Read-only, no live fetching.

| Param    | Type   | Required | Default | Description                   |
|----------|--------|----------|---------|-------------------------------|
| `limit`  | int    | No       | 30      | Max articles                  |
| `source` | string | No       | —       | Filter by source (marketwatch, yahoo, etc.) |
| `days`   | int    | No       | 1       | How many days back to search  |

### `GET /news/search?q=AAPL&limit=20`

Full-text search on news article title + summary using Postgres ILIKE.

| Param   | Type   | Required | Default | Description          |
|---------|--------|----------|---------|----------------------|
| `q`     | string | Yes      | —       | Search query         |
| `limit` | int    | No       | 20      | Max results          |

```bash
curl "http://docker.klo:5000/news/search?q=AAPL+earnings"
```

### `GET /social?source=all&fast=true`

Social media sentiment from Bluesky, Stocktwits, and Reddit.

| Param    | Type   | Required | Default | Description                                 |
|----------|--------|----------|---------|---------------------------------------------|
| `source` | string | No       | `all`   | One of: `bluesky`, `stocktwits`, `reddit`, `all` |
| `fast`   | bool   | No       | false   | Skip live fetch, return cached immediately   |
| `live`   | bool   | No       | false   | Force live fetch with 10s timeout            |

---

## 5. Macro & Indices

### `GET /macro`

Macroeconomic indicators from FRED + LoneStarOracle MCP: CPI, PCE, unemployment, yields, GDP.

```bash
curl http://docker.klo:5000/macro
```

**Response:**
```json
{
  "macro": {
    "indicators": {
      "CPI": { "series_id": "CPIAUCSL", "date": "2026-06-01", "value": "314.5" },
      "unemployment": { "series_id": "UNRATE", "date": "2026-06-01", "value": "4.1" },
      "DGS10": { "series_id": "DGS10", "date": "2026-07-11", "value": "4.35" }
    },
    "yields": {
      "2yr": 4.18, "10yr": 4.35, "30yr": 4.62,
      "spread_10y2y": 0.17, "spread_30y10y": 0.27,
      "curve_status": "flat"
    }
  }
}
```

### `GET /earnings?symbols=AAPL,MSFT`

Earnings calendar from Nasdaq.

| Param     | Type   | Required | Description                      |
|-----------|--------|----------|----------------------------------|
| `symbols` | string | No       | Comma-separated ticker filter    |

### `GET /fear_greed`

Fear & Greed Index from alternative.me.

```bash
curl http://docker.klo:5000/fear_greed
```

### `GET /flow?symbol=AAPL`

Unusual options flow from Unusual Whales RSS.

| Param    | Type   | Required | Description              |
|----------|--------|----------|--------------------------|
| `symbol` | string | No       | Filter by ticker symbol  |

### `GET /insiders?symbols=JPM,BAC`

SEC Form 4 insider filings from EDGAR.

| Param     | Type   | Required | Description                      |
|-----------|--------|----------|----------------------------------|
| `symbols` | string | No       | Comma-separated ticker filter    |

### `GET /congress`

Congressional trading data.

---

## 6. Trader Intercom

### `GET /signals`

Returns all active trader signals (non-stale, within 15 min).

### `POST /signals`

Publish your current trading bias.

```bash
curl -X POST http://docker.klo:5000/signals \
  -H "Content-Type: application/json" \
  -d '{"agent": "hermes-trader-1", "ticker": "AAPL", "bias": "bullish", "conviction": 0.8, "note": "Strong earnings momentum"}'
```

**Body fields:**

| Field        | Type   | Required | Description                          |
|--------------|--------|----------|--------------------------------------|
| `agent`      | string | Yes      | Agent/strategy name                  |
| `ticker`     | string | Yes      | Ticker symbol (uppercased)           |
| `bias`       | string | No       | `bullish`, `bearish`, or `neutral`   |
| `conviction` | float  | No       | Confidence level (0.0–1.0)           |
| `note`       | string | No       | Free-text reasoning                   |

---

## 7. Virtual Trader Management

### `POST /virtual-traders/register`

Register a new virtual trader for an external agent. Creates entry in `trading.virtual_traders` with status `probation`.

```bash
curl -X POST http://docker.klo:5000/virtual-traders/register \
  -H "Content-Type: application/json" \
  -d '{"name": "hermes-trader-1", "base_strategy": "momentum", "initial_params": {"momentum_threshold": 0.5}}'
```

**Request body:**

| Field             | Type   | Required | Description                                           |
|-------------------|--------|----------|-------------------------------------------------------|
| `name`            | string | Yes      | Unique trader name                                    |
| `base_strategy`   | string | Yes      | One of: `momentum`, `value`, `aggro`                  |
| `api_key`         | string | No       | Optional external API key for identity                 |
| `initial_params`  | object | No       | Optional JSON config overrides (SignalParams-compatible) |

**Response (201 Created):**
```json
{
  "id": 42,
  "name": "hermes-trader-1",
  "status": "probation",
  "base_strategy": "momentum",
  "created_at": "2026-07-12"
}
```

### `GET /virtual-traders`

List all registered virtual traders with their current status, win count, and 7-day P&L.

```bash
curl http://docker.klo:5000/virtual-traders
```

**Response:**
```json
{
  "count": 15,
  "traders": [
    {
      "id": 1,
      "name": "trader-kairos",
      "base_trader": "kairos",
      "variant_type": "baseline",
      "status": "live",
      "wins": 12,
      "pnl_7d": 456.78,
      "created_at": "2026-06-01"
    },
    {
      "id": 42,
      "name": "hermes-trader-1",
      "base_trader": "external",
      "variant_type": "params",
      "status": "probation",
      "wins": 0,
      "pnl_7d": 0.0,
      "created_at": "2026-07-12"
    }
  ]
}
```

### `GET /virtual-traders/leaderboard`

Leaderboard of active virtual traders ranked by 7-day P&L.

```bash
curl http://docker.klo:5000/virtual-traders/leaderboard
```

**Response:**
```json
{
  "count": 10,
  "leaderboard": [
    { "rank": 1, "name": "kairos-param-abc123", "base_trader": "kairos", "strategy": "params", "status": "active", "wins": 5, "pnl_7d": 1234.56 },
    { "rank": 2, "name": "stonks-prompt-def456", "base_trader": "stonks", "strategy": "prompt", "status": "active", "wins": 3, "pnl_7d": 789.01 }
  ]
}
```

---

## 8. Trader Config (Exploration Mode)

### `GET /trader/<agent>/config`

Get the current configuration for a trader agent (exploration mode, position sizing, etc.).

```bash
curl http://docker.klo:5000/trader/kairos/config
# or for a Hermes-registered trader:
curl http://docker.klo:5000/trader/hermes-trader-1/config
```

**Response:**
```json
{
  "agent": "kairos",
  "config": {
    "agent_id": "kairos",
    "exploration_mode": false,
    "exploration_started_at": null,
    "max_position_pct": 25.0,
    "conviction_threshold": 0.6,
    "watchlist_size": 20
  }
}
```

If no config row exists, returns defaults with a `note` field.

### `PATCH /trader/<agent>/config`

Update configuration fields for a trader. Uses upsert — create if not exists, update if exists.

```bash
# Enable exploration mode (small trades, lots of them)
curl -X PATCH http://docker.klo:5000/trader/hermes-trader-1/config \
  -H "Content-Type: application/json" \
  -d '{"exploration_mode": true, "max_position_pct": 5.0, "conviction_threshold": 0.3, "watchlist_size": 50}'

# Or set a single field
curl -X PATCH http://docker.klo:5000/trader/kairos/config \
  -H "Content-Type: application/json" \
  -d '{"max_position_pct": 20.0}'
```

**Request body fields:**

| Field                  | Type  | Description                                    |
|------------------------|-------|------------------------------------------------|
| `exploration_mode`     | bool  | Enable/disable small-trades exploration mode   |
| `max_position_pct`     | float | Max position size as % of portfolio (e.g. 5.0) |
| `conviction_threshold` | float | Min conviction to enter a trade (0.0–1.0)      |
| `watchlist_size`       | int   | Max watchlist size                             |

---

## 9. Risk & Analytics

### `GET /risk?symbols=AAPL,MSFT`

Portfolio risk scoring via LoneStarOracle MCP.

### `GET /technical-scan?symbol=AAPL`

Multi-timeframe technical scan (LoneStarOracle).

### `GET /equity-analysis?symbol=AAPL`

Deep equity analysis report.

### `GET /percentile?symbols=AAPL,MSFT,NVDA&metric=momentum_1m`

Percentile rank within the tracked universe.

| Param     | Type   | Required | Default        | Description                              |
|-----------|--------|----------|----------------|------------------------------------------|
| `symbols` | string | Yes      | —              | Comma-separated tickers                  |
| `metric`  | string | No       | `momentum_1m`  | pe_ratio, momentum_1m, momentum_3m, etc. |

### `GET /momentum`

Cross-sectional momentum ranking for Kairos.

### `GET /tick-snapshot`

One-call-per-tick aggregation: quotes, regime, fear_greed, macro, signals, and portfolio state.

### `GET /overnight-sentiment?ticker=AAPL`

Overnight sentiment delta comparing pre-market vs 7-day trailing baseline.

### `GET /source-quality?source=reddit&days=30`

Prediction accuracy per news/social source from the quality-tracking table.

---

## 10. Dashboard & Debug

### `GET /dashboard`

Live HTML dashboard showing cache stats, signal health, scheduler status, and data source health.

### `GET /debug`

⚠️ **SENSITIVE** — Contains API key status, rate limits, error traces. Should be LAN-only.

---

## Error Responses

All endpoints return standard HTTP status codes:

| Code | Meaning                        |
|------|--------------------------------|
| 200  | OK — successful response       |
| 201  | Created — new resource         |
| 400  | Bad Request — missing/invalid params |
| 404  | Not Found — data unavailable   |
| 409  | Conflict — duplicate resource  |
| 500  | Server Error — internal failure |
| 503  | Service Unavailable — data source down |

Error body format:
```json
{
  "error": "symbol parameter required"
}
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                      Data Bus (:5000)                        │
│   ┌──────────────────────────────────────────────────────┐   │
│   │  Flask HTTP Service                                  │   │
│   │  ┌──────────┐  ┌──────────┐  ┌──────────┐           │   │
│   │  │ Quotes    │  │ News     │  │ Macro    │  ...       │   │
│   │  │ Scheduler │  │ Scheduler│  │ Scheduler│           │   │
│   │  │ (5s)      │  │ (180s)   │  │ (6h)     │           │   │
│   │  └──────────┘  └──────────┘  └──────────┘           │   │
│   │  ┌────────────────────────────────────────────────┐   │   │
│   │  │ MemoryCache (in-memory, per-key TTL)           │   │   │
│   │  └────────────────────────────────────────────────┘   │   │
│   │  ┌────────────────────────────────────────────────┐   │   │
│   │  │ DbWriteQueue → SQLite (shared/cache.db) + PG   │   │   │
│   │  └────────────────────────────────────────────────┘   │   │
│   └──────────────────────────────────────────────────────┘   │
│                                                              │
│   External Sources:    Alpaca │ FRED │ Nasdaq │ SEC EDGAR    │
│                        Unusual Whales │ alternative.me       │
│   MCP Servers:         LoneStarOracle │ Praesentire         │
│   Mac GPU:             FinBERT (:5004) │ ML Worker (:5005)   │
└──────────────────────────────────────────────────────────────┘
```
