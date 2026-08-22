"""Installer logic for macOS auto-start and Claude Code hooks."""

import json
import os
import shutil
import sys
from pathlib import Path
from rich.console import Console

console = Console()

def install_macos_launch_agent():
    """Create a LaunchAgent so ContextMesh starts when the Mac boots."""
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    
    plist_path = plist_dir / "com.contextmesh.daemon.plist"
    
    contextmesh_bin = shutil.which("contextmesh")
    if not contextmesh_bin:
        contextmesh_bin = sys.executable.replace("python", "contextmesh")
        
    log_dir = Path.home() / ".contextmesh" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.contextmesh.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{contextmesh_bin}</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>{log_dir}/daemon.error.log</string>
    <key>StandardOutPath</key>
    <string>{log_dir}/daemon.out.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin')}</string>
    </dict>
</dict>
</plist>
"""
    plist_path.write_text(plist_content)
    
    # Load the agent
    os.system(f"launchctl unload {plist_path} 2>/dev/null")
    os.system(f"launchctl load {plist_path}")
    
    console.print(f"[green]✓ macOS auto-start configured (LaunchAgent loaded).[/green]")


def setup_claude_hooks():
    """Inject ContextMesh hooks into ~/.claude/settings.json."""
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings = {}
    if settings_path.exists():
        try:
            with open(settings_path, "r") as f:
                settings = json.load(f)
        except Exception:
            pass

    if "hooks" not in settings:
        settings["hooks"] = {}
    hooks = settings["hooks"]

    # We use curl directly to send the hook to the daemon instead of a bash script
    events = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
    for event in events:
        if event not in hooks:
            hooks[event] = []
            
        cmd = (
            f"curl -s -X POST http://127.0.0.1:8765/hook "
            f"-H 'Content-Type: application/json' "
            f"-d '{{\"event_type\": \"{event}\", "
            f"\"session_id\": \"'$CLAUDE_SESSION_ID'\", "
            f"\"project_path\": \"'$CLAUDE_PROJECT_DIR'\"}}' > /dev/null 2>&1 || true"
        )
        
        # Check if already installed
        exists = False
        for item in hooks[event]:
            if "hooks" in item:
                for h in item["hooks"]:
                    if h.get("type") == "command" and "8765/hook" in h.get("command", ""):
                        exists = True
                        break
        
        if not exists:
            hooks[event].append({
                "hooks": [{
                    "type": "command",
                    "command": cmd
                }]
            })

    settings_path.write_text(json.dumps(settings, indent=2))
    console.print(f"[green]✓ Claude Code global hooks configured.[/green]")


def setup_mcp_for_project(project_path: str):
    """Run the claude mcp add command for the current project."""
    import subprocess
    console.print(f"[yellow]Connecting Claude Code to ContextMesh router...[/yellow]")
    try:
        subprocess.run(["claude", "mcp", "add", "contextmesh", "contextmesh", "mcp"], check=True)
        console.print(f"[green]✓ MCP Server connected.[/green]")
    except Exception as e:
        console.print(f"[red]Failed to add MCP server: {e}[/red]")
        console.print("Make sure you run this inside a directory where you use Claude Code.")
