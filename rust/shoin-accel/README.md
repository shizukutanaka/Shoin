# shoin-accel — optional Rust accelerator

A PyO3 extension module (`shoin._shoin_accel`) that accelerates Shoin's
compute-bound kernels:

- **vector search** — the full-table cosine scan (`vector_topk`)
- **BM25 LIKE fallback** — candidate-pool selection (`needle_pool`)
- **ingest text processing** — `estimate_tokens` and the `_tail` cutter

Shoin's contract is unchanged: **zero required dependencies, pure Python
always works**. The accelerator is strictly optional — `shoin/accel.py`
imports it when present and silently falls back to the pure-Python path
when it is missing, disabled (`SHOIN_ACCEL=0`), or raises. Scores and
ranking order are bit-identical between the two paths (the Rust kernels
re-implement the Python semantics operation-for-operation; see the
module docs in `src/lib.rs`).

## Build

Requires a Rust toolchain and Python 3.11+ headers (the interpreter you
run Shoin with):

```sh
rust/shoin-accel/build.sh   # cargo build --release + copy into shoin/
```

This produces `shoin/_shoin_accel.so` (git-ignored build artifact). To
remove the accelerator, delete that file — Shoin returns to the pure
Python path with no other change.

## Verify

```sh
python3 -c "import shoin.accel; print(shoin.accel.available())"  # True
SHOIN_ACCEL=0 python3 -c "import shoin.accel; print(shoin.accel.available())"  # False
```
