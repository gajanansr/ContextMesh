# truecost — a cache-aware audit of token-savings claims

**Status:** design approved, not yet implemented
**Date:** 2026-09-09

## Problem

The fastest-growing category of AI devtool ships an unverifiable headline number.
`rtk-ai/rtk` (79.2k stars) claims 60-90% token reduction. jCodeMunch claims 95%.
Spotify's Portal read-shunt claims "around a whopping 90%". None of them publish a
reproducible, cache-aware measurement, and several measure something that is not
the user's bill.

Three errors recur, all of them measured in this repository:

1. **Cache amplification ignored.** Anything injected into a cached session converts
   cache *reads* (0.1x) into cache *writes* (1.25x or 2.0x). Measured amplification
   here ranged 6.4x to 23.2x the payload's own size. A tool can cut token count and
   raise the bill; an independent report found a 38.4% output reduction producing a
   6.8% cost *increase*.
2. **One-sided billing.** A tool that delegates work to a second model reports only
   the parent's tokens. Rebuilding Portal's architecture and billing both sides moved
   its result from -25.7% (their accounting) to -10.9% (total spend). The single
   replicate that actually invoked the delegate saved nothing at all.
3. **Unverified delivery.** No tool in this category checks that its own treatment
   reached the model. Delivery verification has rescued three runs in this repo and
   invalidated nine results before they became claims, including an 88% "win" that
   was entirely cache ordering.

## The asset

`bench/` already implements the measurement. ~1,263 lines transfer unchanged:

| Module | Role |
|---|---|
| `costs.py` | Cache-aware billing, four token classes at true rates. Validated to six decimals against Claude Code's own accounting. |
| `transcript.py` | Session transcript parsing, per-turn usage. |
| `runner.py` | `Arm` registration, `Task`, `Matrix`, replicate execution, arm-order rotation. |
| `report.py` | Paired stats, 95% intervals, explicit "no significant difference", delivery reporting. |
| `arms.py` | Third-party arm abstraction: `command_prefix`, `settings`, `setup`/`teardown`, `requires`, `delivery_marker`, `expects_marker`. |

`run_crosstool_bench.py` already establishes the pairing discipline: each tool is
compared against the control that isolates *its* mechanism, not a shared baseline.
Headroom pairs against a passthrough proxy so proxy overhead cancels; RTK pairs
against `off` because without it no hook exists at all.

## Scope — v1

Four subjects, in this order:

1. **ContextMesh** (self-audit, published first). RepoMap at +45.6% / +74.6% /
   +35.9% cost across three orderings; a removed metric that supplied 99.5% of its
   own headline. Leading with this is the credibility anchor.
2. **RTK** — arm already exists (`94c4dfe`). Largest subject, 79.2k stars.
3. **Portal read-shunt** — already measured; needs only re-running under the new CLI.
4. **Headroom** — prior run is void (`--no-optimize` did not gate tool-schema
   compaction, `anthropic.py:2782`, reported upstream). Requires a valid control
   before any number is published.

## Architecture

New repository. ContextMesh stays archived and becomes one measured subject.

```
truecost/
  truecost/
    core/          # costs, transcript, runner, report, arms — moved as-is
    manifest.py    # TOML -> Arm + claim metadata
    corpus/
      claim/       # per-tool reproduction of its own published benchmark
      neutral/     # shared versioned task set on pinned OSS repos
    verdict.py     # Comparison -> one published row
    cli.py
  subjects/        # one TOML per audited tool
  results/         # raw JSON, one file per run, committed
  README.md        # the leaderboard
```

### Tool manifest

Adding a subject is a config file, not a code change. The manifest wraps the existing
`Arm` fields and adds the published claim and its source URL, so the verdict can state
claimed-versus-measured without a human transcribing either.

### Two-axis corpus

- **`claim/`** reproduces the tool's own published benchmark verbatim, on its own
  fixture and question. This is the number a maintainer cannot call unrepresentative.
  Precedent: `corpus_shunt.py` ran Portal's benchmark question #1 verbatim against a
  file 10x their fixture.
- **`neutral/`** is a versioned task set on pinned real OSS repos, identical across
  tools, enabling the leaderboard. The `claim/` number is what defends it.

Every task carries a control on which the tool's mechanism cannot fire. Controls have
twice returned 0.00 turn change, which is the only evidence that in-house tasks are
not rigged.

### Both-sides billing

First-class, not a special case. Any subject that moves work to a second model is
billed across both by default, and the verdict prints the tool's own accounting and
the total side by side. The gap between those two lines is the finding.

## Methodology rules — non-negotiable

Carried over from what already caught nine bad results:

- Every published number ships its raw JSON in `results/`.
- Delivery is verified or the row reads `UNVERIFIED`. Never silently trusted.
- Control arms assert marker *absence*, which is how treatment leaking into a
  baseline gets caught.
- A discarded warm-up per task; arm order rotated. Cold-vs-warm cache ordering was
  measured at 8x on a trivial task — larger than any effect being sought.
- Replicates paired by task; 95% intervals; an explicit "no significant difference"
  verdict. A single run can never produce a headline.
- Each tool is paired against the control that isolates its own mechanism.

## Verdict format

One row per tool:

```
RTK          claimed -60..-90%   measured -X.X% [CI lo, hi]   n=12   UNVERIFIED
Portal shunt claimed -90%        measured -10.9% (total)      n=3    VERIFIED
             their accounting    -25.7%
ContextMesh  claimed (withdrawn) measured +45.6%              n=6    VERIFIED
```

`INVALID` is a first-class outcome, printed when delivery verification fails or a
control is broken. The void Headroom run publishes as `INVALID` with its reason.

## Disclosure policy

Notify the maintainer before publishing; publish their response alongside the verdict;
give them a window to respond. Precedent: the Headroom bug was reported upstream
rather than published as a gotcha, and that run turned out to be void — the policy
would have caught it before it became a public claim. This is what makes the project
an audit rather than a dunk, and it is what makes maintainers fix things.

## Out of scope for v1

- Non-Claude-Code clients. Every measurement is one machine, one model, one client,
  and the README must say so.
- A hosted site or CI-run leaderboard. README is the leaderboard.
- Auditing tools whose claims are about latency or quality rather than cost.

## Success criteria

- A third party can reproduce any published row from the repo alone.
- Adding a subject requires only a TOML file and a `claim/` corpus entry.
- At least one maintainer response is published alongside a verdict.
- No row is published without either `VERIFIED` delivery or an explicit
  `UNVERIFIED` label.

## Phasing

v1 is too large for one implementation plan. Two phases, each independently shippable:

**Phase 1 — the rig, proved on ourselves.** Extract `core/` unchanged, build
`manifest.py`, `verdict.py`, `cli.py`, and the `claim/` corpus. Ship the ContextMesh
self-audit as the only row. This is a complete, publishable artifact: a working
auditor and one honest verdict against its own author.

**Phase 2 — the leaderboard.** RTK, Portal, and Headroom audits, plus the `neutral/`
corpus for cross-tool comparison. Each subject lands independently, under the
disclosure policy.

Phase 1 must be complete and its results published before Phase 2 begins. Auditing
other people's tools with a rig that has never been run end to end is exactly the
failure this project exists to criticise.

## Open decisions

- Package name: `truecost` (free on PyPI; `receipts` and `tokenaudit` are taken).
  Pending confirmation.
