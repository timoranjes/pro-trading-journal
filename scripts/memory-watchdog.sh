#!/bin/bash
# Memory watchdog for Hermes Gateway
# Checks macOS memory pressure and prunes sessions before SIGTERM kills the gateway
# Exit 0 = all good, stdout = action taken (delivered to user)
# NOTE: All output must be valid UTF-8. For no_agent=True, empty stdout = silent.

set -e
export LC_ALL=en_US.UTF-8 2>/dev/null || true

# Thresholds
MEM_FREE_THRESHOLD_MB=100       # Alert if free memory drops below this
SESSION_COUNT_THRESHOLD=50      # Alert if active sessions exceed this
SESSION_RETENTION_DAYS=7        # Delete session files older than this

SESSIONS_DIR="$HOME/.hermes/sessions"
SESSIONS_JSON="$SESSIONS_DIR/sessions.json"

# Get free memory in MB
free_pages=$(vm_stat | grep "Pages free:" | awk '{print $3}' | tr -d '.')
free_mb=$((free_pages * 16384 / 1048576))

# Get active session count
session_count=0
if [ -f "$SESSIONS_JSON" ]; then
    session_count=$(python3 -c "
import json
with open('$SESSIONS_JSON') as f:
    data = json.load(f)
print(len(data))
" 2>/dev/null || echo "0")
fi

# Get gateway PID and memory usage
gw_pid=$(launchctl list 2>/dev/null | grep "ai.hermes.gateway" | awk '{print $1}')
gw_mem_mb=0
if [ -n "$gw_pid" ] && [ "$gw_pid" != "0" ]; then
    gw_mem_mb=$(ps -o rss= -p "$gw_pid" 2>/dev/null | awk '{printf "%.0f", $1/1024}' || echo "0")
fi

alerts=""

# Check 1: Free memory too low
if [ "$free_mb" -lt "$MEM_FREE_THRESHOLD_MB" ]; then
    alerts="${alerts}WARNING: Low memory: ${free_mb}MB free (threshold: ${MEM_FREE_THRESHOLD_MB}MB)
"

    # Prune empty sessions from sessions.json
    if [ -f "$SESSIONS_JSON" ] && [ "$session_count" -gt 0 ]; then
        pruned=$(python3 -c "
import json, time
with open('$SESSIONS_JSON') as f:
    sessions = json.load(f)
original = len(sessions)
stale = []
for sid, sess in list(sessions.items()):
    if not sess.get('messages'):
        stale.append(sid)
        continue
    # Check if last message is older than retention
    try:
        last_ts = sess['messages'][-1].get('timestamp', '')
        if last_ts.endswith('Z'):
            last_ts = last_ts[:-1] + '+00:00'
        lt = time.mktime(time.strptime(last_ts[:19], '%Y-%m-%dT%H:%M:%S'))
        if (time.time() - lt) > $SESSION_RETENTION_DAYS * 86400:
            stale.append(sid)
    except:
        stale.append(sid)
for sid in stale:
    del sessions[sid]
with open('$SESSIONS_JSON', 'w') as f:
    json.dump(sessions, f)
print(len(stale))
" 2>/dev/null || echo "0")
        alerts="${alerts}  - Pruned ${pruned} stale sessions from registry
"
    fi

    # Delete old session files
    old_files=$(find "$SESSIONS_DIR" -name "session_*.json" -mtime +3 | wc -l)
    if [ "$old_files" -gt 0 ]; then
        find "$SESSIONS_DIR" -name "session_*.json" -mtime +3 -delete
        find "$SESSIONS_DIR" -name "*.jsonl" -mtime +3 -delete
        find "$SESSIONS_DIR" -name "request_dump_*.json" -mtime +1 -delete
        freed=$(du -sm "$SESSIONS_DIR" | awk '{print $1}')
        alerts="${alerts}  - Deleted ${old_files} old session files (${freed}MB remaining on disk)
"
    fi
fi

# Check 2: Too many active sessions
if [ "$session_count" -gt "$SESSION_COUNT_THRESHOLD" ]; then
    alerts="${alerts}WARNING: High session count: ${session_count} (threshold: ${SESSION_COUNT_THRESHOLD})
"
fi

# Check 3: Gateway memory usage
if [ "$gw_mem_mb" -gt 500 ]; then
    alerts="${alerts}WARNING: Gateway using ${gw_mem_mb}MB RAM (>500MB)
"
fi

# Output results (empty stdout = silent for no_agent watchdog pattern)
if [ -n "$alerts" ]; then
    printf "Hermes Memory Watchdog Alert:\n"
    printf "Free memory: %sMB | Sessions: %s | Gateway: %sMB\n" "$free_mb" "$session_count" "$gw_mem_mb"
    printf "%s" "$alerts"
fi
# If no alerts: produce NO output at all (not even a newline)
