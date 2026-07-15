# Bankroll — Stonks Capital Risk Ceiling

> **Active ceiling:** ${{ceiling}}
> **Portfolio value:** ${{portfolio_value}}
> **Risk multiplier:** 1.25× (Stonks runs aggressive)

## Formula

```
ceiling = max($5.00, portfolio_value × 0.01 × 1.25)
growth  = 1 + win_streak × 0.015
decay   = 1 - loss_streak × 0.01
```

## State

| Metric | Value |
|--------|-------|
| Ceiling | ${{ceiling}} |
| Deployed | ${{deployed}} |
| Remaining | ${{remaining}} |
| Wins/Losses | {{wins}}/{{losses}} |

## Positions

{{#positions}}
- {{ticker}}: ${{cost}} ({{pct}}% of ceiling)
{{/positions}}

## Session P&L

- Closed: ${{closed_pnl}}
- Running streak: {{streak}}

---
*Auto-generated. Synced from Alpaca + PG.*
