"""Shoin configuration: constants and environment-derived settings."""

from __future__ import annotations

import json
import os
from pathlib import Path

VERSION = "0.2.136"

DEFAULT_PORT = 7440
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # REQ-002: 10MB upload limit
MAX_QUESTION_LEN = 2000  # chars; a longer FTS5 OR-expression becomes pathologically slow
MAX_NAME_LEN = 200       # chars; notebook names and note titles
MAX_TITLE_LEN = 500      # chars; source titles silently truncated (external content)
CHUNK_TOKENS = 512  # REQ-003: target tokens per chunk
CHUNK_OVERLAP = 64  # REQ-003: overlap tokens between chunks
TOP_K = 8  # default retrieval depth
URL_TIMEOUT_SEC = 15
URL_MAX_REDIRECTS = 3
MAX_CHUNKS_PER_NOTEBOOK = 50_000  # spec.md STRIDE DoS control; generous headroom


def config_file() -> Path:
    """Path to the optional JSON config file, per README.md's documented location."""
    return Path.home() / ".config" / "shoin" / "config.json"


def _file_config() -> dict[str, str]:
    """Best-effort load of the optional JSON config file.

    README.md has documented "環境変数または ~/.config/shoin/config.json" (environment
    variables OR config.json) as the two configuration paths since v0.1.0, but no code
    ever actually read the file — every setting was environment-variable-only. Missing
    or malformed files are silently ignored: config.json is optional, and callers
    (_get) always let a set environment variable take precedence.
    """
    try:
        raw = config_file().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    # Every non-string JSON value is a well-formed value, not a malformed file —
    # but blindly str()-ing it can silently produce a wrong-but-truthy setting
    # instead of falling through to env/default the way an absent key correctly
    # does (the v0.2.102 fix only caught null; this generalizes it): null and
    # containers (list/dict) never correspond to a sensible scalar setting, so
    # they're dropped entirely (behave like an absent key). bool is checked
    # before the int/float allowlist since bool is an int subclass in Python —
    # str(True) == "True" is not a value any setting expects. Plain numbers
    # (int/float) ARE allowed through: {"SHOIN_PORT": 8080} without quotes is
    # the natural, common way to write a port number in JSON.
    result: dict[str, str] = {}
    for k, v in data.items():
        if v is None or isinstance(v, (bool, list, dict)):
            continue
        if isinstance(v, (str, int, float)):
            result[str(k)] = str(v)
    return result


def _get(key: str, default: str) -> str:
    """Environment variable, then config.json, then the built-in default."""
    env = os.environ.get(key)
    if env is not None:
        return env
    return _file_config().get(key, default)


def data_dir() -> Path:
    """Resolve the data directory (SHOIN_DATA_DIR > config.json > XDG > ~/.local/share)."""
    env = _get("SHOIN_DATA_DIR", "")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "shoin"


def db_path() -> Path:
    return data_dir() / "shoin.sqlite3"


def llm_url() -> str:
    return _get("SHOIN_LLM_URL", "http://localhost:11434/v1")


def llm_model() -> str:
    return _get("SHOIN_LLM_MODEL", "qwen3:4b")


def embed_model() -> str:
    """Embedding model name. Empty string disables vector search (BM25 only)."""
    return _get("SHOIN_EMBED_MODEL", "nomic-embed-text")


def ui_lang() -> str:
    return _get("SHOIN_LANG", "ja")


def port() -> int:
    try:
        return int(_get("SHOIN_PORT", "") or DEFAULT_PORT)
    except (ValueError, TypeError):
        return DEFAULT_PORT


def multi_query_enabled() -> bool:
    """Opt-in switch for multi-query RAG-Fusion retrieval (SHOIN_MULTI_QUERY).

    Default OFF: rewriting the question costs one extra LLM call per ask, a
    real multi-second latency on the 4B-class local models this project targets
    ("Lightweight First"). Users who prefer recall over latency opt in with
    SHOIN_MULTI_QUERY=1. When the LLM is unreachable the feature silently
    degrades to single-query retrieval either way.
    """
    return _get("SHOIN_MULTI_QUERY", "").strip().lower() in ("1", "true", "yes", "on")


def embed_batch() -> int | None:
    """Optional override for the embedding batch size (SHOIN_EMBED_BATCH).

    Returns None when unset/invalid so pipeline.py falls back to its EMBED_BATCH
    module default (16). Lets users tune batch size to their endpoint's capacity
    (larger for fast endpoints, smaller for memory-constrained ones) — previously
    a hardcoded constant with no override, per CLAUDE.md's own known-gap note.
    """
    raw = _get("SHOIN_EMBED_BATCH", "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except (ValueError, TypeError):
        return None
    return n if n >= 1 else None
