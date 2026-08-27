"""Run a shell command on behalf of the Hook Engine.

The hook rewrites every Bash call through here, which makes this the most
dangerous code in the project: anything it loses, the agent never sees, and
anything it misreports, the agent believes.

Two failures this exists to prevent, both previously live:

- A timeout discarded the command's output entirely and printed `None`. A
  build that ran 5 minutes and then hung returned nothing at all -- no log, no
  error text, no clue.
- The wrapper exited 0 regardless of what the command did, so a failing test
  suite was indistinguishable from a passing one.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

# Long enough for a real build or test suite. The previous 300s killed
# ordinary work; a ceiling still exists so a hung command cannot wedge the
# session forever.
DEFAULT_TIMEOUT_SECONDS = 1800
TIMEOUT_ENV_VAR = "CONTEXTMESH_TIMEOUT"
TIMEOUT_EXIT_CODE = 124  # conventional, matches coreutils `timeout`


def resolve_timeout(explicit: int | None = None) -> int:
    """Timeout in seconds: explicit, else CONTEXTMESH_TIMEOUT, else default.

    A non-positive or unparseable value disables the timeout rather than
    falling back to a short one -- a user who asks for no limit should not get
    a five-minute one.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get(TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else 0


@dataclass(frozen=True)
class Execution:
    output: str
    returncode: int
    timed_out: bool


def _decode(stream: str | bytes | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


def _combine(stdout: str, stderr: str) -> str:
    combined = stdout
    if stderr:
        combined += f"\n[STDERR]\n{stderr}"
    return combined


def execute(command: str, timeout: int | None = None, cwd: str | None = None) -> Execution:
    """Run `command`, always returning whatever output it produced."""
    seconds = resolve_timeout(timeout)

    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=seconds or None, cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        # Partial output is the whole point: a command that hung after
        # printing 2000 useful lines must not come back empty.
        partial = _combine(_decode(exc.stdout), _decode(exc.stderr))
        notice = (
            f"\n[ContextMesh: command exceeded {seconds}s and was terminated. "
            f"Output above is what it produced before that point. "
            f"Raise {TIMEOUT_ENV_VAR} if this command legitimately needs longer.]"
        )
        return Execution(output=partial + notice, returncode=TIMEOUT_EXIT_CODE, timed_out=True)

    return Execution(
        output=_combine(proc.stdout or "", proc.stderr or ""),
        returncode=proc.returncode,
        timed_out=False,
    )
