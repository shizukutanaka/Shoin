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
from shoin.pipeline import _embed_chunks, index_source  # noqa: E402
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
        self.assertEqual(len(hits), 6)  # 2 sources × first 3 chunks
        self.assertEqual(len({h.source_id for h in hits}), 2)

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
        llm = FakeLLM(
            reply="1. 目的は何か？\n- 仕組みはどう動きますか?\n* 装飾のみのノイズ行\n結論として要約\n2. 制約は何か"
        )
        qs = suggest_questions(self.store, llm, self.nb)
        self.assertEqual(qs, ["目的は何か？", "仕組みはどう動きますか?", "制約は何か"])

    def test_suggest_questions_accepts_ka_with_trailing_period(self) -> None:
        """LLMs often append 。 even with 'no decoration' instructions — must not drop."""
        llm = FakeLLM(reply="この書院はどう動くのか。\n内容について説明します。")
        qs = suggest_questions(self.store, llm, self.nb)
        self.assertEqual(qs, ["この書院はどう動くのか。"])

    def test_suggest_questions_empty_notebook(self) -> None:
        empty = self.store.create_notebook("空")
        self.assertEqual(suggest_questions(self.store, FakeLLM(), empty.id), [])

    def test_suggest_questions_llm_unavailable_returns_empty(self) -> None:
        """DoD: suggest_questions degrades gracefully when LLM is down (graceful degradation)."""
        self.assertEqual(suggest_questions(self.store, FakeLLM(chat_error=True), self.nb), [])


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

    def test_bibtex_escapes_braces(self) -> None:
        bib = export(self.store, self.nb, "bibtex")
        self.assertIn("@misc{shoin1,", bib)
        self.assertIn("title = {資料1}", bib)
        self.assertNotIn("{中括弧}", bib)  # braces never leak from titles

    def test_ris_structure(self) -> None:
        ris = export(self.store, self.nb, "ris")
        self.assertEqual(ris.count("TY  - GEN"), 2)
        self.assertEqual(ris.count("ER  - "), 2)

    def test_unknown_format(self) -> None:
        with self.assertRaises(ValueError):
            export(self.store, self.nb, "docx")

    def test_notes_crud(self) -> None:
        notes = self.store.list_notes(self.nb)
        self.assertEqual(len(notes), 1)
        self.store.delete_note(int(notes[0]["id"]))
        self.assertEqual(self.store.list_notes(self.nb), [])


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
            self.assertIn("- 質問ですか？", out)

            rc, out, _ = self._run(["--db", db, "export", "1", "--format", "bibtex"], llm)
            self.assertEqual(rc, 0)
            self.assertIn("@misc{shoin1,", out)

            rc, out, _ = self._run(["--db", db, "notebook", "list"], llm)
            self.assertIn("sources=1", out)

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


if __name__ == "__main__":
    unittest.main(verbosity=0)
