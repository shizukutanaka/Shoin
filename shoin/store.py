"""SQLite persistence layer: migrations, notebooks, sources, chunks, FTS5.

Single-file database. Foreign keys + WAL. FTS5 uses the trigram tokenizer so
that CJK text is searchable without external tokenizers (SQLite >= 3.34).
"""

from __future__ import annotations

import array
import base64
import contextlib
import hashlib
import json
import math
import operator
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, TypeVar

from .chunk import _MAX_CONTEXT_CHARS
from .config import (
    MAX_BODY_LEN,
    MAX_CHUNKS_PER_NOTEBOOK,
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
    (
        15,
        # Content epoch for the src_text pager (v0.2.706): a chunk-text
        # mutation that keeps the count constant (update_chunk_text, or a
        # refresh whose re-chunk lands the same count) moves no row the
        # pager's `total` frame can see — splicing post-change rows under
        # pre-change ones tears the displayed document. The mutators bump
        # this counter so every page fetch can detect any content change.
        """
        ALTER TABLE sources ADD COLUMN content_rev INTEGER NOT NULL DEFAULT 0;
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
    # allow_nan=False: NaN/Infinity are not valid JSON — a persisted copy
    # would re-emit as the same non-standard literal on every response,
    # breaking strict JSON.parse consumers (v0.2.735, weakness #119).
    # ValueError here is a coded VALIDATION_FIELD_FORMAT_INVALID via
    # validate_source_meta, and NOTEBOOK_IMPORT_INVALID at import.
    return json.dumps(
        meta,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def validate_source_meta(meta: Any) -> None:
    """Enforce the source-meta contract: dict, JSON-serializable,
    serialized form ≤ SOURCE_META_MAX — shared by update_source_meta and
    request-side pre-validation so a multi-field PATCH rejects before the
    first write, never after a sibling field already committed (v0.2.728).
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


def validate_notebook_settings(settings: Any) -> None:
    """Enforce the notebook settings contract: NB_SETTING_KEYS only,
    bounded non-bool ints (config.py bounds mirror).

    Shared by update_notebook_settings and request-side pre-validation —
    a multi-field PATCH validates every field before the first write so a
    rejection never lands after a sibling field already committed
    (v0.2.728).
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
    for key, value in settings.items():
        lo, hi = _NB_SETTING_BOUNDS[key]
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"settings.{key} must be an integer in {lo}..{hi}",
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
        text = _meta_dump(value)
    elif isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("meta is not a JSON object")
        text = _meta_dump(parsed)
    else:
        raise ValueError("meta is not a JSON object")
    # v0.2.715: the writer-side SOURCE_META_MAX bound applies on every
    # normalization path, not just update_source_meta's PATCH — an
    # import/restore document reached _insert_tree_rows with meta
    # unbounded, so a giant meta object persisted verbatim and
    # _source_json embedded it on every detail fetch (the #97 family).
    if len(text.encode("utf-8")) > SOURCE_META_MAX:
        raise ValueError(f"meta exceeds {SOURCE_META_MAX} bytes")
    return text


# Per-key bounds every writer and importer agrees on (v0.2.710):
# update_notebook_settings rejects out-of-range values outright, and
# _import_settings_text drops them. Retrieval reads these keys raw.
_NB_SETTING_BOUNDS = {
    "top_k": (1, SEARCH_K_MAX),
    "source_text_tokens": (
        NB_SOURCE_TEXT_TOKENS_MIN,
        NB_SOURCE_TEXT_TOKENS_MAX,
    ),
}


def _import_settings_text(value: Any) -> str:
    """Validate a document's `settings` field to canonical JSON text.

    _meta_text gives the shape gate (None → '{}', non-object →
    ValueError → the caller's coded error). The value-level check then
    mirrors update_notebook_settings per-entry rather than per-object:
    an unknown key stays (it is inert on this build — a newer version
    may emit it), while a KNOWN key carrying a non-int or out-of-range
    value is dropped so the global default binds. A verbatim copy would
    let a crafted document persist e.g. top_k=10**9 (whole-table
    retrieval) or a non-int that detonates the reader's int() into a
    raw error (v0.2.710).
    """
    # _meta_text also bounds the whole object at SOURCE_META_MAX
    # (v0.2.715): per-entry limits would still let N medium entries
    # amplify the stored text; settings ride every detail response too.
    parsed = json.loads(_meta_text(value))
    kept = {
        k: v for k, v in parsed.items()
        if k not in _NB_SETTING_BOUNDS
        or (
            isinstance(v, int)
            and not isinstance(v, bool)
            and _NB_SETTING_BOUNDS[k][0] <= v <= _NB_SETTING_BOUNDS[k][1]
        )
    }
    return _meta_dump(kept)


def _import_str(value: Any) -> None:
    """Reject a document field that is not a storable UTF-8 string.

    _insert_tree_rows binds document fields verbatim: a non-str reaches
    sqlite as a raw InterfaceError (unbindable) or silently coerced
    garbage, and a lone surrogate dies as UnicodeEncodeError — never the
    coded document rejection. The StoreError carries
    NOTEBOOK_IMPORT_INVALID straight through the caller's except;
    .encode()'s UnicodeEncodeError is a ValueError, so the same generic
    rejection follows from the surrounding try either way.
    """
    if not isinstance(value, str):
        raise StoreError(
            "NOTEBOOK_IMPORT_INVALID", "export field is not a string"
        )
    # v0.2.714: a per-field size bound on EVERY document string, not just
    # bodies — _insert_tree_rows binds fields verbatim, so a giant title,
    # origin, sha256, chunk text or timestamp also persists at whatever
    # size the MAX_IMPORT_BYTES doc cap allows and then amplifies through
    # the verbatim embeds (title/origin ride _source_json on every detail
    # fetch; chunk text is loaded whole on every retrieval).
    if len(value) > MAX_BODY_LEN:
        raise StoreError(
            "NOTEBOOK_IMPORT_INVALID", "export field exceeds the field limit"
        )
    value.encode("utf-8")


_IMPORTED_ORIGIN_PREFIX = "imported:"


def _neutralize_import_origins(doc: dict[str, Any]) -> dict[str, Any]:
    """File-path origins in an untrusted export must not become
    refreshable: refresh re-reads file origins from disk, so a shared
    document carrying /etc/passwd would turn refresh_source — and the
    API refresh endpoint that calls it — into an arbitrary-file read of
    whatever the doc named. Non-URL origins are kept, prefixed, for
    display only. Enforced inside import_notebook itself so the CLI
    `shoin import` entry shares the guard _h_nb_import used to apply
    alone (v0.2.705 — the store is the sink every caller funnels
    through; merge/trash-restore skip it because their rows come from
    the user's own archives, not a foreign document)."""
    sources = doc.get("sources") if isinstance(doc, dict) else None
    if isinstance(sources, list):
        for s in sources:
            origin = s.get("origin") if isinstance(s, dict) else None
            if isinstance(origin, str) and not origin.startswith(
                ("http://", "https://")
            ):
                s["origin"] = _IMPORTED_ORIGIN_PREFIX + origin
    return doc


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
    report_json: str | None,
    id_map: dict[Any, int],
    chunk_map: dict[Any, int] | None = None,
) -> str | None:
    """Rewrite a citation_report's id pointers through an id remap.

    Reports carry real source ids ({"S1": 4}) — a verbatim copy across
    duplicate/import/restore leaves dead or wrong pointers once the
    tree is re-inserted under fresh ids. S# keys and every other field
    pass through; an entry whose source did not come along is dropped
    rather than left pointing at a dead row. With `chunk_map`, each
    source_chunk_ids list ({"S1": [12, 13]}) is rewritten the same way
    (v0.2.686) — chunk rowids are re-assigned on every re-insert, so a
    verbatim copy pointed at dead or unrelated chunks. A non-JSON or
    non-dict report passes through verbatim (corrupt-report
    convention).
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
        sci = report.get("source_chunk_ids")
        if chunk_map is not None and isinstance(sci, dict):
            mapped_sci: dict[str, list[int]] = {}
            for k, ids in sci.items():
                if not isinstance(ids, list):
                    continue
                kept = [
                    chunk_map[i]
                    for i in ids
                    if isinstance(i, int)
                    and not isinstance(i, bool)
                    and i in chunk_map
                ]
                if kept:
                    mapped_sci[str(k)] = kept
            report["source_chunk_ids"] = mapped_sci
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
    # Chunk-content epoch (v0.2.706, migration 15): bumped by every
    # mutation of the source's chunk text so the src_text pager can reject
    # a page that crosses a mid-read edit — the `total` count cannot see
    # same-count replacements.
    content_rev: int = 0


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

    @contextlib.contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Hold one consistent read view across several SELECTs.

        pysqlite never begins a transaction for SELECT, so `with self.conn`
        leaves every statement on its own auto-commit snapshot — a concurrent
        writer's commit lands between two of them and the caller assembles
        state that never existed together (an export with sources listed but
        their chunks already deleted). An explicit BEGIN pins the WAL read
        snapshot for the whole block; writers on other connections proceed
        unblocked (WAL readers don't serialize against writers).
        """
        self.conn.execute("BEGIN")
        try:
            yield
        finally:
            # Reads never mutate, so ROLLBACK is unconditional: it ends the
            # snapshot on the success path AND on exceptions alike — and is a
            # no-op should the body somehow have ended the TX already.
            self.conn.rollback()

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
        validate_notebook_settings(settings)
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
        with self.conn:
            # BEGIN IMMEDIATE (v0.2.683): take the write lock BEFORE the
            # payload read. With the default deferred begin, the
            # auto-commit payload SELECTs ran under no lock — a row
            # committed by another writer in the gap between serialize
            # and DELETE was removed without ever being archived.
            self.conn.execute("BEGIN IMMEDIATE")
            nb = self.get_notebook(notebook_id)
            payload = self._notebook_tree_payload(notebook_id)
            self.conn.execute(
                "INSERT INTO trash_items(notebook_id, name, deleted_at, payload)"
                " VALUES(?,?,?,?)",
                (notebook_id, nb.name, _now(), payload),
            )
            self.conn.execute("DELETE FROM notebooks WHERE id=?", (notebook_id,))

    def count_notebooks(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM notebooks"
        ).fetchone()
        return int(row["n"]) if row else 0

    def trash_list(
        self, limit: int | None = None, offset: int = 0
    ) -> list[TrashItem]:
        """Newest-first trash index — columns only, payload never parsed.

        limit/offset page the result (GET /api/trash, v0.2.696) — the CLI
        keeps passing no limit and still gets the full list.
        """
        if limit is not None:
            rows = self.conn.execute(
                "SELECT id, notebook_id, name, deleted_at, kind"
                " FROM trash_items ORDER BY deleted_at DESC, id DESC"
                " LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
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

    def count_trash(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM trash_items"
        ).fetchone()
        return int(row["n"]) if row else 0

    def trash_restore(self, trash_id: int) -> dict[str, Any]:
        """Re-insert a trashed entity in one TX, dispatching on payload kind.

        Notebook archives (v0.2.654) keep the notebook's own id and
        re-insert children under fresh rowids via the shared tree
        writer (v0.2.686) — chunk INSERTs re-fire the FTS triggers and
        base64 vectors land verbatim, so a restored tree is searchable
        immediately at zero re-embed cost, and recycled rowids can
        never collide with the restore. Source archives (v0.2.667)
        follow the same contract inside their parent notebook, which
        must still exist (NOTEBOOK_NOT_FOUND): the probe pairs the
        archived notebook_id with the archived created_at (v0.2.689),
        so a rowid recycled by delete+create counts as "parent gone",
        and an occupied source id refuses with SOURCE_ALREADY_EXISTS,
        never a silent merge or id rewrite.
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
                nb["settings"] = _import_settings_text(nb.get("settings"))
            elif kind == "source":
                src = payload["source"]
                chunks = payload["chunks"]
                for c in chunks:
                    if c["embedding"] is not None:
                        c["embedding"] = base64.b64decode(c["embedding"]["$blob"])
                src["meta"] = _meta_text(src.get("meta"))
                nb_id = int(src["notebook_id"])
                nb_created_at = payload.get("nb_created_at")
                nb_created_at = (
                    str(nb_created_at) if nb_created_at is not None else None
                )
            elif kind == "note":
                note = payload["note"]
                nb_id = int(note["notebook_id"])
                nb_created_at = payload.get("nb_created_at")
                nb_created_at = (
                    str(nb_created_at) if nb_created_at is not None else None
                )
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
            return self._restore_trashed_source(
                src, chunks, nb_id, trash_id, nb_created_at
            )
        return self._restore_trashed_note(note, nb_id, trash_id, nb_created_at)

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
        with self.conn:
            # BEGIN IMMEDIATE (v0.2.685): the ALREADY_EXISTS probe must run
            # under the write lock — an auto-commit read left a gap where a
            # concurrent create turned the coded refusal into a raw
            # IntegrityError on INSERT (same TOCTOU class as v0.2.683).
            self.conn.execute("BEGIN IMMEDIATE")
            if self.conn.execute(
                "SELECT 1 FROM notebooks WHERE id=?", (nb["id"],)
            ).fetchone():
                raise StoreError(
                    "NOTEBOOK_ALREADY_EXISTS",
                    f"notebook {nb['id']} already exists — cannot restore over it",
                )
            self.conn.execute(
                "INSERT INTO notebooks(id, name, created_at, updated_at, settings)"
                " VALUES(?,?,?,?,?)",
                (nb["id"], nb["name"], nb["created_at"], nb["updated_at"],
                 nb["settings"]),
            )
            # v0.2.686: children re-insert through the shared tree writer
            # under FRESH ids. This path used to re-insert the archived
            # ids verbatim, but INTEGER PRIMARY KEY rowids recycle as
            # max(rowid)+1 — once a later insert took an archived
            # source/chunk/note/studio/message id, restore died on a raw
            # PRIMARY KEY conflict the probes never covered (the only
            # probe checked the notebook id itself).
            self._insert_tree_rows(
                nb["id"], sources, chunks, notes, studio_outputs, messages
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
        nb_created_at: str | None,
    ) -> dict[str, Any]:
        """Source archive restore (v0.2.667): original id inside its parent
        notebook — chunk INSERTs re-fire the FTS triggers, so the source is
        searchable again in the same commit that lands it. The parent probe
        pairs id with the archived created_at (v0.2.689): rowids recycle,
        so an id-only probe would mis-parent the source into whatever
        notebook later reoccupied the id. Archives predating the key fall
        back to the id-only check."""
        with self.conn:
            # BEGIN IMMEDIATE (v0.2.685): both probes run under the write
            # lock — a concurrent delete/create in the gap used to surface
            # as a raw FK/PK IntegrityError instead of the coded refusal.
            self.conn.execute("BEGIN IMMEDIATE")
            nb_row = self.conn.execute(
                "SELECT created_at FROM notebooks WHERE id=?", (nb_id,)
            ).fetchone()
            if nb_row is None or (
                nb_created_at is not None
                and str(nb_row["created_at"]) != nb_created_at
            ):
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
                # v0.2.686: chunks take fresh rowids — deleted ids recycle
                # as max(rowid)+1, so re-inserting the archived ids
                # collided with whatever later insert reused them.
                self.conn.execute(
                    "INSERT INTO chunks(source_id, seq, text, context,"
                    " embedding, embedding_norm) VALUES(?,?,?,?,?,?)",
                    (
                        src["id"], c["seq"], c["text"],
                        c["context"], c["embedding"], c["embedding_norm"],
                    ),
                )
            self.conn.execute("DELETE FROM trash_items WHERE id=?", (trash_id,))
            self.touch_notebook(nb_id)
            self._optimize_fts()
        return {"kind": "source", "id": int(src["id"]), "name": str(src["title"])}

    def _restore_trashed_note(
        self, note: dict[str, Any], nb_id: int, trash_id: int,
        nb_created_at: str | None,
    ) -> dict[str, Any]:
        """Note archive restore (v0.2.667): fresh id inside its parent
        notebook — nothing outside delete/list references note ids, so
        re-assignment loses nothing and the insert can never collide. The
        parent probe pairs id with the archived created_at (v0.2.689) for
        the same recycled-rowid reason as the source restore."""
        with self.conn:
            # BEGIN IMMEDIATE (v0.2.685): the parent probe runs under the
            # write lock — a concurrent delete_notebook in the gap used to
            # turn the coded NOTEBOOK_NOT_FOUND into a raw FK violation.
            self.conn.execute("BEGIN IMMEDIATE")
            nb_row = self.conn.execute(
                "SELECT created_at FROM notebooks WHERE id=?", (nb_id,)
            ).fetchone()
            if nb_row is None or (
                nb_created_at is not None
                and str(nb_row["created_at"]) != nb_created_at
            ):
                raise StoreError(
                    "NOTEBOOK_NOT_FOUND",
                    f"notebook {nb_id} is gone — cannot restore note into it",
                )
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
        valid import document, and vice versa.

        Read under one WAL snapshot (v0.2.703): _notebook_tree_dict issues
        several auto-commit SELECTs, so a concurrent delete could land
        between them and serialize a torn document — sources present but
        chunks/notes/messages gone. The md/bib/ris exports got the same
        guard in v0.2.700; this path is the machine-transfer envelope, so
        a torn doc here gets *imported* and the loss persists on the far
        side. Trash-restore callers run inside their own write TX and
        call _notebook_tree_dict directly, so the snapshot lives here at
        the standalone-read boundary only."""
        with self.read_snapshot():
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

        The payload is untrusted bytes: file-path origins it carries are
        neutralized before anything else reads them (v0.2.705). The call
        stays inside the try so a non-dict payload still classifies as
        NOTEBOOK_IMPORT_INVALID rather than a raw AttributeError.
        """
        try:
            _neutralize_import_origins(payload)
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
            # malformed like a non-object meta; _import_settings_text also
            # drops out-of-range/non-int values on known keys (v0.2.710).
            nb_settings_text = _import_settings_text(nb.get("settings"))
            src_ids: set[Any] = set()
            for s in sources:
                # v0.2.692: a crafted export may repeat a source id —
                # _insert_tree_rows keys id_map by the doc id, so the LAST
                # duplicate wins and every chunk/report bound to that id
                # silently rebinds to the wrong source (the earlier row
                # lands with zero chunks). Reject duplicates outright.
                if s["id"] in src_ids:
                    raise StoreError(
                        "NOTEBOOK_IMPORT_INVALID",
                        "export lists the same source id more than once",
                    )
                src_ids.add(s["id"])
                for k in ("kind", "title", "origin", "sha256", "added_at"):
                    s[k]
                # v0.2.693: document rows bypass every guard the write
                # path enforces — _insert_tree_rows binds fields verbatim
                # while add_source/update_source_weight/add_studio_output/
                # add_message enforce the kind/role vocabularies and a
                # finite 0..SOURCE_WEIGHT_MAX range, and _utf8 every bound
                # string. Without the same checks here a crafted export
                # persists an out-of-vocabulary kind or role (phantom rows
                # no caller can overwrite) or an Infinity weight; a NaN
                # weight, a dict field, or a lone surrogate instead died
                # on a raw sqlite error — not the coded document
                # rejection. _import_str enforces str-typed, UTF-8-
                # encodable text fields; encode failures surface as
                # ValueError → NOTEBOOK_IMPORT_INVALID below.
                if s["kind"] not in SOURCE_KINDS:
                    raise StoreError(
                        "NOTEBOOK_IMPORT_INVALID",
                        "export source kind is not a known kind",
                    )
                # weight is optional in the document (pre-v0.2.657 exports
                # lack it) but must be numeric when present; meta is the
                # same — absent or the canonical object/string only
                # (pre-v0.2.658).
                w = float(s.get("weight", 1.0))
                if not math.isfinite(w) or not 0.0 <= w <= SOURCE_WEIGHT_MAX:
                    raise StoreError(
                        "NOTEBOOK_IMPORT_INVALID",
                        "export source weight is outside the finite range",
                    )
                for k in ("title", "origin", "sha256", "added_at"):
                    _import_str(s[k])
                _meta_text(s.get("meta"))
            for c in chunks:
                if c["source_id"] not in src_ids:
                    raise StoreError(
                        "NOTEBOOK_IMPORT_INVALID",
                        "chunk references a source not in the export",
                    )
                for k in ("seq", "text", "context", "embedding_norm"):
                    c[k]
                for k in ("text", "context"):
                    _import_str(c[k])
                # v0.2.736: json's float parser — never parse_constant —
                # produces infinity for a legal exponent like 1e400, so
                # file bytes can carry a non-finite seq/embedding_norm
                # into the verbatim bind; seq re-emits as Infinity on
                # every source-text/search response (strict-parser
                # poison, Devin Review on v0.2.735).
                if not math.isfinite(float(c["seq"])):
                    raise StoreError(
                        "NOTEBOOK_IMPORT_INVALID",
                        "chunk seq is not finite",
                    )
                # embedding_norm is NULL whenever the chunk carries no
                # embedding — a legit export emits null, only a present
                # value must be a finite number.
                if c["embedding_norm"] is not None and not math.isfinite(
                    float(c["embedding_norm"])
                ):
                    raise StoreError(
                        "NOTEBOOK_IMPORT_INVALID",
                        "chunk embedding_norm is not finite",
                    )
                if c["embedding"] is not None:
                    # v0.2.714: bound the base64 BEFORE decode — the blob
                    # binds verbatim and is loaded on every vector
                    # retrieval, so a giant one amplifies like a giant
                    # text field. (Legit embeddings are ~dim*4 bytes,
                    # i.e. ≤~22KB base64 for 4k-dim vectors.)
                    blob64 = c["embedding"]["$blob"]
                    if isinstance(blob64, str) and len(blob64) > MAX_BODY_LEN:
                        raise StoreError(
                            "NOTEBOOK_IMPORT_INVALID",
                            "export embedding blob exceeds the field limit",
                        )
                    c["embedding"] = base64.b64decode(blob64)
            for n in notes:
                for k in ("title", "body", "created_at"):
                    n[k]
                for k in ("title", "body", "created_at"):
                    _import_str(n[k])
            for o in studio_outputs:
                for k in ("kind", "body", "citation_report", "created_at"):
                    o[k]
                if o["kind"] not in STUDIO_KINDS:
                    raise StoreError(
                        "NOTEBOOK_IMPORT_INVALID",
                        "export studio kind is not a known kind",
                    )
                for k in ("body", "citation_report", "created_at"):
                    _import_str(o[k])
            for m in messages:
                for k in ("role", "body", "citation_report", "created_at"):
                    m[k]
                if m["role"] not in ("user", "assistant"):
                    raise StoreError(
                        "NOTEBOOK_IMPORT_INVALID",
                        "export message role is not a known role",
                    )
                for k in ("body", "citation_report", "created_at"):
                    _import_str(m[k])
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
        # v0.2.679: dedupe sources by sha256 before inserting. UNIQUE is
        # (notebook_id, sha256), so a merge whose source notebook shares
        # content with the target — or a crafted import listing the same
        # sha twice — used to die on a raw IntegrityError (HTTP 500) after
        # partially inserting.
        # v0.2.682: dedupe only on identical CONTENT, not just an
        # identical sha — update_chunk_text edits a chunk without
        # touching the source's origin-sha, so an edited copy carries a
        # stale label. Same sha + same (seq, text) corpus → remap onto
        # the existing row and skip its chunk INSERTs (identical chunks,
        # citation_reports resolve via the remap). Same sha + different
        # corpus → the doc's sha no longer describes its content, so
        # recompute the label from the doc's own chunks and keep BOTH
        # versions — discarding the incoming chunks was silent data loss.
        src_chunks: dict[Any, list[tuple[Any, str]]] = {}
        for c in chunks:
            src_chunks.setdefault(c["source_id"], []).append(
                (c["seq"], c["text"])
            )
        # sha -> owner: an existing sources.id (int) or the dict of a doc
        # source still waiting for its INSERT (in-document collisions).
        seen_sha: dict[str, Any] = {
            str(row[1]): int(row[0])
            for row in self.conn.execute(
                "SELECT id, sha256 FROM sources WHERE notebook_id=?",
                (notebook_id,),
            )
        }

        def _corpus(owner: Any) -> list[tuple[Any, str]]:
            if isinstance(owner, int):
                return [
                    (r[0], r[1])
                    for r in self.conn.execute(
                        "SELECT seq, text FROM chunks WHERE source_id=?",
                        (owner,),
                    )
                ]
            return list(src_chunks.get(owner["id"], []))

        deduped: set[Any] = set()
        dedupe_owner: dict[Any, Any] = {}
        for s in sources:
            mine = sorted(src_chunks.get(s["id"], []))
            sha = str(s["sha256"])
            n = -1  # -1 = the doc's own sha; >=0 = rehash with salt n
            while True:
                owner = seen_sha.get(sha)
                if owner is None:
                    seen_sha[sha] = s
                    s["sha256"] = sha
                    break
                if sorted(_corpus(owner)) == mine:
                    deduped.add(s["id"])
                    dedupe_owner[s["id"]] = owner
                    break
                n += 1
                h = hashlib.sha256()
                if n:
                    h.update(str(n).encode("ascii"))
                for _seq, t in mine:
                    h.update(t.encode("utf-8"))
                    h.update(b"\n")
                sha = h.hexdigest()
        # v0.2.672: the per-notebook chunk cap is a product invariant, not
        # an ingest-rate limit — the vector leg scans every chunk in a
        # notebook, so an over-cap corpus slows EVERY query on it. Until
        # now only the ingest writers (index_source/refresh_source, in
        # pipeline.py) enforced it; import and merge bypassed it entirely.
        # Guarding the shared tree writer covers both at one point.
        # v0.2.679: only genuinely-new chunks count — deduped sources
        # contribute none.
        existing = self.counts(notebook_id)["chunks"]
        incoming = sum(1 for c in chunks if c["source_id"] not in deduped)
        if existing + incoming > MAX_CHUNKS_PER_NOTEBOOK:
            raise StoreError(
                "INGEST_NOTEBOOK_FULL",
                f"notebook chunk limit exceeded: {existing} existing"
                f" + {incoming} incoming > {MAX_CHUNKS_PER_NOTEBOOK}",
            )
        id_map: dict[Any, int] = {}
        for s in sources:
            if s["id"] in deduped:
                owner = dedupe_owner[s["id"]]
                # An int owner is an existing row; a dict owner is a doc
                # source already INSERTed earlier in this loop.
                if isinstance(owner, int):
                    id_map[s["id"]] = owner
                else:
                    id_map[s["id"]] = id_map[owner["id"]]
                continue
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
            seen_sha[str(s["sha256"])] = id_map[s["id"]]
        chunk_id_map: dict[Any, int] = {}
        for c in chunks:
            if c["source_id"] in deduped:
                continue
            cur = self.conn.execute(
                "INSERT INTO chunks(source_id, seq, text, context,"
                " embedding, embedding_norm) VALUES(?,?,?,?,?,?)",
                (
                    id_map[c["source_id"]], c["seq"], c["text"],
                    c["context"], c["embedding"], c["embedding_norm"],
                ),
            )
            # v0.2.686: reports also carry source_chunk_ids — remap the
            # chunk pointers through a fresh-id map too, or re-inserted
            # reports point at dead or unrelated rows.
            chunk_id_map[c["id"]] = int(cur.lastrowid or 0)
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
                    _remap_report_source_ids(
                        o["citation_report"], id_map, chunk_id_map
                    ),
                    o["created_at"],
                ),
            )
        for m in messages:
            self.conn.execute(
                "INSERT INTO messages(notebook_id, role, body,"
                " citation_report, created_at) VALUES(?,?,?,?,?)",
                (
                    notebook_id, m["role"], m["body"],
                    _remap_report_source_ids(
                        m["citation_report"], id_map, chunk_id_map
                    ),
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
        then archived to trash and deleted in the same transaction:
        a merge is recoverable via `trash restore`.

        One transaction (v0.2.687): serialize, copy, archive and delete
        all run under a single BEGIN IMMEDIATE write lock. The earlier
        two-transaction shape serialized the source under auto-commit
        and deleted afterwards — a row committed by another writer in
        between was archived into trash but never copied into the
        target. A crash anywhere now rolls the whole merge back:
        nothing is duplicated, nothing is dropped.
        """
        self.get_notebook(target_id)
        self.get_notebook(source_id)
        if target_id == source_id:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                "cannot merge a notebook into itself",
            )
        with self.conn:
            # Re-probe + serialize under the write lock: an existence
            # result older than the lock is stale, and serialize-then-
            # delete must not observe a tree that changed mid-way.
            self.conn.execute("BEGIN IMMEDIATE")
            self.get_notebook(target_id)
            try:
                doc = self._notebook_tree_dict(source_id)
                payload = json.dumps(doc, ensure_ascii=False)
                for c in doc["chunks"]:
                    if c["embedding"] is not None:
                        c["embedding"] = base64.b64decode(
                            c["embedding"]["$blob"]
                        )
                self._insert_tree_rows(
                    target_id,
                    doc["sources"],
                    doc["chunks"],
                    doc["notes"],
                    doc["studio_outputs"],
                    doc["messages"],
                )
                # The archive is the same serialized tree — under the
                # lock it is exactly what the delete removes.
                self.conn.execute(
                    "INSERT INTO trash_items(notebook_id, name, deleted_at,"
                    " payload) VALUES(?,?,?,?)",
                    (
                        source_id,
                        doc["notebook"]["name"],
                        _now(),
                        payload,
                    ),
                )
                self.conn.execute(
                    "DELETE FROM notebooks WHERE id=?", (source_id,)
                )
                self.touch_notebook(target_id)
                self._optimize_fts()
            except (KeyError, TypeError, ValueError) as exc:
                raise StoreError(
                    "SYSTEM_INTERNAL_ERROR",
                    f"notebook {source_id} tree could not be serialized",
                ) from exc
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
        ts = _now()
        with self.conn:
            # BEGIN IMMEDIATE (v0.2.708): the existence probe, the
            # chunk-cap probe and every INSERT..SELECT of the copy must
            # share one write-TX snapshot. Under the deferred begin they
            # ran on different commit points — a concurrent delete of the
            # source notebook in the gap produced a committed "duplicate"
            # with zero child rows and no error.
            self.conn.execute("BEGIN IMMEDIATE")
            src = self.get_notebook(notebook_id)
            if name is None:
                suffix = " (copy)"
                name = src.name[: MAX_NAME_LEN - len(suffix)] + suffix
            name = name.strip()
            if not name:
                raise StoreError(
                    "VALIDATION_REQUIRED_FIELD_MISSING", "notebook name is empty"
                )
            if len(name) > MAX_NAME_LEN:
                raise StoreError(
                    "VALIDATION_FIELD_FORMAT_INVALID",
                    f"name too long (max {MAX_NAME_LEN} chars)",
                )
            _utf8(name, "name")
            # v0.2.672: this fork copies the tree itself rather than via
            # _insert_tree_rows (INSERT..SELECT then, per-row since v0.2.733
            # so the fresh chunk rowids can be mapped), so it needs its own
            # cap check — a normal notebook cannot breach it (0 + same
            # count), but duplicating an already over-limit notebook would
            # replicate the broken invariant. Inside the transaction like
            # the sibling guard.
            if self.counts(notebook_id)["chunks"] > MAX_CHUNKS_PER_NOTEBOOK:
                raise StoreError(
                    "INGEST_NOTEBOOK_FULL",
                    f"notebook chunk limit exceeded: "
                    f"{self.counts(notebook_id)['chunks']} chunks"
                    f" > {MAX_CHUNKS_PER_NOTEBOOK}"
                    " — cannot duplicate an over-limit notebook",
                )
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
            chunk_id_map: dict[int, int] = {}
            for row in self.conn.execute(
                "SELECT c.id, c.source_id, c.seq, c.text, c.context,"
                " c.embedding, c.embedding_norm FROM chunks c"
                " JOIN sources s ON s.id=c.source_id WHERE s.notebook_id=?"
                " ORDER BY c.source_id, c.seq",
                (notebook_id,),
            ).fetchall():
                cur = self.conn.execute(
                    "INSERT INTO chunks(source_id, seq, text, context,"
                    " embedding, embedding_norm) VALUES(?,?,?,?,?,?)",
                    (
                        id_map[int(row["source_id"])], row["seq"], row["text"],
                        row["context"], row["embedding"], row["embedding_norm"],
                    ),
                )
                # v0.2.733: the per-row insert replaces INSERT..SELECT so the
                # fresh chunk rowid lands in chunk_id_map — the report remap
                # below rewrites source_chunk_ids through it. Without it a
                # duplicated report kept pointing at the SOURCE notebook's
                # chunks (live rows under a different notebook — a verbatim
                # cross-notebook pointer, the exact tear v0.2.686 closed on
                # the import/merge/restore paths).
                chunk_id_map[int(row["id"])] = int(cur.lastrowid or 0)
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
                        _remap_report_source_ids(
                            row["citation_report"], id_map, chunk_id_map
                        ),
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
                        _remap_report_source_ids(
                            row["citation_report"], id_map, chunk_id_map
                        ),
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
        if isinstance(origin, str) and len(origin) > MAX_BODY_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"origin too long (max {MAX_BODY_LEN} chars)",
            )
        if isinstance(sha256, str) and len(sha256) > MAX_BODY_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"sha256 too long (max {MAX_BODY_LEN} chars)",
            )
        _utf8(title, "title")
        _utf8(origin, "origin")
        _utf8(sha256, "sha256")
        ts = _now()
        try:
            # `with self.conn:` commits INSERT+touch atomically and rolls both
            # back on failure — a failed touch must not leave the new row
            # pending for a later commit on this connection to publish (the
            # caller sees an error yet the source appears). Same leak class
            # as the v0.2.417-418 add_studio_output fixes.
            with self.conn:
                # BEGIN IMMEDIATE (v0.2.731): the parent probe AND the dedupe
                # probe must see the same commit point the INSERT lands on —
                # run at autocommit, they could see a notebook rowid later
                # deleted and reused, inserting the source under a notebook
                # the probe never saw.
                self.conn.execute("BEGIN IMMEDIATE")
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
        if isinstance(origin, str) and len(origin) > MAX_BODY_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"origin too long (max {MAX_BODY_LEN} chars)",
            )
        _utf8(title, "title")
        _utf8(origin, "origin")
        with self.conn:
            # BEGIN IMMEDIATE (v0.2.712): the title AND notebook_id must come
            # from the same commit point the UPDATE lands on — a pre-lock
            # read could see a rowid later deleted and reused, steering the
            # context rewrite's old-title and the touch_notebook below to
            # stale values. src is the locked-snapshot read, so it IS the
            # re-read the older in-transaction SELECT provided.
            self.conn.execute("BEGIN IMMEDIATE")
            src = self.get_source(source_id)  # raises SOURCE_NOT_FOUND if missing
            old_title = str(src.title)
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

    @staticmethod
    def _source_of(row: sqlite3.Row) -> Source:
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
            int(row["content_rev"]) if "content_rev" in row.keys() else 0,
        )

    def sources_for_notebook(self, notebook_id: int) -> list[Source]:
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE notebook_id=? ORDER BY id", (notebook_id,)
        ).fetchall()
        return [self._source_of(r) for r in rows]

    def list_sources_page(
        self, notebook_id: int, offset: int, limit: int
    ) -> list[Source]:
        # Newest-first: page 0 overlaps the detail payload's embedded sources.
        return [
            self._source_of(r)
            for r in self.conn.execute(
                "SELECT * FROM sources WHERE notebook_id=? ORDER BY id DESC"
                " LIMIT ? OFFSET ?",
                (notebook_id, limit, offset),
            ).fetchall()
        ]

    def count_sources(self, notebook_id: int) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM sources WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()["n"]
        )

    def get_source(self, source_id: int) -> Source:
        row = self.conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise StoreError("SOURCE_NOT_FOUND", f"source {source_id} not found")
        return self._source_of(row)

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
        # Undo-log trash (v0.2.667): same archive-then-delete TX as
        # delete_notebook — a deleted upload whose tmp origin is long
        # gone is unrecoverable without this record. Chunks ride in the
        # payload (base64 embeddings) and re-fire FTS triggers on restore.
        with self.conn:
            # BEGIN IMMEDIATE (v0.2.683): same TOCTOU as delete_notebook —
            # the payload read must run under the write lock or a
            # concurrent refresh/commit is deleted unarchived.
            self.conn.execute("BEGIN IMMEDIATE")
            src = self.get_source(source_id)  # raises SOURCE_NOT_FOUND if missing
            src_row = self.conn.execute(
                "SELECT * FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            # Parent identity beyond the raw id (v0.2.689): INTEGER PRIMARY
            # KEY rowids recycle as max+1, so the restore probe must pair
            # id with created_at or a deleted-and-recreated notebook
            # silently adopts the restored source.
            nb_row = self.conn.execute(
                "SELECT created_at FROM notebooks WHERE id=?", (src.notebook_id,)
            ).fetchone()
            payload = json.dumps(
                {
                    "kind": "source",
                    "source": dict(src_row) if src_row is not None else {},
                    "chunks": self._chunk_dicts(source_id),
                    "nb_created_at": (
                        str(nb_row["created_at"]) if nb_row is not None else None
                    ),
                },
                ensure_ascii=False,
            )
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
        ids: list[int] = []
        try:
            with self.conn:
                # BEGIN IMMEDIATE (v0.2.709): the chunk-cap probe must see
                # the same commit point the inserts land on — pipeline's
                # pre-check read an unlocked count, so two concurrent
                # replaces in one notebook could both pass and over-fill
                # it. The notebook-total-excluding-this-source formula
                # mirrors pipeline.refresh_source's.
                # The src probe also runs under the lock (v0.2.712): a
                # pre-lock read could see a rowid later deleted and
                # reused, steering the cap count and the touch to the
                # wrong notebook.
                self.conn.execute("BEGIN IMMEDIATE")
                src = self.get_source(source_id)  # raises SOURCE_NOT_FOUND if missing
                nb_chunks = self.counts(src.notebook_id)["chunks"]
                here = self.count_chunks_for_source(source_id)
                if nb_chunks - here + len(texts) > MAX_CHUNKS_PER_NOTEBOOK:
                    raise StoreError(
                        "INGEST_NOTEBOOK_FULL",
                        f"notebook chunk limit exceeded: {nb_chunks - here} existing"
                        f" (excl. this source) + {len(texts)} new"
                        f" > {MAX_CHUNKS_PER_NOTEBOOK}",
                    )
                self.conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
                for seq, text in enumerate(texts):
                    ctx = contexts[seq] if contexts is not None else ""
                    cur = self.conn.execute(
                        "INSERT INTO chunks(source_id, seq, text, context) VALUES (?,?,?,?)",
                        (source_id, seq, text, ctx),
                    )
                    ids.append(int(cur.lastrowid or 0))
                # Bump the content epoch (v0.2.706): a same-count
                # replacement changes every chunk's text while `total`
                # stays put — the src_text pager needs this flag to
                # refuse splicing post-change pages under pre-change ones.
                self.conn.execute(
                    "UPDATE sources SET content_rev=content_rev+1 WHERE id=?",
                    (source_id,),
                )
                if sha256 is not None:
                    # COALESCE(?, title), not a Python-side `title or src.title`
                    # fallback: resolving the fallback in SQL reads the row's
                    # own current value atomically — the fix for refresh
                    # overwriting a user's custom title (v0.2.87).
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
        if isinstance(sha256, str) and len(sha256) > MAX_BODY_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"sha256 too long (max {MAX_BODY_LEN} chars)",
            )
        _utf8(title, "title")
        _utf8(sha256, "sha256")
        try:
            with self.conn:
                # BEGIN IMMEDIATE + src probe under the lock (v0.2.712):
                # same single-commit-point fix as update_source_title —
                # src.title is now the locked-snapshot value, so it IS the
                # in-transaction re-read the older SELECT provided.
                self.conn.execute("BEGIN IMMEDIATE")
                src = self.get_source(source_id)  # raises SOURCE_NOT_FOUND if missing
                old_title = str(src.title)
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
        validate_source_meta(meta)
        text = _meta_dump(meta)
        with self.conn:
            # BEGIN IMMEDIATE + src probe under the lock (v0.2.712): the
            # notebook_id the touch below targets must come from the same
            # commit point the UPDATE lands on.
            self.conn.execute("BEGIN IMMEDIATE")
            src = self.get_source(source_id)
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
        ids: list[int] = []
        try:
            with self.conn:
                # BEGIN IMMEDIATE (v0.2.709): the cap probe must see the
                # same commit point the inserts land on — pipeline's
                # pre-check read an unlocked count, so two concurrent
                # ingests could each pass and both commit, breaching
                # MAX_CHUNKS_PER_NOTEBOOK. The probe moves to the sink.
                # v0.2.731: the parent-source probe moves under the same
                # lock — at autocommit it could see a source rowid later
                # deleted and reused, steering the cap count and the
                # touch at a notebook the probe never saw while the
                # INSERT lands on the recycled row.
                self.conn.execute("BEGIN IMMEDIATE")
                src = self.get_source(source_id)  # raises SOURCE_NOT_FOUND if missing
                existing = self.counts(src.notebook_id)["chunks"]
                if existing + len(texts) > MAX_CHUNKS_PER_NOTEBOOK:
                    raise StoreError(
                        "INGEST_NOTEBOOK_FULL",
                        f"notebook chunk limit exceeded: {existing} existing"
                        f" + {len(texts)} new > {MAX_CHUNKS_PER_NOTEBOOK}",
                    )
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

    def _set_embedding_pair(
        self,
        chunk_id: int,
        blob: bytes,
        norm: float,
        expected_text: str | None = None,
    ) -> None:
        # Two statements, one transaction, and the order matters: writing the
        # embedding fires the migration-9 trigger, which clears the cached norm
        # unconditionally; the second statement then writes the norm that belongs
        # to the vector just stored. Doing both in ONE statement would need the
        # trigger to guess whether a writer knew about the norm column, and every
        # guess has a case it gets wrong (migration 8's did — see its note).
        # Measured: the split costs nothing (29.1 us vs 32.5 us per chunk).
        if expected_text is None:
            cur = self.conn.execute(
                "UPDATE chunks SET embedding=? WHERE id=?", (blob, chunk_id)
            )
        else:
            # The vector only lands while the row still carries the exact text
            # it was computed from. A deleted rowid that has been reused for a
            # different chunk's text misses the WHERE — same CHUNK_NOT_FOUND
            # contract as a plainly missing row, instead of silently storing
            # a vector that describes foreign content (v0.2.729).
            cur = self.conn.execute(
                "UPDATE chunks SET embedding=? WHERE id=? AND text=?",
                (blob, chunk_id, expected_text),
            )
        if cur.rowcount == 0:
            raise StoreError("CHUNK_NOT_FOUND", f"chunk {chunk_id} not found")
        self.conn.execute(
            "UPDATE chunks SET embedding_norm=? WHERE id=?", (norm, chunk_id)
        )

    def set_embedding(
        self,
        chunk_id: int,
        vec: list[float],
        *,
        commit: bool = True,
        expected_text: str | None = None,
    ) -> None:
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
                self._set_embedding_pair(
                    chunk_id, packed.tobytes(), norm, expected_text
                )
        else:
            # commit=False callers own the surrounding transaction
            # (_embed_chunks rolls back a partial batch on failure).
            self._set_embedding_pair(
                chunk_id, packed.tobytes(), norm, expected_text
            )

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

    def id_seq_text_chunks_for_source(
        self, source_id: int, *, limit: int | None = None, offset: int = 0
    ) -> list[tuple[int, int, str]]:
        """Return (chunk_id, seq, text) triples for a source, ordered by seq.

        The chunk id lets the source viewer mark exactly which passages an
        answer was grounded in (citation_report's source_chunk_ids). The older
        text_chunks_for_source() returns only (seq, text) and is kept as-is
        because other callers depend on that shape.

        limit/offset page the seq ordering (v0.2.695) — the text endpoint
        batches row fetches so an oversized source can't materialize its
        whole text in one shot.
        """
        sql = "SELECT id, seq, text FROM chunks WHERE source_id=? ORDER BY seq"
        args: tuple[int, ...] = (source_id,)
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args = (source_id, limit, offset)
        rows = self.conn.execute(sql, args).fetchall()
        return [(int(r["id"]), int(r["seq"]), str(r["text"])) for r in rows]

    def count_chunks_for_source(self, source_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (source_id,)
        ).fetchone()
        return int(row["n"])

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
        if len(text) > MAX_BODY_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"text too long (max {MAX_BODY_LEN} chars)",
            )
        _utf8(text, "text")
        with self.conn:
            # BEGIN IMMEDIATE + the JOIN probe under the lock (v0.2.712):
            # source_id/notebook_id must come from the same commit point
            # the UPDATE and rev-bump land on — a pre-lock read could see
            # a chunk rowid later deleted and reused under a different
            # source, steering the edit and the touch at the wrong rows.
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT c.id, c.source_id, s.notebook_id FROM chunks c"
                " JOIN sources s ON s.id=c.source_id WHERE c.id=?",
                (chunk_id,),
            ).fetchone()
            if row is None:
                raise StoreError("CHUNK_NOT_FOUND", f"chunk {chunk_id} not found")
            cur = self.conn.execute(
                "UPDATE chunks SET text=?, embedding=NULL, embedding_norm=NULL"
                " WHERE id=?",
                (text, chunk_id),
            )
            if cur.rowcount == 0:
                raise StoreError(
                    "CHUNK_NOT_FOUND", f"chunk {chunk_id} was concurrently deleted"
                )
            # Bump the source's content epoch (v0.2.706): a src_text page
            # fetched after this commits must not splice under the
            # pre-edit page — the row count did not move.
            rev_cur = self.conn.execute(
                "UPDATE sources SET content_rev=content_rev+1 WHERE id=?",
                (int(row["source_id"]),),
            )
            if rev_cur.rowcount == 0:
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
        if isinstance(body, str) and len(body) > MAX_BODY_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"body too long (max {MAX_BODY_LEN} chars)",
            )
        _utf8(title, "title")
        _utf8(body, "body")
        try:
            # Atomic INSERT+touch — same pending-leak guard as add_source
            # (v0.2.419): a failed touch must not leave the new note pending.
            with self.conn:
                # BEGIN IMMEDIATE (v0.2.731): the parent probe must see the
                # same commit point the INSERT lands on — at autocommit it
                # could see a notebook rowid later deleted and reused,
                # persisting the note under a notebook the probe never saw.
                self.conn.execute("BEGIN IMMEDIATE")
                self.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
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
        # Undo-log trash (v0.2.667): a user-typed note is unrecoverable
        # text — same archive-then-delete contract as notebook/source.
        with self.conn:
            # BEGIN IMMEDIATE (v0.2.683): the row SELECT must run under
            # the write lock — an update committed between read and
            # delete used to be archived stale and removed fresh.
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            if row is None:
                raise StoreError("NOTE_NOT_FOUND", f"note {note_id} not found")
            # Same parent-identity capture as delete_source (v0.2.689).
            nb_row = self.conn.execute(
                "SELECT created_at FROM notebooks WHERE id=?",
                (int(row["notebook_id"]),),
            ).fetchone()
            payload = json.dumps(
                {
                    "kind": "note",
                    "note": dict(row),
                    "nb_created_at": (
                        str(nb_row["created_at"]) if nb_row is not None else None
                    ),
                },
                ensure_ascii=False,
            )
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
        if isinstance(body, str) and len(body) > MAX_BODY_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"body too long (max {MAX_BODY_LEN} chars)",
            )
        _utf8(body, "body")
        _utf8(citation_report, "citation_report")
        try:
            # `with self.conn:` commits INSERT+DELETE atomically and rolls
            # both back on failure — a failed prune must not leave the
            # rejected row pending for a later write on this connection to
            # publish (latest_studio_outputs takes MAX(id), so it would
            # displace the good output). Insert-then-delete also scopes the
            # prune to `id < lastrowid`, preserving a newer concurrent row.
            with self.conn:
                # BEGIN IMMEDIATE (v0.2.731): the parent probe must see the
                # same commit point the INSERT lands on — at autocommit it
                # could see a notebook rowid later deleted and reused,
                # persisting the output under a notebook the probe never saw.
                self.conn.execute("BEGIN IMMEDIATE")
                self.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
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
        if isinstance(body, str) and len(body) > MAX_BODY_LEN:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID",
                f"body too long (max {MAX_BODY_LEN} chars)",
            )
        _utf8(body, "body")
        _utf8(citation_report, "citation_report")
        try:
            # Atomic INSERT+touch — same pending-leak guard as add_source
            # (v0.2.419): a failed touch must not leave the new message pending.
            with self.conn:
                # BEGIN IMMEDIATE (v0.2.731): the parent probe must see the
                # same commit point the INSERT lands on — at autocommit it
                # could see a notebook rowid later deleted and reused,
                # persisting the turn under a notebook the probe never saw.
                self.conn.execute("BEGIN IMMEDIATE")
                self.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
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
        with self.conn:
            # BEGIN IMMEDIATE (v0.2.731): the parent probe must see the same
            # commit point the DELETE lands on — at autocommit it could see a
            # notebook rowid later deleted and reused, wiping the messages of
            # a notebook the probe never saw.
            self.conn.execute("BEGIN IMMEDIATE")
            self.get_notebook(notebook_id)
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

    def list_notebooks_with_counts(
        self, limit: int | None = None, offset: int = 0
    ) -> list[NotebookWithCounts]:
        """Return all notebooks with source/chunk counts in a single query (avoids N+1).

        limit/offset page the result (GET /api/notebooks, v0.2.696) — the
        CLI keeps passing no limit and still gets the full list.
        """
        if limit is not None:
            rows = self.conn.execute(
                "SELECT n.id, n.name,"
                " COUNT(DISTINCT s.id) AS sources,"
                " COUNT(DISTINCT c.id) AS chunks"
                " FROM notebooks n"
                " LEFT JOIN sources s ON s.notebook_id = n.id"
                " LEFT JOIN chunks c ON c.source_id = s.id"
                " GROUP BY n.id"
                " ORDER BY n.updated_at DESC, n.id DESC"
                " LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
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
