# Learning Loop Closure — Implementation Plan

> **For Hermes:** Build each task sequentially with TDD. Verify before moving on.

**Goal:** Make the paper trading system actually learn — reflection → journal analysis → prompt/param changes → verify improvement. Repurpose Aldridge as buy-and-hold value and Kairos as ML backtesting engineer.

**Architecture:** Three new modules: `src/reflection.py` (per-tick learning), `src/synthesis.py` (nightly summary + auto-promote), `src/fundamentals.py` (Aldridge data). Kairos gets a backtesting toolkit via `src/backtest_kit.py`. The closed loop is: trade → journal → reflect → analyze → synthesize → promote → repeat.

**Tech Stack:** Python 3.11, OpenRouter API, Postgres on docker.klo, yfinance + yahooquery for fundamentals, pytest for testing.

---

## Key Design Decisions

1. **Reflection happens per-tick, not per-day.** After each decision, ask "what did I learn? what would I do differently?" Store in journal. Feed back into next tick's prompt.
2. **Synthesis runs nightly.** Analyze all journals from the day, produce a summary, propose changes (params + prompt diffs), auto-promote if thresholds met.
3. **Aldridge uses fundamental filters, not signals.** P/E < industry avg, P/B < threshold, positive earnings growth, dividend > 0. Buy once, rebalance weekly.
4. **Kairos builds tools, doesn't just trade.** Access to full historical data, ability to spawn simulation runs, model registry, evaluation harness.
5. **Journal is the connective tissue.** Every reflection, every counterfactual, every synthesis reads from and writes to the journal.

---

## Task 1: Add fundamental data collector for Aldridge

**Objective:** Collect P/E, P/B, market cap, dividend yield, earnings growth for the watchlist. Store in `market_data.fundamentals` table. Aldridge uses this to filter buy candidates.

**Files:**
- Create: `src/fundamentals.py`
- Modify: `src/db/connection.py` (add `insert_fundamentals`)
- Create: `tests/test_fundamentals.py`

**Schema:**
```sql
CREATE TABLE IF NOT EXISTS market_data.fundamentals (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(10) NOT NULL,
    fetched_at TIMESTAMPTZ DEFAULT NOW(),
    pe_ratio FLOAT,
    pb_ratio FLOAT,
    market_cap BIGINT,
    dividend_yield FLOAT,
    earnings_growth FLOAT,
    revenue_growth FLOAT,
    debt_to_equity FLOAT,
    free_cash_flow BIGINT,
    sector VARCHAR(50),
    industry VARCHAR(100),
    UNIQUE(ticker, fetched_at)
);
```

**Step 1:** Write test — `test_fetch_fundamentals` verifies yahooquery returns PE/PB/market cap for AAPL.
**Step 2:** Implement `src/fundamentals.py` with `fetch_fundamentals(ticker)` and `backfill_all(tickers)`.
**Step 3:** Verify data lands in Postgres.

---

## Task 2: Implement per-tick trader reflection

**Objective:** After each LLM decision, ask a second LLM call: "Given what happened this tick, what did you learn? Would you do anything differently?" Store in journal. Feed reflection into NEXT tick's prompt.

**Files:**
- Create: `src/reflection.py`
- Modify: `src/simulator.py` (call reflection after each decision)
- Modify: `src/prompt_builder.py` (include reflection context)
- Create: `tests/test_reflection.py`

**Reflection prompt:**
```
You just made this trading decision:
[{timestamp}] {decision} {ticker} @ ${price}: {rationale}

Signal context: regime={regime}, momentum={score}, RSI={rsi}

Reflect: What did you learn from this tick? What would you do differently next time in similar conditions? Be specific and actionable.
```

**Step 1:** Write test — verify reflection produces non-empty structured output.
**Step 2:** Implement `reflect_on_decision()` in `src/reflection.py`.
**Step 3:** Wire into `simulator.py` — call after each decision, store in `journal` alongside the decision entry.
**Step 4:** Modify `prompt_builder.py` — include last 3 reflections in the prompt context.
**Step 5:** Test end-to-end — simulate 10 ticks, verify reflections appear in journal and influence later decisions.

---

## Task 3: Journal analysis + counterfactual loop

**Objective:** After a sweep completes, analyze journals for patterns: high-conviction losses, missed opportunities, regime-specific performance. Generate concrete prompt/param change suggestions.

**Files:**
- Create: `src/journal_analyzer.py`
- Modify: `src/simulator.py` (run analysis after sweep)
- Create: `tests/test_journal_analyzer.py`

**Analysis dimensions:**
1. Conviction vs outcome: decisions with conviction > 0.5 that lost money
2. Regime performance: which regime has worst win rate?
3. Missed opportunities: ticks with strong signals where HOLD was chosen
4. Size mistakes: positions that were too large for the drawdown

**Output format:**
```python
@dataclass
class JournalInsight:
    category: str        # "HIGH_CONVICTION_LOSS", "REGIME_WEAKNESS", etc.
    description: str     # Human-readable finding
    suggestion: str      # Concrete change to make
    confidence: float    # 0.0-1.0 how sure the system is
    evidence: List[str]  # Supporting journal entries
```

**Step 1:** Write test — analyze a journal with 3 high-conviction losses, verify insights generated.
**Step 2:** Implement `analyze_journal()` with LLM-powered analysis.
**Step 3:** Wire into `simulator.py` — call after `run_sweep`, feed insights into hypothesis engine.

---

## Task 4: Nightly synthesis + auto-promotion

**Objective:** Every night, aggregate all journal insights from the day's sweeps into a summary. Rank suggestions by confidence. Auto-promote changes that meet thresholds (3+ nights sustained improvement).

**Files:**
- Create: `src/synthesis.py`
- Modify: night pipeline (call synthesis after sweeps)
- Create: `tests/test_synthesis.py`

**Synthesis output:**
```
=== Nightly Learning Summary: 2026-07-06 ===

Kairos — 324 scenarios, 47 trades
  Learned: "Buying during HIGH_VOLATILITY regime loses 80% of the time"
  Suggestion: "Add rule: skip BUY signals when vol_regime=HIGH"
  Confidence: 0.85 → AUTO-PROMOTED

Aldridge — no trades (fundamental data missing)
  PENDING: fundamentals backfill

Stonks — 156 scenarios, 12 trades
  Learned: "News sentiment lagged price moves by 2+ ticks"
  Suggestion: "Use sentiment as confirmation, not trigger"
  Confidence: 0.42 → Needs more validation
```

**Promotion thresholds:**
- Confidence > 0.75 AND sustained 3+ nights → AUTO-PROMOTE to trader config
- Confidence > 0.5 AND sustained 2 nights → Create PR for review
- Below threshold → Log for next night's analysis

**Step 1:** Write test — synthesize 2 traders' insights, verify ranking and threshold logic.
**Step 2:** Implement `Synthesizer.synthesize()` and `Promoter.evaluate()`.
**Step 3:** Wire into night pipeline — call after all sweeps complete.

---

## Task 5: Kairos backtesting toolkit

**Objective:** Kairos is no longer just a trader — it's the ML engineer. Give it tools to spawn simulations, register models, run backtest campaigns, and produce evaluation reports.

**Files:**
- Create: `src/backtest_kit.py`
- Create: `tests/test_backtest_kit.py`

**Toolkit capabilities:**
```python
class BacktestKit:
    def spawn_simulation(self, config: SimulationConfig) -> str  # returns run_id
    def list_models(self) -> List[ModelInfo]
    def register_model(self, model: ModelInfo) -> None
    def run_campaign(self, campaign: CampaignConfig) -> CampaignReport
    def evaluate(self, run_ids: List[str]) -> EvaluationReport
    def promote(self, model_id: str, target: str) -> bool  # to production
```

**Step 1:** Write tests for each capability.
**Step 2:** Implement BacktestKit.
**Step 3:** Wire Kairos' agent prompt to reference these tools.

---

## Task 6: Integration test — closed learning loop

**Objective:** Prove the loop closes end-to-end: simulate trades → reflect → analyze journal → generate insight → apply change → re-simulate → verify improvement.

**Files:**
- Create: `tests/test_learning_loop.py`

**Test scenario:**
```
1. Run 20-tick simulation with default params → journal with reflections
2. Analyze journal → generate insight (e.g., "volatility filter too tight")
3. Apply suggested change → relax vol_regime_threshold
4. Re-run same 20 ticks → verify more trades AND better P&L
5. Assert: post-change score > pre-change score
```

**Step 1:** Write the integration test (it will FAIL initially).
**Step 2:** Run it — verify it fails (proving the loop doesn't close yet).
**Step 3:** Fix any missing wiring until the test PASSES.
**Step 4:** Commit as "test: integration test for closed learning loop"

---

## Task 7: Aldridge buy-and-hold agent refactor

**Objective:** Rewrite Aldridge's agent prompt and decision logic. No more signal-based trading. Instead: weekly fundamental screen → buy qualified stocks → hold until fundamentals deteriorate.

**Files:**
- Modify: `config/traders.yaml` (Aldridge model, strategy)
- Create: `src/aldridge_strategy.py`
- Create: `tests/test_aldridge_strategy.py`

**Decision logic:**
```python
def aldridge_weekly_screen(fundamentals: List[Fundamental]) -> List[str]:
    """Screen for buy-and-hold candidates."""
    return [
        f.ticker for f in fundamentals
        if (f.pe_ratio and f.pe_ratio < 20 and f.pe_ratio > 0
            and f.dividend_yield and f.dividend_yield > 0.01
            and f.earnings_growth and f.earnings_growth > 0.05
            and f.debt_to_equity and f.debt_to_equity < 2.0)
    ]
```

**Step 1:** Write test — screen 10 tickers, verify only qualified pass.
**Step 2:** Implement `aldridge_strategy.py`.
**Step 3:** Update Aldridge prompt to describe buy-and-hold role.
**Step 4:** Integration test — weekly cycle with real fundamental data.

---

## Verification Checklist

- [ ] `src/reflection.py` — per-tick reflection works, stored in journal
- [ ] `src/journal_analyzer.py` — generates actionable insights from journals
- [ ] `src/synthesis.py` — nightly summary with auto-promotion
- [ ] `src/fundamentals.py` — fetches real P/E, P/B, div yield for watchlist
- [ ] `src/backtest_kit.py` — Kairos can spawn and evaluate simulations
- [ ] `src/aldridge_strategy.py` — buy-and-hold filter works
- [ ] `tests/test_learning_loop.py` — CLOSED LOOP PASSES
- [ ] All existing tests still pass (358 passed before changes)

---

## Risks

1. **Reflection cost**: Adding a second LLM call per tick doubles scenario cost. Mitigation: use cheapest model (v4-flash) for reflection, only v4-pro for decisions.
2. **Fundamental data quality**: yahooquery is unreliable. Mitigation: cache aggressively, accept missing data gracefully.
3. **Over-fitting to reflection**: If reflection says "buy less" and next tick goes up, system learns wrong lesson. Mitigation: aggregate over many ticks, use statistical significance.
4. **Prompt bloat**: Adding reflections to context grows prompts. Mitigation: cap at 3 most recent reflections, summarize older ones.
