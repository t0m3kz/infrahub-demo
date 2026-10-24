"""Unit tests for the load-balancer inventory helpers of the integration scenarios."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from tests.integration.test_helpers import fetch_loadbalancer_inventory, match_loadbalancer_contexts
from tests.unit.integration_scenario_helpers_helpers import _no_propagation_delay  # noqa: F401 (autouse fixture)

# ---------------------------------------------------------------------------
# Load-balancer inventory (test_62, test_64)
# ---------------------------------------------------------------------------


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
    non_prod = _lb_context("lb-dc12-NON-PROD", dcs=("DC12",), namespaces=("NON-PROD", "NON-PROD"))
    dedicated = _lb_context("lb-c005-context", tenant="C005-P-DC12")

    by_key, unkeyed = match_loadbalancer_contexts([prod, non_prod, dedicated], ("DC12",), ("C005-P-DC12",))

    assert by_key == {
        ("DC12", "PROD", None): prod,
        ("DC12", "NON-PROD", None): non_prod,
        ("DC12", "PROD", "C005-P-DC12"): dedicated,
    }
    assert unkeyed == []


def test_match_loadbalancer_contexts_drops_other_suites_and_unkeys_the_ambiguous() -> None:
    """DC6's and an unlisted tenant's contexts are dropped; no DC, two namespaces or a duplicate key is unkeyed."""
    first = _lb_context("lb-dc10-PROD", dcs=("DC10",))
    duplicate = _lb_context("lb-dc10-PROD-again", dcs=("DC10",))
    two_vrfs = _lb_context("lb-dc10-both", dcs=("DC10",), namespaces=("PROD", "NON-PROD"))
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


async def test_fetch_loadbalancer_inventory_parses_legs_ports_and_vips() -> None:
    """The leg sub-interfaces, the border-leaf load-balancer ports (with their role) and each VIP's
    address, members, component and segment prefix come out flat; a port on another role's device is ignored."""

    def value(v: Any) -> dict:
        return {"value": v}

    node = {
        "id": "ctx",
        "name": value("lb-dc10-PROD"),
        "vlan_id": value(3100),
        "tenant": {"node": None},
        "cluster": {
            "node": {
                "name": value("lb-dc10-ha"),
                "capabilities": {
                    "edges": [
                        {
                            "node": {
                                "id": "lb1",
                                "deployment": {"node": {"__typename": "TopologyDataCenter", "name": value("DC10")}},
                            }
                        }
                    ]
                },
            }
        },
        "served_deployments": {
            "edges": [
                {"node": {"id": "d2", "name": value("C008-P-DC10")}},
                {"node": {"id": "d1", "name": value("C005-P-DC10")}},
            ]
        },
        "vip_prefix": {"node": {"prefix": value("100.66.96.0/26"), "ip_namespace": {"node": {"name": value("PROD")}}}},
        "capabilities": {
            "edges": [
                {
                    "node": {
                        "__typename": "DcimVirtualInterface",
                        "id": "sub1",
                        "name": value("eth1.3100"),
                        "device": {"node": {"id": "lb1", "name": value("lb-01"), "role": value("load-balancer")}},
                        "ip_address": {
                            "node": {
                                "address": value("100.66.64.5/29"),
                                "ip_namespace": {"node": {"name": value("PROD"), "namespace_type": value("prod")}},
                            }
                        },
                    }
                },
                {
                    "node": {
                        "__typename": "DcimPhysicalInterface",
                        "id": "p1",
                        "name": value("Ethernet1/20"),
                        "role": value("load-balancer"),
                        "device": {"node": {"id": "bl1", "name": value("bl-01"), "role": value("border-leaf")}},
                    }
                },
                {
                    "node": {
                        "__typename": "DcimPhysicalInterface",
                        "id": "p2",
                        "name": value("eth1"),
                        "role": value("uplink"),
                        "device": {"node": {"id": "lb1", "name": value("lb-01"), "role": value("load-balancer")}},
                    }
                },
            ]
        },
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
                                {"node": {"address": value("10.0.0.11/24")}},
                                {"node": {"address": value("10.0.0.10/24")}},
                            ]
                        },
                        "component": {
                            "node": {
                                "fqdn": value("api.example"),
                                "network_segment": {
                                    "node": {
                                        "name": value("web-p"),
                                        "gateway": {"node": {"ip_prefix": {"node": {"prefix": value("10.0.0.0/24")}}}},
                                    }
                                },
                            }
                        },
                    }
                }
            ]
        },
    }
    client = MagicMock()
    client.execute_graphql = AsyncMock(return_value={"ManagedLoadbalancerContext": {"edges": [{"node": node}]}})

    (context,) = await fetch_loadbalancer_inventory(client=client, branch="main")

    assert context["cluster_dcs"] == ["DC10"]
    assert context["cluster_member_ids"] == ["lb1"]
    assert context["served"] == ["C005-P-DC10", "C008-P-DC10"]
    assert (context["vip_prefix"], context["vip_prefix_namespace"]) == ("100.66.96.0/26", "PROD")
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
            "members": ["10.0.0.10/24", "10.0.0.11/24"],
            "component": "api.example",
            "segment": "web-p",
            "segment_prefix": "10.0.0.0/24",
        }
    ]
