"""Determine whether Claude subscription limits count raw or billed-equivalent tokens.

Anthropic does not document this, and it decides whether ContextMesh helped or
hurt on a Pro/Max plan. Every measurement in this repo is billed-equivalent
(cache reads discounted 10x), which is correct for API billing. If subscription
limits instead count raw tokens, the conclusions may invert: across all three
datasets ContextMesh used *fewer* raw tokens while costing more billed-equivalent.

## The idea

One session cannot answer it, because the limit's denominator is unknown. Two
sessions with very different raw:billed ratios can.

    Session A - "cache-heavy": many turns, little new content.
                Raw tokens >> billed-equivalent (ratio often 8-12x).
    Session B - "fresh": one turn, large new content, nothing cached.
                Raw ~= billed-equivalent (ratio near 1-2x).

Read /status before and after each. Then:

    if limits count RAW        -> delta_status / raw_tokens is ~constant across A and B
    if limits count BILLED-EQ  -> delta_status / billed_equiv is ~constant across A and B

Whichever ratio holds steady is the basis. The two sessions are built to have
deliberately different ratios so the two hypotheses give clearly different
predictions.

## Use

    python -m bench.probe_limit_basis --session A     # note /status before and after
    python -m bench.probe_limit_basis --session B
    python -m bench.probe_limit_basis --analyse \\
        --a-status 3.0 --b-status 5.0

Run A and B close together, inside the same 5-hour window, with no other Claude
usage in between, or unrelated traffic pollutes the /status delta.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from bench.runner import find_transcript
from bench.transcript import parse_session

# Many small turns against a warm prefix: cache reads pile up, new content stays
# tiny, so raw tokens greatly exceed billed-equivalent.
CACHE_HEAVY_PROMPT = (
    "Do these one at a time, using Bash for each, and keep every reply to a "
    "single word. 1) echo alpha 2) echo bravo 3) echo charlie 4) echo delta "
    "5) echo echo 6) echo foxtrot 7) echo golf 8) echo hotel. "
    "Then reply DONE."
)

# One turn over a large novel file: almost everything is cache-creation or fresh
# input, so raw and billed-equivalent land close together.
FRESH_PROMPT = (
    "Read the file payload.txt in full, then reply with only the number of "
    "lines it contains. Do not summarise it."
)

FRESH_PAYLOAD_LINES = 6_000


def build_workdir(kind: str) -> Path:
    work = Path(tempfile.mkdtemp(prefix=f"cm-limitprobe-{kind}-"))
    if kind == "B":
        # Distinctive, low-entropy-but-novel text so it cannot hit any cache.
        lines = [
            f"record {i:05d} | widget-{i % 97} | status=pending | owner=team-{i % 13}"
            for i in range(FRESH_PAYLOAD_LINES)
        ]
        (work / "payload.txt").write_text("\n".join(lines))
    return work


def run_session(kind: str) -> dict:
    work = build_workdir(kind)
    prompt = CACHE_HEAVY_PROMPT if kind == "A" else FRESH_PROMPT

    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json",
         "--permission-mode", "bypassPermissions"],
        cwd=work, capture_output=True, text=True, timeout=900,
    )
    payload = json.loads(proc.stdout)
    session_id = payload.get("session_id", "")

    transcript = find_transcript(session_id)
    if not transcript:
        raise SystemExit(f"no transcript found for session {session_id}")

    usage = parse_session(transcript).usage
    raw = (usage.input_tokens + usage.cache_read_tokens
           + usage.cache_write_5m_tokens + usage.cache_write_1h_tokens
           + usage.output_tokens)
    billed = usage.billed_input_equivalent + usage.output_tokens

    return {
        "kind": kind,
        "session_id": session_id,
        "raw_tokens": raw,
        "billed_equiv": round(billed, 1),
        "ratio": round(raw / billed, 2) if billed else 0.0,
        "cache_read": usage.cache_read_tokens,
        "cache_write": usage.cache_write_5m_tokens + usage.cache_write_1h_tokens,
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "turns": parse_session(transcript).assistant_turns,
    }


def analyse(a: dict, b: dict, a_status: float, b_status: float) -> str:
    """Compare which normalisation is stable across the two sessions."""
    lines = ["", "ANALYSIS", "=" * 62]
    lines.append(f"{'':10}{'raw':>12}{'billed-eq':>12}{'ratio':>8}{'/status Δ':>12}")
    for s, delta in ((a, a_status), (b, b_status)):
        lines.append(f"{s['kind']:10}{s['raw_tokens']:>12,}{s['billed_equiv']:>12,.0f}"
                     f"{s['ratio']:>8.2f}{delta:>11.2f}%")

    if min(a_status, b_status) <= 0:
        lines += ["", "Both /status deltas must be > 0 to compare. If one did not move,",
                  "the session was too small — increase it and re-run."]
        return "\n".join(lines)

    # Percent of limit consumed per token, under each hypothesis.
    raw_rate = (a_status / a["raw_tokens"], b_status / b["raw_tokens"])
    bil_rate = (a_status / a["billed_equiv"], b_status / b["billed_equiv"])

    def spread(pair):
        lo, hi = min(pair), max(pair)
        return (hi - lo) / hi if hi else 1.0

    raw_spread, bil_spread = spread(raw_rate), spread(bil_rate)

    lines += ["", f"  %limit per RAW token        A={raw_rate[0]:.3e}  B={raw_rate[1]:.3e}"
                  f"   spread {raw_spread:.0%}",
              f"  %limit per BILLED-EQ token  A={bil_rate[0]:.3e}  B={bil_rate[1]:.3e}"
              f"   spread {bil_spread:.0%}", ""]

    if abs(raw_spread - bil_spread) < 0.15:
        verdict = ("INCONCLUSIVE — the two normalisations agree too closely. The\n"
                   "  sessions' raw:billed ratios were not different enough. Make A\n"
                   "  longer (more turns) or B larger (bigger payload) and re-run.")
    elif raw_spread < bil_spread:
        verdict = ("Limits look RAW-token based. Consumption per raw token held\n"
                   "  steady while per-billed-token did not.\n"
                   "  => ContextMesh's billed penalty may not apply to a subscription.")
    else:
        verdict = ("Limits look BILLED-EQUIVALENT based (cache reads discounted).\n"
                   "  => the +38% billed penalty measured in this repo does apply,\n"
                   "     and ContextMesh cost you real usage headroom.")

    lines.append("  VERDICT: " + verdict)
    lines += ["", "  Caveat: n=1 per condition. Treat as indicative, not settled.",
              "  Re-run both if the two spreads are close."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session", choices=["A", "B"], help="run one probe session")
    p.add_argument("--analyse", action="store_true", help="analyse two saved runs")
    p.add_argument("--a-status", type=float, help="/status %% consumed by session A")
    p.add_argument("--b-status", type=float, help="/status %% consumed by session B")
    p.add_argument("--out", type=Path, default=Path("limit-probe.json"))
    args = p.parse_args(argv)

    saved = json.loads(args.out.read_text()) if args.out.exists() else {}

    if args.session:
        print(f"Running session {args.session}. Note your /status BEFORE reading this.\n")
        result = run_session(args.session)
        saved[args.session] = result
        args.out.write_text(json.dumps(saved, indent=2))
        print(json.dumps(result, indent=2))
        print(f"\nSaved to {args.out}. Now check /status again and record the delta.")
        return 0

    if args.analyse:
        if "A" not in saved or "B" not in saved:
            raise SystemExit(f"{args.out} needs both sessions; run --session A and B first")
        if args.a_status is None or args.b_status is None:
            raise SystemExit("pass --a-status and --b-status (the /status deltas, in %)")
        print(analyse(saved["A"], saved["B"], args.a_status, args.b_status))
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
