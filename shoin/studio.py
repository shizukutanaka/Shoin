"""Studio outputs: grounded documents generated from a notebook's sources.

Five kinds (REQ-101): briefing / study_guide / faq / timeline / mindmap.
Unlike Q&A, Studio uses an overview of *all* sources (first chunks per source)
rather than query-driven retrieval. Every output carries a citation_report.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

from .citation import CitationReport, make_report
from .config import ui_lang
from .llm import LLMError
from .qa import _t as _qa_t, ChatBackend, build_context
from .search import Hit
from .store import Store, StoreError

KINDS = ("briefing", "study_guide", "faq", "timeline", "mindmap")

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
        "en": "Create an FAQ in Markdown in Q&A format, 5–8 questions. Each answer must cite its source.",
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
        "en": "List {n} questions a reader might ask about these sources, one per line, no decoration.",
    },
}

# Matches common list-item prefixes after NFKC normalization:
# numeric ("1.", "10)", "3、") and bullet ("-", "*", "·" <U+00B7 from "・">, "•", "–", "—").
# Using regex instead of str.lstrip so that digit-leading questions like
# "2024年の出来事は？" are not corrupted (lstrip strips any leading digit).
_LIST_PREFIX_RE = re.compile(r"^(?:\d+[.)、]\s*|[-*·•–—]\s*)")

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
                "SELECT id, source_id, text FROM chunks WHERE source_id=? ORDER BY seq",
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
                f"SELECT id, source_id, text FROM chunks"
                f" WHERE source_id=? AND seq IN ({ph}) ORDER BY seq",
                (src_id, *target_seqs),
            ).fetchall()
        hits.extend(Hit(r["id"], r["source_id"], r["text"], score=1.0) for r in rows)
    return hits


def generate(
    store: Store, llm: ChatBackend, notebook_id: int, kind: str, persist: bool = True
) -> StudioResult:
    """Generate one Studio output. Raises LLMError when the endpoint is down."""
    if kind not in KINDS:
        raise StoreError("STUDIO_KIND_INVALID", f"unknown studio kind: {kind!r}")
    store.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
    hits = overview_hits(store, notebook_id)
    if not hits:
        raise StoreError("NOTEBOOK_EMPTY", "notebook has no sources to ground on")
    context = build_context(store, hits, budget_tokens=STUDIO_BUDGET_TOKENS)
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
    report = make_report(body, context.source_titles, context.source_ids, context.source_bodies)
    if persist:
        store.add_studio_output(notebook_id, kind, body, json.dumps(report))
    return StudioResult(kind, body, report)


def suggest_questions(store: Store, llm: ChatBackend, notebook_id: int, n: int = 4) -> list[str]:
    """Suggested questions for a notebook (REQ-102). Best-effort parsing."""
    store.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND if missing
    hits = overview_hits(store, notebook_id, per_source=2)
    if not hits:
        return []
    context = build_context(store, hits, budget_tokens=1600)
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
        return []
    questions: list[str] = []
    for line in text.splitlines():
        q = _LIST_PREFIX_RE.sub("", unicodedata.normalize("NFKC", line.strip())).strip()
        q_base = q.rstrip("。．!?")  # strip trailing punctuation for endswith check
        if q and ("?" in q or q_base.endswith("か")):
            questions.append(q)
    return questions[:n]
