"""
ContextMesh Configuration.

Config is loaded from (in priority order):
  1. .contextmesh/config.toml  (project-level)
  2. ~/.contextmesh/config.toml  (user-level)
  3. Defaults

Example config.toml:
  [daemon]
  port = 8765
  host = "127.0.0.1"

  [router]
  default_budget_tokens = 15000
  hot_budget_fraction = 0.20
  warm_budget_fraction = 0.50
  code_budget_fraction = 0.30

  [embeddings]
  model = "all-MiniLM-L6-v2"   # local, no API key needed

  [tasks]
  topic_shift_threshold = 0.35   # cosine distance threshold for task boundary
  min_turns_per_task = 3

  [tracker]
  input_price_per_mtok = 3.0     # Claude Enterprise input (cached)
  uncached_price_per_mtok = 15.0 # Claude uncached input

  [mcp]
  server_name = "contextmesh"

  [proxy]
  enabled = false
  port = 8099
  upstream = "https://api.anthropic.com"
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DaemonConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "info"


@dataclass
class RouterConfig:
    default_budget_tokens: int = 15_000
    # Fraction of budget per tier/category
    hot_budget_fraction: float = 0.20    # Current thread
    warm_budget_fraction: float = 0.50   # Related decisions, code
    code_budget_fraction: float = 0.30   # Repo graph context
    # Scorer weights (should sum to ~1.0)
    weight_semantic: float = 0.15
    weight_graph_proximity: float = 0.25
    weight_file_overlap: float = 0.20
    weight_recency: float = 0.15
    weight_causal: float = 0.15
    weight_unresolved: float = 0.10


@dataclass
class EmbeddingsConfig:
    model: str = "all-MiniLM-L6-v2"  # 384-dim, ~22MB, fast
    batch_size: int = 32
    cache_dir: str = ""  # empty = default HuggingFace cache


@dataclass
class TasksConfig:
    topic_shift_threshold: float = 0.35  # cosine distance 0-1
    min_turns_per_task: int = 3
    max_hot_tasks: int = 1
    warm_window_hours: float = 24.0


@dataclass
class TrackerConfig:
    input_price_per_mtok: float = 3.0     # USD per million tokens (cached)
    uncached_price_per_mtok: float = 15.0  # USD per million tokens (uncached)
    mcp_call_overhead_tokens: int = 300    # Estimated overhead per get_context() call


@dataclass
class MCPConfig:
    server_name: str = "contextmesh"
    transport: str = "stdio"  # "stdio" or "sse"
    sse_port: int = 8766


@dataclass
class ProxyConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8099
    upstream: str = "https://api.anthropic.com"


@dataclass
class Config:
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    router: RouterConfig = field(default_factory=RouterConfig)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    tasks: TasksConfig = field(default_factory=TasksConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)

    # Resolved at load time
    data_dir: Path = field(default_factory=lambda: Path.home() / ".contextmesh")
    project_path: Path = field(default_factory=Path.cwd)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge override into base."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_toml(path: Path) -> dict[str, Any]:
    if path.exists():
        with open(path, "rb") as f:
            return tomllib.load(f)
    return {}


def load_config(project_path: Path | None = None) -> Config:
    """Load and merge config from user-level and project-level files."""
    project_path = project_path or Path.cwd()

    user_cfg = _load_toml(Path.home() / ".contextmesh" / "config.toml")
    project_cfg = _load_toml(project_path / ".contextmesh" / "config.toml")

    raw = _merge(user_cfg, project_cfg)

    cfg = Config(project_path=project_path)

    # Apply daemon config
    if d := raw.get("daemon"):
        cfg.daemon = DaemonConfig(**{k: v for k, v in d.items() if hasattr(DaemonConfig, k)})

    if r := raw.get("router"):
        cfg.router = RouterConfig(**{k: v for k, v in r.items() if hasattr(RouterConfig, k)})

    if e := raw.get("embeddings"):
        cfg.embeddings = EmbeddingsConfig(**{k: v for k, v in e.items() if hasattr(EmbeddingsConfig, k)})

    if t := raw.get("tasks"):
        cfg.tasks = TasksConfig(**{k: v for k, v in t.items() if hasattr(TasksConfig, k)})

    if tr := raw.get("tracker"):
        cfg.tracker = TrackerConfig(**{k: v for k, v in tr.items() if hasattr(TrackerConfig, k)})

    if m := raw.get("mcp"):
        cfg.mcp = MCPConfig(**{k: v for k, v in m.items() if hasattr(MCPConfig, k)})

    if p := raw.get("proxy"):
        cfg.proxy = ProxyConfig(**{k: v for k, v in p.items() if hasattr(ProxyConfig, k)})

    # Data dir: respect CONTEXTMESH_DATA_DIR env var
    if env_data := os.environ.get("CONTEXTMESH_DATA_DIR"):
        cfg.data_dir = Path(env_data)
    else:
        cfg.data_dir = Path.home() / ".contextmesh"

    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    return cfg


# Module-level singleton — call load_config() to override
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(cfg: Config) -> None:
    global _config
    _config = cfg
