# SPEC-v3: Self-Improving Paper Trading System

> **Status:** Design phase — not yet built. Supersedes SPEC-v2.
> **Goal:** Three AI traders (Kairos, Aldridge, Stonks) that measurably improve over time through two-speed learning, validated by rigorous out-of-sample testing, running on distributed hardware.
> **Success criterion:** 90-day rolling Calmar ratio > SPY buy-and-hold, with max drawdown < 15%.

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
| **OpenClaw** (.73) | Agent host — traders live here | Claude/Gemini via API |
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
| **Calmar ratio** | annualized_return ÷ abs(max_drawdown) | 0.50 | Balances return and risk in one number. Rewards consistency. |
| **Profit factor** | gross_profit ÷ gross_loss | 0.30 | "Do the wins outweigh the losses?" Pure edge detection. |
| **Expectancy** | total_pnl ÷ num_trades | 0.20 | "How much does each trade make on average?" Dollar-denominated. |

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

### 13.1 Prompt Versioning

```
agents/
  traders/
    kairos/
      prompt.md          ← current (has version header)
      prompt_history/
        v1.0.0.md        ← original
        v1.0.1.md        ← "more emphasis on risk"
        v1.0.2.md        ← current (improved by sweep)
```

### 13.2 Weekly Review (Sunday)

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
