"""Integration test — an infrastructure VLAN (ManagedVlan) of a VLAN domain on the 30_all branch (continues test_64b).

A transfer VLAN is created on a free customer port of a DC11 leaf, in the VLAN domain of that leaf (its MLAG, or the leaf
itself): the leaf renders the VLAN as pure L2 (declared, tagged on the port, no SVI), `CheckVlanDomainMembership` is
silent, and the same VLAN carried by a port of a leaf of ANOTHER domain is the one finding. Everything created here is
removed again, so test_65's snapshot of the branch is unchanged.

Runs against the branch test_59 built, after test_64b.
"""

import logging
import re
from typing import Any, cast

import pytest
from infrahub_sdk import InfrahubClient

from checks.vlan_domain_membership import CheckVlanDomainMembership
from tasks import ALL_DEMO_BRANCH
from transforms.config.access_leaf import AccessLeaf
from transforms.config.leaf import Leaf

from .all_demo_firewall_constants import MANAGEMENT_SERVICES_DC
from .conftest import TestInfrahubDockerWithClient
from .dc_topology_helpers import fetch_dc_topology
from .firewall_config_helpers import SCENARIO_NAME
from .snapshot_helpers import render_device_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

LEAF_CLASSES = {"leaf": Leaf, "access-leaf": AccessLeaf}
TRANSFER_VLAN_ID = 20
TRANSFER_VLAN_NAME = "transfer-fw"
NXOS = "cisco_nxos"


def _interface_stanza(rendered: str, name: str) -> str | None:
    """The lines of `interface <name>` up to the `!` that closes it (a blank line may follow the header)."""
    match = re.search(rf"^interface {re.escape(name)}\n(.*?)^!$", rendered, re.MULTILINE | re.DOTALL)
    return None if match is None else match.group(1)


async def _domain_id(client: InfrahubClient, branch: str, device_name: str) -> str:
    """The VLAN domain of a device: its ManagedMLAG when paired, else the device itself."""
    device = await client.get(
        kind="DcimPhysicalDevice", name__value=device_name, branch=branch, include=["capabilities"]
    )
    mlag = next((peer for peer in getattr(device, "capabilities").peers if peer.typename == "ManagedMLAG"), None)
    return str(mlag.id if mlag else device.id)


async def _free_customer_port(client: InfrahubClient, branch: str, device_name: str) -> Any:
    """A customer-facing port of the device that is neither cabled nor carries a capability."""
    ports = await client.filters(
        kind="DcimPhysicalInterface",
        device__name__value=device_name,
        role__value="customer",
        branch=branch,
        include=["cable", "capabilities"],
    )
    free = [
        port
        for port in sorted(ports, key=lambda p: str(getattr(p, "name").value))
        if not getattr(port, "cable").id and not getattr(port, "capabilities").peers
    ]
    assert free, f"{device_name} has no free customer port on branch '{branch}'"
    return free[0]


async def _nxos_leaves(client: InfrahubClient, branch: str) -> list[tuple[str, str]]:
    """(name, role) of the NX-OS leaves of the services DC, in name order, one per VLAN domain."""
    topology = await fetch_dc_topology(client, branch, MANAGEMENT_SERVICES_DC)
    leaves: list[tuple[str, str]] = []
    domains: set[str] = set()
    for device in sorted(topology["devices"], key=lambda d: str(d["name"])):
        name, role = str(device["name"]), str(device.get("role"))
        if role not in LEAF_CLASSES:
            continue
        node = await client.get(kind="DcimPhysicalDevice", name__value=name, branch=branch, include=["platform"])
        if getattr(node, "platform").peer.name.value != NXOS:
            continue
        domain = await _domain_id(client, branch, name)
        if domain not in domains:
            domains.add(domain)
            leaves.append((name, role))
    return leaves


class TestAllDemoInfraVlan(TestInfrahubDockerWithClient):
    """An infrastructure VLAN is rendered on its leaf and guarded by CheckVlanDomainMembership."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    @pytest.mark.order(404)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_08_infra_vlan_is_rendered_and_checked(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """The leaf declares and tags the VLAN, the check passes in its domain and fails in another."""
        logging.info("=== %s - Step 8: Infrastructure VLAN ===", SCENARIO_NAME)
        client = async_client_main
        leaves = await _nxos_leaves(client, scenario_branch)
        assert len(leaves) >= 2, f"{MANAGEMENT_SERVICES_DC} needs NX-OS leaves of two VLAN domains, got {leaves}"
        (leaf, role), (other_leaf, _) = leaves[0], leaves[1]

        domain_id = await _domain_id(client, scenario_branch, leaf)
        port = await _free_customer_port(client, scenario_branch, leaf)
        foreign_port = await _free_customer_port(client, scenario_branch, other_leaf)
        vlan = await client.create(
            kind="ManagedVlan",
            branch=scenario_branch,
            data={
                "name": TRANSFER_VLAN_NAME,
                "vlan_id": TRANSFER_VLAN_ID,
                "role": "transfer",
                "vlan_domain": {"id": domain_id},
                "capabilities": [{"id": port.id}],
            },
        )
        await vlan.save(allow_upsert=True)
        try:
            rendered = await render_device_config(
                client, scenario_branch, leaf, query="leaf_config", transform_cls=LEAF_CLASSES[role]
            )
            assert re.search(rf"^vlan {TRANSFER_VLAN_ID}\n  name {TRANSFER_VLAN_NAME}$", rendered, re.MULTILINE), (
                f"{leaf} does not declare vlan {TRANSFER_VLAN_ID}"
            )
            assert f"Vlan{TRANSFER_VLAN_ID}" not in rendered, f"{leaf} renders an SVI for the pure L2 VLAN"
            port_block = _interface_stanza(rendered, str(port.name.value))
            assert port_block is not None, f"{leaf} does not render {port.name.value}"
            assert f"switchport access vlan {TRANSFER_VLAN_ID}" in port_block, (
                f"{port.name.value} of {leaf}:\n{port_block}"
            )

            assert await self._findings(client, scenario_branch) == [], "the VLAN on its own domain is reported"

            getattr(vlan, "capabilities").add(foreign_port.id)
            await vlan.save()
            findings = await self._findings(client, scenario_branch)
            assert len(findings) == 1, f"expected one finding for the port of {other_leaf}, got {findings}"
            assert other_leaf in findings[0]["message"]
        finally:
            await vlan.delete()

        rendered = await render_device_config(
            client, scenario_branch, leaf, query="leaf_config", transform_cls=LEAF_CLASSES[role]
        )
        assert not re.search(rf"^vlan {TRANSFER_VLAN_ID}$", rendered, re.MULTILINE), f"{leaf} still declares the VLAN"

    @staticmethod
    async def _findings(client: InfrahubClient, branch: str) -> list[dict[str, Any]]:
        """Run CheckVlanDomainMembership on the live query result and return what it reports."""
        raw = await client.query_gql_query(name="vlan_domain_membership", branch_name=branch)
        check = cast(Any, CheckVlanDomainMembership.__new__(CheckVlanDomainMembership))
        findings: list[dict[str, Any]] = []
        check.log_error = lambda **kwargs: findings.append(kwargs)
        check.validate(raw.get("data") or raw)
        return findings
