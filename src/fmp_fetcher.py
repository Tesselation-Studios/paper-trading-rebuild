#!/usr/bin/env python3
"""
FMP Fetcher — Congressional Trading (Financial Modeling Prep)

Free-tier replacement for Finnhub's congressional-trading endpoint, which
is paid-tier-gated (confirmed HTTP 403 on this account, 2026-07-31).

FMP's free tier does NOT include the per-symbol `senate-trades`/`house-trades`
endpoints (confirmed HTTP 402 "Restricted Endpoint" on this account) — only
the broad `senate-latest`/`house-latest` feeds work, capped at 100 records
per call (`limit` above 100 also 402s). Those feeds return the ~100 most
recent disclosures across ALL symbols (roughly a week-plus of activity,
2026-07-31 spot check: 100 records spanned 2026-07-16 to 2026-07-24) — so
this fetcher pulls the broad feed and filters by symbol locally, rather
than querying per-symbol. Real coverage, just recency-limited: a symbol
with no congressional activity in roughly the last 1-2 weeks will show no
trades here even if one happened further back. Good enough for "did a
member of congress recently trade this" as one pre-entry signal among
several — not a full historical lookup.

Usage:
    python3 src/fmp_fetcher.py --symbol AAPL
    python3 src/fmp_fetcher.py --symbol AAPL --no-cache
"""

import sys
import json
import os
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any

import requests
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FMP_BASE_URL = "https://financialmodelingprep.com/stable"
# Free tier defaults to 100 records/call -- but quirkily, passing ANY
# explicit `limit` param (even =99 or =100, the same as the default) 402s;
# only omitting the param entirely works (confirmed 2026-07-31). So the
# fetch calls below deliberately do NOT pass `limit`. Congressional
# disclosures lag the actual trade by weeks (STOCK Act filing deadline),
# so hourly freshness doesn't matter -- a long cache TTL conserves the
# 250 req/day free-tier budget.
CACHE_DIR = Path(__file__).resolve().parent.parent / "shared" / "cache"
CACHE_FILE = CACHE_DIR / "fmp_cache.json"
CACHE_TTL_SECONDS = 12 * 3600  # 12 hours

REQUEST_TIMEOUT = 15  # seconds


# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------

def _load_env():
    """Load environment for FMP_API_KEY — same repo-local-.env-first pattern
    as finnhub_fetcher.py, since systemd's EnvironmentFile already injects
    this when running inside databus.service."""
    repo_env = Path(__file__).resolve().parent.parent / ".env"
    if repo_env.exists():
        load_dotenv(repo_env, override=False)

    global_env = Path.home() / ".openclaw" / ".env"
    if global_env.exists():
        load_dotenv(global_env, override=False)


def _get_api_key() -> Optional[str]:
    _load_env()
    return os.getenv("FMP_API_KEY")


# ---------------------------------------------------------------------------
# Cache helpers (same shape as finnhub_fetcher.py)
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, default=str))


def _cache_get(key: str) -> Optional[Any]:
    cache = _load_cache()
    entry = cache.get(key)
    if not entry:
        return None
    try:
        age = (datetime.now() - datetime.fromisoformat(entry.get("_cached_at", ""))).total_seconds()
    except (ValueError, TypeError):
        return None
    if age > CACHE_TTL_SECONDS:
        return None
    return entry.get("data")


def _cache_set(key: str, data: Any):
    cache = _load_cache()
    cache[key] = {"_cached_at": datetime.now().isoformat(), "data": data}
    _save_cache(cache)


# ---------------------------------------------------------------------------
# FMP calls
# ---------------------------------------------------------------------------

def _fmp_call(endpoint: str, params: Dict[str, Any] = None) -> Any:
    """GET a `stable` FMP endpoint. Returns parsed JSON (list/dict), or a
    dict with 'error' on failure — matches finnhub_fetcher.py's convention."""
    api_key = _get_api_key()
    if not api_key:
        return {"error": "FMP_API_KEY not set in environment"}

    url = f"{FMP_BASE_URL}{endpoint}"
    all_params = {"apikey": api_key}
    if params:
        all_params.update(params)

    try:
        resp = requests.get(url, params=all_params, timeout=REQUEST_TIMEOUT)

        if resp.status_code == 402:
            return {"error": "endpoint or limit not available on this FMP plan (402)"}
        if resp.status_code == 429:
            return {"error": "rate_limited", "status_code": 429}
        if resp.status_code in (401, 403):
            return {"error": f"API auth failed (HTTP {resp.status_code})"}
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "raw": resp.text[:500]}

        return resp.json()
    except requests.exceptions.Timeout:
        return {"error": f"Request timeout after {REQUEST_TIMEOUT}s"}
    except requests.exceptions.ConnectionError:
        return {"error": "Connection error — FMP API unreachable"}
    except Exception as e:
        return {"error": str(e)}


def _fetch_latest_feed(chamber: str) -> List[dict]:
    """Fetch and cache the broad senate-latest or house-latest feed
    (unfiltered, ~100 most recent disclosures across all symbols)."""
    cache_key = f"latest_{chamber}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    endpoint = "/senate-latest" if chamber == "senate" else "/house-latest"
    data = _fmp_call(endpoint)
    if isinstance(data, dict) and "error" in data:
        # Don't cache errors — let the next call retry.
        return []
    if not isinstance(data, list):
        return []

    _cache_set(cache_key, data)
    return data


def get_congressional_trading(symbol: str, use_cache: bool = True) -> dict:
    """
    Fetch congressional trading data for a symbol, filtered from FMP's
    broad senate-latest/house-latest feeds (free-tier compatible).

    Returns the same shape as finnhub_fetcher.get_congressional_trading()
    so callers can swap sources without changing their handling:
        {'status', 'symbol', 'source', 'fetched_at', 'trades': [...]}
    """
    symbol_u = symbol.upper()
    result = {
        "status": "ok",
        "symbol": symbol_u,
        "source": "fmp",
        "fetched_at": datetime.now().isoformat(),
        "trades": [],
    }

    if not _get_api_key():
        result["status"] = "error"
        result["error"] = "FMP_API_KEY not set in environment"
        return result

    senate = _fetch_latest_feed("senate") if use_cache else _fmp_call("/senate-latest")
    house = _fetch_latest_feed("house") if use_cache else _fmp_call("/house-latest")

    if isinstance(senate, dict) and "error" in senate:
        senate = []
    if isinstance(house, dict) and "error" in house:
        house = []

    combined = [(t, "senate") for t in senate] + [(t, "house") for t in house]

    result["trades"] = [
        {
            "transaction_date": t.get("transactionDate", ""),
            "disclosure_date": t.get("disclosureDate", ""),
            "politician_name": f"{t.get('firstName', '')} {t.get('lastName', '')}".strip(),
            "chamber": chamber,
            "district": t.get("district", ""),
            "ticker": t.get("symbol", ""),
            "asset_name": t.get("assetDescription", ""),
            "asset_type": t.get("assetType", ""),
            "type": t.get("type", ""),
            "amount_range": t.get("amount", ""),
            "owner": t.get("owner", ""),
            "ptr_link": t.get("link", ""),
        }
        for t, chamber in combined
        if t.get("symbol", "").upper() == symbol_u
    ]
    result["coverage_note"] = (
        "Free-tier FMP only: reflects the most recent ~100 disclosures per "
        "chamber (roughly 1-2 weeks), not full history."
    )

    return result


def main():
    parser = argparse.ArgumentParser(description="Fetch congressional trading data via FMP")
    parser.add_argument("--symbol", required=True, help="Ticker symbol")
    parser.add_argument("--no-cache", action="store_true", help="Bypass cache, force live fetch")
    args = parser.parse_args()

    result = get_congressional_trading(args.symbol, use_cache=not args.no_cache)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
