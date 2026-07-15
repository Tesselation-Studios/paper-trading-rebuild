#!/usr/bin/env python3
"""
Stock Discovery — find undervalued/momentum stocks that fit the bankroll ceiling.

Fetches momentum-ranked candidates from the data bus, checks prices, and
returns a ranked list of stocks that fit within a price range.

Usage:
    python3 scripts/stock_discovery.py --ceiling 100           # stocks ≤ $33 (3 shares)
    python3 scripts/stock_discovery.py --ceiling 100 --agent stonks
    python3 scripts/stock_discovery.py --ceiling 100 --max-price 50
    python3 scripts/stock_discovery.py --ceiling 100 --json    # machine-readable
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_BUS = os.getenv("DATA_BUS_URL", "http://docker.klo:5000")
STATE_DIR = Path(__file__).resolve().parent.parent / "state"


# ── Known liquid stocks under $50 ─────────────────────────────────────────────
# Broad universe for scanning. Agents can expand beyond this via social discovery.
CORE_UNIVERSE = [
    # Fintech
    "SOFI", "UPST", "AFRM", "HOOD",
    # EV / Clean energy
    "RIVN", "NIO", "CHWY", "LCID",
    # Meme / Retail
    "GME", "AMC", "FUBO", "BB",
    # Tech
    "SNAP", "PLTR", "RKLB", "ASTS",
    # Crypto proxy
    "COIN", "MARA", "RIOT", "CLSK",
    # Consumer
    "F", "INTC", "AMD", "MU", "WBD", "DISH",
    # Energy
    "XOM", "CVX", "OXY", "COP",
    # Biotech
    "MRNA", "BNTX", "CRSP",
    # Airlines / Travel
    "AAL", "DAL", "UAL", "LYFT", "UBER",
    # Gaming
    "CROX", "DECK", "ONON",
    # Streaming
    "ROKU", "NFLX", "SPOT",
]


def fetch_quotes(symbols: list[str]) -> dict:
    """Fetch quote data from data bus."""
    joined = ",".join(symbols)
    url = f"{DATA_BUS}/quotes?symbols={urllib.parse.quote(joined)}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            # Data bus returns {quotes: {SYM: {...}}}, unwrap if needed
            if "quotes" in data:
                return data["quotes"]
            return data
    except Exception as e:
        print(f"[WARN] Quote fetch failed: {e}", file=sys.stderr)
        return {}


def fetch_momentum() -> dict:
    """Fetch momentum ranking from data bus."""
    url = f"{DATA_BUS}/momentum"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[WARN] Momentum fetch failed: {e}", file=sys.stderr)
        return {}


def fetch_technical_scan(symbol: str) -> dict:
    """Fetch technical scan for a single symbol."""
    url = f"{DATA_BUS}/technical-scan?symbol={symbol}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("technical_scan") or data
    except Exception as e:
        return {}


def discover_stocks(
    ceiling: float,
    max_price: Optional[float] = None,
    min_shares: int = 2,
    top_n: int = 10,
    include_technical: bool = False,
) -> Dict[str, Any]:
    """Stock screener for Kairos — systematic discovery from momentum-ranked universe.

    Primary source: GET /momentum cross-sectional ranking (data bus).
    Secondary source: broad liquid universe (fintech, EV, retail, crypto, biotech, etc.)

    Filters by price to fit bankroll ceiling, ranks by composite momentum score.

    Args:
        ceiling: Bankroll ceiling in dollars (portfolio × 0.01)
        max_price: Maximum stock price. Defaults to ceiling / min_shares.
        min_shares: Minimum shares to buy (for price filtering).
        top_n: Max candidates to return.
        include_technical: Whether to include technical scan data.

    Returns:
        Dict with candidates, metadata.
    """
    if max_price is None:
        max_price = ceiling / min_shares

    # 1. Primary source: momentum-ranked universe from data bus
    momentum_data = fetch_momentum()
    momentum_picks = momentum_data.get("top_buys", [])
    momentum_avoids = momentum_data.get("top_avoids", [])

    # 2. Build the screening universe: momentum picks + broad liquid universe
    screen_universe = list(set(momentum_picks + CORE_UNIVERSE))
    quotes = fetch_quotes(screen_universe)

    # 2. Filter by price
    candidates = []
    for sym, data in quotes.items():
        if not isinstance(data, dict):
            continue
        price = data.get("close") or data.get("price")
        if price is None:
            continue
        price = float(price)
        if price <= 0:
            continue
        if price > max_price:
            continue

        # Max shares we could buy at this price
        max_shares = int(ceiling / price)
        if max_shares < 1:
            continue

        volume = data.get("volume", 0)
        rsi = data.get("rsi")
        change_pct = data.get("change_pct")
        volume_ratio = data.get("volume_ratio")

        # Momentum score (0-1): weighted combination of signals
        momentum_score = _compute_momentum_score(data, sym in momentum_picks)

        candidates.append({
            "symbol": sym,
            "price": price,
            "max_shares": max_shares,
            "cost_3_shares": round(price * 3, 2),
            "change_pct": change_pct,
            "volume": volume,
            "volume_ratio": volume_ratio,
            "rsi": rsi,
            "in_momentum_top": sym in momentum_picks,
            "momentum_score": round(momentum_score, 3),
            "quote": data,
        })

    # 3. Sort by momentum score descending
    candidates.sort(key=lambda c: c["momentum_score"], reverse=True)

    # 4. Include technical scans for top candidates
    if include_technical:
        for c in candidates[:5]:
            c["technical_scan"] = fetch_technical_scan(c["symbol"])

    top_candidates = candidates[:top_n]

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "ceiling": ceiling,
        "max_price": max_price,
        "min_shares": min_shares,
        "total_screened": len(candidates),
        "candidates": top_candidates,
        "momentum_regime": momentum_data.get("market_regime", "unknown"),
        "momentum_top_buys": momentum_picks,
        "momentum_avoids": momentum_avoids,
    }


def _compute_momentum_score(quote: dict, in_momentum_top: bool) -> float:
    """Compute a composite momentum score (0-1) from quote data."""
    score = 0.0
    signals = 0

    # Price change (0.3 weight)
    change = quote.get("change_pct")
    if change is not None:
        change = float(change)
        if change > 0:
            score += min(change / 10, 0.3)  # capped at 10% change = 0.3
        signals += 1

    # RSI (0.3 weight) — ideal range 55-75
    rsi = quote.get("rsi")
    if rsi is not None:
        rsi = float(rsi)
        if 55 <= rsi <= 75:
            score += 0.3  # Sweet spot: momentum without being overbought
        elif 45 <= rsi < 55:
            score += 0.15  # Neutral, room to run
        elif rsi > 75:
            score += 0.1  # Overbought, risky but momentum
        signals += 1

    # Volume ratio (0.2 weight)
    vol_ratio = quote.get("volume_ratio")
    if vol_ratio is not None:
        vol_ratio = float(vol_ratio)
        if vol_ratio >= 2.0:
            score += 0.2
        elif vol_ratio >= 1.5:
            score += 0.1
        signals += 1

    # In momentum top (0.2 weight)
    if in_momentum_top:
        score += 0.2
    signals += 1

    return score / (signals if signals > 0 else 1)


def format_discovery_report(result: Dict[str, Any]) -> str:
    """Format discovery results as a human-readable report."""
    lines = []
    lines.append(f"# Stock Discovery Report")
    lines.append(f"Fetched: {result['fetched_at']}")
    lines.append(f"Bankroll ceiling: ${result['ceiling']:.2f} | Max price: ${result['max_price']:.2f}")
    lines.append(f"Momentum regime: {result.get('momentum_regime', 'unknown')} (from GET /momentum)")
    lines.append(f"Stocks screened: {result['total_screened']} | Showing: top {len(result['candidates'])}")
    lines.append(f"Momentum top buys: {', '.join(result.get('momentum_top_buys', [])) or 'none within price range'}")
    lines.append("")

    if not result["candidates"]:
        lines.append("No candidates found in this price range.")
        lines.append("Expand the ceiling or lower min_shares to discover more.")
        return "\n".join(lines)

    # Table header
    lines.append("| Rank | Symbol | Price | 3x Cost | Chg% | RSI | Vol Ratio | Momentum | In Top | Max Shares |")
    lines.append("|------|--------|-------|---------|------|-----|-----------|----------|--------|------------|")

    for i, c in enumerate(result["candidates"], 1):
        chg = f"{c['change_pct']:+.2f}%" if c['change_pct'] else "N/A"
        rsi = f"{c['rsi']:.1f}" if c['rsi'] else "N/A"
        vr = f"{c['volume_ratio']:.1f}x" if c['volume_ratio'] else "N/A"
        top = "⭐" if c["in_momentum_top"] else ""
        lines.append(
            f"| {i} | {c['symbol']:6s} | ${c['price']:<6.2f} | "
            f"${c['cost_3_shares']:<6.2f} | {chg:<6s} | "
            f"{rsi:<4s} | {vr:<7s} | "
            f"{c['momentum_score']:<7.3f} | {top:<6s} | {c['max_shares']:<3d} |"
        )

    lines.append("")
    lines.append("## Discovery Notes")

    # Highlight best candidates
    if result["candidates"]:
        best = result["candidates"][0]
        lines.append(f"**Top pick**: {best['symbol']} at ${best['price']:.2f}")
        lines.append(f"  - Buy {best['max_shares']} shares for ${best['max_shares'] * best['price']:.2f} "
                     f"({best['max_shares'] * best['price'] / result['ceiling'] * 100:.0f}% of ceiling)")
        if best["rsi"]:
            lines.append(f"  - RSI {best['rsi']:.1f} — "
                         f"{'momentum sweet spot' if 55 <= best['rsi'] <= 75 else 'building momentum' if best['rsi'] > 45 else 'oversold recovery' if best['rsi'] <= 45 else 'overbought caution' if best['rsi'] > 75 else 'neutral'}")

    if len(result["candidates"]) > 1:
        lines.append("")
        lines.append("**Also consider:**")
        for c in result["candidates"][1:4]:
            lines.append(f"  - {c['symbol']} at ${c['price']:.2f} "
                         f"(momentum {c['momentum_score']:.2f}, "
                         f"{c['max_shares']} shares for ${c['max_shares'] * c['price']:.2f})")

    lines.append("")
    lines.append("### How to Use")
    lines.append("1. Review the top candidates for community buzz (social, news, flow)")
    lines.append("2. Run the entry gate before submitting any order")
    lines.append("3. Log discovery notes to strategy_notes/<DATE>_discovery.md")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Stock Discovery — find stocks that fit the bankroll")
    parser.add_argument("--ceiling", type=float, default=100.0, help="Bankroll ceiling")
    parser.add_argument("--agent", default="stonks", help="Agent name (for portfolio value)")
    parser.add_argument("--max-price", type=float, default=None, help="Max stock price")
    parser.add_argument("--min-shares", type=int, default=2, help="Minimum shares to buy")
    parser.add_argument("--top-n", type=int, default=10, help="Top N candidates to return")
    parser.add_argument("--technical", action="store_true", help="Include technical scans")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--save", action="store_true", help="Save report to state/")
    args = parser.parse_args()

    # Try to get portfolio value from heartbeat-state.json
    if args.agent:
        hb_path = STATE_DIR / "heartbeat-state.json"
        if hb_path.exists():
            try:
                data = json.loads(hb_path.read_text())
                val = data.get(f"last_{args.agent}")
                if val:
                    args.ceiling = round(float(val) * 0.01, 2)
            except Exception:
                pass

    result = discover_stocks(
        ceiling=args.ceiling,
        max_price=args.max_price,
        min_shares=args.min_shares,
        top_n=args.top_n,
        include_technical=args.technical,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        report = format_discovery_report(result)
        print(report)

        if args.save:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            path = STATE_DIR / f"discovery_{args.agent}_{date_str}.md"
            path.write_text(report)
            print(f"\nReport saved to {path}")


if __name__ == "__main__":
    main()