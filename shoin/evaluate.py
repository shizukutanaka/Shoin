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

from collections import deque
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
    # Expected ids that do NOT exist in the notebook (deleted or renumbered —
    # sqlite source ids are autoincrement, so re-adding a source rekeys it).
    # Such an id can never be retrieved, so the case is silently unwinnable:
    # without surfacing it, a stale case file reads as a retrieval regression
    # forever — the same "silently rekeyed" class diff_reports guards on the
    # question side.
    missing: list[int] = field(default_factory=list)


@dataclass
class EvalReport:
    cases: list[CaseResult] = field(default_factory=list)
    recall: float = 0.0  # mean over cases
    mrr: float = 0.0  # mean reciprocal rank


@dataclass(frozen=True)
class CaseDelta:
    """One case's score movement between two runs of the same case file."""

    question: str
    recall_before: float
    recall_after: float
    rr_before: float
    rr_after: float


@dataclass
class EvalDiff:
    """Baseline-vs-current comparison: aggregate deltas plus per-case moves.

    The per-case list keeps only cases whose score CHANGED — an A/B run exists
    to answer "did this setting help", so unchanged cases are noise. Questions
    present in only one run are surfaced separately: a silently-rekeyed case
    file would otherwise look like a score change.
    """

    d_recall: float = 0.0
    # Means over the PAIRED questions only — the CLI prints these beside the
    # deltas so the comparison rows describe the same population the delta was
    # computed on (full-run means stay in the eval summary above the diff).
    recall_before: float = 0.0
    recall_after: float = 0.0
    mrr_before: float = 0.0
    mrr_after: float = 0.0
    d_mrr: float = 0.0
    case_deltas: list[CaseDelta] = field(default_factory=list)
    new_questions: list[str] = field(default_factory=list)
    dropped_questions: list[str] = field(default_factory=list)
    # How many questions the aggregate deltas were computed over. When the case
    # file was edited between runs this is < len(cases) on both sides — the
    # context a reader needs to trust d_recall/d_mrr.
    matched_questions: int = 0


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
    # One fetch for every case's expected-id existence check — a source deleted
    # and re-added gets a NEW autoincrement id, so stale case files referencing
    # the old id would otherwise score 0 with no hint that the case, not the
    # retrieval, is what changed.
    present = {s.id for s in store.sources_for_notebook(notebook_id)}
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
        results.append(
            CaseResult(
                case.question,
                list(expected),
                ranked,
                recall,
                rr,
                [sid for sid in expected if sid not in present],
            )
        )
    n = len(results)
    return EvalReport(
        cases=results,
        recall=sum(r.recall for r in results) / n if n else 0.0,
        mrr=sum(r.reciprocal_rank for r in results) / n if n else 0.0,
    )


def report_to_dict(rep: EvalReport, k: int) -> dict[str, object]:
    """Serialize a run for `--save` — the baseline a later `--diff` compares
    against. `k` is stored so a diff across different search depths warns
    instead of comparing apples to oranges."""
    return {
        "k": k,
        "recall": rep.recall,
        "mrr": rep.mrr,
        "cases": [
            {
                "q": c.question,
                "expected": c.expected,
                "retrieved": c.retrieved,
                "recall": c.recall,
                "rr": c.reciprocal_rank,
                "missing": c.missing,
            }
            for c in rep.cases
        ],
    }


def report_from_dict(data: object) -> tuple[EvalReport, int | None]:
    """Rebuild a saved report. Raises ValueError on malformed input — same
    refuse-to-degrade rule as parse_cases: a silently-dropped baseline case
    would fabricate a score delta."""
    if not isinstance(data, dict):
        raise ValueError("baseline file must contain a JSON object")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("baseline file has no 'cases' array")
    cases: list[CaseResult] = []
    for i, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise ValueError(f"baseline case {i}: expected an object")
        q = raw.get("q")
        exp = raw.get("expected")
        got = raw.get("retrieved")
        rec = raw.get("recall")
        rr = raw.get("rr")
        miss = raw.get("missing", [])  # absent in pre-v0.2.551 baselines
        if (
            not isinstance(q, str)
            or not isinstance(exp, list)
            or not isinstance(got, list)
            or not isinstance(rec, (int, float))
            or not isinstance(rr, (int, float))
            or not isinstance(miss, list)
            or any(isinstance(s, bool) or not isinstance(s, int) for s in miss)
        ):
            raise ValueError(f"baseline case {i}: missing or mistyped fields")
        cases.append(CaseResult(q, exp, got, float(rec), float(rr), list(miss)))
    rec_all = data.get("recall")
    mrr_all = data.get("mrr")
    if not isinstance(rec_all, (int, float)) or not isinstance(mrr_all, (int, float)):
        raise ValueError("baseline file has missing or non-numeric recall/mrr")
    k_raw = data.get("k")
    return (
        EvalReport(cases=cases, recall=float(rec_all), mrr=float(mrr_all)),
        int(k_raw) if isinstance(k_raw, (int, float)) else None,
    )


def diff_reports(before: EvalReport, after: EvalReport) -> EvalDiff:
    """Compare two runs of (ideally) the same case file: baseline → current.

    Cases match by question text — the case file may be reordered or edited
    between runs, and index-matching would mislabel edits as regressions. A
    question occurring twice pairs occurrence-by-occurrence in file order;
    unpaired extra occurrences are excluded from the aggregates (a leftover
    is neither "new" nor "dropped" — the question still exists in both runs).
    Aggregate deltas use only the questions present in BOTH runs; new/dropped
    questions are listed separately instead of distorting the score change.
    """
    before_qs = {c.question for c in before.cases}
    after_qs = {c.question for c in after.cases}
    pool: dict[str, deque[CaseResult]] = {}
    for c in before.cases:
        pool.setdefault(c.question, deque()).append(c)
    deltas: list[CaseDelta] = []
    matched_old: list[CaseResult] = []
    matched_new: list[CaseResult] = []
    for c in after.cases:
        q_before = pool.get(c.question)
        if not q_before:
            continue
        old = q_before.popleft()
        matched_old.append(old)
        matched_new.append(c)
        if old.recall != c.recall or old.reciprocal_rank != c.reciprocal_rank:
            deltas.append(
                CaseDelta(
                    c.question,
                    old.recall,
                    c.recall,
                    old.reciprocal_rank,
                    c.reciprocal_rank,
                )
            )
    # Aggregate deltas are computed over the SHARED questions only. Comparing
    # the raw report means (after.recall - before.recall) would fold case-set
    # edits into the score: dropping a hard case between runs would read as a
    # retrieval improvement that never happened — the measurement lying about
    # exactly the question the tool exists to answer.
    n_matched = len(matched_new)
    d_recall = d_mrr = 0.0
    recall_before = recall_after = mrr_before = mrr_after = 0.0
    if n_matched:
        recall_before = sum(c.recall for c in matched_old) / n_matched
        recall_after = sum(c.recall for c in matched_new) / n_matched
        mrr_before = sum(c.reciprocal_rank for c in matched_old) / n_matched
        mrr_after = sum(c.reciprocal_rank for c in matched_new) / n_matched
        d_recall = recall_after - recall_before
        d_mrr = mrr_after - mrr_before
    return EvalDiff(
        d_recall=d_recall,
        recall_before=recall_before,
        recall_after=recall_after,
        mrr_before=mrr_before,
        mrr_after=mrr_after,
        d_mrr=d_mrr,
        case_deltas=deltas,
        matched_questions=n_matched,
        new_questions=[c.question for c in after.cases if c.question not in before_qs],
        dropped_questions=[c.question for c in before.cases if c.question not in after_qs],
    )
