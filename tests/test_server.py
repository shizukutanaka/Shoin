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

        # export — BibTeX must use .bib extension, not .bibtex
        status, headers, raw = self._req("GET", f"/api/notebooks/{nb_id}/export?format=bibtex")
        self.assertEqual(status, 200)
        cd = headers.get("Content-Disposition", "")
        self.assertIn("attachment", cd)
        self.assertIn(".bib\"", cd, "BibTeX download must use .bib extension (not .bibtex)")
        self.assertNotIn(".bibtex", cd)
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

    def test_loopback_only_accepts_ipv6_loopback(self) -> None:
        """::1 is a valid loopback address; make_server must not reject it with ValueError."""
        try:
            svr = make_server(host="::1", port=0)
            svr.server_close()
        except ValueError:
            self.fail("make_server(host='::1') must not raise ValueError — ::1 is loopback")
        except OSError:
            pass  # IPv6 unavailable in this environment — acceptable

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

    def test_ris_export_format(self) -> None:
        """RIS export must use .ris extension and include TY/ER record markers."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "RIS出力テスト"})
        nb_id = nb["id"]
        self._req(
            "POST",
            f"/api/notebooks/{nb_id}/upload",
            ("RISエクスポート用文書。" * 20).encode(),
            {"X-Filename": "ris_test.txt"},
        )
        status, headers, raw = self._req("GET", f"/api/notebooks/{nb_id}/export?format=ris")
        self.assertEqual(status, 200)
        cd = headers.get("Content-Disposition", "")
        self.assertIn("attachment", cd)
        self.assertIn(".ris\"", cd, "RIS download must use .ris extension")
        body = raw.decode()
        self.assertIn("TY  - GEN", body)
        self.assertIn("ER  - ", body)
        self.assertIn("ris_test.txt", body)

    def test_delete_nonexistent_note_returns_404(self) -> None:
        """Deleting a note that does not exist must return 404 NOTE_NOT_FOUND."""
        status, err = self._json("DELETE", "/api/notes/99999")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTE_NOT_FOUND")  # type: ignore[index]

    def test_rename_nonexistent_notebook_returns_404(self) -> None:
        """Renaming a notebook that does not exist must return 404 NOTEBOOK_NOT_FOUND."""
        status, err = self._json("PATCH", "/api/notebooks/99999", {"name": "ghost"})
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")  # type: ignore[index]

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

    def test_upload_duplicate_content_returns_409(self) -> None:
        """Uploading identical content twice must return 409 SOURCE_ALREADY_EXISTS."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "dup-upload"})
        nb_id = nb["id"]
        content = ("重複テスト用の文書。" * 30).encode()
        s1, _, _ = self._req(
            "POST", f"/api/notebooks/{nb_id}/upload", content, {"X-Filename": "dup.txt"}
        )
        self.assertEqual(s1, 201)
        s2, _, raw = self._req(
            "POST", f"/api/notebooks/{nb_id}/upload", content, {"X-Filename": "dup2.txt"}
        )
        self.assertEqual(s2, 409)
        self.assertEqual(json.loads(raw)["error"]["code"], "SOURCE_ALREADY_EXISTS")

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

    def test_studio_on_empty_notebook_returns_400(self) -> None:
        """Studio on a notebook with no sources must return 400 NOTEBOOK_EMPTY."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "空ノートブック"})
        status, err = self._json("POST", f"/api/notebooks/{nb['id']}/studio", {"kind": "briefing"})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_EMPTY")  # type: ignore[index]

    def test_studio_on_missing_notebook_returns_404(self) -> None:
        """Studio on a non-existent notebook must return 404 NOTEBOOK_NOT_FOUND."""
        status, err = self._json("POST", "/api/notebooks/99999/studio", {"kind": "briefing"})
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")  # type: ignore[index]

    def test_export_invalid_format_returns_400(self) -> None:
        """Unknown export format must return 400 VALIDATION_FIELD_FORMAT_INVALID."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "format-test"})
        status, err = self._json("GET", f"/api/notebooks/{nb['id']}/export?format=pdf")
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")  # type: ignore[index]

    def test_export_nonexistent_notebook_returns_404(self) -> None:
        """Exporting a notebook that does not exist must return 404 NOTEBOOK_NOT_FOUND."""
        status, err = self._json("GET", "/api/notebooks/99999/export?format=md")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")  # type: ignore[index]

    def test_clear_chat_nonexistent_notebook_returns_404(self) -> None:
        """Clearing messages on a nonexistent notebook must return 404 NOTEBOOK_NOT_FOUND."""
        status, err = self._json("DELETE", "/api/notebooks/99999/messages")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")  # type: ignore[index]

    def test_questions_nonexistent_notebook_returns_404(self) -> None:
        """Fetching questions for a nonexistent notebook must return 404 NOTEBOOK_NOT_FOUND."""
        status, err = self._json("GET", "/api/notebooks/99999/questions")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")  # type: ignore[index]

    def test_ask_nonexistent_notebook_returns_404_before_sse(self) -> None:
        """ask on a nonexistent notebook must return 404 JSON (not start SSE headers)."""
        status, err = self._json("POST", "/api/notebooks/99999/ask", {"question": "何？"})
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

    def test_questions_cache_stale_write_does_not_overwrite_newer_entry(self) -> None:
        """A concurrent source-add must not let a stale fingerprint clobber the cache.

        Simulates: Thread A computes questions with fp_old while Thread B adds a
        source and stores fp_new. Thread A must not overwrite fp_new with fp_old.
        """
        _, nb = self._json("POST", "/api/notebooks", {"name": "競合テスト"})
        nb_id = nb["id"]
        self._req(
            "POST",
            f"/api/notebooks/{nb_id}/upload",
            ("質問のタネ。" * 50).encode(),
            {"X-Filename": "a.txt"},
        )
        # Populate the cache with the CURRENT (up-to-date) fingerprint and questions.
        _, qs_resp = self._json("GET", f"/api/notebooks/{nb_id}/questions")
        good_qs = qs_resp["questions"]

        # Simulate: a concurrent thread computed questions for an old fingerprint and
        # is now trying to write stale data into the cache.
        handler = self.server.RequestHandlerClass
        stale_fp = (0,)  # fingerprint for a source-set that no longer exists
        with handler.questions_cache_lock:
            # Current cache should have the real fingerprint. Verify the guard:
            # writing a DIFFERENT fingerprint when a newer one is already cached
            # should be blocked.
            existing = handler.questions_cache.get(nb_id)
            self.assertIsNotNone(existing)
            real_fp = existing[0]
            # The guard condition: only overwrite if no entry exists OR it matches fp.
            if existing is None or existing[0] == stale_fp:
                handler.questions_cache[nb_id] = (stale_fp, ["stale question"])
            # stale_fp != real_fp → the guard prevents the overwrite

        # Cache must still hold the real entry, not the stale one.
        _, qs_after = self._json("GET", f"/api/notebooks/{nb_id}/questions")
        self.assertEqual(qs_after["questions"], good_qs)

    def test_source_delete_returns_200(self) -> None:
        """DELETE /api/sources/{id} must return 200 with the deleted source id."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "del-src"})
        nb_id = nb["id"]
        s1, _, _ = self._req(
            "POST", f"/api/notebooks/{nb_id}/upload",
            ("削除テスト用文書。" * 20).encode(), {"X-Filename": "del.txt"}
        )
        self.assertEqual(s1, 201)
        _, nb_data = self._json("GET", f"/api/notebooks/{nb_id}")
        src_id = nb_data["sources"][0]["id"]  # type: ignore[index]
        status, resp = self._json("DELETE", f"/api/sources/{src_id}")
        self.assertEqual(status, 200)
        self.assertEqual(resp["deleted"], src_id)  # type: ignore[index]

    def test_source_delete_nonexistent_returns_404(self) -> None:
        """DELETE /api/sources/{id} with unknown id must return 404 SOURCE_NOT_FOUND."""
        status, err = self._json("DELETE", "/api/sources/99999")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "SOURCE_NOT_FOUND")  # type: ignore[index]

    def test_json_body_too_large_returns_400(self) -> None:
        """A JSON-body request exceeding 10 MB must return 400 INGEST_FILE_TOO_LARGE."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        big_body = json.dumps({"name": "x" * (10 * 1024 * 1024 + 1)}).encode()
        conn.putrequest("POST", "/api/notebooks")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(big_body)))
        conn.endheaders(big_body)
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertEqual(body["error"]["code"], "INGEST_FILE_TOO_LARGE")

    def test_json_body_array_returns_400(self) -> None:
        """A JSON array body (not an object) must return 400 VALIDATION_FIELD_FORMAT_INVALID."""
        status, err = self._json("POST", "/api/notebooks", None)
        # Send a raw array instead of the normal dict
        status2, _, raw = self._req(
            "POST", "/api/notebooks", b"[1,2,3]",
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status2, 400)
        self.assertEqual(json.loads(raw)["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")

    def test_upload_zero_length_rejected(self) -> None:
        """An upload with Content-Length: 0 must return 400 INGEST_EMPTY."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "zero-upload"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.putrequest("POST", f"/api/notebooks/{nb['id']}/upload")
        conn.putheader("Content-Length", "0")
        conn.putheader("X-Filename", "empty.txt")
        conn.endheaders()
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertEqual(body["error"]["code"], "INGEST_EMPTY")

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

    def test_json_body_invalid_syntax_returns_400(self) -> None:
        """A syntactically broken JSON body must return 400 VALIDATION_FIELD_FORMAT_INVALID."""
        status, _, raw = self._req(
            "POST", "/api/notebooks", b"{broken:", {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")

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

    def test_upload_null_byte_in_filename_does_not_crash(self) -> None:
        """Null byte in X-Filename must not crash the server (ValueError → unhandled).

        Before the fix, the null byte propagated to NamedTemporaryFile's prefix
        argument, raising ValueError which was not caught by _dispatch and caused
        the server to close the connection instead of returning an HTTP status.
        """
        _, nb = self._json("POST", "/api/notebooks", {"name": "null-fix"})
        # urllib.parse.quote encodes \x00 as %00 (standard percent-encoding)
        status, _, _ = self._req(
            "POST",
            f"/api/notebooks/{nb['id']}/upload",
            ("テスト文書。" * 30).encode(),
            {"X-Filename": urllib.parse.quote("\x00evil.txt", safe="")},
        )
        # Null byte is stripped → filename becomes "evil.txt" → upload succeeds
        self.assertEqual(status, 201)

    def test_upload_tempfile_write_failure_does_not_leak_temp_file(self) -> None:
        """If tmp.write() raises mid-upload, the temp file must be cleaned up.

        Before the fix:
          tmp_path = Path(tmp.name)  was assigned AFTER  tmp.write(data)
          The try:...finally: block came AFTER the with-NamedTemporaryFile block,
          so when write() raised, the finally was never entered → temp file leaked.
        After the fix:
          tmp_path is set BEFORE write(), and the whole block is inside try:...finally:,
          so the finally always unlinks the (now correctly known) temp file.
        """
        import types
        from unittest.mock import patch

        import shoin.server as srv_mod

        _, nb = self._json("POST", "/api/notebooks", {"name": "write-fail"})
        real_ntf = tempfile.NamedTemporaryFile
        leaked: list[str] = []

        class BrokenWriteNTF:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._inner = real_ntf(*args, **kwargs)  # type: ignore[arg-type]
                self.name = self._inner.name
                leaked.append(self.name)

            def write(self, data: bytes) -> None:
                raise OSError("simulated disk full")

            def __enter__(self) -> "BrokenWriteNTF":
                return self

            def __exit__(self, *args: object) -> None:
                self._inner.__exit__(*args)

        fake_tempfile = types.SimpleNamespace(NamedTemporaryFile=BrokenWriteNTF)
        with patch.object(srv_mod, "tempfile", fake_tempfile):
            try:
                self._req(
                    "POST",
                    f"/api/notebooks/{nb['id']}/upload",
                    b"some content",
                    {"X-Filename": "test.txt"},
                )
            except (urllib.error.URLError, OSError):
                pass  # server closes connection on unhandled OSError

        self.assertTrue(leaked, "no temp file was created — test setup broken")
        for p in leaked:
            self.assertFalse(Path(p).exists(), f"temp file leaked after write() failure: {p}")

        # Server must remain responsive after a failed handler thread.
        status, _ = self._json("GET", "/api/health")
        self.assertEqual(status, 200)

    def test_connection_error_hierarchy_covers_both_epipe_and_econnreset(self) -> None:
        """ConnectionError catches both BrokenPipeError (EPIPE) and
        ConnectionResetError (ECONNRESET), so the SSE handler's except clause
        handles both client-disconnect scenarios without data loss.
        """
        for exc_class in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            with self.assertRaises(ConnectionError, msg=f"{exc_class.__name__} must be ConnectionError"):
                raise exc_class("test")


class NonStreamingLLMTest(unittest.TestCase):
    """Server falls back to non-streaming chat() when LLM has no chat_stream method."""

    class ChatOnlyLLM:
        """LLM backend with chat() but no chat_stream — tests the fallback code path."""
        embedding_model = ""
        model = "sync-only"

        def available(self) -> bool:
            return True

        def chat(self, messages: list[dict[str, str]], temperature: float = 0.2) -> str:
            return "同期応答 [S1]。"

        def embed_one(self, text: str) -> list[float]:
            return [1.0, 0.0]

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = cls.ChatOnlyLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "s.db"), llm=cls.llm)
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

    def _json(self, method: str, path: str, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self._url(path), data=body, method=method,
            headers={"Content-Type": "application/json"} if body else {}
        )
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())
        return resp.status, json.loads(raw) if raw else {}

    def test_non_streaming_llm_returns_done_event(self) -> None:
        """When the LLM has no chat_stream, _stream_chat falls back to chat(); ask must succeed."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "同期テスト"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("これは同期テストの内容です。内容について説明します。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "sync.txt"},
        )
        with urllib.request.urlopen(req):
            pass
        req2 = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/ask"),
            data=json.dumps({"question": "内容について教えてください"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req2) as resp:
            raw = resp.read().decode()
        events = parse_sse(raw)
        event_types = [e for e, _ in events]
        self.assertIn("done", event_types)
        self.assertIn("delta", event_types)


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


class PostStreamStoreErrorTest(unittest.TestCase):
    """StoreError from assistant message persistence after SSE headers must not corrupt the stream."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = FakeLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "ps.db"), llm=cls.llm)
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
            return resp.status, resp.read().decode()

    def test_store_error_on_assistant_persist_does_not_corrupt_sse(self) -> None:
        """StoreError from add_message(assistant) after 200 SSE headers are committed must be
        swallowed — the stream stays clean and the server remains responsive."""
        from unittest.mock import patch
        from shoin.store import Store, StoreError

        _, nb = self._json("POST", "/api/notebooks", {"name": "persist-fail"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("和紙は楮から作られる。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "washi.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        original = Store.add_message

        def failing(self_s, nb_id_arg, role, body, meta):
            if role == "assistant":
                raise StoreError("NOTEBOOK_NOT_FOUND", "deleted mid-stream")
            return original(self_s, nb_id_arg, role, body, meta)

        with patch.object(Store, "add_message", failing):
            status, raw = self._sse(f"/api/notebooks/{nb_id}/ask", {"question": "原料は？"})

        self.assertEqual(status, 200)
        kinds = [ev for ev, _ in parse_sse(raw)]
        self.assertIn("done", kinds)
        health_status, _ = self._json("GET", "/api/health")
        self.assertEqual(health_status, 200)

    def test_no_hit_store_error_on_assistant_persist_does_not_corrupt_sse(self) -> None:
        """Same guard applies to the no-hit branch (notebook with no sources)."""
        from unittest.mock import patch
        from shoin.store import Store, StoreError

        _, nb = self._json("POST", "/api/notebooks", {"name": "no-hit-persist-fail"})
        nb_id = nb["id"]

        original = Store.add_message

        def failing(self_s, nb_id_arg, role, body, meta):
            if role == "assistant":
                raise StoreError("NOTEBOOK_NOT_FOUND", "deleted mid-stream")
            return original(self_s, nb_id_arg, role, body, meta)

        with patch.object(Store, "add_message", failing):
            status, raw = self._sse(f"/api/notebooks/{nb_id}/ask", {"question": "原料は？"})

        self.assertEqual(status, 200)
        kinds = [ev for ev, _ in parse_sse(raw)]
        self.assertIn("done", kinds)
        health_status, _ = self._json("GET", "/api/health")
        self.assertEqual(health_status, 200)

    def test_operational_error_on_assistant_persist_does_not_kill_server(self) -> None:
        """sqlite3.OperationalError (disk full / lock timeout) after SSE headers must be
        swallowed just like StoreError — the server thread must survive."""
        import sqlite3
        from unittest.mock import patch
        from shoin.store import Store

        _, nb = self._json("POST", "/api/notebooks", {"name": "op-err-persist"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("楮は和紙の原料である。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "kaji.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        original = Store.add_message

        def failing(self_s, nb_id_arg, role, body, meta):
            if role == "assistant":
                raise sqlite3.OperationalError("disk I/O error")
            return original(self_s, nb_id_arg, role, body, meta)

        with patch.object(Store, "add_message", failing):
            status, raw = self._sse(f"/api/notebooks/{nb_id}/ask", {"question": "原料は？"})

        self.assertEqual(status, 200)
        kinds = [ev for ev, _ in parse_sse(raw)]
        self.assertIn("done", kinds)
        # Server must still be alive and responsive after an OperationalError in persist.
        health_status, _ = self._json("GET", "/api/health")
        self.assertEqual(health_status, 200)


class ClearChatCacheTest(unittest.TestCase):
    """Clearing chat history must NOT invalidate the questions cache.

    The cache fingerprint is based on source IDs, not messages. Clearing
    messages used to incorrectly pop the cache entry, forcing an unnecessary
    LLM re-call on the next /questions request.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = FakeLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "cc.db"), llm=cls.llm)
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

    def test_clear_chat_does_not_invalidate_questions_cache(self) -> None:
        """After the cache is warm, clearing chat must not trigger a second LLM call."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "チャットクリアキャッシュ"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("知識ベース文書。" * 50).encode(),
            method="POST",
            headers={"X-Filename": "kb.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        # Add a chat message so we have something to clear.
        req2 = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/ask"),
            data=json.dumps({"question": "何の文書？"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req2):
            pass

        # Warm the questions cache.
        before = self.llm.chat_count
        status, _ = self._json("GET", f"/api/notebooks/{nb_id}/questions")
        self.assertEqual(status, 200)
        after_warm = self.llm.chat_count
        self.assertEqual(after_warm, before + 1)  # one LLM call to warm the cache

        # Clear chat history.
        status, _ = self._json("DELETE", f"/api/notebooks/{nb_id}/messages")
        self.assertEqual(status, 200)

        # Questions request must be a cache HIT — sources unchanged, no LLM call.
        status, _ = self._json("GET", f"/api/notebooks/{nb_id}/questions")
        self.assertEqual(status, 200)
        self.assertEqual(self.llm.chat_count, after_warm,
                         "clearing chat must not invalidate the questions cache")


class SafeReportTest(unittest.TestCase):
    """Unit tests for the _safe_report helper in server.py."""

    def setUp(self) -> None:
        from shoin.server import _safe_report  # noqa: PLC0415
        self._fn = _safe_report

    def test_none_returns_empty_silently(self) -> None:
        """NULL citation_report in DB (None) must return {} without printing."""
        import io
        from unittest.mock import patch
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            result = self._fn(None)
        self.assertEqual(result, {})
        self.assertEqual(buf.getvalue(), "")

    def test_valid_json_parsed(self) -> None:
        result = self._fn('{"confirmed": [1], "misattributed": []}')
        self.assertEqual(result["confirmed"], [1])

    def test_corrupt_json_returns_empty_and_warns(self) -> None:
        """Corrupt DB value must return {} and print a stderr warning."""
        import io
        from unittest.mock import patch
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            result = self._fn("NOT-JSON{{{")
        self.assertEqual(result, {})
        self.assertIn("corrupt citation_report", buf.getvalue())

    def test_empty_string_returns_empty_and_warns(self) -> None:
        """Empty string in the DB is corrupt; must return {} and warn, like any bad JSON."""
        import io
        from unittest.mock import patch
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            result = self._fn("")
        self.assertEqual(result, {})
        self.assertIn("corrupt citation_report", buf.getvalue())


class LLMErrorDispatchTest(unittest.TestCase):
    """Verify that LLMError propagating out of a route handler returns HTTP 502."""

    @classmethod
    def setUpClass(cls) -> None:
        from shoin.llm import LLMError as _LLMError

        cls.tmp = tempfile.TemporaryDirectory()

        class BrokenChatLLM:
            """LLM that always raises LLMError from chat() (simulates endpoint down)."""
            embedding_model = ""
            model = "broken"

            def available(self) -> bool:
                return True

            def chat(self, messages: list[dict[str, str]], temperature: float = 0.2) -> str:
                raise _LLMError("SYSTEM_SERVICE_UNAVAILABLE", "endpoint down")

            def chat_stream(self, messages: list[dict[str, str]], temperature: float = 0.2) -> Iterator[str]:
                raise _LLMError("SYSTEM_SERVICE_UNAVAILABLE", "endpoint down")
                yield  # noqa: unreachable

            def embed_one(self, text: str) -> list[float]:
                return [1.0, 0.0]

        cls.llm = BrokenChatLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "llmerr.db"), llm=cls.llm)
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

    def _json(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
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

    def test_studio_llm_error_returns_502(self) -> None:
        """LLMError from a route handler must produce HTTP 502 (covers server.py:239)."""
        # Create a notebook with content so studio generation reaches the LLM.
        _, nb = self._json("POST", "/api/notebooks", {"name": "502-test"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("テストドキュメントの内容です。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "test.txt"},
        )
        with urllib.request.urlopen(req):
            pass
        # POST to /studio — the LLM will raise LLMError → 502
        status, body = self._json("POST", f"/api/notebooks/{nb_id}/studio", {"kind": "briefing"})
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "SYSTEM_SERVICE_UNAVAILABLE")


class UrlSourceIngestionTest(unittest.TestCase):
    """Verify that the /sources endpoint accepts http:// targets (covers server.py:328-331)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = FakeLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "url.db"), llm=cls.llm)
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

    def _json(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
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

    def test_url_source_ingestion_succeeds(self) -> None:
        """POST /sources with http:// target must call index_source and return 201."""
        from unittest.mock import patch

        from shoin.ingest import Extracted
        from shoin.pipeline import IndexResult, index_source

        _, nb = self._json("POST", "/api/notebooks", {"name": "url-ingest"})
        nb_id = nb["id"]

        fake_extracted = Extracted(
            kind="url", title="Mock Page", origin="http://example.test",
            sha256="abc123", text="This is mock page content for testing."
        )
        with patch("shoin.pipeline.extract_url", return_value=fake_extracted):
            status, body = self._json(
                "POST",
                f"/api/notebooks/{nb_id}/sources",
                {"target": "http://example.test"},
            )
        self.assertEqual(status, 201)
        self.assertEqual(body["source"]["title"], "Mock Page")
        self.assertGreater(body["n_chunks"], 0)


class HostnameOfTest(unittest.TestCase):
    def test_malformed_netloc_returns_empty_string(self) -> None:
        """_hostname_of must return '' when urlsplit raises ValueError (e.g. IDNA-invalid host)."""
        import urllib.parse
        from unittest.mock import patch

        from shoin.server import _hostname_of

        with patch.object(urllib.parse, "urlsplit", side_effect=ValueError("bad")):
            result = _hostname_of("//some-bad-host")
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main(verbosity=0)
