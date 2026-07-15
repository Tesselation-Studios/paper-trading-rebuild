# Stonks Heartbeat

Read `skills/skill-stonks-strategy/SKILL.md` for full strategy rules.

**Core flow:**
0. Check inbox — `curl -s "http://localhost:8080/inbox?agent=stonks"` — respond to any pending Hermes messages
1. Portfolio check — `python3 src/skill_portfolio.py --account stonks`
2. Social pulse — scan community chatter on positions and watchlist
3. **Stock discovery** — social/community scan:
   - Scan Reddit (`GET /social?source=reddit`), Stocktwits (`/social?source=stocktwits`),
     Bluesky (`/social?source=bluesky`), and news (`GET /news-cache`) for trending tickers
   - Check unusual options flow (`GET /flow`) for tickers with heavy whale activity
   - Cross-reference with `/momentum` endpoint for momentum signal confirmation
   - Filter by bankroll ceiling: price must fit within your ceiling for at least 2 shares
   - Propose at least 1 new ticker with community momentum
   - Log discovery to `strategy_notes/<DATE>_discovery.md`
4. Data bus — flow, fear & greed, earnings calendar
5. **Pre-trade gate** — before every BUY, run the entry gate:
   ```
   python3 src/stonks_entry_gate.py --agent stonks \
       --action BUY --ticker FUBO --quantity 3 --price 9.93 \
       --stop-loss 8.94 --confidence 0.78 --signals 4 \
       --rsi 54.2 --macd-bullish --volume-ratio 2.5 \
       --fear-greed 25 --catalyst 0.3
   ```
   If the gate returns FAIL, DO NOT submit the order. The gate is
   code-enforced — you cannot override it. Log the failure reason.
   If the gate returns PASS, proceed with the trade.
6. Journal a note on what you're watching and your read
7. Sync decisions to Postgres — `python3 scripts/sync_decisions_to_pg.py --apply`  # writes decisions + journal to trading.trader_decisions + trading.trader_journal
8. Update profile
9. Learning loop — `python3 -m src.learning_loop --agent trader-stonks`. Read the report. If param tweaks were applied, adjust your strategy accordingly. Pay attention to the **binding constraint** — focus improvement there.
10. `python3 src/stonks_entry_gate.py --agent stonks --action HOLD --ticker NONE --json` (updates bankroll with current portfolio value)
11. Tick flasher — `curl -s -X POST http://localhost:5002/api/tick/stonks -H 'Content-Type: application/json' -d '{}' > /dev/null`

Output HEARTBEAT_OK when done.
