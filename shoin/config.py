"""Shoin configuration: constants and environment-derived settings."""

from __future__ import annotations

import os
from pathlib import Path

VERSION = "0.1.76"

DEFAULT_PORT = 7440
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # REQ-002: 10MB upload limit
CHUNK_TOKENS = 512  # REQ-003: target tokens per chunk
CHUNK_OVERLAP = 64  # REQ-003: overlap tokens between chunks
TOP_K = 8  # default retrieval depth
URL_TIMEOUT_SEC = 15
URL_MAX_REDIRECTS = 3


def data_dir() -> Path:
    """Resolve the data directory (SHOIN_DATA_DIR > XDG > ~/.local/share)."""
    env = os.environ.get("SHOIN_DATA_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "shoin"


def db_path() -> Path:
    return data_dir() / "shoin.sqlite3"


def llm_url() -> str:
    return os.environ.get("SHOIN_LLM_URL", "http://localhost:11434/v1")


def llm_model() -> str:
    return os.environ.get("SHOIN_LLM_MODEL", "qwen3:4b")


def embed_model() -> str:
    """Embedding model name. Empty string disables vector search (BM25 only)."""
    return os.environ.get("SHOIN_EMBED_MODEL", "nomic-embed-text")


def ui_lang() -> str:
    return os.environ.get("SHOIN_LANG", "ja")


def port() -> int:
    return int(os.environ.get("SHOIN_PORT", str(DEFAULT_PORT)))
