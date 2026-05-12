#!/bin/bash
# Picks the next improvement topic: pattern analysis suggestions take priority,
# then falls back to rotation topics if no implementable suggestions exist.

WATERMARK="$HOME/.hermes/data/improvement-watermark.json"
TASK_FILE="$HOME/.hermes/data/top-improvement-task.json"
ROTATION_WATERMARK="$HOME/.hermes/data/rotation-watermark.json"
mkdir -p "$HOME/.hermes/data"

# ── Priority 1: Check for implementable pattern analysis task ──
if [ -f "$TASK_FILE" ]; then
  TASK=$(python3 -c "
import json, sys
from datetime import datetime, timedelta
data = json.load(open('$TASK_FILE'))
if data.get('task'):
    # Only use tasks generated in last 24h
    gen = datetime.fromisoformat(data.get('generated_at', '2000-01-01'))
    if datetime.now() - gen < timedelta(hours=24):
        print(json.dumps(data))
    else:
        print('{}')
else:
    print('{}')
")
  
  if [ "$TASK" != "{}" ] && [ -n "$TASK" ]; then
    TASK_NAME=$(echo "$TASK" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['task'])")
    TASK_TRIGGER=$(echo "$TASK" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['trigger'])")
    TASK_HINT=$(echo "$TASK" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('hint',''))")
    TASK_PRIORITY=$(echo "$TASK" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['priority'])")
    
    # Mark task as consumed so it doesn't repeat
    echo "{\"task\": null, \"reason\": \"consumed\", \"consumed_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > "$TASK_FILE"
    
    echo "⚡ PATTERN-DRIVEN TASK [$TASK_PRIORITY]: $TASK_NAME"
    echo "Trigger: $TASK_TRIGGER"
    echo "Hint: $TASK_HINT"
    exit 0
  fi
fi

# ── Priority 2: Fall back to rotation topics ──
TOPICS=(
  "agent-startup-latency: Analyze and reduce Hermes Agent startup time — config loading, model initialization, skill discovery"
  "context-window-efficiency: Research context compression strategies — what can be dropped from system prompt without losing quality"
  "cron-job-reliability: Audit all 37 cron jobs — find failures, silent errors, dedup gaps, delivery issues"
  "skill-discovery-rate: Investigate why skills aren't being loaded — improve skill_view() call rate before tasks"
  "memory-bloat-prevention: Analyze memory entries for redundancy, stale data, and compression opportunities"
  "web-search-quality: Research better search strategies for financial data — Exa vs Google vs specialized APIs"
  "error-recovery-patterns: Study common failure modes (gateway disconnect, OOM, API rate limits) and harden against them"
  "hook-system-improvements: Evaluate shell hooks (pre_llm_call, on_session_start) for better context injection and logging"
  "swarmvault-integration: Research how to better use SwarmVault for knowledge persistence — retrieval quality, consolidation"
  "discord-delivery-reliability: Audit message delivery patterns — lost messages, thread handling, rate limit resilience"
  "model-routing-optimization: Research which models are best suited for which tasks — cost vs quality analysis"
  "file-io-performance: Investigate slow file operations — read_file, write_file, search_files — optimize or replace"
  "terminal-pattern-improvements: Find better patterns for terminal usage — background processes, output parsing, error handling"
  "price-attribution-quality: Evaluate the price attribution LLM pipeline — source quality, citation accuracy, freshness"
  "watchdog-and-monitoring: Research proactive monitoring patterns — can we detect issues before they surface?"
)

# Load or initialize rotation watermark
if [ -f "$ROTATION_WATERMARK" ]; then
  LAST_INDEX=$(python3 -c "import json; print(json.load(open('$ROTATION_WATERMARK')).get('last_index', -1))")
else
  LAST_INDEX=-1
fi

# Calculate next index
NEXT_INDEX=$(( (LAST_INDEX + 1) % ${#TOPICS[@]} ))
TOPIC="${TOPICS[$NEXT_INDEX]}"
TOPIC_NAME="${TOPIC%%:*}"
TOPIC_DESC="${TOPIC#*: }"

# Save watermark
echo "{\"last_index\": $NEXT_INDEX, \"topic\": \"$TOPIC_NAME\", \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > "$ROTATION_WATERMARK"

echo "📋 ROTATION TOPIC #$(( NEXT_INDEX + 1 ))/${#TOPICS[@]}: $TOPIC_NAME"
echo "$TOPIC_DESC"
