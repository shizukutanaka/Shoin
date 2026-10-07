"""Opt-in structured event log (SHOIN_LOG_JSON) — product-review #45.

SHOIN_DEBUG prints human-readable retrieval diagnostics; this module is the
machine-readable half. When SHOIN_LOG_JSON is truthy, emit() writes one JSON
object per line to stderr — ``{"ts": ISO-8601, "event": name, ...}`` —
so a wrapper script or `tail -f | jq` can observe real-use latency and
degradation rates without the privacy cost of telemetry. stderr is the
channel for the same reason every diagnostic is: stdout stays machine-
readable output only.

Privacy boundary (the same local-first contract as everything else): events
carry counts, ids and timings — never user content. Field names in
_PRIVATE_FIELDS are dropped from every event, so a future call site cannot
accidentally leak a question body or a file path into the log.
"""

from __future__ import annotations

import json
import sys
import unicodedata

from .config import log_json_enabled
from .store import _now

_PRIVATE_FIELDS = (
    "question",
    "text",
    "title",
    "origin",
    "url",
    "path",
    "content",
    "body",
)


def emit(event: str, **fields: object) -> None:
    """Write one JSON event line to stderr; a silent no-op when disabled.

    Never raises — a logging failure must not break the operation it reports
    (default=str keeps non-JSON values serializable; a broken stderr is
    swallowed). Keys colliding with _PRIVATE_FIELDS are dropped, not emitted.
    """
    if not log_json_enabled():
        return
    safe = {k: v for k, v in fields.items() if k not in _PRIVATE_FIELDS}
    line = json.dumps(
        {"ts": _now(), "event": event, **safe},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    try:
        print(line, file=sys.stderr)
    except (OSError, ValueError, UnicodeEncodeError):
        pass


# v0.2.717: terminal-injection hygiene for untrusted bytes. Stored fields
# (titles, origins, imported notebook names, message/studio bodies, section
# breadcrumbs, suggestion terms) are attacker-controlled — a crafted export
# document or hostile local-LLM endpoint can smuggle ESC sequences (color
# rewrite, OSC 8 links, OSC 52 clipboard write, cursor/home) or Cf bidi
# overrides (Trojan Source-style visual spoofing) that the terminal
# interprets when printed. Escaping preserves the row/line shape and keeps
# the original bytes readable as literal escapes.
_ESCAPED_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")


def one_line(text: str) -> str:
    """Render an externally-controlled string safe for single-line output.

    Status rows and label fields are emitted one-per-line; a stored title,
    CLI argument, or env value containing a control character (\n, \r, ESC,
    U+2028…) would split the row or rewrite earlier terminal output — a
    forged `✓` line is indistinguishable from a real one.
    """
    out: list[str] = []
    for ch in text:
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif unicodedata.category(ch) in _ESCAPED_CATEGORIES:
            cp = ord(ch)
            out.append(f"\\x{cp:02x}" if cp < 0x100 else f"\\u{cp:04x}")
        else:
            out.append(ch)
    return "".join(out)


def safe_text(text: str) -> str:
    """Like one_line but keeps \n and \t for multi-line untrusted bodies.

    LLM answers, stored message bodies, and Studio output are printed as
    real multi-line text; escaping newlines would destroy the layout the
    user asked for. \r is still escaped (it overwrites the current line),
    as are every other Cc/Cf/Zl/Zp codepoint.
    """
    out: list[str] = []
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
        elif ch == "\r":
            out.append("\\r")
        elif unicodedata.category(ch) in _ESCAPED_CATEGORIES:
            cp = ord(ch)
            out.append(f"\\x{cp:02x}" if cp < 0x100 else f"\\u{cp:04x}")
        else:
            out.append(ch)
    return "".join(out)
