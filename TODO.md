# ContextMesh — closed

This file tracked the work that took ContextMesh from a broken prototype to a
measured one. That work is done and the project is archived. See README.md for
the results and the reasoning.

## What got finished

- Six real defects fixed, each verified against the failure it caused: symbol
  names sliced with byte offsets into a str (which garbled the RepoMap for any
  file containing a non-ASCII character, i.e. most of them), cross-project node
  bleed, duplicate accumulation on re-index, output destroyed on timeout,
  swallowed exit codes, and case-sensitive project path matching.
- The fabricated savings metric removed. It had been supplying 99.5% of its own
  headline from an invented constant.
- Released as 0.10.0 and verified installed end to end.
- Session memory built and measured. RepoMap measured three times.
- A benchmark harness that verifies its own treatment was delivered.
- A bug reported upstream to Headroom: `--no-optimize` does not gate
  tool-schema compaction (`anthropic.py:2782`).

## What was not finished, and why

- **Relevance gating.** Implemented, disabled. No cheap signal separated
  relevant from irrelevant memory. Needs embeddings; the 28s torch import that
  looked like a blocker was an implementation choice, not a constraint —
  Headroom does the same work in under 50ms.
- **Prefix stabilisation.** Identified from the literature after the measurement
  work concluded, never tested. The most promising remaining idea: the injected
  block changes every session as memory accumulates, so it can never inherit a
  warm cache.
- **A paper.** The amplification finding turned out to be prior art. TokenPilot
  (arXiv 2606.17016) states it as its opening premise. Searching the literature
  before the experiments would have saved most of the effort spent rediscovering
  it — the single most useful lesson here.

## Honest closing position

The features work. The economics do not. Anything injected into a cached session
bills at a multiple of its own size, so it must be worth roughly ten times its
token count, and only curated memory ever cleared that bar. That is a property of
the approach, not of this implementation, and no amount of feature work in this
repository changes it.
