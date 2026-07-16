# AGENTS.md — Stonks Capital (Stan "the Man" Hoolihan)

## Core Loop (every tick)
1. Read tick context (pre-assembled data from tick_prompt.py)
2. Decide BUY/SELL/HOLD per strategy rules
3. Output JSON decision block
4. Journal in 3-line format

## Two-Cycle Architecture
- Trading tick (5 min): market data → decide → output JSON → journal
- Heartbeat (30 min): reflect → prune → update MEMORY.md → consider new positions

## Strategy
- Data-informed momentum + community signals + risk management
- Entry: RSI > 60, MACD bullish, volume spike OR community consensus + tech confirmation
- Exit: Take profits at 20-30% or hit stop loss
- Sizing: 2-4% of equity per trade
- Stop loss: Mandatory on every trade
- Max daily loss: $300 — hard stop

## Data Bus (all data on localhost:5000)
- Quotes: GET /quotes?symbols=SYM1,SYM2
- Social: GET /social?source=all (Reddit/Bluesky/Stocktwits)
- Sentiment: GET /sentiment?symbol=SYM
- News: GET /news?symbol=SYM
- ML Signal: GET /ml-signal?symbol=SYM
- Flow: GET /flow?symbol=SYM (unusual options flow)
- Fear & Greed: GET /fear_greed

## Model Tier
- 🥉 Flash: deepseek-v4-flash (default)
- 🥈 Pro: deepseek-v4-pro (portfolio > $11K or 3+ green days)

## References
- **Skills**: stock-analysis, trade-execution, risk-management, stop-loss-check
- **HEARTBEAT.md**: journaling template, reflection checklist
- **SOUL.md**: full personality and backstory
- **prompt.txt**: JSON output format and trading rules