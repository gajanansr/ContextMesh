"""Benchmark arms, including third-party context tools.

The harness was built to A/B ContextMesh against itself. Its value is larger
than that: no tool in this category publishes a reproducible, cache-aware
measurement, so the same rig can measure Headroom, RTK, Aider, or Claude
Code's native behaviour on equal terms.

An arm is everything that distinguishes one condition from another:

- `env`            environment overlay (how ContextMesh's own arms work)
- `command_prefix` wrapper argv, e.g. ("headroom", "wrap") or ("rtk",)
- `settings`       a `claude --settings` file, for hook-based tools
- `setup`/`teardown` shell run once around the arm, for tools needing a proxy

## On honesty about third-party arms

Delivery verification is the check that separates "this feature does not help"
from "this feature never ran"; it has already rescued three runs here. For
ContextMesh we verify delivery by finding an injected block in the session
transcript. For a third-party tool that compresses in a proxy, there may be no
transcript-visible evidence at all.

So each arm declares `delivery_marker` when its effect is observable and
`None` when it is not. An arm with no marker is not silently trusted: the
report labels it UNVERIFIED, and any conclusion drawn from it is weaker than
one drawn from a verified arm. Publishing a comparison that quietly assumes a
competitor's tool was active would be the same error this harness exists to
prevent, pointed at someone else.

Nothing here installs anything. Unavailable arms are skipped and named.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Arm:
    """One condition under test."""

    name: str
    env: dict[str, str] = field(default_factory=dict)
    command_prefix: tuple[str, ...] = ()
    settings: Path | None = None
    # Extra argv appended to the `claude` invocation, e.g. --mcp-config for a
    # tool the agent calls rather than one that injects behind its back.
    extra_args: tuple[str, ...] = ()
    setup: str | None = None
    teardown: str | None = None
    # Executables that must be on PATH for this arm to run at all.
    requires: tuple[str, ...] = ()
    # A string whose presence in the session transcript proves the arm's
    # treatment actually reached the model. None means unverifiable -- see the
    # module docstring.
    delivery_marker: str | None = None
    # Whether the marker is *expected* to be present. Control arms expect
    # absence, which is how treatment leaking into a baseline gets caught.
    expects_marker: bool = True
    notes: str = ""

    @property
    def verifiable(self) -> bool:
        return self.delivery_marker is not None

    def missing_requirements(self) -> list[str]:
        return [exe for exe in self.requires if shutil.which(exe) is None]

    def available(self) -> tuple[bool, str]:
        missing = self.missing_requirements()
        if missing:
            return False, f"{self.name}: not installed ({', '.join(missing)})"
        return True, ""


# ── ContextMesh's own arms ──────────────────────────────────────────────────
#
# The repomap arms hold memory off in both directions: varying two injected
# blocks at once cannot say which one paid for itself.

CONTEXTMESH_ARMS: dict[str, Arm] = {
    "off": Arm(
        name="off",
        env={"CONTEXTMESH_DISABLE": "1"},
        delivery_marker="ContextMesh",
        expects_marker=False,
        notes="ContextMesh fully inert. The baseline for every comparison.",
    ),
    "on": Arm(
        name="on",
        delivery_marker="ContextMesh memory",
        notes="Memory recall on, RepoMap at its default (off).",
    ),
    "repomap": Arm(
        name="repomap",
        env={"CONTEXTMESH_NO_MEMORY": "1", "CONTEXTMESH_REPOMAP": "1"},
        delivery_marker="ContextMesh RepoMap",
        notes="RepoMap only, memory suppressed.",
    ),
    "norepomap": Arm(
        name="norepomap",
        env={"CONTEXTMESH_NO_MEMORY": "1", "CONTEXTMESH_NO_REPOMAP": "1"},
        delivery_marker="ContextMesh RepoMap",
        expects_marker=False,
        notes="Neither block injected. Baseline for the RepoMap comparison.",
    ),
}


# ── Third-party arms ────────────────────────────────────────────────────────
#
# Command shapes come from each project's own documentation. They are declared
# here so the harness is ready to run them; none are installed automatically,
# and each is skipped with a reason when its executable is absent.

# Headroom is driven through `headroom proxy` + ANTHROPIC_BASE_URL rather than
# `headroom wrap claude`. `wrap` rewrites ~/.claude.json at user scope and
# installs Serena; the proxy form changes nothing outside the benchmark's own
# environment and is verified to work with OAuth (non-API-key) auth.
#
# The pairing matters more than either arm alone. `--no-optimize` runs the same
# proxy in passthrough, so comparing optimize-vs-passthrough cancels the
# proxy's own latency and token overhead and isolates the compression itself.
# Comparing "through a proxy" against "no proxy at all" would confound the two.
HEADROOM_PORT = 8787
HEADROOM_PASSTHROUGH_PORT = 8788

THIRD_PARTY_ARMS: dict[str, Arm] = {
    "headroom": Arm(
        name="headroom",
        requires=("headroom",),
        env={
            "CONTEXTMESH_DISABLE": "1",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{HEADROOM_PORT}",
        },
        # Compression happens inside the proxy. Nothing about that is
        # necessarily visible in the transcript, so delivery cannot be
        # confirmed the way ContextMesh's injection can.
        delivery_marker=None,
        notes=(
            "Headroom proxy with optimization enabled. ContextMesh disabled so "
            "the two cannot overlap. Delivery is unverifiable from the "
            "transcript -- weaker evidence than a verified arm. Pair with "
            "headroom-passthrough, not with a no-proxy baseline."
        ),
    ),
    "headroom-passthrough": Arm(
        name="headroom-passthrough",
        requires=("headroom",),
        env={
            "CONTEXTMESH_DISABLE": "1",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{HEADROOM_PASSTHROUGH_PORT}",
        },
        delivery_marker=None,
        expects_marker=False,
        notes=(
            "Same proxy, --no-optimize. The control for the headroom arm: "
            "identical network path, no compression, so the difference is "
            "the optimization rather than the proxy."
        ),
    ),
    "rtk": Arm(
        name="rtk",
        requires=("rtk",),
        env={"CONTEXTMESH_DISABLE": "1"},
        # RTK installs a PreToolUse hook that rewrites Bash calls, the same
        # mechanism ContextMesh uses. The driver points --settings at a
        # generated file so nothing is installed globally.
        delivery_marker=None,
        notes=(
            "RTK compresses command output via per-command parsers. Comparable "
            "to ContextMesh's output capture, not to its memory. Needs a "
            "settings file wiring its hook; see the cross-tool driver."
        ),
    ),
}


# ── symbolgraph ─────────────────────────────────────────────────────────────
#
# symbolgraph is a different shape from every arm above. Headroom and RTK act
# on the agent behind its back; ContextMesh injects on a hook. symbolgraph is
# an MCP server the agent chooses to call, so its cost is opt-in and its
# delivery is *visible* -- a tool call leaves `mcp__symbolgraph__` in the
# transcript. That makes it the only third-party arm here that can be verified
# the same way ContextMesh's own arms are.
#
# Both arms pass --strict-mcp-config so whatever MCP servers the operator has
# configured globally cannot leak into either side of the comparison.

def symbolgraph_arms(mcp_config: Path, sg_bin: Path | None = None) -> dict[str, Arm]:
    """Build the pair, given a written --mcp-config file for the `on` side.

    `sg_bin` enables the arm's own install step. symbolgraph's documented
    setup is `sg init`, which writes an AGENTS.md telling the agent to "use
    sg search / sg context / definition / callers / callees before reading
    files". Registering the MCP server without that instruction measures a
    tool the agent never reaches for: in the first run here the server was
    available in all 12 treatment runs and invoked in none of them, because
    grep was the obvious move and nothing suggested otherwise. Installing the
    tool the way its author documents is the only fair test of it.
    """
    install = teardown = None
    if sg_bin is not None:
        # `sg init` writes AGENTS.md, .mcp.json and per-editor configs. On
        # Claude Code the MCP entry registers the tools but the instruction
        # never lands: Claude Code reads CLAUDE.md, and sg init does not write
        # one. Registered-but-unmentioned, the agent greps instead -- 0
        # invocations in 9 runs. Copying sg's own AGENTS.md text to CLAUDE.md
        # closes that gap without inventing wording of ours, so the tool is
        # tested as its author intends rather than as its installer leaves it.
        install = (
            f"{sg_bin} init --agent all >/dev/null 2>&1 || true; "
            "[ -f AGENTS.md ] && cp AGENTS.md CLAUDE.md || true"
        )
        # Leave the tree as the baseline sees it, so a stray AGENTS.md cannot
        # follow the fixture into an sg-off run.
        teardown = (
            "rm -rf AGENTS.md CLAUDE.md .mcp.json .cursor .vscode "
            "opencode.json .gemini .github/copilot-instructions.md"
        )
    return {
        "sg-off": Arm(
            name="sg-off",
            env={"CONTEXTMESH_DISABLE": "1"},
            extra_args=("--strict-mcp-config",),
            delivery_marker="mcp__symbolgraph",
            expects_marker=False,
            # Strip any install artefacts before the baseline runs.
            setup=(
                "rm -rf AGENTS.md CLAUDE.md .mcp.json .cursor .vscode "
                "opencode.json .gemini .github/copilot-instructions.md"
            ),
            notes=(
                "Plain Claude Code: Read, Grep and Glob only, and no AGENTS.md. "
                "The baseline symbolgraph's savings claim is implicitly "
                "measured against."
            ),
        ),
        "sg-on": Arm(
            name="sg-on",
            env={"CONTEXTMESH_DISABLE": "1"},
            extra_args=("--strict-mcp-config", "--mcp-config", str(mcp_config)),
            delivery_marker="mcp__symbolgraph",
            setup=install,
            teardown=teardown,
            notes=(
                "Same agent with the symbolgraph MCP server available. The "
                "agent still has Read and Grep -- withholding them would test "
                "a tool nobody uses that way, and would hide the failure mode "
                "that matters: falling back to a full read after a pack that "
                "did not answer the question."
            ),
        ),
    }


ALL_ARMS: dict[str, Arm] = {**CONTEXTMESH_ARMS, **THIRD_PARTY_ARMS}


# Registration lives in bench.runner (register_arms), which owns both the
# spec table and the env-only view run_once validates against.


def resolve(name: str) -> Arm:
    try:
        return ALL_ARMS[name]
    except KeyError:
        raise ValueError(
            f"unknown arm {name!r}; known arms: {', '.join(sorted(ALL_ARMS))}"
        ) from None


def check_availability(names: list[str]) -> tuple[list[str], list[str]]:
    """Split requested arms into (runnable, skipped-with-reason)."""
    runnable, skipped = [], []
    for name in names:
        arm = resolve(name)
        ok, reason = arm.available()
        (runnable if ok else skipped).append(name if ok else reason)
    return runnable, skipped


def run_arm_hook(command: str | None, cwd: Path, timeout: int = 300) -> tuple[int, str]:
    """Run an arm's setup/teardown shell, with ContextMesh inert."""
    if not command:
        return 0, ""
    import os

    env = dict(os.environ, CONTEXTMESH_DISABLE="1")
    try:
        proc = subprocess.run(
            command, shell=True, cwd=cwd, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout + proc.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return 124, f"arm setup timed out after {timeout}s"
