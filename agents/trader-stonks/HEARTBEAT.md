# Stonks Heartbeat

Read `skills/skill-stonks-strategy/SKILL.md` for full strategy rules.

**Core flow:**
0. Check inbox — `curl -s "http://localhost:8080/inbox?agent=stonks"` — respond to any pending Hermes messages
1. Portfolio check — `python3 src/skill_portfolio.py --account stonks`
2. Social pulse — scan community chatter on positions and watchlist
3. **Stock discovery** — scan Reddit/Bluesky/Stocktwits for trending tickers. Check unusual options flow (`GET /flow`). Propose at least 1 new ticker with community momentum. Log discovery to `strategy_notes/<DATE>_discovery.md`.
4. Data bus — flow, fear & greed, earnings calendar
5. Pre-trade gate — `python3 src/stonks_entry_gate.py` enforces entry rules
6. Journal a note on what you're watching and your read
7. Update profile
8. Learning loop — `python3 -m src.learning_loop --agent trader-stonks`. Read the report. If param tweaks were applied, adjust your strategy accordingly. Pay attention to the **binding constraint** — focus improvement there.
9. `python3 src/heartbeat_timestamp.py stonks`

Output HEARTBEAT_OK when done.
