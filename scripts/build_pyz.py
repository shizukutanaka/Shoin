"""Build dist/shoin.pyz — a single-file zipapp of the Shoin package.

stdlib-only distribution path (v0.2.668): a .pyz runs with just
`python3 shoin.pyz <subcommand>` — no pip install, venv, or repo clone.
The interpreter requirement (Python 3.11+) is the same as the package's
declared requires-python; it is not a native binary.

Usage: python3 scripts/build_pyz.py [output-path]
"""

import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The archive needs a top-level __main__.py. zipapp's own `main=` emits
# `shoin.cli.main()` and DROPS the returned exit code — write our own so
# `sys.exit(main())` keeps the coded rc (same as shoin/__main__.py).
MAIN = "import sys\nfrom shoin.cli import main\nsys.exit(main())\n"


def main() -> int:
    out = (
        Path(sys.argv[1]).expanduser()
        if len(sys.argv) > 1
        else ROOT / "dist" / "shoin.pyz"
    )
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "app"
        shutil.copytree(
            ROOT / "shoin",
            stage / "shoin",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        (stage / "__main__.py").write_text(MAIN, encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(
            stage, out, interpreter="/usr/bin/env python3", compressed=True
        )
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
