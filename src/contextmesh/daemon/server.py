import logging
from importlib.metadata import version as _pkg_version

try:
    _VERSION = _pkg_version("claude-contextmesh")
except Exception:
    _VERSION = "dev"

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

    try:
        import asyncio
        from contextmesh.graph.watcher import start_watcher
        from contextmesh.graph.repo import init_repo_graph
        from contextmesh.config import get_config
        
        config = get_config()
        repo_graph = init_repo_graph(config.project_path, db)
        start_watcher(repo_graph, config.project_path)
        logger.info("File watcher started for %s", config.project_path)
    except Exception as e:
        logger.warning("File watcher failed to start: %s", e)

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
    app = FastAPI(title="ContextMesh Daemon", version=_VERSION, lifespan=lifespan)

    from fastapi.responses import HTMLResponse

    @app.get("/dashboard")
    async def dashboard():
        """Serve a beautiful Web UI for token savings."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>ContextMesh God-Mode Dashboard</title>
            <style>
                body { font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 30px; }
                .container { max-width: 1200px; margin: 0 auto; }
                h1 { font-size: 2rem; background: linear-gradient(to right, #38bdf8, #818cf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
                .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; margin-top: 20px; }
                .card { background: #1e293b; padding: 20px; border-radius: 12px; border: 1px solid #334155; }
                .card h3 { margin: 0 0 10px 0; color: #94a3b8; font-size: 0.9rem; text-transform: uppercase; }
                .card .value { font-size: 2rem; font-weight: bold; }
                .text-green { color: #4ade80; }
                .text-blue { color: #38bdf8; }
                
                .section { margin-top: 30px; background: #1e293b; padding: 20px; border-radius: 12px; border: 1px solid #334155; }
                .section h2 { margin-top: 0; font-size: 1.2rem; color: #cbd5e1; }
                
                table { width: 100%; border-collapse: collapse; margin-top: 10px; }
                th, td { text-align: left; padding: 12px; border-bottom: 1px solid #334155; font-size: 0.9rem; }
                th { color: #94a3b8; font-weight: normal; }
                td { color: #e2e8f0; }
                
                .chart-container { height: 250px; display: flex; align-items: flex-end; gap: 10px; margin-top: 20px; padding-top: 20px; border-top: 1px solid #334155;}
                .bar-group { display: flex; flex-direction: column; justify-content: flex-end; align-items: center; flex: 1; height: 100%; }
                .bar { width: 100%; background: #4ade80; border-radius: 4px 4px 0 0; min-height: 1px; transition: height 0.3s; }
                .bar-label { font-size: 0.75rem; color: #94a3b8; margin-top: 5px; text-align: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; width: 100%; }
                .bar-val { font-size: 0.75rem; color: #f8fafc; margin-bottom: 5px; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>ContextMesh God-Mode</h1>
                
                <div class="grid">
                    <div class="card">
                        <h3>Total Tokens Sent (Raw)</h3>
                        <div class="value text-blue" id="stat-raw">0</div>
                    </div>
                    <div class="card">
                        <h3>Total Tokens Compressed</h3>
                        <div class="value text-blue" id="stat-compressed">0</div>
                    </div>
                    <div class="card">
                        <h3>Total Tokens Saved</h3>
                        <div class="value text-green" id="stat-saved">0</div>
                    </div>
                    <div class="card">
                        <h3>Est. Cost Saved (USD)</h3>
                        <div class="value text-green" id="stat-cost">$0.0000</div>
                    </div>
                </div>

                <div class="section">
                    <h2>Tokens Saved (Last 10 Turns)</h2>
                    <div class="chart-container" id="chart"></div>
                </div>

                <div class="section">
                    <h2>RTK Interception Log</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Timestamp</th>
                                <th>Session</th>
                                <th>Routed Tokens</th>
                                <th>Saved Tokens</th>
                                <th>Compression %</th>
                                <th>Cost Saved USD</th>
                            </tr>
                        </thead>
                        <tbody id="log-body">
                            <tr><td colspan="6" style="text-align: center;">Waiting for data...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <script>
                async function fetchData() {
                    try {
                        const globRes = await fetch('/savings');
                        const glob = await globRes.json();
                        
                        document.getElementById('stat-raw').innerText = glob.total_accumulated_tokens?.toLocaleString() || '0';
                        document.getElementById('stat-compressed').innerText = glob.total_routed_tokens?.toLocaleString() || '0';
                        document.getElementById('stat-saved').innerText = glob.total_tokens_saved?.toLocaleString() || '0';
                        document.getElementById('stat-cost').innerText = '$' + (glob.total_cost_saved_usd || 0).toFixed(4);

                        let turns = [];
                        try {
                            const turnsRes = await fetch('/savings/proxy_session/turns');
                            if (turnsRes.ok) {
                                turns = await turnsRes.json();
                            }
                        } catch (e) {
                            console.warn("Could not fetch turns for proxy_session", e);
                        }

                        const tbody = document.getElementById('log-body');
                        if (turns && turns.length > 0) {
                            tbody.innerHTML = turns.map(t => {
                                const comp = t.compression_ratio ? (t.compression_ratio * 100).toFixed(1) + '%' : 'N/A';
                                const cost = t.cost_saved_usd ? '$' + t.cost_saved_usd.toFixed(4) : '$0.0000';
                                return `<tr>
                                    <td>${new Date(t.timestamp).toLocaleTimeString()}</td>
                                    <td>${(t.session_id || '').substring(0,8)}...</td>
                                    <td>${t.routed_tokens?.toLocaleString() || 0}</td>
                                    <td class="text-green">${t.tokens_saved?.toLocaleString() || 0}</td>
                                    <td>${comp}</td>
                                    <td class="text-green">${cost}</td>
                                </tr>`;
                            }).join('');
                        } else {
                            tbody.innerHTML = '<tr><td colspan="6" style="text-align: center;">No interception logs yet...</td></tr>';
                        }

                        const chart = document.getElementById('chart');
                        if (turns && turns.length > 0) {
                            const chartData = turns.slice(0, 10).reverse();
                            const maxVal = Math.max(...chartData.map(t => t.tokens_saved || 0), 100);
                            chart.innerHTML = chartData.map((t, i) => {
                                const saved = t.tokens_saved || 0;
                                const height = Math.max((saved / maxVal) * 200, 2);
                                return `
                                    <div class="bar-group">
                                        <div class="bar-val">${saved.toLocaleString()}</div>
                                        <div class="bar" style="height: ${height}px;"></div>
                                        <div class="bar-label">Turn ${chartData.length - i}</div>
                                    </div>
                                `;
                            }).join('');
                        } else {
                            chart.innerHTML = '<div style="color: #94a3b8; width: 100%; text-align: center; margin-bottom: 20px;">No chart data</div>';
                        }

                    } catch (err) {
                        console.error("Dashboard fetch error:", err);
                    }
                }
                
                fetchData();
                setInterval(fetchData, 3000);
            </script>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    from pydantic import BaseModel
    class RepoMapRequest(BaseModel):
        project_path: str

    @app.post("/repomap")
    async def get_repomap(req: RepoMapRequest):
        """Generate and return the AST structural map of the repository."""
        from contextmesh.graph.repo import get_repo_graph, init_repo_graph
        from contextmesh.store.db import get_db
        from contextmesh.graph.repomap import generate_repomap
        import logging
        from pathlib import Path
        
        logger.info(f"Generating RepoMap for {req.project_path}")
        
        try:
            repo_graph = get_repo_graph()
        except RuntimeError:
            repo_graph = init_repo_graph(Path(req.project_path), get_db())
            
        # Ensure it's indexed
        await repo_graph.index_project()
        
        # Generate the map
        map_text = await generate_repomap(req.project_path)
        return {"repomap": map_text}

    @app.post("/hook")
    async def post_hook(event: HookEvent, background_tasks: BackgroundTasks):
        logger.info("Hook event: %s session=%s", event.event_type, event.session_id)
        background_tasks.add_task(handle_hook_event, event)
        return {"status": "ok"}

    @app.get("/health")
    async def get_health():
        return {"status": "ok", "version": _VERSION}

    @app.get("/stats")
    async def get_stats():
        db = get_db()
        try:
            sessions = await db.fetchone("SELECT COUNT(*) as count FROM sessions")
            nodes = await db.fetchone("SELECT COUNT(*) as count FROM nodes")
            tokens = await db.fetchone("SELECT SUM(tokens_saved) as total FROM token_savings")
            # Also count proxy-only turns (where session_id = 'proxy_session')
            proxy_turns = await db.fetchone("SELECT COUNT(*) as count FROM token_savings")
            proxy_saved = await db.fetchone(
                "SELECT SUM(accumulated_session_tokens - routed_tokens) as saved FROM token_savings"
            )
            return {
                "session_count": sessions["count"] if sessions else 0,
                "node_count": nodes["count"] if nodes else 0,
                "total_tokens_saved": (tokens["total"] or 0) if tokens else 0,
                "proxy_turns_tracked": proxy_turns["count"] if proxy_turns else 0,
                "proxy_tokens_saved": int(proxy_saved["saved"] or 0) if proxy_saved else 0,
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
