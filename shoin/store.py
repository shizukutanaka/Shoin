"""SQLite persistence layer: migrations, notebooks, sources, chunks, FTS5.

Single-file database. Foreign keys + WAL. FTS5 uses the trigram tokenizer so
that CJK text is searchable without external tokenizers (SQLite >= 3.34).
"""

from __future__ import annotations

import array
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar, TypedDict

from .chunk import _MAX_CONTEXT_CHARS
from .config import MAX_NAME_LEN, MAX_TITLE_LEN

_T = TypeVar("_T")


def _retry_on_lock(fn: Callable[[], _T], attempts: int = 5) -> _T:
    """Retry `fn` when SQLite reports 'database is locked'.

    PRAGMA busy_timeout covers ordinary table-level lock contention, but a few
    operations (switching a brand-new file to WAL mode, the first migration on a
    shared file several threads open simultaneously) have a narrower lock window
    that busy_timeout doesn't fully close. Re-running is safe for both call sites
    that use this: PRAGMA journal_mode is idempotent, and migrate() re-reads the
    applied-version state from the DB before doing any work.
    """
    if attempts < 1:
        return fn()
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            last_exc = exc
            time.sleep(0.05 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


class _Counts(TypedDict):
    sources: int
    chunks: int


class NotebookWithCounts(TypedDict):
    id: int
    name: str
    counts: _Counts

# --- schema migrations (append-only; never edit a shipped entry) ---

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        # All DDL uses IF NOT EXISTS so that two concurrent migrations on the same
        # fresh file are idempotent: the second thread's DDL is a no-op after the
        # first thread commits.  CREATE TRIGGER IF NOT EXISTS requires SQLite ≥ 3.35.
        """
        CREATE TABLE IF NOT EXISTS notebooks(
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY,
          notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
          kind TEXT NOT NULL,
          title TEXT NOT NULL,
          origin TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          added_at TEXT NOT NULL,
          UNIQUE(notebook_id, sha256)
        );
        CREATE TABLE IF NOT EXISTS chunks(
          id INTEGER PRIMARY KEY,
          source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
          seq INTEGER NOT NULL,
          text TEXT NOT NULL,
          embedding BLOB
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
        CREATE TABLE IF NOT EXISTS notes(
          id INTEGER PRIMARY KEY,
          notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          body TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS studio_outputs(
          id INTEGER PRIMARY KEY,
          notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
          kind TEXT NOT NULL,
          body TEXT NOT NULL,
          citation_report TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY,
          notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
          role TEXT NOT NULL,
          body TEXT NOT NULL,
          citation_report TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
          text, content='chunks', content_rowid='id', tokenize='trigram'
        );
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
          INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
          INSERT INTO chunks_fts(chunks_fts, rowid, text)
          VALUES('delete', old.id, old.text);
        END;
        """,
    ),
    (
        2,
        """
        CREATE INDEX IF NOT EXISTS idx_sources_notebook ON sources(notebook_id);
        CREATE INDEX IF NOT EXISTS idx_notes_notebook ON notes(notebook_id);
        CREATE INDEX IF NOT EXISTS idx_studio_notebook ON studio_outputs(notebook_id);
        CREATE INDEX IF NOT EXISTS idx_messages_notebook ON messages(notebook_id);
        """,
    ),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """,
    ),
    (
        4,
        # Composite index lets list_messages_recent run in O(limit) instead of
        # O(total messages for notebook) by matching notebook_id then scanning
        # id DESC directly without a full-table sort.
        """
        CREATE INDEX IF NOT EXISTS idx_messages_notebook_id_desc ON messages(notebook_id, id DESC);
        """,
    ),
    (
        5,
        # Contextual chunk metadata: a per-chunk heading breadcrumb (chunk.py's
        # split_text_with_context) is indexed ALONGSIDE the chunk text so a query
        # term that appears in a section heading — but not the chunk body — still
        # retrieves that chunk (deterministic, LLM-free variant of Anthropic's
        # Contextual Retrieval, 2024). chunks_fts gains a `context` column and the
        # triggers mirror both columns. Rebuilt from scratch because FTS5 external-
        # content tables cannot ALTER in a column; DROP+CREATE+backfill is the
        # supported path. Existing chunks backfill with context='' (no heading data
        # on record) and degrade to the previous text-only behaviour until re-indexed.
        # All DDL is IF NOT EXISTS / idempotent so a concurrent re-run is a no-op.
        """
        ALTER TABLE chunks ADD COLUMN context TEXT NOT NULL DEFAULT '';
        DROP TRIGGER IF EXISTS chunks_ai;
        DROP TRIGGER IF EXISTS chunks_ad;
        DROP TABLE IF EXISTS chunks_fts;
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
          context, text, content='chunks', content_rowid='id', tokenize='trigram'
        );
        INSERT INTO chunks_fts(rowid, context, text)
          SELECT id, context, text FROM chunks;
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
          INSERT INTO chunks_fts(rowid, context, text)
          VALUES (new.id, new.context, new.text);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
          INSERT INTO chunks_fts(chunks_fts, rowid, context, text)
          VALUES('delete', old.id, old.context, old.text);
        END;
        """,
    ),
    (
        6,
        # UPDATE trigger for the external-content FTS index. Migrations 1/5 only
        # ever mirrored INSERT and DELETE because no code path updated chunk rows'
        # indexed columns — but update_source_title() now refreshes chunks.context
        # when a source is renamed (keeping the v0.2.123 title-in-context signal
        # fresh). Without this trigger, any UPDATE of text/context would silently
        # desynchronize chunks_fts from the chunks table: FTS5 external-content
        # tables see only what the triggers tell them, and a stale index is the
        # exact silent-staleness failure class this project repeatedly fixes.
        # Scoped to OF text, context so the frequent embedding-BLOB updates
        # (set_embedding) don't pay double FTS writes.
        """
        CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF text, context ON chunks BEGIN
          INSERT INTO chunks_fts(chunks_fts, rowid, context, text)
          VALUES('delete', old.id, old.context, old.text);
          INSERT INTO chunks_fts(rowid, context, text)
          VALUES (new.id, new.context, new.text);
        END;
        """,
    ),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def pack_vector(vec: list[float]) -> bytes:
    """Pack a float vector into a compact little-endian float32 BLOB."""
    return array.array("f", vec).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


@dataclass(frozen=True)
class Notebook:
    id: int
    name: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Source:
    id: int
    notebook_id: int
    kind: str
    title: str
    origin: str
    sha256: str
    added_at: str


@dataclass(frozen=True)
class Chunk:
    id: int
    source_id: int
    seq: int
    text: str
    embedding: list[float] | None


class StoreError(Exception):
    """Persistence error with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Store:
    """Thin typed wrapper around the Shoin SQLite database."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        # busy_timeout MUST be set before any statement that can block on a lock —
        # including journal_mode itself. Switching a brand-new file to WAL mode
        # briefly needs exclusive access to create the -wal/-shm files; when several
        # threads race to do this simultaneously on the same fresh file, whichever
        # PRAGMA runs first with no busy_timeout yet configured raised
        # 'database is locked' immediately instead of waiting.
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Even with busy_timeout set first, switching a brand-new file to WAL mode
        # is still occasionally reported by SQLite as locked when several threads
        # race to create the -wal/-shm files at the same instant (a narrower window
        # than ordinary table-level busy_timeout coverage). Retry defends against
        # that residual race; PRAGMA journal_mode is idempotent to re-run.
        _retry_on_lock(lambda: self.conn.execute("PRAGMA journal_mode = WAL"))
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- migrations ---

    def migrate(self) -> int:
        """Apply pending migrations. Returns the resulting schema version.

        Wrapped in _retry_on_lock: each retry re-reads `current` from the DB, so a
        retry after a partial failure is safe — the already-idempotent
        IF NOT EXISTS / INSERT OR IGNORE migrations below just skip what a previous
        attempt already applied.
        """
        return _retry_on_lock(self._migrate_once)

    def _migrate_once(self) -> int:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)"
        )
        self.conn.commit()
        row = self.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        current = int(row["v"] or 0)
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            # executescript() issues COMMIT before running, so `with self.conn:` cannot
            # protect the DDL + version-INSERT atomically — a crash between them leaves
            # tables in place but no version record, breaking every subsequent startup.
            # Embedding the INSERT inside BEGIN/COMMIT makes the whole migration one
            # atomic SQLite write (SQLite DDL is transactional).
            # INSERT OR IGNORE makes the migration idempotent: if two threads
            # both read current=N and race to apply the same migration, the
            # second thread's DDL (all IF NOT EXISTS, including the FTS5 virtual
            # table in migration 1) is a no-op and the duplicate INSERT is
            # silently ignored rather than crashing.
            self.conn.executescript(
                f"BEGIN;\n{sql.strip()}\n"
                f"INSERT OR IGNORE INTO schema_migrations(version) VALUES ({int(version)});\n"
                "COMMIT;"
            )
            current = version
        return current

    # --- notebooks ---

    def create_notebook(self, name: str) -> Notebook:
        name = name.strip()
        if not name:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "notebook name is empty")
        if len(name) > MAX_NAME_LEN:
            raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", f"name too long (max {MAX_NAME_LEN} chars)")
        ts = _now()
        cur = self.conn.execute(
            "INSERT INTO notebooks(name, created_at, updated_at) VALUES (?,?,?)",
            (name, ts, ts),
        )
        self.conn.commit()
        return Notebook(int(cur.lastrowid or 0), name, ts, ts)

    def get_notebook(self, notebook_id: int) -> Notebook:
        row = self.conn.execute("SELECT * FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
        if row is None:
            raise StoreError("NOTEBOOK_NOT_FOUND", f"notebook {notebook_id} not found")
        return Notebook(row["id"], row["name"], row["created_at"], row["updated_at"])

    def list_notebooks(self) -> list[Notebook]:
        rows = self.conn.execute("SELECT * FROM notebooks ORDER BY updated_at DESC").fetchall()
        return [Notebook(r["id"], r["name"], r["created_at"], r["updated_at"]) for r in rows]

    def rename_notebook(self, notebook_id: int, name: str) -> None:
        name = name.strip()
        if not name:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "notebook name is empty")
        if len(name) > MAX_NAME_LEN:
            raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", f"name too long (max {MAX_NAME_LEN} chars)")
        cur = self.conn.execute(
            "UPDATE notebooks SET name=?, updated_at=? WHERE id=?",
            (name, _now(), notebook_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            raise StoreError("NOTEBOOK_NOT_FOUND", f"notebook {notebook_id} not found")

    def delete_notebook(self, notebook_id: int) -> None:
        cur = self.conn.execute("DELETE FROM notebooks WHERE id=?", (notebook_id,))
        self.conn.commit()
        if cur.rowcount == 0:
            raise StoreError("NOTEBOOK_NOT_FOUND", f"notebook {notebook_id} not found")

    def touch_notebook(self, notebook_id: int) -> None:
        """Stamp the notebook's updated_at. Does NOT commit — callers must commit."""
        self.conn.execute("UPDATE notebooks SET updated_at=? WHERE id=?", (_now(), notebook_id))

    # --- sources / chunks ---

    def add_source(
        self, notebook_id: int, kind: str, title: str, origin: str, sha256: str
    ) -> Source:
        title = title[:MAX_TITLE_LEN]  # silently truncate; titles come from external content
        self.get_notebook(notebook_id)
        dup = self.conn.execute(
            "SELECT id FROM sources WHERE notebook_id=? AND sha256=?",
            (notebook_id, sha256),
        ).fetchone()
        if dup is not None:
            raise StoreError(
                "SOURCE_ALREADY_EXISTS",
                f"identical source already in notebook (source id {dup['id']})",
            )
        ts = _now()
        try:
            cur = self.conn.execute(
                "INSERT INTO sources(notebook_id, kind, title, origin, sha256, added_at)"
                " VALUES (?,?,?,?,?,?)",
                (notebook_id, kind, title, origin, sha256, ts),
            )
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e):
                raise StoreError(
                    "SOURCE_ALREADY_EXISTS",
                    "identical source already in notebook (concurrent upload)",
                )
            if "FOREIGN KEY" in str(e):
                raise StoreError(
                    "NOTEBOOK_NOT_FOUND",
                    f"notebook {notebook_id} was deleted during source addition",
                )
            # Unexpected constraint violation (e.g. CHECK, NOT NULL) — propagate
            # as a generic internal error rather than a misleading NOTEBOOK_NOT_FOUND.
            raise StoreError("SYSTEM_INTERNAL_ERROR", f"unexpected constraint violation: {e}") from e
        self.touch_notebook(notebook_id)
        self.conn.commit()
        return Source(int(cur.lastrowid or 0), notebook_id, kind, title, origin, sha256, ts)

    def update_source_title(self, source_id: int, title: str, origin: str) -> None:
        title = title.strip()[:MAX_TITLE_LEN]
        if not title:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "source title is empty")
        src = self.get_source(source_id)  # also validates existence; notebook_id needed below
        with self.conn:
            # Re-read the title INSIDE the transaction (not src.title from the
            # pre-transaction snapshot) so the chunk-context prefix rewrite below
            # keys off the row's actual current value — same stale-snapshot
            # concern the v0.2.98 COALESCE fix addressed for refresh.
            row = self.conn.execute(
                "SELECT title FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            if row is None:
                raise StoreError("SOURCE_NOT_FOUND", f"source {source_id} was concurrently deleted")
            old_title = str(row["title"])
            cur = self.conn.execute(
                "UPDATE sources SET title=?, origin=? WHERE id=?", (title, origin, source_id)
            )
            if cur.rowcount == 0:
                raise StoreError("SOURCE_NOT_FOUND", f"source {source_id} was concurrently deleted")
            if title != old_title:
                self._rewrite_chunk_context_titles(source_id, old_title, title)
            self.touch_notebook(src.notebook_id)

    def _rewrite_chunk_context_titles(
        self, source_id: int, old_title: str, new_title: str
    ) -> None:
        """Refresh the title prefix in each chunk's context after a source rename.

        v0.2.123 folds the source title into every chunk's context breadcrumb
        ("title > heading > …") for retrieval. Without this rewrite, a renamed
        source keeps matching FTS queries for its OLD title — and never matches
        its new one — indefinitely. Runs inside the caller's transaction (the
        migration-6 chunks_au trigger keeps chunks_fts in sync with each UPDATE).
        Rows whose context doesn't start with the old title (pre-migration-5
        backfills with context='', or a title truncated mid-word by the 200-char
        context cap) are left untouched — no match means no safe rewrite.
        """
        prefix = f"{old_title} > "
        rows = self.conn.execute(
            "SELECT id, context FROM chunks WHERE source_id=?", (source_id,)
        ).fetchall()
        for r in rows:
            ctx = str(r["context"])
            if ctx == old_title:
                new_ctx = new_title
            elif ctx.startswith(prefix):
                new_ctx = f"{new_title} > {ctx[len(prefix):]}"
            else:
                continue
            self.conn.execute(
                "UPDATE chunks SET context=? WHERE id=?",
                (new_ctx[:_MAX_CONTEXT_CHARS], r["id"]),
            )

    def sources_for_notebook(self, notebook_id: int) -> list[Source]:
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE notebook_id=? ORDER BY id", (notebook_id,)
        ).fetchall()
        return [
            Source(
                r["id"],
                r["notebook_id"],
                r["kind"],
                r["title"],
                r["origin"],
                r["sha256"],
                r["added_at"],
            )
            for r in rows
        ]

    def get_source(self, source_id: int) -> Source:
        row = self.conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise StoreError("SOURCE_NOT_FOUND", f"source {source_id} not found")
        return Source(
            row["id"],
            row["notebook_id"],
            row["kind"],
            row["title"],
            row["origin"],
            row["sha256"],
            row["added_at"],
        )

    def delete_source(self, source_id: int) -> None:
        src = self.get_source(source_id)  # raises SOURCE_NOT_FOUND if missing
        cur = self.conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        if cur.rowcount == 0:
            raise StoreError("SOURCE_NOT_FOUND", f"source {source_id} was concurrently deleted")
        self.touch_notebook(src.notebook_id)
        self.conn.commit()

    def replace_chunks_for_source(
        self,
        source_id: int,
        texts: list[str],
        *,
        sha256: str | None = None,
        title: str | None = None,
        contexts: list[str] | None = None,
    ) -> list[int]:
        """Atomically replace all chunks for a source (DELETE old + INSERT new).

        Used by refresh_source to update stale URL content while keeping the
        source ID intact (preserving citation history in stored messages).
        Raises SOURCE_NOT_FOUND if the source was concurrently deleted.

        When sha256 and title are provided, the source metadata is updated in the
        SAME transaction as the chunk replacement, eliminating the two-phase commit
        gap that previously existed between replace_chunks_for_source and the
        separate update_source_sha256 call in pipeline.refresh_source.
        """
        if not texts:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "replacement chunk list must not be empty")
        if contexts is not None and len(contexts) != len(texts):
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"contexts length ({len(contexts)}) must match texts ({len(texts)})",
            )
        src = self.get_source(source_id)  # raises SOURCE_NOT_FOUND if missing
        ids: list[int] = []
        try:
            with self.conn:
                self.conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
                for seq, text in enumerate(texts):
                    ctx = contexts[seq] if contexts is not None else ""
                    cur = self.conn.execute(
                        "INSERT INTO chunks(source_id, seq, text, context) VALUES (?,?,?,?)",
                        (source_id, seq, text, ctx),
                    )
                    ids.append(int(cur.lastrowid or 0))
                if sha256 is not None:
                    # COALESCE(?, title), not a Python-side `title or src.title` fallback:
                    # src.title was read by get_source() BEFORE this transaction began, so
                    # a concurrent PATCH /api/sources/{id} rename that commits in the window
                    # between that read and this UPDATE would be silently clobbered by the
                    # stale snapshot — reintroducing exactly the bug v0.2.87 fixed (refresh
                    # overwriting a user's custom title), just via a race instead of always.
                    # Resolving the fallback in SQL reads the CURRENT row value atomically.
                    new_title = title[:MAX_TITLE_LEN] if title is not None else None
                    meta_cur = self.conn.execute(
                        "UPDATE sources SET sha256=?, title=COALESCE(?, title) WHERE id=?",
                        (sha256, new_title, source_id),
                    )
                    if meta_cur.rowcount == 0:
                        raise StoreError("SOURCE_NOT_FOUND", f"source {source_id} was concurrently deleted")
                self.touch_notebook(src.notebook_id)
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e):
                raise StoreError("SOURCE_ALREADY_EXISTS", "refreshed content hash matches another existing source")
            if "FOREIGN KEY" in str(e):
                # chunks.source_id REFERENCES sources(id) ON DELETE CASCADE — this is
                # the genuine concurrent-deletion case: the source row was removed
                # between get_source() above and this INSERT.
                raise StoreError(
                    "SOURCE_NOT_FOUND", f"source {source_id} was deleted during chunk replacement"
                )
            # Unexpected constraint violation (e.g. CHECK, NOT NULL) — propagate as a
            # generic internal error rather than a misleading SOURCE_NOT_FOUND (mirrors
            # the same v0.2.53 fix already applied to add_source(), never ported here).
            raise StoreError("SYSTEM_INTERNAL_ERROR", f"unexpected constraint violation: {e}") from e
        return ids

    def update_source_sha256(self, source_id: int, sha256: str, title: str) -> None:
        """Update the content hash and title of a source after a refresh.

        Callers that need atomic chunk-replacement + metadata update should pass
        sha256/title to replace_chunks_for_source instead of calling this separately.
        This method is retained for callers that update metadata without replacing chunks.
        """
        title = title[:MAX_TITLE_LEN]
        src = self.get_source(source_id)  # raises SOURCE_NOT_FOUND if missing
        try:
            cur = self.conn.execute(
                "UPDATE sources SET sha256=?, title=? WHERE id=?", (sha256, title, source_id)
            )
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e):
                raise StoreError(
                    "SOURCE_ALREADY_EXISTS", "refreshed content hash matches another existing source"
                )
            # This is an UPDATE that never touches notebook_id, so no FOREIGN KEY
            # violation is possible here — anything else (e.g. a NOT NULL on the
            # sha256 column) is a genuine unexpected constraint violation, not a
            # duplicate-hash collision. Mirrors the v0.2.53/86/104 fix pattern.
            raise StoreError("SYSTEM_INTERNAL_ERROR", f"unexpected constraint violation: {e}") from e
        if cur.rowcount == 0:
            raise StoreError("SOURCE_NOT_FOUND", f"source {source_id} was concurrently deleted")
        self.touch_notebook(src.notebook_id)
        self.conn.commit()

    def add_chunks(
        self, source_id: int, texts: list[str], contexts: list[str] | None = None
    ) -> list[int]:
        if not texts:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "chunk list must not be empty")
        if contexts is not None and len(contexts) != len(texts):
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"contexts length ({len(contexts)}) must match texts ({len(texts)})",
            )
        ids: list[int] = []
        try:
            with self.conn:
                for seq, text in enumerate(texts):
                    ctx = contexts[seq] if contexts is not None else ""
                    cur = self.conn.execute(
                        "INSERT INTO chunks(source_id, seq, text, context) VALUES (?,?,?,?)",
                        (source_id, seq, text, ctx),
                    )
                    ids.append(int(cur.lastrowid or 0))
        except sqlite3.IntegrityError as e:
            if "FOREIGN KEY" in str(e):
                # chunks.source_id REFERENCES sources(id) — this is the genuine
                # concurrent-deletion case: the source row was removed mid-insert.
                raise StoreError(
                    "SOURCE_NOT_FOUND", f"source {source_id} was deleted during chunk insertion"
                )
            # Unexpected constraint violation (e.g. future CHECK, NOT NULL) — propagate
            # as a generic internal error rather than a misleading SOURCE_NOT_FOUND.
            # Mirrors the same v0.2.53 fix already applied to add_source() and
            # replace_chunks_for_source() (v0.2.86), never ported to this third sibling.
            raise StoreError("SYSTEM_INTERNAL_ERROR", f"unexpected constraint violation: {e}") from e
        return ids

    def set_embedding(self, chunk_id: int, vec: list[float], *, commit: bool = True) -> None:
        if not vec:
            raise StoreError("EMBEDDING_INVALID", "embedding vector must not be empty")
        cur = self.conn.execute(
            "UPDATE chunks SET embedding=? WHERE id=?", (pack_vector(vec), chunk_id)
        )
        if cur.rowcount == 0:
            raise StoreError("CHUNK_NOT_FOUND", f"chunk {chunk_id} not found")
        if commit:
            self.conn.commit()

    def chunks_for_notebook(self, notebook_id: int) -> list[Chunk]:
        rows = self.conn.execute(
            "SELECT c.* FROM chunks c JOIN sources s ON s.id=c.source_id"
            " WHERE s.notebook_id=? ORDER BY c.id",
            (notebook_id,),
        ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def get_chunk(self, chunk_id: int) -> Chunk:
        row = self.conn.execute("SELECT * FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        if row is None:
            raise StoreError("CHUNK_NOT_FOUND", f"chunk {chunk_id} not found")
        return self._row_to_chunk(row)

    def chunks_for_source(self, source_id: int) -> list[Chunk]:
        rows = self.conn.execute(
            "SELECT * FROM chunks WHERE source_id=? ORDER BY seq", (source_id,)
        ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def text_chunks_for_source(self, source_id: int) -> list[tuple[int, str]]:
        """Return (seq, text) pairs for a source without loading embedding data."""
        rows = self.conn.execute(
            "SELECT seq, text FROM chunks WHERE source_id=? ORDER BY seq", (source_id,)
        ).fetchall()
        return [(int(r["seq"]), str(r["text"])) for r in rows]

    def id_text_chunks_for_notebook(self, notebook_id: int) -> list[tuple[int, str]]:
        """Return (chunk_id, text) pairs for all chunks in a notebook without embeddings."""
        rows = self.conn.execute(
            "SELECT c.id, c.text FROM chunks c JOIN sources s ON s.id=c.source_id"
            " WHERE s.notebook_id=? ORDER BY c.id",
            (notebook_id,),
        ).fetchall()
        return [(int(r["id"]), str(r["text"])) for r in rows]

    def id_context_text_chunks_for_notebook(
        self, notebook_id: int
    ) -> list[tuple[int, str, str]]:
        """Return (chunk_id, context, text) triples for all chunks in a notebook.

        Used by reindex_notebook so re-embedding feeds the model the SAME
        context+text string index_source did — otherwise a reindexed notebook
        would mix context-aware and text-only vectors that no longer compare.
        """
        rows = self.conn.execute(
            "SELECT c.id, c.context, c.text FROM chunks c JOIN sources s ON s.id=c.source_id"
            " WHERE s.notebook_id=? ORDER BY c.id",
            (notebook_id,),
        ).fetchall()
        return [(int(r["id"]), str(r["context"]), str(r["text"])) for r in rows]

    @staticmethod
    def _row_to_chunk(r: sqlite3.Row) -> Chunk:
        blob: Any = r["embedding"]
        emb = unpack_vector(blob) if blob is not None else None
        return Chunk(r["id"], r["source_id"], r["seq"], r["text"], emb)

    # --- notes / studio outputs ---

    def add_note(self, notebook_id: int, title: str, body: str) -> int:
        title = title.strip()
        if not title:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "note title is empty")
        if len(title) > MAX_NAME_LEN:
            raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", f"title too long (max {MAX_NAME_LEN} chars)")
        self.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
        try:
            cur = self.conn.execute(
                "INSERT INTO notes(notebook_id, title, body, created_at) VALUES (?,?,?,?)",
                (notebook_id, title, body, _now()),
            )
        except sqlite3.IntegrityError as e:
            if "FOREIGN KEY" not in str(e):
                # notes has no UNIQUE constraint, so the only expected IntegrityError
                # here is the FK on notebook_id (genuine concurrent deletion).
                # Mirrors the v0.2.53/86/104 fix pattern.
                raise StoreError("SYSTEM_INTERNAL_ERROR", f"unexpected constraint violation: {e}") from e
            raise StoreError(
                "NOTEBOOK_NOT_FOUND",
                f"notebook {notebook_id} was deleted during note insertion",
            )
        self.touch_notebook(notebook_id)
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def list_notes(self, notebook_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM notes WHERE notebook_id=? ORDER BY id", (notebook_id,)
            ).fetchall()
        )

    def delete_note(self, note_id: int) -> None:
        row = self.conn.execute("SELECT notebook_id FROM notes WHERE id=?", (note_id,)).fetchone()
        if row is None:
            raise StoreError("NOTE_NOT_FOUND", f"note {note_id} not found")
        cur = self.conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
        if cur.rowcount == 0:
            raise StoreError("NOTE_NOT_FOUND", f"note {note_id} was concurrently deleted")
        self.touch_notebook(int(row["notebook_id"]))
        self.conn.commit()

    def add_studio_output(
        self, notebook_id: int, kind: str, body: str, citation_report: str
    ) -> int:
        self.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
        try:
            cur = self.conn.execute(
                "INSERT INTO studio_outputs(notebook_id, kind, body, citation_report,"
                " created_at) VALUES (?,?,?,?,?)",
                (notebook_id, kind, body, citation_report, _now()),
            )
        except sqlite3.IntegrityError as e:
            if "FOREIGN KEY" not in str(e):
                # studio_outputs has no UNIQUE constraint, so the only expected
                # IntegrityError here is the FK on notebook_id (genuine concurrent
                # deletion). Mirrors the v0.2.53/86/104 fix pattern.
                raise StoreError("SYSTEM_INTERNAL_ERROR", f"unexpected constraint violation: {e}") from e
            raise StoreError(
                "NOTEBOOK_NOT_FOUND",
                f"notebook {notebook_id} was deleted during studio output insertion",
            )
        self.touch_notebook(notebook_id)
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def latest_studio_outputs(self, notebook_id: int) -> list[sqlite3.Row]:
        """Latest output per kind."""
        return list(
            self.conn.execute(
                "SELECT * FROM studio_outputs WHERE notebook_id=? AND id IN ("
                " SELECT MAX(id) FROM studio_outputs WHERE notebook_id=? GROUP BY kind)"
                " ORDER BY kind",
                (notebook_id, notebook_id),
            ).fetchall()
        )

    # --- messages ---

    def add_message(
        self, notebook_id: int, role: str, body: str, citation_report: str = "{}"
    ) -> int:
        self.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
        try:
            cur = self.conn.execute(
                "INSERT INTO messages(notebook_id, role, body, citation_report, created_at)"
                " VALUES (?,?,?,?,?)",
                (notebook_id, role, body, citation_report, _now()),
            )
        except sqlite3.IntegrityError as e:
            if "FOREIGN KEY" not in str(e):
                # messages has no UNIQUE constraint, so the only expected
                # IntegrityError here is the FK on notebook_id (genuine concurrent
                # deletion). Mirrors the v0.2.53/86/104 fix pattern.
                raise StoreError("SYSTEM_INTERNAL_ERROR", f"unexpected constraint violation: {e}") from e
            raise StoreError(
                "NOTEBOOK_NOT_FOUND",
                f"notebook {notebook_id} was deleted during message insertion",
            )
        self.touch_notebook(notebook_id)
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def list_messages(self, notebook_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM messages WHERE notebook_id=? ORDER BY id", (notebook_id,)
            ).fetchall()
        )

    def list_messages_recent(self, notebook_id: int, limit: int) -> list[sqlite3.Row]:
        """Most recent *limit* messages in chronological order (avoids full scan)."""
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE notebook_id=? ORDER BY id DESC LIMIT ?",
            (notebook_id, limit),
        ).fetchall()
        return list(reversed(rows))

    def clear_messages(self, notebook_id: int) -> None:
        self.get_notebook(notebook_id)
        self.conn.execute("DELETE FROM messages WHERE notebook_id=?", (notebook_id,))
        self.touch_notebook(notebook_id)
        self.conn.commit()

    def counts(self, notebook_id: int) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT s.id) AS sources, COUNT(c.id) AS chunks"
            " FROM sources s LEFT JOIN chunks c ON c.source_id = s.id"
            " WHERE s.notebook_id = ?",
            (notebook_id,),
        ).fetchone()
        return {"sources": int(row["sources"]), "chunks": int(row["chunks"])}

    def list_notebooks_with_counts(self) -> list[NotebookWithCounts]:
        """Return all notebooks with source/chunk counts in a single query (avoids N+1)."""
        rows = self.conn.execute(
            "SELECT n.id, n.name,"
            " COUNT(DISTINCT s.id) AS sources,"
            " COUNT(DISTINCT c.id) AS chunks"
            " FROM notebooks n"
            " LEFT JOIN sources s ON s.notebook_id = n.id"
            " LEFT JOIN chunks c ON c.source_id = s.id"
            " GROUP BY n.id"
            " ORDER BY n.updated_at DESC"
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "name": str(r["name"]),
                "counts": {"sources": int(r["sources"]), "chunks": int(r["chunks"])},
            }
            for r in rows
        ]

    # --- settings ---------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        """Return a stored setting value, or None if the key has never been set."""
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Upsert a setting key/value pair."""
        self.conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)",
            (key, value),
        )
        self.conn.commit()
