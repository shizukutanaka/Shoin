"""Retrieval evaluation: measure recall/MRR on user-authored cases.

Every retrieval decision in this project (RRF fusion, contextual chunking,
multi-query RAG-Fusion, chunk overlap) has been justified from the literature,
never measured on the user's own corpus — yet the same literature consistently
ends with "and measure", because the reported effect sizes are corpus- and
retriever-specific (a 2026 systematic study found chunk overlap gave no benefit
on SPLADE/English-QA, the opposite of the common 10-20% recommendation).

This module closes that gap without adding a dependency or leaving the machine:
the user writes a handful of question -> expected-source cases for their own
notebook and can then answer concrete questions like "does SHOIN_MULTI_QUERY=1
actually help MY documents?" with evidence instead of belief.

Metrics are deliberately the two simplest that answer "did retrieval surface the
right documents": recall (share of expected sources found within top-k) and MRR
(1/rank of the first expected source). No aggregate "quality score" is invented —
same principle as citation.py: report what is directly measurable, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import TOP_K
from .qa import ChatBackend, _check_embed_model_ok, _query_vector, retrieve_for_question
from .store import Store


@dataclass(frozen=True)
class EvalCase:
    """One question plus the source ids a correct retrieval must surface."""

    question: str
    expected_source_ids: list[int]


@dataclass
class CaseResult:
    question: str
    expected: list[int]
    retrieved: list[int]  # distinct source ids, best rank first
    recall: float  # share of `expected` present in `retrieved`
    reciprocal_rank: float  # 1/rank of the first expected source, else 0.0


@dataclass
class EvalReport:
    cases: list[CaseResult] = field(default_factory=list)
    recall: float = 0.0  # mean over cases
    mrr: float = 0.0  # mean reciprocal rank


def parse_cases(data: object) -> list[EvalCase]:
    """Parse the cases file's decoded JSON into EvalCase objects.

    Expected shape: [{"q": "...", "sources": [1, 2]}, ...]. Raises ValueError
    with a concrete message on malformed input — a silently-skipped case would
    quietly inflate the score, which is worse than refusing to run.
    """
    if not isinstance(data, list):
        raise ValueError("cases file must contain a JSON array of case objects")
    cases: list[EvalCase] = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            raise ValueError(f"case {i}: expected an object, got {type(raw).__name__}")
        q = raw.get("q")
        if not isinstance(q, str) or not q.strip():
            raise ValueError(f"case {i}: 'q' must be a non-empty string")
        srcs = raw.get("sources")
        if not isinstance(srcs, list) or not srcs:
            raise ValueError(f"case {i}: 'sources' must be a non-empty array of source ids")
        ids: list[int] = []
        for s in srcs:
            if isinstance(s, bool) or not isinstance(s, int):
                raise ValueError(f"case {i}: source ids must be integers, got {s!r}")
            ids.append(s)
        cases.append(EvalCase(q.strip(), ids))
    if not cases:
        raise ValueError("cases file contains no cases")
    return cases


def evaluate(
    store: Store,
    llm: ChatBackend,
    notebook_id: int,
    cases: list[EvalCase],
    k: int = TOP_K,
) -> EvalReport:
    """Run each case through the SAME retrieval path `ask()` uses.

    Going through retrieve_for_question() (not retrieve()) is the point: the
    measurement then reflects the user's actual configuration, so toggling
    SHOIN_MULTI_QUERY and re-running compares what they will really experience.
    """
    store.get_notebook(notebook_id)  # raises NOTEBOOK_NOT_FOUND
    results: list[CaseResult] = []
    for case in cases:
        qvec = _query_vector(llm, case.question) if _check_embed_model_ok(store, llm) else None
        hits = retrieve_for_question(store, llm, notebook_id, case.question, qvec, k=k)
        # Rank by source, not by chunk: a source found via its 3rd chunk is still
        # found. Keep first-seen order so the rank reflects retrieval quality.
        ranked: list[int] = []
        for h in hits:
            if h.source_id not in ranked:
                ranked.append(h.source_id)
        expected = case.expected_source_ids
        found = [sid for sid in expected if sid in ranked]
        recall = len(found) / len(expected) if expected else 0.0
        rr = 0.0
        for pos, sid in enumerate(ranked, start=1):
            if sid in expected:
                rr = 1.0 / pos
                break
        results.append(CaseResult(case.question, list(expected), ranked, recall, rr))
    n = len(results)
    return EvalReport(
        cases=results,
        recall=sum(r.recall for r in results) / n if n else 0.0,
        mrr=sum(r.reciprocal_rank for r in results) / n if n else 0.0,
    )
