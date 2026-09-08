"""Driver: does shunting large reads to a cheaper model reduce *total* cost?

Spotify's Portal reports ~90% savings for this architecture. Their eval file
says what that measures -- "Claude context tokens with vs without shunt" -- and
the delegate's tokens appear nowhere. The files are still read in full, by a
model. The tokens moved; whether anyone saved anything depends on what the
delegate costs, which is the quantity the published number omits.

So the headline metric here is `total_cost_usd`: the parent session plus every
delegate call it made. Reporting the parent alone would reproduce the exact
error this run exists to test, and it would show a large fake win -- the whole
point of a shunt is that the parent's number goes down.

## Pairing

    shunt-on vs shunt-off -- same agent, same tasks, same model. The only
    difference is a PreToolUse hook that blocks Read above 350 lines and names
    the delegate script instead.

## Delivery

Proven by spend, not by substring. The delegate appends its billed cost to a
log which is truncated before every run, so a run where the delegate actually
ran has a non-zero delegate cost and one where it did not has zero. This
matters: the block reason naming `bulk-read` lands in the transcript whether
or not the agent ever runs it, so a substring check would report delivery for
a run that shunted nothing -- the same false pass that invalidated two
symbolgraph runs before it was caught.

`control` reads nothing, so its delegate cost is expected to be zero; it is
excluded from the delivery expectation rather than counted as a failure.

Usage: python -m bench.run_shunt_bench [--replicates N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from statistics import mean

from bench.arms import Arm
from bench.corpus_shunt import build_fixture, build_tasks
from bench.report import success_rate
from bench.runner import register_arms, run_matrix
from bench.shunt import MIN_LINES, blocked_reads, delegate_cost, install

REPO_ROOT = Path(__file__).resolve().parent.parent
ARMS = ["shunt-off", "shunt-on"]
# Reads nothing, so the delegate can never bill on it.
NO_READ_TASKS = {"control"}


def shunt_arms(settings: Path, cost_log: Path, block_log: Path) -> dict[str, Arm]:
    # Truncating the log in each arm's setup is what makes per-run delegate
    # spend attributable: runs are strictly sequential, so whatever is in the
    # log when a run finishes belongs to that run.
    truncate = f": > {cost_log}; : > {block_log}"
    return {
        "shunt-off": Arm(
            name="shunt-off",
            env={"CONTEXTMESH_DISABLE": "1"},
            setup=truncate,
            notes="Plain Claude Code. Reads whatever it wants, at full price.",
        ),
        "shunt-on": Arm(
            name="shunt-on",
            env={"CONTEXTMESH_DISABLE": "1"},
            settings=settings,
            setup=truncate,
            notes=(
                f"PreToolUse hook blocks Read over {MIN_LINES} lines and points "
                "at the Haiku delegate. Offset/limit reads still pass, so the "
                "agent is redirected rather than blinded."
            ),
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replicates", type=int, default=3)
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", default=None,
                        help="Comma-separated task ids to run (default: all)")
    args = parser.parse_args()

    workdir = Path(args.workdir).resolve() if args.workdir else (
        REPO_ROOT / "bench" / "results" / "_shunt_work"
    )
    workdir.mkdir(parents=True, exist_ok=True)

    settings, cost_log, block_log = install(workdir)
    register_arms(shunt_arms(settings, cost_log, block_log))

    print(f"Building fixture in {workdir} ...")
    fixture = build_fixture(workdir)
    tasks = build_tasks(fixture)
    if args.only:
        wanted = {t.strip() for t in args.only.split(",") if t.strip()}
        tasks = [t for t in tasks if t.task_id in wanted]
        if not tasks:
            raise SystemExit(f"no tasks matched {sorted(wanted)}")
    print(f"{len(tasks)} tasks x {len(ARMS)} arms x {args.replicates} replicates "
          f"= {len(tasks) * len(ARMS) * args.replicates} runs "
          f"(+{len(tasks) * len(ARMS)} warm-up)")

    if args.dry_run:
        print("Dry run: fixture built, no agent runs.")
        return 0

    out = Path(args.output) if args.output else (
        REPO_ROOT / "bench" / "results" / "shunt.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []

    def on_result(result) -> None:
        # Read before the next run's setup truncates it.
        d = delegate_cost(cost_log)
        row = result.row()
        row["delegate_cost_usd"] = round(d, 6)
        row["blocked_reads"] = blocked_reads(block_log)
        row["total_cost_usd"] = round(result.cost_usd + d, 6)
        rows.append(row)
        out.write_text(json.dumps(rows, indent=2) + "\n")
        status = "ok" if result.verified else ("FAIL" if result.verified is False else "?")
        print(f"  {result.task_id:10s} {result.arm:9s} r{result.replicate:<2d} {status:4s} "
              f"parent=${result.cost_usd:.4f} delegate=${d:.4f} "
              f"total=${row['total_cost_usd']:.4f} blocked={row['blocked_reads']} "
              f"turns={result.turns}")

    os.environ.setdefault("CONTEXTMESH_DISABLE", "1")
    matrix = run_matrix(tasks, replicates=args.replicates, arms=ARMS,
                        model=args.model, on_result=on_result)

    print("\n" + "=" * 72)
    report(rows, matrix)
    print(f"\nrows -> {out}")
    return 0


def report(rows: list[dict], matrix=None) -> None:
    read_rows = [r for r in rows if r["task_id"] not in NO_READ_TASKS]
    on_read = [r for r in read_rows if r["arm"] == "shunt-on"]
    fired = sum(1 for r in on_read if r["delegate_cost_usd"] > 0)
    off_billed = sum(1 for r in read_rows
                     if r["arm"] == "shunt-off" and r["delegate_cost_usd"] > 0)

    attempts = sum(r["blocked_reads"] for r in on_read)
    print("Premise check: large reads the agent actually attempted")
    print(f"  shunt-on   {attempts} blocked read(s) across {len(on_read)} run(s)")
    if attempts == 0:
        print("  The hook never fired. The agent never tried to read a large")
        print("  file, so there was no expensive read for a shunt to replace.")
    print()
    print("Delivery (delegate actually billed, read tasks only):")
    print(f"  shunt-on   {fired}/{len(on_read)} runs shunted (expected all)")
    print(f"  shunt-off  {off_billed}/{len(read_rows) - len(on_read)} "
          f"runs shunted (expected none)")
    if fired < len(on_read) or off_billed:
        print("\n  INVALID: delivery is not clean. Any comparison below "
              "measures something other than the shunt.\n")

    if matrix is not None:
        for arm in ARMS:
            passed, total = success_rate(matrix, arm)
            print(f"  verified: {arm:9s} {passed}/{total}")

    print("\nPer task (mean):")
    print(f"{'task':11s} {'arm':9s} {'parent':>9s} {'delegate':>9s} {'TOTAL':>9s}  turns")
    for task in sorted({r["task_id"] for r in rows}):
        for arm in ARMS:
            sel = [r for r in rows if r["task_id"] == task and r["arm"] == arm]
            if not sel:
                continue
            print(f"{task:11s} {arm:9s} "
                  f"{mean(r['cost_usd'] for r in sel):9.4f} "
                  f"{mean(r['delegate_cost_usd'] for r in sel):9.4f} "
                  f"{mean(r['total_cost_usd'] for r in sel):9.4f}  "
                  f"{mean(r['turns'] for r in sel):.1f}")

    # The headline. Parent-only is printed beside it precisely because that is
    # the number the published claim reports, and the gap between the two is
    # the finding.
    print("\nRead tasks only, shunt-on vs shunt-off:")
    for label, key in (("parent only (what Portal reports)", "cost_usd"),
                       ("TOTAL (both models)", "total_cost_usd")):
        off = mean(r[key] for r in read_rows if r["arm"] == "shunt-off")
        on = mean(r[key] for r in read_rows if r["arm"] == "shunt-on")
        print(f"  {label:36s} {off:.4f} -> {on:.4f}  "
              f"{100 * (on - off) / off:+.1f}%")


if __name__ == "__main__":
    raise SystemExit(main())
