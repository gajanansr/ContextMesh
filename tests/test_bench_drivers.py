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
