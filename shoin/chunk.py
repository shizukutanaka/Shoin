"""Chunking: heading-aware splitting with CJK-aware token estimation.

Token estimate (no external tokenizer): 1 token per CJK character plus 1 token
per non-CJK word-ish run. Conservative enough for budget control on <=8B models.
"""

from __future__ import annotations

import re

from .config import CHUNK_OVERLAP, CHUNK_TOKENS

_CJK_RANGES = (
    (0x0E00, 0x0E7F),    # Thai
    (0x0E80, 0x0EFF),    # Lao
    (0x1000, 0x109F),    # Myanmar
    (0x1780, 0x17FF),    # Khmer
    (0x3000, 0x303F),    # CJK symbols and punctuation (。、　〆々 etc.)
    (0x3040, 0x30FF),    # hiragana + katakana
    (0x3400, 0x4DBF),    # CJK ext A
    (0x4E00, 0x9FFF),    # CJK unified ideographs
    (0xF900, 0xFAFF),    # CJK compat
    (0xFF10, 0xFF19),    # fullwidth digits ０-９
    (0xFF21, 0xFF3A),    # fullwidth Latin uppercase Ａ-Ｚ
    (0xFF41, 0xFF5A),    # fullwidth Latin lowercase ａ-ｚ
    (0xFF66, 0xFF9D),    # halfwidth katakana
    (0xAC00, 0xD7A3),    # Hangul syllables
    (0x20000, 0x2A6DF),  # CJK ext B (supplementary plane — rare/historical chars)
    (0x2A700, 0x2CEAF),  # CJK ext C/D/E/F
    (0x2CEB0, 0x2EBEF),  # CJK ext G/H
)

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_HEADING_RE = re.compile(r"^#{1,6}\s")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。．！？!?\n；])|(?<=\.)(?=\s)")

# Words/identifiers up to this length cost a flat 1 token (CLAUDE.md's documented
# "ASCII words: 1 token per word" model — real natural-language words and typical
# identifiers rarely exceed this). An unbroken alphanumeric run LONGER than this
# (a base64 data: URI, a long hex hash, minified/obfuscated code with no spaces)
# is weighted at ~4 chars/token beyond the threshold instead of still costing a
# flat 1 token regardless of length — without this, estimate_tokens() undercounts
# a 200,000-character run to a single-digit token count, silently defeating both
# split_text()'s chunk-size cap and build_context()'s per-source token budget.
_LONG_RUN_THRESHOLD = 40


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _is_word_char(ch: str) -> bool:
    """True for exactly the characters _WORD_RE ([A-Za-z0-9_]) matches.

    _tail() and _truncate_tokens() scan character-by-character to detect word
    runs and must agree with _WORD_RE (the source of truth estimate_tokens()
    uses) on run boundaries. Python's str.isalnum() is Unicode-wide — true
    for Cyrillic/Greek/accented-Latin/etc. — so using it directly let those
    scripts merge into a single run that _WORD_RE would split at each
    non-ASCII letter, undercounting the true token cost and letting more
    text through than the caller's limit allows.
    """
    return ch.isascii() and (ch.isalnum() or ch == "_")


def _run_token_cost(n: int) -> int:
    """Token cost of a single word-ish run of length *n* (see _LONG_RUN_THRESHOLD)."""
    if n <= _LONG_RUN_THRESHOLD:
        return 1
    return 1 + (n - _LONG_RUN_THRESHOLD + 3) // 4


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if is_cjk(ch))
    words = sum(_run_token_cost(len(m)) for m in _WORD_RE.findall(text))
    return cjk + words


def _blocks(text: str) -> list[str]:
    """Split into blocks at markdown headings and blank lines."""
    blocks: list[str] = []
    buf: list[str] = []
    for line in text.splitlines():
        if _HEADING_RE.match(line):
            if buf:
                blocks.append("\n".join(buf).strip())
                buf = []
            buf.append(line)
        elif not line.strip():
            if buf:
                blocks.append("\n".join(buf).strip())
                buf = []
        else:
            buf.append(line)
    if buf:
        blocks.append("\n".join(buf).strip())
    return [b for b in blocks if b]


def _hard_split(block: str, limit: int) -> list[str]:
    """Split an oversize block by sentences, then by char windows as last resort."""
    parts: list[str] = []
    buf = ""
    for sent in _SENTENCE_SPLIT_RE.split(block):
        if not sent:
            continue
        if buf and estimate_tokens(buf + sent) > limit:
            parts.append(buf)
            buf = sent
        else:
            buf += sent
    if buf:
        parts.append(buf)
    out: list[str] = []
    for p in parts:
        tok = estimate_tokens(p)
        if tok > limit:
            # Character-window fallback for pathological unbroken text.
            # Convert the token budget to a character budget using this text's
            # own token density (CJK ≈ 1 char/token; ASCII ≈ 5 chars/token).
            # Using limit directly as a char index (the old code) produced chunks
            # that were ~5× too small for ASCII text.
            chars_per_token = len(p) / tok
            window = max(int(limit * chars_per_token), 1)
            for i in range(0, len(p), window):
                out.append(p[i : i + window])
        elif tok == 0 and len(p) > limit * 5:
            # Zero-token text (Arabic, Hebrew, Cyrillic, pure punctuation) escapes
            # estimate_tokens(); a pathologically long block (> limit*5 chars) must
            # still be split.  Use limit*5 chars as a conservative character budget
            # (matches ~5 chars/token ASCII density as an upper bound).
            window = max(limit * 5, 1)
            for i in range(0, len(p), window):
                out.append(p[i : i + window])
        else:
            out.append(p)
    return [p.strip() for p in out if p.strip()]


def _tail(text: str, tokens: int) -> str:
    """Return a suffix of *text* containing roughly *tokens* tokens."""
    if tokens <= 0:
        return ""
    acc = 0
    run_len = 0
    run_credited = False  # base 1-token cost of the current run already counted
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if is_cjk(ch):
            if run_len and not run_credited:
                # An alnum run was interrupted by this CJK character (common in
                # Japanese text with no space before an ASCII model/section
                # number, e.g. "型番ABC123456") — credit the run's base token
                # cost here, the same way the punctuation/space branch below
                # does, so it isn't silently dropped from the count.
                acc += 1
                if acc >= tokens:
                    return text[i + 1 :].lstrip()
            run_len = 0
            run_credited = False
            acc += 1
            if acc >= tokens:
                return text[i:].lstrip()
        elif _is_word_char(ch):
            run_len += 1
            # A normal-length word/identifier is only credited once fully
            # scanned (at its left boundary, below) so a short word is never
            # cut mid-word. Once a run proves "long" (run_len exceeds the
            # threshold), its base cost is locked in regardless of where it
            # eventually ends, so it's credited immediately here instead of
            # waiting for a boundary that a pathologically long run (base64
            # blob, long hash) may never reach before *tokens* is satisfied —
            # deferring it in that case would silently drop the base cost.
            # Beyond that, interim credits every ~4 chars keep such a run
            # bounded instead of pulling the whole thing in regardless of
            # *tokens*.
            if run_len == _LONG_RUN_THRESHOLD + 1:
                run_credited = True
                # Both the base cost (locked in the moment the run proves
                # "long") and the first interim credit for crossing the
                # threshold land on this same character — matching
                # _run_token_cost()'s closed form (1 + ceil((n-40)/4), whose
                # ceil term's first unit is also earned at n=41).
                acc += 2
                if acc >= tokens:
                    return text[i:].lstrip()
            elif run_len > _LONG_RUN_THRESHOLD and (run_len - _LONG_RUN_THRESHOLD) % 4 == 1:
                acc += 1
                if acc >= tokens:
                    return text[i:].lstrip()
        else:
            if run_len and not run_credited:
                acc += 1  # word boundary crossed: credit the run that just ended
                if acc >= tokens:
                    return text[i + 1 :].lstrip()
            run_len = 0
            run_credited = False
    return text


def split_text(
    text: str,
    chunk_tokens: int = CHUNK_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP,
) -> list[str]:
    """Split *text* into chunks of ~chunk_tokens with overlap between chunks."""
    pieces: list[str] = []
    for block in _blocks(text):
        if estimate_tokens(block) > chunk_tokens:
            pieces.extend(_hard_split(block, chunk_tokens))
        else:
            pieces.append(block)

    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        candidate = f"{buf}\n\n{piece}" if buf else piece
        if buf and estimate_tokens(candidate) > chunk_tokens:
            chunks.append(buf)
            buf = _tail(buf, overlap_tokens)
            buf = f"{buf}\n\n{piece}" if buf else piece
        else:
            buf = candidate
    if buf.strip():
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]
