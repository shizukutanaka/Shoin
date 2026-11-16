# Performance measurements

Measured performance record for Shoin's main user paths. Numbers are
medians from the benchmark harness described below; the harness itself
is reproducible from the method section. Absolute values come from a
throttled shared VM and are inflated by contention — treat ratios and
same-process A/B comparisons as the reliable signal, not the raw
milliseconds.

## Method

- **Paths measured**: startup (`import shoin.cli`), ingest
  (`pipeline.index_source`, embeddings off), chunking (inside ingest),
  search (`search.bm25_search`, `bm25_prf_search`, `vector_search`,
  `retrieve` in BM25-only and hybrid modes), citation verification
  (`citation.make_report`).
- **Corpus**: synthetic Japanese paragraphs (~400 chars per chunk,
  headings, topic vocabulary, numeric and era-name expressions),
  ingested through the real pipeline at the default 512-token chunk
  size. Embeddings are deterministic pseudo-vectors (768-dim) written
  through `store.set_embedding`, so vector timings measure the scan
  itself, not an embedder.
- **Scales**: 510 / 5,101 / 20,405 / 48,974 chunks in one notebook
  (the product cap is 50,000 chunks per notebook).
- **Protocol**: 1 warmup + 5 repetitions per query, median reported.
  "Before" is `main` at v0.2.746; "after" is v0.2.747.
- **Environment**: shared VM, 1.5 vCPU cap, 6 GB RAM, Python 3.12.3,
  SQLite 3.45.1. Other tenants compete for the same CPUs, so repeated
  runs of unchanged code vary by tens of percent.

## Findings (before the fix)

| Path | 510 chunks | 5,101 | 20,405 | 48,974 |
|---|---|---|---|---|
| ingest (total) | 0.81 s | 3.75 s | 25.6 s | 37.4 s |
| bm25, era/numeric query 「令和6年の予算」 | **990 ms** | **5,267 ms** | **12,062 ms** | **22,604 ms** |
| bm25, 2-char query 「経済」 (LIKE path) | 11 ms | 200 ms | 635 ms | ~750 ms |
| bm25, FTS-covered long query | 3–7 ms | 13–33 ms | 23–95 ms | 77–214 ms |
| vector_search (any query) | ~40 ms | ~450 ms | ~900 ms | ~2,000 ms |
| citation report (8 sources) | — | 24.5 ms | — | — |
| startup `import shoin.cli` | 0.2–0.5 s (all scales; store-independent) | | | |

One path dominated everything else by an order of magnitude:
**numeric / era-name queries in the BM25 LIKE fallback**.

Root cause, decomposed at 510 chunks (query 「令和6年の予算」): the era
bridge expands the query to ~75 substring needles (the era year plus
its digit-script variants). The fallback SQL evaluated the density
score in `ORDER BY`, recomputing `LOWER(c.text)` and
`LENGTH(LOWER(c.text))` for every (row × needle) pair:

| Component | Time |
|---|---|
| FTS query alone | 8 ms |
| LIKE `WHERE` only | 30 ms |
| LIKE + `ORDER BY` score expression | 884 ms |
| Python-side `_needle_score` over the same rows | 84 ms |

## Fix (v0.2.747)

`shoin/search.py`, `bm25_search` only; the score expression, weights,
2,000-row cap, and tie-break are unchanged:

1. The candidate scan is wrapped in a `WITH m AS MATERIALIZED (...)`
   CTE that computes `LOWER(text)` / `LOWER(context)` and their
   lengths **once per row**.
2. Each needle's occurrence-count term is guarded by
   `instr(lt, needle) > 0`, skipping the `REPLACE`-based count when
   the needle is absent (the count is definitionally 0 there).

**Equivalence**: pre-change and post-change code return identical
(chunk_id, score) lists for 22 queries × k ∈ {8, 24} on the 510- and
5,101-chunk stores, and for the numeric query on the 48,974-chunk
store. A regression test pins the cap's observable contract (past
2,000 candidates, the densest rows still win).

## Results (medians, ms)

bm25_search, 「令和6年の予算」:

| Chunks | Before | After | Speedup |
|---|---|---|---|
| 510 | 990 | 117 | 8.5× |
| 5,101 | 5,267 | 1,049 | 5.0× |
| 20,405 | 12,062 | 3,858 | 3.1× |
| 48,974 | 22,604* | 9,160 | 2.5× |

\* single-call measurement in a shared process (the full before-grid
at this scale was aborted for cost); after is the grid median.

`retrieve()` (hybrid) on the same query: 838 → 147 ms (510),
5,806 → 1,299 ms (5,101), 12,928 → 4,697 ms (20,405),
11,030 ms after at 48,974 (no before-grid value).

Other queries did not regress: 「経済」 bm25 200 → 98 ms (5,101),
635 → 358 ms (20,405); FTS-covered queries unchanged within noise.

## Measured and deliberately not changed

- **vector_search linear scan** (~40–100 µs/chunk): a two-phase
  prototype that defers text fetches to the top-k gained only ~3% in
  A/B — the dot product itself dominates. Beating it needs an index
  or a storage-format change, not a local tweak.
- **Ingest** (~1 ms/chunk at small scales): tokenization + FTS
  insert; not on any interactive critical path.
- **Startup** (~0.2–0.5 s on this VM): import-graph bound, flat
  across store sizes.
- **Citation verification** (~25 ms for 8 sources): negligible next
  to retrieval.

## Residual known cost

At the 50,000-chunk notebook cap, the needle-rich LIKE path still
takes ~9 s on this throttled VM (expect several× faster on a desktop
CPU). The residue is inherent to substring occurrence counting over
~41k candidate rows × ~75 needles. Removing it needs an n-gram
index — a storage-format change, deliberately not bundled into this
fix and left for a separate decision.
