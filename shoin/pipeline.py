"""Ingestion pipeline: extract -> chunk -> index -> (optional) embed.

Embedding is best-effort: any LLMError leaves the source indexed for BM25-only
retrieval (degradation is a first-class mode, see spec REQ-004/008).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .chunk import split_text
from .config import MAX_CHUNKS_PER_NOTEBOOK
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
    expected_dim: int | None = None
    try:
        for i in range(0, len(texts), EMBED_BATCH):
            batch_ids = chunk_ids[i : i + EMBED_BATCH]
            vectors = embed(texts[i : i + EMBED_BATCH])
            count = 0
            for cid, vec in zip(batch_ids, vectors):
                # Establish expected dimension from the first vector and validate all
                # subsequent vectors against it.  A mismatched dimension (e.g. from a
                # restarting endpoint momentarily returning truncated vectors) would
                # silently corrupt the embedding index; treat it as a non-fatal LLMError
                # so BM25-only retrieval remains intact.
                if expected_dim is None:
                    expected_dim = len(vec)
                elif len(vec) != expected_dim:
                    raise LLMError(
                        "SYSTEM_LLM_BAD_RESPONSE",
                        f"embedding dimension mismatch: expected {expected_dim}, got {len(vec)}",
                    )
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
    # Only record the model as "current" when every chunk in this call actually
    # got a fresh vector, OR this is the non-force (index_source) path where an
    # un-embedded chunk is simply NULL — safely excluded by vector_search()'s
    # `WHERE embedding IS NOT NULL`, not a corrupting wrong-model vector.
    # force=True (reindex_notebook) OVERWRITES existing vectors in place: a
    # partial failure there leaves some chunks with fresh current_model vectors
    # and others with their OLD, untouched, different-model vectors — both
    # non-NULL, both included in cosine comparisons. Recording embed_model as
    # fully consistent in that case (done > 0 but done < len(texts)) would make
    # _check_embed_model_ok() report no mismatch over a DB that is provably
    # still mixed, silently defeating the exact guard this exists to protect.
    # Leaving the setting untouched instead means it still reflects the OLD
    # model, so the mismatch guard correctly disables vector search until a
    # subsequent reindex fully succeeds.
    if done and (not force or done == len(texts)):
        store.set_setting("embed_model", current_model)
    return done


def index_source(
    store: Store,
    notebook_id: int,
    target: str,
    llm: ChatBackend | None = None,
    *,
    title: str | None = None,
) -> IndexResult:
    """Ingest a local file path or public URL into a notebook.

    title overrides the title inferred from the file/URL (useful when the caller
    knows the user-supplied name, e.g. an upload's original filename vs. a tmpfile
    path), so the source row is committed with the correct title in a single
    transaction — no second update_source_title commit needed.
    """
    if target.startswith(("http://", "https://")):
        extracted = extract_url(target)
    else:
        extracted = extract_file(target)
    # Guard before add_source so that zero-text documents don't leave an orphaned
    # source row (no chunks → permanently invisible to all retrieval queries).
    texts = split_text(extracted.text)
    if not texts:
        raise IngestError("INGEST_EMPTY", "no text content could be extracted from source")
    # spec.md STRIDE DoS control: cap total chunks per notebook. Checked before
    # add_source so an over-limit ingest never commits an orphaned source row.
    existing_chunks = store.counts(notebook_id)["chunks"]
    if existing_chunks + len(texts) > MAX_CHUNKS_PER_NOTEBOOK:
        raise IngestError(
            "INGEST_NOTEBOOK_FULL",
            f"notebook chunk limit exceeded: {existing_chunks} existing + {len(texts)} new"
            f" > {MAX_CHUNKS_PER_NOTEBOOK}",
        )
    source = store.add_source(
        notebook_id, extracted.kind, title or extracted.title, extracted.origin, extracted.sha256
    )
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
    # Guard against SHA-256 collision BEFORE replacing chunks.  Without this check,
    # replace_chunks_for_source commits new chunks and then update_source_sha256 raises
    # SOURCE_ALREADY_EXISTS, leaving the source with new chunks but the old sha256/title —
    # a permanently inconsistent state.
    dup = store.conn.execute(
        "SELECT id FROM sources WHERE notebook_id=? AND sha256=? AND id!=?",
        (src.notebook_id, extracted.sha256, source_id),
    ).fetchone()
    if dup:
        raise StoreError("SOURCE_ALREADY_EXISTS", "refreshed content matches an existing source")
    texts = split_text(extracted.text)
    if not texts:
        raise IngestError("INGEST_EMPTY", "no text content could be extracted from refreshed source")
    # spec.md STRIDE DoS control (same guard as index_source): cap total chunks
    # per notebook. Subtract this source's own current chunk count first — a
    # refresh REPLACES this source's chunks, it doesn't add a new source, so the
    # check must be against the notebook total *excluding* what's about to be
    # replaced, or a same-size (or shrinking) refresh at/near the cap would be
    # wrongly rejected.
    notebook_chunks = store.counts(src.notebook_id)["chunks"]
    this_source_chunks = len(store.text_chunks_for_source(source_id))
    if notebook_chunks - this_source_chunks + len(texts) > MAX_CHUNKS_PER_NOTEBOOK:
        raise IngestError(
            "INGEST_NOTEBOOK_FULL",
            f"notebook chunk limit exceeded: {notebook_chunks - this_source_chunks} existing"
            f" (excl. this source) + {len(texts)} new > {MAX_CHUNKS_PER_NOTEBOOK}",
        )
    # Pass sha256/title to replace_chunks_for_source so the metadata update happens
    # in the SAME transaction as the chunk replacement — eliminating the two-phase
    # commit gap that previously left new chunks committed with stale sha256/title
    # if the process was killed between the two separate commits.
    chunk_ids = store.replace_chunks_for_source(
        source_id, texts, sha256=extracted.sha256, title=extracted.title
    )
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
