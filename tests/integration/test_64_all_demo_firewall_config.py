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
device-agnostic: place_policies_in_contexts() (transforms/helpers/firewall.py)
gives a context only the rules of the segments of the deployments it serves,
so the render runs on every member of the cluster serving the policy's source
segment (c001-nordix-prod-p, deployment C001-P-FR — the FR metro's firewall
pair), whose rules are in that segment's own policy
(seg-c001-nordix-prod-p-egress), and once on a firewall outside it that must
not carry the rules.

The same module then covers the inter-VRF exchange legs the customer boarding
generators build through each DC's firewall contexts (utils/exchange_transit.py):
CheckFirewall on a DC10 (Check Point) and DC11 (PAN-OS) member, the rendered
firewall config of every DC (leg interface, VIP, default route via the INTERNET
leg, PROD/NON-PROD isolation), and a DC11 border-leaf config carrying the
transit VRF routes. Everything is derived from the loaded graph and from
test_constants' environment-driven expectations.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient

from checks.firewall import CheckFirewall
from generators.protocols import DcimPhysicalDevice
from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.edge import Edge
from transforms.config.firewall import Firewall
from transforms.config.leaf import Leaf
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    MANAGEMENT_TYPE,
    NON_PUBLIC_PREFIXES,
    transit_addresses,
    transit_vlan,
    transit_vni,
    zone_name_for_namespace_type,
)
from utils.internet_space import NON_INTERNET_PREFIXES

from .conftest import TestInfrahubDockerWithClient
from .test_constants import (
    ALL_DEMO_BRANCH,
    ALL_DEMO_DC_NAMES,
    ALL_DEMO_DEDICATED_FIREWALL_TENANTS,
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    ALL_DEMO_INTERNET_BGWS,
    ALL_DEMO_INTERNET_EGRESS_RULES,
    ALL_DEMO_INTERNET_ISLAND_EDGES,
    ALL_DEMO_MANAGEMENT_RULES,
    ALL_DEMO_SNAT_POOL_PREFIX,
    INTERNET_L3_VNI,
    INTERNET_ROUTE_TARGET,
    MANAGEMENT_CLIENT_SEGMENT_PREFIX,
    MANAGEMENT_CLIENT_SEGMENT_VLAN,
    MANAGEMENT_NAMESPACE,
    MANAGEMENT_SERVICES_DC,
    MANAGEMENT_SERVICES_GATEWAY,
    MANAGEMENT_SERVICES_LEAF,
    MANAGEMENT_SERVICES_PREFIX,
    MANAGEMENT_SERVICES_VLAN,
    transit_namespace_type,
)
from .test_helpers import fetch_dc_topology, fetch_transit_inventory, match_transit_contexts

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: Colo Cloud/Partner/SaaS Zone Policy"

# The three destination CIDRs from 07_zone_policies/04_destination_prefixes.yml,
# each backing one SecurityPolicyRule in 07_zone_policies/05_security_policy.yml.
EXPECTED_DESTINATION_CIDRS = ("10.40.0.0/16", "198.18.0.0/24", "198.51.100.0/24")

# The rule names from 05_security_policy.yml. Unlike the CIDRs, nothing else in
# the branch carries them, so their absence from a config proves the policy was
# not placed there.
EXPECTED_RULE_NAMES = ("nordix-prod-to-aws", "nordix-prod-to-partner-acme", "nordix-prod-to-saas-zscaler")

# The policy's source segment -> its colocation deployment -> the serving
# context's HA cluster, plus the firewalls of the metro the deployment sits in.
SOURCE_SEGMENT = "c001-nordix-prod-p"
SERVING_CLUSTER_QUERY = """
query ($segment: String!) {
  ManagedVlanSegment(name__value: $segment) {
    edges {
      node {
        customer_deployment {
          node {
            ... on ManagedTenantScoped {
              serving_firewall_context { node { name { value } cluster { node { id } } } }
            }
            ... on TopologyCustomerColocation {
              parent {
                node {
                  ... on TopologyColocationMetro {
                    devices(role__value: "firewall") { edges { node { name { value } } } }
                  }
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

FIREWALLS_QUERY = """
query {
  DcimPhysicalDevice(role__value: "firewall") {
    edges { node { name { value } platform { node { name { value } } } } }
  }
}
"""

SEGMENT_QUERY = """
query ($name: String!) {
  ManagedNetworkSegment(name__value: $name) {
    edges {
      node {
        ... on ManagedVlanSegment { vlan_id { value } }
        gateway { node { ip_prefix { node { prefix { value } } } } }
      }
    }
  }
}
"""

_ACL_LINE = re.compile(
    r"^\s+(?P<seq>\d+) (?P<action>permit|deny) (?P<proto>\S+) (?P<src>\S+) (?P<dst>\S+)(?P<rest>.*)$"
)

_ROOT = str(Path(__file__).parent.parent.parent)


async def _render(
    client: InfrahubClient,
    branch: str,
    device: str,
    query: str = "firewall_config",
    transform_cls: type[Any] = Firewall,
) -> str:
    """Run a device-config query and its transform in-process, return the rendered config."""
    raw = await client.query_gql_query(name=query, branch_name=branch, variables={"device": device})
    data: dict[str, Any] = raw.get("data") or raw

    transform = transform_cls.__new__(transform_cls)
    transform.root_directory = _ROOT
    rendered = await transform.transform(data)
    assert isinstance(rendered, str) and rendered, f"{query} transform produced no output for device {device}"
    return rendered


def _has_ip(rendered: str, ip: str) -> bool:
    """Whether `ip` appears in `rendered` as a whole address (10.0.0.1 is not in 10.0.0.12 / 110.0.0.1)."""
    return re.search(rf"(?<![\d.]){re.escape(ip)}(?![\d])", rendered) is not None


def _block(rendered: str, header: str) -> str | None:
    """The indented lines under the line `header` (an NX-OS `vrf context X` / `interface VlanN` stanza), None if absent."""
    match = re.search(rf"^{re.escape(header)}\n((?:[ \t]+.*\n)*)", rendered + "\n", re.MULTILINE)
    return None if match is None else match.group(1)


async def _platform_by_name(client: InfrahubClient, branch: str) -> dict[str, str | None]:
    """Every firewall device name -> its platform name."""
    firewalls = await client.execute_graphql(query=FIREWALLS_QUERY, branch_name=branch)
    return {
        edge["node"]["name"]["value"]: (((edge["node"].get("platform") or {}).get("node") or {}).get("name") or {}).get(
            "value"
        )
        for edge in firewalls["DcimPhysicalDevice"]["edges"]
    }


def _management_border_leaf_errors(device: str, rendered: str, contexts: list[dict[str, Any]]) -> list[str]:
    """What a border leaf must carry for the MANAGEMENT exchange of `contexts`.

    `vrf context MANAGEMENT` exists, each context's MANAGEMENT transit SVI (VLAN = transit_vlan(ctx VLAN,
    management), anycast .1 of the leg /29) is in that VRF, and no tenant (non-public) prefix is routed
    inside the VRF: tenants are source-NATed behind the leg VIP, so the leaf needs no return route.
    """
    errors: list[str] = []
    vrf_block = _block(rendered, f"vrf context {MANAGEMENT_NAMESPACE}")
    if vrf_block is None:
        return [f"{device}: no 'vrf context {MANAGEMENT_NAMESPACE}'"]
    for prefix, nexthop in re.findall(r"^\s+ip route (\S+) (\S+)", vrf_block, re.MULTILINE):
        if any(ipaddress.IPv4Network(prefix).subnet_of(private) for private in NON_PUBLIC_PREFIXES):
            errors.append(
                f"{device}: 'ip route {prefix} {nexthop}' inside 'vrf context {MANAGEMENT_NAMESPACE}' "
                "(tenants are NATed behind the leg VIP, no return route)"
            )
    for context in contexts:
        vlan = transit_vlan(int(context["vlan"]), MANAGEMENT_TYPE)
        anycast = transit_addresses(str(_leg_network(context, MANAGEMENT_NAMESPACE)))["anycast"]
        svi = _block(rendered, f"interface Vlan{vlan}")
        if svi is None:
            errors.append(f"{device}: no 'interface Vlan{vlan}' (MANAGEMENT transit of {context['name']})")
        elif f"vrf member {MANAGEMENT_NAMESPACE}" not in svi or f"ip address {anycast}/29" not in svi:
            errors.append(
                f"{device}: Vlan{vlan} is not 'vrf member {MANAGEMENT_NAMESPACE}' with 'ip address {anycast}/29'"
            )
    return errors


def _dc_contexts(inventory: dict[str, list[dict[str, Any]]]) -> dict[tuple[str, str | None], dict[str, Any]]:
    """The 30_all DC firewall contexts keyed (dc, tenant), tenant None for a DC's shared one."""
    by_key, _ = match_transit_contexts(inventory["contexts"], ALL_DEMO_DC_NAMES, ALL_DEMO_DEDICATED_FIREWALL_TENANTS)
    return by_key


def _leg_network(context: dict[str, Any], namespace: str) -> ipaddress.IPv4Network:
    """The /29 of a context's leg in `namespace` (taken from its first member's address)."""
    leg = next(leg for leg in context["legs"] if leg["namespace"] == namespace)
    return ipaddress.IPv4Interface(leg["address"]).network


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
        segment: {"members", "metro_firewalls", "outsider", "context"}, names sorted.
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
        metro = (deployment.get("parent") or {}).get("node") or {}
        metro_firewalls = sorted(
            edge["node"]["name"]["value"] for edge in (metro.get("devices") or {}).get("edges", [])
        )

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
            "metro_firewalls": metro_firewalls,
            "outsider": outsiders[0] if outsiders else None,
            "context": str((context.get("name") or {}).get("value") or ""),
        }

    @pytest.mark.order(398)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_cluster", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_00_serving_cluster_is_the_metro_pair(self, serving_cluster: dict[str, Any]) -> None:
        """The context serving a colocation deployment lives on the firewall
        pair of the metro the deployment sits in, not on some DC's cluster."""
        logging.info("=== %s - Step 0: Serving Cluster ===", SCENARIO_NAME)

        assert len(serving_cluster["members"]) == 2, f"serving cluster is not a pair: {serving_cluster['members']}"
        assert serving_cluster["members"] == serving_cluster["metro_firewalls"], (
            f"'{SOURCE_SEGMENT}' is served by cluster {serving_cluster['members']}, "
            f"expected its metro's firewalls {serving_cluster['metro_firewalls']}"
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

        The colocation context is policy-only: its vsys renders, but it has no
        data-plane leg, so the config carries no interface unit, no import of
        an interface into the vsys and no virtual router for it."""
        logging.info("=== %s - Step 2: Render firewall_config ===", SCENARIO_NAME)

        context = serving_cluster["context"]
        assert context, "serving context has no name"
        errors: list[str] = []
        for device in serving_cluster["members"]:
            rendered = await _render(async_client_main, scenario_branch, device)
            if f"set vsys {context}" not in rendered:
                errors.append(f"{device}: no vsys '{context}' rendered")
            leg_lines = [
                line
                for line in rendered.splitlines()
                if (f"vsys {context} import network" in line)
                or (f"vsys {context} zone" in line and "network layer3" in line)
                or (f"virtual-router vr-{context}" in line)
                or ("set network interface ethernet" in line and " layer3 units " in line)
            ]
            if leg_lines:
                errors.append(f"{device}: policy-only context '{context}' renders leg line(s) {leg_lines}")
            missing = [name for name in (*EXPECTED_RULE_NAMES, *EXPECTED_DESTINATION_CIDRS) if name not in rendered]
            if missing:
                errors.append(f"{device}: missing seg-c001-nordix-prod-p-egress rule(s)/CIDR(s) {missing}")

        outsider = serving_cluster["outsider"]
        assert outsider, f"no firewall outside {serving_cluster['members']} on their platform to render against"
        rendered = await _render(async_client_main, scenario_branch, outsider)
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
    @pytest.mark.parametrize("dc", ["DC10", "DC11"])
    async def test_03_check_firewall_passes_on_transit_dcs(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        dc: str,
    ) -> None:
        """CheckFirewall (incl. validate_exchange_gateways) passes on a member of
        DC10's Check Point pair and DC11's PAN-OS pair: one local leg per
        exchange namespace, in a /29, tagged with the transit VLAN."""
        logging.info("=== %s - Step 3: CheckFirewall (%s transit legs) ===", SCENARIO_NAME, dc)

        shared = _dc_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))[
            (dc, None)
        ]
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
        """Every physical member of each DC's shared context renders its legs.

        Per leg: the sub-interface, the firewall VIP (/29 .4) and, per vendor,
        the member address (Check Point) and the default route 0.0.0.0/0 via the
        INTERNET leg's border-leaf anycast (/29 .1). A context that holds both
        PROD and NON-PROD (DC12's shared one) gets the logged PROD<->NON-PROD
        deny; the others must not.
        """
        logging.info("=== %s - Step 4: Render transit legs ===", SCENARIO_NAME)

        contexts = _dc_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        platform_by_name = await _platform_by_name(async_client_main, scenario_branch)

        errors: list[str] = []
        rendered_count = 0
        for expected in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS:
            if expected["tenant"] is not None:
                continue  # a dedicated context's members are virtual: not a firewall_config device
            dc = expected["dc"]
            context = contexts.get((dc, None))
            if context is None:
                errors.append(f"{dc}: no shared context")
                continue
            internet_net = _leg_network(context, "INTERNET")
            internet_anycast = transit_addresses(str(internet_net))["anycast"]
            deny_names = [
                f"deny-{zone_name_for_namespace_type(a)}-to-{zone_name_for_namespace_type(b)}"
                for a, b in (("prod", "non_prod"), ("non_prod", "prod"))
            ]
            isolated = {"PROD", "NON-PROD"} <= set(expected["namespaces"])

            for device in sorted({str(leg["device"]) for leg in context["legs"]}):
                platform = platform_by_name.get(device)
                if platform not in ("panos", "checkpoint_gaia"):
                    errors.append(f"{dc}/{device}: platform '{platform}' has no transit assertions in this test")
                    continue
                rendered = await _render(async_client_main, scenario_branch, device)
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
                    if (name in rendered) != isolated:
                        errors.append(
                            f"{dc}/{device}: '{name}' {'missing' if isolated else 'present'}, "
                            f"VRFs {sorted(expected['namespaces'])}"
                        )

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

        contexts = _dc_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        dc = "DC11"
        dc_contexts = [context for (context_dc, _), context in contexts.items() if context_dc == dc]
        shared = contexts[(dc, None)]
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
            rendered = await _render(
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
                    ipaddress.IPv4Network(prefix).subnet_of(private) for private in NON_PUBLIC_PREFIXES
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

        contexts = _dc_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        errors: list[str] = []
        for rule_name, rule in ALL_DEMO_INTERNET_EGRESS_RULES.items():
            dc = rule["dc"]
            segments = (
                await async_client_main.execute_graphql(
                    query=SEGMENT_QUERY, variables={"name": rule["source_segment"]}, branch_name=scenario_branch
                )
            )["ManagedNetworkSegment"]["edges"]
            assert segments, f"segment '{rule['source_segment']}' not found on branch '{scenario_branch}'"
            segment = segments[0]["node"]
            prefix = segment["gateway"]["node"]["ip_prefix"]["node"]["prefix"]["value"]
            vlan_id = segment["vlan_id"]["value"]

            # --- firewall: the shared context of the DC ---
            context = contexts[(dc, None)]
            members = sorted({str(leg["device"]) for leg in context["legs"]})
            assert members, f"{dc}: the shared context has no firewall member"
            base = rf"^set vsys {re.escape(str(context['name']))} rulebase security rules {re.escape(rule_name)}"
            for device in members:
                rendered = await _render(async_client_main, scenario_branch, device)
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
            with_vlan: list[str] = []
            seen_acls: set[str] = set()
            acls: dict[str, list[dict[str, str]]] = {}
            for device in sorted(topology["devices"], key=lambda d: str(d["name"])):
                if device.get("role") not in leaf_classes:
                    continue
                rendered = await _render(
                    async_client_main,
                    scenario_branch,
                    str(device["name"]),
                    query="leaf_config",
                    transform_cls=leaf_classes[str(device["role"])],
                )
                seen_acls.update(re.findall(r"^ip access-list (\S+)", rendered, re.MULTILINE))
                if re.search(rf"^vlan {vlan_id}$", rendered, re.MULTILINE):
                    with_vlan.append(str(device["name"]))
                block = re.search(rf"^ip access-list ACL-VLAN{vlan_id}-IN\n((?:[ \t]+.*\n)*)", rendered, re.MULTILINE)
                if block is not None:
                    acls[str(device["name"])] = [
                        match.groupdict() for line in block.group(1).splitlines() if (match := _ACL_LINE.match(line))
                    ]
            if not acls:
                errors.append(
                    f"{dc}: no leaf or access-leaf renders ACL-VLAN{vlan_id}-IN for '{rule['source_segment']}' "
                    f"(devices carrying vlan {vlan_id}: {with_vlan}; ACL names on the DC11 leafs: {sorted(seen_acls)})"
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

        contexts = _dc_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        platform_by_name = await _platform_by_name(async_client_main, scenario_branch)

        errors: list[str] = []
        rendered_by_platform: dict[str, int] = {}
        for expected in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS:
            if expected["tenant"] is not None:
                continue  # a dedicated context's members are virtual: not a firewall_config device
            dc = expected["dc"]
            context = contexts.get((dc, None))
            if context is None or not context["snat"]:
                errors.append(f"{dc}: no shared context with a snat_address")
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
                if platform not in ("panos", "checkpoint_gaia"):
                    errors.append(f"{dc}/{device}: platform '{platform}' has no SNAT assertions in this test")
                    continue
                rendered = await _render(async_client_main, scenario_branch, device)
                rendered_by_platform[str(platform)] = rendered_by_platform.get(str(platform), 0) + 1
                if platform == "panos":
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

        assert {"panos", "checkpoint_gaia"} <= set(rendered_by_platform), (
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
        """The metro BGWs export the ISP default route as EVPN type-5; the island edges only filter it.

        eg-fr01 / eg-pa01 (ALL_DEMO_INTERNET_BGWS) carry the INTERNET L3 VNI and
        the global route-target in `vrf context INTERNET`, advertise it into
        EVPN under `vrf INTERNET`, and filter the transit session (default-only
        in, only the public SNAT block out, aggregated with summary-only). eg-fr02 / eg-pa02 (ALL_DEMO_INTERNET_ISLAND_EDGES) are
        no BGW: same transit filters, no VNI.
        """
        logging.info("=== %s - Step 6: Render edge INTERNET upstream ===", SCENARIO_NAME)

        errors: list[str] = []
        for device in sorted(ALL_DEMO_INTERNET_BGWS | ALL_DEMO_INTERNET_ISLAND_EDGES):
            rendered = await _render(
                async_client_main, scenario_branch, device, query="edge_config", transform_cls=Edge
            )
            bgw = device in ALL_DEMO_INTERNET_BGWS

            for needle in (
                "route-map RM-TRANSIT-IN in",
                "route-map RM-TRANSIT-OUT out",
                "route-map RM-TRANSIT-OUT deny",
            ):
                if needle not in rendered:
                    errors.append(f"{device}: no '{needle}'")
            if not re.search(r"ip prefix-list \S+ seq \d+ permit 0\.0\.0\.0/0\n", rendered):
                errors.append(f"{device}: no default-only prefix-list")

            # The public block goes upstream as one summary and nothing else leaves (islands too).
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
            if not bgw:
                if re.search(rf"\bvni {INTERNET_L3_VNI}\b", rendered):
                    errors.append(f"{device}: island edge renders vni {INTERNET_L3_VNI}")
                continue
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

        contexts = _dc_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        platform_by_name = await _platform_by_name(async_client_main, scenario_branch)
        management_zone = zone_name_for_namespace_type(MANAGEMENT_TYPE)

        errors: list[str] = []
        rendered_by_platform: dict[str, int] = {}
        for expected in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS:
            if expected["tenant"] is not None:
                continue  # a dedicated context's members are virtual: not a firewall_config device
            dc = expected["dc"]
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
            has_services = dc == MANAGEMENT_SERVICES_DC
            for device in sorted({str(leg["device"]) for leg in context["legs"]}):
                platform = platform_by_name.get(device)
                if platform not in ("panos", "checkpoint_gaia"):
                    errors.append(f"{dc}/{device}: platform '{platform}' has no MANAGEMENT assertions in this test")
                    continue
                rendered = await _render(async_client_main, scenario_branch, device)
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
                    if has_services:
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
                    if (route in rendered) != has_services:
                        errors.append(f"{dc}/{device}: '{route}' {'missing' if has_services else 'present'}")
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
        shared context's vsys on every member: source 10.6.103.0/24, destination 10.250.0.0/24, service
        svc-udp-<port>, action allow.
        """
        logging.info("=== %s - Step 7: Render core DNS/NTP rules ===", SCENARIO_NAME)

        context = _dc_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))[
            (MANAGEMENT_SERVICES_DC, None)
        ]
        members = sorted({str(leg["device"]) for leg in context["legs"]})
        assert members, f"{MANAGEMENT_SERVICES_DC}: the shared context has no firewall member"
        errors: list[str] = []
        for device in members:
            rendered = await _render(async_client_main, scenario_branch, device)
            for rule_name, (protocol, port) in ALL_DEMO_MANAGEMENT_RULES.items():
                base = f"set vsys {context['name']} rulebase security rules {rule_name}"
                for line in (
                    f"{base} from {zone_name_for_namespace_type('prod')}",
                    f"{base} to {zone_name_for_namespace_type(MANAGEMENT_TYPE)}",
                    f"{base} source {MANAGEMENT_CLIENT_SEGMENT_PREFIX}",
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

        Leaf/access-leaf of VLAN 103 (c006): the PBR bypass ACL, if rendered, does not permit
        10.250.0.0/24, so the flow goes to the firewall. The access-leaf pinned to the services segment
        renders `interface Vlan250` in VRF MANAGEMENT with the anycast gateway 10.250.0.1/24 and no
        PBR (no `ip policy route-map`, no RM-LEAF-PBR-VLAN250 / PBR-BYPASS-VLAN250).
        """
        logging.info("=== %s - Step 7: Leaf PBR and the services SVI ===", SCENARIO_NAME)

        errors: list[str] = []
        topology = await fetch_dc_topology(async_client_main, scenario_branch, MANAGEMENT_SERVICES_DC)
        leaf_classes = {"leaf": Leaf, "access-leaf": AccessLeaf}
        carrying_client_vlan: list[str] = []
        services_leaf_rendered = False
        for device in sorted(topology["devices"], key=lambda d: str(d["name"])):
            if device.get("role") not in leaf_classes:
                continue
            name = str(device["name"])
            rendered = await _render(
                async_client_main,
                scenario_branch,
                name,
                query="leaf_config",
                transform_cls=leaf_classes[str(device["role"])],
            )
            if re.search(rf"^vlan {MANAGEMENT_CLIENT_SEGMENT_VLAN}$", rendered, re.MULTILINE):
                carrying_client_vlan.append(name)
                bypass = _block(rendered, f"ip access-list PBR-BYPASS-VLAN{MANAGEMENT_CLIENT_SEGMENT_VLAN}")
                if bypass is not None and MANAGEMENT_SERVICES_PREFIX in bypass:
                    errors.append(
                        f"{name}: PBR-BYPASS-VLAN{MANAGEMENT_CLIENT_SEGMENT_VLAN} permits {MANAGEMENT_SERVICES_PREFIX}"
                    )
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
            f"no leaf of {MANAGEMENT_SERVICES_DC} carries VLAN {MANAGEMENT_CLIENT_SEGMENT_VLAN}"
        )
        assert services_leaf_rendered, (
            f"{MANAGEMENT_SERVICES_LEAF} is not a leaf or access-leaf of {MANAGEMENT_SERVICES_DC}"
        )
        assert not errors, (
            f"Leaf PBR towards the services segment on branch '{scenario_branch}' is wrong:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
