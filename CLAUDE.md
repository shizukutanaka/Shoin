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

### Citation Verification: Three-Layer Machine Checks

Citation hallucination (fabricated quotes, wrong numbers) is one of the most user-visible LLM failure modes. Shoin runs three dependency-free, LLM-free checks on every assistant response and Studio output:

**1. Range Check** (`validate_citations`): Detect `[S99]` when only 5 sources exist. Out-of-range numbers are the narrowest, highest-confidence hallucination signal.

**2. Grounding Confirmation** (`verify_grounding`): A cited sentence's wording is compared to the source text using character-bigram overlap. When overlap >= 30% (CONFIRM_MIN, calibrated for CJK), the citation is flagged `confirmed` — strong positive evidence the claim is lexically supported.

**3. Mis-numbering Detection** (`verify_grounding`): When a sentence does *not* match its cited source but *does* strongly match a *different* source (with a 20% gap margin, MISMATCH_GAP), the citation number is flagged `misattributed` — the model likely cited the wrong source.

**Key Design Decision**: Lexical overlap is asymmetric. High overlap reliably *confirms* support. Low overlap is inconclusive—a correct synonym paraphrase and a true misattribution both score ~0. So the checks only *assert* what they can stand behind (confirmation, or a wrong number) and *stay silent otherwise* rather than falsely accusing a correctly paraphrased answer. No aggregate grounding score is emitted; the `confirmed` and `misattributed` lists are the complete signal. See CHANGELOG v0.1.4 for the design rationale.

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
- `make_report()`: construct CitationReport with confirmed/misattributed lists
- `CitationReport` TypedDict: cited, invalid, coverage, source_map, source_id_map, confirmed, misattributed

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
  - GET `/api/notebooks/{id}/messages` → chat history
  - POST `/api/notebooks/{id}/ask` → SSE stream (delta + meta + done)
  - GET/POST `/api/health` → LLM status, embedding model
- SSE Streaming: sends meta event (with citation report skeleton), delta events (tokens), done event (final report + status)
- `_h_ask_sse()`: manages streaming, catches BrokenPipeError/ConnectionResetError, saves partial responses

**`cli.py`** (Command-Line Interface)
- Subcommands: notebook, add, ask, studio, questions, export, serve, reindex
- Maps to the same backends (Store, LLM, Q&A) as the web server
- Internationalization: respects SHOIN_LANG for output

**`export.py`** (Format Export)
- Formats: Markdown (full notebook dump), BibTeX, RIS
- Handles malformed JSON in citation_report gracefully
- Escapes special characters (backslash, newlines) per format spec

---

## Version History: v0.1.37 → v0.2.50

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

End of guide. For detailed architecture decisions, see `Plan.md` and `docs/spec.md`. For security model, see `docs/adr/ADR-001-ssrf-ip-pinning.md`. For changelog granularity (bug-by-bug, v0.1.0 onward), see `CHANGELOG.md`.
