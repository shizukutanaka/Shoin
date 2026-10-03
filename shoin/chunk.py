"""Chunking: heading-aware splitting with CJK-aware token estimation.

Token estimate (no external tokenizer): 1 token per CJK character plus 1 token
per non-CJK word-ish run. Conservative enough for budget control on <=8B models.
"""

from __future__ import annotations

import bisect
import re
import unicodedata

from .config import CHUNK_OVERLAP, CHUNK_TOKENS

_CJK_RANGES = (
    (0x0E00, 0x0E7F),    # Thai
    (0x0E80, 0x0EFF),    # Lao
    (0x1000, 0x109F),    # Myanmar
    (0x1780, 0x17FF),    # Khmer
    # NFKC-foldable blocks: characters that decompose to canonical
    # Japanese/ASCII spellings.  Outside the ranges they silently
    # vanished from query_terms (a '㍻元年' query searched '元年' alone)
    # and escaped the CJK token cost; inside, term_variants' NFKC form
    # bridges them to canonical spellings (㍻→平成, ㎏→kg, ﬁ→fi, Ⅲ→III).
    (0x00AA, 0x00AA),    # ª feminine ordinal → a
    (0x00B2, 0x00B3),    # ² ³ superscripts
    (0x00B5, 0x00B5),    # µ micro sign → μ
    (0x00B9, 0x00B9),    # ¹
    (0x00BA, 0x00BA),    # º masculine ordinal → o
    (0x00BC, 0x00BE),    # ¼ ½ ¾ vulgar fractions
    (0x1100, 0x11FF),    # Hangul Jamo (decomposed — macOS NFD filenames)
    (0x1B001, 0x1B152),  # kana supplement + ext-A hentaigana (→ katakana)
    (0x2044, 0x2044),    # ⁄ fraction slash → /
    (0x2070, 0x209C),    # superscript/subscript digits and letters ⁰-₉ₐ-ₜ
    (0x2100, 0x214F),    # letterlike symbols ℃ ℉ № ℠ ㏄ (all fold)
    (0x2160, 0x2188),    # Roman numerals Ⅰ-Ⅻ ⅰ-ⅻ ↀ-ↈ
    (0x2460, 0x24FF),    # enclosed alphanumerics ①Ⓐⓐ
    (0x3000, 0x303F),    # CJK symbols and punctuation (。、　〆々 etc.)
    (0x3040, 0x30FF),    # hiragana + katakana
    (0x3130, 0x318F),    # Hangul compatibility jamo ㄱ-ㆎ (fold to jamo)
    (0x31F0, 0x31FF),    # katakana phonetic extensions (Ainu kana)
    (0x3200, 0x33FF),    # enclosed CJK letters/months + compat (㈱㋿㍻㎏)
    (0x3400, 0x4DBF),    # CJK ext A
    (0x4E00, 0x9FFF),    # CJK unified ideographs
    (0xF900, 0xFAFF),    # CJK compat
    (0xFF10, 0xFF19),    # fullwidth digits ０-９
    (0xFF21, 0xFF3A),    # fullwidth Latin uppercase Ａ-Ｚ
    (0xFF41, 0xFF5A),    # fullwidth Latin lowercase ａ-ｚ
    (0xFF61, 0xFF65),    # halfwidth CJK punctuation ｡｢｣､ and middle dot ･
    (0xFF66, 0xFF9F),    # halfwidth katakana + voiced/semi-voiced marks ﾞﾟ
    (0xAC00, 0xD7A3),    # Hangul syllables
    (0x1F200, 0x1F2FF),  # enclosed ideographic supplement (🈶🈚🈸🈯🉐)
    (0x1D400, 0x1D7FF),  # math alphanumeric 𝐀-𝞃 (→ ASCII letters)
    (0x20000, 0x2A6DF),  # CJK ext B (supplementary plane — rare/historical chars)
    (0x2A700, 0x2CEAF),  # CJK ext C/D/E/F
    (0x2CEB0, 0x2EBEF),  # CJK ext G/H
    (0xFB00, 0xFB4F),    # alphabetic presentation forms ﬀ-ﬅ + Hebrew forms
    (0xFFA0, 0xFFDC),    # halfwidth Hangul jamo (NFKC → jamo → syllables)
    (0xFFE0, 0xFFE6),    # fullwidth currency/symbols ￠￡￥￦
    # Alphabetic scripts: letters, vowel marks and digits that are NOT
    # NFKC-foldable but ARE content.  Before this, 'café' silently lost
    # é, a Cyrillic/Greek/Arabic query matched nothing at all (its whole
    # term list was dropped), and every such char rode the token budget
    # at cost 0.  Blocks are taken near-whole — punctuation they contain
    # (Hebrew ־, Arabic ؛؟, danda, Armenian stops) is excluded from word
    # runs by _is_cjk_word()'s category test, not by range surgery.
    (0x00C0, 0x00FF),    # Latin-1 letters à-ÿ (× ÷ excluded word-side)
    (0x0100, 0x024F),    # Latin extended A+B
    (0x0250, 0x02AF),    # IPA extensions
    (0x02B0, 0x02FF),    # spacing modifier letters
    (0x0300, 0x036F),    # combining diacritical marks (NFD text)
    (0x0370, 0x03FF),    # Greek and Coptic
    (0x0400, 0x052F),    # Cyrillic + supplement
    (0x0530, 0x058F),    # Armenian
    (0x0590, 0x05FF),    # Hebrew (letters + nikkud marks)
    (0x0600, 0x06FF),    # Arabic (letters + harakat + Eastern digits)
    (0x0700, 0x077F),    # Syriac + Arabic supplement
    (0x0780, 0x07BF),    # Thaana
    (0x07C0, 0x07FF),    # NKo
    (0x08A0, 0x08FF),    # Arabic extended-A
    (0x0900, 0x097F),    # Devanagari
    (0x0980, 0x09FF),    # Bengali
    (0x0A00, 0x0A7F),    # Gurmukhi
    (0x0A80, 0x0AFF),    # Gujarati
    (0x0B00, 0x0B7F),    # Oriya
    (0x0B80, 0x0BFF),    # Tamil
    (0x0C00, 0x0C7F),    # Telugu
    (0x0C80, 0x0CFF),    # Kannada
    (0x0D00, 0x0D7F),    # Malayalam
    (0x0D80, 0x0DFF),    # Sinhala
    (0x0F00, 0x0FFF),    # Tibetan
    (0x10A0, 0x10FF),    # Georgian
    (0x1200, 0x137F),    # Ethiopic
    (0x13A0, 0x13FF),    # Cherokee
    (0x1400, 0x167F),    # Unified Canadian Aboriginal Syllabics
    (0x1680, 0x169F),    # Ogham (1680 space excluded word-side)
    (0x16A0, 0x16FF),    # Runic
    (0x1800, 0x18AF),    # Mongolian
    (0x1AB0, 0x1AFF),    # combining diacritical marks extended
    (0x1D00, 0x1DBF),    # phonetic extensions
    (0x1DC0, 0x1DFF),    # combining marks supplement
    (0x1E00, 0x1EFF),    # Latin extended additional
    (0x1F00, 0x1FFF),    # Greek extended
    (0x20D0, 0x20FF),    # combining marks for symbols
    (0x2C60, 0x2C7F),    # Latin extended-C
    (0x2DE0, 0x2DFF),    # Cyrillic extended-A
    (0xA640, 0xA69F),    # Cyrillic extended-B
    (0xA720, 0xA7FF),    # Latin extended-D
    (0xAB30, 0xAB6F),    # Latin extended-E
    (0xFE20, 0xFE2F),    # combining half marks
    (0xE0100, 0xE01EF),  # variation selectors supplement
    # Symbol/emoji closure (v0.2.530): the last invisible class — So/Sc/Sk
    # characters and sequence gluers.  '☕' / '😀' / '✓' / '€' used to drop
    # from query_terms entirely ('☕カフェ' searched 'カフェ' alone, an
    # emoji-only query returned nothing) and cost 0 tokens.  Sequence
    # joiners (ZWNJ/ZWJ, variation selectors) become word characters too so
    # '👨‍💻' and '☕️' keep their codepoint runs — FTS5 trigram indexes
    # them, and the LIKE needles do literal substring anyway.  Historic
    # scripts already ride the isalnum path; these fill the So-shaped hole.
    (0x200C, 0x200D),    # ZWNJ/ZWJ — Indic orthography + emoji sequences
    (0x20A0, 0x20CF),    # currency symbols € ₹ ₽ ₩ (Sc — content)
    (0x2190, 0x245F),    # arrows, math ops ∑√∫, misc technical ⌘⌚
    (0x2500, 0x2BFF),    # box/geometric shapes, misc symbols ☕⚠★,
                        # dingbats ✓✈✂, supplemental arrows/math
    (0x2FF0, 0x2FFF),    # ideographic description chars ⿴
    (0x31C0, 0x31EF),    # CJK strokes
    (0xFE00, 0xFE0F),    # variation selectors VS1-16 (emoji text/emoji form)
    (0x1B000, 0x1B0FF),  # kana supplement (hentaigana — whole-range)
    (0x1F000, 0x1F1FF),  # mahjong/domino/cards + regional indicators
    (0x1F300, 0x1FAFF),  # emoji + symbols + enclosed supplement 🄯
)

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_HEADING_RE = re.compile(r"^#{1,6}\s")
# ｡ (U+FF61) is the halfwidth JIS X 0201 counterpart of 。 and terminates a
# sentence identically — it is common in cp932 legacy text, which ingest._decode()
# actively prefers, and NFKC folds it to 。 anyway. Without it, both this splitter's
# consumers went width-blind: _hard_split() produced chunks cut mid-sentence at an
# arbitrary character window, and citation.py's verify_grounding()/uncited_sentences()
# saw one giant "sentence" whose diluted bigram overlap hid unsupported claims.
# (The other terminators need no halfwidth twin: ！？ fold to the ASCII !? already
# in the class, and ． 's halfwidth is ASCII . handled by the (?<=\.)(?=\s) branch.)
# v0.2.533: the scripts v0.2.529 made word characters carry their own
# terminators, and without them a Hindi or Urdu paragraph is one giant
# "sentence" — the same width-blind failure ｡ had, one script family wider:
# ।॥ danda (Devanagari & friends), ။ Myanmar section, ។ Khmer khan,
# །༎ Tibetan shad/nyis shad, 。 Urdu/Arabic full stop, ؟ Arabic question,
# ።፧፨ Ethiopic full stop/question/paragraph, ᠃ Mongolian full stop,
# ։ Armenian full stop, ׃ Hebrew sof pasuq.  Each is unambiguous like 。
# (unlike ASCII '.', no space-guard needed — none appears mid-number).
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[。．！？!?\n；｡।॥။។།༎۔؟።፧፨᠃։׃])|(?<=\.)(?=\s)"
)
# A genuine ATX heading closing sequence per CommonMark: one or more '#'
# preceded by at least one space, with optional trailing spaces only
# (e.g. "## Heading ##" -> the " ##" suffix). Requiring the preceding space
# is what distinguishes a real closing sequence from a title that legitimately
# ends in '#' with no space before it (language names like "C#"/"F#") — a
# bare rstrip("#") cannot make that distinction and silently deletes the
# character from such titles regardless of context.
_ATX_CLOSING_RE = re.compile(r"\s+#+\s*$")

# Upper bound on a chunk's contextual breadcrumb (heading path joined with " > ").
# The breadcrumb is prepended to the chunk only for RETRIEVAL (FTS5 + embedding),
# never shown to the user or fed to citation verification, so it needs to carry the
# heading signal without bloating the index. A deeply nested document with long
# headings could otherwise produce a breadcrumb longer than the chunk itself.
_MAX_CONTEXT_CHARS = 200

# Words/identifiers up to this length cost a flat 1 token (CLAUDE.md's documented
# "ASCII words: 1 token per word" model — real natural-language words and typical
# identifiers rarely exceed this). An unbroken alphanumeric run LONGER than this
# (a base64 data: URI, a long hex hash, minified/obfuscated code with no spaces)
# is weighted at ~4 chars/token beyond the threshold instead of still costing a
# flat 1 token regardless of length — without this, estimate_tokens() undercounts
# a 200,000-character run to a single-digit token count, silently defeating both
# split_text()'s chunk-size cap and build_context()'s per-source token budget.
_LONG_RUN_THRESHOLD = 40


def _merged_cjk_bounds() -> list[int]:
    """Flatten _CJK_RANGES into sorted [start, end+1, start, end+1, ...] boundaries.

    Ranges are merged first because the table is NOT written sorted or disjoint
    (verified — adjacent blocks touch), and the parity test below is only correct
    on disjoint, ordered intervals. Merging here means a future range added in any
    position or overlapping an existing one still classifies correctly.
    """
    merged: list[list[int]] = []
    for lo, hi in sorted(_CJK_RANGES):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    bounds: list[int] = []
    for lo, hi in merged:
        bounds.append(lo)
        bounds.append(hi + 1)
    return bounds


_CJK_BOUNDS = _merged_cjk_bounds()
# The same range table as a regex character class. The regex engine scans in C,
# so counting CJK characters with it beats calling is_cjk() per character by ~59x
# (measured) — and estimate_tokens() is the hot function of the whole ingest path.
# search.py builds _NEG_RE's classes from _CJK_RANGES the same way.
_CJK_CLASS = "".join(f"\\U{lo:08x}-\\U{hi:08x}" for lo, hi in _CJK_RANGES)
_NON_CJK_RE = re.compile(f"[^{_CJK_CLASS}]+")


def is_cjk(ch: str) -> bool:
    # bisect over disjoint sorted boundaries: a code point is inside a range iff
    # an odd number of boundaries lie at or below it. ~4 comparisons instead of a
    # linear scan of every range, with identical results (exhaustively checked at
    # every range edge plus a wide random sample).
    return bisect.bisect_right(_CJK_BOUNDS, ord(ch)) % 2 == 1


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
    # Strip everything that is NOT CJK and measure what is left: one C-level regex
    # pass. Measured against the alternatives on a 1M-character document — this is
    # 12.4 ms / 4.1 MB peak, re.findall is 96.3 ms / 70.5 MB (it materializes one
    # string object per matched character), and the previous per-character Python
    # loop was ~730 ms. Counting the same set either way.
    cjk = len(_NON_CJK_RE.sub("", text))
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
    buf_tokens = 0
    for sent in _SENTENCE_SPLIT_RE.split(block):
        if not sent:
            continue
        # Track the running total instead of estimate_tokens(buf + sent): the
        # latter rescans all of buf once per sentence — ~50µs × the block's
        # sentence count, which is a measurable stall on newline-dense input
        # (100k lines ≈ 5s). Additivity is exact for CJK; an ASCII word run
        # split across the boundary can overestimate by ≤1 token, which errs
        # toward splitting early — safe.
        sent_tokens = estimate_tokens(sent)
        if buf and buf_tokens + sent_tokens > limit:
            parts.append(buf)
            buf = sent
            buf_tokens = sent_tokens
        else:
            buf += sent
            buf_tokens += sent_tokens
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
            out.extend(p[i : i + window] for i in range(0, len(p), window))
        elif tok == 0 and len(p) > limit * 5:
            # Zero-token text (Arabic, Hebrew, Cyrillic, pure punctuation) escapes
            # estimate_tokens(); a pathologically long block (> limit*5 chars) must
            # still be split.  Use limit*5 chars as a conservative character budget
            # (matches ~5 chars/token ASCII density as an upper bound).
            window = max(limit * 5, 1)
            out.extend(p[i : i + window] for i in range(0, len(p), window))
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


def _heading_level(line: str) -> int:
    """ATX heading depth of *line* (number of leading '#'), or 0 if not a heading."""
    if not _HEADING_RE.match(line):
        return 0
    n = 0
    for ch in line:
        if ch == "#":
            n += 1
        else:
            break
    return n


def _context_blocks(text: str) -> list[tuple[str, str]]:
    """Yield (breadcrumb, block) pairs, tracking the markdown heading hierarchy.

    The breadcrumb is the chain of enclosing headings joined with " > " (e.g.
    "モデル構成 > エンコーダ"). It captures the section a block lives under so that
    a chunk split away from its heading — or merged across heading-less prose —
    still carries the section context for retrieval.  Purely structural: no LLM,
    no extra latency, derived from the same _blocks() split split_text() uses.
    """
    stack: list[tuple[int, str]] = []
    out: list[tuple[str, str]] = []
    for block in _blocks(text):
        first = block.split("\n", 1)[0]
        lvl = _heading_level(first)
        if lvl:
            # A heading closes every open section at the same or deeper level.
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            title = _ATX_CLOSING_RE.sub("", first[lvl:].strip()).strip()
            if title:
                stack.append((lvl, title))
        out.append((" > ".join(t for _, t in stack), block))
    return out


def split_text_with_context(
    text: str,
    chunk_tokens: int = CHUNK_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP,
) -> list[tuple[str, str]]:
    """Like split_text(), but pairs each chunk with its heading breadcrumb.

    Returns (context, chunk_text) tuples. The chunk_text is byte-for-byte
    identical to what split_text() returns for the same input; the context is
    the breadcrumb of the heading section in which the chunk *begins* (the tail
    carried over from a previous chunk for overlap keeps the new chunk's own
    section, since that is where the fresh content lives).
    """
    pieces: list[tuple[str, str]] = []
    for ctx, block in _context_blocks(text):
        if estimate_tokens(block) > chunk_tokens:
            pieces.extend((ctx, p) for p in _hard_split(block, chunk_tokens))
        else:
            pieces.append((ctx, block))

    chunks: list[tuple[str, str]] = []
    buf = ""
    buf_ctx = ""
    buf_tokens = 0
    for ctx, piece in pieces:
        piece_tokens = estimate_tokens(piece)
        # Running total rather than estimate_tokens(candidate): same rescans-
        # buf-per-piece stall as _hard_split above. ±1 boundary overestimate is
        # safe (errs toward emitting early).
        if buf and buf_tokens + piece_tokens > chunk_tokens:
            chunks.append((buf_ctx, buf))
            buf = _tail(buf, overlap_tokens)
            buf_tokens = estimate_tokens(buf)
            buf = f"{buf}\n\n{piece}" if buf else piece
            buf_ctx = ctx
            buf_tokens += piece_tokens
        else:
            if not buf:
                buf_ctx = ctx
            buf = f"{buf}\n\n{piece}" if buf else piece
            buf_tokens += piece_tokens
    if buf.strip():
        chunks.append((buf_ctx, buf))
    return [(c[:_MAX_CONTEXT_CHARS], t.strip()) for c, t in chunks if t.strip()]


def split_text(
    text: str,
    chunk_tokens: int = CHUNK_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP,
) -> list[str]:
    """Split *text* into chunks of ~chunk_tokens with overlap between chunks."""
    return [t for _, t in split_text_with_context(text, chunk_tokens, overlap_tokens)]


# --- Spelling-fold primitives (v0.2.540) ------------------------------------
# Retrieval bridges spelling variants query-side (term_variants enumerates
# the spellings a term might carry).  The citation checks face the same
# variants on BOTH sides of a comparison — an answer echoing データ as
# でーた, 學習 as 学習, café as cafe, ٣٤٥ as 345 — but they cannot enumerate:
# instead both texts fold to one canonical spelling so every variant pair
# converges.  The tables live here, the shared bottom layer, because both
# search.py (variant generation) and citation.py (comparison folding) use
# them; importing across between those two would be circular.

# shinjitai → kyujitai.  Ambiguous simplifications (弁, 台, 与…) pick the
# most common predecessor — an occasionally wrong old form is harmless in
# both consumers: the variant adds an extra needle/gram, and the fold only
# ever merges spellings of the same word.
_SHIN_TO_KYU: dict[str, str] = {
    "圧": "壓", "悪": "惡", "為": "爲", "医": "醫", "壱": "壹", "隠": "隱",
    "栄": "榮", "衛": "衞", "円": "圓", "縁": "緣", "応": "應", "欧": "歐",
    "殴": "毆", "桜": "櫻", "温": "溫", "穏": "穩", "仮": "假", "価": "價",
    "画": "畫", "会": "會", "懐": "懷", "壊": "壞", "概": "槪", "拡": "擴",
    "殻": "殼", "覚": "覺", "学": "學", "楽": "樂", "缶": "罐", "関": "關",
    "陥": "陷", "勧": "勸", "寛": "寛", "観": "觀", "気": "氣", "亀": "龜",
    "偽": "僞", "戯": "戲", "犠": "犧", "旧": "舊", "拠": "據", "挙": "擧",
    "虚": "虛", "峡": "峽", "狭": "狹", "郷": "鄕", "暁": "曉", "区": "區",
    "駆": "驅", "継": "繼", "茎": "莖", "渓": "溪", "経": "經", "蛍": "螢",
    "軽": "輕", "鶏": "鷄", "芸": "藝", "撃": "擊", "研": "硏", "県": "縣",
    "倹": "儉", "剣": "劍", "険": "險", "献": "獻", "検": "驗", "顕": "顯",
    "広": "廣", "効": "效", "鉱": "鑛", "号": "號", "国": "國", "穀": "榖",
    "黒": "黑", "砕": "碎", "済": "濟", "剤": "劑", "斎": "齋", "雑": "雜",
    "桟": "棧", "賛": "贊", "蚕": "蠶", "残": "殘", "辞": "辭", "歯": "齒",
    "児": "兒", "湿": "濕", "実": "實", "写": "寫", "釈": "釋", "寿": "壽",
    "収": "收", "従": "從", "渋": "澁", "獣": "獸", "縦": "縱", "粛": "肅",
    "処": "處", "将": "將", "奨": "奬", "醤": "醬", "焼": "燒", "証": "證",
    "条": "條", "乗": "乘", "剰": "剩", "浄": "淨", "畳": "疊", "縄": "繩",
    "壌": "壤", "醸": "釀", "嬢": "孃", "触": "觸", "寝": "寢", "慎": "愼",
    "真": "眞", "尽": "盡", "図": "圖", "粋": "粹", "酔": "醉", "穂": "穗",
    "随": "隨", "髄": "髓", "枢": "樞", "数": "數", "声": "聲", "静": "靜",
    "摂": "攝", "専": "專", "浅": "淺", "戦": "戰", "践": "踐", "銭": "錢",
    "潜": "潛", "繊": "纖", "禅": "禪", "壮": "壯", "争": "爭", "荘": "莊",
    "装": "裝", "捜": "搜", "挿": "插", "蔵": "藏", "臓": "臟", "増": "增",
    "即": "卽", "属": "屬", "続": "續", "堕": "墮", "対": "對", "体": "體",
    "帯": "帶", "滞": "滯", "台": "臺", "滝": "瀧", "択": "擇", "沢": "澤",
    "単": "單", "胆": "膽", "団": "團", "弾": "彈", "断": "斷", "痴": "癡",
    "虫": "蟲", "鋳": "鑄", "庁": "廳", "徴": "徵", "聴": "聽", "懲": "懲",
    "勅": "敕", "転": "轉", "伝": "傳", "灯": "燈", "当": "當", "盗": "盜",
    "稲": "稻", "徳": "德", "独": "獨", "読": "讀", "弐": "貳", "悩": "惱",
    "脳": "腦", "覇": "霸", "拝": "拜", "廃": "廢", "売": "賣", "麦": "麥",
    "発": "發", "髪": "髮", "抜": "拔", "蛮": "蠻", "秘": "祕", "浜": "濱",
    "氷": "冰", "弁": "辯", "歩": "步", "宝": "寶", "豊": "豐", "没": "沒",
    "万": "萬", "満": "滿", "黙": "默", "訳": "譯", "薬": "藥", "与": "與",
    "誉": "譽", "揺": "搖", "様": "樣", "謡": "謠", "来": "來", "覧": "覽",
    "竜": "龍", "涙": "淚", "塁": "壘", "暦": "曆", "歴": "歷", "恋": "戀",
    "楼": "樓", "録": "錄", "練": "練", "齢": "齡", "労": "勞", "炉": "爐",
    "禄": "祿", "乱": "亂", "湾": "灣",
}

_KYU_TO_SHIN = {v: k for k, v in _SHIN_TO_KYU.items()}

# Latin letters NFKC does not fold to ASCII: digraphs and letters with no
# canonical decomposition (ICU Latin-ASCII transliterator's core closed set).
# The mark-strip half handles the composable rest (é→e, ñ→n, ü→u).
_LATIN_SPECIALS: dict[str, str] = {
    "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ß": "ss", "ẞ": "SS", "ø": "o", "Ø": "O",
    "đ": "d", "Đ": "D", "þ": "th", "Þ": "TH", "ð": "d", "Ð": "D",
    "ł": "l", "Ł": "L", "ı": "i", "ŋ": "n", "Ŋ": "N",
    "ħ": "h", "Ħ": "H", "ə": "e", "Ə": "E",
}


def _ascii_fold(term: str) -> str:
    """ASCII spelling of a Latin term: diacritics stripped, specials mapped.

    Decompose each character and drop its combining marks only when the
    base is an ASCII letter — kana dakuten/handedakuten decompose too
    (ド → ト + ゛) but their base is not ASCII, so 'データ' stays 'データ'
    rather than emitting the dead 'テータ' spelling."""
    out: list[str] = []
    for ch in term:
        mapped = _LATIN_SPECIALS.get(ch)
        if mapped is not None:
            out.append(mapped)
            continue
        decomp = unicodedata.normalize("NFD", ch)
        if len(decomp) > 1 and decomp[0].isascii() and decomp[0].isalpha():
            out.append("".join(c for c in decomp if not unicodedata.combining(c)))
        else:
            out.append(ch)
    return "".join(out)


def _match_fold(text: str) -> str:
    """Canonical spelling for content comparison (v0.2.540).

    NFKC + casefold leaves same-word spellings byte-distinct — データ vs
    でーた, 學 vs 学, café vs cafe, ٣٤٥ vs 345, ド in NFC vs ト + ゛ in
    NFD, ソフト with a U+00AD SHY vs ソフト — so any check that compares
    an answer against a source was blind to the very spellings retrieval
    bridges.  A fold applied to BOTH sides needs no enumeration: every
    variant pair converges to one canonical form.

    Per character, after NFKC + casefold: katakana → hiragana, format
    characters (ZWSP/SHY/ZWNJ/WJ/tags) dropped, decimal digit rows →
    ASCII digits, kyujitai → shinjitai, Latin specials → ASCII, and
    combining marks on ASCII bases stripped."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFKC", text).casefold():
        cp = ord(ch)
        if 0x30A1 <= cp <= 0x30F6:
            out.append(chr(cp - 0x60))  # katakana → hiragana
            continue
        if unicodedata.category(ch) == "Cf":
            continue  # ZWSP / SHY / ZWNJ / WJ / tag characters carry no content
        if unicodedata.combining(ch):
            # A mark that survived NFKC could not compose into any base char —
            # marks that can are consumed by the composition pass above (and
            # precomposed accents lose theirs in the decomp branch below).
            # What reaches here is a stray diacritic glued to a non-letter or
            # stacked behind an already-composed char (é + ◌́): it carries no
            # glyph, and keeping it split the fold of its neighbors, making
            # the fold non-idempotent ('é'+◌́ → 'e'+◌́ → 'e') and letting NFD
            # fragments diverge from their NFC spellings.
            continue
        mapped = _LATIN_SPECIALS.get(ch)
        if mapped is not None:
            out.append(mapped)
            continue
        if ch.isdecimal():  # every script's Nd row → ASCII digits
            out.append(chr(ord("0") + unicodedata.decimal(ch)))
            continue
        kyu = _KYU_TO_SHIN.get(ch)
        if kyu is not None:
            out.append(kyu)
            continue
        decomp = unicodedata.normalize("NFD", ch)
        if len(decomp) > 1 and decomp[0].isascii() and decomp[0].isalpha():
            out.append("".join(c for c in decomp if not unicodedata.combining(c)))
        else:
            out.append(ch)
    return "".join(out)


def _digit_fold(s: str) -> str:
    """Every script's Nd row → ASCII digits (same mapping _match_fold applies
    inline): '٣٤٥', '३४५', '๓๔๕' all become '345'.  int()/float() already
    accept these digits; the fold exists for code that compares digit
    strings verbatim."""
    return "".join(
        chr(ord("0") + unicodedata.decimal(c)) if c.isdecimal() else c
        for c in s
    )
