#!/usr/bin/env bash
#
# sync_prompts.sh — Push canonical prompt templates to trading-agent-prompts repo
#
# During migration from trading-agent-prompts/ → prompts/ directory, this script
# keeps the legacy repo in sync as a mirror. Once migration is complete and all
# consumers point to paper-trading-rebuild/prompts/, the trading-agent-prompts
# repo can be archived.
#
# Usage:
#   scripts/sync_prompts.sh              # Sync all traders
#   scripts/sync_prompts.sh --dry-run    # Show what would change
#   scripts/sync_prompts.sh --trader kairos  # Sync single trader
#
# Requirements:
#   - Both repos must be cloned locally
#   - Set TRADING_AGENT_PROMPTS_PATH env var or default is used

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROMPTS_DIR="${REPO_ROOT}/prompts"

# The legacy repo path
LEGACY_REPO="${TRADING_AGENT_PROMPTS_PATH:-$HOME/projects/trading-agent-prompts}"

DRY_RUN=false
TARGET_TRADER=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Push canonical prompt templates from prompts/ to the trading-agent-prompts repo.

OPTIONS:
  --trader NAME     Sync only a single trader (kairos, aldridge, stonks)
  --dry-run         Show what would be synced without changing files
  --legacy PATH     Override trading-agent-prompts repo path
                    (default: $LEGACY_REPO)
  -h, --help        Show this help

ENVIRONMENT:
  TRADING_AGENT_PROMPTS_PATH   Path to trading-agent-prompts repo
EOF
    exit 0
}

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --trader)
            TARGET_TRADER="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --legacy)
            LEGACY_REPO="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown option: $1"
            usage
            ;;
    esac
done

# Validate prompts/ directory
if [[ ! -d "$PROMPTS_DIR" ]]; then
    echo "ERROR: prompts/ directory not found at $PROMPTS_DIR"
    exit 1
fi

# Validate legacy repo
if [[ ! -d "$LEGACY_REPO" ]]; then
    echo "ERROR: Legacy repo not found at $LEGACY_REPO"
    echo "  Set TRADING_AGENT_PROMPTS_PATH or use --legacy PATH"
    exit 1
fi

if [[ ! -d "$LEGACY_REPO/.git" ]]; then
    echo "ERROR: $LEGACY_REPO is not a git repository"
    exit 1
fi

echo "=== Prompt Template Sync ==="
echo "  Source:      $PROMPTS_DIR"
echo "  Destination: $LEGACY_REPO"
echo "  Mode:        $([ "$DRY_RUN" = true ] && echo 'DRY RUN' || echo 'LIVE')"
echo ""

TRADERS=("kairos" "aldridge" "stonks")
if [[ -n "$TARGET_TRADER" ]]; then
    TRADERS=("$TARGET_TRADER")
fi

CHANGES=0

for trader in "${TRADERS[@]}"; do
    SRC="$PROMPTS_DIR/${trader}.txt"
    DST_DIR="$LEGACY_REPO/${trader}"
    DST="$DST_DIR/daily_tick.md"

    if [[ ! -f "$SRC" ]]; then
        echo "WARNING: Source template not found: $SRC — skipping $trader"
        continue
    fi

    if $DRY_RUN; then
        if [[ -f "$DST" ]]; then
            if ! diff -q "$SRC" "$DST" &>/dev/null; then
                echo "[DRY RUN] $trader: would update daily_tick.md"
                CHANGES=$((CHANGES + 1))
            else
                echo "[DRY RUN] $trader: up to date"
            fi
        else
            echo "[DRY RUN] $trader: would create daily_tick.md (not found)"
            CHANGES=$((CHANGES + 1))
        fi
    else
        mkdir -p "$DST_DIR"
        cp "$SRC" "$DST"
        echo "  ✓ $trader: synced → $DST"
        CHANGES=$((CHANGES + 1))
    fi
done

echo ""
if $DRY_RUN; then
    echo "Dry run complete. $CHANGES trader(s) would be updated."
else
    echo "Sync complete. $CHANGES trader(s) updated."
    echo ""
    echo "Next steps:"
    echo "  cd $LEGACY_REPO"
    echo "  git add -A && git commit -m 'sync: prompt templates from paper-trading-rebuild/prompts/'"
    echo "  git push"
fi
