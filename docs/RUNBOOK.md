# Operational Runbook

> What to do when things break. Quick reference for the paper trading system.

## System Health Check

```bash
# Data bus alive?
curl -s http://192.168.1.41:5000/health | jq .status
# Expected: "ok"

# Postgres reachable?
PGPASSWORD=trade123 psql -h 192.168.1.179 -p 5433 -U trader -d trading -c "SELECT 1"
# Expected: 1

# Dashboard up?
curl -s -o /dev/null -w "%{http_code}" http://192.168.1.41:5002
# Expected: 200

# Gateway healthy?
openclaw gateway status
# Expected: running
```

---

## Incident: Gateway Down

**Symptoms:** Traders not responding, webhooks failing, `openclaw gateway status` shows "stopped" or errors.

**Check:**
```bash
# Is the process alive?
ps aux | grep openclaw | grep -v grep

# Check gateway logs
tail -100 ~/.openclaw/logs/gateway.log

# Systemd status (if managed)
systemctl --user status openclaw-gateway
```

**Fix:**
```bash
# Restart gateway
openclaw gateway restart

# If that fails, check for port conflicts
ss -tlnp | grep -E "18789|8644"

# If crash-looping, check the healthcheck watchdog
python3 /home/openclaw/projects/paper-trading-rebuild/scripts/gateway_healthcheck_watchdog.py --check
python3 /home/openclaw/projects/paper-trading-rebuild/scripts/gateway_healthcheck_watchdog.py --restart
```

**Watchdog auto-recovery:** `gateway_healthcheck_watchdog.py` runs on cron. It auto-restarts with cooldown and detects crash loops. State in `state/gateway_watchdog.json`.

---

## Incident: Postgres Down

**Symptoms:** Dashboard shows stale data, "connection refused" errors in logs, sync scripts failing.

**Check:**
```bash
# Is the Docker container running?
ssh openclaw@192.168.1.179 "docker ps | grep postgres"

# Can we connect?
PGPASSWORD=trade123 psql -h 192.168.1.179 -p 5433 -U trader -d trading -c "SELECT NOW()"
```

**Fix:**
```bash
# Restart the container
ssh openclaw@192.168.1.179 "docker restart paper-trading-postgres"

# Wait 10s then verify
sleep 10
PGPASSWORD=trade123 psql -h 192.168.1.179 -p 5433 -U trader -d trading -c "SELECT NOW()"

# If container is gone entirely
ssh openclaw@192.168.1.179 "cd /path/to/compose && docker compose up -d postgres"
```

**After recovery:** Run `scripts/sync_alpaca_positions.py --apply` to backfill any missed position snapshots.

---

## Incident: Trader Crash / Not Trading

**Symptoms:** No new journal entries, no trades for a trader, `agent_state.is_active = false`.

**Check:**
```bash
# Check trader state in PG
PGPASSWORD=trade123 psql -h 192.168.1.179 -p 5433 -U trader -d trading -c \
  "SELECT agent_id, is_active, last_heartbeat, last_trade, pnl FROM trading.agent_state"

# Check trader watchdog state
python3 /home/openclaw/projects/paper-trading-rebuild/scripts/trader_watchdog.py --check

# Check crash recovery state
cat /home/openclaw/projects/paper-trading-rebuild/state/crash-recovery.json
```

**Fix:**

**If paused (circuit breaker):**
```bash
# Check why
PGPASSWORD=trade123 psql -h 192.168.1.179 -p 5433 -U trader -d trading -c \
  "SELECT * FROM trading.risk_state WHERE agent_id = 'kairos'"

# If safe to unpause:
python3 /home/openclaw/projects/paper-trading-rebuild/scripts/d_state_watchdog.py --unpause kairos
```

**If crash-looping (5+ crashes in 24h):**
```bash
# Check crash counter
python3 /home/openclaw/projects/paper-trading-rebuild/scripts/d_state_watchdog.py --status

# Manual unpause (override threshold):
python3 /home/openclaw/projects/paper-trading-rebuild/scripts/d_state_watchdog.py --unpause kairos
```

**If agent process died:**
```bash
# The watchdog should auto-restart. Check:
python3 /home/openclaw/projects/paper-trading-rebuild/scripts/trader_watchdog.py --restart
```

---

## Incident: Data Bus Stale / Not Updating

**Symptoms:** `/quotes` returns old data, `stale: true` in responses, schedulers not running.

**Check:**
```bash
# Check health + scheduler state
curl -s http://192.168.1.41:5000/health | jq '.schedulers[] | {name, last_run, run_count}'

# Check data bus logs
tail -100 /home/openclaw/projects/paper-trading-rebuild/state/data_bus.log

# Is the data bus process running?
ps aux | grep data_bus | grep -v grep
```

**Fix:**
```bash
# Kill and restart data bus
pkill -f "data_bus.py"
cd /home/openclaw/projects/paper-trading-rebuild
nohup python3 src/data_bus.py > state/data_bus.log 2>&1 &

# Verify recovery
sleep 5
curl -s http://192.168.1.41:5000/health | jq .status
```

**If Alpaca rate-limited:** Check the fetch queue — it handles rate limiting automatically. Backoff should self-resolve within 1 minute.

---

## Incident: Dashboard Down

**Symptoms:** `http://192.168.1.41:5002` returns connection refused or 500.

**Check:**
```bash
curl -s -o /dev/null -w "%{http_code}" http://192.168.1.41:5002
```

**Fix:**
```bash
# Restart dashboard (check how it's launched)
cd /home/openclaw/projects/paper-trading-rebuild
nohup python3 src/canvas_dashboard.py > state/dashboard.log 2>&1 &
# or
nohup python3 src/pg_dashboard.py > state/pg_dashboard.log 2>&1 &
```

---

## Incident: Cron Jobs Not Running

**Symptoms:** No new tick events, sync scripts stale, nightly pipeline didn't run.

**Check:**
```bash
# List crontab
crontab -l | grep paper-trading-rebuild

# Check cron logs for recent runs
grep "paper-trading-rebuild" /var/log/syslog | tail -20

# Check lock files (stale locks block execution)
ls -la /tmp/*.lock | grep -E "tick-cron|sync-|learning-loop|nightly"
```

**Fix:**
```bash
# Remove stale lock files
rm -f /tmp/tick-cron.lock /tmp/sync-decisions.lock /tmp/sync-journals.lock /tmp/sync-positions.lock

# Verify crontab is active
systemctl status cron

# Run a tick manually to verify
cd /home/openclaw/projects/paper-trading-rebuild && python3 scripts/tick_cron.py --tick
```

---

## Incident: Pre-Market Gate Blocking Trades

**Symptoms:** No ticks being dispatched, `state/.pre_market_blocked` exists.

**Check:**
```bash
# Check if blocked
ls -la /home/openclaw/projects/paper-trading-rebuild/state/.pre_market_blocked

# Check gate log
tail -20 /home/openclaw/projects/paper-trading-rebuild/state/pre_market_gate.log

# Run validation manually
python3 /home/openclaw/projects/paper-trading-rebuild/scripts/validate_prompt_format.py
```

**Fix:**
```bash
# If validation fails, fix the prompts first
# Then remove the block file
rm /home/openclaw/projects/paper-trading-rebuild/state/.pre_market_blocked

# Re-run gate to confirm
python3 /home/openclaw/projects/paper-trading-rebuild/scripts/pre_market_gate.py
```

---

## Cron Schedule Reference

All times Eastern. All jobs use `flock` for mutual exclusion.

| Time | Job | Script |
|------|-----|--------|
| **Every 5 min, 9:30-16:00 ET** | Trading tick dispatch | `scripts/tick_cron.py --tick` |
| **Every 5 min, 9:30-16:00 ET** | Position sync (Alpaca → PG) | `src/sync_alpaca_positions.py --apply` |
| **Every 5 min, 9:30-16:00 ET** | Decision sync → PG | `scripts/sync_decisions_to_pg.py --apply` |
| **Every 5 min, 9:30-16:00 ET** | Journal sync → PG | `scripts/sync_journals_to_pg.py --apply` |
| **Every 5 min, 9:30-16:00 ET** | Agent state sync → PG | `scripts/sync_agents_to_pg.py` |
| **Every 30 min, off-hours** | Position/decision/journal sync (reduced freq) | Same scripts |
| **9:00 AM ET** | Pre-market tick (warm-up) | `scripts/tick_cron.py --tick` |
| **9:15 AM ET** | Pre-market format validation gate | `scripts/pre_market_gate.py` |
| **10:00 AM-3:00 PM ET, hourly** | Learning loop (dry-run + optimize) | `src/learning_loop.py --all --optimize --days 1` |
| **4:35 PM ET** | Nightly signal pipeline (no LLM) | `scripts/nightly_pipeline.py --skip-llm` |
| **4:35 PM ET** | EOD learning loop (optimize) | `src/learning_loop.py --all --optimize --days 1` |
| **4:45 PM ET** | EOD reflection | `src/reflection_cron.py --all` |
| **4:45 PM ET** | Auto-promote prompts | `scripts/auto_promote_prompts.py --apply` |
| **Saturday 8:00 AM ET** | Weekly strategy evolution | `src/strategy_evolution.py --all` |

## Log Locations

| Component | Log Path |
|-----------|----------|
| Data bus | `state/data_bus.log` |
| Tick dispatch | `state/tick_cron.log` |
| Position sync | `state/sync_positions.log` |
| Decision sync | `state/sync_decisions.log` |
| Journal sync | `state/sync_journals.log` |
| Nightly pipeline | `state/nightly_pipeline.log` |
| Learning loop (hourly) | `state/learning_loop_hourly.log` |
| Learning loop (EOD) | `state/learning_loop_eod.log` |
| Reflection cron | `state/reflection_cron.log` |
| Pre-market gate | `state/pre_market_gate.log` |
| Auto-promote | `state/auto_promote.log` |
| Strategy evolution | `state/strategy_evolution.log` |
| Gateway watchdog | `state/gateway_watchdog.log` |

## Quick Diagnostics

```bash
# One-liner health check
echo "=== Data Bus ===" && curl -s http://192.168.1.41:5000/health | jq '{status, uptime: .uptime_seconds, schedulers: [.schedulers[].name]}' && \
echo "=== PG ===" && PGPASSWORD=trade123 psql -h 192.168.1.179 -p 5433 -U trader -d trading -c "SELECT agent_id, is_active, pnl, last_heartbeat FROM trading.agent_state" && \
echo "=== Ticks Today ===" && PGPASSWORD=trade123 psql -h 192.168.1.179 -p 5433 -U trader -d trading -c "SELECT trader_id, count(*) as ticks FROM trading.tick_queue WHERE tick_time > CURRENT_DATE GROUP BY trader_id"

# Check for stuck locks
ls -la /tmp/*.lock 2>/dev/null

# Check disk space
df -h /home/openclaw

# Check memory
free -h
```

## Related Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — System overview
- [API.md](API.md) — Data bus endpoints
- [DB_SCHEMA.md](DB_SCHEMA.md) — Database schema
- [SPEC.md](../SPEC.md) — Master specification
