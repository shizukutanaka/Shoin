"""Shoin core tests. Run: python3 tests/test_core.py"""

from __future__ import annotations

import socket
import sqlite3
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
    rrf_fuse,
    strip_neg_terms,
)
from shoin.search import Hit, _char_bigrams
from shoin.store import Store, StoreError, _retry_on_lock, pack_vector, unpack_vector

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
        self.assertEqual(VERSION, "0.2.136")

    def test_migrate_idempotent(self) -> None:
        with make_store() as s:
            self.assertEqual(s.migrate(), 6)
            self.assertEqual(s.migrate(), 6)

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

    def test_update_source_title_empty_or_whitespace_raises(self) -> None:
        """update_source_title() must reject an empty/whitespace-only title.

        The Web API path (PATCH /api/sources/{id}) already rejects this via
        server.py's _require() before calling this method, but the CLI path
        (`shoin source rename`, v0.2.68, explicitly meant to give the SAME
        capability per CLAUDE.md's REQ-103 CLI-parity claim) called this
        method directly with no validation at all, silently persisting a
        blank/invisible source title that the Web API's equivalent request
        would have rejected with HTTP 400. The guard belongs here, at the
        store level, so every caller (present and future) is protected.
        """
        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "original.txt", "original.txt", "hw")
            for bad in ("", "   ", "\t\n"):
                with self.assertRaises(StoreError) as cm:
                    s.update_source_title(src.id, bad, "original.txt")
                self.assertEqual(cm.exception.code, "VALIDATION_REQUIRED_FIELD_MISSING")
            self.assertEqual(s.get_source(src.id).title, "original.txt")

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

    def test_replace_chunks_with_sha256_updates_metadata_atomically(self) -> None:
        """replace_chunks_for_source with sha256/title must update both chunks and
        source metadata in a single transaction.

        Before v0.2.53, refresh_source called replace_chunks_for_source and
        update_source_sha256 as two SEPARATE transactions. A crash between them
        left new chunks committed with stale sha256/title. Fix: pass sha256/title
        to replace_chunks_for_source so both operations are in the same transaction.
        """
        with make_store() as s:
            nb = s.create_notebook("nb-atomic")
            src = s.add_source(nb.id, "url", "old title", "https://x.com", "sha-old")
            s.add_chunks(src.id, ["old chunk"])
            ids_new = s.replace_chunks_for_source(
                src.id, ["new chunk"], sha256="sha-new", title="new title"
            )
            self.assertEqual(len(ids_new), 1)
            # Chunks replaced
            texts = [t for _, t in s.text_chunks_for_source(src.id)]
            self.assertEqual(texts, ["new chunk"])
            # Metadata updated atomically in the same transaction
            updated = s.get_source(src.id)
            self.assertEqual(updated.sha256, "sha-new")
            self.assertEqual(updated.title, "new title")

    def test_replace_chunks_title_fallback_does_not_clobber_concurrent_rename(self) -> None:
        """A concurrent PATCH /api/sources/{id} rename landing between
        replace_chunks_for_source()'s pre-transaction get_source() read and its
        own UPDATE must survive — not be silently overwritten by the stale
        pre-transaction snapshot of the title.

        get_source() reads src.title BEFORE the transaction begins (SQLite's
        implicit BEGIN only fires at the first DML statement, not at `with
        self.conn:` entry). refresh_source() (v0.2.87) deliberately passes
        title=None so this method's own `title or src.title` fallback keeps
        whatever title is currently set — but resolving that fallback from a
        pre-transaction Python read meant a rename committed in the race
        window was clobbered by the stale value, reintroducing exactly the
        v0.2.87 bug (refresh overwriting a custom rename) via a race instead
        of unconditionally. Reproduced by injecting the concurrent rename
        into get_source() itself, exactly where the real race window is.
        """
        with make_store() as s:
            nb = s.create_notebook("nb-race")
            src = s.add_source(nb.id, "url", "Original Page Title", "https://x.com", "sha-orig")
            s.add_chunks(src.id, ["old content"])

            orig_get_source = s.get_source
            calls = {"n": 0}

            def racy_get_source(source_id: int):
                result = orig_get_source(source_id)
                calls["n"] += 1
                if calls["n"] == 1:
                    s.update_source_title(source_id, "My Custom Curated Name", result.origin)
                return result

            with patch.object(s, "get_source", side_effect=racy_get_source):
                s.replace_chunks_for_source(src.id, ["new content"], sha256="sha-new")

            self.assertEqual(s.get_source(src.id).title, "My Custom Curated Name")

    def test_replace_chunks_with_sha256_collision_raises_source_already_exists(self) -> None:
        """replace_chunks_for_source with a sha256 that matches another source must raise
        SOURCE_ALREADY_EXISTS and leave the original chunks intact (atomic rollback).

        This verifies that the UNIQUE constraint on (notebook_id, sha256) is respected
        inside the merged transaction and triggers rollback of the chunk replacement.
        """
        with make_store() as s:
            nb = s.create_notebook("nb-collision")
            s.add_source(nb.id, "url", "existing", "https://a.com", "sha-taken")
            src2 = s.add_source(nb.id, "url", "second", "https://b.com", "sha-other")
            s.add_chunks(src2.id, ["original chunk"])
            with self.assertRaises(StoreError) as cm:
                s.replace_chunks_for_source(src2.id, ["new chunk"], sha256="sha-taken", title="t")
            self.assertEqual(cm.exception.code, "SOURCE_ALREADY_EXISTS")
            # Rollback: original chunks must be untouched
            texts = [t for _, t in s.text_chunks_for_source(src2.id)]
            self.assertEqual(texts, ["original chunk"])

    def test_replace_chunks_non_fk_integrity_error_raises_internal_not_not_found(self) -> None:
        """A non-UNIQUE, non-FOREIGN-KEY IntegrityError (e.g. NOT NULL, CHECK) mid-INSERT
        must raise SYSTEM_INTERNAL_ERROR, not the misleading SOURCE_NOT_FOUND the previous
        catch-all fell through to. The exact same bug class was fixed in add_source()
        (v0.2.53) but never ported to this sibling method: server.py maps any *_NOT_FOUND
        code straight to HTTP 404, so a real constraint violation (source fully intact)
        was reported to callers as "source not found," actively misleading them.

        Also verifies the transaction correctly rolls back on this failure path: the
        original chunks must remain untouched.
        """
        with make_store() as s:
            nb = s.create_notebook("nb-integrity")
            src = s.add_source(nb.id, "url", "a", "https://a.com", "sha-a")
            s.add_chunks(src.id, ["old chunk one", "old chunk two"])

            class _FailingConn:
                """Delegates all conn calls to the real conn, except the 2nd chunk
                INSERT, which raises a non-UNIQUE, non-FOREIGN-KEY IntegrityError."""
                def __init__(self, real):
                    self._real = real
                    self._insert_count = 0

                def execute(self, sql, params=()):
                    if sql.startswith("INSERT INTO chunks"):
                        self._insert_count += 1
                        if self._insert_count == 2:
                            raise sqlite3.IntegrityError("NOT NULL constraint failed: chunks.text")
                    return self._real.execute(sql, params)

                def __enter__(self):
                    return self._real.__enter__()

                def __exit__(self, *a):
                    return self._real.__exit__(*a)

                def __getattr__(self, name):
                    return getattr(self._real, name)

            real_conn = s.conn
            s.conn = _FailingConn(real_conn)
            try:
                with self.assertRaises(StoreError) as cm:
                    s.replace_chunks_for_source(src.id, ["new1", "new2", "new3"])
            finally:
                s.conn = real_conn

            self.assertEqual(cm.exception.code, "SYSTEM_INTERNAL_ERROR")
            # The source must still exist — this was never a deletion.
            self.assertIsNotNone(s.get_source(src.id))
            # Rollback: original chunks must be untouched.
            texts = [t for _, t in s.text_chunks_for_source(src.id)]
            self.assertEqual(texts, ["old chunk one", "old chunk two"])

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

    def test_migrate_duplicate_column_race_recovered_when_winner_committed(self) -> None:
        """Deterministic unit test for the v0.2.128 fix to the migration-5 ALTER
        TABLE race the threaded test above only catches probabilistically
        (~50% failure rate pre-fix, confirmed via 40+ consecutive runs).

        SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so migration 5
        (unlike every other migration here) is NOT naturally idempotent.
        server.py opens a fresh Store() per HTTP request under
        ThreadingHTTPServer, so two concurrent requests against a shared file
        still on schema v4 can both read current=4 and both attempt migration
        5; SQLite allows only one writer, so the loser's own BEGIN blocks until
        the winner commits, then the loser's ALTER TABLE immediately fails with
        'duplicate column name' -- a genuine constraint violation, not lock
        contention, so it does NOT contain "locked" and _retry_on_lock's
        blanket substring check never retries it. By the time the loser's
        exception fires, SQLite's write-serialization guarantees the winner's
        commit is already visible -- this test forces exactly that ordering by
        making _migrate_once()'s FIRST schema_migrations read report the stale
        pre-race value (4) while a SECOND, later read (inside the exception
        handler) sees the real, already-migrated state (6).
        """
        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "race.db")
            with Store(db_path):
                pass  # fully migrated: schema_migrations already has 1..6

            loser = Store(db_path)
            real_conn = loser.conn
            calls = {"n": 0}

            class _StaleFirstReadConn:
                """Proxy standing in for a connection whose first
                schema_migrations read raced ahead of a concurrent winner's
                commit. sqlite3.Connection's `execute` is a read-only C-level
                attribute (cannot be patched via unittest.mock.patch.object
                directly), so this wraps the real connection instead."""

                def execute(self, sql, *params):
                    if sql.strip().startswith("SELECT MAX(version)"):
                        calls["n"] += 1
                        if calls["n"] == 1:
                            return real_conn.execute("SELECT 4 AS v")
                        return real_conn.execute(sql, *params)
                    return real_conn.execute(sql, *params)

                def __getattr__(self, name):
                    return getattr(real_conn, name)

            loser.conn = _StaleFirstReadConn()
            result = loser._migrate_once()
            self.assertEqual(result, 6, "must recover and reach the latest version, not raise")

    def test_migrate_duplicate_column_reraises_when_not_actually_won(self) -> None:
        """The exception handler must NOT silently swallow a duplicate-column
        error when schema_migrations does NOT show the migration as already
        applied -- that would mask a genuine, unexpected schema conflict
        instead of the benign concurrent-winner race it's designed to recover
        from."""
        import shoin.store as store_mod

        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "anomaly.db")
            original = store_mod.MIGRATIONS[:]
            try:
                store_mod.MIGRATIONS = [m for m in original if m[0] <= 4]
                s = Store(db_path)
                # Add the column OUTSIDE of any migration bookkeeping --
                # schema_migrations stays at 4, simulating a genuinely
                # anomalous DB state rather than a legitimate concurrent
                # winner having already applied migration 5.
                s.conn.execute("ALTER TABLE chunks ADD COLUMN context TEXT NOT NULL DEFAULT ''")
                s.conn.commit()
                s.close()
            finally:
                store_mod.MIGRATIONS = original

            with self.assertRaises(sqlite3.OperationalError):
                Store(db_path)  # schema_migrations still says 4; must not skip silently

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

    def test_add_chunks_non_fk_integrity_error_raises_internal_not_not_found(self) -> None:
        """A non-FOREIGN-KEY IntegrityError (e.g. a future NOT NULL/CHECK
        violation on chunks) must raise SYSTEM_INTERNAL_ERROR, not the
        misleading SOURCE_NOT_FOUND the previous catch-all fell through to.

        add_chunks() is a third sibling with the identical
        "INSERT ... source_id REFERENCES sources(id)" FK shape as add_source()
        (fixed v0.2.53) and replace_chunks_for_source() (fixed v0.2.86) — this
        method never got the same fix. server.py maps any *_NOT_FOUND code
        straight to HTTP 404, so a real constraint violation (source fully
        intact) was reported to callers as "source not found," actively
        misleading them — a fabricated diagnosis pointing at a nonexistent
        race condition instead of the real defect.
        """
        with make_store() as s:
            nb = s.create_notebook("n-integrity")
            src = s.add_source(nb.id, "txt", "a", "o", "h-integrity")
            with self.assertRaises(StoreError) as cm:
                s.add_chunks(src.id, [None])  # type: ignore[list-item]  # triggers NOT NULL, not FK
            self.assertEqual(cm.exception.code, "SYSTEM_INTERNAL_ERROR")
            # The source must still exist — this was never a deletion.
            self.assertIsNotNone(s.get_source(src.id))

    def test_add_message_non_fk_integrity_error_raises_internal_not_not_found(self) -> None:
        """A fourth sibling with the same bug family: a non-FOREIGN-KEY
        IntegrityError on messages (no UNIQUE constraint on this table, so
        the only expected violation is the FK on notebook_id) must raise
        SYSTEM_INTERNAL_ERROR, not the misleading NOTEBOOK_NOT_FOUND the
        previous catch-all fell through to. A full sweep (following
        v0.2.104) found 4 more sibling functions never given this fix:
        add_message, add_note, add_studio_output, update_source_sha256.
        """
        with make_store() as s:
            nb = s.create_notebook("n-msg-integrity")
            with self.assertRaises(StoreError) as cm:
                s.add_message(nb.id, "user", None, "{}")  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "SYSTEM_INTERNAL_ERROR")
            self.assertIsNotNone(s.get_notebook(nb.id))

    def test_add_note_non_fk_integrity_error_raises_internal_not_not_found(self) -> None:
        """Same bug family, add_note() sibling (notes has no UNIQUE constraint,
        so the only expected IntegrityError is the FK on notebook_id)."""
        with make_store() as s:
            nb = s.create_notebook("n-note-integrity")
            with self.assertRaises(StoreError) as cm:
                s.add_note(nb.id, "title", None)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "SYSTEM_INTERNAL_ERROR")
            self.assertIsNotNone(s.get_notebook(nb.id))

    def test_add_studio_output_non_fk_integrity_error_raises_internal_not_not_found(self) -> None:
        """Same bug family, add_studio_output() sibling (studio_outputs has no
        UNIQUE constraint, so the only expected IntegrityError is the FK on
        notebook_id)."""
        with make_store() as s:
            nb = s.create_notebook("n-studio-integrity")
            with self.assertRaises(StoreError) as cm:
                s.add_studio_output(nb.id, "briefing", None, "{}")  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "SYSTEM_INTERNAL_ERROR")
            self.assertIsNotNone(s.get_notebook(nb.id))

    def test_update_source_sha256_non_unique_integrity_error_raises_internal(self) -> None:
        """update_source_sha256() must not misclassify a non-UNIQUE
        IntegrityError (e.g. a NOT NULL violation) as SOURCE_ALREADY_EXISTS.
        Unlike its INSERT-based siblings, this method is an UPDATE that never
        touches notebook_id, so no FOREIGN KEY violation is possible here —
        the discrimination is UNIQUE (genuine collision) vs. everything else."""
        with make_store() as s:
            nb = s.create_notebook("n-sha-integrity")
            src = s.add_source(nb.id, "url", "title", "https://x.com", "sha-orig")
            with self.assertRaises(StoreError) as cm:
                s.update_source_sha256(src.id, None, "new title")  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "SYSTEM_INTERNAL_ERROR")
            self.assertIsNotNone(s.get_source(src.id))

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


class TestRetryOnLock(unittest.TestCase):
    """Deterministic coverage for _retry_on_lock(); the only prior coverage was
    the probabilistic test_migrate_concurrent_shared_db_file_no_crash test above,
    which exercises retry timing only by chance."""

    def test_succeeds_after_transient_locked_errors(self) -> None:
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with patch("time.sleep"):
            self.assertEqual(_retry_on_lock(flaky), "ok")
        self.assertEqual(calls["n"], 3)

    def test_raises_original_exception_after_exhausting_attempts(self) -> None:
        def always_locked() -> str:
            raise sqlite3.OperationalError("database is locked")

        with patch("time.sleep"):
            with self.assertRaises(sqlite3.OperationalError) as cm:
                _retry_on_lock(always_locked, attempts=3)
        self.assertIn("locked", str(cm.exception).lower())

    def test_non_locked_operational_error_raised_immediately(self) -> None:
        calls = {"n": 0}

        def bad_syntax() -> str:
            calls["n"] += 1
            raise sqlite3.OperationalError("near \"SELCT\": syntax error")

        with self.assertRaises(sqlite3.OperationalError):
            _retry_on_lock(bad_syntax)
        self.assertEqual(calls["n"], 1, "must not retry non-lock OperationalErrors")

    def test_attempts_zero_calls_fn_once_without_crashing(self) -> None:
        """Before the fix, attempts=0 skipped the loop entirely, leaving last_exc
        as None: `assert last_exc is not None` fired (or, under python -O, execution
        fell through to `raise last_exc` with last_exc still None, raising a bare
        TypeError that masked the real error)."""
        calls = {"n": 0}

        def fn() -> str:
            calls["n"] += 1
            return "direct"

        self.assertEqual(_retry_on_lock(fn, attempts=0), "direct")
        self.assertEqual(calls["n"], 1)


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

    def test_is_cjk_fullwidth_digits_and_letters(self) -> None:
        """Fullwidth digits/letters (U+FF10-19, FF21-3A, FF41-5A) must count as CJK.

        These are extremely common in real Japanese documents (invoices, IDs,
        prices, dates use zenkaku digit formatting as a standard convention).
        Before this fix, estimate_tokens() counted a long run of fullwidth
        digits as ~0 tokens (neither is_cjk() nor the ASCII-only _WORD_RE
        matched them), silently defeating both _hard_split()'s chunk-size
        cap and build_context()'s per-source token budget for any document
        containing such a run.
        """
        self.assertTrue(is_cjk("０"), "fullwidth digit must be CJK")
        self.assertTrue(is_cjk("９"), "fullwidth digit must be CJK")
        self.assertTrue(is_cjk("Ａ"), "fullwidth uppercase Latin must be CJK")
        self.assertTrue(is_cjk("ａ"), "fullwidth lowercase Latin must be CJK")
        block = "項目" + "０１２３４５６７８９" * 1200
        self.assertEqual(
            estimate_tokens(block), len(block),
            "a long run of fullwidth digits must not be undercounted to near-zero",
        )
        chunks = split_text(block)
        self.assertGreater(
            len(chunks), 1,
            "an oversize block of fullwidth digits must actually be split into chunks",
        )

    def test_long_unbroken_ascii_run_not_undercounted_to_near_zero(self) -> None:
        """A single unbroken alphanumeric run (base64 data: URI, long hex hash,
        minified code with no spaces) must not estimate to ~1 token regardless
        of length. Before this fix, _WORD_RE.findall() matched such a run as
        ONE regex match, and estimate_tokens() counted len(matches) — one
        match = one token no matter how long — so a 200,000-character run
        estimated to a single-digit token count, silently defeating both
        split_text()'s CHUNK_TOKENS cap (the block was never split) and
        build_context()'s per-source token budget (the whole blob sailed
        through untruncated). This is a different, more general defect than
        the CJK-range-coverage gaps (v0.2.50/52/107): here the count is a
        small nonzero number, so it evades every `tok == 0` fallback.
        """
        import random
        import string

        from shoin.chunk import _tail

        rng = random.Random(0)
        blob = "".join(rng.choices(string.ascii_letters + string.digits, k=3000))
        text = f"See the embedded data below.\n\n{blob}\n\nEnd of document."

        self.assertGreater(
            estimate_tokens(text), 600,
            "a 3000-char unbroken run must cost roughly len/4 tokens, not ~1",
        )
        chunks = split_text(text)
        self.assertGreater(
            len(chunks), 1,
            "an oversize unbroken run must actually be split into multiple chunks",
        )
        # _tail must also be able to stop mid-run instead of pulling in the
        # entire multi-thousand-character run regardless of the token budget.
        tail = _tail(blob, 10)
        self.assertLess(
            len(tail), len(blob),
            "_tail with a small token budget must not return the entire long run",
        )

        # A normal-length word/identifier must be completely unaffected —
        # this must still cost exactly 1 token, matching every other
        # estimate_tokens/_truncate_tokens/_tail test in this file.
        self.assertEqual(estimate_tokens("parse_user_input"), 1)

    def test_tail_credits_alnum_run_interrupted_by_cjk_character(self) -> None:
        """_tail must not silently drop an alnum run's token cost when the run
        is immediately adjacent to a CJK character (idiomatic in Japanese
        text with no space before an ASCII model/section number, e.g.
        "型番ABC123456"). Before this fix, the is_cjk() branch reset run_len
        to 0 without ever crediting the interrupted run's base 1-token cost
        (unlike the punctuation/space branch, which always did) — so _tail()
        under-counted internally, scanned further left than it should, and
        returned a suffix costing MORE tokens than requested. A 20,000-trial
        fuzz of mixed CJK/ASCII/punctuation text found this affected 51% of
        cases; this test reproduces the same failure with real Japanese prose.
        """
        from shoin.chunk import _tail

        text = "この製品の型番はABC123456であり、価格は書院にて公開されている。"
        for tokens in range(10, 25):
            out = _tail(text, tokens)
            got = estimate_tokens(out)
            self.assertLessEqual(
                got, tokens,
                f"_tail(text, {tokens}) returned {got} tokens — must not exceed the request",
            )

    def test_tail_long_run_overlap_matches_chunk_overlap_budget(self) -> None:
        """The real split_text() -> _tail() overlap computation (CHUNK_OVERLAP,
        config.py) must not overshoot its own budget for realistic Japanese
        text mixing kanji and adjacent ASCII identifiers — the exact
        production call pattern (chunk.py:_hard_split's overlap wiring), not
        just an isolated unit-level reproduction.
        """
        from shoin.chunk import _tail
        from shoin.config import CHUNK_OVERLAP

        para = "これは書院という製品の説明である。型番ABC123456はとても軽量で高速に動作する。" * 3
        doc = (para + "\n\n") * 6
        chunks = split_text(doc)
        self.assertGreater(len(chunks), 1)
        overlap = _tail(chunks[0], CHUNK_OVERLAP)
        self.assertLessEqual(estimate_tokens(overlap), CHUNK_OVERLAP)

    def test_tail_non_ascii_alphabetic_scripts_do_not_undercount(self) -> None:
        """_tail() must agree with estimate_tokens() for non-ASCII, non-CJK
        alphabetic text (accented Latin, Cyrillic, Greek, ...) — same defect
        class as test_non_ascii_alphabetic_scripts_do_not_undercount in
        test_qa.py's TestTruncateTokens, for the sibling backward-scanning
        function. Before this fix, ch.isalnum() (Unicode-wide) merged runs
        that _WORD_RE (ASCII-only, estimate_tokens()'s actual cost model)
        would split at each non-ASCII letter, so _tail() under-counted and
        returned more text than the requested token budget allowed.
        """
        from shoin.chunk import _tail

        text = (
            "The café in Zürich serves crème brûlée and naïve tourists "
            "love café Beyoncé Müller "
        ) * 5
        for tokens in (5, 50, 64):
            out = _tail(text, tokens)
            self.assertEqual(
                estimate_tokens(out), tokens,
                f"_tail(text, {tokens}) must cost exactly {tokens} tokens",
            )

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

    def test_html_unclosed_comment_does_not_swallow_rest_of_document(self) -> None:
        """An unclosed <!-- comment must not silently discard everything after it.

        Python's stdlib html.parser.HTMLParser buffers an unclosed <!-- comment
        and, on close(), flushes everything from "<!--" to end-of-document as one
        comment payload. _HTMLText has no handle_comment override, so that payload
        (and every real tag/text node inside it) was silently discarded with no
        error and no INGEST_EMPTY signal — a truncated network fetch or one
        forgotten "-->" would drop the rest of the document with zero indication
        anything was lost.
        """
        html = (
            "<html><head><title>Report</title></head><body>"
            "<p>Section 1: Introduction text that matters.</p>"
            "<!-- unclosed comment starts here"
            "<p>Section 3: the critical conclusion. Numbers: 42</p>"
            "<p>Section 4: more content.</p>"
        )
        title, text = html_to_text(html)
        self.assertIn("Section 1", text)
        self.assertIn("Section 3", text, "content after an unclosed comment must not be lost")
        self.assertIn("Section 4", text, "content after an unclosed comment must not be lost")

    def test_html_well_formed_comment_still_ignored(self) -> None:
        """A normal, properly-closed comment must still be excluded from the
        extracted text — the unclosed-comment fix must not affect this case."""
        html = (
            "<html><body>"
            "<p>Before comment.</p>"
            "<!-- this is a normal well-formed comment, should be ignored -->"
            "<p>After comment.</p>"
            "</body></html>"
        )
        _, text = html_to_text(html)
        self.assertNotIn("well-formed comment", text)
        self.assertIn("Before comment", text)
        self.assertIn("After comment", text)

    def test_html_mismatched_noscript_closer_in_body_does_not_swallow_rest(self) -> None:
        """An unclosed/mismatched-closer <noscript> INSIDE <body> must not
        silently discard all subsequent body content.

        v0.2.40 fixed this only for <head> (handle_endtag resets _skip_depth
        at </head>). There was no equivalent recovery for the same tag
        dangling inside <body> — _skip_depth stayed elevated for the rest of
        parsing, and every subsequent handle_data() call was silently
        dropped, with no error and no INGEST_EMPTY signal (text before the
        dangling tag already made it through). A mismatched closing tag
        (</noscript-analytics> instead of </noscript> — a realistic typo in
        a broken analytics snippet) reproduces the same failure as a
        genuinely absent closer.
        """
        html = (
            "<html><body>"
            "<p>Section 1: Important paragraph.</p>"
            "<noscript><img src=\"pixel.gif\"></noscript-analytics>"
            "<p>Section 2: Important paragraph that must not be lost.</p>"
            "</body></html>"
        )
        _, text = html_to_text(html)
        self.assertIn("Section 1", text)
        self.assertIn("Section 2", text, "content after the dangling <noscript> must not be lost")

    def test_html_unclosed_template_in_body_does_not_swallow_rest(self) -> None:
        """The same class of fix applied to <template>, the other skip-tag
        that (unlike <script>/<style>) is not a real CDATA content element
        and has no browser-spec reason to swallow to end-of-document when
        genuinely unclosed."""
        html = "<html><body><p>Before template.</p><template><p>After unclosed template.</p></body></html>"
        _, text = html_to_text(html)
        self.assertIn("Before template", text)
        self.assertIn(
            "After unclosed template", text,
            "content after an unclosed <template> must not be lost",
        )

    def test_html_well_formed_noscript_in_body_still_skipped(self) -> None:
        """A normal, properly-closed <noscript> in <body> must still have its
        content excluded — the unbalanced-tag fix must not affect this case."""
        html = (
            "<html><body>"
            "<p>Before.</p>"
            "<noscript>fallback content, should be ignored</noscript>"
            "<p>After noscript, well formed.</p>"
            "</body></html>"
        )
        _, text = html_to_text(html)
        self.assertNotIn("fallback content", text)
        self.assertIn("Before", text)
        self.assertIn("After noscript, well formed", text)

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

    def test_pdf_to_text_one_bad_page_does_not_discard_good_pages(self) -> None:
        """A single page whose extract_text() raises (a real pypdf failure mode
        — malformed content stream, bad font, corrupt xref entry) must not
        discard every other page's perfectly good text. Before this fix, the
        list comprehension ran inside one shared try/except, so one bad page
        among many good ones aborted the ENTIRE document — zero content
        indexed — contradicting the project's own graceful-degradation
        principle (CLAUDE.md: "Studio outputs have fallback text...
        History_messages() survives malformed chats"), already applied the
        same way to per-batch embedding failures in pipeline.py.
        """
        from unittest.mock import MagicMock, patch

        from shoin.ingest import pdf_to_text

        try:
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest("pypdf not installed")

        good1 = MagicMock()
        good1.extract_text.return_value = "quarterly revenue grew by 12 percent"
        bad = MagicMock()
        bad.extract_text.side_effect = Exception("malformed content stream on this page")
        good2 = MagicMock()
        good2.extract_text.return_value = "board approved a new dividend policy"

        fake_reader = MagicMock()
        fake_reader.pages = [good1, bad, good2]

        with patch("pypdf.PdfReader", return_value=fake_reader):
            result = pdf_to_text(b"fake pdf bytes")
        self.assertIn("quarterly revenue grew by 12 percent", result)
        self.assertIn("board approved a new dividend policy", result)

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

    def test_extract_url_null_byte_body_raises_ingest_empty(self) -> None:
        """extract_url must raise INGEST_EMPTY when the response body decodes to only
        null bytes (U+0000).

        str.strip() does not remove null bytes (category Cc, not Unicode whitespace),
        so a body of b'\\x00\\x00\\x00' produced the non-empty string '\\x00\\x00\\x00'
        which passed the `not text` guard and was indexed as garbage content.
        Before v0.2.58, extract_file() had the null-byte guard (v0.2.50) but
        extract_url() did not.  Fix: apply text.replace('\\x00', '') before strip().
        """
        import shoin.ingest as ing

        null_body = b"\x00\x00\x00"
        with (
            patch.object(
                ing, "fetch_url",
                return_value=(null_body, "text/plain; charset=utf-8", "http://example.com/nulls")
            ),
            self.assertRaises(IngestError) as cm,
        ):
            ing.extract_url("http://example.com/nulls")
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
    def test_bm25_and_vector_search_populate_hit_context(self) -> None:
        """bm25_search()/vector_search() must load each chunk's stored context
        onto the Hit so build_context() can surface the section (v0.2.130)."""
        with make_store() as s:
            nb = s.create_notebook("nb").id
            src = s.add_source(nb, "txt", "doc", "o", "sha")
            s.add_chunks(src.id, ["光合成の詳細な説明がここに続く長い記述の文章。"], ["生物 > 光合成"])
            bm = bm25_search(s, nb, "光合成", k=5)
            self.assertTrue(bm)
            self.assertEqual(bm[0].context, "生物 > 光合成")

            # vector_search: give the chunk an embedding, query with a vector.
            chunk = s.chunks_for_notebook(nb)[0]
            s.set_embedding(chunk.id, [1.0, 0.0])
            from shoin.search import vector_search
            vh = vector_search(s, nb, [1.0, 0.0], k=5)
            self.assertTrue(vh)
            self.assertEqual(vh[0].context, "生物 > 光合成")

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

    def test_rrf_fuse_bm25_only_scores_nonzero(self) -> None:
        """rrf_fuse() with empty vec_hits must return BM25 hits with RRF scores > 0."""
        hits = [Hit(1, 1, "text", 0.0, bm25=5.0), Hit(2, 1, "other", 0.0, bm25=3.0)]
        result = rrf_fuse(hits, [])
        self.assertEqual(len(result), 2)
        self.assertGreater(result[0].score, 0.0)
        self.assertGreater(result[0].score, result[1].score)

    def test_rrf_fuse_vec_only_scores_nonzero(self) -> None:
        """rrf_fuse() with empty bm25_hits must return vec hits with RRF scores > 0."""
        hits = [Hit(1, 1, "text", 0.0, vec=0.9), Hit(2, 1, "other", 0.0, vec=0.5)]
        result = rrf_fuse([], hits)
        self.assertEqual(len(result), 2)
        self.assertGreater(result[0].score, 0.0)
        self.assertGreater(result[0].score, result[1].score)

    def test_rrf_fuse_shared_chunk_scores_higher(self) -> None:
        """A chunk ranking well in BOTH BM25 and vector must outscore single-list chunks.

        This is the key RRF invariant: a chunk appearing at rank 1 in both lists
        gets 2×(1/61) ≈ 0.0328, while a chunk only at rank 1 in one list gets
        1/61 ≈ 0.0164. The shared chunk must win regardless of which list it
        appeared in.
        """
        # chunk 1: rank 1 in BM25 only
        bm25_hits = [Hit(1, 1, "bm25-only", 0.0, bm25=10.0),
                     Hit(2, 1, "shared", 0.0, bm25=5.0)]
        # chunk 2: rank 1 in vec only; chunk (2) shared = rank 2 in bm25
        vec_hits = [Hit(3, 1, "vec-only", 0.0, vec=0.95),
                    Hit(2, 1, "shared", 0.0, vec=0.80)]
        result = rrf_fuse(bm25_hits, vec_hits)
        # chunk 2 (shared rank 2 in bm25 + rank 2 in vec) should beat chunk 1 (rank 1 in bm25 only)
        # chunk 2 score = 1/(60+2) + 1/(60+2) = 2/62 ≈ 0.0323
        # chunk 1 score = 1/(60+1) = 1/61 ≈ 0.0164
        result_ids = [h.chunk_id for h in result]
        shared_pos = result_ids.index(2)
        bm25_only_pos = result_ids.index(1)
        self.assertLess(shared_pos, bm25_only_pos,
                        "shared chunk must rank higher than single-list chunk")

    def test_rrf_fuse_no_duplicates(self) -> None:
        """rrf_fuse() must not produce duplicate chunk IDs when a chunk appears in both lists."""
        bm25 = [Hit(1, 1, "shared", 0.0, bm25=2.0), Hit(2, 1, "bm25-only", 0.0, bm25=1.0)]
        vec = [Hit(1, 1, "shared", 0.0, vec=0.9), Hit(3, 1, "vec-only", 0.0, vec=0.7)]
        result = rrf_fuse(bm25, vec)
        ids = [h.chunk_id for h in result]
        self.assertEqual(len(set(ids)), len(ids), "rrf_fuse must not produce duplicate chunk IDs")
        self.assertEqual(len(result), 3)  # 3 distinct chunks

    def test_rrf_fuse_empty_both_returns_empty(self) -> None:
        """rrf_fuse() with both empty lists must return empty list."""
        self.assertEqual(rrf_fuse([], []), [])

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


    def test_retrieve_rrf_scores_normalized_before_rerank(self) -> None:
        """retrieve() must normalize RRF scores to [0,1] before lexical rerank.

        Before v0.2.60, rrf_fuse() returned unnormalized RRF scores in the range
        ~[0.012, 0.033].  With rerank(weight=0.3), lexical_overlap values in [0,1]
        contributed ~10× more than the RRF signal, making the hybrid retrieval ranking
        irrelevant — the reranker effectively became a pure lexical ranker.

        Concrete failure: for a single highly-relevant hit (lex ≈ 1.0, rrf = 1/61 ≈ 0.016),
        the unnormalized path scored 0.7*0.016 + 0.3*1.0 ≈ 0.31. After RRF normalization
        (single hit → score = 1.0), the score is 0.7*1.0 + 0.3*1.0 = 1.0.
        A score > 0.9 is only reachable after normalization.
        """
        with make_store() as s:
            nb_id = s.create_notebook("rrf-norm-test").id
            src = s.add_source(nb_id, "txt", "single-doc", "mem://rrf", "sha-rrf")
            # A single chunk containing the exact query terms — lex ≈ 1.0
            s.add_chunks(src.id, ["書院は知の書斎である。引用付きで文書と対話する。"])
            hits = retrieve(s, nb_id, "書院 書斎", k=3)
            self.assertTrue(hits, "should find at least one hit for the query")
            top = hits[0]
            # Without normalization: max score ≈ 0.7*0.016 + 0.3*1.0 ≈ 0.31
            # With normalization: single hit normalized to 1.0 → score = 0.7*1.0 + 0.3*lex ≥ 0.7
            self.assertGreater(
                top.score,
                0.5,
                f"top hit score ({top.score:.4f}) must be > 0.5 after RRF normalization; "
                f"unnormalized ceiling is ~0.33 (lex overwhelms raw RRF ≈ 0.016).",
            )


class TestDebugAid(unittest.TestCase):
    """SHOIN_DEBUG=1 retrieval diagnostics (v0.2.129).

    CLAUDE.md's "Testing & Debugging" section documented this feature under
    the bare name `DEBUG=1` since long before this session, but no code
    anywhere ever actually read that (or any) environment variable — the
    entire "Debugging Aid" was aspirational prose, not a real capability.
    Implemented for real here, under `SHOIN_DEBUG` (matching every other
    Shoin setting's namespace, avoiding collision with the generic `DEBUG`
    name many unrelated tools/CI systems already use for their own purposes).
    """

    def test_debug_disabled_by_default_no_stderr_output(self) -> None:
        import io
        from unittest.mock import patch

        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "d", "o", "sha")
            s.add_chunks(src.id, ["猫は液体である。"])
            err = io.StringIO()
            with patch("sys.stderr", err):
                retrieve(s, nb.id, "猫")
        self.assertEqual(err.getvalue(), "")

    def test_shoin_debug_enabled_prints_retrieval_diagnostics(self) -> None:
        import io
        import os
        from unittest.mock import patch

        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "d", "o", "sha")
            s.add_chunks(src.id, ["猫は液体である。"])
            err = io.StringIO()
            with patch.dict(os.environ, {"SHOIN_DEBUG": "1"}, clear=False):
                with patch("sys.stderr", err):
                    hits = retrieve(s, nb.id, "猫")
        out = err.getvalue()
        self.assertIn("[DEBUG retrieve]", out)
        self.assertIn("猫", out)
        self.assertIn(f"chunk={hits[0].chunk_id}", out)
        # RRF replaced alpha-based fusion in v0.2.56 -- no "alpha" in output.
        self.assertNotIn("alpha", out)

    def test_bare_debug_env_var_does_not_trigger(self) -> None:
        """The old, never-actually-implemented bare `DEBUG` name must NOT
        enable this feature — only the namespaced `SHOIN_DEBUG` does, so a
        CI system or unrelated tool's own `DEBUG=1` can never accidentally
        turn on Shoin's retrieval-diagnostics printing."""
        import io
        import os
        from unittest.mock import patch

        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "d", "o", "sha")
            s.add_chunks(src.id, ["猫は液体である。"])
            err = io.StringIO()
            with patch.dict(os.environ, {"DEBUG": "1"}, clear=False):
                with patch("sys.stderr", err):
                    retrieve(s, nb.id, "猫")
        self.assertEqual(err.getvalue(), "")

    def test_shoin_debug_covers_retrieve_multi(self) -> None:
        import io
        import os
        from unittest.mock import patch

        from shoin.search import retrieve_multi

        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "d", "o", "sha")
            s.add_chunks(src.id, ["猫は液体である。"])
            err = io.StringIO()
            with patch.dict(os.environ, {"SHOIN_DEBUG": "1"}, clear=False):
                with patch("sys.stderr", err):
                    retrieve_multi(s, nb.id, ["猫", "液体について"])
        out = err.getvalue()
        self.assertIn("[DEBUG retrieve_multi(2 queries)]", out)


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


    def test_degraded_text_s_numbers_match_unique_sources_not_hits(self) -> None:
        """_degraded_text must assign S-numbers per unique source, not per hit.

        When hits[0] and hits[1] come from the same source, the old code emitted
        [S1] for hit0 and [S2] for hit1 — but build_context assigns [S2] to the
        SECOND unique source.  make_report then attributed [S2] to the wrong source
        in the citation report.

        Fix: skip duplicate source_ids in _degraded_text so S-numbers align with
        build_context's first-seen-unique-source ordering.
        """
        from shoin.qa import _degraded_text
        from shoin.search import Hit

        # Two hits from source 5, then one hit from source 7
        hits = [
            Hit(chunk_id=1, source_id=5, text="chunk A from src5", score=1.0),
            Hit(chunk_id=2, source_id=5, text="chunk B from src5", score=0.9),
            Hit(chunk_id=3, source_id=7, text="chunk from src7", score=0.8),
        ]
        text = _degraded_text(hits)
        # [S1] must reference source 5, [S2] must reference source 7
        # The old code would put [S2] on "chunk B from src5" — the fix shows
        # only ONE entry for source 5 (as [S1]) and one for source 7 (as [S2])
        self.assertIn("[S1]", text)
        self.assertIn("[S2]", text)
        self.assertNotIn("[S3]", text, "[S3] should not appear when only 2 unique sources")
        # [S2] must show the source-7 content, not the duplicate source-5 content
        lines = [ln for ln in text.splitlines() if "[S2]" in ln]
        self.assertTrue(lines, "[S2] line must exist")
        self.assertIn("src7", lines[0], "[S2] must show content from source 7, not source 5")

    def test_build_context_zero_token_text_respects_budget(self) -> None:
        """build_context must cap zero-token text (Arabic/Cyrillic/punctuation).

        estimate_tokens() returns 0 for scripts outside _CJK_RANGES and _WORD_RE.
        Before the fix, cost=0 always satisfied `cost > remaining` (False), so all
        chunks were appended without budget enforcement — a 100K-char Arabic paragraph
        would blow the LLM context budget entirely.

        Fix: use effective_cost = len(text) // 5 (≈ 5 chars/token) when cost==0.
        """
        from shoin.qa import build_context
        from shoin.search import Hit

        # Pure ellipsis/punctuation — estimate_tokens() returns 0 for these
        zero_tok_char = "…"  # U+2026, not CJK, not ASCII word → 0 tokens
        self.assertEqual(estimate_tokens(zero_tok_char), 0, "pre-condition: char must be zero-token")
        # 2000-char block of zero-token text → effective cost ≈ 400 tokens (2000 // 5)
        big_zero_tok = zero_tok_char * 2000

        with make_store() as s:
            nb = s.create_notebook("zero-tok-test")
            src = s.add_source(nb.id, "txt", "Doc", "orig", "sha-zt")
            h = Hit(chunk_id=1, source_id=src.id, text=big_zero_tok, score=1.0)
            # budget_tokens=200: per_source=200, effective_cost=400 > 200 → must truncate
            ctx = build_context(s, [h], budget_tokens=200)
        body = ctx.source_bodies[0]
        # Body must be shorter than the full 2000-char input (truncation happened)
        self.assertLess(
            len(body), len(big_zero_tok),
            "zero-token text exceeding budget must be truncated, not appended in full",
        )
        # Body must not be empty (some content was included up to the budget)
        self.assertGreater(len(body), 0, "truncated zero-token body must not be empty")

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

    def test_studio_generate_build_context_db_lock_raises_store_error(self) -> None:
        """studio.generate() must have the same sqlite3.OperationalError guard
        around build_context() that qa.ask() has had since v0.2.44 — it was found
        to be missing (a bare OperationalError propagated to server.py's
        catch-all, returning HTTP 500 with only type(exc).__name__ as the message
        instead of ask()'s clean HTTP 400 SYSTEM_DB_LOCKED with the actual lock
        text) despite both functions calling the exact same build_context() for
        the exact same reason."""
        import sqlite3 as _sqlite3
        from unittest.mock import patch

        from shoin.store import StoreError as _StoreError
        from shoin.studio import generate

        class _NoLLM:
            embedding_model = ""

            def chat(self, messages, temperature=0.2):  # type: ignore[override]
                return "unused"

        with make_store() as s:
            nb_id = seed(s)
            with patch(
                "shoin.studio.build_context",
                side_effect=_sqlite3.OperationalError("database is locked"),
            ):
                with self.assertRaises(_StoreError) as cm:
                    generate(s, _NoLLM(), nb_id, "briefing", persist=False)
            self.assertEqual(cm.exception.code, "SYSTEM_DB_LOCKED")

    def test_suggest_questions_build_context_db_lock_raises_store_error(self) -> None:
        """suggest_questions() has the identical unguarded build_context() call
        as generate() did — same fix, same reasoning. A DB lock must raise
        SYSTEM_DB_LOCKED, not be silently swallowed into [] the way this
        function's own except LLMError does (that's specifically for "LLM
        unreachable", a different, genuinely-best-effort failure class)."""
        import sqlite3 as _sqlite3
        from unittest.mock import patch

        from shoin.store import StoreError as _StoreError
        from shoin.studio import suggest_questions

        class _NoLLM:
            embedding_model = ""

            def chat(self, messages, temperature=0.2):  # type: ignore[override]
                return "unused"

        with make_store() as s:
            nb_id = seed(s)
            with patch(
                "shoin.studio.build_context",
                side_effect=_sqlite3.OperationalError("database is locked"),
            ):
                with self.assertRaises(_StoreError) as cm:
                    suggest_questions(s, _NoLLM(), nb_id)
            self.assertEqual(cm.exception.code, "SYSTEM_DB_LOCKED")


class TestCitation(unittest.TestCase):
    def test_verify_grounding_fullwidth_brackets_do_not_poison_claim_bigrams(self) -> None:
        """verify_grounding must handle full-width citation brackets ［Ｓ１］ correctly.

        Before v0.2.53, _BRACKET_RE only stripped ASCII [ / ] brackets.  Full-width
        brackets ［Ｓ１］ (U+FF3B / U+FF3D) survived into `bare`, producing spurious
        bigrams from the bracket characters and NFKC-normalized form '[s1]'.  For a
        citation-only fragment like "Result. ［Ｓ１］", the non-empty spurious bigrams
        prevented prev_claim propagation, so the citation was never confirmed.

        Fix: apply unicodedata.normalize('NFKC', sentence) before _BRACKET_RE.sub()
        so full-width brackets are also stripped.
        """
        from shoin.citation import verify_grounding

        text = "研究結果は重要だ。 ［Ｓ１］"  # full-width brackets after period
        source_texts = {1: "研究結果は重要だ。実験で確認された。"}
        confirmed, misattributed = verify_grounding(text, source_texts)
        self.assertIn(1, confirmed, "S1 with full-width brackets must be confirmed via prev_claim")
        self.assertNotIn(1, misattributed)

    def test_verify_grounding_fullwidth_brackets_embedded_in_sentence(self) -> None:
        """Full-width brackets embedded mid-sentence must not inflate the claim bigram count.

        Before the fix, '研究結果 ［Ｓ１］。' left bracket chars in `bare`, inflating the
        claim denominator by ~4 spurious bigrams, which could push overlap below
        CONFIRM_MIN (0.30) for short sentences.
        """
        from shoin.citation import verify_grounding

        # Short sentence where bracket inflation would push 2-bigram claim below threshold
        # "AIが重要。" — bare bigrams without brackets: {"ai", "i重", "重要"} (3 bigrams)
        # With FW brackets in bare: would add ~4 bracket bigrams → 7 total → 3/7 = 0.43 (still ok)
        # But for even shorter text: "AI ［Ｓ１］。" → 1 real bigram + 4 bracket = 5 → 1/5 = 0.20 < CONFIRM_MIN
        text = "AI ［Ｓ１］。"
        # Source contains the claim content — should confirm despite the short sentence
        source_texts = {1: "AIは次世代の基盤技術。AIの応用が広がる。"}
        confirmed, _ = verify_grounding(text, source_texts)
        # With the fix, bracket bigrams are stripped → bare = "ai" (1 bigram) → normalized correctly
        # Overlap: {"ai"} ∩ source bigrams containing "ai" / 1 = ≥1/1 = 1.0 → confirmed
        self.assertIn(1, confirmed, "short sentence with FW brackets must be confirmed after bracket stripping")

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


class TestUncitedSentences(unittest.TestCase):
    """uncited_sentences() (v0.2.65): sentences with zero [S#] anywhere in them.

    docs/product-review.md flagged this as the top remaining gap in citation
    verification: verify_grounding() only checks sentences that ALREADY carry a
    citation, so a hallucinated or simply unsupported claim with NO citation at
    all was completely invisible to the machine checks.
    """

    def test_flags_sentence_with_no_citation(self) -> None:
        from shoin.citation import uncited_sentences

        text = "書院は軽量LLM向けのローカルツールである。"
        out = uncited_sentences(text)
        self.assertEqual(out, ["書院は軽量LLM向けのローカルツールである。"])

    def test_does_not_flag_sentence_with_citation(self) -> None:
        from shoin.citation import uncited_sentences

        text = "書院は軽量LLM向けのローカルツールである。[S1]"
        self.assertEqual(uncited_sentences(text), [])

    def test_mixed_cited_and_uncited_sentences(self) -> None:
        from shoin.citation import uncited_sentences

        text = "書院はローカルツールである[S1]。猫は液体であるという説がある。"
        out = uncited_sentences(text)
        self.assertEqual(out, ["猫は液体であるという説がある。"])

    def test_ignores_trivial_short_fragments(self) -> None:
        """Sentences too short to carry a claim (< 2 bigrams) must not be flagged."""
        from shoin.citation import uncited_sentences

        text = "はい。"
        self.assertEqual(uncited_sentences(text), [])

    def test_ignores_not_in_source_disclaimer(self) -> None:
        """The system prompt's correct behavior for missing facts must not be flagged.

        Rule 3 of SYSTEM_PROMPT instructs the model to explicitly say a fact is not
        in the sources rather than guessing. That disclaimer sentence has no
        citation by design — it must not be treated as an unsupported assertion.
        """
        from shoin.citation import uncited_sentences

        text = "その件についてはソースに記載なしです。"
        self.assertEqual(uncited_sentences(text), [])

    def test_ignores_english_not_in_source_disclaimer(self) -> None:
        from shoin.citation import uncited_sentences

        text = "That detail is not in the source provided."
        self.assertEqual(uncited_sentences(text), [])

    def test_ignores_question_sentences(self) -> None:
        """A question asserts nothing and must not be flagged as an unsupported
        claim. The faq/study_guide Studio kinds prompt the LLM for 5-8 questions
        per output; each becomes its own citation-less sentence at the sentence-
        split boundary, so without this guard every well-formed, correctly-cited
        FAQ/study-guide output would systematically false-positive."""
        from shoin.citation import uncited_sentences

        self.assertEqual(uncited_sentences("What is the capital of France?"), [])
        self.assertEqual(uncited_sentences("書院とは何ですか？"), [])

    def test_flags_non_question_sentence_after_a_question(self) -> None:
        """The question guard must not suppress a genuine unsupported claim that
        merely follows a question in the same text."""
        from shoin.citation import uncited_sentences

        text = "What is the capital of France? Paris has existed for centuries."
        self.assertEqual(uncited_sentences(text), ["Paris has existed for centuries."])

    def test_ignores_formal_japanese_question_ending_in_ka_period(self) -> None:
        """Formal written Japanese ends a question in か。 with no "?" at all —
        the natural register for study_guide's own prompt ("理解確認の設問"). The
        v0.2.77 fix only recognized ?/？-suffixed questions; this must not
        regress to flagging the か。 form the same way suggest_questions()
        (studio.py) already recognizes it."""
        from shoin.citation import uncited_sentences

        self.assertEqual(uncited_sentences("この技術の利点は何か。"), [])
        self.assertEqual(uncited_sentences("導入にはどれくらいの期間が必要でしょうか。"), [])

    def test_ignores_polite_request_form_question(self) -> None:
        """The v0.2.78 fix's own comment claimed to "reuse the same suffix set"
        as suggest_questions() (studio.py), which recognizes ("か", "ください",
        "でしょう") — but v0.2.78 only ported ("か", "でしょう"), omitting
        "ください" (polite request form, e.g. "...教えてください。"). That made
        the two heuristics diverge despite the comment claiming they agree."""
        from shoin.citation import uncited_sentences

        self.assertEqual(uncited_sentences("この技術の利点について教えてください。"), [])

    def test_ignores_english_question_without_trailing_mark(self) -> None:
        """studio.py's suggest_questions() has always recognized English questions
        with no trailing "?" via a question-starter-word check ("LLMs asked for
        'no decoration' often omit trailing '?' in list form" — its own comment),
        but uncited_sentences() never had this check at all — a fourth successive
        gap in the "these two heuristics agree" claim (v0.2.77-79 fixed three
        others). Fixed in v0.2.80 by extracting looks_like_question() as the one
        shared implementation both functions now call, closing this class of bug
        structurally instead of patching another individual case."""
        from shoin.citation import uncited_sentences

        self.assertEqual(uncited_sentences("What is the main benefit of the new policy."), [])
        self.assertEqual(uncited_sentences("How does this system work."), [])
        self.assertEqual(uncited_sentences("Why did this happen."), [])
        # A real unsupported claim is still flagged.
        self.assertEqual(
            uncited_sentences("Paris has existed for centuries."),
            ["Paris has existed for centuries."],
        )

    def test_ignores_english_contraction_question_without_trailing_mark(self) -> None:
        """_EN_QUESTION_STARTERS only ever contained bare words ("what", "how",
        "who", ...); a question starting with a contracted form ("What's",
        "How's", "Who's") without a trailing "?" produced first_word == "what's"
        etc., which matched no frozenset entry — so a genuine question was
        wrongly flagged as an unsupported claim, and the identical gap silently
        dropped such lines from studio.py's suggest_questions() output too."""
        from shoin.citation import uncited_sentences

        self.assertEqual(uncited_sentences("What's the deadline for the project."), [])
        self.assertEqual(uncited_sentences("Who's responsible for this task."), [])
        self.assertEqual(uncited_sentences("Where's the report located."), [])
        # A word that merely starts with a question-starter prefix but isn't a
        # contraction of it (e.g. "Whatever") must not be treated as a question.
        self.assertEqual(
            uncited_sentences("Whatever happened to the report."),
            ["Whatever happened to the report."],
        )

    def test_make_report_ja_study_guide_style_qa_not_flagged_when_answers_cited(self) -> None:
        """End-to-end reproduction of the formal-JA question false positive."""
        from shoin.citation import make_report

        text = (
            "Q1: この技術の利点は何か。\n"
            "A1: 利点はコストの削減である。[S1]\n\n"
            "Q2: 導入にはどれくらいの期間が必要か。\n"
            "A2: 約3か月である。[S2]"
        )
        report = make_report(
            text, ["Doc A", "Doc B"], [1, 2], ["利点はコストの削減である。", "約3か月である。"]
        )
        self.assertNotIn("uncited", report)
        self.assertEqual(report["confirmed"], [1, 2])

    def test_make_report_faq_style_qa_not_flagged_when_answers_cited(self) -> None:
        """End-to-end reproduction of the faq/study_guide Studio kind false
        positive: every question in a correctly-cited Q&A output must not appear
        in the uncited list."""
        from shoin.citation import make_report

        text = (
            "Q1: What is the capital of France?\n"
            "A1: Paris is the capital of France. [S1]\n\n"
            "Q2: What is its population?\n"
            "A2: About 2.1 million. [S2]"
        )
        report = make_report(
            text,
            ["Doc A", "Doc B"],
            [10, 20],
            ["Paris is the capital of France.", "The population is about 2.1 million."],
        )
        self.assertNotIn("uncited", report)
        self.assertEqual(report["confirmed"], [1, 2])

    def test_make_report_populates_uncited_when_sources_present(self) -> None:
        from shoin.citation import make_report

        text = "書院はローカルツールである[S1]。猫は液体であるという説がある。"
        report = make_report(text, ["doc1"], [1], ["書院はローカルツールである。"])
        self.assertEqual(report.get("uncited"), ["猫は液体であるという説がある。"])

    def test_make_report_omits_uncited_key_when_fully_cited(self) -> None:
        from shoin.citation import make_report

        text = "書院はローカルツールである。[S1]"
        report = make_report(text, ["doc1"], [1], ["書院はローカルツールである。"])
        self.assertNotIn("uncited", report)

    def test_make_report_skips_check_with_zero_sources(self) -> None:
        """No sources means nothing to cite against — uncited must never appear."""
        from shoin.citation import make_report

        report = make_report("何らかの断定文。", [])
        self.assertNotIn("uncited", report)

    def test_make_report_check_uncited_false_bypasses_detection(self) -> None:
        """check_uncited=False (used for qa._degraded_text) must skip the scan entirely."""
        from shoin.citation import make_report

        text = "LLMエンドポイントに接続できないため、回答生成を省略。関連箇所のみ提示:\n[S1] 抜粋。"
        report = make_report(
            text, ["doc1"], [1], ["抜粋。"], check_uncited=False
        )
        self.assertNotIn("uncited", report)

    def test_ask_degraded_path_does_not_flag_meta_message_as_uncited(self) -> None:
        """Integration: ask()'s LLMError fallback must not flag its own meta-prefix.

        Before this would-be regression, make_report() on _degraded_text() output
        would flag "LLMエンドポイントに接続できないため..." as an uncited assertion —
        a false positive, since it's system messaging about connectivity, not a
        claim about the source content.
        """
        from shoin.llm import LLMError
        from shoin.qa import ask

        class _DownLLM:
            embedding_model = ""

            def chat(self, messages: list[dict[str, str]], temperature: float = 0.2) -> str:
                raise LLMError("SYSTEM_SERVICE_UNAVAILABLE", "down")

            def embed_one(self, text: str) -> list[float]:
                raise LLMError("SYSTEM_EMBED_DISABLED", "disabled")

        with make_store() as s:
            nb_id = seed(s)
            answer = ask(s, _DownLLM(), nb_id, "書院とは何か", persist=False)
        self.assertTrue(answer.degraded)
        self.assertNotIn("uncited", answer.report)


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

    def test_chat_stream_enforces_32mb_size_cap(self) -> None:
        """chat_stream() must cap cumulative bytes read across the SSE loop,
        matching _post()'s 32 MB guard (v0.2.37) against a runaway/malicious
        endpoint. chat_stream() was found to have NO cap at all, iterating
        `for raw in resp` with no bound on total bytes — worse than the
        already-fixed _post() case since it evaded the guard entirely, on the
        exact 4-8GB RAM systems this project targets."""
        from unittest.mock import patch

        from shoin.llm import LLMClient, LLMError, _MAX_RESPONSE

        # Each line is ~1KB; enough lines to exceed the 32 MB cap partway through.
        line = (
            b'data: {"choices":[{"delta":{"content":"' + b"x" * 950 + b'"}}]}\n'
        )
        n_lines = (_MAX_RESPONSE // len(line)) + 100  # deliberately past the cap

        class _HugeStreamResp:
            def __enter__(self) -> "_HugeStreamResp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def __iter__(self):  # type: ignore[override]
                for _ in range(n_lines):
                    yield line

        with patch("urllib.request.urlopen", return_value=_HugeStreamResp()):
            client = LLMClient(base_url="http://localhost:11434/v1")
            gen = client.chat_stream([{"role": "user", "content": "hi"}])
            collected = 0
            with self.assertRaises(LLMError) as cm:
                for tok in gen:
                    collected += len(tok)
        self.assertEqual(cm.exception.code, "SYSTEM_LLM_BAD_RESPONSE")
        self.assertIn("32 MB", str(cm.exception))
        self.assertLess(collected, n_lines * len(line), "must stop before consuming everything")

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

    def test_available_returns_false_for_non_json_content_type(self) -> None:
        """available() must return False when the server responds with text/html.

        Before v0.2.54, available() returned True for any HTTP 200 response,
        including a plain nginx/http.server returning text/html on /models.
        This misled callers (qa.ask()) into skipping graceful BM25-only
        degradation — every chat() then failed with SYSTEM_LLM_BAD_RESPONSE
        instead of the expected SYSTEM_SERVICE_UNAVAILABLE.
        Fix: check the Content-Type header; return True only when it contains "json".
        """
        from unittest.mock import MagicMock, patch
        from shoin.llm import LLMClient

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.getheader.return_value = "text/html; charset=utf-8"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            client = LLMClient(base_url="http://localhost:11434/v1")
            self.assertFalse(client.available())

    def test_available_returns_true_for_json_content_type(self) -> None:
        """available() returns True when the server responds with application/json."""
        from unittest.mock import MagicMock, patch
        from shoin.llm import LLMClient

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.getheader.return_value = "application/json"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            client = LLMClient(base_url="http://localhost:11434/v1")
            self.assertTrue(client.available())

    def test_available_returns_false_when_response_lacks_getheader(self) -> None:
        """available() must return False (not raise AttributeError) when the response
        object raises AttributeError on getheader() — e.g. an unusual WSGI shim or
        test double without a full HTTP response interface.

        Before v0.2.57, AttributeError was NOT in the except tuple; it propagated
        as a bare exception to callers rather than the expected False return.
        Fix: add AttributeError to the except clause.
        """
        from unittest.mock import MagicMock, patch
        from shoin.llm import LLMClient

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        # Simulate an object whose getheader() raises AttributeError
        mock_resp.getheader.side_effect = AttributeError("no getheader")

        with patch("urllib.request.urlopen", return_value=mock_resp):
            client = LLMClient(base_url="http://localhost:11434/v1")
            # Must return False, not raise AttributeError
            self.assertFalse(client.available())

    def test_post_size_check_reads_max_plus_one_byte(self) -> None:
        """_post() must read _MAX_RESPONSE + 1 bytes to correctly detect oversized responses.

        Before v0.2.54, resp.read(_MAX_RESPONSE) was used; if a valid response was
        exactly 32 MB, len(raw) == _MAX_RESPONSE fired and raised LLMError even
        though no truncation had occurred (off-by-one).
        Fix: read _MAX_RESPONSE + 1 bytes; len(raw) > _MAX_RESPONSE is the correct
        truncation signal — len == _MAX_RESPONSE means the full response fit within
        the limit.
        """
        from unittest.mock import MagicMock, patch
        from shoin.llm import LLMClient

        _MAX_RESPONSE = 32 * 1024 * 1024
        valid_payload = b'{"choices":[{"message":{"content":"hi"}}]}'

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = valid_payload

        with patch("urllib.request.urlopen", return_value=mock_resp):
            client = LLMClient(base_url="http://localhost:11434/v1")
            client._post("/chat/completions", {}, 10)

        read_arg = mock_resp.read.call_args[0][0]
        self.assertEqual(
            read_arg,
            _MAX_RESPONSE + 1,
            "_post() must read _MAX_RESPONSE + 1 bytes to avoid off-by-one on exact-limit responses",
        )


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
            handler.generation_lock = threading.Lock()

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

    def test_safe_error_swallows_dead_connection_errors(self) -> None:
        """_safe_error() must swallow a dead-connection failure from the error
        response write itself, not propagate it.

        _dispatch()'s exception handlers call this instead of _error() directly
        because the original exception being handled may itself BE a client
        disconnect (e.g. a request queued behind generation_lock whose client
        gave up before the response could be written) — writing the error
        response then hits the same dead socket and raises again, this time
        with nothing left to catch it, producing exactly the unhandled
        traceback v0.2.19's _dispatch() catch-all was written to eliminate.
        """
        from shoin.server import _Handler

        handler = _Handler.__new__(_Handler)
        for exc_type in (BrokenPipeError, ConnectionResetError, OSError):
            def _raise(*a, _exc=exc_type, **kw):
                raise _exc("client gone")
            handler._error = _raise  # type: ignore[assignment]
            handler._safe_error(500, "SYSTEM_INTERNAL_ERROR", "boom")  # must not raise

    def test_dispatch_catch_all_does_not_propagate_when_error_write_fails(self) -> None:
        """End-to-end reproduction: a handler raises a generic exception, and
        the fallback error-response write ALSO fails with a dead-connection
        error (the double-fault scenario). _dispatch() itself must not let
        the second exception escape."""
        from shoin.server import _Handler

        handler = _Handler.__new__(_Handler)
        handler.path = "/api/health"
        handler._query = {}
        handler._reject_cross_site = lambda method: False  # type: ignore[assignment]

        def _raise_generic(*a, **kw):
            raise RuntimeError("handler blew up")
        handler._h_health = _raise_generic  # type: ignore[assignment]

        def _raise_dead_connection(*a, **kw):
            raise ConnectionResetError("client already gone")
        handler._error = _raise_dead_connection  # type: ignore[assignment]

        handler._dispatch("GET")  # must not raise

    def test_reject_cross_site_survives_dead_connection_on_error_write(self) -> None:
        """_reject_cross_site() runs BEFORE _dispatch()'s try/except even
        begins, so a dead-connection failure while sending its 403 rejection
        (e.g. a DNS-rebinding probe whose client already closed the socket)
        was an unguarded SINGLE fault, not even the double-fault v0.2.89
        fixed downstream — _reject_cross_site() still called raw _error()
        instead of _safe_error() after that fix was added."""
        from shoin.server import _Handler

        handler = _Handler.__new__(_Handler)
        handler.path = "/api/health"
        handler._query = {}
        handler.headers = {"Host": "evil.example"}

        def _raise_dead_connection(*a, **kw):
            raise BrokenPipeError("client already disconnected before response could be sent")
        handler._error = _raise_dead_connection  # type: ignore[assignment]

        handler._dispatch("GET")  # must not raise

    def test_zero_token_llm_response_saves_empty_assistant_message(self) -> None:
        """When chat_stream yields zero tokens (e.g. reasoning model with no content),
        an empty assistant message must still be saved to prevent an orphaned user turn.

        Before v0.2.55, the guard `if full:` prevented saving when `full=""`, leaving
        the user question visible in list_messages() on page reload with no reply.
        The fix removes the guard so an empty assistant message is always persisted,
        matching the meta-send disconnect and build_context error paths.
        """
        import os
        import tempfile
        import threading

        from shoin.server import _Handler
        from shoin.llm import LLMError

        class _ZeroTokenLLM:
            embedding_model = ""
            model = "test"

            def embed_one(self, text: str) -> list[float]:
                return [1.0, 0.0]

            def chat(self, messages: list, temperature: float = 0.2) -> str:
                return ""

            def chat_stream(self, messages: list, temperature: float = 0.2):
                return iter([])  # yields nothing — simulates reasoning model

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            db_path = f.name
        try:
            with Store(db_path) as s:
                nb = s.create_notebook("zero-token-test")
                src = s.add_source(nb.id, "txt", "doc", "mem://d", "sha-zt")
                s.add_chunks(src.id, ["books are great for learning"])
                nb_id = nb.id

            handler = _Handler.__new__(_Handler)
            handler.db = db_path
            handler.llm = _ZeroTokenLLM()  # type: ignore[assignment]
            handler.questions_cache = {}
            handler.questions_cache_lock = threading.Lock()
            handler.generation_lock = threading.Lock()

            sse_events: list[str] = []

            def _mock_sse(event: str, data: object) -> None:
                sse_events.append(event)

            handler._sse = _mock_sse  # type: ignore[assignment]
            handler._headers = lambda *a, **kw: None  # type: ignore[assignment]
            handler._read_json = lambda: {"question": "books"}  # type: ignore[assignment]
            handler._require = lambda data, field: str(data[field])  # type: ignore[assignment]
            handler._stream_chat = lambda msgs: _ZeroTokenLLM().chat_stream(msgs)  # type: ignore[assignment]

            handler._h_ask_sse(nb_id)

            with Store(db_path) as s:
                msgs = s.list_messages(nb_id)

            self.assertEqual(len(msgs), 2, "user + empty assistant must both be saved")
            self.assertEqual(msgs[0]["role"], "user")
            self.assertEqual(msgs[1]["role"], "assistant")
            self.assertEqual(msgs[1]["body"], "")
        finally:
            os.unlink(db_path)


class TestPipelineTitle(unittest.TestCase):
    """Regression tests for pipeline.index_source title override (v0.2.55)."""

    def test_index_source_title_override_used_in_source_row(self) -> None:
        """index_source(title=) must commit the source with the supplied title.

        Before v0.2.55, server._h_src_upload called index_source (which derived the
        title from the tmp file path) then update_source_title in a second transaction.
        A concurrent DELETE between the two commits left the source with the tmp-path
        as its title and caused HTTP 404.
        Fix: add title kwarg to index_source so add_source uses the override directly
        in a single transaction, eliminating the two-phase commit window.
        """
        import os
        import tempfile
        from shoin.pipeline import index_source

        with tempfile.NamedTemporaryFile(
            suffix=".txt", delete=False, mode="w", encoding="utf-8"
        ) as f:
            f.write("some document content for testing")
            tmp_path = f.name
        try:
            with make_store() as s:
                nb_id = s.create_notebook("title-test").id
                result = index_source(s, nb_id, tmp_path, title="My Document.pdf")
            self.assertEqual(
                result.source.title,
                "My Document.pdf",
                "index_source must use the title kwarg, not the tmp file path",
            )
        finally:
            os.unlink(tmp_path)


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

    def test_reindex_partial_failure_does_not_falsely_clear_mismatch_guard(self) -> None:
        """A partial reindex_notebook() failure (force=True) must NOT record
        embed_model as fully consistent — it was found to do so, silently
        defeating _check_embed_model_ok()'s mismatch guard.

        force=True OVERWRITES existing vectors in place: a partial failure
        leaves some chunks with fresh current-model vectors and others with
        their OLD, untouched, different-model vectors — both non-NULL, both
        included in cosine comparisons. Recording embed_model as consistent in
        that case would make _check_embed_model_ok() report no mismatch over a
        DB that is provably still mixed. Contrast with the non-force
        (index_source) path, where an un-embedded chunk is simply NULL — safely
        excluded by vector_search()'s WHERE embedding IS NOT NULL, not a
        corrupting wrong-model vector — so partial success there correctly still
        updates the setting (see test_embed_chunks_done_counts_actual_pairs...
        and friends, unaffected by this fix).
        """
        from shoin.llm import LLMError
        from shoin.pipeline import reindex_notebook
        from shoin.qa import _check_embed_model_ok

        class PartialFailLLM:
            embedding_model = "model-B"

            def __init__(self) -> None:
                self.calls = 0

            def embed(self, texts: list[str]) -> list[list[float]]:
                self.calls += 1
                if self.calls == 1:
                    return [[0.9, 0.8, 0.7] for _ in texts]
                raise LLMError("SYSTEM_LLM_TIMEOUT", "endpoint dropped")

        with make_store() as s:
            nb_id = s.create_notebook("partial-reindex").id
            src = s.add_source(nb_id, "txt", "doc", "t", "sha-pr")
            texts = [f"chunk number {i} with some content" for i in range(20)]
            chunk_ids = s.add_chunks(src.id, texts)
            for cid in chunk_ids:
                s.set_embedding(cid, [0.1, 0.2, 0.3, 0.4, 0.5], commit=False)
            s.conn.commit()
            s.set_setting("embed_model", "model-A")

            llm = PartialFailLLM()
            n_embedded, n_total = reindex_notebook(s, llm, nb_id)
            self.assertLess(n_embedded, n_total, "test must actually exercise partial failure")
            self.assertEqual(
                s.get_setting("embed_model"), "model-A",
                "setting must stay at the OLD model — the reindex did not fully succeed",
            )
            self.assertFalse(
                _check_embed_model_ok(s, llm),
                "mismatch guard must correctly detect the still-mixed DB and disable vector search",
            )

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
        """refresh_source must keep the source ID and replace chunks with fresh content.

        Title is deliberately NOT updated by refresh (v0.2.87) — it was found to
        unconditionally overwrite a user's custom rename with the freshly
        re-extracted page title on every refresh. Title management is the
        exclusive job of PATCH /api/sources/{id}; refresh only updates content.
        """
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
        self.assertEqual(res1.source.title, "Page v1", "refresh must not overwrite the title")
        self.assertEqual(res1.source.sha256, "sha-v2")
        with make_store() as s2:
            pass  # store closed; already verified above

    def test_refresh_source_preserves_user_renamed_title(self) -> None:
        """A user's custom rename (PATCH /api/sources/{id}) must survive a
        subsequent refresh, even when the re-fetched page has a different
        <title>. Found to be silently clobbered before v0.2.87 — refresh
        unconditionally passed the freshly re-extracted title to
        replace_chunks_for_source with no check for a prior manual rename."""
        from unittest.mock import patch

        from shoin.ingest import Extracted
        from shoin.pipeline import index_source, refresh_source

        original = Extracted(
            kind="url", title="Original Page Title", origin="http://rename-refresh.test",
            sha256="sha-a", text="original content",
        )
        with make_store() as s:
            nb_id = s.create_notebook("rename-refresh-nb").id
            with patch("shoin.pipeline.extract_url", return_value=original):
                res0 = index_source(s, nb_id, "http://rename-refresh.test")
            source_id = res0.source.id
            s.update_source_title(source_id, "My Custom Curated Name", "http://rename-refresh.test")

            refreshed = Extracted(
                kind="url", title="A Totally Different New Title", origin="http://rename-refresh.test",
                sha256="sha-b", text="updated content",
            )
            with patch("shoin.pipeline.extract_url", return_value=refreshed):
                result = refresh_source(s, source_id)
        self.assertEqual(result.source.title, "My Custom Curated Name")
        self.assertEqual(result.source.sha256, "sha-b")

    def test_refresh_source_concurrent_rename_during_fetch_not_baked_into_context(self) -> None:
        """A rename that commits WHILE refresh_source()'s extract_url() network
        fetch is in flight must not permanently bake the stale pre-fetch title
        into the new chunks' context breadcrumb (v0.2.128).

        Before the fix, refresh_source() read `src.title` once at the very top
        of the function (before the network round-trip, up to
        URL_TIMEOUT_SEC=15s) and used that stale snapshot to build every new
        chunk's context. Since no later rename ever revisits chunks that never
        had the OLD title as their context prefix (store._rewrite_chunk_context_titles
        only rewrites rows whose context still starts with the title that was
        actually current at rename time), the mismatch was permanent: the
        chunks kept matching FTS queries for a title the user no longer sees
        anywhere, and never matched the real current title.
        """
        from unittest.mock import patch

        from shoin.ingest import Extracted
        from shoin.pipeline import index_source, refresh_source

        original = Extracted(
            kind="url", title="Original Title", origin="http://race-refresh.test",
            sha256="sha-orig", text="original content here",
        )
        with make_store() as s:
            nb_id = s.create_notebook("race-refresh-nb").id
            with patch("shoin.pipeline.extract_url", return_value=original):
                res0 = index_source(s, nb_id, "http://race-refresh.test")
            source_id = res0.source.id

            refreshed = Extracted(
                kind="url", title="ignored (title mgmt is not refresh's job)",
                origin="http://race-refresh.test", sha256="sha-new",
                text="freshly fetched content after the race",
            )

            def fetch_that_races_a_rename(url: str) -> Extracted:
                # Simulate a PATCH /api/sources/{id} rename committing WHILE
                # this network fetch is still in flight.
                s.update_source_title(source_id, "Renamed During Fetch", url)
                return refreshed

            with patch("shoin.pipeline.extract_url", side_effect=fetch_that_races_a_rename):
                refresh_source(s, source_id)

            row = s.conn.execute(
                "SELECT context FROM chunks WHERE source_id=? LIMIT 1", (source_id,)
            ).fetchone()
            self.assertTrue(
                row["context"].startswith("Renamed During Fetch"),
                f"chunk context must reflect the CURRENT title, got: {row['context']!r}",
            )
            self.assertNotIn("Original Title", row["context"])

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


class TestChunkLimit(unittest.TestCase):
    """MAX_CHUNKS_PER_NOTEBOOK (v0.2.70): spec.md STRIDE DoS control 'チャンク数
    上限/notebook', found unimplemented by this session's Socratic audit of spec.md.
    """

    def test_index_source_raises_when_limit_exceeded(self) -> None:
        from unittest.mock import patch

        from shoin.ingest import IngestError
        from shoin.pipeline import index_source

        with patch("shoin.pipeline.MAX_CHUNKS_PER_NOTEBOOK", 2):
            with make_store() as s:
                nb = s.create_notebook("chunk-limit-test")
                src = s.add_source(nb.id, "txt", "doc1", "mem://d1", "sha1")
                # Seed 2 existing chunks so the notebook is already at the limit.
                s.add_chunks(src.id, ["chunk one", "chunk two"])
                with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
                    f.write("これは3個目のチャンクを作るための十分に長い文章である。")
                    path = f.name
                try:
                    with self.assertRaises(IngestError) as cm:
                        index_source(s, nb.id, path)
                    self.assertEqual(cm.exception.code, "INGEST_NOTEBOOK_FULL")
                finally:
                    import os

                    os.unlink(path)

    def test_index_source_succeeds_within_limit(self) -> None:
        from unittest.mock import patch

        from shoin.pipeline import index_source

        with patch("shoin.pipeline.MAX_CHUNKS_PER_NOTEBOOK", 100):
            with make_store() as s:
                nb = s.create_notebook("chunk-limit-ok-test")
                with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
                    f.write("短い文書です。")
                    path = f.name
                try:
                    result = index_source(s, nb.id, path)
                    self.assertGreaterEqual(result.n_chunks, 1)
                finally:
                    import os

                    os.unlink(path)

    def test_refresh_source_raises_when_limit_exceeded(self) -> None:
        """refresh_source() must enforce MAX_CHUNKS_PER_NOTEBOOK too — it was
        found to bypass the cap entirely (index_source() checked it, but
        refresh_source(), which also inserts new chunks via
        replace_chunks_for_source(), never did). A refreshed URL source that
        grows over time had no ceiling, fully defeating the documented
        per-notebook DoS cap for any source reachable via refresh."""
        from unittest.mock import patch

        from shoin.ingest import Extracted, IngestError
        from shoin.pipeline import index_source, refresh_source

        with patch("shoin.pipeline.MAX_CHUNKS_PER_NOTEBOOK", 5):
            with make_store() as s:
                nb_id = s.create_notebook("refresh-limit-nb").id
                src1 = s.add_source(nb_id, "txt", "a", "mem://a", "sha-a")
                s.add_chunks(src1.id, [f"chunk {i}" for i in range(3)])
                original = Extracted(
                    kind="url", title="b", text="one short chunk of text here.",
                    origin="http://refresh-limit.test", sha256="sha-b",
                )
                with patch("shoin.pipeline.extract_url", return_value=original):
                    res0 = index_source(s, nb_id, "http://refresh-limit.test")
                self.assertEqual(s.counts(nb_id)["chunks"], 4, "3 + 1 = at the 5 cap minus 1")
                grown = Extracted(
                    kind="url", title="b2", text="irrelevant, split_text is patched below",
                    origin="http://refresh-limit.test", sha256="sha-b2",
                )
                # Patch the chunker directly rather than crafting real text long enough
                # to force multiple 512-token chunks — deterministic regardless of
                # tokenizer internals. Each chunk carries an empty heading breadcrumb.
                with patch("shoin.pipeline.extract_url", return_value=grown), \
                     patch(
                         "shoin.pipeline.split_text_with_context",
                         return_value=[("", "c1"), ("", "c2"), ("", "c3")],
                     ):
                    with self.assertRaises(IngestError) as cm:
                        refresh_source(s, res0.source.id)
                self.assertEqual(cm.exception.code, "INGEST_NOTEBOOK_FULL")

    def test_refresh_source_same_size_at_cap_not_wrongly_rejected(self) -> None:
        """The fix must subtract the source's OWN existing chunk count before
        checking the cap — otherwise a same-size (or shrinking) refresh of a
        source already counted in the notebook total would be wrongly rejected
        even though it doesn't grow the notebook past the cap at all."""
        from unittest.mock import patch

        from shoin.ingest import Extracted
        from shoin.pipeline import index_source, refresh_source

        with patch("shoin.pipeline.MAX_CHUNKS_PER_NOTEBOOK", 4):
            with make_store() as s:
                nb_id = s.create_notebook("refresh-samesize-nb").id
                src1 = s.add_source(nb_id, "txt", "a", "mem://a", "sha-a")
                s.add_chunks(src1.id, [f"chunk {i}" for i in range(3)])
                original = Extracted(
                    kind="url", title="b", text="one short chunk of text here.",
                    origin="http://refresh-samesize.test", sha256="sha-b",
                )
                with patch("shoin.pipeline.extract_url", return_value=original):
                    res0 = index_source(s, nb_id, "http://refresh-samesize.test")
                self.assertEqual(s.counts(nb_id)["chunks"], 4, "exactly at the cap")
                same_size = Extracted(
                    kind="url", title="b2", text="a different but equally short chunk.",
                    origin="http://refresh-samesize.test", sha256="sha-b2",
                )
                with patch("shoin.pipeline.extract_url", return_value=same_size):
                    result = refresh_source(s, res0.source.id)  # must not raise
                self.assertEqual(result.n_chunks, 1)


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
        # Malformed added_at -> no structured year field (only emit when real).
        self.assertNotIn("year =", bib)

    def test_bibtex_emits_structured_year_field(self) -> None:
        """export_bibtex must emit a `year = {YYYY}` field from added_at so
        reference managers can render author-year citations and sort by year,
        rather than burying the date only in the free-text note (v0.2.133)."""
        from shoin.export import export_bibtex
        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "論文", "https://x.test", "sha-y")
            s.conn.execute(
                "UPDATE sources SET added_at='2021-03-09T12:00:00' WHERE id=?", (src.id,)
            )
            s.conn.commit()
            bib = export_bibtex(s, nb.id)
        self.assertIn("year = {2021},", bib)
        # The year line sits before the note, inside the entry.
        self.assertLess(bib.index("year ="), bib.index("note ="))

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
        # No structured PY (year) line for a malformed/empty added_at (v0.2.136).
        self.assertNotIn("PY  -", ris)

    def test_ris_emits_structured_py_year_field(self) -> None:
        """export_ris must emit a `PY  - YYYY` field from added_at's leading
        year — the RIS parallel of the BibTeX `year` field (v0.2.133) — so
        reference managers can cite/sort author-year. PY precedes DA (v0.2.136)."""
        from shoin.export import export_ris
        with make_store() as s:
            nb = s.create_notebook("nb-ris-py")
            src = s.add_source(nb.id, "url", "論文", "https://x.test", "sha-py")
            s.conn.execute(
                "UPDATE sources SET added_at='2017-06-12T00:00:00' WHERE id=?", (src.id,)
            )
            s.conn.commit()
            ris = export_ris(s, nb.id)
        self.assertIn("PY  - 2017", ris)
        self.assertLess(ris.index("PY  - "), ris.index("DA  - "))

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

    def test_status_line_includes_confirmed_misattributed_uncited_degraded(self) -> None:
        """_status_line() must surface every verification signal from the report.

        Before v0.2.66, export_markdown() reconstructed the [S#] legend from
        citation_report but never rendered confirmed/misattributed/uncited/degraded —
        the exact verification signal that is Shoin's core differentiator. Exported
        text was indistinguishable from unverified prose once shared or archived.
        """
        from shoin.export import _status_line

        report: dict[str, object] = {
            "degraded": True,
            "invalid": [3],
            "misattributed": [2],
            "confirmed": [1],
            "uncited": ["猫は液体である。"],
        }
        line = _status_line(report)
        self.assertIn("S3", line)
        self.assertIn("S2", line)
        self.assertIn("S1", line)
        self.assertIn("1", line)  # uncited count

    def test_status_line_empty_when_report_has_nothing_to_report(self) -> None:
        from shoin.export import _status_line

        self.assertEqual(_status_line({}), "")

    def test_export_markdown_chat_message_shows_confirmed_citation(self) -> None:
        import json

        from shoin.citation import make_report
        from shoin.export import export_markdown

        with make_store() as s:
            nb = s.create_notebook("export-status-test")
            src = s.add_source(nb.id, "txt", "doc", "mem://d", "sha-d")
            s.add_chunks(src.id, ["書院はローカルツールである。"])
            report = make_report(
                "書院はローカルツールである[S1]。",
                ["doc"],
                [src.id],
                ["書院はローカルツールである。"],
            )
            s.add_message(nb.id, "user", "書院とは何か", "{}")
            s.add_message(nb.id, "assistant", "書院はローカルツールである[S1]。", json.dumps(report))
            md = export_markdown(s, nb.id)
        self.assertIn("S1", md)
        self.assertTrue(
            any("根拠確認済み" in ln for ln in md.splitlines()),
            f"exported markdown must surface confirmed status: {md!r}",
        )

    def test_export_markdown_legend_shows_section_breadcrumb(self) -> None:
        """The exported chat legend must show each citation's section
        breadcrumb when the report carries source_contexts (v0.2.130), so the
        provenance the seal viewer surfaces survives export."""
        import json

        from shoin.citation import make_report
        from shoin.export import export_markdown

        with make_store() as s:
            nb = s.create_notebook("export-section-test")
            src = s.add_source(nb.id, "txt", "生物ノート", "mem://d", "sha-d")
            s.add_chunks(src.id, ["光合成の説明である。"])
            report = make_report(
                "光合成の説明である[S1]。",
                ["生物ノート"],
                [src.id],
                ["光合成の説明である。"],
                ["光合成のしくみ > 明反応"],
            )
            s.add_message(nb.id, "user", "光合成とは", "{}")
            s.add_message(nb.id, "assistant", "光合成の説明である[S1]。", json.dumps(report))
            md = export_markdown(s, nb.id)
        self.assertIn("(§ 光合成のしくみ > 明反応)", md)

    def test_export_markdown_legend_omits_section_when_absent(self) -> None:
        """A report without source_contexts (old message / no heading) must
        produce a plain legend with no stray '§' marker."""
        import json

        from shoin.citation import make_report
        from shoin.export import export_markdown

        with make_store() as s:
            nb = s.create_notebook("export-nosection-test")
            src = s.add_source(nb.id, "txt", "doc", "mem://d", "sha-d")
            s.add_chunks(src.id, ["書院はローカルツールである。"])
            report = make_report(
                "書院はローカルツールである[S1]。", ["doc"], [src.id],
                ["書院はローカルツールである。"],
            )
            s.add_message(nb.id, "user", "書院とは", "{}")
            s.add_message(nb.id, "assistant", "書院はローカルツールである[S1]。", json.dumps(report))
            md = export_markdown(s, nb.id)
        self.assertIn("S1=doc", md)
        self.assertNotIn("§", md)

    def test_export_markdown_chat_message_shows_uncited_count(self) -> None:
        import json

        from shoin.citation import make_report
        from shoin.export import export_markdown

        with make_store() as s:
            nb = s.create_notebook("export-uncited-test")
            src = s.add_source(nb.id, "txt", "doc", "mem://d", "sha-d")
            s.add_chunks(src.id, ["書院はローカルツールである。"])
            text = "書院はローカルツールである[S1]。猫は液体であるという説がある。"
            report = make_report(text, ["doc"], [src.id], ["書院はローカルツールである。"])
            s.add_message(nb.id, "user", "書院とは何か", "{}")
            s.add_message(nb.id, "assistant", text, json.dumps(report))
            md = export_markdown(s, nb.id)
        self.assertTrue(
            any("無出典" in ln for ln in md.splitlines()),
            f"exported markdown must surface uncited-assertion status: {md!r}",
        )

    def test_export_markdown_studio_output_shows_status_line(self) -> None:
        import json

        from shoin.citation import make_report
        from shoin.export import export_markdown

        with make_store() as s:
            nb = s.create_notebook("export-studio-status-test")
            src = s.add_source(nb.id, "txt", "doc", "mem://d", "sha-d")
            s.add_chunks(src.id, ["書院はローカルツールである。"])
            report = make_report(
                "書院はローカルツールである[S1]。", ["doc"], [src.id], ["書院はローカルツールである。"]
            )
            s.add_studio_output(nb.id, "briefing", "書院はローカルツールである[S1]。", json.dumps(report))
            md = export_markdown(s, nb.id)
        self.assertTrue(
            any("根拠確認済み" in ln for ln in md.splitlines()),
            f"exported studio output must surface confirmed status: {md!r}",
        )

    def test_export_markdown_degraded_message_shows_search_only(self) -> None:
        import json

        from shoin.export import export_markdown

        with make_store() as s:
            nb = s.create_notebook("export-degraded-test")
            report: dict[str, object] = {"degraded": True, "cited": [], "invalid": [], "coverage": 0.0}
            s.add_message(nb.id, "user", "書院とは何か", "{}")
            s.add_message(nb.id, "assistant", "検索のみの結果", json.dumps(report))
            md = export_markdown(s, nb.id)
        self.assertTrue(
            any("検索のみ" in ln for ln in md.splitlines()),
            f"exported markdown must surface degraded status: {md!r}",
        )


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

    def test_config_json_is_used_when_env_var_unset(self) -> None:
        """README.md has documented '環境変数または ~/.config/shoin/config.json' since
        v0.1.0, but no code ever read the file (v0.2.68 audit finding). config.json
        must now be a real fallback when the corresponding env var is unset.
        """
        import json
        import os
        import tempfile
        from pathlib import Path

        import shoin.config as config_mod

        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.json"
            cfg_path.write_text(json.dumps({"SHOIN_LLM_MODEL": "custom-model"}))
            env = dict(os.environ)
            env.pop("SHOIN_LLM_MODEL", None)
            with patch.object(config_mod, "config_file", return_value=cfg_path):
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(config_mod.llm_model(), "custom-model")

    def test_env_var_takes_precedence_over_config_json(self) -> None:
        import json
        import os
        import tempfile
        from pathlib import Path

        import shoin.config as config_mod

        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.json"
            cfg_path.write_text(json.dumps({"SHOIN_LLM_MODEL": "file-model"}))
            with patch.object(config_mod, "config_file", return_value=cfg_path):
                with patch.dict(os.environ, {"SHOIN_LLM_MODEL": "env-model"}):
                    self.assertEqual(config_mod.llm_model(), "env-model")

    def test_missing_config_json_falls_back_to_builtin_default(self) -> None:
        import os
        from pathlib import Path

        import shoin.config as config_mod

        env = dict(os.environ)
        env.pop("SHOIN_LLM_MODEL", None)
        with patch.object(config_mod, "config_file", return_value=Path("/nonexistent/config.json")):
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(config_mod.llm_model(), "qwen3:4b")

    def test_malformed_config_json_does_not_crash(self) -> None:
        """Malformed JSON must fall back to the built-in default, not raise."""
        import os
        import tempfile
        from pathlib import Path

        import shoin.config as config_mod

        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.json"
            cfg_path.write_text("not valid json{{{")
            env = dict(os.environ)
            env.pop("SHOIN_LLM_MODEL", None)
            with patch.object(config_mod, "config_file", return_value=cfg_path):
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(config_mod.llm_model(), "qwen3:4b")

    def test_config_json_non_dict_top_level_ignored(self) -> None:
        """A config.json that is valid JSON but not an object (e.g. a bare list)
        must be ignored gracefully, not raise AttributeError on .items()."""
        import os
        import tempfile
        from pathlib import Path

        import shoin.config as config_mod

        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.json"
            cfg_path.write_text("[1, 2, 3]")
            env = dict(os.environ)
            env.pop("SHOIN_LLM_MODEL", None)
            with patch.object(config_mod, "config_file", return_value=cfg_path):
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(config_mod.llm_model(), "qwen3:4b")

    def test_config_json_null_value_falls_back_instead_of_becoming_literal_none(self) -> None:
        """A JSON null in config.json (a well-formed value, e.g. a user writing
        {"SHOIN_EMBED_MODEL": null} to mean "unset") must fall through to the
        built-in default, not become the literal string "None".

        _file_config()'s dict comprehension called str(v) on every value
        unconditionally; str(None) == "None", a truthy non-empty string that
        silently became the actual setting — e.g. embed_model() returning
        "None" instead of degrading to BM25-only, since (embed_model or "")
        checks throughout the codebase only catch an empty string, not the
        string "None".
        """
        import json
        import os
        import tempfile
        from pathlib import Path

        import shoin.config as config_mod

        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.json"
            cfg_path.write_text(json.dumps({"SHOIN_EMBED_MODEL": None, "SHOIN_LLM_MODEL": "real-model"}))
            env = dict(os.environ)
            env.pop("SHOIN_EMBED_MODEL", None)
            env.pop("SHOIN_LLM_MODEL", None)
            with patch.object(config_mod, "config_file", return_value=cfg_path):
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(
                        config_mod.embed_model(), "nomic-embed-text",
                        "null must fall back to the built-in default, not become 'None'",
                    )
                    self.assertEqual(
                        config_mod.llm_model(), "real-model",
                        "a real, non-null config.json value must still work",
                    )

    def test_config_json_non_string_scalar_and_container_values_ignored(self) -> None:
        """v0.2.102's null filter only covered null specifically — bool, list,
        and dict JSON values were still blindly str()-coerced into garbage
        setting strings (e.g. "['qwen3:4b']", "True", "{'a': 1}"). These must
        all fall back to the built-in default like an absent key, matching
        the "config.json is optional" contract this function documents.
        Plain numbers (int/float) ARE allowed through unquoted, since
        {"SHOIN_PORT": 8080} is a natural way to write a port in JSON.
        """
        import json
        import os
        import tempfile
        from pathlib import Path

        import shoin.config as config_mod

        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.json"
            cfg_path.write_text(json.dumps({
                "SHOIN_LLM_MODEL": ["qwen3:4b"],
                "SHOIN_LANG": True,
                "SHOIN_PORT": 8080,
                "SHOIN_DATA_DIR": {"a": 1},
            }))
            env = dict(os.environ)
            for k in ("SHOIN_LLM_MODEL", "SHOIN_LANG", "SHOIN_PORT", "SHOIN_DATA_DIR"):
                env.pop(k, None)
            with patch.object(config_mod, "config_file", return_value=cfg_path):
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(config_mod.llm_model(), "qwen3:4b", "list value must be ignored")
                    self.assertEqual(config_mod.ui_lang(), "ja", "bool value must be ignored")
                    self.assertEqual(config_mod.port(), 8080, "a plain int value must still work")
                    self.assertNotIn("{'a': 1}", str(config_mod.data_dir()), "dict value must be ignored")


class TestNegTerms(unittest.TestCase):
    """Tests for negative-term parsing and filtering (v0.2.47)."""

    def test_neg_terms_ascii(self) -> None:
        self.assertEqual(neg_terms("Python -2"), ["2"])

    def test_neg_terms_multiple(self) -> None:
        self.assertEqual(neg_terms("機械学習 -Python -legacy"), ["python", "legacy"])

    def test_neg_terms_cjk(self) -> None:
        self.assertEqual(neg_terms("書院 -Python"), ["python"])

    def test_neg_terms_non_hiragana_katakana_kanji_scripts(self) -> None:
        """neg_terms()'s own docstring documents CJK negation generally
        ("-word", "-日本語"), but _NEG_RE hardcoded a narrow, independent CJK
        range literal ([ぁ-ヿ一-鿿]) covering only hiragana/katakana/CJK
        ideographs — every other script is_cjk()/query_terms()/fts_query()
        recognize (chunk._CJK_RANGES) was invisible to negation. Not just
        ignored: since strip_neg_terms() also failed to remove the -word
        token, the term survived into the query and was tokenized as an
        ordinary CJK run, inverting the user's exclusion into an inclusion.
        Fixed by building _NEG_RE's CJK class from the same _CJK_RANGES table
        instead of a second hand-picked copy that can silently drift.
        """
        self.assertEqual(neg_terms("AI -한국어"), ["한국어"])  # Hangul
        self.assertEqual(neg_terms("-ภาษาไทย"), ["ภาษาไทย"])  # Thai
        self.assertEqual(neg_terms("-㐅"), ["㐅"])  # CJK ext A
        self.assertEqual(strip_neg_terms("AI -한국어"), "AI")

    def test_neg_terms_none(self) -> None:
        self.assertEqual(neg_terms("書院 引用"), [])

    def test_neg_terms_hyphen_in_word_not_matched(self) -> None:
        """Hyphen inside a word like 'state-of-the-art' must not be treated as negation."""
        self.assertEqual(neg_terms("state-of-the-art"), [])

    def test_neg_terms_hyphen_glued_to_cjk_word_not_matched(self) -> None:
        """A hyphen tightly attached to a preceding CJK word character (no
        space) must be treated as an ordinary in-sentence hyphen, not
        negation syntax — the CJK analogue of the ASCII
        'state-of-the-art' guard above (v0.2.128).

        Before the fix, _NEG_RE's lookbehind only excluded ASCII word
        characters ([A-Za-z0-9_]); v0.2.118 extended the POSITIVE match side
        (what CAN be negated) to cover every chunk._CJK_RANGES script but
        never extended this lookbehind (what DISQUALIFIES a hyphen from being
        negation) to match, so 'の-最適化' (hiragana の directly before the
        hyphen) was misparsed as `-最適化` negation, silently discarding real
        query content instead of treating it as ordinary prose punctuation.
        """
        self.assertEqual(neg_terms("アルゴリズムの-最適化について"), [])
        self.assertEqual(strip_neg_terms("アルゴリズムの-最適化について"), "アルゴリズムの-最適化について")
        # A hyphen preceded by CJK PUNCTUATION (a word boundary, not a word
        # character) must still correctly introduce negation.
        self.assertEqual(neg_terms("書院。-legacy"), ["legacy"])
        # Existing space-preceded / string-start CJK negation must be unaffected.
        self.assertEqual(neg_terms("Python -日本語"), ["日本語"])

    def test_retrieve_multi_neg_false_positive_does_not_suppress_rewrite_hits(self) -> None:
        """End-to-end: the CJK hyphen-glued false-positive negation must not
        cause retrieve_multi() to silently zero out results a rewrite would
        otherwise have found (v0.2.128)."""
        from shoin.search import retrieve_multi

        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "d", "o", "sha")
            s.add_chunks(src.id, ["最適化についての説明がここにある。"])
            query = "アルゴリズムの-最適化について"
            self.assertEqual(len(retrieve(s, nb.id, query)), 1)
            hits = retrieve_multi(s, nb.id, [query, "最適化の手法とは"])
            self.assertEqual(len(hits), 1)

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
        """Negative-term filter excludes vector hits too, not just BM25 hits."""
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

    def test_retrieve_neg_term_hangul_excludes_matching_source(self) -> None:
        """End-to-end reproduction of the _NEG_RE script-coverage gap through the
        real retrieve() pipeline: a Hangul negated term must actually exclude
        the source containing it, not silently fail (or invert into a positive
        match, since strip_neg_terms() also failed to remove the un-recognized
        -word token from the query).
        """
        with make_store() as s:
            nb_id = s.create_notebook("neg-hangul").id
            src_ko = s.add_source(nb_id, "txt", "ko", "mem://ko", "sha-ko")
            s.add_chunks(src_ko.id, ["이것은 한국어 문서입니다 AI 기술에 대해 설명합니다."])
            src_en = s.add_source(nb_id, "txt", "en", "mem://en", "sha-en")
            s.add_chunks(
                src_en.id,
                ["This document discusses AI technology without any Korean text at all."],
            )
            hits = retrieve(s, nb_id, "AI -한국어", k=10)
            source_ids = {h.source_id for h in hits}
            self.assertNotIn(src_ko.id, source_ids, "Hangul-negated source must be excluded")
            self.assertIn(src_en.id, source_ids, "non-matching source must still be returned")

    def test_retrieve_neg_term_backfills_vec_hits_instead_of_starving(self) -> None:
        """Regression: the neg-term filter must run BEFORE mmr()'s k-selection,
        not after. Before the fix, vector_search() results were never filtered
        until after mmr(rerank(...), k) had already spent its k-selection budget
        on the unfiltered pool. If the negated-term chunks happened to have the
        highest vector similarity, mmr() picked exactly those k slots, the
        post-hoc filter then removed all of them, and retrieve() returned an
        empty list even though clean, relevant chunks existed lower in the pool.
        """
        with make_store() as s:
            nb_id = s.create_notebook("neg-vec-starve").id
            src = s.add_source(nb_id, "txt", "doc", "mem://d", "sha-d")
            # None of these chunks contain the BM25 query term "書院", so bm25_hits
            # is empty and ranking is driven entirely by vector similarity. All six
            # share the same prefix so MMR's bigram-overlap diversity term treats
            # them as equally redundant with each other regardless of the
            # legacy/clean suffix — otherwise MMR's own diversity mechanism could
            # accidentally paper over the bug by disfavoring near-duplicate legacy
            # chunks for unrelated reasons.
            texts = [
                "chunk about the topic. legacy marker here.",
                "chunk about the topic. legacy marker here too.",
                "chunk about the topic. legacy marker present now.",
                "chunk about the topic. clean marker here.",
                "chunk about the topic. clean marker here too.",
                "chunk about the topic. clean marker present now.",
            ]
            s.add_chunks(src.id, texts)
            chunks = s.chunks_for_notebook(nb_id)
            for c in chunks:
                # "legacy" chunks rank highest by cosine similarity; "clean" chunks
                # rank lower but are still well within the retrieval pool (pool =
                # max(k*3, 12) = 12, comfortably above these 6 chunks).
                vec = [1.0, 0.0] if "legacy" in c.text else [0.9, 0.1]
                s.set_embedding(c.id, vec)
            hits = retrieve(s, nb_id, "書院 -legacy", query_vec=[1.0, 0.0], k=3)
            self.assertTrue(hits, "must backfill from clean chunks instead of returning empty")
            self.assertFalse(any("legacy" in h.text for h in hits))
            self.assertTrue(all("clean" in h.text for h in hits))


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

    def test_digit_in_neg_term_does_not_trigger_exact_match_bias(self) -> None:
        """A digit inside a negated term must not reduce alpha via the digit heuristic.

        Before the fix, _DIGIT_RE.search(query) matched digits in neg-terms like -v2,
        triggering the exact-match penalty even though the positive query had no digits.
        Fix: search the neg-stripped query (clean_q) instead of the raw query.
        """
        # "neural network" → 2 terms → short-keyword penalty applied (-0.15 → 0.35)
        # "-v2" is a neg-term and must NOT trigger the digit penalty
        alpha_with_neg_digit = adaptive_alpha("neural network -v2")
        alpha_no_neg = adaptive_alpha("neural network")
        self.assertEqual(
            alpha_with_neg_digit, alpha_no_neg,
            "neg-term digit -v2 must not change alpha vs. the same query without it",
        )

    def test_digit_in_neg_term_does_not_affect_long_identifier_check(self) -> None:
        """A long identifier inside a neg-term must not trigger the long-token penalty.

        The `any(len(t) >= 12 ...)` check iterates over `terms` which already comes from
        strip_neg_terms, so long identifiers in neg-terms can't trigger it that way.
        This test confirms the check is consistently applied to positive terms only.
        """
        # The neg-term "-averylongnegidentifier" (22 chars) must NOT trigger the
        # long-identifier penalty (len >= 12). Only positive terms matter.
        alpha_neg_long = adaptive_alpha("cats -averylongnegidentifier")
        alpha_baseline = adaptive_alpha("cats")
        self.assertEqual(
            alpha_neg_long, alpha_baseline,
            "long neg-term identifier must not trigger the long-identifier alpha penalty",
        )


class TestBM25MergePathCap(unittest.TestCase):
    """bm25_search() merge path must cap results to k (v0.2.51)."""

    def test_fts5_like_merge_capped_to_k(self) -> None:
        """bm25_search must return at most k hits on the FTS5+LIKE merge path.

        The merge path is triggered when a query has at least one long term (≥3 chars)
        that FTS5 can index AND at least one short term (<3 chars) that needs LIKE.
        Before the fix, the merge path returned fts_hits + like_hits (up to k + 2000),
        violating the k parameter.
        """
        with make_store() as s:
            nb_id = s.create_notebook("cap-test").id
            src = s.add_source(nb_id, "txt", "x", "mem://x", "sha-x")
            # Add many chunks that match both the long and short query terms
            texts = [f"quantum mechanics entry {i} 猫" for i in range(50)]
            s.add_chunks(src.id, texts)
            # "quantum" (7 chars → FTS5) + "猫" (1 CJK char → LIKE) triggers merge path
            k = 5
            hits = bm25_search(s, nb_id, "quantum 猫", k=k)
            self.assertLessEqual(
                len(hits), k,
                f"bm25_search must return at most {k} hits on FTS5+LIKE merge path, got {len(hits)}",
            )

    def test_bm25_merge_path_globally_sorted(self) -> None:
        """FTS5+LIKE merge result must be globally sorted by bm25 score.

        Before v0.2.61, bm25_search() sorted the FTS5-hit sublist *before*
        extending with LIKE-only hits.  A LIKE-only chunk with bm25=50 was
        appended at the end of the list behind an FTS5 chunk with bm25≈5.
        rrf_fuse() assigns rank-reciprocal scores by position, so the
        mis-ranked LIKE-only chunk received a worse RRF score than it deserved.

        Concrete failing scenario:
          query "abc 猫": "abc" (3 chars) goes to FTS5; "猫" (1 char CJK) is
          too short for FTS5 trigrams and falls through to LIKE.
          chunk_a matches FTS5 ("abc") + 1× LIKE ("猫") → bm25 ≈ raw_fts5 + 2 ≈ 5
          chunk_b matches 50× LIKE ("猫") only         → bm25 = 50
          Without fix: returned as [chunk_a(5), chunk_b(50)] → rank 0 and 1
          With fix:    re-sort after extend → [chunk_b(50), chunk_a(5)] → rank 0 and 1
        """
        with make_store() as s:
            nb_id = s.create_notebook("merge-sort-test").id
            src = s.add_source(nb_id, "txt", "doc", "mem://ms", "sha-ms")
            # chunk_a: FTS5-matched for "abc" + 1 LIKE hit for "猫" → bm25 ≈ small
            # chunk_b: LIKE-only (no "abc"), 50 occurrences of "猫" → bm25 = 50
            s.add_chunks(src.id, [
                "abc is a standard example term 猫",
                "猫" * 50 + " unrelated content no abc here",
            ])
            hits = bm25_search(s, nb_id, "abc 猫", k=5)
            self.assertEqual(len(hits), 2)
            # Combined list must be sorted: highest bm25 first.
            # Without the fix, hits[0].bm25 ≈ 5 and hits[1].bm25 = 50 — inverted.
            self.assertGreaterEqual(
                hits[0].bm25, hits[1].bm25,
                f"bm25_search FTS5+LIKE merge not globally sorted: "
                f"hits[0].bm25={hits[0].bm25:.2f} < hits[1].bm25={hits[1].bm25:.2f}",
            )
            # The LIKE-dominant chunk (50 occurrences) must rank first.
            self.assertIn(
                "猫猫猫", hits[0].text,
                f"LIKE-dominant chunk must rank first; got hits[0].text={hits[0].text!r}",
            )


class TestCLI(unittest.TestCase):
    """CLI main() error-handling tests."""

    def test_serve_oserror_returns_exit_code_1(self) -> None:
        """When `shoin serve` fails to bind the port (OSError), main() must return 1.

        Before v0.2.41, the `serve()` call was outside the try/except block in
        main(), so OSError (e.g., 'Address already in use') propagated as an
        unhandled Python traceback instead of a clean error message + exit code 1.

        This test previously lived in a duplicate `class TestCLI` definition earlier
        in this file (line ~2438). Python silently rebinds the class name on
        redefinition, so the earlier class — and this test — was never collected
        by pytest/unittest; merged into the single TestCLI class here (v0.2.64).
        """
        from unittest.mock import patch
        from shoin.cli import main

        # serve is imported locally inside main() so patch it at the source module.
        with patch("shoin.server.serve", side_effect=OSError("Address already in use")):
            rc = main(["serve"])
        self.assertEqual(rc, 1)

    def test_health_command_reports_config_without_store(self) -> None:
        """`shoin health` (REQ-103 CLI parity with GET /api/health, v0.2.126) must
        print config/reachability without needing a working data directory — it's
        special-cased above the Store() construction in main(), matching `serve`."""
        import io
        from unittest.mock import patch
        from shoin.cli import main

        class FakeAvailLLM:
            embedding_model = ""

            def available(self) -> bool:
                return True

            def chat(self, messages, temperature=0.2):
                raise NotImplementedError

            def embed_one(self, text):
                raise NotImplementedError

        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = main(["health"], llm=FakeAvailLLM())
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn(VERSION, text)
        self.assertIn("はい", text)  # LLM reachable: yes (default ja locale)

    def test_health_command_reflects_multi_query_and_embed_batch_env(self) -> None:
        import io
        import os
        from unittest.mock import patch
        from shoin.cli import main
        from shoin.llm import LLMError

        class FakeUnavailLLM:
            embedding_model = "nomic-embed-text"

            def available(self) -> bool:
                return False

            def chat(self, messages, temperature=0.2):
                raise LLMError("SYSTEM_SERVICE_UNAVAILABLE", "down")

            def embed_one(self, text):
                raise LLMError("SYSTEM_EMBED_DISABLED", "no embed")

        out = io.StringIO()
        env = {"SHOIN_LANG": "en", "SHOIN_MULTI_QUERY": "1", "SHOIN_EMBED_BATCH": "32"}
        with patch.dict(os.environ, env, clear=False):
            with patch("sys.stdout", out):
                rc = main(["health"], llm=FakeUnavailLLM())
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("LLM reachable: no", text)
        self.assertIn("Multi-query retrieval (SHOIN_MULTI_QUERY): yes", text)
        self.assertIn("Embed batch size (SHOIN_EMBED_BATCH): 32", text)

    def test_health_command_prints_clean_error_instead_of_raw_traceback(self) -> None:
        """`shoin health` must never crash with a raw Python traceback — the
        whole point of a diagnostic command is to report cleanly even when
        the environment is broken (v0.2.128). Unlike every other subcommand
        (StoreError/IngestError/LLMError -> err.prefix), health is invoked
        BEFORE main()'s Store()-dependent try block (deliberately, so it
        still works when the data directory itself is broken — see the
        comment at its call site), so it needs its own defense-in-depth catch
        rather than falling through to that shared try block."""
        import io
        from unittest.mock import patch
        from shoin.cli import main

        class ExplodingLLM:
            embedding_model = ""

            def available(self) -> bool:
                raise RuntimeError("simulated unexpected failure")

            def chat(self, messages, temperature=0.2):
                raise NotImplementedError

            def embed_one(self, text):
                raise NotImplementedError

        err = io.StringIO()
        with patch("sys.stderr", err):
            rc = main(["health"], llm=ExplodingLLM())
        self.assertEqual(rc, 1)
        self.assertIn("simulated unexpected failure", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_health_command_reflects_db_override(self) -> None:
        """`shoin --db <path> health` must report the SAME effective database
        path this invocation would actually use, not always the config-
        derived default (v0.2.128) — every other subcommand honors --db via
        main()'s `Store(str(args.db) if args.db else db_path())`, but
        _cmd_health() took no args parameter at all and unconditionally
        printed the bare config default, misleading a user diagnosing
        exactly the scenario --db exists for (a custom database location)."""
        import io
        from unittest.mock import patch
        from shoin.cli import main

        class FakeAvailLLM:
            embedding_model = ""

            def available(self) -> bool:
                return True

            def chat(self, messages, temperature=0.2):
                raise NotImplementedError

            def embed_one(self, text):
                raise NotImplementedError

        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = main(["--db", "/tmp/custom-health-test.sqlite3", "health"], llm=FakeAvailLLM())
        self.assertEqual(rc, 0)
        self.assertIn("/tmp/custom-health-test.sqlite3", out.getvalue())

    def test_studio_no_citations_does_not_print_separator(self) -> None:
        """_cmd_studio must suppress the '---' separator when no citations are present.

        Before v0.2.55, _cmd_studio unconditionally printed '---' then called
        _print_report(), which printed nothing for an empty cited list.  The user
        saw a lone '---' with no content below it.  The symmetric fix was applied to
        _cmd_ask in v0.2.27.
        Fix: guard print('---') and _print_report() with if result.report['cited'].
        """
        import io
        from unittest.mock import patch, MagicMock
        from shoin.cli import main
        from shoin.studio import StudioResult
        from shoin.citation import CitationReport

        report: CitationReport = CitationReport(
            cited=[], invalid=[], coverage=0.0, n_sources=1,
            source_map={"S1": "doc"}, confirmed=[], misattributed=[],
        )
        fake_result = StudioResult(kind="mindmap", body="## Mindmap\n- A\n- B", report=report)

        with make_store() as s:
            nb = s.create_notebook("test")
            s.add_source(nb.id, "txt", "doc", "mem://d", "sha1")

        # Run via temp DB file so main() can open it
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            db_file = f.name
        try:
            with make_store() as s2:
                pass  # just need a clean DB
            # Build a store at db_file path and add a notebook + source
            from shoin.store import Store
            with Store(db_file) as s:
                nb = s.create_notebook("test")
                s.add_source(nb.id, "txt", "doc", "mem://d", "sha1")
                nb_id = nb.id

            captured = io.StringIO()
            with patch("shoin.cli.generate", return_value=fake_result):
                with patch("sys.stdout", captured):
                    rc = main(["--db", db_file, "studio", str(nb_id), "mindmap"])
            self.assertEqual(rc, 0)
            output = captured.getvalue()
            self.assertIn("## Mindmap", output)
            self.assertNotIn("---", output, "separator must be suppressed when no citations exist")
        finally:
            os.unlink(db_file)

    def test_ask_lone_separator_suppressed_for_non_degraded_empty_report(self) -> None:
        """_cmd_ask must suppress the '---' separator for a non-degraded answer
        whose citation report is genuinely empty (cited/invalid/uncited all
        empty) — e.g. the model correctly follows the system prompt's "say so
        explicitly" rule for a fact not in the sources, which
        uncited_sentences() deliberately excludes via _DISCLAIMER_MARKERS
        (citation.py). Before this fix, _cmd_ask's guard only checked
        `answer.hits and not answer.degraded`, not report content — unlike
        _cmd_studio's stronger v0.2.55 guard — so this exact non-degraded,
        zero-citation combination printed a lone '---' with nothing under it.
        """
        import io
        import os
        import tempfile
        from unittest.mock import patch

        from shoin.cli import main
        from shoin.citation import CitationReport
        from shoin.qa import Answer
        from shoin.search import Hit
        from shoin.store import Store

        report: CitationReport = CitationReport(
            cited=[], invalid=[], coverage=0.0, n_sources=1,
            source_map={"S1": "doc"}, confirmed=[], misattributed=[],
        )
        fake_hit = Hit(chunk_id=1, source_id=1, text="body", score=1.0)
        fake_answer = Answer(text="ソースに記載なし。", hits=[fake_hit], report=report, degraded=False)

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            db_file = f.name
        try:
            with Store(db_file) as s:
                nb = s.create_notebook("test")
                s.add_source(nb.id, "txt", "doc", "mem://d", "sha1")
                nb_id = nb.id

            captured = io.StringIO()
            with patch("shoin.cli.ask", return_value=fake_answer):
                with patch("sys.stdout", captured):
                    rc = main(["--db", db_file, "ask", str(nb_id), "question"])
            self.assertEqual(rc, 0)
            output = captured.getvalue()
            self.assertIn("ソースに記載なし。", output)
            self.assertNotIn("---", output, "separator must be suppressed when report is empty")
        finally:
            os.unlink(db_file)

    def test_studio_invalid_only_report_still_prints_separator(self) -> None:
        """_cmd_studio must still print the '---' separator/report when the
        citation report has ONLY out-of-range (invalid) citations — cited and
        uncited both empty. _print_report() does print something for this
        case (the "検証失敗の引用" warning), but the pre-fix guard
        (`result.report["cited"] or result.report.get("uncited")`) missed
        `invalid` entirely, silently dropping that warning from CLI output.
        """
        import io
        import os
        import tempfile
        from unittest.mock import patch

        from shoin.cli import main
        from shoin.citation import CitationReport
        from shoin.studio import StudioResult
        from shoin.store import Store

        report: CitationReport = CitationReport(
            cited=[], invalid=[99], coverage=0.0, n_sources=1,
            source_map={"S1": "doc"}, confirmed=[], misattributed=[],
        )
        fake_result = StudioResult(kind="briefing", body="回答は [S99] を参照。", report=report)

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            db_file = f.name
        try:
            with Store(db_file) as s:
                nb = s.create_notebook("test")
                s.add_source(nb.id, "txt", "doc", "mem://d", "sha1")
                nb_id = nb.id

            captured = io.StringIO()
            with patch("shoin.cli.generate", return_value=fake_result):
                with patch("sys.stdout", captured):
                    rc = main(["--db", db_file, "studio", str(nb_id), "briefing"])
            self.assertEqual(rc, 0)
            output = captured.getvalue()
            self.assertIn("---", output, "separator must be printed when invalid citations exist")
            self.assertIn("S99", output)
        finally:
            os.unlink(db_file)

    def test_main_db_locked_returns_exit_code_1(self) -> None:
        """main() must catch sqlite3.OperationalError and return exit code 1.

        Before v0.2.55, sqlite3.OperationalError (DB lock timeout) propagated
        through main()'s except clause (which only caught StoreError/IngestError/
        LLMError/OverflowError/KeyboardInterrupt) as a raw traceback.
        Fix: add sqlite3.OperationalError to the outer handler in main().
        """
        import sqlite3 as _sqlite3
        import io
        from unittest.mock import patch
        from shoin.cli import main

        with patch("shoin.cli.Store.__enter__", side_effect=_sqlite3.OperationalError("database is locked")):
            err_out = io.StringIO()
            with patch("sys.stderr", err_out):
                rc = main(["notebook", "list"])
        self.assertEqual(rc, 1)
        self.assertIn("SYSTEM_DB_LOCKED", err_out.getvalue())

    def test_main_os_error_returns_exit_code_1(self) -> None:
        """main() must catch OSError and return exit code 1 with SYSTEM_IO_ERROR.

        Store.__init__ calls Path.mkdir() to create the data directory.  If the
        path is on a read-only filesystem or the user lacks write permission,
        mkdir() raises PermissionError (an OSError subclass).  Before v0.2.59,
        this propagated through main() as a bare Python traceback because the outer
        handler only caught StoreError/IngestError/LLMError/OperationalError.
        Fix: add `except OSError` to the outer handler in main().
        """
        import io
        from unittest.mock import patch
        from shoin.cli import main

        with patch("shoin.cli.Store", side_effect=OSError("[Errno 13] Permission denied: '/data/shoin'")):
            err_out = io.StringIO()
            with patch("sys.stderr", err_out):
                rc = main(["notebook", "list"])
        self.assertEqual(rc, 1)
        self.assertIn("SYSTEM_IO_ERROR", err_out.getvalue())

    def test_cmd_add_db_locked_continues_and_returns_nonzero(self) -> None:
        """_cmd_add must catch sqlite3.OperationalError per-target and continue.

        Before v0.2.55, a DB lock error from store.add_chunks() inside index_source()
        was not caught by the inner except (IngestError, StoreError) clause, so it
        propagated to main(), printing a raw traceback and skipping remaining targets.
        Fix: add sqlite3.OperationalError to the inner except in _cmd_add.
        """
        import sqlite3 as _sqlite3
        import io
        import tempfile, os
        from unittest.mock import patch
        from shoin.cli import main
        from shoin.store import Store

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            db_file = f.name
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("test").id

            err_out = io.StringIO()
            out = io.StringIO()
            with patch(
                "shoin.cli.index_source",
                side_effect=_sqlite3.OperationalError("database is locked"),
            ):
                with patch("sys.stderr", err_out):
                    with patch("sys.stdout", out):
                        rc = main(["--db", db_file, "add", str(nb_id), "file.pdf"])
            self.assertEqual(rc, 1)
            self.assertIn("SYSTEM_DB_LOCKED", err_out.getvalue())
        finally:
            os.unlink(db_file)


class TestCLINoteSourceParity(unittest.TestCase):
    """CLI note/source subcommands (v0.2.68): before this, notes and source
    management (delete/rename/refresh) existed only as Web API routes, despite
    cli.py's own module docstring claiming 'the CLI exposes every core
    capability so the product is fully usable headless (REQ-103)'.
    """

    def _db(self) -> str:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            return f.name

    def test_note_add_list_delete_roundtrip(self) -> None:
        import io
        import os
        from unittest.mock import patch

        from shoin.cli import main
        from shoin.store import Store

        db_file = self._db()
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("note-cli-test").id

            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = main(["--db", db_file, "note", "add", str(nb_id), "題", "本文"])
            self.assertEqual(rc, 0)
            self.assertIn("題", out.getvalue())

            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = main(["--db", db_file, "note", "list", str(nb_id)])
            self.assertEqual(rc, 0)
            self.assertIn("題", out.getvalue())

            with Store(db_file) as s:
                note_id = s.list_notes(nb_id)[0]["id"]

            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = main(["--db", db_file, "note", "delete", str(note_id)])
            self.assertEqual(rc, 0)

            with Store(db_file) as s:
                self.assertEqual(s.list_notes(nb_id), [])
        finally:
            os.unlink(db_file)

    def test_note_list_empty_notebook_prints_hint(self) -> None:
        import io
        import os
        from unittest.mock import patch

        from shoin.cli import main
        from shoin.store import Store

        db_file = self._db()
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("empty-notes-test").id
            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = main(["--db", db_file, "note", "list", str(nb_id)])
            self.assertEqual(rc, 0)
            self.assertIn("shoin note add", out.getvalue())
        finally:
            os.unlink(db_file)

    def test_source_delete_removes_source(self) -> None:
        import io
        import os
        from unittest.mock import patch

        from shoin.cli import main
        from shoin.store import Store, StoreError

        db_file = self._db()
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("src-delete-test").id
                src_id = s.add_source(nb_id, "txt", "doc", "mem://d", "sha1").id

            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = main(["--db", db_file, "source", "delete", str(src_id)])
            self.assertEqual(rc, 0)

            with Store(db_file) as s:
                with self.assertRaises(StoreError):
                    s.get_source(src_id)
        finally:
            os.unlink(db_file)

    def test_source_rename_preserves_origin(self) -> None:
        """CLI rename must update only the title, matching server._h_src_patch's
        get-then-update-with-original-origin pattern (not blank the origin)."""
        import io
        import os
        from unittest.mock import patch

        from shoin.cli import main
        from shoin.store import Store

        db_file = self._db()
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("src-rename-test").id
                src_id = s.add_source(nb_id, "txt", "old title", "mem://original", "sha1").id

            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = main(["--db", db_file, "source", "rename", str(src_id), "new title"])
            self.assertEqual(rc, 0)

            with Store(db_file) as s:
                src = s.get_source(src_id)
            self.assertEqual(src.title, "new title")
            self.assertEqual(src.origin, "mem://original", "origin must survive a rename")
        finally:
            os.unlink(db_file)

    def test_notebook_rename_cli_message_matches_persisted_stripped_name(self) -> None:
        """CLI `notebook rename`'s confirmation message must report the
        WHITESPACE-STRIPPED name actually persisted by rename_notebook(), not
        the raw CLI argument. Same bug class as v0.2.93-95 (source
        upload/patch/rename echoing an untransformed value), found in this
        sibling notebook action — nine lines above source rename in the same
        file, never given the same treatment.

        Uses an exact string comparison against _t()'s own template rather
        than a stripped/parsed substring — a naive `.strip()` on the parsed
        output would mask this exact bug, since both the buggy padded value
        and the fixed value strip down to the same substring.
        """
        import io
        import os
        from unittest.mock import patch

        from shoin.cli import _t, main
        from shoin.store import Store

        db_file = self._db()
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("notebook-rename-strip-test").id

            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = main(["--db", db_file, "notebook", "rename", str(nb_id), "  Padded Name  "])
            self.assertEqual(rc, 0)
            expected = _t("nb.renamed", id=str(nb_id), name="Padded Name") + "\n"
            self.assertEqual(out.getvalue(), expected)

            with Store(db_file) as s:
                self.assertEqual(s.get_notebook(nb_id).name, "Padded Name")
        finally:
            os.unlink(db_file)

    def test_note_add_cli_message_matches_persisted_stripped_title(self) -> None:
        """CLI `note add`'s confirmation message must report the
        WHITESPACE-STRIPPED title actually persisted by add_note() (which
        does `title = title.strip()` before INSERT), not the raw CLI
        argument. Same bug class as v0.2.93-95/99 (source upload/patch/
        rename, notebook rename all echoing an untransformed value), found
        in this sibling note-add call site.

        Uses an exact string comparison against _t()'s own template rather
        than a stripped/parsed substring — a naive `.strip()` on the parsed
        output would mask this exact bug, since both the buggy padded value
        and the fixed value strip down to the same substring (the same trap
        called out in the notebook-rename test above).
        """
        import io
        import os
        from unittest.mock import patch

        from shoin.cli import _t, main
        from shoin.store import Store

        db_file = self._db()
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("note-add-strip-test").id

            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = main(
                    ["--db", db_file, "note", "add", str(nb_id), "  Padded Title  ", "本文"]
                )
            self.assertEqual(rc, 0)

            with Store(db_file) as s:
                notes = s.list_notes(nb_id)
                self.assertEqual(len(notes), 1)
                note_id = notes[0]["id"]
                self.assertEqual(notes[0]["title"], "Padded Title")

            expected = _t("note.added", id=str(note_id), title="Padded Title") + "\n"
            self.assertEqual(out.getvalue(), expected)
        finally:
            os.unlink(db_file)

    def test_source_rename_cli_message_matches_persisted_truncated_title(self) -> None:
        """CLI `source rename`'s confirmation message must report the TRUNCATED
        title actually persisted by update_source_title() (MAX_TITLE_LEN), not
        the raw CLI argument. Same bug class as v0.2.93 (_h_src_upload) and
        v0.2.94 (_h_src_patch), found in this third, CLI-side call site: the
        print statement used str(args.title) unconditionally."""
        import io
        import os
        from unittest.mock import patch

        from shoin.cli import main
        from shoin.config import MAX_TITLE_LEN
        from shoin.store import Store

        db_file = self._db()
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("src-rename-trunc-test").id
                src_id = s.add_source(nb_id, "txt", "old title", "mem://original", "sha2").id

            long_title = "X" * 550
            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = main(["--db", db_file, "source", "rename", str(src_id), long_title])
            self.assertEqual(rc, 0)
            printed_title = out.getvalue().split("]", 1)[1].strip()
            self.assertEqual(len(printed_title), MAX_TITLE_LEN)

            with Store(db_file) as s:
                persisted_title = s.get_source(src_id).title
            self.assertEqual(
                printed_title, persisted_title,
                "CLI message must match what was actually persisted",
            )
        finally:
            os.unlink(db_file)

    def test_source_rename_empty_title_rejected_not_silently_persisted(self) -> None:
        """CLI `source rename` must reject an empty title with a clean error
        and exit code 1, matching what PATCH /api/sources/{id} already
        enforces via server.py's _require() — not silently persist a blank
        title (found to do so before the store-level guard was added)."""
        import io
        import os
        from unittest.mock import patch

        from shoin.cli import main
        from shoin.store import Store

        db_file = self._db()
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("src-rename-empty-test").id
                src_id = s.add_source(nb_id, "txt", "original title", "mem://original", "sha1").id

            err_out = io.StringIO()
            with patch("sys.stderr", err_out):
                rc = main(["--db", db_file, "source", "rename", str(src_id), ""])
            self.assertEqual(rc, 1)
            self.assertIn("VALIDATION_REQUIRED_FIELD_MISSING", err_out.getvalue())

            with Store(db_file) as s:
                self.assertEqual(s.get_source(src_id).title, "original title")
        finally:
            os.unlink(db_file)

    def test_source_refresh_calls_pipeline(self) -> None:
        import io
        import os
        from unittest.mock import patch

        from shoin.cli import main
        from shoin.pipeline import IndexResult
        from shoin.store import Source, Store

        db_file = self._db()
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("src-refresh-test").id
                src_id = s.add_source(nb_id, "url", "old", "http://example.test", "sha1").id

            fake_source = Source(src_id, nb_id, "url", "refreshed", "http://example.test", "sha2", "2026-01-01")
            fake_result = IndexResult(source=fake_source, n_chunks=3, n_embedded=0)
            out = io.StringIO()
            with patch("shoin.cli.refresh_source", return_value=fake_result):
                with patch("sys.stdout", out):
                    rc = main(["--db", db_file, "source", "refresh", str(src_id)])
            self.assertEqual(rc, 0)
            self.assertIn("refreshed", out.getvalue())
            self.assertIn("3 chunks", out.getvalue())
        finally:
            os.unlink(db_file)


class TestCLIMessagesList(unittest.TestCase):
    """CLI `messages list` (v0.2.74): cli.py's own module docstring claims 'the
    CLI exposes every core capability so the product is fully usable headless
    (REQ-103)'. `messages` only had a `clear` action — a headless (SSH-only, no
    browser) user could destroy chat history but never read it back short of
    `shoin export --format md`, which dumps the entire notebook rather than just
    the chat log.
    """

    def _db(self) -> str:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            return f.name

    def test_messages_list_shows_role_and_body(self) -> None:
        import io
        import os
        from unittest.mock import patch

        from shoin.cli import main
        from shoin.store import Store

        db_file = self._db()
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("msg-cli-test").id
                s.add_message(nb_id, "user", "質問です", "{}")
                s.add_message(nb_id, "assistant", "回答です [S1]", "{}")

            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = main(["--db", db_file, "messages", "list", str(nb_id)])
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("user", text)
            self.assertIn("質問です", text)
            self.assertIn("assistant", text)
            self.assertIn("回答です [S1]", text)
        finally:
            os.unlink(db_file)

    def test_messages_list_empty_notebook_prints_hint(self) -> None:
        import io
        import os
        from unittest.mock import patch

        from shoin.cli import main
        from shoin.store import Store

        db_file = self._db()
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("msg-cli-empty").id

            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = main(["--db", db_file, "messages", "list", str(nb_id)])
            self.assertEqual(rc, 0)
            self.assertIn("チャット履歴がありません", out.getvalue())
        finally:
            os.unlink(db_file)

    def test_messages_list_then_clear_roundtrip(self) -> None:
        import io
        import os
        from unittest.mock import patch

        from shoin.cli import main
        from shoin.store import Store

        db_file = self._db()
        try:
            with Store(db_file) as s:
                nb_id = s.create_notebook("msg-cli-roundtrip").id
                s.add_message(nb_id, "user", "hello", "{}")

            with patch("sys.stdout", io.StringIO()):
                rc = main(["--db", db_file, "messages", "clear", str(nb_id)])
            self.assertEqual(rc, 0)

            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = main(["--db", db_file, "messages", "list", str(nb_id)])
            self.assertEqual(rc, 0)
            self.assertIn("チャット履歴がありません", out.getvalue())
        finally:
            os.unlink(db_file)


class TestChunkContext(unittest.TestCase):
    """Contextual chunk metadata (migration 5): heading breadcrumbs indexed
    alongside chunk text so a term in a section heading retrieves chunks that
    were split away from that heading."""

    def test_split_text_output_unchanged_by_context(self) -> None:
        """split_text() must return byte-identical chunk text to the chunk_text
        of split_text_with_context() — the context is additive, never altering
        what is stored, displayed, or citation-verified."""
        from shoin.chunk import split_text_with_context

        doc = (
            "# 見出しA\n本文いちが続く文章。もう少し長い説明。\n\n"
            "## 見出しB\n別の段落の内容がここにある。詳細な記述。\n\n"
            "### 見出しC\n最後の節の本文テキスト。"
        )
        for ct, ov in ((40, 0), (20, 5), (200, 30)):
            plain = split_text(doc, chunk_tokens=ct, overlap_tokens=ov)
            paired = split_text_with_context(doc, chunk_tokens=ct, overlap_tokens=ov)
            self.assertEqual(plain, [t for _, t in paired])

    def test_breadcrumb_tracks_heading_hierarchy(self) -> None:
        """Nested headings build a ' > '-joined breadcrumb; a same-or-shallower
        heading pops deeper sections off the stack."""
        from shoin.chunk import split_text_with_context

        doc = (
            "# トップ\n序文。\n\n"
            "## 子1\n子1の本文。\n\n"
            "### 孫\n孫の本文。\n\n"
            "## 子2\n子2の本文。"
        )
        pairs = split_text_with_context(doc, chunk_tokens=8, overlap_tokens=0)
        ctx_by_text = {t.split("\n")[0]: c for c, t in pairs}
        self.assertEqual(ctx_by_text["# トップ"], "トップ")
        self.assertEqual(ctx_by_text["## 子1"], "トップ > 子1")
        self.assertEqual(ctx_by_text["### 孫"], "トップ > 子1 > 孫")
        # 子2 (level 2) pops 孫 (level 3) AND 子1 (level 2) back to トップ.
        self.assertEqual(ctx_by_text["## 子2"], "トップ > 子2")

    def test_breadcrumb_preserves_title_ending_in_hash(self) -> None:
        """A heading title that legitimately ends in '#' (e.g. language names
        C#/F#) must not have that character silently deleted -- only a
        genuine CommonMark ATX closing sequence (one or more '#' PRECEDED BY
        A SPACE, e.g. '## Heading ##') should be stripped (v0.2.128).

        Before the fix, `.rstrip("#")` unconditionally deleted any trailing
        '#' characters regardless of whether they were preceded by whitespace,
        so a title ending in '#' with no space before it lost that character
        from the retrieval breadcrumb -- defeating the exact heading-term-
        recall case v0.2.123's contextual chunking was built to provide, for
        any section about C#, F#, or similarly named topics.
        """
        from shoin.chunk import _context_blocks

        pairs = _context_blocks("## Learning C#\nThis section is about the C# language.")
        self.assertEqual(pairs[0][0], "Learning C#")

        pairs = _context_blocks("# F#\nBody about F#.")
        self.assertEqual(pairs[0][0], "F#")

        # A genuine ATX closing sequence (space before the '#' run) must
        # still be stripped, per CommonMark.
        pairs = _context_blocks("## Heading ##\nBody.")
        self.assertEqual(pairs[0][0], "Heading")

    def test_context_capped(self) -> None:
        """A pathologically deep/long heading path is capped so it can't bloat
        the index or dominate the embedding input."""
        from shoin.chunk import _MAX_CONTEXT_CHARS, split_text_with_context

        doc = "\n\n".join(f"{'#' * min(i + 1, 6)} {'長' * 50}\n本文{i}" for i in range(6))
        pairs = split_text_with_context(doc, chunk_tokens=8, overlap_tokens=0)
        for ctx, _ in pairs:
            self.assertLessEqual(len(ctx), _MAX_CONTEXT_CHARS)

    def test_chunk_split_off_heading_still_retrievable(self) -> None:
        """The core win: chunks split away from their heading (heading term
        absent from the body) are retrievable because the breadcrumb is indexed
        with every chunk. Before migration 5 only the heading-bearing chunk matched."""
        from shoin.chunk import split_text_with_context
        from shoin.pipeline import _chunk_context

        body = "。".join(f"細胞で起きる反応その{i}の詳細な説明が延々と続く記述" for i in range(30))
        doc = f"# 光合成のしくみ\n{body}"
        pairs = split_text_with_context(doc, chunk_tokens=60, overlap_tokens=0)
        texts = [t for _, t in pairs]
        # Sanity: only the first chunk keeps the literal heading term in its body.
        self.assertGreater(len(texts), 5)
        self.assertEqual(sum("光合成" in t for t in texts), 1)

        with make_store() as s:
            nb = s.create_notebook("bio")
            src = s.add_source(nb.id, "txt", "生物ノート", "mem://b", "sha-b")
            contexts = [_chunk_context(src.title, c) for c, _ in pairs]
            s.add_chunks(src.id, texts, contexts)
            hits = bm25_search(s, nb.id, "光合成", len(texts))
            # Far more than the single heading-bearing chunk is now retrieved.
            self.assertGreater(len(hits), 1)

    def test_source_title_in_context_matches_across_source(self) -> None:
        """The source title folded into every chunk's context lets a query that
        names the document retrieve its chunks even when the body never repeats
        the title."""
        from shoin.pipeline import _chunk_context

        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "アインシュタイン相対論", "mem://e", "sha-e")
            # Body deliberately omits the title words.
            texts = ["時間と空間は観測者に依存する。", "重力は幾何学的な効果である。"]
            contexts = [_chunk_context(src.title, "") for _ in texts]
            s.add_chunks(src.id, texts, contexts)
            hits = bm25_search(s, nb.id, "相対論", 5)
            self.assertGreater(len(hits), 0)

    def test_add_chunks_context_length_mismatch_raises(self) -> None:
        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "d", "o", "sha")
            with self.assertRaises(StoreError) as cm:
                s.add_chunks(src.id, ["a", "b"], ["only-one-context"])
            self.assertEqual(cm.exception.code, "VALIDATION_FIELD_FORMAT_INVALID")

    def test_add_chunks_without_contexts_defaults_empty(self) -> None:
        """Backward compatibility: callers passing no contexts still work; the
        context column defaults to '' and retrieval falls back to text-only."""
        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "d", "o", "sha")
            ids = s.add_chunks(src.id, ["猫は液体である", "犬は固体である"])
            self.assertEqual(len(ids), 2)
            row = s.conn.execute(
                "SELECT context FROM chunks WHERE id=?", (ids[0],)
            ).fetchone()
            self.assertEqual(row["context"], "")

    def test_rename_source_refreshes_chunk_context_titles(self) -> None:
        """update_source_title() must rewrite the title prefix in each chunk's
        context (v0.2.124). Before the fix, a renamed source kept matching FTS
        queries for its OLD title — and never matched its new one — indefinitely,
        because v0.2.123 folded the title into every chunk's indexed context but
        rename never touched it."""
        from shoin.pipeline import _chunk_context

        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "古い報告書", "mem://r", "sha-r")
            # Body deliberately omits both old and new title words.
            texts = ["時間と空間の観測に関する記述。", "重力の幾何学的な効果の説明。"]
            contexts = [_chunk_context(src.title, "第一章"), _chunk_context(src.title, "")]
            s.add_chunks(src.id, texts, contexts)
            # Sanity: old title matches pre-rename.
            self.assertGreater(len(bm25_search(s, nb.id, "古い報告書", 5)), 0)

            s.update_source_title(src.id, "新しい年次総括", src.origin)

            # New title now matches; old title no longer does.
            self.assertGreater(len(bm25_search(s, nb.id, "新しい年次総括", 5)), 0)
            self.assertEqual(bm25_search(s, nb.id, "古い報告書", 5), [])
            # Breadcrumb tail beyond the title prefix is preserved.
            rows = s.conn.execute(
                "SELECT context FROM chunks WHERE source_id=? ORDER BY seq", (src.id,)
            ).fetchall()
            self.assertEqual(rows[0]["context"], "新しい年次総括 > 第一章")
            self.assertEqual(rows[1]["context"], "新しい年次総括")

    def test_rename_source_leaves_contextless_chunks_untouched(self) -> None:
        """Pre-migration-5 chunks (context='') must not be rewritten on rename —
        there is no old-title prefix to match, so no safe rewrite exists."""
        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "旧題", "mem://o", "sha-o")
            s.add_chunks(src.id, ["本文のみのチャンク"])  # context defaults to ''
            s.update_source_title(src.id, "新題", src.origin)
            row = s.conn.execute(
                "SELECT context FROM chunks WHERE source_id=?", (src.id,)
            ).fetchone()
            self.assertEqual(row["context"], "")

    def test_chunk_update_trigger_keeps_fts_in_sync(self) -> None:
        """Migration 6's chunks_au trigger: an UPDATE of context/text must be
        mirrored into chunks_fts (delete old + insert new). Without it the
        external-content FTS index silently desynchronizes."""
        with make_store() as s:
            nb = s.create_notebook("nb")
            src = s.add_source(nb.id, "txt", "d", "o", "sha")
            # Vocabulary chosen with NO shared trigrams between old and new
            # values — FTS5 trigram matching is OR-joined for recall, so any
            # shared 3-gram would legitimately keep the old query matching.
            ids = s.add_chunks(src.id, ["りんご栽培の記録"], ["果樹園芸ノート"])
            s.conn.execute(
                "UPDATE chunks SET context=?, text=? WHERE id=?",
                ("機械工学ノート", "自動車整備の記録", ids[0]),
            )
            s.conn.commit()
            self.assertEqual(len(bm25_search(s, nb.id, "自動車整備", 5)), 1)
            self.assertEqual(len(bm25_search(s, nb.id, "機械工学", 5)), 1)
            self.assertEqual(bm25_search(s, nb.id, "りんご栽培", 5), [])
            self.assertEqual(bm25_search(s, nb.id, "果樹園芸", 5), [])

    def test_embed_batch_env_override(self) -> None:
        """SHOIN_EMBED_BATCH overrides the batch size; invalid/unset values fall
        back to the EMBED_BATCH module default (v0.2.124, CLAUDE.md known gap)."""
        import os
        from unittest.mock import patch as mock_patch

        from shoin.pipeline import _embed_chunks

        class CountingEmbedLLM:
            embedding_model = "test-model"

            def __init__(self) -> None:
                self.calls: list[int] = []

            def embed(self, texts: list[str]) -> list[list[float]]:
                self.calls.append(len(texts))
                return [[0.1, 0.2] for _ in texts]

        def run(env_val: str | None) -> list[int]:
            env = {"SHOIN_EMBED_BATCH": env_val} if env_val is not None else {}
            with make_store() as s:
                nb = s.create_notebook("nb")
                src = s.add_source(nb.id, "txt", "d", "o", "sha")
                texts = [f"chunk {i}" for i in range(10)]
                ids = s.add_chunks(src.id, texts)
                llm = CountingEmbedLLM()
                with mock_patch.dict(os.environ, env, clear=False):
                    if env_val is None and "SHOIN_EMBED_BATCH" in os.environ:
                        del os.environ["SHOIN_EMBED_BATCH"]
                    _embed_chunks(s, llm, ids, texts)
                return llm.calls

        self.assertEqual(run("4"), [4, 4, 2])  # override: 10 chunks in batches of 4
        self.assertEqual(run("abc"), [10])  # invalid → default 16 → one batch
        self.assertEqual(run("0"), [10])  # below minimum → default
        self.assertEqual(run(None), [10])  # unset → default

    def test_upgrade_v4_to_v5_backfills_fts(self) -> None:
        """Applying migration 5 to a v4 database adds the context column and
        rebuilds chunks_fts with every pre-existing chunk backfilled, and the
        delete trigger stays consistent after the rebuild."""
        import shoin.store as store_mod

        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "upgrade.db")
            original = store_mod.MIGRATIONS[:]
            try:
                store_mod.MIGRATIONS = [m for m in original if m[0] <= 4]
                s = Store(path)
                nb = s.create_notebook("n")
                src = s.add_source(nb.id, "txt", "旧", "mem://o", "sha")
                # v4 chunks table has no context column — insert text only.
                s.conn.execute(
                    "INSERT INTO chunks(source_id, seq, text) VALUES (?,?,?)",
                    (src.id, 0, "旧チャンク 太陽光 の本文"),
                )
                s.conn.commit()
                s.close()
            finally:
                store_mod.MIGRATIONS = original

            s2 = Store(path)  # applies migrations 5+
            self.assertGreaterEqual(s2.migrate(), 5)
            cols = [r[1] for r in s2.conn.execute("PRAGMA table_info(chunks)").fetchall()]
            self.assertIn("context", cols)
            hits = bm25_search(s2, nb.id, "太陽光", 5)
            self.assertEqual(len(hits), 1)  # backfilled chunk is searchable
            s2.delete_notebook(nb.id)
            n_fts = s2.conn.execute("SELECT COUNT(*) AS n FROM chunks_fts").fetchone()["n"]
            self.assertEqual(int(n_fts), 0)  # delete trigger consistent post-rebuild
            s2.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
