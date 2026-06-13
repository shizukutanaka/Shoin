"""Shoin CLI: notebook management, ingestion, grounded Q&A, studio, export.

`serve` (Web UI) lands in Phase 4; the CLI exposes every core capability so the
product is fully usable headless (REQ-103).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .citation import CitationReport
from .config import TOP_K, VERSION, db_path, port, ui_lang
from .export import FORMATS, export
from .ingest import IngestError
from .llm import LLMClient, LLMError
from .pipeline import index_source
from .qa import ChatBackend, ask
from .store import Store, StoreError
from .studio import KINDS, generate, suggest_questions

_STRINGS: dict[str, dict[str, str]] = {
    "ja": {
        "nb.created": "作成: [{id}] {name}",
        "nb.deleted": "削除完了",
        "nb.renamed": "改名完了: [{id}] {name}",
        "msg.cleared": "チャット履歴をクリアしました",
        "cite.invalid": "⚠ 検証失敗の引用(ソース範囲外): {bad}",
        "cite.confirmed": " ✓根拠確認済み",
        "cite.misattr": " ⚠番号取り違えの可能性",
        "err.prefix": "エラー[{code}] {msg}",
    },
    "en": {
        "nb.created": "Created: [{id}] {name}",
        "nb.deleted": "Deleted",
        "nb.renamed": "Renamed: [{id}] {name}",
        "msg.cleared": "Chat history cleared",
        "cite.invalid": "⚠ Invalid citations (out of range): {bad}",
        "cite.confirmed": " ✓ grounding confirmed",
        "cite.misattr": " ⚠ possible wrong source",
        "err.prefix": "Error[{code}] {msg}",
    },
}


def _t(key: str, **kw: str) -> str:
    lang = ui_lang() if ui_lang() in _STRINGS else "en"
    tmpl = _STRINGS[lang].get(key) or _STRINGS["ja"][key]
    return tmpl.format(**kw) if kw else tmpl


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shoin", description="Shoin (書院) — local notebook")
    p.add_argument("--version", action="version", version=f"shoin {VERSION}")
    p.add_argument("--db", default=None, help="データベースパス(既定: SHOIN_DATA_DIR)")
    sub = p.add_subparsers(dest="command", required=True)

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

    msgs = sub.add_parser("messages", help="チャット履歴管理")
    msgssub = msgs.add_subparsers(dest="action", required=True)
    msgs_clear = msgssub.add_parser("clear", help="履歴クリア")
    msgs_clear.add_argument("notebook_id", type=int)

    add = sub.add_parser("add", help="ソース追加(ファイル/URL)")
    add.add_argument("notebook_id", type=int)
    add.add_argument("targets", nargs="+")

    askp = sub.add_parser("ask", help="ソース限定Q&A")
    askp.add_argument("notebook_id", type=int)
    askp.add_argument("question")
    askp.add_argument("-k", type=int, default=TOP_K, help="検索深さ")

    st = sub.add_parser("studio", help="Studio出力生成")
    st.add_argument("notebook_id", type=int)
    st.add_argument("kind", choices=KINDS)

    q = sub.add_parser("questions", help="推奨質問の提案")
    q.add_argument("notebook_id", type=int)

    ex = sub.add_parser("export", help="エクスポート")
    ex.add_argument("notebook_id", type=int)
    ex.add_argument("--format", choices=FORMATS, default="md")

    sv = sub.add_parser("serve", help="Web UI起動 (127.0.0.1のみ)")
    sv.add_argument("--port", type=int, default=port(), help=f"ポート(既定: {port()})")
    return p


def _print_report(report: CitationReport) -> None:
    if report["invalid"]:
        bad = ", ".join(f"S{i}" for i in report["invalid"])
        print(_t("cite.invalid", bad=bad))
    confirmed: set[int] = set(report.get("confirmed") or [])
    misattr: set[int] = set(report.get("misattributed") or [])
    for c in report["cited"]:
        title = report["source_map"].get(f"S{c}", "")
        if c in confirmed:
            marker = _t("cite.confirmed")
        elif c in misattr:
            marker = _t("cite.misattr")
        else:
            marker = ""
        print(f"  [S{c}] {title}{marker}")


def _cmd_notebook(store: Store, args: argparse.Namespace) -> int:
    action = str(args.action)
    if action == "new":
        nb = store.create_notebook(str(args.name))
        print(_t("nb.created", id=str(nb.id), name=nb.name))
    elif action == "list":
        for nb in store.list_notebooks():
            c = store.counts(nb.id)
            print(f"[{nb.id}] {nb.name}  sources={c['sources']} chunks={c['chunks']}")
    elif action == "delete":
        store.delete_notebook(int(args.notebook_id))
        print(_t("nb.deleted"))
    elif action == "rename":
        store.rename_notebook(int(args.notebook_id), str(args.name))
        print(_t("nb.renamed", id=str(args.notebook_id), name=str(args.name)))
    return 0


def _cmd_messages(store: Store, args: argparse.Namespace) -> int:
    action = str(args.action)
    if action == "clear":
        store.clear_messages(int(args.notebook_id))
        print(_t("msg.cleared"))
    return 0


def _cmd_add(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    rc = 0
    for target in [str(t) for t in args.targets]:
        try:
            result = index_source(store, int(args.notebook_id), target, llm)
            print(
                f"✓ {result.source.title}: {result.n_chunks} chunks ({result.n_embedded} embedded)"
            )
        except (IngestError, StoreError) as exc:
            print(f"✗ {target}: [{exc.code}] {exc}", file=sys.stderr)
            rc = 1
    return rc


def _cmd_ask(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    answer = ask(store, llm, int(args.notebook_id), str(args.question), k=int(args.k))
    print(answer.text)
    if answer.hits:
        print("---")
        _print_report(answer.report)
    return 0


def _cmd_studio(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    result = generate(store, llm, int(args.notebook_id), str(args.kind))
    print(result.body)
    print("---")
    _print_report(result.report)
    return 0


def _cmd_questions(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    for q in suggest_questions(store, llm, int(args.notebook_id)):
        print(f"- {q}")
    return 0


def main(argv: Sequence[str] | None = None, llm: ChatBackend | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if str(args.command) == "serve":
        from .server import serve

        serve(int(args.port), str(args.db) if args.db else None)
        return 0
    backend: ChatBackend = llm if llm is not None else LLMClient()
    try:
        with Store(str(args.db) if args.db else db_path()) as store:
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
            if command == "messages":
                return _cmd_messages(store, args)
            if command == "export":
                print(export(store, int(args.notebook_id), str(args.format)), end="")
                return 0
    except (StoreError, IngestError, LLMError) as exc:
        print(_t("err.prefix", code=exc.code, msg=str(exc)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
