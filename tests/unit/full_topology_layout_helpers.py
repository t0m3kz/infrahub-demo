"""Path-independent, schema-aware fingerprints of authored objects and nested parent bindings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from infrahub_sdk.spec.object import DataProcessorFactory, InfrahubObjectParameters

from tests.integration.demo_data import Declared, _resolve, human_friendly_id, relationship, relationship_names


def canonical(value: Any) -> str:
    """Stable JSON without file paths or object-memory identities."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hfid(obj: Declared) -> list[Any]:
    """Resolve actual schema HFIDs, preserving declaration evidence for unresolved computed parts."""
    parts = []
    for part in human_friendly_id(obj.kind):
        try:
            parts.append(_resolve(obj, part.split("__")))
        except (LookupError, ValueError):
            parts.append({"unresolved": part, "declaration": obj.item})
    return [obj.kind, parts]


async def fingerprints(root: Path) -> dict[str, list[str]]:
    """Expand ranges using the SDK and flatten every nested form with its schema parent edge."""
    result: dict[str, list[str]] = {"top": [], "flat": [], "bindings": []}

    async def walk(
        kind: str,
        rows: list[dict[str, Any]],
        params: InfrahubObjectParameters,
        parent: Declared | None = None,
        edge: str = "",
    ) -> None:
        for item in rows:
            obj = Declared(root, kind, item, parent)
            ancestors = []
            ancestor = parent
            while ancestor:
                ancestors.append(_hfid(ancestor))
                ancestor = ancestor.enclosing
            result["flat"].append(canonical([kind, item, _hfid(obj), ancestors]))
            if parent is not None:
                rel = relationship(parent.kind, edge) or {}
                inverse = [
                    [key, relationship(kind, key)]
                    for key in sorted(relationship_names(kind))
                    if rel.get("identifier") and (relationship(kind, key) or {}).get("identifier") == rel["identifier"]
                ]
                result["bindings"].append(canonical([_hfid(parent), edge, rel, _hfid(obj), inverse, ancestors]))
            for key, value in item.items():
                if isinstance(value, dict) and "data" in value:
                    child_kind = value.get("kind") or (relationship(kind, key) or {})["peer"]
                    data = value["data"]
                    rows = (
                        await DataProcessorFactory.process_data(kind=child_kind, parameters=params, data=data)
                        if isinstance(data, list)
                        else [data]
                    )
                    await walk(child_kind, rows, params, obj, key)
                elif isinstance(value, list):
                    for child in value:
                        if isinstance(child, dict) and "data" in child:
                            await walk(child["kind"], [child["data"]], params, obj, key)

    for path in sorted(root.rglob("*.yml")):
        for doc in yaml.safe_load_all(path.read_text()):
            spec = doc["spec"]
            params = InfrahubObjectParameters(**spec.get("parameters", {}))
            rows = await DataProcessorFactory.process_data(kind=spec["kind"], parameters=params, data=spec["data"])
            result["top"] += [
                canonical([doc["apiVersion"], doc["kind"], spec["kind"], params.model_dump(), row]) for row in rows
            ]
            await walk(spec["kind"], rows, params)
    return {key: sorted(value) for key, value in result.items()}
