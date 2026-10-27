"""Integration test — the 30_all tenant-scoped services (continues test_62).

The firewall and load-balancer contexts (shared vs dedicated, driven by the customer design blueprint), the exchange
transit legs and SNAT addresses, the VIPs of the load_balanced components with their R_server/R_client rules, and the
segment deployment legs, where a stretch segment must carry one leg per DC.

Runs against the branch test_59 builds; it never loads data of its own.
"""

import ipaddress
import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient

from tasks import ALL_DEMO_BRANCH
from utils.data_cleaning import clean_data
from utils.exchange_transit import EXCHANGE_PEERS, TRANSIT_OFFSETS, transit_addresses, transit_vlan
from utils.limits import (
    FW_CONTEXT_VLAN_END,
    FW_CONTEXT_VLAN_START,
    LB_CONTEXT_VLAN_END,
    LB_CONTEXT_VLAN_START,
    LB_VIP_PREFIXLEN,
)

from .all_demo_circuit_constants import ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS
from .all_demo_constants import ALL_DEMO_DC_NAMES
from .all_demo_firewall_constants import (
    ALL_DEMO_BORDER_FIREWALL_PORTS,
    ALL_DEMO_DEDICATED_FIREWALL_TENANTS,
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    ALL_DEMO_FIREWALL_MEMBERS,
    ALL_DEMO_SHARED_FIREWALL_CONTEXTS,
    ALL_DEMO_SNAT_ADDRESSES,
    ALL_DEMO_SNAT_POOL_PREFIX,
    ALL_DEMO_TRANSIT_EXCHANGES,
    ALL_DEMO_TRANSIT_LEGS,
    ALL_DEMO_TRANSIT_PREFIXES,
    ALL_DEMO_TRANSIT_RESOURCES,
    ALL_DEMO_TRANSIT_SUBINTERFACES,
    deployment_environment,
    deployment_namespace,
    transit_namespace_type,
)
from .all_demo_loadbalancer_constants import (
    ALL_DEMO_CLOUD_LOAD_BALANCED_COMPONENTS,
    ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS,
    ALL_DEMO_LB_CLIENT_DEPENDENCIES,
    ALL_DEMO_LB_LEG_RESOURCES,
    ALL_DEMO_LB_VIP_RESOURCES,
    ALL_DEMO_LOAD_BALANCED_COMPONENTS,
    ALL_DEMO_LOADBALANCER_CONTEXTS,
    ALL_DEMO_LOADBALANCER_MEMBERS,
    ALL_DEMO_SEGMENT_LEGS,
    ALL_DEMO_SHARED_LOADBALANCER_CONTEXTS,
)
from .conftest import TestInfrahubDockerWithClient
from .interconnect_queries import (
    CLOUD_LOAD_BALANCERS_QUERY,
    RULES_BY_POLICY_QUERY,
    RULES_BY_SOURCE_SEGMENT_QUERY,
    SCENARIO_NAME,
)
from .interconnect_rules import _TRANSIT_PEER_NAMES, _exchange_ends, _rules
from .service_inventory_helpers import (
    fetch_loadbalancer_inventory,
    fetch_tenant_services,
    fetch_transit_inventory,
    match_loadbalancer_contexts,
    match_transit_contexts,
    scope_tenant_services_to_dcs,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def _expected_loadbalancer_context_name(cluster: str, namespace: str, tenant: str | None) -> str:
    """`shared-{cluster}-{namespace}` for a shared context; for a dedicated one the pair's `{base}-ha-{class}` named
    `{base}-{class}` (generators/firewall_context_spec.py _ContextTarget.context_name), all lowercase."""
    scope = namespace.lower()
    if not tenant:
        return f"shared-{cluster}-{scope}"
    return f"{cluster.removesuffix(f'-ha-{scope}')}-{scope}"


class TestAllDemoInterconnects(TestInfrahubDockerWithClient):
    """Verify the circuit layer and the tenant-scoped services on top of it."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

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
        DC's, and the serving colocation zone's); a context with a tenant exists only
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
                f"expected {ALL_DEMO_SHARED_FIREWALL_CONTEXTS} — one per firewall cluster and tenant namespace"
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
        graph itself reports must agree with them. The FR2 colocation zone's
        shared context is held to the same rules as a DC's: its legs face the
        zone's two edges, whose firewall-role ports are tagged with it, and its
        VLAN comes from the zone's FirewallContext pool (3000-3099).
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
            label = f"{expected['site']}/{expected['tenant'] or 'shared'}/{expected['namespace']}"
            context = by_key.get((expected["site"], expected["namespace"], expected["tenant"]))
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
                    f"{label}: VLAN {context_vlan} is outside the zone pool {FW_CONTEXT_VLAN_START}-{FW_CONTEXT_VLAN_END}"
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

            # The service ports the members' uplinks are cabled to (border leafs of a DC, edges of a zone).
            if len(context["border_ports"]) != ALL_DEMO_FIREWALL_MEMBERS:
                errors.append(
                    f"{label}: '{context_name}' tags {len(context['border_ports'])} service port(s), "
                    f"expected {ALL_DEMO_FIREWALL_MEMBERS}"
                )
            for port in context["border_ports"]:
                ports_by_site.setdefault(expected["site"], {}).setdefault(str(port["id"]), set()).add(context_name)
                port_devices_by_site.setdefault(expected["site"], set()).add(str(port["device"]))

        # Every exchange has a gateway context.
        for exchange in inventory["exchanges"]:
            if not exchange["gateway_id"]:
                errors.append(f"{exchange['name']}: exchange without a gateway context")

        # Totals, all derived from the customers' environments in all_demo_firewall_constants.
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
        # A zone's two ports sit on its two DIFFERENT edges (one per firewall member), not both on one.
        for site in ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS:
            devices = port_devices_by_site.get(site, set())
            if len(devices) != ALL_DEMO_FIREWALL_MEMBERS:
                errors.append(f"{site}: service ports tagged on {sorted(devices)}, expected one per edge")

        # Every colocation context must be a serving zone's: no stray one without legs (policy-only) is left.
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
        FR2 colocation zone's included (it must not reuse a DC's address).
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
            label = f"{expected['site']}/{expected['tenant'] or 'shared'}/{expected['namespace']}"
            context = by_key.get((expected["site"], expected["namespace"], expected["tenant"]))
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
        footprint of the DC is in, `shared-{cluster}-{namespace}`, and one dedicated
        `{owner}-{first}-{second}-{class}` per footprint whose design sets
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
            elif name != _expected_loadbalancer_context_name(str(context["cluster"]), namespace, tenant):
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
