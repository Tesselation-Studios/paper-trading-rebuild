# 🗺️ Paper Trading Rebuild — Master Roadmap

> **Repo:** `Tesselation-Studios/paper-trading-rebuild`
> **Board:** [GitHub Projects](https://github.com/users/casper-bot-wodinga/projects/2)
> **Last updated:** 2026-07-06 (overnight Canvas watch doc)
> **Active profile:** Raf watching on Canvas — this is the single source of truth for what's being worked on.

---

## 📊 Health Dashboard

| Check | Status | Detail |
|-------|--------|--------|
| Risk gate BOOTSTRAP_MODE | ✅ `True` (invariant #12 patched) | WARNING-only quality gates, no false vetoes |
| Docker VM (.179) | ✅ Up, Postgres ready | Postgres port 5433 accessible |
| OpenClaw (.41) | ✅ Up at `192.168.1.41` | Data bus :5000, dashboard :5002 |
| Live traders | ⚠️ Paused / stalled | See P0 bugs below |
| Data bus | ❌ Stale quotes | Returning June 16 data for COIN, MSTR, PLTR |
| Stonks pipeline | ❌ Silent | Not producing signals |
| Learning loop | ❌ Empty | Returns no trade data (format mismatches) |

---

## 📐 Ownership Boundaries

| Owner | Runs On | Scope |
|-------|---------|-------|
| **Hermes** (.131) | `/home/raf/projects/paper-trading-rebuild/src/` | Signal engine, replay, validation, metrics, DB layer, risk gate, prompt sweep, synthesis, backfill, Docker infra, integration tests, config |
| **Casper** (.41) | OpenClaw agents, `paper-trading-teams/` | Trader agents (Kairos/Aldridge/Stonks), data bus (:5000), dashboard (:5002), MCP skills, sentiment pipeline, heartbeat sessions, Alpaca execution, sync_alpaca_positions |

> **Communication:** Hermes ↔ Casper bridge at `~/projects/hermes-openclaw-bridge/`. Delegate via Casper's `main` agent.

---

## 🔴 P0 — Traders Can't Trade (Drop Everything)

These bugs prevent live trading from functioning. No learning loop can run without trades flowing.

### Data Bus — Stale / Broken

| # | Issue | Owner | Blocked? | Action |
|---|-------|-------|----------|--------|
| **43** | Data bus returning stale quotes — COIN, MSTR, PLTR stuck on June 16 | Casper | Nothing | Restart data bus; investigate cache expiry; add freshness heartbeat |
| **41** | Kairos: Sector ETF data 20 days stale | Casper | Blocked by #43 | Fix after data bus is healthy; add sector ETF refresh to data bus cron |
| **54** | `sync_alpaca_positions` returns stale data — positions don't match Alpaca reality | Casper | Nothing | Fix sync logic; add reconciliation alert on mismatch |
| — | **Casper: Stale quotes** — data bus returning June 16 data system-wide | Casper | Nothing | Root cause: data bus cache layer not refreshing. Hermes can't fix — Casper owns data bus |

### Pipeline — Silent / Stalled

| # | Issue | Owner | Blocked? | Action |
|---|-------|-------|----------|--------|
| — | **Casper: Stonks pipeline silence** — not producing signals, no trades for days | Casper | Nothing | Investigate Stonks cron; check sentiment API connectivity; verify OpenClaw session alive |
| **53** | Volume filter blocks entries in CHOPPY + Extreme Fear regimes — traders can't act when they should | Hermes | Nothing | ✅ Fixed in `63eeb03`. Volume filter now bypasses when MEAN_REVERTING + F&G ≤ 30. `volume_ratio` + `volume_pass` in SignalReport. |
| **45** | Entry gate blocking sell orders — risk gate rejects SELL decisions | Hermes | Nothing | ✅ Fixed in `b6cddb5`. Added ConvictionGate — BUY-only validation, never blocks SELL. 7 new tests. |

### Aldridge — Stop-Loss

| # | Issue | Owner | Blocked? | Action |
|---|-------|-------|----------|--------|
| **42** | ADBE missing stop-loss in DB — Aldridge entered without stop, no protection | Hermes | Nothing | Backfill stop-loss for ADBE position; add assertion that every BUY writes stop-loss |
| — | **Casper: Aldridge stop-loss not wired up** — stop-loss checks not running on OpenClaw side | Casper | Nothing | Wire stop-loss monitoring into Aldridge heartbeat; Hermes can only enforce at risk-gate level |

---

## 🟠 P1 — Traders Trading But Broken

Trades flow, but with bugs that corrupt data, waste tokens, or cause wrong decisions.

### Learning Loop — Empty / Broken

| # | Issue | Owner | Blocked? | Action |
|---|-------|-------|----------|--------|
| **52** | Learning loop returns no trade data — pipeline format mismatch | Hermes | P0 data bus | Fix format contract between data bus output and learning loop input; add schema validation |
| **44** | Learning loop returns empty — CLI format mismatch | Hermes | P0 data bus | Same root cause as #52; unify CLI ↔ pipeline format |
| — | **Casper: LL-001→6** — 6 learning loop cards, none started | Casper | P0 fixes | These are Casper's learning loop integration tasks; delegate once P0 bugs resolved |

### Trader-Specific Bugs

| # | Issue | Owner | Blocked? | Action |
|---|-------|-------|----------|--------|
| **49** | Aldridge: Tech concentration 48% exceeds 25% limit | Casper | Nothing | Aldridge needs sector balance rule in prompt; risk gate should enforce `max_sector_pct: 0.30` from `config/risk.yaml` |
| **48** | Kairos: `skill_market_data` fails for comma-separated tickers | Casper | Nothing | Fix MCP skill to handle comma-separated ticker lists; add test |
| **47** | Aldridge: `add_to_watchlist()` NameError on `dry_run` | Casper | Nothing | Variable scoping bug in Aldridge agent code; add `dry_run` to function scope |
| **46** | Social sentiment API returning neutral/empty — Stonks has no edge signal | Casper | Nothing | Check Reddit/Twitter API access; verify sentiment aggregator is running; add freshness alert |

### Schema & Monitoring

| # | Issue | Owner | Blocked? | Action |
|---|-------|-------|----------|--------|
| **28** | Fix DB drift — `schema.sql` vs reality (Postgres tables don't match spec) | Hermes | Nothing | Diff `src/db/schema.sql` against live Postgres; generate migration; apply |
| **29** | D-state alert for stuck traders — no alert when trader hasn't produced a decision in N ticks | Hermes | Nothing | Add D-state monitor to Hermes watchdog cron; alert if trader silent > 3 ticks |

---

## 🟡 P2 — Enhancements (Build After P0/P1)

These are the rebuild's feature set. They make the system better but aren't blocking basic trading.

### Validation & Quality

| # | Enhancement | Owner | Blocked? | Notes |
|---|-------------|-------|----------|-------|
| **21** | Two-phase validation (signal → LLM) | Hermes | — | `src/sweep_validation.py` exists; wire into nightly pipeline. Phase 1: signal engine filter (free). Phase 2: LLM replay (top 3). Agreement gate. |
| **19** | Walk-forward validation | Hermes | — | `src/validation.py` exists. Integrate into nightly sweep. Train T-90→T-30, validate T-30→T. |
| **20** | Transaction costs in replay | Hermes | — | `src/transaction_costs.py` exists. Wire into `src/replay.py` ReplayHarness. Critical per fusion review — optimizes net P&L, not gross. |
| **16** | Integration test: learning loop end-to-end | Hermes | P0/P1 loop fixes | Full pipeline test: data → signal → replay → sweep → promote. |

### Prompt Management

| # | Enhancement | Owner | Blocked? | Notes |
|---|-------------|-------|----------|-------|
| **27** | Prompt tiering — prod/candidate registry | Hermes | — | `src/prompt_versioning.py` exists. Formally separate prod vs candidate prompts. Gate promotions. |
| **14** | Nightly synthesis + auto-promote | Hermes | — | `src/synthesis.py` exists. Wire cron, fan-out to Docker workers, auto-PR winners. |

### Data Infrastructure

| # | Enhancement | Owner | Blocked? | Notes |
|---|-------------|-------|----------|-------|
| **17** | `backfill_bars.py` — Yahoo 5-min fetcher | Hermes | — | `scripts/backfill_bars.py` exists. Run backfill for historical data. Needed for walk-forward. |
| **18** | BarLoader — Parquet → Tick bridge | Hermes | — | `src/bar_loader.py` exists. Fast historical data loading for replay workers. |
| **23** | Parameter history tracking | Hermes | — | Log every parameter change with before/after scores. Track convergence/oscillation per fusion review. |

### Analytics & Dashboards

| # | Enhancement | Owner | Blocked? | Notes |
|---|-------------|-------|----------|-------|
| **25** | Performance metrics dashboard | Hermes | — | Live rolling Calmar, Sortino, PF, Expectancy. Compare vs SPY B&H. |
| **26** | A/B experiment runner | Hermes | P0 | Shadow mode from SPEC §11. Run new config alongside live for 5 days before promoting. |

### Trader Enhancements

| # | Enhancement | Owner | Blocked? | Notes |
|---|-------------|-------|----------|-------|
| **22** | Structured JSON journals | Hermes | — | `src/journal_analyzer.py` exists. Enforce JSON schema on journal entries. Makes counterfactuals possible. |
| **12** | Per-tick reflection | Hermes | — | `src/reflection.py` exists. After each tick: what happened, why, what would I change? |
| **13** | Journal analysis: counterfactual loop | Hermes | — | "Would holding longer have worked?" "Should I have bought what I watched?" Per SPEC §14. |
| **11** | Aldridge value investor + fundamentals | Hermes | — | `src/aldridge_strategy.py` and `src/fundamentals.py` exist. Wire fundamentals scoring (P/E, dividend, earnings quality) into Aldridge's signal feed. |
| **15** | Kairos ML backtesting toolkit | Hermes | — | ML backtesting for Kairos momentum strategy. Regime-aware performance. |

### Automation

| # | Enhancement | Owner | Blocked? | Notes |
|---|-------------|-------|----------|-------|
| **24** | Learning loop cron (nightly automation) | Hermes | P0/P1 loop fixes | Wire nightly cron: sweep → validate → promote. Runs 3 AM ET. |

---

## 🟢 P3 — Nice-to-Have / Future

These aren't blocking anything. Do them when there's bandwidth.

| # | Task | Owner | Blocked? | Notes |
|---|------|-------|----------|-------|
| **8** | Migrate Canvas + Dashboard to Docker (.179) | Hermes | Docker VM up | Move dashboard from .41 to .179. Containerize. Phased per DECISIONS #5. |

### Post-Migration Cleanup

| Task | Owner | Notes |
|------|-------|-------|
| Remove SQLite dependency from rebuild codebase | Hermes | After migration complete, drop all SQLite references |
| Decommission legacy `paper-trading-teams/` data bus | Casper | After rebuild data bus is stable |
| Clean up old cron jobs pointing to legacy code | Both | Audit all crons; only rebuild crons should remain |
| Archive `paper-trading-teams/` repo | Hermes | After all traders migrated and stable for 30 days |
| Remove sync bridge (Postgres → SQLite) | Hermes | Per DECISIONS #5, Phase 3 |
| Verify all 13 architectural invariants hold post-migration | Hermes | Run `python3 -m pytest tests/test_spec_verify.py -v` |

---

## 📋 Casper's Remaining Cards (Delegate via Bridge)

These are Casper-owned items from his backlog that aren't captured in GitHub issues:

| Card | Description | Status | Delegate? |
|------|-------------|--------|-----------|
| LL-001 | Learning loop: trade data ingestion | Not started | Delegate after P0 data bus fixes |
| LL-002 | Learning loop: signal correlation | Not started | Delegate after LL-001 |
| LL-003 | Learning loop: prompt impact scoring | Not started | Delegate after LL-002 |
| LL-004 | Learning loop: auto-parameter proposal | Not started | Blocked by P0/P1 |
| LL-005 | Learning loop: validation gate | Not started | Blocked by P0/P1 |
| LL-006 | Learning loop: promote/revert | Not started | Blocked by P0/P1 |
| — | Stale quotes — data bus root cause | In progress? | **URGENT: Blocking everything** |
| — | Stonks pipeline silence | Not started | Delegate immediately |
| — | Aldridge stop-loss wiring | Not started | Delegate after P0 buys flow |

---

## 🎯 Next Actions (Tonight / Tomorrow)

### Hermes — Immediate (P0)
- [x] Fix #53: Volume filter bypass in CHOPPY + Extreme Fear (`src/signals.py`) ✅ `63eeb03`
- [x] Fix #45: Entry gate should allow SELL (`src/risk/gates.py`) ✅ `b6cddb5`
- [ ] Fix #42: Backfill ADBE stop-loss; add assertion
- [ ] Fix #28: Diff schema.sql vs live Postgres, generate migration

### Hermes — After P0
- [ ] Fix #52/#44: Unify learning loop format
- [ ] Fix #29: D-state alert watchdog
- [ ] Wire #20: Transaction costs into replay harness

### Delegate to Casper (via bridge)
- [ ] **URGENT:** Fix stale data bus quotes — system-wide June 16 data
- [ ] **URGENT:** Investigate Stonks pipeline silence
- [ ] Fix #41: Sector ETF data refresh
- [ ] Fix #54: sync_alpaca_positions reconciliation
- [ ] Fix #48: skill_market_data comma-separated tickers
- [ ] Fix #47: add_to_watchlist() NameError
- [ ] Fix #46: Social sentiment API
- [ ] Fix #49: Aldridge tech concentration

### Delegate to Casper — Learning Loop (after P0)
- [ ] LL-001 through LL-006: Learning loop cards
- [ ] Wire Aldridge stop-loss monitoring

---

## 📊 Issue Summary

| Priority | Count | Issues |
|----------|-------|--------|
| P0 | 10 | #54, #53, #43, #45, #42, #41 + 4 Casper items |
| P1 | 9 | #52, #49, #48, #47, #46, #44, #29, #28 + Casper LL cards |
| P2 | 16 | #27, #26, #25, #24, #23, #22, #21, #20, #19, #18, #17, #16, #15, #14, #13, #12, #11 |
| P3 | 2+cleanup | #8 + post-migration items |
| **Total** | **37+** | |

---

## 🏗️ Architectural Invariants — Status Check

Per `SPEC.md` §1.3. Must audit post-migration.

| # | Invariant | Status | Notes |
|---|-----------|--------|-------|
| 1 | Write-on-transaction | ⚠️ | Verify Postgres writes are synchronous |
| 2 | Async confirmation | ⚠️ | Verify no blocking Alpaca calls |
| 3 | No dead code | ✅ | CI gate enforces coverage |
| 4 | Config from files, not DB | ✅ | `config/risk.yaml` is source of truth |
| 5 | Trader-as-learner | ✅ | Agents do inference in their own ticks |
| 6 | Ground truth is P&L | ⚠️ | Need to verify realized P&L pipeline |
| 7 | Out-of-sample validation | ⚠️ | Walk-forward wired but not integrated into nightly |
| 8 | Idempotent ticks | ⚠️ | Need tests |
| 9 | Bootstrap fast and small | ✅ | Learning mode active, loose start |
| 10 | Risk gate mirrors prompts | ⚠️ | #29-style drift — need CI enforcement |
| 11 | Cron is trigger, not instruction | ✅ | Cron messages fixed per DECISIONS #14 |
| 12 | Decision quality gates warning-only during bootstrap | ✅ | BOOTSTRAP_MODE=True (just patched) |
| 13 | Cron timeout must exceed model inference time | ⚠️ | Verify 600s timeouts set for Aldridge/pro |

---

## 🔗 Quick Links

| What | Where |
|------|-------|
| Repo | `Tesselation-Studios/paper-trading-rebuild` |
| Issues | [GitHub Issues](https://github.com/Tesselation-Studios/paper-trading-rebuild/issues) |
| Board | [GitHub Projects](https://github.com/users/casper-bot-wodinga/projects/2) |
| Dashboard | `http://192.168.1.41:5002` |
| Data bus | `http://192.168.1.41:5000` |
| Spec | [SPEC.md](SPEC.md) |
| Decisions | [DECISIONS.md](DECISIONS.md) |
| Fusion review | [fusion-review.md](fusion-review.md) |
| Docker VM | `192.168.1.179`, Postgres :5433 |
| Hermes ↔ Casper bridge | `~/projects/hermes-openclaw-bridge/` |

---

> **Canvas watch note:** This document is the overnight reference. Raf watches Canvas. Mark items `[x]` as completed. Add new bugs to P0/P1 as discovered. The goal: all P0 items cleared by Tuesday market open.
