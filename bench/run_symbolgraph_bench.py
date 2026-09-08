"""Driver: does symbolgraph's retrieval saving survive an agent loop?

symbolgraph publishes "87% fewer context tokens", measured as the size of a
context pack against the size of the ground-truth files a query needed. No
agent runs in that measurement. This runs one, bills it cache-aware, and asks
the question the retrieval number cannot reach: does the agent that has the
tool finish the task for less than the agent that does not?

## The pairing

    sg-on vs sg-off — same agent, same tasks, same model; the MCP server is
    the only difference. Both keep Read and Grep.

Leaving Read and Grep available to sg-on is deliberate. Removing them would
measure a tool nobody uses that way, and would hide the failure this exists to
catch: a pack that does not answer the question, followed by a full file read,
billed on top of the pack rather than instead of it.

## Why this can go either way

symbolgraph is *substitutive* — the agent pulls a pack instead of reading a
file — where ContextMesh's RepoMap was *additive*, pushed into every session
on top of whatever the agent did anyway. Amplification multiplies both sides
of a substitutive comparison, so it cancels; it did not cancel for the
RepoMap, which is why the RepoMap lost. That difference is the reason this
run is worth doing rather than assuming the answer.

Usage: python -m bench.run_symbolgraph_bench --sg-bin PATH [--replicates N]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from bench.arms import symbolgraph_arms
from bench.corpus_symbolgraph import build_fixture, build_tasks, index_fixture
from bench.report import delivery_report, format_report, invoked_tool, success_rate
from bench.runner import Matrix, RunResult, find_transcript, register_arms, run_matrix
from bench.transcript import parse_session

REPO_ROOT = Path(__file__).resolve().parent.parent
ARMS = ["sg-off", "sg-on"]
MARKER = "mcp__symbolgraph"


def write_mcp_config(workdir: Path, sg_mcp: Path) -> Path:
    """A --mcp-config naming only symbolgraph.

    Paired with --strict-mcp-config on both arms, so the operator's own MCP
    servers cannot join either side of the comparison.
    """
    path = workdir / "mcp-symbolgraph.json"
    path.write_text(json.dumps({
        "mcpServers": {"symbolgraph": {"command": str(sg_mcp), "args": []}}
    }, indent=2))
    return path


def resolve_binaries(sg_bin: str | None) -> tuple[Path, Path]:
    """Locate `sg` and `sg-mcp`, preferring an explicit --sg-bin."""
    if sg_bin:
        sg = Path(sg_bin).expanduser().resolve()
    else:
        found = shutil.which("sg")
        if not found:
            raise SystemExit(
                "sg not found. Pass --sg-bin /path/to/.venv/bin/sg "
                "(the symbolgraph checkout's venv is fine)."
            )
        sg = Path(found)
    mcp = sg.with_name("sg-mcp")
    if not sg.exists():
        raise SystemExit(f"sg binary not found at {sg}")
    if not mcp.exists():
        raise SystemExit(f"sg-mcp not found next to sg at {mcp}")
    return sg, mcp


def reanalyse(path: Path) -> Matrix:
    """Rebuild a Matrix from saved rows by re-parsing each run's transcript.

    The agent runs are the expensive, non-deterministic part; locating and
    parsing their transcripts is neither. When only the parse was broken --
    as it was when find_transcript ignored CLAUDE_CONFIG_DIR -- re-running
    the agents would burn tokens to reproduce data already on disk, and would
    reproduce it slightly differently. Matrix.from_json cannot be used here
    because it replays the stored (zeroed) costs rather than re-reading them.
    """
    matrix = Matrix()
    missing = 0
    for row in json.loads(path.read_text()):
        result = RunResult(
            task_id=row["task_id"], arm=row["arm"], replicate=row["replicate"],
            session_id=row.get("session_id", ""), verified=row.get("verified"),
            cli_error=bool(row.get("cli_error")),
            cli_cost_usd=float(row.get("cli_cost_usd") or 0.0),
            cli_num_turns=int(row.get("turns") or 0),
            duration_s=float(row.get("duration_s") or 0.0),
            error=row.get("error", ""),
        )
        result.transcript = find_transcript(result.session_id)
        if result.transcript:
            result.session = parse_session(result.transcript)
        else:
            missing += 1
        matrix.add(result)
    if missing:
        print(f"WARNING: {missing} transcript(s) still not found; those runs "
              f"contribute zeros and the comparison is not trustworthy.")
    return matrix


def report(matrix: Matrix) -> None:
    print("\n" + "=" * 72)
    # invoked_tool, not the substring default: a registered-but-unused MCP
    # server passes a substring check while measuring nothing.
    print(delivery_report(matrix, MARKER, "symbolgraph calls",
                          treatment_arm="sg-on", baseline_arm="sg-off",
                          detector=invoked_tool))
    for arm in ARMS:
        passed, total = success_rate(matrix, arm)
        print(f"delivery/verify: {arm:7s} {passed}/{total} tasks verified")
    print(format_report(matrix, baseline="sg-off", treatment="sg-on"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replicates", type=int, default=2)
    parser.add_argument("--sg-bin", default=None, help="Path to the `sg` executable")
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--output", default=None, help="Where to write result rows")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build and index the fixture, then stop.")
    parser.add_argument("--reanalyse", default=None, metavar="ROWS.json",
                        help="Re-parse transcripts for a completed run and "
                             "re-report, without running any agents.")
    args = parser.parse_args()

    if args.reanalyse:
        report(reanalyse(Path(args.reanalyse)))
        return 0

    sg, sg_mcp = resolve_binaries(args.sg_bin)

    workdir = Path(args.workdir).resolve() if args.workdir else (
        REPO_ROOT / "bench" / "results" / "_symbolgraph_work"
    )
    workdir.mkdir(parents=True, exist_ok=True)

    mcp_config = write_mcp_config(workdir, sg_mcp)
    register_arms(symbolgraph_arms(mcp_config, sg_bin=sg))

    print(f"Building fixture in {workdir} ...")
    fixture = build_fixture(workdir, sg)
    print(f"Indexing {fixture.root} with embeddings (slow, once) ...")
    index_fixture(fixture)

    tasks = build_tasks(fixture)
    print(f"{len(tasks)} tasks x {len(ARMS)} arms x {args.replicates} replicates "
          f"= {len(tasks) * len(ARMS) * args.replicates} runs "
          f"(+{len(tasks) * len(ARMS)} warm-up)")

    if args.dry_run:
        print("Dry run: fixture built and indexed, no agent runs.")
        return 0

    # Each row as it lands, so a long run is not all-or-nothing.
    rows: list[dict] = []
    out = Path(args.output) if args.output else (
        REPO_ROOT / "bench" / "results" / "symbolgraph.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    def on_result(result) -> None:
        rows.append(result.row())
        out.write_text(json.dumps(rows, indent=2) + "\n")
        status = "ok" if result.verified else ("FAIL" if result.verified is False else "?")
        print(f"  {result.task_id:9s} {result.arm:7s} r{result.replicate:<2d} "
              f"{status:4s} ${result.cost_usd:.4f} "
              f"billed={result.billed_input_equivalent:>9,.0f} turns={result.turns}")

    os.environ.setdefault("CONTEXTMESH_DISABLE", "1")
    matrix = run_matrix(
        tasks, replicates=args.replicates, arms=ARMS,
        model=args.model, on_result=on_result,
    )

    report(matrix)
    print(f"\nrows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
