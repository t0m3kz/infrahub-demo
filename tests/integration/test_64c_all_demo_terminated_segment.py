"""Integration test - the 30_all segment terminated on a firewall context (continues test_64).

C005's `firewalled-dc10` segment (data/demos/30_all/07_applications/c005/00_segments.yml) names DC10's shared PROD
firewall context in `terminated_on`. What that has to produce on the branch test_59 built:

- the reconcile (add_vxlan_segment) tags the border service ports facing the pair with the segment, as generated
  edges (an account as `source`), and only those;
- the fabric only bridges it: each border leaf has a VLAN row and renders the VLAN with its L2 VNI and no SVI;
- the firewall derives the segment's `<uplink trunk>.<vlan>` sub-interface from the VLAN of the border leaf its trunk
  is cabled to, with the segment's gateway as the pair's VIP.

Runs in-process like test_64 (the query and the transform, no Proposed Change), after it.
"""

import logging
import re
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient

from tasks import ALL_DEMO_BRANCH
from transforms.config.border_leaf import BorderLeaf
from utils.exchange_transit import SERVICE_CONTEXT_ROLES

from .conftest import TestInfrahubDockerWithClient
from .demo_data import ROOT, declared, name
from .segment_helpers import fetch_segment_ports, fetch_segment_vlans
from .snapshot_helpers import render_device_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: terminated segment"

_DEMO = ROOT / "data" / "demos" / "30_all"

TERMINATED_SEGMENT_QUERY = """
query TerminatedSegment($name: String!) {
  ManagedVxlanSegment(name__value: $name) {
    edges {
      node {
        id
        gateway { node { address { value } } }
        terminated_on {
          node {
            name { value }
            cluster { node { capabilities { edges { node { name { value } } } } } }
            capabilities { edges { node { __typename id role { value } } } }
          }
        }
      }
    }
  }
}
"""


def _terminated_segment_names() -> list[str]:
    """The names of the 30_all segments that name a `terminated_on` context (computed from the data files)."""
    return [name(obj) for obj in declared("ManagedVxlanSegment", _DEMO) if "terminated_on" in obj.item]


class TestAllDemoTerminatedSegment(TestInfrahubDockerWithClient):
    """The ports, the fabric and the firewall of a terminated segment."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    @staticmethod
    async def _segment(client: InfrahubClient, branch: str, segment_name: str) -> dict[str, Any]:
        """The segment with its gateway, its context's name, firewall members and border service ports."""
        result = await client.execute_graphql(
            query=TERMINATED_SEGMENT_QUERY, variables={"name": segment_name}, branch_name=branch
        )
        edges = result["ManagedVxlanSegment"]["edges"]
        assert edges, f"segment '{segment_name}' not found on branch '{branch}'"
        node = edges[0]["node"]
        context = node["terminated_on"]["node"]
        assert context, f"segment '{segment_name}' is terminated on no context on branch '{branch}'"
        service_port_ids = {
            edge["node"]["id"]
            for edge in context["capabilities"]["edges"]
            if edge["node"]["__typename"] == "DcimPhysicalInterface"
            and edge["node"]["role"]["value"] == SERVICE_CONTEXT_ROLES["ManagedFirewallContext"]
        }
        members = sorted(edge["node"]["name"]["value"] for edge in context["cluster"]["node"]["capabilities"]["edges"])
        return {
            "id": node["id"],
            "name": segment_name,
            "gateway": node["gateway"]["node"]["address"]["value"],
            "context": context["name"]["value"],
            "service_port_ids": service_port_ids,
            "members": members,
        }

    @pytest.mark.order(402)
    @pytest.mark.dependency(
        scope="session", name="all_demo_terminated_ports", depends=["all_demo_firewall_transit_render"]
    )
    @pytest.mark.asyncio
    async def test_00_the_border_service_ports_carry_the_segment(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """Exactly the border-leaf ports tagged with the context are on the segment (members of their switch's row).

        Nothing tags a customer port (the segment has no component and no port is assigned to its deployments).
        """
        names = _terminated_segment_names()
        assert names, "the 30_all data terminates no segment"
        for segment_name in names:
            segment = await self._segment(async_client_main, scenario_branch, segment_name)
            ports = await fetch_segment_ports(async_client_main, scenario_branch, segment["id"])

            assert segment["service_port_ids"], f"context {segment['context']} has no border service port"
            assert {port["id"] for port in ports} == segment["service_port_ids"], (
                f"{segment_name}: ports on the segment {[(p['device'], p['name']) for p in ports]} are not the "
                f"border service ports of {segment['context']}"
            )
            assert all(port["device_role"] == "border-leaf" for port in ports), ports

    @pytest.mark.order(402)
    @pytest.mark.dependency(scope="session", depends=["all_demo_terminated_ports"])
    @pytest.mark.asyncio
    async def test_01_the_fabric_bridges_it_without_an_svi(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """Each border leaf has the segment's VLAN row, renders the VLAN with its VNI and has no `interface Vlan<id>`.

        The gateway is the pair's VIP, not an anycast gateway: no SVI, so no VRF demand either (no `ip address`
        of the gateway on the leaf).
        """
        errors: list[str] = []
        rendered_count = 0
        for segment_name in _terminated_segment_names():
            segment = await self._segment(async_client_main, scenario_branch, segment_name)
            ports = await fetch_segment_ports(async_client_main, scenario_branch, segment["id"])
            rows = (
                await fetch_segment_vlans(
                    client=async_client_main, branch=scenario_branch, segment_id=segment["id"], max_attempts=1
                )
            )["records"]
            gateway_ip = segment["gateway"].split("/")[0]
            for device in sorted({str(port["device"]) for port in ports}):
                row = next((r for r in rows if str(r["device_label"]).endswith(device)), None)
                if row is None:
                    errors.append(f"{segment_name}: no VLAN row on border leaf {device}")
                    continue
                rendered = await render_device_config(
                    async_client_main, scenario_branch, device, query="border_leaf_config", transform_cls=BorderLeaf
                )
                rendered_count += 1
                vlan, vni = row["vlan_id"], row["vni"]
                if re.search(rf"^vlan {vlan}\s*$", rendered, re.MULTILINE) is None:
                    errors.append(f"{device}: no 'vlan {vlan}' for {segment_name}")
                if re.search(rf"vn-segment {vni}\b", rendered) is None:
                    errors.append(f"{device}: no 'vn-segment {vni}' for {segment_name}")
                if re.search(rf"^interface Vlan{vlan}\b", rendered, re.MULTILINE):
                    errors.append(f"{device}: 'interface Vlan{vlan}' - a terminated segment has no SVI")
                if gateway_ip in rendered:
                    errors.append(f"{device}: the pair's VIP {gateway_ip} is rendered on the fabric")
        assert rendered_count, "no border leaf was rendered"
        assert not errors, "the fabric side of the terminated segment is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    @pytest.mark.order(402)
    @pytest.mark.dependency(scope="session", depends=["all_demo_terminated_ports"])
    @pytest.mark.asyncio
    async def test_02_the_firewall_renders_the_derived_sub_interface(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """Every member of the pair renders `<trunk>.<vlan>` with the VLAN of the border leaf its trunk faces and
        the gateway as the pair's VIP (Check Point: the member's interface and the cluster interface)."""
        errors: list[str] = []
        rendered_count = 0
        for segment_name in _terminated_segment_names():
            segment = await self._segment(async_client_main, scenario_branch, segment_name)
            rows = (
                await fetch_segment_vlans(
                    client=async_client_main, branch=scenario_branch, segment_id=segment["id"], max_attempts=1
                )
            )["records"]
            border_vlans = {r["vlan_id"] for r in rows}
            gateway_ip = segment["gateway"].split("/")[0]
            assert segment["members"], f"{segment['context']}: no firewall member"
            for member in segment["members"]:
                rendered = await render_device_config(async_client_main, scenario_branch, member)
                rendered_count += 1
                sub_interfaces = re.findall(r"^set interface (\S+)\.(\d+) vlan \2$", rendered, re.MULTILINE)
                vlans = {int(vlan) for _, vlan in sub_interfaces}
                if not vlans & border_vlans:
                    errors.append(
                        f"{member}: no '<trunk>.<vlan>' with a VLAN of the border leaves {sorted(border_vlans)} "
                        f"(rendered: {sorted(vlans)})"
                    )
                    continue
                if f'ip-address "{gateway_ip}"' not in rendered:
                    errors.append(f"{member}: the VIP {gateway_ip} is not a cluster interface address")
        assert rendered_count, "no firewall member was rendered"
        assert not errors, "the firewall side of the terminated segment is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
