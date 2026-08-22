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

```bash
# Clone and install
git clone https://github.com/you/ContextMesh
cd ContextMesh
pip install -e .

# Run the install script (sets up hooks + config)
bash scripts/install.sh

# Index your project's codebase
contextmesh index /path/to/your/project

# Start the daemon
contextmesh start

# Start the MCP server (in a separate terminal)
contextmesh mcp
```

### For exact token measurement (Claude Enterprise)

```bash
# Start the token proxy
contextmesh proxy

# Then set in your environment:
export ANTHROPIC_BASE_URL=http://localhost:8099

# This records EXACT token counts from actual API responses
# including cache_creation_input_tokens and cache_read_input_tokens
```

## Adding ContextMesh to a project

Copy `CLAUDE.md` from this repo to your project root. Claude Code will automatically instruct Claude to use the `get_context` tool at the start of each response.

```bash
cp /path/to/ContextMesh/CLAUDE.md /path/to/your/project/CLAUDE.md
```

## Register MCP server with Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "contextmesh": {
      "command": "contextmesh",
      "args": ["mcp"]
    }
  }
}
```

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
