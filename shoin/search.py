"""Retrieval pipeline: BM25 + vector, RRF fusion, rerank, MMR.

Design (Plan.md / Hako v0.10.2 lineage):
- BM25 via SQLite FTS5 trigram (CJK-capable). LIKE fallback for short queries.
- Vector scores computed in-process over notebook chunks (local scale).
- Fusion: Reciprocal Rank Fusion (Cormack et al. SIGIR 2009, k=60) replaces
  the previous min-max-normalized convex combination. RRF is robust to
  scale incompatibility between BM25 (TF-IDF-like) and cosine ([0,1]) scores.
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
_NEG_RE = re.compile(r"(?<![A-Za-z0-9_])-([A-Za-z0-9_]+|[\u3041-\u30FF\u4E00-\u9FFF]+)")


def _esc_like(s: str) -> str:
    """Escape SQL LIKE special characters using '|' as the escape sentinel."""
    return s.replace("|", "||").replace("%", "|%").replace("_", "|_")


def neg_terms(query: str) -> list[str]:
    """Extract negated terms from a query (`-word`, `-日本語`).

    A leading minus not preceded by a word character introduces a negative
    filter.  The matched tokens are lower-cased for case-insensitive matching.
    Example: "Python -2.7 -legacy" → ["2.7", "legacy"] (note: "2.7" contains
    a dot that _NEG_RE does not capture across; the caller strips the raw hit).
    """
    return [m.group(1).lower() for m in _NEG_RE.finditer(query)]


def strip_neg_terms(query: str) -> str:
    """Return query with all `-term` tokens removed (for FTS5/LIKE processing)."""
    return _NEG_RE.sub("", query).strip()


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


def _is_cjk_word(ch: str) -> bool:
    """CJK content character (letter/syllable), excluding CJK Symbols and Punctuation.

    CJK Symbols and Punctuation (U+3000–U+303F) includes 。、　 etc. which
    act as word-boundary characters in query tokenisation. is_cjk() includes
    that block (for token-budget estimation) so we need a separate predicate.

    Exception: 々 (U+3005, ideographic iteration mark) appears inside words
    (人々, 様々) and must stay part of CJK word runs, not break them.
    """
    cp = ord(ch)
    if not is_cjk(ch):
        return False
    if 0x3000 <= cp <= 0x303F:
        return cp == 0x3005  # 々 is a word character; everything else is punctuation/space
    return True


def query_terms(query: str) -> list[str]:
    """Split a query into terms: ascii words plus contiguous CJK runs."""
    terms = _WORD_RE.findall(query)
    run = ""
    for ch in query:
        if _is_cjk_word(ch):
            run += ch
        elif run:
            terms.append(run)
            run = ""
    if run:
        terms.append(run)
    return [t for t in terms if t]


def _kana_alt(term: str) -> str:
    """Return the hiragana↔katakana alternate of a kana run, or the original term.

    Converts katakana (U+30A1–U+30F6) ↔ hiragana (U+3041–U+3096) character by
    character.  Pure kanji or mixed kanji/kana strings are converted on the kana
    characters only.  Returns the original string unchanged when no conversion
    occurred (e.g. pure kanji terms like 書院).

    Rationale: documents indexed with katakana (コード) are missed by hiragana
    queries (こーど) and vice-versa, because SQLite FTS5 trigram tokeniser is
    not kana-aware.  Adding alternate-script trigrams to the OR expression
    bridges the gap without a language detection dependency.
    """
    result = []
    changed = False
    for c in term:
        cp = ord(c)
        if 0x30A1 <= cp <= 0x30F6:  # full-width katakana → hiragana
            result.append(chr(cp - 0x60))
            changed = True
        elif 0x3041 <= cp <= 0x3096:  # hiragana → katakana
            result.append(chr(cp + 0x60))
            changed = True
        else:
            result.append(c)
    return "".join(result) if changed else term


def fts_query(query: str) -> str:
    """Build a recall-oriented FTS5 MATCH expression.

    ASCII words become quoted terms; CJK runs are decomposed into their
    trigrams.  For kana-containing terms, trigrams for the katakana↔hiragana
    alternate script are also added so that a katakana query finds hiragana-
    indexed documents and vice-versa.  Everything is OR-joined: BM25 ranks
    denser matches higher and precision is restored downstream by the lexical
    reranker + MMR.
    """
    groups: list[str] = []
    seen: set[str] = set()
    for term in query_terms(query):
        term = term.replace("\x00", "").replace('"', '""')
        if len(term) < 3:
            continue
        if is_cjk(term[0]):
            grams: list[str] = [term[i : i + 3] for i in range(len(term) - 2)]
            alt = _kana_alt(term)
            if alt != term:
                alt_grams = [alt[i : i + 3] for i in range(len(alt) - 2)]
                grams = grams + alt_grams
        else:
            grams = [term]
        for g in grams:
            if g not in seen:
                seen.add(g)
                groups.append(f'"{g}"')
    return " OR ".join(groups)


def _fallback_needles(query: str) -> list[str]:
    """Substring needles for the LIKE-scan fallback (CJK bigrams + words ≥ 2 chars).

    Single-character ASCII terms (A, I, …) are excluded: '%A%' matches almost
    every English chunk and floods results with irrelevant hits.  Single-char CJK
    terms (猫, 木, …) are kept because they can be meaningful content words and
    a LIKE like '%猫%' is still a selective filter.
    """
    needles: list[str] = []
    for term in query_terms(query):
        if is_cjk(term[0]):
            if len(term) >= 2:
                needles.extend(term[i : i + 2] for i in range(len(term) - 1))
            else:
                needles.append(term)  # 1-char CJK content word: keep
        elif len(term) >= 2:  # skip single ASCII chars like "A", "I"
            needles.append(term)
    return list(dict.fromkeys(needles))


# --- candidate generation -------------------------------------------------


def bm25_search(store: Store, notebook_id: int, query: str, k: int) -> list[Hit]:
    # Strip negated tokens before building FTS5/LIKE queries.
    negs = neg_terms(query)
    clean_query = strip_neg_terms(query) if negs else query

    expr = fts_query(clean_query)
    fts_hits: list[Hit] = []
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
            fts_hits.append(Hit(r["id"], r["source_id"], r["text"], 0.0, bm25=-float(r["rank"])))
        # Return early only when fts_query covered every query term (no terms with
        # len < 3 were silently skipped).  Short terms still need the LIKE path.
        if fts_hits and all(len(t) >= 3 for t in query_terms(clean_query)):
            if negs:
                fts_hits = _apply_neg_filter(fts_hits, negs)
            return fts_hits
    # LIKE-scan fallback: covers short queries and any terms with len < 3 that
    # fts_query skips.  Pushing the filter into SQL means every chunk in the notebook
    # is searched regardless of insertion order — no silent truncation for recently-
    # added sources.  SQLite LIKE is case-insensitive for ASCII and case-exact for CJK
    # (correct in both cases since CJK has no case).  Special LIKE characters in
    # the needle ('|', '%', '_') are escaped with '|' as the sentinel.
    needles = _fallback_needles(clean_query)
    if not needles:
        if negs:
            fts_hits = _apply_neg_filter(fts_hits, negs)
        return fts_hits  # return whatever FTS5 found (possibly empty)
    conditions = " OR ".join(f"c.text LIKE ? ESCAPE '|'" for _ in needles)
    like_params = [f"%{_esc_like(n)}%" for n in needles]
    # Cap at 2000 rows: LIKE has no BM25 scoring so we fetch a generous pool,
    # score in Python, and take the top k.  Without the cap a common CJK bigram
    # on a large notebook can pull tens of thousands of rows into memory.
    like_cap = max(k * 10, 2000)
    rows = store.conn.execute(
        f"SELECT c.id, c.source_id, c.text FROM chunks c"
        f" JOIN sources s ON s.id = c.source_id"
        f" WHERE s.notebook_id = ? AND ({conditions})"
        f" LIMIT ?",
        [notebook_id, *like_params, like_cap],
    ).fetchall()
    like_hits: list[Hit] = []
    for r in rows:
        text = str(r["text"])
        low = text.lower()
        score = float(sum(low.count(n.lower()) for n in needles))
        if score > 0:
            like_hits.append(Hit(r["id"], r["source_id"], text, 0.0, bm25=score))
    like_hits.sort(key=lambda h: h.bm25, reverse=True)
    if fts_hits:
        # FTS5 found results for long terms; add LIKE score to FTS5 hits so they
        # compare fairly against LIKE-only hits.  Raw FTS5 BM25 is near-zero for
        # small corpora (~2e-6), while LIKE scores are integers (1, 2, …).
        # Without this, min-max normalization in fuse() makes LIKE-only hits
        # dominate even when the FTS5 hit matches more query terms.
        fts_ids = {h.chunk_id for h in fts_hits}
        for h in fts_hits:
            low = h.text.lower()
            h.bm25 += float(sum(low.count(n.lower()) for n in needles))
        fts_hits.extend(h for h in like_hits if h.chunk_id not in fts_ids)
        # Re-sort the combined list after extending with LIKE-only hits.  The
        # previous sort ran before the extend, leaving LIKE-only hits appended
        # after FTS5 hits regardless of score — a LIKE-only chunk with bm25=50
        # would rank behind an FTS5 chunk with bm25=5.  rrf_fuse() uses rank
        # position (1/(k+rank+1)), so a wrong order here gives wrong RRF scores.
        fts_hits.sort(key=lambda h: h.bm25, reverse=True)
        if negs:
            fts_hits = _apply_neg_filter(fts_hits, negs)
        # The LIKE-only path caps at k; cap the merge path for consistency so
        # callers can rely on the k parameter being respected on all code paths.
        return fts_hits[:k]
    result = like_hits[:k]
    if negs:
        result = _apply_neg_filter(result, negs)
    return result


def _apply_neg_filter(hits: list[Hit], negs: list[str]) -> list[Hit]:
    """Remove hits whose text contains any negated term (case-insensitive)."""
    return [h for h in hits if not any(n in h.text.lower() for n in negs)]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    result = dot / (na * nb)
    return result if math.isfinite(result) else 0.0


def vector_search(store: Store, notebook_id: int, query_vec: list[float] | None, k: int) -> list[Hit]:
    if not query_vec:
        return []
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
    """Vector weight in [0.2, 0.8]. Lexical-looking queries push toward BM25.

    Heuristics (applied in order, each adjusts alpha from 0.5 baseline):
    - Short keyword query (≤ 3 terms, no question markers) → -0.15 (BM25 favoured)
    - Natural-language question (≥ 6 terms, ends with か/？/?) → +0.15 (semantic)
    - Digits or long identifiers → -0.15 (exact match)
    - Quoted phrase → -0.10 (exact match)
    """
    alpha = 0.5
    terms = query_terms(strip_neg_terms(query))
    q_tail = query.rstrip("。．!！?？ 　\t\n")
    q_ws = query.rstrip(" 　\t\n")
    is_question = (
        q_tail.endswith("か") or q_ws.endswith(("?", "？"))
    )
    if len(terms) <= 3 and not is_question:
        alpha -= 0.15  # short keyword lookup: exact match matters
    if len(terms) >= 6 or is_question:
        alpha += 0.15  # natural-language question: semantics matter
    # Use neg-stripped query for digit/quote checks so a neg-term like -v2 or
    # -"phrase" doesn't falsely bias alpha toward exact-match retrieval.
    clean_q = strip_neg_terms(query)
    if _DIGIT_RE.search(clean_q) or any(len(t) >= 12 and not is_cjk(t[0]) for t in terms):
        alpha -= 0.15  # identifiers / numbers: exact match matters
    if '"' in clean_q or "「" in clean_q:
        alpha -= 0.10  # quoted phrase: exact match matters
    return min(0.8, max(0.2, alpha))


def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        # All values are essentially equal. Near-zero means no signal → 0.0.
        # Equal and non-zero means undifferentiated relevance → 1.0 (tie).
        return [0.0 if lo < 1e-12 else 1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def fuse(bm25_hits: list[Hit], vec_hits: list[Hit], alpha: float) -> list[Hit]:
    """Convex combination over min-max normalised score lists.

    When only one signal is available, the degenerate case normalises that
    signal to [0..1] directly (same as the convex combination with the other
    weight at 0 and normalization applied before combining).  This keeps MMR
    scores symmetric: BM25-only and vec-only paths both produce scores in
    [0..1] so MMR's relevance/diversity trade-off is not biased by which signal
    happened to return results.
    """
    if not vec_hits:
        for h, n in zip(bm25_hits, _minmax([h.bm25 for h in bm25_hits])):
            h.score = n
            h.detail["bm25_norm"] = n
        return sorted(bm25_hits, key=lambda h: h.score, reverse=True)
    if not bm25_hits:
        # Symmetric case: only vector hits; normalize to [0..1] directly so
        # MMR gets the same score range as the BM25-only path above.
        for h, n in zip(vec_hits, _minmax([h.vec for h in vec_hits])):
            h.score = n
            h.detail["vec_norm"] = n
        return sorted(vec_hits, key=lambda h: h.score, reverse=True)
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


def rrf_fuse(bm25_hits: list[Hit], vec_hits: list[Hit], k: int = 60) -> list[Hit]:
    """Reciprocal Rank Fusion over BM25 and vector rank lists.

    RRF score for a chunk: sum of 1/(k + rank + 1) across all rank lists it
    appears in.  rank is 0-based within each sorted list (rank 0 = highest
    relevance).  k=60 is the empirically optimal constant from Cormack et al.
    SIGIR 2009, confirmed across TREC, WANDS, and hybrid-search benchmarks.

    Advantages over the previous min-max convex combination (fuse()):
    1. No score-scale normalization required — BM25 raw values and cosine
       similarity [0,1] are on completely incompatible scales; min-max is
       per-query, misbehaves on single-hit result sets (v0.1.45 bug), and
       compresses the BM25 dynamic range so any single vector hit dominates.
    2. No alpha tuning required — adaptive_alpha() heuristics disappear.
    3. A chunk that ranks well in BOTH lists scores higher than one that only
       ranks well in one, which is the correct semantic for hybrid retrieval.

    The legacy fuse() is kept for backward compatibility with direct callers.
    retrieve() uses rrf_fuse() as of v0.2.56.
    """
    scores: dict[int, float] = {}
    all_hits: dict[int, Hit] = {}

    for rank, h in enumerate(bm25_hits):
        scores[h.chunk_id] = scores.get(h.chunk_id, 0.0) + 1.0 / (k + rank + 1)
        all_hits[h.chunk_id] = h
        h.detail["rrf_bm25_rank"] = float(rank + 1)

    for rank, h in enumerate(vec_hits):
        scores[h.chunk_id] = scores.get(h.chunk_id, 0.0) + 1.0 / (k + rank + 1)
        if h.chunk_id not in all_hits:
            all_hits[h.chunk_id] = h
        else:
            all_hits[h.chunk_id].vec = h.vec
        all_hits[h.chunk_id].detail["rrf_vec_rank"] = float(rank + 1)

    for cid, rrf in scores.items():
        all_hits[cid].score = rrf

    return sorted(all_hits.values(), key=lambda h: h.score, reverse=True)


# --- rerank + diversity ---------------------------------------------------


def _char_bigrams(text: str) -> set[str]:
    t = text.lower()
    if len(t) < 2:
        return set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


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
    """Full pipeline: candidates -> RRF fusion -> lexical rerank -> MMR."""
    pool = max(k * 3, 12)
    negs = neg_terms(query)
    clean = strip_neg_terms(query) if negs else query
    bm25_hits = bm25_search(store, notebook_id, query, pool)
    vec_hits = vector_search(store, notebook_id, query_vec, pool) if query_vec else []
    fused = rrf_fuse(bm25_hits, vec_hits)
    # Normalize RRF scores to [0,1] before lexical rerank so the weight=0.3
    # blend ratio is calibrated correctly. Without this, RRF scores (~0.01-0.03)
    # are overwhelmed by lexical_overlap values in [0,1]: lex contributes ~10×
    # more than the RRF signal, making the reranker effectively ignore hybrid
    # retrieval. The old fuse() emitted [0,1] scores implicitly via _minmax;
    # rrf_fuse() emits raw rank-reciprocal values and needs explicit normalization.
    if fused:
        normed = _minmax([h.score for h in fused])
        for h, n in zip(fused, normed):
            h.score = n
    results = mmr(rerank(clean, fused), k)
    # Apply negative-term filter after fusion so vector hits are also excluded.
    if negs:
        results = _apply_neg_filter(results, negs)
    return results
