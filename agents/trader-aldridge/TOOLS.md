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

## Value/Fundamental Tools
- `data-bus__get_sentiment` — narrative shifts (earnings misses, guidance cuts, management changes)
- `data-bus__get_quotes` for fundamentals overlay screening
- `data-bus__get_insiders` — insider conviction signals

## Alpaca Executor (via exec tool)
`python3 ~/projects/paper-trading-rebuild/scripts/executor.py --account aldridge --action BUY|SELL --ticker SYM --qty N`

## Bankroll
- `read bankroll.md` — current ceiling at start of tick
- Win a closed trade → ceiling × 1.01
- Lose a closed trade → ceiling × 0.99

## Journal (append-only workspace file)
Append to `journal/YYYY-MM-DD.md` in workspace. Never edit.

## Skills Loaded
- fundamentals: P/E, EPS, balance sheet analysis
- trade-execution: buy/sell with risk checks
- market-data: data bus conventions
- risk-management: stop-loss verification, daily loss limits
- trading-hours: market open/close check
