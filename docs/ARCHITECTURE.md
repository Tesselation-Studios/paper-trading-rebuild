# Architecture

> The paper trading rebuild system — three AI traders, two-speed learning, one data bus.

## System Overview

Three AI traders (Kairos, Aldridge, Stonks) running $10K paper portfolios on Alpaca, competing in a tournament ending 12/31/2026. Each trader is an OpenClaw agent with a unique strategy, personality, and tick cadence. The system learns at two speeds: intraday parameter tuning (gradient descent on signal weights) and nightly prompt sweeps (virtual trader variants compete, winners promoted).

```
┌──────────────────────────────────────────────────────────────┐
│                     Nightly Learning Loop                    │
│  prompt sweep → walk-forward validation → winner promotion   │
└──────────────────────────┬───────────────────────────────────┘
                           │ improves prompts & params
┌──────────────────────────▼───────────────────────────────────┐
│                     Three AI Traders                          │
│  Kairos (momentum, 5min)  Aldridge (value, 30min)            │
│  Stonks (sentiment, 15min)                                   │
│  Each: AGENTS.md → signals → decision → journal → trade      │
└──────────────────────────┬───────────────────────────────────┘
                           │ reads quotes, sentiment, signals
┌──────────────────────────▼───────────────────────────────────┐
│              Data Bus (Flask, port 5000, .41)                 │
│  42 endpoints: quotes, sentiment, flow, macro, technical     │
│  scan, options, congress, insiders, signals, momentum        │
│  17 tickers, real-time via Alpaca API + scheduler cache       │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│         Postgres (Docker .179:5433, database: trading)        │
│  Core tables: positions, trades, snapshots, daily_pnl        │
│  Learning: param_history, tick_queue, promotion_log          │
│  Risk: risk_state, circuit_breaker_state                     │
│  Agents: agent_profile, agent_state, journal entries         │
└──────────────────────────────────────────────────────────────┘
```

## Hardware

| Machine | IP | Role |
|---------|----|------|
| **OpenClaw** | 192.168.1.41 | Agent host (traders live here), data bus (:5000), dashboard (:5002), webhook receiver (:18789) |
| **Docker** | 192.168.1.179 | Postgres (:5433), backtest workers (Docker Compose) |
| **Hermes** | 192.168.1.131 | Orchestrator, cron jobs, PR review, spec-keeper |
| **TrueNAS** | 192.168.1.96 | Data lake, Pi-hole DNS, historical archives |
| **Mac** | 192.168.1.237 | ML worker (FinBERT, HMM training) |

## Repository Layout

```
paper-trading-rebuild/
├── src/                    # Source code
│   ├── data_bus.py         # Flask data bus (42 endpoints, 7000+ lines)
│   ├── generate_tick.py    # Tick assembly, prompt construction
│   ├── tick_producer.py    # Tick dispatch to trader agents
│   ├── orchestrator.py     # Coordinator, heartbeat management
│   ├── trader.py           # Core trader logic
│   ├── signals.py          # Signal computation (RSI, momentum, etc.)
│   ├── regime_detector.py  # K-Means + rule-based regime detection
│   ├── grading.py          # Trade grading (ObjectiveScorer)
│   ├── scoring.py          # Leaderboard scoring (Calmar, Sortino, PF)
│   ├── learning_loop.py    # Intraday gradient descent
│   ├── nightly_replay.py   # Replay engine for prompt sweeps
│   ├── historical_sim.py   # Historical simulation harness
│   ├── virtual_runner.py   # Virtual trader lifecycle
│   ├── reflection.py       # Tactical → strategic reflection
│   ├── reflection_cron.py  # Cron-driven reflection job
│   ├── strategy_evolution.py  # Weekly structural improvements
│   ├── prompt_builder.py   # Prompt template assembly
│   ├── prompt_sweep.py     # Variant generation for sweeps
│   ├── prompt_self_edit.py # Agent self-edits its own prompt
│   ├── prompt_tiering.py   # Tier-based prompt management
│   ├── prompt_versioning.py # Prompt version tracking
│   ├── sweep_validation.py # Walk-forward validation
│   ├── validation.py       # Statistical validation
│   ├── validation_gate.py  # Decision quality gates
│   ├── format_validator.py # Prompt format validation
│   ├── bar_loader.py       # OHLCV bar loading from Alpaca
│   ├── data_fetcher.py     # Alpaca API data fetching
│   ├── fetch_queue.py      # Rate-limit-aware fetch queue
│   ├── fundamentals.py     # Company fundamentals
│   ├── news_collector.py   # News aggregation
│   ├── news_fetcher.py     # News source fetching
│   ├── config_loader.py    # YAML config loading
│   ├── circuit_breaker.py  # Drawdown circuit breakers
│   ├── drawdown_knockout.py # Drawdown knockout logic
│   ├── safety.py           # Safety checks and limits
│   ├── transaction_costs.py # Slippage + commission model
│   ├── meta_cog.py         # Meta-cognition (self-assessment)
│   ├── knowledge.py        # Knowledge base management
│   ├── hypothesis.py       # Hypothesis generation
│   ├── journal_analyzer.py # Journal entry analysis
│   ├── sync_alpaca_positions.py # Alpaca position sync
│   ├── sync_exits.py       # Exit tracking
│   ├── sync_exits_pg.py    # Postgres exit tracking
│   ├── sync_trades.py      # Trade synchronization
│   ├── leaderboard_api.py  # Leaderboard API
│   ├── canvas_dashboard.py # Canvas dashboard builder
│   ├── pg_dashboard.py     # Postgres-backed dashboard
│   ├── skill_alpaca.py     # Alpaca trading skills
│   ├── skill_cross_sectional_momentum.py # Momentum signal
│   ├── kairos_backtest.py  # Kairos-specific backtest
│   ├── aldridge_strategy.py # Aldridge strategy
│   ├── train_regime_detector.py # Regime model training
│   ├── d_state_watchdog.py # Trader state watchdog
│   ├── metrics.py          # System metrics
│   ├── llm_engine.py       # LLM inference engine
│   ├── param_history.py    # Parameter history tracking
│   ├── virtual_cull.py     # Virtual trader culling
│   ├── virtual_rotate.py   # Virtual trader rotation
│   ├── replay.py           # Replay module
│   ├── simulator.py        # Trading simulator
│   ├── synthesis.py        # Nightly synthesis
│   ├── db/                 # Database layer
│   │   ├── connection.py   # Connection pooling (asyncpg)
│   │   ├── dual_writer.py  # SQLite+PG dual write
│   │   ├── queries.py      # All SQL queries
│   │   ├── live_schema.sql # Production schema reference
│   │   └── schema.sql      # Full DDL
│   ├── risk/               # Risk management
│   │   ├── gates.py        # Entry/exit gates
│   │   ├── manager.py      # Risk manager
│   │   └── stop_loss.py    # Stop-loss logic
│   └── observability/      # Observability
│       ├── alert.py        # Alerting
│       ├── logger.py       # Structured logging
│       ├── metrics.py      # Metrics collection
│       └── telegram.py     # Telegram notifications
├── scripts/                # Operational scripts
│   ├── nightly_pipeline.py # Nightly optimization pipeline
│   ├── nightly_synthesis.py # Nightly synthesis
│   ├── tick_cron.py        # Trading tick cron
│   ├── tick_prep.py        # Tick preparation
│   ├── tick_prompt.py      # Prompt assembly
│   ├── pre_market_gate.py  # Pre-market validation
│   ├── validate_prompt_format.py # Format validation
│   ├── promote_virtual_to_live.py # Virtual promotion
│   ├── promote_sweep_winner.py # Sweep winner promotion
│   ├── promotion_check.py  # Promotion eligibility check
│   ├── gateway_healthcheck_watchdog.py # Gateway monitoring
│   ├── trader_watchdog.py  # Trader process watchdog
│   ├── backfill_bars.py    # Historical bar backfill
│   ├── backfill_bars_alpaca.py # Alpaca bar backfill
│   ├── backfill_market_data.py # Market data backfill
│   ├── backfill_stop_loss.py # Stop-loss backfill
│   ├── sync_bars_to_pg.py  # Bar sync to Postgres
│   ├── migrate_sqlite_to_pg.py # SQLite→PG migration
│   ├── quick_migrate_pg.py # Quick PG migration
│   ├── apply_migrations.py # Migration runner
│   ├── deploy_skills.py    # Skill deployment
│   ├── check_circuit_breakers.py # Circuit breaker check
│   ├── check_risk_prompt_consistency.py # Risk/prompt audit
│   ├── seed_agents.py      # Agent seeding
│   ├── train_kairos.py     # Kairos training
│   ├── verify_learning_loop.py # Learning loop verification
│   ├── push_observability_board.py # Observability push
│   ├── openrouter_models.py # OpenRouter model listing
│   ├── separate_synthesis_output.py # Synthesis output splitter
│   ├── format_test.py      # Format testing
│   └── entrypoint_simulator.py # Entry point simulation
├── agents/                 # Trader agent definitions
│   ├── kairos.py           # Zara Chen — Momentum/ML
│   ├── aldridge.py         # Edmund Whitfield — Value/Fundamentals
│   └── stonks.py           # Stan Hoolihan — Meme/Sentiment
├── config/                 # YAML configuration
│   ├── paper.yaml          # Paper trading config
│   ├── data_bus.yaml       # Data bus config (TTLs, symbols)
│   ├── traders.yaml        # Trader definitions
│   └── risk.yaml           # Risk parameters
├── migrations/             # Postgres migrations (011+)
├── tests/                  # ~620 tests
├── specs/                  # Detailed sub-specs (19 files)
├── state/                  # Runtime state (cron logs, caches)
├── docs/                   # Documentation (you are here)
├── SPEC.md                 # Master specification
├── README.md               # Project overview
└── AGENTS.md               # Agent context (for bots)
```

## Data Flow

### Trading Tick Flow (Intraday)

```
1. Cron triggers tick_cron.py every 5 min (market hours)
       │
2. tick_producer.py enqueues tick to trading.tick_queue
       │
3. generate_tick.py assembles prompt:
   ├── Read trader AGENTS.md (strategy, rules, personality)
   ├── Read market regime (data bus /ml-signal)
   ├── Read portfolio state (data bus /self/stats or PG)
   ├── Read latest quotes (data bus /quotes)
   ├── Read signals (data bus /signals)
   ├── Read recent journal entries (PG)
   └── Assemble into prompt template
       │
4. Trader agent (OpenClaw) receives tick:
   ├── Reads prompt context
   ├── May call tools: data-bus__get_quotes, etc.
   ├── Makes BUY/SELL/HOLD decision
   └── Writes decision journal + trade intent
       │
5. Post-tick processing:
   ├── sync_alpaca_positions.py → PG (every 5 min)
   ├── sync_decisions_to_pg.py → PG (every 5 min)
   ├── sync_journals_to_pg.py → PG (every 5 min)
   └── learning_loop.py → gradient descent on params
```

### Nightly Learning Loop

```
1. Market close (4:00 PM ET)
       │
2. End-of-day processing:
   ├── learning_loop.py --optimize (4:35 PM, EOD)
   ├── reflection_cron.py --all (4:45 PM)
   ├── nightly_pipeline.py --skip-llm (4:35 PM, signal sweeps)
   └── auto_promote_prompts.py --apply (4:45 PM)
       │
3. Nightly sweep (3:00 AM ET, or manual):
   ├── prompt_sweep.py generates N variants
   ├── historical_sim.py replays yesterday on each variant
   ├── grading.py scores each variant (Calmar, Sortino, PF)
   ├── sweep_validation.py validates on OOS data
   └── Winner promoted if p < 0.05 and better than baseline
       │
4. Weekly (Saturday 8 AM):
   └── strategy_evolution.py --all
       └── Structural improvements, code changes
```

### Data Bus → Agent Flow

```
Data Bus (.41:5000)                 Agent Tool (OpenClaw)
┌─────────────────────┐             ┌──────────────────────────┐
│ /quotes             │────────────▶│ data-bus__get_quotes     │
│ /sentiment          │────────────▶│ data-bus__get_sentiment  │
│ /technical-scan     │────────────▶│ data-bus__get_technical  │
│ /ml-signal          │────────────▶│ data-bus__get_regime     │
│ /macro              │────────────▶│ data-bus__get_macro      │
│ /flow               │────────────▶│ data-bus__get_flow       │
│ /insiders           │────────────▶│ data-bus__get_insiders   │
│ /self/stats         │────────────▶│ data-bus__get_self_stats │
│ /risk               │────────────▶│ data-bus__get_risk       │
│ /sentiment-divergence│───────────▶│ data-bus__get_divergence │
│ /portfolio (via PG) │────────────▶│ data-bus__get_portfolio  │
└─────────────────────┘             └──────────────────────────┘
```

## Key Components

### Data Bus (`src/data_bus.py`)

Flask application serving 42 REST endpoints. Cached scheduler model:
- **Market-hours schedulers**: Run every N seconds during trading (9:30-16:00 ET)
- **Always-on schedulers**: Run continuously (crypto, macro, fear_greed)
- **Off schedulers**: Manual-trigger only

TTL-based caching with configurable freshness windows. Sources: Alpaca API, Lonestar (options/insiders/earnings), FRED (macro), FinBERT (sentiment).

### Trader Agents

| Trader | Agent | Strategy | Tick | Stock Universe |
|--------|-------|----------|------|----------------|
| Zara Chen | Kairos Capital | Momentum/ML, volume-filtered entries, HMM regime | 5 min | 20 tickers, tech-heavy |
| Edmund Whitfield | Aldridge & Partners | Value/Fundamentals, buy-and-hold blue chips | 30 min | Large cap, dividend payers |
| Stan Hoolihan | Stonks Capital | Meme/Sentiment, social consensus, hype detection | 15 min | High-social-volume stocks |

Each trader is an OpenClaw agent with:
- `AGENTS.md` — strategy, rules, personality, decision framework
- `SOUL.md` — persona and tone
- `MEMORY.md` — learned preferences, journal entries
- `prompts/{trader}.txt` — tick prompt template with placeholders

### Postgres Database

**Host:** Docker (.179:5433)  
**Database:** `trading`  
**Schema:** `trading`  
**User:** `trader`

Core tables (see [DB_SCHEMA.md](DB_SCHEMA.md) for full schema):
- Trading: `positions`, `executed_trades`, `portfolio_snapshots`, `daily_pnl`, `orders`
- Learning: `param_history`, `tick_queue`, `orchestrator_log`, `promotion_log`, `trade_signals`
- Agents: `agent_profile`, `agent_state`, `agent_reflections`, `daily_reflections`
- Risk: `risk_state`, `circuit_breaker_state`
- Virtual: `virtual_traders`, `promotion_summary`, `tier_snapshots`
- Data: `sentiment`, `bars`, `news`

### Validation Gates

Changes must pass walk-forward validation before going live:
1. **Training window**: [T-90, T-30] days
2. **Validation window**: [T-30, today]
3. **Acceptance criteria**: Validation Sharpe > 0, > Baseline, > Training × 0.7, t-stat > 1.96
4. **Pre-market gate**: 9:15 AM ET — validates prompt format before trading starts

### Learning System

Three speeds of improvement:

| Speed | What | When | How |
|-------|------|------|-----|
| **Intraday** | Signal parameter tuning | Every tick | Finite-difference gradient descent on 20+ numerical params |
| **Nightly** | Prompt evolution | After close | 20-100 prompt variants replayed on yesterday's data |
| **Weekly** | Code changes | Saturday 8 AM | Structural improvements via strategy_evolution.py |

## Communication

### Agent-to-Agent (Webhooks)

| Endpoint | Host | Purpose |
|----------|------|---------|
| `/hooks/wake` | .41:18789 | Hermes → Casper (Bearer auth) |
| `/hooks/agent` | .41:18789 | Agent-to-agent dispatch |
| `POST /webhooks/main` | .131:8644 | Casper → Hermes (HMAC signed) |

### Cron Jobs (on .41)

20 cron entries manage: tick dispatch (every 5 min), position sync, decision sync, journal sync, learning loop (hourly + EOD), nightly pipeline, pre-market gate, reflection, strategy evolution (weekly).

See [RUNBOOK.md](RUNBOOK.md) for the full cron table and troubleshooting.

## Architectural Invariants

These cannot be violated. All code and PRs are audited against them.

1. **Write-on-transaction**: Every state change writes to DB before returning
2. **Async confirmation**: No blocking on external APIs
3. **No dead code**: Every path exercised by ≥1 test
4. **Config from files, not DB**: Trader configs live in git
5. **Trader-as-learner**: Agents do inference in their own ticks
6. **Ground truth is P&L**: All improvement measured by realized P&L
7. **Out-of-sample validation**: No param change without OOS validation
8. **Idempotent ticks**: Same tick twice → same result
9. **Bootstrap fast and small**: New strategies start loose, tighten via learning
10. **Risk gate mirrors prompts**: Risk config derives from trader prompts
11. **Cron is trigger, not instruction**: Cron nudges, strategy lives in AGENTS.md
12. **Decision quality gates warning-only during bootstrap**: First 30 trades or +5% equity
13. **Cron timeout > model inference × 3**: Minimum: P99 latency × 3

## Related Docs

- [API.md](API.md) — Data bus endpoint reference
- [DB_SCHEMA.md](DB_SCHEMA.md) — Database schema
- [RUNBOOK.md](RUNBOOK.md) — Operational runbook
- [SPEC.md](../SPEC.md) — Master specification
- [specs/](../specs/) — Detailed sub-specs (19 files)
