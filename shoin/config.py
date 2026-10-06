"""Shoin configuration: constants and environment-derived settings."""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.parse
from pathlib import Path

VERSION = "0.2.681"
API_VERSION = "1"  # X-Shoin-API response header; bump only on breaking changes


DEFAULT_PORT = 7440
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # REQ-002: 10MB upload limit
MAX_QUESTION_LEN = 2000  # chars; a longer FTS5 OR-expression becomes pathologically slow
MAX_NAME_LEN = 200       # chars; notebook names and note titles
MAX_TITLE_LEN = 500      # chars; source titles silently truncated (external content)
CHUNK_TOKENS = 512  # REQ-003: target tokens per chunk
CHUNK_OVERLAP = 64  # REQ-003: overlap tokens between chunks
TOP_K = 8  # default retrieval depth
SEARCH_K_MAX = 50  # /search k cap — hits carry full chunk text; unbounded k dumps the corpus
URL_TIMEOUT_SEC = 15
URL_MAX_REDIRECTS = 3
# Bound on any single blocking socket op on an accepted connection. Without it a
# client that opens a socket and sends nothing (or a partial body) holds its
# request thread forever — unbounded local connection leaks exhaust threads.
REQUEST_SOCKET_SEC = 120
MAX_CHUNKS_PER_NOTEBOOK = 50_000  # spec.md STRIDE DoS control; generous headroom
# v0.2.681: bound on the `shoin import` document. The API path is capped by
# _read_json at MAX_UPLOAD_BYTES; the CLI read the whole file/stdin into
# memory uncapped, so a hostile or accidental giant export OOM-killed the
# process mid-parse. 1 GiB leaves headroom over the largest legit export
# (~50k chunks × text + base64 embedding ≈ a few hundred MB) while bounding
# the resident set a hostile document can force.
MAX_IMPORT_BYTES = 1 << 30
NB_MESSAGES_LIMIT = 500  # messages embedded in GET /api/notebooks/{id} (UI history view)
NB_NOTES_LIMIT = 500  # notes embedded in GET /api/notebooks/{id} (UI notes pane)
QUERY_VEC_CACHE_SIZE = 64  # LRU entries for question embeddings (per model+question)
EMBED_MODEL_SETTING_KEY = "embed_model"  # settings key for the stored-vector builder model
# REQ-004: per-source retrieval weight bound. The weight multiplies a source's
# fused [0,1] retrieval score — 1.0 is neutral, >1 promotes, <1 demotes, 0 pins
# the source's chunks to the pool floor. Bounded so one source can dominate a
# ranking but never overflow it; store/server/CLI share this same bound.
SOURCE_WEIGHT_MAX = 8.0
# REQ-002: per-source metadata (author/year/…) serialized JSON byte bound.
# Meta is descriptive citation data a user writes via PATCH/CLI — unbounded
# it would become a per-source blob column; 4KB covers a reference manager's
# worth of fields with headroom while keeping detail responses small.
SOURCE_META_MAX = 4096

# REQ-004: per-notebook retrieval overrides (v0.2.659, product-review #20).
# notebooks.settings is a freeform JSON object, but only these keys act;
# bounds mirror the global defaults they override (qa.py's
# MIN_PER_SOURCE_TOKENS=64 floor and CONTEXT_TOKENS=2400 total budget, and
# SEARCH_K_MAX above). The settings writer whitelists keys so a typo'd key
# is a coded 400, not a silently inert setting.
NB_SETTING_KEYS = ("top_k", "source_text_tokens")
NB_SOURCE_TEXT_TOKENS_MIN = 64
NB_SOURCE_TEXT_TOKENS_MAX = 2400


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


def endpoint_is_external(url: str) -> bool:
    """True when an endpoint URL is NOT this machine (v0.2.674).

    The product promise is that document text and questions never leave
    the host — a non-loopback base_url silently breaks it (chunk text to
    the embeddings route, context+questions to chat). Loopback literals,
    `localhost` names and unspecified bind addresses count as local;
    anything else — LAN hosts, public IPs, DNS names — is external. A
    malformed URL reports False: it fails loudly at request time anyway
    and "cannot send anywhere" is not a leak.
    """
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a DNS name other than localhost — never this machine
    return not (ip.is_loopback or ip.is_unspecified)


def _split_authority(url: str) -> tuple[str, str, str]:
    """(prefix, authority, tail): prefix is 'scheme://' ('' when absent),
    authority the host/userinfo span before the first '/', '?' or '#',
    tail everything after it. Purely lexical — never raises on malformed
    input, which is the whole point for the error paths that use it."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return "", "", url
    end = len(rest)
    for delim in "/?#":
        i = rest.find(delim)
        if 0 <= i < end:
            end = i
    return f"{scheme}://", rest[:end], rest[end:]


def url_userinfo(url: str) -> str:
    """The userinfo (user:pass) portion of a URL — '' when absent (v0.2.678).

    Needed by callers that must know the exact credential substring a URL
    embeds (e.g. to scrub it from a server echo), where the redacted form
    is not enough."""
    _prefix, authority, _tail = _split_authority(url)
    if "@" not in authority:
        return ""
    return authority.rsplit("@", 1)[0]


def redact_url_credentials(url: str) -> str:
    """Strip userinfo (user:pass@) from a URL for display (v0.2.675).

    OpenAI-compatible gateways behind proxies take credentials in the
    endpoint URL userinfo (the user:pass@ prefix before the host).
    Every user-visible surface
    that echoes the endpoint — stderr warnings, LLMError messages, the
    health line — must show WHERE requests go without echoing the secret.
    Authority ends at the first '/', '?' or '#'; an '@' inside the query
    or fragment is data, not a credential, and stays untouched. A URL
    without '//' has no authority to carry userinfo — returned as-is
    (the '@' a bare path contains is not a credential).
    """
    prefix, authority, tail = _split_authority(url)
    if not prefix or "@" not in authority:
        return url
    return f"{prefix}{authority.rsplit('@', 1)[1]}{tail}"


def llm_model() -> str:
    return _get("SHOIN_LLM_MODEL", "qwen3:4b")


def embed_model() -> str:
    """Embedding model name. Empty string disables vector search (BM25 only)."""
    return _get("SHOIN_EMBED_MODEL", "nomic-embed-text")


def llm_api_key() -> str:
    """Bearer credential for auth-gated LLM gateways (SHOIN_LLM_API_KEY).

    Empty by default: local runtimes (Ollama/llama.cpp) ignore auth, and a
    stray `Authorization: Bearer ` header is itself a malformed-credential
    signal to strict gateways — so the header is attached only when set.
    """
    return _get("SHOIN_LLM_API_KEY", "")


def llm_retries() -> int:
    """Extra chat/embed attempts on transport failure (SHOIN_LLM_RETRIES, 0-5).

    Default 2 — catches the transient blip (endpoint restarting, a refused
    connection racing its own listen). Invalid or out-of-range values fall
    back to the default rather than disabling retries (same invalid->default
    contract as port()); an explicit 0 disables.
    """
    try:
        n = int(_get("SHOIN_LLM_RETRIES", "2"))
    except (ValueError, TypeError):
        return 2
    return n if 0 <= n <= 5 else 2


def ui_lang() -> str:
    return _get("SHOIN_LANG", "ja")


def theme_css_path() -> Path:
    """User theme stylesheet, served verbatim at /api/theme.css (v0.2.643).

    SHOIN_THEME_CSS > config.json > ~/.config/shoin/theme.css — next to
    config.json by default so one directory carries all optional config.
    """
    env = _get("SHOIN_THEME_CSS", "")
    if env:
        return Path(env).expanduser()
    return config_file().parent / "theme.css"


def port() -> int:
    try:
        n = int(_get("SHOIN_PORT", "") or DEFAULT_PORT)
    except (ValueError, TypeError):
        return DEFAULT_PORT
    # Same invalid->default contract as chunk_tokens()/embed_batch(): an
    # out-of-range port would otherwise reach HTTPServer as OverflowError
    # (not the OSError cli.main() catches) — a raw traceback at startup.
    return n if 0 <= n <= 65535 else DEFAULT_PORT


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


def chunk_tokens() -> int:
    """Target tokens per chunk (SHOIN_CHUNK_TOKENS), default CHUNK_TOKENS (512).

    Chunk size is a first-order retrieval-quality knob but was hardcoded, so the
    `shoin eval` measurement v0.2.141 shipped could not actually be run against
    different chunk sizes on the user's own corpus. This exposes it, mirroring
    SHOIN_EMBED_BATCH: an invalid or non-positive value falls back to the default.
    Takes effect on the next index/reindex (existing chunks keep their size).
    """
    raw = _get("SHOIN_CHUNK_TOKENS", "").strip()
    if not raw:
        return CHUNK_TOKENS
    try:
        n = int(raw)
    except (ValueError, TypeError):
        return CHUNK_TOKENS
    return n if n >= 1 else CHUNK_TOKENS


def chunk_overlap() -> int:
    """Overlap tokens between chunks (SHOIN_CHUNK_OVERLAP), default CHUNK_OVERLAP (64).

    The companion knob to chunk_tokens(). A 2026 systematic study found overlap
    gave no measurable benefit on one benchmark while the usual advice is 10-20%,
    and Shoin's own measurement (CLAUDE.md v0.2.141) showed it raises adjacent-chunk
    similarity — i.e. feeds the MMR redundancy penalty — so whether it helps *here*
    is exactly the kind of corpus-specific question `shoin eval` exists to answer.
    Invalid/negative values, or any value >= the effective chunk size (which would
    make chunks overlap wholly or stall progress), fall back to the default.
    """
    raw = _get("SHOIN_CHUNK_OVERLAP", "").strip()
    if not raw:
        return CHUNK_OVERLAP
    try:
        n = int(raw)
    except (ValueError, TypeError):
        return CHUNK_OVERLAP
    if n < 0 or n >= chunk_tokens():
        return CHUNK_OVERLAP
    return n

def log_json_enabled() -> bool:
    """Opt-in structured logging switch (SHOIN_LOG_JSON, v0.2.652).

    When truthy, shoin/log.py's emit() writes one JSON object per event to
    stderr — the machine-readable observability half that complements
    SHOIN_DEBUG's human-readable diagnostics. Default OFF (stderr quiet)."""
    return _get("SHOIN_LOG_JSON", "").strip().lower() in ("1", "true", "yes", "on")
