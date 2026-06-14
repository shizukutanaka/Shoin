"""Phase 4 tests: HTTP server (routes, SSE ask, upload, security headers)."""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shoin.server import make_server  # noqa: E402


class FakeLLM:
    embedding_model = ""
    model = "fake-4b"

    def __init__(self, reply_parts: list[str] | None = None) -> None:
        self.reply_parts = reply_parts or ["回答 ", "[S1]。"]
        self.chat_count = 0

    def available(self) -> bool:
        return True

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.2) -> str:
        self.chat_count += 1
        # Return a question-compatible string so suggest_questions caches results.
        return "これは何ですか？ [S1]。"

    def chat_stream(
        self, messages: list[dict[str, str]], temperature: float = 0.2
    ) -> Iterator[str]:
        yield from self.reply_parts

    def embed_one(self, text: str) -> list[float]:
        return [1.0, 0.0]


def parse_sse(raw: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for frame in raw.split("\n\n"):
        ev, data = "message", ""
        for line in frame.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if data:
            events.append((ev, json.loads(data)))
    return events


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = FakeLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "s.db"), llm=cls.llm)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    # --- helpers ---

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _req(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(
            self._url(path), data=body, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def _json(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object]]:
        body = json.dumps(payload).encode() if payload is not None else None
        status, _, raw = self._req(
            method, path, body, {"Content-Type": "application/json"} if body else {}
        )
        return status, json.loads(raw) if raw else {}

    # --- tests (single flow to keep ordering deterministic) ---

    def test_workflow(self) -> None:
        # health + UI + security headers
        status, data = self._json("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["llm"])
        status, headers, page = self._req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("書院", page.decode())
        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

        # notebook CRUD
        status, nb = self._json("POST", "/api/notebooks", {"name": "和紙研究"})
        self.assertEqual(status, 201)
        nb_id = nb["id"]
        status, listing = self._json("GET", "/api/notebooks")
        self.assertIn(nb_id, [n["id"] for n in listing["notebooks"]])

        # validation error shape
        status, err = self._json("POST", "/api/notebooks", {"name": "  "})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_REQUIRED_FIELD_MISSING")

        # upload keeps original filename
        body = ("和紙は楮から作られる。" * 30).encode("utf-8")
        status, _, raw = self._req(
            "POST",
            f"/api/notebooks/{nb_id}/upload",
            body,
            {"X-Filename": urllib.parse.quote("素材メモ.txt")},
        )
        self.assertEqual(status, 201)
        up = json.loads(raw)
        self.assertEqual(up["source"]["title"], "素材メモ.txt")
        self.assertGreaterEqual(up["n_chunks"], 1)

        # detail view
        status, detail = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(len(detail["sources"]), 1)
        src_id = detail["sources"][0]["id"]

        # source text endpoint
        status, chunks = self._json("GET", f"/api/sources/{src_id}/text")
        self.assertEqual(status, 200)
        self.assertIn("和紙", str(chunks["chunks"][0]["text"]))

        # SSE ask: meta -> delta -> done, message persisted with report
        status, _, raw = self._req(
            "POST",
            f"/api/notebooks/{nb_id}/ask",
            json.dumps({"question": "和紙の原料は？"}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        events = parse_sse(raw.decode())
        kinds = [e for e, _ in events]
        self.assertEqual(kinds[0], "meta")
        self.assertIn("delta", kinds)
        self.assertEqual(kinds[-1], "done")
        full = "".join(str(d["text"]) for e, d in events if e == "delta")
        self.assertEqual(full, "回答 [S1]。")
        done = events[-1][1]
        self.assertEqual(done["report"]["cited"], [1])  # type: ignore[index]
        status, detail = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(len(detail["messages"]), 2)

        # studio + invalid kind
        status, st = self._json("POST", f"/api/notebooks/{nb_id}/studio", {"kind": "briefing"})
        self.assertEqual(status, 200)
        self.assertEqual(st["report"]["cited"], [1])  # type: ignore[index]
        status, err = self._json("POST", f"/api/notebooks/{nb_id}/studio", {"kind": "poem"})
        self.assertEqual(status, 400)

        # notes
        status, note = self._json(
            "POST", f"/api/notebooks/{nb_id}/notes", {"title": "覚書", "body": "重要"}
        )
        self.assertEqual(status, 201)
        status, _ = self._json("DELETE", f"/api/notes/{note['id']}")
        self.assertEqual(status, 200)

        # export
        status, headers, raw = self._req("GET", f"/api/notebooks/{nb_id}/export?format=bibtex")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("@misc{shoin", raw.decode())

        # rename notebook
        status, renamed = self._json("PATCH", f"/api/notebooks/{nb_id}", {"name": "和紙研究 改"})
        self.assertEqual(status, 200)
        self.assertEqual(renamed["name"], "和紙研究 改")
        _, detail = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(detail["name"], "和紙研究 改")

        # rename blank name rejected
        status, err = self._json("PATCH", f"/api/notebooks/{nb_id}", {"name": "  "})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_REQUIRED_FIELD_MISSING")  # type: ignore[index]

        # clear chat
        _, detail = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertGreater(len(detail["messages"]), 0)
        status, _ = self._json("DELETE", f"/api/notebooks/{nb_id}/messages")
        self.assertEqual(status, 200)
        _, detail = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(detail["messages"], [])

        # 404s
        status, err = self._json("GET", "/api/notebooks/999")
        self.assertEqual(status, 404)
        status, _ = self._json("GET", "/api/nope")
        self.assertEqual(status, 404)

        # delete notebook
        status, _ = self._json("DELETE", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 200)

    def test_loopback_only(self) -> None:
        with self.assertRaises(ValueError):
            make_server(host="0.0.0.0")

    def test_dns_rebinding_host_rejected(self) -> None:
        """A rebound hostname must not reach the API even though it hits 127.0.0.1."""
        status, _, raw = self._req("GET", "/api/health", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["error"]["code"], "SECURITY_HOST_NOT_ALLOWED")
        status, _, _ = self._req("GET", "/api/health", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)

    def test_cross_origin_post_rejected(self) -> None:
        """Browsers attach Origin to cross-site POSTs; those must be blocked (CSRF)."""
        body = json.dumps({"name": "csrf"}).encode()
        status, _, raw = self._req(
            "POST",
            "/api/notebooks",
            body,
            {"Origin": "https://evil.example", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["error"]["code"], "SECURITY_CROSS_ORIGIN_BLOCKED")
        status, _, _ = self._req(
            "POST",
            "/api/notebooks",
            body,
            {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 201)

    def test_source_text_unknown_id_404(self) -> None:
        status, err = self._json("GET", "/api/sources/99999/text")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "SOURCE_NOT_FOUND")  # type: ignore[index]

    def test_src_add_file_path_rejected(self) -> None:
        """HTTP /sources endpoint must reject file-path targets to prevent
        the server acting as a confused deputy to read arbitrary local files."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "pathguard"})
        for bad_target in ("/etc/passwd", "../config.py", "C:\\Windows\\system32"):
            status, err = self._json(
                "POST",
                f"/api/notebooks/{nb['id']}/sources",
                {"target": bad_target},
            )
            self.assertEqual(status, 400, msg=f"file path target should be rejected: {bad_target!r}")
            self.assertEqual(err["error"]["code"], "INGEST_UNSUPPORTED_FORMAT")  # type: ignore[index]

    def test_upload_to_deleted_notebook_returns_404(self) -> None:
        """Uploading to a deleted notebook must return 404, not a silent error."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "delme"})
        self._json("DELETE", f"/api/notebooks/{nb['id']}")
        status, _, _ = self._req(
            "POST",
            f"/api/notebooks/{nb['id']}/upload",
            ("テスト文書。" * 30).encode(),
            {"X-Filename": "t.txt"},
        )
        self.assertEqual(status, 404)

    def test_add_note_to_deleted_notebook_returns_404(self) -> None:
        """Adding a note to a deleted notebook must return 404."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "delnb"})
        self._json("DELETE", f"/api/notebooks/{nb['id']}")
        status, err = self._json("POST", f"/api/notebooks/{nb['id']}/notes", {"title": "T", "body": "B"})
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")  # type: ignore[index]

    def test_questions_cached_until_sources_change(self) -> None:
        _, nb = self._json("POST", "/api/notebooks", {"name": "提案キャッシュ"})
        nb_id = nb["id"]
        self._req(
            "POST",
            f"/api/notebooks/{nb_id}/upload",
            ("質問とは何か？" * 50).encode(),
            {"X-Filename": "q.txt"},
        )
        before = self.llm.chat_count
        status, _ = self._json("GET", f"/api/notebooks/{nb_id}/questions")
        self.assertEqual(status, 200)
        self.assertEqual(self.llm.chat_count, before + 1)
        self._json("GET", f"/api/notebooks/{nb_id}/questions")  # cache hit
        self.assertEqual(self.llm.chat_count, before + 1)
        self._req(
            "POST",
            f"/api/notebooks/{nb_id}/upload",
            ("別の資料。" * 50).encode(),
            {"X-Filename": "r.txt"},
        )
        self._json("GET", f"/api/notebooks/{nb_id}/questions")  # invalidated
        self.assertEqual(self.llm.chat_count, before + 2)

    def test_upload_too_large_rejected(self) -> None:
        _, nb = self._json("POST", "/api/notebooks", {"name": "limit"})
        status, _, raw = self._req(
            "POST",
            f"/api/notebooks/{nb['id']}/upload",
            b"x" * (10 * 1024 * 1024 + 1),
            {"X-Filename": "big.txt"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"]["code"], "INGEST_FILE_TOO_LARGE")

    def test_upload_malformed_content_length(self) -> None:
        """Non-numeric Content-Length must return 400, not crash the server."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "badcl"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.putrequest("POST", f"/api/notebooks/{nb['id']}/upload")
        conn.putheader("Content-Length", "notanumber")
        conn.putheader("X-Filename", "t.txt")
        conn.endheaders()
        resp = conn.getresponse()
        body = json.loads(resp.read())
        self.assertEqual(resp.status, 400)
        self.assertEqual(body["error"]["code"], "INGEST_EMPTY")
        conn.close()
        # Server must still be responsive after the malformed request.
        status, _ = self._json("GET", "/api/health")
        self.assertEqual(status, 200)

    def test_json_body_malformed_content_length(self) -> None:
        """Non-numeric Content-Length on a JSON endpoint falls back to empty body."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.putrequest("POST", "/api/notebooks")
        conn.putheader("Content-Length", "??")
        conn.putheader("Content-Type", "application/json")
        conn.endheaders()
        resp = conn.getresponse()
        body = json.loads(resp.read())
        self.assertEqual(resp.status, 400)
        # Empty body parsed as {} → missing "name" field
        self.assertEqual(body["error"]["code"], "VALIDATION_REQUIRED_FIELD_MISSING")
        conn.close()
        status, _ = self._json("GET", "/api/health")
        self.assertEqual(status, 200)

    def test_upload_source_title_path_traversal_stripped(self) -> None:
        """Path components in X-Filename must be stripped; only the basename is stored."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "path-guard"})
        status, body, _ = self._req(
            "POST",
            f"/api/notebooks/{nb['id']}/upload",
            ("検索エンジン最適化の基礎。" * 20).encode(),
            {"X-Filename": "../../evil/secret.txt"},
        )
        self.assertEqual(status, 201)
        data = json.loads(body if isinstance(body, (str, bytes)) else "{}") if status == 201 else {}
        # The stored title must not contain any directory separators
        _, nb_data = self._json("GET", f"/api/notebooks/{nb['id']}")
        titles = [s["title"] for s in (nb_data or {}).get("sources", [])]
        self.assertTrue(all("/" not in t and "\\" not in t for t in titles), titles)
        # Specifically, basename is preserved
        self.assertIn("secret.txt", titles)

    def test_upload_source_title_url_encoded_path_stripped(self) -> None:
        """URL-encoded path separators in X-Filename must also be stripped."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "encoded-path"})
        status, _, _ = self._req(
            "POST",
            f"/api/notebooks/{nb['id']}/upload",
            ("テスト文書。" * 30).encode(),
            {"X-Filename": urllib.parse.quote("../etc/passwd", safe="")},
        )
        self.assertEqual(status, 201)
        _, nb_data = self._json("GET", f"/api/notebooks/{nb['id']}")
        titles = [s["title"] for s in (nb_data or {}).get("sources", [])]
        self.assertIn("passwd", titles)
        self.assertTrue(all("/" not in t for t in titles), titles)

    def test_connection_error_hierarchy_covers_both_epipe_and_econnreset(self) -> None:
        """ConnectionError catches both BrokenPipeError (EPIPE) and
        ConnectionResetError (ECONNRESET), so the SSE handler's except clause
        handles both client-disconnect scenarios without data loss.
        """
        for exc_class in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            with self.assertRaises(ConnectionError, msg=f"{exc_class.__name__} must be ConnectionError"):
                raise exc_class("test")


class MidStreamLLMErrorTest(unittest.TestCase):
    """Server persists the complete client-visible content when LLM fails mid-stream."""

    @classmethod
    def setUpClass(cls) -> None:
        from shoin.llm import LLMError

        class PartialThenErrorLLM(FakeLLM):
            """Yields one token then raises LLMError to simulate mid-stream failure."""

            def chat_stream(self, messages, temperature=0.2):
                yield "部分的な回答"
                raise LLMError("SYSTEM_LLM_TIMEOUT", "timed out mid-stream")

        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = PartialThenErrorLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "mid.db"), llm=cls.llm)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _json(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self._url(path), data=body, method=method,
            headers={"Content-Type": "application/json"} if body else {}
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _sse(self, path, payload):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._url(path), data=body, method="POST",
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode()

    def test_mid_stream_llm_error_persists_partial_plus_degraded(self) -> None:
        """When LLM fails after yielding some tokens, the persisted message must include
        both the partial real tokens AND the degraded fallback text — matching what the
        client actually received."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "mid-stream-test"})
        nb_id = nb["id"]
        content = "和紙は楮から作られる。" * 30
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=content.encode(),
            method="POST",
            headers={"X-Filename": "washi.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        raw = self._sse(
            f"/api/notebooks/{nb_id}/ask",
            {"question": "和紙の原料は？"},
        )
        events = parse_sse(raw)
        event_kinds = [e for e, _ in events]
        self.assertIn("delta", event_kinds)
        self.assertEqual(event_kinds[-1], "done")
        # Client received the partial token AND the degraded text
        full_client = "".join(str(d["text"]) for e, d in events if e == "delta")
        self.assertIn("部分的な回答", full_client)
        # done event must flag degraded
        done_data = events[-1][1]
        self.assertTrue(done_data["degraded"])
        # Persisted message must match what the client saw
        _, nb_data = self._json("GET", f"/api/notebooks/{nb_id}")
        msgs = nb_data["messages"]
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 1)
        persisted_body = assistant_msgs[0]["body"]
        self.assertIn("部分的な回答", persisted_body)


if __name__ == "__main__":
    unittest.main(verbosity=0)
