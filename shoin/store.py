"""SQLite persistence layer: migrations, notebooks, sources, chunks, FTS5.

Single-file database. Foreign keys + WAL. FTS5 uses the trigram tokenizer so
that CJK text is searchable without external tokenizers (SQLite >= 3.34).
"""

from __future__ import annotations

import array
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- schema migrations (append-only; never edit a shipped entry) ---

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE notebooks(
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE sources(
          id INTEGER PRIMARY KEY,
          notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
          kind TEXT NOT NULL,
          title TEXT NOT NULL,
          origin TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          added_at TEXT NOT NULL,
          UNIQUE(notebook_id, sha256)
        );
        CREATE TABLE chunks(
          id INTEGER PRIMARY KEY,
          source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
          seq INTEGER NOT NULL,
          text TEXT NOT NULL,
          embedding BLOB
        );
        CREATE INDEX idx_chunks_source ON chunks(source_id);
        CREATE TABLE notes(
          id INTEGER PRIMARY KEY,
          notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          body TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE studio_outputs(
          id INTEGER PRIMARY KEY,
          notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
          kind TEXT NOT NULL,
          body TEXT NOT NULL,
          citation_report TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );
        CREATE TABLE messages(
          id INTEGER PRIMARY KEY,
          notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
          role TEXT NOT NULL,
          body TEXT NOT NULL,
          citation_report TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
          text, content='chunks', content_rowid='id', tokenize='trigram'
        );
        CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
          INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
        END;
        CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
          INSERT INTO chunks_fts(chunks_fts, rowid, text)
          VALUES('delete', old.id, old.text);
        END;
        """,
    ),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        # ThreadingHTTPServer opens one Store per request: wait out writer
        # overlap instead of failing immediately with SQLITE_BUSY.
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- migrations ---

    def migrate(self) -> int:
        """Apply pending migrations. Returns the resulting schema version."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)"
        )
        row = self.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        current = int(row["v"] or 0)
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            with self.conn:
                self.conn.executescript(sql)
                self.conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
            current = version
        return current

    # --- notebooks ---

    def create_notebook(self, name: str) -> Notebook:
        name = name.strip()
        if not name:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "notebook name is empty")
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
        rows = self.conn.execute("SELECT * FROM notebooks ORDER BY id").fetchall()
        return [Notebook(r["id"], r["name"], r["created_at"], r["updated_at"]) for r in rows]

    def rename_notebook(self, notebook_id: int, name: str) -> None:
        name = name.strip()
        if not name:
            raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "notebook name is empty")
        self.get_notebook(notebook_id)
        self.conn.execute(
            "UPDATE notebooks SET name=?, updated_at=? WHERE id=?",
            (name, _now(), notebook_id),
        )
        self.conn.commit()

    def delete_notebook(self, notebook_id: int) -> None:
        self.get_notebook(notebook_id)
        self.conn.execute("DELETE FROM notebooks WHERE id=?", (notebook_id,))
        self.conn.commit()

    def touch_notebook(self, notebook_id: int) -> None:
        self.conn.execute("UPDATE notebooks SET updated_at=? WHERE id=?", (_now(), notebook_id))
        self.conn.commit()

    # --- sources / chunks ---

    def add_source(
        self, notebook_id: int, kind: str, title: str, origin: str, sha256: str
    ) -> Source:
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
        except sqlite3.IntegrityError:
            raise StoreError(
                "SOURCE_ALREADY_EXISTS",
                "identical source already in notebook (concurrent upload)",
            )
        self.conn.commit()
        self.touch_notebook(notebook_id)
        return Source(int(cur.lastrowid or 0), notebook_id, kind, title, origin, sha256, ts)

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
        self.get_source(source_id)  # raises SOURCE_NOT_FOUND if missing
        self.conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        self.conn.commit()

    def add_chunks(self, source_id: int, texts: list[str]) -> list[int]:
        ids: list[int] = []
        with self.conn:
            for seq, text in enumerate(texts):
                cur = self.conn.execute(
                    "INSERT INTO chunks(source_id, seq, text) VALUES (?,?,?)",
                    (source_id, seq, text),
                )
                ids.append(int(cur.lastrowid or 0))
        return ids

    def set_embedding(self, chunk_id: int, vec: list[float]) -> None:
        self.conn.execute("UPDATE chunks SET embedding=? WHERE id=?", (pack_vector(vec), chunk_id))
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

    @staticmethod
    def _row_to_chunk(r: sqlite3.Row) -> Chunk:
        blob: Any = r["embedding"]
        emb = unpack_vector(blob) if blob is not None else None
        return Chunk(r["id"], r["source_id"], r["seq"], r["text"], emb)

    # --- notes / studio outputs ---

    def add_note(self, notebook_id: int, title: str, body: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO notes(notebook_id, title, body, created_at) VALUES (?,?,?,?)",
            (notebook_id, title, body, _now()),
        )
        self.conn.commit()
        self.touch_notebook(notebook_id)
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
        self.conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
        self.conn.commit()
        self.touch_notebook(int(row["notebook_id"]))

    def add_studio_output(
        self, notebook_id: int, kind: str, body: str, citation_report: str
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO studio_outputs(notebook_id, kind, body, citation_report,"
            " created_at) VALUES (?,?,?,?,?)",
            (notebook_id, kind, body, citation_report, _now()),
        )
        self.conn.commit()
        self.touch_notebook(notebook_id)
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
        cur = self.conn.execute(
            "INSERT INTO messages(notebook_id, role, body, citation_report, created_at)"
            " VALUES (?,?,?,?,?)",
            (notebook_id, role, body, citation_report, _now()),
        )
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
        self.conn.commit()

    def counts(self, notebook_id: int) -> dict[str, int]:
        n_sources = self.conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE notebook_id=?", (notebook_id,)
        ).fetchone()["n"]
        n_chunks = self.conn.execute(
            "SELECT COUNT(*) AS n FROM chunks c JOIN sources s ON s.id=c.source_id"
            " WHERE s.notebook_id=?",
            (notebook_id,),
        ).fetchone()["n"]
        return {"sources": int(n_sources), "chunks": int(n_chunks)}
