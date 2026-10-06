"""Shoin CLI: notebook management, ingestion, grounded Q&A, studio, export.

`serve` (Web UI) lands in Phase 4; the CLI exposes every core capability so the
product is fully usable headless (REQ-103).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import unicodedata
from collections.abc import Sequence
from pathlib import Path

from .citation import COVERAGE_LOW, CitationReport, found_bits
from .config import (
    EMBED_MODEL_SETTING_KEY,
    MAX_QUESTION_LEN,
    MAX_TITLE_LEN,
    TOP_K,
    VERSION,
    chunk_overlap,
    chunk_tokens,
    db_path,
    embed_batch,
    embed_model,
    llm_model,
    llm_url,
    multi_query_enabled,
    port,
    ui_lang,
)
from .export import FORMATS, export
from .ingest import IngestError
from .llm import LLMClient, LLMError
from .pipeline import (
    index_source,
    refresh_all_sources,
    refresh_source,
    reindex_notebook,
    rename_source,
)
from .qa import (
    ChatBackend,
    _check_embed_model_ok,
    _query_vector,
    ask,
    expand_query,
    retrieve_for_question,
)
from .search import suggest_corrections
from .store import Store, StoreError
from .studio import KINDS, generate, suggest_questions


def _pos_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return n


def _port_num(value: str) -> int:
    n = int(value)
    if not 0 <= n <= 65535:
        raise argparse.ArgumentTypeError("port must be in 0-65535")
    return n


def _one_line(text: str) -> str:
    """Render an externally-controlled string safe for single-line output.

    Status rows and label fields are emitted one-per-line; a stored title,
    CLI argument, or env value containing a control character (\n, \r, ESC,
    U+2028…) would split the row or rewrite earlier terminal output — a forged
    `✓` line is indistinguishable from a real one. Escaping preserves the row
    shape and keeps the original bytes readable.
    """
    out: list[str] = []
    for ch in text:
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif unicodedata.category(ch) in ("Cc", "Zl", "Zp"):
            cp = ord(ch)
            out.append(f"\\x{cp:02x}" if cp < 0x100 else f"\\u{cp:04x}")
        else:
            out.append(ch)
    return "".join(out)


_STRINGS: dict[str, dict[str, str]] = {
    "ja": {
        "nb.created": "作成: [{id}] {name}",
        "nb.deleted": "削除完了",
        "nb.renamed": "改名完了: [{id}] {name}",
        "nb.settings_set": "設定完了: [{id}] {settings}",
        "nb.duplicated": "複製完了: [{id}] {name}",
        "nb.merged": "統合完了: [{id}] {name} (nb{src} を吸収・ゴミ箱へアーカイブ)",
        "nb.imported": "インポート完了: [{id}] {name}",
        "trash.empty": "ゴミ箱は空です。",
        "trash.item": "[{id}] {kind} {name}  (nb_id {nb_id}・削除 {ts})",
        "trash.restored": "復元完了: [{id}] {name}",
        "trash.purged": "アーカイブを完全削除しました",
        "trash.emptied": "ゴミ箱を空にしました: {n}件",
        "vacuum.done": "VACUUM完了: {before} → {after} ({freed} 回収)",
        "stats.freelist": "回収可能な空き領域: {n}",
        "nb.empty": "書院がありません。`shoin notebook new <名前>` で作成。",
        "msg.cleared": "チャット履歴をクリアしました",
        "msg.empty": "チャット履歴がありません。",
        "cite.invalid": "⚠ 検証失敗の引用(ソース範囲外): {bad}",
        "cite.confirmed": " ✓根拠確認済み",
        "cite.misattr": " ⚠番号取り違えの可能性",
        "cite.numeric": " ⚠数値が出典に無し",
        "cite.unit": " ⚠単位が出典と不一致",
        "cite.negation": " ⚠出典と逆の主張の可能性",
        "cite.uncited": "⚠ 無出典の断定文({n}件、引用なし):",
        "cite.uncited_supported": "出典内一致=引用欠落の疑い",
        "cite.found": "検出: ",
        "cite.found_fts": "全文",
        "cite.found_vec": "意味",
        "cite.found_lex": "語彙",
        "cite.degenerate": "⚠ 繰り返し生成の疑い({n}件):",
        "cite.truncated": "⚠ 出力が途中で打ち切られた可能性(トークン上限)",
        "cite.contradict": "⚠ 前後の記述が矛盾({n}件):",
        "cite.coverage_low": (
            "⚠ 引用被覆 低: {n}/{total} ソースのみ引用"
            "(取得済みの根拠を使い切っていない可能性)"
        ),
        "eval.header": "検索精度 (k={k}, {n}件のケース)",
        "search.header": "横断検索結果 ({n}件)",
        "search.hit": "  #{rank} [nb{nb_id} {nb}] {src} ({score}): {text}",
        "search.suggest": "もしかして: {terms}",
        "eval.recall": "  recall  : {v}  (期待ソースのうち上位kに現れた割合)",
        "eval.mrr": "  MRR     : {v}  (最初に当たった期待ソースの順位の逆数)",
        "eval.case_ok": "  ✓ {q}",
        "eval.case_ng": "  ✗ {q}",
        "eval.case_detail": "      期待={exp} 取得={got}",
        "eval.case_missing": (
            "      警告: 期待ソース {ids} はノートブックに存在しない"
            " (削除/別idの可能性)"
        ),
        "eval.saved": "ベースライン保存: {f}",
        "eval.gen_saved": "ケース雛形を保存しました: {f} ({n}件)",
        "eval.diff_header": "ベースライン比較 ({f})",
        "eval.diff_recall": "  recall  : {old} → {new} ({d})",
        "eval.diff_mrr": "  MRR     : {old} → {new} ({d})",
        "eval.diff_case": "  Δ {q}: recall {ro}→{rn}, MRR {mo}→{mn}",
        "eval.diff_matched": "  (差分は共通 {n} 件で計算)",
        "eval.diff_new": "  新規ケース {n}件 (ベースライン無し)",
        "eval.diff_dropped": "  削除ケース {n}件 (現実行に無し)",
        "eval.diff_k_warn": (
            "  注意: ベースラインは k={bk} で計測 (現実行 k={k})"
            " — 同条件での比較ではありません"
        ),
        "err.prefix": "エラー[{code}] {msg}",
        "reindex.done": "✓ {n}/{total} チャンクを再埋め込みしました",
        "reindex.no_embed": "埋め込みモデル未設定 (SHOIN_EMBED_MODEL)。スキップ。",
        "note.added": "追加: [{id}] {title}",
        "note.deleted": "ノート削除完了",
        "note.empty": "ノートがありません。`shoin note add <書院ID> <題> <本文>` で追加。",
        "src.deleted": "ソース削除完了",
        "src.renamed": "改名完了: [{id}] {title}",
        "src.refreshed": "✓ {title}: {chunks} chunks ({embedded} embedded)",
        "src.pages_failed": "⚠ {n} ページのテキスト抽出に失敗（索引は不完全です）",
        "src.refresh_all.done": (
            "一括再取込: {n}件 (更新 {refreshed} / 変更なし {unchanged} / "
            "skip {skipped} / 失敗 {failed})"
        ),
        "src.weighted": "重み設定完了: [{id}] weight={w} (0-8、検索スコアへ乗算)",
        "src.meta_set": "メタデータ設定完了: [{id}] {meta}",
        "chunk.edited": "チャンク更新完了: [{id}] (埋め込みクリア — reindexで再構築)",
        "health.version": "バージョン: {v}",
        "health.llm_ok": "LLM到達可能: {v}",
        "health.yes": "はい",
        "health.no": "いいえ",
        "health.model": "生成モデル: {v}",
        "health.embed_model": "埋め込みモデル: {v}",
        "health.embed_model_off": "(無効 — BM25のみ)",
        # v0.2.661 (product-review #17): the stored-vector builder model plus
        # the repair hint — matches the /api/health indexed_embed_model and
        # embed_model_changed fields.
        "health.indexed_embed_model": "索引済み埋め込みモデル: {v}",
        "health.embed_changed_hint": (
            "Warning: モデル変更を検出 — ベクトル検索停止中。"
            "`shoin reindex <nb>` で再構築"
        ),
        "health.multi_query": "マルチクエリ検索(SHOIN_MULTI_QUERY): {v}",
        "health.embed_batch": "埋め込みバッチサイズ(SHOIN_EMBED_BATCH): {v}",
        "health.chunking": (
            "チャンク設定(SHOIN_CHUNK_TOKENS/OVERLAP): "
            "{tokens}トークン/オーバーラップ{overlap}"
        ),
        "health.embed_batch_default": "{n} (既定)",
        "health.data_dir": "データベースファイル: {v}",
        "health.llm_url": "LLMエンドポイント: {v}",
        "stats.name": "ノートブック: {v}",
        "stats.sources": "ソース: {n}",
        "stats.chunks": "チャンク: {n}",
        "stats.notes": "ノート: {n}",
        "stats.messages": "メッセージ: {n}",
        "stats.studio_outputs": "Studio出力: {n}",
        "stats.db_bytes": "DBサイズ: {n}",
        "stats.metrics_header": "利用メトリクス:",
        "stats.metric_ask": "  ask: {n} 回 (degraded {d}・no-hit {z}・fail {f}・平均 {ms}ms)",
        "stats.metric_index": "  index: {n} 回 (fail {f}・embed-skip {e}・平均 {ms}ms)",
        "backup.done": "バックアップを保存しました: {path}",
    },
    "en": {
        "nb.created": "Created: [{id}] {name}",
        "nb.deleted": "Deleted",
        "nb.renamed": "Renamed: [{id}] {name}",
        "nb.settings_set": "Settings set: [{id}] {settings}",
        "nb.duplicated": "Duplicated: [{id}] {name}",
        "nb.merged": "Merged: [{id}] {name} (absorbed nb{src}, archived to trash)",
        "nb.imported": "Imported: [{id}] {name}",
        "trash.empty": "Trash is empty.",
        "trash.item": "[{id}] {kind} {name}  (nb_id {nb_id}, deleted {ts})",
        "trash.restored": "Restored: [{id}] {name}",
        "trash.purged": "Trash archive purged.",
        "trash.emptied": "Trash emptied: {n} archive(s).",
        "vacuum.done": "Vacuum done: {before} → {after} ({freed} reclaimed).",
        "stats.freelist": "Reclaimable free space: {n}",
        "nb.empty": "No notebooks. Create one with `shoin notebook new <name>`.",
        "msg.cleared": "Chat history cleared",
        "msg.empty": "No chat history.",
        "cite.invalid": "⚠ Invalid citations (out of range): {bad}",
        "cite.confirmed": " ✓ grounding confirmed",
        "cite.misattr": " ⚠ possible wrong source",
        "cite.numeric": " ⚠ number not in source",
        "cite.unit": " ⚠ unit differs from source",
        "cite.negation": " ⚠ possible contradiction with source",
        "cite.uncited": "⚠ Uncited assertions ({n}, no citation):",
        "cite.uncited_supported": "matches a source — missing citation",
        "cite.found": "found: ",
        "cite.found_fts": "full-text",
        "cite.found_vec": "semantic",
        "cite.found_lex": "lexical",
        "cite.degenerate": "⚠ Possible generation loop ({n}):",
        "cite.contradict": "⚠ Contradictory statements ({n}):",
        "cite.truncated": "⚠ Output may be truncated (token limit reached)",
        "cite.coverage_low": (
            "⚠ Low citation coverage: only {n}/{total} sources cited "
            "(the answer may not use all retrieved evidence)"
        ),
        "eval.header": "Retrieval quality (k={k}, {n} cases)",
        "search.header": "Cross-notebook hits ({n})",
        "search.hit": "  #{rank} [nb{nb_id} {nb}] {src} ({score}): {text}",
        "search.suggest": "Did you mean: {terms}",
        "eval.recall": "  recall  : {v}  (share of expected sources found in top-k)",
        "eval.mrr": "  MRR     : {v}  (reciprocal rank of the first expected source)",
        "eval.case_ok": "  ✓ {q}",
        "eval.case_ng": "  ✗ {q}",
        "eval.case_detail": "      expected={exp} retrieved={got}",
        "eval.case_missing": (
            "      warning: expected source id(s) {ids} not in this notebook"
            " (deleted or rekeyed?)"
        ),
        "eval.saved": "Baseline saved: {f}",
        "eval.gen_saved": "Wrote case skeleton: {f} ({n} cases)",
        "eval.diff_header": "Baseline comparison ({f})",
        "eval.diff_recall": "  recall  : {old} → {new} ({d})",
        "eval.diff_mrr": "  MRR     : {old} → {new} ({d})",
        "eval.diff_case": "  Δ {q}: recall {ro}→{rn}, MRR {mo}→{mn}",
        "eval.diff_matched": "  (deltas computed over {n} shared questions)",
        "eval.diff_new": "  {n} new case(s) (no baseline entry)",
        "eval.diff_dropped": "  {n} case(s) dropped (absent in this run)",
        "eval.diff_k_warn": (
            "  note: baseline was measured at k={bk} (current k={k})"
            " — not a like-for-like comparison"
        ),
        "err.prefix": "Error[{code}] {msg}",
        "reindex.done": "✓ Re-embedded {n}/{total} chunks",
        "reindex.no_embed": "No embedding model set (SHOIN_EMBED_MODEL). Skipped.",
        "note.added": "Added: [{id}] {title}",
        "note.deleted": "Note deleted",
        "note.empty": "No notes. Add one with `shoin note add <notebook_id> <title> <body>`.",
        "src.deleted": "Source deleted",
        "src.renamed": "Renamed: [{id}] {title}",
        "src.refreshed": "✓ {title}: {chunks} chunks ({embedded} embedded)",
        "src.pages_failed": "⚠ {n} page(s) could not be extracted — the index is incomplete",
        "src.refresh_all.done": (
            "refresh-all: {n} source(s) ({refreshed} refreshed / "
            "{unchanged} unchanged / {skipped} skipped / {failed} failed)"
        ),
        "src.weighted": "Weight set: [{id}] weight={w} (0-8, multiplies retrieval score)",
        "src.meta_set": "Metadata set: [{id}] {meta}",
        "chunk.edited": "Chunk updated: [{id}] (embedding cleared — run reindex to rebuild)",
        "health.version": "Version: {v}",
        "health.llm_ok": "LLM reachable: {v}",
        "health.yes": "yes",
        "health.no": "no",
        "health.model": "Chat model: {v}",
        "health.embed_model": "Embed model: {v}",
        "health.embed_model_off": "(disabled — BM25 only)",
        "health.indexed_embed_model": "Indexed embed model: {v}",
        "health.embed_changed_hint": (
            "Warning: model change detected — vector search paused. "
            "Run `shoin reindex <nb>` to rebuild."
        ),
        "health.multi_query": "Multi-query retrieval (SHOIN_MULTI_QUERY): {v}",
        "health.embed_batch": "Embed batch size (SHOIN_EMBED_BATCH): {v}",
        "health.chunking": (
            "Chunking (SHOIN_CHUNK_TOKENS/OVERLAP): "
            "{tokens} tokens / overlap {overlap}"
        ),
        "health.embed_batch_default": "{n} (default)",
        "health.data_dir": "Database file: {v}",
        "health.llm_url": "LLM endpoint: {v}",
        "stats.name": "Notebook: {v}",
        "stats.sources": "Sources: {n}",
        "stats.chunks": "Chunks: {n}",
        "stats.notes": "Notes: {n}",
        "stats.messages": "Messages: {n}",
        "stats.studio_outputs": "Studio outputs: {n}",
        "stats.db_bytes": "DB size: {n}",
        "stats.metrics_header": "Usage metrics:",
        "stats.metric_ask": "  ask: {n} calls (degraded {d}, no-hit {z}, failed {f}, avg {ms}ms)",
        "stats.metric_index": "  index: {n} calls (failed {f}, embed-skipped {e}, avg {ms}ms)",
        "backup.done": "Backup written: {path}",
    },
}


def _t(key: str, **kw: str) -> str:
    lang = ui_lang()
    if lang not in _STRINGS:
        lang = "en"
    tmpl = _STRINGS[lang].get(key) or _STRINGS["en"][key]
    return tmpl.format(**kw) if kw else tmpl


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shoin", description="Shoin (書院) — local notebook")
    p.add_argument("--version", action="version", version=f"shoin {VERSION}")
    p.add_argument("--db", default=None, help="データベースパス(既定: SHOIN_DATA_DIR)")
    sub = p.add_subparsers(dest="command", required=True)

    # REQ-105: the full subcommand surface — UI-free operation of every feature
    nb = sub.add_parser("notebook", help="ノートブック管理")
    nbsub = nb.add_subparsers(dest="action", required=True)
    nb_new = nbsub.add_parser("new", help="作成")
    nb_new.add_argument("name")
    nbsub.add_parser("list", help="一覧")
    nb_del = nbsub.add_parser("delete", help="削除")
    nb_del.add_argument("notebook_id", type=int)
    nb_ren = nbsub.add_parser("rename", help="改名")
    nb_ren.add_argument("notebook_id", type=int)
    nb_ren.add_argument("name")
    nb_dup = nbsub.add_parser("duplicate", help="複製(ソース・チャンク・ノート等を全コピー)")
    nb_dup.add_argument("notebook_id", type=int)
    nb_dup.add_argument("name", nargs="?", default=None)
    nb_mg = nbsub.add_parser(
        "merge", help="統合(別nbの中身を取込み、元nbはゴミ箱へアーカイブ)"
    )
    nb_mg.add_argument("notebook_id", type=int)
    nb_mg.add_argument("source_id", type=int)
    nb_set = nbsub.add_parser(
        "settings",
        help="nb単位設定 (top_k=N source_text_tokens=N; 引数なしで表示・--clearで消去)",
    )
    nb_set.add_argument("notebook_id", type=int)
    nb_set.add_argument("pairs", nargs="*", metavar="key=value")
    nb_set.add_argument("--clear", action="store_true")

    msgs = sub.add_parser("messages", help="チャット履歴管理")
    msgssub = msgs.add_subparsers(dest="action", required=True)
    msgs_list = msgssub.add_parser("list", help="一覧")
    msgs_list.add_argument("notebook_id", type=int)
    msgs_clear = msgssub.add_parser("clear", help="履歴クリア")
    msgs_clear.add_argument("notebook_id", type=int)

    add = sub.add_parser("add", help="ソース追加(ファイル/URL)")
    add.add_argument("notebook_id", type=int)
    add.add_argument("targets", nargs="+")

    ri = sub.add_parser("reindex", help="ノートブックの埋め込みを再構築")
    ri.add_argument("notebook_id", type=int)

    note = sub.add_parser("note", help="ノート管理")
    notesub = note.add_subparsers(dest="action", required=True)
    note_add = notesub.add_parser("add", help="追加")
    note_add.add_argument("notebook_id", type=int)
    note_add.add_argument("title")
    note_add.add_argument("body")
    note_list = notesub.add_parser("list", help="一覧")
    note_list.add_argument("notebook_id", type=int)
    note_del = notesub.add_parser("delete", help="削除")
    note_del.add_argument("note_id", type=int)

    src = sub.add_parser("source", help="ソース管理")
    srcsub = src.add_subparsers(dest="action", required=True)
    src_del = srcsub.add_parser("delete", help="削除")
    src_del.add_argument("source_id", type=int)
    src_ren = srcsub.add_parser("rename", help="改名")
    src_ren.add_argument("source_id", type=int)
    src_ren.add_argument("title")
    src_ref = srcsub.add_parser("refresh", help="URLソースの再取込")
    src_ref.add_argument("source_id", type=int)
    src_ra = srcsub.add_parser(
        "refresh-all", help="ノートブック内の全ソースを一括再取込 (cron向け)"
    )
    src_ra.add_argument("notebook_id", type=int)
    src_w = srcsub.add_parser(
        "weight", help="検索重みの設定 (1.0=標準・>1優遇・<1降格・0で下位固定)"
    )
    src_w.add_argument("source_id", type=int)
    src_w.add_argument("weight", type=float)
    src_m = srcsub.add_parser(
        "meta",
        help="ソースメタデータ (author=… year=…; 引数なしで表示・--clearで消去)",
    )
    src_m.add_argument("source_id", type=int)
    src_m.add_argument("pairs", nargs="*", metavar="key=value")
    src_m.add_argument("--clear", action="store_true")

    chk = sub.add_parser("chunk", help="チャンク管理")
    chksub = chk.add_subparsers(dest="action", required=True)
    chk_edit = chksub.add_parser("edit", help="抽出テキスト修正(埋込みクリア→reindexで再構築)")
    chk_edit.add_argument("chunk_id", type=int)
    chk_edit.add_argument("text")

    askp = sub.add_parser("ask", help="ソース限定Q&A")
    askp.add_argument("notebook_id", type=int)
    askp.add_argument("question")
    # default=None lets the notebook's own settings.top_k decide (v0.2.659):
    # an explicit -k still wins over the setting.
    askp.add_argument(
        "-k", type=_pos_int, default=None, help="検索深さ (省略時: nb設定か既定値)"
    )
    askp.add_argument(
        "--source",
        dest="source_ids",
        action="append",
        type=_pos_int,
        metavar="ID",
        help="このソースIDのみを検索対象にする(複数回指定可)",
    )

    st = sub.add_parser("studio", help="Studio出力生成")
    st.add_argument("notebook_id", type=int)
    st.add_argument("kind", choices=KINDS)

    q = sub.add_parser("questions", help="推奨質問の提案")
    q.add_argument("notebook_id", type=int)

    srch = sub.add_parser("search", help="全ノートブック横断検索")
    srch.add_argument("question")
    srch.add_argument("-k", type=_pos_int, default=TOP_K, help="検索深さ")

    ev = sub.add_parser("eval", help="検索精度を測定 (recall/MRR)")
    ev.add_argument("notebook_id", type=int)
    ev.add_argument(
        "cases", nargs="?", help='JSONファイル: [{"q": "質問", "sources": [1, 2]}]'
    )
    ev.add_argument("-k", type=_pos_int, default=TOP_K, help="検索深さ")
    ev.add_argument("--save", metavar="FILE", help="この実行をベースラインJSONとして保存")
    ev.add_argument("--diff", metavar="FILE", help="保存済みベースラインとの差分を表示")
    ev.add_argument(
        "--gen",
        nargs="?",
        const="-",
        metavar="FILE",
        help="ノートブックからケース雛形を生成 (--gen単独=stdout、--gen FILE=保存)",
    )

    ex = sub.add_parser("export", help="エクスポート")
    ex.add_argument("notebook_id", type=int)
    # "tree" (v0.2.655): the machine-transfer document — a JSON notebook
    # tree, not a human-readable export format, so it stays out of
    # export.FORMATS and its mime/ext/lockstep pins.
    ex.add_argument("--format", choices=(*FORMATS, "tree"), default="md")

    imp = sub.add_parser(
        "import", help="インポート(export --format tree のJSONからnbを復元)"
    )
    imp.add_argument("file", help="export JSONファイル ('-' でstdin)")

    sv = sub.add_parser("serve", help="Web UI起動 (127.0.0.1のみ)")
    sv.add_argument("--port", type=_port_num, default=port(), help=f"ポート(既定: {port()})")

    sub.add_parser("health", help="設定・LLM到達性を表示 (headless diagnostics)")

    stt = sub.add_parser("stats", help="ノートブック統計 (ソース/チャンク/DBサイズ)")
    stt.add_argument("notebook_id", type=int)

    bk = sub.add_parser("backup", help="DBをバックアップ (オンラインスナップショット)")
    bk.add_argument("dest")

    tr = sub.add_parser("trash", help="ゴミ箱 (削除済みnbの一覧・復元・完全削除)")
    trsub = tr.add_subparsers(dest="action", required=True)
    trsub.add_parser("list", help="一覧")
    tr_res = trsub.add_parser("restore", help="元id・埋込み込みで復元")
    tr_res.add_argument("trash_id", type=int)
    tr_pur = trsub.add_parser("purge", help="アーカイブを完全削除(復元不可)")
    tr_pur.add_argument("trash_id", type=int)
    trsub.add_parser("empty", help="ゴミ箱を空にする(全アーカイブ完全削除・復元不可)")

    sub.add_parser("vacuum", help="DBをVACUUMして削除済み領域をOSへ返却")
    return p


def _print_report(report: CitationReport) -> None:
    if report["invalid"]:
        bad = ", ".join(f"S{i}" for i in report["invalid"])
        print(_t("cite.invalid", bad=bad))
    confirmed: set[int] = set(report.get("confirmed") or [])
    misattr: set[int] = set(report.get("misattributed") or [])
    numeric: set[int] = set(report.get("numeric_mismatch") or [])
    unit: set[int] = set(report.get("unit_mismatch") or [])
    negation: set[int] = set(report.get("negation_mismatch") or [])
    # Section breadcrumb per cited source (v0.2.131) — completes v0.2.130's
    # in-app seal viewer and the Markdown export on the CLI surface too, so a
    # headless `shoin ask` user sees WHICH section each citation is grounded in
    # (REQ-103 CLI/Web parity). Absent on old reports/no-heading sources.
    raw_ctx = report.get("source_contexts")
    section_map: dict[str, str] = raw_ctx if isinstance(raw_ctx, dict) else {}
    # Retrieval provenance per cited source (v0.2.229) — the same "which channel
    # surfaced it" signal the seal viewer shows, on the headless surface too
    # (REQ-103 parity). Absent on old reports.
    raw_det = report.get("source_detail")
    detail_map: dict[str, dict[str, float]] = (
        raw_det if isinstance(raw_det, dict) else {}
    )
    for c in report["cited"]:
        title = report["source_map"].get(f"S{c}", "")
        section = section_map.get(f"S{c}", "")
        sec = f" (§ {section})" if section else ""
        bits = [
            f"{_t('cite.found_' + kind)} #{int(v)}"
            if kind != "lex"
            else f"{_t('cite.found_' + kind)} {v:.2f}"
            for kind, v in found_bits(detail_map.get(f"S{c}"))
        ]
        prov = f" [{_t('cite.found')}{' + '.join(bits)}]" if bits else ""
        if c in confirmed:
            marker = _t("cite.confirmed")
        elif c in misattr:
            # Wrong number — append the source the claim actually matches when
            # the report names one (v0.2.220): the fix becomes a one-char edit.
            sugg = (report.get("misattributed_suggested") or {}).get(f"S{c}")
            marker = _t("cite.misattr") + (f"→{sugg}" if sugg else "")
        elif c in numeric:
            marker = _t("cite.numeric")
        elif c in unit:
            marker = _t("cite.unit")
        elif c in negation:
            marker = _t("cite.negation")
        else:
            marker = ""
        print(f"  [S{c}] {_one_line(title)}{sec}{prov}{marker}")
    uncited = report.get("uncited") or []
    if uncited:
        supported = set(report.get("uncited_supported") or [])
        print(_t("cite.uncited", n=str(len(uncited))))
        sup_src = report.get("uncited_supported_source") or {}
        for sentence in uncited:
            # Grounded uncited = citation omission; ungrounded = the dangerous kind.
            mark = (
                f" [{_t('cite.uncited_supported')}→{_one_line(str(sup_src.get(sentence, '')))}]"
                if sentence in supported
                else ""
            )
            print(f"  - {_one_line(sentence)}{mark}")
    degenerate = report.get("degenerate") or []
    if degenerate:
        print(_t("cite.degenerate", n=str(len(degenerate))))
        for snippet in degenerate:
            print(f"  - {_one_line(snippet)}")
    contradict = report.get("self_contradiction") or []
    if contradict:
        print(_t("cite.contradict", n=str(len(contradict))))
        for sentence in contradict:
            print(f"  - {_one_line(sentence)}")
    if report.get("truncated"):
        # finish_reason "length": generation stopped at the token limit — the
        # report flag the Web badge and export status line already carry.
        print(_t("cite.truncated"))
    # Low coverage = the answer cited only a small share of the sources it was
    # given, i.e. it may be ignoring retrieved evidence. The Web UI has warned
    # about this since early on; the CLI silently dropped it despite REQ-103
    # CLI/Web parity (v0.2.138).
    cov = report.get("coverage")
    n_sources = report.get("n_sources") or 0
    if isinstance(cov, (int, float)) and report["cited"] and n_sources and cov < COVERAGE_LOW:
        print(_t("cite.coverage_low", n=str(len(set(report["cited"]))), total=str(n_sources)))


def _report_has_output(report: CitationReport) -> bool:
    """True iff _print_report() would print anything for this report.

    The ask/studio "---" guards must neither print a bare separator over an
    empty report (v0.2.27/55) nor skip a report over keys the printer renders
    but the guard forgot — invalid/uncited/truncated were added piecemeal
    (v0.2.55, v0.2.245), and degenerate/self_contradiction were still missing:
    an answer of repeated questions or disclaimers produces a degenerate-only
    report (uncited filters questions/disclaimers), so its generation-loop
    warning vanished on the CLI while the Web badge showed it. One predicate
    shared by both call sites keeps the guard and the printer from drifting
    a third time. `coverage` prints only when `cited` is non-empty, so it is
    covered transitively."""
    return bool(
        report["invalid"]
        or report["cited"]
        or report.get("uncited")
        or report.get("degenerate")
        or report.get("self_contradiction")
        or report.get("truncated")
    )


def _db_arg(args: argparse.Namespace) -> str | None:
    """The --db override with ~ expansion. Shell only expands a tilde at word
    start, so `--db=~/x.db` arrives literally and Path() would create a real
    `~` directory in the cwd — while SHOIN_DATA_DIR is already expanded inside
    db_path(). Every Path(str(args.*)) in this module expands the same way."""
    return str(Path(str(args.db)).expanduser()) if args.db else None


def _cmd_health(llm: ChatBackend, db: str | None = None) -> int:
    """Headless equivalent of GET /api/health (REQ-103 CLI parity) — a user
    running only the CLI previously had no way to check LLM reachability or
    confirm SHOIN_MULTI_QUERY/SHOIN_EMBED_BATCH actually took effect without
    starting the Web server or reading source/env directly.

    *db* is the top-level --db override, the same value main() passes to
    Store() for every other subcommand — health must report the SAME
    effective database path this invocation would actually use, not always
    the config-derived default, or a user diagnosing "why isn't my custom
    --db setup working" is told about a path this invocation never touches.
    """
    from .pipeline import EMBED_BATCH as _default_embed_batch

    avail = getattr(llm, "available", lambda: False)()
    print(_t("health.version", v=VERSION))
    print(_t("health.llm_url", v=_one_line(llm_url())))
    print(_t("health.llm_ok", v=_t("health.yes") if avail else _t("health.no")))
    print(_t("health.model", v=_one_line(llm_model())))
    em = embed_model()
    print(_t("health.embed_model", v=em if em.strip() else _t("health.embed_model_off")))
    # v0.2.661 (product-review #17): name the model that built the stored
    # vectors; when it diverges from the configured one, vector search is
    # silently disabled — print the same repair hint ask/search emit.
    # Best-effort like _h_health: `shoin health` must keep answering even
    # when the data dir itself is the broken thing being diagnosed.
    indexed = ""
    try:
        with Store(db or db_path()) as store:
            indexed = (store.get_setting(EMBED_MODEL_SETTING_KEY) or "").strip()
    except (OSError, StoreError, sqlite3.OperationalError):
        # sqlite3.connect propagates raw OSErrors for unopenable paths.
        pass
    print(
        _t(
            "health.indexed_embed_model",
            v=indexed if indexed else _t("health.embed_model_off"),
        )
    )
    if indexed and em.strip() and indexed != em.strip():
        print(_t("health.embed_changed_hint"), file=sys.stderr)
    mq = _t("health.yes") if multi_query_enabled() else _t("health.no")
    print(_t("health.multi_query", v=mq))
    batch = embed_batch()
    batch_v = str(batch) if batch is not None else _t(
        "health.embed_batch_default", n=str(_default_embed_batch)
    )
    print(_t("health.embed_batch", v=batch_v))
    print(_t("health.chunking", tokens=str(chunk_tokens()), overlap=str(chunk_overlap())))
    print(_t("health.data_dir", v=_one_line(db if db else str(db_path()))))
    return 0


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"


def _cmd_stats(store: Store, args: argparse.Namespace) -> int:
    nb = store.get_notebook(int(args.notebook_id))
    s = store.notebook_stats(nb.id)
    print(_t("stats.name", v=nb.name))
    print(_t("stats.sources", n=str(s["sources"])))
    print(_t("stats.chunks", n=str(s["chunks"])))
    print(_t("stats.notes", n=str(s["notes"])))
    print(_t("stats.messages", n=str(s["messages"])))
    print(_t("stats.studio_outputs", n=str(s["studio_outputs"])))
    print(_t("stats.db_bytes", n=_human_bytes(store.db_bytes())))
    print(_t("stats.freelist", n=_human_bytes(store.freelist_bytes())))
    m = store.usage_metrics()
    if m:
        # Durable content-free counters (see Store.bump_metrics): totals since
        # first use, not this session — the in-product half of SHOIN_LOG_JSON.
        print(_t("stats.metrics_header"))
        asks = int(m.get("ask.count", 0.0))
        print(
            _t(
                "stats.metric_ask",
                n=str(asks),
                d=str(int(m.get("ask.degraded", 0.0))),
                z=str(int(m.get("ask.nohit", 0.0))),
                f=str(int(m.get("ask.fail", 0.0))),
                ms=str(round(m.get("ask.ms", 0.0) / asks)) if asks else "0",
            )
        )
        idx = int(m.get("index.ok", 0.0))
        print(
            _t(
                "stats.metric_index",
                n=str(idx),
                f=str(int(m.get("index.fail", 0.0))),
                e=str(int(m.get("index.embed_skip", 0.0))),
                ms=str(round(m.get("index.ms", 0.0) / idx)) if idx else "0",
            )
        )
    return 0


def _cmd_backup(store: Store, args: argparse.Namespace) -> int:
    dest = Path(args.dest).expanduser()
    store.backup_to(dest)
    print(_t("backup.done", path=str(dest)))
    return 0


def _cmd_import(store: Store, args: argparse.Namespace) -> int:
    """`shoin import <file>` (v0.2.655) — rebuild a notebook from an
    `export --format tree` document under fresh ids. "-" reads stdin so
    export|import pipes work."""
    import json

    try:
        if str(args.file) == "-":
            raw_text = sys.stdin.read()
        else:
            raw_text = Path(str(args.file)).expanduser().read_text(encoding="utf-8")
        raw = json.loads(raw_text)
    except OSError as exc:
        raise StoreError(
            "SYSTEM_IO_ERROR", f"cannot read export file: {exc}"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Same classification as _cmd_eval's cases file: a non-UTF-8 or
        # non-JSON file is a 400-class input defect, never a traceback.
        raise StoreError(
            "NOTEBOOK_IMPORT_INVALID", f"export file is not valid JSON: {exc}"
        ) from exc
    nb = store.import_notebook(raw)
    print(_t("nb.imported", id=str(nb.id), name=_one_line(nb.name)))
    return 0


def _cmd_eval(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    """Measure retrieval quality on user-authored cases.

    Runs the SAME retrieval path ask() uses, so toggling SHOIN_MULTI_QUERY (or
    any other setting) and re-running compares what the user will really get —
    turning "the literature says X helps" into "it helps on MY notebook, or it
    doesn't". See shoin/evaluate.py for why this exists.
    """
    import json

    from .evaluate import evaluate, gen_cases, parse_cases

    if args.gen is not None:
        # --gen (product-review #44): scaffold one skeleton case per chunked
        # source — the sources array carries the real data (this notebook's
        # actual ids, the part hand-authoring gets wrong); the title-derived
        # question is a seed for hand-editing, not a measured ground truth.
        skeleton = gen_cases(store, int(args.notebook_id))
        payload = json.dumps(skeleton, ensure_ascii=False, indent=1) + "\n"
        if args.gen == "-":
            print(payload, end="")
        else:
            Path(str(args.gen)).expanduser().write_text(payload, encoding="utf-8")
            print(
                _t(
                    "eval.gen_saved",
                    f=_one_line(str(args.gen)),
                    n=str(len(skeleton)),
                )
            )
        return 0
    if args.cases is None:
        raise StoreError(
            "VALIDATION_REQUIRED_FIELD_MISSING",
            "missing cases file (or --gen to scaffold one)",
        )
    try:
        raw = json.loads(Path(str(args.cases)).expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        raise StoreError("SYSTEM_IO_ERROR", f"cannot read cases file: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # UnicodeDecodeError comes from read_text's strict UTF-8 decode — a
        # non-UTF-8 file is definitionally not JSON, and neither it nor
        # JSONDecodeError is an OSError, so without this both escape main()'s
        # handler chain as a raw traceback.
        raise StoreError(
            "VALIDATION_FIELD_FORMAT_INVALID",
            f"cases file is not valid JSON: {exc}",
        ) from exc
    try:
        cases = parse_cases(raw)
    except ValueError as exc:
        raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", str(exc)) from exc
    rep = evaluate(store, llm, int(args.notebook_id), cases, k=int(args.k))
    print(_t("eval.header", k=str(args.k), n=str(len(rep.cases))))
    print(_t("eval.recall", v=f"{rep.recall:.3f}"))
    print(_t("eval.mrr", v=f"{rep.mrr:.3f}"))
    for c in rep.cases:
        ok = c.recall >= 1.0
        print(_t("eval.case_ok" if ok else "eval.case_ng", q=_one_line(c.question)))
        if not ok:
            print(_t("eval.case_detail", exp=str(c.expected), got=str(c.retrieved)))
            if c.missing:
                print(_t("eval.case_missing", ids=str(c.missing)))
    if args.save:
        from .evaluate import report_to_dict

        Path(str(args.save)).expanduser().write_text(
            json.dumps(report_to_dict(rep, int(args.k)), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(_t("eval.saved", f=_one_line(str(args.save))))
    if args.diff:
        from .evaluate import diff_reports, report_from_dict

        try:
            base_raw = json.loads(Path(str(args.diff)).expanduser().read_text(encoding="utf-8"))
        except OSError as exc:
            raise StoreError("SYSTEM_IO_ERROR", f"cannot read baseline file: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreError(
                "VALIDATION_FIELD_FORMAT_INVALID", f"baseline file is not valid JSON: {exc}"
            ) from exc
        try:
            base, base_k = report_from_dict(base_raw)
        except ValueError as exc:
            raise StoreError("VALIDATION_FIELD_FORMAT_INVALID", str(exc)) from exc
        diff = diff_reports(base, rep)
        print(_t("eval.diff_header", f=_one_line(str(args.diff))))
        # The comparison rows print the means over the MATCHED questions — the
        # same population the deltas were computed on. Printing the full-run
        # means (base.recall / rep.recall) would show e.g. 0.500 → 1.000 next
        # to (+0.000): two different populations labeled as one comparison.
        print(
            _t(
                "eval.diff_recall",
                old=f"{diff.recall_before:.3f}",
                new=f"{diff.recall_after:.3f}",
                d=f"{diff.d_recall:+.3f}",
            )
        )
        print(
            _t(
                "eval.diff_mrr",
                old=f"{diff.mrr_before:.3f}",
                new=f"{diff.mrr_after:.3f}",
                d=f"{diff.d_mrr:+.3f}",
            )
        )
        if base_k is not None and base_k != int(args.k):
            print(_t("eval.diff_k_warn", bk=str(base_k), k=str(args.k)))
        for cd in diff.case_deltas:
            print(
                _t(
                    "eval.diff_case",
                    q=_one_line(cd.question),
                    ro=f"{cd.recall_before:.3f}",
                    rn=f"{cd.recall_after:.3f}",
                    mo=f"{cd.rr_before:.3f}",
                    mn=f"{cd.rr_after:.3f}",
                )
            )
        if diff.new_questions or diff.dropped_questions:
            print(_t("eval.diff_matched", n=str(diff.matched_questions)))
        if diff.new_questions:
            print(_t("eval.diff_new", n=str(len(diff.new_questions))))
        if diff.dropped_questions:
            print(_t("eval.diff_dropped", n=str(len(diff.dropped_questions))))
    return 0


def _cmd_notebook(store: Store, args: argparse.Namespace) -> int:
    action = str(args.action)
    if action == "new":
        nb = store.create_notebook(str(args.name))
        print(_t("nb.created", id=str(nb.id), name=_one_line(nb.name)))
    elif action == "list":
        rows = store.list_notebooks_with_counts()
        if not rows:
            print(_t("nb.empty"))
        for row in rows:
            c = row["counts"]
            print(
                f"[{row['id']}] {_one_line(row['name'])}"
                f"  sources={c['sources']} chunks={c['chunks']}"
            )
    elif action == "delete":
        store.delete_notebook(int(args.notebook_id))
        print(_t("nb.deleted"))
    elif action == "rename":
        store.rename_notebook(int(args.notebook_id), str(args.name))
        # rename_notebook() strips whitespace before persisting — echo the same
        # stripped value here, not the raw CLI argument, matching the v0.2.93-95
        # fix already applied to this action's sibling, source rename, below.
        print(_t("nb.renamed", id=str(args.notebook_id), name=_one_line(str(args.name).strip())))
    elif action == "duplicate":
        nb = store.duplicate_notebook(int(args.notebook_id), args.name)
        print(_t("nb.duplicated", id=str(nb.id), name=_one_line(nb.name)))
    elif action == "merge":
        nb = store.merge_notebooks(int(args.notebook_id), int(args.source_id))
        print(
            _t(
                "nb.merged",
                id=str(nb.id),
                name=_one_line(nb.name),
                src=str(args.source_id),
            )
        )
    elif action == "settings":
        import json
        # Same key-level merge surface as `source meta`: read the current
        # object, merge int-valued `key=value` pairs, write back; --clear
        # removes the whole override object.
        nb0 = store.get_notebook(int(args.notebook_id))
        if not args.pairs and not args.clear:
            print(json.dumps(nb0.settings, ensure_ascii=False, sort_keys=True))
        else:
            settings = {} if args.clear else dict(nb0.settings)
            for pair in args.pairs:
                key, sep, value = pair.partition("=")
                if not sep or not key.strip():
                    raise StoreError(
                        "VALIDATION_FIELD_FORMAT_INVALID",
                        f"settings pair must be key=value, got {pair!r}",
                    )
                try:
                    settings[key.strip()] = int(value)
                except ValueError:
                    raise StoreError(
                        "VALIDATION_FIELD_FORMAT_INVALID",
                        f"settings.{key.strip()} must be an integer, got {value!r}",
                    ) from None
            store.update_notebook_settings(int(args.notebook_id), settings)
            print(
                _t(
                    "nb.settings_set",
                    id=str(args.notebook_id),
                    settings=json.dumps(settings, ensure_ascii=False, sort_keys=True),
                )
            )
    return 0


def _cmd_trash(store: Store, args: argparse.Namespace) -> int:
    action = str(args.action)
    if action == "list":
        rows = store.trash_list()
        if not rows:
            print(_t("trash.empty"))
        for r in rows:
            print(
                _t(
                    "trash.item",
                    id=str(r["id"]),
                    kind=r["kind"],
                    name=_one_line(r["name"]),
                    nb_id=str(r["notebook_id"]),
                    ts=r["deleted_at"],
                )
            )
    elif action == "restore":
        res = store.trash_restore(int(args.trash_id))
        print(_t("trash.restored", id=str(res["id"]), name=_one_line(res["name"])))
    elif action == "purge":
        store.trash_purge(int(args.trash_id))
        print(_t("trash.purged"))
    elif action == "empty":
        n = store.trash_purge_all()
        print(_t("trash.emptied", n=str(n)))
    return 0


def _cmd_vacuum(store: Store, args: argparse.Namespace) -> int:
    res = store.vacuum()
    print(
        _t(
            "vacuum.done",
            before=_human_bytes(res["before"]),
            after=_human_bytes(res["after"]),
            freed=_human_bytes(res["freed"]),
        )
    )
    return 0


def _cmd_messages(store: Store, args: argparse.Namespace) -> int:
    action = str(args.action)
    if action == "list":
        # Every mutating sibling (add/clear/ask/studio/eval) validates the
        # notebook up front; list_messages() does not, so a typo'd id would
        # print "no chat history" and read as an existing-but-empty notebook —
        # the empty-vs-nonexistent conflation the API's 404 avoids.
        store.get_notebook(int(args.notebook_id))
        messages = store.list_messages(int(args.notebook_id))
        if not messages:
            print(_t("msg.empty"))
        for m in messages:
            print(f"[{m['id']}] {m['role']}: {m['body']}")
    elif action == "clear":
        store.clear_messages(int(args.notebook_id))
        print(_t("msg.cleared"))
    return 0


def _cmd_add(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    rc = 0
    # expanduser() on each target: `add` takes positional args so word-start
    # `~/x.md` is shell-expanded already, but a quoted '~/x.md' arrives
    # literally — same contract as every other Path(str(*)) in this module.
    # Only ~-prefixed targets go through Path(): it collapses a URL's "//".
    for target in [
        str(Path(str(t)).expanduser()) if str(t).startswith("~") else str(t)
        for t in args.targets
    ]:
        try:
            result = index_source(store, int(args.notebook_id), target, llm)
            print(
                f"✓ {_one_line(result.source.title)}: {result.n_chunks}"
                f" chunks ({result.n_embedded} embedded)"
            )
            if result.pages_failed:
                # Don't report a partial index as complete: the graceful
                # per-page PDF fallback drops failed pages silently.
                print(
                    _t("src.pages_failed", n=str(result.pages_failed)),
                    file=sys.stderr,
                )
        except (IngestError, StoreError) as exc:
            print(f"✗ {_one_line(target)}: [{exc.code}] {exc}", file=sys.stderr)
            rc = 1
        except sqlite3.OperationalError as exc:
            print(f"✗ {_one_line(target)}: [SYSTEM_DB_LOCKED] {exc}", file=sys.stderr)
            rc = 1
    return rc


def _cmd_ask(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    # Strip + reject empty, matching the API's _require("question") contract —
    # otherwise a whitespace-only question is persisted as a real user turn and
    # silently answered via the degraded path instead of refused like the API.
    question = str(args.question).strip()
    if not question:
        raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "missing field: question")
    if len(question) > MAX_QUESTION_LEN:
        raise StoreError(
            "VALIDATION_FIELD_FORMAT_INVALID",
            f"question too long (max {MAX_QUESTION_LEN} characters)",
        )
    deltas: list[str] = []

    def _emit(delta: str) -> None:
        print(delta, end="", flush=True)
        deltas.append(delta)

    answer = ask(
        store,
        llm,
        int(args.notebook_id),
        question,
        k=args.k,
        source_ids=args.source_ids,
        on_delta=_emit,
    )
    streamed = "".join(deltas)
    if deltas:
        print()  # end the streamed answer's line
    if streamed != answer.text:
        # No streaming capability (deltas empty → "" ≠ text) or the stream died
        # mid-answer into the degraded path — the already-emitted partial stays
        # visible and the final answer is printed in full, matching the SSE
        # contract that partial text is real and persisted.
        print(answer.text)
    # A non-degraded answer can still legitimately carry an empty report — e.g.
    # the model correctly follows the system prompt's "say so explicitly" rule
    # for a fact not in the sources, which uncited_sentences() deliberately
    # excludes from `uncited` (citation.py's _DISCLAIMER_MARKERS). Printing a
    # bare "---" with nothing under it is the same defect v0.2.27/v0.2.55 fixed
    # elsewhere; guard on actual report content too, not just hits/degraded.
    if answer.hits and not answer.degraded and _report_has_output(answer.report):
        print("---")
        _print_report(answer.report)
    return 0


def _cmd_studio(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    result = generate(store, llm, int(args.notebook_id), str(args.kind))
    print(result.body)
    if _report_has_output(result.report):
        print("---")
        _print_report(result.report)
    return 0


def _cmd_questions(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    for q in suggest_questions(store, llm, int(args.notebook_id)):
        print(f"- {q}")
    return 0


def _cmd_search(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    """Cross-notebook retrieval (v0.2.649): the same pipeline ask() runs, with
    notebook_id=None — every source in the DB is a candidate and each hit is
    prefixed by its owning notebook so the user can route back to `ask`."""
    question = str(args.question).strip()
    if not question:
        raise StoreError("VALIDATION_REQUIRED_FIELD_MISSING", "missing field: question")
    if len(question) > MAX_QUESTION_LEN:
        raise StoreError(
            "VALIDATION_FIELD_FORMAT_INVALID",
            f"question too long (max {MAX_QUESTION_LEN} characters)",
        )
    retrieval_q = expand_query(question, [])
    qvec = (
        _query_vector(llm, retrieval_q)
        if _check_embed_model_ok(store, llm)
        else None
    )
    hits = retrieve_for_question(store, llm, None, retrieval_q, qvec, k=int(args.k))
    meta = store.notebooks_for_sources([h.source_id for h in hits])
    print(_t("search.header", n=str(len(hits))))
    if not hits:
        suggestions = suggest_corrections(store, None, question)
        if suggestions:
            print(_t("search.suggest", terms=" ".join(suggestions)))
    for i, h in enumerate(hits):
        nb_id, nb_name, title = meta.get(h.source_id, (0, "", ""))
        print(
            _t(
                "search.hit",
                rank=str(i + 1),
                nb_id=str(nb_id),
                nb=_one_line(nb_name),
                src=_one_line(title),
                score=f"{h.score:.2f}",
                text=_one_line(h.text[:80]),
            )
        )
    return 0


def _cmd_reindex(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    if not (llm.embedding_model or "").strip():
        print(_t("reindex.no_embed"), file=sys.stderr)
        return 1
    n, total = reindex_notebook(store, llm, int(args.notebook_id))
    print(_t("reindex.done", n=str(n), total=str(total)))
    return 0


def _cmd_note(store: Store, args: argparse.Namespace) -> int:
    action = str(args.action)
    if action == "add":
        # add_note() strips whitespace before persisting (store.py); echo the
        # same stripped value here, not the raw CLI argument, matching the
        # v0.2.93/94/95/99 fix applied to this codebase's other echo sites.
        title = str(args.title).strip()
        note_id = store.add_note(int(args.notebook_id), title, str(args.body))
        print(_t("note.added", id=str(note_id), title=_one_line(title)))
    elif action == "list":
        # Same empty-vs-nonexistent contract as `shoin messages list` above:
        # add_note() validates via get_notebook() but list_notes() does not.
        store.get_notebook(int(args.notebook_id))
        notes = store.list_notes(int(args.notebook_id))
        if not notes:
            print(_t("note.empty"))
        for n in notes:
            print(f"[{n['id']}] {_one_line(n['title'])}")
    elif action == "delete":
        store.delete_note(int(args.note_id))
        print(_t("note.deleted"))
    return 0


def _cmd_source(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    action = str(args.action)
    if action == "delete":
        store.delete_source(int(args.source_id))
        print(_t("src.deleted"))
    elif action == "rename":
        src = store.get_source(int(args.source_id))
        # rename_source refreshes the embeddings the old title is baked into as
        # well as the row + FTS context (v0.2.160); best-effort, never fatal.
        rename_source(store, src.id, str(args.title), src.origin, llm)
        # update_source_title() silently truncates to MAX_TITLE_LEN before
        # persisting (config.py: "source titles silently truncated") — echo
        # the same truncated value here, not the raw CLI argument, matching
        # the v0.2.93/94 fix applied to this endpoint's Web API siblings.
        printed_title = str(args.title).strip()[:MAX_TITLE_LEN]
        print(_t("src.renamed", id=str(src.id), title=_one_line(printed_title)))
    elif action == "refresh":
        result = refresh_source(store, int(args.source_id), llm)
        print(
            _t(
                "src.refreshed",
                title=_one_line(result.source.title),
                chunks=str(result.n_chunks),
                embedded=str(result.n_embedded),
            )
        )
        if result.pages_failed:
            print(_t("src.pages_failed", n=str(result.pages_failed)), file=sys.stderr)
    elif action == "refresh-all":
        results = refresh_all_sources(store, int(args.notebook_id), llm)
        tally: dict[str, int] = {}
        for r in results:
            status = str(r["status"])
            tally[status] = tally.get(status, 0) + 1
            line = f"[{r['id']}] {r['title']}: {status}"
            if status == "failed":
                line += f" ({r['code']})"
            print(line)
        print(
            _t(
                "src.refresh_all.done",
                n=str(len(results)),
                refreshed=str(tally.get("refreshed", 0)),
                unchanged=str(tally.get("unchanged", 0)),
                skipped=str(tally.get("skipped", 0)),
                failed=str(tally.get("failed", 0)),
            )
        )
    elif action == "weight":
        # update_source_weight raises the coded StoreError for range/type —
        # argparse's type=float accepts nan/inf, so the store bound is the
        # boundary (same single-validation contract as the API's validator).
        store.update_source_weight(int(args.source_id), float(args.weight))
        print(
            _t(
                "src.weighted",
                id=str(args.source_id),
                w=str(float(args.weight)),
            )
        )
    elif action == "meta":
        import json
        # Key-level merge surface over the store's whole-object REPLACE:
        # read the current object, merge `key=value` pairs, write back.
        # `--clear` is the remove-class complement — key deletion is the
        # empty-object special case of replace semantics, not a third verb.
        src = store.get_source(int(args.source_id))
        if not args.pairs and not args.clear:
            print(json.dumps(src.meta, ensure_ascii=False, sort_keys=True))
        else:
            meta = {} if args.clear else dict(src.meta)
            for pair in args.pairs:
                key, sep, value = pair.partition("=")
                if not sep or not key.strip():
                    raise StoreError(
                        "VALIDATION_FIELD_FORMAT_INVALID",
                        f"meta pair must be key=value, got {pair!r}",
                    )
                meta[key.strip()] = value
            store.update_source_meta(int(args.source_id), meta)
            print(
                _t(
                    "src.meta_set",
                    id=str(args.source_id),
                    meta=json.dumps(meta, ensure_ascii=False, sort_keys=True),
                )
            )
    return 0


def _cmd_chunk(store: Store, args: argparse.Namespace) -> int:
    action = str(args.action)
    if action == "edit":
        chunk = store.update_chunk_text(int(args.chunk_id), str(args.text))
        print(_t("chunk.edited", id=str(chunk.id)))
    return 0


def main(argv: Sequence[str] | None = None, llm: ChatBackend | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if str(args.command) == "serve":
        from .server import serve

        try:
            serve(int(args.port), _db_arg(args))
        except OSError as exc:
            print(_t("err.prefix", code="SYSTEM_PORT_IN_USE", msg=str(exc)), file=sys.stderr)
            return 1
        return 0
    backend: ChatBackend = llm if llm is not None else LLMClient()
    if str(args.command) == "health":
        # No Store needed: health is a diagnostic of config/LLM reachability,
        # useful precisely when the data directory itself might be the problem
        # (mirrors `serve` being special-cased above the Store() construction,
        # hence its own try/except rather than sharing the Store()-dependent
        # commands' try block below). A broad except here (unlike the specific
        # StoreError/IngestError/LLMError taxonomy below) is appropriate: this
        # command has no state-mutating side effects and no well-defined
        # exception type of its own — the goal is simply that a diagnostic
        # command never crashes with a raw traceback instead of a clean
        # err.prefix message, matching every other subcommand's guarantee.
        try:
            return _cmd_health(backend, _db_arg(args))
        except Exception as exc:  # noqa: BLE001 - see comment above
            print(_t("err.prefix", code="SYSTEM_INTERNAL_ERROR", msg=str(exc)), file=sys.stderr)
            return 1
    try:
        with Store(_db_arg(args) or db_path()) as store:
            command = str(args.command)
            if command == "notebook":
                return _cmd_notebook(store, args)
            if command == "add":
                return _cmd_add(store, backend, args)
            if command == "ask":
                return _cmd_ask(store, backend, args)
            if command == "studio":
                return _cmd_studio(store, backend, args)
            if command == "questions":
                return _cmd_questions(store, backend, args)
            if command == "eval":
                return _cmd_eval(store, backend, args)
            if command == "search":
                return _cmd_search(store, backend, args)
            if command == "messages":
                return _cmd_messages(store, args)
            if command == "reindex":
                return _cmd_reindex(store, backend, args)
            if command == "note":
                return _cmd_note(store, args)
            if command == "source":
                return _cmd_source(store, backend, args)
            if command == "chunk":
                return _cmd_chunk(store, args)
            if command == "stats":
                return _cmd_stats(store, args)
            if command == "backup":
                return _cmd_backup(store, args)
            if command == "trash":
                return _cmd_trash(store, args)
            if command == "vacuum":
                return _cmd_vacuum(store, args)
            if command == "export":
                if str(args.format) == "tree":
                    import json

                    print(
                        json.dumps(
                            store.export_notebook(int(args.notebook_id)),
                            ensure_ascii=False,
                            indent=1,
                        )
                    )
                else:
                    print(export(store, int(args.notebook_id), str(args.format)), end="")
                return 0
            if command == "import":
                return _cmd_import(store, args)
    except (StoreError, IngestError, LLMError) as exc:
        print(_t("err.prefix", code=exc.code, msg=str(exc)), file=sys.stderr)
        return 1
    except sqlite3.OperationalError as exc:
        print(_t("err.prefix", code="SYSTEM_DB_LOCKED", msg=str(exc)), file=sys.stderr)
        return 1
    except OSError as exc:
        # Store.__init__ calls mkdir() for the data directory: a PermissionError or
        # other OSError (e.g. SHOIN_DATA_DIR points to a read-only filesystem) would
        # otherwise escape as a bare Python traceback.
        print(_t("err.prefix", code="SYSTEM_IO_ERROR", msg=str(exc)), file=sys.stderr)
        return 1
    except UnicodeEncodeError as exc:
        # A lone surrogate in printed output — e.g. from a custom ChatBackend
        # passed to main(llm=...) emitting surrogate tokens (LLMClient strips
        # them since v0.2.599, but external backends are unguarded) — fails
        # every print() write on a strict-UTF-8 stdout. Same boundary catch
        # as OverflowError below: a coded err.prefix, never a traceback.
        print(
            _t("err.prefix", code="SYSTEM_INTERNAL_ERROR", msg=str(exc)),
            file=sys.stderr,
        )
        return 1
    except OverflowError:
        print(
            _t(
                "err.prefix",
                code="VALIDATION_INTEGER_OVERFLOW",
                msg="ID value too large for SQLite",
            ),
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - process boundary catch-all
        # Same parity as server.py's _dispatch, which maps any stray exception
        # to a coded SYSTEM_INTERNAL_ERROR envelope instead of leaking a raw
        # failure: the CLI's identical contract is "coded err.prefix line, never
        # a traceback" (the health command's comment states it outright). A
        # custom ChatBackend that raises a non-LLMError — RuntimeError, an
        # SDK-specific exception — is the reachable path: every in-tree backend
        # wraps into LLMError, but external backends are unguarded, and without
        # this catch their exceptions escape main() as a bare traceback while
        # the API sibling answers the same failure coded. Internal bugs are NOT
        # silently masked: SYSTEM_INTERNAL_ERROR still signals "unexpected" to
        # the user exactly as a traceback would, just without the stack noise —
        # and it preserves the contract that callers parsing stderr only ever
        # see the coded err.prefix shape.
        print(
            _t("err.prefix", code="SYSTEM_INTERNAL_ERROR", msg=f"{type(exc).__name__}: {exc}"),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
