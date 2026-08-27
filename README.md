# ContextMesh

**Session memory and codebase context for Claude Code.** Your next session
starts knowing what the last one learned — including what didn't work.

Runs entirely on local hooks. No proxy, no MCP server, no API key handling.
Works with Claude Pro/Max OAuth, API keys, Bedrock, and Vertex.

---

## What it actually does

| | |
|---|---|
| **Session memory** | Harvests each finished session into a knowledge graph — goals, files changed, errors hit, decisions, dead ends — and recalls the relevant parts into your next session. |
| **AST RepoMap** | Parses your codebase with Tree-sitter and injects a structural map before your first turn, so Claude knows the architecture without reading files to find it. |
| **Output capture** | Large command output is written to disk and summarised in context, with a pointer to read the rest. Nothing is discarded. |

---

## Measured results

Claims here come from `bench/`, which A/Bs real Claude Code sessions with the
hooks live versus inert and bills every token at its true rate — cache reads at
0.1×, cache writes at 1.25× or 2.0× depending on TTL. Raw data is in
`bench/results/`.

Session memory, 3 tasks × 3 replicates, delivery-verified, all runs passing
their task check:

| | turns | 95% CI | cost |
|---|---|---|---|
| Tasks memory applies to | **−17.9%** | [−1.62, −0.04] | no significant difference |
| Task memory can't help (control) | 0.00 | [0.00, 0.00] | +$0.027 (n.s.) |

**What that means:** memory gets Claude to the answer in roughly one fewer
turn, at the same price. It is not a cost saving — the tokens it injects
roughly cancel the turns it saves. When nothing relevant is stored it is a
small pure cost, which is why gating recall on relevance is the next
priority.

**What it does not mean:** prompt caching alone already cuts cost ~85% on a
typical session, measured across 241 local sessions. Every number above is a
delta on top of caching, not instead of it. Treat any tool claiming "90% fewer
tokens" against an uncached baseline with suspicion — including earlier
versions of this README, which did exactly that.

The RepoMap has **not** been measured yet. It injects roughly 2,500 tokens per
session and its benefit is assumed, not demonstrated. That is the next thing
the harness will test.

---

## Install

```bash
pipx install claude-contextmesh
contextmesh init          # registers hooks in ~/.claude/settings.json
```

Then index a project once so the RepoMap and memory have something to work with:

```bash
cd your-project
contextmesh index .
```

Use `claude` normally. Memory accumulates as you work.

Optional — a file watcher that keeps the RepoMap current, plus a dashboard:

```bash
contextmesh start &
contextmesh dashboard
```

---

## Commands

```bash
contextmesh init            # one-time global setup
contextmesh index .         # index the current project
contextmesh status          # check hooks are active
contextmesh stats           # measured token savings on tool output
contextmesh recall          # show what memory would be injected here
contextmesh harvest <file>  # backfill memory from an old transcript
contextmesh uninstall       # remove all integrations
```

---

## How it works

Three hooks, one binary:

```
UserPromptSubmit  ── first prompt only ──► inject RepoMap + recalled memory
PreToolUse        ── Bash commands ──────► capture output, summarise if large
SessionEnd        ── session finished ───► harvest transcript into the graph
```

Memory is injected **once per session**, not per prompt. Injecting every turn
appends a fresh block each time, and the compounding cost exceeds anything
recall saves.

Harvesting is deterministic and reads only the local transcript — no model
call, so it costs nothing and cannot fail on an expired login. That limits it
to what is observable: goals, file modifications, errors, commits, test runs.
Decisions and hypotheses need a model pass and are deliberately not guessed at.

Errors on files that were never subsequently fixed become `UNRESOLVED_ISSUE`
nodes. That is the dead-end signal, and it is the thing a compression tool
structurally cannot offer: your next session is told what already failed.

---

## Configuration

`~/.contextmesh/config.toml`:

```toml
[tracker]
input_price_per_mtok = 5.0
```

Environment variables:

| Variable | Effect |
|---|---|
| `CONTEXTMESH_DISABLE=1` | Makes every hook inert without uninstalling |
| `CONTEXTMESH_TIMEOUT` | Command timeout in seconds (default 1800; `0` disables) |
| `CONTEXTMESH_DATA_DIR` | Override the database location |

---

## Benchmarking it yourself

```bash
python -m bench.run_memory_bench --replicates 3
```

The harness verifies its own treatment was delivered and prints `INVALID` if
not — it caught two runs that reported a clean "no significant difference" for
a feature that had never actually run. It also reports "no significant
difference" whenever the confidence interval spans zero, so a single run can
never produce a headline.

---

## Limitations

- Significant result rests on n=6 paired runs. Real, but barely clear of zero.
- Benchmark tasks are authored in-house; the control task is the only guard
  against that bias.
- Relevance gating is implemented but disabled — no cheap signal separates
  relevant from irrelevant memory, and embeddings cost 28s to import in a hook.
- Extraction misses decisions and reasoning, by design.

---

## License

MIT
