"""Tests for the A/B runner.

Subprocess is mocked throughout -- these must not spend API tokens.
The ordering tests exist because cache-order bias was measured at 8x on a
trivial task, which is larger than any effect the benchmark is looking for.
"""

import json
import subprocess

import pytest

from bench import runner
from bench.runner import ARMS, Matrix, Task, run_matrix, run_once


@pytest.fixture
def task(tmp_path):
    return Task(task_id="t1", prompt="do a thing", repo=tmp_path, timeout_s=30)


def _cli_payload(**over):
    payload = {
        "is_error": False,
        "session_id": "sess-1",
        "total_cost_usd": 0.25,
        "num_turns": 4,
        "result": "done",
    }
    payload.update(over)
    return json.dumps(payload)


def _fake_run(monkeypatch, stdout="", calls=None, raises=None):
    def fake(cmd, **kwargs):
        if calls is not None:
            calls.append({"cmd": cmd, "env": kwargs.get("env", {}), "cwd": kwargs.get("cwd")})
        if raises:
            raise raises
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(runner.subprocess, "run", fake)


def test_off_arm_disables_the_hook_and_on_arm_does_not():
    assert ARMS["off"]["CONTEXTMESH_DISABLE"] == "1"
    assert "CONTEXTMESH_DISABLE" not in ARMS["on"]


def test_unknown_arm_rejected(task):
    with pytest.raises(ValueError):
        run_once(task, "sideways", 0)


def test_off_arm_passes_disable_env(monkeypatch, task):
    calls = []
    _fake_run(monkeypatch, _cli_payload(), calls)
    monkeypatch.setattr(runner, "find_transcript", lambda *a, **k: None)

    run_once(task, "off", 0)

    assert calls[0]["env"]["CONTEXTMESH_DISABLE"] == "1"


def test_on_arm_does_not_set_disable_env(monkeypatch, task):
    calls = []
    _fake_run(monkeypatch, _cli_payload(), calls)
    monkeypatch.setattr(runner, "find_transcript", lambda *a, **k: None)

    run_once(task, "on", 0)

    assert "CONTEXTMESH_DISABLE" not in calls[0]["env"]


def test_model_is_pinned_when_given(monkeypatch, task):
    calls = []
    _fake_run(monkeypatch, _cli_payload(), calls)
    monkeypatch.setattr(runner, "find_transcript", lambda *a, **k: None)

    run_once(task, "on", 0, model="claude-opus-5")

    assert "--model" in calls[0]["cmd"]
    assert "claude-opus-5" in calls[0]["cmd"]


def test_cli_error_is_captured(monkeypatch, task):
    _fake_run(monkeypatch, _cli_payload(is_error=True, result="Not logged in"))
    monkeypatch.setattr(runner, "find_transcript", lambda *a, **k: None)

    result = run_once(task, "on", 0)

    assert result.cli_error is True
    assert "Not logged in" in result.error


def test_unparseable_output_is_an_error_not_a_crash(monkeypatch, task):
    _fake_run(monkeypatch, "not json at all")
    result = run_once(task, "on", 0)
    assert result.cli_error is True
    assert "unparseable" in result.error


def test_timeout_is_recorded(monkeypatch, task):
    _fake_run(monkeypatch, raises=subprocess.TimeoutExpired("claude", 30))
    result = run_once(task, "on", 0)
    assert result.cli_error is True
    assert "timed out" in result.error


def test_verify_command_decides_success(monkeypatch, tmp_path):
    t = Task(task_id="t", prompt="p", repo=tmp_path, verify="true")
    _fake_run(monkeypatch, _cli_payload())
    monkeypatch.setattr(runner, "find_transcript", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_shell", lambda *a, **k: (0, ""))

    assert run_once(t, "on", 0).verified is True


def test_failed_verify_marks_run_unverified(monkeypatch, tmp_path):
    t = Task(task_id="t", prompt="p", repo=tmp_path, verify="false")
    _fake_run(monkeypatch, _cli_payload())
    monkeypatch.setattr(runner, "find_transcript", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_shell", lambda *a, **k: (1, "assertion failed"))

    result = run_once(t, "on", 0)
    assert result.verified is False
    assert "verify failed" in result.error


def test_setup_failure_skips_the_run(monkeypatch, tmp_path):
    t = Task(task_id="t", prompt="p", repo=tmp_path, setup="false")
    calls = []
    _fake_run(monkeypatch, _cli_payload(), calls)
    monkeypatch.setattr(runner, "_shell", lambda *a, **k: (1, "boom"))

    result = run_once(t, "on", 0)

    assert "setup failed" in result.error
    assert calls == []  # claude never invoked


def test_matrix_counterbalances_arm_order(monkeypatch, task):
    """Neither arm may systematically occupy the cold-cache first slot."""
    order = []

    def fake_once(t, arm, replicate, model=None):
        order.append((replicate, arm))
        return runner.RunResult(task_id=t.task_id, arm=arm, replicate=replicate)

    monkeypatch.setattr(runner, "run_once", fake_once)
    run_matrix([task], replicates=2, arms=["on", "off"], warmup=False)

    assert order == [(0, "on"), (0, "off"), (1, "off"), (1, "on")]


def test_rotation_generalises_beyond_two_arms(monkeypatch, task):
    """A cross-tool comparison has more than two arms; ABBA does not cover it."""
    order = []

    def fake_once(t, arm, replicate, model=None):
        order.append(arm)
        return runner.RunResult(task_id=t.task_id, arm=arm, replicate=replicate)

    monkeypatch.setattr(runner, "run_once", fake_once)
    run_matrix([task], replicates=3, arms=["a", "b", "c"], warmup=False)

    # Each replicate rotates the starting arm.
    assert order == ["a", "b", "c", "b", "c", "a", "c", "a", "b"]
    # Over a full cycle every arm leads exactly once, so none owns the cold slot.
    assert sorted(order[0::3]) == ["a", "b", "c"]


def test_matrix_runs_and_discards_a_warmup(monkeypatch, task):
    seen = []

    def fake_once(t, arm, replicate, model=None):
        seen.append(replicate)
        return runner.RunResult(task_id=t.task_id, arm=arm, replicate=replicate)

    monkeypatch.setattr(runner, "run_once", fake_once)
    matrix = run_matrix([task], replicates=1, arms=["on", "off"], warmup=True)

    assert -1 in seen                                    # warm-up executed
    assert all(r.replicate >= 0 for r in matrix.results)  # and excluded


def test_find_transcript_locates_session(tmp_path):
    d = tmp_path / "projects" / "some-project"
    d.mkdir(parents=True)
    (d / "abc.jsonl").write_text("{}")

    assert runner.find_transcript("abc", config_dir=tmp_path) == d / "abc.jsonl"
    assert runner.find_transcript("nope", config_dir=tmp_path) is None
    assert runner.find_transcript("", config_dir=tmp_path) is None


def test_matrix_serializes(task):
    m = Matrix()
    m.add(runner.RunResult(task_id="t", arm="on", replicate=0))
    assert json.loads(m.to_json())[0]["arm"] == "on"


def test_find_transcript_honours_claude_config_dir(tmp_path, monkeypatch):
    """A custom CLAUDE_CONFIG_DIR must not zero out every measurement.

    When the transcript is not found, `verified` and `turns` still populate
    from the CLI payload while cost and billed tokens fall to 0.0 -- so the
    run looks successful and reports a confident null. Found by running the
    symbolgraph benchmark on a machine using ~/.claude-g.
    """
    from bench.runner import find_transcript

    session = "abc12345-0000-0000-0000-000000000000"
    config_dir = tmp_path / ".claude-alt"
    project = config_dir / "projects" / "-some-project"
    project.mkdir(parents=True)
    transcript = project / f"{session}.jsonl"
    transcript.write_text("{}\n")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "nowhere"))

    assert find_transcript(session) == transcript


def test_find_transcript_falls_back_to_home_without_env(tmp_path, monkeypatch):
    from bench.runner import find_transcript

    session = "def67890-0000-0000-0000-000000000000"
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / "-some-project"
    project.mkdir(parents=True)
    transcript = project / f"{session}.jsonl"
    transcript.write_text("{}\n")

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))

    assert find_transcript(session) == transcript


def test_warmup_covers_every_arm(monkeypatch, tmp_path):
    """Each arm needs its own warm-up when arms differ in the system prefix.

    An MCP server's tool schemas change the cached prefix, so an arm that was
    never warmed pays a cold cache write on its first real run while the
    warmed arm does not -- a systematic penalty charged to whichever arm was
    not arms[0].
    """
    from bench import runner

    calls = []

    def fake_run_once(task, arm, replicate, model=None):
        calls.append((task.task_id, arm, replicate))
        return runner.RunResult(task_id=task.task_id, arm=arm, replicate=replicate)

    monkeypatch.setattr(runner, "run_once", fake_run_once)

    task = runner.Task(task_id="t", prompt="p", repo=tmp_path)
    runner.run_matrix([task], replicates=1, arms=["a", "b"], warmup=True)

    warmups = {arm for _, arm, rep in calls if rep == -1}
    assert warmups == {"a", "b"}, f"only warmed {warmups}"
