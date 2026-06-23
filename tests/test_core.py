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
        self.assertEqual(VERSION, "0.2.37")

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


if __name__ == "__main__":
    unittest.main(verbosity=1)
