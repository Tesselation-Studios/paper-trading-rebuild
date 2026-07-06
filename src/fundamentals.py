"""Fundamental data pipeline for Aldridge (buy-and-hold value investor).

Fetches fundamental metrics via yahooquery and stores them in
market_data.fundamentals. Supports single-ticker fetch and bulk backfill.

Usage:
    python3 -m src.fundamentals backfill           # backfill all Aldridge tickers
    python3 -m src.fundamentals backfill --ticker AAPL  # single ticker
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

from src.db.connection import get_connection

log = logging.getLogger("fundamentals")

# ── Default tickers for Aldridge (value-oriented) ─────────────────────────────

ALDRIDGE_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "JPM", "V", "WMT", "JNJ", "XOM", "UNH", "MA", "HD", "PG",
    "BAC", "CVX", "PFE", "KO", "PEP", "T", "VZ", "INTC",
]


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class Fundamentals:
    """Fundamental data for a single ticker at a point in time."""

    ticker: str
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    market_cap: Optional[float] = None
    dividend_yield: Optional[float] = None
    earnings_growth: Optional[float] = None
    revenue_growth: Optional[float] = None
    debt_to_equity: Optional[float] = None
    free_cash_flow: Optional[float] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    fetched_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for DB insertion."""
        return {
            "ticker": self.ticker,
            "pe_ratio": self.pe_ratio,
            "pb_ratio": self.pb_ratio,
            "market_cap": self.market_cap,
            "dividend_yield": self.dividend_yield,
            "earnings_growth": self.earnings_growth,
            "revenue_growth": self.revenue_growth,
            "debt_to_equity": self.debt_to_equity,
            "free_cash_flow": self.free_cash_flow,
            "sector": self.sector,
            "industry": self.industry,
            "fetched_at": self.fetched_at or datetime.now(timezone.utc),
        }


# ── Fetcher ───────────────────────────────────────────────────────────────────


def _safe_float(value: Any) -> Optional[float]:
    """Convert value to float, returning None if not possible."""
    if value is None:
        return None
    try:
        f = float(value)
        if f != f:  # NaN check
            return None
        return f
    except (ValueError, TypeError):
        return None


def fetch_fundamentals(ticker: str) -> Fundamentals:
    """Fetch fundamental data for a single ticker using yahooquery.

    Args:
        ticker: Stock symbol (e.g. 'AAPL').

    Returns:
        Fundamentals dataclass with all available metrics.
    """
    from yahooquery import Ticker

    yq = Ticker(ticker)
    result = Fundamentals(ticker=ticker, fetched_at=datetime.now(timezone.utc))

    try:
        # Summary detail (valuation ratios)
        summary = yq.summary_detail
        if summary and ticker in summary:
            s = summary[ticker]
            result.pe_ratio = _safe_float(s.get("trailingPE"))
            result.pb_ratio = _safe_float(s.get("priceToBook"))
            result.market_cap = _safe_float(s.get("marketCap"))
            result.dividend_yield = _safe_float(s.get("dividendYield"))
            if result.dividend_yield is not None:
                # yahooquery returns yield as decimal (e.g. 0.005 = 0.5%)
                # Convert to percentage for screening
                result.dividend_yield *= 100.0

        # Key statistics (growth, D/E, FCF)
        stats = yq.key_stats
        if stats and ticker in stats:
            st = stats[ticker]
            result.earnings_growth = _safe_float(st.get("earningsGrowth"))
            if result.earnings_growth is not None:
                result.earnings_growth *= 100.0  # decimal → percentage
            result.revenue_growth = _safe_float(st.get("revenueGrowth"))
            if result.revenue_growth is not None:
                result.revenue_growth *= 100.0
            result.debt_to_equity = _safe_float(st.get("debtToEquity"))
            result.free_cash_flow = _safe_float(st.get("freeCashflow"))

        # Asset profile (sector/industry)
        profile = yq.asset_profile
        if profile and ticker in profile:
            p = profile[ticker]
            result.sector = p.get("sector")
            result.industry = p.get("industry")

        log.info("Fetched fundamentals for %s: PE=%s, PB=%s, Div=%.2f%%, EG=%.1f%%",
                 ticker, result.pe_ratio, result.pb_ratio,
                 result.dividend_yield or 0, result.earnings_growth or 0)

    except Exception as e:
        log.warning("Partial fetch for %s: %s", ticker, e)

    return result


# ── Storage ───────────────────────────────────────────────────────────────────


def store_fundamentals(
    fundamentals: Fundamentals,
    conn: Optional[psycopg2.extensions.connection] = None,
) -> bool:
    """Store fundamental data in market_data.fundamentals.

    Args:
        fundamentals: Fundamentals dataclass to store.
        conn: Database connection (creates new if None).

    Returns:
        True if stored successfully.
    """
    close_conn = conn is None
    if close_conn:
        conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO market_data.fundamentals
                   (ticker, pe_ratio, pb_ratio, market_cap, dividend_yield,
                    earnings_growth, revenue_growth, debt_to_equity,
                    free_cash_flow, sector, industry, fetched_at)
                   VALUES (%(ticker)s, %(pe_ratio)s, %(pb_ratio)s, %(market_cap)s,
                           %(dividend_yield)s, %(earnings_growth)s,
                           %(revenue_growth)s, %(debt_to_equity)s,
                           %(free_cash_flow)s, %(sector)s, %(industry)s,
                           %(fetched_at)s)
                   ON CONFLICT (ticker, fetched_at) DO NOTHING""",
                fundamentals.to_dict(),
            )
        return True
    except Exception as e:
        log.error("Failed to store fundamentals for %s: %s", fundamentals.ticker, e)
        return False
    finally:
        if close_conn:
            conn.close()


def backfill_all(
    tickers: Optional[List[str]] = None,
    delay: float = 0.5,
) -> Dict[str, bool]:
    """Fetch and store fundamentals for all tickers.

    Args:
        tickers: List of ticker symbols. Defaults to ALDRIDGE_TICKERS.
        delay: Seconds to wait between tickers (rate limiting).

    Returns:
        Dict mapping ticker → success boolean.
    """
    if tickers is None:
        tickers = ALDRIDGE_TICKERS

    conn = get_connection()
    results: Dict[str, bool] = {}

    for i, ticker in enumerate(tickers):
        log.info("[%d/%d] Fetching fundamentals for %s...", i + 1, len(tickers), ticker)
        try:
            f = fetch_fundamentals(ticker)
            ok = store_fundamentals(f, conn=conn)
            results[ticker] = ok
        except Exception as e:
            log.error("Failed to fetch %s: %s", ticker, e)
            results[ticker] = False

        if i < len(tickers) - 1:
            time.sleep(delay)

    conn.close()

    succeeded = sum(1 for v in results.values() if v)
    log.info("Backfill complete: %d/%d tickers stored", succeeded, len(tickers))
    return results


def load_fundamentals(
    ticker: str,
    conn: Optional[psycopg2.extensions.connection] = None,
) -> Optional[Fundamentals]:
    """Load the most recent fundamentals for a ticker from the database.

    Args:
        ticker: Stock symbol.
        conn: Database connection.

    Returns:
        Fundamentals or None if not found.
    """
    close_conn = conn is None
    if close_conn:
        conn = get_connection()

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM market_data.fundamentals
                   WHERE ticker = %s
                   ORDER BY fetched_at DESC
                   LIMIT 1""",
                (ticker,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return Fundamentals(
                ticker=row["ticker"],
                pe_ratio=row["pe_ratio"],
                pb_ratio=row["pb_ratio"],
                market_cap=row["market_cap"],
                dividend_yield=row["dividend_yield"],
                earnings_growth=row["earnings_growth"],
                revenue_growth=row["revenue_growth"],
                debt_to_equity=row["debt_to_equity"],
                free_cash_flow=row["free_cash_flow"],
                sector=row["sector"],
                industry=row["industry"],
                fetched_at=row["fetched_at"],
            )
    finally:
        if close_conn:
            conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fundamentals Pipeline — Aldridge value investing data"
    )
    sub = parser.add_subparsers(dest="command")

    backfill_p = sub.add_parser("backfill", help="Backfill fundamental data")
    backfill_p.add_argument("--ticker", default=None, help="Single ticker to fetch")
    backfill_p.add_argument("--delay", type=float, default=0.5,
                            help="Delay between tickers (seconds)")

    fetch_p = sub.add_parser("fetch", help="Fetch and display fundamentals for a ticker")
    fetch_p.add_argument("--ticker", required=True)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    if args.command == "backfill":
        if args.ticker:
            f = fetch_fundamentals(args.ticker)
            ok = store_fundamentals(f)
            print(f"Stored fundamentals for {args.ticker}: {'OK' if ok else 'FAILED'}")
        else:
            results = backfill_all(delay=args.delay)
            succeeded = sum(1 for v in results.values() if v)
            print(f"Backfill complete: {succeeded}/{len(results)} tickers stored")
            for t, ok in sorted(results.items()):
                print(f"  {t}: {'OK' if ok else 'FAILED'}")

    elif args.command == "fetch":
        f = fetch_fundamentals(args.ticker)
        print(f"Fundamentals for {f.ticker} (fetched {f.fetched_at}):")
        print(f"  P/E Ratio:       {f.pe_ratio}")
        print(f"  P/B Ratio:       {f.pb_ratio}")
        print(f"  Market Cap:      {f.market_cap}")
        print(f"  Dividend Yield:  {f.dividend_yield}%")
        print(f"  Earnings Growth: {f.earnings_growth}%")
        print(f"  Revenue Growth:  {f.revenue_growth}%")
        print(f"  Debt/Equity:     {f.debt_to_equity}")
        print(f"  Free Cash Flow:  {f.free_cash_flow}")
        print(f"  Sector:          {f.sector}")
        print(f"  Industry:        {f.industry}")


if __name__ == "__main__":
    main()
