"""Guard: no Python module in the repository may exceed ``MAX_LINES`` lines.

A module that long is several topics in one file: it is slow to read, collides in review and hides which behaviour a
failing test belongs to. Split it by topic: a test module into sibling ``test_<name>_<topic>.py`` modules with shared
builders in a ``<name>_helpers.py``; a generator into a mixin or helper module; a transform helper into a sibling
module along the topic seam. Covers ``tests/`` and the application packages ``generators/``, ``transforms/``,
``checks/`` and ``utils/``.
"""

from pathlib import Path

MAX_LINES = 1000

# Modules exempt from the cap, relative to the repository root, each with its reason.
PROTOCOLS_MODULE = "generators/protocols.py"
EXEMPT_MODULES: dict[str, str] = {
    PROTOCOLS_MODULE: "hand-curated typing mirror of the schema, one class per kind",
}

TESTS_DIR = Path(__file__).resolve().parent.parent
ROOT = TESTS_DIR.parent
APPLICATION_DIRS = ("generators", "transforms", "checks", "utils")


def _oversized(directory: Path) -> dict[str, int]:
    """Python modules under ``directory`` over ``MAX_LINES`` lines, keyed by repository-relative path."""
    return {
        path.relative_to(ROOT).as_posix(): lines
        for path in directory.rglob("*.py")
        if "__pycache__" not in path.parts and ".scratch" not in path.parts
        if path.relative_to(ROOT).as_posix() not in EXEMPT_MODULES
        if (lines := len(path.read_text(encoding="utf-8").splitlines())) > MAX_LINES
    }


def test_no_oversized_test_module() -> None:
    """Every Python module under ``tests/`` stays within ``MAX_LINES``."""
    offenders = _oversized(TESTS_DIR)
    assert not offenders, f"split these test modules by topic (limit {MAX_LINES} lines): {offenders}"


def test_no_oversized_application_module() -> None:
    """Every Python module under generators/, transforms/, checks/ and utils/ stays within ``MAX_LINES``."""
    offenders = {path: lines for name in APPLICATION_DIRS for path, lines in _oversized(ROOT / name).items()}
    assert not offenders, f"split these modules by topic into a mixin or helper (limit {MAX_LINES} lines): {offenders}"


def test_exempt_modules_exist() -> None:
    """An exemption names a module that exists, so a rename or removal drops it from the list."""
    missing = [name for name in EXEMPT_MODULES if not (ROOT / name).is_file()]
    assert not missing, f"drop these exemptions, the modules are gone: {missing}"
