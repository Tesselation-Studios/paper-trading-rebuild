# Overnight Simulation Engine — Implementation Plan

> **For Hermes:** Build this step-by-step. TDD where indicated.
> **Goal:** Build an overnight harness that replays weeks of market data through LLM-powered traders using direct OpenRouter API calls, with journal accumulation across ticks, generating measurable scores to compare prompt/parameter variants.
> **Architecture:** New `src/simulator.py` in paper-trading-rebuild. Uses existing `replay.py` (harness), `signals.py` (math), `metrics.py` (scoring), `prompt_versioning.py` (variant mgmt). New: OpenRouter API integration + journal accumulator + sweep runner.
> **Tech Stack:** Python 3.11, NumPy, OpenRouter HTTP API, paper-trading-rebuild venv.

---

## Design Decisions

| Decision | Why |
|----------|-----|
| OpenRouter API directly | Skip OpenClaw agent dispatch — 30x faster per tick |
| Journal in prompt, not disk | Matches real trading day: agent "remembers" earlier decisions, doesn't stop to edit files |
| PromptRepo for variant management | Already built — git branches per variant, PR-based promotion |
| ReplayHarness unchanged | Pure math — tick execution stays deterministic |
| Scoring via `objective_score()` | Already exists — Calmar ratio, max drawdown, Sharpe |

---

## Architecture

```
Nightly Sweep:
  ┌──────────────────────────────────────────────────────────┐
  │ 1. Generate N prompt variants (PromptRepo)                │
  │ 2. For each variant:                                      │
  │    ┌─────────────────────────────────────────────────┐    │
  │    │ For each tick in market_data:                    │    │
  │    │   a. SignalEngine → indicators (math, fast)      │    │
  │    │   b. Build prompt: variant.md + journal[0..i-1]  │    │
  │    │      + tick_data + signal report                 │    │
  │    │   c. POST OpenRouter → parse TraderDecision      │    │
  │    │   d. Simulate fill, update portfolio             │    │
  │    │   e. Append to journal: what was decided + why   │    │
  │    └─────────────────────────────────────────────────┘    │
  │ 3. Score each run → rank variants                         │
  │ 4. Promote winner (PR to main), prune losers              │
  └──────────────────────────────────────────────────────────┘
```

## Component: Journal Accumulator

Not a file write — a running list of strings injected into the prompt:

```python
journal = []

def build_prompt(tick, signal, journal, prompt_md, memory_md):
    """Build the LLM prompt for one tick."""
    journal_text = "\n".join(journal[-10:])  # last 10 entries for context window
    return f"""You are a trading agent. Your strategy:

{prompt_md}

Your past decisions today:
{journal_text or '(no decisions yet — start of day)'}

Memory / market context:
{memory_md}

Current tick:
Ticker: {tick.ticker} | Price: {tick.close} | RSI: {tick.rsi}
Regime: {tick.regime} | Momentum: {tick.momentum}

Signal report:
Composite: {signal.composite_signal:.2f} | Conviction: {signal.conviction:.2f}

You have {portfolio.cash} cash, {len(portfolio.positions)} open positions.
Respond with JSON: {{"decision": "BUY|SELL|HOLD", "conviction": 0.0-1.0, "rationale": "..."}}"""
```

After each tick, the LLM's response is parsed and a journal entry appended:
```python
journal.append(f"[{tick.timestamp}] {decision.decision} {tick.ticker}@{tick.close}: {parsed['rationale']}")
```

---

## Files

### Create: `src/simulator.py`

New module. Three classes + one runner function:

```python
class LLMDecisionEngine:
    """Calls OpenRouter API for trading decisions."""
    def __init__(self, prompt_md: str, memory_md: str, model: str, api_key: str):
        ...
    def decide(self, tick: Tick, signal: SignalReport, journal: List[str],
               portfolio: Portfolio) -> TraderDecision:
        """Build prompt, call OpenRouter, parse response."""
        ...

class JournalManager:
    """Accumulates journal entries across ticks, feeds context window."""
    def __init__(self, max_entries: int = 10):
        self.entries: List[str] = []
    def add(self, entry: str) -> None: ...
    def last_n(self, n: int) -> str: ...
    def full(self) -> str: ...

class SweepRunner:
    """Runs N prompt variants through historical data, scores them."""
    def __init__(self, prompt_repo: PromptRepo, harness: ReplayHarness,
                 signal_engine: SignalEngine, model: str, api_key: str):
        ...
    def run_sweep(self, market_data: List[Tick], trader: str,
                  n_variants: int = 50) -> List[SweepResult]:
        """Generate variants, run each, return ranked results."""
        ...
    def run_parameter_and_prompt_sweep(
        self, market_data: List[Tick], trader: str,
        param_grid: Dict[str, List[float]],
        n_variants: int = 20) -> List[SweepResult]:
        """Simultaneously test param + prompt changes."""
        ...

@dataclass
class SweepResult:
    trader: str
    variant_id: int
    prompt_content: str
    params: Dict[str, float]
    metrics: ReplayResult
    objective_score: float
    journal: List[str]
```

### Modify: `src/replay.py`

The harness currently calls `TraderFn(tick, portfolio)`. We need it to also pass `signal` and `journal` context. Add optional params to `TraderFn` signature — OR use a closure that captures the needed state:

```python
# New: closure-based trader for replay
def make_llm_trader(engine: LLMDecisionEngine, signal_engine: SignalEngine,
                    journal_mgr: JournalManager) -> TraderFn:
    """Create a TraderFn that wraps LLM + signal + journal."""
    def trader_fn(tick: Tick, portfolio: Portfolio) -> TraderDecision:
        signal = signal_engine.process(tick)
        journal = journal_mgr.entries
        decision = engine.decide(tick, signal, journal, portfolio)
        if decision.decision != "HOLD":
            entry = f"[{tick.timestamp}] {decision.decision} {tick.ticker}@{tick.close}: {decision.rationale}"
            journal_mgr.add(entry)
        return decision
    return trader_fn
```

No changes to `ReplayHarness.run()` — closure pattern keeps existing interface.

### Create: `config/sweep.yaml`

```yaml
sweep:
  variants_per_trader: 50       # how many prompt variants to test nightly
  days_of_data: 14              # weeks of historical data
  parallel_requests: 4          # concurrent OpenRouter calls
  model: "openrouter/anthropic/claude-sonnet-4.5"
  scoring_metric: "objective"   # use objective_score()

openrouter:
  api_key_env: "OPENROUTER_API_KEY"
  base_url: "https://openrouter.ai/api/v1"
  timeout: 30                   # seconds per API call
  max_retries: 2

traders:
  - kairos
  - aldridge
  - stonks
```

### Create: `tests/test_simulator.py`

| Test | What it validates |
|------|-------------------|
| `test_llm_engine_builds_prompt` | Prompt includes tick data, signal, journal, portfolio |
| `test_llm_engine_parses_buy` | OpenRouter response JSON → TraderDecision(BUY) |
| `test_llm_engine_parses_hold` | Response → HOLD |
| `test_llm_engine_handles_malformed` | Garbage response → HOLD (safe fallback) |
| `test_journal_accumulation` | Journal grows across ticks, last_n() works |
| `test_sweep_runner_generates_variants` | N prompt branches created |
| `test_sweep_runner_ranks_correctly` | Higher objective_score → earlier in results |
| `test_end_to_end_synthetic` | 20 synthetic uptrend ticks → positive P&L |
| `test_prompt_and_param_sweep` | Combined sweep produces results for both axes |

---

## Step-by-Step Tasks

### Phase 0: Setup

**Task 0.1: Review existing infra**
- Read `src/metrics.py` (objective_score signature)
- Read `src/signals.py` (SignalReport fields)
- Read `src/replay.py` (Tick, Portfolio, TraderDecision, ReplayHarness)
- Read `src/prompt_versioning.py` (PromptRepo API)
- Read `config/traders.yaml` (model settings)

**Task 0.2: Verify OpenRouter access**
- Test: `curl -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/models`
- Confirm api key works, note rate limits

### Phase 1: LLM Decision Engine

**Task 1.1: Create LLMDecisionEngine skeleton**
- File: `src/simulator.py`
- Class with `__init__`, `decide()`, `_build_prompt()`, `_parse_response()`
- Stub `decide()` returns HOLD until wired

**Task 1.2: Build prompt formatter**
- Implement `_build_prompt()` — compact but complete tick context
- Test: `test_llm_engine_builds_prompt` — verify prompt contains tick data, signal, journal, portfolio

**Task 1.3: Wire OpenRouter API call**
- `POST /api/v1/chat/completions` with model from config
- Temperature=0.3 (keep it moderately deterministic)
- Parse JSON response → TraderDecision
- Error handling: malformed JSON, HTTP errors, timeouts → HOLD
- Test: `test_llm_engine_parses_buy`, `_hold`, `_handles_malformed`

**Task 1.4: Make it work with real API**
- Test with one real tick + dummy prompt → verify OpenRouter response

### Phase 2: Journal Manager

**Task 2.1: Implement JournalManager**
- Simple list-based accumulator
- `add()`, `last_n()`, `full()`
- Max entries configurable
- Test: `test_journal_accumulation`

### Phase 3: Sweep Runner

**Task 3.1: Implement SweepRunner**
- Takes PromptRepo, ReplayHarness, SignalEngine, model, api_key
- `run_sweep()`: for each variant, create LLMDecisionEngine → make_llm_trader → harness.run() → score → collect SweepResult
- Returns list sorted by objective_score descending

**Task 3.2: Variant generation**
- Use PromptRepo's existing variant creation
- Variants are slight prompt perturbations (add/remove sentences, adjust thresholds in text)

**Task 3.3: End-to-end test**
- Synthetic uptrend data, 2 variants, verify both run and produce scores
- Test: `test_end_to_end_synthetic`

### Phase 4: Configuration

**Task 4.1: Create sweep.yaml config**
- Sweep settings, OpenRouter settings, trader list

**Task 4.2: Configuration loader**
- `load_sweep_config()` function in simulator.py
- Reads sweep.yaml, applies env var overrides

### Phase 5: CLI + Cron

**Task 5.1: CLI entry point**
```bash
python3 -m src.simulator --trader kairos --days 14
python3 -m src.simulator --all-traders --days 7
python3 -m src.simulator --trader kairos --param-sweep
```

**Task 5.2: Overnight cron**
- Schedule via Hermes cron: nightly at 1am ET
- Run sweep for all traders
- Report results (ranked variants with scores) to Telegram
- Auto-promote winner if improvement > threshold

### Phase 6: Parameter + Prompt Combined Sweep

**Task 6.1: Combined sweep support**
- `run_parameter_and_prompt_sweep()` — grid search over param × prompt variants
- Each combination gets its own run, scored, ranked
- Output: which param+prompt combo performed best

**Task 6.2: Test combined sweep**
- 2 params × 3 variants = 6 runs, verify ranking

---

## Risks & Tradeoffs

| Risk | Mitigation |
|------|-----------|
| OpenRouter costs (50 variants × 14 days × 50 ticks = 35K API calls/night) | Cap variants, use cheaper model for sweeps (flash?), sample fewer ticks |
| LLM non-determinism | Temperature=0.3, but accept variance. Run baseline twice to measure noise floor |
| Prompt overflow with long journals | last_n(10) cap. Longest running context = ~2K tokens |
| Market data availability | Use Alpaca historical API. Fallback: synthetic data for testing |

---

## Verification

After each phase, verify:
1. `pytest tests/test_simulator.py -v` — all new tests pass
2. `pytest tests/ -v` — no regressions in existing rebuild tests
3. Manual run with 2-day synthetic data → produces ranked results
4. OpenRouter API errors handled gracefully (HOLD, don't crash)
