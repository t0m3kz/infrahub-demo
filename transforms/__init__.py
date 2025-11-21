"""Transforms."""

import sys
from pathlib import Path

# Add parent directory to sys.path so utils package can be imported
# This is needed because Infrahub loads files from git without utils in path
_repo_root = Path(__file__).parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
