# ContextMesh — Intelligent Context Layer for Claude Code

> One coding session. Infinite memory. 90% fewer tokens.

ContextMesh is a **transparent AI proxy + memory engine** that sits between Claude Code and the Anthropic API. It automatically compresses token waste, maintains a persistent knowledge graph of your work, and gives Claude perfect memory across sessions — all without changing how you use Claude.

**Just run `contextmesh init` once. Then use `claude` normally forever.**

---

## How it works

```
Your Terminal
    │
    ▼
┌──────────────────────────────────────────┐
│         ContextMesh Smart Proxy          │  ← Intercepts every request
│  ┌─────────────────────────────────┐     │
│  │  RTK Output Compressor          │     │  ← Crushes terminal noise
│  │  Anti-Context Auto-Flusher      │     │  ← Removes old resolved turns
│  │  Session Resumption Injector    │     │  ← Injects last session memory
│  └─────────────────────────────────┘     │
└──────────────────────────────────────────┘
    │
    ▼
Anthropic API  (only sees clean, compressed, focused context)
    │
    ▼
┌──────────────────────────────────────────┐
│         ContextMesh Daemon               │  ← Background brain
│  ┌───────────────┐  ┌─────────────────┐  │
│  │ Session Graph │  │   AST RepoMap   │  │  ← NetworkX + Tree-sitter
│  │ (your memory) │  │ (code structure)│  │
│  └───────────────┘  └─────────────────┘  │
│  ┌─────────────────────────────────┐     │
│  │  File Watcher (auto-reindex)    │     │  ← Keeps repomap always fresh
│  └─────────────────────────────────┘     │
└──────────────────────────────────────────┘
    │
    ▼ MCP Server
Claude Code ← get_context() | get_project_architecture() | record_decision()
```

---

## Features

| Feature | What it does | Savings |
|---|---|---|
| **RTK Output Compressor** | Intercepts massive `grep`, `npm test`, `cat` outputs and crushes the middle noise | Up to 90% on tool outputs |
| **Anti-Context Auto-Flusher** | Silently drops old resolved tool calls from history when context bloats past 150k chars | 30–60% on long sessions |
| **AST Repo-Map** | Parses your entire codebase with Tree-sitter, gives Claude a dense structural map instead of raw files | 95% on code reads |
| **Session Resumption** | Automatically injects last session summary on startup — no `/resume` command | Saves re-explanation tokens |
| **File Watcher** | Auto-reindexes changed files in background so the repo map is always fresh | Zero manual indexing |
| **God-Mode Dashboard** | Beautiful live web UI showing tokens saved, cost averted, and compression chart | — |
| **Universal Auth** | Works with API keys, Claude Max/Pro subscriptions, AWS Bedrock, and Google Vertex | — |

---

## Installation

```bash
# Install globally
pipx install claude-contextmesh

# One-time transparent setup (like RTK/Headroom — no wrapper needed after this!)
cd /path/to/your/project
contextmesh init
```

`contextmesh init` does 4 things automatically:
1. **Shell Profile** — writes `ANTHROPIC_BASE_URL` to `~/.zshrc` so every `claude` session routes through the proxy
2. **Claude Code Hooks** — installs `PreToolUse`/`PostToolUse` hooks in `~/.claude/settings.json`
3. **Persistent Service** — installs a macOS LaunchAgent (or Linux systemd unit) so the proxy auto-starts on login
4. **MCP Server** — connects the ContextMesh brain to Claude Code

Then **reload your shell** once:
```bash
source ~/.zshrc   # or source ~/.bashrc
```

From now on just use `claude` normally. ContextMesh intercepts everything transparently.

---

## Commands

### Core
```bash
contextmesh init          # One-time transparent setup (run once per machine)
contextmesh start         # Start the daemon manually (if not using the service)
contextmesh stop          # Stop the proxy service
contextmesh stop --all    # Stop both proxy AND daemon
contextmesh uninstall     # Remove all ContextMesh integrations cleanly
```

### Stats & Monitoring
```bash
contextmesh stats                           # Global token savings report
contextmesh stats --session SESSION_ID      # Per-session breakdown
contextmesh turns --session SESSION_ID      # Per-turn savings table
contextmesh status                          # Check if daemon + proxy are running
contextmesh dashboard                       # Open live web dashboard in browser
```

### Codebase
```bash
contextmesh index .                         # Manually index the current project
contextmesh proxy                           # Start the proxy manually (foreground)
contextmesh mcp                             # Run the MCP server (stdio)
```

---

## Live Dashboard

After running `contextmesh start`, open your browser to:

```
http://127.0.0.1:8765/dashboard
```

You'll see a live, auto-refreshing dark-themed dashboard with:
- **4 stat cards**: Raw tokens sent, Compressed tokens, Total saved (green), USD saved (green)
- **SVG bar chart**: Tokens saved per last 10 turns
- **RTK Interception Log**: Every compression event with timestamps and savings %

Or use the CLI shortcut:
```bash
contextmesh dashboard
```

---

## Token Savings Report

```bash
$ contextmesh stats

╭─ ContextMesh Global Token Savings Report ─╮
│  Sessions tracked           │       12    │
│  Turns tracked              │      284    │
│  Total baseline tokens      │  2,847,000  │
│  Total routed tokens        │    391,000  │
│  Tokens saved               │  2,456,000  │
│  Net saved (after overhead) │  2,412,000  │
│  Avg compression ratio      │       14%   │
│  Estimated cost saved       │    $7.3680  │
╰────────────────────────────────────────────╯
```

---

## MCP Tools available to Claude

| Tool | What Claude uses it for |
|---|---|
| `get_context(session_id, task_hint, budget_tokens)` | Retrieve optimally scored context for the current task |
| `get_project_architecture(project_path)` | Get AST repo-map (class/function signatures) without reading full files |
| `record_decision(session_id, content, consequence)` | Permanently store an architectural decision |
| `get_savings_report(session_id)` | See token savings from inside a session |
| `switch_task(session_id, new_task_name)` | Explicitly switch task context |
| `get_task_graph(session_id)` | View task hierarchy and node counts |

---

## Architecture

### Memory Tiers

```
HOT   → Current task context (always in every request)
WARM  → Related decisions, nearby graph nodes (retrieved on demand)
COLD  → Full historical archive (never auto-injected, always searchable)
```

### Context Scoring

Before every `get_context()` call, every node in the graph is scored:

```
score =
    semantic_relevance   (local embedding cosine similarity)
  + graph_proximity      (BFS distance — depth 1=1.0, depth 2=0.7, depth 3=0.4)
  + file_overlap         (Jaccard similarity with current task files)
  + recency              (exponential decay from last_active)
  + causal_relevance     (DECISION/BUG/SOLUTION type bonus)
  + unresolved_bonus     (UNRESOLVED_ISSUE always surfaces)
```

### Dual Graph

**Session Graph** — captures every meaningful event:
- User prompts, tool results, file reads/writes
- Decisions, bugs, solutions, errors, test results
- Typed edges: `caused_by`, `solved_by`, `depends_on`, `same_task`

**Repo Graph** — deterministic code relationships (Tree-sitter):
- Functions, classes, methods across `.py`, `.ts`, `.js`, `.go`, `.rs`
- `calls`, `imports`, `same_file`, `tested_by`, `inherits` edges
- Auto-updated by the file watcher on every save

---

## Configuration

`~/.contextmesh/config.toml` (global) or `.contextmesh/config.toml` (per project):

```toml
[router]
default_budget_tokens = 15000

[tracker]
input_price_per_mtok = 3.0        # Claude cached input price (USD per million)
uncached_price_per_mtok = 15.0

[embeddings]
model = "all-MiniLM-L6-v2"        # Local model, no API key needed (~22MB)

[tasks]
topic_shift_threshold = 0.35      # Cosine distance to auto-detect task switch

[proxy]
port = 8099
```

---

## Supported Claude Auth Modes

ContextMesh auto-detects how you authenticate and behaves accordingly:

| Mode | Detection | Behavior |
|---|---|---|
| **API Key** | `ANTHROPIC_API_KEY` is set | Full proxy (compression + cost tracking) |
| **Max/Pro Subscription** | OAuth login, no API key | Full proxy (compression only, no per-token cost) |
| **AWS Bedrock** | `CLAUDE_CODE_USE_BEDROCK=1` | Proxy skipped, daemon + MCP active |
| **Google Vertex** | `CLAUDE_CODE_USE_VERTEX=1` | Proxy skipped, daemon + MCP active |

---

## Development

```bash
git clone https://github.com/gajanansr/ContextMesh
cd ContextMesh
pip install -e ".[dev]"
```

---

## License

MIT
