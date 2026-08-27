"""Tests for the command executor.

Everything the agent learns about a shell command comes through here, so the
failure modes that matter are the silent ones: output discarded on timeout,
and a failing command reported as success.
"""

import os

import pytest

from contextmesh.utils.executor import (
    DEFAULT_TIMEOUT_SECONDS,
    TIMEOUT_ENV_VAR,
    TIMEOUT_EXIT_CODE,
    execute,
    resolve_timeout,
)


def test_captures_stdout():
    assert "hello" in execute("echo hello").output


def test_captures_stderr_separately_labelled():
    result = execute("echo oops >&2")
    assert "oops" in result.output
    assert "[STDERR]" in result.output


def test_exit_code_is_propagated():
    """A failing test suite must not look like a passing one."""
    assert execute("exit 3").returncode == 3
    assert execute("true").returncode == 0


def test_timeout_preserves_partial_output():
    """The bug this module exists for: a hung command returned literally None."""
    result = execute("echo useful-line; sleep 30", timeout=1)

    assert result.timed_out
    assert "useful-line" in result.output
    assert result.returncode == TIMEOUT_EXIT_CODE


def test_timeout_explains_itself_to_the_model():
    result = execute("sleep 30", timeout=1)
    assert "exceeded 1s" in result.output
    assert TIMEOUT_ENV_VAR in result.output


def test_successful_command_is_not_marked_timed_out():
    assert execute("echo fine").timed_out is False


def test_default_timeout_is_long_enough_for_a_build():
    assert resolve_timeout() >= 1800
    assert DEFAULT_TIMEOUT_SECONDS >= 1800


def test_explicit_timeout_wins():
    assert resolve_timeout(42) == 42


def test_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "60")
    assert resolve_timeout() == 60


def test_non_positive_env_disables_the_limit(monkeypatch):
    """Asking for no limit must not silently yield a short one."""
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "0")
    assert resolve_timeout() == 0


def test_garbage_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "soon")
    assert resolve_timeout() == DEFAULT_TIMEOUT_SECONDS


def test_disabled_timeout_still_runs_the_command(monkeypatch):
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "0")
    assert "ok" in execute("echo ok").output
