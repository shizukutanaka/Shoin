"""Citation extraction and machine verification.

Differentiator (spec REQ-006): hallucinated attributions are mechanically
detectable (arXiv:2412.18004). Every generated text is checked against the
actual source count; out-of-range citations are flagged, coverage is measured,
and a source map ([S1] -> title) is attached for verifiability.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TypedDict

# A citation lives inside square brackets and may combine several sources:
# [S1] / [S1, S2] / [S1; S3] / [S1 and S2] / [S1][S2]. Full-width brackets,
# digits and 'Ｓ' (common from JP-first models) are normalized via NFKC first.
_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
_SNUM_RE = re.compile(r"[Ss]\s*(\d+)")


class CitationReport(TypedDict):
    cited: list[int]
    invalid: list[int]
    coverage: float
    n_sources: int
    source_map: dict[str, str]


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


def make_report(text: str, source_titles: list[str]) -> CitationReport:
    """Build the citation_report attached to every generated answer/output."""
    n = len(source_titles)
    valid, invalid = validate_citations(text, n)
    return CitationReport(
        cited=valid,
        invalid=invalid,
        coverage=(len(set(valid)) / n) if n else 0.0,
        n_sources=n,
        source_map={f"S{i + 1}": t for i, t in enumerate(source_titles)},
    )
