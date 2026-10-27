"""Integration test — validate and render the colo cloud/partner/SaaS zone
policy rules (data/demos/30_all/08_interconnects/07_zone_policies/) against a
real loaded branch.

Runs the check and the transform in-process against the branch test_59
built, rather than creating a Proposed Change. `.infrahub.yml` declares a
firewall_config artifact definition targeting every firewall in the
"firewalls" group, so a PC over the full 30_all branch would fan out artifact
generation across a hundred-odd devices to prove something one device's worth
of query+check+render already proves.

CheckFirewall (checks/firewall.py) validates the rules one firewall enforces —
the same ones its config renders (Firewall.collect_policies): the policies of
the segments it serves and the rules into them. The render is not
device-agnostic: place_policies_in_contexts() (transforms/helpers/firewall_contexts.py)
gives a context only the rules of the segments of the deployments it serves,
so the render runs on every member of the cluster serving the policy's source
segment (c001-nordix-prod-p, deployment C001-P-FR — the FR2 zone's firewall
pair), whose rules are in that segment's own policy
(seg-c001-nordix-prod-p-egress), and once on a firewall outside it that must
not carry the rules.

The same module then covers the inter-VRF exchange legs the customer boarding
generators build through each firewall context (utils/exchange_transit.py), a
DC's and the FR2 colocation zone's alike:
CheckFirewall on a DC10 (Check Point), DC11 and FR2 (PAN-OS) member, the rendered
firewall config of every site (leg interface, VIP, default route via the INTERNET
leg, PROD/NON-PROD isolation), a DC11 border-leaf config carrying the
transit VRF routes, and the FR edges' config carrying the zone's transit legs. Everything is derived from the loaded graph and from
all_demo_firewall_constants' environment-driven expectations.

A last step runs CheckLispFabric and renders every switch of the offices' LISP fabrics (add_office_fabric): the
distribution-switches as map-servers and the access-switches as edges, with the WAN exit where the office has an
SD-WAN edge.

The load-balancer steps close the one-arm LB path of the two load_balanced components
(ALL_DEMO_LOAD_BALANCED_COMPONENTS): CheckLoadBalancer on the members of the contexts holding
their VIPs, the F5 render of those contexts (partition, strict route domain = namespace l3_vni,
leg, virtual servers with SNAT automap), the border leafs' leg VLAN/VNI/anycast and VIP /26
static, and the client leaf's PBR bypass of the VIP/32.

This module holds the steps up to the border-leaf and edge transit renders. The management exchange, the office
fabric LISP render and the load-balancer steps follow in test_64b_all_demo_management_lb_config.py; the queries and
rules of both are in firewall_config_helpers.py.
"""

import ipaddress
import logging
import re
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient

from checks.firewall import CheckFirewall
from generators.protocols import DcimPhysicalDevice
from tasks import ALL_DEMO_BRANCH
from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.edge import Edge
from transforms.config.leaf import Leaf
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    transit_addresses,
    transit_vlan,
    transit_vni,
    zone_name_for_namespace_type,
)
from utils.internet_space import NON_INTERNET_PREFIXES

from .all_demo_circuit_constants import (
    ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS,
    ALL_DEMO_DCI_CIRCUITS,
    ALL_DEMO_INTERNET_BGWS,
    INTERNET_L3_VNI,
    INTERNET_ROUTE_TARGET,
)
from .all_demo_firewall_constants import (
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    ALL_DEMO_INTERNET_EGRESS_RULES,
    ALL_DEMO_SNAT_POOL_PREFIX,
    transit_namespace_type,
)
from .conftest import TestInfrahubDockerWithClient
from .dc_topology_helpers import fetch_dc_topology
from .firewall_config_helpers import (
    _ACL_LINE,
    EXPECTED_DESTINATION_CIDRS,
    EXPECTED_RULE_NAMES,
    FIREWALLS_QUERY,
    SCENARIO_NAME,
    SERVING_CLUSTER_QUERY,
    SOURCE_SEGMENT,
    _dci_session_errors,
    _edge_transit_errors,
    _fabric_pair_errors,
    _has_ip,
    _leg_network,
    _management_border_leaf_errors,
    _panos_context_leg_errors,
    _platform_by_name,
    _segment_on_switches,
    _shared_contexts,
    _site_contexts,
)
from .service_inventory_helpers import fetch_transit_inventory
from .snapshot_helpers import render_device_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestAllDemoFirewallConfig(TestInfrahubDockerWithClient):
    """Validate and render the nordix segment's egress policy on the firewalls serving it."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    @pytest.fixture
    async def serving_cluster(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> dict[str, Any]:
        """The firewall cluster whose context serves the policy's source
        segment: {"members", "zone_firewalls", "outsider", "context"}, names sorted.
        outsider is a firewall outside the cluster on the members' platform,
        so its template renders rule names too. Function-scoped: an async
        fixture can't be class-scoped under this suite's function-scoped
        asyncio fixture loop (pytest-asyncio ScopeMismatch)."""
        result = await async_client_main.execute_graphql(
            query=SERVING_CLUSTER_QUERY, variables={"segment": SOURCE_SEGMENT}, branch_name=scenario_branch
        )
        segments = result["ManagedVlanSegment"]["edges"]
        assert segments, f"segment '{SOURCE_SEGMENT}' not found on branch '{scenario_branch}'"
        deployment = (segments[0]["node"].get("customer_deployment") or {}).get("node") or {}
        context = (deployment.get("serving_firewall_context") or {}).get("node") or {}
        cluster_id = ((context.get("cluster") or {}).get("node") or {}).get("id")
        assert cluster_id, f"no serving firewall context with a cluster for '{SOURCE_SEGMENT}'"
        zone = (deployment.get("serving_zone") or {}).get("node") or {}
        zone_firewalls = sorted(edge["node"]["name"]["value"] for edge in (zone.get("devices") or {}).get("edges", []))

        devices = await async_client_main.filters(
            kind=DcimPhysicalDevice, role__value="firewall", capabilities__ids=[cluster_id], branch=scenario_branch
        )
        assert devices, f"no firewall device in cluster {cluster_id} on branch '{scenario_branch}'"
        members = sorted(device.name.value for device in devices)

        firewalls = await async_client_main.execute_graphql(query=FIREWALLS_QUERY, branch_name=scenario_branch)
        platform_by_name = {
            edge["node"]["name"]["value"]: ((edge["node"].get("platform") or {}).get("node") or {})
            .get("name", {})
            .get("value")
            for edge in firewalls["DcimPhysicalDevice"]["edges"]
        }
        member_platforms = {platform_by_name.get(name) for name in members}
        outsiders = sorted(
            name for name, platform in platform_by_name.items() if name not in members and platform in member_platforms
        )
        return {
            "members": members,
            "zone_firewalls": zone_firewalls,
            "outsider": outsiders[0] if outsiders else None,
            "context": str((context.get("name") or {}).get("value") or ""),
        }

    @pytest.mark.order(398)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_cluster", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_00_serving_cluster_is_the_zone_pair(self, serving_cluster: dict[str, Any]) -> None:
        """The context serving a colocation deployment lives on the firewall
        pair of its serving zone, not on some DC's cluster."""
        logging.info("=== %s - Step 0: Serving Cluster ===", SCENARIO_NAME)

        assert len(serving_cluster["members"]) == 2, f"serving cluster is not a pair: {serving_cluster['members']}"
        assert serving_cluster["members"] == serving_cluster["zone_firewalls"], (
            f"'{SOURCE_SEGMENT}' is served by cluster {serving_cluster['members']}, "
            f"expected its serving zone's firewalls {serving_cluster['zone_firewalls']}"
        )
        logging.info("Serving cluster: %s", serving_cluster["members"])

    @pytest.mark.order(399)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_check", depends=["all_demo_firewall_cluster"])
    @pytest.mark.asyncio
    async def test_01_check_firewall_passes(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        serving_cluster: dict[str, Any],
    ) -> None:
        """CheckFirewall must find no errors in the rules the serving firewall
        enforces — including seg-c001-nordix-prod-p-egress's rules, whose
        destination is a prefix only (no destination segment/zone)."""
        logging.info("=== %s - Step 1: CheckFirewall ===", SCENARIO_NAME)
        serving_firewall_device_name = serving_cluster["members"][0]

        check = CheckFirewall(
            branch=scenario_branch,
            client=async_client_main,
            params={"device": serving_firewall_device_name},
        )
        passed = await check.run()

        messages = [str(entry.get("message") or "") for entry in check.logs]
        for message in messages:
            logging.info("check: %s", message)

        errors = [str(entry.get("message") or "") for entry in check.logs if entry.get("level") == "ERROR"]
        assert passed and not errors, (
            f"CheckFirewall failed on branch '{scenario_branch}' (device={serving_firewall_device_name}):\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
        logging.info("CheckFirewall passed for device %s", serving_firewall_device_name)

    @pytest.mark.order(400)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_render", depends=["all_demo_firewall_check"])
    @pytest.mark.asyncio
    async def test_02_firewall_config_renders_colo_zone_policy_rules(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        serving_cluster: dict[str, Any],
    ) -> None:
        """Render the real firewall_config transform against live data on
        every member of the serving cluster and confirm the policy's rules and
        destination CIDRs made it all the way from the object files through
        the generator-populated graph, the firewall_config query, and
        get_zone_policies() into real device config text. A firewall outside
        the cluster on the same platform must not carry the rules: strict
        placement puts a rule only where its segment is served.

        The colocation context also carries transit legs, like a DC's: the FR
        pair is PAN-OS, so each member renders its layer3 sub-interfaces,
        vsys imports, zones, the vsys's own virtual router with the 0/0 via the
        INTERNET leg, the SNAT rules and the MANAGEMENT denies
        (_panos_context_leg_errors, the same wording as the DC11 steps)."""
        logging.info("=== %s - Step 2: Render firewall_config ===", SCENARIO_NAME)

        context = serving_cluster["context"]
        assert context, "serving context has no name"
        site_contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        site, namespace, inventory_context = next(
            (
                (site, namespace, candidate)
                for (site, namespace, tenant), candidate in site_contexts.items()
                if tenant is None and candidate["name"] == context
            ),
            ("", "", None),
        )
        assert inventory_context is not None and site in ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS, (
            f"serving context '{context}' is not a serving colocation zone's shared context"
        )
        expected = next(
            e
            for e in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS
            if e["site"] == site and e["namespace"] == namespace and e["tenant"] is None
        )
        platform_by_name = await _platform_by_name(async_client_main, scenario_branch)
        errors: list[str] = []
        for device in serving_cluster["members"]:
            rendered = await render_device_config(async_client_main, scenario_branch, device)
            if f"set vsys {context}" not in rendered:
                errors.append(f"{device}: no vsys '{context}' rendered")
            if platform_by_name.get(device) != "paloalto_panos":
                errors.append(f"{device}: platform '{platform_by_name.get(device)}', the FR pair is expected PAN-OS")
                continue
            if not any(leg["device"] == device for leg in inventory_context["legs"]):
                errors.append(f"{device}: no transit leg of '{context}' in the graph")
            errors.extend(_panos_context_leg_errors(device, rendered, inventory_context, expected))
            missing = [name for name in (*EXPECTED_RULE_NAMES, *EXPECTED_DESTINATION_CIDRS) if name not in rendered]
            if missing:
                errors.append(f"{device}: missing seg-c001-nordix-prod-p-egress rule(s)/CIDR(s) {missing}")

        outsider = serving_cluster["outsider"]
        assert outsider, f"no firewall outside {serving_cluster['members']} on their platform to render against"
        rendered = await render_device_config(async_client_main, scenario_branch, outsider)
        leaked = [name for name in EXPECTED_RULE_NAMES if name in rendered]
        if leaked:
            errors.append(f"{outsider}: does not serve '{SOURCE_SEGMENT}' but renders rule(s) {leaked}")

        assert not errors, f"firewall_config on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered firewall_config: rules on %s, none on %s", serving_cluster["members"], outsider)

    @pytest.mark.order(400)
    @pytest.mark.dependency(scope="session", depends=["all_demo_exchange_transit"])
    @pytest.mark.asyncio
    @pytest.mark.parametrize("dc", ["DC10", "DC11", "FR2"])
    async def test_03_check_firewall_passes_on_transit_dcs(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        dc: str,
    ) -> None:
        """CheckFirewall (incl. validate_exchange_gateways) passes on a member of
        DC10's Check Point pair, DC11's PAN-OS pair and the FR2 zone's PAN-OS
        pair: one local leg per exchange namespace, in a /29, tagged with the
        transit VLAN."""
        logging.info("=== %s - Step 3: CheckFirewall (%s transit legs) ===", SCENARIO_NAME, dc)

        (shared, *_) = _shared_contexts(
            _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch)), dc
        )
        member = sorted({str(leg["device"]) for leg in shared["legs"]})[0]

        check = CheckFirewall(branch=scenario_branch, client=async_client_main, params={"device": member})
        passed = await check.run()

        errors = [str(entry.get("message") or "") for entry in check.logs if entry.get("level") == "ERROR"]
        assert passed and not errors, f"CheckFirewall failed on {dc} member {member}:\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    @pytest.mark.order(400)
    @pytest.mark.dependency(
        scope="session", name="all_demo_firewall_transit_render", depends=["all_demo_exchange_transit"]
    )
    @pytest.mark.asyncio
    async def test_04_firewall_config_renders_transit_legs(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Every physical member of each site's shared contexts renders their legs.

        Per leg: the sub-interface, the firewall VIP (/29 .4) and, per vendor,
        the member address (Check Point) and the default route 0.0.0.0/0 via the
        INTERNET leg's border-leaf anycast (/29 .1). No context holds both PROD and
        NON-PROD (each tenant namespace has its own), so no PROD<->NON-PROD deny
        is rendered anywhere.
        """
        logging.info("=== %s - Step 4: Render transit legs ===", SCENARIO_NAME)

        contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        platform_by_name = await _platform_by_name(async_client_main, scenario_branch)

        errors: list[str] = []
        rendered_count = 0
        for expected in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS:
            if expected["tenant"] is not None:
                continue  # a dedicated context's members are virtual: not a firewall_config device
            dc = expected["site"]
            context = contexts.get((dc, expected["namespace"], None))
            if context is None:
                errors.append(f"{dc}/{expected['namespace']}: no shared context")
                continue
            internet_net = _leg_network(context, "INTERNET")
            internet_anycast = transit_addresses(str(internet_net))["anycast"]
            deny_names = [
                f"deny-{zone_name_for_namespace_type(a)}-to-{zone_name_for_namespace_type(b)}"
                for a, b in (("prod", "non_prod"), ("non_prod", "prod"))
            ]

            for device in sorted({str(leg["device"]) for leg in context["legs"]}):
                platform = platform_by_name.get(device)
                if platform not in ("paloalto_panos", "checkpoint_gaia"):
                    errors.append(f"{dc}/{device}: platform '{platform}' has no transit assertions in this test")
                    continue
                rendered = await render_device_config(async_client_main, scenario_branch, device)
                rendered_count += 1
                for leg in (leg for leg in context["legs"] if leg["device"] == device):
                    network = ipaddress.IPv4Interface(leg["address"]).network
                    vip = transit_addresses(str(network))["vip"]
                    if str(leg["interface"]) not in rendered:
                        errors.append(f"{dc}/{device}: leg interface {leg['interface']} not rendered")
                    if not _has_ip(rendered, vip):
                        errors.append(f"{dc}/{device}: VIP {vip} of {leg['namespace']} leg not rendered")
                    if platform == "checkpoint_gaia" and not _has_ip(rendered, str(leg["address"]).split("/")[0]):
                        errors.append(f"{dc}/{device}: member address {leg['address']} not rendered")
                default_route = (
                    f"static-route default nexthop gateway address {internet_anycast}"
                    if platform == "checkpoint_gaia"
                    else "destination 0.0.0.0/0"
                )
                if default_route not in rendered or not _has_ip(rendered, internet_anycast):
                    errors.append(
                        f"{dc}/{device}: no 0.0.0.0/0 via the INTERNET leg {internet_anycast} ({default_route})"
                    )
                for name in deny_names:
                    if name in rendered:
                        errors.append(f"{dc}/{device}: '{name}' present, no context holds both PROD and NON-PROD")

        assert rendered_count, "no firewall config was rendered"
        assert not errors, f"rendered transit legs on branch '{scenario_branch}' are wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered transit legs on %d firewall(s)", rendered_count)

    @pytest.mark.order(400)
    @pytest.mark.dependency(
        scope="session", name="all_demo_border_leaf_transit_render", depends=["all_demo_exchange_transit"]
    )
    @pytest.mark.asyncio
    async def test_05_border_leaf_config_renders_transit_routes(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Each DC11 border leaf carries the transit VRFs of its tagged contexts.

        The shared context (no tenant) is the default-route owner, so inside
        `vrf context PROD` there must be `ip route 0.0.0.0/0 <PROD leg VIP>`
        (inside the VRF, not a global `nh vrf`), every leg's transit VNI as a
        `vn-segment`, and the static routes redistributed into EVPN. `vrf context
        INTERNET` routes each context's SNAT /32 to its INTERNET leg VIP and carries
        no tenant (non-public) prefix.
        """
        logging.info("=== %s - Step 5: Render border-leaf transit routes ===", SCENARIO_NAME)

        contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        dc = "DC11"
        dc_contexts = [context for (context_dc, _, _), context in contexts.items() if context_dc == dc]
        shared = contexts[(dc, "PROD", None)]
        prod_vip = transit_addresses(str(_leg_network(shared, "PROD")))["vip"]
        transit_vnis = sorted(
            {
                transit_vni(transit_vlan(int(context["vlan"]), transit_namespace_type(str(leg["namespace"]))))
                for context in dc_contexts
                for leg in context["legs"]
            }
        )
        border_leafs = sorted({str(port["device"]) for context in dc_contexts for port in context["border_ports"]})
        assert border_leafs, f"{dc}: no border leaf carries a firewall context"

        errors: list[str] = []
        for device in border_leafs:
            rendered = await render_device_config(
                async_client_main, scenario_branch, device, query="border_leaf_config", transform_cls=BorderLeaf
            )
            for vrf in ("PROD", "INTERNET"):
                if f"vrf context {vrf}" not in rendered:
                    errors.append(f"{device}: no 'vrf context {vrf}'")
            block = re.search(r"^vrf context PROD\n((?:[ \t]+.*\n)*)", rendered + "\n", re.MULTILINE)
            if block is None or f"ip route 0.0.0.0/0 {prod_vip}" not in block.group(1):
                errors.append(f"{device}: 'ip route 0.0.0.0/0 {prod_vip}' is not inside 'vrf context PROD'")
            for vni in transit_vnis:
                if re.search(rf"vn-segment {vni}\b", rendered) is None:
                    errors.append(f"{device}: no 'vn-segment {vni}' (transit VNI)")
            if "redistribute static route-map RM-VRF-STATIC-2-EVPN-PROD" not in rendered:
                errors.append(f"{device}: PROD statics are not redistributed into EVPN")
            errors.extend(_management_border_leaf_errors(device, rendered, dc_contexts))
            internet_block = re.search(r"^vrf context INTERNET\n((?:[ \t]+.*\n)*)", rendered + "\n", re.MULTILINE)
            if internet_block is None or f"route-target both {INTERNET_ROUTE_TARGET}" not in internet_block.group(1):
                errors.append(
                    f"{device}: 'route-target both {INTERNET_ROUTE_TARGET}' is not inside 'vrf context INTERNET'"
                )
            if internet_block is None:
                continue
            # Return path: each served context's SNAT /32 -> its INTERNET leg VIP, and no tenant prefix.
            internet_routes = re.findall(r"^\s+ip route (\S+) (\S+)", internet_block.group(1), re.MULTILINE)
            for context in dc_contexts:
                snat = ipaddress.IPv4Interface(str(context["snat"])).ip
                vip = transit_addresses(str(_leg_network(context, "INTERNET")))["vip"]
                if (f"{snat}/32", vip) not in internet_routes:
                    errors.append(f"{device}: no 'ip route {snat}/32 {vip}' inside 'vrf context INTERNET'")
            for prefix, _nexthop in internet_routes:
                if prefix.split("/")[0] != "0.0.0.0" and any(
                    ipaddress.IPv4Network(prefix).subnet_of(ipaddress.IPv4Network(private))
                    for private in NON_INTERNET_PREFIXES
                ):
                    errors.append(f"{device}: tenant (non-public) prefix {prefix} routed inside 'vrf context INTERNET'")

        assert not errors, f"{dc} border-leaf config on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Border-leaf transit routes rendered on %s (transit VNIs %s)", border_leafs, transit_vnis)

    @pytest.mark.order(400)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_06_internet_egress_rule_renders_on_firewall_and_leaf(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """A permit to the prefix-less Internet segment (ALL_DEMO_INTERNET_EGRESS_RULES).

        Firewall: every member of the DC's shared context renders the rule in
        that context's vsys from the source's zone to INTERNET-ZONE, source the
        segment's prefix, destination any. Leaf: the ACL of the source segment's
        VLAN denies every NON_INTERNET_PREFIXES range from the segment before
        the `permit ... any`, and has no `permit <proto> <src> any` ahead of
        the last of those denies.
        """
        logging.info("=== %s - Step 6: Internet egress rule ===", SCENARIO_NAME)

        contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        errors: list[str] = []
        for rule_name, rule in ALL_DEMO_INTERNET_EGRESS_RULES.items():
            dc = rule["dc"]
            source = await _segment_on_switches(async_client_main, scenario_branch, rule["source_segment"])
            prefix = source.prefix

            # --- firewall: the shared context of the DC ---
            context = contexts[(dc, "PROD", None)]  # the rule's source segment is a PROD one
            members = sorted({str(leg["device"]) for leg in context["legs"]})
            assert members, f"{dc}: the shared context has no firewall member"
            base = rf"^set vsys {re.escape(str(context['name']))} rulebase security rules {re.escape(rule_name)}"
            for device in members:
                rendered = await render_device_config(async_client_main, scenario_branch, device)
                expected = {
                    "to": rf"{base} to {re.escape(rule['destination_zone'])}$",
                    "from": rf"{base} from \S+$",
                    "source": rf"{base} source {re.escape(prefix)}$",
                    "destination": rf"{base} destination any$",
                    "service": rf"{base} service svc-{rule['protocol']}-{rule['port']}$",
                    "action": rf"{base} action allow$",
                }
                for part, pattern in expected.items():
                    if re.search(pattern, rendered, re.MULTILINE) is None:
                        errors.append(f"{dc}/{device}: no rendered '{rule_name}' {part} line matching /{pattern}/")

            # --- leaf: the ACL of the source segment's VLAN ---
            topology = await fetch_dc_topology(async_client_main, scenario_branch, dc)
            leaf_classes = {"leaf": Leaf, "access-leaf": AccessLeaf}
            # The ACL is named after the segment's VLAN on each switch (ACL-VLAN<id>-IN).
            with_vlan: list[str] = []
            seen_acls: set[str] = set()
            acls: dict[str, list[dict[str, str]]] = {}
            for device in sorted(topology["devices"], key=lambda d: str(d["name"])):
                if device.get("role") not in leaf_classes:
                    continue
                device_name = str(device["name"])
                rendered = await render_device_config(
                    async_client_main,
                    scenario_branch,
                    device_name,
                    query="leaf_config",
                    transform_cls=leaf_classes[str(device["role"])],
                )
                seen_acls.update(re.findall(r"^ip access-list (\S+)", rendered, re.MULTILINE))
                vlan_id = source.vlan_on(device_name)
                if vlan_id is None:
                    continue
                if re.search(rf"^vlan {vlan_id}$", rendered, re.MULTILINE):
                    with_vlan.append(f"{device_name} (vlan {vlan_id})")
                block = re.search(rf"^ip access-list ACL-VLAN{vlan_id}-IN\n((?:[ \t]+.*\n)*)", rendered, re.MULTILINE)
                if block is not None:
                    acls[device_name] = [
                        match.groupdict() for line in block.group(1).splitlines() if (match := _ACL_LINE.match(line))
                    ]
            if not acls:
                errors.append(
                    f"{dc}: no leaf or access-leaf renders the ACL of '{rule['source_segment']}' "
                    f"(its VLAN per switch: {source.vlans_by_device or source.vlan_id}; devices carrying it: "
                    f"{with_vlan}; ACL names on the {dc} leafs: {sorted(seen_acls)})"
                )
            for device, lines in acls.items():
                denies = [
                    index
                    for index, line in enumerate(lines)
                    if line["action"] == "deny" and line["src"] == prefix and line["dst"] in NON_INTERNET_PREFIXES
                ]
                denied = {lines[index]["dst"] for index in denies}
                if denied != set(NON_INTERNET_PREFIXES):
                    errors.append(f"{device}: denies from {prefix} miss {sorted(set(NON_INTERNET_PREFIXES) - denied)}")
                permits_any = [
                    index
                    for index, line in enumerate(lines)
                    if line["action"] == "permit"
                    and line["src"] == prefix
                    and line["dst"] == "any"
                    and line["rest"].split()[:2] == ["eq", str(rule["port"])]
                ]
                if not permits_any:
                    errors.append(f"{device}: no 'permit {rule['protocol']} {prefix} any eq {rule['port']}' in the ACL")
                elif denies and min(permits_any) < max(denies):
                    errors.append(f"{device}: 'permit {prefix} any' precedes a deny of an internal range")
                unconditional = [
                    line
                    for line in lines
                    if line["action"] == "permit" and line["src"] == prefix and line["dst"] == "any"
                ]
                if any(re.search(r"\b(eq|range)\b", line["rest"]) is None for line in unconditional):
                    errors.append(f"{device}: unconditional 'permit ... {prefix} any' (no port) in the ACL")

        assert not errors, f"internet egress rule on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    @pytest.mark.order(401)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_06_firewall_config_renders_snat(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Every member of each DC's shared context source-NATs its tenant zones to its SNAT address.

        PAN-OS (DC11): one `rulebase nat rules snat-<zone>` per tenant zone with
        `source-translation dynamic-ip-and-port translated-address <snat>`, and the
        SNAT address as secondary IP on the INTERNET unit. Check Point (DC10, DC12):
        the SNAT host object and a `mgmt_cli add nat-rule ... translated-source
        "SNAT-<context>"` line per tenant zone.
        """
        logging.info("=== %s - Step 6: Render firewall SNAT ===", SCENARIO_NAME)

        contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        platform_by_name = await _platform_by_name(async_client_main, scenario_branch)

        errors: list[str] = []
        rendered_by_platform: dict[str, int] = {}
        for expected in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS:
            if expected["tenant"] is not None:
                continue  # a dedicated context's members are virtual: not a firewall_config device
            dc = expected["site"]
            context = contexts.get((dc, expected["namespace"], None))
            if context is None or not context["snat"]:
                errors.append(f"{dc}/{expected['namespace']}: no shared context with a snat_address")
                continue
            name = str(context["name"])
            snat = str(ipaddress.IPv4Interface(str(context["snat"])).ip)
            zones = [
                zone_name_for_namespace_type(transit_namespace_type(namespace))
                for namespace in expected["namespaces"]
                if transit_namespace_type(namespace) in EXCHANGE_PEERS  # tenant VRFs only; INTERNET-only SNAT /32
            ]
            for device in sorted({str(leg["device"]) for leg in context["legs"]}):
                platform = platform_by_name.get(device)
                if platform not in ("paloalto_panos", "checkpoint_gaia"):
                    errors.append(f"{dc}/{device}: platform '{platform}' has no SNAT assertions in this test")
                    continue
                rendered = await render_device_config(async_client_main, scenario_branch, device)
                rendered_by_platform[str(platform)] = rendered_by_platform.get(str(platform), 0) + 1
                if platform == "paloalto_panos":
                    for zone in zones:
                        base = f"set vsys {name} rulebase nat rules snat-{zone}"
                        if f"{base} source-translation dynamic-ip-and-port translated-address {snat}" not in rendered:
                            errors.append(f"{dc}/{device}: no PAN-OS SNAT rule for zone {zone} translating to {snat}")
                        if f"{base} to INTERNET-ZONE" not in rendered:
                            errors.append(f"{dc}/{device}: SNAT rule for zone {zone} does not go to INTERNET-ZONE")
                    if not re.search(rf"layer3 units \S+ ip {re.escape(snat)}/32$", rendered, re.MULTILINE):
                        errors.append(f"{dc}/{device}: {snat}/32 is not a secondary IP on the INTERNET unit")
                else:
                    if f'mgmt_cli add host name "SNAT-{name}" ip-address "{snat}"' not in rendered:
                        errors.append(f"{dc}/{device}: no SNAT host object SNAT-{name} = {snat}")
                    nat_rules = [
                        line
                        for line in rendered.splitlines()
                        if line.startswith("mgmt_cli add nat-rule") and f'translated-source "SNAT-{name}"' in line
                    ]
                    if len(nat_rules) != len(zones):
                        errors.append(
                            f"{dc}/{device}: {len(nat_rules)} nat-rule line(s) for SNAT-{name}, {len(zones)} expected"
                        )

        assert {"paloalto_panos", "checkpoint_gaia"} <= set(rendered_by_platform), (
            f"SNAT was rendered on {rendered_by_platform}, expected PAN-OS and Check Point"
        )
        assert not errors, f"Firewall SNAT on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered SNAT on %s", rendered_by_platform)

    @pytest.mark.order(401)
    @pytest.mark.dependency(scope="session", depends=["all_demo_border_leaf_transit_render"])
    @pytest.mark.asyncio
    async def test_06_edge_config_renders_internet_upstream(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """All four colocation edges are BGWs: INTERNET type-5 upstream, and the FABRIC pair session.

        Every edge (ALL_DEMO_INTERNET_BGWS: eg-fr201/202, eg-pa401/402) carries the
        INTERNET L3 VNI and the global route-target in `vrf context INTERNET`,
        advertises it into EVPN under `vrf INTERNET`, and filters the transit
        session (default-only in, only the public SNAT block out, aggregated
        with summary-only). Each edge also peers its zone partner over the
        interlink: one `FABRIC-PEERS` neighbour (iBGP, remote-as = the site AS,
        no peer-type, no update-source, `RM-FABRIC-OUT` out), `evpn multisite
        fabric-tracking` on the interlink port only and `dci-tracking` on the
        dark-fibre ports. The two sessions add_virtual_circuit builds per DCI circuit render as one
        DCI-UNDERLAY-PEERS and one DCI-PEERS neighbour each (_dci_session_errors).
        """
        logging.info("=== %s - Step 6: Render edge INTERNET upstream ===", SCENARIO_NAME)

        errors: list[str] = []
        for device in sorted(ALL_DEMO_INTERNET_BGWS):
            rendered = await render_device_config(
                async_client_main, scenario_branch, device, query="edge_config", transform_cls=Edge
            )

            for needle in (
                "route-map RM-TRANSIT-IN in",
                "route-map RM-TRANSIT-OUT out",
                "route-map RM-TRANSIT-OUT deny",
            ):
                if needle not in rendered:
                    errors.append(f"{device}: no '{needle}'")
            if not re.search(r"ip prefix-list \S+ seq \d+ permit 0\.0\.0\.0/0\n", rendered):
                errors.append(f"{device}: no default-only prefix-list")
            errors += _fabric_pair_errors(device, rendered)
            errors += _dci_session_errors(
                device,
                rendered,
                sum(any(end == device for end, _ in ends) for _, ends in ALL_DEMO_DCI_CIRCUITS.values()),
            )

            # The public block goes upstream as one summary and nothing else leaves (every edge).
            if not re.search(
                rf"ip prefix-list PL-TRANSIT-OUT-PUBLIC seq \d+ permit {re.escape(ALL_DEMO_SNAT_POOL_PREFIX)}\n",
                rendered,
            ):
                errors.append(f"{device}: PL-TRANSIT-OUT-PUBLIC does not permit {ALL_DEMO_SNAT_POOL_PREFIX}")
            if not re.search(
                r"route-map RM-TRANSIT-OUT permit \d+\n\s+match ip address prefix-list PL-TRANSIT-OUT-PUBLIC\n",
                rendered,
            ):
                errors.append(f"{device}: RM-TRANSIT-OUT does not permit PL-TRANSIT-OUT-PUBLIC")

            vrf_context = re.search(r"^vrf context INTERNET\n((?:[ \t]+.*\n)*)", rendered + "\n", re.MULTILINE)
            bgp_vrf = re.search(r"^[ \t]+vrf INTERNET\n((?:[ \t]{4,}.*\n)*)", rendered + "\n", re.MULTILINE)
            if vrf_context is None:
                errors.append(f"{device}: no 'vrf context INTERNET'")
                continue
            has_vni = re.search(rf"^[ \t]+vni {INTERNET_L3_VNI}\b", vrf_context.group(1), re.MULTILINE) is not None
            if bgp_vrf is None or f"aggregate-address {ALL_DEMO_SNAT_POOL_PREFIX} summary-only" not in bgp_vrf.group(1):
                errors.append(
                    f"{device}: no 'aggregate-address {ALL_DEMO_SNAT_POOL_PREFIX} summary-only' inside 'vrf INTERNET'"
                )
            if not has_vni:
                errors.append(f"{device}: no 'vni {INTERNET_L3_VNI}' inside 'vrf context INTERNET'")
            if f"route-target both {INTERNET_ROUTE_TARGET}" not in vrf_context.group(1):
                errors.append(f"{device}: no 'route-target both {INTERNET_ROUTE_TARGET}' inside 'vrf context INTERNET'")
            if bgp_vrf is None or "advertise l2vpn evpn" not in bgp_vrf.group(1):
                errors.append(f"{device}: no 'advertise l2vpn evpn' inside 'vrf INTERNET' of the BGP process")

        assert not errors, f"Edge INTERNET upstream on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    @pytest.mark.order(401)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_06_edge_config_renders_firewall_transit_legs(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Each FR2 edge renders the shared context's transit legs; the PA edges render none.

        The edges are the border leafs' counterpart for a colocation context (a trunk of the
        transit VLANs on the firewall-role port, transit L2VNI, anycast SVI per VRF, the 0/0 and SNAT
        /32 statics inside their `vrf context`); see _edge_transit_errors. The statics stay local:
        INTERNET's SNAT /32 is redistributed into EVPN, PROD's 0/0 is not. The edges of a zone
        without a firewall pair carry no transit VLAN, SVI or static-route map.
        """
        logging.info("=== %s - Step 6: Render edge firewall transit legs ===", SCENARIO_NAME)

        contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        errors: list[str] = []
        served_edges: set[str] = set()
        for site in ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS:
            site_shared = _shared_contexts(contexts, site)
            assert site_shared, f"{site}: no shared firewall context"
            for context in site_shared:
                edges = sorted({str(port["device"]) for port in context["border_ports"]})
                assert len(edges) == 2, f"{site}: service ports on {edges}, expected both edges of the zone"
                served_edges.update(edges)
                for device in edges:
                    rendered = await render_device_config(
                        async_client_main, scenario_branch, device, query="edge_config", transform_cls=Edge
                    )
                    errors.extend(_edge_transit_errors(device, rendered, context))

        for device in sorted(set(ALL_DEMO_INTERNET_BGWS) - served_edges):
            rendered = await render_device_config(
                async_client_main, scenario_branch, device, query="edge_config", transform_cls=Edge
            )
            if re.search(r"^interface Vlan3[0-7]\d{2}$", rendered, re.MULTILINE):
                errors.append(f"{device}: renders a transit SVI, its zone has no firewall pair")
            if "switchport trunk allowed vlan" in rendered or "PL-VRF-STATIC" in rendered:
                errors.append(f"{device}: renders a transit trunk or exchange static, its zone has no firewall pair")

        assert served_edges, "no edge carries a firewall context"
        assert not errors, f"Edge firewall transit legs on branch '{scenario_branch}' are wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered edge transit legs on %s", sorted(served_edges))
