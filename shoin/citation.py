"""Citation extraction and machine verification.

Differentiator (spec REQ-006): hallucinated attributions are mechanically
detectable (arXiv:2412.18004). Four dependency-free, LLM-free checks run on
every generated text:

1. Range check (`validate_citations`): an [S#] number must point at a real
   source. Out-of-range numbers are the narrowest form of citation hallucination.
2. Grounding confirmation (`verify_grounding`): when a cited sentence's wording
   lexically overlaps the source it cites, the citation is *confirmed* — strong
   positive evidence the claim is supported.
3. Mis-numbering detection (`verify_grounding`): when a cited sentence does NOT
   match its cited source but DOES strongly match a *different* source, the
   citation number is very likely wrong — a high-precision error signal.
4. Uncited-assertion detection (`uncited_sentences`): checks 2 and 3 only look
   at sentences that already carry a citation. A hallucinated or unsupported
   claim with *zero* citations anywhere in it is invisible to those checks —
   this scans for exactly that gap (docs/product-review.md priority item #1).

A lexical signal is asymmetric: high overlap reliably *confirms* support, but
low overlap is inconclusive (a correct synonym paraphrase and a true
misattribution both score ~0). So the checks only *assert* what they can stand
behind — confirmation, or a wrong number — and stay silent otherwise rather
than falsely accusing a correctly paraphrased answer.

No aggregate grounding score is emitted: a ratio of confirmed/cited would be
0.0 when all citations are valid synonym paraphrases (inconclusive, not bad),
which contradicts the "stay silent when inconclusive" principle.  The
`confirmed`, `misattributed`, and `uncited` lists are the complete, honest
signal — concrete evidence the user can inspect, not a single opaque number.
"""

from __future__ import annotations

import re
import unicodedata
from typing import NotRequired, TypedDict

from .chunk import _SENTENCE_SPLIT_RE  # single source of truth for sentence boundaries

# A citation lives inside square brackets and may combine several sources:
# [S1] / [S1, S2] / [S1; S3] / [S1 and S2] / [S1][S2]. Full-width brackets,
# digits and 'Ｓ' (common from JP-first models) are normalized via NFKC first.
_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
_SNUM_RE = re.compile(r"[Ss]\s*(\d+)")

# A cited sentence whose character-bigram overlap with a source reaches this is
# treated as lexically supported by that source. Calibrated for CJK, where
# content words are kanji that survive paraphrase.
CONFIRM_MIN = 0.30
# A citation is flagged mis-numbered only when some *other* source beats the
# cited one by at least this margin — a deliberately wide gap so synonym
# paraphrase (which matches nothing strongly) is never mistaken for a wrong number.
MISMATCH_GAP = 0.20

# Phrases the system prompt (qa.py SYSTEM_PROMPT rule 3) instructs the model to use
# when a fact is not in the sources ("state explicitly that it's not in the source,
# do not fill in by guessing"). A sentence containing one of these is the *correct*
# response to missing information, not an unsupported assertion — it must not be
# flagged by uncited_sentences() even though it carries no [S#] citation.
_DISCLAIMER_MARKERS = (
    "記載なし",
    "記載がない",
    "記載は見当たら",
    "見つかりませんでした",
    "not in the source",
    "not mentioned",
    "not found in the source",
)


class CitationReport(TypedDict):
    cited: list[int]
    invalid: list[int]
    coverage: float
    n_sources: int
    source_map: dict[str, str]
    # Maps "S1" -> actual source DB id. Present when the caller supplies source_ids,
    # absent on old persisted reports — consumers must guard with .get().
    source_id_map: NotRequired[dict[str, int]]
    # Grounding checks (present only when source bodies are supplied):
    #   confirmed     -> S-numbers whose cited sentence is lexically supported
    #   misattributed -> S-numbers whose cited sentence clearly belongs elsewhere
    confirmed: NotRequired[list[int]]
    misattributed: NotRequired[list[int]]
    # True when the LLM was unreachable and the answer is search-only excerpts.
    # Absent on non-degraded responses and old persisted reports.
    degraded: NotRequired[bool]
    # Maps "S1" -> excerpt of the text actually retrieved as context for the answer.
    # Allows the UI to show the supporting passage immediately on seal-click without
    # an extra HTTP fetch. Absent on old persisted reports — consumers must guard.
    source_excerpts: NotRequired[dict[str, str]]
    # Sentences that assert content with zero [S#] citations anywhere in them —
    # invisible to verify_grounding(), which only checks already-cited sentences.
    # Present only when n_sources > 0 (nothing to cite against otherwise).
    # Absent on old persisted reports — consumers must guard.
    uncited: NotRequired[list[str]]


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
        return set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _overlap(claim: set[str], source: set[str]) -> float:
    """Fraction of the claim's bigrams that appear in the source."""
    return len(claim & source) / len(claim) if claim else 0.0


def verify_grounding(text: str, source_texts: dict[int, str]) -> tuple[list[int], list[int]]:
    """Check each cited sentence against the source(s) it cites, lexically.

    Returns (confirmed, misattributed):
    - *confirmed*: S-numbers whose cited sentence is lexically supported by them.
    - *misattributed*: S-numbers whose cited sentence matches a *different* source
      far better than the cited one — a likely wrong citation number.

    Sentences whose overlap with the cited source is merely low (no other source
    matches either) are left unflagged: that is the inconclusive case a lexical
    signal cannot tell apart from a correct synonym paraphrase.

    No aggregate score is returned.  A ratio of confirmed/cited would be 0.0
    when all citations are valid synonym paraphrases, which is the inconclusive
    case, not an error — emitting 0.0 would itself be a false negative assertion.
    """
    src_bg = {n: _bigrams(t) for n, t in source_texts.items()}
    confirmed: set[int] = set()
    misattributed: set[int] = set()
    # Carry the most recent non-empty claim bigrams so that citation-only fragments
    # (produced by the (?<=\.)(?=\s) split, e.g. "Sentence. [S1]" → " [S1]") can
    # still be verified against the sentence they annotate.
    prev_claim: set[str] = set()
    for raw in _SENTENCE_SPLIT_RE.split(text):
        sentence = raw.strip()
        if not sentence:
            continue
        nums = [n for n in extract_citations(sentence) if n in source_texts]
        # NFKC-normalize before stripping brackets so that full-width citation brackets
        # ［Ｓ１］ (U+FF3B/U+FF3D) are also removed.  extract_citations already applies
        # NFKC internally; without this normalization, full-width brackets survive into
        # `bare`, producing spurious bigrams that inflate the claim denominator and
        # prevent prev_claim propagation for citation-only full-width fragments.
        bare = _BRACKET_RE.sub(" ", unicodedata.normalize("NFKC", sentence)).strip()
        if not nums:
            cand = _bigrams(bare)
            if cand:
                prev_claim = cand
            continue
        claim = _bigrams(bare)
        if not claim:
            # Citation-only fragment after sentence boundary split (e.g. " [S1]").
            # Re-use the preceding sentence's bigrams so the citation is still
            # verified rather than silently dropped.
            claim = prev_claim
        else:
            prev_claim = claim
        if not claim:
            continue
        for n in nums:
            overlap_n = _overlap(claim, src_bg[n])
            if overlap_n >= CONFIRM_MIN:
                confirmed.add(n)
            else:
                # A different source — including co-cited ones — may match far better,
                # indicating this specific S-number is wrong even if others in the same
                # sentence are correctly cited.
                best_other = max(
                    (_overlap(claim, src_bg[k]) for k in src_bg if k != n), default=0.0
                )
                if best_other >= CONFIRM_MIN and best_other - overlap_n >= MISMATCH_GAP:
                    misattributed.add(n)
            # otherwise inconclusive (possibly a valid paraphrase) — stay silent
    return sorted(confirmed), sorted(misattributed)


# Minimum non-whitespace character count in a sentence's citation-stripped body for
# it to count as a "claim" worth flagging. Filters trivial acknowledgments ("はい。",
# "そう。") without needing an LLM to classify sentence intent. Higher than the
# generic 2-char _bigrams() floor used elsewhere, which is too lenient for this
# purpose (a 3-char filler word already clears it).
_MIN_CLAIM_CHARS = 5


def uncited_sentences(text: str) -> list[str]:
    """Sentences that assert content with zero [S#] citations anywhere in them.

    verify_grounding() only ever looks at sentences that already carry a citation
    (checking whether *that* citation is well-grounded). A hallucinated or simply
    unsupported claim with no citation at all sails through untouched — this is
    the gap docs/product-review.md flagged as the top remaining priority item.

    The most common LLM citation placement is a *trailing* citation-only fragment
    after the sentence boundary split (e.g. "Sentence. [S1]" -> ["Sentence.", "[S1]"],
    the same pattern verify_grounding() resolves via prev_claim, v0.2.44). A sentence
    is only flagged once we've confirmed no such trailing citation resolves it —
    a sentence immediately followed by a citation-only fragment is NOT uncited.

    Trivial fragments (too short to carry a claim, e.g. "はい。"), sentences
    that explicitly say the fact is not in the sources (the *correct* response to
    missing information per the system prompt, not an unsupported assertion), and
    questions (a question asserts nothing — the faq/study_guide Studio kinds ask
    5-8 questions per output by design) are excluded so this stays a
    high-precision signal rather than flagging normal, honest "not in the
    source" disclaimers or well-formed FAQ/study-guide question lines.
    """
    out: list[str] = []
    pending: str | None = None  # most recent uncited sentence, awaiting a trailing citation
    for raw in _SENTENCE_SPLIT_RE.split(text):
        sentence = raw.strip()
        if not sentence:
            continue
        nums = extract_citations(sentence)
        bare = _BRACKET_RE.sub(" ", unicodedata.normalize("NFKC", sentence)).strip()
        has_claim = len(re.sub(r"\s+", "", bare)) >= _MIN_CLAIM_CHARS
        if nums and not has_claim:
            # Citation-only fragment (e.g. the "[S1]" tail of "Sentence. [S1]") —
            # resolves whatever sentence it trails; that sentence is not uncited.
            pending = None
            continue
        # Not a pure citation trailer: any still-pending sentence was never resolved
        # by a trailing citation, so it truly has no citation attached — flag it.
        if pending is not None:
            out.append(pending)
            pending = None
        if nums:
            continue  # this fragment carries its own citation — not uncited
        if not has_claim:
            continue  # too short/trivial to carry a claim worth flagging
        if any(marker in sentence for marker in _DISCLAIMER_MARKERS):
            continue  # explicit "not in source" — correct behavior, not a gap
        if sentence.endswith(("?", "？")):
            continue  # a question asserts nothing; the faq/study_guide kinds ask
            # 5-8 questions per output (studio.py prompts), and each becomes its
            # own citation-less sentence at this split boundary — flagging them
            # would violate this module's own "stay silent unless certain"
            # principle by systematically false-positiving every well-formed,
            # correctly-cited FAQ/study-guide output.
        pending = sentence  # wait to see if a trailing citation-only fragment resolves it
    if pending is not None:
        out.append(pending)
    return out


def make_report(
    text: str,
    source_titles: list[str],
    source_ids: list[int] | None = None,
    source_bodies: list[str] | None = None,
    *,
    check_uncited: bool = True,
) -> CitationReport:
    """Build the citation_report attached to every generated answer/output.

    check_uncited=False skips uncited_sentences() — used for degraded-mode text
    (qa._degraded_text), which prepends a system meta-message ("LLM endpoint
    unreachable...") that carries no citation but is not a content claim about
    the sources; flagging it as an unsupported assertion would be a false positive.
    """
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
        if len(source_ids) != n:
            raise ValueError(
                f"source_ids length {len(source_ids)} must match source_titles length {n}"
            )
        report["source_id_map"] = {f"S{i + 1}": sid for i, sid in enumerate(source_ids)}
    if source_bodies is not None:
        if len(source_bodies) != n:
            raise ValueError(
                f"source_bodies length {len(source_bodies)} must match source_titles length {n}"
            )
        confirmed, misattributed = verify_grounding(
            text, {i + 1: body for i, body in enumerate(source_bodies)}
        )
        report["confirmed"] = confirmed
        report["misattributed"] = misattributed
        # Each body is already bounded by the context token budget (~300–400 tokens
        # ≈ 1 200 chars max), so storing the full body is compact and safe.
        report["source_excerpts"] = {f"S{i + 1}": body for i, body in enumerate(source_bodies)}
    if n and check_uncited:
        uncited = uncited_sentences(text)
        if uncited:
            report["uncited"] = uncited
    return report
