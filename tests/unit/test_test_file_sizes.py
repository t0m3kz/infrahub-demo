"""Guard: no Python module under ``tests/`` may exceed ``MAX_LINES`` lines.

A module that long is several topics in one file: it is slow to read, collides in review and hides which behaviour a
failing test belongs to. Split it by topic into sibling modules, with shared builders in a ``<topic>_helpers.py``.
"""

from pathlib import Path

MAX_LINES = 1000

TESTS_DIR = Path(__file__).resolve().parent.parent


def test_no_oversized_test_module() -> None:
    """Every Python module under ``tests/`` stays within ``MAX_LINES``."""
    root = TESTS_DIR.parent
    offenders = {
        path.relative_to(root).as_posix(): lines
        for path in TESTS_DIR.rglob("*.py")
        if "__pycache__" not in path.parts and ".scratch" not in path.parts
        if (lines := len(path.read_text(encoding="utf-8").splitlines())) > MAX_LINES
    }
    assert not offenders, f"split these test modules by topic (limit {MAX_LINES} lines): {offenders}"
