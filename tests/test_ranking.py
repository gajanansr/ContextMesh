"""Tests for query-personalized RepoMap ranking.

Two benchmarked failures shaped this: alphabetical ordering (+45.6% cost, no
turn benefit) and a static global rank (+74.6%, worse). Both picked content
without reference to the question being asked. The property that matters most
here — and the one neither predecessor had — is that the *same* codebase must
rank differently for different prompts.
"""

import sqlite3

import pytest

from contextmesh.graph.ranking import (
    _identifier_multiplier,
    mentioned_identifiers,
    mentioned_paths,
    rank_symbols,
)
from contextmesh.store.schema import CREATE_SCHEMA_SQL
from contextmesh.utils.injector import _build_repomap_from_db

PROJECT = "/proj"


@pytest.fixture
def con(tmp_path):
    connection = sqlite3.connect(tmp_path / "cm.db")
    connection.row_factory = sqlite3.Row
    connection.executescript(CREATE_SCHEMA_SQL)
    connection.commit()
    yield connection
    connection.close()


def add_definition(con, file_path, name, kind="repo_function", line=1):
    con.execute(
        "INSERT INTO repo_nodes (node_id, project_path, repo_node_type, name,"
        " file_path, start_line, metadata) VALUES (?, ?, ?, ?, ?, ?, '{}')",
        (f"{file_path}:{name}:{line}", PROJECT, kind, name, file_path, line),
    )
    con.commit()


def add_reference(con, file_path, name, count=1):
    con.execute(
        "INSERT OR REPLACE INTO repo_refs (project_path, file_path, name, ref_count)"
        " VALUES (?, ?, ?, ?)",
        (PROJECT, file_path, name, count),
    )
    con.commit()


# ── prompt parsing ──────────────────────────────────────────────────────────

def test_identifiers_extracted_from_prompt():
    idents = mentioned_identifiers("please fix the ContextScorer and extract_nodes")
    assert "ContextScorer" in idents
    assert "extract_nodes" in idents


def test_paths_extracted_from_prompt():
    assert mentioned_paths("edit src/app.py and tests/test_x.py") == {
        "src/app.py", "tests/test_x.py"
    }


# ── identifier weighting ────────────────────────────────────────────────────

def test_identifier_named_in_prompt_weighs_more():
    named = _identifier_multiplier("process_widgets", {"process_widgets"}, 1)
    unnamed = _identifier_multiplier("process_widgets", set(), 1)
    assert named > unnamed


def test_distinctive_names_weigh_more_than_short_ones():
    """A long multi-word name identifies code; a short one barely narrows anything."""
    distinctive = _identifier_multiplier("process_widget_batch", set(), 1)
    plain = _identifier_multiplier("run", set(), 1)
    assert distinctive > plain


def test_private_names_are_downweighted():
    assert _identifier_multiplier("_helper_method", set(), 1) < _identifier_multiplier(
        "helper_method", set(), 1
    )


def test_names_defined_everywhere_are_downweighted():
    """Something defined in 20 files says little about where to look."""
    rare = _identifier_multiplier("process_widgets", set(), 1)
    ubiquitous = _identifier_multiplier("process_widgets", set(), 20)
    assert ubiquitous < rare


# ── ranking ─────────────────────────────────────────────────────────────────

def test_referenced_definition_outranks_unreferenced_one(con):
    add_definition(con, "core.py", "process_widgets")
    add_definition(con, "orphan.py", "unused_helper")
    add_reference(con, "caller_a.py", "process_widgets", 3)
    add_reference(con, "caller_b.py", "process_widgets", 2)

    ranked = rank_symbols(con, PROJECT, "")
    names = [s.name for s in ranked]

    assert "process_widgets" in names
    assert names[0] == "process_widgets"


def test_the_same_codebase_ranks_differently_per_prompt(con):
    """The property a static rank cannot have, and the reason it lost."""
    add_definition(con, "auth.py", "validate_token")
    add_definition(con, "billing.py", "charge_customer")
    # Symmetric graph: neither is structurally more central than the other.
    add_reference(con, "app.py", "validate_token", 5)
    add_reference(con, "app.py", "charge_customer", 5)

    auth_first = rank_symbols(con, PROJECT, "fix validate_token please")
    billing_first = rank_symbols(con, PROJECT, "fix charge_customer please")

    assert auth_first[0].name == "validate_token"
    assert billing_first[0].name == "charge_customer"


def test_mentioning_a_file_biases_toward_it(con):
    add_definition(con, "auth.py", "validate_token")
    add_definition(con, "billing.py", "charge_customer")
    add_reference(con, "app.py", "validate_token", 5)
    add_reference(con, "app.py", "charge_customer", 5)

    ranked = rank_symbols(con, PROJECT, "there is a bug in billing.py")

    assert ranked[0].file_path == "billing.py"


def test_no_references_means_no_ranking(con):
    """Without a reference graph there is nothing to rank; say so, don't guess."""
    add_definition(con, "core.py", "process_widgets")
    assert rank_symbols(con, PROJECT, "anything") == []


def test_empty_project_ranks_nothing(con):
    assert rank_symbols(con, PROJECT, "anything") == []


def test_missing_refs_table_degrades_quietly(tmp_path):
    """A project indexed before repo_refs existed must not crash a session."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(CREATE_SCHEMA_SQL)
    con.execute("DROP TABLE repo_refs")
    con.execute(
        "INSERT INTO repo_nodes (node_id, project_path, repo_node_type, name,"
        " file_path, start_line, metadata) VALUES ('n1', ?, 'repo_function',"
        " 'process_widgets', 'core.py', 1, '{}')",
        (PROJECT,),
    )
    con.commit()

    assert rank_symbols(con, PROJECT, "anything") == []
    con.close()


def test_graph_too_large_is_skipped(con, monkeypatch):
    """Ranking must not spend real time on the user's first turn."""
    from contextmesh.graph import ranking

    monkeypatch.setattr(ranking, "MAX_GRAPH_FILES", 1)
    add_definition(con, "a.py", "process_widgets")
    add_reference(con, "b.py", "process_widgets", 1)
    add_reference(con, "c.py", "process_widgets", 1)

    assert rank_symbols(con, PROJECT, "") == []


# ── injector integration ────────────────────────────────────────────────────

def test_repomap_orders_by_prompt_relevance(tmp_path):
    path = tmp_path / "cm.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(CREATE_SCHEMA_SQL)
    for f, n in (("auth.py", "validate_token"), ("billing.py", "charge_customer")):
        con.execute(
            "INSERT INTO repo_nodes (node_id, project_path, repo_node_type, name,"
            " file_path, start_line, metadata) VALUES (?, ?, 'repo_function', ?, ?, 1, '{}')",
            (f"{f}:{n}", PROJECT, n, f),
        )
        con.execute(
            "INSERT INTO repo_refs (project_path, file_path, name, ref_count)"
            " VALUES (?, 'app.py', ?, 5)",
            (PROJECT, n),
        )
    con.commit()
    con.close()

    auth = _build_repomap_from_db(str(path), PROJECT, "fix validate_token")
    billing = _build_repomap_from_db(str(path), PROJECT, "fix charge_customer")

    assert auth.index("auth.py") < auth.index("billing.py")
    assert billing.index("billing.py") < billing.index("auth.py")


def test_repomap_without_ranking_falls_back_to_file_order(tmp_path):
    """No refs indexed: still produce a usable map, just unranked."""
    path = tmp_path / "cm.db"
    con = sqlite3.connect(path)
    con.executescript(CREATE_SCHEMA_SQL)
    for f, n in (("b_file.py", "b_fn"), ("a_file.py", "a_fn")):
        con.execute(
            "INSERT INTO repo_nodes (node_id, project_path, repo_node_type, name,"
            " file_path, start_line, metadata) VALUES (?, ?, 'repo_function', ?, ?, 1, '{}')",
            (f"{f}:{n}", PROJECT, n, f),
        )
    con.commit()
    con.close()

    repomap = _build_repomap_from_db(str(path), PROJECT, "unrelated question")

    assert repomap.index("a_file.py") < repomap.index("b_file.py")
