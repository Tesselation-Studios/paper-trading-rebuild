# Kairos Heartbeat

Read `skills/skill-kairos-strategy/SKILL.md` for full strategy rules.

**Core flow:**
0. Check inbox — `curl -s "http://localhost:8080/inbox?agent=kairos"` — respond to any pending Hermes messages
1. Portfolio check — `python3 src/skill_portfolio.py --account kairos`
2. Data bus pulse — rides, regime, sentiment, fear & greed
3. **Stock discovery** — check momentum rankings (`GET /momentum`), scan for sector rotation and unusual volume breakouts. Propose at least 1 new ticker for the watchlist. Log discovery to `strategy_notes/<DATE>_discovery.md`.
4. Time-based exit check — flag positions >5 days or >3 days stale
5. Scoreboard sync — `python3 src/sync_exits_pg.py --backfill kairos`  # writes to Postgres trading.trades
6. Log your read, journal a note, update profile
7. Learning loop — `python3 -m src.learning_loop --agent trader-kairos`. Read the report. If param tweaks were applied, adjust your strategy accordingly. Pay attention to the **binding constraint** — focus improvement there.
8. `python3 src/heartbeat_timestamp.py kairos`

Output HEARTBEAT_OK when done.
