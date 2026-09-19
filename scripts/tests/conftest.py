"""Make `render_sydes_pr` importable from the sibling `scripts/` directory
without turning it into an installed package -- this repo is a demo
scaffold of loose scripts, not a Python package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
