# Shoin (書院)

**Language**: [日本語](README.md) | English

Your own private study, kept on your machine. A NotebookLM alternative that shelves your documents and answers with citations — and never sends any data outside.

## Features

- **Notebooks**: Bundle PDF / Markdown / TXT / HTML / WebVTT / URL sources and get answers grounded strictly in that set
- **Cited answers + mechanical citation verification**: Click a `[S1]` / `[S2]` in an answer to jump to the original passage — the cited chunk itself is highlighted, with its section name and detection path (full-text / semantic). The checks cover (1) source numbers that don't exist, (2) a "grounding confirmed" mark from lexical support, (3) wrong-number detection via a strong match to a *different* source (with the likely correct source suggested), and (4) uncited assertions (a sentence that matches a source is distinguished as a missing citation, again with the likely source). Numbers (magnitude shorthand / kanji numerals / spelled-out English / 歩合 / rate notation / era-name years), units, negation flips, tampered verbatim quotes, self-contradictions inside an answer, and repetition degeneration are also machine-checked. Knowing the limits of lexical signals (a synonym paraphrase cannot be judged), the checks only speak up when they are confident, so a correct answer is never falsely accused. No LLM required, zero dependencies
- **Studio outputs**: Generate briefings / study guides / FAQs / timelines / mind maps from your sources. Each output can be saved as a note in one click
- **Notes**: Free-form memos per notebook. Studio outputs can also be turned into notes
- **Export**: A whole notebook as Markdown (notes, Studio outputs, and chat history included); bibliographies as BibTeX / RIS
- **Fully local**: Documents, questions, and generated content are never sent to an external service. Works offline
- **Lightweight**: Designed for small local LLMs (Qwen3-4B class, ≤8B) on machines with around 8 GB of RAM. No Docker, single process

## Installation

```bash
# From source (currently the only method; verified to work)
git clone https://github.com/shizukutanaka/Shoin.git && cd Shoin
pip install .

shoin serve              # then open http://localhost:7440 in your browser
```

Without `pip install`, you can also run it straight from the source tree as `python -m shoin serve` (since v0.2.459 both delegate to the same entry point).

```bash
# Single-file .pyz (no install; only Python 3.11+ required)
# A prebuilt shoin.pyz is attached to GitHub Releases (v0.2.746)
# To build it from source:
python3 scripts/build_pyz.py        # produces dist/shoin.pyz
python3 dist/shoin.pyz serve        # the full feature set in one file
```

> **Not on PyPI yet.** `pip install shoin` does not work yet (publishing
> requires the maintainer's credentials). When installing directly from the
> repository, **name the ref explicitly** — the default branch may point at
> an older version:
>
> ```bash
> pip install "git+https://github.com/shizukutanaka/Shoin.git@main"
> ```

Prerequisite: an OpenAI-compatible local endpoint — [Ollama](https://ollama.com), llama.cpp, or LM Studio.

```bash
ollama pull qwen3:4b              # for generation (recommended)
ollama pull nomic-embed-text      # for embeddings (optional; BM25 search works without it)
```

If you are not sure whether a local LLM is ready, the first-time setup wizard will guide you:

```bash
shoin setup                       # detect an LLM → save the config → try a cited answer on a sample document
```

The wizard probes, in order, your current configuration, Ollama, LM Studio, and llama.cpp for a local endpoint, and saves the one that responds to `~/.config/shoin/config.json`. If none is found, it prints the reason and platform-specific install steps and exits (it never starts a download or installation on its own). Once connected, it prepares a notebook called "Shoin sample" containing only a sample document, so you can experience a cited answer on the spot. `--url` specifies any OpenAI-compatible endpoint, `--no-save` skips saving, and `--skip-demo` skips the trial. If you start `shoin serve` with no configuration file, a one-line pointer to the wizard is printed before startup as well.

## Usage

```bash
shoin setup                           # First-time setup (detect LLM → save config → sample trial)
shoin serve                           # Start the Web UI
shoin notebook new "Research notes"   # The CLI can do everything too
shoin notebook duplicate 1            # Duplicate a notebook (sources/chunks/notes all copied)
shoin notebook merge 1 2              # Merge nb2 into nb1 (nb2 is archived to trash)
shoin notebook settings 1 top_k=4 source_text_tokens=512  # Per-notebook retrieval overrides (unset = global default)
shoin add 1 ./paper.pdf https://example.com/article
shoin add 1 ~/papers/                 # add a whole folder (re-run: adds new files, refreshes changed ones)
shoin source missing 1 ~/papers/      # review source IDs and paths for missing originals
shoin ask 1 "What is this paper's main contribution?"
shoin studio 1 study_guide
shoin health                          # Check configuration and LLM reachability (headless diagnostics)
shoin eval 1 cases.json               # Measure retrieval quality on your own documents (recall/MRR)
# Also: search / questions / messages / note / source (rename/refresh/refresh-all/weight/meta/delete) / chunk edit / reindex
#       stats (usage metrics; the same counters are served at GET /api/metrics) / trash (list|restore|purge|empty — undo for deleted notebooks) / backup
#       vacuum (explicitly merge the FTS index and return deleted space to the OS — physically shrinks the DB) / check (DB diagnostics — physical integrity + missing embeddings, rc=1 when corrupt)
#       export --format tree → shoin import moves a notebook to another machine/DB (shoin-nb-tree-v1)
```

The Web UI has three panes: left = sources, center = chat, right = Studio and notes.
Studio mind maps display a diagram when the generated list has a valid hierarchy. The original text and clickable citations stay below the diagram.

`source missing` checks original paths under the selected folder without changing the index.
Check that any external drive is connected before moving an unwanted source to trash with
`shoin source delete <source_id>`. To undo, find its trash ID with `shoin trash list` and run
`shoin trash restore <trash_id>`.

### Import YouTube captions

Add a WebVTT (`.vtt`) export from YouTube Studio or another caption editor using the
Web UI file picker or `shoin add 1 ./captions.vtt`. The caption text is searchable
and citable without timestamps or formatting tags. Shoin does not fetch captions
from a video URL.

### Audio overview (CLI)

Install [Piper](https://github.com/OHF-Voice/piper1-gpl) so `piper` is on PATH, and
place a voice matching the briefing language (`.onnx` and its matching `.onnx.json`) on your machine.

```bash
shoin studio 1 briefing
shoin audio 1 --model ./voice.onnx --output ./briefing.wav
```

For Japanese narration with [VOICEVOX](https://voicevox.hiroshiba.jp/), start its
engine at `127.0.0.1:50021` and pass a style ID from its `/speakers` response
(the example uses style 3):

```bash
shoin audio 1 --voicevox-speaker 3 --output ./briefing-ja.wav
```

The VOICEVOX engine may include briefing text in its own local HTTP access log.

`audio` narrates the latest saved briefing offline without calling the LLM again.
Citation labels are omitted from the narration but remain in the original Studio output.
Shoin does not download the voice or overwrite an existing output file.

To download a briefing as WAV from Studio in the Web UI, start Shoin with a
local voice, for example `shoin serve --piper-model ./voice.onnx` or
`shoin serve --voicevox-speaker 3`. Open a notebook with a saved briefing and
select **Download audio (WAV)**. The button is hidden unless audio is enabled.

### Search behavior (Japanese text)

Hybrid search: BM25 (FTS5 trigram) + vectors. Variations in Japanese orthography are absorbed automatically at search time:

- **Width and glyph variants match each other**: full-width kana (データベース) / half-width kana (ﾃﾞｰﾀﾍﾞｰｽ, common in legacy cp932 data) / full-width alphanumerics (ＧＰＵ, ２０２４) / ASCII (GPU) all match. The indexed text is kept verbatim; the query side is expanded into variants (v0.2.144).
- **Katakana ↔ hiragana**: bridges the same word written in different scripts, e.g. コード / こーど (v0.2.42).
- **Exclusion search**: prefix a term with `-` to exclude chunks containing it (e.g. `Python -legacy`). Applies to both body text and section headings.
- With no embedding model configured, it runs on BM25 alone (a first-class mode, not a fallback).

## Configuration

Environment variables, or `~/.config/shoin/config.json` (environment variables win):

| Variable | Default | Description |
|----------|---------|-------------|
| `SHOIN_LLM_URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint. When a non-local endpoint is set, a warning is shown at startup / in `shoin health` / in `/api/health` (`llm_external`) — your documents will be sent externally. Credentials embedded in the URL (user:pass@) are stripped when displayed, so secrets never appear |
| `SHOIN_LLM_MODEL` | `qwen3:4b` | Generation model |
| `SHOIN_LLM_API_KEY` | (disabled) | When set, adds `Authorization: Bearer <key>` to every LLM request. For gateways that require authentication (vLLM + proxy, hosted OpenAI-compatible endpoints). When unset, the header itself is not sent. Combined with an `http://` external endpoint, a plaintext-transfer warning is shown at startup (https:// recommended) |
| `SHOIN_EMBED_MODEL` | `nomic-embed-text` | Embedding model (empty = BM25 only) |
| `SHOIN_DATA_DIR` | `~/.local/share/shoin` | Where the SQLite data lives |
| `SHOIN_PORT` | `7440` | Listen port (bound to 127.0.0.1 only) |
| `SHOIN_LANG` | `ja` | UI language (ja/en). Applies to the CLI, exports, and the Web UI. For the Web UI it is only the default on first display — once you press the language toggle in the header, the browser remembers your choice and it takes precedence from then on (v0.2.177) |
| `SHOIN_MULTI_QUERY` | (disabled) | `1` enables multi-query RAG-Fusion search. The question is expanded by the LLM into several paraphrases and the results are fused with RRF (better recall; one extra LLM call per ask) |
| `SHOIN_EMBED_BATCH` | `16` | Batch size for embedding requests (tune to your endpoint's capacity) |
| `SHOIN_LLM_RETRIES` | `2` | Extra retries for chat/embed calls (0–5). Only transient transport failures (connection refused, timeouts, …) are retried with exponential backoff; HTTP errors and malformed responses are not. After retries are exhausted by a connection failure, only one attempt is made until the endpoint recovers, then the normal budget is restored. 0 disables; invalid values fall back to the default |
| `SHOIN_CHUNK_TOKENS` | `512` | Target tokens per chunk. Change it and measure the effect on your own documents with `shoin eval` (takes effect from the next ingest/reindex) |
| `SHOIN_CHUNK_OVERLAP` | `64` | Overlap tokens between chunks (must be below the chunk size; negative or oversized values fall back to the default). The benefit of overlap depends on the document, so measuring is recommended |
| `SHOIN_DEBUG` | (disabled) | `1` prints retrieval diagnostics (BM25/vector hit counts, RRF ranks, final scores) to stderr |
| `SHOIN_LOG_JSON` | (disabled) | `1` emits ingest/answer events as JSON Lines to stderr (`{"ts","event",…}`; `source_indexed` / `ask_completed` — IDs, counts, and ms only, never body text) |
| `SHOIN_EXPORT_CREDIT` | `1` (enabled) | `0` omits the one-line credit appended to Markdown/BibTeX exports ("Exported with Shoin — repository URL"). The line is plain text, so you may also delete it from the output. Machine-readable formats such as RIS and the JSON tree do not carry it |
| `SHOIN_THEME_CSS` | `~/.config/shoin/theme.css` | Path to a user theme CSS. All colors are CSS variables, so overriding `:root{--washi:…}` is enough to restyle. The Web UI loads it via `/api/theme.css` (a missing file or one over 256 KiB degrades to an empty stylesheet) |

Example `cases.json` for `shoin eval` — run it before and after a configuration change (e.g. `SHOIN_MULTI_QUERY=1`) to compare the effect **on your own documents**, not on literature numbers:

```json
[
  {"q": "How is washi made?", "sources": [1]},
  {"q": "How does letterpress printing work?", "sources": [2, 3]}
]
```

Baseline comparison: save a reference run with `--save`, then show the diff with `--diff`.
The diff is aggregated over the questions common to both runs; added and removed questions are listed separately
(questions are matched by their text, so reordering or editing the cases file produces no false regressions).
Duplicate questions are paired in order of appearance.

`--gen` generates a case template from a notebook (one case per source that has chunks,
with the real `sources` ids — a template meant to be hand-edited):

```bash
shoin eval 1 --gen > cases.json                # generate a template, then edit it
shoin eval 1 cases.json --save before.json     # before the configuration change
shoin eval 1 cases.json --diff before.json     # after the change: shows recall/MRR deltas
```

Example `config.json`:

```json
{
  "SHOIN_LLM_MODEL": "qwen3:8b",
  "SHOIN_EMBED_MODEL": "ruri-v3"
}
```

## Performance

Measured values (768-dimension embeddings, development machine. LLM generation time is extra and usually larger):

| Notebook size | One search | Notes |
|---------------|-----------|-------|
| 1,000 chunks | ~20 ms | |
| 5,000 chunks | ~120 ms | |
| 20,000 chunks | ~380 ms | |
| 50,000 chunks (limit) | ~0.94 s | `MAX_CHUNKS_PER_NOTEBOOK` |

| Ingest (1 source) | Time |
|---|---|
| 1 MB of Markdown | ~0.3 s |
| 5 MB | ~1.5 s |
| 9.4 MB (just under the 10 MB limit) | ~3.0 s |

- Search time is roughly proportional to the chunk count. **To keep responses conversational (under 100 ms), split notebooks at around 5,000 chunks** — several smaller notebooks by topic are faster, and citations are easier to follow, than one large book.
- Citation verification takes under 15 ms even for 130k characters and 8 sources, and loading a notebook takes about 9 ms with 300 sources and 200 messages. Neither is perceptible.
- Memory does not depend on the query's chunk count (only the top-k are materialized).

## Supported Models

Any local model that speaks an OpenAI-compatible API. Verified recommendations:

| Use | Model | RAM guide |
|-----|-------|-----------|
| Generation (recommended) | Qwen3 4B / 8B | 4–6 GB |
| Generation (lightweight) | Gemma 3 4B / Phi-4-mini 3.8B | 3–4 GB |
| Embeddings | nomic-embed-text | 0.3 GB |
| Embeddings (Japanese-specialized) | ruri-v3 | 0.5 GB |

## Development

```bash
pip install -e . && pip install -r requirements-dev.txt    # dependencies (pinned versions)
./scripts/verify.sh                                    # all gates (lint/types/tests+coverage/secret scan)
git config core.hooksPath .githooks                    # run automatically before push (once)
```

`scripts/verify.sh` runs the same checks as `ci/ci.yml` in one command.
Enabling `.githooks/pre-push` runs it before every push and stops the push on failure
(`git push --no-verify` in an emergency). This satisfies the requirement that
"everything is verified automatically before it lands" even in an environment
without GitHub Actions permissions — see `ci/README.md` for details.

### Versioning and releases

- The source of truth for the version is `project.version` in `pyproject.toml`;
  the current version is **v0.2.746**. `shoin.config.VERSION` and the
  `Current version` in `CLAUDE.md` point at the same value (pinned by the
  `test_version_markers_agree` test).
- Release tags are `v<version from pyproject.toml>` (e.g. `v0.2.746`).
- Tags are cut at release time. The current version (v0.2.746) is tagged, and
  the prebuilt shoin.pyz is published on GitHub Releases. Older tags
  (`v0.2.474` and the like) do not indicate the current version — install the
  latest from the Release's shoin.pyz or from source (see Installation).

## Support / Commercial support

Shoin is MIT-licensed and free forever, with no feature gating.
Do not paste document contents or confidential company information into public issues.

## License

MIT
