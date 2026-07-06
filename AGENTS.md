# Paper Trading Rebuild — Agent Instructions

> **META-SPEC**: [ai-project-system v0.22](https://github.com/openclaw/openclaw/blob/main/docs/ai-project-system/META-SPEC.md)
> **Repo**: `Tesselation-Studios/paper-trading-rebuild` (bot-owned — code pushes direct to main, spec changes via PR)
> **Board**: [GitHub Projects](https://github.com/users/casper-bot-wodinga/projects/2)
> **Last updated**: 2026-07-06

---

## 1. What This Project Is

Three LLM-powered traders (Kairos/momentum, Aldridge/value, Stonks/aggressive) running $10K paper portfolios on a distributed homelab. Two-speed learning: gradient descent on numeric signal parameters (intraday) + nightly prompt sweeps (overnight). This is the **rebuild** — cleaner, Postgres-native, walk-forward validated.

---

## 2. First Files To Read

1. **AGENTS.md** — you are here
2. **SPEC.md** — architecture, invariants, components
3. **DECISIONS.md** — why things were built this way
4. **fusion-review.md** — architecture critique with overfitting/Calmar/gradient-noise concerns
5. Sub-specs in `specs/` — `kmeans-regime.md`, `nightly-optimization-pipeline.md`

---

## 3. Spec Pipeline

```
META-SPEC → SPEC → CODE → VERIFY → OPERATE
```

- **Spec is always source of truth.** Code that doesn't match spec is wrong.
- **Spec changes → PR only.** Code changes → direct push to main on this bot-owned repo.
- Sub-specs in `specs/` when a component has >3 structural parts.

---

## 4. Branch Lifecycle

Create → work → merge → **DELETE** (local + remote). Branch naming: `<agent>/<what>`, `fix/<issue>`, `feat/<name>`. Never leave stale branches. Use conventional commit prefixes: `fix:`, `feat:`, `chore:`, `refactor:`.

---

## 5. Testing

```bash
python3 -m pytest tests/ -v   # 580+ tests on CI (ubuntu-latest, Python 3.12)
```

CI runs on every push to main. Excludes tests needing homelab access. The replay harness (`src/replay.py`) is the test bed for strategy changes.

---

## 6. Canvas Rules

Canvas (`canvas.wodinga.studio`) is for **dev work only**: builds, deploys, specs, CI results, architecture decisions. No trader updates, P&L snapshots, or live heartbeat logs. Use `canvas-push --board main` for milestones.

---

## 7. Quick Reference

```bash
# Run tests
python3 -m pytest tests/ -v

# Historical replay
python3 src/replay.py --date 2026-07-01

# Signal engine
python3 src/signals.py

# Walk-forward validation
python3 src/validation.py

# Create issue
gh issue create --repo Tesselation-Studios/paper-trading-rebuild --title "..." --label "bug"

# Clean stale branches
git fetch --prune && git branch --merged main | grep -v main | xargs git branch -d
```

---

## 8. Communication

- **Hermes ↔ Casper**: chat bridge (`~/projects/hermes-openclaw-bridge/`). Hermes is active in this repo — coordinate via bridge, don't assume.
- **GitHub Issues**: all bugs, features, tasks at `Tesselation-Studios/paper-trading-rebuild/issues`.
- **GitHub Projects**: [board](https://github.com/users/casper-bot-wodinga/projects/2) is the single source of truth for what's being worked on.