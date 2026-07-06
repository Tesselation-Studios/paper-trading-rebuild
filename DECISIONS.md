# Architectural Decision Records — Paper Trading Rebuild

This file documents key architectural decisions made during the rebuild. Each entry: date, status, context, decision, alternatives, consequences.

**Last updated**: 2026-07-06

> Paired with Issues on the [project board](https://github.com/users/casper-bot-wodinga/projects/2).
> Every major design decision gets an entry. Issues track tasks; this log tracks reasoning.

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
**Status:** Accepted (with constraints from Hermes review)

**Context:**
The legacy system used a rule-based regime classifier (4 hardcoded regimes) with only two features (momentum + volatility). The v4 HMM approach on the Mac GPU worker produced probabilistic regime assignments but required expensive training cycles and serial processing. Market regimes are inherently multi-dimensional — a "panic" regime has volume, breadth, correlation, and VIX characteristics that simple thresholds can't capture.

**Hermes pushback (2026-07-01):** Reject naive k-means. Require temporal features, fixed K with data-driven selection, cluster→label mapping, and A/B evaluation gate before replacing the current rule-based classifier.

**Decision:**
K-means clustering with engineered multi-dimensional features for regime detection — but NOT naive k-means. The implementation must satisfy Hermes's four requirements:

1. **Temporal features:** lagged t-1, t-5, t-20 to capture transitions
2. **Fixed K with data-driven selection:** silhouette score to determine optimal K, not arbitrary choice
3. **Cluster→label mapping:** human-readable regime labels (CHOPPY→cluster_3, TRENDING→cluster_1, etc.) with confidence scores
4. **A/B evaluation gate:** run both classifiers in shadow mode for 10+ days, compare P&L, only replace if k-means wins

K-means is fast, deterministic, and captures more market dimensions than the rule-based classifier — but sacrifices temporal dependency modeling that HMMs provide. The temporal features requirement partially mitigates this.

**Alternatives considered:**
- **HMM (v4 on Mac GPU):** Rejected for rebuild — training latency, serial processing, and complexity overhead. May return as a P3 upgrade.
- **Rule-based (keep legacy):** Rejected — the rebuild's reason for existing is to fix the two-feature blindness.
- **DBSCAN:** Considered for density-based clustering. Rejected — K-means' fixed K is simpler to tune.

**Consequences:**
- **Pro:** Multi-dimensional regime detection captures real market complexity.
- **Pro:** Fast training — cluster assignments computed in seconds, enabling nightly re-training.
- **Pro:** A/B gate ensures we don't regress from the working rule-based classifier.
- **Con:** No native temporal dependency modeling (mitigated by lagged features).
- **Con:** Fixed K — if the "true" number of regimes changes over time, K-means will force-fit. Upgrade path: GMM → HMM.

**Refs:** `specs/kmeans-regime.md`, SPEC §5

---

## 3. Two-Phase Validation (SignalEngine Filter → LLM Replay)

**Date:** 2026-07-06 (design from 2026-07-03, DP-5)
**Status:** Accepted

**Context:**
The legacy system's nightly pipeline tested prompts by replaying them directly through the LLM on historical data. Each replay tick cost real API tokens. With 3 traders × 5 prompt variants × 20 replay dates, nightly costs could exceed $50/day — for experiments that mostly confirmed "this prompt is worse than the current one."

**Decision:**
Two-phase validation pipeline:

1. **Phase 1 (SignalEngine filter):** Run parameter sweeps through the deterministic signal engine first. Only signal parameters — no LLM calls. Benchmark performance against the objective function. Reject any parameter set that underperforms the current baseline.

2. **Phase 2 (LLM replay):** Only the top ~3 surviving parameter sets from Phase 1 graduate to LLM replay. These are the promising candidates — worth the token cost.

**Agreement gate:** Both phases must pick the same winner for promotion. If Phase 1 says "variant A is best" but Phase 2 says "variant B is best," neither is promoted — we avoid promoting a signal-only improvement that the LLM can't capitalize on. Divergences are logged for post-hoc analysis.

**Budget gate:** Max 9 LLM runs per trader per night keeps costs bounded at ~$0.50-$1.50/night.

**Alternatives considered:**
- **Direct LLM sweep (legacy approach):** Rejected — too expensive, too slow.
- **No validation — just ship:** Rejected — defeats the "measurably improve" success criterion.
- **Signal-only optimization (no LLM phase):** Rejected — the LLM traders make the final decision. Signal parameters that look good in isolation may interact poorly with the trader's prompt.

**Consequences:**
- **Pro:** 90%+ cost reduction in nightly validation.
- **Pro:** Faster iteration — Phase 1 runs in seconds, enabling 100+ parameter combinations per night.
- **Pro:** Agreement gate prevents false positives where signal and LLM disagree.
- **Con:** Agreement gate may miss genuine improvements. Mitigated by logging divergences for manual review.
- **Con:** Two-phase adds complexity to the optimization pipeline.

**Refs:** SPEC §6, §14 invariants. DP-5 commit.

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
- **SQLite (keep legacy approach):** Rejected — doesn't scale to concurrent writes.
- **DuckDB:** Considered for analytical performance. Rejected — not an OLTP database.
- **SQLite + Litestream:** Considered for zero-ops durability. Rejected — doesn't solve concurrent writes.

**Consequences:**
- **Pro:** Concurrent write safety — replay, optimization, and live trading can all write simultaneously.
- **Pro:** Alembic migrations, connection pooling, proper backup/restore tooling.
- **Con:** Operational overhead — Postgres container to manage.
- **Con:** Migration still in progress. Legacy traders still write to SQLite.

---

## 5. Dashboard Phased Migration

**Date:** 2026-07-06
**Status:** Accepted

**Context:**
The legacy dashboard on port 5002 has been running stably for months. It reads from SQLite. The rebuild introduces Postgres, a new metrics engine, and a different data model. Replacing the dashboard in one cutover would be risky.

**Decision:**
Three-phase dashboard migration:

1. **Phase 1 (now):** Keep legacy dashboard running. Reads from legacy SQLite. No changes.
2. **Phase 2 (next):** Sync bridge — writes rebuild Postgres data back to legacy SQLite tables so the old dashboard stays current.
3. **Phase 3 (future):** Rebuild-native dashboard reading directly from Postgres. Legacy dashboard decommissioned.

**Alternatives considered:**
- **Big-bang replacement:** Rejected — risk of losing visibility during transition.
- **Dual dashboards indefinitely:** Rejected — confusion about which is authoritative.

**Consequences:**
- **Pro:** No loss of dashboard visibility during transition.
- **Con:** Sync bridge is temporary infrastructure that must be maintained until Phase 3.

---

## 6. Walk-Forward Validation Over Simple Multi-Date Backtest

**Date:** 2026-07-06
**Status:** Accepted

**Context:**
The legacy system's "validation" was running the same parameter set across multiple historical dates and averaging the results. This treats all dates as equally informative regardless of temporal ordering — leaking information and enabling overfitting.

**Decision:**
Walk-forward validation (`src/validation.py`): train on a rolling window, test on the subsequent out-of-sample period, advance the window, repeat. Performance is measured only on out-of-sample periods. If a parameter set looks great in-sample but fails out-of-sample, the walk-forward catches it.

**Alternatives considered:**
- **Simple multi-date backtest:** Rejected — information leakage.
- **Purged cross-validation:** Considered but rejected — overkill for current parameter space size.
- **CPCV:** Rejected — overkill for 5-8 numeric parameters × 3 traders.

**Consequences:**
- **Pro:** True out-of-sample validation. Catches overfitting that simple backtests miss.
- **Pro:** Simulates real operation — always trains on past, tests on future.
- **Con:** More data-hungry and computationally expensive than simple backtesting.

---

## 7. Fixed K=5 Over Auto-Detected K

**Date:** 2026-07-06
**Status:** Accepted

**Context:**
When designing the K-means regime detector, we faced the question of how to choose K — the number of clusters (regimes). Standard practice is the elbow method: plot within-cluster sum of squares vs. K, look for a "knee" in the curve. With 60 days of market data (~60-120 observations after featurization), the elbow method produces no meaningful knee — the curve is a smooth downward slope. Elbow method on small samples is statistical theater.

**Decision:**
Fix K=5. This is an informed guess grounded in how traders and portfolio managers typically describe market regimes: bull, bear, choppy/ranging, high volatility/panic, low volatility/quiet. Five clusters map directly to this mental model. The choice is pragmatic: K=5 is the minimum that covers the "normal" regimes (bull, bear, ranging) plus the edge regimes (panic, quiet).

If the system accumulates enough data over months (200+ trading days), we can revisit with a proper silhouette score analysis. Until then, K=5 is stable, interpretable, and good enough.

**Alternatives considered:**
- **Elbow method:** Rejected — produces no clear knee at 60 days of data. Theater, not statistics.
- **Silhouette score today:** Rejected — same data-limitation problem. Would select K=2 (minimum variance) or K=8 (overfitting to noise), neither useful.
- **Gap statistic:** Rejected — computationally expensive for nightly retraining, same data-limitation problem.
- **K=4 (match legacy):** Considered but rejected — the whole point of k-means is to capture more regimes than the rule-based classifier's 4 buckets.
- **K=6 or 7:** Considered — too many regimes for a system running 60 days of data. Sparse clusters would produce unreliable assignments.

**Consequences:**
- **Pro:** Stable, deterministic clusters every run. No "this Tuesday was cluster 3, last Tuesday was cluster 5" interpretation churn.
- **Pro:** Maps cleanly to trader mental model (bull/bear/choppy/panic/quiet).
- **Con:** If markets evolve new regime types, K=5 will force-fit them into existing clusters.
- **Con:** Mitigated by upgrade path to GMM (soft clustering, variable covariance) when more data accumulates.

**Refs:** `specs/kmeans-regime.md`

---

## 8. Shadow Mode A/B Gate Before Replacing Any Live Classifier

**Date:** 2026-07-06
**Status:** Accepted

**Context:**
The rebuild introduces a new regime classifier (K-means with lagged features, K=5) alongside the legacy rule-based classifier (4 buckets, 2 features). Every time a new component replaces a working one, there's risk of regression — the new classifier might produce plausible-looking labels that actually degrade trading performance. Silent regression is the worst failure mode: the system appears fine but performance degrades over weeks.

**Decision:**
No live classifier replacement without a shadow mode A/B gate. When a new classifier is proposed:

1. **Shadow mode:** Run both classifiers in parallel for 10+ trading days. New classifier produces labels but they're logged only — not consumed by the signal engine.
2. **Gate evaluation:** Compare P&L under the rule-based classifier vs. what would have happened under K-means labels. If K-means wins on the composite objective function (Calmar + Sortino + PF + Expectancy) for 10 consecutive days, the gate opens.
3. **Cutover:** Replace the rule-based classifier. Old classifier's labels continue logging alongside new one for 5 more days (rollback window).

This applies to ANY classifier replacement — not just regime detection. Feature importance model, anomaly detector, market state estimator — all go through the same A/B gate.

**Alternatives considered:**
- **Immediate replacement:** Rejected — too risky. If the classifier regresses, it could take weeks to notice (legacy Stonks pipeline stall went 3+ days unnoticed).
- **A/B without rollback window:** Rejected — if the new classifier has a rare but catastrophic failure mode, we need the 5-day overlap to detect and revert.
- **Holdout set validation only:** Rejected — offline validation on historical data can't capture live market dynamics (regime shifts, structural breaks). Shadow mode tests in live conditions.

**Consequences:**
- **Pro:** No silent regression. Every replacement is validated against a live baseline.
- **Pro:** The 5-day rollback window means even catastrophic failures are bounded to a few days of degraded performance.
- **Pro:** Produces a recorded dataset — we can analyze "old classifier vs. new classifier" performance post-hoc.
- **Con:** Adds operational complexity — two classifiers running in parallel, dual logging, gate evaluation cron.
- **Con:** Takes 10+ calendar days to promote anything. Slows iteration velocity on classifier improvements.

**Refs:** `specs/kmeans-regime.md`, DECISIONS #2

---

## 9. Learning Mode: All Traders Start Loose

**Date:** 2026-07-06
**Decision by:** Hermes
**Status:** Accepted

**Decision:** All three traders begin with looser restrictions, smaller-stock universes, and lower confidence thresholds. No explicit "LEARNING MODE" flag — the starting prompt IS the learning mode. The nightly learning loop tightens parameters over time.

**Why:** After 10+ days of operation, Kairos had 0 live buys, Stonks had 0 live buys, Aldridge had 1 sell. Zero data to learn from. Conservatism is the enemy of improvement — you can't optimize a system that never acts.

**Design philosophy:** The learning loop (parameter tuning, prompt evolution) should be the force that tightens restrictions, narrows watchlists, raises confidence thresholds, and reduces position sizes. The starting prompt just needs to be loose enough to generate 1-2 trades per session. Everything else should emerge from optimization.

**What changed per trader:**

| Trader | Before | After |
|--------|--------|-------|
| Kairos | $50-200 stocks, 0.55 confidence, HOLD-encouraged | $10-40 stocks, 0.30 confidence, minimum 1 BUY/SELL per session |
| Aldridge | Mega-cap only, "do nothing is underrated" | Mid-cap value included ($10-40), "do nothing" suspended |
| Stonks | Small-cap momentum, meme stocks | Same strategy but STARTING with cheap stocks, confidence 0.30 |

**Expected outcome:** 30-60 noisy trades in first 1-2 weeks → learning loop has data → parameters tighten naturally.

**Risks:** Max drawdown during learning phase could hit 5-10% (noisier trading). Mitigated by $10-40 stock range (losses are small in dollar terms) and existing circuit breaker at 10% DD.

**Refs:** `prompts/kairos.txt`, `prompts/aldridge.txt`, `prompts/stonks.txt`

---

## 10. Risk Veto: Decision Quality Gate (Aldridge BAC)

**Date:** 2026-07-06
**Decision by:** Hermes
**Status:** Accepted

**Decision:** Every BUY must have `thesis` ≥ 20 chars AND `signals_used` array with ≥ 1 entry. Missing either = trade rejected.

**Why:** Aldridge submitted `BUY BAC` with 0-character thesis and no signals_used. No rationale = no data = ungradeable. The learning loop can't score a trade with no thesis and no signals. This gate ensures every decision produces a data point the system can learn from.

**Where it lives:** Enforced in OpenClaw's decision validation layer and in the `OUTPUT FORMAT` section of every prompt. The error message:
```
[decision_quality]: Decision quality gate: BUY <TICKER> rejected — thesis
too short (<N> chars, need 20+). Write WHY you are buying <TICKER>: what
signal, what catalyst, what edge. | signals_used missing. Record which
signals triggered this BUY.
```

**Refs:** Issue #TBD on rebuild board.

---

## Template for future entries

```markdown
## YYYY-MM-DD — Short Title

**Decision:** What we decided.

**Why:** The reasoning, trade-offs considered, alternatives rejected.

**Trade-offs:** What we gain vs what we lose.

**Refs:** Issues, commits, spec sections.
```
