"""Benchmark corpus for the AST RepoMap.

The RepoMap has been this project's headline feature since it shipped and has
never been measured. It injects ~2,650 tokens into every session -- 7.7x the
memory block -- on the assumption that knowing the architecture up front beats
searching for it. That assumption is what this tests.

Both arms run with memory switched off, so the only variable is the map.

The fixture is a git clone of this repository, not a synthetic tree: a map of
four toy files proves nothing, and the claim is specifically about navigating a
codebase too big to read. Tasks deliberately do not name the file to edit --
naming it would hand the agent what the map is supposed to provide.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bench.runner import Task

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class RepoFixture:
    root: Path


def build_fixture(workdir: Path) -> RepoFixture:
    """Clone this repo at HEAD so runs cannot touch the working copy."""
    root = (workdir / "repo").resolve()
    if root.exists():
        shutil.rmtree(root)
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", f"file://{REPO_ROOT}", str(root)],
        check=True, capture_output=True,
    )
    return RepoFixture(root=root)


def index_fixture(root: Path, data_dir: Path) -> None:
    """Index the clone into a dedicated database."""
    data_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(REPO_ROOT / ".venv/bin/python"), "-m", "contextmesh.cli", "index", str(root)],
        cwd=REPO_ROOT, check=True, capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "PYTHONPATH": str(REPO_ROOT / "src"),
             "CONTEXTMESH_DATA_DIR": str(data_dir),
             "CONTEXTMESH_DISABLE": "1"},
    )


def reset_command(fixture: RepoFixture) -> str:
    return (
        f"git -C {fixture.root} checkout -- . && "
        f"git -C {fixture.root} clean -fdq"
    )


def build_tasks(fixture: RepoFixture, data_dir: Path, settings: Path) -> list[Task]:
    """Tasks that require locating code, plus a control that does not.

    None of the first two name their target file. That is the point: the
    RepoMap's claim is that it removes the search, so a task that hands over
    the path measures nothing.
    """
    common = {
        "repo": fixture.root,
        "settings": settings,
        "env": {"CONTEXTMESH_DATA_DIR": str(data_dir)},
        "setup": reset_command(fixture),
        "timeout_s": 900,
    }

    return [
        Task(
            task_id="locate-class",
            prompt=(
                "Find the class in this codebase responsible for scoring context "
                "nodes for relevance, and add a method to it named `debug_score` "
                "that takes (self, node, query_text) and returns a dict. Then "
                "reply DONE."
            ),
            verify="grep -q 'def debug_score' src/contextmesh/router/scorer.py",
            **common,
        ),
        Task(
            task_id="locate-function",
            prompt=(
                "Find the function that turns a finished session transcript into "
                "typed knowledge nodes, and add the comment line "
                "`# BENCHMARK MARKER` directly above its def. Then reply DONE."
            ),
            verify="grep -B2 'def extract_nodes' src/contextmesh/memory/extractor.py | grep -q 'BENCHMARK MARKER'",
            **common,
        ),
        Task(
            task_id="control-noindex",
            prompt=(
                "Append a single line reading `Benchmarked with bench/` to the end "
                "of README.md. Then reply DONE."
            ),
            # Needs no structural knowledge; the map cannot help.
            verify="tail -3 README.md | grep -q 'Benchmarked with bench/'",
            **common,
        ),
    ]
