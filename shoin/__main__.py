"""`python -m shoin` entry point.

The console_script (`shoin = shoin.cli:main`) only exists once the package
is installed; running the source tree or a plain `python -m shoin`
previously died on `No module named shoin.__main__`. Delegate both paths
to the same CLI entry so the two invocations can never drift apart.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
