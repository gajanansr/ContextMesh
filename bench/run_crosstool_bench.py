"""Driver: measure ContextMesh, Headroom and RTK on the same tasks.

No tool in this category publishes a reproducible, cache-aware measurement.
This runs several of them through one rig, on identical tasks, with identical
billing accounting, and reports each against the control that isolates its
own mechanism.

## Pairing

Arms are not all comparable to one baseline, and reporting them as if they
were would be misleading:

- ContextMesh `on` vs `off`      -- hooks live vs inert
- Headroom vs headroom-passthrough -- same proxy, optimization on vs off, so
  the proxy's own latency and overhead cancel and only compression is measured
- RTK vs `off`                   -- RTK's hook rewrites Bash calls; without it
  there is no hook at all

Comparing Headroom against a no-proxy baseline would confound compression with
the cost of running a proxy at all, which is why the passthrough arm exists.

## What this cannot show

ContextMesh's arms verify delivery by finding an injected block in the
transcript. Headroom compresses inside a proxy and RTK rewrites tool calls;
neither necessarily leaves transcript-visible evidence, so their arms are
UNVERIFIED. A number from an unverified arm is weaker evidence than one from a
verified arm, and the report says so rather than presenting them as equals.

Usage: python -m bench.run_crosstool_bench [--replicates N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from bench.arms import (
    HEADROOM_PASSTHROUGH_PORT,
    HEADROOM_PORT,
    check_availability,
    resolve,
)
from bench.corpus_repomap import build_fixture, build_tasks, index_fixture
from bench.report import compare, success_rate
from bench.run_memory_bench import _acquire_lock, _hook_settings
from bench.runner import Matrix, run_matrix

REPO_ROOT = Path(__file__).resolve().parent.parent

# (treatment, control) -- each tool against the baseline that isolates it.
PAIRINGS = [
    ("on", "off", "ContextMesh (memory)"),
    ("headroom", "headroom-passthrough", "Headroom (compression)"),
    ("rtk", "off", "RTK (output compression)"),
]


def proxy_listening(port: int) -> bool:
    result = subprocess.run(
        ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def rtk_settings(workdir: Path) -> Path:
    """A settings file wiring RTK's hook, without installing it globally.

    `rtk init -g` would rewrite the user's own ~/.claude/settings.json and
    collide with ContextMesh's hook. Generating the equivalent file here keeps
    the benchmark's configuration inside the benchmark.
    """
    settings = workdir / "rtk-settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "rtk hook"}],
            }]
        }
    }, indent=2))
    return settings


def preflight(arm_names: list[str]) -> list[str]:
    """Reasons the requested arms cannot run. Empty means good to go."""
    problems: list[str] = []

    _runnable, skipped = check_availability(arm_names)
    problems.extend(skipped)

    if "headroom" in arm_names and not proxy_listening(HEADROOM_PORT):
        problems.append(
            f"headroom: no proxy on :{HEADROOM_PORT} "
            f"(start it with `headroom proxy --port {HEADROOM_PORT}`)"
        )
    if "headroom-passthrough" in arm_names and not proxy_listening(HEADROOM_PASSTHROUGH_PORT):
        problems.append(
            f"headroom-passthrough: no proxy on :{HEADROOM_PASSTHROUGH_PORT} "
            f"(start it with `headroom proxy --port {HEADROOM_PASSTHROUGH_PORT} --no-optimize`)"
        )
    return problems


def format_comparison(matrix: Matrix, arm_names: list[str]) -> str:
    lines = ["Cross-tool comparison", "=" * 60, ""]

    lines.append("Task success:")
    for arm in arm_names:
        ok, total = success_rate(matrix, arm)
        pct = f"{100.0 * ok / total:.0f}%" if total else "n/a"
        lines.append(f"  {arm:<24} {ok}/{total} verified ({pct})")

    for treatment, control, label in PAIRINGS:
        if treatment not in arm_names or control not in arm_names:
            continue
        arm = resolve(treatment)
        lines += ["", f"{label}: {treatment} vs {control}"]
        if not arm.verifiable:
            lines.append("  UNVERIFIED — delivery cannot be confirmed from the")
            lines.append("  transcript for this tool. Weaker evidence than a verified arm.")
        for metric in ("cost_usd", "billed_input_equivalent", "turns"):
            c = compare(matrix, metric=metric, baseline=control, treatment=treatment)
            if not c.pairs:
                lines.append(f"  {metric:<26} no comparable pairs")
                continue
            lines.append(
                f"  {metric:<26} {c.mean_delta:+12,.4f}  "
                f"CI [{c.ci_low:+,.4f}, {c.ci_high:+,.4f}]  {c.verdict()}"
            )

    lines += [
        "",
        "Every baseline already includes prompt caching (~85% on its own).",
        "All deltas are on top of caching, not instead of it.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replicates", type=int, default=4)
    parser.add_argument(
        "--arms", default="off,on,headroom,headroom-passthrough",
        help="comma-separated arm names",
    )
    parser.add_argument("--workdir", type=Path,
                        default=Path(os.environ.get("TMPDIR", "/tmp")) / "cm-crosstool")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]

    problems = preflight(arm_names)
    if problems:
        for problem in problems:
            print(f"PREFLIGHT FAILED: {problem}")
        return 2

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
    rtk_settings(workdir)  # generated so an rtk arm can point at it

    total = len(tasks) * args.replicates * len(arm_names)
    print(f"fixture : {fixture.root}")
    print(f"tasks   : {', '.join(t.task_id for t in tasks)}")
    print(f"arms    : {', '.join(arm_names)}")
    print(f"runs    : {total} (+ {len(tasks)} warm-ups)\n")

    if args.replicates < len(arm_names):
        print(f"NOTE: {args.replicates} replicates for {len(arm_names)} arms means the")
        print("rotation does not complete a full cycle; leading arms keep a slight")
        print("cold-cache disadvantage.\n")

    if args.dry_run:
        lock.unlink(missing_ok=True)
        return 0

    done = {"n": 0}

    def progress(result):
        done["n"] += 1
        status = "ok" if result.verified else ("ERR" if result.cli_error else "unverified")
        print(f"[{done['n']:2d}/{total}] {result.task_id:<16} {result.arm:<22} "
              f"r{result.replicate} {status:<11} ${result.cost_usd:.4f} "
              f"{result.turns:>2} turns")

    matrix = run_matrix(tasks, replicates=args.replicates, arms=arm_names, on_result=progress)

    print("\n" + format_comparison(matrix, arm_names))

    out = args.out or (workdir / "results.json")
    out.write_text(matrix.to_json())
    print(f"\nraw results: {out}")
    lock.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
