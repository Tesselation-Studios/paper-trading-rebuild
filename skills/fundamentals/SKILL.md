---
name: fundamentals
description: Fetch company fundamentals and insider trading data
---

# Fundamentals

Fetch financial metrics and insider trading data for value analysis.

## Endpoints

```bash
# Company fundamentals (P/E, EPS, debt, ROE, etc.)
curl -s "http://localhost:5000/fundamentals?symbol=JPM"

# Insider trading (SEC Form 4 filings)
curl -s "http://localhost:5000/insiders?symbol=JPM"
```

## When to Call

- Aldridge: every tick on watchlist candidates
- Kazas: when considering a new position beyond technicals
- Stonks: rarely — only for fundamental validation of emerging plays

## Investment Committee Checklist (Aldridge)

Before any BUY, check:
1. P/E vs sector peers
2. EPS growth trend
3. Debt ratio (< 0.5 preferred)
4. ROE (> 15% preferred)
5. Dividend sustainable? (payout ratio < 60%)

If 3+ check out → BUY at probe size. If 5/5 → full size.

## What NOT to Do

- Don't call for every ticker every tick — fundamentals change slowly
- Don't trade purely on fundamentals without technical confirmation
