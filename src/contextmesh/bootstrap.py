"""
ContextMesh — Main entry point and integration.

Wires all components in dependency order. Called by the daemon server on startup.
All init_* functions are synchronous — bootstrap() is the only async entry point.
"""

from __future__ import annotations

import logging
from pathlib import Path

from contextmesh.config import get_config
from contextmesh.store.db import Database, get_db

logger = logging.getLogger(__name__)


async def bootstrap(project_path: Path | None = None) -> Database:
    """
    Initialize all ContextMesh components in dependency order.
    Returns the already-initialized Database (init_db called by daemon before this).
    """
    cfg = get_config()
    if project_path:
        cfg.project_path = project_path

    # DB is already initialized by daemon startup — just fetch it
    try:
        db = get_db()
    except RuntimeError:
        from contextmesh.store.db import init_db
        db_path = cfg.data_dir / "contextmesh.db"
        db = await init_db(db_path)
    logger.info("Database ready at %s", cfg.data_dir / "contextmesh.db")

    # 2. Embeddings store (sync init)
    try:
        from contextmesh.embeddings.store import init_store
        init_store(cfg.embeddings, db)
        logger.info("Embedding store initialized (model: %s)", cfg.embeddings.model)
    except Exception as e:
        logger.warning("Embeddings store not available: %s", e)

    # 3. Session graph (sync init)
    try:
        from contextmesh.graph.session import init_session_graph
        init_session_graph(db)
        logger.info("Session graph initialized")
    except Exception as e:
        logger.warning("Session graph not available: %s", e)

    # 4. Repo graph (sync init — indexing is separate and expensive)
    try:
        from contextmesh.graph.repo import init_repo_graph
        init_repo_graph(cfg.project_path, db)
        logger.info("Repo graph initialized for %s", cfg.project_path)
    except Exception as e:
        logger.warning("Repo graph not available: %s", e)

    # 5. Task detector (sync init — needs embeddings store)
    try:
        from contextmesh.embeddings.store import get_store
        from contextmesh.tasks.detector import init_detector
        init_detector(cfg.tasks, get_store(), db)
        logger.info("Task detector initialized")
    except Exception as e:
        logger.warning("Task detector not available: %s", e)

    # 6. Task hierarchy (sync init — needs session graph)
    try:
        from contextmesh.graph.session import get_session_graph
        from contextmesh.tasks.hierarchy import init_hierarchy
        init_hierarchy(db, get_session_graph())
        logger.info("Task hierarchy initialized")
    except Exception as e:
        logger.warning("Task hierarchy not available: %s", e)

    # 7. Event handler (sync init)
    try:
        from contextmesh.daemon.handlers import init_handler
        init_handler(db, cfg)
        logger.info("Event handler initialized")
    except Exception as e:
        logger.warning("Event handler not available: %s", e)

    # 8. Token savings tracker (sync init)
    try:
        from contextmesh.tracker.savings import init_tracker
        init_tracker(db, cfg.tracker)
        logger.info("Savings tracker initialized")
    except Exception as e:
        logger.warning("Savings tracker not available: %s", e)

    logger.info("ContextMesh bootstrap complete")
    return db


def setup_logging(level: str = "info") -> None:
    """Configure structured logging."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
    logging.getLogger("transformers").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
