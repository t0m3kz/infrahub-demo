"""Integration test — the 30_all interconnect and tenant-service layer.

Everything here is the part of the model that leaves a single site: the dark
fibre and cross-connects into the colocation on-ramps, the virtual circuits
riding them into AWS and Azure, the SD-WAN branches' internet underlay, and
the cloud-side terminations that close the on-prem-to-cloud path.

Two edges get special attention because the schema deliberately has exactly one
way to express each of them, and a regression would silently split the graph
into islands:

  * ``interface_capabilities`` — how a circuit surfaces on the port it
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

from .conftest import TestInfrahubDockerWithClient
from .test_constants import (
    ALL_DEMO_BRANCH,
    ALL_DEMO_COLOCATION_SERVED,
    ALL_DEMO_DC_NAMES,
    ALL_DEMO_DCI_CIRCUITS,
    ALL_DEMO_DEDICATED_FIREWALL_TENANTS,
    ALL_DEMO_INTERNET_TRANSIT_CIRCUITS,
    ALL_DEMO_PHYSICAL_CIRCUIT_TYPES,
    ALL_DEMO_SEGMENT_LEGS,
    ALL_DEMO_SHARED_FIREWALL_CONTEXTS,
    ALL_DEMO_VIRTUAL_CIRCUITS,
)
from .test_helpers import fetch_interconnect_inventory, fetch_tenant_services, scope_tenant_services_to_dcs

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
            interface_capabilities {
              edges {
                node {
                  name { value }
                  device { node { id } }
                  ... on DcimVirtualInterface { ip_address { node { address { value } } } }
                }
              }
            }
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
        interface_capabilities {
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
# DCI-Technical-IPv6 (data/bootstrap/20_dci_pools.yml) — the DC fabrics run an IPv6 underlay.
DCI_POOL_NETWORK = ipaddress.IPv6Network("fd00:2200::/40")


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
        """Verify the physical underlay: type mix, provider, and two endpoints.

        Every circuit is point-to-point, so exactly two locations is a hard
        invariant — a one-ended circuit is a dangling reference the loader
        accepts silently.
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

        assert not errors, f"30_all physical circuits are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Physical circuits verified: %d (%s)", len(physical), dict(sorted(by_type.items())))

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
                    f"interface_capabilities, expected {expected['interfaces']}"
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
        shared context, which carries one tagged sub-interface per firewall
        member and one on the edge it is cabled to, each with a P2P address. A
        deployment in an edge-only metro has no context: pointing it at another
        metro's firewalls would hairpin its traffic across the WAN.
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

            vlan_id = (context.get("vlan_id") or {}).get("value")
            interfaces = [edge["node"] for edge in (context.get("interface_capabilities") or {}).get("edges", [])]
            on_firewalls = [i for i in interfaces if ((i.get("device") or {}).get("node") or {}).get("id") in members]
            if not vlan_id:
                errors.append(f"{name}: '{context_name}' has no VLAN allocated")
            if len(interfaces) != 2 * len(members) or len(on_firewalls) != len(members):
                errors.append(
                    f"{name}: '{context_name}' has {len(on_firewalls)} firewall and "
                    f"{len(interfaces) - len(on_firewalls)} edge sub-interface(s), expected {len(members)} of each"
                )
            for interface in interfaces:
                iface = interface["name"]["value"]
                if not iface.endswith(f".{vlan_id}"):
                    errors.append(f"{name}: '{context_name}' sub-interface '{iface}' is not tagged {vlan_id}")
                if not ((interface.get("ip_address") or {}).get("node") or {}).get("address", {}).get("value"):
                    errors.append(f"{name}: '{context_name}' sub-interface '{iface}' has no P2P address")

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
                for edge in session["interface_capabilities"]["edges"]
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
