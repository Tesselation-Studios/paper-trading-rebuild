#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# setup_tick_cron.sh — Install market-hours tick producer + orchestrator
#
# Installs cron entries that run every 5 min during US market hours
# (9:30-16:00 ET, Monday-Friday).
#
#   Tick producer:  :00, :05, :10, :15, :20, :25, :30, :35, :40, :45, :50, :55
#   Orchestrator:   :01, :06, :11, :16, :21, :26, :31, :36, :41, :46, :51, :56
#
# Each script has a built-in market-hours guard so it's safe to schedule
# outside those hours without worrying about cron minute precision.
#
# Usage:
#   ./scripts/setup_tick_cron.sh              # install cron entries
#   ./scripts/setup_tick_cron.sh --dry-run    # preview only, no install
#   ./scripts/setup_tick_cron.sh --remove     # uninstall tick cron entries
# ──────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CRON_MARKER="# paper-trading-replay-tick"
CRON_FILE="/tmp/paper-trading-crontab.txt"

# ── Helpers ───────────────────────────────────────────────────────────────

usage() {
    sed -n 's/^# \?//p' "$0"
    exit 0
}

install_cron() {
    local crontab_path
    crontab_path=$(command -v crontab) || {
        echo "ERROR: crontab not found. Install cron (apt install cron / brew install cron)."
        exit 1
    }

    # Backup existing crontab
    crontab -l 2>/dev/null > "$CRON_FILE" || true

    # Remove any previous entries with our marker
    sed -i "/${CRON_MARKER}/d" "$CRON_FILE"

    # ── Tick producer (@ :00, :05, :10, … up to :55) ──
    # Runs on weekdays 9-16; the script's own market-hours guard ensures
    # nothing happens before 09:30.
    cat >> "$CRON_FILE" <<EOF

# ── Pre-market prompt format validation (9:15 AM ET Mon-Fri) ${CRON_MARKER}
# Blocks tick production if prompts are broken — see scripts/pre_market_gate.py
15 9 * * 1-5 cd ${PROJECT_DIR} && python3 scripts/pre_market_gate.py >> ${PROJECT_DIR}/logs/pre_market_gate.log 2>&1 ${CRON_MARKER}

# ── Tick producer: every 5 min Mon-Fri 9-16 (script guards 9:30 start) ${CRON_MARKER}
0,5,10,15,20,25,30,35,40,45,50,55 9,10,11,12,13,14,15,16 * * 1-5 cd ${PROJECT_DIR} && python3 src/tick_producer.py >> ${PROJECT_DIR}/logs/tick_producer.log 2>&1 ${CRON_MARKER}

# ── Orchestrator: every 5 min at +1 offset Mon-Fri 9-16 ${CRON_MARKER}
1,6,11,16,21,26,31,36,41,46,51,56 9,10,11,12,13,14,15,16 * * 1-5 cd ${PROJECT_DIR} && python3 src/orchestrator.py >> ${PROJECT_DIR}/logs/orchestrator.log 2>&1 ${CRON_MARKER}
EOF

    # Install the new crontab
    "${crontab_path}" "$CRON_FILE"
    echo "✓ Installed pre-market gate + tick producer + orchestrator cron entries"
    echo "  Pre-market gate log → ${PROJECT_DIR}/logs/pre_market_gate.log"
    echo "  Logs → ${PROJECT_DIR}/logs/tick_producer.log"
    echo "  Logs → ${PROJECT_DIR}/logs/orchestrator.log"
}

remove_cron() {
    crontab -l 2>/dev/null > "$CRON_FILE" || true
    local before
    before=$(grep -c "${CRON_MARKER}" "$CRON_FILE" 2>/dev/null || echo 0)
    sed -i "/${CRON_MARKER}/d" "$CRON_FILE"
    crontab "$CRON_FILE"
    echo "✓ Removed ${before} tick cron entry/entries"
}

show_cron() {
    echo "── Current crontab ──"
    crontab -l 2>/dev/null || echo "(empty)"
    echo "────────────────────"
}

# ── Main ──────────────────────────────────────────────────────────────────

mkdir -p "${PROJECT_DIR}/logs"

case "${1:-install}" in
    install|--install)
        install_cron
        ;;
    --dry-run)
        echo "── DRY RUN ── Would install:"
        echo "  * 9:15 AM ET Mon-Fri: pre_market_gate.py (prompt format validation)"
        echo "  * Every 5 min Mon-Fri 9-16: tick_producer.py  (@ :00 offset)"
        echo "  * Every 5 min Mon-Fri 9-16: orchestrator.py   (@ :01 offset)"
        echo "  * Scripts guard against running before 09:30 ET"
        echo "  * Logs: ${PROJECT_DIR}/logs/{tick_producer,orchestrator}.log"
        ;;
    remove|--remove)
        remove_cron
        ;;
    show|--show)
        show_cron
        ;;
    *)
        usage
        ;;
esac

# Cleanup temp file
rm -f "$CRON_FILE"