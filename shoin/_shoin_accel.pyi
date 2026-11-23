"""Type stub for the optional Rust extension (rust/shoin-accel/).

The real module is a compiled artifact that is not committed; this stub
lets mypy type-check `shoin/accel.py` against the native signatures in
every environment, with or without the .so present.
"""

from collections.abc import Iterator

__version__: str

def vector_topk(
    query: list[float],
    rows: Iterator[tuple[int, bytes, float | None]],
    k: int,
) -> list[tuple[int, float]]: ...
def needle_pool(
    rows: Iterator[tuple[int, str, str]],
    needles: list[str],
    cap: int,
    ctx_weight: float,
) -> list[int]: ...
def estimate_tokens(text: str, bounds: bytes) -> int: ...
def tail(text: str, tokens: int, bounds: bytes) -> str: ...
