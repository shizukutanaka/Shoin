"""Tests for the optional Rust accelerator dispatch (shoin/accel.py).

Two layers:

* Dispatch tests inject a fake native module, so they run everywhere —
  including CI, where the .so is never built. The wrappers must route to
  the native functions when enabled, return None when disabled or
  absent, and degrade to None on any native failure (the callers then
  run the pure-Python path).
* Differential tests (skipped unless the real .so is importable) pin
  the contract that the Rust kernels return results identical to the
  pure-Python paths — same hits, same scores, bit for bit, same order.
"""

from __future__ import annotations

import os
import unittest
from array import array
from collections.abc import Iterator
from typing import Any
from unittest import mock

from shoin import accel, config, search
from shoin.chunk import _CJK_BOUNDS_BYTES, _tail, estimate_tokens
from shoin.store import Store


class _FakeNative:
    """Records calls and returns canned results."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def vector_topk(
        self,
        query: list[float],
        rows: Iterator[tuple[int, bytes, float | None]],
        k: int,
    ) -> list[tuple[int, float]]:
        self.calls.append("vector_topk")
        return [(7, 0.5)]

    def needle_pool(
        self,
        rows: Iterator[tuple[int, str, str]],
        needles: list[str],
        cap: int,
        ctx_weight: float,
    ) -> list[int]:
        self.calls.append("needle_pool")
        return [3, 1]

    def estimate_tokens(self, text: str, bounds: bytes) -> int:
        self.calls.append("estimate_tokens")
        return 42

    def tail(self, text: str, tokens: int, bounds: bytes) -> str:
        self.calls.append("tail")
        return "tail!"


class _BoomNative:
    """Every entry point raises — the dispatch must degrade, not crash."""

    def vector_topk(self, *args: Any) -> Any:
        raise RuntimeError("boom")

    def needle_pool(self, *args: Any) -> Any:
        raise RuntimeError("boom")

    def estimate_tokens(self, *args: Any) -> Any:
        raise RuntimeError("boom")

    def tail(self, *args: Any) -> Any:
        raise RuntimeError("boom")


def _all_calls():
    return [
        lambda: accel.vector_topk([1.0], iter([]), 3),
        lambda: accel.needle_pool(iter([]), ["x"], 5, 2.0),
        lambda: accel.estimate_tokens("abc", b""),
        lambda: accel.tail("abc", 2, b""),
    ]


class TestAccelDispatch(unittest.TestCase):
    def test_wrappers_route_to_native_when_enabled(self) -> None:
        fake = _FakeNative()
        with (
            mock.patch.object(accel, "_shoin_accel", fake),
            mock.patch.object(accel, "_ENABLED", True),
        ):
            self.assertEqual(accel.vector_topk([1.0], iter([]), 3), [(7, 0.5)])
            self.assertEqual(accel.needle_pool(iter([]), ["x"], 5, 2.0), [3, 1])
            self.assertEqual(accel.estimate_tokens("abc", b""), 42)
            self.assertEqual(accel.tail("abc", 2, b""), "tail!")
        self.assertEqual(
            fake.calls,
            ["vector_topk", "needle_pool", "estimate_tokens", "tail"],
        )

    def test_wrappers_return_none_when_disabled(self) -> None:
        fake = _FakeNative()
        with (
            mock.patch.object(accel, "_shoin_accel", fake),
            mock.patch.object(accel, "_ENABLED", False),
        ):
            for call in _all_calls():
                self.assertIsNone(call())
        self.assertEqual(fake.calls, [])

    def test_wrappers_return_none_when_native_missing(self) -> None:
        with (
            mock.patch.object(accel, "_shoin_accel", None),
            mock.patch.object(accel, "_ENABLED", True),
        ):
            for call in _all_calls():
                self.assertIsNone(call())

    def test_native_failure_degrades_to_none(self) -> None:
        with (
            mock.patch.object(accel, "_shoin_accel", _BoomNative()),
            mock.patch.object(accel, "_ENABLED", True),
        ):
            for call in _all_calls():
                self.assertIsNone(call())

    def test_available_reflects_flag(self) -> None:
        with mock.patch.object(accel, "_ENABLED", True):
            self.assertTrue(accel.available())
        with mock.patch.object(accel, "_ENABLED", False):
            self.assertFalse(accel.available())

    def test_config_switch(self) -> None:
        with (
            mock.patch.object(config, "_file_config", lambda: {}),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertTrue(config.accel_enabled())  # default on
        with mock.patch.dict(os.environ, {"SHOIN_ACCEL": "1"}):
            self.assertTrue(config.accel_enabled())
        for off in ("0", "false", "no", "off"):
            with mock.patch.dict(os.environ, {"SHOIN_ACCEL": off}):
                self.assertFalse(config.accel_enabled())


def _fixture_store() -> tuple[Store, int]:
    store = Store(":memory:")
    nb = store.create_notebook("accel-fixture")
    src = store.add_source(nb.id, "txt", "doc", "mem://doc", "sha-doc")
    texts = [
        "経済と市場の動向を分析する。経済は回復基調にある。",
        "令和6年の予算は50億円に上った。予算の内訳を検証する。",
        "Budget 2024: the budget grew. BUDGET details inside.",
        "免疫に関する研究ノート。免疫細胞の働きを整理する。",
        "何の変哲もない文章。検索語を含まない。",
        "予算と経済の交差する事例。予算、予算、予算と繰り返す。",
    ]
    contexts = [
        "経済 > 動向",
        "制度 > 予算",
        "Finance > Budget",
        "健康 > 免疫",
        "雑記",
        "経済 > 予算",
    ]
    ids = store.add_chunks(src.id, texts, contexts)
    vecs = [
        [0.9, 0.1, 0.0, 0.2],
        [0.2, 0.9, 0.1, 0.0],
        [0.5, 0.5, 0.5, 0.5],
        [0.0, 0.0, 0.9, 0.1],
        [-0.9, 0.1, 0.0, 0.0],
        [0.9, 0.1, 0.0, 0.2],  # exact tie with chunk 1
    ]
    for cid, vec in zip(ids, vecs, strict=True):
        store.set_embedding(cid, vec)
    return store, nb.id


def _hitsig(hits) -> list[tuple[int, float, float, float]]:
    return [(h.chunk_id, h.bm25, h.vec, h.score) for h in hits]


class TestAccelSearchFallback(unittest.TestCase):
    """End-to-end: with a native module that always raises, the search
    paths must produce exactly the pure-Python results."""

    def test_search_falls_back_when_native_raises(self) -> None:
        store, nb = _fixture_store()
        self.addCleanup(store.close)
        qvec = [0.9, 0.1, 0.0, 0.2]
        with mock.patch.object(accel, "_ENABLED", False):
            want_bm25 = _hitsig(search.bm25_search(store, nb, "経済", 8))
            want_vec = _hitsig(search.vector_search(store, nb, qvec, 8))
        with (
            mock.patch.object(accel, "_shoin_accel", _BoomNative()),
            mock.patch.object(accel, "_ENABLED", True),
        ):
            got_bm25 = _hitsig(search.bm25_search(store, nb, "経済", 8))
            got_vec = _hitsig(search.vector_search(store, nb, qvec, 8))
        self.assertEqual(got_bm25, want_bm25)
        self.assertEqual(got_vec, want_vec)


@unittest.skipUnless(accel._shoin_accel is not None, "rust accelerator not built")
class TestAccelNativeDifferential(unittest.TestCase):
    """The Rust kernels must be indistinguishable from the Python paths."""

    def test_estimate_tokens_matches_python(self) -> None:
        samples = [
            "",
            "abc",
            "令和6年の予算は前年度比で増加した",
            "型番ABC123456の在庫を確認する",
            "a" * 39,
            "a" * 40,
            "a" * 41,
            "a" * 44,
            "a" * 45,
            "a" * 100,
            "hello world foo bar baz",
            "日本語とEnglishの混在テキスト。token見積り",
            "①②③囲み文字と㍻元号",
            "x" * 41 + "日本語テスト" * 30,
            "予算" * 100,
        ]
        with mock.patch.object(accel, "_ENABLED", True):
            for s in samples:
                native = accel.estimate_tokens(s, _CJK_BOUNDS_BYTES)
                with mock.patch.object(accel, "_ENABLED", False):
                    python = estimate_tokens(s)
                self.assertEqual(native, python, repr(s))

    def test_tail_matches_python(self) -> None:
        samples = [
            "",
            "abc",
            "  先頭スペースと末尾  ",
            "令和6年の予算は前年度比で増加した",
            "型番ABC123456の在庫",
            "a" * 100,
            "hello world foo bar",
            "x" * 41 + "日本語テスト" * 30,
            "予算" * 100,
            "末尾が単語の途中で切れるケースabc def",
            "\x1c\x1d区切り文字の前後",
        ]
        with mock.patch.object(accel, "_ENABLED", True):
            for s in samples:
                for tokens in (0, 1, 3, 17, 64, 500):
                    native = accel.tail(s, tokens, _CJK_BOUNDS_BYTES)
                    with mock.patch.object(accel, "_ENABLED", False):
                        python = _tail(s, tokens)
                    self.assertEqual(native, python, (repr(s), tokens))

    def test_vector_topk_matches_python_scan(self) -> None:
        def blob(vec: list[float]) -> bytes:
            return array("f", vec).tobytes()

        rows: list[tuple[int, bytes, float | None]] = [
            (1, blob([1.0, 0.0, 0.0, 0.0]), None),
            (2, blob([0.0, 1.0, 0.0, 0.0]), None),
            (3, blob([0.5, 0.5, 0.5, 0.5]), None),
            (4, blob([0.0, 0.0, 0.0, 0.0]), None),  # zero vector
            (5, blob([-1.0, 0.0, 0.0, 0.0]), None),  # negative cosine
            (6, b"\x00\x00", None),  # odd-length blob: skipped
            (7, blob([1.0, 0.0, 0.0]), None),  # dimension mismatch
            (8, blob([0.5, 0.5, 0.5, 0.5]), 2.0),  # stored (wrong) norm wins
            (9, blob([1.0, 0.0, 0.0, 0.0]), None),  # exact tie with row 1
            (10, blob([float("nan"), 0.0, 0.0, 0.0]), None),
            (11, blob([float("inf"), 0.0, 0.0, 0.0]), None),
        ]
        query = [0.9, 0.1, 0.2, 0.0]
        qn = search._vec_norm(query)

        def reference(
            rows_: list[tuple[int, bytes, float | None]], k: int
        ) -> list[tuple[int, float]]:
            scored: list[tuple[int, float]] = []
            for cid, raw, stored in rows_:
                vec = array("f")
                try:
                    vec.frombytes(raw)
                except (ValueError, TypeError):
                    continue
                vn = search._vec_norm(vec) if stored is None else float(stored)
                s = search._cosine_with_norms(query, qn, vec, vn)
                if s > 0.0:
                    scored.append((cid, s))
            scored.sort(key=lambda t: t[1], reverse=True)
            return scored[:k]

        with mock.patch.object(accel, "_ENABLED", True):
            for k in (0, 1, 3, 24):
                native = accel.vector_topk(query, iter(rows), k)
                self.assertEqual(native, reference(rows, k), f"k={k}")
            self.assertEqual(accel.vector_topk(query, iter([]), 5), [])

    def test_needle_pool_matches_sql_formula(self) -> None:
        # Reference: the SQL selector's own formula (ASCII-only corpus,
        # where str.lower() agrees with SQLite's ASCII LOWER()).
        def reference(
            rows_: list[tuple[int, str, str]],
            needles: list[str],
            cap: int,
            weight: float,
        ) -> list[int]:
            scored: list[tuple[float, int]] = []
            for cid, text, ctx in rows_:
                lt, lc = text.lower(), ctx.lower()
                s = sum(
                    lt.count(n.lower()) + (weight if n.lower() in lc else 0.0)
                    for n in needles
                )
                if s > 0:
                    scored.append((s, cid))
            scored.sort(key=lambda t: (-t[0], t[1]))
            return [cid for _, cid in scored[:cap]]

        rows: list[tuple[int, str, str]] = []
        for i in range(3000):
            text = "budget " * (i % 7) + f"document {i} " + "keizai" * (i % 3)
            ctx = "Budget section" if i % 11 == 0 else f"section {i}"
            rows.append((i + 1, text, ctx))
        rows.append((9001, "nothing relevant", "budget in context only"))
        rows.append((9002, "BUDGET BUDGET", ""))
        needles = ["Budget", "keizai"]
        with mock.patch.object(accel, "_ENABLED", True):
            for cap in (10, 2000, 5000):
                native = accel.needle_pool(iter(rows), needles, cap, 2.0)
                self.assertEqual(native, reference(rows, needles, cap, 2.0), f"cap={cap}")

    def test_search_paths_match_python_on_fixture(self) -> None:
        store, nb = _fixture_store()
        self.addCleanup(store.close)
        qvec = [0.9, 0.1, 0.0, 0.2]
        queries = ["経済", "予算", "Budget", "令和6年の予算", "免疫", "存在しない語彙"]
        for q in queries:
            for k in (1, 8):
                with mock.patch.object(accel, "_ENABLED", True):
                    got_bm25 = _hitsig(search.bm25_search(store, nb, q, k))
                    got_vec = _hitsig(search.vector_search(store, nb, qvec, k))
                    got_hybrid = _hitsig(search.retrieve(store, nb, q, qvec, k=k))
                with mock.patch.object(accel, "_ENABLED", False):
                    want_bm25 = _hitsig(search.bm25_search(store, nb, q, k))
                    want_vec = _hitsig(search.vector_search(store, nb, qvec, k))
                    want_hybrid = _hitsig(search.retrieve(store, nb, q, qvec, k=k))
                self.assertEqual(got_bm25, want_bm25, (q, k, "bm25"))
                self.assertEqual(got_vec, want_vec, (q, k, "vector"))
                self.assertEqual(got_hybrid, want_hybrid, (q, k, "hybrid"))


if __name__ == "__main__":
    unittest.main()
