# ContextMesh — Intelligent Context Layer for Claude Code

> One coding session. Infinite memory. 90% fewer tokens.

ContextMesh is a **pure transparent proxy + memory engine** that sits between Claude Code and the Anthropic API. It automatically compresses token waste, maintains a persistent knowledge graph of your work, and injects Aider-style codebase repomaps — all without changing how you use Claude.

**No MCP. No wrappers. Just run `contextmesh init` once globally. Then use `claude` normally forever.**

---

## How it works (Pure Proxy Architecture)

Everything happens automatically in the proxy. Claude doesn't need to learn any new tools or call any MCP endpoints. 

```
Your Terminal (just run `claude`)
    │
    ▼
┌──────────────────────────────────────────┐
│         ContextMesh Smart Proxy          │  ← Intercepts every request globally
│  ┌─────────────────────────────────┐     │
│  │  Aider-Style RepoMap Injector   │     │  ← Injects AST map into system prompt
│  │  Session Resumption Injector    │     │  ← Injects last session memory
│  │  RTK Output Compressor          │     │  ← Crushes terminal noise
│  │  Anti-Context Auto-Flusher      │     │  ← Removes old resolved turns
│  └─────────────────────────────────┘     │
│             ↓ Stats Writer ↓             │
└─────────────┼──────────────┼─────────────┘
              │              │
      To Anthropic API     To Local SQLite (for dashboard & stats)
```

---

## Features

| Feature | What it does | Savings |
|---|---|---|
| **RTK Output Compressor** | Intercepts massive `grep`, `npm test`, `cat` outputs and crushes the middle noise | Up to 90% on tool outputs |
| **AST Repo-Map Injector** | Parses your entire codebase with Tree-sitter and silently injects a dense structural map into Claude's system prompt on Turn 1 | 95% on code reads |
| **Anti-Context Auto-Flusher** | Silently drops old resolved tool calls from history when context bloats past 150k chars | 30–60% on long sessions |
| **Session Resumption** | Automatically injects your last session's summary on startup so Claude remembers decisions | Saves re-explanation tokens |
| **God-Mode Dashboard** | Beautiful live web UI showing exact tokens saved, cost averted, and compression chart | — |
| **Universal Auth** | Works with API keys, Claude Max/Pro subscriptions, AWS Bedrock, and Google Vertex | — |

---

## One-Time Setup

ContextMesh installs **globally** for your entire machine. You do not need to set it up per-repository.

```bash
# 1. Install globally
pipx install claude-contextmesh

# 2. Initialize once for your whole machine
contextmesh init

# 3. Reload your shell
source ~/.zshrc   # or source ~/.bashrc
```

`contextmesh init` does 3 things automatically:
1. **Shell Profile** — writes `ANTHROPIC_BASE_URL` to `~/.zshrc` so every `claude` session routes through the proxy.
2. **Claude Code Hooks** — installs `PreToolUse`/`PostToolUse` hooks in `~/.claude/settings.json`.
3. **Persistent Service** — installs a macOS LaunchAgent (or Linux systemd unit) so the proxy auto-starts on login.

From now on, the global proxy will intercept Claude Code everywhere.

---

## 🚀 Using ContextMesh in a new project

Once you've run the global `init`, ContextMesh's core proxy features (RTK Output Compression, Anti-Context Flusher, and Token Tracking) are **automatically active for every folder on your computer**. Just type `claude` and you are instantly saving tokens.

**However, to enable the AST Repo-Map Injector:**
Claude usually wastes tens of thousands of tokens blindly opening and reading files to understand a new codebase. ContextMesh solves this by injecting a dense, pre-computed AST map of your project on turn 1. 

To enable this massive token-saving feature in a brand new project, just index it once before starting Claude:

```bash
cd /path/to/your/new/project
contextmesh index .

# Now start claude normally
claude
```

---

## Commands

### Core
```bash
contextmesh init          # One-time global setup (run once per machine)
contextmesh status        # Check if the proxy is running and view live savings
contextmesh stop          # Stop the proxy service
contextmesh uninstall     # Cleanly remove all ContextMesh integrations
```

### Stats & Monitoring
```bash
contextmesh stats                           # Global token savings report
contextmesh dashboard                       # Open live web dashboard in browser
```

### Manual Controls (Optional)
```bash
contextmesh proxy &       # Start the proxy manually (if you stopped the service)
contextmesh index .       # Force a manual re-index of the current repo
contextmesh start         # Start the full background daemon (file watcher)
```

---

## Token Savings Report

The stats are tracked directly by the proxy sniffing the Anthropic API usage headers. It's 100% accurate.

```bash
$ contextmesh stats

╭─ ContextMesh Token Savings Report ─╮
│  Sessions tracked           │       12    │
│  Turns tracked              │      284    │
│                             │             │
│  Original tokens (est.)     │  2,847,000  │
│  Actual tokens sent         │    391,000  │
│                             │             │
│  RTK compressed             │  1,102,000  │
│  Context flushed            │  1,354,000  │
│  Total tokens saved         │  2,456,000  │
│  Avg compression ratio      │       14%   │
│  Estimated cost saved       │    $7.3680  │
╰────────────────────────────────────────────╯
```

---

## Live Dashboard

To watch your savings in real-time, start the daemon and open the dashboard:

```bash
contextmesh start &
contextmesh dashboard
```

You'll see a live, auto-refreshing dark-themed UI with:
- **4 stat cards**: Raw tokens sent, Compressed tokens, Total saved, USD saved
- **SVG bar chart**: Tokens saved per recent turns
- **RTK Interception Log**: Every compression event with timestamps and savings %

---

## Architecture: Aider-Style RepoMap

Without ContextMesh, Claude Code blindly guesses which files to read, burning tens of thousands of tokens per file. 

ContextMesh uses Tree-sitter to parse your codebase (`.py`, `.ts`, `.js`, `.go`, `.rs`) and generates a dense Abstract Syntax Tree (AST) map. On the very first message of your session, the proxy intercepts the request and silently injects this map into Claude's system prompt.

What Claude actually sees (invisibly to you):
```text
[ContextMesh RepoMap — injected automatically to save tokens on file reads]
src/contextmesh/proxy.py
  class TokenProxy  (L12)
    def startup_event  (L36)
    def proxy  (L140)
src/contextmesh/utils/compressor.py
    def compress_outbound_payload  (L11)
...
[Use file line numbers above to read only what you need]
```
Claude now knows your exact project architecture instantly on turn 1, saving massive amounts of context.

---

## Configuration

`~/.contextmesh/config.toml` (global):

```toml
[tracker]
input_price_per_mtok = 3.0        # Claude cached input price (USD per million)

[proxy]
port = 8099
```

---

## Supported Auth Modes

ContextMesh acts as a pure local HTTP proxy and supports all official Anthropic auth modes:

| Mode | Detection | Behavior |
|---|---|---|
| **API Key** | `ANTHROPIC_API_KEY` is set | Full proxy (compression + cost tracking) |
| **Max/Pro** | OAuth login | Full proxy (compression only) |
| **Bedrock** | `CLAUDE_CODE_USE_BEDROCK=1` | Handled natively by Claude |
| **Vertex** | `CLAUDE_CODE_USE_VERTEX=1` | Handled natively by Claude |

---

## License

MIT
