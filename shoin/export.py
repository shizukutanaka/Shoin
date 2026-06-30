"""Notebook export: Markdown / BibTeX / RIS (REQ-104)."""

from __future__ import annotations

import json

from .config import ui_lang
from .store import Store

_STRINGS: dict[str, dict[str, str]] = {
    "sources_section": {"ja": "ソース", "en": "Sources"},
    "notes_section": {"ja": "ノート", "en": "Notes"},
    "studio_section": {"ja": "Studio出力", "en": "Studio Output"},
    "chat_section": {"ja": "チャット履歴", "en": "Chat History"},
    "source_label": {"ja": "引用元", "en": "sources"},
}


def _t(key: str) -> str:
    lang = ui_lang()
    return _STRINGS[key].get(lang, _STRINGS[key]["en"])

FORMATS = ("md", "bibtex", "ris")


def export_markdown(store: Store, notebook_id: int) -> str:
    nb = store.get_notebook(notebook_id)
    parts: list[str] = [f"# {_md_line(nb.name)}", ""]

    parts.append(f"## {_t('sources_section')}")
    for i, src in enumerate(store.sources_for_notebook(notebook_id), start=1):
        parts.append(f"- [S{i}] {_md_line(src.title)} ({src.kind}) — {_md_line(src.origin)}")
    parts.append("")

    notes = store.list_notes(notebook_id)
    if notes:
        parts.append(f"## {_t('notes_section')}")
        for n in notes:
            parts.append(f"### {_md_line(n['title'])}")
            parts.append(str(n["body"] or ""))
            parts.append("")

    outputs = store.latest_studio_outputs(notebook_id)
    if outputs:
        parts.append(f"## {_t('studio_section')}")
        for o in outputs:
            parts.append(f"### {o['kind']}")
            parts.append(str(o["body"] or ""))
            parts.append("")

    messages = store.list_messages(notebook_id)
    if messages:
        parts.append(f"## {_t('chat_section')}")
        for m in messages:
            role = str(m["role"])
            body = str(m["body"])
            if role == "user":
                parts.append(f"**User**: {_md_line(body)}")
                parts.append("")
            else:
                try:
                    report: dict[str, object] = json.loads(str(m["citation_report"] or "{}"))
                except (json.JSONDecodeError, ValueError):
                    report = {}
                raw_map = report.get("source_map")
                source_map: dict[str, str] = (
                    {k: str(v) for k, v in raw_map.items()}
                    if isinstance(raw_map, dict)
                    else {}
                )
                if source_map:
                    legend = ", ".join(
                        f"{k}={v}"
                        for k, v in sorted(
                            source_map.items(),
                            key=lambda kv: int(kv[0][1:]) if kv[0][1:].isdigit() else 0,
                        )
                    )
                    parts.append(f"**Assistant** ({_t('source_label')}: {legend}):")
                else:
                    parts.append("**Assistant**:")
                parts.append(body)
                parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def _md_line(text: str) -> str:
    """Collapse multiline text to a single line for Markdown structural elements."""
    return " ".join(text.splitlines())


_BIB_ESC: dict[str, str] = {
    "\\": "\\\\",
    "%": "\\%",    # TeX comment — silently truncates remainder of field
    "&": "\\&",    # TeX tab-stop — LaTeX error outside tabular
    "$": "\\$",    # TeX math delimiter
    "#": "\\#",    # TeX macro parameter
    "_": "\\_",    # TeX subscript — LaTeX error outside math
    # \^{} / \~{} produce circumflex / tilde over an empty box — visually correct
    # and avoid issues in text mode.  Character-by-character replacement prevents
    # the subsequent {}→() substitution from corrupting these two-char sequences.
    "^": "\\^{}",
    "~": "\\~{}",
    "{": "{\\{}",  # {\{} — balanced for BibTeX, renders as literal { in LaTeX
    "}": "{\\}}",  # {\}} — balanced for BibTeX, renders as literal } in LaTeX
}


def _bib_escape(text: str) -> str:
    # splitlines() handles \n, \r\n, and bare \r uniformly.
    single = " ".join(text.splitlines())
    return "".join(_BIB_ESC.get(ch, ch) for ch in single)


def _ris_escape(text: str) -> str:
    """Normalize text for a RIS field value (single-line)."""
    return " ".join(text.splitlines())


def export_bibtex(store: Store, notebook_id: int) -> str:
    store.get_notebook(notebook_id)
    entries: list[str] = []
    for src in store.sources_for_notebook(notebook_id):
        key = f"shoin{src.id}"
        date = (src.added_at or "")[:10] or "unknown"
        entries.append(
            "@misc{" + key + ",\n"
            f"  title = {{{_bib_escape(src.title)}}},\n"
            f"  howpublished = {{{_bib_escape(src.origin)}}},\n"
            f"  note = {{Shoin source, added {date}}}\n"
            "}"
        )
    return "\n\n".join(entries) + ("\n" if entries else "")


def export_ris(store: Store, notebook_id: int) -> str:
    store.get_notebook(notebook_id)
    entries: list[str] = []
    for src in store.sources_for_notebook(notebook_id):
        date = ((src.added_at or "")[:10].replace("-", "/")) or "unknown"
        lines = [
            "TY  - GEN",
            f"TI  - {_ris_escape(src.title)}",
            f"UR  - {_ris_escape(src.origin)}",
            f"DA  - {date}",
            "ER  -",
        ]
        entries.append("\n".join(lines))
    return "\n\n".join(entries) + ("\n" if entries else "")


def export(store: Store, notebook_id: int, fmt: str = "md") -> str:
    if fmt == "md":
        return export_markdown(store, notebook_id)
    if fmt == "bibtex":
        return export_bibtex(store, notebook_id)
    if fmt == "ris":
        return export_ris(store, notebook_id)
    raise ValueError(f"unknown export format: {fmt!r}")
