# Paper Trading Rebuild — Agent Instructions

**Architecture**: Three LLM traders (Kairos/momentum, Aldridge/value, Stonks/aggressive) on $10K paper portfolios. Trading ticks use **pre-assembled prompts** — `tick_prompt.py` merges live data from the data bus (quotes, signals, portfolio, journal, regime) with the trader's prompt template into one context bundle. **No tool calls during trading ticks.** The agent reads pre-assembled context and outputs JSON.

Two-speed learning: gradient descent (intraday) + nightly prompt sweeps.

---

## ⚠️ File Size Limits

| File | Hard Limit | Soft Target | Notes |
|------|-----------|-------------|-------|
| AGENTS.md | 12,000 | **2,000 chars** | Injected every tick — most expensive |
| SOUL.md | 12,000 | 3,000 chars | Persona only |
| TOOLS.md | 12,000 | 1,000 chars | Tool reminders |
| HEARTBEAT.md | 12,000 | 1,000 chars | Checklist |
| SKILL.md | Any | Any | Loaded on demand |

OpenClaw truncates files over 12,000 chars. **Rules**: `wc -c` before commit. Never append — remove before adding. Move details to SKILL.md.

---

## Tick Prompt Architecture

```
Cron → scripts/tick_prompt.py --trader <name>
  → reads prompts/<name>.txt (locked during market hours)
  → hits data bus: quotes, portfolio, regime, signals, journal
  → assembles one prompt with all injected sections
  → agent receives pre-assembled context → outputs JSON
```

Context includes: Market Context (F&G, regime, VIX), Watchlist Quotes table, Portfolio (positions + P&L), Performance Brief, Other Traders' Signals, Recent Journal (last 5), Strategy Prompt. See `specs/trader-ticks.md`.

---

## Spec Pipeline

`META-SPEC → SPEC → CODE → VERIFY → OPERATE`. Spec is source of truth. Spec changes → PR only; code → direct push (bot-owned repo).

---

## Canvas

Canvas.wodinga.studio for dev work: builds, deploys, specs, CI. No trader updates or P&L snapshots.

---

## 7. Observability System

All agents and tests produce structured JSON logs. See `src/observability/` for the core library.

### Structured Logging
```python
from src.observability import setup_logging, get_logger

setup_logging(level="INFO", json_log="logs/trading.jsonl")
log = get_logger("my_agent")
log.info("hello", extra={"key": "value"})  # extra= becomes JSON "data" field
```

### Metrics
```python
from src.observability import metrics
metrics.increment("trades.executed", tags={"trader": "kairos"})
metrics.gauge("portfolio.value", 10587.50)
metrics.observe("latency.llm", 1234)
```

### Alerts
```python
from src.observability import alert
alert.p0("critical", {"trader": "kairos", "reason": "..."})  # → Telegram + Canvas
alert.p1("warning", {"latency_ms": 5000})
alert.info("system ready", {"modules": 3})
```

### Agent Entry Points
```bash
python3 agents/kairos.py --once     # one tick, exit
python3 agents/aldridge.py --once
python3 agents/stonks.py --once
```

### Test Logging (JUnit XML)
```bash
./scripts/run_tests_with_logging.sh                # full suite + Canvas push
./scripts/run_tests_with_logging.sh -m smoke        # smoke tests
./scripts/run_tests_with_logging.sh --coverage      # with HTML coverage
```

### Canvas Observability Board
```bash
python3 scripts/push_observability_board.py --once --board main
python3 scripts/push_observability_board.py --every 300  # loop every 5 min
```

### Cron (every 5 min during market hours)
```
*/5 * * * 1-5 cd ~/projects/paper-trading-rebuild && \
    python3 scripts/push_observability_board.py --every 300 --board trading \
    >> logs/push_observability_board.log 2>&1
```

---

## 8. Canvas Rules

Canvas (`canvas.wodinga.studio`) is for **dev work only**: builds, deploys, specs, CI results, architecture decisions. No trader updates, P&L snapshots, or live heartbeat logs. Use `canvas-push --board main` for milestones. The observability board uses `--board trading` for live health.

---

## 9. Quick Reference

```bash
# Run tests
python3 -m pytest tests/ -v

# Run tests with logging + Canvas push
./scripts/run_tests_with_logging.sh

# Historical replay
python3 src/replay.py --date 2026-07-01

# Signal engine
python3 src/signals.py

# Walk-forward validation
python3 src/validation.py

# Push observability board
python3 scripts/push_observability_board.py --once --board trading

# Create issue
gh issue create --repo Tesselation-Studios/paper-trading-rebuild --title "..." --label "bug"

# Clean stale branches
git fetch --prune && git branch --merged main | grep -v main | xargs git branch -d
```

---

## 10. Communication

- **Hermes ↔ Casper**: chat bridge (`~/projects/hermes-openclaw-bridge/`). Hermes is active in this repo — coordinate via bridge, don't assume.
- **GitHub Issues**: all bugs, features, tasks at `Tesselation-Studios/paper-trading-rebuild/issues`.
- **GitHub Projects**: [board](https://github.com/users/casper-bot-wodinga/projects/2) is the single source of truth for what's being worked on.

---

## 9. Communication

- **Hermes ↔ Casper**: chat bridge (`~/projects/hermes-openclaw-bridge/`). Hermes is active in this repo — coordinate via bridge, don't assume.
- **GitHub Issues**: all bugs, features, tasks at `Tesselation-Studios/paper-trading-rebuild/issues`.
- **GitHub Projects**: [board](https://github.com/users/casper-bot-wodinga/projects/2) is the single source of truth for what's being worked on.
## Communication

- GitHub Issues: bugs, features, tasks
- GitHub Projects: source of truth for active work
- Hermes ↔ Casper: chat bridge (`~/projects/hermes-openclaw-bridge/`)
