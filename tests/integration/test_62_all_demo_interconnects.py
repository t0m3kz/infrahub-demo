"""Integration test — the 30_all interconnect and tenant-service layer.

Everything here is the part of the model that leaves a single site: the dark
fibre and cross-connects into the colocation on-ramps, the virtual circuits
riding them into AWS and Azure, the SD-WAN branches' internet underlay, and
the cloud-side terminations that close the on-prem-to-cloud path.

Two edges get special attention because the schema deliberately has exactly one
way to express each of them, and a regression would silently split the graph
into islands:

  * ``capabilities`` — how a circuit surfaces on the port it
    terminates on. It is the single uniform "what is this port doing" edge, and
    replaced the bespoke ``interfaces`` relationship the node used to declare.
  * ``cloud_endpoints`` — how a circuit reaches its cloud side, which is a
    CloudVirtualInterface, not a device port. Without it the traversal walks
    from a border leaf out to the cage and then stops at an island boundary.

Circuit placement is checked from both sides: a physical circuit ends on two
facilities where its end devices sit (the end the data leaves out is the one
add_circuit derived), an owned virtual circuit on its owner's footprint, and
every location's reverse ``circuits`` lists exactly the circuits naming it.

Also covered: the tenant-scoped firewall and load-balancer contexts (shared vs
dedicated, driven by the customer design blueprint), the VIPs of the
load_balanced components with their R_server/R_client rules, and the segment
deployment legs, where a stretch segment must carry one leg per DC.

Runs against the branch test_59 builds; it never loads data of its own.
"""

import ipaddress
import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient

from generators.helpers.naming import sdwan_edge_name
from generators.helpers.pools import (
    FW_CONTEXT_VLAN_END,
    FW_CONTEXT_VLAN_START,
    LB_CONTEXT_VLAN_END,
    LB_CONTEXT_VLAN_START,
)
from utils.data_cleaning import clean_data
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    LB_VIP_PREFIXLEN,
    TRANSIT_OFFSETS,
    transit_addresses,
    transit_vlan,
    wan_exit_vlan,
)

from .conftest import TestInfrahubDockerWithClient
from .test_constants import (
    ALL_DEMO_BORDER_FIREWALL_PORTS,
    ALL_DEMO_BRANCH,
    ALL_DEMO_CLOUD_LOAD_BALANCED_COMPONENTS,
    ALL_DEMO_COLOCATION_SERVED,
    ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS,
    ALL_DEMO_DC_NAMES,
    ALL_DEMO_DCI_CIRCUITS,
    ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS,
    ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS,
    ALL_DEMO_DEDICATED_FIREWALL_TENANTS,
    ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS,
    ALL_DEMO_EDGE_INTERLINK_INTERFACE,
    ALL_DEMO_EDGE_INTERLINKS,
    ALL_DEMO_EDGE_PAIR_POOL_TEMPLATE,
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    ALL_DEMO_FABRIC_PEERINGS,
    ALL_DEMO_FACILITY_KINDS,
    ALL_DEMO_FIREWALL_MEMBERS,
    ALL_DEMO_FOOTPRINT_KINDS,
    ALL_DEMO_INTERNET_BGWS,
    ALL_DEMO_INTERNET_TRANSIT_CIRCUITS,
    ALL_DEMO_INTERNET_TRANSIT_PEERINGS,
    ALL_DEMO_INTERNET_UPSTREAM_BY_DC,
    ALL_DEMO_ISP_ACCESS_DEVICE,
    ALL_DEMO_ISP_ACCESS_LOCATION,
    ALL_DEMO_ISP_ACCESS_PORTS,
    ALL_DEMO_LB_CLIENT_DEPENDENCIES,
    ALL_DEMO_LB_LEG_RESOURCES,
    ALL_DEMO_LB_VIP_RESOURCES,
    ALL_DEMO_LEGACY_EXCHANGES,
    ALL_DEMO_LOAD_BALANCED_COMPONENTS,
    ALL_DEMO_LOADBALANCER_CONTEXTS,
    ALL_DEMO_LOADBALANCER_MEMBERS,
    ALL_DEMO_METRO_SITE_VIPS,
    ALL_DEMO_OFFICE_WAN_EXITS,
    ALL_DEMO_PHYSICAL_CIRCUIT_TYPES,
    ALL_DEMO_PROVIDER_FACILITY_KINDS,
    ALL_DEMO_SDWAN_EDGE_UPLINK,
    ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS,
    ALL_DEMO_SEGMENT_LEGS,
    ALL_DEMO_SHARED_FIREWALL_CONTEXTS,
    ALL_DEMO_SHARED_LOADBALANCER_CONTEXTS,
    ALL_DEMO_SNAT_ADDRESSES,
    ALL_DEMO_SNAT_POOL_PREFIX,
    ALL_DEMO_TENANT_LOCATION_KINDS,
    ALL_DEMO_TRANSIT_EXCHANGES,
    ALL_DEMO_TRANSIT_LEGS,
    ALL_DEMO_TRANSIT_PREFIXES,
    ALL_DEMO_TRANSIT_RESOURCES,
    ALL_DEMO_TRANSIT_SUBINTERFACES,
    ALL_DEMO_VIRTUAL_CIRCUIT_CHAINS,
    ALL_DEMO_VIRTUAL_CIRCUITS,
    OFFICE_NAMESPACE,
    deployment_environment,
    deployment_namespace,
    transit_namespace_type,
)
from .test_helpers import (
    fetch_interconnect_inventory,
    fetch_loadbalancer_inventory,
    fetch_tenant_services,
    fetch_transit_inventory,
    match_loadbalancer_contexts,
    match_transit_contexts,
    scope_tenant_services_to_dcs,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: Interconnects"

COLOCATION_CONTEXTS_QUERY = """
query {
  TopologyCustomerColocation {
    edges {
      node {
        name { value }
        parent {
          node {
            ... on TopologyColocationMetro {
              devices(role__value: "firewall") { edges { node { id } } }
            }
          }
        }
        serving_firewall_context {
          node {
            name { value }
            vlan_id { value }
            tenant { node { id } }
            cluster { node { ... on ManagedFirewallHA { capabilities { edges { node { id } } } } } }
            capabilities { edges { node { name { value } } } }
          }
        }
      }
    }
  }
}
"""

# The DCI sessions add_circuit builds for the peering_role=dci dark fibres.
DCI_SESSIONS_QUERY = """
query ($names: [String]) {
  ManagedBGPPeering(name__values: $names) {
    edges {
      node {
        name { value }
        peering_role { value }
        session_type { value }
        ttl { value }
        password { node { name { value } } }
        address_families { edges { node { afi { value } safi { value } } } }
        bgp_processes { edges { node { name { value } process_role { value } } } }
        capabilities {
          edges {
            node {
              name { value }
              device { node { name { value } } }
              ... on DcimPhysicalInterface { ip_address { node { address { value } } } }
            }
          }
        }
      }
    }
  }
}
"""

# The interlink ports of the metro edge pairs: role and /127 address.
EDGE_INTERLINK_INTERFACES_QUERY = """
query ($devices: [String], $name: String) {
  DcimPhysicalInterface(device__name__values: $devices, name__value: $name) {
    edges {
      node {
        id
        name { value }
        role { value }
        device { node { name { value } } }
        ip_address { node { address { value } } }
      }
    }
  }
}
"""

# Every cable touching the given interfaces.
EDGE_INTERLINK_CABLES_QUERY = """
query ($ids: [ID]) {
  DcimCable(endpoints__ids: $ids) {
    edges {
      node {
        id
        endpoints { edges { node { id } } }
      }
    }
  }
}
"""

# The office WAN exit sessions add_office_fabric builds toward the SD-WAN edge (the VCO describes the edge's side
# from the same peering): processes with their AS, the key, and the two dot1q sub-interfaces with their addresses.
WAN_EXIT_SESSIONS_QUERY = """
query ($names: [String]) {
  ManagedBGPPeering(name__values: $names) {
    edges {
      node {
        name { value }
        peering_role { value }
        session_type { value }
        ttl { value }
        password { node { name { value } } }
        address_families { edges { node { afi { value } safi { value } } } }
        bgp_processes {
          edges { node { name { value } process_role { value } local_as { node { asn { value } } } } }
        }
        capabilities {
          edges {
            node {
              name { value }
              device { node { name { value } } }
              ... on DcimVirtualInterface {
                parent_interface { node { name { value } } }
                ip_address { node { address { value } ip_namespace { node { name { value } } } } }
              }
            }
          }
        }
      }
    }
  }
}
"""

# The metro-owned slice of Technical-IPv6 the interlink /127 comes from.
EDGE_PAIR_POOL_QUERY = """
query ($name: String) {
  CoreIPPrefixPool(name__value: $name) {
    edges {
      node {
        name { value }
        resources { edges { node { ... on BuiltinIPPrefix { prefix { value } } } } }
      }
    }
  }
}
"""

# The loopback100 Multi-Site VIP of both edges of a metro: one shared address object.
EDGE_PAIR_VIP_QUERY = """
query ($devices: [String]) {
  DcimVirtualInterface(device__name__values: $devices, name__value: "loopback100") {
    edges {
      node {
        role { value }
        device { node { name { value } } }
        ip_address { node { id address { value } } }
      }
    }
  }
}
"""

# The ISP transit sessions: which edge each runs on, and the namespace of its local address.
TRANSIT_SESSIONS_QUERY = """
query ($names: [String]) {
  ManagedBGPPeering(name__values: $names) {
    edges {
      node {
        name { value }
        session_type { value }
        capabilities {
          edges {
            node {
              name { value }
              device { node { name { value } } }
              ... on DcimPhysicalInterface {
                ip_address { node { address { value } ip_namespace { node { name { value } namespace_type { value } } } } }
              }
            }
          }
        }
      }
    }
  }
}
"""
# A rule's match: its policy, segments, the VIP/32s it targets (R_client) and the prefixes it starts
# from (R_server: the load-balancer context's leg).
_RULE_FIELDS = """
        name { value }
        action { value }
        protocol { value }
        port_start { value }
        disabled { value }
        policy { node { name { value } } }
        source_segment { node { name { value } } }
        destination_segment { node { name { value } } }
        destination_ip_addresses { edges { node { address { value } } } }
        source_prefixes { edges { node { prefix { value } } } }
"""
RULES_BY_POLICY_QUERY = (
    "query ($names: [String]) { SecurityPolicyRule(policy__name__values: $names) { edges { node {"
    + _RULE_FIELDS
    + "} } } }"
)
RULES_BY_SOURCE_SEGMENT_QUERY = (
    "query ($names: [String]) { SecurityPolicyRule(source_segment__name__values: $names) { edges { node {"
    + _RULE_FIELDS
    + "} } } }"
)
# The cloud load balancers of the cloud load_balanced components, and their VIPs.
CLOUD_LOAD_BALANCERS_QUERY = """
query ($names: [String]) {
  CloudLoadBalancer(name__values: $names) {
    edges {
      node {
        name { value }
        lb_type { value }
        scheme { value }
        virtual_network { node { name { value } } }
        network_segments { edges { node { name { value } } } }
        member_of_groups { edges { node { name { value } } } }
      }
    }
  }
  LoadbalancerVIP(load_balancer__name__values: $names) {
    edges {
      node {
        hostname { value }
        protocol { value }
        port { value }
        load_balancer { node { name { value } } }
        vip_ip { node { address { value } } }
        component { node { fqdn { value } } }
        pool_members { edges { node { address { value } } } }
      }
    }
  }
}
"""

# Every location with its reverse `circuits`: the same circuit__location edge as a circuit's `locations`.
CIRCUIT_LOCATIONS_QUERY = """
query {
  TopologyConnectableLocation {
    edges {
      node {
        id
        __typename
        name { value }
        circuits { edges { node { id __typename } } }
      }
    }
  }
}
"""


def _rules(result: dict) -> list[dict]:
    """The SecurityPolicyRule nodes of a RULES_BY_* result, flattened."""

    def peer_name(node: dict, relationship: str) -> str | None:
        return (((node.get(relationship) or {}).get("node") or {}).get("name") or {}).get("value")

    return [
        {
            "name": node["name"]["value"],
            "action": node["action"]["value"],
            "protocol": node["protocol"]["value"],
            "port": node["port_start"]["value"],
            "disabled": node["disabled"]["value"],
            "policy": peer_name(node, "policy"),
            "source_segment": peer_name(node, "source_segment"),
            "destination_segment": peer_name(node, "destination_segment"),
            "destination_ips": {
                ipaddress.IPv4Interface(e["node"]["address"]["value"])
                for e in node["destination_ip_addresses"]["edges"]
            },
            "source_prefixes": {
                ipaddress.IPv4Network(e["node"]["prefix"]["value"]) for e in node["source_prefixes"]["edges"]
            },
        }
        for node in (edge["node"] for edge in result["SecurityPolicyRule"]["edges"])
    ]


# DCI-Technical-IPv6 (data/bootstrap/20_dci_pools.yml) — the DC fabrics run an IPv6 underlay.
DCI_POOL_NETWORK = ipaddress.IPv6Network("fd00:2200::/40")


_TRANSIT_PEER_NAMES = {"internet": "INTERNET", "management": "MANAGEMENT"}


_ZONE_KIND = "TopologyColocationZone"
_OFFICE_KIND = "TopologyCustomerOffice"


def _label(location: dict[str, Any]) -> str:
    """`FR2 (TopologyColocationZone)`."""
    return f"{location['name']} ({location['kind']})"


def _at(location: dict[str, Any], place_id: str | None) -> bool:
    """Whether a location is the place ``place_id``, or a cage of it (a zone whose parent is that metro).

    The place is where a device is deployed: a border leaf in its DC, a provider router in its region, an on-ramp
    edge in its METRO, of which the circuit names one cage.
    """
    if not place_id:
        return False
    return location["id"] == place_id or (location["kind"] == _ZONE_KIND and location["parent_id"] == place_id)


def _place(location: dict[str, Any]) -> tuple[str | None, str | None]:
    """(id, name) of the facility a location stands for: a footprint's parent (DC, metro, region), else itself."""
    if location["kind"] in ALL_DEMO_FOOTPRINT_KINDS:
        return location["parent_id"], location["parent_name"]
    return location["id"], location["name"]


def _chain_joins(hops: list[list[dict[str, Any]]], near: dict[str, Any], far: dict[str, Any], internet: bool) -> bool:
    """Whether an underlay (each hop's locations, in order) starts at ``near``'s facility and ends at ``far``'s.

    An internet-backed overlay's underlay ends at an ISP instead: the far end is reached across the internet.
    """
    starts = any(_at(location, _place(near)[0]) for location in hops[0])
    if internet:
        return starts and any(location["kind"] == "TopologyExternalProviders" for location in hops[-1])
    return starts and any(_at(location, _place(far)[0]) for location in hops[-1])


def _deployments(circuit: dict[str, Any]) -> list[str]:
    """`bl-dc101101 in DC10 (TopologyDataCenter)` for each end device of a circuit."""
    return [
        f"{device['device']} in {device['deployment_name']} ({device['deployment_kind']})"
        for device in circuit["end_devices"]
    ]


def _exchange_ends(exchange_name: str, context_name: str) -> tuple[str, ...]:
    """The namespaces `{context}-{A}-{Z}` joins; namespace names may contain '-' (NON-PROD), so match known ones."""
    rest = exchange_name.removeprefix(f"{context_name}-")
    return tuple(
        ns for ns in ALL_DEMO_TRANSIT_RESOURCES if rest == ns or rest.startswith(f"{ns}-") or rest.endswith(f"-{ns}")
    )


class TestAllDemoInterconnects(TestInfrahubDockerWithClient):
    """Verify the circuit layer and the tenant-scoped services on top of it."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    # ------------------------------------------------------------------
    # Physical layer
    # ------------------------------------------------------------------

    @pytest.mark.order(394)
    @pytest.mark.dependency(scope="session", name="all_demo_physical_circuits", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_01_verify_physical_circuits(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the physical underlay: type mix, provider, placement and two distinct ends.

        Every circuit is point-to-point, so exactly two locations and exactly
        two distinct ports (its capabilities, min and max count 2) are
        hard invariants, and a port is an end of at most one circuit — the
        SD-WAN offices' INET-<office> circuits, created concurrently, are where a
        shared access port would show. Each location is a facility, and each
        is where one end's device is deployed (_placement_errors); the ends the
        data leaves out are the ones add_circuit derived (_derived_location_errors).
        """
        logging.info("=== %s - Step 1: Physical Circuits ===", SCENARIO_NAME)

        inventory = await fetch_interconnect_inventory(client=async_client_main, branch=scenario_branch)
        physical = inventory["physical"]

        errors: list[str] = []

        by_type: dict[str, int] = {}
        for circuit in physical:
            by_type[circuit["circuit_type"]] = by_type.get(circuit["circuit_type"], 0) + 1

        for circuit_type, expected in sorted(ALL_DEMO_PHYSICAL_CIRCUIT_TYPES.items()):
            actual = by_type.get(circuit_type, 0)
            if actual != expected:
                errors.append(f"circuit_type '{circuit_type}': {actual} circuit(s), expected {expected}")
        unexpected = sorted(set(by_type) - set(ALL_DEMO_PHYSICAL_CIRCUIT_TYPES))
        if unexpected:
            errors.append(f"unexpected circuit type(s): {unexpected}")

        for circuit in sorted(physical, key=lambda c: c["circuit_id"]):
            circuit_id = circuit["circuit_id"]
            if not circuit["provider"]:
                errors.append(f"{circuit_id}: no provider")
            # The shared circuits (dark fibre, cross-connects, ISP transit) are
            # deliberately unowned; only a customer's own internet underlay
            # names an owner.
            transit = circuit_id in ALL_DEMO_INTERNET_TRANSIT_CIRCUITS
            if circuit["circuit_type"] == "internet" and not transit and not circuit["owner"]:
                errors.append(f"{circuit_id}: internet underlay with no owning customer")
            if transit and circuit["owner"]:
                errors.append(f"{circuit_id}: shared transit circuit owned by '{circuit['owner']}'")
            if circuit["circuit_type"] in ("dark_fiber", "cross_connect") and circuit["owner"]:
                errors.append(f"{circuit_id}: shared {circuit['circuit_type']} circuit owned by '{circuit['owner']}'")

        errors.extend(self._port_errors(physical))
        errors.extend(self._placement_errors(physical))
        errors.extend(self._derived_location_errors(physical))
        errors.extend(self._sdwan_underlay_errors(physical))

        assert not errors, f"30_all physical circuits are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Physical circuits verified: %d (%s)", len(physical), dict(sorted(by_type.items())))

    @staticmethod
    def _port_errors(physical: list[dict]) -> list[str]:
        """Every circuit has two distinct ends, and no port is an end of two circuits."""
        errors: list[str] = []
        circuit_of_port: dict[tuple[str, str], str] = {}
        for circuit in sorted(physical, key=lambda c: c["circuit_id"]):
            circuit_id = circuit["circuit_id"]
            if len(circuit["end_ids"]) != 2 or len(set(circuit["end_ids"])) != 2:
                errors.append(f"{circuit_id}: ends {circuit['ends']}, expected exactly 2 distinct ports")
            for end in circuit["ends"]:
                if end in circuit_of_port:
                    errors.append(f"{end} is an end of both {circuit_of_port[end]} and {circuit_id}")
                circuit_of_port[end] = circuit_id
        return errors

    @staticmethod
    def _placement_errors(physical: list[dict]) -> list[str]:
        """Two distinct facilities, each the deployment of one end's device (a cage: a zone of its metro).

        A facility is a TopologyFacility kind (DC, colocation zone, cloud region,
        ISP, SaaS region, office); a pod, a metro or a footprint is never one, and an
        office only ends its own internet access. The two ends' devices must account for
        the two locations one-to-one: the border leaf's DC, the PE's ISP, the provider
        router's region, and for the on-ramp edge (deployed to the metro) one cage of
        that metro.
        """
        errors: list[str] = []
        for circuit in sorted(physical, key=lambda c: c["circuit_id"]):
            circuit_id = circuit["circuit_id"]
            locations = circuit["location_nodes"]
            if len({location["id"] for location in locations}) != 2:
                errors.append(
                    f"{circuit_id}: terminates at {circuit['locations']}, expected exactly 2 distinct locations"
                )
                continue
            for location in locations:
                if location["kind"] not in ALL_DEMO_FACILITY_KINDS:
                    errors.append(
                        f"{circuit_id}: ends at {_label(location)}, not a facility {sorted(ALL_DEMO_FACILITY_KINDS)}"
                    )
                elif location["kind"] == _OFFICE_KIND and circuit["circuit_type"] != "internet":
                    errors.append(
                        f"{circuit_id}: a {circuit['circuit_type']} circuit ends at office {location['name']}"
                    )
            devices = circuit["end_devices"]
            if len(devices) != 2 or any(not device["deployment_id"] for device in devices):
                errors.append(f"{circuit_id}: end devices {_deployments(circuit)}, expected 2, each with a deployment")
                continue
            (first, second), (left, right) = locations, devices
            if not (
                (_at(first, left["deployment_id"]) and _at(second, right["deployment_id"]))
                or (_at(first, right["deployment_id"]) and _at(second, left["deployment_id"]))
            ):
                errors.append(
                    f"{circuit_id}: locations {[_label(location) for location in locations]} are not where its ends "
                    f"sit {_deployments(circuit)} (each end's deployment, or a cage of the edge's metro)"
                )
        return errors

    @staticmethod
    def _derived_location_errors(physical: list[dict]) -> list[str]:
        """The data authors only what is not derivable; add_circuit wrote exactly the derived ends.

        Derivable: an end device's deployment that is itself a facility (DC, ISP,
        cloud region). The 30_all data must not list it, and the locations the branch
        holds beyond the data's are exactly those. Circuits no data file declares
        (INET-<office>, add_sdwan_edge) are written whole by their generator.
        """
        errors: list[str] = []
        by_id = {circuit["circuit_id"]: circuit for circuit in physical}
        for circuit_id, declared in sorted(ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS.items()):
            circuit = by_id.get(circuit_id)
            if circuit is None:
                errors.append(f"{circuit_id}: declared in 30_all but not on the branch")
                continue
            derivable = {
                str(device["deployment_name"])
                for device in circuit["end_devices"]
                if device["deployment_kind"] in ALL_DEMO_FACILITY_KINDS
            }
            listed = sorted(set(declared) & derivable)
            if listed:
                errors.append(f"{circuit_id}: the data lists {listed}, which add_circuit derives from the end devices")
            missing = sorted(set(declared) - set(circuit["locations"]))
            if missing:
                errors.append(f"{circuit_id}: declared location(s) {missing} are not on the branch")
            written = set(circuit["locations"]) - set(declared)
            if written != derivable - set(declared):
                errors.append(
                    f"{circuit_id}: the generator wrote {sorted(written)} beyond the data's {sorted(declared)}, "
                    f"expected the end devices' facility deployments {sorted(derivable - set(declared))}"
                )
        return errors

    @staticmethod
    def _sdwan_underlay_errors(physical: list[dict]) -> list[str]:
        """INET-<office>: the office edge's uplink to its own access-PE port, in the office and the ISP.

        The offices board in one load and add_sdwan_edge picks the first free
        port of the access PE under a lock, so the five circuits together
        hold exactly xe-0/1/[0-4].
        """
        errors: list[str] = []
        by_id = {circuit["circuit_id"]: circuit for circuit in physical}
        isp_ports: list[str] = []
        for circuit_id, office in sorted(ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS.items()):
            circuit = by_id.get(circuit_id)
            if circuit is None:
                errors.append(f"{circuit_id}: SD-WAN internet underlay of {office} was not created")
                continue
            if (sdwan_edge_name(office), ALL_DEMO_SDWAN_EDGE_UPLINK) not in circuit["ends"]:
                errors.append(
                    f"{circuit_id}: ends {circuit['ends']}, expected {sdwan_edge_name(office)} {ALL_DEMO_SDWAN_EDGE_UPLINK}"
                )
            isp_ends = [port for device, port in circuit["ends"] if device == ALL_DEMO_ISP_ACCESS_DEVICE]
            if len(isp_ends) != 1:
                errors.append(f"{circuit_id}: {len(isp_ends)} end(s) on {ALL_DEMO_ISP_ACCESS_DEVICE}, expected 1")
            isp_ports.extend(isp_ends)
            expected_locations = sorted([office, ALL_DEMO_ISP_ACCESS_LOCATION])
            if circuit["locations"] != expected_locations:
                errors.append(f"{circuit_id}: locations {circuit['locations']}, expected {expected_locations}")
        if set(isp_ports) != ALL_DEMO_ISP_ACCESS_PORTS or len(isp_ports) != len(ALL_DEMO_ISP_ACCESS_PORTS):
            errors.append(
                f"{ALL_DEMO_ISP_ACCESS_DEVICE} ports {sorted(isp_ports)}, "
                f"expected one circuit on each of {sorted(ALL_DEMO_ISP_ACCESS_PORTS)}"
            )
        return errors

    # ------------------------------------------------------------------
    # Virtual layer
    # ------------------------------------------------------------------

    @pytest.mark.order(395)
    @pytest.mark.dependency(scope="session", name="all_demo_virtual_circuits", depends=["all_demo_physical_circuits"])
    @pytest.mark.asyncio
    async def test_02_verify_virtual_circuits(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify each overlay circuit's underlay, ports, cloud termination and placement (_virtual_placement_errors)."""
        logging.info("=== %s - Step 2: Virtual Circuits ===", SCENARIO_NAME)

        inventory = await fetch_interconnect_inventory(client=async_client_main, branch=scenario_branch)
        by_name = {circuit["name"]: circuit for circuit in inventory["virtual"]}

        errors: list[str] = []

        missing = sorted(set(ALL_DEMO_VIRTUAL_CIRCUITS) - set(by_name))
        if missing:
            errors.append(f"virtual circuit(s) missing: {missing}")
        unexpected = sorted(set(by_name) - set(ALL_DEMO_VIRTUAL_CIRCUITS))
        if unexpected:
            errors.append(f"unexpected virtual circuit(s): {unexpected}")

        for name, expected in sorted(ALL_DEMO_VIRTUAL_CIRCUITS.items()):
            circuit = by_name.get(name)
            if circuit is None:
                continue

            if circuit["link_type"] != expected["link_type"]:
                errors.append(f"{name}: link_type '{circuit['link_type']}', expected '{expected['link_type']}'")
            if circuit["transport_mode"] != expected["transport_mode"]:
                errors.append(
                    f"{name}: transport_mode '{circuit['transport_mode']}', expected '{expected['transport_mode']}'"
                )
            if circuit["owner"] != expected["owner"]:
                errors.append(f"{name}: owner '{circuit['owner']}', expected '{expected['owner']}'")

            expected_physical = sorted(expected["physical_circuits"])
            if circuit["physical_circuits"] != expected_physical:
                errors.append(
                    f"{name}: underlay {circuit['physical_circuits']}, expected {expected_physical} — "
                    "physical_circuits is the only mapping from overlay to underlay"
                )

            if len(circuit["interfaces"]) != expected["interfaces"]:
                errors.append(
                    f"{name}: surfaces on {len(circuit['interfaces'])} interface(s) via "
                    f"capabilities, expected {expected['interfaces']}"
                )

            expected_cloud = sorted(expected["cloud_endpoints"])
            if circuit["cloud_endpoints"] != expected_cloud:
                errors.append(f"{name}: cloud_endpoints {circuit['cloud_endpoints']}, expected {expected_cloud}")

        errors.extend(self._virtual_placement_errors(inventory["virtual"], inventory["physical"]))

        assert not errors, f"30_all virtual circuits are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Virtual circuits verified: %d", len(by_name))

    @staticmethod
    def _virtual_placement_errors(virtual: list[dict], physical: list[dict]) -> list[str]:
        """Where each overlay ends, and that its ordered underlay joins those two ends.

        Owned: one end is a footprint (or the office) of the circuit's owner, whose
        facility (a footprint's parent: its DC, metro or region; the office itself)
        is where one of the circuit's end devices is deployed; the far end is a
        provider facility (cloud/SaaS region, cage) or another footprint. Unowned
        (a platform overlay): facilities at both ends. Nothing on a virtual circuit
        is derived, so the branch holds exactly the declared locations.

        The underlay (ALL_DEMO_VIRTUAL_CIRCUIT_CHAINS, tenant end first) is
        contiguous: each hop shares a location with the next, the first hop reaches
        the tenant end's facility (a metro through one of its cages) and the last the
        far end. An internet-backed overlay's last hop ends at an ISP instead: past it,
        the far end is reached across the internet.
        """
        errors: list[str] = []
        physical_by_id = {circuit["circuit_id"]: circuit for circuit in physical}
        for circuit in sorted(virtual, key=lambda c: c["name"]):
            name = circuit["name"]
            locations = circuit["location_nodes"]
            if name in ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS and circuit["locations"] != sorted(
                ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS[name]
            ):
                errors.append(
                    f"{name}: locations {circuit['locations']}, declared "
                    f"{sorted(ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS[name])} (a virtual circuit derives none)"
                )
            if len({location["id"] for location in locations}) != 2:
                errors.append(f"{name}: terminates at {circuit['locations']}, expected exactly 2 distinct locations")
                continue

            if circuit["owner_id"]:
                tenant_ends = [
                    location
                    for location in locations
                    if location["kind"] in ALL_DEMO_TENANT_LOCATION_KINDS
                    and location["owner_id"] == circuit["owner_id"]
                ]
                if not tenant_ends:
                    errors.append(
                        f"{name}: owned by {circuit['owner']}, but neither end {[_label(loc) for loc in locations]} "
                        "is a footprint or office of that owner"
                    )
                    continue
                near = tenant_ends[0]
                far = next(location for location in locations if location["id"] != near["id"])
                if far["kind"] not in ALL_DEMO_PROVIDER_FACILITY_KINDS | ALL_DEMO_TENANT_LOCATION_KINDS:
                    errors.append(f"{name}: far end {_label(far)} is neither a provider facility nor a footprint")
                place_id, place_name = _place(near)
                if not place_id or place_id not in {device["deployment_id"] for device in circuit["end_devices"]}:
                    errors.append(
                        f"{name}: tenant end {_label(near)} sits in {place_name}, where none of its end devices "
                        f"{_deployments(circuit)} is deployed"
                    )
                orientations = [(near, far)]
            else:
                strays = [_label(location) for location in locations if location["kind"] not in ALL_DEMO_FACILITY_KINDS]
                if strays:
                    errors.append(f"{name}: unowned, so both ends must be facilities, but {strays} is not")
                orientations = [(locations[0], locations[1]), (locations[1], locations[0])]

            chain = ALL_DEMO_VIRTUAL_CIRCUIT_CHAINS.get(name, ())
            missing = [circuit_id for circuit_id in chain if circuit_id not in physical_by_id]
            if not chain or missing:
                errors.append(f"{name}: underlay {list(chain)} is empty or {missing} are not on the branch")
                continue
            hops = [physical_by_id[circuit_id]["location_nodes"] for circuit_id in chain]
            for index in range(1, len(hops)):
                if not {location["id"] for location in hops[index - 1]} & {location["id"] for location in hops[index]}:
                    errors.append(f"{name}: underlay hops {chain[index - 1]} and {chain[index]} share no location")
            internet = circuit["transport_mode"] == "internet_backed"
            if not any(_chain_joins(hops, near, far, internet) for near, far in orientations):
                errors.append(
                    f"{name}: underlay {list(chain)} does not run from {_label(orientations[0][0])} to "
                    f"{'an ISP' if internet else _label(orientations[0][1])}"
                )
        return errors

    @pytest.mark.order(395)
    @pytest.mark.dependency(scope="session", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_02b_verify_location_circuits(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify every location's `circuits` lists exactly the circuits that name it, physical and virtual.

        `circuits` is the reverse of a circuit's `locations` (one identifier,
        circuit__location), so the two directions must agree for every location on
        the branch: a circuit missing from its location's list, or a location listing
        a circuit that does not name it, is a schema or generator write that took
        only one side.
        """
        logging.info("=== %s - Step 2b: Location circuits ===", SCENARIO_NAME)

        inventory = await fetch_interconnect_inventory(client=async_client_main, branch=scenario_branch)
        result = await async_client_main.execute_graphql(query=CIRCUIT_LOCATIONS_QUERY, branch_name=scenario_branch)
        cleaned = clean_data(result)

        names: dict[str, str] = {}
        forward: dict[str, set[str]] = {}  # location id -> the circuits naming it
        for circuit in [*inventory["physical"], *inventory["virtual"]]:
            names[circuit["id"]] = circuit.get("circuit_id") or circuit["name"]
            for location in circuit["location_nodes"]:
                forward.setdefault(location["id"], set()).add(circuit["id"])
        reverse: dict[str, set[str]] = {}  # location id -> its `circuits`
        location_names: dict[str, str] = {}
        for location in cleaned.get("TopologyConnectableLocation") or []:
            location_names[str(location["id"])] = f"{location['name']} ({location['typename']})"
            reverse[str(location["id"])] = {str(circuit["id"]) for circuit in location.get("circuits") or []}

        errors: list[str] = []
        for location_id in sorted(set(forward) | set(reverse), key=lambda i: location_names.get(i, i)):
            named_by, listed = forward.get(location_id, set()), reverse.get(location_id, set())
            if named_by != listed:
                label = location_names.get(location_id, location_id)
                errors.append(
                    f"{label}: `circuits` lacks {sorted(names.get(c, c) for c in named_by - listed)} and lists "
                    f"{sorted(names.get(c, c) for c in listed - named_by)} that do not name it"
                )
        if not forward:
            errors.append("no circuit names any location")

        assert not errors, f"30_all location circuits are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Location circuits verified: %d location(s) with circuits", len(forward))

    # ------------------------------------------------------------------
    # Tenant-scoped services
    # ------------------------------------------------------------------

    @pytest.mark.order(396)
    @pytest.mark.dependency(scope="session", name="all_demo_tenant_services", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_03_verify_firewall_contexts(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the firewall contexts match the customers' design blueprints.

        A context with no tenant is a cluster's shared low-risk context (each
        DC's, and the colocation metro's); a context with a tenant exists only
        because that customer's design sets dedicated_firewall. Getting this wrong is how a customer silently ends
        up sharing a security context with everyone else.

        Scoped to the 30_all DCs (and every colocation): the DC6 scenario
        chain merges its own customer footprint into main in the same session.
        """
        logging.info("=== %s - Step 3: Firewall Contexts ===", SCENARIO_NAME)

        services = scope_tenant_services_to_dcs(
            await fetch_tenant_services(client=async_client_main, branch=scenario_branch), ALL_DEMO_DC_NAMES
        )
        contexts = services["firewall_contexts"]

        errors: list[str] = []

        shared = [context for context in contexts if not context["tenant"]]
        if len(shared) != ALL_DEMO_SHARED_FIREWALL_CONTEXTS:
            errors.append(
                f"{len(shared)} shared (tenant-less) firewall context(s), "
                f"expected {ALL_DEMO_SHARED_FIREWALL_CONTEXTS} — one per firewall cluster"
            )

        dedicated = {str(context["tenant"]): context for context in contexts if context["tenant"]}
        missing = sorted(set(ALL_DEMO_DEDICATED_FIREWALL_TENANTS) - set(dedicated))
        if missing:
            errors.append(f"customer(s) whose design demands a dedicated firewall context but have none: {missing}")
        unexpected = sorted(set(dedicated) - set(ALL_DEMO_DEDICATED_FIREWALL_TENANTS))
        if unexpected:
            errors.append(
                f"dedicated firewall context(s) for customer(s) whose design does not ask for one: {unexpected}"
            )

        for tenant, design in sorted(ALL_DEMO_DEDICATED_FIREWALL_TENANTS.items()):
            context = dedicated.get(tenant)
            if context is None:
                continue
            if context["tenant_design"] != design:
                errors.append(f"{tenant}: design '{context['tenant_design']}', expected '{design}'")
            if not context["cluster"]:
                errors.append(f"{tenant}: dedicated context is not attached to a firewall cluster")

        assert not errors, f"30_all firewall contexts are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Firewall contexts verified: %d shared, %d dedicated", len(shared), len(dedicated))

    @pytest.mark.order(397)
    @pytest.mark.dependency(scope="session", name="all_demo_exchange_transit", depends=["all_demo_tenant_services"])
    @pytest.mark.asyncio
    async def test_03b_verify_exchange_transit_legs(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify each firewall context's inter-VRF legs, exchanges and service ports.

        A leg is (context, namespace): every firewall member's VLAN
        sub-interface in that VRF, addressed from a /29 of the VRF's transit
        pool and tagged with the context and with the leg's exchange(s). The
        expectations come from the customers' environments and designs
        (ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS), and the served deployments the
        graph itself reports must agree with them. The FR colocation metro's
        shared context is held to the same rules as a DC's: its legs face the
        metro's two edges, whose firewall-role ports are tagged with it, and its
        VLAN comes from the metro's FirewallContext pool (3000-3099).
        """
        logging.info("=== %s - Step 3b: Exchange Transit Legs ===", SCENARIO_NAME)

        inventory = await fetch_transit_inventory(client=async_client_main, branch=scenario_branch)
        by_key, colocation = match_transit_contexts(
            inventory["contexts"],
            ALL_DEMO_DC_NAMES,
            ALL_DEMO_DEDICATED_FIREWALL_TENANTS,
            ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS,
        )
        exchanges_by_gateway: dict[str, list[dict]] = {}
        for exchange in inventory["exchanges"]:
            exchanges_by_gateway.setdefault(str(exchange["gateway_id"]), []).append(exchange)

        errors: list[str] = []
        leg_count = 0
        subinterface_count = 0
        prefixes: dict[str, set[ipaddress.IPv4Network]] = {}
        exchange_count = 0
        ports_by_site: dict[str, dict[str, set[str]]] = {}  # site -> service port id -> context names
        port_devices_by_site: dict[str, set[str]] = {}  # site -> devices holding the tagged ports

        for expected in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS:
            label = f"{expected['site']}/{expected['tenant'] or 'shared'}"
            context = by_key.get((expected["site"], expected["tenant"]))
            if context is None:
                errors.append(f"{label}: no firewall context")
                continue
            context_name = str(context["name"])

            # The graph's own view of who the context serves must give the same VRFs.
            served_names = sorted(str(d["name"]) for d in context["served"])
            if served_names != sorted(expected["deployments"]):
                errors.append(f"{label}: serves {served_names}, expected {sorted(expected['deployments'])}")
            served_namespaces = {deployment_namespace(name) for name in served_names}
            served_namespaces |= {
                _TRANSIT_PEER_NAMES[peer]
                for name in served_names
                for peer in EXCHANGE_PEERS[transit_namespace_type(deployment_namespace(name))]
            }
            if sorted(served_namespaces) != sorted(expected["namespaces"]):
                errors.append(
                    f"{label}: served deployments {served_names} need VRFs {sorted(served_namespaces)}, "
                    f"expected {sorted(expected['namespaces'])}"
                )
            for served in context["served"]:
                if str(served["environment"]) != deployment_environment(str(served["name"])):
                    errors.append(f"{label}: {served['name']} has environment '{served['environment']}'")

            # Legs: one sub-interface per member per VRF.
            context_vlan = context["vlan"]
            if not context_vlan:
                errors.append(f"{label}: '{context_name}' has no VLAN")
                continue
            if expected["site"] in ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS and not (
                FW_CONTEXT_VLAN_START <= int(context_vlan) <= FW_CONTEXT_VLAN_END
            ):
                errors.append(
                    f"{label}: VLAN {context_vlan} is outside the metro pool {FW_CONTEXT_VLAN_START}-{FW_CONTEXT_VLAN_END}"
                )
            legs_by_namespace: dict[str, list[dict]] = {}
            for leg in context["legs"]:
                legs_by_namespace.setdefault(str(leg["namespace"]), []).append(leg)
            if sorted(legs_by_namespace) != sorted(expected["namespaces"]):
                errors.append(
                    f"{label}: legs in {sorted(legs_by_namespace)}, expected {sorted(expected['namespaces'])}"
                )
            leg_count += len(legs_by_namespace)
            expected_exchange_names = {f"{context_name}-{suffix}" for suffix in expected["exchanges"]}

            for namespace, legs in sorted(legs_by_namespace.items()):
                if namespace not in ALL_DEMO_TRANSIT_RESOURCES:
                    errors.append(f"{label}: leg in unexpected namespace '{namespace}'")
                    continue
                ns_type = transit_namespace_type(namespace)
                subinterface_count += len(legs)
                if len(legs) != ALL_DEMO_FIREWALL_MEMBERS or len({leg["device_id"] for leg in legs}) != len(legs):
                    errors.append(f"{label}/{namespace}: {len(legs)} sub-interface(s), one per member expected")
                networks = set()
                for leg in legs:
                    leg_id = f"{label}/{namespace} {leg['device']}:{leg['interface']}"
                    if not leg["address"]:
                        errors.append(f"{leg_id}: no address")
                        continue
                    address = ipaddress.IPv4Interface(leg["address"])
                    networks.add(address.network)
                    if address.network.prefixlen != 29:
                        errors.append(f"{leg_id}: {leg['address']} is not in a /29")
                    elif not address.network.subnet_of(ipaddress.IPv4Network(ALL_DEMO_TRANSIT_RESOURCES[namespace])):
                        errors.append(
                            f"{leg_id}: {leg['address']} is outside the {namespace} transit pool "
                            f"{ALL_DEMO_TRANSIT_RESOURCES[namespace]}"
                        )
                    elif address.ip not in (address.network[o] for o in TRANSIT_OFFSETS[2:]):
                        errors.append(f"{leg_id}: {leg['address']} is not a member offset (.5/.6) of its /29")
                    if leg["namespace_type"] != ns_type:
                        errors.append(f"{leg_id}: namespace_type '{leg['namespace_type']}', expected '{ns_type}'")
                    sub_vlan = str(leg["interface"]).rsplit(".", 1)[-1]
                    if sub_vlan != str(transit_vlan(int(context_vlan), ns_type)):
                        errors.append(
                            f"{leg_id}: VLAN {sub_vlan}, expected transit_vlan({context_vlan}, {ns_type}) = "
                            f"{transit_vlan(int(context_vlan), ns_type)}"
                        )
                    wanted = sorted(
                        name for name in expected_exchange_names if namespace in _exchange_ends(name, context_name)
                    )
                    if leg["exchanges"] != wanted:
                        errors.append(f"{leg_id}: linked to exchange(s) {leg['exchanges']}, expected {wanted}")
                if len(networks) != 1:
                    errors.append(f"{label}/{namespace}: members are in {sorted(map(str, networks))}, one /29 expected")
                prefixes.setdefault(namespace, set()).update(networks)

            # Exchanges: `{context}-{A}-{Z}`, gateway = this context, never PROD <-> NON-PROD.
            actual_exchanges = exchanges_by_gateway.get(str(context["id"]), [])
            exchange_count += len(actual_exchanges)
            if sorted(str(e["name"]) for e in actual_exchanges) != sorted(expected_exchange_names):
                errors.append(
                    f"{label}: exchanges {sorted(str(e['name']) for e in actual_exchanges)}, "
                    f"expected {sorted(expected_exchange_names)}"
                )
            for exchange in actual_exchanges:
                if exchange["name"] != f"{context_name}-{exchange['namespace_a']}-{exchange['namespace_z']}":
                    errors.append(
                        f"{exchange['name']}: not named {{context}}-{{A}}-{{Z}} "
                        f"({context_name}-{exchange['namespace_a']}-{exchange['namespace_z']})"
                    )
                if exchange["namespace_z_type"] not in EXCHANGE_PEERS.get(str(exchange["namespace_a_type"]), ()):
                    errors.append(
                        f"{exchange['name']}: {exchange['namespace_a_type']} -> {exchange['namespace_z_type']} "
                        f"is not an allowed pairing {EXCHANGE_PEERS}"
                    )

            # The service ports the members' uplinks are cabled to (border leafs of a DC, edges of a metro).
            if len(context["border_ports"]) != ALL_DEMO_FIREWALL_MEMBERS:
                errors.append(
                    f"{label}: '{context_name}' tags {len(context['border_ports'])} service port(s), "
                    f"expected {ALL_DEMO_FIREWALL_MEMBERS}"
                )
            for port in context["border_ports"]:
                ports_by_site.setdefault(expected["site"], {}).setdefault(str(port["id"]), set()).add(context_name)
                port_devices_by_site.setdefault(expected["site"], set()).add(str(port["device"]))

        # Exchanges whose gateway is not a context of the 30_all DCs: only the
        # DC6 chain's own may exist; the legacy bootstrap ones must not.
        for exchange in inventory["exchanges"]:
            if not exchange["gateway_id"]:
                errors.append(f"{exchange['name']}: exchange without a gateway context")
        legacy = sorted(str(e["name"]) for e in inventory["exchanges"] if e["name"] in ALL_DEMO_LEGACY_EXCHANGES)
        if legacy:
            errors.append(f"legacy bootstrap exchange(s) still present: {legacy}")

        # Totals, all derived from the customers' environments in test_constants.
        if leg_count != ALL_DEMO_TRANSIT_LEGS:
            errors.append(f"{leg_count} leg(s), expected {ALL_DEMO_TRANSIT_LEGS}")
        if subinterface_count != ALL_DEMO_TRANSIT_SUBINTERFACES:
            errors.append(f"{subinterface_count} leg sub-interface(s), expected {ALL_DEMO_TRANSIT_SUBINTERFACES}")
        if exchange_count != ALL_DEMO_TRANSIT_EXCHANGES:
            errors.append(f"{exchange_count} exchange(s) on the contexts, expected {ALL_DEMO_TRANSIT_EXCHANGES}")
        for namespace, expected_count in ALL_DEMO_TRANSIT_PREFIXES.items():
            if len(prefixes.get(namespace, set())) != expected_count:
                errors.append(
                    f"{namespace}: {len(prefixes.get(namespace, set()))} transit /29(s), expected {expected_count}"
                )
        port_contexts = {port: names for ports in ports_by_site.values() for port, names in ports.items()}
        if len(port_contexts) != ALL_DEMO_BORDER_FIREWALL_PORTS:
            errors.append(f"{len(port_contexts)} tagged service port(s), expected {ALL_DEMO_BORDER_FIREWALL_PORTS}")
        for site, ports in sorted(ports_by_site.items()):
            contexts_in_site = sum(1 for e in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS if e["site"] == site)
            for port, names in sorted(ports.items()):
                if len(names) != contexts_in_site:
                    errors.append(
                        f"{site}: service port {port} carries {sorted(names)}, expected {contexts_in_site} context(s)"
                    )
        # A metro's two ports sit on its two DIFFERENT edges (one per firewall member), not both on one.
        for site in ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS:
            devices = port_devices_by_site.get(site, set())
            if len(devices) != ALL_DEMO_FIREWALL_MEMBERS:
                errors.append(f"{site}: service ports tagged on {sorted(devices)}, expected one per edge")

        # Every colocation context must be a served metro's: no stray one without legs (policy-only) is left.
        if colocation:
            errors.append(f"unexpected colocation firewall context(s) {sorted(str(c['name']) for c in colocation)}")

        assert not errors, f"30_all exchange transit legs are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info(
            "Exchange transit verified: %d leg(s), %d exchange(s), %d service port(s)",
            leg_count,
            exchange_count,
            len(port_contexts),
        )

    @pytest.mark.order(397)
    @pytest.mark.dependency(scope="session", name="all_demo_snat", depends=["all_demo_exchange_transit"])
    @pytest.mark.asyncio
    async def test_03c_verify_snat_addresses(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Each context with an INTERNET leg owns exactly one public SNAT /32.

        The address lives in the INTERNET namespace, inside the FW-SNAT-INTERNET-IPv4
        block (ALL_DEMO_SNAT_POOL_PREFIX), is a /32 and is distinct per context, the
        FR colocation metro's included (it must not reuse a DC's address).
        """
        logging.info("=== %s - Step 3c: SNAT addresses ===", SCENARIO_NAME)

        inventory = await fetch_transit_inventory(client=async_client_main, branch=scenario_branch)
        by_key, _ = match_transit_contexts(
            inventory["contexts"],
            ALL_DEMO_DC_NAMES,
            ALL_DEMO_DEDICATED_FIREWALL_TENANTS,
            ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS,
        )
        pool = ipaddress.IPv4Network(ALL_DEMO_SNAT_POOL_PREFIX)

        errors: list[str] = []
        seen: dict[str, str] = {}
        for expected in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS:
            label = f"{expected['site']}/{expected['tenant'] or 'shared'}"
            context = by_key.get((expected["site"], expected["tenant"]))
            if context is None:
                errors.append(f"{label}: no firewall context")
                continue
            snat = context["snat"]
            if not snat:
                errors.append(f"{label}: '{context['name']}' has an INTERNET leg but no snat_address")
                continue
            interface = ipaddress.IPv4Interface(str(snat))
            if interface.network.prefixlen != 32:
                errors.append(f"{label}: snat_address {snat} is not a /32")
            if interface.ip not in pool:
                errors.append(f"{label}: snat_address {snat} is outside {ALL_DEMO_SNAT_POOL_PREFIX}")
            if context["snat_namespace"] != "INTERNET":
                errors.append(f"{label}: snat_address {snat} is in namespace '{context['snat_namespace']}'")
            if str(interface.ip) in seen:
                errors.append(f"{label}: snat_address {snat} is also the one of {seen[str(interface.ip)]}")
            seen[str(interface.ip)] = label

        if len(seen) != ALL_DEMO_SNAT_ADDRESSES:
            errors.append(f"{len(seen)} distinct SNAT address(es), expected {ALL_DEMO_SNAT_ADDRESSES}")

        assert not errors, f"30_all SNAT addresses are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("SNAT verified: %d address(es) in %s", len(seen), ALL_DEMO_SNAT_POOL_PREFIX)

    @pytest.mark.order(397)
    @pytest.mark.dependency(scope="session", name="all_demo_loadbalancer_contexts", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_03d_verify_loadbalancer_contexts(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the load-balancer contexts, their one-arm leg, service ports and VIP range.

        One shared context per (DC cluster, tenant namespace) a non-dedicated
        footprint of the DC is in, `{cluster}-{NAMESPACE}`, and one dedicated
        `{cluster}-context` per footprint whose design sets
        dedicated_loadbalancer (ALL_DEMO_LOADBALANCER_CONTEXTS), each serving
        exactly those footprints. Per context: a VLAN from the DC's
        lb-context pool (3100-3199), unique in the DC; one leg in the tenant VRF,
        a /29 of LB-Leg-<NS> with the members at .5/.6 in name order on
        `<uplink>.<transit_vlan>` sub-interfaces of the cluster's members; both
        members' border-leaf `load-balancer` ports tagged with it; a /26 VIP
        range of LB-VIP-<NS> in the same namespace. Scoped to the 30_all DCs.
        """
        logging.info("=== %s - Step 3d: Load-balancer contexts ===", SCENARIO_NAME)

        contexts = await fetch_loadbalancer_inventory(client=async_client_main, branch=scenario_branch)
        by_key, unkeyed = match_loadbalancer_contexts(
            contexts, ALL_DEMO_DC_NAMES, ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS
        )

        errors: list[str] = []
        shared = sorted(key for key in by_key if key[2] is None)
        if len(shared) != ALL_DEMO_SHARED_LOADBALANCER_CONTEXTS:
            errors.append(
                f"{len(shared)} shared load-balancer context(s) {shared}, expected "
                f"{ALL_DEMO_SHARED_LOADBALANCER_CONTEXTS} — one per (DC cluster, tenant namespace) with a footprint"
            )
        dedicated = sorted(str(key[2]) for key in by_key if key[2] is not None)
        if dedicated != sorted(ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS):
            errors.append(
                f"dedicated load-balancer contexts for {dedicated}, expected the dedicated_loadbalancer footprints "
                f"{sorted(ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS)}"
            )
        unexpected = sorted(str(key) for key in set(by_key) - set(ALL_DEMO_LOADBALANCER_CONTEXTS))
        if unexpected:
            errors.append(f"unexpected load-balancer context(s) {unexpected}")
        if unkeyed:
            errors.append(
                f"load-balancer context(s) with no DC, no single leg namespace or a duplicate key: "
                f"{sorted(str(c['name']) for c in unkeyed)}"
            )

        vlans_by_dc: dict[str, dict[int, str]] = {}
        ports_by_dc: dict[str, dict[str, set[str]]] = {}  # dc -> tagged port id -> context names
        leg_networks: dict[ipaddress.IPv4Network, str] = {}
        vip_ranges: dict[ipaddress.IPv4Network, str] = {}
        for key, served in ALL_DEMO_LOADBALANCER_CONTEXTS.items():
            dc, namespace, tenant = key
            label = f"{dc}/{namespace}/{tenant or 'shared'}"
            context = by_key.get(key)
            if context is None:
                errors.append(f"{label}: no load-balancer context")
                continue
            name = str(context["name"])
            if not context["cluster"]:
                errors.append(f"{label}: '{name}' is on no load-balancer cluster")
            elif name != (f"{context['cluster']}-context" if tenant else f"{context['cluster']}-{namespace}"):
                errors.append(f"{label}: named '{name}', expected the {'dedicated' if tenant else 'shared'} name")
            if context["served"] != sorted(served):
                errors.append(f"{label}: '{name}' serves {context['served']}, expected {sorted(served)}")

            vlan = context["vlan"]
            if not vlan or not LB_CONTEXT_VLAN_START <= int(vlan) <= LB_CONTEXT_VLAN_END:
                errors.append(f"{label}: VLAN {vlan} outside {LB_CONTEXT_VLAN_START}-{LB_CONTEXT_VLAN_END}")
            elif int(vlan) in vlans_by_dc.setdefault(dc, {}):
                errors.append(f"{label}: VLAN {vlan} is also the one of {vlans_by_dc[dc][int(vlan)]} in {dc}")
            if vlan:
                vlans_by_dc.setdefault(dc, {})[int(vlan)] = name

            # The one leg: a sub-interface per member, in the tenant VRF only.
            ns_type = transit_namespace_type(namespace)
            legs = sorted(context["legs"], key=lambda leg: str(leg["device"]))
            if len(legs) != ALL_DEMO_LOADBALANCER_MEMBERS or len({leg["device_id"] for leg in legs}) != len(legs):
                errors.append(f"{label}: {len(legs)} leg sub-interface(s), one per member expected")
            strangers = sorted(
                str(leg["device"]) for leg in legs if leg["device_id"] not in context["cluster_member_ids"]
            )
            if strangers:
                errors.append(f"{label}: leg sub-interface(s) on {strangers}, not members of '{context['cluster']}'")
            networks = {ipaddress.IPv4Interface(leg["address"]).network for leg in legs if leg["address"]}
            if len(networks) != 1 or len([leg for leg in legs if leg["address"]]) != len(legs):
                errors.append(
                    f"{label}: leg addresses in {sorted(map(str, networks))}, one /29 for every member expected"
                )
            else:
                (network,) = networks
                if network.prefixlen != 29 or not network.subnet_of(
                    ipaddress.IPv4Network(ALL_DEMO_LB_LEG_RESOURCES[namespace])
                ):
                    errors.append(
                        f"{label}: leg {network} is not a /29 of {ALL_DEMO_LB_LEG_RESOURCES[namespace]} "
                        f"(LB-Leg-{namespace}-IPv4)"
                    )
                if network in leg_networks:
                    errors.append(f"{label}: leg {network} is also the leg of {leg_networks[network]}")
                leg_networks[network] = name
                members = transit_addresses(str(network))["members"]
                for leg, expected_ip in zip(legs, members, strict=False):
                    if str(ipaddress.IPv4Interface(leg["address"]).ip) != expected_ip:
                        errors.append(
                            f"{label}: {leg['device']} holds {leg['address']}, its member address by name order "
                            f"is {expected_ip}"
                        )
            for leg in legs:
                if leg["namespace_type"] != ns_type:
                    errors.append(
                        f"{label}: {leg['device']}:{leg['interface']} in namespace type {leg['namespace_type']}"
                    )
                if vlan and str(leg["interface"]).rsplit(".", 1)[-1] != str(transit_vlan(int(vlan), ns_type)):
                    errors.append(
                        f"{label}: {leg['device']}:{leg['interface']}, expected the leg VLAN "
                        f"transit_vlan({vlan}, {ns_type}) = {transit_vlan(int(vlan), ns_type)}"
                    )

            # The border leafs' load-balancer ports the members' uplinks are cabled to.
            if len(context["border_ports"]) != ALL_DEMO_LOADBALANCER_MEMBERS:
                errors.append(
                    f"{label}: '{name}' tags {len(context['border_ports'])} border port(s), "
                    f"expected {ALL_DEMO_LOADBALANCER_MEMBERS}"
                )
            for port in context["border_ports"]:
                if port["role"] != "load-balancer":
                    errors.append(f"{label}: tags {port['device']}:{port['interface']} of role '{port['role']}'")
                ports_by_dc.setdefault(dc, {}).setdefault(str(port["id"]), set()).add(name)

            # The routed VIP range.
            if not context["vip_prefix"]:
                errors.append(f"{label}: '{name}' has no VIP prefix")
                continue
            vip_range = ipaddress.IPv4Network(context["vip_prefix"])
            if vip_range.prefixlen != LB_VIP_PREFIXLEN or not vip_range.subnet_of(
                ipaddress.IPv4Network(ALL_DEMO_LB_VIP_RESOURCES[namespace])
            ):
                errors.append(
                    f"{label}: VIP prefix {vip_range} is not a /{LB_VIP_PREFIXLEN} of {ALL_DEMO_LB_VIP_RESOURCES[namespace]}"
                )
            if context["vip_prefix_namespace"] != namespace:
                errors.append(f"{label}: VIP prefix {vip_range} is in namespace '{context['vip_prefix_namespace']}'")
            if vip_range in vip_ranges:
                errors.append(f"{label}: VIP prefix {vip_range} is also the one of {vip_ranges[vip_range]}")
            vip_ranges[vip_range] = name

        # Every context of a DC is tagged on the same two load-balancer ports.
        for dc, ports in sorted(ports_by_dc.items()):
            expected_names = sum(1 for key in ALL_DEMO_LOADBALANCER_CONTEXTS if key[0] == dc)
            if len(ports) != ALL_DEMO_LOADBALANCER_MEMBERS:
                errors.append(f"{dc}: load-balancer contexts tagged on {len(ports)} port(s), expected 2")
            for port, names in sorted(ports.items()):
                if len(names) != expected_names:
                    errors.append(f"{dc}: load-balancer port {port} carries {sorted(names)}, expected {expected_names}")

        assert not errors, f"30_all load-balancer contexts are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Load-balancer contexts verified: %d shared, %d dedicated", len(shared), len(dedicated))

    @pytest.mark.order(397)
    @pytest.mark.dependency(
        scope="session", name="all_demo_loadbalancer_vips", depends=["all_demo_loadbalancer_contexts"]
    )
    @pytest.mark.asyncio
    async def test_03e_verify_loadbalancer_vips_and_rules(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the VIPs of the load_balanced components and the rules around them.

        Exactly one LoadbalancerVIP per (load_balanced component, port, context
        serving its instances' deployment) on the whole branch
        (ALL_DEMO_LOAD_BALANCED_COMPONENTS), its vip_ip a /32 inside the
        context's VIP range, fronting the component, with at least one pool
        member inside the component's segment prefix. R_server: the context's
        policy lb-<context> permits the leg /29 to the component's segment on
        the port. R_client (strict load balancing): a dependency into the
        component (ALL_DEMO_LB_CLIENT_DEPENDENCIES) is ruled from its source
        segment to the VIP/32 on the port, and never directly to the
        component's segment on that port. A component on cloud instances
        (ALL_DEMO_CLOUD_LOAD_BALANCED_COMPONENTS) has one CloudLoadBalancer in
        its instances' VPC instead, its VIPs without an address.
        """
        logging.info("=== %s - Step 3e: Load-balancer VIPs and rules ===", SCENARIO_NAME)

        contexts = await fetch_loadbalancer_inventory(client=async_client_main, branch=scenario_branch)
        by_key, _ = match_loadbalancer_contexts(contexts, ALL_DEMO_DC_NAMES, ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS)
        key_by_name = {str(context["name"]): key for key, context in by_key.items()}

        errors: list[str] = []
        expected = {
            (fqdn, protocol, port, key)
            for fqdn, component in ALL_DEMO_LOAD_BALANCED_COMPONENTS.items()
            for protocol, port in component["ports"]
            for key in component["contexts"]
        }
        actual: dict[tuple[str, str, int, Any], dict[str, Any]] = {}
        for context in contexts:
            for vip in context["vips"]:
                vip_key = (
                    str(vip["hostname"]),
                    str(vip["protocol"]),
                    int(vip["port"]),
                    key_by_name.get(str(context["name"]), str(context["name"])),
                )
                if vip_key in actual:
                    errors.append(f"VIP {vip_key} exists twice")
                actual[vip_key] = {**vip, "context": context}
        missing = sorted(map(str, expected - set(actual)))
        if missing:
            errors.append(f"VIP(s) missing: {missing}")
        extra = sorted(map(str, set(actual) - expected))
        if extra:
            errors.append(f"unexpected VIP(s): {extra}")

        vip_addresses: dict[tuple[str, str, int, Any], ipaddress.IPv4Interface] = {}
        for vip_key in sorted(expected & set(actual), key=repr):
            fqdn, _protocol, _port, _key = vip_key
            vip, context = actual[vip_key], actual[vip_key]["context"]
            label = f"VIP {fqdn} {vip['protocol']}/{vip['port']} on '{context['name']}'"
            component = ALL_DEMO_LOAD_BALANCED_COMPONENTS[fqdn]
            if vip["component"] != fqdn or vip["segment"] != component["segment"]:
                errors.append(
                    f"{label}: fronts {vip['component']} in {vip['segment']}, expected {fqdn} in {component['segment']}"
                )
            if not vip["address"]:
                errors.append(f"{label}: no vip_ip")
                continue
            address = ipaddress.IPv4Interface(vip["address"])
            vip_addresses[vip_key] = address
            if address.network.prefixlen != 32:
                errors.append(f"{label}: vip_ip {address} is not a /32")
            if not context["vip_prefix"] or address.ip not in ipaddress.IPv4Network(context["vip_prefix"]):
                errors.append(f"{label}: vip_ip {address} is outside the context's VIP range {context['vip_prefix']}")
            if not vip["members"]:
                errors.append(f"{label}: no pool member")
            elif not vip["segment_prefix"] or any(
                ipaddress.IPv4Interface(member).ip not in ipaddress.IPv4Network(vip["segment_prefix"])
                for member in vip["members"]
            ):
                errors.append(f"{label}: pool members {vip['members']} are not all in {vip['segment_prefix']}")

        # R_server: in lb-<context>, the leg /29 -> the component's segment on the VIP port.
        lb_policies = sorted({f"lb-{actual[vip_key]['context']['name']}" for vip_key in expected & set(actual)})
        result = await async_client_main.execute_graphql(
            query=RULES_BY_POLICY_QUERY, variables={"names": lb_policies}, branch_name=scenario_branch
        )
        lb_rules = _rules(result)
        for vip_key in sorted(expected & set(actual), key=repr):
            fqdn, protocol, port, _key = vip_key
            context = actual[vip_key]["context"]
            legs = {ipaddress.IPv4Interface(leg["address"]).network for leg in context["legs"] if leg["address"]}
            policy = f"lb-{context['name']}"
            matches = [
                rule
                for rule in lb_rules
                if rule["policy"] == policy
                and rule["protocol"] == protocol
                and rule["port"] == port
                and rule["destination_segment"] == ALL_DEMO_LOAD_BALANCED_COMPONENTS[fqdn]["segment"]
            ]
            if len(matches) != 1:
                errors.append(f"R_server of {fqdn} {protocol}/{port}: {len(matches)} rule(s) in {policy}, expected 1")
                continue
            rule = matches[0]
            if rule["action"] != "permit" or rule["disabled"]:
                errors.append(f"R_server {rule['name']}: action {rule['action']}, disabled {rule['disabled']}")
            if rule["source_prefixes"] != legs:
                errors.append(
                    f"R_server {rule['name']}: source prefixes {sorted(map(str, rule['source_prefixes']))}, "
                    f"expected the leg {sorted(map(str, legs))}"
                )

        # R_client: from each client's segment to the VIP/32, and no direct rule to the target's segment.
        result = await async_client_main.execute_graphql(
            query=RULES_BY_SOURCE_SEGMENT_QUERY,
            variables={"names": sorted({c["source_segment"] for c in ALL_DEMO_LB_CLIENT_DEPENDENCIES.values()})},
            branch_name=scenario_branch,
        )
        client_rules = _rules(result)
        for dependency, client in sorted(ALL_DEMO_LB_CLIENT_DEPENDENCIES.items()):
            target = ALL_DEMO_LOAD_BALANCED_COMPONENTS[client["target"]]
            from_source = [rule for rule in client_rules if rule["source_segment"] == client["source_segment"]]
            for protocol, port in client["ports"]:
                if (protocol, port) not in target["ports"]:
                    continue
                for key in target["contexts"]:
                    vip = vip_addresses.get((client["target"], protocol, port, key))
                    if vip is None:
                        errors.append(f"{dependency}: no VIP of {client['target']} {protocol}/{port} on {key}")
                        continue
                    r_client = [
                        rule
                        for rule in from_source
                        if rule["destination_ips"] == {vip} and rule["protocol"] == protocol and rule["port"] == port
                    ]
                    if len(r_client) != 1:
                        errors.append(
                            f"{dependency}: {len(r_client)} rule(s) from {client['source_segment']} to the VIP {vip} "
                            f"{protocol}/{port}, expected 1 (R_client)"
                        )
                    elif r_client[0]["destination_segment"] or r_client[0]["action"] != "permit":
                        errors.append(
                            f"{dependency}: R_client {r_client[0]['name']} has destination segment "
                            f"{r_client[0]['destination_segment']}, action {r_client[0]['action']}"
                        )
                direct = [
                    str(rule["name"])
                    for rule in from_source
                    if rule["destination_segment"] == target["segment"] and rule["port"] == port
                ]
                if direct:
                    errors.append(
                        f"{dependency}: direct rule(s) {direct} from {client['source_segment']} to "
                        f"{target['segment']} on {protocol}/{port}; a load-balanced target is reached through its VIP only"
                    )

        # Cloud (ALL_DEMO_CLOUD_LOAD_BALANCED_COMPONENTS): one CloudLoadBalancer per component in its instances'
        # VPC and subnets, in the cloud_loadbalancers artifact group; one VIP per port on it, without an address
        # (the cloud assigns it), the instances' private addresses as pool members. No context, leg or rule.
        cloud = ALL_DEMO_CLOUD_LOAD_BALANCED_COMPONENTS
        result = await async_client_main.execute_graphql(
            query=CLOUD_LOAD_BALANCERS_QUERY,
            variables={"names": sorted(c["load_balancer"] for c in cloud.values())},
            branch_name=scenario_branch,
        )
        cleaned = clean_data(result)
        cloud_lbs = {str(lb["name"]): lb for lb in cleaned.get("CloudLoadBalancer") or []}
        cloud_vips = cleaned.get("LoadbalancerVIP") or []
        for fqdn, component in sorted(cloud.items()):
            name = component["load_balancer"]
            lb = cloud_lbs.get(name)
            if lb is None:
                errors.append(f"cloud load balancer {name} of {fqdn} missing")
                continue
            found = (
                lb.get("lb_type"),
                lb.get("scheme"),
                (lb.get("virtual_network") or {}).get("name"),
                tuple(sorted(str(segment["name"]) for segment in lb.get("network_segments") or [])),
            )
            wanted = ("l4_cloud", component["scheme"], component["virtual_network"], component["segments"])
            if found != wanted:
                errors.append(f"cloud load balancer {name}: (lb_type, scheme, VPC, subnets) {found}, expected {wanted}")
            if "cloud_loadbalancers" not in {group.get("name") for group in lb.get("member_of_groups") or []}:
                errors.append(f"cloud load balancer {name} is not in the cloud_loadbalancers group")
            vips = [vip for vip in cloud_vips if (vip.get("load_balancer") or {}).get("name") == name]
            ports = sorted((str(vip["protocol"]), int(vip["port"])) for vip in vips)
            if ports != sorted(component["ports"]):
                errors.append(f"cloud load balancer {name}: VIPs on {ports}, expected {sorted(component['ports'])}")
            for vip in vips:
                label = f"cloud VIP {fqdn} {vip['protocol']}/{vip['port']} on {name}"
                if vip.get("hostname") != fqdn or (vip.get("component") or {}).get("fqdn") != fqdn:
                    errors.append(f"{label}: hostname/component is not {fqdn}")
                if (vip.get("vip_ip") or {}).get("address"):
                    errors.append(f"{label}: has a vip_ip, the cloud assigns it")
                members = tuple(sorted(str(member["address"]) for member in vip.get("pool_members") or []))
                if members != component["members"]:
                    errors.append(f"{label}: pool members {members}, expected {component['members']}")

        assert not errors, f"30_all load-balancer VIPs are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info(
            "Load-balancer VIPs verified: %d VIP(s), %d client dependency(ies), %d cloud load balancer(s)",
            len(actual),
            len(ALL_DEMO_LB_CLIENT_DEPENDENCIES),
            len(cloud),
        )

    @pytest.mark.order(397)
    @pytest.mark.dependency(scope="session", name="all_demo_segment_legs", depends=["all_demo_tenant_services"])
    @pytest.mark.asyncio
    async def test_04_verify_segment_deployments(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify each segment is deployed into every DC its scope names.

        A ``dc_pair``-scoped segment that only materialises one leg looks fine
        in isolation — the segment exists, the VNI is allocated — but half the
        stretch is missing.

        Scoped like test_03: legs in another suite's DC are not 30_all's.
        """
        logging.info("=== %s - Step 4: Segment Deployment Legs ===", SCENARIO_NAME)

        services = scope_tenant_services_to_dcs(
            await fetch_tenant_services(client=async_client_main, branch=scenario_branch), ALL_DEMO_DC_NAMES
        )

        legs_by_segment: dict[str, list[str]] = {}
        for leg in services["segment_deployments"]:
            segment = str(leg["segment"] or "<unknown>")
            legs_by_segment.setdefault(segment, []).append(str(leg["deployment"] or "<unknown>"))

        errors: list[str] = []

        for segment, expected_dcs in sorted(ALL_DEMO_SEGMENT_LEGS.items()):
            actual = sorted(legs_by_segment.get(segment, []))
            if actual != sorted(expected_dcs):
                errors.append(f"{segment}: deployed into {actual}, expected {sorted(expected_dcs)}")

        unexpected = sorted(set(legs_by_segment) - set(ALL_DEMO_SEGMENT_LEGS))
        if unexpected:
            errors.append(f"unexpected segment deployment(s): {unexpected}")

        vni_missing = sorted(str(leg["segment"]) for leg in services["segment_deployments"] if not leg["vni"])
        if vni_missing:
            errors.append(f"segment deployment(s) with no VNI allocated: {vni_missing}")

        assert not errors, f"30_all segment deployments are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info(
            "Segment legs verified: %d across %d segment(s)", len(services["segment_deployments"]), len(legs_by_segment)
        )

    @pytest.mark.order(408)
    @pytest.mark.dependency(scope="session", depends=["all_demo_tenant_services"])
    @pytest.mark.asyncio
    async def test_05_verify_colocation_serving_contexts(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify each colocation deployment is served by its own metro's firewalls.

        A deployment in a metro with a firewall pair is served by that pair's
        shared context. That context has a VLAN from the metro's pool and one
        transit sub-interface per member per VRF leg (ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS;
        test_03b checks their addresses and exchanges). A deployment in an
        edge-only metro has no context: pointing it at another metro's firewalls
        would hairpin its traffic across the WAN.
        """
        logging.info("=== %s - Step 5: Colocation Serving Contexts ===", SCENARIO_NAME)

        result = await async_client_main.execute_graphql(query=COLOCATION_CONTEXTS_QUERY, branch_name=scenario_branch)
        deployments = {
            edge["node"]["name"]["value"]: edge["node"] for edge in result["TopologyCustomerColocation"]["edges"]
        }

        errors: list[str] = []
        if sorted(deployments) != sorted(ALL_DEMO_COLOCATION_SERVED):
            errors.append(
                f"colocation deployments {sorted(deployments)}, expected {sorted(ALL_DEMO_COLOCATION_SERVED)}"
            )

        for name, served in sorted(ALL_DEMO_COLOCATION_SERVED.items()):
            deployment = deployments.get(name)
            if deployment is None:
                continue
            metro = (deployment.get("parent") or {}).get("node") or {}
            metro_firewalls = {edge["node"]["id"] for edge in (metro.get("devices") or {}).get("edges", [])}
            context = (deployment.get("serving_firewall_context") or {}).get("node")

            if not served:
                if metro_firewalls:
                    errors.append(f"{name}: metro has firewalls, expected an edge-only metro")
                if context:
                    errors.append(f"{name}: edge-only metro, but served by '{context['name']['value']}'")
                continue

            if not context:
                errors.append(f"{name}: metro has firewalls but the deployment has no serving_firewall_context")
                continue
            context_name = context["name"]["value"]
            if (context.get("tenant") or {}).get("node"):
                errors.append(f"{name}: served by tenant-dedicated '{context_name}', expected the metro's shared one")
            cluster = (context.get("cluster") or {}).get("node") or {}
            members = {edge["node"]["id"] for edge in (cluster.get("capabilities") or {}).get("edges", [])}
            if not metro_firewalls or members != metro_firewalls:
                errors.append(f"{name}: '{context_name}' is on cluster {sorted(members)}, not the metro's firewalls")

            site = name.rsplit("-", 1)[-1]
            expected = next(
                (e for e in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS if e["site"] == site and e["tenant"] is None), None
            )
            if expected is None:
                errors.append(f"{name}: no expected transit context for the metro '{site}'")
                continue
            vlan = (context.get("vlan_id") or {}).get("value")
            if not vlan or not FW_CONTEXT_VLAN_START <= int(vlan) <= FW_CONTEXT_VLAN_END:
                errors.append(f"{name}: '{context_name}' VLAN {vlan}, expected one in the metro pool")
                continue
            # Sub-interfaces are `<uplink>.<vlan>`; the tagged edge ports (Ethernet1/15-16) have no dot.
            sub_vlans = sorted(
                str(edge["node"]["name"]["value"]).rsplit(".", 1)[-1]
                for edge in (context.get("capabilities") or {}).get("edges", [])
                if "." in str(edge["node"]["name"]["value"])
            )
            expected_vlans = sorted(
                str(transit_vlan(int(vlan), transit_namespace_type(namespace)))
                for namespace in expected["namespaces"]
                for _ in range(ALL_DEMO_FIREWALL_MEMBERS)
            )
            if sub_vlans != expected_vlans:
                errors.append(f"{name}: '{context_name}' sub-interface VLANs {sub_vlans}, expected {expected_vlans}")

        assert not errors, f"30_all colocation serving contexts are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info(
            "Colocation serving contexts verified: %d served, %d edge-only",
            sum(ALL_DEMO_COLOCATION_SERVED.values()),
            len(ALL_DEMO_COLOCATION_SERVED) - sum(ALL_DEMO_COLOCATION_SERVED.values()),
        )

    # ------------------------------------------------------------------
    # DCI sessions (add_circuit, peering_role dci)
    # ------------------------------------------------------------------

    @pytest.mark.order(409)
    @pytest.mark.dependency(scope="session", depends=["all_demo_physical_circuits"])
    @pytest.mark.asyncio
    async def test_06_verify_dci_sessions(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify add_circuit built one keyed EVPN Multi-Site session per dark fibre.

        Each session runs between the two ends' overlay BGP processes over a
        /127 from the DCI pool, carries IPv6 unicast + L2VPN EVPN, and is keyed
        with the DC fabric's overlay key.
        """
        logging.info("=== %s - Step 6: DCI Sessions ===", SCENARIO_NAME)

        expected = {f"DCI-{circuit_id}": ends for circuit_id, ends in ALL_DEMO_DCI_CIRCUITS.items()}
        result = await async_client_main.execute_graphql(
            query=DCI_SESSIONS_QUERY, variables={"names": sorted(expected)}, branch_name=scenario_branch
        )
        sessions = {edge["node"]["name"]["value"]: edge["node"] for edge in result["ManagedBGPPeering"]["edges"]}

        errors: list[str] = []
        if sorted(sessions) != sorted(expected):
            errors.append(f"DCI sessions {sorted(sessions)}, expected {sorted(expected)}")

        for name, (dc_name, ends) in sorted(expected.items()):
            session = sessions.get(name)
            if session is None:
                continue
            if session["peering_role"]["value"] != "dci" or session["session_type"]["value"] != "EBGP":
                errors.append(f"{name}: {session['peering_role']['value']}/{session['session_type']['value']}")
            if session["ttl"]["value"] != 1:
                errors.append(f"{name}: ttl {session['ttl']['value']}, expected 1 (directly connected)")
            key = ((session.get("password") or {}).get("node") or {}).get("name", {}).get("value")
            if key != f"{dc_name.lower()}-overlay-key":
                errors.append(f"{name}: keyed with '{key}', expected '{dc_name.lower()}-overlay-key'")
            families = sorted(
                (edge["node"]["afi"]["value"], edge["node"]["safi"]["value"])
                for edge in session["address_families"]["edges"]
            )
            if families != [("ipv6", "unicast"), ("l2vpn", "evpn")]:
                errors.append(f"{name}: address families {families}")
            processes = sorted(
                (edge["node"]["name"]["value"], edge["node"]["process_role"]["value"])
                for edge in session["bgp_processes"]["edges"]
            )
            if processes != sorted((f"{device}-bgp-overlay", "overlay") for device, _ in ends):
                errors.append(f"{name}: runs on {processes}")
            interfaces = {
                (edge["node"]["device"]["node"]["name"]["value"], edge["node"]["name"]["value"]): (
                    ((edge["node"].get("ip_address") or {}).get("node") or {}).get("address", {}).get("value")
                )
                for edge in session["capabilities"]["edges"]
            }
            if sorted(interfaces) != sorted(ends):
                errors.append(f"{name}: on interfaces {sorted(interfaces)}, expected {sorted(ends)}")
                continue
            addresses = [ipaddress.IPv6Interface(a) for a in interfaces.values() if a and ":" in a]
            if len(addresses) != 2 or addresses[0].network != addresses[1].network:
                errors.append(
                    f"{name}: interface addresses {sorted(interfaces.values(), key=str)} are not one IPv6 P2P"
                )
            elif addresses[0].network.prefixlen != 127 or not addresses[0].network.subnet_of(DCI_POOL_NETWORK):
                errors.append(f"{name}: P2P {addresses[0].network} is not a /127 from {DCI_POOL_NETWORK}")

        assert not errors, f"30_all DCI sessions are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("DCI sessions verified: %d", len(sessions))
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    @pytest.mark.order(410)
    @pytest.mark.dependency(scope="session", depends=["all_demo_physical_circuits"])
    @pytest.mark.asyncio
    async def test_07_verify_internet_upstream(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the ISP transit sessions and that each DC's DCI lands on both upstream BGWs.

        The four TRANSIT-* eBGP sessions run in the INTERNET namespace (an
        `internet`-type namespace is what makes the edge filter them and
        export 0/0 as EVPN type-5). The upstream BGW of a DC is the edge its
        dark fibres end on: both edges of its metro, each a Multi-Site BGW with its own DCI.
        """
        logging.info("=== %s - Step 7: INTERNET upstream ===", SCENARIO_NAME)

        errors: list[str] = []
        result = await async_client_main.execute_graphql(
            query=TRANSIT_SESSIONS_QUERY,
            variables={"names": sorted(ALL_DEMO_INTERNET_TRANSIT_PEERINGS)},
            branch_name=scenario_branch,
        )
        sessions = {edge["node"]["name"]["value"]: edge["node"] for edge in result["ManagedBGPPeering"]["edges"]}
        if sorted(sessions) != sorted(ALL_DEMO_INTERNET_TRANSIT_PEERINGS):
            errors.append(f"transit sessions {sorted(sessions)}, expected {sorted(ALL_DEMO_INTERNET_TRANSIT_PEERINGS)}")

        for name, edge_device in sorted(ALL_DEMO_INTERNET_TRANSIT_PEERINGS.items()):
            session = sessions.get(name)
            if session is None:
                continue
            if session["session_type"]["value"] != "EBGP":
                errors.append(f"{name}: session type {session['session_type']['value']}, expected EBGP")
            local = [
                edge["node"]
                for edge in session["capabilities"]["edges"]
                if edge["node"]["device"]["node"]["name"]["value"] == edge_device
            ]
            if len(local) != 1:
                errors.append(f"{name}: {len(local)} interface(s) on {edge_device}, expected 1")
                continue
            namespace = (((local[0].get("ip_address") or {}).get("node") or {}).get("ip_namespace") or {}).get(
                "node"
            ) or {}
            if namespace.get("name", {}).get("value") != "INTERNET":
                errors.append(f"{name}: local address of {edge_device} is not in namespace INTERNET")
            elif namespace["namespace_type"]["value"] != "internet":
                errors.append(f"{name}: namespace INTERNET has type {namespace['namespace_type']['value']}")

        transit_edges = set(ALL_DEMO_INTERNET_TRANSIT_PEERINGS.values())
        if transit_edges != ALL_DEMO_INTERNET_BGWS:
            errors.append(f"transit edges {sorted(transit_edges)} are not exactly the BGWs")

        # Each DC's DCI sessions land on BOTH BGWs of the metro it fronts, and on no other edge.
        dci = await async_client_main.execute_graphql(
            query=DCI_SESSIONS_QUERY,
            variables={"names": [f"DCI-{circuit_id}" for circuit_id in ALL_DEMO_DCI_CIRCUITS]},
            branch_name=scenario_branch,
        )
        landing: dict[str, set[str]] = {}
        for edge in dci["ManagedBGPPeering"]["edges"]:
            node = edge["node"]
            circuit_id = node["name"]["value"].removeprefix("DCI-")
            devices = {i["node"]["device"]["node"]["name"]["value"] for i in node["capabilities"]["edges"]}
            landing.setdefault(ALL_DEMO_DCI_CIRCUITS[circuit_id][0], set()).update(
                device for device in devices if device.startswith("eg-")
            )
        for dc_name, upstream in sorted(ALL_DEMO_INTERNET_UPSTREAM_BY_DC.items()):
            if landing.get(dc_name) != set(upstream):
                errors.append(
                    f"{dc_name}: DCI lands on {sorted(landing.get(dc_name, ()))}, expected {sorted(upstream)}"
                )
        all_upstream = set().union(*ALL_DEMO_INTERNET_UPSTREAM_BY_DC.values())
        if all_upstream != ALL_DEMO_INTERNET_BGWS:
            errors.append("the upstream BGWs are not exactly ALL_DEMO_INTERNET_BGWS")

        assert not errors, f"30_all INTERNET upstream is wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("INTERNET upstream verified: %s", ALL_DEMO_INTERNET_UPSTREAM_BY_DC)
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    @pytest.mark.order(411)
    @pytest.mark.dependency(scope="session", depends=["all_demo_physical_circuits"])
    @pytest.mark.asyncio
    async def test_08_verify_metro_edge_pair(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify each metro with exactly two physical edges (FR, PA) links and peers them.

        Per metro: one DcimCable between Ethernet1/8 of both edges, both ports
        role `interlink` and addressed from one /127 inside the metro slice of
        Technical-IPv6; exactly one FABRIC-<a>-<b> iBGP session (ttl 1, role
        fabric, IPv6 unicast + l2vpn evpn) on both overlay processes, keyed
        with the metro's own key; and one shared Multi-Site VIP (a single
        IpamIPAddress on loopback100 of both edges).
        """
        logging.info("=== %s - Step 8: Metro edge pair ===", SCENARIO_NAME)

        client = async_client_main
        errors: list[str] = []
        sessions_result = await client.execute_graphql(
            query=DCI_SESSIONS_QUERY,
            variables={"names": sorted(ALL_DEMO_FABRIC_PEERINGS.values())},
            branch_name=scenario_branch,
        )
        sessions = {
            edge["node"]["name"]["value"]: edge["node"] for edge in sessions_result["ManagedBGPPeering"]["edges"]
        }
        all_fabric = await client.execute_graphql(
            query='query { ManagedBGPPeering(peering_role__value: "fabric") { count } }', branch_name=scenario_branch
        )
        fabric_count = all_fabric["ManagedBGPPeering"]["count"]
        if fabric_count != len(ALL_DEMO_FABRIC_PEERINGS):
            errors.append(f"{fabric_count} fabric session(s), expected {len(ALL_DEMO_FABRIC_PEERINGS)}")

        for metro, ends in sorted(ALL_DEMO_EDGE_INTERLINKS.items()):
            devices = [device for device, _ in ends]
            ports = await client.execute_graphql(
                query=EDGE_INTERLINK_INTERFACES_QUERY,
                variables={"devices": devices, "name": ALL_DEMO_EDGE_INTERLINK_INTERFACE},
                branch_name=scenario_branch,
            )
            by_device = {
                e["node"]["device"]["node"]["name"]["value"]: e["node"] for e in ports["DcimPhysicalInterface"]["edges"]
            }
            if sorted(by_device) != sorted(devices):
                errors.append(
                    f"{metro}: {ALL_DEMO_EDGE_INTERLINK_INTERFACE} on {sorted(by_device)}, expected {devices}"
                )
                continue
            for device, node in sorted(by_device.items()):
                if node["role"]["value"] != "interlink":
                    errors.append(f"{metro}/{device}: {node['name']['value']} has role {node['role']['value']}")

            port_ids = {node["id"] for node in by_device.values()}
            cable_result = await client.execute_graphql(
                query=EDGE_INTERLINK_CABLES_QUERY, variables={"ids": sorted(port_ids)}, branch_name=scenario_branch
            )
            cables = cable_result["DcimCable"]["edges"]
            if len(cables) != 1:
                errors.append(f"{metro}: {len(cables)} cable(s) on the interlink ports, expected exactly 1")
            elif {e["node"]["id"] for e in cables[0]["node"]["endpoints"]["edges"]} != port_ids:
                errors.append(f"{metro}: the cable does not join exactly the two interlink ports")

            pool_name = ALL_DEMO_EDGE_PAIR_POOL_TEMPLATE.format(metro=metro)
            pool = await client.execute_graphql(
                query=EDGE_PAIR_POOL_QUERY, variables={"name": pool_name}, branch_name=scenario_branch
            )
            pool_edges = pool["CoreIPPrefixPool"]["edges"]
            slices = [
                ipaddress.IPv6Network(r["node"]["prefix"]["value"])
                for pool_edge in pool_edges
                for r in pool_edge["node"]["resources"]["edges"]
                if r["node"].get("prefix")
            ]
            if len(pool_edges) != 1 or not slices:
                errors.append(f"{metro}: pool {pool_name} missing or without a prefix ({len(pool_edges)} pool(s))")
            addresses = [
                ipaddress.IPv6Interface(node["ip_address"]["node"]["address"]["value"])
                for node in by_device.values()
                if node.get("ip_address")
            ]
            if len(addresses) != 2 or addresses[0].network != addresses[1].network:
                errors.append(f"{metro}: interlink addresses {addresses} are not one IPv6 P2P")
            elif addresses[0].network.prefixlen != 127 or not any(addresses[0].network.subnet_of(s) for s in slices):
                errors.append(f"{metro}: interlink {addresses[0].network} is not a /127 inside {pool_name} {slices}")

            name = ALL_DEMO_FABRIC_PEERINGS[metro]
            session = sessions.get(name)
            if session is None:
                errors.append(f"{metro}: no {name} session")
            else:
                if session["peering_role"]["value"] != "fabric" or session["session_type"]["value"] != "IBGP":
                    errors.append(f"{name}: {session['peering_role']['value']}/{session['session_type']['value']}")
                if session["ttl"]["value"] != 1:
                    errors.append(f"{name}: ttl {session['ttl']['value']}, expected 1 (directly connected)")
                key = ((session.get("password") or {}).get("node") or {}).get("name", {}).get("value")
                if key != f"{metro}-fabric-key":
                    errors.append(f"{name}: keyed with '{key}', expected '{metro}-fabric-key'")
                families = sorted(
                    (e["node"]["afi"]["value"], e["node"]["safi"]["value"])
                    for e in session["address_families"]["edges"]
                )
                if families != [("ipv6", "unicast"), ("l2vpn", "evpn")]:
                    errors.append(f"{name}: address families {families}")
                processes = sorted(
                    (e["node"]["name"]["value"], e["node"]["process_role"]["value"])
                    for e in session["bgp_processes"]["edges"]
                )
                if processes != sorted((f"{device}-bgp-overlay", "overlay") for device in devices):
                    errors.append(f"{name}: runs on {processes}")
                session_ports = sorted(
                    (e["node"]["device"]["node"]["name"]["value"], e["node"]["name"]["value"])
                    for e in session["capabilities"]["edges"]
                )
                if session_ports != sorted(ends):
                    errors.append(f"{name}: on interfaces {session_ports}, expected {sorted(ends)}")

            # Shared Multi-Site VIP: one address object, referenced by loopback100 of both edges.
            vip_result = await client.execute_graphql(
                query=EDGE_PAIR_VIP_QUERY, variables={"devices": devices}, branch_name=scenario_branch
            )
            vips = {
                e["node"]["device"]["node"]["name"]["value"]: e["node"]
                for e in vip_result["DcimVirtualInterface"]["edges"]
            }
            if sorted(vips) != sorted(devices):
                errors.append(f"{metro}: loopback100 on {sorted(vips)}, expected {devices}")
                continue
            vip_ids = {((node.get("ip_address") or {}).get("node") or {}).get("id") for node in vips.values()}
            if len(vip_ids) != 1 or None in vip_ids:
                errors.append(f"{metro}: loopback100 of {devices} reference {len(vip_ids)} address object(s), not 1")
            if any(node["role"]["value"] != "multisite-vip" for node in vips.values()):
                errors.append(f"{metro}: loopback100 is not role multisite-vip on every edge")
            vip_address = ALL_DEMO_METRO_SITE_VIPS[metro]
            count = await client.execute_graphql(
                query="query ($a: String) { IpamIPAddress(address__value: $a) { count } }",
                variables={"a": vip_address},
                branch_name=scenario_branch,
            )
            if count["IpamIPAddress"]["count"] != 1:
                errors.append(f"{metro}: {count['IpamIPAddress']['count']} IpamIPAddress for {vip_address}, expected 1")

        assert not errors, f"30_all metro edge pairs are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Metro edge pairs verified: %s", sorted(ALL_DEMO_EDGE_INTERLINKS))
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    @pytest.mark.order(411)
    @pytest.mark.dependency(scope="session", depends=["all_demo_physical_circuits"])
    @pytest.mark.asyncio
    async def test_09_verify_office_wan_exit(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify each office with a fabric and an SD-WAN edge exits through it (add_office_fabric).

        Per distribution-switch: one WAN-EXIT-<switch>-<edge> eBGP session (role regular, ttl 1, ipv4 unicast, keyed
        with the office's wan-exit key) between a regular BGP process on the switch and one on the edge, over two
        dot1q sub-interfaces <port>.<tag> in the office's tenant VRF (the tag is the VRF's wan_exit_vlan) addressed
        from one /31. The switches of an office share one AS, the edge has another, both from the 32-bit private pool.
        """
        logging.info("=== %s - Step 9: Office WAN exit ===", SCENARIO_NAME)

        errors: list[str] = []
        for office, switches in sorted(ALL_DEMO_OFFICE_WAN_EXITS.items()):
            edge = f"{office}-EDGE1"
            namespace = OFFICE_NAMESPACE
            tag = wan_exit_vlan(transit_namespace_type(namespace))
            expected = {f"WAN-EXIT-{switch}-{edge}": switch for switch in switches}
            result = await async_client_main.execute_graphql(
                query=WAN_EXIT_SESSIONS_QUERY, variables={"names": sorted(expected)}, branch_name=scenario_branch
            )
            sessions = {e["node"]["name"]["value"]: e["node"] for e in result["ManagedBGPPeering"]["edges"]}
            if sorted(sessions) != sorted(expected):
                errors.append(f"{office}: WAN exit sessions {sorted(sessions)}, expected {sorted(expected)}")

            switch_asns: set[int] = set()
            edge_asns: set[int] = set()
            for name, switch in sorted(expected.items()):
                session = sessions.get(name)
                if session is None:
                    continue
                if (session["peering_role"]["value"], session["session_type"]["value"]) != ("regular", "EBGP"):
                    errors.append(f"{name}: {session['peering_role']['value']}/{session['session_type']['value']}")
                if session["ttl"]["value"] != 1:
                    errors.append(f"{name}: ttl {session['ttl']['value']}, expected 1 (directly connected)")
                key = ((session.get("password") or {}).get("node") or {}).get("name", {}).get("value")
                if key != f"{office.lower()}-wan-exit-key":
                    errors.append(f"{name}: keyed with '{key}', expected '{office.lower()}-wan-exit-key'")
                families = sorted(
                    (e["node"]["afi"]["value"], e["node"]["safi"]["value"])
                    for e in session["address_families"]["edges"]
                )
                if families != [("ipv4", "unicast")]:
                    errors.append(f"{name}: address families {families}")
                processes = {e["node"]["name"]["value"]: e["node"] for e in session["bgp_processes"]["edges"]}
                if sorted(processes) != sorted([f"{switch}-bgp-wan-exit", f"{edge}-bgp-wan-exit"]):
                    errors.append(f"{name}: runs on {sorted(processes)}")
                else:
                    if {p["process_role"]["value"] for p in processes.values()} != {"regular"}:
                        errors.append(
                            f"{name}: process roles {[p['process_role']['value'] for p in processes.values()]}"
                        )
                    switch_asns.add(processes[f"{switch}-bgp-wan-exit"]["local_as"]["node"]["asn"]["value"])
                    edge_asns.add(processes[f"{edge}-bgp-wan-exit"]["local_as"]["node"]["asn"]["value"])
                ends = {
                    e["node"]["device"]["node"]["name"]["value"]: e["node"] for e in session["capabilities"]["edges"]
                }
                if sorted(ends) != sorted([switch, edge]):
                    errors.append(f"{name}: on devices {sorted(ends)}, expected {sorted([switch, edge])}")
                    continue
                networks = set()
                for device, end in sorted(ends.items()):
                    parent = ((end.get("parent_interface") or {}).get("node") or {}).get("name", {}).get("value")
                    if not parent or end["name"]["value"] != f"{parent}.{tag}":
                        errors.append(f"{name}: {device} interface {end['name']['value']}, expected <port>.{tag}")
                    ip = ((end.get("ip_address") or {}).get("node")) or {}
                    if ((ip.get("ip_namespace") or {}).get("node") or {}).get("name", {}).get("value") != namespace:
                        errors.append(f"{name}: {device} address is not in {namespace}")
                    if ip.get("address"):
                        networks.add(ipaddress.IPv4Interface(ip["address"]["value"]).network)
                if len(networks) != 1 or next(iter(networks)).prefixlen != 31:
                    errors.append(f"{name}: the two ends are not one IPv4 /31 ({sorted(map(str, networks))})")
            if len(switch_asns) > 1:
                errors.append(f"{office}: the switches use several AS numbers {sorted(switch_asns)}, expected one")
            if switch_asns & edge_asns or any(asn < 4200000000 for asn in switch_asns | edge_asns):
                errors.append(f"{office}: AS numbers {sorted(switch_asns)} / {sorted(edge_asns)} (edge)")

        assert not errors, f"30_all office WAN exits are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Office WAN exits verified: %s", sorted(ALL_DEMO_OFFICE_WAN_EXITS))
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
