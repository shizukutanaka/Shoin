"""Studio outputs: grounded documents generated from a notebook's sources.

Five kinds (REQ-101): briefing / study_guide / faq / timeline / mindmap.
Unlike Q&A, Studio uses an overview of *all* sources (first chunks per source)
rather than query-driven retrieval. Every output carries a citation_report.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass

from .chunk import _match_fold
from .citation import CitationReport, looks_like_question, make_report
from .config import MAX_QUESTION_LEN, ui_lang
from .llm import LLMError
from .qa import _LIST_PREFIX_RE, ChatBackend, build_context
from .qa import _t as _qa_t
from .search import Hit
from .store import STUDIO_KINDS, Source, Store, StoreError

# Re-export of store.STUDIO_KINDS — the vocabulary lives in store.py because
# add_studio_output() guards on it, and store.py cannot import this module back.
KINDS = STUDIO_KINDS

_INSTRUCTIONS: dict[str, dict[str, str]] = {
    "briefing": {
        "ja": (
            "全ソースを横断する簡潔なブリーフィング文書をMarkdownで作成。"
            "構成: 概要(3文以内) / 主要ポイント(箇条書き) / 留意点。"
        ),
        "en": (
            "Create a concise briefing document in Markdown covering all sources. "
            "Structure: Executive Summary (3 sentences max) / Key Points (bullets) / Caveats."
        ),
    },
    "study_guide": {
        "ja": (
            "学習ガイドをMarkdownで作成。構成: 重要概念の解説 / 理解確認の設問5問 / "
            "各設問の模範解答(根拠引用付き)。"
        ),
        "en": (
            "Create a study guide in Markdown. "
            "Structure: Explanation of key concepts / 5 comprehension questions / "
            "Model answers for each with source citations."
        ),
    },
    "faq": {
        "ja": "想定FAQをMarkdownで作成。Q&A形式で5〜8問。各回答に根拠引用。",
        "en": (
            "Create an FAQ in Markdown in Q&A format, 5–8 questions. "
            "Each answer must cite its source."
        ),
    },
    "timeline": {
        "ja": (
            "ソース中の出来事・日付を時系列に整理した年表をMarkdownで作成。"
            "日付不明の項目は『時期不明』として末尾にまとめる。"
        ),
        "en": (
            "Create a chronological timeline in Markdown of events and dates in the sources. "
            "Group items with no date at the end under 'Date Unknown'."
        ),
    },
    "mindmap": {
        "ja": (
            "ソース全体の概念構造をMarkdownの階層箇条書き(マインドマップ)で表現。"
            "ルート1項目、深さ3階層まで。"
        ),
        "en": (
            "Represent the conceptual structure of all sources as a Markdown hierarchical "
            "bullet list (mind map). One root item, maximum 3 levels deep."
        ),
    },
}

_STRINGS: dict[str, dict[str, str]] = {
    "sources_header": {"ja": "ソース", "en": "Sources"},
    "instructions_header": {"ja": "指示", "en": "Instructions"},
    "citation_note": {
        "ja": "事実を述べる箇所には必ず [S番号] の引用を付ける。",
        "en": "Cite all factual statements with [S number] references.",
    },
    "question_prompt": {
        "ja": "このソース群に対して読者が尋ねそうな質問を{n}個、1行1問・装飾なしで列挙。",
        "en": (
            "List {n} questions a reader might ask about these sources, "
            "one per line, no decoration."
        ),
    },
    # v0.2.660 (product-review #43): deterministic suggestion shape when the
    # LLM is unreachable — same "tell me about the title" skeleton eval
    # --gen emits, so an unreachable model never collapses the surface to [].
    "question_fallback": {
        "ja": "「{title}」とは何ですか",
        "en": "What is \"{title}\"?",
    },
}

# _LIST_PREFIX_RE moved to qa.py (v0.2.125): rewrite_queries() parses the same
# LLM list-output convention, and two independently-maintained copies of one
# parsing rule is exactly the drift failure v0.2.80 consolidated elsewhere.

STUDIO_BUDGET_TOKENS = 2800
OVERVIEW_CHUNKS_PER_SOURCE = 3


def _t(key: str) -> str:
    lang = ui_lang()
    return _STRINGS[key].get(lang, _STRINGS[key]["en"])


def _t_kind(kind: str) -> str:
    lang = ui_lang()
    return _INSTRUCTIONS[kind].get(lang, _INSTRUCTIONS[kind]["en"])


@dataclass
class StudioResult:
    kind: str
    body: str
    report: CitationReport


def overview_hits(
    store: Store, notebook_id: int, per_source: int = OVERVIEW_CHUNKS_PER_SOURCE
) -> list[Hit]:
    """Representative chunks: equidistant across each source's full length.

    Sampling from positions 0, mid, end (rather than the first *per_source* chunks)
    ensures that long documents contribute content from their full span — not just
    their introduction — to Studio outputs like timelines and mindmaps.
    """
    size_rows = store.conn.execute(
        "SELECT c.source_id, MAX(c.seq) AS max_seq"
        " FROM chunks c JOIN sources s ON s.id=c.source_id"
        " WHERE s.notebook_id=? GROUP BY c.source_id ORDER BY c.source_id",
        (notebook_id,),
    ).fetchall()
    hits: list[Hit] = []
    for sr in size_rows:
        if per_source <= 0:
            continue
        src_id: int = sr["source_id"]
        max_seq: int = sr["max_seq"]
        if max_seq + 1 <= per_source:
            rows = store.conn.execute(
                "SELECT id, source_id, text, context FROM chunks WHERE source_id=? ORDER BY seq",
                (src_id,),
            ).fetchall()
        else:
            if per_source <= 1:
                target_seqs: list[int] = [0]
            else:
                target_seqs = sorted(
                    {i * max_seq // (per_source - 1) for i in range(per_source)}
                )
            ph = ",".join("?" * len(target_seqs))
            rows = store.conn.execute(
                f"SELECT id, source_id, text, context FROM chunks"
                f" WHERE source_id=? AND seq IN ({ph}) ORDER BY seq",
                (src_id, *target_seqs),
            ).fetchall()
        hits.extend(
            Hit(r["id"], r["source_id"], r["text"], score=1.0, context=str(r["context"] or ""))
            for r in rows
        )
    return hits


def generate(
    store: Store, llm: ChatBackend, notebook_id: int, kind: str, persist: bool = True
) -> StudioResult:
    """Generate one Studio output. Raises LLMError when the endpoint is down."""
    if kind not in KINDS:
        raise StoreError("STUDIO_KIND_INVALID", f"unknown studio kind: {kind!r}")
    # v0.2.721: the sampling SELECTs (per-source sizes, then rows), the
    # notebook probe and build_context's source re-reads all run under one
    # WAL snapshot — on auto-commit reads a concurrent
    # replace_chunks_for_source/delete landing mid-flight splices chunks
    # from different commits into the one output this call persists.
    with store.read_snapshot():
        store.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
        hits = overview_hits(store, notebook_id)
        if not hits:
            raise StoreError("NOTEBOOK_EMPTY", "notebook has no sources to ground on")
        # Mirrors qa.ask()'s identical guard (v0.2.44) around the same
        # build_context() call: a bare sqlite3.OperationalError from a WAL
        # busy_timeout would otherwise propagate to server.py's catch-all,
        # which returns HTTP 500 with only type(exc).__name__ as the message
        # (the real "database is locked" text is dropped) instead of ask()'s
        # clean HTTP 400 SYSTEM_DB_LOCKED with the actual lock message.
        try:
            # rank_weighted=False (v0.2.552): overview hits carry no relevance
            # ranking — every sampled chunk scores 1.0 in source-id order — so the
            # harmonic decay would arbitrarily hand source #1 ~6x source #10's
            # excerpt in outputs documented to cover all sources equally.
            context = build_context(
                store, hits, budget_tokens=STUDIO_BUDGET_TOKENS, rank_weighted=False
            )
        except sqlite3.OperationalError as exc:
            raise StoreError(
                "SYSTEM_DB_LOCKED",
                f"database locked during context build: {exc}",
            ) from exc
    sh = _t("sources_header")
    ih = _t("instructions_header")
    cn = _t("citation_note")
    user = f"## {sh}\n{context.block}\n\n## {ih}\n{_t_kind(kind)}\n{cn}"
    body = llm.chat(
        [
            {"role": "system", "content": _qa_t("system_prompt")},
            {"role": "user", "content": user},
        ]
    )
    if not body or not body.strip():
        raise LLMError("SYSTEM_LLM_BAD_RESPONSE", "empty response from LLM")
    report = make_report(
        body, context.source_titles, context.source_ids, context.source_bodies,
        context.source_contexts, context.source_chunk_ids, context.source_detail,
    )
    # finish_reason "length" = output stopped at MAX_TOKENS mid-list — flag it
    # so Studio cards carry the same truncated warning as chat answers.
    if getattr(llm, "last_finish_reason", None) == "length":
        report["truncated"] = True
    if persist:
        store.add_studio_output(notebook_id, kind, body, json.dumps(report))
    return StudioResult(kind, body, report)


def questions_fingerprint(store: Store, notebook_id: int) -> tuple[object, ...]:
    """Fingerprint of everything suggest_questions() reads (v0.2.701).

    (id, sha256, title) alone misses the one content mutation that keeps
    all three: update_chunk_text() rewrites a chunk's text in place while
    sources.sha256 stays put — the sha labels the *origin* document by
    design (the v0.2.682 dedupe relies on that), so the edit moved nothing
    in the tuple while changing exactly what suggestions would be built
    from. The fingerprint therefore also carries the sampled hit rows
    themselves — the same overview_hits(per_source=2) the generator
    consumes — so every mutation that could change the output moves it:
    add/delete/refresh/rename AND in-place edits. Sampling keeps the check
    cheap too: O(sources + per_source×source_count), never the corpus.
    """
    # v0.2.721: both halves under one snapshot — a torn fingerprint
    # (sources@commitA + sampled hits@commitB) would label cached questions
    # with a corpus state that never coherently existed.
    with store.read_snapshot():
        # per_source must match suggest_questions()'s sample width below.
        return _questions_fingerprint_rows(
            store.sources_for_notebook(notebook_id),
            overview_hits(store, notebook_id, per_source=2),
        )


def _questions_fingerprint_rows(
    sources: list[Source], hits: list[Hit]
) -> tuple[object, ...]:
    """The fingerprint tuple over already-read rows — split from
    questions_fingerprint() so suggest_questions_fingerprinted() can key the
    cache on the exact rows it sampled inside its own snapshot (v0.2.723)."""
    return (
        tuple((s.id, s.sha256, s.title) for s in sources),
        tuple((h.chunk_id, h.source_id, h.text, h.context) for h in hits),
    )


def suggest_questions(store: Store, llm: ChatBackend, notebook_id: int, n: int = 4) -> list[str]:
    """Suggested questions for a notebook (REQ-102). Best-effort parsing."""
    return suggest_questions_fingerprinted(store, llm, notebook_id, n)[0]


def suggest_questions_fingerprinted(
    store: Store, llm: ChatBackend, notebook_id: int, n: int = 4
) -> tuple[list[str], tuple[object, ...]]:
    """(questions, fingerprint-of-read-state).

    v0.2.723: _h_questions keyed the cache on a fingerprint computed BEFORE
    generation — a write landing between the two calls cached questions
    generated from state B under state A's key, so a later request seeing
    state A was served suggestions describing content that was never in it.
    Keying on the fingerprint read inside the generation snapshot makes the
    cache key always describe exactly the corpus the questions were built
    from.
    """
    # v0.2.721: probe + sampling SELECTs + build_context under one snapshot —
    # same one-commit corpus contract as generate() and qa.ask() (v0.2.720).
    with store.read_snapshot():
        store.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
        hits = overview_hits(store, notebook_id, per_source=2)
        fingerprint = _questions_fingerprint_rows(
            store.sources_for_notebook(notebook_id), hits
        )
        if not hits:
            return [], fingerprint
        # Same guard as generate() above and qa.ask() (v0.2.44) around the
        # identical build_context() call. A DB lock is a different failure class
        # from the LLMError this function already swallows into [] below (that's
        # specifically for "LLM unreachable", a best-effort degradation) — raise
        # so the caller gets a diagnosable SYSTEM_DB_LOCKED error instead of a
        # silent, misleading "no suggestions" result indistinguishable from "no
        # sources".
        try:
            context = build_context(
                store, hits, budget_tokens=1600, rank_weighted=False
            )
        except sqlite3.OperationalError as exc:
            raise StoreError(
                "SYSTEM_DB_LOCKED",
                f"database locked during context build: {exc}",
            ) from exc
    sh = _t("sources_header")
    prompt = _t("question_prompt").format(n=n)
    user = f"## {sh}\n{context.block}\n\n{prompt}"
    try:
        text = llm.chat(
            [
                {"role": "system", "content": _qa_t("system_prompt")},
                {"role": "user", "content": user},
            ]
        )
    except LLMError:
        # Model unreachable must not read as "this notebook has nothing
        # worth asking" — fall back to title-derived skeleton questions
        # (v0.2.660, product-review #43).
        return _title_questions(store, notebook_id, hits, n), fingerprint
    # Question detection is shared with citation.py's uncited_sentences() via
    # looks_like_question() — see that function's docstring for why this used to
    # be two independently-drifting copies of the same heuristic.
    # Two filters beyond that, both user-visible defects when absent:
    # - > MAX_QUESTION_LEN: the /ask endpoint rejects questions that long, so
    #   the app would suggest a question it cannot itself answer (a degenerate
    #   LLM can emit a multi-KB runaway line that becomes a huge cached chip).
    # - duplicates: repetition-prone local LLMs can emit the same line twice,
    #   rendering identical suggestion chips.
    questions: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        q = _LIST_PREFIX_RE.sub("", unicodedata.normalize("NFKC", line.strip())).strip()
        # Folded dedup key (v0.2.545): データ設計は? / でーた設計は? render as
        # two chips that would answer identically.
        key = _match_fold(q)
        if (
            len(q) >= 2
            and len(q) <= MAX_QUESTION_LEN
            and looks_like_question(q)
            and key not in seen
        ):
            seen.add(key)
            questions.append(q)
    return questions[:n], fingerprint


# Titles longer than this are skipped rather than wrapped — a filename dump
# or near-pathological title would produce a suggestion the /ask surface
# itself reads as noise (and 60 + the template wrap stays far under
# MAX_QUESTION_LEN).
_FALLBACK_TITLE_MAX = 60


def _title_questions(
    store: Store, notebook_id: int, hits: list[Hit], n: int
) -> list[str]:
    """Deterministic question seeds derived from source titles (v0.2.660).

    suggest_questions() calls this only when llm.chat raised LLMError — a
    model that answered but produced no question-shaped lines still returns
    [], because "reachable and chose nothing" is honest where "unreachable"
    is not. Title questions are answerable by construction (their source is
    in the notebook) but deliberately shallow — they name a source, not a
    theme inside it, mirroring the eval --gen skeleton. URL-lookalike,
    oversized, and duplicate-folded titles are skipped; a hit whose source
    disappeared between the two reads is simply absent from the map.
    """
    titles = {s.id: s.title for s in store.sources_for_notebook(notebook_id)}
    out: list[str] = []
    seen: set[str] = set()
    done: set[int] = set()
    for h in hits:
        if h.source_id in done or h.source_id not in titles:
            continue
        done.add(h.source_id)
        title = unicodedata.normalize("NFKC", titles[h.source_id]).strip()
        if (
            not (2 <= len(title) <= _FALLBACK_TITLE_MAX)
            or "://" in title
            or title.startswith("www.")
        ):
            continue
        key = _match_fold(title)
        if key in seen:
            continue
        seen.add(key)
        # _t call sites must supply their placeholders inline (i18n pin).
        out.append(_t("question_fallback").format(title=title))
        if len(out) >= n:
            break
    return out
