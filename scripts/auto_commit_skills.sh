#!/bin/bash
# Auto-commit any changes to ~/.hermes/skills/ to prevent data loss.
# Runs every 5 minutes via cron. Safe to run repeatedly.

set -e

HERMES_HOME="$HOME/.hermes"
SKILLS_DIR="$HERMES_HOME/skills"

cd "$HERMES_HOME"

# Exit if not a git repo
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Not a git repo: $HERMES_HOME"
    exit 1
fi

# Check if skills/ has any changes
if ! git diff --quiet -- "$SKILLS_DIR" 2>/dev/null || ! git diff --cached --quiet -- "$SKILLS_DIR" 2>/dev/null; then
    # Stage skills/
    git add "$SKILLS_DIR"
    
    # Only commit if there's something to commit
    if ! git diff --cached --quiet -- "$SKILLS_DIR" 2>/dev/null; then
        git commit -m "auto-backup: skills $(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null 2>&1
        # Fetch first to update tracking ref, then decide
        git fetch origin main >/dev/null 2>&1
        if git merge-base --is-ancestor origin/main HEAD 2>/dev/null; then
            # We're ahead → fast-forward push works
            push_output=$(git push origin main 2>&1) || echo "ERROR: skills push failed — $push_output"
        else
            # Diverged (concurrent run) — rebase with autostash, then push
            git pull --rebase --autostash origin main >/dev/null 2>&1 || true
            push_output=$(git push origin main 2>&1) || echo "ERROR: skills push failed — $push_output"
        fi
    fi
else
    : # silent - no changes
fi
