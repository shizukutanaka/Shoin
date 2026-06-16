"""Chunking: heading-aware splitting with CJK-aware token estimation.

Token estimate (no external tokenizer): 1 token per CJK character plus 1 token
per non-CJK word-ish run. Conservative enough for budget control on <=8B models.
"""

from __future__ import annotations

import re

from .config import CHUNK_OVERLAP, CHUNK_TOKENS

_CJK_RANGES = (
    (0x0E00, 0x0E7F),  # Thai
    (0x0E80, 0x0EFF),  # Lao
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
    (0x3000, 0x303F),  # CJK symbols and punctuation (。、　〆々 etc.)
    (0x3040, 0x30FF),  # hiragana + katakana
    (0x3400, 0x4DBF),  # CJK ext A
    (0x4E00, 0x9FFF),  # CJK unified
    (0xF900, 0xFAFF),  # CJK compat
    (0xFF66, 0xFF9D),  # halfwidth katakana
    (0xAC00, 0xD7A3),  # Hangul syllables
)

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_HEADING_RE = re.compile(r"^#{1,6}\s")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。．！？!?\n；])")


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if is_cjk(ch))
    words = len(_WORD_RE.findall(text))
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
        if estimate_tokens(p) <= limit:
            out.append(p)
            continue
        # character-window fallback (pathological unbroken text)
        window = max(limit, 1)
        for i in range(0, len(p), window):
            out.append(p[i : i + window])
    return [p.strip() for p in out if p.strip()]


def _tail(text: str, tokens: int) -> str:
    """Return a suffix of *text* containing roughly *tokens* tokens."""
    if tokens <= 0:
        return ""
    acc = 0
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if is_cjk(ch):
            acc += 1
        elif not ch.isalnum() and i + 1 < len(text) and text[i + 1].isalnum():
            acc += 1  # word boundary crossed
        if acc >= tokens:
            # For CJK, position i IS the token — include it.
            # For word boundaries, i is the non-alnum separator before the word
            # that starts at i+1 — exclude the separator.
            return (text[i:] if is_cjk(ch) else text[i + 1 :]).lstrip()
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
