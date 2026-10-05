"""Static contract tests for the single-file Web UI.

docs/product-review.md 短所#8 / backlog#11: UI regressions were caught only by
live Playwright verification during whichever session happened to touch the UI,
and never persisted — so a break stayed invisible until someone manually redid
the same clicks.

Rather than add browser-automation infrastructure (a heavy dependency, a browser
download, and a slow suite) the requirement was questioned first: what actually
breaks in a single vanilla-JS file, and how much of it needs a *browser* to see?
Three classes cover most of it, and none of them need one:

1. **JS syntax** — one typo silently breaks the entire UI, since the whole app is
   a single <script> block. `node --check` catches it; the test SKIPs (never
   fails) when node is unavailable, so the suite stays dependency-free.
2. **i18n completeness** — every data-i18n* key the HTML references must exist in
   BOTH locales, or a JA or EN user sees a blank/English-only control. This is a
   real regression path: v0.2.71 converted 11 hardcoded aria-labels to the i18n
   mechanism precisely because they had drifted.
3. **API contract** — every /api/… path the UI fetches must match a route the
   server actually registers. Catches "renamed the route, forgot the caller",
   which is otherwise a 404 discovered only by clicking.

What this deliberately does NOT cover: rendering, layout, and event wiring — the
things that genuinely need a browser. Those remain live-verified per the project
convention. This closes the cheap 80%, honestly labelled.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

from shoin.server import _Handler

_UI = Path(__file__).resolve().parent.parent / "shoin" / "static" / "index.html"


def _html() -> str:
    return _UI.read_text(encoding="utf-8")


def _script_body(html: str) -> str:
    """The contents of the single <script> block that is the whole application."""
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m, "index.html must contain exactly one inline <script> block"
    return m.group(1)


def _js_block(src: str, marker: str) -> str:
    """The JS source from *marker* through its matching closing brace.

    Used to lift a single function/const object out of the monolithic script so
    node can execute it against stubbed DOM globals — the mechanism v0.2.230
    added for behavioral (not just static) UI checks.
    """
    start = src.index(marker)
    depth, end = 0, start
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return src[start:end]


def _run_node(js_source: str) -> tuple[int, str]:
    node = shutil.which("node")
    if not node:
        return -1, "node not available"
    with tempfile.TemporaryDirectory() as d:
        js = Path(d) / "ui.mjs"
        js.write_text(js_source, encoding="utf-8")
        proc = subprocess.run(
            [node, str(js)], capture_output=True, text=True, timeout=60
        )
    return proc.returncode, proc.stderr or proc.stdout


class TestUIContract(unittest.TestCase):
    def test_javascript_parses(self) -> None:
        """A syntax error anywhere kills the whole UI — the app is one script block."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available; JS syntax check skipped")
        with tempfile.TemporaryDirectory() as d:
            js = Path(d) / "ui.mjs"
            js.write_text(_script_body(_html()), encoding="utf-8")
            proc = subprocess.run(
                [node, "--check", str(js)], capture_output=True, text=True, timeout=60
            )
            self.assertEqual(proc.returncode, 0, f"index.html JS does not parse:\n{proc.stderr}")

    def test_every_i18n_key_exists_in_both_locales(self) -> None:
        """A key referenced by the HTML but missing from a locale renders blank."""
        html = _html()
        script = _script_body(html)
        # Keys the markup asks for, via any data-i18n* attribute flavour.
        used = set(re.findall(r'data-i18n[a-z-]*="([^"]+)"', html))
        self.assertTrue(used, "expected data-i18n attributes in index.html")

        # Keys each locale defines. The I18N table is `ja: { "k":"v", ... }`.
        locales: dict[str, set[str]] = {}
        for loc in ("ja", "en"):
            m = re.search(rf"\b{loc}:\s*\{{(.*?)\n\s*\}}", script, re.S)
            self.assertIsNotNone(m, f"I18N.{loc} block not found in index.html")
            assert m is not None
            locales[loc] = set(re.findall(r'"([^"]+)"\s*:', m.group(1)))

        for loc, defined in locales.items():
            missing = sorted(used - defined)
            self.assertEqual(missing, [], f"data-i18n keys missing from I18N.{loc}: {missing}")

    def test_every_i18n_attribute_kind_is_applied(self) -> None:
        """applyI18n localizes each data-i18n* attribute *kind* via a fixed
        querySelectorAll list. A markup attr whose kind has no selector —
        e.g. data-i18n-value added later — sits unlocalized forever: no key
        lookup ever runs on it. Pin the used kinds to the handled ones."""
        html = _html()
        script = _script_body(html)
        used = set(re.findall(r'\b(data-i18n[a-z-]*)="', html))
        handled = set(
            re.findall(r'querySelectorAll\("\[(data-i18n[a-z-]*)\]"\)', script)
        )
        self.assertTrue(handled, "expected applyI18n selectors in index.html")
        self.assertEqual(
            used - handled, set(),
            f"data-i18n* attribute kinds applyI18n never applies: "
            f"{sorted(used - handled)} — markup would stay unlocalized",
        )

    def test_every_studio_kind_has_a_label_in_both_locales(self) -> None:
        """studio.KINDS (Python) and the UI's i18n table are a cross-language contract.

        The Studio buttons build their label dynamically — `t("studio."+kind)` —
        so a kind added to KINDS without a matching i18n key renders a button with
        a BLANK label. The data-i18n scan above cannot see this: the key never
        appears literally in the markup. Only comparing the two sources catches it.
        """
        from shoin.studio import KINDS

        script = _script_body(_html())
        for loc in ("ja", "en"):
            m = re.search(rf"\b{loc}:\s*\{{(.*?)\n\s*\}}", script, re.S)
            self.assertIsNotNone(m, f"I18N.{loc} block not found")
            assert m is not None
            keys = set(re.findall(r'"([^"]+)"\s*:', m.group(1)))
            missing = [k for k in KINDS if f"studio.{k}" not in keys]
            self.assertEqual(
                missing, [], f"studio kinds with no I18N.{loc} label (blank button): {missing}"
            )

    def test_literal_t_keys_resolve_and_locales_are_symmetric(self) -> None:
        """Pin the two i18n invariants the attribute scan cannot see.

        (a) Every literal `t("k")` call site must resolve in ja — the primary
        locale and `t()`'s last-resort fallback (`I18N.ja[k] || k`). A typo'd
        or deleted key renders the raw key text in a toast/badge instead of a
        message, silently. (b) ja and en must define the same key set —
        the data-i18n attr test only checks keys the markup references, so a
        locale-only key (or a dynamic `t()` key added to one side) drifts
        unnoticed.
        """
        script = _script_body(_html())
        locales: dict[str, set[str]] = {}
        for loc in ("ja", "en"):
            m = re.search(rf"\b{loc}:\s*\{{(.*?)\n\s*\}}", script, re.S)
            self.assertIsNotNone(m, f"I18N.{loc} block not found")
            assert m is not None
            locales[loc] = set(re.findall(r'"([^"]+)"\s*:', m.group(1)))

        literal = set(re.findall(r'\bt\("([a-z][a-z0-9._]*)"\)', script))
        self.assertTrue(literal, "expected literal t() call sites")
        missing = sorted(literal - locales["ja"])
        self.assertEqual(missing, [], f"t() keys missing from I18N.ja (raw key renders): {missing}")
        self.assertEqual(
            locales["ja"] - locales["en"],
            set(),
            f"I18N.ja-only keys (en falls back to ja text): "
            f"{sorted(locales['ja'] - locales['en'])}",
        )
        self.assertEqual(
            locales["en"] - locales["ja"],
            set(),
            f"I18N.en-only keys (ja user sees raw key): {sorted(locales['en'] - locales['ja'])}",
        )

    def test_i18n_values_keep_placeholder_parity(self) -> None:
        """Each key's `{name}` placeholder set must be identical in ja and en.

        Call sites substitute manually — `t("reindex.ok").replace("{n}", v)` —
        so a placeholder present in one locale's template but absent from the
        other leaks the raw `{n}` into that locale's UI (the replace finds
        nothing). Key-set symmetry (above) can't see this: both locales define
        the key; only the placeholder names inside the values diverge.
        """
        script = _script_body(_html())
        tables: dict[str, dict[str, set[str]]] = {}
        for loc in ("ja", "en"):
            m = re.search(rf"\b{loc}:\s*\{{(.*?)\n\s*\}}", script, re.S)
            self.assertIsNotNone(m, f"I18N.{loc} block not found")
            assert m is not None
            tables[loc] = {
                k: set(re.findall(r"\{([a-z_]+)\}", v))
                for k, v in re.findall(r'"([^"]+)"\s*:\s*"((?:[^"\\]|\\.)*)"', m.group(1))
            }
        for key in sorted(tables["ja"]):
            self.assertIn(key, tables["en"])
            self.assertEqual(
                tables["ja"][key],
                tables["en"][key],
                f"I18N[{key}] placeholders diverge: ja={tables['ja'][key]} en={tables['en'][key]}",
            )

    def test_i18n_call_sites_substitute_every_placeholder(self) -> None:
        """Each `t("k")` call on a placeholder-bearing key must `.replace` every
        name the template defines — and nothing else.

        A placeholder present in the template but never substituted leaks the
        raw `{n}` into the rendered toast/label; a `.replace("{m}")` for a name
        no template defines is dead code that usually signals the template and
        call site have drifted apart. Checks both directions per source line,
        against the union of ja+en placeholder sets.
        """
        script = _script_body(_html())
        values: dict[str, set[str]] = {}
        for loc in ("ja", "en"):
            m = re.search(rf"\b{loc}:\s*\{{(.*?)\n\s*\}}", script, re.S)
            assert m is not None
            for k, v in re.findall(r'"([^"]+)"\s*:\s*"((?:[^"\\]|\\.)*)"', m.group(1)):
                values.setdefault(k, set()).update(re.findall(r"\{([a-z_]+)\}", v))
        placeholder_keys = {k for k, ph in values.items() if ph}
        self.assertTrue(placeholder_keys, "expected placeholder-bearing i18n keys")

        for lineno, line in enumerate(script.splitlines(), 1):
            keys = re.findall(r'\bt\("([a-z][a-z0-9._]*)"\)', line)
            if not keys:
                continue
            replaced = set(re.findall(r'\.replace\("\{([a-z_]+)\}"', line))
            used = set().union(*(values.get(k, set()) for k in keys))
            for k in keys:
                missing = values.get(k, set()) - replaced
                self.assertEqual(
                    missing, set(),
                    f"line {lineno}: t({k}) leaves {sorted(missing)} unsubstituted "
                    "(raw braces render in the UI)",
                )
            dead = replaced - used
            self.assertEqual(
                dead, set(),
                f"line {lineno}: replaces {sorted(dead)} for names no t() key on "
                "the line defines — dead substitution",
            )

    def test_studio_kinds_match_between_server_and_ui(self) -> None:
        """The UI's `const KINDS` array must equal studio.KINDS exactly.

        buildKindButtons() iterates the JS array; _h_studio validates against the
        Python tuple. A kind added on only one side either renders no button at
        all (server-only) or a button that always fails STUDIO_KIND_INVALID
        (UI-only) — both silently, since every existing test exercises each side
        independently.
        """
        from shoin.studio import KINDS

        script = _script_body(_html())
        m = re.search(r"\bKINDS\s*=\s*\[([^\]]*)\]", script)
        self.assertIsNotNone(m, "const KINDS array not found in index.html")
        assert m is not None
        ui_kinds = re.findall(r'"([^"]+)"', m.group(1))
        self.assertEqual(ui_kinds, list(KINDS))

    def test_export_formats_match_between_server_and_ui(self) -> None:
        """The UI's export hrefs must cover exactly export.FORMATS.

        Each download link hardcodes `?format=X`; _h_export rejects anything
        outside FORMATS. A format added server-side silently gets no link; a
        link to a removed format 400s on click. Comparing the two sets pins
        both directions.
        """
        from shoin.export import FORMATS

        script = _script_body(_html())
        ui_fmts = set(re.findall(r'export\?format=([a-z]+)', script))
        self.assertTrue(ui_fmts, "expected export ?format= hrefs in index.html")
        self.assertEqual(ui_fmts, set(FORMATS))

    def test_coverage_low_matches_between_server_and_ui(self) -> None:
        """index.html hardcodes `const COVERAGE_LOW = 0.5` beside a comment
        saying to keep it in sync with citation.COVERAGE_LOW and the export
        threshold — a comment is not a guard. Pin the numeric equality so a
        threshold moved on only one side can't silently mislabel low-coverage
        answers in the UI."""
        from shoin.citation import COVERAGE_LOW

        script = _script_body(_html())
        m = re.search(r"\bCOVERAGE_LOW\s*=\s*([0-9.]+)", script)
        self.assertIsNotNone(m, "const COVERAGE_LOW not found in index.html")
        assert m is not None
        self.assertEqual(float(m.group(1)), COVERAGE_LOW)

    def test_ui_report_keys_are_all_produced_by_citation_report(self) -> None:
        """Every `report.X` the UI reads must be a CitationReport key.

        The SSE `done` frame and persisted assistant messages carry
        make_report()'s output verbatim; the UI reads ~20 report.* keys to
        render badges, the seal detail, and the low-coverage marker. Renaming
        or dropping a Python key leaves the reads silently undefined — every
        check badge degrades with all server-side tests still green. Subset
        (not equality): producer-only keys like n_sources and quote_mismatch
        (folded into misattributed) legitimately have no UI reader.
        """
        script = _script_body(_html())
        ui_keys = set(re.findall(r"\breport\.([a-z_]+)", script))
        self.assertTrue(ui_keys, "expected report.* reads in index.html")
        from shoin.citation import CitationReport

        produced = set(CitationReport.__annotations__)
        unknown = ui_keys - produced
        self.assertFalse(
            unknown,
            f"index.html reads report keys CitationReport does not declare: {unknown}",
        )

    def test_api_wrapper_maps_the_error_envelope(self) -> None:
        """Every handler surfaces failures via toast(e.message); api() is what
        turns the server's `{"error":{code,message}}` envelope into that
        message, falling back to `[status] err.generic` when the body isn't
        JSON. A regression here turns every API failure into a generic or
        thrown-response-object toast across the whole UI."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        try:
            fn = _js_block(src, "async function api")
        except ValueError:
            self.fail("api() not found in index.html")
        # The wrapper calls the global fetch(); a `next` slot swaps responses
        # per case while api() is embedded once.
        harness = (
            """\
const t = k => k;
let next;
const fetch = async () => next;
const mkRes = (ok, status, body, bad) => ({
  ok, status,
  json: bad ? async () => { throw new Error("not json") }
            : async () => body,
});
"""
            + fn
            + """
(async () => {
  next = mkRes(true, 200, {});
  const r = await api("/x");
  if (r !== next) { console.error("api() did not return the response"); process.exit(1) }
  next = mkRes(false, 400, {error: {code: "VALIDATION_X", message: "bad input"}});
  try { await api("/x"); console.error("400 did not throw"); process.exit(1) }
  catch (e) { if (e.message !== "[VALIDATION_X] bad input")
    { console.error("envelope mapping wrong: " + e.message); process.exit(1) } }
  next = mkRes(false, 500, null, true);
  try { await api("/x"); console.error("500 did not throw"); process.exit(1) }
  catch (e) { if (e.message !== "[500] err.generic")
    { console.error("fallback mapping wrong: " + e.message); process.exit(1) } }
  console.log("ok");
})();
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_jpost_serializes_json_request(self) -> None:
        """v0.2.333: jpost() is the request-side half of the api() boundary
        contract — every mutating call must reach fetch() as method=POST with
        Content-Type: application/json and a JSON.stringify'd body. A dropped
        header or raw-object body would silently break every write handler
        (create/rename/notes/studio/reindex all go through jpost)."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        api_fn = _js_block(src, "async function api")
        m = re.search(r"const jpost = [^\n]*", src)
        if not m:
            self.fail("jpost not found in index.html")
        harness = """\
const t = k => k;
let lastCall;
const fetch = async (path, opts) => {
  lastCall = {path, opts};
  return {ok: true, json: async () => ({})};
};
""" + api_fn + "\n" + m.group(0) + """
(async () => {
  await jpost("/api/x", {kind: "brief", n: 1});
  const c = lastCall;
  if (!c || c.path !== "/api/x" || !c.opts || c.opts.method !== "POST"
      || !c.opts.headers || c.opts.headers["Content-Type"] !== "application/json"
      || c.opts.body !== '{"kind":"brief","n":1}')
    { console.error("jpost shape wrong: " + JSON.stringify(c)); process.exit(1) }
  console.log("ok");
})();
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_ask_scope_selection_end_to_end(self) -> None:
        """v0.2.632: the ask payload mirrors the API's source_ids contract —
        a partial selection carries `source_ids`, a full selection sends the
        unscoped field-less body, and zero selection is refused by the handler
        (source_ids:[] would silently mean "whole notebook", the opposite of
        unchecking everything). Pins the two pure seams under node plus the
        literal wiring the contract hangs on."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        # Wiring seams: the submit handler consults scopeSelection(), passes a
        # literal object (the request-body pin's readable contract), and the
        # zero-selection guard names the toast key.
        self.assertIn("scopeSelection()", src)
        self.assertIn("scopedIds(scope.sel, scope.live)", src)
        self.assertIn("source_ids:ids", src)
        self.assertIn("chat.noscope", src)
        self.assertIn("srcSel.delete", src)
        scope_fn = _js_block(src, "function scopeSelection")
        payload_fn = _js_block(src, "function scopedIds")
        harness = """\
let cur = {sources: [{id: 1}, {id: 2}, {id: 3}]};
const srcSel = new Set([1, 2, 3]);
""" + scope_fn + "\n" + payload_fn + """
const bad = [];
{ const r = scopeSelection(); const ids = scopedIds(r.sel, r.live);
  if (r.sel.length !== 3) bad.push("full selection lost ids");
  if (ids !== null) bad.push("full selection leaks source_ids"); }
srcSel.delete(2);
{ const r = scopeSelection(); const ids = scopedIds(r.sel, r.live);
  if (JSON.stringify(ids) !== "[1,3]") bad.push("partial ids: " + JSON.stringify(ids)); }
srcSel.clear();
{ const r = scopeSelection();
  if (r.sel.length !== 0) bad.push("zero selection not empty"); }
if (bad.length) { console.error(bad.join("; ")); process.exit(1) }
console.log("ok");
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_route_arity_matches_capture_groups(self) -> None:
        """_dispatch invokes handler(*[int(g) for g in m.groups()]) — so each
        capturing group must be (a) numeric, else int() ValueErrors into a
        500 at request time, and (b) exactly as many as the handler's
        declared parameters, else TypeError into a 500 the same way. The
        route-integrity pin below only checks the handler name resolves —
        not that the call signature fits the groups the pattern yields."""
        import inspect

        for _verb, pattern, name in _Handler._ROUTES:
            fn = getattr(_Handler, f"_h_{name}")
            n_params = len(inspect.signature(fn).parameters) - 1  # self
            n_groups = re.compile(pattern).groups
            self.assertEqual(
                n_params, n_groups,
                f"{name}: handler takes {n_params} args but "
                f"{pattern!r} yields {n_groups} groups",
            )
            for g in re.findall(r"\(([^()]+)\)", pattern):
                self.assertEqual(
                    g, r"\d+",
                    f"{name}: non-numeric capture ({g!r}) would "
                    "ValueError in int(g) at request time",
                )

    def test_every_api_path_matches_a_registered_route(self) -> None:
        """A path or verb the UI fetches but the server never registers is a
        404/405 in waiting. Path-only matching would let api() (GET) slip onto
        a POST-only route — the server answers 405 at click time."""
        script = _script_body(_html())

        # Call sites look like api("/api/…"), api(`/api/…${expr}/…`, {method:"X"}),
        # or jpost("/api/…") (always POST). Bare api() defaults to GET.
        seen: list[tuple[str, str, str]] = []
        for lineno, line in enumerate(script.splitlines(), 1):
            calls = list(re.finditer(r'(api|jpost)\(\s*[`"](/api/[^`"?]*)', line))
            for i, m in enumerate(calls):
                fn, raw = m.group(1), m.group(2)
                # Search for a method override only up to the next api()/jpost()
                # on the same line so adjacent calls don't cross-attribute.
                tail = line[m.end():calls[i + 1].start() if i + 1 < len(calls) else len(line)]
                meth = re.search(r'method\s*:\s*"([A-Z]+)"', tail)
                verb = "POST" if fn == "jpost" else (meth.group(1) if meth else "GET")
                seen.append((verb, raw, f"line {lineno}"))
        self.assertTrue(seen, "expected /api/ calls in index.html")

        for verb, raw, where in seen:
            # Substitute ${...} interpolations with a concrete id so the literal
            # can be matched against the server's numeric-id route patterns.
            concrete = re.sub(r"\$\{[^}]*\}", "1", raw).rstrip("/")
            self.assertTrue(
                any(v == verb and re.match(p, concrete) for v, p, _ in _Handler._ROUTES),
                f"index.html {where} calls {verb} {raw!r} (as {concrete!r}) "
                "but no matching server route accepts that method",
            )

    def test_route_table_and_request_metadata_are_consistent(self) -> None:
        """The last contract edges. Server-side, `_dispatch` resolves
        handlers as `getattr(self, f"_h_{name}")` — a route whose name
        has no `_h_*` method AttributeErrors into a 500 at call time, and
        a verb with no `do_<VERB>` method is a 501 before that.
        Client-side, request *metadata* names — custom `X-*` headers and
        `?query=` params — are dictionary lookups on the server: a typo
        doesn't 400, the `.get` returns None and the handler silently
        falls back (`"upload.txt"` as filename, or a default format).
        Pin the route table's internal integrity and the metadata names
        the JS actually sends."""
        server = (_UI.parent.parent / "server.py").read_text(encoding="utf-8")
        script = _script_body(_html())

        # Route table: every name has a handler, every verb a do_* method.
        handlers = set(re.findall(r"def (_h_[a-z_]+)\(", server))
        do_verbs = {d[3:] for d in re.findall(r"def (do_[A-Z]+)\(", server)}
        missing = [f"_h_{name}" for _, _, name in _Handler._ROUTES
                   if f"_h_{name}" not in handlers]
        self.assertEqual(missing, [], f"routes name handlers that don't exist: {missing}")
        uncovered = {v for v, _, _ in _Handler._ROUTES} - do_verbs
        self.assertEqual(uncovered, set(), f"route verbs with no do_* method: {uncovered}")

        # Request metadata: every X-* header and ?param= the JS sends is
        # one the server reads. Standard headers (Content-Type) are the
        # client's business; custom X-* ones are the contract.
        sent_headers = set(re.findall(r'"(X-[A-Za-z-]+)"\s*:', script))
        read_headers = set(re.findall(r'self\.headers\.get\("([^"]+)"', server))
        self.assertEqual(
            sent_headers - read_headers, set(),
            f"X-* headers the UI sends but the server never reads: "
            f"{sorted(sent_headers - read_headers)}",
        )
        sent_params = set(re.findall(r"/api/[^`\"?\s]*\?(\w+)=", script))
        read_params = set(re.findall(r'self\._query\.get\("([^"]+)"', server))
        self.assertEqual(
            sent_params - read_params, set(),
            f"?params the UI sends but the server never reads: "
            f"{sorted(sent_params - read_params)}",
        )

    def test_route_patterns_are_fully_anchored(self) -> None:
        """Every _ROUTES pattern must match its path and only its path.
        `_dispatch` uses re.match (start-anchored only): a pattern missing
        the trailing `$` would still match its intended path — passing every
        unit test — while also accepting any longer path that starts with it
        (e.g. `GET /api/sources/5/text/extra` reaching src_text). Pin the
        anchors lexically AND behaviorally: build the one concrete path a
        pattern matches, then assert junk on either side doesn't match."""
        for _verb, pattern, name in _Handler._ROUTES:
            self.assertTrue(pattern.startswith("^"), f"{name}: pattern not ^-anchored")
            self.assertTrue(pattern.endswith("$"), f"{name}: pattern not $-anchored")
            concrete = re.sub(r"\(\\d\+\)", "1", pattern.strip("^$"))
            self.assertIsNotNone(
                re.match(pattern, concrete), f"{name}: doesn't match {concrete!r}"
            )
            self.assertIsNone(
                re.match(pattern, concrete + "/extra"),
                f"{name}: prefix-matches a longer path ({concrete}/extra)",
            )
            self.assertIsNone(
                re.match(pattern, "/x" + concrete),
                f"{name}: matches with a leading segment (/x{concrete})",
            )

    def test_template_placeholder_and_value_contracts(self) -> None:
        """Value-level contracts below the field-name layer.

        `_h_ui` substitutes `__SHOIN_LANG__` with the server locale — it
        must appear EXACTLY once (a second occurrence is also corrupted
        by the blind byte replace) and the literal must survive in the
        server's replace call, or language seeding silently dies — the
        exact half-true bug this file's header warns about. Likewise the
        meta name itself: `meta[name="X"]` JS selectors ⊆ `meta name="X"`
        in markup. Below that, two value sets the UI offers must be ones
        the pipeline honours: `accept=` extensions ⊆ `_EXT_KIND` (a
        selectable file that ingest then rejects), and `?format=` values
        ⊆ export `FORMATS` (a link that 400s at click time)."""
        html = _html()
        script = _script_body(html)
        server = (_UI.parent.parent / "server.py").read_text(encoding="utf-8")
        ingest = (_UI.parent.parent / "ingest.py").read_text(encoding="utf-8")
        export_mod = (_UI.parent.parent / "export.py").read_text(encoding="utf-8")

        # Server template placeholder: exactly one occurrence, server
        # substitutes it, JS reads the same meta name.
        self.assertEqual(
            html.count("__SHOIN_LANG__"), 1,
            "__SHOIN_LANG__ must appear exactly once — the byte replace "
            "would corrupt every occurrence equally",
        )
        self.assertIn(
            'b"__SHOIN_LANG__"', server,
            "server must substitute the __SHOIN_LANG__ placeholder",
        )
        meta_sel = set(re.findall(r'meta\[name="([^"]+)"\]', script))
        meta_names = set(re.findall(r'<meta name="([^"]+)"', html))
        self.assertEqual(
            meta_sel - meta_names, set(),
            f"meta names the JS reads but the markup lacks: "
            f"{sorted(meta_sel - meta_names)}",
        )

        # accept= ≡ ingest _EXT_KIND (both directions: an offered
        # extension ingest rejects 400s at upload time; a supported
        # extension missing from accept= is silently unselectable in
        # the picker even though the pipeline ingests it — .markdown
        # and .htm were missing until v0.2.375)
        accept_m = re.search(r'accept="([^"]+)"', html)
        self.assertIsNotNone(accept_m, "file input needs an accept list")
        assert accept_m is not None
        offered = {x.strip() for x in accept_m.group(1).split(",") if x.strip()}
        kind_m = re.search(r"_EXT_KIND\s*=\s*\{([^}]*)\}", ingest)
        self.assertIsNotNone(kind_m, "ingest._EXT_KIND dict expected")
        assert kind_m is not None
        supported = set(re.findall(r'"(\.\w+)"\s*:', kind_m.group(1)))
        self.assertEqual(
            offered, supported,
            f"accept= must offer exactly the ingestible extensions — "
            f"unsupported offered: {sorted(offered - supported)}, "
            f"ingestible but unselectable: {sorted(supported - offered)}",
        )

        # ?format= values ⊆ FORMATS
        sent_fmts = set(re.findall(r"/api/[^`\"?\s]*\?format=(\w+)", script))
        formats_m = re.search(r'FORMATS\s*=\s*\(([^)]*)\)', export_mod)
        self.assertIsNotNone(formats_m, "export.FORMATS tuple expected")
        assert formats_m is not None
        formats = set(re.findall(r'"(\w+)"', formats_m.group(1)))
        self.assertEqual(
            sent_fmts - formats, set(),
            f"?format= values the UI sends but export rejects: "
            f"{sorted(sent_fmts - formats)}",
        )

    def test_request_body_fields_match_server_reads(self) -> None:
        """Every JSON key the UI sends must be a field the handler actually
        reads, and every field the handler requires must be sent. A typo'd
        key (`{titl}` for `title`) is silently ignored server-side; a dropped
        required key 400s on every call. Bodies arrive as jpost(url, {…}) or
        api(url, {…, body: JSON.stringify({…})}); a non-JSON body (the file
        upload's raw bytes) carries no fields. Resolving per call site:
        jpost→POST, api→GET unless {method:"X"}."""
        script = _script_body(_html())

        def match_brace(src: str, i: int) -> int:
            """i points at '{'; return the index just past its match."""
            depth = 0
            while i < len(src):
                if src[i] == "{":
                    depth += 1
                elif src[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return i + 1
                i += 1
            return len(src)

        def top_level_keys(obj_src: str) -> set[str]:
            """Top-level keys of an object literal: `k:` pairs and `{k}` shorthand."""
            keys: set[str] = set()
            depth = 0
            part = ""
            for c in obj_src:
                if c in "{[(":
                    depth += 1
                elif c in "}])":
                    depth -= 1
                if c == "," and depth == 0:
                    m = re.match(r"\s*([A-Za-z_$][\w$]*)", part)
                    if m:
                        keys.add(m.group(1))
                    part = ""
                    continue
                part += c
            m = re.match(r"\s*([A-Za-z_$][\w$]*)", part)
            if m:
                keys.add(m.group(1))
            return keys

        # (verb, concrete_path, body-keys-or-None) per call site
        calls: list[tuple[str, str, set[str] | None, str]] = []
        for m in re.finditer(r'(api|jpost)\(\s*[`"](/api/[^`"?]*)', script):
            fn, raw = m.group(1), m.group(2)
            rest_i = m.end()
            # past the closing quote
            while rest_i < len(script) and script[rest_i] in "`\"":
                rest_i += 1
            while rest_i < len(script) and script[rest_i] in " \t":
                rest_i += 1
            verb = "POST" if fn == "jpost" else "GET"
            body_keys: set[str] | None = None
            if rest_i < len(script) and script[rest_i] == ",":
                brace = script.find("{", rest_i)
                if brace >= 0:
                    end = match_brace(script, brace)
                    obj = script[brace + 1:end - 1]
                    if fn == "jpost":
                        body_keys = top_level_keys(obj)
                    else:
                        sm = re.search(r'method\s*:\s*"([A-Z]+)"', obj)
                        if sm:
                            verb = sm.group(1)
                        bm = re.search(r"body\s*:\s*JSON\.stringify\(", obj)
                        if bm:
                            inner_brace = obj.find("{", bm.end())
                            if inner_brace >= 0:
                                inner_end = match_brace(obj, inner_brace)
                                body_keys = top_level_keys(obj[inner_brace + 1:inner_end - 1])
            concrete = re.sub(r"\$\{[^}]*\}", "1", raw).rstrip("/")
            calls.append(
                (verb, concrete, body_keys,
                 f"line {script[:m.start()].count(chr(10)) + 1}")
            )

        # Per handler: fields it reads from the JSON body.
        import ast

        import shoin.server
        server_src = Path(shoin.server.__file__).read_text(encoding="utf-8")
        handlers: dict[str, ast.AST] = {
            n.name: n
            for n in ast.walk(ast.parse(server_src))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("_h_")
        }
        routes = {name: (v, p) for v, p, name in _Handler._ROUTES}

        for verb, path, sent, where in calls:
            name = next(
                (n for n, (v, p) in routes.items() if v == verb and re.match(p, path)),
                None,
            )
            self.assertIsNotNone(name, f"{where}: no route for {verb} {path}")
            assert name is not None
            fn = handlers.get(f"_h_{name}")
            self.assertIsNotNone(fn, f"{where}: route {name} has no handler")
            required: set[str] = set()
            allowed: set[str] = set()
            assert fn is not None
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                key = (
                    node.args[1].value
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                    else None
                )
                if not isinstance(key, str):
                    key = None
                if node.func.attr == "_require" and key:
                    required.add(key)
                    allowed.add(key)
                elif node.func.attr == "_optional_str" and key:
                    allowed.add(key)
                elif node.func.attr == "_optional_id_list" and key:
                    allowed.add(key)
                elif (
                    node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "data"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    allowed.add(node.args[0].value)

            if sent is None:
                self.assertEqual(
                    required, set(),
                    f"{where}: {verb} {path} sends no JSON body but "
                    f"_h_{name} requires {sorted(required)}",
                )
            else:
                self.assertEqual(
                    sent - allowed, set(),
                    f"{where}: {verb} {path} sends {sorted(sent - allowed)} "
                    f"that _h_{name} never reads (allowed: {sorted(allowed)})",
                )
                self.assertEqual(
                    required - sent, set(),
                    f"{where}: {verb} {path} omits required field(s) "
                    f"{sorted(required - sent)} — every call 400s",
                )

    def test_response_fields_match_server_emissions(self) -> None:
        """Every `v.<field>` the UI reads off a fetch response must be a key the
        handler's `_json({…})` actually emits. Renaming a response key
        (`{sources}` → `{items}`) turns every `cur.sources` into `undefined` —
        no 400, no error, just an empty list. Worse than a bad request field:
        completely silent.

        Bindings resolve by scope, not name: `const j = await (await
        api(PATH)).json()` binds `j` to that route's payload for the enclosing
        block; `cur = j` aliases it (assignment, not declaration → survives
        outside the function); `for (const s of cur.sources)` and
        `j.chunks.some(c => …)` bind the element shape;
        `const last = (d.messages||[]).filter(…).pop()` binds it too. A `const
        j = JSON.parse(…)` or param `j` shadows correctly. Multi-path handlers
        emit the INTERSECTION of their `_json` keysets (a key on only some
        paths may still come back undefined). Emitted shapes come from AST:
        dict literals, list comprehensions, calls into shoin.server/shoin.store
        functions (return-shape merge); anything else is opaque — reads under
        it are unverifiable and never fail."""
        script = _script_body(_html())
        n = len(script)
        IDENT = r"[A-Za-z_$][\w$]*"

        # ---- mask strings/comments/regex literals out of the code ----
        code = [False] * n
        depth = [0] * n    # {} depth before char i (code positions)
        pdepth = [0] * n   # () depth before char i
        d = pd = 0
        i = 0
        state = "code"
        tpl_stack: list[int] = []  # {} depth at each ${ — its } re-enters tpl
        KEYWORDS = {
            "return", "typeof", "case", "throw", "in", "of", "new", "delete",
            "void", "do", "else", "yield", "await", "instanceof",
        }
        while i < n:
            c = script[i]
            nxt = script[i + 1] if i + 1 < n else ""
            if state == "code":
                code[i] = True
                depth[i] = d
                pdepth[i] = pd
                if c == "/" and nxt == "/":
                    code[i] = code[i + 1] = False
                    state = "lc"
                    i += 2
                    continue
                if c == "/" and nxt == "*":
                    code[i] = code[i + 1] = False
                    state = "bc"
                    i += 2
                    continue
                if c == "/" and nxt != "=":
                    # regex literal iff the previous code token can't end an expr
                    k = i - 1
                    while k >= 0 and (not code[k] or script[k] in " \t\n"):
                        k -= 1
                    prev = script[k] if k >= 0 else ""
                    is_re = not prev or prev not in ")]}1234567890"
                    if prev.isalnum() or prev in "_$":
                        wm = re.search(r"([A-Za-z_$][\w$]*)$", script[: k + 1])
                        is_re = bool(wm and wm.group(1) in KEYWORDS)
                    if is_re:
                        state = "re"
                        code[i] = False
                        i += 1
                        continue
                if c == "'":
                    state = "sq"
                    code[i] = False
                elif c == '"':
                    state = "dq"
                    code[i] = False
                elif c == "`":
                    state = "tpl"
                    code[i] = False
                elif c == "{":
                    d += 1
                elif c == "}":
                    # interp `}` sits one depth above the recorded ${ depth
                    if tpl_stack and d == tpl_stack[-1] + 1:
                        tpl_stack.pop()
                        d -= 1
                        state = "tpl"
                        code[i] = False
                    else:
                        d -= 1
                elif c == "(":
                    pd += 1
                elif c == ")":
                    pd -= 1
                i += 1
                continue
            if state == "lc":
                if c == "\n":
                    state = "code"
                i += 1
            elif state == "bc":
                if c == "*" and nxt == "/":
                    i += 2
                    state = "code"
                else:
                    i += 1
            elif state == "sq":
                if c == "\\":
                    i += 2
                elif c == "'":
                    state = "code"
                    i += 1
                else:
                    i += 1
            elif state == "dq":
                if c == "\\":
                    i += 2
                elif c == '"':
                    state = "code"
                    i += 1
                else:
                    i += 1
            elif state == "tpl":
                if c == "\\":
                    i += 2
                elif c == "`":
                    state = "code"
                    i += 1
                elif c == "$" and nxt == "{":
                    tpl_stack.append(d)
                    d += 1
                    state = "code"
                    i += 2
                else:
                    i += 1
            elif state == "re":
                if c == "\\":
                    i += 2
                elif c == "[":
                    i += 1
                    # char class: consume to ] (a / inside can't close the regex)
                    while i < n:
                        cc = script[i]
                        if cc == "\\":
                            i += 2
                            continue
                        i += 1
                        if cc == "]":
                            break
                elif c == "/":
                    state = "code"
                    i += 1
                else:
                    i += 1

        brace_match: dict[int, int] = {}
        bstack: list[int] = []
        for p in range(n):
            if not code[p]:
                continue
            if script[p] == "{":
                bstack.append(p)
            elif script[p] == "}" and bstack:
                brace_match[bstack.pop()] = p

        def next_code(pos: int) -> int:
            while pos < n and (not code[pos] or script[pos] in " \t\n"):
                pos += 1
            return pos

        def enclosing_block_end(pos: int) -> int:
            """Position of the `}` closing the tightest {}-block holding pos."""
            d0 = depth[pos]
            for p in range(pos, n):
                if code[p] and script[p] == "}" and depth[p] == d0:
                    return p
            return n

        def next_block_scope(pos: int) -> int:
            """End of the first `{` block after pos (for-of/=>{}/fn-body/catch)."""
            p = pos
            while p < n:
                if code[p]:
                    if script[p] == "{":
                        return brace_match.get(p, n)
                    if script[p] == ";":
                        return p
                p += 1
            return n

        def expr_end(pos: int, pd0: int) -> int:
            """End of an `=>expr` arrow body whose expr starts at paren depth pd0."""
            p = pos
            while p < n:
                if code[p]:
                    c = script[p]
                    if c in ";\n":
                        return p
                    if c in ",)}" and pdepth[p] <= pd0:
                        return p
                p += 1
            return n

        # ---- emitted shapes from shoin.server/shoin.store AST ----
        import ast

        import shoin.server
        import shoin.store

        def funcs_of(mod: object) -> dict[str, ast.AST]:
            src = Path(mod.__file__).read_text(encoding="utf-8")
            return {
                nd.name: nd
                for nd in ast.walk(ast.parse(src))
                if isinstance(nd, (ast.FunctionDef, ast.AsyncFunctionDef))
            }

        FUNCS: dict[str, ast.AST] = {
            **funcs_of(shoin.store), **funcs_of(shoin.server)
        }
        Shape = object  # dict[str, Shape] | ("list", Shape) | None

        def merge(a: Shape, b: Shape) -> Shape:
            if a is None or b is None:
                return None
            if isinstance(a, dict) and isinstance(b, dict):
                return {k: merge(a[k], b[k]) for k in a.keys() & b.keys()}
            if isinstance(a, tuple) and isinstance(b, tuple) and a[0] == b[0] == "list":
                return ("list", merge(a[1], b[1]))
            return None

        def each_node(fn: ast.AST) -> Iterator[ast.AST]:
            # nested FunctionDef bodies are a different scope — prune, don't walk
            """Yield fn's nodes without descending into nested function defs."""
            for ch in ast.iter_child_nodes(fn):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                yield ch
                yield from each_node(ch)

        def shape_of(node: ast.AST | None, seen: frozenset[str] = frozenset()) -> Shape:
            if node is None:
                return None
            if isinstance(node, ast.Dict):
                return {
                    k.value: shape_of(v, seen)
                    for k, v in zip(node.keys, node.values, strict=True)
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
            if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
                return ("list", shape_of(node.elt, seen))
            if isinstance(node, (ast.List, ast.Tuple)):
                return ("list", shape_of(node.elts[0], seen) if node.elts else None)
            if isinstance(node, ast.Call):
                fname = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                    if isinstance(node.func, ast.Name)
                    else ""
                )
                fn = FUNCS.get(fname)
                if fn is None or fname in seen:
                    return None
                assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                merged: Shape = None
                first = True
                for nd in each_node(fn):
                    if isinstance(nd, ast.Return):
                        s = shape_of(nd.value, seen | {fname})
                        merged = s if first else merge(merged, s)
                        first = False
                return merged
            return None

        routes = {name: (v, p) for v, p, name in _Handler._ROUTES}
        emitted: dict[tuple[str, str], Shape] = {}
        for name, (verb, pat) in routes.items():
            fn = FUNCS.get(f"_h_{name}")
            if fn is None:
                continue
            merged: Shape = None
            first = True
            for nd in each_node(fn):
                if (
                    isinstance(nd, ast.Call)
                    and isinstance(nd.func, ast.Attribute)
                    and nd.func.attr == "_json"
                    and nd.args
                ):
                    s = shape_of(nd.args[0])
                    merged = s if first else merge(merged, s)
                    first = False
            if merged is not None:
                emitted[(verb, pat)] = merged

        def route_shape(verb: str, concrete: str) -> Shape:
            for (v, p), s in emitted.items():
                if v == verb and re.match(p, concrete):
                    return s
            return None

        # ---- bindings: (name, start, scope_end, shape) ----
        bindings: list[tuple[str, int, int, Shape]] = []

        def bind(name: str, start: int, end: int, shape: Shape) -> None:
            bindings.append((name, start, end, shape))

        def binding_at(name: str, pos: int) -> Shape:
            best: tuple[int, Shape] | None = None
            for nm, st, en, sh in bindings:
                if nm == name and st <= pos < en and (best is None or st > best[0]):
                    best = (st, sh)
            return best[1] if best else None

        def scope_of_prev(name: str, pos: int) -> int:
            """Scope end for a non-decl assignment: the live binding's scope."""
            end = -1
            for nm, st, en, _ in bindings:
                if nm == name and st <= pos and en > end:
                    end = en
            return end if end >= 0 else n

        def member_shape(base: Shape, field: str) -> Shape:
            return base.get(field) if isinstance(base, dict) else None

        # `v = <rhs>` / `const v = <rhs>` — not `==`, `=>`, or member `x.f =`
        for m in re.finditer(
            rf"(?<![\w$.])(?:(const|let|var)\s+)?({IDENT})\s*=(?!=|>)\s*", script
        ):
            if not code[m.start()]:
                continue
            decl, name = m.group(1), m.group(2)
            rhs_start = m.end()
            e = rhs_start
            while e < n and not (code[e] and script[e] == ";"):
                e += 1
            rhs = script[rhs_start:e]
            start = rhs_start
            end = enclosing_block_end(start) if decl else scope_of_prev(name, start)
            api_m = re.match(
                r"await\s*\(?\s*await\s+(api|jpost)\(\s*[`\"]([^`\"?]*)", rhs
            )
            if api_m:
                shape: Shape = None
                if ".json()" in rhs:
                    verb = "POST" if api_m.group(1) == "jpost" else "GET"
                    if api_m.group(1) == "api":
                        om = re.search(r'method\s*:\s*"([A-Z]+)"', rhs)
                        if om:
                            verb = om.group(1)
                    concrete = re.sub(r"\$\{[^}]*\}", "1", api_m.group(2)).rstrip("/")
                    shape = route_shape(verb, concrete)
                bind(name, start, end, shape)
                continue
            if re.match(r"await\b", rhs):
                # a raw Response/promise — member reads aren't payload fields
                bind(name, start, end, None)
                continue
            # `(<v>.<f>||[])` / `v.f` / `v` / `v[i]` / chains ending .pop()/.at()/.find()/[i]
            sub = re.match(
                rf"^\(?\s*({IDENT})\s*(?:\.\s*({IDENT}))?(?:\s*\|\|\s*\[\s*\])?\s*\)?",
                rhs,
            )
            if sub:
                base = binding_at(sub.group(1), start)
                shape = base
                if sub.group(2):
                    shape = member_shape(base, sub.group(2))
                tail = rhs[sub.end():]
                if (
                    isinstance(shape, tuple)
                    and shape[0] == "list"
                    and re.search(r"\.(pop|at|find)\s*\(|\[\s*\d+\s*\]", tail)
                ):
                    shape = shape[1]
                bind(name, start, end, shape)
                continue
            bind(name, start, end, None)

        # `const|let|var v` with no initializer → unknown
        for m in re.finditer(rf"(?:const|let|var)\s+({IDENT})\s*(?=[;,)])", script):
            if code[m.start()]:
                bind(m.group(1), m.start(), enclosing_block_end(m.start()), None)

        # `for (const w of <expr>)` — element binding over the body block
        for m in re.finditer(rf"for\s*\(\s*(?:const|let)\s+({IDENT})\s+of\s+", script):
            if not code[m.start()]:
                continue
            p = m.end()
            while p < n and not (code[p] and script[p] == ")"):
                p += 1
            expr = script[m.end():p]
            em = re.match(
                rf"^\(?\s*({IDENT})\s*(?:\.\s*({IDENT}))?(?:\s*\|\|\s*\[\s*\])?\s*\)?\s*$",
                expr,
            )
            shape: Shape = None
            if em:
                base = binding_at(em.group(1), m.start())
                if em.group(2):
                    base = member_shape(base, em.group(2))
                if isinstance(base, tuple) and base[0] == "list":
                    shape = base[1]
            bind(m.group(1), m.start(), next_block_scope(p + 1), shape)

        def arrow_scope(arrow_pos: int) -> int:
            bstart = next_code(arrow_pos + 2)
            if bstart < n and script[bstart] == "{":
                return brace_match.get(bstart, n)
            return expr_end(bstart, pdepth[arrow_pos])

        # `.forEach/.map/.filter/…(w =>` — param binds the receiver's element shape
        METHOD = "forEach|map|filter|some|find|every|flatMap|reduce|findIndex|findLast"
        method_arrows: set[int] = set()
        for m in re.finditer(
            rf"\.({METHOD})\s*\(\s*(?:\(\s*{IDENT}(?:\s*,\s*{IDENT})*\s*\)"
            rf"|{IDENT})\s*=>",
            script,
        ):
            if not code[m.start()]:
                continue
            arrow = m.end() - 2
            method_arrows.add(arrow)
            recv_src = script[max(0, m.start() - 120): m.start()]
            rm = re.search(
                rf"(\(?\s*{IDENT}(?:\s*\.\s*{IDENT})*(?:\s*\|\|\s*\[\s*\]\s*)?\)?)\s*$",
                recv_src,
            )
            shape: Shape = None
            if rm:
                parts = re.findall(IDENT, rm.group(1).replace("||", " "))
                if parts:
                    base = binding_at(parts[0], m.start())
                    for f in parts[1:]:
                        base = member_shape(base, f)
                    if isinstance(base, tuple) and base[0] == "list":
                        shape = base[1]
            params_txt = m.group(0)[m.group(0).rindex("(") + 1: m.group(0).rindex("=>")]
            for w in re.findall(IDENT, params_txt):
                bind(w, m.start(), arrow_scope(arrow), shape)

        # generic arrow params `x=>` / `(x,y)=>` outside method calls → unknown
        for m in re.finditer(
            rf"(?:\(\s*({IDENT}(?:\s*,\s*{IDENT})*)\s*\)|({IDENT}))\s*=>", script
        ):
            if not code[m.start()]:
                continue
            arrow = m.end() - 2
            if arrow in method_arrows:
                continue
            params_txt = m.group(1) or m.group(2) or ""
            for w in re.findall(IDENT, params_txt):
                bind(w, m.start(), arrow_scope(arrow), None)

        # function params + catch(e) → unknown over their block
        for m in re.finditer(rf"function\s*{IDENT}?\s*\(([^)]*)\)", script):
            if code[m.start()]:
                for w in re.findall(IDENT, m.group(1)):
                    bind(w, m.start(), next_block_scope(m.end()), None)
        for m in re.finditer(rf"catch\s*\(\s*({IDENT})", script):
            if code[m.start()]:
                bind(m.group(1), m.start(), next_block_scope(m.end()), None)

        # ---- reads: v.f / v.f.g / v[i].f attributed to the innermost binding ----
        violations: list[str] = []
        for m in re.finditer(
            rf"\b({IDENT})\s*(\[\s*\d+\s*\])?(\s*\.\s*{IDENT})+", script
        ):
            if not code[m.start()]:
                continue
            name = m.group(1)
            shape = binding_at(name, m.start())
            if m.group(2) and isinstance(shape, tuple) and shape[0] == "list":
                shape = shape[1]
            if not isinstance(shape, dict):
                continue
            pos = m.start() + len(name)
            line = script[: m.start()].count("\n") + 1
            while pos < n:
                hm = re.match(rf"\s*\.\s*({IDENT})", script[pos:])
                if not hm:
                    break
                f = hm.group(1)
                if f not in shape:
                    violations.append(
                        f"line {line}: {name}.{f} not in emitted keys"
                    )
                    break
                shape = shape[f]
                pos += hm.end()
                if isinstance(shape, tuple) and shape[0] == "list":
                    idx = re.match(r"\s*\[\s*\d+\s*\]", script[pos:])
                    if not idx:
                        break
                    shape = shape[1]
                    pos += idx.end()
                if not isinstance(shape, dict):
                    break

        self.assertEqual(
            violations,
            [],
            "UI reads fields the server never emits:\n" + "\n".join(violations),
        )

    def test_script_hygiene_and_focus_visibility(self) -> None:
        """Two quiet-degradation classes: (a) the visible focus
        indicator — remove the :focus-visible rule or blanket
        outline:none and keyboard users can no longer see where focus
        is; one scoped exemption (.src-rename, whose border is the
        indicator) is the maximum. (b) script hygiene — console.*,
        debugger, eval/new Function, document.write, javascript: URLs,
        and inline on*= handlers all fail CSP-style review and ship
        noise or injection surface to users."""
        html = _html()
        style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
        script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        self.assertRegex(
            style,
            r":focus(-visible)?\s*\{[^}]*outline",
            "no visible focus indicator rule in <style>",
        )
        self.assertLessEqual(
            style.count("outline:none"),
            1,
            "more than the one sanctioned scoped outline:none",
        )
        self.assertEqual(
            re.findall(r"console\.\w+|debugger\b|\beval\s*\(|new Function|"
                       r"document\.write", script),
            [],
            "debug/eval constructs in <script>",
        )
        self.assertEqual(
            re.findall(r'javascript:|\son\w+="', html),
            [],
            "javascript: URL or inline on*= handler present",
        )

    def test_document_structure_contract(self) -> None:
        """Document chrome and outline: the structural bits AT and
        browsers lean on that degrade silently — found in v0.2.361
        that the viewer dialog's heading was h3 after a single h1
        (skipped level). Pins: exactly one non-empty <title>, charset
        and viewport meta, one <main>, exactly one <h1> and no skipped
        heading levels, and no positive tabindex (markup or JS-set:
        positive values fight the natural tab order)."""
        html = _html()
        markup = re.sub(
            r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S
        )
        titles = re.findall(r"<title>([^<]*)</title>", markup)
        self.assertEqual(len(titles), 1, "expected exactly one <title>")
        self.assertTrue(titles[0].strip(), "empty <title>")
        self.assertIn("charset", markup, "missing charset meta")
        self.assertIn("viewport", markup, "missing viewport meta")
        self.assertEqual(
            len(re.findall(r"<main\b", markup)), 1, "expected one <main>"
        )
        headings = [
            int(n) for n in re.findall(r"<h([1-6])\b", markup)
        ]
        self.assertEqual(
            headings.count(1), 1, "expected exactly one <h1>"
        )
        top = 0
        skipped = []
        for n in headings:
            if n > top + 1:
                skipped.append(f"h{top} -> h{n}")
            top = max(top, n)
        self.assertEqual(
            skipped, [], f"heading levels must not skip: {skipped}"
        )
        bad = re.findall(r'tabindex="(\d+)"', html) + list(
            re.findall(r"tabIndex\s*=\s*(\d+)", html)
        )
        positive = [v for v in bad if int(v) > 0]
        self.assertEqual(
            positive, [], f"positive tabindex found: {positive}"
        )

    def test_form_controls_have_accessible_names(self) -> None:
        """A control whose only name is its placeholder loses that name
        the moment the user types — assistive tech then announces an
        unlabeled field. Every markup <input>/<textarea>/<select> needs
        a durable name: aria-label, aria-labelledby, the data-i18n-aria
        indirection, an associated <label for=>, a wrapping <label>, or
        a title. (type=hidden controls exempt.) Found in v0.2.360: the
        four primary inputs were placeholder-only while file/url used
        the data-i18n-aria pattern."""
        html = _html()
        markup = re.sub(
            r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S
        )
        labeled_ids = set(
            re.findall(r'<label\b[^>]*\bfor="([^"]+)"', markup)
        )
        wrapped: set[str] = set()
        for m in re.finditer(r"<label\b[^>]*>(.*?)</label>", markup, re.S):
            for im in re.finditer(r'id="([^"]+)"', m.group(1)):
                wrapped.add(im.group(1))
        violations: list[str] = []
        for m in re.finditer(r"<(input|textarea|select)\b([^>]*)>", markup):
            tag, attrs = m.group(1), m.group(2)
            if 'type="hidden"' in attrs:
                continue
            id_m = re.search(r'id="([^"]+)"', attrs)
            named = (
                "aria-label" in attrs
                or "aria-labelledby" in attrs
                or "data-i18n-aria" in attrs
                or "title=" in attrs
                or (id_m and id_m.group(1) in labeled_ids)
                or (id_m and id_m.group(1) in wrapped)
            )
            if not named:
                violations.append(
                    f"<{tag}{attrs}> has no accessible name "
                    "(placeholder is not durable)"
                )
        self.assertEqual(violations, [], "\n".join(violations))

    def test_a11y_lexical_contract(self) -> None:
        """Misspelled a11y vocabulary fails *silently*: `aria-labelled`
        (no 'by') or `role="tab-panel"` are ignored by assistive tech
        with no error anywhere — the element simply loses its wiring.

        Pins: every `aria-*` name used in markup or written via
        `setAttribute` is a real WAI-ARIA attribute; every `role=` value
        is a real WAI-ARIA role; and the tabs pattern stays complete —
        each `role="tab"` carries `aria-selected` + `aria-controls`,
        each `role="tabpanel"` carries `aria-labelledby`."""
        html = _html()
        aria_attrs = {
            "aria-activedescendant", "aria-atomic", "aria-autocomplete",
            "aria-braillelabel", "aria-brailleroledescription", "aria-busy",
            "aria-checked", "aria-colcount", "aria-colindex",
            "aria-colindextext", "aria-colspan", "aria-controls",
            "aria-current", "aria-describedby", "aria-description",
            "aria-details", "aria-disabled", "aria-dropeffect",
            "aria-errormessage", "aria-expanded", "aria-flowto",
            "aria-grabbed", "aria-haspopup", "aria-hidden", "aria-invalid",
            "aria-keyshortcuts", "aria-label", "aria-labelledby",
            "aria-level", "aria-live", "aria-modal", "aria-multiline",
            "aria-multiselectable", "aria-orientation", "aria-owns",
            "aria-placeholder", "aria-posinset", "aria-pressed",
            "aria-readonly", "aria-relevant", "aria-required",
            "aria-roledescription", "aria-rowcount", "aria-rowindex",
            "aria-rowindextext", "aria-rowspan", "aria-selected",
            "aria-setsize", "aria-sort", "aria-valuemax", "aria-valuemin",
            "aria-valuenow", "aria-valuetext",
        }
        roles = {
            "alert", "alertdialog", "application", "article", "banner",
            "button", "cell", "checkbox", "columnheader", "combobox",
            "complementary", "contentinfo", "definition", "dialog",
            "directory", "document", "feed", "figure", "form", "grid",
            "gridcell", "group", "heading", "img", "link", "list",
            "listbox", "listitem", "log", "main", "marquee", "math",
            "menu", "menubar", "menuitem", "menuitemcheckbox",
            "menuitemradio", "navigation", "none", "note", "option",
            "presentation", "progressbar", "radio", "radiogroup", "region",
            "row", "rowgroup", "rowheader", "scrollbar", "search",
            "searchbox", "separator", "slider", "spinbutton", "status",
            "switch", "tab", "table", "tablist", "tabpanel", "term",
            "textbox", "timer", "toolbar", "tooltip", "tree", "treegrid",
            "treeitem",
        }

        used_aria = set(re.findall(r'\baria-[a-z]+', html))
        self.assertEqual(
            sorted(used_aria - aria_attrs), [],
            "aria-* names outside the WAI-ARIA vocabulary — silently ignored",
        )
        used_roles = set(re.findall(r'role="([^"]+)"', html))
        self.assertEqual(
            sorted(used_roles - roles), [],
            "role= values outside the WAI-ARIA vocabulary — silently ignored",
        )

        tabs = re.findall(r'<button\b(?=[^>]*role="tab")([^>]*)>', html)
        self.assertTrue(tabs, "no role=tab buttons found — tabs pattern gone")
        for attrs in tabs:
            self.assertIn("aria-selected", attrs, "role=tab missing aria-selected")
            self.assertIn("aria-controls", attrs, "role=tab missing aria-controls")
        self.assertTrue(
            re.search(r'role="tablist"', html), "tabs lost their tablist role"
        )
        for m in re.finditer(r'role="tabpanel"([^>]*)', html):
            self.assertIn(
                "aria-labelledby", m.group(1),
                "role=tabpanel missing aria-labelledby",
            )

    def test_css_class_names_stay_in_sync(self) -> None:
        """Both directions of the class-name contract fail silently:

        - JS toggles a class CSS never defines (`classList.add("foo")`
          with no `.foo` rule) — the visual state it was meant to paint
          just doesn't happen.
        - CSS defines a class nothing constructs (`.toast` on a rule
          whose element only carried `id=` — found here in v0.2.358) —
          dead styling that reads as if it works.

        Class names travel through `el("div","cls")`, `className`,
        `classList.*`, `class="..."`, and composed strings like
        `"seal "+k`, so "constructed" means: appears as a word inside
        any quoted literal in the file."""
        html = _html()
        style = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
        defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", style))
        self.assertTrue(defined, "no CSS classes found — <style> scan broken")

        class_ctx: set[str] = set()

        def absorb(v: str) -> None:
            for w in v.split():
                if w and not w.startswith(("$", "{")):
                    class_ctx.add(w)

        for m in re.finditer(
            r'class\s*=\s*"([^"]*)"|class\s*=\s*\'([^\']*)\'', html
        ):
            absorb(m.group(1) or m.group(2) or "")
        for m in re.finditer(
            r'classList\.(?:add|remove|toggle|contains)\("([^"]+)"\)', html
        ):
            absorb(m.group(1))
        for m in re.finditer(r"className\s*=\s*([^;]+);", html):
            for lit in re.findall(r'"([^"]*)"', m.group(1)):
                absorb(lit)
        for m in re.finditer(r'el\("[a-z0-9]+",\s*((?:"[^"]*"|[^,])+)', html):
            argtext = re.sub(r'el\("[a-z0-9]+"', "", m.group(1))
            for lit in re.findall(r'"([^"]*)"', argtext):
                absorb(lit)

        self.assertEqual(
            sorted(class_ctx - defined),
            [],
            "markup/JS uses classes the stylesheet never defines",
        )

        words: set[str] = set()
        for lit in re.findall(r'"([^"\n]*)"', html) + re.findall(r"'([^'\n]*)'", html):
            words.update(lit.split())
        self.assertEqual(
            sorted(defined - words),
            [],
            "CSS classes nothing constructs — dead styling",
        )

    def test_markup_health_and_offline_scope(self) -> None:
        """Markup invariants that fail silently rather than loudly.

        - `id=` must be unique: `$("#x")` binds the FIRST element, so a
          duplicate silently re-routes every lookup to the wrong node.
        - `<html lang>` seeds the initial a11y locale and
          `documentElement.lang` must be written on toggle — a removed
          assignment leaves screen readers pronouncing EN text as JA.
        - `<button>` inside `<form>` defaults to type="submit": one added
          without an explicit type turns every click into a form post.
        - No `src`/`href="http…"` anywhere: the app is offline by design
          and CSP `connect-src 'self'` + `default-src 'none'` would break
          the reference anyway — one sneaks in only as a dead feature."""
        html = _html()
        script = _script_body(html)

        ids = re.findall(r'\bid="([^"]+)"', html)
        dup = sorted({x for x in ids if ids.count(x) > 1})
        self.assertEqual(dup, [], f"duplicate id= values: {dup}")

        html_m = re.search(r'<html lang="([a-z]+)"', html)
        self.assertIsNotNone(html_m, "<html> needs a lang attribute")
        assert html_m is not None
        self.assertIn(html_m.group(1), ("ja", "en"),
                      f"html lang={html_m.group(1)!r} outside the supported locales")
        self.assertTrue(
            re.search(r"documentElement\.lang\s*=", script),
            "applyI18n must update documentElement.lang on toggle",
        )

        for m in re.finditer(r"<form\b[^>]*>(.*?)</form>", html, re.S):
            for b in re.finditer(r"<button\b([^>]*)>", m.group(1)):
                self.assertIn(
                    "type=", b.group(1),
                    f"<button> inside <form> defaults to submit — "
                    f"give it an explicit type: {b.group(0)!r}",
                )

        external = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+)"', html)
        self.assertEqual(external, [],
                         f"external resource references (offline + CSP): {external}")

    def test_dark_mode_palette_is_complete(self) -> None:
        """v0.2.638: `@media (prefers-color-scheme:dark)` re-skins the app by
        overriding the palette — whatever it misses silently stays light.

        - The block must redefine every surface variable the light `:root`
          declares (washi/paper/sumi/sumi-soft/seiji-ink/shu/kohaku/matsu/
          border/border-strong) to a different hex — a forgotten var leaves
          a light surface under dark text or dark text on a dark pane.
        - Surfaces meant to stay dark (header band, toast) must bind the
          dedicated `--band`/`--band-ink` vars — remapping `--sumi` to a
          light color would invert them into light bands, and leaving them
          on `--washi` keeps them light-texted on a dark band.
        - Tinted literal surfaces (.badge.warn/.err/.dim, #banner) are not
          var-driven — each needs a literal override inside the block."""
        html = _html()
        style = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
        m = re.search(
            r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{(.*?)\n\}",
            style, re.S,
        )
        self.assertIsNotNone(
            m, "no prefers-color-scheme:dark block in <style>")
        assert m is not None
        block = m.group(1)

        for var in (
            "--washi", "--paper", "--sumi", "--sumi-soft", "--seiji-ink",
            "--shu", "--kohaku", "--matsu", "--border", "--border-strong",
        ):
            light = re.search(rf"{var}:(#[0-9A-Fa-f]+)", style)
            dark = re.search(rf"{var}:(#[0-9A-Fa-f]+)", block)
            self.assertIsNotNone(
                dark, f"{var} not redefined inside the dark block")
            assert light is not None and dark is not None
            self.assertNotEqual(
                light.group(1), dark.group(1),
                f"{var} identical in light and dark palettes",
            )

        for sel in (r"header\{", r"#toast\{"):
            rule = re.search(rf"{sel}[^}}]*}}", style)
            self.assertIsNotNone(rule, f"{sel} rule not found")
            assert rule is not None
            body = rule.group(0)
            self.assertIn("var(--band)", body,
                          f"{sel} band background not on --band")
            for var in ("--sumi", "--washi"):
                self.assertNotIn(
                    f"var({var})", body,
                    f"{sel} paints the band from {var} — inverts under "
                    "dark mode",
                )
        for sel in (".badge.warn", ".badge.err", ".badge.dim", "#banner"):
            self.assertIn(
                sel, block,
                f"{sel} keeps its light literal bg under dark mode",
            )

    def test_print_styles_fold_chrome_and_expand_panes(self) -> None:
        """v0.2.640: `@media print` turns the three-pane app into paper —
        chrome folds, panes flatten, the palette is forced light.

        - The block must hide the interactive chrome (buttons, inputs,
          composer, adders, tabs, toast, banner) — otherwise a printed
          answer carries the UI skeleton.
        - The grid/scroll layout must flatten (`main{display:block}`,
          `.pane{display:block}`, `.pane-body{overflow:visible}`) —
          without it only the visible scroll window prints.
        - `--washi` must re-map to a light hex: under
          prefers-color-scheme:dark the dark block still matches in
          print and would emit light text on white paper.
        - Messages need page-break-inside:avoid so a Q/A isn't split
          across pages."""
        html = _html()
        style = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
        m = re.search(r"@media\s+print\s*\{(.*)", style, re.S)
        self.assertIsNotNone(m, "no @media print block in <style>")
        assert m is not None
        block = m.group(1)

        for sel in (
            "#composer", "#banner", "#toast", ".tabs", ".adders",
            ".note-form", ".kinds", "button", "input",
        ):
            self.assertIn(
                sel, block,
                f"{sel} chrome still prints (not hidden by @media print)",
            )
        self.assertRegex(block, r"main\{[^}]*display:block")
        self.assertRegex(block, r"\.pane[^{]*\{[^}]*display:block")
        self.assertRegex(block, r"\.pane-body\{[^}]*overflow:visible")
        washi = re.search(r"--washi:(#[0-9A-Fa-f]+)", block)
        self.assertIsNotNone(
            washi, "print block does not re-map --washi to a light hex")
        assert washi is not None
        self.assertGreater(
            min(int(washi.group(1)[i:i + 2], 16) for i in (1, 3, 5)), 200,
            "--washi under print must be paper-white, not a dark hex",
        )
        self.assertIn("page-break-inside:avoid", block)
        ids = set(re.findall(r'\bid="([^"]+)"', html))
        for sel in re.findall(r"#[A-Za-z_][\w-]*", block):
            if not re.fullmatch(r"#[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3})?", sel):
                self.assertIn(
                    sel[1:], ids,
                    f"{sel} in print block matches no element id",
                )

    def test_narrow_viewport_contract(self) -> None:
        """v0.2.641: the ≤880px layout is a contract, not a nicety — the
        mobile audit (product-review #30) pinned what keeps the app usable
        on a phone-width viewport.

        - `main` must collapse to one column and panes must switch: a
          non-active `.pane` hides, `.pane.active` shows, and `.tabs`
          becomes visible — the only path between panes when the three
          columns no longer fit.
        - Flex form rows overflow without `min-width:0`: a text input's
          intrinsic width beats flex shrinking (min-width:auto), so every
          input inside `.row`/`#askForm` needs the override.
        - The viewer sheet's padding must shrink — 24px×2 of chrome at
          360px eats a tenth of the sheet."""
        html = _html()
        style = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
        m = re.search(r"@media\s*\(max-width:\s*880px\)\s*\{(.*?)\n\}", style, re.S)
        self.assertIsNotNone(m, "no max-width:880px block in <style>")
        assert m is not None
        block = m.group(1)

        self.assertRegex(block, r"main\{[^}]*grid-template-columns:1fr")
        self.assertRegex(block, r"\.pane\{[^}]*display:none")
        self.assertRegex(block, r"\.pane\.active\{[^}]*display:flex")
        self.assertRegex(block, r"\.tabs\{[^}]*display:flex")
        self.assertIn(".tabs button", block)
        for inp in ("#askInput", "#nbName", "#urlInput"):
            self.assertIn(
                inp, block,
                f"{inp} lacks min-width:0 — overflows its flex row at ≤880px",
            )
        self.assertIn("min-width:0", block)
        self.assertRegex(block, r"#viewer\.open\{[^}]*padding:")

        # every tab's data-pane target and every pane's controller must
        # line up — a one-way switch strands a pane with no way back
        panes = set(re.findall(r'class="pane(?:\s[^"]*)?"\s+id="([^"]+)"', html))
        targets = set(re.findall(r'data-pane="([^"]+)"', html))
        self.assertEqual(targets, panes,
                         f"tab/pane mismatch: tabs={targets} panes={panes}")

    def test_every_id_reference_resolves_to_an_element(self) -> None:
        """A $("#id") or getElementById("id") with no matching id= attribute is
        a silent TypeError on the next interaction — renames of the element
        never reach the lookups. The same applies to markup references:
        for=, aria-labelledby/controls/describedby/owns/activedescendant, and
        href="#id" — a stale one silently unwires the a11y tree. Pin every
        literal id reference to a real id."""
        html = _html()
        ids = set(re.findall(r'\bid="([^"]+)"', html))
        refs = set(re.findall(r'\$\(\s*"#([A-Za-z0-9_-]+)"\s*\)', html))
        refs |= set(
            re.findall(r'getElementById\(\s*["\']([A-Za-z0-9_-]+)["\']\s*\)', html)
        )
        for attr in (
            "for",
            "aria-labelledby",
            "aria-controls",
            "aria-describedby",
            "aria-owns",
            "aria-activedescendant",
        ):
            for value in re.findall(rf'\b{attr}="([^"]+)"', html):
                # These attributes hold space-separated id lists.
                refs.update(value.split())
        refs |= set(re.findall(r'href="#([A-Za-z0-9_-]+)"', html))
        self.assertTrue(refs, "expected id lookups in index.html")
        missing = refs - ids
        self.assertEqual(
            missing, set(),
            f"id lookups with no element: {sorted(missing)} — "
            "renamed the element, or the lookup targets a dead id",
        )

    def test_renderFullSource_verifies_chunk_against_excerpt(self) -> None:
        """v0.2.230: chunks.id is a plain rowid — after refresh_source() replaces
        a source's chunks, new chunks can reuse the ids an old report stored, so
        an id match alone can pin the 'cited here' mark on text the citation
        never saw. renderFullSource must only mark a chunk whose head appears in
        the stored excerpt — and keep the old id-only behavior without one."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        start = src.index("function renderFullSource")
        depth, end = 0, start
        for i in range(start, len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        fn = src[start:end]
        harness = """\
let appended = [];
function el(tag, cls, text){ return {tag, cls, text, children: [],
  prepend(x){this.children.unshift(x)}, append(x){this.children.push(x)},
  scrollIntoView(){}} }
function t(k){ return k }
const container = { replaceChildren(){ appended = [] }, append(x){ appended.push(x) } }
""" + fn + """
renderFullSource(container,
  [{id:5, text:"全く別の文。"}, {id:6, text:"甲。乙。"}], [5, 6], "甲。乙。")
const marked = appended.filter(b => b.cls === "src-chunk cited-chunk").map(b => b.text)
if (JSON.stringify(marked) !== JSON.stringify(["甲。乙。"]))
  { console.error("stale-id marking: " + JSON.stringify(marked)); process.exit(1) }
renderFullSource(container, [{id:5, text:"全く別の文。"}], [5], null)
if (!appended.some(b => b.cls === "src-chunk cited-chunk"))
  { console.error("id marking lost without excerpt"); process.exit(1) }
console.log("ok")
"""
        with tempfile.TemporaryDirectory() as d:
            js = Path(d) / "ui.mjs"
            js.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                [node, str(js)], capture_output=True, text=True, timeout=60
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_renderNotebook_clears_export_links_without_notebook(self) -> None:
        """v0.2.179 defect class: after the last notebook is deleted, the export
        links must lose their href — a stale /api/notebooks/{deleted}/export URL
        otherwise stays visible and silently 404s on click. Executes the real
        renderNotebook under node with a stub DOM and asserts both directions:
        hrefs set when a notebook is open, removed when cur goes null."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        fn = _js_block(src, "function renderNotebook")
        scope_fns = _js_block(src, "function scopeSelection") + _js_block(
            src, "function updateScopeInfo"
        )
        harness = """\
const reg = {};
function $(sel){
  if (!reg[sel]) reg[sel] = {hidden:false, textContent:"",
    replaceChildren(){}, append(){}, setAttribute(){}, contains(){return false},
    removeAttribute(n){ delete this[n]; }};
  return reg[sel];
}
function el(tag, cls, text){ return {tag, cls, text, children:[],
  append(x){this.children.push(x)}, setAttribute(){}, querySelector(){return null}} }
function t(k){ return k }
const document = { activeElement: null };
let srcIndex = new Map();
let cur = {id: 7, name: "nb", sources: []};
const srcSel = new Set(), knownIds = new Set(); let selNb = -1;
let externalPendingRename = null;
function renderChatHistory(){} function renderStudio(){}
function renderNotes(){} function refreshQuestions(){}
function startSourceRename(){ return {setSelectionRange(){}} }
""" + fn + scope_fns + """
renderNotebook();
if (reg["#exMd"].href !== "/api/notebooks/7/export?format=md")
  { console.error("export href not bound: " + reg["#exMd"].href); process.exit(1) }
cur = null;
renderNotebook();
if (reg["#exMd"].href !== undefined || reg["#exBib"].href !== undefined
    || reg["#exRis"].href !== undefined)
  { console.error("stale export href survived cur=null"); process.exit(1) }
console.log("ok")
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_lang_resolution_prefers_valid_server_code(self) -> None:
        """v0.2.177 defect class: SHOIN_LANG never reached the UI. The resolver
        runs once at script load — localStorage beats the server-injected meta
        tag; an unsubstituted __SHOIN_LANG__ placeholder (length > 2) is rejected
        by the length check, not string compare; a locale with no I18N table
        falls back to en. Executes the real resolver + I18N + t() under node."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        i18n = _js_block(src, "const I18N = {")
        # The resolver block: `const _serverLang` .. `const t = ...` (applyI18n
        # follows t and is not part of the contract under test).
        i1 = src.index("const _serverLang")
        i2 = src.index("function applyI18n")
        resolver = src[i1:i2]
        harness = i18n + """
function resolveLang(lsv, metaContent, nav){
  const localStorage = { getItem(k){ return lsv; }, setItem(){} };
  const _lsGet = k => { try{ return localStorage.getItem(k) }catch(e){ return null } };
  const _lsSet = (k,v) => { try{ localStorage.setItem(k,v) }catch(e){} };
  const document = { querySelector(s){
      return metaContent === null ? null : {content: metaContent} } };
  const navigator = { language: nav };
""" + resolver + """
  return {lang, t};
}
const r1 = resolveLang(null, "en", "ja");
if (r1.lang !== "en" || r1.t("app.title") !== I18N.en["app.title"])
  { console.error("server code not honored: " + r1.lang); process.exit(1) }
const r2 = resolveLang(null, "__SHOIN_LANG__", "ja");
if (r2.lang !== "ja")
  { console.error("unsubstituted placeholder leaked: " + r2.lang); process.exit(1) }
const r3 = resolveLang("en", "ja", "ja");
if (r3.lang !== "en")
  { console.error("localStorage did not win: " + r3.lang); process.exit(1) }
const r4 = resolveLang(null, null, "fr");
if (r4.lang !== "en" || r4.t("app.title") !== I18N.en["app.title"])
  { console.error("unknown locale not falling back: " + r4.lang); process.exit(1) }
console.log("ok")
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_renderWithSeals_styles_each_flag_class(self) -> None:
        """The seal chip is the visual proof of verification — every warning
        check's flag must change the chip class, not just its tooltip. Executes
        the real renderWithSeals under node and asserts the full class matrix:
        invalid→bad; misattributed/numeric/unit/negation→mis; confirmed→ok;
        unflagged→plain; full-width ［Ｓ］ normalized; combined [S1, S2] → two
        chips; non-citation brackets stay text."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        fn = _js_block(src, "function renderWithSeals")
        harness = """\
function el(tag, cls, text){ return {tag, cls, text, children:[],
  append(x){this.children.push(x)}, setAttribute(){}} }
function t(k){ return k }
const document = { createTextNode(s){ return {text: s, isText: true} } };
const srcIndex = new Map();
function openSeal(){}
const container = { children: [], replaceChildren(){ this.children = [] },
  append(x){ this.children.push(x) } };
""" + fn + """
renderWithSeals(container,
  "甲 [S1] 乙 [S2] 丙 [S3] 丁 [S4] 戊 [S5] 己 [S6] 庚 [S7] ［Ｓ８］ [S1, S4] [note]",
  {invalid: [3], misattributed: [4], numeric_mismatch: [5],
   unit_mismatch: [6], negation_mismatch: [7], confirmed: [1, 8],
   source_map: {}});
const chips = container.children.filter(c => c.cls && c.cls.startsWith("seal"));
const got = chips.map(c => c.cls);
const want = ["seal ok","seal","seal bad","seal mis","seal mis","seal mis",
              "seal mis","seal ok","seal ok","seal mis"];
if (JSON.stringify(got) !== JSON.stringify(want))
  { console.error("chip classes: " + JSON.stringify(got)); process.exit(1) }
if (!container.children.some(c => c.isText && c.text.includes("[note]")))
  { console.error("non-citation bracket lost"); process.exit(1) }
console.log("ok")
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_reportBadges_covers_every_flag(self) -> None:
        """v0.2.235: reportBadges is the single badge chain shared by the live
        SSE path and the persisted-history path (the duplication is how
        negation_mismatch warned in badges while the seal stayed unstyled).
        Executes the real function under node and asserts every flag lands a
        badge of the right class — plus the coverage badge only fires on a
        real number, not a missing/null coverage field."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        fn = _js_block(src, "function reportBadges")
        harness = """\
function el(tag, cls, text){ return {tag, cls, text, children:[],
  append(x){this.children.push(x)}, setAttribute(){}} }
function t(k){ return k }
const COVERAGE_LOW = 0.5;
function mkc(){ return {children: [], append(x){ this.children.push(x) }} }
""" + fn + """
const c = mkc();
reportBadges(c, {
  degraded: true, invalid: [9], misattributed: [4],
  misattributed_suggested: {S4: "S1"}, numeric_mismatch: [5],
  unit_mismatch: [6], negation_mismatch: [7], confirmed: [1, 2],
  uncited: ["句a", "句b"], uncited_supported: ["句b"],
  uncited_supported_source: {"句b": "S3"}, degenerate: ["x","x","x"],
  self_contradiction: ["y","z"], cited: [1], coverage: 0.3,
  truncated: true});
const classes = c.children.map(b => b.cls);
const want = ["badge dim","badge err","badge err","badge err","badge err",
              "badge err","badge dim","badge warn","badge warn","badge warn",
              "badge warn","badge warn"];
if (JSON.stringify(classes) !== JSON.stringify(want))
  { console.error("badges: " + JSON.stringify(classes)); process.exit(1) }
const cbadges = c.children.filter(
    b => b.cls === "badge warn" && String(b.text).includes("coverage"));
if (cbadges.length !== 1) { console.error("coverage badge missing"); process.exit(1) }
// v0.2.245: a finish_reason "length" answer must carry a visible warning chip.
const tbadges = c.children.filter(
    b => b.cls === "badge warn" && String(b.text).includes("truncated"));
if (tbadges.length !== 1) { console.error("truncated badge missing"); process.exit(1) }
const c2 = mkc();
reportBadges(c2, {cited: [1], coverage: null});
if (c2.children.length !== 0)
  { console.error("null coverage fired a badge"); process.exit(1) }
const c3 = mkc();
reportBadges(c3, {});
if (c3.children.length !== 0) { console.error("empty report made badges"); process.exit(1) }
console.log("ok")
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_renderStudio_shows_all_warning_badges(self) -> None:
        """v0.2.235: a Studio card's heading badges were a third badge chain —
        and it silently dropped numeric_mismatch, unit_mismatch, negation,
        misattributed_suggested, degraded and confirmed. A fabricated number
        inside a briefing must warn there exactly as it does in chat. Executes
        the real renderStudio + reportBadges under node: every flag lands."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        harness = (
            """\
function el(tag, cls, text){ return {tag, cls, text, children:[],
  append(x){this.children.push(x)}, setAttribute(){}} }
function t(k){ return k }
const COVERAGE_LOW = 0.5;
function renderWithSeals(){}
const out = {children: [], replaceChildren(){ this.children = [] },
  append(x){ this.children.push(x) }};
const $ = s => s === "#studioOut" ? out : {replaceChildren(){}, append(){}};
let cur = {studio: [{kind: "briefing", body: "甲 [S1]",
  report: {invalid: [9], misattributed: [4], numeric_mismatch: [5],
           unit_mismatch: [6], negation_mismatch: [7], confirmed: [1],
           uncited: ["句"], degenerate: ["x"], self_contradiction: ["y"],
           cited: [1], coverage: 0.3}}]};
"""
            + _js_block(src, "function reportBadges")
            + "\n"
            + _js_block(src, "function renderStudio")
            + """
renderStudio();
const h = out.children[0].children[0];
const texts = h.children.filter(b => b.cls && b.cls.startsWith("badge"))
  .map(b => b.cls);
const want = ["badge err","badge err","badge err","badge err","badge err",
              "badge dim","badge warn","badge warn","badge warn","badge warn"];
if (JSON.stringify(texts) !== JSON.stringify(want))
  { console.error("studio badges: " + JSON.stringify(texts)); process.exit(1) }
console.log("ok")
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_openSeal_disambiguates_title_collision(self) -> None:
        """v0.2.239: on pre-source_id_map reports the title fallback opened the
        FIRST same-titled source — silent misattribution when two sources share
        a title. Executes the real openSeal under node: with a collision it
        probes candidates' chunks and opens the one containing the excerpt
        head; single match and id-less reports keep the old fast paths."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        harness = (
            """\
let _sealSeq = 0;
const calls = [];
function showSource(id){ calls.push(id) }
function toast(){}
const srcIndex = new Map([[1, {s:1, title:"dup"}],
                        [2, {s:2, title:"dup"}],
                        [3, {s:3, title:"other"}]]);
const fetchLog = [];
const chunkA = {text: "まったく別の本文がここにある。"};
const chunkB = {text: "引用箇所のテキストはここにある。後続の文も続く。"};
async function api(path){ fetchLog.push(path);
  return {json: async () => ({chunks:
    path === "/api/sources/2/text" ? [chunkB] : [chunkA]})}; }
const excerpt = "前文。引用箇所のテキストはここにある。後続の文も続く。後文";
(async () => {
  await openSeal(1, {"S1": "dup"}, null, {"S1": excerpt}, null, null, null);
  if (calls[0] !== 2)
    { console.error("collision picked source " + calls[0]); process.exit(1) }
  calls.length = 0;
  await openSeal(2, {"S2": "other"}, null, {"S2": excerpt}, null, null, null);
  if (calls[0] !== 3) { console.error("single match broke"); process.exit(1) }
  calls.length = 0;
  await openSeal(3, {"S3": "dup"}, null, null, null, null, null);
  if (calls[0] !== 1) { console.error("no-excerpt fallback broke"); process.exit(1) }
  console.log("ok")
})();
"""
        )
        rc, out = _run_node(harness + _js_block(src, "async function openSeal"))
        self.assertEqual(rc, 0, out)

    def test_done_handler_toggles_degraded_pane_badge(self) -> None:
        """v0.2.240: #degBadge ("検索のみ" pane-head indicator) was dead UI —
        never unhidden, while the SSE done frame carried `degraded` all along.
        Executes the real `else if (ev==="done")` block under node and asserts
        the badge tracks j.degraded in both directions."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        harness = (
            """\
const degBadge = {hidden: true};
const chatEl = {scrollTop: 0, scrollHeight: 0};
const $ = s => s === "#degBadge" ? degBadge : s === "#chat" ? chatEl : {};
function el(t2, c, txt){ return {tag:t2, cls:c, text:txt, children:[],
  append(x){this.children.push(x)}} }
function t(k){ return k }
function renderWithSeals(){}
function reportBadges(){}
const bd = {parentElement: {kids: [], append(x){this.kids.push(x)}}};
const acc = "回答テキスト";
let gotDone = false, failed = false;
function runDone(j){ let ev = "done"; if (false) {}
"""
            + _js_block(src, 'else if (ev==="done")')
            + """
}
runDone({report: {degraded: true}, degraded: true});
if (degBadge.hidden !== false)
  { console.error("badge not shown on degraded done"); process.exit(1) }
runDone({report: {}, degraded: false});
if (degBadge.hidden !== true)
  { console.error("badge not hidden on normal done"); process.exit(1) }
console.log("ok")
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_sse_error_event_is_surfaced(self) -> None:
        """v0.2.241: the server emits `ev==="error"` mid-stream
        ({"code","message"}) but the client dispatch only handled
        meta/delta/done — a mid-stream failure left a partial answer frozen
        with no signal at all. Executes the real error branch under node and
        asserts the toast carries the server message."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        try:
            block = _js_block(src, 'else if (ev==="error")')
        except ValueError:
            self.fail("SSE error event has no handler branch in the dispatch")
        harness = (
            """\
const toasts = [];
let gotDone = false, failed = false;
function toast(m){ toasts.push(m) }
function runErr(j){ let ev = "error"; if (false) {}
"""
            + block
            + """
}
runErr({code: "SYSTEM_INTERNAL_ERROR", message: "mid-stream boom"});
if (toasts.length !== 1 || !String(toasts[0]).includes("mid-stream boom"))
  { console.error("error event not surfaced: " + JSON.stringify(toasts)); process.exit(1) }
runErr({code: "SOME_CODE"});
if (!String(toasts[1]).includes("SOME_CODE"))
  { console.error("code-only error fell through: " + JSON.stringify(toasts)); process.exit(1) }
console.log("ok")
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_no_dead_sse_meta_store(self) -> None:
        """v0.2.242: the dispatch once stored `meta = j` in a variable that was
        never read — the meta frame's payload (sources {s,title,source_id}) is
        fully redundant with the done frame's report (source_map /
        source_id_map). Guards against reintroducing a dead meta store; the
        frame itself is still consumed-and-advanced by the parser above."""
        src = _script_body(_html())
        self.assertNotIn('ev==="meta"', src)

    def test_sse_payload_fields_match_the_envelope(self) -> None:
        """Per event, the fields the JS dispatcher reads off `j` must be a
        subset of the keys the server's `_sse("ev", {...})` payload dicts emit.
        Renaming `report`→`summary` in the done frame would leave every seal
        and badge silently absent — the parser still runs, `j.report` is just
        undefined. The report.* inner keys are pinned separately; this is the
        envelope."""
        import ast

        import shoin.server

        server_src = Path(shoin.server.__file__).read_text(encoding="utf-8")
        # Per event, collect each emission site's top-level payload keys.
        # Sites must be symmetric: a union would hide one path dropping a key.
        emitted: dict[str, list[set[str]]] = {}
        for node in ast.walk(ast.parse(server_src)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_sse"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                continue
            ev = str(node.args[0].value)
            keys: set[str] = set()
            if len(node.args) > 1 and isinstance(node.args[1], ast.Dict):
                keys = {
                    str(k.value)
                    for k in node.args[1].keys
                    if isinstance(k, ast.Constant)
                }
            emitted.setdefault(ev, []).append(keys)

        src = _script_body(_html())
        for ev in ("delta", "done", "error"):
            sites = emitted.get(ev, [])
            self.assertTrue(sites, f"server never emits an '{ev}' frame")
            first = sites[0]
            for i, site in enumerate(sites[1:], 1):
                self.assertEqual(
                    site, first,
                    f"SSE '{ev}' frame: emission site {i} carries "
                    f"{sorted(site)} but site 0 carries {sorted(first)} — "
                    "asymmetric envelope",
                )
            block = _js_block(src, f'ev==="{ev}"')
            reads = set(re.findall(r"\bj\.([a-zA-Z_]+)", block))
            missing = reads - first
            self.assertEqual(
                missing, set(),
                f"SSE '{ev}' frame: JS reads {sorted(missing)} but the "
                f"envelope only carries {sorted(first)}",
            )

    def test_dropped_stream_restores_persisted_answer(self) -> None:
        """v0.2.246: when the SSE stream ends without a done frame (a proxy or
        network cut), the server has already persisted the complete assistant
        message + report, but the live bubble used to keep the seal-less
        partial text — diverging silently from what a reload renders. The
        recovery path re-fetches the notebook and re-renders the stored
        message through the same renderWithSeals/reportBadges chain. Executes
        the real block under node."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        try:
            block = _js_block(src, "if (!gotDone && !failed)")
        except ValueError:
            self.fail("no recovery block for a done-less stream end")
        harness = (
            """\
const calls = {render: [], badges: [], toasts: [], appended: [], api: 0};
const degBadge = {hidden: true};
const $ = s => s === "#degBadge" ? degBadge : {};
function t(k){ return k }
function el(t2, c, txt){ return {tag:t2, cls:c, text:txt, children:[],
  append(x){ this.children.push(x) }} }
const bd = {cleared: 0, parentElement: {append(x){ calls.appended.push(x) }},
  replaceChildren(){ this.cleared++ }};
function renderWithSeals(b, body, report){ calls.render.push({body, report}) }
function reportBadges(c, report){ calls.badges.push(report);
  if (report.degraded) c.append({cls: "badge dim"}) }
function toast(m){ calls.toasts.push(m) }
const nbId = 7;
let apiImpl = async () => ({json: async () => ({messages: [
  {role: "user", body: "q", report: null},
  {role: "assistant", body: "full answer [S1]", report: {degraded: true}}]})});
const api = p => { calls.api++; return apiImpl(p) };
async function run(gotDone, failed){ let acc = "partial text";
"""
            + block
            + """
  return acc }
let acc = await run(false, false);
if (acc !== "full answer [S1]")
  { console.error("persisted answer not restored: " + acc); process.exit(1) }
if (calls.render.length !== 1 || calls.render[0].report.degraded !== true)
  { console.error("render: " + JSON.stringify(calls.render)); process.exit(1) }
if (degBadge.hidden !== false)
  { console.error("degBadge not driven by restored report"); process.exit(1) }
if (calls.appended.length !== 1 || bd.cleared !== 1)
  { console.error("bubble/cites not rebuilt"); process.exit(1) }
calls.api = 0;
acc = await run(true, false);
if (acc !== "partial text" || calls.api !== 0)
  { console.error("refetched after a normal done frame"); process.exit(1) }
acc = await run(false, true);
if (calls.api !== 0)
  { console.error("refetched after an error frame"); process.exit(1) }
apiImpl = async () => { throw new Error("down") };
acc = await run(false, false);
if (!calls.toasts.some(m => String(m).includes("stream_dropped")))
  { console.error("no drop toast on refetch failure"); process.exit(1) }
apiImpl = async () => ({json: async () => ({messages: [{role: "user", body: "q"}]})});
acc = await run(false, false);
if (acc !== "partial text")
  { console.error("partial clobbered when nothing persisted"); process.exit(1) }
if (calls.toasts.length !== 2)
  { console.error("toasts: " + calls.toasts.length); process.exit(1) }
console.log("ok")
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_open_notebook_drops_out_of_order_responses(self) -> None:
        """v0.2.249: openNotebook() resolves races by sequence. Clicking
        notebook A then B used to be last-*write*-wins: if A's slower response
        landed after B's, cur ended up on A — the pane shows the notebook the
        user did not select last. The _nbSeq guard (same shape as _sealSeq)
        makes the newest call win and discards stale resolves. Executes the
        real function under node."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        try:
            block = _js_block(src, "async function openNotebook")
        except ValueError:
            self.fail("no openNotebook function in index.html")
        harness = (
            """\
let _nbSeq = 0;
let cur = null;
const calls = {rendered: [], loaded: [], toasts: []};
function renderNotebook(){ calls.rendered.push(cur && cur.id) }
function loadNotebooks(){ calls.loaded.push(cur && cur.id) }
function toast(m){ calls.toasts.push(String(m)) }
const pending = {};
function api(url){ return new Promise((res, rej) => { pending[url] = {res, rej} }) }
const tick = () => new Promise(r => setTimeout(r, 0));
const nbUrl = id => `/api/notebooks/${id}`;
"""
            + block
            + """
async function main(){
  // Out-of-order resolve: B resolves first, A lands second — cur must stay B.
  openNotebook(1); openNotebook(2);
  pending[nbUrl(2)].res({json: async () => ({id: 2})});
  await tick();
  if (!cur || cur.id !== 2) throw new Error("latest selection lost");
  pending[nbUrl(1)].res({json: async () => ({id: 1})});
  await tick();
  if (cur.id !== 2) throw new Error("stale response overwrote newer selection: " + cur.id);
  // A lone call still opens normally.
  openNotebook(3);
  pending[nbUrl(3)].res({json: async () => ({id: 3})});
  await tick();
  if (cur.id !== 3) throw new Error("normal open broken: " + cur.id);
  // A stale failure must not toast (the user already moved on); a fresh one must.
  openNotebook(4); openNotebook(5);
  pending[nbUrl(5)].res({json: async () => ({id: 5})});
  await tick();
  pending[nbUrl(4)].rej(new Error("old-failure"));
  await tick();
  if (calls.toasts.length) throw new Error("stale error toasted");
  openNotebook(6);
  pending[nbUrl(6)].rej(new Error("fresh-failure"));
  await tick();
  if (!calls.toasts.some(m => m.includes("fresh-failure")))
    throw new Error("fresh error not toasted");
  console.log("ok")
}
main().catch(e => { console.error(e.message || e); process.exit(1) })
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_render_chat_history_discloses_omitted_messages(self) -> None:
        """v0.2.250: GET /api/notebooks/{id} embeds at most NB_MESSAGES_LIMIT
        messages and reports messages_omitted. renderChatHistory must prepend
        the disclosure line when the flag is set — capping the payload without
        an indicator would silently hide user history. Executes the real
        function under node."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        try:
            block = _js_block(src, "function renderChatHistory")
        except ValueError:
            self.fail("no renderChatHistory function in index.html")
        harness = (
            """\
let cur = {messages: [{role:"user", body:"q", report:null},
                      {role:"assistant", body:"a", report:{}}],
           messages_omitted: 8};
const calls = {prepended: [], added: []};
const chat = {cleared: 0,
  replaceChildren(){ this.cleared++ },
  prepend(x){ calls.prepended.push(x) },
  append(){}, scrollTop: 0, scrollHeight: 0};
const degBadge = {hidden: false}, chatEmpty = {hidden: true}, clearChat = {hidden: false};
const map = {"#degBadge": degBadge, "#chat": chat,
             "#chatEmpty": chatEmpty, "#clearChat": clearChat};
const $ = s => map[s];
function el(tag, cls, txt){ return {tag, cls, text: txt} }
function t(k){ return k + "={n}" }
function addMsg(role, body, report){ calls.added.push(role + ":" + body) }
"""
            + block
            + """
renderChatHistory();
if (calls.prepended.length !== 1 || !String(calls.prepended[0].text).includes("8"))
  { console.error("omitted-count line missing: " + JSON.stringify(calls.prepended));
    process.exit(1) }
if (calls.added.length !== 2)
  { console.error("messages not rendered: " + calls.added.length); process.exit(1) }
cur.messages_omitted = 0;
renderChatHistory();
if (calls.prepended.length !== 1)
  { console.error("disclosure shown with zero omitted"); process.exit(1) }
cur.messages_omitted = undefined;  // payloads without the key must render clean
renderChatHistory();
if (calls.prepended.length !== 1)
  { console.error("disclosure shown when key absent"); process.exit(1) }
console.log("ok")
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_render_notes_discloses_omitted_notes(self) -> None:
        """v0.2.409: GET /api/notebooks/{id} embeds at most NB_NOTES_LIMIT notes
        and reports notes_omitted. renderNotes must append the disclosure line
        when the flag is set — capping the payload without an indicator would
        silently hide user notes. Executes the real function under node."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        try:
            block = _js_block(src, "function renderNotes")
        except ValueError:
            self.fail("no renderNotes function in index.html")
        harness = (
            """\
let cur = {id: 1, notes: [{id: 5, title: "n5", body: "b5"},
                         {id: 6, title: "n6", body: "b6"}],
           notes_omitted: 8};
const noteList = {cleared: 0, kids: [],
  replaceChildren(){ this.cleared++; this.kids = [] },
  append(x){ this.kids.push(x) }};
const map = {"#noteList": noteList};
const $ = s => map[s];
function el(tag, cls, txt){ return {tag, cls, text: txt, kids: [],
  append(...xs){ this.kids.push(...xs) }, setAttribute(){}, onclick: null} }
function t(k){ return k + "={n}" }
function api(){ return Promise.reject(new Error("no net")) }
function openNotebook(){}
function toast(){}
"""
            + block
            + """
renderNotes();
if (noteList.kids.length !== 3)
  { console.error("disclosure + 2 notes expected, got: " + noteList.kids.length); process.exit(1) }
if (!String(noteList.kids[0].text).includes("8"))
  { console.error("omitted-count line missing: " + JSON.stringify(noteList.kids[0]));
    process.exit(1) }
cur.notes_omitted = 0;
renderNotes();
if (noteList.kids.length !== 2)
  { console.error("disclosure shown with zero omitted"); process.exit(1) }
cur.notes_omitted = undefined;  // payloads without the key must render clean
renderNotes();
if (noteList.kids.length !== 2)
  { console.error("disclosure shown when key absent"); process.exit(1) }
cur.notes = [];
renderNotes();
if (noteList.kids.length !== 1 || !noteList.kids[0].kids.length)
  { console.error("empty state not rendered"); process.exit(1) }
console.log("ok")
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_render_studio_saves_output_as_note(self) -> None:
        """v0.2.412: REQ-103 claims studio outputs can be saved as notes, but
        the only path was manual copy-paste into the note form — the spec
        capability existed in name only. renderStudio must give every output
        card a save button that POSTs {title, body} to /api/notebooks/{id}/notes
        with the raw body (not the seal-rendered DOM). Executes the real
        function under node."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        try:
            block = _js_block(src, "function renderStudio")
        except ValueError:
            self.fail("no renderStudio function in index.html")
        harness = (
            """\
let cur = {id: 7, studio: [{kind: "briefing", body: "raw md body", report: null}],
           notes_omitted: 0};
const studioOut = {cleared: 0, kids: [],
  replaceChildren(){ this.cleared++; this.kids = [] },
  append(x){ this.kids.push(x) }};
const map = {"#studioOut": studioOut};
const $ = s => map[s];
function el(tag, cls, txt){ return {tag, cls, text: txt, kids: [],
  append(...xs){ this.kids.push(...xs) }, setAttribute(){}, onclick: null, disabled: false} }
function t(k){ return k }
function renderWithSeals(){}
function reportBadges(){}
const posts = [];
async function jpost(path, body){ posts.push({path, body}); }
function toast(){}
let reopened = 0;
function openNotebook(){ reopened++ }
"""
            + block
            + """
renderStudio();
if (studioOut.kids.length !== 1)
  { console.error("card count: " + studioOut.kids.length); process.exit(1) }
const card = studioOut.kids[0];
const btn = card.kids.find(k => k.tag === "button" && k.text === "studio.savenote");
if (!btn) {
  console.error("save-as-note button missing: "
    + JSON.stringify(card.kids.map(k=>k.tag+":"+k.text)));
  process.exit(1) }
btn.onclick();  // async — awaits jpost; wait a tick
await new Promise(r => setTimeout(r, 10));
if (posts.length !== 1) { console.error("no note POST: " + posts.length); process.exit(1) }
if (posts[0].path !== "/api/notebooks/7/notes")
  { console.error("wrong path: " + posts[0].path); process.exit(1) }
if (posts[0].body.title !== "studio.briefing" || posts[0].body.body !== "raw md body")
  { console.error("wrong payload: " + JSON.stringify(posts[0].body)); process.exit(1) }
if (reopened !== 1) { console.error("notebook not reopened: " + reopened); process.exit(1) }
console.log("ok")
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_health_failure_reflects_offline_and_recovers(self) -> None:
        """v0.2.264 defect class: a failed /api/health fetch flipped window._llmOn
        to false but left the lamp green and the banner hidden — the UI claimed
        "LLM on" while internal state said off. Executes the real health() under
        node: api() rejects, then succeeds with llm:true — asserts the lamp and
        banner track both directions and the off→on transition re-fetches
        question chips."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        fn = _js_block(src, "async function health")
        harness = """\
const reg = {};
function mkEl(){
  const cls = new Set();
  return {textContent:"", title:"", style:{},
    classList:{toggle:(c,on)=>{on?cls.add(c):cls.delete(c)},
               remove:c=>cls.delete(c), add:c=>cls.add(c), contains:c=>cls.has(c)}};
}
function $(sel){ if (!reg[sel]) reg[sel] = mkEl(); return reg[sel]; }
function t(k){ return k }
let refetches = 0;
function refreshQuestions(){ refetches++ }
const window = {};
let apiImpl = async () => ({ json: async () => ({ llm: true, model: "m", embed_model: "e" }) });
async function api(path){ return apiImpl(path); }
""" + fn + """
await health();                                    // initial on (fires first refetch)
if (!reg["#lamp"].classList.contains("on")) { console.error("lamp not on"); process.exit(1) }
if (refetches !== 1) {
  console.error("first off->on did not refetch: " + refetches);
  process.exit(1) }
apiImpl = async () => { throw new Error("net down") };
await health();                                    // fetch failure
if (window._llmOn !== false) { console.error("_llmOn not false after failure"); process.exit(1) }
if (reg["#lamp"].classList.contains("on")) {
  console.error("lamp stayed green on failure"); process.exit(1) }
if (reg["#banner"].style.display !== "block") {
  console.error("banner hidden on failure"); process.exit(1) }
if (refetches !== 1) { console.error("failure refetched questions"); process.exit(1) }
apiImpl = async () => ({ json: async () => ({ llm: true }) });
await health();                                    // off->on recovery
if (!window._llmOn) { console.error("_llmOn not restored"); process.exit(1) }
if (reg["#banner"].style.display !== "none") {
  console.error("banner still shown after recovery"); process.exit(1) }
if (refetches !== 2) {
  console.error("questions not refetched on off->on: " + refetches);
  process.exit(1) }
console.log("ok")
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_refreshQuestions_chips_guards_and_race(self) -> None:
        """v0.2.265: pin refreshQuestions' three contract surfaces under node —
        chips render as buttons that fill #askInput on click, the guard skips
        fetching entirely when there is no notebook/sources/LLM, and chips for
        a stale (switched-away) notebook id are dropped rather than shown."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        fn = _js_block(src, "async function refreshQuestions")
        harness = """\
const reg = {};
function mkEl(){ return {children:[], value:"", focused:false,
  replaceChildren(){ this.children=[] }, append(x){ this.children.push(x) },
  focus(){ this.focused = true }, onclick:null, type:"", cls:"", tag:"", text:""}; }
function $(sel){ if (!reg[sel]) reg[sel] = mkEl(); return reg[sel]; }
function el(tag, cls, text){ const e = mkEl(); e.tag=tag; e.cls=cls; e.text=text; return e }
function t(k){ return k }
const window = { _llmOn: true };
let calls = [];
let apiImpl = async (path) => { calls.push(path);
  return { json: async () => ({ questions: ["質問Aですか?", "質問Bですか?"] }) }; };
async function api(path){ return apiImpl(path); }
let cur = { id: 1, sources: [{id: 5}] };
""" + fn + """
await refreshQuestions();
if (calls.length !== 1) { console.error("no fetch for live nb: " + calls); process.exit(1) }
if (reg["#qs"].children.length !== 2) {
  console.error("chips not rendered: " + reg["#qs"].children.length);
  process.exit(1) }
const chip = reg["#qs"].children[0];
if (chip.tag !== "button" || chip.type !== "button" || chip.cls !== "q-chip")
  { console.error("chip shape wrong: "
      + JSON.stringify({t:chip.tag, ty:chip.type, c:chip.cls}));
    process.exit(1) }
chip.onclick();
if (reg["#askInput"].value !== "質問Aですか?" || !reg["#askInput"].focused)
  { console.error("chip click did not fill+focus input"); process.exit(1) }

// Guards: no sources / llm off / no notebook -> cleared, no fetch.
cur = { id: 1, sources: [] };
await refreshQuestions();
window._llmOn = false; cur = { id: 1, sources: [{id: 5}] };
await refreshQuestions();
cur = null;
await refreshQuestions();
if (calls.length !== 1 || reg["#qs"].children.length !== 0)
  { console.error("guards fetched or kept chips: calls=" + calls.length
      + " chips=" + reg["#qs"].children.length); process.exit(1) }

// Race: notebook switches while the fetch is in flight -> chips dropped.
window._llmOn = true; cur = { id: 9, sources: [{id: 5}] };
apiImpl = async () => ({ json: async () => { cur = { id: 10, sources: [{id:5}] };
  return { questions: ["staleですか?"] }; } });
await refreshQuestions();
if (reg["#qs"].children.length !== 0)
  { console.error("stale-notebook chips rendered"); process.exit(1) }
console.log("ok")
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_renderNotebook_preserves_in_progress_rename(self) -> None:
        """v0.2.266: pin renderNotebook's pendingRename machinery under node —
        an in-progress rename input must be detached (onblur/onkeydown nulled
        so the DOM teardown can't fire a phantom commit) and restored after the
        rebuild via BOTH detection paths: document.activeElement (unrelated
        rebuilds) and the externalPendingRename stash (sibling-button clicks)."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        fn = _js_block(src, "function renderNotebook")
        scope_fns = _js_block(src, "function scopeSelection") + _js_block(
            src, "function updateScopeInfo"
        )
        harness = """\
const reg = {};
function mkEl(){ return {children:[], parent:null, value:"", hidden:false,
  disabled:false, textContent:"", href:"", title:"",
  classList:{ contains(c){ return false }, add(){}, remove(){} },
  dataset:{},
  replaceChildren(){ this.children=[] },
  append(x){ this.children.push(x); x.parent=this },
  contains(x){ while(x){ if(x===this) return true; x=x.parent } return false },
  removeAttribute(n){ if(n==="href") delete this.href },
  setAttribute(n,v){}, querySelector(){ return null },
  setSelectionRange(){}, focus(){}, onclick:null, ondblclick:null, onkeydown:null,
  onblur:null, onchange:null, tabIndex:0}; }
function $(sel){ if (!reg[sel]) reg[sel] = mkEl(); return reg[sel]; }
function el(tag, cls, text){ const e = mkEl(); e.tag=tag; e.cls=cls; e.text=text; return e }
function t(k){ return k }
function fmt(x){ return String(x) }
function showSource(){}
function toast(){}
function renderChatHistory(){}
function renderStudio(){}
function renderNotes(){}
function refreshQuestions(){}
let apiCalls = [];
async function api(path, o){ apiCalls.push(path); return {json:async()=>({})}; }
function openNotebook(){}
let notebooks = [], cur = null, srcIndex = new Map();
const srcSel = new Set(), knownIds = new Set(); let selNb = -1;
let externalPendingRename = null;
let renameCalls = [], selCalls = [];
function startSourceRename(s, tt, row, initial){
  renameCalls.push({srcId: s.id, initial});
  const inp = mkEl(); inp.cls = "src-rename"; return inp;
}
const document = { activeElement: null };
""" + fn + scope_fns + """
// Path 1: focused rename input inside #srcList survives the rebuild.
cur = { id:1, name:"nb", sources:[{id:5,title:"old",kind:"txt"}],
    messages:[], studio:[], notes:[] };
const rin = mkEl();
rin.classList = { contains: c => c === "src-rename" };
rin.dataset = { srcId: "5" };
rin.value = "mid-edit"; rin.selectionStart = 2; rin.selectionEnd = 4;
rin.onblur = ()=>{}; rin.onkeydown = ()=>{};
$("#srcList").children = [rin]; rin.parent = $("#srcList");
document.activeElement = rin;
renderNotebook();
if (renameCalls.length !== 1 || renameCalls[0].srcId !== 5 || renameCalls[0].initial !== "mid-edit")
  { console.error("activeElement path did not restore: "
      + JSON.stringify(renameCalls)); process.exit(1) }
if (rin.onblur !== null || rin.onkeydown !== null)
  { console.error("old rename handlers not detached — phantom commit risk"); process.exit(1) }
if (srcIndex.get(5).s !== 1) { console.error("srcIndex not repopulated"); process.exit(1) }

// Path 2: externalPendingRename stash (sibling refresh-button click) restored once.
renameCalls = [];
externalPendingRename = { srcId: 5, value: "stashed-v", selStart: 0, selEnd: 1 };
document.activeElement = null;
renderNotebook();
if (renameCalls.length !== 1 || renameCalls[0].initial !== "stashed-v")
  { console.error("stash path did not restore"); process.exit(1) }
if (externalPendingRename !== null)
  { console.error("stash not consumed"); process.exit(1) }
renameCalls = [];
renderNotebook();
if (renameCalls.length !== 0)
  { console.error("stash restored twice"); process.exit(1) }

// Pending rename for a deleted source id is dropped, not applied.
externalPendingRename = { srcId: 99, value: "ghost", selStart: 0, selEnd: 0 };
renderNotebook();
if (renameCalls.length !== 0)
  { console.error("ghost rename applied"); process.exit(1) }
console.log("ok")
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_source_refresh_button_posts_stashes_and_restores(self) -> None:
        """v0.2.326: the refresh button's onclick is the producer half of the
        externalPendingRename contract (the consumer half is pinned by
        renderNotebook): it must stash an in-progress rename with selection,
        detach that input's handlers, disable+relabel itself during flight,
        POST to /api/sources/{id}/refresh, toast pages_failed when nonzero,
        reload the notebook, and restore itself on error."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        fn = _js_block(src, "function renderNotebook")
        scope_fns = _js_block(src, "function scopeSelection") + _js_block(
            src, "function updateScopeInfo"
        )
        fn_embed = _js_block(src, "function embedNote")
        harness = """\
const reg = {};
function mkEl(){ return {children:[], parent:null, value:"", hidden:false,
  disabled:false, textContent:"", href:"", title:"",
  classList:{ contains(c){ return false }, add(){}, remove(){} },
  dataset:{},
  replaceChildren(){ this.children=[] },
  append(x){ this.children.push(x); x.parent=this },
  contains(x){ while(x){ if(x===this) return true; x=x.parent } return false },
  removeAttribute(n){ if(n==="href") delete this.href },
  setAttribute(n,v){}, querySelector(){ return null },
  setSelectionRange(){}, focus(){}, onclick:null, ondblclick:null, onkeydown:null,
  onblur:null, onchange:null, tabIndex:0, selectionStart:0, selectionEnd:0}; }
function $(sel){ if (!reg[sel]) reg[sel] = mkEl(); return reg[sel]; }
function el(tag, cls, text){ const e = mkEl(); e.tag=tag; e.cls=cls;
  e.text=text; e.textContent=text; return e }
function t(k){ return k === "src.pages_failed" ? "{n} pages failed" : k }
let window = {};
let apiCalls = []; let nextJson = {pages_failed: 0}; let failNext = false;
async function api(path, o){ apiCalls.push({path, method: o && o.method});
  if (failNext) throw new Error("refresh boom");
  return {json:async()=>nextJson}; }
let opens = [], toasts = [];
function openNotebook(id){ opens.push(id) }
function toast(m){ toasts.push(m) }
function showSource(){}
function renderChatHistory(){} function renderStudio(){}
function renderNotes(){} function refreshQuestions(){}
let notebooks = [], cur = null, srcIndex = new Map();
const srcSel = new Set(), knownIds = new Set(); let selNb = -1;
let externalPendingRename = null;
let renameCalls = [];
function startSourceRename(s, tt, row, initial){
  renameCalls.push({srcId: s.id, initial});
  const inp = mkEl(); inp.cls = "src-rename"; return inp;
}
const document = { activeElement: null };
""" + fn_embed + fn + scope_fns + """
(async () => {
cur = { id:3, name:"nb", sources:[{id:9,title:"t",kind:"url",origin:"https://x",refreshable:true}],
  messages:[], studio:[], notes:[] };
renderNotebook();
const row = $("#srcList").children[0];
const ref = row.children.find(c => c.cls === "src-act" && c.textContent === "↻");
const tt = row.children.find(c => c.cls === "t");
if (!ref || !tt) { console.error("refresh button/row not built"); process.exit(1) }
// In-progress rename in the same row -> stash + handler detach.
const rin = mkEl(); rin.value = "edited"; rin.selectionStart = 1; rin.selectionEnd = 3;
rin.onblur = () => {}; rin.onkeydown = () => {};
tt.querySelector = sel => sel === "input.src-rename" ? rin : null;
await ref.onclick({stopPropagation(){}});
if (!externalPendingRename || externalPendingRename.srcId !== 9
    || externalPendingRename.value !== "edited"
    || externalPendingRename.selStart !== 1 || externalPendingRename.selEnd !== 3)
  { console.error("rename not stashed: " + JSON.stringify(externalPendingRename)); process.exit(1) }
if (rin.onblur !== null || rin.onkeydown !== null)
  { console.error("stashed input handlers not detached"); process.exit(1) }
if (apiCalls.length !== 1 || apiCalls[0].path !== "/api/sources/9/refresh"
    || apiCalls[0].method !== "POST")
  { console.error("refresh POST wrong: " + JSON.stringify(apiCalls)); process.exit(1) }
if (opens[0] !== 3) { console.error("no reload after refresh"); process.exit(1) }
if (toasts.length !== 1 || toasts[0].indexOf("src.refresh.ok") !== 0)
  { console.error("refresh toast wrong: " + JSON.stringify(toasts)); process.exit(1) }
externalPendingRename = null;
// pages_failed surfaces in the toast via src.pages_failed {n} substitution.
nextJson = {pages_failed: 2};
await ref.onclick({stopPropagation(){}});
if (toasts[1] !== "src.refresh.ok 2 pages failed")
  { console.error("pages_failed toast wrong: " + toasts[1]); process.exit(1) }
// Error path: toast the message and restore the button (no reload).
failNext = true;
const opensBefore = opens.length;
await ref.onclick({stopPropagation(){}});
if (toasts[2] !== "refresh boom" || ref.disabled || ref.textContent !== "↻"
    || opens.length !== opensBefore)
  { console.error("error path wrong: " + JSON.stringify(toasts)); process.exit(1) }
console.log("ok")
})();
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_embed_skip_surfaced_in_ingest_toasts(self) -> None:
        """v0.2.390: when embeddings are configured (window._embedOn from
        /api/health) but an ingest embeds fewer chunks than it produced —
        endpoint failure, stored-model mismatch, or a partial batch — the
        toast must say so instead of presenting the index as complete (the
        same defect class pages_failed covers). Silent when embeddings are
        off, where 0 embedded is the first-class mode."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        fn = _js_block(src, "function embedNote")
        harness = """\
function t(k){ return k === "src.embed_short" ? "{n}/{total} embedded" : k }
let window = {};
""" + fn + """
const cases = [
  [{_embedOn:true},  {n_embedded:0, n_chunks:5}, " 0/5 embedded"],
  [{_embedOn:true},  {n_embedded:2, n_chunks:5}, " 2/5 embedded"],
  [{_embedOn:true},  {n_embedded:5, n_chunks:5}, ""],
  [{_embedOn:true},  {n_embedded:0, n_chunks:0}, ""],
  [{_embedOn:true},  {}, ""],
  [{_embedOn:false}, {n_embedded:0, n_chunks:5}, ""],
];
for (const [w, j, want] of cases) {
  window._embedOn = w._embedOn;
  const got = embedNote(j);
  if (got !== want)
    { console.error("embedNote wrong: " + JSON.stringify({w,j,got,want})); process.exit(1) }
}
console.log("ok")
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)
        # Wire check: health() tracks _embedOn both ways, all three ingest
        # toasts (upload, URL add, refresh) append embedNote(j), and every
        # toast line that announces a completed ingest carries the suffix —
        # a future ingest path that forgets embedNote fails here even when
        # the call count is unchanged.
        self.assertIn("window._embedOn = !!j.embed_model", src)
        self.assertIn("window._embedOn=false", src)
        self.assertEqual(src.count("embedNote(j)"), 4)  # 1 definition + 3 call sites
        toast_lines = [
            line for line in src.splitlines()
            if "toast(" in line and ('t("sources.added")' in line or 't("src.refresh.ok")' in line)
        ]
        self.assertEqual(len(toast_lines), 3)
        for line in toast_lines:
            self.assertIn("embedNote(j)", line, f"ingest toast without embedNote: {line.strip()}")

    def test_source_row_delete_and_rename_guard(self) -> None:
        """v0.2.329: the source row's remaining three unpinned wirings —
        (a) the × delete button disables itself, DELETEs /api/sources/{id},
        reloads the notebook, and restores itself + toasts on failure;
        (b) row click/Enter/Space opens the source viewer EXCEPT while an
        in-progress rename input lives inside `tt` (the guard that keeps a
        stray click from tearing down the edit); (c) tt.ondblclick starts
        the rename."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        fn = _js_block(src, "function renderNotebook")
        scope_fns = _js_block(src, "function scopeSelection") + _js_block(
            src, "function updateScopeInfo"
        )
        harness = """\
const reg = {};
function mkEl(){ return {children:[], parent:null, value:"", hidden:false,
  disabled:false, textContent:"", href:"", title:"",
  classList:{ contains(c){ return false }, add(){}, remove(){} },
  dataset:{},
  replaceChildren(){ this.children=[] },
  append(x){ this.children.push(x); x.parent=this },
  contains(x){ while(x){ if(x===this) return true; x=x.parent } return false },
  removeAttribute(n){ if(n==="href") delete this.href },
  setAttribute(n,v){}, querySelector(){ return null },
  setSelectionRange(){}, focus(){}, onclick:null, ondblclick:null, onkeydown:null,
  onblur:null, onchange:null, tabIndex:0, selectionStart:0, selectionEnd:0}; }
function $(sel){ if (!reg[sel]) reg[sel] = mkEl(); return reg[sel]; }
function el(tag, cls, text){ const e = mkEl(); e.tag=tag; e.cls=cls;
  e.text=text; e.textContent=text; return e }
function t(k){ return k }
let apiCalls = []; let failNext = false;
async function api(path, o){ apiCalls.push({path, method: o && o.method});
  if (failNext) throw new Error("del boom");
  return {json:async()=>({})}; }
let opens = [], toasts = [], shown = [];
function openNotebook(id){ opens.push(id) }
function toast(m){ toasts.push(m) }
function showSource(id, title){ shown.push(id) }
function renderChatHistory(){} function renderStudio(){}
function renderNotes(){} function refreshQuestions(){}
let notebooks = [], cur = null, srcIndex = new Map();
const srcSel = new Set(), knownIds = new Set(); let selNb = -1;
let externalPendingRename = null;
let renameCalls = [];
function startSourceRename(s, tt, row, initial){
  renameCalls.push({srcId: s.id, initial});
  const inp = mkEl(); inp.cls = "src-rename"; return inp;
}
const document = { activeElement: null };
""" + fn + scope_fns + """
(async () => {
// Non-URL source: no refresh button — row children are [no][tt][del].
cur = { id:3, name:"nb", sources:[{id:9,title:"t",kind:"md"}],
  messages:[], studio:[], notes:[] };
renderNotebook();
const row = $("#srcList").children[0];
const tt = row.children.find(c => c.cls === "t");
const del = row.children.find(c => c.cls === "src-act" && c.textContent === "×");
if (!tt || !del) { console.error("row not built"); process.exit(1) }
// (b) click + Enter/Space open the viewer; unrelated keys do not.
row.onclick();
row.onkeydown({key:"Enter", target:row, preventDefault(){}});
row.onkeydown({key:" ", target:row, preventDefault(){}});
row.onkeydown({key:"x", target:row, preventDefault(){}});
if (shown.length !== 3 || shown.some(x => x !== 9))
  { console.error("row open wiring wrong: " + JSON.stringify(shown)); process.exit(1) }
// v0.2.554: keydown bubbling from a child control (the × button here, or the
// in-place rename input below) must not fire showSource nor preventDefault —
// Enter there activates the child's own action, not the row's.
let rowPd = false;
row.onkeydown({key:"Enter", target:del, preventDefault(){ rowPd = true }});
row.onkeydown({key:"Enter", target:mkEl(), preventDefault(){ rowPd = true }});
if (shown.length !== 3 || rowPd)
  { console.error("bubbled child keydown fired the row action"); process.exit(1) }
// (b-guard) while a rename input lives in tt, clicks must not open the viewer.
const rin = mkEl();
tt.querySelector = sel => sel === "input.src-rename" ? rin : null;
row.onclick();
if (shown.length !== 3)
  { console.error("rename-in-progress guard broken"); process.exit(1) }
// (c) double-clicking the title span starts the rename.
let stopped = false;
tt.ondblclick({stopPropagation(){ stopped = true }});
if (renameCalls.length !== 1 || renameCalls[0].srcId !== 9 || !stopped)
  { console.error("dblclick rename wiring wrong"); process.exit(1) }
// (a) delete: disable → DELETE → reload; on failure restore + toast, no reload.
await del.onclick({stopPropagation(){}});
if (!del.disabled)
  { console.error("del was not disabled in flight"); process.exit(1) }
if (apiCalls.length !== 1 || apiCalls[0].path !== "/api/sources/9"
    || apiCalls[0].method !== "DELETE")
  { console.error("delete call wrong: " + JSON.stringify(apiCalls)); process.exit(1) }
if (opens[0] !== 3) { console.error("no reload after delete"); process.exit(1) }
failNext = true;
const opensBefore = opens.length;
await del.onclick({stopPropagation(){}});
if (toasts[0] !== "del boom" || del.disabled || opens.length !== opensBefore)
  { console.error("delete error path wrong: " + JSON.stringify(toasts)); process.exit(1) }
console.log("ok")
})();
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_showsource_lazy_details_loads_once(self) -> None:
        """v0.2.326: the excerpt path's <details> toggle lazy-loads full text
        exactly once (dataset.loaded guard), renders into its body on success,
        writes the error into the body on failure, and honors the stale-signal
        check — the remaining unpinned branch of showSource."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        try:
            show = _js_block(src, "async function showSource")
        except ValueError as e:
            self.fail(f"showSource not found: {e}")
        harness = (
            """\
const calls = {renders: [], toasts: []};
const mk = () => {
  const n = {textContent: "", children: [], kids: [], dataset: {}, style: {},
    disabled: false, open: false, _cbs: {}, className: "", tag: "",
    replaceChildren(){ n.children = []; n.kids = []; },
    append(...xs){ n.children.push(...xs); n.kids.push(...xs); },
    prepend(x){ n.children.unshift(x); n.kids.unshift(x); },
    classList: {add(){}, remove(){}, contains: () => false},
    focus(){}, addEventListener(ev, cb){ n._cbs[ev] = cb; },
    querySelector(){ return null }, querySelectorAll(){ return [] },
    setAttribute(){}, scrollIntoView(){},
  };
  return n;
};
const els = {};
const $ = s => els[s] || (els[s] = mk());
const document = {activeElement: null,
  createElement: tag => { const n = mk(); n.tag = tag; return n; }};
const el = (tag, cls, txt) => { const n = mk(); n.tag = tag; n.className = cls;
  n.textContent = txt || ""; return n; };
const t = k => k;
let _srcAbort = null, _viewerOpener = null;
const deferred = [];
const api = (p, opts) => { const rec = {path: p, sig: opts && opts.signal};
  deferred.push(rec);
  return new Promise((res, rej) => { rec.res = res; rec.rej = rej; }); };
const renderFullSource = (c, chunks) => calls.renders.push(chunks);
const toast = m => calls.toasts.push(m);
const closeViewer = () => {};
"""
            + show
            + """
(async () => {
  // Excerpt path: builds a lazy <details>; the toggle fires the fetch once.
  showSource(9, "T", "excerpt text", "sec1", [1], null);
  const vt = els["#viewerText"];
  const det = vt.kids.find(c => c.className === "full-src");
  if (!det || !det._cbs.toggle)
    { console.error("lazy details not wired"); process.exit(1) }
  det.open = true;
  det._cbs.toggle();
  if (deferred.length !== 1 || deferred[0].path !== "/api/sources/9/text")
    { console.error("lazy fetch wrong"); process.exit(1) }
  deferred[0].res({json: async () => ({chunks: [{id: 1, seq: 0, text: "c"}]})});
  await new Promise(r => setTimeout(r, 0));
  if (calls.renders.length !== 1)
    { console.error("lazy render did not happen"); process.exit(1) }
  // Second toggle is a no-op (dataset.loaded).
  det._cbs.toggle();
  await new Promise(r => setTimeout(r, 0));
  if (deferred.length !== 1)
    { console.error("lazy fetch fired twice"); process.exit(1) }
  // Failure writes into the body instead of toasting (viewer-local error).
  showSource(10, "T2", "excerpt2", null, null, null);
  const det2 = els["#viewerText"].kids.find(c => c.className === "full-src");
  det2.open = true;
  det2._cbs.toggle();
  deferred[1].rej(new Error("fetch boom"));
  await new Promise(r => setTimeout(r, 0));
  const body2 = det2.kids[det2.kids.length - 1];
  if (body2.textContent !== "fetch boom" || calls.toasts.length !== 0)
    { console.error("lazy error path wrong"); process.exit(1) }
  // Stale signal: det3's fetch is in flight when showSource(12) aborts the
  // source-11 controller — its late resolution must not render.
  showSource(11, "T3", "e3", null, null, null);
  const det3 = els["#viewerText"].kids.find(c => c.className === "full-src");
  det3.open = true;
  det3._cbs.toggle();
  const rendersBefore = calls.renders.length;
  showSource(12, "T4", "e4", null, null, null);   // aborts deferred[2]'s signal
  deferred[2].res({json: async () => ({chunks: []})});
  await new Promise(r => setTimeout(r, 0));
  if (calls.renders.length !== rendersBefore)
    { console.error("stale lazy fetch rendered"); process.exit(1) }
  console.log("ok");
})();
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_lazy_details_retries_after_failure(self) -> None:
        """v0.2.489: a failed lazy full-source fetch must clear
        `dataset.loaded` so the collapse→reopen gesture retries — before
        this fix the flag stayed set and the error text was pinned on
        forever within that viewer session. The retry must also reuse the
        SAME body element (a second placeholder must never appear)."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        show = _js_block(src, "async function showSource")
        harness = (
            """\
const calls = {renders: []};
const mk = () => {
  const n = {textContent: "", children: [], kids: [], dataset: {}, style: {},
    disabled: false, open: false, _cbs: {}, className: "", tag: "",
    replaceChildren(){ n.children = []; n.kids = []; },
    append(...xs){ n.children.push(...xs); n.kids.push(...xs); },
    prepend(x){ n.children.unshift(x); n.kids.unshift(x); },
    classList: {add(){}, remove(){}, contains: () => false},
    focus(){}, addEventListener(ev, cb){ n._cbs[ev] = cb; },
    querySelector(sel){ return n.kids.find(k => k.className === sel.slice(1)) || null },
    querySelectorAll(){ return [] },
    setAttribute(){}, scrollIntoView(){},
  };
  return n;
};
const els = {};
const $ = s => els[s] || (els[s] = mk());
const document = {activeElement: null,
  createElement: tag => { const n = mk(); n.tag = tag; return n; }};
const el = (tag, cls, txt) => { const n = mk(); n.tag = tag; n.className = cls;
  n.textContent = txt || ""; return n; };
const t = k => k;
let _srcAbort = null, _viewerOpener = null;
const deferred = [];
const api = (p, opts) => { const rec = {path: p, sig: opts && opts.signal};
  deferred.push(rec);
  return new Promise((res, rej) => { rec.res = res; rec.rej = rej; }); };
const renderFullSource = (c, chunks) => calls.renders.push(chunks);
const toast = () => {};
const closeViewer = () => {};
"""
            + show
            + """
(async () => {
  showSource(9, "T", "excerpt text", "sec1", [1], null);
  const det = els["#viewerText"].kids.find(c => c.className === "full-src");
  det.open = true;
  det._cbs.toggle();
  // First fetch fails: the flag MUST clear so reopening retries, the
  // error text lands in the one body element, and no toast is used.
  deferred[0].rej(new Error("fetch boom"));
  await new Promise(r => setTimeout(r, 0));
  if (det.dataset.loaded)
    { console.error("loaded still set after failure — retry impossible"); process.exit(1) }
  const bodies = () => det.kids.filter(k => k.className === "full-body");
  if (bodies().length !== 1 || bodies()[0].textContent !== "fetch boom")
    { console.error("error body wrong"); process.exit(1) }
  // Reopen gesture retries: a second fetch is issued into the SAME body,
  // which flips back to the loading placeholder in flight.
  det._cbs.toggle();
  if (deferred.length !== 2)
    { console.error("reopen did not retry the fetch"); process.exit(1) }
  if (bodies().length !== 1 || bodies()[0].textContent !== "…")
    { console.error("retry must reuse the same body with placeholder"); process.exit(1) }
  deferred[1].res({json: async () => ({chunks: [{id: 1, seq: 0, text: "c"}]})});
  await new Promise(r => setTimeout(r, 0));
  if (calls.renders.length !== 1 || bodies().length !== 1)
    { console.error("successful retry did not render into the one body"); process.exit(1) }
  if (!det.dataset.loaded)
    { console.error("loaded must be set after a successful load"); process.exit(1) }
  console.log("ok");
})();
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_startSourceRename_commit_and_cancel_paths(self) -> None:
        """v0.2.267: pin startSourceRename's five exit paths under node —
        Enter commits via PATCH + reload, blur to a sibling row control skips
        the commit entirely (the sibling's own click rebuilds), Escape cancels
        without a PATCH, empty/unchanged input reloads without a PATCH, and a
        second commit() (blur firing after Enter) is a no-op."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        fn = _js_block(src, "function startSourceRename")
        harness = """\
function mkEl(){ return {children:[], value:"", type:"", className:"", dataset:{},
  disabled:false, focused:false,
  replaceChildren(){ this.children=[] },
  append(x){ this.children.push(x); x.parent = this }, parent:null,
  contains(x){ while(x){ if(x===this) return true; x=x.parent } return false },
  setAttribute(){}, focus(){ this.focused = true }, select(){}, blur(){},
  onclick:null, onblur:null, onkeydown:null,
  click(){ if(this.onclick) this.onclick({stopPropagation(){}}) } }; }
function el(tag, cls, text){ const e = mkEl(); e.tag=tag; e.cls=cls; e.text=text; return e }
function t(k){ return k }
function toast(){}
const document = { createElement: () => mkEl() };
let cur = { id: 7 };
let patches = [], reloads = [];
async function api(path, o){ patches.push({path, body: o && o.body}); return {json:async()=>({})}; }
function openNotebook(id){ reloads.push(id) }
""" + fn + """
const s = { id: 5, title: "old", kind: "txt" };
function fresh(){ const tt = mkEl(), row = mkEl(); row.children = [tt]; tt.parent = row;
  return { tt, row, input: startSourceRename(s, tt, row) }; }
const key = k => ({ key: k, preventDefault(){} });

// Enter -> PATCH with trimmed title + reload.
let f = fresh(); f.input.value = "  new name  ";
f.input.onkeydown(key("Enter"));
await new Promise(r => setTimeout(r, 0));
if (patches.length !== 1 || !patches[0].path.endsWith("/sources/5")
    || !patches[0].body.includes("new name"))
  { console.error("Enter commit failed: " + JSON.stringify(patches)); process.exit(1) }
if (reloads.length !== 1 || reloads[0] !== 7) { console.error("no reload"); process.exit(1) }

// Second commit (blur AFTER Enter already committed) -> no second PATCH.
f.input.onblur({ relatedTarget: null });
await new Promise(r => setTimeout(r, 0));
if (patches.length !== 1) { console.error("double commit"); process.exit(1) }

// Blur to a sibling element inside the row -> NO commit, NO reload.
patches = []; reloads = [];
f = fresh(); f.input.value = "changed";
const sibling = mkEl(); f.row.children.push(sibling); sibling.parent = f.row;
f.input.onblur({ relatedTarget: sibling });
await new Promise(r => setTimeout(r, 0));
if (patches.length !== 0 || reloads.length !== 0)
  { console.error("sibling blur committed"); process.exit(1) }

// Blur to outside the row -> commit fires.
patches = []; reloads = [];
f.input.onblur({ relatedTarget: mkEl() });
await new Promise(r => setTimeout(r, 0));
if (patches.length !== 1) { console.error("outside blur did not commit"); process.exit(1) }

// Escape -> no PATCH, blur before reload (resurrection guard), reload happens.
patches = []; reloads = [];
f = fresh(); f.input.value = "discarded";
let blurred = false; f.input.blur = () => { blurred = true };
f.input.onkeydown(key("Escape"));
if (!blurred) { console.error("Escape did not blur before reload"); process.exit(1) }
await new Promise(r => setTimeout(r, 0));
if (patches.length !== 0 || reloads.length !== 1)
  { console.error("Escape committed or skipped reload"); process.exit(1) }

// Unchanged / empty title -> reload only, no PATCH.
patches = []; reloads = [];
f = fresh(); f.input.value = "old";      // unchanged
f.input.onkeydown(key("Enter"));
await new Promise(r => setTimeout(r, 0));
f = fresh(); f.input.value = "   ";      // whitespace-only
f.input.onkeydown(key("Enter"));
await new Promise(r => setTimeout(r, 0));
if (patches.length !== 0 || reloads.length !== 2)
  { console.error("unchanged/empty committed or skipped reload: "
      + patches.length + "/" + reloads.length); process.exit(1) }
console.log("ok")
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_loadNotebooks_list_delete_and_rename_paths(self) -> None:
        """v0.2.268: pin loadNotebooks' contract under node — rows render with
        the current notebook highlighted and a S# count label; click/Enter
        opens; delete-current clears `cur` and auto-opens the first remaining
        notebook; delete requires confirm() and rename requires a non-blank
        prompt() before any request fires."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        fn = _js_block(src, "async function loadNotebooks")
        harness = """\
const reg = {};
function mkEl(){ return {children:[], parent:null, className:"", value:"",
  textContent:"", title:"", disabled:false, tabIndex:0,
  append(...xs){ xs.forEach(x => { this.children.push(x); x.parent=this }) },
  replaceChildren(...xs){ this.children=[];
    xs.forEach(x => { this.children.push(x); x.parent=this }) },
  setAttribute(n,v){ this["attr_"+n]=v },
  onclick:null, onkeydown:null,
  click(){ if(this.onclick) this.onclick({stopPropagation(){}}) },
  press(k){ if(this.onkeydown) this.onkeydown({key:k, target:this, preventDefault(){}}) } }; }
function $(sel){ if (!reg[sel]) reg[sel] = mkEl(); return reg[sel]; }
function el(tag, cls, text){ const e = mkEl(); e.tag=tag; e.cls=cls; e.text=text; return e }
function t(k){ return k }
function toast(){}
function renderNotebook(){}
let notebooks = [], cur = null, srcIndex = new Map();
let calls = [], opens = [];
async function api(path, o){ calls.push({path, o});
  if (path === "/api/notebooks")
    return {json: async()=>({notebooks: listNow})};
  return {json: async()=>({})}; }
function openNotebook(id){ opens.push(id) }
let listNow = [{id:1, name:"A", counts:{sources:2}}, {id:3, name:"B", counts:{sources:0}}];
let promptRet = null, confirmRet = true;
function prompt(msg, def){ prompt.calls.push(def); return promptRet }
prompt.calls = [];
function confirm(msg){ return confirmRet }
""" + fn + """
// Render: two rows, current highlighted, count labels present.
cur = { id: 3, name: "B" };
await loadNotebooks();
const lis = $("#nbList").children;
if (lis.length !== 2) { console.error("row count " + lis.length); process.exit(1) }
if (lis[0].className !== "" || lis[1].className !== "cur")
  { console.error("highlight wrong: " + lis[0].className + "/"
      + lis[1].className); process.exit(1) }
if (lis[0].children[1].text !== "2册")
  { console.error("count label: " + lis[0].children[1].text); process.exit(1) }

// Row click + Enter opens; Space opens too.
lis[0].click(); lis[1].press("Enter"); lis[0].press(" ");
if (JSON.stringify(opens) !== JSON.stringify([1,3,1]))
  { console.error("open wiring: " + opens); process.exit(1) }
// v0.2.554: a keydown bubbling up from a row's OWN BUTTON must not fire the
// row action — the handler's preventDefault() would cancel the button's
// native Enter/Space activation (unreachable control + wrong command run).
let pdSeen = false;
lis[0].onkeydown({key:"Enter", target: lis[0].children[2],
  preventDefault(){ pdSeen = true }});
if (opens.length !== 3 || pdSeen)
  { console.error("bubbled child keydown fired the row action"); process.exit(1) }

// Rename: blank prompt -> no PATCH; valid -> PATCH + cur.name synced + reload.
opens = []; calls = []; promptRet = "   ";
lis[1].children[2].onclick({stopPropagation(){}});
await new Promise(r => setTimeout(r, 0));
if (calls.length !== 0) { console.error("blank rename sent a request"); process.exit(1) }
promptRet = "B2";
lis[1].children[2].onclick({stopPropagation(){}});
await new Promise(r => setTimeout(r, 0));
if (!calls.some(c => c.path === "/api/notebooks/3" && c.o.method === "PATCH"))
  { console.error("rename PATCH missing: " + JSON.stringify(calls)); process.exit(1) }
if (cur.name !== "B2") { console.error("cur.name not synced"); process.exit(1) }

// Delete-current: confirm veto -> nothing; confirm -> DELETE + cur cleared +
// auto-open the first remaining notebook.
calls = []; opens = []; confirmRet = false;
await loadNotebooks();
const delBtn = $("#nbList").children[1].children[3];
delBtn.onclick({stopPropagation(){}});
await new Promise(r => setTimeout(r, 0));
if (calls.some(c => c.o && c.o.method === "DELETE"))
  { console.error("delete fired without confirm"); process.exit(1) }
confirmRet = true; listNow = [{id:1, name:"A", counts:{sources:2}}];
delBtn.onclick({stopPropagation(){}});
await new Promise(r => setTimeout(r, 10));
if (!calls.some(c => c.path === "/api/notebooks/3" && c.o.method === "DELETE"))
  { console.error("DELETE missing"); process.exit(1) }
if (cur !== null) { console.error("cur not cleared on delete"); process.exit(1) }
if (opens[opens.length-1] !== 1)
  { console.error("no auto-open after delete: " + opens); process.exit(1) }

// Empty notebook list -> cur cleared + empty-state card rendered.
listNow = []; cur = { id: 1 };
await loadNotebooks();
if (cur !== null) { console.error("cur kept on empty list"); process.exit(1) }
if ($("#nbList").children[0].cls !== "empty")
  { console.error("no empty state"); process.exit(1) }
console.log("ok")
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_loadNotebooks_toasts_instead_of_rejecting(self) -> None:
        """loadNotebooks() is called fire-and-forget from ~15 sites (post-
        mutation refresh, row clicks, openNotebook's own rebuild, the boot
        call). Until the guard its siblings always had, its fetch+json lived
        outside any try — a malformed body or envelope error produced an
        `unhandledrejection` at every one of those sites: no toast, stale
        list, console-only evidence. The fix follows the file's own
        convention (openNotebook/health/refreshQuestions each self-guard):
        loadNotebooks catches and toasts. Pin both directions under node —
        the promise resolves AND the message reaches toast."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        fn = _js_block(src, "async function loadNotebooks")
        harness = """\
let toasted = null;
const toast = m => { toasted = m; };
const t = k => k;
const api = async () => { throw new Error("[500] down"); };
""" + fn + """
(async () => {
  await loadNotebooks();   // must resolve, not reject
  if (toasted !== "[500] down") {
    console.error("api failure did not toast: " + JSON.stringify(toasted));
    process.exit(1);
  }
  console.log("ok");
})();
"""
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_localStorage_access_is_failure_tolerant(self) -> None:
        """v0.2.484: `localStorage.getItem("shoin.lang")` ran at script top
        level — in a browser where storage is disabled (private-mode
        restrictions, sandboxed iframe, cookies blocked) the SecurityError
        killed the ENTIRE boot: markup renders, every control inert. Accesses
        now funnel through `_lsGet`/`_lsSet`, which degrade to no-ops. Pin
        both directions under node (throwing store vs. real passthrough) and
        lexical containment: `localStorage.` must appear only inside the two
        helper bodies — any new bare call site is the same boot-killer."""
        src = _script_body(_html())
        helpers = []
        for name in ("_lsGet", "_lsSet"):
            m = re.search(rf"const {name}[^\n]*", src)
            if not m:
                self.fail(f"{name} not found in index.html")
            helpers.append(m.group(0))
        # Containment: outside the two helper bodies, `localStorage.` must not
        # appear at all — a bare call site re-opens the boot-killer.
        outside = src
        for h in helpers:
            outside = outside.replace(h, "", 1)
        self.assertEqual(
            outside.count("localStorage."), 0,
            "bare localStorage access outside _lsGet/_lsSet — "
            "an unguarded call at top level kills the whole app boot",
        )
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        harness = (
            "let localStorage = {"
            "  getItem(){ throw new Error('SecurityError') },"
            "  setItem(){ throw new Error('SecurityError') } };\n"
            + "\n".join(helpers)
            + """
if (_lsGet("shoin.lang") !== null)
  { console.error("throwing store not degraded to null"); process.exit(1) }
_lsSet("shoin.lang", "en");  // must not throw
const real = { v: "x", getItem(k){ return this.v }, setItem(k,v){ this.v = v } };
localStorage = real;
if (_lsGet("shoin.lang") !== "x")
  { console.error("passthrough broke"); process.exit(1) }
_lsSet("shoin.lang", "ja");
if (real.v !== "ja")
  { console.error("set passthrough broke"); process.exit(1) }
console.log("ok");
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_collection_reads_are_boundary_normalized(self) -> None:
        """v0.2.486: response-shape reads were defensively INCONSISTENT —
        `cur.sources.forEach` raw on one line, `cur.sources?.length` two
        functions later; `(j.chunks || [])` in one fetch, bare `j.chunks`
        passed to `renderFullSource` in the next. A malformed/truncated
        response then produced a TypeError toast instead of an empty render
        — same input class, different user-visible outcome depending on
        which call site got it. Collections are now normalized at the trust
        boundary (openNotebook's `cur = {...defaults, ...j}`) or defaulted
        at the consumer, so inside render* every collection always exists.
        Pin the boundary spreads lexically and `renderFullSource`'s
        tolerance under node."""
        src = _script_body(_html())
        self.assertIn("notebooks = j.notebooks || [];", src)
        m = re.search(r"cur = \{[^}]*\.\.\.j[^}]*\};", src)
        self.assertIsNotNone(m, "cur boundary normalization missing")
        for field in ("sources", "messages", "notes", "studio"):
            self.assertIn(f"{field}:[],", m.group(0))
        self.assertIn("(chunks || []).forEach", src)
        self.assertIn("(j.questions || []).forEach", src)
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        fn = _js_block(src, "function renderFullSource")
        harness = (
            "let replaced = 0;\n"
            "const container = { replaceChildren(){replaced++}, append(){} };\n"
            + fn
            + """
renderFullSource(container);            // chunks entirely absent
renderFullSource(container, undefined); // explicitly undefined
if (replaced !== 2) throw "replaceChildren count " + replaced;
console.log("ok");
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)
        self.assertIn("ok", out)

    def test_lang_placeholder_appears_exactly_once(self) -> None:
        """server.py's _h_ui() does a blind byte replace of "__SHOIN_LANG__" —
        safe only because the token appears exactly once in the shipped file
        (the <meta> tag it's meant for). A second occurrence anywhere else
        (e.g. in a comment quoting the token, as an earlier draft of this
        feature briefly had) would be silently corrupted by that replace too."""
        html = _html()
        self.assertEqual(
            html.count("__SHOIN_LANG__"),
            1,
            "the placeholder must appear exactly once, or _h_ui()'s replace() "
            "will corrupt every occurrence, not just the intended <meta> tag",
        )
        self.assertIn('<meta name="shoin-lang" content="__SHOIN_LANG__">', html)

    def test_no_html_injection_sinks(self) -> None:
        """v0.2.288+: the UI renders every user/source/LLM string through
        textContent/createElement — zero HTML-injection sinks exists by
        construction today. Pin that property so a future handler can't
        introduce innerHTML/insertAdjacentHTML/document.write/eval and turn a
        notebook title or model answer into a stored-XSS vector."""
        html = _html()
        sinks = [
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "new Function",
        ]
        found = [s for s in sinks if s in html]
        self.assertEqual(found, [], f"HTML injection sink(s) in index.html: {found}")

    def test_create_and_add_handlers_disable_clear_reload_reenable(self) -> None:
        """v0.2.318: the three write entry points — create notebook (nbForm),
        add URL source (urlBtn), file upload (fileInput) — share one contract:
        disable the control during the POST, clear the input on success,
        reload the notebook view, and always re-enable in finally (failure
        included). Runs all three real handlers under node."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        blocks = []
        for marker in ('$("#nbForm").onsubmit', '$("#fileInput").onchange', '$("#urlBtn").onclick'):
            try:
                blocks.append(_js_block(src, marker))
            except ValueError:
                self.fail(f"handler not found: {marker}")
        nb_form, file_input, url_btn = blocks
        fn_embed = _js_block(src, "function embedNote")
        harness = (
            fn_embed
            + """\
const calls = {posts: [], opens: [], toasts: []};
const cur = {id: 9};
const nbName = {value: "new nb"};
const urlInput = {value: "https://example.com/x"};
const urlBtnEl = {disabled: false, textContent: "Add"};
const fileInputEl = {files: [{name: "a.pdf"}], disabled: false, value: "C:\\\\f"};
const $ = s => s === "#nbName" ? nbName : s === "#urlInput" ? urlInput
    : s === "#urlBtn" ? urlBtnEl : {};
let failNext = false;
const jpost = async (p, body) => {
  calls.posts.push(["POST", p, body]);
  if (failNext) throw new Error("[X] boom");
  return {json: async () => p === "/api/notebooks" ? {id: 9} : {pages_failed: 0}};
};
const api = async (p, opts) => {
  calls.posts.push(["UP", p, opts.headers["X-Filename"]]);
  if (failNext) throw new Error("[X] boom");
  return {json: async () => ({pages_failed: 0})};
};
const t = k => k;
let window = {};
async function openNotebook(id){ calls.opens.push(id) }
function toast(m){ calls.toasts.push(m) }
const events = {};
const nbFormEv = {preventDefault(){}, target: {querySelector: () => nbBtn}};
const nbBtn = {disabled: false};
"""
            + f"events.nbForm = async e=>\n{nb_form.split('onsubmit = async e=>',1)[1]}\n"
            + f"events.fileInput = async e=>\n{file_input.split('onchange = async e=>',1)[1]}\n"
            + f"events.urlBtn = async ()=>\n{url_btn.split('onclick = async ()=>',1)[1]}\n"
            + """\
(async () => {
  // nbForm: POST /api/notebooks, name cleared, notebook opened, btn re-enabled
  await events.nbForm(nbFormEv);
  if (calls.posts[0][1] !== "/api/notebooks"
      || calls.posts[0][2].name !== "new nb")
    { console.error("nbForm POST wrong: " + JSON.stringify(calls.posts[0])); process.exit(1) }
  if (nbName.value !== "" || calls.opens[0] !== 9 || nbBtn.disabled)
    { console.error("nbForm cleanup wrong: "
        + JSON.stringify({v: nbName.value, opens: calls.opens,
            d: nbBtn.disabled})); process.exit(1) }
  // urlBtn: POST sources, input cleared, opened, button label restored
  const label = urlBtnEl.textContent;
  await events.urlBtn();
  if (calls.posts[1][1] !== "/api/notebooks/9/sources"
      || calls.posts[1][2].target !== "https://example.com/x")
    { console.error("urlBtn POST wrong: " + JSON.stringify(calls.posts[1])); process.exit(1) }
  if (urlInput.value !== "" || calls.opens[1] !== 9 || urlBtnEl.disabled
      || urlBtnEl.textContent !== label)
    { console.error("urlBtn cleanup wrong"); process.exit(1) }
  // fileInput: upload POST with filename header, picker cleared + re-enabled
  await events.fileInput({target: fileInputEl});
  if (calls.posts[2][1] !== "/api/notebooks/9/upload"
      || calls.posts[2][2] !== "a.pdf")
    { console.error("fileInput POST wrong: " + JSON.stringify(calls.posts[2])); process.exit(1) }
  if (fileInputEl.value !== "" || fileInputEl.disabled)
    { console.error("fileInput cleanup wrong"); process.exit(1) }
  // Failure path: toast fires and controls still re-enable (finally)
  failNext = true;
  urlInput.value = "https://again";
  const before = calls.toasts.length;
  await events.urlBtn();
  if (calls.toasts.length !== before + 1 || urlBtnEl.disabled
      || urlBtnEl.textContent !== label)
    { console.error("error path wrong: " + JSON.stringify(calls.toasts)); process.exit(1) }
  console.log("ok")
})();
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_lang_toggle_rewrites_every_i18n_attribute_class(self) -> None:
        """v0.2.320: the ja/en key sets are pinned statically, but the toggle
        itself was unverified — langBtn must flip lang, persist the choice to
        localStorage, and applyI18n() must rewrite ALL four attribute classes
        ([data-i18n] text, [data-i18n-ph] placeholder, [data-i18n-title] title,
        [data-i18n-aria] aria-label) plus the button label and
        documentElement.lang. Runs the real applyI18n + onclick under node."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        try:
            apply_fn = _js_block(src, "function applyI18n()")
            onclick = _js_block(src, '$("#langBtn").onclick')
        except ValueError as exc:
            self.fail(f"i18n block not found: {exc}")
        harness = (
            """\
const I18N = {ja: {"tabs.chat": "対話", "a11y.lang": "言語", "f.ph": "問い"},
              en: {"tabs.chat": "Chat", "a11y.lang": "Language", "f.ph": "Ask"}};
let lang = "ja";
const stored = [];
const localStorage = {setItem(k, v){ stored.push([k, v]) }};
const _lsGet = k => { try{ return localStorage.getItem(k) }catch(e){ return null } };
const _lsSet = (k,v) => { try{ localStorage.setItem(k,v) }catch(e){} };
const texts = [{dataset: {i18n: "tabs.chat"}, textContent: "?"},
               {dataset: {i18n: "a11y.lang"}, textContent: "?"}];
const phs = [{dataset: {i18nPh: "f.ph"}, placeholder: "?"}];
const titles = [{dataset: {i18nTitle: "tabs.chat"}, title: "?"}];
const arias = [{dataset: {i18nAria: "a11y.lang"}, setAttribute(n, v){
  this[n] = v }}];
const langBtn = {textContent: "?", setAttribute(n, v){ this[n] = v }};
const document = {documentElement: {}, querySelectorAll(sel){
  if (sel === "[data-i18n]") return texts;
  if (sel === "[data-i18n-ph]") return phs;
  if (sel === "[data-i18n-title]") return titles;
  if (sel === "[data-i18n-aria]") return arias;
  return [] }};
const $ = s => s === "#langBtn" ? langBtn : {};
const rebuilt = [];
function buildKindButtons(){ rebuilt.push(1) }
const cur = {id: 1};
const rendered = [];
function renderNotebook(){ rendered.push(1) }
const t = k => (I18N[lang] && I18N[lang][k]) || I18N.ja[k] || k;
"""
            + apply_fn
            + "\n"
            + f"const langOnclick = ()=>\n{onclick.split('onclick = ()=>',1)[1]}\n"
            + """\
applyI18n();
if (texts[0].textContent !== "対話" || phs[0].placeholder !== "問い"
    || titles[0].title !== "対話" || arias[0]["aria-label"] !== "言語"
    || langBtn.textContent !== "EN"
    || document.documentElement.lang !== "ja")
  { console.error("ja applyI18n wrong: " + JSON.stringify(texts)); process.exit(1) }
langOnclick();
if (lang !== "en" || stored.length !== 1
    || stored[0][0] !== "shoin.lang" || stored[0][1] !== "en")
  { console.error("toggle/persist wrong: " + JSON.stringify({lang, stored})); process.exit(1) }
if (texts[0].textContent !== "Chat" || phs[0].placeholder !== "Ask"
    || titles[0].title !== "Chat" || arias[0]["aria-label"] !== "Language"
    || langBtn.textContent !== "日本語" || langBtn["aria-label"] !== "Language"
    || document.documentElement.lang !== "en")
  { console.error("en applyI18n wrong"); process.exit(1) }
if (rebuilt.length !== 1 || rendered.length !== 1)
  { console.error("rebuild hooks wrong: " + JSON.stringify({rebuilt, rendered})); process.exit(1) }
console.log("ok")
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_note_reindex_clear_handlers_disable_and_reenable(self) -> None:
        """v0.2.319: the remaining write handlers — note create (noteForm),
        note delete (renderNotes ×), reindex (reindexBtn), clear-chat
        (clearChat), studio generate (buildKindButtons onclick) — share the
        disable→call→reload→finally-re-enable contract. A dropped re-enable
        leaves a permanently dead control; a wrong path hits a wrong route.
        Runs all five real handlers under node."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        blocks = {}
        for name, marker in (
            ("noteForm", '$("#noteForm").onsubmit'),
            ("renderNotes", "function renderNotes()"),
            ("reindex", '$("#reindexBtn").onclick'),
            ("clearChat", '$("#clearChat").onclick'),
            ("buildKinds", "function buildKindButtons()"),
        ):
            try:
                blocks[name] = _js_block(src, marker)
            except ValueError:
                self.fail(f"handler not found: {marker}")
        harness = (
            """\
const calls = {apis: [], posts: [], opens: [], toasts: []};
const cur = {id: 9, sources: [{id: 1}], notes: [], studio: []};
const noteTitle = {value: "T"}, noteBody = {value: "B"};
const reindexEl = {disabled: false, textContent: "reindex"};
const clearEl = {disabled: false};
const kindsEl = {kids: [], replaceChildren(){ this.kids = [] },
  append(x){ this.kids.push(x) }};
const noteList = {kids: [], replaceChildren(){ this.kids = [] },
  append(x){ this.kids.push(x) }};
const $ = s => s === "#noteTitle" ? noteTitle : s === "#noteBody" ? noteBody
    : s === "#reindexBtn" ? reindexEl : s === "#clearChat" ? clearEl
    : s === "#kinds" ? kindsEl : s === "#noteList" ? noteList : {};
function el(t2, c, txt){ const n = {tag: t2, cls: c, text: txt,
  textContent: txt, children: [], kids: [], append(...xs){
  this.children.push(...xs) }, setAttribute(){}}; return n }
const t = k => k;
let failNext = false;
const jpost = async (p, body) => {
  calls.posts.push([p, body]);
  if (failNext) throw new Error("[X] boom");
  return {json: async () => ({})};
};
const api = async (p, opts) => {
  calls.apis.push([opts && opts.method || "GET", p]);
  if (failNext) throw new Error("[X] boom");
  return {json: async () => ({n_embedded: 3, n_total: 5})};
};
async function openNotebook(id){ calls.opens.push(id) }
function toast(m){ calls.toasts.push(m) }
function renderWithSeals(){}
function reportBadges(){}
const KINDS = ["briefing","study_guide","faq","timeline","mindmap"];
const events = {};
"""
            + "events.noteForm = async e=>\n"
            + f"{blocks['noteForm'].split('onsubmit = async e=>',1)[1]}\n"
            + "events.reindex = async ()=>\n"
            + f"{blocks['reindex'].split('onclick = async ()=>',1)[1]}\n"
            + "events.clearChat = async ()=>\n"
            + f"{blocks['clearChat'].split('onclick = async ()=>',1)[1]}\n"
            + f"{blocks['buildKinds']}\n"
            + f"{blocks['renderNotes']}\n"
            + """\
const noteBtn = {disabled: false};
const noteEv = {preventDefault(){}, target: {querySelector: () => noteBtn}};
(async () => {
  // noteForm: POST title+body, both fields cleared, opened, btn re-enabled
  await events.noteForm(noteEv);
  if (calls.posts[0][0] !== "/api/notebooks/9/notes"
      || calls.posts[0][1].title !== "T" || calls.posts[0][1].body !== "B")
    { console.error("noteForm POST wrong: " + JSON.stringify(calls.posts[0])); process.exit(1) }
  if (noteTitle.value !== "" || noteBody.value !== "" || calls.opens[0] !== 9
      || noteBtn.disabled)
    { console.error("noteForm cleanup wrong"); process.exit(1) }
  // note delete via renderNotes: DELETE /api/notes/{id}, reload, re-enable
  cur.notes = [{id: 5, title: "n", body: "b"}];
  renderNotes();
  const delBtn = noteList.kids[0].children[0];
  await delBtn.onclick();
  const lastApi = calls.apis[calls.apis.length - 1];
  if (lastApi[0] !== "DELETE" || lastApi[1] !== "/api/notes/5")
    { console.error("note delete wrong: " + JSON.stringify(lastApi)); process.exit(1) }
  if (calls.opens[1] !== 9 || delBtn.disabled)
    { console.error("note delete cleanup wrong"); process.exit(1) }
  // reindex: POST path + result toast with substituted counts + re-enable
  await events.reindex();
  if (calls.apis[calls.apis.length - 1][1] !== "/api/notebooks/9/reindex")
    { console.error("reindex path wrong"); process.exit(1) }
  if (reindexEl.disabled || reindexEl.textContent !== "reindex")
    { console.error("reindex cleanup wrong"); process.exit(1) }
  // clearChat: DELETE messages + reload
  await events.clearChat();
  if (calls.apis[calls.apis.length - 1][0] !== "DELETE"
      || calls.apis[calls.apis.length - 1][1] !== "/api/notebooks/9/messages")
    { console.error("clearChat wrong"); process.exit(1) }
  if (calls.opens[calls.opens.length - 1] !== 9 || clearEl.disabled)
    { console.error("clearChat cleanup wrong"); process.exit(1) }
  // buildKindButtons: one button per KIND, onclick POSTs studio + reloads
  buildKindButtons();
  if (kindsEl.kids.length !== 5)
    { console.error("kind buttons count wrong: " + kindsEl.kids.length); process.exit(1) }
  const kb = kindsEl.kids[0];
  await kb.onclick();
  const sp = calls.posts[calls.posts.length - 1];
  if (sp[0] !== "/api/notebooks/9/studio" || sp[1].kind !== "briefing")
    { console.error("studio POST wrong: " + JSON.stringify(sp)); process.exit(1) }
  if (calls.opens[calls.opens.length - 1] !== 9 || kb.disabled
      || kb.textContent !== "studio.briefing")
    { console.error("studio cleanup wrong"); process.exit(1) }
  // Guard: no sources -> no POST; failure -> toast + still re-enabled
  cur.sources = [];
  const postsBefore = calls.posts.length;
  await kb.onclick();
  if (calls.posts.length !== postsBefore)
    { console.error("no-source guard broken"); process.exit(1) }
  cur.sources = [{id: 1}];
  failNext = true;
  const tb = calls.toasts.length;
  await kb.onclick();
  if (calls.toasts.length !== tb + 1 || kb.disabled)
    { console.error("studio error path wrong"); process.exit(1) }
  console.log("ok")
})();
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_showsource_aborts_stale_fetch_and_traps_focus(self) -> None:
        """v0.2.323: the source viewer's two safety behaviors are a modal
        contract, not decoration — the `_srcAbort`/`sig.aborted` pair keeps a
        slow source-N response from painting over the source the user opened
        after it (the same defect class _nbSeq/_sealSeq already pin), and the
        dialog's focus trap wraps Tab/Shift+Tab inside it. Both run the real
        extracted code under node.
        """
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        try:
            show = _js_block(src, "async function showSource")
            trap = _js_block(src, '$("#viewer").addEventListener("keydown"')
        except ValueError as e:
            self.fail(f"block not found: {e}")
        harness = (
            """\
const calls = {renders: [], toasts: [], closed: 0};
const mk = () => {
  const n = {textContent: "", children: [], kids: [], dataset: {}, style: {},
    disabled: false, _focused: false, _open: false, _cbs: {}, _q: () => [],
    replaceChildren(){ n.children = []; n.kids = []; },
    append(...xs){ n.children.push(...xs); n.kids.push(...xs); },
    classList: {add(){ n._open = true }, remove(){ n._open = false },
      contains(c){ return c === "open" ? n._open : false }},
    focus(){ n._focused = true; },
    addEventListener(ev, cb){ n._cbs[ev] = cb; },
    querySelectorAll(s){ return n._q(s); },
    setAttribute(){},
  };
  return n;
};
const els = {};
const $ = s => els[s] || (els[s] = mk());
const document = {activeElement: null, createElement: () => mk()};
const el = (tag, cls, txt) => { const n = mk(); n.tag = tag; n.className = cls;
  n.textContent = txt || ""; return n; };
const t = k => k;
let _srcAbort = null, _viewerOpener = null;
const deferred = [];
const api = (p, opts) => { const rec = {path: p, sig: opts && opts.signal};
  deferred.push(rec); return new Promise(res => { rec.res = res; }); };
const renderFullSource = (c, chunks) => calls.renders.push(chunks);
const toast = m => calls.toasts.push(m);
const closeViewer = () => calls.closed++;
"""
            + show
            + "\n"
            + trap
            + ");\n"
            + """\
(async () => {
  // Abort guard: opening source 2 must abort source 1's in-flight fetch, and
  // 1's late response must never reach the viewer.
  showSource(1, "T1");
  showSource(2, "T2");
  const d1 = deferred[0], d2 = deferred[1];
  if (deferred[0].path !== "/api/sources/1/text")
    { console.error("wrong fetch path: " + deferred[0].path); process.exit(1) }
  if (!d1.sig.aborted || d2.sig.aborted)
    { console.error("abort wiring broken"); process.exit(1) }
  d1.res({json: async () => ({chunks: [{id: 1, seq: 0, text: "stale"}]})});
  await new Promise(r => setTimeout(r, 0));
  if (calls.renders.length !== 0)
    { console.error("stale source rendered into viewer"); process.exit(1) }
  d2.res({json: async () => ({chunks: [{id: 2, seq: 0, text: "live"}]})});
  await new Promise(r => setTimeout(r, 0));
  if (calls.renders.length !== 1)
    { console.error("live source not rendered"); process.exit(1) }
  if (!els["#viewer"]._open)
    { console.error("viewer never opened"); process.exit(1) }

  // Focus trap: Tab on the last focusable wraps to first, Shift+Tab on first
  // wraps to last, Escape closes. Requires the dialog open (set by showSource).
  const viewer = els["#viewer"];
  const f1 = mk(), f2 = mk();
  viewer._q = () => [f1, f2];
  const kd = viewer._cbs.keydown;
  if (!kd) { console.error("viewer keydown not wired"); process.exit(1) }
  let prevented = false;
  document.activeElement = f2;
  kd({key: "Tab", shiftKey: false, preventDefault(){ prevented = true }});
  if (!prevented || !f1._focused)
    { console.error("Tab wrap to first broken"); process.exit(1) }
  prevented = false;
  document.activeElement = f1;
  kd({key: "Tab", shiftKey: true, preventDefault(){ prevented = true }});
  if (!prevented || !f2._focused)
    { console.error("Shift+Tab wrap to last broken"); process.exit(1) }
  prevented = false;
  kd({key: "x", shiftKey: false, preventDefault(){ prevented = true }});
  if (prevented)
    { console.error("non-Tab key stole focus flow"); process.exit(1) }
  const before = calls.closed;
  kd({key: "Escape", shiftKey: false, preventDefault(){}});
  if (calls.closed !== before + 1)
    { console.error("Escape did not close viewer"); process.exit(1) }
  console.log("ok");
})();
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_sse_frame_parser_buffers_and_dispatches(self) -> None:
        """v0.2.317: the SSE frame parser (the \\n\\n splitter + event:/data:
        accumulation inside the askForm read loop) is the one dispatch path no
        existing pin exercises — a regression in buffering would silently drop
        every frame. Feeds bytes split mid-frame through the real while-loop
        body under node and asserts delta accumulation + done dispatch."""
        if not shutil.which("node"):
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(_html())
        try:
            loop = _js_block(src, 'while ((i = buf.indexOf("\\n\\n")) >= 0)')
        except ValueError:
            self.fail("SSE frame-split loop not found in askForm handler")
        harness = (
            """\
let buf = "", acc = "", gotDone = false, failed = false, i;
const doneReports = [], toasts = [];
const degBadge = {hidden: true};
const chatEl = {scrollTop: 0, scrollHeight: 0};
const $ = s => s === "#degBadge" ? degBadge : s === "#chat" ? chatEl : {};
function el(t2, c, txt){ return {children: [], append(x){ this.children.push(x) }} }
function t(k){ return k }
const bd = {textContent: "", parentElement: {kids: [],
  append(x){ this.kids.push(x) }}, replaceChildren(){}};
function renderWithSeals(b, body, report){ doneReports.push({body, report}) }
function reportBadges(c, report){}
function toast(m){ toasts.push(m) }
function pump(text){ buf += text;
"""
            + loop
            + """
}
// Frames arrive split mid-data — the parser must buffer until \\n\\n.
pump('event: delta\\ndata: {"text":"he"}\\n\\nevent: delta\\nda');
pump('ta: {"text":"llo"}\\n\\nevent: done\\nda');
pump('ta: {"report":{"confirmed":1},"degraded":false}\\n\\n');
pump('event: error\\ndata: not-json\\n\\n');   // malformed JSON: skip quietly
pump('event: done\\n\\n');                     // no data: skipped by !data guard
if (acc !== "hello")
  { console.error("delta accumulation broken: " + JSON.stringify(acc)); process.exit(1) }
if (!gotDone || doneReports.length !== 1 || doneReports[0].body !== "hello"
    || doneReports[0].report.confirmed !== 1)
  { console.error("done dispatch broken: " + JSON.stringify(doneReports)
      + " gotDone=" + gotDone); process.exit(1) }
if (failed || toasts.length !== 0)
  { console.error("phantom error frame: " + JSON.stringify(toasts)); process.exit(1) }
if (buf !== "")
  { console.error("trailing bytes left in buffer: " + JSON.stringify(buf)); process.exit(1) }
console.log("ok")
"""
        )
        rc, out = _run_node(harness)
        self.assertEqual(rc, 0, out)

    def test_tabs_follow_the_wai_aria_pattern(self) -> None:
        """v0.2.311: the pane switcher declares role=tablist/tab, a contract
        that promises keyboard interaction — ArrowLeft/Right/Home/End must
        move focus AND selection between tabs (automatic activation), and
        each tab must be linked to its panel via aria-controls ↔
        aria-labelledby. Verify the wiring statically, then execute the real
        handler under node and drive it with synthetic key events."""
        html = _html()
        for tid, pid in (
            ("tabSrc", "paneSrc"),
            ("tabChat", "paneChat"),
            ("tabStudio", "paneStudio"),
        ):
            self.assertIn(f'id="{tid}"', html)
            self.assertIn(f'aria-controls="{pid}"', html)
            self.assertIn(f'id="{pid}" role="tabpanel" aria-labelledby="{tid}"', html)
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available; JS behavior check skipped")
        src = _script_body(html)
        block = src[src.index("const selectTab") : src.index('$("#langBtn").onclick')]
        harness = """\
const panes = {};
for (const id of ["paneSrc","paneChat","paneStudio"])
  panes[id] = {_on:false, classList:{
    add(){ panes[id]._on = true; }, remove(){ panes[id]._on = false; }}};
const tabs = ["paneSrc","paneChat","paneStudio"].map(p=>({
  dataset:{pane:p}, _sel:"false", focused:false,
  setAttribute(k,v){ if(k==="aria-selected") this._sel=v; },
  focus(){ tabs.forEach(t=>t.focused=false); this.focused=true; },
}));
const document = {querySelectorAll: sel =>
  sel===".tabs button" ? tabs : sel===".pane" ? Object.values(panes) : []};
const $ = sel => panes[sel.slice(1)];
""" + block + """
const key = (tab,k)=>tab.onkeydown({key:k, preventDefault(){}});
const state = ()=>JSON.stringify([
  tabs.findIndex(t=>t._sel==="true"),
  tabs.findIndex(t=>t.focused),
  ["paneSrc","paneChat","paneStudio"].findIndex(p=>panes[p]._on)]);
const check = (want, what)=>{ if(state()!==want){
  console.error(what+": "+state()+" != "+want); process.exit(1); } };
tabs[0].onclick();
check("[0,-1,0]", "click selects tab0");
key(tabs[0],"ArrowRight"); check("[1,1,1]", "ArrowRight moves to next tab");
key(tabs[1],"ArrowRight"); check("[2,2,2]", "ArrowRight moves to last tab");
key(tabs[2],"ArrowRight"); check("[0,0,0]", "ArrowRight wraps to first");
key(tabs[0],"ArrowLeft");  check("[2,2,2]", "ArrowLeft wraps to last");
key(tabs[2],"Home");       check("[0,0,0]", "Home jumps to first");
key(tabs[0],"End");        check("[2,2,2]", "End jumps to last");
key(tabs[0],"x");          check("[2,2,2]", "non-arrow keys are ignored");
console.log("ok");
"""
        code, out = _run_node(harness)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
