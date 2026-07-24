#!/usr/bin/env python3
"""
skill_combo_fetch.py — One Tool Call To Rule Them All

Parallel fetcher that replaces 5-6 sequential tool calls with one.
Fetches market hours, prices+technicals, fundamentals, portfolio, and ML
signals all concurrently. Cuts trader tick time from ~75s to ~10s.

Usage:
    python3 src/skill_combo_fetch.py --account aldridge --with-fundamentals
    python3 src/skill_combo_fetch.py --account kairos --with-ml MSFT
    python3 src/skill_combo_fetch.py --account stonks
    python3 src/skill_combo_fetch.py --account aldridge --tickers JPM BAC WFC

Output: Single JSON blob to stdout. All debug/log output goes to stderr.
"""

import sys
import json
import argparse
import os
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timedelta, date
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# Path & env setup — must happen before any imports that read os.environ
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# Third-party imports (no env-reading at module level)
import pandas as pd
import pandas_ta as ta
import requests
from dotenv import load_dotenv

from src import rate_limiter


# ---------------------------------------------------------------------------
# Env loading — per-account first, global fallback
# ---------------------------------------------------------------------------

def load_env(account: str) -> None:
    """
    Load environment variables for a trading account.

    Strategy:
    1. Load per-account env (~/.openclaw/workspace-trader-{account}/.env)
    2. Load global env (~/.openclaw/.env) with override=False
       → fills in ALPHA_VANTAGE_API_KEY, OPENROUTER_API_KEY, etc.
       without clobbering account-specific keys already loaded in step 1.
    """
    account_env = Path.home() / ".openclaw" / f"workspace-trader-{account}" / ".env"
    if account_env.exists():
        load_dotenv(account_env)

    global_env = Path.home() / ".openclaw" / ".env"
    if global_env.exists():
        load_dotenv(global_env, override=False)


def _get_alpaca_creds(account: str) -> tuple:
    """
    Get Alpaca API key + secret for a specific account.

    Accepts both naming conventions:
      - ALPACA_{ACCOUNT}_KEY / ALPACA_{ACCOUNT}_SECRET  (new style)
      - {ACCOUNT}_API_KEY / {ACCOUNT}_SECRET_KEY         (old style)
    """
    upper = account.upper()
    key = os.getenv(f"ALPACA_{upper}_KEY") or os.getenv(f"{upper}_API_KEY")
    secret = os.getenv(f"ALPACA_{upper}_SECRET") or os.getenv(f"{upper}_SECRET_KEY")
    return key, secret


def _get_any_alpaca_creds() -> tuple:
    """Return the first valid Alpaca credentials found across all accounts."""
    for acct in ("kairos", "aldridge", "stonks"):
        k, s = _get_alpaca_creds(acct)
        if k and s:
            return k, s
    return None, None


# ---------------------------------------------------------------------------
# Market hours — inlined to avoid import side-effects from market_hours.py
# ---------------------------------------------------------------------------

from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_MARKET_OPEN = (9, 30)
_MARKET_CLOSE = (16, 0)
_EARLY_CLOSE = (14, 0)

# Fixed holidays: (month, day)
_FIXED_HOLIDAYS = [(1, 1), (7, 4), (12, 25)]


def _get_nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime:
    """Return the nth occurrence of a weekday in a month."""
    d = datetime(year, month, 1)
    days_until = (weekday - d.weekday()) % 7
    d = d + timedelta(days=days_until + 7 * (n - 1))
    return d


def _get_last_weekday(year: int, month: int, weekday: int) -> datetime:
    """Return the last occurrence of a weekday in a month."""
    if month == 12:
        d = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = datetime(year, month + 1, 1) - timedelta(days=1)
    days_back = (d.weekday() - weekday) % 7
    return d - timedelta(days=days_back)


def _calculate_easter(year: int) -> datetime:
    """Calculate Easter Sunday (Computus algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime(year, month, day)


def _get_floating_holidays(year: int) -> list:
    """Return floating holidays for a year."""
    easter = _calculate_easter(year)
    return [
        _get_nth_weekday(year, 1, 0, 3),             # MLK Jr Day (3rd Mon Jan)
        _get_nth_weekday(year, 2, 0, 3),             # Presidents Day (3rd Mon Feb)
        easter - timedelta(days=2),                   # Good Friday
        _get_last_weekday(year, 5, 0),                # Memorial Day (last Mon May)
        _get_nth_weekday(year, 9, 0, 1),              # Labor Day (1st Mon Sep)
        _get_nth_weekday(year, 11, 3, 4),             # Thanksgiving (4th Thu Nov)
    ]


def _get_early_close_days(year: int) -> list:
    """Return early-close days for a year."""
    thanksgiving = _get_nth_weekday(year, 11, 3, 4)
    results = []
    # Day after Thanksgiving (Friday)
    day_after = thanksgiving + timedelta(days=1)
    if day_after.weekday() < 5:
        results.append(day_after)
    # Christmas Eve
    xmas_eve = datetime(year, 12, 24)
    if xmas_eve.weekday() < 5:
        results.append(xmas_eve)
    return results


def _is_holiday(d: date) -> bool:
    """Check if a date is a trading holiday."""
    for m, day_num in _FIXED_HOLIDAYS:
        if d.month == m and d.day == day_num:
            return True
    for h in _get_floating_holidays(d.year):
        if d == h.date():
            return True
    return False


def _is_early_close(d: date) -> bool:
    """Check if a date is an early-close day."""
    return any(ec.date() == d for ec in _get_early_close_days(d.year))


def check_market_open() -> dict:
    """
    Check whether the NYSE market is currently open.

    Returns a dict suitable for inclusion in the combo response.
    """
    try:
        now = datetime.now(ET)
        weekday = now.weekday()

        if weekday >= 5:
            return {"market_open": False, "reason": "weekend",
                    "next_open_approx": "Monday 9:30 AM ET"}

        if _is_holiday(now.date()):
            return {"market_open": False, "reason": "holiday",
                    "next_open_approx": "next trading day 9:30 AM ET"}

        close_time = _EARLY_CLOSE if _is_early_close(now.date()) else _MARKET_CLOSE
        current = (now.hour, now.minute)

        if current < _MARKET_OPEN:
            return {"market_open": False, "reason": "pre-market",
                    "opens_at": "9:30 AM ET"}
        elif current >= close_time:
            return {"market_open": False, "reason": "after-hours",
                    "closed_at": f"{close_time[0]:02d}:{close_time[1]:02d} ET"}
        else:
            return {"market_open": True,
                    "closes_at": f"{close_time[0]:02d}:{close_time[1]:02d} ET",
                    "is_early_close": _is_early_close(now.date())}

    except Exception as e:
        return {"market_open": None, "error": str(e)}


# ---------------------------------------------------------------------------
# Fetcher: Prices + Technical Indicators (Alpaca Data API)
# ---------------------------------------------------------------------------

def fetch_prices_indicators(tickers: list) -> dict:
    """
    Fetch day-level OHLCV bars and calculate technical indicators.

    One Alpaca Data API call for ~100 days of bars → extract latest price
    and compute RSI(14), MACD(12,26,9), MA(20) from the full history.

    Returns dict keyed by ticker, or None for tickers with insufficient data.
    """
    tickers = list(tickers)
    key, secret = _get_any_alpaca_creds()
    if not key or not secret:
        return {"error": "No Alpaca credentials available for data API"}

    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = StockHistoricalDataClient(key, secret)
        end_date = date.today()
        start_date = end_date - timedelta(days=100)

        print(f"[combo_fetch] Fetching bars for {len(tickers)} tickers via Alpaca Data API...",
              file=sys.stderr)

        request = StockBarsRequest(
            symbol_or_symbols=tickers,
            timeframe=TimeFrame.Day,
            start=start_date,
            end=end_date,
        )
        bars = client.get_stock_bars(request)
        bar_data = bars.data if hasattr(bars, 'data') else bars

        result = {}
        for ticker in tickers:
            if ticker not in bar_data or len(bar_data[ticker]) < 1:
                result[ticker] = None
                continue

            bars_list = bar_data[ticker]
            latest = bars_list[-1]
            closes = pd.Series([float(b.close) for b in bars_list])
            volumes = [int(b.volume) for b in bars_list]

            # --- Volume ratio (latest vs 20-day average) ---
            vol_ratio = None
            if len(volumes) >= 21:
                avg_vol_20d = sum(volumes[-21:-1]) / 20.0
                if avg_vol_20d > 0:
                    vol_ratio = round(volumes[-1] / avg_vol_20d, 2)

            # --- Price data ---
            prev_close = float(bars_list[-2].close) if len(bars_list) >= 2 else float(latest.close)
            change_pct = round((float(latest.close) - prev_close) / prev_close * 100, 2) if prev_close else None

            # --- Technical indicators (require ≥14 bars for RSI, ≥26 for MACD) ---
            rsi_val = macd_line = macd_signal_val = ma20_val = macd_status = None
            if len(bars_list) >= 14:
                try:
                    rsi_series = ta.rsi(closes, length=14)
                    if rsi_series is not None and not rsi_series.empty:
                        v = rsi_series.iloc[-1]
                        rsi_val = round(float(v), 1) if not pd.isna(v) else None
                except Exception:
                    pass

                try:
                    macd_df = ta.macd(closes, fast=12, slow=26, signal=9)
                    if macd_df is not None and not macd_df.empty:
                        ml = macd_df.iloc[-1, 0]
                        ms = macd_df.iloc[-1, 1]
                        macd_line = round(float(ml), 4) if not pd.isna(ml) else None
                        macd_signal_val = round(float(ms), 4) if not pd.isna(ms) else None
                except Exception:
                    pass

                try:
                    ma20_series = ta.sma(closes, length=20)
                    if ma20_series is not None and not ma20_series.empty:
                        v = ma20_series.iloc[-1]
                        ma20_val = round(float(v), 2) if not pd.isna(v) else None
                except Exception:
                    pass

                if macd_line is not None and macd_signal_val is not None:
                    macd_status = "bullish" if macd_line > macd_signal_val else "bearish"

            # MACD histogram (diff = MACD line - signal line) from column index 2
            macd_hist = None
            if macd_df is not None and not macd_df.empty:
                try:
                    mh = macd_df.iloc[-1, 2]
                    macd_hist = round(float(mh), 4) if not pd.isna(mh) else None
                except Exception:
                    pass

            result[ticker] = {
                "price": round(float(latest.close), 2),
                "open": round(float(latest.open), 2),
                "high": round(float(latest.high), 2),
                "low": round(float(latest.low), 2),
                "volume": volumes[-1] if volumes else None,
                "volume_ratio": vol_ratio,
                "change_pct": change_pct,
                "rsi": rsi_val,
                "macd": macd_status,
                "ma20": ma20_val,
                "macd_line": macd_line,
                "macd_signal": macd_signal_val,
                "macd_diff": macd_hist,
                "data_bars": len(bars_list),
            }

        print(f"[combo_fetch] Got price+indicator data for {sum(1 for v in result.values() if v is not None)}/{len(tickers)} tickers",
              file=sys.stderr)
        return result

    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Fetcher: Finnhub Real-Time Quotes
# ---------------------------------------------------------------------------

def _fetch_single_finnhub_quote(ticker: str) -> tuple:
    """Fetch a single real-time quote from Finnhub. Returns (ticker, data_or_None)."""
    try:
        from src.finnhub_fetcher import get_quote
        result = get_quote(ticker)
        if result.get("status") == "ok" and "quote" in result:
            q = result["quote"]
            return ticker, {
                "price": q.get("current"),
                "open": q.get("open"),
                "high": q.get("high"),
                "low": q.get("low"),
                "change_pct": q.get("change_pct"),
                "previous_close": q.get("previous_close"),
            }
        return ticker, None
    except Exception as e:
        print(f"[combo_fetch] Finnhub quote error for {ticker}: {e}", file=sys.stderr)
        return ticker, None


def fetch_finnhub_quotes(tickers: list) -> dict:
    """
    Fetch real-time quotes from Finnhub for a list of tickers.
    Slower than Alpaca bars but provides true real-time prices.
    """
    tickers = list(tickers)
    print(f"[combo_fetch] Fetching Finnhub quotes for {len(tickers)} tickers...",
          file=sys.stderr)

    result = {}
    # Finnhub free tier: 60 calls/min. Use 2 workers with stagger.
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {}
        for i, t in enumerate(tickers):
            futures[ex.submit(_fetch_single_finnhub_quote, t)] = t
            time.sleep(0.05)  # 50ms stagger to avoid burst

        for f in as_completed(futures):
            try:
                t, data = f.result()
                if data:
                    result[t] = data
            except Exception as e:
                print(f"[combo_fetch] Finnhub quote future error: {e}", file=sys.stderr)

    print(f"[combo_fetch] Got Finnhub quotes for {len(result)}/{len(tickers)} tickers",
          file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# Fetcher: Finnhub Congressional Trading
# ---------------------------------------------------------------------------

def fetch_congressional_trading(tickers: list) -> dict:
    """
    Fetch congressional trading data from Finnhub for a list of tickers.
    Returns summary signals: tickers with recent buys, sells, cluster buys.
    """
    tickers = list(tickers)
    print(f"[combo_fetch] Fetching congressional trading for {len(tickers)} tickers via Finnhub...",
          file=sys.stderr)

    try:
        from src.finnhub_fetcher import fetch_all_congressional
        result = fetch_all_congressional(tickers)
        return result.get("summary", {})
    except Exception as e:
        print(f"[combo_fetch] Congressional trading error: {e}", file=sys.stderr)
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Fetcher: Fundamentals (Alpha Vantage)
# ---------------------------------------------------------------------------

def _fetch_single_fundamental(ticker: str, api_key: str) -> tuple:
    """Fetch fundamentals for a single ticker from Alpha Vantage."""
    try:
        url = (f"https://www.alphavantage.co/query"
               f"?function=OVERVIEW&symbol={ticker}&apikey={api_key}")
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return ticker, None

        data = resp.json()

        # Alpha Vantage rate-limit note / error
        if "Note" in data:
            print(f"[combo_fetch] Alpha Vantage rate limit: {data['Note'][:80]}",
                  file=sys.stderr)
            return ticker, None
        if "Error Message" in data:
            print(f"[combo_fetch] Alpha Vantage error for {ticker}: {data['Error Message']}",
                  file=sys.stderr)
            return ticker, None
        # Empty response (invalid ticker)
        if not data or "Symbol" not in data:
            return ticker, None

        def _f(key):
            v = data.get(key)
            if v is None or v == "None" or v == "":
                return None
            return v

        return ticker, {
            "pe_ratio": float(_f("PERatio")) if _f("PERatio") else None,
            "eps": float(_f("EPS")) if _f("EPS") else None,
            "dividend_yield": float(_f("DividendYield")) if _f("DividendYield") else None,
            "analyst_target": float(_f("AnalystTargetPrice")) if _f("AnalystTargetPrice") else None,
            "roe": float(_f("ReturnOnEquityTTM")) if _f("ReturnOnEquityTTM") else None,
            "market_cap": int(_f("MarketCapitalization")) if _f("MarketCapitalization") else None,
        }

    except Exception as e:
        print(f"[combo_fetch] Error fetching fundamentals for {ticker}: {e}", file=sys.stderr)
        return ticker, None


def fetch_fundamentals(tickers: list) -> dict:
    """
    Fetch company fundamentals from Alpha Vantage.

    Respects the 5-req/min rate limit by using max 5 parallel workers and a
    small stagger. Alpha Vantage free tier = 25 req/day — tracked in
    shared/rate_limits.json (rate_limiter.py) and checked PER TICKER before
    each real request, so we stop making doomed calls once the daily budget
    is gone instead of silently getting empty results back.

    Returns dict keyed by ticker. Tickers skipped for being over the daily
    limit get {"rate_limited": True} instead of real fundamentals or being
    silently omitted — callers need to tell "no data" apart from "budget's
    gone for today" (2026-07-24: previously indistinguishable).
    """
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY") or os.getenv("ALPHA_VANTAGE_KEY")
    if not api_key:
        return {"error": "ALPHA_VANTAGE_API_KEY / ALPHA_VANTAGE_KEY not set"}

    tickers = list(tickers)
    print(f"[combo_fetch] Fetching fundamentals for {len(tickers)} tickers via Alpha Vantage...",
          file=sys.stderr)

    result = {}
    fetchable = []
    for t in tickers:
        allowed, remaining = rate_limiter.check_and_increment("alpha_vantage", limit=25)
        if allowed:
            fetchable.append(t)
        else:
            print(f"[combo_fetch] {t}: Alpha Vantage daily limit reached, skipping", file=sys.stderr)
            result[t] = {"rate_limited": True, "resets_at": rate_limiter.resets_at()}

    # Max 5 concurrent to stay under Alpha Vantage's 5 req/min rate limit
    with ThreadPoolExecutor(max_workers=5) as ex:
        # Stagger submissions slightly to avoid burst rate-limiting
        futures = {}
        for i, t in enumerate(fetchable):
            futures[ex.submit(_fetch_single_fundamental, t, api_key)] = t
            if i < len(fetchable) - 1:
                time.sleep(0.25)  # 250ms stagger

        for f in as_completed(futures):
            try:
                t, data = f.result()
                if data:
                    result[t] = data
            except Exception as e:
                print(f"[combo_fetch] Fundamental future error: {e}", file=sys.stderr)

    print(f"[combo_fetch] Got fundamentals for {len(result)}/{len(tickers)} tickers",
          file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# Fetcher: Portfolio (Alpaca Trading API)
# ---------------------------------------------------------------------------

def fetch_portfolio(account: str) -> dict:
    """
    Fetch portfolio state from Alpaca for a specific account.

    Returns cash, equity, buying_power, positions list, and daily P&L.
    """
    key, secret = _get_alpaca_creds(account)
    if not key or not secret:
        return {"error": f"No Alpaca credentials for account '{account}'"}

    try:
        from alpaca.trading.client import TradingClient

        print(f"[combo_fetch] Fetching portfolio for {account}...", file=sys.stderr)

        client = TradingClient(key, secret, paper=True)
        acct = client.get_account()

        equity = float(acct.equity)
        last_equity = float(acct.last_equity) if acct.last_equity is not None else equity
        cash = float(acct.cash)
        buying_power = float(acct.buying_power)

        positions = []
        for p in client.get_all_positions():
            positions.append({
                "ticker": p.symbol,
                "qty": float(p.qty),
                "avg_entry": round(float(p.avg_entry_price), 2),
                "current_price": round(float(p.current_price), 2),
                "market_value": round(float(p.market_value), 2),
                "unrealized_pl": round(float(p.unrealized_pl), 2),
                "unrealized_plpc": round(float(p.unrealized_plpc) * 100, 2),
            })

        print(f"[combo_fetch] Portfolio: equity=${equity:.2f}, {len(positions)} positions",
              file=sys.stderr)

        return {
            "cash": round(cash, 2),
            "equity": round(equity, 2),
            "buying_power": round(buying_power, 2),
            "positions": positions,
            "daily_pnl": round(equity - last_equity, 2),
            "position_count": len(positions),
        }

    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Fetcher: ML Signal (local ML worker)
# ---------------------------------------------------------------------------

def fetch_ml_signal(ticker: str) -> dict:
    """
    Fetch momentum regime prediction from the ML worker endpoint.

    Falls back gracefully if the endpoint is unreachable.
    """
    endpoint = os.getenv("ML_ENDPOINT_URL", "http://localhost:5000")

    try:
        # First, fetch 90 days of historical bars for feature warmup
        key, secret = _get_any_alpaca_creds()
        if not key or not secret:
            return {"signal": "unavailable", "error": "No Alpaca credentials for data API"}

        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        print(f"[combo_fetch] Fetching ML signal for {ticker}...", file=sys.stderr)

        data_client = StockHistoricalDataClient(key, secret)
        end_date = date.today()
        start_date = end_date - timedelta(days=90)

        request = StockBarsRequest(
            symbol_or_symbols=[ticker],
            timeframe=TimeFrame.Day,
            start=start_date,
            end=end_date,
        )
        bars = data_client.get_stock_bars(request)
        bar_data = bars.data if hasattr(bars, 'data') else bars

        if ticker not in bar_data or len(bar_data[ticker]) < 20:
            return {"signal": "unavailable",
                    "error": f"Insufficient data for {ticker} (need 20+ bars)"}

        bars_list = bar_data[ticker]
        payload = {
            "dates":  [str(b.timestamp.date()) for b in bars_list],
            "open":   [float(b.open)   for b in bars_list],
            "high":   [float(b.high)   for b in bars_list],
            "low":    [float(b.low)    for b in bars_list],
            "close":  [float(b.close)  for b in bars_list],
            "volume": [int(b.volume)   for b in bars_list],
        }

        resp = requests.post(f"{endpoint}/predict/momentum_regime", json=payload, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            return {
                "ticker": ticker,
                "signal": data.get("signal"),
                "confidence": data.get("confidence"),
                "regime": data.get("regime"),
                "support": data.get("support"),
                "resistance": data.get("resistance"),
            }
        else:
            return {"signal": "unavailable",
                    "error": f"ML endpoint returned {resp.status_code}"}

    except requests.exceptions.ConnectionError:
        return {"signal": "unavailable",
                "error": f"ML endpoint unreachable at {endpoint}"}
    except requests.exceptions.Timeout:
        return {"signal": "unavailable", "error": "ML endpoint timeout"}
    except Exception as e:
        print(f"[combo_fetch] ML signal error: {e}", file=sys.stderr)
        return {"signal": "unavailable", "error": str(e)}


# ---------------------------------------------------------------------------
# Orchestrator: run all fetches in parallel
# ---------------------------------------------------------------------------

def combo_fetch(account: str, tickers: list,
                with_fundamentals: bool = False,
                with_ml: str = None) -> dict:
    """
    Run all fetches concurrently. Everything is parallel — market hours,
    prices, portfolio, fundamentals, and ML signal all fire at the same time.

    Args:
        account: 'aldridge', 'kairos', or 'stonks'
        tickers: list of ticker symbols (required, no default)
        with_fundamentals: if True, also fetch Alpha Vantage fundamentals
        with_ml: ticker symbol to fetch ML momentum signal for (or None)

    Returns:
        dict with keys: market_open, timestamp, prices, [fundamentals],
                        portfolio, [ml_signal]
    """
    if not tickers:
        return {"error": "tickers is required (no default)", "timestamp": datetime.now().isoformat()}

    start = time.time()
    result = {
        "timestamp": datetime.now().isoformat(),
    }

    # Build task list
    tasks = {}

    with ThreadPoolExecutor(max_workers=10) as ex:
        # Always fetch market hours (instant, no API call)
        future_market = ex.submit(check_market_open)
        tasks["market"] = future_market

        # Always fetch prices + indicators
        future_prices = ex.submit(fetch_prices_indicators, tickers)
        tasks["prices"] = future_prices

        # Always fetch portfolio
        future_portfolio = ex.submit(fetch_portfolio, account)
        tasks["portfolio"] = future_portfolio

        # Optional: fundamentals
        if with_fundamentals:
            future_fund = ex.submit(fetch_fundamentals, tickers)
            tasks["fundamentals"] = future_fund

        # Optional: ML signal
        if with_ml:
            future_ml = ex.submit(fetch_ml_signal, with_ml)
            tasks["ml"] = future_ml

        # Collect results as they complete
        for key, future in tasks.items():
            try:
                data = future.result(timeout=60)
                if key == "market":
                    result["market_open"] = data.get("market_open")
                    result["market_details"] = data
                elif key == "prices":
                    result["prices"] = data if isinstance(data, dict) else {"error": str(data)}
                elif key == "portfolio":
                    result["portfolio"] = data
                elif key == "fundamentals":
                    result["fundamentals"] = data
                elif key == "ml":
                    result["ml_signal"] = data
            except Exception as e:
                print(f"[combo_fetch] Task '{key}' failed: {e}", file=sys.stderr)
                if key == "market":
                    result["market_open"] = None
                    result["market_details"] = {"error": str(e)}
                else:
                    result[key] = {"error": str(e)}

    elapsed = time.time() - start
    result["_elapsed_seconds"] = round(elapsed, 2)
    print(f"[combo_fetch] All fetches complete in {elapsed:.1f}s", file=sys.stderr)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Combo Fetch — parallel market data, fundamentals, portfolio, ML"
    )
    parser.add_argument("--account", required=True,
                        choices=["aldridge", "kairos", "stonks"],
                        help="Trading account to fetch portfolio for")
    parser.add_argument("--tickers", nargs="*",
                        help="Ticker symbols (space-separated). Default: full watchlist.")
    parser.add_argument("--with-fundamentals", action="store_true",
                        help="Also fetch Alpha Vantage fundamentals (costs API quota)")
    parser.add_argument("--with-ml", metavar="TICKER",
                        help="Also fetch ML momentum signal for this ticker")

    args = parser.parse_args()

    # Load env BEFORE any fetches
    load_env(args.account)

    tickers = args.tickers
    if not tickers:
        # Default: full watchlist for this account from shared/trader.db
        try:
            db_path = _PROJECT_ROOT / "shared" / "trader.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA busy_timeout=5000")
            rows = conn.execute(
                "SELECT ticker FROM trader_watchlist WHERE trader_id = ? ORDER BY ticker",
                (args.account,)
            ).fetchall()
            conn.close()
            tickers = [row[0] for row in rows]
        except Exception as e:
            print(f"[combo_fetch] Failed to load watchlist from DB: {e}", file=sys.stderr)
            tickers = []

    # Run
    result = combo_fetch(
        account=args.account,
        tickers=tickers if tickers else None,
        with_fundamentals=args.with_fundamentals,
        with_ml=args.with_ml,
    )

    # Output: JSON only to stdout
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
