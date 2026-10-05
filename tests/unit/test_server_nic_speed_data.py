"""Every DC6 demo server NIC matches a customer port it can be cabled to.

The endpoint generator cables servers with strict speed validation: a 100G NIC
facing only 25G switch ports gets no cable at all, the bonds are still
planned, and the run completes without an error. Only the integration suite's
"servers without any uplink cable" check notices, an hour in. These checks
read the YAML alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_DATA = Path(__file__).parents[2] / "data"
_SERVERS = _DATA / "demos" / "06_servers" / "servers"
_RACK_FILES = [
    _DATA / "demos" / "01_data_center" / "dc6" / "01_topology.yml",
    _DATA / "demos" / "02_switch_dc6" / "01_dc-6-switches.yml",
    _DATA / "demos" / "03_rack_dc6" / "dc-6-rack.yml",
]
# Roles add_endpoint cables servers to
_ACCESS_ROLES = {"tor", "l2-leaf", "access-leaf"}


def _walk(node: Any, kind: str, found: list[tuple[str, dict[str, Any]]]) -> None:
    """Collect (kind, entry) for every object, following nested kind/data blocks."""
    if isinstance(node, dict):
        if "kind" in node and "data" in node:
            _walk(node["data"], node["kind"], found)
            return
        found.append((kind, node))
        for value in node.values():
            _walk(value, kind, found)
    elif isinstance(node, list):
        for item in node:
            _walk(item, kind, found)


def _objects(paths: list[Path]) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for path in paths:
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and "spec" in doc:
                _walk(doc["spec"]["data"], doc["spec"]["kind"], found)
    return found


def _objects_of(entry: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    _walk(entry.get("interfaces"), "", found)
    return found


def _customer_port_types() -> dict[str, set[str]]:
    """Device template name -> interface types of its customer ports."""
    ports: dict[str, set[str]] = {}
    for path in sorted((_DATA / "bootstrap").glob("10_physical_devices_templates_*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if not doc or "spec" not in doc:
                continue
            for template in doc["spec"]["data"]:
                interfaces = (template.get("interfaces") or {}).get("data") or []
                ports[template["template_name"]] = {
                    i["interface_type"] for i in interfaces if i.get("role") == "customer" and i.get("interface_type")
                }
    return ports


def _access_templates_by_rack() -> dict[str, set[str]]:
    """Rack shortname -> templates of the access switches it holds."""
    racks: dict[str, set[str]] = {}
    for kind, entry in _objects(_RACK_FILES):
        if kind != "LocationRack" or "shortname" not in entry:
            continue
        templates = {t[2] for t in entry.get("fabric_templates") or [] if t[1] in _ACCESS_ROLES}
        racks.setdefault(entry["shortname"], set()).update(templates)
    return racks


def _row(rack: str) -> str:
    """'ktw-1-s-1-r-2-5' -> 'ktw-1-s-1-r-2' (suite and row)."""
    return rack.rsplit("-", 1)[0]


def _reachable_templates(rack: str, racks: dict[str, set[str]]) -> set[str]:
    """The rack's own access switches, else the network rack(s) in its row (middle_rack)."""
    if racks.get(rack):
        return racks[rack]
    return {t for name, templates in racks.items() if _row(name) == _row(rack) for t in templates}


def _servers() -> list[dict[str, Any]]:
    return [
        entry
        for kind, entry in _objects(sorted(_SERVERS.glob("*.yml")))
        if kind == "DcimPhysicalDevice" and entry.get("role") == "endpoint"
    ]


def test_server_data_is_found() -> None:
    """Guard against the parametrization below silently collecting nothing."""
    assert len(_servers()) >= 6


@pytest.mark.parametrize("server", _servers(), ids=lambda s: s["name"])
def test_server_nics_match_a_reachable_customer_port(server: dict[str, Any]) -> None:
    """Each lag-member NIC has a customer port of its own speed on a switch it can reach."""
    racks = _access_templates_by_rack()
    port_types = _customer_port_types()
    templates = _reachable_templates(server["rack"], racks)
    assert templates, f"{server['name']}: no tor/l2-leaf/access-leaf reachable from rack {server['rack']}"

    offered = set().union(*(port_types.get(t, set()) for t in templates))
    nics = {
        i["name"]: i["interface_type"]
        for kind, i in _objects_of(server)
        if kind == "DcimPhysicalInterface" and i.get("role") == "lag"
    }
    mismatched = {name: kind for name, kind in nics.items() if kind not in offered}
    assert not mismatched, (
        f"{server['name']} (rack {server['rack']}): NIC(s) {mismatched} match no customer port "
        f"on {sorted(templates)}, which offer {sorted(offered)}"
    )
