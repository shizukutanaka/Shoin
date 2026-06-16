"""Phase 3 tests: studio outputs, export, ingestion pipeline, CLI."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shoin import cli  # noqa: E402
from shoin.export import export  # noqa: E402
from shoin.llm import LLMError  # noqa: E402
from shoin.pipeline import _embed_chunks, index_source, reindex_notebook  # noqa: E402
from shoin.store import Store, StoreError  # noqa: E402
from shoin.studio import KINDS, generate, overview_hits, suggest_questions  # noqa: E402


class FakeLLM:
    """Deterministic backend: canned chat reply + optional failing batch embed."""

    def __init__(
        self,
        reply: str = "要点 [S1]。",
        embedding_model: str = "",
        fail_embed_after: int | None = None,
        chat_error: bool = False,
    ) -> None:
        self.reply = reply
        self.embedding_model = embedding_model
        self.fail_embed_after = fail_embed_after
        self.chat_error = chat_error
        self.embed_calls = 0
        self.chat_prompts: list[str] = []

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.2) -> str:
        if self.chat_error:
            raise LLMError("SYSTEM_SERVICE_UNAVAILABLE", "down")
        self.chat_prompts.append(messages[-1]["content"])
        return self.reply

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        if self.fail_embed_after is not None and self.embed_calls > self.fail_embed_after:
            raise LLMError("LLM_HTTP_ERROR", "embed endpoint down")
        return [[1.0, 0.0, 0.0] for _ in texts]

    def embed_one(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


def seed_notebook(store: Store, n_sources: int = 2, chunks_per_source: int = 4) -> int:
    nb = store.create_notebook("研究")
    for s in range(n_sources):
        src = store.add_source(nb.id, "file", f"資料{s + 1}", f"/tmp/s{s}.txt", f"sha{s}")
        store.add_chunks(
            src.id, [f"資料{s + 1}の段落{c}。内容テキスト。" for c in range(chunks_per_source)]
        )
    return nb.id


class StudioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.nb = seed_notebook(self.store)

    def tearDown(self) -> None:
        self.store.close()

    def test_overview_hits_covers_all_sources(self) -> None:
        hits = overview_hits(self.store, self.nb, per_source=3)
        self.assertEqual(len(hits), 6)  # 2 sources × 3 sampled chunks
        self.assertEqual(len({h.source_id for h in hits}), 2)

    def test_overview_hits_equidistant_spans_full_document(self) -> None:
        """Long sources: sampled chunks must include content from end, not just start."""
        store = Store(":memory:")
        nb = store.create_notebook("n")
        src = store.add_source(nb.id, "file", "長文資料", "/t", "h0")
        texts = [f"段落{i}" for i in range(10)]  # 10 chunks, seq 0–9
        store.add_chunks(src.id, texts)
        hits = overview_hits(store, nb.id, per_source=3)
        seqs = [h.text for h in hits]
        # Must include first and last chunk, not [段落0, 段落1, 段落2]
        self.assertIn("段落0", seqs)
        self.assertIn("段落9", seqs)
        self.assertNotEqual(seqs, ["段落0", "段落1", "段落2"])
        store.close()

    def test_generate_persists_with_citation_report(self) -> None:
        llm = FakeLLM(reply="ブリーフィング [S1] と [S2]。")
        result = generate(self.store, llm, self.nb, "briefing")
        self.assertEqual(result.report["cited"], [1, 2])
        self.assertEqual(result.report["invalid"], [])
        rows = self.store.latest_studio_outputs(self.nb)
        self.assertEqual(len(rows), 1)
        stored = json.loads(rows[0]["citation_report"])
        self.assertEqual(stored["cited"], [1, 2])

    def test_generate_flags_invalid_citation(self) -> None:
        llm = FakeLLM(reply="正当 [S1]。捏造 [S9]。")
        result = generate(self.store, llm, self.nb, "faq")
        self.assertEqual(result.report["invalid"], [9])

    def test_generate_rejects_unknown_kind(self) -> None:
        with self.assertRaises(StoreError) as ctx:
            generate(self.store, FakeLLM(), self.nb, "poem")
        self.assertEqual(ctx.exception.code, "STUDIO_KIND_INVALID")

    def test_generate_empty_notebook(self) -> None:
        empty = self.store.create_notebook("空")
        with self.assertRaises(StoreError) as ctx:
            generate(self.store, FakeLLM(), empty.id, "briefing")
        self.assertEqual(ctx.exception.code, "NOTEBOOK_EMPTY")

    def test_all_kinds_have_instructions(self) -> None:
        llm = FakeLLM(reply="本文 [S1]。")
        for kind in KINDS:
            result = generate(self.store, llm, self.nb, kind, persist=False)
            self.assertEqual(result.kind, kind)

    def test_suggest_questions_parses_lines(self) -> None:
        # NFKC normalization converts full-width ？ → ASCII ? in output questions.
        llm = FakeLLM(
            reply="1. 目的は何か？\n- 仕組みはどう動きますか?\n* 装飾のみのノイズ行\n結論として要約\n2. 制約は何か"
        )
        qs = suggest_questions(self.store, llm, self.nb)
        self.assertEqual(qs, ["目的は何か?", "仕組みはどう動きますか?", "制約は何か"])

    def test_suggest_questions_accepts_ka_with_trailing_period(self) -> None:
        """LLMs often append 。 even with 'no decoration' instructions — must not drop."""
        llm = FakeLLM(reply="この書院はどう動くのか。\n内容について説明します。")
        qs = suggest_questions(self.store, llm, self.nb)
        self.assertEqual(qs, ["この書院はどう動くのか。"])

    def test_suggest_questions_accepts_ka_with_fullwidth_period(self) -> None:
        """Fullwidth period ． (U+FF0E) becomes ASCII . after NFKC; rstrip must still strip it."""
        # Before the fix, rstrip("。．!?") contained U+FF0E (fullwidth period) but not
        # U+002E (ASCII period).  After NFKC normalization the string contains U+002E,
        # so the trailing period was never stripped and the question was silently dropped.
        llm = FakeLLM(reply="この書院はどう動くのか．\n内容について説明します。")
        qs = suggest_questions(self.store, llm, self.nb)
        # q is the NFKC-normalised form (fullwidth period → ASCII period); still kept in output.
        self.assertEqual(qs, ["この書院はどう動くのか."])

    def test_suggest_questions_strips_fullwidth_list_prefixes(self) -> None:
        """CJK-first LLMs often emit １. or ２） prefixes — NFKC normalization must strip them."""
        llm = FakeLLM(
            reply="１. 目的は何か？\n２） 仕組みはどう動きますか？\n３、制約は何か"
        )
        qs = suggest_questions(self.store, llm, self.nb)
        # Full-width numbers/punctuation must be stripped; bare question text remains.
        # NFKC also converts full-width ？ → ASCII ?, so check for ASCII ?.
        self.assertIn("目的は何か?", qs)
        self.assertIn("仕組みはどう動きますか?", qs)
        self.assertIn("制約は何か", qs)

    def test_suggest_questions_preserves_digit_leading_questions(self) -> None:
        """Questions that legitimately start with digits must not be corrupted.

        lstrip("0123456789...") was used before v0.1.66 and stripped any leading
        digit, turning "2024年の出来事は？" into "年の出来事は？".  The regex
        replacement only strips recognised list-prefix patterns (e.g. "1. ", "3、")
        so questions about years or 3D/IPv6 topics survive intact.
        """
        llm = FakeLLM(reply="1. 目的は何か？\n2024年の出来事は？\n3Dモデリングとは何ですか？")
        qs = suggest_questions(self.store, llm, self.nb)
        # numbered prefix must be stripped from first question
        self.assertIn("目的は何か?", qs)
        # year-leading question must not have its digits stripped
        self.assertIn("2024年の出来事は?", qs)
        # alphanumeric-code question must survive intact
        self.assertIn("3Dモデリングとは何ですか?", qs)

    def test_suggest_questions_empty_notebook(self) -> None:
        empty = self.store.create_notebook("空")
        self.assertEqual(suggest_questions(self.store, FakeLLM(), empty.id), [])

    def test_suggest_questions_llm_unavailable_returns_empty(self) -> None:
        """DoD: suggest_questions degrades gracefully when LLM is down (graceful degradation)."""
        self.assertEqual(suggest_questions(self.store, FakeLLM(chat_error=True), self.nb), [])

    def test_generate_missing_notebook_raises(self) -> None:
        with self.assertRaises(StoreError) as ctx:
            generate(self.store, FakeLLM(), 99999, "briefing")
        self.assertEqual(ctx.exception.code, "NOTEBOOK_NOT_FOUND")

    def test_generate_empty_llm_response_raises(self) -> None:
        """Empty string from LLM must raise SYSTEM_LLM_BAD_RESPONSE, not silently persist."""
        with self.assertRaises(LLMError) as ctx:
            generate(self.store, FakeLLM(reply=""), self.nb, "briefing")
        self.assertEqual(ctx.exception.code, "SYSTEM_LLM_BAD_RESPONSE")

    def test_generate_whitespace_only_llm_response_raises(self) -> None:
        """Whitespace-only LLM response must also raise SYSTEM_LLM_BAD_RESPONSE."""
        with self.assertRaises(LLMError) as ctx:
            generate(self.store, FakeLLM(reply="   \n  "), self.nb, "briefing")
        self.assertEqual(ctx.exception.code, "SYSTEM_LLM_BAD_RESPONSE")

    def test_suggest_questions_missing_notebook_raises(self) -> None:
        with self.assertRaises(StoreError) as ctx:
            suggest_questions(self.store, FakeLLM(), 99999)
        self.assertEqual(ctx.exception.code, "NOTEBOOK_NOT_FOUND")

    def test_add_studio_output_missing_notebook_raises(self) -> None:
        """add_studio_output must raise NOTEBOOK_NOT_FOUND, not IntegrityError."""
        with self.assertRaises(StoreError) as ctx:
            self.store.add_studio_output(99999, "briefing", "body", "{}")
        self.assertEqual(ctx.exception.code, "NOTEBOOK_NOT_FOUND")

    def test_generate_english_instructions(self) -> None:
        import os

        llm = FakeLLM(reply="Summary [S1].")
        with patch.dict(os.environ, {"SHOIN_LANG": "en"}):
            result = generate(self.store, llm, self.nb, "briefing", persist=False)
        self.assertEqual(result.kind, "briefing")
        # Verify English strings appear in the prompt sent to the LLM.
        prompt = llm.chat_prompts[-1]
        self.assertIn("## Sources", prompt)
        self.assertIn("## Instructions", prompt)
        self.assertIn("Executive Summary", prompt)
        self.assertNotIn("## ソース", prompt)

    def test_suggest_questions_english_prompt(self) -> None:
        import os

        llm = FakeLLM(reply="What is this? How does it work?")
        with patch.dict(os.environ, {"SHOIN_LANG": "en"}):
            suggest_questions(self.store, llm, self.nb)
        prompt = llm.chat_prompts[-1]
        self.assertIn("## Sources", prompt)
        self.assertIn("questions a reader might ask", prompt)
        self.assertNotIn("## ソース", prompt)


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.nb = seed_notebook(self.store, n_sources=2)
        self.store.add_note(self.nb, "メモ1", "本文{中括弧}付き")
        self.store.add_studio_output(self.nb, "briefing", "要約 [S1]", "{}")

    def tearDown(self) -> None:
        self.store.close()

    def test_markdown_contains_sections(self) -> None:
        md = export(self.store, self.nb, "md")
        self.assertIn("# 研究", md)
        self.assertIn("[S1] 資料1", md)
        self.assertIn("### メモ1", md)
        self.assertIn("### briefing", md)

    def test_markdown_includes_chat_history(self) -> None:
        import json as _json

        report = _json.dumps({"source_map": {"S1": "資料1"}, "cited": [1], "invalid": []})
        self.store.add_message(self.nb, "user", "質問：主要点は？")
        self.store.add_message(self.nb, "assistant", "要約[S1]。", report)
        md = export(self.store, self.nb, "md")
        self.assertIn("## チャット履歴", md)
        self.assertIn("**User**: 質問：主要点は？", md)
        self.assertIn("**Assistant**", md)
        self.assertIn("S1=資料1", md)
        self.assertIn("要約[S1]。", md)

    def test_bibtex_escapes_braces(self) -> None:
        bib = export(self.store, self.nb, "bibtex")
        self.assertIn("@misc{shoin1,", bib)
        self.assertIn("title = {資料1}", bib)
        self.assertNotIn("{中括弧}", bib)  # braces never leak from titles

    def test_bibtex_escapes_backslashes(self) -> None:
        """Backslashes in titles/origins (e.g. Windows paths) must be doubled."""
        from shoin.export import _bib_escape

        self.assertEqual(_bib_escape("C:\\Users\\file.txt"), "C:\\\\Users\\\\file.txt")
        # Braces are still escaped, and backslash is escaped first to avoid double-escaping
        self.assertEqual(_bib_escape("a\\{b}"), "a\\\\(b)")

    def test_bibtex_escapes_tex_special_chars(self) -> None:
        """TeX special characters must be escaped so LaTeX can compile the .bib file.

        % causes silent truncation (comment); & causes a LaTeX error outside tabular;
        $ starts math mode; # is a parameter marker; _ causes errors outside math.
        ^ is the superscript operator; ~ is a non-breaking space (active character).
        """
        from shoin.export import _bib_escape

        # percent sign: most destructive — silently truncates rest of field in TeX
        self.assertEqual(_bib_escape("100%"), "100\\%")
        # URL with percent-encoded space (%20) and ampersand must both be escaped
        url = "https://example.com/page?q=hello%20world&lang=en"
        escaped = _bib_escape(url)
        self.assertIn("\\%20", escaped)   # %20 → \%20 (percent sign escaped, digits untouched)
        self.assertIn("\\&", escaped)     # & → \&
        # ampersand
        self.assertEqual(_bib_escape("foo & bar"), "foo \\& bar")
        # dollar sign
        self.assertEqual(_bib_escape("$5 off"), "\\$5 off")
        # hash
        self.assertEqual(_bib_escape("item #1"), "item \\#1")
        # underscore (common in filenames and URLs)
        self.assertEqual(_bib_escape("my_file.txt"), "my\\_file.txt")
        # backslash before percent must not produce \\% (already escaped)
        self.assertEqual(_bib_escape("\\%"), "\\\\\\%")
        # caret: TeX superscript operator — LaTeX error in text mode.
        # \^{} produces a circumflex accent over an empty box (visually ^).
        # The {} must NOT be corrupted to () by the {/} → (/) substitution.
        self.assertEqual(_bib_escape("O(n^2)"), "O(n\\^{}2)")
        self.assertNotIn("\\^()", _bib_escape("x^y"),
                         "\\^{} must keep LaTeX braces, not () from the literal-brace substitution")
        # tilde: TeX active char (non-breaking space) — common in academic URLs.
        # \~{} produces a tilde accent over an empty box (visually ~).
        url_tilde = "http://example.com/~user/paper.pdf"
        self.assertIn("\\~{}", _bib_escape(url_tilde))
        self.assertEqual(_bib_escape("~"), "\\~{}")

    def test_ris_date_uses_slash_format(self) -> None:
        """RIS 2001 spec requires DA  - YYYY/MM/DD (slashes), not ISO 8601 dashes."""
        ris = export(self.store, self.nb, "ris")
        import re as _re
        # Every DA field must use slashes, not dashes.
        for da_val in _re.findall(r"DA  - (\S+)", ris):
            self.assertRegex(da_val, r"^\d{4}/\d{2}/\d{2}$", f"DA field {da_val!r} must use YYYY/MM/DD")

    def test_ris_structure(self) -> None:
        ris = export(self.store, self.nb, "ris")
        self.assertEqual(ris.count("TY  - GEN"), 2)
        self.assertEqual(ris.count("ER  - "), 2)

    def test_ris_entries_separated_by_blank_line(self) -> None:
        """RIS 2001 spec requires a blank line between entries (two or more sources)."""
        ris = export(self.store, self.nb, "ris")
        # With 2 sources the output must contain a blank line between the two entries.
        self.assertIn("ER  - \n\nTY  - GEN", ris, "RIS entries must be separated by a blank line")

    def test_ris_escape_handles_crlf(self) -> None:
        """CRLF (Windows) line endings in field values must collapse to a single space."""
        from shoin.export import _ris_escape

        self.assertEqual(_ris_escape("line1\r\nline2"), "line1 line2")
        self.assertEqual(_ris_escape("line1\rline2"), "line1 line2")

    def test_bib_escape_handles_crlf(self) -> None:
        """CRLF (Windows) line endings in BibTeX field values must collapse to a single space."""
        from shoin.export import _bib_escape

        self.assertEqual(_bib_escape("line1\r\nline2"), "line1 line2")

    def test_unknown_format(self) -> None:
        with self.assertRaises(ValueError):
            export(self.store, self.nb, "docx")

    def test_export_missing_notebook_raises_for_all_formats(self) -> None:
        """bibtex and ris must raise NOTEBOOK_NOT_FOUND, not return empty strings."""
        from shoin.store import StoreError

        for fmt in ("md", "bibtex", "ris"):
            with self.assertRaises(StoreError) as cm:
                export(self.store, 99999, fmt)
            self.assertEqual(cm.exception.code, "NOTEBOOK_NOT_FOUND", msg=f"format={fmt}")

    def test_notes_crud(self) -> None:
        notes = self.store.list_notes(self.nb)
        self.assertEqual(len(notes), 1)
        self.store.delete_note(int(notes[0]["id"]))
        self.assertEqual(self.store.list_notes(self.nb), [])

    def test_markdown_invalid_citation_report_ignored(self) -> None:
        """Corrupted citation_report in DB must not crash export_markdown."""
        self.store.conn.execute(
            "INSERT INTO messages(notebook_id, role, body, citation_report, created_at)"
            " VALUES (?, 'assistant', 'reply text', 'NOT_VALID_JSON', '2024-01-01T00:00:00Z')",
            (self.nb,),
        )
        self.store.conn.commit()
        md = export(self.store, self.nb, "md")
        self.assertIn("reply text", md)
        self.assertIn("**Assistant**:", md)

    def test_markdown_english_section_headers(self) -> None:
        import os

        with patch.dict(os.environ, {"SHOIN_LANG": "en"}):
            md = export(self.store, self.nb, "md")
        self.assertIn("## Sources", md)
        self.assertIn("## Notes", md)
        self.assertIn("## Studio Output", md)
        self.assertNotIn("## ソース", md)
        self.assertNotIn("## ノート", md)

    def test_markdown_english_chat_section_and_source_label(self) -> None:
        import json as _json
        import os

        report = _json.dumps({"source_map": {"S1": "Doc1"}, "cited": [1], "invalid": []})
        self.store.add_message(self.nb, "user", "Question?")
        self.store.add_message(self.nb, "assistant", "Answer [S1].", report)
        with patch.dict(os.environ, {"SHOIN_LANG": "en"}):
            md = export(self.store, self.nb, "md")
        self.assertIn("## Chat History", md)
        self.assertNotIn("## チャット履歴", md)
        self.assertIn("(sources:", md)
        self.assertNotIn("(引用元:", md)

    def test_markdown_message_empty_citation_report(self) -> None:
        """Assistant message with empty citation_report '{}' must export without crash."""
        self.store.add_message(self.nb, "user", "Q?")
        self.store.add_message(self.nb, "assistant", "A.", "{}")
        md = export(self.store, self.nb, "md")
        self.assertIn("**Assistant**", md)
        self.assertIn("A.", md)

    def test_markdown_empty_notebook(self) -> None:
        """Notebook with no sources, notes, or messages must still export cleanly."""
        empty_nb = self.store.create_notebook("空白").id
        md = export(self.store, empty_nb, "md")
        self.assertIn("# 空白", md)
        self.assertIn("ソース", md)


class StudioHitsEdgeCaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.nb = seed_notebook(self.store)

    def tearDown(self) -> None:
        self.store.close()

    def test_overview_hits_per_source_zero_returns_empty(self) -> None:
        """per_source=0 must skip all sources and return an empty list."""
        hits = overview_hits(self.store, self.nb, per_source=0)
        self.assertEqual(hits, [])

    def test_overview_hits_single_chunk_source(self) -> None:
        """A source with exactly 1 chunk must return that chunk for any per_source >= 1."""
        store = Store(":memory:")
        nb = store.create_notebook("one-chunk").id
        src = store.add_source(nb, "txt", "tiny", "/t", "h1")
        store.add_chunks(src.id, ["唯一のチャンク"])
        hits = overview_hits(store, nb, per_source=3)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].text, "唯一のチャンク")
        store.close()


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.nb = self.store.create_notebook("取込").id

    def tearDown(self) -> None:
        self.store.close()

    def _tmp_txt(self, tmpdir: str, body: str) -> str:
        path = Path(tmpdir) / "doc.txt"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_index_source_no_llm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = index_source(self.store, self.nb, self._tmp_txt(td, "段落。" * 50))
        self.assertGreaterEqual(result.n_chunks, 1)
        self.assertEqual(result.n_embedded, 0)
        self.assertEqual(self.store.counts(self.nb)["chunks"], result.n_chunks)

    def test_index_source_with_embedding(self) -> None:
        llm = FakeLLM(embedding_model="nomic-embed-text")
        with tempfile.TemporaryDirectory() as td:
            result = index_source(self.store, self.nb, self._tmp_txt(td, "本文。" * 50), llm)
        self.assertEqual(result.n_embedded, result.n_chunks)

    def test_embed_partial_failure_keeps_progress(self) -> None:
        src = self.store.add_source(self.nb, "file", "t", "/tmp/t", "x")
        texts = ["a", "b", "c", "d"]
        ids = self.store.add_chunks(src.id, texts)
        llm = FakeLLM(embedding_model="m", fail_embed_after=1)
        with patch("shoin.pipeline.EMBED_BATCH", 2):
            done = _embed_chunks(self.store, llm, ids, texts)
        self.assertEqual(done, 2)  # first batch persisted, second failed

    def test_embed_partial_failure_records_model(self) -> None:
        """embed_model is recorded even on partial LLM failure so future model-change warnings fire."""
        src = self.store.add_source(self.nb, "file", "tp", "/tmp/tp", "xp")
        texts = ["a", "b", "c", "d"]
        ids = self.store.add_chunks(src.id, texts)
        llm = FakeLLM(embedding_model="nomic-embed-text", fail_embed_after=1)
        with patch("shoin.pipeline.EMBED_BATCH", 2):
            done = _embed_chunks(self.store, llm, ids, texts)
        self.assertEqual(done, 2)
        self.assertEqual(self.store.get_setting("embed_model"), "nomic-embed-text")

    def test_embed_model_change_warns_and_returns_zero(self) -> None:
        """Changing SHOIN_EMBED_MODEL prints a warning and skips embedding to keep DB coherent."""
        src = self.store.add_source(self.nb, "file", "t2", "/tmp/t2", "y")
        ids = self.store.add_chunks(src.id, ["text"])
        # Seed a first embedding run with model-A.
        self.store.set_setting("embed_model", "model-A")
        llm_b = FakeLLM(embedding_model="model-B")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            done = _embed_chunks(self.store, llm_b, ids, ["text"])
        self.assertIn("model-A", err.getvalue())
        self.assertIn("model-B", err.getvalue())
        # Mismatch must skip all embedding so the DB stays coherent.
        self.assertEqual(done, 0)
        # Stored model must not be updated — old embeddings are still model-A.
        self.assertEqual(self.store.get_setting("embed_model"), "model-A")

    def test_reindex_notebook_re_embeds_all_chunks(self) -> None:
        """reindex_notebook re-embeds every chunk and returns (n, total)."""
        # PipelineTest has no sources by default; seed one first.
        src = self.store.add_source(self.nb, "file", "t", "/tmp/t", "hash-ri")
        self.store.add_chunks(src.id, ["段落A。", "段落B。", "段落C。"])
        llm = FakeLLM(embedding_model="nomic-embed-text")
        n, total = reindex_notebook(self.store, llm, self.nb)
        self.assertEqual(n, total)
        self.assertGreater(total, 0)
        for chunk in self.store.chunks_for_notebook(self.nb):
            self.assertIsNotNone(chunk.embedding)

    def test_reindex_empty_notebook_returns_zero(self) -> None:
        empty_nb = self.store.create_notebook("空").id
        n, total = reindex_notebook(self.store, FakeLLM(embedding_model="m"), empty_nb)
        self.assertEqual((n, total), (0, 0))

    def test_reindex_missing_notebook_raises(self) -> None:
        with self.assertRaises(StoreError) as cm:
            reindex_notebook(self.store, FakeLLM(embedding_model="m"), 99999)
        self.assertEqual(cm.exception.code, "NOTEBOOK_NOT_FOUND")

    def test_reindex_after_model_change_succeeds(self) -> None:
        """reindex_notebook must bypass the mismatch guard — it IS the migration path.

        Bug introduced in v0.1.61: _embed_chunks returned 0 when stored_model !=
        current_model, blocking the one command meant to fix that mismatch.
        Fix: reindex_notebook passes force=True to _embed_chunks.
        """
        src = self.store.add_source(self.nb, "file", "t-rm", "/tmp/t-rm", "hash-rm")
        ids = self.store.add_chunks(src.id, ["段落X。"])
        # First pass: embed with model-A, store records "model-A".
        llm_a = FakeLLM(embedding_model="model-A")
        _embed_chunks(self.store, llm_a, ids, ["段落X。"])
        self.assertEqual(self.store.get_setting("embed_model"), "model-A")

        # User switches to model-B and runs reindex — must not be blocked.
        llm_b = FakeLLM(embedding_model="model-B")
        n, total = reindex_notebook(self.store, llm_b, self.nb)

        self.assertEqual(n, total, "reindex must embed all chunks")
        self.assertGreater(total, 0)
        # After reindex the stored model must reflect the new model.
        self.assertEqual(self.store.get_setting("embed_model"), "model-B")

    def test_embed_chunks_chunk_deleted_mid_batch_does_not_raise(self) -> None:
        """set_embedding raises StoreError when a chunk is concurrently deleted.
        _embed_chunks must absorb it (best-effort), not propagate it to the caller."""
        from unittest.mock import patch

        from shoin.store import StoreError as SE

        src = self.store.add_source(self.nb, "file", "td", "/tmp/td", "zz")
        ids = self.store.add_chunks(src.id, ["a", "b", "c", "d"])
        llm = FakeLLM(embedding_model="model-x")
        call_count = 0

        def _set_embedding_raises_on_third(chunk_id, vec, *, commit=True):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise SE("CHUNK_NOT_FOUND", "chunk deleted mid-batch")

        with patch.object(self.store, "set_embedding", side_effect=_set_embedding_raises_on_third):
            done = _embed_chunks(self.store, llm, ids, ["a", "b", "c", "d"])
        # First batch had 4 chunks; failed on the 3rd → 0 committed from this batch.
        self.assertEqual(done, 0)

    def test_embed_chunks_chunk_deleted_partial_batch_not_committed(self) -> None:
        """When set_embedding raises mid-batch, the uncommitted partial-batch
        writes must be rolled back so they aren't silently flushed by set_setting()."""
        from unittest.mock import patch

        from shoin.store import StoreError as SE

        src = self.store.add_source(self.nb, "file", "tc", "/tmp/tc", "zz2")
        ids = self.store.add_chunks(src.id, ["p", "q", "r", "s"])
        llm = FakeLLM(embedding_model="model-y")
        call_count = 0

        real_set_embedding = self.store.set_embedding

        def _spy_raises_on_third(chunk_id, vec, *, commit=True):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise SE("CHUNK_NOT_FOUND", "chunk deleted mid-batch")
            real_set_embedding(chunk_id, vec, commit=commit)

        with patch.object(self.store, "set_embedding", side_effect=_spy_raises_on_third):
            _embed_chunks(self.store, llm, ids, ["p", "q", "r", "s"])

        # After the StoreError abort and rollback, no chunks should have embeddings.
        for chunk in self.store.chunks_for_notebook(self.nb):
            self.assertIsNone(chunk.embedding, f"chunk {chunk.id} should not have embedding")


class CliTest(unittest.TestCase):
    def _run(self, argv: list[str], llm: FakeLLM) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv, llm=llm)
        return rc, out.getvalue(), err.getvalue()

    def test_full_workflow(self) -> None:
        llm = FakeLLM(reply="回答 [S1]。\n質問ですか？")
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "shoin.db")
            doc = Path(td) / "memo.txt"
            doc.write_text("会議メモ。決定事項あり。" * 20, encoding="utf-8")

            rc, out, _ = self._run(["--db", db, "notebook", "new", "案件A"], llm)
            self.assertEqual(rc, 0)
            self.assertIn("[1] 案件A", out)

            rc, out, _ = self._run(["--db", db, "add", "1", str(doc)], llm)
            self.assertEqual(rc, 0)
            self.assertIn("✓ memo", out)

            rc, out, _ = self._run(["--db", db, "ask", "1", "決定事項は？"], llm)
            self.assertEqual(rc, 0)
            self.assertIn("回答 [S1]", out)
            self.assertIn("[S1] memo", out)

            rc, out, _ = self._run(["--db", db, "studio", "1", "briefing"], llm)
            self.assertEqual(rc, 0)
            self.assertIn("[S1] memo", out)

            rc, out, _ = self._run(["--db", db, "questions", "1"], llm)
            self.assertEqual(rc, 0)
            self.assertIn("- 質問ですか?", out)

            rc, out, _ = self._run(["--db", db, "export", "1", "--format", "bibtex"], llm)
            self.assertEqual(rc, 0)
            self.assertIn("@misc{shoin1,", out)

            rc, out, _ = self._run(["--db", db, "notebook", "list"], llm)
            self.assertIn("sources=1", out)

            # rename notebook
            rc, out, _ = self._run(["--db", db, "notebook", "rename", "1", "案件A改"], llm)
            self.assertEqual(rc, 0)
            self.assertIn("案件A改", out)

            # clear messages then verify
            rc, out, _ = self._run(["--db", db, "messages", "clear", "1"], llm)
            self.assertEqual(rc, 0)
            self.assertIn("クリア", out)

            rc, _, _ = self._run(["--db", db, "notebook", "delete", "1"], llm)
            self.assertEqual(rc, 0)

    def test_add_missing_file_returns_error(self) -> None:
        llm = FakeLLM()
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "shoin.db")
            self._run(["--db", db, "notebook", "new", "x"], llm)
            rc, _, err = self._run(["--db", db, "add", "1", "/no/such/file.txt"], llm)
        self.assertEqual(rc, 1)
        self.assertIn("✗", err)

    def test_studio_error_surfaces_code(self) -> None:
        llm = FakeLLM()
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "shoin.db")
            self._run(["--db", db, "notebook", "new", "x"], llm)
            rc, _, err = self._run(["--db", db, "studio", "1", "briefing"], llm)
        self.assertEqual(rc, 1)
        self.assertIn("NOTEBOOK_EMPTY", err)

    def test_ask_shows_confirmed_marker(self) -> None:
        """CLI _print_report surfaces grounding confirmation (three-stage verification)."""
        # Reply that lexically overlaps the source text → confirmed
        content = "会議メモ。決定事項あり。" * 20
        llm = FakeLLM(reply="会議メモの決定事項[S1]。")
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "shoin.db")
            doc = Path(td) / "memo.txt"
            doc.write_text(content, encoding="utf-8")
            self._run(["--db", db, "notebook", "new", "n"], llm)
            self._run(["--db", db, "add", "1", str(doc)], llm)
            rc, out, _ = self._run(["--db", db, "ask", "1", "決定事項は？"], llm)
        self.assertEqual(rc, 0)
        self.assertIn("✓根拠確認済み", out)

    def test_cli_i18n_english_output(self) -> None:
        """SHOIN_LANG=en switches CLI messages to English."""
        import os

        llm = FakeLLM()
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "shoin.db")
            env_orig = os.environ.get("SHOIN_LANG")
            os.environ["SHOIN_LANG"] = "en"
            try:
                rc, out, _ = self._run(["--db", db, "notebook", "new", "MyNote"], llm)
                self.assertEqual(rc, 0)
                self.assertIn("Created:", out)
                rc2, out2, _ = self._run(["--db", db, "notebook", "rename", "1", "MyNote2"], llm)
                self.assertEqual(rc2, 0)
                self.assertIn("Renamed:", out2)
                rc3, out3, _ = self._run(["--db", db, "messages", "clear", "1"], llm)
                self.assertIn("Chat history cleared", out3)
                rc4, out4, _ = self._run(["--db", db, "notebook", "delete", "1"], llm)
                self.assertIn("Deleted", out4)
            finally:
                if env_orig is None:
                    os.environ.pop("SHOIN_LANG", None)
                else:
                    os.environ["SHOIN_LANG"] = env_orig

    def test_reindex_cli_command(self) -> None:
        """shoin reindex <id> re-embeds chunks and reports count."""
        llm = FakeLLM(embedding_model="nomic-embed-text")
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "shoin.db")
            doc = Path(td) / "doc.txt"
            doc.write_text("テスト本文。" * 30, encoding="utf-8")
            self._run(["--db", db, "notebook", "new", "n"], llm)
            self._run(["--db", db, "add", "1", str(doc)], llm)
            rc, out, _ = self._run(["--db", db, "reindex", "1"], llm)
        self.assertEqual(rc, 0)
        self.assertIn("✓", out)
        self.assertIn("/", out)  # "n/total" format

    def test_notebook_list_empty_prints_message(self) -> None:
        llm = FakeLLM()
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "shoin.db")
            rc, out, _ = self._run(["--db", db, "notebook", "list"], llm)
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip())  # not silent when empty

    def test_ris_export_escapes_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "shoin.db")
            with Store(db) as s:
                nb = s.create_notebook("n")
                s.add_source(nb.id, "txt", "Title\nLine2", "origin\nline2", "h1")
            result = export(Store(db), nb.id, "ris")
        # Each RIS field must be a single line — no raw newline inside a field value
        ti_line = result.split("TI  - ")[1].split("\n")[0]
        ur_line = result.split("UR  - ")[1].split("\n")[0]
        self.assertNotIn("\n", ti_line)
        self.assertNotIn("\n", ur_line)
        # The escaped text should have been joined with a space
        self.assertIn("Title Line2", ti_line)


if __name__ == "__main__":
    unittest.main(verbosity=0)
