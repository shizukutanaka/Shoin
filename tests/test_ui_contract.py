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
        # Keys the markup asks for, via any of the four attribute flavours.
        used = set(re.findall(r'data-i18n(?:-aria|-ph|-title)?="([^"]+)"', html))
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
            f"I18N.ja-only keys (en falls back to ja text): {sorted(locales['ja'] - locales['en'])}",
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
let externalPendingRename = null;
function renderChatHistory(){} function renderStudio(){}
function renderNotes(){} function refreshQuestions(){}
function startSourceRename(){ return {setSelectionRange(){}} }
""" + fn + """
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
  const localStorage = { getItem(k){ return lsv; } };
  const document = { querySelector(s){ return metaContent === null ? null : {content: metaContent} } };
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
const cbadges = c.children.filter(b => b.cls === "badge warn" && String(b.text).includes("coverage"));
if (cbadges.length !== 1) { console.error("coverage badge missing"); process.exit(1) }
// v0.2.245: a finish_reason "length" answer must carry a visible warning chip.
const tbadges = c.children.filter(b => b.cls === "badge warn" && String(b.text).includes("truncated"));
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
  { console.error("omitted-count line missing: " + JSON.stringify(calls.prepended)); process.exit(1) }
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
if (refetches !== 1) { console.error("first off->on did not refetch: " + refetches); process.exit(1) }
apiImpl = async () => { throw new Error("net down") };
await health();                                    // fetch failure
if (window._llmOn !== false) { console.error("_llmOn not false after failure"); process.exit(1) }
if (reg["#lamp"].classList.contains("on")) { console.error("lamp stayed green on failure"); process.exit(1) }
if (reg["#banner"].style.display !== "block") { console.error("banner hidden on failure"); process.exit(1) }
if (refetches !== 1) { console.error("failure refetched questions"); process.exit(1) }
apiImpl = async () => ({ json: async () => ({ llm: true }) });
await health();                                    // off->on recovery
if (!window._llmOn) { console.error("_llmOn not restored"); process.exit(1) }
if (reg["#banner"].style.display !== "none") { console.error("banner still shown after recovery"); process.exit(1) }
if (refetches !== 2) { console.error("questions not refetched on off->on: " + refetches); process.exit(1) }
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
if (reg["#qs"].children.length !== 2) { console.error("chips not rendered: " + reg["#qs"].children.length); process.exit(1) }
const chip = reg["#qs"].children[0];
if (chip.tag !== "button" || chip.type !== "button" || chip.cls !== "q-chip")
  { console.error("chip shape wrong: " + JSON.stringify({t:chip.tag, ty:chip.type, c:chip.cls})); process.exit(1) }
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
  { console.error("guards fetched or kept chips: calls=" + calls.length + " chips=" + reg["#qs"].children.length); process.exit(1) }

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
let externalPendingRename = null;
let renameCalls = [], selCalls = [];
function startSourceRename(s, tt, row, initial){
  renameCalls.push({srcId: s.id, initial});
  const inp = mkEl(); inp.cls = "src-rename"; return inp;
}
const document = { activeElement: null };
""" + fn + """
// Path 1: focused rename input inside #srcList survives the rebuild.
cur = { id:1, name:"nb", sources:[{id:5,title:"old",kind:"txt"}], messages:[], studio:[], notes:[] };
const rin = mkEl();
rin.classList = { contains: c => c === "src-rename" };
rin.dataset = { srcId: "5" };
rin.value = "mid-edit"; rin.selectionStart = 2; rin.selectionEnd = 4;
rin.onblur = ()=>{}; rin.onkeydown = ()=>{};
$("#srcList").children = [rin]; rin.parent = $("#srcList");
document.activeElement = rin;
renderNotebook();
if (renameCalls.length !== 1 || renameCalls[0].srcId !== 5 || renameCalls[0].initial !== "mid-edit")
  { console.error("activeElement path did not restore: " + JSON.stringify(renameCalls)); process.exit(1) }
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
let externalPendingRename = null;
let renameCalls = [];
function startSourceRename(s, tt, row, initial){
  renameCalls.push({srcId: s.id, initial});
  const inp = mkEl(); inp.cls = "src-rename"; return inp;
}
const document = { activeElement: null };
""" + fn + """
(async () => {
cur = { id:3, name:"nb", sources:[{id:9,title:"t",kind:"url",origin:"https://x"}],
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
let externalPendingRename = null;
let renameCalls = [];
function startSourceRename(s, tt, row, initial){
  renameCalls.push({srcId: s.id, initial});
  const inp = mkEl(); inp.cls = "src-rename"; return inp;
}
const document = { activeElement: null };
""" + fn + """
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
row.onkeydown({key:"Enter", preventDefault(){}});
row.onkeydown({key:" ", preventDefault(){}});
row.onkeydown({key:"x", preventDefault(){}});
if (shown.length !== 3 || shown.some(x => x !== 9))
  { console.error("row open wiring wrong: " + JSON.stringify(shown)); process.exit(1) }
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
  onclick:null, onblur:null, onkeydown:null, click(){ if(this.onclick) this.onclick({stopPropagation(){}}) } }; }
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
  replaceChildren(...xs){ this.children=[]; xs.forEach(x => { this.children.push(x); x.parent=this }) },
  setAttribute(n,v){ this["attr_"+n]=v },
  onclick:null, onkeydown:null,
  click(){ if(this.onclick) this.onclick({stopPropagation(){}}) },
  press(k){ if(this.onkeydown) this.onkeydown({key:k, preventDefault(){}}) } }; }
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
  { console.error("highlight wrong: " + lis[0].className + "/" + lis[1].className); process.exit(1) }
if (lis[0].children[1].text !== "2册")
  { console.error("count label: " + lis[0].children[1].text); process.exit(1) }

// Row click + Enter opens; Space opens too.
lis[0].click(); lis[1].press("Enter"); lis[0].press(" ");
if (JSON.stringify(opens) !== JSON.stringify([1,3,1]))
  { console.error("open wiring: " + opens); process.exit(1) }

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
        harness = (
            """\
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
    { console.error("nbForm cleanup wrong: " + JSON.stringify({v: nbName.value, opens: calls.opens, d: nbBtn.disabled})); process.exit(1) }
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
            + f"events.noteForm = async e=>\n{blocks['noteForm'].split('onsubmit = async e=>',1)[1]}\n"
            + f"events.reindex = async ()=>\n{blocks['reindex'].split('onclick = async ()=>',1)[1]}\n"
            + f"events.clearChat = async ()=>\n{blocks['clearChat'].split('onclick = async ()=>',1)[1]}\n"
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
