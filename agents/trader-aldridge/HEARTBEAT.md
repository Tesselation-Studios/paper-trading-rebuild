# Aldridge Heartbeat — Persistent Session

This is a **persistent session**, not a cron job. You run continuously during market hours (9:30 AM - 4:00 PM ET).

## Two-Cycle Architecture

### Cycle 1: Trading Tick (every 5 min)
1. Read tick context (pre-assembled data from tick_prompt.py)
2. Screen for value: oversold names, low P/E, price near support, insider buying
3. Apply Investment Committee questions before every decision
4. Decide BUY/SELL/HOLD
5. Output JSON decision block
6. Journal

### Cycle 2: Heartbeat (every 30 min)
After every 6th tick, run the heartbeat maintenance loop:

1. **Reflect** — Review last 6 journal entries. Thesis integrity check.
2. **Distill** — Update MEMORY.md with new insights. Prune stale entries.
3. **Prune** — Trim prompt.txt if it grew beyond 2,500 chars. Move verbose sections to skills/.
4. **Consider new positions** — Check fundamentals for new value opportunities.
5. **Portfolio check** — `python3 src/skill_portfolio.py --account aldridge`
6. **Stop-loss check** — `python3 src/skill_stop_check.py --account aldridge`
7. **Fundamentals scan** — Check for new value names, re-check existing positions
8. **Learning loop** — `python3 -m src.learning_loop --agent trader-aldridge`
9. **HEARTBEAT_OK** — Signal completion

## Between Cycles
- Sleep 60 seconds between ticks
- If no trades for 30 min, verify data bus connectivity
- If market closed, idle until next open

## Self-Improvement Rules
- If prompt.txt is stale (old dates, dead tickers): **edit it yourself**
- If MEMORY.md is bloated: **prune it**
- If a skill is missing or outdated: **create or update it**
- If you discover a new working pattern: **add it to MEMORY.md**

Output HEARTBEAT_OK after each heartbeat cycle.