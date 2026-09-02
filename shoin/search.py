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

import array
import heapq
import math
import operator
import os
import re
import sys
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from .chunk import _CJK_RANGES, is_cjk
from .config import TOP_K
from .store import Store

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
# Built from chunk._CJK_RANGES (the same table is_cjk()/query_terms()/fts_query()
# already use) instead of a second hand-picked range literal \u2014 the original
# hardcoded [\u3041-\u30FF\u4E00-\u9FFF] only covered hiragana/katakana/CJK
# ideographs, silently failing (and inverting into a positive match) for
# Hangul, Thai, Lao, Myanmar, Khmer, CJK ext A-H, and fullwidth digits/letters.
_CJK_NEG_CLASS = "".join(f"\\U{lo:08X}-\\U{hi:08X}" for lo, hi in _CJK_RANGES)
# Lookbehind class for "this hyphen is attached to a preceding word, so it's an
# ordinary hyphen, not negation syntax" (e.g. "state-of-the-art"). This must
# exclude CJK Symbols and Punctuation (U+3000-U+303F, \u3002\u3001\u3000 etc.) the same way
# _is_cjk_word() does below \u2014 those are word BOUNDARIES, not word characters,
# so a hyphen right after one (e.g. a full-width space) should still be able to
# introduce negation. v0.2.118 extended the POSITIVE match side (_CJK_NEG_CLASS,
# what CAN be negated) to cover every _CJK_RANGES script, but never extended
# this lookbehind (what precedes a hyphen that DISQUALIFIES it from being
# negation) to match \u2014 so a hyphen tightly glued to a preceding CJK word
# character (e.g. "\u30A2\u30EB\u30B4\u30EA\u30BA\u30E0\u306E-\u6700\u9069\u5316", hiragana \u306E directly before the
# hyphen) was misparsed as `-\u6700\u9069\u5316` negation syntax instead of an ordinary
# in-sentence hyphen, silently discarding real query content.
_CJK_WORD_NEG_CLASS = "".join(
    f"\\U{lo:08X}-\\U{hi:08X}" for lo, hi in _CJK_RANGES if (lo, hi) != (0x3000, 0x303F)
)
_NEG_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?<![{_CJK_WORD_NEG_CLASS}])-([A-Za-z0-9_]+|[{_CJK_NEG_CLASS}]+)"
)


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
    # The chunk's indexed context breadcrumb ("title > heading > …", v0.2.123).
    # Retrieval-only until now; carried on the Hit so build_context() can surface
    # the section a citation came from in the UI. Defaults to "" so every existing
    # positional Hit(...) construction in tests stays valid.
    context: str = ""


# --- query helpers --------------------------------------------------------


def _is_cjk_word(ch: str) -> bool:
    """CJK content character (letter/syllable), excluding CJK Symbols and Punctuation.

    CJK Symbols and Punctuation (U+3000–U+303F) includes 。、　 etc. which
    act as word-boundary characters in query tokenisation. is_cjk() includes
    that block (for token-budget estimation) so we need a separate predicate.

    Exception: 々 (U+3005, ideographic iteration mark) appears inside words
    (人々, 様々) and must stay part of CJK word runs, not break them.

    The halfwidth block (U+FF61–FF65) gets the mirror-image treatment, so a
    halfwidth string tokenises exactly like its fullwidth equivalent: ｡｢｣､ are
    boundaries like 。「」、, while ･ (U+FF65) stays a word character because its
    NFKC target ・ (U+30FB) already is one.  Without this, ｿﾌﾄｳｪｱ･ｱｰｷﾃｸﾁｬ and
    ソフトウェア・アーキテクチャ would split into different numbers of terms.
    """
    cp = ord(ch)
    if not is_cjk(ch):
        return False
    if 0x3000 <= cp <= 0x303F:
        return cp == 0x3005  # 々 is a word character; everything else is punctuation/space
    # ｡｢｣､ (U+FF61–FF64) are the halfwidth counterparts of 。「」、 and break runs
    # the same way; ･ (U+FF65) is excluded from this test because its NFKC target
    # ・ (U+30FB) is already a word character.
    return not 0xFF61 <= cp <= 0xFF64


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


def _build_fw_to_hw() -> dict[str, str]:
    """Fullwidth-kana → halfwidth map, derived from NFKC rather than hand-written.

    NFKC folds halfwidth to fullwidth (ﾃﾞ → デ), so inverting its own output is
    the only way to get the reverse direction without maintaining a second table
    that can silently drift out of sync — the same "derive it from the shared
    source" discipline v0.2.118 applied to _NEG_RE's character classes.

    Voiced morae need the two-codepoint sequences (ﾃ + ﾞ) as well as the singles,
    because NFKC composes them into one fullwidth character.
    """
    table: dict[str, str] = {}
    for cp in range(0xFF61, 0xFFA0):
        table.setdefault(unicodedata.normalize("NFKC", chr(cp)), chr(cp))
    for base in range(0xFF66, 0xFF9E):
        for mark in ("ﾞ", "ﾟ"):
            composed = unicodedata.normalize("NFKC", chr(base) + mark)
            if len(composed) == 1:
                table.setdefault(composed, chr(base) + mark)
    return table


_FW_TO_HW = _build_fw_to_hw()


def _to_katakana(s: str) -> str:
    """Hiragana → katakana (one-directional, unlike _kana_alt's swap)."""
    return "".join(chr(ord(c) + 0x60) if 0x3041 <= ord(c) <= 0x3096 else c for c in s)


def _to_hiragana(s: str) -> str:
    """Katakana → hiragana (one-directional, unlike _kana_alt's swap)."""
    return "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in s)


def _to_halfwidth(s: str) -> str:
    """Fullwidth katakana → halfwidth, via the inverted-NFKC table."""
    return "".join(_FW_TO_HW.get(c, c) for c in s)


def _to_fullwidth_ascii(s: str) -> str:
    """ASCII → fullwidth forms (Ａ-Ｚ ０-９ …), the U+FEE0 offset block."""
    return "".join(chr(ord(c) + 0xFEE0) if 0x21 <= ord(c) <= 0x7E else c for c in s)


def term_variants(term: str) -> list[str]:
    """Width/script spellings of *term* that should all retrieve each other.

    Japanese text encodes the same word three ways — fullwidth kana (データ),
    halfwidth JIS X 0201 kana (ﾃﾞｰﾀ, ubiquitous in cp932 exports, which
    ingest._decode() actively prefers), and fullwidth ASCII (ＧＰＵ, ２０２４,
    ordinary in JA prose).  SQLite's FTS5 trigram tokeniser folds case (including
    fullwidth Latin) but never width, and SQL LIKE folds neither, so nothing
    bridges these spellings unless the query does it explicitly.  Standard
    practice elsewhere is an NFKC pass before tokenisation (Elasticsearch's
    icu_normalizer, Lucene's cjk_width filter); Shoin cannot normalise at index
    time because chunk text must stay byte-identical to the source (v0.2.123),
    so the bridge is built query-side instead, extending the katakana↔hiragana
    alternates v0.2.42 already generated.

    Conversions are deliberately one-directional and composed off the NFKC form
    rather than iterated to a fixed point: a naive closure over _kana_alt's swap
    emits mixed-width nonsense (でーた → でｰた) because the swap flips fullwidth
    kana while leaving an already-halfwidth ｰ alone.  Only variants that actually
    differ are emitted, so a pure-kanji or plain-ASCII-lowercase term whose forms
    all coincide yields just itself.
    """
    norm = unicodedata.normalize("NFKC", term)
    katakana = _to_katakana(norm)
    candidates = [term, norm, _to_hiragana(norm), katakana, _to_halfwidth(katakana)]
    if norm.isascii():
        candidates.append(_to_fullwidth_ascii(norm))
    out: list[str] = []
    for v in candidates:
        if v and v not in out:
            out.append(v)
    return out


def _fts_escape(term: str) -> str:
    """Escape a term for inclusion in a double-quoted FTS5 MATCH string.

    Must run per variant at emission time, never once before term_variants():
    NFKC *creates* a double quote out of ＂ (U+FF02), so a term escaped only in
    its raw form can still carry an unescaped quote into the MATCH expression
    through one of its variants.
    """
    return term.replace("\x00", "").replace('"', '""')


def fts_query(query: str) -> str:
    """Build a recall-oriented FTS5 MATCH expression.

    ASCII words become quoted terms; CJK runs are decomposed into their
    trigrams.  Every width/script variant of a term (term_variants) contributes
    its own grams, so a katakana query finds hiragana-indexed documents, a
    fullwidth query finds halfwidth-indexed ones, and vice-versa in both cases.
    Everything is OR-joined: BM25 ranks denser matches higher and precision is
    restored downstream by the lexical reranker + MMR.
    """
    groups: list[str] = []
    seen: set[str] = set()
    for raw_term in query_terms(query):
        # Trigram-vs-whole-term is a property of the TERM, not of each spelling:
        # a fullwidth ASCII variant is is_cjk()-true (fullwidth Latin lives in
        # _CJK_RANGES), so branching per variant would shred ｗｅａｔｈｅｒ into five
        # trigrams while its own raw form stays one quoted word.  FTS5 matches a
        # quoted string of 3+ characters through the trigram index either way.
        cjk_term = is_cjk(raw_term[0])
        for variant in term_variants(raw_term):
            term = _fts_escape(variant)
            # Shorter-than-trigram variants contribute nothing here (the gram
            # comprehension below is simply empty); bm25_search's coverage check
            # knows this and keeps the LIKE fallback alive for them.
            if len(term) < 3:
                continue
            if cjk_term:
                grams: list[str] = [term[i : i + 3] for i in range(len(term) - 2)]
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

    Needles are generated per width/script variant (term_variants).  SQL LIKE
    compares codepoints and folds neither case beyond ASCII nor width, so the
    only way this branch can bridge spellings is to materialise each one as its
    own needle.  This is also where kana bridging reaches the LIKE path at all:
    v0.2.42 added katakana↔hiragana alternates to fts_query only, and closed
    with "the LIKE-scan fallback path for short terms is unchanged" — so every
    two-character kana query (こー vs コー) stayed script-brittle, since terms
    that short never reach FTS5's trigram tokeniser in the first place.
    """
    needles: list[str] = []
    for raw_term in query_terms(query):
        # Drop a single-character ASCII term before expanding it: is_cjk('Ａ') is
        # true (fullwidth Latin lives in _CJK_RANGES), so its fullwidth variant
        # would otherwise fall into the CJK branch's keep-1-char path and
        # reintroduce precisely the flooding needle the raw term was excluded to
        # avoid.  Eligibility is a property of the term, not of each spelling.
        if not is_cjk(raw_term[0]) and len(raw_term) < 2:
            continue
        for term in term_variants(raw_term):
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
            "SELECT c.id, c.source_id, c.text, c.context, bm25(chunks_fts) AS rank"
            " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
            " JOIN sources s ON s.id = c.source_id"
            " WHERE chunks_fts MATCH ? AND s.notebook_id = ?"
            " ORDER BY rank LIMIT ?",
            (expr, notebook_id, k),
        ).fetchall()
        for r in rows:
            fts_hits.append(
                Hit(
                    r["id"], r["source_id"], r["text"], 0.0,
                    bm25=-float(r["rank"]), context=str(r["context"] or ""),
                )
            )
        # Return early only when fts_query covered every query term (no terms with
        # len < 3 were silently skipped).  Short terms still need the LIKE path.
        #
        # Coverage is measured over every VARIANT, not just the raw term, because
        # NFKC can shorten a term below the trigram floor: ｶﾞｽ is 3 characters but
        # normalises to ガス, which is 2, and fts_query's gram comprehension then
        # yields nothing for it — silently, with no error.  Checking only the raw
        # length would read "fully covered" and skip the LIKE scan, leaving every
        # fullwidth-spelled document unreachable for exactly the halfwidth queries
        # this variant machinery exists to serve.
        if fts_hits and all(
            len(v) >= 3 for t in query_terms(clean_query) for v in term_variants(t)
        ):
            if negs:
                fts_hits = _apply_neg_filter(fts_hits, negs)
            return fts_hits
    # LIKE-scan fallback: covers short queries and any terms with len < 3 that
    # fts_query skips.  Pushing the filter into SQL means every chunk in the notebook
    # is searched regardless of insertion order — no silent truncation for recently-
    # added sources.  SQLite LIKE is case-insensitive for ASCII and case-exact for CJK
    # (correct in both cases since CJK has no case).  Special LIKE characters in
    # the needle ('|', '%', '_') are escaped with '|' as the sentinel.
    #
    # The scan covers c.context as well as c.text.  A bare-term FTS5 MATCH searches
    # every column of chunks_fts, so the FTS path has matched the contextual
    # breadcrumb (source title > heading path, v0.2.123) since that column existed —
    # but this path only ever looked at c.text, so the same term found a
    # heading-only match through one branch and nothing through the other, purely
    # by term length.  That is not a corner case for a JA-first tool: the trigram
    # tokeniser needs >= 3 characters, so EVERY two-character Japanese compound
    # (総説, 経済, 免疫 …) — the most common query shape in Japanese — skips FTS5
    # entirely and lands here, which meant contextual retrieval's headline recall
    # win was silently absent for exactly those queries.
    needles = _fallback_needles(clean_query)
    if not needles:
        if negs:
            fts_hits = _apply_neg_filter(fts_hits, negs)
        return fts_hits  # return whatever FTS5 found (possibly empty)
    conditions = " OR ".join(
        "(c.text LIKE ? ESCAPE '|' OR c.context LIKE ? ESCAPE '|')" for _ in needles
    )
    like_params = [p for n in needles for p in (f"%{_esc_like(n)}%",) * 2]
    # Cap at 2000 rows: LIKE has no BM25 scoring so we fetch a generous pool,
    # score in Python, and take the top k.  Without the cap a common CJK bigram
    # on a large notebook can pull tens of thousands of rows into memory.
    like_cap = max(k * 10, 2000)
    rows = store.conn.execute(
        f"SELECT c.id, c.source_id, c.text, c.context FROM chunks c"
        f" JOIN sources s ON s.id = c.source_id"
        f" WHERE s.notebook_id = ? AND ({conditions})"
        f" LIMIT ?",
        [notebook_id, *like_params, like_cap],
    ).fetchall()
    like_hits: list[Hit] = []
    for r in rows:
        text = str(r["text"])
        score = _needle_score(text, str(r["context"] or ""), needles)
        if score > 0:
            like_hits.append(
                Hit(r["id"], r["source_id"], text, 0.0, bm25=score, context=str(r["context"] or ""))
            )
    like_hits.sort(key=lambda h: h.bm25, reverse=True)
    if fts_hits:
        # FTS5 found results for long terms; add LIKE score to FTS5 hits so they
        # compare fairly against LIKE-only hits.  Raw FTS5 BM25 is near-zero for
        # small corpora (~2e-6), while LIKE scores are integers (1, 2, …).
        # Without this, the min-max rescale before rerank makes LIKE-only hits
        # dominate even when the FTS5 hit matches more query terms.
        fts_ids = {h.chunk_id for h in fts_hits}
        for h in fts_hits:
            h.bm25 += _needle_score(h.text, h.context, needles)
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


def _needle_score(text: str, context: str, needles: list[str]) -> float:
    """Count LIKE-fallback needle occurrences across a chunk's text and context.

    Both fields count at weight 1.0, which is deliberately the same weighting the
    FTS path gets: SQLite's bm25(chunks_fts) defaults every column to 1.0, and the
    point of scoring context here is to remove the divergence between the two
    branches, not to introduce a new tuning knob on one of them.  A section's
    breadcrumb is identical across all of that section's chunks, so a context match
    lifts the whole section uniformly and never reorders chunks within it.
    """
    low_text = text.lower()
    low_ctx = context.lower()
    return float(
        sum(low_text.count(n.lower()) + low_ctx.count(n.lower()) for n in needles)
    )


def _apply_neg_filter(hits: list[Hit], negs: list[str]) -> list[Hit]:
    """Remove hits whose text or context contains any negated term (case-insensitive).

    Context is checked for the same reason it is now searched and scored: a chunk
    can be retrieved *because of* its breadcrumb, so `-term` must be able to
    exclude it on that same basis.  Otherwise `legacy` surfaces a source whose only
    mention of it is in its title while `-legacy` cannot suppress it — the filter
    would be blind to exactly the signal that produced the hit.

    Both sides are NFKC-folded for that same reason, one dimension over: now that
    a halfwidth-spelled chunk is retrievable by a fullwidth query, `-データ` has to
    be able to suppress a ﾃﾞｰﾀ-only document.  This does widen `-term` slightly —
    `-GPU` now also excludes a ＧＰＵ-only chunk — which is the intended reading of
    an exclusion, and the symmetric counterpart of the widened positive match.

    Each hit's text and context are NFKC-folded once, not once per negated term:
    the fold is the expensive part (a full chunk body) and does not depend on
    which needle it is tested against.
    """
    folded_negs = [unicodedata.normalize("NFKC", n).lower() for n in negs]
    out: list[Hit] = []
    for h in hits:
        folded_text = unicodedata.normalize("NFKC", h.text).lower()
        folded_ctx = unicodedata.normalize("NFKC", h.context).lower()
        if not any(n in folded_text or n in folded_ctx for n in folded_negs):
            out.append(h)
    return out


_MUL = operator.mul  # bound once: map(operator.mul, ...) beats a generator expression


def _vec_norm(v: Sequence[float]) -> float:
    return math.sqrt(sum(map(_MUL, v, v)))


def _cosine_prepared(query: Sequence[float], query_norm: float, vec: Sequence[float]) -> float:
    """cosine() with the QUERY's norm already computed.

    vector_search compares one query against every chunk in the notebook, so the
    query's norm is loop-invariant — recomputing it per chunk spent len(query)
    multiply-adds per row for a value that never changes (768 * 50,000 = 38.4M
    wasted operations at the documented MAX_CHUNKS_PER_NOTEBOOK). Same
    hoist-the-invariant fix as v0.2.145 did for the NFKC folds in rerank().

    Results are bit-identical to cosine(): the same dot / (|q| * |v|), only with
    |q| supplied. Accepts any float Sequence so callers can pass an array('f')
    straight from the BLOB instead of materializing a 768-element list per chunk.
    """
    if not query_norm or len(query) != len(vec):
        return 0.0
    dot = sum(map(_MUL, query, vec))
    vn = math.sqrt(sum(map(_MUL, vec, vec)))
    if vn == 0.0:
        return 0.0
    result = dot / (query_norm * vn)
    return result if math.isfinite(result) else 0.0


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return _cosine_prepared(a, _vec_norm(a), b)


def vector_search(store: Store, notebook_id: int, query_vec: list[float] | None, k: int) -> list[Hit]:
    if not query_vec:
        return []
    # Streamed, not fetchall(): only the top k survive, so there is no reason to
    # hold every row and every Hit in memory at once. At the documented
    # MAX_CHUNKS_PER_NOTEBOOK that materialization cost ~184 MB of allocation for
    # a single query (measured, 768-dim vectors) on machines the README sizes at
    # 4-8 GB total while also hosting the LLM. heapq.nlargest keeps k items and is
    # defined as sorted(..., key=..., reverse=True)[:k], so ties still resolve in
    # row order and the returned list is identical to the previous sort-then-slice.
    cur = store.conn.execute(
        "SELECT c.id, c.source_id, c.text, c.context, c.embedding FROM chunks c"
        " JOIN sources s ON s.id = c.source_id"
        " WHERE s.notebook_id = ? AND c.embedding IS NOT NULL",
        (notebook_id,),
    )
    # Hoisted out of the per-chunk loop: the query's norm is the same for every
    # row, and unpacking straight into an array('f') avoids building a 768-float
    # Python list per chunk. Scores are unchanged (see _cosine_prepared).
    query_norm = _vec_norm(query_vec)

    def _scored() -> Iterator[Hit]:
        for r in cur:
            vec = array.array("f")
            vec.frombytes(r["embedding"])
            yield Hit(
                r["id"],
                r["source_id"],
                r["text"],
                0.0,
                vec=_cosine_prepared(query_vec, query_norm, vec),
                context=str(r["context"] or ""),
            )

    return heapq.nlargest(k, _scored(), key=lambda h: h.vec)


# --- fusion ---------------------------------------------------------------


def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        # All values are essentially equal. Near-zero means no signal → 0.0.
        # Equal and non-zero means undifferentiated relevance → 1.0 (tie).
        return [0.0 if lo < 1e-12 else 1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def rrf_fuse(bm25_hits: list[Hit], vec_hits: list[Hit], k: int = 60) -> list[Hit]:
    """Reciprocal Rank Fusion over BM25 and vector rank lists.

    RRF score for a chunk: sum of 1/(k + rank + 1) across all rank lists it
    appears in.  rank is 0-based within each sorted list (rank 0 = highest
    relevance).  k=60 is the empirically optimal constant from Cormack et al.
    SIGIR 2009, confirmed across TREC, WANDS, and hybrid-search benchmarks.

    Advantages over the previous min-max convex combination (removed v0.2.150):
    1. No score-scale normalization required — BM25 raw values and cosine
       similarity [0,1] are on completely incompatible scales; min-max is
       per-query, misbehaves on single-hit result sets (v0.1.45 bug), and
       compresses the BM25 dynamic range so any single vector hit dominates.
    2. No alpha tuning required — the old adaptive-alpha heuristics disappear.
    3. A chunk that ranks well in BOTH lists scores higher than one that only
       ranks well in one, which is the correct semantic for hybrid retrieval.

    retrieve() has used rrf_fuse() exclusively since v0.2.56.
    """
    return rrf_fuse_lists(
        [bm25_hits, vec_hits], k, detail_names=["rrf_bm25_rank", "rrf_vec_rank"]
    )


def rrf_fuse_lists(
    lists: list[list[Hit]], k: int = 60, detail_names: list[str] | None = None
) -> list[Hit]:
    """Reciprocal Rank Fusion over an arbitrary number of ranked hit lists.

    Generalization of rrf_fuse() for multi-query retrieval (RAG-Fusion /
    DMQR-RAG lineage): each query's BM25 and vector result lists all contribute
    1/(k + rank + 1) per appearance, so a chunk found by several query
    rewrites naturally outranks one found by a single phrasing. The same RRF
    constant and rank semantics as the two-list case apply.

    detail_names labels each list's rank in Hit.detail (defaults to
    "rrf_rank_{i}"); rrf_fuse() passes its legacy two-list names so existing
    detail consumers are unaffected.
    """
    names = detail_names or [f"rrf_rank_{i}" for i in range(len(lists))]
    scores: dict[int, float] = {}
    all_hits: dict[int, Hit] = {}

    for li, hits in enumerate(lists):
        for rank, h in enumerate(hits):
            scores[h.chunk_id] = scores.get(h.chunk_id, 0.0) + 1.0 / (k + rank + 1)
            cur = all_hits.setdefault(h.chunk_id, h)
            if cur is not h:
                # Merge signal fields onto the canonical Hit: a chunk seen by a
                # vector list carries vec, by a BM25 list carries bm25 — keep both.
                if h.vec:
                    cur.vec = h.vec
                if h.bm25 and not cur.bm25:
                    cur.bm25 = h.bm25
            cur.detail[names[li]] = float(rank + 1)

    for cid, rrf in scores.items():
        all_hits[cid].score = rrf

    return sorted(all_hits.values(), key=lambda h: h.score, reverse=True)


# --- rerank + diversity ---------------------------------------------------


def _char_bigrams(text: str) -> set[str]:
    t = text.lower()
    if len(t) < 2:
        return set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _norm_query_terms(query: str) -> list[str]:
    """Query terms, NFKC-folded and lower-cased — the form the scorers compare in."""
    return [unicodedata.normalize("NFKC", t).lower() for t in query_terms(query)]


def _overlap_from_norm(norm_terms: list[str], text: str) -> float:
    """lexical_overlap's core, given already-normalised terms (see rerank())."""
    if not norm_terms:
        return 0.0
    low = unicodedata.normalize("NFKC", text).lower()
    score = 0.0
    for t in norm_terms:
        tf = low.count(t)
        score += tf / (tf + 1.0)  # saturate repeated occurrences
    return score / len(norm_terms)


def lexical_overlap(query: str, text: str) -> float:
    """Saturating term-frequency overlap between query terms and text.

    Both sides are NFKC-folded so width spellings count as the same term, for the
    same reason bm25_search matches them and _apply_neg_filter excludes on them:
    a chunk retrieved through one spelling must not be scored as though it
    contained none of the query.  Left width-blind, rerank() handed every
    halfwidth-matched hit lex=0.0 and then pushed it back down — the v0.2.143
    failure shape, one spelling dimension over.
    """
    return _overlap_from_norm(_norm_query_terms(query), text)


def rerank(query: str, hits: list[Hit], weight: float = 0.3) -> list[Hit]:
    """Blend retrieval score with a zero-dependency lexical signal.

    The lexical signal reads the chunk's context breadcrumb alongside its text,
    for the same reason bm25_search() scores both and _apply_neg_filter() excludes
    on both: every field retrieval can *find* a chunk by must also be visible to
    the stage that re-scores it.  Scoring text only meant a chunk retrieved via its
    breadcrumb was handed lex=0.0 and then actively pushed back down by the very
    reranker that ran on it — so a document whose title names the topic lost to one
    that merely name-drops it in passing.

    Concatenating (rather than max-ing) the two fields keeps the equal 1.0
    weighting _needle_score() and SQLite's bm25(chunks_fts) already use, and
    lexical_overlap()'s per-term tf/(tf+1) saturation bounds what the breadcrumb
    can contribute.  A breadcrumb is identical across all chunks of a section, so
    this lifts a section uniformly and never reorders chunks within it.

    The query is tokenised and NFKC-folded once here, not once per hit inside
    lexical_overlap: the term set is identical across the whole hit list, only the
    text being scored changes.
    """
    norm_terms = _norm_query_terms(query)
    for h in hits:
        lex = _overlap_from_norm(norm_terms, f"{h.text}\n{h.context}" if h.context else h.text)
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


# --- debugging aid ---------------------------------------------------------


def _debug_enabled() -> bool:
    """SHOIN_DEBUG=1 prints retrieval diagnostics to stderr (CLAUDE.md's own
    documented "Debugging Aid" — long claimed under the bare name `DEBUG`,
    but until now never actually read anywhere in the source; `DEBUG` alone
    would also collide with the many unrelated tools/CI systems that already
    use that generic name, unlike every other Shoin setting's `SHOIN_`
    namespace).

    A raw os.environ check, not routed through config._get()'s config.json-
    aware machinery: this is a one-off toggle for an interactive debugging
    session, not a persistent user preference like SHOIN_MULTI_QUERY/
    SHOIN_EMBED_BATCH, so it has no config.json equivalent.
    """
    return os.environ.get("SHOIN_DEBUG", "").strip().lower() not in ("", "0", "false")


def _debug_print(label: str, query: str, negs: list[str], bm25_n: int, vec_n: int, final: list[Hit]) -> None:
    print(
        f"[DEBUG {label}] query={query!r} negs={negs} bm25_hits={bm25_n}"
        f" vec_hits={vec_n} final={len(final)}",
        file=sys.stderr,
    )
    for i, h in enumerate(final, start=1):
        print(
            f"[DEBUG {label}]   #{i} chunk={h.chunk_id} source={h.source_id}"
            f" score={h.score:.4f} bm25={h.bm25:.4f} vec={h.vec:.4f} detail={h.detail}",
            file=sys.stderr,
        )


# --- top-level ------------------------------------------------------------


def retrieve(
    store: Store,
    notebook_id: int,
    query: str,
    query_vec: list[float] | None = None,
    k: int = TOP_K,
) -> list[Hit]:
    """Full pipeline: candidates -> RRF fusion -> lexical rerank -> MMR.

    This single-query path is behaviourally identical to retrieve_multi() called
    with one query (verified by a 400-case fuzz over BM25-only and vector modes,
    ranking AND score). It is deliberately NOT collapsed into that delegation:
    retrieve() is the default, hot path and fuses exactly two lists via the
    two-arg rrf_fuse() primitive, which carries its own RRF-scoring test suite;
    retrieve_multi() exists only for the opt-in multi-query feature and fuses N
    lists via rrf_fuse_lists(). Keeping the arity-matched primitives means the
    common case reads without an inert N-query loop and the tested rrf_fuse()
    primitive keeps a production caller.
    """
    pool = max(k * 3, 12)
    negs = neg_terms(query)
    clean = strip_neg_terms(query) if negs else query
    bm25_hits = bm25_search(store, notebook_id, query, pool)
    vec_hits = vector_search(store, notebook_id, query_vec, pool) if query_vec else []
    # bm25_search() already excludes negated-term hits internally; vector_search()
    # has no query text to do the same, so filter it here. This must happen BEFORE
    # fusion/MMR (not after, on the final k results): MMR spends its k-selection
    # budget against this pool, so filtering post-selection can silently starve
    # the result set below k (or to zero) when negated hits would otherwise have
    # been MMR's top picks, even though valid candidates remain lower in the pool.
    if negs:
        vec_hits = _apply_neg_filter(vec_hits, negs)
    fused = rrf_fuse(bm25_hits, vec_hits)
    # Normalize RRF scores to [0,1] before lexical rerank so the weight=0.3
    # blend ratio is calibrated correctly. Without this, RRF scores (~0.01-0.03)
    # are overwhelmed by lexical_overlap values in [0,1]: lex contributes ~10×
    # more than the RRF signal, making the reranker effectively ignore hybrid
    # retrieval. rrf_fuse() emits raw rank-reciprocal values, so _minmax here
    # rescales them to [0,1] before the lexical blend.
    if fused:
        normed = _minmax([h.score for h in fused])
        for h, n in zip(fused, normed):
            h.score = n
    result = mmr(rerank(clean, fused), k)
    if _debug_enabled():
        _debug_print("retrieve", query, negs, len(bm25_hits), len(vec_hits), result)
    return result


def retrieve_multi(
    store: Store,
    notebook_id: int,
    queries: list[str],
    query_vecs: list[list[float] | None] | None = None,
    k: int = TOP_K,
) -> list[Hit]:
    """Multi-query RAG-Fusion retrieval: fuse ranked lists from several phrasings.

    queries[0] is the user's ORIGINAL query — it alone defines the negative-term
    filter (`-word`) and the lexical-rerank reference, so LLM rewrites can never
    weaken an explicit user exclusion or hijack the rerank signal. Rewrites are
    stripped of any `-token` of their own (a rewrite is not a place to introduce
    filters) and their BM25/vector lists are filtered by the original negs before
    fusion — the same pre-fusion placement the single-query path uses (v0.2.73).
    """
    if not queries:
        return []
    pool = max(k * 3, 12)
    primary = queries[0]
    negs = neg_terms(primary)
    clean = strip_neg_terms(primary) if negs else primary
    vecs: list[list[float] | None] = list(query_vecs or [])
    vecs += [None] * (len(queries) - len(vecs))

    lists: list[list[Hit]] = []
    total_bm25 = 0
    total_vec = 0
    for i, (q, qv) in enumerate(zip(queries, vecs)):
        q_search = q if i == 0 else strip_neg_terms(q)
        bm25_hits = bm25_search(store, notebook_id, q_search, pool)
        if negs and i > 0:
            # bm25_search() already applied the primary query's own negs (i==0);
            # rewrite lists were searched without them and need the filter here.
            bm25_hits = _apply_neg_filter(bm25_hits, negs)
        lists.append(bm25_hits)
        total_bm25 += len(bm25_hits)
        if qv:
            vec_hits = vector_search(store, notebook_id, qv, pool)
            if negs:
                vec_hits = _apply_neg_filter(vec_hits, negs)
            lists.append(vec_hits)
            total_vec += len(vec_hits)

    fused = rrf_fuse_lists(lists)
    if fused:
        normed = _minmax([h.score for h in fused])
        for h, n in zip(fused, normed):
            h.score = n
    result = mmr(rerank(clean, fused), k)
    if _debug_enabled():
        _debug_print(f"retrieve_multi({len(queries)} queries)", primary, negs, total_bm25, total_vec, result)
    return result
