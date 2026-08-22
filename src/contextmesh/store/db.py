"""Async SQLite database wrapper for ContextMesh."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from contextmesh.store.schema import CREATE_SCHEMA_SQL, SCHEMA_VERSION

logger = logging.getLogger(__name__)


class Database:
    """Async SQLite wrapper with connection pooling via a single persistent connection."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the database connection and ensure schema is up to date."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        await self._apply_schema()
        logger.info("Database connected: %s", self.db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    async def _apply_schema(self) -> None:
        """Apply schema if not already at current version."""
        await self._conn.executescript(CREATE_SCHEMA_SQL)

        # Check / record version
        async with self._conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            current = row["version"] if row else 0

        if current < SCHEMA_VERSION:
            from datetime import datetime, timezone
            await self._conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
            )
            await self._conn.commit()
            logger.info("Schema migrated to version %d", SCHEMA_VERSION)

    # ──────────────────────────────────────────────────
    # Generic helpers
    # ──────────────────────────────────────────────────

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)

    async def executemany(self, sql: str, params_seq: list[tuple]) -> None:
        await self.conn.executemany(sql, params_seq)

    async def commit(self) -> None:
        await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def insert(self, table: str, data: dict[str, Any]) -> None:
        """Insert a single row. JSON-encodes any dict/list values."""
        processed = {k: _json_encode(v) for k, v in data.items()}
        cols = ", ".join(processed.keys())
        placeholders = ", ".join("?" * len(processed))
        sql = f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})"
        await self.conn.execute(sql, tuple(processed.values()))
        await self.conn.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Context manager for explicit transactions."""
        async with self.conn:
            yield

    # ──────────────────────────────────────────────────
    # Domain-specific helpers
    # ──────────────────────────────────────────────────

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return await self.fetchone(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )

    async def upsert_session(self, session_id: str, project_path: str, metadata: dict | None = None) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await self.execute(
            """
            INSERT INTO sessions(session_id, project_path, started_at, last_active, metadata)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET last_active = excluded.last_active
            """,
            (session_id, project_path, now, now, json.dumps(metadata or {})),
        )
        await self.commit()

    async def touch_session(self, session_id: str) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await self.execute(
            "UPDATE sessions SET last_active = ? WHERE session_id = ?", (now, session_id)
        )
        await self.commit()

    async def get_active_task(self, session_id: str) -> dict[str, Any] | None:
        return await self.fetchone(
            "SELECT * FROM tasks WHERE session_id = ? AND tier = 'hot' ORDER BY last_active DESC LIMIT 1",
            (session_id,),
        )

    async def get_session_nodes(
        self,
        session_id: str,
        tier: str | None = None,
        task_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        conditions = ["session_id = ?"]
        params: list[Any] = [session_id]
        if tier:
            conditions.append("tier = ?")
            params.append(tier)
        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)
        where = " AND ".join(conditions)
        return await self.fetchall(
            f"SELECT * FROM nodes WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        )

    async def get_cumulative_tokens(self, session_id: str) -> int:
        """Total accumulated tokens for this session (baseline for savings calc)."""
        row = await self.fetchone(
            "SELECT MAX(cumulative_tokens) as total FROM session_accumulator WHERE session_id = ?",
            (session_id,),
        )
        return row["total"] or 0 if row else 0

    async def record_accumulator_turn(
        self,
        session_id: str,
        role: str,
        token_count: int,
        content_preview: str = "",
    ) -> int:
        """Append a turn to the session accumulator. Returns new cumulative total."""
        current = await self.get_cumulative_tokens(session_id)
        new_total = current + token_count

        row = await self.fetchone(
            "SELECT MAX(turn_index) as max_idx FROM session_accumulator WHERE session_id = ?",
            (session_id,),
        )
        next_idx = (row["max_idx"] or 0) + 1 if row else 1

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await self.execute(
            """
            INSERT INTO session_accumulator
              (session_id, turn_index, role, content_preview, token_count, cumulative_tokens, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, next_idx, role, content_preview[:200], token_count, new_total, now),
        )
        await self.commit()
        return new_total

    async def record_turn_savings(
        self,
        turn_id: str,
        session_id: str,
        task_id: str | None,
        accumulated_tokens: int,
        routed_tokens: int,
        mcp_overhead_tokens: int,
        hot_tokens: int,
        warm_tokens: int,
        cold_tokens: int,
        repo_tokens: int,
        input_price_per_mtok: float,
        included_nodes: list[str] | None = None,
    ) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await self.execute(
            """
            INSERT OR REPLACE INTO token_savings
              (turn_id, session_id, task_id, timestamp,
               accumulated_session_tokens, routed_tokens, mcp_overhead_tokens,
               hot_tokens, warm_tokens, cold_tokens, repo_tokens,
               input_price_per_mtok, included_nodes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                turn_id, session_id, task_id, now,
                accumulated_tokens, routed_tokens, mcp_overhead_tokens,
                hot_tokens, warm_tokens, cold_tokens, repo_tokens,
                input_price_per_mtok,
                json.dumps(included_nodes or []),
            ),
        )
        await self.commit()


def _json_encode(v: Any) -> Any:
    """Encode dicts and lists as JSON strings; pass other types through."""
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return v


# ──────────────────────────────────────────────────
# Global singleton (initialized by daemon startup)
# ──────────────────────────────────────────────────
_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db


async def init_db(db_path: Path) -> Database:
    global _db
    _db = Database(db_path)
    await _db.connect()
    return _db
