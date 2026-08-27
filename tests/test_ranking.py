"""Tests for RepoMap ranking.

The benchmark measured the alphabetical, un-ranked map as a net cost
(bench/results/repomap-2026-08-27.json: +45.6% cost, no significant turn
reduction). These tests pin the replacement: a file that is actually
referenced by other files in the project must outrank one that is not,
regardless of where either sorts alphabetically.
"""

import json

import pytest

from contextmesh.graph.ranking import apply_file_ranks, compute_file_ranks
from contextmesh.models.edges import EdgeType, RepoEdge
from contextmesh.models.nodes import NodeType, RepoNode
from contextmesh.store.db import Database
from contextmesh.utils.injector import _build_repomap_from_db, _rank_of


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "cm.db")
    await database.connect()
    yield database
    await database.close()


async def _seed(db, project_path, files: dict[str, str]):
    """Write files to disk and insert matching repo_nodes rows.

    `files` maps relative path -> source text. Each top-level `def NAME(` in
    the text becomes a repo_function node so ranking has symbols to work with.
    """
    import re

    for rel_path, text in files.items():
        full = project_path / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)

        node = RepoNode(
            project_path=str(project_path),
            repo_node_type=NodeType.REPO_FILE,
            name=full.name,
            file_path=rel_path,
        )
        await db.insert("repo_nodes", node.to_db_row())

        for match in re.finditer(r"^def (\w+)\(", text, re.MULTILINE):
            fn = RepoNode(
                project_path=str(project_path),
                repo_node_type=NodeType.REPO_FUNCTION,
                name=match.group(1),
                file_path=rel_path,
                start_line=text[: match.start()].count("\n") + 1,
            )
            await db.insert("repo_nodes", fn.to_db_row())


async def test_referenced_file_outranks_unreferenced_file(db, tmp_path):
    """The property the benchmark result demands: relevance must separate files."""
    await _seed(db, tmp_path, {
        "core.py": "def process_widgets():\n    return 1\n",
        "caller_a.py": "from core import process_widgets\nprocess_widgets()\n",
        "caller_b.py": "from core import process_widgets\nprocess_widgets()\n",
        "orphan.py": "def unrelated_helper():\n    return 2\n",
    })

    ranks = await compute_file_ranks(str(tmp_path), db)

    assert ranks["core.py"] > ranks["orphan.py"]


async def test_empty_project_ranks_nothing(db, tmp_path):
    assert await compute_file_ranks(str(tmp_path), db) == {}


async def test_files_with_no_cross_references_rank_nothing(db, tmp_path):
    """Two isolated files must not error, and must not fabricate a signal."""
    await _seed(db, tmp_path, {
        "a.py": "def alpha():\n    return 1\n",
        "b.py": "def bravo():\n    return 2\n",
    })
    assert await compute_file_ranks(str(tmp_path), db) == {}


async def test_short_names_are_excluded_as_noise(db, tmp_path):
    """'get' or 'run' appearing everywhere must not manufacture false centrality."""
    await _seed(db, tmp_path, {
        "core.py": "def run():\n    return 1\n",
        "caller.py": "run()\nrun()\nrun()\n",
    })
    assert await compute_file_ranks(str(tmp_path), db) == {}


async def test_apply_file_ranks_writes_into_metadata(db, tmp_path):
    await _seed(db, tmp_path, {
        "core.py": "def process_widgets():\n    return 1\n",
        "caller.py": "from core import process_widgets\nprocess_widgets()\n",
    })

    updated = await apply_file_ranks(str(tmp_path), db)
    assert updated > 0

    rows = await db.fetchall(
        "SELECT file_path, metadata FROM repo_nodes WHERE file_path = 'core.py'"
    )
    ranks = [json.loads(r["metadata"])["rank"] for r in rows]
    assert all(r > 0 for r in ranks)


async def test_apply_file_ranks_preserves_existing_metadata(db, tmp_path):
    """Writing rank must not clobber other metadata a node already carries."""
    await _seed(db, tmp_path, {
        "core.py": "def process_widgets():\n    return 1\n",
        "caller.py": "from core import process_widgets\nprocess_widgets()\n",
    })
    await db.execute(
        "UPDATE repo_nodes SET metadata = ? WHERE file_path = 'core.py'",
        (json.dumps({"custom": "keep-me"}),),
    )
    await db.commit()

    await apply_file_ranks(str(tmp_path), db)

    rows = await db.fetchall(
        "SELECT metadata FROM repo_nodes WHERE file_path = 'core.py' AND repo_node_type = 'repo_function'"
    )
    meta = json.loads(rows[0]["metadata"])
    assert meta["custom"] == "keep-me"
    assert meta["rank"] > 0


async def test_apply_file_ranks_is_a_noop_when_nothing_to_rank(db, tmp_path):
    assert await apply_file_ranks(str(tmp_path), db) == 0


# ── injector: rank-ordered selection ────────────────────────────────────────

def test_rank_of_reads_stored_score():
    assert _rank_of(json.dumps({"rank": 0.42})) == 0.42


def test_rank_of_defaults_to_zero_for_unranked_nodes():
    """A project indexed before ranking shipped must degrade, not crash."""
    assert _rank_of(None) == 0.0
    assert _rank_of("{}") == 0.0
    assert _rank_of("not json") == 0.0


def _insert_repomap_row(con, project, file_path, name, node_type, line, rank=None):
    metadata = json.dumps({"rank": rank}) if rank is not None else "{}"
    con.execute(
        "INSERT INTO repo_nodes (node_id, project_path, repo_node_type, name,"
        " file_path, start_line, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"{file_path}:{name}:{line}", project, node_type, name, file_path, line, metadata),
    )


@pytest.fixture
def repomap_db(tmp_path):
    import sqlite3

    from contextmesh.store.schema import CREATE_SCHEMA_SQL

    path = tmp_path / "repomap.db"
    con = sqlite3.connect(path)
    con.executescript(CREATE_SCHEMA_SQL)
    con.commit()
    con.close()
    return path


def test_higher_ranked_file_is_listed_first(repomap_db):
    import sqlite3

    con = sqlite3.connect(repomap_db)
    _insert_repomap_row(con, "/p", "low.py", "low_fn", "repo_function", 1, rank=0.01)
    _insert_repomap_row(con, "/p", "high.py", "high_fn", "repo_function", 1, rank=0.9)
    con.commit()
    con.close()

    repomap = _build_repomap_from_db(str(repomap_db), "/p")

    assert repomap.index("high.py") < repomap.index("low.py")


def test_unranked_project_falls_back_to_alphabetical(repomap_db):
    """Old index data with no rank must not crash or reorder unpredictably."""
    import sqlite3

    con = sqlite3.connect(repomap_db)
    _insert_repomap_row(con, "/p", "b.py", "b_fn", "repo_function", 1)
    _insert_repomap_row(con, "/p", "a.py", "a_fn", "repo_function", 1)
    con.commit()
    con.close()

    repomap = _build_repomap_from_db(str(repomap_db), "/p")

    assert repomap.index("a.py") < repomap.index("b.py")


def test_low_relevance_symbols_are_omitted_not_silently_dropped(repomap_db, monkeypatch):
    """Budget spends on rank first; what doesn't fit is stated, not hidden."""
    import sqlite3

    from contextmesh.utils import injector

    monkeypatch.setattr(injector, "MAX_REPOMAP_CHARS", 130)

    con = sqlite3.connect(repomap_db)
    _insert_repomap_row(con, "/p", "high.py", "important_fn", "repo_function", 1, rank=0.9)
    _insert_repomap_row(con, "/p", "low.py", "unimportant_fn", "repo_function", 1, rank=0.01)
    con.commit()
    con.close()

    repomap = _build_repomap_from_db(str(repomap_db), "/p")

    assert "important_fn" in repomap
    assert "omitted" in repomap
