"""Ingestion pipeline: extract -> chunk -> index -> (optional) embed.

Embedding is best-effort: any LLMError leaves the source indexed for BM25-only
retrieval (degradation is a first-class mode, see spec REQ-004/008).
"""

from __future__ import annotations

from dataclasses import dataclass

from .chunk import split_text
from .ingest import extract_file, extract_url
from .llm import LLMError
from .qa import ChatBackend
from .store import Source, Store

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


def _embed_chunks(store: Store, llm: ChatBackend, chunk_ids: list[int], texts: list[str]) -> int:
    """Best-effort batch embedding. Returns the number of embedded chunks."""
    embed = getattr(llm, "embed", None)
    if not llm.embedding_model or embed is None:
        return 0
    done = 0
    try:
        for i in range(0, len(texts), EMBED_BATCH):
            batch_ids = chunk_ids[i : i + EMBED_BATCH]
            vectors = embed(texts[i : i + EMBED_BATCH])
            for cid, vec in zip(batch_ids, vectors):
                store.set_embedding(cid, vec)
            done += len(batch_ids)
    except LLMError:
        return done  # partial embedding is fine: BM25 covers the rest
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
