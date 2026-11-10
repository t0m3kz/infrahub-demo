"""Unit tests for the load-balancer inventory helpers of the integration scenarios."""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.integration.query_files import QUERY_GET_LOADBALANCER_INVENTORY, QUERY_GET_TRANSIT_INVENTORY
from tests.integration.service_inventory_helpers import (
    _context_legs_and_ports,
    fetch_loadbalancer_inventory,
    match_loadbalancer_contexts,
)
from tests.unit.encapsulation_helpers import raw_encapsulation
from tests.unit.integration_scenario_helpers_helpers import _no_propagation_delay  # noqa: F401 (autouse fixture)
from tests.unit.test_circuit_query_shapes import _selected_paths

# ---------------------------------------------------------------------------
# Load-balancer inventory (test_62, test_64)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "root", "owners"),
    [
        # A leg's VRF is the ManagedVRF among its capabilities (InterfaceRoutingZoneFields), next to the circuits' VRF.
        (
            "get_loadbalancer_inventory.gql",
            "ManagedLoadbalancerContext",
            {"shared_devices.interfaces.zone_capabilities", "shared_devices.interfaces.zone_capabilities.vrf"},
        ),
        (
            "get_transit_inventory.gql",
            "ManagedFirewallContext",
            {"shared_devices.interfaces.zone_capabilities", "shared_devices.interfaces.zone_capabilities.vrf"},
        ),
        ("get_transit_inventory.gql", "TopologyRoutedExchange", {"vrf_a", "vrf_z"}),
    ],
    ids=["loadbalancer", "transit-legs", "transit-exchange"],
)
def test_inventory_queries_select_routing_from_vrf_and_only_ipam_identity(
    root_dir: Path,
    filename: str,
    root: str,
    owners: set[str],
) -> None:
    """Inventory legs and exchange sides select exact VRF owners, never direct IPAM or zone routing."""
    paths = _selected_paths(root_dir / "tests/integration/queries" / filename, root, by_alias=True)
    for owner in owners:
        assert {path.removeprefix(f"{owner}.") for path in paths if path.startswith(f"{owner}.")} >= {
            "id",
            "deployment",
            "deployment.id",
            "deployment.name",
            "security_zone",
            "security_zone.id",
            "security_zone.name",
            "security_zone.vrf_type",
            "security_zone.l3_vni",
        }
    zone_paths = {path for path in paths if path.endswith("security_zone")}
    assert zone_paths == {f"{owner}.security_zone" for owner in owners}
    ipam_paths = {path for path in paths if ".ip_namespace." in path}
    expected_ipam = (
        set()
        if root == "TopologyRoutedExchange"
        else {
            "shared_devices.interfaces.ip_address.ip_namespace.name",
            "vip_prefix.ip_namespace.name"
            if root == "ManagedLoadbalancerContext"
            else "snat_address.ip_namespace.name",
        }
    )
    assert ipam_paths == expected_ipam
    assert not {"security_zone_a", "security_zone_z", "capabilities.security_zone"} & paths


def test_inventory_query_constants_include_transitive_owner_fragments(root_dir: Path) -> None:
    """The queries actually sent by the inventory helper include their shared routing definitions."""
    from utils.graphql_queries import load_query

    assert QUERY_GET_LOADBALANCER_INVENTORY == load_query(
        root_dir / "tests/integration/queries/get_loadbalancer_inventory.gql"
    )
    assert QUERY_GET_TRANSIT_INVENTORY == load_query(root_dir / "tests/integration/queries/get_transit_inventory.gql")


def _lb_context(
    name: str, *, tenant: str | None = None, dcs: tuple[str, ...] = (), namespaces: tuple[str, ...] = ("PROD",)
) -> dict:
    """One inventory context as fetch_loadbalancer_inventory returns it (only the keys the matcher reads)."""
    return {
        "name": name,
        "tenant": tenant,
        "cluster_dcs": list(dcs),
        "legs": [{"namespace": namespace} for namespace in namespaces],
    }


def test_match_loadbalancer_contexts_keys_by_dc_namespace_and_tenant() -> None:
    """A shared context is keyed by its cluster's DC and its legs' namespace, a dedicated one by its tenant's DC."""
    prod = _lb_context("lb-dc12-PROD", dcs=("DC12",))
    non_prod = _lb_context("lb-dc12-NONPROD", dcs=("DC12",), namespaces=("NONPROD", "NONPROD"))
    dedicated = _lb_context("lb-c005-context", tenant="C005-P-DC12")

    by_key, unkeyed = match_loadbalancer_contexts([prod, non_prod, dedicated], ("DC12",), ("C005-P-DC12",))

    assert by_key == {
        ("DC12", "PROD", None): prod,
        ("DC12", "NONPROD", None): non_prod,
        ("DC12", "PROD", "C005-P-DC12"): dedicated,
    }
    assert unkeyed == []


def test_match_loadbalancer_contexts_drops_other_suites_and_unkeys_the_ambiguous() -> None:
    """DC6's and an unlisted tenant's contexts are dropped; no DC, two namespaces or a duplicate key is unkeyed."""
    first = _lb_context("lb-dc10-PROD", dcs=("DC10",))
    duplicate = _lb_context("lb-dc10-PROD-again", dcs=("DC10",))
    two_vrfs = _lb_context("lb-dc10-both", dcs=("DC10",), namespaces=("PROD", "NONPROD"))
    no_leg = _lb_context("lb-dc10-unwired", dcs=("DC10",), namespaces=())
    no_dc = _lb_context("lb-orphan")
    contexts = [
        first,
        duplicate,
        two_vrfs,
        no_leg,
        no_dc,
        _lb_context("lb-dc6-PROD", dcs=("DC6",)),
        _lb_context("lb-c099-context", tenant="C099-P-DC10"),
    ]

    by_key, unkeyed = match_loadbalancer_contexts(contexts, ("DC10",), ("C007-P-DC10",))

    assert by_key == {("DC10", "PROD", None): first}
    assert unkeyed == [duplicate, two_vrfs, no_leg, no_dc]


@pytest.mark.parametrize(
    ("zone", "vrf_type", "l3_vni"),
    [("PROD", "prod", 60000), ("NONPROD", "non_prod", 60001), ("MANAGEMENT", "management", 60003)],
)
def test_raw_service_leg_resolves_zone_independently_of_ipam(zone: str, vrf_type: str, l3_vni: int) -> None:
    """Identical addresses in builtin default keep the leg's explicit routing authority (its VRF) after clean_data."""
    node = {
        "id": "ctx",
        "shared_devices": _raw_members("ManagedLoadbalancerContext", "ctx", 3100, _raw_vrf(zone, vrf_type, l3_vni)),
    }
    legs, ports = _context_legs_and_ports(node)
    assert ports == []
    assert (legs[0]["namespace"], legs[0]["vrf_type"]) == (zone, vrf_type)
    # A leg in no VRF has no routing zone, whatever its address says.
    legs, _ = _context_legs_and_ports(
        {"id": "ctx", "shared_devices": _raw_members("ManagedLoadbalancerContext", "ctx", 3100, None)}
    )
    assert (legs[0]["namespace"], legs[0]["vrf_type"]) == (None, None)


def _raw_vrf(zone: str, vrf_type: str, l3_vni: int) -> dict[str, Any]:
    """A raw ManagedVRF of DC10 in ``zone``."""
    return {
        "__typename": "ManagedVRF",
        "id": f"vrf-dc10-{zone}",
        "deployment": {"node": {"id": "dc10", "name": {"value": "DC10"}}},
        "security_zone": {
            "node": {
                "id": f"zone-{zone}",
                "name": {"value": zone},
                "vrf_type": {"value": vrf_type},
                "l3_vni": {"value": l3_vni},
            }
        },
    }


def _raw_members(
    kind: str, ctx_id: str, vlan: int, vrf: dict[str, Any] | None, *, far_port: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The raw members of a context: one load balancer whose uplink ``eth1`` (cabled to ``far_port``) carries the leg
    ``eth1.<vlan>``, addressed in default and in ``vrf`` (none: in no VRF)."""
    uplink = {
        "__typename": "DcimPhysicalInterface",
        "id": "up1",
        "name": {"value": "eth1"},
        "role": {"value": "uplink"},
        "cable": {"node": {"id": "c1", "endpoints": {"edges": [{"node": far_port}]}}} if far_port else {"node": None},
    }
    leg = {
        "__typename": "DcimVirtualInterface",
        "id": "sub1",
        "name": {"value": f"eth1.{vlan}"},
        "role": {"value": "service"},
        **raw_encapsulation(vlan, {"id": "up1", "name": {"value": "eth1"}}),
        "zone_capabilities": {"edges": [{"node": vrf}] if vrf else []},
        "ip_address": {
            "node": {"address": {"value": "100.66.64.5/29"}, "ip_namespace": {"node": {"name": {"value": "default"}}}}
        },
        "capabilities": {"edges": []},
    }
    member = {
        "__typename": "DcimPhysicalDevice",
        "id": "lb1",
        "name": {"value": "lb-01"},
        "role": {"value": "load-balancer"},
        "shared_capabilities": {"edges": [{"node": {"__typename": kind, "id": ctx_id, "vlan_id": {"value": vlan}}}]},
        "interfaces": {"edges": [{"node": uplink}, {"node": leg}]},
    }
    return {"edges": [{"node": member}]}


async def test_fetch_loadbalancer_inventory_parses_legs_ports_and_vips() -> None:
    """The leg sub-interfaces, the border-leaf load-balancer ports (with their role) and each VIP's
    address, servers, member addresses and health checks come out flat; a port on another role's device is ignored."""

    def value(v: Any) -> dict:
        return {"value": v}

    node = {
        "id": "ctx",
        "name": value("lb-dc10-PROD"),
        "vlan_id": value(3100),
        "tenant": {"node": None},
        "served_deployments": {
            "edges": [
                {"node": {"id": "d2", "name": value("C008-P-DC10")}},
                {"node": {"id": "d1", "name": value("C005-P-DC10")}},
            ]
        },
        "vip_prefix": {
            "node": {
                "prefix": value("100.66.96.0/26"),
                "ip_namespace": {"node": {"name": {"value": "default"}}},
            }
        },
        "shared_devices": _raw_members(
            "ManagedLoadbalancerContext",
            "ctx",
            3100,
            _raw_vrf("PROD", "prod", 60000),
            far_port={
                "__typename": "DcimPhysicalInterface",
                "id": "p1",
                "name": value("Ethernet1/20"),
                "role": value("load-balancer"),
                "device": {"node": {"id": "bl1", "name": value("bl-01"), "role": value("border-leaf")}},
            },
        ),
        "vips": {
            "edges": [
                {
                    "node": {
                        "hostname": value("api.example"),
                        "protocol": value("tcp"),
                        "port": value(443),
                        "vip_ip": {"node": {"address": value("100.66.96.10/32")}},
                        "pool_members": {
                            "edges": [
                                {
                                    "node": {
                                        "name": value("vm-2"),
                                        "primary_address": {"node": {"address": value("10.0.0.11/24")}},
                                    }
                                },
                                {
                                    "node": {
                                        "name": value("vm-1"),
                                        "primary_address": {"node": {"address": value("10.0.0.10/24")}},
                                    }
                                },
                            ]
                        },
                        "health_checks": {
                            "edges": [
                                {
                                    "node": {
                                        "check": value("tcp"),
                                        "rise": value(2),
                                        "fall": value(3),
                                        "timeout": value(1000),
                                    }
                                },
                                {
                                    "node": {
                                        "check": value("http"),
                                        "rise": value(3),
                                        "fall": value(3),
                                        "timeout": value(2000),
                                    }
                                },
                            ]
                        },
                    }
                }
            ]
        },
    }
    # A context holds no HA edge: its pair is the ManagedLoadbalancerHA among its member's own capabilities, and
    # its DC the member's deployment.
    (member,) = node["shared_devices"]["edges"]
    member["node"]["capabilities"] = {
        "edges": [
            {
                "node": {
                    "__typename": "ManagedLoadbalancerHA",
                    "id": "ha1",
                    "name": value("lb-dc10-ha"),
                    "capabilities": {"edges": [{"node": {"id": "lb1"}}]},
                }
            },
            {"node": {"__typename": "ManagedBGP"}},
        ]
    }
    member["node"]["deployment"] = {"node": {"__typename": "TopologyDataCenter", "name": value("DC10")}}
    client = MagicMock()
    client.execute_graphql = AsyncMock(return_value={"ManagedLoadbalancerContext": {"edges": [{"node": node}]}})

    (context,) = await fetch_loadbalancer_inventory(client=client, branch="main")

    assert context["cluster"] == "lb-dc10-ha"
    assert context["cluster_dcs"] == ["DC10"]
    assert context["cluster_member_ids"] == ["lb1"]
    assert context["served"] == ["C005-P-DC10", "C008-P-DC10"]
    assert (context["vip_prefix"], context["vip_prefix_namespace"]) == ("100.66.96.0/26", "default")
    assert [(leg["interface"], leg["address"], leg["namespace"]) for leg in context["legs"]] == [
        ("eth1.3100", "100.66.64.5/29", "PROD")
    ]
    assert [(port["id"], port["role"]) for port in context["border_ports"]] == [("p1", "load-balancer")]
    assert context["vips"] == [
        {
            "hostname": "api.example",
            "protocol": "tcp",
            "port": 443,
            "address": "100.66.96.10/32",
            "servers": ["vm-1", "vm-2"],
            "members": ["10.0.0.10/24", "10.0.0.11/24"],
            "health_checks": (("http", "3", "3", "2000"), ("tcp", "2", "3", "1000")),
        }
    ]
