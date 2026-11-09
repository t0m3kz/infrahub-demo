"""Authored EVPN sessions must have real global-table endpoints and consistent route targets."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from transforms.helpers.bgp import get_bgp_profile
from utils.data_cleaning import clean_data

_DATA = Path(__file__).parents[2] / "data/demos/00_full_topology"


def _declarations(kind: str) -> Iterator[tuple[Path, dict[str, Any]]]:
    for path in sorted(_DATA.rglob("*.yml")):
        for document in yaml.safe_load_all(path.read_text()):
            if document and document["spec"]["kind"] == kind:
                for row in document["spec"]["data"]:
                    yield path, row


def _named(kind: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for _, row in _declarations(kind):
        result.setdefault(row["name"], {}).update(row)
    return result


_PEERS = _named("ManagedBGPPeering")
_PROCESSES = _named("ManagedBGP")
_LOOPBACKS = {row["device"]: row for _, row in _declarations("DcimVirtualInterface") if row.get("name") == "Loopback0"}
_ASNS = {f"AS{row['asn']}": row["asn"] for _, row in _declarations("RoutingAutonomousSystem")}
_EVPN = sorted(name for name, row in _PEERS.items() if row.get("peering_role") in {"overlay", "dci"})


def _raw_process(process_name: str) -> dict[str, Any]:
    process = _PROCESSES[process_name]
    return {
        "__typename": "ManagedBGP",
        "name": {"value": process_name},
        "status": {"value": process["status"]},
        "process_role": {"value": process["process_role"]},
        "local_as": {"node": {"asn": {"value": _ASNS[process["local_as"]]}}},
        "router_id": {
            "node": {
                "address": {"value": process["router_id"]["data"]["address"]},
                "ip_namespace": {"node": {"name": {"value": "default"}}},
            }
        },
        "capabilities": {"edges": [{"node": {"name": {"value": device}}} for device in process["capabilities"]]},
    }


def _raw_peering(name: str) -> dict[str, Any]:
    peer = _PEERS[name]
    return {
        "name": {"value": name},
        "session_type": {"value": peer["session_type"]},
        "peering_role": {"value": peer["peering_role"]},
        "ttl": {"value": peer["ttl"]},
        "send_extended_community": {"value": peer["send_extended_community"]},
        "bgp_processes": {"edges": [{"node": _raw_process(process)} for process in peer["bgp_processes"]]},
        "capabilities": {
            "edges": [
                {
                    "node": {
                        "security_zone": {"node": {"name": {"value": _LOOPBACKS[device]["ip_address"][1]}}},
                        "__typename": "DcimVirtualInterface",
                        "name": {"value": interface},
                        "device": {"node": {"name": {"value": device}}},
                        "ip_address": {
                            "node": {
                                "address": {"value": _LOOPBACKS[device]["ip_address"][0]},
                                "ip_namespace": {"node": {"name": {"value": "default"}}},
                            }
                        },
                    }
                }
                for device, interface in peer["capabilities"]
            ]
        },
    }


def test_every_authored_evpn_peering_is_bound_once_without_changing_its_process_pair() -> None:
    """The final binding phase covers the original overlay and DCI definitions, not an invented subset."""
    original = {
        row["name"]: row
        for path, row in _declarations("ManagedBGPPeering")
        if path.parent.name != "06_fabric" and row.get("peering_role") in {"overlay", "dci"}
    }
    bindings = [row for path, row in _declarations("ManagedBGPPeering") if path.parent.name == "06_fabric"]
    assert len(original) == len(bindings) == 45
    assert {row["name"] for row in bindings} == set(original)
    for row in bindings:
        assert row["bgp_processes"] == original[row["name"]]["bgp_processes"]
        assert row["peering_role"] == original[row["name"]]["peering_role"]
        assert row["address_families"] == original[row["name"]]["address_families"]


def test_each_overlay_process_has_one_actual_loopback_reusing_its_router_id_in_default() -> None:
    """EVPN transport is in the global table; the loopback and router ID refer to the same address."""
    processes = [process for process in _PROCESSES.values() if process["process_role"] == "overlay"]
    assert len(processes) == len(_LOOPBACKS) == 21
    for process in processes:
        (device,) = process["capabilities"]
        loopback = _LOOPBACKS[device]
        address = process["router_id"]["data"]
        assert loopback["role"] == "loopback" and loopback["status"] == "active"
        assert loopback["ip_address"] == [address["address"], "default"]
        assert address["ip_namespace"] == "default"
        assert not loopback.get("vrf")


@pytest.mark.parametrize("name", _EVPN)
def test_actual_bgp_helper_builds_both_directions_from_raw_query_shaped_endpoints(name: str) -> None:
    """Every modeled EVPN adjacency resolves two real IPs and ASNs without becoming a tenant VRF session."""
    peer = _PEERS[name]
    assert len(peer["capabilities"]) == 2
    assert {device for device, _ in peer["capabilities"]} == {
        _PROCESSES[process]["capabilities"][0] for process in peer["bgp_processes"]
    }
    for process_name in peer["bgp_processes"]:
        service = _raw_process(process_name)
        service["peerings"] = {"edges": [{"node": _raw_peering(name)}]}
        (device,) = _PROCESSES[process_name]["capabilities"]
        (configuration,) = get_bgp_profile([clean_data(service)], device_name=device)
        (session,) = configuration["sessions"]
        assert session["local_ip"]["address"] == _LOOPBACKS[device]["ip_address"][0]
        assert session["remote_ip"]["address"] != session["local_ip"]["address"]
        assert session["remote_as"]["asn"] != session["local_as"]["asn"]
        assert session["send_extended_community"] is True
        assert session["session_type"] == "EBGP_MULTIHOP" and session["ttl"] == 2
        assert "vrf" not in session


@pytest.mark.parametrize("end_count", [0, 1])
def test_missing_evpn_endpoints_still_raise_instead_of_silently_dropping_sessions(end_count: int) -> None:
    """Completing the fixture must not weaken the existing malformed-adjacency guard."""
    peer = deepcopy(_raw_peering(_EVPN[0]))
    peer["capabilities"]["edges"] = peer["capabilities"]["edges"][:end_count]
    process_name = _PEERS[_EVPN[0]]["bgp_processes"][0]
    service = _raw_process(process_name)
    service["peerings"] = {"edges": [{"node": peer}]}
    device = _PROCESSES[process_name]["capabilities"][0]
    with pytest.raises(ValueError, match="expected 2 capabilities"):
        get_bgp_profile([clean_data(service)], device_name=device)


def test_route_target_administrators_use_existing_site_asns_without_rewriting_site_metadata() -> None:
    """All six facilities choose an already-modeled local ASN; a DC upsert retains its design and index."""
    anchors = [
        row
        for kind in ("TopologyDataCenter", "TopologyColocationMetro")
        for path, row in _declarations(kind)
        if path.parent.name == "06_fabric"
    ]
    assert len(anchors) == 6 and len({row["evpn_rt_as"] for row in anchors}) == 6
    for anchor in anchors:
        assert anchor["evpn_rt_as"] in _ASNS
        site = anchor["name"].removeprefix("DC-").lower()
        edge = f"{site}-edge-01-bgp" if anchor["name"].startswith("DC-") else f"{site}-colo1-edge-01-bgp"
        assert anchor["evpn_rt_as"] == _PROCESSES[edge]["local_as"]
    for path, row in _declarations("TopologyDataCenter"):
        if path.parent.name != "06_fabric":
            final = _named("TopologyDataCenter")[row["name"]]
            assert {key: final[key] for key in ("parent", "index", "design")} == {
                key: row[key] for key in ("parent", "index", "design")
            }


def test_final_internet_vrf_membership_keeps_the_edge_and_both_external_firewall_borders() -> None:
    """A later VRF upsert must not remove the existing upstream member while adding the firewall transit demand."""
    vrf = _named("ManagedVRF")["dc-ktw1-vrf-internet"]
    assert set(vrf["shared_devices"]) == {"ktw1-edge-01", "ktw1-bl-01", "ktw1-bl-02"}
