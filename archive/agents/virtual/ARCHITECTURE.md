# Virtual Competitor Architecture

## Overview

Virtual competitors are now **full OpenClaw agents** running on this VM (.41), replacing the old Docker-based approach on docker.klo (.179). This design was prompted by [Issue #90](https://github.com/Tesselation-Studios/paper-trading-rebuild/issues/90) and replaces issues #80, #83, and #84.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────┐
│                  VM .41 (openclaw)                   │
│                                                      │
│  ┌──────────────────────────────────────┐            │
│  │         ORCHESTRATOR                  │            │
│  │  src/orchestrator.py                  │            │
│  │                                       │            │
│  │  For each pending tick:               │            │
│  │  1. Dispatch to LIVE traders          │            │
│  │  2. Dispatch to VIRTUAL competitors   │            │
│  │     (via sessions_send or inbox)      │            │
│  └────────────────┬─────────────────────┘            │
│                   │                                  │
│         ┌─────────┼─────────────┐                    │
│         ▼         ▼              ▼                    │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐              │
│  │ Live     │ │ Live     │ │ Live     │              │
│  │ Kairos   │ │ Aldridge │ │ Stonks   │              │
│  │ (OpenClaw│ │ (OpenClaw│ │ (OpenClaw│              │
│  │  .41)    │ │  .41)    │ │  .41)    │              │
│  └──────────┘ └──────────┘ └──────────┘              │
│                                                      │
│  ┌──────────────────────────────┐                    │
│  │ 9 VIRTUAL COMPETITOR AGENTS  │                    │
│  │ (OpenClaw .41, 5-min tick)   │                    │
│  │                              │                    │
│  │ Kairos: agg / cons / contra  │                    │
│  │ Aldridge: agg / cons / contra│                    │
│  │ Stonks: agg / cons / contra  │                    │
│  └──────────────────────────────┘                    │
│                                                      │
│  ┌──────────────────────────────┐                    │
│  │ NIGHTLY REPLAY (Python)      │                    │
│  │ agents/virtual/scripts/       │                    │
│  │ Uses LLMEngine (direct API)   │                    │
│  │ Accelerated on historical    │                    │
│  │ bars from data bus            │                    │
│  └──────────────────────────────┘                    │
│                                                      │
│  ┌──────────────────────────────┐                    │
│  │ PROMOTION PIPELINE            │                    │
│  │ virtual_promote.py            │                    │
│  │ Swaps AGENTS.md/SOUL.md when  │                    │
│  │ variant beats live trader     │                    │
│  └──────────────────────────────┘                    │
└─────────────────────────────────────────────────────┘
```

## Daytime Operation (Market Hours, 5-min Cadence)

```
┌─────────┐     ┌──────────────┐     ┌───────────────┐
│ Data Bus│────→│ Orchestrator │────→│ Live Traders   │
│ (docker │     │ dispatches   │     │ (OpenClaw, .41)│
│  .25)   │     │ MARKET TICK  │     └───────────────┘
└─────────┘     │ to all       │     ┌───────────────┐
                │ agents       │────→│ Virtual Compet-│
                └──────────────┘     │ itors (9 Open- │
                                     │ Claw, .41)    │
                                     └───────────────┘
```

1. Orchestrator fetches market snapshot from data bus
2. Dispatches MARKET TICK to live traders (sessions_send)
3. Simultaneously dispatches MARKET TICK to virtual competitors
4. Each agent makes independent decision on next heartbeat
5. Decisions logged to Postgres (trade_source='live' or 'virtual')

## Night Operation (Post-Market, Accelerated)

```
┌──────────────┐     ┌─────────────────┐     ┌──────────────┐
│ Data Bus     │────→│ virtual_nightly │────→│ .replay_     │
│ (historical  │     │ _replay.py      │     │ results/     │
│  bars API)   │     │ (Python, .41)   │     │ (JSON scores)│
└──────────────┘     └────────┬────────┘     └──────────────┘
                              │
                              ▼
                     ┌─────────────────────┐
                     │ virtual_promote.py  │
                     │ reviews scores,     │
                     │ promotes winners    │
                     └─────────────────────┘
```

1. Run `virtual_nightly_replay.py` (can be cron'd)
2. For each variant: load AGENTS.md + config.yaml, run ReplayHarness on historical bars
3. Score: total return, win rate, Sharpe, max drawdown
4. Scores saved to `agents/virtual/.replay_results/`
5. Run `virtual_promote.py --review` to check eligibility

## Virtual Competitor Roster

### Kairos Variants (base: momentum trader Zara Chen)

| Variant | Conviction | Position Size | Stop | Max Pos | Key Difference |
|---------|-----------|--------------|------|---------|----------------|
| **Aggressive** | 0.25 | 3% | 5% | 5 | Lower bar for entry, bigger swings |
| **Conservative** | 0.75 | 1% | 3% | 2 | Strict 3/3 confirmations, skip CHOPPY |
| **Contrarian** | 0.35 | 2% | 4% | 4 | Mean-reversion RSI<40/R>70, volume spike |

### Aldridge Variants (base: value investor Edmund Whitfield)

| Variant | Conviction | Position Size | Max P/E | Max Pos | Key Difference |
|---------|-----------|--------------|---------|---------|----------------|
| **Aggressive** | 0.30 | 3% | 25 | 4 | Fear-contrarian value, wider moats |
| **Conservative** | 0.70 | 1.5% | 15 | 2 | Deep value only, max quality |
| **Contrarian** | 0.40 | 2% | 30 | 3 | GARP (P/E/G<1.0), growth at reasonable price |

### Stonks Variants (base: sentiment trader Stan Hoolihan)

| Variant | Conviction | Position Size | Social Vol | Max Pos | Key Difference |
|---------|-----------|--------------|-----------|---------|----------------|
| **Aggressive** | 0.20 | 4% | 2x | 6 | Sentiment maximalist, any trigger |
| **Conservative** | 0.60 | 1.5% | 3x | 3 | Social+technical confirmation required |
| **Contrarian** | 0.35 | 2% | 5x | 4 | Anti-crowd: short hype, buy panic |

## Files

| File | Purpose |
|------|---------|
| `agents/virtual/{base}/{type}/AGENTS.md` | Variant trading instructions |
| `agents/virtual/{base}/{type}/SOUL.md` | Variant persona |
| `agents/virtual/{base}/{type}/config.yaml` | Parameter overrides |
| `agents/virtual/{base}/{type}/skills/SKILL.md` | Full strategy rules |
| `agents/virtual/scripts/virtual_trader_orchestrator.py` | MARKET TICK dispatch |
| `agents/virtual/scripts/virtual_nightly_replay.py` | Accelerated replay |
| `agents/virtual/scripts/virtual_promote.py` | Promotion pipeline |
| `agents/virtual/scripts/HEARTBEAT.md.template` | Heartbeat config for virtual agents |
| `agents/virtual/scripts/openclaw_agent_registration.md` | How to register in openclaw.json |
| `agents/virtual/.replay_results/` | Nightly replay scores (auto-created) |
| `agents/virtual/.archived_promotions/` | Archived live trader files (auto-created) |
| `.tasks/paper-trading-rebuild-issue-90.md` | Task tracker |
| `src/orchestrator.py` | Updated with virtual competitor dispatch |

## OpenClaw Agent Registration

Virtual competitors must be registered in `~/.openclaw/openclaw.json` under `agents.list`. See `agents/virtual/scripts/openclaw_agent_registration.md` for the exact config blocks.

Each virtual gets:
- A workspace at `/home/openclaw/.openclaw/workspace-virtual-{base}-{type}/`
- 5-minute heartbeat during market hours
- Same model tier as live traders (deepseek-v4-flash)
- Inbox tool for receiving MARKET TICK messages

After registration:
```bash
openclaw gateway restart    # reload config
mkdir -p workspace/...      # create workspaces
cp agents/virtual/{base}/{type}/*.md workspace/.../   # copy variant files
cp HEARTBEAT.md.template workspace/.../HEARTBEAT.md   # copy heartbeat
```