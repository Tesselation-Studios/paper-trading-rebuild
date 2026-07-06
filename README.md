# Paper Trading Rebuild

Clean-slate, spec-driven rebuild of the paper trading system — three LLM-powered traders competing with $10K each over 6 months. Built on OpenClaw multi-agent framework with Postgres, Docker, and a self-improving learning loop.

> **Repo:** `Tesselation-Studios/paper-trading-rebuild` — sole active repo as of July 2026
> **Spec:** [SPEC-v3.md](SPEC-v3.md) — design spec with architectural invariants
> **Board:** [GitHub Issues](https://github.com/Tesselation-Studios/paper-trading-rebuild/issues)
> **Dashboard:** `http://192.168.1.41:5002`

---

## The Competition

| Trader | Firm | Strategy | Edge |
|--------|------|----------|------|
| **Zara Chen** | Kairos Capital | Momentum / ML | HMM regime detection, volume-filtered entries |
| **Edmund Whitfield** | Aldridge & Partners | Value / Fundamentals | Buy-and-hold blue chips, earnings quality scoring |
| **Stan Hoolihan** | Stonks Capital | Meme / Sentiment | Reddit pulse, social consensus, hype detection |

Each trader is a persistent OpenClaw agent with personality, evolving strategy notes, and a daily journal. They trade independently, read each other's logs, and compete.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        OpenClaw (.41)                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────────┐  │
│  │  Kairos  │  │ Aldridge │  │  Stonks  │  │  Data Bus :5000│  │
│  │ Momentum │  │  Value   │  │  Meme    │  │  17 tickers    │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └───────┬────────┘  │
│       │             │             │                 │           │
│       └─────────────┼─────────────┘                 │           │
│                     │                               │           │
│               Dashboard :5002                       │           │
└─────────────────────┼───────────────────────────────┼───────────┘
                      │                               │
                      ▼                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                     Docker (.179)                                │
│  ┌──────────────────┐  ┌──────────────────────────────────────┐ │
│  │  Postgres :5433  │  │  Backtest Workers (nightly sweeps)   │ │
│  │  trading DB      │  │  Parallel replay harness             │ │
│  └──────────────────┘  └──────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
                      ▲
                      │ coordinates, monitors
┌─────────────────────┴───────────────────────────────────────────┐
│                     Hermes (.131)                                │
│  Orchestrator, spec-keeper, cron jobs, PR reviewer              │
└──────────────────────────────────────────────────────────────────┘
```

### Two-Speed Learning Loop

| Speed | What | When | How |
|-------|------|------|-----|
| **1 — Intraday** | Parameter tuning | Every trading tick | Finite-difference gradient on signal engine params |
| **2 — Nightly** | Prompt evolution | After market close | Walk-forward replay, two-phase validation gate |
| **3 — Weekly** | Code changes | Weekends | Auto-PR with review gate |

### DB Migration (in progress)

| Component | Current | Target |
|-----------|---------|--------|
| Live traders | SQLite | Postgres `trading.*` |
| Dashboard | SQLite | Postgres |
| Backtest pipeline | Postgres | Already migrated |

See [#30](https://github.com/Tesselation-Studios/paper-trading-rebuild/issues/30) for migration status.

---

## Project Structure

```
src/
  db/
    schema.sql             ← Postgres core schema (market_data + trading)
    live_schema.sql        ← Live trading tables (positions, orders, etc.)
    connection.py          ← asyncpg pool + sync compatibility shim
    queries.py             ← DB query helpers
  signals.py               ← Signal engine, threshold relaxation
  simulator.py             ← Walk-forward simulator, pre-warm, auto-relax
  validation.py            ← Walk-forward split, overfit detection
  sweep_validation.py      ← Two-phase signal to LLM validation gate
  prompt_sweep.py          ← Nightly prompt variant generator
  reflection.py            ← Per-tick reflection + counterfactual loop
  journal_analyzer.py      ← Heuristic detectors for learning loop
  synthesis.py             ← Nightly synthesis + auto-promotion
  fundamentals.py          ← Aldridge fundamentals pipeline
  aldridge_strategy.py     ← Buy-and-hold value strategy
  sync_trades.py           ← Alpaca position sync (Postgres-ready)
  risk/                    ← Risk management modules

prompts/
  kairos.txt               ← Kairos system prompt

scripts/
  migrate_sqlite_to_pg.py  ← SQLite to Postgres migration (idempotent)
  backfill_bars.py         ← Yahoo Finance to Postgres bar backfill

tests/
  test_two_phase_validation.py  ← 25 tests for two-phase validation gate

specs/
  nightly-optimization-pipeline.md  ← Walk-forward sweep design

infra/
  docker/                  ← Docker Compose for Postgres + workers

SPEC-v3.md                 ← Master design spec
```

---

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Run tests (unit only, no homelab deps)
pytest -m "not homelab"

# Apply schema to Postgres
psql -h 192.168.1.179 -p 5433 -U trader -d trading -f src/db/schema.sql

# Run a signal sweep (Phase 1 only, zero API cost)
python3 src/sweep_validation.py --trader kairos --dates 20 --phase1-only

# Test SQLite to Postgres migration (dry-run)
python3 scripts/migrate_sqlite_to_pg.py --dry-run
```

---

## Homelab Infrastructure

| Host | IP | Role |
|------|----|------|
| **OpenClaw** | `.41` | Agent host, data bus (:5000), dashboard (:5002) |
| **Docker** | `.179` | Postgres (:5433), backtest workers |
| **Hermes** | `.131` | Orchestrator, cron jobs, PR review |
| **TrueNAS** | `.96` | Data lake, Pi-hole DNS |
| **Mac** | `.237` | ML worker (FinBERT, HMM training) |

Source of truth: `~/.hermes/homelab.env`

---

## CI/CD

- **GitHub Actions**: Runs on push to `main` — unit tests (no homelab deps)
- **Self-hosted runner**: Planned on `.179` for integration tests
- **Deploy**: `git push` triggers Casper to pull on OpenClaw

---

## Monitoring

| What | Where | Schedule |
|------|-------|----------|
| **Kairos Journal** | Hermes cron | Odd hours (9:30, 11:30, 1:30, 3:30 ET) |
| **Trader Heartbeat** | Hermes cron | Every 2 min |
| **Overnight Learning** | Hermes cron | 3 AM ET |
| **Dashboard** | `http://192.168.1.41:5002` | Live |
