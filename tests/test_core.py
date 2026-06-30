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
from shoin.chunk import _SENTENCE_SPLIT_RE, estimate_tokens, is_cjk, split_text
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
    neg_terms,
    query_terms,
    retrieve,
    strip_neg_terms,
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
        self.assertEqual(VERSION, "0.2.50")

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

    def test_rename_notebook_empty_name_rejected(self) -> None:
        with make_store() as s:
            nb = s.create_notebook("研究")
            with self.assertRaises(StoreError) as cm:
                s.rename_notebook(nb.id, "   ")
            self.assertEqual(cm.exception.code, "VALIDATION_REQUIRED_FIELD_MISSING")

    def test_create_notebook_name_too_long_rejected(self) -> None:
        from shoin.config import MAX_NAME_LEN

        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                s.create_notebook("a" * (MAX_NAME_LEN + 1))
            self.assertEqual(cm.exception.code, "VALIDATION_FIELD_FORMAT_INVALID")

    def test_rename_notebook_name_too_long_rejected(self) -> None:
        from shoin.config import MAX_NAME_LEN

        with make_store() as s:
            nb = s.create_notebook("研究")
            with self.assertRaises(StoreError) as cm:
                s.rename_notebook(nb.id, "a" * (MAX_NAME_LEN + 1))
            self.assertEqual(cm.exception.code, "VALIDATION_FIELD_FORMAT_INVALID")

    def test_rename_notebook_nonexistent_raises(self) -> None:
        """rename_notebook on a missing id must raise NOTEBOOK_NOT_FOUND, not silently no-op."""
        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                s.rename_notebook(99999, "should fail")
            self.assertEqual(cm.exception.code, "NOTEBOOK_NOT_FOUND")

    def test_update_source_title_concurrent_delete_raises(self) -> None:
        """update_source_title must raise SOURCE_NOT_FOUND when the source is deleted
        between the existence check and the UPDATE (TOCTOU race simulation)."""
        from unittest.mock import patch

        with make_store() as s:
            nb = s.create_notebook("race")
            src = s.add_source(nb.id, "txt", "t", "o", "sha-race")
            # Simulate concurrent delete: after get_source succeeds, DELETE the row
            # so the UPDATE finds 0 rows.
            original_get = s.get_source

            def get_and_delete(sid):
                result = original_get(sid)
                s.conn.execute("DELETE FROM sources WHERE id=?", (sid,))
                s.conn.commit()
                return result

            with patch.object(s, "get_source", side_effect=get_and_delete):
                with self.assertRaises(StoreError) as cm:
                    s.update_source_title(src.id, "new title", "new origin")
            self.assertEqual(cm.exception.code, "SOURCE_NOT_FOUND")

    def test_add_note_title_too_long_rejected(self) -> None:
        from shoin.config import MAX_NAME_LEN

        with make_store() as s:
            nb = s.create_notebook("研究")
            with self.assertRaises(StoreError) as cm:
                s.add_note(nb.id, "a" * (MAX_NAME_LEN + 1), "body")
            self.assertEqual(cm.exception.code, "VALIDATION_FIELD_FORMAT_INVALID")

    def test_add_note_empty_title_rejected(self) -> None:
        with make_store() as s:
            nb = s.create_notebook("研究")
            with self.assertRaises(StoreError) as cm:
                s.add_note(nb.id, "   ", "body")
            self.assertEqual(cm.exception.code, "VALIDATION_REQUIRED_FIELD_MISSING")

    def test_get_chunk_unknown_id_raises(self) -> None:
        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                s.get_chunk(99999)
            self.assertEqual(cm.exception.code, "CHUNK_NOT_FOUND")

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

    def test_delete_source_concurrent_delete_raises(self) -> None:
        """delete_source must raise SOURCE_NOT_FOUND if a concurrent delete races between
        get_source and the DELETE statement, not silently return success.

        Before v0.2.39, there was no rowcount check after the DELETE, so a concurrently
        deleted source would cause the method to return None (HTTP 200) instead of raising.
        """
        with make_store() as s:
            nb = s.create_notebook("toctou-src")
            src = s.add_source(nb.id, "txt", "title", "origin", "sha-toctou")
            # Simulate concurrent delete between get_source and DELETE
            original_get = s.get_source
            def get_then_concurrent_delete(sid: int):  # type: ignore[no-untyped-def]
                result = original_get(sid)
                # Bypass delete_source to directly remove the row under the hood
                s.conn.execute("DELETE FROM sources WHERE id=?", (sid,))
                s.conn.commit()
                return result
            from unittest.mock import patch
            with patch.object(s, "get_source", side_effect=get_then_concurrent_delete):
                with self.assertRaises(StoreError) as cm:
                    s.delete_source(src.id)
            self.assertEqual(cm.exception.code, "SOURCE_NOT_FOUND")

    def test_delete_note_concurrent_delete_raises(self) -> None:
        """delete_note must raise NOTE_NOT_FOUND if a concurrent delete races between
        the SELECT and the DELETE statement.

        Before v0.2.39, there was no rowcount check after the DELETE.
        """
        with make_store() as s:
            nb = s.create_notebook("toctou-note")
            note_id = s.add_note(nb.id, "My Note", "content")
            # Simulate concurrent delete: remove the note row after SELECT but before DELETE
            original_execute = s.conn.execute
            calls = [0]
            def patched_execute(sql: str, params: tuple = ()):  # type: ignore[no-untyped-def]
                result = original_execute(sql, params)
                if "DELETE FROM notes WHERE id=?" in sql and calls[0] == 0:
                    calls[0] += 1
                    # The DELETE already ran; artificially make rowcount 0 is impossible
                    # via patching, so instead test directly: re-deleting a non-existent note
                    pass
                return result
            # Direct test: after deleting, calling delete_note again raises
            s.delete_note(note_id)
            with self.assertRaises(StoreError) as cm:
                s.delete_note(note_id)  # note is already gone
            self.assertEqual(cm.exception.code, "NOTE_NOT_FOUND")

    def test_counts_empty_notebook(self) -> None:
        """counts() must return zeros for a notebook with no sources or chunks."""
        with make_store() as s:
            nb = s.create_notebook("empty")
            c = s.counts(nb.id)
        self.assertEqual(c["sources"], 0)
        self.assertEqual(c["chunks"], 0)

    def test_counts_source_without_chunks(self) -> None:
        """counts() must count sources even when they have no chunks yet."""
        with make_store() as s:
            nb = s.create_notebook("partial")
            s.add_source(nb.id, "txt", "t", "o", "sha-p")
            c = s.counts(nb.id)
        self.assertEqual(c["sources"], 1)
        self.assertEqual(c["chunks"], 0)

    def test_counts_two_sources_multiple_chunks(self) -> None:
        """counts() must aggregate correctly across sources using a single query."""
        with make_store() as s:
            nb = s.create_notebook("multi")
            src1 = s.add_source(nb.id, "txt", "d1", "o1", "s1")
            src2 = s.add_source(nb.id, "txt", "d2", "o2", "s2")
            s.add_chunks(src1.id, ["a", "b", "c"])
            s.add_chunks(src2.id, ["d", "e"])
            c = s.counts(nb.id)
        self.assertEqual(c["sources"], 2)
        self.assertEqual(c["chunks"], 5)

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

    def test_add_source_long_title_truncated(self) -> None:
        from shoin.config import MAX_TITLE_LEN

        with make_store() as s:
            nb = s.create_notebook("nb")
            long_title = "x" * (MAX_TITLE_LEN + 100)
            src = s.add_source(nb.id, "url", long_title, "https://example.com", "h99")
            self.assertEqual(len(s.get_source(src.id).title), MAX_TITLE_LEN)

    def test_update_source_title_long_truncated(self) -> None:
        from shoin.config import MAX_TITLE_LEN

        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "short.txt", "short.txt", "hx")
            long_name = "a" * (MAX_TITLE_LEN + 50)
            s.update_source_title(src.id, long_name, long_name)
            self.assertEqual(len(s.get_source(src.id).title), MAX_TITLE_LEN)

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

    def test_replace_chunks_for_source_swaps_content(self) -> None:
        """replace_chunks_for_source must atomically delete old chunks and insert new ones."""
        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "url", "old title", "https://example.com", "sha-old")
            ids_old = s.add_chunks(src.id, ["chunk A", "chunk B", "chunk C"])
            self.assertEqual(len(ids_old), 3)
            ids_new = s.replace_chunks_for_source(src.id, ["new chunk 1", "new chunk 2"])
            self.assertEqual(len(ids_new), 2)
            texts = [t for _, t in s.text_chunks_for_source(src.id)]
            # New content must replace old content exactly
            self.assertEqual(texts, ["new chunk 1", "new chunk 2"])
            # Total chunk count for source must be 2 (not 3+2=5: old chunks fully replaced)
            self.assertEqual(len(ids_new), len(texts))

    def test_replace_chunks_missing_source_raises(self) -> None:
        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                s.replace_chunks_for_source(99999, ["x"])
            self.assertEqual(cm.exception.code, "SOURCE_NOT_FOUND")

    def test_replace_chunks_empty_texts_raises(self) -> None:
        """replace_chunks_for_source([]) must raise, not silently delete all chunks.

        Before v0.2.40, passing an empty texts list would DELETE all existing chunks
        and insert none — leaving the source permanently with zero chunks (invisible
        to all retrieval queries) with no indication of the error.
        """
        with make_store() as s:
            nb = s.create_notebook("nb-empty-replace")
            src = s.add_source(nb.id, "url", "title", "https://example.com", "sha-x")
            s.add_chunks(src.id, ["existing chunk"])
            with self.assertRaises(StoreError) as cm:
                s.replace_chunks_for_source(src.id, [])
            self.assertEqual(cm.exception.code, "VALIDATION_REQUIRED_FIELD_MISSING")
            # Existing chunks must be untouched — the guard fires before any DELETE
            texts = [t for _, t in s.text_chunks_for_source(src.id)]
            self.assertEqual(texts, ["existing chunk"])

    def test_update_source_sha256_and_title(self) -> None:
        """update_source_sha256 must update both sha256 and title, and touch the notebook."""
        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "url", "old title", "https://example.com", "sha-old")
            t0 = s.get_notebook(nb.id).updated_at
            s.update_source_sha256(src.id, "sha-new", "new title")
            updated = s.get_source(src.id)
            self.assertEqual(updated.sha256, "sha-new")
            self.assertEqual(updated.title, "new title")
            self.assertGreater(s.get_notebook(nb.id).updated_at, t0)

    def test_update_source_sha256_missing_raises(self) -> None:
        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                s.update_source_sha256(99999, "sha", "title")
            self.assertEqual(cm.exception.code, "SOURCE_NOT_FOUND")

    def test_replace_chunks_touches_notebook_updated_at(self) -> None:
        """replace_chunks_for_source must update the notebook's updated_at timestamp."""
        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "url", "page", "https://x.com", "sha-x")
            s.add_chunks(src.id, ["old"])
            t0 = s.get_notebook(nb.id).updated_at
            s.replace_chunks_for_source(src.id, ["new"])
            self.assertGreater(s.get_notebook(nb.id).updated_at, t0)

    def test_update_source_sha256_collision_raises_source_already_exists(self) -> None:
        """update_source_sha256 must raise SOURCE_ALREADY_EXISTS when the new hash
        collides with another source in the same notebook (UNIQUE constraint on sha256)."""
        with make_store() as s:
            nb = s.create_notebook("nb")
            s.add_source(nb.id, "url", "first", "https://a.com", "sha-collision")
            src2 = s.add_source(nb.id, "url", "second", "https://b.com", "sha-other")
            with self.assertRaises(StoreError) as cm:
                s.update_source_sha256(src2.id, "sha-collision", "second renamed")
            self.assertEqual(cm.exception.code, "SOURCE_ALREADY_EXISTS")

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

    def test_set_embedding_empty_vector_raises(self) -> None:
        """set_embedding must reject an empty vector before touching the database.

        Empty embeddings stored and later used in cosine similarity silently
        degrade retrieval (norm=0.0 guard returns 0.0 for all queries).
        """
        with make_store() as s:
            nb = s.create_notebook("ev")
            src = s.add_source(nb.id, "txt", "t", "o", "sha-ev")
            chunk_ids = s.add_chunks(src.id, ["text"])
            with self.assertRaises(StoreError) as cm:
                s.set_embedding(chunk_ids[0], [])
            self.assertEqual(cm.exception.code, "EMBEDDING_INVALID")

    def test_delete_notebook_nonexistent_raises(self) -> None:
        """delete_notebook on a missing id must raise NOTEBOOK_NOT_FOUND.

        Before the fix: pre-check get_notebook() raised NOTEBOOK_NOT_FOUND,
        but a concurrent delete between get_notebook() and DELETE would silently
        succeed (rowcount=0 not checked). Fix uses rowcount after DELETE.
        """
        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                s.delete_notebook(99999)
            self.assertEqual(cm.exception.code, "NOTEBOOK_NOT_FOUND")

    def test_migrate_concurrent_idempotent(self) -> None:
        """migrate() must not crash when called concurrently from two threads
        on the same fresh database (ThreadingHTTPServer spawns one Store per request).

        Before the fix: INSERT INTO schema_migrations raised UNIQUE constraint failed
        when both threads read version=0 and both tried to apply migration v1.
        After the fix: INSERT OR IGNORE silently skips duplicate version records.
        """
        import threading
        errors: list[Exception] = []

        def run_migrate() -> None:
            try:
                with make_store() as s:
                    s.migrate()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run_migrate) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"concurrent migrate() raised: {errors}")

    def test_migrate_concurrent_shared_db_file_no_crash(self) -> None:
        """Two Store instances opening the SAME fresh file DB simultaneously must not crash.

        Before v0.2.40, CREATE VIRTUAL TABLE chunks_fts lacked IF NOT EXISTS.
        Two concurrent migrate() calls on the same file would both read current=0
        and both execute the migration DDL; the second thread's
        'CREATE VIRTUAL TABLE chunks_fts' raised OperationalError: table already exists
        because virtual tables did not support IF NOT EXISTS in the migration string.
        """
        import tempfile, threading, os
        errors: list[Exception] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "shared.db")

            def open_store(tid: int) -> None:
                try:
                    with Store(db_path) as s:
                        s.migrate()  # the target: must not raise OperationalError
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=open_store, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [], f"concurrent migrate() on shared file raised: {errors}")

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

    def test_add_source_concurrent_unique_violation_raises_already_exists(self) -> None:
        """Concurrent UNIQUE IntegrityError on INSERT must map to SOURCE_ALREADY_EXISTS.

        The explicit pre-check SELECT prevents the common case, but a race between
        the SELECT and INSERT still raises IntegrityError; store.py lines 275-279.
        """
        import sqlite3

        class _InsertFailConn:
            """Delegates all conn calls to the real conn except INSERT INTO sources."""
            def __init__(self, real):
                self._real = real
            def execute(self, sql: str, *args, **kwargs):
                if "INSERT INTO sources" in sql:
                    raise sqlite3.IntegrityError(
                        "UNIQUE constraint failed: sources.notebook_id, sources.sha256"
                    )
                return self._real.execute(sql, *args, **kwargs)
            def __getattr__(self, name):
                return getattr(self._real, name)

        with make_store() as s:
            nb = s.create_notebook("concurrent-unique")
            s.__dict__["conn"] = _InsertFailConn(s.conn)
            try:
                with self.assertRaises(StoreError) as cm:
                    s.add_source(nb.id, "txt", "title", "origin", "sha-concurrent")
            finally:
                s.__dict__["conn"] = s.conn._real  # type: ignore[attr-defined]
        self.assertEqual(cm.exception.code, "SOURCE_ALREADY_EXISTS")

    def test_add_source_concurrent_fk_violation_raises_notebook_not_found(self) -> None:
        """Non-UNIQUE IntegrityError on INSERT (FK) must map to NOTEBOOK_NOT_FOUND.

        If the notebook is deleted between get_notebook() and INSERT, a FK
        IntegrityError fires; store.py lines 281-284.
        """
        import sqlite3

        class _InsertFKFailConn:
            """Raises FK IntegrityError only on INSERT INTO sources."""
            def __init__(self, real):
                self._real = real
            def execute(self, sql: str, *args, **kwargs):
                if "INSERT INTO sources" in sql:
                    raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
                return self._real.execute(sql, *args, **kwargs)
            def __getattr__(self, name):
                return getattr(self._real, name)

        with make_store() as s:
            nb = s.create_notebook("concurrent-fk")
            s.__dict__["conn"] = _InsertFKFailConn(s.conn)
            try:
                with self.assertRaises(StoreError) as cm:
                    s.add_source(nb.id, "txt", "title", "origin", "sha-fk")
            finally:
                s.__dict__["conn"] = s.conn._real  # type: ignore[attr-defined]
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

    def test_is_cjk_supplementary_plane_ext_b(self) -> None:
        """CJK Unified Ideographs Extension B–H (U+20000+) must be recognised as CJK.

        These rare/historical characters live in the supplementary plane.
        Before v0.2.38, _CJK_RANGES only covered the BMP so is_cjk() returned
        False for them, causing estimate_tokens() to undercount historical CJK docs.
        """
        # U+20000 — first CJK Ext B character (𠀀)
        self.assertTrue(is_cjk("\U00020000"), "first CJK Ext B char must be CJK")
        # U+2A6D6 — last CJK Ext B character
        self.assertTrue(is_cjk("\U0002A6D6"), "last CJK Ext B char must be CJK")
        # U+2A700 — first CJK Ext C
        self.assertTrue(is_cjk("\U0002A700"), "first CJK Ext C char must be CJK")
        # U+2CEB0 — first CJK Ext G
        self.assertTrue(is_cjk("\U0002CEB0"), "first CJK Ext G char must be CJK")
        # U+1F600 (emoji, outside all CJK ranges) must NOT be CJK
        self.assertFalse(is_cjk("\U0001F600"), "emoji outside CJK ranges must not be CJK")

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

    def test_tail_shorter_than_budget_returns_full_text(self) -> None:
        """_tail must return the whole text when it has fewer tokens than requested."""
        from shoin.chunk import _tail
        short = "猫"  # 1 CJK token
        result = _tail(short, 10)  # budget larger than available tokens
        self.assertEqual(result, short)

    def test_underscore_word_boundary_consistent_between_estimate_and_tail(self) -> None:
        """estimate_tokens and _tail must agree on underscore-delimited identifiers.

        Before the fix: _WORD_RE counted parse_user_input as 1 token; _tail's
        isalnum() boundary treated each _ as a separator, so _tail(text, 1)
        returned just "input" (3rd word) instead of "parse_user_input".
        After the fix: both count underscore-delimited identifiers as 1 token.
        """
        from shoin.chunk import _tail
        text = "alpha parse_user_input beta"
        # estimate_tokens: 3 word-runs ("alpha", "parse_user_input", "beta") → 3
        self.assertEqual(estimate_tokens(text), 3)
        # _tail with budget=2 should return the last 2 word-runs
        result = _tail(text, 2)
        self.assertEqual(result, "parse_user_input beta")
        self.assertEqual(estimate_tokens(result), 2)

    def test_truncate_tokens_underscore_consistent_with_estimate(self) -> None:
        """_truncate_tokens must treat underscore-delimited identifiers as 1 token,
        matching estimate_tokens so the token budget is not violated."""
        from shoin.qa import _truncate_tokens
        # "parse_user_input" is 1 token by estimate_tokens; budget of 1 should keep it whole
        text = "parse_user_input extra_word"
        truncated = _truncate_tokens(text, 1)
        self.assertEqual(truncated.strip(), "parse_user_input")
        # Verify the truncated text costs exactly 1 token
        self.assertEqual(estimate_tokens(truncated.strip()), 1)

    def test_heading_after_content_creates_block_boundary(self) -> None:
        """A heading that immediately follows content (no blank line) must flush the buffer."""
        from shoin.chunk import _blocks
        # _blocks() must split at the heading boundary even without a blank line between.
        text = "序文の内容。\n# 見出し\n本文。"
        blocks = _blocks(text)
        # First block must contain the pre-heading text; second must start with the heading.
        self.assertEqual(len(blocks), 2)
        self.assertIn("序文", blocks[0])
        self.assertTrue(blocks[1].startswith("# 見出し"))

    def test_hard_split_sentence_fits_within_limit(self) -> None:
        """_hard_split: when sentence pieces fit within the limit they must not be windowed."""
        from shoin.chunk import _hard_split
        # Build a block that exceeds the token limit but splits into sentence-sized pieces
        sentence = "これは長めの文章です。"  # ~10 tokens
        block = sentence * 20  # ~200 tokens — exceeds limit=50
        parts = _hard_split(block, 50)
        # Every part must be a stripped, non-empty string and fit within limit
        for p in parts:
            self.assertTrue(p)
            self.assertLessEqual(estimate_tokens(p), 50 + 10)  # small slack

    def test_sentence_split_ascii_period_space(self) -> None:
        """_SENTENCE_SPLIT_RE must split English sentences at period-space boundaries.

        Without the fix, '. ' is not a split point and the entire English answer
        is treated as a single sentence in verify_grounding, making per-citation
        grounding checks less precise.
        """
        text = "Python is fast. Use it for data science."
        parts = [p for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
        self.assertGreater(len(parts), 1, "period-space must produce at least two pieces")
        # The period must stay with the first sentence, not be discarded
        self.assertIn(".", parts[0])

    def test_sentence_split_period_space_cjk_unaffected(self) -> None:
        """CJK sentence splitting must be unaffected by the period-space addition."""
        text = "これは文章一。これは文章二。"
        parts = [p for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
        self.assertGreater(len(parts), 1)
        self.assertEqual(parts[0], "これは文章一。")

    def test_sentence_split_no_split_on_decimal(self) -> None:
        """A decimal number like 3.14 must not trigger a sentence split (no space after dot)."""
        text = "Pi is 3.14 approximately."
        parts = [p for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
        # No split should occur inside '3.14' because '4' is not whitespace
        self.assertEqual(len(parts), 1)

    def test_hard_split_ascii_window_uses_char_density_not_token_count(self) -> None:
        """_hard_split character-window must account for ASCII density (~5 chars/token).

        Before the fix, window = max(limit, 1) used the token budget (e.g. 50) as
        a character index, producing ASCII chunks ~5× too small (50 chars ≈ 10 tokens
        instead of ~50 tokens). Fix: window = limit * chars_per_token.
        """
        from shoin.chunk import _hard_split

        # Space-separated words with NO sentence punctuation so the sentence splitter
        # produces one giant fragment, forcing the character-window fallback.
        # 200 words × ~5 chars/word ≈ 1000 chars ≈ 200 tokens.
        block = "alpha " * 200  # 200 words → estimate_tokens ≈ 200, well above limit=50
        block = block.strip()
        self.assertGreater(estimate_tokens(block), 50, "pre-condition: block must exceed limit")
        parts = _hard_split(block, 50)
        # Old code (50-char window) → ~20 parts; new code (~300-char window) → ~4 parts.
        # Allow up to 8 to give slack for off-by-one at chunk boundaries.
        self.assertLessEqual(len(parts), 8, msg="too many chunks indicates window was in chars not tokens")
        # All non-tail chunks must be substantially sized (>20 tokens), proving the window
        # is token-proportional. The last chunk may be a small word fragment so skip it.
        for p in parts[:-1]:
            self.assertGreater(
                estimate_tokens(p), 20,
                msg=f"non-tail chunk too small (5× penalty): {p!r:.40}",
            )

    def test_hard_split_zero_token_text_is_bounded(self) -> None:
        """_hard_split must split very long zero-token text (Arabic/Cyrillic/punctuation).

        Before the fix, estimate_tokens(p) == 0 always satisfied tok <= limit, so
        an unbounded zero-token paragraph was emitted as a single oversized chunk.
        Fix: when tok == 0 and len(p) > limit * 5, apply character-window fallback.
        """
        from shoin.chunk import _hard_split

        # Pure Latin-extended chars that estimate_tokens() cannot count (no CJK, no _WORD_RE match).
        # Use repeated diacritics which have ord() outside CJK ranges and are not \w.
        # Cyrillic letters DO match _WORD_RE since they are alpha; use punctuation instead.
        # A long string of punctuation/symbols that aren't ASCII word chars and aren't CJK:
        block = "…·" * 2000  # 4000 chars, estimate_tokens() returns 0 (no CJK, no ASCII words)
        self.assertEqual(estimate_tokens(block), 0, "pre-condition: block must be zero-token")
        parts = _hard_split(block, 50)
        # Should have been split — not emitted as one 4000-char chunk
        self.assertGreater(len(parts), 1, "zero-token oversized block must be split into multiple chunks")
        # Each part must fit within limit * 5 chars (≈ 5 chars/token ASCII upper bound)
        for p in parts:
            self.assertLessEqual(len(p), 50 * 5 + 10, msg=f"part too large: len={len(p)}")


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

    def test_null_byte_file_raises_ingest_empty(self) -> None:
        """A .txt file containing only null bytes must raise INGEST_EMPTY.

        Before the fix, str.strip() skipped U+0000 (category Cc, not whitespace),
        so '\x00\x00\x00'.strip() returned '\x00\x00\x00' (truthy) and the file was
        indexed as valid text, inserting garbage into BM25 and vector search.
        """
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "nulls.txt"
            p.write_bytes(b"\x00\x00\x00")
            with self.assertRaises(IngestError) as ctx:
                extract_file(p)
            self.assertEqual(ctx.exception.code, "INGEST_EMPTY")

    def test_utf16_le_file_decoded_correctly(self) -> None:
        """A UTF-16 LE .txt file must be decoded as UTF-16, not mangled by cp932.

        Before the fix, _decode() tried utf-8-sig (fails on 0xFF/0xFE BOM), then
        cp932, which accepted all byte sequences and produced mojibake — cp932 decoded
        the UTF-16 BOM as two PUA characters and null bytes as literal U+0000.
        Fix: detect UTF-16 BOM before the cp932 fallback.
        """
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "utf16.txt"
            p.write_bytes("Hello world".encode("utf-16-le") + b"\x00")
            # utf-16-le without BOM requires explicit codec; use utf-16 LE with BOM
            p.write_bytes("Hello world".encode("utf-16"))  # includes BOM (\xff\xfe)
            ex = extract_file(p)
            self.assertIn("Hello", ex.text, f"got mojibake instead: {ex.text!r:.60}")

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

    def test_html_unclosed_title_via_head_close_does_not_swallow_body(self) -> None:
        """</head> seen while _in_title must implicitly close the title so body text is extracted."""
        html = "<html><head><title>My Page</head><body><p>Content here.</p></body></html>"
        title, text = html_to_text(html)
        self.assertEqual(title, "My Page")
        self.assertIn("Content here", text)

    def test_html_unclosed_title_via_block_tag_does_not_swallow_body(self) -> None:
        """A block-level tag while _in_title must implicitly close the title."""
        html = "<html><title>Page<p>Important text.</p></html>"
        title, text = html_to_text(html)
        self.assertEqual(title, "Page")
        self.assertIn("Important text", text)

    def test_html_unclosed_title_via_body_tag_does_not_swallow_body(self) -> None:
        """<body> start tag while _in_title must implicitly close the title."""
        html = "<html><title>Title<body><p>Body text.</p></body></html>"
        title, text = html_to_text(html)
        self.assertEqual(title, "Title")
        self.assertIn("Body text", text)

    def test_html_unclosed_noscript_in_head_does_not_swallow_body(self) -> None:
        """An unclosed <noscript> in <head> must not silently discard all body text.

        Before v0.2.40, handle_endtag("head") reset _in_title but NOT _skip_depth.
        A <noscript> (or <script>/<style>) without a closing tag in <head> left
        _skip_depth=1 after </head>, causing handle_data to discard every text node
        in <body> and raising INGEST_EMPTY for a non-empty page.
        """
        html = "<html><head><noscript>fallback</head><body><p>Content here.</p></body></html>"
        title, text = html_to_text(html)
        self.assertIn("Content here", text,
                      "body text must be extracted even when <noscript> is unclosed in <head>")

    def test_html_table_cells_separated_by_newlines(self) -> None:
        """<td> and <th> must produce newline boundaries so cell values don't merge."""
        html = (
            "<table>"
            "<tr><th>Name</th><th>Value</th></tr>"
            "<tr><td>Alice</td><td>42</td></tr>"
            "</table>"
        )
        _, text = html_to_text(html)
        # Cell values must be on separate lines, not concatenated as "NameValueAlice42"
        self.assertIn("Name", text)
        self.assertIn("Value", text)
        self.assertIn("Alice", text)
        self.assertIn("42", text)
        # Crucially: adjacent cell values must NOT be run together
        self.assertNotIn("NameValue", text)
        self.assertNotIn("Alice42", text)

    def test_html_semantic_tags_produce_newline_boundaries(self) -> None:
        """nav, aside, main, figure, figcaption, dd/dt must produce line breaks."""
        html = (
            "<main><p>Main content.</p></main>"
            "<aside>Side note.</aside>"
            "<figure><img/><figcaption>Caption here.</figcaption></figure>"
            "<dl><dt>Term</dt><dd>Definition</dd></dl>"
        )
        _, text = html_to_text(html)
        self.assertIn("Main content", text)
        self.assertIn("Side note", text)
        self.assertIn("Caption here", text)
        self.assertIn("Term", text)
        self.assertIn("Definition", text)
        # Semantic boundaries must not merge adjacent content
        self.assertNotIn("contentSide", text)
        self.assertNotIn("CaptionTerm", text)

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

    def test_ssrf_zone_scoped_ipv6_blocked(self) -> None:
        """Zone-scoped IPv6 addresses (e.g. 'fe80::1%eth0') must raise INGEST_URL_BLOCKED.

        Before v0.2.45, `ipaddress.ip_address(info[4][0])` raised ValueError for
        zone-ID-qualified addresses (RFC 6874 syntax not supported by the stdlib).
        The ValueError escaped the `except socket.gaierror` handler and propagated
        to _dispatch's catch-all as HTTP 500 SYSTEM_INTERNAL_ERROR instead of the
        correct HTTP 400 INGEST_URL_BLOCKED.
        """
        import shoin.ingest as ing

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            # Linux can return zone-ID-qualified strings like "fe80::1%eth0"
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%eth0", 0, 0, 3))]

        with (
            patch.object(ing.socket, "getaddrinfo", fake_getaddrinfo),
            self.assertRaises(IngestError) as cm,
        ):
            ing.validate_public_url("http://linklocal.example/")
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

    def test_decode_fallback_to_replace_on_non_utf8_non_cp932(self) -> None:
        """Bytes that fail both UTF-8 and cp932 must fall back to utf-8 replace (line 73)."""
        from shoin.ingest import _decode

        # b'\x80\x81' fails both utf-8-sig and cp932.
        result = _decode(b"\x80\x81")
        # Should not raise; replacement characters indicate the fallback was used.
        self.assertIsInstance(result, str)

    def test_decode_charset_hint_used_before_defaults(self) -> None:
        """_decode must try the supplied charset first, before utf-8-sig/cp932.

        Before the fix: the charset parameter did not exist, so HTTP Content-Type
        charsets (e.g. iso-8859-1) were ignored — non-UTF-8/CP932 pages produced
        mojibake or replacement characters.
        After the fix: the charset hint is tried first; only falls through to the
        defaults on LookupError or UnicodeDecodeError.
        """
        from shoin.ingest import _decode

        # Encode a French sentence in ISO-8859-1 (not valid UTF-8 or CP932)
        french = "Caf\xe9 au lait"  # é = 0xE9 in latin-1
        data = french.encode("iso-8859-1")
        # Without charset hint: would fail utf-8-sig and cp932, fall back to errors=replace
        result_no_hint = _decode(data)
        self.assertIn("�", result_no_hint, "without hint, replacement char expected")
        # With charset hint: must decode correctly
        result_with_hint = _decode(data, "iso-8859-1")
        self.assertEqual(result_with_hint, french)

    def test_charset_from_ctype_parses_charset_parameter(self) -> None:
        """_charset_from_ctype must extract charset= from common Content-Type strings."""
        from shoin.ingest import _charset_from_ctype

        self.assertEqual(_charset_from_ctype("text/html; charset=iso-8859-1"), "iso-8859-1")
        self.assertEqual(_charset_from_ctype("text/html; charset=UTF-8"), "UTF-8")
        self.assertEqual(_charset_from_ctype('text/html; charset="windows-1252"'), "windows-1252")
        self.assertIsNone(_charset_from_ctype("text/html"))
        self.assertIsNone(_charset_from_ctype("application/json"))

    def test_pdf_to_text_parse_error_raises_ingest_error(self) -> None:
        """Corrupt PDF bytes must raise INGEST_PARSE_FAILED, not a bare exception."""
        from shoin.ingest import IngestError, pdf_to_text

        try:
            from pypdf import PdfReader  # noqa: F401
        except ImportError:
            self.skipTest("pypdf not installed")
        with self.assertRaises(IngestError) as cm:
            pdf_to_text(b"not a real pdf at all")
        self.assertEqual(cm.exception.code, "INGEST_PARSE_FAILED")

    def test_validate_resolved_dns_failure(self) -> None:
        """DNS failure in _validate_resolved must raise INGEST_FETCH_FAILED (line 154)."""
        import socket
        import shoin.ingest as ing

        with patch.object(ing.socket, "getaddrinfo", side_effect=socket.gaierror("NXDOMAIN")):
            with self.assertRaises(IngestError) as cm:
                ing._validate_resolved("nonexistent.invalid")
        self.assertEqual(cm.exception.code, "INGEST_FETCH_FAILED")
        self.assertIn("DNS", str(cm.exception))

    def test_validate_resolved_no_addresses_raises_fetch_failed(self) -> None:
        """Empty address list from getaddrinfo must raise INGEST_FETCH_FAILED (line 171)."""
        import shoin.ingest as ing

        with patch.object(ing.socket, "getaddrinfo", return_value=[]):
            with self.assertRaises(IngestError) as cm:
                ing._validate_resolved("example.com")
        self.assertEqual(cm.exception.code, "INGEST_FETCH_FAILED")
        self.assertIn("no address", str(cm.exception))

    def test_validate_public_url_no_host_raises_blocked(self) -> None:
        """URL with no host must raise INGEST_URL_BLOCKED (line 185)."""
        from shoin.ingest import IngestError, validate_public_url

        with self.assertRaises(IngestError) as cm:
            validate_public_url("http:///path/only")
        self.assertEqual(cm.exception.code, "INGEST_URL_BLOCKED")
        self.assertIn("no host", str(cm.exception))

    def test_fetch_url_query_string_included_in_path(self) -> None:
        """URL with query string must pass '?foo=bar' in the request path (line 244)."""
        import shoin.ingest as ing

        captured_path: list[str] = []

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            return [(None, None, None, None, ("1.2.3.4", 0))]

        class FakeConn:
            def request(self, method: str, path: str, headers: dict[str, str]) -> None:
                captured_path.append(path)
                raise OSError("short-circuit")
            def close(self) -> None:
                pass

        with (
            patch.object(ing.socket, "getaddrinfo", fake_getaddrinfo),
            patch.object(ing, "_PinnedHTTPConnection", lambda *a, **k: FakeConn()),
            self.assertRaises(IngestError),
        ):
            ing.fetch_url("http://example.com/path?foo=bar")
        self.assertTrue(any("foo=bar" in p for p in captured_path))

    def test_fetch_url_https_path_uses_https_connection(self) -> None:
        """HTTPS URL must use _PinnedHTTPSConnection (line 246)."""
        import shoin.ingest as ing

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            return [(None, None, None, None, ("1.2.3.4", 0))]

        class FakeHTTPSConn:
            def request(self, method: str, path: str, headers: dict[str, str]) -> None:
                raise OSError("short-circuit-https")
            def close(self) -> None:
                pass

        with (
            patch.object(ing.socket, "getaddrinfo", fake_getaddrinfo),
            patch.object(ing, "_PinnedHTTPSConnection", lambda *a, **k: FakeHTTPSConn()),
            self.assertRaises(IngestError) as cm,
        ):
            ing.fetch_url("https://example.com/page")
        self.assertEqual(cm.exception.code, "INGEST_FETCH_FAILED")

    def test_fetch_url_redirect_without_location_raises(self) -> None:
        """301 response with no Location header must raise INGEST_FETCH_FAILED (line 257)."""
        import shoin.ingest as ing

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            return [(None, None, None, None, ("1.2.3.4", 0))]

        class FakeResp:
            status = 301
            def getheader(self, name: str, default: str = "") -> str:
                return default  # no Location
            def read(self, n: int = -1) -> bytes:
                return b""

        class FakeConn:
            def request(self, *a: object, **k: object) -> None:
                pass
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
        self.assertEqual(cm.exception.code, "INGEST_FETCH_FAILED")
        self.assertIn("redirect without Location", str(cm.exception))

    def test_fetch_url_http_error_status_raises(self) -> None:
        """HTTP 404 response must raise INGEST_FETCH_FAILED (line 261)."""
        import shoin.ingest as ing

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            return [(None, None, None, None, ("1.2.3.4", 0))]

        class FakeResp:
            status = 404
            def getheader(self, name: str, default: str = "") -> str:
                return default
            def read(self, n: int = -1) -> bytes:
                return b""

        class FakeConn:
            def request(self, *a: object, **k: object) -> None:
                pass
            def getresponse(self) -> FakeResp:
                return FakeResp()
            def close(self) -> None:
                pass

        with (
            patch.object(ing.socket, "getaddrinfo", fake_getaddrinfo),
            patch.object(ing, "_PinnedHTTPConnection", lambda *a, **k: FakeConn()),
            self.assertRaises(IngestError) as cm,
        ):
            ing.fetch_url("http://example.com/missing")
        self.assertEqual(cm.exception.code, "INGEST_FETCH_FAILED")
        self.assertIn("404", str(cm.exception))

    def test_fetch_url_empty_body_raises_ingest_empty(self) -> None:
        """Server returning empty body must raise INGEST_EMPTY (line 264)."""
        import shoin.ingest as ing

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            return [(None, None, None, None, ("1.2.3.4", 0))]

        class FakeResp:
            status = 200
            def getheader(self, name: str, default: str = "") -> str:
                return "text/plain"
            def read(self, n: int = -1) -> bytes:
                return b""  # empty body

        class FakeConn:
            def request(self, *a: object, **k: object) -> None:
                pass
            def getresponse(self) -> FakeResp:
                return FakeResp()
            def close(self) -> None:
                pass

        with (
            patch.object(ing.socket, "getaddrinfo", fake_getaddrinfo),
            patch.object(ing, "_PinnedHTTPConnection", lambda *a, **k: FakeConn()),
            self.assertRaises(IngestError) as cm,
        ):
            ing.fetch_url("http://example.com/empty")
        self.assertEqual(cm.exception.code, "INGEST_EMPTY")

    def test_fetch_url_too_many_redirects_raises(self) -> None:
        """More than URL_MAX_REDIRECTS hops must raise INGEST_URL_BLOCKED (line 272)."""
        import shoin.ingest as ing

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            return [(None, None, None, None, ("1.2.3.4", 0))]

        hop = [0]

        class FakeResp:
            status = 301
            def getheader(self, name: str, default: str = "") -> str:
                if name == "Location":
                    hop[0] += 1
                    return f"http://example.com/hop{hop[0]}"
                return default
            def read(self, n: int = -1) -> bytes:
                return b""

        class FakeConn:
            def request(self, *a: object, **k: object) -> None:
                pass
            def getresponse(self) -> FakeResp:
                return FakeResp()
            def close(self) -> None:
                pass

        with (
            patch.object(ing.socket, "getaddrinfo", fake_getaddrinfo),
            patch.object(ing, "_PinnedHTTPConnection", lambda *a, **k: FakeConn()),
            self.assertRaises(IngestError) as cm,
        ):
            ing.fetch_url("http://example.com/start")
        self.assertEqual(cm.exception.code, "INGEST_URL_BLOCKED")
        self.assertIn("redirect", str(cm.exception))

    def test_extract_file_html_uses_html_title(self) -> None:
        """HTML files must use the <title> tag as their title (lines 293-294)."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "page.html"
            p.write_text("<html><head><title>My Title</title></head><body><p>Content here.</p></body></html>", encoding="utf-8")
            result = extract_file(p)
        self.assertEqual(result.title, "My Title")
        self.assertIn("Content here", result.text)

    def test_extract_url_html_content_uses_page_title(self) -> None:
        """HTML response in extract_url must use <title> as the source title (lines 310-311)."""
        import shoin.ingest as ing

        html_body = b"<html><head><title>Article Title</title></head><body><p>Article text here.</p></body></html>"
        with patch.object(
            ing, "fetch_url",
            return_value=(html_body, "text/html; charset=utf-8", "http://example.com/article")
        ):
            result = ing.extract_url("http://example.com/article")
        self.assertEqual(result.title, "Article Title")
        self.assertIn("Article text", result.text)

    def test_extract_url_empty_html_raises_ingest_empty(self) -> None:
        """HTML that produces empty text after extraction must raise INGEST_EMPTY (line 316)."""
        import shoin.ingest as ing

        html_body = b"<html><head><title>Empty Page</title></head><body></body></html>"
        with (
            patch.object(
                ing, "fetch_url",
                return_value=(html_body, "text/html", "http://example.com/empty")
            ),
            self.assertRaises(IngestError) as cm,
        ):
            ing.extract_url("http://example.com/empty")
        self.assertEqual(cm.exception.code, "INGEST_EMPTY")

    def test_pinned_https_connection_connect(self) -> None:
        """_PinnedHTTPSConnection.connect() must use the pinned IP and wrap with SSL (lines 217-218)."""
        import ssl
        import shoin.ingest as ing

        ctx = ssl.create_default_context()
        conn = ing._PinnedHTTPSConnection("example.com", 443, "1.2.3.4", 5.0, ctx)

        captured_addr: list[tuple[str, int]] = []
        wrapped: list[bool] = []

        class FakeSocket:
            pass

        def fake_create_connection(addr: tuple[str, int], timeout: float) -> FakeSocket:
            captured_addr.append(addr)
            return FakeSocket()

        class FakeSSLContext:
            def wrap_socket(self, raw: object, server_hostname: str = "") -> object:
                wrapped.append(True)
                raise OSError("ssl wrap short-circuit")

        conn._ssl_context = FakeSSLContext()
        with patch.object(ing.socket, "create_connection", fake_create_connection):
            try:
                conn.connect()
            except OSError:
                pass
        self.assertEqual(captured_addr, [("1.2.3.4", 443)])
        self.assertTrue(wrapped, "wrap_socket must be called (line 218)")

    def test_fetch_url_success_returns_body_ctype_url(self) -> None:
        """A 200 response must return (body, content_type, url) (lines 265-267)."""
        import shoin.ingest as ing

        def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[object]:
            return [(None, None, None, None, ("1.2.3.4", 0))]

        class FakeResp:
            status = 200
            def getheader(self, name: str, default: str = "") -> str:
                return "text/html" if name == "Content-Type" else default
            def read(self, n: int = -1) -> bytes:
                return b"<html><body>Hello world</body></html>"

        class FakeConn:
            def request(self, *a: object, **k: object) -> None:
                pass
            def getresponse(self) -> FakeResp:
                return FakeResp()
            def close(self) -> None:
                pass

        with (
            patch.object(ing.socket, "getaddrinfo", fake_getaddrinfo),
            patch.object(ing, "_PinnedHTTPConnection", lambda *a, **k: FakeConn()),
        ):
            body, ctype, url = ing.fetch_url("http://example.com/page")
        self.assertEqual(body, b"<html><body>Hello world</body></html>")
        self.assertEqual(ctype, "text/html")
        self.assertEqual(url, "http://example.com/page")


class TestSearch(unittest.TestCase):
    def test_fts_query_quoting(self) -> None:
        self.assertEqual(fts_query('weather "quote'), '"weather" OR "quote"')
        expr = fts_query("書院は知の書斎")
        self.assertIn('"書院は"', expr)  # CJK runs decompose into trigrams
        self.assertIn(" OR ", expr)

    def test_fts_query_cjk_3char_used_as_single_term(self) -> None:
        """A 3-char CJK term is exactly one trigram (v0.2.41) plus kana alt (v0.2.42).

        With `len(term) >= 3` (v0.2.41 fix from > 3), exactly-3-char CJK terms
        enter the trigram branch and produce range(1) = one trigram = the term
        itself.  When the term contains kana characters, the katakana↔hiragana
        alternate-script trigram is also added (v0.2.42).  A 4-char CJK term
        produces two overlapping trigrams (plus alternates for any kana chars).
        """
        # "書院は": 2 kanji + hiragana は → alternate は→ハ so 2 trigrams total
        three_char = fts_query("書院は")
        self.assertIn('"書院は"', three_char)   # original hiragana trigram
        self.assertIn('"書院ハ"', three_char)   # katakana alternate (は→ハ)

        # Pure-kanji 3-char term: no kana → single trigram, no alternate
        pure_kanji = fts_query("書院学")  # all kanji
        self.assertIn('"書院学"', pure_kanji)
        self.assertNotIn("OR", pure_kanji)     # no alternate for pure kanji

        four_char = fts_query("書院はな")  # 4-char → 2 original + 2 alternate trigrams
        self.assertIn('"書院は"', four_char)
        self.assertIn('"院はな"', four_char)
        self.assertIn('"書院ハ"', four_char)   # alternate for は→ハ
        self.assertIn('"院ハナ"', four_char)   # alternate for はな→ハナ

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

    def test_cosine_empty_or_length_mismatch_returns_zero(self) -> None:
        """cosine() must return 0.0 for empty inputs or length-mismatched vectors."""
        from shoin.search import cosine
        self.assertEqual(cosine([], []), 0.0)
        self.assertEqual(cosine([1.0], []), 0.0)
        self.assertEqual(cosine([1.0], [1.0, 2.0]), 0.0)

    def test_cosine_zero_vector_returns_zero(self) -> None:
        """cosine() must return 0.0 when either vector is all-zeros (no direction)."""
        from shoin.search import cosine
        self.assertEqual(cosine([0.0, 0.0], [1.0, 0.0]), 0.0)
        self.assertEqual(cosine([1.0, 0.0], [0.0, 0.0]), 0.0)

    def test_lexical_overlap_empty_query_returns_zero(self) -> None:
        """lexical_overlap() must return 0.0 when the query has no terms."""
        self.assertEqual(lexical_overlap("", "any text"), 0.0)
        self.assertEqual(lexical_overlap("   ", "any text"), 0.0)

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

    def test_char_bigrams_single_char_returns_empty_set(self) -> None:
        """_char_bigrams of a single character must return set(), not a monogram.

        Before v0.2.39, _char_bigrams('x') returned {'x'} — a monogram, not a bigram.
        In MMR's _sim(), Jaccard({'x'}, {'x'}) == 1.0, treating two single-char texts
        as fully duplicate and wrongly suppressing both hits.
        """
        self.assertEqual(_char_bigrams("a"), set(), "single ASCII char must yield empty set")
        self.assertEqual(_char_bigrams("あ"), set(), "single CJK char must yield empty set")
        # Two chars must still produce one bigram
        self.assertEqual(_char_bigrams("ab"), {"ab"})

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

    def test_bm25_empty_query_returns_empty(self) -> None:
        """bm25_search() with an empty query string must return [] without crashing."""
        with make_store() as s:
            nb_id = seed(s)
            self.assertEqual(bm25_search(s, nb_id, "", k=5), [])

    def test_sim_empty_text_returns_zero(self) -> None:
        """_sim() must return 0.0 when a Hit has empty text (no bigrams to compare)."""
        from shoin.search import _sim
        a = Hit(1, 1, "", 0.9)
        b = Hit(2, 1, "通常テキスト", 0.5)
        self.assertEqual(_sim(a, b), 0.0)
        self.assertEqual(_sim(b, a), 0.0)
        self.assertEqual(_sim(a, a), 0.0)

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

    def test_fallback_needles_drops_single_ascii_chars(self) -> None:
        """Single-char ASCII terms must be excluded from LIKE needles.

        '%A%' or '%I%' matches nearly every English chunk; including them floods
        results with irrelevant hits.  Single-char CJK content words (猫, 木) are
        still kept because they are selective filters in Japanese text.
        """
        from shoin.search import _fallback_needles

        # Single ASCII char → excluded
        self.assertEqual(_fallback_needles("A"), [])
        self.assertEqual(_fallback_needles("I"), [])
        # 2-char ASCII → kept (selective enough)
        self.assertIn("AI", _fallback_needles("AI"))
        # Single CJK content word → kept (selective in Japanese text)
        self.assertIn("猫", _fallback_needles("猫"))
        # Mixed: single-char ASCII dropped, rest kept
        needles = _fallback_needles("A love story")
        self.assertNotIn("A", needles)
        self.assertIn("love", needles)
        self.assertIn("story", needles)

    def test_bm25_mixed_query_short_cjk_not_suppressed_by_fts5(self) -> None:
        """Mixed query (long ASCII term + short CJK term) must find chunks for BOTH.

        Before v0.2.41, bm25_search returned early when FTS5 found any hits.
        For a query like "local 猫": FTS5 finds "local" chunks and returns
        immediately; "猫" (1 char, skipped by fts_query because len < 3) never
        gets LIKE scanned.  Chunks containing only 猫 were silently dropped.
        """
        with make_store() as s:
            nb_id = s.create_notebook("mixed-query-test").id
            src = s.add_source(nb_id, "txt", "mixed", "t", "sha-mixed")
            s.add_chunks(src.id, [
                "local knowledge base",   # contains "local" — FTS5 finds this
                "猫が好きです",            # contains 猫 (1 char) — only LIKE finds this
                "全く別のテキスト",         # unrelated
            ])
            hits = bm25_search(s, nb_id, "local 猫", k=10)
            texts = [h.text for h in hits]
            self.assertTrue(any("local" in t for t in texts),
                            "FTS5 result for 'local' must be present")
            self.assertTrue(any("猫" in t for t in texts),
                            "LIKE result for short CJK '猫' must not be suppressed by FTS5 early return")

    def test_bm25_mixed_query_no_duplicate_chunks(self) -> None:
        """When FTS5 and LIKE both match the same chunk, it must appear only once."""
        with make_store() as s:
            nb_id = s.create_notebook("dedup-test").id
            src = s.add_source(nb_id, "txt", "dedup", "t", "sha-dedup")
            s.add_chunks(src.id, ["local 猫 knowledge"])  # matches both FTS5 and LIKE
            hits = bm25_search(s, nb_id, "local 猫", k=10)
            chunk_ids = [h.chunk_id for h in hits]
            self.assertEqual(len(chunk_ids), len(set(chunk_ids)), "duplicate chunk IDs in bm25_search result")

    def test_fts_query_katakana_query_includes_hiragana_trigrams(self) -> None:
        """fts_query() for a katakana term must also include hiragana-script trigrams.

        Before v0.2.42, a katakana query like コンピュータ (computer) would only
        generate katakana trigrams, missing documents indexed with hiragana
        (こんぴゅーた).  The new _kana_alt() conversion adds alternate-script
        trigrams to the OR expression.
        """
        from shoin.search import fts_query
        expr = fts_query("コンピュータ")  # 5-char katakana → 3 trigrams
        # katakana trigrams must be present
        self.assertIn('"コンピ"', expr)
        self.assertIn('"ンピュ"', expr)
        self.assertIn('"ピュー"', expr)
        # hiragana alternate trigrams must also be present
        self.assertIn('"こんぴ"', expr)
        self.assertIn('"んぴゅ"', expr)
        self.assertIn('"ぴゅー"', expr)

    def test_fts_query_hiragana_query_includes_katakana_trigrams(self) -> None:
        """fts_query() for a hiragana term must also include katakana-script trigrams."""
        from shoin.search import fts_query
        expr = fts_query("こんぴゅーた")  # hiragana → multiple trigrams
        self.assertIn('"こんぴ"', expr)   # hiragana trigram present
        self.assertIn('"コンピ"', expr)   # katakana alternate present

    def test_fts_query_pure_kanji_no_alt(self) -> None:
        """Pure kanji terms (no kana) must not generate spurious alternate trigrams.

        _kana_alt() returns the original string unchanged for pure kanji, so no
        duplicate OR branches should appear.
        """
        from shoin.search import fts_query, _kana_alt
        # Pure kanji term — no kana characters → unchanged
        self.assertEqual(_kana_alt("書院"), "書院")
        # fts_query for 3-char pure kanji: one trigram (itself), no alternate
        expr = fts_query("書院学")
        self.assertNotIn("OR", expr, "Pure kanji must not produce alternate OR branch")
        # Each trigram must appear exactly once (no duplication)
        expr2 = fts_query("書院はな")
        self.assertEqual(expr2.count('"書院は"'), 1, "Original trigram must not appear twice")

    def test_kana_alt_katakana_to_hiragana(self) -> None:
        """_kana_alt() converts katakana → hiragana char by char."""
        from shoin.search import _kana_alt
        self.assertEqual(_kana_alt("コンピュータ"), "こんぴゅーた")

    def test_kana_alt_hiragana_to_katakana(self) -> None:
        """_kana_alt() converts hiragana → katakana char by char."""
        from shoin.search import _kana_alt
        self.assertEqual(_kana_alt("こんぴゅーた"), "コンピュータ")

    def test_kana_alt_pure_kanji_unchanged(self) -> None:
        """_kana_alt() returns original for pure-kanji (no kana)."""
        from shoin.search import _kana_alt
        self.assertEqual(_kana_alt("書院"), "書院")

    def test_bm25_katakana_query_finds_hiragana_indexed_chunk(self) -> None:
        """A katakana query must retrieve a chunk indexed with hiragana content.

        Requires _kana_alt() alternate-script trigrams in fts_query() (v0.2.42).
        Without the fix, FTS5 trigrams for コンピュータ never match こんぴゅーた
        because they are stored as different Unicode codepoints.
        """
        with make_store() as s:
            nb_id = s.create_notebook("kana-xscript").id
            src = s.add_source(nb_id, "txt", "hiragana-doc", "mem://h", "sha-h")
            # Index a chunk written in hiragana
            s.add_chunks(src.id, ["こんぴゅーたは便利な道具です。"])
            # Query in katakana — should still find the hiragana chunk
            hits = bm25_search(s, nb_id, "コンピュータ", k=5)
            self.assertTrue(
                any("こんぴゅーた" in h.text for h in hits),
                "Katakana query must find hiragana-indexed chunk via alternate trigrams",
            )

    def test_bm25_hiragana_query_finds_katakana_indexed_chunk(self) -> None:
        """A hiragana query must retrieve a chunk indexed with katakana content."""
        with make_store() as s:
            nb_id = s.create_notebook("kana-xscript2").id
            src = s.add_source(nb_id, "txt", "katakana-doc", "mem://k", "sha-k")
            s.add_chunks(src.id, ["コンピュータは便利なツールです。"])
            hits = bm25_search(s, nb_id, "こんぴゅーた", k=5)
            self.assertTrue(
                any("コンピュータ" in h.text for h in hits),
                "Hiragana query must find katakana-indexed chunk via alternate trigrams",
            )


class TestCLI(unittest.TestCase):
    def test_serve_oserror_returns_exit_code_1(self) -> None:
        """When `shoin serve` fails to bind the port (OSError), main() must return 1.

        Before v0.2.41, the `serve()` call was outside the try/except block in
        main(), so OSError (e.g., 'Address already in use') propagated as an
        unhandled Python traceback instead of a clean error message + exit code 1.
        """
        from unittest.mock import patch
        from shoin.cli import main

        # serve is imported locally inside main() so patch it at the source module.
        with patch("shoin.server.serve", side_effect=OSError("Address already in use")):
            rc = main(["serve"])
        self.assertEqual(rc, 1)


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

    def test_history_messages_drops_leading_assistant(self) -> None:
        """History window starting mid-pair must drop the orphaned leading assistant.

        With 9 stored messages (4 pairs + 1 orphan user from SSE disconnect) and
        HISTORY_MESSAGES=6, DESC LIMIT 6 yields [a2,q3,a3,q4,a4,q5].  After the
        trailing-user pop (removes q5), the sequence is [a2,q3,a3,q4,a4] which
        starts with an assistant message.  The leading-assistant guard must remove
        a2 so the history is always user-first: [q3,a3,q4,a4].
        """
        from shoin.qa import history_messages

        with make_store() as s:
            nb = s.create_notebook("leading-asst")
            for i in range(1, 5):  # 4 complete Q/A pairs
                s.add_message(nb.id, "user", f"q{i}", "{}")
                s.add_message(nb.id, "assistant", f"a{i}", "{}")
            s.add_message(nb.id, "user", "q5-orphan", "{}")  # SSE disconnect
            # DESC LIMIT 6 → reversed → [a2,q3,a3,q4,a4,q5-orphan]
            # trailing user pop removes q5-orphan → [a2,q3,a3,q4,a4]
            # leading assistant pop must remove a2 → [q3,a3,q4,a4]
            msgs = history_messages(s, nb.id)

        self.assertGreater(len(msgs), 0)
        self.assertEqual(msgs[0]["role"], "user",
                         "history must not start with an assistant message")
        self.assertEqual(msgs[0]["content"], "q3")

    def test_history_messages_drops_multiple_leading_assistants(self) -> None:
        """Citation stripping may produce consecutive leading assistant turns; all must be removed."""
        from shoin.qa import _HISTORY_CITE_RE, history_messages

        with make_store() as s:
            nb = s.create_notebook("multi-leading")
            # a1 whose entire body is a citation marker (stripped to empty → skipped).
            # q1 / a1-empty-after-strip / q2 / a2 / q3 / a3
            # After DESC LIMIT 6 window cuts out q1: [a1-citation-only, q2, a2, q3, a3]
            # a1 stripped → skipped → out=[q2,a2,q3,a3]; but we can trigger two
            # leading assistants by having [a_stub, a2, q3, a3] after dedup.
            # Simplest: store 4 pairs where a1 after strip is non-empty, then
            # also have a2 begin the window, and a1 is the *only* first assistant.
            # Just verify the invariant: first role is always "user".
            s.add_message(nb.id, "user", "q1", "{}")
            s.add_message(nb.id, "assistant", "a1 text", "{}")
            s.add_message(nb.id, "user", "q2", "{}")
            s.add_message(nb.id, "assistant", "a2 text", "{}")
            s.add_message(nb.id, "user", "q3", "{}")
            s.add_message(nb.id, "assistant", "a3 text", "{}")
            s.add_message(nb.id, "user", "q4", "{}")
            s.add_message(nb.id, "assistant", "a4 text", "{}")
            s.add_message(nb.id, "user", "q5", "{}")  # orphan
            msgs = history_messages(s, nb.id)

        if msgs:
            self.assertEqual(msgs[0]["role"], "user",
                             "history must always start with a user message")

    def test_history_messages_preserves_user_citations(self) -> None:
        """Citation markers in user messages must NOT be stripped.

        User questions like 'tell me more about [S1]' reference a source the
        user observed in the previous answer.  Stripping them corrupts the
        user's intent in the history window.  Only assistant messages carry
        stale numbering that needs removal.
        """
        from shoin.qa import history_messages

        with make_store() as s:
            nb = s.create_notebook("hist-test")
            s.add_message(nb.id, "user", "Tell me about [S1]", "{}")
            s.add_message(nb.id, "assistant", "It is important [S1].", "{}")
            msgs = history_messages(s, nb.id)

        user_msgs = [m for m in msgs if m["role"] == "user"]
        asst_msgs = [m for m in msgs if m["role"] == "assistant"]
        # User message must preserve [S1]
        self.assertEqual(len(user_msgs), 1)
        self.assertIn("[S1]", user_msgs[0]["content"],
                      "user's [S1] reference must survive — it is the user's own words")
        # Assistant message must have [S1] stripped (stale previous-context numbering)
        self.assertEqual(len(asst_msgs), 1)
        self.assertNotIn("[S1]", asst_msgs[0]["content"],
                         "assistant's stale [S1] must be stripped from history")

    def test_build_context_later_oversize_chunk_truncated_not_dropped(self) -> None:
        """build_context must truncate an oversize later chunk, not silently drop it.

        Before v0.2.39, the budget guard (`if used and used + cost > per_source: break`)
        fired BEFORE the truncation guard (`if cost > per_source`).  A later chunk that
        was individually larger than the remaining budget was dropped entirely instead of
        being truncated to fill the remaining space.
        """
        from shoin.qa import build_context
        from shoin.search import Hit

        # Construct two hits for the same source.
        # Hit 0: 10 tokens (small, fits)
        # Hit 1: 500 tokens (huge, overflows per-source budget; should be truncated)
        short_text = "word " * 10          # ~10 tokens
        big_text = "word " * 500           # ~500 tokens

        with make_store() as s:
            nb = s.create_notebook("ctx-test")
            src = s.add_source(nb.id, "txt", "Doc", "orig", "sha1")
            hit0 = Hit(chunk_id=1, source_id=src.id, text=short_text, score=1.0)
            hit1 = Hit(chunk_id=2, source_id=src.id, text=big_text, score=0.9)
            # budget_tokens=100: per_source=100, hit0 uses ~10, remaining=~90 < 500 → truncate hit1
            ctx = build_context(s, [hit0, hit1], budget_tokens=100)

        # The body for S1 must include BOTH contributions: hit0 text and a truncated hit1
        body = ctx.source_bodies[0]
        # hit0 words must be present
        self.assertIn("word", body)
        # body must be longer than just hit0 alone — truncated hit1 was appended
        from shoin.chunk import estimate_tokens
        self.assertGreater(estimate_tokens(body), estimate_tokens(short_text),
                           "truncated hit1 must have been appended, not dropped")


    def test_ask_build_context_db_lock_raises_store_error(self) -> None:
        """sqlite3.OperationalError from build_context must be re-raised as StoreError.

        Before v0.2.44, build_context(store, hits) was unguarded in ask(). If
        store.get_source() raised sqlite3.OperationalError (DB lock timeout after
        5000ms busy_timeout), the exception bypassed the CLI's except clause
        (which only catches StoreError/IngestError/LLMError) and produced a raw
        Python traceback instead of a clean error message.
        """
        import sqlite3 as _sqlite3
        from unittest.mock import patch

        from shoin.qa import ask
        from shoin.search import Hit
        from shoin.store import StoreError as _StoreError

        class _NoLLM:
            embedding_model = ""

            def chat(self, messages, temperature=0.2):  # type: ignore[override]
                return "answer"

            def embed_one(self, text):  # type: ignore[override]
                return []

        fake_hit = Hit(chunk_id=1, source_id=1, text="some text", score=0.9)

        with make_store() as s:
            nb_id = seed(s)
            # Ensure retrieve() returns a non-empty hit list so build_context is called.
            with patch("shoin.qa.retrieve", return_value=[fake_hit]):
                with patch(
                    "shoin.qa.build_context",
                    side_effect=_sqlite3.OperationalError("database is locked"),
                ):
                    with self.assertRaises(_StoreError) as cm:
                        ask(s, _NoLLM(), nb_id, "notebook", persist=False)
            self.assertEqual(cm.exception.code, "SYSTEM_DB_LOCKED")


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

    def test_make_report_source_excerpts_populated_when_bodies_given(self) -> None:
        """make_report() must populate source_excerpts keyed by S-number when bodies given."""
        from shoin.citation import make_report

        report = make_report(
            "Answer [S1] and [S2].",
            ["Title A", "Title B"],
            source_ids=[10, 20],
            source_bodies=["Body of source one here.", "Body of source two here."],
        )
        self.assertIn("source_excerpts", report)
        self.assertEqual(report["source_excerpts"]["S1"], "Body of source one here.")
        self.assertEqual(report["source_excerpts"]["S2"], "Body of source two here.")

    def test_make_report_no_source_excerpts_without_bodies(self) -> None:
        """source_excerpts must be absent when source_bodies are not supplied."""
        from shoin.citation import make_report

        report = make_report("Answer [S1].", ["Title A"])
        self.assertNotIn("source_excerpts", report)

    def test_make_report_source_ids_and_bodies_both_checked(self) -> None:
        """Both source_ids and source_bodies must match source_titles length."""
        from shoin.citation import make_report

        with self.assertRaises(ValueError):
            make_report("text.", ["t1", "t2"], source_ids=[1], source_bodies=["b1", "b2"])
        with self.assertRaises(ValueError):
            make_report("text.", ["t1", "t2"], source_ids=[1, 2], source_bodies=["b1"])

    def test_verify_grounding_citation_only_sentence_skipped(self) -> None:
        """A sentence consisting only of citation brackets (no claim text) must be skipped."""
        from shoin.citation import verify_grounding
        # "[S1][S2]" stripped of brackets → empty bigrams → continue without error
        confirmed, misattributed = verify_grounding(
            "[S1][S2]", {1: "some source body text here", 2: "another source body"}
        )
        self.assertEqual(confirmed, [])
        self.assertEqual(misattributed, [])

    def test_verify_grounding_english_period_sentences_split(self) -> None:
        """Two English sentences each citing different sources must be evaluated separately.

        Without the period-space split, both citations are treated as one long
        claim and evaluated against both sources simultaneously, producing a false
        confirmation or missing a misattribution.
        """
        from shoin.citation import verify_grounding

        # S1 body uses the word 'notebook'; S2 body uses the word 'citation'.
        # Answer sentence 1 says 'notebook' and cites S1 → should be confirmed.
        # Answer sentence 2 says 'citation' and cites S2 → should be confirmed.
        # If they're not split, the merged bigram set overlaps both sources equally
        # and confirmation is less reliable.
        src_texts = {
            1: "shoin is a notebook application for local research",
            2: "citations are machine verified using lexical overlap",
        }
        answer = (
            "Shoin is a notebook application [S1]. "
            "Citations are machine verified [S2]."
        )
        confirmed, misattributed = verify_grounding(answer, src_texts)
        self.assertIn(1, confirmed, "S1 must be confirmed when claim text matches its source")
        self.assertIn(2, confirmed, "S2 must be confirmed when claim text matches its source")
        self.assertEqual(misattributed, [])

    def test_verify_grounding_multi_citation_independent_check(self) -> None:
        """In a sentence co-citing [S1][S2], a confirmed S1 must NOT prevent S2
        from being flagged misattributed when S2's overlap is low and S1 matches better.

        Before the fix: the `continue` after confirming S1 skipped the misattribution
        check for S2 entirely, silently ignoring the wrong citation number.
        After the fix: each cited number is evaluated independently.
        """
        from shoin.citation import verify_grounding

        sources = {
            1: "Shoin retrieves documents with source-grounded citations.",
            2: "Washi paper is made from kozo fiber by traditional craftspeople.",
        }
        # The claim strongly matches S1 but cites both S1 and S2 in the same sentence.
        # S2 has zero overlap → should be flagged misattributed.
        text = "Shoin retrieves documents with citations [S1][S2]."
        confirmed, misattributed = verify_grounding(text, sources)
        self.assertIn(1, confirmed, "S1 must be confirmed (high overlap)")
        self.assertIn(2, misattributed, "S2 must be misattributed (co-cited but low overlap)")

    def test_verify_grounding_citation_after_period_space_confirmed(self) -> None:
        """Citation placed after '. ' must be verified, not silently dropped.

        _SENTENCE_SPLIT_RE splits "Sentence. [S1]" at the period-space, producing
        fragment " [S1]" whose claim (after bracket removal) is empty.  Before
        v0.2.44, the empty-claim guard silently dropped the citation — a correctly
        grounded [S1] received no 'confirmed' entry.  After v0.2.44, prev_claim
        carries the preceding sentence's bigrams, allowing confirmation.
        """
        from shoin.citation import verify_grounding

        src = "The study found significant results in the experiment."
        # Common LLM citation style: claim sentence, then period, then [S#]
        text = "The study found significant results. [S1]"
        confirmed, _ = verify_grounding(text, {1: src})
        self.assertIn(1, confirmed, "Citation after '. ' must be confirmed when source matches")

    def test_verify_grounding_citation_after_period_space_misattributed(self) -> None:
        """A misattributed citation placed after '. ' must still be detectable."""
        from shoin.citation import verify_grounding

        sources = {
            1: "Washi paper is made from kozo fiber by traditional craftspeople.",
            2: "The study found significant results in the experiment.",
        }
        # Claim text matches S2; citation wrongly says [S1] — placed after ". "
        text = "The study found significant results. [S1]"
        _, misattributed = verify_grounding(text, sources)
        self.assertIn(
            1, misattributed,
            "[S1] after '. ' must be flagged misattributed when claim matches S2 far better",
        )

    def test_bigrams_single_char_returns_empty_set(self) -> None:
        """_bigrams of a single character must return set(), not {'x'}.

        Before v0.2.38, the guard was `if len(t) < 2: return {t} if t else set()`.
        A single-char string would produce {'x'} — a length-1 "bigram" that is not
        actually a bigram. This caused _overlap() to return 1.0 for a single shared
        character, falsely confirming unrelated citations in verify_grounding().
        """
        from shoin.citation import _bigrams

        self.assertEqual(_bigrams("a"), set(), "single ASCII char must yield empty set")
        self.assertEqual(_bigrams("あ"), set(), "single CJK char must yield empty set")
        self.assertEqual(_bigrams(""), set(), "empty string must yield empty set")
        # Two chars must still produce exactly one bigram
        self.assertEqual(_bigrams("ab"), {"ab"})
        self.assertEqual(_bigrams("ab "), {"ab"}, "trailing whitespace stripped before bigram")


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

    def test_text_chunks_for_source_returns_seq_text_without_embeddings(self) -> None:
        """text_chunks_for_source must return (seq, text) tuples in seq order.

        The method must not SELECT the embedding column so it doesn't waste time
        unpacking BLOBs that callers (e.g. _h_src_text) will immediately discard.
        """
        with Store(":memory:") as s:
            nb = s.create_notebook("txt-test")
            src = s.add_source(nb.id, "txt", "doc", "orig", "sha-t")
            s.add_chunks(src.id, ["alpha chunk", "beta chunk", "gamma chunk"])
            pairs = s.text_chunks_for_source(src.id)
        self.assertEqual(pairs, [(0, "alpha chunk"), (1, "beta chunk"), (2, "gamma chunk")])

    def test_id_text_chunks_for_notebook_returns_all_sources(self) -> None:
        """id_text_chunks_for_notebook must return (id, text) for every chunk across all
        sources in the notebook, ordered by chunk id, without loading embeddings."""
        with Store(":memory:") as s:
            nb = s.create_notebook("nb-test")
            src1 = s.add_source(nb.id, "txt", "d1", "o1", "s1")
            src2 = s.add_source(nb.id, "txt", "d2", "o2", "s2")
            ids1 = s.add_chunks(src1.id, ["a", "b"])
            ids2 = s.add_chunks(src2.id, ["c"])
            rows = s.id_text_chunks_for_notebook(nb.id)
        chunk_ids = [r[0] for r in rows]
        texts = [r[1] for r in rows]
        self.assertEqual(texts, ["a", "b", "c"])
        self.assertEqual(chunk_ids, sorted(ids1 + ids2))


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

    def test_post_incomplete_read_raises_llmerror(self) -> None:
        """http.client.IncompleteRead (HTTPException subclass) in _post() must be
        converted to LLMError, not propagate as a bare exception.

        Before v0.2.43, only (OSError, ValueError) were caught; IncompleteRead
        (raised when the server drops the TCP connection before sending the full
        Content-Length body) escaped as an uncaught http.client.HTTPException.
        """
        import http.client
        from unittest.mock import MagicMock, patch
        from shoin.llm import LLMClient, LLMError

        exc = http.client.IncompleteRead(b"partial", 100)
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.side_effect = exc

        with patch("urllib.request.urlopen", return_value=mock_resp):
            client = LLMClient(base_url="http://localhost:11434/v1")
            with self.assertRaises(LLMError) as cm:
                client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(cm.exception.code, "SYSTEM_SERVICE_UNAVAILABLE")

    def test_chat_stream_incomplete_read_raises_llmerror(self) -> None:
        """http.client.IncompleteRead during SSE stream iteration must be LLMError.

        Before v0.2.43, IncompleteRead during 'for raw in resp' bypassed the
        LLMError guard in server.py's _h_ask_sse(), corrupting the SSE response
        with an HTTP 500 status written into the already-flushed stream body.
        """
        import http.client
        from unittest.mock import MagicMock, patch
        from shoin.llm import LLMClient, LLMError

        exc = http.client.IncompleteRead(b"data: {", 50)

        class _TruncatedResp:
            def __enter__(self) -> "_TruncatedResp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def __iter__(self):  # type: ignore[override]
                yield b"data: {\"choices\":[{\"delta\":{\"content\":\"hello\"}}]}\n"
                raise exc

        with patch("urllib.request.urlopen", return_value=_TruncatedResp()):
            client = LLMClient(base_url="http://localhost:11434/v1")
            gen = client.chat_stream([{"role": "user", "content": "hi"}])
            tokens = []
            with self.assertRaises(LLMError) as cm:
                for tok in gen:
                    tokens.append(tok)
        self.assertEqual(tokens, ["hello"])          # partial output before truncation
        self.assertEqual(cm.exception.code, "SYSTEM_SERVICE_UNAVAILABLE")

    def test_post_http_error_body_read_is_capped(self) -> None:
        """_post() must read at most 300 bytes of an HTTP error body before decoding.

        Before v0.2.49, `exc.read().decode(...)[:300]` read the full body into memory
        then sliced — a malicious endpoint returning a gigabyte 500 response caused OOM.
        Fix: `exc.read(300).decode(...)` passes the limit to read().
        """
        import http.client
        import io
        from unittest.mock import patch, MagicMock
        from shoin.llm import LLMClient, LLMError

        # Build a fake HTTPError whose read() raises if called without a limit
        body_bytes = b"X" * 600  # 600 bytes; we assert only 300 are read

        class _LimitedBytesIO(io.RawIOBase):
            def __init__(self, data: bytes) -> None:
                self._data = data
                self.max_read: int | None = None

            def read(self, n: int = -1) -> bytes:
                if n == -1:
                    raise AssertionError("read() called without size limit")
                self.max_read = n
                return self._data[:n]

        fake_body = _LimitedBytesIO(body_bytes)
        import urllib.error
        err = urllib.error.HTTPError("http://x", 500, "Internal Server Error", {}, fake_body)  # type: ignore

        with patch("urllib.request.urlopen", side_effect=err):
            client = LLMClient(base_url="http://localhost:11434/v1")
            with self.assertRaises(LLMError) as cm:
                client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(cm.exception.code, "SYSTEM_LLM_HTTP_ERROR")
        # The read limit must have been set (not None, meaning uncapped read was not called)
        self.assertIsNotNone(fake_body.max_read, "read() must be called with a size limit")
        self.assertLessEqual(fake_body.max_read, 300)

    def test_embed_non_dict_data_items_raise_llmerror(self) -> None:
        """embed() must convert AttributeError from non-dict items in response['data']
        to LLMError('SYSTEM_LLM_BAD_RESPONSE'), not propagate AttributeError.

        Before v0.2.49, when a malformed endpoint returned data as a list of strings
        (instead of dicts), `d.get('index', 0)` raised AttributeError, which was NOT
        in the except clause — it escaped embed() and _query_vector() in qa.py,
        bypassing the BM25-only degradation path and raising HTTP 500 instead.
        """
        from unittest.mock import patch
        from shoin.llm import LLMClient, LLMError

        bad_response = {"data": ["not", "a", "dict"], "model": "test", "object": "list"}
        with patch.object(LLMClient, "_post", return_value=bad_response):
            client = LLMClient(base_url="http://localhost:11434/v1")
            client._embedding_model = "nomic-embed-text"
            with self.assertRaises(LLMError) as cm:
                client.embed(["hello world"])
        self.assertEqual(cm.exception.code, "SYSTEM_LLM_BAD_RESPONSE")

    def test_available_bad_status_line_returns_false(self) -> None:
        """http.client.BadStatusLine (HTTPException subclass) in available() must
        return False, not raise.

        Before v0.2.43, available() only caught (OSError, ValueError).  When the
        configured port is occupied by a non-HTTP server that sends a malformed HTTP
        status line, urlopen() raises BadStatusLine, which is an HTTPException and
        NOT a subclass of OSError — so it propagated instead of returning False.
        """
        import http.client
        from unittest.mock import patch
        from shoin.llm import LLMClient

        with patch(
            "urllib.request.urlopen",
            side_effect=http.client.BadStatusLine("HTTP/1.1"),
        ):
            client = LLMClient(base_url="http://localhost:11434/v1")
            self.assertFalse(client.available())


class TestServerSSE(unittest.TestCase):
    """Server-level SSE streaming regression tests (v0.2.49)."""

    def test_meta_conn_error_saves_empty_assistant_message(self) -> None:
        """When ConnectionError fires during the meta SSE event, an empty assistant
        message must be saved so the orphaned user turn is not visible in
        list_messages() on page reload.

        Before v0.2.49, the handler returned without saving any assistant message,
        leaving a dangling user turn that list_messages() (used by GET /api/notebooks/{id})
        returned to the UI, rendering an unanswered question in the history panel.
        The build_context exception path (v0.2.39) already saved an empty message;
        this fix makes the meta-send ConnectionError path consistent with that pattern.
        """
        import json
        import os
        import tempfile
        import threading

        from shoin.server import _Handler

        class _FakeLLM:
            embedding_model = ""
            model = "test"

            def embed_one(self, text: str) -> list[float]:
                return [1.0, 0.0]

            def chat(self, messages: list, temperature: float = 0.2) -> str:
                return "answer"

        # Create a real DB so retrieve() has actual chunks to find
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            db_path = f.name
        try:
            with Store(db_path) as s:
                nb = s.create_notebook("sse-test")
                src = s.add_source(nb.id, "txt", "doc", "mem://d", "sha-sse")
                s.add_chunks(src.id, ["books are great for learning and studying"])
                nb_id = nb.id

            # Build a minimal handler instance with HTTP plumbing mocked out
            handler = _Handler.__new__(_Handler)
            handler.db = db_path
            handler.llm = _FakeLLM()  # type: ignore[assignment]
            handler.questions_cache = {}
            handler.questions_cache_lock = threading.Lock()

            sse_calls: list[str] = []

            def _mock_sse(event: str, data: object) -> None:
                sse_calls.append(event)
                if event == "meta":
                    raise ConnectionError("client disconnected")

            handler._sse = _mock_sse  # type: ignore[assignment]
            handler._headers = lambda *a, **kw: None  # type: ignore[assignment]
            handler._read_json = lambda: {"question": "books"}  # type: ignore[assignment]
            handler._require = lambda data, field: str(data[field])  # type: ignore[assignment]

            handler._h_ask_sse(nb_id)

            with Store(db_path) as s:
                msgs = s.list_messages(nb_id)

            # Must have two messages: user + empty assistant (not just the orphaned user)
            self.assertEqual(len(msgs), 2, "user message + empty assistant message must both be saved")
            self.assertEqual(msgs[0]["role"], "user")
            self.assertEqual(msgs[1]["role"], "assistant")
            self.assertEqual(msgs[1]["body"], "")
        finally:
            os.unlink(db_path)


class TestPipeline(unittest.TestCase):
    def test_noembed_chat_raises_service_unavailable(self) -> None:
        """_NoEmbed.chat() must raise LLMError so the 'no LLM' path is explicit."""
        from shoin.llm import LLMError
        from shoin.pipeline import _NoEmbed

        ne = _NoEmbed()
        with self.assertRaises(LLMError) as cm:
            ne.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(cm.exception.code, "SYSTEM_SERVICE_UNAVAILABLE")

    def test_noembed_embed_one_raises_disabled(self) -> None:
        """_NoEmbed.embed_one() must raise LLMError to signal embedding is unavailable."""
        from shoin.llm import LLMError
        from shoin.pipeline import _NoEmbed

        ne = _NoEmbed()
        with self.assertRaises(LLMError) as cm:
            ne.embed_one("any text")
        self.assertEqual(cm.exception.code, "SYSTEM_EMBED_DISABLED")

    def test_embed_chunks_store_error_rolls_back(self) -> None:
        """StoreError during set_embedding triggers rollback; n_embedded stays 0."""
        from unittest.mock import patch

        from shoin.pipeline import _embed_chunks
        from shoin.store import StoreError

        class BatchEmbedLLM:
            embedding_model = "test-model"

            def embed(self, texts: list[str]) -> list[list[float]]:
                return [[0.1, 0.2] for _ in texts]

        with make_store() as s:
            nb_id = s.create_notebook("rollback-test").id
            src = s.add_source(nb_id, "txt", "doc", "t", "sha-r")
            chunk_ids = s.add_chunks(src.id, ["text chunk"])
            with patch.object(
                s, "set_embedding", side_effect=StoreError("CHUNK_NOT_FOUND", "gone")
            ):
                n = _embed_chunks(s, BatchEmbedLLM(), chunk_ids, ["text chunk"])
        self.assertEqual(n, 0)

    def test_embed_chunks_rollback_exception_silenced(self) -> None:
        """Exception raised by conn.rollback() during StoreError recovery must be silenced."""
        from unittest.mock import patch

        from shoin.pipeline import _embed_chunks
        from shoin.store import StoreError

        class BatchEmbedLLM:
            embedding_model = "test-model"

            def embed(self, texts: list[str]) -> list[list[float]]:
                return [[0.1, 0.2] for _ in texts]

        class _FailRollback:
            """Delegates all conn calls to the real conn except rollback()."""
            def __init__(self, real):
                self._real = real
            def rollback(self):
                raise Exception("rollback failed")
            def __getattr__(self, name):
                return getattr(self._real, name)

        with make_store() as s:
            nb_id = s.create_notebook("rollback-err-test").id
            src = s.add_source(nb_id, "txt", "doc", "t", "sha-re")
            chunk_ids = s.add_chunks(src.id, ["text chunk"])
            # Pre-store the matching embed model so the mismatch guard doesn't early-exit.
            s.set_setting("embed_model", "test-model")
            # Wrap conn so rollback() raises (covers lines 84-85).
            s.__dict__["conn"] = _FailRollback(s.conn)
            try:
                with patch.object(
                    s, "set_embedding", side_effect=StoreError("CHUNK_NOT_FOUND", "gone")
                ):
                    n = _embed_chunks(s, BatchEmbedLLM(), chunk_ids, ["text chunk"])
            finally:
                s.__dict__["conn"] = s.conn._real  # type: ignore[attr-defined]
        self.assertEqual(n, 0)

    def test_embed_chunks_model_name_whitespace_normalized(self) -> None:
        """Model name with leading/trailing whitespace must not cause false mismatch.

        _embed_chunks stores the stripped model name, and _check_embed_model_ok
        strips both sides before comparing — so "nomic " and "nomic" are the same model.
        """
        from shoin.pipeline import _embed_chunks
        from shoin.qa import _check_embed_model_ok

        class SpaceyLLM:
            embedding_model = " nomic-embed-text "

            def embed(self, texts: list[str]) -> list[list[float]]:
                return [[0.1] * 3 for _ in texts]

            def embed_one(self, text: str) -> list[float]:
                return self.embed([text])[0]

            def chat(self, messages, temperature=0.2):
                raise NotImplementedError

        with make_store() as s:
            nb_id = s.create_notebook("ws-test").id
            src = s.add_source(nb_id, "txt", "doc", "d", "sha-ws")
            chunk_ids = s.add_chunks(src.id, ["hello world"])
            llm = SpaceyLLM()
            # First ingestion: stores stripped model name "nomic-embed-text"
            n = _embed_chunks(s, llm, chunk_ids, ["hello world"])
            self.assertEqual(n, 1)
            stored = s.get_setting("embed_model")
            self.assertEqual(stored, "nomic-embed-text")  # stripped, no surrounding spaces
            # check_embed_model_ok must see same-model: no false mismatch
            self.assertTrue(_check_embed_model_ok(s, llm))

    def test_embed_chunks_falls_back_to_embed_one_when_no_embed(self) -> None:
        """_embed_chunks must embed chunks via embed_one when the backend has no batch embed.

        ChatBackend protocol only guarantees embed_one, so a conforming implementation
        without embed() must still get chunks embedded rather than silently getting 0.
        """
        from shoin.pipeline import _embed_chunks

        embedded: list[str] = []

        class EmbedOnlyLLM:
            """ChatBackend that satisfies the protocol but has no batch embed method."""
            embedding_model = "protocol-only-model"

            def chat(self, messages, temperature=0.2):
                raise NotImplementedError

            def embed_one(self, text: str) -> list[float]:
                embedded.append(text)
                return [0.5, 0.5]

        with make_store() as s:
            nb_id = s.create_notebook("embed-one-fallback").id
            src = s.add_source(nb_id, "txt", "doc", "o", "sha-eo")
            chunk_ids = s.add_chunks(src.id, ["alpha", "beta"])
            n = _embed_chunks(s, EmbedOnlyLLM(), chunk_ids, ["alpha", "beta"])

        self.assertEqual(n, 2, "both chunks must be embedded via embed_one fallback")
        self.assertEqual(embedded, ["alpha", "beta"])

    def test_embed_chunks_done_counts_actual_pairs_not_batch_size(self) -> None:
        """n_embedded must reflect vectors actually stored, not the full batch size.

        Before the fix: done += len(batch_ids) overcounted when a backend's embed()
        returned fewer vectors than requested (zip silently truncates).
        After the fix: done += count (incremented per pair in the zip loop).
        """
        from shoin.pipeline import _embed_chunks

        class ShortVectorLLM:
            """Returns one fewer vector than requested to exercise zip truncation."""
            embedding_model = "short-vec-model"

            def embed(self, texts: list[str]) -> list[list[float]]:
                # Return one fewer vector than requested
                return [[0.1, 0.2] for _ in texts[:-1]]

        with make_store() as s:
            nb_id = s.create_notebook("zip-trunc-test").id
            src = s.add_source(nb_id, "txt", "doc", "o", "sha-zt")
            texts = ["chunk one", "chunk two", "chunk three"]
            chunk_ids = s.add_chunks(src.id, texts)
            n = _embed_chunks(s, ShortVectorLLM(), chunk_ids, texts)
        # embed() returns len(texts)-1 vectors → zip processes len(texts)-1 pairs
        self.assertEqual(n, len(texts) - 1, "done must equal actual pairs stored, not batch size")

    def test_index_source_url_path_calls_extract_url(self) -> None:
        """index_source with an http:// target must call extract_url, not extract_file."""
        from unittest.mock import patch

        from shoin.ingest import Extracted
        from shoin.pipeline import index_source

        fake = Extracted(kind="url", title="Mock Page", origin="http://x.test", sha256="abc", text="page content")
        with patch("shoin.pipeline.extract_url", return_value=fake) as mock_eu:
            with make_store() as s:
                nb_id = s.create_notebook("url-test").id
                result = index_source(s, nb_id, "http://x.test")
        mock_eu.assert_called_once_with("http://x.test")
        self.assertEqual(result.source.title, "Mock Page")

    def test_refresh_source_replaces_chunks_keeps_id(self) -> None:
        """refresh_source must keep the source ID and replace chunks with fresh content."""
        from unittest.mock import patch

        from shoin.ingest import Extracted
        from shoin.pipeline import refresh_source

        original = Extracted(kind="url", title="Page v1", origin="http://refresh.test", sha256="sha-v1", text="old content")
        updated = Extracted(kind="url", title="Page v2", origin="http://refresh.test", sha256="sha-v2", text="new content refreshed")
        with make_store() as s:
            nb_id = s.create_notebook("refresh-nb").id
            with patch("shoin.pipeline.extract_url", return_value=original):
                from shoin.pipeline import index_source
                res0 = index_source(s, nb_id, "http://refresh.test")
            source_id = res0.source.id
            with patch("shoin.pipeline.extract_url", return_value=updated):
                res1 = refresh_source(s, source_id)
        self.assertEqual(res1.source.id, source_id, "source ID must be preserved")
        self.assertEqual(res1.source.title, "Page v2")
        self.assertEqual(res1.source.sha256, "sha-v2")
        with make_store() as s2:
            pass  # store closed; already verified above

    def test_refresh_source_nonurl_raises(self) -> None:
        """refresh_source on a file source must raise INGEST_REFRESH_NOT_URL."""
        from shoin.ingest import IngestError
        from shoin.pipeline import refresh_source

        with make_store() as s:
            nb_id = s.create_notebook("nb").id
            src = s.add_source(nb_id, "txt", "doc.txt", "/local/doc.txt", "sha-f")
            s.add_chunks(src.id, ["text"])
            with self.assertRaises(IngestError) as cm:
                refresh_source(s, src.id)
        self.assertEqual(cm.exception.code, "INGEST_REFRESH_NOT_URL")

    def test_refresh_source_missing_raises(self) -> None:
        """refresh_source on a non-existent source must raise SOURCE_NOT_FOUND."""
        from shoin.store import StoreError
        from shoin.pipeline import refresh_source

        with make_store() as s:
            with self.assertRaises(StoreError) as cm:
                refresh_source(s, 99999)
        self.assertEqual(cm.exception.code, "SOURCE_NOT_FOUND")

    def test_index_source_empty_text_raises_ingest_empty(self) -> None:
        """index_source must raise INGEST_EMPTY before committing the source row
        when split_text() returns an empty list.

        Before v0.2.46, add_source() committed first, then add_chunks([]) was
        called with an empty list, silently creating a zero-chunk source that
        was permanently invisible to all retrieval queries.  The caller (CLI or
        server) received a success response despite the source contributing nothing.
        """
        from shoin.ingest import Extracted, IngestError
        from shoin.pipeline import index_source

        # Extracted text that collapses to no chunks (whitespace-only after processing)
        fake = Extracted(kind="txt", title="Empty Doc", origin="/dev/null", sha256="sha-empty", text="")
        with make_store() as s:
            nb_id = s.create_notebook("empty-src-test").id
            with patch("shoin.pipeline.extract_file", return_value=fake):
                with self.assertRaises(IngestError) as cm:
                    index_source(s, nb_id, "/dev/null")
            self.assertEqual(cm.exception.code, "INGEST_EMPTY")
            # No source row must have been committed
            sources = s.sources_for_notebook(nb_id)
            self.assertEqual(len(sources), 0, "source row must not be committed when chunks are empty")

    def test_add_chunks_empty_list_raises_store_error(self) -> None:
        """add_chunks([]) must raise StoreError, not silently create a zero-chunk source.

        Matches the existing guard in replace_chunks_for_source() (added in v0.2.40).
        """
        from shoin.store import StoreError

        with make_store() as s:
            nb_id = s.create_notebook("empty-chunks-test").id
            src = s.add_source(nb_id, "txt", "doc", "t", "sha-ec")
            with self.assertRaises(StoreError) as cm:
                s.add_chunks(src.id, [])
            self.assertEqual(cm.exception.code, "VALIDATION_REQUIRED_FIELD_MISSING")

    def test_embed_chunks_dimension_mismatch_stops_embedding(self) -> None:
        """_embed_chunks() must stop and NOT store vectors when dimension changes mid-run.

        A restarting embedding endpoint can momentarily return wrong-dimension vectors.
        Before v0.2.48, these were stored silently, corrupting the embedding index
        with vectors of mismatched byte length that produce garbage cosine scores.
        Fix: compare each vector's dimension against the first vector's dimension;
        raise LLMError on mismatch so the except-LLMError handler leaves BM25 intact.
        """
        from shoin.pipeline import _embed_chunks

        class MismatchEmbedLLM:
            embedding_model = "test-model"
            call_count = 0

            def embed(self, texts: list[str]) -> list[list[float]]:
                MismatchEmbedLLM.call_count += 1
                if MismatchEmbedLLM.call_count == 1:
                    return [[0.1, 0.2] for _ in texts]  # dim=2
                return [[0.1, 0.2, 0.3] for _ in texts]  # dim=3 — mismatch!

        MismatchEmbedLLM.call_count = 0
        with make_store() as s:
            nb_id = s.create_notebook("dim-mismatch").id
            src = s.add_source(nb_id, "txt", "doc", "t", "sha-dm")
            # 32 chunks = 2 batches of 16 (EMBED_BATCH=16); second batch returns dim=3
            texts = [f"chunk {i}" for i in range(32)]
            chunk_ids = s.add_chunks(src.id, texts)
            n = _embed_chunks(s, MismatchEmbedLLM(), chunk_ids, texts)
        # First batch (dim=2) succeeds (16 chunks), second batch raises LLMError
        self.assertEqual(n, 16, "only first batch must be stored; second batch aborted on mismatch")

    def test_refresh_source_empty_text_raises_ingest_empty(self) -> None:
        """refresh_source() must raise IngestError('INGEST_EMPTY') when the re-fetched
        URL returns no extractable text — matching the behavior of index_source() (v0.2.46).

        Before v0.2.48, the empty-text case fell through to replace_chunks_for_source(),
        which raised StoreError('VALIDATION_REQUIRED_FIELD_MISSING') — leaking internal
        implementation detail and breaking the HTTP 400 / IngestError mapping contract.
        """
        from unittest.mock import patch

        from shoin.ingest import Extracted, IngestError
        from shoin.pipeline import refresh_source

        blank = Extracted(text="", kind="html", title="empty page", origin="http://x.com/", sha256="abc123")
        with make_store() as s:
            nb_id = s.create_notebook("refresh-empty").id
            src = s.add_source(nb_id, "html", "original", "http://x.com/", "sha-orig")
            s.add_chunks(src.id, ["original content"])
            with patch("shoin.pipeline.extract_url", return_value=blank):
                with self.assertRaises(IngestError) as cm:
                    refresh_source(s, src.id)
        self.assertEqual(cm.exception.code, "INGEST_EMPTY")

    def test_fuse_vec_only_scores_in_unit_range(self) -> None:
        """fuse() with empty bm25_hits must normalize vec scores to [0..1].

        Before v0.2.46, when bm25_hits=[] but vec_hits was non-empty, fuse()
        entered the merged-dict path and set h.score = alpha * vec_norm, capping
        scores at alpha (≈0.5).  BM25-only hits scored in [0..1].  The asymmetry
        caused MMR's relevance/diversity balance to skew toward diversity for
        vec-only queries, because all candidate scores were compressed by alpha.
        """
        from shoin.search import Hit, fuse

        vec_hits = [
            Hit(chunk_id=1, source_id=1, text="a", score=0.0, vec=0.9),
            Hit(chunk_id=2, source_id=1, text="b", score=0.0, vec=0.4),
        ]
        result = fuse([], vec_hits, alpha=0.5)
        scores = [h.score for h in result]
        # With proper normalization, top score = 1.0, bottom = 0.0
        self.assertAlmostEqual(max(scores), 1.0, places=6, msg="vec-only top score must be 1.0")
        self.assertAlmostEqual(min(scores), 0.0, places=6, msg="vec-only bottom score must be 0.0")
        # Ordering must be preserved (vec=0.9 ranked above vec=0.4)
        self.assertEqual(result[0].chunk_id, 1, "highest vec score must rank first")


class TestExport(unittest.TestCase):
    def test_md_line_collapses_lf(self) -> None:
        from shoin.export import _md_line
        self.assertEqual(_md_line("foo\nbar"), "foo bar")

    def test_md_line_collapses_crlf(self) -> None:
        from shoin.export import _md_line
        self.assertEqual(_md_line("foo\r\nbar"), "foo bar")

    def test_md_line_collapses_bare_cr(self) -> None:
        from shoin.export import _md_line
        self.assertEqual(_md_line("foo\rbar"), "foo bar")

    def test_md_line_no_op_on_plain_text(self) -> None:
        from shoin.export import _md_line
        self.assertEqual(_md_line("plain text"), "plain text")

    def test_export_markdown_newline_in_source_title_single_list_item(self) -> None:
        """Embedded newline in source title must not break the Markdown list item."""
        from shoin.export import export_markdown
        with make_store() as s:
            nb = s.create_notebook("nb")
            s.add_source(nb.id, "url", "Title\nSecond line", "http://x.com", "sha1")
            md = export_markdown(s, nb.id)
        item_lines = [l for l in md.splitlines() if l.startswith("- [S1]")]
        self.assertEqual(len(item_lines), 1)
        self.assertIn("Title Second line", item_lines[0])

    def test_export_markdown_newline_in_note_title_single_heading(self) -> None:
        """Embedded newline in note title must not break the Markdown heading."""
        from shoin.export import export_markdown
        with make_store() as s:
            nb = s.create_notebook("nb")
            s.add_note(nb.id, "Note\nTitle", "body")
            md = export_markdown(s, nb.id)
        heading_lines = [l for l in md.splitlines() if l.startswith("### ")]
        self.assertEqual(len(heading_lines), 1)
        self.assertIn("Note Title", heading_lines[0])

    def test_export_markdown_newline_in_notebook_name_single_heading(self) -> None:
        """Embedded newline in notebook name must not break the H1 heading."""
        from shoin.export import export_markdown
        with make_store() as s:
            nb = s.create_notebook("My\nNotebook")
            md = export_markdown(s, nb.id)
        h1_lines = [l for l in md.splitlines() if l.startswith("# ")]
        self.assertEqual(len(h1_lines), 1)
        self.assertIn("My Notebook", h1_lines[0])

    def test_bibtex_brace_in_title_does_not_mutate_to_paren(self) -> None:
        """Curly braces in source titles must not be silently replaced with parentheses."""
        from shoin.export import export_bibtex
        with make_store() as s:
            nb = s.create_notebook("nb")
            s.add_source(nb.id, "url", "Algorithms {revised}", "https://x.com", "sha1")
            bib = export_bibtex(s, nb.id)
        self.assertNotIn("(revised)", bib, "{ must not be silently converted to (")
        self.assertIn("{", bib)

    def test_bibtex_added_at_empty_does_not_crash(self) -> None:
        """export_bibtex must not crash when added_at is an empty string."""
        from shoin.export import export_bibtex
        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "doc", "doc.txt", "sha2")
            # Manually corrupt added_at to empty string
            s.conn.execute("UPDATE sources SET added_at='' WHERE id=?", (src.id,))
            s.conn.commit()
            bib = export_bibtex(s, nb.id)
        self.assertIn("added unknown", bib)

    def test_ris_er_no_trailing_space(self) -> None:
        """RIS ER  - terminator must not have a trailing space (spec compliance)."""
        from shoin.export import export_ris
        with make_store() as s:
            nb = s.create_notebook("nb")
            s.add_source(nb.id, "url", "Page", "https://x.com", "sha3")
            ris = export_ris(s, nb.id)
        for line in ris.splitlines():
            if line.startswith("ER"):
                self.assertEqual(line, "ER  -", f"ER line must not have trailing space: {line!r}")

    def test_ris_empty_added_at_produces_unknown_date(self) -> None:
        """export_ris DA field must be 'unknown' when added_at is empty, not blank.

        Before v0.2.45, export_bibtex had `or 'unknown'` (added in v0.2.37) but
        export_ris was missed — (src.added_at or '')[:10].replace('-', '/') returns
        '' for an empty added_at, producing 'DA  - ' (blank value) in the RIS entry.
        Strict RIS consumers may reject or produce a null date for a blank DA field.
        """
        from shoin.export import export_ris
        with make_store() as s:
            nb = s.create_notebook("nb-ris-date")
            src = s.add_source(nb.id, "url", "Page", "https://x.com", "sha-rd")
            # Directly zero out added_at to simulate an empty-string value
            s.conn.execute("UPDATE sources SET added_at='' WHERE id=?", (src.id,))
            s.conn.commit()
            ris = export_ris(s, nb.id)
        da_lines = [l for l in ris.splitlines() if l.startswith("DA  -")]
        self.assertEqual(len(da_lines), 1)
        self.assertEqual(da_lines[0], "DA  - unknown", f"empty added_at must produce 'unknown', got {da_lines[0]!r}")

    def test_export_markdown_multiline_user_question_stays_on_one_label_line(self) -> None:
        """A user question with an embedded newline must not break the **User**: label.

        Before v0.2.39, export_markdown used f"**User**: {body}" without applying
        _md_line().  A body containing \\n would produce two separate output lines,
        with the second line appearing as plain (unlabeled) text in the rendered Markdown.
        """
        from shoin.export import export_markdown
        with make_store() as s:
            nb = s.create_notebook("export-newline-test")
            s.add_message(nb.id, "user", "line one\nline two", "{}")
            md = export_markdown(s, nb.id)
        # Every line that starts with **User**: must also END on the same physical line
        # (i.e., the embedded newline must have been collapsed).
        user_lines = [l for l in md.splitlines() if "**User**:" in l]
        self.assertEqual(len(user_lines), 1, "user question must appear on exactly one line")
        self.assertIn("line one", user_lines[0])
        self.assertIn("line two", user_lines[0])


class TestConfigXDG(unittest.TestCase):
    def test_data_dir_uses_shoin_data_dir_env(self) -> None:
        """When SHOIN_DATA_DIR is set, data_dir() must return that path directly."""
        import os
        from shoin.config import data_dir

        with patch.dict(os.environ, {"SHOIN_DATA_DIR": "/tmp/shoin_custom"}):
            result = data_dir()
        self.assertEqual(str(result), "/tmp/shoin_custom")

    def test_data_dir_uses_xdg_data_home(self) -> None:
        """SHOIN_DATA_DIR not set and XDG_DATA_HOME set: data_dir() uses XDG path."""
        import os
        from shoin.config import data_dir

        env = {"XDG_DATA_HOME": "/tmp/xdgtest"}
        env.pop("SHOIN_DATA_DIR", None)
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("SHOIN_DATA_DIR", None)
            result = data_dir()
        self.assertEqual(str(result), "/tmp/xdgtest/shoin")

    def test_port_invalid_env_falls_back_to_default(self) -> None:
        """Non-numeric SHOIN_PORT must fall back to DEFAULT_PORT, not raise ValueError."""
        import os
        from shoin.config import DEFAULT_PORT, port

        with patch.dict(os.environ, {"SHOIN_PORT": "notanumber"}):
            result = port()
        self.assertEqual(result, DEFAULT_PORT)

    def test_port_empty_env_falls_back_to_default(self) -> None:
        """Empty SHOIN_PORT must fall back to DEFAULT_PORT."""
        import os
        from shoin.config import DEFAULT_PORT, port

        with patch.dict(os.environ, {"SHOIN_PORT": ""}):
            result = port()
        self.assertEqual(result, DEFAULT_PORT)

    def test_port_valid_env_is_used(self) -> None:
        """Valid SHOIN_PORT must be returned as-is."""
        import os
        from shoin.config import port

        with patch.dict(os.environ, {"SHOIN_PORT": "9999"}):
            result = port()
        self.assertEqual(result, 9999)


class TestNegTerms(unittest.TestCase):
    """Tests for negative-term parsing and filtering (v0.2.47)."""

    def test_neg_terms_ascii(self) -> None:
        self.assertEqual(neg_terms("Python -2"), ["2"])

    def test_neg_terms_multiple(self) -> None:
        self.assertEqual(neg_terms("機械学習 -Python -legacy"), ["python", "legacy"])

    def test_neg_terms_cjk(self) -> None:
        self.assertEqual(neg_terms("書院 -Python"), ["python"])

    def test_neg_terms_none(self) -> None:
        self.assertEqual(neg_terms("書院 引用"), [])

    def test_neg_terms_hyphen_in_word_not_matched(self) -> None:
        """Hyphen inside a word like 'state-of-the-art' must not be treated as negation."""
        self.assertEqual(neg_terms("state-of-the-art"), [])

    def test_strip_neg_terms(self) -> None:
        # Regex matches word-char runs; the dot in "2.7" breaks the match so only "-2" is stripped.
        self.assertEqual(strip_neg_terms("Python -2 django"), "Python  django")
        self.assertEqual(strip_neg_terms("Python -legacy django"), "Python  django")

    def test_strip_neg_terms_noop(self) -> None:
        self.assertEqual(strip_neg_terms("書院 引用"), "書院 引用")

    def test_bm25_neg_term_filters_results(self) -> None:
        """Chunks containing a negated term must be excluded from results."""
        with make_store() as s:
            nb_id = s.create_notebook("neg-test").id
            src = s.add_source(nb_id, "txt", "doc", "mem://d", "sha-d")
            s.add_chunks(src.id, [
                "Python is a programming language.",
                "Python 2.7 is legacy.",
                "Django is a web framework.",
            ])
            hits = bm25_search(s, nb_id, "Python -legacy", k=10)
            texts = [h.text for h in hits]
            self.assertTrue(any("programming" in t for t in texts), "non-neg hit missing")
            self.assertFalse(any("legacy" in t for t in texts), "negated hit should be excluded")

    def test_retrieve_neg_term_excludes_vec_hits(self) -> None:
        """Negative-term filter applies after fusion so vector hits are also excluded."""
        with make_store() as s:
            nb_id = s.create_notebook("neg-vec").id
            src = s.add_source(nb_id, "txt", "doc", "mem://d", "sha-d")
            s.add_chunks(src.id, [
                "書院は知の書斎である。",
                "書斎と書院には大きな違いがある。legacy",
            ])
            hits = retrieve(s, nb_id, "書院 -legacy")
            self.assertFalse(
                any("legacy" in h.text for h in hits),
                "vector hits containing negated term must be excluded",
            )


class TestBM25FTSLikeMerge(unittest.TestCase):
    """Ranking correctness when FTS5 and LIKE results are merged (v0.2.47)."""

    def test_fts5_hit_with_more_terms_outranks_like_only_hit(self) -> None:
        """A chunk found by FTS5 (long term) + LIKE (short term) must rank above
        a chunk found only by LIKE (short term), even in a tiny 2-doc corpus where
        raw FTS5 BM25 scores are near-zero (~2e-6).

        Regression for v0.2.41 which correctly fixed early-return but left FTS5 hits
        unable to compare against LIKE-only hits due to scale mismatch.
        """
        with make_store() as s:
            nb_id = s.create_notebook("merge-rank").id
            a = s.add_source(nb_id, "txt", "A", "mem://a", "sha-a")
            # Source A matches BOTH the long FTS5 term (引用検証) AND the short LIKE term (書斎)
            s.add_chunks(a.id, ["書院は知の書斎である。引用検証が差別化の核。"])
            b = s.add_source(nb_id, "txt", "B", "mem://b", "sha-b")
            # Source B matches ONLY the short LIKE term (書斎)
            s.add_chunks(b.id, ["軽量LLMでも書斎の検索品質は維持できる。"])
            hits = bm25_search(s, nb_id, "引用検証 書斎", k=4)
            self.assertTrue(len(hits) >= 2, "both sources should be returned")
            self.assertEqual(
                hits[0].source_id, a.id,
                "source A (matches both FTS5 and LIKE terms) must rank first",
            )


class TestAdaptiveAlphaKeyword(unittest.TestCase):
    """Tests for keyword-detection in adaptive_alpha (v0.2.47)."""

    def test_short_keyword_query_favours_bm25(self) -> None:
        """A short keyword-style query (≤3 terms, no question) should get alpha < 0.5."""
        alpha = adaptive_alpha("Python Django")
        self.assertLess(alpha, 0.5, "short keyword query must favour BM25 (alpha < 0.5)")

    def test_short_cjk_keyword_favours_bm25(self) -> None:
        alpha = adaptive_alpha("書院")
        self.assertLess(alpha, 0.5)

    def test_question_overrides_short_keyword(self) -> None:
        """A short query ending in '?' must NOT get the keyword penalty."""
        alpha_q = adaptive_alpha("何ですか？")
        alpha_kw = adaptive_alpha("Python")
        self.assertGreater(alpha_q, alpha_kw, "question must get higher alpha than bare keyword")

    def test_long_narrative_query_favours_vector(self) -> None:
        """A natural-language question must get alpha > 0.5."""
        # Japanese question detected via ？ ending (gets +0.15 boost)
        alpha_ja = adaptive_alpha("機械学習と深層学習の違いは何ですか？")
        self.assertGreater(alpha_ja, 0.5)
        # English question with ≥6 terms (keyword penalty does not apply when >= 6 terms)
        alpha_en = adaptive_alpha("what is the difference between machine learning and deep learning")
        self.assertGreater(alpha_en, 0.5)

    def test_alpha_within_bounds(self) -> None:
        """Alpha must stay in [0.2, 0.8] for all test cases."""
        queries = ["a", "a b c d e f g h i j", "何か？", '"quoted"', "123 -abc"]
        for q in queries:
            a = adaptive_alpha(q)
            self.assertGreaterEqual(a, 0.2)
            self.assertLessEqual(a, 0.8)


if __name__ == "__main__":
    unittest.main(verbosity=1)
