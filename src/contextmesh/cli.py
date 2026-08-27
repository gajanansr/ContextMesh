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
def stop() -> None:
    """Stop the ContextMesh background daemon (file watcher & web UI)."""
    import subprocess
    import socket
    
    def _is_running(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    if _is_running(8765):
        subprocess.run(["pkill", "-f", "contextmesh.daemon"], capture_output=True)
        subprocess.run(["pkill", "-f", "contextmesh start"], capture_output=True)
        console.print("[green]✓[/green] ContextMesh daemon stopped successfully.")
    else:
        console.print("[dim]Daemon is not running.[/dim]")

# ──────────────────────────────────────────────────────────────────────────────
# stats
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--session", default=None, help="Session ID (omit for global summary)")
def stats(session: str | None) -> None:
    """Show real-time token savings from the Hook Engine."""
    from rich.table import Table
    from rich.panel import Panel
    import sqlite3
    from contextmesh.config import get_config
    
    db_path = str(get_config().data_dir / "contextmesh.db")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    
    try:
        if session:
            rows = con.execute("SELECT * FROM proxy_measurements WHERE session_id = ?", (session,)).fetchall()
            turns = len(rows)
            sessions = 1 if turns > 0 else 0
            orig = sum(r["original_input_tokens"] or 0 for r in rows)
            routed = sum(r["input_tokens"] or 0 for r in rows)
            saved = sum(r["rtk_tokens_saved"] or 0 for r in rows)
        else:
            row = con.execute("""
                SELECT 
                    COUNT(*) as turns,
                    COUNT(DISTINCT session_id) as sessions,
                    SUM(original_input_tokens) as orig,
                    SUM(input_tokens) as routed,
                    SUM(rtk_tokens_saved) as saved
                FROM proxy_measurements
            """).fetchone()
            turns = row["turns"] or 0
            sessions = row["sessions"] or 0
            orig = row["orig"] or 0
            routed = row["routed"] or 0
            saved = row["saved"] or 0
            
        if turns == 0:
            console.print("[yellow]No token tracking data found yet.[/yellow]")
            return

        # Only measured quantities are reported. The previous version added an
        # invented (files * 10 + symbols * 5) "averted exploration" term that
        # supplied ~99.5% of its own headline; it was unfalsifiable and it made
        # regressions invisible, because the fake term dwarfed the real one.
        # Use `bench/` for a controlled before/after comparison.
        memory_nodes = con.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
        memory_sessions = con.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]

        title = f"ContextMesh (Session: {session[:8]})" if session else "ContextMesh (Global)"
        table = Table(title=title)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green", justify="right")

        table.add_row("Sessions tracked", f"{sessions:,}")
        table.add_row("Tool calls intercepted", f"{turns:,}")
        table.add_row("Output chars before compression", f"{orig * 4:,}")
        table.add_row("Output chars after compression", f"{routed * 4:,}")
        table.add_row("Tokens saved on tool output", f"[bold]{saved:,}[/bold]")
        table.add_row("", "")
        table.add_row("Sessions in memory", f"{memory_sessions:,}")
        table.add_row("Knowledge nodes stored", f"{memory_nodes:,}")

        console.print(Panel(table, border_style="blue"))
        console.print(
            "[dim]Tool-output savings are measured. They exclude what the RepoMap\n"
            "and memory injection cost, so this is not a net figure — run the\n"
            "harness in bench/ for a controlled net comparison.[/dim]"
        )
    except Exception as e:
        console.print(f"[red]Error reading stats:[/red] {e}")
    finally:
        con.close()

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
    import sys
    from contextmesh.utils.executor import execute

    execution = execute(command)
    raw_output = execution.output

    if not raw_output.strip():
        raw_output = "(Command executed successfully with no output)"

    # 3. Compress (RTK style) to out-of-band digest
    original_chars = len(raw_output)
    if original_chars > 12_000:
        import hashlib, os
        digest = hashlib.md5(command.encode()).hexdigest()[:8]
        out_dir = get_config().data_dir / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{digest}.txt"
        out_file.write_text(raw_output)
        
        compressed = (
            raw_output[:3000] 
            + f"\n\n... [ContextMesh RTK Compressor: Full output saved to {out_file} to save tokens. Use `cat` or `head`/`tail` on that file to read the rest.] ...\n" 
        )
    else:
        compressed = raw_output

    saved_chars = original_chars - len(compressed)
    rtk_tokens_saved = saved_chars // 4
    final_output = compressed

    # 4. Inject RepoMap (Turn 1 only) & Write Stats
    try:
        db_path = str(get_config().data_dir / "contextmesh.db")
        
        # Inject RepoMap ONLY on the very first tool call of the session
        try:
            con = sqlite3.connect(db_path, timeout=5)
            turn_count = con.execute("SELECT COUNT(*) FROM proxy_measurements WHERE session_id = ?", (session,)).fetchone()[0]
            con.close()
            
            if turn_count == 0:
                import os
                repomap = _build_repomap_from_db(db_path, os.getcwd())
                if repomap:
                    final_output = "[ContextMesh: Injected full codebase RepoMap to prevent blind exploration. See earlier in output.]\n\n=== COMMAND OUTPUT ===\n" + final_output
                    final_output = repomap + "\n\n" + final_output
        except Exception as e:
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
    sys.exit(execution.returncode)



@main.command()
@click.argument("transcript", type=click.Path(exists=True))
@click.option("--session", required=True, help="Session ID the transcript belongs to")
@click.option("--project", default=".", help="Project root the session ran in")
def harvest(transcript: str, session: str, project: str) -> None:
    """Extract knowledge nodes from a session transcript into the graph.

    Runs automatically on SessionEnd; this is for backfilling old sessions.
    """
    from collections import Counter

    from contextmesh.config import get_config
    from contextmesh.memory.extractor import extract_nodes
    from contextmesh.memory.store import save_nodes

    project_path = str(Path(project).resolve())
    nodes = extract_nodes(transcript, session, project_path)
    if not nodes:
        console.print("[yellow]Nothing extractable in that transcript.[/yellow]")
        return

    saved = save_nodes(str(get_config().data_dir / "contextmesh.db"), session, project_path, nodes)
    console.print(f"[green]Harvested[/green] {saved} nodes from session {session[:8]}")
    for node_type, count in Counter(n.node_type.value for n in nodes).most_common():
        console.print(f"  {count:4d}  {node_type}")


@main.command()
@click.option("--project", default=".", help="Project root to recall for")
@click.option("--prompt", default="", help="Prompt to rank memory against")
def recall(project: str, prompt: str) -> None:
    """Print the memory block that would be injected into a new session."""
    from contextmesh.config import get_config
    from contextmesh.memory.recall import build_recall_context

    context = build_recall_context(
        db_path=str(get_config().data_dir / "contextmesh.db"),
        project_path=str(Path(project).resolve()),
        prompt=prompt,
    )
    if not context:
        console.print("[dim]No memory recorded for this project yet.[/dim]")
        return
    console.print(context)
    console.print(f"\n[dim]{len(context)} chars (~{len(context)//4} tokens)[/dim]")


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
    """Check if ContextMesh hooks are installed and active."""
    import json
    from pathlib import Path
    
    settings_path = Path.home() / ".claude" / "settings.json"
    active = False
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
            pre_tool = data.get("hooks", {}).get("PreToolUse", [])
            for entry in pre_tool:
                for h in entry.get("hooks", []):
                    if h.get("command") == "contextmesh-hook":
                        active = True
                        break
        except Exception:
            pass
            
    if active:
        console.print("[bold green]✓ ContextMesh Hook Engine is active[/bold green]")
        console.print("  It is natively intercepting Claude Code commands.")
        import sqlite3
        from contextmesh.config import get_config
        db_path = str(get_config().data_dir / "contextmesh.db")
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute("SELECT SUM(rtk_tokens_saved) as saved FROM proxy_measurements").fetchone()
            saved = row["saved"] or 0
            
            sessions = con.execute("SELECT COUNT(DISTINCT session_id) as c FROM proxy_measurements").fetchone()["c"] or 0
            nodes = con.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"] or 0
            console.print(f"  [cyan]Tool output: {saved:,} tokens saved across {sessions:,} sessions[/cyan]")
            console.print(f"  [cyan]Memory: {nodes:,} knowledge nodes recalled across sessions[/cyan]")
        except Exception:
            pass
        finally:
            con.close()
    else:
        console.print("[bold red]✗ ContextMesh Hook Engine is not active[/bold red]")
        console.print("  Run [bold cyan]contextmesh init[/bold cyan] to enable it.")

if __name__ == "__main__":
    main()