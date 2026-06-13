"""Retrieval pipeline: BM25 + vector, Convex Combination fusion, rerank, MMR.

Design (Plan.md / Hako v0.10.2 lineage):
- BM25 via SQLite FTS5 trigram (CJK-capable). LIKE fallback for short queries.
- Vector scores computed in-process over notebook chunks (local scale).
- Fusion: convex combination with adaptive alpha (arXiv:2604.01733, 2604.16394).
- Zero-dependency lexical reranker + MMR diversity (arXiv:2305.14499 lineage).
- Without embeddings the pipeline degrades to pure BM25 (first-class mode).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .chunk import is_cjk
from .config import TOP_K
from .store import Store, unpack_vector

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_DIGIT_RE = re.compile(r"\d")
_FALLBACK_SCAN_LIMIT = 2000  # cap for the LIKE-scan fallback in bm25_search


@dataclass
class Hit:
    chunk_id: int
    source_id: int
    text: str
    score: float
    bm25: float = 0.0
    vec: float = 0.0
    detail: dict[str, float] = field(default_factory=dict)


# --- query helpers --------------------------------------------------------


def query_terms(query: str) -> list[str]:
    """Split a query into terms: ascii words plus contiguous CJK runs."""
    terms = _WORD_RE.findall(query)
    run = ""
    for ch in query:
        if is_cjk(ch):
            run += ch
        elif run:
            terms.append(run)
            run = ""
    if run:
        terms.append(run)
    return [t for t in terms if t]


def fts_query(query: str) -> str:
    """Build a recall-oriented FTS5 MATCH expression.

    ASCII words become quoted terms; CJK runs are decomposed into their
    trigrams. Everything is OR-joined: BM25 ranks denser matches higher and
    precision is restored downstream by the lexical reranker + MMR.
    """
    groups: list[str] = []
    seen: set[str] = set()
    for term in query_terms(query):
        term = term.replace("\x00", "").replace('"', '""')
        if len(term) < 3:
            continue
        grams = (
            [term[i : i + 3] for i in range(len(term) - 2)]
            if is_cjk(term[0]) and len(term) > 3
            else [term]
        )
        for g in grams:
            if g not in seen:
                seen.add(g)
                groups.append(f'"{g}"')
    return " OR ".join(groups)


def _fallback_needles(query: str) -> list[str]:
    """Substring needles for the LIKE-scan fallback (CJK bigrams + words)."""
    needles: list[str] = []
    for term in query_terms(query):
        if is_cjk(term[0]) and len(term) >= 2:
            needles.extend(term[i : i + 2] for i in range(len(term) - 1))
        else:
            needles.append(term)
    return list(dict.fromkeys(needles))


# --- candidate generation -------------------------------------------------


def bm25_search(store: Store, notebook_id: int, query: str, k: int) -> list[Hit]:
    expr = fts_query(query)
    hits: list[Hit] = []
    if expr:
        rows = store.conn.execute(
            "SELECT c.id, c.source_id, c.text, bm25(chunks_fts) AS rank"
            " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
            " JOIN sources s ON s.id = c.source_id"
            " WHERE chunks_fts MATCH ? AND s.notebook_id = ?"
            " ORDER BY rank LIMIT ?",
            (expr, notebook_id, k),
        ).fetchall()
        for r in rows:
            hits.append(Hit(r["id"], r["source_id"], r["text"], 0.0, bm25=-float(r["rank"])))
        if hits:
            return hits
    # fallback: substring scan (short/inflected queries the trigram index misses)
    needles = _fallback_needles(query)
    if not needles:
        return []
    # Limit the scan to avoid O(n) full-table reads on large notebooks.
    rows = store.conn.execute(
        "SELECT c.id, c.source_id, c.text FROM chunks c"
        " JOIN sources s ON s.id = c.source_id WHERE s.notebook_id = ? LIMIT ?",
        (notebook_id, _FALLBACK_SCAN_LIMIT),
    ).fetchall()
    for r in rows:
        text = str(r["text"])
        low = text.lower()
        score = float(sum(low.count(n.lower()) for n in needles))
        if score > 0:
            hits.append(Hit(r["id"], r["source_id"], text, 0.0, bm25=score))
    hits.sort(key=lambda h: h.bm25, reverse=True)
    return hits[:k]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def vector_search(store: Store, notebook_id: int, query_vec: list[float], k: int) -> list[Hit]:
    rows = store.conn.execute(
        "SELECT c.id, c.source_id, c.text, c.embedding FROM chunks c"
        " JOIN sources s ON s.id = c.source_id"
        " WHERE s.notebook_id = ? AND c.embedding IS NOT NULL",
        (notebook_id,),
    ).fetchall()
    hits = [
        Hit(
            r["id"],
            r["source_id"],
            r["text"],
            0.0,
            vec=cosine(query_vec, unpack_vector(r["embedding"])),
        )
        for r in rows
    ]
    hits.sort(key=lambda h: h.vec, reverse=True)
    return hits[:k]


# --- fusion ---------------------------------------------------------------


def adaptive_alpha(query: str) -> float:
    """Vector weight in [0.2, 0.8]. Lexical-looking queries push toward BM25."""
    alpha = 0.5
    terms = query_terms(query)
    # Strip trailing punctuation (。or ？ after か is common in LLM-generated questions)
    q_tail = query.rstrip("。．!！?？ 　\t\n")
    if len(terms) >= 6 or q_tail.endswith(("?", "？", "か")):
        alpha += 0.15  # natural-language question: semantics matter
    if _DIGIT_RE.search(query) or any(len(t) >= 12 and not is_cjk(t[0]) for t in terms):
        alpha -= 0.15  # identifiers / numbers: exact match matters
    if '"' in query or "「" in query:
        alpha -= 0.10  # quoted phrase: exact match matters
    return min(0.8, max(0.2, alpha))


def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def fuse(bm25_hits: list[Hit], vec_hits: list[Hit], alpha: float) -> list[Hit]:
    """Convex combination over min-max normalised score lists."""
    if not vec_hits:
        for h, n in zip(bm25_hits, _minmax([h.bm25 for h in bm25_hits])):
            h.score = n
        return sorted(bm25_hits, key=lambda h: h.score, reverse=True)
    merged: dict[int, Hit] = {}
    for h, n in zip(bm25_hits, _minmax([h.bm25 for h in bm25_hits])):
        merged[h.chunk_id] = h
        h.detail["bm25_norm"] = n
    for h, n in zip(vec_hits, _minmax([h.vec for h in vec_hits])):
        cur = merged.setdefault(h.chunk_id, h)
        cur.vec = h.vec
        cur.detail["vec_norm"] = n
    for h in merged.values():
        h.score = alpha * h.detail.get("vec_norm", 0.0) + (1 - alpha) * h.detail.get(
            "bm25_norm", 0.0
        )
    return sorted(merged.values(), key=lambda h: h.score, reverse=True)


# --- rerank + diversity ---------------------------------------------------


def _char_bigrams(text: str) -> set[str]:
    t = text.lower()
    return {t[i : i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t}


def lexical_overlap(query: str, text: str) -> float:
    """Saturating term-frequency overlap between query terms and text."""
    terms = query_terms(query)
    if not terms:
        return 0.0
    low = text.lower()
    score = 0.0
    for t in terms:
        tf = low.count(t.lower())
        score += tf / (tf + 1.0)  # saturate repeated occurrences
    return score / len(terms)


def rerank(query: str, hits: list[Hit], weight: float = 0.3) -> list[Hit]:
    """Blend retrieval score with a zero-dependency lexical signal."""
    for h in hits:
        lex = lexical_overlap(query, h.text)
        h.detail["lex"] = lex
        h.score = (1 - weight) * h.score + weight * lex
    return sorted(hits, key=lambda h: h.score, reverse=True)


def _sim(a: Hit, b: Hit) -> float:
    ga, gb = _char_bigrams(a.text[:600]), _char_bigrams(b.text[:600])
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def mmr(hits: list[Hit], k: int, lam: float = 0.7) -> list[Hit]:
    """Maximal Marginal Relevance: relevance vs. redundancy trade-off."""
    pool = list(hits)
    selected: list[Hit] = []
    while pool and len(selected) < k:
        best_idx, best_val = 0, -math.inf
        for i, cand in enumerate(pool):
            redundancy = max((_sim(cand, s) for s in selected), default=0.0)
            val = lam * cand.score - (1 - lam) * redundancy
            if val > best_val:
                best_idx, best_val = i, val
        selected.append(pool.pop(best_idx))
    return selected


# --- top-level ------------------------------------------------------------


def retrieve(
    store: Store,
    notebook_id: int,
    query: str,
    query_vec: list[float] | None = None,
    k: int = TOP_K,
) -> list[Hit]:
    """Full pipeline: candidates -> CC fusion -> lexical rerank -> MMR."""
    pool = max(k * 3, 12)
    bm25_hits = bm25_search(store, notebook_id, query, pool)
    vec_hits = vector_search(store, notebook_id, query_vec, pool) if query_vec else []
    fused = fuse(bm25_hits, vec_hits, adaptive_alpha(query))
    return mmr(rerank(query, fused), k)
