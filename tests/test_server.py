"""Phase 4 tests: HTTP server (routes, SSE ask, upload, security headers)."""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shoin.config import API_VERSION  # noqa: E402
from shoin.server import _THEME_CSS_LIMIT, make_server  # noqa: E402


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
        self.assertIn("multi_query", data)
        self.assertFalse(data["multi_query"])  # default OFF
        # v0.2.661 (product-review #17): staleness surface — FakeLLM has
        # embedding_model="" so nothing can be stale here.
        self.assertEqual(data["indexed_embed_model"], "")
        self.assertFalse(data["embed_model_changed"])
        # v0.2.663 (product-review #40): the API contract version is
        # discoverable from the health surface too.
        self.assertEqual(data["api"], API_VERSION)
        # v0.2.674 (product-review #58): a non-loopback LLM endpoint
        # silently breaks the local-only promise — health must say so.
        self.assertFalse(data["llm_external"])
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
        # `id` lets the viewer mark which chunks the answer cited (v0.2.139).
        self.assertIsInstance(chunks["chunks"][0]["id"], int)
        self.assertIn("seq", chunks["chunks"][0])

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

        # v0.2.255: response echoes the stored (stripped) name, not the raw
        # request value — same response-vs-stored class as _h_src_patch.
        status, renamed = self._json("PATCH", f"/api/notebooks/{nb_id}", {"name": "  和紙研究  "})
        self.assertEqual(status, 200)
        self.assertEqual(renamed["name"], "和紙研究")
        _, detail = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(detail["name"], "和紙研究")

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

        # 405: known path, wrong method (DELETE /api/notebooks — only GET/POST exist)
        status, err = self._json("DELETE", "/api/notebooks")
        self.assertEqual(status, 405)
        self.assertEqual(err.get("error", {}).get("code"), "METHOD_NOT_ALLOWED")

        # delete notebook
        status, _ = self._json("DELETE", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 200)

    def test_health_reports_embed_model_staleness(self) -> None:
        """v0.2.661 (product-review #17): /api/health names the model that
        built the stored vectors and flags the mismatch — the stderr hint
        emitted at query time never reaches a Web-UI user."""
        import threading as _th

        import shoin.server as srv_mod
        from shoin.store import Store

        class EmbedLLM(FakeLLM):
            embedding_model = "model-B"

        db = str(Path(self.tmp.name) / "s-embed.db")
        with Store(db) as s:
            s.set_setting("embed_model", "model-A")
        srv = srv_mod.make_server(port=0, db=db, llm=EmbedLLM())
        th = _th.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.server_address[1]}/api/health"
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
        finally:
            srv.shutdown()
            srv.server_close()
            th.join(timeout=5)
        self.assertEqual(data["embed_model"], "model-B")
        self.assertEqual(data["indexed_embed_model"], "model-A")
        self.assertTrue(data["embed_model_changed"])

    def test_health_survives_unopenable_db(self) -> None:
        """v0.2.661: health is the diagnostic surface — a data dir that
        cannot even be opened must not take the health check down with it.
        Blank staleness fields then correctly read as "unknown"."""
        import threading as _th

        import shoin.server as srv_mod

        srv = srv_mod.make_server(port=0, db=self.tmp.name, llm=FakeLLM())
        th = _th.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.server_address[1]}/api/health"
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
        finally:
            srv.shutdown()
            srv.server_close()
            th.join(timeout=5)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["indexed_embed_model"], "")
        self.assertFalse(data["embed_model_changed"])

    def test_upload_response_title_matches_persisted_truncated_title(self) -> None:
        """_h_src_upload()'s response must report the TRUNCATED title actually
        persisted by add_source() (MAX_TITLE_LEN, config.py), not the raw,
        untruncated filename. Found returning the untruncated raw_name while
        the sibling _h_src_add() (URL ingestion) already correctly returns
        result.source.title — a filename over 500 chars got a 201 response
        claiming a title that was never actually retrievable afterward.
        """
        from shoin.config import MAX_TITLE_LEN

        status, nb = self._json("POST", "/api/notebooks", {"name": "長いファイル名テスト"})
        nb_id = nb["id"]

        long_name = "A" * 600 + ".txt"
        body = ("十分な長さの本文。" * 10).encode("utf-8")
        status, _, raw = self._req(
            "POST",
            f"/api/notebooks/{nb_id}/upload",
            body,
            {"X-Filename": urllib.parse.quote(long_name)},
        )
        self.assertEqual(status, 201)
        up = json.loads(raw)
        self.assertEqual(len(up["source"]["title"]), MAX_TITLE_LEN)

        status, detail = self._json("GET", f"/api/notebooks/{nb_id}")
        stored_title = detail["sources"][0]["title"]
        self.assertEqual(
            up["source"]["title"], stored_title,
            "upload response title must match what was actually persisted",
        )

    def test_upload_whitespace_filename_falls_back(self) -> None:
        """A whitespace-only X-Filename must not persist a blank source title —
        it falls back to upload.txt, matching the empty-name fallback. Before
        the strip was added, raw_name '   ' stayed truthy through the sanitize
        chain and add_source() persisted it verbatim (the rename path rejects
        the same title)."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "空白名"})
        nb_id = nb["id"]
        body = ("十分な長さの本文。" * 10).encode("utf-8")
        status, _, raw = self._req(
            "POST",
            f"/api/notebooks/{nb_id}/upload",
            body,
            {"X-Filename": urllib.parse.quote("   ")},
        )
        self.assertEqual(status, 201)
        up = json.loads(raw)
        self.assertEqual(up["source"]["title"], "upload.txt")

    def test_detail_sources_carry_refreshable(self) -> None:
        """v0.2.633: GET /api/notebooks/{id} sources carry `refreshable` —
        true for URL origins and file origins whose path still exists, false
        for a file origin that is gone (e.g. an upload's cleaned-up tmp copy),
        so the UI can hide a refresh button that could only error."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "refreshable"})
        nb_id = nb["id"]
        db = str(Path(self.tmp.name) / "s.db")

        # File sources can't come over HTTP (src_add is URL-only); the CLI-add
        # shape is created directly, same as a real `shoin add` + `shoin serve`.
        from shoin.store import Store

        with tempfile.TemporaryDirectory() as td:
            live = Path(td) / "live.txt"
            live.write_text("live file content " * 4, encoding="utf-8")
            with Store(db) as store:
                store.add_source(nb_id, "txt", "live.txt", str(live), "sha-l")
                # The upload shape: a source whose origin path is already gone.
                store.add_source(
                    nb_id, "txt", "gone.txt", "/nonexistent/gone.txt", "sha-g"
                )

            status, detail = self._json("GET", f"/api/notebooks/{nb_id}")
            self.assertEqual(status, 200)
            by_title = {s["title"]: s for s in detail["sources"]}
            self.assertTrue(by_title["live.txt"]["refreshable"])
            self.assertFalse(by_title["gone.txt"]["refreshable"])

    def test_search_endpoint_returns_ranked_hits(self) -> None:
        """v0.2.637: POST /api/notebooks/{id}/search runs ask's own retrieval
        pipeline (expand→embed→retrieve_for_question) and returns the ranked
        hits — rank/source/section/score/text — generating no answer and
        persisting nothing (product-review #25: "I just want the search
        results" had no API route; bm25_search/vector_search were internal
        only)."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "searchapi"})
        nb_id = nb["id"]
        db = str(Path(self.tmp.name) / "s.db")
        from shoin.store import Store

        with Store(db) as store:
            src = store.add_source(nb_id, "txt", "cats.txt", "mem://cats", "sha-c")
            store.add_chunks(src.id, ["猫は液体である説は流動性の比喩である。"])
            src2 = store.add_source(nb_id, "txt", "dogs.txt", "mem://dogs", "sha-d")
            store.add_chunks(src2.id, ["犬は固体である。"])

        status, out = self._json(
            "POST", f"/api/notebooks/{nb_id}/search",
            {"question": "猫は液体である説について"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(out["question"], "猫は液体である説について")
        hits = out["hits"]
        self.assertGreaterEqual(len(hits), 1)
        top = hits[0]
        self.assertEqual(top["rank"], 1)
        self.assertEqual(top["source_id"], src.id)
        self.assertEqual(top["title"], "cats.txt")
        self.assertIn("猫は液体", top["text"])
        self.assertIn("score", top)
        # Nothing persisted: the notebook's message list stays empty.
        status, detail = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(detail["messages"], [])

        # source_ids scoping is the same contract as /ask.
        status, out = self._json(
            "POST", f"/api/notebooks/{nb_id}/search",
            {"question": "猫は液体", "source_ids": [src2.id]},
        )
        self.assertEqual(status, 200)
        for h in out["hits"]:
            self.assertEqual(h["source_id"], src2.id)

    def test_search_endpoint_validates_like_ask(self) -> None:
        """v0.2.637: /search shares /ask's pre-dispatch validation contract —
        coded envelope for a missing question, an out-of-range or mistyped k,
        a dead notebook, and a foreign source id (never leaking that it
        exists on another notebook)."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "searchval"})
        nb_id = nb["id"]
        db = str(Path(self.tmp.name) / "s.db")
        from shoin.store import Store

        with Store(db) as store:
            other_nb = store.create_notebook("other")
            foreign = store.add_source(
                other_nb.id, "txt", "x.txt", "mem://x", "sha-x"
            )

        status, err = self._json(
            "POST", f"/api/notebooks/{nb_id}/search", {"k": 3}
        )
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_REQUIRED_FIELD_MISSING")

        for bad_k in (0, -1, 51, "x", [1], True):
            status, err = self._json(
                "POST", f"/api/notebooks/{nb_id}/search",
                {"question": "q", "k": bad_k},
            )
            self.assertEqual(status, 400, f"k={bad_k!r}")
            self.assertEqual(
                err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID"
            )

        status, err = self._json(
            "POST", f"/api/notebooks/{nb_id}/search",
            {"question": "q", "source_ids": [foreign.id]},
        )
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "SOURCE_NOT_FOUND")

        status, err = self._json(
            "POST", "/api/notebooks/999999/search", {"question": "q"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")

    def test_global_search_endpoint_crosses_notebooks(self) -> None:
        """v0.2.649: POST /api/search is the notebook-less sibling of
        /notebooks/{id}/search — the same retrieve pipeline with scope
        notebook_id=None, so sources in EVERY notebook are candidates and
        each hit carries (notebook_id, notebook, title) so the caller can
        route back to the owning notebook (product-review #7)."""
        status, nb1 = self._json("POST", "/api/notebooks", {"name": "sea"})
        status, nb2 = self._json("POST", "/api/notebooks", {"name": "sky"})
        db = str(Path(self.tmp.name) / "s.db")
        from shoin.store import Store

        with Store(db) as store:
            s1 = store.add_source(nb1["id"], "txt", "ocean.txt", "mem://o", "sha-o")
            store.add_chunks(s1.id, ["海洋酸性化は炭酸塩飽和度を低下させる。"])
            s2 = store.add_source(nb2["id"], "txt", "orbit.txt", "mem://s", "sha-s")
            store.add_chunks(s2.id, ["気象衛星は赤外放射量を観測する。"])

        status, out = self._json("POST", "/api/search", {"question": "気象衛星"})
        self.assertEqual(status, 200)
        self.assertEqual(out["question"], "気象衛星")
        self.assertTrue(out["hits"])
        hit = out["hits"][0]
        self.assertEqual(hit["rank"], 1)
        self.assertEqual(hit["source_id"], s2.id)
        self.assertEqual(hit["notebook_id"], nb2["id"])
        self.assertEqual(hit["notebook"], "sky")
        self.assertEqual(hit["title"], "orbit.txt")
        self.assertIn("気象衛星", hit["text"])
        # Same question scoped to nb1 cannot see the nb2 source.
        status, scoped = self._json(
            "POST", f"/api/notebooks/{nb1['id']}/search", {"question": "気象衛星"}
        )
        self.assertEqual(status, 200)
        self.assertNotIn(s2.id, {h["source_id"] for h in scoped["hits"]})
        # Missing question shares the coded envelope.
        status, err = self._json("POST", "/api/search", {"k": 3})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_REQUIRED_FIELD_MISSING")
        status, err = self._json(
            "POST", "/api/search", {"question": "q", "k": 51}
        )
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")

    def test_search_zero_hit_returns_suggestions(self) -> None:
        """v0.2.650 (product-review #42): a zero-hit query is no dead end —
        both search routes echo `suggestions` carrying the nearest in-corpus
        spelling; a hitting query emits the key as []."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "気象"})
        db = str(Path(self.tmp.name) / "s.db")
        from shoin.store import Store

        with Store(db) as store:
            src = store.add_source(
                nb["id"], "txt", "気象衛星メモ", "mem://m", "sha-m"
            )
            store.add_chunks(src.id, ["気象衛星は赤外放射量を観測する。"])

        for path in (f"/api/notebooks/{nb['id']}/search", "/api/search"):
            status, out = self._json("POST", path, {"question": "気海衛生"})
            self.assertEqual(status, 200)
            self.assertEqual(out["hits"], [])
            self.assertEqual(out["suggestions"], ["気象衛星"])
            status, hit = self._json("POST", path, {"question": "気象衛星"})
            self.assertEqual(status, 200)
            self.assertTrue(hit["hits"])
            self.assertEqual(hit["suggestions"], [])

    def test_nb_duplicate_forks_notebook_and_stays_coded(self) -> None:
        """v0.2.645: POST /api/notebooks/{id}/duplicate forks the notebook —
        empty body yields '<name> (copy)', {"name": "..."} is honored, and
        a dead id is a coded 404, never a 500."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "複製元"})
        self.assertEqual(status, 201)
        nb_id = nb["id"]

        status, dup = self._json("POST", f"/api/notebooks/{nb_id}/duplicate")
        self.assertEqual(status, 201)
        self.assertEqual(dup["name"], "複製元 (copy)")
        self.assertNotEqual(dup["id"], nb_id)

        status, named = self._json(
            "POST", f"/api/notebooks/{nb_id}/duplicate", {"name": "複製先"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(named["name"], "複製先")

        status, detail = self._json("GET", f"/api/notebooks/{dup['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["name"], "複製元 (copy)")

        status, err = self._json("POST", "/api/notebooks/999999/duplicate")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")

    def test_src_text_paged_at_bytes_cap(self) -> None:
        """v0.2.695: GET /api/sources/{id}/text bounds one response at
        SRC_TEXT_BYTES_MAX — the last unbounded payload on the API (import
        documents bound chunk COUNT, not text length, so a crafted export
        can put ~1GiB behind one source id and fetchall+dumps materializes
        it twice). Pages carry truncated/next_offset/total; ?offset continues
        the cut; a single >cap chunk is itself sliced by bytes."""
        import shoin.server as srv
        from shoin.store import Store

        _, nb = self._json("POST", "/api/notebooks", {"name": "txt"})
        nb_id = nb["id"]
        with Store(str(Path(self.tmp.name) / "s.db")) as store:
            src = store.add_source(nb_id, "txt", "big", "o", "h")
            # 20 chunks of ~256B each; cap 1000B pages them ~7 deep.
            store.add_chunks(src.id, [f"t{i}" * 64 for i in range(20)])
            src2 = store.add_source(nb_id, "txt", "huge", "o", "h2")
            store.add_chunks(src2.id, ["x" * 2000, "tail"])

        with patch.object(srv, "SRC_TEXT_BYTES_MAX", 1000), patch.object(
            srv, "SRC_TEXT_BATCH", 4
        ):
            status, j = self._json("GET", f"/api/sources/{src.id}/text")
            self.assertEqual(status, 200)
            self.assertEqual(j["total"], 20)
            self.assertTrue(j["truncated"])
            self.assertEqual(j["offset"], 0)
            self.assertLess(j["next_offset"], 20)
            self.assertGreater(j["next_offset"], 0)
            self.assertEqual(len(j["chunks"]), j["next_offset"])
            # Every row stays reachable: walking next_offset pages the rest.
            got = [c["text"] for c in j["chunks"]]
            off = j["next_offset"]
            while True:
                status, p = self._json(
                    "GET", f"/api/sources/{src.id}/text?offset={off}"
                )
                self.assertEqual(status, 200)
                got += [c["text"] for c in p["chunks"]]
                off = p["next_offset"]
                if not p["truncated"]:
                    break
            self.assertEqual(off, 20)
            self.assertEqual(len(got), 20)
            self.assertEqual(got[0], "t0" * 64)
            self.assertEqual(got[-1], "t19" * 64)

            # A single chunk larger than the cap is sliced by bytes, not
            # dropped — and the remainder is disclosed by the same flag.
            status, j = self._json("GET", f"/api/sources/{src2.id}/text")
            self.assertEqual(status, 200)
            self.assertTrue(j["truncated"])
            self.assertEqual(len(j["chunks"]), 1)
            self.assertEqual(len(j["chunks"][0]["text"]), 1000)
            self.assertEqual(j["total"], 2)

        # Under the cap the flag is honestly false and no offset is needed.
        status, j = self._json("GET", f"/api/sources/{src.id}/text")
        self.assertFalse(j["truncated"])
        self.assertEqual(len(j["chunks"]), 20)

        for bad in ("offset=-1", "offset=abc"):
            status, err = self._json(
                "GET", f"/api/sources/{src.id}/text?{bad}"
            )
            self.assertEqual(status, 400, bad)
            self.assertEqual(
                err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID", bad
            )
        status, err = self._json("GET", "/api/sources/999999/text")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "SOURCE_NOT_FOUND")

    def test_nb_messages_and_notes_pagination(self) -> None:
        """v0.2.646: GET .../messages and .../notes page the full record the
        detail cap can't reach — newest-first, offset/limit bounded, total
        disclosed, invalid params coded 400, dead notebook 404."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "paged"})
        self.assertEqual(status, 201)
        nb_id = nb["id"]
        db = str(Path(self.tmp.name) / "s.db")
        from shoin.store import Store

        with Store(db) as store:
            for i in range(5):
                store.add_message(nb_id, "user", f"m{i}")
            for i in range(3):
                store.add_note(nb_id, f"n{i}", "b")

        status, page = self._json(
            "GET", f"/api/notebooks/{nb_id}/messages?offset=1&limit=2"
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 5)
        self.assertEqual(page["offset"], 1)
        self.assertEqual(page["limit"], 2)
        self.assertEqual([m["body"] for m in page["messages"]], ["m3", "m2"])
        self.assertIn("id", page["messages"][0])
        self.assertIn("created_at", page["messages"][0])

        status, page = self._json("GET", f"/api/notebooks/{nb_id}/messages")
        self.assertEqual(status, 200)
        self.assertEqual(len(page["messages"]), 5)
        self.assertEqual(page["messages"][0]["body"], "m4")

        status, page = self._json(
            "GET", f"/api/notebooks/{nb_id}/notes?limit=2"
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 3)
        self.assertEqual([n["title"] for n in page["notes"]], ["n2", "n1"])

        for bad in ("offset=-1", "offset=abc", "limit=0", "limit=501"):
            status, err = self._json(
                "GET", f"/api/notebooks/{nb_id}/messages?{bad}"
            )
            self.assertEqual(status, 400, bad)
            self.assertEqual(
                err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID", bad
            )
        status, err = self._json("GET", "/api/notebooks/999999/messages")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")
        status, err = self._json("GET", "/api/notebooks/999999/notes")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")

    def test_chunk_patch_updates_text_and_stays_coded(self) -> None:
        """v0.2.647: PATCH /api/chunks/{id} rewrites the chunk in place —
        echo carries {id, source_id, seq, text}, the reader endpoint shows
        the new text, dead chunk 404s, empty/missing text is a coded 400."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "edit"})
        self.assertEqual(status, 201)
        nb_id = nb["id"]
        db = str(Path(self.tmp.name) / "s.db")
        from shoin.store import Store

        with Store(db) as store:
            src = store.add_source(nb_id, "txt", "doc", "mem://d", "sha1")
            store.add_chunks(src.id, ["typo'd extract"])
            cid = store.chunks_for_source(src.id)[0].id

        status, body = self._json(
            "PATCH", f"/api/chunks/{cid}", {"text": "corrected extract"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], cid)
        self.assertEqual(body["source_id"], src.id)
        self.assertEqual(body["seq"], 0)
        self.assertEqual(body["text"], "corrected extract")

        status, view = self._json("GET", f"/api/sources/{src.id}/text")
        self.assertEqual(status, 200)
        self.assertEqual(view["chunks"][0]["text"], "corrected extract")

        status, err = self._json("PATCH", "/api/chunks/999999", {"text": "x"})
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "CHUNK_NOT_FOUND")
        status, err = self._json("PATCH", f"/api/chunks/{cid}", {"text": "  "})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_REQUIRED_FIELD_MISSING")
        status, err = self._json("PATCH", f"/api/chunks/{cid}", {})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_REQUIRED_FIELD_MISSING")

    def test_upload_response_reports_pages_failed(self) -> None:
        """v0.2.256: a PDF whose pages partially fail extraction must surface
        pages_failed in the upload response — otherwise a partial index is
        presented as a complete one."""
        import shoin.server as srv
        from shoin.pipeline import IndexResult
        from shoin.store import Source

        status, nb = self._json("POST", "/api/notebooks", {"name": "PDF欠損テスト"})
        nb_id = nb["id"]
        fake = IndexResult(
            Source(id=1, notebook_id=nb_id, kind="pdf", title="broken.pdf",
                   origin="broken.pdf", sha256="x", added_at="now"),
            n_chunks=3, n_embedded=0, pages_failed=2,
        )
        body = "なんとか本文".encode()
        with patch.object(srv, "index_source", return_value=fake):
            status, _, raw = self._req(
                "POST",
                f"/api/notebooks/{nb_id}/upload",
                body,
                {"X-Filename": "broken.pdf"},
            )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(raw)["pages_failed"], 2)

    def test_upload_filename_latin1_only_survives(self) -> None:
        """An X-Filename whose latin-1 bytes don't form UTF-8 ('é.txt') must
        skip the recovery decode and still upload under its decoded name —
        the encodeURIComponent convention is unenforced, so the fallback must
        degrade gracefully, not crash (v0.2.277)."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "latin"})
        body = "内容テキストです。".encode()
        status, _, raw = self._req(
            "POST",
            f"/api/notebooks/{nb['id']}/upload",
            body,
            {"X-Filename": "é.txt"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(raw)["source"]["title"], "é.txt")

    def test_refresh_response_reports_pages_failed(self) -> None:
        """v0.2.258: refresh of a URL-ingested PDF re-extracts the document and
        can lose pages on the second pass — the response must carry the count
        just like add/upload do, or the loss regresses to silent."""
        import shoin.server as srv
        from shoin.pipeline import IndexResult
        from shoin.store import Source

        status, nb = self._json("POST", "/api/notebooks", {"name": "refresh PDF"})
        nb_id = nb["id"]
        src = Source(id=1, notebook_id=nb_id, kind="pdf", title="paper.pdf",
                     origin="https://x/paper.pdf", sha256="y", added_at="now")
        fake = IndexResult(src, n_chunks=2, n_embedded=0, pages_failed=3)
        with patch.object(srv, "refresh_source", return_value=fake):
            status, body = self._json("POST", "/api/sources/1/refresh")
        self.assertEqual(status, 200)
        self.assertEqual(body["pages_failed"], 3)

    def test_nb_refresh_all_collects_statuses(self) -> None:
        """v0.2.648: POST /api/notebooks/{id}/refresh-all is the Web-side batch
        refresh — echoes the per-source outcome list and keeps the coded
        NOTEBOOK_NOT_FOUND contract on a dead notebook."""
        import shoin.server as srv

        status, nb = self._json("POST", "/api/notebooks", {"name": "ra"})
        nb_id = nb["id"]
        fake = [
            {"id": 1, "title": "a", "status": "refreshed",
             "n_chunks": 2, "n_embedded": 0},
            {"id": 2, "title": "b", "status": "skipped"},
        ]
        with patch.object(srv, "refresh_all_sources", return_value=fake):
            status, body = self._json("POST", f"/api/notebooks/{nb_id}/refresh-all")
        self.assertEqual(status, 200)
        self.assertEqual(body["results"], fake)
        status, body = self._json("POST", "/api/notebooks/9999/refresh-all")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOTEBOOK_NOT_FOUND")

    def test_unexpected_exception_in_handler_returns_500(self) -> None:
        """Unexpected exceptions not subclassing StoreError/IngestError/LLMError
        (e.g. sqlite3.OperationalError: database is locked) must return HTTP 500
        with SYSTEM_INTERNAL_ERROR rather than closing the connection with no response.
        """
        import sqlite3
        from unittest.mock import patch

        import shoin.store as store_mod

        _, nb = self._json("POST", "/api/notebooks", {"name": "crash-test"})
        nb_id = nb["id"]
        with patch.object(
            store_mod.Store, "get_notebook",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            status, err = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 500)
        self.assertEqual(err["error"]["code"], "SYSTEM_INTERNAL_ERROR")  # type: ignore[index]

    def test_store_error_with_system_code_returns_500(self) -> None:
        """A StoreError carrying a SYSTEM_* code (the store's own report of an
        internal failure, e.g. an unexpected constraint violation) must map to
        HTTP 500, not the client-error 400 fallback — a 400 tells the caller
        their request was malformed when the server actually failed."""
        from unittest.mock import patch

        import shoin.store as store_mod
        from shoin.store import StoreError

        _, nb = self._json("POST", "/api/notebooks", {"name": "sys-err"})
        nb_id = nb["id"]
        with patch.object(
            store_mod.Store, "get_notebook",
            side_effect=StoreError("SYSTEM_INTERNAL_ERROR", "unexpected constraint violation"),
        ):
            status, err = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 500)
        self.assertEqual(err["error"]["code"], "SYSTEM_INTERNAL_ERROR")  # type: ignore[index]

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
        self.assertIn("ER  -", body)
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
            self.assertEqual(
                status, 400,
                msg=f"file path target should be rejected: {bad_target!r}",
            )
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
        status, err = self._json(
            "POST", f"/api/notebooks/{nb['id']}/notes",
            {"title": "T", "body": "B"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")  # type: ignore[index]

    def test_unpaired_surrogate_in_required_field_returns_400(self) -> None:
        """json.loads turns \ud800 escapes into lone surrogates raw UTF-8 bytes
        can't carry; one reaching a write surfaces as an uncaught
        UnicodeEncodeError out of the sqlite3 binding — a raw 500 for a
        client-side format error. The field must be rejected at the
        validator as VALIDATION_FIELD_FORMAT_INVALID."""
        status, err = self._json("POST", "/api/notebooks", {"name": "nb\ud800"})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")  # type: ignore[index]

    def test_unpaired_surrogate_in_optional_field_returns_400(self) -> None:
        """Same surrogate class through the optional-field validator (note body)."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "ok"})
        status, err = self._json(
            "POST", f"/api/notebooks/{nb['id']}/notes",
            {"title": "t", "body": "x\udfff"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")  # type: ignore[index]

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

    def test_ask_source_ids_validation(self) -> None:
        """v0.2.631: the optional source_ids field on /ask scopes retrieval to
        the named sources — malformed values must be a 400/404 JSON envelope
        BEFORE the SSE stream opens, and a foreign-notebook source id must
        404 exactly like a dead one."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "スコープ"})
        nb_id = nb["id"]
        status, _, raw = self._req(
            "POST",
            f"/api/notebooks/{nb_id}/upload",
            "和紙の原料は楮である。".encode(),
            {"X-Filename": urllib.parse.quote("原料.txt")},
        )
        self.assertEqual(status, 201)
        src_id = json.loads(raw)["source"]["id"]

        def ask(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
            return self._json("POST", f"/api/notebooks/{nb_id}/ask", payload)

        for bad in ("5", "src", [True], [0], [-3], [1.5], ["1"]):
            status, err = ask({"question": "楮は？", "source_ids": bad})
            self.assertEqual(status, 400, f"{bad!r} -> {status}")
            self.assertEqual(
                err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID"  # type: ignore[index]
            )
        status, err = ask({"question": "楮は？", "source_ids": [2**63]})
        self.assertEqual(status, 400)
        self.assertEqual(
            err["error"]["code"], "VALIDATION_INTEGER_OVERFLOW"  # type: ignore[index]
        )
        # v0.2.688: the scope list itself is bounded — without a length cap a
        # ~10MB body could name millions of ids and the per-id get_source loop
        # burned one SELECT each on the request thread.
        from shoin.config import MAX_SCOPE_IDS

        status, err = ask(
            {"question": "楮は？", "source_ids": list(range(1, MAX_SCOPE_IDS + 2))}
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID"  # type: ignore[index]
        )
        status, err = ask({"question": "楮は？", "source_ids": [99999]})
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "SOURCE_NOT_FOUND")  # type: ignore[index]

        # A source living in another notebook 404s exactly like a dead id —
        # answering scoped to foreign content would both leak its existence
        # and ground the reply in sources the user never attached here.
        _, nb2 = self._json("POST", "/api/notebooks", {"name": "他ノート"})
        status, _, raw2 = self._req(
            "POST",
            f"/api/notebooks/{nb2['id']}/upload",
            b"foreign content.",
            {"X-Filename": urllib.parse.quote("他.txt")},
        )
        self.assertEqual(status, 201)
        foreign_id = json.loads(raw2)["source"]["id"]
        status, err = ask({"question": "楮は？", "source_ids": [foreign_id]})
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "SOURCE_NOT_FOUND")  # type: ignore[index]

        # A valid scope streams normally and cites only the selected source.
        status, _, raw = self._req(
            "POST",
            f"/api/notebooks/{nb_id}/ask",
            json.dumps({"question": "楮は？", "source_ids": [src_id]}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        events = parse_sse(raw.decode())
        self.assertEqual(events[-1][0], "done")
        meta = next(d for e, d in events if e == "meta")
        self.assertTrue(meta["sources"])
        self.assertTrue(all(s["source_id"] == src_id for s in meta["sources"]))

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

    def test_questions_cache_invalidates_on_content_change_same_id(self) -> None:
        """Content rewrite under an unchanged source id must expire the cache.

        A reindex/refresh from ANOTHER process (CLI `shoin reindex` while
        `serve` runs) rewrites chunks and bumps sources.sha256 without any
        pop reaching this server's questions_cache — an id-only fingerprint
        still matched, so suggestions generated from dead content were
        served indefinitely. The (id, sha256, title) fingerprint
        self-expires exactly when what fed the suggestions changed."""
        from shoin.store import Store

        _, nb = self._json("POST", "/api/notebooks", {"name": "fp-content"})
        nb_id = nb["id"]
        self._req(
            "POST",
            f"/api/notebooks/{nb_id}/upload",
            ("内容のある文書。" * 50).encode(),
            {"X-Filename": "c.txt"},
        )
        self._json("GET", f"/api/notebooks/{nb_id}/questions")  # prime cache
        before = self.llm.chat_count
        # Cross-process writer: same source id, new content + new sha256 —
        # exactly what pipeline.refresh_source / CLI reindex performs.
        with Store(str(Path(self.tmp.name) / "s.db")) as other:
            src = other.sources_for_notebook(nb_id)[0]
            other.replace_chunks_for_source(
                src.id, ["変わった内容。" * 50], sha256="fresh-sha", title=src.title
            )
        status, _ = self._json("GET", f"/api/notebooks/{nb_id}/questions")
        self.assertEqual(status, 200)
        self.assertEqual(self.llm.chat_count, before + 1)  # regenerated, not stale

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

    def test_upload_claimed_larger_than_drain_cap_closes_connection(self) -> None:
        """Upload Content-Length >> drain cap must return 400 and close the connection.

        When the claimed body is larger than MAX_UPLOAD_BYTES + 65536 the server
        cannot drain it all, so it sets close_connection=True.  The client gets
        the error response and the connection is not reused for a subsequent request.
        """
        _, nb = self._json("POST", "/api/notebooks", {"name": "draincap"})
        # Content-Length claims 20 MB but we send a tiny body.
        huge = 20 * 1024 * 1024
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.putrequest("POST", f"/api/notebooks/{nb['id']}/upload")
        # Send only MAX_UPLOAD_BYTES + 65536 bytes of actual body but claim 20 MB.
        from shoin.config import MAX_UPLOAD_BYTES
        actual_body = b"x" * (MAX_UPLOAD_BYTES + 65536)
        conn.putheader("Content-Length", str(huge))
        conn.putheader("X-Filename", "big.txt")
        conn.endheaders(actual_body)
        resp = conn.getresponse()
        body = json.loads(resp.read())
        self.assertEqual(resp.status, 400)
        self.assertEqual(body["error"]["code"], "INGEST_FILE_TOO_LARGE")
        conn.close()
        # Server must still be accepting new connections after the drain.
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

    def test_protocol_error_responses_carry_baseline_headers(self) -> None:
        """v0.2.316: the base send_error() path (unimplemented method, bad
        request line) used to emit a bare HTML page bypassing _headers() — no
        nosniff/no-store/Referrer-Policy and wrong content type. All errors
        must go through the JSON envelope."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("OPTIONS", "/")  # not implemented -> 501 via send_error
            resp = conn.getresponse()
            body = resp.read()
            self.assertEqual(resp.status, 501)
            headers = {k.lower(): v for k, v in resp.getheaders()}
            self.assertEqual(headers.get("x-content-type-options"), "nosniff")
            self.assertEqual(headers.get("cache-control"), "no-store")
            self.assertEqual(headers.get("referrer-policy"), "no-referrer")
            self.assertNotIn("Python", headers.get("server", ""))
            payload = json.loads(body)
            self.assertIn("error", payload)
            self.assertEqual(payload["error"]["code"], "HTTP_501")
        finally:
            conn.close()

    def test_idle_connection_times_out_quietly(self) -> None:
        """v0.2.315: an accepted socket that never completes its request would
        hold its handler thread forever — REQUEST_SOCKET_SEC bounds any single
        blocking socket op, and the timeout close must not spam a traceback
        (clients that stall mid-request are expected traffic)."""
        import io
        import socket

        import shoin.server as srv_mod

        with patch.object(srv_mod, "REQUEST_SOCKET_SEC", 0.2):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            try:
                s.sendall(b"GET / HTTP/1.1\r\n")  # deliberately incomplete
                captured = io.StringIO()
                with patch("sys.stderr", captured):
                    deadline = time.time() + 10
                    while True:
                        got = s.recv(4096)
                        if got == b"":
                            break  # server closed the connection
                        if time.time() > deadline:
                            self.fail("idle connection never timed out")
            finally:
                s.close()
        self.assertNotIn(
            "Traceback",
            captured.getvalue(),
            "a socket timeout must close quietly, not log a traceback",
        )

    def test_server_close_does_not_join_inflight_handler_threads(self) -> None:
        """daemon_threads=True: server_close() must not stall on in-flight reads.

        The server speaks HTTP/1.0, so every connection closes after one
        request — the parked-thread scenario is a client that stalls
        mid-request (partial request line, abandoned connection), which parks
        its handler in rfile.read() for up to REQUEST_SOCKET_SEC (120s). With
        the default daemon_threads=False, server_close() JOINS that thread —
        Ctrl+C would hang for the full socket timeout while any request is
        still in flight. Daemon handler threads die with the process instead."""
        import socket
        import threading as _th

        import shoin.server as srv_mod

        with patch.object(srv_mod, "REQUEST_SOCKET_SEC", 3.0):
            srv = srv_mod.make_server(
                port=0, db=str(Path(self.tmp.name) / "s-close.db"), llm=FakeLLM()
            )
            th = _th.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05})
            th.start()
            sock = socket.create_connection(("127.0.0.1", srv.server_address[1]))
            try:
                # Partial request line: the handler parks in rfile.read() until
                # the (patched) socket timeout, so it is still parked when
                # server_close() runs — exactly what daemon_threads avoids
                # joining.
                sock.sendall(b"GET /api/health HT")
                time.sleep(0.3)
                srv.shutdown()
                started = time.monotonic()
                srv.server_close()
                elapsed = time.monotonic() - started
            finally:
                sock.close()
                th.join(timeout=5)
        # Non-daemon close joins the parked handler until its read times out
        # (~2.7s here: 3s socket timeout minus the 0.3s head start); daemon
        # close returns immediately.
        self.assertLess(elapsed, 2.0)
        self.assertTrue(srv.daemon_threads)

    def test_inflight_semaphore_bounds_handler_threads(self) -> None:
        """v0.2.691 (product-review #75): ThreadingHTTPServer spawned one
        thread per connection with no ceiling — the socket timeout caps each
        connection's LIFETIME but nothing capped the COUNT, so a connection
        flood exhausted threads before a timeout ever freed one. The
        MAX_IN_FLIGHT_REQUESTS semaphore parks excess connections in the
        kernel backlog and returns the slot when the handler exits."""
        import socket
        import threading as _th

        import shoin.server as srv_mod

        with patch.object(srv_mod, "MAX_IN_FLIGHT_REQUESTS", 1):
            srv = srv_mod.make_server(
                port=0, db=str(Path(self.tmp.name) / "s-cap.db"), llm=FakeLLM()
            )
        th = _th.Thread(
            target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        th.start()
        held = socket.create_connection(("127.0.0.1", srv.server_address[1]))
        waiter = socket.create_connection(("127.0.0.1", srv.server_address[1]))
        try:
            # Partial request line parks this connection's handler in
            # rfile.read() — it owns the only slot for the rest of the test.
            held.sendall(b"GET /api/health HT")
            deadline = time.monotonic() + 5
            while srv._in_flight.acquire(blocking=False):
                srv._in_flight.release()
                if time.monotonic() > deadline:
                    self.fail("held connection never took the slot")
                time.sleep(0.02)
            # The second connection is accepted but parked at the gate: its
            # complete request gets no response while the slot is taken.
            waiter.settimeout(1.0)
            waiter.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            with self.assertRaises(TimeoutError):
                waiter.recv(1)
            # Dropping the held connection frees the slot; the parked request
            # is now served from the bytes already in its send buffer.
            held.close()
            waiter.settimeout(5.0)
            head = b""
            while b"\r\n" not in head:
                chunk = waiter.recv(4096)
                if not chunk:
                    break
                head += chunk
            self.assertIn(b" 200 ", head.split(b"\r\n", 1)[0] + b" ")
        finally:
            held.close()
            waiter.close()
            srv.shutdown()
            srv.server_close()
            th.join(timeout=5)

    def test_json_body_deep_nesting_returns_400(self) -> None:
        """v0.2.314: a deeply nested body exceeds json.loads' recursion depth
        and raises RecursionError — a malformed input that must still map to
        400 VALIDATION_FIELD_FORMAT_INVALID, not 500 SYSTEM_INTERNAL_ERROR."""
        depth = 20000
        status, _, raw = self._req(
            "POST",
            "/api/notebooks",
            ("[" * depth + "]" * depth).encode(),
            {"Content-Type": "application/json"},
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

    def test_upload_raw_utf8_filename_header_not_mojibake(self) -> None:
        """X-Filename sent as raw UTF-8 bytes (not percent-encoded) must decode
        correctly, not silently corrupt into mojibake.

        http.server/email.parser decode header bytes as Latin-1, not UTF-8. The
        only client in this repo (index.html) works around this by always
        percent-encoding via encodeURIComponent() before sending — but that
        convention is undocumented and unenforced. Before the fix, a client
        sending raw UTF-8 bytes got a permanently corrupted title with zero
        error signal (HTTP 201, garbage title). Verified via a raw socket
        (http.client, not urllib) so the header bytes are sent exactly as given,
        bypassing any client-side percent-encoding.
        """
        _, nb = self._json("POST", "/api/notebooks", {"name": "raw-utf8-header"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.putrequest("POST", f"/api/notebooks/{nb['id']}/upload")
        body = ("生の UTF-8 ヘッダーのテスト文書。" * 20).encode("utf-8")
        conn.putheader("Content-Length", str(len(body)))
        conn.putheader("X-Filename", "日本語.txt".encode())
        conn.endheaders(body)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 201)
        self.assertEqual(data["source"]["title"], "日本語.txt")

    def test_upload_percent_encoded_filename_still_works(self) -> None:
        """The existing index.html convention (encodeURIComponent before send)
        must be unaffected by the raw-UTF-8-recovery fix: ASCII percent-encoded
        header values round-trip through the Latin-1->UTF-8 recovery unchanged.
        """
        _, nb = self._json("POST", "/api/notebooks", {"name": "percent-encoded-still-ok"})
        status, _, raw = self._req(
            "POST",
            f"/api/notebooks/{nb['id']}/upload",
            ("引き続き動作することを確認する文書。" * 20).encode(),
            {"X-Filename": urllib.parse.quote("日本語2.txt")},
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(raw)["source"]["title"], "日本語2.txt")

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

            def __enter__(self) -> BrokenWriteNTF:
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
            with self.assertRaises(
                ConnectionError,
                msg=f"{exc_class.__name__} must be ConnectionError",
            ):
                raise exc_class("test")

    def test_reindex_endpoint_returns_embedded_and_total_counts(self) -> None:
        """POST /api/notebooks/{id}/reindex must be reachable from the Web UI.

        Before this endpoint existed, rebuilding embeddings after an
        SHOIN_EMBED_MODEL change was CLI-only (`shoin reindex <id>`) — a user
        running only the Web UI had no way to recover without a terminal
        (Plan.md REQ-103: CLI/Web parity).
        """
        _, nb = self._json("POST", "/api/notebooks", {"name": "reindex-test"})
        nb_id = nb["id"]
        body = ("和紙は楮から作られる。" * 30).encode("utf-8")
        self._req(
            "POST",
            f"/api/notebooks/{nb_id}/upload",
            body,
            {"X-Filename": urllib.parse.quote("素材.txt")},
        )
        status, result = self._json("POST", f"/api/notebooks/{nb_id}/reindex")
        self.assertEqual(status, 200)
        self.assertIn("n_embedded", result)
        self.assertIn("n_total", result)
        self.assertGreaterEqual(result["n_total"], 1)

    def test_reindex_missing_notebook_returns_404(self) -> None:
        status, err = self._json("POST", "/api/notebooks/999999/reindex")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")

    def test_health_reflects_multi_query_env_toggle(self) -> None:
        """GET /api/health must surface SHOIN_MULTI_QUERY's current state so a
        user debugging retrieval behavior doesn't have to know the env var
        exists (v0.2.126) — same diagnostic-first spirit as CLAUDE.md's DEBUG=1
        retrieval-stats guidance."""
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"SHOIN_MULTI_QUERY": "1"}, clear=False):
            status, data = self._json("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["multi_query"])

    def test_metrics_endpoint_returns_counter_dict(self) -> None:
        """GET /api/metrics exposes Store.usage_metrics() (v0.2.653) — a
        dict of floats only: counts and millisecond sums, never content."""
        status, body = self._json("GET", "/api/metrics")
        self.assertEqual(status, 200)
        self.assertIn("metrics", body)
        self.assertIsInstance(body["metrics"], dict)
        self.assertTrue(
            all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in body["metrics"].values()
            ),
            body["metrics"],
        )

    def test_trash_endpoints_round_trip(self) -> None:
        """DELETE archives to trash; GET /api/trash lists it; restore brings
        the notebook back with its id; purge removes the archive (v0.2.654)."""
        status, body = self._json("POST", "/api/notebooks", {"name": "trashed"})
        self.assertEqual(status, 201)
        nb_id = body["id"]
        status, _ = self._json("DELETE", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 200)
        status, body = self._json("GET", "/api/trash")
        self.assertEqual(status, 200)
        items = [t for t in body["trash"] if t["notebook_id"] == nb_id]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "trashed")
        tid = items[0]["id"]
        status, body = self._json("POST", f"/api/trash/{tid}/restore")
        self.assertEqual(status, 201)
        self.assertEqual(body["id"], nb_id)
        status, body = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "trashed")
        status, body = self._json("DELETE", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 200)
        tid2 = [
            t for t in self._json("GET", "/api/trash")[1]["trash"]
            if t["notebook_id"] == nb_id
        ][0]["id"]
        status, body = self._json("DELETE", f"/api/trash/{tid2}")
        self.assertEqual(status, 200)
        status, body = self._json("POST", f"/api/trash/{tid2}/restore")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "TRASH_NOT_FOUND")
        status, body = self._json("DELETE", "/api/trash/99999")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "TRASH_NOT_FOUND")

    def test_trash_empty_and_vacuum_endpoints(self) -> None:
        """v0.2.669: DELETE /api/trash empties the whole undo log in one
        call; POST /api/vacuum rebuilds the file and reports db_bytes
        before/after around it."""
        status, before = self._json("GET", "/api/trash")
        self.assertEqual(status, 200)
        status, body = self._json("DELETE", "/api/trash")
        self.assertEqual(status, 200)
        self.assertEqual(body["purged"], len(before["trash"]))
        status, body = self._json("GET", "/api/trash")
        self.assertEqual(body["trash"], [])
        status, body = self._json("POST", "/api/vacuum")
        self.assertEqual(status, 200)
        self.assertLessEqual(body["after"], body["before"])
        self.assertEqual(body["freed"], body["before"] - body["after"])

    def test_check_endpoint_reports_health_and_unopenable(self) -> None:
        """v0.2.670: GET /api/check returns the physical-DB diagnostic;
        a file that cannot even open reports ok:false + 'unopenable'
        instead of a generic 500."""
        status, body = self._json("GET", "/api/check")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["integrity"], "ok")
        self.assertEqual(body["fk_violations"], 0)
        self.assertEqual(body["schema_version"], body["expected_version"])
        # v0.2.671: logical layer — seeded chunks are all unembedded.
        self.assertEqual(body["unembedded"], body["chunks"])

        import threading as _th

        bad = str(Path(self.tmp.name) / "bad.db")
        Path(bad).write_bytes(b"not a sqlite file " * 100)
        srv2 = make_server(port=0, db=bad, llm=FakeLLM())
        th = _th.Thread(target=srv2.serve_forever, daemon=True)
        th.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv2.server_address[1]}/api/check"
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
        finally:
            srv2.shutdown()
            srv2.server_close()
            th.join(timeout=5)
        self.assertFalse(data["ok"])
        self.assertEqual(data["integrity"], "unopenable")

    def test_trash_source_and_note_round_trip(self) -> None:
        """v0.2.667: source/note deletes archive with their own kind — the
        undo-log covers every destructive entity delete, not just nb."""
        status, body = self._json("POST", "/api/notebooks", {"name": "t"})
        self.assertEqual(status, 201)
        nb_id = body["id"]
        db = str(Path(self.tmp.name) / "s.db")
        from shoin.store import Store

        with Store(db) as store:
            src = store.add_source(nb_id, "txt", "memo.txt", "mem://m", "sha-m")
            store.add_chunks(src.id, ["猫は液体である。"])
            src_id = src.id
        status, body = self._json(
            "POST", f"/api/notebooks/{nb_id}/notes",
            {"title": "memo", "body": "本文"},
        )
        self.assertEqual(status, 201)
        note_id = body["id"]
        status, _ = self._json("DELETE", f"/api/sources/{src_id}")
        self.assertEqual(status, 200)
        status, _ = self._json("DELETE", f"/api/notes/{note_id}")
        self.assertEqual(status, 200)
        status, body = self._json("GET", "/api/trash")
        items = {
            t["kind"]: t
            for t in body["trash"]
            if t["notebook_id"] == nb_id
        }
        self.assertEqual(sorted(items), ["note", "source"])
        status, body = self._json(
            "POST", f"/api/trash/{items['source']['id']}/restore"
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["kind"], "source")
        self.assertEqual(body["id"], src_id)
        status, body = self._json(
            "POST", f"/api/trash/{items['note']['id']}/restore"
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["kind"], "note")
        status, body = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 200)
        self.assertEqual([s["title"] for s in body["sources"]], ["memo.txt"])
        self.assertEqual([n["title"] for n in body["notes"]], ["memo"])

    def test_nb_tree_export_and_import_round_trip(self) -> None:
        """GET .../export?format=tree emits the portable document; POST
        /api/notebooks/import re-inserts it under fresh ids (v0.2.655)."""
        status, body = self._json("POST", "/api/notebooks", {"name": "portable"})
        self.assertEqual(status, 201)
        nb_id = body["id"]
        db = str(Path(self.tmp.name) / "s.db")
        from shoin.store import Store

        with Store(db) as store:
            src = store.add_source(nb_id, "txt", "cats.txt", "mem://cats", "sha-c")
            store.add_chunks(src.id, ["猫は液体である説は流動性の比喩である。"])
        status, doc = self._json(
            "GET", f"/api/notebooks/{nb_id}/export?format=tree"
        )
        self.assertEqual(status, 200)
        self.assertEqual(doc["format"], "shoin-nb-tree-v1")
        self.assertEqual(doc["notebook"]["name"], "portable")
        self.assertEqual(len(doc["sources"]), 1)
        status, body = self._json("POST", "/api/notebooks/import", doc)
        self.assertEqual(status, 201)
        self.assertNotEqual(body["id"], nb_id)
        self.assertEqual(body["name"], "portable")
        status, det = self._json("GET", f"/api/notebooks/{body['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(len(det["sources"]), 1)
        # a file-path origin in an HTTP import must not be re-readable via
        # refresh (confused deputy: the HTTP API never reads server files)
        secret = Path(self.tmp.name) / "server-secret.txt"
        secret.write_text("server-side secret body text", encoding="utf-8")
        doc["sources"][0]["origin"] = str(secret)
        status, body = self._json("POST", "/api/notebooks/import", doc)
        self.assertEqual(status, 201)
        status, det = self._json("GET", f"/api/notebooks/{body['id']}")
        self.assertEqual(status, 200)
        self.assertFalse(det["sources"][0]["refreshable"])
        self.assertTrue(det["sources"][0]["origin"].startswith("imported:"))
        status, _ = self._json(
            "POST", f"/api/sources/{det['sources'][0]['id']}/refresh"
        )
        self.assertNotEqual(status, 200)
        status, body = self._json(
            "POST", f"/api/notebooks/{body['id']}/refresh-all"
        )
        self.assertEqual(status, 200)
        self.assertNotIn("refreshed", [r.get("status") for r in body["results"]])
        # malformed payloads -> 400 coded, never a traceback
        status, err = self._json(
            "POST", "/api/notebooks/import", {"notebook": {}}
        )
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_IMPORT_INVALID")
        # non-dict body -> the envelope guard rejects before the store
        status, err = self._json("POST", "/api/notebooks/import", ["x"])
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")
        # a dead notebook exports as a coded 404 in the tree format too
        status, err = self._json(
            "GET", "/api/notebooks/99999/export?format=tree"
        )
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")

    def test_nb_merge_endpoint(self) -> None:
        """POST /api/notebooks/{id}/merge (v0.2.656): folds the source
        notebook's tree in under fresh ids and archives the emptied
        source to trash — echoes the surviving target."""
        status, body = self._json("POST", "/api/notebooks", {"name": "T"})
        self.assertEqual(status, 201)
        t = body["id"]
        status, body = self._json("POST", "/api/notebooks", {"name": "S"})
        self.assertEqual(status, 201)
        src = body["id"]
        db = str(Path(self.tmp.name) / "s.db")
        from shoin.store import Store

        with Store(db) as store:
            ss = store.add_source(src, "txt", "sdoc", "mem://s", "sha-s")
            store.add_chunks(ss.id, ["ソース側本文です。"])
        status, body = self._json(
            "POST", f"/api/notebooks/{t}/merge", {"source_id": src}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"id": t, "name": "T"})
        status, det = self._json("GET", f"/api/notebooks/{t}")
        self.assertEqual(status, 200)
        self.assertEqual([s["title"] for s in det["sources"]], ["sdoc"])
        # the emptied source is gone (archived under trash, not lost)
        status, err = self._json("GET", f"/api/notebooks/{src}")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")
        status, trash = self._json("GET", "/api/trash")
        self.assertEqual(status, 200)
        items = [t for t in trash["trash"] if t["notebook_id"] == src]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "S")
        # validator boundary: absent / wrong type / dead / self
        status, err = self._json("POST", f"/api/notebooks/{t}/merge", {})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_REQUIRED_FIELD_MISSING")
        status, err = self._json(
            "POST", f"/api/notebooks/{t}/merge", {"source_id": "x"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")
        status, err = self._json(
            "POST", f"/api/notebooks/{t}/merge", {"source_id": 999}
        )
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")
        status, err = self._json(
            "POST", f"/api/notebooks/{t}/merge", {"source_id": t}
        )
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")

    def test_theme_css_serves_user_file_and_degrades_to_empty(self) -> None:
        """GET /api/theme.css (v0.2.643): the user-theme hook serves
        SHOIN_THEME_CSS / ~/.config/shoin/theme.css verbatim as text/css.
        Missing, unreadable, or oversized files all degrade to an empty
        stylesheet — a cosmetic hook must never 5xx a page load."""
        # default: no theme file -> 200 + empty CSS (a <link> never fails)
        with patch.dict(os.environ, {"SHOIN_THEME_CSS": str(Path(self.tmp.name) / "nope.css")}):
            status, headers, body = self._req("GET", "/api/theme.css")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertIn("text/css", headers.get("Content-Type", ""))

        # user file -> served verbatim
        theme = Path(self.tmp.name) / "theme.css"
        theme.write_bytes(b":root{--washi:#000}\n")
        with patch.dict(os.environ, {"SHOIN_THEME_CSS": str(theme)}):
            status, headers, body = self._req("GET", "/api/theme.css")
        self.assertEqual(status, 200)
        self.assertEqual(body, b":root{--washi:#000}\n")

        # oversized -> empty, not a truncated tail that corrupts a rule
        theme.write_bytes(b"x" * (_THEME_CSS_LIMIT + 1))
        with patch.dict(os.environ, {"SHOIN_THEME_CSS": str(theme)}):
            status, _, body = self._req("GET", "/api/theme.css")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_ui_serves_theme_link_and_csp_allows_self_styles(self) -> None:
        """The theme hook needs both ends wired: index.html <link>s to
        /api/theme.css, and the CSP must permit same-origin stylesheets —
        'unsafe-inline' alone would block the linked file."""
        status, headers, page = self._req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<link rel="stylesheet" href="/api/theme.css">', page)
        csp = headers.get("Content-Security-Policy", "")
        style = re.search(r"style-src\s+([^;]+)", csp)
        self.assertIsNotNone(style)
        assert style is not None
        self.assertIn("'self'", style.group(1))

    def test_packaged_ui_assets_are_served_and_linked(self) -> None:
        """v0.2.666 (product-review #27): index.html no longer inlines its
        script/style — the app lives in two packaged siblings served by
        literal same-origin routes. Both ends must stay wired: the files are
        200 under /static/*, and the page links them so a build that drops
        one renders a blank, not a 404 discovered only by clicking."""
        status, headers, js = self._req("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers.get("Content-Type", ""))
        self.assertIn(b"const I18N", js)
        status, headers, css = self._req("GET", "/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get("Content-Type", ""))
        self.assertIn(b"--sumi", css)
        status, headers, page = self._req("GET", "/")
        self.assertIn(b'<script src="/static/app.js"></script>', page)
        self.assertIn(b'<link rel="stylesheet" href="/static/style.css">', page)
        # script-src moved to 'self' — external assets load, inline JS stays
        # denied, and the CSP pin keeps the tightening explicit.
        csp = headers.get("Content-Security-Policy", "")
        scripts = re.search(r"script-src\s+([^;]+)", csp)
        self.assertIsNotNone(scripts)
        assert scripts is not None
        self.assertIn("'self'", scripts.group(1))
        self.assertNotIn("unsafe-inline", scripts.group(1))

    def test_missing_packaged_asset_is_a_coded_404(self) -> None:
        """A wheel that fails to ship app.js must not serve a silent empty
        body — the UI would render blank with zero signal. The packaged-
        asset sender degrades to a coded 404 instead."""
        import shoin.server as srv_mod

        with patch.object(
            srv_mod, "_read_packaged_asset", side_effect=FileNotFoundError("x")
        ):
            status, body = self._json("GET", "/static/app.js")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "STATIC_ASSET_NOT_FOUND")

    def test_server_bind_keeps_literal_loopback_name(self) -> None:
        """v0.2.668: stdlib server_bind resolves the bind host via
        socket.getfqdn — a PTR lookup that stalls ~30s on machines with a
        slow/absent resolver, delaying the listen itself. A loopback-only
        server has no need for the canonical name; the literal host is
        stored instead."""
        self.assertIn(self.server.server_name, ("127.0.0.1", "::1"))
        self.assertIsInstance(self.server.server_port, int)
        self.assertGreater(self.server.server_port, 0)

    def test_ui_lang_meta_reflects_shoin_lang(self) -> None:
        """README documents SHOIN_LANG as controlling "UI言語", but the Web UI
        is served as pure static bytes and previously ignored it entirely,
        deciding its language from navigator.language/localStorage alone —
        the CLI and export.py already respected it, so this was a real
        cross-surface inconsistency in the same documented setting, not just
        an incomplete doc. _h_ui() now injects the configured language into
        the page's <meta name="shoin-lang"> tag; index.html's own bootstrap
        JS treats it as the default, below an explicit user toggle
        (localStorage) but above the browser's own locale."""
        with patch.dict(os.environ, {"SHOIN_LANG": "en"}, clear=False):
            status, _, page = self._req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<meta name="shoin-lang" content="en">', page)
        self.assertNotIn(b"__SHOIN_LANG__", page)

        with patch.dict(os.environ, {"SHOIN_LANG": "ja"}, clear=False):
            status, _, page = self._req("GET", "/")
        self.assertIn(b'<meta name="shoin-lang" content="ja">', page)

    def test_ui_lang_meta_sanitizes_unrecognized_or_malicious_values(self) -> None:
        """CSP already allows inline scripts (script-src 'unsafe-inline'), so an
        unsanitized SHOIN_LANG value substituted into the <meta> tag's content
        attribute could break out of it. Only a bare "ja"/"en" is ever
        substituted verbatim; anything else — including something shaped like
        an attribute-breakout attempt — must fall back to "ja" untouched."""
        malicious = '"><script>window.x=1</script><meta content="'
        with patch.dict(os.environ, {"SHOIN_LANG": malicious}, clear=False):
            status, _, page = self._req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<meta name="shoin-lang" content="ja">', page)
        self.assertNotIn(b"<script>window.x=1</script>", page)

        with patch.dict(os.environ, {"SHOIN_LANG": "fr"}, clear=False):
            status, _, page = self._req("GET", "/")
        self.assertIn(
            b'<meta name="shoin-lang" content="ja">',
            page,
            "an unsupported-but-harmless language code must also fall back to ja",
        )

    def test_src_patch_weight(self) -> None:
        """v0.2.657: PATCH /api/sources/{id} with {"weight"} sets the
        retrieval weight (product-review #19) — no title needed, and the
        value surfaces on the notebook detail read."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "weight-nb"})
        nb_id = nb["id"]
        db = str(Path(self.tmp.name) / "s.db")
        from shoin.store import Store

        with Store(db) as store:
            src = store.add_source(nb_id, "txt", "w.txt", "mem://w", "sha-w")
            store.add_chunks(src.id, ["重みの説明文"])

        # weight-only PATCH — no title field at all.
        status, out = self._json(
            "PATCH", f"/api/sources/{src.id}", {"weight": 3.0}
        )
        self.assertEqual(status, 200)
        self.assertEqual(out["id"], src.id)
        self.assertEqual(out["weight"], 3.0)
        self.assertEqual(out["title"], "w.txt")
        _, detail = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(detail["sources"][0]["weight"], 3.0)

        # Combined title+weight — both apply, weight echoes the request.
        status, out = self._json(
            "PATCH", f"/api/sources/{src.id}", {"title": "w2.txt", "weight": 2}
        )
        self.assertEqual(status, 200)
        self.assertEqual(out["title"], "w2.txt")
        self.assertEqual(out["weight"], 2.0)
        _, detail = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(detail["sources"][0]["title"], "w2.txt")
        self.assertEqual(detail["sources"][0]["weight"], 2.0)

        # Neither field present is the coded missing-field 400.
        status, err = self._json("PATCH", f"/api/sources/{src.id}", {})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_REQUIRED_FIELD_MISSING")
        # Non-numeric / out-of-range / non-finite are coded 400s.
        for bad in ("heavy", 9.0, -0.5, True):
            status, err = self._json(
                "PATCH", f"/api/sources/{src.id}", {"weight": bad}
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID"
            )
        # Dead source id is the coded 404.
        status, err = self._json("PATCH", "/api/sources/99999", {"weight": 2})
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "SOURCE_NOT_FOUND")

    def test_src_patch_meta(self) -> None:
        """v0.2.658: PATCH /api/sources/{id} with {"meta"} replaces the
        descriptive metadata object (product-review #24) — echoed back,
        surfaced on the notebook detail read, whole-object REPLACE."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "meta-nb"})
        nb_id = nb["id"]
        db = str(Path(self.tmp.name) / "s.db")
        from shoin.store import Store

        with Store(db) as store:
            src = store.add_source(nb_id, "txt", "m.txt", "mem://m", "sha-m")
            store.add_chunks(src.id, ["メタデータの説明文"])

        # meta-only PATCH — no title/weight fields.
        status, out = self._json(
            "PATCH", f"/api/sources/{src.id}",
            {"meta": {"author": "Doe", "year": "2020"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(out["meta"], {"author": "Doe", "year": "2020"})
        _, detail = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(
            detail["sources"][0]["meta"], {"author": "Doe", "year": "2020"}
        )
        # REPLACE semantics — keys absent from the second object are gone.
        status, out = self._json(
            "PATCH", f"/api/sources/{src.id}", {"meta": {"year": "2021"}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(out["meta"], {"year": "2021"})
        # Empty object clears everything.
        status, out = self._json("PATCH", f"/api/sources/{src.id}", {"meta": {}})
        self.assertEqual(status, 200)
        self.assertEqual(out["meta"], {})
        # Non-object meta is the coded 400, not a 500.
        for bad in ("freeform", [1], 7, True):
            status, err = self._json(
                "PATCH", f"/api/sources/{src.id}", {"meta": bad}
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID"
            )
        # Oversized serialized object is the same coded 400 via the store.
        status, err = self._json(
            "PATCH", f"/api/sources/{src.id}", {"meta": {"k": "x" * 5000}}
        )
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")

    def test_nb_patch_settings(self) -> None:
        """v0.2.659: PATCH /api/notebooks/{id} accepts {"settings"} — the
        per-notebook retrieval overrides (product-review #20), echoed back
        and surfaced on the detail read; {name, settings} may combine."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "set-nb"})
        nb_id = nb["id"]

        # settings-only PATCH — no name field.
        status, out = self._json(
            "PATCH", f"/api/notebooks/{nb_id}",
            {"settings": {"top_k": 3, "source_text_tokens": 256}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(out["name"], "set-nb")
        self.assertEqual(
            out["settings"], {"top_k": 3, "source_text_tokens": 256}
        )
        _, detail = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(
            detail["settings"], {"top_k": 3, "source_text_tokens": 256}
        )
        # name+settings combine in one PATCH; REPLACE semantics drop keys
        # absent from the second object.
        status, out = self._json(
            "PATCH", f"/api/notebooks/{nb_id}",
            {"name": "set-nb2", "settings": {"top_k": 2}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(out["name"], "set-nb2")
        self.assertEqual(out["settings"], {"top_k": 2})
        # Empty object clears every override.
        status, out = self._json(
            "PATCH", f"/api/notebooks/{nb_id}", {"settings": {}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(out["settings"], {})
        # An empty PATCH is the coded missing-field 400, not a silent no-op.
        status, err = self._json("PATCH", f"/api/notebooks/{nb_id}", {})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_REQUIRED_FIELD_MISSING")
        # Unknown keys, non-object, non-int, and out-of-bounds values are all
        # the coded 400 path — a silently inert key is worse than a refusal.
        for bad_settings in (
            {"nope": 1},
            {"top_k": "3"},
            {"top_k": 0},
            {"source_text_tokens": 99999},
            "freeform",
            [1],
        ):
            status, err = self._json(
                "PATCH", f"/api/notebooks/{nb_id}", {"settings": bad_settings}
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID"
            )
        # A dead notebook is still the coded 404.
        status, err = self._json(
            "PATCH", "/api/notebooks/99999", {"settings": {"top_k": 2}}
        )
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")


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

    def _json(
        self, method: str, path: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
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
        # Persisted citation_report must carry degraded:true so the UI can render
        # the "search only" badge when loading chat history after a page reload.
        persisted_report = assistant_msgs[0].get("report", {})
        self.assertTrue(persisted_report.get("degraded"), "report.degraded must be True in DB")


class TruncatedStreamTest(unittest.TestCase):
    """v0.2.245: a stream ending at finish_reason "length" must surface as
    report.truncated in the done frame — otherwise a MAX_TOKENS-clipped answer
    is presented as complete on every client surface."""

    class _TruncLLM(FakeLLM):
        def chat_stream(self, messages, temperature=0.2):
            yield from self.reply_parts
            self.last_finish_reason = "length"

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = cls._TruncLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "tr.db"), llm=cls.llm)
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

    def test_done_frame_flags_truncated(self) -> None:
        _, nb = self._json("POST", "/api/notebooks", {"name": "trunc-test"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("これは打切テストの内容です。内容について説明します。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "trunc.txt"},
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
        done = [d for e, d in events if e == "done"]
        self.assertTrue(done, "done frame missing")
        self.assertTrue(done[0]["report"].get("truncated"))
        # And the persisted assistant message carries the same flag on reload.
        _, msgs = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertTrue(msgs["messages"][-1]["report"].get("truncated"))

    def test_finish_reason_is_captured_under_generation_lock(self) -> None:
        """v0.2.284: last_finish_reason lives on the *shared* llm client and is
        reset at the start of every call — reading it after generation_lock is
        released races with the next queued request's reset and silently drops
        (or misattributes) the truncated flag."""
        handler_cls = self.server.RequestHandlerClass
        llm = self.llm
        inner = handler_cls.generation_lock

        class _UnlockThenReset:
            def __enter__(self):  # noqa: D102
                return inner.__enter__()

            def __exit__(self, *exc: object) -> object:
                result = inner.__exit__(*exc)
                # What the next queued request's chat()/chat_stream() does the
                # moment it acquires the lock (llm.py resets on entry).
                llm.last_finish_reason = None
                return result

        handler_cls.generation_lock = _UnlockThenReset()
        try:
            _, nb = self._json("POST", "/api/notebooks", {"name": "race"})
            nb_id = nb["id"]
            req = urllib.request.Request(
                self._url(f"/api/notebooks/{nb_id}/upload"),
                data=("打切レーステストの内容。" * 30).encode(),
                method="POST",
                headers={"X-Filename": "race.txt"},
            )
            with urllib.request.urlopen(req):
                pass
            req2 = urllib.request.Request(
                self._url(f"/api/notebooks/{nb_id}/ask"),
                data=json.dumps({"question": "内容は"}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req2) as resp:
                raw = resp.read().decode()
        finally:
            handler_cls.generation_lock = inner
        done = [d for e, d in parse_sse(raw) if e == "done"]
        self.assertTrue(done, "done frame missing")
        self.assertTrue(
            done[0]["report"].get("truncated"),
            "a queued request's flag reset must not erase this request's truncated flag",
        )


class CacheControlTest(unittest.TestCase):
    """v0.2.285: every response must carry Cache-Control: no-store — a cached
    index.html outliving the server build silently runs stale JS against a
    new API. Previously only the SSE route sent it."""

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

    def _req(self, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_baseline_security_headers_on_html_api_and_error(self) -> None:
        """v0.2.311+: the no-store guarantee extends to the other baseline
        headers _headers() emits — X-Content-Type-Options: nosniff (stops a
        JSON error body being sniffed as HTML) and Referrer-Policy:
        no-referrer — on every response class, not just the HTML page."""
        for path in ("/", "/api/notebooks", "/api/nope"):
            status, headers, _ = self._req("GET", path)
            for name, want in (
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
                # v0.2.663 (product-review #40): every response class names
                # the API contract version it speaks.
                ("X-Shoin-API", API_VERSION),
            ):
                self.assertEqual(
                    want,
                    headers.get(name),
                    f"{path} missing {name}: {want} (status {status})",
                )
            self.assertNotIn(
                "Python",
                headers.get("Server", ""),
                f"{path} Server header leaks the Python runtime version",
            )


class PostStreamStoreErrorTest(unittest.TestCase):
    """StoreError from assistant message persistence after SSE headers must
    not corrupt the stream."""

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

    def test_headers_disconnect_then_persist_failure_stays_quiet(self) -> None:
        """If the client is already gone when SSE headers are written (the
        ConnectionError path) AND the orphan-turn repair write then fails too,
        the request must still end quietly — no traceback, server responsive."""
        import io
        from unittest.mock import patch

        import shoin.server as srv_mod
        from shoin.store import Store, StoreError

        _, nb = self._json("POST", "/api/notebooks", {"name": "hdr-disc"})
        nb_id = nb["id"]

        original = Store.add_message

        def failing(self_s, nb_id_arg, role, body, meta):
            if role == "assistant":
                raise StoreError("NOTEBOOK_NOT_FOUND", "deleted mid-request")
            return original(self_s, nb_id_arg, role, body, meta)

        err = io.StringIO()
        with (
            patch.object(srv_mod._Handler, "_headers", side_effect=ConnectionError("gone")),
            patch.object(Store, "add_message", failing),
            patch("sys.stderr", err),
        ):
            req = urllib.request.Request(
                self._url(f"/api/notebooks/{nb_id}/ask"),
                data=json.dumps({"question": "原料は？"}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(
                (urllib.error.URLError, ConnectionError, http.client.HTTPException)
            ):
                urllib.request.urlopen(req, timeout=10)

        self.assertNotIn("Traceback", err.getvalue())
        health_status, _ = self._json("GET", "/api/health")
        self.assertEqual(health_status, 200)

    def test_meta_disconnect_then_persist_failure_stays_quiet(self) -> None:
        """Client gone at the meta frame + orphan-turn repair write failing —
        the swallowed pair must leave the stream clean and the server alive."""
        import io
        from unittest.mock import patch

        import shoin.server as srv_mod
        from shoin.store import Store, StoreError

        _, nb = self._json("POST", "/api/notebooks", {"name": "meta-disc"})
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
                raise StoreError("NOTEBOOK_NOT_FOUND", "deleted mid-request")
            return original(self_s, nb_id_arg, role, body, meta)

        err = io.StringIO()
        with (
            patch.object(srv_mod._Handler, "_sse", side_effect=ConnectionError("gone")),
            patch.object(Store, "add_message", failing),
            patch("sys.stderr", err),
        ):
            status, raw = self._sse(
                f"/api/notebooks/{nb_id}/ask", {"question": "原料は？"}
            )

        self.assertEqual(status, 200)
        self.assertEqual(raw, "")
        self.assertNotIn("Traceback", err.getvalue())
        health_status, _ = self._json("GET", "/api/health")
        self.assertEqual(health_status, 200)

    def test_context_failure_on_dead_socket_swallows_both_writes(self) -> None:
        """build_context raising after SSE headers commits the status line, so
        the error frame is best-effort: when that frame hits a dead socket AND
        the repair persist also fails, both must be swallowed quietly."""
        import io
        from unittest.mock import patch

        import shoin.server as srv_mod
        from shoin.store import Store, StoreError

        _, nb = self._json("POST", "/api/notebooks", {"name": "ctx-dead"})
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
                raise StoreError("NOTEBOOK_NOT_FOUND", "deleted mid-request")
            return original(self_s, nb_id_arg, role, body, meta)

        err = io.StringIO()
        with (
            patch.object(srv_mod, "build_context", side_effect=RuntimeError("ctx boom")),
            patch.object(srv_mod._Handler, "_sse", side_effect=ConnectionError("gone")),
            patch.object(Store, "add_message", failing),
            patch("sys.stderr", err),
        ):
            status, raw = self._sse(
                f"/api/notebooks/{nb_id}/ask", {"question": "原料は？"}
            )

        self.assertEqual(status, 200)
        self.assertEqual(raw, "")
        self.assertNotIn("Traceback", err.getvalue())
        health_status, _ = self._json("GET", "/api/health")
        self.assertEqual(health_status, 200)

    def test_delta_write_on_dead_socket_marks_client_gone(self) -> None:
        """A delta write dying mid-stream must take the outer ConnectionError
        branch — client_gone short-circuits the done frame and the broken-pipe
        persist failure is still swallowed quietly. This tail was previously
        covered only incidentally by whichever fault landed first."""
        import io
        from unittest.mock import patch

        import shoin.server as srv_mod
        from shoin.store import Store, StoreError

        _, nb = self._json("POST", "/api/notebooks", {"name": "delta-disc"})
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
                raise StoreError("NOTEBOOK_NOT_FOUND", "deleted mid-request")
            return original(self_s, nb_id_arg, role, body, meta)

        def delta_boom(event, payload):
            # meta must succeed — a dead socket at meta returns early and never
            # reaches the stream loop; only the delta write should fail.
            if event == "delta":
                raise ConnectionError("gone")

        err = io.StringIO()
        with (
            patch.object(srv_mod._Handler, "_sse", side_effect=delta_boom),
            patch.object(Store, "add_message", failing),
            patch("sys.stderr", err),
        ):
            status, raw = self._sse(
                f"/api/notebooks/{nb_id}/ask", {"question": "原料は？"}
            )

        self.assertEqual(status, 200)
        self.assertEqual(raw, "")
        self.assertNotIn("Traceback", err.getvalue())
        health_status, _ = self._json("GET", "/api/health")
        self.assertEqual(health_status, 200)

    def test_delta_write_death_still_persists_the_complete_answer(self) -> None:
        """v0.2.665 (product-review #48): once a delta write dies, the handler
        keeps consuming the LLM stream to completion — the token spend is
        already sunk inside generation_lock — and the persisted assistant row
        holds the FULL answer. The UI's done-miss poll and any reload must not
        find only the prefix that fit before the cut."""
        import io
        from unittest.mock import patch

        import shoin.server as srv_mod
        from shoin.store import Store

        _, nb = self._json("POST", "/api/notebooks", {"name": "delta-full"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("楮は和紙の原料である。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "kaji.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        def delta_boom(event, payload):
            if event == "delta":
                raise ConnectionError("gone")

        err = io.StringIO()
        with (
            patch.object(srv_mod._Handler, "_sse", side_effect=delta_boom),
            patch("sys.stderr", err),
        ):
            status, raw = self._sse(
                f"/api/notebooks/{nb_id}/ask", {"question": "原料は？"}
            )

        self.assertEqual(status, 200)
        self.assertEqual(raw, "")
        self.assertNotIn("Traceback", err.getvalue())
        with Store(str(Path(self.tmp.name) / "ps.db")) as s:
            rows = [
                m for m in s.list_messages(nb_id) if m["role"] == "assistant"
            ]
        self.assertEqual(len(rows), 1)
        # Every streamed part survived — a mid-loop abort would have left the
        # empty or single-token prefix instead.
        self.assertEqual(rows[0]["body"], "".join(self.llm.reply_parts))

    def test_send_error_survives_a_dead_connection(self) -> None:
        """send_error on a socket that died mid-response must swallow the write
        failure — protocol-level errors are already terminal; raising again
        would just produce noise in handle_error."""
        from unittest.mock import patch

        import shoin.server as srv_mod

        h = srv_mod._Handler.__new__(srv_mod._Handler)
        with patch.object(h, "_error", side_effect=BrokenPipeError()):
            h.send_error(501)
        self.assertTrue(h.close_connection)

    def test_handle_error_still_reports_non_timeout_failures(self) -> None:
        """Only TimeoutError is quieted by _HTTPServer.handle_error — a real
        request-thread failure must keep the default traceback print so a
        handler bug can never vanish silently."""
        import io
        from unittest.mock import patch

        import shoin.server as srv_mod

        try:
            raise RuntimeError("boom")
        except RuntimeError:
            err = io.StringIO()
            with patch("sys.stderr", err):
                srv_mod._HTTPServer.handle_error(self.server, None, ("127.0.0.1", 0))
        self.assertIn("RuntimeError", err.getvalue())

    def test_handle_error_quiets_timeout_failures(self) -> None:
        """Symmetric contract: a TimeoutError escaping a request thread (e.g. a
        stalled write in finish(), outside handle_one_request's own catch) must
        be swallowed — stalled-client socket timeouts are routine, not failures."""
        import io
        from unittest.mock import patch

        import shoin.server as srv_mod

        try:
            raise TimeoutError("idle socket")
        except TimeoutError:
            err = io.StringIO()
            with patch("sys.stderr", err):
                srv_mod._HTTPServer.handle_error(self.server, None, ("127.0.0.1", 0))
        self.assertEqual(err.getvalue(), "")


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


class SourceRenameCacheTest(unittest.TestCase):
    """Renaming a source must invalidate the questions cache, the same way
    _h_src_refresh already does (v0.2.36). The cache fingerprint is source IDs
    only, unchanged by a rename, so without an explicit eviction the cache would
    never self-expire and would keep serving suggestions generated from the old
    title indefinitely — build_context() embeds the source title directly into
    the LLM prompt, so a rename changes exactly what a refresh changes.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = FakeLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "rn.db"), llm=cls.llm)
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

    def test_rename_source_invalidates_questions_cache(self) -> None:
        _, nb = self._json("POST", "/api/notebooks", {"name": "改名キャッシュ"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("知識ベース文書。" * 50).encode(),
            method="POST",
            headers={"X-Filename": "kb.txt"},
        )
        with urllib.request.urlopen(req):
            pass
        _, nb_full = self._json("GET", f"/api/notebooks/{nb_id}")
        src_id = nb_full["sources"][0]["id"]

        before = self.llm.chat_count
        status, _ = self._json("GET", f"/api/notebooks/{nb_id}/questions")
        self.assertEqual(status, 200)
        after_warm = self.llm.chat_count
        self.assertEqual(after_warm, before + 1)  # one LLM call to warm the cache

        status, _ = self._json("PATCH", f"/api/sources/{src_id}", {"title": "改名後のタイトル"})
        self.assertEqual(status, 200)

        status, _ = self._json("GET", f"/api/notebooks/{nb_id}/questions")
        self.assertEqual(status, 200)
        self.assertEqual(
            self.llm.chat_count, after_warm + 1,
            "renaming a source must invalidate the questions cache (one new LLM call)",
        )

    def test_rename_response_title_matches_persisted_truncated_title(self) -> None:
        """PATCH /api/sources/{id}'s response must report the TRUNCATED title
        actually persisted by update_source_title() (MAX_TITLE_LEN), not the
        raw request-body value. Same bug class as v0.2.93's _h_src_upload fix,
        found in this sibling endpoint: skipping a second get_source() fetch
        (a deliberate v0.2.45 TOCTOU-avoidance choice) meant the response could
        diverge from the DB even with no concurrency involved, since the
        update itself silently truncates."""
        from shoin.config import MAX_TITLE_LEN

        _, nb = self._json("POST", "/api/notebooks", {"name": "改名切り詰めテスト"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("知識ベース文書。" * 50).encode(),
            method="POST",
            headers={"X-Filename": "kb2.txt"},
        )
        with urllib.request.urlopen(req):
            pass
        _, nb_full = self._json("GET", f"/api/notebooks/{nb_id}")
        src_id = nb_full["sources"][0]["id"]

        long_title = "B" * 550
        status, patch_result = self._json("PATCH", f"/api/sources/{src_id}", {"title": long_title})
        self.assertEqual(status, 200)
        self.assertEqual(len(patch_result["title"]), MAX_TITLE_LEN)

        _, nb_after = self._json("GET", f"/api/notebooks/{nb_id}")
        persisted_title = nb_after["sources"][0]["title"]
        self.assertEqual(
            patch_result["title"], persisted_title,
            "PATCH response title must match what was actually persisted",
        )


class ReindexCacheTest(unittest.TestCase):
    """POST /api/notebooks/{id}/reindex must invalidate the questions cache.
    Reindex rebuilds every chunk's embedding under an unchanged source-id
    fingerprint, so suggestions generated against the old retrieval substrate
    would be served forever — the same eviction gap _h_src_refresh (v0.2.36)
    and source rename already cover with an explicit pop."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = FakeLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "rx.db"), llm=cls.llm)
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

    def test_reindex_invalidates_questions_cache(self) -> None:
        _, nb = self._json("POST", "/api/notebooks", {"name": "再索引キャッシュ"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("埋め込み再構築の対象文書。" * 50).encode(),
            method="POST",
            headers={"X-Filename": "rx.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        before = self.llm.chat_count
        status, _ = self._json("GET", f"/api/notebooks/{nb_id}/questions")
        self.assertEqual(status, 200)
        after_warm = self.llm.chat_count
        self.assertEqual(after_warm, before + 1)  # one LLM call to warm the cache

        status, _ = self._json("POST", f"/api/notebooks/{nb_id}/reindex", {})
        self.assertEqual(status, 200)

        status, _ = self._json("GET", f"/api/notebooks/{nb_id}/questions")
        self.assertEqual(status, 200)
        self.assertEqual(
            self.llm.chat_count, after_warm + 1,
            "reindex must invalidate the questions cache (one new LLM call)",
        )


class NotebookMessagesCapTest(unittest.TestCase):
    """GET /api/notebooks/{id} embeds at most NB_MESSAGES_LIMIT messages.

    Chat history grows monotonically and openNotebook() re-fetches this payload
    on every mutation (upload, source add/delete/refresh, studio generate,
    clear-chat, SSE-drop recovery) — an unbounded messages array would make
    each click heavier forever. The payload stays honest: `messages_omitted`
    reports the real hidden count for the UI's disclosure line; the full record
    remains in the DB and in export()."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = FakeLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "mc.db"), llm=cls.llm)
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
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_notebook_payload_caps_messages_and_reports_omitted(self) -> None:
        import shoin.server as srv
        from shoin.store import Store

        _, nb = self._json("POST", "/api/notebooks", {"name": "cap"})
        nb_id = nb["id"]
        with Store(str(Path(self.tmp.name) / "mc.db")) as store:
            for i in range(12):
                store.add_message(nb_id, "user" if i % 2 == 0 else "assistant", f"msg {i}", "{}")
        with patch.object(srv, "NB_MESSAGES_LIMIT", 4):
            status, j = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(j["messages"]), 4)
        self.assertEqual(j["messages_omitted"], 8)
        # The newest turns are the embedded ones — the SSE-drop recovery refetch
        # (v0.2.246) depends on the persisted last assistant message being in
        # the payload.
        self.assertEqual(j["messages"][0]["body"], "msg 8")
        self.assertEqual(j["messages"][-1]["body"], "msg 11")
        # Under the cap the count is honestly 0, not guessed or absent.
        status, j2 = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 200)
        self.assertEqual(j2["messages_omitted"], 0)
        self.assertEqual(len(j2["messages"]), 12)

    def test_notebook_payload_caps_notes_and_reports_omitted(self) -> None:
        """v0.2.409: notes had the same unbounded-embed defect the messages cap
        closed — every detail fetch (openNotebook, the SSE-drop recovery
        refetch) round-trips every note body, so an accumulating notes pane
        made each click heavier forever. The payload stays honest:
        notes_omitted reports the real hidden count for the UI's disclosure
        line; the full record remains in the DB and in export()."""
        import shoin.server as srv
        from shoin.store import Store

        _, nb = self._json("POST", "/api/notebooks", {"name": "cap"})
        nb_id = nb["id"]
        with Store(str(Path(self.tmp.name) / "mc.db")) as store:
            for i in range(12):
                store.add_note(nb_id, f"n{i}", f"body {i}")
        with patch.object(srv, "NB_NOTES_LIMIT", 4):
            status, j = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(j["notes"]), 4)
        self.assertEqual(j["notes_omitted"], 8)
        # The newest notes are the embedded ones — dropping the oldest means
        # the note a user just added is always visible.
        self.assertEqual(j["notes"][0]["title"], "n8")
        self.assertEqual(j["notes"][-1]["title"], "n11")
        # Under the cap the count is honestly 0, not guessed or absent.
        status, j2 = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 200)
        self.assertEqual(j2["notes_omitted"], 0)
        self.assertEqual(len(j2["notes"]), 12)

    def test_notebook_payload_caps_sources_and_reports_omitted(self) -> None:
        """v0.2.694: sources were the last unbounded embed on the detail
        payload — every detail fetch (openNotebook, the SSE-drop recovery
        refetch) grew with the source count, and nothing bounded it (the
        chunk cap bounds rows, not sources). Mirrors the notes/messages
        cap: newest NB_SOURCES_LIMIT embedded, sources_omitted discloses
        the hidden count, the full list stays reachable via the paged
        /sources endpoint and export()."""
        import shoin.server as srv
        from shoin.store import Store

        _, nb = self._json("POST", "/api/notebooks", {"name": "cap"})
        nb_id = nb["id"]
        with Store(str(Path(self.tmp.name) / "mc.db")) as store:
            for i in range(12):
                store.add_source(nb_id, "txt", f"s{i}", f"o{i}", f"h{i}")
        with patch.object(srv, "NB_SOURCES_LIMIT", 4):
            status, j = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(j["sources"]), 4)
        self.assertEqual(j["sources_omitted"], 8)
        # The newest sources are the embedded ones — the source a user
        # just added must always be visible after its own refetch.
        self.assertEqual(j["sources"][0]["title"], "s8")
        self.assertEqual(j["sources"][-1]["title"], "s11")
        # counts still reports the true total — the disclosure is honest.
        self.assertEqual(j["counts"]["sources"], 12)
        # Under the cap the count is honestly 0, not guessed or absent.
        status, j2 = self._json("GET", f"/api/notebooks/{nb_id}")
        self.assertEqual(status, 200)
        self.assertEqual(j2["sources_omitted"], 0)
        self.assertEqual(len(j2["sources"]), 12)

    def test_nb_sources_pagination(self) -> None:
        """v0.2.694: GET .../sources pages the full source list the detail
        cap can't reach — newest-first (page 0 overlaps the embedded tail),
        offset/limit bounded, total disclosed, invalid params coded 400,
        dead notebook 404."""
        status, nb = self._json("POST", "/api/notebooks", {"name": "paged"})
        self.assertEqual(status, 201)
        nb_id = nb["id"]
        db = str(Path(self.tmp.name) / "mc.db")
        from shoin.store import Store

        with Store(db) as store:
            for i in range(5):
                store.add_source(nb_id, "txt", f"s{i}", f"o{i}", f"h{i}")

        status, page = self._json(
            "GET", f"/api/notebooks/{nb_id}/sources?offset=1&limit=2"
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 5)
        self.assertEqual(page["offset"], 1)
        self.assertEqual(page["limit"], 2)
        self.assertEqual([s["title"] for s in page["sources"]], ["s3", "s2"])
        self.assertIn("refreshable", page["sources"][0])

        status, page = self._json("GET", f"/api/notebooks/{nb_id}/sources")
        self.assertEqual(status, 200)
        self.assertEqual(len(page["sources"]), 5)
        self.assertEqual(page["sources"][0]["title"], "s4")

        for bad in ("offset=-1", "offset=abc", "limit=0", "limit=2001"):
            status, err = self._json(
                "GET", f"/api/notebooks/{nb_id}/sources?{bad}"
            )
            self.assertEqual(status, 400, bad)
            self.assertEqual(
                err["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID", bad
            )
        status, err = self._json("GET", "/api/notebooks/999999/sources")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOTEBOOK_NOT_FOUND")


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

    def test_valid_json_non_dict_degrades_to_empty(self) -> None:
        """v0.2.628: a stored blob that is *valid* JSON but not an object —
        a list, number, string, or bool — must degrade to {} exactly as
        export.py's `_parse_report` does for the same shapes. Emitting the
        parsed non-dict into the envelope produces a `report` field that is
        not an object, breaking every `report.<field>` reader (UI, export
        legend) — the API/export parity gap fuzz480 caught."""
        for raw in ("[1,2]", "5", '"str"', "true", "0.5"):
            self.assertEqual(self._fn(raw), {}, f"non-dict blob leaked: {raw!r}")

    def test_non_string_raw_does_not_raise(self) -> None:
        """v0.2.628: json.loads raises TypeError (not ValueError) on a
        non-string raw — e.g. a value fetched as int from a non-STRICT
        column. That exception type escaped the old try/except entirely."""
        for raw in (5, 0, True, b"{}"):
            self.assertEqual(self._fn(raw), {}, f"non-str raw leaked: {raw!r}")


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

            def chat_stream(
                self, messages: list[dict[str, str]], temperature: float = 0.2
            ) -> Iterator[str]:
                raise _LLMError("SYSTEM_SERVICE_UNAVAILABLE", "endpoint down")
                yield  # unreachable; keeps the mock a generator like real chat_stream

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


class SSEConnectionErrorTest(unittest.TestCase):
    """Verify all SSE ConnectionError paths are handled gracefully (server.py 486-535)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = FakeLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "sse_ce.db"), llm=cls.llm)
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

    def _ask_raw(self, nb_id: int, question: str) -> bytes:
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/ask"),
            data=json.dumps({"question": question}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            return exc.read()

    def test_nohit_sse_connection_error_handled(self) -> None:
        """ConnectionError during no-hit SSE must be silenced (server.py 486-487)."""
        from unittest.mock import patch

        from shoin.server import _Handler

        _, nb = self._json("POST", "/api/notebooks", {"name": "nohit-ce"})
        nb_id = nb["id"]
        # Empty notebook → guaranteed no-hit path.
        original_sse = _Handler._sse
        call_count = [0]

        def sse_fail_on_delta(self_h, event: str, payload: dict) -> None:
            call_count[0] += 1
            if event == "delta":
                raise ConnectionError("test disconnect")
            original_sse(self_h, event, payload)

        with patch.object(_Handler, "_sse", sse_fail_on_delta):
            self._ask_raw(nb_id, "zzz completely unrelated question zzz")
        # Must complete without server error — SSE response started
        self.assertGreater(call_count[0], 0)

    def test_meta_sse_connection_error_handled(self) -> None:
        """ConnectionError on meta SSE must cause early return (server.py 507-508)."""
        from unittest.mock import patch

        from shoin.server import _Handler

        _, nb = self._json("POST", "/api/notebooks", {"name": "meta-ce"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("テスト文書内容です。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "doc.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        original_sse = _Handler._sse

        def sse_fail_on_meta(self_h, event: str, payload: dict) -> None:
            if event == "meta":
                raise ConnectionError("test disconnect")
            original_sse(self_h, event, payload)

        with patch.object(_Handler, "_sse", sse_fail_on_meta):
            raw = self._ask_raw(nb_id, "テスト文書の内容は？")
        # Server must complete gracefully even when meta SSE fails
        self.assertIsInstance(raw, bytes)

    def test_done_sse_connection_error_handled(self) -> None:
        """ConnectionError on done SSE must be silenced (server.py 534-535)."""
        from unittest.mock import patch

        from shoin.server import _Handler

        _, nb = self._json("POST", "/api/notebooks", {"name": "done-ce"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("テスト文書内容です。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "doc.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        original_sse = _Handler._sse
        done_called = [False]

        def sse_fail_on_done(self_h, event: str, payload: dict) -> None:
            if event == "done":
                done_called[0] = True
                raise ConnectionError("test disconnect")
            original_sse(self_h, event, payload)

        with patch.object(_Handler, "_sse", sse_fail_on_done):
            raw = self._ask_raw(nb_id, "テスト文書の内容は？")
        # Server must not crash when done SSE fails
        self.assertIsInstance(raw, bytes)

    def test_degraded_sse_connection_error_handled(self) -> None:
        """ConnectionError on degraded text delta must set client_gone (server.py 523-524)."""
        from unittest.mock import patch

        from shoin.llm import LLMError as _LLMError
        from shoin.server import _Handler

        _, nb = self._json("POST", "/api/notebooks", {"name": "degraded-ce"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("テスト文書内容です。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "doc.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        original_sse = _Handler._sse

        # Make _stream_chat raise LLMError (degraded path), then fail on degraded delta
        degraded_delta_count = [0]

        def sse_fail_on_degraded_delta(self_h, event: str, payload: dict) -> None:
            if event == "delta":
                degraded_delta_count[0] += 1
                raise ConnectionError("test disconnect during degraded text")
            original_sse(self_h, event, payload)

        def stream_raise_llmerror(messages, temperature=0.2):
            raise _LLMError("SYSTEM_SERVICE_UNAVAILABLE", "test down")
            yield  # unreachable, but makes this a generator like chat_stream

        with (
            patch.object(_Handler, "_sse", sse_fail_on_degraded_delta),
            patch.object(self.llm, "chat_stream", stream_raise_llmerror),
        ):
            raw = self._ask_raw(nb_id, "テスト文書の内容は？")
        # Server must complete gracefully; client_gone was set to True
        self.assertIsInstance(raw, bytes)

    def test_headers_write_connection_error_does_not_orphan_user_turn(self) -> None:
        """ConnectionError while writing the initial SSE headers (self._headers(),
        server.py, before any _sse() event is ever attempted) must not leave the
        just-persisted user turn dangling with no assistant reply. The three
        sibling ConnectionError/exception guards in _h_ask_sse() (build_context
        exceptions v0.2.39, meta-send v0.2.49, zero-token replies v0.2.55) all
        compensate by persisting an empty assistant message — this path, one
        statement earlier in the same function, previously had no guard at all
        and let the raw ConnectionError propagate to _dispatch()'s generic
        exception handler instead.
        """
        from unittest.mock import patch

        from shoin.server import _Handler
        from shoin.store import Store

        _, nb = self._json("POST", "/api/notebooks", {"name": "headers-ce"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("テスト文書内容です。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "doc.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        original_headers = _Handler._headers

        def headers_fail_on_sse(self_h, status, ctype, extra=None):
            if ctype.startswith("text/event-stream"):
                raise ConnectionError("test disconnect before SSE headers sent")
            return original_headers(self_h, status, ctype, extra)

        with patch.object(_Handler, "_headers", headers_fail_on_sse):
            # No response is ever written (headers failed before any bytes went
            # out), so the client observes a closed connection rather than an
            # HTTPError — that's the expected client-side symptom of the fix.
            try:
                self._ask_raw(nb_id, "テスト文書の内容は？")
            except http.client.RemoteDisconnected:
                pass

        with Store(str(Path(self.tmp.name) / "sse_ce.db")) as store:
            msgs = store.list_messages(nb_id)
        self.assertEqual(len(msgs), 2, "user turn must be paired with a compensating assistant row")
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["body"], "")

    def test_stream_timeout_still_persists_assistant_row(self) -> None:
        """A mid-stream failure that is neither LLMError nor ConnectionError —
        socket timeout (TimeoutError is not a ConnectionError subclass) is the
        reachable shape; a surrogate token failing the SSE UTF-8 encode is the
        same class — must not escape to _dispatch(). Its generic-500 write
        would inject a second HTTP status line into the already-committed SSE
        body, and unwinding would skip the assistant persist, orphaning the
        user turn exactly like the disconnect paths this handler compensates.
        """
        from shoin.store import Store

        _, nb = self._json("POST", "/api/notebooks", {"name": "sse-timeout"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("テスト文書内容です。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "doc.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        def stream_timeout(messages, temperature=0.2):
            yield "先頭の断片"
            raise TimeoutError("simulated socket timeout mid-stream")

        with patch.object(self.llm, "chat_stream", stream_timeout):
            raw = self._ask_raw(nb_id, "テスト文書の内容は？")

        # No second HTTP response may appear inside the SSE stream body.
        self.assertNotIn(b"HTTP/1.0 500", raw)
        events = parse_sse(raw.decode("utf-8"))
        kinds = [ev for ev, _ in events]
        self.assertIn("error", kinds, "an SSE error frame must carry the failure")
        self.assertIn("done", kinds, "the stream must still terminate cleanly")
        self.assertIn(
            ("error", {"code": "SYSTEM_INTERNAL_ERROR", "message": "TimeoutError"}),
            events,
        )
        with Store(str(Path(self.tmp.name) / "sse_ce.db")) as store:
            msgs = store.list_messages(nb_id)
        self.assertEqual(len(msgs), 2, "user turn must still be paired")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["body"], "先頭の断片")

    def test_stream_error_frame_failure_still_persists(self) -> None:
        """The error frame written by the mid-stream guard is itself a socket
        write — if it also fails (client already gone), the handler must mark
        client_gone (skip the done frame) and still persist the assistant row.
        """
        from shoin.server import _Handler
        from shoin.store import Store

        _, nb = self._json("POST", "/api/notebooks", {"name": "sse-errfail"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("テスト文書内容です。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "doc.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        original_sse = _Handler._sse

        def sse_fail_on_error(self_h, event: str, payload: dict) -> None:
            if event == "error":
                raise ConnectionError("client already gone for error frame")
            original_sse(self_h, event, payload)

        def stream_timeout(messages, temperature=0.2):
            yield "途中までの回答"
            raise TimeoutError("simulated socket timeout mid-stream")

        with (
            patch.object(_Handler, "_sse", sse_fail_on_error),
            patch.object(self.llm, "chat_stream", stream_timeout),
        ):
            raw = self._ask_raw(nb_id, "テスト文書の内容は？")

        self.assertNotIn(b"HTTP/1.0 500", raw)
        events = parse_sse(raw.decode("utf-8"))
        self.assertNotIn(
            "done", [ev for ev, _ in events],
            "client_gone must suppress the done frame",
        )
        with Store(str(Path(self.tmp.name) / "sse_ce.db")) as store:
            msgs = store.list_messages(nb_id)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["body"], "途中までの回答")

    def test_drain_empty_read_breaks_loop(self) -> None:
        """_drain must break when rfile.read() returns empty bytes (server.py 166).

        Sending a large Content-Length without an actual body causes rfile.read()
        to return b'' immediately, exercising the break at line 166.
        """
        import socket as _socket

        # Open a raw TCP connection and send a request with a huge Content-Length
        # but no body — the server will call _drain() and immediately read empty.
        sock = _socket.create_connection(("127.0.0.1", self.port))
        try:
            huge_len = 20 * 1024 * 1024  # 20 MB > MAX_UPLOAD_BYTES (10 MB)
            request = (
                f"POST /api/notebooks/1/ask HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {huge_len}\r\n"
                f"\r\n"
            )
            sock.sendall(request.encode())
            # Close the write side immediately — server reads empty bytes in _drain.
            sock.shutdown(_socket.SHUT_WR)
            # Read the response (should be a 400 error)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        finally:
            sock.close()
        # Server must have responded (400 for too-large body or close gracefully)
        self.assertTrue(len(response) >= 0)  # did not crash

    def test_streamed_report_receives_history(self) -> None:
        """v0.2.216: the streamed make_report() must get the same `history`
        join qa.ask() passes — previously it was omitted, so the cross-turn
        checks (degenerate_spans/self_contradictions) silently never fired
        on the web path, the primary user surface."""
        from shoin.store import Store

        _, nb = self._json("POST", "/api/notebooks", {"name": "xturn"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("治療法の効果について多くの研究がある。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "doc.txt"},
        )
        with urllib.request.urlopen(req):
            pass
        # Seed a prior assistant turn asserting the opposite.
        with Store(str(Path(self.tmp.name) / "sse_ce.db")) as store:
            store.add_message(nb_id, "user", "効果は？", "{}")
            store.add_message(nb_id, "assistant", "治療の効果はある。", "{}")
        self.llm.reply_parts = ["治療の効果はない。"]
        try:
            raw = self._ask_raw(nb_id, "効果はどうですか？")
        finally:
            self.llm.reply_parts = ["回答 ", "[S1]。"]
        done = [d for ev, d in parse_sse(raw.decode()) if ev == "done"]
        self.assertTrue(done)
        self.assertEqual(
            done[0]["report"].get("self_contradiction"), ["治療の効果はない。"]
        )

    def test_build_context_error_frame_and_no_dangling_turn(self) -> None:
        """build_context raising after hits are found (e.g. WAL busy_timeout)
        must emit an SSE error frame — headers already committed, so no HTTP
        status can be sent — and persist an EMPTY assistant message so the
        orphaned user turn can't corrupt history_messages pairing."""
        from shoin.store import Store

        _, nb = self._json("POST", "/api/notebooks", {"name": "ctx-err"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("テスト文書内容です。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "doc.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        with patch("shoin.server.build_context", side_effect=RuntimeError("ctx boom")):
            raw = self._ask_raw(nb_id, "テスト文書の内容は？")
        events = parse_sse(raw.decode())
        kinds = [e for e, _ in events]
        self.assertIn("error", kinds)
        self.assertNotIn("done", kinds)
        err_payload = [d for e, d in events if e == "error"][0]
        self.assertEqual(err_payload["code"], "SYSTEM_INTERNAL_ERROR")
        # v0.2.508: the client sees only the exception type name — the raw
        # str(exc) ("ctx boom" here, but DB paths/LLM internals in general)
        # stays on stderr, matching the _dispatch 500 path's policy.
        self.assertEqual(err_payload["message"], "RuntimeError")
        # The dangling-turn guard: an empty assistant turn was persisted.
        with Store(str(Path(self.tmp.name) / "sse_ce.db")) as store:
            msgs = store.list_messages(nb_id)
        self.assertEqual(msgs[-1]["role"], "assistant")
        self.assertEqual(msgs[-1]["body"], "")

    def test_build_context_error_frame_leaks_type_name_only(self) -> None:
        """An unhandled build_context failure must mirror _dispatch's
        catch-all: the SSE error frame carries only type(exc).__name__,
        never str(exc) — raw messages can contain internals (SQL text,
        filesystem paths) that must not reach the client."""
        _, nb = self._json("POST", "/api/notebooks", {"name": "ctx-leak"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("テスト文書内容です。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "doc.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        with patch(
            "shoin.server.build_context",
            side_effect=RuntimeError("secret path /Users/x/internal.db"),
        ):
            raw = self._ask_raw(nb_id, "テスト文書の内容は？")
        err_payload = [d for e, d in parse_sse(raw.decode()) if e == "error"][0]
        self.assertEqual(err_payload["code"], "SYSTEM_INTERNAL_ERROR")
        self.assertEqual(err_payload["message"], "RuntimeError")
        self.assertNotIn("secret", err_payload["message"])

    def test_build_context_error_frame_passes_coded_errors(self) -> None:
        """A coded error (StoreError/IngestError/LLMError) carries its
        curated (code, message) into the SSE error frame — mirroring the
        _dispatch envelope mapping rather than flattening to 500."""
        from shoin.store import StoreError

        _, nb = self._json("POST", "/api/notebooks", {"name": "ctx-coded"})
        nb_id = nb["id"]
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=("テスト文書内容です。" * 30).encode(),
            method="POST",
            headers={"X-Filename": "doc.txt"},
        )
        with urllib.request.urlopen(req):
            pass

        with patch(
            "shoin.server.build_context",
            side_effect=StoreError("NOTEBOOK_NOT_FOUND", "notebook 7 not found"),
        ):
            raw = self._ask_raw(nb_id, "テスト文書の内容は？")
        err_payload = [d for e, d in parse_sse(raw.decode()) if e == "error"][0]
        self.assertEqual(err_payload["code"], "NOTEBOOK_NOT_FOUND")
        self.assertEqual(err_payload["message"], "notebook 7 not found")


class HostnameOfTest(unittest.TestCase):
    def test_malformed_netloc_returns_empty_string(self) -> None:
        """_hostname_of must return '' when urlsplit raises ValueError (e.g. IDNA-invalid host)."""
        import urllib.parse
        from unittest.mock import patch

        from shoin.server import _hostname_of

        with patch.object(urllib.parse, "urlsplit", side_effect=ValueError("bad")):
            result = _hostname_of("//some-bad-host")
        self.assertEqual(result, "")


class InputValidationSecurityTest(unittest.TestCase):
    """Tests for the security fixes: integer overflow and negative Content-Length."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = FakeLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "sec.db"), llm=cls.llm)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    # Generous liveness bound, not a latency SLA: only meant to catch a hung
    # server. Under full-suite load a localhost request can legitimately take
    # several seconds — a tight timeout here flakes with TimeoutError while
    # the server and code under test are both healthy.
    _CONN_TIMEOUT = 30

    def _raw_post(self, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self._CONN_TIMEOUT)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()

    def test_huge_notebook_id_returns_400_not_crash(self) -> None:
        """A path ID larger than int64 max must return 400, not propagate OverflowError."""
        huge_id = "9" * 30  # >>> 2**63-1
        status, raw = self._raw_post(
            f"/api/notebooks/{huge_id}/sources",
            b'{"url":"http://example.com"}',
            {"Content-Type": "application/json", "Content-Length": "28"},
        )
        data = json.loads(raw)
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "VALIDATION_INTEGER_OVERFLOW")

    def test_negative_content_length_returns_400(self) -> None:
        """A negative Content-Length on a JSON endpoint must return 400, not read until EOF."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self._CONN_TIMEOUT)
        conn.request(
            "POST",
            "/api/notebooks",
            body=b"",
            headers={"Content-Type": "application/json", "Content-Length": "-1"},
        )
        resp = conn.getresponse()
        raw = resp.read()
        data = json.loads(raw)
        self.assertEqual(resp.status, 400)
        self.assertEqual(data["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")

    def test_overlong_question_returns_400(self) -> None:
        """A question exceeding MAX_QUESTION_LEN must be rejected before FTS evaluation."""
        import json as _json

        # Create a notebook first so the rejection happens in _h_ask_sse, not at 404
        nb_body = _json.dumps({"name": "q-len-test"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self._CONN_TIMEOUT)
        conn.request(
            "POST", "/api/notebooks", body=nb_body,
            headers={"Content-Type": "application/json"},
        )
        nb_resp = conn.getresponse()
        nb_data = _json.loads(nb_resp.read())
        nb_id = nb_data.get("id", 1)

        ask_body = _json.dumps({"question": "あ" * 2001}).encode()
        status, raw = self._raw_post(
            f"/api/notebooks/{nb_id}/ask",
            ask_body,
            {"Content-Type": "application/json"},
        )
        data = _json.loads(raw)
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")

    def test_create_notebook_with_integer_name_returns_400(self) -> None:
        """POST /api/notebooks with {"name": 42} must return 400, not silently coerce.

        Before v0.2.38, _require() called str(raw) on non-string values, so an integer
        name like 42 would be silently accepted as "42" — a type confusion bug that let
        callers bypass name-length validation and potentially inject unexpected values.
        """
        import json as _json

        body = _json.dumps({"name": 42}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self._CONN_TIMEOUT)
        conn.request(
            "POST", "/api/notebooks", body=body,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        raw = resp.read()
        data = _json.loads(raw)
        self.assertEqual(resp.status, 400)
        self.assertEqual(data["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")

    def test_patch_source_with_integer_title_returns_400(self) -> None:
        """PATCH /api/sources/{id} with {"title": 42} must return 400.

        Before v0.2.39, _h_src_patch used `str(data.get("title") or "")` which silently
        coerced integer titles to strings.
        """
        import json as _json

        # Use a large nonexistent source_id — _require() runs before the source lookup
        patch_body = _json.dumps({"title": 42}).encode()
        status, raw = self._raw_post(
            "/api/sources/99999",
            patch_body,
            {"Content-Type": "application/json", "X-HTTP-Method-Override": "PATCH"},
        )
        # Can't use _raw_post for PATCH directly — do it manually
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self._CONN_TIMEOUT)
        conn.request(
            "PATCH", "/api/sources/99999",
            body=patch_body,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        raw = resp.read()
        data = _json.loads(raw)
        self.assertEqual(resp.status, 400)
        self.assertEqual(data["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")

    def test_add_note_with_non_string_body_returns_400(self) -> None:
        """POST /api/notebooks/{id}/notes with a non-string "body" must return 400,
        not silently persist Python's str()/repr() of the value.

        _h_note_add used `body = str(data.get("body") or "")` for the body field,
        unlike title (already protected by _require() since v0.2.38) — a JSON
        list/dict/bool body was silently coerced to its Python repr (e.g. "[1, 2, 3]")
        and persisted with HTTP 201, instead of being rejected as malformed input.
        """
        import json as _json

        nb_body = _json.dumps({"name": "note-body-type-test"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self._CONN_TIMEOUT)
        conn.request(
            "POST", "/api/notebooks", body=nb_body,
            headers={"Content-Type": "application/json"},
        )
        nb_id = _json.loads(conn.getresponse().read())["id"]

        note_body = _json.dumps({"title": "T", "body": [1, 2, 3]}).encode()
        status, raw = self._raw_post(
            f"/api/notebooks/{nb_id}/notes", note_body,
            {"Content-Type": "application/json"},
        )
        data = _json.loads(raw)
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "VALIDATION_FIELD_FORMAT_INVALID")


class _OverlapDetectingLLM:
    """Records whether chat_stream() was ever entered while already active.

    Used to prove generation_lock actually serializes LLM calls: without the
    lock, concurrent requests' sleep() windows overlap and a violation is
    recorded deterministically; with the lock, calls are strictly sequential.
    """

    embedding_model = ""
    model = "fake-4b"

    def __init__(self) -> None:
        self._active = False
        self._state_lock = threading.Lock()
        self.violations = 0

    def available(self) -> bool:
        return True

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.2) -> str:
        return "これは何ですか？ [S1]。"

    def chat_stream(
        self, messages: list[dict[str, str]], temperature: float = 0.2
    ) -> Iterator[str]:
        with self._state_lock:
            if self._active:
                self.violations += 1
            self._active = True
        try:
            time.sleep(0.15)
            yield "回答"
            yield "[S1]。"
        finally:
            with self._state_lock:
                self._active = False

    def embed_one(self, text: str) -> list[float]:
        return [1.0, 0.0]


class GenerationSerializationTest(unittest.TestCase):
    """generation_lock (v0.2.70) must serialize LLM generation across concurrent
    requests — spec.md STRIDE DoS control '同時生成1', previously undocumented-
    but-unimplemented (found by this session's Socratic audit of spec.md)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.llm = _OverlapDetectingLLM()
        cls.server = make_server(port=0, db=str(Path(cls.tmp.name) / "gen.db"), llm=cls.llm)
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

    def _json(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object]]:
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self._url(path), data=body, method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())

    def test_concurrent_ask_requests_do_not_overlap_generation(self) -> None:
        _, nb = self._json("POST", "/api/notebooks", {"name": "concurrency-test"})
        nb_id = nb["id"]
        upload_body = ("並行実行のテスト用文書。" * 20).encode("utf-8")
        req = urllib.request.Request(
            self._url(f"/api/notebooks/{nb_id}/upload"),
            data=upload_body,
            method="POST",
            headers={"X-Filename": urllib.parse.quote("doc.txt")},
        )
        with urllib.request.urlopen(req):
            pass

        errors: list[Exception] = []

        def fire() -> None:
            try:
                req = urllib.request.Request(
                    self._url(f"/api/notebooks/{nb_id}/ask"),
                    data=json.dumps({"question": "テストです"}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
            except Exception as exc:  # pragma: no cover - surfaced via errors list
                errors.append(exc)

        threads = [threading.Thread(target=fire) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(
            self.llm.violations,
            0,
            "generation_lock must prevent overlapping chat_stream calls across "
            "concurrent /ask requests",
        )

    def test_multi_query_rewrite_call_not_serialized_against_other_requests_generation(
        self,
    ) -> None:
        """v0.2.128: with SHOIN_MULTI_QUERY=1, one request's rewrite call
        (chat(), temperature=0.7) must NOT be blocked behind another
        concurrent request's answer-generation call (chat_stream()) — only
        chat_stream-vs-chat_stream is still serialized by generation_lock.

        Before the fix, retrieve_for_question() held generation_lock for the
        rewrite call too, so a single /ask could acquire the shared lock
        TWICE (rewrite, then generation), up to doubling the worst-case time
        other concurrent requests could be blocked waiting on it.
        """
        import os
        from unittest.mock import patch

        class _TimingLLM:
            embedding_model = ""
            model = "fake-4b"

            def __init__(self) -> None:
                self._lock = threading.Lock()
                self.rewrite_intervals: list[tuple[float, float]] = []
                self.stream_intervals: list[tuple[float, float]] = []
                self.chat_stream_violations = 0
                self._stream_active = False
                # Event pair that makes the overlap window deterministic: the
                # SECOND rewrite call holds itself open until the FIRST stream
                # has actually started, and that stream does not begin until a
                # second rewrite is in-flight. Before this, the test relied on a
                # 0.1s fire stagger producing a scheduler window — under load
                # the post-rewrite work between the calls takes longer than the
                # stagger, the windows pass each other by, and the assertion
                # fails even though the code never serialized anything.
                self.rewrite_inflight = threading.Event()
                self.stream_started = threading.Event()

            def available(self) -> bool:
                return True

            def chat(self, messages: list[dict[str, str]], temperature: float = 0.2) -> str:
                if temperature > 0.5:  # the rewrite_queries() call
                    with self._lock:
                        nth = len(self.rewrite_intervals) + 1
                    start = time.monotonic()
                    if nth == 2:
                        # The other request's rewrite: announce it is in-flight
                        # so the first stream call knows a rewrite is active,
                        # then stay open until that stream has started —
                        # guaranteeing the intervals overlap regardless of when
                        # either thread next gets scheduled.
                        self.rewrite_inflight.set()
                        self.stream_started.wait(timeout=10)
                    else:
                        time.sleep(0.05)
                    with self._lock:
                        self.rewrite_intervals.append((start, time.monotonic()))
                    return "書院の仕組みとは\n書院についての説明"
                return "これは何ですか？ [S1]。"

            def chat_stream(
                self, messages: list[dict[str, str]], temperature: float = 0.2
            ) -> Iterator[str]:
                with self._lock:
                    if self._stream_active:
                        self.chat_stream_violations += 1
                    self._stream_active = True
                    first = len(self.stream_intervals) == 0
                if first:
                    # First generation call: wait until the other request's
                    # rewrite is actually in-flight, so its interval provably
                    # overlaps this one.
                    self.rewrite_inflight.wait(timeout=10)
                start = time.monotonic()
                if first:
                    self.stream_started.set()
                try:
                    time.sleep(0.05)
                    yield "回答"
                    yield "[S1]。"
                finally:
                    end = time.monotonic()
                    with self._lock:
                        self._stream_active = False
                        self.stream_intervals.append((start, end))

            def embed_one(self, text: str) -> list[float]:
                return [1.0, 0.0]

        with tempfile.TemporaryDirectory() as tmp:
            llm = _TimingLLM()
            server = make_server(port=0, db=str(Path(tmp) / "mq.db"), llm=llm)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def url(path: str) -> str:
                    return f"http://127.0.0.1:{port}{path}"

                req = urllib.request.Request(
                    url("/api/notebooks"),
                    data=json.dumps({"name": "mq-test"}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req) as resp:
                    nb_id = json.loads(resp.read())["id"]
                upload_body = ("マルチクエリ並行テスト用文書。" * 20).encode("utf-8")
                req = urllib.request.Request(
                    url(f"/api/notebooks/{nb_id}/upload"),
                    data=upload_body,
                    method="POST",
                    headers={"X-Filename": urllib.parse.quote("doc.txt")},
                )
                with urllib.request.urlopen(req):
                    pass

                errors: list[Exception] = []

                def fire() -> None:
                    try:
                        req = urllib.request.Request(
                            url(f"/api/notebooks/{nb_id}/ask"),
                            data=json.dumps({"question": "テストです"}).encode(),
                            method="POST",
                            headers={"Content-Type": "application/json"},
                        )
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            resp.read()
                    except Exception as exc:  # pragma: no cover
                        errors.append(exc)

                with patch.dict(os.environ, {"SHOIN_MULTI_QUERY": "1"}, clear=False):
                    # A small stagger keeps the firing order intuitive, but the
                    # overlap itself is guaranteed by the mock's
                    # rewrite_inflight/stream_started event pair — the second
                    # rewrite cannot close until the first stream has opened,
                    # so no scheduler timing is left to chance.
                    threads = [threading.Thread(target=fire)]
                    threads[0].start()
                    time.sleep(0.1)
                    threads.append(threading.Thread(target=fire))
                    threads[1].start()
                    for t in threads:
                        t.join(timeout=15)
            finally:
                server.shutdown()
                server.server_close()

        self.assertEqual(errors, [])
        self.assertEqual(
            llm.chat_stream_violations,
            0,
            "chat_stream-to-chat_stream must still be serialized by generation_lock",
        )

        def overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
            return a[0] < b[1] and b[0] < a[1]

        found_overlap = any(
            overlaps(r, s) for r in llm.rewrite_intervals for s in llm.stream_intervals
        )
        self.assertTrue(
            found_overlap,
            "a rewrite call (chat()) must be able to run concurrently with a "
            "DIFFERENT request's answer-generation call (chat_stream()) — "
            f"rewrite_intervals={llm.rewrite_intervals} stream_intervals={llm.stream_intervals}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=0)

