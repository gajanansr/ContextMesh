from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from contextmesh.models.nodes import ContextResponse

console = Console()

def print_session_report(summary: dict) -> None:
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Metric")
    table.add_column("Value", justify="right")

    ratio = summary.get("avg_compression_ratio", 1.0)
    color = "red"
    if ratio < 0.3: color = "bright_green"
    elif ratio < 0.5: color = "green"
    elif ratio < 0.7: color = "yellow"

    table.add_row("Turns tracked", str(summary.get("total_turns", 0)))
    table.add_row("Total baseline tokens", f"{summary.get('total_accumulated_tokens', 0):,}")
    table.add_row("Total routed tokens", f"{summary.get('total_routed_tokens', 0):,}")
    table.add_row("Tokens saved", f"[green]{summary.get('total_tokens_saved', 0):,}[/green]")
    table.add_row("Net saved (after overhead)", f"[green]{summary.get('total_net_tokens_saved', 0):,}[/green]")
    table.add_row("Avg compression ratio", f"[{color}]{ratio:.0%}[/{color}]")
    table.add_row("Estimated cost saved", f"[yellow]${summary.get('total_cost_saved_usd', 0.0):.4f}[/yellow]")

    console.print(Panel(table, title="ContextMesh Token Savings Report", expand=False))

def print_global_report(summary: dict) -> None:
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Metric")
    table.add_column("Value", justify="right")

    ratio = summary.get("avg_compression_ratio", 1.0)
    color = "red"
    if ratio < 0.3: color = "bright_green"
    elif ratio < 0.5: color = "green"
    elif ratio < 0.7: color = "yellow"

    table.add_row("Sessions tracked", str(summary.get("session_count", 0)))
    table.add_row("Turns tracked", str(summary.get("total_turns", 0)))
    table.add_row("Total baseline tokens", f"{summary.get('total_accumulated_tokens', 0):,}")
    table.add_row("Total routed tokens", f"{summary.get('total_routed_tokens', 0):,}")
    table.add_row("Tokens saved", f"[green]{summary.get('total_tokens_saved', 0):,}[/green]")
    table.add_row("Net saved (after overhead)", f"[green]{summary.get('total_net_tokens_saved', 0):,}[/green]")
    table.add_row("Avg compression ratio", f"[{color}]{ratio:.0%}[/{color}]")
    table.add_row("Estimated cost saved", f"[yellow]${summary.get('total_cost_saved_usd', 0.0):.4f}[/yellow]")

    console.print(Panel(table, title="ContextMesh Global Token Savings Report", expand=False))

def print_turn_table(turns: list[dict]) -> None:
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Timestamp")
    table.add_column("Task")
    table.add_column("Accumulated", justify="right")
    table.add_column("Routed", justify="right")
    table.add_column("Saved", justify="right")
    table.add_column("Ratio", justify="right")
    table.add_column("Cost Saved", justify="right")

    for turn in turns:
        saved = turn.get("tokens_saved", 0)
        color = "green" if saved > 0 else "red"
        
        table.add_row(
            turn.get("timestamp", "")[:19].replace("T", " "),
            turn.get("task_id", "None") or "None",
            f"{turn.get('accumulated_session_tokens', 0):,}",
            f"{turn.get('routed_tokens', 0):,}",
            f"[{color}]{saved:,}[/{color}]",
            f"{turn.get('compression_ratio', 1.0):.0%}",
            f"[yellow]${turn.get('cost_saved_usd', 0.0):.4f}[/yellow]"
        )
        
    console.print(table)

def print_live_savings(response: ContextResponse, accumulated: int) -> None:
    saved = max(0, accumulated - response.total_tokens)
    ratio = response.total_tokens / accumulated if accumulated > 0 else 1.0
    cost_saved = saved * 3.0 / 1000000.0  # Just a basic estimation for log
    console.print(f"📊 Turn: {response.total_tokens}t / {accumulated}t baseline → saved {saved}t ({ratio:.0%}) | ${cost_saved:.4f} saved")
