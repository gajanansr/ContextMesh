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
| **AST RepoMap** | Parses your codebase with Tree-sitter into a structural map. **Off by default** — it was measured and it lost. See below. |
| **Output capture** | Large command output is written to disk and summarised in context, with a pointer to read the rest. Nothing is discarded. |

---

## Measured results

Claims here come from `bench/`, which A/Bs real Claude Code sessions with the
hooks live versus inert and bills every token at its true rate — cache reads at
0.1×, cache writes at 1.25× or 2.0× depending on TTL. Raw data is in
`bench/results/`.

Session memory, re-measured after broadening the corpus from two memory
mechanisms to three (a DECISION, an UNRESOLVED_ISSUE, and a SOLUTION —
distinct node types, not three variations on the same recall), delivery
verified 0/12 baseline / 12/12 treatment, 23 of 24 runs passing:

| | turns | 95% CI | cost |
|---|---|---|---|
| Two original mechanisms (n=6) | **−28.1%** | [−2.79, −0.21] | no significant difference |
| Task memory can't help (control) | 0.00 | [0.00, 0.00] | +$0.021 (n.s.) |

**What that means:** memory gets Claude to the answer in fewer turns, at the
same price. It is not a cost saving — the tokens it injects roughly cancel the
turns it saves. When nothing relevant is stored it is a small pure cost across
two separate runs of the control task, which is why gating recall on
relevance is the next priority.

**A third mechanism, reported separately because it isn't as clean:** a task
needing a recalled SOLUTION (retries must back off exponentially, not retry
immediately) showed a much larger effect — but one `off`-arm replicate failed
its verification outright and another spiked to 16 turns and $0.27, against a
flat 4 turns and ~$0.05 every time with memory. Pooling all three mechanisms
gives turns −45.6% (CI [−6.50, −0.004]) — technically significant, but that
number is carried by one extreme outlier and I'm not publishing it as a
headline. What I will stand behind: without memory, this task was
*inconsistent* — sometimes it failed, sometimes it took much longer — and with
memory it wasn't, once. That's a claim about reliability, not mean turns, and
it's a mode of failure a compression-only tool has no mechanism to prevent.

**What it does not mean:** prompt caching alone already cuts cost ~85% on a
typical session, measured across 241 local sessions. Every number above is a
delta on top of caching, not instead of it. Treat any tool claiming "90% fewer
tokens" against an uncached baseline with suspicion — including earlier
versions of this README, which did exactly that.

### The RepoMap was measured, and it lost

Same method, 3 tasks × 3 replicates, delivery verified 9/9 treatment and 0/9
control, all 18 runs passing their check:

| | turns | cost |
|---|---|---|
| Overall | −0.33 (n.s.) | **+45.6%** [+0.044, +0.129] |
| Locate tasks only | −0.50 (n.s.) | **+40.9%** [+0.041, +0.161] |
| Control | 0.00 | +$0.058 (n.s.) |

It costs significantly more and does not significantly reduce turns, even on
tasks written specifically to reward knowing where code lives. **It is
therefore off by default**; set `CONTEXTMESH_REPOMAP=1` to opt in.

The suspected cause was ranking: the map was ordered alphabetically by file
path and truncated at 10k characters, spending its budget on whatever sorted
first instead of what the task needed. That was fixed — symbols are now
selected by PageRank over cross-file symbol references, the same idea Aider
uses — and the benchmark was re-run (`bench/results/repomap-ranked-2026-08-28.json`,
same corpus, delivery confirmed 9/9 / 0/9):

| | turns | cost |
|---|---|---|
| Overall (ranked) | −0.33 (n.s.) | **+74.6%** [+0.022, +0.036] |

Worse. Ranking fixed *what* the map contains, correctly, but not the deeper
problem: it still costs a fixed ~2,500 tokens whether its contents are
well-chosen or not, and that tax has to be earned back in saved turns on every
session — including tasks that don't need broad architectural context at all.
Better sorting inside a fixed budget doesn't fix a tax that shouldn't always
be paid. The likely next step is making the cost variable — skip injection on
small codebases, size the budget to the task, or gate it on relevance the way
memory should be — not further reordering. Both results stand; the map stays
off by default either way.

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
| `CONTEXTMESH_REPOMAP=1` | Opt into RepoMap injection (off by default) |
| `CONTEXTMESH_NO_MEMORY=1` | Disable memory recall |

---

## Benchmarking it yourself

```bash
python -m bench.run_memory_bench --replicates 3
python -m bench.run_repomap_bench --replicates 3
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
