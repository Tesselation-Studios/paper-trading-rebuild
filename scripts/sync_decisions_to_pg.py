#!/usr/bin/env python3
"""
Sync trader decisions from OpenClaw workspace journal files to Postgres.

Reads journal markdown files from each trader's OpenClaw workspace,
extracts structured decisions (BUY/SELL/HOLD actions with ticker, quantity,
thesis, confidence, stop-loss) and writes them to trading.trader_decisions.

Also reads decision JSONL files (state/{trader}-decisions.jsonl) if they exist
for higher-precision structured data.

Usage:
    python3 scripts/sync_decisions_to_pg.py          # dry-run (print only)
    python3 scripts/sync_decisions_to_pg.py --apply  # actually insert
    python3 scripts/sync_decisions_to_pg.py --apply --force  # re-insert all
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

TRADERS = ["kairos", "aldridge", "stonks"]
AGENT_IDS = {t: f"trader-{t}" for t in TRADERS}

PG_DSN = os.getenv(
    "PG_DSN",
    "host=192.168.1.179 port=5433 dbname=trading user=trader",
)

WORKSPACE_ROOT = Path(os.getenv("OPENCLAW_HOME", "/home/openclaw")) / ".openclaw"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"


# ── Decision Parsing ────────────────────────────────────────────────────────

# Pattern: "## Tick 9:53 ET — BUY FUBO 3 @ $9.93"
SECTION_HEADER_DECISION = re.compile(
    r"## .*?[—-]\s+(BUY|SELL)\s+(\w+)\s+(\d+(?:\.\d+)?)\s*@\s*\$?(\d+\.?\d*)",
    re.IGNORECASE,
)

# Pattern: "Decision: BUY FUBO 3 @ $9.93"
DECISION_LINE = re.compile(
    r"(?:Decision|Action|Executed)\s*[:：]\s*(BUY|SELL)\s+(\w+)\s+(\d+(?:\.\d+)?)\s*@\s*\$?(\d+\.?\d*)",
    re.IGNORECASE,
)

# Pattern: "HOLD ALL" decisions
HOLD_LINE = re.compile(
    r"(?:Decision|Action|Verdict)\s*[:：]\s*HOLD\b",
    re.IGNORECASE,
)

# Pattern: "Order ID: xyz" or "Order": xyz
ORDER_ID_LINE = re.compile(r"(?:Order ID|Order)\s*[:：]\s*([\w-]+)")

# Pattern: "Status: filled" or "Status: ✅ filled"
STATUS_LINE = re.compile(r"Status\s*[:：]\s*[✅❌️]*\s*(\w+)", re.IGNORECASE)

# Pattern: "Confidence: 0.78" or "Conviction: 0.65"
CONFIDENCE_LINE = re.compile(
    r"(?:Confidence|Conviction)\s*[:：]\s*(\d+\.?\d*)", re.IGNORECASE
)

# Pattern: "Stop loss: $8.94" or "Stop: $14.88"
STOP_LOSS_LINE = re.compile(
    r"(?:Stop loss|Stop)\s*[:：]\s*\$?(\d+\.?\d*)", re.IGNORECASE
)

# Pattern: "Thesis: ..." or "Why FUBO?" or "Reason: ..."
THESIS_LINE = re.compile(
    r"(?:Thesis|Why|Reason|Plan)\s*[:：]\s*(.+)", re.IGNORECASE
)

# Pattern: "BUY 3 SNAP @ $4.82" in running text
INLINE_ORDER = re.compile(
    r"(BUY|SELL)\s+(\d+(?:\.\d+)?)\s+(\w+)\s*@\s*\$?(\d+\.?\d*)",
    re.IGNORECASE,
)


def parse_journal_for_decisions(path: Path) -> list[dict]:
    """Parse a journal file for structured decisions.

    Returns list of decision dicts: {agent_id, timestamp, action, ticker,
    quantity, stop_loss, confidence, thesis, source}
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    date_str = path.stem  # e.g. "2026-07-15"

    decisions = []
    current_section = ""
    current_lines = []
    in_decision_block = False

    for line in lines:
        if line.startswith("## "):
            # Process previous section
            if current_section and current_lines and in_decision_block:
                d = _extract_decision_from_section(
                    current_section, current_lines, date_str
                )
                if d:
                    decisions.append(d)

            current_section = line.strip("# ").strip()
            current_lines = []
            in_decision_block = bool(
                SECTION_HEADER_DECISION.match(line)
                or "DECISION" in line.upper()
                or "EXECUTED" in line.upper()
                or "BUY " in line.upper()
                or "SELL " in line.upper()
            )
        else:
            stripped = line.strip()
            if stripped and ("BUY" in stripped.upper() or "SELL" in stripped.upper()):
                in_decision_block = True
            if stripped:
                current_lines.append(stripped)

    # Process last section
    if current_section and current_lines and in_decision_block:
        d = _extract_decision_from_section(
            current_section, current_lines, date_str
        )
        if d:
            decisions.append(d)

    return decisions


def _extract_time_from_section(section: str, date_str: str) -> str:
    """Extract timestamp from section header."""
    m = re.search(r"(\d{1,2}):(\d{2})", section)
    if m:
        return f"{date_str}T{m.group(1).zfill(2)}:{m.group(2)}:00"
    # Try "X:XX AM/PM" format
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", section, re.IGNORECASE)
    if m:
        hour = int(m.group(1))
        minute = m.group(2)
        ampm = m.group(3).upper()
        if ampm == "PM" and hour < 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        return f"{date_str}T{hour:02d}:{minute}:00"
    return f"{date_str}T12:00:00"


def _extract_decision_from_section(
    section: str, lines: list[str], date_str: str
) -> dict | None:
    """Extract a structured decision from a journal section."""
    section_text = "\n".join(lines)
    timestamp = _extract_time_from_section(section, date_str)

    # Try section header format first (most reliable)
    m = SECTION_HEADER_DECISION.match(section)
    if m:
        action = m.group(1).upper()
        ticker = m.group(2).upper()
        qty = float(m.group(3))
        return _build_decision(action, ticker, qty, lines, section_text, timestamp, section)

    # Try inline decision line
    m = DECISION_LINE.search(section_text)
    if m:
        action = m.group(1).upper()
        ticker = m.group(2).upper()
        qty = float(m.group(3))
        return _build_decision(action, ticker, qty, lines, section_text, timestamp, section)

    # Try inline order format (e.g., "BUY 3 SNAP @ $4.82")
    m = INLINE_ORDER.search(section_text)
    if m:
        action = m.group(1).upper()
        qty = float(m.group(2))
        ticker = m.group(3).upper()
        return _build_decision(action, ticker, qty, lines, section_text, timestamp, section)

    # Check for HOLD decision
    if HOLD_LINE.search(section_text) or "HOLD" in section.upper():
        return _build_decision("HOLD", None, None, lines, section_text, timestamp, section)

    return None


def _build_decision(
    action: str,
    ticker: str | None,
    quantity: float | None,
    lines: list[str],
    section_text: str,
    timestamp: str,
    section_header: str = "",
) -> dict:
    """Build a decision dict, extracting remaining fields from context."""
    section_text_joined = "\n".join(lines)

    # Extract stop loss
    stop_loss = None
    m = STOP_LOSS_LINE.search(section_text_joined)
    if m:
        stop_loss = float(m.group(1))

    # Extract confidence
    confidence = None
    m = CONFIDENCE_LINE.search(section_text_joined)
    if m:
        val = float(m.group(1))
        if val > 1:
            val = val / 100  # e.g. 72/100 → 0.72
        confidence = round(val, 2)

    # Extract thesis (first matching line)
    thesis = None
    m = THESIS_LINE.search(section_text_joined)
    if m:
        thesis = m.group(1).strip()[:500]

    # Extract order status
    status = None
    m = STATUS_LINE.search(section_text_joined)
    if m:
        status = m.group(1).lower()

    # Extract order ID
    order_id = None
    m = ORDER_ID_LINE.search(section_text_joined)
    if m:
        order_id = m.group(1)

    # Mood from section header
    mood = _extract_mood(section_header)

    return {
        "action": action,
        "ticker": ticker.upper() if ticker else None,
        "quantity": quantity,
        "stop_loss": stop_loss,
        "confidence": confidence,
        "thesis": thesis,
        "order_id": order_id,
        "order_status": status,
        "timestamp": timestamp,
        "mood": mood,
        "raw_entry": section_text[:2000],
    }


def _extract_mood(text: str) -> str:
    """Extract a mood keyword from text."""
    moods = [
        "bullish", "bearish", "neutral", "cautious", "confident", "optimistic",
        "pessimistic", "anxious", "excited", "patient", "aggressive", "defensive",
        "hopeful", "worried", "greedy", "fearful", "manic", "chill",
    ]
    for mood in moods:
        if mood in text.lower():
            return mood
    return ""


def read_decision_jsonl(trader: str) -> list[dict]:
    """Read structured decisions from JSONL file if it exists."""
    path = STATE_DIR / f"{trader}-decisions.jsonl"
    if not path.exists():
        return []
    decisions = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    decisions.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return decisions


# ── Database ─────────────────────────────────────────────────────────────────


def get_db():
    """Get a Postgres connection."""
    import psycopg2 as _psycopg2
    conn = _psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def get_latest_decision_ts(conn, agent_id: str) -> str | None:
    """Get the most recent decision timestamp in the DB for this agent."""
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(timestamp) FROM trading.trader_decisions WHERE agent_id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        cur.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def insert_decisions(
    conn, agent_id: str, decisions: list[dict], latest_ts: str | None, force: bool = False
) -> int:
    """Insert decisions into trading.trader_decisions, skipping duplicates."""
    if not decisions:
        return 0

    cur = conn.cursor()
    inserted = 0
    for d in decisions:
        ts = d["timestamp"]

        # Skip if we already have this timestamp (or earlier) in the DB
        if not force and latest_ts and ts <= latest_ts:
            continue

        # Map action to DB format
        action = d.get("action", "HOLD")

        try:
            cur.execute(
                """INSERT INTO trading.trader_decisions
                   (agent_id, timestamp, action, ticker, quantity, stop_loss,
                    confidence, thesis, source)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'journal_sync')
                   ON CONFLICT DO NOTHING""",
                (
                    agent_id,
                    ts,
                    action,
                    d.get("ticker"),
                    d.get("quantity"),
                    d.get("stop_loss"),
                    d.get("confidence"),
                    d.get("thesis"),
                ),
            )
            inserted += 1
        except Exception as e:
            print(f"  [WARN] insert failed for {agent_id} @ {ts}: {e}", file=sys.stderr)

    conn.commit()
    cur.close()
    return inserted


def insert_journal_entry(
    conn, agent_id: str, d: dict
) -> bool:
    """Insert a single journal entry into trader_journal."""
    if not d.get("raw_entry"):
        return False

    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO trading.trader_journal
               (agent_id, timestamp, mood, entry, confidence, source)
               VALUES (%s, %s, %s, %s, %s, 'decision_sync')
               ON CONFLICT DO NOTHING""",
            (
                agent_id,
                d["timestamp"],
                d.get("mood", ""),
                d["raw_entry"],
                d.get("confidence"),
            ),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"  [WARN] journal insert failed for {agent_id}: {e}", file=sys.stderr)
        return False


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Sync trader decisions from journal files to Postgres"
    )
    parser.add_argument("--apply", action="store_true", help="Actually insert")
    parser.add_argument("--force", action="store_true", help="Re-insert all decisions")
    args = parser.parse_args()

    try:
        import psycopg2 as _psycopg2  # noqa: F401
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)

    if not WORKSPACE_ROOT.exists():
        print(f"ERROR: Workspace root not found: {WORKSPACE_ROOT}", file=sys.stderr)
        sys.exit(1)

    conn = get_db()
    total_decisions = 0
    total_journal = 0

    for trader in TRADERS:
        agent_id = AGENT_IDS[trader]
        workspace = WORKSPACE_ROOT / f"workspace-trader-{trader}"
        journal_dir = workspace / "journal"

        if not workspace.exists():
            print(f"[SKIP] {trader} — workspace not found: {workspace}")
            continue

        print(f"\n── {trader} ({agent_id}) ──")

        latest_ts = get_latest_decision_ts(conn, agent_id)
        if latest_ts:
            print(f"  Latest DB decision: {latest_ts}")
        else:
            print(f"  Latest DB decision: (none)")

        # 1. Read decisions from JSONL files (highest precision)
        jsonl_decisions = read_decision_jsonl(trader)

        # 2. Read decisions from journal markdown files
        journal_decisions = []
        if journal_dir.exists():
            journal_files = sorted(journal_dir.glob("*.md"))
            print(f"  Journal files: {len(journal_files)}")
            for jf in journal_files:
                decisions = parse_journal_for_decisions(jf)
                journal_decisions.extend(decisions)
                if decisions:
                    print(f"    Found {len(decisions)} decisions in {jf.name}")
        
        # Also check agent-level journal.md (primary journal on .41)
        agent_journal = WORKSPACE_ROOT / f"agents/trader-{trader}/journal.md"
        if agent_journal.exists():
            print(f"  Agent journal: {agent_journal}")
            decisions = parse_journal_for_decisions(agent_journal)
            if decisions:
                print(f"    Found {len(decisions)} decisions in agent journal.md")
                journal_decisions.extend(decisions)

        # Merge: JSONL decisions take precedence, journal decisions fill gaps
        all_decisions = jsonl_decisions + journal_decisions
        # Deduplicate by timestamp
        seen_timestamps = set()
        unique_decisions = []
        for d in all_decisions:
            ts = d.get("timestamp", "")
            if ts not in seen_timestamps:
                seen_timestamps.add(ts)
                unique_decisions.append(d)

        print(f"  Total unique decisions: {len(unique_decisions)}")
        if unique_decisions:
            print(f"  Newest: {unique_decisions[-1].get('timestamp', '?')}")

        if args.apply:
            n = insert_decisions(conn, agent_id, unique_decisions, latest_ts, args.force)
            if n > 0:
                print(f"  Inserted {n} new decisions")
                total_decisions += n

            # Also insert journal entries from decisions
            for d in unique_decisions:
                if d.get("raw_entry"):
                    insert_journal_entry(conn, agent_id, d)
                    total_journal += 1
            if total_journal:
                print(f"  Synced {total_journal} journal entries")
        else:
            new_decisions = [
                d for d in unique_decisions
                if args.force or not latest_ts or d.get("timestamp", "") > latest_ts
            ]
            if new_decisions:
                print(f"  Would insert {len(new_decisions)} new decisions")
                for d in new_decisions[:5]:
                    print(
                        f"    {d.get('timestamp', '?'):25s} "
                        f"{(d.get('action') or '?'):5s} "
                        f"{(d.get('ticker') or '?'):6s} "
                        f"{d.get('quantity') or '?':>4}"
                    )

    conn.close()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n=== {mode}: {total_decisions} decisions, {total_journal} journal entries ===")

    if not args.apply:
        print("\nRun with --apply to actually insert.")


if __name__ == "__main__":
    main()