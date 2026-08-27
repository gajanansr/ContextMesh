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
- [x] Broaden the corpus beyond the two original memory mechanisms. Added
      `backoff`, needing a SOLUTION node rather than a DECISION or an
      UNRESOLVED_ISSUE — a third mechanism, not a third variation on the same
      one. `bench/results/memory-broadened-2026-08-28.json`, delivery
      confirmed 0/12 off / 12/12 on, 23/24 verified.
      convention+deadend alone (n=6, same shape as the 08-27 result): turns
      -1.5, CI [-2.79, -0.21], -28.1%, significant — consistent with and
      slightly stronger than the original -17.9%.
      Adding backoff (n=8): turns -3.25, CI [-6.50, -0.004], -45.6% —
      technically significant but fragile: one `off` replicate went
      unverified and another took 16 turns / $0.27 (vs a flat 4 turns / ~$0.05
      every time `on`), so the pooled number is carried by a single outlier
      and I'm not standing behind the -45.6% figure on its own.
      What I will stand behind: without memory this task was inconsistent —
      1 of 3 replicates failed outright, another spiked hard — and with memory
      it was 4 turns and passed, every time. That is a claim about variance
      and reliability, not mean turns, and it is the kind of thing a
      compression-only tool cannot offer regardless of ranking or gating.
      Control: turns 0.00 across all 3 replicates again, cost +$0.021 (n.s.)
      — the falsification check passes a second time on an expanded corpus.
- [x] Port Aider's query-personalized PageRank and re-measure. THIRD RESULT,
      BEST OF THE THREE, STILL LOSES:
      `bench/results/repomap-personalized-2026-08-28.json`.
        alphabetical      cost +45.6%   turns -0.33 (n.s.)
        static PageRank   cost +74.6%   turns -0.33 (n.s.)
        personalized      cost +35.9%   turns -0.86 (n.s.)   <- CI [+0.035,+0.121]
      Personalization clearly helped — cost regression roughly halved from the
      static version and the turn reduction is the largest yet (locate-class
      12.00 -> 10.67, locate-function 5.00 -> 4.00). It still did not flip:
      cost is significantly up, turns are not significantly down.
      CAVEAT, and it matters: 3 of 18 runs died on an API limit, all but one
      of them on the control task (control got 1/3 and 2/3 usable). This run
      therefore has effectively no falsification check, so it is weaker
      evidence than the two clean runs before it. The direction is consistent
      across all three, which is why I am not re-running it a fourth time to
      chase the control — but it should be stated, not buried.
      Default stays off.
- [ ] Re-measure after the gating change; confirm cost-neutral becomes
      cost-positive. Blocked on the same thing as gating itself: a warm
      embeddings model in the daemon. NOTE: Headroom does exactly this at
      <50ms with local embeddings, so the 28s torch import I called a blocker
      was a bad implementation path, not a real constraint.
- [x] Run the cross-tool comparison. Harness works end to end; 48 runs, all
      48 verified. `bench/results/crosstool-2026-08-28.json`. Two findings,
      one about us and one about Headroom.

- [ ] URGENT — memory is a significant COST on non-curated memory.
      Cross-tool run, n=12 pairs, delivery verified 0/12 off, 11/12 on:
        cost   +32.0%  CI [+0.0097, +0.1009]   significant
        billed +40.5%  CI [+3997, +17836]      significant
        turns  -0.58   CI [-1.84, +0.67]       no significant difference
      This does NOT contradict the -28.1% turns result; it isolates a
      condition that result never tested. The seeded corpus supplies curated,
      relevant memory. This corpus seeds nothing: memory accrued from the
      benchmark's own 12 prior sessions -- repeats of the same three prompts
      plus file-modification noise. So:
        curated, relevant memory    -> -28.1% turns, cost-neutral
        memory as it naturally accrues -> +32% cost, no turn benefit
      Per-task, the control (which memory cannot help) went 12,486 -> 25,853
      billed input: injection roughly doubled it for nothing.
      The public claim must be qualified accordingly, and relevance gating is
      no longer an optimisation -- it is required for the feature to be
      net-positive outside a curated fixture.

- [ ] Re-run the Headroom comparison with a valid control. The current one is
      VOID: `--no-optimize` does not disable compression in headroom 0.36.5.
      A proxy started with that flag, whose banner printed
      "Optimization: DISABLED", compressed the one request sent to it by 5.0%
      (1,398 tokens removed). Confirmed not to be shared-stats pollution -- a
      third proxy on an unused port reported zeros until traffic arrived.
      Both arms therefore compressed (4.6% vs 4.9%) and the comparison
      measured compression against compression, which is exactly why every
      metric came back "no significant difference".
      Nothing can currently be said about whether Headroom helps or hurts.
      Worth reporting upstream; it is an open-source project.
- [ ] Publish methodology as a results page. Nobody in this category can
      currently prove their numbers — that is the opening. Everything needed
      exists in bench/results/; this is a writing task now, not a measurement
      one.

## Known limits to keep honest
- The clean, standable result is n=6 (convention+deadend). The n=8 pooled
  figure that includes backoff is real data but fragile — one extreme outlier
  carries it, and I would not publish -45.6% as a headline on its own.
- Tasks authored in-house; the control task is the only guard against that.
- Decisions/hypotheses beyond DECISION/SOLUTION/UNRESOLVED_ISSUE are not
  extracted — deterministic extraction cannot see them, and guessing would be
  worse than omitting them.
- RepoMap: two negative results now (unranked +45.6%, ranked +74.6%, both
  significant, neither showing a significant turn benefit). Off by default.
  Not worth a third attempt without a new idea about *when* to inject, not
  just *what* or *how ordered*.
