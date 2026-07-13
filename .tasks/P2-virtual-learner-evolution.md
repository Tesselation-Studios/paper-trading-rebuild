# P2: Virtual Trader Learning Loop — compete, compare, evolve

> **From:** Neko-chan 🐱 | **Priority:** P2 | **Depends on:** P0-deploy-virtual-runner

## What
The existing virtual runners (`virtual_runner.py`, `virtual_rotate.py`, `virtual_cull.py`) generate variants but don't have a learning loop that compares strategies across virtuals and evolves the best ones. Add a nightly comparison + evolution pipeline.

## Architecture
```
Every night (20:00 ET):
  → Load ALL virtual traders' decisions + P&L for today
  → Group by signal type, sector, position size, holding period
  → For each strategy variant:
      ├─ Win rate by signal type
      ├─ Average P&L per trade
      ├─ Sharpe ratio (if enough trades)
      └─ Best/worst tickers
  → Compare against LIVE trader baseline
  → Find: "Which virtual strategy would have outperformed the live trader today?"
  → Cull bottom 3 virtuals
  → Generate 3 new variants from top-performing strategy
  → Log comparison metrics to trading.rotation_log
```

## Steps

### 1. Create `src/virtual_learner.py`
```python
"""Learning loop for virtual traders — compare, evolve, improve."""
import psycopg2
from datetime import datetime, date, timedelta
from collections import defaultdict
import json

def analyze_virtual_performance(conn, target_date=None):
    """Compare all virtual traders + live trader for a given date."""
    target_date = target_date or date.today()
    cur = conn.cursor()
    
    # 1. Load all trades for this date
    cur.execute("""
        SELECT trader_id, ticker, entry_price, exit_price, shares, pnl, 
               trade_source, regime
        FROM trading.trades
        WHERE entry_time::date = %s
    """, (target_date,))
    
    # 2. Group by trader and compute metrics
    traders = defaultdict(lambda: {
        "trades": [], "wins": 0, "losses": 0, "total_pnl": 0.0,
        "tickers": set(), "regimes": defaultdict(int)
    })
    
    for row in cur.fetchall():
        trader_id, ticker, entry, exit_p, shares, pnl, source, regime = row
        t = traders[trader_id]
        t["trades"].append({
            "ticker": ticker, "pnl": float(pnl or 0),
            "regime": regime, "source": source
        })
        t["total_pnl"] += float(pnl or 0)
        if pnl and float(pnl) > 0: t["wins"] += 1
        elif pnl: t["losses"] += 1
        t["tickers"].add(ticker)
        if regime: t["regimes"][regime] += 1
    
    # 3. Compute metrics for each trader
    results = {}
    for tid, data in traders.items():
        n = len(data["trades"])
        results[tid] = {
            "trader_id": tid,
            "trades": n,
            "wins": data["wins"],
            "losses": data["losses"],
            "win_rate": round(data["wins"] / max(n, 1), 3),
            "total_pnl": round(data["total_pnl"], 2),
            "avg_pnl": round(data["total_pnl"] / max(n, 1), 2),
            "unique_tickers": len(data["tickers"]),
            "top_regime": max(data["regimes"], key=data["regimes"].get) if data["regimes"] else None,
        }
    
    # 4. Rank by P&L
    ranked = sorted(results.values(), key=lambda x: x["total_pnl"], reverse=True)
    
    # 5. Log rotation
    cur.execute("""
        INSERT INTO trading.rotation_log (date, comparison_json)
        VALUES (%s, %s)
    """, (target_date, json.dumps({"ranked": ranked})))
    conn.commit()
    
    return ranked

def suggest_new_variants(conn, ranked, n_new=3):
    """Generate new virtual trader variants from top performers."""
    if not ranked:
        return []
    
    # Top 3 traders' strategies = seed for new variants
    top_traders = [r for r in ranked if r["trades"] >= 3][:3]
    if not top_traders:
        return []
    
    # Perturbation strategies
    variants = []
    for i, t in enumerate(top_traders):
        perturbations = [
            {"momentum_threshold": 0.20, "variant_type": "tighter"},
            {"momentum_threshold": 0.30, "variant_type": "looser"},
            {"rsi_oversold": 25, "variant_type": "conservative_rsi"},
        ][:n_new]
        
        for p in perturbations[:max(1, n_new // len(top_traders))]:
            variants.append({
                "base_trader": t["trader_id"],
                "variant_type": p["variant_type"],
                "config": json.dumps(p),
                "status": "active",
                "equity": 10000.00,
            })
    
    return variants
```

### 2. Add to nightly cron
Schedule: 20:30 ET daily (after market close, after rotation)

### 3. Known strategy variants to seed
```
1. "momentum_tighter"   — momentum_threshold=0.20, smaller positions
2. "momentum_looser"    — momentum_threshold=0.30, bigger positions
3. "rsi_conservative"   — only buy RSI < 25, sell RSI > 75
4. "volume_priority"    — require volume_ratio > 2.0 before any buy
5. "sector_diversified" — max 1 per sector, spread across 5+ sectors
6. "tech_focused"       — only tech stocks (AAPL, MSFT, NVDA, etc.)
7. "mean_reversion"     — buy RSI < 30, sell RSI > 70
8. "news_aggressive"    — buy on positive news sentiment + volume
```

## Verification
```sql
SELECT * FROM trading.rotation_log ORDER BY date DESC LIMIT 5;
```