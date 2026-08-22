#!/usr/bin/env bash
set -euo pipefail

CONTEXTMESH_DIR="${HOME}/.contextmesh"
VENV_DIR="${CONTEXTMESH_DIR}/venv"
SETTINGS_FILE="${HOME}/.claude/settings.json"
CONFIG_FILE="${CONTEXTMESH_DIR}/config.toml"

echo "Creating ContextMesh environment..."
mkdir -p "$CONTEXTMESH_DIR"
python3 -m venv "$VENV_DIR"

echo "Installing contextmesh package..."
"$VENV_DIR/bin/pip" install -e .

echo "Setting up ContextMesh config..."
if [ ! -f "$CONFIG_FILE" ]; then
cat << 'EOF' > "$CONFIG_FILE"
[daemon]
port = 8765
host = "127.0.0.1"

[router]
default_budget_tokens = 15000
hot_budget_fraction = 0.20
warm_budget_fraction = 0.50
code_budget_fraction = 0.30

[embeddings]
model = "all-MiniLM-L6-v2"

[tasks]
topic_shift_threshold = 0.35
min_turns_per_task = 3

[tracker]
input_price_per_mtok = 3.0
uncached_price_per_mtok = 15.0

[mcp]
server_name = "contextmesh"

[proxy]
enabled = false
port = 8099
upstream = "https://api.anthropic.com"
EOF
fi

echo "Setting up Claude Code hooks..."
mkdir -p "$(dirname "$SETTINGS_FILE")"

HOOK_SCRIPT="$(pwd)/scripts/hook_event.sh"
chmod +x "$HOOK_SCRIPT"

# We use a python script to merge the hooks section safely
"$VENV_DIR/bin/python" - <<EOF
import json
import os

settings_path = "$SETTINGS_FILE"
hook_script = "$HOOK_SCRIPT"

if os.path.exists(settings_path):
    with open(settings_path, "r") as f:
        try:
            settings = json.load(f)
        except json.JSONDecodeError:
            settings = {}
else:
    settings = {}

if "hooks" not in settings:
    settings["hooks"] = {}

hooks = settings["hooks"]

events = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
for event in events:
    if event not in hooks:
        hooks[event] = []
    
    # Check if our hook is already there
    cmd = f"CONTEXTMESH_EVENT_TYPE={event} CLAUDE_SESSION_ID=\\$CLAUDE_SESSION_ID CLAUDE_PROJECT_DIR=\\$CLAUDE_PROJECT_DIR {hook_script}"
    
    exists = False
    for item in hooks[event]:
        if "hooks" in item:
            for h in item["hooks"]:
                if h.get("type") == "command" and h.get("command") == cmd:
                    exists = True
                    break
    
    if not exists:
        hooks[event].append({
            "hooks": [{
                "type": "command",
                "command": cmd
            }]
        })

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)

EOF

echo "Installation complete!"
echo "To start the daemon, run:"
echo "$VENV_DIR/bin/uvicorn contextmesh.daemon.server:create_app --host 127.0.0.1 --port 8765"
