#!/bin/bash
# Casper's PR-based push: branch → push → PR → auto-merge on green CI
# Usage: pr-push "fix: description of change"
# Workflow:
#   1. Creates auto/<slug> branch
#   2. Commits staged changes with the message
#   3. Pushes to origin
#   4. Opens a PR with auto-merge enabled
#   5. CI runs → passes → auto-merges to main

set -euo pipefail
REPO_DIR="${REPO_DIR:-$HOME/projects/paper-trading-rebuild}"
COMMIT_MSG="${1:?Usage: pr-push \"commit message\"}"

cd "$REPO_DIR"

# Ensure we're on main and up-to-date
git checkout main
git pull origin main

# Generate branch name from commit message
SLUG=$(echo "$COMMIT_MSG" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | sed 's/^-//;s/-$//' | cut -c1-50)
BRANCH="auto/$SLUG-$(date +%H%M)"

# Create and push branch
git checkout -b "$BRANCH"
git commit -m "$COMMIT_MSG"
git push -u origin "$BRANCH"

# Open PR with auto-merge
gh pr create \
  --base main \
  --head "$BRANCH" \
  --title "$COMMIT_MSG" \
  --body "Auto-PR from Casper. Auto-merges when CI passes." \
  --label auto-merge

# Enable auto-merge (squash, delete branch after)
gh pr merge --auto --squash --delete-branch

echo ""
echo "✓ PR created from $BRANCH → main"
echo "  CI will run → auto-merge on green"
echo "  Track: gh pr view --web"
