import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, HTTPException

from contextmesh.config import Config, get_config, load_config, set_config
from contextmesh.store.db import Database, get_db, init_db
from contextmesh.models.nodes import HookEvent, ContextRequest, ContextResponse
from contextmesh.daemon.handlers import handle_hook_event, init_handler

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    cfg = load_config()
    set_config(cfg)

    db_path = cfg.data_dir / "contextmesh.db"
    db = await init_db(db_path)

    init_handler(db, cfg)

    # Try to bootstrap all optional components
    try:
        from contextmesh.bootstrap import bootstrap
        await bootstrap(cfg.project_path)
    except Exception as e:
        logger.warning("Full bootstrap failed (%s) — using minimal mode", e)

    yield

    # Shutdown
    try:
        db_instance = get_db()
        await db_instance.close()
    except Exception:
        pass


def create_app() -> FastAPI:
    app = FastAPI(title="ContextMesh Daemon", version="0.1.0", lifespan=lifespan)

    from fastapi.responses import HTMLResponse

    @app.get("/dashboard")
    async def dashboard():
        """Serve a beautiful Web UI for token savings."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>ContextMesh Dashboard</title>
            <style>
                body { font-family: -apple-system, system-ui, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 40px; }
                .container { max-width: 1000px; margin: 0 auto; }
                h1 { font-size: 2.5rem; background: linear-gradient(to right, #38bdf8, #818cf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
                .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-top: 30px; }
                .card { background: #1e293b; padding: 25px; border-radius: 12px; border: 1px solid #334155; }
                .card h3 { margin: 0 0 10px 0; color: #94a3b8; font-size: 0.9rem; text-transform: uppercase; letter-spacing: 1px; }
                .card .value { font-size: 2.5rem; font-weight: bold; color: #f8fafc; }
                .card .value.green { color: #4ade80; }
                .log-section { margin-top: 40px; background: #1e293b; padding: 25px; border-radius: 12px; border: 1px solid #334155; }
                pre { background: #0f172a; padding: 15px; border-radius: 8px; color: #a78bfa; overflow-x: auto; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>ContextMesh V2</h1>
                <p style="color: #cbd5e1;">Live Token Compression & Savings Dashboard</p>
                
                <div class="grid">
                    <div class="card">
                        <h3>Tokens Sent (Raw)</h3>
                        <div class="value" id="raw-tokens">--</div>
                    </div>
                    <div class="card">
                        <h3>Tokens Sent (Compressed)</h3>
                        <div class="value" id="routed-tokens">--</div>
                    </div>
                    <div class="card">
                        <h3>Tokens Saved</h3>
                        <div class="value green" id="saved-tokens">--</div>
                    </div>
                    <div class="card">
                        <h3>Est. Cost Saved</h3>
                        <div class="value green" id="cost-saved">$--</div>
                    </div>
                </div>

                <div class="log-section">
                    <h3>Recent RTK Interceptions</h3>
                    <pre id="logs">Waiting for Claude Code commands...</pre>
                </div>
            </div>

            <script>
                async function fetchStats() {
                    try {
                        const res = await fetch('/savings');
                        const data = await res.json();
                        
                        document.getElementById('raw-tokens').innerText = data.total_accumulated_tokens.toLocaleString();
                        document.getElementById('routed-tokens').innerText = data.total_routed_tokens.toLocaleString();
                        document.getElementById('saved-tokens').innerText = data.total_tokens_saved.toLocaleString();
                        document.getElementById('cost-saved').innerText = '$' + data.total_cost_saved_usd.toFixed(4);
                        
                        if (data.total_tokens_saved > 0) {
                            document.getElementById('logs').innerText = `[ContextMesh RTK] Intercepted outbound payload!\nSuccessfully crushed ${data.total_tokens_saved.toLocaleString()} tokens of unstructured noise.`;
                        }
                    } catch (e) {
                        console.error(e);
                    }
                }
                setInterval(fetchStats, 2000);
                fetchStats();
            </script>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    @app.post("/hook")
    async def post_hook(event: HookEvent, background_tasks: BackgroundTasks):
        logger.info("Hook event: %s session=%s", event.event_type, event.session_id)
        background_tasks.add_task(handle_hook_event, event)
        return {"status": "ok"}

    @app.get("/health")
    async def get_health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/stats")
    async def get_stats():
        db = get_db()
        try:
            sessions = await db.fetchone("SELECT COUNT(*) as count FROM sessions")
            nodes = await db.fetchone("SELECT COUNT(*) as count FROM nodes")
            tokens = await db.fetchone("SELECT SUM(tokens_saved) as total FROM token_savings")
            return {
                "session_count": sessions["count"] if sessions else 0,
                "node_count": nodes["count"] if nodes else 0,
                "total_tokens_saved": tokens["total"] if tokens and tokens["total"] else 0,
            }
        except Exception as e:
            logger.error("Error getting stats: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error")

    @app.get("/savings")
    async def get_savings_global():
        """Global token savings summary across all sessions."""
        db = get_db()
        try:
            rows = await db.fetchall("SELECT * FROM token_savings")
            sessions = await db.fetchall("SELECT COUNT(*) as count FROM sessions")
            session_count = sessions[0]["count"] if sessions else 0
            if not rows:
                return {
                    "session_count": session_count,
                    "total_turns": 0, "total_accumulated_tokens": 0,
                    "total_routed_tokens": 0, "total_tokens_saved": 0,
                    "total_net_tokens_saved": 0, "avg_compression_ratio": 1.0,
                    "total_cost_saved_usd": 0.0, "best_turn_savings": 0,
                }
            tot_accum = sum(r["accumulated_session_tokens"] for r in rows)
            tot_routed = sum(r["routed_tokens"] for r in rows)
            tot_saved = sum(r["tokens_saved"] for r in rows)
            tot_net = sum(r["net_tokens_saved"] for r in rows)
            cost_saved = sum(r["cost_saved_usd"] for r in rows)
            best = max((r["tokens_saved"] for r in rows), default=0)
            avg_ratio = (tot_routed / tot_accum) if tot_accum > 0 else 1.0
            return {
                "session_count": session_count,
                "total_turns": len(rows),
                "total_accumulated_tokens": tot_accum,
                "total_routed_tokens": tot_routed,
                "total_tokens_saved": tot_saved,
                "total_net_tokens_saved": tot_net,
                "avg_compression_ratio": avg_ratio,
                "total_cost_saved_usd": cost_saved,
                "best_turn_savings": best,
            }
        except Exception as e:
            logger.error("Error getting global savings: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/savings/{session_id}")
    async def get_savings(session_id: str):
        """Token savings summary for a session — called by MCP get_savings_report tool."""
        db = get_db()
        try:
            rows = await db.fetchall(
                "SELECT * FROM token_savings WHERE session_id = ?", (session_id,)
            )
            if not rows:
                return {
                    "session_id": session_id, "total_turns": 0,
                    "total_accumulated_tokens": 0, "total_routed_tokens": 0,
                    "total_tokens_saved": 0, "total_net_tokens_saved": 0,
                    "avg_compression_ratio": 1.0, "total_cost_saved_usd": 0.0,
                    "best_turn_savings": 0,
                }
            tot_accum = sum(r["accumulated_session_tokens"] for r in rows)
            tot_routed = sum(r["routed_tokens"] for r in rows)
            tot_saved = sum(r["tokens_saved"] for r in rows)
            tot_net = sum(r["net_tokens_saved"] for r in rows)
            cost_saved = sum(r["cost_saved_usd"] for r in rows)
            best = max((r["tokens_saved"] for r in rows), default=0)
            avg_ratio = (tot_routed / tot_accum) if tot_accum > 0 else 1.0
            return {
                "session_id": session_id,
                "total_turns": len(rows),
                "total_accumulated_tokens": tot_accum,
                "total_routed_tokens": tot_routed,
                "total_tokens_saved": tot_saved,
                "total_net_tokens_saved": tot_net,
                "avg_compression_ratio": avg_ratio,
                "total_cost_saved_usd": cost_saved,
                "best_turn_savings": best,
            }
        except Exception as e:
            logger.error("Error getting savings: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/savings/{session_id}/turns")
    async def get_savings_turns(session_id: str, limit: int = 20):
        """Per-turn savings data for the turns table view."""
        db = get_db()
        try:
            rows = await db.fetchall(
                """SELECT ts.*, t.name as task_name
                   FROM token_savings ts
                   LEFT JOIN tasks t ON ts.task_id = t.task_id
                   WHERE ts.session_id = ?
                   ORDER BY ts.timestamp DESC LIMIT ?""",
                (session_id, limit),
            )
            return rows
        except Exception as e:
            logger.error("Error getting turns: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/tasks/{session_id}")
    async def get_tasks(session_id: str):
        """Task hierarchy for a session."""
        db = get_db()
        try:
            task_rows = await db.fetchall(
                "SELECT * FROM tasks WHERE session_id = ? ORDER BY last_active DESC",
                (session_id,),
            )
            tasks_out = []
            for t in task_rows:
                node_count_row = await db.fetchone(
                    "SELECT COUNT(*) as c FROM nodes WHERE task_id = ?", (t["task_id"],)
                )
                tasks_out.append({
                    "task_id": t["task_id"],
                    "name": t["name"],
                    "tier": t["tier"],
                    "status": t["status"],
                    "task_type": t["task_type"],
                    "started_at": t["started_at"],
                    "last_active": t.get("last_active"),
                    "node_count": node_count_row["c"] if node_count_row else 0,
                })
            return {"session_id": session_id, "tasks": tasks_out}
        except Exception as e:
            logger.error("Error getting tasks: %s", e)
            raise HTTPException(status_code=500, detail=str(e))



    @app.post("/context")
    async def post_context(request: ContextRequest) -> ContextResponse:
        db = get_db()
        # Try real assembler first, fall back to stub
        try:
            from contextmesh.router.assembler import ContextAssembler
            from contextmesh.embeddings.store import get_store
            from contextmesh.graph.session import get_session_graph
            from contextmesh.graph.repo import get_repo_graph
            assembler = ContextAssembler(
                get_config().router, db,
                get_store(), get_session_graph(), get_repo_graph(),
            )
            response = await assembler.assemble(request)
        except Exception as e:
            logger.warning("Real assembler failed (%s), using stub", e)
            response = await _assemble_context_stub(request, db)

        # Record savings
        try:
            from contextmesh.tracker.savings import get_tracker
            tracker = get_tracker()
            task = await db.get_active_task(request.session_id)
            task_id = task.get("task_id") if task else None
            await tracker.record_turn(request.session_id, task_id, response)
        except Exception as e:
            logger.warning("Savings tracking failed: %s", e)

        return response

    return app


# WSGI-compatible app object for uvicorn
app = create_app()


async def _assemble_context_stub(request: ContextRequest, db: Database) -> ContextResponse:
    """Stub assembler — returns hot nodes when real assembler unavailable."""
    nodes = await db.get_session_nodes(request.session_id, tier="hot", limit=50)
    parts = []
    total_tokens = 0
    for n in nodes:
        text = n.get("summary") or n["content"]
        tokens = n.get("token_count", 0)
        total_tokens += tokens
        if total_tokens > request.budget_tokens:
            break
        parts.append(text)
    accumulated = await db.get_cumulative_tokens(request.session_id)
    return ContextResponse(
        session_id=request.session_id,
        context_text="\n\n---\n\n".join(parts),
        total_tokens=total_tokens,
        hot_tokens=total_tokens,
        accumulated_session_tokens=accumulated,
        tokens_saved=max(0, accumulated - total_tokens),
        compression_ratio=(total_tokens / accumulated) if accumulated > 0 else 1.0,
    )
