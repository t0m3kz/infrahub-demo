"""Schema rules the server enforces on load that no unit test used to see."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"
SCHEMA_FILES = sorted(SCHEMA_DIR.rglob("*.yml"))


@pytest.mark.parametrize("schema_file", SCHEMA_FILES, ids=lambda path: str(path.relative_to(SCHEMA_DIR)))
def test_a_generic_does_not_inherit_from_another_generic(schema_file: Path) -> None:
    """Infrahub rejects `inherit_from` on a generic ("Unknown field"): shared fields are inherited by the nodes."""
    document = yaml.safe_load(schema_file.read_text()) or {}
    offenders = [
        f"{generic.get('namespace', '')}{generic.get('name', '?')}"
        for generic in document.get("generics") or []
        if "inherit_from" in generic
    ]
    assert not offenders, f"{schema_file.name}: generics with inherit_from: {offenders}"


def test_every_schema_file_was_found() -> None:
    """The parametrisation above is not vacuous."""
    assert len(SCHEMA_FILES) > 20
