import json
import httpx
from fastmcp import FastMCP

mcp = FastMCP(name='contextmesh')
DAEMON_URL = "http://localhost:8765"

@mcp.tool()
def get_context(session_id: str, task_hint: str = '', budget_tokens: int = 15000, files_hint: str = '') -> str:
    """Get optimized context projection for the current session."""
    try:
        req = {
            "session_id": session_id,
            "task_hint": task_hint,
            "budget_tokens": budget_tokens,
            "files_hint": files_hint.split(",") if files_hint else []
        }
        # In a real app we would call daemon /context endpoint, mocking for now.
        # But per requirements we call daemon via HTTP:
        with httpx.Client() as client:
            resp = client.post(f"{DAEMON_URL}/context", json=req, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            return data.get("context_text", "No context generated.")
    except Exception as e:
        return f"Error retrieving context. Ensure daemon is running at {DAEMON_URL}: {e}"

@mcp.tool()
def get_project_architecture(project_path: str) -> str:
    """
    Get a dense, structural AST map (RepoMap) of the entire codebase.
    Use this to understand classes, functions, and architecture without reading full files.
    """
    try:
        req = {"project_path": project_path}
        with httpx.Client() as client:
            resp = client.post(f"{DAEMON_URL}/repomap", json=req, timeout=30.0)
            resp.raise_for_status()
            data = resp.json()
            return data.get("repomap", "Failed to generate repomap.")
    except Exception as e:
        return f"Error generating architecture map. Ensure daemon is running: {e}"

@mcp.tool()
def record_decision(session_id: str, content: str, consequence: str = '', files: str = '', symbols: str = '', confidence: float = 0.9) -> str:
    """Record an architectural decision."""
    try:
        req = {
            "event_type": "Notification",
            "session_id": session_id,
            "metadata": {
                "decision": content,
                "consequence": consequence,
                "files": files.split(",") if files else [],
                "symbols": symbols.split(",") if symbols else [],
                "confidence": confidence
            }
        }
        with httpx.Client() as client:
            resp = client.post(f"{DAEMON_URL}/hook", json=req, timeout=5.0)
            resp.raise_for_status()
            return f"Decision recorded: {content[:50]}"
    except Exception as e:
        return f"Error recording decision: {e}"

@mcp.tool()
def get_task_graph(session_id: str) -> str:
    """Get task hierarchy and node counts."""
    try:
        with httpx.Client() as client:
            resp = client.get(f"{DAEMON_URL}/stats", params={"session_id": session_id}, timeout=5.0)
            resp.raise_for_status()
            return json.dumps(resp.json(), indent=2)
    except Exception as e:
        return f"Error retrieving task graph: {e}"

@mcp.tool()
def get_savings_report(session_id: str) -> str:
    """Get formatted token savings summary text."""
    try:
        with httpx.Client() as client:
            resp = client.get(f"{DAEMON_URL}/savings/{session_id}", timeout=5.0)
            resp.raise_for_status()
            summary = resp.json()
            lines = [
                "Token Savings Report",
                f"Total Baseline Tokens: {summary.get('total_accumulated_tokens', 0)}",
                f"Total Routed Tokens: {summary.get('total_routed_tokens', 0)}",
                f"Total Tokens Saved: {summary.get('total_tokens_saved', 0)}",
                f"Net Tokens Saved: {summary.get('total_net_tokens_saved', 0)}",
                f"Cost Saved (USD): ${summary.get('total_cost_saved_usd', 0.0):.4f}",
                f"Compression Ratio: {summary.get('avg_compression_ratio', 1.0):.0%}"
            ]
            return "\n".join(lines)
    except Exception as e:
        return f"Error retrieving savings report: {e}"

@mcp.tool()
def switch_task(session_id: str, new_task_name: str) -> str:
    """Switch to a new task."""
    try:
        req = {
            "session_id": session_id,
            "new_task_name": new_task_name
        }
        with httpx.Client() as client:
            resp = client.post(f"{DAEMON_URL}/task/switch", json=req, timeout=5.0)
            resp.raise_for_status()
            return f"Task switched to: {new_task_name}"
    except Exception as e:
        return f"Error switching task: {e}"

def get_mcp_app() -> FastMCP:
    return mcp

if __name__ == '__main__':
    mcp.run()
