"""
SQLite schema for ContextMesh.

All timestamps stored as ISO-8601 strings (UTC).
All JSON blobs stored as TEXT.
Embeddings stored as BLOB (numpy float32 array bytes).
"""

SCHEMA_VERSION = 3

CREATE_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

-- ─────────────────────────────────────────────
-- Sessions
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    project_path  TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    last_active   TEXT NOT NULL,
    metadata      TEXT             -- JSON: {git_branch, git_commit, ...}
);

-- ─────────────────────────────────────────────
-- Tasks (hierarchical: project > feature > thread)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    task_id        TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    parent_task_id TEXT REFERENCES tasks(task_id),
    name           TEXT NOT NULL,
    description    TEXT,
    task_type      TEXT NOT NULL,  -- 'project' | 'feature' | 'thread'
    status         TEXT NOT NULL,  -- 'active' | 'dormant' | 'completed'
    tier           TEXT NOT NULL,  -- 'hot' | 'warm' | 'cold'
    started_at     TEXT NOT NULL,
    last_active    TEXT,
    files_involved TEXT,           -- JSON: ["path/a.ts", "path/b.ts"]
    symbols        TEXT,           -- JSON: ["ClassName", "functionName"]
    metadata       TEXT            -- JSON
);

CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_tier ON tasks(tier);

-- ─────────────────────────────────────────────
-- Session Nodes (events, decisions, facts, etc.)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nodes (
    node_id         TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    task_id         TEXT REFERENCES tasks(task_id),
    node_type       TEXT NOT NULL,   -- NodeType enum
    content         TEXT NOT NULL,   -- Raw content / full text
    summary         TEXT,            -- Abstracted summary (filled async)
    files_involved  TEXT,            -- JSON: ["path/a.ts"]
    symbols         TEXT,            -- JSON: ["ClassName.method"]
    git_commit      TEXT,
    confidence      REAL DEFAULT 1.0,
    importance      REAL DEFAULT 0.5, -- 0.0–1.0
    tier            TEXT NOT NULL DEFAULT 'hot',
    token_count     INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL,
    metadata        TEXT             -- JSON: tool_name, exit_code, etc.
);

CREATE INDEX IF NOT EXISTS idx_nodes_session ON nodes(session_id);
CREATE INDEX IF NOT EXISTS idx_nodes_task ON nodes(task_id);
CREATE INDEX IF NOT EXISTS idx_nodes_tier ON nodes(tier);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_nodes_files ON nodes(files_involved);

-- ─────────────────────────────────────────────
-- Session Graph Edges
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS edges (
    edge_id   TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    edge_type TEXT NOT NULL,   -- EdgeType enum
    weight    REAL DEFAULT 1.0,
    metadata  TEXT             -- JSON
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);

-- ─────────────────────────────────────────────
-- Repository Graph Nodes (code structure)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS repo_nodes (
    node_id        TEXT PRIMARY KEY,
    project_path   TEXT NOT NULL,
    repo_node_type TEXT NOT NULL,   -- 'file' | 'function' | 'class' | 'method' | 'module'
    name           TEXT NOT NULL,
    qualified_name TEXT,            -- full.qualified.Name
    file_path      TEXT,
    start_line     INTEGER,
    end_line       INTEGER,
    language       TEXT,
    signature      TEXT,
    docstring      TEXT,
    token_count    INTEGER DEFAULT 0,
    last_modified  TEXT,
    metadata       TEXT             -- JSON
);

CREATE INDEX IF NOT EXISTS idx_repo_nodes_project ON repo_nodes(project_path);
CREATE INDEX IF NOT EXISTS idx_repo_nodes_file ON repo_nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_repo_nodes_name ON repo_nodes(name);

-- ─────────────────────────────────────────────
-- Repository Graph Edges (code relationships)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS repo_edges (
    edge_id   TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES repo_nodes(node_id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES repo_nodes(node_id) ON DELETE CASCADE,
    edge_type TEXT NOT NULL,  -- 'calls' | 'imports' | 'same_file' | 'tested_by' | 'inherits'
    metadata  TEXT
);

CREATE INDEX IF NOT EXISTS idx_repo_edges_source ON repo_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_repo_edges_target ON repo_edges(target_id);

-- ─────────────────────────────────────────────
-- Identifier references (per file), for RepoMap ranking
-- ─────────────────────────────────────────────
-- Which identifiers each file *uses*, as opposed to repo_nodes which records
-- what each file *defines*. Ranking needs both: an edge runs from the file
-- referencing a name to the file defining it, and PageRank over that graph is
-- what surfaces the code a codebase actually depends on.
--
-- Stored per file rather than as repo_edges rows because the graph is rebuilt
-- at recall time with personalization from the user's prompt, so the edges
-- themselves are query-dependent and cannot be precomputed.
CREATE TABLE IF NOT EXISTS repo_refs (
    project_path TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    name         TEXT NOT NULL,
    ref_count    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_path, file_path, name)
);

CREATE INDEX IF NOT EXISTS idx_repo_refs_project ON repo_refs(project_path);
CREATE INDEX IF NOT EXISTS idx_repo_refs_name ON repo_refs(name);

-- ─────────────────────────────────────────────
-- Embeddings (session + repo nodes)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS embeddings (
    node_id    TEXT PRIMARY KEY,
    node_table TEXT NOT NULL,   -- 'nodes' | 'repo_nodes'
    embedding  BLOB NOT NULL,   -- numpy float32 array, little-endian
    dim        INTEGER NOT NULL,
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- ─────────────────────────────────────────────
-- Token Savings Tracker
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS token_savings (
    turn_id                    TEXT PRIMARY KEY,
    session_id                 TEXT NOT NULL REFERENCES sessions(session_id),
    task_id                    TEXT REFERENCES tasks(task_id),
    timestamp                  TEXT NOT NULL,

    -- Baseline: what the full accumulated session would have been
    accumulated_session_tokens INTEGER NOT NULL DEFAULT 0,

    -- Actual: what ContextMesh routed
    routed_tokens              INTEGER NOT NULL DEFAULT 0,
    mcp_overhead_tokens        INTEGER NOT NULL DEFAULT 300,

    -- Memory tier breakdown
    hot_tokens                 INTEGER NOT NULL DEFAULT 0,
    warm_tokens                INTEGER NOT NULL DEFAULT 0,
    cold_tokens                INTEGER NOT NULL DEFAULT 0,
    repo_tokens                INTEGER NOT NULL DEFAULT 0,

    -- Calculated savings
    tokens_saved               INTEGER GENERATED ALWAYS AS
                                   (MAX(0, accumulated_session_tokens - routed_tokens)) STORED,
    net_tokens_saved           INTEGER GENERATED ALWAYS AS
                                   (MAX(0, accumulated_session_tokens - routed_tokens - mcp_overhead_tokens)) STORED,
    compression_ratio          REAL GENERATED ALWAYS AS
                                   (CASE WHEN accumulated_session_tokens > 0
                                    THEN CAST(routed_tokens AS REAL) / accumulated_session_tokens
                                    ELSE 1.0 END) STORED,

    -- Cost (USD) based on configurable per-MTok pricing
    input_price_per_mtok       REAL NOT NULL DEFAULT 3.0,
    baseline_cost_usd          REAL GENERATED ALWAYS AS
                                   (accumulated_session_tokens * input_price_per_mtok / 1000000.0) STORED,
    actual_cost_usd            REAL GENERATED ALWAYS AS
                                   ((routed_tokens + mcp_overhead_tokens) * input_price_per_mtok / 1000000.0) STORED,
    cost_saved_usd             REAL GENERATED ALWAYS AS
                                   (MAX(0.0, (accumulated_session_tokens - routed_tokens - mcp_overhead_tokens)
                                       * input_price_per_mtok / 1000000.0)) STORED,

    -- Debug: which nodes were included (JSON array of node_ids)
    included_nodes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_savings_session ON token_savings(session_id);
CREATE INDEX IF NOT EXISTS idx_savings_timestamp ON token_savings(timestamp);

-- ─────────────────────────────────────────────
-- Raw Session Token Accumulator (for baseline)
-- Tracks rolling token count of the raw session
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS session_accumulator (
    session_id        TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    turn_index        INTEGER NOT NULL,
    role              TEXT NOT NULL,    -- 'user' | 'assistant' | 'tool_result'
    content_preview   TEXT,            -- First 200 chars for debug
    token_count       INTEGER NOT NULL,
    cumulative_tokens INTEGER NOT NULL,
    timestamp         TEXT NOT NULL,
    PRIMARY KEY (session_id, turn_index)
);

-- ─────────────────────────────────────────────
-- Proxy Measurements (actual API response tokens)
-- Populated by the optional local proxy mode
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS proxy_measurements (
    measurement_id               TEXT PRIMARY KEY,
    session_id                   TEXT,
    timestamp                    TEXT NOT NULL,
    model                        TEXT,
    input_tokens                 INTEGER DEFAULT 0,
    output_tokens                INTEGER DEFAULT 0,
    cache_creation_input_tokens  INTEGER DEFAULT 0,
    cache_read_input_tokens      INTEGER DEFAULT 0,
    original_input_tokens        INTEGER DEFAULT 0,  -- tokens before RTK compression
    rtk_tokens_saved             INTEGER DEFAULT 0,  -- tokens crushed by RTK compressor
    flush_tokens_saved           INTEGER DEFAULT 0,  -- tokens removed by context flusher
    -- Derived
    total_input_with_cache       INTEGER GENERATED ALWAYS AS
                                     (input_tokens + cache_read_input_tokens) STORED,
    request_preview              TEXT   -- First 200 chars of request body
);

CREATE INDEX IF NOT EXISTS idx_proxy_session ON proxy_measurements(session_id);
CREATE INDEX IF NOT EXISTS idx_proxy_timestamp ON proxy_measurements(timestamp);
"""

DROP_SCHEMA_SQL = """
DROP TABLE IF EXISTS proxy_measurements;
DROP TABLE IF EXISTS session_accumulator;
DROP TABLE IF EXISTS token_savings;
DROP TABLE IF EXISTS embeddings;
DROP TABLE IF EXISTS repo_edges;
DROP TABLE IF EXISTS repo_nodes;
DROP TABLE IF EXISTS edges;
DROP TABLE IF EXISTS nodes;
DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS schema_version;
"""
