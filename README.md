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
ContextMesh is the intelligent memory and context layer for **Claude Code**. 

Instead of letting Claude waste tens of thousands of tokens blindly reading files and repeating mistakes, ContextMesh seamlessly intercepts Claude's local tools to compress output and inject a dense architectural map of your project.

### Why use ContextMesh?
1. **The AST Repo-Map Injector:** Forces Claude to understand your entire project architecture on Turn 1 (like Aider).
2. **RTK Token Compression:** Intercepts noisy shell commands (`npm install`, `grep`) and compresses them by up to 90% before Claude sees them.
3. **100% Claude Pro Compatible:** Powered entirely by local Hooks. Zero network proxies. Works flawlessly with API keys, Claude Pro, AWS Bedrock, and Google Vertex.

---

## ⚡ Quickstart

### 1. Global Setup (Run Once)
Run this once for your entire machine:
```bash
pipx install claude-contextmesh
contextmesh init
```
This installs the **ContextMesh Hook Engine** directly into Claude Code's global settings. From now on, ContextMesh silently intercepts and compresses terminal commands.

### 2. Using it in a Project
To enable the massive token-saving **AST Repo-Map**, you need ContextMesh to index your codebase. You have two options:

**Option A: The Manual Way (One-time)**
Run this once when you enter a new project:
```bash
contextmesh index .
```
This builds the map. (You will need to run it again later if you make major structural changes to your files).

**Option B: The Automatic Way (File Watcher)**
Run this in the background while you work:
```bash
contextmesh start &
```
This starts the ContextMesh Daemon for the current repo. It will automatically watch your files for changes and keep the RepoMap perfectly up-to-date in real time. It also hosts a beautiful token-savings dashboard at `http://localhost:8765/dashboard`.

Now, just type `claude` and enjoy the token savings!

---

## 📊 Checking Stats

### Core
```bash
contextmesh init          # One-time global setup (run once per machine)
contextmesh status        # Check if the proxy is running and view live savings
contextmesh stop          # Stop the daemon
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
