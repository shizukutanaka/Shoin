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
