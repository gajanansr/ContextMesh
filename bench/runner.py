"""A/B runner: execute a task under Claude Code with the hook live vs inert.

Both arms run with identical settings, auth, model, and working directory.
The only difference is CONTEXTMESH_DISABLE, which makes the hook pass through
without rewriting anything. That keeps the comparison honest -- no global
settings mutation, and the arms can run without interfering with each other.

Cost comes from the transcript parser. The CLI's own `total_cost_usd` is
recorded alongside it as an independent check -- the two agree to six
decimals on real runs, which is what validates the billing model.

Two confounds this module controls for, both larger than any plausible
context-layer effect:

- Cache ordering. Whichever arm runs first pays cache-creation; the second
  reads a warm cache. Observed at 8x on a trivial task. Countered with a
  discarded warm-up run per task plus counterbalanced arm order.
- Give-up runs. An agent that quits early looks cheap. Every task carries a
  `verify` command; unverified runs are reported but excluded from cost
  comparisons.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from bench.transcript import SessionCost, parse_session

# arm name -> environment overlay applied to the child process
ARMS: dict[str, dict[str, str]] = {
    "on": {},
    "off": {"CONTEXTMESH_DISABLE": "1"},
}


@dataclass(frozen=True)
class Task:
    """One benchmark task.

    `verify` is a shell command run in `repo` after the agent finishes; exit 0
    means the task succeeded. Without one, success is unmeasurable and the
    token numbers are meaningless -- a run that gives up early always looks
    cheap.
    """

    task_id: str
    prompt: str
    repo: Path
    verify: str | None = None
    setup: str | None = None
    timeout_s: int = 1800
    # Settings file passed to `claude --settings`. Needed to exercise a hook
    # build other than whatever is registered globally.
    settings: Path | None = None
    # Extra environment for the agent process, e.g. CONTEXTMESH_DATA_DIR to
    # give each run its own database. Applied before the arm overlay, so an
    # arm can still override it.
    env: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo", Path(self.repo))


@dataclass
class RunResult:
    task_id: str
    arm: str
    replicate: int
    session_id: str = ""
    transcript: Path | None = None
    session: SessionCost | None = None
    verified: bool | None = None
    cli_error: bool = False
    cli_cost_usd: float = 0.0
    cli_num_turns: int = 0
    duration_s: float = 0.0
    error: str = ""

    @property
    def turns(self) -> int:
        return self.session.assistant_turns if self.session else self.cli_num_turns

    @property
    def cost_usd(self) -> float:
        return self.session.cost_usd if self.session else 0.0

    @property
    def billed_input_equivalent(self) -> float:
        return self.session.usage.billed_input_equivalent if self.session else 0.0

    def row(self) -> dict:
        return {
            "task_id": self.task_id,
            "arm": self.arm,
            "replicate": self.replicate,
            "session_id": self.session_id,
            "verified": self.verified,
            "turns": self.turns,
            "cost_usd": round(self.cost_usd, 6),
            "cli_cost_usd": self.cli_cost_usd,
            "billed_input_equivalent": round(self.billed_input_equivalent, 1),
            "duration_s": round(self.duration_s, 1),
            "cli_error": self.cli_error,
            "error": self.error,
        }


def find_transcript(session_id: str, config_dir: Path | None = None) -> Path | None:
    """Locate a session transcript by id."""
    if not session_id:
        return None
    root = config_dir or (Path.home() / ".claude")
    matches = sorted(root.glob(f"projects/*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _shell(command: str, cwd: Path, timeout: int, extra_env: dict | None = None) -> tuple[int, str]:
    """Run a setup/verify command with the hook inert, so it never self-measures."""
    env = dict(os.environ, CONTEXTMESH_DISABLE="1")
    env.update(extra_env or {})
    try:
        proc = subprocess.run(
            command, shell=True, cwd=cwd, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout + proc.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def run_once(task: Task, arm: str, replicate: int, model: str | None = None) -> RunResult:
    """Run one task under one arm, once."""
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {sorted(ARMS)}")

    result = RunResult(task_id=task.task_id, arm=arm, replicate=replicate)

    if task.setup:
        code, out = _shell(task.setup, task.repo, timeout=300, extra_env=task.env)
        if code != 0:
            result.error = f"setup failed ({code}): {out[-300:]}"
            return result

    env = dict(os.environ)
    env.update(task.env)
    env.update(ARMS[arm])
    # Keep the agent from inheriting our own session's identity.
    env.pop("CLAUDE_CODE_SESSION_ID", None)

    cmd = [
        "claude", "-p", task.prompt,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
    ]
    if task.settings:
        cmd += ["--settings", str(task.settings)]
    if model:
        cmd += ["--model", model]

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=task.repo, env=env,
            capture_output=True, text=True, timeout=task.timeout_s,
        )
    except subprocess.TimeoutExpired:
        result.duration_s = time.monotonic() - started
        result.error = f"claude timed out after {task.timeout_s}s"
        result.cli_error = True
        return result
    result.duration_s = time.monotonic() - started

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result.error = f"unparseable CLI output: {(proc.stdout or proc.stderr)[:300]}"
        result.cli_error = True
        return result

    result.session_id = payload.get("session_id") or ""
    result.cli_error = bool(payload.get("is_error"))
    result.cli_cost_usd = float(payload.get("total_cost_usd") or 0.0)
    result.cli_num_turns = int(payload.get("num_turns") or 0)
    if result.cli_error:
        result.error = str(payload.get("result") or "")[:300]

    result.transcript = find_transcript(result.session_id)
    if result.transcript:
        result.session = parse_session(result.transcript)

    if task.verify:
        code, out = _shell(task.verify, task.repo, timeout=600, extra_env=task.env)
        result.verified = code == 0
        if code != 0 and not result.error:
            result.error = f"verify failed ({code}): {out[-200:]}"

    return result


@dataclass
class Matrix:
    """Results for tasks x arms x replicates."""

    results: list[RunResult] = field(default_factory=list)

    def add(self, result: RunResult) -> None:
        self.results.append(result)

    def for_arm(self, arm: str) -> list[RunResult]:
        return [r for r in self.results if r.arm == arm]

    def to_json(self) -> str:
        return json.dumps([r.row() for r in self.results], indent=2)


def run_matrix(
    tasks: list[Task],
    replicates: int = 3,
    arms: list[str] | None = None,
    model: str | None = None,
    warmup: bool = True,
    on_result=None,
) -> Matrix:
    """Run every task under every arm, `replicates` times.

    Order matters more than it looks. A cold prompt cache makes the first run
    of a task several times more expensive than the second, so:

    - one warm-up run per task is executed and discarded, and
    - arm order is reversed on odd replicates (ABBA), so neither arm
      systematically occupies the cold slot.

    With an even number of replicates each arm takes the first slot equally
    often; odd counts leave a residual bias toward the arm listed first.
    """
    arms = list(arms or ARMS)
    matrix = Matrix()

    for task in tasks:
        if warmup:
            run_once(task, arms[0], replicate=-1, model=model)

        for replicate in range(replicates):
            ordered = arms if replicate % 2 == 0 else list(reversed(arms))
            for arm in ordered:
                result = run_once(task, arm, replicate, model=model)
                matrix.add(result)
                if on_result:
                    on_result(result)

    return matrix
