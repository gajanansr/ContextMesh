"""Tests for benchmark reporting.

The property that matters most is the one a marketing-driven harness would
lack: noisy data must produce "no significant difference", not a headline.
"""

import pytest

from bench.report import Comparison, compare, format_report, success_rate, t_critical
from bench.runner import Matrix, RunResult


class FakeUsage:
    def __init__(self, billed):
        self.billed_input_equivalent = billed


class FakeSession:
    def __init__(self, cost):
        self._cost = cost
        self.usage = FakeUsage(cost * 100_000)

    @property
    def cost_usd(self):
        return self._cost

    @property
    def assistant_turns(self):
        return 5


def _run(arm, replicate, cost, turns=5, verified=True, task="t1", error=False):
    r = RunResult(task_id=task, arm=arm, replicate=replicate, verified=verified)
    r.cli_error = error
    r.cli_num_turns = turns
    r.session = FakeSession(cost)
    return r


def _matrix(off_costs, on_costs, **kw):
    m = Matrix()
    for i, (o, n) in enumerate(zip(off_costs, on_costs)):
        m.add(_run("off", i, o, **kw))
        m.add(_run("on", i, n, **kw))
    return m


def test_t_critical_shrinks_with_more_data():
    assert t_critical(1) > t_critical(10) > t_critical(30) >= 1.96
    assert t_critical(0) == float("inf")


def test_consistent_improvement_is_significant():
    m = _matrix([1.00, 1.02, 0.98, 1.01], [0.50, 0.51, 0.49, 0.50])
    c = compare(m, metric="cost_usd")

    assert c.pairs == 4
    assert c.mean_delta < 0
    assert c.significant
    assert "reduction" in c.verdict()


def test_noisy_data_yields_no_significant_difference():
    """The property a marketing harness would not have."""
    m = _matrix([1.0, 0.4, 1.6, 0.7], [0.9, 1.5, 0.5, 1.2])
    c = compare(m, metric="cost_usd")

    assert not c.significant
    assert c.verdict() == "no significant difference"


def test_regression_is_reported_as_an_increase():
    m = _matrix([0.50, 0.51, 0.49], [1.00, 1.02, 0.98])
    c = compare(m, metric="cost_usd")
    assert c.mean_delta > 0 and c.significant
    assert "increase" in c.verdict()


def test_single_pair_is_never_significant():
    """One run each cannot establish anything, however large the gap."""
    m = _matrix([10.0], [0.1])
    c = compare(m, metric="cost_usd")

    assert c.pairs == 1
    assert not c.significant
    assert "insufficient data" in c.verdict()


def test_unverified_pairs_excluded_by_default():
    m = _matrix([1.0, 1.0], [0.5, 0.5], verified=False)
    assert compare(m, metric="cost_usd").pairs == 0


def test_errored_runs_excluded():
    m = _matrix([1.0, 1.0], [0.5, 0.5], error=True)
    assert compare(m, metric="cost_usd").pairs == 0


def test_pairs_require_matching_task_and_replicate():
    m = Matrix()
    m.add(_run("off", 0, 1.0, task="a"))
    m.add(_run("on", 0, 0.5, task="b"))
    assert compare(m, metric="cost_usd").pairs == 0


def test_success_rate_counts_verified():
    m = Matrix()
    m.add(_run("on", 0, 1.0, verified=True))
    m.add(_run("on", 1, 1.0, verified=False))
    assert success_rate(m, "on") == (1, 2)


def test_report_warns_when_treatment_completes_fewer_tasks():
    """A cost win bought by giving up must not read as a win."""
    m = Matrix()
    m.add(_run("off", 0, 1.0, verified=True))
    m.add(_run("on", 0, 0.1, verified=False))

    text = format_report(m)

    assert "WARNING" in text
    assert "fewer tasks" in text


def test_report_mentions_the_caching_baseline():
    text = format_report(_matrix([1.0, 1.0], [0.5, 0.5]))
    assert "caching" in text.lower()


def test_percent_change_none_when_baseline_zero():
    c = Comparison("m", "off", "on", 2, 0.0, 1.0, 1.0, 0.5, 1.5)
    assert c.percent_change is None
