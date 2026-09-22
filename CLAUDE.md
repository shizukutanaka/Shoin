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

### Citation Verification: Machine Checks

Citation hallucination (fabricated quotes, wrong numbers, unsupported assertions) is one of the most user-visible LLM failure modes. Shoin runs dependency-free, LLM-free checks on every assistant response and Studio output:

**1. Range Check** (`validate_citations`): Detect `[S99]` when only 5 sources exist. Out-of-range numbers are the narrowest, highest-confidence hallucination signal.

**2. Grounding Confirmation** (`verify_grounding`): A cited sentence's wording is compared to the source text using character-bigram overlap. When overlap >= 30% (CONFIRM_MIN, calibrated for CJK), the citation is flagged `confirmed` — strong positive evidence the claim is lexically supported.

**3. Mis-numbering Detection** (`verify_grounding`): When a sentence does *not* match its cited source but *does* strongly match a *different* source (with a 20% gap margin, MISMATCH_GAP), the citation number is flagged `misattributed` — the model likely cited the wrong source.

**4. Uncited-Assertion Detection** (`uncited_sentences`, v0.2.65): Checks 2 and 3 only ever examine sentences that *already* carry a citation. A hallucinated or simply unsupported claim with *zero* citations anywhere in it is invisible to those checks — this was docs/product-review.md's top-priority open gap. `uncited_sentences()` scans for sentences with no `[S#]` marker, resolving the common trailing-citation pattern ("Sentence. [S1]") the same way `verify_grounding()` does, and excludes trivial filler and explicit "not in the source" disclaimers (the *correct* response to missing facts, not an unsupported assertion).

**5. Numeric-Consistency Check** (`numeric_mismatches`, v0.2.184): a cited claim asserting a digit string (≥2 digits or a decimal) the source never contains is flagged — the fabricated-statistic failure shape the citation literature flags as dominant (arXiv:2510.20303).

**6. Quote-Mismatch Check** (`quote_mismatches`, v0.2.187): a 「…」/"…" span cited to S_n but appearing verbatim in a *different* source is exact-string proof of misattribution — folded into `misattributed`.

**7. Degeneration Check** (`degenerate_spans`, v0.2.188): verbatim ≥3× repetition in the answer itself — the repeat-loop failure small local LLMs are prone to; the only check that inspects the answer rather than its citations.

**8. Unit-Consistency Check** (`unit_mismatches`, v0.2.190): a number present in the source but asserted under an incompatible unit ("100km" vs "100m", "100億円" vs "100万円") — invisible to check 5's presence test.

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
- `term_variants()`: width/script spellings of a term (half/full-width kana, fullwidth ASCII) so they retrieve one another (v0.2.144)
- `rrf_fuse()` / `rrf_fuse_lists()`: Reciprocal Rank Fusion — the ONLY fusion path (v0.2.56; the old convex-combination `fuse()`/`adaptive_alpha()` were deleted in v0.2.150)
- `_minmax()`: min-max normalization (rescales RRF scores to [0,1] before the lexical rerank)
- `rerank()` / `mmr()`: lexical rerank then diversity (lexical bigram overlap)
- `retrieve()`: orchestrates BM25 + vector + RRF fusion + rerank + MMR

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

## Version History

The detailed, bug-by-bug version history (v0.1.37 →, growing) moved to `docs/HISTORY.md`
at v0.2.172 — measured, the 174 entries present at that point were 344.5KB of this file's
370KB (93%), and `CLAUDE.md` is loaded in full as project instructions on every session
touching this repo, regardless of task relevance. `docs/HISTORY.md` explains why in full
and holds every entry
verbatim; nothing was deleted, only un-inlined. Read it directly, or `grep` it, when you
need precedent for a specific bug class, design decision, or file's own change history —
the same way this project's own audit rounds have always searched it (`grep -n
"_CJK_RANGES" CLAUDE.md`-style lookups work identically against `docs/HISTORY.md`).

**Append new entries to the top of `docs/HISTORY.md`'s Version History section, not here.**
Update only this line's version range and the pin below.

Current version: **v0.2.225** — see `docs/HISTORY.md` for what changed and why.

---

End of guide. For detailed architecture decisions, see `Plan.md` and `docs/spec.md`. For security model, see `docs/adr/ADR-001-ssrf-ip-pinning.md`. For the full bug-by-bug changelog: `CHANGELOG.md` covers v0.1.0–v0.1.55 (frozen there deliberately, see its own header note); v0.1.56 onward lives in `docs/HISTORY.md` (linked from this file's "Version History" section above), and `git log` carries a one-line summary per version throughout.
