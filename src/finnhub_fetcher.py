#!/usr/bin/env python3
"""
Finnhub Data Fetcher — Congressional Trading, Insider Transactions, SEC Filings

Fetches congressional trading data, insider transactions, insider sentiment,
SEC filings, company news, and real-time quotes from Finnhub.

Finnhub free tier: 60 API calls/minute. This module includes a rate-limit
throttle to stay within that limit. Results are cached to a local JSON file
with a 1-hour TTL.

Usage:
    # Fetch congressional trading for a single ticker
    python3 src/finnhub_fetcher.py --ticker AAPL --endpoint congressional

    # Fetch all endpoints for a ticker
    python3 src/finnhub_fetcher.py --ticker MSFT --all

    # Fetch congressional + insider for the Stonks watchlist
    python3 src/finnhub_fetcher.py --watchlist

    # Fetch real-time quote
    python3 src/finnhub_fetcher.py --quote AAPL

    # CLI help
    python3 src/finnhub_fetcher.py --help
"""

import sys
import json
import os
import time
import argparse
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import Optional, Dict, List, Any

import requests
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Finnhub free tier: 60 requests/minute
FINNHUB_RATE_LIMIT_RPM = 60
FINNHUB_RATE_LIMIT_PERIOD = 60.0  # seconds

# Cache settings — 2026-07-31 port: this module originally lived inside
# workspace-trader-stonks itself (the pre-consolidation paper-trading-teams
# layout); ported into paper-trading-rebuild (data_bus.py's actual repo),
# so the cache belongs under THIS repo's own shared/cache dir, not the old
# hardcoded path into a different project's workspace.
CACHE_DIR = Path(__file__).resolve().parent.parent / "shared" / "cache"
CACHE_FILE = CACHE_DIR / "finnhub_cache.json"
CACHE_TTL_SECONDS = 3600  # 1 hour

# Request timeout
REQUEST_TIMEOUT = 15  # seconds

# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------

def _load_env():
    """Load environment for FINNHUB_API_KEY.

    2026-07-31 port: when running inside data_bus.py (databus.service),
    systemd's EnvironmentFile already injects this repo's own .env
    (paper-trading-rebuild/.env, where FINNHUB_API_KEY actually lives —
    confirmed NOT present in ~/.openclaw/.env) directly into the process
    environment before Python starts, so os.getenv() already works with
    zero calls here. This function exists for the module's standalone CLI
    usage (`python3 src/finnhub_fetcher.py ...`), where nothing has loaded
    that yet -- load the repo-local .env first (override=False, since a
    real env var set by the caller should win), falling back to the old
    global-then-workspace-trader-stonks search in case this ever runs
    somewhere those still apply."""
    repo_env = Path(__file__).resolve().parent.parent / ".env"
    if repo_env.exists():
        load_dotenv(repo_env, override=False)

    global_env = Path.home() / ".openclaw" / ".env"
    if global_env.exists():
        load_dotenv(global_env, override=False)

    stonks_env = Path.home() / ".openclaw" / "workspace-trader-stonks" / ".env"
    if stonks_env.exists():
        load_dotenv(stonks_env, override=False)


def _get_api_key() -> Optional[str]:
    """Get Finnhub API key from environment."""
    _load_env()
    return os.getenv("FINNHUB_API_KEY")


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple rate limiter for Finnhub's 60 calls/min free tier."""

    def __init__(self, max_calls: int = FINNHUB_RATE_LIMIT_RPM,
                 period: float = FINNHUB_RATE_LIMIT_PERIOD):
        self.max_calls = max_calls
        self.period = period
        self.calls: List[float] = []

    def wait_if_needed(self):
        """Block if we've exceeded the rate limit."""
        now = time.time()
        # Remove calls older than the period
        self.calls = [t for t in self.calls if now - t < self.period]

        if len(self.calls) >= self.max_calls:
            sleep_time = self.calls[0] + self.period - now + 0.1
            if sleep_time > 0:
                print(f"[finnhub] Rate limit reached, sleeping {sleep_time:.1f}s...",
                      file=sys.stderr)
                time.sleep(sleep_time)
            self.calls = [t for t in self.calls
                          if time.time() - t < self.period]

        self.calls.append(time.time())


# Global rate limiter instance
_rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Finnhub API call helper
# ---------------------------------------------------------------------------

def _finnhub_call(endpoint: str, params: Dict[str, Any] = None) -> dict:
    """
    Make a rate-limited call to Finnhub API.

    Args:
        endpoint: Finnhub API endpoint path (e.g., '/stock/congressional-trading')
        params: URL query parameters (token is added automatically)

    Returns:
        Parsed JSON response as dict, or {'error': ...} on failure
    """
    api_key = _get_api_key()
    if not api_key:
        return {"error": "FINNHUB_API_KEY not set in environment"}

    _rate_limiter.wait_if_needed()

    url = f"https://finnhub.io/api/v1{endpoint}"
    all_params = {"token": api_key}
    if params:
        all_params.update(params)

    try:
        resp = requests.get(url, params=all_params, timeout=REQUEST_TIMEOUT)

        if resp.status_code == 429:
            print(f"[finnhub] Rate limit hit (429), backing off 5s...",
                  file=sys.stderr)
            time.sleep(5)
            return {"error": "rate_limited", "status_code": 429}

        if resp.status_code == 401 or resp.status_code == 403:
            return {"error": f"API auth failed (HTTP {resp.status_code})"}

        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "raw": resp.text[:500]}

        data = resp.json()
        return data

    except requests.exceptions.Timeout:
        return {"error": f"Request timeout after {REQUEST_TIMEOUT}s"}
    except requests.exceptions.ConnectionError:
        return {"error": "Connection error — Finnhub API unreachable"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    """Load Finnhub cache from disk."""
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict):
    """Save Finnhub cache to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, default=str))


def _cache_get(key: str) -> Optional[dict]:
    """Get a cached value if it exists and is not expired."""
    cache = _load_cache()
    entry = cache.get(key)
    if not entry:
        return None

    cached_at = entry.get("_cached_at", "")
    try:
        age = (datetime.now() - datetime.fromisoformat(cached_at)).total_seconds()
    except (ValueError, TypeError):
        return None

    if age > CACHE_TTL_SECONDS:
        return None

    return entry.get("data")


def _cache_set(key: str, data: Any):
    """Set a cache entry with the current timestamp."""
    cache = _load_cache()
    cache[key] = {
        "_cached_at": datetime.now().isoformat(),
        "data": data,
    }
    _save_cache(cache)


def _cache_invalidate(key: str):
    """Remove a specific cache key."""
    cache = _load_cache()
    cache.pop(key, None)
    _save_cache(cache)


# ---------------------------------------------------------------------------
# Finnhub Endpoints
# ---------------------------------------------------------------------------

def get_congressional_trading(symbol: str, use_cache: bool = True) -> dict:
    """
    Fetch congressional trading data for a symbol from Finnhub.

    Finnhub endpoint: GET /stock/congressional-trading

    Returns:
        dict with 'status', 'symbol', 'trades' (list), 'error' on failure
    """
    cache_key = f"congressional_{symbol.upper()}"

    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            cached["_from_cache"] = True
            return cached

    result = {
        "status": "ok",
        "symbol": symbol.upper(),
        "source": "finnhub",
        "fetched_at": datetime.now().isoformat(),
        "trades": [],
    }

    data = _finnhub_call("/stock/congressional-trading",
                         {"symbol": symbol.upper()})
    if "error" in data:
        result["status"] = "error"
        result["error"] = data["error"]
        return result

    # Finnhub returns {data: [...], symbol: "..."}
    trades = data.get("data", [])
    if not isinstance(trades, list):
        trades = []

    result["trades"] = [
        {
            "transaction_date": t.get("transactionDate", ""),
            "filing_date": t.get("filingDate", ""),
            "politician_name": t.get("name", ""),
            "chamber": t.get("chamber", ""),  # house / senate
            "party": "",  # Finnhub doesn't provide party
            "ticker": t.get("symbol", symbol.upper()),
            "asset_name": t.get("assetName", ""),
            "type": _normalize_trade_type(t.get("transactionType", "")),
            "amount_range": t.get("amount", ""),
            "owner": t.get("ownerType", ""),
            "ptr_link": t.get("reportUrl", ""),
        }
        for t in trades
    ]

    _cache_set(cache_key, result)
    return result


def get_insider_transactions(symbol: str, use_cache: bool = True) -> dict:
    """
    Fetch insider transactions (Form 4 filings) for a symbol.

    Finnhub endpoint: GET /stock/insider-transactions

    Returns:
        dict with 'status', 'symbol', 'transactions' (list)
    """
    cache_key = f"insider_tx_{symbol.upper()}"

    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            cached["_from_cache"] = True
            return cached

    result = {
        "status": "ok",
        "symbol": symbol.upper(),
        "source": "finnhub",
        "fetched_at": datetime.now().isoformat(),
        "transactions": [],
    }

    data = _finnhub_call("/stock/insider-transactions",
                         {"symbol": symbol.upper()})
    if "error" in data:
        result["status"] = "error"
        result["error"] = data["error"]
        return result

    # Finnhub returns {data: [...], symbol: "..."}
    txs = data.get("data", [])
    if not isinstance(txs, list):
        txs = []

    result["transactions"] = [
        {
            "name": tx.get("name", ""),
            "position": tx.get("position", ""),
            "share": tx.get("share"),
            "change": tx.get("change"),
            "transaction_date": tx.get("transactionDate", ""),
            "transaction_type": tx.get("transactionCode", ""),  # P=Purchase, S=Sale
            "transaction_price": tx.get("transactionPrice"),
            "filing_date": tx.get("filingDate", ""),
            "company": tx.get("companyName", ""),
            "isin": tx.get("isin", ""),
        }
        for tx in txs
    ]

    _cache_set(cache_key, result)
    return result


def get_insider_sentiment(symbol: str, use_cache: bool = True) -> dict:
    """
    Fetch insider sentiment data (aggregated insider buy/sell trends).

    Finnhub endpoint: GET /stock/insider-sentiment

    Returns:
        dict with 'status', 'symbol', 'sentiment' (dict with monthly data)
    """
    cache_key = f"insider_sentiment_{symbol.upper()}"

    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            cached["_from_cache"] = True
            return cached

    result = {
        "status": "ok",
        "symbol": symbol.upper(),
        "source": "finnhub",
        "fetched_at": datetime.now().isoformat(),
        "sentiment": {},
    }

    # Insider sentiment requires from/to dates — default to last 12 months
    end = date.today()
    start = end - timedelta(days=365)

    data = _finnhub_call("/stock/insider-sentiment", {
        "symbol": symbol.upper(),
        "from": start.isoformat(),
        "to": end.isoformat(),
    })
    if "error" in data:
        result["status"] = "error"
        result["error"] = data["error"]
        return result

    # Finnhub returns {data: [...], symbol: "..."}
    entries = data.get("data", [])
    if not isinstance(entries, list):
        entries = []

    # Aggregate: sum buys/sells, compute net insider ratio
    total_buy = sum(e.get("change", 0) for e in entries
                    if e.get("change", 0) > 0)
    total_sell = sum(abs(e.get("change", 0)) for e in entries
                     if e.get("change", 0) < 0)

    total_mspr = [e.get("mspr", 0) for e in entries
                  if isinstance(e.get("mspr"), (int, float))]
    avg_mspr = sum(total_mspr) / len(total_mspr) if total_mspr else 0

    result["sentiment"] = {
        "monthly_data": entries[-12:],  # Last 12 months
        "total_buy_shares": total_buy,
        "total_sell_shares": total_sell,
        "net_insider_ratio": round(total_buy / (total_buy + total_sell), 3)
        if (total_buy + total_sell) > 0 else 0,
        "avg_mspr": round(avg_mspr, 3),  # Monthly Share Purchase Ratio
        "signal": "bullish" if avg_mspr > 0 else "bearish" if avg_mspr < 0 else "neutral",
    }

    _cache_set(cache_key, result)
    return result


def get_sec_filings(symbol: str, use_cache: bool = True) -> dict:
    """
    Fetch SEC filings for a symbol.

    Finnhub endpoint: GET /stock/filings

    Returns:
        dict with 'status', 'symbol', 'filings' (list of recent filings)
    """
    cache_key = f"sec_filings_{symbol.upper()}"

    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            cached["_from_cache"] = True
            return cached

    result = {
        "status": "ok",
        "symbol": symbol.upper(),
        "source": "finnhub",
        "fetched_at": datetime.now().isoformat(),
        "filings": [],
    }

    # Finnhub filings: optional 'from'/'to' dates
    data = _finnhub_call("/stock/filings", {"symbol": symbol.upper()})
    if "error" in data:
        result["status"] = "error"
        result["error"] = data["error"]
        return result

    filings = data if isinstance(data, list) else data.get("data", [])

    # Filter to relevant filing types
    relevant_types = {"4", "4/A", "3", "5", "8-K", "10-K", "10-Q"}
    result["filings"] = [
        {
            "filing_date": f.get("filedDate", ""),
            "form_type": f.get("form", ""),
            "accession_number": f.get("accessNumber", ""),
            "report_url": f.get("reportUrl", ""),
            "period_of_report": f.get("periodOfReport", ""),
        }
        for f in filings[:30]  # Keep last 30 filings
    ]

    # Count Form 4 (insider) filings
    form_4_count = sum(1 for f in result["filings"]
                       if f["form_type"] in ("4", "4/A"))
    result["insider_filing_count"] = form_4_count

    _cache_set(cache_key, result)
    return result


def get_company_news(symbol: str, from_date: str = None,
                     to_date: str = None, use_cache: bool = True) -> dict:
    """
    Fetch company news from Finnhub.

    Finnhub endpoint: GET /company-news

    Args:
        symbol: Stock ticker
        from_date: Start date (YYYY-MM-DD), defaults to 7 days ago
        to_date: End date (YYYY-MM-DD), defaults to today

    Returns:
        dict with 'status', 'symbol', 'news' (list)
    """
    if to_date is None:
        to_date = date.today().isoformat()
    if from_date is None:
        from_date = (date.today() - timedelta(days=7)).isoformat()

    cache_key = f"news_{symbol.upper()}_{from_date}_{to_date}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            cached["_from_cache"] = True
            return cached

    result = {
        "status": "ok",
        "symbol": symbol.upper(),
        "source": "finnhub",
        "fetched_at": datetime.now().isoformat(),
        "from_date": from_date,
        "to_date": to_date,
        "news": [],
    }

    data = _finnhub_call("/company-news", {
        "symbol": symbol.upper(),
        "from": from_date,
        "to": to_date,
    })
    if "error" in data:
        result["status"] = "error"
        result["error"] = data["error"]
        return result

    articles = data if isinstance(data, list) else []
    result["news"] = [
        {
            "id": a.get("id"),
            "headline": a.get("headline", ""),
            "summary": a.get("summary", ""),
            "source": a.get("source", ""),
            "published_at": a.get("datetime"),
            "url": a.get("url", ""),
            "category": a.get("category", ""),
        }
        for a in articles[:20]
    ]

    _cache_set(cache_key, result)
    return result


def get_quote(symbol: str) -> dict:
    """
    Fetch real-time quote from Finnhub.

    Finnhub endpoint: GET /quote

    Note: Quotes are NOT cached (too volatile). Use sparingly.

    Returns:
        dict with 'status', 'symbol', 'quote' data
    """
    result = {
        "status": "ok",
        "symbol": symbol.upper(),
        "source": "finnhub",
        "fetched_at": datetime.now().isoformat(),
    }

    data = _finnhub_call("/quote", {"symbol": symbol.upper()})
    if "error" in data:
        result["status"] = "error"
        result["error"] = data["error"]
        return result

    result["quote"] = {
        "current": data.get("c"),   # Current price
        "change": data.get("d"),    # Change
        "change_pct": data.get("dp"),  # Percent change
        "high": data.get("h"),      # High price of the day
        "low": data.get("l"),       # Low price of the day
        "open": data.get("o"),      # Open price of the day
        "previous_close": data.get("pc"),  # Previous close price
        "timestamp": data.get("t"),  # Unix timestamp
    }

    return result


# ---------------------------------------------------------------------------
# Bulk fetch helpers
# ---------------------------------------------------------------------------

def fetch_all_congressional(tickers: List[str] = None) -> dict:
    """
    Fetch congressional trading data for all available tickers.

    First checks a known list of tickers with recent congressional activity,
    then fetches detailed data for each. Scans the watchlist by default.

    Args:
        tickers: List of ticker symbols (required)

    Returns:
        dict with 'status', 'fetched_at', 'results' (per-ticker dict),
        'summary' (aggregate signals)
    """
    if tickers is None:
        return {"status": "error", "error": "tickers is required"}

    result = {
        "status": "ok",
        "fetched_at": datetime.now().isoformat(),
        "results": {},
        "summary": {
            "total_trades_found": 0,
            "tickers_with_congressional_activity": [],
            "recent_buys": [],
            "recent_sells": [],
            "cluster_buys": [],
        },
    }

    # Track buys/sells across tickers for cluster detection
    ticker_buy_count = {}

    for ticker in tickers:
        data = get_congressional_trading(ticker)
        result["results"][ticker] = data

        if data["status"] != "ok":
            continue

        trades = data.get("trades", [])
        if not trades:
            continue

        result["summary"]["total_trades_found"] += len(trades)
        result["summary"]["tickers_with_congressional_activity"].append(ticker)

        # Check for recent trades (last 30 days)
        cutoff = datetime.now() - timedelta(days=30)
        for trade in trades:
            try:
                trade_date = datetime.strptime(
                    trade.get("transaction_date", ""), "%Y-%m-%d")
            except ValueError:
                try:
                    trade_date = datetime.strptime(
                        trade.get("filing_date", ""), "%Y-%m-%d")
                except ValueError:
                    continue

            if trade_date < cutoff:
                continue

            trade_type = trade.get("type", "").lower()
            if "buy" in trade_type or "purchase" in trade_type:
                result["summary"]["recent_buys"].append({
                    "ticker": ticker,
                    "politician": trade.get("politician_name", ""),
                    "chamber": trade.get("chamber", ""),
                    "amount": trade.get("amount_range", ""),
                    "date": trade.get("transaction_date", "") or trade.get("filing_date", ""),
                })
                ticker_buy_count[ticker] = ticker_buy_count.get(ticker, 0) + 1
            elif "sell" in trade_type or "sale" in trade_type:
                result["summary"]["recent_sells"].append({
                    "ticker": ticker,
                    "politician": trade.get("politician_name", ""),
                    "chamber": trade.get("chamber", ""),
                    "amount": trade.get("amount_range", ""),
                    "date": trade.get("transaction_date", "") or trade.get("filing_date", ""),
                })

    # Detect cluster buys (multiple congresspeople buying same ticker)
    for ticker, count in ticker_buy_count.items():
        if count >= 2:
            result["summary"]["cluster_buys"].append({
                "ticker": ticker,
                "congresspeople_count": count,
            })

    return result


def fetch_all_for_ticker(symbol: str, use_cache: bool = True) -> dict:
    """
    Fetch all Finnhub endpoints for a single ticker in one call.

    Useful as a single comprehensive data pull for a trade decision.

    Returns:
        dict with keys: symbol, quote, congressional, insider_tx,
                        insider_sentiment, sec_filings, news
    """
    result = {
        "status": "ok",
        "symbol": symbol.upper(),
        "fetched_at": datetime.now().isoformat(),
    }

    # Quote (never cached — always fresh)
    result["quote"] = get_quote(symbol)

    # Congressional trading
    result["congressional"] = get_congressional_trading(symbol, use_cache)

    # Insider transactions
    result["insider_tx"] = get_insider_transactions(symbol, use_cache)

    # Insider sentiment
    result["insider_sentiment"] = get_insider_sentiment(symbol, use_cache)

    # SEC filings
    result["sec_filings"] = get_sec_filings(symbol, use_cache)

    # Company news
    result["news"] = get_company_news(symbol, use_cache=use_cache)

    # Build a summary signal
    buy_signals = 0
    sell_signals = 0

    # Congressional buys = bullish
    congress_trades = result["congressional"].get("trades", [])
    recent_cutoff = datetime.now() - timedelta(days=30)
    for t in congress_trades:
        try:
            td = datetime.strptime(t.get("transaction_date", ""), "%Y-%m-%d")
        except ValueError:
            try:
                td = datetime.strptime(t.get("filing_date", ""), "%Y-%m-%d")
            except ValueError:
                continue
        if td >= recent_cutoff:
            ttype = t.get("type", "").lower()
            if "buy" in ttype or "purchase" in ttype:
                buy_signals += 1
            elif "sell" in ttype or "sale" in ttype:
                sell_signals += 1

    # Insider buys = very bullish
    insider_txs = result["insider_tx"].get("transactions", [])
    for tx in insider_txs:
        if tx.get("transaction_type") in ("P", "Purchase"):
            buy_signals += 1
        elif tx.get("transaction_type") in ("S", "Sale"):
            sell_signals += 1

    # Insider sentiment
    sentiment = result["insider_sentiment"].get("sentiment", {})
    if sentiment.get("signal") == "bullish":
        buy_signals += 1
    elif sentiment.get("signal") == "bearish":
        sell_signals += 1

    result["summary_signal"] = {
        "buy_signals": buy_signals,
        "sell_signals": sell_signals,
        "net_signal": "bullish" if buy_signals > sell_signals
        else "bearish" if sell_signals > buy_signals
        else "neutral",
        "conviction": min(abs(buy_signals - sell_signals) / 5.0, 1.0),
    }

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_trade_type(finnhub_type: str) -> str:
    """Normalize Finnhub trade type to a consistent format."""
    t = finnhub_type.lower().strip()
    if "purchase" in t or "buy" in t:
        return "buy"
    elif "sale" in t or "sell" in t:
        return "sell"
    elif "exchange" in t:
        return "exchange"
    return finnhub_type  # Return as-is if unknown


def _parse_amount_range(amount_str: str) -> tuple:
    """
    Parse an amount range string like '$1,001 - $15,000' into (low, high).
    Returns (None, None) if unparseable.
    """
    if not amount_str:
        return None, None
    try:
        parts = amount_str.replace("$", "").replace(",", "").split("-")
        low = int(parts[0].strip()) if len(parts) > 0 else None
        high = int(parts[1].strip()) if len(parts) > 1 else None
        return low, high
    except (ValueError, IndexError):
        return None, None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Finnhub Data Fetcher — Congressional trading, insider data, SEC filings, quotes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --ticker AAPL --endpoint congressional
  %(prog)s --ticker MSFT --all
  %(prog)s --watchlist
  %(prog)s --quote AAPL
  %(prog)s --ticker NVDA --endpoint insider_tx
  %(prog)s --ticker AAPL --endpoint news
  %(prog)s --invalidate-cache
""")

    parser.add_argument("--ticker", "-t", help="Single ticker symbol")
    parser.add_argument("--quote", "-q", help="Get real-time quote for a ticker (alias for --ticker X --endpoint quote)")
    parser.add_argument("--endpoint", "-e",
                        choices=["congressional", "insider_tx", "insider_sentiment",
                                 "sec_filings", "news", "quote"],
                        help="Specific Finnhub endpoint to call")
    parser.add_argument("--all", "-a", action="store_true",
                        help="Fetch all endpoints for the given ticker")
    parser.add_argument("--watchlist", "-w", action="store_true",
                        help="Fetch congressional trading for given tickers (requires --tickers)")
    parser.add_argument("--tickers", nargs="+",
                        help="List of ticker symbols (required for --watchlist)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Skip cache, fetch fresh data")
    parser.add_argument("--invalidate-cache", action="store_true",
                        help="Clear the entire Finnhub cache")
    parser.add_argument("--compact", action="store_true",
                        help="Output compact JSON (no pretty-printing)")

    args = parser.parse_args()

    # Handle --invalidate-cache
    if args.invalidate_cache:
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
            print(json.dumps({"status": "ok", "message": "Finnhub cache cleared"}))
        else:
            print(json.dumps({"status": "ok", "message": "No cache file found"}))
        sys.exit(0)

    # Handle --quote shorthand
    if args.quote:
        args.ticker = args.quote
        args.endpoint = "quote"

    # Determine the output
    indent = None if args.compact else 2
    use_cache = not args.no_cache

    # --watchlist mode
    if args.watchlist:
        if not args.tickers:
            print(json.dumps({"status": "error", "error": "--tickers is required with --watchlist"}), file=sys.stderr)
            sys.exit(1)
        result = fetch_all_congressional(args.tickers)
        print(json.dumps(result, indent=indent, default=str))
        sys.exit(0)

    # --ticker with a specific endpoint
    if args.ticker and args.endpoint:
        endpoint_map = {
            "congressional": get_congressional_trading,
            "insider_tx": get_insider_transactions,
            "insider_sentiment": get_insider_sentiment,
            "sec_filings": get_sec_filings,
            "news": get_company_news,
            "quote": get_quote,
        }
        func = endpoint_map[args.endpoint]
        if args.endpoint == 'quote':
            result = func(args.ticker)
        else:
            result = func(args.ticker, use_cache=use_cache)
        print(json.dumps(result, indent=indent, default=str))
        sys.exit(0)

    # --ticker --all
    if args.ticker and args.all:
        result = fetch_all_for_ticker(args.ticker, use_cache=use_cache)
        print(json.dumps(result, indent=indent, default=str))
        sys.exit(0)

    # No valid arguments
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
