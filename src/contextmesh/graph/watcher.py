import asyncio
import logging
from pathlib import Path
from watchfiles import awatch
from contextmesh.graph.repo import RepoGraph

logger = logging.getLogger(__name__)

async def watch_and_reindex(repo_graph: RepoGraph, project_path: Path) -> None:
    """
    Watches the project directory for file changes and re-indexes
    changed files in the AST graph automatically.
    Runs as a background asyncio task.
    """
    supported_extensions = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"}
    skip_dirs = {"node_modules", "__pycache__", ".git", "dist", "build", "venv", ".venv"}

    def watch_filter(change, path_str):
        path = Path(path_str)
        # Skip hidden directories
        if any(part.startswith('.') and part != '.' for part in path.parts[:-1]):
            return False
        # Skip specified directories
        if any(part in skip_dirs for part in path.parts):
            return False
        # Only supported extensions
        if path.suffix not in supported_extensions:
            return False
        return True

    while True:
        try:
            async for changes in awatch(project_path, watch_filter=watch_filter):
                for change, path_str in changes:
                    try:
                        file_path = path_str
                        try:
                            file_path = str(Path(path_str).relative_to(project_path))
                        except ValueError:
                            pass
                            
                        await repo_graph.on_file_changed(file_path)
                        logger.info(f'[Watcher] Reindexed {file_path}')
                    except Exception as e:
                        logger.error(f'[Watcher] Error reindexing {path_str}: {e}')
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f'[Watcher] Watcher loop error: {e}')
            await asyncio.sleep(5)

def start_watcher(repo_graph: RepoGraph, project_path: Path) -> asyncio.Task:
    return asyncio.create_task(watch_and_reindex(repo_graph, project_path))
