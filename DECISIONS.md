# Architectural Decision Records — Paper Trading Rebuild

This file documents key architectural decisions made during the rebuild. Each entry: date, status, context, decision, alternatives, consequences.

**Last updated**: 2026-07-06

---

## 1. Rebuild Over Legacy

**Date:** 2026-07-05
**Status:** Accepted

**Context:**
The legacy `paper-trading-teams` codebase accumulated 11 months of organic growth — 5,200-line `data_bus.py`, SQLite with multi-tenant schema drift, cron-vanishing bugs, and silent pipeline stalls. Fixing each issue in-place would require touching 15+ files with cascading side effects. The system had drifted far from its original architecture.

**Decision:**
Start a clean rebuild (`paper-trading-rebuild`) with the benefit of every lesson learned. Keep the existing dashboard and live traders running against the legacy codebase while the rebuild matures in parallel. The rebuild is a spec-first project — every component is designed before it's written.

**Alternatives considered:**
- **Incremental refactor:** Rejected — the surface area was too large. Changing the DB layer alone would have touched the data bus, heartbeat, dashboard, and all three trader agents simultaneously.
- **Fork and patch:** Rejected — maintaining a fork while the original kept evolving would create merge hell.
- **Greenfield in a new language (Rust/Go):** Rejected — the team knows Python, the ML ecosystem is Python-native, and the LLM agents run in OpenClaw (Node.js). A Python rebuild is the fastest path to production.

**Consequences:**
- **Pro:** Clean architecture designed from first principles. Postgres-native. Walk-forward validated.
- **Pro:** No downtime — traders continue running on legacy while rebuild matures.
- **Con:** Two systems to maintain during transition. Legacy traders write to SQLite; rebuild reads from Postgres.
- **Con:** Migration risk — traders must switch from legacy signals to rebuild signals without regressing P&L.

---

## 2. K-Means Over HMM for Regime Detection

**Date:** 2026-07-06
**Status:** Accepted

**Context:**
The legacy system used a rule-based regime classifier (4 hardcoded regimes: TRENDING_UP, TRENDING_DOWN, HIGH_VOLATILITY, MEAN_REVERTING) with only two features (momentum + volatility). The v4 HMM approach on the Mac GPU worker produced probabilistic regime assignments but required expensive training cycles and serial processing. Market regimes are inherently multi-dimensional — a "panic" regime has volume, breadth, correlation, and VIX characteristics that simple thresholds can't capture.

**Decision:**
Use K-means clustering with engineered multi-dimensional features (momentum, volatility, volume profile, breadth, VIX term structure, overnight variance) for regime detection. K-means is fast, deterministic, and captures more market dimensions than the rule-based classifier — but it sacrifices the temporal dependency modeling that HMMs provide.

**Temporal compromise acknowledged:**
K-means treats each day as independent. It can't model "panic on Monday → transition on Tuesday → calm on Wednesday" as a sequence. This is a deliberate trade-off: the faster iteration speed of K-means (train in seconds vs. minutes for HMM) enables more frequent model updates and parameter sweeps. If temporal regime transitions prove critical, upgrade path: K-means → Gaussian Mixture (GMM) → HMM.

**Alternatives considered:**
- **HMM (v4 on Mac GPU):** Rejected for rebuild — training latency, serial processing, and complexity overhead. May return as a P3 upgrade.
- **Rule-based (keep legacy):** Rejected — the rebuild's reason for existing is to fix the two-feature blindness.
- **DBSCAN:** Considered for density-based clustering. Rejected — K-means' fixed K is simpler to tune and the spec already defines K=5 clusters.

**Consequences:**
- **Pro:** Multi-dimensional regime detection captures real market complexity.
- **Pro:** Fast training — cluster assignments computed in seconds, enabling nightly re-training.
- **Con:** No temporal dependency modeling. Regime transitions are invisible to the model.
- **Con:** Fixed K — if the "true" number of regimes changes over time, K-means will force-fit.

---

## 3. Two-Phase Validation (SignalEngine Filter → LLM Replay)

**Date:** 2026-07-06
**Status:** Accepted

**Context:**
The legacy system's nightly pipeline tested prompts by replaying them directly through the LLM on historical data. Each replay tick cost real API tokens. With 3 traders × 5 prompt variants × 20 replay dates, nightly costs could exceed $50/day in LLM API calls — for experiments that mostly confirmed "this prompt is worse than the current one."

**Decision:**
Two-phase validation pipeline:

1. **Phase 1 (SignalEngine filter):** Run parameter sweeps through the deterministic signal engine first. Only signal parameters — no LLM calls. Benchmark performance against the objective function. Reject any parameter set that underperforms the current baseline.

2. **Phase 2 (LLM replay):** Only the top ~3 surviving parameter sets from Phase 1 graduate to LLM replay. These are the promising candidates — worth the token cost.

This is analogous to a pre-screen in drug discovery: test thousands of candidates cheaply, only run expensive tests on the most promising.

**Alternatives considered:**
- **Direct LLM sweep (legacy approach):** Rejected — too expensive, too slow.
- **No validation — just ship:** Rejected — defeats the "measurably improve" success criterion.
- **Signal-only optimization (no LLM phase):** Rejected — the LLM traders make the final decision. Signal parameters that look good in isolation may interact poorly with the trader's prompt.

**Consequences:**
- **Pro:** 90%+ cost reduction in nightly validation. Only 3-5 LLM replays per trader per night.
- **Pro:** Faster iteration — Phase 1 runs in seconds, enabling 100+ parameter combinations per night.
- **Pro:** Natural separation of concerns — signal engine and LLM trader are independently tunable.
- **Con:** Phase 1 is a proxy — it might filter out parameters that the LLM would have used well.
- **Con:** Two-phase adds complexity to the optimization pipeline orchestration.

---

## 4. Postgres Over SQLite

**Date:** 2026-07-05
**Status:** Accepted (migration in progress)

**Context:**
The legacy system used SQLite (`shared/trader.db`) for simplicity — zero operations, file-based backup. This worked for a single-machine, three-trader workload. But the rebuild introduces: concurrent LLM replay workers writing decisions in parallel, the nightly optimization pipeline running alongside live traders, and future ambitions for multi-trader scaling.

SQLite's single-writer lock (even in WAL mode) becomes a bottleneck under these concurrent write patterns. The rebuild also needs: proper schema migrations (Alembic), connection pooling, point-in-time recovery, and the ability to run analytics queries without blocking live writes.

**Decision:**
Postgres as the rebuild's database. The legacy system continues writing to SQLite during the transition. The rebuild's `src/db/` layer provides a clean abstraction so consumers don't care about the underlying engine.

**Alternatives considered:**
- **SQLite (keep legacy approach):** Rejected — doesn't scale to concurrent writes from replay + optimization + live trading.
- **DuckDB:** Considered for analytical performance. Rejected — DuckDB is an analytical engine, not an OLTP database. Doesn't handle concurrent writes well.
- **SQLite + Litestream for replication:** Considered for zero-ops Postgres-like durability. Rejected — doesn't solve the concurrent write problem.

**Consequences:**
- **Pro:** Concurrent write safety — replay, optimization, and live trading can all write simultaneously.
- **Pro:** Alembic migrations, connection pooling, proper backup/restore tooling.
- **Pro:** Grows with system complexity (no ceiling like SQLite).
- **Con:** Operational overhead — Postgres container to manage, connection strings, pg_hba.conf.
- **Con:** Migration still in progress — `scripts/migrate_sqlite_to_pg.py` exists but not all consumers are cut over.
- **Con:** Legacy traders still write to SQLite — data bus reads from legacy DB during transition.

---

## 5. Dashboard Phased Migration

**Date:** 2026-07-06
**Status:** Accepted

**Context:**
The legacy dashboard (`src/leaderboard_api.py` + `src/leaderboard_ui/`) on port 5002 has been running stably for months. It reads from SQLite, serves P&L curves, leaderboard rankings, and trade history. The rebuild introduces Postgres, a new metrics engine, and a different data model. Replacing the dashboard in one cutover would be risky — if the rebuild dashboard has bugs, Raf loses visibility into trader P&L for potentially days.

**Decision:**
Three-phase dashboard migration:

1. **Phase 1 (now):** Keep legacy dashboard running. It reads from legacy SQLite. No changes.
2. **Phase 2 (next):** Sync bridge — `scripts/sync_bridge.py` writes rebuild Postgres data back to legacy SQLite tables so the old dashboard stays current while rebuild matures.
3. **Phase 3 (future):** Rebuild-native dashboard — new frontend reading directly from Postgres. Legacy dashboard decommissioned.

**Alternatives considered:**
- **Big-bang dashboard replacement:** Rejected — risk of losing Raf's visibility during critical transition period.
- **Rebuild dashboard first, traders second:** Rejected — dashboard without traders has nothing to display. Traders must come first.
- **Dual dashboards indefinitely:** Rejected — confusion about which dashboard is authoritative.

**Consequences:**
- **Pro:** No loss of dashboard visibility during transition.
- **Pro:** Phased cutover allows incremental validation of rebuild data against legacy.
- **Con:** Sync bridge is a temporary piece of infrastructure that must be maintained until Phase 3.
- **Con:** Two dashboards exist concurrently during Phase 3 cutover — potential confusion.

---

## 6. Walk-Forward Validation Over Simple Multi-Date Backtest

**Date:** 2026-07-06
**Status:** Accepted

**Context:**
The legacy system's "validation" was running the same parameter set across multiple historical dates and averaging the results. This is a simple multi-date backtest — it treats all dates as equally informative, regardless of temporal ordering. In financial ML, this is dangerous: training on January through June and testing on January through June (even as individual dates) leaks information. The optimizer can overfit to patterns that are common across the entire date range but don't persist out-of-sample.

**Decision:**
Walk-forward validation (`src/validation.py`): train on a rolling window, test on the subsequent out-of-sample period, advance the window, repeat. This simulates how the system actually runs — you only know the past when making decisions about the future.

Example: Train on Jan 1-30 → test on Jan 31-Feb 6. Then train on Jan 8-Feb 6 → test on Feb 7-13. Repeat. Performance is measured only on out-of-sample periods. If a parameter set looks great in-sample but fails out-of-sample, the walk-forward catches it.

**Alternatives considered:**
- **Simple multi-date backtest (legacy approach):** Rejected — information leakage.
- **Purged cross-validation (scikit-learn TimeSeriesSplit):** Considered but rejected — purging adjacent dates adds complexity for marginal gain at our sample sizes.
- **Combinatorial purged cross-validation (CPCV):** Rejected — overkill for a parameter space this small (5-8 numeric parameters × 3 traders).

**Consequences:**
- **Pro:** True out-of-sample validation — catches overfitting that simple backtests miss.
- **Pro:** Simulates real operation — the system always trains on the past, tests on the future.
- **Pro:** Built into the optimization pipeline — parameter sweeps are walk-forward by default.
- **Con:** More data-hungry — needs enough historical data for meaningful rolling windows.
- **Con:** Walk-forward is computationally more expensive (N train+test cycles vs. 1 train + N tests).
