# SPEC: Self-Improving Paper Trading System

> **META-SPEC**: [ai-project-system v0.22](https://github.com/openclaw/openclaw/blob/main/docs/ai-project-system/META-SPEC.md)
> **Repo**: `Tesselation-Studios/paper-trading-rebuild`
> **Status**: Built + evolving — 3 traders live, Postgres native, webhook comms active
> **Updated**: 2026-07-15
>
> This file is the **master index**. Each subsystem has its own spec in `specs/` with
> implementation details. Details here are architecture-wide — everything else lives downstream.

---

## Purpose

Three AI traders (Kairos, Aldridge, Stonks) running $10K paper portfolios, competing in a tournament ending 12/31/2026 to maximize portfolio value. Two-speed learning: intraday parameter tuning + nightly prompt sweeps. Virtual traders (param/prompt variants) shadow live traders to discover improvements. All validated against out-of-sample data.

---

## Architecture

### Two-Speed Learning

```
MARKET DATA (Alpaca → Data Bus)
        │
        ▼
┌──────────────────┐     ┌──────────────────┐
│   SIGNAL ENGINE   │     │   LLM TRADER      │
│ (gradient-tuned)  │────▶│ (OpenClaw agent)  │
│                   │     │                   │
│ • Momentum, RSI   │     │ Reads signals +   │
│ • Vol filters     │     │   context         │
│ • Position sizing │     │ Makes BUY/SELL/   │
│ • Regime weights  │     │   HOLD decision   │
│ • Conviction       │     │   Writes decision │
│   calibration     │     │   journal         │
│                   │     │ CAN override      │
│        ↑          │     │   signals         │
│        │ gradient │     │        ↑          │
│        │ descent  │     │        │ prompt   │
│        │(intraday)│     │        │ sweep    │
│                   │     │        │(nightly) │
└──────────────────┘     └──────────────────┘
        │                                      │
        └──────────────┬───────────────────────┘
                       ▼
             ┌──────────────────────┐
             │  OBJECTIVE FUNCTION  │
             │  Calmar + PF + Exp   │
             │  Scores every trade  │
             └──────────────────────┘
```

### Three Speeds of Improvement

| Speed | What | When | How |
|-------|------|------|-----|
| **1. Gradient descent** | Tune numeric signal params (momentum threshold, RSI, sizing) | Every tick | Finite-difference, small steps |
| **2. Prompt sweep** | Test N prompt variants on historical replay | Nightly | Virtual traders compete, winner promoted |
| **3. Code changes** | Structural improvements to signal engine, risk gate, etc. | Weekly or as needed | Bot-owned repo, direct push to main |

### Hardware

| Machine | IP | Role |
|---------|----|------|
| **Hermes** | .131 | Orchestrator, spec-keeper, PR reviewer |
| **OpenClaw** | .41 | Agent host — traders live here |
| **Docker** | .179 | Backtest workers, replay harness |

Data flows through Postgres on Docker (.179:5433) and the data bus (.41:5000). TrueNAS (.96) stores historical archives.

---

## Architectural Invariants

These cannot be violated. All code and PRs are audited against them.

1. **Write-on-transaction**: Every state change writes to DB before returning. No in-memory-only state.
2. **Async confirmation**: No blocking on external APIs. Fire, record intent, confirm later.
3. **No dead code**: Every code path exercised by at least one test. Coverage gate in CI.
4. **Config from files, not DB**: All trader configs live in git. DB holds data, not settings.
5. **Trader-as-learner**: Agents do inference in their own ticks. No separate learning pipeline feeding results back.
6. **Ground truth is P&L**: All improvement measured by realized P&L outcomes. Heuristic scores are diagnostic only.
7. **Out-of-sample validation**: No parameter change accepted without validation on unseen data.
8. **Idempotent ticks**: Running the same tick twice produces the same result.
9. **Bootstrap fast and small**: Every new strategy begins loose (cheap stocks $10-40, 1-2% equity, 0.30 confidence). Learning loop tightens — not the starting prompt.
10. **Risk gate mirrors prompts**: Risk gate config is a derivative of trader prompts, not independent policy. Changing one requires changing the other.
11. **Cron is trigger, not instruction**: Cron messages are nudges. They don't specify strategy, stock universe, or entry rules — those live in AGENTS.md.
12. **Decision quality gates are warning-only during bootstrap**: First 30 trades or +5% equity — thesis/signals_used/exit_condition are WARN only, not VETO.
13. **Cron timeout must exceed model inference time × 3**: Minimum: model's P99 latency × 3.

---

## Agent Communication

Native webhooks power bidirectional agent-to-agent communication:

| Endpoint | Host | Purpose |
|----------|------|---------|
| `/hooks/wake` | OpenClaw .41:18789 | Hermes messages Casper (Bearer: `hermes-hook-2026`) |
| `/hooks/agent` | OpenClaw .41:18789 | Agent-to-agent dispatch |
| `POST /webhooks/main` | Hermes .131:8644 | Casper messages Hermes (HMAC signed) |

**Chat bridge (fallback):** `localhost:8644` server when webhooks are unavailable.

---

## Sub-Spec Index

| Spec File | Covers | Status |
|-----------|--------|--------|
| [`specs/architecture.md`](specs/architecture.md) | Detailed hardware, data flow, agent comms | 🟢 Active |
| [`specs/objective-function.md`](specs/objective-function.md) | Calmar, Sortino, PF, composite score, benchmarks | 🟢 Active |
| [`specs/signal-engine.md`](specs/signal-engine.md) | Gradient descent tuning, XGBoost classifier, params | 🟢 Active |
| [`specs/trader-ticks.md`](specs/trader-ticks.md) | Tick architecture, heartbeat, prompt structure, cold start | 🟢 Active |
| [`specs/kmeans-regime.md`](specs/kmeans-regime.md) | K-Means regime detection, feature vectors, cluster analysis | 🟢 Active |
| [`specs/validation.md`](specs/validation.md) | Walk-forward validation, statistical significance | 🟢 Active |
| [`specs/virtual-traders.md`](specs/virtual-traders.md) | Virtual trader system — param/prompt variants | 🟢 Active |
| [`specs/virtual-trader-rotation.md`](specs/virtual-trader-rotation.md) | Weekly culling/promotion of virtual traders | 🟢 Active |
| [`specs/nightly-optimization-pipeline.md`](specs/nightly-optimization-pipeline.md) | Nightly sweep pipeline, worker orchestration | 🟢 Active |
| [`specs/drawdown-management.md`](specs/drawdown-management.md) | Circuit breaker, cooling-off, recovery mode | 🟢 Active |
| [`specs/cold-start.md`](specs/cold-start.md) | Warm-up period, default params, bootstrap phase | 🟢 Active |
| [`specs/learning-simulation.md`](specs/learning-simulation.md) | Simulation engine, hypothesis generation, tool learning | 🟢 Active |
| [`specs/agent-files.md`](specs/agent-files.md) | OpenClaw file types, size limits, prompt assembly | 🟢 Active |
| [`specs/operational-hygiene.md`](specs/operational-hygiene.md) | Prompt deployment, cron hygiene, monitoring, bootstrap gates | 🟢 Active |

**Future / aspirational** (not implemented, tracked for later):
- Regime-scoped parameter overrides
- Multi-timeframe evaluation (5-min, 1-hour, daily)
- Counterfactual analysis (hold-longer simulation)
- Cross-trader learning / signal board
- DB migration system (Phase 2)
- HMM regime model integration
- Genetic algorithm for variant generation

---

## Current Bootstrap State (Kairos, Jul 9)

| Parameter | Value | Notes |
|-----------|-------|-------|
| Confidence threshold | 0.15 | Low bar to get trades flowing |
| Position size | 5% of equity | Generates meaningful signal |
| Stop loss | 5% | Standard |
| Ticker universe | 20 | Broad enough for opportunity |
| Shorts allowed | Yes | Generates data both directions |
| Risk gate behavior | WARN (not VETO) | First 30 trades |
| PG reader | Direct PG reads | Fully migrated from SQLite |

---

## Quick Reference

```bash
# Run tests
python3 -m pytest tests/ -v

# Historical replay
python3 src/replay.py --date 2026-07-01

# Pre-market format validation (8 AM ET)
python3 scripts/validate_prompt_format.py

# Nightly sweep
python3 scripts/night_pipeline_v2.py

# Create issue
gh issue create --repo Tesselation-Studios/paper-trading-rebuild --title "..." --label "bug"
```

---

## Current State Assessment (2026-07-15)

> This section tracks drift between the SPEC's aspirational architecture and the live running system. Updated by Fusion Router review on 2026-07-10. Last updated 2026-07-15 (issue #148 resolution).

### 🔴 Critical Drift

| SPEC Claim | Live State | Impact |
|---|---|---|
| "LLM never touches a tool during a trading tick" — pre-assembled prompt | AGENTS.md lists 4+ tool calls per tick (`curl`, `python3 skill_*.py`, etc.) | 2-7 min per tick wasted on tool execution; P99 timeout risk |
| Drawdown >15% → knockout, score=0 | Aldridge at 75% max DD, still trading | Circuit breaker not implemented — knocked-out trader still positions |

| JSON schema: `decision`/`conviction`/`rationale` | Live uses `action`/`confidence`/`reasoning` | Incompatible parsers; downstream tools read wrong fields |

### ✅ Resolved (2026-07-15)

| SPEC Claim | Resolution |
|---|---|
| `prompts/{trader}.txt` is prompt source | **Fixed (#148)** — Canonical `prompts/{kairos,aldridge,stonks}.txt` templates now live in the repo with `{regime}`, `{signal_report}`, `{portfolio_state}`, `{journal_entries}` placeholders. Templates use `decision`/`conviction`/`rationale` schema (aligned with #147). `scripts/sync_prompts.sh` provides backward compat mirror to `trading-agent-prompts/` during migration. |

### 🟡 Significant Drift

| SPEC Claim | Live State | Impact |
|---|---|---|
| ~~XGBoost accuracy 78%~~ | ✅ Resolved 2026-07-16 — model never deployed (no `.pkl` file); 63% was a false attribution (Kairos prompt doesn't mention it) | Moved to Not Yet Deployed |
| K-Means regime with 10 features | Rule-based `TRENDING_UP/DOWN/HIGH_VOL/MEAN_REVERTING` | K-means not deployed; old classifier still running |
| Multi-date walk-forward sweep | Single-date sweep with synthetic data fallback | Prompt overfitting; synthetic data is noise |
| Pre-market format validation blocks open | No evidence this gate is active | Broken prompts could hit production |

### 🟢 Not Yet Deployed

| Spec Subsystem | Status | Priority |
|---|---|---|
| Virtual traders (shadow + rotation) | Not deployed — tables don't exist | P2 |
| K-Means regime detector (`regime_detector.py`) | Spec defined, not deployed | P3 |
| BarLoader + backfill pipeline | Parquet data severely lopsided (61K rows on one day, 2 on others) | P1 |
| XGBoost momentum classifier | Spec defines 78% accuracy in `specs/signal-engine.md`, no model file exists in repo | P2 |
| CostModel in replay | Not implemented | P2 |

### Live System Health

| Trader | P&L | Win Rate | Max DD | Status |
|--------|-----|----------|--------|--------|
| **Kairos** | -$65 to -$83 | 0-16.7% | 14.7% | 🟡 0.3% from knockout |
| **Aldridge** | — | 50% | **75%** | 🔴 Should be knocked out |
| **Stonks** | $0 | 0% | 13.2% | 🔴 Not trading at all |

### ⚠️ Active Risk: Prompt Bloat

Nightly synthesis is appended into AGENTS.md (~80 lines/night). Current files are ~10K chars — approaching OpenClaw's 12K hard limit. Mid-file instructions will silently be truncated. Synthesis MUST be moved to a separate file or DB.

### Action Items from Fusion Router

See `FR-1` through `FR-18` in the fusion router review at `~/.tasks/review-fusion-router-rebuild.md`. Prioritized as P0-P4. The `.tasks/` queue in `~/.tasks/ready/` drives execution via the orchestrator heartbeat.

**Next milestone:** Migrate tick architecture from tool-based to pre-assembled prompt (FR-2, FR-3). Unblocks FR-4 (schema unification) and all downstream work.