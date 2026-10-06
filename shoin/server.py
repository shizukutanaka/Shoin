"""Shoin web server: stdlib HTTP, bound to 127.0.0.1 only (spec STRIDE).

Single-user local app. Each request opens its own Store (SQLite/WAL), the LLM
backend is shared and injectable for tests. `ask` streams over SSE; everything
else is plain JSON. No path-based static serving: only the embedded index.html.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
import tempfile
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .citation import make_report
from .config import (
    API_VERSION,
    EMBED_MODEL_SETTING_KEY,
    MAX_QUESTION_LEN,
    MAX_TITLE_LEN,
    MAX_UPLOAD_BYTES,
    NB_MESSAGES_LIMIT,
    NB_NOTES_LIMIT,
    REQUEST_SOCKET_SEC,
    SEARCH_K_MAX,
    SOURCE_WEIGHT_MAX,
    TOP_K,
    VERSION,
    db_path,
    multi_query_enabled,
    theme_css_path,
    ui_lang,
)
from .export import FORMATS, export
from .ingest import IngestError
from .llm import LLMClient, LLMError
from .pipeline import (
    index_source,
    refresh_all_sources,
    refresh_source,
    reindex_notebook,
    rename_source,
    source_is_refreshable,
)
from .qa import (
    SOURCE_TEXT_TOKENS as _QA_SOURCE_TEXT_TOKENS,
)
from .qa import (
    ChatBackend,
    _check_embed_model_ok,
    _degraded_text,
    _embed_model_stale,
    _query_vector,
    build_context,
    build_messages,
    expand_query,
    history_messages,
    retrieve_for_question,
)
from .qa import (
    _t as _qa_t,
)
from .search import suggest_corrections
from .store import Store, StoreError
from .studio import KINDS, generate, suggest_questions

# Startup-log strings for serve(). Kept module-local (same minimal pattern as
# export.py) rather than imported from cli.py, which imports THIS module.
# Everything else the server emits is machine-facing JSON; these two lines are
# the only human-facing text it prints, and they were hardcoded Japanese, so a
# SHOIN_LANG=en user's very first interaction with the product was untranslated.
_STRINGS: dict[str, dict[str, str]] = {
    "serve.no_egress": {
        "ja": "外部送信なし。Ctrl+C で終了。",
        "en": "No data leaves this machine. Ctrl+C to stop.",
    },
    "serve.stopped": {"ja": "停止。", "en": "Stopped."},
}


def _t(key: str) -> str:
    lang = ui_lang()
    return _STRINGS[key].get(lang, _STRINGS[key]["en"])

_STATIC = Path(__file__).resolve().parent / "static" / "index.html"
# v0.2.643: bound the user-theme response — a cosmetic hook must not be a
# DoS backdoor by pointing SHOIN_THEME_CSS at a giant file.
_THEME_CSS_LIMIT = 256 * 1024

_EXPORT_MIME = {
    "md": "text/markdown; charset=utf-8",
    "bibtex": "application/x-bibtex; charset=utf-8",
    "ris": "application/x-research-info-systems; charset=utf-8",
}
# BibTeX files are universally expected to have the .bib extension, not .bibtex.
_EXPORT_EXT = {"md": "md", "bibtex": "bib", "ris": "ris"}


def _check_utf8(key: str, value: str) -> None:
    """Reject strings that cannot round-trip through UTF-8.

    json.loads materializes lone surrogates from \ud800-style escapes that raw
    UTF-8 request bytes cannot carry. One reaching a write surfaces as an
    uncaught UnicodeEncodeError out of the sqlite3 binding — or out of
    json.dumps(...).encode("utf-8") if it ever reaches a response — a raw 500
    for what is a client-side format error.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise StoreError(
            "VALIDATION_FIELD_FORMAT_INVALID", f"{key} contains an unpaired surrogate"
        ) from None

# Hostnames a browser may legitimately use to reach this loopback server.
# Anything else (e.g. attacker.example rebound to 127.0.0.1) is rejected:
# DNS rebinding / CSRF defense for the local web UI (spec STRIDE).
_ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname_of(netloc_like: str) -> str:
    """Extract a lowercase hostname from a Host header or Origin URL."""
    try:
        if "://" in netloc_like:
            return (urllib.parse.urlsplit(netloc_like).hostname or "").lower()
        return (urllib.parse.urlsplit(f"//{netloc_like}").hostname or "").lower()
    except ValueError:
        return ""


Json = dict[str, Any]


def _safe_report(raw: Any) -> dict[str, Any]:
    """Parse a citation_report JSON blob; return empty dict on corrupt data."""
    if raw is None:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (json.JSONDecodeError, ValueError):
        print(f"Warning: corrupt citation_report in DB (ignored): {raw!r:.120}", file=sys.stderr)
        return {}
    # A stored blob can be *valid* JSON without being a report — `"[1,2]"`,
    # `"5"`, `"true"` all parse but emit a non-object `report` field that the
    # response schema never produces (every reader does `report.<field>`).
    # export.py's `_parse_report` degrades the same shapes to {}, so the API
    # and export surfaces now degrade identically.
    return parsed if isinstance(parsed, dict) else {}


def _notebook_json(store: Store, nb_id: int) -> Json:
    nb = store.get_notebook(nb_id)
    # Chats grow monotonically; without a cap every mutation round-trips the
    # whole history (and the SSE-drop recovery refetches it too). Embed only the
    # newest NB_MESSAGES_LIMIT and report how many were omitted — the UI shows
    # an honest "earlier N not shown" line rather than silently dropping them;
    # export() and the DB still hold the full record.
    recent_msgs = store.list_messages_recent(nb_id, NB_MESSAGES_LIMIT + 1)
    omitted = 0
    if len(recent_msgs) > NB_MESSAGES_LIMIT:
        recent_msgs = recent_msgs[len(recent_msgs) - NB_MESSAGES_LIMIT :]
        omitted = store.count_messages(nb_id) - len(recent_msgs)
    # Same cap for notes: they embed verbatim in this payload, so an
    # accumulating notes pane would otherwise make every detail fetch
    # (openNotebook, the SSE-drop recovery refetch) heavier forever. Newest
    # NB_NOTES_LIMIT are kept — dropping the oldest means the note a user
    # just added is always visible; notes_omitted discloses the hidden count
    # and export() still writes the full record.
    all_notes = store.list_notes(nb_id)
    notes_omitted = max(0, len(all_notes) - NB_NOTES_LIMIT)
    notes = all_notes[notes_omitted:]
    return {
        "id": nb.id,
        "name": nb.name,
        "settings": nb.settings,
        "counts": store.counts(nb_id),
        "sources": [
            {
                "id": s.id,
                "kind": s.kind,
                "title": s.title,
                "origin": s.origin,
                "weight": s.weight,
                "meta": s.meta,
                # Refreshability is decided by what the origin can still be
                # read from, not by kind: URL sources always qualify; a file
                # source qualifies only while its recorded path still exists
                # (an upload's tmp copy is unlinked after ingest, so it reads
                # false and the UI hides a button that could only error).
                # Shared with the batch path — see pipeline.source_is_refreshable.
                "refreshable": source_is_refreshable(s),
            }
            for s in store.sources_for_notebook(nb_id)
        ],
        "notes": [
            {"id": n["id"], "title": n["title"], "body": n["body"]} for n in notes
        ],
        "notes_omitted": notes_omitted,
        "studio": [
            {
                "kind": o["kind"],
                "body": o["body"],
                "report": _safe_report(o["citation_report"]),
            }
            for o in store.latest_studio_outputs(nb_id)
        ],
        "messages": [
            {
                "role": m["role"],
                "body": m["body"],
                "report": _safe_report(m["citation_report"]),
            }
            for m in recent_msgs
        ],
        "messages_omitted": omitted,
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = f"shoin/{VERSION}"
    sys_version = ""  # keep the Python runtime version out of every Server header

    def setup(self) -> None:
        super().setup()
        self.request.settimeout(REQUEST_SOCKET_SEC)

    llm: ChatBackend  # set by make_server
    db: str
    questions_cache: dict[
        int, tuple[tuple[tuple[int, str, str], ...], list[str]]
    ]  # set by make_server; fingerprint = (source id, sha256, title) per source
    questions_cache_lock: threading.Lock  # guards questions_cache across threads
    generation_lock: threading.Lock  # serializes LLM generation (spec.md STRIDE DoS control)

    # --- plumbing -------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        return

    def _headers(self, status: int, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Nothing here is cacheable: index.html must not outlive the server
        # build serving it (stale JS vs new API), and API responses are
        # live notebook state. Was SSE-only; hoisted to cover every response.
        self.send_header("Cache-Control", "no-store")
        # Every response class (JSON, SSE, static, errors) declares the API
        # contract it speaks — clients can detect a breaking-change boundary
        # without parsing bodies (v0.2.663).
        self.send_header("X-Shoin-API", API_VERSION)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, payload: Json, status: int = 200) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            # Payload fields can carry lone surrogates — a custom ChatBackend's
            # error message or model name, an LLM-derived snippet materialized
            # from a stored citation_report blob — and strict UTF-8 cannot
            # encode them. On the error-envelope path a crash here would leave
            # the request with no HTTP response at all, so emit \ud800 escapes
            # instead; the client's JSON.parse restores them. The compact
            # ensure_ascii=False path stays the fast path for CJK payloads.
            body = json.dumps(payload).encode("ascii")
        self._headers(status, "application/json; charset=utf-8", {"Content-Length": str(len(body))})
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json({"error": {"code": code, "message": message}}, status)

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        # Base's send_error emits a bare HTML page that bypasses _headers() —
        # no nosniff, no no-store, no Referrer-Policy. Route every protocol-level
        # error (bad request line, unimplemented method, oversized headers)
        # through the shared JSON envelope so the baseline headers always apply.
        self.close_connection = True
        try:
            self._error(
                code, f"HTTP_{code}", message or self.responses.get(code, ("Error",))[0]
            )
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _safe_error(self, status: int, code: str, message: str) -> None:
        """_error(), but swallows a dead-connection write failure.

        Used from _dispatch()'s exception handlers, where the original
        exception may itself BE a client disconnect (e.g. a request queued
        behind generation_lock whose client gave up and closed the socket
        before the response could be written). Calling self._error() there
        attempts a second write to the same dead socket, raising the same
        exception class again — this time with nothing left to catch it,
        producing exactly the unhandled traceback the catch-all in
        _dispatch() (v0.2.19) was written to eliminate. There is nothing
        more to do if the client is already gone, so this logs and moves on.
        """
        try:
            self._error(status, code, message)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            print(
                f"Client disconnected before error response could be sent: {exc}",
                file=sys.stderr,
            )

    def _read_json(self) -> Json:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n < 0:
            raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", "invalid Content-Length")
        if n > MAX_UPLOAD_BYTES:
            self._drain(n)  # consume (bounded) so the error response reaches the client
            raise IngestError("INGEST_FILE_TOO_LARGE", "request body too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            # RecursionError: a deeply nested body exceeds json.loads' depth —
            # still a 400 input defect, not a 500.
            raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", f"bad JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", "JSON object required")
        return data

    def _drain(self, n: int) -> None:
        """Discard an oversize request body (bounded) so the error reaches the client.

        Responding and closing while a large undrained body still sits in the
        socket's receive buffer makes the kernel send RST, which can destroy
        the error response before the client reads it — drain bounded bytes
        first. The cap bounds the read work; beyond it we stop draining and
        force a close (already the HTTP/1.0 default, kept as a guard for any
        future protocol_version change).
        """
        remaining = min(n, MAX_UPLOAD_BYTES + 65536)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        if n > MAX_UPLOAD_BYTES + 65536:
            self.close_connection = True

    def _require(self, data: Json, key: str) -> str:
        raw = data.get(key)
        if raw is not None and not isinstance(raw, str):
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"{key} must be a string, got {type(raw).__name__}",
            )
        value = (raw or "").strip()
        if not value:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", f"missing field: {key}")
        _check_utf8(key, value)
        return value

    def _optional_str(self, data: Json, key: str) -> str:
        """Like _require(), but the field may be missing or empty — only its
        TYPE is validated. Without this, a non-string optional field (e.g. a
        JSON list/dict/bool) silently gets Python's str()/repr() coercion
        applied instead of being rejected — the exact type-confusion class
        v0.2.38 fixed for required fields via _require(), left unpatched for
        optional ones.
        """
        raw = data.get(key)
        if raw is not None and not isinstance(raw, str):
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"{key} must be a string, got {type(raw).__name__}",
            )
        value = raw or ""
        _check_utf8(key, value)
        return value

    def _optional_id_list(self, data: Json, key: str) -> list[int] | None:
        """Optional list-of-ids field: absent -> None (unscoped), present ->
        validated positive ints. The _require/_optional_str siblings cover
        strings; a list field needs its own typed reader so handlers never
        touch the raw bound dict directly (the v0.2.408 pin's contract)."""
        raw = data.get(key)
        if raw is None:
            return None
        if not isinstance(raw, list):
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"{key} must be a list, got {type(raw).__name__}",
            )
        out: list[int] = []
        for item in raw:
            if not isinstance(item, int) or isinstance(item, bool) or item < 1:
                raise StoreError(
                    "VALIDATION_FIELD_FORMAT_INVALID",
                    f"{key} must be positive integers",
                )
            if item >= 2**63:
                raise StoreError("VALIDATION_INTEGER_OVERFLOW", "ID out of range")
            out.append(item)
        return out

    def _optional_int(
        self, data: Json, key: str, lo: int, hi: int, default: int
    ) -> int:
        """Optional bounded-int field: absent -> default; present -> an int
        in [lo, hi] or VALIDATION_FIELD_FORMAT_INVALID. The numeric sibling
        of _optional_str/_optional_id_list — a JSON list/dict/bool or an
        out-of-range value must be a coded 400, not a raw comparison or a
        silently unbounded read."""
        value = self._optional_int_or_none(data, key, lo, hi)
        if value is None:
            return default
        return value

    def _optional_int_or_none(
        self, data: Json, key: str, lo: int, hi: int
    ) -> int | None:
        """k-resolution sibling of _optional_int (v0.2.659): absent -> None,
        so the caller can hand the decision to the notebook's own settings
        (nb_search's k — an explicit field still gets the same bounds
        validation)."""
        raw = data.get(key)
        if raw is None:
            return None
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"{key} must be an integer, got {type(raw).__name__}",
            )
        if not lo <= raw <= hi:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"{key} must be in {lo}..{hi}",
            )
        return raw

    def _required_int(self, data: Json, key: str) -> int:
        """Required positive-int field — the numeric sibling of _require:
        absent -> VALIDATION_REQUIRED_FIELD_MISSING; present -> the same
        positive/bounded validation _optional_int applies."""
        if data.get(key) is None:
            raise StoreError(
                "VALIDATION_REQUIRED_FIELD_MISSING", f"missing field: {key}"
            )
        return self._optional_int(data, key, 1, 2**63 - 1, 0)

    def _optional_float(
        self, data: Json, key: str, lo: float, hi: float
    ) -> float | None:
        """Optional bounded-float field: absent -> None; present -> a finite
        float in [lo, hi] or VALIDATION_FIELD_FORMAT_INVALID. The float
        sibling of _optional_int: JSON has one number type so ints are
        accepted, bools/non-numbers are rejected, and a non-finite or
        out-of-range value is a coded 400 — never a raw comparison or a
        silent clamp."""
        raw = data.get(key)
        if raw is None:
            return None
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"{key} must be a number, got {type(raw).__name__}",
            )
        value = float(raw)
        if not math.isfinite(value) or not lo <= value <= hi:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"{key} must be a finite number in {lo}..{hi}",
            )
        return value

    def _optional_json_obj(self, data: Json, key: str) -> dict[str, Any] | None:
        """Optional JSON-object field: absent -> None; present -> the dict
        itself or VALIDATION_FIELD_FORMAT_INVALID. Type-shape only —
        serializability and the byte bound are the store writer's
        contract (update_source_meta), so the same limit governs every
        write path instead of drifting per surface."""
        raw = data.get(key)
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"{key} must be a JSON object, got {type(raw).__name__}",
            )
        return raw

    # --- routing --------------------------------------------------------

    _ROUTES: tuple[tuple[str, str, str], ...] = (
        ("GET", r"^/$", "ui"),
        ("GET", r"^/api/theme\.css$", "theme_css"),
        ("GET", r"^/api/health$", "health"),
        ("GET", r"^/api/metrics$", "metrics"),
        ("GET", r"^/api/trash$", "trash_list"),
        ("POST", r"^/api/trash/(\d+)/restore$", "trash_restore"),
        ("DELETE", r"^/api/trash/(\d+)$", "trash_purge"),
        ("GET", r"^/api/notebooks$", "nb_list"),
        ("POST", r"^/api/notebooks$", "nb_create"),
        ("GET", r"^/api/notebooks/(\d+)$", "nb_get"),
        ("PATCH", r"^/api/notebooks/(\d+)$", "nb_rename"),
        ("DELETE", r"^/api/notebooks/(\d+)$", "nb_delete"),
        ("POST", r"^/api/notebooks/(\d+)/duplicate$", "nb_duplicate"),
        ("POST", r"^/api/notebooks/(\d+)/merge$", "nb_merge"),
        ("POST", r"^/api/notebooks/import$", "nb_import"),
        ("GET", r"^/api/notebooks/(\d+)/messages$", "nb_messages"),
        ("GET", r"^/api/notebooks/(\d+)/notes$", "nb_notes"),
        ("POST", r"^/api/notebooks/(\d+)/sources$", "src_add"),
        ("POST", r"^/api/notebooks/(\d+)/upload$", "src_upload"),
        ("PATCH", r"^/api/sources/(\d+)$", "src_patch"),
        ("DELETE", r"^/api/sources/(\d+)$", "src_delete"),
        ("GET", r"^/api/sources/(\d+)/text$", "src_text"),
        ("PATCH", r"^/api/chunks/(\d+)$", "chunk_patch"),
        ("POST", r"^/api/sources/(\d+)/refresh$", "src_refresh"),
        ("POST", r"^/api/notebooks/(\d+)/refresh-all$", "nb_refresh_all"),
        ("POST", r"^/api/notebooks/(\d+)/ask$", "ask_sse"),
        ("POST", r"^/api/notebooks/(\d+)/search$", "nb_search"),
        ("POST", r"^/api/search$", "global_search"),
        ("POST", r"^/api/notebooks/(\d+)/studio$", "studio"),
        ("GET", r"^/api/notebooks/(\d+)/questions$", "questions"),
        ("POST", r"^/api/notebooks/(\d+)/notes$", "note_add"),
        ("DELETE", r"^/api/notes/(\d+)$", "note_delete"),
        ("DELETE", r"^/api/notebooks/(\d+)/messages$", "nb_clear_chat"),
        ("GET", r"^/api/notebooks/(\d+)/export$", "export"),
        ("POST", r"^/api/notebooks/(\d+)/reindex$", "nb_reindex"),
    )

    def _reject_cross_site(self, method: str) -> bool:
        """DNS-rebinding / CSRF guard. True when the request was rejected.

        The Host header must name this loopback server, and any Origin on a
        state-changing request must be a local one (browsers attach Origin to
        cross-site POSTs even in no-cors mode, so this blocks them).
        """
        host = self.headers.get("Host") or ""
        if _hostname_of(host) not in _ALLOWED_HOSTNAMES:
            # _safe_error, not _error: this runs before _dispatch()'s try/except
            # even begins, so a dead-connection failure here (e.g. a rebinding
            # probe that closed its socket before the response arrived) would
            # otherwise propagate as a raw unhandled traceback — an unguarded
            # single fault, not even the double-fault v0.2.89 fixed downstream.
            self._safe_error(403, "SECURITY_HOST_NOT_ALLOWED", f"unexpected Host: {host!r}")
            return True
        origin = self.headers.get("Origin")
        if origin and method != "GET" and _hostname_of(origin) not in _ALLOWED_HOSTNAMES:
            self._safe_error(403, "SECURITY_CROSS_ORIGIN_BLOCKED", f"cross-site origin: {origin!r}")
            return True
        return False

    def _dispatch(self, method: str) -> None:
        if self._reject_cross_site(method):
            return
        parsed = urllib.parse.urlsplit(self.path)
        self._query = urllib.parse.parse_qs(parsed.query)
        for verb, pattern, name in self._ROUTES:
            if verb != method:
                continue
            m = re.match(pattern, parsed.path)
            if m:
                handler: Callable[..., None] = getattr(self, f"_h_{name}")
                try:
                    args = [int(g) for g in m.groups()]
                    # SQLite integers are 64-bit signed; reject out-of-range IDs before
                    # they reach the driver and raise an uncaught OverflowError.
                    _I64 = 2**63
                    if any(a >= _I64 or a < -_I64 for a in args):
                        raise StoreError("VALIDATION_INTEGER_OVERFLOW", "ID out of range")
                    handler(*args)
                except StoreError as exc:
                    if exc.code.endswith("_NOT_FOUND"):
                        status = 404
                    elif exc.code.endswith("_ALREADY_EXISTS"):
                        status = 409
                    elif exc.code.startswith("SYSTEM_"):
                        status = 500
                    else:
                        status = 400
                    self._safe_error(status, exc.code, str(exc))
                except IngestError as exc:
                    self._safe_error(400, exc.code, str(exc))
                except LLMError as exc:
                    self._safe_error(502, exc.code, str(exc))
                except Exception as exc:
                    print(
                        f"Internal error handling {method} {parsed.path}: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    self._safe_error(500, "SYSTEM_INTERNAL_ERROR", type(exc).__name__)
                return
        path_matched = any(re.match(pattern, parsed.path) for _, pattern, _ in self._ROUTES)
        if path_matched:
            self._error(405, "METHOD_NOT_ALLOWED", f"{method} not allowed on {parsed.path}")
        else:
            self._error(404, "ROUTE_NOT_FOUND", f"no route: {method} {parsed.path}")

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # --- handlers -------------------------------------------------------

    def _h_ui(self) -> None:
        # README documents SHOIN_LANG as controlling "UI言語" without qualifying
        # it to server-side text only, but the Web UI is served as pure static
        # bytes and previously ignored it entirely, deciding its own language
        # from navigator.language/localStorage alone -- the exact "documented
        # but half-true" gap this project keeps finding (v0.2.75/112/129/148/173).
        # Seed the page's default via the single "__SHOIN_LANG__" placeholder in
        # the static file's meta tag, substituted here -- not the user's own
        # explicit in-browser toggle choice (localStorage), which must keep
        # winning once set; see index.html's precedence comment.
        # test_ui_contract.py pins that the placeholder appears exactly once in
        # the shipped file, so a future edit can't silently reintroduce a second
        # occurrence for this blind byte replace to also corrupt.
        # Strictly allowlisted (not merely escaped): CSP already permits inline
        # scripts (script-src 'unsafe-inline'), so an unsanitized value in the
        # replaced attribute could break out of it; only a bare "ja"/"en" is
        # ever substituted, anything else silently falls back to "ja".
        lang = ui_lang()
        safe_lang = lang if lang in ("ja", "en") else "ja"
        body = _STATIC.read_bytes().replace(b"__SHOIN_LANG__", safe_lang.encode("ascii"))
        self._headers(
            200,
            "text/html; charset=utf-8",
            {
                "Content-Length": str(len(body)),
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline' 'self';"
                    " script-src 'unsafe-inline'; connect-src 'self'; img-src data:;"
                    " frame-ancestors 'none'"
                ),
                "X-Frame-Options": "DENY",
            },
        )
        self.wfile.write(body)

    def _h_theme_css(self) -> None:
        # User theme hook: the palette is already :root variables, so a file
        # dropped next to config.json restyles the app without a build.
        # Missing/unreadable/oversized all degrade to an empty stylesheet —
        # a 5xx or truncated tail would only break the optional hook.
        try:
            path = theme_css_path()
            body = b"" if path.stat().st_size > _THEME_CSS_LIMIT else path.read_bytes()
        except OSError:
            body = b""
        self._headers(
            200,
            "text/css; charset=utf-8",
            {"Content-Length": str(len(body))},
        )
        self.wfile.write(body)

    def _h_health(self) -> None:
        avail = getattr(self.llm, "available", lambda: False)()
        model = getattr(self.llm, "model", "")
        embed_model = getattr(self.llm, "embedding_model", "")
        # Surface the stored-vector builder model and the staleness flag so
        # a model swap is diagnosable without reading server stderr
        # (v0.2.661, product-review #17). Health must keep answering even
        # when the DB itself is the broken thing being diagnosed, so the
        # read is best-effort: blank fields then still say "unknown".
        indexed_embed_model = ""
        embed_stale = False
        try:
            with Store(self.db) as store:
                indexed_embed_model = (
                    store.get_setting(EMBED_MODEL_SETTING_KEY) or ""
                ).strip()
                embed_stale = _embed_model_stale(store, embed_model)
        except (OSError, StoreError, sqlite3.OperationalError):
            # sqlite3.connect propagates raw OSErrors for unopenable paths
            # (directory, permission) alongside its own OperationalError.
            pass
        self._json(
            {
                "status": "ok",
                "version": VERSION,
                "api": API_VERSION,
                "llm": avail,
                "model": model,
                "embed_model": embed_model,
                "indexed_embed_model": indexed_embed_model,
                "embed_model_changed": embed_stale,
                # Surfaces the SHOIN_MULTI_QUERY opt-in state (v0.2.126) so a user
                # debugging "why is retrieval slow / why isn't recall improving"
                # doesn't have to know the env var exists — same diagnostic-first
                # spirit as CLAUDE.md's DEBUG=1 retrieval-stats guidance.
                "multi_query": multi_query_enabled(),
            }
        )

    def _h_metrics(self) -> None:
        # Content-free usage counters (counts / millisecond sums written by
        # Store.bump_metrics) — the in-product half of the SHOIN_LOG_JSON
        # observability pair: durable totals vs per-event lines.
        with Store(self.db) as store:
            self._json({"metrics": store.usage_metrics()})

    def _h_trash_list(self) -> None:
        with Store(self.db) as store:
            self._json({"trash": store.trash_list()})

    def _h_trash_restore(self, item_id: int) -> None:
        with Store(self.db) as store:
            nb = store.trash_restore(item_id)
        self._json({"id": nb.id, "name": nb.name}, 201)

    def _h_trash_purge(self, item_id: int) -> None:
        with Store(self.db) as store:
            store.trash_purge(item_id)
        self._json({"purged": item_id})

    def _h_nb_list(self) -> None:
        with Store(self.db) as store:
            self._json({"notebooks": store.list_notebooks_with_counts()})

    def _h_nb_create(self) -> None:
        name = self._require(self._read_json(), "name")
        with Store(self.db) as store:
            nb = store.create_notebook(name)
            self._json({"id": nb.id, "name": nb.name}, 201)

    def _h_nb_get(self, nb_id: int) -> None:
        with Store(self.db) as store:
            self._json(_notebook_json(store, nb_id))

    def _h_nb_rename(self, nb_id: int) -> None:
        # PATCH accepts {name} and/or {settings} (v0.2.659) — either field
        # alone is valid; an empty object is the coded missing-field 400.
        data = self._read_json()
        name = self._optional_str(data, "name") or None
        settings = self._optional_json_obj(data, "settings")
        if name is None and settings is None:
            raise StoreError(
                "VALIDATION_REQUIRED_FIELD_MISSING",
                "missing field: name or settings",
            )
        with Store(self.db) as store:
            nb = store.get_notebook(nb_id)
            if name is not None:
                # Echo the normalized name, not the raw request value — store
                # strips whitespace before persisting, so echoing `name` would
                # report a name the row never had (same response-vs-stored
                # class as v0.2.93's _h_src_patch truncation).
                name = name.strip()
                store.rename_notebook(nb_id, name)
            if settings is not None:
                store.update_notebook_settings(nb_id, settings)
                nb = store.get_notebook(nb_id)
        self._json(
            {
                "id": nb_id,
                "name": name if name is not None else nb.name,
                "settings": nb.settings,
            }
        )

    def _h_nb_delete(self, nb_id: int) -> None:
        with Store(self.db) as store:
            store.delete_notebook(nb_id)
        with self.questions_cache_lock:
            self.questions_cache.pop(nb_id, None)
        self._json({"deleted": nb_id})

    def _h_nb_clear_chat(self, nb_id: int) -> None:
        with Store(self.db) as store:
            store.clear_messages(nb_id)
        self._json({"cleared": nb_id})

    def _h_nb_import(self) -> None:
        with Store(self.db) as store:
            nb = store.import_notebook(self._read_json())
            self._json({"id": nb.id, "name": nb.name}, status=201)

    def _h_nb_duplicate(self, nb_id: int) -> None:
        # Optional {"name": "..."} — absent/empty body forks as "<name> (copy)".
        name = self._optional_str(self._read_json(), "name") or None
        with Store(self.db) as store:
            nb = store.duplicate_notebook(nb_id, name)
            self._json({"id": nb.id, "name": nb.name}, 201)

    def _h_nb_merge(self, nb_id: int) -> None:
        # {"source_id": N} — folds nb N's tree into this one, then archives
        # and deletes N via the trash undo-log (merge is recoverable).
        source_id = self._required_int(self._read_json(), "source_id")
        with Store(self.db) as store:
            nb = store.merge_notebooks(nb_id, source_id)
            self._json({"id": nb.id, "name": nb.name})

    def _q_int(self, key: str, lo: int, hi: int, default: int) -> int:
        """Query-string bounded int: absent -> default; non-numeric or
        out-of-range -> coded 400. The _optional_* siblings read JSON
        bodies; URL params need the same typed boundary so 'limit=abc' is
        a 400, not a ValueError 500 (v0.2.646)."""
        raw_list = self._query.get(key)
        raw = raw_list[0] if raw_list else None
        if raw is None:
            return default
        try:
            val = int(raw, 10)
        except ValueError:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID", f"{key} must be an integer"
            ) from None
        if not lo <= val <= hi:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"{key} must be within {lo}..{hi}",
            )
        return val

    def _h_nb_messages(self, nb_id: int) -> None:
        # Cursor the detail cap doesn't reach: newest-first offset/limit
        # over the full chat history, with total for progress display.
        offset = self._q_int("offset", 0, 2**63 - 1, 0)
        limit = self._q_int("limit", 1, NB_MESSAGES_LIMIT, NB_MESSAGES_LIMIT)
        with Store(self.db) as store:
            store.get_notebook(nb_id)
            self._json(
                {
                    "messages": [
                        {
                            "id": m["id"],
                            "role": m["role"],
                            "body": m["body"],
                            "report": _safe_report(m["citation_report"]),
                            "created_at": m["created_at"],
                        }
                        for m in store.list_messages_page(nb_id, offset, limit)
                    ],
                    "total": store.count_messages(nb_id),
                    "offset": offset,
                    "limit": limit,
                }
            )

    def _h_nb_notes(self, nb_id: int) -> None:
        offset = self._q_int("offset", 0, 2**63 - 1, 0)
        limit = self._q_int("limit", 1, NB_NOTES_LIMIT, NB_NOTES_LIMIT)
        with Store(self.db) as store:
            store.get_notebook(nb_id)
            self._json(
                {
                    "notes": [
                        {
                            "id": n["id"],
                            "title": n["title"],
                            "body": n["body"],
                            "created_at": n["created_at"],
                        }
                        for n in store.list_notes_page(nb_id, offset, limit)
                    ],
                    "total": store.count_notes(nb_id),
                    "offset": offset,
                    "limit": limit,
                }
            )

    def _h_src_add(self, nb_id: int) -> None:
        target = self._require(self._read_json(), "target")
        if not target.startswith(("http://", "https://")):
            # File-path ingestion is CLI-only; the HTTP API must not act as a
            # confused deputy to read arbitrary server-side files.
            raise IngestError(
                "INGEST_UNSUPPORTED_FORMAT", "target must be an http:// or https:// URL"
            )
        with Store(self.db) as store:
            store.get_notebook(nb_id)  # raises NOTEBOOK_NOT_FOUND → 404 before ingesting
            result = index_source(store, nb_id, target, self.llm)
            self._json(
                {
                    "source": {"id": result.source.id, "title": result.source.title},
                    "n_chunks": result.n_chunks,
                    "n_embedded": result.n_embedded,
                    # PDF pages whose text extraction failed — the response
                    # must not present a partial index as a complete one.
                    "pages_failed": result.pages_failed,
                },
                201,
            )

    def _h_src_upload(self, nb_id: int) -> None:
        header_name = self.headers.get("X-Filename") or "upload.txt"
        # http.server/email.parser decode header bytes as Latin-1, not UTF-8. The
        # only client in this repo (index.html) works around this by always
        # percent-encoding via encodeURIComponent() before sending, but that
        # convention is undocumented and unenforced — a client sending raw UTF-8
        # bytes gets silently mis-decoded mojibake that unquote() cannot fix (it
        # only reverses %XX escapes). Recover via a Latin-1->UTF-8 round-trip
        # first; ASCII/percent-encoded values round-trip unchanged, so the
        # existing encodeURIComponent() path is unaffected.
        try:
            header_name = header_name.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        raw_name = (
            Path(urllib.parse.unquote(header_name)).name
            or "upload.txt"
        ).replace("\x00", "").replace("\r", "").replace("\n", "").strip() or "upload.txt"
        suffix = Path(raw_name).suffix.lower() or ".txt"
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise IngestError("INGEST_EMPTY", "invalid Content-Length header") from exc
        if n <= 0:
            raise IngestError("INGEST_EMPTY", "empty upload")
        if n > MAX_UPLOAD_BYTES:
            self._drain(n)
            raise IngestError("INGEST_FILE_TOO_LARGE", "upload exceeds 10MB limit")
        data = self.rfile.read(n)
        # tmp_path is set as soon as the temp file is created — before the write —
        # so the finally always has a valid path to unlink even if write() fails.
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=Path(raw_name).stem[:40] or "upload", suffix=suffix, delete=False
            ) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(data)
            with Store(self.db) as store:
                store.get_notebook(nb_id)  # raises NOTEBOOK_NOT_FOUND → 404 before ingesting
                # Pass the user's original filename as title so add_source commits it
                # in a single transaction — no separate update_source_title needed,
                # which eliminates a TOCTOU window where a concurrent DELETE could
                # leave the source in the DB with the tmp-path as its title.
                result = index_source(store, nb_id, str(tmp_path), self.llm, title=raw_name)
                self._json(
                    {
                        # result.source.title, not raw_name: add_source() silently
                        # truncates to MAX_TITLE_LEN, so a filename over that limit
                        # would otherwise report a title back to the client that
                        # was never actually persisted (matches _h_src_add()'s
                        # existing correct pattern below).
                        "source": {"id": result.source.id, "title": result.source.title},
                        "n_chunks": result.n_chunks,
                        "n_embedded": result.n_embedded,
                        "pages_failed": result.pages_failed,
                    },
                    201,
                )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def _h_src_patch(self, src_id: int) -> None:
        data = self._read_json()
        title = self._optional_str(data, "title") or None
        weight = self._optional_float(data, "weight", 0.0, SOURCE_WEIGHT_MAX)
        meta = self._optional_json_obj(data, "meta")
        if title is None and weight is None and meta is None:
            raise StoreError(
                "VALIDATION_REQUIRED_FIELD_MISSING",
                "missing field: title, weight, or meta",
            )
        with Store(self.db) as store:
            src = store.get_source(src_id)
            if weight is not None:
                store.update_source_weight(src_id, weight)
            if meta is not None:
                store.update_source_meta(src_id, meta)
            if title is not None:
                # rename_source, not store.update_source_title, so the embeddings
                # that bake in the old title are refreshed too (v0.2.160); the
                # rename itself commits regardless of whether embedding succeeds.
                rename_source(store, src_id, title, src.origin, self.llm)
        # Use src_id and title/weight from the request — no second get_source() to
        # avoid a TOCTOU window where a concurrent delete would return HTTP 404
        # despite the update having already committed successfully. But
        # update_source_title() itself silently truncates to MAX_TITLE_LEN before
        # persisting (same class of bug v0.2.93 fixed in _h_src_upload) — apply
        # the identical truncation here so the response matches what was actually
        # written, without a second DB round trip. A weight-only PATCH echoes the
        # pre-read src.title — a racing rename in the same window resolves to
        # whichever committed last, and the DB row (not this echo) is the truth.
        echo_title = (title if title is not None else src.title)[:MAX_TITLE_LEN]
        # A renamed title changes the prompt build_context() sends to the LLM the
        # same way a content refresh does (same fix as _h_src_refresh, v0.2.36):
        # evict stale question suggestions, since the cache key is source IDs only
        # and a rename doesn't change those, so it would never self-expire. A
        # weight/meta change never reaches the generation prompt — no evict.
        if title is not None:
            with self.questions_cache_lock:
                self.questions_cache.pop(src.notebook_id, None)
        self._json(
            {
                "id": src_id,
                "title": echo_title,
                "weight": weight if weight is not None else src.weight,
                "meta": meta if meta is not None else src.meta,
            }
        )

    def _h_src_delete(self, src_id: int) -> None:
        with Store(self.db) as store:
            store.delete_source(src_id)
        self._json({"deleted": src_id})

    def _h_src_refresh(self, src_id: int) -> None:
        with Store(self.db) as store:
            result = refresh_source(store, src_id, self.llm)
        # Source content changed: evict stale question suggestions eagerly. The
        # content-aware fingerprint (id + sha256 + title) would already miss on
        # the next read, but the explicit pop keeps the dict small.
        nb_id = result.source.notebook_id
        with self.questions_cache_lock:
            self.questions_cache.pop(nb_id, None)
        self._json(
            {
                "source": {"id": result.source.id, "title": result.source.title},
                "n_chunks": result.n_chunks,
                "n_embedded": result.n_embedded,
                # Same pages_failed surfacing as add/upload (v0.2.257): a URL
                # source that IS a PDF re-extracts on refresh and can lose
                # pages on the second pass too.
                "pages_failed": result.pages_failed,
            },
            200,
        )

    def _h_nb_refresh_all(self, nb_id: int) -> None:
        with Store(self.db) as store:
            results = refresh_all_sources(store, nb_id, self.llm)
        # Any source's content may have changed: evict cached suggestions
        # (same staleness class as the per-source refresh, v0.2.36).
        with self.questions_cache_lock:
            self.questions_cache.pop(nb_id, None)
        self._json({"results": results})

    def _h_src_text(self, src_id: int) -> None:
        with Store(self.db) as store:
            store.get_source(src_id)  # raises SOURCE_NOT_FOUND → 404 if missing
            # `id` lets the viewer mark which chunks an answer actually cited
            # (citation_report.source_chunk_ids, v0.2.139). Additive field.
            rows = store.id_seq_text_chunks_for_source(src_id)
            self._json(
                {"chunks": [{"id": cid, "seq": seq, "text": text} for cid, seq, text in rows]}
            )

    def _h_chunk_patch(self, chunk_id: int) -> None:
        text = self._require(self._read_json(), "text")
        with Store(self.db) as store:
            chunk = store.update_chunk_text(chunk_id, text)
            # A chunk edit changes retrievable content without moving the
            # source sha256 the questions fingerprint keys on — the cache
            # would never self-expire (same staleness class as a rename,
            # v0.2.36), so evict it here.
            nb_id = store.get_source(chunk.source_id).notebook_id
            with self.questions_cache_lock:
                self.questions_cache.pop(nb_id, None)
        self._json(
            {"id": chunk.id, "source_id": chunk.source_id, "seq": chunk.seq,
             "text": chunk.text}
        )

    def _h_studio(self, nb_id: int) -> None:
        kind = self._require(self._read_json(), "kind")
        if kind not in KINDS:
            raise StoreError("STUDIO_KIND_INVALID", f"kind must be one of {KINDS}")
        with Store(self.db) as store:
            # spec.md STRIDE DoS control: serialize LLM generation (see _h_ask_sse).
            with self.generation_lock:
                result = generate(store, self.llm, nb_id, kind)
            self._json({"kind": result.kind, "body": result.body, "report": dict(result.report)})

    def _h_questions(self, nb_id: int) -> None:
        with Store(self.db) as store:
            store.get_notebook(nb_id)
            # Suggestions change when the source SET or its content changes;
            # cache per notebook so reopening the UI does not re-run the LLM
            # every time. sha256 moves on refresh (same-source-id content
            # rewrite — including `shoin src refresh` from another process,
            # which the per-request fingerprint is the only check that can
            # see) and title feeds the chunk contexts suggest_questions()
            # reads. `shoin reindex` only re-embeds; suggestions read chunk
            # text/context, not vectors, so reindex does not move it.
            fingerprint = tuple(
                (s.id, s.sha256, s.title) for s in store.sources_for_notebook(nb_id)
            )
            with self.questions_cache_lock:
                cached = self.questions_cache.get(nb_id)
            if cached is not None and cached[0] == fingerprint:
                self._json({"questions": cached[1]})
                return
            # spec.md STRIDE DoS control: serialize LLM generation (see _h_ask_sse).
            with self.generation_lock:
                questions = suggest_questions(store, self.llm, nb_id)
            # Cache the result regardless of whether questions is empty.  An LLM
            # failure on an active notebook (non-empty fingerprint) returns [] but
            # NOT caching it causes every subsequent poll to fire a full LLM
            # timeout (up to CHAT_TIMEOUT_SEC=180s), creating a retry storm.
            # "Permanent suppression" is not an issue because the cache is
            # invalidated whenever sources are added, deleted, or refreshed
            # (via questions_cache.pop(nb_id, None)).
            # Guard: only write if no newer fingerprint was stored while the LLM
            # was running (concurrent source-add could otherwise be overwritten).
            with self.questions_cache_lock:
                existing = self.questions_cache.get(nb_id)
                if existing is None or existing[0] == fingerprint:
                    self.questions_cache[nb_id] = (fingerprint, questions)
            self._json({"questions": questions})

    def _h_note_add(self, nb_id: int) -> None:
        data = self._read_json()
        title = self._require(data, "title")
        body = self._optional_str(data, "body")
        with Store(self.db) as store:
            note_id = store.add_note(nb_id, title, body)
            self._json({"id": note_id}, 201)

    def _h_note_delete(self, note_id: int) -> None:
        with Store(self.db) as store:
            store.delete_note(note_id)
        self._json({"deleted": note_id})

    def _h_export(self, nb_id: int) -> None:
        fmt = (self._query.get("format") or ["md"])[0]
        if fmt == "tree":
            # Machine-transfer document (v0.2.655): the trash undo-log's
            # envelope — pairs with POST /api/notebooks/import. Kept OUT
            # of export.FORMATS: that tuple is pinned in lockstep with
            # the mime/ext tables and the UI's download links, none of
            # which apply to a JSON tree (it is a response body, not an
            # attachment users pick from the export menu).
            with Store(self.db) as store:
                self._json(store.export_notebook(nb_id))
            return
        if fmt not in FORMATS:
            raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", f"format must be one of {FORMATS}")
        with Store(self.db) as store:
            text = export(store, nb_id, fmt)
        body = text.encode("utf-8")
        self._headers(
            200,
            _EXPORT_MIME[fmt],
            {
                "Content-Length": str(len(body)),
                "Content-Disposition": (
                    f'attachment; filename="notebook-{nb_id}.{_EXPORT_EXT[fmt]}"'
                ),
            },
        )
        self.wfile.write(body)

    def _h_nb_reindex(self, nb_id: int) -> None:
        """Rebuild embeddings for a notebook. Previously CLI-only (`shoin reindex`);
        a user running only the Web UI had no way to recover from an embedding-model
        change without dropping to a terminal (Plan.md REQ-103: CLI/Web parity)."""
        with Store(self.db) as store:
            n_embedded, n_total = reindex_notebook(store, self.llm, nb_id)
        # Reindex changes every chunk's embedding, so overview_hits() can surface
        # different chunks — cached suggestions were generated against the old
        # retrieval substrate. The fingerprint (source-id tuple) is unchanged by
        # reindex, so without this the stale suggestions would never self-expire
        # (same invalidation gap _h_src_refresh's pop covers for content changes).
        with self.questions_cache_lock:
            self.questions_cache.pop(nb_id, None)
        self._json({"n_embedded": n_embedded, "n_total": n_total})

    # --- SSE ask --------------------------------------------------------

    def _sse(self, event: str, payload: Json) -> None:
        # ensure_ascii=True (the json.dumps default), deliberately: a payload
        # string carrying a lone surrogate — reachable from rows stored before
        # the field gates landed (v0.2.430/v0.2.609), e.g. an old notebook
        # name in a meta frame or a history sentence echoed into a report —
        # is emitted as a \ud800 escape instead of crashing .encode(). The
        # callers only catch ConnectionError, so an encode failure here
        # propagated to _dispatch's 500 writer: a second HTTP status line
        # injected into the already-committed SSE stream body.
        data = json.dumps(payload)
        self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
        self.wfile.flush()

    def _stream_chat(self, messages: list[dict[str, str]]) -> Iterator[str]:
        stream = getattr(self.llm, "chat_stream", None)
        if stream is not None:
            yield from stream(messages)
        else:
            yield self.llm.chat(messages)

    def _h_nb_search(self, nb_id: int) -> None:
        """Retrieval-only sibling of /ask: the same search pipeline, but the
        ranked hits are the response — no answer generated, nothing
        persisted (product-review weakness #25's "just give me the search
        results" route). Stateless: history is NOT folded in, so the query
        is the literal question, not a conversation turn."""
        body = self._read_json()
        question = self._require(body, "question")
        if len(question) > MAX_QUESTION_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"question too long (max {MAX_QUESTION_LEN} characters)",
            )
        # _or_none hands k-resolution to retrieve_for_question: a
        # notebook-level settings.top_k wins over TOP_K when the request
        # body carries no explicit k (v0.2.659).
        k = self._optional_int_or_none(body, "k", lo=1, hi=SEARCH_K_MAX)
        scope_ids = self._optional_id_list(body, "source_ids")
        with Store(self.db) as store:
            store.get_notebook(nb_id)  # 404, same contract as /ask
            for sid in scope_ids or ():
                # Same non-leak rule as /ask: a foreign source id is a dead id.
                if store.get_source(sid).notebook_id != nb_id:
                    raise StoreError("SOURCE_NOT_FOUND", f"source {sid} not found")
            retrieval_q = expand_query(question, [])
            qvec = (
                _query_vector(self.llm, retrieval_q)
                if _check_embed_model_ok(store, self.llm)
                else None
            )
            hits = retrieve_for_question(
                store, self.llm, nb_id, retrieval_q, qvec, k=k, source_ids=scope_ids
            )
            titles = {s.id: s.title for s in store.sources_for_notebook(nb_id)}
            # Zero hits is a dead end (product-review #42): offer the nearest
            # in-corpus spellings so a typo'd query has somewhere to go. Only
            # emitted on the empty path — a non-empty list never needs it.
            suggestions = (
                suggest_corrections(store, nb_id, question) if not hits else []
            )
            self._json(
                {
                    "question": question,
                    "suggestions": suggestions,
                    "hits": [
                        {
                            "rank": i + 1,
                            "chunk_id": h.chunk_id,
                            "source_id": h.source_id,
                            "title": titles.get(h.source_id, ""),
                            "section": h.context,
                            "seq": h.seq,
                            "score": round(h.score, 6),
                            "bm25": round(h.bm25, 6),
                            "vec": round(h.vec, 6),
                            "text": h.text,
                        }
                        for i, h in enumerate(hits)
                    ],
                }
            )

    def _h_global_search(self) -> None:
        """Cross-notebook sibling of /notebooks/{id}/search (v0.2.649,
        product-review #7): the same retrieve_for_question pipeline with
        notebook_id=None — every source in the DB is a candidate and each
        hit carries its notebook identity so the caller can route back to
        the owning notebook. Stateless like nb_search: no history, nothing
        persisted, retrieval only (no answer generated)."""
        body = self._read_json()
        question = self._require(body, "question")
        if len(question) > MAX_QUESTION_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"question too long (max {MAX_QUESTION_LEN} characters)",
            )
        k = self._optional_int(body, "k", lo=1, hi=SEARCH_K_MAX, default=TOP_K)
        with Store(self.db) as store:
            retrieval_q = expand_query(question, [])
            qvec = (
                _query_vector(self.llm, retrieval_q)
                if _check_embed_model_ok(store, self.llm)
                else None
            )
            hits = retrieve_for_question(
                store, self.llm, None, retrieval_q, qvec, k=k
            )
            meta = store.notebooks_for_sources([h.source_id for h in hits])
            # A source deleted by a concurrent request between the search and
            # this provenance lookup is dropped rather than KeyErroring — the
            # same toleration nb_search's titles.get() already applies.
            hits = [h for h in hits if h.source_id in meta]
            suggestions = (
                suggest_corrections(store, None, question) if not hits else []
            )
            self._json(
                {
                    "question": question,
                    "suggestions": suggestions,
                    "hits": [
                        {
                            "rank": i + 1,
                            "chunk_id": h.chunk_id,
                            "notebook_id": meta[h.source_id][0],
                            "notebook": meta[h.source_id][1],
                            "source_id": h.source_id,
                            "title": meta[h.source_id][2],
                            "section": h.context,
                            "seq": h.seq,
                            "score": round(h.score, 6),
                            "bm25": round(h.bm25, 6),
                            "vec": round(h.vec, 6),
                            "text": h.text,
                        }
                        for i, h in enumerate(hits)
                    ],
                }
            )

    def _h_ask_sse(self, nb_id: int) -> None:
        body = self._read_json()
        question = self._require(body, "question")
        if len(question) > MAX_QUESTION_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"question too long (max {MAX_QUESTION_LEN} characters)",
            )
        # Optional per-question scoping: "ask only these sources". Absent or an
        # empty list means the whole notebook (the SQL helper treats both as
        # unscoped). Validation happens BEFORE headers go out so a malformed
        # field is still a 400 envelope, not an SSE error frame mid-stream.
        scope_ids = self._optional_id_list(body, "source_ids")
        with Store(self.db) as store:
            store.get_notebook(nb_id)  # 404 before headers go out
            for sid in scope_ids or ():
                # A foreign source id must 404 exactly like a dead one —
                # answering scoped to another notebook's sources would both
                # leak its existence and silently ground the reply in content
                # the user never attached to this notebook.
                if store.get_source(sid).notebook_id != nb_id:
                    raise StoreError("SOURCE_NOT_FOUND", f"source {sid} not found")
            history = history_messages(store, nb_id)  # before persisting this turn
            retrieval_q = expand_query(question, history)
            qvec = (
                _query_vector(self.llm, retrieval_q)
                if _check_embed_model_ok(store, self.llm)
                else None
            )
            # Single-query retrieve() unless SHOIN_MULTI_QUERY opts in. Neither
            # this call's rewrite LLM request nor the qvec embedding call above
            # it is serialized under generation_lock (spec.md single-generation
            # DoS control) — only the actual answer-generation streaming call
            # below is. See retrieve_for_question()'s own docstring for why.
            hits = retrieve_for_question(
                store, self.llm, nb_id, retrieval_q, qvec, source_ids=scope_ids
            )
            store.add_message(nb_id, "user", question, "{}")

            try:
                self._headers(200, "text/event-stream; charset=utf-8")
            except ConnectionError:
                # Client disconnected before SSE headers could even be sent. Save an
                # empty assistant message so the orphaned user turn (already
                # persisted above) is not visible in list_messages() on page reload —
                # same guard already applied to the meta-send ConnectionError path
                # (v0.2.49) and the build_context exception path (v0.2.39).
                try:
                    store.add_message(nb_id, "assistant", "", json.dumps(make_report("", [])))
                except Exception:
                    pass
                return
            if not hits:
                no_hit = _qa_t("no_hit")
                report = make_report(no_hit, [])
                try:
                    self._sse("meta", {"sources": []})
                    self._sse("delta", {"text": no_hit})
                    self._sse("done", {"report": dict(report), "degraded": False})
                except ConnectionError:
                    pass  # client disconnected; still persist the assistant message below
                try:
                    store.add_message(nb_id, "assistant", no_hit, json.dumps(report))
                except Exception:
                    pass  # post-SSE persist: notebook deleted or DB error; stream already clean
                return

            try:
                # Per-notebook retrieval budget override (v0.2.659) — the
                # same knob qa.ask() applies on its own build_context call.
                nb_budget = int(
                    store.notebook_settings(nb_id).get(
                        "source_text_tokens", _QA_SOURCE_TEXT_TOKENS
                    )
                )
                context = build_context(store, hits, budget_tokens=nb_budget)
            except Exception as exc:
                # Headers already committed; must not let this propagate to _dispatch
                # (it would write a new HTTP status line into the SSE body stream).
                # Message policy mirrors _dispatch: coded errors carry their
                # curated (code, message); anything else leaks only the type
                # name — str(exc) can carry internals (SQL text, paths).
                # Full detail still goes to stderr.
                print(
                    f"build_context failed mid-SSE: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                try:
                    if isinstance(exc, (StoreError, IngestError, LLMError)):
                        self._sse("error", {"code": exc.code, "message": str(exc)})
                    else:
                        self._sse(
                            "error",
                            {"code": "SYSTEM_INTERNAL_ERROR", "message": type(exc).__name__},
                        )
                except ConnectionError:
                    pass
                # Prevent dangling user turn: save an empty assistant message so
                # history_messages() sees a complete pair instead of an orphaned user turn.
                try:
                    store.add_message(nb_id, "assistant", "", json.dumps(make_report("", [])))
                except Exception:
                    pass
                return

            try:
                self._sse(
                    "meta",
                    {
                        "sources": [
                            {"s": i + 1, "title": t, "source_id": sid}
                            for i, (t, sid) in enumerate(
                                zip(context.source_titles, context.source_ids, strict=True)
                            )
                        ]
                    },
                )
            except ConnectionError:
                # Client disconnected before any SSE was sent.  Save an empty assistant
                # message so the orphaned user turn is not visible in list_messages() on
                # page reload — matching the build_context exception path (v0.2.39).
                try:
                    store.add_message(nb_id, "assistant", "", json.dumps(make_report("", [])))
                except Exception:
                    pass
                return

            parts: list[str] = []
            degraded = False
            truncated = False
            client_gone = False
            try:
                # spec.md STRIDE DoS control: serialize actual LLM generation so
                # concurrent requests queue rather than multiply memory/compute load
                # on the lightweight local endpoint. Retrieval/context-building above
                # is not serialized; only the token-generation call is.
                with self.generation_lock:
                    for token in self._stream_chat(build_messages(question, context, history)):
                        parts.append(token)
                        if client_gone:
                            continue
                        try:
                            self._sse("delta", {"text": token})
                        except ConnectionError:
                            # Client dropped mid-answer — keep consuming the
                            # generator to completion anyway: the token spend
                            # is already sunk inside the serialized lock, and
                            # the persist below then writes the COMPLETE
                            # answer, so the client's done-miss recovery and
                            # any page reload see the full answer rather than
                            # the prefix that happened to fit before the cut
                            # (v0.2.665).
                            client_gone = True
                    # Read last_finish_reason while still holding the lock — the
                    # shared llm resets it at the start of every chat/stream call,
                    # so reading after release races with the next queued request.
                    truncated = getattr(self.llm, "last_finish_reason", None) == "length"
            except LLMError:
                degraded = True
                text = _degraded_text(hits)
                parts.append(text)
                try:
                    self._sse("delta", {"text": text})
                except ConnectionError:
                    client_gone = True
            except ConnectionError:
                client_gone = True
            except Exception as exc:
                # Any other mid-stream failure — a socket timeout (TimeoutError
                # is NOT a ConnectionError), a surrogate token that fails the
                # SSE UTF-8 encode, an unexpected backend error type — must not
                # propagate to _dispatch(): the SSE headers are already
                # committed, so its 500 write would inject a second status
                # line into the stream body, and unwinding would skip the
                # persist below, orphaning the user turn exactly like the
                # disconnect paths above are built to prevent.
                print(
                    f"stream failed mid-SSE: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                try:
                    if isinstance(exc, (StoreError, IngestError)):
                        self._sse("error", {"code": exc.code, "message": str(exc)})
                    else:
                        self._sse(
                            "error",
                            {"code": "SYSTEM_INTERNAL_ERROR", "message": type(exc).__name__},
                        )
                except Exception:
                    # The error frame couldn't reach the client either —
                    # nothing more to send; the persist below still runs.
                    client_gone = True
            full = "".join(parts)
            report = make_report(
                full,
                context.source_titles,
                context.source_ids,
                context.source_bodies,
                context.source_contexts,
                context.source_chunk_ids,
                context.source_detail,
                check_uncited=not degraded,
                # Same history join qa.ask() passes — without it the
                # cross-turn checks (degenerate_spans/self_contradictions)
                # silently never fire on the streamed path.
                history="\n".join(
                    m["content"] for m in history if m["role"] == "assistant"
                ),
            )
            if degraded:
                report["degraded"] = True
            # finish_reason "length" = the stream ended at MAX_TOKENS — surface
            # it like degraded so a clipped answer is not shown as complete.
            if truncated:
                report["truncated"] = True
            if not client_gone:
                try:
                    self._sse("done", {"report": dict(report), "degraded": degraded})
                except ConnectionError:
                    pass
            # Always persist the assistant message — even when full="" (zero-token LLM
            # response, e.g. reasoning models that emit no content, or client disconnect
            # before any tokens).  An empty assistant message is preferable to leaving
            # the user turn orphaned: history_messages() would silently drop lone user
            # turns, but list_messages() (page-reload) shows them as unanswered questions.
            # This matches the meta-send disconnect path (lines above) and the
            # build_context error path that both persist empty assistant messages.
            try:
                store.add_message(nb_id, "assistant", full, json.dumps(report))
            except Exception:
                pass  # post-SSE persist: notebook deleted or DB error; stream already clean


def make_server(
    host: str = "127.0.0.1",
    port: int = 0,
    db: str | None = None,
    llm: ChatBackend | None = None,
) -> ThreadingHTTPServer:
    """Build a configured server. host is pinned to loopback by design."""
    if not (host.startswith("127.") or host == "::1"):
        raise ValueError("Shoin binds to loopback only (privacy by design)")
    handler = type(
        "ShoinHandler",
        (_Handler,),
        {
            "llm": llm if llm is not None else LLMClient(),
            "db": db or str(db_path()),
            "questions_cache": {},
            "questions_cache_lock": threading.Lock(),
            "generation_lock": threading.Lock(),
        },
    )
    return _HTTPServer((host, port), handler)


class _HTTPServer(ThreadingHTTPServer):
    # Handler threads are daemons: a client that stalls mid-request (partial
    # request line, abandoned connection) parks its handler in rfile.read() for
    # up to REQUEST_SOCKET_SEC, and non-daemon threads are JOINED by
    # server_close() — Ctrl+C would stall for the full socket timeout whenever
    # any request is still in flight. Local single-user server semantics make a
    # dead-at-exit thread strictly correct (the same choice python -m
    # http.server makes).
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that stalls mid-request simply hits the per-request socket
        # timeout and gets closed — not an error worth a traceback. Everything
        # else keeps the default (print to stderr).
        if isinstance(sys.exc_info()[1], TimeoutError):
            return
        super().handle_error(request, client_address)


def serve(port: int, db: str | None = None) -> None:  # pragma: no cover (blocking loop)
    server = make_server(port=port, db=db)
    actual = server.server_address[1]
    print(f"Shoin (書院) v{VERSION} — http://127.0.0.1:{actual}/")
    print(_t("serve.no_egress"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n" + _t("serve.stopped"))
    finally:
        server.server_close()
