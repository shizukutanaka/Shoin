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
5. Numeric-consistency check (`numeric_mismatches`): checks 2 and 3 compare
   wording, which misses the most common hallucination shape the citation
   literature actually documents (arXiv:2510.20303, ACL-industry CiteFix):
   a correctly-attributed sentence carrying a *fabricated* statistic. A claim
   citing [S1] that asserts a digit string S1 never contains is flagged — the
   highest-precision hallucination signal available without an NLI model.
6. Quote-mismatch check (`quote_mismatches`): a 「…」/"…" span cited to S_n
   but appearing verbatim in a *different* source is exact-string proof the
   number is wrong — folded into `misattributed` (v0.2.187).
7. Degeneration check (`degenerate_spans`): verbatim ≥3× repetition in the
   answer itself — the failure shape small local LLMs are prone to
   (v0.2.188).
8. Unit-consistency check (`unit_mismatches`): the numeric check asks only
   whether a digit string exists; a number present under a DIFFERENT unit
   ("100km" vs "100m", "100億円" vs "100万円") is the same magnitude of
   fabrication and invisible to it (v0.2.190).

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
# An answer citing fewer than this fraction of the sources it was given may be
# ignoring retrieved evidence — a signal the reader should know about, since the
# product's promise is that answers are grounded in *the user's* sources. Shared
# by every surface that reports coverage (Web UI badge, CLI report, Markdown
# export) so the three cannot drift apart on what counts as "low".
COVERAGE_LOW = 0.5

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

# Common English question-starter words. LLMs asked for "no decoration" often
# omit trailing "?" in list form; these words reliably identify questions.
_EN_QUESTION_STARTERS = frozenset(
    "what how why when where who which does is are was were will would could should can".split()
)


def looks_like_question(text: str) -> bool:
    """Heuristic "is this a question" detector.

    Single source of truth for a check needed in two places — uncited_sentences()
    below (a question asserts nothing, so it must not be flagged as an unsupported
    claim) and studio.py's suggest_questions() (extracting candidate questions from
    LLM output). v0.2.77 through v0.2.79 each independently discovered the other
    call site recognized a question pattern this one didn't (？/か/でしょう/ください,
    one at a time) — three successive partial fixes to two copies of the same
    heuristic drifting apart. Centralizing it here means the two can no longer
    diverge; both call sites now go through this one function.

    NFKC-normalizes first so full-width "？" is treated identically to ASCII "?".
    """
    norm = unicodedata.normalize("NFKC", text)
    if "?" in norm:
        return True
    base = norm.rstrip("。.!?")
    if base.endswith(("か", "ください", "でしょう")):
        return True
    # Strip a trailing contraction ("What's", "Who'd", "What'll") before the
    # lookup — the bare frozenset entries would otherwise never match "what's".
    first_word = norm.split()[0].lower().split("'")[0] if norm.split() else ""
    return first_word in _EN_QUESTION_STARTERS


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
    #   confirmed         -> S-numbers whose cited sentence is lexically supported
    #   misattributed     -> S-numbers whose cited sentence clearly belongs elsewhere
    #   numeric_mismatch  -> S-numbers whose cited claim asserts a number the
    #                        source never contains (present only when non-empty)
    #   quote_mismatch    -> S-numbers cited for a verbatim quote that lives in
    #                        a different source; also folded into `misattributed`
    #                        (present only when non-empty)
    #   unit_mismatch     -> S-numbers whose cited claim asserts a number the
    #                        source carries under an incompatible unit
    #                        (present only when non-empty)
    confirmed: NotRequired[list[int]]
    misattributed: NotRequired[list[int]]
    numeric_mismatch: NotRequired[list[int]]
    quote_mismatch: NotRequired[list[int]]
    unit_mismatch: NotRequired[list[int]]
    # True when the LLM was unreachable and the answer is search-only excerpts.
    # Absent on non-degraded responses and old persisted reports.
    degraded: NotRequired[bool]
    # Maps "S1" -> excerpt of the text actually retrieved as context for the answer.
    # Allows the UI to show the supporting passage immediately on seal-click without
    # an extra HTTP fetch. Absent on old persisted reports — consumers must guard.
    source_excerpts: NotRequired[dict[str, str]]
    # Maps "S1" -> section breadcrumb (heading path, title-prefix stripped) the
    # excerpt came from, e.g. "光合成のしくみ > 明反応". Lets the UI show WHICH section
    # a citation is grounded in, not just its text. Only present for S-numbers with
    # a non-empty section. Absent on old persisted reports — consumers must guard.
    source_contexts: NotRequired[dict[str, str]]
    # Maps "S1" -> the chunk ids whose text was actually placed in the prompt for
    # that source. Lets the UI mark those exact passages inside the full source
    # text, so a reader can verify the cited wording in its original position
    # instead of trusting a detached excerpt (visual source attribution).
    # Absent on old persisted reports — consumers must guard.
    source_chunk_ids: NotRequired[dict[str, list[int]]]
    # Sentences that assert content with zero [S#] citations anywhere in them —
    # invisible to verify_grounding(), which only checks already-cited sentences.
    # Present only when n_sources > 0 (nothing to cite against otherwise).
    # Absent on old persisted reports — consumers must guard.
    uncited: NotRequired[list[str]]
    # Snippets of verbatim repetition signalling an LLM degeneration loop —
    # answer-internal, so present whenever it fires (no sources needed).
    # Absent on old persisted reports — consumers must guard.
    degenerate: NotRequired[list[str]]


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


def _segment_claims(norm: str, valid: list[int]) -> dict[int, str]:
    """Attribute each citation to the clause-text that precedes it.

    A sentence carrying several citations makes several claims; comparing the
    WHOLE sentence against each cited source dilutes every one of them with
    the other clauses' wording. With ordinary Japanese clause joining
    ("…であり、…") that dilution is severe enough to push a correctly-cited
    source below CONFIRM_MIN *and* let a different co-cited source win by the
    MISMATCH_GAP margin — a false "wrong source number" accusation, which is
    exactly what this module's design forbids (v0.1.4: never accuse a correct
    answer). The citation markers are themselves the clause delimiters, so the
    split needs no NLI model or LLM: the text since the previous marker is what
    this marker cites. Sub-sentence attribution is where citation research is
    heading (arXiv:2509.20859); this is its dependency-free special case.

    Returns {} when the sentence has fewer than two citation positions — the
    whole-sentence comparison is already correct there and stays untouched.
    Shared by verify_grounding() (bigram overlap per clause) and
    numeric_mismatches() (digit strings per clause) so the two can never
    diverge on WHICH text a citation is held responsible for — the
    v0.2.77-79 duplicated-heuristic drift lesson.
    """
    spans = list(_BRACKET_RE.finditer(norm))
    cited_spans = [
        (m, [int(x) for x in _SNUM_RE.findall(m.group(1)) if int(x) in valid])
        for m in spans
    ]
    cited_spans = [(m, ns) for m, ns in cited_spans if ns]
    if len(cited_spans) < 2:
        return {}
    out: dict[int, str] = {}
    prev_end = 0
    for m, ns in cited_spans:
        seg = _BRACKET_RE.sub(" ", norm[prev_end : m.start()]).strip()
        prev_end = m.end()
        if not _bigrams(seg):
            continue  # adjacent markers ("[S1][S2]") — fall back to the sentence
        for n in ns:
            out[n] = seg
    return out


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
        # Sub-sentence attribution: when this sentence carries several citations,
        # each is judged against the clause it annotates rather than the whole
        # sentence (see _segment_claims). Empty dict → whole-sentence behavior.
        segments = _segment_claims(unicodedata.normalize("NFKC", sentence), nums)
        for n in nums:
            claim_n = _bigrams(segments[n]) if n in segments else claim
            overlap_n = _overlap(claim_n, src_bg[n])
            if overlap_n >= CONFIRM_MIN:
                confirmed.add(n)
            else:
                # A different source — including co-cited ones — may match far better,
                # indicating this specific S-number is wrong even if others in the same
                # sentence are correctly cited.
                # Compare rivals against the SAME clause, or the two sides of the
                # MISMATCH_GAP comparison would be measured on different units.
                best_other = max(
                    (_overlap(claim_n, src_bg[k]) for k in src_bg if k != n), default=0.0
                )
                if best_other >= CONFIRM_MIN and best_other - overlap_n >= MISMATCH_GAP:
                    misattributed.add(n)
            # otherwise inconclusive (possibly a valid paraphrase) — stay silent
    return sorted(confirmed), sorted(misattributed)


# --- numeric consistency (v0.2.184) ------------------------------------------

_NUM_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")
_NUM_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")


def _numbers(text: str) -> set[str]:
    """Significant digit strings in text (NFKC-folded): ≥2 digits or a decimal.

    Single bare digits are excluded — nearly every Japanese text contains one
    (第3版, 3月), so flagging them would be noise, not signal. Thousand
    separators are stripped before matching so "1,234" and "1234" compare equal.
    """
    t = _NUM_COMMA_RE.sub("", unicodedata.normalize("NFKC", text))
    return {
        m.group(0)
        for m in _NUM_TOKEN_RE.finditer(t)
        if "." in m.group(0) or len(m.group(0)) >= 2
    }


# Magnitude suffixes that turn "3.2万" into the value 32000 (v0.2.192).
# Japanese shorthand arithmetic is read constantly — a model legitimately
# expands "3.2万円" to "32000円" — so a digit-string presence check alone
# flags a correct restatement. "千万"/"百万" precede "万" in the alternation
# (ordered leftmost matching). Spelled-out numerals stay unchecked —
# ambiguous, per the module's silent principle.
_MAG_SUFFIX = {
    "千": 1_000,
    "万": 10_000,
    "百万": 1_000_000,
    "千万": 10_000_000,
    "億": 100_000_000,
    "兆": 1_000_000_000_000,
}
# Kanji numerals are NOT ambiguous — they follow positional notation
# (digit chars 一…九 plus place chars 十/百/千): "二十億" is unambiguously
# 20億, "百三万" is 103万, "一億二千万" is 120,000,000 (v0.2.195, replacing
# the v0.2.193 single-kanji-only approximation). Runs exclude the group
# separators 万/億/兆, which attach to the run as suffixes.
_KANJI_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_PLACE = {"十": 10, "百": 100, "千": 1_000}
_KANJI_RUN = r"[一二三四五六七八九十百千]+"
_NUM_PART = rf"(?:\d+(?:\.\d+)?|{_KANJI_RUN})"
_MAG_SUF = r"(?:千万|百万|億|万|千|兆)"
_MAG_NUM_RE = re.compile(rf"({_NUM_PART})(千万|百万|億|万|千|兆)")
# Chained magnitudes (v0.2.194): "1億2000万" — or kanji "一億二千万", or mixed
# "一億2000万" — = 120,000,000. A chain is ≥2 adjacent numeral+suffix pairs;
# the sum is added alongside the per-part values.
_MAG_CHAIN_RE = re.compile(rf"(?:{_NUM_PART}{_MAG_SUF}){{2,}}")
# Bare kanji-numeral runs (v0.2.195): "十二人" ↔ "12人". The lookahead keeps
# the run maximal — a run ending right before another numeral or suffix char
# is a component of a larger form, not a standalone value.
_KANJI_BARE_RE = re.compile(r"([一二三四五六七八九十百千]{2,})(?![一二三四五六七八九十百千万億兆])")

# Spelled-out English numerals (v0.2.196): "three million" ↔ "3000000",
# "twenty-one" ↔ "21" — English sources assert the same values in words and
# the digit-string presence check flagged the correct restatement. "and" is
# deliberately not a separator ("one and two" is a list, not a sum), so the
# BrE "three hundred and twenty" splits into two runs — a documented miss,
# not a wrong expansion.
_EN_SMALL = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_EN_BIG = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_EN_NUM_RE = re.compile(
    r"(?<![a-zA-Z])("
    + "|".join([*_EN_SMALL, "hundred", *_EN_BIG])
    + r")(?:[ -]("
    + "|".join([*_EN_SMALL, "hundred", *_EN_BIG])
    + r"))*(?![a-zA-Z])",
    re.IGNORECASE,
)

# 歩合 notation (v0.2.197): "6割3分" = 63%, "五割" = 50%, "2割5分8厘" = 25.8%.
# 割 = 10%, 分 = 1%, 厘 = 0.1% — deterministic, so a claim asserting the
# percent value no longer false-flags. 割 is required: bare "五分" reads as
# minutes or as half of "五分五分" (50-50 odds), never a percentage alone.
_WARI_RE = re.compile(rf"({_NUM_PART})割(?:({_NUM_PART})分)?(?:({_NUM_PART})厘)?")

# Unit-conversion equivalence (v0.2.198): a claim saying "180分" against a
# source writing "3時間" asserts the same duration — yet 180 never occurs in
# the source text, so the presence check false-flagged. The conversion is
# deterministic within each dimension family; months and years stay out
# (28–31 days / 365–366 days are genuinely ambiguous).
_SCALE_FAMILIES: list[dict[str, float]] = [
    {"秒": 1 / 60, "分": 1.0, "時間": 60.0, "日": 1440.0, "週": 10080.0, "週間": 10080.0},
    {
        "mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0,
        "ミリメートル": 0.001, "センチメートル": 0.01, "メートル": 1.0, "キロメートル": 1000.0,
    },
    {"g": 1.0, "kg": 1000.0, "グラム": 1.0, "キログラム": 1000.0},
    {"ml": 0.001, "cc": 0.001, "L": 1.0, "ミリリットル": 0.001, "リットル": 1.0},
]
_UNIT_SCALE = {u: (i, s) for i, fam in enumerate(_SCALE_FAMILIES) for u, s in fam.items()}
# Dedicated pair extractor — separate from _UNIT_NUM_RE because the unit check
# deliberately excludes 時/分/秒/日 (indistinguishable from date chains), but
# conversion pairs only ever SUPPRESS flags, and only same-family equality
# suppresses, so the ambiguity that justified exclusion cannot cause a miss.
_CONV_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)(週間|時間|日|週|秒|分|[a-zA-Zμµ°%]+|[ァ-ヶー]+)")


def _conv_values(text: str) -> set[tuple[int, float]]:
    """(family, canonical value) pairs extractable from text, including
    adjacent same-family sums — "1時間30分" yields (time, 60), (time, 30),
    and (time, 90)."""
    t = _NUM_COMMA_RE.sub("", unicodedata.normalize("NFKC", text))
    ms = list(_CONV_NUM_RE.finditer(t))
    vals: set[tuple[int, float]] = set()
    for i, m in enumerate(ms):
        ent = _UNIT_SCALE.get(m.group(2))
        if ent is None:
            continue
        fam, sc = ent
        acc = float(m.group(1)) * sc
        vals.add((fam, acc))
        for j in range(i + 1, len(ms)):
            nxt = ms[j]
            ent2 = _UNIT_SCALE.get(nxt.group(2))
            if ent2 is None or ent2[0] != fam or len(t[ms[j - 1].end():nxt.start()]) > 2:
                break
            acc += float(nxt.group(1)) * ent2[1]
            vals.add((fam, acc))
    return vals


def _en_value(run: str) -> int | None:
    """Value of a spelled-out English numeral run, or None when ambiguous.

    Accumulates small numbers, multiplies by hundred/thousand/million/billion:
    "three hundred twenty five" → 325, "two million" → 2,000,000. An empty
    local reads as one ("a hundred" → 100, "million" → 1,000,000).
    """
    total = 0
    local = 0
    used = False
    for tok in re.split(r"[ -]+", run.lower()):
        if tok in _EN_SMALL:
            local += _EN_SMALL[tok]
        elif tok == "hundred":
            local = (local or 1) * 100
        else:
            total += (local or 1) * _EN_BIG[tok]
            local = 0
        used = True
    return total + local if used else None


def _kanji_value(run: str) -> int | None:
    """Positional value of a kanji-numeral run, or None when ambiguous.

    Digits (一…九) apply to the place char (十/百/千) that follows them, or
    add to the running total at the end: "百三" → 100+3, "二十" → 2×10.
    A pure digit run like "二三" ("a few") carries no place char and is a
    counting sequence, not a numeral — inconclusive → None.
    """
    total = 0
    digit = 0
    seen_place = False
    for ch in run:
        if ch in _KANJI_DIGIT:
            if digit:
                return None  # consecutive digits ("一二三") — not a numeral
            digit = _KANJI_DIGIT[ch]
        else:
            total += (digit or 1) * _KANJI_PLACE[ch]
            digit = 0
            seen_place = True
    if not seen_place:
        return digit if len(run) == 1 else None
    return total + digit


def _part_value(part: str) -> float | None:
    if part[0].isdigit():
        return float(part)
    v = _kanji_value(part)
    return float(v) if v is not None else None


def _numbers_expanded(text: str) -> set[str]:
    """_numbers() plus canonical values for magnitude-suffixed shorthand.

    A number carrying a 千/万/百万/千万/億/兆 suffix is represented by its
    expanded value INSTEAD of the raw digits: "3.2万" → {"32000"}. The raw
    string is removed because the written digits literally do not occur in a
    source that spelled the value out ("32000"), and keeping it would flag a
    correct restatement. Kanji numerals (一万, 十二万, 一億二千万) expand
    additively — no digit string exists to remove. Only integral expansions
    are added (non-integral values like 1.2345万 have no canonical spelling —
    inconclusive).
    """
    t = _NUM_COMMA_RE.sub("", unicodedata.normalize("NFKC", text))
    nums = _numbers(t)
    suffixed: set[str] = set()
    # Numeral+suffix pairs INSIDE a chain are components, not asserted values:
    # "1億2000万" asserts 120,000,000 — keeping "1億"→1e8 and "2000万"→2e7 as
    # separate members would flag a claim spelling the summed value out.
    chain_spans = [m.span() for m in _MAG_CHAIN_RE.finditer(t)]
    for m in _MAG_NUM_RE.finditer(t):
        part, suf = m.group(1), m.group(2)
        if part[0].isdigit():
            suffixed.add(part)
        if any(cs <= m.start() < ce for cs, ce in chain_spans):
            continue
        pv = _part_value(part)
        if pv is None:
            continue
        v = pv * _MAG_SUFFIX[suf]
        r = round(v)
        if abs(v - r) < 1e-6:
            nums.add(str(r))
    for m in _MAG_CHAIN_RE.finditer(t):
        total = 0.0
        for p in _MAG_NUM_RE.finditer(m.group(0)):
            pv = _part_value(p.group(1))
            if pv is None:
                break
            total += pv * _MAG_SUFFIX[p.group(2)]
        else:
            r = round(total)
            if abs(total - r) < 1e-6:
                nums.add(str(r))
    for m in _KANJI_BARE_RE.finditer(t):
        kv = _kanji_value(m.group(1))
        if kv is not None and kv > 0:
            nums.add(str(kv))
    for m in _EN_NUM_RE.finditer(t):
        ev = _en_value(m.group(0))
        if ev is not None and ev > 0:
            nums.add(str(ev))
    for m in _WARI_RE.finditer(t):
        wari = _part_value(m.group(1))
        fun = _part_value(m.group(2)) if m.group(2) else 0.0
        rin = _part_value(m.group(3)) if m.group(3) else 0.0
        if wari is None or fun is None or rin is None:
            continue
        v = wari * 10 + fun + rin * 0.1
        # >100% is not a real 歩合 value ("十二割" is nonsense) — leave
        # inconclusive text unchecked rather than registering a phantom.
        if 0 < v <= 100:
            nums.add(str(round(v)) if abs(v - round(v)) < 1e-6 else str(round(v, 1)))
    return nums - suffixed


def numeric_mismatches(text: str, source_texts: dict[int, str]) -> list[int]:
    """S-numbers whose cited claim asserts a number absent from that source.

    verify_grounding() compares wording, which structurally misses the most
    common hallucination shape the citation literature documents — a correctly
    attributed sentence carrying a fabricated statistic (arXiv:2510.20303's
    audit of real RAG answers found numeric errors dominate the citation-failure
    taxonomy; ACL-industry CiteFix ships the same check). A claim citing [S1]
    that asserts a digit string S1 never contains is flagged.

    Same sentence- and clause-level attribution as verify_grounding (shared
    _segment_claims): each citation is judged against the clause it annotates,
    and a trailing "[S1]" fragment inherits the previous sentence's claim.

    Deliberately asymmetric like the bigram checks: a claim number FOUND in the
    source is no proof of correctness (rounding, derived arithmetic), and a
    spelled-out number (three, 三) is never checked (ambiguous). Only an absent
    digit string asserts anything — the module's "stay silent when inconclusive"
    principle applied to numerals.
    """
    src_norm = {
        n: _NUM_COMMA_RE.sub("", unicodedata.normalize("NFKC", t))
        for n, t in source_texts.items()
    }
    src_nums = {n: _numbers_expanded(t) for n, t in src_norm.items()}
    src_conv = {n: _conv_values(t) for n, t in src_norm.items()}
    out: set[int] = set()
    prev_claim = ""
    for raw in _SENTENCE_SPLIT_RE.split(text):
        sentence = raw.strip()
        if not sentence:
            continue
        nums = [n for n in extract_citations(sentence) if n in source_texts]
        bare = _BRACKET_RE.sub(" ", unicodedata.normalize("NFKC", sentence)).strip()
        if not nums:
            if bare:
                prev_claim = bare
            continue
        claim_text = bare or prev_claim
        if bare:
            prev_claim = bare
        if not claim_text:
            continue
        segments = _segment_claims(unicodedata.normalize("NFKC", sentence), nums)
        for n in nums:
            claim_n = segments.get(n, claim_text)
            # Exact set membership catches expanded magnitudes (32000 ↔ 3.2万);
            # the substring fallback preserves v0.2.184's rounding tolerance
            # (claim "63" stays silent inside source "63.5%"); the conversion
            # check suppresses only when the claim's OWN unit pairs with the
            # same canonical value in the same family — "300円" against a
            # source saying "5時間" (→300min) stays flagged because 円 is
            # not a time unit.
            conv_by_num: dict[str, set[tuple[int, float]]] = {}
            for m in _CONV_NUM_RE.finditer(claim_n):
                ent = _UNIT_SCALE.get(m.group(2))
                if ent is not None:
                    conv_by_num.setdefault(m.group(1), set()).add((ent[0], float(m.group(1)) * ent[1]))
            if any(
                num not in src_nums[n]
                and num not in src_norm[n]
                and conv_by_num.get(num, set()).isdisjoint(src_conv[n])
                for num in _numbers_expanded(claim_n)
            ):
                out.add(n)
    return sorted(out)


# --- unit consistency (v0.2.190) ----------------------------------------------
# A significant number followed by a unit suffix. Three bounded suffix classes:
# - ASCII units/symbols: kg, km, GB, kWh, ppm, %, °C, μg — any letter run
#   (% and ° included since 25%, 25°C read as single tokens)
# - katakana units: キロ, メートル, ドル, パーセント — a >=1-char run
# - a fixed counter-kanji set (persons/items/machines/currency/orders):
#   time counters (年月日時分秒) are deliberately EXCLUDED — date chains like
#   "2024年3月" make a bare 年 ambiguous between "year count" and "date part",
#   so checking it would be noise, not signal.
_UNIT_ASCII = r"[a-zA-Zμµ°%]+"
_UNIT_KANA = r"[ァ-ヶー]+"
_UNIT_KANJI = "人件台枚頭本冊回個歳才名位番号階話巻章節項目園校社国店軒棟戸席便着足組粒錠滴羽匹杯両円倍億万千"
_UNIT_NUM_RE = re.compile(rf"(\d+(?:\.\d+)?)({_UNIT_ASCII}|{_UNIT_KANA}|[{_UNIT_KANJI}]+)")

# Same-unit spellings across scripts (v0.2.191). NFKC already folds the
# composed forms (㎞→km, ℓ→l, ％→%), so what remains are genuine aliases:
# katakana spellings of SI/imperial units, and counter kanji that name the
# same thing (歳/才, 名/人, 軒/棟/戸). ASCII units keep their
# case — MW vs mW and B vs b are real distinctions, so no case-folding.
# Directional on purpose: ambiguous colloquial tokens point at ALL their
# possible readings (キロ→{km,kg}, ミリ→{mm,ml}) while the precise readings
# never list each other — "100km" vs "100kg" still flags. The check is
# `a ∈ aliases(b) or b ∈ aliases(a)`, so a bare ambiguous token can only
# under-flag, never over-flag.
_UNIT_ALIASES: dict[str, frozenset[str]] = {
    "km": frozenset({"キロメートル"}),
    "キロメートル": frozenset({"km"}),
    "キロ": frozenset({"km", "kg", "キロメートル", "キログラム"}),
    "m": frozenset({"メートル"}),
    "メートル": frozenset({"m"}),
    "cm": frozenset({"センチ", "センチメートル"}),
    "センチ": frozenset({"cm"}),
    "センチメートル": frozenset({"cm"}),
    "mm": frozenset({"ミリメートル"}),
    "ミリメートル": frozenset({"mm"}),
    "ミリ": frozenset({"mm", "ml", "ミリメートル", "ミリリットル"}),
    "ml": frozenset({"ミリリットル"}),
    "ミリリットル": frozenset({"ml"}),
    "kg": frozenset({"キログラム"}),
    "キログラム": frozenset({"kg"}),
    "g": frozenset({"グラム"}),
    "グラム": frozenset({"g"}),
    "mg": frozenset({"ミリグラム"}),
    "ミリグラム": frozenset({"mg"}),
    "t": frozenset({"トン"}),
    "トン": frozenset({"t"}),
    "l": frozenset({"リットル"}),
    "リットル": frozenset({"l"}),
    "%": frozenset({"パーセント"}),
    "パーセント": frozenset({"%"}),
    "$": frozenset({"ドル"}),
    "ドル": frozenset({"$"}),
    "€": frozenset({"ユーロ"}),
    "ユーロ": frozenset({"€"}),
    "lb": frozenset({"ポンド"}),
    "ポンド": frozenset({"lb"}),
    "W": frozenset({"ワット"}),
    "ワット": frozenset({"W"}),
    "kW": frozenset({"キロワット"}),
    "キロワット": frozenset({"kW"}),
    "V": frozenset({"ボルト"}),
    "ボルト": frozenset({"V"}),
    "A": frozenset({"アンペア"}),
    "アンペア": frozenset({"A"}),
    "Hz": frozenset({"ヘルツ"}),
    "ヘルツ": frozenset({"Hz"}),
    "kHz": frozenset({"キロヘルツ"}),
    "キロヘルツ": frozenset({"kHz"}),
    "MHz": frozenset({"メガヘルツ"}),
    "メガヘルツ": frozenset({"MHz"}),
    "GHz": frozenset({"ギガヘルツ"}),
    "ギガヘルツ": frozenset({"GHz"}),
    "B": frozenset({"バイト"}),
    "バイト": frozenset({"B"}),
    "KB": frozenset({"キロバイト"}),
    "キロバイト": frozenset({"KB"}),
    "MB": frozenset({"メガバイト"}),
    "メガバイト": frozenset({"MB"}),
    "GB": frozenset({"ギガバイト"}),
    "ギガバイト": frozenset({"GB"}),
    "TB": frozenset({"テラバイト"}),
    "テラバイト": frozenset({"TB"}),
    "hp": frozenset({"馬力"}),
    "馬力": frozenset({"hp"}),
    "ha": frozenset({"ヘクタール"}),
    "ヘクタール": frozenset({"ha"}),
    # counter-kanji equivalents: same count, different spelling. Deliberately
    # excludes 本/冊 (long objects vs bound volumes — different semantics) and
    # 番/位 (serial position vs rank — can differ); only pairs that mean the
    # same count for every referent qualify.
    "歳": frozenset({"才"}),
    "才": frozenset({"歳"}),
    "名": frozenset({"人"}),
    "人": frozenset({"名"}),
    "軒": frozenset({"棟", "戸"}),
    "棟": frozenset({"軒", "戸"}),
    "戸": frozenset({"軒", "棟"}),
}


def _unit_pairs(text: str) -> list[tuple[str, str]]:
    """(number, unit) pairs for significant numbers (same threshold as _numbers)."""
    t = _NUM_COMMA_RE.sub("", unicodedata.normalize("NFKC", text))
    return [
        (m.group(1), m.group(2))
        for m in _UNIT_NUM_RE.finditer(t)
        if "." in m.group(1) or len(m.group(1)) >= 2
    ]


def _units_compat(a: str, b: str) -> bool:
    """Same unit, one extending the other ('1億' vs '1億円' are consistent
    elaboration, not a swap), or a known cross-script alias (km↔キロメートル,
    歳↔才) via _UNIT_ALIASES."""
    return (
        a == b
        or a.startswith(b)
        or b.startswith(a)
        or a in _UNIT_ALIASES.get(b, frozenset())
        or b in _UNIT_ALIASES.get(a, frozenset())
    )


def unit_mismatches(text: str, source_texts: dict[int, str]) -> list[int]:
    """S-numbers whose cited claim asserts a number with a DIFFERENT unit.

    numeric_mismatches() only asks whether a digit string exists in the
    source; a number that IS present but carries another unit is the same
    magnitude of fabrication and structurally invisible to it — '100km' vs
    '100m', '25%' vs '25ppm', '100億円' vs '100万円' all pass the
    presence check while being wrong. Here the cited claim's (number, unit)
    pairs are compared against the units the source attaches to that same
    number.

    Deliberately asymmetric like the other checks: only fires when the
    source attaches a *different, incompatible* unit to the same number —
    a source occurrence with no unit is inconclusive (the unit may live in
    the surrounding text), a claim number absent from the source is
    numeric_mismatches()' job, and prefix-extending units are elaboration.
    Same sentence- and clause-level attribution via _segment_claims.
    """
    src_norm = {
        n: _NUM_COMMA_RE.sub("", unicodedata.normalize("NFKC", t))
        for n, t in source_texts.items()
    }
    src_units = {n: _unit_pairs(t) for n, t in src_norm.items()}
    out: set[int] = set()
    prev_claim = ""
    for raw in _SENTENCE_SPLIT_RE.split(text):
        sentence = raw.strip()
        if not sentence:
            continue
        nums = [n for n in extract_citations(sentence) if n in source_texts]
        bare = _BRACKET_RE.sub(" ", unicodedata.normalize("NFKC", sentence)).strip()
        if not nums:
            if bare:
                prev_claim = bare
            continue
        claim_text = bare or prev_claim
        if bare:
            prev_claim = bare
        if not claim_text:
            continue
        segments = _segment_claims(unicodedata.normalize("NFKC", sentence), nums)
        for n in nums:
            claim_n = segments.get(n, claim_text)
            for num, unit in _unit_pairs(claim_n):
                if num not in src_norm[n]:
                    continue  # absent number — numeric_mismatches()' signal
                units_n = [v for num2, v in src_units[n] if num2 == num]
                if units_n and not any(_units_compat(unit, v) for v in units_n):
                    out.add(n)
                    break
    return sorted(out)


# --- verbatim-quote consistency (v0.2.187) ------------------------------------

_QUOTE_RE = re.compile(r"「([^」]+)」|\"([^\"]+)\"")
# Minimum non-whitespace chars inside a quote marker for it to count as a
# verbatim-quotation claim. Shorter 「…」 spans are concept names/emphasis
# (「重要な点」), which never assert "this wording appears in the source".
_QUOTE_MIN = 8
# Near-verbatim (doctored-quote) bounds: a span ≥12 chars sharing ≥60% of its
# bigrams with some source while matching none verbatim derives from that
# source but asserts wording it never wrote — an error whether the overlap is
# with the cited source (paraphrase wearing quotes) or a different one
# (near-verbatim misattribution). Below 12 chars a topic-term emphasis could
# coincidentally share 60% of its bigrams; below 0.6 the text could be a
# legitimately loose quote-adjacent paraphrase — inconclusive, stays silent.
_DOCTORED_MIN_LEN = 12
_DOCTORED_MIN_OVERLAP = 0.6


def _quote_spans(text: str) -> list[str]:
    """Quoted spans ≥ _QUOTE_MIN, normalised for verbatim containment checks.

    Only 「…」 and "…" count: 『…』 marks work titles (《書名》), and ASCII
    apostrophes are too ambiguous to be quotation marks.
    """
    out: list[str] = []
    for m in _QUOTE_RE.finditer(text):
        q = m.group(1) or m.group(2)
        q = re.sub(r"\s+", "", unicodedata.normalize("NFKC", q)).lower()
        if len(q) >= _QUOTE_MIN:
            out.append(q)
    return out


def quote_mismatches(text: str, source_texts: dict[int, str]) -> list[int]:
    """S-numbers cited for a verbatim quote that lives in a *different* source.

    The same evidence shape as verify_grounding()'s misattributed flag, but
    on the exact-string signal only a direct quotation provides: a 「…」/"…"
    span whose characters appear verbatim in source m yet not in cited source
    n is unambiguous proof that n is the wrong number for that claim — no
    lexical-overlap margin needed. Quoted fabrication is a top entry in the
    citation-failure taxonomy (arXiv:2510.20303), and bigram checks can miss
    it entirely because a paraphrased surrounding sentence still scores
    overlap with the wrongly-cited source.

    A second, near-verbatim shape (v0.2.199): a span ≥ _DOCTORED_MIN_LEN
    whose bigram overlap with some source exceeds _DOCTORED_MIN_OVERLAP
    while matching NO source verbatim is a doctored quote — the assertive
    「…」 claims exact wording the source never wrote, yet the text clearly
    derives from that source (paraphrase wearing quotes, or near-verbatim
    of a different source — both are citation errors).

    Deliberately asymmetric like the other checks: a span found in NO source
    at any meaningful overlap could be fabricated, but it could equally be
    emphasis-「」 — inconclusive, so it stays silent. Same sentence- and
    clause-level attribution as verify_grounding()/numeric_mismatches() via
    the shared _segment_claims.
    """
    src_norm = {
        n: re.sub(r"\s+", "", unicodedata.normalize("NFKC", t)).lower()
        for n, t in source_texts.items()
    }
    src_bg = {n: _bigrams(t) for n, t in src_norm.items()}
    out: set[int] = set()
    prev_claim = ""
    for raw in _SENTENCE_SPLIT_RE.split(text):
        sentence = raw.strip()
        if not sentence:
            continue
        nums = [n for n in extract_citations(sentence) if n in source_texts]
        bare = _BRACKET_RE.sub(" ", unicodedata.normalize("NFKC", sentence)).strip()
        if not nums:
            if bare:
                prev_claim = bare
            continue
        claim_text = bare or prev_claim
        if bare:
            prev_claim = bare
        if not claim_text:
            continue
        segments = _segment_claims(unicodedata.normalize("NFKC", sentence), nums)
        for n in nums:
            claim_n = segments.get(n, claim_text)
            for q in _quote_spans(claim_n):
                if q in src_norm[n]:
                    continue
                if any(q in src_norm[k] for k in src_norm if k != n) or (
                    len(q) >= _DOCTORED_MIN_LEN
                    and any(
                        _overlap(_bigrams(q), bg) >= _DOCTORED_MIN_OVERLAP
                        for bg in src_bg.values()
                    )
                ):
                    out.add(n)
                    break
    return sorted(out)


# --- generation-degeneration signals (v0.2.188) --------------------------------

# Minimum normalised length of a repeated unit for it to count as degeneration:
# short phrases recur legitimately ("である。", "for example"), while a ≥6-char
# span repeating ≥3 times consecutively — or a ≥10-char sentence appearing ≥3
# times in one answer — is the classic repeat-loop failure shape of small LLMs
# (the reason llama.cpp/Ollama ship repeat-penalty sampling guards).
_DEGEN_SPAN_MIN = 6
_DEGEN_SENT_MIN = 10
_DEGEN_REPEAT = 3
_DEGEN_SNIP = 40
_DEGEN_SPAN_RE = re.compile(rf"(.{{{_DEGEN_SPAN_MIN},}}?)\1{{{_DEGEN_REPEAT - 1},}}")


def degenerate_spans(text: str) -> list[str]:
    """Snippets of repeated content signalling an LLM degeneration loop.

    Two orthogonal shapes, both mechanical and dependency-free:
    - the same normalised sentence (≥10 chars) appearing ≥3 times in the
      answer — the "parroting" loop;
    - any ≥6-char span repeating ≥3 times *consecutively* anywhere in the
      text — the "stuck tail" loop sampling guards exist to prevent.

    Deliberately asymmetric like the other checks: nothing is flagged below
    these bounds — parallel structures ("Aである。Bである。") and honest
    emphasis repeat *differently*, never verbatim-normed ≥3 times.
    """
    low = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).lower()
    out: set[str] = set()
    counts: dict[str, int] = {}
    for raw in _SENTENCE_SPLIT_RE.split(text):
        s = re.sub(r"\s+", "", unicodedata.normalize("NFKC", raw)).lower()
        if len(s) >= _DEGEN_SENT_MIN:
            counts[s] = counts.get(s, 0) + 1
    for s, c in counts.items():
        if c >= _DEGEN_REPEAT:
            out.add(s[:_DEGEN_SNIP])
    for m in _DEGEN_SPAN_RE.finditer(low):
        out.add(m.group(1)[:_DEGEN_SNIP])
    return sorted(out)


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
        # A question asserts nothing; the faq/study_guide kinds ask 5-8 questions
        # per output (studio.py prompts), and each becomes its own citation-less
        # sentence at this split boundary — flagging them would violate this
        # module's own "stay silent unless certain" principle by systematically
        # false-positiving every well-formed, correctly-cited FAQ/study-guide output.
        if looks_like_question(sentence):
            continue
        pending = sentence  # wait to see if a trailing citation-only fragment resolves it
    if pending is not None:
        out.append(pending)
    return out


def make_report(
    text: str,
    source_titles: list[str],
    source_ids: list[int] | None = None,
    source_bodies: list[str] | None = None,
    source_contexts: list[str] | None = None,
    source_chunk_ids: list[list[int]] | None = None,
    *,
    check_uncited: bool = True,
) -> CitationReport:
    """Build the citation_report attached to every generated answer/output.

    check_uncited=False skips uncited_sentences() — used for degraded-mode text
    (qa._degraded_text), which prepends a system meta-message ("LLM endpoint
    unreachable...") that carries no citation but is not a content claim about
    the sources; flagging it as an unsupported assertion would be a false positive.

    source_contexts (S1..Sn order, "" where a source has no section) surfaces the
    heading path each citation is grounded in; only non-empty entries are stored.
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
        num_mis = numeric_mismatches(text, {i + 1: body for i, body in enumerate(source_bodies)})
        if num_mis:
            report["numeric_mismatch"] = num_mis
        quote_mis = quote_mismatches(text, {i + 1: body for i, body in enumerate(source_bodies)})
        if quote_mis:
            # Same evidence shape as misattributed — the claim's content lives
            # in a different source — so it merges into that flag, while
            # quote_mismatch records which numbers were flagged via quotes.
            report["misattributed"] = sorted(set(misattributed) | set(quote_mis))
            report["quote_mismatch"] = quote_mis
        unit_mis = unit_mismatches(text, {i + 1: body for i, body in enumerate(source_bodies)})
        if unit_mis:
            report["unit_mismatch"] = unit_mis
        # Each body is already bounded by the context token budget (~300–400 tokens
        # ≈ 1 200 chars max), so storing the full body is compact and safe.
        report["source_excerpts"] = {f"S{i + 1}": body for i, body in enumerate(source_bodies)}
    if source_contexts is not None:
        if len(source_contexts) != n:
            raise ValueError(
                f"source_contexts length {len(source_contexts)} must match"
                f" source_titles length {n}"
            )
        sc = {f"S{i + 1}": ctx for i, ctx in enumerate(source_contexts) if ctx}
        if sc:
            report["source_contexts"] = sc
    if source_chunk_ids is not None:
        if len(source_chunk_ids) != n:
            raise ValueError(
                f"source_chunk_ids length {len(source_chunk_ids)} must match"
                f" source_titles length {n}"
            )
        sci = {f"S{i + 1}": ids for i, ids in enumerate(source_chunk_ids) if ids}
        if sci:
            report["source_chunk_ids"] = sci
    if n and check_uncited:
        uncited = uncited_sentences(text)
        if uncited:
            report["uncited"] = uncited
    deg = degenerate_spans(text)
    if deg:
        report["degenerate"] = deg
    return report
