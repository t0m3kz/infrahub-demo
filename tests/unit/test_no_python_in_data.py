"""data/demos holds object files and documentation only: no Python (loaders and helpers live under tests/ or utils/)."""

from pathlib import Path

DEMOS = Path(__file__).resolve().parents[2] / "data" / "demos"


def test_no_python_file_under_data_demos() -> None:
    """Any *.py below data/demos fails the test, with the offending paths."""
    found = sorted(str(path.relative_to(DEMOS)) for path in DEMOS.rglob("*.py"))
    assert not found, f"Python files under data/demos (move them to tests/ or utils/): {found}"
