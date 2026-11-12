"""Validators."""

import sys
from pathlib import Path

# Infrahub loads files from git without the repo root on sys.path.
_repo_root = Path(__file__).parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
