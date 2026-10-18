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

Also covered: the tenant-scoped firewall contexts (shared vs dedicated, driven
by the customer design blueprint) and the segment deployment legs, where a
stretch segment must carry one leg per DC.

Runs against the branch test_59 builds; it never loads data of its own.
"""

import ipaddress
import logging

import pytest
from infrahub_sdk import InfrahubClient

from generators.helpers.naming import sdwan_edge_name
from generators.helpers.pools import FW_CONTEXT_VLAN_END, FW_CONTEXT_VLAN_START
from utils.exchange_transit import EXCHANGE_PEERS, TRANSIT_OFFSETS, transit_vlan, wan_exit_vlan

from .conftest import TestInfrahubDockerWithClient
from .test_constants import (
    ALL_DEMO_BORDER_FIREWALL_PORTS,
    ALL_DEMO_BRANCH,
    ALL_DEMO_COLOCATION_SERVED,
    ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS,
    ALL_DEMO_DC_NAMES,
    ALL_DEMO_DCI_CIRCUITS,
    ALL_DEMO_DEDICATED_FIREWALL_TENANTS,
    ALL_DEMO_EDGE_INTERLINK_INTERFACE,
    ALL_DEMO_EDGE_INTERLINKS,
    ALL_DEMO_EDGE_PAIR_POOL_TEMPLATE,
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    ALL_DEMO_FABRIC_PEERINGS,
    ALL_DEMO_FIREWALL_MEMBERS,
    ALL_DEMO_INTERNET_BGWS,
    ALL_DEMO_INTERNET_TRANSIT_CIRCUITS,
    ALL_DEMO_INTERNET_TRANSIT_PEERINGS,
    ALL_DEMO_INTERNET_UPSTREAM_BY_DC,
    ALL_DEMO_ISP_ACCESS_DEVICE,
    ALL_DEMO_ISP_ACCESS_LOCATION,
    ALL_DEMO_ISP_ACCESS_PORTS,
    ALL_DEMO_LEGACY_EXCHANGES,
    ALL_DEMO_METRO_SITE_VIPS,
    ALL_DEMO_OFFICE_WAN_EXITS,
    ALL_DEMO_PHYSICAL_CIRCUIT_TYPES,
    ALL_DEMO_SDWAN_EDGE_UPLINK,
    ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS,
    ALL_DEMO_SEGMENT_LEGS,
    ALL_DEMO_SHARED_FIREWALL_CONTEXTS,
    ALL_DEMO_SNAT_ADDRESSES,
    ALL_DEMO_SNAT_POOL_PREFIX,
    ALL_DEMO_TRANSIT_EXCHANGES,
    ALL_DEMO_TRANSIT_LEGS,
    ALL_DEMO_TRANSIT_PREFIXES,
    ALL_DEMO_TRANSIT_RESOURCES,
    ALL_DEMO_TRANSIT_SUBINTERFACES,
    ALL_DEMO_VIRTUAL_CIRCUITS,
    OFFICE_NAMESPACE,
    deployment_environment,
    deployment_namespace,
    transit_namespace_type,
)
from .test_helpers import (
    fetch_interconnect_inventory,
    fetch_tenant_services,
    fetch_transit_inventory,
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
# DCI-Technical-IPv6 (data/bootstrap/20_dci_pools.yml) — the DC fabrics run an IPv6 underlay.
DCI_POOL_NETWORK = ipaddress.IPv6Network("fd00:2200::/40")


_TRANSIT_PEER_NAMES = {"internet": "INTERNET", "management": "MANAGEMENT"}


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
        """Verify the physical underlay: type mix, provider, two locations and two distinct ends.

        Every circuit is point-to-point, so exactly two locations and exactly
        two distinct ports (its capabilities, min and max count 2) are
        hard invariants, and a port is an end of at most one circuit — the
        SD-WAN offices' INET-<office> circuits, created concurrently, are where a
        shared access port would show.
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
            if len(circuit["locations"]) != 2:
                errors.append(f"{circuit_id}: terminates at {circuit['locations']}, expected exactly 2 locations")
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
        """Verify each overlay circuit's underlay, ports and cloud termination."""
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

        assert not errors, f"30_all virtual circuits are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Virtual circuits verified: %d", len(by_name))

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
        VLAN comes from the metro's FirewallContext pool (3000-3199).
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
