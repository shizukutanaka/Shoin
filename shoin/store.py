"""SQLite persistence layer: migrations, notebooks, sources, chunks, FTS5.

Single-file database. Foreign keys + WAL. FTS5 uses the trigram tokenizer so
that CJK text is searchable without external tokenizers (SQLite >= 3.34).
"""

from __future__ import annotations

import array
import base64
import contextlib
import json
import math
import operator
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, TypeVar

from .chunk import _MAX_CONTEXT_CHARS
from .config import (
    MAX_NAME_LEN,
    MAX_TITLE_LEN,
    NB_SETTING_KEYS,
    NB_SOURCE_TEXT_TOKENS_MAX,
    NB_SOURCE_TEXT_TOKENS_MIN,
    SEARCH_K_MAX,
    SOURCE_META_MAX,
    SOURCE_WEIGHT_MAX,
    data_dir,
)

_T = TypeVar("_T")

# The studio-output kind vocabulary. Defined here — not in studio.py — so the
# store's own write guard can check it without a circular import; studio.py
# re-exports it as KINDS for its callers.
STUDIO_KINDS = ("briefing", "study_guide", "faq", "timeline", "mindmap")

# The source kind vocabulary: ingest._EXT_KIND values plus "url". The store
# guards writes on it for the same reason as STUDIO_KINDS — export's RIS TY
# mapping (_RIS_TYPE), the md legend, and the UI badge all consume kind, so a
# typo'd literal silently degrades exported citations and the source list.
SOURCE_KINDS = ("txt", "md", "html", "pdf", "url")

# Settings-table key prefix for the usage counters written by bump_metrics —
# the prefix is what usage_metrics scans for, and it keeps product counters
# out of any future user-facing settings namespace.
_METRIC_PREFIX = "metric."


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
    if last_exc is None:
        raise AssertionError("unreachable: lock-retry loop exited without an exception")
    raise last_exc


def _utf8(value: object, field: str) -> None:
    """Return *value* unchanged, or raise a coded StoreError when it is not
    UTF-8 encodable.

    sqlite3 encodes bound str parameters as strict UTF-8: a lone surrogate
    reaches a write as a raw UnicodeEncodeError — not a StoreError — so it
    bypasses every caller's coded-error mapping (HTTP 400, CLI err.prefix).
    Such strings really do arrive: POSIX argv/env decode invalid bytes via
    surrogateescape (CLI subcommands, `shoin add` of a filename with non-UTF-8
    bytes, SHOIN_* env vars), and the API layer's field gate does not cover
    values derived downstream (a source title taken from such a filename).
    Checking at the write keeps the coded contract regardless of caller.
    """
    if not isinstance(value, str):
        # Not this gate's problem — e.g. a None body is the column's own
        # NOT NULL violation, already mapped to SYSTEM_INTERNAL_ERROR.
        return
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as e:
        raise StoreError(
            "VALIDATION_FIELD_FORMAT_INVALID",
            f"{field} is not UTF-8 encodable",
        ) from e


class _Counts(TypedDict):
    sources: int
    chunks: int


class NotebookWithCounts(TypedDict):
    id: int
    name: str
    counts: _Counts


class TrashItem(TypedDict):
    id: int
    notebook_id: int
    name: str
    deleted_at: str
    kind: str

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
    (
        7,
        # Cache each embedding's own L2 norm next to it. vector_search compares one
        # query against every chunk, and recomputing sqrt(sum(v*v)) per row was 54%
        # of the remaining per-chunk cost (measured, v0.2.162) for a value that never
        # changes between queries. Safe to cache because set_embedding() is the ONLY
        # writer of chunks.embedding in the codebase and now writes both columns in
        # the same UPDATE, so the pair cannot drift. NULL means "not computed yet"
        # (rows written before this migration): vector_search falls back to computing
        # it, so an un-reindexed notebook keeps identical scores, just slower.
        # ALTER TABLE has no IF NOT EXISTS in SQLite; _migrate_once() handles the
        # concurrent "duplicate column name" case (v0.2.128).
        """
        ALTER TABLE chunks ADD COLUMN embedding_norm REAL;
        """,
    ),
    (
        8,
        # Invalidate the cached norm whenever the embedding is rewritten WITHOUT
        # the norm being rewritten in the same statement — which is exactly what a
        # binary older than v0.2.164 does, since it does not know the column exists.
        # Reproduced before this trigger existed: an old binary's write left the
        # previous vector's norm in place and vector_search returned 3.0 for a pair
        # whose true cosine is 0.6 — outside cosine's range, so it would dominate
        # every ranking. CLAUDE.md records downgrade safety (an older binary opening
        # a newer schema keeps working) as a property of this project; migration 7
        # narrowed it, and this restores it.
        #
        # The guard lives in the DATABASE precisely because the offending writer is
        # a binary that knows nothing about any of this. set_embedding (the only
        # in-repo writer) updates both columns, so new.embedding_norm differs and
        # the trigger stays quiet; in the one case where a new vector happens to
        # have the identical norm, the trigger fires, the norm goes NULL, and
        # vector_search recomputes it — still correct, merely uncached.
        """
        CREATE TRIGGER IF NOT EXISTS chunks_norm_invalidate
        AFTER UPDATE OF embedding ON chunks
        WHEN new.embedding_norm IS old.embedding_norm
        BEGIN
          UPDATE chunks SET embedding_norm = NULL WHERE id = new.id;
        END;
        """,
    ),
    (
        9,
        # Migration 8's WHEN clause tried to recognise an old binary's write by
        # "the norm did not change in this statement". It also matched a case it
        # must not: re-embedding UNCHANGED content with the same model produces the
        # same vector and therefore the same norm, so `shoin reindex` — the very
        # repair action the docs point at — discarded every correct cached norm and
        # left search permanently on the slow fallback path (measured: 10/10 rows
        # NULL after a reindex).
        #
        # The condition is deleted instead of refined. set_embedding now writes the
        # embedding and the norm as two statements in one transaction, so the
        # trigger can fire unconditionally on the first and the second restores the
        # cache — no value coincidence can confuse it. An old binary issues only the
        # first statement, so it still lands on NULL and the correct fallback.
        """
        DROP TRIGGER IF EXISTS chunks_norm_invalidate;
        CREATE TRIGGER IF NOT EXISTS chunks_norm_invalidate
        AFTER UPDATE OF embedding ON chunks
        BEGIN
          UPDATE chunks SET embedding_norm = NULL WHERE id = new.id;
        END;
        """,
    ),
    (
        10,
        # Undo-log trash archive: delete_notebook serializes the whole
        # notebook tree to JSON (chunk embedding BLOBs base64-tagged)
        # and writes it here in the same transaction as the cascade
        # DELETE — a delete can never commit without its undo record.
        # Deliberately NOT a deleted_at flag on notebooks: a soft-delete
        # marker would force every read path (list/get/retrieve x4/
        # counts/export/ask) to learn the filter, and one missed path
        # silently leaks trashed content. No FK to notebooks — the
        # parent row is deleted by design. notebook_id/name/deleted_at
        # are duplicated as columns so trash_list() never parses payloads.
        """
        CREATE TABLE IF NOT EXISTS trash_items(
          id INTEGER PRIMARY KEY,
          notebook_id INTEGER NOT NULL,
          name TEXT NOT NULL,
          deleted_at TEXT NOT NULL,
          payload TEXT NOT NULL
        );
        """,
    ),
    (
        11,
        # Per-source retrieval weight (v0.2.657, product-review #19):
        # retrieve()/retrieve_multi() multiply each fused hit's score by its
        # source's weight, so an authoritative document can outrank a pasted
        # scratch note. NOT NULL DEFAULT 1.0 keeps every pre-existing source
        # neutral — the migration alone cannot shift a single ranking. ALTER
        # TABLE has no IF NOT EXISTS; _migrate_once() absorbs the concurrent
        # "duplicate column name" case (same contract as migration 7).
        """
        ALTER TABLE sources ADD COLUMN weight REAL NOT NULL DEFAULT 1.0;
        """,
    ),
    (
        12,
        # Per-source metadata (v0.2.658, product-review #24): a JSON object
        # column carrying citation-descriptive fields (author, year, …) that
        # titles/origins cannot express. '{}'-defaulted so every pre-existing
        # source reads as having empty metadata — the migration alone changes
        # nothing visible. Same ALTER TABLE concurrency contract as the
        # weight column above.
        """
        ALTER TABLE sources ADD COLUMN meta TEXT NOT NULL DEFAULT '{}';
        """,
    ),
    (
        13,
        # Per-notebook settings (v0.2.659, product-review #20): a JSON
        # object column for retrieval overrides (top_k,
        # source_text_tokens) that were previously process-global
        # constants only. '{}' means "use the global defaults" — the
        # migration alone changes no retrieval behavior.
        """
        ALTER TABLE notebooks ADD COLUMN settings TEXT NOT NULL DEFAULT '{}';
        """,
    ),
    (
        14,
        # Trash kind column (v0.2.667): the undo-log generalizes from
        # notebook-only archives to per-entity payloads — a source (with
        # its chunks and embeddings) or a single note. Every pre-existing
        # row is a notebook tree, so 'notebook' backfills truthfully;
        # new archives write the payload's kind explicitly. trash_list()
        # surfaces kind so the two archive classes read differently
        # without ever parsing payloads.
        """
        ALTER TABLE trash_items ADD COLUMN kind TEXT NOT NULL DEFAULT 'notebook';
        """,
    ),
]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _meta_dump(meta: dict[str, Any]) -> str:
    """Canonical serialization for a source's meta object.

    sort_keys + tight separators make logically-equal dicts byte-identical
    in the column — a re-PATCH of the same fields cannot churn storage,
    and round-trip tests compare strings, not dict ordering.
    """
    return json.dumps(
        meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _meta_text(value: Any) -> str:
    """Normalize a payload's `meta` field to canonical JSON text.

    Accepts the two shapes a tree payload can legitimately carry: the raw
    TEXT column (a JSON string, our own export/archive form) or a JSON
    object (a foreign generator's natural spelling). Everything else is
    garbage — raise ValueError so callers funnel it into their own coded
    error (NOTEBOOK_IMPORT_INVALID at import, SYSTEM_INTERNAL_ERROR at
    trash restore). Missing/None means the pre-column default '{}'.
    """
    if value is None:
        return "{}"
    if isinstance(value, dict):
        return _meta_dump(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("meta is not a JSON object")
        return _meta_dump(parsed)
    raise ValueError("meta is not a JSON object")


def _settings_of(row: sqlite3.Row) -> dict[str, Any]:
    """Parse a notebooks row's settings column for the Notebook dataclass.

    A MIGRATIONS-truncated test fixture — or a row read against a file
    mid-upgrade — can carry no settings column at all: the honest read is
    the same '{}' the column's DEFAULT produces once migration 13 lands.
    A non-object stored value also degrades to {} rather than faulting
    the read path (update_notebook_settings is the only writer, and it
    cannot emit one).
    """
    if "settings" not in row.keys():
        return {}
    parsed = json.loads(row["settings"])
    return parsed if isinstance(parsed, dict) else {}


def pack_vector(vec: list[float]) -> bytes:
    """Pack a float vector into a compact little-endian float32 BLOB."""
    return array.array("f", vec).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


def _remap_report_source_ids(
    report_json: str | None, id_map: dict[Any, int]
) -> str | None:
    """Rewrite a citation_report's source_id_map through an id remap.

    Reports carry real source ids ({"S1": 4}) — a verbatim copy across
    duplicate/import leaves dead or wrong pointers once the tree is
    re-inserted under fresh ids. S# keys and every other field pass
    through; an entry whose source did not come along is dropped rather
    than left pointing at a dead row. A non-JSON or non-dict report
    passes through verbatim (corrupt-report convention).
    """
    if report_json is None:
        return None
    try:
        report = json.loads(report_json)
        sim = report.get("source_id_map")
        if isinstance(sim, dict):
            mapped: dict[str, int] = {}
            for k, v in sim.items():
                # bool is an int subclass — never a source id.
                if isinstance(v, int) and not isinstance(v, bool) and v in id_map:
                    mapped[str(k)] = id_map[v]
            report["source_id_map"] = mapped
        return json.dumps(report, ensure_ascii=False)
    except (ValueError, TypeError, AttributeError):
        return report_json


@dataclass(frozen=True)
class Notebook:
    id: int
    name: str
    created_at: str
    updated_at: str
    # Per-notebook retrieval overrides (v0.2.659), parsed from the
    # settings column on read. Only NB_SETTING_KEYS act; {} = global
    # defaults.
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Source:
    id: int
    notebook_id: int
    kind: str
    title: str
    origin: str
    sha256: str
    added_at: str
    weight: float = 1.0
    # Freeform JSON object of descriptive metadata (author/year/…), parsed
    # from the meta column on read — never a raw TEXT leak to callers.
    meta: dict[str, Any] = field(default_factory=dict)


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
        self._db_file: Path | None = None
        if path != ":memory:":
            db_file = Path(path)
            self._db_file = db_file
            db_file.parent.mkdir(parents=True, exist_ok=True)
            # Pre-create at 0600 so the DB is born private: it holds the user's
            # documents and chat history, and would otherwise sit umask-readable
            # (644) to other users on a shared system until the chmod below.
            fd = os.open(db_file, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
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
        if path != ":memory:":
            # Repair permissions on existing installs too: the data dir itself
            # is tightened only when it is the app's own (a --db path inside a
            # foreign directory tightens the file but never that directory).
            # SQLite already gives -wal/-shm sidecars the DB file's mode; the
            # glob covers sidecars and DBs left world-readable before this fix.
            if db_file.parent == data_dir():
                os.chmod(db_file.parent, 0o700)
            for f in db_file.parent.glob(db_file.name + "*"):
                # Never chmod through a symlink: inside a shared --db parent a
                # planted link would tighten an arbitrary file it points to.
                if f.is_symlink():
                    continue
                with contextlib.suppress(OSError):
                    os.chmod(f, 0o600)

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
            #
            # EXCEPTION: `ALTER TABLE ... ADD COLUMN` (migration 5) has no
            # `IF NOT EXISTS` form in SQLite, so it is NOT naturally idempotent
            # like every other statement here. server.py opens a fresh Store()
            # per HTTP request under ThreadingHTTPServer, so two concurrent
            # requests against a shared file still on the old schema can both
            # read the same `current` and both attempt this migration. SQLite
            # allows only one writer: the loser's own `BEGIN` blocks (governed
            # by busy_timeout) until the winner commits, then the loser's ALTER
            # TABLE statement executes immediately after and fails with
            # "duplicate column name" — a genuine constraint violation, not lock
            # contention, so it does NOT contain "locked" and _retry_on_lock's
            # blanket substring check never retries it.
            try:
                self.conn.executescript(
                    f"BEGIN;\n{sql.strip()}\n"
                    f"INSERT OR IGNORE INTO schema_migrations(version) VALUES ({int(version)});\n"
                    "COMMIT;"
                )
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise
                # The failing ALTER TABLE aborted this script before COMMIT,
                # leaving this connection's own transaction open with nothing
                # applied (the ALTER never took effect) — roll it back before
                # reusing the connection. Then check whether a concurrent
                # winner already fully committed this exact version: if so,
                # this migration's effect already exists in the DB and it's
                # safe to skip; otherwise it's a genuine, unexpected schema
                # conflict (not this migration's own column) and must propagate.
                self.conn.rollback()
                winner = self.conn.execute(
                    "SELECT MAX(version) AS v FROM schema_migrations"
                ).fetchone()
                if int(winner["v"] or 0) < version:
                    raise
            current = version
        return current

    # --- notebooks ---  (REQ-001: CRUD; delete cascades sources/notes/messages)

    def create_notebook(self, name: str) -> Notebook:
        name = name.strip()
        if not name:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "notebook name is empty")
        if len(name) > MAX_NAME_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"name too long (max {MAX_NAME_LEN} chars)",
            )
        _utf8(name, "name")
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
        return Notebook(
            row["id"],
            row["name"],
            row["created_at"],
            row["updated_at"],
            _settings_of(row),
        )

    def list_notebooks(self) -> list[Notebook]:
        rows = self.conn.execute(
            "SELECT * FROM notebooks ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        return [
            Notebook(
                r["id"],
                r["name"],
                r["created_at"],
                r["updated_at"],
                _settings_of(r),
            )
            for r in rows
        ]

    def rename_notebook(self, notebook_id: int, name: str) -> None:
        name = name.strip()
        if not name:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "notebook name is empty")
        if len(name) > MAX_NAME_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"name too long (max {MAX_NAME_LEN} chars)",
            )
        _utf8(name, "name")
        cur = self.conn.execute(
            "UPDATE notebooks SET name=?, updated_at=? WHERE id=?",
            (name, _now(), notebook_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            raise StoreError("NOTEBOOK_NOT_FOUND", f"notebook {notebook_id} not found")

    def notebook_settings(self, notebook_id: int) -> dict[str, Any]:
        """Read-side accessor for per-notebook retrieval overrides
        (v0.2.659). Read-only — returns {} for a missing notebook so the
        retrieval path never turns a settings lookup into a 404 of its
        own (a dead notebook already returns no hits)."""
        row = self.conn.execute(
            "SELECT settings FROM notebooks WHERE id=?", (notebook_id,)
        ).fetchone()
        if row is None:
            return {}
        parsed = json.loads(row["settings"])
        return parsed if isinstance(parsed, dict) else {}

    def update_notebook_settings(
        self, notebook_id: int, settings: dict[str, Any]
    ) -> None:
        """Replace a notebook's retrieval-override object (v0.2.659, #20).

        Whole-object REPLACE like update_source_meta — PATCH/CLI merge
        client-side. Unlike source meta this is NOT freeform: only
        NB_SETTING_KEYS act, so an unknown key would be a silently inert
        setting — rejected instead. Bounds mirror the global constants
        each key overrides (config.py). Calls touch_notebook: settings
        change generated output (retrieval depth / prompt budget), the
        same content-bearing class as rename.
        """
        if not isinstance(settings, dict):
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"settings must be a JSON object, got {type(settings).__name__}",
            )
        unknown = sorted(k for k in settings if k not in NB_SETTING_KEYS)
        if unknown:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"unknown settings keys: {', '.join(unknown)} "
                f"(allowed: {', '.join(NB_SETTING_KEYS)})",
            )
        bounds = {
            "top_k": (1, SEARCH_K_MAX),
            "source_text_tokens": (
                NB_SOURCE_TEXT_TOKENS_MIN,
                NB_SOURCE_TEXT_TOKENS_MAX,
            ),
        }
        for key, value in settings.items():
            lo, hi = bounds[key]
            if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
                raise StoreError(
                    "VALIDATION_FIELD_FORMAT_INVALID",
                    f"settings.{key} must be an integer in {lo}..{hi}",
                )
        text = _meta_dump(settings)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE notebooks SET settings=? WHERE id=?",
                (text, notebook_id),
            )
            if cur.rowcount == 0:
                raise StoreError(
                    "NOTEBOOK_NOT_FOUND", f"notebook {notebook_id} not found"
                )
            self.touch_notebook(notebook_id)

    def _notebook_tree_dict(self, notebook_id: int) -> dict[str, Any]:
        """Serialize the whole notebook tree for trash archive / export.

        Column fidelity over the ORM projections: raw rows keep every
        field (context, embedding_norm, citation_report) so a restore
        or import re-inserts byte-identical children. Embedding BLOBs
        are base64-tagged — verbatim-valid on restore, zero re-embed
        cost. "format" stamps the document kind: a trash payload IS an
        export document, so import/trash-restore share one envelope.
        """
        nb = self.get_notebook(notebook_id)
        payload: dict[str, Any] = {
            "format": "shoin-nb-tree-v1",
            "notebook": {
                "id": nb.id,
                "name": nb.name,
                "created_at": nb.created_at,
                "updated_at": nb.updated_at,
                "settings": nb.settings,
            },
            "sources": [],
            "chunks": [],
            "notes": [],
            "studio_outputs": [],
            "messages": [],
        }
        src_ids: list[int] = []
        for r in self.conn.execute(
            "SELECT * FROM sources WHERE notebook_id=? ORDER BY id",
            (notebook_id,),
        ):
            payload["sources"].append(dict(r))
            src_ids.append(int(r["id"]))
        for sid in src_ids:
            payload["chunks"].extend(self._chunk_dicts(sid))
        payload["notes"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM notes WHERE notebook_id=? ORDER BY id",
                (notebook_id,),
            )
        ]
        payload["studio_outputs"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM studio_outputs WHERE notebook_id=? ORDER BY id",
                (notebook_id,),
            )
        ]
        payload["messages"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM messages WHERE notebook_id=? ORDER BY id",
                (notebook_id,),
            )
        ]
        return payload

    def _notebook_tree_payload(self, notebook_id: int) -> str:
        return json.dumps(
            self._notebook_tree_dict(notebook_id), ensure_ascii=False
        )

    def _chunk_dicts(self, source_id: int) -> list[dict[str, Any]]:
        """Raw chunk rows for one source with embedding BLOBs base64-tagged.

        Shared by the notebook-tree serializer and the source-level trash
        archive (v0.2.667) — the same verbatim-valid, zero-re-embed shape.
        """
        rows: list[dict[str, Any]] = []
        for r in self.conn.execute(
            "SELECT * FROM chunks WHERE source_id=? ORDER BY seq", (source_id,)
        ):
            row = dict(r)
            if row["embedding"] is not None:
                row["embedding"] = {
                    "$blob": base64.b64encode(row["embedding"]).decode("ascii")
                }
            rows.append(row)
        return rows

    def delete_notebook(self, notebook_id: int) -> None:
        # Undo-log trash (v0.2.654): archive-then-delete in ONE
        # transaction — the undo record can never be missing for a
        # committed delete, and live read paths need no filter changes.
        nb = self.get_notebook(notebook_id)
        payload = self._notebook_tree_payload(notebook_id)
        with self.conn:
            self.conn.execute(
                "INSERT INTO trash_items(notebook_id, name, deleted_at, payload)"
                " VALUES(?,?,?,?)",
                (notebook_id, nb.name, _now(), payload),
            )
            self.conn.execute("DELETE FROM notebooks WHERE id=?", (notebook_id,))

    def trash_list(self) -> list[TrashItem]:
        """Newest-first trash index — columns only, payload never parsed."""
        rows = self.conn.execute(
            "SELECT id, notebook_id, name, deleted_at, kind"
            " FROM trash_items ORDER BY deleted_at DESC, id DESC"
        ).fetchall()
        return [
            TrashItem(
                id=int(r["id"]),
                notebook_id=int(r["notebook_id"]),
                name=str(r["name"]),
                deleted_at=str(r["deleted_at"]),
                kind=str(r["kind"]),
            )
            for r in rows
        ]

    def trash_restore(self, trash_id: int) -> dict[str, Any]:
        """Re-insert a trashed entity in one TX, dispatching on payload kind.

        Notebook archives (v0.2.654) re-insert with their original ids —
        chunk INSERTs re-fire the FTS triggers and base64 vectors land
        verbatim, so a restored tree is searchable immediately at zero
        re-embed cost. Source archives (v0.2.667) follow the same
        contract inside their parent notebook, which must still exist
        (NOTEBOOK_NOT_FOUND): an occupied source id refuses with
        SOURCE_ALREADY_EXISTS, never a silent merge or id rewrite.
        Note archives get a fresh id — nothing outside delete/list
        references note ids, so re-assignment loses nothing and cannot
        collide. INTEGER PRIMARY KEY reuses max(id)+1, so every
        occupied-id refusal is a real conflict, not paranoia.
        Returns {"kind", "id", "name"} — the restored entity's identity.
        """
        row = self.conn.execute(
            "SELECT payload FROM trash_items WHERE id=?", (trash_id,)
        ).fetchone()
        if row is None:
            raise StoreError("TRASH_NOT_FOUND", f"trash item {trash_id} not found")
        try:
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                raise ValueError("trash payload is not an object")
            kind = str(payload.get("kind", "notebook"))
            if kind == "notebook":
                nb = payload["notebook"]
                sources = payload["sources"]
                chunks = payload["chunks"]
                notes = payload["notes"]
                studio_outputs = payload["studio_outputs"]
                messages = payload["messages"]
                for c in chunks:
                    if c["embedding"] is not None:
                        c["embedding"] = base64.b64decode(c["embedding"]["$blob"])
                # Normalize meta inside the same corrupt-payload boundary — a
                # garbage meta field is the same defect class as a broken
                # embedding tag, and must surface SYSTEM_INTERNAL_ERROR, not
                # a raw ValueError escaping mid-transaction.
                for s in sources:
                    s["meta"] = _meta_text(s.get("meta"))
                nb["settings"] = _meta_text(nb.get("settings"))
            elif kind == "source":
                src = payload["source"]
                chunks = payload["chunks"]
                for c in chunks:
                    if c["embedding"] is not None:
                        c["embedding"] = base64.b64decode(c["embedding"]["$blob"])
                src["meta"] = _meta_text(src.get("meta"))
                nb_id = int(src["notebook_id"])
            elif kind == "note":
                note = payload["note"]
                nb_id = int(note["notebook_id"])
            else:
                raise ValueError(f"unknown trash kind {kind!r}")
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError(
                "SYSTEM_INTERNAL_ERROR",
                f"trash item {trash_id} payload is corrupt",
            ) from exc
        if kind == "notebook":
            return self._restore_notebook_tree(
                nb, sources, chunks, notes, studio_outputs, messages, trash_id
            )
        if kind == "source":
            return self._restore_trashed_source(src, chunks, nb_id, trash_id)
        return self._restore_trashed_note(note, nb_id, trash_id)

    def _restore_notebook_tree(
        self,
        nb: dict[str, Any],
        sources: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        notes: list[dict[str, Any]],
        studio_outputs: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        trash_id: int,
    ) -> dict[str, Any]:
        if self.conn.execute(
            "SELECT 1 FROM notebooks WHERE id=?", (nb["id"],)
        ).fetchone():
            raise StoreError(
                "NOTEBOOK_ALREADY_EXISTS",
                f"notebook {nb['id']} already exists — cannot restore over it",
            )
        with self.conn:
            self.conn.execute(
                "INSERT INTO notebooks(id, name, created_at, updated_at, settings)"
                " VALUES(?,?,?,?,?)",
                (nb["id"], nb["name"], nb["created_at"], nb["updated_at"],
                 nb["settings"]),
            )
            for s in sources:
                self.conn.execute(
                    "INSERT INTO sources(id, notebook_id, kind, title, origin,"
                    " sha256, added_at, weight, meta) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        s["id"], s["notebook_id"], s["kind"], s["title"],
                        s["origin"], s["sha256"], s["added_at"],
                        # Archives written before migration 11/12 carry no
                        # weight/meta key — restore them neutral rather
                        # than refusing.
                        float(s.get("weight", 1.0)),
                        s["meta"],
                    ),
                )
            for c in chunks:
                self.conn.execute(
                    "INSERT INTO chunks(id, source_id, seq, text, context,"
                    " embedding, embedding_norm) VALUES(?,?,?,?,?,?,?)",
                    (
                        c["id"], c["source_id"], c["seq"], c["text"],
                        c["context"], c["embedding"], c["embedding_norm"],
                    ),
                )
            for n in notes:
                self.conn.execute(
                    "INSERT INTO notes(id, notebook_id, title, body, created_at)"
                    " VALUES(?,?,?,?,?)",
                    (
                        n["id"], n["notebook_id"], n["title"],
                        n["body"], n["created_at"],
                    ),
                )
            for o in studio_outputs:
                self.conn.execute(
                    "INSERT INTO studio_outputs(id, notebook_id, kind, body,"
                    " citation_report, created_at) VALUES(?,?,?,?,?,?)",
                    (
                        o["id"], o["notebook_id"], o["kind"], o["body"],
                        o["citation_report"], o["created_at"],
                    ),
                )
            for m in messages:
                self.conn.execute(
                    "INSERT INTO messages(id, notebook_id, role, body,"
                    " citation_report, created_at) VALUES(?,?,?,?,?,?)",
                    (
                        m["id"], m["notebook_id"], m["role"], m["body"],
                        m["citation_report"], m["created_at"],
                    ),
                )
            self.conn.execute("DELETE FROM trash_items WHERE id=?", (trash_id,))
            self._optimize_fts()
        return {"kind": "notebook", "id": nb["id"], "name": nb["name"]}

    def _restore_trashed_source(
        self,
        src: dict[str, Any],
        chunks: list[dict[str, Any]],
        nb_id: int,
        trash_id: int,
    ) -> dict[str, Any]:
        """Source archive restore (v0.2.667): original id inside its parent
        notebook — chunk INSERTs re-fire the FTS triggers, so the source is
        searchable again in the same commit that lands it."""
        if self.conn.execute(
            "SELECT 1 FROM notebooks WHERE id=?", (nb_id,)
        ).fetchone() is None:
            raise StoreError(
                "NOTEBOOK_NOT_FOUND",
                f"notebook {nb_id} is gone — cannot restore source into it",
            )
        if self.conn.execute(
            "SELECT 1 FROM sources WHERE id=?", (src["id"],)
        ).fetchone():
            raise StoreError(
                "SOURCE_ALREADY_EXISTS",
                f"source {src['id']} already exists — cannot restore over it",
            )
        with self.conn:
            self.conn.execute(
                "INSERT INTO sources(id, notebook_id, kind, title, origin,"
                " sha256, added_at, weight, meta) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    src["id"], nb_id, src["kind"], src["title"], src["origin"],
                    src["sha256"], src["added_at"],
                    # Archives written before migrations 11/12 carry no
                    # weight/meta key — restore them neutral rather than
                    # refusing (same contract as notebook-tree restore).
                    float(src.get("weight", 1.0)),
                    src["meta"],
                ),
            )
            for c in chunks:
                self.conn.execute(
                    "INSERT INTO chunks(id, source_id, seq, text, context,"
                    " embedding, embedding_norm) VALUES(?,?,?,?,?,?,?)",
                    (
                        c["id"], src["id"], c["seq"], c["text"],
                        c["context"], c["embedding"], c["embedding_norm"],
                    ),
                )
            self.conn.execute("DELETE FROM trash_items WHERE id=?", (trash_id,))
            self.touch_notebook(nb_id)
            self._optimize_fts()
        return {"kind": "source", "id": int(src["id"]), "name": str(src["title"])}

    def _restore_trashed_note(
        self, note: dict[str, Any], nb_id: int, trash_id: int
    ) -> dict[str, Any]:
        """Note archive restore (v0.2.667): fresh id inside its parent
        notebook — nothing outside delete/list references note ids, so
        re-assignment loses nothing and the insert can never collide."""
        if self.conn.execute(
            "SELECT 1 FROM notebooks WHERE id=?", (nb_id,)
        ).fetchone() is None:
            raise StoreError(
                "NOTEBOOK_NOT_FOUND",
                f"notebook {nb_id} is gone — cannot restore note into it",
            )
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO notes(notebook_id, title, body, created_at)"
                " VALUES(?,?,?,?)",
                (nb_id, note["title"], note["body"], note["created_at"]),
            )
            self.conn.execute("DELETE FROM trash_items WHERE id=?", (trash_id,))
            self.touch_notebook(nb_id)
        return {
            "kind": "note",
            "id": int(cur.lastrowid or 0),
            "name": str(note["title"]),
        }

    def trash_purge(self, trash_id: int) -> None:
        """Permanently drop one trash archive — the undo record itself."""
        cur = self.conn.execute("DELETE FROM trash_items WHERE id=?", (trash_id,))
        self.conn.commit()
        if cur.rowcount == 0:
            raise StoreError("TRASH_NOT_FOUND", f"trash item {trash_id} not found")

    def trash_purge_all(self) -> int:
        """Permanently drop every trash archive in one TX — empties the
        undo log. Returns the number of archives dropped (v0.2.669)."""
        with self.conn:
            cur = self.conn.execute("DELETE FROM trash_items")
        return cur.rowcount

    def export_notebook(self, notebook_id: int) -> dict[str, Any]:
        """Portable notebook-tree document (v0.2.655) — the same envelope
        as the trash undo-log: a deleted notebook's archive is already a
        valid import document, and vice versa."""
        return self._notebook_tree_dict(notebook_id)

    def import_notebook(self, payload: dict[str, Any]) -> Notebook:
        """Insert an export document as a NEW notebook (v0.2.655).

        Fresh ids everywhere — the file's ids mean nothing in the target
        DB, so sources/chunks/notes/outputs/messages are re-inserted and
        re-keyed like duplicate_notebook. citation_report source ids are
        remapped through the new source ids (_remap_report_source_ids);
        entries whose source did not come along are dropped. Embedding
        BLOBs decode back verbatim — same model, zero re-embed cost —
        and chunk INSERTs re-fire the FTS triggers, so the notebook is
        searchable the moment import returns.
        """
        try:
            nb = payload["notebook"]
            name = nb["name"]
            if not isinstance(name, str):
                raise StoreError(
                    "NOTEBOOK_IMPORT_INVALID", "export name is not a string"
                )
            sources = payload["sources"]
            chunks = payload["chunks"]
            notes = payload["notes"]
            studio_outputs = payload["studio_outputs"]
            messages = payload["messages"]
            # settings is optional in the document (pre-v0.2.659 exports
            # lack it) — absent normalizes to '{}', anything non-object is
            # malformed like a non-object meta.
            nb_settings_text = _meta_text(nb.get("settings"))
            src_ids: set[Any] = set()
            for s in sources:
                src_ids.add(s["id"])
                for k in ("kind", "title", "origin", "sha256", "added_at"):
                    s[k]
                # weight is optional in the document (pre-v0.2.657 exports
                # lack it) but must be numeric when present; meta is the
                # same — absent or the canonical object/string only
                # (pre-v0.2.658).
                float(s.get("weight", 1.0))
                _meta_text(s.get("meta"))
            for c in chunks:
                if c["source_id"] not in src_ids:
                    raise StoreError(
                        "NOTEBOOK_IMPORT_INVALID",
                        "chunk references a source not in the export",
                    )
                for k in ("seq", "text", "context", "embedding_norm"):
                    c[k]
                if c["embedding"] is not None:
                    c["embedding"] = base64.b64decode(c["embedding"]["$blob"])
            for n in notes:
                for k in ("title", "body", "created_at"):
                    n[k]
            for o in studio_outputs:
                for k in ("kind", "body", "citation_report", "created_at"):
                    o[k]
            for m in messages:
                for k in ("role", "body", "citation_report", "created_at"):
                    m[k]
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError(
                "NOTEBOOK_IMPORT_INVALID",
                "payload is not a valid notebook export",
            ) from exc
        # Import semantics are lenient where create is strict: a file from
        # a newer version may carry a name past our current cap — truncate
        # rather than refuse the whole notebook.
        name = name.strip()[:MAX_NAME_LEN] or "imported"
        _utf8(name, "name")
        ts = _now()
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO notebooks(name, created_at, updated_at, settings)"
                    " VALUES(?,?,?,?)",
                    (name, ts, ts, nb_settings_text),
                )
                new_id = int(cur.lastrowid or 0)
                self._insert_tree_rows(
                    new_id, sources, chunks, notes, studio_outputs, messages
                )
                self._optimize_fts()
        except sqlite3.InterfaceError as exc:
            # Unbindable value types (dict where TEXT belongs) are a
            # malformed payload, not a DB failure — classify honestly.
            raise StoreError(
                "NOTEBOOK_IMPORT_INVALID", "export rows failed type checks"
            ) from exc
        return Notebook(new_id, name, ts, ts, json.loads(nb_settings_text))

    def _insert_tree_rows(
        self,
        notebook_id: int,
        sources: list[Any],
        chunks: list[Any],
        notes: list[Any],
        studio_outputs: list[Any],
        messages: list[Any],
    ) -> None:
        """Re-insert a serialized notebook tree under FRESH ids.

        Callee-transacted: the caller owns `with self.conn:` (same
        contract as touch_notebook). Shared by import_notebook, which
        inserts the new notebook row first, and merge_notebooks, which
        targets an existing one. Source ids are re-keyed through
        id_map; citation_report source_id_map is rewritten through the
        same map so reports never point at dead or wrong sources.
        Chunk INSERTs re-fire the FTS triggers, so merged/imported
        content is searchable the moment the transaction commits.
        """
        id_map: dict[Any, int] = {}
        for s in sources:
            cur = self.conn.execute(
                "INSERT INTO sources"
                "(notebook_id, kind, title, origin, sha256, added_at, weight,"
                " meta) VALUES(?,?,?,?,?,?,?,?)",
                (
                    notebook_id, s["kind"], s["title"], s["origin"],
                    s["sha256"], s["added_at"],
                    # Same optional-key contract as trash_restore: exports
                    # predating the column import neutral.
                    float(s.get("weight", 1.0)),
                    _meta_text(s.get("meta")),
                ),
            )
            id_map[s["id"]] = int(cur.lastrowid or 0)
        for c in chunks:
            self.conn.execute(
                "INSERT INTO chunks(source_id, seq, text, context,"
                " embedding, embedding_norm) VALUES(?,?,?,?,?,?)",
                (
                    id_map[c["source_id"]], c["seq"], c["text"],
                    c["context"], c["embedding"], c["embedding_norm"],
                ),
            )
        for n in notes:
            self.conn.execute(
                "INSERT INTO notes(notebook_id, title, body, created_at)"
                " VALUES(?,?,?,?)",
                (notebook_id, n["title"], n["body"], n["created_at"]),
            )
        for o in studio_outputs:
            self.conn.execute(
                "INSERT INTO studio_outputs(notebook_id, kind, body,"
                " citation_report, created_at) VALUES(?,?,?,?,?)",
                (
                    notebook_id, o["kind"], o["body"],
                    _remap_report_source_ids(o["citation_report"], id_map),
                    o["created_at"],
                ),
            )
        for m in messages:
            self.conn.execute(
                "INSERT INTO messages(notebook_id, role, body,"
                " citation_report, created_at) VALUES(?,?,?,?,?)",
                (
                    notebook_id, m["role"], m["body"],
                    _remap_report_source_ids(m["citation_report"], id_map),
                    m["created_at"],
                ),
            )

    def _optimize_fts(self) -> None:
        """Merge every FTS5 b-tree segment into one ('optimize' insert).

        Callee-transacted: the caller owns `with self.conn:` (same
        contract as touch_notebook). Each chunk INSERT creates a new
        unmerged index segment; automerge only chips at the pile
        incrementally, so after bulk writes the trigram index carries
        avoidable segment overhead on top of its inherent per-gram
        cost. Every chunk-write path calls this once at the end of its
        transaction: the merge runs on whatever rows the caller just
        indexed, inside the same commit boundary (v0.2.664).
        """
        self.conn.execute(
            "INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')"
        )

    def merge_notebooks(self, target_id: int, source_id: int) -> Notebook:
        """Fold one notebook into another (v0.2.656).

        The source notebook's sources, chunks, notes, studio outputs
        and messages are re-inserted into the target under fresh ids —
        the same re-keying as duplicate/import (chunk source_ids and
        citation_report source_id_maps remapped, embeddings carried
        verbatim, FTS re-indexed on INSERT). The source notebook is
        then deleted through delete_notebook, which archives the whole
        tree to trash in the same transaction: a merge is recoverable
        via `trash restore`.

        Ordering is copy-then-delete, two transactions: a crash
        mid-merge can duplicate content (target gains the rows, source
        still lives and can be retried or trashed by hand), never
        lose it.
        """
        self.get_notebook(target_id)
        self.get_notebook(source_id)
        if target_id == source_id:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                "cannot merge a notebook into itself",
            )
        doc = self._notebook_tree_dict(source_id)
        try:
            for c in doc["chunks"]:
                if c["embedding"] is not None:
                    c["embedding"] = base64.b64decode(c["embedding"]["$blob"])
            with self.conn:
                self._insert_tree_rows(
                    target_id,
                    doc["sources"],
                    doc["chunks"],
                    doc["notes"],
                    doc["studio_outputs"],
                    doc["messages"],
                )
                self.touch_notebook(target_id)
                self._optimize_fts()
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError(
                "SYSTEM_INTERNAL_ERROR",
                f"notebook {source_id} tree could not be serialized",
            ) from exc
        self.delete_notebook(source_id)
        return self.get_notebook(target_id)

    def touch_notebook(self, notebook_id: int) -> None:
        """Stamp the notebook's updated_at. Does NOT commit — callers must commit."""
        self.conn.execute("UPDATE notebooks SET updated_at=? WHERE id=?", (_now(), notebook_id))

    def duplicate_notebook(self, notebook_id: int, name: str | None = None) -> Notebook:
        """Full-fidelity fork of a notebook in one transaction (v0.2.645).

        Sources, chunks (embedding BLOBs verbatim — the same embed model
        produced them, so they stay valid), notes, studio outputs and
        messages are all copied. The FTS triggers re-index new chunks on
        INSERT, and the questions cache is keyed on source ids so nothing
        bleeds across the fork. Child rows keep their original timestamps
        — they describe the copied content, not the copy event.
        """
        src = self.get_notebook(notebook_id)
        if name is None:
            suffix = " (copy)"
            name = src.name[: MAX_NAME_LEN - len(suffix)] + suffix
        name = name.strip()
        if not name:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "notebook name is empty")
        if len(name) > MAX_NAME_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"name too long (max {MAX_NAME_LEN} chars)",
            )
        _utf8(name, "name")
        ts = _now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO notebooks(name, created_at, updated_at, settings)"
                " VALUES(?,?,?,?)",
                (name, ts, ts, _meta_dump(src.settings)),
            )
            new_id = int(cur.lastrowid or 0)
            id_map: dict[int, int] = {}
            for row in self.conn.execute(
                "SELECT * FROM sources WHERE notebook_id=? ORDER BY id",
                (notebook_id,),
            ).fetchall():
                cur = self.conn.execute(
                    "INSERT INTO sources"
                    "(notebook_id, kind, title, origin, sha256, added_at, weight,"
                    " meta) VALUES (?,?,?,?,?,?,?,?)",
                    (new_id, row["kind"], row["title"], row["origin"],
                     row["sha256"], row["added_at"], float(row["weight"]),
                     row["meta"]),
                )
                id_map[int(row["id"])] = int(cur.lastrowid or 0)
            for old_src, new_src in id_map.items():
                self.conn.execute(
                    "INSERT INTO chunks(source_id, seq, text, context,"
                    " embedding, embedding_norm)"
                    " SELECT ?, seq, text, context, embedding, embedding_norm"
                    " FROM chunks WHERE source_id=? ORDER BY seq",
                    (new_src, old_src),
                )
            self.conn.execute(
                "INSERT INTO notes(notebook_id, title, body, created_at)"
                " SELECT ?, title, body, created_at FROM notes WHERE notebook_id=?",
                (new_id, notebook_id),
            )
            for row in self.conn.execute(
                "SELECT kind, body, citation_report, created_at"
                " FROM studio_outputs WHERE notebook_id=? ORDER BY id",
                (notebook_id,),
            ).fetchall():
                self.conn.execute(
                    "INSERT INTO studio_outputs(notebook_id, kind, body,"
                    " citation_report, created_at) VALUES(?,?,?,?,?)",
                    (
                        new_id, row["kind"], row["body"],
                        _remap_report_source_ids(row["citation_report"], id_map),
                        row["created_at"],
                    ),
                )
            for row in self.conn.execute(
                "SELECT role, body, citation_report, created_at"
                " FROM messages WHERE notebook_id=? ORDER BY id",
                (notebook_id,),
            ).fetchall():
                self.conn.execute(
                    "INSERT INTO messages(notebook_id, role, body,"
                    " citation_report, created_at) VALUES(?,?,?,?,?)",
                    (
                        new_id, row["role"], row["body"],
                        _remap_report_source_ids(row["citation_report"], id_map),
                        row["created_at"],
                    ),
                )
            self._optimize_fts()
        return Notebook(new_id, name, ts, ts, src.settings)

    # --- sources / chunks ---

    def add_source(
        self, notebook_id: int, kind: str, title: str, origin: str, sha256: str
    ) -> Source:
        if kind not in SOURCE_KINDS:
            # Same fail-at-the-write class as add_message()'s role guard and
            # add_studio_output()'s kind guard: kind drives the RIS TY mapping
            # in export and the UI badge — a typo'd literal silently exports
            # wrong citation types and renders a nonsense badge with no
            # corrective path (kind is immutable post-insert).
            raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", f"unknown source kind: {kind!r}")
        # Silently truncate like update_source_title() — titles come from
        # external content — but also strip + reject empty, the same validation
        # update_source_title() and update_source_sha256() already apply. A
        # whitespace title could otherwise enter via the ingest path (caller-
        # supplied title=, whitespace filename) and persist a blank title the
        # rename path itself refuses to write.
        title = title.strip()[:MAX_TITLE_LEN]
        if not title:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "source title is empty")
        _utf8(title, "title")
        _utf8(origin, "origin")
        _utf8(sha256, "sha256")
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
            # `with self.conn:` commits INSERT+touch atomically and rolls both
            # back on failure — a failed touch must not leave the new row
            # pending for a later commit on this connection to publish (the
            # caller sees an error yet the source appears). Same leak class
            # as the v0.2.417-418 add_studio_output fixes.
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO sources(notebook_id, kind, title, origin, sha256, added_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (notebook_id, kind, title, origin, sha256, ts),
                )
                self.touch_notebook(notebook_id)
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e):
                raise StoreError(
                    "SOURCE_ALREADY_EXISTS",
                    "identical source already in notebook (concurrent upload)",
                ) from e
            if "FOREIGN KEY" in str(e):
                raise StoreError(
                    "NOTEBOOK_NOT_FOUND",
                    f"notebook {notebook_id} was deleted during source addition",
                ) from e
            # Unexpected constraint violation (e.g. CHECK, NOT NULL) — propagate
            # as a generic internal error rather than a misleading NOTEBOOK_NOT_FOUND.
            raise StoreError(
                "SYSTEM_INTERNAL_ERROR",
                f"unexpected constraint violation: {e}",
            ) from e
        return Source(int(cur.lastrowid or 0), notebook_id, kind, title, origin, sha256, ts)

    def update_source_title(self, source_id: int, title: str, origin: str) -> None:
        title = title.strip()[:MAX_TITLE_LEN]
        if not title:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "source title is empty")
        _utf8(title, "title")
        _utf8(origin, "origin")
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
                float(r["weight"]),
                json.loads(r["meta"]),
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
            float(row["weight"]),
            json.loads(row["meta"]),
        )

    def notebooks_for_sources(
        self, source_ids: list[int]
    ) -> dict[int, tuple[int, str, str]]:
        """source_id -> (notebook_id, notebook name, source title).

        Read-side provenance map for the cross-notebook search (v0.2.649):
        a global hit carries only its source_id, so one query resolves the
        notebook identity and title for every surfaced source at once.
        Ids travel as a bound JSON parameter via json_each — the codebase's
        variable-IN idiom — never interpolated into SQL text.
        """
        if not source_ids:
            return {}
        rows = self.conn.execute(
            "SELECT s.id, s.notebook_id, n.name, s.title FROM sources s"
            " JOIN notebooks n ON n.id = s.notebook_id"
            " WHERE s.id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted({int(i) for i in source_ids})),),
        ).fetchall()
        return {
            int(r["id"]): (int(r["notebook_id"]), str(r["name"]), str(r["title"]))
            for r in rows
        }

    def delete_source(self, source_id: int) -> None:
        src = self.get_source(source_id)  # raises SOURCE_NOT_FOUND if missing
        # Undo-log trash (v0.2.667): same archive-then-delete TX as
        # delete_notebook — a deleted upload whose tmp origin is long
        # gone is unrecoverable without this record. Chunks ride in the
        # payload (base64 embeddings) and re-fire FTS triggers on restore.
        src_row = self.conn.execute(
            "SELECT * FROM sources WHERE id=?", (source_id,)
        ).fetchone()
        payload = json.dumps(
            {
                "kind": "source",
                "source": dict(src_row) if src_row is not None else {},
                "chunks": self._chunk_dicts(source_id),
            },
            ensure_ascii=False,
        )
        with self.conn:
            self.conn.execute(
                "INSERT INTO trash_items(notebook_id, name, deleted_at, payload, kind)"
                " VALUES(?,?,?,?,'source')",
                (src.notebook_id, src.title, _now(), payload),
            )
            cur = self.conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
            if cur.rowcount == 0:
                raise StoreError("SOURCE_NOT_FOUND", f"source {source_id} was concurrently deleted")
            self.touch_notebook(src.notebook_id)

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
            raise StoreError(
                "VALIDATION_REQUIRED_FIELD_MISSING",
                "replacement chunk list must not be empty",
            )
        if contexts is not None and len(contexts) != len(texts):
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"contexts length ({len(contexts)}) must match texts ({len(texts)})",
            )
        # Same strip+reject contract as the other three title writers
        # (add_source / update_source_title / update_source_sha256): without
        # it a whitespace-only title would persist — the blank title the
        # rename path itself refuses to write. Validated up front so a bad
        # title fails before any chunk is touched.
        new_title = title.strip()[:MAX_TITLE_LEN] if title is not None else None
        if title is not None and not new_title:
            raise StoreError(
                "VALIDATION_REQUIRED_FIELD_MISSING", "source title is empty"
            )
        if new_title is not None:
            _utf8(new_title, "title")
        if sha256 is not None:
            _utf8(sha256, "sha256")
        # Same up-front gate as the title check above: a bad value must fail
        # before any chunk row is touched.
        for text in texts:
            _utf8(text, "chunk text")
        if contexts is not None:
            for ctx in contexts:
                _utf8(ctx, "chunk context")
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
                    meta_cur = self.conn.execute(
                        "UPDATE sources SET sha256=?, title=COALESCE(?, title) WHERE id=?",
                        (sha256, new_title, source_id),
                    )
                    if meta_cur.rowcount == 0:
                        raise StoreError(
                            "SOURCE_NOT_FOUND",
                            f"source {source_id} was concurrently deleted",
                        )
                self.touch_notebook(src.notebook_id)
                self._optimize_fts()
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e):
                raise StoreError(
                    "SOURCE_ALREADY_EXISTS",
                    "refreshed content hash matches another existing source",
                ) from e
            if "FOREIGN KEY" in str(e):
                # chunks.source_id REFERENCES sources(id) ON DELETE CASCADE — this is
                # the genuine concurrent-deletion case: the source row was removed
                # between get_source() above and this INSERT.
                raise StoreError(
                    "SOURCE_NOT_FOUND", f"source {source_id} was deleted during chunk replacement"
                ) from e
            # Unexpected constraint violation (e.g. CHECK, NOT NULL) — propagate as a
            # generic internal error rather than a misleading SOURCE_NOT_FOUND (mirrors
            # the same v0.2.53 fix already applied to add_source(), never ported here).
            raise StoreError(
                "SYSTEM_INTERNAL_ERROR",
                f"unexpected constraint violation: {e}",
            ) from e
        return ids

    def update_source_sha256(self, source_id: int, sha256: str, title: str) -> None:
        """Update the content hash and title of a source after a refresh.

        Callers that need atomic chunk-replacement + metadata update should pass
        sha256/title to replace_chunks_for_source instead of calling this separately.
        This method is retained for callers that update metadata without replacing chunks.
        """
        title = title.strip()[:MAX_TITLE_LEN]
        if not title:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "source title is empty")
        _utf8(title, "title")
        _utf8(sha256, "sha256")
        src = self.get_source(source_id)  # raises SOURCE_NOT_FOUND if missing
        try:
            with self.conn:
                # Re-read inside the transaction so the context rewrite keys off
                # the row's actual value, not a stale snapshot — same concern as
                # update_source_title's in-transaction read.
                row = self.conn.execute(
                    "SELECT title FROM sources WHERE id=?", (source_id,)
                ).fetchone()
                if row is None:
                    raise StoreError(
                        "SOURCE_NOT_FOUND",
                        f"source {source_id} was concurrently deleted",
                    )
                old_title = str(row["title"])
                cur = self.conn.execute(
                    "UPDATE sources SET sha256=?, title=? WHERE id=?", (sha256, title, source_id)
                )
                if cur.rowcount == 0:
                    raise StoreError(
                        "SOURCE_NOT_FOUND",
                        f"source {source_id} was concurrently deleted",
                    )
                if title != old_title:
                    self._rewrite_chunk_context_titles(source_id, old_title, title)
                self.touch_notebook(src.notebook_id)
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e):
                raise StoreError(
                    "SOURCE_ALREADY_EXISTS",
                    "refreshed content hash matches another existing source",
                ) from e
            # This is an UPDATE that never touches notebook_id, so no FOREIGN KEY
            # violation is possible here — anything else (e.g. a NOT NULL on the
            # sha256 column) is a genuine unexpected constraint violation, not a
            # duplicate-hash collision. Mirrors the v0.2.53/86/104 fix pattern.
            raise StoreError(
                "SYSTEM_INTERNAL_ERROR",
                f"unexpected constraint violation: {e}",
            ) from e

    def update_source_weight(self, source_id: int, weight: float) -> None:
        """Set a source's retrieval weight (v0.2.657, product-review #19).

        The weight multiplies the fused normalized score of every hit the
        source contributes — 1.0 neutral, >1 promotes, <1 demotes, 0 pins
        its chunks to the pool floor. Bounded to [0, SOURCE_WEIGHT_MAX]:
        one source may dominate a ranking but cannot overflow it, and a
        non-finite value must never reach the score field. Single-statement
        UPDATE + rowcount covers both the never-existed and the concurrently-
        deleted id as SOURCE_NOT_FOUND. touch_notebook is deliberately NOT
        called: weight is a retrieval preference, not content, so the
        notebook list's updated_at ordering must not churn on it.
        """
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"weight must be a number, got {type(weight).__name__}",
            )
        value = float(weight)
        if not math.isfinite(value) or not 0.0 <= value <= SOURCE_WEIGHT_MAX:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"weight must be finite in 0..{SOURCE_WEIGHT_MAX}",
            )
        with self.conn:
            cur = self.conn.execute(
                "UPDATE sources SET weight=? WHERE id=?", (value, source_id)
            )
            if cur.rowcount == 0:
                raise StoreError(
                    "SOURCE_NOT_FOUND", f"source {source_id} not found"
                )

    def update_source_meta(self, source_id: int, meta: dict[str, Any]) -> None:
        """Replace a source's descriptive metadata object (v0.2.658, #24).

        Whole-object REPLACE, not a per-key merge — idempotent PATCH
        semantics; key-level merging lives at the CLI layer (it reads,
        merges, and writes the whole object back). The column stores
        canonical JSON (_meta_dump: sorted keys, tight separators) so
        logically-equal objects serialize byte-identically and a re-PATCH
        cannot churn storage. Bounds: must be a dict, must serialize to
        JSON, serialized form ≤ SOURCE_META_MAX — descriptive citation
        data, not a blob column. touch_notebook IS called: author/year
        changes what exports emit, which is content-bearing the same way
        a rename is. Single-statement UPDATE + rowcount covers both
        never-existed and concurrently-deleted ids as SOURCE_NOT_FOUND.
        """
        if not isinstance(meta, dict):
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"meta must be a JSON object, got {type(meta).__name__}",
            )
        try:
            text = _meta_dump(meta)
        except (TypeError, ValueError) as e:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                "meta must be JSON-serializable",
            ) from e
        if len(text.encode("utf-8")) > SOURCE_META_MAX:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"meta exceeds {SOURCE_META_MAX} bytes",
            )
        src = self.get_source(source_id)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE sources SET meta=? WHERE id=?", (text, source_id)
            )
            if cur.rowcount == 0:
                raise StoreError(
                    "SOURCE_NOT_FOUND", f"source {source_id} not found"
                )
            self.touch_notebook(src.notebook_id)

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
        # Up-front like replace_chunks_for_source's gate: a bad string must
        # fail before any chunk row is written.
        for text in texts:
            _utf8(text, "chunk text")
        if contexts is not None:
            for ctx in contexts:
                _utf8(ctx, "chunk context")
        src = self.get_source(source_id)  # raises SOURCE_NOT_FOUND if missing
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
                self.touch_notebook(src.notebook_id)
                self._optimize_fts()
        except sqlite3.IntegrityError as e:
            if "FOREIGN KEY" in str(e):
                # chunks.source_id REFERENCES sources(id) — this is the genuine
                # concurrent-deletion case: the source row was removed mid-insert.
                raise StoreError(
                    "SOURCE_NOT_FOUND", f"source {source_id} was deleted during chunk insertion"
                ) from e
            # Unexpected constraint violation (e.g. future CHECK, NOT NULL) — propagate
            # as a generic internal error rather than a misleading SOURCE_NOT_FOUND.
            # Mirrors the same v0.2.53 fix already applied to add_source() and
            # replace_chunks_for_source() (v0.2.86), never ported to this third sibling.
            raise StoreError(
                "SYSTEM_INTERNAL_ERROR",
                f"unexpected constraint violation: {e}",
            ) from e
        return ids

    def _set_embedding_pair(self, chunk_id: int, blob: bytes, norm: float) -> None:
        # Two statements, one transaction, and the order matters: writing the
        # embedding fires the migration-9 trigger, which clears the cached norm
        # unconditionally; the second statement then writes the norm that belongs
        # to the vector just stored. Doing both in ONE statement would need the
        # trigger to guess whether a writer knew about the norm column, and every
        # guess has a case it gets wrong (migration 8's did — see its note).
        # Measured: the split costs nothing (29.1 us vs 32.5 us per chunk).
        cur = self.conn.execute(
            "UPDATE chunks SET embedding=? WHERE id=?", (blob, chunk_id)
        )
        if cur.rowcount == 0:
            raise StoreError("CHUNK_NOT_FOUND", f"chunk {chunk_id} not found")
        self.conn.execute(
            "UPDATE chunks SET embedding_norm=? WHERE id=?", (norm, chunk_id)
        )

    def set_embedding(self, chunk_id: int, vec: list[float], *, commit: bool = True) -> None:
        if not vec:
            raise StoreError("EMBEDDING_INVALID", "embedding vector must not be empty")
        # Norm computed from the float32 round-trip (array("f", vec)), not from the
        # float64 input, so it is bit-identical to what search computes on the BLOB
        # it reads back — a float64 norm would shift scores in the last bits.
        packed = array.array("f", vec)
        norm = math.sqrt(sum(map(operator.mul, packed, packed)))
        if commit:
            # This call owns the transaction: `with self.conn:` lands both
            # statements or rolls both back — a failed norm write must not
            # leave the vector write pending for a later commit on this
            # connection to publish unnormed (v0.2.417-418 leak class).
            with self.conn:
                self._set_embedding_pair(chunk_id, packed.tobytes(), norm)
        else:
            # commit=False callers own the surrounding transaction
            # (_embed_chunks rolls back a partial batch on failure).
            self._set_embedding_pair(chunk_id, packed.tobytes(), norm)

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

    def id_seq_text_chunks_for_source(self, source_id: int) -> list[tuple[int, int, str]]:
        """Return (chunk_id, seq, text) triples for a source, ordered by seq.

        The chunk id lets the source viewer mark exactly which passages an
        answer was grounded in (citation_report's source_chunk_ids). The older
        text_chunks_for_source() returns only (seq, text) and is kept as-is
        because other callers depend on that shape.
        """
        rows = self.conn.execute(
            "SELECT id, seq, text FROM chunks WHERE source_id=? ORDER BY seq", (source_id,)
        ).fetchall()
        return [(int(r["id"]), int(r["seq"]), str(r["text"])) for r in rows]

    def update_chunk_text(self, chunk_id: int, text: str) -> Chunk:
        """Replace one chunk's text (v0.2.647 — the fix path for extraction
        errors, which previously required delete + re-add of the source).

        The embedding is cleared, not kept: it was computed from the old
        text, so retaining it would silently retrieve the wrong content.
        NULL degrades to the BM25 leg ("no vector signal", same contract as
        a corrupt BLOB), and `reindex` rebuilds the vector. The chunks_au
        UPDATE trigger re-syncs the FTS index inside the same write.
        """
        text = text.strip()
        if not text:
            raise StoreError(
                "VALIDATION_REQUIRED_FIELD_MISSING", "chunk text is empty"
            )
        _utf8(text, "text")
        row = self.conn.execute(
            "SELECT c.id, s.notebook_id FROM chunks c"
            " JOIN sources s ON s.id=c.source_id WHERE c.id=?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            raise StoreError("CHUNK_NOT_FOUND", f"chunk {chunk_id} not found")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE chunks SET text=?, embedding=NULL, embedding_norm=NULL"
                " WHERE id=?",
                (text, chunk_id),
            )
            if cur.rowcount == 0:
                raise StoreError(
                    "CHUNK_NOT_FOUND", f"chunk {chunk_id} was concurrently deleted"
                )
            self.touch_notebook(int(row["notebook_id"]))
        return self.get_chunk(chunk_id)

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

    def id_context_text_chunks_for_source(self, source_id: int) -> list[tuple[int, str, str]]:
        """Return (chunk_id, context, text) triples for one source, ordered by seq.

        The source-scoped counterpart of id_context_text_chunks_for_notebook,
        used by pipeline.rename_source to re-embed only the renamed source's
        chunks — the context column already holds the REWRITTEN title prefix
        (update_source_title refreshes it in the same transaction), so feeding
        these rows through _embed_input reproduces exactly what index_source
        would have embedded had the source carried the new title all along.
        """
        rows = self.conn.execute(
            "SELECT id, context, text FROM chunks WHERE source_id=? ORDER BY seq", (source_id,)
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
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"title too long (max {MAX_NAME_LEN} chars)",
            )
        _utf8(title, "title")
        _utf8(body, "body")
        self.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
        try:
            # Atomic INSERT+touch — same pending-leak guard as add_source
            # (v0.2.419): a failed touch must not leave the new note pending.
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO notes(notebook_id, title, body, created_at) VALUES (?,?,?,?)",
                    (notebook_id, title, body, _now()),
                )
                self.touch_notebook(notebook_id)
        except sqlite3.IntegrityError as e:
            if "FOREIGN KEY" not in str(e):
                # notes has no UNIQUE constraint, so the only expected IntegrityError
                # here is the FK on notebook_id (genuine concurrent deletion).
                # Mirrors the v0.2.53/86/104 fix pattern.
                raise StoreError(
                    "SYSTEM_INTERNAL_ERROR",
                    f"unexpected constraint violation: {e}",
                ) from e
            raise StoreError(
                "NOTEBOOK_NOT_FOUND",
                f"notebook {notebook_id} was deleted during note insertion",
            ) from e
        return int(cur.lastrowid or 0)

    def list_notes(self, notebook_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM notes WHERE notebook_id=? ORDER BY id", (notebook_id,)
            ).fetchall()
        )

    def count_notes(self, notebook_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM notes WHERE notebook_id=?", (notebook_id,)
        ).fetchone()
        return int(row[0])

    def list_notes_page(
        self, notebook_id: int, offset: int, limit: int
    ) -> list[sqlite3.Row]:
        # Newest-first: page 0 overlaps the detail payload's embedded notes.
        return list(
            self.conn.execute(
                "SELECT * FROM notes WHERE notebook_id=? ORDER BY id DESC"
                " LIMIT ? OFFSET ?",
                (notebook_id, limit, offset),
            ).fetchall()
        )

    def delete_note(self, note_id: int) -> None:
        row = self.conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if row is None:
            raise StoreError("NOTE_NOT_FOUND", f"note {note_id} not found")
        # Undo-log trash (v0.2.667): a user-typed note is unrecoverable
        # text — same archive-then-delete contract as notebook/source.
        payload = json.dumps({"kind": "note", "note": dict(row)}, ensure_ascii=False)
        with self.conn:
            self.conn.execute(
                "INSERT INTO trash_items(notebook_id, name, deleted_at, payload, kind)"
                " VALUES(?,?,?,?,'note')",
                (int(row["notebook_id"]), str(row["title"]), _now(), payload),
            )
            cur = self.conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
            if cur.rowcount == 0:
                raise StoreError("NOTE_NOT_FOUND", f"note {note_id} was concurrently deleted")
            self.touch_notebook(int(row["notebook_id"]))

    def add_studio_output(
        self, notebook_id: int, kind: str, body: str, citation_report: str
    ) -> int:
        if kind not in STUDIO_KINDS:
            # latest_studio_outputs() GROUP BYs on kind, so a typo'd literal
            # persists as a phantom kind — grouped out of every UI section and
            # rendered by export under a nonsense heading — with no caller able
            # to overwrite it. Same fail-at-the-write class as add_message()'s
            # role guard.
            raise StoreError("STUDIO_KIND_INVALID", f"unknown studio kind: {kind!r}")
        _utf8(body, "body")
        _utf8(citation_report, "citation_report")
        self.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
        try:
            # `with self.conn:` commits INSERT+DELETE atomically and rolls
            # both back on failure — a failed prune must not leave the
            # rejected row pending for a later write on this connection to
            # publish (latest_studio_outputs takes MAX(id), so it would
            # displace the good output). Insert-then-delete also scopes the
            # prune to `id < lastrowid`, preserving a newer concurrent row.
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO studio_outputs(notebook_id, kind, body, citation_report,"
                    " created_at) VALUES (?,?,?,?,?)",
                    (notebook_id, kind, body, citation_report, _now()),
                )
                self.conn.execute(
                    "DELETE FROM studio_outputs WHERE notebook_id=? AND kind=? AND id<?",
                    (notebook_id, kind, int(cur.lastrowid or 0)),
                )
                self.touch_notebook(notebook_id)
        except sqlite3.IntegrityError as e:
            if "FOREIGN KEY" not in str(e):
                # studio_outputs has no UNIQUE constraint, so the only expected
                # IntegrityError here is the FK on notebook_id (genuine concurrent
                # deletion). Mirrors the v0.2.53/86/104 fix pattern.
                raise StoreError(
                    "SYSTEM_INTERNAL_ERROR",
                    f"unexpected constraint violation: {e}",
                ) from e
            raise StoreError(
                "NOTEBOOK_NOT_FOUND",
                f"notebook {notebook_id} was deleted during studio output insertion",
            ) from e
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
        if role not in ("user", "assistant"):
            # history_messages() coerces any unknown role to "assistant" — a
            # typo'd literal would silently corrupt turn alternation, so fail
            # loudly at the write.
            raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", f"unknown message role: {role!r}")
        _utf8(body, "body")
        _utf8(citation_report, "citation_report")
        self.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
        try:
            # Atomic INSERT+touch — same pending-leak guard as add_source
            # (v0.2.419): a failed touch must not leave the new message pending.
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO messages(notebook_id, role, body, citation_report, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (notebook_id, role, body, citation_report, _now()),
                )
                self.touch_notebook(notebook_id)
        except sqlite3.IntegrityError as e:
            if "FOREIGN KEY" not in str(e):
                # messages has no UNIQUE constraint, so the only expected
                # IntegrityError here is the FK on notebook_id (genuine concurrent
                # deletion). Mirrors the v0.2.53/86/104 fix pattern.
                raise StoreError(
                    "SYSTEM_INTERNAL_ERROR",
                    f"unexpected constraint violation: {e}",
                ) from e
            raise StoreError(
                "NOTEBOOK_NOT_FOUND",
                f"notebook {notebook_id} was deleted during message insertion",
            ) from e
        return int(cur.lastrowid or 0)

    def count_messages(self, notebook_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE notebook_id=?", (notebook_id,)
        ).fetchone()
        return int(row[0])

    def list_messages(self, notebook_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM messages WHERE notebook_id=? ORDER BY id", (notebook_id,)
            ).fetchall()
        )

    def list_messages_page(
        self, notebook_id: int, offset: int, limit: int
    ) -> list[sqlite3.Row]:
        # Newest-first: page 0 overlaps the detail payload's embedded tail.
        return list(
            self.conn.execute(
                "SELECT * FROM messages WHERE notebook_id=? ORDER BY id DESC"
                " LIMIT ? OFFSET ?",
                (notebook_id, limit, offset),
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
        with self.conn:
            self.conn.execute("DELETE FROM messages WHERE notebook_id=?", (notebook_id,))
            self.touch_notebook(notebook_id)

    def counts(self, notebook_id: int) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT s.id) AS sources, COUNT(c.id) AS chunks"
            " FROM sources s LEFT JOIN chunks c ON c.source_id = s.id"
            " WHERE s.notebook_id = ?",
            (notebook_id,),
        ).fetchone()
        return {"sources": int(row["sources"]), "chunks": int(row["chunks"])}

    def notebook_stats(self, notebook_id: int) -> dict[str, int]:
        """Row counts across every notebook content table, in one query."""
        row = self.conn.execute(
            "SELECT"
            " (SELECT COUNT(*) FROM sources WHERE notebook_id=?),"
            " (SELECT COUNT(*) FROM chunks c"
            "  JOIN sources s ON s.id=c.source_id WHERE s.notebook_id=?),"
            " (SELECT COUNT(*) FROM notes WHERE notebook_id=?),"
            " (SELECT COUNT(*) FROM messages WHERE notebook_id=?),"
            " (SELECT COUNT(*) FROM studio_outputs WHERE notebook_id=?)",
            (notebook_id,) * 5,
        ).fetchone()
        return {
            "sources": int(row[0]),
            "chunks": int(row[1]),
            "notes": int(row[2]),
            "messages": int(row[3]),
            "studio_outputs": int(row[4]),
        }

    def backup_to(self, dest: Path | str) -> None:
        """Write an online snapshot of this database to dest.

        SQLite's backup API copies page-by-page against the live connection,
        so the snapshot is consistent even while the DB stays open and
        writable. dest is created (0600, same privacy as the database) or
        overwritten; refusing dest == the live DB prevents truncating the
        source it reads from.
        """
        dest_file = Path(dest)
        if self._db_file is not None and dest_file.resolve() == self._db_file.resolve():
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                "backup destination must differ from the live database path",
            )
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dest_file, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        target = sqlite3.connect(str(dest_file))
        try:
            self.conn.backup(target)
        finally:
            target.close()
        os.chmod(dest_file, 0o600)

    def db_bytes(self) -> int:
        """Bytes the database occupies on disk (page_count * page_size)."""
        page_count = int(self.conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(self.conn.execute("PRAGMA page_size").fetchone()[0])
        return page_count * page_size

    def freelist_bytes(self) -> int:
        """Bytes sitting on SQLite's free page list — the share of the db
        file that deleted rows left behind and only vacuum() hands back
        to the OS (v0.2.669)."""
        freelist = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
        page_size = int(self.conn.execute("PRAGMA page_size").fetchone()[0])
        return freelist * page_size

    def vacuum(self) -> dict[str, int]:
        """Hand deleted pages back to the OS and report db_bytes() around
        the rebuild (v0.2.669).

        SQLite never shrinks the db file on its own: every DELETE moves
        pages onto the free list, where they keep occupying disk until a
        full VACUUM rebuilds the file. wal_checkpoint(TRUNCATE) runs
        first so pending -wal frames fold into the main file before the
        rebuild (otherwise the checkpoint could resurrect a larger db).
        VACUUM refuses to run inside a transaction by definition, so
        these executes deliberately stay outside `with self.conn:` —
        the caller must hold no open transaction (every call site here
        runs on an idle connection)."""
        before = self.db_bytes()
        # Consume the checkpoint row so the cursor is fully read.
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self.conn.execute("VACUUM")
        after = self.db_bytes()
        return {"before": before, "after": after, "freed": before - after}

    def check(self) -> dict[str, Any]:
        """Physical-DB diagnostic (v0.2.670): the integrity_check verdict,
        the foreign_key_check violation count, and the applied schema
        version against the expected head.

        A file that cannot even be opened never reaches this method —
        callers surface that as "unopenable" themselves. Corruption found
        mid-check propagates as sqlite3.DatabaseError from the PRAGMA,
        which IS the diagnostic the caller reports, so it is deliberately
        not swallowed here.
        """
        rows = self.conn.execute("PRAGMA integrity_check(20)").fetchall()
        errors = [str(r[0]) for r in rows]
        integ_ok = errors == ["ok"]
        fk_rows = self.conn.execute("PRAGMA foreign_key_check").fetchall()
        row = self.conn.execute(
            "SELECT MAX(version) AS v FROM schema_migrations"
        ).fetchone()
        # v0.2.671: logical integrity — a chunk with no embedding is a dead
        # vector leg the ingest-time "0 embedded" toast leaves invisible
        # afterwards. Counts only: `ok` stays the PHYSICAL verdict (an
        # embed-disabled install legitimately has every chunk unembedded).
        chunk_row = self.conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN embedding IS NULL THEN 1 ELSE 0 END) AS miss"
            " FROM chunks"
        ).fetchone()
        expected = MIGRATIONS[-1][0] if MIGRATIONS else 0
        return {
            "ok": integ_ok and not fk_rows,
            "integrity": "ok" if integ_ok else "corrupt",
            "integrity_errors": [] if integ_ok else errors,
            "fk_violations": len(fk_rows),
            "schema_version": int(row["v"] or 0),
            "expected_version": expected,
            "chunks": int(chunk_row["n"] or 0),
            "unembedded": int(chunk_row["miss"] or 0),
        }

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
            " ORDER BY n.updated_at DESC, n.id DESC"
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
        _utf8(key, "key")
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Upsert a setting key/value pair."""
        _utf8(key, "key")
        _utf8(value, "value")
        self.conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)",
            (key, value),
        )
        self.conn.commit()

    # --- usage metrics ------------------------------------------------------

    def bump_metrics(self, deltas: dict[str, float]) -> None:
        """Add each delta to a named usage counter (durable, content-free).

        Counters are settings rows under `metric.*` keys — they survive
        restarts and need no schema migration. Each update is one
        INSERT..ON CONFLICT statement, so the read-modify-write happens
        inside SQLite's own expression and two writers on separate
        connections cannot lose increments the way a SELECT-then-UPDATE
        pair would. Names are the product's own constants and values are
        counts/millisecond sums — user content never lands here (the same
        privacy boundary as shoin/log.py's _PRIVATE_FIELDS).

        Best-effort by contract: a counter write must never break the
        operation it counts, so database-level failures are swallowed.
        """
        if not deltas:
            return
        try:
            with self.conn:
                for name, delta in deltas.items():
                    self.conn.execute(
                        "INSERT INTO settings(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET "
                        "value = CAST(CAST(value AS REAL) + ? AS TEXT)",
                        (_METRIC_PREFIX + name, repr(float(delta)), float(delta)),
                    )
        except (OSError, sqlite3.Error):
            pass

    def usage_metrics(self) -> dict[str, float]:
        """Every `metric.*` counter as name -> value.

        A hand-corrupted settings row (non-numeric text) is skipped rather
        than breaking the metrics surface that reads it.
        """
        rows = self.conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?",
            (_METRIC_PREFIX + "%",),
        ).fetchall()
        out: dict[str, float] = {}
        for r in rows:
            try:
                out[str(r["key"])[len(_METRIC_PREFIX):]] = float(r["value"])
            except (TypeError, ValueError):
                continue
        return out
