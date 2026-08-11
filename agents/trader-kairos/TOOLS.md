## Data Bus (built-in OpenClaw tools)
- `data-bus__get_quotes` — OHLCV + RSI for watchlist symbols
- `data-bus__get_technical_scan` — multi-TF (15m/1h/4h/1d) scan with RSI/MACD/BB
- `data-bus__get_market_regime` — ML regime classifier (bullish/bearish/choppy/exhausted)
- `data-bus__get_macro` — fear & greed, rates, macro overlay
- `data-bus__get_sentiment` — FinBERT + bilingual sentiment
- `data-bus__get_flow` — unusual options flow (sweeps, dark pool, blocks)
- `data-bus__get_portfolio` — live positions, P&L, cash
- `data-bus__get_self_stats` — own win rates by signal/sector/regime
- `data-bus__get_risk` — portfolio risk scoring (concentration, VaR, correlation)
- `data-bus__get_insiders` — insider trading filings (SEC Form 4)

## Momentum-Specific Tools
- `data-bus__get_quotes` with symbols for RSI screening (entry > 55 in TRENDING, < 45 oversold in CHOPPY)
- `data-bus__get_technical_scan` for multi-timeframe confirmation
- Volume filter: 1.2x avg volume required before entry

## Alpaca Executor (via exec tool)
`python3 ~/projects/paper-trading-rebuild/scripts/executor.py --account kairos --action BUY|SELL --ticker SYM --qty N`

## Bankroll
- `read bankroll.md` — current ceiling at start of tick
- Win a closed trade → ceiling × 1.01
- Lose a closed trade → ceiling × 0.99

## Journal (append-only workspace file)
Append to `journal/YYYY-MM-DD.md` in workspace. Never edit.

## Skills Loaded
- momentum-signals: RSI, MACD, volume confirmation
- trade-execution: buy/sell with risk checks
- market-data: data bus conventions
- risk-management: stop-loss discipline
- trading-hours: market open/close check
