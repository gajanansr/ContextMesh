import json
import logging
import os
import aiosqlite

logger = logging.getLogger(__name__)

async def get_last_session_summary(db_path: str) -> str | None:
    """
    Queries the SQLite DB for the most recent session and returns
    a compact, structured summary string.
    Returns None if no previous sessions exist.
    """
    if not os.path.exists(db_path):
        return None

    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            
            # Find the most recent session
            async with db.execute(
                "SELECT session_id, project_path, last_active FROM sessions ORDER BY last_active DESC LIMIT 1"
            ) as cursor:
                session = await cursor.fetchone()
                if not session:
                    return None
            
            session_id = session["session_id"]
            project_path = session["project_path"]
            
            # Parse last_active for nicer formatting if it's ISO format
            last_active = session["last_active"]
            if "T" in last_active:
                last_active = last_active.replace("T", " at ")[:19] + " UTC"

            # Get top 10 most important nodes for that session
            async with db.execute(
                """
                SELECT summary, content, files_involved 
                FROM nodes 
                WHERE session_id = ? AND tier IN ('hot', 'warm')
                ORDER BY importance DESC 
                LIMIT 10
                """,
                (session_id,)
            ) as cursor:
                nodes = await cursor.fetchall()

            if not nodes:
                return None

            key_context = []
            files_touched = set()

            for node in nodes:
                # Use summary if available, else a shortened content
                text = node["summary"] or node["content"]
                if text:
                    # Clean up newlines for a compact list
                    text = text.replace('\n', ' ').strip()
                    if len(text) > 150:
                        text = text[:147] + "..."
                    key_context.append(f"- {text}")
                
                if node["files_involved"]:
                    try:
                        files = json.loads(node["files_involved"])
                        if isinstance(files, list):
                            files_touched.update(files)
                    except json.JSONDecodeError:
                        pass
            
            if not key_context:
                return None
            
            summary = [
                "[ContextMesh Session Memory]",
                f"Last session: {last_active}",
                f"Project: {project_path}",
                "Key context from last session:"
            ]
            summary.extend(key_context)
            if files_touched:
                summary.append(f"Files touched: {', '.join(sorted(files_touched))}")
            summary.append("[End of session memory. You are now continuing this work.]")

            return "\n".join(summary)

    except Exception as e:
        logger.error(f"Error generating session summary: {e}")
        return None
