"""Generators."""

import sys
from pathlib import Path

# Infrahub loads files from git without utils on the path
_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
