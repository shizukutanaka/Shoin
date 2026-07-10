"""Shoin configuration: constants and environment-derived settings."""

from __future__ import annotations

import json
import os
from pathlib import Path

VERSION = "0.2.78"

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
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


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
