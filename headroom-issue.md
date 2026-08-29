[BUG] `--no-optimize` is not a true passthrough: tool-schema compaction still runs and is counted as compression

## Summary

`headroom proxy --no-optimize` is documented as "Passthrough mode (no optimization)" and its banner prints `Optimization: DISABLED`, but the request body is still modified and `/stats` still reports compression.

Message compression *is* correctly disabled — `CompressionDecision.decide()` honours `config.optimize=False` exactly as its docstring describes. The remaining transform is tool-schema compaction, which appears to run unconditionally.

On a single trivial request through a `--no-optimize` proxy, `/stats` reported 1 request compressed, 5.0% average, and 1,398 tokens removed.

## Environment

- headroom-ai 0.36.5 (`pipx install --python python3.13 "headroom-ai[all]"`)
- Python 3.13.14, macOS arm64
- Client: Claude Code via `ANTHROPIC_BASE_URL`, model `claude-opus-5`

## Reproduction

1. Start a proxy on an unused port:

```bash
headroom proxy --port 8799 --no-optimize
```

Banner reports:

```
Mode:         cache
Optimization: DISABLED
```

2. Confirm no traffic seen yet:

```bash
curl -s http://127.0.0.1:8799/stats | python3 -c "
import json,sys; d=json.load(sys.stdin)['summary']
print('api_requests       :', d['api_requests'])
print('requests_compressed:', d['compression']['requests_compressed'])"
```

```
api_requests       : 0
requests_compressed: 0
```

3. Send exactly one request:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8799 claude -p "reply with exactly: PONG"
```

4. Re-read stats:

```
api_requests        : 1
requests_compressed : 1
avg_compression_pct : 5.0
total_tokens_removed: 1398
```

1,398 tokens removed on a "reply PONG" request, where there is essentially no message content to compress — consistent with the tool schemas being compacted rather than the messages.

I ruled out stats being shared between proxy instances: a freshly started proxy on an unused port reports zeros until traffic reaches it (step 2), and the counters only moved after the single request in step 3.

## Where it appears to come from

In `headroom/proxy/handlers/anthropic.py`:

- Line 1645 — `CompressionDecision.decide(...)` is computed, and line 1665 correctly guards message compression with `if _decision.should_compress and not _skip_compression_for_backpressure:`. This part works as documented.
- Line 2782 — `compact_tools(body)` is called with no surrounding `should_compress`, `config.optimize`, or `_bypass` guard, so Layer 1 compaction (stripping `$schema`, `title`, `examples`, whitespace normalisation) runs regardless.

`headroom/proxy/tool_schema_compaction.py` gates Layers 2 and 3 behind `HEADROOM_TOOL_DESC_MAX_CHARS` and `HEADROOM_TOOL_DESC_STRIP_SEMANTIC`, but Layer 1 has no equivalent switch.

The savings are then counted into the same `/stats` compression totals, so a passthrough proxy reports non-zero compression.

## Why it matters

`--no-optimize` is the natural control condition when measuring whether the proxy helps: comparing an optimizing proxy against a passthrough proxy isolates compression from the proxy's own overhead, which is fairer than optimizing-proxy vs no-proxy.

I used it that way while benchmarking context tools. Both arms then reported ~4.6% and ~4.9% average compression across 12 runs each, and every metric came back "no significant difference" — because the arms were not actually different. That invalidated the comparison.

Separately, if a user sets `--no-optimize` specifically to guarantee byte-stable requests (prefix-cache stability), Layer 1 compaction still mutates the body.

## Possible fixes

I am not sure which behaviour is intended, so I have not sent a PR:

1. **If `--no-optimize` should mean "touch nothing"** — gate `compact_tools` at `anthropic.py:2782` (and the equivalent OpenAI call site) on the same decision that guards message compression.
2. **If Layer 1 is intentionally always-on** — because it is lossless and semantics-preserving — then the help text ("Passthrough mode (no optimization)") and the `Optimization: DISABLED` banner are misleading, and `/stats` arguably should not count it as compression when optimization is off.

Happy to open a PR for whichever you prefer, with a test.

## Related

#1360 reports that `--no-optimize`, `HEADROOM_PROXY_COMPRESSION` and `HEADROOM_PROXY_COMPRESSION_MODE` do not apply to `headroom wrap`, filed as a feature request. This is distinct: the flag is partially ineffective on `headroom proxy` itself.

## Not tested

- Whether `HEADROOM_PROXY_COMPRESSION=0` fully disables both layers.
- Whether the `x-headroom-bypass: true` / `x-headroom-mode: passthrough` headers also leave tool schemas untouched.
- The OpenAI and Gemini handlers, though `compact_tools` is shared.
