"""Ingestion pipeline: extract -> chunk -> index -> (optional) embed.

Embedding is best-effort: any LLMError leaves the source indexed for BM25-only
retrieval (degradation is a first-class mode, see spec REQ-004/008).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .chunk import split_text
from .ingest import IngestError, extract_file, extract_url
from .llm import LLMError
from .qa import ChatBackend
from .store import Source, Store, StoreError

EMBED_BATCH = 16


@dataclass
class IndexResult:
    source: Source
    n_chunks: int
    n_embedded: int


class _NoEmbed:
    """Null backend used when no LLM is supplied."""

    embedding_model = ""

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.2) -> str:
        raise LLMError("SYSTEM_SERVICE_UNAVAILABLE", "no LLM configured")

    def embed_one(self, text: str) -> list[float]:
        raise LLMError("SYSTEM_EMBED_DISABLED", "no embedding backend")


def _embed_chunks(
    store: Store,
    llm: ChatBackend,
    chunk_ids: list[int],
    texts: list[str],
    *,
    force: bool = False,
) -> int:
    """Best-effort batch embedding. Returns the number of embedded chunks.

    force=True skips the model-mismatch guard. Pass it only from reindex_notebook,
    which is specifically designed to rebuild embeddings after a model change.
    """
    current_model = (llm.embedding_model or "").strip()
    if not current_model:
        return 0
    # Prefer the batch method; fall back to embed_one so any ChatBackend conforming
    # to the protocol (which only requires embed_one) gets full embedding support.
    embed = getattr(llm, "embed", None)
    if embed is None:
        embed_one = getattr(llm, "embed_one", None)
        if embed_one is None:
            return 0
        embed = lambda batch: [embed_one(t) for t in batch]  # noqa: E731
    stored_model = (store.get_setting("embed_model") or "").strip()
    if not force and stored_model and stored_model != current_model:
        # Vectors in the DB were produced by a different model; cosine scores
        # between old and new embeddings are meaningless. Skip embedding so the
        # DB stays coherent; the user should re-index to rebuild all embeddings.
        print(
            f"Warning: embedding model changed from {stored_model!r} to"
            f" {current_model!r}. Re-index sources to rebuild embeddings.",
            file=sys.stderr,
        )
        return 0
    done = 0
    try:
        for i in range(0, len(texts), EMBED_BATCH):
            batch_ids = chunk_ids[i : i + EMBED_BATCH]
            vectors = embed(texts[i : i + EMBED_BATCH])
            count = 0
            for cid, vec in zip(batch_ids, vectors):
                store.set_embedding(cid, vec, commit=False)
                count += 1
            store.conn.commit()  # one commit per batch, not per chunk
            done += count
    except LLMError:
        pass  # partial embedding is fine: BM25 covers the rest
    except Exception:
        # StoreError (concurrent chunk delete) or sqlite3.OperationalError
        # (busy_timeout on conn.commit()). Roll back the partial uncommitted
        # batch so its embeddings aren't silently committed later
        # (set_setting() below issues conn.commit(), which would flush them).
        try:
            store.conn.rollback()
        except Exception:
            pass
    if done:
        # Record the model used so a future model change triggers the mismatch
        # warning even when a previous run only partially succeeded.
        store.set_setting("embed_model", current_model)
    return done


def index_source(
    store: Store,
    notebook_id: int,
    target: str,
    llm: ChatBackend | None = None,
) -> IndexResult:
    """Ingest a local file path or public URL into a notebook."""
    if target.startswith(("http://", "https://")):
        extracted = extract_url(target)
    else:
        extracted = extract_file(target)
    source = store.add_source(
        notebook_id, extracted.kind, extracted.title, extracted.origin, extracted.sha256
    )
    texts = split_text(extracted.text)
    chunk_ids = store.add_chunks(source.id, texts)
    n_embedded = _embed_chunks(store, llm or _NoEmbed(), chunk_ids, texts)
    return IndexResult(source, len(chunk_ids), n_embedded)


def refresh_source(
    store: Store,
    source_id: int,
    llm: ChatBackend | None = None,
) -> IndexResult:
    """Re-fetch a URL source in-place, replacing chunks while keeping the source ID.

    The source ID is preserved so that citation references in stored messages
    remain resolvable after the content update. Only URL sources can be refreshed;
    file sources raise IngestError(INGEST_REFRESH_NOT_URL).
    """
    src = store.get_source(source_id)
    if not src.origin.startswith(("http://", "https://")):
        raise IngestError("INGEST_REFRESH_NOT_URL", "refresh is only supported for URL sources")
    extracted = extract_url(src.origin)
    texts = split_text(extracted.text)
    chunk_ids = store.replace_chunks_for_source(source_id, texts)
    store.update_source_sha256(source_id, extracted.sha256, extracted.title)
    n_embedded = _embed_chunks(store, llm or _NoEmbed(), chunk_ids, texts)
    updated_src = store.get_source(source_id)
    return IndexResult(updated_src, len(chunk_ids), n_embedded)


def reindex_notebook(store: Store, llm: ChatBackend, notebook_id: int) -> tuple[int, int]:
    """Re-embed all chunks for a notebook with the current embedding model.

    Returns (n_embedded, n_total). Useful after changing SHOIN_EMBED_MODEL.
    Raises StoreError(NOTEBOOK_NOT_FOUND) if the notebook does not exist.
    """
    store.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
    rows = store.id_text_chunks_for_notebook(notebook_id)
    if not rows:
        return 0, 0
    chunk_ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    n_embedded = _embed_chunks(store, llm, chunk_ids, texts, force=True)
    return n_embedded, len(rows)
