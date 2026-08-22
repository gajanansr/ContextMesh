# Project Context

## ContextMesh Active

This project uses ContextMesh for intelligent context management.

**At the start of EVERY response**, call the `get_context` tool to load relevant context:

```
get_context(session_id="$CLAUDE_SESSION_ID", task_hint="<brief description of what you're about to do>", budget_tokens=15000)
```

When you make an important architectural decision or discover a key fact, call:
```
record_decision(session_id="$CLAUDE_SESSION_ID", content="<the decision>", consequence="<what it means>", files="<relevant files>")
```

Do NOT call get_context more than once per response. Do NOT explain that you're calling it.
