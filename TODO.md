# ContextMesh — road to production

Status of the work that turns this from a broken prototype into something
defensible. Ordered by what blocks what.

Measured baseline (2026-08-27, `bench/results/memory-2026-08-27.json`):
memory reduces turns 17.9% on tasks it applies to (CI [-1.62, -0.04]) and is
cost-neutral doing so. On irrelevant tasks it is a pure ~$0.027/session tax.
Prompt caching alone already saves 85.6%; every claim is a delta on top of that.

## 1. Ship it  — nothing below matters until the code users run is this code
- [x] Fix `contextmesh run` 300s timeout: it kills long commands AND discards
      all output. Data loss, not a nuisance. Make it configurable, keep partial
      output, report the timeout to the model instead of swallowing it.
- [ ] Merge `bench/measurement-core` into main
- [ ] Bump to 0.10.0, build, reinstall via pipx
- [ ] Verify installed hooks register all three events on a real session

## 2. Make the features actually pay off
- [~] Gate memory injection on relevance score. IMPLEMENTED BUT OFF —
      no cheap signal separates relevant from irrelevant memory (file overlap
      and word overlap both score a related and an unrelated prompt at 0.300).
      Needs embeddings; sentence-transformers costs 28s to import, so it has
      to live in the daemon with the hook falling back when it is down. The control task proved
      injection is pure cost when nothing relevant is stored; `ContextScorer`
      already computes the score, so this is a threshold, not new machinery.
- [x] Move the RepoMap injection off the first Bash tool result. Today it never
      fires if a session opens with Read/Grep, and it lands after the agent has
      already planned. Belongs at SessionStart/UserPromptSubmit — also the only
      position inside the cached prefix.

## 3. Tell the truth about it
- [ ] Rewrite README: it still documents the deleted network proxy, port 8099,
      and "100% accurate" API-header sniffing. Claim what is measured — fewer
      turns at equal cost — not "90% fewer tokens".

## 4. Earn the comparison
- [ ] Measure the RepoMap itself. It now injects ~10k chars (~2.5k tokens) per
      session at the same point as memory, and has never been measured. The
      harness can A/B it the same way it did memory.
- [ ] Broaden the corpus beyond one synthetic fixture and my own authored tasks
- [ ] Re-measure after the gating change; confirm cost-neutral becomes
      cost-positive
- [ ] Publish methodology. Nobody in this category can currently prove their
      numbers — that is the opening.

## Known limits to keep honest
- n=6 pairs on the significant result. Real, but barely clear of zero.
- Tasks authored in-house; the control task is the only guard against that.
- Decisions/hypotheses are not extracted — deterministic extraction cannot see
  them, and guessing would be worse than omitting them.
