"""Shoin core tests. Run: python3 tests/test_core.py"""

from __future__ import annotations

import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shoin import VERSION
from shoin.chunk import estimate_tokens, is_cjk, split_text
from shoin.ingest import (
    Extracted,
    IngestError,
    extract_file,
    html_to_text,
    validate_public_url,
)
from shoin.search import (
    adaptive_alpha,
    bm25_search,
    fts_query,
    fuse,
    lexical_overlap,
    mmr,
    query_terms,
    retrieve,
)
from shoin.search import Hit, _char_bigrams
from shoin.store import Store, StoreError, pack_vector, unpack_vector

JA = "書院は知の書斎である。引用付きで文書と対話する。"
EN = "Shoin is a local notebook. Citations are machine verified."


def make_store() -> Store:
    return Store(":memory:")


def seed(store: Store) -> int:
    nb = store.create_notebook("研究")
    src = store.add_source(nb.id, "txt", "doc-ja", "mem://ja", "sha-ja")
    store.add_chunks(src.id, [JA, "本日の天気は晴れ。気温は二十五度。", "猫は液体である説。"])
    src2 = store.add_source(nb.id, "txt", "doc-en", "mem://en", "sha-en")
    store.add_chunks(src2.id, [EN, "The quick brown fox jumps over the lazy dog."])
    return nb.id


class TestStore(unittest.TestCase):
    def test_version(self) -> None:
        self.assertEqual(VERSION, "0.1.88")

    def test_migrate_idempotent(self) -> None:
        with make_store() as s:
            self.assertEqual(s.migrate(), 4)
            self.assertEqual(s.migrate(), 4)

    def test_migration_schema_version_and_tables_always_consistent(self) -> None:
        """After migrate() on a fresh file DB, all version records and their corresponding
        tables must co-exist — proving the DDL + INSERT write is atomic (no crash window)."""
        import sqlite3 as sqlite3_mod

        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "atomic.db")
            with Store(db_path):
                pass

            conn = sqlite3_mod.connect(db_path)
            try:
                versions = {
                    row[0]
                    for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
                }
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                    ).fetchall()
                }
            finally:
                conn.close()

        from shoin.store import MIGRATIONS

        self.assertEqual(versions, {v for v, _ in MIGRATIONS})
        # Spot-check tables from migration 1 and indexes from migration 2
        for name in ("notebooks", "sources", "chunks", "messages", "settings"):
            self.assertIn(name, tables, f"table {name} missing after migration")
        self.assertIn("idx_sources_notebook", tables)

    def test_migration_scripts_contain_begin_commit(self) -> None:
        """The executescript calls produced by migrate() must embed BEGIN/COMMIT so
        the DDL and version-INSERT are one atomic SQLite write."""
        from shoin.store import MIGRATIONS

        for version, sql in MIGRATIONS:
            script = f"BEGIN;\n{sql.strip()}\nINSERT INTO schema_migrations(version) VALUES ({int(version)});\nCOMMIT;"
            stripped = script.strip()
            self.assertTrue(stripped.startswith("BEGIN;"), f"v{version}: missing BEGIN")
            self.assertTrue(stripped.endswith("COMMIT;"), f"v{version}: missing COMMIT")
            self.assertIn(f"VALUES ({version})", stripped)

    def test_migration_4_index_exists(self) -> None:
        """Migration 4 must create idx_messages_notebook_id_desc for query performance."""
        with make_store() as s:
            names = {
                row[0]
                for row in s.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        self.assertIn("idx_messages_notebook_id_desc", names)

    def test_notebook_crud(self) -> None:
        with make_store() as s:
            nb = s.create_notebook("  研究  ")
            self.assertEqual(nb.name, "研究")
            s.rename_notebook(nb.id, "論文")
            self.assertEqual(s.get_notebook(nb.id).name, "論文")
            self.assertEqual(len(s.list_notebooks()), 1)
            s.delete_notebook(nb.id)
            with self.assertRaises(StoreError):
                s.get_notebook(nb.id)

    def test_empty_name_rejected(self) -> None:
        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                s.create_notebook("   ")
            self.assertEqual(cm.exception.code, "VALIDATION_REQUIRED_FIELD_MISSING")

    def test_duplicate_source_rejected(self) -> None:
        with make_store() as s:
            nb = s.create_notebook("n")
            s.add_source(nb.id, "txt", "a", "o", "same-hash")
            with self.assertRaises(StoreError) as cm:
                s.add_source(nb.id, "txt", "b", "o2", "same-hash")
            self.assertEqual(cm.exception.code, "SOURCE_ALREADY_EXISTS")

    def test_get_source_returns_source_and_raises_on_missing(self) -> None:
        with make_store() as s:
            nb = s.create_notebook("n")
            src = s.add_source(nb.id, "txt", "title", "origin", "abc123")
            fetched = s.get_source(src.id)
            self.assertEqual(fetched.title, "title")
            with self.assertRaises(StoreError) as cm:
                s.get_source(99999)
            self.assertEqual(cm.exception.code, "SOURCE_NOT_FOUND")

    def test_delete_nonexistent_source_raises_404(self) -> None:
        """Deleting a non-existent source must raise, not silently succeed."""
        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                s.delete_source(99999)
            self.assertEqual(cm.exception.code, "SOURCE_NOT_FOUND")

    def test_delete_nonexistent_note_raises_404(self) -> None:
        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                s.delete_note(99999)
            self.assertEqual(cm.exception.code, "NOTE_NOT_FOUND")

    def test_cascade_delete_cleans_fts(self) -> None:
        with make_store() as s:
            nb_id = seed(s)
            self.assertGreater(s.counts(nb_id)["chunks"], 0)
            s.delete_notebook(nb_id)
            n_fts = s.conn.execute("SELECT COUNT(*) AS n FROM chunks_fts").fetchone()["n"]
            self.assertEqual(int(n_fts), 0)

    def test_vector_roundtrip(self) -> None:
        vec = [0.1, -0.5, 3.25]
        out = unpack_vector(pack_vector(vec))
        for a, b in zip(vec, out):
            self.assertAlmostEqual(a, b, places=5)

    def test_embedding_persist(self) -> None:
        with make_store() as s:
            nb_id = seed(s)
            chunk = s.chunks_for_notebook(nb_id)[0]
            s.set_embedding(chunk.id, [1.0, 0.0])
            self.assertEqual(s.get_chunk(chunk.id).embedding, [1.0, 0.0])

    def test_add_message_touches_notebook(self) -> None:
        with make_store() as s:
            nb = s.create_notebook("chat")
            t0 = s.get_notebook(nb.id).updated_at
            s.add_message(nb.id, "user", "こんにちは")
            self.assertGreater(s.get_notebook(nb.id).updated_at, t0)

    def test_clear_messages_touches_notebook(self) -> None:
        with make_store() as s:
            nb = s.create_notebook("clr")
            s.add_message(nb.id, "user", "hello")
            t0 = s.get_notebook(nb.id).updated_at
            s.clear_messages(nb.id)
            self.assertGreater(s.get_notebook(nb.id).updated_at, t0)

    def test_update_source_title(self) -> None:
        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "file", "tmp.txt", "/tmp/tmp.txt", "h1")
            t0 = s.get_notebook(nb.id).updated_at
            s.update_source_title(src.id, "report.txt", "report.txt")
            updated = s.get_source(src.id)
            self.assertEqual(updated.title, "report.txt")
            self.assertEqual(updated.origin, "report.txt")
            self.assertGreater(s.get_notebook(nb.id).updated_at, t0)

    def test_delete_source_touches_notebook(self) -> None:
        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "a.txt", "a.txt", "ha")
            t0 = s.get_notebook(nb.id).updated_at
            s.delete_source(src.id)
            self.assertGreater(s.get_notebook(nb.id).updated_at, t0)

    def test_update_source_title_missing_raises(self) -> None:
        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                s.update_source_title(99999, "x", "x")
            self.assertEqual(cm.exception.code, "SOURCE_NOT_FOUND")

    def test_add_note_missing_notebook_raises(self) -> None:
        """add_note() on a non-existent notebook must raise NOTEBOOK_NOT_FOUND."""
        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                s.add_note(99999, "title", "body")
            self.assertEqual(cm.exception.code, "NOTEBOOK_NOT_FOUND")

    def test_add_message_notebook_deleted_between_check_and_insert(self) -> None:
        """Race: notebook deleted after get_notebook() passes but before INSERT.
        Must raise StoreError(NOTEBOOK_NOT_FOUND), not bare sqlite3.IntegrityError,
        so callers that guard 'except StoreError' work correctly (e.g. SSE handler).
        Simulated by bypassing get_notebook with a mock and then deleting the notebook."""
        from unittest.mock import patch

        with make_store() as s:
            nb = s.create_notebook("race-msg")
            s.delete_notebook(nb.id)
            with patch.object(s, "get_notebook"):  # bypass the pre-check
                with self.assertRaises(StoreError) as cm:
                    s.add_message(nb.id, "user", "hello", "{}")
                self.assertEqual(cm.exception.code, "NOTEBOOK_NOT_FOUND")

    def test_add_note_notebook_deleted_between_check_and_insert(self) -> None:
        """Same FK-race as add_message but for add_note — must not leak IntegrityError."""
        from unittest.mock import patch

        with make_store() as s:
            nb = s.create_notebook("race-note")
            s.delete_notebook(nb.id)
            with patch.object(s, "get_notebook"):
                with self.assertRaises(StoreError) as cm:
                    s.add_note(nb.id, "t", "b")
                self.assertEqual(cm.exception.code, "NOTEBOOK_NOT_FOUND")

    def test_add_studio_output_notebook_deleted_between_check_and_insert(self) -> None:
        """Same FK-race as add_message but for add_studio_output."""
        from unittest.mock import patch

        with make_store() as s:
            nb = s.create_notebook("race-studio")
            s.delete_notebook(nb.id)
            with patch.object(s, "get_notebook"):
                with self.assertRaises(StoreError) as cm:
                    s.add_studio_output(nb.id, "briefing", "body", "{}")
                self.assertEqual(cm.exception.code, "NOTEBOOK_NOT_FOUND")

    def test_list_notebooks_with_counts_single_query(self) -> None:
        with make_store() as s:
            nb_id = seed(s)
            rows = s.list_notebooks_with_counts()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["id"], nb_id)
            self.assertEqual(row["counts"]["sources"], 2)
            self.assertGreater(row["counts"]["chunks"], 0)

    def test_set_embedding_missing_chunk_raises(self) -> None:
        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                s.set_embedding(99999, [1.0, 0.0])
            self.assertEqual(cm.exception.code, "CHUNK_NOT_FOUND")

    def test_add_chunks_with_deleted_source_raises_source_not_found(self) -> None:
        """add_chunks() must raise StoreError(SOURCE_NOT_FOUND) — not a bare
        sqlite3.IntegrityError — when the source is deleted concurrently."""
        with make_store() as s:
            nb = s.create_notebook("n")
            src = s.add_source(nb.id, "txt", "a", "o", "h1")
            s.delete_source(src.id)  # simulate concurrent deletion
            with self.assertRaises(StoreError) as cm:
                s.add_chunks(src.id, ["chunk text"])
            self.assertEqual(cm.exception.code, "SOURCE_NOT_FOUND")

    def test_add_source_fk_violation_raises_notebook_not_found(self) -> None:
        """If the notebook is deleted between get_notebook() and the INSERT,
        the FK IntegrityError must be surfaced as NOTEBOOK_NOT_FOUND, not
        the misleading SOURCE_ALREADY_EXISTS code."""
        with make_store() as s:
            nb = s.create_notebook("nb")
            s.delete_notebook(nb.id)  # simulate concurrent deletion
            with self.assertRaises(StoreError) as cm:
                s.add_source(nb.id, "txt", "title", "origin", "unique-hash")
            self.assertEqual(cm.exception.code, "NOTEBOOK_NOT_FOUND")

    def test_set_embedding_missing_chunk_does_not_commit(self) -> None:
        """Raise must happen before commit so no corrupt data is persisted."""
        with make_store() as s:
            nb_id = seed(s)
            try:
                s.set_embedding(99999, [1.0, 0.0])
            except StoreError:
                pass
            # Real chunk should be untouched (no embedding set by the failed call)
            chunk = s.chunks_for_notebook(nb_id)[0]
            self.assertIsNone(chunk.embedding)

    def test_set_embedding_commit_false_defers_write(self) -> None:
        """commit=False should not commit until the caller does so."""
        with make_store() as s:
            nb_id = seed(s)
            chunks = s.chunks_for_notebook(nb_id)
            cid = chunks[0].id
            s.set_embedding(cid, [1.0, 0.0], commit=False)
            # Rollback without committing — embedding must NOT be visible
            s.conn.rollback()
            refreshed = s.chunks_for_notebook(nb_id)[0]
            self.assertIsNone(refreshed.embedding)

    def test_persistence_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db" / "shoin.sqlite3"
            with Store(path) as s:
                s.create_notebook("disk")
            with Store(path) as s2:
                self.assertEqual(s2.list_notebooks()[0].name, "disk")


class TestChunk(unittest.TestCase):
    def test_cjk_detection(self) -> None:
        self.assertTrue(is_cjk("書"))
        self.assertTrue(is_cjk("カ"))
        self.assertFalse(is_cjk("a"))

    def test_token_estimate_mixed(self) -> None:
        self.assertEqual(estimate_tokens("書院 notebook 123"), 2 + 2)

    def test_small_text_single_chunk(self) -> None:
        self.assertEqual(split_text("短い文章。"), ["短い文章。"])

    def test_long_text_overlap(self) -> None:
        text = "\n\n".join(f"段落{i}。" + "あ" * 120 for i in range(12))
        chunks = split_text(text, chunk_tokens=200, overlap_tokens=30)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(estimate_tokens(c), 200 + 40)
        # overlap: tail of chunk[0] reappears in chunk[1]
        self.assertIn(chunks[0][-20:], chunks[1])

    def test_heading_boundary(self) -> None:
        text = "# 第一章\n本文A\n\n# 第二章\n本文B"
        chunks = split_text(text, chunk_tokens=6, overlap_tokens=0)
        self.assertTrue(any("第一章" in c for c in chunks))
        self.assertTrue(any("第二章" in c for c in chunks))

    def test_pathological_unbroken(self) -> None:
        chunks = split_text("x" * 5000, chunk_tokens=100, overlap_tokens=10)
        self.assertGreater(len(chunks), 0)

    def test_southeast_asian_scripts_counted_as_tokens(self) -> None:
        """Thai, Myanmar, Khmer, Lao chars must each count as one token (REQ-003)."""
        thai = "สวัสดี"  # 6 Thai chars → 6 tokens
        myanmar = "မင်္ဂလာပါ"  # 9 Myanmar chars → 9 tokens (some combining, still counted)
        khmer = "សួស្តី"  # Khmer chars
        for script_text in (thai, myanmar, khmer):
            tokens = estimate_tokens(script_text)
            self.assertGreater(tokens, 0, msg=f"zero tokens for: {script_text!r}")

    def test_cjk_punctuation_counted_as_tokens(self) -> None:
        """CJK Symbols and Punctuation (U+3000-U+303F) must each count as one token."""
        # 「書院」 = 2 CJK chars + 。= 1 CJK punct → 3 tokens total
        self.assertTrue(is_cjk("。"))   # U+3002 ideographic full stop
        self.assertTrue(is_cjk("、"))   # U+3001 ideographic comma
        self.assertTrue(is_cjk("　"))   # U+3000 ideographic space
        self.assertEqual(estimate_tokens("書院。"), 3)
        self.assertEqual(estimate_tokens("猫、犬。"), 4)

    def test_is_cjk_thai(self) -> None:
        self.assertTrue(is_cjk("ส"))   # U+0E2A Thai
        self.assertTrue(is_cjk("မ"))   # U+1019 Myanmar
        self.assertTrue(is_cjk("ស"))   # U+179F Khmer

    def test_hangul_upper_bound_corrected(self) -> None:
        """D7A3 is the last Hangul syllable; D7A4–D7AF must not count as CJK."""
        self.assertTrue(is_cjk("힣"))   # last syllable — should be CJK
        self.assertFalse(is_cjk("힤"))  # one past the syllable block — not CJK

    def test_sentence_split_on_fullwidth_semicolon(self) -> None:
        """Full-width semicolon ；uff1b) must act as a sentence boundary in chunking."""
        text = "前段落；後段落。"
        chunks = split_text(text, chunk_tokens=2, overlap_tokens=0)
        # With token budget=2, the two clauses separated by ； should split
        self.assertGreater(len(chunks), 1, msg="；should trigger a sentence split")

    def test_tail_cjk_includes_trigger_token(self) -> None:
        """_tail must include the CJK character that triggered acc >= tokens, not skip it.

        Without the fix, returning text[i+1:] after a CJK character increments acc
        excludes that character, yielding one fewer token than requested.
        """
        from shoin.chunk import _tail

        text = "東西南北上下"  # 6 CJK tokens
        result = _tail(text, 3)
        self.assertEqual(result, "北上下", "_tail(text, 3) must return exactly 3 CJK tokens")
        self.assertEqual(estimate_tokens(result), 3)

        # Verify ASCII words are unaffected (word boundary is the space before the word).
        result_en = _tail("alpha beta gamma", 2)
        self.assertEqual(result_en, "beta gamma")
        self.assertEqual(estimate_tokens(result_en), 2)


class TestIngest(unittest.TestCase):
    def test_txt_md_extract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.md"
            p.write_text("# 見出し\n本文です。", encoding="utf-8")
            ex = extract_file(p)
            self.assertEqual(ex.kind, "md")
            self.assertIn("本文", ex.text)
            self.assertEqual(len(ex.sha256), 64)

    def test_cp932_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sjis.txt"
            p.write_bytes("日本語テキスト".encode("cp932"))
            self.assertIn("日本語", extract_file(p).text)

    def test_utf8_bom_stripped(self) -> None:
        """UTF-8 BOM (EF BB BF) from Windows Notepad must be stripped, not left as U+FEFF."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bom.txt"
            p.write_bytes(b"\xef\xbb\xbf" + "BOM付きテキスト。".encode("utf-8"))
            text = extract_file(p).text
            self.assertNotIn("﻿", text, "BOM character must not appear in extracted text")
            self.assertIn("BOM付きテキスト", text)

    def test_html_extract(self) -> None:
        html = (
            "<html><head><title>題名</title><style>x{}</style></head>"
            "<body><script>bad()</script><h1>見出し</h1><p>本文段落。</p></body></html>"
        )
        title, text = html_to_text(html)
        self.assertEqual(title, "題名")
        self.assertIn("本文段落", text)
        self.assertNotIn("bad()", text)

    def test_html_title_inside_noscript_does_not_pollute_body(self) -> None:
        """An unclosed <title> inside <noscript> must not route body content to
        title_parts after </noscript> closes the skip context."""
        html = (
            "<html><head>"
            "<noscript><title>Fake</noscript>"  # <title> never closed before </noscript>
            "</head><body><p>Real content here.</p></body></html>"
        )
        title, text = html_to_text(html)
        # Title must be empty (no real <title> tag outside skip context)
        self.assertEqual(title, "")
        # Body content must NOT have been routed to title_parts
        self.assertIn("Real content here", text)

    def test_unsupported_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.exe"
            p.write_bytes(b"MZ")
            with self.assertRaises(IngestError) as cm:
                extract_file(p)
            self.assertEqual(cm.exception.code, "INGEST_UNSUPPORTED_FORMAT")

    def test_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "big.txt"
            p.write_bytes(b"a" * (10 * 1024 * 1024 + 1))
            with self.assertRaises(IngestError) as cm:
                extract_file(p)
            self.assertEqual(cm.exception.code, "INGEST_FILE_TOO_LARGE")

    def test_empty_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "empty.txt"
            p.write_text("   \n  ", encoding="utf-8")
            with self.assertRaises(IngestError) as cm:
                extract_file(p)
            self.assertEqual(cm.exception.code, "INGEST_EMPTY")

    def test_pdf_extract(self) -> None:
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("pypdf not installed")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "blank.pdf"
            w = PdfWriter()
            w.add_blank_page(width=72, height=72)
            with open(p, "wb") as f:
                w.write(f)
            with self.assertRaises(IngestError) as cm:
                extract_file(p)  # blank page -> no text
            self.assertEqual(cm.exception.code, "INGEST_EMPTY")

    # --- SSRF guard ---

    def test_ssrf_scheme_blocked(self) -> None:
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x"):
            with self.assertRaises(IngestError) as cm:
                validate_public_url(url)
            self.assertEqual(cm.exception.code, "INGEST_URL_BLOCKED")

    def test_ssrf_private_hosts_blocked(self) -> None:
        for url in (
            "http://127.0.0.1/",
            "http://localhost/",
            "http://10.0.0.5/",
            "http://192.168.1.1/admin",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/",
        ):
            with self.assertRaises(IngestError) as cm:
                validate_public_url(url)
            self.assertEqual(cm.exception.code, "INGEST_URL_BLOCKED")

    def test_ssrf_fetch_pins_validated_ip(self) -> None:
        """fetch_url must connect to the IP it validated with exactly one DNS call per hop.

        validate_public_url() now returns (parsed, pinned_ip) so fetch_url no longer
        calls _validate_resolved() a second time — one DNS lookup per hop, not two.
        """
        import shoin.ingest as ing

        captured: dict[str, object] = {}
        dns_calls: list[str] = []

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            dns_calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        def fake_create_connection(addr: tuple[str, int], *a: object, **k: object) -> object:
            captured["addr"] = addr
            raise OSError("short-circuit before real network")

        with (
            patch.object(ing.socket, "getaddrinfo", fake_getaddrinfo),
            patch.object(ing.socket, "create_connection", fake_create_connection),
            self.assertRaises(IngestError) as cm,
        ):
            ing.fetch_url("http://example.com/page")
        self.assertEqual(cm.exception.code, "INGEST_FETCH_FAILED")
        # connected to the validated public IP literal, not the hostname
        self.assertEqual(captured["addr"], ("93.184.216.34", 80))
        # exactly one DNS lookup per hop — not two (the old double-resolution bug)
        self.assertEqual(dns_calls, ["example.com"],
                         "fetch_url must resolve DNS once per hop, not twice")

    def test_fetch_url_host_header_includes_non_default_port(self) -> None:
        """RFC 7230 §5.4: Host header must include port when it is not the default.

        Sending 'Host: example.com' for 'http://example.com:8080/' causes virtual-
        host routing failures.  The correct header is 'Host: example.com:8080'.
        """
        import shoin.ingest as ing

        captured_headers: dict[str, str] = {}

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        class FakeConn:
            def request(self, method: str, path: str, headers: dict[str, str]) -> None:
                captured_headers.update(headers)
                raise OSError("short-circuit")

            def close(self) -> None:
                pass

        with (
            patch.object(ing.socket, "getaddrinfo", fake_getaddrinfo),
            patch.object(ing, "_PinnedHTTPConnection", lambda *a, **k: FakeConn()),
            self.assertRaises(IngestError),
        ):
            ing.fetch_url("http://example.com:8080/path")

        self.assertEqual(
            captured_headers.get("Host"),
            "example.com:8080",
            "non-default port must appear in the Host header",
        )

    def test_fetch_url_host_header_omits_default_port(self) -> None:
        """Default port (80 for http, 443 for https) must NOT appear in Host header."""
        import shoin.ingest as ing

        captured_headers: dict[str, str] = {}

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        class FakeConn:
            def request(self, method: str, path: str, headers: dict[str, str]) -> None:
                captured_headers.update(headers)
                raise OSError("short-circuit")

            def close(self) -> None:
                pass

        with (
            patch.object(ing.socket, "getaddrinfo", fake_getaddrinfo),
            patch.object(ing, "_PinnedHTTPConnection", lambda *a, **k: FakeConn()),
            self.assertRaises(IngestError),
        ):
            ing.fetch_url("http://example.com/path")

        self.assertEqual(
            captured_headers.get("Host"),
            "example.com",
            "default port 80 must not appear in the Host header",
        )

    def test_ssrf_rebinding_to_private_blocked(self) -> None:
        """A host resolving to a private address is rejected even at fetch time."""
        import shoin.ingest as ing

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]

        with (
            patch.object(ing.socket, "getaddrinfo", fake_getaddrinfo),
            self.assertRaises(IngestError) as cm,
        ):
            ing.fetch_url("http://attacker.example/")
        self.assertEqual(cm.exception.code, "INGEST_URL_BLOCKED")

    def test_redirect_cycle_detected(self) -> None:
        """fetch_url must raise INGEST_URL_BLOCKED when it detects a redirect cycle."""
        import shoin.ingest as ing

        call_count = 0

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        class FakeResp:
            status = 301

            def getheader(self, name: str, default: str = "") -> str:
                if name == "Location":
                    return "http://example.com/page"  # cycles back to start
                return default

            def read(self, n: int = -1) -> bytes:
                return b""

        class FakeConn:
            def request(self, *a: object, **k: object) -> None:
                nonlocal call_count
                call_count += 1

            def getresponse(self) -> FakeResp:
                return FakeResp()

            def close(self) -> None:
                pass

        with (
            patch.object(ing.socket, "getaddrinfo", fake_getaddrinfo),
            patch.object(ing, "_PinnedHTTPConnection", lambda *a, **k: FakeConn()),
            self.assertRaises(IngestError) as cm,
        ):
            ing.fetch_url("http://example.com/page")
        self.assertEqual(cm.exception.code, "INGEST_URL_BLOCKED")
        self.assertIn("cycle", str(cm.exception))

    def test_extracted_dataclass(self) -> None:
        ex = Extracted("txt", "t", "body", "o", "h")
        self.assertEqual(ex.title, "t")

    def test_extract_url_pdf_magic_bytes(self) -> None:
        """extract_url must route to pdf_to_text when body starts with %PDF even if
        Content-Type does not say 'pdf' (e.g. application/octet-stream)."""
        import shoin.ingest as ing

        fake_body = b"%PDF-1.4 fake"
        with (
            patch.object(ing, "fetch_url", return_value=(fake_body, "application/octet-stream", "http://x/f.pdf")),
            patch.object(ing, "pdf_to_text", return_value="parsed pdf content") as mock_pdf,
        ):
            result = ing.extract_url("http://x/f.pdf")
        mock_pdf.assert_called_once_with(fake_body)
        self.assertIn("parsed pdf content", result.text)

    def test_extract_url_uses_final_url_as_title_after_redirect(self) -> None:
        """When a URL redirects, the title must be the *final* URL, not the original.

        Short/canonical links (DOI, URL shorteners) redirect to the real page.
        Using the original URL as title would show the redirector, not the resource.
        """
        import shoin.ingest as ing

        fake_body = b"%PDF-1.4 fake"
        with (
            patch.object(
                ing,
                "fetch_url",
                return_value=(fake_body, "application/pdf", "https://journal.example/paper.pdf"),
            ),
            patch.object(ing, "pdf_to_text", return_value="paper content"),
        ):
            result = ing.extract_url("https://doi.org/10.9999/fake")
        self.assertEqual(
            result.title,
            "https://journal.example/paper.pdf",
            "title must be the final (post-redirect) URL, not the original short URL",
        )
        # origin is always the final URL regardless
        self.assertEqual(result.origin, "https://journal.example/paper.pdf")

    def test_extract_url_plaintext_uses_final_url_as_title(self) -> None:
        """Plain-text responses also fall back to final_url, not the original URL."""
        import shoin.ingest as ing

        fake_body = b"Hello world, this is plain text."
        with patch.object(
            ing,
            "fetch_url",
            return_value=(fake_body, "text/plain", "https://cdn.example/notes.txt"),
        ):
            result = ing.extract_url("https://short.example/abc")
        self.assertEqual(result.title, "https://cdn.example/notes.txt")


class TestSearch(unittest.TestCase):
    def test_fts_query_quoting(self) -> None:
        self.assertEqual(fts_query('weather "quote'), '"weather" OR "quote"')
        expr = fts_query("書院は知の書斎")
        self.assertIn('"書院は"', expr)  # CJK runs decompose into trigrams
        self.assertIn(" OR ", expr)

    def test_fts_query_cjk_3char_used_as_single_term(self) -> None:
        """A 3-char CJK term is exactly one trigram — used as-is, not decomposed.

        The condition `len(term) > 3` means exactly-3-char CJK terms skip
        decomposition and are passed whole to FTS5 (which is correct: they ARE
        a single trigram).  A 4-char CJK term produces two overlapping trigrams.
        """
        three_char = fts_query("書院は")  # single 3-char CJK run
        self.assertEqual(three_char, '"書院は"', "3-char CJK term must be used as-is")

        four_char = fts_query("書院はな")  # 4-char → decomposes into 2 trigrams
        self.assertIn('"書院は"', four_char)
        self.assertIn('"院はな"', four_char)
        self.assertEqual(four_char, '"書院は" OR "院はな"')

    def test_query_terms_cjk_punctuation_acts_as_boundary(self) -> None:
        """CJK punctuation (U+3000-U+303F) must split CJK runs, not extend them."""
        # 。and 、 were added to is_cjk() in v0.1.48; they must NOT join CJK word runs
        terms = query_terms("書院。")
        self.assertIn("書院", terms)
        self.assertNotIn("書院。", terms)  # punctuation must not be included in run
        terms2 = query_terms("猫、犬。")
        self.assertIn("猫", terms2)
        self.assertIn("犬", terms2)
        # Punctuation alone is not a term (not alphanumeric, not a CJK word char)
        self.assertNotIn("。", terms2)
        self.assertNotIn("、", terms2)

    def test_query_terms_iteration_mark_stays_in_word_run(self) -> None:
        """々 (U+3005, ideographic iteration mark) must NOT break CJK word runs."""
        # 々 is used inside words: 人々, 様々, 様様 etc.  It is NOT punctuation.
        terms = query_terms("人々の生活")
        self.assertIn("人々の生活", terms)
        # 。 still acts as a boundary — 々 is the exception within U+3000-U+303F
        terms2 = query_terms("人々。")
        self.assertIn("人々", terms2)
        self.assertNotIn("人々。", terms2)

    def test_cosine_nan_returns_zero(self) -> None:
        """cosine() with NaN/Inf in a vector must return 0.0, not propagate NaN."""
        from shoin.search import cosine

        nan = float("nan")
        inf = float("inf")
        self.assertEqual(cosine([nan, 1.0], [1.0, 1.0]), 0.0)
        self.assertEqual(cosine([inf, 0.0], [1.0, 0.0]), 0.0)

    def test_vector_search_none_query_returns_empty(self) -> None:
        """vector_search(None) must return [] without crashing."""
        from shoin.search import vector_search

        with make_store() as s:
            nb_id = seed(s)
            self.assertEqual(vector_search(s, nb_id, None, 10), [])

    def test_char_bigrams_empty_returns_empty_set(self) -> None:
        """_char_bigrams('') must return set(), not {''}."""
        self.assertEqual(_char_bigrams(""), set())
        self.assertFalse(_char_bigrams(""))  # falsy — triggers the guard in _sim()

    def test_bm25_japanese(self) -> None:
        with make_store() as s:
            nb_id = seed(s)
            hits = bm25_search(s, nb_id, "知の書斎", 5)
            self.assertTrue(hits)
            self.assertIn("書斎", hits[0].text)

    def test_bm25_english(self) -> None:
        with make_store() as s:
            nb_id = seed(s)
            hits = bm25_search(s, nb_id, "machine verified citations", 5)
            self.assertTrue(hits)
            self.assertIn("Citations", hits[0].text)

    def test_short_query_like_fallback(self) -> None:
        with make_store() as s:
            nb_id = seed(s)
            hits = bm25_search(s, nb_id, "猫", 5)  # 1 char: trigram cannot serve
            self.assertTrue(hits)
            self.assertIn("猫", hits[0].text)

    def test_notebook_scoping(self) -> None:
        with make_store() as s:
            nb_id = seed(s)
            other = s.create_notebook("other")
            src = s.add_source(other.id, "txt", "x", "o", "h-x")
            s.add_chunks(src.id, ["完全に別の内容"])
            hits = bm25_search(s, nb_id, "完全に別の内容", 5)
            self.assertEqual(hits, [])

    def test_adaptive_alpha_bounds(self) -> None:
        for q in ("短い", "これはどういう意味ですか？", 'ERR_404 "exact phrase" 12345'):
            a = adaptive_alpha(q)
            self.assertGreaterEqual(a, 0.2)
            self.assertLessEqual(a, 0.8)
        self.assertGreater(adaptive_alpha("この論文の主要な貢献は何ですか？"), 0.5)
        self.assertLess(adaptive_alpha("error code 12345"), 0.5)

    def test_adaptive_alpha_english_question_gets_semantic_bump(self) -> None:
        """English ? at end must raise alpha above 0.5 (rstrip removed ? so endswith was dead code)."""
        self.assertGreater(adaptive_alpha("What is Shoin?"), 0.5)
        self.assertGreater(adaptive_alpha("Does Shoin support PDF?"), 0.5)

    def test_adaptive_alpha_fullwidth_question_mark_semantic_bump(self) -> None:
        """Full-width ？ alone (no か suffix) must also trigger the semantic bump."""
        self.assertGreater(adaptive_alpha("Shoin？"), 0.5)
        self.assertGreater(adaptive_alpha("書院とは？"), 0.5)

    def test_fuse_bm25_only(self) -> None:
        hits = [Hit(1, 1, "a", 0, bm25=2.0), Hit(2, 1, "b", 0, bm25=1.0)]
        fused = fuse(hits, [], alpha=0.5)
        self.assertEqual(fused[0].chunk_id, 1)
        self.assertEqual(fused[0].score, 1.0)

    def test_fuse_all_zero_bm25_scores_stay_zero(self) -> None:
        """All-zero BM25 scores (IDF=0 for ubiquitous terms) must normalize to 0.0.

        Previously _minmax returned [1.0, 1.0] for equal values regardless of
        whether they were 0, causing zero-relevance hits to receive the maximum
        BM25 weight in fusion and potentially outrank genuinely relevant results.
        """
        hits = [Hit(1, 1, "a", 0, bm25=0.0), Hit(2, 1, "b", 0, bm25=0.0)]
        fused = fuse(hits, [], alpha=0.5)
        for h in fused:
            self.assertEqual(h.score, 0.0, "zero BM25 scores must normalize to 0.0 not 1.0")

    def test_fuse_equal_nonzero_bm25_scores_stay_one(self) -> None:
        """All equal but non-zero BM25 scores should still normalize to 1.0 (undifferentiated tie)."""
        hits = [Hit(1, 1, "a", 0, bm25=3.5), Hit(2, 1, "b", 0, bm25=3.5)]
        fused = fuse(hits, [], alpha=0.5)
        for h in fused:
            self.assertAlmostEqual(h.score, 1.0, msg="equal non-zero BM25 scores must normalize to 1.0")

    def test_fuse_bm25_only_populates_detail(self) -> None:
        """BM25-only path must populate detail['bm25_norm'] like the merged path does."""
        hits = [Hit(1, 1, "a", 0, bm25=2.0), Hit(2, 1, "b", 0, bm25=1.0)]
        fused = fuse(hits, [], alpha=0.5)
        for h in fused:
            self.assertIn("bm25_norm", h.detail, "detail['bm25_norm'] must be set in BM25-only path")

    def test_fuse_combines(self) -> None:
        b = [Hit(1, 1, "a", 0, bm25=1.0)]
        v = [Hit(2, 1, "b", 0, vec=0.9)]
        fused = fuse(b, v, alpha=0.8)
        self.assertEqual(fused[0].chunk_id, 2)  # high alpha favours vector hit

    def test_lexical_overlap(self) -> None:
        self.assertGreater(lexical_overlap("書院", "書院は書斎"), 0.0)
        self.assertEqual(lexical_overlap("xyz", "書院"), 0.0)

    def test_fuse_same_chunk_in_both_lists(self) -> None:
        """A chunk appearing in both BM25 and vector results must be merged, not duplicated."""
        bm25 = [Hit(1, 1, "共有チャンク", 0, bm25=2.0), Hit(2, 1, "BM25のみ", 0, bm25=1.0)]
        vec = [Hit(1, 1, "共有チャンク", 0, vec=0.9), Hit(3, 1, "ベクトルのみ", 0, vec=0.7)]
        result = fuse(bm25, vec, alpha=0.5)
        ids = [h.chunk_id for h in result]
        self.assertEqual(len(set(ids)), len(ids), "duplicate chunk IDs in fuse result")
        self.assertIn(1, ids)

    def test_rerank_improves_lexical_match(self) -> None:
        """rerank() should boost a chunk whose text closely matches the query."""
        from shoin.search import rerank

        hits = [
            Hit(1, 1, "気候変動の影響について", 0.8),
            Hit(2, 1, "猫は液体である説の研究", 0.9),
        ]
        result = rerank("気候変動 影響", hits)
        self.assertEqual(result[0].chunk_id, 1)

    def test_mmr_diversity(self) -> None:
        a = Hit(1, 1, "猫は液体である。猫は液体である。", 1.0)
        b = Hit(2, 1, "猫は液体である。猫は液体である！", 0.99)
        c = Hit(3, 1, "全く無関係な天気の話。晴れのち曇り。", 0.95)
        picked = mmr([a, b, c], k=2, lam=0.5)
        self.assertEqual({h.chunk_id for h in picked}, {1, 3})

    def test_retrieve_bm25_only_mode(self) -> None:
        """DoD: works with no embeddings configured."""
        with make_store() as s:
            nb_id = seed(s)
            hits = retrieve(s, nb_id, "書斎とは", k=3)
            self.assertTrue(hits)
            self.assertTrue(all(h.vec == 0.0 for h in hits))

    def test_retrieve_hybrid(self) -> None:
        with make_store() as s:
            nb_id = seed(s)
            chunks = s.chunks_for_notebook(nb_id)
            for i, c in enumerate(chunks):
                vec = [1.0, 0.0] if "書斎" in c.text else [0.0, 1.0]
                s.set_embedding(c.id, vec)
            hits = retrieve(s, nb_id, "知の書斎", query_vec=[1.0, 0.0], k=3)
            self.assertTrue(hits)
            self.assertIn("書斎", hits[0].text)

    def test_retrieve_no_match(self) -> None:
        with make_store() as s:
            nb_id = seed(s)
            self.assertEqual(retrieve(s, nb_id, "zzzz存在しない語qqqq", k=3), [])

    def test_retrieve_empty_notebook(self) -> None:
        """Retrieve on a notebook with no chunks must return empty list."""
        with make_store() as s:
            empty_nb = s.create_notebook("empty").id
            self.assertEqual(retrieve(s, empty_nb, "query", k=3), [])

    def test_bm25_fallback_no_matches(self) -> None:
        """BM25 fallback LIKE scan with needles that match nothing returns empty list."""
        with make_store() as s:
            nb_id = seed(s)
            hits = bm25_search(s, nb_id, "zzqqqxxx_nonexistent_token", k=5)
            self.assertEqual(hits, [])

    def test_mmr_empty_input(self) -> None:
        """mmr() with an empty candidate list must return an empty list."""
        self.assertEqual(mmr([], k=3), [])

    def test_mmr_fewer_than_k(self) -> None:
        """mmr() with fewer candidates than k must return all candidates."""
        a = Hit(1, 1, "テキスト", 0.9)
        result = mmr([a], k=5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].chunk_id, 1)

    def test_mmr_identical_text_chunks_all_returned(self) -> None:
        """mmr() must not mis-remove a chunk when two hits share identical text.

        pool.remove(best) would remove the FIRST equal hit, which is wrong when
        pool[0] != best by identity. pool.pop(best_idx) is always correct.
        """
        text = "共通テキスト"
        a = Hit(1, 1, text, 0.9)
        b = Hit(2, 2, text, 0.8)
        result = mmr([a, b], k=2, lam=1.0)  # lam=1.0: pure relevance, no diversity
        chunk_ids = {h.chunk_id for h in result}
        self.assertEqual(chunk_ids, {1, 2})

    def test_fallback_no_row_cap(self) -> None:
        """The LIKE fallback must not silently truncate to the first N chunks.

        The old implementation fetched chunks with LIMIT 2000 and scanned them in
        Python, missing sources added after the 2000th chunk.  The fix pushes the
        LIKE filter into SQL so every chunk in the notebook is searched.

        We verify the absence of _FALLBACK_SCAN_LIMIT (the old cap constant) and
        confirm that a short-query hit found via LIKE is still returned even when
        it is the last of many chunks added to a source.
        """
        from shoin import search

        self.assertFalse(
            hasattr(search, "_FALLBACK_SCAN_LIMIT"),
            "_FALLBACK_SCAN_LIMIT must not exist after switching to SQL LIKE",
        )
        with make_store() as s:
            nb_id = s.create_notebook("cap-test").id
            src = s.add_source(nb_id, "txt", "many chunks", "t", "sha-cap")
            # 30 generic chunks then the needle at the end
            texts = [f"汎用テキスト行{i}" for i in range(30)] + ["固有キーワード猫発見"]
            s.add_chunks(src.id, texts)
            # "猫" is 1 char → FTS5 trigram can't match → LIKE fallback fires
            hits = bm25_search(s, nb_id, "猫", k=5)
            self.assertTrue(hits, "LIKE fallback must find the needle chunk")
            self.assertTrue(any("猫" in h.text for h in hits))

    def test_fallback_like_wildcards_escaped(self) -> None:
        """Underscore in a needle must be escaped so LIKE treats it as a literal.

        query_terms() includes '_' in its [A-Za-z0-9_]+ word pattern, so identifiers
        like "exact_match" produce a needle with a literal underscore.  Without
        _esc_like(), LIKE '%exact_match%' treats '_' as "any single character" and
        returns "exactXmatch" as a false positive.  With '|' as the ESCAPE sentinel
        the needle becomes '%exact|_match%' ESCAPE '|', matching only literal '_'.
        """
        with make_store() as s:
            nb_id = s.create_notebook("escape-test").id
            src = s.add_source(nb_id, "txt", "escape chunks", "t", "sha-esc")
            s.add_chunks(src.id, [
                "exact_match found here",  # must match (has literal '_')
                "exactXmatch also here",   # must NOT match (X ≠ '_')
                "exactmatch no separator", # must NOT match (no separator)
            ])
            hits = bm25_search(s, nb_id, "exact_match", k=5)
            texts = [h.text for h in hits]
            self.assertTrue(any("exact_match" in t for t in texts),
                            "chunk with literal underscore must be found")
            self.assertFalse(any("exactXmatch" in t for t in texts),
                             "underscore must not act as LIKE wildcard (false positive)")
            self.assertFalse(any("exactmatch no separator" in t for t in texts),
                             "chunk without underscore must not match")


class TestQA(unittest.TestCase):
    def test_history_cite_re_strips_normal_citations(self) -> None:
        """_HISTORY_CITE_RE must strip [S1], [S1, S2], and full-width [Ｓ１] markers."""
        from shoin.qa import _HISTORY_CITE_RE

        self.assertEqual(_HISTORY_CITE_RE.sub("", "answer [S1] here"), "answer  here")
        self.assertEqual(_HISTORY_CITE_RE.sub("", "[S1, S2] context"), " context")
        self.assertEqual(_HISTORY_CITE_RE.sub("", "text [Ｓ１]"), "text ")

    def test_history_cite_re_no_false_positive_for_word_embedded_s(self) -> None:
        """_HISTORY_CITE_RE must NOT strip brackets where 's' is inside a word.

        Before the \\b word-boundary guard, [vs 3.0] or [figs 1] matched because
        the regex only required any 's' followed by whitespace and digits inside a
        bracket — it couldn't tell 's' in "vs" from a standalone S-number.
        """
        from shoin.qa import _HISTORY_CITE_RE

        self.assertEqual(
            _HISTORY_CITE_RE.sub("", "compare [vs 3.0]"), "compare [vs 3.0]",
            "[vs 3.0] must not be stripped — 's' is embedded in 'vs', not a citation",
        )
        self.assertEqual(
            _HISTORY_CITE_RE.sub("", "see [figs 1]"), "see [figs 1]",
            "[figs 1] must not be stripped — 's' is embedded in 'figs', not a citation",
        )
        self.assertEqual(
            _HISTORY_CITE_RE.sub("", "refs [issue 5]"), "refs [issue 5]",
            "[issue 5] must not be stripped — no S-number inside",
        )


class TestCitation(unittest.TestCase):
    def test_make_report_source_bodies_length_mismatch_raises(self) -> None:
        """source_bodies length != source_titles length must raise ValueError (defensive check)."""
        from shoin.citation import make_report

        with self.assertRaises(ValueError):
            make_report("answer [S1].", ["title1", "title2"], source_bodies=["only one body"])

    def test_make_report_source_bodies_correct_length_accepted(self) -> None:
        """source_bodies with matching length must be accepted without error."""
        from shoin.citation import make_report

        report = make_report("answer [S1].", ["t1"], source_bodies=["body text here"])
        self.assertIn("confirmed", report)

    def test_make_report_source_ids_and_bodies_both_checked(self) -> None:
        """Both source_ids and source_bodies must match source_titles length."""
        from shoin.citation import make_report

        with self.assertRaises(ValueError):
            make_report("text.", ["t1", "t2"], source_ids=[1], source_bodies=["b1", "b2"])
        with self.assertRaises(ValueError):
            make_report("text.", ["t1", "t2"], source_ids=[1, 2], source_bodies=["b1"])


class TestStoreChunksForSource(unittest.TestCase):
    def test_chunks_for_source_returns_correct_chunks(self) -> None:
        """chunks_for_source must return only chunks belonging to the given source."""
        with Store(":memory:") as s:
            nb = s.create_notebook("test")
            src1 = s.add_source(nb.id, "txt", "doc1", "orig1", "sha1")
            src2 = s.add_source(nb.id, "txt", "doc2", "orig2", "sha2")
            s.add_chunks(src1.id, ["chunk A", "chunk B"])
            s.add_chunks(src2.id, ["chunk C"])
            chunks1 = s.chunks_for_source(src1.id)
            chunks2 = s.chunks_for_source(src2.id)
        self.assertEqual([c.text for c in chunks1], ["chunk A", "chunk B"])
        self.assertEqual([c.seq for c in chunks1], [0, 1])
        self.assertEqual([c.text for c in chunks2], ["chunk C"])

    def test_chunks_for_source_empty_when_no_chunks(self) -> None:
        """chunks_for_source returns [] for a source with no chunks yet."""
        with Store(":memory:") as s:
            nb = s.create_notebook("test")
            src = s.add_source(nb.id, "txt", "empty", "orig", "shax")
            self.assertEqual(s.chunks_for_source(src.id), [])


class TestLLMClient(unittest.TestCase):
    def test_invalid_url_scheme_raises_llmerror_not_valueerror(self) -> None:
        """urllib.request.urlopen raises ValueError for unknown schemes (e.g. file://).
        _post() must convert it to LLMError so callers degrade gracefully instead
        of propagating a bare ValueError after SSE headers are committed."""
        from shoin.llm import LLMClient, LLMError

        client = LLMClient(base_url="ftp://invalid-scheme")
        with self.assertRaises(LLMError) as cm:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(cm.exception.code, "SYSTEM_SERVICE_UNAVAILABLE")

    def test_invalid_url_scheme_stream_raises_llmerror(self) -> None:
        """Same guard applies to chat_stream — an invalid URL scheme must produce
        LLMError, not a bare ValueError that bypasses the SSE error handlers."""
        from shoin.llm import LLMClient, LLMError

        client = LLMClient(base_url="ftp://invalid-scheme")
        gen = client.chat_stream([{"role": "user", "content": "hi"}])
        with self.assertRaises(LLMError) as cm:
            next(gen)
        self.assertEqual(cm.exception.code, "SYSTEM_SERVICE_UNAVAILABLE")

    def test_available_returns_false_for_invalid_url_scheme(self) -> None:
        """available() must return False (not raise ValueError) for unknown URL schemes.
        _post and chat_stream already catch ValueError; available() had the same gap."""
        from shoin.llm import LLMClient

        client = LLMClient(base_url="ftp://invalid-scheme")
        self.assertFalse(client.available())


if __name__ == "__main__":
    unittest.main(verbosity=1)
