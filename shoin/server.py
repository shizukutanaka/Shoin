"""Shoin web server: stdlib HTTP, bound to 127.0.0.1 only (spec STRIDE).

Single-user local app. Each request opens its own Store (SQLite/WAL), the LLM
backend is shared and injectable for tests. `ask` streams over SSE; everything
else is plain JSON. No path-based static serving: only the embedded index.html.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .citation import make_report
from .config import MAX_UPLOAD_BYTES, VERSION, db_path
from .export import FORMATS, export
from .ingest import IngestError
from .llm import LLMClient, LLMError
from .pipeline import index_source
from .qa import (
    ChatBackend,
    _degraded_text,
    _query_vector,
    _t as _qa_t,
    build_context,
    build_messages,
    expand_query,
    history_messages,
)
from .search import retrieve
from .store import Store, StoreError
from .studio import KINDS, generate, suggest_questions

_STATIC = Path(__file__).resolve().parent / "static" / "index.html"

_EXPORT_MIME = {
    "md": "text/markdown; charset=utf-8",
    "bibtex": "application/x-bibtex; charset=utf-8",
    "ris": "application/x-research-info-systems; charset=utf-8",
}

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
    try:
        return json.loads(str(raw) or "{}") or {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _notebook_json(store: Store, nb_id: int) -> Json:
    nb = store.get_notebook(nb_id)
    return {
        "id": nb.id,
        "name": nb.name,
        "counts": store.counts(nb_id),
        "sources": [
            {"id": s.id, "kind": s.kind, "title": s.title, "origin": s.origin}
            for s in store.sources_for_notebook(nb_id)
        ],
        "notes": [
            {"id": n["id"], "title": n["title"], "body": n["body"]} for n in store.list_notes(nb_id)
        ],
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
            for m in store.list_messages(nb_id)
        ],
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = f"shoin/{VERSION}"
    llm: ChatBackend  # set by make_server
    db: str
    questions_cache: dict[int, tuple[tuple[int, ...], list[str]]]  # set by make_server
    questions_cache_lock: threading.Lock  # guards questions_cache across threads

    # --- plumbing -------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        return

    def _headers(self, status: int, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, payload: Json, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", {"Content-Length": str(len(body))})
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json({"error": {"code": code, "message": message}}, status)

    def _read_json(self) -> Json:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_UPLOAD_BYTES:
            self._drain(n)  # consume (bounded) so the error response reaches the client
            raise IngestError("INGEST_FILE_TOO_LARGE", "request body too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", f"bad JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", "JSON object required")
        return data

    def _drain(self, n: int) -> None:
        """Discard an oversize request body (bounded) so the error reaches the client."""
        remaining = min(n, MAX_UPLOAD_BYTES + 65536)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _require(self, data: Json, key: str) -> str:
        value = str(data.get(key) or "").strip()
        if not value:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", f"missing field: {key}")
        return value

    # --- routing --------------------------------------------------------

    _ROUTES: tuple[tuple[str, str, str], ...] = (
        ("GET", r"^/$", "ui"),
        ("GET", r"^/api/health$", "health"),
        ("GET", r"^/api/notebooks$", "nb_list"),
        ("POST", r"^/api/notebooks$", "nb_create"),
        ("GET", r"^/api/notebooks/(\d+)$", "nb_get"),
        ("PATCH", r"^/api/notebooks/(\d+)$", "nb_rename"),
        ("DELETE", r"^/api/notebooks/(\d+)$", "nb_delete"),
        ("POST", r"^/api/notebooks/(\d+)/sources$", "src_add"),
        ("POST", r"^/api/notebooks/(\d+)/upload$", "src_upload"),
        ("DELETE", r"^/api/sources/(\d+)$", "src_delete"),
        ("GET", r"^/api/sources/(\d+)/text$", "src_text"),
        ("POST", r"^/api/notebooks/(\d+)/ask$", "ask_sse"),
        ("POST", r"^/api/notebooks/(\d+)/studio$", "studio"),
        ("GET", r"^/api/notebooks/(\d+)/questions$", "questions"),
        ("POST", r"^/api/notebooks/(\d+)/notes$", "note_add"),
        ("DELETE", r"^/api/notes/(\d+)$", "note_delete"),
        ("DELETE", r"^/api/notebooks/(\d+)/messages$", "nb_clear_chat"),
        ("GET", r"^/api/notebooks/(\d+)/export$", "export"),
    )

    def _reject_cross_site(self, method: str) -> bool:
        """DNS-rebinding / CSRF guard. True when the request was rejected.

        The Host header must name this loopback server, and any Origin on a
        state-changing request must be a local one (browsers attach Origin to
        cross-site POSTs even in no-cors mode, so this blocks them).
        """
        host = self.headers.get("Host") or ""
        if _hostname_of(host) not in _ALLOWED_HOSTNAMES:
            self._error(403, "SECURITY_HOST_NOT_ALLOWED", f"unexpected Host: {host!r}")
            return True
        origin = self.headers.get("Origin")
        if origin and method != "GET" and _hostname_of(origin) not in _ALLOWED_HOSTNAMES:
            self._error(403, "SECURITY_CROSS_ORIGIN_BLOCKED", f"cross-site origin: {origin!r}")
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
                    handler(*[int(g) for g in m.groups()])
                except StoreError as exc:
                    status = 404 if exc.code.endswith("_NOT_FOUND") else 400
                    self._error(status, exc.code, str(exc))
                except IngestError as exc:
                    self._error(400, exc.code, str(exc))
                except LLMError as exc:
                    self._error(502, exc.code, str(exc))
                return
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
        body = _STATIC.read_bytes()
        self._headers(
            200,
            "text/html; charset=utf-8",
            {
                "Content-Length": str(len(body)),
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline';"
                    " script-src 'unsafe-inline'; connect-src 'self'; img-src data:;"
                    " frame-ancestors 'none'"
                ),
                "X-Frame-Options": "DENY",
            },
        )
        self.wfile.write(body)

    def _h_health(self) -> None:
        avail = getattr(self.llm, "available", lambda: False)()
        model = getattr(self.llm, "model", "")
        embed_model = getattr(self.llm, "embedding_model", "")
        self._json(
            {
                "status": "ok",
                "version": VERSION,
                "llm": avail,
                "model": model,
                "embed_model": embed_model,
            }
        )

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
        name = self._require(self._read_json(), "name")
        with Store(self.db) as store:
            store.rename_notebook(nb_id, name)
        self._json({"id": nb_id, "name": name})

    def _h_nb_delete(self, nb_id: int) -> None:
        with Store(self.db) as store:
            store.delete_notebook(nb_id)
        with self.questions_cache_lock:
            self.questions_cache.pop(nb_id, None)
        self._json({"deleted": nb_id})

    def _h_nb_clear_chat(self, nb_id: int) -> None:
        with Store(self.db) as store:
            store.clear_messages(nb_id)
        with self.questions_cache_lock:
            self.questions_cache.pop(nb_id, None)
        self._json({"cleared": nb_id})

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
                },
                201,
            )

    def _h_src_upload(self, nb_id: int) -> None:
        raw_name = (
            Path(urllib.parse.unquote(self.headers.get("X-Filename") or "upload.txt")).name
            or "upload.txt"
        )
        suffix = Path(raw_name).suffix.lower() or ".txt"
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise IngestError("INGEST_EMPTY", "invalid Content-Length header")
        if n <= 0:
            raise IngestError("INGEST_EMPTY", "empty upload")
        if n > MAX_UPLOAD_BYTES:
            self._drain(n)
            raise IngestError("INGEST_FILE_TOO_LARGE", "upload exceeds 10MB limit")
        data = self.rfile.read(n)
        with tempfile.NamedTemporaryFile(
            prefix=Path(raw_name).stem[:40] or "upload", suffix=suffix, delete=False
        ) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            with Store(self.db) as store:
                store.get_notebook(nb_id)  # raises NOTEBOOK_NOT_FOUND → 404 before ingesting
                result = index_source(store, nb_id, str(tmp_path), self.llm)
                # keep the user's filename, not the temp path
                store.update_source_title(result.source.id, raw_name, raw_name)
                self._json(
                    {
                        "source": {"id": result.source.id, "title": raw_name},
                        "n_chunks": result.n_chunks,
                        "n_embedded": result.n_embedded,
                    },
                    201,
                )
        finally:
            tmp_path.unlink(missing_ok=True)

    def _h_src_delete(self, src_id: int) -> None:
        with Store(self.db) as store:
            store.delete_source(src_id)
        self._json({"deleted": src_id})

    def _h_src_text(self, src_id: int) -> None:
        with Store(self.db) as store:
            store.get_source(src_id)  # raises SOURCE_NOT_FOUND → 404 if missing
            rows = store.conn.execute(
                "SELECT seq, text FROM chunks WHERE source_id=? ORDER BY seq", (src_id,)
            ).fetchall()
            self._json({"chunks": [{"seq": r["seq"], "text": r["text"]} for r in rows]})

    def _h_studio(self, nb_id: int) -> None:
        kind = self._require(self._read_json(), "kind")
        if kind not in KINDS:
            raise StoreError("STUDIO_KIND_INVALID", f"kind must be one of {KINDS}")
        with Store(self.db) as store:
            result = generate(store, self.llm, nb_id, kind)
            self._json({"kind": result.kind, "body": result.body, "report": dict(result.report)})

    def _h_questions(self, nb_id: int) -> None:
        with Store(self.db) as store:
            store.get_notebook(nb_id)
            # Suggestions only change when the source set changes; cache per
            # notebook so reopening the UI does not re-run the LLM every time.
            fingerprint = tuple(s.id for s in store.sources_for_notebook(nb_id))
            with self.questions_cache_lock:
                cached = self.questions_cache.get(nb_id)
            if cached is not None and cached[0] == fingerprint:
                self._json({"questions": cached[1]})
                return
            questions = suggest_questions(store, self.llm, nb_id)
            # Only cache non-empty results when sources exist; an empty list
            # from LLM failure would otherwise suppress questions permanently.
            if questions or not fingerprint:
                with self.questions_cache_lock:
                    self.questions_cache[nb_id] = (fingerprint, questions)
            self._json({"questions": questions})

    def _h_note_add(self, nb_id: int) -> None:
        data = self._read_json()
        title = self._require(data, "title")
        body = str(data.get("body") or "")
        with Store(self.db) as store:
            note_id = store.add_note(nb_id, title, body)
            self._json({"id": note_id}, 201)

    def _h_note_delete(self, note_id: int) -> None:
        with Store(self.db) as store:
            store.delete_note(note_id)
        self._json({"deleted": note_id})

    def _h_export(self, nb_id: int) -> None:
        fmt = (self._query.get("format") or ["md"])[0]
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
                "Content-Disposition": f'attachment; filename="notebook-{nb_id}.{fmt}"',
            },
        )
        self.wfile.write(body)

    # --- SSE ask --------------------------------------------------------

    def _sse(self, event: str, payload: Json) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
        self.wfile.flush()

    def _stream_chat(self, messages: list[dict[str, str]]) -> Iterator[str]:
        stream = getattr(self.llm, "chat_stream", None)
        if stream is not None:
            yield from stream(messages)
        else:
            yield self.llm.chat(messages)

    def _h_ask_sse(self, nb_id: int) -> None:
        question = self._require(self._read_json(), "question")
        with Store(self.db) as store:
            store.get_notebook(nb_id)  # 404 before headers go out
            history = history_messages(store, nb_id)  # before persisting this turn
            retrieval_q = expand_query(question, history)
            qvec = _query_vector(self.llm, retrieval_q)
            hits = retrieve(store, nb_id, retrieval_q, query_vec=qvec)
            store.add_message(nb_id, "user", question, "{}")

            self._headers(200, "text/event-stream; charset=utf-8", {"Cache-Control": "no-store"})
            if not hits:
                no_hit = _qa_t("no_hit")
                report = make_report(no_hit, [])
                try:
                    self._sse("meta", {"sources": []})
                    self._sse("delta", {"text": no_hit})
                    self._sse("done", {"report": dict(report), "degraded": False})
                except ConnectionError:
                    pass  # client disconnected; still persist the assistant message below
                store.add_message(nb_id, "assistant", no_hit, json.dumps(report))
                return

            context = build_context(store, hits)
            try:
                self._sse(
                    "meta",
                    {
                        "sources": [
                            {"s": i + 1, "title": t, "source_id": sid}
                            for i, (t, sid) in enumerate(
                                zip(context.source_titles, context.source_ids)
                            )
                        ]
                    },
                )
            except ConnectionError:
                store.add_message(nb_id, "assistant", "", "{}")
                return

            parts: list[str] = []
            degraded = False
            client_gone = False
            try:
                for token in self._stream_chat(build_messages(question, context, history)):
                    parts.append(token)
                    self._sse("delta", {"text": token})
            except LLMError:
                degraded = True
                text = _degraded_text(hits)
                parts = [text]
                try:
                    self._sse("delta", {"text": text})
                except ConnectionError:
                    client_gone = True
            except ConnectionError:
                client_gone = True
            full = "".join(parts)
            report = make_report(
                full, context.source_titles, context.source_ids, context.source_bodies
            )
            if not client_gone:
                try:
                    self._sse("done", {"report": dict(report), "degraded": degraded})
                except ConnectionError:
                    pass
            store.add_message(nb_id, "assistant", full, json.dumps(report))


def make_server(
    host: str = "127.0.0.1",
    port: int = 0,
    db: str | None = None,
    llm: ChatBackend | None = None,
) -> ThreadingHTTPServer:
    """Build a configured server. host is pinned to loopback by design."""
    if not host.startswith("127."):
        raise ValueError("Shoin binds to loopback only (privacy by design)")
    handler = type(
        "ShoinHandler",
        (_Handler,),
        {
            "llm": llm if llm is not None else LLMClient(),
            "db": db or str(db_path()),
            "questions_cache": {},
            "questions_cache_lock": threading.Lock(),
        },
    )
    return ThreadingHTTPServer((host, port), handler)


def serve(port: int, db: str | None = None) -> None:  # pragma: no cover (blocking loop)
    server = make_server(port=port, db=db)
    actual = server.server_address[1]
    print(f"Shoin (書院) v{VERSION} — http://127.0.0.1:{actual}/")
    print("外部送信なし。Ctrl+C で終了。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。")
    finally:
        server.server_close()
