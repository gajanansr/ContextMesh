"""Turn A/B runs into a verdict, including the verdict of "no difference".

A benchmark that can only produce a headline number is a marketing tool. With
agent runs the variance between replicates of the *same* arm is large, so the
default answer to "did this help?" is usually "we cannot tell yet" -- and this
module is built to say so.

Comparisons are paired by (task, replicate): the arms ran back to back under
the same conditions, so the difference within a pair cancels most of the drift
that makes unpaired means useless at these sample sizes.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from bench.runner import Matrix, RunResult, find_transcript

# Two-tailed 95% critical values of Student's t by degrees of freedom.
# A table avoids a scipy dependency for the handful of df a benchmark reaches.
_T_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
    27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def t_critical(df: int) -> float:
    if df <= 0:
        return float("inf")
    return _T_95.get(df, 1.96)


@dataclass(frozen=True)
class Comparison:
    """A paired comparison of one metric between two arms."""

    metric: str
    baseline_arm: str
    treatment_arm: str
    pairs: int
    baseline_mean: float
    treatment_mean: float
    mean_delta: float          # treatment - baseline; negative is an improvement
    ci_low: float
    ci_high: float

    @property
    def significant(self) -> bool:
        """True when the 95% interval excludes zero."""
        return self.pairs >= 2 and (self.ci_low > 0 or self.ci_high < 0)

    @property
    def percent_change(self) -> float | None:
        if not self.baseline_mean:
            return None
        return 100.0 * self.mean_delta / self.baseline_mean

    def verdict(self) -> str:
        if self.pairs < 2:
            return "insufficient data (need at least 2 paired runs)"
        if not self.significant:
            return "no significant difference"
        direction = "reduction" if self.mean_delta < 0 else "increase"
        pct = self.percent_change
        return f"{abs(pct):.1f}% {direction}" if pct is not None else direction


def _paired(matrix: Matrix, baseline: str, treatment: str, verified_only: bool):
    """Yield (baseline_run, treatment_run) for each task/replicate pair."""
    def key(r: RunResult) -> tuple[str, int]:
        return (r.task_id, r.replicate)

    base = {key(r): r for r in matrix.for_arm(baseline)}
    treat = {key(r): r for r in matrix.for_arm(treatment)}

    for k in sorted(base.keys() & treat.keys()):
        b, t = base[k], treat[k]
        if b.cli_error or t.cli_error:
            continue
        # A run that gave up early looks cheap. Comparing it to one that
        # finished measures nothing but the giving up.
        if verified_only and not (b.verified and t.verified):
            continue
        yield b, t


def compare(
    matrix: Matrix,
    metric: str = "cost_usd",
    baseline: str = "off",
    treatment: str = "on",
    verified_only: bool = True,
) -> Comparison:
    """Paired comparison of one metric. `metric` is any numeric RunResult attribute."""
    pairs = list(_paired(matrix, baseline, treatment, verified_only))
    base_values = [float(getattr(b, metric)) for b, _ in pairs]
    treat_values = [float(getattr(t, metric)) for _, t in pairs]
    deltas = [t - b for b, t in zip(base_values, treat_values)]

    n = len(deltas)
    mean_delta = statistics.fmean(deltas) if deltas else 0.0
    if n >= 2:
        stderr = statistics.stdev(deltas) / (n ** 0.5)
        margin = t_critical(n - 1) * stderr
    else:
        margin = float("inf") if n else 0.0

    return Comparison(
        metric=metric,
        baseline_arm=baseline,
        treatment_arm=treatment,
        pairs=n,
        baseline_mean=statistics.fmean(base_values) if base_values else 0.0,
        treatment_mean=statistics.fmean(treat_values) if treat_values else 0.0,
        mean_delta=mean_delta,
        ci_low=mean_delta - margin,
        ci_high=mean_delta + margin,
    )


def success_rate(matrix: Matrix, arm: str) -> tuple[int, int]:
    """(verified, attempted) for an arm. Cost means nothing without this."""
    runs = [r for r in matrix.for_arm(arm) if not r.cli_error]
    return sum(1 for r in runs if r.verified), len(runs)


def format_report(
    matrix: Matrix,
    baseline: str = "off",
    treatment: str = "on",
    metrics: tuple[str, ...] = ("cost_usd", "billed_input_equivalent", "turns"),
) -> str:
    """Human-readable summary. Leads with success rate, then per-metric verdicts."""
    lines = [
        f"ContextMesh benchmark: {treatment} vs {baseline}",
        "=" * 60,
        "",
        "Task success (a cost win with a success drop is a regression):",
    ]

    for arm in (baseline, treatment):
        ok, total = success_rate(matrix, arm)
        pct = f"{100.0 * ok / total:.0f}%" if total else "n/a"
        lines.append(f"  {arm:<10} {ok}/{total} verified ({pct})")

    base_ok, base_total = success_rate(matrix, baseline)
    treat_ok, treat_total = success_rate(matrix, treatment)
    if base_total and treat_total and treat_ok / treat_total < base_ok / base_total:
        lines.append("")
        lines.append("  WARNING: treatment completed fewer tasks. Cost deltas below")
        lines.append("  are not a win -- compare only across verified pairs.")

    lines += ["", "Paired comparisons (verified runs only):"]

    for metric in metrics:
        c = compare(matrix, metric=metric, baseline=baseline, treatment=treatment)
        lines.append("")
        lines.append(f"  {metric}  ({c.pairs} paired run(s))")
        if c.pairs == 0:
            lines.append("    no comparable pairs")
            continue
        lines.append(f"    {baseline:<10} mean {c.baseline_mean:,.4f}")
        lines.append(f"    {treatment:<10} mean {c.treatment_mean:,.4f}")
        lines.append(f"    delta      {c.mean_delta:+,.4f}")
        if c.pairs >= 2:
            lines.append(f"    95% CI     [{c.ci_low:+,.4f}, {c.ci_high:+,.4f}]")
        lines.append(f"    verdict    {c.verdict()}")

    lines += [
        "",
        "Note: the baseline already includes prompt caching, which reduces cost",
        "~85% on its own. Any delta here is on top of that, not instead of it.",
    ]
    return "\n".join(lines)


# Marker strings the hook writes into an injected block. Delivery is checked
# against the session transcript rather than a hook-side log, because the
# transcript is what the model actually received.
MEMORY_MARKER = "ContextMesh memory"
REPOMAP_MARKER = "ContextMesh RepoMap"


def received_marker(result: RunResult, marker: str) -> bool:
    """Whether this run's transcript contains an injected block."""
    path = result.transcript or find_transcript(result.session_id)
    if not path or not path.exists():
        return False
    try:
        return marker in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def delivery_report(
    matrix: Matrix,
    marker: str,
    label: str,
    treatment_arm: str = "on",
    baseline_arm: str = "off",
) -> str:
    """Confirm the treatment reached the treatment arm and only that arm.

    Without this, "no significant difference" is ambiguous between "the feature
    does not help" and "the feature never ran" -- and two runs of this harness
    reported a confident null for a treatment that had been silently wiped out.

    Zero comparable runs is itself a failure. An earlier version reported 0/0
    and said nothing, which is the exact silence the check exists to break.
    """
    lines = [f"Treatment delivery ({label} present in the session):"]
    problems = []

    for arm, expected in ((baseline_arm, False), (treatment_arm, True)):
        runs = [r for r in matrix.for_arm(arm) if not r.cli_error]
        got = sum(1 for r in runs if received_marker(r, marker))
        lines.append(
            f"  {arm:<12} {got}/{len(runs)} runs received it "
            f"(expected {'all' if expected else 'none'})"
        )
        if not runs:
            problems.append(f"{arm} has no usable runs")
        elif expected and got < len(runs):
            problems.append(f"{arm} was missing the treatment in {len(runs) - got} run(s)")
        elif not expected and got:
            problems.append(f"{arm} received the treatment in {got} run(s)")

    errored = [r for r in matrix.results if r.cli_error]
    if errored:
        lines.append(f"  ({len(errored)} run(s) failed outright and are excluded)")

    if problems:
        lines += ["", "  INVALID: " + "; ".join(problems) + ".",
                  "  Any comparison below measures nothing. Fix delivery, re-run."]
    return "\n".join(lines)
