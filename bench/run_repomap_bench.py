"""Driver: does the AST RepoMap pay for the ~2,650 tokens it injects?

Usage: python -m bench.run_repomap_bench [--replicates N] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from bench.corpus_repomap import build_fixture, build_tasks, index_fixture
from bench.report import format_report
from bench.run_memory_bench import _acquire_lock, _delivery_report, _hook_settings
from bench.runner import run_matrix

BASELINE_ARM = "norepomap"
TREATMENT_ARM = "repomap"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replicates", type=int, default=3)
    parser.add_argument("--workdir", type=Path,
                        default=Path(os.environ.get("TMPDIR", "/tmp")) / "cm-repomapbench")
    parser.add_argument("--dry-run", action="store_true")
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
    index_fixture(fixture.root, data_dir)
    tasks = build_tasks(fixture, data_dir, _hook_settings(workdir))

    total = len(tasks) * (args.replicates * 2 + 1)
    print(f"fixture : {fixture.root}")
    print(f"tasks   : {', '.join(t.task_id for t in tasks)}")
    print(f"arms    : {TREATMENT_ARM} vs {BASELINE_ARM} (memory off in both)")
    print(f"runs    : {total}\n")

    if args.dry_run:
        for task in tasks:
            print(f"--- {task.task_id}\n    {task.prompt[:78]}…\n    verify: {task.verify}")
        lock.unlink(missing_ok=True)
        return 0

    done = {"n": 0}

    def progress(result):
        done["n"] += 1
        status = "ok" if result.verified else ("ERR" if result.cli_error else "unverified")
        print(f"[{done['n']:2d}/{total - len(tasks)}] {result.task_id:<16} "
              f"{result.arm:<10} r{result.replicate} {status:<11} "
              f"${result.cost_usd:.4f} {result.turns:>2} turns {result.duration_s:5.1f}s")

    matrix = run_matrix(tasks, replicates=args.replicates,
                        arms=[BASELINE_ARM, TREATMENT_ARM], on_result=progress)

    print("\n" + _delivery_report(workdir, matrix))
    print("\n" + format_report(matrix, baseline=BASELINE_ARM, treatment=TREATMENT_ARM))

    out = args.out or (workdir / "results.json")
    out.write_text(matrix.to_json())
    print(f"\nraw results: {out}")
    lock.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
