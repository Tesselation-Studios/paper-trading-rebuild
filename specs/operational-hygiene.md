# Operational Hygiene

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-09

---

## Prompt Deployment Path

Traders run inside OpenClaw workspaces. The full deployment path:

```
paper-trading-prompts/{trader}/prompt.txt  (git source of truth)
  → openclaw-workspace-trader-{name}/AGENTS.md (output format + workflow)
  → openclaw-workspace-trader-{name}/skills/persona-strategy/SKILL.md (strategy)
```

**Invariant:** After any prompt change, verify the workspace files match. A prompt in git that the trader never reads is dead code.

**Updated**: 2026-07-14 (v3 — per-trader crons, mode flip, nightly maintenance)

---

## Cron Hygiene (v3)

### Per-Trader Tick Crons

Each trader has its OWN tick cron, offset by 1 minute to prevent concurrent Alpaca API calls:

| Cron | Schedule | Offset | Timeout |
|------|----------|--------|---------|
| `Stonks Tick (5-min)` | `*/5 9-16 * * 1-5` | 0 min | 180s |
| `Kairos Tick (5-min)` | `1-56/5 9-16 * * 1-5` | +1 min | 180s |
| `Aldridge Tick (5-min)` | `2-57/5 9-16 * * 1-5` | +2 min | 180s |

**Naming convention**: `{Trader} Tick (5-min)` — descriptive, lowercase trader in cron name.

**Invariants:**
- **No shared crons across traders.** One cron handling all 3 traders = cascading timeouts. Per SPEC invariant #15.
- **Offset schedule mandatory.** Three traders hitting Alpaca simultaneously = rate limits.
- **Cron timeout ≥ 180s.** Each tick involves sessions_send (wait for trader reply) + sessions_history (read reply) + possible exec (save decision). 180s is 3× the P99 for these session operations.
- **Inline prompts should be minimal.** "Step 1: sessions_send → Step 2: sessions_history → Step 3: exec save" format. No strategy instructions — those live in AGENTS.md.
- **ToolsAllow = [sessions_send, sessions_history, exec].** Tick runners don't need browser, data-bus, or messaging tools.

### Mode Flip Crons

| Cron | Schedule | Purpose |
|------|----------|---------|
| `Mode Flip: LIVE (market open)` | `30 9 * * 1-5` | Set all traders to LIVE |
| `Mode Flip: HISTORICAL (market close)` | `0 16 * * 1-5` | Set all traders to HISTORICAL |

**Invariant**: Mode flip must run BEFORE the first tick after open/close. LIVE flip at 9:30 AM ensures 9:35 tick runs in LIVE mode.

### Nightly Maintenance Cron

| Cron | Schedule | Timeout | Purpose |
|------|----------|---------|---------|
| `Nightly Pre-Market Maintenance` | `0 5 * * 1-5` | 7200s (2h) | Full system check + auto-fix |

Runs `nightly_check.py` (12-point check) + iterates on fixing failures. Has `exec`, `cron`, and `gateway` tools for self-healing. Reports readiness via Telegram announce on failure.

### Cron Failure Recovery

- **Tick cron timeout → auto-retry next cycle.** Missed ticks are acceptable (5-min gap). Do NOT trigger alerts for single missed ticks.
- **Mode flip cron failure → alert immediately.** Wrong mode = real money in wrong table or no trading at all.
- **Nightly maintenance failure → alert via Telegram announce.** System not ready for market day.
- **Consecutive tick errors ≥ 3** → diagnostic: check trader session health, Alpaca connectivity, data bus. Auto-fix if possible.

### Cron Monitoring

All crons report to `cron runs` history. Nightly maintenance reads cron failure counts and includes them in the readiness report. Stale/disabled crons are flagged as errors.

### Removed (v2)
- ~~`scripts/tick_prompt.py` pre-assembly~~ — v3 uses tool-based ticks, not pre-assembled prompts.
- ~~"No duplicate crons per trader"~~ — now enforced structurally: one cron per trader, no overlap possible.

## Intraday Monitoring (v3)

Trading runs autonomously via 5-min per-trader tick crons. Monitoring is diagnostic, not operational.

| Check | When | What |
|-------|------|------|
| Trader session health | Every 30 min | `sessions_history` — trader has recent messages, not stuck |
| Cron health | Every 30 min | All 3 tick crons enabled, not in error loop |
| Portfolio drift | Every hour | Positions match expectations, no unexpected sells |
| Pending orders | Every hour | No stale bracket legs or rogue sell orders |
| DB writes | Every hour | Recent decisions in `trading.decisions` |

**Self-healing protocol:**
1. Detect anomaly (e.g., 12 pending sell orders)
2. Cancel/clean up (cancel_all_orders)
3. Diagnose root cause (trader issued batch stop-losses due to bad tick context)
4. Fix or flag for maintenance

## After-Hours Format Validation (v3)

Replaced by `scripts/trader_check.py` — 7-point per-trader self-check that runs as part of the 5 AM nightly maintenance pipeline:

1. API keys found
2. Alpaca account accessible
3. Portfolio readable
4. Data bus healthy
5. Order API reachable (dry-run place_order.py)
6. Database accessible
7. Open orders clean (no stale pending orders)

**If any trader fails → system NOT READY → Telegram alert.** Fixed by the nightly maintenance agent (2-hour budget for auto-fix).

## Change Budget

Per trader, per month: maximum 5 parameter changes at the code level. The optimizer must choose which changes matter most.

## Rollback

Every accepted code change creates a rollback point. If live performance degrades > 10% within 10 days of merge, auto-revert and journal why.