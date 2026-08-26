"""Tests for session memory: extraction, storage, and recall.

The extractor is the part that can quietly rot -- a regex that stops matching
produces an empty memory rather than an error, and nobody notices. Each
signal it claims to detect is pinned here.
"""

import json

import pytest

from contextmesh.memory.extractor import (
    _files_written_by_shell,
    _is_meaningful_prompt,
    extract_nodes,
)
from contextmesh.memory.recall import build_recall_context, files_in_prompt
from contextmesh.memory.store import connect, ensure_session, load_project_nodes, save_nodes
from contextmesh.models.nodes import NodeType
from contextmesh.store.schema import CREATE_SCHEMA_SQL


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "cm.db"
    con = connect(path)
    con.executescript(CREATE_SCHEMA_SQL)
    con.commit()
    con.close()
    return path


def _transcript(tmp_path, *records):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records))
    return path


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _tool_use(name, tool_input, tool_id="tu1"):
    return {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
        ]},
    }


def _tool_result(tool_id="tu1", is_error=False, content="output"):
    return {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id,
             "is_error": is_error, "content": content}
        ]},
    }


# ── extraction ────────────────────────────────────────────────────────────

def test_meaningful_prompts_kept_filler_dropped():
    assert _is_meaningful_prompt("wire up the memory layer please")
    for filler in ("retry", "ok", "  Continue. ", "yes", "y"):
        assert not _is_meaningful_prompt(filler)


def test_system_injected_prompts_are_not_user_goals():
    assert not _is_meaningful_prompt("<system-reminder>do a thing</system-reminder>")


def test_user_prompt_extracted(tmp_path):
    nodes = extract_nodes(_transcript(tmp_path, _user("add retry logic to the client")), "s", "/p")
    assert [n.node_type for n in nodes] == [NodeType.USER_PROMPT]


def test_duplicate_prompts_stored_once(tmp_path):
    t = _transcript(tmp_path, _user("do the same thing"), _user("do the same thing"))
    assert len(extract_nodes(t, "s", "/p")) == 1


def test_edit_tool_records_file_modification(tmp_path):
    t = _transcript(tmp_path, _tool_use("Edit", {"file_path": "src/app.py"}))
    nodes = extract_nodes(t, "s", "/p")
    assert nodes[0].node_type is NodeType.FILE_MODIFICATION
    assert nodes[0].files_involved == ["src/app.py"]


@pytest.mark.parametrize("command,expected", [
    ("cat > src/foo.py <<EOF", {"src/foo.py"}),
    ("sed -i '' 's/a/b/' bench/x.py", {"bench/x.py"}),
    ("echo hi > /dev/null", set()),
    ('echo "cmd -> exit code"', set()),      # bare word after a fake redirect
    ("echo hi > $TMPDIR/x", set()),          # unexpanded variable
])
def test_shell_writes_detected(command, expected):
    assert _files_written_by_shell(command) == expected


def test_failed_tool_becomes_error_node(tmp_path):
    t = _transcript(
        tmp_path,
        _tool_use("Edit", {"file_path": "src/broken.py"}),
        _tool_result(is_error=True, content="SyntaxError"),
    )
    errors = [n for n in extract_nodes(t, "s", "/p") if n.node_type in
              (NodeType.ERROR, NodeType.UNRESOLVED_ISSUE)]
    assert errors and "SyntaxError" in errors[0].content


def test_error_on_never_fixed_file_becomes_unresolved(tmp_path):
    """The dead-end signal: it broke and nothing ever fixed it."""
    t = _transcript(
        tmp_path,
        _tool_use("Read", {"file_path": "src/cursed.py"}),
        _tool_result(is_error=True, content="ImportError"),
    )
    nodes = extract_nodes(t, "s", "/p")
    assert any(n.node_type is NodeType.UNRESOLVED_ISSUE for n in nodes)


def test_error_on_later_fixed_file_stays_a_plain_error(tmp_path):
    t = _transcript(
        tmp_path,
        _tool_use("Read", {"file_path": "src/fixed.py"}, "a"),
        _tool_result("a", is_error=True, content="boom"),
        _tool_use("Edit", {"file_path": "src/fixed.py"}, "b"),
    )
    kinds = {n.node_type for n in extract_nodes(t, "s", "/p")}
    assert NodeType.ERROR in kinds
    assert NodeType.UNRESOLVED_ISSUE not in kinds


def test_commit_and_test_commands_are_typed(tmp_path):
    t = _transcript(
        tmp_path,
        _tool_use("Bash", {"command": "git commit -m wip"}, "a"),
        _tool_use("Bash", {"command": "pytest tests/ -q"}, "b"),
    )
    kinds = {n.node_type for n in extract_nodes(t, "s", "/p")}
    assert NodeType.COMMIT in kinds and NodeType.TEST_RESULT in kinds


def test_missing_transcript_yields_nothing(tmp_path):
    assert extract_nodes(tmp_path / "nope.jsonl", "s", "/p") == []


def test_malformed_lines_do_not_crash(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("{bad json\n" + json.dumps(_user("a real prompt here")))
    assert len(extract_nodes(path, "s", "/p")) == 1


# ── storage ───────────────────────────────────────────────────────────────

def test_ensure_session_latches_once(db):
    con = connect(db)
    try:
        assert ensure_session(con, "s1", "/proj") is True
        assert ensure_session(con, "s1", "/proj") is False
    finally:
        con.close()


def test_save_is_idempotent(db, tmp_path):
    nodes = extract_nodes(_transcript(tmp_path, _user("a meaningful goal here")), "s1", "/proj")
    save_nodes(db, "s1", "/proj", nodes)
    save_nodes(db, "s1", "/proj", nodes)
    assert len(load_project_nodes(db, "/proj")) == 1


def test_nodes_are_scoped_by_project(db, tmp_path):
    a = extract_nodes(_transcript(tmp_path / "a", _user("project A work here")), "s1", "/a")
    b = extract_nodes(_transcript(tmp_path / "b", _user("project B work here")), "s2", "/b")
    save_nodes(db, "s1", "/a", a)
    save_nodes(db, "s2", "/b", b)

    assert len(load_project_nodes(db, "/a")) == 1
    assert "project A" in load_project_nodes(db, "/a")[0]["content"]


def test_current_session_excluded_from_recall(db, tmp_path):
    nodes = extract_nodes(_transcript(tmp_path, _user("something memorable here")), "s1", "/proj")
    save_nodes(db, "s1", "/proj", nodes)
    assert load_project_nodes(db, "/proj", exclude_session="s1") == []


# ── recall ────────────────────────────────────────────────────────────────

def test_files_in_prompt_detected():
    assert files_in_prompt("please fix src/app.py and tests/test_x.py") == [
        "src/app.py", "tests/test_x.py"
    ]


def test_recall_is_empty_without_memory(db):
    assert build_recall_context(db, "/proj", "do something") == ""


def test_recall_includes_prior_work(db, tmp_path):
    nodes = extract_nodes(
        _transcript(tmp_path, _user("migrate the auth module to OAuth")), "s1", "/proj"
    )
    save_nodes(db, "s1", "/proj", nodes)

    context = build_recall_context(db, "/proj", "continue the migration", exclude_session="s2")

    assert "ContextMesh memory" in context
    assert "OAuth" in context


def test_recall_respects_budget(db, tmp_path):
    records = [_user(f"a distinct and sufficiently long goal number {i}") for i in range(60)]
    save_nodes(db, "s1", "/proj", extract_nodes(_transcript(tmp_path, *records), "s1", "/proj"))

    context = build_recall_context(db, "/proj", "what next", exclude_session="s2", budget_chars=400)

    assert len(context) < 1200  # budget governs entries; header/footer are fixed
