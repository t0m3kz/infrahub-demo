"""Unit tests for the interconnect inventory helper of the integration scenarios."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from tests.integration.circuit_helpers import fetch_interconnect_inventory
from tests.unit.integration_scenario_helpers_helpers import _no_propagation_delay  # noqa: F401 (autouse fixture)


async def test_fetch_interconnect_inventory_parses_location_and_end_placement() -> None:
    """A circuit location keeps its kind, parent (a cage's metro, a footprint's DC) and owner, and each end
    its device's deployment, with the metro above a zone or virtual colocation deployment: test_62 checks
    circuit placement on exactly these fields."""

    def value(v: Any) -> dict:
        return {"value": v}

    def end(
        device: str,
        deployment: str,
        kind: str,
        parent: dict | None = None,
        peers: list[dict] | None = None,
        port: dict | None = None,
    ) -> dict:
        return {
            "node": {
                "id": f"if-{device}",
                "name": value("Ethernet1/35"),
                "capabilities": {"edges": [{"node": peer} for peer in peers or []]},
                **(port or {}),
                "device": {
                    "node": {
                        "id": f"dev-{device}",
                        "name": value(device),
                        "deployment": {
                            "node": {
                                "id": f"id-{deployment}",
                                "__typename": kind,
                                "name": value(deployment),
                                **({"parent": parent} if parent else {}),
                            }
                        },
                    }
                },
            }
        }

    metro = {"node": {"id": "id-FR", "__typename": "TopologyColocationMetro", "name": value("FR")}}
    dc = {"node": {"id": "id-DC10", "__typename": "TopologyDataCenter", "name": value("DC10")}}
    owner = {"node": {"id": "org-C005", "name": value("Drentec BV")}}
    physical = {
        "id": "pc1",
        "circuit_id": value("DF-DC10-EQXFR2"),
        "circuit_type": value("dark_fiber"),
        "status": value("active"),
        "provider": {"node": {"name": value("P016")}},
        "owner": {"node": None},
        "locations": {
            "edges": [
                {
                    "node": {
                        "id": "id-FR2",
                        "__typename": "TopologyColocationZone",
                        "name": value("FR2"),
                        "parent": metro,
                    }
                },
                {"node": {"id": "id-DC10", "__typename": "TopologyDataCenter", "name": value("DC10")}},
            ]
        },
        "capabilities": {
            "edges": [
                end("bl-dc101101", "DC10", "TopologyDataCenter"),
                end("eg-fr201", "FR2", "TopologyColocationZone", parent=metro),
            ]
        },
    }

    def carrier(circuit_id: str) -> dict:
        return {
            "__typename": "TopologyCarrierCircuit",
            "id": f"pc-{circuit_id}",
            "circuit_id": value(circuit_id),
            "status": value("active"),
        }

    # A handoff: our edge's sub-interface rides its PARENT's cross-connect, the DC-side end is cabled, not carried.
    virtual = {
        "id": "vc1",
        "__typename": "TopologySessionCircuit",
        "circuit_type": value("handoff"),
        "name": value("HANDOFF-C005-DC10-AWS-EUC1"),
        "owner": owner,
        "ip_namespace": {"node": {"id": "ns-prod", "name": value("PROD")}},
        "password": {"node": {"name": value("eqx-fr2-aws-vif-key")}},
        "cloud_endpoint": {
            "node": {
                "id": "vif1",
                "__typename": "CloudVirtualInterface",
                "name": value("vif-1"),
                "vlan_id": value(1000),
            }
        },
        "locations": {
            "edges": [
                {
                    "node": {
                        "id": "id-C005-P-DC10",
                        "__typename": "TopologyCustomerDC",
                        "name": value("C005-P-DC10"),
                        "parent": dc,
                        "owner": {"node": {"id": "org-C005", "__typename": "OrganizationCustomer"}},
                    }
                },
            ]
        },
        "capabilities": {
            "edges": [
                end(
                    "bl-dc101101",
                    "DC10",
                    "TopologyDataCenter",
                    peers=[
                        # a segment on the port is no carrier; the same circuit twice counts once
                        {"__typename": "ManagedVxlanSegment"},
                        carrier("DF-DC10-EQXFR2"),
                        carrier("DF-DC10-EQXFR2"),
                    ],
                    port={
                        "__typename": "DcimPhysicalInterface",
                        "cable": {"node": {"id": "cable1"}},
                        "ip_address": {"node": {"address": value("10.255.128.1/31")}},
                    },
                ),
                end(
                    "eg-fr201",
                    "FR2",
                    "TopologyColocationZone",
                    parent=metro,
                    port={
                        "__typename": "DcimVirtualInterface",
                        "name": value("Ethernet1/13.1000"),
                        "ip_address": {"node": None},
                        "parent_interface": {
                            "node": {
                                "id": "if-parent",
                                "name": value("Ethernet1/13"),
                                "cable": {"node": None},
                                "capabilities": {"edges": [{"node": carrier("XC-EQXFR2-AWS-EUC1")}]},
                            }
                        },
                    },
                ),
            ]
        },
    }
    client = MagicMock()
    client.execute_graphql = AsyncMock(
        return_value={
            "TopologyCarrierCircuit": {"edges": [{"node": physical}]},
            "TopologySessionCircuit": {"edges": [{"node": virtual}]},
        }
    )

    inventory = await fetch_interconnect_inventory(client=client, branch="main")

    (circuit,) = inventory["physical"]
    assert (circuit["id"], circuit["owner_id"], circuit["locations"]) == ("pc1", None, ["DC10", "FR2"])
    assert circuit["location_nodes"] == [
        {
            "id": "id-DC10",
            "kind": "TopologyDataCenter",
            "name": "DC10",
            "parent_id": None,
            "parent_kind": None,
            "parent_name": None,
            "owner_id": None,
        },
        {
            "id": "id-FR2",
            "kind": "TopologyColocationZone",
            "name": "FR2",
            "parent_id": "id-FR",
            "parent_kind": "TopologyColocationMetro",
            "parent_name": "FR",
            "owner_id": None,
        },
    ]
    assert circuit["end_devices"] == [
        {
            "device": "bl-dc101101",
            "device_id": "dev-bl-dc101101",
            "deployment_id": "id-DC10",
            "deployment_kind": "TopologyDataCenter",
            "deployment_name": "DC10",
            "deployment_parent_id": None,
        },
        {
            "device": "eg-fr201",
            "device_id": "dev-eg-fr201",
            "deployment_id": "id-FR2",
            "deployment_kind": "TopologyColocationZone",
            "deployment_name": "FR2",
            "deployment_parent_id": "id-FR",
        },
    ]
    (handoff,) = inventory["virtual"]
    assert (handoff["id"], handoff["circuit_type"], handoff["owner"], handoff["owner_id"]) == (
        "vc1",
        "handoff",
        "Drentec BV",
        "org-C005",
    )
    (footprint,) = handoff["location_nodes"]
    assert (footprint["kind"], footprint["parent_id"], footprint["parent_name"], footprint["owner_id"]) == (
        "TopologyCustomerDC",
        "id-DC10",
        "DC10",
        "org-C005",
    )
    assert [device["deployment_name"] for device in handoff["end_devices"]] == ["DC10", "FR2"]
    # the carrier is derived from the ports and their parents, each circuit once; ends are sorted pairs
    assert handoff["carrier"] == ["DF-DC10-EQXFR2", "XC-EQXFR2-AWS-EUC1"]
    assert handoff["ends"] == [("bl-dc101101", "Ethernet1/35"), ("eg-fr201", "Ethernet1/13.1000")]
    assert [(c["circuit_id"], c["status"]) for c in handoff["carriers"]] == [
        ("DF-DC10-EQXFR2", "active"),
        ("XC-EQXFR2-AWS-EUC1", "active"),
    ]
    physical_end, virtual_end = handoff["end_nodes"]
    assert (physical_end["kind"], physical_end["address"], physical_end["cabled"], physical_end["parent_id"]) == (
        "DcimPhysicalInterface",
        "10.255.128.1/31",
        True,
        None,
    )
    assert (virtual_end["kind"], virtual_end["address"], virtual_end["cabled"]) == ("DcimVirtualInterface", None, False)
    assert (virtual_end["parent_id"], virtual_end["parent_name"]) == ("if-parent", "Ethernet1/13")
    assert [c["circuit_id"] for c in virtual_end["carriers"]] == ["XC-EQXFR2-AWS-EUC1"]
    assert (handoff["ip_namespace"], handoff["password"]) == (
        "PROD",
        "eqx-fr2-aws-vif-key",
    )
    assert (handoff["cloud_endpoint"], handoff["cloud_endpoint_vlan"]) == ("vif-1", 1000)


async def test_fetch_interconnect_inventory_leaves_the_handoff_fields_empty_on_the_other_kinds() -> None:
    """A tunnel or an interconnect has no namespace, limit, key or cloud endpoint."""
    tunnel = {
        "id": "vc2",
        "__typename": "TopologySessionCircuit",
        "circuit_type": {"value": "tunnel"},
        "name": {"value": "C001-OFFICE-SDWAN-FR2"},
        "owner": {"node": None},
        "locations": {"edges": []},
        "capabilities": {"edges": []},
    }
    client = MagicMock()
    client.execute_graphql = AsyncMock(
        return_value={"TopologyCarrierCircuit": {"edges": []}, "TopologySessionCircuit": {"edges": [{"node": tunnel}]}}
    )

    inventory = await fetch_interconnect_inventory(client=client, branch="main")

    (circuit,) = inventory["virtual"]
    assert circuit["circuit_type"] == "tunnel"
    assert (circuit["owner"], circuit["ends"], circuit["carrier"], circuit["end_nodes"]) == (None, [], [], [])
    assert (circuit["ip_namespace"], circuit["password"]) == (None, None)
    assert (circuit["cloud_endpoint"], circuit["cloud_endpoint_vlan"]) == (None, None)
