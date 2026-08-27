"""Benchmark corpus for the memory layer.

The question is narrow: when a project already has session history, does
recalling it pay for the tokens the recall costs?

Answering that needs a project with history, so each task runs in a fixture
repo seeded by a scripted prior session. The seeded memory is built once and
snapshotted; every replicate starts from that snapshot, otherwise replicate N
would see the memory of replicates 0..N-1 and the arms would drift apart.

These tasks are authored, not third-party, and three of the four were written
expecting memory to help. That is a real bias and it is why `control` exists:
memory is irrelevant to it, so a "win" there means something other than
memory is producing the difference -- cache ordering being the usual culprit.
Read the control first.

The three memory-relevant tasks each exercise a different node type on
purpose, rather than three variations on the same recall: `convention` needs a
DECISION (where something lives), `deadend` needs an UNRESOLVED_ISSUE (what
already broke), `backoff` needs a SOLUTION (how something was decided to
behave). A corpus that only ever tested one memory mechanism could look like
it generalises when it has only shown one narrow case works.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bench.runner import Task

# A prior session's knowledge, expressed the way the extractor would have
# stored it. Seeding directly keeps the fixture deterministic -- driving a
# real agent to produce it would vary run to run.
SEED_NODES = [
    {
        "node_type": "user_prompt",
        "content": (
            "Set up this project's settings module. Every tunable lives in "
            "settings.py as an UPPER_SNAKE constant, and all durations are "
            "expressed in seconds. Do not create a config.py."
        ),
        "files": ["settings.py"],
        "importance": 0.9,
    },
    {
        "node_type": "decision",
        "content": (
            "Settings live in settings.py only. An earlier attempt used "
            "config.py and was removed; two settings modules caused imports "
            "to silently pick up stale values."
        ),
        "files": ["settings.py"],
        "importance": 1.0,
    },
    {
        "node_type": "unresolved_issue",
        "content": (
            "Do not import settings.py from utils/net.py at module scope. It "
            "creates a circular import that fails only under pytest, not at "
            "the REPL. Import inside the function instead."
        ),
        "files": ["utils/net.py"],
        "importance": 0.95,
    },
    {
        "node_type": "solution",
        "content": (
            "Immediate retries hammered the endpoint during an outage and made "
            "it worse. Retries in this project must back off exponentially -- "
            "wait base_delay * (2 ** attempt) between attempts, never retry "
            "immediately."
        ),
        "files": ["utils/net.py"],
        "importance": 0.9,
    },
]

FIXTURE_FILES = {
    "settings.py": "TIMEOUT_SECONDS = 30\nMAX_WORKERS = 4\n",
    "utils/__init__.py": "",
    "utils/net.py": (
        '"""Networking helpers."""\n\n\n'
        "def fetch(url):\n"
        "    from settings import TIMEOUT_SECONDS\n\n"
        "    return f\"GET {url} timeout={TIMEOUT_SECONDS}\"\n"
    ),
    "README.md": "# fixture project\n\nA small project used by the ContextMesh benchmark.\n",
}


@dataclass(frozen=True)
class Fixture:
    root: Path
    seed_db: Path


def _write_fixture(root: Path) -> None:
    for rel, body in FIXTURE_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def _git_init(root: Path) -> None:
    run = lambda *a: subprocess.run(a, cwd=root, capture_output=True, check=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "bench@contextmesh.local")
    run("git", "config", "user.name", "ContextMesh Bench")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "fixture baseline")


def build_fixture(workdir: Path) -> Fixture:
    """Create the fixture repo and a database pre-seeded with prior memory."""
    from contextmesh.memory.store import connect, ensure_session
    from contextmesh.store.schema import CREATE_SCHEMA_SQL

    root = (workdir / "project").resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    _write_fixture(root)

    # The reset between replicates is `git checkout && git clean`, which needs
    # a real repo. Without one, files an earlier replicate created survive and
    # the next replicate's verify passes without the agent doing anything.
    _git_init(root)

    seed_db = (workdir / "seed.db").resolve()
    if seed_db.exists():
        seed_db.unlink()

    con = connect(seed_db)
    try:
        con.executescript(CREATE_SCHEMA_SQL)
        ensure_session(con, "seed-session", str(root))
        for index, node in enumerate(SEED_NODES):
            con.execute(
                "INSERT INTO nodes (node_id, session_id, node_type, content,"
                " files_involved, symbols, confidence, importance, tier,"
                " token_count, created_at, metadata)"
                " VALUES (?, ?, ?, ?, ?, '[]', 1.0, ?, 'warm', 0,"
                " '2026-08-01T00:00:00+00:00', '{}')",
                (
                    f"seed_{index}",
                    "seed-session",
                    node["node_type"],
                    node["content"],
                    json.dumps(node["files"]),
                    node["importance"],
                ),
            )
        con.commit()
    finally:
        con.close()

    return Fixture(root=root, seed_db=seed_db)


def reset_command(fixture: Fixture, data_dir: Path) -> str:
    """Shell that restores repo and memory to the seeded state before a run."""
    return (
        f"rm -rf {data_dir} && mkdir -p {data_dir} && "
        f"cp {fixture.seed_db} {data_dir}/contextmesh.db && "
        f"git -C {fixture.root} checkout -- . && "
        f"git -C {fixture.root} clean -fdq"
    )


def build_tasks(fixture: Fixture, data_dir: Path, settings: Path) -> list[Task]:
    """The four benchmark tasks.

    convention  -- needs a DECISION: settings live in settings.py, not config.py
    deadend     -- needs an UNRESOLVED_ISSUE: the circular import already hit
    backoff     -- needs a SOLUTION: retries must back off exponentially
    control     -- unrelated to anything in memory; the falsification check
    """
    common = {
        "repo": fixture.root,
        "settings": settings,
        "env": {"CONTEXTMESH_DATA_DIR": str(data_dir)},
        "setup": reset_command(fixture, data_dir),
        "timeout_s": 600,
    }

    return [
        Task(
            task_id="convention",
            prompt=(
                "Add a tunable for how many times a failed request should be "
                "retried, defaulting to 3. Follow this project's existing "
                "conventions. Then reply DONE."
            ),
            # Passes only if it went in settings.py with the right shape and
            # no rival config.py appeared.
            verify=(
                "test ! -f config.py && "
                "grep -qE '^[A-Z_]*RETR[A-Z_]* *= *3' settings.py"
            ),
            **common,
        ),
        Task(
            task_id="deadend",
            prompt=(
                "In utils/net.py, add a function `fetch_with_retries(url)` that "
                "uses the retry count from the settings module. Then reply DONE."
            ),
            # Passes only if the module-scope import of settings was avoided.
            verify=(
                "grep -q 'def fetch_with_retries' utils/net.py && "
                "! grep -qE '^(from settings|import settings)' utils/net.py"
            ),
            **common,
        ),
        Task(
            task_id="backoff",
            prompt=(
                "Add a `retry_with_backoff(fn, max_attempts=3)` helper to "
                "utils/net.py that retries `fn` on failure, following how this "
                "project has decided retries should behave. Then reply DONE."
            ),
            # Passes only if it actually backs off exponentially, not merely
            # if the function exists -- an agent could name it "backoff" and
            # still retry immediately, which memory is what would prevent.
            verify=(
                "grep -q 'def retry_with_backoff' utils/net.py && "
                "grep -qE '(2\\s*\\*\\*|\\*\\*\\s*2|pow\\(2)' utils/net.py"
            ),
            **common,
        ),
        Task(
            task_id="control",
            prompt=(
                "Create a file CHANGELOG.md containing a single line: "
                "'## 0.1.0 - initial release'. Then reply DONE."
            ),
            # Nothing in memory pertains to this. Memory should not help.
            verify="grep -q '0.1.0' CHANGELOG.md",
            **common,
        ),
    ]
