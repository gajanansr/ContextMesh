"""Benchmark corpus for the read-shunt.

The tasks that matter here are the ones a shunt is actually for: questions
whose answer needs the *contents* of large files, with no symbol you can grep
for. That distinction is the lesson from the symbolgraph run, where locate
questions ("which file defines X") were answered by a single grep and the
retrieval tool was never invoked at all -- 0 times in 9 runs. A tool that the
agent routes around cannot be measured.

Here the routing is not optional: a PreToolUse hook blocks any Read over 350
lines, so the only ways past it are the delegate or an offset/limit read of a
section the agent has to locate first. Both arms face the same files.

Fixture is the FastAPI package at a pinned SHA. It has what this needs --
routing.py at 6,447 lines and applications.py at 4,774, both far over the
threshold, in a real codebase rather than a synthetic one.

Tasks:

- `router`    -- one large file (routing.py, 6,447 lines). The simplest case
                 for a shunt and the one its published numbers lean on.
- `crossfile` -- two large files (applications.py + routing.py, 11,221 lines
                 combined). Portal's own benchmark calls this shape
                 "multi-file-cross-read" and it is where reading directly is
                 most expensive, so it is where a delegate should win biggest.
- `control`   -- creates a file, reads nothing. The hook can never fire, so
                 any difference is cache ordering or the cost of installing
                 the hook at all. Read it first.

Verification greps ANSWER.md for two independent facts per task, so an agent
that guesses one from the filename still fails. ANSWER.md is deleted in setup:
a stale answer from the previous replicate would otherwise verify with no work
done, which would make the cheapest arm look best precisely when it did least.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bench.runner import Task

FASTAPI_URL = "https://github.com/fastapi/fastapi.git"
FASTAPI_SHA = "49033471594ea5d99a80abdf1043231b7791ee49"


@dataclass(frozen=True)
class ShuntFixture:
    root: Path


def build_fixture(workdir: Path) -> ShuntFixture:
    """Clone FastAPI at the pinned SHA and keep only the package."""
    root = (workdir / "fastapi-pkg").resolve()
    if root.exists():
        shutil.rmtree(root)

    clone = workdir / "_fastapi-clone"
    if clone.exists():
        shutil.rmtree(clone)
    subprocess.run(["git", "clone", "--quiet", FASTAPI_URL, str(clone)],
                   check=True, capture_output=True)
    subprocess.run(["git", "checkout", "--quiet", FASTAPI_SHA],
                   cwd=clone, check=True, capture_output=True)
    shutil.copytree(clone / "fastapi", root)
    shutil.rmtree(clone)

    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=bench@local", "-c", "user.name=bench",
         "commit", "--quiet", "-m", "fixture"],
        cwd=root, check=True, capture_output=True,
    )
    return ShuntFixture(root=root)


def reset_command(fixture: ShuntFixture) -> str:
    return (
        f"git -C {fixture.root} checkout -- . && "
        f"git -C {fixture.root} clean -qfd && "
        f"rm -f {fixture.root}/ANSWER.md"
    )


def build_tasks(fixture: ShuntFixture) -> list[Task]:
    common = {"repo": fixture.root, "setup": reset_command(fixture), "timeout_s": 1800}
    answer = "Write your answer to ANSWER.md, then reply DONE."

    return [
        Task(
            task_id="router",
            prompt=(
                "In this FastAPI package, routing.py defines the router. Name the "
                "method that mounts one router inside another under a path prefix, "
                "and name the class that method belongs to. " + answer
            ),
            # include_router on APIRouter. Both terms required: the method name
            # alone is guessable from the question's phrasing.
            verify=(
                "grep -qE 'include_router\\b' ANSWER.md && "
                "grep -qE 'APIRouter\\b' ANSWER.md"
            ),
            **common,
        ),
        Task(
            task_id="crossfile",
            prompt=(
                "In this FastAPI package, answer using applications.py and "
                "routing.py: name the method on the FastAPI class that builds and "
                "returns the OpenAPI schema, and name the method on APIRouter that "
                "mounts a sub-router under a prefix. " + answer
            ),
            # FastAPI.openapi (applications.py) + APIRouter.include_router.
            # 11,221 lines across the two files, both over the threshold.
            verify=(
                "grep -qE 'openapi\\b' ANSWER.md && "
                "grep -qE 'include_router\\b' ANSWER.md"
            ),
            **common,
        ),
        Task(
            task_id="exports",
            prompt=(
                "Read routing.py. What are all the exported items and what do "
                "they do? Write the full list to ANSWER.md, then reply DONE."
            ),
            # Portal's own benchmark question #1, verbatim, against a file 10x
            # the size of their fixture. This is the shape a shunt is built
            # for: no symbol to grep, the answer needs the whole file. It is
            # also the only task here that makes the agent attempt a large
            # read at all.
            verify=(
                "grep -qE 'APIRouter\\b' ANSWER.md && "
                "grep -qE 'APIRoute\\b' ANSWER.md"
            ),
            **common,
        ),
        Task(
            task_id="control",
            prompt=(
                "Create a file ANSWER.md containing exactly the line "
                "'## 0.1.0 - initial release'. Then reply DONE."
            ),
            # No reads, so the hook cannot fire and the delegate cannot bill.
            verify="grep -q '0.1.0 - initial release' ANSWER.md",
            **common,
        ),
    ]
