#!/usr/bin/env bash
# post_api_request hook - Track token usage and alert on thresholds
# Fired after every API request with usage data

# Read JSON payload from stdin
payload=$(cat)

# Extract usage data
# NOTE: shell_hooks._serialize_payload() nests all non-top-level kwargs under .extra
# Top-level keys are: hook_event_name, tool_name, tool_input, session_id, cwd, extra
session_id=$(echo "$payload" | jq -r '.session_id // "unknown"')
model=$(echo "$payload" | jq -r '.extra.model // "unknown"')
usage=$(echo "$payload" | jq -r '.extra.usage // {}')
input_tokens=$(echo "$usage" | jq -r '.input_tokens // 0')
output_tokens=$(echo "$usage" | jq -r '.output_tokens // 0')
total_tokens=$((input_tokens + output_tokens))

# Get current timestamp
timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Log to session usage file
log_file="$HOME/.hermes/logs/token-usage.log"
mkdir -p "$(dirname "$log_file")"
echo "{\"timestamp\":\"$timestamp\",\"session_id\":\"$session_id\",\"model\":\"$model\",\"input_tokens\":$input_tokens,\"output_tokens\":$output_tokens,\"total_tokens\":$total_tokens}" >> "$log_file"

# Check daily budget threshold (default: 500K tokens = ~$2.70 on MiniMax)
# Read from env or use default
daily_budget=${HERMES_DAILY_TOKEN_BUDGET:-500000}

# Calculate today's usage
today=$(date -u +"%Y-%m-%d")
today_usage=$(grep "\"$today\"" "$log_file" 2>/dev/null | jq -s '[.[].total_tokens] | add // 0')

# Alert if approaching budget (80% threshold)
threshold=$((daily_budget * 80 / 100))
if [[ "$today_usage" -gt "$threshold" ]]; then
    alert_file="$HOME/.hermes/logs/budget-alerts.log"
    echo "{\"timestamp\":\"$timestamp\",\"date\":\"$today\",\"usage\":$today_usage,\"budget\":$daily_budget,\"threshold_pct\":80}" >> "$alert_file"
    
    # Return alert notification
    echo "{\"alert\":\"budget_warning\",\"message\":\"Daily token usage at ${today_usage}/${daily_budget} (>${threshold})\",\"usage\":$today_usage}"
    exit 0
fi

# Return empty - observer hook
echo '{}'
