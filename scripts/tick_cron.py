#!/usr/bin/env python3
"""Tick Cron — produce ticks AND dispatch agentTurn to all paper traders.

Dual-mode operation:
  1. Tick producer mode (no flags):      fetch quotes from data bus, enqueue to DB
  2. Full tick + dispatch mode (--tick):  produce ticks, then dispatch each trader
     agent via openclaw agent --agent <id> --message-file <prompt>

Usage:
    python3 scripts/tick_cron.py                          # enqueue ticks only
    python3 scripts/tick_cron.py --tick                    # full tick + dispatch
    python3 scripts/tick_cron.py --tick --dry-run          # print what would happen
    python3 scripts/tick_cron.py --tick --agent kairos     # single agent only
    python3 scripts/tick_cron.py --data-bus URL            # override data bus
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import uuid
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

log = logging.getLogger("tick_cron")

# ── Defaults ──────────────────────────────────────────────────────────────

DB_DSN = "postgresql://trader:@192.168.1.179:5433/trading"
DATA_BUS_URL = "http://localhost:5000/tick-snapshot"

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
STATE_DIR = PROJECT_DIR / "state"
PRE_MARKET_SENTINEL = STATE_DIR / ".pre_market_blocked"

# Paper traders to dispatch automatically (cron still fires every 5 min, but
# dispatches nothing by default now — see below). Use --agent <id> to dispatch
# a specific trader manually for testing.
#
# 2026-07-21: emptied deliberately.
#   - trader-stonks: superseded by the openclaw-native cron "stonks-tick"
#     (id 1eb7e710-...), which spawns Stan via sessions_spawn with a clean
#     task description. This script's own assemble_prompt()/tick_prompt.py
#     pre-templates a stale v3-era prompt (hardcoded 10-stock universe
#     including INTC, "no tools, JSON only" framing) that was intermittently
#     hijacking real ticks — Stan fixated on INTC for 8 straight ticks
#     11:10-11:45 ET today instead of checking his real 14-position
#     portfolio. Running both dispatchers on the same */5 9-15 schedule was
#     also racing and causing "isolated agent setup timed out" failures.
#   - trader-kairos / trader-aldridge: retired per Raf's decision — they were
#     still trading unsupervised on stale pre-v4 logic despite being
#     documented as retired in paper-trading-agents/AGENTS.md (2026-07-18).
#     That doc update never actually stopped dispatch until now.
PAPER_TRADERS = []

# Market hours (ET)
MARKET_OPEN = 9.5   # 09:30 ET
MARKET_CLOSE = 16.0  # 16:00 ET


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Tick cron — produce ticks and dispatch paper traders"
    )
    p.add_argument("--db-dsn", default=DB_DSN, help="Postgres DSN")
    p.add_argument(
        "--data-bus", default=DATA_BUS_URL, help="Data bus URL for market quotes"
    )
    p.add_argument(
        "--tick",
        action="store_true",
        help="Full tick: produce ticks AND dispatch paper traders",
    )
    p.add_argument(
        "--agent",
        default=None,
        help="Single agent to dispatch (e.g. kairos). Used with --tick.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print ticks + dispatch plan without executing",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return p.parse_args(argv)


def is_market_hours() -> bool:
    """Check if current time is within US market hours (Mon-Fri 09:30-16:00 ET)."""
    now = datetime.now(timezone.utc)
    # Approximate ET: UTC-4 (EDT)
    et_hour = now.hour - 4
    if et_hour < 0:
        et_hour += 24
    et_minute = now.minute
    et_decimal = et_hour + et_minute / 60.0

    # Weekday check: Monday=0, Sunday=6
    if now.weekday() >= 5:
        return False

    return MARKET_OPEN <= et_decimal < MARKET_CLOSE


def fetch_quotes(url: str) -> List[Dict[str, Any]]:
    log.info("Fetching quotes from %s", url)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:
        log.error("Failed to fetch quotes from %s: %s", url, exc)
        return []

    raw = body if isinstance(body, list) else body.get("quotes", body.get("data", {}))
    now = datetime.now(timezone.utc)
    quotes: List[Dict[str, Any]] = []
    # /tick-snapshot returns {symbol: {price, volume, ...}}
    # /quotes returns [{symbol:..., price:..., ...}]
    if isinstance(raw, dict):
        for symbol, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            quotes.append({
                "tick_id": str(uuid.uuid4()),
                "symbol": symbol,
                "price": float(entry.get("price", entry.get("close", entry.get("p", 0.0)))),
                "volume": int(entry.get("volume", entry.get("v", 0))),
                "timestamp": entry.get("timestamp", now.isoformat()),
                "source": "data_bus",
            })
    return quotes


def ensure_tick_queue_table(conn) -> None:
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trading.tick_queue (
            id SERIAL PRIMARY KEY,
            tick_data JSONB NOT NULL,
            status TEXT DEFAULT 'pending',
            error TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            processed_at TIMESTAMPTZ
        )
    """)
    conn.commit()
    cur.close()


def insert_ticks(conn, ticks: List[Dict[str, Any]]) -> int:
    cur = conn.cursor()
    rows = [(t.get("tick_id", str(uuid.uuid4())), json.dumps(t)) for t in ticks]
    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO trading.tick_queue (tick_id, tick_data) VALUES %s",
        rows,
        template="(%s, %s::jsonb)",
    )
    n = cur.rowcount
    conn.commit()
    cur.close()
    return n


def check_pre_market_gate() -> tuple[bool, str]:
    if PRE_MARKET_SENTINEL.exists():
        try:
            reason = PRE_MARKET_SENTINEL.read_text().strip()
        except Exception:
            reason = "Unknown validation failure"
        return False, reason
    return True, ""


def dispatch_agent(agent_id: str, dry_run: bool = False) -> Dict[str, Any]:
    """Dispatch a single paper trader via openclaw agent.

    Steps:
      1. Run tick_prompt.py --trader <short_name> to get the prompt
      2. Write prompt to a temp file
      3. Run: openclaw agent --agent <agent_id> --message-file <file>
      4. Return result
    """
    short_name = agent_id.replace("trader-", "", 1)
    prompt_script = SCRIPTS_DIR / "tick_prompt.py"
    prompt_file = STATE_DIR / f".tick_prompt_{short_name}.json"

    log.info("Generating prompt for %s via tick_prompt.py", agent_id)

    if dry_run:
        log.info("[DRY RUN] Would run: python3 %s --trader %s", prompt_script, short_name)
        log.info("[DRY RUN] Would run: openclaw agent --agent %s --message-file %s", agent_id, prompt_file)
        return {"agent": agent_id, "status": "dry_run", "decision": None}

    try:
        result = subprocess.run(
            [sys.executable, str(prompt_script), "--trader", short_name],
            capture_output=True, text=True, timeout=60,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or f"exit code {result.returncode}"
            log.error("tick_prompt.py for %s failed: %s", agent_id, error_msg)
            return {"agent": agent_id, "status": "error", "error": error_msg}

        prompt_text = result.stdout.strip()
        if not prompt_text:
            log.warning("tick_prompt.py for %s produced empty output", agent_id)
            return {"agent": agent_id, "status": "error", "error": "empty prompt"}

        prompt_file.write_text(prompt_text)
        log.info("Prompt written to %s (%d bytes)", prompt_file, len(prompt_text))

    except subprocess.TimeoutExpired:
        log.error("tick_prompt.py for %s timed out after 60s", agent_id)
        return {"agent": agent_id, "status": "error", "error": "timeout"}
    except Exception as exc:
        log.error("tick_prompt.py for %s raised: %s", agent_id, exc)
        return {"agent": agent_id, "status": "error", "error": str(exc)}

    # Step 2: Dispatch via openclaw agent
    openclaw_bin = os.getenv("OPENCLAW_BIN", "/home/openclaw/.npm-global/bin/openclaw")
    log.info("Dispatching %s via openclaw agent", agent_id)

    try:
        result = subprocess.run(
            [openclaw_bin, "agent", "--agent", agent_id, "--message-file", str(prompt_file)],
            capture_output=True, text=True, timeout=300,
            cwd=str(PROJECT_DIR),
        )
        if prompt_file.exists():
            prompt_file.unlink()

        if result.returncode != 0:
            error_msg = result.stderr.strip() or f"exit code {result.returncode}"
            log.error("openclaw agent for %s failed: %s", agent_id, error_msg)
            return {"agent": agent_id, "status": "error", "error": error_msg}

        log.info("Agent %s dispatched successfully", agent_id)
        return {"agent": agent_id, "status": "success", "output": result.stdout.strip()[:500]}

    except subprocess.TimeoutExpired:
        log.error("openclaw agent for %s timed out after 300s", agent_id)
        if prompt_file.exists():
            prompt_file.unlink()
        return {"agent": agent_id, "status": "error", "error": "timeout"}
    except Exception as exc:
        if prompt_file.exists():
            prompt_file.unlink()
        log.error("openclaw agent for %s raised: %s", agent_id, exc)
        return {"agent": agent_id, "status": "error", "error": str(exc)}


def main() -> None:
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Market hours guard ──
    if not is_market_hours():
        msg = "Market is closed. Skipping tick dispatch."
        log.info(msg)
        print("Market closed — no tick dispatched.")
        return

    # ── Pre-market validation gate ──
    if not args.dry_run:
        gate_ok, gate_reason = check_pre_market_gate()
        if not gate_ok:
            log.error(
                "PRE-MARKET GATE BLOCKED -- refusing to enqueue ticks: %s",
                gate_reason,
            )
            log.error(
                "Run 'python3 scripts/validate_prompt_format.py' to diagnose. "
                "Remove state/.pre_market_blocked to override."
            )
            sys.exit(1)

    # ── Step 1: Fetch and enqueue ticks ──
    ticks = fetch_quotes(args.data_bus)
    if not ticks:
        log.warning("No ticks fetched from data bus; nothing to enqueue.")
    else:
        log.info("Fetched %d tick(s) from data bus", len(ticks))

        if args.dry_run:
            print("-- DRY RUN -- would insert these ticks --")
            for t in ticks:
                print(json.dumps(t, indent=2, default=str))
        else:
            conn = psycopg2.connect(args.db_dsn)
            try:
                ensure_tick_queue_table(conn)
                n = insert_ticks(conn, ticks)
                log.info("Enqueued %d tick(s) into trading.tick_queue", n)
            finally:
                conn.close()

    # ── Step 2: Dispatch agents (only with --tick) ──
    if not args.tick:
        if args.dry_run:
            print("-- DRY RUN -- tick production done (no dispatch without --tick) --")
        return

    # Determine which agents to dispatch
    if args.agent:
        agent_id = args.agent if args.agent.startswith("trader-") else f"trader-{args.agent}"
        agents = [agent_id]
    else:
        agents = PAPER_TRADERS

    log.info("Dispatching %d paper trader(s): %s", len(agents), agents)

    if args.dry_run:
        print("-- DRY RUN -- would dispatch these agents --")
        for a in agents:
            dispatch_agent(a, dry_run=True)
        return

    # Dispatch each agent sequentially
    results = []
    for agent_id in agents:
        log.info("=" * 60)
        log.info("Dispatching %s...", agent_id)
        result = dispatch_agent(agent_id)
        results.append(result)
        status_icon = chr(10003) if result["status"] == "success" else chr(10007)
        log.info("%s %s: %s", status_icon, agent_id, result["status"])
        if len(agents) > 1 and result["status"] == "success":
            time.sleep(2)

    success_count = sum(1 for r in results if r["status"] == "success")
    error_count = sum(1 for r in results if r["status"] == "error")
    log.info("=" * 60)
    log.info("Tick dispatch complete: %d success, %d error", success_count, error_count)
    print(f"Tick dispatch complete: {success_count} succeeded, {error_count} failed")


if __name__ == "__main__":
    main()
