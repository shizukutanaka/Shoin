# CLAUDE.md: Shoin (書院) Developer Guide

## Project Overview

**Shoin** is a local, citation-verified alternative to NotebookLM designed for offline research and knowledge work. It bundles PDF, Markdown, HTML, TXT files and URLs into "notebooks" and allows you to ask questions grounded strictly in those sources. Unlike cloud-based alternatives, all data stays local and no queries are sent externally. The name (書院 / shoin) means a scholarly study or library room in classical Japanese architecture.

**Key Design Principle**: Lightweight. Shoin targets 4–8 GB RAM systems running small open-source LLMs (Qwen3-4B, Phi-4, Gemma-3), not large proprietary models. It works without an LLM entirely—falling back to retrieval only when the endpoint is unavailable.

**Core Use Case**: Researchers, students, and professionals read long documents (research papers, reports, books) and want to ask questions with full source citation verification, zero external dependencies, and confidence that their private documents never leave their machine.

### Architectural Layers

```
┌─────────────────────────────────────────────────┐
│           Web UI (single-file HTML)             │ User-facing
├─────────────────────────────────────────────────┤
│  Web Server (HTTP/SSE)  + CLI (subcommands)    │ API & I/O
├─────────────────────────────────────────────────┤
│  Q&A (query expansion, context build, prompts) │ Logic
│  Studio (5 output kinds), Citation verification │
│  Search (BM25 + vector, fusion, rerank, MMR)   │
├─────────────────────────────────────────────────┤
│  Ingest (extract text from PDF/HTML/URL)       │ Input
│  Chunk & Index (token-aware splitting, FTS5)   │
├─────────────────────────────────────────────────┤
│  Store (SQLite + WAL, migrations, FTS5 index)  │ Persistence
├─────────────────────────────────────────────────┤
│  LLMClient (OpenAI-compatible async wrapper)   │ LLM Bridge
└─────────────────────────────────────────────────┘
```

Each layer is independent: the pipeline works without an LLM, retrieval works at any scale, the UI degrades gracefully when unavailable.

---

## Core Concepts

### Source Grounding via S-numbers

Every answer and Studio output cites sources as `[S1]`, `[S2]`, etc. These are **not magic**: they are indices into the notebook's ranked retrieval results, regenerated fresh for each query. The mapping is transparent to the user; clicking `[S1]` jumps to the first relevant source.

**Why S-numbers?** Lightweight LLMs struggle with long source titles and URLs in the context window. `[S1] [S2]` takes 2 tokens; the full text of "https://example.com/very/long/research/paper#section2" takes 10+. For 2.4K token budget and 6-turn history, brevity matters.

**Citation Extraction** (`citation.py`):
- Pattern: `[SsＳｓ]` (ASCII/full-width S variants) followed by digits, optionally comma/semicolon/`and`-separated
- Examples: `[S1]`, `[Ｓ１]`, `[S1, S2]`, `[S1 and S3]` all parse correctly
- First pass: extract all cited numbers, validate they fall in range 1..N_sources

### Citation Verification: Four-Layer Machine Checks

Citation hallucination (fabricated quotes, wrong numbers, unsupported assertions) is one of the most user-visible LLM failure modes. Shoin runs four dependency-free, LLM-free checks on every assistant response and Studio output:

**1. Range Check** (`validate_citations`): Detect `[S99]` when only 5 sources exist. Out-of-range numbers are the narrowest, highest-confidence hallucination signal.

**2. Grounding Confirmation** (`verify_grounding`): A cited sentence's wording is compared to the source text using character-bigram overlap. When overlap >= 30% (CONFIRM_MIN, calibrated for CJK), the citation is flagged `confirmed` — strong positive evidence the claim is lexically supported.

**3. Mis-numbering Detection** (`verify_grounding`): When a sentence does *not* match its cited source but *does* strongly match a *different* source (with a 20% gap margin, MISMATCH_GAP), the citation number is flagged `misattributed` — the model likely cited the wrong source.

**4. Uncited-Assertion Detection** (`uncited_sentences`, v0.2.65): Checks 2 and 3 only ever examine sentences that *already* carry a citation. A hallucinated or simply unsupported claim with *zero* citations anywhere in it is invisible to those checks — this was docs/product-review.md's top-priority open gap. `uncited_sentences()` scans for sentences with no `[S#]` marker, resolving the common trailing-citation pattern ("Sentence. [S1]") the same way `verify_grounding()` does, and excludes trivial filler and explicit "not in the source" disclaimers (the *correct* response to missing facts, not an unsupported assertion).

**Key Design Decision**: Lexical overlap is asymmetric. High overlap reliably *confirms* support. Low overlap is inconclusive—a correct synonym paraphrase and a true misattribution both score ~0. So the checks only *assert* what they can stand behind (confirmation, or a wrong number, or a bare unfounded assertion) and *stay silent otherwise* rather than falsely accusing a correctly paraphrased answer. No aggregate grounding score is emitted; the `confirmed`, `misattributed`, and `uncited` lists are the complete signal. See CHANGELOG v0.1.4 for the design rationale.

### History Management: Stripping Stale Citations, Deduplicating Roles

Shoin supports multi-turn conversation by including up to 6 recent prior turns in the prompt context (HISTORY_MESSAGES=6, each truncated to 160 tokens to keep total budget ~2.4K). Challenges:

**Stale Citation Stripping**: When including message N-1 in the prompt for query N, the [S1]..[Sn] numbers from the previous context are stale—they don't match the fresh retrieval for query N. If the model echoes stale numbers, they become meaningless. Solution: remove all `[S#]` markers from history messages before re-prompting.

**Role Deduplication**: In parallel requests, Thread A may be retrieving while Thread B adds a new source and flushes its questions cache with an older fingerprint. Race condition: Thread A's cached result overwrites Thread B's newer one. After history_messages() processes chat turns and skips empty assistant turns, the msg sequence can become [user₁, user₂, asst₂] (two consecutive user turns), violating OpenAI API alternation. Solution: strip both trailing user turns at the end AND any consecutive same-role pairs in the middle (`history_messages`, v0.1.52).

### Vector + BM25 Hybrid Retrieval with MMR Diversity

Retrieval combines two signals:

**BM25 (Full-Text Search)** via SQLite FTS5 trigram tokenizer:
- Query `query_terms()` extracts ASCII words and contiguous CJK runs (e.g., "Shoin書院" → ["Shoin", "書院"])
- CJK terms are decomposed into trigrams: 書院 → [書院, 院理] etc. (prefix-free overlap for recall)
- ASCII terms >= 3 chars are kept as-is
- Each term is expanded into its width/script spellings by `term_variants()` (v0.2.144), so katakana↔hiragana (v0.2.42), fullwidth↔halfwidth kana (データ / ﾃﾞｰﾀ), and ASCII↔fullwidth ASCII (GPU / ＧＰＵ) all retrieve one another. FTS5's trigram tokenizer folds case but never width, and SQL LIKE folds neither, so the bridge is built query-side — indexed text stays byte-identical to the source
- FTS5 MATCH expression is OR-joined for recall; BM25() scoring ranks denser matches higher
- Fallback: LIKE scan for short queries (< 3 chars) when FTS5 index misses them, limited to 2000 rows

**Vector Search** (optional):
- Each chunk is embedded via `ChatBackend.embed_one(text)` when embeddings are enabled
- Cosine similarity is computed in-process over notebook chunks (local scale, no external API)
- Normalizes NaN/Inf results to 0.0 (guards against corrupted embeddings from broken endpoints)

**Fusion** (Reciprocal Rank Fusion, RRF — since v0.2.56):
- `rrf_fuse(bm25_hits, vec_hits)`: `score = Σ 1/(k + rank + 1)` with k=60 (Cormack SIGIR 2009), summed across whichever result lists a chunk appears in
- Uses only rank positions, bypassing the scale incompatibility between raw FTS5 BM25 values and cosine similarity scores in [0,1] — no per-query normalization or alpha tuning needed
- A chunk found by both BM25 (rank 1) and vector (rank 1) scores ≈0.0328; found by only one at rank 1 scores ≈0.0164, naturally combining both signals
- `fuse()`/`adaptive_alpha()` (the earlier convex-combination design: `score = alpha * vec_score + (1-alpha) * bm25_score`, with alpha adjusted for natural-language queries/identifiers/short keywords) still exist in `search.py` for backward compatibility with existing tests, but `retrieve()` no longer calls either — RRF scores are min-max normalized to [0,1] before being passed to the lexical MMR reranker below

**MMR Reranking** (Maximum Marginal Relevance):
- After fusion, the top-k hits are reranked to maximize relevance while minimizing redundancy
- Lexical overlap (character bigrams) measures diversity; the next selected hit must be both relevant and differ from already-selected hits
- Prevents result lists dominated by near-duplicate chunks (e.g., different pages of the same multi-page document)

**Degradation**: When embeddings are disabled, the pipeline uses pure BM25 (first-class mode, not a fallback).

### Markdown Newline Normalization

Markdown and plain-text sources arrive with mixed line endings: `\r\n` (Windows), `\r` (old Mac), `\n` (Unix). Inside HTML, extracted text may also have `<br>` tags or implicit paragraph breaks. Challenge: preserve intentional line breaks (e.g., poetry, lists) while normalizing incidental whitespace.

**Solution**: Chunks are split sentence-by-sentence first (regex on `.!？。；！`, plus `\n\n` paragraph boundaries). Within each sentence, **only** sequences of 2+ newlines are collapsed to a single space; single newlines become spaces. This preserves hard line breaks (poetry) while merging soft wraps (reflowing paragraphs).

See `chunk.py` `_hard_split()` and citation.py `_SENTENCE_SPLIT_RE`.

### WAL Mode + FTS5 Cascade Deletes

SQLite persistence uses Write-Ahead Logging (WAL) mode to allow concurrent reads while writes are pending. This is essential for the web server's `ThreadingHTTPServer` which spawns a thread per request.

**Cascade Deletes**: Every foreign key is `ON DELETE CASCADE`. When a source is deleted, all its chunks are deleted; when a notebook is deleted, all sources/chunks/notes/messages/studio_outputs in that notebook are deleted. The FTS5 virtual table is kept in sync via triggers (`chunks_ai`, `chunks_ad`), so the trigram index never accumulates orphaned entries.

**Migrations**: Schema changes are recorded in `MIGRATIONS` list (append-only). Each migration is tagged with a version number and applied once at startup. Migrations 1–4 are:
1. Initial schema (notebooks, sources, chunks, FTS5, notes, studio_outputs, messages)
2. Add `notebook_id` indexes (performance)
3. Add settings table (for tracking embed model version)
4. Add composite message index for recent-message retrieval

---

## Strengths (Audited and Correct)

### SSRF Guard with DNS Pinning

When a user adds a URL source, Shoin fetches the content over HTTP. An SSRF vulnerability would allow an attacker to request internal endpoints (e.g., `http://169.254.169.254/`) to extract cloud metadata. Shoin's defense:

1. **IP Pinning** (ADR-001): When resolving a hostname, the first resolution result is pinned. If the hostname later resolves to a different IP (DNS rebinding attack), the request is rejected.
2. **Localhost Rejection**: IPs in `127.0.0.0/8`, `::1/128`, and `169.254.0.0/16` (link-local) are rejected outright.
3. **HTTP Only**: `file://` and other schemes are not supported.

Code: `ingest.py` `fetch_url()` and `_check_ip_pinning()`.

### Atomic Chunk Insertion + Best-Effort Embedding

`add_chunks()` wraps all INSERT statements for a source in a single `with self.conn:` transaction; if any chunk INSERT fails (e.g., disk full) all are rolled back. The source row itself is committed separately by `add_source()` before `add_chunks()` is called, so a disk-full failure between those two steps could leave a source with zero chunks in the DB. In practice this is very unlikely (the source row was just written, so the same disk that accepted it will almost always accept the much larger chunk batch too). Embedding failures are always non-fatal: `_embed_chunks()` catches `LLMError` and leaves the source indexed for BM25-only retrieval.

### Token-Aware Truncation

The context window is budgeted: 2400 tokens, allocated as:
- ~900 tokens: system prompt + source headers
- ~1000 tokens: source text (split equally across TOP_K sources)
- ~400 tokens: recent history (6 messages, 160 each)
- ~100 tokens: user query

When truncating source text to fit the budget, naive character truncation would split mid-word or mid-UTF-8-sequence. Solution: `chunk.py` `_truncate_tokens(text, limit)` scans left-to-right, counts tokens via `estimate_tokens()` (which models LLM tokenizer behavior for CJK and punctuation), and stops exactly at the limit without mangling characters.

Token estimation models:
- ASCII words: 1 token per word (~4 chars)
- CJK characters (including Hiragana/Katakana/Hangul/Thai/Lao/Myanmar/Khmer): 1 token per character
- Punctuation (。、　 etc.): 1 token per character

### Lexical Overlap Verification

The bigram-overlap check in `verify_grounding()` compares claim vs. source using character bigrams after NFKC normalization and whitespace stripping. Example:

```
Claim: "猫が好きです。"
Source: "私は猫が大好きです。犬も好きです。"
Bigrams claim: {"猫が", "が好", "好き", "き。"}
Bigrams source: {"私は", "は猫", "猫が", "が大", "大好", "好き", "き。", "。犬", "犬も", "も好", "好き", "き。"}
Overlap: {"猫が", "好き", "き。"} / 4 = 0.75 >= CONFIRM_MIN (0.30) → confirmed
```

The check is conservative: single bigrams like `好き` (common adjective suffix) alone won't trigger confirmation; the claim must hit 30% of its bigrams in the source. This avoids false positives from shared particles and formality markers.

---

## Known Weaknesses & Tech Debt

### Lightweight LLM Compatibility (Limited Token Budget)

**Problem**: Qwen3-4B, Phi-4, and similar models have smaller context windows (4K–8K tokens) than GPT-4 (128K). Shoin's 2400-token context budget is conservative, leaving room for prompt overhead. However, this means:

- Long sources are truncated; users may not see the full text.
- Rare multi-turn conversations (10+ turns) risk dropping early history due to token limit.
- Very large notebooks (500+ sources) retrieve only TOP_K=8 sources, potentially missing nuanced cross-source synthesis.

**Mitigation**: The design is intentional. Shallow context encourages focused, specific questions rather than open-ended exploration. For exploratory work, Studio outputs (briefing, timeline, mindmap) pre-synthesize the sources into digests.

### Embedding Batch Size (Closed, v0.2.124)

**Correction (v0.2.112)**: This section previously claimed `ChatBackend.embed_one(text)` embeds one chunk per HTTP request and that batching was unimplemented ("Ollama and llama.cpp have different batch API signatures... A batch API would require vendor detection"). That was never true of the actual code: `LLMClient.embed(texts: list[str])` (`llm.py`) has always sent a single `POST /embeddings` with `{"model": ..., "input": texts}` — the standard OpenAI-compatible batch shape — and `_embed_chunks()` (`pipeline.py`) has always preferred it over `embed_one()`, grouping texts into batches and issuing one HTTP request per batch. `embed_one()` exists only as a `ChatBackend` protocol convenience for single-text callers (e.g. query-time embedding in `search.py`) and as the fallback for a hypothetical backend that doesn't implement `embed()` at all — not the code path production ingest actually takes.

**Gap closed (v0.2.124)**: `EMBED_BATCH` (module default 16 in `pipeline.py`) is no longer the only option — `SHOIN_EMBED_BATCH` overrides it (`config.embed_batch()`, invalid/unset values fall back to the default). A 100-chunk source triggers `ceil(100/EMBED_BATCH)` embedding requests; a user running an endpoint that comfortably handles larger batches (or one that's memory-constrained and prefers smaller ones) can now tune this without editing source. See README's Configuration table.

### UI State Management Could Be More Robust

**Problem** (fixed in v0.2.21): SSE StrockError during streaming could orphan messages (user query saved, assistant response lost). History reconstruction would see dangling user turns and output malformed message sequences to the LLM.

**Fix** (`history_messages`, v0.1.52 onward):
- Catch connection errors during SSE and save partial responses.
- Deduplicate consecutive same-role turns: if a user turn is orphaned (no assistant reply), the next message processing removes it.

**Remaining**: Concurrent notebook switches during question-fetch could display wrong recommendations. This is a race condition in the browser (not the server). Low priority since the UI is single-user.

### No Distributed Tracing

**Problem**: In production deployments, debugging slow queries across the retrieval → LLM → response pipeline is opaque. No request ID, no server-side timing logs.

**Why Not Fixed**: Shoin targets single-machine deployment. Logging would bloat the binary and add I/O overhead. Users who debug often look at Flask/Django logs; Shoin's minimal HTTP server doesn't have equivalent introspection.

**Workaround**: Set `SHOIN_DEBUG=1` to print retrieval stats (BM25/vector hit counts, RRF ranks, final scores) to stderr — see "Debugging Aid" below. No request timing is captured (that part of the original "Workaround" claim was itself aspirational, not implemented); a request ID / timing log remains genuinely out of scope per "Why Not Fixed" above.

---

## Key Files & Sections

### Core Modules

**`store.py`** (Persistence Layer)
- `MIGRATIONS`: Schema definition (notebooks, sources, chunks, FTS5, settings)
- `Notebook`, `Source`, `Chunk`: dataclasses, frozen for safety
- `Store` class: transaction management, batch operations
- Key methods:
  - `add_source(notebook_id, kind, title, origin, sha256)` → Source, opens transaction
  - `set_embedding(chunk_id, vec, *, commit)` → updates chunks.embedding BLOB; commit=False for batch use
  - `sources_for_notebook(nb_id)` → list of sources ordered by id
  - `list_notebooks_with_counts()` → single LEFT JOIN query (no N+1)

**`search.py`** (Retrieval Engine)
- `Hit`: scored chunk + source metadata, carries detail dict for fusion info
- `query_terms()`: tokenize query into ASCII words + CJK runs
- `fts_query()`: build FTS5 MATCH expression (trigrams for CJK, quoted terms for ASCII)
- `bm25_search()`: FTS5 scoring + LIKE fallback
- `vector_search()`: cosine similarity over notebook chunks (if embeddings enabled)
- `adaptive_alpha()`: boost semantic search for natural-language queries
- `_minmax()`: min-max normalization for fusion
- `mmr()`: rerank hits by diversity (lexical bigram overlap)
- `retrieve()`: orchestrates BM25 + vector + fusion + rerank

**`citation.py`** (Citation Extraction & Verification)
- `extract_citations(text)`: find all [S#] markers, handle full-width variants
- `validate_citations()`: check against n_sources, split into valid/invalid
- `_bigrams()`: character-level bigrams for overlap calculation
- `verify_grounding()`: sentence-by-sentence comparison (source text vs. claim)
- `uncited_sentences()`: sentences with zero [S#] anywhere in them — catches unsupported assertions `verify_grounding()` never looks at (v0.2.65)
- `make_report()`: construct CitationReport with confirmed/misattributed/uncited lists
- `CitationReport` TypedDict: cited, invalid, coverage, source_map, source_id_map, confirmed, misattributed, uncited

**`chunk.py`** (Text Chunking & Tokenization)
- `is_cjk()`: Unicode range check (East Asian blocks + Thai/Lao/Myanmar/Khmer)
- `estimate_tokens()`: approximate token count for truncation
- `_hard_split()`: chunk text into sentences respecting token limit
- `_SENTENCE_SPLIT_RE`: regex boundary for `.!？。；！` + paragraph breaks
- Newline normalization: collapse 2+ newlines to space, preserve single newlines

**`ingest.py`** (Document Ingestion)
- `extract_url()`: fetch HTTP/HTTPS URLs, detect content type, extract text
- `_check_ip_pinning()`: DNS rebinding defense
- `index_source()`: main pipeline—fetch, extract, chunk, embed, commit
- Error classes: `IngestError` with stable codes (INGEST_UNSUPPORTED_FORMAT, INGEST_URL_BLOCKED, etc.)

**`pipeline.py`** (Orchestration)
- `index_source()`: wraps ingest + store + embed
- `reindex_notebook()`: re-embed all chunks with new embeddings model
- `_embed_chunks()`: batch embedding with partial-failure handling

**`qa.py`** (Question Answering)
- `ChatBackend` Protocol: minimal interface (chat, embed_one, embedding_model)
- `GroundedContext`: source_titles, block (flattened source text), source_ids
- `build_context()`: format TOP_K sources into [S1] [S2]... structure
- `build_messages()`: construct OpenAI-format message list (system + history + user)
- `expand_query()`: append prior user message for follow-up questions
- `history_messages()`: reconstruct chat history, strip stale [S#], deduplicate roles
- `ask()`: orchestrate retrieve → build_context → build_messages → chat → verify_grounding
- `suggest_questions()`: LLM-generated list of follow-up questions (cached per notebook fingerprint)

**`studio.py`** (Synthesis Outputs)
- `KINDS`: ["briefing", "study_guide", "faq", "timeline", "mindmap"]
- `overview_hits()`: sample sources equally (not just first K) for balanced synthesis
- `generate()`: LLM prompt → Studio output, attach citation report
- `suggest_questions()`: recommendations cached by source fingerprint

**`llm.py`** (OpenAI-Compatible Client)
- `LLMClient`: wraps urllib (no external deps)
- `_post()`: JSON request/response, handles HTTP errors, JSON decode errors, timeouts
- `chat()`: blocking single call, returns string
- `chat_stream()`: yields tokens as SSE deltas, guards against `{"error": "..."}` in stream
- `embed_one()`: embed single text
- `available()`: health check via `/models` endpoint
- Error codes: SYSTEM_LLM_TIMEOUT, SYSTEM_SERVICE_UNAVAILABLE, SYSTEM_LLM_BAD_RESPONSE, etc.

**`server.py`** (Web Server)
- Single-threaded HTTP handler per request (`ThreadingHTTPServer`)
- Bound to 127.0.0.1 only; rejects non-local Hostnames (DNS rebinding defense)
- Routes:
  - GET `/` → static index.html
  - GET/POST/DELETE `/api/notebooks/*` → CRUD
  - POST `/api/notebooks/{id}/sources` → ingest (file upload or URL)
  - GET `/api/notebooks/{id}` → full notebook payload; chat history is embedded as its `"messages"` array (there is no dedicated GET route for messages alone — a prior version of this doc incorrectly claimed one existed, v0.2.75)
  - DELETE `/api/notebooks/{id}/messages` → clear chat history
  - POST `/api/notebooks/{id}/ask` → SSE stream (delta + meta + done)
  - POST `/api/notebooks/{id}/reindex` → rebuild embeddings (CLI/Web parity, v0.2.67)
  - GET `/api/health` → LLM status, embedding model (GET only; no POST route is registered)
- SSE Streaming: sends meta event (with citation report skeleton), delta events (tokens), done event (final report + status)
- `_h_ask_sse()`: manages streaming, catches BrokenPipeError/ConnectionResetError, saves partial responses

**`cli.py`** (Command-Line Interface)
- Subcommands: notebook, add, ask, studio, questions, export, serve, reindex, note (add/list/delete), source (delete/rename/refresh) (v0.2.68), health (v0.2.127)
- Maps to the same backends (Store, LLM, Q&A) as the web server
- Internationalization: respects SHOIN_LANG for output

**`export.py`** (Format Export)
- Formats: Markdown (full notebook dump), BibTeX, RIS
- Handles malformed JSON in citation_report gracefully
- Escapes special characters (backslash, newlines) per format spec
- `_status_line()`: renders confirmed/misattributed/uncited/degraded status inline for chat messages and Studio outputs in the Markdown export, so citation verification survives outside the app (v0.2.66)

---

## Version History: v0.1.37 → v0.2.145

### v0.2.145 (2026-08-16)
**Fixed (performance)**: Re-scrutinizing v0.2.144's own diff (the v0.1.4/0.1.5 "re-examine the previous round's deliverable" discipline) found that the two NFKC folds it added to the retrieval hot path both recomputed work that does not vary across the loop they sit in. Neither is a correctness bug — output is byte-identical, pinned by a regression test — but both run on *every* query, so the waste is real.

- **`_apply_neg_filter()`** NFKC-folded each hit's full text and context *inside* the `any(... for n in folded_negs)` generator, so a chunk body was normalized once per negated term instead of once. The fold is the expensive part and does not depend on which needle it is tested against; hoisting it above the `any()` cut a 24-hit / 2-neg call from ~164 µs to ~81 µs (measured), and the NFKC-call count from 98 to 50.
- **`rerank()`** called `lexical_overlap(query, …)` per hit, and `lexical_overlap()` re-ran `query_terms()` + a per-term NFKC fold every time — but the term set is identical across the whole hit list; only the text changes. Extracting `_norm_query_terms()` (fold once) + `_overlap_from_norm()` (score against prepared terms) and hoisting the term prep out of `rerank()`'s loop cut a 24-hit / 3-term rerank from ~496 µs to ~122 µs (measured, 4.1×). `lexical_overlap()` keeps its exact signature and behavior — it now delegates to the two helpers — so its existing callers and tests are untouched.

1 regression test added (`test_rerank_hoisted_terms_match_per_hit_lexical_overlap`) pinning that `rerank()`'s per-hit `lex` equals a direct `lexical_overlap()` call on the same `text\ncontext` string, so the hoist can never silently drift from the public function. `pytest tests/` runs 700 tests. `ruff check shoin/search.py` is clean; `mypy shoin/` unchanged (only the pre-existing `pypdf` stub note).

### v0.2.144 (2026-08-15)
**Fixed**: Width and script spellings of the same Japanese word never retrieved one another. Japanese encodes one word three ways — fullwidth kana (データベース), halfwidth JIS X 0201 kana (ﾃﾞｰﾀﾍﾞｰｽ, what cp932 exports carry, and `ingest._decode()` *prefers* cp932), and fullwidth ASCII (ＧＰＵ, ２０２４, ordinary in JA prose). SQLite's FTS5 trigram tokenizer folds case (verified: `"ｇｐｕ"` MATCHes `ＧＰＵ`) but never width, SQL LIKE folds neither, and the search layer applied no normalization of its own — while `citation.py`'s `_bigrams()` NFKC-folds *both* sides and the frontend's `renderWithSeals()` has since v0.2.109. **Search was the only width-blind layer**, so Shoin would happily mark a citation ✓ against a source its own search could not reach with the same query string. The standard treatment is an NFKC pass before tokenization ([Elastic's kuromoji guidance](https://www.elastic.co/blog/how-to-implement-japanese-full-text-search-in-elasticsearch) recommends `icu_normalizer`; Lucene ships `cjk_width` for exactly this); Shoin cannot normalize at index time because chunk text must stay byte-identical to the source (v0.2.123), so the bridge is built query-side, extending the katakana↔hiragana alternates v0.2.42 already generated.

- **Six failures, all live-reproduced against a real `Store`**: (1) `データベース` missed the ﾃﾞｰﾀﾍﾞｰｽ document and vice versa, on both the FTS and LIKE branches; (2) `GPU` returned 0 hits on a ＧＰＵ document, and the reverse also failed; (3) `query_terms("ﾃﾞｰﾀﾍﾞｰｽ")` → `['ﾃ','ｰﾀﾍ','ｰｽ']` because U+FF9E/FF9F (halfwidth voiced marks) were missing from `_CJK_RANGES`, so every dakuten split the run and exactly **one** trigram survived into the MATCH expression; (4) `neg_terms("Python -ﾃﾞｰﾀ")` → `['ﾃ']` with the residue `ﾞｰﾀ` left in the query as a **positive** term — the v0.2.118 inversion class, for a script the project claims to support; (5) `_kana_alt` (v0.2.42) had exactly one call site, inside `fts_query`, gated to terms ≥ 3 characters, so the 2-character kana query `こー` could not find コード — v0.2.42's own entry closed with *"The LIKE-scan fallback path for short terms is unchanged"*, the same half-covered-branch omission v0.2.142 had to fix for `context`; (6) `lexical_overlap("データベース", "ﾃﾞｰﾀﾍﾞｰｽの設計。")` was `0.0`, so `rerank()` would hand a width-matched hit `lex=0` and push it straight back down — the v0.2.143 failure shape, one spelling dimension over.
- **Fix**: `(0xFF61, 0xFF65)` and `(0xFF66, 0xFF9F)` added to `_CJK_RANGES`, which repairs (3) and — because `_NEG_RE` builds both its character classes from that same tuple — (4) for free. New `term_variants()` composes one-directional conversions off the NFKC form (`_to_hiragana`/`_to_katakana`/`_to_halfwidth`/`_to_fullwidth_ascii`), emitting only spellings that actually differ; the FW→HW kana table is **derived by inverting NFKC's own output** rather than hand-written, the v0.2.118 derive-from-the-shared-source discipline. `fts_query()` and `_fallback_needles()` both expand every term through it, so the two branches gain the bridge together this time. `lexical_overlap()` and `_apply_neg_filter()` NFKC-fold both sides, per the rule v0.2.142/143 established: **every form retrieval can find a chunk by must be visible to every stage that filters or re-scores it.**
- **Directionality is load-bearing**: a naive closure over `_kana_alt`'s *swap* emits mixed-width nonsense (でーた → `でｰた`, verified), because the swap flips fullwidth kana while leaving an already-halfwidth `ｰ` alone. Composing halfwidth conversion only over the katakana-ized form keeps every variant internally consistent in one width. ≤5 variants/term, single pass, no fixed-point iteration.
- **Two traps found while implementing, both silent**: (a) `bm25_search()`'s coverage guard measured the **raw** term length — `ｶﾞｽ` is 3 characters and passes, but normalizes to `ガス` (2), which FTS5 cannot trigram, so `fts_query` produced nothing for it while the guard still read "fully covered" and skipped the LIKE scan; coverage is now measured over every variant. (b) NFKC *creates* a double quote out of `＂` (U+FF02), so escaping once before variant generation would let an unescaped `"` reach the MATCH expression; escaping now runs per variant at emission.
- **A regression I introduced, caught by an existing test**: expanding needles per variant let a single ASCII character slip back in — `is_cjk('Ａ')` is true, so `_fallback_needles("A")` returned `['Ａ']` through the CJK keep-1-char path, reintroducing exactly the flooding needle the ASCII rule exists to drop. Eligibility is now decided on the term before expansion.
- **Not blunted**: `term_variants("研究論文")` is `['研究論文']` — a term whose spellings all coincide yields only itself, so pure-kanji expressions are byte-identical to before (regression-tested), and an absent term still matches nothing.

**Measured with `shoin eval`**: a mixed-width notebook — a cp932-era `システム台帳.md` written in halfwidth kana, a `性能レポート.md` using fullwidth ＧＰＵ, and a plain-kanji `運用メモ.md` — queried in the width a user would naturally type (`データベース`, `サーバー`, `バックアップ`, `GPU`, plus `障害対応` as a same-width control): **recall 0.200 → 1.000, MRR 0.200 → 1.000**. All four cross-width queries retrieved *literally nothing* before; the control passed on both sides and is what the 0.200 floor is made of.

9 regression tests added (`TestWidthVariants`). Fail-then-pass could not be run through the test class itself — it imports `term_variants`, which does not exist pre-fix — so the same nine assertions were re-expressed against the pre-fix public API and run under `git stash` on `shoin/search.py` + `shoin/chunk.py`: **8 of 9 fail pre-fix**, the ninth being the no-over-fire control that correctly passes on both sides. `pytest tests/` now runs 699 tests. `ruff check` is clean on all changed files; `mypy shoin/` is unchanged (only the documented pre-existing `pypdf` stub note). One existing test was **updated, not deleted**: `test_fts_query_quoting` asserted `fts_query('weather "quote')` exactly, and each ASCII term now also contributes its fullwidth spelling. Keeping that direction is deliberate — dropping it would leave the bridge one-way (ＧＰＵ finds a GPU document via NFKC, but GPU could never find a ＧＰＵ one), and an asymmetric bridge is precisely the defect shape v0.2.142 was about. The rejected alternative — gating fullwidth-ASCII to uppercase/digit terms so the test stays green — buys a passing assertion with an unprincipled heuristic about Japanese typography. Drive-by: a comment in `qa.py` described the citation regex as `[SsＳｸ]`; the regex itself was always correct (`[SsＳｓ]`).

**Noted (not actioned)**: the vector path is untouched. `_embed_input()` has a byte-identity contract (context-less chunks must embed identically so pre-migration-5 notebooks need no reindex), and normalizing only the query side would put query and document vectors in different spaces — while normalizing the index side is blocked by the chunk-purity invariant. Embedding tokenizers handle width variance themselves, so this is recorded rather than guessed at. Also unchanged: `mmr()`'s `_sim()` still compares raw text, so a halfwidth and a fullwidth near-duplicate look non-redundant to the diversity term — the same diversity-policy question v0.2.143 declined to answer by guess. And `chunk._SENTENCE_SPLIT_RE` does not split on `｡`, a pre-existing chunking behavior separate from retrieval.

### v0.2.143 (2026-08-09)
**Fixed**: `rerank()` (`search.py`) computed its lexical signal from `h.text` alone, never `h.context` — the immediate sibling of v0.2.142, found by asking which *other* retrieval stages read only one of the two fields FTS5 indexes. The rule the two fixes share: **every field retrieval can find a chunk by must be visible to the stage that re-scores it.** Otherwise v0.2.142's win is handed to the reranker and taken straight back.

- **Concrete failure, live-reproduced** (`retrieve()`, real `Store`, two sources with *identical* evidence — one occurrence of 免疫 each, A's in its breadcrumb `免疫レポート > 概要`, C's in the body of off-topic meeting minutes): `rerank()` gave A `lex=0.0` and C `lex=0.5`, so the 30% lexical term amplified an arbitrary BM25 tie-break into a decisive gap. With C inserted first the ranking was **C 0.85 / A 0.000** — the document actually *about* the topic scored to literally nothing. Inserting A first instead gave A 0.7 / C 0.15, i.e. the outcome was decided by row order plus a signal that was structurally blind to half the index.
- **Fix**: `rerank()` scores `f"{h.text}\n{h.context}"`. Concatenating rather than max-ing keeps the equal 1.0 field weighting `_needle_score()` and SQLite's `bm25(chunks_fts)` already use, and `lexical_overlap()`'s per-term `tf/(tf+1)` saturation bounds what a breadcrumb can contribute. A breadcrumb is identical across a section's chunks, so this lifts a section uniformly and never reorders chunks within it — the same argument v0.2.142 made for scoring. Post-fix both documents score `lex=0.5` and A is no longer zeroed in either insertion order; in a three-source check a genuinely on-topic body still ranks first (0.925), the title-named document rises from 0.344 to 0.544, and the name-drop stays last (0.15).
- **Empty-context chunks are byte-identical to before** (pre-migration-5 backfills, sources with no headings): the ternary passes `h.text` unchanged, and a regression test pins `lex == lexical_overlap(query, text)` for that case.

**Measured with `shoin eval`**: the practical severity is at the top-k boundary, where a zeroed hit drops out of results entirely. On a notebook of 10 meeting-note sources that each name-drop 免疫 once plus one `免疫レポート.md` whose body only paraphrases, querying `免疫`: **MRR 0.125 → 0.167** — the target document moved from rank 8 to rank 6, past two noise documents. Reported honestly: **recall was 1.000 both before and after**, because at `TOP_K=8` the document was still (barely) inside the window on this corpus; the gain here is ranking, not recall, and `shoin eval`'s source-granularity metrics show exactly that much and no more.

4 regression tests added (`TestRerankContext`): breadcrumb visible to `lex`, contextless-hit no-regression control, the equal-evidence/either-insertion-order reproduction, and a three-source ordering assertion with a control query whose term appears in no breadcrumb. Fail-then-pass verified via `git stash` on `shoin/search.py` — **2 of the 4 fail pre-fix**; the other two are controls that correctly pass on both sides. `pytest tests/` now runs 690 tests. `ruff check shoin/search.py` is clean; `mypy shoin/` is unchanged (only the documented pre-existing `pypdf` stub note).

**Noted (not actioned)**: `mmr()`'s redundancy term (`_sim()`) also reads `text` only. Including context there is *not* the same call — it would make any two chunks of one source look more redundant (they share the title prefix), which changes what MMR diversifies over rather than correcting a blind spot. That is a behavioural question about diversity policy, and `shoin eval`'s source-level metrics cannot settle it; it is recorded rather than guessed at, per the v0.2.141 discipline.

### v0.2.142 (2026-08-08)
**Fixed**: The LIKE-scan fallback in `bm25_search()` (`search.py`) never searched `chunks.context`, so v0.2.123's contextual-retrieval recall win was silently absent for **every two-character Japanese compound** — the most common query shape in Japanese. Found by following the contextual-retrieval line of work (Anthropic 2024; and the fielded-BM25 lineage, [Robertson et al.'s BM25F](https://www.researchgate.net/publication/221613382_Simple_BM25_extension_to_multiple_weighted_fields), whose whole premise is that a document's *fields* must all be searchable and weighted) back into Shoin's own two-column FTS5 table.

- **Root cause — a code-path artifact, not a design decision**: a bare-term FTS5 `MATCH` searches every column of `chunks_fts`, so the FTS branch has matched the breadcrumb (source title > heading path) since the column existed. The LIKE branch — which exists precisely because the trigram tokeniser cannot index terms shorter than 3 characters — only ever looked at `c.text`. Which branch handles a term depends solely on its *length*, so the same term retrieved a heading-only match through one branch and nothing through the other.
- **Why this is not a corner case**: `fts_query()` skips every term with `len < 3`, and Japanese content words are overwhelmingly 2-character compounds (熟語: 総説, 経済, 免疫, 概要 …). Reproduced against a real `Store`: a source titled `猫の飼育ガイド` whose body never says 猫 was found by `猫の飼育` (4 chars → FTS5 → 2 hits) and **not** by `猫` (0 hits). `AI 総説` — where *every* term is short, so `fts_query()` returns `""` and the LIKE path is the only path — returned 0 hits for `AI`, for `総説`, and for `AI 総説` itself.
- **Fix**: the fallback scan now matches `c.text OR c.context`, and scoring counts needles in both fields at weight 1.0 via a new shared `_needle_score()` — deliberately the same weighting the FTS branch already has, since SQLite's `bm25(chunks_fts)` defaults every column to 1.0. The goal is to remove the divergence between the branches, not to add a tuning knob to one of them. A breadcrumb is identical across all chunks of a section, so a context match lifts that section uniformly and never reorders chunks within it. `_needle_score()` is also used for the FTS/LIKE merge path's score augmentation, so both places that score needles stay in lock-step by construction.
- **`_apply_neg_filter()` extended to context for the same reason**: a chunk can now be retrieved *because of* its breadcrumb, so `-term` must be able to exclude it on that basis. Otherwise `legacy` surfaces a source whose only mention is in its title while `-legacy` cannot suppress it — the filter would be blind to exactly the signal that produced the hit.
- **Not blunted**: the scan stays selective. A needle absent from both fields still matches nothing (regression-tested), so this widens recall without flooding results.

**Measured with `shoin eval`, not just asserted** — using v0.2.141's own tool the way that entry framed it ("run it before and after a setting change"). On a two-source JA notebook whose documents are *named* by their topic (`免疫レポート.md`, `気象メモ.md`) while their bodies use only descriptive prose, with 2-character queries `免疫` / `気象`: **recall 0.000 → 1.000, MRR 0.000 → 1.000**. Pre-fix both queries retrieved literally nothing.

**Where it makes no difference, stated honestly** (two corpora were built and measured before finding the one above, and both are reported here rather than dropped): when a heading line survives inside some chunk's own `text` — a short document that fits in one chunk, or the *first* chunk of a long section — the source is already retrievable through `c.text`, and since `shoin eval` scores at **source** granularity, both corpora measured 1.000 before and after. The fix changes the outcome exactly when no chunk's text carries the term: a source title (or a heading whose section was split away from it, v0.2.123's original motivating case) is the only place it appears. Chunk-level ranking within an already-found source can still shift; that is below what these metrics measure and is not claimed here.

4 regression tests added (`TestLikeFallbackContext`): short-term breadcrumb matching across all four failing shapes, a no-regression selectivity control, neg-term exclusion via context, and an end-to-end `retrieve()` assertion that the win survives fusion + rerank + MMR. Fail-then-pass verified via `git stash` on `shoin/search.py` (3 of the 4 fail pre-fix; the selectivity control correctly passes on both sides). `pytest tests/` now runs 686 tests. `ruff check shoin/search.py` is clean and `mypy shoin/` is unchanged (only the documented pre-existing `pypdf` stub note).

**Noted (not actioned)**: whether `context` and `text` deserve *different* BM25 weights is a real open question the BM25F literature raises (and `BM25-FIC`, CEUR Vol-2741, proposes estimating field weights analytically) — but changing a default on someone else's benchmark is exactly what v0.2.141 shipped `shoin eval` to stop doing. Equal weighting is the correct default here because it is what the FTS branch already does; a user who wants to know if their corpus prefers otherwise now has a way to measure it.

### v0.2.141 (2026-07-18)
**Added**: `shoin eval <notebook_id> <cases.json>` — retrieval measurement on the user's own corpus (`shoin/evaluate.py`, new module).

**Why**: every retrieval decision in this project — RRF (v0.2.56), contextual chunking (v0.2.123), multi-query RAG-Fusion (v0.2.125), the `CHUNK_OVERLAP=64` default — was justified *from the literature and never measured on Shoin's own data*. The same literature consistently ends with "and measure", because reported effect sizes are corpus- and retriever-specific. Concretely: a [2026 systematic analysis](https://www.digitalapplied.com/blog/rag-chunking-strategies-2026-retrieval-quality-playbook) found chunk overlap gave **no** measurable benefit (SPLADE + Mistral-8B on Natural Questions), the opposite of the usual 10–20% recommendation — and a Shoin-specific measurement showed overlap raises adjacent-chunk similarity from 0.598 to 0.692, i.e. it actively feeds the redundancy term MMR penalizes. Whether that *helps or hurts here* was unanswerable, because Shoin had no way to measure retrieval at all. Rather than change a default on someone else's benchmark, this ships the missing capability.

- `evaluate.py`: `parse_cases()` (strict — a silently-skipped malformed case would inflate the score, so it raises with a concrete per-case message) and `evaluate()` returning recall (share of expected sources in top-k) and MRR (1/rank of the first expected source). No invented aggregate "quality score" — same principle as `citation.py`: report only what is directly measurable.
- Runs through `retrieve_for_question()`, the *same* path `ask()` uses, so toggling `SHOIN_MULTI_QUERY` (or any setting) and re-running compares what the user will really experience. Ranking is by **source**, not chunk: a source found via its 3rd chunk is still found.
- `cli.py`: `_cmd_eval()` + `eval` subparser + `eval.*` i18n keys (ja/en). Malformed/unreadable cases files map to `VALIDATION_FIELD_FORMAT_INVALID` / `SYSTEM_IO_ERROR` with exit 1, never a traceback.
- README documents the command and the cases.json shape, framed as "run it before and after a setting change".

4 regression tests added (`EvalTest`): hit/miss scoring with correct recall+MRR, strict parse rejection of six malformed shapes, valid-parse trimming, and a CLI end-to-end asserting the metrics are printed. Live-verified end-to-end in both locales and on all three error paths. `pytest tests/` now runs 682 tests. `mypy shoin/` and `ruff check shoin/` remain clean.

### v0.2.140 (2026-07-18)
**Fixed**: `verify_grounding()` falsely accused a **correctly cited** source of being the wrong source number when two claims were joined into one sentence — the exact failure mode this module's design forbids ("never accuse a correct answer", v0.1.4). Found by following recent citation-attribution research (sub-sentence granularity: [arXiv:2509.20859](https://arxiv.org/html/2509.20859v1), [NAACL 2025 positional fine-grained citation](https://aclanthology.org/2025.naacl-long.23.pdf)) back into Shoin's own lexical checker.

- **Reproduced**: sources `S1=和紙は楮の繊維から作られる。` / `S2=活版印刷は…重要な発明である。` (long). As two sentences — `和紙は楮から作られる[S1]。活版印刷は…である[S2]。` → `([1,2], [])`, both confirmed, correct. The **same claims joined with ordinary Japanese 連用中止形** — `和紙は楮から作られであり[S1]、活版印刷は…である[S2]。` → `([2], [1])`: **S1 flagged misattributed**. Root cause: the whole sentence's bigrams were compared against *each* cited source, so the long second clause diluted S1's overlap from 0.889 to 0.118 (below `CONFIRM_MIN`) while co-cited S2 cleared it by more than `MISMATCH_GAP` — manufacturing a "wrong source number" verdict out of prose style alone. JA-first users hit this constantly.
- **Fix** — sub-sentence attribution, dependency-free: the citation markers *are* the clause delimiters, so no NLI model or LLM is needed. New `_segment_claims()` maps each citation to the text since the previous marker; `verify_grounding()` uses that clause for both the confirm test and the `MISMATCH_GAP` rival comparison (both sides must be measured on the same unit). Deliberately conservative: it returns `{}` — leaving whole-sentence behavior completely untouched — when the sentence has fewer than two citation positions, and falls back per-citation when a segment is empty (adjacent markers like `[S1][S2]`).
- **Not blunted**: a clause citing a source that plainly belongs to the *other* clause is still flagged (regression test asserts a swapped-citation sentence yields `misattributed == [1, 2]`).

2 regression tests added, fail-then-pass verified via `git stash` on `shoin/citation.py`. `pytest tests/` now runs 678 tests. `mypy shoin/` and `ruff check shoin/` remain clean.

### v0.2.139 (2026-07-18)
**Added**: Cited passages are now marked inside the full-source viewer — the last mile of "verifiable citation". A First-Principles pass asked what a *human* needs to verify a citation: (a) which source (b) which section (c) the supporting text (d) **that the text really sits in the document, in context**. (a)–(c) shipped in v0.2.130–132 + `source_excerpts`; (d) did not: the viewer showed the excerpt and, separately, the whole source as one concatenated blob, leaving the reader to eyeball-scan a long document for the passage. Recent work on visual source attribution (VISA, arXiv:2412.14457) and RAG-trust UI practice both point at highlight-based attribution as the mechanism that closes this.

Solved **exactly**, not heuristically, by carrying the chunk ids that were actually placed in the prompt:
- `qa.py`: `GroundedContext.source_chunk_ids: list[list[int]]` (S1..Sn). `build_context()` collects `h.chunk_id` in lock-step with `texts`, so a chunk dropped by the per-source token budget is never marked — and a chunk *truncated* to fit still is, because its text did reach the prompt.
- `citation.py`: `make_report(..., source_chunk_ids=None)` → `report["source_chunk_ids"] = {"S1": [7, 8]}`; `CitationReport` gained `NotRequired[dict[str, list[int]]]`. Length-validated and empty-omitting like `source_contexts`. All three callers (`qa.ask()` normal+degraded, `server._h_ask_sse()`, `studio.generate()`) wired.
- `store.py`/`server.py`: new `id_seq_text_chunks_for_source()`; `GET /api/sources/{id}/text` now returns `id` alongside `seq`/`text` (additive — the older `text_chunks_for_source()` is untouched since other callers depend on its shape).
- `index.html`: new `renderFullSource()` renders the full text chunk-by-chunk instead of one joined string, adds `.cited-chunk` styling + a `引用箇所`/`cited here` label to the marked chunks, and `scrollIntoView()`s the first one. `citedIds` threads through `renderWithSeals` → `openSeal` → `showSource`, the same path as `source_contexts`. Absent (old persisted reports, Studio outputs) → unmarked plain rendering, no scroll.

4 regression tests added (build_context lists only in-prompt chunks; make_report mapping/omit-empty/length-mismatch; `/text` returns `id`), fail-then-pass verified via `git stash`. **UI live-verified in a real browser** (Playwright, Chromium, real server, 4-chunk document): asking a question and expanding "view full source" rendered 4 chunks with **exactly 1** marked `cited here` and scrolled to it — confirming cited/uncited discrimination, not blanket highlighting. Zero console/page errors.

`pytest tests/` now runs 676 tests. `mypy shoin/` and `ruff check shoin/` remain clean.

### v0.2.138 (2026-07-18)
**Added**: Low-citation-coverage warning on the CLI and in the Markdown export, plus a single shared threshold constant. Found by a First-Principles pass over the product: if the core promise is "answers grounded in *your* sources, verifiably", then "this answer cited only 1 of the 4 sources it was given" is a first-class signal the reader needs — it says the answer may be ignoring retrieved evidence. `coverage` has always been computed in `make_report()` and warned about in the Web UI (`chat.coverage.low` badge), but **both the CLI report and the Markdown export silently dropped it** — the same three-surface parity gap that the section breadcrumb had (v0.2.130/131/132), on a signal that predates all of them.

- `citation.py`: new `COVERAGE_LOW = 0.5` constant with a docstring explaining the semantic. Previously the threshold was a bare `0.5` literal repeated in **three** places in `index.html` and nowhere else — no single source of truth, and nothing stopping the surfaces from drifting apart on what counts as "low".
- `cli.py` `_print_report()`: prints `⚠ 引用被覆 低: {n}/{total} ソースのみ引用…` / `⚠ Low citation coverage: only {n}/{total} sources cited…` (new `cite.coverage_low` key, ja/en) when the answer has citations but coverage is below the threshold.
- `export.py` `_status_line()`: appends `⚠引用被覆 低 (1/4)` / `⚠ low citation coverage (1/4)` to the existing status line (new `status_coverage_low` key, ja/en).
- `index.html`: the three hardcoded `0.5` literals now reference one `COVERAGE_LOW` JS constant documented as mirroring the Python one.
- Guard semantics on all surfaces: only warn when the answer actually **has** citations (`cited` non-empty) — a zero-citation answer's story is told by `uncited`/`invalid`, not by coverage.

2 regression tests added (export status line: low warns with `(1/4)` / high doesn't / no-citations doesn't; CLI report: same low-vs-high pair), fail-then-pass verified via `git stash` on `shoin/export.py`+`shoin/cli.py`. Live-verified end-to-end through `shoin ask` (4 sources, 1 cited → warning printed) and `export_markdown()`. `pytest tests/` now runs 673 tests. `mypy shoin/` and `ruff check shoin/` remain clean.

### v0.2.137 (2026-07-18)
**Added**: RIS reference type now reflects the source kind. `export_ris()` hardcoded `TY  - GEN` (generic) for every entry regardless of what the source actually is — but Shoin is primarily a URL-ingesting tool, and RIS defines `ELEC` (electronic/web resource) exactly for that case. Reference managers use `TY` to categorize entries and choose how to render them (a web article vs. an unspecified generic item), so every exported web source was mis-typed. New `_RIS_TYPE` map: `url`/`html` → `ELEC`, everything else → `GEN` (file-based kinds have no better standard RIS type, so the previous behavior is preserved for them). 1 regression test added (`test_ris_type_reflects_source_kind`, asserting the exact TY sequence for url/pdf/html sources), fail-then-pass verified via `git stash` on `shoin/export.py`. **Not changed**: BibTeX's `@misc` — `@online` is BibLaTeX-only and would break plain-BibTeX consumers, so `@misc` remains correct there. `pytest tests/` now runs 671 tests. `mypy shoin/` and `ruff check shoin/export.py` remain clean.

### v0.2.136 (2026-07-18)
**Added**: Structured `PY` (publication year) field in the RIS export — the RIS parallel of the BibTeX `year` field (v0.2.133), closing an asymmetry that v0.2.133 itself introduced (BibTeX gained a structured year, RIS did not). Reference managers (Zotero, Mendeley, EndNote) use RIS `PY` as the canonical year field for author-year citation and sorting; the generic `DA` (date) field is not reliably parsed into a year by all of them, so a RIS export could not be cited/sorted author-year despite carrying the date. `export_ris()` now emits `PY  - YYYY` (before the existing `DA` line) using the same guarded 4-digit leading-year extraction as BibTeX (`year.isdigit() and len(year)==4`), so a malformed/empty `added_at` produces no stray `PY` line. 1 regression test added (`test_ris_emits_structured_py_year_field`; the existing empty-`added_at` test extended to assert no `PY  -` line appears), fail-then-pass verified via `git stash` on `shoin/export.py`. `pytest tests/` now runs 670 tests. `mypy shoin/` and `ruff check shoin/export.py` remain clean.

### v0.2.135 (2026-07-18)
**Added (docs)**: Second-pass deepening of the v0.2.134 agent instruction set + product review — following the project's own "re-scrutinize the previous round's deliverable" discipline (v0.1.4/0.1.5 lineage).

1. **`docs/agents/sonnet.md` にタスクレシピ集(§4)とエントリテンプレート(§5)を追補** — 初版は原則の列挙に留まり、手を動かすための具体的手順が無かった。頻出3タスク（環境変数追加 / バグ修正の11段手順 / i18n）を実コマンド・参照実装付きで段階列挙し、Version History エントリの骨組みテンプレートを追加。Sonnet 級には抽象原則より手順書が効くという判断。
2. **`docs/agents/opus.md` に監査ワークフロー雛形(§5)と危険地図(§6)を追補** — find→敵対的検証→修正の回し方と発見の採用基準（「具体的入力→具体的誤出力」のみ）、および store/server/chunk/search/citation のファイル別「触る前に読むべき History エントリ」一覧（migration 並行冪等・`_retry_on_lock` の substring 限界・SSE 切断ガードの層構造・トークン計数3関数の同期義務・`_NEG_RE` lookbehind・沈黙原則）。
3. **`docs/product-review.md` の追補** — 長所に XSS 安全 UI(`textContent`のみ, `innerHTML` 不使用を確認)+ aria-label i18n(`data-i18n-aria`)を、短所にブラウザ自動化テストの恒久スイート不在(`grep -ri playwright tests/` が空を確認)を、バックログに Playwright スモークの CI 追加(#11)を追加。

コード変更なし。`pytest tests/` は 669 件のまま全緑、`mypy shoin/`・`ruff check` 影響なし。全追補主張はコード実挙動と突合済み。

### v0.2.134 (2026-07-18)
**Added (docs)**: Model-tier agent instruction documents + full refresh of the product review.

1. **`docs/product-review.md` を v0.1.0 時点(2026-06-13)の内容から v0.2.133 時点へ全面更新** — 旧版は 11 バージョン帯 ×130 リリース分陳腐化していた（初版の指摘→修正の往復記録は CLAUDE.md Version History / CHANGELOG.md にバグ単位で残っているため要約参照に置換）。新版は現在の長所8点（節文脈の3面表示・研究裏付き検索改善・診断可能性・fail-then-pass 文化まで含む）、現存する弱点7点（CI 未稼働、`ruff format` 不整合、CHANGELOG 停止、PyPI 未発行、改名後の埋め込み陳腐化、トークン予算、デフォルトブランチ）、優先度・難易度・担当推奨・エントリポイント付きの改善案バックログ10項目で構成。全主張はコード実挙動と突合済み（テスト669件・14モジュール・CHANGELOG の v0.1.55 停止点・Studio エクスポートに凡例が無いこと等を個別確認 — v0.2.75/112/129 の「文書だけの空約束」教訓に従う）。
2. **`docs/agents/opus.md` / `docs/agents/sonnet.md` 新設** — モデル能力帯別のエージェント作業指示書。共通の儀式（バージョンbump三点セット+Version History 追記、`git stash` による fail-then-pass、UI 変更の実ブラウザ検証、`ruff format` 禁止、全スイート緑、main 追従 push）を両書に独立記載した上で、Opus 版は高リスク領域（migration/検索スコアリング/引用検証セマンティクス/プロンプト/並行性設計/マルチエージェント監査）を開放、Sonnet 版は加算的タスクに限定し同領域を明示的に禁止、確信の持てない発見は修正せず `**Noted (not actioned)**:` 書式（v0.2.72/133 前例）で記録するエスカレーション手順を規定。
3. CLAUDE.md「Contributing & Philosophy」冒頭に `docs/agents/` への導線を追加。

コード変更なし。`pytest tests/` は 669 件のまま全緑、`mypy shoin/`・`ruff check` 影響なし。

### v0.2.133 (2026-07-18)
**Added**: Structured `year` field in the BibTeX export. `export_bibtex()`'s `@misc` entries previously carried the date only inside the free-text `note` field (`note = {Shoin source, added 2026-07-14}`), which reference managers (Zotero, Mendeley, BibLaTeX) do not parse — so an exported source could not be cited author-year or sorted by year. Now emits `year = {YYYY}` (extracted from `added_at`'s 4-digit leading year) before the note, only when a genuine 4-digit year is present (`year.isdigit() and len(year)==4`), so a malformed/empty `added_at` produces no stray `year = {}` field. 1 regression test added (`test_bibtex_emits_structured_year_field`; the existing empty-`added_at` test extended to assert no `year =` line appears), fail-then-pass verified via `git stash` on `shoin/export.py`. `pytest tests/` now runs 669 tests. `mypy shoin/` and `ruff check shoin/export.py` remain clean.

**Noted (not actioned)**: `ruff format --check .` (a step in the parked `ci/ci.yml`) would fail on 16 of 18 files — the project maintains its formatting by hand (it passes `ruff check`, the linter, but was never run through `ruff format`, the autoformatter, whose Black-style output conflicts with the codebase's deliberate aligned-comment style). This is a pre-existing mismatch in the *parked* CI (`.github/workflows/` still doesn't exist, so nothing runs it), not a regression from this session; resolving it — either adopting `ruff format` wholesale or dropping that CI step — is a maintainer style decision, so it's recorded here rather than changed unilaterally.

### v0.2.132 (2026-07-14)
**Added**: Section breadcrumb in the CLI's citation report — the third and final surface for v0.2.130's contextual-retrieval provenance (after the in-app seal viewer, v0.2.130, and the Markdown export legend, v0.2.131). `cli._print_report()`'s per-cited-source line now appends `(§ …)` when the report carries `source_contexts`: `[S1] 生物ノート (§ 光合成のしくみ > 明反応) ✓根拠確認済み`, so a headless `shoin ask` user sees WHICH section each citation is grounded in (REQ-103 CLI/Web parity — the same principle that drove `shoin health` in v0.2.127). Reads `report.get("source_contexts")` with an `isinstance(..., dict)` guard; absent (old reports, no-heading sources) → plain line, no stray `§`. 1 regression test added (`test_print_report_shows_section_breadcrumb`: section shown for S1, and S2 without a section gets no `§`), fail-then-pass verified via `git stash` on `shoin/cli.py`, plus live-verified end-to-end through `shoin ask`. `pytest tests/` now runs 668 tests. `mypy shoin/` and `ruff check shoin/cli.py` remain clean.

### v0.2.131 (2026-07-14)
**Added**: Section breadcrumb in the Markdown export's chat legend — the export-surface completion of v0.2.130. `export_markdown()`'s per-assistant-message source legend (`S1=論文A, S2=論文B`) now appends the section when the persisted `citation_report` carries `source_contexts`: `S1=生物ノート (§ 光合成のしくみ > 明反応)`. So an answer archived or shared outside the app keeps the same "which section is this grounded in" provenance the seal viewer surfaces in-app — mirroring how v0.2.66 brought the citation-verification *status* into the export. Reads `report.get("source_contexts")` with the same `isinstance(..., dict)` guard the existing `source_map` read uses; absent (old messages, no-heading sources) → plain legend, no stray `§`. 2 regression tests added (section present → `(§ …)` in legend; absent → no `§`), fail-then-pass verified via `git stash` on `shoin/export.py`. `pytest tests/` now runs 667 tests. `mypy shoin/` and `ruff check shoin/export.py` remain clean.

### v0.2.130 (2026-07-14)
**Added**: Section breadcrumb surfaced in the citation UI — the natural completion of v0.2.123's contextual chunking, which until now used the `chunks.context` breadcrumb ("title > heading > …") for *retrieval only*, invisible to the user. Clicking a `[S1]` seal now shows **which section** of the source the cited passage came from, so the reader can see a citation is grounded in "光合成のしくみ > 明反応", not just somewhere in the document. Purely additive: the LLM prompt and the four-stage citation verifier are untouched (`source_bodies` stays pure text), and old persisted reports without the field degrade silently via the existing `.get()` guard convention.

- `search.py`: `Hit` gained a `context: str = ""` field (default keeps every existing positional `Hit(...)` in tests valid); `bm25_search()` (both FTS and LIKE paths), `vector_search()`, and `studio.overview_hits()` now `SELECT c.context` and load it onto each Hit.
- `qa.py`: `GroundedContext` gained `source_contexts: list[str]` (S1..Sn order). `build_context()` takes each source's TOP hit's context, strips the title prefix via the new `_section_from_context(context, title)` helper (`context == title` → `""`, `"title > rest"` → `"rest"`, no-match → `""`), and stores the heading path.
- `citation.py`: `make_report(..., source_contexts=None)` maps non-empty sections to `report["source_contexts"]` (`{"S1": "…"}`); `CitationReport` gained `source_contexts: NotRequired[dict[str, str]]`. Length-validated like `source_ids`/`source_bodies`. All three production callers (`qa.ask()` normal + degraded, `server._h_ask_sse()`, `studio.generate()`) pass `context.source_contexts` through.
- `index.html`: `renderWithSeals()`/`openSeal()`/`showSource()` thread `source_contexts` from the report; the seal's hover tooltip appends `› section`, and the source viewer shows a `節: …` / `Section: …` label above the excerpt (new `viewer.section` i18n key ja/en, new `.section-label` style). Absent-section case guarded by `if(section)`.

7 backend regression tests added (Hit.context loaded by bm25/vector search; build_context title-prefix stripping + empty-for-contextless; make_report mapping/omit-empty/length-mismatch; ask() end-to-end report carries source_contexts), fail-then-pass verified via `git stash`. **UI live-verified in a real browser** (Playwright, Chromium, real server + contextual content): asked a question, clicked the seal → viewer showed `Section: 生物ノート` and the tooltip read `grounding confirmed — bio.md › 生物ノート`, zero console/page errors.

`pytest tests/` now runs 665 tests. `mypy shoin/` and `ruff check shoin/` remain clean.

### v0.2.129 (2026-07-14)
**Fixed (docs + implementation)**: A fresh review of the "Known Weaknesses & Tech Debt" and "Testing & Debugging" sections — following the same "check a doc claim against the actual code" method that found stale claims in v0.2.75/112/113 — found two more, both in this exact vein:

1. **The "Debugging Aid" (`DEBUG=1` retrieval stats) was pure fiction.** `grep`-confirmed: no code anywhere read `os.environ.get("DEBUG"...)` or any variant, despite this line existing in "Testing & Debugging" (and referenced again in the "No Distributed Tracing" section's own "Workaround") since long before this session. A user following the guide to debug a relevance problem would set `DEBUG=1`, see nothing, and have no idea the feature never existed. Implemented for real this time: `search.py`'s `retrieve()`/`retrieve_multi()` now print the query, extracted `-word` negations, BM25/vector hit counts, and per-final-hit chunk/source id + fused score + raw BM25/cosine scores + the detail dict (RRF ranks, lexical-rerank weight) to stderr — but under `SHOIN_DEBUG`, not the bare `DEBUG` the doc originally promised: `DEBUG` collides with the generic name many unrelated tools/CI systems already use for their own purposes, unlike every other Shoin setting, which is namespaced under `SHOIN_`. The doc lines were corrected to match (both the name and — since RRF replaced alpha-based fusion in v0.2.56 — dropping the now-nonexistent "fusion alpha" from the described output), including `docs/spec.md`'s matching claim.
2. **"Embedding Batch Size Is a Fixed Constant" hadn't been updated since v0.2.124 closed the gap it describes.** The section still said `EMBED_BATCH` "not configurable via an environment variable... has no way to tune this without editing source" — but `SHOIN_EMBED_BATCH` (added three versions ago, this session) already does exactly that. Retitled to "(Closed, v0.2.124)" and rewritten to describe the actual current state instead of the pre-fix gap.

4 regression tests added (`TestDebugAid`): disabled-by-default silence, `SHOIN_DEBUG=1` output content (including a "no 'alpha' in output" assertion pinning the RRF-era description), the old bare `DEBUG` name correctly NOT triggering, and `retrieve_multi()` coverage. Verified fail-then-pass via `git stash` on `shoin/search.py`.

`pytest tests/` now runs 658 tests. `mypy shoin/` and `ruff check shoin/search.py` remain clean.

### v0.2.128 (2026-07-14)
**Fixed**: An 8-agent parallel Workflow audit (find → 3-vote adversarial verify per finding) targeting this session's own new code (v0.2.123–127: contextual chunking, multi-query RAG-Fusion, CLI/Web health) plus a general frontend sweep found 8 confirmed defects, 1 of them HIGH severity. All 8 fixed with regression tests, fail-then-pass verified via `git stash`.

1. **[HIGH] Migration 5's `ALTER TABLE ADD COLUMN` was not idempotent, breaking concurrent server startup.** SQLite has no `ADD COLUMN IF NOT EXISTS`; every other migration-5 statement is `IF NOT EXISTS`/`IF EXISTS`, but the ALTER TABLE is not. `server.py` opens a fresh `Store()` (which runs `migrate()`) on every HTTP request under `ThreadingHTTPServer`, so two concurrent requests against a shared file still on schema v4 can both read `current=4` and both attempt migration 5; SQLite allows only one writer, so the loser's own `BEGIN` blocks until the winner commits, then the loser's `ALTER TABLE` immediately fails with `duplicate column name: context` — a genuine constraint violation, not lock contention, so it doesn't contain "locked" and `_retry_on_lock()`'s blanket substring check never retries it. **Empirically confirmed via the project's own pre-existing `test_migrate_concurrent_shared_db_file_no_crash` test**: ~50% failure rate across repeated runs (this exact test exists specifically because of a near-identical bug fixed in v0.2.40/v0.2.71 for a different migration). Fix: `_migrate_once()` now catches `OperationalError` containing "duplicate column name", rolls back the dangling transaction the failed `ALTER TABLE` left open, and re-checks `schema_migrations` — if a concurrent winner already fully committed this version (guaranteed visible by SQLite's write-serialization once the loser's blocked `BEGIN` unblocks), skip forward instead of raising; otherwise re-raise (a genuine, unexpected schema conflict must not be silently swallowed). Stress-tested 80/80 consecutive runs green post-fix (0 failures, matching the v0.2.71 precedent's own verification bar). 2 deterministic regression tests added (mocking/engineering the exact two-read race point directly, not relying on thread-timing luck) alongside the existing probabilistic threaded test.

2. **[MEDIUM] `refresh_source()` baked a stale pre-fetch title into new chunks' context.** `refresh_source()` read `src.title` at the very top of the function, before `extract_url()`'s network round-trip (up to `URL_TIMEOUT_SEC=15s`). A `PATCH /api/sources/{id}` rename committing during that fetch window was silently and *permanently* lost from `chunks.context` — `sources.title` updated correctly, but the new chunks' context breadcrumb kept the OLD title forever, since no later rename ever revisits chunks whose context never had the old title as a matching prefix (`_rewrite_chunk_context_titles()`, v0.2.124, only rewrites rows that DO match). Fix: re-read the current title immediately before building contexts (right after `extract_url()` returns), shrinking the race window from ~15s to a single DB round-trip — the same "narrow, don't fully eliminate" tradeoff this codebase already accepts elsewhere for similarly narrow single-user-app races.

3. **[MEDIUM] `_NEG_RE`'s lookbehind excluded only ASCII word characters, not CJK — misparsing an ordinary in-sentence hyphen as `-word` negation syntax.** v0.2.118 extended the *positive* match side (what CAN be negated) to cover every `chunk._CJK_RANGES` script, but never extended the *lookbehind* (what disqualifies a hyphen from being negation, e.g. `state-of-the-art`) to match — so `アルゴリズムの-最適化について` (hiragena の directly before the hyphen, no space) was misparsed as `-最適化` negation, silently discarding real query content. Exercised more severely via v0.2.125's `retrieve_multi()`: negs are derived from the primary query alone and applied to every fused list including LLM rewrites, so this false positive could zero out results a rewrite would otherwise have surfaced. Fix: added a second lookbehind built from `_CJK_RANGES` minus the CJK-punctuation block (punctuation is a word *boundary*, not a word character, so a hyphen after one — e.g. `書院。-legacy` — must still correctly introduce negation).

4. **[MEDIUM] A single `/ask` could acquire `generation_lock` twice, doubling the worst-case block time for other concurrent requests.** `retrieve_for_question()` (v0.2.125) held `generation_lock` for the rewrite LLM call, then `_h_ask_sse()` acquired the *same* lock again later for the actual answer generation — up to `2 × CHAT_TIMEOUT_SEC=360s` cumulative hold for one request, worse because the rewrite-call acquisition happened *before* SSE headers were sent (no confirmation the client was even still connected). Fix: stopped serializing the rewrite call under `generation_lock` at all, matching how `_query_vector()`'s embedding calls (already unlocked, both before and after this session) are treated — a short 2-phrasing rewrite is comparable LLM-endpoint load to an embedding call, not the long, fully-streamed answer generation the lock exists to protect. `chat_stream`-to-`chat_stream` serialization (the actual DoS control) is untouched and re-verified.

5. **[MEDIUM] `shoin health` crashed with a raw, unhandled `ValueError` for a malformed IPv6-bracket `SHOIN_LLM_URL`** (e.g. `http://[::1:11434/v1`, a plausible unclosed-bracket typo) — defeating the new command's whole "works even when things are broken" purpose (v0.2.127). Root cause: `LLMClient.available()` built `urllib.request.Request(...)` *before* its own `try:` block, so the `ValueError` `urllib.parse.urlsplit()` raises for malformed IPv6 syntax escaped uncaught — even though the `except` clause already listed "ValueError: unknown URL scheme" as a case it exists to catch; every *other* malformed-URL shape (bad port, embedded space, missing scheme) was already correctly caught. Fix: moved `Request()` construction inside the `try:`. Also added a small defense-in-depth `try/except Exception` around `_cmd_health()`'s call in `main()` (mirroring `serve`'s own pattern) so a diagnostic command can never crash with a raw traceback regardless of future changes, without disturbing its deliberate placement above the `Store()` construction.

6. **[LOW] `shoin health`'s "Data directory" line ignored the top-level `--db` override.** Every other subcommand honors `--db` via `main()`'s `Store(str(args.db) if args.db else db_path())`, but `_cmd_health()` took no `args` parameter at all and unconditionally printed the config-derived default — misleading a user diagnosing exactly the scenario `--db` exists for. Fix: `_cmd_health()` now takes the resolved `db` value and prints it (relabeled "Database file" — now shows the exact resolved file path in both the default and override case, more precise than the previous bare directory).

7. **[LOW] Heading titles ending in `#` (e.g. "C#", "F#") lost that character in the contextual-chunking breadcrumb.** `_context_blocks()` used `.rstrip("#")` to strip a markdown ATX closing sequence (`## Heading ##` → `Heading`), but `rstrip` deletes *any* trailing `#` unconditionally — CommonMark requires a genuine closing sequence to be preceded by whitespace, a distinction `rstrip` cannot make. Fix: replaced with a regex (`\s+#+\s*$`) that only strips a `#`-run genuinely preceded by whitespace, preserving titles that legitimately end in `#` with no space before it while still correctly stripping well-formed closing sequences.

8. **[LOW, frontend] Clicking a URL source's ↻ refresh button while its title was mid-rename silently discarded the typed text.** Clicking the refresh button moves DOM focus onto it, firing `blur` on the rename `<input>`; the existing `relatedTarget`-based skip (added so the delete/refresh button's own click isn't swallowed) correctly avoids auto-committing, but also means `renderNotebook()`'s `document.activeElement`-based "preserve an in-progress edit across an unrelated rebuild" logic (v0.2.88) never sees this case — by the time the refresh's `openNotebook()` rebuild runs, focus is on the button, not the input, so the typed text is wiped with zero commit, zero restore, zero toast. Unlike the delete button (where discarding is *correct*, since the source is about to disappear), the source survives a refresh, so there's no justification for losing the edit. Fix: the refresh handler now stashes the uncommitted value into a small module-level variable before proceeding, and `renderNotebook()` falls back to it when `document.activeElement` doesn't show an in-progress rename — reusing the exact same restoration path v0.2.88 already built for other unrelated rebuilds. **Live-verified in a real browser (Playwright, Chromium) against a real running server** with a mocked-successful refresh (so the full rebuild path actually runs, not just the failure short-circuit): pre-fix, the rename input and typed text were confirmed gone after refresh completed; post-fix, the input is restored with the typed value and focus. The sibling delete-button case was also verified unaffected (source correctly removed, no restore attempted). No pytest regression test — this project's suite has no browser-automation coverage; frontend-only changes are verified live per CLAUDE.md's own UI-testing rule.

`pytest tests/` now runs 654 tests (up from 644). `mypy shoin/` and `ruff check` remain clean on every changed source file (pre-existing, unrelated test-file style findings and the `pypdf` stub note are untouched).

### v0.2.127 (2026-07-13)
**Added**: `shoin health` CLI subcommand — the headless equivalent of `GET /api/health` (v0.2.126), closing a genuine REQ-103 CLI/Web parity gap this session's audit found immediately after adding the Web endpoint: a user running only the CLI (SSH-only server, no browser) had no way to check LLM reachability or confirm `SHOIN_MULTI_QUERY`/`SHOIN_EMBED_BATCH` actually took effect without starting the Web server or reading source/env directly.

- `cli.py`: `_cmd_health()` prints version, LLM endpoint URL, reachability (`llm.available()`, matching `server._h_health()`'s `getattr(..., "available", lambda: False)` pattern for test-double compatibility), chat model, embed model (or a "disabled — BM25 only" note when empty), the `SHOIN_MULTI_QUERY` state, the effective `SHOIN_EMBED_BATCH` (or `"{default} (default)"` when unset), and the resolved data directory. Respects `SHOIN_LANG` via the existing `_t()` i18n mechanism (new `health.*` string keys, ja/en).
- Special-cased in `main()` above the `Store()` construction — same treatment as `serve` — so `shoin health` still works and is useful even when the data directory itself is the problem (permission error, bad `SHOIN_DATA_DIR`), rather than failing before it can report anything.
- 2 regression tests added (`TestCLI`): default output contains version + reachability text; env-driven fields (`SHOIN_MULTI_QUERY=1`, `SHOIN_EMBED_BATCH=32`, `SHOIN_LANG=en`) are correctly reflected. Verified live in both locales and with `available()` true/false. Verified fail-then-pass via `git stash` on `shoin/cli.py`.

`pytest tests/` now runs 644 tests. `mypy shoin/` and `ruff check shoin/cli.py` remain clean.

### v0.2.126 (2026-07-13)
**Added**: `GET /api/health` now reports `multi_query: bool` (the current `SHOIN_MULTI_QUERY` state, v0.2.126) — a small diagnostic addition in the same spirit as CLAUDE.md's own "Debugging Aid" (`DEBUG=1` retrieval-stats) guidance: a user wondering why retrieval feels slow, or why enabling multi-query doesn't seem to change results, previously had no way to confirm the flag actually took effect without reading source or env directly. 2 regression tests added (`test_workflow`'s existing health assertion extended with the default-off case; a new `test_health_reflects_multi_query_env_toggle` covering the on case), fail-then-pass verified via `git stash` on `shoin/server.py`. `pytest tests/` now runs 642 tests. `mypy shoin/` and `ruff check shoin/server.py` remain clean.

### v0.2.125 (2026-07-13)
**Added**: Multi-query RAG-Fusion retrieval (`SHOIN_MULTI_QUERY=1`, opt-in) — the second research-backed retrieval upgrade from this session's literature survey (after v0.2.123's contextual chunking): DMQR-RAG (arXiv 2411.13154) and the RAG-Fusion lineage report ~10% P@5 improvement from fusing ranked lists across several LLM-generated query rewrites, and Shoin's existing RRF infrastructure (v0.2.56) makes the fusion step nearly free.

- `qa.py`: `rewrite_queries(llm, question, n=MULTI_QUERY_REWRITES=2)` asks the LLM for alternate phrasings (new `rewrite_prompt` ja/en strings; `temperature=0.7` for diversity — noise rewrites only add RRF-tolerated extra lists, the original query is always list 1). Parsing reuses `_LIST_PREFIX_RE`, **moved here from `studio.py`** (which now imports it) because `rewrite_queries()` and `suggest_questions()` parse the same LLM list-output convention — two independently-maintained copies of one parsing rule is exactly the drift failure v0.2.80 consolidated elsewhere. NFKC-normalized casefold dedupe drops rewrites duplicating the original/each other; each rewrite capped to `MAX_QUESTION_LEN` (pathological-FTS5-query guard). Any `LLMError` → `[]`, same silent-degradation contract as `_query_vector()`.
- `qa.py`: `retrieve_for_question(store, llm, nb_id, retrieval_q, qvec, k, lock)` — the single retrieval entry point `ask()` and `server._h_ask_sse()` now share. With the flag off (default) it is exactly `retrieve()` — byte-identical behavior, zero extra LLM traffic (regression-tested: `ask()` issues exactly one chat call). With the flag on, rewrites are fetched (under `server.py`'s `generation_lock` when supplied, honoring the v0.2.70 single-generation DoS control), each rewrite embedded only if the original query itself embedded, and all lists fused.
- `search.py`: `rrf_fuse_lists(lists, k, detail_names)` generalizes RRF to N ranked lists; `rrf_fuse()` now delegates to it with its legacy two-list detail names (`rrf_bm25_rank`/`rrf_vec_rank`), scores and detail keys unchanged (equivalence regression test). `retrieve_multi(store, nb_id, queries, query_vecs, k)`: `queries[0]` is the user's ORIGINAL query and alone defines the `-word` negative filter and the lexical-rerank reference — LLM rewrites can never resurrect an explicitly excluded chunk (rewrites are neg-stripped, and their BM25/vector lists filtered by the original negs BEFORE fusion, the v0.2.73 pre-fusion placement) nor hijack the rerank signal.
- `config.py`: `multi_query_enabled()` — **default OFF**: one extra LLM call per ask is multi-second latency on the 4B-class local models this project targets ("Lightweight First"); users trade latency for recall explicitly. README config table documents it (the v0.2.66 discoverability lesson).

8 regression tests added (`TestMultiQuery`, `tests/test_qa.py`): rewrite parsing/dedupe/cap, LLMError→[], two-list `rrf_fuse` equivalence through the new generalized path, the headline recall win (a chunk sharing no trigram with the original query, found only via a rewrite), neg-term preservation across rewrites, default-off single-chat-call guarantee, enabled-path end-to-end (rewrite call then answer, correct citation report), and rewrite-failure fallback to single-query. `pytest tests/` now runs 641 tests. `mypy shoin/` and `ruff check shoin/` clean (the pre-existing `pypdf` stub note untouched).

### v0.2.124 (2026-07-13)
**Fixed**: Two correctness gaps v0.2.123 (contextual chunk metadata) left open, found by reviewing that feature's own integration surface immediately after shipping it, plus the one remaining documented config gap:

1. **Source rename left chunk contexts stale** — `_chunk_context()` folds the source title into every chunk's indexed `context`, but `update_source_title()` (the shared Web `PATCH /api/sources/{id}` / CLI `source rename` path) never touched `chunks.context`. A renamed source kept matching FTS queries for its OLD title — and never matched its new one — indefinitely, silently inverting the v0.2.123 title-in-context recall win for exactly the sources users curate most. Fix: `update_source_title()` now rewrites each chunk context's title prefix (exact-match or `old_title + " > "` prefix) in the same transaction as the title UPDATE, re-reading the current title inside the transaction (the v0.2.98 stale-snapshot lesson) and re-capping to `_MAX_CONTEXT_CHARS`. Rows with no matching prefix (pre-migration-5 `context=''` backfills, cap-truncated titles) are left untouched — no match means no safe rewrite. Embedding vectors keep the old title inside their input until reindex (acceptable degradation: FTS is the primary context signal, and `reindex_notebook` reads the now-fresh `context` column).
2. **No FTS `AFTER UPDATE` trigger** — migrations 1/5 mirrored only INSERT/DELETE into the external-content `chunks_fts` because nothing ever UPDATEd an indexed column; fix 1 introduces exactly such an UPDATE. Migration 6 adds `chunks_au` (`AFTER UPDATE OF text, context`) issuing the FTS delete+insert pair, scoped so embedding-BLOB updates don't pay double FTS writes. Without it, fix 1 would have traded silently-stale contexts for a silently-desynchronized FTS index — the same failure class, one layer down.
3. **`SHOIN_EMBED_BATCH`** — CLAUDE.md's own "Embedding Batch Size Is a Fixed Constant" known-gap note, now closed: `config.embed_batch()` parses the env var (invalid/`<1`/unset → `None`), and `_embed_chunks()` uses it when set, falling back to the `EMBED_BATCH=16` module constant (kept so existing tests that patch `shoin.pipeline.EMBED_BATCH` are unaffected when the env is unset).

4 regression tests added (rename-refreshes-context with breadcrumb-tail preservation, contextless-rows-untouched, UPDATE-trigger FTS consistency with trigram-disjoint vocabulary, env-override batching with invalid-value fallbacks); fixes 1+2 verified fail-then-pass via `git stash` on `shoin/store.py`. `pytest tests/` now runs 633 tests. `mypy shoin/` and `ruff check` remain clean on changed files.

### v0.2.123 (2026-07-11)
**Added**: Contextual chunk metadata — a deterministic, zero-dependency, LLM-free variant of Anthropic's *Contextual Retrieval* (2024). Each chunk is now indexed alongside a **context breadcrumb**: the source title plus the markdown heading path of the section the chunk lives in (e.g. `生物ノート > 光合成のしくみ > 明反応`). Motivation surfaced from a survey of recent RAG-retrieval literature: the dominant, reproducible recall win for local/lightweight setups is *contextual chunking* — attaching each chunk's document/section context so a query term appearing only in a heading (or only in the document title) still retrieves chunks that the fixed-size splitter carried away from that heading. Shoin's own `split_text()` is heading-aware only at block boundaries; once a long section is hard-split or blocks are merged for the overlap window, every chunk after the first loses the heading line from its body, so a heading term matched exactly one chunk. This closes that gap without an LLM call, extra latency, or a new dependency — staying inside Shoin's zero-dependency / offline / ≤8B-LLM design envelope.

Implementation:
- `chunk.py`: new `split_text_with_context()` returns `(breadcrumb, chunk_text)` pairs, tracking a heading stack across `_blocks()` (a same-or-shallower heading pops deeper sections). Breadcrumb capped at `_MAX_CONTEXT_CHARS = 200`. `split_text()` now delegates to it and drops the context, so its output is **byte-for-byte unchanged** (proven by `test_split_text_output_unchanged_by_context` across three chunk/overlap settings). The chunk *text* itself is never modified — display, the LLM prompt (`build_context`), and the four-stage citation verifier all keep seeing pure source text.
- `store.py`: migration 5 adds `chunks.context` and rebuilds `chunks_fts` as a two-column (`context`, `text`) external-content FTS5 table with mirroring insert/delete triggers; existing chunks backfill with `context=''` and degrade to the previous text-only behaviour until re-indexed (no forced reindex). `add_chunks()`/`replace_chunks_for_source()` gain an optional `contexts` list (length-validated); a new `id_context_text_chunks_for_notebook()` feeds reindex.
- `pipeline.py`: `_chunk_context(title, breadcrumb)` folds the source title into every chunk's context; `_embed_input(context, text)` prepends the breadcrumb to the embedding input so **vector** search benefits too. `index_source`, `refresh_source`, and `reindex_notebook` all route through `_embed_input` so a notebook never mixes context-aware and text-only vectors. `search.py` needed **no change** — bare-term FTS5 MATCH already searches all columns, so a heading-term match boosts the chunk's BM25 score automatically.

`pytest tests/` now runs **629** tests (8 new in `TestChunkContext`, covering breadcrumb hierarchy/pop, the split-off-heading recall win, title-in-context cross-source match, context-length validation, backward-compatible context-less `add_chunks`, and the v4→v5 upgrade backfill + post-rebuild delete-trigger consistency). `mypy shoin/` and `ruff check shoin/` are clean on all changed source files (the pre-existing `pypdf` stub note and unrelated test-file style warnings are untouched).

### v0.2.122 (2026-07-10)
**Fixed**: A fiftieth background audit round found `startSourceRename()`'s (`index.html`) Escape-to-cancel handler didn't actually cancel — it silently re-opened the same unsaved edit with the discarded text still in it, and a follow-up Enter keypress then committed that supposedly-cancelled text as the permanent source title. `renderNotebook()`'s v0.2.88 "preserve an in-progress rename across an *unrelated* background rebuild" logic (note add/delete, upload, etc.) keys off whether the rename `<input>` is still `document.activeElement` when the async reload completes. The Enter/`commit()` path avoids colliding with this only incidentally: `commit()` sets `input.disabled = true` before awaiting the PATCH, and disabling a focused form control forces the browser to blur it, so the input is no longer `document.activeElement` by the time `renderNotebook()` runs. The Escape handler (`input.onkeydown`, before this fix: `if(e.key==="Escape"){openNotebook(nb.id);}`) never disabled or blurred the input first — so it was still focused when the async `openNotebook()` reload completed, and the "preserve unrelated edit" logic misfired on the user's own deliberate cancellation, resurrecting an identical, still-focused rename box containing the exact text the user just tried to discard.

- Live-reproduced in a real browser (Playwright, Chromium) against a real running server: double-clicked a source title, typed `SHOULD_BE_DISCARDED` over the original, pressed Escape. Before the fix: the rename input remained present and focused, still containing `SHOULD_BE_DISCARDED`; the server-side title was still correctly unchanged at that point, but pressing Enter afterward (a natural instinct after Escape visibly "did nothing") committed it — `GET /api/notebooks/{id}` confirmed the source title was now permanently `SHOULD_BE_DISCARDED`, the text the user had explicitly tried to discard.
- Fix: the Escape branch now sets `committed = true` (making the existing `onblur` handler's `commit()` call a no-op via `commit()`'s own pre-existing `if (committed) return;` guard) and calls `input.blur()` before `openNotebook(nb.id)`, mirroring what the Enter path gets for free via `input.disabled = true`. This removes focus from the cancelled input before the async reload's `document.activeElement` check runs, so `renderNotebook()` no longer mistakes a deliberate cancellation for an unrelated in-progress edit worth preserving.
- Verified live after the fix with the identical reproduction: 0 rename inputs remain after Escape, and the server-side title stays correctly unchanged even after a stray Enter keypress that previously committed the discarded text.
- No pytest regression test added — this project's test suite has no Playwright/browser-automation coverage (frontend-only changes are verified live per CLAUDE.md's own UI-testing rule rather than via a persisted automated test, matching how the v0.2.88 UI-only fix was handled this session).

`pytest tests/` still runs 621 tests (no Python files changed this round). `mypy shoin/` and `ruff check shoin/` remain clean (no Python changes).

### v0.2.121 (2026-07-10)
**Fixed**: A forty-ninth background audit round found `build_context()`'s (`qa.py`) per-source token floor (`per_source = max(budget_tokens // len(order), 64)`) had no corresponding ceiling on the number of sources it applies to. The `64`-token floor exists so a handful of sources each get a *meaningful* minimum share rather than a near-zero one when `budget_tokens` is divided across them — but once `len(order)` exceeds `budget_tokens // 64`, the floor overrides the division entirely, and *total* consumption becomes `64 * len(order)`, unbounded in source count. `qa.ask()`'s call site never reaches this in practice (`retrieve(..., k=TOP_K)` caps `hits` at 8 distinct sources, and `8*64=512` is well under any reasonable budget), which is exactly why this had gone unnoticed — but `studio.py`'s `overview_hits()` samples chunks from **every** source in the notebook with no cap at all (`SELECT ... GROUP BY c.source_id`, no `LIMIT`), and `config.MAX_CHUNKS_PER_NOTEBOOK` only bounds total chunks, not source count. A notebook with many small sources (URL clippings, short documents — an entirely normal usage pattern, not an exotic edge case) fed an unbounded-source-count `hits` list straight into `build_context()` from both `generate()` and `suggest_questions()`.

- Live-reproduced against a real in-memory `Store`: seeded a notebook with 10 through 200 sources, called the real `overview_hits()` → `build_context(..., budget_tokens=STUDIO_BUDGET_TOKENS=2800)` pipeline, and measured actual cost via `estimate_tokens()`. The overshoot began almost exactly at the predicted threshold (`2800 // 64 = 43` sources) and grew linearly thereafter: 44 sources → 1.07x, 100 sources → 2.43x, 200 sources → 4.86x (13,600 tokens against a 2,800-token budget) — for the exact "8GB RAM, Qwen3-4B/4K–8K context" audience CLAUDE.md targets, a Studio-generation prompt whose source text alone already exceeds most local models' entire context window, before the system prompt is even added, with zero warning or truncation anywhere downstream (`generate()` sends this straight to `llm.chat()`). The same mechanism affects `suggest_questions()`'s `budget_tokens=1600` call (threshold at 25 sources).
- Fix: added `MIN_PER_SOURCE_TOKENS = 64` as a named constant (replacing the bare literal) and, before computing `per_source`, cap `order` to `order[:max(budget_tokens // MIN_PER_SOURCE_TOKENS, 1)]` — the number of sources the floor can actually support within `budget_tokens`. `order` is source-id-first-seen order, so this drops the lowest-priority tail rather than truncating arbitrarily. `qa.ask()`'s existing behavior is completely unchanged (8 sources is always well under any reasonable cap); `studio.py`'s previously-unbounded path now correctly caps total consumption instead of silently ballooning with notebook size.
- Verified live after the fix with the identical 200-source reproduction: total context cost is now capped at a small, constant overshoot (~1.06x, from structural `[S{idx}] {title}` header text overhead unrelated to this bug) regardless of whether the notebook has 44 or 200 sources — the linear unbounded growth is eliminated.
- 1 regression test added (`test_build_context_source_count_does_not_unboundedly_grow_budget`, `tests/test_qa.py`), seeding 200 sources and asserting both that total cost stays under `1.5×` the budget and that the included source count is capped to what the floor supports. Verified fail-then-pass via `git stash` on `shoin/qa.py` alone: pre-fix, the same 200-source setup produced 13,600 tokens against a 4,200-token (`budget*1.5`) ceiling.

`pytest tests/` now runs 621 tests (up from 620). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding is untouched by this change).

### v0.2.120 (2026-07-10)
**Fixed**: A forty-eighth background audit round found `_HTMLText`/`html_to_text()` (`ingest.py`) silently discarded all body content after an unclosed or mismatched-closer `<noscript>`/`<template>` tag **inside `<body>`** — the same bug class v0.2.40 fixed, but only for the `<head>` case (`handle_endtag()` resets `self._skip_depth = 0` exclusively on the `tag == "head"` branch). There was no equivalent recovery when the same tag dangled inside `<body>`: `_skip_depth` stayed elevated for the rest of parsing, and every subsequent `handle_data()` call was silently dropped (`if self._skip_depth: return`). Since content *before* the dangling tag already made it through, `text` was non-empty, so `extract_file()`/`extract_url()`'s `if not text: raise INGEST_EMPTY` guard never fired — ingestion silently "succeeded" with truncated content and zero error signal, exactly the failure mode v0.2.40/v0.2.96/v0.2.97 were each written to eliminate, just in an unswept location. `<script>`/`<style>` are real CDATA content elements (Python stdlib `HTMLParser.CDATA_CONTENT_ELEMENTS`) — an unclosed one legitimately swallowing to end-of-document matches real browser tokenizer spec, not a defect — but `<noscript>`/`<template>` are not CDATA elements, so their swallow-to-EOF behavior here was purely an artifact of this module's own balanced-counter implementation having no recovery path.

- Live-reproduced via the real public `extract_file()` API: an HTML document with `<noscript><img src="pixel.gif"></noscript-analytics>` (a realistic typo — a broken analytics snippet's mismatched closing tag) followed by two more paragraphs of real content. Before the fix: `extract_file()` returned success (kind `html`, correct title, non-empty text) with no exception and no `INGEST_EMPTY`, but "Section 2" and both following paragraphs were permanently absent from the extracted text — invisible to BM25/vector search and citations, with zero indication anything was lost. A genuinely unclosed `<template>` (no closing tag at all) reproduced identically.
- Fix: mirrored the exact neutralization technique already used for the `<!--` unclosed-comment bug (v0.2.97) — before parsing, count `<noscript`/`<template>` opens vs. well-formed `</noscript>`/`</template>` closes via precise regex (not a naive substring check, which would itself be fooled by a mismatched closer like `</noscript-analytics>`); if opens exceed closes, find the last unmatched opening tag's `>` and inject a synthetic closer immediately after it, converting it into an already-closed, empty element so real content following it parses normally instead of being buffered into `_skip_depth` forever. `<script>`/`<style>` are deliberately excluded from this neutralization, preserving their spec-correct swallow behavior.
- Verified both cases fixed via the same live reproduction, plus confirmed a well-formed, properly-closed `<noscript>` in `<body>` still correctly excludes its content (the fix is scoped to the unbalanced case only, no regression to normal skip behavior).
- 3 regression tests added (`tests/test_core.py`): `test_html_mismatched_noscript_closer_in_body_does_not_swallow_rest` (the realistic mismatched-closer reproduction), `test_html_unclosed_template_in_body_does_not_swallow_rest` (genuinely-unclosed `<template>`), and `test_html_well_formed_noscript_in_body_still_skipped` (control case, no regression). Verified fail-then-pass via `git stash` on `shoin/ingest.py` alone: pre-fix, both new "content must survive" tests failed with the trailing content missing entirely.

`pytest tests/` now runs 620 tests (up from 617). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding is untouched by this change).

### v0.2.119 (2026-07-10)
**Fixed**: A forty-seventh background audit round, pivoting from "hardcoded CJK range literals" (v0.2.118) to "hardcoded word-boundary detection," found `_truncate_tokens()` (`qa.py`) and `_tail()` (`chunk.py`) — both already fuzz-hardened for CJK-adjacency and long-run bugs in v0.2.114/115 — used `ch.isalnum() or ch == '_'` to detect word-run boundaries, while `estimate_tokens()` (the authoritative cost function both are documented to stay in lock-step with) computes word cost via `_WORD_RE = re.compile(r"[A-Za-z0-9_]+")` — **ASCII-only**. Python's `str.isalnum()` is Unicode-wide: true for Cyrillic, Greek, Armenian, Georgian, Devanagari, and all accented Latin (é, ü, ñ, ç, …) — none of which are CJK (so `is_cjk()` doesn't count them) and none of which `_WORD_RE` matches. A word like `"Müller"` was scanned as **one** contiguous run by the two truncation functions (flat 1-token base cost), while `_WORD_RE.findall("Müller")` returns `['M', 'ller']` — **two** separate 1-token matches, since the ASCII-only regex can't bridge across "ü". Both truncation functions therefore under-counted and let more text through than the caller's limit allowed, for any French/German/Spanish/Portuguese/Scandinavian/Turkish/Polish/Russian/Ukrainian/Bulgarian/Greek content mixed with ASCII — a large fraction of the non-English, non-CJK content this project otherwise goes out of its way to support.

- Live-reproduced: `_truncate_tokens('abcŦdef', 1)` returned `'abcŦdef'` in full (`estimate_tokens('abcŦdef')` is 2, not ≤1). Realistic French prose ("The café in Zürich serves crème brûlée and naïve tourists love café Beyoncé Müller" × 5) through both functions: `_truncate_tokens(text, 50)` and `_tail(text, 64)` each cost 1+ tokens over their requested limit before the fix, exactly matching each other after it. A 5,000-trial fuzz mixing ASCII/CJK/Cyrillic/accented-Latin/punctuation confirmed 0 overshoots for both functions post-fix.
- Fix: extracted `_is_word_char(ch)` in `chunk.py` — `ch.isascii() and (ch.isalnum() or ch == "_")`, i.e. exactly the character set `_WORD_RE` matches — and switched both `_tail()` and `_truncate_tokens()` (which imports the helper from `chunk.py`) to use it instead of the Unicode-wide `isalnum()` check. This is the same "single shared source of truth instead of a second hand-maintained copy that can silently drift" fix shape as v0.2.118's `_NEG_RE`/`_CJK_RANGES` consolidation and v0.2.80's `looks_like_question()` consolidation — three instances of the identical bug pattern found and closed this session.
- 2 regression tests added: `test_non_ascii_alphabetic_scripts_do_not_undercount` (`tests/test_qa.py`, `_truncate_tokens()`) and `test_tail_non_ascii_alphabetic_scripts_do_not_undercount` (`tests/test_core.py`, `_tail()`), both using the same realistic French-prose reproduction and asserting exact token-cost agreement with `estimate_tokens()`. Verified fail-then-pass via `git stash` on `shoin/chunk.py`+`shoin/qa.py` together: pre-fix, both tests failed with `6 != 5`.

`pytest tests/` now runs 617 tests (up from 615). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding is untouched by this change).

### v0.2.118 (2026-07-10)
**Fixed**: A forty-sixth background audit round found `_NEG_RE` (`search.py`, the `-word` negative-term search filter added in v0.2.47) hardcoded its own narrow, independently-maintained CJK character class (`[ぁ-ヿ一-鿿]`, hiragana/katakana/CJK-ideographs only) instead of reusing `chunk._CJK_RANGES` — the shared table `is_cjk()`, `query_terms()`, and `fts_query()` all already use, which additionally covers Hangul, Thai, Lao, Myanmar, Khmer, CJK Extensions A–H, CJK Compat, and fullwidth digits/letters. `neg_terms()`'s own docstring documents CJK negation generally ("`-word`, `-日本語`"), not "hiragana/katakana/kanji only." This is the same "two independently-maintained copies of one concept silently drifting apart" pattern this project's changelog already flagged and fixed once before (v0.2.80's `looks_like_question()` consolidation) — just never checked for this particular pair.

- **Concrete impact, worse than a silent no-op**: because `strip_neg_terms()` uses the same `_NEG_RE`, a `-word` token in an unsupported script wasn't stripped from the query either — it survived into the text, got tokenized by `query_terms()` as an ordinary positive search term (the leading `-` simply discarded at the tokenization boundary), and was treated as a **positive inclusion signal** by `fts_query()`/`bm25_search()`. A user's exclusion attempt was not just ignored, it was inverted.
- Live-reproduced: `neg_terms('-한국어')` (Hangul), `neg_terms('-ภาษาไทย')` (Thai), and `neg_terms('-㐅')` (CJK ext-A) all returned `[]` before the fix, while the docstring's own Japanese example (`neg_terms('Python -日本語')` → `['日本語']`) worked correctly — confirming the gap was script-specific, not a general negation failure. End-to-end through the real `retrieve()` pipeline: a notebook with one Korean-language source and one English source, queried with `'AI -한국어'`, returned **both** sources — the Korean one that the user explicitly tried to exclude was not filtered out. An ASCII control query (`'AI -legacy'`) correctly excluded its matching source, isolating the defect to `_NEG_RE`'s script coverage specifically.
- Fix: build `_NEG_RE`'s CJK character-class alternative from `chunk._CJK_RANGES` directly (`"".join(f"\\U{lo:08X}-\\U{hi:08X}" for lo, hi in _CJK_RANGES)`) instead of a second hand-picked literal, so negation support for a script can no longer silently drift out of sync with tokenization support for that same script — a future CJK range added to `_CJK_RANGES` (as happened three times already this session: v0.1.42 Thai/Lao/Myanmar/Khmer, v0.2.38 CJK ext B–H, v0.2.107 fullwidth digits/letters) now automatically gains negation support too.
- Verified live after the fix: all four previously-failing scripts (Hangul, Thai, CJK ext-A, fullwidth Latin) now correctly extracted and stripped; the end-to-end `retrieve()` reproduction now correctly excludes only the Korean source and returns the English one.
- 2 regression tests added (`tests/test_core.py`, `TestNegTerms`): `test_neg_terms_non_hiragana_katakana_kanji_scripts` (Hangul/Thai/CJK-ext-A unit coverage for `neg_terms()`/`strip_neg_terms()`) and `test_retrieve_neg_term_hangul_excludes_matching_source` (the real end-to-end `retrieve()` reproduction with two sources). Verified fail-then-pass via `git stash` on `shoin/search.py` alone: pre-fix, both tests failed exactly as the live reproduction predicted.

`pytest tests/` now runs 615 tests (up from 613). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding, now at a shifted line number from this diff, is untouched by this change).

### v0.2.117 (2026-07-10)
**Fixed**: A forty-fifth background audit round found `_cmd_ask()` (`cli.py`) had never actually closed the "lone `---` separator with nothing under it" bug class its own v0.2.27 changelog entry claims to have fixed. v0.2.27 guarded on `answer.hits and not answer.degraded`, but that only prevents the separator for the *degraded* (LLM-unreachable) case — it says nothing about whether the citation report itself has any content. v0.2.55 later fixed the identical bug class in the sibling `_cmd_studio()` with a *stronger* guard checking report content (`result.report["cited"] or result.report.get("uncited")`), but that stronger guard was never ported back to `_cmd_ask()`.

- **Concrete failing path**: a non-degraded, successful answer that correctly follows the system prompt's rule 3 ("if a fact is not in the sources, say so explicitly") — e.g. `"ソースに記載なし。"` — legitimately carries zero citations. `citation.uncited_sentences()` deliberately excludes disclaimer sentences like this from its `uncited` list (`citation.py`'s `_DISCLAIMER_MARKERS`, exactly the *correct* behavior, not a bug), so `report["cited"]`, `report["invalid"]`, and `report.get("uncited")` are all empty — but `answer.hits` is non-empty and `answer.degraded` is `False`, the exact combination `_cmd_ask`'s old guard let through.
- Live-reproduced: `shoin ask <nb> <question>` against a `FakeLLM` returning `"ソースに記載なし。"` for a notebook with one matching source printed `'ソースに記載なし。\n---\n'` — the separator with nothing below it.
- Fix: widened `_cmd_ask()`'s guard to also require actual report content, mirroring `_cmd_studio()`'s pattern: `if answer.hits and not answer.degraded and (answer.report["cited"] or answer.report["invalid"] or answer.report.get("uncited")):`.
- **A second, related bug found and fixed in the same function pair**: `_cmd_studio()`'s own v0.2.55 guard (`result.report["cited"] or result.report.get("uncited")`) never checked `invalid` — so a report with ONLY out-of-range citations (`[S99]` when only 1 source exists: `cited` empty, `invalid` non-empty) silently dropped the `---`/warning entirely, even though `_print_report()` does print something for that case (the "検証失敗の引用" out-of-range warning, `cli.py:152-154`). Live-reproduced: a `[S99]`-citing Studio output printed only the body text, with the invalid-citation warning completely missing from CLI output. Fixed with the same 3-way `cited or invalid or uncited` check, ported to `_cmd_studio()` too.
- 2 regression tests added (`test_ask_lone_separator_suppressed_for_non_degraded_empty_report`, `test_studio_invalid_only_report_still_prints_separator`, `tests/test_core.py`), both mocking `ask()`/`generate()` directly (following the existing `test_studio_no_citations_does_not_print_separator` v0.2.55 test pattern) rather than depending on a real LLM's exact wording. Verified fail-then-pass via `git stash` on `shoin/cli.py` alone: pre-fix, the ask test found `'---'` unexpectedly present, and the studio test found it unexpectedly absent.

`pytest tests/` now runs 613 tests (up from 611). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding is untouched by this change).

### v0.2.116 (2026-07-10)
**Fixed**: A forty-fourth background audit round, pivoting away from the now-thoroughly-audited token-estimation area, found `_h_ask_sse()` (`server.py`) had one remaining unguarded write in its own disconnect-handling chain — the same "orphaned user turn" bug class fixed three times already in this exact function (v0.2.39 `build_context()` exceptions, v0.2.49 the `meta`-send `ConnectionError`, v0.2.55 zero-token replies), just one statement earlier than any of them. `store.add_message(nb_id, "user", question, "{}")` persists the user's turn, and the very next statement — `self._headers(200, "text/event-stream; charset=utf-8", ...)`, the first write of the SSE response (`end_headers()` → `flush_headers()` → `self.wfile.write(...)`) — was completely unguarded. If the client had already disconnected by that point (a real, reachable window: retrieval just ran and can take time; the user can navigate away, cancel the fetch, or close the tab), `self.wfile.write()` raises `BrokenPipeError`/`ConnectionResetError` (`ConnectionError` subclasses), which propagated uncaught to `_dispatch()`'s generic exception handler — HTTP 500, and no code path ever ran to attach the compensating empty assistant message the three sibling fixes rely on.

- Live-reproduced against a real running server: patched `_Handler._headers` to raise `ConnectionError` specifically for the SSE content-type (simulating a client that disconnected right as headers were about to be sent), then asked a real question. Before the fix: `Internal error handling POST /api/notebooks/1/ask: ConnectionError`, HTTP 500, and `store.list_messages(nb_id)` showed exactly one row — the user's question — with zero assistant rows, reproducing the same "unanswered question on page reload" UX failure the v0.2.55 changelog entry describes, via a codepath none of the three prior fixes touch. Confirmed via grep that `tests/test_server.py`'s `SSEConnectionErrorTest` class (which exhaustively covers `ConnectionError` on the `meta`/`delta`/`done` `_sse()` calls) had zero coverage of `_headers()` itself.
- Fix: wrapped the initial `self._headers(...)` call in the same `try/except ConnectionError` pattern already used for the `meta`-send guard immediately below it — on failure, persist an empty assistant message (matching the three sibling guards exactly) and return.
- Verified live after the fix with the same reproduction: `store.list_messages(nb_id)` now shows the user row correctly paired with an empty assistant row, closing the pair.
- 1 regression test added (`test_headers_write_connection_error_does_not_orphan_user_turn`, `SSEConnectionErrorTest` in `tests/test_server.py`), patching `_Handler._headers` (not `_sse`, which the class's four existing tests all patch) to fail specifically on the SSE content-type. Verified fail-then-pass via `git stash` on `shoin/server.py` alone: pre-fix, `list_messages()` returned exactly 1 row instead of the expected paired 2.

`pytest tests/` now runs 611 tests (up from 610). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding is untouched by this change).

### v0.2.115 (2026-07-10)
**Fixed**: A forty-third background audit round, applying the same "unbounded-run assumption" lens that found v0.2.114's bug, found `_tail()` (`chunk.py`) — the sibling function `_truncate_tokens()`'s v0.2.114 fix was ported to — had two of its own defects in the same area, both concrete and live-reproduced rather than speculative:

1. **CJK-adjacency drops a run's entire token credit**: the `is_cjk(ch)` branch reset `run_len = 0` without ever crediting the interrupted alnum run's base 1-token cost, unlike the punctuation/space (`else`) branch a few lines below, which always did. This is idiomatic, common Japanese text — kanji directly abutting an ASCII model/section number/acronym with no space (`型番ABC123456`, `第3章`) — not an adversarial edge case. A 20,000-trial fuzz of mixed CJK/ASCII/punctuation text found **51% of cases** overshot the requested token budget by 1–15+ tokens because of this. Live-reproduced with real prose: `_tail("この製品の型番はABC123456であり、価格は書院にて公開されている。", 20)` returned 21 tokens, jumping straight past the requested 20. Through the real production path (`split_text()`'s chunk-overlap computation, `CHUNK_OVERLAP=64`), the actual overlap text for a realistic mixed-content document cost 66 tokens against the configured 64-token budget.
2. **A pathologically long pure-alnum run (no CJK/punctuation at all) overshoots by exactly 1 token**: the "run proves long, credit it immediately" logic added in v0.2.114 credited only the run's base cost at the threshold crossing, not the first unit of the length-weighted excess term that `_run_token_cost()`'s closed form (`1 + ceil((n-40)/4)`) also earns at that exact same character — so the two fell out of sync by 1 whenever the function returned early via that specific credit.
- Fix (1): the `is_cjk` branch now credits an interrupted run's base cost the same way the punctuation/space branch does, using a new `run_credited` flag to track whether a run's base cost has already been paid (needed because fix 2 also pays the base cost, earlier, for long runs — without the flag a long run's boundary credit would double-count it).
- Fix (2): the threshold-crossing credit now adds 2 in one step (`acc += 2`) — the base cost plus the excess term's first unit — matching `_run_token_cost()`'s formula exactly at that character, verified via a targeted fuzz (2000 trials of pure 41–5000-char alphanumeric runs against random small budgets): before this second fix, 23/2000 still overshot (by exactly 1 token each); after, 0/2000.
- Verified both fixes together with the same 20,000-trial mixed-content fuzz used to find bug 1: 0/20,000 overshoots (down from 10,180/20,000 pre-fix), and re-verified 0 mid-word cuts (the pre-existing "never cut a short word mid-word" guarantee, unaffected).
- 2 regression tests added (`tests/test_core.py`): `test_tail_credits_alnum_run_interrupted_by_cjk_character` (the realistic Japanese-prose reproduction, sweeping token budgets 10–24) and `test_tail_long_run_overlap_matches_chunk_overlap_budget` (the real `split_text()` → `_tail()` production call pattern with `CHUNK_OVERLAP`, not just an isolated unit call). Verified fail-then-pass via `git stash` on `shoin/chunk.py` alone: pre-fix, `_tail(text, 20)` returned 21 tokens and the real chunk-overlap computation returned 66 tokens against the 64-token budget.

`pytest tests/` now runs 610 tests (up from 608). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding is untouched by this change).

### v0.2.114 (2026-07-10)
**Fixed**: A forty-second background audit round found `estimate_tokens()`/`_truncate_tokens()`/`_tail()` (`chunk.py`/`qa.py`) undercount by an unbounded, arbitrarily large factor for any single unbroken alphanumeric run — a fundamentally different, more general defect than the specific CJK-script-coverage gaps already fixed (v0.2.50 Arabic/Hebrew/Cyrillic, v0.2.52 the zero-token `build_context()` guard, v0.2.107 fullwidth digits/letters). `_WORD_RE = re.compile(r"[A-Za-z0-9_]+")` (`chunk.py`) matches an entire unbroken run as ONE regex match; `estimate_tokens()` (pre-fix) computed `words = len(_WORD_RE.findall(text))` — counting **one match as exactly one token, regardless of the run's length**. A base64 `data:` URI, a long hex hash, a genomic sequence, or minified/obfuscated code with no whitespace all produce exactly this shape. Because the resulting estimate is a small *nonzero* number (not exactly 0), it evades every existing `tok == 0` special-case fallback from v0.2.50/52/107 — those fixes only trigger when a chunk's *entire* estimate is zero.

- **Concrete impact, live-reproduced**: a synthetic 200,078-character document containing one 200,000-character random alphanumeric run estimated to **14 tokens** (should be ~50,000). `split_text()` (`chunk.py`, `if estimate_tokens(block) > chunk_tokens:`) never triggered `_hard_split()` at all — the entire 200KB blob was stored as a single unsplit DB chunk, bypassing `CHUNK_TOKENS=512` completely. Through the real `build_context()` (`qa.py`, default `SOURCE_TEXT_TOKENS=1000` budget), a 300,000-character version of the same blob was placed **verbatim, untruncated**, into the LLM prompt (`ctx.block` length: 300,124 characters) — silently defeating the documented per-source token budget on every single query that retrieves that chunk, for exactly the kind of lightweight 4–8GB-RAM local-model deployment this project explicitly targets.
- Fix: added `_LONG_RUN_THRESHOLD = 40` (chars) — words/identifiers up to this length keep costing a flat 1 token, matching CLAUDE.md's documented "ASCII words: 1 token per word" model and leaving every existing normal-word test assertion (e.g. `"parse_user_input"` == 1 token, v0.2.28) completely unaffected. A run *longer* than the threshold is weighted at ~4 chars/token beyond it (`chunk._run_token_cost()`, a new small helper: `1 + (n - 40 + 3) // 4` for `n > 40`). `_truncate_tokens()` (`qa.py`) and `_tail()` (`chunk.py`) were updated with matching incremental logic — tracking a per-run character counter and crediting interim tokens every ~4 chars once a run exceeds the threshold — so both can now stop *mid-run* instead of unconditionally including (or, for `_truncate_tokens`, unconditionally passing through) the entire pathological run regardless of the requested limit. `_truncate_tokens()` imports `_LONG_RUN_THRESHOLD` directly from `chunk.py` rather than redefining it, so the three functions (already required to stay in sync per the v0.2.28 changelog entry) cannot independently drift.
- Verified both live reproductions after the fix: the 200,078-char document now estimates ~50,004 tokens and `split_text()` produces 98 chunks; `_truncate_tokens(blob, 50)` on a 50,000-char run now returns 236 chars (proportional to the limit) instead of the full 50,000; the 300,000-char `build_context()` case now correctly bounds `ctx.block` to 4,063 characters instead of 300,124.
- 2 regression tests added: `test_long_unbroken_ascii_run_not_undercounted_to_near_zero` (`tests/test_core.py`, covering `estimate_tokens()`, `split_text()`, and `_tail()` together, plus a control assertion that a normal-length identifier is still exactly 1 token) and `test_long_unbroken_run_is_bounded_not_sent_through_untouched` (`tests/test_qa.py`, covering `_truncate_tokens()` plus the same normal-identifier control). Verified fail-then-pass via `git stash` on `shoin/chunk.py`+`shoin/qa.py` together: pre-fix, a 3000-char run estimated to 9 tokens (not >600) and a 50,000-char run truncated to 50 tokens still returned all 50,000 characters unbounded.

`pytest tests/` now runs 608 tests (up from 606). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding is untouched by this change).

### v0.2.113 (2026-07-10)
**Fixed (docs)**: A forty-first background audit round, following on from v0.2.112's discovery that a "Known Weaknesses" claim was stale, spot-checked the rest of that section and the "Core Concepts" search prose against the actual code and found two more drifted claims in this same file:

1. **`TOP_K` misstated as 10**: "Known Weaknesses & Tech Debt" → "Lightweight LLM Compatibility" claimed "Very large notebooks (500+ sources) retrieve only TOP_K=10 sources" — but `config.py: TOP_K = 8` has been 8 since the constant was introduced; every call site (`search.py`, `qa.py`, `cli.py`) uses this default unmodified, and production `server.py` never overrides it. Live-reproduced: `retrieve()` against a real 20-chunk notebook with default `k` returned exactly 8 hits. Notably, this exact same file's own v0.2.100 changelog entry (written 13 versions earlier) already correctly states "`TOP_K` (8)" in its own reproduction text — the file had been internally self-contradictory (line 176 said 10, the v0.2.100 entry said 8) since 2026-07-08, unnoticed through 41 rounds of self-auditing until this one actually diffed the two claims.
2. **Stale pre-RRF fusion description**: "Core Concepts" → "Vector + BM25 Hybrid Retrieval" still described the pre-v0.2.56 convex-combination design ("Fusion (Convex Combination): `score = alpha * vec_score + (1-alpha) * bm25_score`... Adaptive alpha... Min-max normalization") as if it were the current pipeline. `search.py`'s actual `retrieve()` has called `rrf_fuse(bm25_hits, vec_hits)` exclusively since v0.2.56 — `fuse()`/`adaptive_alpha()` still exist in the module only for backward compatibility with older tests, per that function's own docstring, and are never invoked by `retrieve()`.

- Fix: corrected `TOP_K=10` → `TOP_K=8`. Rewrote the Fusion subsection to describe RRF (the actual current algorithm, `score = Σ 1/(k+rank+1)`, k=60) and explicitly note `fuse()`/`adaptive_alpha()` are retained-but-unused legacy code, not the live path.
- No code changes — both are documentation-only corrections, verified against the actual running `retrieve()`/`config.TOP_K` rather than assumed from memory. No regression test applicable.

`pytest tests/` still runs 606 tests (no Python files changed this round). `mypy shoin/` and `ruff check shoin/` remain clean (no Python changes).

### v0.2.112 (2026-07-10)
**Fixed (docs)**: A fortieth background audit round found the "No Batch Embeddings API Support" section of this very file (the "Known Weaknesses & Tech Debt" section) was factually false and had been for the entire session — no prior audit round had ever checked a "Known Weaknesses" doc claim against the actual code. It claimed `ChatBackend.embed_one(text)` embeds one chunk per HTTP request ("Ingest of a 100-chunk source triggers 100 HTTP requests") and that batching was unimplemented ("Ollama and llama.cpp have different batch API signatures... A batch API would require vendor detection or optional configuration"). Neither claim was ever true of the actual code: `LLMClient.embed(texts: list[str])` (`llm.py`) has always sent a single `POST /embeddings` with `{"model": ..., "input": texts}` — the standard OpenAI-compatible batch shape — and `_embed_chunks()` (`pipeline.py`) has always preferred it over `embed_one()`, grouping texts into batches of `EMBED_BATCH = 16` per HTTP request. `embed()` predates essentially this entire changelog (added in v0.1.29, well before the v0.1.37 start of this document's version history).

- Live-reproduced: ran `pipeline.index_source()` against a real `Store` with a `FakeLLM` tracking separate call counts for `embed()` (batch) and `embed_one()` (per-chunk), on a 50-chunk source. Result: `embed() batch calls: 4` (= `ceil(50/16)`), `embed_one() calls: 0` — confirming production ingest exclusively uses the batch path. For the doc's own "100-chunk source" example, the true request count is `ceil(100/16) = 7`, not 100.
- **Concrete impact of the stale doc**: a contributor reading "Why Not Fixed" would believe batching is architecturally hard and unimplemented, risking either wasted re-implementation effort or avoidance of the area under a false premise; a user diagnosing slow ingest would be given a wrong root-cause model (O(n) requests instead of the actual O(n/16)).
- Fix: rewrote the section (renamed to "Embedding Batch Size Is a Fixed Constant") to describe what's actually true — batching exists and is preferred — and narrowed the "remaining gap" to something genuinely unaddressed: `EMBED_BATCH=16` is a hardcoded module constant in `pipeline.py` with no environment-variable override, confirmed via `grep -n EMBED_BATCH shoin/*.py` (only the one definition and its two use sites, no config plumbing anywhere).
- No code changes — this is a documentation-only correction. No regression test applicable (nothing in the test suite asserts CLAUDE.md prose); the live reproduction above is the verification.

`pytest tests/` still runs 606 tests (no Python files changed this round). `mypy shoin/` and `ruff check shoin/` remain clean (no Python changes).

### v0.2.111 (2026-07-10)
**Fixed**: A thirty-ninth background audit round found CLI `note add` (`_cmd_note()`, `cli.py`) was the sixth site of the same title-echo-mismatch bug class fixed five times already this session (v0.2.93 `_h_src_upload`, v0.2.94 `_h_src_patch`, v0.2.95 CLI `source rename`, v0.2.99 CLI `notebook rename`): `store.add_note()` (`store.py:594`) does `title = title.strip()` before persisting, but the CLI's confirmation `print()` used the raw, unstripped `str(args.title)` — a call site never given the same treatment.

- Live-reproduced: `shoin note add 1 "  Padded Title  " "body"` printed `追加: [1]   Padded Title  ` (leading/trailing spaces intact), while `store.list_notes()` confirmed the persisted row actually holds `'Padded Title'` — a false statement about the user's own data, identical in class and impact to the five prior fixes.
- Fix: compute the stripped title once and use it for both the `add_note()` call and the confirmation message, mirroring the exact pattern already used for `source rename` (`cli.py`) and `notebook rename` (`cli.py`).
- 1 regression test added (`test_note_add_cli_message_matches_persisted_stripped_title`), using an exact string comparison against `_t()`'s own template output — not a `.strip()`'d substring check, which would mask this exact bug since both the buggy padded value and the fixed value strip down to the same substring (the same trap the v0.2.99 changelog entry called out). Verified fail-then-pass via `git stash` on `shoin/cli.py` alone: pre-fix, the printed message was `'追加: [1]   Padded Title  \n'` against an expected `'追加: [1] Padded Title\n'`.

`pytest tests/` now runs 606 tests (up from 605). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding is untouched by this change).

### v0.2.110 (2026-07-10)
**Fixed**: A thirty-eighth background audit round found `build_messages()` (`qa.py`) could reintroduce the exact consecutive-same-role prompt bug this codebase already fixed twice for other causes (v0.1.52 "Role Deduplication," v0.2.20 leading-assistant guard) — this time via `history_messages()`'s own v0.2.76 fix. When the most recent assistant reply was persisted with an empty body (a zero-token reasoning-model response, or any of the SSE disconnect/`build_context`-exception paths from v0.2.39/v0.2.49/v0.2.55 that intentionally persist `store.add_message(nb_id, "assistant", "", ...)`), `history_messages()` correctly keeps the preceding user turn (v0.2.76's whole point — treating it as answered, not orphaned) but the returned list then legitimately ends in a `"user"` role. `build_messages()` (qa.py:285-293, pre-fix) unconditionally appended `{"role": "user", "content": user}` right after `*(history or [])` with no check for this, so the final prompt sent to the LLM ended `[..., {"role": "user", ...}, {"role": "user", ...}]` — two consecutive user turns, violating OpenAI API alternation, exactly the invariant `history_messages()`'s own inline comment says must never happen ("would give the LLM two consecutive user messages... which is semantically wrong").

- **Why this survived undetected**: the existing regression test for the underlying scenario, `test_empty_assistant_reply_is_not_treated_as_orphan` (v0.2.76), only asserts on `history_messages()`'s and `expand_query()`'s output in isolation — it never carries the result into `build_messages()`, so it passed while the full pipeline was broken. The real production path (`qa.ask()` at qa.py:368/383, and `server.py:624/693` `_h_ask_sse()`) calls `expand_query(question, history)` and `build_messages(question, context, history)` with the identical `history` list, reproducing the bug end-to-end.
- Live-reproduced: seeded a store with Q1→A1 (normal), then Q2 whose assistant reply was persisted empty. `history_messages()` correctly returned `[..user:Q1, assistant:A1, user:Q2]`. `build_messages("Q3", ctx, hist)` roles were `['system', 'user', 'assistant', 'user', 'user']` — the trailing `Q2` and the new `Q3` turn both role `"user"`.
- Fix: `build_messages()` now takes a local copy of `history` and drops a trailing `"user"`-role entry before appending the new user turn, so the prompt always alternates correctly. This is safe because both real call sites already pass the same `history` list to `expand_query()` **before** calling `build_messages()` — retrieval expansion still sees and uses the trailing turn (preserving v0.2.76's actual purpose of anchoring follow-up retrieval to the real most-recent question); only the LLM-facing prompt now independently guarantees valid alternation. No change to `history_messages()` itself was needed — its careful orphan-vs-empty-reply distinction is untouched.
- 1 regression test added (`test_empty_assistant_reply_history_does_not_break_role_alternation`, `tests/test_qa.py`), carrying the exact v0.2.76 scenario all the way through `build_messages()` and asserting no two consecutive roles match, plus that the trailing `Q2` turn is dropped while the new `Q3` turn survives. Verified fail-then-pass via `git stash` on `shoin/qa.py` alone: pre-fix, the test failed with `roles = ['system', 'user', 'assistant', 'user', 'user']`.

`pytest tests/` now runs 605 tests (up from 604). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding is untouched by this change).

### v0.2.109 (2026-07-10)
**Fixed**: A thirty-seventh background audit round found `renderWithSeals()` (`index.html`) never NFKC-normalized message text before scanning it for `[S#]` citation markers, unlike the backend's `extract_citations()` (`citation.py:44-45`), which normalizes with `unicodedata.normalize("NFKC", text)` before applying its bracket/S-number regexes — a normalization `citation.py`'s own comment says exists specifically because full-width `Ｓ` "common from JP-first models" needs to match. CLAUDE.md's own "Source Grounding via S-numbers" section documents `[Ｓ１]`/full-width digits as explicitly supported input. The frontend's `reB = /\[([^\[\]]+)\]/g` only matches ASCII brackets (U+005B/5D, not U+FF3B/FF3D) and the inner `/[Ss]\s*(\d+)/g` only matches ASCII digits — so a JP-first local model (the exact model class Shoin targets: Qwen3-4B, Phi-4, Gemma-3, with `ui_lang()` defaulting to `ja`) emitting `［Ｓ１］` produced a citation the backend correctly parsed, verified, and counted toward `coverage`, but the chat/Studio UI rendered as inert plain text with no seal-chip styling, no click-to-view-source, and no verification-status color — silently breaking the product's own headline UX claim ("clicking `[S1]` jumps to the first relevant source") for exactly the input class the backend was hardened for.

- Live-reproduced in a real browser (Playwright, Chromium): started `make_server()` with a `FakeLLM` streaming `"回答です。"` + `"［Ｓ１］"`, uploaded a source, asked a question through the actual UI form (`#askInput`/`#askBtn`). Before the fix, the rendered chat bubble was `回答です。［Ｓ１］` — the full-width citation left as dead, unstyled text, `button.seal` count 0. The server-side citation report simultaneously confirmed `[1]` cited/confirmed for the identical text, proving the mismatch is real and not just a hypothetical Python-vs-JS regex gap.
- Fix: `renderWithSeals()` now normalizes with `text = text.normalize("NFKC")` (a standard JS method, zero dependency, matching the project's zero-external-dependency policy) as its first step, exactly mirroring `citation.py`'s NFKC-before-regex order, so `［Ｓ１］` → `[S1]` and the existing bracket/digit regexes correctly pick it up.
- Verified live in the same browser harness after the fix: the identical full-width citation now renders as a proper `button.seal` chip (`S1`, clickable, correctly styled/titled from the citation report). Also re-verified the pre-existing ASCII case (`"回答 " + "[S1]。"`, matching `tests/test_server.py`'s `FakeLLM` default reply) still renders correctly with zero regression and zero console/page errors.
- No pytest regression test added — this project's test suite has no Playwright/browser-automation coverage (frontend-only changes are verified live per CLAUDE.md's own UI-testing rule rather than via a persisted automated test, matching how the v0.2.88 UI-only fix was handled this session).

`pytest tests/` still runs 604 tests (no Python files changed this round). `mypy shoin/` and `ruff check shoin/` remain clean (no Python changes; the one pre-existing `search.py` F541 finding is untouched).

### v0.2.108 (2026-07-10)
**Fixed**: A thirty-sixth background audit round found `looks_like_question()` (`citation.py`, the shared question-detection heuristic centralized in v0.2.80 after four successive drift-fixes) failed to recognize English questions beginning with a contraction — "What's", "Who's", "Where's", "When's", "How's", "Why's". `_EN_QUESTION_STARTERS` (line 73-75) is a frozenset of bare words ("what how why when where who which does is are was were will would could should can"); the lookup at line 98 took `norm.split()[0].lower()` with no handling for a trailing `'s`, so a question starting with a contraction produced `first_word == "what's"` etc., which matches nothing in the frozenset — the exact same class of gap (an English question with no trailing `?`) that v0.2.37/v0.2.77-80 fixed for the bare-word case, just never extended to contractions.

- Live-reproduced: `looks_like_question("What's the deadline for the project.")` returned `False` (should be `True`); `uncited_sentences("What's the deadline for the project. How does this affect the budget.")` correctly left the bare-"how" sentence alone but wrongly flagged the contracted "What's..." sentence as an unsupported claim — an equally genuine, equally citation-less question. The identical gap independently breaks `studio.py`'s `suggest_questions()` (same shared function): an LLM-produced suggestion line like `"What's the total budget for the project"` (no trailing `?`) is silently dropped from the suggestion list instead of shown to the user.
- Fix: strip a trailing contraction before the frozenset lookup — `first_word = norm.split()[0].lower().split("'")[0]` — so `"what's"` → `"what"`, `"who's"` → `"who"`. The split-based approach also naturally handles other contractions (`"What'll"`, `"Who'd"`) without enumerating each one. Verified `"Whatever"` (shares the `"what"` prefix but is not a contraction of it) is correctly NOT treated as a question — `"whatever".split("'")[0] == "whatever"`, which isn't in the frozenset.
- 1 regression test added (`test_ignores_english_contraction_question_without_trailing_mark`, `tests/test_core.py`), covering "What's"/"Who's"/"Where's" plus the "Whatever" non-regression case; verified fail-then-pass via `git stash` on `shoin/citation.py` alone.

`pytest tests/` now runs 604 tests (up from 603). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding is untouched by this change).

### v0.2.107 (2026-07-10)
**Fixed**: A thirty-fifth background audit round found `_CJK_RANGES` (`chunk.py`) omitted the Fullwidth Forms alphanumeric sub-ranges (U+FF10–19 fullwidth digits ０-９, U+FF21–3A/FF41–5A fullwidth Latin Ａ-Ｚ/ａ-ｚ). These are extremely common in real Japanese documents — invoices, ID/account numbers, prices, dates conventionally use zenkaku digit formatting — unlike the already-fixed Arabic/Hebrew/Cyrillic case (v0.2.52), which is rare for Shoin's JA-focused audience. `is_cjk()` returned `False` for them and the ASCII-only `_WORD_RE` never matched them either, so `estimate_tokens()` undercounted a long run of fullwidth digits to near-zero regardless of length — and because any chunk containing even one ordinary kanji/ASCII word made the chunk's overall `cost > 0`, the v0.2.50/v0.2.52 zero-token-script char-based fallback (which only triggers when a chunk's *entire* token count is exactly 0) never engaged either.

- **Concrete impact, live-reproduced**: a 12,002-character block (`"項目" + "０１２３４５６７８９" * 1200`) estimated at only 2 tokens instead of ~12,000, so `_hard_split()`'s `CHUNK_TOKENS=512` cap was never triggered at ingestion time — `split_text()` returned it as a single unsplit chunk. Separately, through the real `build_context()`, a single `Hit` of `"本" + "０" * 50000` (50,001 chars) sailed through the documented `SOURCE_TEXT_TOKENS=1000` budget completely untruncated (50,028 chars placed into the LLM prompt) — the exact failure mode v0.2.50/v0.2.52 were written to prevent, just for a far more common real-world script for this project's stated JA-focused audience.
- Fix: added the three Fullwidth Forms alphanumeric ranges to `_CJK_RANGES`. Since `is_cjk()`/`_CJK_RANGES` live in `chunk.py` and are imported directly by `qa.py` (`_truncate_tokens`, `_tail`) and `search.py` (`query_terms`, `fts_query`), this single change fixes `estimate_tokens()`, `_hard_split()`, and `_truncate_tokens()` together, and as a side benefit stops `query_terms()` from silently dropping fullwidth-digit search terms (they previously matched neither the CJK-run branch nor the ASCII `_WORD_RE` branch).
- Verified both reproductions after the fix: the 12,002-char block now correctly estimates 12,002 tokens and splits into 24 chunks of ~512–576 tokens each; the 50,001-char `build_context()` case now correctly truncates to 1,027 chars for a 1000-token budget.
- 2 regression tests added: `test_is_cjk_fullwidth_digits_and_letters` (`tests/test_core.py`) and `test_build_context_fullwidth_digits_do_not_bypass_budget` (`tests/test_qa.py`); verified fail-then-pass via `git stash` on `shoin/chunk.py` alone — both failed with the exact pre-fix undercounted/untruncated values before the fix, passed after.

`pytest tests/` now runs 603 tests (up from 601). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding is untouched by this change).

### v0.2.106 (2026-07-10)
**Fixed**: A thirty-fourth background audit round, tracing `_h_src_upload()`'s (`server.py`) `X-Filename` header handling back to its only caller, found the correctness of the whole function depends entirely on an undocumented, unenforced convention: `index.html:866` always sends `X-Filename: encodeURIComponent(f.name)`. Python's stdlib `http.server`/`email.parser` decodes header bytes as Latin-1, not UTF-8 — a client that sends a non-ASCII filename as raw UTF-8 bytes (not percent-encoded) gets a silently, permanently corrupted title with zero error signal, since `urllib.parse.unquote()` only reverses `%XX` escapes and cannot repair a Latin-1 mis-decode.

- **Concrete impact, live-reproduced**: opened a raw socket and POSTed to `/api/notebooks/{id}/upload` with the literal UTF-8 bytes for `日本語.txt` in the `X-Filename` header (no percent-encoding). Before the fix: `HTTP/1.0 201 Created`, `{"source": {"id": 1, "title": "æ\x97¥æ\x9c¬èª\x9e.txt"}, ...}` — the upload succeeds and the title is permanently mojibake, with no indication anything went wrong. Any non-browser client (a curl script, a different frontend, a future mobile app) that didn't know about the `encodeURIComponent()` convention would hit this on every non-ASCII filename.
- Fix: before the existing `unquote()`/`Path(...).name` processing, attempt a guarded Latin-1→UTF-8 round-trip on the raw header value (`header_name.encode("latin-1").decode("utf-8")`, falling back to the original value on `UnicodeDecodeError`/`UnicodeEncodeError`). This recovers the correct Unicode text for a raw-UTF-8-bytes client. The existing `index.html` percent-encoded path is unaffected: percent-encoded ASCII round-trips through Latin-1→UTF-8 unchanged (ASCII is valid in both encodings), so `unquote()` still runs correctly afterward and decodes the `%XX` escapes as before.
- Verified both paths after the fix with the same raw-socket reproduction: the raw-UTF-8-bytes case now correctly persists `"日本語.txt"`; the existing percent-encoded case (`index.html`'s actual behavior) continues to work unchanged.
- 2 regression tests added to `tests/test_server.py`: `test_upload_raw_utf8_filename_header_not_mojibake` (uses `http.client` with `putheader()` passed raw UTF-8 bytes directly, bypassing any client-side percent-encoding, to prove the header-decode fix rather than a client encoding convention) and `test_upload_percent_encoded_filename_still_works` (confirms the existing convention is unaffected). Verified fail-then-pass via `git stash` on `shoin/server.py` alone: the mojibake test failed with the exact pre-fix corrupted string (`'æ\x97¥æ\x9c¬èª\x9e.txt' != '日本語.txt'`) before the fix, passed after.

`pytest tests/` now runs 601 tests (up from 599). `mypy shoin/` and `ruff check shoin/` remain clean (the one pre-existing `search.py` F541 finding is untouched by this change).

### v0.2.105 (2026-07-08)
**Fixed**: A thirty-third background audit round, explicitly instructed to do an exhaustive final sweep of every `sqlite3.IntegrityError` catch site in the codebase (following the v0.2.53/v0.2.86/v0.2.104 pattern found three times already), found the "port the fix to every sibling" discipline had itself been applied to only 3 of 7 sites. `grep -rn "except sqlite3.IntegrityError" shoin/` confirmed all 7 catch sites live in `store.py` (none in `pipeline.py`/`server.py`/`qa.py`/`studio.py`/`cli.py`); 4 more still used the old bare, unconditional form, unconditionally re-raising a misleading error code for *any* `IntegrityError`:

- `add_note()` — always raised `NOTEBOOK_NOT_FOUND`
- `add_studio_output()` — always raised `NOTEBOOK_NOT_FOUND`
- `add_message()` — always raised `NOTEBOOK_NOT_FOUND`
- `update_source_sha256()` — always raised `SOURCE_ALREADY_EXISTS`

- Live-reproduced all 4: `add_message(nb.id, "user", None, "{}")`, `add_note(nb.id, "title", None)`, and `add_studio_output(nb.id, "briefing", None, "{}")` (each triggering a genuine `NOT NULL` violation, not a deletion) all raised `NOTEBOOK_NOT_FOUND` with the notebook demonstrably still present; `update_source_sha256(src.id, None, "title")` (a `NOT NULL` on `sources.sha256`) raised `SOURCE_ALREADY_EXISTS` instead of reflecting the real cause.
- Fix: `add_note()`/`add_studio_output()`/`add_message()` now check `"FOREIGN KEY" in str(e)` before mapping to `NOTEBOOK_NOT_FOUND` (none of these three tables have a `UNIQUE` constraint, so FK-vs-else is the complete discrimination). `update_source_sha256()` — an `UPDATE` that never touches `notebook_id`, so no FK violation is possible there at all — checks `"UNIQUE" in str(e)` before mapping to `SOURCE_ALREADY_EXISTS` instead. All four now raise `SYSTEM_INTERNAL_ERROR` for anything else, matching the established pattern.
- 4 regression tests added, one per function, plus verified every genuine FK/UNIQUE case (concurrent notebook deletion, real sha256 collision) still classifies correctly. Verified fail-then-pass for all 4 via `git stash` as with every fix this session. The `sqlite3.OperationalError`/`SYSTEM_DB_LOCKED` sibling-classification family was also swept this round and confirmed fully consistent — no further gaps there.

`pytest tests/` now runs 599 tests (up from 595). `mypy shoin/` and `ruff check shoin/store.py` remain clean.

### v0.2.104 (2026-07-08)
**Fixed**: A thirty-second background audit round, deliberately trying a cross-cutting consistency angle rather than another field-specific hunt, found `add_chunks()` (`store.py`) was a third sibling with the exact IntegrityError-misclassification bug v0.2.53 fixed in `add_source()` and v0.2.86 fixed in `replace_chunks_for_source()` — never ported to this one. All three share the identical `INSERT ... source_id REFERENCES sources(id)` FK shape; `add_chunks()` still had the pre-v0.2.53 catch-all: any `sqlite3.IntegrityError` at all — not just a genuine `FOREIGN KEY` violation from concurrent deletion — was reported as `SOURCE_NOT_FOUND`.

- Live-reproduced: `add_chunks(src.id, [None])` (triggering a `NOT NULL` violation on `chunks.text`, not a deletion) raised `StoreError("SOURCE_NOT_FOUND", "source 1 was deleted during chunk insertion")` while the source demonstrably still existed — a fabricated diagnosis identical in class to the exact bug already fixed twice elsewhere.
- **Concrete impact**: `server.py` maps any `*_NOT_FOUND` code straight to HTTP 404. A future schema tightening (a `CHECK` constraint, a caller bug passing wrong-typed data past mypy at runtime) would surface as a spurious "source not found" 404 instead of a genuine 500, actively misleading users and sending debugging effort toward a nonexistent race condition instead of the real defect.
- Fix: mirror the identical pattern already used in the other two siblings — check `"FOREIGN KEY" in str(e)` before mapping to `SOURCE_NOT_FOUND` (the genuine concurrent-deletion case); anything else raises `SYSTEM_INTERNAL_ERROR`.
- 1 regression test added (`test_add_chunks_non_fk_integrity_error_raises_internal_not_not_found`); verified fail-then-pass via `git stash` as with every fix this session. The existing genuine-deletion test (`test_add_chunks_with_deleted_source_raises_source_not_found`) continues to pass unchanged, confirmed directly alongside the fix.

`pytest tests/` now runs 595 tests (up from 594). `mypy shoin/` and `ruff check shoin/store.py` remain clean.

### v0.2.103 (2026-07-08)
**Fixed**: A thirty-first background audit round found the v0.2.102 fix for `_file_config()` was itself incomplete — it only filtered JSON `null`, the specific case investigated that round, not the general principle its own comment claimed ("behaves like key not present... matching this function's own documented 'config.json is optional' contract"). Any other non-string JSON value — `true`/`false`, a list, a dict — was still blindly `str()`-coerced into a garbage setting string (`"True"`, `"['qwen3:4b']"`, `"{'a': 1}"`) instead of being treated as absent.

- **Concrete impact**: a user hand-editing `~/.config/shoin/config.json` making the natural mistake of wrapping a value in an array (e.g. copying from an array-valued example, `{"SHOIN_LLM_MODEL": ["qwen3:4b"]}`) got `llm_model() == "['qwen3:4b']"`, sent verbatim as the `"model"` field in every `/chat/completions` request — breaking all LLM generation with an opaque HTTP error and zero diagnostic pointing back to the config file.
- Live-reproduced: `{"SHOIN_LLM_MODEL": ["qwen3:4b"], "SHOIN_LANG": true, "SHOIN_DATA_DIR": {"a": 1}}` produced exactly the garbage strings described above pre-fix.
- Fix: generalized the filter to explicitly allow only `str`/`int`/`float` scalars (checking `bool` first and excluding it, since `bool` is an `int` subclass in Python) and reject `None`/`list`/`dict` entirely. Plain unquoted numbers are deliberately still allowed through (`{"SHOIN_PORT": 8080}` is a natural, common way to write a port number in JSON) — confirmed this still works correctly alongside the new rejections.
- 1 regression test added (`test_config_json_non_string_scalar_and_container_values_ignored`), covering list/bool/dict rejection and legitimate-int acceptance in one pass; verified fail-then-pass via `git stash` as with every fix this session. The v0.2.102 null-specific test continues to pass unchanged.

`pytest tests/` now runs 594 tests (up from 593). `mypy shoin/` and `ruff check shoin/config.py` remain clean.

### v0.2.102 (2026-07-08)
**Fixed**: A thirtieth background audit round found `_file_config()` (`config.py`, v0.2.69) called `str(v)` unconditionally on every JSON value in config.json — a JSON `null` (a well-formed value, e.g. a user writing `{"SHOIN_EMBED_MODEL": null}` intending "unset," a natural JSON idiom) was coerced to the literal string `"None"` instead of being treated as absent. This silently produced a wrong, truthy setting value rather than falling through to env/built-in-default the way the function's own docstring implies for unset settings ("config.json is optional... callers always let a set environment variable take precedence").

- **Concrete impact**: `embed_model()` would return the string `"None"` instead of degrading to BM25-only search, since the codebase's `(embed_model or "").strip()` guards throughout `qa.py`/`llm.py` only catch an empty string, not the string `"None"` — a truthy value gets sent as a real model name to the embeddings endpoint on every request. The same coercion corrupts `SHOIN_LLM_URL` → `"None"` and `SHOIN_DATA_DIR` → a bogus directory literally named `None` under cwd.
- Live-reproduced: a config.json with `{"SHOIN_EMBED_MODEL": null}` produced `embed_model() == "None"` before the fix.
- Fix: filter out `None` values in `_file_config()`'s dict comprehension, so a JSON `null` behaves like an absent key and correctly falls through to env/default — matching the function's own documented contract.
- 1 regression test added (`test_config_json_null_value_falls_back_instead_of_becoming_literal_none`); verified fail-then-pass via `git stash` as with every fix this session. The existing `TestConfigXDG` suite (missing file, malformed JSON, non-dict top-level) continues to pass unchanged.

`pytest tests/` now runs 593 tests (up from 592). `mypy shoin/` and `ruff check shoin/config.py` remain clean.

### v0.2.101 (2026-07-08)
**Fixed**: A twenty-ninth background audit round, following directly from v0.2.100's discovery that `build_context()`'s default budget didn't actually enforce its documented sub-share, found the SAME class of gap in `history_messages()`: `HISTORY_MESSAGES(6) * HISTORY_TOKENS_EACH(160) = 960`, not the CLAUDE.md-documented "~400 tokens: recent history" sub-share. The existing per-message `HISTORY_TOKENS_EACH=160` cap only ever bounded *each individual* message — there was no code anywhere enforcing a ceiling on the *sum* across all included messages.

- **Concrete impact**: a history-heavy multi-turn conversation (each retained turn near the 160-token per-message cap) could add up to 960 tokens of history on top of the other three, now-correctly-enforced shares (900 system prompt + 1000 source text + 100 query, per v0.2.100), pushing the real worst-case prompt to ~2960 tokens — well past the documented 2400-token `CONTEXT_TOKENS` ceiling this project explicitly targets for lightweight 4K–8K-context local models.
- Reproduced directly: 3 turn-pairs of substantial length through `history_messages()` summed to 960 tokens total, exactly matching `HISTORY_MESSAGES * HISTORY_TOKENS_EACH` and confirming no total cap existed.
- Fix: added `HISTORY_TOKENS_TOTAL = 400` and restructured `history_messages()` to build the message list *most-recent-first* (prioritizing the newest, most relevant turns), tracking a running token total and stopping once the cumulative budget is exhausted, then reversing back to chronological order before the existing dedup/leading-assistant-trim/trailing-orphan post-processing runs unchanged. The per-message `HISTORY_TOKENS_EACH` cap is retained alongside the new total cap (whichever is tighter applies to each message), so one very long single turn still can't dominate the budget on its own.
- 1 regression test added (`test_history_total_tokens_capped_at_documented_subshare`); verified fail-then-pass via `git stash` as with every fix this session. The existing `test_history_is_bounded` (message-count and per-message-length bounds) continues to pass unchanged — this fix is additive, not a behavior regression of that guarantee.

`pytest tests/` now runs 592 tests (up from 591). `mypy shoin/` and `ruff check shoin/qa.py` remain clean.

### v0.2.100 (2026-07-08)
**Fixed**: A twenty-eighth background audit round, explicitly barred from the now-closed echo-mismatch pattern, found `build_context()`'s default `budget_tokens` was the full documented `CONTEXT_TOKENS` total (2400) instead of its documented "source text" sub-share (~1000, per CLAUDE.md's own "Token-Aware Truncation" breakdown: ~900 system prompt+headers, ~1000 source text, ~400 history, ~100 query). `qa.ask()` and `server._h_ask_sse()` both call `build_context(store, hits)` with no override, so this misleading default let source text alone consume the entire documented total prompt budget before the system prompt, history, and query were even added on top — directly undermining the project's stated purpose of fitting prompts within lightweight 4K–8K-context local models (Qwen3-4B, Phi-4, Gemma-3).

- Reproduced with the exact production call pattern: `TOP_K` (8) sources of ample text through `build_context()`'s old default, then `build_messages()` — the system+source-text+query prompt alone totaled ~2646 tokens, already ~246 tokens over the documented 2400-token *total*, before the ~400-token history allowance was even added.
- Fix: added `SOURCE_TEXT_TOKENS = 1000` as the documented sub-share constant, and changed `build_context()`'s default parameter from `CONTEXT_TOKENS` to `SOURCE_TEXT_TOKENS`. `studio.py`'s two call sites (`STUDIO_BUDGET_TOKENS`, `1600`) already passed their own explicit budgets for their different prompt shapes and are unaffected. No other call sites in the codebase relied on the old default (confirmed by grep).
- 1 regression test added (`test_default_budget_leaves_room_for_full_prompt_within_context_tokens`), using the same real production call pattern rather than a synthetic edge case; verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 591 tests (up from 590). `mypy shoin/` and `ruff check shoin/qa.py` remain clean.

### v0.2.99 (2026-07-08)
**Fixed**: A twenty-seventh background audit round found the CLI `notebook rename` action had the same echo-mismatch bug class fixed three times already for source titles (v0.2.93 `_h_src_upload`, v0.2.94 `_h_src_patch`, v0.2.95 CLI `source rename`) — this time for notebook names. `store.rename_notebook()` does `name = name.strip()` before persisting, but `_cmd_notebook()`'s "rename" action printed `str(args.name)`, the raw unstripped CLI argument, nine lines above the already-fixed `source rename` action in the same file.

- **Concrete impact**: `shoin notebook rename 1 "  Padded Name  "` persists `"Padded Name"` but prints a confirmation claiming the name is `"  Padded Name  "` — a headless/scripting user trusting stdout is told something false about their own data.
- Live-reproduced: CLI output showed the padded name while the DB held the stripped version.
- Fix: apply `.strip()` to the value used in the confirmation message, matching `rename_notebook()`'s own transform.
- 1 regression test added, using an exact string comparison against the same `_t()` template (a naive `.strip()` on the parsed output would have masked this exact bug, since both the buggy padded value and the fixed value strip down to the same substring — caught this while writing the test, before it could ship as a false-negative regression test). Verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 590 tests (up from 589). `mypy shoin/` and `ruff check shoin/cli.py` remain clean.

### v0.2.98 (2026-07-08)
**Fixed**: A twenty-sixth background audit round found `replace_chunks_for_source()` (`store.py`) had a TOCTOU race that could reintroduce the exact bug v0.2.87 fixed — refresh silently overwriting a user's custom source title — via a race instead of unconditionally. `src.title` (used as the Python-side fallback `title or src.title` when `refresh_source()` deliberately passes `title=None` to preserve a custom rename) is read by `get_source()` *before* the transaction begins (SQLite's implicit `BEGIN` only fires at the first DML statement, not at `with self.conn:` entry). A concurrent `PATCH /api/sources/{id}` rename that commits in the window between that read and this method's own `UPDATE` was silently clobbered by the stale pre-transaction snapshot.

- **Concrete impact**: a user renames a URL source to a custom title at nearly the same moment a `↻ Refresh` completes its network fetch and begins committing new chunks; the refresh's stale in-memory title snapshot wins, and the rename is lost with zero error or indication to either request.
- Reproduced directly by injecting the concurrent rename into `get_source()` itself — exactly where the real race window is — and confirmed the pre-fix code loses the rename while correctly-fixed code preserves it.
- Fix: replaced the Python-side `title or src.title` fallback with SQL-side `title=COALESCE(?, title)`, so the fallback resolves against whatever the row's title actually is *at UPDATE-time*, atomically, rather than a stale out-of-transaction Python read.
- 1 regression test added (`test_replace_chunks_title_fallback_does_not_clobber_concurrent_rename`); verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 589 tests (up from 588). `mypy shoin/` and `ruff check shoin/store.py` remain clean.

### v0.2.97 (2026-07-08)
**Fixed**: A twenty-fifth background audit round found `html_to_text()`/`_HTMLText` (`ingest.py`) silently discarded all content after an unclosed `<!--` comment — the same "all-or-nothing where graceful degradation should apply" shape v0.2.96 just fixed for PDF pages, but via a completely different mechanism. Python's stdlib `html.parser.HTMLParser` buffers an unclosed `<!--` comment and, on `close()`, flushes everything from `<!--` through end-of-document as a single comment payload (verified directly against the stdlib); `_HTMLText` has no `handle_comment` override, so that payload — and every real tag/text node inside it — is silently discarded with no error and no `INGEST_EMPTY` signal (text *before* the dangling `<!--` still makes it through, so the empty-content guard never fires).

- **Concrete impact**: a truncated network fetch, a developer's single forgotten `-->`, or a CMS export bug produces an HTML source where ingestion reports success but every paragraph after the unclosed comment silently vanishes from the indexed text — retrieval/citations for those sections simply never exist, with zero signal to the user.
- Reproduced directly: fed a 4-section HTML document with an unclosed `<!--` before section 3; sections 3 and 4 were completely absent from the extracted text pre-fix.
- Fix: before parsing, detect a genuinely unbalanced comment marker (`html.count("<!--") > html.count("-->")`) and neutralize the last unclosed `<!--` by closing it immediately (an empty comment), so the real content that follows parses normally instead of being buffered into oblivion. A well-formed, properly-closed comment is completely unaffected — confirmed with a dedicated regression test that its content is still correctly excluded from extracted text.
- 2 regression tests added (`test_html_unclosed_comment_does_not_swallow_rest_of_document`, `test_html_well_formed_comment_still_ignored`); verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 588 tests (up from 586). `mypy shoin/` and `ruff check shoin/ingest.py` remain clean.

### v0.2.96 (2026-07-08)
**Fixed**: A twenty-fourth background audit round, deliberately pivoted to fresh territory after three rounds closed the title-truncation-echo pattern, found `pdf_to_text()` (`ingest.py`) discarded ALL extracted pages when a single page's `extract_text()` call raised — a real, documented pypdf failure mode (malformed content stream, bad font, corrupt xref entry on one page of an otherwise-fine PDF). The list comprehension `[page.extract_text() or "" for page in reader.pages]` ran inside one shared `try/except`, so one bad page among many good ones aborted extraction of the entire document, contradicting the project's own stated "Graceful Degradation" design principle (CLAUDE.md: "Studio outputs have fallback text... History_messages() survives malformed chats") — already applied the same way to per-batch embedding failures in `pipeline.py`'s `_embed_chunks()`.

- Reproduced directly: mocked a 3-page PDF reader where the middle page's `extract_text()` raises; pre-fix, `pdf_to_text()` raised `IngestError("INGEST_PARSE_FAILED")` and lost all 3 pages' worth of real, extractable content.
- Fix: extract each page independently inside its own `try/except`, skipping (not discarding the whole document for) a page that fails; a page that raises no longer prevents its siblings from contributing text. The outer `try/except` around `PdfReader(BytesIO(data))` construction itself is unchanged — a genuinely unparseable file (not a valid PDF at all) still correctly raises `INGEST_PARSE_FAILED` immediately, confirmed the existing `test_pdf_to_text_parse_error_raises_ingest_error` test still passes unchanged.
- 1 regression test added (`test_pdf_to_text_one_bad_page_does_not_discard_good_pages`); verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 586 tests (up from 585). `mypy shoin/` and `ruff check shoin/ingest.py` remain clean.

### v0.2.95 (2026-07-08)
**Fixed**: A twenty-third background audit round found the same title-truncation-echo-mismatch bug class (v0.2.93 `_h_src_upload`, v0.2.94 `_h_src_patch`) in a third, CLI-side call site: `_cmd_source()`'s "rename" action (`cli.py`) printed `str(args.title)` — the raw, untruncated CLI argument — in its confirmation message, instead of the value `update_source_title()` actually truncated to `MAX_TITLE_LEN` (500 chars) before persisting.

- **Concrete impact**: `shoin source rename <id> "<550-char title>"` prints "改名完了: [1] " followed by the full 550-char string, implying that's the new title, while the DB row only holds the first 500 characters — a headless/SSH user scripting off this output (or just trusting it) is told something false about their own data.
- Live-reproduced: CLI stdout showed a 550-char title while the persisted title was 500 chars.
- Fix: apply the identical `.strip()[:MAX_TITLE_LEN]` transform to the value used in the confirmation message, mirroring the Web API fixes.
- 1 regression test added (`test_source_rename_cli_message_matches_persisted_truncated_title`); verified fail-then-pass via `git stash` as with every fix this session. Confirmed `_cmd_notebook()`'s "new" action is NOT affected — it already correctly uses the returned `nb.name`, not `args.name`.

`pytest tests/` now runs 585 tests (up from 584). `mypy shoin/` and `ruff check shoin/cli.py` remain clean.

### v0.2.94 (2026-07-08)
**Fixed**: A twenty-second background audit round found `_h_src_patch()` (`server.py`, source rename) had the exact same bug class v0.2.93 just fixed in the sibling `_h_src_upload()`: `store.update_source_title()` silently truncates to `MAX_TITLE_LEN` (500 chars) before persisting, but the handler's response echoed the raw, untruncated request-body title. Skipping a second `get_source()` fetch (a deliberate v0.2.45 TOCTOU-avoidance choice, to avoid a delete-race window returning a misleading 404 after a successful update) meant the response could diverge from the DB with no concurrency involved at all — the update itself does the silent transform.

- Live-reproduced against a real running server: `PATCH /api/sources/{id}` with a 550-char title returned HTTP 200 with the full 550-char title in the response, while a follow-up `GET /api/notebooks/{id}` showed the persisted title truncated to 500.
- Fix: apply the identical `title[:MAX_TITLE_LEN]` truncation to the response value — no second DB round trip needed (the TOCTOU-avoidance property v0.2.45 established is preserved), just matching the deterministic transform `update_source_title()` itself already applies.
- 1 regression test added (`test_rename_response_title_matches_persisted_truncated_title`); verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 584 tests (up from 583). `mypy shoin/` and `ruff check shoin/server.py` remain clean.

### v0.2.93 (2026-07-08)
**Fixed**: A twenty-first background audit round found `_h_src_upload()` (`server.py`) returned the raw, untruncated filename in its HTTP response instead of `result.source.title` — the title actually persisted by `add_source()`, which silently truncates to `MAX_TITLE_LEN` (500 chars, `config.py`: `"source titles silently truncated (external content)"`). The sibling URL-ingestion handler `_h_src_add()` already did this correctly (`"title": result.source.title`); only the upload path used the pre-truncation `raw_name`.

- **Concrete impact**: a file uploaded with a filename (from the `X-Filename` header) over 500 characters got a `201` response claiming the full untruncated name, but a subsequent `GET /api/notebooks/{id}` showed the truncated 500-char title actually in the DB — any API consumer trusting the upload response instead of re-fetching (a script, another agent, a future UI change) would be told a title that was never actually retrievable.
- Live-reproduced against a real running server: uploaded a 604-char filename, response title length was 604, but the stored title length was 500 — mismatched.
- Fix: changed the response to use `result.source.title`, matching `_h_src_add()`'s existing correct pattern.
- 1 regression test added (`test_upload_response_title_matches_persisted_truncated_title`); verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 583 tests (up from 582). `mypy shoin/` and `ruff check shoin/server.py` remain clean.

### v0.2.92 (2026-07-08)
**Fixed**: A twentieth background audit round found `_h_note_add()` (`server.py`) still used the exact `str(data.get(...) or "")` type-confusion pattern v0.2.38 fixed for `title` — but only for `title`, one field over: `body = str(data.get("body") or "")`. A `POST /api/notebooks/{id}/notes` with `{"title": "T", "body": [1, 2, 3]}` returned HTTP 201 and silently persisted Python's `str()` coercion of the list (`"[1, 2, 3]"`) as the note body, instead of being rejected the way an equally-malformed `title` field already correctly is.

- Live-reproduced against a real running server: list/dict/bool bodies all returned 201 and were stored as their Python `repr()` strings, not the JSON the client actually sent.
- Fix: added `_optional_str()` — the same type-check `_require()` already does, but without the "must be non-empty" requirement (since `body` is legitimately optional, unlike `title`). `_h_note_add()` now uses it in place of the raw `str(...)` coercion.
- 1 regression test added (`test_add_note_with_non_string_body_returns_400`, live server); verified fail-then-pass via `git stash` as with every fix this session. Grepped for the same `str(data.get(...) or "")` pattern elsewhere in `server.py` — this was the only remaining occurrence.

`pytest tests/` now runs 582 tests (up from 581). `mypy shoin/` and `ruff check shoin/server.py` remain clean.

### v0.2.91 (2026-07-08)
**Fixed**: A nineteenth background audit round, moved to fresh territory now that server.py's disconnect-handling area was closed, found `store.update_source_title()` had no empty/whitespace-title validation at all — it relied entirely on the caller to guard against this. The Web API path (`PATCH /api/sources/{id}`) is protected by `server.py`'s `_require()` (strips and rejects empty/whitespace with `VALIDATION_REQUIRED_FIELD_MISSING`) before ever calling this method, but the CLI path (`shoin source rename`, v0.2.68, explicitly meant to give "the SAME" capability per `cli.py`'s own REQ-103 CLI-parity claim) called `store.update_source_title()` directly with zero validation — `_cmd_source()`'s "rename" branch just passes `str(args.title)` straight through.

- **Concrete impact**: `shoin source rename 5 ""` (or an accidental `shoin source rename 5 "   "` from a shell quoting mistake) silently persisted a blank/invisible source title with exit code 0, while the equivalent `PATCH /api/sources/5` request would have returned HTTP 400. Live-reproduced: both empty and whitespace-only renames via the CLI succeeded and were written to the DB before the fix.
- Fix: moved the guard to the store level — `update_source_title()` now strips and rejects an empty title with `StoreError("VALIDATION_REQUIRED_FIELD_MISSING", ...)`, mirroring the exact pattern `add_note()` already uses for the identical class of gap. This protects every caller (CLI, Web, and any future one) uniformly rather than patching only the CLI path; the Web path's existing `_require()` check is now a redundant-but-harmless first line of defense, not the only one.
- 2 regression tests added: a store-level test (empty/whitespace/tab-newline all rejected, title unchanged) and a CLI-level test confirming `main()` exits 1 with a clean `VALIDATION_REQUIRED_FIELD_MISSING` stderr message instead of silently persisting. Verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 581 tests (up from 579). `mypy shoin/` and `ruff check shoin/store.py` remain clean.

### v0.2.90 (2026-07-08)
**Fixed**: An eighteenth background audit round, specifically hunting for more instances of the v0.2.89 double-fault pattern, found `_reject_cross_site()` (`server.py`) still called raw `self._error()` instead of the newly-added `self._safe_error()` at both of its call sites. This is worse than the v0.2.89 case: `_reject_cross_site()` runs *before* `_dispatch()`'s try/except block even begins (line 252, `if self._reject_cross_site(method): return`), so a dead-connection failure while sending its 403 rejection isn't a double fault — it's a completely **unguarded single fault**, propagating straight out of `_dispatch()`/`do_GET` as a raw unhandled traceback.

- **Concrete impact**: a client that sends a request with a spoofed/rebound `Host` header (or any request hitting the DNS-rebinding/CSRF guard, spec.md STRIDE) and closes its socket before the 403 response arrives — realistic for automated rebinding probes or a hung-up connection — triggers exactly the crash class v0.2.19's catch-all was built to prevent, on a code path that predates entering that catch-all entirely.
- Reproduced directly: mocked `_error()` to raise `BrokenPipeError` and called `_dispatch("GET")` with a disallowed `Host` header — the exception propagated uncaught on the pre-fix code.
- Fix: both `self._error(...)` calls in `_reject_cross_site()` now use `self._safe_error(...)`, matching the four call sites already converted in `_dispatch()`'s own except branches.
- 1 regression test added; verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 579 tests (up from 578). `mypy shoin/` and `ruff check shoin/server.py` remain clean.

### v0.2.89 (2026-07-08)
**Fixed**: A seventeenth background audit round found `_dispatch()`'s (`server.py`) v0.2.19 catch-all exception handler could itself throw and escape uncaught — the exact class of crash that changelog entry claimed to have eliminated. All four `except` branches (`StoreError`/`IngestError`/`LLMError`/generic `Exception`) call `self._error(...)` to write an error response. If the *original* exception was itself a client disconnect (`BrokenPipeError`/`ConnectionResetError` — realistic under `generation_lock`, v0.2.70: a request queued behind an in-progress generation whose client gave up and closed the socket before the response could be written), the fallback `self._error(...)` performs a second write to the same dead socket, raising the same exception class again — this time completely outside any try/except, propagating through `do_GET`/`do_POST` as a genuine unhandled traceback.

- Reproduced deterministically with raw sockets: held `generation_lock` with a slow streaming request on notebook A, queued a `GET /api/notebooks/B/questions` behind it, hard-RST-closed that connection while queued, then let the lock release — the server log showed exactly the predicted double fault (`ConnectionResetError` caught by the catch-all, then `BrokenPipeError` from the fallback write escaping unhandled).
- Fix: added `_safe_error()`, which calls `_error()` inside a `try/except (BrokenPipeError, ConnectionResetError, OSError)` that logs and swallows — there is nothing more to do once the client is confirmed gone. All four `_dispatch()` except branches now call `_safe_error()` instead of `_error()` directly, since any of them (not just the catch-all) could hit the same double-fault if the connection died before the response was written.
- 2 regression tests added: a direct unit test of `_safe_error()` swallowing all three dead-connection exception types, and an end-to-end reproduction of the double-fault through `_dispatch()` itself (handler raises a generic exception, the fallback write also raises `ConnectionResetError`, `_dispatch()` must not propagate). Verified fail-then-pass via `git stash` as with every fix this session — the second test reproduces the exact unhandled-exception traceback shape on the pre-fix code.

`pytest tests/` now runs 578 tests (up from 576). `mypy shoin/` and `ruff check shoin/server.py` remain clean.

### v0.2.88 (2026-07-08)
**Fixed**: A sixteenth background audit round found `renderNotebook()` (`index.html`) silently discarded an in-progress, uncommitted source-rename edit whenever ANY unrelated write elsewhere in the app succeeded — note add/delete, upload, studio generation, clear-chat, source refresh/delete — all call `openNotebook()` on success, which unconditionally tore down and rebuilt `#srcList` from scratch with no awareness that a `.src-rename` input was mid-edit.

- Extracted the inline-rename entry logic into a reusable `startSourceRename(s, tt, row, prefillValue)` function (previously inlined only in the dblclick handler), so `renderNotebook()` can also call it to *restore* an uncommitted edit after a rebuild it didn't cause.
- Two bugs surfaced and fixed during live Playwright verification of the restore itself (not found by static reading alone — CLAUDE.md's own rule to verify UI changes in a real browser before calling them done caught both):
  1. Removing the old (still-focused) input via `list.replaceChildren()` fires a native `blur` event; the old input's own `onblur` handler treated that as "user navigated away" and auto-committed the uncommitted edit via `PATCH` mid-rebuild, racing the restoration and immediately undoing it. Fixed by detaching the old input's `onblur`/`onkeydown` handlers before removal — it's being replaced by a fresh input with fresh handlers regardless.
  2. The restore call originally ran *before* the row was appended to the live DOM, so `input.focus()` inside `startSourceRename()` was a no-op (you cannot focus a detached element). Fixed by moving the restore call to after `list.append(row)`.
- Verified live end-to-end against a real running server: (a) a background re-render while the rename input remains focused now preserves both the typed value and cursor position and keeps focus; (b) genuinely blurring the input (e.g. clicking into an unrelated form field) still correctly auto-commits via the pre-existing `onblur` handler, unchanged; (c) the normal dblclick → type → Enter → commit flow and a full add-note/delete-source smoke pass both complete with zero console/page errors.
- No pytest regression test added — this project's test suite has no Playwright/browser-automation coverage (frontend changes are verified live per CLAUDE.md's own UI-testing rule, not via a persisted automated test, matching how prior UI-only fixes this session were verified and documented).

`pytest tests/` still runs 576 tests (no Python files changed this round). `mypy shoin/` and `ruff check` remain clean (no Python changes).

### v0.2.87 (2026-07-08)
**Fixed**: A fifteenth background audit round — after a systematic sweep confirmed every `except sqlite3.IntegrityError`/`OperationalError` in the codebase now classifies correctly (v0.2.53/86's fixes are complete, and `add_note`/`delete_note` already have the v0.2.39 TOCTOU guard) — found `refresh_source()` (`pipeline.py`) unconditionally passed the freshly re-extracted page `<title>` to `replace_chunks_for_source()`, silently overwriting any custom title the user had set via `PATCH /api/sources/{id}` (the "Source Title Edit" feature, added in the *same* v0.2.35 commit as refresh). `refresh_source()`'s own docstring only promises to update *content* ("Re-fetch a URL source in-place, replacing all chunks... while keeping the source ID"); it says nothing about title, yet title was clobbered on every single refresh regardless.

- **Concrete impact**: a user who curates a meaningful name for a URL source (e.g. "Q3 Pricing Doc" instead of the site's raw `<title>`) loses that label the next time they click the `↻` refresh button — the UI shows a generic "Source refreshed" toast with zero indication the title reverted, and there was no way to opt out.
- Live-reproduced: renamed a source to "My Custom Curated Name," refreshed it against a page reporting a different `<title>`, and the stored title reverted to the raw page title every time.
- Fix: `refresh_source()` no longer passes `title=` to `replace_chunks_for_source()` at all — only `sha256` (content) is updated. `replace_chunks_for_source()`'s own `title or src.title` fallback then correctly leaves whatever title is currently set (custom or original) untouched. Title management remains the exclusive job of `PATCH /api/sources/{id}`, matching the docstring's own scope.
- Updated `test_refresh_source_replaces_chunks_keeps_id` (which asserted the old, now-intentionally-changed behavior) and added `test_refresh_source_preserves_user_renamed_title`; verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 576 tests (up from 575). `mypy shoin/` and `ruff check shoin/pipeline.py` remain clean.

### v0.2.86 (2026-07-08)
**Fixed**: A fourteenth background audit round found `replace_chunks_for_source()` (`store.py`, used by `refresh_source()`) misclassified any non-UNIQUE `sqlite3.IntegrityError` as `SOURCE_NOT_FOUND` — the exact bug class v0.2.53 fixed in its sibling `add_source()` ("classified any non-UNIQUE `IntegrityError` as `NOTEBOOK_NOT_FOUND`... Fix: explicitly check for `'FOREIGN KEY'`... all other `IntegrityError` variants now raise `SYSTEM_INTERNAL_ERROR`"), but that fix was never ported to this method, despite both being touched in the *same* v0.2.53 changelog entry for an unrelated atomicity issue.

- **Concrete impact**: `server.py` maps any `*_NOT_FOUND` code straight to HTTP 404. A genuine constraint violation during `POST /api/sources/{id}/refresh` (NOT NULL, CHECK, or any future constraint that isn't the UNIQUE `(notebook_id, sha256)` or the `chunks.source_id` FOREIGN KEY) would report the source as deleted — HTTP 404 — even though it's fully intact, actively misleading both the UI and any script/CLI-parity caller.
- Live-reproduced with a connection wrapper that raises a NOT NULL-style `IntegrityError` mid-INSERT-loop (not a real deletion): pre-fix reported `SOURCE_NOT_FOUND` while `get_source()` confirmed the source still existed; the transaction correctly rolled back (old chunks intact) even before this fix — only the error *classification* was wrong, not the transaction's atomicity.
- Fix: mirror `add_source()`'s exact three-way classification — `"UNIQUE" in str(e)` → `SOURCE_ALREADY_EXISTS`, `"FOREIGN KEY" in str(e)` → `SOURCE_NOT_FOUND` (the genuine case, since `chunks.source_id REFERENCES sources(id) ON DELETE CASCADE` means a real concurrent deletion legitimately raises this), anything else → `SYSTEM_INTERNAL_ERROR`.
- 1 regression test added (`test_replace_chunks_non_fk_integrity_error_raises_internal_not_not_found`), which also incidentally re-verifies the existing rollback/atomicity guarantee (old chunks untouched) on this exact failure path; verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 575 tests (up from 574). `mypy shoin/` and `ruff check shoin/store.py` remain clean.

### v0.2.85 (2026-07-08)
**Fixed**: A thirteenth background audit round found `chat_stream()` (`llm.py`) had no response-size cap at all, despite `_post()` (the sibling blocking-call method) being fixed for the exact same threat in v0.2.37 ("`_post()` called `resp.read()` with no size limit. A malicious or buggy LLM endpoint returning gigabytes would be read entirely into memory... Fix: cap at 32 MB"). `chat_stream()` iterates `for raw in resp:` line-by-line with zero bound on cumulative bytes across the SSE loop — actually more attacker-favorable than the pre-v0.2.37 `_post()` bug, since a misbehaving or compromised endpoint (`SHOIN_LLM_URL` pointed at an untrusted service) could stream unbounded `data: {...}` lines and OOM the process on the exact 4-8GB RAM systems this project targets per CLAUDE.md's own "Lightweight First" principle.

- Live-reproduced: mocked a streaming response emitting ~44,000 ~1KB SSE lines (~42MB total); `chat_stream()` consumed all of it with no error before the fix.
- Fix: hoisted `_MAX_RESPONSE = 32 * 1024 * 1024` from a local variable inside `_post()` to a module-level constant shared by both methods. `chat_stream()`'s loop now tracks cumulative bytes read and raises `LLMError("SYSTEM_LLM_BAD_RESPONSE", "stream exceeded 32 MB size limit")` once the cap is exceeded — the same exception type and code the loop's existing `{"error": ...}` SSE-payload check already raises, so `server.py`'s existing `_h_ask_sse()` error handling needed no changes to correctly surface this new failure mode.
- 1 regression test added (`test_chat_stream_enforces_32mb_size_cap`); verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 574 tests (up from 573). `mypy shoin/` and `ruff check shoin/llm.py` remain clean.

### v0.2.84 (2026-07-08)
**Fixed**: A twelfth background audit round found that `_embed_chunks()` (`pipeline.py`) could silently defeat its own embedding-model mismatch guard after a partial `reindex_notebook()` failure. The unconditional `if done: store.set_setting("embed_model", current_model)` recorded the new model as fully consistent whenever *any* chunk succeeded — but `reindex_notebook()` calls `_embed_chunks(..., force=True)`, which OVERWRITES existing vectors in place. If the embedding endpoint drops mid-run (network failure, restart, timeout — the same class of transient failure `_embed_chunks()`'s own `except LLMError: pass` comment says is expected), the chunks a later batch never reached still hold their OLD, untouched, different-model vectors — non-NULL, and therefore still included in `vector_search()`'s cosine comparisons. Recording `embed_model` as the new model in that state made `_check_embed_model_ok()` (`qa.py`) report *no* mismatch over a DB that was provably still mixed — exactly the corruption its own docstring says it exists to prevent ("Mixing embeddings from two models makes cosine scores meaningless").

- Live-reproduced: seeded 40 chunks with model-A vectors, ran `reindex_notebook()` with a fake LLM that succeeds on batch 1 (16 chunks) then raises `LLMError` on batch 2 — `embed_model` setting was written as model-B, `_check_embed_model_ok()` returned `True` (no mismatch), while 24/40 chunks still held 5-dim model-A vectors alongside 16 3-dim model-B ones.
- Fix: only record `embed_model` on the `force=True` path when *every* chunk in the call succeeded (`done == len(texts)`); on partial failure the setting is left at the old model, so the guard correctly reports a mismatch and disables vector search until a full reindex succeeds. The non-force (`index_source`) path is unaffected and deliberately unchanged — an un-embedded chunk there is simply `NULL` (safely excluded by `vector_search()`'s `WHERE embedding IS NOT NULL`), not a stale wrong-model vector, so partial success correctly still updates the setting exactly as before.
- 1 regression test added (`test_reindex_partial_failure_does_not_falsely_clear_mismatch_guard`), asserting the setting stays at the old model and the mismatch guard correctly fires; verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 573 tests (up from 572). `mypy shoin/` and `ruff check shoin/pipeline.py` remain clean.

### v0.2.83 (2026-07-08)
**Fixed**: An eleventh background audit round found `_h_src_patch()` (source rename, `server.py`) never invalidates `questions_cache`, unlike its sibling `_h_src_refresh()` (v0.2.36), which does: `with self.questions_cache_lock: self.questions_cache.pop(nb_id, None)`. The cache fingerprint is `tuple(s.id for s in store.sources_for_notebook(nb_id))` — source IDs only — and a rename doesn't change those, so the cache never self-expires. `build_context()` (`qa.py`) embeds each source's title directly into the LLM prompt (`f"[S{idx}] {title}\n<<<SOURCE S{idx}\n..."`), so a rename changes exactly what a refresh changes, but only refresh got the eviction fix.

- **Concrete impact**: a user generates question suggestions, renames a source to fix a typo or clarify its content, reopens the suggestions panel — gets stale suggestions generated from the old title, and the LLM is never re-invoked, indefinitely (the cache only expires when sources are added/deleted/refreshed, not renamed). Live-reproduced against a real running server: warmed the cache (1 LLM call), renamed a source via `PATCH /api/sources/{id}`, requested suggestions again — chat call count stayed at 1 instead of incrementing to 2.
- Fix: `_h_src_patch()` now evicts `questions_cache[src.notebook_id]` after a successful rename, using the exact same pattern as `_h_src_refresh()`.
- 1 regression test added (`SourceRenameCacheTest`, live server + `FakeLLM` call-count assertion, mirroring the existing `ClearChatCacheTest` pattern); verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 572 tests (up from 571). `mypy shoin/` remains clean; the 5 `ruff check` findings in `test_server.py` are unchanged pre-existing style issues, none touching the new test class (confirmed by line-range comparison).

### v0.2.82 (2026-07-08)
**Fixed**: A tenth background audit round, specifically hunting for more instances of the v0.2.81 "sibling functions, inconsistent guard" pattern, found `studio.py`'s `generate()` and `suggest_questions()` both lacked the `sqlite3.OperationalError` guard around `build_context()` that `qa.ask()` has had since v0.2.44. All three functions call the exact same `build_context()` for the exact same reason (a `get_source()` inside it can hit SQLite's `busy_timeout` under WAL lock contention), but only `ask()` had ever been given the fix.

- **Concrete impact**: under lock contention, `POST /api/notebooks/{id}/studio` and `GET /api/notebooks/{id}/questions` propagated a bare `sqlite3.OperationalError` into `server.py`'s catch-all (`_dispatch()`), returning HTTP 500 with `code="SYSTEM_INTERNAL_ERROR"` and a message of just `type(exc).__name__` — literally the string `"OperationalError"`, since that branch passes `type(exc).__name__` rather than `str(exc)`, dropping the actual "database is locked" diagnostic text entirely. `ask()`'s equivalent failure returns a clean HTTP 400 `SYSTEM_DB_LOCKED` with the real message.
- Fix: both functions now use the identical `try/except sqlite3.OperationalError → raise StoreError("SYSTEM_DB_LOCKED", ...)` pattern as `ask()`. For `suggest_questions()` specifically: a DB lock is a different failure class from the `LLMError` this function already swallows into `[]` (that catch is for "LLM unreachable," a deliberate best-effort degradation per its own docstring) — a DB lock now raises rather than silently returning an empty suggestion list indistinguishable from "notebook has no sources."
- 2 regression tests added (`generate()` and `suggest_questions()`, mirroring the existing `ask()` test); verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 571 tests (up from 569). `mypy shoin/` and `ruff check shoin/studio.py` remain clean.

### v0.2.81 (2026-07-08)
**Fixed**: A ninth background audit round, deliberately moved to fresh territory now that the citation-question-detection area was structurally closed in v0.2.80, found that `refresh_source()` (`pipeline.py`) completely bypassed the `MAX_CHUNKS_PER_NOTEBOOK` DoS cap (v0.2.70's spec.md STRIDE control). `index_source()` checks `existing_chunks + len(texts) > MAX_CHUNKS_PER_NOTEBOOK` before committing a new source; `refresh_source()` — which also inserts new chunks, via `store.replace_chunks_for_source()` — never referenced `MAX_CHUNKS_PER_NOTEBOOK` at all. Confirmed via grep: the constant appears in `pipeline.py` only inside `index_source()`.

- **Concrete impact**: a URL source that grows over time (a paginated archive, a feed, an attacker-controlled endpoint) can be refreshed repeatedly via `shoin source refresh` or `POST /api/sources/{id}/refresh` with no ceiling on how many chunks each refresh adds, fully defeating the documented per-notebook cap for any source reachable via refresh — not just a theoretical gap, reproduced live with the cap monkeypatched to 5: `replace_chunks_for_source()` happily inserted 20 chunks into an already-over-cap notebook.
- Fix: `refresh_source()` now computes `notebook_chunks - this_source_chunks + len(texts)` before replacing — subtracting the source's own current chunk count first, since a refresh *replaces* that source's chunks rather than adding a new source. Without the subtraction, a same-size (or shrinking) refresh of a source already counted in the notebook total would be wrongly rejected even though it doesn't grow the notebook past the cap at all; verified this directly (a refresh at exactly the cap, replacing 4 chunks with 1, succeeds without raising).
- 2 regression tests added: the over-cap growing-refresh case correctly raises `INGEST_NOTEBOOK_FULL`, and the same-size-at-cap case correctly does not. Verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 569 tests (up from 567). `mypy shoin/` and `ruff check shoin/pipeline.py` remain clean.

### v0.2.80 (2026-07-08)
**Fixed**: An eighth background audit round, explicitly briefed to do one final adversarial pass on the question-detection heuristic before considering it closed (given v0.2.77/78/79 were three successive partial fixes to the same spot), found a fourth gap — and confirmed the deeper root cause: the heuristic existed as **two independently-maintained copies** in two files that kept drifting apart, not one heuristic with isolated bugs. `studio.py`'s `suggest_questions()` has always had a first-word English-question-starter check ("LLMs asked for 'no decoration' often omit trailing '?' in list form" — its own comment) that `uncited_sentences()` never had at all, despite v0.2.77-79's comments each claiming the two "agree."

- Live-reproduced: `uncited_sentences("What is the main benefit of the new policy.")` and `"How does this system work."` (no trailing `?`, exactly the LLM behavior `suggest_questions()`'s own comment documents as common) were both flagged as unsupported claims.
- Rather than a fifth patch adding one more case to `citation.py`'s copy, extracted `looks_like_question()` as the single shared implementation in `citation.py` (question-mark substring after NFKC normalization, JA suffix set, EN starter-word set — the union of everything both copies previously checked separately). `uncited_sentences()` and `studio.py`'s `suggest_questions()` now both call this one function; the two can no longer independently drift, since there is only one implementation left to drift.
- Verified byte-identical behavior preserved for `suggest_questions()`'s existing test coverage (NFKC normalization order and the `"?" in q` substring check, not `endswith`, are both preserved exactly) — no regression in the un-changed original heuristic's own scope, only the addition of what `uncited_sentences()` was missing.
- 1 more regression test added (3 EN question-starter forms without trailing punctuation, plus a control case confirming a real unsupported claim is still flagged); verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 567 tests (up from 566). `mypy shoin/` and `ruff check shoin/citation.py shoin/studio.py` remain clean.

### v0.2.79 (2026-07-08)
**Fixed**: A seventh background audit round, specifically briefed to re-scrutinize the v0.2.78 fix itself rather than broaden scope (given v0.2.77's first attempt at this same fix had itself been incomplete), found the v0.2.78 fix was *also* incomplete in a small but concrete way: its own comment claimed to "reuse the same suffix set studio.py's `suggest_questions()` already established... so the two question-detection heuristics in this codebase agree" — but the ported tuple was `("か", "でしょう")`, silently dropping `"ください"` (polite request form, e.g. "…について教えてください。") from `suggest_questions()`'s actual three-item tuple `("か", "ください", "でしょう")`. The claim of agreement was false as written.

- Live-reproduced: `uncited_sentences("この技術の利点について教えてください。")` returned the sentence as a false-positive unsupported claim, despite it being a polite-form question asserting nothing — the same false-positive class v0.2.77/78 both set out to eliminate, just for this one remaining suffix.
- Fix: added `"ください"` back to the tuple, restoring actual parity with `studio.py`'s heuristic.
- 1 more regression test added; verified fail-then-pass via `git stash` as with every fix this session.

`pytest tests/` now runs 566 tests (up from 565). `mypy shoin/` and `ruff check shoin/citation.py` remain clean.

### v0.2.78 (2026-07-08)
**Fixed**: A sixth background audit round, explicitly briefed to re-examine the v0.2.77 fix itself, found that fix was incomplete: `uncited_sentences()`'s question-exclusion guard only recognized `?`/`？`-suffixed questions, not the extremely common formal-Japanese question construction ending in `か。` with no question mark at all (e.g. `この技術の利点は何か。`) — the natural register for `study_guide`'s own JA prompt ("理解確認の設問"). This re-triggered the exact systematic false-positive v0.2.77 set out to eliminate, just for the JA-formal-register half of it. Notably, the sibling function in the same codebase, `suggest_questions()` (`studio.py`), already recognized this exact pattern (`q_base.endswith(("か", "ください", "でしょう"))`) — the v0.2.77 fix had reused a narrower heuristic than the one already established elsewhere.

- Reproduced directly: a correctly-cited JA study-guide-style Q&A (`この技術の利点は何か。` / `導入にはどれくらいの期間が必要か。`, both answers properly citing `[S1]`/`[S2]`) still returned both questions in `uncited`, despite `confirmed` correctly showing both grounded.
- Fix: `uncited_sentences()` now strips trailing `。.!?？` and also checks for a `か`/`でしょう` suffix, matching `suggest_questions()`'s existing heuristic so the two question-detection mechanisms in this codebase agree instead of diverging.
- 3 more regression tests added (bare か。/でしょうか。 forms, plus an end-to-end `make_report()` reproduction); verified fail-then-pass via `git stash` as with every fix this session. The v0.2.77 English/？-suffixed tests continue to pass unchanged.

`pytest tests/` now runs 565 tests (up from 563). `mypy shoin/` and `ruff check shoin/citation.py` remain clean.

### v0.2.77 (2026-07-08)
**Fixed**: A fifth background audit round found that `uncited_sentences()` (`citation.py`, v0.2.65) systematically false-positived on the `faq` and `study_guide` Studio kinds — the exact two kinds whose own prompts ask the LLM for 5-8 questions per output (`studio.py`'s `faq`/`study_guide` prompt templates). Every question line in a generated FAQ or study guide has no `[S#]` marker of its own (a question asserts nothing, so there is nothing to cite), but `uncited_sentences()` had no way to tell "a claim with no citation" (the real gap it exists to catch) apart from "a question with no citation" (structurally never a claim) — it flagged both identically.

- Reproduced directly: `make_report()` on a correctly-cited 2-question FAQ (each answer properly citing its source, `confirmed: [1, 2]`) still returned `uncited: ['Q1: ...?', 'Q2: ...?']` — a systematic false positive on every single generation of these two kinds, not an edge case, undermining this module's own explicitly documented design principle (`citation.py`'s module docstring: the checks "stay silent otherwise rather than falsely accusing a correctly paraphrased answer").
- Fix: `uncited_sentences()` now skips any fragment ending in `?`/`？` — a question is never itself an assertion requiring a citation. The existing `pending`-fragment-flush logic is unaffected: a genuine uncited claim that happens to precede a question in the same text is still correctly flagged (verified with a dedicated regression test), so the fix narrowly targets question fragments themselves, not sentences near them.
- 3 regression tests added: bare question sentences (English + Japanese) are not flagged; a real unsupported claim following a question is still flagged; and an end-to-end `make_report()` reproduction of the exact FAQ false-positive scenario now correctly omits the `uncited` key. Verified the new tests fail against the pre-fix code via `git stash` before restoring the fix.

`pytest tests/` now runs 563 tests (up from 560). `mypy shoin/` and `ruff check shoin/citation.py` remain clean.

### v0.2.76 (2026-07-08)
**Fixed**: A fourth background audit round found a genuine, subtle bug in `history_messages()` (`qa.py`): it conflated "no assistant reply row exists at all" (a true orphan, e.g. server crash before persisting anything) with "an assistant reply row exists but has an empty body" (a legitimate, already-persisted degraded/error/zero-token-response turn — server.py has always persisted *something* for every completed turn since v0.2.55, specifically so the DB never has a truly dangling question). Both cases produced an empty `body` after filtering and were indistinguishable by the time the trailing-user-turn-strip loop ran, so a real, answered (if emptily) user question got silently stripped from history as if it were an orphan.

- **Concrete failing chain**: Q1→A1 (normal exchange), Q2→A2 where A2 is persisted with an empty body (zero-token LLM response from a reasoning model — the exact class of lightweight local model this project targets per CLAUDE.md's "Lightweight First" principle — or any of the SSE disconnect/`build_context`-exception paths that persist an empty assistant row), then Q3 is a short follow-up (`< 30 chars`, the threshold `expand_query()` uses). Before the fix: A2's empty body caused Q2 to look like a trailing orphan, `history_messages()` stripped it, and `expand_query()`'s "prepend the last user question" logic silently anchored Q3's retrieval to **Q1** instead of Q2 — the wrong topic, with no error signal anywhere.
- Fix: compute `has_trailing_answer` from the **raw** last DB row's role (before any empty-body filtering), not from the post-filter list. Only strip the trailing user turn when the raw last row is genuinely a `user` row with zero reply rows at all; an assistant row — even an empty one — means the preceding user turn was legitimately handled and must survive into history.
- Verified with a regression test (`test_empty_assistant_reply_is_not_treated_as_orphan`) that reproduces the exact 4-message chain above; confirmed it fails against the pre-fix code via `git stash` before restoring the fix (asserted `'問2' not found` pre-fix, passes post-fix). The existing `test_orphaned_user_message_trimmed_from_history` (true-orphan case, no trailing assistant row at all) continues to pass unchanged — confirming the fix is additive, not a regression of the original v0.1.52/v0.2.55 protection.

**Fixed (docs)**: The same round re-checked CLAUDE.md's route list (the exact section that had the v0.2.75 bug) in the other direction — code-has-it-but-doc-doesn't and doc-claims-it-but-code-doesn't — and found `GET/POST /api/health` claimed a `POST` route that was never registered (`server.py`'s `_ROUTES` has only `("GET", r"^/api/health$", "health")`). Corrected to `GET` only.

`pytest tests/` now runs 560 tests (up from 559). `mypy shoin/` remains clean.

### v0.2.75 (2026-07-08)
**Fixed (docs)**: A third background audit round found that this file's own "Key Files & Sections" → `server.py` route list (the paragraph directly above "Version History") documented `GET /api/notebooks/{id}/messages → chat history` as a real endpoint — it never existed. The only route matching that path is `DELETE /api/notebooks/{id}/messages` (clear chat history, `server.py` line 211); chat history is actually delivered embedded as the `"messages"` array inside the combined `GET /api/notebooks/{id}` payload (`_notebook_json()`), and `index.html` confirms the frontend never calls the documented path for a GET. Anyone — a developer, an external script, or another agent — taking this doc at face value and issuing `GET /api/notebooks/1/messages` would get HTTP 405 `METHOD_NOT_ALLOWED` (the DELETE route's pattern matches the path, so `_dispatch()`'s wrong-verb branch fires) instead of the chat history they expected, with no hint from the response that the path itself was never real. Corrected the route list to describe actual behavior; no code changed, since the combined-payload design is the correct one (matches v0.2.74's finding that the CLI's own `messages list` reads the same `store.list_messages()` rather than a dedicated endpoint).

### v0.2.74 (2026-07-08)

### v0.2.74 (2026-07-08)
**Feature**: `shoin messages list <notebook_id>` — a second background audit round (explicitly briefed to avoid re-checking anything already fixed, including v0.2.73) found that `cli.py`'s own module docstring claims "the CLI exposes every core capability so the product is fully usable headless (REQ-103)," a claim v0.2.68 acted on directly for notes and sources — but the `messages` subcommand only ever had a `clear` action, never a `list`. A headless (SSH-only, no browser) user could destroy a notebook's chat history but never read it back; the only workaround was `shoin export --format md`, which dumps the entire notebook (sources, notes, studio outputs, chat) rather than just the conversation log, and isn't documented anywhere as the intended substitute.

- `cli.py`: new `messages list` subparser + `_cmd_messages()` branch, a thin wrapper around the existing `store.list_messages()` (already used by `server._notebook_json()` and `export.export_markdown()`). Prints `[id] role: body` per message, or a `msg.empty` hint (new i18n key, ja/en) for a notebook with no messages yet — matching the `note.empty`/`note list` pattern from v0.2.68 exactly, including that neither validates notebook existence first (`list_notes()` and `list_messages()` both silently return `[]` for a nonexistent notebook ID; only the destructive `clear`/`delete` actions call `get_notebook()` to raise `NOTEBOOK_NOT_FOUND` — pre-existing, intentional asymmetry, not touched here).
- 3 regression tests (`TestCLIMessagesList`): list shows role+body for both a user and assistant turn (including a `[S1]` citation marker surviving verbatim), the empty-notebook hint, and a list→clear→list roundtrip confirming the hint reappears after clearing.

`pytest tests/` now runs 559 tests (up from 556). `mypy shoin/` remains clean.

### v0.2.73 (2026-07-08)
**Fixed**: A background research agent doing a fresh "過不足" gap audit (explicitly briefed to avoid re-litigating anything already in this changelog) found a real, concrete bug in `retrieve()` (`search.py`) that survived 72 prior versions and the v0.2.72 code-review pass: the negative-term filter (`-word` syntax, v0.2.47) could silently starve retrieval results below `k` — down to zero — instead of backfilling from valid lower-ranked candidates.

- **Root cause**: `bm25_search()` already excludes negated-term hits internally (filters before returning), but `vector_search()` has no query text and does no such filtering. `retrieve()` fused the (already-filtered) BM25 hits with the (unfiltered) vector hits, ran `mmr(rerank(clean, fused), k)` to select the final `k` results — spending MMR's entire k-selection budget against a pool that still contained negated-term hits — and only *afterward* called `_apply_neg_filter()` on the already-selected top-`k` list. If the negated-term chunks happened to rank highest by vector similarity, MMR picked exactly those `k` slots, the post-hoc filter then removed all of them, and `retrieve()` returned fewer results than `k` (or an empty list) even though clean, relevant chunks existed one rank lower in the same pool and were never given a chance to be selected.
- Reproduced concretely with a regression test before fixing: 6 chunks with a shared text prefix (so MMR's own diversity mechanism doesn't accidentally paper over the bug by disfavoring near-duplicate "legacy" chunks for unrelated reasons) where the 3 negated-term chunks have the highest cosine similarity to the query vector — `retrieve(..., "書院 -legacy", ..., k=3)` returned `[]` pre-fix, confirmed by temporarily reverting just the `search.py` change (`git stash`) and re-running the new test in isolation.
- Fix: move the neg-term filter to apply to `vec_hits` *before* fusion/MMR (matching where `bm25_search()` already does its own filtering), so MMR only ever selects from an already-eligible pool. The redundant post-selection filter call is removed — filtering the inputs makes filtering the output unnecessary. `test_retrieve_neg_term_excludes_vec_hits` (v0.2.47) continues to pass unchanged; added `test_retrieve_neg_term_backfills_vec_hits_instead_of_starving` for the starvation case specifically.

`pytest tests/` now runs 556 tests (up from 555). `mypy shoin/` remains clean.

### v0.2.72 (2026-07-07)
**Fixed**: Ran `/code-review --effort high HEAD~1` against the v0.2.71 commit itself (8 finder angles, 1-vote recall-biased verify) — the first time this session invoked the code-review skill instead of doing purely manual audit passes, per the session's own personalized model-usage self-critique. It found that the v0.2.71 "commercial-grade quality" pass had introduced its own small gaps, ironic given the pass's subject matter.

- `shoin/static/index.html`: the inline source-rename `commit()` handler (added v0.2.35, touched again in v0.2.71 for the busy-state pass) set `input.disabled = true` before its `PATCH` call but, unlike every sibling handler touched in the same v0.2.71 diff (`#nbForm`, `#noteForm`, `#fileInput`, `#urlBtn`, notebook rename/delete), had no `finally` to reset it. `commit()`'s own catch block calls `openNotebook(nb.id)`, which has its own internal try/catch and silently no-ops (toast only) if that follow-up fetch also fails — so two consecutive failures (rename fails, then the recovery re-fetch also fails) left the rename `<input>` permanently disabled with no code path to re-enable it. Fixed by adding the missing `finally { input.disabled = false; }`.
- `#clearChat` and the note-delete `×` button were never given the busy-state guard at all, despite the v0.2.71 changelog's own claim that the pattern was "applied ... uniformly" — a double-click on either fired two concurrent DELETE requests, the exact class of bug that pass was fixing everywhere else. Both now follow the same `disabled = true` / `finally { disabled = false }` pattern as their siblings.
- `#langBtn` lost its static `aria-label="Switch language"` HTML fallback entirely in v0.2.71 (replaced by a JS-only `applyI18n()` call), unlike the other 6 elements converted to `data-i18n-aria` in the same diff, which all kept their static fallback attribute alongside the new i18n hook. If any earlier synchronous script code throws before `applyI18n()` runs, `#langBtn` — uniquely among the 7 — would have no `aria-label` at all. Restored the static fallback attribute.
- `shoin/store.py`: `_retry_on_lock(fn, attempts=0)` skipped its loop body entirely, leaving `last_exc` as `None`; the trailing `assert last_exc is not None` then fired (or, under `python -O` which strips asserts, execution fell through to `raise last_exc` with `last_exc` still `None`, raising a bare `TypeError` that masked the real SQLite error). Dormant today — both call sites (`journal_mode = WAL`, `migrate()`) use the default `attempts=5` — but `_retry_on_lock` is written as a general-purpose reusable helper with no guard against a future `attempts=0` caller. Fixed: `if attempts < 1: return fn()` at entry, so a zero-attempts call degrades to a direct, unretried call instead of crashing on its own bookkeeping.
- Added `TestRetryOnLock` (4 deterministic tests) — `_retry_on_lock`'s only prior coverage was the flaky `test_migrate_concurrent_shared_db_file_no_crash` test (v0.2.40), which exercises retry timing only by chance (4 racing threads, ~0% failure post-v0.2.71 but non-deterministic by nature). The new tests use a mock function with a call counter and `patch("time.sleep")` to deterministically cover: eventual success after transient "locked" errors, exhausting all attempts re-raises the original exception, a non-"locked" `OperationalError` is never retried, and the just-fixed `attempts=0` path.

`pytest tests/` now runs 555 tests (up from 551). `mypy shoin/` remains clean. `ruff check` findings are unchanged (all 18 pre-existing, none touch the new/changed lines — confirmed by line-range grep before and after this round).

**Not fixed (noted, not actioned)**: the same review surfaced three lower-severity/more-speculative candidates this round did not act on, consistent with the project's "confirmed bugs with concrete failing paths, not speculative changes" discipline: (1) `_retry_on_lock`'s fixed non-jittered backoff (50/100/150/200/250ms) could in theory synchronize retries across many racing threads (thundering-herd risk) — no evidence this has actually happened in the 120-run stress test from v0.2.71; (2) the busy-state disable/restore pattern is now hand-copied at 10+ call sites in `index.html` rather than factored into one helper — a real simplification opportunity but not a bug; (3) `_retry_on_lock`'s final failed attempt still sleeps before giving up, adding a fixed ~0.75s to every `Store()` construction failure during a genuine (non-transient) lock/corruption outage — a deliberate tradeoff of the retry design, not an oversight.

### v0.2.71 (2026-07-01)
**Fixed**: A "make this commercial-grade quality" audit (2 parallel Explore agents surveying frontend + backend) found and fixed a real, empirically-reproduced concurrency bug in `store.py`'s startup path — not just a flaky test, an actual production reliability gap.

- **Root cause**: `Store.__init__()` set `PRAGMA busy_timeout = 5000` *after* `PRAGMA journal_mode = WAL`. Switching a brand-new file to WAL mode briefly needs exclusive access to create the `-wal`/`-shm` files; when several threads raced to do this on the same fresh file simultaneously, whichever PRAGMA ran first — before busy_timeout had been configured — raised `OperationalError: database is locked` immediately instead of waiting. This was empirically reproduced at a ~13% failure rate over 40 runs of `test_migrate_concurrent_shared_db_file_no_crash`, previously worked around session-to-session via an ad-hoc `pytest --deselect` CLI flag with zero record of *why* anywhere in the repo.
- Fix: reordered `busy_timeout` to be the *first* PRAGMA set, before `foreign_keys` and `journal_mode`. This alone roughly halved the failure rate (~13% → ~5%), confirming the diagnosis but not fully closing the window.
- Added `store._retry_on_lock()`: a small shared retry-with-backoff helper (5 attempts, 50ms/attempt backoff) wrapping both the `journal_mode = WAL` PRAGMA and `Store.migrate()`. Safe to retry in both cases — `PRAGMA journal_mode` is idempotent, and `migrate()` re-reads the applied-version state from the DB before doing any work, so a retry after a partial failure just skips what a previous attempt already committed (the existing `IF NOT EXISTS`/`INSERT OR IGNORE` idempotency from v0.2.33 made this safe with no further changes).
- Verified with 120 consecutive runs of the specific concurrency test post-fix: **0 failures**. `pytest tests/` (full suite, no `--deselect` needed for the first time this session) now runs clean: 551 passed.

**Feature**: Frontend accessibility and UX-consistency fixes found by the same audit.

- 11 hardcoded English `aria-label` values in `index.html` bypassed the existing `data-i18n`/`data-i18n-title` i18n convention (`langBtn`, `paneSrc`, `fileInput`, `urlInput`, `paneChat`, `#qs`, `paneStudio`, source-delete/view/citation-chip/note-delete dynamic labels) — Japanese-locale screen-reader users got English labels. Added a `data-i18n-aria` attribute pattern (same mechanism as `data-i18n-title`, v0.2.66) for the static elements, and routed the dynamically-generated ones through `t()` directly.
- Inconsistent busy/disabled state during in-flight requests: `#nbForm`, `#noteForm`, `#urlBtn`, `#fileInput`, notebook rename/delete buttons, and the inline source-rename `commit()` had no disabled state while their request was in flight (risk of duplicate submissions on a double-click/double-Enter), while Studio-generate/reindex/source-refresh buttons already correctly did. Applied the same disable-during-request pattern uniformly.

**Fixed (packaging)**: `pyproject.toml` had zero PyPI `classifiers` — no Python version, license, OS, or audience metadata, hurting PyPI discoverability. Added a standard classifier set (Development Status, Python 3.11/3.12, MIT License, OS Independent, audience/topic tags, Japanese/English natural language).

**Known gap, not fixed (needs repo-admin action)**: The same audit confirmed `.github/workflows/` does not exist — **zero CI actually runs on this repo**. `ci/ci.yml` (lint → mypy --strict → test → coverage → secret-scan → SBOM) is fully written but parked in `ci/` per `ci/README.md`'s own explanation: a GitHub App `workflows` permission restriction prevents automated agents (including this session) from writing to `.github/workflows/`. Activating it requires a human with repo-admin access to run `git mv ci/ci.yml .github/workflows/ci.yml`. Every commit in this project's history — including all of this session's — has landed without automated verification.

**Fixed**: `pyproject.toml` version was `0.2.70`, aligned with `config.py` `VERSION = "0.2.71"`.

### v0.2.70 (2026-07-01)
**Feature**: LLM generation serialization + per-notebook chunk cap — a fifth Socratic "過不足" audit pass, this time cross-examining `docs/spec.md`'s own STRIDE DoS-control table against the codebase. spec.md has documented "DoS対策: アップロード10MB上限、同時生成1、チャンク数上限/notebook" (upload cap, single concurrent generation, per-notebook chunk cap) — the upload cap was real (`MAX_UPLOAD_BYTES`), but the other two were never implemented. `server.py` let `ThreadingHTTPServer` run unlimited concurrent LLM generations, and `pipeline.index_source()` had no ceiling on chunks accumulated per notebook.

- `server.py`: new `generation_lock: threading.Lock` class attribute (same injection pattern as the existing `questions_cache_lock`), instantiated once in `make_server()`. Wraps only the actual LLM-generation call in `_h_ask_sse()` (the `_stream_chat()` loop), `_h_studio()` (`generate()`), and `_h_questions()` (`suggest_questions()`, cache-miss path only) — retrieval, context-building, and SSE header/meta-event sending are NOT serialized, only token generation itself. A concurrent second request blocks until the first's generation completes, which is the correct behavior for a single-user local app on a lightweight LLM endpoint (no retry/429 needed).
- `config.MAX_CHUNKS_PER_NOTEBOOK = 50_000`: generous per-notebook ceiling. `pipeline.index_source()` checks `existing + new > limit` (via `store.counts()`) before `add_source()` commits, raising `IngestError("INGEST_NOTEBOOK_FULL")` so an over-limit ingest never leaves an orphaned source row.
- Regression test for the lock uses a `_OverlapDetectingLLM` fake that records a violation if `chat_stream()` is entered while already active (with a `sleep(0.15)` window to make races deterministic); verified by temporarily reverting the lock and confirming the test fails with `violations=2` before restoring the fix.
- Two pre-existing tests in `tests/test_core.py` construct `_Handler` instances manually via `_Handler.__new__()` (bypassing `make_server()`'s attribute injection) — both needed `handler.generation_lock = threading.Lock()` added alongside the existing `questions_cache_lock` line.
- 2 chunk-cap regression tests (`TestChunkLimit`).

**Fixed (docs)**: The same audit found `docs/spec.md` had drifted from the implementation in three more places — the search pipeline diagram still showed the pre-v0.2.56 Convex Combination fusion instead of RRF; REQ-006/引用検証仕様 documented only the range-check + coverage design instead of the four-stage verification (confirmed/misattributed/uncited, completed v0.2.65); and the migration description claimed "up/down両方向" when the actual implementation is intentionally up-only/append-only (down migrations are unsafe in SQLite). All three corrected to match the current, tested implementation. Also fixed the 非機能要件 logging line, which claimed JSON-structured logging with trace_id — the actual design (documented correctly in this file's own "No Distributed Tracing" section) is intentionally minimal stderr output; spec.md now matches. `SECURITY.md`'s Supported Versions table listed only `0.1.x`; added `0.2.x`.

`pytest tests/` now runs 550 tests. `mypy shoin/` and `ruff check shoin/` remain clean.

**Fixed**: `pyproject.toml` version was `0.2.69`, aligned with `config.py` `VERSION = "0.2.70"`.

### v0.2.69 (2026-07-01)
**Feature**: `~/.config/shoin/config.json` support — a fourth Socratic "過不足" audit pass, this time cross-examining README.md's own promises against the actual codebase rather than CLI/Web parity. README.md has documented *"環境変数または `~/.config/shoin/config.json`"* (environment variables OR config.json) as the two configuration paths since v0.1.0 — but `grep -r "config.json" shoin/` returned zero matches. Every `config.py` accessor was `os.environ.get(...)`-only; the JSON config file was pure vaporware documented for 68 versions with no code ever reading it.

- `config.config_file() -> Path`: `~/.config/shoin/config.json`, matching README's literal documented location.
- `config._file_config() -> dict[str, str]`: best-effort JSON load; missing file, unreadable file, malformed JSON, or a non-object top level (e.g. a bare list) are all silently ignored — config.json is optional, never a hard dependency.
- `config._get(key, default) -> str`: environment variable, then config.json, then the built-in default. Deliberately not `functools.lru_cache`d — `ui_lang()` is called on nearly every request-handling path via `_t()` (cli.py/export.py/qa.py/studio.py), and caching a tiny JSON read across process lifetime risks stale-value bugs (e.g. in tests that patch env/file state) for a negligible I/O saving.
- `data_dir()`, `llm_url()`, `llm_model()`, `embed_model()`, `ui_lang()`, `port()` all now route through `_get()`, so every documented setting genuinely supports the config-file fallback README always claimed.
- README.md also fixed: "三段の引用検証" (three-stage citation verification) was stale relative to `uncited_sentences()` (v0.2.65, this session's own fourth check) — now "四段" with the uncited-assertion check listed. Added a `config.json` example block to the Configuration section.
- 6 regression tests (`TestConfigXDG`): config.json fallback when env unset, env-var precedence over config.json, missing-file fallback, malformed-JSON fallback, non-dict-top-level fallback. Verified live end-to-end (`HOME=<tmp> config.json → llm_model()` returns the file's value).

`pytest tests/` now runs 547 tests. `mypy shoin/` and `ruff check shoin/` remain clean.

**Fixed**: `pyproject.toml` version was `0.2.68`, aligned with `config.py` `VERSION = "0.2.69"`.

### v0.2.68 (2026-07-01)
**Feature**: CLI note/source management — a third Socratic "過不足" audit pass, this time asking whether Web UI capabilities are reachable from the CLI (the reverse of v0.2.67, which closed the Web-missing-CLI-feature gap). `cli.py`'s own module docstring claims *"the CLI exposes every core capability so the product is fully usable headless (REQ-103)"* — but notes (add/list/delete) and source management (delete/rename/refresh) existed only as Web API routes with zero CLI subcommands. A fully headless user (SSH-only server, no browser) had no way to manage notes or clean up/rename/refresh sources without importing `shoin.store`/`shoin.pipeline` directly in a Python REPL.

- New `shoin note {add,list,delete}` subcommands, thin wrappers around the existing `store.add_note()`/`list_notes()`/`delete_note()` (already used by the Web API).
- New `shoin source {delete,rename,refresh}` subcommands, thin wrappers around `store.delete_source()`/`update_source_title()` and `pipeline.refresh_source()`. `rename` fetches the source first to preserve `origin` (matching `server._h_src_patch`'s get-then-update pattern — `update_source_title()` requires both fields since it updates them in one UPDATE).
- 5 regression tests (`TestCLINoteSourceParity`): add/list/delete roundtrip, empty-notebook hint text, source delete removes the row, rename preserves origin (regression-proofing the exact bug class server.py already avoided), refresh calls the pipeline function correctly.

**Fixed**: `pyproject.toml` version was `0.2.67`, aligned with `config.py` `VERSION = "0.2.68"`.

### v0.2.67 (2026-07-01)
**Feature**: Web UI reindex — a second Socratic "過不足" audit pass asked whether every CLI capability has Web UI parity, per Plan.md's explicit "CLI Parity (REQ-103)" design principle. `shoin reindex <id>` (rebuild embeddings after an `SHOIN_EMBED_MODEL` change) existed only as a CLI subcommand; a user running only the Web UI had no way to recover from a stale/mismatched embedding model without dropping to a terminal. `_check_embed_model_ok()` (`qa.py`) already silently falls back to BM25-only search on a mismatch — the fix was previously reachable only outside the app the mismatch was detected in.

- `server.py`: new route `POST /api/notebooks/{id}/reindex` → `_h_nb_reindex()`, a thin wrapper around the existing `pipeline.reindex_notebook()` (already used by the CLI). Raises `NOTEBOOK_NOT_FOUND` (404) for a missing notebook, matching every other notebook-scoped route.
- `index.html`: new "埋め込みを再構築" (Rebuild embeddings) button in the Studio pane below the export section, with a `title` tooltip (via the `data-i18n-title` pattern from v0.2.66) explaining when to use it. Reports `{n}/{total} チャンクを再埋め込みしました` via toast on completion.
- 2 regression tests added (`ServerTest.test_reindex_endpoint_returns_embedded_and_total_counts`, `test_reindex_missing_notebook_returns_404`).

**Fixed**: `pyproject.toml` version was `0.2.66`, aligned with `config.py` `VERSION = "0.2.67"`.

### v0.2.66 (2026-07-01)
**Feature**: Citation verification status surfaced in Markdown export — a Socratic "過不足" (excess/deficient feature) audit of the product asked: does every exported artifact still carry the product's flagship differentiator (machine-checked citation verification)? Tracing `export_markdown()` found it reconstructed the `[S#]` source legend from `citation_report` but never rendered `confirmed`/`misattributed`/`uncited`/`degraded` — the exact verification signal. Once a Q&A exchange or Studio output was exported to Markdown (to share, archive, or paste into a report), it became visually indistinguishable from unverified prose; the newly-added `uncited` field (v0.2.65) had zero representation in exports either.

- `export._status_line(report) -> str`: builds a single Markdown status line from a `citation_report` dict (`検索のみ` / `⚠検証失敗: S3` / `⚠番号取り違えの可能性: S2` / `✓根拠確認済み: S1` / `⚠無出典の断定文 (N)`, joined with ` / `). Empty string when there's nothing to report.
- `export._parse_report()`: extracted the existing malformed-JSON-safe parsing (previously inlined only in the chat-message loop) so both the chat-message and Studio-output loops in `export_markdown()` share one safe parser.
- Wired into both loops: assistant chat messages now print the status line under the body; Studio output cards do too (Studio outputs previously rendered *zero* citation metadata in export, not even the `[S#]` legend).
- 6 regression tests added (`_status_line` unit tests + integration tests asserting `confirmed`/`uncited`/`degraded` text actually appears in `export_markdown()` output for messages and Studio outputs).

**Minor**: The same audit also asked whether the `-word` negative-term search filter (v0.2.47, fully wired through `search.py`) is discoverable by an actual user. It is not — `#askInput`'s placeholder never mentioned it, so a user would need to read the source or CHANGELOG to learn the syntax exists. Added a `title` tooltip (`data-i18n-title`, a new i18n attribute pattern alongside the existing `data-i18n-ph`) explaining `-word` exclusion syntax with an example.

**Fixed**: `pyproject.toml` version was `0.2.65`, aligned with `config.py` `VERSION = "0.2.66"`.

### v0.2.65 (2026-07-01)
**Feature**: Uncited-assertion detection — the top-priority open item from `docs/product-review.md`'s "未実装" (not yet implemented) list. `verify_grounding()`'s two existing checks (grounding confirmation, mis-numbering detection) only ever examine sentences that *already* carry a `[S#]` citation; a hallucinated or simply unsupported factual claim with zero citations anywhere in it was completely invisible to Shoin's citation verification, despite "the machine-checkable citation verification" being the product's flagship differentiator.

- `citation.uncited_sentences(text) -> list[str]`: scans sentence-split fragments for ones with no citation marker anywhere. Mirrors `verify_grounding()`'s design: no aggregate score, a concrete list of the actual flagged sentences (the project's own v0.1.5 lesson — aggregate scores are misleading when partially inconclusive; lists are honest).
- Resolves the most common LLM citation placement — a trailing citation-only fragment after the sentence-boundary split (`"Sentence. [S1]"` → `["Sentence.", "[S1]"]`, the same pattern `verify_grounding()` handles via `prev_claim`, v0.2.44) — via a `pending`/lookahead mechanism so a sentence immediately followed by its own trailing `[S#]` is correctly NOT flagged. An earlier draft without this lookahead flagged the single most common valid citation pattern in the whole system as an error; caught by writing `test_does_not_flag_sentence_with_citation` before shipping.
- Excludes trivial filler (`_MIN_CLAIM_CHARS = 5`, so short acknowledgments like `"はい。"` don't count as claims) and sentences containing an explicit "not in the source" disclaimer (`_DISCLAIMER_MARKERS`) — the system prompt's rule 3 instructs the model to say exactly that when a fact is missing, so it is correct behavior, not an unsupported assertion.
- `make_report()` gained `check_uncited: bool = True`. `qa.ask()`'s and `server._h_ask_sse()`'s degraded-mode fallback (`_degraded_text()`) pass `check_uncited=False`, because the degraded prefix ("LLM endpoint unreachable...") is system meta-commentary, not a claim about source content, and would otherwise be a guaranteed false positive on every degraded answer.
- `CitationReport` gained `uncited: NotRequired[list[str]]`. `cli._print_report()` prints the count and each sentence; `cli._cmd_studio()`'s existing "suppress separator when nothing to show" guard (v0.2.55) was widened to `if result.report["cited"] or result.report.get("uncited")` so uncited-only reports aren't silently dropped. `index.html` renders a new `⚠ uncited assertions: N` badge (with the actual sentences in a hover tooltip) in the chat message renderer, the SSE streaming `done` handler, and the Studio output card header.
- 11 regression tests added (`TestUncitedSentences`), including an integration test asserting `ask()`'s degraded path never flags its own meta-message.

**Fixed**: `pyproject.toml` version was `0.2.64`, aligned with `config.py` `VERSION = "0.2.65"`.

### v0.2.64 (2026-07-01)
**Fixed**: `ruff check .` (configured in `pyproject.toml`, never run as part of this audit loop's verification command until now) reported `tests/test_core.py` defined `class TestCLI(unittest.TestCase):` twice — once at line 2438 (a single test, `test_serve_oserror_returns_exit_code_1`) and again at line 4136 ("CLI main() error-handling tests"). Python silently rebinds the class name on the second `class` statement, so the first `TestCLI` object becomes unreachable — pytest/unittest test discovery only ever sees the second one. `test_serve_oserror_returns_exit_code_1` (added in v0.2.41 specifically to cover `main()`'s `except OSError` handler around the `serve()` call) had been **silently never executing** since the second `TestCLI` class was introduced; every subsequent `pytest tests/` run in this project's history reported it as passing without ever running it. Fix: merged the orphaned test into the surviving `TestCLI` class; deleted the now-empty duplicate class shell.

**Fixed**: The same ruff run flagged `F811 Redefinition of unused test_add_source_fk_violation_raises_notebook_not_found` — two methods with the identical name in the same `TestStore` class (lines 526 and 698), both asserting the same behavior (`add_source()` on a deleted notebook raises `NOTEBOOK_NOT_FOUND`). Unlike the `TestCLI` case, this was a true duplicate, not a coverage loss: the surviving definition (line 698) already covered the exact scenario. Fix: removed the shadowed duplicate at line 526.

`pytest tests/` now collects and runs 518 tests (up from 517 — the previously dead `test_serve_oserror_returns_exit_code_1` now actually executes and passes). `python -m mypy shoin/` remains clean. The remaining 29 `ruff check` findings (unused imports, ambiguous single-letter variable names, multiple-imports-per-line) in test files are cosmetic style issues with no functional impact and were left as-is per this project's audit discipline (fix confirmed bugs with concrete failing paths, not speculative style cleanup).

**Fixed**: `pyproject.toml` version was `0.2.63`, aligned with `config.py` `VERSION = "0.2.64"`.

### v0.2.63 (2026-07-01)
**Fixed**: `mypy --strict` (configured in `pyproject.toml`) reported 2 real type errors, never caught because mypy had not been run as part of the audit loop's verification command.

- `store.py` `list_notebooks_with_counts()` was typed `-> list[dict[str, object]]`. The nested `"counts"` value is itself a `dict[str, int]`, but the outer `dict[str, object]` annotation erased that structure, so `cli.py`'s `_cmd_notebook()` (`c = row["counts"]; ... c['sources']`) failed to type-check: indexing an `object` is not allowed. Fix: added `NotebookWithCounts`/`_Counts` `TypedDict`s (matching the existing `CitationReport` TypedDict pattern in `citation.py`) and changed the return type to `list[NotebookWithCounts]`. No runtime behavior change — the dict literal returned by the method already matched this shape.
- `ingest.py`: `_HTMLText.RCDATA_CONTENT_ELEMENTS = ("textarea",)` overrides a `html.parser.HTMLParser` class attribute that typeshed marks `Final`. This override is deliberate and tested (see v0.2.40 changelog: it's the mechanism that keeps `<title>` out of raw-text/CDATA mode so an unclosed `<title>` doesn't swallow the rest of the document). `HTMLParser` itself does not enforce `Final` at runtime, so the override works correctly; it is a type-checker-only violation. Fix: added `# type: ignore[misc]` with a comment explaining why the override is safe, rather than changing the (correct, tested) runtime behavior.

`python -m mypy shoin/` now reports "Success: no issues found in 14 source files". Added `mypy shoin/` to the working verification command for future audit passes — `pytest tests/test_core.py` alone (v0.2.56–v0.2.61) and even `pytest tests/` alone (v0.2.62) were both insufficient to catch static-typing regressions.

**Fixed**: `pyproject.toml` version was `0.2.62`, aligned with `config.py` `VERSION = "0.2.63"`.

### v0.2.62 (2026-07-01)
**Fixed**: Two tests in `tests/test_qa.py` were failing when the full `tests/` directory was run together (they had only been passing because prior audit sessions verified fixes by running `tests/test_core.py` in isolation, never the full suite).

- `test_available_returns_true_when_endpoint_reachable`: the mock `urlopen` return value was a bare `io.BytesIO` with no `getheader()` method and no `Content-Type` header. Since v0.2.54, `available()` requires the response's `Content-Type` header to contain `"json"` to return `True` (distinguishing a real LLM API server from an unrelated HTTP server on the same port); the AttributeError from the missing `getheader()` is caught (v0.2.57) and `available()` correctly returns `False` for this unrealistic mock — but the test still asserted `True`. The mock never accounted for the Content-Type check added four versions after the test was originally written. Fix: give the mock a `getheader()` method returning `"application/json"` for the `Content-Type` header, matching what `urllib.request.urlopen()` actually returns in production (an `http.client.HTTPResponse`).
- `test_llm_response_too_large_raises_bad_response`: used exactly `32 * 1024 * 1024` bytes as the response body. Since v0.2.54's boundary fix, `_post()` only raises the size-exceeded error when `len(raw) > _MAX_RESPONSE` — exactly 32 MB is valid and falls through to `json.loads()`, which fails with "invalid JSON" (the body was `b"x" * _MAX`, not valid JSON) instead of the expected "32 MB" message. The test was asserting the pre-v0.2.54 boundary (`>=`) after the fix intentionally moved it to `>`. Fix: use `_MAX + 1` bytes so the response genuinely exceeds the cap and the size-exceeded path fires as intended.

Neither `llm.py` production code needed a change — both failures were stale test fixtures from before the Content-Type check (v0.2.54) and the boundary fix (v0.2.54) were introduced, never updated to match. Running `pytest tests/` (all four test files together, not just `test_core.py`) is required to catch this class of regression; `tests/test_qa.py`, `tests/test_server.py`, and `tests/test_studio.py` were not part of the working test command used during the v0.2.56–v0.2.61 audit passes.

**Fixed**: `pyproject.toml` version was `0.2.61`, aligned with `config.py` `VERSION = "0.2.62"`.

### v0.2.61 (2026-06-30)
**Fixed**: `bm25_search()` (`search.py`) FTS5+LIKE merge path sorted the FTS5-hit sublist *before* extending with LIKE-only hits, leaving the combined list globally unsorted. A LIKE-only chunk with `bm25=50` was appended after an FTS5 chunk with `bm25≈5`, so `rrf_fuse()` received the hits out of rank order and assigned a worse rank-reciprocal score to the higher-scoring LIKE-only chunk. This affected queries mixing a long ASCII/CJK term (handled by FTS5 trigrams) with a short term (<3 chars, handled by LIKE scan), e.g. `"local 猫"`. Fix: move the `fts_hits.sort()` call to after the `extend()` so the combined list is globally sorted before being passed to `rrf_fuse()`. Regression test added.

**Fixed**: `pyproject.toml` version was `0.2.60`, aligned with `config.py` `VERSION = "0.2.61"`.

### v0.2.60 (2026-06-30)
**Fixed**: `retrieve()` (`search.py`) did not normalize RRF scores to [0,1] before passing them to `rerank()`. `rrf_fuse()` returns rank-reciprocal scores in the range ~[0.012, 0.033] (i.e., `1/(k+rank+1)` with k=60, pool≤24), while `lexical_overlap()` returns values in [0,1]. With `rerank(weight=0.3)`, the lexical term (`0.3 * lex`) contributed up to 13× more than the RRF term (`0.7 * rrf_score`), making the reranker effectively ignore the hybrid retrieval signal entirely — the final ordering was determined almost entirely by lexical repetition, not by BM25/vector rank. The previous `fuse()` function emitted [0,1] scores via `_minmax` implicitly, but `rrf_fuse()` emits raw rank-reciprocal values. Fix: in `retrieve()`, apply `_minmax` to the fused hit scores before passing to `rerank()`, restoring the intended 70/30 RRF-vs-lexical blend ratio. Regression test added.

**Fixed**: `pyproject.toml` version was `0.2.59`, aligned with `config.py` `VERSION = "0.2.60"`.

### v0.2.59 (2026-06-30)
**Fixed**: `main()` (`cli.py`) did not catch `OSError`. `Store.__init__` calls `Path.mkdir(parents=True, exist_ok=True)` to create the data directory; when `SHOIN_DATA_DIR` or the default `~/.local/share/shoin` is on a read-only filesystem or the user lacks write permission, `mkdir()` raises `PermissionError` (an `OSError` subclass). Before this fix, the exception propagated through `main()`'s `except (StoreError, IngestError, LLMError, OverflowError, KeyboardInterrupt)` handler as a raw Python traceback. Fix: add `except OSError` clause to the outer handler in `main()` that prints `SYSTEM_IO_ERROR` and returns exit code 1. Regression test added.

**Fixed**: `pyproject.toml` version was `0.2.58`, aligned with `config.py` `VERSION = "0.2.59"`.

### v0.2.58 (2026-06-30)
**Fixed**: `extract_url()` (`ingest.py`) did not strip null bytes (U+0000) from extracted text before the empty-content guard. `str.strip()` skips null bytes (Unicode category Cc, not whitespace), so a URL returning a body of all-null bytes produced the non-empty string `"\x00\x00\x00"` — truthy, passing `if not text:` — and the garbage content was indexed into BM25 and vector search. The identical fix was applied to `extract_file()` in v0.2.50 (`text = text.replace("\x00", "").strip()`) but `extract_url()` was missed. Fix: apply the same `replace("\x00", "")` guard before `strip()` in `extract_url()`. Regression test added.

**Fixed**: `pyproject.toml` version was `0.2.57`, aligned with `config.py` `VERSION = "0.2.58"`.

### v0.2.57 (2026-06-30)
**Fixed**: `available()` (`llm.py`) raised `AttributeError` when `urlopen` returned a response object without a `getheader()` method — e.g. an unusual WSGI shim or a test double with a bare interface. `resp.getheader("Content-Type", "")` (added in v0.2.54) is not part of the `io.IOBase` contract; only `http.client.HTTPResponse` guarantees it. `AttributeError` was not in the `except (OSError, ValueError, http.client.HTTPException)` clause, so it propagated as a bare exception instead of the expected `False` return. Fix: add `AttributeError` to the except tuple. Regression test added.

**Fixed**: `pyproject.toml` version was `0.2.56`, aligned with `config.py` `VERSION = "0.2.57"`.

### v0.2.56 (2026-06-30)
**Feature**: Reciprocal Rank Fusion (RRF) replacing convex-combination score fusion in `retrieve()` (`search.py`). The previous `fuse(adaptive_alpha(query))` combined min-max-normalized BM25 and vector scores via a linear combination. Min-max normalization is per-query and pathological on single-hit result sets (v0.1.45 class bug); adaptive alpha adds heuristics that can fire incorrectly on neg-term queries (v0.2.51 class bug). RRF (`score = Σ 1/(k + rank + 1)`, k=60, Cormack SIGIR 2009) uses only rank positions, completely bypassing scale incompatibility between raw FTS5 BM25 values and cosine similarity scores in [0,1]. A chunk found by both BM25 (rank 1) and vector (rank 1) scores ≈ 0.0328; found by only one at rank 1 scores ≈ 0.0164 — naturally combining both signals without normalization or alpha tuning. `rrf_fuse()` added after `fuse()`; `retrieve()` now calls `rrf_fuse(bm25_hits, vec_hits)` instead of `fuse(adaptive_alpha(query), ...)`. `fuse()` and `adaptive_alpha()` retained for backward compatibility with existing tests. 5 regression tests added.

**Fixed**: `pyproject.toml` version was `0.2.55`, aligned with `config.py` `VERSION = "0.2.56"`.

### v0.2.55 (2026-06-30)
**Fixed**: `_h_ask_sse()` (`server.py`) left an orphaned user turn in the DB when the LLM's `chat_stream()` yielded zero tokens (e.g., a reasoning model that emits only `<think>` tokens with no `content` deltas). `parts=[]`, `full=""`, and the guard `if full:` prevented `store.add_message()` from saving any assistant message. On page reload, `list_messages()` returned the unanswered user question with no reply. All other disconnect/error paths (meta-send, `build_context` exception) already saved empty assistant messages; this path was inconsistent. Fix: remove the `if full:` guard so an empty assistant message is always persisted after SSE streaming, regardless of content length. Regression test added.

**Fixed**: `_h_src_upload()` (`server.py`) committed the source row via `index_source()` (which used the tmp file path as the title) and then called `store.update_source_title()` as a second separate transaction. A concurrent `DELETE /api/sources/{id}` in the window between the two commits caused `update_source_title` to raise `SOURCE_NOT_FOUND` → HTTP 404, while the source remained in the DB with the tmp-path as its title — invisible to the client who received an error. Fix: add an optional `title: str | None = None` keyword argument to `pipeline.index_source()`; when supplied, it overrides `extracted.title` in the `store.add_source()` call so the source is committed with the correct user filename in a single transaction, eliminating the two-phase commit window. `server._h_src_upload` now passes `title=raw_name`. Regression test added.

**Fixed**: `cli.py` (`main()` outer handler) did not catch `sqlite3.OperationalError`. Any `store.*` call that timed out waiting for the SQLite WAL write lock (after the 5000ms `busy_timeout`) raised `sqlite3.OperationalError: database is locked`. This propagated through `main()`'s `except (StoreError, IngestError, LLMError, OverflowError, KeyboardInterrupt)` — which does not include `OperationalError` — and produced a raw Python traceback instead of a clean error message. Fix: add `except sqlite3.OperationalError` clause to `main()` that prints `err.prefix` with `SYSTEM_DB_LOCKED` and returns exit code 1. Regression test added.

**Fixed**: `_cmd_studio()` (`cli.py`) unconditionally printed `---` and called `_print_report()` after generating Studio output, even when the output contained no `[S#]` citations. The result was a lone `---` line with nothing below it — the same issue fixed for `_cmd_ask` in v0.2.27. Fix: guard the separator and report with `if result.report["cited"]:`, matching the `_cmd_ask` pattern. Regression test added.

**Fixed**: `_cmd_add()` (`cli.py`) per-target inner `except (IngestError, StoreError)` did not catch `sqlite3.OperationalError`. A DB lock timeout during `store.add_chunks()` inside `index_source()` was not caught by the inner handler, propagating to `main()` (which also didn't catch it, as fixed above) and printing a raw traceback while skipping the remaining targets in the batch. Fix: add `except sqlite3.OperationalError` to the inner handler so the per-file loop continues with remaining targets, printing a clean error for the locked file. Regression test added.

**Fixed**: `pyproject.toml` version was `0.2.54`, aligned with `config.py` `VERSION = "0.2.55"`.

### v0.2.54 (2026-06-30)
**Fixed**: `available()` (`llm.py`) returned `True` for any HTTP 200 response, including a plain HTTP server (nginx, `http.server`) on the configured port returning `text/html` on `GET /models`. The function opened the connection and immediately returned `True` without reading or validating the response body. Callers in `qa.ask()` use `available()` to decide whether to degrade to BM25-only retrieval; with a false-positive `True`, they skipped the `SYSTEM_SERVICE_UNAVAILABLE` degradation path and called `llm.chat()`, which raised `SYSTEM_LLM_BAD_RESPONSE` (invalid JSON) on every request — a worse error code that bypassed callers' graceful degradation checks. Fix: after `urlopen()`, read the `Content-Type` header; return `True` only when it contains `"json"` (all OpenAI-compatible endpoints send `application/json`). 2 regression tests added.

**Fixed**: `_post()` (`llm.py`) raised `LLMError` for valid JSON responses of exactly 32 MB. `resp.read(_MAX_RESPONSE)` reads up to 32 MB; if the response is exactly 32,768,000 bytes, `len(raw) == _MAX_RESPONSE` is `True` and `LLMError("SYSTEM_LLM_BAD_RESPONSE", "response exceeded 32 MB size limit")` is raised even though the full response was received without truncation. Fix: read `_MAX_RESPONSE + 1` bytes and check `len(raw) > _MAX_RESPONSE` — when the response is exactly 32 MB, `read(_MAX_RESPONSE + 1)` returns only 32 MB bytes (nothing more is available), so the guard correctly does not fire. Regression test added.

**Fixed**: `pyproject.toml` version was `0.2.53`, aligned with `config.py` `VERSION = "0.2.54"`.

### v0.2.53 (2026-06-30)
**Fixed**: `replace_chunks_for_source()` (`store.py`) and `update_source_sha256()` were called as two separate transactions in `pipeline.refresh_source()`. A process crash between the two commits left the source in an inconsistent state: new chunk content committed with stale `sha256` and `title` in the source row. Fix: add optional `sha256` and `title` keyword parameters to `replace_chunks_for_source()` so the source metadata update runs inside the SAME `with self.conn:` block as the chunk DELETE+INSERT, making the entire refresh atomic. `pipeline.refresh_source()` now passes `sha256/title` directly and omits the separate `update_source_sha256()` call. Regression tests added.

**Fixed**: `add_source()` (`store.py`) classified any non-UNIQUE `IntegrityError` as `NOTEBOOK_NOT_FOUND` via an implicit else branch. A future CHECK or NOT NULL constraint violation on the `sources` table would produce a misleading HTTP 404 "notebook not found" error instead of HTTP 500. Fix: explicitly check for `"FOREIGN KEY"` in the error message for the `NOTEBOOK_NOT_FOUND` path; all other `IntegrityError` variants now raise `SYSTEM_INTERNAL_ERROR` instead. Regression test added.

**Fixed**: `verify_grounding()` (`citation.py`) applied `_BRACKET_RE.sub(" ", sentence)` to strip `[S#]` markers before computing claim bigrams — but `_BRACKET_RE` only matches ASCII `[`/`]` (U+005B/U+005D). Full-width citation brackets `［Ｓ１］` (U+FF3B/U+FF3D), which some Japanese LLMs output, were not stripped. They survived into `bare`, adding ~4 spurious bigrams from the NFKC-normalized bracket form. For citation-only fragments like `"Result. ［Ｓ１］"`, the non-empty spurious bigrams prevented `prev_claim` propagation (the `if not claim` guard was bypassed), so the citation was never confirmed. For short sentences with embedded brackets, the inflated denominator pushed overlap below `CONFIRM_MIN`. Fix: apply `unicodedata.normalize("NFKC", sentence)` before `_BRACKET_RE.sub()` so full-width brackets are normalized to ASCII and stripped. Two regression tests added.

**Fixed**: `pyproject.toml` version was `0.2.52`, aligned with `config.py` `VERSION = "0.2.53"`.

### v0.2.52 (2026-06-30)
**Fixed**: `_degraded_text()` (`qa.py`) assigned S-numbers per-hit instead of per-unique-source, causing a mismatch with `build_context`'s per-source S-numbering. When the top two retrieval hits came from the same source, `_degraded_text` emitted `[S2]` for a second chunk of source 0 — but `make_report` (and the user-visible citation report) attributed `[S2]` to a completely different source (the second unique source in `context.source_titles`). The user saw content from source 0 labelled as source 1, and `[S3]` was reported as out-of-range even when a third source existed. Fix: skip duplicate `source_id`s in the enumeration loop so S-numbers increment only when a new source is encountered, matching `build_context`'s first-seen-unique-source ordering. Regression test added.

**Fixed**: `build_context()` (`qa.py`) did not enforce the per-source token budget for scripts where `estimate_tokens()` returns 0 (Arabic, Cyrillic, Hebrew, Devanagari, pure punctuation — outside `_CJK_RANGES` and `_WORD_RE`). `cost = 0` made `cost > remaining` always `False`, so all chunks were appended without any budget cap — a source with 20 Arabic paragraphs could consume the entire LLM context window. Fix: compute `effective_cost = cost if cost > 0 else len(h.text) // 5` (≈ ASCII word density as a conservative upper bound) and use `effective_cost` for the budget guard and accumulator. When truncating zero-token text that overflows, use `h.text[:remaining * 5]` as a character-window fallback since `_truncate_tokens` also returns the full text for zero-token input. Regression test added.

**Fixed**: `pyproject.toml` version was `0.2.51`, aligned with `config.py` `VERSION = "0.2.52"`.

### v0.2.51 (2026-06-30)
**Fixed**: `adaptive_alpha()` (`search.py`) called `_DIGIT_RE.search(query)` on the raw query including neg-terms. A query like `"neural network -v2"` triggered the digit-presence penalty (`alpha -= 0.15`) because the digit `2` exists in the negated token `-v2`, biasing retrieval toward exact-match even though the positive content had no digits. Fix: use `clean_q = strip_neg_terms(query)` as the target for both the digit regex and the quoted-phrase `'"' in query` check, so neg-terms never influence the alpha heuristic. Regression test added.

**Fixed**: `bm25_search()` (`search.py`) merge path (FTS5 + LIKE) returned the combined result list without slicing to `k`. When a query contained at least one long term (≥3 chars) handled by FTS5 and at least one short term (<3 chars) handled by LIKE, `bm25_search(store, nb_id, query, k=10)` could return up to `k + 2000` hits instead of `k`. The LIKE-only path (line 247) and FTS5-only early-return already sliced to `k`; only the merge path was uncapped. Fix: add `[:k]` cap before returning from the merge path, making all three exit paths consistent. Regression test added.

**Fixed**: `pyproject.toml` version was `0.2.50`, aligned with `config.py` `VERSION = "0.2.51"`.

### v0.2.50 (2026-06-30)
**Fixed**: `_hard_split()` (`chunk.py`) used `window = max(limit, 1)` as a character index when falling back to the character-window path for unbreakable text. For ASCII text (≈5 chars/token), this produced chunks approximately 5× too small (a 512-token budget produced 512-char chunks ≈ 102 tokens instead of ≈512 tokens). Fix: compute `chars_per_token = len(p) / tok` and use `window = max(int(limit * chars_per_token), 1)` so the window is proportional to the actual character density of the text. Regression test added.

**Fixed**: `_hard_split()` (`chunk.py`) did not split very long zero-token text (Arabic, Hebrew, Cyrillic, pure punctuation). `estimate_tokens()` returns 0 for scripts outside its CJK and ASCII-word coverage; a zero result always satisfies `tok <= limit`, so a pathologically long zero-token paragraph (e.g., 100K chars of Arabic) was emitted as a single oversized chunk with no further splitting. Fix: add an explicit guard — when `tok == 0` and `len(p) > limit * 5`, apply the character-window fallback with `window = max(limit * 5, 1)` (matching ≈5 chars/token ASCII density as a conservative upper bound). Regression test added.

**Fixed**: `_decode()` (`ingest.py`) did not detect UTF-16 BOM-prefixed files. A `.txt` or `.md` file encoded as UTF-16 with a byte-order mark (`\xff\xfe` or `\xfe\xff`) was mishandled: `utf-8-sig` rejected it (0xFF is invalid UTF-8), then `cp932` accepted every byte sequence silently — producing mojibake (PUA characters, embedded null bytes) rather than the correct text. The cp932 fallback was designed for Shift-JIS Japanese content, not as a universal binary-safe decoder. Fix: when `data[:2]` matches a UTF-16 BOM, prepend `"utf-16"` to the candidate list before `utf-8-sig` and `cp932`. Regression test added.

**Fixed**: `extract_file()` (`ingest.py`) did not strip null bytes (U+0000) before the empty-text guard. `str.strip()` removes Unicode whitespace (category Zs/Zl/Zp and ASCII controls), but U+0000 is category Cc (control) and is NOT stripped. A `.txt` file containing only null bytes produced `'\x00\x00\x00'` — truthy, non-empty — so `if not text:` did not fire and the file was indexed as valid content, inserting garbage into BM25 and vector search. Fix: add `text = text.replace("\x00", "").strip()` before the guard so null-only files raise `INGEST_EMPTY`. Regression test added.

**Fixed**: `pyproject.toml` version was `0.2.49`, aligned with `config.py` `VERSION = "0.2.50"`.

### v0.2.49 (2026-06-30)
**Fixed**: `_post()` (`llm.py`) called `exc.read().decode(...)[:300]` on HTTPError response bodies — reading the entire body into memory before slicing to 300 chars. A malicious or misconfigured endpoint returning a gigabyte 500 response caused OOM before the truncation ran. Fix: `exc.read(300).decode(...)` passes the size limit to `read()` directly.

**Fixed**: `embed()` (`llm.py`) raised `AttributeError` when a malformed endpoint returned `response["data"]` as a list of non-dict items (e.g., strings). The `lambda d: int(d.get("index", 0))` sort key called `.get()` on a `str`, raising `AttributeError`. This was NOT in the `except (KeyError, TypeError, ValueError, OverflowError)` clause, so it escaped `embed()` and `_query_vector()` in `qa.py`, bypassing the BM25-only degradation path and producing HTTP 500. Fix: add `AttributeError` to the exception tuple.

**Fixed**: `_h_ask_sse()` (`server.py`) returned without saving an empty assistant message when `ConnectionError` fired during the `meta` SSE event send (line ~593). This left a dangling user turn visible in `list_messages()` (used by `GET /api/notebooks/{id}` to populate the chat history panel on page reload), rendering an unanswered question in the UI. The analogous `build_context` exception path (v0.2.39) already saved an empty assistant message for this exact reason; the `meta`-send `ConnectionError` path was inconsistent. Fix: add `store.add_message(nb_id, "assistant", "", json.dumps(make_report("", [])))` before `return` in the `except ConnectionError` block, matching the `build_context` error path pattern.

**Fixed**: `pyproject.toml` version was `0.2.48`, aligned with `config.py` `VERSION = "0.2.49"`.

### v0.2.48 (2026-06-30)
**Fixed**: `_embed_chunks()` (`pipeline.py`) stored vectors of any dimension without validation. A temporarily misconfigured or restarting embedding endpoint can return vectors of the wrong dimension (e.g., 384 floats when 768 are expected); these were packed via `array.array("f", vec).tobytes()` and stored without a dimension check. On subsequent `vector_search()`, cosine similarity compared BLOBs of different byte lengths, producing garbage scores. The `embed_model` mismatch guard only fires on model *name* change; it does not fire if the same model name returns different-dimension vectors. The `force=True` path in `reindex_notebook` additionally bypasses even the name guard. Fix: establish `expected_dim` from the first vector in the first batch and validate all subsequent vectors against it; raise `LLMError("SYSTEM_LLM_BAD_RESPONSE", ...)` on mismatch so the `except LLMError: pass` handler leaves BM25-only retrieval intact. Regression test added.

**Fixed**: `refresh_source()` (`pipeline.py`) passed an empty `texts` list directly to `replace_chunks_for_source()` when the re-fetched URL returned no extractable text. This raised `StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "replacement chunk list must not be empty")` — a store-layer error that leaks implementation detail to the HTTP caller. The symmetric fix was applied to `index_source()` in v0.2.46 (`IngestError("INGEST_EMPTY")`), but `refresh_source()` was missed. Fix: add the same `if not texts: raise IngestError("INGEST_EMPTY", ...)` guard before calling `replace_chunks_for_source()`.

**Fixed**: `pyproject.toml` version was `0.2.47`, aligned with `config.py` `VERSION = "0.2.48"`.

### v0.2.47 (2026-06-30)
**Feature**: Negative-term filtering in queries — prefix a word with `-` to exclude chunks containing it (e.g. `Python -legacy`). `neg_terms(query)` parses the negated tokens; `strip_neg_terms(query)` removes them before FTS5/LIKE processing; `_apply_neg_filter(hits, negs)` does the post-retrieval exclusion. The filter is applied at both the `bm25_search()` stage and the final `retrieve()` output (so vector hits are also excluded). A `-` preceded by a word character (e.g. `state-of-the-art`) is treated as a hyphen, not negation.

**Fixed**: `bm25_search()` FTS5+LIKE merge path produced wrong ranking when FTS5's raw BM25 score was near-zero (~2e-6 for small corpora) while LIKE-only hits had integer scores (1, 2, …). After min-max normalization in `fuse()`, LIKE-only hits dominated even when the FTS5 hit matched more query terms. Fix: when merging the two result sets, compute the LIKE needle score for each FTS5 hit and add it to `h.bm25`. A chunk found by both FTS5 (long term) and LIKE (short term) now correctly ranks above a chunk found only by LIKE (short term). Regression test added.

**Feature**: Query type detection in `adaptive_alpha()` — short keyword queries (≤ 3 terms, no question markers) now get `alpha -= 0.15`, biasing toward BM25/exact-match retrieval. This complements the existing +0.15 boost for natural-language questions (ending in か/？/?) and the -0.15 penalty for identifiers/numbers. The adjustments are additive and clamped to [0.2, 0.8]. Research source: Qiita/Zenn/GitHub RAG improvement survey (2024).

**Fixed**: `pyproject.toml` version was `0.2.46`, aligned with `config.py` `VERSION = "0.2.47"`.

### v0.2.46 (2026-06-30)
**Fixed**: `index_source()` (`pipeline.py`) committed the source row via `add_source()` before checking whether `split_text()` produced any chunks. When `split_text()` returned `[]` (e.g. whitespace-only text, scanned PDF with no extractable content), `add_chunks(source_id, [])` was called with an empty list, silently creating a zero-chunk source that was permanently invisible to BM25 search, vector search, and `build_context`. The caller (CLI or server) received a success response with `0 chunks`. Fix: call `split_text()` before `add_source()` and raise `IngestError("INGEST_EMPTY", ...)` immediately if the result is empty, so no source row is committed. As belt-and-suspenders defense, `add_chunks()` (`store.py`) now also raises `StoreError("VALIDATION_REQUIRED_FIELD_MISSING", ...)` on an empty list, matching the existing guard in `replace_chunks_for_source()` (added in v0.2.40).

**Fixed**: `_h_questions()` (`server.py`) skipped writing to `questions_cache` when `suggest_questions()` returned an empty list due to LLM failure on a notebook with active sources (`if questions or not fingerprint:` evaluated to False). Every subsequent request to `GET /api/notebooks/{id}/questions` then re-fired the LLM call with its full timeout (up to `CHAT_TIMEOUT_SEC=180s`), creating an unbounded retry storm in degraded mode. The fear of "permanent suppression" was unfounded — the cache is invalidated whenever sources are added, deleted, or refreshed via `questions_cache.pop(nb_id, None)`. Fix: remove the `questions or` condition and always write to cache when a fingerprint is available.

**Fixed**: `fuse()` (`search.py`) was asymmetric in its score normalization: BM25-only hits scored in [0..1] (via `_minmax` on the `not vec_hits` early-return path), but when `bm25_hits=[]` and only vector hits were present, the code fell through to the merged-dict convex-combination path and set `h.score = alpha * vec_norm`, capping scores at `alpha` (≈0.5). The compressed score range caused MMR's relevance/diversity trade-off to skew toward diversity for vec-only queries, since the `lam * cand.score` relevance term was halved relative to the BM25-only case. Fix: add a symmetric `if not bm25_hits:` early-return path that normalizes vec scores directly to [0..1], matching the behavior of the existing `not vec_hits` path.

**Fixed**: `pyproject.toml` version was `0.2.45`, aligned with `config.py` `VERSION = "0.2.46"`.

### v0.2.45 (2026-06-30)
**Fixed**: `export_ris()` (`export.py`) produced a blank `DA` field (`"DA  - "`) when `added_at` is an empty string. The v0.2.37 fix that added `or "unknown"` fallback was applied to `export_bibtex()` but not to `export_ris()`. Fix: add `or "unknown"` to the `date` assignment in `export_ris()`, matching the bibtex path.

**Fixed**: `_validate_resolved()` (`ingest.py`) raised bare `ValueError` for zone-scoped IPv6 addresses (e.g. `"fe80::1%eth0"` returned by `socket.getaddrinfo()` on Linux for link-local interfaces). `ipaddress.ip_address()` does not accept RFC 6874 zone IDs. The `ValueError` was not caught by the `except socket.gaierror` handler and propagated through `fetch_url()` and `_dispatch()` as HTTP 500 `SYSTEM_INTERNAL_ERROR` instead of the correct HTTP 400 `INGEST_URL_BLOCKED`. Zone-scoped addresses are inherently link-local (non-public), so the correct behavior is to reject them with `INGEST_URL_BLOCKED`. Fix: wrap `ipaddress.ip_address(raw_addr)` in `try/except ValueError` and raise `IngestError("INGEST_URL_BLOCKED", ...)`.

**Fixed**: `_h_src_patch()` (`server.py`) called `store.get_source(src_id)` a second time after `update_source_title()` returned, purely to build the JSON response `{"id": ..., "title": ...}`. With `ThreadingHTTPServer`, a concurrent `DELETE /api/sources/{id}` between the committed UPDATE and the second `get_source` raised `SOURCE_NOT_FOUND`, causing the client to receive HTTP 404 even though the rename had already succeeded. Fix: remove the second `get_source()` and build the response directly from `src_id` and `title` (already known from the request), eliminating the TOCTOU window.

**Fixed**: `pyproject.toml` version was `0.2.44`, aligned with `config.py` `VERSION = "0.2.45"`.

### v0.2.44 (2026-06-30)
**Fixed**: `verify_grounding()` (`citation.py`) silently dropped citations placed after a period-space boundary (`"Sentence. [S1]"`). `_SENTENCE_SPLIT_RE` splits on `(?<=\.)(?=\s)`, isolating `" [S1]"` as a fragment. After bracket removal, the claim bigrams are empty (`_bigrams("")` → `set()`), and the `if not claim: continue` guard drops the citation entirely — it receives neither a `confirmed` entry nor a `misattributed` entry, even when the cited source perfectly matches the preceding sentence. This is the most common LLM citation placement pattern (end-of-sentence). Fix: track `prev_claim` — the bigrams of the most recent non-citation fragment — and use them when a citation-only fragment's own claim is empty, so the citation is verified against the sentence it annotates. 2 regression tests added.

**Fixed**: `ask()` (`qa.py`) did not guard `build_context(store, hits)` against `sqlite3.OperationalError`. If `store.get_source()` inside `build_context` raised `OperationalError` (DB lock timeout after 5000ms `busy_timeout`), it propagated through `ask()` and bypassed the CLI's `except (StoreError, IngestError, LLMError)` handler, producing a raw Python traceback. The server path was already protected by `_h_ask_sse()`'s `except Exception` guard (`v0.2.31`); the CLI path was not. Fix: wrap the `build_context(store, hits)` call in a `try/except sqlite3.OperationalError` that re-raises as `StoreError("SYSTEM_DB_LOCKED", ...)`.

**Fixed**: `pyproject.toml` version was `0.2.43`, aligned with `config.py` `VERSION = "0.2.44"`.

### v0.2.43 (2026-06-30)
**Fixed**: `_post()` (`llm.py`) did not catch `http.client.HTTPException` (specifically `http.client.IncompleteRead`). When a local LLM endpoint (Ollama, llama.cpp) drops the TCP connection before sending the full `Content-Length` body — e.g. OOM kill, server crash mid-response — `resp.read()` raises `IncompleteRead`, a subclass of `HTTPException` and NOT of `OSError`. None of the three `except` handlers caught it, so it propagated as a bare exception to callers. In `_embed_chunks`, `IncompleteRead` hit `except Exception` (the rollback path) instead of `except LLMError` (the silent-skip/degradation path). In `ask()` and other chat callers, the bare exception bypassed the `LLMError` guard entirely. Fix: add `http.client.HTTPException` to the `(OSError, ValueError)` clause in `_post()`.

**Fixed**: `chat_stream()` (`llm.py`) had the same uncaught `http.client.HTTPException` gap as `_post()`. During SSE stream iteration (`for raw in resp:`), a TCP truncation before `data: [DONE]` raises `IncompleteRead`. Neither `except urllib.error.HTTPError` nor `except (OSError, ValueError)` caught it, so it bypassed the `LLMError` guard in `server.py`'s `_h_ask_sse()` and corrupted the SSE response with an HTTP 500 status line written into the already-flushed stream body — the same class of corruption `v0.2.31` fixed for `build_context()`, but not for the LLM stream path itself. Fix: add `http.client.HTTPException` to the `(OSError, ValueError)` clause in `chat_stream()`.

**Fixed**: `available()` (`llm.py`) did not catch `http.client.HTTPException`. When `SHOIN_LLM_URL` points to a port occupied by a non-HTTP server (e.g. a raw TCP service sending a malformed status line), `urlopen()` raises `http.client.BadStatusLine` — an `HTTPException` subclass, not `OSError`. `available()` is declared to return `bool`; propagating `BadStatusLine` instead was a latent type contract violation. Fix: add `http.client.HTTPException` to the `(OSError, ValueError)` clause in `available()`.

**Fixed**: `pyproject.toml` version was `0.2.42`, aligned with `config.py` `VERSION = "0.2.43"`.

### v0.2.42 (2026-06-30)
**Feature**: Katakana↔Hiragana cross-script search (`search.py`). SQLite FTS5's trigram tokeniser is not kana-aware: a katakana query like コンピュータ would never match a document indexed with hiragana (こんぴゅーた) because the two scripts use different Unicode codepoints. Fix: add `_kana_alt(term)` helper that converts a CJK run character-by-character (katakana U+30A1–U+30F6 ↔ hiragana U+3041–U+3096, offset ±0x60). In `fts_query()`, when a CJK term contains kana, the trigrams of both the original and the alternate-script form are included in the OR expression. Pure-kanji terms (no kana) are unaffected: `_kana_alt()` returns the original string unchanged so no duplicate OR branch is emitted. The LIKE-scan fallback path for short terms is unchanged. The feature is zero-dependency and requires no language detection.

**Fixed**: `pyproject.toml` version was `0.2.41`, aligned with `config.py` `VERSION = "0.2.42"`.

### v0.2.41 (2026-06-23)
**Fixed**: `bm25_search()` (`search.py`) returned early when FTS5 found any hits, even when some query terms had `len < 3` (silently skipped by `fts_query`). For a mixed query like `"local 猫"`: FTS5 found "local" chunks and returned immediately; "猫" (1 char, below the 3-char FTS5 trigram minimum) never got LIKE-scanned — chunks containing only 猫 were silently dropped. Fix: replace the unconditional `if hits: return hits` with a guard that checks whether all query terms were covered by FTS5 (`all(len(t) >= 3 for t in query_terms(query))`). When short terms exist, the LIKE scan runs and its results (for chunks not already found by FTS5) are merged without duplicates.

**Fixed**: `fts_query()` (`search.py`) used `len(term) > 3` to decide whether to decompose a CJK term into trigrams, skipping the trigram branch for exactly-3-char terms. While the behavior was identical in practice (a 3-char term's single trigram equals the term itself), the condition was inconsistent with the design intent of the trigram tokenizer. Fix: `len(term) > 3` → `len(term) >= 3` for correctness.

**Fixed**: `main()` (`cli.py`) placed the `serve()` call outside the `try/except` block, so `OSError` from `ThreadingHTTPServer.__init__` (e.g., `[Errno 98] Address already in use` when the port is already occupied) propagated as an unhandled Python traceback instead of a clean error message. Fix: wrap the `serve()` call in its own `try/except OSError` that prints the error with the standard `err.prefix` format and returns exit code 1.

**Fixed**: `pyproject.toml` version was `0.2.40`, aligned with `config.py` `VERSION = "0.2.41"`.

### v0.2.40 (2026-06-23)
**Fixed**: `_HTMLText.handle_endtag()` (`ingest.py`) reset `_in_title` on `</head>` but did not reset `_skip_depth`. An unclosed `<noscript>`, `<script>`, or `<style>` tag in `<head>` left `_skip_depth=1` for the entire `<body>`, causing `handle_data` to discard every text node. Pages with malformed markup (e.g., `<noscript>` without `</noscript>` in `<head>`) raised `INGEST_EMPTY` instead of extracting body content. Fix: reset `_skip_depth = 0` alongside `_in_title` in the `tag == "head"` branch of `handle_endtag`.

**Fixed**: `replace_chunks_for_source()` (`store.py`) accepted an empty `texts` list without error. Passing `texts=[]` would DELETE all existing chunks and commit zero new chunks — leaving the source permanently with zero content, invisible to all retrieval queries, with no indication of the error. Fix: raise `StoreError("VALIDATION_REQUIRED_FIELD_MISSING")` at entry when `texts` is empty.

**Fixed**: `CREATE VIRTUAL TABLE chunks_fts` (migration 1, `store.py`) lacked `IF NOT EXISTS`. Two concurrent `Store.__init__()` calls on a fresh DB file could both read `current=0` and both execute the DDL; the second thread's `CREATE VIRTUAL TABLE` raised `OperationalError: table chunks_fts already exists`. The comment in `migrate()` incorrectly stated "all IF NOT EXISTS"; the FTS5 virtual table was the exception. Fix: add `IF NOT EXISTS` to the `CREATE VIRTUAL TABLE` statement — supported since SQLite 3.9.0 (2015), well within the 3.34+ requirement.

### v0.2.39 (2026-06-23)
**Fixed**: `build_context()` (`qa.py`) silently dropped an oversize chunk when it was not the first chunk for a source. The budget guard (`if used and used + cost > per_source: break`) came before the truncation guard (`if cost > per_source`), so only the first chunk ever got token-aware truncation. A later chunk that exceeded the remaining budget was thrown away entirely instead of being truncated to fill the space. Fix: replace both guards with a unified `remaining = per_source - used` check; any chunk that doesn't fit is truncated to `remaining` tokens and then the loop breaks.

**Fixed**: `refresh_source()` (`pipeline.py`) checked for SHA-256 collision *after* replacing chunks, leaving the DB inconsistent on failure. If `replace_chunks_for_source()` committed new chunks and then `update_source_sha256()` raised `SOURCE_ALREADY_EXISTS` (refreshed content matched another source in the same notebook), the source row retained the old hash and title while its chunks already contained new content — a permanently inconsistent state. Fix: query for an existing source with the same `(notebook_id, sha256)` pair *before* replacing chunks, raising `SOURCE_ALREADY_EXISTS` if found, so the operation fails cleanly with no DB mutation.

**Fixed**: `delete_source()` (`store.py`) had a TOCTOU gap: `get_source()` confirmed existence, but no `rowcount` check followed the `DELETE`. If a concurrent thread deleted the source between those two steps, `DELETE` matched 0 rows and the method silently returned success (HTTP 200) instead of raising `SOURCE_NOT_FOUND`. Fix: check `cur.rowcount == 0` after the `DELETE` and raise `SOURCE_NOT_FOUND` if nothing was deleted — the same pattern applied to `rename_notebook()` and `delete_notebook()` in v0.2.29 and v0.2.33.

**Fixed**: `delete_note()` (`store.py`) had the same TOCTOU gap as `delete_source()`: the `DELETE` was not followed by a `rowcount` check. Fix: add the `cur.rowcount == 0` guard and raise `NOTE_NOT_FOUND`.

**Fixed**: `_char_bigrams()` (`search.py`) returned `{t}` for a single-character input (e.g., `_char_bigrams("a")` returned `{"a"}`). The same class of bug was fixed in `citation._bigrams()` in v0.2.38. In MMR's `_sim()`, Jaccard of two monogram sets containing the same character equals 1.0, causing single-character chunk texts to be treated as fully duplicate and suppressed by MMR. Fix: mirror the v0.2.38 guard — `if len(t) < 2: return set()`.

**Fixed**: `_h_src_patch()` (`server.py`) used `str(data.get("title") or "")` instead of `self._require()`. A non-string `"title"` value like `42` was silently coerced to `"42"` — the same type-confusion class fixed in `_require()` in v0.2.38, but `_h_src_patch` was not using `_require()`. Fix: replace the manual check with `self._require(self._read_json(), "title")`.

**Fixed**: `_h_ask_sse()` (`server.py`) left an orphaned user message in the DB when `build_context()` raised an exception. The SSE error event was sent and the handler returned, but no assistant message was saved — leaving a dangling user turn that `history_messages()` would silently drop on the next request. Fix: save an empty assistant message (matching the no-hits path pattern) before returning from the `build_context` error handler.

**Fixed**: `export_markdown()` (`export.py`) used `f"**User**: {body}"` without applying `_md_line()`. A user question with an embedded `\n` produced two output lines: `**User**: first line` (bold, labeled) and `second line` (plain, unlabeled) — visually broken in rendered Markdown. All other structural text (source titles, note titles, notebook name) already went through `_md_line()`; chat message bodies were missed. Fix: apply `_md_line(body)` to collapse embedded newlines on the user label line.

**Fixed**: `pyproject.toml` version was `0.1.16`, diverged from `config.py`'s `VERSION = "0.2.38"`. Both are now aligned at `0.2.39`.

### v0.2.38 (2026-06-23)
**Fixed**: `_bigrams()` (`citation.py`) returned `{t}` for single-character input (e.g., `_bigrams("a")` returned `{"a"}`). A character-1 set is not a bigram; passing it to `_overlap()` made a sentence whose sole overlap with a source was one shared character score 1.0, falsely confirming unrelated citations in `verify_grounding()`. Fix: guard `if len(t) < 2: return set()` so the function returns an empty set for inputs of fewer than two characters.

**Fixed**: `_CJK_RANGES` (`chunk.py`) omitted the CJK Unified Ideographs Extension B/C/D/E/F/G/H blocks (U+20000–U+2EBEF). Historical, variant, and rare CJK characters in the supplementary plane were classified as non-CJK, causing `estimate_tokens()` to undercount their token cost by roughly 4× (counted as ~¼-word ASCII runs instead of 1 token per character). This led to context budget overflows for texts containing supplementary-plane characters. Fix: add three ranges — `(0x20000, 0x2A6DF)`, `(0x2A700, 0x2CEAF)`, `(0x2CEB0, 0x2EBEF)` — to `_CJK_RANGES`.

**Fixed**: `_require()` (`server.py`) silently coerced non-string JSON values to strings via `str(raw)`. A request body like `{"name": 42}` would create a notebook named `"42"`, bypassing type expectations. Fix: add an `isinstance` guard before the coercion; non-string non-null values now raise `VALIDATION_FIELD_FORMAT_INVALID` (HTTP 400).

### v0.2.37 (2026-06-23)
**Fixed**: `suggest_questions()` (`studio.py`) filtered English questions too aggressively: the `"?" in q` guard silently dropped valid English questions when the LLM omitted trailing punctuation (e.g., "What is the main thesis" in list form). Japanese questions survived via the `か`/`でしょう` endswith fallback; English ones did not. Fix: accept any line of sufficient length (>= 8 chars) since the prompt already constrains output to questions only.

**Fixed**: `_bib_escape()` (`export.py`) mapped `{` → `(` and `}` → `)`, silently mutating source titles containing curly braces (e.g., "Algorithms {revised}" became "Algorithms (revised)"). Fix: replace with `{\{}` and `{\}}` — balanced BibTeX groups that render as literal braces in LaTeX.

**Fixed**: `export_bibtex()` and `export_ris()` (`export.py`) did not guard against empty or None `added_at` values. `src.added_at[:10]` on an empty string produces `""`, silently inserting a blank date. Fix: `(src.added_at or "")[:10] or "unknown"`.

**Fixed**: `export_ris()` (`export.py`) emitted `"ER  - "` (with trailing space) as the end-of-record marker. The RIS 2001 spec requires `"ER  -"` with no trailing whitespace; some strict reference managers reject records with whitespace after the dash. Fix: remove the trailing space.

**Fixed**: `_post()` (`llm.py`) called `resp.read()` with no size limit. A malicious or buggy LLM endpoint returning gigabytes would be read entirely into memory before JSON parsing, causing OOM. Fix: cap at 32 MB via `resp.read(32 * 1024 * 1024)`; raise `SYSTEM_LLM_BAD_RESPONSE` if the cap is hit.

### v0.2.36 (2026-06-23)
**Fixed**: `_h_src_refresh()` (`server.py`) did not evict the `questions_cache` entry for the affected notebook. The cache fingerprint is `tuple(s.id for s in store.sources_for_notebook(nb_id))`; since source IDs are preserved on refresh (by design), the fingerprint never changes, so stale question suggestions from before the content update were served indefinitely. Fix: add `questions_cache.pop(nb_id, None)` under `questions_cache_lock` after `refresh_source()` returns.

**Fixed**: `store.replace_chunks_for_source()` did not call `touch_notebook()`. If `update_source_sha256()` was never called (e.g., it raised mid-pipeline), the notebook's `updated_at` timestamp would never reflect the chunk replacement. Fix: call `self.touch_notebook(src.notebook_id)` inside the `with self.conn:` block so the timestamp update is atomic with the DELETE+INSERT.

**Fixed**: `store.update_source_sha256()` did not catch `sqlite3.IntegrityError` from the UPDATE. When a refreshed URL returns content whose SHA-256 already exists in the same notebook (i.e., a duplicate source by content), SQLite raised a UNIQUE constraint violation on `(notebook_id, sha256)`, which propagated as an unhandled exception and returned HTTP 500. Fix: wrap the UPDATE in `try/except sqlite3.IntegrityError` and raise `StoreError("SOURCE_ALREADY_EXISTS", ...)` so the dispatcher maps it to HTTP 409.

**Fixed**: `pipeline.py` `refresh_source()` used a local `from .ingest import IngestError` import with a misleading comment "to avoid circular at module level". There is no circular import: `ingest.py` does not import `pipeline.py`. Fix: remove the local import and add `IngestError` to the existing module-level import on line 13.

**Fixed** (UI): Clicking inside the inline source-rename input caused `row.onclick` to fire (`showSource`), opening the source viewer while trying to type. Fix: add `input.onclick = e => e.stopPropagation()` to block click propagation from the input to the row.

**Fixed** (UI): Clicking the delete `×` button while the source-rename input had focus caused the `onblur` handler to fire first, calling `openNotebook()` and rebuilding the DOM — the delete button's `onclick` was then lost on the destroyed element. Fix: use `e.relatedTarget` in `onblur` to detect focus moving to another element within the same row and skip the commit in that case; the action button's own handler runs normally and rebuilds the DOM.

**Fixed** (UI): Inline title-edit `commit` closure captured `cur` by reference. If the user switched notebooks between the double-click and Enter/blur, `openNotebook(cur.id)` would reload the newly selected notebook. Fix: capture `const nb = cur` at the top of the `ondblclick` handler so `commit` uses the notebook at the time of the rename initiation.

### v0.2.35 (2026-06-23)
**Feature**: Source Refresh (`POST /api/sources/{id}/refresh`) — re-fetch a URL source in-place, replacing all chunks atomically while keeping the source ID. This preserves citation references in stored messages (existing `[S1]` links remain valid after a content update). Only URL sources support refresh; file sources return `INGEST_REFRESH_NOT_URL`. UI: URL sources now show a `↻` refresh button in the source list.

- `store.replace_chunks_for_source(source_id, texts)`: Atomic DELETE-old + INSERT-new within a single `with self.conn:` transaction. Raises `SOURCE_NOT_FOUND` if the source was concurrently deleted.
- `store.update_source_sha256(source_id, sha256, title)`: Updates the content hash and title of a source after a refresh; touches the notebook `updated_at` timestamp.
- `pipeline.refresh_source(store, source_id, llm)`: Validates the source is a URL, calls `extract_url()`, calls `replace_chunks_for_source()`, calls `update_source_sha256()`, then re-embeds with `_embed_chunks()`. Returns `IndexResult`.
- New route `("POST", r"^/api/sources/(\d+)/refresh$", "src_refresh")` + handler `_h_src_refresh()` in `server.py`.

**Feature**: Source Title Edit (`PATCH /api/sources/{id}`) — rename a source's display title inline. The existing `store.update_source_title()` method was not exposed through any API. Now:

- New route `("PATCH", r"^/api/sources/(\d+)$", "src_patch")` + handler `_h_src_patch()` in `server.py`. Accepts `{"title": "new name"}` JSON body.
- UI: double-clicking a source title in the source list opens an inline input. `Enter` commits, `Escape` cancels. `blur` also commits to handle click-away.

### v0.2.34 (2026-06-22)
**Fixed**: `_degraded_text()` (`qa.py`) used `[S?]` citation markers instead of valid `[S1]`, `[S2]`, etc. When degraded (LLM unreachable), the response would display source excerpts but the citation extraction regex couldn't parse `[S?]` (requires digits), resulting in an empty `citation_report["cited"]` list and `coverage: 0.0` even though sources were actually shown. Fix: use `[S{i + 1}]` to generate sequential source numbers matching the actual sources, so citations extract correctly and coverage reflects the sources cited.

### v0.2.33 (2026-06-22)
**Fixed**: `store.delete_notebook()` called `get_notebook()` then `DELETE` in separate steps, the same TOCTOU gap already fixed in `rename_notebook()` (v0.2.29). A notebook deleted between those two steps caused the DELETE to silently match 0 rows — the method returned success while no deletion occurred. Fix: remove the pre-check read and check `cur.rowcount == 0` after the DELETE, raising `NOTEBOOK_NOT_FOUND` atomically.

**Fixed**: `store.set_embedding()` accepted zero-length vectors without error. An embedding endpoint returning an empty list `[]` would store `b""` (empty BLOB), which later produces a zero-norm in cosine similarity (guarded to 0.0, but silently degrades retrieval). Fix: raise `StoreError("EMBEDDING_INVALID", ...)` when `vec` is empty.

**Fixed**: `store.migrate()` used `INSERT INTO schema_migrations(version)` which raises a UNIQUE constraint violation when two concurrent threads (from `ThreadingHTTPServer`) both read `version=0` and both try to apply the same migration. The second thread would crash with an unhandled `sqlite3.OperationalError`. Fix: change to `INSERT OR IGNORE INTO schema_migrations(version)` so duplicate version records are silently skipped — all migration DDL is already idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).

### v0.2.32 (2026-06-22)
**Fixed**: `_decode()` (`ingest.py`) ignored the `charset=` parameter in the HTTP `Content-Type` header. Pages encoded as ISO-8859-1, Windows-1252, EUC-JP, or any non-UTF-8/non-CP932 encoding fell through to `data.decode("utf-8", errors="replace")`, producing replacement characters (`?`) for all non-ASCII content. Fix: add a `charset: str | None` parameter to `_decode()`; try the provided charset first (guarded with `LookupError` for unknown names), then fall through to the existing `utf-8-sig` / `cp932` defaults. Add `_charset_from_ctype()` helper to parse the `charset=` value from the Content-Type string; called in `extract_url()` and passed through to `_decode()`.

### v0.2.31 (2026-06-22)
**Fixed**: `_h_ask_sse()` (`server.py`) did not guard `build_context()` after SSE headers were committed. If `build_context()` raised a non-`StoreError` exception (e.g. `sqlite3.OperationalError` on DB-lock timeout), it propagated to `_dispatch`'s `except Exception` handler, which called `_error(500, ...)` → wrote a new HTTP status line into the already-flushed SSE stream body, corrupting the response. Fix: wrap `build_context()` in its own `try/except Exception` block that sends an SSE `error` event and returns cleanly.

**Fixed**: `index.html` ask handler — if the SSE stream was established (HTTP 200, `meta` event received) but then closed without any `delta` or `done` events (the primary trigger being the above `build_context` corruption), the spinner appended to the message bubble was never cleared. The `catch` block only fires on fetch rejection or `reader.read()` throw, not a clean stream close. Fix: add `if (!acc) bd.replaceChildren()` to the `finally` block to clear the spinner whenever no content arrived.

### v0.2.30 (2026-06-22)
**Fixed**: `verify_grounding()` (`citation.py`) processed all cited S-numbers in a sentence as a group rather than independently. When a sentence co-cited `[S1][S2]` and S1 was confirmed (overlap >= CONFIRM_MIN), `continue` skipped misattribution detection for S2 entirely — even if S2's overlap was 0% and S1 matched the claim far better than S2. Fix: restructure the inner loop to evaluate each cited S-number independently; each number is confirmed if its own overlap >= CONFIRM_MIN, or flagged misattributed if any *other* source (including co-cited ones) matches far better. Regression test added.

### v0.2.29 (2026-06-22)
**Fixed**: `_embed_chunks()` (`pipeline.py`) counted `done += len(batch_ids)` instead of actual stored pairs. When a `ChatBackend.embed()` returns fewer vectors than requested, Python's `zip` silently truncates, storing fewer embeddings while `n_embedded` overcounted by the full batch size. Fix: increment `done` once per pair inside the `zip` loop.

**Fixed**: `_embed_chunks()` `except StoreError` did not catch `sqlite3.OperationalError` from `conn.commit()` (raised on DB-lock timeout). The uncommitted batch's `set_embedding(commit=False)` calls would remain pending and could be flushed by a later `conn.commit()` elsewhere. Fix: broaden to `except Exception` so the rollback guard covers both `StoreError` and DB lock failures.

**Fixed**: `store.rename_notebook()` called `get_notebook()` then `UPDATE` in separate steps. A notebook deleted between those two steps would cause the `UPDATE` to silently match 0 rows — the method returned success while no rename occurred. Fix: remove the pre-check read and check `cur.rowcount == 0` after the `UPDATE` instead, raising `NOTEBOOK_NOT_FOUND` atomically.

**Fixed**: `store.update_source_title()` had the same TOCTOU gap as `rename_notebook` — no `rowcount` check after the `UPDATE`. Fix: check `cur.rowcount == 0` and raise `SOURCE_NOT_FOUND` if the source was concurrently deleted.

### v0.2.28 (2026-06-22)
**Fixed**: `_truncate_tokens()` (`qa.py`) and `_tail()` (`chunk.py`) treated `_` as a word separator (via `ch.isalnum()`) while `estimate_tokens()` counted it as a word character (via `_WORD_RE = r"[A-Za-z0-9_]+"`) — causing `parse_user_input` to cost 1 token by `estimate_tokens` but 3 tokens in the truncation/tail logic. Consequence: source context was cut to roughly half the intended token budget for any document with underscore-delimited identifiers (code, config keys). Fix: replace `ch.isalnum()` with `ch.isalnum() or ch == '_'` in both functions so all three are consistent. Regression tests added.

### v0.2.27 (2026-06-22)
**Fixed**: `showSource()` lazy-load `<details>` toggle listener (added in v0.2.26) fetched full source text without an abort signal, leaving orphaned HTTP requests in-flight when the viewer was switched before the `<details>` was expanded. The outer `sig` (`AbortController.signal`) was in scope but not passed to the inner `api()` call. Fix: pass `{signal: sig}` to the fetch, add `if (sig.aborted) return;` guard after the await, and change the catch handler to `if (!sig.aborted) body.textContent = e.message` to suppress abort errors.

**Fixed**: `_cmd_ask()` (`cli.py`) printed a spurious `---` separator followed by an empty citation report when the answer was degraded (LLM unreachable, search-only). `answer.hits` is truthy for degraded answers (retrieval still runs), but the degraded text contains no `[S#]` markers, so `_print_report()` produced empty output. Fix: change `if answer.hits:` to `if answer.hits and not answer.degraded:`.

### v0.2.26 (2026-06-22)
**Feature**: Source passage highlighting — clicking an [S1] seal chip now shows the retrieved excerpt immediately (no network round-trip), with lazy-load of the full source text via `<details>`. The specific context fed to the LLM is preserved in `source_excerpts` inside `CitationReport` so the excerpt is available from history on reload without an extra API call.

- `citation.py`: Added `source_excerpts: NotRequired[dict[str, str]]` to `CitationReport`. Populated in `make_report()` when `source_bodies` are supplied (each entry is the retrieved context text for that S-number, already bounded by the context token budget).
- `index.html`: `renderWithSeals()` extracts `source_excerpts` from the report and passes it through `openSeal()` to `showSource()`. `showSource()` now renders the excerpt as a highlighted passage block, then lazily fetches the full source only when the user expands the "View full source" `<details>` element. Falls back to immediate fetch when no excerpt is available (old persisted messages, Studio outputs).

### v0.2.25 (2026-06-22)
**Fixed**: `bm25_search()` LIKE fallback had no SQL `LIMIT` clause. For large notebooks, a short query (single CJK character, 2-char ASCII term) could pull tens of thousands of rows into memory before Python-side scoring and truncation to k. Fix: cap the LIKE scan at `max(k * 10, 2000)` rows via `LIMIT ?` in the SQL, matching the "limited to 2000 rows" statement in CLAUDE.md that was previously documentation-only.

**Fixed (docs)**: CLAUDE.md "Atomic Database Operations" section incorrectly stated that source + chunks + embed operations are wrapped in a single transaction. `add_source()` commits independently before `add_chunks()` is called, so a failure between the two (very unlikely in practice) could leave a zero-chunk source. Corrected to accurately describe `add_chunks()` atomicity and best-effort embedding.

### v0.2.24 (2026-06-21)
**Fixed**: `degraded: true` was not persisted to the `citation_report` JSON stored in the `messages` table. When a user reloaded the page or switched notebooks, historical degraded (search-only) answers lost their "search only" badge. Fix: add `degraded: NotRequired[bool]` to `CitationReport` TypedDict (`citation.py`), set it in the degraded path of `ask()` (`qa.py`) and in `_h_ask_sse()` (`server.py`), and render it in `addMsg()` by checking `report.degraded` (`index.html`). The dead `#degBadge` header element (initialized hidden and never shown) is left in place but is no longer the primary indicator — per-message badges in the chat history are now the correct mechanism.

### v0.2.23 (2026-06-21)
**Fixed**: `_drain()` did not set `self.close_connection = True` when `Content-Length` exceeded the drain cap (`MAX_UPLOAD_BYTES + 65536`). In HTTP/1.1 keep-alive mode, undrained bytes would corrupt subsequent request parsing on the same connection. Fix: mark connection for close when the declared body size exceeds what we can drain.

**Fixed**: `config.port()` raised unhandled `ValueError` when `SHOIN_PORT` was set to a non-numeric value (e.g. `SHOIN_PORT=abc`), propagating through `_build_parser()` in `main()` as an uncaught exception. Fix: wrap in `try/except (ValueError, TypeError)` and fall back to `DEFAULT_PORT`.

### v0.2.22 (2026-06-21)
**Fixed**: `_dispatch()` returned HTTP 404 `ROUTE_NOT_FOUND` when a known path was accessed with an unsupported HTTP method (e.g. `DELETE /api/notebooks`). Per RFC 9110 §15.5.6, the correct status is 405 `METHOD_NOT_ALLOWED`. Fix: after the main route-matching loop, do a second pass checking whether any route's path pattern matches — if yes, return 405; if no path matches at all, return 404.

**Fixed (docs)**: CLAUDE.md incorrectly stated `SHOIN_EMBED_MODEL` defaults to empty (BM25-only). Actual default is `nomic-embed-text`; set it to empty string to disable embeddings.

**Fixed (docs)**: CLAUDE.md listed FTS5 triggers as `chunks_ai`, `chunks_ad`, `chunks_ad` (duplicate). Correct list is `chunks_ai` (after insert) and `chunks_ad` (after delete).

### v0.2.21 (2026-06-21)
**Fixed**: Deleting the last notebook left the header title, source list, chat, and studio showing stale content from the deleted notebook. `loadNotebooks()` entered the `!notebooks.length` branch without calling `renderNotebook()`. Fix: call `renderNotebook()` (with `cur=null`) and add an `else` branch in `renderNotebook()` that explicitly clears chat, studio, notes, question chips, and hides the source-empty indicator when `cur === null`.

**Fixed**: The degraded-mode badge (`#degBadge`, "検索のみ" / "search only") was only reset in the SSE `done` handler and was never cleared when switching notebooks. After switching away from a notebook whose last answer was degraded, the badge remained visible for the new (non-degraded) notebook. Fix: hide `#degBadge` at the start of `renderChatHistory()`.

### v0.2.20 (2026-06-21)
**Fixed**: `history_messages()` had a trailing-user guard (`while out and out[-1]["role"] == "user": out.pop()`) but no symmetric leading-assistant guard. When the `HISTORY_MESSAGES=6` window landed mid-pair (the paired user question is outside the window), the history started with an assistant message, giving the LLM the sequence `[system, asst, user, ...]` — protocol-unusual for OpenAI-compatible APIs. Fix: add `while out and out[0]["role"] == "assistant": out.pop(0)`.

### v0.2.19 (2026-06-21)
**Fixed**: `export_markdown()` lacked newline normalization on notebook name, source titles, source origins, and note titles. A title containing `\n` (e.g., from a source with a multiline HTML title) would silently produce two separate Markdown list items or headings instead of one. BibTeX (`_bib_escape`) and RIS (`_ris_escape`) already normalized newlines; added `_md_line()` helper to match.

**Fixed**: Unexpected exceptions in `_dispatch()` (e.g., `sqlite3.OperationalError: database is locked` after busy_timeout expires) were unhandled — the HTTP framework logged a traceback but sent no response, so the client received an abrupt connection close. Fix: add catch-all `except Exception` in `_dispatch()` that logs to stderr and returns HTTP 500 with error code `SYSTEM_INTERNAL_ERROR`.

### v0.1.37 → v0.1.55

### v0.1.55 (2026-06-14)
**Fixed**: `_post()` not using `errors="replace"` when decoding LLM response; non-UTF-8 bytes (Latin-1 error messages, binary junk) would raise `UnicodeDecodeError` instead of being caught and converted to `LLMError`. Discovered via Socratic question "do all 3 decode sites have consistent error handling?"

### v0.1.54 (2026-06-14)
**Fixed**: `adaptive_alpha()` stripping trailing punctuation then checking if query ends with `?` (unreachable code); English questions like "What is Shoin?" lost semantic-search boost. Use `rstrip(" \t\n")` before the endswith check.

### v0.1.53 (2026-06-14)
**Fixed**: `questions_cache` TOCTOU race when adding sources concurrently; older fingerprint could overwrite newer cache entry. Guard with fingerprint check before write.

### v0.1.52 (2026-06-14)
**Fixed**: `history_messages()` skipping cite-only assistant turns generated consecutive user messages, violating OpenAI API alternation. Add post-loop deduplication of same-role pairs.
**Refactored**: `_SENTENCE_SPLIT_RE` duplicated in chunk.py and citation.py; consolidate to single source of truth.

### v0.1.51 (2026-06-14)
**Fixed**: `_is_cjk_word()` treating 々 (iteration mark, U+3005) as punctuation, breaking "人々" queries. Add exception: 0x3005 stays part of CJK word runs.

### v0.1.50 (2026-06-14)
**Fixed**: `chat_stream()` silently swallowing `{"error": "..."}` SSE events from Ollama/llama.cpp. Check for `"error"` key before accessing `"choices"`.
**Fixed**: `_h_ask_sse()` overwriting partial LLM output with error prefix on mid-stream failure. Append instead of replace.
**Fixed**: `suggest_questions()` unreachable code after NFKC normalization (？→?, ！→!). Simplify to `rstrip("。．!?")`.

### v0.1.49 (2026-06-14)
**Fixed**: v0.1.48 regression: FTS5 generating trigrams like "書院。" including punctuation. Add `_is_cjk_word()` to exclude U+3000–U+303F.
**Fixed**: `_char_bigrams("")` returning `{""}` instead of `set()`, causing empty Hit to score 1.0 in MMR.

### v0.1.48 (2026-06-14)
**Fixed**: CJK symbol/punctuation block (U+3000–U+303F: 。、　) not included in `_CJK_RANGES`, so token estimation underestimated Japanese text. Add the block.
**Fixed**: `_SENTENCE_SPLIT_RE` not treating full-width semicolon ； as sentence boundary.

### v0.1.47 (2026-06-14)
**Fixed**: `_h_ask_sse()` saving empty assistant message when client disconnects before LLM sends any tokens. Add `if full:` guard.

### v0.1.46 (2026-06-14)
**Fixed**: `export_ris()` missing blank lines between RIS entries (2001 spec requires them). Use `"\n\n".join()`.

### v0.1.45 (2026-06-14)
**Fixed**: `_minmax()` returning all-1.0 when all scores are 0 (IDF=0, common words). Special case: if `lo < 1e-12` return 0.0.

### v0.1.44 (2026-06-14)
**Fixed**: `suggest_questions()` not stripping full-width list prefixes (１. ２） ３、). Apply NFKC normalization before lstrip.

### v0.1.43 (2026-06-14)
**Fixed**: `_SENTENCE_SPLIT_RE` in citation.py not including ； (full-width semicolon), causing sentence splitting to diverge from chunk.py.

### v0.1.42 (2026-06-14)
**Fixed**: `_CJK_RANGES` missing Thai/Lao/Myanmar/Khmer blocks, token estimation way off. Add U+0E00–0E7F, U+0E80–0EFF, U+1000–109F, U+1780–17FF.
**Fixed**: `_SENTENCE_SPLIT_RE` not treating ； as sentence end.

### v0.1.41 (2026-06-13)
**Fixed**: Timeout and connection refused mapped to same error code. Distinguish via `exc.reason` check for `TimeoutError`.

### v0.1.40 (2026-06-13)
**Fixed**: `add_message()` without existence check, FK constraint error uncaught. Add `store.get_notebook()` guard.

### v0.1.39 (2026-06-13)
**Fixed**: `add_studio_output()` same FK issue. Add notebook existence check.

### v0.1.38 (2026-06-13)
**Security**: `/sources` API accepting file paths as `target`, allowing SSRF to read `/etc/passwd`. Restrict to `http://` / `https://` URLs only.

### v0.1.37 (2026-06-13)
**Fixed**: `embed()` silently discarding embeddings when server returns fewer results than requested. Add length validation.

---

## Testing & Debugging

**Test Suites**: `tests/test_*.py` — unit tests for store, citation, Q&A, server SSE, Studio. Run `pytest tests/`.

**Debugging Aid** (implemented v0.2.129 — this line long documented the feature under the name `DEBUG=1` while the source never actually read any such variable; renamed to `SHOIN_DEBUG` for the same `SHOIN_`-namespace safety every other setting already has, avoiding collision with the many unrelated tools/CI systems that use the generic name `DEBUG` for their own purposes): Set `SHOIN_DEBUG=1` to print retrieval diagnostics to stderr from `search.py`'s `retrieve()`/`retrieve_multi()` — the query, extracted `-word` negations, BM25/vector hit counts, and for each of the final selected chunks: chunk/source id, fused score, raw BM25/cosine scores, and the detail dict (RRF ranks per list, the lexical-rerank weight — RRF replaced the older alpha-based fusion in v0.2.56, so there is no "fusion alpha" to print anymore). Useful when result relevance seems off.

**Common Issues**:
1. **Embeddings disappearing after model change**: Check `settings` table for `embed_model` value. If NULL or stale, run `shoin reindex <notebook_id>` to rebuild.
2. **BM25-only mode unexpectedly active**: Confirm `SHOIN_EMBED_MODEL` is not empty/whitespace. Use `(llm.embedding_model or "").strip()` check.
3. **SSE stream cuts off mid-token**: Check client logs for `ConnectionResetError`. Partial responses are now saved (v0.1.47+), but may appear incomplete in UI.
4. **Citation report shows empty `confirmed`/`misattributed` lists**: This is normal—means all citations are in the inconclusive (paraphrase) category. No error.

---

## Deployment Notes

**System Requirements**:
- Python 3.10+
- SQLite 3.34+ (for FTS5 trigram tokenizer)
- 8GB RAM minimum for 4B LLM + Shoin
- Local OpenAI-compatible endpoint (Ollama, llama.cpp, LM Studio)

**Configuration** via `~/.config/shoin/config.json` or environment variables:
- `SHOIN_LLM_URL`: Base URL (default `http://localhost:11434/v1`)
- `SHOIN_LLM_MODEL`: Generation model (default `qwen3:4b`)
- `SHOIN_EMBED_MODEL`: Embedding model (default `nomic-embed-text`; set to empty string to force BM25-only mode)
- `SHOIN_DATA_DIR`: SQLite path (default `~/.local/share/shoin`)
- `SHOIN_PORT`: HTTP port (default 7440, 127.0.0.1 only)
- `SHOIN_LANG`: UI language (ja/en)

**Single-Machine Deployment**: Shoin is designed for single-user, single-machine use. It binds to 127.0.0.1 and uses `ThreadingHTTPServer` for simplicity. Do not expose to untrusted networks without a reverse proxy (nginx with auth).

---

## Contributing & Philosophy

**AI エージェント向け作業指示書**: このリポジトリで作業するエージェントセッションは、モデルの能力帯に応じた指示書を最初に読むこと — `docs/agents/opus.md`（Opus級: migration/検索スコアリング/引用検証セマンティクス等の高リスク領域まで自律作業可）/ `docs/agents/sonnet.md`（Sonnet級: 加算的タスクに限定、禁止領域と「Noted (not actioned)」エスカレーション手順を規定）。着手候補の台帳は `docs/product-review.md` の改善案バックログ。

**Design Principles**:
1. **Zero External Dependencies**: urllib only. No requests, no numpy, no PyTorch. Slim binary, easy to audit.
2. **Lightweight First**: Target 4–8B LLMs explicitly. Trade some capability for speed and memory.
3. **Local by Default**: All data stays on disk. No cloud telemetry, no opt-out required.
4. **Graceful Degradation**: Search works without LLM. Studio outputs have fallback text. History_messages() survives malformed chats.
5. **Citation Integrity**: Three-layer verification, but only assert high-confidence findings. Silence when unsure, don't accuse.

**Code Style**: 
- Type hints everywhere (Python 3.10+ syntax).
- Dataclasses for domain objects (frozen where possible).
- Functions, not classes, for utility logic.
- Docstrings explain *why* not just *what*.
- Test file per module (`test_X.py` for `X.py`).

**Adding Features**: 
- Start by reading `Plan.md` and relevant CHANGELOG entries to understand design decisions.
- Add migrations in `store.py` MIGRATIONS list (append-only, never edit shipped entries).
- Update CLI, server routes, UI in tandem.
- Add i18n strings to `_STRINGS` dicts in affected modules.
- Run tests: `pytest tests/` and manual UI check.

---

End of guide. For detailed architecture decisions, see `Plan.md` and `docs/spec.md`. For security model, see `docs/adr/ADR-001-ssrf-ip-pinning.md`. For changelog granularity (bug-by-bug): `CHANGELOG.md` covers v0.1.0–v0.1.55; it was not kept up to date after that point, so v0.1.56 onward is recorded only in this file's own "Version History" section above (and in `git log`, which has a one-line summary per version).
