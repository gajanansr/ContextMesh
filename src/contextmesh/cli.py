"""ContextMesh CLI — start, mcp, proxy, stats, turns, tasks, index, status."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
import httpx
import uvicorn
from rich.console import Console

from contextmesh.config import load_config

console = Console()
DAEMON_URL = "http://localhost:8765"


@click.group()
def main() -> None:
    """ContextMesh — intelligent context layer for Claude Code."""
    pass


# ──────────────────────────────────────────────────────────────────────────────
# start
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True)
@click.option("--reload", is_flag=True, default=False, help="Hot-reload (dev mode)")
@click.option("--log-level", default="info", show_default=True)
@click.option("--project", default=".", show_default=True, help="Project root to index")
def start(host: str, port: int, reload: bool, log_level: str, project: str) -> None:
    """Start the ContextMesh daemon."""
    from contextmesh.bootstrap import setup_logging
    setup_logging(log_level)

    project_path = Path(project).resolve()
    console.print(f"[bold green]ContextMesh daemon[/bold green] starting on {host}:{port}")
    console.print(f"Project: {project_path}")
    console.print("Press Ctrl+C to stop.\n")

    import os
    os.environ["CONTEXTMESH_PROJECT_PATH"] = str(project_path)

    uvicorn.run(
        "contextmesh.daemon.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
    )


# ──────────────────────────────────────────────────────────────────────────────
# stop
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--all", "stop_all", is_flag=True, default=False, help="Stop proxy + daemon")
def stop(stop_all: bool) -> None:
    """Stop the ContextMesh proxy (and optionally the daemon)."""
    import platform
    import subprocess
    import signal
    import socket

    def _is_running(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    system = platform.system()
    stopped_any = False

    # ── Stop Proxy (port 8099) ──────────────────────────────────────────
    if _is_running(8099):
        # Try to stop the LaunchAgent/systemd service first (graceful)
        if system == "Darwin":
            from pathlib import Path
            plist = Path.home() / "Library" / "LaunchAgents" / "ai.contextmesh.proxy.plist"
            if plist.exists():
                subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
                console.print("[green]✓[/green] Proxy LaunchAgent stopped")
                stopped_any = True
        elif system == "Linux":
            result = subprocess.run(
                ["systemctl", "--user", "stop", "contextmesh-proxy"],
                capture_output=True
            )
            if result.returncode == 0:
                console.print("[green]✓[/green] Proxy systemd service stopped")
                stopped_any = True

        # Fallback: pkill the process directly
        if _is_running(8099):
            subprocess.run(["pkill", "-f", "contextmesh proxy"], capture_output=True)
            console.print("[green]✓[/green] Proxy process killed")
            stopped_any = True
    else:
        console.print("[dim]Proxy is not running (port 8099)[/dim]")

    # ── Stop Daemon (port 8765) ────────────────────────────────────────
    if stop_all:
        if _is_running(8765):
            subprocess.run(["pkill", "-f", "contextmesh.daemon"], capture_output=True)
            subprocess.run(["pkill", "-f", "contextmesh start"], capture_output=True)
            console.print("[green]✓[/green] Daemon stopped")
            stopped_any = True
        else:
            console.print("[dim]Daemon is not running (port 8765)[/dim]")

    if not stopped_any:
        console.print("[yellow]No ContextMesh services are currently running.[/yellow]")
    else:
        console.print("\n[bold green]✓ Services stopped successfully.[/bold green]")
        console.print("[dim]To restart the proxy engine, run: contextmesh proxy &[/dim]")


# Cleanup complete

# ──────────────────────────────────────────────────────────────────────────────
# stats
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--session", default=None, help="Session ID (omit for global summary)")
def stats(session: str | None) -> None:
    """Show real-time token savings driven by the proxy (no MCP needed)."""
    from rich.table import Table
    from rich.panel import Panel

    try:
        if session:
            resp = httpx.get(f"{DAEMON_URL}/savings/{session}", timeout=5.0)
        else:
            resp = httpx.get(f"{DAEMON_URL}/savings", timeout=5.0)
        resp.raise_for_status()
        d = resp.json()

        # Also fetch detailed proxy stats
        stats_resp = httpx.get(f"{DAEMON_URL}/stats", timeout=5.0)
        s = stats_resp.json() if stats_resp.status_code == 200 else {}

        table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
        table.add_column("Metric", style="bold white")
        table.add_column("Value", justify="right", style="bold green")

        turns        = d.get("total_turns", s.get("turns_tracked", 0))
        sessions     = d.get("session_count", s.get("sessions_tracked", 0))
        original     = s.get("original_tokens", d.get("total_accumulated_tokens", 0))
        actual       = d.get("total_routed_tokens", s.get("actual_tokens", 0))
        rtk_saved    = s.get("rtk_tokens_saved", 0)
        flush_saved  = s.get("flush_tokens_saved", 0)
        total_saved  = d.get("total_tokens_saved", rtk_saved + flush_saved)
        cost_saved   = d.get("total_cost_saved_usd", s.get("cost_saved_usd", 0.0))
        ratio        = d.get("avg_compression_ratio", (actual / original) if original > 0 else 1.0)

        table.add_row("Sessions tracked",          str(sessions))
        table.add_row("Turns tracked",             str(turns))
        table.add_row("", "")
        table.add_row("Original tokens (est.)",    f"{original:,}")
        table.add_row("Actual tokens sent",        f"{actual:,}")
        table.add_row("", "")
        table.add_row("[green]RTK compressed[/green]",    f"[green]{rtk_saved:,}[/green]")
        table.add_row("[green]Context flushed[/green]",   f"[green]{flush_saved:,}[/green]")
        table.add_row("[bold green]Total tokens saved[/bold green]", f"[bold green]{total_saved:,}[/bold green]")
        table.add_row("Avg compression ratio",     f"{ratio:.0%}")
        table.add_row("[bold green]Estimated cost saved[/bold green]", f"[bold green]${cost_saved:.4f}[/bold green]")

        console.print()
        console.print(Panel(table, title="[bold]ContextMesh Token Savings Report[/bold]", border_style="cyan"))
        console.print()
        if turns == 0:
            console.print("[yellow]No turns tracked yet. Make sure the proxy is running: [bold]contextmesh proxy &[/bold][/yellow]")

    except httpx.ConnectError:
        console.print("[red]Daemon not running.[/red] Start it with: [bold]contextmesh start[/bold]")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")


# ──────────────────────────────────────────────────────────────────────────────
# turns
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--session", required=True, help="Session ID")
@click.option("--limit", default=20, show_default=True, help="Number of turns to show")
def turns(session: str, limit: int) -> None:
    """Show per-turn token savings table."""
    from contextmesh.tracker.reporter import print_turn_table
    try:
        resp = httpx.get(
            f"{DAEMON_URL}/savings/{session}/turns",
            params={"limit": limit},
            timeout=5.0,
        )
        resp.raise_for_status()
        print_turn_table(resp.json())
    except httpx.ConnectError:
        console.print("[red]Daemon not running.[/red] Start it with: [bold]contextmesh start[/bold]")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")


# ──────────────────────────────────────────────────────────────────────────────
# tasks
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--session", required=True, help="Session ID")
def tasks(session: str) -> None:
    """Show task hierarchy for a session."""
    try:
        resp = httpx.get(f"{DAEMON_URL}/tasks/{session}", timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        console.print(f"\n[bold]Tasks for session {session}[/bold]\n")
        for t in data.get("tasks", []):
            tier_color = {"hot": "green", "warm": "yellow", "cold": "dim"}.get(
                t.get("tier", "cold"), "white"
            )
            console.print(
                f"  [{tier_color}]{t.get('tier','?').upper()}[/{tier_color}] "
                f"[bold]{t.get('name','?')}[/bold] "
                f"({t.get('status','?')}) — {t.get('node_count', 0)} nodes"
            )
    except httpx.ConnectError:
        console.print("[red]Daemon not running.[/red] Start it with: [bold]contextmesh start[/bold]")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")


# ──────────────────────────────────────────────────────────────────────────────
# index
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("path", default=".", required=False)
def index(path: str) -> None:
    """
    Index a project's codebase into the repo graph.

    This parses all source files using Tree-sitter and stores functions,
    classes, and file relationships in the SQLite database. Run once per
    project (or after large refactors). Incremental updates happen
    automatically via PostToolUse hooks.
    """
    project_path = Path(path).resolve()
    if not project_path.exists():
        console.print(f"[red]Path does not exist:[/red] {project_path}")
        sys.exit(1)

    console.print(f"[bold green]Indexing[/bold green] {project_path} …")

    async def _run() -> None:
        cfg = load_config(project_path)
        from contextmesh.store.db import init_db
        from contextmesh.graph.repo import RepoGraph

        db_path = cfg.data_dir / "contextmesh.db"
        db = await init_db(db_path)
        repo = RepoGraph(project_path, db)

        with console.status("Parsing source files…"):
            stats = await repo.index_project()

        await db.close()

        console.print(f"[green]Done![/green]")
        console.print(f"  Files:     {stats.get('files', 0)}")
        console.print(f"  Functions: {stats.get('functions', 0)}")
        console.print(f"  Classes:   {stats.get('classes', 0)}")
        console.print(f"  Edges:     {stats.get('edges', 0)}")

    asyncio.run(_run())


# ──────────────────────────────────────────────────────────────────────────────
# install-mac
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# init
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("payload_b64")
@click.option("--session", required=True)
def run(payload_b64: str, session: str) -> None:
    """Internal executor used by the Hook Engine."""
    import base64
    import subprocess
    import sqlite3
    import uuid
    from contextmesh.config import get_config
    from contextmesh.utils.injector import _build_repomap_from_db

    # 1. Decode command
    try:
        command = base64.b64decode(payload_b64).decode("utf-8")
    except Exception:
        print("ContextMesh Error: Failed to decode command.")
        return

    # 2. Execute locally
    proc = subprocess.run(command, shell=True, capture_output=True, text=True)
    raw_output = proc.stdout
    if proc.stderr:
        raw_output += f"\n[STDERR]\n{proc.stderr}"
    
    if not raw_output.strip():
        raw_output = "(Command executed successfully with no output)"

    # 3. Compress (RTK style)
    original_chars = len(raw_output)
    if original_chars > 12_000:
        compressed = (
            raw_output[:3000] 
            + f"\n\n... [ContextMesh RTK Compressor: {original_chars - 6000} chars removed to save tokens] ...\n\n" 
            + raw_output[-3000:]
        )
    else:
        compressed = raw_output

    saved_chars = original_chars - len(compressed)
    rtk_tokens_saved = saved_chars // 4
    final_output = compressed

    # 4. Inject RepoMap (Turn 1 only) & Write Stats
    try:
        db_path = str(get_config().data_dir / "contextmesh.db")
        
        # Try to inject Repomap first (even if DB locks, we can just read the file)
        try:
            # We'll just always inject it for the first tool call of the session
            # Claude's session id changes each run. 
            repomap = _build_repomap_from_db(db_path)
            if repomap:
                final_output = repomap + "\n\n=== COMMAND OUTPUT ===\n" + final_output
        except Exception:
            pass

        con = sqlite3.connect(db_path, timeout=5)
        # Record stat measurement
        mid = uuid.uuid4().hex
        est_tokens = len(compressed) // 4
        orig_est_tokens = original_chars // 4
        
        con.execute(
            """INSERT INTO proxy_measurements 
               (measurement_id, session_id, timestamp, model, input_tokens, original_input_tokens, rtk_tokens_saved, request_preview)
               VALUES (?, ?, datetime('now'), 'hook', ?, ?, ?, ?)""",
            (mid, session, est_tokens, orig_est_tokens, rtk_tokens_saved, command[:100])
        )
        con.commit()
        con.close()
    except Exception as e:
        # Log the error so we can debug
        with open("/tmp/contextmesh_err.log", "a") as f:
            f.write(f"DB Error: {str(e)}\n")
        pass

    # 5. Output to Claude
    print(final_output)



@main.command()
def init() -> None:
    """Global 1-click setup. Run this once per machine."""
    from contextmesh.installer import full_install
    console.print("[bold]Initializing ContextMesh globally for your machine...[/bold]\n")
    full_install()


@main.command()
def uninstall() -> None:
    """Remove all ContextMesh transparent integrations from this machine."""
    from contextmesh.installer import full_uninstall
    full_uninstall()

# ──────────────────────────────────────────────────────────────────────────────
# dashboard
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
def dashboard() -> None:
    """Open the God-Mode dashboard in your browser."""
    import webbrowser
    url = f"{DAEMON_URL}/dashboard"
    console.print(f"[bold green]Opening dashboard[/bold green] at {url}")
    webbrowser.open(url)


# ──────────────────────────────────────────────────────────────────────────────
# status
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
def status() -> None:
    """Check if the daemon is running."""
    try:
        resp = httpx.get(f"{DAEMON_URL}/health", timeout=3.0)
        resp.raise_for_status()
        data = resp.json()
        console.print(
            f"[bold green]✓ Daemon is running[/bold green] "
            f"(v{data.get('version', '?')}) at {DAEMON_URL}"
        )
        # Also show quick global stats
        try:
            s = httpx.get(f"{DAEMON_URL}/stats", timeout=2.0).json()
            console.print(
                f"  Sessions: {s.get('session_count', 0)} | "
                f"Nodes: {s.get('node_count', 0)} | "
                f"Tokens saved: {s.get('total_tokens_saved', 0):,}"
            )
            proxy_turns = s.get("proxy_turns_tracked", 0)
            proxy_saved = s.get("proxy_tokens_saved", 0)
            if proxy_turns > 0:
                console.print(
                    f"  [bold green]RTK Proxy:[/bold green] "
                    f"{proxy_turns} turns tracked | "
                    f"~{proxy_saved:,} tokens compressed"
                )
        except Exception:
            pass
    except httpx.ConnectError:
        console.print(
            f"[red]✗ Daemon not running[/red] at {DAEMON_URL}\n"
            "Start it with: [bold]contextmesh start[/bold]"
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")


if __name__ == "__main__":
    main()
