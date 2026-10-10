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
from transforms.config.border_leaf import BorderLeaf
from transforms.config.firewall import Firewall
from utils.exchange_transit import transit_addresses, transit_vlan, transit_vni, zone_name_for_namespace_type

from .conftest import TestInfrahubDockerWithClient
from .test_constants import (
    ALL_DEMO_BRANCH,
    ALL_DEMO_DC_NAMES,
    ALL_DEMO_DEDICATED_FIREWALL_TENANTS,
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    transit_namespace_type,
)
from .test_helpers import fetch_transit_inventory, match_transit_contexts

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
              serving_firewall_context { node { cluster { node { id } } } }
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
        segment: {"members", "metro_firewalls", "outsider"}, names sorted.
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
        return {"members": members, "metro_firewalls": metro_firewalls, "outsider": outsiders[0] if outsiders else None}

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
        placement puts a rule only where its segment is served."""
        logging.info("=== %s - Step 2: Render firewall_config ===", SCENARIO_NAME)

        errors: list[str] = []
        for device in serving_cluster["members"]:
            rendered = await _render(async_client_main, scenario_branch, device)
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
        firewalls = await async_client_main.execute_graphql(query=FIREWALLS_QUERY, branch_name=scenario_branch)
        platform_by_name = {
            edge["node"]["name"]["value"]: (
                ((edge["node"].get("platform") or {}).get("node") or {}).get("name") or {}
            ).get("value")
            for edge in firewalls["DcimPhysicalDevice"]["edges"]
        }

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
        `vn-segment`, and the static routes redistributed into EVPN.
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

        assert not errors, f"{dc} border-leaf config on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Border-leaf transit routes rendered on %s (transit VNIs %s)", border_leafs, transit_vnis)
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
