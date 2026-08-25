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


if __name__ == "__main__":
    unittest.main(verbosity=1)
