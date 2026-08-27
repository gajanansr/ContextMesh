"""
ContextMesh Transparent Installation.

Like RTK and Headroom, ContextMesh should work without any wrapper.
This module handles:
1. Writing ANTHROPIC_BASE_URL globally to the user's shell profile
2. Writing PreToolUse/PostToolUse Claude Code hooks for RTK-style interception
3. Setting up the proxy as a persistent LaunchAgent (macOS) / systemd unit (Linux)
4. Removing all of the above on uninstall
"""

from __future__ import annotations

import os
import platform
import subprocess
import json
from pathlib import Path
from rich.console import Console

console = Console()

PROXY_PORT = 8099
SHELL_EXPORT_LINE = f'\n# ContextMesh — transparent AI proxy\nexport ANTHROPIC_BASE_URL="http://127.0.0.1:{PROXY_PORT}"\n'
SHELL_MARKER = "# ContextMesh — transparent AI proxy"

SHELL_PROFILES = [
    Path.home() / ".zshrc",
    Path.home() / ".bashrc",
    Path.home() / ".bash_profile",
    Path.home() / ".profile",
    Path.home() / ".config" / "fish" / "config.fish",
]


# ── Shell Profile ──────────────────────────────────────────────────────────────

def _detect_active_shell_profile() -> tuple[Path, bool]:
    """Return (profile_path, is_fish)."""
    shell = os.environ.get("SHELL", "")
    
    if "fish" in shell:
        p = Path.home() / ".config" / "fish" / "config.fish"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(exist_ok=True)
        return p, True
        
    if "zsh" in shell:
        p = Path.home() / ".zshrc"
        p.touch(exist_ok=True)
        return p, False
        
    if "bash" in shell:
        for candidate in [Path.home() / ".bashrc", Path.home() / ".bash_profile"]:
            if candidate.exists():
                return candidate, False
        p = Path.home() / ".bashrc"
        p.touch(exist_ok=True)
        return p, False
        
    # Fallback
    return Path.home() / ".profile", False


def inject_shell_env() -> Path | None:
    """
    Write ANTHROPIC_BASE_URL into the user's shell profile.
    Handles Bash/Zsh (export) and Fish (set -gx).
    """
    profile, is_fish = _detect_active_shell_profile()
    content = profile.read_text(errors="replace") if profile.exists() else ""

    if SHELL_MARKER in content:
        console.print(f"  [dim]Shell profile already configured ({profile.name})[/dim]")
        return None

    export_line = (
        f'\n{SHELL_MARKER}\nset -gx ANTHROPIC_BASE_URL "http://127.0.0.1:{PROXY_PORT}"\n'
        if is_fish else
        f'\n{SHELL_MARKER}\nexport ANTHROPIC_BASE_URL="http://127.0.0.1:{PROXY_PORT}"\n'
    )

    with open(profile, "a") as f:
        f.write(export_line)

    console.print(f"  [green]✓[/green] Added ANTHROPIC_BASE_URL to [bold]{profile}[/bold]")
    return profile


def remove_shell_env() -> None:
    """Remove the ANTHROPIC_BASE_URL lines from all shell profiles."""
    for profile in SHELL_PROFILES:
        if not profile.exists():
            continue
        lines = profile.read_text(errors="replace").splitlines(keepends=True)
        new_lines = []
        skip_next = False
        for line in lines:
            if SHELL_MARKER in line:
                skip_next = True
                continue
            if skip_next and "ANTHROPIC_BASE_URL" in line:
                skip_next = False
                continue
            new_lines.append(line)
        profile.write_text("".join(new_lines))
    console.print("  [green]✓[/green] Removed ANTHROPIC_BASE_URL from shell profiles")


# ── Claude Code Hooks ──────────────────────────────────────────────────────────

HOOK_COMMAND_PROXY = (
    "contextmesh proxy-hook"
)

def _get_claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


# Hook Engine entry point is one binary; Claude Code dispatches by event.
HOOK_EVENTS = ("PreToolUse", "UserPromptSubmit", "SessionEnd")


def inject_claude_hooks() -> None:
    """Register the ContextMesh Hook Engine for every event it handles.

    PreToolUse compresses tool output, UserPromptSubmit recalls prior-session
    memory, SessionEnd harvests the finished transcript. Registration is
    idempotent -- existing entries are left alone and missing ones added, so
    upgrading from a PreToolUse-only install just works.
    """
    settings_path = _get_claude_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
        except Exception:
            data = {}

    hooks = data.setdefault("hooks", {})
    added = []

    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        already = any(
            h.get("command") == "contextmesh-hook"
            for entry in entries
            for h in entry.get("hooks", [])
        )
        if already:
            continue
        entries.append({
            "matcher": "*",
            "hooks": [{"type": "command", "command": "contextmesh-hook"}],
        })
        added.append(event)

    if added:
        settings_path.write_text(json.dumps(data, indent=2))
        console.print(
            f"  [green]OK[/green] Hook Engine registered for {', '.join(added)} "
            f"in [bold]~/.claude/settings.json[/bold]"
        )
    else:
        console.print("  [dim]Claude Code hooks already installed[/dim]")


def remove_claude_hooks() -> None:
    """Remove ContextMesh hooks from ~/.claude/settings.json."""
    settings_path = _get_claude_settings_path()
    if not settings_path.exists():
        return

    data = json.loads(settings_path.read_text())
    hooks = data.get("hooks", {})

    for event in ["PreToolUse", "PostToolUse"]:
        event_list = hooks.get(event, [])
        hooks[event] = [
            entry for entry in event_list
            if not any("contextmesh" in h.get("command", "") for h in entry.get("hooks", []))
        ]

    settings_path.write_text(json.dumps(data, indent=2))
    console.print("  [green]✓[/green] Removed ContextMesh hooks from ~/.claude/settings.json")


# ── Persistent Proxy Service ──────────────────────────────────────────────────

LAUNCHD_LABEL = "ai.contextmesh.proxy"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

def _find_contextmesh_bin() -> str:
    """Find the absolute path to the contextmesh binary."""
    result = subprocess.run(["which", "contextmesh"], capture_output=True, text=True)
    return result.stdout.strip() or "contextmesh"


def install_proxy_service() -> None:
    """
    Install the ContextMesh proxy as a persistent background service.
    - macOS: LaunchAgent plist
    - Linux: systemd user unit (fallback)
    """
    system = platform.system()
    bin_path = _find_contextmesh_bin()
    log_file = str(Path.home() / ".contextmesh" / "proxy.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    if system == "Darwin":
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{bin_path}</string>
        <string>proxy</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
</dict>
</plist>"""
        LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
        LAUNCHD_PLIST.write_text(plist)

        # Load it immediately
        subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True)
        result = subprocess.run(["launchctl", "load", str(LAUNCHD_PLIST)], capture_output=True, text=True)

        if result.returncode == 0:
            console.print(f"  [green]✓[/green] Proxy installed as macOS LaunchAgent (auto-starts on login)")
        else:
            console.print(f"  [yellow]⚠[/yellow] LaunchAgent written but load failed: {result.stderr.strip()}")

    elif system == "Linux":
        systemd_dir = Path.home() / ".config" / "systemd" / "user"
        systemd_dir.mkdir(parents=True, exist_ok=True)
        unit_path = systemd_dir / "contextmesh-proxy.service"
        unit = f"""[Unit]
Description=ContextMesh Token Proxy
After=network.target

[Service]
ExecStart={bin_path} proxy
Restart=always
StandardOutput=append:{log_file}
StandardError=append:{log_file}

[Install]
WantedBy=default.target
"""
        unit_path.write_text(unit)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "contextmesh-proxy"], capture_output=True)
        console.print("  [green]✓[/green] Proxy installed as systemd user service (auto-starts on login)")
    else:
        console.print(f"  [yellow]⚠[/yellow] Unsupported OS ({system}). Please start the proxy manually with: contextmesh proxy")


def uninstall_proxy_service() -> None:
    """Remove the persistent proxy service."""
    system = platform.system()
    if system == "Darwin" and LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True)
        LAUNCHD_PLIST.unlink()
        console.print("  [green]✓[/green] Removed macOS LaunchAgent")
    elif system == "Linux":
        subprocess.run(["systemctl", "--user", "disable", "--now", "contextmesh-proxy"], capture_output=True)
        unit_path = Path.home() / ".config" / "systemd" / "user" / "contextmesh-proxy.service"
        if unit_path.exists():
            unit_path.unlink()
        console.print("  [green]✓[/green] Removed systemd service")


# ── Public API ─────────────────────────────────────────────────────────────────

def setup_mcp_for_project(project_path: str) -> None:
    """Run the claude mcp add command for the current project."""
    console.print("[yellow]Connecting Claude Code to ContextMesh router...[/yellow]")
    try:
        subprocess.run(["claude", "mcp", "add", "contextmesh", "contextmesh", "mcp"], check=True)
        console.print("[green]✓ MCP Server connected.[/green]")
    except Exception as e:
        console.print(f"[red]Failed to add MCP server: {e}[/red]")
        console.print("Make sure you run this inside a directory where you use Claude Code.")


def full_install() -> None:
    """
    Full Hook Engine installation.
    After this, Claude Code routes terminal commands through ContextMesh automatically.
    """
    console.rule("[bold blue]ContextMesh Setup")
    console.print("\n[bold]Initializing Hook Engine...[/bold]\n")

    console.print("[dim]1/1[/dim] Registering Claude Code hooks...")
    inject_claude_hooks()

    console.print("\n[bold green]✨ ContextMesh is active and intercepting.[/bold green]")
    console.print("You no longer need any special commands. Just run [bold cyan]claude[/bold cyan] normally.")
    console.rule()


def full_uninstall() -> None:
    """Remove all ContextMesh integrations."""
    console.rule("[bold red]Uninstalling ContextMesh")
    console.print("\n[bold]Removing Hook Engine...[/bold]\n")
    
    remove_claude_hooks()
    
    # Clean up legacy stuff if it exists from older versions
    remove_shell_env()
    try:
        uninstall_proxy_service()
    except Exception:
        pass
    
    try:
        result = subprocess.run(["claude", "mcp", "remove", "contextmesh"], capture_output=True, text=True)
        if result.returncode == 0:
            console.print("  [green]✓[/green] Removed legacy MCP configuration")
    except Exception:
        pass
        
    console.print("\n[bold green]✓ Uninstallation complete.[/bold green]")
    console.print("Your system has been restored to its original state.\n")
    console.rule()
