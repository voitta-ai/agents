"""Enable `python -m framework run ...` alongside the `agent` console script."""
from __future__ import annotations

import sys

from framework.cli import main

if __name__ == "__main__":
    sys.exit(main())
