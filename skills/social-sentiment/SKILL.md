---
name: social-sentiment
description: Social media sentiment, news analysis, and community signal discovery
---

# Social Sentiment

Community-driven signal discovery for Stonks Capital. Multi-source stacking with freshness decay.

## Endpoints

```bash
# FinBERT + bilingual sentiment
curl -s "http://localhost:5000/sentiment?symbol=HOOD"

# Cross-language divergence (EN vs ZH)
curl -s "http://localhost:5000/sentiment-divergence?symbol=HOOD"

# News headlines
curl -s "http://localhost:5000/news?symbol=HOOD"

# Options flow (sweeps, dark pool, blocks)
curl -s "http://localhost:5000/flow?symbol=HOOD"

# Other traders' signals
curl -s "http://localhost:5000/signals"
```

## Signal Stack (Stonks)

Stack signals for conviction. More signals = higher confidence.

| Source | Tool | Weight |
|--------|------|--------|
| Overnight sentiment delta | `src/overnight_sentiment.py` | High |
| News sentiment | `/sentiment` or `/news` | Medium |
| Price + volume confirmation | `/quotes` | Medium |
| Options flow | `/flow` | Low |
| Social buzz | `web_search` site:reddit.com | Low |

## Publishing Your Signal

After each decision, push to the signal bus:
```bash
curl -s -X POST http://localhost:5000/signals \
  -H 'Content-Type: application/json' \
  -d '{"agent":"trader-stonks","ticker":"HOOD","bias":"bullish","conviction":0.7,"regime":"MOMENTUM","note":"social buzz + options flow"}'
```

## Browser Deep-Dive (Stonks)

When community buzz is high but data bus returns sparse results, use `web_search` for:
- Recent StockTwits/Reddit threads
- Discord callouts
- Bluesky trending

## What NOT to Do

- Don't trade purely on social buzz without technical confirmation
- Don't use stale signals — social sentiment decays fast (< 1 hour)
