"""Tests for hook install/uninstall.

0.10.0 shipped an uninstall that reported success while leaving two hooks
behind, so anyone who removed ContextMesh kept paying for memory injection they
believed was gone. The round-trip property below is what would have caught it.
"""

import json

import pytest

from contextmesh import installer
from contextmesh.installer import (
    HOOK_EVENTS,
    inject_claude_hooks,
    is_contextmesh_hook,
    remove_claude_hooks,
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Point the installer at a throwaway settings.json."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(installer, "_get_claude_settings_path", lambda: path)
    return path


def contextmesh_hooks(path):
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [
        h
        for entries in data.get("hooks", {}).values()
        for entry in entries
        for h in entry.get("hooks", [])
        if is_contextmesh_hook(h)
    ]


def test_install_registers_every_event(settings):
    inject_claude_hooks()

    data = json.loads(settings.read_text())
    for event in HOOK_EVENTS:
        commands = [h["command"] for e in data["hooks"][event] for h in e["hooks"]]
        assert "contextmesh-hook" in commands


def test_install_then_uninstall_leaves_nothing(settings):
    """The regression that shipped in 0.10.0.

    Uninstall swept only PreToolUse/PostToolUse, so UserPromptSubmit and
    SessionEnd survived while the command printed "restored to its original
    state".
    """
    inject_claude_hooks()
    assert contextmesh_hooks(settings)

    remove_claude_hooks()

    assert contextmesh_hooks(settings) == []


def test_uninstall_sweeps_events_it_was_never_told_about(settings):
    """An older uninstall must still remove a newer version's hooks."""
    settings.write_text(json.dumps({
        "hooks": {
            "SomeFutureEvent": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "contextmesh-hook"}]}
            ]
        }
    }))

    remove_claude_hooks()

    assert contextmesh_hooks(settings) == []


def test_uninstall_preserves_other_tools_in_the_same_entry(settings):
    """Filtering used to drop the whole entry, taking co-located hooks with it."""
    settings.write_text(json.dumps({
        "hooks": {
            "SessionEnd": [{
                "matcher": "*",
                "hooks": [
                    {"type": "command", "command": "contextmesh-hook"},
                    {"type": "command", "command": "some-other-tool --flag"},
                ],
            }]
        }
    }))

    remove_claude_hooks()

    data = json.loads(settings.read_text())
    surviving = [h["command"] for e in data["hooks"]["SessionEnd"] for h in e["hooks"]]
    assert surviving == ["some-other-tool --flag"]


def test_uninstall_preserves_unrelated_events(settings):
    settings.write_text(json.dumps({
        "hooks": {
            "Stop": [{"matcher": "*", "hooks": [{"type": "command", "command": "unrelated"}]}]
        }
    }))

    remove_claude_hooks()

    data = json.loads(settings.read_text())
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "unrelated"


def test_uninstall_is_idempotent(settings):
    inject_claude_hooks()
    remove_claude_hooks()
    remove_claude_hooks()  # must not raise on an already-clean file

    assert contextmesh_hooks(settings) == []


def test_install_is_idempotent(settings):
    inject_claude_hooks()
    inject_claude_hooks()

    assert len(contextmesh_hooks(settings)) == len(HOOK_EVENTS)


def test_malformed_settings_are_left_untouched(settings):
    """Never rewrite a config we could not parse."""
    settings.write_text("{ this is not json")

    remove_claude_hooks()

    assert settings.read_text() == "{ this is not json"


def test_missing_settings_file_is_not_an_error(settings):
    remove_claude_hooks()  # must not raise
    assert not settings.exists()


def test_backup_written_before_rewriting(settings):
    inject_claude_hooks()
    original = settings.read_text()

    remove_claude_hooks()

    backup = settings.with_suffix(settings.suffix + ".contextmesh-bak")
    assert backup.exists()
    assert backup.read_text() == original
