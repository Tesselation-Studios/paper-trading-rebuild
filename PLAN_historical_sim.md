# Plan: Historical Simulation System for Trader Self-Improvement

## Architecture Principles
- **Data bus** (docker.klo:5000) is the single source for ALL data. Local VM never runs its own DB.
- **No SSH.** Everything connects to docker.klo services. PromptBuilder reads local `agents/` files.
- **The replay engine (`src/replay.py`) is the core.** It already works — ticks in, trades out. We build around it.

## The CLI: `historical-sim`

A single entry point that replaces both `prompt_sweep` and `simulator`:

```bash
# Intraday simulation (5-min ticks)
python3 -m src.historical_sim intraday \
  --traders kairos,aldridge,stonks \
  --days 2026-07-06..2026-07-10 \
  --tickers dynamic \
  --variants 5

# Long-term simulation (daily ticks)
python3 -m src.historical_sim longterm \
  --trader aldridge \
  --start-date 2026-01-15 \
  --hold-period 30d \
  --tickers AAPL,MSFT,NVDA \
  --save-reflections

# Self-improvement loop (trains over multiple cycles)
python3 -m src.historical_sim improve \
  --traders kairos,stonks --days 5 --variants 5 --iterations 10
```

---

## Two Simulation Levels

### Level 1: Long-Term / Daily Simulation
**Purpose**: Test investment theses over weeks or months. Aldridge's territory — value picks, macro plays.

**How it works**:
1. Pick a start date in the past (e.g., 2026-01-15)
2. Virtual trader reads news/sentiment/fundamentals from data bus for that date
3. Trader picks stocks to buy
4. System fast-forwards to future dates (e.g., +7d, +30d, +90d) — each "tick" is a trading day
5. At each check-in, trader reads daily signals, decides HOLD/ADD/CUT/EXIT
6. Record P&L, reflections, win/loss on each pick
7. Score the prompt variant by total return, Sharpe, win rate, max drawdown

**Data sources** (from data bus):
- Daily bars (5-min → daily aggregation or Alpaca daily bars, 2yr history)
- News articles for the date (if cached)
- Fundamentals snapshot for the date
- Macro indicators for the date

### Level 2: Intraday / 5-Min Simulation
**Purpose**: Test tick-by-tick trading decisions. Kairos/Stonks territory — momentum, volume, intraday signals.

**How it works**:
1. Pick a date (e.g., 2026-07-10)
2. Load 5-min bars for all tracked tickers from data bus cache
3. Feed ticks in chronological order (across all tickers)
4. At each tick, trader evaluates tickers, picks stocks, makes BUY/SELL/HOLD decisions
5. Trades execute in the replay harness
6. At end of day: record P&L, win rate, Calmar, reflections
7. Score the prompt variant

**Data sources** (from data bus):
- 5-min bars for last 3 days (Alpaca's window) or our cached parquet files
- RSI/MACD/ATR indicators (computed from bars)

---

## Simulation Flow (both levels)

```
For each prompt variant:
  Create virtual trader (shares the LLM — same agent prompt, same model)
  
  For each day in the date range:
    Load ticks for that day from data bus
    Initialize portfolio (carry positions forward for Level 1)
    
    For each tick:
      Feed tick to virtual trader
      Trader may request additional data from data bus (quotes, news, fundamentals)
      Trader returns decision BUY/SELL/HOLD + conviction + rationale
      Replay harness executes trade, updates P&L
    
    End of day:
      Record reflections, synthesize learnings
      Log decision history to journal
  
  Score variant by total return, Sharpe, win rate, Calmar ratio
  Save params + score + prompt diff to sweep_results (Postgres on docker.klo)
  
If improvement loop:
  Promote winning variant
  Generate N new variants from winner
  Repeat
```

---

## What Needs To Be Built

### Phase 0: Fix Infrastructure (~1hr)

1. **Verify data bus Postgres connection** — `psql postgresql://trader:@192.168.1.179:5433/trading`
2. **Fix PromptBuilder to read local files** — add `_load_from_local()` that reads `agents/trader-{name}/AGENTS.md`, `prompt.txt`, `SOUL.md`, `MEMORY.md` directly (no SSH)
3. **Verify bar data availability** — check what dates/tickers are cached in parquet. Backfill weekends if needed.

### Phase 1: Build `historical_sim` CLI (~3hr)

The CLI app (`src/historical_sim.py`) with three subcommands:

**Subcommand: `intraday`**
- `--traders`: kairos, aldridge, stonks, or all
- `--days`: date range (2026-07-06..2026-07-10) or N
- `--tickers`: fixed list or "dynamic" (trader picks from all)
- `--variants`: how many prompt variants to test
- `--walk-forward`: enable train/val split
- `--train-days`, `--val-days`: for walk-forward
- `--output`: results file path

**Subcommand: `longterm`**
- `--trader`: single trader (aldridge primarily)
- `--start-date`: first date to evaluate
- `--check-in`: days between re-evaluations (7, 14, 30)
- `--hold-max`: maximum days to hold a position
- `--tickers`: list or "from-universe"
- `--variants`: how many prompt variants to test
- `--news`: enable news lookups during evaluation

**Subcommand: `improve`**
- `--traders`, `--days`, `--variants`, `--iterations`, `--mode` (intraday|longterm)
- Runs the full self-improvement loop
- Each iteration: sweep → score → promote winner → generate new variants from winner → repeat

### Phase 2: Wire Data Bus Queries (~2hr)

Virtual traders need the data bus during simulation. Currently they get data through bar_loader (parquet) + signals module.

**For Level 1 (long-term/daily)**:
- Add end point to data bus: `GET /historical/quotes?symbol=AAPL&date=2026-01-15`
- Add end point: `GET /historical/news?date=2026-01-15&tickers=AAPL,MSFT`
- Traders call these during simulation just like they call live endpoints

**For both levels**:
- Data bus already serves cached data. Just need to make date-parameterized queries.
- During weekends, add cron to data bus for historical backfill: `scripts/backfill_historical_news.py`, `scripts/backfill_historical_sentiment.py`

### Phase 3: Multi-Day Position Support (~2hr)

**Problem**: Current `ReplayHarness` resets portfolio per simulation run. For long-term tests, positions need to persist across days.

**Fix**: Extend `ReplayHarness` to:
- Accept an initial portfolio state (positions + cash from previous day)
- Record end-of-day portfolio as output
- Next day's simulation starts from that portfolio

For long-term sims: run N sequential days, each starting from the previous day's portfolio. The trader decides HOLD/ADD/CUT/EXIT based on evolving signals.

### Phase 4: Reflection + Auto-Tuning (~2hr)

After each simulation day/sweep:
1. Run `journal_analyzer` on the day's decisions
2. Generate learning signals (regime weaknesses, win rates, signal correlations)
3. Synthesize into prompt improvement suggestions
4. If in improvement loop: generate N new variants from winner, incorporating learnings
5. Log to `sweep_results` table (Postgres on docker.klo)

### Phase 5: Scale & Schedule (~1hr)

- Add cron: weekend-long sweep for all 3 traders
- Add cron: post-close daily improvement (single iteration)
- Post results to canvas
- Track parameter evolution over time

---

## What NOT To Do (anti-scope)
- Don't touch `prompt_sweep.py`'s phase 2 or `sweep_validation.py`. `historical_sim` replaces both.
- Don't run DB on the Casper VM. Everything connects to docker.klo Postgres or reads local parquet.
- Don't SSH anywhere. PromptBuilder reads local `agents/` directory.
- Don't build a web UI. CLI + canvas is sufficient.
- Don't handle real money. Simulation only.

---

## Verification

### Does a sweep produce trade differentiation?
```bash
python3 -m src.historical_sim intraday \
  --trader kairos --days 1 --tickers SPY,AAPL \
  --variants 5
```
Expected: variants have different P&L, win rate, trade count.

### Does a sweep beat live baseline?
```bash
python3 -m src.historical_sim intraday \
  --trader kairos --days 5 --tickers dynamic \
  --variants 5 --walk-forward
```
Expected: at least one variant beats baseline on validation days.

### Does the improvement loop converge?
```bash
python3 -m src.historical_sim improve \
  --trader stonks --days 3 --variants 5 --iterations 5
```
Expected: scores improve over iterations (not guaranteed, but that's the experiment).

---

**Total estimated time**: ~8-10hr of focused build work. Then it runs forever as a cron.