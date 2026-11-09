"""Terminated gateways follow internal service moves without taking another footprint's segment."""

from ipaddress import IPv4Network, ip_interface
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from generators.firewall_context import FirewallContextMixin
from generators.protocols import ManagedVxlanSegment
from generators.segment_vlan import segment_lock_key
from generators.topology.customer_dc import CustomerDeploymentDCExchangeGenerator
from tests.integration.demo_data import declared
from tests.unit.customer_generator_helpers import make_generator
from utils.limits import FW_CONTEXT_VLAN_END, FW_CONTEXT_VLAN_START


def _segment(*footprints: str, context: str = "shared") -> Any:
    return SimpleNamespace(
        id="segment",
        name=SimpleNamespace(value="terminated"),
        terminated_on=SimpleNamespace(id=context),
        customer_deployments=SimpleNamespace(peers=[SimpleNamespace(id=peer) for peer in footprints]),
        save=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_local_termination_moves_untracked_under_its_segment_lock() -> None:
    """The segment is authored intent, never an output reclaimed by customer cleanup."""
    gen = make_generator(CustomerDeploymentDCExchangeGenerator)
    segment = _segment("customer")
    gen.client.filters.return_value = [segment]

    await FirewallContextMixin._relink_terminated_segments(gen, "customer", "dedicated")

    assert segment.terminated_on == "dedicated"
    segment.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)
    gen.acquire_resource_lock.assert_awaited_once_with(segment_lock_key("segment"))
    gen.release_resource_lock.assert_awaited_once()
    gen.client.filters.assert_awaited_once_with(
        kind=ManagedVxlanSegment,
        customer_deployments__ids=["customer"],
        terminated_on__isnull=False,
        include=["customer_deployments"],
    )


@pytest.mark.asyncio
async def test_already_correct_termination_does_not_write_or_lock() -> None:
    """A second boarding leaves the termination relation unchanged."""
    gen = make_generator(CustomerDeploymentDCExchangeGenerator)
    segment = _segment("customer", context="dedicated")
    gen.client.filters.return_value = [segment]

    await FirewallContextMixin._relink_terminated_segments(gen, "customer", "dedicated")

    segment.save.assert_not_awaited()
    gen.acquire_resource_lock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_context", ["shared", None, "dedicated"])
async def test_shared_termination_moves_only_when_all_footprints_use_the_new_context(
    other_context: str | None,
) -> None:
    """A boarding cannot move a shared gateway away from a second footprint's firewall."""
    gen = make_generator(CustomerDeploymentDCExchangeGenerator)
    segment = _segment("customer", "other")
    others = (
        [SimpleNamespace(serving_firewall_context=SimpleNamespace(id=other_context))]
        if other_context is not None
        else []
    )
    gen.client.filters.side_effect = [[segment], others]

    if other_context == "dedicated":
        await FirewallContextMixin._relink_terminated_segments(gen, "customer", "dedicated")
        segment.save.assert_awaited_once()
    else:
        with pytest.raises(ValueError, match="different serving firewall contexts"):
            await FirewallContextMixin._relink_terminated_segments(gen, "customer", "dedicated")
        assert segment.terminated_on.id == "shared"
        segment.save.assert_not_awaited()
    gen.release_resource_lock.assert_awaited_once()


def test_waw_prerequisites_request_only_internal_dedication(root_dir: Path) -> None:
    """Source intent reuses the bootstrap PAN-OS template and reserves the existing shared VLAN."""
    sample = root_dir / "data/demos/00_full_topology"
    design = yaml.safe_load((sample / "00_common/00_global/05_customer_designs.yml").read_text())["spec"]["data"][0]
    footprint = yaml.safe_load((sample / "20_C900/DC-WAW1/03_tenant/00_customer_dc.yml").read_text())["spec"]["data"][0]
    pool = yaml.safe_load((sample / "10_topology/DC-WAW1/00_site/00b_firewall_context_pool.yml").read_text())["spec"][
        "data"
    ][0]
    assert footprint["design"] == design["name"]
    assert design["dedicated_firewall"] is True
    assert design["dedicated_loadbalancer"] is False
    assert design["ports_100g"] == 4
    assert pool["node"] == "ManagedFirewallContext"
    assert pool["node_attribute"] == "vlan_id"
    assert pool["start_range"] == FW_CONTEXT_VLAN_START + 1
    assert pool["end_range"] == FW_CONTEXT_VLAN_END


def test_authored_firewall_transit_addresses_have_explicit_non_pool_prefixes(root_dir: Path) -> None:
    """Bare legacy addresses must reserve their /29s before the production prefix allocator runs."""
    sample = root_dir / "data/demos/00_full_topology"
    prefixes: set[tuple[str, str]] = set()
    addresses: set[tuple[str, str]] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            namespace = value.get("ip_namespace")
            if isinstance(namespace, str):
                prefix = value.get("prefix")
                if isinstance(prefix, str) and prefix.startswith("100.66.") and not value.get("is_pool"):
                    prefixes.add((prefix, namespace))
                address = value.get("address")
                if isinstance(address, str) and address.startswith("100.66."):
                    addresses.add((str(ip_interface(address).network), namespace))
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    for path in sample.rglob("*.yml"):
        collect(yaml.safe_load(path.read_text())["spec"]["data"])
    assert addresses
    assert addresses <= prefixes
    pools = [pool.item for pool in declared("CoreIPPrefixPool", root_dir / "data/bootstrap")]
    roots = [
        IPv4Network(pool["resources"]["data"][0]["prefix"]) for pool in pools if pool["name"].startswith("FW-Transit-")
    ]
    assert roots
    assert {namespace for _, namespace in addresses | prefixes} == {"default"}
    assert all(any(IPv4Network(prefix).subnet_of(root) for root in roots) for prefix, _ in addresses)
