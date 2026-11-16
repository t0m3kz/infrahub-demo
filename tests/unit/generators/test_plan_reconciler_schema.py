"""The field names the reconciler sends, checked against the schema."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import pytest
import yaml

from generators.plan import DeviceSpec, TopologyPlan
from tests.unit.plan_reconciler_helpers import FakeClient, make_generator

SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
# Keys every kind accepts on create besides its schema fields
_UNIVERSAL_KEYS = {"id", "object_template", "member_of_groups"}


@functools.cache
def _schema() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(kind -> {attribute and relationship names declared on it}, kind -> its generics), extensions merged."""
    fields: dict[str, set[str]] = {}
    parents: dict[str, set[str]] = {}

    def add(kind: str, entry: dict[str, Any]) -> None:
        fields.setdefault(kind, set()).update(
            item["name"] for key in ("attributes", "relationships") for item in entry.get(key) or []
        )
        parents.setdefault(kind, set()).update(entry.get("inherit_from") or [])

    for path in sorted(SCHEMAS.rglob("*.yml")):
        document = yaml.safe_load(path.read_text()) or {}
        for section in ("nodes", "generics"):
            for entry in document.get(section) or []:
                add(f"{entry['namespace']}{entry['name']}", entry)
            for entry in (document.get("extensions") or {}).get(section) or []:
                add(entry["kind"], entry)
    return fields, parents


def schema_fields(kind: str) -> set[str]:
    """Every attribute and relationship name of ``kind``, its generics' included."""
    fields, parents = _schema()
    names: set[str] = set()
    pending = [kind]
    while pending:
        current = pending.pop()
        names |= fields.get(current, set())
        pending.extend(parents.get(current, set()))
    return names


class TestSchemaFieldNames:
    """The keys of every create() data are real attributes or relationships of the kind (a wrong one is dropped
    silently by the SDK, or rejected by the server)."""

    @pytest.mark.parametrize(
        ("kind", "key"),
        [
            ("DcimPhysicalDevice", "owner"),
            ("DcimPhysicalDevice", "deployment"),
            ("DcimPhysicalDevice", "description"),
            ("DcimPhysicalDevice", "status"),
        ],
    )
    def test_the_schema_loader_knows_the_fields_the_reconciler_writes(self, kind: str, key: str) -> None:
        assert key in schema_fields(kind)
        assert "connected_endpoints" not in schema_fields("DcimCable")

    @pytest.mark.asyncio
    async def test_device_create_data_uses_schema_fields(self) -> None:
        client = FakeClient()
        client.seed("TemplateDcimPhysicalDevice", template_name="TPL")
        plan = TopologyPlan(root_id="r")
        plan.add(
            DeviceSpec(
                name="dev",
                template_name="TPL",
                owner_id="o",
                deployment_id="d",
                description="x",
            )
        )
        await make_generator(client).apply_plan(plan)

        sent = set().union(*(set(data) for kind, _, data, _ in client.saves if kind == "DcimPhysicalDevice"))
        assert sent - _UNIVERSAL_KEYS <= schema_fields("DcimPhysicalDevice")
        assert {"status", "owner", "deployment", "description"} <= sent
