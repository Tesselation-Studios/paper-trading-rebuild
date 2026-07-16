#!/bin/bash
# deploy.sh — Pull latest images from GHCR and restart docker-compose stack
# Usage: ./scripts/deploy.sh [TAG]
#   TAG defaults to "latest". Use a commit SHA to rollback to a specific version.
#
# Required env vars (set these in .env or export):
#   GHCR_USER  — GitHub username for GHCR login
#   GHCR_TOKEN — GitHub personal access token with read:packages scope
#
# Setup (one-time):
#   echo $GHCR_TOKEN | docker login ghcr.io -u $GHCR_USER --password-stdin

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TAG="${1:-latest}"
REGISTRY="ghcr.io"
IMAGE="tesselation-studios/paper-trading-rebuild"

# ── Colors ─────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[deploy]${NC} $1"; }
warn() { echo -e "${YELLOW}[deploy]${NC} $1"; }
err()  { echo -e "${RED}[deploy]${NC} $1"; }

# ── Pre-flight ─────────────────────────────────────────────────────────
if ! docker info >/dev/null 2>&1; then
    err "Docker daemon not running"
    exit 1
fi

cd "$PROJECT_DIR"

# ── Login (skip if already logged in) ──────────────────────────────────
if ! docker info 2>/dev/null | grep -q "Registry: $REGISTRY"; then
    if [ -z "${GHCR_USER:-}" ] || [ -z "${GHCR_TOKEN:-}" ]; then
        warn "GHCR_USER/GHCR_TOKEN not set — attempting unauthenticated pull"
    else
        echo "$GHCR_TOKEN" | docker login "$REGISTRY" -u "$GHCR_USER" --password-stdin
    fi
fi

# ── Pull ───────────────────────────────────────────────────────────────
log "Pulling images (tag: $TAG)..."
docker pull "$REGISTRY/$IMAGE:$TAG" || { err "Pull failed for $IMAGE:$TAG"; exit 1; }
docker pull "$REGISTRY/$IMAGE:$TAG-simulator" 2>/dev/null || warn "Simulator image not found (non-fatal)"

# ── Tag as local ───────────────────────────────────────────────────────
docker tag "$REGISTRY/$IMAGE:$TAG" "paper-trading-rebuild:$TAG"
log "Tagged paper-trading-rebuild:$TAG"

# ── Record deployment ──────────────────────────────────────────────────
mkdir -p state
echo "$TAG $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> state/deploy-history.log
log "Deploy recorded to state/deploy-history.log"

# ── Restart stack ──────────────────────────────────────────────────────
log "Restarting docker-compose stack..."
docker compose down --remove-orphans
docker compose up -d --wait

# ── Health check ───────────────────────────────────────────────────────
log "Running health checks..."
sleep 5

check_url() {
    local url="$1"
    local label="$2"
    if curl -sf --max-time 10 "$url" >/dev/null 2>&1; then
        log "  ✓ $label healthy ($url)"
    else
        warn "  ✗ $label not responding ($url)"
    fi
}

check_url "http://localhost:5000/health"    "data-bus"
check_url "http://localhost:5004/api/summary" "dashboard"

# ── Summary ────────────────────────────────────────────────────────────
log "Deploy complete — tag: $TAG"
log "Rollback: ./scripts/deploy.sh <previous-tag>"
log "History: tail -5 state/deploy-history.log"
