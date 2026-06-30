"""Source ingestion: text extraction for files and URLs with SSRF guards.

Supported kinds: txt, md, html, pdf, url. All extraction is local; URL fetch
is the only network path and is restricted to public http(s) hosts.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import urllib.parse
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
    " section article header footer nav aside main"
    " blockquote pre dd dt dl figure figcaption".split()
)


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
    """Minimal stdlib HTML -> text extractor (skips script/style, keeps blocks)."""

    # Remove "title" from Python's RCDATA_CONTENT_ELEMENTS so the tokenizer does
    # not enter raw-text mode on <title> — otherwise </noscript> (or any other tag)
    # inside an unclosed <title> is consumed as text rather than fired as a closing
    # tag, leaving _skip_depth permanently > 0 and silently swallowing the body.
    RCDATA_CONTENT_ELEMENTS = ("textarea",)

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self._skip_depth += 1
        elif tag == "title" and not self._skip_depth:
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            if self._in_title:
                self._in_title = False  # implicit close: block content can't appear inside <title>
            self.parts.append("\n")
        elif tag in ("body", "html") and self._in_title:
            self._in_title = False  # structural tag implies <title> was never properly closed

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "head":
            # An unclosed <noscript>/<script>/<style> in <head> must not leak into
            # <body> and swallow all body text.  Reset both guards at </head> so
            # malformed markup like <noscript>fallback</head><body>Content</body>
            # still extracts "Content" rather than raising INGEST_EMPTY.
            self._skip_depth = 0
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


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, text) extracted from an HTML document."""
    parser = _HTMLText()
    parser.feed(html)
    raw = "".join(parser.parts)
    lines = [ln.strip() for ln in raw.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return "".join(parser.title_parts).strip(), text


def pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise IngestError(
            "INGEST_PDF_SUPPORT_MISSING",
            "pypdf is not installed; install with: pip install pypdf",
        ) from exc
    try:
        reader = PdfReader(BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise IngestError("INGEST_PARSE_FAILED", f"PDF parse failed: {exc}") from exc
    return "\n\n".join(p.strip() for p in pages if p.strip())


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
        except ValueError:
            # Zone-scoped link-local addresses (e.g. "fe80::1%eth0") are not
            # accepted by ip_address(). They are inherently non-public, so
            # reject them with the same error as other blocked addresses.
            raise IngestError(
                "INGEST_URL_BLOCKED",
                f"host resolves to non-public address: {raw_addr!r}",
            )
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
        self.sock = self._ssl_context.wrap_socket(raw, server_hostname=self.host)


_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


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
            conn.request("GET", path, headers={"User-Agent": f"shoin/{VERSION}", "Host": host_header})
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
        data = p.read_bytes()
    except OSError as exc:
        raise IngestError("INGEST_FETCH_FAILED", f"cannot read file: {exc}") from exc
    _check_size(data)
    title = p.name
    if kind == "pdf":
        text = pdf_to_text(data)
    elif kind == "html":
        html_title, text = html_to_text(_decode(data))
        title = html_title or title
    else:
        text = _decode(data)
    text = text.strip()
    if not text:
        raise IngestError("INGEST_EMPTY", f"no extractable text in {p.name}")
    return Extracted(kind, title, text, str(p), _digest(data))


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
    if "pdf" in low or body.lstrip()[:4] == b"%PDF":
        text, title = pdf_to_text(body), final_url
    elif "html" in low or body.lstrip()[:1] == b"<":
        title, text = html_to_text(_decode(body, charset))
        title = title or final_url
    else:
        text, title = _decode(body, charset), final_url
    text = text.strip()
    if not text:
        raise IngestError("INGEST_EMPTY", f"no extractable text at {url}")
    return Extracted("url", title, text, final_url, _digest(body))
