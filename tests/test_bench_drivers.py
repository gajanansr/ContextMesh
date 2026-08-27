"""Smoke tests for the benchmark drivers.

A driver that fails to import wastes a whole run before anyone notices -- a
refactor of the shared delivery check deleted the lock helper out from under
the repomap driver, and the only symptom was a benchmark that produced nothing.
"""

import importlib

import pytest

DRIVERS = ("bench.run_memory_bench", "bench.run_repomap_bench")


@pytest.mark.parametrize("module", DRIVERS)
def test_driver_imports(module):
    importlib.import_module(module)


@pytest.mark.parametrize("module", DRIVERS)
def test_driver_exposes_main(module):
    assert callable(importlib.import_module(module).main)


def test_shared_helpers_are_available_to_both():
    """The repomap driver borrows these from the memory driver."""
    memory = importlib.import_module("bench.run_memory_bench")
    assert callable(memory._acquire_lock)
    assert callable(memory._hook_settings)


def test_repomap_arms_are_registered():
    from bench.run_repomap_bench import BASELINE_ARM, TREATMENT_ARM
    from bench.runner import ARMS

    assert BASELINE_ARM in ARMS and TREATMENT_ARM in ARMS
    # Memory must be off in both, or the map is not the only variable.
    assert ARMS[BASELINE_ARM]["CONTEXTMESH_NO_MEMORY"] == "1"
    assert ARMS[TREATMENT_ARM]["CONTEXTMESH_NO_MEMORY"] == "1"
    # And the map itself must differ between them.
    assert "CONTEXTMESH_NO_REPOMAP" in ARMS[BASELINE_ARM]
    assert "CONTEXTMESH_NO_REPOMAP" not in ARMS[TREATMENT_ARM]


def test_preflight_reports_clean_when_hook_honours_toggles(monkeypatch):
    import subprocess

    from bench import runner

    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, "", ""),
    )
    assert runner.preflight_arms() == []


def test_preflight_flags_a_hook_that_ignores_toggles(monkeypatch):
    """The contamination that leaked the RepoMap into 2 of 9 baseline runs."""
    import subprocess

    from bench import runner

    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, "x" * 5000, ""),
    )
    problems = runner.preflight_arms()

    assert problems
    assert "reinstall" in problems[0]


def test_preflight_tolerates_hook_not_installed(monkeypatch):
    from bench import runner

    def boom(*a, **k):
        raise OSError("not found")

    monkeypatch.setattr(runner.subprocess, "run", boom)
    assert runner.preflight_arms() == []


# ── cross-tool arms ─────────────────────────────────────────────────────────

def test_contextmesh_arms_are_all_verifiable():
    """Our own arms must always prove delivery -- that check has rescued 3 runs."""
    from bench.arms import CONTEXTMESH_ARMS

    assert all(arm.verifiable for arm in CONTEXTMESH_ARMS.values())


def test_baseline_arms_expect_the_marker_absent():
    """A control that expects the treatment present cannot catch leakage."""
    from bench.arms import CONTEXTMESH_ARMS

    assert CONTEXTMESH_ARMS["off"].expects_marker is False
    assert CONTEXTMESH_ARMS["norepomap"].expects_marker is False


def test_repomap_arms_isolate_the_map_from_memory():
    from bench.arms import CONTEXTMESH_ARMS

    for name in ("repomap", "norepomap"):
        assert CONTEXTMESH_ARMS[name].env["CONTEXTMESH_NO_MEMORY"] == "1"


def test_third_party_arms_disable_contextmesh():
    """Two context layers at once measures neither."""
    from bench.arms import THIRD_PARTY_ARMS

    assert all(a.env.get("CONTEXTMESH_DISABLE") == "1" for a in THIRD_PARTY_ARMS.values())


def test_third_party_arms_declare_themselves_unverifiable():
    """Honesty requirement: we cannot confirm a proxy-based tool acted.

    Claiming otherwise would be the same error this harness exists to catch,
    aimed at a competitor.
    """
    from bench.arms import THIRD_PARTY_ARMS

    assert all(not a.verifiable for a in THIRD_PARTY_ARMS.values())


def test_missing_tools_are_skipped_with_a_reason_not_silently():
    from bench.arms import check_availability

    runnable, skipped = check_availability(["off", "headroom", "rtk"])

    assert "off" in runnable
    assert any("headroom" in s and "not installed" in s for s in skipped)
    assert any("rtk" in s for s in skipped)


def test_unknown_arm_is_rejected():
    from bench.arms import resolve

    with pytest.raises(ValueError, match="unknown arm"):
        resolve("nonexistent")
