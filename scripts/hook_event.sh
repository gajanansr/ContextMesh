#!/usr/bin/env bash
# ContextMesh hook relay script
# Called by Claude Code hooks. Reads JSON payload from stdin, POSTs to daemon.
# Usage: echo '{...}' | CONTEXTMESH_EVENT_TYPE=PostToolUse ./scripts/hook_event.sh

set -euo pipefail

DAEMON_URL="${CONTEXTMESH_DAEMON_URL:-http://127.0.0.1:8765}"
EVENT_TYPE="${CONTEXTMESH_EVENT_TYPE:-Unknown}"
SESSION_ID="${CLAUDE_SESSION_ID:-unknown}"
PROJECT_PATH="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# Read the hook payload from stdin
PAYLOAD=$(cat)

# Merge with our envelope fields
FULL_PAYLOAD=$(jq -n \
  --arg event_type "$EVENT_TYPE" \
  --arg session_id "$SESSION_ID" \
  --arg project_path "$PROJECT_PATH" \
  --argjson payload "$PAYLOAD" \
  '($payload) + {event_type: $event_type, session_id: $session_id, project_path: $project_path}'
)

# Fire and forget — don't block Claude Code
curl -s -f -X POST \
  "${DAEMON_URL}/hook" \
  -H 'Content-Type: application/json' \
  -d "$FULL_PAYLOAD" \
  --max-time 2 \
  > /dev/null 2>&1 &

# Always exit 0 — never block Claude Code
exit 0
