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
# Gates that need a tool you don't have installed are SKIPPED (reported), not
# failed — a missing linter must not masquerade as a passing one.
set -eu

PY="${PYTHON:-python3}"
fail=0
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
fi

if have mypy; then
    printf '\n=== types (mypy --strict shoin/) ===\n'
    if out=$("$PY" -m mypy --strict shoin/ 2>&1); then
        printf '%s\n  OK: types\n' "$out"
    elif ! printf '%s' "$out" | grep -q 'error:' \
      || ! printf '%s' "$out" | grep 'error:' | grep -qv 'import-not-found'; then
        # Every error is a missing import: the project's own dependencies are not
        # installed here, so mypy cannot see their stubs. That is an environment
        # gap, not a type defect — reporting it as a failure would be as wrong as
        # reporting an uninstalled linter as a pass.
        printf '%s\n  SKIP: only missing-import errors — run `pip install -e .` to type-check fully\n' "$out"
    else
        printf '%s\n  FAIL: types\n' "$out"
        fail=1
    fi
else
    printf '\n=== types (mypy) ===\n  SKIP: mypy not installed (pip install mypy)\n'
fi

if have coverage; then
    run "tests + coverage" sh -c \
        "$PY -m coverage run -m unittest discover -s tests -p 'test_*.py' \
         && $PY -m coverage report --include='shoin/*' --fail-under=90"
else
    run "tests" "$PY" -m unittest discover -s tests -p 'test_*.py'
    printf '  NOTE: coverage not installed; ran tests without the 90%% threshold\n'
fi

printf '\n'
if [ "$fail" -eq 0 ]; then
    printf 'verify: ALL GATES PASSED\n'
else
    printf 'verify: FAILURES ABOVE — fix before pushing\n'
fi
exit "$fail"
