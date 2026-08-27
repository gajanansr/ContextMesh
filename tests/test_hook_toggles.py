"""Tests for which blocks the hook injects.

The RepoMap default is off because it was measured and lost: cost +45.6%
significant, turns -0.33 not significant. These pin that decision so it cannot
be flipped back by accident -- only by a benchmark that earns it.
"""

import pytest

from contextmesh import hook


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (hook.DISABLE_ENV_VAR, hook.NO_REPOMAP_ENV_VAR,
                hook.NO_MEMORY_ENV_VAR, hook.REPOMAP_ENV_VAR):
        monkeypatch.delenv(var, raising=False)


def test_repomap_is_off_by_default():
    """It costs 45.6% more and does not reduce turns. Opt in deliberately."""
    assert hook.repomap_enabled() is False


def test_memory_is_on_by_default():
    """Memory earned it: -17.9% turns at no significant cost."""
    assert hook.memory_enabled() is True


def test_repomap_opt_in(monkeypatch):
    monkeypatch.setenv(hook.REPOMAP_ENV_VAR, "1")
    assert hook.repomap_enabled() is True


def test_explicit_suppression_beats_opt_in(monkeypatch):
    """NO_REPOMAP must win, so a benchmark control arm cannot be overridden."""
    monkeypatch.setenv(hook.REPOMAP_ENV_VAR, "1")
    monkeypatch.setenv(hook.NO_REPOMAP_ENV_VAR, "1")
    assert hook.repomap_enabled() is False


def test_memory_can_be_suppressed(monkeypatch):
    monkeypatch.setenv(hook.NO_MEMORY_ENV_VAR, "1")
    assert hook.memory_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_spellings_accepted(monkeypatch, value):
    monkeypatch.setenv(hook.REPOMAP_ENV_VAR, value)
    assert hook.repomap_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "  "])
def test_falsey_spellings_rejected(monkeypatch, value):
    monkeypatch.setenv(hook.REPOMAP_ENV_VAR, value)
    assert hook.repomap_enabled() is False


def test_disable_switch_kills_everything(monkeypatch):
    monkeypatch.setenv(hook.DISABLE_ENV_VAR, "1")
    assert hook.is_disabled() is True


def test_benchmark_treatment_arm_opts_in():
    """Defaulting the map off must not silently empty the treatment arm."""
    from bench.runner import ARMS

    assert ARMS["repomap"]["CONTEXTMESH_REPOMAP"] == "1"
    assert "CONTEXTMESH_REPOMAP" not in ARMS["norepomap"]
