"""Unit tests for ColocationMetroGenerator (generators/topology/colocation.py).

The generator replaces hand-written cage routers with a declarative
``fabric_templates`` list on the metro, so the tests concentrate on the three
things the demo data now depends on being exactly right:

- which fabric_templates entries are usable, and that a bad one warns rather
  than failing the whole metro (FailOnErrorLogger turns error() into a task
  failure);
- the metro's four pool names and — crucially — their KINDS, since
  create_devices() resolves "{metro}-loopback-pool" and hands it to
  allocate_next_ip_address(), which needs an address pool, not a prefix pool;
- the generated device names, because data/demos/30_all/08_interconnects/
  references "eg-fr01" by name.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.helpers.naming import DeviceNameContext, DeviceNamingConfig
from generators.helpers.pools import FW_CONTEXT_VLAN_END, FW_CONTEXT_VLAN_START
from generators.topology.colocation import (
    _COLO_FW_CONTEXT_P2P_SLICE_LENGTH,
    _COLO_LOOPBACK_PREFIX_LENGTH,
    _COLO_MANAGEMENT_PREFIX_LENGTH,
    _COLO_SERVICE_ROLES,
    _COLO_TECHNICAL_PREFIX_LENGTH,
    _COLO_VALID_FABRIC_ROLES,
    ColocationMetroGenerator,
)

_EDGE_TEMPLATE = {
    "id": "tpl-edge",
    # The cleaned form of the `template_kind: __typename` alias the query
    # selects — aliased because clean_data() would rewrite __typename to
    # "typename". This is what tells the generator to build a physical device.
    "template_kind": "TemplateDcimPhysicalDevice",
    "template_name": "N9K-C9316D-GX_EDGE",
    "platform": {"id": "plat-nxos", "name": "cisco_nxos"},
    "device_type": "dt-9316d",
}

# Same edge template, but with the real N9K-C9316D-GX_EDGE port layout
# attached (see data/bootstrap/10_physical_devices_templates_cisco_nxos.yaml):
# a `firewall`-role port block, no `load-balancer`-role ports at all.
_EDGE_TEMPLATE_WITH_FIREWALL_PORTS = {
    **_EDGE_TEMPLATE,
    "id": "tpl-edge-fw-ports",
    "interfaces": [
        {"name": "Ethernet1/15", "role": "firewall"},
        {"name": "Ethernet1/16", "role": "firewall"},
    ],
}

# A hypothetical edge template with BOTH firewall- and load-balancer-role
# ports (mirrors N9K-C9336C-FX2_BORDER_LEAF's layout) — used where a test
# wants every colocation role to be cablable, not just structurally valid.
_EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS = {
    **_EDGE_TEMPLATE,
    "id": "tpl-edge-all-ports",
    "interfaces": [
        {"name": "Ethernet1/15", "role": "firewall"},
        {"name": "Ethernet1/16", "role": "firewall"},
        {"name": "Ethernet1/17", "role": "load-balancer"},
        {"name": "Ethernet1/18", "role": "load-balancer"},
    ],
}

_VIRTUAL_EDGE_TEMPLATE = {
    "id": "tpl-vedge",
    "template_kind": "TemplateDcimVirtualDevice",
    "template_name": "C8000V_EDGE",
}


def _make_generator() -> Any:
    gen = ColocationMetroGenerator.__new__(ColocationMetroGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.get = AsyncMock()
    gen.client.create = AsyncMock()
    gen.client.allocate_next_ip_prefix = AsyncMock(return_value=MagicMock(id="prefix-1"))
    gen.fabric_name = "fr"
    gen.pod_name = None
    gen.deployment_id = "metro-1"
    setattr(gen, "acquire_resource_lock", AsyncMock(return_value="lock-id"))  # noqa: B010
    setattr(gen, "release_resource_lock", AsyncMock())  # noqa: B010
    return gen


def _metro(**overrides: Any) -> dict[str, Any]:
    metro: dict[str, Any] = {
        "id": "metro-1",
        "name": "FR",
        "deployment_type": "physical",
        "naming_convention": "standard",
        "fabric_templates": [{"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE}],
    }
    metro.update(overrides)
    return metro


class TestFabricTemplateValidation:
    def test_valid_entry_is_kept(self) -> None:
        gen = _make_generator()
        gen.data = _metro()

        assert gen._valid_fabric_templates() == gen.data["fabric_templates"]
        gen.logger.warning.assert_not_called()

    @pytest.mark.parametrize(
        "entry",
        [
            # DC-only fabric tiers are meaningless in a metro that has no fabric.
            {"role": "super-spine", "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": "leaf", "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": None, "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": "edge", "quantity": 0, "template": _EDGE_TEMPLATE},
            {"role": "edge", "quantity": 2, "template": None},
        ],
    )
    def test_unusable_entry_warns_and_is_dropped(self, entry: dict[str, Any]) -> None:
        gen = _make_generator()
        gen.data = _metro(fabric_templates=[entry])

        assert gen._valid_fabric_templates() == []
        gen.logger.warning.assert_called_once()
        # warning(), never error(): one bad entry must not fail the whole metro.
        gen.logger.error.assert_not_called()

    def test_one_bad_entry_does_not_drop_the_good_ones(self) -> None:
        gen = _make_generator()
        good = {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE}
        gen.data = _metro(
            fabric_templates=[
                {"role": "border-leaf", "quantity": 2, "template": _EDGE_TEMPLATE},
                good,
            ]
        )

        assert gen._valid_fabric_templates() == [good]

    def test_every_colocation_role_is_accepted(self) -> None:
        """Every colocation role is structurally valid — given an edge
        template with the matching ports, none of them get dropped as
        uncablable either (see TestUncablableServiceEntries for the
        edge-lacks-the-port case)."""
        gen = _make_generator()
        entries = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS},
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": "load-balancer", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen.data = _metro(fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()


class TestUncablableServiceEntries:
    """_drop_uncablable_service_entries: a physical firewall/load-balancer
    entry is only usable if some physical edge template in the same metro
    exposes a same-named port role — otherwise create_devices() would build
    a real HA pair and create_chain_cabling would fail the task on it right
    after, with the appliance already left behind."""

    def test_service_role_with_no_matching_edge_port_is_dropped(self) -> None:
        """N9K-C9316D-GX_EDGE has `firewall` ports but no `load-balancer`
        ports — exactly the gap that used to reach create_chain_cabling."""
        gen = _make_generator()
        entries = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE_WITH_FIREWALL_PORTS},
            {"role": "load-balancer", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen.data = _metro(fabric_templates=entries)

        assert gen._valid_fabric_templates() == [entries[0]]
        gen.logger.warning.assert_called_once()
        gen.logger.error.assert_not_called()

    def test_service_role_with_a_matching_edge_port_is_kept(self) -> None:
        gen = _make_generator()
        entries = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE_WITH_FIREWALL_PORTS},
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen.data = _metro(fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    def test_no_physical_edge_entry_at_all_is_not_this_checks_concern(self) -> None:
        """A metro can legitimately declare a service role with no edge pair
        at all (Paris-style) — _cable_metro_services's own no-op guard covers
        that already, so this check must not also drop the entry."""
        gen = _make_generator()
        entries = [{"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE}]
        gen.data = _metro(fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    def test_virtual_service_entry_is_exempt(self) -> None:
        """A virtual firewall is never cabled in the first place (see
        _create_metro_devices), so a physical edge lacking the port is
        irrelevant to it."""
        gen = _make_generator()
        entries = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE_WITH_FIREWALL_PORTS},
            {"role": "load-balancer", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
        ]
        gen.data = _metro(deployment_type="hybrid", fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()


class TestDeploymentStrategy:
    """deployment_type gates which device kind a metro may instantiate.

    It is the only signal available at generator time: a cage's racks are
    attached by a later file in the same load, so even a metro we own racks in
    shows none anywhere beneath it when the created-trigger fires.
    """

    @pytest.mark.parametrize(
        ("deployment_type", "template", "expected_kept"),
        [
            ("physical", _EDGE_TEMPLATE, True),
            ("physical", _VIRTUAL_EDGE_TEMPLATE, False),
            ("virtual", _VIRTUAL_EDGE_TEMPLATE, True),
            ("virtual", _EDGE_TEMPLATE, False),
            ("hybrid", _EDGE_TEMPLATE, True),
            ("hybrid", _VIRTUAL_EDGE_TEMPLATE, True),
            # No strategy declared: the schema defaults to physical, so a
            # physical template is fine and a virtual one is not.
            (None, _EDGE_TEMPLATE, True),
            (None, _VIRTUAL_EDGE_TEMPLATE, False),
        ],
    )
    def test_template_kind_is_checked_against_the_strategy(
        self, deployment_type: str | None, template: dict[str, Any], expected_kept: bool
    ) -> None:
        gen = _make_generator()
        entry = {"role": "edge", "quantity": 2, "template": template}
        gen.data = _metro(deployment_type=deployment_type, fabric_templates=[entry])

        assert gen._valid_fabric_templates() == ([entry] if expected_kept else [])
        assert gen.logger.warning.called is not expected_kept
        gen.logger.error.assert_not_called()

    def test_unknown_strategy_skips_everything_without_guessing(self) -> None:
        gen = _make_generator()
        gen.data = _metro(
            deployment_type="quantum",
            fabric_templates=[{"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE}],
        )

        assert gen._valid_fabric_templates() == []
        assert "quantum" in gen.logger.warning.call_args.args[0]
        gen.logger.error.assert_not_called()

    def test_template_without_a_kind_is_skipped(self) -> None:
        """Guards a query regression: no template_kind means we cannot tell a
        physical template from a virtual one, so nothing gets built."""
        gen = _make_generator()
        gen.data = _metro(fabric_templates=[{"role": "edge", "quantity": 2, "template": {"id": "tpl-edge"}}])

        assert gen._valid_fabric_templates() == []
        gen.logger.warning.assert_called_once()
        gen.logger.error.assert_not_called()


class TestGenerateEntry:
    @pytest.mark.asyncio
    async def test_missing_root_key_logs_error(self) -> None:
        gen = _make_generator()

        await gen.generate({})

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_raw_graphql_response_is_cleaned(self) -> None:
        """generate() must run clean_data() first — without it the root key is
        an {"edges": [...]} dict and nothing is ever iterated."""
        gen = _make_generator()
        gen._ensure_colocation_pools = AsyncMock()
        gen._create_metro_devices = AsyncMock(return_value={"edge": ["eg-fr01", "eg-fr02"]})
        gen._cable_metro_services = AsyncMock()
        raw = {
            "TopologyColocationMetro": {
                "edges": [
                    {
                        "node": {
                            "id": "metro-1",
                            "name": {"value": "FR"},
                            "naming_convention": {"value": "standard"},
                            "deployment_type": {"value": "physical"},
                            "fabric_templates": {
                                "edges": [
                                    {
                                        "node": {
                                            "role": {"value": "edge"},
                                            "quantity": {"value": 2},
                                            "template": {
                                                "node": {
                                                    "id": "tpl-edge",
                                                    "template_kind": "TemplateDcimPhysicalDevice",
                                                }
                                            },
                                        }
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        }

        await gen.generate(raw)

        assert gen.fabric_name == "fr"
        assert gen.pod_name is None
        gen._ensure_colocation_pools.assert_awaited_once_with(metro_id="metro-1")
        gen._create_metro_devices.assert_awaited_once()
        assert gen._create_metro_devices.await_args_list[-1].kwargs["templates"] == [
            {
                "role": "edge",
                "quantity": 2,
                "template": {"id": "tpl-edge", "template_kind": "TemplateDcimPhysicalDevice"},
            }
        ]
        # The names the device pass produced are what the cabling pass acts on —
        # passed through, not re-queried.
        gen._cable_metro_services.assert_awaited_once_with(physical_names_by_role={"edge": ["eg-fr01", "eg-fr02"]})

    @pytest.mark.asyncio
    async def test_metro_without_templates_creates_no_pools(self) -> None:
        """Every metro is a colocation_metros member so the per-kind created
        trigger never fires on a non-member, which means most metros reach this
        generator with nothing to build. Those must not acquire pools."""
        gen = _make_generator()
        gen._ensure_colocation_pools = AsyncMock()
        gen._create_metro_devices = AsyncMock()

        await gen.generate({"TopologyColocationMetro": [_metro(fabric_templates=[])]})

        gen._ensure_colocation_pools.assert_not_awaited()
        gen._create_metro_devices.assert_not_awaited()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_cabling_runs_after_devices_not_before(self) -> None:
        """Ordering is load-bearing, not incidental: _cable_metro_services
        resolves interfaces by device name, so a cabling pass that ran first
        would find no devices and log create_chain_cabling's missing-ports
        error — a task failure."""
        gen = _make_generator()
        gen._ensure_colocation_pools = AsyncMock()
        calls: list[str] = []
        gen._create_metro_devices = AsyncMock(side_effect=lambda **_: calls.append("devices") or {})
        gen._cable_metro_services = AsyncMock(side_effect=lambda **_: calls.append("cabling"))

        await gen.generate({"TopologyColocationMetro": [_metro()]})

        assert calls == ["devices", "cabling"]

    @pytest.mark.asyncio
    async def test_metro_with_firewalls_gets_context_pools(self) -> None:
        """customer_colocation.py looks the context pools up by name for
        every deployment in the metro, so a metro with firewalls must have
        them before the first boarding."""
        gen = _make_generator()
        gen._ensure_colocation_pools = AsyncMock()
        gen._create_metro_devices = AsyncMock(
            return_value={"edge": ["eg-fr01", "eg-fr02"], "firewall": ["fw-fr01", "fw-fr02"]}
        )
        gen._ensure_firewall_context_pools = AsyncMock()
        gen._cable_metro_services = AsyncMock()
        gen._ensure_metro_evpn = AsyncMock()

        await gen.generate({"TopologyColocationMetro": [_metro()]})

        gen._ensure_firewall_context_pools.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_metro_without_firewalls_gets_no_context_pools(self) -> None:
        """No firewall means no context is ever created in the metro."""
        gen = _make_generator()
        gen._ensure_colocation_pools = AsyncMock()
        gen._create_metro_devices = AsyncMock(return_value={"edge": ["eg-fr01", "eg-fr02"]})
        gen._ensure_firewall_context_pools = AsyncMock()
        gen._cable_metro_services = AsyncMock()
        gen._ensure_metro_evpn = AsyncMock()

        await gen.generate({"TopologyColocationMetro": [_metro()]})

        gen._ensure_firewall_context_pools.assert_not_awaited()


class TestPools:
    @pytest.mark.asyncio
    async def test_pool_names_kinds_and_parents(self) -> None:
        gen = _make_generator()
        gen.ensure_sliced_pool = AsyncMock(side_effect=lambda **kw: MagicMock(id=f"pool-{kw['pool_name']}"))
        gen.upsert_asn_pool = AsyncMock()
        gen.upsert_number_pool = AsyncMock()
        gen.client.get = AsyncMock(return_value=AsyncMock())

        await gen._ensure_colocation_pools(metro_id="metro-1")

        calls = {c.kwargs["pool_name"]: c.kwargs for c in gen.ensure_sliced_pool.await_args_list}
        assert set(calls) == {
            "fr-loopback-pool",
            "fr-loopback-ipv6-pool",
            "fr-management-pool",
            "fr-technical-pool",
        }
        # The EVPN Multi-Site VTEP loopback is IPv6, matching the IPv6 DCI.
        assert calls["fr-loopback-ipv6-pool"]["kind"] == "address"
        assert calls["fr-loopback-ipv6-pool"]["parent_pool_name"] == "Loopback-IPv6"
        assert calls["fr-loopback-ipv6-pool"]["prefix_length"] == 120
        # The metro hosts stretched segments, so it needs its own VNI pool.
        vni_kwargs = gen.upsert_number_pool.await_args_list[-1].kwargs
        assert vni_kwargs["pool_name"] == "fr-vni-pool"
        # An address pool for both loopbacks and management: create_devices()
        # feeds each straight to allocate_next_ip_address().
        assert calls["fr-loopback-pool"]["kind"] == "address"
        assert calls["fr-loopback-pool"]["parent_pool_name"] == "Loopback-IPv4"
        assert calls["fr-loopback-pool"]["prefix_length"] == _COLO_LOOPBACK_PREFIX_LENGTH
        assert calls["fr-management-pool"]["kind"] == "address"
        assert calls["fr-management-pool"]["parent_pool_name"] == "Management-IPv4"
        assert calls["fr-management-pool"]["prefix_length"] == _COLO_MANAGEMENT_PREFIX_LENGTH
        # P2P links get whole prefixes handed out, so this one is a prefix pool.
        assert calls["fr-technical-pool"]["kind"] == "prefix"
        assert calls["fr-technical-pool"]["parent_pool_name"] == "Technical-IPv4"
        assert calls["fr-technical-pool"]["prefix_length"] == _COLO_TECHNICAL_PREFIX_LENGTH

    @pytest.mark.asyncio
    async def test_asn_pool_drawn_from_the_name_hash_grid(self) -> None:
        """The metro ASN pool comes from the same deterministic grid as DC fabric ASNs."""
        gen = _make_generator()
        gen.ensure_sliced_pool = AsyncMock(return_value=MagicMock(id="pool-x"))
        gen.upsert_asn_pool = AsyncMock()
        gen.upsert_number_pool = AsyncMock()
        gen.client.get = AsyncMock(return_value=AsyncMock())

        await gen._ensure_colocation_pools(metro_id="metro-1")

        kwargs = gen.upsert_asn_pool.await_args_list[-1].kwargs
        assert kwargs["pool_name"] == "fr-asn-pool"
        # Private-use 4-byte range, from the same deterministic name-hash grid
        # DC fabric ASNs use, so a metro can never collide with a DC.
        assert 4_200_000_000 <= kwargs["start_range"] < kwargs["end_range"] <= 4_294_967_295

    @pytest.mark.asyncio
    async def test_ip_pools_are_attached_with_a_plain_save(self) -> None:
        """allow_upsert=True would resend every relationship and re-fire the
        fabric_templates `updated` trigger on each pool attach. Untracked: the
        metro is the run's target, so its own cleanup must never reach it."""
        gen = _make_generator()
        gen.ensure_sliced_pool = AsyncMock(side_effect=lambda **kw: MagicMock(id=f"id-{kw['pool_name']}"))
        gen.upsert_asn_pool = AsyncMock(return_value=MagicMock(id="asn-pool"))
        gen.upsert_number_pool = AsyncMock(return_value=MagicMock(id="vni-pool"))
        metro = AsyncMock()
        gen.client.get = AsyncMock(return_value=metro)

        await gen._ensure_colocation_pools(metro_id="metro-1")

        assert metro.loopback_pool == {"id": "id-fr-loopback-pool"}
        assert metro.management_pool == {"id": "id-fr-management-pool"}
        assert metro.technical_pool == {"id": "id-fr-technical-pool"}
        # The ASN and VNI pools ride the same single save.
        assert metro.asn_pool == {"id": "asn-pool"}
        assert metro.vni_pool == {"id": "vni-pool"}
        metro.save.assert_awaited_once_with(update_group_context=False)
        gen.client.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sliced_address_pool_wraps_the_allocated_prefix(self) -> None:
        gen = _make_generator()
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock(id="parent-1"))
        pool = AsyncMock()
        gen.client.create = AsyncMock(return_value=pool)

        await gen.ensure_sliced_pool(
            pool_name="fr-loopback-pool",
            parent_pool_name="Loopback-IPv4",
            prefix_length=28,
            role="loopback",
            kind="address",
        )

        # Sliced out of the global bootstrap pool, never a runtime-invented
        # supernet, and keyed by an identifier so the slice is idempotent.
        alloc = gen.client.allocate_next_ip_prefix.await_args_list[-1].kwargs
        assert alloc["identifier"] == "fr-loopback-pool"
        assert alloc["prefix_length"] == 28
        assert alloc["data"] == {"role": "loopback"}

        create = gen.client.create.await_args_list[-1].kwargs
        assert create["kind"].__name__ == "CoreIPAddressPool"
        assert create["data"]["default_address_type"] == "IpamIPAddress"
        assert create["data"]["resources"] == [gen.client.allocate_next_ip_prefix.return_value]
        pool.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_sliced_prefix_pool_uses_the_prefix_kind(self) -> None:
        gen = _make_generator()
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock(id="parent-1"))
        gen.client.create = AsyncMock(return_value=AsyncMock())

        await gen.ensure_sliced_pool(
            pool_name="fr-technical-pool",
            parent_pool_name="Technical-IPv4",
            prefix_length=26,
            role="technical",
            kind="prefix",
        )

        create = gen.client.create.await_args_list[-1].kwargs
        assert create["kind"].__name__ == "CoreIPPrefixPool"
        assert create["data"]["default_prefix_type"] == "IpamPrefix"

    @pytest.mark.asyncio
    async def test_default_prefix_length_overrides_the_slice_length(self) -> None:
        gen = _make_generator()
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock(id="parent-1"))
        gen.client.create = AsyncMock(return_value=AsyncMock())

        await gen.ensure_sliced_pool(
            pool_name="fr-fw-context-p2p-pool",
            parent_pool_name="FW-Context-P2P-IPv4",
            prefix_length=24,
            role="technical",
            kind="prefix",
            default_prefix_length=31,
        )

        assert gen.client.allocate_next_ip_prefix.await_args_list[-1].kwargs["prefix_length"] == 24
        assert gen.client.create.await_args_list[-1].kwargs["data"]["default_prefix_length"] == 31


class TestFirewallContextPools:
    """_ensure_firewall_context_pools() now delegates to PoolMixin.
    ensure_firewall_context_pools, the mechanism shared with dc.py's
    per-DC equivalent — see that method's docstring for the concurrent-
    create race it serializes against."""

    def _make_gen(self) -> Any:
        gen = _make_generator()
        gen.upsert_number_pool = AsyncMock()
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock(id="parent-pool-id"))
        gen.client.allocate_next_ip_prefix = AsyncMock(return_value=MagicMock(id="metro-slice-id"))
        # ensure_firewall_context_pools() serializes concurrent callers via
        # acquire_resource_lock/release_resource_lock — not under test except
        # in test_serializes_on_a_lock_scoped_to_this_metro below, and the
        # lock's own client.delete() call has no mock set up on this plain
        # MagicMock client.
        gen.acquire_resource_lock = AsyncMock(return_value="lock-id")
        gen.release_resource_lock = AsyncMock()
        return gen

    @pytest.mark.asyncio
    async def test_vlan_pool_matches_the_dc_range(self) -> None:
        """The pool customer_colocation.py reads by name, numbering
        ManagedFirewallContext.vlan_id in the DC context range."""
        gen = self._make_gen()

        await gen._ensure_firewall_context_pools()

        kwargs = gen.upsert_number_pool.await_args_list[-1].kwargs
        assert kwargs["pool_name"] == "fr-fw-context-vlan-pool"
        assert kwargs["node"] == "ManagedFirewallContext"
        assert kwargs["node_attribute"] == "vlan_id"
        assert (kwargs["start_range"], kwargs["end_range"]) == (FW_CONTEXT_VLAN_START, FW_CONTEXT_VLAN_END)

    @pytest.mark.asyncio
    async def test_p2p_pool_is_a_slice_handing_out_31s(self) -> None:
        """The pool's default must be a /31 point-to-point link, cut from a
        slice of the global IPv4 bootstrap pool — the metro's edge legs are
        always Technical-IPv4, unlike dc.py's dual-stack choice."""
        gen = self._make_gen()

        await gen._ensure_firewall_context_pools()

        gen._get_parent_pool_with_retry.assert_awaited_once_with("FW-Context-P2P-IPv4")
        alloc_kwargs = gen.client.allocate_next_ip_prefix.call_args.kwargs
        assert alloc_kwargs["prefix_length"] == _COLO_FW_CONTEXT_P2P_SLICE_LENGTH
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["default_prefix_length"] == 31

    @pytest.mark.asyncio
    async def test_serializes_on_a_lock_scoped_to_this_metro(self) -> None:
        """Two overlapping calls for the SAME metro must not both create a
        CoreIPPrefixPool with the same name — previously unguarded here
        (dc.py's equivalent already serialized on a per-DC lock)."""
        gen = self._make_gen()
        calls: list[str] = []
        gen.acquire_resource_lock = AsyncMock(side_effect=lambda key: calls.append(f"acquire:{key}") or "lock-id")
        gen.release_resource_lock = AsyncMock(side_effect=lambda lock_id: calls.append(f"release:{lock_id}"))

        await gen._ensure_firewall_context_pools()

        assert calls == ["acquire:fw-context-pools-fr", "release:lock-id"]


class TestMetroDevices:
    @pytest.mark.asyncio
    async def test_edge_devices_deploy_to_the_metro_with_flat_naming(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["eg-fr01", "eg-fr02"])
        gen.data = _metro()

        await gen._create_metro_devices(templates=gen.data["fabric_templates"], metro_id="metro-1")

        kwargs = gen.create_devices.await_args_list[-1].kwargs
        assert kwargs["device_role"] == "edge"
        assert kwargs["quantity"] == 2
        # The metro owns the on-ramp tier, like a DC owns its border-leafs —
        # never the cage the kit happens to be racked in.
        assert kwargs["deployment_id"] == "metro-1"
        assert kwargs["naming_convention"] == "standard"
        # A metro's on-ramp is flat: no fabric/pod/suite/row/rack path to encode.
        assert kwargs["options"]["indexes"] == []
        assert kwargs["options"]["allocate_loopback"] is True
        assert kwargs["options"]["loopback_prefix_length"] == 32
        assert "group_name" not in kwargs["options"]
        # A physical template must not be flipped to DcimVirtualDevice.
        assert "virtual" not in kwargs["options"]
        # An edge router is not an HA appliance — it peers, it does not fail over.
        assert "ha_kind" not in kwargs["options"]

    @pytest.mark.asyncio
    async def test_virtual_template_creates_virtual_devices(self) -> None:
        """A provider-hosted on-ramp router (Network Edge, MCR) is a
        DcimVirtualDevice, which create_devices() selects via options["virtual"]."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["eg-am01"])
        templates = [{"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE}]
        gen.data = _metro(deployment_type="virtual", fabric_templates=templates)

        await gen._create_metro_devices(templates=templates, metro_id="metro-1")

        kwargs = gen.create_devices.await_args_list[-1].kwargs
        assert kwargs["options"]["virtual"] is True
        # Loopback and management addressing are unchanged: an MCR still has a
        # loopback and still peers over it.
        assert kwargs["options"]["allocate_loopback"] is True

    @pytest.mark.asyncio
    async def test_load_balancer_leaves_its_group_to_create_devices(self) -> None:
        """The role's bootstrap group is resolved once, in create_devices (ROLE_GROUP_NAMES)."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["lb-fr01"])
        templates = [{"role": "load-balancer", "quantity": 2, "template": _EDGE_TEMPLATE}]

        gen.data = _metro(fabric_templates=templates)
        await gen._create_metro_devices(templates=templates, metro_id="metro-1")

        assert "group_name" not in gen.create_devices.await_args_list[-1].kwargs["options"]

    @pytest.mark.parametrize(
        ("role", "expected_ha_kind"),
        [
            ("firewall", "ManagedFirewallHA"),
            ("load-balancer", "ManagedLoadbalancerHA"),
        ],
    )
    @pytest.mark.asyncio
    async def test_service_appliances_pair_into_an_ha_domain_without_a_loopback(
        self, role: str, expected_ha_kind: str
    ) -> None:
        """A colocation firewall/load-balancer is the same shape as a DC's: the
        HA domain is built inside create_devices() off options["ha_kind"], and
        the appliance gets no loopback because it is not part of the underlay or
        the overlay — only the edge routers route."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["a-fr01", "a-fr02"])
        templates = [{"role": role, "quantity": 2, "template": _EDGE_TEMPLATE}]
        gen.data = _metro(fabric_templates=templates)

        await gen._create_metro_devices(templates=templates, metro_id="metro-1")

        options = gen.create_devices.await_args_list[-1].kwargs["options"]
        assert options["ha_kind"] == expected_ha_kind
        assert "allocate_loopback" not in options
        assert "loopback_prefix_length" not in options
        # Still the metro's own kit, exactly like the edge pair.
        assert gen.create_devices.await_args_list[-1].kwargs["deployment_id"] == "metro-1"

    def test_ha_roles_are_a_subset_of_the_valid_colocation_roles(self) -> None:
        """A service role the metro would never build is dead code, and a
        role the metro builds but cannot pair is a silent single point of
        failure — keep the two in step."""
        assert _COLO_SERVICE_ROLES < _COLO_VALID_FABRIC_ROLES

    @pytest.mark.asyncio
    async def test_naming_convention_is_lowercased_and_defaulted(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=[])
        templates = [{"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE}]

        gen.data = _metro(fabric_templates=templates, naming_convention="Standard")
        await gen._create_metro_devices(templates=templates, metro_id="metro-1")
        assert gen.create_devices.await_args_list[-1].kwargs["naming_convention"] == "standard"

        gen.data = _metro(fabric_templates=templates, naming_convention=None)
        await gen._create_metro_devices(templates=templates, metro_id="metro-1")
        assert gen.create_devices.await_args_list[-1].kwargs["naming_convention"] == "standard"

    @pytest.mark.asyncio
    async def test_every_role_gets_its_own_create_devices_call(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=[])
        templates = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen.data = _metro(fabric_templates=templates)

        await gen._create_metro_devices(templates=templates, metro_id="metro-1")

        assert [c.kwargs["device_role"] for c in gen.create_devices.await_args_list] == ["edge", "firewall"]

    @pytest.mark.asyncio
    async def test_created_names_are_returned_keyed_by_role(self) -> None:
        """_cable_metro_services needs to know which names are edges and which
        are appliances, and nothing else records that — the devices all deploy to
        the same metro, so a later query by deployment cannot tell them apart
        without re-reading each device's role."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(side_effect=[["eg-fr01", "eg-fr02"], ["fw-fr01", "fw-fr02"]])
        templates = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen.data = _metro(fabric_templates=templates)

        physical_names_by_role = await gen._create_metro_devices(templates=templates, metro_id="metro-1")

        assert physical_names_by_role == {"edge": ["eg-fr01", "eg-fr02"], "firewall": ["fw-fr01", "fw-fr02"]}

    @pytest.mark.asyncio
    async def test_two_entries_for_one_role_accumulate(self) -> None:
        """Nothing in the schema stops a metro declaring two firewall entries
        (two models), and the second must not overwrite the first — both pairs
        hang off the same edges."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(side_effect=[["fw-fr01", "fw-fr02"], ["fw-fr03", "fw-fr04"]])
        templates = [
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen.data = _metro(fabric_templates=templates)

        physical_names_by_role = await gen._create_metro_devices(templates=templates, metro_id="metro-1")

        assert physical_names_by_role == {"firewall": ["fw-fr01", "fw-fr02", "fw-fr03", "fw-fr04"]}

    @pytest.mark.asyncio
    async def test_virtual_devices_are_created_but_never_cabled(self) -> None:
        """A DcimCable is fibre between two chassis, so provider-hosted instances
        must not be handed to the cabling pass — the link between two of them is a
        connection inside the provider's platform.

        Not hypothetical: PA-VM_EDGE_CUSTOMER_* exposes `uplink` on eth[1-6],
        exactly the role _cable_border_services claims on the service side, so
        without this a deployment_type=virtual metro declaring an edge and a
        firewall would reach create_chain_cabling and fail the task on
        C8000V_EDGE's missing `firewall` ports.
        """
        gen = _make_generator()
        gen.create_devices = AsyncMock(side_effect=[["eg-am01", "eg-am02"], ["fw-am01", "fw-am02"]])
        templates = [
            {"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
            {"role": "firewall", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
        ]
        gen.data = _metro(fabric_templates=templates, deployment_type="virtual")

        physical_names_by_role = await gen._create_metro_devices(templates=templates, metro_id="metro-1")

        # Created — withholding them from cabling must not mean skipping them.
        assert [c.kwargs["device_role"] for c in gen.create_devices.await_args_list] == ["edge", "firewall"]
        assert all(c.kwargs["options"]["virtual"] for c in gen.create_devices.await_args_list)
        assert physical_names_by_role == {}

    @pytest.mark.asyncio
    async def test_hybrid_metro_reports_only_its_physical_kit(self) -> None:
        """deployment_type=hybrid allows both kinds in one metro, so the split is
        per fabric_templates entry rather than per metro."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(side_effect=[["eg-fr01", "eg-fr02"], ["fw-fr01", "fw-fr02"]])
        templates = [
            {"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen.data = _metro(fabric_templates=templates, deployment_type="hybrid")

        physical_names_by_role = await gen._create_metro_devices(templates=templates, metro_id="metro-1")

        assert physical_names_by_role == {"firewall": ["fw-fr01", "fw-fr02"]}


class TestMetroServiceCabling:
    """The leg from the metro's edge pair to its firewall/load-balancer pair.

    PR-1 as first written created a firewall pair wired to nothing but itself:
    the HA sync cable joined fw-fr01 to fw-fr02 and no cable reached either from
    the on-ramp routers that are supposed to front them.
    """

    @pytest.mark.asyncio
    async def test_firewall_pair_is_cabled_to_the_edge_pair(self) -> None:
        gen = _make_generator()
        gen._cable_border_services = AsyncMock()

        await gen._cable_metro_services(
            physical_names_by_role={"edge": ["eg-fr01", "eg-fr02"], "firewall": ["fw-fr01", "fw-fr02"]}
        )

        kwargs = gen._cable_border_services.await_args_list[-1].kwargs
        # The edges are the "border" side of the leg: they are the tier in the
        # underlay that the appliance hangs off, exactly as a DC's border-leafs
        # are for its own shared services.
        assert kwargs["border_names"] == ["eg-fr01", "eg-fr02"]
        assert kwargs["firewall_names"] == ["fw-fr01", "fw-fr02"]
        assert kwargs["load_balancer_names"] == []
        # Same role names dc.py uses, so a `firewall`-role port faces a firewall.
        # N9K-C9316D-GX_EDGE provides Ethernet1/[15-16] for this.
        assert kwargs["border_role_for"] == {"firewall": "firewall", "load-balancer": "load-balancer"}

    @pytest.mark.asyncio
    async def test_mode_is_always_pbr(self) -> None:
        """The inline mode would chain edge->fw->lb->edge, which needs a tier
        that both hands traffic to the appliance and takes it back. An on-ramp
        router pair forwards on to a DC or a cloud fabric instead, and the metro
        carries no connectivity_mode attribute to say otherwise."""
        gen = _make_generator()
        gen._cable_border_services = AsyncMock()

        await gen._cable_metro_services(
            physical_names_by_role={"edge": ["eg-fr01"], "firewall": ["fw-fr01"], "load-balancer": ["lb-fr01"]}
        )

        assert gen._cable_border_services.await_args_list[-1].kwargs["connectivity_mode"] == "pbr"

    @pytest.mark.parametrize(
        "physical_names_by_role",
        [
            # Paris: an on-ramp with no appliances at all.
            {"edge": ["eg-pa01", "eg-pa02"]},
            # Appliances declared with no edge pair to hang them off.
            {"firewall": ["fw-pa01", "fw-pa02"]},
            # Amsterdam re-run before anything was declared.
            {},
        ],
    )
    @pytest.mark.asyncio
    async def test_nothing_to_cable_is_a_no_op(self, physical_names_by_role: dict[str, list[str]]) -> None:
        """Most metros in the demo data have no services, and one side missing
        must not reach create_chain_cabling — it would log a missing-ports error,
        which FailOnErrorLogger turns into a task failure."""
        gen = _make_generator()
        gen._cable_border_services = AsyncMock()

        await gen._cable_metro_services(physical_names_by_role=physical_names_by_role)

        gen._cable_border_services.assert_not_awaited()
        gen.logger.error.assert_not_called()


class TestGeneratedMetroDeviceNames:
    """data/demos/30_all/08_interconnects/ references these names literally, so
    they are part of this generator's contract, not an implementation detail."""

    @pytest.mark.parametrize(
        ("metro", "role", "index", "expected"),
        [
            ("fr", "edge", 1, "eg-fr01"),
            ("fr", "edge", 2, "eg-fr02"),
            ("pa", "edge", 1, "eg-pa01"),
            ("fr", "firewall", 1, "fw-fr01"),
            ("fr", "load-balancer", 1, "lb-fr01"),
        ],
    )
    def test_standard_strategy_with_no_location_path(self, metro: str, role: str, index: int, expected: str) -> None:
        name = DeviceNamingConfig(strategy="standard").format_device_name(
            DeviceNameContext.from_indexes(fabric_name=metro, device_role=role, role_index=index, indexes=[])
        )
        assert name == expected


def _loopback(device: str, role: str, *, addressed: bool = True, iface_id: str | None = None) -> MagicMock:
    loopback = MagicMock()
    loopback.id = iface_id or f"{device}-{role}"
    loopback.role.value = role
    loopback.device.peer.name.value = device
    loopback.device.peer.id = f"dev-{device}"
    loopback.ip_address.initialized = addressed
    loopback.ip_address.id = f"ip-{device}-{role}"
    return loopback


def _evpn_generator(*, existing_as: bool = True, asn_pool: bool = True, loopbacks: list | None = None) -> Any:
    gen = _make_generator()
    gen._edge_devices = {"eg-fr01": "cisco_nxos"}
    gen._vtep_pool = MagicMock(id="vtep-pool")
    site_as = MagicMock(id="as-site")
    site_as.save = AsyncMock()
    metro_node = MagicMock()
    metro_node.save = AsyncMock()

    async def _get(**kwargs: Any) -> Any:
        if kwargs.get("kind") == "CoreNumberPool":
            return MagicMock(id="asn-pool") if asn_pool else None
        return metro_node

    gen.client.get = AsyncMock(side_effect=_get)
    gen.client.filters = AsyncMock(
        side_effect=[
            [site_as] if existing_as else [],
            [_loopback("eg-fr01", "loopback")] if loopbacks is None else loopbacks,
        ]
    )
    created = MagicMock()
    created.save = AsyncMock()
    gen.client.create = AsyncMock(return_value=created)
    gen.client.allocate_next_ip_address = AsyncMock(return_value=MagicMock(id="ip-vtep-new"))
    gen._metro_node = metro_node
    return gen


def _created(gen: Any, kind: str) -> list[dict[str, Any]]:
    """Data of every client.create() call for `kind` (a protocol class or its name)."""
    return [
        c.kwargs["data"]
        for c in gen.client.create.await_args_list
        if getattr(c.kwargs["kind"], "__name__", c.kwargs["kind"]) == kind
    ]


class TestMetroEvpn:
    """_ensure_metro_evpn: site AS, VTEP loopback and overlay BGP per edge."""

    @pytest.mark.asyncio
    async def test_no_edges_is_a_no_op(self) -> None:
        """A metro without physical edges makes no EVPN objects."""
        gen = _evpn_generator()
        gen._edge_devices = {}
        await gen._ensure_metro_evpn(metro_id="metro-1")
        gen.client.create.assert_not_awaited()
        gen.client.filters.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_site_as_reused_and_attached(self) -> None:
        """The site AS is found by description — never re-drawn from the pool."""
        gen = _evpn_generator()
        await gen._ensure_metro_evpn(metro_id="metro-1")
        assert gen.client.filters.await_args_list[0].kwargs["description__value"] == "fr EVPN site ASN"
        assert _created(gen, "RoutingAutonomousSystem") == []
        assert gen._metro_node.evpn_rt_as == {"id": "as-site"}
        gen._metro_node.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_existing_site_as_tracked_on_rerun(self) -> None:
        """The reused AS is not saved, so it is added to the run's group by
        hand — else delete_unused_nodes deletes it and the next run draws a
        new ASN, repointing every edge's overlay BGP."""
        gen = _evpn_generator()
        gen.client.group_context.related_node_ids = []
        await gen._ensure_metro_evpn(metro_id="metro-1")
        assert "as-site" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_new_site_as_drawn_from_metro_pool(self) -> None:
        gen = _evpn_generator(existing_as=False)
        await gen._ensure_metro_evpn(metro_id="metro-1")
        (as_data,) = _created(gen, "RoutingAutonomousSystem")
        assert as_data["asn"] == {"from_pool": {"id": "asn-pool"}}
        assert as_data["description"] == "fr EVPN site ASN"

    @pytest.mark.asyncio
    async def test_no_pool_and_no_as_logs_error(self) -> None:
        gen = _evpn_generator(existing_as=False, asn_pool=False)
        await gen._ensure_metro_evpn(metro_id="metro-1")
        gen.logger.error.assert_called_once()
        gen.client.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_vtep_allocated_and_overlay_bgp_created(self) -> None:
        gen = _evpn_generator()
        await gen._ensure_metro_evpn(metro_id="metro-1")
        assert gen.client.allocate_next_ip_address.await_args.kwargs["identifier"] == "eg-fr01-vtep"
        assert gen.client.allocate_next_ip_address.await_args.kwargs["prefix_length"] == 128
        (vtep,) = _created(gen, "DcimVirtualInterface")
        assert vtep["role"] == "loopback-vtep"
        assert vtep["name"] == "loopback1"
        assert "id" not in vtep
        (bgp,) = _created(gen, "ManagedBGP")
        assert bgp["name"] == "eg-fr01-bgp-overlay"
        assert bgp["local_as"] == {"id": "as-site"}
        assert bgp["router_id"] == {"id": "ip-eg-fr01-loopback"}
        assert bgp["process_role"] == "overlay"

    @pytest.mark.asyncio
    async def test_existing_vtep_reused_idempotently(self) -> None:
        """A re-run keeps the VTEP's id and address — no new allocation."""
        loopbacks = [_loopback("eg-fr01", "loopback"), _loopback("eg-fr01", "loopback-vtep", iface_id="vtep-1")]
        gen = _evpn_generator(loopbacks=loopbacks)
        await gen._ensure_metro_evpn(metro_id="metro-1")
        gen.client.allocate_next_ip_address.assert_not_awaited()
        (vtep,) = _created(gen, "DcimVirtualInterface")
        assert vtep["id"] == "vtep-1"
        assert vtep["ip_address"] == {"id": "ip-eg-fr01-loopback-vtep"}

    @pytest.mark.asyncio
    async def test_edge_without_addressed_loopback_skipped(self) -> None:
        gen = _evpn_generator(loopbacks=[_loopback("eg-fr01", "loopback", addressed=False)])
        await gen._ensure_metro_evpn(metro_id="metro-1")
        gen.logger.error.assert_called_once()
        assert _created(gen, "ManagedBGP") == []
