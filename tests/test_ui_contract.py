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

    def test_every_api_path_matches_a_registered_route(self) -> None:
        """A path the UI fetches but the server never registers is a 404 in waiting."""
        script = _script_body(_html())
        # Fetch paths appear as api("/api/…") or api(`/api/…${expr}/…`).
        raw_paths = set(re.findall(r'api\(\s*[`"](/api/[^`"?]*)', script))
        self.assertTrue(raw_paths, "expected /api/ calls in index.html")

        patterns = [p for _verb, p, _name in _Handler._ROUTES]
        for raw in sorted(raw_paths):
            # Substitute ${...} interpolations with a concrete id so the literal
            # can be matched against the server's numeric-id route patterns.
            concrete = re.sub(r"\$\{[^}]*\}", "1", raw).rstrip("/")
            self.assertTrue(
                any(re.match(p, concrete) for p in patterns),
                f"index.html calls {raw!r} (as {concrete!r}) but no server route matches it",
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
