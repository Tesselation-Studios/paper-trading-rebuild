# Stonks Heartbeat — Persistent Session

This is a **persistent session**, not a cron job. You run continuously during market hours (9:30 AM - 4:00 PM ET).

## Two-Cycle Architecture

### Cycle 1: Trading Tick (every 5 min)
1. Read tick context (pre-assembled data from tick_prompt.py)
2. Decide BUY/SELL/HOLD per strategy rules
3. Output JSON decision block
4. Journal in 3-line format (see AGENTS.md)

### Cycle 2: Heartbeat (every 30 min)
After every 6th tick, run the heartbeat maintenance loop:

1. **Reflect** — Review last 6 journal entries. What worked? What didn't? Patterns?
2. **Distill** — Update MEMORY.md with new insights. Prune stale entries.
3. **Prune prompt** — If prompt.txt grew beyond 2,500 chars, trim it. Move verbose sections to skill files.
4. **Consider new positions** — Check watchlist for new stocks matching strategy. Run stock discovery.
5. **Check inbox** — `curl -s "http://localhost:8080/inbox?agent=stonks"`
6. **Portfolio check** — `python3 src/skill_portfolio.py --account stonks`
7. **Social pulse** — Scan community chatter on positions and watchlist
8. **Learning loop** — `python3 -m src.learning_loop --agent trader-stonks`
9. **HEARTBEAT_OK** — Signal completion

## Between Cycles
- Sleep 60 seconds between ticks
- If no trades for 30 min, check data bus connectivity
- If market closed, idle until next open

## Self-Improvement Rules
- If prompt.txt is stale (old dates, dead tickers): **edit it yourself**
- If MEMORY.md is bloated: **prune it**
- If a skill is missing or outdated: **create or update it**
- If you discover a new working pattern: **add it to MEMORY.md**

Output HEARTBEAT_OK after each heartbeat cycle.