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
- FTS5 MATCH expression is OR-joined for recall; BM25() scoring ranks denser matches higher
- Fallback: LIKE scan for short queries (< 3 chars) when FTS5 index misses them, limited to 2000 rows

**Vector Search** (optional):
- Each chunk is embedded via `ChatBackend.embed_one(text)` when embeddings are enabled
- Cosine similarity is computed in-process over notebook chunks (local scale, no external API)
- Normalizes NaN/Inf results to 0.0 (guards against corrupted embeddings from broken endpoints)

**Fusion** (Convex Combination):
- Linear combination: `score = alpha * vec_score + (1-alpha) * bm25_score`
- Adaptive alpha: natural-language queries (ending in `？`, `?`, or `か`) get higher alpha (+0.15) to prioritize semantic search
- Min-max normalization accounts for different score ranges per query (scores are local, not global)

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
- Very large notebooks (500+ sources) retrieve only TOP_K=10 sources, potentially missing nuanced cross-source synthesis.

**Mitigation**: The design is intentional. Shallow context encourages focused, specific questions rather than open-ended exploration. For exploratory work, Studio outputs (briefing, timeline, mindmap) pre-synthesize the sources into digests.

### No Batch Embeddings API Support

**Problem**: `ChatBackend.embed_one(text)` embeds a single chunk at a time. Ingest of a 100-chunk source triggers 100 HTTP requests to the embeddings endpoint (Ollama `/api/embeddings`, llama.cpp `/embeddings`).

**Impact**: Ingest of large PDFs is slow (~2–5 seconds per source on typical hardware). For users adding dozens of sources in a session, this compounds.

**Why Not Fixed**: Ollama and llama.cpp have different batch API signatures and conventions. Shoin prioritizes simplicity and broad compatibility (works with any OpenAI-compatible endpoint). A batch API would require vendor detection or optional configuration.

**Workaround**: Disable embeddings (leave `SHOIN_EMBED_MODEL` unset) if ingest speed matters more than semantic search.

### UI State Management Could Be More Robust

**Problem** (fixed in v0.2.21): SSE StrockError during streaming could orphan messages (user query saved, assistant response lost). History reconstruction would see dangling user turns and output malformed message sequences to the LLM.

**Fix** (`history_messages`, v0.1.52 onward):
- Catch connection errors during SSE and save partial responses.
- Deduplicate consecutive same-role turns: if a user turn is orphaned (no assistant reply), the next message processing removes it.

**Remaining**: Concurrent notebook switches during question-fetch could display wrong recommendations. This is a race condition in the browser (not the server). Low priority since the UI is single-user.

### No Distributed Tracing

**Problem**: In production deployments, debugging slow queries across the retrieval → LLM → response pipeline is opaque. No request ID, no server-side timing logs.

**Why Not Fixed**: Shoin targets single-machine deployment. Logging would bloat the binary and add I/O overhead. Users who debug often look at Flask/Django logs; Shoin's minimal HTTP server doesn't have equivalent introspection.

**Workaround**: Add `DEBUG=1` environment variable to print timing/retrieval stats to stderr.

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
- Subcommands: notebook, add, ask, studio, questions, export, serve, reindex, note (add/list/delete), source (delete/rename/refresh) (v0.2.68)
- Maps to the same backends (Store, LLM, Q&A) as the web server
- Internationalization: respects SHOIN_LANG for output

**`export.py`** (Format Export)
- Formats: Markdown (full notebook dump), BibTeX, RIS
- Handles malformed JSON in citation_report gracefully
- Escapes special characters (backslash, newlines) per format spec
- `_status_line()`: renders confirmed/misattributed/uncited/degraded status inline for chat messages and Studio outputs in the Markdown export, so citation verification survives outside the app (v0.2.66)

---

## Version History: v0.1.37 → v0.2.91

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

**Debugging Aid**: Set `DEBUG=1` to see retrieval stats (BM25 scores, vector scores, fusion alpha) printed to stderr. Useful when result relevance seems off.

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
