# SPEC: Self-Improving Paper Trading System

> **META-SPEC**: [ai-project-system v0.22](https://github.com/openclaw/openclaw/blob/main/docs/ai-project-system/META-SPEC.md)
> **Repo**: `Tesselation-Studios/paper-trading-rebuild`
> **Status:** Built + evolving — traders live, learning loop active, Postgres migration in progress
> **Goal:** Three AI traders (Kairos, Aldridge, Stonks) that measurably improve over time through two-speed learning, validated by rigorous out-of-sample testing, running on distributed hardware.
> **Success criterion:** 90-day rolling Calmar ratio > SPY buy-and-hold, with max drawdown < 15%.
> **Last updated:** 2026-07-06

> **META-SPEC compliance:**
> - **Purpose** — see below
> - **Architecture** — §1
> - **Components** — §§2–24
> - **Verification** — §§20, 25 (inline verification scenarios)

---

## Purpose

Build a self-improving paper trading system where three AI traders measurably get better over time — validated by out-of-sample testing — and provide a platform for experimenting with multi-speed learning, prompt evolution, and distributed compute.

---

## §1 — Architecture Overview

### 1.1 Two-Speed Learning

```
┌─────────────────────────────────────────────────────────────────┐
│                        MARKET DATA                              │
│                  (Alpaca API → Data Bus)                        │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────┐     ┌──────────────────────────────┐
│        SIGNAL ENGINE          │     │        LLM TRADER            │
│    (numerical, gradient-tuned)│────▶│    (OpenClaw agent)          │
│                              │     │                              │
│  • Momentum thresholds       │     │  Reads signals + context     │
│  • RSI bounds                │     │  Makes BUY/SELL/HOLD call    │
│  • Volatility filters        │     │  Writes decision journal     │
│  • Position sizing           │     │  CAN override signals        │
│  • Stop-loss distances       │     │  Override tracked for eval   │
│  • Regime weights            │     │                              │
│  • Confidence calibration    │     │  Prompt: git-versioned       │
│                              │     │  Evolves nightly via sweep   │
│        ↑                     │     │        ↑                     │
│        │ gradient descent     │     │        │ prompt sweep        │
│        │ (intraday, per tick) │     │        │ (nightly, parallel) │
│        │                      │     │        │                     │
└────────┼──────────────────────┘     └────────┼─────────────────────┘
         │                                      │
         └──────────────┬───────────────────────┘
                        │
                        ▼
              ┌──────────────────────┐
              │   OBJECTIVE FUNCTION │
              │   Calmar + PF + Exp  │
              │   Scores every trade │
              └──────────────────────┘
```

**Speed 1 — Intraday parameter tuning (gradient descent):**
- Runs every trading tick (3-4x per market day)
- Finite-difference gradient on signal engine parameters
- Small steps, bounded ranges, safe defaults
- Only adjusts numerical parameters; never touches prompts

**Speed 2 — Nightly prompt evolution (parallel sweep):**
- After market close, replay yesterday's data 100+ times
- Each replay: different prompt variant OR tool configuration
- Fan-out across Docker/Ollama/OpenClaw workers
- Rank by objective score on replay data
- Best variant > original? Auto-PR to agents repo
- If no variant beats original, journal "no improvement found"

**Speed 3 — Weekly code changes (serial coder):**
- Nightly pipeline detects structural improvements needed
- Coder agent writes actual code changes, commits as PR
- Hermes + Casper review and merge
- Runs on weekend or low-cost hours

### 1.2 Distributed Hardware

| Machine | Role | Resources |
|---------|------|-----------|
| **Hermes** (.131) | Orchestrator, spec-keeper, PR reviewer | Coordinates, fixes breakages |
| **OpenClaw** (.41) | Agent host — traders live here | Claude/Gemini via API |
| **Docker** (.179) | Backtest workers, replay harness | 20 parallel containers |
| **Mac** (.237) | Ollama inference for prompt sweeps | Free local inference |
| **TrueNAS** (.96) | Data lake | Historical data, trade DB, equity curves |

Data flow: TrueNAS stores everything. Every machine mounts it. Replay harness reads historical data from TrueNAS. Docker workers write results back. Hermes reads results from TrueNAS to generate PRs.

### 1.3 Architectural Invariants

These cannot be violated. All code and PRs are audited against them.

1. **Write-on-transaction**: Every state change writes to DB before returning. No in-memory-only state.
2. **Async confirmation**: No blocking on external APIs. Fire, record intent, confirm later.
3. **No dead code**: Every code path is exercised by at least one test. Coverage gate in CI.
4. **Config from files, not DB**: All trader configs live in git. The DB holds data, not settings.
5. **Trader-as-learner**: Agents do inference in their own ticks. No separate learning pipeline that feeds results back.
6. **Ground truth is P&L**: All improvement is measured by realized P&L outcomes. Heuristic scores are diagnostic only, never the optimization target.
7. **Out-of-sample validation**: No parameter change is accepted without validation on data the optimizer hasn't seen.
8. **Idempotent ticks**: Running the same tick twice produces the same result. No side effects that depend on timing.

---

## §2 — Objective Function

### 2.1 Metrics

| Metric | Formula | Weight | Why |
|--------|---------|--------|-----|
| **Calmar ratio** | annualized_return ÷ abs(max_drawdown) | 0.40 | Balances return and risk in one number. Rewards consistency. |
| **Sortino ratio** | (return - risk_free) ÷ downside_deviation | 0.15 | Like Sharpe but only penalizes downside volatility. Better for trading. |
| **Profit factor** | gross_profit ÷ gross_loss | 0.30 | "Do the wins outweigh the losses?" Pure edge detection. |
| **Expectancy** | total_pnl ÷ num_trades | 0.15 | "How much does each trade make on average?" Dollar-denominated. |

**Knockout condition**: If max_drawdown > 15%, objective_score = 0 regardless of other metrics. Trader is paused.

### 2.2 Composite Score

```
objective_score(trader, window_days=30) → float

Step 1: Compute rolling metrics over window_days
Step 2: Z-score expectancy against trader's own history (normalizes scale)
Step 3: Apply weights
Step 4: Apply knockout

Returns a single number. Higher = better. Zero = broken.
```

### 2.3 Benchmarks

Every night, compute the same metrics for **SPY buy-and-hold** over the identical period.

| Condition | Action |
|-----------|--------|
| Calmar < SPY Calmar | Signal: strategy underperforms benchmark. Optimizer proposes larger changes. |
| Calmar > SPY Calmar, improving | Signal: strategy has edge. Optimizer proposes fine-tuning. |
| Profit factor < 1.0 for 30 days | Signal: no edge. Trader journals "do I have a real strategy?" |

---

## §3 — Signal Engine (Gradient-Descent Tuned)

### 3.1 What Gets Tuned

The signal engine produces structured data that the LLM trader reads. All tunable parameters are continuous floats with bounded ranges.

```yaml
# Per-trader, version-controlled in agents repo
signal_params:
  momentum:
    threshold: 0.55          # float [0.3, 0.9]
    lookback_days: 20        # int [5, 60]
    decay_rate: 0.85         # float [0.5, 0.99]
    
  mean_reversion:
    rsi_oversold: 30.0       # float [15, 40]
    rsi_overbought: 70.0     # float [60, 85]
    bollinger_std: 2.0       # float [1.0, 3.0]
    
  volatility:
    regime_threshold: 0.25   # float [0.1, 0.5] — stddev daily returns
    reduction_multiplier: 0.7 # float [0.3, 1.0] — position size in high vol
    
  position_sizing:
    base_size_pct: 0.15      # float [0.05, 0.30] — % of equity per position
    conviction_multiplier: 1.5  # float [1.0, 3.0] — scale when conviction high
    max_positions: 5         # int [1, 10]
    
  risk:
    stop_loss_pct: 0.05      # float [0.02, 0.10]
    take_profit_pct: 0.15    # float [0.05, 0.30]
    trailing_stop_pct: 0.03  # float [0.01, 0.08]
    
  regime_weights:
    trending_up: 1.0         # float [0.2, 2.0]
    trending_down: 0.5       # float [0.0, 1.5]
    mean_reverting: 0.8      # float [0.2, 2.0]
    high_volatility: 0.4     # float [0.0, 1.0]
```

### 3.2 Finite-Difference Gradient Descent

Runs every trading tick:

```
for each param in signal_params:
    baseline_score = objective_score(trader, window_days=5)
    
    param += epsilon                    # perturb up
    replay_score_up = replay_last_n_ticks(trader, signal_params, n=10)
    
    param -= 2*epsilon                  # perturb down
    replay_score_down = replay_last_n_ticks(trader, signal_params, n=10)
    
    grad = (replay_score_up - replay_score_down) / (2 * epsilon)
    param += learning_rate * grad       # step toward improvement
    
    clip(param, bounds.min, bounds.max) # stay in safe range
```

Constraints:
- Learning rate: 0.01 per tick
- Max parameter change per tick: 5% of range
- Minimum 3 ticks between changes to the same parameter
- All changes logged with before/after scores

---

## §4 — LLM Trader (Prompt-Evolved)

### 4.1 Trader Decision Loop

Every tick, the trader agent receives:

1. **Signal report**: Structured output from signal engine (momentum scores, RSI, regime, position sizing recommendations)
2. **Portfolio state**: Positions, cash, P&L, current drawdown
3. **Market context**: SPY trend, VIX level, sector rotation signals
4. **Journal**: Last 10 entries of the trader's own reflection log
5. **Signal board**: Other traders' recent observations (see §7)

The agent must output:
```json
{
  "decision": "BUY | SELL | HOLD",
  "ticker": "AAPL",
  "conviction": 0.72,
  "rationale": "Momentum signal 0.81, RSI at 42 (not overbought), SPY trending up. Regime: TRENDING_UP.",
  "signal_override": false,
  "override_reason": null
}
```

### 4.2 Prompt Structure

```
You are {trader_name}, a paper trading agent.

Strategy: {strategy_description}

Current regime: {regime}, confidence: {regime_confidence}

Signal Report:
{signal_report}

Portfolio:
{portfolio_state}

Other traders' observations:
{signals_board}

Your recent journal:
{journal_entries}

Make a trading decision. You CAN override the signal engine if your analysis disagrees.
If you override, explain why.
```

### 4.3 Nightly Prompt Sweep

After market close (16:00 ET):

```
Step 1: Record yesterday's full market data (prices, signals, decisions, outcomes)
Step 2: Generate N prompt variants (N = 20–100):
    - Rephrased strategy descriptions
    - Different emphasis (risk-focused, opportunity-focused, balanced)
    - Modified tool descriptions
    - Different journal prompts
Step 3: Fan out to workers:
    - Docker (.179): 20 parallel replay workers
    - Mac (.237): Ollama inference for cheaper runs
    - OpenClaw (.73): Claude/Gemini for high-quality variants
Step 4: Each worker replays yesterday's data with its prompt variant
Step 5: Rank all variants + original by objective_score on replay
Step 6: If best variant > original + threshold (1% improvement):
    - Create PR to agents repo with the winning prompt
    - Include replay metrics: "Original Calmar: 1.2, Variant #47 Calmar: 1.35"
    - Auto-merge if improvement > 5% and all tests pass
Step 7: If no variant beats original:
    - Journal: "Nightly sweep: 100 variants tested, none improved on baseline."
```

---

## §5 — Regime Detection

### 5.1 Regime Classifier

Simple, explainable, not an ML black box:

| Regime | Detection | What it means |
|--------|-----------|---------------|
| **TRENDING_UP** | SPY > 20-day SMA AND ADX > 25 AND slope positive | Buy dips, ride momentum |
| **TRENDING_DOWN** | SPY < 20-day SMA AND ADX > 25 AND slope negative | Reduce positions, favor shorts |
| **MEAN_REVERTING** | ADX < 20 AND SPY within Bollinger Bands | Buy oversold, sell overbought |
| **HIGH_VOLATILITY** | VIX > 25 OR daily range > 2% | Reduce size, widen stops |

Regime is computed per tick and attached to every trade record. Performance is tracked per-regime so the optimizer knows "Kairos has Sharpe 1.8 in TRENDING_UP but -0.4 in MEAN_REVERTING."

### 5.2 Regime-Aware Parameters

Each trader can have different signal parameters per regime:

```yaml
regime_overrides:
  TRENDING_UP:
    momentum: {threshold: 0.55}
    position_sizing: {base_size_pct: 0.20}
  MEAN_REVERTING:
    momentum: {threshold: 0.35}
    position_sizing: {base_size_pct: 0.10}
  HIGH_VOLATILITY:
    position_sizing: {base_size_pct: 0.05}
    risk: {stop_loss_pct: 0.08}
```

The optimizer proposes regime-scoped changes. "In MEAN_REVERTING, reduce momentum threshold from 0.40 to 0.35" — not a global change.

---

## §6 — Validation & Overfitting Prevention

### 6.1 Walk-Forward Validation

Every parameter change must pass walk-forward validation:

```
Training window: [T-90 days, T-30 days]
Validation window: [T-30 days, T today]

Acceptance criteria:
  1. Validation Sharpe > 0 (positive on unseen data)
  2. Validation Sharpe > Baseline Sharpe (improved vs current params)
  3. Validation Sharpe > Training Sharpe × 0.7 (not grossly overfit)

If criteria fail: REJECT with reason.
If criteria pass: ACCEPT with confidence = validation_sharpe / training_sharpe.
```

### 6.2 Statistical Significance

Before accepting a parameter change, compute:

```
baseline_metrics = replay(trader, current_params, validation_window)
candidate_metrics = replay(trader, proposed_params, validation_window)

t_stat = (candidate_sharpe - baseline_sharpe) / pooled_std_error

if t_stat < 1.96:  # 95% confidence
    REJECT: "Improvement not statistically significant (p > 0.05)"
```

### 6.3 Minimum Evaluation Period

- Parameter changes are frozen for 5 trading days after acceptance
- No new proposals for the same parameter during the freeze
- After 5 days, the change is evaluated: did live performance match the validation prediction?
- If live performance degraded: auto-revert and flag

---

## §7 — Knowledge Sharing & Skill Evolution

### 7.0 Trader Skill Profiles

Each trader has a distinct starting toolkit. Tools are locked — a trader can't use what it doesn't have. But it can request access through the learning loop.

```
Trader skill profiles (initial):
  Kairos:   momentum_tools, RSI, MACD, volume_profile
  Aldridge: value_tools, P/E_screening, dividend_analysis, sector_rotation
  Stonks:   sentiment_tools, news_scraping, social_signals, fear_greed
```

**Tool request workflow:**
1. Trader journals: "I see a pattern I can't capture with my current tools. I need {tool}."
2. Casper reviews: is this tool appropriate? Does it overlap with another trader?
3. If approved: tool added to trader's skill manifest in agents repo
4. Tool usage tracked per trade: did using this tool improve outcomes?
5. If unused for 30 days: tool revoked

This creates natural divergence without artificial constraints. Traders converge on strategy but diverge on tool access based on what they prove works.

### 7.1 Signal Board

Traders publish observations to `/signals` endpoint each tick. Other traders read it.

```json
{
  "trader": "kairos",
  "type": "observation | lesson | alert",
  "ticker": "AAPL",
  "observation": "Momentum breakout above 0.8 confirmed by volume spike",
  "regime": "TRENDING_UP",
  "confidence": 0.72
}
```

### 7.2 Cross-Trader Learning

- **Divergence detection**: If all three traders have negative alpha simultaneously, probable regime shift → escalate.
- **Correlation check**: If two traders hold the same ticker, flag for herding risk.
- **Shared lessons**: Successful trade patterns are published and injected into other traders' context.

---

## §8 — Drawdown Management

### 8.1 Circuit Breaker

| Drawdown | Action |
|----------|--------|
| < 5% | Normal trading |
| 5–10% | Position sizes reduced by 50% |
| 10–15% | Trading paused. Learning loop only (observe, don't act). |
| > 15% | Emergency stop. Trader disabled. Human must re-enable. |

### 8.2 Cooling-Off

After 3 consecutive losing trades: skip the next 2 signals. Journal: "Cooling off after 3 consecutive losses. Reviewing what went wrong."

### 8.3 Recovery Mode

When trading is paused: the trader enters observation-only mode. It still processes ticks, makes mock decisions, and journals — but orders are not sent. It exits recovery when it can articulate a coherent reason for the drawdown and a plan to address it.

---

## §9 — Cold Start

### 9.1 Warm-Up Period

First 10 trading days per trader:

- Position sizes: 50% of normal
- Stop losses: 1.5x wider (more room to learn)
- No parameter tuning (insufficient data)
- No prompt evolution (insufficient data)
- All trades tagged `mode: warmup`

Minimum thresholds before full operation:
- 20 closed trades OR 30 trading days
- Positive expectancy in warm-up

### 9.2 Default Parameters

Each trader ships with conservative defaults backtested against 2 years of historical data (not optimized — just "reasonable"). These defaults are in the agents repo and serve as the fallback if optimization ever goes wrong.

---

## §10 — Performance Tracking

### 10.1 Daily Snapshots

Every night, write to `performance_history` table on TrueNAS:

```
trader_id, date, equity, cash, pnl, calmar_30d, calmar_90d,
sharpe_30d, profit_factor, win_rate, max_drawdown,
trades_closed, trades_won, avg_hold_hours, regime_distribution
```

### 10.2 Rolling Leaderboard

```
Leaderboard (30-day rolling):
1. Kairos:  Calmar 2.1, PF 1.8, MaxDD -4.2%  ▲ +0.3 from last week
2. Stonks:  Calmar 1.6, PF 1.4, MaxDD -7.1%  ▼ -0.1 from last week
3. Aldridge: Calmar 1.1, PF 1.2, MaxDD -9.8%  — no change
           SPY B&H: Calmar 0.9
```

### 10.3 Improvement Score

"Is the system getting better over time?"

```
improvement_score = (current_30d_calmar - calmar_30d_60_days_ago) / abs(calmar_30d_60_days_ago)

Positive → improving. Negative → getting worse. Zero → flat.
```

---

## §11 — A/B Testing (Shadow Mode)

### 11.1 Shadow Execution

When a learning loop PR is opened with proposed parameter changes:

1. **Shadow mode**: The new config runs alongside the live config for 5 trading days
2. Shadow trader makes decisions, logs them, but orders are NOT sent
3. After 5 days: compare shadow vs live P&L

### 11.2 Auto-Merge Criteria

```
if shadow_calmar > live_calmar + 0.2 AND shadow_max_drawdown <= live_max_drawdown:
    AUTO-MERGE with label "shadow-validated"
else:
    FLAG for human review with label "needs-review"
```

### 11.3 Rollback

Every accepted PR creates a rollback point. If live performance degrades > 10% within 10 days of merge, auto-revert and journal why.

---

## §12 — Change Velocity Control

### 12.1 Change Budget

Per trader, per month: maximum 5 parameter changes. The optimizer must choose which changes matter most.

### 12.2 Change Damping

Parameter changes are smoothed: `new_value = 0.7 × old_value + 0.3 × proposed_value`

Prevents whipsaw. A parameter that oscillates weekly isn't converging.

### 12.3 Revert Detection

If a parameter is changed and then reverted within 20 days, the change budget for that parameter is halved next month. The optimizer is learning to be wrong — it needs to slow down.

---

## §13 — Prompt Evolution Pipeline

### 13.1 Git-Based Prompt Versioning

Prompts live in the agents repo (`casper-bot-wodinga/paper-trading-agents`). Every version is a git commit. Branching is how we experiment; merging is how we promote.

```
agents/
  traders/
    kairos/prompt.md       ← the actual prompt file (git-tracked)
    aldridge/prompt.md
    stonks/prompt.md
```

**Branch structure:**

```
main                        ← production prompt. What traders use in live ticks.
│
├── kairos/v1.0.0           ← tagged stable releases (git tag, immutable)
├── kairos/v1.0.1
├── aldridge/v1.0.0
├── stonks/v1.0.0
│
sweep/YYYY-MM-DD/           ← nightly auto-generated branches
├── kairos/variant-001      ← "paraphrased strategy, Calmar +0.3 on replay"
├── kairos/variant-047      ← "risk emphasis = high, Sortino +0.5"
├── kairos/variant-089      ← "reordered instructions, Calmar -0.1"
└── ... (100 total per night)
│
experiment/{trader}/         ← manual or agent-proposed experiments
├── kairos/more-momentum
├── aldridge/value-only
└── stonks/sentiment-heavy
```

**Winner promotion flow:**

```
Night: sweep runs 100 variants on Docker workers
  → variant-047 scores +5.2% Calmar vs baseline on replay
  → auto-PR: sweep/2026-07-05/kairos/variant-047 → main
  → CI runs: replay validation, statistical significance check
  → Hermes + Casper review
  → Squash-merged to main
  → Tagged: kairos/v1.0.3
  → Trader picks up new prompt on next tick
```

**Pruning rules (runs nightly via cron):**

| Branch pattern | Delete after | Condition |
|---|---|---|
| `sweep/*` | 7 days | Always — sweep branches are disposable |
| `experiment/*` | 14 days | If no PR opened and no commits in 7 days |
| Tags | Never | Immutable release history |

```bash
# Nightly cleanup cron
git fetch --prune
git branch -r | grep 'sweep/' | while read b; do
  git push origin --delete "${b#origin/}"
done
```

Losing variants are the point — 99 of 100 branches die every night, but the 1 that survived made the trader better. The repo stays clean because dead branches auto-prune.

Automated analysis:
1. Group trades by prompt version active at the time
2. Compare performance: "v1.0.2 had Sharpe 0.3 higher than v1.0.1"
3. Analyze prompt instructions: which instructions correlate with wins vs losses?
4. Propose specific prompt edits: "Replace 'prioritize momentum' with 'prioritize momentum but check RSI first'"

### 13.3 Prompt Sweep Infrastructure

```python
# Runs nightly on Docker workers
def generate_variants(base_prompt: str, n: int = 100) -> list[PromptVariant]:
    """Generate N prompt variants using:
    - Paraphrasing (reword strategy description)
    - Emphasis shifts (risk-focused, opportunity-focused, balanced)
    - Instruction ordering (does order of rules matter?)
    - Example injection (add winning trade as example)
    """
```

---

## §14 — Counterfactual Analysis

### 14.1 Hold-Longer Analysis

For every closed winning trade: simulate holding +1, +3, +5, +10 more days. Report:
- "Held AAPL 2 days for +3.2%. Counterfactual: holding 5 days = +8.7% (+5.5% opportunity cost)"
- If opportunity cost > 50% of realized gain, flag for hold period review

### 14.2 Missed Entry Analysis

For tickers on the trader's watchlist that weren't traded: "Would buying have been profitable?"
- If yes: trader was too conservative. Propose reducing conviction threshold.
- If no: trader correctly avoided a bad trade. Reinforce current behavior.

### 14.3 Stop-Loss Optimization

Simulate tighter/wider stops on past trades. Find optimal stop distance that maximizes P&L while keeping drawdown below threshold.

---

## §15 — Corporate Actions & Data Integrity

### 15.1 Detection

Check Alpaca corporate actions API daily. Flag: splits, dividends, mergers, delistings, trading halts.

### 15.2 Adjustment

- All prices served by data bus are split-adjusted
- Dividend dates: adjust position cost basis, don't treat as P&L
- Delisted ticker: liquidate position, exclude from history
- Trading halt: skip ticker for that heartbeat

### 15.3 Learning Data Integrity

Trades affected by corporate actions are excluded from the learning loop's optimization dataset. Tagged in DB as `exclude_reason: corporate_action`.

---

## §16 — Distributed Compute Architecture

### 16.1 Data Lake (TrueNAS .96)

```
/mnt/truenas/trading/
  market_data/
    bars/           ← 1-min OHLCV per ticker, back to 2015
    quotes/         ← bid/ask
    fundamentals/   ← earnings, splits, dividends
  trader_data/
    trades.db       ← every trade, every trader
    equity_curves/  ← daily snapshots
    performance/    ← rolling metrics
  replay_cache/     ← preprocessed data for fast replay
```

### 16.2 Docker Workers (.179)

```yaml
# docker-compose.yml
services:
  replay-worker:
    image: paper-trading-replay
    deploy:
      replicas: 20
    volumes:
      - truenas_mount:/data
    command: python worker.py --mode replay --config $CONFIG_HASH
    resources:
      limits:
        cpus: '1'
        memory: 512M
```

20 parallel workers, each replays one variant. Results aggregated by the orchestrator.

### 16.3 Ollama Workers (Mac .237)

For prompt sweep variants that can use cheaper local inference instead of API calls. Reduces costs by ~60% for sweep runs.

### 16.4 Orchestration

Hermes (.131) controls the nightly pipeline:
1. Dispatch sweep to Docker + Ollama
2. Collect results from TrueNAS
3. Rank by objective score
4. Generate PR if improvement found
5. Update dashboard

---

## §17 — Nightly Pipeline

```
20:00 ET — Market data final. Begin pipeline.

Step 1: Regime Detection
  Compute regime for each tick of the day

Step 2: Trade Settlement
  Confirm all fills, reconcile P&L, update trade DB

Step 3: Performance Snapshots
  Compute daily metrics, write to performance_history

Step 4: Counterfactual Analysis
  Run hold-longer, missed-entry, stop-loss optimization

Step 5: Parameter Optimization (Gradient)
  Run finite-diff gradient on day's data
  Propose changes that pass validation

Step 6: Prompt Sweep (Parallel)
  Generate 100 variants, fan out to Docker/Ollama
  Replay day's data with each variant
  Rank by objective score

Step 7: Auto-PR
  If parameter change OR prompt variant improves objective:
    Create PR with before/after metrics, validation results
  If no improvement:
    Journal "no changes proposed"

Step 8: Knowledge Sharing
  Publish trader observations to shared signal board
  Cross-trader analysis: divergences, herding, regime shifts

Step 9: Dashboard Update
  Push updated leaderboard, equity curves, regime map

Step 10: Agent Journal
  Each trader writes nightly reflection:
    "What I learned today. What I'd do differently. What I'm watching tomorrow."
```

---

## §18 — Auto-Heal (Enhanced)

### 18.1 Monitors (All `no_agent: true` crons)

| Monitor | Frequency | What it checks |
|---------|-----------|----------------|
| `drawdown_monitor` | 15 min | Any trader > 10% DD → alert. > 15% → pause. |
| `stale_trader` | 30 min | Any trader with 0 trades in 2 days → nudge |
| `data_freshness` | 15 min | Alpaca data age < 5 min |
| `regime_drift` | 30 min | Regime changed? Alert traders. |
| `optimization_health` | Daily | Last N parameter changes: did they improve live P&L? If no, slow learning rate. |
| `cost_monitor` | 1 hour | API spend vs budget. Alert at 80%. |

### 18.2 Escalation Protocol

| Severity | Channel | Example |
|----------|---------|---------|
| P3 (info) | Log only | "Regime transitioned to MEAN_REVERTING" |
| P2 (warning) | Canvas | "Aldridge drawdown at 8%, reducing position sizes" |
| P1 (critical) | Canvas + Telegram DM | "Kairos drawdown > 15%, PAUSED" |
| P0 (emergency) | Canvas + Telegram + pause all trading | "Data bus down. No market data available." |

---

## §19 — Phased Implementation

### Phase 0: Foundation (✅ Done)
- Clean repo, green CI, SPEC-v3 (this document)
- Placeholder test, config skeletons

### Phase 1: Config Isolation + Signal Engine
- Isolated YAML configs per trader per regime
- Signal engine with bounded parameters
- Gradient descent framework (not yet live)

### Phase 2: Test Harness + Replay
- Walk-forward validation framework
- Replay harness with train/val split
- Objective function implementation

### Phase 3: Learning Loop
- Intraday gradient descent (Speed 1)
- Nightly prompt sweep (Speed 2)
- A/B shadow mode
- Change velocity governor

### Phase 4: Risk + Regime + Drawdown
- Regime classifier
- Regime-aware parameter sets
- Drawdown circuit breaker
- Auto-heal monitors

### Phase 5: Knowledge Sharing + Counterfactuals
- Signal board
- Cross-trader learning
- Counterfactual analysis

### Phase 6: Distributed Compute
- Docker workers for parallel sweeps
- Ollama integration for cheap inference
- TrueNAS data lake
- Dashboard

---

## §20 — Verification Scenarios

### OBJ-001: Objective Function Correctness
- Given: 10 trades with known P&L
- When: compute_objective_score() is called
- Then: Calmar, profit factor, and expectancy match manual calculation
- And: score > 0 for profitable sequence, 0 for drawdown > 15%

### GRAD-001: Parameter Bounds
- Given: momentum_threshold at 0.55 with bounds [0.3, 0.9]
- When: gradient proposes +0.5 step
- Then: parameter clips to 0.9, not above

### REGIME-001: Detection
- Given: SPY above 20-day SMA, ADX = 30, positive slope
- When: detect_regime() is called
- Then: returns TRENDING_UP with confidence > 0.7

### REGIME-002: Parameter Switching
- Given: regime changes from TRENDING_UP to HIGH_VOLATILITY
- When: trader loads config
- Then: position size reduces to HIGH_VOLATILITY override

### VAL-001: Walk-Forward Rejection
- Given: parameter change improves training Sharpe but degrades validation Sharpe
- When: validate() is called
- Then: returns REJECT with "overfit" reason

### VAL-002: Statistical Significance
- Given: proposed change improves Sharpe by 0.01 with std_err 0.05
- When: significance_test() is called
- Then: returns NOT_SIGNIFICANT (t_stat < 1.96)

### SWEEP-001: Prompt Sweep
- Given: 100 prompt variants, yesterday's market data
- When: run_nightly_sweep() completes
- Then: best variant's Calmar is reported
- And: if best > original + threshold, PR is created

### SHADOW-001: A/B Testing
- Given: PR with parameter change, shadow mode enabled
- When: 5 trading days elapse
- Then: shadow metrics are compared to live
- And: auto-merge triggers if shadow Calmar > live Calmar + 0.2

### DD-001: Drawdown Circuit Breaker
- Given: trader at 12% drawdown
- When: heartbeat ticks
- Then: position sizes reduced by 50%
- And: warning posted to Canvas
- When: drawdown reaches 15%
- Then: trader paused, Telegram alert sent

### COLD-001: Warm-Up Mode
- Given: new trader with 5 closed trades
- When: learning loop runs
- Then: returns "insufficient data" (threshold: 20 trades)
- And: trades are tagged mode:warmup

### SHARE-001: Cross-Trader Signal
- Given: Kairos posts observation to /signals
- When: Aldridge's next tick reads /signals
- Then: Kairos's observation appears in Aldridge's context

---

## §21 — Resolved Design Decisions

### 21.1 Gradient Step Size — Aggressive

Start at **0.03 per tick** (3× original proposal). Rationale: 5 months in and traders haven't delivered. A momentum threshold at 0.55 with ±0.03 per tick moves across the full range [0.3, 0.9] in ~20 ticks (about 1 week). Fast enough to matter, bounded enough to not destroy.

Still constrained by:
- Max parameter change per tick: 8% of range
- Minimum 3 ticks between changes to same parameter
- All changes logged with before/after scores

This is itself a meta-parameter: `gradient.learning_rate` in config, tunable over time.

### 21.2 Prompt Sweep Variant Count

**100 variants per night**, scaling to 200 if Mac Ollama throughput allows. Each replay uses yesterday's data (see §21.5). Cost ceiling: $0.25/night for API inference, $0 for Ollama workers.

### 21.3 Auto-Merge Thresholds — Tiered

All thresholds are tunable parameters in `learning_loop.auto_merge` config:

| Calmar Improvement | Action |
|---|---|
| > 10% | Auto-merge, no human needed |
| 5–10% | Auto-merge with notification to Telegram |
| 1–5% | PR created, labeled `needs-review` |
| < 1% | No PR (below noise floor) |

### 21.4 Trader Strategy — Converge Allowed, Tools Diverge

Traders are allowed to converge on similar strategies. Correlation is monitored but not prevented.

**Tool-based divergence**: Each trader starts with different tool access and can request new tools through the learning loop. A trader that's succeeding with a specific tool keeps it. A trader that wants to experiment requests access.

```
Trader skill profiles (initial):
  Kairos:   momentum tools, RSI, MACD
  Aldridge: value tools, P/E screening, dividend analysis
  Stonks:   sentiment tools, news scraping, social media signals

Learning loop addition:
  - Trader can propose: "I want access to {tool}"
  - Proposal includes: why it helps, expected impact
  - Casper reviews and approves/denies
  - Approved tools added to trader's skill manifest
  - Tool usage tracked per trade for effectiveness scoring
```

This means the skill system is part of the learning loop. A trader that never uses a tool loses it. A trader that proves a tool's value keeps it and shares the evidence.

### 21.5 Replay Harness — Always Default to Yesterday

The replay harness always replays the most recent complete trading day by default. No guessing date ranges.

```bash
# Default: replay yesterday
python replay.py --trader kairos

# Go back N days
python replay.py --trader kairos --days-back 5

# Specific date range (rare, for debugging)
python replay.py --trader kairos --from 2026-06-15 --to 2026-06-20
```

Data storage on TrueNAS is organized by date: `market_data/bars/YYYY/MM/DD/`. The replay harness reads the requested day(s), builds the market state, and feeds it to the trader. This makes the nightly sweep dead simple: always replay yesterday, always.

### 21.6 Timeframe

Daily bars for the signal engine. 5-min bars provided to the LLM trader as supplementary intraday context. Gradient descent operates on daily-level parameters only. Add intraday parameters (entry/exit timing within the day) as a future phase once daily-level system is stable.

---

## §22 — Reinforcement Learning (Q-Learning)

### 22.1 Why RL for Trading

The gradient descent approach (§3) treats parameter tuning as a continuous optimization problem. But trading is fundamentally a **sequential decision problem under uncertainty**: an agent observes a state (market data, portfolio), takes an action (BUY/SELL/HOLD), receives a reward (P&L), and transitions to a new state. This maps exactly to the RL framework.

Q-Learning learns a policy: given a market state, what action maximizes expected future reward? This is more principled than perturbing parameters and checking if P&L improved — it directly models the decision process.

### 22.2 How Q-Learning Replaces/Supplements Gradient Descent

```
State space (discretized):
  - Regime: TRENDING_UP | TRENDING_DOWN | MEAN_REVERTING | HIGH_VOL
  - Momentum signal: LOW | MEDIUM | HIGH
  - RSI zone: OVERSOLD | NEUTRAL | OVERBOUGHT
  - Portfolio exposure: 0-20% | 20-40% | 40-60% | 60-80% | 80-100%
  - Current drawdown: <5% | 5-10% | >10%

Action space:
  - BUY (with conviction: 0.25 | 0.50 | 0.75 | 1.0)
  - SELL
  - HOLD

Reward:
  - +1 for profitable closed trade
  - -1 for losing closed trade
  - -0.1 per tick for holding (encourages action)
  - -5 for drawdown > 10% (catastrophic penalty)
```

### 22.3 Q-Table Update

```
Q(s, a) ← Q(s, a) + α [r + γ · max_a' Q(s', a') - Q(s, a)]

α = learning rate (0.1, decays over time)
γ = discount factor (0.95 — value future rewards highly)
```

The Q-table is persisted to disk after every update. On startup, the trader loads its Q-table. Over weeks of trading, the table converges to an optimal policy.

### 22.4 Dual-Learner Architecture

The system runs **both** gradient descent (§3) and Q-Learning (§22) simultaneously:

```
Tick processing:
  1. Signal engine produces structured signals (gradient-tuned params)
  2. Q-Learning agent proposes action based on state
  3. LLM trader reviews both and makes final decision
  4. After trade closes: Q-table updated with reward
  5. Nightly: gradient runs on signal params, Q-table evaluation runs on replay

Conflict resolution:
  If gradient says BUY and Q-Learning says HOLD:
    → LLM trader adjudicates, journaling which signal it followed
    → Both learners get the outcome data
    → Over time, the better learner's decisions correlate with better outcomes
```

### 22.5 Q-Learning vs Gradient Descent

| Aspect | Gradient Descent (§3) | Q-Learning (§22) |
|--------|----------------------|-------------------|
| What it tunes | Continuous signal params | Discrete action policy |
| Update speed | Per tick (small adjustments) | Per closed trade (sparse but meaningful) |
| Cold start | Needs 20+ trades to be useful | Needs 100+ state visits per action |
| Overfitting risk | High (needs walk-forward validation) | Moderate (discretization provides regularization) |
| Interpretability | Easy (threshold = 0.55) | Hard (Q-values are opaque) |
| Computation | Cheap (perturb + replay) | Cheap (table lookup + update) |

Both run in parallel. The objective function (§2) evaluates which approach is driving better outcomes and weights decisions accordingly.

---

## §23 — Enhanced Risk Metrics

### 23.1 Sortino Ratio

The Sortino ratio is like Sharpe but only penalizes **downside** volatility. For trading, upside volatility is profit — we shouldn't penalize it.

```
Sortino = (R_p - R_f) / σ_d

R_p = portfolio return
R_f = risk-free rate (T-bill yield, ~4%)
σ_d = downside deviation (stddev of negative returns only)
```

A Sortino > 2.0 is excellent. Added to the objective function (§2.2) at weight 0.15.

### 23.2 Value at Risk (VaR)

VaR answers: "What's the worst-case loss with 95% confidence over the next day?"

```
VaR_95 = μ - 1.645 × σ

μ = mean daily return
σ = stddev daily return
1.645 = z-score for 95% confidence
```

Example: VaR_95 = -$150 means "we're 95% confident we won't lose more than $150 tomorrow."

Used in the risk system (§5) as an additional gate: if VaR_95 exceeds 5% of portfolio, reduce position sizes.

### 23.3 Risk Budget

Each trader gets a risk budget: max 5% VaR per position, max 15% VaR across portfolio. The drawdown circuit breaker (§8) already covers catastrophic scenarios; VaR covers the normal-case worst day.

---

## §24 — Unsupervised Regime Detection

### 24.1 K-Means Clustering for Market States

The rule-based regime detector (§5) uses simple thresholds (ADX > 25, VIX > 25). This works but is rigid. K-Means clustering discovers natural market states from data:

```
Features per day:
  - SPY daily return
  - SPY 20-day rolling volatility
  - VIX level
  - Advance-decline ratio (NYSE)
  - Sector correlation (avg pairwise corr of 11 sector ETFs)
  - Volume relative to 20-day avg

K = 4 clusters (matching our 4 regimes, but discovered from data)

Nightly: recluster last 90 days of data
Label clusters: which one performed best for momentum? For value?
```

### 24.2 PCA for Dimensionality Reduction

The raw market data has hundreds of correlated features. PCA reduces to 3-5 principal components:

```
PC1: "risk-on/risk-off" axis (explains ~40% variance)
PC2: "momentum/value" axis (explains ~20% variance)
PC3: "volatility regime" axis (explains ~15% variance)
```

The LLM trader's context includes: "PC1 is at +1.2σ (risk-on), PC2 at -0.8σ (value over momentum)."

### 24.3 Dual Regime Pipeline

Both the rule-based (§5) and unsupervised (§24) regime detectors run in parallel. The objective function evaluates which produces better regime-tagged performance. Over time, the system may shift weight toward the better detector or ensemble them.

---

## §25 — Verification Scenarios (Additions)

### RL-001: Q-Table Persistence
- Given: trader with 50 state visits and learned Q-values
- When: trader process restarts
- Then: Q-table loads from disk with all 50 entries intact

### RL-002: Q-Learning Convergence
- Given: static market environment (replay mode)
- When: 500 episodes of Q-Learning
- Then: Q-values stabilize (max delta < 0.01 for 10 episodes)

### SORTINO-001: Downside-Only Calculation
- Given: returns = [+2%, +3%, -1%, +4%, -5%]
- When: compute_sortino() is called
- Then: only [-1%, -5%] contribute to downside deviation
- And: [+2%, +3%, +4%] are excluded

### VAR-001: Confidence Calculation
- Given: daily returns with μ = 0.1%, σ = 1.5%
- When: compute_var_95() is called
- Then: VaR_95 ≈ -2.37% (μ - 1.645 × σ)

### KMEANS-001: Cluster Discovery
- Given: 90 days of market features
- When: K-Means with K=4 is run
- Then: 4 distinct clusters found
- And: each day assigned to exactly one cluster

---

## §26 — Agent File Architecture

How OpenClaw agents structure their knowledge. This is what the simulation engine simulates.

### 26.1 File Types and Purposes

| File | Where | Purpose | Changing Frequency |
|------|-------|---------|-------------------|
| `AGENTS.md` | `agent/AGENTS.md` | Operating manual: what the agent owns, its principles, escalation rules, how it operates | Occasionally (learns a better workflow) |
| `SOUL.md` | `agent/SOUL.md` | Personality and voice: how the agent thinks, speaks, and journals | Rarely (identity is stable) |
| `IDENTITY.md` | `agent/IDENTITY.md` | Metadata: name, emoji, creature type, vibe | Almost never |
| `TOOLS.md` | `agent/TOOLS.md` | Local notes: SSH hosts, API endpoints, device nicknames — environment-specific | When infrastructure changes |
| `MEMORY.md` | `agent/MEMORY.md` | Persistent learnings: what went well yesterday, news to watch, market observations | Daily (written during nightly reflection) |
| `SKILL.md` | `skills/<name>/SKILL.md` | Tool procedures: how to use Alpaca API, how to compute RSI, how to execute a trade. Reusable across agents. | When a better procedure is discovered |

### 26.2 Skill vs Agent File

**Skills are SHARED procedures.** `skill-alpaca-kairos` teaches Kairos how to use the Alpaca API. Multiple agents can load the same skill. Skills say "here's how you do X."

**Agent files are PERSONAL context.** AGENTS.md says "you are Kairos, you trade momentum." SOUL.md says "you're confident, aggressive, journal in first person." They differ per agent.

### 26.3 Prompt Assembly Order

When an OpenClaw agent receives a tick, its prompt is assembled as:

```
[IDENTITY.md]        → "I am Kairos. I trade momentum."
[AGENTS.md]          → "My job: read signals, decide BUY/SELL/HOLD, journal."
[SOUL.md]            → "I'm confident. I stick to what's proven."
[SKILL.md files]     → "Here's how to use Alpaca. Here's how to compute signal strength."
[TOOLS.md]           → "Alpaca endpoint: https://paper-api.alpaca.markets. My key is in ENV."
[MEMORY.md]          → "Yesterday AAPL broke out. Watching for follow-through."
[JOURNAL entries]    → Last 10 tick decisions and rationales
[TICK DATA]          → Current price, RSI, momentum, regime, portfolio state
```

### 26.4 Prompt Size Constraint

OpenClaw limits prompt size. Smaller is always better. The simulation must respect this:

- **Target:** Under 3,000 tokens for the full assembled prompt
- **Strategy:** Push technical detail into skills (loaded on demand, not always in context). Keep AGENTS.md and SOUL.md tight.
- **Journal:** Cap at last 10 entries. Trim rationales to one sentence.
- **Skills:** Reference by name with 1-line summary, not full procedure text.

### 26.5 What Gets Tweaked Overnight

| File | Tweaked? | How |
|------|----------|-----|
| AGENTS.md | Yes | Rule changes: "buy when momentum > 0.6" → "buy when momentum > 0.5 AND RSI < 70" |
| SOUL.md | Rarely | Shift emphasis: "be aggressive" → "be patient" |
| TOOLS.md | No | Updated only when infrastructure changes |
| MEMORY.md | Read-only | Read during simulation, not written back |
| SKILL.md | Yes | Procedure improvements: "use limit orders not market orders" |
| IDENTITY.md | No | Never changes |

---

## §27 — Simulation & Learning Engine

The overnight training system. Runs hundreds of prompt × parameter × regime scenarios on
a growing window of historical data. The system proposes its own hypotheses, tests them, and
promotes what works — the trader agents learn without daily human tweaking.

### 27.1 Scale

**Hundreds of scenarios every night.** Not 20 conservative variants. The full matrix:

```
3 traders
  × 5-10 prompt variants (AGENTS.md tweaks)
  × 3-5 param configurations (signal engine thresholds)
  × 4 regime contexts (TRENDING_UP, TRENDING_DOWN, MEAN_REVERTING, HIGH_VOL)
  × growing data window (5 days fast, 30 days deep, 90 days weekend)
```

| Pipeline | Scenarios | Data Window | Model | Cost Est. |
|----------|-----------|-------------|-------|-----------|
| **Fast screen** (nightly) | ~150-300 | 5 days | v4-flash | ~$0.10 |
| **Deep validation** (nightly, top candidates) | ~15-25 | 30 days | v4-flash | ~$0.08 |
| **Weekend sweep** (top performers) | ~5-10 | 90 days | v4-pro + flash | ~$0.15 |
| **Total weekly** | ~1,200 scenarios | | | ~$1.50 |

### 27.2 What Gets Tested

Each scenario varies:

**Prompt axis** (changes to AGENTS.md and SKILL.md):
- Decision rules: "buy when momentum > 0.6" → different thresholds, added conditions
- Tool usage: "use limit orders" vs "use market orders", "check sector first" vs "check volume first"
- Risk posture: "aggressive" vs "defensive" framing in SOUL.md
- Journal style: detailed vs terse rationales (affects context window utilization)
- Skill references: which skills are loaded in which order

**Parameter axis** (signal engine numbers):
- `momentum_threshold`, `rsi_oversold`, `base_size_pct`, `stop_loss_pct`
- `conviction_multiplier`, `max_positions`, regime weights
- Bounded, continuous, safe ranges only

**Regime axis** (market context filter):
- Run scenarios tagged by regime to discover: "does this prompt+param combo work in ALL regimes or only TRENDING_UP?"
- If a combo excels in one regime but tanks in another → regime-scoped config

### 27.3 Autonomous Hypothesis Generation

The system doesn't just test human-proposed variants. It proposes its own:

```
Nightly analysis:
  1. Load last night's results (all scenarios, all scores)
  2. Group by: trader, regime, prompt variant, param config
  3. Find patterns:
     - "Kairos scores +0.15 better when momentum_threshold > 0.55 in TRENDING_UP"
     - "Aldridge's 'check sector ETF' prompt scores -0.08 vs baseline — hurts value strategy"
     - "Stonks Calmar drops when position_size > 0.15 in HIGH_VOL"
  4. Generate hypotheses:
     - "If momentum_threshold=0.65 AND 'check volume first', Kairos might do better"
     - "If we remove 'sector check' for Aldridge but add 'PE < industry avg'..."
  5. Queue hypotheses as new scenarios for next night
```

This means the active variant list grows organically. Winners persist, losers drop out,
new ideas generated from patterns. Human review optional but not required.

### 27.4 Growing Data Window

The simulation window expands as days pass:

```
Day 1:   replay yesterday (5 scenarios/day → 5 total ticks)
Day 7:   replay last 5 days (25 ticks)
Day 14:  replay last 10 days (50 ticks)
Day 30:  replay last 20 days (100 ticks)
Day 90:  replay last 60 days (300 ticks) — weekend deep sweep
```

Each night, the growing window means more statistical confidence. A variant that
scores well on 5 days might regress on 30. The system learns this and adapts.

### 27.5 Trader Strategy Optimization

Each trader's AGENTS.md encodes their strategy. The simulation optimizes within
that strategy, not against it:

| Trader | Strategy | What Gets Optimized |
|--------|----------|--------------------|
| **Kairos** (momentum) | Buy strength, ride trends | Momentum thresholds, trend confirmation rules, conviction scaling |
| **Aldridge** (value) | Buy undervalued, wait | Value signal weights, patience rules, multi-position sizing |
| **Stonks** (sentiment) | Follow narrative, act fast | Sentiment thresholds, entry/exit speed, meme filtering |

The system never says "Kairos should trade value." It says "Kairos's momentum
works best with these thresholds in this regime."

### 27.6 Tool Proficiency Learning

Traders have tools (skills). The simulation discovers which tools produce edge:

```
For each trader:
  For each skill they have access to:
    Run scenarios WITH the skill loaded vs WITHOUT
    Measure: does having this skill improve Calmar?

  Result:
    skill-alpaca-kairos:   +0.12 Calmar (essential)
    stock-analysis:         +0.08 Calmar (useful)
    sell-the-news:          -0.03 Calmar (maybe harmful — over-cautious on good news?)
    self-improvement:       +0.01 Calmar (marginal)

  → System proposes: drop sell-the-news, keep others
  → If sustained for 5+ nights: auto-remove skill from config
```

### 27.7 Journal as Learning Signal

The journal isn't just context for the next tick — it's training data:

```
After a simulation run, analyze the journal:
  - Which decisions had high conviction but lost? (overconfidence)
  - Which had low conviction but won? (underconfidence — missed sizing)
  - Do journal entries become more accurate over the day? (learning)
  - Does the agent contradict itself? ("buying AAPL" at 10am, "AAPL overvalued" at 2pm)

This analysis feeds back into AGENTS.md:
  "You tend to be overconfident early in the day. Consider smaller first positions."
```

### 27.8 Weekly Summary & Auto-Promotion

Every Monday morning, the system produces a summary of the past week's learning:

```
Week of July 5-11:

Kairos — 1,247 scenarios tested
  Best combo: momentum_threshold=0.62 + "check volume first" + limit orders
  Calmar improvement: +0.18 over baseline (sustained 5 nights)
  → AUTO-PROMOTED to main. New version: kairos/v1.2.0

Aldridge — 1,103 scenarios tested
  Best combo: PE_gate=15 + "wait for pullback" + 5 max positions
  Calmar improvement: +0.09 over baseline (sustained 3 nights)
  → PR opened, labeled needs-review

Stonks — 980 scenarios tested
  Best combo: sentiment_threshold=0.7 + "ignore meme stocks"
  Calmar improvement: +0.04 (2 nights, volatile)
  → Kept in active list, needs more validation
```

### 27.9 Implementation

CLI:
```bash
python3 -m src.simulator sweep --all              # nightly: all traders, fast screen
python3 -m src.simulator sweep --trader kairos    # single trader
python3 -m src.simulator deep --trader kairos     # top candidates, 30-day data
python3 -m src.simulator weekend                  # 90-day deep sweep, all traders
python3 -m src.simulator analyze --trader kairos  # generate hypotheses from results
python3 -m src.simulator promote --trader kairos  # auto-promote if thresholds met
```

Architecture:
```
src/simulator.py          ← main simulation loop + CLI
src/hypothesis.py         ← pattern analysis + variant generation
src/prompt_builder.py     ← assemble full prompt from agent files + journal + tick
src/llm_engine.py         ← OpenRouter API calls, response parsing
config/sweep.yaml         ← scenario matrix config, cost limits, schedule
```

---

## §28 — Database Migration System

### 28.1 Why

The trading schema evolves. Parameters get added, tables get columns, indices change.
Manual `schema.sql` files drift from what's actually running. We need versioned,
repeatable, automated migrations — like Liquibase but lightweight.

### 28.2 Design

Simple migration system: numbered SQL files, applied in order, tracked in a
`schema_migrations` table.

```
src/db/migrations/
  001_initial_schema.sql      ← what's running now
  002_add_news_sentiment.sql  ← future: add sentiment column to news
  003_add_indexes.sql         ← future: perf indexes
```

Each migration file has an `UP` section (apply) and optional `DOWN` section (rollback).
A `migrate` CLI command applies any unapplied migrations.

### 28.3 Implementation (backlog — Phase 2)

```
python3 -m src.db.migrate status    # show applied + pending
python3 -m src.db.migrate up        # apply pending
python3 -m src.db.migrate down 1    # rollback last migration
python3 -m src.db.migrate create    # scaffold new migration file
```

Migration tracking table:
```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ DEFAULT NOW(),
    checksum TEXT
);
```

### 28.4 Priority

Not urgent — current schema is stable for Phase 1. Add before Phase 2 (distributed
workers, multi-machine schema sync). Tracked in GitHub Issues as `backlog/db-migrations`.

---

## §29 — Signal Threshold Tuning & Sweep Optimization

**Status:** Active — implemented based on sweep testing results showing 0 trades
across all scenarios. The signal engine correctly identifies regimes but produces
composite signals too weak to trigger trading decisions.

### 29.1 Root Cause

The `momentum_threshold` at 0.55 acts as a divisor in the momentum score
computation (`scaled = score / threshold`). At 0.55, a 1% price move becomes
a momentum score of ~0.018, which when blended with RSI and regime components
produces composite signals peaking around ±0.22 — far below the conviction
thresholds that trigger BUY/SELL decisions.

### 29.2 Solution: Relaxed Threshold Presets

Instead of a single conservative default, the signal engine now supports
multiple preset configurations:

```yaml
# Presets in SignalParams
conservative:  # Original defaults — production, vetted against 2y data
  momentum_threshold: 0.55
  rsi_oversold: 30
  rsi_overbought: 70

relaxed:       # Sweep starting point — lower bar to get trades flowing
  momentum_threshold: 0.25    # [0.10, 0.35] sweep range
  rsi_oversold: 35            # [25, 45]
  rsi_overbought: 65          # [55, 75]
  vol_regime_threshold: 0.20  # [0.10, 0.40]
  base_size_pct: 0.12         # smaller positions = more safety

aggressive:    # Max sensitivity — for finding the edge
  momentum_threshold: 0.15
  rsi_oversold: 40
  rsi_overbought: 60
  vol_reduction_multiplier: 0.5
```

### 29.3 Pre-Warm Mechanism

Each `run_scenario` creates a fresh `SignalEngine()`, which needs 20+ ticks
before indicators (momentum, RSI, volatility) produce meaningful values.
The first 20 ticks of every scenario are effectively dead air.

**Fix:** Before the scoring loop, feed 30 initial ticks to the signal engine
to establish a baseline price history. Only start counting trades from tick 31.

```
for tick in market_data[:30]:   # pre-warm (silent)
    signal_engine.process(tick)

for tick in market_data[30:]:   # scoring loop
    ...
```

### 29.4 Three-Phase Night Pipeline

Replace the current two-phase (backfill → sweep loop) with three phases:

| Phase | What | Duration |
|-------|------|----------|
| **1. Backfill** | yfinance → Postgres, all tickers, 30 days | ~5 min |
| **2. Sweep (relaxed)** | All traders × relaxed thresholds × variants | ~60 min |
| **3. Auto-relax & re-sweep** | Detect 0-trade runs, lower thresholds, re-run | ~120 min |

**Phase 3 auto-relax logic:**

```
for each trader:
    if max_trades_across_all_scenarios == 0:
        for param in [momentum_threshold, rsi_oversold, rsi_overbought]:
            relax by 20% of range (toward more permissive)
        re-run sweep with relaxed params
    elif max_trades < 3:
        relax by 10% of range
        re-run sweep
    else:
        record results, move on
```

Max 3 relaxation iterations per night per trader to avoid runaway.

### 29.5 Learning Loop Feedback Target

`objective_score` penalizes no-trade runs at -1.5. After each sweep iteration,
the hypothesis engine should:

1. **Lower thresholds** on traders scoring -1.5 (no trades) — the signal bar is too high
2. **Raise thresholds** on traders with >20 trades and negative PnL — overtrading
3. **Fine-tune** traders with 3-20 trades and positive PnL — sweet spot

This creates a self-correcting feedback loop: thresholds automatically converge
to the level where trades actually happen, then optimize for quality.

### 29.6 Implementation

All of the above are implemented in:
- `src/signals.py` — `SignalParams.relaxed_sweep()` and `aggressive()` factory methods
- `src/simulator.py` — Pre-warm in `run_scenario`, auto-relax in `run_sweep`
- `scripts/night_pipeline_v2.py` — Three-phase pipeline with auto-relax loop
