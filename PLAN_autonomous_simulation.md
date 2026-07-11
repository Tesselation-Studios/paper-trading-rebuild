# Plan: Autonomous Historical Simulation for Trader Self-Improvement

## Goal
Run historical simulations that perfectly match real-time trading behavior, but faster. Virtual traders make decisions on past market data using real LLM prompts. Score them. Reflect. Generate new prompts. Repeat. The system improves on its own.

## Principles
- **Data bus is the only source of truth** for all market data. Requests go through it.
- **Real LLM calls** during simulation. No signal-engine shortcuts. The agent should make the same decisions it would make live.
- **Dynamic ticker selection** — traders pick their own stocks during simulation, just like live.
- **Walk-forward validation** — train on N days, validate on M days, repeat.
- **Full reflection** — between simulation ticks, record observations, synthesize insights.
- **Self-improvement loop** — winner prompts get promoted, parametrized, and evolved.

---

## What Exists (proven working)

| Component | Status | Notes |
|-----------|--------|-------|
| `src/bar_loader.py` | ✅ Works | Loads 5-min bars from local parquet cache |
| `src/replay.py` | ✅ Works | `ReplayHarness` — replay engine with portfolio, trades, P&L |
| `src/llm_engine.py` | ✅ Works | Makes real OpenRouter LLM calls |
| `src/signals.py` | ✅ Fixed | `momentum_threshold` recalibrated from 0.55 → 0.0005 |
| `src/prompt_sweep.py` | ⚠️ Patched | Signal-only phase1 works. Phase2 wants stale `replay_controller.py` |
| `src/simulator.py` | ⚠️ Half-wired | Has `sweep/deep/weekend` commands. PromptBuilder SSH's to localhost for agent files |
| `src/prompt_builder.py` | ❌ SSH dead end | Tries `ssh openclaw@192.168.1.41 cat ...` instead of reading local files |
| `agents/trader-{name}/prompt.txt` | ✅ Have them | Real prompt files for all 3 traders |
| Data bus (port 5000) | ✅ Running | Serves cached market data, fundamentals, sentiment |

## What Needs to Happen

### 1. Fix PromptBuilder to Read Local Agent Files
**Problem**: PromptBuilder SSH's to `openclaw@192.168.1.41` (this machine) to read agent files. On failure, falls back to hardcoded defaults — which are useless generic templates.

**Fix**: Add `_load_from_local()` method that reads from `project_root/agents/trader-{name}/prompt.txt`, `AGENTS.md`, `SOUL.md`, `MEMORY.md` directly. This is what `prompt_sweep.py` (`read_trader_prompt`) already does for AGENTS.md — extend to all files.

**File**: `src/prompt_builder.py`
**Change**: ~30 lines — add local file reading before SSH attempt.

### 2. Fix the Simulation Loop to Use Real Data
**Problem**: The single-date simulation was using `load_historical_ticks` which generates synthetic ticks. I already patched this to use `_load_dates_data` (real BarLoader data).

**Fix already applied**: Single-date sweep now calls `_load_dates_data([date_str])` at line 1049.

**Still needed**: Multi-date walk-forward sweep already works — uses `_load_dates_data`. Just need to verify interval_minutes=5 propagates properly.

### 3. Enable Dynamic Ticker Selection During Simulation
**Problem**: Virtual traders use a fixed ticker list. They should be able to choose stocks based on market conditions, just like live traders do.

**Fix**: The LLM trader function already receives a portfolio + market data tick. The trader can:
- Request fundamentals for any ticker (from data bus)
- Request sentiment for any ticker (from data bus)
- Get quotes for any ticker (from data bus)
- Make a decision based on what they find

The ticker universe should be the full tracked list (~30 tickers from `resolve_tickers("all")`). The trader picks from them based on signal strength for each.

### 4. Run Simulations Over Multiple Days (Walk-Forward)
**Problem**: The walk-forward split logic in `prompt_sweep.py` needs sufficient trading days. With 3 weeks of historical data (~15 trading days), we can do train=10, val=5 splits.

**Status**: Walk-forward exists in `_run_multidate_sweep`. Works if dates >= (train_days + val_days) * windows.

### 5. Between-Tick Reflections → Prompt Evolution
**Problem**: No between-tick reflection recording happens during simulation.

**Fix**: After each simulation tick (or at end of day), run the reflection loop:
- Call `src.journal_analyzer` on the day's decisions
- Synthesize insights into learning signals
- If a variant outperforms baseline, generate a new variant that incorporates the winning changes
- Save to `state/sweep_results/` for the next iteration

### 6. Score, Promote, Repeat (The Self-Improvement Loop)
**Problem**: The scoring + promotion pipeline exists in `prompt_sweep.py` but only promotes to git branches (not to live trader prompts).

**Fix**: 
- After sweep completes, attach winner score to the variant
- Promote winner prompt to `agents/trader-{name}/AGENTS.md` (for next simulation iteration)
- Log winning params to `parameter_history` table for ML analysis
- Generate N new variants from the winner for the next sweep cycle

---

## Implementation Order

### Phase A: Fix PromptBuilder (~1 hr)
1. Add `_load_from_local(self) -> AgentFiles` to prompt_builder.py
2. Read `agents/{trader}/prompt.txt` (or AGENTS.md), SOUL.md, MEMORY.md
3. Test: run `simulator test --trader kairos` — should use REAL prompts, not defaults

### Phase B: Verify Single-Day Sim Loop (~30 min)
4. Run `prompt_sweep --trader kairos --variants 5 --date 2026-07-10 --dry-run`
5. Verify: different variants produce different scores, trades > 0
6. Remove `--dry-run` — let LLM calls happen, verify they work

### Phase C: Multi-Day Walk-Forward Sweep (~1 hr)
7. Run `prompt_sweep --trader kairos --variants 5 --dates 5` (uses 44 date split: train+val)
8. Verify walk-forward windows work end-to-end
9. Add `--phase2` once prompt_builder reads local files (the simulator becomes the LLM replay engine)

### Phase D: Self-Improvement Loop (~2 hr)
10. Wire sweep results → reflection → new variant generation
11. Create `scripts/run_autonomous_sweep.sh` — the overnight loop script
12. Test: run loop twice, verify second iteration produces better prompts

### Phase E: Scale (~1 hr)
13. Run all 3 traders through the loop
14. Add dynamic ticker universe (30+ tickers, traders pick)
15. Verify: trader decisions differ across days based on market conditions

---

## What I'm NOT Going To Do (anti-scope)
- NOT rebuilding the replay controller. The simulator IS the replay controller.
- NOT supporting Phase 2 via `sweep_validation.py` + `replay_controller.py`. That path is dead.
- NOT doing signal-only sweeps for final validation. Real LLM calls only.
- NOT wiring to OpenClaw agent configs. Local file reading is sufficient.
- NOT adding Postgres dependency for sweep results. SQLite + parquet is fine.

---

## First Action: Fix PromptBuilder
The single change that unblocks everything. Once PromptBuilder reads local files, the simulator can use real agent prompts, and the full LLM-based sweep pipeline works.
