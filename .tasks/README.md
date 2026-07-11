# `.tasks/` — Task Tracking & Orchestration

## Layout

```
.tasks/
├── README.md                        ← this file
├── P1-wire-learning-loop-pg.md      🔴 Blocked — requires Postgres migration
├── P1-build-meta-cog-loop.md        🔴 Depends on PG wiring
├── virtual-trader-implementation.md 📄 Spec reference — rotation/cull system
├── llm_replay_test.py               🛠️ LLM replay test (PG only)
├── nightly_replay_test.py           🛠️ Nightly variant sweep (PG only)
└── archive/                         ← Completed, stale, or superseded items
    ├── P0-deploy-virtual-runner.md   ✅ Done — virtual_runner deployed, wired, crons set
    ├── casper_sync_report.md         ✅ DB cutover alignment complete
    ├── orchestrator.py               ⏸️ Stale (last tick Jul 9) — superseded by direct work
    ├── orchestrator_state.json       ⏸️ Corresponding state file
    ├── create_key.py                 🛠️ One-time OpenRouter sub-key script
    ├── create_sub_key.py             🛠️ Same, different variant
    └── test_openrouter.py            🛠️ Key validation script
```

## Status Legend

| Mark | Meaning |
|------|---------|
| ✅ | Done |
| 🔴 | Blocked (note: by what) |
| 📄 | Reference / spec |
| 🛠️ | Utility / tool |
| ⏸️ | Stale / superseded |

## Active Items (Monday July 13 onward)

Traders start Monday. Current readiness:
- Trader Tick Cycle cron ✅ (5-min, Mon-Fri 9:30-16:00)
- Trader Health Check cron ✅
- Daily Learning Loop cron ✅ (16:30 ET, post-market analysis)
- Virtual runner wired into learning loop ✅
- Git state clean, pushed to main ✅

The P1 chain (PG wiring → meta-cog) is deferred until Postgres migration.