"""Studio outputs: grounded documents generated from a notebook's sources.

Five kinds (REQ-101): briefing / study_guide / faq / timeline / mindmap.
Unlike Q&A, Studio uses an overview of *all* sources (first chunks per source)
rather than query-driven retrieval. Every output carries a citation_report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .citation import CitationReport, make_report
from .llm import LLMError
from .qa import SYSTEM_PROMPT, ChatBackend, build_context
from .search import Hit
from .store import Store, StoreError

KINDS = ("briefing", "study_guide", "faq", "timeline", "mindmap")

_INSTRUCTIONS: dict[str, str] = {
    "briefing": (
        "全ソースを横断する簡潔なブリーフィング文書をMarkdownで作成。"
        "構成: 概要(3文以内) / 主要ポイント(箇条書き) / 留意点。"
    ),
    "study_guide": (
        "学習ガイドをMarkdownで作成。構成: 重要概念の解説 / 理解確認の設問5問 / "
        "各設問の模範解答(根拠引用付き)。"
    ),
    "faq": "想定FAQをMarkdownで作成。Q&A形式で5〜8問。各回答に根拠引用。",
    "timeline": (
        "ソース中の出来事・日付を時系列に整理した年表をMarkdownで作成。"
        "日付不明の項目は『時期不明』として末尾にまとめる。"
    ),
    "mindmap": (
        "ソース全体の概念構造をMarkdownの階層箇条書き(マインドマップ)で表現。"
        "ルート1項目、深さ3階層まで。"
    ),
}

STUDIO_BUDGET_TOKENS = 2800
OVERVIEW_CHUNKS_PER_SOURCE = 3


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
    hits = overview_hits(store, notebook_id)
    if not hits:
        raise StoreError("NOTEBOOK_EMPTY", "notebook has no sources to ground on")
    context = build_context(store, hits, budget_tokens=STUDIO_BUDGET_TOKENS)
    user = (
        f"## ソース\n{context.block}\n\n## 指示\n{_INSTRUCTIONS[kind]}\n"
        "事実を述べる箇所には必ず [S番号] の引用を付ける。"
    )
    body = llm.chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
    )
    report = make_report(body, context.source_titles, context.source_ids, context.source_bodies)
    if persist:
        store.add_studio_output(notebook_id, kind, body, json.dumps(report))
    return StudioResult(kind, body, report)


def suggest_questions(store: Store, llm: ChatBackend, notebook_id: int, n: int = 4) -> list[str]:
    """Suggested questions for a notebook (REQ-102). Best-effort parsing."""
    hits = overview_hits(store, notebook_id, per_source=2)
    if not hits:
        return []
    context = build_context(store, hits, budget_tokens=1600)
    user = (
        f"## ソース\n{context.block}\n\n"
        f"このソース群に対して読者が尋ねそうな質問を{n}個、1行1問・装飾なしで列挙。"
    )
    try:
        text = llm.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ]
        )
    except LLMError:
        return []
    questions: list[str] = []
    for line in text.splitlines():
        q = line.strip().lstrip("0123456789.-*・ 　").strip()
        q_base = q.rstrip("。．!！?？")  # strip trailing punctuation for endswith check
        if q and ("?" in q or "？" in q or q_base.endswith("か")):
            questions.append(q)
    return questions[:n]
