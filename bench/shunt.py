"""A read-shunt for Claude Code, and the accounting Portal's benchmark omits.

Spotify's Portal ships a `shunt` plugin that blocks Claude from reading large
files and delegates the read to a second model (AiKA), so the file contents
never enter Claude's context. Their post reports "around a whopping 90%"
savings; their own eval file says what that measures: "Claude context tokens
with vs without shunt", counted as `chars / 4`. The delegate's tokens are
never counted anywhere.

That is the flaw worth fixing rather than repeating. The files still get read,
in full, by a model -- the tokens moved, they did not vanish. Whether anyone
saved anything depends entirely on what the delegate costs, which is the one
quantity the published number leaves out.

So this rebuilds the architecture with two changes:

- the delegate is Haiku, reachable by anyone with Claude Code, rather than a
  Portal instance with AiKA enabled;
- every delegate call appends its own billed cost to a log, so the metric is
  *total* spend across both models, not one side of it.

Why the design deserves a fair test despite that criticism: it is the first
one we have measured whose mechanism points the right way. ContextMesh's
RepoMap was additive -- it pushed tokens into a session on top of everything
else, and amplification billed them at 6.4x. symbolgraph was substitutive but
against a cheap alternative -- its pack replaced a grep and was bigger than
the grep. A shunt is subtractive against an expensive one: a 6,447-line file
never enters the prefix at all, and the thing being removed is exactly the
thing amplification would have billed at 6-23x for the rest of the session.

It also fixes the adoption failure we measured on symbolgraph, where the
agent ignored an available tool in 9 runs out of 9. A hook that *blocks* Read
cannot be ignored. That is the right call, and it is also the main risk: a
blocked read the agent genuinely needed is a failed task, which is why the
harness reports verified success beside cost and refuses to read one without
the other.
"""

from __future__ import annotations

import json
from pathlib import Path

# Portal's default. Kept identical so the comparison is to their design, not
# to a threshold we tuned.
MIN_LINES = 350
DELEGATE_MODEL = "claude-haiku-4-5-20251001"


def _bulk_read_script(cost_log: Path) -> str:
    """The delegate. Mirrors Portal's `bulk-read`: files in, answer out.

    The child is invoked with no --settings, so the blocking hook does not
    apply to it -- otherwise the delegate would block its own reads and
    recurse. It reads via stdin rather than the Read tool anyway.
    """
    return f'''#!/bin/bash
# Delegate a bulk file read to a cheaper model. Files go to the delegate and
# never into the caller's context; only the answer comes back.
set -uo pipefail

question=""
paths=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --question) question="$2"; shift 2 ;;
    --paths)    shift; while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do paths+=("$1"); shift; done ;;
    *)          shift ;;
  esac
done

if [ -z "$question" ] || [ ${{#paths[@]}} -eq 0 ]; then
  echo "usage: bulk-read --question Q --paths f1 [f2 ...]" >&2
  exit 1
fi

# A typo'd path would otherwise be sent as an empty block and produce a
# confident answer about nothing.
for p in "${{paths[@]}}"; do
  if [ ! -r "$p" ]; then echo "Error: unreadable: $p" >&2; exit 1; fi
done

msg=$(mktemp)
trap 'rm -f "$msg"' EXIT
for p in "${{paths[@]}}"; do
  {{ printf '<file path="%s">\\n' "$p"; cat "$p"; printf '</file>\\n\\n'; }} >> "$msg"
done
printf 'Answer concisely, in bullets, using only the files above.\\nQuestion: %s\\n' \\
  "$question" >> "$msg"

out=$(CONTEXTMESH_DISABLE=1 claude -p "$(cat "$msg")" \\
        --model {DELEGATE_MODEL} \\
        --output-format json \\
        --permission-mode bypassPermissions 2>/dev/null)

# Bill the delegate. Without this line the measurement reproduces exactly the
# error it was built to expose.
printf '%s\\n' "$out" | python3 -I -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("total_cost_usd") or 0.0)
except Exception:
    print(0.0)
' >> "{cost_log}"

printf '%s\\n' "$out" | python3 -I -c '
import json, sys
try:
    print(json.load(sys.stdin).get("result") or "")
except Exception:
    print("")
'
'''


def _hook_script(script_path: Path, block_log: Path) -> str:
    """PreToolUse gate: block a large Read, name the alternative.

    A block whose reason does not say what to do instead just costs a turn.
    The offset/limit escape hatch is Portal's and is kept: an agent that needs
    exact text to edit must still be able to get it.
    """
    return f'''#!/bin/bash
MIN_LINES="${{SHUNT_MIN_LINES:-{MIN_LINES}}}"
payload=$(cat)

file_path=$(printf '%s' "$payload" | python3 -I -c '
import json, sys
try:
    d = json.load(sys.stdin)
    inp = d.get("tool_input") or {{}}
    # An offset/limit read is already scoped; let it through.
    if inp.get("offset") is not None or inp.get("limit") is not None:
        print("")
    else:
        print(inp.get("file_path") or "")
except Exception:
    print("")
')

[ -z "$file_path" ] && exit 0
[ -f "$file_path" ] || exit 0

lines=$(wc -l < "$file_path" 2>/dev/null | tr -d " " || echo 0)
[ "$lines" -le "$MIN_LINES" ] && exit 0

# Record the attempt. Whether the agent even tries a large read is the
# premise under test: a shunt can only save on reads that would have
# happened, and if the agent greps instead, the baseline it is measured
# against never existed.
printf '%s %s\n' "$lines" "$file_path" >> "{block_log}"

reason="File is ${{lines}} lines (threshold: ${{MIN_LINES}}). Do not read it directly. Run: {script_path} --question \\"<your question>\\" --paths ${{file_path}}  — this delegates the read to a cheaper model and returns an answer. If you need exact text to edit, re-read with offset/limit for just that section."

python3 -I -c '
import json, sys
print(json.dumps({{"decision": "block", "reason": sys.argv[1]}}))
' "$reason"
'''


def install(workdir: Path) -> tuple[Path, Path, Path]:
    """Write hook, delegate and settings. Returns (settings, cost_log, block_log)."""
    workdir.mkdir(parents=True, exist_ok=True)
    cost_log = workdir / "shunt-delegate-cost.log"
    cost_log.write_text("")
    block_log = workdir / "shunt-blocks.log"
    block_log.write_text("")

    script = workdir / "bulk-read"
    script.write_text(_bulk_read_script(cost_log))
    script.chmod(0o755)

    hook = workdir / "shunt-hook.sh"
    hook.write_text(_hook_script(script, block_log))
    hook.chmod(0o755)

    settings = workdir / "shunt-settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {"matcher": "Read", "hooks": [{"type": "command", "command": str(hook)}]}
            ]
        }
    }, indent=2))
    return settings, cost_log, block_log


def delegate_cost(cost_log: Path) -> float:
    """Total billed by the delegate since the log was last truncated."""
    if not cost_log.exists():
        return 0.0
    total = 0.0
    for line in cost_log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            total += float(line)
        except ValueError:
            continue
    return total


def blocked_reads(block_log: Path) -> int:
    """How many large reads the hook intercepted since the log was truncated."""
    if not block_log.exists():
        return 0
    return len([ln for ln in block_log.read_text().splitlines() if ln.strip()])
