# Aldridge Heartbeat

Read `skills/skill-aldridge-strategy/SKILL.md` for full strategy rules.

**Core flow:**
0. Check inbox — `curl -s "http://localhost:8080/inbox?agent=aldridge"` — respond to any pending Hermes messages
1. Portfolio check — `python3 src/skill_portfolio.py --account aldridge`
2. Macro scan — briefing, macro data, interest rates
3. **Stock discovery** — scan undervalued sectors and beaten-down quality names. Check fundamentals (`GET /fundamentals`), insiders (`GET /insiders`), news (`GET /news` or `GET /news-cache`), and macro rotations. Propose at least 1 new value candidate. Log discovery to `strategy_notes/<DATE>_discovery.md`.
4. Thesis integrity — news, fundamentals, insiders for each position
5. Sync decisions to Postgres — `python3 scripts/sync_decisions_to_pg.py --apply`  # writes decisions + journal to trading.trader_decisions + trading.trader_journal
6. Journal a note on conviction and thesis status
7. Learning loop — `python3 -m src.learning_loop --agent trader-aldridge`. Read the report. If param tweaks were applied, adjust your strategy accordingly. Pay attention to the **binding constraint** — focus improvement there.
8. Update profile
9. `python3 src/heartbeat_timestamp.py aldridge`

Output HEARTBEAT_OK when done.
