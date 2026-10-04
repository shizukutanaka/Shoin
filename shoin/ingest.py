"""Source ingestion: text extraction for files and URLs with SSRF guards.

Supported kinds: txt, md, html, pdf, url. All extraction is local; URL fetch
is the only network path and is restricted to public http(s) hosts.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import urllib.parse
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from .config import MAX_UPLOAD_BYTES, URL_MAX_REDIRECTS, URL_TIMEOUT_SEC, VERSION

_EXT_KIND = {
    ".txt": "txt",
    ".md": "md",
    ".markdown": "md",
    ".html": "html",
    ".htm": "html",
    ".pdf": "pdf",
}

_BLOCK_TAGS = frozenset(
    "p div br li ul ol h1 h2 h3 h4 h5 h6"
    " tr td th table caption thead tbody tfoot"
    " section article header aside main"
    " blockquote pre dd dt dl figure figcaption".split()
)

# Boilerplate chrome whose text is navigation chrome, not document content:
# menus, cookie/related-link lists, and page footers get chunked, embedded,
# and cited as if they were part of the source — the classic noise trafilatura
# / readability-style extraction removes before retrieval. Skipping is done at
# the parser level via _skip_depth, and _SKIP_TAG_BALANCE below neutralizes an
# unclosed opener so a malformed <nav> can't swallow the rest of the page.
# <header>/<aside> deliberately stay: articles use them for lead paragraphs
# and substantive sidebars, not just boilerplate.
_BOILERPLATE_TAGS = frozenset("nav footer form".split())


class IngestError(Exception):
    """Ingestion failure with a stable error code (spec: error code scheme)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Extracted:
    kind: str
    title: str
    text: str
    origin: str
    sha256: str
    # Pages whose text extraction raised (PDF only). Surfaced so the ingest
    # caller can warn: the graceful per-page fallback means a corrupt page's
    # content silently vanishes from the index without this signal.
    pages_failed: int = 0


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_size(data: bytes) -> None:
    if len(data) > MAX_UPLOAD_BYTES:
        raise IngestError(
            "INGEST_FILE_TOO_LARGE",
            f"source exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit",
        )


def _decode(data: bytes, charset: str | None = None) -> str:
    candidates = []
    if charset:
        candidates.append(charset)
    # BOM detection: cp932 accepts any byte sequence so it would silently produce
    # mojibake for UTF-16/32 content. Detect BOM-prefixed content explicitly before
    # the cp932 fallback so the correct codec is used. UTF-32 MUST be checked before
    # UTF-16: the UTF-32 LE BOM (FF FE 00 00) begins with the UTF-16 LE BOM (FF FE),
    # so a 2-byte-first test misdetects UTF-32 LE as UTF-16 and decodes every
    # character interleaved with a null (Unicode BOM FAQ's documented ambiguity),
    # and the UTF-32 BE BOM (00 00 FE FF) is missed entirely and falls to cp932
    # garbage. Longest BOM first is the correct precedence.
    if data[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        candidates.append("utf-32")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        candidates.append("utf-16")
    # utf-8-sig handles plain UTF-8 and BOM-prefixed UTF-8 (Windows Notepad);
    # cp932 covers Shift-JIS, the dominant legacy encoding for Japanese content.
    candidates.extend(["utf-8-sig", "cp932"])
    for enc in candidates:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class _HTMLText(HTMLParser):
    """Minimal stdlib HTML -> text extractor (skips script/style and boilerplate, keeps blocks)."""

    # Remove "title" from Python's RCDATA_CONTENT_ELEMENTS so the tokenizer does
    # not enter raw-text mode on <title> — otherwise </noscript> (or any other tag)
    # inside an unclosed <title> is consumed as text rather than fired as a closing
    # tag, leaving _skip_depth permanently > 0 and silently swallowing the body.
    # typeshed marks this attribute Final for internal-implementation-detail reasons;
    # HTMLParser itself does not enforce that at runtime, and this override is the
    # documented, tested mechanism for the <title> raw-text fix above.
    RCDATA_CONTENT_ELEMENTS = ("textarea",)  # type: ignore[misc]

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        # Open skip elements by name, innermost last. A bare counter can't
        # tell which element a stray endtag refers to: </nav> must not close
        # an enclosing <noscript>, and </footer> must not pop a <form>.
        self._skip_stack: list[str] = []
        self._in_title = False
        self._saw_head = False

    @property
    def _skip_depth(self) -> int:
        return len(self._skip_stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "template") or tag in _BOILERPLATE_TAGS:
            self._skip_stack.append(tag)
        elif tag == "title" and not self._skip_depth:
            self._in_title = True
        elif tag == "head":
            self._saw_head = True
        elif tag in _BLOCK_TAGS:
            if self._in_title:
                self._in_title = False  # implicit close: block content can't appear inside <title>
            self.parts.append("\n")
        elif tag in ("body", "html") and self._in_title:
            self._in_title = False  # structural tag implies <title> was never properly closed

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template") or tag in _BOILERPLATE_TAGS:
            # Pop through the matching opener, DOM-style: an endtag closes
            # its element plus anything implicitly nested inside it; a stray
            # endtag for a tag that isn't open is ignored entirely.
            if tag in self._skip_stack:
                while self._skip_stack.pop() != tag:
                    pass
        elif tag == "title":
            self._in_title = False
        elif tag == "head" and self._saw_head:
            # An unclosed <noscript>/<script>/<style> in <head> must not leak into
            # <body> and swallow all body text.  Reset both guards at </head> so
            # malformed markup like <noscript>fallback</head><body>Content</body>
            # still extracts "Content" rather than raising INGEST_EMPTY. Gated on
            # a real <head> opener: a stray </head> inside some other unclosed
            # element would otherwise zero the skip depth mid-element and leak
            # the rest of its text.
            self._skip_stack.clear()
            if self._in_title:
                self._in_title = False  # </head> without </title> implicitly closes the title
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.parts.append(data)


_SKIP_TAG_BALANCE = (
    # (open regex, close regex, tag name). script/style are deliberately
    # excluded: an unclosed <script>/<style> legitimately swallows the rest
    # of the document per real browser tokenizer behavior (they're CDATA
    # content elements), so that's not a defect to neutralize. noscript and
    # template are NOT CDATA elements — an unclosed or mismatched-closer one
    # (e.g. a typo'd </noscript-analytics> from a broken analytics snippet)
    # is purely an artifact of this module's own balanced-counter _skip_depth
    # tracking having no recovery path, unlike <head> (which already resets
    # _skip_depth at </head>, v0.2.40) — this covers the same class of bug
    # when it happens inside <body> instead.
    (re.compile(r"<noscript\b", re.I), re.compile(r"</noscript\s*>", re.I), "noscript"),
    (re.compile(r"<template\b", re.I), re.compile(r"</template\s*>", re.I), "template"),
    # nav/footer/form are now skip-depth elements too (v0.2.256): an unclosed
    # one would swallow the entire rest of the document, which is strictly
    # worse than keeping its boilerplate text — the closer injection below
    # degrades to the old keep-the-text behavior for malformed markup.
    (re.compile(r"<nav\b", re.I), re.compile(r"</nav\s*>", re.I), "nav"),
    (re.compile(r"<footer\b", re.I), re.compile(r"</footer\s*>", re.I), "footer"),
    (re.compile(r"<form\b", re.I), re.compile(r"</form\s*>", re.I), "form"),
)


def _outside_tag(html: str, pos: int) -> bool:
    """True when pos is not inside a '<...>' tag region.

    Regexes see tag-like text inside attribute values or malformed markup
    (e.g. the "</nav" of "<a title='x'</nav>"), but HTMLParser never fires
    such matches — they are attribute junk, not tags. Neutralization pairing
    must ignore them or it simulates a different document than the parser
    will see. A bare "<" only opens a tag when followed by a tag-start char
    (ASCII letter, "/", "!", "?" — matching HTMLParser's tagfind); "< 2",
    "<3", or a non-ASCII letter like "<テ" is literal text — so the scan
    walks back to the most recent *valid* opener before comparing ">"s.
    """
    lt = html.rfind("<", 0, pos)
    while lt != -1:
        nxt = html[lt + 1 : lt + 2]
        if nxt and ((nxt.isascii() and nxt.isalpha()) or nxt in "/!?"):
            break
        lt = html.rfind("<", 0, lt)
    if lt == -1:
        return True
    return lt <= html.rfind(">", 0, pos)


def _comment_spans(html: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of <!--...--> comments.

    A "<!--" inside a tag's attribute region is attribute text, not a
    comment opener, so openers are filtered; a "-->" once inside a comment
    closes it regardless of tags. An unclosed comment runs to end-of-
    document, matching HTMLParser's buffering behavior.
    """
    spans: list[tuple[int, int]] = []
    start = -1
    for m in re.finditer(r"<!--|-->", html):
        if start == -1:
            # A "<!--" inside a tag's attribute region is attribute text,
            # not a comment opener — keep the parser-aligned filter on it.
            if m.group() == "<!--" and _outside_tag(html, m.start()):
                start = m.start()
        else:
            # Inside a comment the first "-->" is the closer at parse
            # level — comment content is CDATA-ish and does not respect
            # tag boundaries, so no outside-tag filter applies here.
            if m.group() == "-->":
                spans.append((start, m.end()))
                start = -1
    if start != -1:
        spans.append((start, len(html)))
    return spans


def _live(html: str, pos: int, spans: list[tuple[int, int]]) -> bool:
    """True when pos fires as markup — outside tags and outside comments."""
    return _outside_tag(html, pos) and not any(
        s <= pos < e for s, e in spans
    )


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, text) extracted from an HTML document."""
    # An unclosed <!-- comment causes stdlib html.parser.HTMLParser to buffer
    # everything from "<!--" through end-of-document and flush it as one
    # comment payload on close() (verified against the stdlib directly) —
    # _HTMLText has no handle_comment override, so that payload, and every
    # real tag/text node inside it, is silently discarded with no error and
    # no INGEST_EMPTY signal (text before the dangling "<!--" already made it
    # through). A truncated network fetch or one forgotten "-->" would
    # otherwise drop the rest of the document with zero indication anything
    # was lost. Neutralize a genuinely unbalanced "<!--" by closing it
    # immediately (an empty comment) so real content after it still parses.
    # Every "<!--" that never sees a "-->" before the next "<!--" or EOF is an
    # unclosed comment — neutralize EACH of them (right-to-left so earlier
    # offsets stay valid). Closing only the LAST one leaves an earlier open to
    # swallow everything up to the injected comment's own "-->".
    opens = [m.start() for m in re.finditer(r"<!--", html) if _outside_tag(html, m.start())]
    for i in range(len(opens) - 1, -1, -1):
        pos = opens[i]
        end = opens[i + 1] if i + 1 < len(opens) else len(html)
        # The first "-->" after an opener is its closer at parse level —
        # comment content does not respect tag regions, so no outside-tag
        # filter applies (an "<!--" open inside a tag can't exist here:
        # such candidates were already excluded from `opens`).
        closed = re.search(r"-->", html[pos + 4 : end]) is not None
        if not closed:
            html = html[:pos] + "<!---->" + html[pos + 4 :]
    # Same neutralization technique for an unbalanced <noscript>/<template>:
    # find the last unmatched opening tag and inject a synthetic closer
    # immediately after it (converting it to an empty, already-closed
    # element), so _skip_depth doesn't stay elevated for the rest of the
    # document and swallow all subsequent body content.
    for open_re, close_re, tag in _SKIP_TAG_BALANCE:
        # Pair opens/closes in document order with a stack: closer injection
        # must target the *unmatched* opener. Putting all missing closers
        # after the LAST opener breaks when an earlier open was left unclosed
        # but the last one is properly paired — the injected closer converts
        # the well-formed element into an empty one and its boilerplate text
        # leaks into the output.
        spans = _comment_spans(html)
        events = sorted(
            [(m.end(), True) for m in open_re.finditer(html) if _live(html, m.start(), spans)]
            + [(m.end(), False) for m in close_re.finditer(html) if _live(html, m.start(), spans)]
        )
        unmatched: list[int] = []
        for end, is_open in events:
            if is_open:
                unmatched.append(end)
            elif unmatched:
                unmatched.pop()
        # Insert right-to-left so earlier offsets stay valid.
        for end in reversed(unmatched):
            gt = html.find(">", end)
            if gt != -1:
                # One injected closer per unmatched open so the skip-depth
                # counter returns to zero rather than staying elevated for
                # the rest of the document.
                html = html[: gt + 1] + f"</{tag}>" + html[gt + 1 :]
    parser = _HTMLText()
    parser.feed(html)
    raw = "".join(parser.parts)
    lines = [ln.strip() for ln in raw.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return "".join(parser.title_parts).strip(), text


def pdf_to_text(data: bytes) -> tuple[str, int]:
    """Extract text per page, tolerating per-page failures.

    Returns (text, n_failed_pages): pages whose extract_text() raised are
    dropped — a malformed content stream on one page must not discard the
    rest — but the caller learns HOW MANY were lost so it can warn instead
    of silently indexing a partial document."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise IngestError(
            "INGEST_PDF_SUPPORT_MISSING",
            "pypdf is not installed; install with: pip install pypdf",
        ) from exc
    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise IngestError("INGEST_PARSE_FAILED", f"PDF parse failed: {exc}") from exc
    # Extract each page independently: a malformed content stream, bad font,
    # or corrupt xref entry on ONE page (a real pypdf failure mode) must not
    # discard every other page's perfectly good text — matches the project's
    # own graceful-degradation principle (CLAUDE.md: "Studio outputs have
    # fallback text... History_messages() survives malformed chats"), already
    # applied the same way to per-batch embedding failures in pipeline.py.
    # Iterate by index: pypdf resolves page objects lazily, so `pages[i]` itself
    # can raise (corrupt xref entry) before extract_text() is ever reached —
    # an iterator would abort the whole document on that page instead of
    # skipping it. Enumerating the sequence at all is the document-level
    # failure case and maps to the same parse error as construction.
    try:
        page_seq = reader.pages
        n_pages = len(page_seq)
    except Exception as exc:
        raise IngestError("INGEST_PARSE_FAILED", f"PDF page list failed: {exc}") from exc
    pages: list[str] = []
    n_failed = 0
    for i in range(n_pages):
        try:
            pages.append(page_seq[i].extract_text() or "")
        except Exception:
            n_failed += 1
            continue
    return "\n\n".join(p.strip() for p in pages if p.strip()), n_failed


# --- SSRF guard -----------------------------------------------------------


def _validate_resolved(host: str) -> str:
    """Resolve a host, reject any non-public address, return one validated IP.

    The returned IP literal is what callers must connect to: resolving once and
    pinning the result closes the DNS-rebinding window between check and connect.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise IngestError("INGEST_FETCH_FAILED", f"DNS resolution failed: {host}") from exc
    chosen = ""
    for info in infos:
        raw_addr = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_addr)
        except ValueError as exc:
            # Zone-scoped link-local addresses (e.g. "fe80::1%eth0") are not
            # accepted by ip_address(). They are inherently non-public, so
            # reject them with the same error as other blocked addresses.
            raise IngestError(
                "INGEST_URL_BLOCKED",
                f"host resolves to non-public address: {raw_addr!r}",
            ) from exc
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise IngestError("INGEST_URL_BLOCKED", f"host resolves to non-public address: {ip}")
        if not chosen:
            chosen = str(info[4][0])
    if not chosen:
        raise IngestError("INGEST_FETCH_FAILED", f"no address for host: {host}")
    return chosen


def validate_public_url(url: str) -> tuple[urllib.parse.ParseResult, str]:
    """Reject non-http(s) schemes and hosts resolving to non-public addresses.

    Returns (parsed_url, pinned_ip) so callers can connect to the validated IP
    directly without a second DNS lookup.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise IngestError("INGEST_URL_BLOCKED", f"scheme not allowed: {parsed.scheme!r}")
    if not parsed.hostname:
        raise IngestError("INGEST_URL_BLOCKED", "URL has no host")
    try:
        # .port is lazy: urlparse does not validate the port field until it is
        # accessed, so ':abc' / out-of-range / negative ports raise ValueError
        # here rather than mid-request (fetch_url reads .port outside its
        # IngestError handling — this is the same 400-vs-500 defect class as
        # the zone-scoped IPv6 fix, v0.2.45).
        _ = parsed.port
    except ValueError as exc:
        raise IngestError("INGEST_URL_BLOCKED", f"invalid port: {exc}") from exc
    pinned = _validate_resolved(parsed.hostname)
    return parsed, pinned


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a pre-validated IP, not a fresh lookup."""

    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS variant: connect to the pinned IP but keep SNI/cert on the hostname."""

    def __init__(
        self,
        host: str,
        port: int,
        pinned_ip: str,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip
        self._ssl_context = context

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._ssl_context.wrap_socket(
                raw, server_hostname=self.host
            )
        except Exception:
            # On handshake failure the SSLSocket may never take ownership of
            # the fd — close the raw socket so each failed TLS attempt does
            # not leak one fd.
            raw.close()
            raise


_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


def _inflate(body: bytes, wbits: int, *, multi_member: bool = False) -> bytes:
    """Inflate with the output bounded DURING decompression: a small encoded body
    can expand ~1000x per layer, so checking the size afterwards is too late."""
    out = b""
    data = body
    while True:
        d = zlib.decompressobj(wbits)
        out += d.decompress(data, MAX_UPLOAD_BYTES + 1 - len(out))
        _check_size(out)
        if not d.eof:
            raise zlib.error("incomplete or truncated stream")
        data = d.unused_data.lstrip(b"\x00")
        if not (multi_member and data):
            return out


def _decode_content_encoding(header: str | None, body: bytes) -> bytes:
    """Decode a Content-Encoding response body; refuse what we cannot decode.

    fetch_url never sends Accept-Encoding, so a spec-compliant server replies
    unencoded — but some hosts and CDNs gzip unconditionally, and http.client
    does not decode it transparently. Raw gzip bytes would then reach
    _decode()'s cp932 fallback (which accepts any byte sequence) and index
    mojibake into the notebook with zero signal. Encodings are applied in
    reverse order (the header lists them in application order); anything we
    cannot decode (br, zstd, …) fails cleanly rather than poisoning the index.
    """
    encodings = [e.strip().lower() for e in (header or "").split(",") if e.strip()]
    for enc in reversed(encodings):
        if enc == "identity":
            continue
        if enc in ("gzip", "x-gzip"):
            try:
                body = _inflate(body, 16 + zlib.MAX_WBITS, multi_member=True)
            except zlib.error as exc:
                raise IngestError("INGEST_FETCH_FAILED", f"corrupt gzip body: {exc}") from exc
        elif enc == "deflate":
            try:
                body = _inflate(body, zlib.MAX_WBITS)
            except zlib.error:
                try:
                    body = _inflate(body, -zlib.MAX_WBITS)
                except zlib.error as exc:
                    raise IngestError(
                        "INGEST_FETCH_FAILED", f"corrupt deflate body: {exc}"
                    ) from exc
        else:
            raise IngestError("INGEST_UNSUPPORTED_FORMAT", f"unsupported Content-Encoding: {enc}")
    if encodings:
        # The wire cap bounded the encoded form; bound the inflated form too.
        _check_size(body)
    return body


def fetch_url(url: str) -> tuple[bytes, str, str]:
    """Fetch a public URL. Returns (body, content_type, final_url).

    Every hop is re-validated and the connection is pinned to the validated IP,
    so a host cannot rebind DNS to a private address between check and connect.
    """
    current = url
    seen_urls: set[str] = set()
    for _ in range(URL_MAX_REDIRECTS + 1):
        if current in seen_urls:
            raise IngestError("INGEST_URL_BLOCKED", "redirect cycle detected")
        seen_urls.add(current)
        parsed, pinned = validate_public_url(current)
        host = parsed.hostname or ""
        default_port = 443 if parsed.scheme == "https" else 80
        port = parsed.port or default_port
        # RFC 7230 §5.4: Host header must include port when non-default.
        host_header = host if port == default_port else f"{host}:{port}"
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        if parsed.scheme == "https":
            conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
                host, port, pinned, URL_TIMEOUT_SEC, ssl.create_default_context()
            )
        else:
            conn = _PinnedHTTPConnection(host, port, pinned, URL_TIMEOUT_SEC)
        try:
            conn.request(
                "GET",
                path,
                headers={"User-Agent": f"shoin/{VERSION}", "Host": host_header},
            )
            resp = conn.getresponse()
            if resp.status in _REDIRECT_CODES:
                location = resp.getheader("Location")
                if not location:
                    raise IngestError("INGEST_FETCH_FAILED", "redirect without Location")
                current = urllib.parse.urljoin(current, location)
                continue
            if resp.status >= 400:
                raise IngestError("INGEST_FETCH_FAILED", f"HTTP {resp.status} for {current}")
            body = resp.read(MAX_UPLOAD_BYTES + 1)
            if not body:
                raise IngestError("INGEST_EMPTY", f"server returned empty body for {current}")
            _check_size(body)
            body = _decode_content_encoding(resp.getheader("Content-Encoding"), body)
            ctype = resp.getheader("Content-Type") or ""
            return body, ctype, current
        except (OSError, http.client.HTTPException) as exc:
            raise IngestError("INGEST_FETCH_FAILED", f"fetch failed: {exc}") from exc
        finally:
            conn.close()
    raise IngestError("INGEST_URL_BLOCKED", "too many redirects")


# --- public API -----------------------------------------------------------


def extract_file(path: Path | str) -> Extracted:
    """Extract text from a local file (kind inferred from extension)."""
    p = Path(path)
    kind = _EXT_KIND.get(p.suffix.lower())
    if kind is None:
        raise IngestError("INGEST_UNSUPPORTED_FORMAT", f"unsupported extension: {p.suffix!r}")
    try:
        # Size-gate on stat() before read_bytes() — a huge local file must be
        # rejected without loading it into memory just to learn it is over the
        # limit. read_bytes() still feeds _check_size below: the file could
        # grow between the stat and the read.
        if p.stat().st_size > MAX_UPLOAD_BYTES:
            raise IngestError(
                "INGEST_FILE_TOO_LARGE",
                f"source exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit",
            )
        data = p.read_bytes()
    except OSError as exc:
        raise IngestError("INGEST_FETCH_FAILED", f"cannot read file: {exc}") from exc
    _check_size(data)
    title = p.name
    pages_failed = 0
    if kind == "pdf":
        text, pages_failed = pdf_to_text(data)
    elif kind == "html":
        html_title, text = html_to_text(_decode(data))
        title = html_title or title
    else:
        text = _decode(data)
    # Strip null bytes (U+0000): str.strip() skips them (category Cc, not whitespace),
    # so a file containing only \x00 bytes would pass the `not text` guard without this.
    text = text.replace("\x00", "").strip()
    if not text:
        raise IngestError("INGEST_EMPTY", f"no extractable text in {p.name}")
    return Extracted(kind, title, text, str(p), _digest(data), pages_failed)


def _charset_from_ctype(ctype: str) -> str | None:
    """Extract the charset parameter from a Content-Type header value."""
    for part in ctype.split(";"):
        kv = part.strip().split("=", 1)
        if len(kv) == 2 and kv[0].strip().lower() == "charset":
            return kv[1].strip().strip('"').strip("'")
    return None


def extract_url(url: str) -> Extracted:
    """Fetch and extract text from a public URL (html / pdf / plain text)."""
    body, ctype, final_url = fetch_url(url)
    low = ctype.lower()
    charset = _charset_from_ctype(ctype)
    pages_failed = 0
    if "pdf" in low or body.lstrip()[:4] == b"%PDF":
        text, pages_failed = pdf_to_text(body)
        title = final_url
    elif "html" in low or body.lstrip()[:1] == b"<":
        title, text = html_to_text(_decode(body, charset))
        title = title or final_url
    else:
        text, title = _decode(body, charset), final_url
    # Strip null bytes (U+0000): str.strip() skips them (category Cc, not
    # whitespace), so content containing only \x00 bytes would pass `not text`
    # unchanged.  The same guard was applied to extract_file() in v0.2.50.
    text = text.replace("\x00", "").strip()
    if not text:
        raise IngestError("INGEST_EMPTY", f"no extractable text at {url}")
    return Extracted("url", title, text, final_url, _digest(body), pages_failed)
