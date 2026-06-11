"""Notebook export: Markdown / BibTeX / RIS (REQ-104)."""

from __future__ import annotations

from .store import Store

FORMATS = ("md", "bibtex", "ris")


def export_markdown(store: Store, notebook_id: int) -> str:
    nb = store.get_notebook(notebook_id)
    parts: list[str] = [f"# {nb.name}", ""]

    parts.append("## ソース")
    for i, src in enumerate(store.sources_for_notebook(notebook_id), start=1):
        parts.append(f"- [S{i}] {src.title} ({src.kind}) — {src.origin}")
    parts.append("")

    notes = store.list_notes(notebook_id)
    if notes:
        parts.append("## ノート")
        for n in notes:
            parts.append(f"### {n['title']}")
            parts.append(str(n["body"]))
            parts.append("")

    outputs = store.latest_studio_outputs(notebook_id)
    if outputs:
        parts.append("## Studio出力")
        for o in outputs:
            parts.append(f"### {o['kind']}")
            parts.append(str(o["body"]))
            parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def _bib_escape(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\n", " ")


def export_bibtex(store: Store, notebook_id: int) -> str:
    entries: list[str] = []
    for src in store.sources_for_notebook(notebook_id):
        key = f"shoin{src.id}"
        entries.append(
            "@misc{" + key + ",\n"
            f"  title = {{{_bib_escape(src.title)}}},\n"
            f"  howpublished = {{{_bib_escape(src.origin)}}},\n"
            f"  note = {{Shoin source, added {src.added_at}}}\n"
            "}"
        )
    return "\n\n".join(entries) + ("\n" if entries else "")


def export_ris(store: Store, notebook_id: int) -> str:
    entries: list[str] = []
    for src in store.sources_for_notebook(notebook_id):
        lines = [
            "TY  - GEN",
            f"TI  - {src.title}",
            f"UR  - {src.origin}",
            f"DA  - {src.added_at[:10]}",
            "ER  - ",
        ]
        entries.append("\n".join(lines))
    return "\n".join(entries) + ("\n" if entries else "")


def export(store: Store, notebook_id: int, fmt: str = "md") -> str:
    if fmt == "md":
        return export_markdown(store, notebook_id)
    if fmt == "bibtex":
        return export_bibtex(store, notebook_id)
    if fmt == "ris":
        return export_ris(store, notebook_id)
    raise ValueError(f"unknown export format: {fmt!r}")
