"""Integration test — the 30_all management exchange, office fabric and load-balancer renders (continues test_64).

The management exchange legs and services rules on the firewalls, the leaf without PBR to the management services,
CheckLispFabric and the LISP render of the offices' switches, then the load-balancer steps: CheckLoadBalancer on the
members of the contexts holding the VIPs of ALL_DEMO_LOAD_BALANCED_COMPONENTS, the F5 render of those contexts, the
border leafs' leg and VIP static, and the client leaf's PBR bypass of the VIP/32.

Runs against the branch test_59 built, after test_64.
"""

import ipaddress
import logging
import re

import pytest
from infrahub_sdk import InfrahubClient

from checks.lisp_fabric import CheckLispFabric
from checks.loadbalancer import CheckLoadBalancer
from tasks import ALL_DEMO_BRANCH
from transforms.config.access_leaf import AccessLeaf
from transforms.config.access_switch import AccessSwitch
from transforms.config.border_leaf import BorderLeaf
from transforms.config.distribution_switch import DistributionSwitch
from transforms.config.leaf import Leaf
from transforms.config.loadbalancer import LoadBalancer
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    MANAGEMENT_TYPE,
    transit_addresses,
    transit_vlan,
    zone_name_for_namespace_type,
)

from .all_demo_circuit_constants import ALL_DEMO_OFFICE_FABRICS, ALL_DEMO_OFFICE_WAN_EXITS
from .all_demo_constants import ALL_DEMO_DC_NAMES
from .all_demo_firewall_constants import (
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    ALL_DEMO_MANAGEMENT_RULES,
    MANAGEMENT_CLIENT_SEGMENT,
    MANAGEMENT_NAMESPACE,
    MANAGEMENT_SERVICES_DC,
    MANAGEMENT_SERVICES_GATEWAY,
    MANAGEMENT_SERVICES_LEAF,
    MANAGEMENT_SERVICES_PREFIX,
    MANAGEMENT_SERVICES_VLAN,
    transit_namespace_type,
)
from .all_demo_loadbalancer_constants import (
    ALL_DEMO_DC_LOADBALANCER_PLATFORMS,
    ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS,
    ALL_DEMO_LB_CLIENT_DEPENDENCIES,
    ALL_DEMO_LOAD_BALANCED_COMPONENTS,
    ALL_DEMO_NAMESPACE_L3_VNI,
    ALL_DEMO_SEGMENT_LEGS,
)
from .conftest import TestInfrahubDockerWithClient
from .dc_topology_helpers import fetch_dc_topology
from .firewall_config_helpers import (
    SCENARIO_NAME,
    _block,
    _border_leaf_lb_leg_errors,
    _f5_block,
    _f5_blocks,
    _leg_network,
    _platform_by_name,
    _segment_on_switches,
    _site_contexts,
    _vip_contexts,
)
from .service_inventory_helpers import (
    fetch_loadbalancer_inventory,
    fetch_transit_inventory,
    match_loadbalancer_contexts,
)
from .snapshot_helpers import render_device_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestAllDemoFirewallConfig(TestInfrahubDockerWithClient):
    """Validate and render the nordix segment's egress policy on the firewalls serving it."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    @pytest.mark.order(402)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_07_firewall_config_renders_management_exchange(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Every member of each DC's shared context renders the MANAGEMENT exchange.

        The MANAGEMENT-ZONE leg, tenant zones source-NATed to the MANAGEMENT leg VIP (PAN-OS
        `snat-<zone>-MANAGEMENT`, Check Point `SNAT-<context>-MANAGEMENT`; the VIP already is the
        interface address, so no secondary IP) and a logged MANAGEMENT-ZONE -> tenant-zone deny per
        tenant zone held. Only DC11 holds the services segment, so only its context routes
        10.250.0.0/24 via the MANAGEMENT leg anycast (.1); the other DCs have the leg but no such route.
        """
        logging.info("=== %s - Step 7: Render firewall MANAGEMENT exchange ===", SCENARIO_NAME)

        contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        platform_by_name = await _platform_by_name(async_client_main, scenario_branch)
        management_zone = zone_name_for_namespace_type(MANAGEMENT_TYPE)

        errors: list[str] = []
        rendered_by_platform: dict[str, int] = {}
        for expected in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS:
            if expected["tenant"] is not None:
                continue  # a dedicated context's members are virtual: not a firewall_config device
            dc = expected["site"]
            context = contexts.get((dc, None))
            if context is None:
                errors.append(f"{dc}: no shared context")
                continue
            name = str(context["name"])
            network = _leg_network(context, MANAGEMENT_NAMESPACE)
            addresses = transit_addresses(str(network))
            tenant_zones = [
                zone_name_for_namespace_type(transit_namespace_type(namespace))
                for namespace in expected["namespaces"]
                if transit_namespace_type(namespace) in EXCHANGE_PEERS
            ]
            other_zones = {zone_name_for_namespace_type(ns_type) for ns_type in EXCHANGE_PEERS} - set(tenant_zones)
            for device in sorted({str(leg["device"]) for leg in context["legs"]}):
                platform = platform_by_name.get(device)
                if platform not in ("panos", "checkpoint_gaia"):
                    errors.append(f"{dc}/{device}: platform '{platform}' has no MANAGEMENT assertions in this test")
                    continue
                rendered = await render_device_config(async_client_main, scenario_branch, device)
                rendered_by_platform[str(platform)] = rendered_by_platform.get(str(platform), 0) + 1
                leg_interface = next(
                    str(leg["interface"])
                    for leg in context["legs"]
                    if leg["device"] == device and leg["namespace"] == MANAGEMENT_NAMESPACE
                )
                if platform == "panos":
                    prefix = f"set vsys {name} rulebase nat rules snat-"
                    if f"set vsys {name} zone {management_zone} network layer3 {leg_interface}" not in rendered:
                        errors.append(f"{dc}/{device}: {leg_interface} is not in {management_zone} of vsys {name}")
                    for zone in tenant_zones:
                        rule = f"{prefix}{zone}-{MANAGEMENT_NAMESPACE}"
                        for line in (
                            f"{rule} from {zone}",
                            f"{rule} to {management_zone}",
                            f"{rule} source-translation dynamic-ip-and-port translated-address {addresses['vip']}",
                        ):
                            if line not in rendered:
                                errors.append(f"{dc}/{device}: no '{line}'")
                        if f"deny-{management_zone}-to-{zone}" not in rendered:
                            errors.append(f"{dc}/{device}: no deny-{management_zone}-to-{zone}")
                    if re.search(rf"layer3 units \S+ ip {re.escape(addresses['vip'])}/32$", rendered, re.MULTILINE):
                        errors.append(
                            f"{dc}/{device}: leg VIP {addresses['vip']} is also a /32 secondary (not owned twice)"
                        )
                    routes = re.findall(
                        r"static-route (rt-\d+) destination " + re.escape(MANAGEMENT_SERVICES_PREFIX), rendered
                    )
                    # The zone's CIDRs come from the global SecurityZone root, so every context with a
                    # MANAGEMENT leg routes the services prefix to its own anycast (inert outside DC11:
                    # no rule permits it there and the border leaf has no such prefix in its VRF).
                    nexthops = {
                        route
                        for route in routes
                        if re.search(
                            rf"static-route {route} nexthop ip-address {re.escape(addresses['anycast'])}$",
                            rendered,
                            re.MULTILINE,
                        )
                    }
                    if not nexthops:
                        errors.append(
                            f"{dc}/{device}: no route to {MANAGEMENT_SERVICES_PREFIX} via {addresses['anycast']}"
                        )
                else:
                    if f"set interface {leg_interface} security-zone {management_zone}" not in rendered:
                        errors.append(f"{dc}/{device}: {leg_interface} is not in {management_zone}")
                    host = (
                        f'mgmt_cli add host name "SNAT-{name}-{MANAGEMENT_NAMESPACE}" ip-address "{addresses["vip"]}"'
                    )
                    if host not in rendered:
                        errors.append(f"{dc}/{device}: no '{host}'")
                    nat_rules = [
                        line
                        for line in rendered.splitlines()
                        if line.startswith("mgmt_cli add nat-rule")
                        and f'translated-source "SNAT-{name}-{MANAGEMENT_NAMESPACE}"' in line
                        and f'original-destination "{management_zone}"' in line
                    ]
                    if len(nat_rules) != len(tenant_zones):
                        errors.append(
                            f"{dc}/{device}: {len(nat_rules)} MANAGEMENT nat-rule line(s), {len(tenant_zones)} expected"
                        )
                    for zone in tenant_zones:
                        if f'name "deny-{management_zone}-to-{zone}"' not in rendered:
                            errors.append(f"{dc}/{device}: no deny-{management_zone}-to-{zone}")
                    route = (
                        f"static-route {MANAGEMENT_SERVICES_PREFIX} nexthop gateway address {addresses['anycast']} on"
                    )
                    if route not in rendered:
                        errors.append(f"{dc}/{device}: '{route}' missing")
                for zone in other_zones:
                    if f"deny-{management_zone}-to-{zone}" in rendered:
                        errors.append(f"{dc}/{device}: deny towards {zone}, which the context does not hold")

        assert {"panos", "checkpoint_gaia"} <= set(rendered_by_platform), (
            f"MANAGEMENT was rendered on {rendered_by_platform}, expected PAN-OS and Check Point"
        )
        assert not errors, f"Firewall MANAGEMENT exchange on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered MANAGEMENT exchange on %s", rendered_by_platform)

    @pytest.mark.order(402)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_07_management_services_rules_render_on_dc11_firewall(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """DC11 (PAN-OS) permits PROD-ZONE -> MANAGEMENT-ZONE udp/53 and udp/123 from the c006 segment.

        Rules `nordix-prod-to-core-dns` / `nordix-prod-to-core-ntp` (05_security_policy.yml) render in the
        shared context's vsys on every member: source the client segment's prefix (allocated by
        add_vxlan_segment, read from the graph), destination 10.250.0.0/24, service svc-udp-<port>,
        action allow.
        """
        logging.info("=== %s - Step 7: Render core DNS/NTP rules ===", SCENARIO_NAME)

        client_prefix = (
            await _segment_on_switches(async_client_main, scenario_branch, MANAGEMENT_CLIENT_SEGMENT)
        ).prefix
        context = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))[
            (MANAGEMENT_SERVICES_DC, None)
        ]
        members = sorted({str(leg["device"]) for leg in context["legs"]})
        assert members, f"{MANAGEMENT_SERVICES_DC}: the shared context has no firewall member"
        errors: list[str] = []
        for device in members:
            rendered = await render_device_config(async_client_main, scenario_branch, device)
            for rule_name, (protocol, port) in ALL_DEMO_MANAGEMENT_RULES.items():
                base = f"set vsys {context['name']} rulebase security rules {rule_name}"
                for line in (
                    f"{base} from {zone_name_for_namespace_type('prod')}",
                    f"{base} to {zone_name_for_namespace_type(MANAGEMENT_TYPE)}",
                    f"{base} source {client_prefix}",
                    f"{base} destination {MANAGEMENT_SERVICES_PREFIX}",
                    f"{base} service svc-{protocol}-{port}",
                    f"{base} action allow",
                ):
                    if line not in rendered:
                        errors.append(f"{device}: no '{line}'")

        assert not errors, f"DC11 core services rules on branch '{scenario_branch}' are wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    @pytest.mark.order(402)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_07_leaf_config_has_no_pbr_to_the_management_services(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """The services segment is never steered, and nothing bypasses the firewall towards it.

        Leaf/access-leaf carrying the c006 segment (its VLAN on that switch, read from the graph): the PBR
        bypass ACL, if rendered, does not permit 10.250.0.0/24, so the flow goes to the firewall. The access-leaf pinned to the services segment
        renders `interface Vlan250` in VRF MANAGEMENT with the anycast gateway 10.250.0.1/24 and no
        PBR (no `ip policy route-map`, no RM-LEAF-PBR-VLAN250 / PBR-BYPASS-VLAN250).
        """
        logging.info("=== %s - Step 7: Leaf PBR and the services SVI ===", SCENARIO_NAME)

        errors: list[str] = []
        client = await _segment_on_switches(async_client_main, scenario_branch, MANAGEMENT_CLIENT_SEGMENT)
        topology = await fetch_dc_topology(async_client_main, scenario_branch, MANAGEMENT_SERVICES_DC)
        leaf_classes = {"leaf": Leaf, "access-leaf": AccessLeaf}
        carrying_client_vlan: list[str] = []
        services_leaf_rendered = False
        for device in sorted(topology["devices"], key=lambda d: str(d["name"])):
            if device.get("role") not in leaf_classes:
                continue
            name = str(device["name"])
            rendered = await render_device_config(
                async_client_main,
                scenario_branch,
                name,
                query="leaf_config",
                transform_cls=leaf_classes[str(device["role"])],
            )
            client_vlan = client.vlan_on(name)
            if client_vlan is not None and re.search(rf"^vlan {client_vlan}$", rendered, re.MULTILINE):
                carrying_client_vlan.append(name)
                bypass = _block(rendered, f"ip access-list PBR-BYPASS-VLAN{client_vlan}")
                if bypass is not None and MANAGEMENT_SERVICES_PREFIX in bypass:
                    errors.append(f"{name}: PBR-BYPASS-VLAN{client_vlan} permits {MANAGEMENT_SERVICES_PREFIX}")
            if name != MANAGEMENT_SERVICES_LEAF:
                continue
            services_leaf_rendered = True
            svi = _block(rendered, f"interface Vlan{MANAGEMENT_SERVICES_VLAN}")
            if svi is None:
                errors.append(f"{name}: no 'interface Vlan{MANAGEMENT_SERVICES_VLAN}'")
            else:
                if f"vrf member {MANAGEMENT_NAMESPACE}" not in svi:
                    errors.append(f"{name}: Vlan{MANAGEMENT_SERVICES_VLAN} is not in VRF {MANAGEMENT_NAMESPACE}")
                if f"ip address {MANAGEMENT_SERVICES_GATEWAY}" not in svi:
                    errors.append(
                        f"{name}: Vlan{MANAGEMENT_SERVICES_VLAN} lacks 'ip address {MANAGEMENT_SERVICES_GATEWAY}'"
                    )
                if "ip policy route-map" in svi:
                    errors.append(f"{name}: Vlan{MANAGEMENT_SERVICES_VLAN} carries a PBR policy")
            for pbr_object in (
                f"RM-LEAF-PBR-VLAN{MANAGEMENT_SERVICES_VLAN}",
                f"PBR-BYPASS-VLAN{MANAGEMENT_SERVICES_VLAN}",
            ):
                if pbr_object in rendered:
                    errors.append(f"{name}: renders {pbr_object}")

        assert carrying_client_vlan, (
            f"no leaf of {MANAGEMENT_SERVICES_DC} carries {MANAGEMENT_CLIENT_SEGMENT} "
            f"(its VLAN per switch: {client.vlans_by_device or client.vlan_id})"
        )
        assert services_leaf_rendered, (
            f"{MANAGEMENT_SERVICES_LEAF} is not a leaf or access-leaf of {MANAGEMENT_SERVICES_DC}"
        )
        assert not errors, (
            f"Leaf PBR towards the services segment on branch '{scenario_branch}' is wrong:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    @pytest.mark.order(402)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_08_office_fabric_check_passes_and_switches_render_lisp(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """CheckLispFabric finds nothing to report, and every switch of an office fabric renders its LISP site.

        The distribution-switches are the co-located map-servers: ``router lisp``, ``map-server``, the office's
        ``site`` with its key and the EID records of its segments, over the OSPF underlay. The access-switches are
        the edges: ``router lisp`` with ``etr map-server ... key`` toward every map-server and no map-server role of
        their own. An office with an SD-WAN edge adds the exit: ``proxy-etr`` on the borders, ``use-petr`` on the
        edges. No office renders the EVPN control plane or VXLAN (the BGP process of the WAN exit is not EVPN). The
        shape of the render is pinned, not device behaviour: every LISP line is unverified-no-lab.
        """
        logging.info("=== %s - Step 8: Office fabrics ===", SCENARIO_NAME)

        check = CheckLispFabric(branch=scenario_branch, client=async_client_main, params={})
        passed = await check.run()
        check_errors = [str(entry.get("message") or "") for entry in check.errors]
        assert passed and not check_errors, f"CheckLispFabric failed on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in check_errors
        )
        assert ALL_DEMO_OFFICE_FABRICS, "the 30_all data declares no office fabric"

        errors: list[str] = []
        for office, switches in sorted(ALL_DEMO_OFFICE_FABRICS.items()):
            wan_exit = office in ALL_DEMO_OFFICE_WAN_EXITS
            for role, query, transform_cls in (
                ("distribution-switch", "distribution_switch_config", DistributionSwitch),
                ("access-switch", "access_switch_config", AccessSwitch),
            ):
                assert switches[role], f"{office} has no {role}"
                for device in switches[role]:
                    rendered = await render_device_config(
                        async_client_main, scenario_branch, device, query=query, transform_cls=transform_cls
                    )
                    expected = {
                        "router lisp": r"^router lisp$",
                        "OSPF underlay": r"^router ospf ",
                        "etr map-server ... key": r"^\s+etr map-server \S+ key \S+$",
                    }
                    if role == "distribution-switch":
                        expected |= {
                            "map-server": r"^\s+map-server$",
                            f"site {office}": rf"^\s+site {re.escape(office)}$",
                            "site authentication-key": r"^\s+authentication-key \S+$",
                            "L3 eid-record": r"^\s+eid-record instance-id \d+ \S+/\d+ accept-more-specifics$",
                            "L2 eid-record": r"^\s+eid-record instance-id \d+ any-mac$",
                        }
                    if wan_exit:
                        expected["proxy-etr" if role == "distribution-switch" else "use-petr"] = (
                            r"^\s+proxy-etr$" if role == "distribution-switch" else r"^\s+use-petr \S+$"
                        )
                    errors += [
                        f"{device}: no '{what}'"
                        for what, pattern in expected.items()
                        if not re.search(pattern, rendered, re.MULTILINE)
                    ]
                    if role == "access-switch" and re.search(r"^\s+map-server$", rendered, re.MULTILINE):
                        errors.append(f"{device}: renders the map-server role (only distribution-switches are)")
                    errors += [
                        f"{device}: renders '{pattern}'"
                        for pattern in (r"l2vpn evpn", r"\bnve1\b")
                        if re.search(pattern, rendered, re.MULTILINE)
                    ]
        assert not errors, f"Office fabric renders on branch '{scenario_branch}' are wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Office fabrics verified: %s", sorted(ALL_DEMO_OFFICE_FABRICS))

    # ------------------------------------------------------------------
    # Load-balancer contexts (one-arm, SNAT automap): the LB, the border leafs, the client leaf
    # ------------------------------------------------------------------

    @pytest.mark.order(403)
    @pytest.mark.dependency(scope="session", name="all_demo_loadbalancer_check", depends=["all_demo_loadbalancer_vips"])
    @pytest.mark.asyncio
    async def test_09_check_loadbalancer_passes(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """CheckLoadBalancer finds no error on every member of a context holding VIPs: the
        appliance is ready, and every context of its cluster has its VLAN, VIP range, a leg on
        the member and VIPs inside the range with pool members."""
        logging.info("=== %s - Step 9: CheckLoadBalancer ===", SCENARIO_NAME)

        contexts = await _vip_contexts(async_client_main, scenario_branch)
        errors: list[str] = []
        for key, context in sorted(contexts.items(), key=repr):
            members = sorted({str(leg["device"]) for leg in context["legs"]})
            assert members, f"{key}: '{context['name']}' has no member with a leg"
            for member in members:
                check = CheckLoadBalancer(branch=scenario_branch, client=async_client_main, params={"device": member})
                passed = await check.run()
                found = [str(entry.get("message") or "") for entry in check.logs if entry.get("level") == "ERROR"]
                if not passed or found:
                    errors.append(f"{member} ({context['name']}): {found or 'check failed'}")

        assert not errors, f"CheckLoadBalancer failed on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    @pytest.mark.order(403)
    @pytest.mark.dependency(scope="session", depends=["all_demo_loadbalancer_check"])
    @pytest.mark.asyncio
    async def test_09_loadbalancer_config_renders_contexts(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Every member of a context holding VIPs renders the context (F5 TMOS, the DC10 and DC12 pairs).

        The context's partition (`SHARED-<NS>` for a shared one) defaults to a strict route
        domain whose id is the tenant namespace's l3_vni (PROD 60000, NON-PROD 60001, parent
        none); the leg VLAN tagged on the uplink, the member's own self IP (.5/.6), the floating
        .4 and the default route to the border anycast .1; per VIP a virtual server
        `destination /<partition>/<vip>:<port>` with SNAT automap and a pool of its members.
        """
        logging.info("=== %s - Step 9: Render load-balancer contexts ===", SCENARIO_NAME)

        contexts = await _vip_contexts(async_client_main, scenario_branch)
        errors: list[str] = []
        rendered_count = 0
        for (dc, namespace, tenant), context in sorted(contexts.items(), key=repr):
            platform = ALL_DEMO_DC_LOADBALANCER_PLATFORMS.get(dc)
            assert platform == "f5_tmos", f"{dc}: load-balancer platform '{platform}', this test asserts the F5 render"
            name = str(context["name"])
            partition = f"SHARED-{namespace}" if tenant is None else "-".join(tenant.split("-")[:2]).upper()
            route_domain = ALL_DEMO_NAMESPACE_L3_VNI[namespace]
            p = f"/{partition}/"
            vlan = transit_vlan(int(context["vlan"]), transit_namespace_type(namespace))
            for leg in sorted(context["legs"], key=lambda leg: str(leg["device"])):
                device = str(leg["device"])
                rendered = await render_device_config(
                    async_client_main, scenario_branch, device, query="loadbalancer_config", transform_cls=LoadBalancer
                )
                rendered_count += 1
                addresses = transit_addresses(str(ipaddress.IPv4Interface(leg["address"]).network))
                label = f"{dc}/{device} ({name})"
                expected_blocks = {
                    f"auth partition {partition}": (f"default-route-domain {route_domain}",),
                    f"net route-domain /Common/rd{route_domain}": (
                        f"id {route_domain}",
                        "parent none",
                        "strict enabled",
                        f"/Common/vlan{vlan}",
                    ),
                    f"net vlan /Common/vlan{vlan}": (f"tag {vlan}", "tagged"),
                    f"net self {p}self{vlan}": (f"address {leg['address']}",),
                    f"net self {p}float{vlan}": (f"address {addresses['vip']}/29", "floating enabled"),
                    f"net route {p}default": (f"gw {addresses['anycast']}", "network default"),
                }
                for header, lines in expected_blocks.items():
                    block = _f5_block(rendered, header)
                    if block is None:
                        errors.append(f"{label}: no '{header}'")
                        continue
                    errors += [f"{label}: '{header}' lacks '{line}'" for line in lines if line not in block]
                for vip in context["vips"]:
                    vip_ip = str(ipaddress.IPv4Interface(vip["address"]).ip)
                    virtuals = [
                        block
                        for block in _f5_blocks(rendered, "ltm virtual")
                        if f"destination {p}{vip_ip}:{vip['port']}" in block
                    ]
                    if len(virtuals) != 1:
                        errors.append(
                            f"{label}: {len(virtuals)} virtual server(s) 'destination {p}{vip_ip}:{vip['port']}'"
                        )
                    elif "type automap" not in virtuals[0]:
                        errors.append(f"{label}: the virtual server of {vip_ip}:{vip['port']} has no SNAT automap")
                    for member in vip["members"]:
                        member_ip = str(ipaddress.IPv4Interface(member).ip)
                        if f"{p}{member_ip}:{vip['port']}" not in rendered:
                            errors.append(
                                f"{label}: pool member {member_ip}:{vip['port']} of {vip['hostname']} not rendered"
                            )

        assert rendered_count, "no load-balancer config was rendered"
        assert not errors, f"loadbalancer_config on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered load-balancer contexts on %d member(s)", rendered_count)

    @pytest.mark.order(403)
    @pytest.mark.dependency(scope="session", depends=["all_demo_loadbalancer_vips"])
    @pytest.mark.asyncio
    async def test_09_border_leaf_config_renders_loadbalancer_legs(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """The border leafs of a DC holding VIPs carry every load-balancer context of the DC.

        Per context: the `load-balancer` port facing each member trunks the leg VLAN; the leg
        VLAN has its L2VNI (transit_vni) and an anycast SVI (.1 of the leg /29) in the tenant
        VRF; `vrf context <NS>` routes the VIP /26 to the leg's floating .4, and the static is
        exported into EVPN (PL-VRF-STATIC-<NS>).
        """
        logging.info("=== %s - Step 9: Render border-leaf load-balancer legs ===", SCENARIO_NAME)

        by_key, _ = match_loadbalancer_contexts(
            await fetch_loadbalancer_inventory(client=async_client_main, branch=scenario_branch),
            ALL_DEMO_DC_NAMES,
            ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS,
        )
        vip_dcs = sorted(
            {key[0] for component in ALL_DEMO_LOAD_BALANCED_COMPONENTS.values() for key in component["contexts"]}
        )
        errors: list[str] = []
        for dc in vip_dcs:
            dc_contexts = [context for key, context in by_key.items() if key[0] == dc]
            border_leafs = sorted({str(port["device"]) for context in dc_contexts for port in context["border_ports"]})
            assert border_leafs, f"{dc}: no border leaf carries a load-balancer context"
            for device in border_leafs:
                rendered = await render_device_config(
                    async_client_main, scenario_branch, device, query="border_leaf_config", transform_cls=BorderLeaf
                )
                for context in dc_contexts:
                    errors += _border_leaf_lb_leg_errors(device, rendered, context)

        assert not errors, f"border-leaf load-balancer legs on branch '{scenario_branch}' are wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Border-leaf load-balancer legs rendered in %s", vip_dcs)

    @pytest.mark.order(403)
    @pytest.mark.dependency(scope="session", depends=["all_demo_loadbalancer_vips"])
    @pytest.mark.asyncio
    async def test_09_client_leaf_bypasses_the_firewall_to_the_vip(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """A client of a load-balanced component reaches its VIP in its own VRF, never through the firewall.

        Every leaf or access-leaf carrying the client's segment (ALL_DEMO_LB_CLIENT_DEPENDENCIES)
        permits the VIP/32 in the PBR bypass ACL of the segment's VLAN.
        """
        logging.info("=== %s - Step 9: Client leaf bypass to the VIP ===", SCENARIO_NAME)

        contexts = await _vip_contexts(async_client_main, scenario_branch)
        leaf_classes = {"leaf": Leaf, "access-leaf": AccessLeaf}
        errors: list[str] = []
        for dependency, client in sorted(ALL_DEMO_LB_CLIENT_DEPENDENCIES.items()):
            vips = [
                str(ipaddress.IPv4Interface(vip["address"]).ip)
                for key in ALL_DEMO_LOAD_BALANCED_COMPONENTS[client["target"]]["contexts"]
                for vip in contexts[key]["vips"]
                if vip["component"] == client["target"] and (vip["protocol"], vip["port"]) in client["ports"]
            ]
            assert vips, f"{dependency}: no VIP of {client['target']} on {client['ports']}"
            source = await _segment_on_switches(async_client_main, scenario_branch, client["source_segment"])
            carrying: list[str] = []
            for dc in sorted(set(ALL_DEMO_SEGMENT_LEGS[client["source_segment"]]) & set(ALL_DEMO_DC_NAMES)):
                topology = await fetch_dc_topology(async_client_main, scenario_branch, dc)
                for device in sorted(topology["devices"], key=lambda d: str(d["name"])):
                    vlan = source.vlan_on(str(device["name"]))
                    if device.get("role") not in leaf_classes or vlan is None:
                        continue
                    rendered = await render_device_config(
                        async_client_main,
                        scenario_branch,
                        str(device["name"]),
                        query="leaf_config",
                        transform_cls=leaf_classes[str(device["role"])],
                    )
                    if not re.search(rf"^vlan {vlan}$", rendered, re.MULTILINE):
                        continue
                    carrying.append(str(device["name"]))
                    bypass = _block(rendered, f"ip access-list PBR-BYPASS-VLAN{vlan}") or ""
                    errors += [
                        f"{device['name']}: PBR-BYPASS-VLAN{vlan} of {client['source_segment']} does not permit {vip}/32"
                        for vip in vips
                        if not re.search(rf"\bpermit ip any {re.escape(vip)}/32$", bypass, re.MULTILINE)
                    ]
            if not carrying:
                errors.append(f"{dependency}: no leaf renders the VLAN of {client['source_segment']}")

        assert not errors, f"client leaf VIP bypass on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
