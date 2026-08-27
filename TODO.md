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
- [x] Merge `bench/measurement-core` into main
- [x] Bump to 0.10.0, build, reinstall via pipx
- [x] Verify installed hooks register all three events on a real session
- [x] Fix case-sensitive project matching — found by dogfooding the install,
      which injected nothing because ~/documents and ~/Documents were two
      separate silos

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
- [x] Rewrite README: it still documents the deleted network proxy, port 8099,
      and "100% accurate" API-header sniffing. Claim what is measured — fewer
      turns at equal cost — not "90% fewer tokens".

## 4. Earn the comparison
- [x] Measure the RepoMap. RESULT: it loses. Cost +45.6% significant, turns
      -0.33 not significant, even on locate tasks. Now off by default
      (CONTEXTMESH_REPOMAP=1 opts in).
- [x] Rank the RepoMap by PageRank over cross-file symbol references instead of
      sorting alphabetically. IMPLEMENTED AND RE-MEASURED, STILL LOSES:
      `bench/results/repomap-ranked-2026-08-28.json`, same corpus, delivery
      confirmed 9/9 treatment / 0/9 control. Cost +74.6% (CI [+0.0218,
      +0.0364]), significant — worse than the unranked +45.6%. Turns -0.33
      overall (n.s.), though both locate tasks individually trended better
      turn counts than the unranked run did on the same tasks (locate-class
      9.33→9.67 unranked vs 4.67→4.00 ranked; not a paired comparison across
      runs, so directional only, not a claim).
      Correct ordering was not the whole problem: the map still costs ~2,500
      tokens whether or not its contents are well-chosen, and that fixed tax
      has to be earned back in saved turns every session, on tasks that often
      don't need broad architectural context at all. The real fix is probably
      making the tax variable — skip injection on small codebases, size the
      budget to the task, or gate it on the same relevance signal memory
      needs anyway (see the item above) — not better sorting inside a fixed
      budget. Default stays off. Not picking this back up without a new idea
      for *when* to inject, not just *what*.
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
