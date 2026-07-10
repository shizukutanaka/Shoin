"""Shoin CLI: notebook management, ingestion, grounded Q&A, studio, export.

`serve` (Web UI) lands in Phase 4; the CLI exposes every core capability so the
product is fully usable headless (REQ-103).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence

from .citation import CitationReport
from .config import MAX_QUESTION_LEN, MAX_TITLE_LEN, TOP_K, VERSION, db_path, port, ui_lang
from .export import FORMATS, export
from .ingest import IngestError
from .llm import LLMClient, LLMError
from .pipeline import index_source, reindex_notebook, refresh_source
from .qa import ChatBackend, ask
from .store import Store, StoreError
from .studio import KINDS, generate, suggest_questions

_STRINGS: dict[str, dict[str, str]] = {
    "ja": {
        "nb.created": "作成: [{id}] {name}",
        "nb.deleted": "削除完了",
        "nb.renamed": "改名完了: [{id}] {name}",
        "nb.empty": "書院がありません。`shoin notebook new <名前>` で作成。",
        "msg.cleared": "チャット履歴をクリアしました",
        "msg.empty": "チャット履歴がありません。",
        "cite.invalid": "⚠ 検証失敗の引用(ソース範囲外): {bad}",
        "cite.confirmed": " ✓根拠確認済み",
        "cite.misattr": " ⚠番号取り違えの可能性",
        "cite.uncited": "⚠ 無出典の断定文({n}件、引用なし):",
        "err.prefix": "エラー[{code}] {msg}",
        "reindex.done": "✓ {n}/{total} チャンクを再埋め込みしました",
        "reindex.no_embed": "埋め込みモデル未設定 (SHOIN_EMBED_MODEL)。スキップ。",
        "note.added": "追加: [{id}] {title}",
        "note.deleted": "ノート削除完了",
        "note.empty": "ノートがありません。`shoin note add <書院ID> <題> <本文>` で追加。",
        "src.deleted": "ソース削除完了",
        "src.renamed": "改名完了: [{id}] {title}",
        "src.refreshed": "✓ {title}: {chunks} chunks ({embedded} embedded)",
    },
    "en": {
        "nb.created": "Created: [{id}] {name}",
        "nb.deleted": "Deleted",
        "nb.renamed": "Renamed: [{id}] {name}",
        "nb.empty": "No notebooks. Create one with `shoin notebook new <name>`.",
        "msg.cleared": "Chat history cleared",
        "msg.empty": "No chat history.",
        "cite.invalid": "⚠ Invalid citations (out of range): {bad}",
        "cite.confirmed": " ✓ grounding confirmed",
        "cite.misattr": " ⚠ possible wrong source",
        "cite.uncited": "⚠ Uncited assertions ({n}, no citation):",
        "err.prefix": "Error[{code}] {msg}",
        "reindex.done": "✓ Re-embedded {n}/{total} chunks",
        "reindex.no_embed": "No embedding model set (SHOIN_EMBED_MODEL). Skipped.",
        "note.added": "Added: [{id}] {title}",
        "note.deleted": "Note deleted",
        "note.empty": "No notes. Add one with `shoin note add <notebook_id> <title> <body>`.",
        "src.deleted": "Source deleted",
        "src.renamed": "Renamed: [{id}] {title}",
        "src.refreshed": "✓ {title}: {chunks} chunks ({embedded} embedded)",
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
    uncited = report.get("uncited") or []
    if uncited:
        print(_t("cite.uncited", n=str(len(uncited))))
        for sentence in uncited:
            print(f"  - {sentence}")


def _cmd_notebook(store: Store, args: argparse.Namespace) -> int:
    action = str(args.action)
    if action == "new":
        nb = store.create_notebook(str(args.name))
        print(_t("nb.created", id=str(nb.id), name=nb.name))
    elif action == "list":
        rows = store.list_notebooks_with_counts()
        if not rows:
            print(_t("nb.empty"))
        for row in rows:
            c = row["counts"]
            print(f"[{row['id']}] {row['name']}  sources={c['sources']} chunks={c['chunks']}")
    elif action == "delete":
        store.delete_notebook(int(args.notebook_id))
        print(_t("nb.deleted"))
    elif action == "rename":
        store.rename_notebook(int(args.notebook_id), str(args.name))
        # rename_notebook() strips whitespace before persisting — echo the same
        # stripped value here, not the raw CLI argument, matching the v0.2.93-95
        # fix already applied to this action's sibling, source rename, below.
        print(_t("nb.renamed", id=str(args.notebook_id), name=str(args.name).strip()))
    return 0


def _cmd_messages(store: Store, args: argparse.Namespace) -> int:
    action = str(args.action)
    if action == "list":
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
    for target in [str(t) for t in args.targets]:
        try:
            result = index_source(store, int(args.notebook_id), target, llm)
            print(
                f"✓ {result.source.title}: {result.n_chunks} chunks ({result.n_embedded} embedded)"
            )
        except (IngestError, StoreError) as exc:
            print(f"✗ {target}: [{exc.code}] {exc}", file=sys.stderr)
            rc = 1
        except sqlite3.OperationalError as exc:
            print(f"✗ {target}: [SYSTEM_DB_LOCKED] {exc}", file=sys.stderr)
            rc = 1
    return rc


def _cmd_ask(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    question = str(args.question)
    if len(question) > MAX_QUESTION_LEN:
        raise StoreError(
            "VALIDATION_FIELD_FORMAT_INVALID",
            f"question too long (max {MAX_QUESTION_LEN} characters)",
        )
    answer = ask(store, llm, int(args.notebook_id), question, k=int(args.k))
    print(answer.text)
    # A non-degraded answer can still legitimately carry an empty report — e.g.
    # the model correctly follows the system prompt's "say so explicitly" rule
    # for a fact not in the sources, which uncited_sentences() deliberately
    # excludes from `uncited` (citation.py's _DISCLAIMER_MARKERS). Printing a
    # bare "---" with nothing under it is the same defect v0.2.27/v0.2.55 fixed
    # elsewhere; guard on actual report content too, not just hits/degraded.
    if answer.hits and not answer.degraded and (
        answer.report["cited"] or answer.report["invalid"] or answer.report.get("uncited")
    ):
        print("---")
        _print_report(answer.report)
    return 0


def _cmd_studio(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    result = generate(store, llm, int(args.notebook_id), str(args.kind))
    print(result.body)
    # _print_report() also prints something for an invalid-only report (out-of-
    # range [S#] citations, cli.py's own _print_report()) — the pre-existing
    # guard here missed that case, silently dropping the warning from CLI
    # output. Same fix shape as _cmd_ask()'s report-content guard.
    if result.report["cited"] or result.report["invalid"] or result.report.get("uncited"):
        print("---")
        _print_report(result.report)
    return 0


def _cmd_questions(store: Store, llm: ChatBackend, args: argparse.Namespace) -> int:
    for q in suggest_questions(store, llm, int(args.notebook_id)):
        print(f"- {q}")
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
        print(_t("note.added", id=str(note_id), title=title))
    elif action == "list":
        notes = store.list_notes(int(args.notebook_id))
        if not notes:
            print(_t("note.empty"))
        for n in notes:
            print(f"[{n['id']}] {n['title']}")
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
        store.update_source_title(src.id, str(args.title), src.origin)
        # update_source_title() silently truncates to MAX_TITLE_LEN before
        # persisting (config.py: "source titles silently truncated") — echo
        # the same truncated value here, not the raw CLI argument, matching
        # the v0.2.93/94 fix applied to this endpoint's Web API siblings.
        printed_title = str(args.title).strip()[:MAX_TITLE_LEN]
        print(_t("src.renamed", id=str(src.id), title=printed_title))
    elif action == "refresh":
        result = refresh_source(store, int(args.source_id), llm)
        print(
            _t(
                "src.refreshed",
                title=result.source.title,
                chunks=str(result.n_chunks),
                embedded=str(result.n_embedded),
            )
        )
    return 0


def main(argv: Sequence[str] | None = None, llm: ChatBackend | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if str(args.command) == "serve":
        from .server import serve

        try:
            serve(int(args.port), str(args.db) if args.db else None)
        except OSError as exc:
            print(_t("err.prefix", code="SYSTEM_PORT_IN_USE", msg=str(exc)), file=sys.stderr)
            return 1
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
            if command == "reindex":
                return _cmd_reindex(store, backend, args)
            if command == "note":
                return _cmd_note(store, args)
            if command == "source":
                return _cmd_source(store, backend, args)
            if command == "export":
                print(export(store, int(args.notebook_id), str(args.format)), end="")
                return 0
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
    except OverflowError:
        print(
            _t("err.prefix", code="VALIDATION_INTEGER_OVERFLOW", msg="ID value too large for SQLite"),
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
