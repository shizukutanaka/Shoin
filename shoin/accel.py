"""Optional Rust acceleration for the compute-bound kernels.

Shoin's contract is zero required dependencies on the pure-Python path.
The Rust kernels (rust/shoin-accel/, built into the optional
`shoin._shoin_accel` extension module) are an accelerator only:

* importable + `SHOIN_ACCEL` on (the default) → the kernels below route
  to Rust;
* module missing (a normal pip install), `SHOIN_ACCEL=0`, or *any*
  failure inside a native call → every function here returns None and
  the caller runs the original pure-Python implementation instead.

The native kernels re-implement the Python semantics operation-for-
operation (bit-identical scores and ordering), so which path ran is
invisible in the results — only in the timing. Nothing here raises:
the fallback decision is made by the callers on the None return.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from . import config

try:
    from . import _shoin_accel
except ImportError:
    _shoin_accel = None  # type: ignore[assignment]

# Evaluated once at import: config reads hit the filesystem (config.json),
# and estimate_tokens/tail sit in per-chunk hot loops, so the switch must
# not be re-read per call. Toggling SHOIN_ACCEL means a fresh process,
# like every other startup-time setting.
_ENABLED: bool = _shoin_accel is not None and config.accel_enabled()

_R = TypeVar("_R")


def _call(func: Callable[..., _R], *args: Any) -> _R | None:
    """One guarded native call: any failure degrades to the fallback.

    The broad catch is the contract, not sloppiness — a native bug must
    never take down a search or an ingest that pure Python can serve.
    (Catalogued in the except-handler inventory like every broad catch.)
    """
    try:
        return func(*args)
    except Exception:
        return None


def available() -> bool:
    """True when the native kernels are present and enabled."""
    return _ENABLED


def vector_topk(
    query: list[float],
    rows: Iterator[tuple[int, bytes, float | None]],
    k: int,
) -> list[tuple[int, float]] | None:
    """Cosine top-k over embedding rows, or None to use the Python scan.

    `rows` yields (chunk_id, embedding blob, stored norm or None) in the
    same order the SQL scan would visit them; the result is the ranked
    (chunk_id, score) list with only strictly positive scores, ties in
    row order — exactly `vector_search`'s own selection.
    """
    native = _shoin_accel
    if native is None or not _ENABLED:
        return None
    return _call(native.vector_topk, query, rows, k)


def needle_pool(
    rows: Iterator[tuple[int, str, str]],
    needles: list[str],
    cap: int,
    ctx_weight: float,
) -> list[int] | None:
    """LIKE-fallback candidate pool (chunk ids, pool order), or None.

    Mirrors the selection/scoring of bm25_search's SQL CTE (ASCII-folded
    occurrence counts + the flat context bonus); the final per-chunk
    scoring stays in Python. See the Rust module docs for the equivalence
    argument.
    """
    native = _shoin_accel
    if native is None or not _ENABLED:
        return None
    return _call(native.needle_pool, rows, needles, cap, ctx_weight)


def estimate_tokens(text: str, bounds: bytes) -> int | None:
    """`chunk.estimate_tokens` via Rust, or None to use the Python body.

    `bounds` is chunk's packed CJK range table (the table keeps a single
    owner; it is passed per call rather than duplicated in Rust).
    """
    native = _shoin_accel
    if native is None or not _ENABLED:
        return None
    return _call(native.estimate_tokens, text, bounds)


def tail(text: str, tokens: int, bounds: bytes) -> str | None:
    """`chunk._tail` via Rust, or None to use the Python body."""
    native = _shoin_accel
    if native is None or not _ENABLED:
        return None
    return _call(native.tail, text, tokens, bounds)
