"""Citation extraction and machine verification.

Differentiator (spec REQ-006): hallucinated attributions are mechanically
detectable (arXiv:2412.18004). Two independent, dependency-free checks run on
every generated text:

1. Range check (`validate_citations`): an [S#] number must point at a real
   source. Out-of-range numbers are the narrowest form of citation hallucination.
2. Grounding check (`verify_grounding`): the *content* of a cited sentence must
   actually overlap the cited source's text. This catches the common, harder
   case — a claim mis-attributed to a real source that does not support it.
   The signal is lexical (character-bigram overlap), so it is advisory: low
   overlap flags a *weak* citation, not a definitively false one.
"""

from __future__ import annotations

import re
import unicodedata
from typing import NotRequired, TypedDict

# A citation lives inside square brackets and may combine several sources:
# [S1] / [S1, S2] / [S1; S3] / [S1 and S2] / [S1][S2]. Full-width brackets,
# digits and 'Ｓ' (common from JP-first models) are normalized via NFKC first.
_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
_SNUM_RE = re.compile(r"[Ss]\s*(\d+)")

# Sentence boundaries for JP + EN. Citations stay attached to their sentence so
# each claim is graded against the sources it actually cites.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。．！？!?\n])")

# A cited sentence is "weak" when the fraction of its character bigrams found in
# the cited source(s) falls below this. Calibrated for CJK: content words are
# kanji that survive paraphrase, so genuinely grounded claims score ~0.5-1.0
# while unrelated claims sit near 0.1-0.2 (a lone shared copula bigram such as
# "ある" must not clear the bar). Advisory only — a flag, not a verdict.
GROUNDING_MIN = 0.30


class CitationReport(TypedDict):
    cited: list[int]
    invalid: list[int]
    coverage: float
    n_sources: int
    source_map: dict[str, str]
    # Maps "S1" -> actual source DB id. Present when the caller supplies source_ids,
    # absent on old persisted reports — consumers must guard with .get().
    source_id_map: NotRequired[dict[str, int]]
    # Grounding check (present only when source bodies are supplied):
    #   weak      -> S-numbers cited by at least one weakly-grounded sentence
    #   grounding -> fraction of cited sentences that are well-grounded (1.0 = all)
    weak: NotRequired[list[int]]
    grounding: NotRequired[float]


def extract_citations(text: str) -> list[int]:
    """Return the sorted unique source numbers cited in *text*.

    Only S-numbers *inside brackets* count, so a bare "S1" in prose is not a
    false positive, while combined forms like "[S1, S2]" are both captured.
    """
    norm = unicodedata.normalize("NFKC", text or "")
    nums: set[int] = set()
    for span in _BRACKET_RE.findall(norm):
        nums.update(int(m) for m in _SNUM_RE.findall(span))
    return sorted(nums)


def validate_citations(text: str, n_sources: int) -> tuple[list[int], list[int]]:
    """Split citations into (valid, invalid) against *n_sources* real sources."""
    cited = extract_citations(text)
    valid = [c for c in cited if 1 <= c <= n_sources]
    invalid = [c for c in cited if c < 1 or c > n_sources]
    return valid, invalid


def _bigrams(text: str) -> set[str]:
    """Character bigrams of NFKC-normalised, whitespace-stripped text."""
    t = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).lower())
    if len(t) < 2:
        return {t} if t else set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def verify_grounding(text: str, source_texts: dict[int, str]) -> tuple[list[int], float]:
    """Grade each cited sentence by lexical overlap with the source(s) it cites.

    Returns (weak, score): *weak* is the sorted S-numbers cited by at least one
    weakly-grounded sentence; *score* is the fraction of cited sentences that are
    well-grounded (1.0 when there are no cited sentences). Sentences whose cited
    numbers are all out-of-range are skipped — that is the range check's job.
    """
    weak: set[int] = set()
    cited = 0
    grounded = 0
    for raw in _SENTENCE_SPLIT_RE.split(text):
        sentence = raw.strip()
        if not sentence:
            continue
        nums = [n for n in extract_citations(sentence) if n in source_texts]
        if not nums:
            continue
        cited += 1
        claim = _bigrams(_BRACKET_RE.sub(" ", sentence))  # drop the [S#] markers
        if not claim:
            grounded += 1
            continue
        support: set[str] = set()
        for n in nums:
            support |= _bigrams(source_texts[n])
        if len(claim & support) / len(claim) >= GROUNDING_MIN:
            grounded += 1
        else:
            weak.update(nums)
    score = grounded / cited if cited else 1.0
    return sorted(weak), score


def make_report(
    text: str,
    source_titles: list[str],
    source_ids: list[int] | None = None,
    source_bodies: list[str] | None = None,
) -> CitationReport:
    """Build the citation_report attached to every generated answer/output."""
    n = len(source_titles)
    valid, invalid = validate_citations(text, n)
    report = CitationReport(
        cited=valid,
        invalid=invalid,
        coverage=(len(set(valid)) / n) if n else 0.0,
        n_sources=n,
        source_map={f"S{i + 1}": t for i, t in enumerate(source_titles)},
    )
    if source_ids is not None:
        report["source_id_map"] = {f"S{i + 1}": sid for i, sid in enumerate(source_ids)}
    if source_bodies is not None:
        weak, score = verify_grounding(text, {i + 1: body for i, body in enumerate(source_bodies)})
        report["weak"] = weak
        report["grounding"] = score
    return report
