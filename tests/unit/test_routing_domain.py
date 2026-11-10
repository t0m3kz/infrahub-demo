"""Native GraphQL ownership is independent of builtin default IPAM identity."""

from typing import Any

import pytest

from transforms.helpers.policy import rule_zone
from utils.data_cleaning import clean_data
from utils.routing_domain import RoutingDomainError, interface_security_zone, prefix_security_zone
from utils.segment_addressing import routed_segment_namespace
from utils.vrf_demand import address_namespaces


def test_prefix_only_policy_uses_standalone_registration_zone() -> None:
    """A selector-only rule obtains its domain from the selected registered prefix."""
    assert (
        rule_zone(
            {
                "destination_zone": {"name": "INTERNET"},
                "destination_prefixes": [{"prefix": "192.0.2.0/24"}],
            },
            "destination",
        )
        == "INTERNET"
    )


def test_prefix_only_policy_without_registration_zone_fails_closed() -> None:
    """The default IPAM identity is not a policy routing authority."""
    with pytest.raises(RoutingDomainError, match="missing security zone"):
        rule_zone(
            {"destination_prefixes": [{"prefix": "192.0.2.0/24", "ip_namespace": {"name": "default"}}]},
            "destination",
        )


def zone(name: str = "PROD", kind: str = "prod", vni: int = 60000) -> dict[str, Any]:
    """A native SecurityZone relationship."""
    return {"node": {"id": name, "name": {"value": name}, "vrf_type": {"value": kind}, "l3_vni": {"value": vni}}}


def in_vrf(**vrf: Any) -> dict[str, Any]:
    """The raw capability that puts an interface in a VRF: the ManagedVRF among its ``zone_capabilities``."""
    return {"zone_capabilities": {"edges": [{"node": {"__typename": "ManagedVRF", **vrf}}]}}


def iface(**owners: Any) -> dict[str, Any]:
    """A routed interface with a builtin-default IPAM address."""
    return clean_data(
        {
            "name": {"value": "Ethernet1.100"},
            "ip_address": {
                "node": {"address": {"value": "10.0.0.0/31"}, "ip_namespace": {"node": {"name": {"value": "default"}}}}
            },
            **owners,
        }
    )


@pytest.mark.parametrize("owner", ["direct", "circuit"])
def test_explicit_owners_demand_prod_with_default_ipam(owner: str) -> None:
    """Each native routing owner creates tenant demand without an IPAM-name dependency."""
    owners = {
        "direct": in_vrf(security_zone=zone()),
        "circuit": {
            **in_vrf(security_zone=zone()),
            "capabilities": {
                "edges": [
                    {
                        "node": {
                            "__typename": "TopologySessionCircuit",
                            "circuit_type": {"value": "handoff"},
                            "vrf": {"node": {"security_zone": zone()}},
                        }
                    }
                ]
            },
        },
    }
    port = iface(**owners[owner])
    assert address_namespaces([port]) == frozenset({"PROD"})
    assert interface_security_zone(port)["l3_vni"] == 60000


def test_underlay_address_has_no_managed_vrf_demand() -> None:
    """An unowned global/underlay interface is excluded, not put in a default managed VRF."""
    assert address_namespaces([iface()]) == frozenset()


def test_missing_handoff_domain_fails_closed() -> None:
    """A tenant circuit cannot silently become a global-table session."""
    port = iface(
        **in_vrf(id="vrf-1", name={"value": "dc1-vrf-prod"}),
        capabilities={
            "edges": [{"node": {"__typename": "TopologySessionCircuit", "circuit_type": {"value": "handoff"}}}]
        },
    )
    with pytest.raises(RoutingDomainError, match="missing security zone"):
        interface_security_zone(port)


def test_conflicting_native_owners_are_rejected() -> None:
    """Direct and SVI authority cannot disagree."""
    circuit = {
        "__typename": "TopologySessionCircuit",
        "circuit_type": {"value": "handoff"},
        "vrf": {"node": {"security_zone": zone("NONPROD", "non_prod", 60001)}},
    }
    port = iface(**in_vrf(security_zone=zone()), capabilities={"edges": [{"node": circuit}]})
    with pytest.raises(RoutingDomainError, match="conflicting security zones"):
        interface_security_zone(port)


def test_routed_segment_uses_zone_and_terminated_segment_has_no_fabric_demand() -> None:
    """A default-IPAM gateway demands its zone unless a firewall terminates it."""
    segment = clean_data(
        {"name": {"value": "web"}, "security_zone": zone(), "gateway": {"node": {"address": {"value": "10.0.0.1/24"}}}}
    )
    assert routed_segment_namespace(segment)["name"] == "PROD"
    segment["terminated_on"] = {"id": "firewall-context"}
    assert routed_segment_namespace(segment) == {}


def test_routed_segment_without_zone_is_rejected() -> None:
    """Gateway presence does not authorize an implicit routing domain."""
    with pytest.raises(RoutingDomainError, match="missing security zone"):
        routed_segment_namespace({"name": "web", "gateway": {"address": "10.0.0.1/24"}})


def test_standalone_prefix_requires_its_explicit_registry_zone() -> None:
    """A prefix-only registration supplies routing authority, not its IPAM namespace."""
    prefix = clean_data({"prefix": {"value": "198.51.100.0/24"}})
    owner = clean_data({"vrf": {"node": {"id": "vrf-inet", "security_zone": zone("INTERNET", "internet", 60002)}}})
    assert prefix_security_zone(prefix, owner)["name"] == "INTERNET"
    unzoned = clean_data({"vrf": {"node": {"id": "vrf-x", "name": {"value": "x"}}}})
    with pytest.raises(RoutingDomainError, match="missing security zone"):
        prefix_security_zone({"prefix": "198.51.100.0/24"}, unzoned)
