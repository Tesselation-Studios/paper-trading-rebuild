# P2: News Discovery Pipeline — organic stock discovery via news sentiment

> **From:** Neko-chan 🐱 | **Priority:** P2 | **Depends on:** Nothing

## What
`market_data.news` table exists but is empty. Fill it with a cron that fetches news headlines + sentiment scores for the trader universe. Traders then discover stocks organically by querying "what stocks have positive news right now?"

## Architecture
```
Every 15 min during market hours:
  1. Fetch news for tickers in our universe (via Finnhub API or free NewsAPI)
  2. Score sentiment (-1 to +1) using keyword-based or FinBERT scoring
  3. Insert into market_data.news
  4. Traders query: "SELECT ticker, title, sentiment FROM market_data.news 
     WHERE published_at > now() - interval '1 hour' ORDER BY ABS(sentiment) DESC"
```

## Steps

### 1. Create the news fetcher script
File: `src/news_fetcher.py`

```python
"""Fetch news headlines + sentiment for trader universe."""
import json, time, logging, os
from datetime import datetime, timezone
from urllib.request import urlopen, Request
import psycopg2

UNIVERSE = ["AAPL","MSFT","NVDA","META","TSLA","AMD","PLTR","HOOD","COIN",
            "JPM","BAC","GS","MS","V","SPY","QQQ","IWM","AMZN","GOOGL",
            "NFLX","DIS","BA","CAT","GE","XOM","CVX","LLY","ABBV","KO",
            "PEP","WMT","COST","JNJ","UNH","INTC","CSCO","QCOM","TXN",
            "ADBE","CRM","ORCL","IBM","SMCI","MU","WDC","MSTR","SOFI",
            "MAR","DASH","UBER","PYPL","SNAP","RDDT","GME","AMC",
            "NKE","HD","LOW","TMO","DUK","NEE","UPS"]

def fetch_finnhub_news(ticker):
    """Fetch news for a ticker via Finnhub (free tier, 60 req/min)."""
    key = os.getenv("FINNHUB_API_KEY", "")
    if not key:
        return []
    url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from=2026-07-01&to=2026-07-12&token={key}"
    try:
        req = Request(url)
        with urlopen(req, timeout=10) as resp:
            articles = json.loads(resp.read())
            return [{
                "ticker": ticker,
                "title": a.get("headline", ""),
                "body": a.get("summary", ""),
                "source": a.get("source", "finnhub"),
                "published_at": datetime.fromtimestamp(a.get("datetime", 0), tz=timezone.utc),
                "sentiment": simple_sentiment(a.get("headline", "") + " " + a.get("summary", ""))
            } for a in articles[:10] if a.get("headline")]
    except Exception as e:
        log.warning("Finnhub fetch failed for %s: %s", ticker, e)
        return []

def simple_sentiment(text: str) -> float:
    """Simple keyword-based sentiment score (-1 to +1)."""
    positive = {"beat","surge","upgrade","buy","bullish","growth","profit",
                "record","positive","outperform","raise","strong","gain"}
    negative = {"miss","drop","downgrade","sell","bearish","loss","decline",
                "weak","cut","investigation","lawsuit","warning","fall"}
    words = text.lower().split()
    pos = sum(1 for w in words if w in positive)
    neg = sum(1 for w in words if w in negative)
    total = pos + neg
    return round((pos - neg) / max(total, 1), 2) if total > 0 else 0.0

def store_news(conn, articles):
    cur = conn.cursor()
    for a in articles:
        # Dedup by url_hash
        url_hash = str(hash(a.get("title", "")))
        cur.execute("""
            INSERT INTO market_data.news 
                (source, url_hash, ticker, title, body, sentiment, published_at, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (url_hash) DO NOTHING
        """, (a["source"], url_hash, a["ticker"], a["title"], a["body"],
              a["sentiment"], a["published_at"], datetime.now(timezone.utc)))
    conn.commit()
```

### 2. Add UNIQUE constraint on news table
```sql
ALTER TABLE market_data.news ADD CONSTRAINT news_url_hash_unique UNIQUE (url_hash);
```

### 3. Deploy cron
Schedule: Every 15 min during market hours, run `src/news_fetcher.py`

### 4. Update trader prompts
Add to each trader's AGENTS.md (ONE line):
```
📰 NEWS: Check market_data.news for tickers with strong sentiment before deciding.
```

## Verification
```sql
SELECT ticker, title, sentiment FROM market_data.news 
WHERE published_at > now() - interval '1 hour' AND sentiment > 0.5
ORDER BY published_at DESC LIMIT 10;
```