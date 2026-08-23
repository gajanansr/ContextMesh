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
# mcp
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
def mcp() -> None:
    """Start the ContextMesh MCP server (stdio transport for Claude Code)."""
    console.print("[bold green]ContextMesh MCP server[/bold green] starting (stdio)…")
    from contextmesh.mcp.server import mcp as mcp_server
    mcp_server.run()


# ──────────────────────────────────────────────────────────────────────────────
# proxy
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8099, show_default=True)
def proxy(host: str, port: int) -> None:
    """
    Start the token measurement proxy.

    \b
    Set ANTHROPIC_BASE_URL=http://127.0.0.1:8099 in your environment
    so Claude Code routes API calls through this proxy. The proxy
    records exact token counts (including cache hits) from real API
    responses into the SQLite database.
    """
    console.print(f"[bold green]Token proxy[/bold green] on {host}:{port}")
    console.print("Set: [yellow]export ANTHROPIC_BASE_URL=http://127.0.0.1:8099[/yellow]")
    from contextmesh.proxy import proxy_app
    uvicorn.run(proxy_app, host=host, port=port, log_level="warning")


# ──────────────────────────────────────────────────────────────────────────────
# stats
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--session", default=None, help="Session ID (omit for global summary)")
def stats(session: str | None) -> None:
    """Show token savings report."""
    from contextmesh.tracker.reporter import print_session_report, print_global_report
    try:
        if session:
            resp = httpx.get(f"{DAEMON_URL}/savings/{session}", timeout=5.0)
            resp.raise_for_status()
            print_session_report(resp.json())
        else:
            resp = httpx.get(f"{DAEMON_URL}/savings", timeout=5.0)
            resp.raise_for_status()
            print_global_report(resp.json())
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

@main.command()
def install_mac() -> None:
    """Register ContextMesh to start automatically on macOS boot."""
    from contextmesh.installer import install_macos_launch_agent, setup_claude_hooks
    console.print("[bold]Installing ContextMesh as a background service...[/bold]")
    install_macos_launch_agent()
    setup_claude_hooks()
    console.print("\n[bold green]Installation complete![/bold green]")
    console.print("The daemon is now running in the background. You never have to manually run `contextmesh start` again.")

# ──────────────────────────────────────────────────────────────────────────────
# init
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
def init() -> None:
    """1-Click setup for a new repository."""
    import os
    import subprocess
    from contextmesh.installer import setup_mcp_for_project
    
    project_path = Path.cwd()
    console.print(f"[bold]Initializing ContextMesh for {project_path.name}...[/bold]")
    
    # 1. Connect MCP
    setup_mcp_for_project(str(project_path))
    
    # 2. Index the codebase
    console.print("\n[yellow]Scanning codebase...[/yellow]")
    # Call the index command logic via subprocess
    try:
        subprocess.run(["contextmesh", "index", "."], check=True)
    except Exception as e:
        console.print(f"[red]Failed to index codebase: {e}[/red]")
        
    console.print("\n[bold green]Ready![/bold green] Just run `claude` or `claude-mesh` to begin.")

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
