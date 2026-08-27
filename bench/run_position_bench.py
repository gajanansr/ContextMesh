"""Does *where* we inject context change what it costs?

The cross-tool run found memory costing +32% while injecting only ~350 tokens,
with the excess traced to cache accounting: cache reads fell and 1-hour cache
writes rose, and writes bill at 2.0x against reads at 0.1x. The hypothesis was
that injecting into the first user message disturbs the cached prefix, so the
penalty is cache invalidation rather than payload size.

This isolates position from payload. The same ~440-token block is injected at
UserPromptSubmit and at SessionStart, against a no-injection control, on one
task. Payload identical, position varying, so any difference is position.

It runs through `run_matrix` rather than a sequential script deliberately. A
first hand-rolled attempt ran the three conditions back to back and showed
both injections *cheaper* than no injection -- an artifact of the control
running first on a cold cache, which is the same confound this harness exists
to remove. Warm-up plus arm rotation is not optional even for a quick probe.

Usage: python -m bench.run_position_bench [--replicates N]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
from pathlib import Path

from bench.arms import Arm
from bench.runner import ARM_SPECS, ARMS, Matrix, Task, find_transcript, run_matrix
from bench.transcript import parse_session

# ~440 tokens, the size of a real recalled-memory block.
PAYLOAD_LINES = 14

TASK_PROMPT = (
    "Create three files a.txt, b.txt and c.txt, each containing its own name. "
    "Then run `ls` with Bash to confirm, and reply DONE."
)


def build_payload(workdir: Path) -> Path:
    lines = ["[ContextMesh memory — 8 items recalled from 3 earlier sessions in this project]"]
    for i in range(PAYLOAD_LINES):
        lines.append(
            f"  DECISION: architectural choice number {i} about module layout, "
            f"retries, and error handling conventions  [src/mod_{i}.py]"
        )
    payload = workdir / "payload.txt"
    payload.write_text("\n".join(lines) + "\n")
    return payload


def build_arms(workdir: Path) -> list[str]:
    """Register three arms differing only in which hook event injects."""
    payload = build_payload(workdir)

    shim = workdir / "inject.sh"
    shim.write_text(f"#!/bin/bash\ncat > /dev/null\ncat {payload}\nexit 0\n")
    shim.chmod(0o755)

    empty = workdir / "settings-none.json"
    empty.write_text(json.dumps({"hooks": {}}))

    names = []
    for arm_name, event in (
        ("pos-none", None),
        ("pos-ups", "UserPromptSubmit"),
        ("pos-sessionstart", "SessionStart"),
    ):
        if event is None:
            settings = empty
        else:
            settings = workdir / f"settings-{event}.json"
            settings.write_text(json.dumps({
                "hooks": {event: [{"matcher": "*", "hooks": [
                    {"type": "command", "command": str(shim)}
                ]}]}
            }, indent=2))

        arm = Arm(
            name=arm_name,
            env={"CONTEXTMESH_DISABLE": "1"},  # isolate: only this shim injects
            settings=settings,
            delivery_marker="ContextMesh memory" if event else None,
            expects_marker=bool(event),
        )
        ARM_SPECS[arm_name] = arm
        ARMS[arm_name] = dict(arm.env)
        names.append(arm_name)
    return names


def cache_breakdown(matrix: Matrix, arm: str) -> dict:
    reads, writes, billed, costs = [], [], [], []
    for result in matrix.for_arm(arm):
        if result.cli_error:
            continue
        path = result.transcript or find_transcript(result.session_id)
        if not path:
            continue
        usage = parse_session(path).usage
        reads.append(usage.cache_read_tokens)
        writes.append(usage.cache_write_1h_tokens + usage.cache_write_5m_tokens)
        billed.append(usage.billed_input_equivalent)
        costs.append(result.cost_usd)
    if not billed:
        return {}
    mean = statistics.fmean
    return {
        "n": len(billed),
        "cache_read": mean(reads),
        "cache_write": mean(writes),
        "billed": mean(billed),
        "cost": mean(costs),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replicates", type=int, default=3)
    parser.add_argument("--workdir", type=Path,
                        default=Path(os.environ.get("TMPDIR", "/tmp")) / "cm-posbench")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    workdir = args.workdir
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    repo = workdir / "work"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)

    arm_names = build_arms(workdir)
    task = Task(
        task_id="threefiles",
        prompt=TASK_PROMPT,
        repo=repo,
        verify="test -f a.txt && test -f b.txt && test -f c.txt",
        setup="rm -f *.txt",
        timeout_s=300,
        settings=workdir / "settings-none.json",
    )

    total = args.replicates * len(arm_names)
    print(f"arms  : {', '.join(arm_names)}")
    print(f"runs  : {total} (+1 warm-up)  payload ~440 tokens, identical in both injecting arms\n")

    done = {"n": 0}

    def progress(result):
        done["n"] += 1
        status = "ok" if result.verified else ("ERR" if result.cli_error else "unverified")
        print(f"[{done['n']:2d}/{total}] {result.arm:<18} r{result.replicate} "
              f"{status:<11} ${result.cost_usd:.4f} {result.turns:>2} turns")

    matrix = run_matrix([task], replicates=args.replicates, arms=arm_names, on_result=progress)

    print(f"\n{'arm':<18}{'n':>3}{'cache_read':>12}{'cache_write':>13}{'billed_eq':>11}{'cost':>9}")
    baseline = cache_breakdown(matrix, "pos-none")
    for arm in arm_names:
        stats = cache_breakdown(matrix, arm)
        if not stats:
            print(f"{arm:<18} no usable runs")
            continue
        delta = ""
        if baseline and arm != "pos-none":
            delta = f"   billed {stats['billed'] - baseline['billed']:+,.0f} vs none"
        print(f"{arm:<18}{stats['n']:>3}{stats['cache_read']:>12,.0f}"
              f"{stats['cache_write']:>13,.0f}{stats['billed']:>11,.0f}"
              f"{stats['cost']:>9.4f}{delta}")

    print("\nCache reads bill at 0.1x, writes at 1.25x (5m) or 2.0x (1h).")
    print("A position that converts reads into writes costs ~20x on the moved tokens.")

    out = args.out or (workdir / "results.json")
    out.write_text(matrix.to_json())
    print(f"\nraw results: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
