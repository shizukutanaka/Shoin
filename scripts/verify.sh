#!/usr/bin/env sh
# Shoin verification gate — the same checks ci/ci.yml runs, in one command.
#
# Why this exists: GitHub Actions cannot be activated from an automated agent
# (pushing .github/workflows/ is refused without the App's `workflows`
# permission — verified empirically, see ci/README.md). But "CI runs on GitHub"
# was never the actual requirement; "every commit is verified before it lands"
# was. This script plus .githooks/pre-push delivers that with no GitHub
# permission at all, and doubles as the local pre-flight once CI is activated.
#
#   ./scripts/verify.sh          run every gate
#   git config core.hooksPath .githooks   run it automatically before each push
#
# A gate whose tool is missing is reported as SKIP, not silently marked FAIL —
# a missing linter must not masquerade as a *failing* one either, since a
# fresh/rebuilt environment (this happens: containers get recycled) shouldn't
# read as "the code is broken" when it's actually "the tool never got
# installed here." But because GitHub Actions cannot run for this project at
# all (see above), this script is the ONLY verification gate that exists —
# there is no CI backstop to catch what a SKIP silently let through. A gate
# missing ENTIRELY (ruff/mypy/coverage not installed — an entire verification
# dimension never attempted) therefore blocks the push by default too
# (reported as INCOMPLETE, not ALL GATES PASSED — the earlier wording was
# checked directly against a reproduction with all three absent and found to
# say "ALL GATES PASSED" with exit 0, which is exactly the "missing tool
# masquerades as a pass" failure this comment already claimed to guard
# against for years). README documents these as required dev dependencies
# for exactly this reason: `pip install -e . && pip install ruff mypy
# coverage`. The narrower, previously-verified mypy "only missing-import
# errors" case (the project's OWN dependency, e.g. pypdf, not installed —
# v0.2.153) is a real SKIP but does NOT block: mypy still ran and
# type-checked everything else, unlike the tool being absent.
#
# To explicitly run with whatever is installed and accept a blocking gap
# (e.g. a deliberate quick test-only check), set
# SHOIN_VERIFY_ALLOW_INCOMPLETE=1.
set -eu

PY="${PYTHON:-python3}"
fail=0
skipped=0
run() {  # run <label> <command...>
    label="$1"; shift
    printf '\n=== %s ===\n' "$label"
    if "$@"; then
        printf '  OK: %s\n' "$label"
    else
        printf '  FAIL: %s\n' "$label"
        fail=1
    fi
}
have() { "$PY" -c "import $1" >/dev/null 2>&1; }

if have ruff; then
    # pyproject pins the rule set, so this cannot drift with the ruff version.
    run "lint (ruff check)" "$PY" -m ruff check .
else
    printf '\n=== lint (ruff check) ===\n  SKIP: ruff not installed (pip install ruff)\n'
    skipped=1
fi

if have mypy; then
    printf '\n=== types (mypy --strict shoin/) ===\n'
    if out=$("$PY" -m mypy --strict shoin/ 2>&1); then
        printf '%s\n  OK: types\n' "$out"
    elif ! printf '%s' "$out" | grep -q 'error:' \
      || ! printf '%s' "$out" | grep 'error:' | grep -qv 'import-not-found'; then
        # Every error is a missing import: the project's own dependencies are not
        # installed here, so mypy cannot see their stubs. That is a narrow,
        # previously-verified environment gap (v0.2.153: installing the project's
        # own declared dependency makes this "Success: no issues found"), NOT the
        # same class as mypy never running at all — mypy still type-checked
        # everything else in strict mode with the unresolved names as Any, so
        # this does NOT count toward `skipped`'s blocking policy below.
        printf '%s\n  SKIP: only missing-import errors — run `pip install -e .` to type-check fully\n' "$out"
    else
        printf '%s\n  FAIL: types\n' "$out"
        fail=1
    fi
else
    printf '\n=== types (mypy) ===\n  SKIP: mypy not installed (pip install mypy)\n'
    skipped=1
fi

if have coverage; then
    run "tests + coverage" sh -c \
        "$PY -m coverage run -m unittest discover -s tests -p 'test_*.py' \
         && $PY -m coverage report --include='shoin/*' --fail-under=90"
else
    run "tests" "$PY" -m unittest discover -s tests -p 'test_*.py'
    printf '  SKIP: coverage not installed; ran tests without the 90%% threshold\n'
    skipped=1
fi

printf '\n'
if [ "$fail" -ne 0 ]; then
    printf 'verify: FAILURES ABOVE — fix before pushing\n'
elif [ "$skipped" -ne 0 ] && [ "${SHOIN_VERIFY_ALLOW_INCOMPLETE:-}" != "1" ]; then
    printf 'verify: INCOMPLETE — one or more gates SKIPPED above (missing tool), so\n'
    printf 'this is NOT a verified pass: this script is the only verification gate\n'
    printf 'this project has (GitHub Actions cannot run here). Install the missing\n'
    printf 'tool(s), or set SHOIN_VERIFY_ALLOW_INCOMPLETE=1 to push anyway.\n'
    fail=1
elif [ "$skipped" -ne 0 ]; then
    printf 'verify: gates ran that could run; SKIPPED gates above were accepted via\n'
    printf 'SHOIN_VERIFY_ALLOW_INCOMPLETE=1 — this is a partial check, not a full pass.\n'
else
    printf 'verify: ALL GATES PASSED\n'
fi
exit "$fail"
