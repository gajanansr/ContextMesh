# ContextMesh

**Archived.** A context layer for Claude Code — session memory and an AST repo map,
delivered through local hooks. It works, it is measured, and the measurements say it
is not worth running. This README explains what was built, what was measured, and why
it stopped.

The code is functional and installable. The benchmark harness in `bench/` is the part
most likely to be useful to someone else.

---

## Why it was archived

The tool injects context into a session — recalled memory, a codebase map. Injecting
anything into a cached LLM session converts prompt-cache **reads** (billed at 0.1×)
into cache **writes** (billed at 1.25× or 2.0×). The billed cost of an injected block
is therefore a multiple of the block itself:

| Experiment | Payload injected | Extra tokens billed | Ratio |
|---|---|---|---|
| `bench/results/position-2026-08-28.json` | 440 | +3,965 | 9.0× |
| `bench/results/repomap-2026-08-28.json` | ~2,650 | +16,870 | 6.4× |
| `bench/results/crosstool-2026-08-28.json` | ~470 | +10,916 | 23.2× |

So an injected block must be worth roughly an order of magnitude more than its own
size. Session memory clears that bar only when what it recalls is genuinely relevant.
The repo map never cleared it.

This is not a new discovery. [TokenPilot](https://arxiv.org/pdf/2606.17016) states it
as its opening premise — "unconstrained sequence mutations alter layouts, introducing
prefix mismatches and cache invalidation" — and reports prefix stabilisation moving
cost from \$8.31 to \$4.35 and cache hit rate from 38.7% to 79.2%.
[ProjectDiscovery](https://projectdiscovery.io/blog/how-we-cut-llm-cost-with-prompt-caching)
documented the same effect, 7% → 84% hit rate. It was rediscovered here
independently, which validates the harness and settles the product question.

---

## What was measured

Everything below came from `bench/`, which A/Bs real Claude Code sessions and bills
every token at its true rate. Raw data is in `bench/results/`.

### Session memory — works, conditionally

| Condition | turns | cost |
|---|---|---|
| Curated, relevant memory (n=6) | **−28.1%** [−2.79, −0.21] | no significant difference |
| Memory as it naturally accumulates (n=12) | −0.58 (n.s.) | **+32.0%** [+0.010, +0.101] |
| Task memory cannot help (control) | 0.00 [0.00, 0.00] | +$0.021 (n.s.) |

Memory reduces turns when it recalls something relevant, at no extra cost. Left to
accumulate — which is how memory actually behaves — it costs 32% more and buys
nothing. Relevance gating would decide whether the feature is net-positive; it is
implemented but disabled, because no cheap signal separated relevant from irrelevant
memory (file overlap and word overlap both scored a related and an unrelated prompt
identically at 0.300).

One result worth keeping: on a task needing a recalled prior decision, the baseline
failed 1 of 3 replicates and spiked to 16 turns on another, while every run with
memory took 4 turns and passed. That is a claim about reliability, not mean cost.

### AST RepoMap — does not work

Measured three times, with a different ordering each time:

| Ordering | turns | cost |
|---|---|---|
| Alphabetical by file path | −0.33 (n.s.) | **+45.6%** |
| Static global PageRank | −0.33 (n.s.) | **+74.6%** |
| Query-personalised PageRank (Aider's approach) | −0.86 (n.s.) | **+35.9%** |

Ranking demonstrably improved *what* the map contained and never made it worth
sending. A fixed ~2,650-token block has to be earned back every session, including on
tasks needing no architectural context. Off by default; `CONTEXTMESH_REPOMAP=1` opts in.

### Prompt caching dwarfs all of it

Across 241 local sessions, prompt caching alone accounts for an **85.6%** cost
reduction before any tool acts. Every number above is a delta on top of that. Treat
any tool claiming "90% fewer tokens" against an uncached baseline with suspicion —
including this project's own earlier README, which did exactly that on the strength of
a constant that supplied 99.5% of its own headline.

---

## The benchmark harness

`bench/` is the reusable part.

- **Cache-aware billing.** Reads Claude Code's session transcripts and bills each token
  class at its real rate. Validated to six decimal places against the CLI's own cost
  accounting.
- **Delivery verification.** Confirms a tool's treatment actually reached the model
  before comparing anything, and prints `INVALID` when it did not. This caught three
  runs that reported a clean "no significant difference" for a feature that had never
  run at all.
- **Paired statistics.** Replicates paired by task, 95% intervals, and an explicit
  "no significant difference" verdict. A single run can never produce a headline.
- **Confound controls.** A discarded warm-up per task and rotated arm order, because
  cold-versus-warm cache ordering was measured at 8× on a trivial task — larger than
  any effect being looked for.

```bash
python -m bench.run_memory_bench --replicates 3
python -m bench.run_repomap_bench --replicates 3
python -m bench.run_position_bench --replicates 3
python -m bench.run_crosstool_bench --replicates 4   # ContextMesh vs Headroom
```

Nine results were rejected by these checks before they could become claims, including
an 88% "win" that was entirely cache ordering, and two comparisons whose control arm
was silently receiving the treatment.

---

## Install

Still works, if you want it. Memory is on, the repo map is off.

```bash
pipx install claude-contextmesh
contextmesh init
cd your-project && contextmesh index .
```

| Variable | Effect |
|---|---|
| `CONTEXTMESH_DISABLE=1` | Makes every hook inert |
| `CONTEXTMESH_REPOMAP=1` | Opt into RepoMap injection (off by default) |
| `CONTEXTMESH_NO_MEMORY=1` | Disable memory recall |
| `CONTEXTMESH_TIMEOUT` | Command timeout, seconds (default 1800; `0` disables) |
| `CONTEXTMESH_DATA_DIR` | Override the database location |

`contextmesh uninstall` removes every hook it finds. If you installed **0.10.0**,
that version's uninstall swept only `PreToolUse`/`PostToolUse` and reported success
while leaving `UserPromptSubmit` and `SessionEnd` behind — memory injection kept
running after you believed it was gone. Fixed in 0.10.1; upgrade before uninstalling,
or remove any entry whose command contains `contextmesh` from
`~/.claude/settings.json` by hand.

---

## What would be worth trying next

Not planned, but these are the open threads, in order of promise:

1. **Prefix stabilisation.** The injected block changes every session as memory
   accumulates, so it can never inherit a warm cache. TokenPilot's canonicalisation
   approach — a byte-identical prefix across sessions — was never tested here and is
   the most likely path to making injection affordable.
2. **Relevance gating with real embeddings.** The blocker was believed to be a 28s
   `sentence-transformers` import. Headroom does the same job at <50ms with local
   embeddings, so that was an implementation problem, not a constraint.
3. **Delivery verification as a standalone idea.** No other tool in this category
   appears to check that its own treatment was applied.

---

## Limitations of everything above

- Small samples. The clean memory result is n=6; the cross-tool run is n=12. Real, but
  not large.
- Benchmark tasks were authored in-house. The control tasks are the only guard against
  that, and they did their job — 0.00 turn change, twice.
- One machine, one model, one client. Nothing here is established as vendor-general.
- The Headroom comparison is **void**: its `--no-optimize` control mode still compacts
  tool schemas, so both arms compressed. Reported upstream. Nothing in this repo
  should be read as a measurement of Headroom.

---

## License

MIT
