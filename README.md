# ContextMesh — Intelligent Context Layer for Claude Code

> One coding session. Infinite memory. Minimal active context.

ContextMesh sits between Claude Code and the model. It captures every event in your session, builds a structured knowledge graph of your work, and dynamically provides Claude with only the most relevant context for the current task — saving tokens, reducing cost, and preventing context overload.

## How it works

```
Claude Code Hooks → ContextMesh Daemon → Session Graph + Repo Graph
                                               ↓
                              Context Router (multi-signal scoring)
                                               ↓
                              Claude Code ← MCP Server (get_context)
                                               ↓
                              Token Savings Tracker → Report
```

**The key insight**: semantic similarity alone fails for long coding sessions because everything related to the same feature looks similar. ContextMesh uses graph proximity + code dependency relationships + causal edges + recency + file overlap — not just vectors.

## Installation

The easiest way to install ContextMesh globally is using `pipx`.

```bash
# 1. Install globally
pipx install claude-contextmesh

# 2. Register as a macOS background service (runs silently on boot)
contextmesh install-mac
```

## Setup a new project

When you start working on a new repository, just run:

```bash
cd /path/to/your/project
contextmesh init
```
*This instantly connects Claude Code to the MCP server, indexes your codebase for the repo graph, and configures the hooks.*

## Using ContextMesh

You have two options for running Claude Code with ContextMesh:

### Option A: Standard usage
Just run `claude` normally. Claude will automatically query ContextMesh for relevant memory and codebase context using the MCP server.

### Option B: Token Proxy mode (Recommended for Enterprise)
If you want to measure *exact* token savings and cost reductions on your Anthropic bill, use our wrapper command instead:
```bash
claude-mesh
```
*This acts exactly like Claude Code, but silently routes traffic through the local ContextMesh proxy to measure cache hits and real token usage.*


## View token savings

```bash
# Session summary
contextmesh stats --session YOUR_SESSION_ID

# Recent turns with savings breakdown
contextmesh turns --session YOUR_SESSION_ID --limit 20

# Global summary across all sessions
contextmesh stats

# If using proxy mode — actual API token counts
contextmesh stats --proxy
```

## Token savings tracker

Every time Claude calls `get_context()`, ContextMesh records:

| Metric | What it is |
|--------|-----------|
| **Accumulated tokens** | What the full session history would have been |
| **Routed tokens** | What ContextMesh actually provided |
| **Tokens saved** | The difference |
| **Compression ratio** | routed / accumulated |
| **Cost saved** | Based on your configured per-MTok price |

## Architecture

### Dual Graph

**Session Graph** — captures every meaningful event:
- User prompts, tool results, file reads/writes
- Decisions, bugs, solutions, errors, test results
- Typed edges: `caused_by`, `solved_by`, `depends_on`, `same_task`

**Repo Graph** — deterministic code relationships:
- Functions, classes, methods, files (Tree-sitter parsed)
- `calls`, `imports`, `same_file`, `tested_by`, `inherits` edges
- Updated incrementally on every file write

### Hot / Warm / Cold Memory

```
HOT   → Current task context (always injected)
WARM  → Related tasks, decisions, nearby graph nodes (retrieved on demand)
COLD  → Full historical transcripts (never auto-injected)
```

### Context Router

Before every `get_context()` call:

```
context_score =
    semantic_relevance   (embedding cosine similarity)
  + graph_proximity      (BFS distance in session graph)
  + file_overlap         (Jaccard similarity with current task files)
  + recency              (exponential decay from now)
  + causal_relevance     (DECISION/BUG/SOLUTION type bonus)
  + unresolved_bonus     (UNRESOLVED_ISSUE always surfaces)
```

### Cache-aware assembly order

```
STATIC (cacheable)
─────────────────────────────
=== CURRENT TASK ===
[hot nodes — current thread]

=== RELEVANT DECISIONS ===
[top-scored decisions]

DYNAMIC
─────────────────────────────
=== RELATED CODE CONTEXT ===
[repo graph: functions/classes in touched files]

=== RECENT HISTORY ===
[recent warm nodes]

=== UNRESOLVED ISSUES ===
[always surfaced]
```

## Configuration

`.contextmesh/config.toml` in your project (or `~/.contextmesh/config.toml` globally):

```toml
[router]
default_budget_tokens = 15000

[tracker]
input_price_per_mtok = 3.0       # Claude Enterprise cached input price
uncached_price_per_mtok = 15.0

[embeddings]
model = "all-MiniLM-L6-v2"       # Local, no API key needed

[tasks]
topic_shift_threshold = 0.35     # Cosine distance to detect task switch
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/
```
