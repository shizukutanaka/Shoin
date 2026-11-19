"""Shoin CLI: notebook management, ingestion, grounded Q&A, studio, export.

`serve` (Web UI) lands in Phase 4; the CLI exposes every core capability so the
product is fully usable headless (REQ-103).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .audio import AudioError, write_briefing_audio
from .chunk import split_text_with_context
from .citation import COVERAGE_LOW, CitationReport, found_bits
from .config import (
    EMBED_MODEL_SETTING_KEY,
    MAX_IMPORT_BYTES,
    MAX_QUESTION_LEN,
    MAX_TITLE_LEN,
    TOP_K,
    VERSION,
    _file_config,
    chunk_overlap,
    chunk_tokens,
    config_file,
    db_path,
    embed_batch,
    embed_model,
    endpoint_is_external,
    llm_model,
    llm_url,
    multi_query_enabled,
    port,
    redact_url_credentials,
    ui_lang,
)
from .export import FORMATS, export
from .ingest import _EXT_KIND, IngestError
from .llm import LLMClient, LLMError
from .log import one_line as _one_line
from .log import safe_text as _safe_text
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
from .store import Source, Store, StoreError
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


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically: sibling temp file + os.replace.

    A plain write_text that is interrupted mid-write (Ctrl-C, kill,
    disk-full) leaves a half-written JSON document behind — the next
    `eval --diff` then dies parsing a corrupt baseline that reads as a
    save the user believed succeeded. The pid-suffixed temp keeps two
    concurrent invocations from clobbering each other's staging file.
    """
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise StoreError("SYSTEM_IO_ERROR", f"cannot write file: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


# v0.2.717: the terminal-escape helpers live in log.py (the output leaf)
# so non-CLI warn paths (qa's embed-model warning) can share them, and the
# Cf category was added — bidi overrides are the same spoofing class as ESC.


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
        "check.integrity": "整合性: {v}",
        "check.fk": "外部キー違反: {n}件",
        "check.schema": "スキーマ版: {n}",
        "check.unopenable": "DBを開けません: {msg}",
        "check.chunks": "チャンク: {n}",
        "check.unembedded": "ベクトル未付与: {n}件",
        "check.embed_hint": (
            "⚠ 一部チャンクにベクトルがありません——"
            "`shoin reindex <書院ID>` で再付与できます"
        ),
        "check.corrupt_hint": (
            "⚠ 破損の可能性——`shoin backup` やエクスポートからの"
            "復元を検討してください"
        ),
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
        "add.dir_empty": "✗ {path}: 対応形式のファイルがありません (.txt/.md/.html/.pdf)",
        "add.unchanged": "= {title}: 変更なし",
        "add.duplicate": "= {path}: 同一内容のソースが既にあります (skip)",
        "add.dir_done": (
            "フォルダ取込: {n}件 (追加 {added} / 更新 {refreshed} / 変更なし {unchanged} / "
            "重複 {duplicate} / 失敗 {failed})"
        ),
        "src.deleted": "ソース削除完了",
        "src.renamed": "改名完了: [{id}] {title}",
        "src.refreshed": "✓ {title}: {chunks} chunks ({embedded} embedded)",
        "src.pages_failed": "⚠ {n} ページのテキスト抽出に失敗（索引は不完全です）",
        "src.refresh_all.done": (
            "一括再取込: {n}件 (更新 {refreshed} / 変更なし {unchanged} / "
            "skip {skipped} / 失敗 {failed})"
        ),
        "src.missing.not_dir": "フォルダにアクセスできません: {path}",
        "src.missing.unchecked": "[{id}] 元ファイルを確認できません: {path} ({error})",
        "src.missing.empty": "見つからない元ファイルはありません。",
        "src.missing.hint": (
            "確認後、不要なソースだけを shoin source delete <source_id> でゴミ箱へ移動できます。"
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
        "health.llm_external": (
            "警告: LLMエンドポイントはローカルではありません"
            "——チャンク本文と質問文がこのマシンを離れます"
        ),
        "setup.title": "Shoin 初回セットアップ",
        "setup.step_detect": "1/3 ローカルLLMを検出しています…",
        "setup.step_config": "2/3 構成を保存しています…",
        "setup.step_demo": "3/3 サンプル文書で引用付き回答を体験しています…",
        "setup.label_configured": "現在の設定",
        "setup.label_custom": "指定されたエンドポイント",
        "setup.probe_miss": "  検出先: {url} … 応答なし",
        "setup.found": "✓ LLMが見つかりました: {label} ({url})",
        "setup.not_found": "✗ ローカルLLMが見つかりませんでした",
        "setup.not_found_reason": (
            "理由: 検出した全てのエンドポイントが応答しませんでした。"
            "LLMが未導入か、起動していない可能性があります。"
        ),
        "setup.guide_intro": (
            "次の手順で導入できます"
            "（ダウンロードやインストールは自動では行いません）:"
        ),
        "setup.guide_win": (
            "  1. https://ollama.com/download から Ollama を入れるか、"
            "LM Studio を導入してください。"
        ),
        "setup.guide_mac": (
            "  1. https://ollama.com/download から Ollama を入れるか、"
            "`brew install ollama` で導入してください。"
        ),
        "setup.guide_linux": (
            "  1. https://ollama.com/download の手順で Ollama を入れるか、"
            "LM Studio / llama.cpp を導入してください。"
        ),
        "setup.guide_pull": (
            "  2. モデルを取得してください: "
            "`ollama pull qwen3:4b` と `ollama pull nomic-embed-text`"
            "（埋め込み用は任意）"
        ),
        "setup.guide_start": (
            "  3. LLMを起動してください（Ollamaは通常自動で起動します。"
            "手動の場合は `ollama serve`）。"
        ),
        "setup.guide_rerun": "  4. 起動したら、もう一度 `shoin setup` を実行してください。",
        "setup.guide_custom": (
            "別のエンドポイントを使う場合は "
            "`shoin setup --url <OpenAI互換URL>` を指定できます。"
        ),
        "setup.guide_bm25": (
            "なお LLMなしでも検索だけは使えます: `shoin serve` で起動して"
            "文書を追加すると全文検索(BM25)で探せます"
            "（回答の生成にはLLMが必要です）。"
        ),
        "setup.prompt_url": (
            "カスタムエンドポイントのURLを入力してください（空Enterで終了）: "
        ),
        "setup.saved": "✓ 構成を保存しました: {path}",
        "setup.no_save": "構成は保存しませんでした（--no-save）",
        "setup.config_values": (
            "  LLMエンドポイント: {url} / 生成モデル: {model}"
            " / 埋め込みモデル: {embed}"
        ),
        "setup.env_note": (
            "注意: 環境変数 SHOIN_LLM_URL 等が設定されている場合は、"
            "保存した構成より環境変数が優先されます。"
        ),
        "setup.sample_nb_name": "Shoin サンプル",
        "setup.sample_title": "Shoinについて（サンプル文書）",
        "setup.sample_text": (
            "# Shoinについて\n\n"
            "Shoin（書院）は、文書を自分のパソコンの中だけに置いて使う"
            "ノートブックです。文書や質問が外部のサーバーへ送信されることは"
            "ありません。\n\n"
            "ShoinのWeb UIは既定でポート7440で起動します。対応する文書形式は"
            "PDF、Markdown、テキスト、HTMLです。\n\n"
            "回答には必ず引用が付き、引用は元の文書の該当箇所へ移動して"
            "確認できます。埋め込みモデルが無い場合はBM25という全文検索"
            "だけで動作します。"
        ),
        "setup.sample_question": "Shoinは文書を外部のサーバーへ送信しますか？",
        "setup.demo_question": "サンプルの質問: {q}",
        "setup.demo_degraded": (
            "⚠ LLMに接続できず、引用付き回答の体験を完了できませんでした。"
            "LLMの起動を確認して `shoin setup` を再実行してください。"
        ),
        "setup.demo_done": "✓ 体験が完了しました。サンプルノートブック: [{id}] {name}",
        "setup.next_steps": (
            "次の手順: `shoin serve` で Web UI を開くか、"
            "`shoin ask {id} \"質問\"` でサンプルへ質問できます。"
        ),
        "setup.skip_demo": (
            "サンプル体験はスキップしました（--skip-demo）。"
            "`shoin serve` で開始できます。"
        ),
        "setup.serve_hint": (
            "初めての方は `shoin setup` でLLMの検出と初期構成ができます。"
        ),
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
        "help.description": "Shoin (書院) — local notebook",
        "help.db": "データベースパス(既定: SHOIN_DATA_DIR)",
        "help.notebook": "ノートブック管理",
        "help.create": "作成",
        "help.list": "一覧",
        "help.delete": "削除",
        "help.rename": "改名",
        "help.add": "追加",
        "help.nb.duplicate": "複製(ソース・チャンク・ノート等を全コピー)",
        "help.nb.merge": "統合(別nbの中身を取込み、元nbはゴミ箱へアーカイブ)",
        "help.nb.settings": (
            "nb単位設定 (top_k=N source_text_tokens=N; 引数なしで表示・--clearで消去)"
        ),
        "help.messages": "チャット履歴管理",
        "help.messages.clear": "履歴クリア",
        "help.add_source": "ソース追加(ファイル/フォルダ/URL)",
        "help.reindex": "ノートブックの埋め込みを再構築",
        "help.note": "ノート管理",
        "help.source": "ソース管理",
        "help.source.refresh": "ソースの再取込 (URL、または元ファイルが残るファイルソース)",
        "help.source.refresh_all": "ノートブック内の全ソースを一括再取込 (cron向け)",
        "help.source.missing": "指定フォルダ内で元ファイルが見つからないソースを表示 (変更なし)",
        "help.source.weight": "検索重みの設定 (1.0=標準・>1優遇・<1降格・0で下位固定)",
        "help.source.meta": (
            "ソースメタデータ (author=… year=…; 引数なしで表示・--clearで消去)"
        ),
        "help.chunk": "チャンク管理",
        "help.chunk.edit": "抽出テキスト修正(埋込みクリア→reindexで再構築)",
        "help.ask": "ソース限定Q&A",
        "help.ask.k": "検索深さ (省略時: nb設定か既定値)",
        "help.ask.source": "このソースIDのみを検索対象にする(複数回指定可)",
        "help.k": "検索深さ",
        "help.studio": "Studio出力生成",
        "help.audio": "保存済みブリーフィングをローカル音声 (WAV) に変換",
        "help.audio.model": "ローカルの Piper 音声モデル (.onnx)",
        "help.audio.output": "新規 WAV ファイルの保存先",
        "audio.saved": "音声を保存: {path}",
        "audio.no_briefing": "先に shoin studio {id} briefing でブリーフィングを生成してください。",
        "help.questions": "推奨質問の提案",
        "help.search": "全ノートブック横断検索",
        "help.eval": "検索精度を測定 (recall/MRR)",
        "help.eval.cases": "JSONファイル (「q」と「sources」の配列)",
        "help.eval.save": "この実行をベースラインJSONとして保存",
        "help.eval.diff": "保存済みベースラインとの差分を表示",
        "help.eval.gen": "ノートブックからケース雛形を生成 (--gen単独=stdout、--gen FILE=保存)",
        "help.export": "エクスポート",
        "help.import": "インポート(export --format tree のJSONからnbを復元)",
        "help.import.file": "export JSONファイル ('-' でstdin)",
        "help.serve": "Web UI起動 (127.0.0.1のみ)",
        "help.serve.port": "ポート(既定: {port})",
        "help.health": "設定・LLM到達性を表示 (headless diagnostics)",
        "help.setup": "初回セットアップ (LLM検出→構成保存→サンプル文書で引用付き回答を体験)",
        "help.setup.url": "OpenAI互換エンドポイントURL (検出より優先)",
        "help.setup.model": "生成モデル名",
        "help.setup.embed_model": "埋め込みモデル名 (空文字でBM25のみ)",
        "help.setup.no_save": "構成を config.json に保存しない",
        "help.setup.skip_demo": "サンプル文書での体験を省略",
        "help.stats": "ノートブック統計 (ソース/チャンク/DBサイズ)",
        "help.backup": "DBをバックアップ (オンラインスナップショット)",
        "help.trash": "ゴミ箱 (削除済みnbの一覧・復元・完全削除)",
        "help.trash.restore": "元id・埋込み込みで復元",
        "help.trash.purge": "アーカイブを完全削除(復元不可)",
        "help.trash.empty": "ゴミ箱を空にする(全アーカイブ完全削除・復元不可)",
        "help.vacuum": "DBをVACUUMして削除済み領域をOSへ返却",
        "help.check": "DB物理整合性を診断 (integrity/FK違反/スキーマ版、破損時rc=1)",
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
        "check.integrity": "Integrity: {v}",
        "check.fk": "Foreign-key violations: {n}",
        "check.schema": "Schema version: {n}",
        "check.unopenable": "Cannot open database: {msg}",
        "check.chunks": "Chunks: {n}",
        "check.unembedded": "Chunks without embeddings: {n}",
        "check.embed_hint": (
            "⚠ some chunks have no embedding —"
            " run `shoin reindex <notebook-id>` to rebuild"
        ),
        "check.corrupt_hint": (
            "⚠ possible corruption — consider restoring from"
            " `shoin backup` or an export."
        ),
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
        "add.dir_empty": "✗ {path}: no supported files (.txt/.md/.html/.pdf)",
        "add.unchanged": "= {title}: unchanged",
        "add.duplicate": "= {path}: identical source already in notebook (skipped)",
        "add.dir_done": (
            "folder add: {n} file(s) ({added} added / {refreshed} refreshed / "
            "{unchanged} unchanged / {duplicate} duplicate / {failed} failed)"
        ),
        "src.deleted": "Source deleted",
        "src.renamed": "Renamed: [{id}] {title}",
        "src.refreshed": "✓ {title}: {chunks} chunks ({embedded} embedded)",
        "src.pages_failed": "⚠ {n} page(s) could not be extracted — the index is incomplete",
        "src.refresh_all.done": (
            "refresh-all: {n} source(s) ({refreshed} refreshed / "
            "{unchanged} unchanged / {skipped} skipped / {failed} failed)"
        ),
        "src.missing.not_dir": "Cannot access directory: {path}",
        "src.missing.unchecked": "[{id}] Cannot check original file: {path} ({error})",
        "src.missing.empty": "No missing original files.",
        "src.missing.hint": (
            "After reviewing, move individual sources to trash with "
            "shoin source delete <source_id>."
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
        "health.llm_external": (
            "Warning: the LLM endpoint is not local"
            " — chunk text and questions leave this machine"
        ),
        "setup.title": "Shoin first-time setup",
        "setup.step_detect": "1/3 Detecting a local LLM…",
        "setup.step_config": "2/3 Saving the configuration…",
        "setup.step_demo": "3/3 Trying a cited answer on a sample document…",
        "setup.label_configured": "current configuration",
        "setup.label_custom": "specified endpoint",
        "setup.probe_miss": "  probed: {url} … no response",
        "setup.found": "✓ LLM found: {label} ({url})",
        "setup.not_found": "✗ No local LLM was found",
        "setup.not_found_reason": (
            "Reason: none of the probed endpoints responded. "
            "An LLM may not be installed yet, or it may not be running."
        ),
        "setup.guide_intro": (
            "Install one with these steps "
            "(nothing is downloaded or installed automatically):"
        ),
        "setup.guide_win": (
            "  1. Install Ollama from https://ollama.com/download, "
            "or install LM Studio."
        ),
        "setup.guide_mac": (
            "  1. Install Ollama from https://ollama.com/download, "
            "or run `brew install ollama`."
        ),
        "setup.guide_linux": (
            "  1. Install Ollama following https://ollama.com/download, "
            "or install LM Studio / llama.cpp."
        ),
        "setup.guide_pull": (
            "  2. Fetch the models: `ollama pull qwen3:4b` and "
            "`ollama pull nomic-embed-text` (the embedding model is optional)"
        ),
        "setup.guide_start": (
            "  3. Start the LLM (Ollama usually starts automatically; "
            "otherwise run `ollama serve`)."
        ),
        "setup.guide_rerun": "  4. Once it is running, run `shoin setup` again.",
        "setup.guide_custom": (
            "To use a different endpoint, pass "
            "`shoin setup --url <OpenAI-compatible URL>`."
        ),
        "setup.guide_bm25": (
            "Note: search alone works without an LLM — start `shoin serve`, "
            "add documents, and full-text (BM25) search finds them "
            "(answer generation needs an LLM)."
        ),
        "setup.prompt_url": (
            "Enter a custom endpoint URL (empty Enter to finish): "
        ),
        "setup.saved": "✓ Configuration saved: {path}",
        "setup.no_save": "Configuration was not saved (--no-save)",
        "setup.config_values": (
            "  LLM endpoint: {url} / chat model: {model} / embed model: {embed}"
        ),
        "setup.env_note": (
            "Note: when environment variables such as SHOIN_LLM_URL are set, "
            "they take precedence over the saved configuration."
        ),
        "setup.sample_nb_name": "Shoin Sample",
        "setup.sample_title": "About Shoin (sample document)",
        "setup.sample_text": (
            "# About Shoin\n\n"
            "Shoin is a notebook that keeps documents only on your own "
            "computer. Documents and questions are never sent to an external "
            "server.\n\n"
            "Shoin's Web UI starts on port 7440 by default. Supported "
            "document formats are PDF, Markdown, text, and HTML.\n\n"
            "Every answer carries citations, and each citation jumps to the "
            "matching passage in the original document. Without an embedding "
            "model, Shoin works with BM25 full-text search alone."
        ),
        "setup.sample_question": "Does Shoin send documents to an external server?",
        "setup.demo_question": "Sample question: {q}",
        "setup.demo_degraded": (
            "⚠ Could not reach the LLM, so the cited-answer trial did not "
            "complete. Check that the LLM is running and run `shoin setup` again."
        ),
        "setup.demo_done": "✓ Trial complete. Sample notebook: [{id}] {name}",
        "setup.next_steps": (
            "Next steps: open the Web UI with `shoin serve`, or ask the "
            "sample with `shoin ask {id} \"question\"`."
        ),
        "setup.skip_demo": (
            "Sample trial skipped (--skip-demo). Start with `shoin serve`."
        ),
        "setup.serve_hint": (
            "First time? Run `shoin setup` to detect an LLM and save the "
            "initial configuration."
        ),
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
        "help.description": "Shoin — local notebook",
        "help.db": "Database path (default: SHOIN_DATA_DIR)",
        "help.notebook": "Manage notebooks",
        "help.create": "Create",
        "help.list": "List",
        "help.delete": "Delete",
        "help.rename": "Rename",
        "help.add": "Add",
        "help.nb.duplicate": "Duplicate (copies sources, chunks, notes — everything)",
        "help.nb.merge": "Merge (absorb another notebook; the source is archived to trash)",
        "help.nb.settings": (
            "Per-notebook settings (top_k=N source_text_tokens=N; "
            "no pairs shows them, --clear removes)"
        ),
        "help.messages": "Manage chat history",
        "help.messages.clear": "Clear history",
        "help.add_source": "Add sources (files/folders/URLs)",
        "help.reindex": "Rebuild a notebook's embeddings",
        "help.note": "Manage notes",
        "help.source": "Manage sources",
        "help.source.refresh": (
            "Re-fetch a source (URL, or a file source whose original file still exists)"
        ),
        "help.source.refresh_all": "Re-fetch every source in a notebook (for cron)",
        "help.source.missing": "List missing original files under a folder (read-only)",
        "help.source.weight": (
            "Set search weight (1.0=standard, >1 boosts, <1 demotes, 0 pins to the bottom)"
        ),
        "help.source.meta": (
            "Source metadata (author=… year=…; no pairs shows it, --clear removes)"
        ),
        "help.chunk": "Manage chunks",
        "help.chunk.edit": "Edit extracted text (clears the embedding; rebuild with reindex)",
        "help.ask": "Q&A over sources",
        "help.ask.k": "Retrieval depth (default: the notebook's setting or the built-in default)",
        "help.ask.source": "Restrict retrieval to this source ID (may be repeated)",
        "help.k": "Retrieval depth",
        "help.studio": "Generate Studio output",
        "help.audio": "Narrate a saved briefing locally as a WAV file",
        "help.audio.model": "Local Piper voice model (.onnx)",
        "help.audio.output": "Path for a new WAV file",
        "audio.saved": "Saved audio: {path}",
        "audio.no_briefing": "Generate a briefing first with shoin studio {id} briefing.",
        "help.questions": "Suggest questions",
        "help.search": "Search across all notebooks",
        "help.eval": "Measure retrieval quality (recall/MRR)",
        "help.eval.cases": "JSON file (an array of \"q\"/\"sources\" entries)",
        "help.eval.save": "Save this run as a baseline JSON",
        "help.eval.diff": "Show the diff against a saved baseline",
        "help.eval.gen": (
            "Generate a case template from a notebook "
            "(--gen alone = stdout, --gen FILE = save)"
        ),
        "help.export": "Export",
        "help.import": "Import (restore a notebook from export --format tree JSON)",
        "help.import.file": "Export JSON file ('-' for stdin)",
        "help.serve": "Start the Web UI (127.0.0.1 only)",
        "help.serve.port": "Port (default: {port})",
        "help.health": "Show configuration and LLM reachability (headless diagnostics)",
        "help.setup": (
            "First-time setup (detect an LLM, save the config, "
            "try a cited answer on a sample document)"
        ),
        "help.setup.url": "OpenAI-compatible endpoint URL (takes priority over detection)",
        "help.setup.model": "Generation model name",
        "help.setup.embed_model": "Embedding model name (empty string = BM25 only)",
        "help.setup.no_save": "Do not save the configuration to config.json",
        "help.setup.skip_demo": "Skip the sample-document trial",
        "help.stats": "Notebook statistics (sources/chunks/DB size)",
        "help.backup": "Back up the DB (online snapshot)",
        "help.trash": "Trash (list, restore, or permanently delete deleted notebooks)",
        "help.trash.restore": "Restore with the original ID and embeddings",
        "help.trash.purge": "Permanently delete an archive (unrecoverable)",
        "help.trash.empty": "Empty the trash (permanently delete all archives)",
        "help.vacuum": "VACUUM the DB and return deleted space to the OS",
        "help.check": (
            "Diagnose DB physical integrity "
            "(integrity/FK violations/schema version; rc=1 if corrupt)"
        ),
    },
}


def _t(key: str, **kw: str) -> str:
    lang = ui_lang()
    if lang not in _STRINGS:
        lang = "en"
    tmpl = _STRINGS[lang].get(key) or _STRINGS["en"][key]
    return tmpl.format(**kw) if kw else tmpl


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shoin", description=_t("help.description"))
    p.add_argument("--version", action="version", version=f"shoin {VERSION}")
    p.add_argument("--db", default=None, help=_t("help.db"))
    sub = p.add_subparsers(dest="command", required=True)

    # REQ-105: the full subcommand surface — UI-free operation of every feature
    nb = sub.add_parser("notebook", help=_t("help.notebook"))
    nbsub = nb.add_subparsers(dest="action", required=True)
    nb_new = nbsub.add_parser("new", help=_t("help.create"))
    nb_new.add_argument("name")
    nbsub.add_parser("list", help=_t("help.list"))
    nb_del = nbsub.add_parser("delete", help=_t("help.delete"))
    nb_del.add_argument("notebook_id", type=int)
    nb_ren = nbsub.add_parser("rename", help=_t("help.rename"))
    nb_ren.add_argument("notebook_id", type=int)
    nb_ren.add_argument("name")
    nb_dup = nbsub.add_parser("duplicate", help=_t("help.nb.duplicate"))
    nb_dup.add_argument("notebook_id", type=int)
    nb_dup.add_argument("name", nargs="?", default=None)
    nb_mg = nbsub.add_parser(
        "merge", help=_t("help.nb.merge")
    )
    nb_mg.add_argument("notebook_id", type=int)
    nb_mg.add_argument("source_id", type=int)
    nb_set = nbsub.add_parser(
        "settings",
        help=_t("help.nb.settings"),
    )
    nb_set.add_argument("notebook_id", type=int)
    nb_set.add_argument("pairs", nargs="*", metavar="key=value")
    nb_set.add_argument("--clear", action="store_true")

    msgs = sub.add_parser("messages", help=_t("help.messages"))
    msgssub = msgs.add_subparsers(dest="action", required=True)
    msgs_list = msgssub.add_parser("list", help=_t("help.list"))
    msgs_list.add_argument("notebook_id", type=int)
    msgs_clear = msgssub.add_parser("clear", help=_t("help.messages.clear"))
    msgs_clear.add_argument("notebook_id", type=int)

    add = sub.add_parser("add", help=_t("help.add_source"))
    add.add_argument("notebook_id", type=int)
    add.add_argument("targets", nargs="+")

    ri = sub.add_parser("reindex", help=_t("help.reindex"))
    ri.add_argument("notebook_id", type=int)

    note = sub.add_parser("note", help=_t("help.note"))
    notesub = note.add_subparsers(dest="action", required=True)
    note_add = notesub.add_parser("add", help=_t("help.add"))
    note_add.add_argument("notebook_id", type=int)
    note_add.add_argument("title")
    note_add.add_argument("body")
    note_list = notesub.add_parser("list", help=_t("help.list"))
    note_list.add_argument("notebook_id", type=int)
    note_del = notesub.add_parser("delete", help=_t("help.delete"))
    note_del.add_argument("note_id", type=int)

    src = sub.add_parser("source", help=_t("help.source"))
    srcsub = src.add_subparsers(dest="action", required=True)
    src_del = srcsub.add_parser("delete", help=_t("help.delete"))
    src_del.add_argument("source_id", type=int)
    src_ren = srcsub.add_parser("rename", help=_t("help.rename"))
    src_ren.add_argument("source_id", type=int)
    src_ren.add_argument("title")
    src_ref = srcsub.add_parser(
        "refresh", help=_t("help.source.refresh")
    )
    src_ref.add_argument("source_id", type=int)
    src_ra = srcsub.add_parser(
        "refresh-all", help=_t("help.source.refresh_all")
    )
    src_ra.add_argument("notebook_id", type=int)
    src_missing = srcsub.add_parser("missing", help=_t("help.source.missing"))
    src_missing.add_argument("notebook_id", type=int)
    src_missing.add_argument("directory")
    src_w = srcsub.add_parser(
        "weight", help=_t("help.source.weight")
    )
    src_w.add_argument("source_id", type=int)
    src_w.add_argument("weight", type=float)
    src_m = srcsub.add_parser(
        "meta",
        help=_t("help.source.meta"),
    )
    src_m.add_argument("source_id", type=int)
    src_m.add_argument("pairs", nargs="*", metavar="key=value")
    src_m.add_argument("--clear", action="store_true")

    chk = sub.add_parser("chunk", help=_t("help.chunk"))
    chksub = chk.add_subparsers(dest="action", required=True)
    chk_edit = chksub.add_parser("edit", help=_t("help.chunk.edit"))
    chk_edit.add_argument("chunk_id", type=int)
    chk_edit.add_argument("text")

    askp = sub.add_parser("ask", help=_t("help.ask"))
    askp.add_argument("notebook_id", type=int)
    askp.add_argument("question")
    # default=None lets the notebook's own settings.top_k decide (v0.2.659):
    # an explicit -k still wins over the setting.
    askp.add_argument(
        "-k", type=_pos_int, default=None, help=_t("help.ask.k")
    )
    askp.add_argument(
        "--source",
        dest="source_ids",
        action="append",
        type=_pos_int,
        metavar="ID",
        help=_t("help.ask.source"),
    )

    st = sub.add_parser("studio", help=_t("help.studio"))
    st.add_argument("notebook_id", type=int)
    st.add_argument("kind", choices=KINDS)

    audio = sub.add_parser("audio", help=_t("help.audio"), description=_t("help.audio"))
    audio.add_argument("notebook_id", type=_pos_int)
    audio.add_argument("--model", type=Path, required=True, help=_t("help.audio.model"))
    audio.add_argument("--output", type=Path, required=True, help=_t("help.audio.output"))

    q = sub.add_parser("questions", help=_t("help.questions"))
    q.add_argument("notebook_id", type=int)

    srch = sub.add_parser("search", help=_t("help.search"))
    srch.add_argument("question")
    srch.add_argument("-k", type=_pos_int, default=TOP_K, help=_t("help.k"))

    ev = sub.add_parser("eval", help=_t("help.eval"))
    ev.add_argument("notebook_id", type=int)
    ev.add_argument(
        "cases", nargs="?", help=_t("help.eval.cases")
    )
    ev.add_argument("-k", type=_pos_int, default=TOP_K, help=_t("help.k"))
    ev.add_argument("--save", metavar="FILE", help=_t("help.eval.save"))
    ev.add_argument("--diff", metavar="FILE", help=_t("help.eval.diff"))
    ev.add_argument(
        "--gen",
        nargs="?",
        const="-",
        metavar="FILE",
        help=_t("help.eval.gen"),
    )

    ex = sub.add_parser("export", help=_t("help.export"))
    ex.add_argument("notebook_id", type=int)
    # "tree" (v0.2.655): the machine-transfer document — a JSON notebook
    # tree, not a human-readable export format, so it stays out of
    # export.FORMATS and its mime/ext/lockstep pins.
    ex.add_argument("--format", choices=(*FORMATS, "tree"), default="md")

    imp = sub.add_parser(
        "import", help=_t("help.import")
    )
    imp.add_argument("file", help=_t("help.import.file"))

    sv = sub.add_parser("serve", help=_t("help.serve"))
    sv.add_argument(
        "--port",
        type=_port_num,
        default=port(),
        help=_t("help.serve.port", port=str(port())),
    )

    sub.add_parser("health", help=_t("help.health"))

    setup = sub.add_parser(
        "setup",
        help=_t("help.setup"),
    )
    setup.add_argument(
        "--url", default=None, help=_t("help.setup.url")
    )
    setup.add_argument("--model", default=None, help=_t("help.setup.model"))
    setup.add_argument(
        "--embed-model", default=None, help=_t("help.setup.embed_model")
    )
    setup.add_argument(
        "--no-save", action="store_true", help=_t("help.setup.no_save")
    )
    setup.add_argument(
        "--skip-demo", action="store_true", help=_t("help.setup.skip_demo")
    )

    stt = sub.add_parser("stats", help=_t("help.stats"))
    stt.add_argument("notebook_id", type=int)

    bk = sub.add_parser("backup", help=_t("help.backup"))
    bk.add_argument("dest")

    tr = sub.add_parser("trash", help=_t("help.trash"))
    trsub = tr.add_subparsers(dest="action", required=True)
    trsub.add_parser("list", help=_t("help.list"))
    tr_res = trsub.add_parser("restore", help=_t("help.trash.restore"))
    tr_res.add_argument("trash_id", type=int)
    tr_pur = trsub.add_parser("purge", help=_t("help.trash.purge"))
    tr_pur.add_argument("trash_id", type=int)
    trsub.add_parser("empty", help=_t("help.trash.empty"))

    sub.add_parser("vacuum", help=_t("help.vacuum"))
    sub.add_parser(
        "check", help=_t("help.check")
    )
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
        sec = f" (§ {_one_line(section)})" if section else ""
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
    # v0.2.675 (product-review #59): the URL may carry userinfo
    # credentials (the user:pass@ prefix before the host) — show the
    # endpoint, never the secret.
    print(_t("health.llm_url", v=_one_line(redact_url_credentials(llm_url()))))
    # v0.2.674 (product-review #58): the product promise is that document
    # text and questions never leave this machine — a non-loopback
    # endpoint silently breaks it. stderr, same pattern as the
    # embed-model-staleness hint below.
    if endpoint_is_external(llm_url()):
        print(_t("health.llm_external"), file=sys.stderr)
    print(_t("health.llm_ok", v=_t("health.yes") if avail else _t("health.no")))
    print(_t("health.model", v=_one_line(llm_model())))
    em = embed_model()
    print(_t("health.embed_model", v=_one_line(em) if em.strip() else _t("health.embed_model_off")))
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


# Well-known local OpenAI-compatible endpoints the setup wizard probes, in
# preference order. Every entry is loopback: detection never contacts an
# external host, and the wizard never downloads or installs anything —
# installation stays an explicit user action (see _setup_guide_lines).
_SETUP_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("Ollama", "http://localhost:11434/v1"),
    ("LM Studio", "http://localhost:1234/v1"),
    ("llama.cpp", "http://localhost:8080/v1"),
)


def _setup_probe(url: str) -> bool:
    """Reachability of one candidate endpoint via the real LLM client."""
    return LLMClient(base_url=url).available()


def _setup_client(url: str, model: str, embed: str) -> ChatBackend:
    return LLMClient(base_url=url, model=model, embedding_model=embed)


def _setup_candidates(
    configured: str, extra: str | None
) -> list[tuple[str, str]]:
    """Probe order: the user's explicit --url, the configured endpoint,
    then the well-known local runtimes — deduplicated by URL."""
    raw: list[tuple[str, str]] = []
    if extra:
        raw.append((_t("setup.label_custom"), extra))
    raw.append((_t("setup.label_configured"), configured))
    raw.extend(_SETUP_ENDPOINTS)
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []
    for label, url in raw:
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        candidates.append((label, url))
    return candidates


def _setup_detect(
    llm: ChatBackend,
    candidates: list[tuple[str, str]],
    probe: Callable[[str], bool],
) -> tuple[str, str] | None:
    """First reachable (label, url), printing one miss line per failure so
    a detection failure is never silent about where it looked. The
    configured endpoint is checked through the caller's backend (the same
    object `health` reports on); other candidates go through *probe*."""
    configured = llm_url().rstrip("/")
    for label, url in candidates:
        if url.rstrip("/") == configured:
            ok = getattr(llm, "available", lambda: False)()
        else:
            ok = probe(url)
        if ok:
            return label, url
        print(_t("setup.probe_miss", url=_one_line(redact_url_credentials(url))))
    return None


def _setup_guide_lines() -> list[str]:
    """Platform-specific install guidance. Guidance only: the wizard
    prints these steps and stops — downloads and installs are the user's
    explicit actions, never the wizard's."""
    if sys.platform.startswith("win"):
        return [_t("setup.guide_win")]
    if sys.platform == "darwin":
        return [_t("setup.guide_mac")]
    return [_t("setup.guide_linux")]


def _setup_save_config(url: str, model: str, embed: str) -> Path:
    """Persist the detected configuration to config.json, preserving any
    keys the user already had. Written through the shared atomic writer
    so an interrupted save never leaves a half-written config behind."""
    import json

    merged = _file_config()
    merged["SHOIN_LLM_URL"] = url
    merged["SHOIN_LLM_MODEL"] = model
    merged["SHOIN_EMBED_MODEL"] = embed
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
    return path


def _setup_ensure_sample(store: Store) -> int:
    """Create (or reuse) the sample notebook with one sample source, so a
    first-time user reaches a cited answer without preparing a document.
    Idempotent: an existing sample notebook with sources is left as-is."""
    import hashlib

    name = _t("setup.sample_nb_name")
    nb_id = 0
    for nb in store.list_notebooks():
        if nb.name == name:
            nb_id = nb.id
            break
    if not nb_id:
        nb_id = store.create_notebook(name).id
    if store.counts(nb_id)["sources"] == 0:
        text = _t("setup.sample_text")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        src = store.add_source(
            nb_id, "txt", _t("setup.sample_title"), "shoin://setup-sample", digest
        )
        pairs = split_text_with_context(
            text, chunk_tokens=chunk_tokens(), overlap_tokens=chunk_overlap()
        )
        store.add_chunks(
            src.id, [t for _, t in pairs], [c for c, _ in pairs]
        )
    return nb_id


def _cmd_setup(
    store: Store,
    llm: ChatBackend,
    args: argparse.Namespace,
    probe: Callable[[str], bool] | None = None,
    prompt: Callable[[str], str] | None = None,
    client_factory: Callable[[str, str, str], ChatBackend] | None = None,
) -> int:
    """First-time setup wizard: detect a local LLM, save the working
    configuration, then let the user experience a citation-verified
    answer on a bundled sample document. Returns 1 (with the reason and
    the next step printed) when no LLM is reachable or the sample trial
    cannot complete — a failed setup is never reported as success."""
    probe_fn = probe if probe is not None else _setup_probe
    print(_t("setup.title"))
    print(_t("setup.step_detect"))
    candidates = _setup_candidates(
        llm_url(), str(args.url) if args.url else None
    )
    found = _setup_detect(llm, candidates, probe_fn)
    if found is None and prompt is not None:
        custom = prompt(_t("setup.prompt_url")).strip()
        if custom:
            if probe_fn(custom):
                found = (_t("setup.label_custom"), custom)
            else:
                print(
                    _t(
                        "setup.probe_miss",
                        url=_one_line(redact_url_credentials(custom)),
                    ),
                    file=sys.stderr,
                )
    if found is None:
        print(_t("setup.not_found"), file=sys.stderr)
        print(_t("setup.not_found_reason"), file=sys.stderr)
        print(_t("setup.guide_intro"))
        for line in _setup_guide_lines():
            print(line)
        print(_t("setup.guide_pull"))
        print(_t("setup.guide_start"))
        print(_t("setup.guide_rerun"))
        print(_t("setup.guide_custom"))
        print(_t("setup.guide_bm25"))
        return 1
    label, url = found
    print(
        _t(
            "setup.found",
            label=_one_line(label),
            url=_one_line(redact_url_credentials(url)),
        )
    )
    if endpoint_is_external(url):
        print(_t("health.llm_external"), file=sys.stderr)
    model = str(args.model) if args.model else llm_model()
    embed = str(args.embed_model) if args.embed_model is not None else embed_model()
    print(_t("setup.step_config"))
    if args.no_save:
        print(_t("setup.no_save"))
    else:
        path = _setup_save_config(url, model, embed)
        print(_t("setup.saved", path=_one_line(str(path))))
    print(
        _t(
            "setup.config_values",
            url=_one_line(redact_url_credentials(url)),
            model=_one_line(model),
            embed=_one_line(embed) if embed.strip() else _t("health.embed_model_off"),
        )
    )
    print(_t("setup.env_note"))
    if args.skip_demo:
        print(_t("setup.skip_demo"))
        return 0
    print(_t("setup.step_demo"))
    nb_id = _setup_ensure_sample(store)
    if url.rstrip("/") == llm_url().rstrip("/"):
        demo_llm: ChatBackend = llm
    elif client_factory is not None:
        demo_llm = client_factory(url, model, embed)
    else:
        demo_llm = _setup_client(url, model, embed)
    question = _t("setup.sample_question")
    print(_t("setup.demo_question", q=question))
    answer = ask(store, demo_llm, nb_id, question)
    print(_safe_text(answer.text))
    if answer.degraded:
        print(_t("setup.demo_degraded"), file=sys.stderr)
        return 1
    if answer.hits and _report_has_output(answer.report):
        print("---")
        _print_report(answer.report)
    print(
        _t("setup.demo_done", id=str(nb_id), name=_t("setup.sample_nb_name"))
    )
    print(_t("setup.next_steps", id=str(nb_id)))
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
    print(_t("stats.name", v=_one_line(nb.name)))
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

    oversize = StoreError(
        "NOTEBOOK_IMPORT_INVALID",
        f"export document exceeds {MAX_IMPORT_BYTES // (1 << 30)}GB limit",
    )
    try:
        # v0.2.681: bound the document BEFORE json.loads — the file/stdin used
        # to be read in full with no cap, so a hostile or accidental giant
        # export OOM-killed the process mid-parse. Stat the file first like
        # extract_file does, then bound the read itself for a file that grew
        # (or a stdin stream, which has no stat).
        if str(args.file) == "-":
            raw_bytes = sys.stdin.buffer.read(MAX_IMPORT_BYTES + 1)
            if len(raw_bytes) > MAX_IMPORT_BYTES:
                raise oversize
        else:
            p = Path(str(args.file)).expanduser()
            if p.stat().st_size > MAX_IMPORT_BYTES:
                raise oversize
            raw_bytes = p.read_bytes()
            if len(raw_bytes) > MAX_IMPORT_BYTES:
                raise oversize
        raw = json.loads(raw_bytes.decode("utf-8"))
    except OSError as exc:
        raise StoreError(
            "SYSTEM_IO_ERROR", f"cannot read export file: {exc}"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        # Same classification as _cmd_eval's cases file: a non-UTF-8 or
        # non-JSON file is a 400-class input defect, never a traceback.
        # v0.2.718: RecursionError too — a deeply nested doc (server.py's
        # _read_json already codes the same shape) must not fall through to
        # the catch-all's SYSTEM_INTERNAL_ERROR: input defect, not 500.
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
            _atomic_write_text(Path(str(args.gen)).expanduser(), payload)
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
    oversize = StoreError(
        "VALIDATION_FIELD_FORMAT_INVALID",
        f"cases file exceeds {MAX_IMPORT_BYTES // (1 << 30)}GB limit",
    )
    try:
        # v0.2.690: same bound as _cmd_import — the cases file was read in
        # full with no cap, so a giant file OOM-killed the process before
        # the coded parse errors below could apply. Stat first, then bound
        # the read for a file that grew between the two.
        p = Path(str(args.cases)).expanduser()
        if p.stat().st_size > MAX_IMPORT_BYTES:
            raise oversize
        raw_bytes = p.read_bytes()
        if len(raw_bytes) > MAX_IMPORT_BYTES:
            raise oversize
        raw = json.loads(raw_bytes.decode("utf-8"))
    except OSError as exc:
        raise StoreError("SYSTEM_IO_ERROR", f"cannot read cases file: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        # UnicodeDecodeError comes from the strict UTF-8 decode — a
        # non-UTF-8 file is definitionally not JSON, and neither it nor
        # JSONDecodeError is an OSError, so without this both escape main()'s
        # handler chain as a raw traceback. RecursionError (v0.2.718): a
        # deeply nested file is the same 400-class defect, not a 500.
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

        _atomic_write_text(
            Path(str(args.save)).expanduser(),
            json.dumps(report_to_dict(rep, int(args.k)), ensure_ascii=False, indent=1),
        )
        print(_t("eval.saved", f=_one_line(str(args.save))))
    if args.diff:
        from .evaluate import diff_reports, report_from_dict

        base_oversize = StoreError(
            "VALIDATION_FIELD_FORMAT_INVALID",
            f"baseline file exceeds {MAX_IMPORT_BYTES // (1 << 30)}GB limit",
        )
        try:
            base_p = Path(str(args.diff)).expanduser()
            if base_p.stat().st_size > MAX_IMPORT_BYTES:
                raise base_oversize
            base_bytes = base_p.read_bytes()
            if len(base_bytes) > MAX_IMPORT_BYTES:
                raise base_oversize
            base_raw = json.loads(base_bytes.decode("utf-8"))
        except OSError as exc:
            raise StoreError("SYSTEM_IO_ERROR", f"cannot read baseline file: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            # RecursionError (v0.2.718): deeply nested baseline = same
            # 400-class input defect as malformed JSON, not a 500.
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


def _cmd_check(db: str | None) -> int:
    """Diagnose the physical DB — the counterpart of `health` (config/LLM
    reachability): this command answers "is the file itself sound".
    Runs outside main()'s shared `with Store(...)` block for the same
    reason `health`/`serve` do — a file that cannot even open is exactly
    the finding this command exists to report (v0.2.670).
    """
    try:
        with Store(db or db_path()) as store:
            res = store.check()
    except (OSError, StoreError, sqlite3.DatabaseError) as exc:
        # DatabaseError covers OperationalError and catches 'file is not a
        # database' — a Store() on a corrupt file raises it at the first
        # PRAGMA, before check() could run.
        print(_t("check.unopenable", msg=str(exc)), file=sys.stderr)
        return 1
    print(_t("check.integrity", v=str(res["integrity"])))
    for line in res["integrity_errors"]:
        print(f"  {_one_line(line)}")
    print(_t("check.fk", n=str(res["fk_violations"])))
    print(
        _t(
            "check.schema",
            n=f"{res['schema_version']}/{res['expected_version']}",
        )
    )
    # v0.2.671: logical integrity — chunks with no embedding are a dead
    # vector leg; the ingest-time toast is the only place that used to
    # say so. The repair hint only matters when embeddings are enabled.
    print(_t("check.chunks", n=str(res["chunks"])))
    print(_t("check.unembedded", n=str(res["unembedded"])))
    if res["unembedded"] and embed_model().strip():
        print(_t("check.embed_hint"), file=sys.stderr)
    if not res["ok"]:
        print(_t("check.corrupt_hint"), file=sys.stderr)
    return 0 if res["ok"] else 1


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
            print(
                f"[{m['id']}] {_one_line(str(m['role']))}:"
                f" {_safe_text(str(m['body']))}"
            )
    elif action == "clear":
        store.clear_messages(int(args.notebook_id))
        print(_t("msg.cleared"))
    return 0


def _dir_files(root: str) -> list[str]:
    """Supported files under a directory, in sorted depth-first order.

    Dot-entries (.git, editor swap files) are skipped, and os.walk does not
    follow directory symlinks, so a link cycle cannot loop."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        out.extend(
            os.path.join(dirpath, name)
            for name in sorted(filenames)
            if not name.startswith(".") and Path(name).suffix.casefold() in _EXT_KIND
        )
    return out


def _cmd_add(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    rc = 0
    notebook_id = int(args.notebook_id)
    # A directory target expands to its supported files. Re-running the same
    # folder add is a sync: a file whose path is already a source is
    # refreshed in place (no stale second copy), byte-identical content
    # elsewhere is skipped, and only new files are added.
    items: list[tuple[str, bool]] = []
    # expanduser() on each target: `add` takes positional args so word-start
    # `~/x.md` is shell-expanded already, but a quoted '~/x.md' arrives
    # literally — same contract as every other Path(str(*)) in this module.
    # Only ~-prefixed targets go through Path(): it collapses a URL's "//".
    for target in [
        str(Path(str(t)).expanduser()) if str(t).startswith("~") else str(t)
        for t in args.targets
    ]:
        if not os.path.isdir(target):
            items.append((target, False))
            continue
        files = _dir_files(target)
        if not files:
            print(_t("add.dir_empty", path=_one_line(target)), file=sys.stderr)
            rc = 1
        items.extend((f, True) for f in files)
    has_dir = any(in_dir for _, in_dir in items)
    known: dict[str, Source] = {}
    if has_dir:
        known = {s.origin: s for s in store.sources_for_notebook(notebook_id)}
    tally = dict.fromkeys(("added", "refreshed", "unchanged", "duplicate", "failed"), 0)
    for target, in_dir in items:
        status = "added"
        try:
            src = known.get(os.path.abspath(target)) if in_dir else None
            if src is None:
                result = index_source(store, notebook_id, target, llm)
            else:
                result = refresh_source(store, src.id, llm)
                if result.source.sha256 == src.sha256:
                    status = "unchanged"
                    print(_t("add.unchanged", title=_one_line(result.source.title)))
                    continue
                status = "refreshed"
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
            if in_dir and exc.code == "SOURCE_ALREADY_EXISTS":
                status = "duplicate"
                print(_t("add.duplicate", path=_one_line(target)))
                continue
            status = "failed"
            print(f"✗ {_one_line(target)}: [{exc.code}] {exc}", file=sys.stderr)
            rc = 1
        except sqlite3.OperationalError as exc:
            status = "failed"
            print(f"✗ {_one_line(target)}: [SYSTEM_DB_LOCKED] {exc}", file=sys.stderr)
            rc = 1
        finally:
            if in_dir:
                tally[status] += 1
    if has_dir:
        print(
            _t(
                "add.dir_done",
                n=str(sum(tally.values())),
                added=str(tally["added"]),
                refreshed=str(tally["refreshed"]),
                unchanged=str(tally["unchanged"]),
                duplicate=str(tally["duplicate"]),
                failed=str(tally["failed"]),
            )
        )
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
        print(_safe_text(delta), end="", flush=True)
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
        print(_safe_text(answer.text))
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
    print(_safe_text(result.body))
    if _report_has_output(result.report):
        print("---")
        _print_report(result.report)
    return 0


def _cmd_questions(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    for q in suggest_questions(store, llm, int(args.notebook_id)):
        print(f"- {_safe_text(q)}")
    return 0


def _cmd_audio(store: Store, args: argparse.Namespace) -> int:
    notebook_id = int(args.notebook_id)
    with store.read_snapshot():
        store.get_notebook(notebook_id)
        briefing = next(
            (row for row in store.latest_studio_outputs(notebook_id) if row["kind"] == "briefing"),
            None,
        )
    if briefing is None:
        raise AudioError("AUDIO_BRIEFING_NOT_FOUND", _t("audio.no_briefing", id=str(notebook_id)))
    write_briefing_audio(str(briefing["body"]), args.model, args.output)
    print(_t("audio.saved", path=_safe_text(str(args.output))))
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
            print(
                _t(
                    "search.suggest",
                    terms=" ".join(_one_line(s) for s in suggestions),
                )
            )
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


def _list_missing_sources(store: Store, notebook_id: int, directory: str) -> int:
    with store.read_snapshot():
        store.get_notebook(notebook_id)
        sources = store.sources_for_notebook(notebook_id)
    try:
        root = Path(os.path.abspath(Path(directory).expanduser()))
    except RuntimeError:  # unknown home in a ~user path
        print(_t("src.missing.not_dir", path=_one_line(directory)), file=sys.stderr)
        return 1
    if not root.is_dir():
        print(_t("src.missing.not_dir", path=_one_line(str(root))), file=sys.stderr)
        return 1

    found_missing = False
    rc = 0
    for src in sources:
        # Match the absolute locator recorded by `shoin add`, not the realpath.
        path = Path(src.origin)
        if not path.is_absolute() or not path.is_relative_to(root):
            continue
        try:
            path.stat()
        except FileNotFoundError:
            found_missing = True
            print(f"[{src.id}] {_one_line(src.title)}: {_one_line(str(path))}")
        except (OSError, ValueError) as exc:
            rc = 1
            print(
                _t(
                    "src.missing.unchecked", id=str(src.id),
                    path=_one_line(str(path)), error=_one_line(str(exc)),
                ),
                file=sys.stderr,
            )
    if found_missing:
        print(_t("src.missing.hint"))
    elif rc == 0:
        print(_t("src.missing.empty"))
    return rc


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
            line = f"[{r['id']}] {_one_line(str(r['title']))}: {status}"
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
    elif action == "missing":
        return _list_missing_sources(store, int(args.notebook_id), str(args.directory))
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

        if not config_file().exists():
            # First run: point at the wizard once, on stderr, without
            # probing anything — serve must start just as fast as before.
            print(_t("setup.serve_hint"), file=sys.stderr)
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
    if str(args.command) == "check":
        # Same Store()-free position as health: a corrupt file raises inside
        # Store() itself, so the diagnostic must open its own connection.
        return _cmd_check(_db_arg(args))
    try:
        with Store(_db_arg(args) or db_path()) as store:
            command = str(args.command)
            if command == "notebook":
                return _cmd_notebook(store, args)
            if command == "setup":
                return _cmd_setup(
                    store,
                    backend,
                    args,
                    prompt=input if sys.stdin.isatty() else None,
                )
            if command == "add":
                return _cmd_add(store, backend, args)
            if command == "ask":
                return _cmd_ask(store, backend, args)
            if command == "studio":
                return _cmd_studio(store, backend, args)
            if command == "audio":
                return _cmd_audio(store, args)
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
    except (StoreError, IngestError, LLMError, AudioError) as exc:
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
