# Why We Keep Missing Bugs — Root Cause Analysis

## The Bugs We Found

| Bug | How It Happened | How It Was Found |
|-----|----------------|-----------------|
| **Silent import failure** — `AlpacaExecutor` not in Docker image, `except: pass` swallowed it | Dockerfile didn't COPY `src/execute.py`, but `leaderboard_api.py` imported it. Bare `except: pass` caught the ImportError silently. | Manually tracing code paths in the container after dashboard showed 0 positions |
| **Column name mismatch** — SQL used `qty` but DB column is `quantity` | Code was written against a different schema (the original SQLite schema had `qty`, Postgres schema has `quantity`). No schema validation. | psycopg2 threw `UndefinedColumn` at runtime |
| **Orphaned top-level JS** — `for (const pos of positions)` at depth 0 where `positions` is undefined | A `renderOptions()` function was partially removed — the function body was left behind as orphaned top-level code. | Manually inspecting the HTML after the dashboard showed "Connecting..." forever |
| **DNS misconfiguration** — Pi-hole (192.168.1.25) couldn't resolve Docker-internal hostnames | `dns:` was set globally in compose, overriding Docker's internal resolver. | Test suite returned 500s on 5/7 endpoints |
| **PGPORT mismatch** — Data-bus connected to 5433 (host-mapped) instead of 5432 (Docker internal) | `dual_writer` used `PGPORT` env var from the host, which was 5433. | Manually checking data-bus logs after portfolio snapshots weren't appearing |
| **Trader ID prefix mismatch** — `trader-kairos` vs `kairos` | `agent_state` stores prefixed names, `trader_positions` uses short names. No name normalization. | Portfolio snapshots showed `open_positions=0` |

## Root Causes

### 1. Silent Failure Pattern (Bugs #1, #3, #4, #5)
The most common pattern: **errors that are caught and silently absorbed**. Bare `except: pass` blocks, or `except Exception: pass` blocks, allow the program to continue running with broken state. The dashboard showed "Connecting..." forever, the API returned 0 positions, snapshots showed 0 — all with no error message.

**Why tests didn't catch this:** Unit tests test functions in isolation. They don't test Docker image contents, DNS resolution, or port mapping. The test suite was pure Python — it didn't validate the Docker build or runtime environment.

### 2. Schema Drift (Bug #2)
The SQLite schema and Postgres schema diverged over time. Column names (`qty` vs `quantity`), table names (`executed_trades` vs `trades`), and ID formats (`kairos` vs `trader-kairos`) all drifted independently.

**Why tests didn't catch this:** Tests were written against the code, not against the database. No schema validation tests existed that checked "does this SQL query actually match the real DB schema?"

### 3. Partial Refactoring (Bug #3)
When `renderOptions()` was removed from the HTML, only the function declaration was removed — the body remained as orphaned top-level code. This is a classic "partial delete" problem.

**Why tests didn't catch this:** No JS syntax validation or linting ran on the HTML. The dashboard was tested by looking at it, not by running a linter.

### 4. Environment-Specific Config (Bugs #4, #5, #6)
DNS, ports, and hostnames are environment-specific. The code worked on the developer's machine but not in Docker. The compose file was tuned for one environment and broke in another.

**Why tests didn't catch this:** No integration tests ran against the actual Docker deployment. Tests ran in isolation, not against the real stack.

## What We Need to Change

### 🛡️ Immediate Guardrails (Already Added)
1. **No silent excepts** — `test_bug_regression.py` checks for bare `except: pass` in production code
2. **Schema validation** — `test_guardrails.py` checks DB column names match code expectations
3. **Orphaned JS detection** — `test_guardrails.py` checks for top-level for-loops referencing undeclared variables
4. **Docker image integrity** — `test_bug_regression.py` checks that all imported modules are in the Dockerfile COPY
5. **DNS/port consistency** — `test_bug_regression.py` checks compose files for DNS overrides
6. **Trader ID normalization** — `test_bug_regression.py` checks for ID prefix handling

### 🔧 Systemic Fixes (Not Yet Implemented)
1. **Add a CI pipeline** — GitHub Actions that runs the guardrail tests on every PR
2. **Add Docker integration tests** — A test that builds the image, starts the container, and verifies the API works
3. **Add schema migration tests** — Before any SQL migration, validate against the actual DB schema
4. **Add JS linting to CI** — Run `node --check` and ESLint on all HTML/CSS/JS
5. **Add environment validation** — A startup script that checks DNS, port connectivity, and required env vars, failing fast with clear error messages

### 🧠 The Deeper Problem
The root cause is that **testing was manual and happened too late**. Bugs were found by humans looking at dashboards, not by automated checks. The fix is:

1. **Test the deployment, not just the code** — Docker images, compose files, DNS resolution, and port mappings are part of the system
2. **Fail fast, not silently** — Every `except` should log, metrics, or crash with a clear message. Silent failures hide bugs
3. **Validate schemas at startup** — If the code expects column `quantity`, check it exists at startup, not at first query
4. **Lint everything** — JS, HTML, Dockerfiles, compose files — all need automated validation