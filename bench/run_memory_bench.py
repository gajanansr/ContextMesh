"""Driver: does recalling prior-session memory pay for itself?

Usage: python -m bench.run_memory_bench [--replicates N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from bench.corpus import build_fixture, build_tasks
from bench.report import MEMORY_MARKER, delivery_report, format_report
from bench.runner import run_matrix

REPO_ROOT = Path(__file__).resolve().parent.parent


def _hook_settings(workdir: Path) -> Path:
    """Point Claude Code at this working copy's hook, not the installed one.

    The shim records every invocation and how many bytes the hook emitted.
    A benchmark that assumes its treatment was applied can report a confident
    null for a feature that never ran -- which is exactly what happened on the
    first attempt here.
    """
    shim = workdir / "cmhook.sh"
    shim.write_text(
        "#!/bin/bash\n"
        f"LOG={workdir}/hook.log\n"
        "PAYLOAD=$(cat)\n"
        f"OUT=$(printf '%s' \"$PAYLOAD\" | (cd {REPO_ROOT} && "
        f"PYTHONPATH={REPO_ROOT}/src {REPO_ROOT}/.venv/bin/python -m contextmesh.hook))\n"
        "SID=$(printf '%s' \"$PAYLOAD\" | sed -n 's/.*\"session_id\":\"\\([^\"]*\\)\".*/\\1/p')\n"
        "EVT=$(printf '%s' \"$PAYLOAD\" | sed -n 's/.*\"hook_event_name\":\"\\([^\"]*\\)\".*/\\1/p')\n"
        "printf '%s %s %s\\n' \"$SID\" \"$EVT\" \"${#OUT}\" >> \"$LOG\"\n"
        "printf '%s' \"$OUT\"\n"
    )
    shim.chmod(0o755)

    settings = workdir / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            event: [{"matcher": "*", "hooks": [{"type": "command", "command": str(shim)}]}]
            for event in ("UserPromptSubmit", "SessionEnd")
        }
    }, indent=2))
    return settings


def _acquire_lock(workdir_parent: Path) -> Path:
    """Refuse to run when another instance is live.

    Two concurrent runs share a workdir, and each one's reset does
    `rm -rf <data_dir>`. The loser's seeded database is deleted mid-session,
    recall silently returns nothing, and the run reports a confident null for
    a treatment that was wiped out from under it. That happened twice here
    before the cause was found.
    """
    lock = workdir_parent / "cm-membench.lock"
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)          # signal 0 just tests existence
        except (ValueError, ProcessLookupError):
            lock.unlink(missing_ok=True)   # stale
        except PermissionError:
            pass                     # exists, owned by another user
        else:
            raise SystemExit(
                f"another benchmark is running (pid {pid}). "
                f"Wait for it, or remove {lock} if it is stale."
            )
    lock.write_text(str(os.getpid()))
    return lock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replicates", type=int, default=3)
    parser.add_argument("--workdir", type=Path,
                        default=Path(os.environ.get("TMPDIR", "/tmp")) / "cm-membench")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build the fixture and print the plan; run nothing.")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    workdir = args.workdir
    workdir.parent.mkdir(parents=True, exist_ok=True)
    lock = _acquire_lock(workdir.parent)

    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    fixture = build_fixture(workdir)
    data_dir = workdir / "data"
    tasks = build_tasks(fixture, data_dir, _hook_settings(workdir))

    total = len(tasks) * (args.replicates * 2 + 1)
    print(f"fixture : {fixture.root}")
    print(f"tasks   : {', '.join(t.task_id for t in tasks)}")
    print(f"runs    : {total} ({len(tasks)} tasks x {args.replicates} replicates "
          f"x 2 arms, + 1 warm-up each)\n")

    if args.dry_run:
        lock.unlink(missing_ok=True)
        for task in tasks:
            print(f"--- {task.task_id}\n    prompt: {task.prompt[:70]}…"
                  f"\n    verify: {task.verify}")
        return 0

    done = {"n": 0}

    def progress(result):
        done["n"] += 1
        status = "ok" if result.verified else ("ERR" if result.cli_error else "unverified")
        print(f"[{done['n']:2d}/{total - len(tasks)}] {result.task_id:<11} "
              f"{result.arm:<4} r{result.replicate} "
              f"{status:<11} ${result.cost_usd:.4f} {result.turns:>2} turns "
              f"{result.duration_s:5.1f}s")

    matrix = run_matrix(tasks, replicates=args.replicates,
                        arms=["off", "on"], on_result=progress)

    print("\n" + delivery_report(matrix, MEMORY_MARKER, "recalled memory"))
    print("\n" + format_report(matrix, baseline="off", treatment="on"))

    out = args.out or (workdir / "results.json")
    out.write_text(matrix.to_json())
    print(f"\nraw results: {out}")
    lock.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
