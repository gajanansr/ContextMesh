"""Benchmark corpus for symbolgraph (Deepjyoti-Sarmah/coding-RAG-system).

symbolgraph claims "87% fewer context tokens" — on FastAPI, 4,923 tokens per
query down to 831. That number is a retrieval measurement: the size of a
context pack against the size of the ground-truth files a query needed. It is
computed without running an agent, so it cannot see what the agent does next.

That gap is the same one that made ContextMesh's own RepoMap look good. A
context tool is only cheap if the cheap thing *replaces* the expensive thing.
If the agent reads the pack and then reads the file anyway, the pack is a
surcharge and the measured "saving" ran the wrong direction.

## Why the fixture is the FastAPI package, not the FastAPI repo

symbolgraph's published FastAPI run pins `source_dir: fastapi` — the package,
not the checkout. That is not a detail. Indexing the whole repo pulls tests
and docs into the candidate pool, and on the dependency-injection query it
pushes the ground-truth file out of the pack entirely:

    whole repo, budget 2000  -> params.py, param_functions.py, 3 test files
    package,    budget 2000  -> solve_dependencies (fastapi/dependencies/utils.py) present

Benchmarking the misconfiguration would measure our carelessness rather than
his tool. The fixture is therefore the package at his pinned SHA, which is
exactly the scope his 83.1% was computed on.

## Task selection, and why it was rewritten

The first task set asked locate-style questions -- "which file defines the
function that resolves sub-dependencies". symbolgraph was registered and
never called: 0 invocations across 12 treatment runs. Adding his documented
`sg init` AGENTS.md ("use sg search / sg context / definition / callers /
callees before reading files") did not change it, and neither did putting the
same instruction in CLAUDE.md. The agent ran one `grep` and answered.

That is not the tool failing. A locate question is one grep, and the agent is
right to prefer it -- which also undercuts the premise of the published
saving, whose baseline assumes the agent reads whole files it would in fact
have grepped. But it means locate tasks cannot measure the tool at all: both
arms do the same thing and the comparison is noise.

Relationship questions do get it adopted. Asked which functions call
solve_dependencies, the agent called index_repository, callers and search
unprompted. So the tasks here are graph questions -- the shape where a symbol
index has something grep does not, and the only shape where the treatment is
actually exercised:

- `callers`  -- every file containing a call to solve_dependencies. Ground
                truth is routing.py and dependencies/utils.py (the recursive
                call), so an agent that stops at the first hit fails.
- `callsite` -- which method calls get_openapi, and where. Ground truth is
                FastAPI.openapi in applications.py.
- `control`  -- answerable without reading any source. symbolgraph cannot
                help, so a difference here is cache ordering or the fixed
                cost of registering the server, not retrieval. Read it first.

Tasks name a behaviour, never a file. Verification greps for the ground-truth
paths *and* the symbol, so naming the right file for the wrong reason fails.
Every task writes to ANSWER.md, deleted in setup -- a stale answer from the
previous replicate would otherwise verify with no work done.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bench.runner import Task

FASTAPI_URL = "https://github.com/fastapi/fastapi.git"
# The SHA symbolgraph pins in benchmarks/results/fastapi.json.
FASTAPI_SHA = "49033471594ea5d99a80abdf1043231b7791ee49"


@dataclass(frozen=True)
class SymbolgraphFixture:
    root: Path
    sg_bin: Path


def build_fixture(workdir: Path, sg_bin: Path) -> SymbolgraphFixture:
    """Clone FastAPI at the pinned SHA and keep only the package."""
    root = (workdir / "fastapi-pkg").resolve()
    if root.exists():
        shutil.rmtree(root)

    clone = workdir / "_fastapi-clone"
    if clone.exists():
        shutil.rmtree(clone)
    subprocess.run(
        ["git", "clone", "--quiet", FASTAPI_URL, str(clone)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "--quiet", FASTAPI_SHA],
        cwd=clone, check=True, capture_output=True,
    )

    shutil.copytree(clone / "fastapi", root)
    shutil.rmtree(clone)

    # A git repo so `git checkout -- .` can reset edits between replicates.
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=bench@local", "-c", "user.name=bench",
         "commit", "--quiet", "-m", "fixture"],
        cwd=root, check=True, capture_output=True,
    )
    return SymbolgraphFixture(root=root, sg_bin=sg_bin)


def index_fixture(fixture: SymbolgraphFixture) -> None:
    """Build the symbolgraph index, embeddings included.

    `--embed` matters: his published FastAPI numbers are the vector-enabled
    arm, and running the full-text-only path would measure a weaker tool than
    the one he claims for.
    """
    subprocess.run(
        [str(fixture.sg_bin), "index", ".", "--embed"],
        cwd=fixture.root, check=True, capture_output=True, timeout=1800,
    )


def reset_command(fixture: SymbolgraphFixture) -> str:
    """Restore the tree between runs, keeping the index (built once, reused).

    `.sg/` is excluded from the clean so a reset does not throw away the
    index and silently turn the sg-on arm into a slower sg-off.
    """
    return (
        f"git -C {fixture.root} checkout -- . && "
        f"git -C {fixture.root} clean -qfd -e .sg && "
        f"rm -f {fixture.root}/ANSWER.md"
    )


def build_tasks(fixture: SymbolgraphFixture) -> list[Task]:
    common = {"repo": fixture.root, "setup": reset_command(fixture), "timeout_s": 1800}
    answer = "Write your answer to ANSWER.md, then reply DONE."

    return [
        Task(
            task_id="callers",
            prompt=(
                "In this FastAPI package, find every function or method that "
                "calls solve_dependencies. List each one with the file it is "
                "defined in. " + answer
            ),
            # routing.py (3 call sites) and dependencies/utils.py (recursive).
            # Both must appear: stopping at routing.py is the common miss.
            verify=(
                "grep -q 'routing\\.py' ANSWER.md && "
                "grep -q 'dependencies/utils\\.py' ANSWER.md"
            ),
            **common,
        ),
        Task(
            task_id="callsite",
            prompt=(
                "In this FastAPI package, the OpenAPI document is built by a "
                "function called get_openapi. Find what calls it: name the "
                "method and the file that method is defined in. " + answer
            ),
            # FastAPI.openapi in applications.py:1070.
            verify=(
                "grep -q 'applications\\.py' ANSWER.md && "
                "grep -qE 'openapi\\b' ANSWER.md"
            ),
            **common,
        ),
        Task(
            task_id="control",
            prompt=(
                "Create a file ANSWER.md containing exactly the line "
                "'## 0.1.0 - initial release'. Then reply DONE."
            ),
            # Retrieval is irrelevant here. A win for either arm is an artefact.
            verify="grep -q '0.1.0 - initial release' ANSWER.md",
            **common,
        ),
    ]
