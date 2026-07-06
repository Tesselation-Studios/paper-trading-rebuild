# Paper Trading Rebuild

> Three AI traders. Six months. $10K each. One question: can LLMs systematically improve at trading?

**Repo:** `Tesselation-Studios/paper-trading-rebuild` — sole active repo
**Spec:** [SPEC.md](SPEC.md) — 26 sections, 20+ verification scenarios
**Board:** [GitHub Issues](https://github.com/Tesselation-Studios/paper-trading-rebuild/issues)
**Dashboard:** `http://192.168.1.41:5002`

---

## What This Is

A spec-driven rebuild of the paper trading system. Three AI traders — each with their own strategy, portfolio, personality, and OpenClaw agent — trade paper money on Alpaca and compete for the best risk-adjusted returns.

| Trader | Firm | Strategy | Edge | Tick Speed |
|--------|------|----------|------|------------|
| **Zara Chen** | Kairos Capital | Momentum / ML | HMM regime detection, volume-filtered entries | 5 min |
| **Edmund Whitfield** | Aldridge & Partners | Value / Fundamentals | Buy-and-hold blue chips, earnings quality scoring | 30 min |
| **Stan Hoolihan** | Stonks Capital | Meme / Sentiment | Reddit pulse, social consensus, hype detection | 15 min |

They're persistent agents — they journal every decision, read each other's logs, and the system tracks who's right and why. At night, while the markets sleep, the optimization loop runs: testing prompt variants, validating signal parameters, and promoting whatever actually works.

---

## Why Raf Built It

Not to make money. (Paper trading — nobody's retiring on this.)

To answer a harder question: **Can an LLM-powered system measurably improve at a complex sequential decision task through structured experimentation?**

The traders argue with each other. The system watches. Every decision is journaled. Every outcome is measured. Every night, the learning loop closes the feedback gap:

1. Replay yesterday's data with N prompt variants
2. Rank by objective score (Calmar, profit factor, Sortino)
3. If any variant beats the baseline, auto-PR the winning prompt
4. If nothing works, journal "no improvement found" — that's data too

It's an experiment in whether LLMs can do systematic, closed-loop improvement — not just inference, but *learning from outcomes* and getting better according to a measurable objective function.

---

## How It Works

```
              ┌──────────────────────────────────────┐
              │      Nightly Optimization Loop        │
              │  (walk-forward backtesting)           │
              │  prompt sweep → signal validation     │
              │  → LLM replay → winner promotion      │
              └──────────────┬───────────────────────┘
                             │ improves prompts & params
              ┌──────────────▼───────────────────────┐
              │         Three AI Traders               │
              │  Kairos  (momentum, 5-min ticks)       │
              │  Aldridge (value,    30-min ticks)     │
              │  Stonks   (sentiment,15-min ticks)     │
              └──────────────┬───────────────────────┘
                             │ trade decisions
              ┌──────────────▼───────────────────────┐
              │         Data Bus (port 5000)           │
              │  quotes · sentiment · flow · macro    │
              │  technical scan · market regime        │
              │  17 tickers, real-time via Alpaca API  │
              └──────────────┬───────────────────────┘
                             │
              ┌──────────────▼───────────────────────┐
              │    Trading Dashboard (port 5002)       │
              │  leaderboard · positions · journal     │
              │  activity feed · trader cards          │
              │  live P&L, rolling metrics              │
              └──────────────────────────────────────┘
```

### The Learning Loop

The system has three speeds of improvement, operating on different timescales:

| Speed | What | When | How |
|-------|------|------|-----|
| **1 — Intraday** | Signal engine parameter tuning | Every trading tick | Finite-difference gradient descent on 20+ numerical params (momentum thresholds, RSI bounds, stop-loss distances) |
| **2 — Nightly** | Prompt evolution | After market close | Generate 20–100 prompt variants → replay yesterday's data on each → rank by objective score → auto-PR the winner |
| **3 — Weekly** | Code changes | Weekends | Learning loop detects structural improvements needed → coder agent writes code → PR with review gate |

### Two-Phase Validation Gate

Every proposed change — whether a signal parameter tweak or a full prompt rewrite — must pass walk-forward validation before going live:

1. **Training window**: [T-90, T-30] days — optimizer proposes changes on this data
2. **Validation window**: [T-30, T today] — unseen data, no cheating
3. **Acceptance criteria**:
   - Validation Sharpe > 0 (positive on unseen data)
   - Validation Sharpe > Baseline Sharpe (actual improvement)
   - Validation Sharpe > Training Sharpe × 0.7 (not overfit)
   - t-stat > 1.96 for 95% statistical significance

### Objective Function

```
objective_score = 0.40 × Calmar + 0.15 × Sortino + 0.30 × Profit Factor + 0.15 × Expectancy
```

Knockout: max drawdown > 15% → score = 0, trader paused.

Every night, the same metrics are computed for SPY buy-and-hold. If a trader can't beat the benchmark, learning rate accelerates. If profit factor < 1.0 for 30 days, the trader journals "do I have a real strategy?"

---

## Key Numbers

| Metric | Value |
|--------|-------|
| **Tests** | ~620 collected, 556 passing (CI gate) |
| **Traders** | 3 (Kairos, Aldridge, Stonks) |
| **Tracking horizon** | 6 months (started Feb 2026) |
| **Database** | Postgres (migrating from SQLite) on Docker | 
| **Walk-forward window** | 90-day training, 30-day validation |
| **Prompt sweep variants** | 20–100 per trader per night |
| **Alpaca tickers** | 17 (major equities + SPY hedge) |
| **Backend workers** | Docker Compose, 20 parallel replay containers |
| **Hosts** | 5 (OpenClaw, Docker, Hermes, Mac, TrueNAS) |

---

## Current State — July 2026

**Live paper trading** on Alpaca. Real ticks. Real decisions. Real journals.

**P&L (since inception):**
- **Stonks** — +6% (Stan's social sentiment signals found a groove)
- **Aldridge** — +1% (Edmund plays it safe — low volatility, low returns)
- **Kairos** — −7% (Zara's momentum strategy is being optimized; drawdown monitor active)

**Pipeline maturity:**

| System | Status |
|--------|--------|
| Real-time data bus | ✅ Live, 17 tickers |
| Trader agents (OpenClaw) | ✅ All three trading |
| ALPACA trade execution | ✅ Paper: orders fill, P&L tracked |
| DB migration (SQLite → Postgres) | 🔄 In progress ([#30](https://github.com/Tesselation-Studios/paper-trading-rebuild/issues/30)) |
| Hourly journal review | ✅ Hermes cron, every trader |
| Nightly optimization | ✅ Full pipeline: sweep → validate → promote |
| Walk-forward validation | ✅ Two-phase gate, statistical significance |
| Signal gradient descent | ✅ Intraday parameter tuning |
| Reflection loop (tactical → strategic) | ✅ Hourly + EOD + overnight |
| Dashboard | ✅ Live at `http://192.168.1.41:5002` |

**What's running every day:**
- **9:30–16:00 ET** — Trading ticks. Traders read the data bus, consult signals, make decisions.
- **Every 2 hours** — Hermes checks: heartbeat alive? drawdown safe? journals written?
- **16:00 ET** — Market close. Trade settlement, P&L reconciliation.
- **16:00–17:00 ET** — Tactical → reflective journal synthesis.
- **3:00 AM ET** — Nightly optimization loop: prompt sweeps, parameter proposals, auto-PR.
- **Weekends** — Code changes, structural improvements, spec reviews.

---

## Quick Start

```bash
# Prerequisites: Python 3.11+, Postgres (or Docker), Alpaca API keys

# Clone
git clone https://github.com/Tesselation-Studios/paper-trading-rebuild.git
cd paper-trading-rebuild

# Install
pip install -r requirements.txt

# Run tests (unit only, no homelab deps)
pytest -m "not homelab"

# Apply schema to Postgres
psql -h localhost -p 5433 -U trader -d trading -f src/db/schema.sql

# Run a signal sweep (Phase 1 only, zero API cost)
python3 src/sweep_validation.py --trader kairos --dates 20 --phase1-only

# Test SQLite to Postgres migration (dry-run)
python3 scripts/migrate_sqlite_to_pg.py --dry-run
```

See **[SPEC.md](SPEC.md)** for the full canonical specification — 26 sections covering architecture, signal engine, LLM trader design, RL integration, regime detection, drawdown management, A/B shadow mode, distributed compute, and 20+ verification scenarios with explicit acceptance criteria.

> **Pro tip**: The project structure is in `src/` (signal engine, validation, prompt sweep, synthesis), `tests/` (~620 tests), `config/` (per-component YAML), `prompts/` (trader system prompts), and `infra/docker/` (Postgres + worker Docker Compose).

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
