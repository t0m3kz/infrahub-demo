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
from generators.topology.colocation import (
    _COLO_HA_KIND_BY_ROLE,
    _COLO_LOOPBACK_PREFIX_LENGTH,
    _COLO_MANAGEMENT_PREFIX_LENGTH,
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
        gen = _make_generator()
        entries = [
            {"role": role, "quantity": 2, "template": _EDGE_TEMPLATE} for role in ("edge", "firewall", "load-balancer")
        ]
        gen.data = _metro(fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries


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
        gen._create_metro_devices = AsyncMock()
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


class TestPools:
    @pytest.mark.asyncio
    async def test_pool_names_kinds_and_parents(self) -> None:
        gen = _make_generator()
        gen._ensure_sliced_pool = AsyncMock(side_effect=lambda **kw: MagicMock(id=f"pool-{kw['pool_name']}"))
        gen.upsert_asn_pool = AsyncMock()
        gen.client.get = AsyncMock(return_value=AsyncMock())

        await gen._ensure_colocation_pools(metro_id="metro-1")

        calls = {c.kwargs["pool_name"]: c.kwargs for c in gen._ensure_sliced_pool.await_args_list}
        assert set(calls) == {"fr-loopback-pool", "fr-management-pool", "fr-technical-pool"}
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
    async def test_asn_pool_is_attached_by_the_pool_helper(self) -> None:
        gen = _make_generator()
        gen._ensure_sliced_pool = AsyncMock(return_value=MagicMock(id="pool-x"))
        gen.upsert_asn_pool = AsyncMock()
        gen.client.get = AsyncMock(return_value=AsyncMock())

        await gen._ensure_colocation_pools(metro_id="metro-1")

        kwargs = gen.upsert_asn_pool.await_args_list[-1].kwargs
        assert kwargs["pool_name"] == "fr-asn-pool"
        assert kwargs["parent_kind"] == "TopologyColocationMetro"
        assert kwargs["parent_id"] == "metro-1"
        assert kwargs["parent_attr"] == "asn_pool"
        # Private-use 4-byte range, from the same deterministic name-hash grid
        # DC fabric ASNs use, so a metro can never collide with a DC.
        assert 4_200_000_000 <= kwargs["start_range"] < kwargs["end_range"] <= 4_294_967_295

    @pytest.mark.asyncio
    async def test_ip_pools_are_attached_with_a_plain_save(self) -> None:
        """allow_upsert=True would resend every relationship and re-fire the
        fabric_templates `updated` trigger on each pool attach."""
        gen = _make_generator()
        gen._ensure_sliced_pool = AsyncMock(side_effect=lambda **kw: MagicMock(id=f"id-{kw['pool_name']}"))
        gen.upsert_asn_pool = AsyncMock()
        metro = AsyncMock()
        gen.client.get = AsyncMock(return_value=metro)

        await gen._ensure_colocation_pools(metro_id="metro-1")

        assert metro.loopback_pool == {"id": "id-fr-loopback-pool"}
        assert metro.management_pool == {"id": "id-fr-management-pool"}
        assert metro.technical_pool == {"id": "id-fr-technical-pool"}
        metro.save.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_sliced_address_pool_wraps_the_allocated_prefix(self) -> None:
        gen = _make_generator()
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock(id="parent-1"))
        pool = AsyncMock()
        gen.client.create = AsyncMock(return_value=pool)

        await gen._ensure_sliced_pool(
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

        await gen._ensure_sliced_pool(
            pool_name="fr-technical-pool",
            parent_pool_name="Technical-IPv4",
            prefix_length=26,
            role="technical",
            kind="prefix",
        )

        create = gen.client.create.await_args_list[-1].kwargs
        assert create["kind"].__name__ == "CoreIPPrefixPool"
        assert create["data"]["default_prefix_type"] == "IpamPrefix"


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
    async def test_load_balancer_overrides_the_derived_group_name(self) -> None:
        """create_devices() would derive "load-balancers"; the bootstrap group
        is "loadbalancers"."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["lb-fr01"])
        templates = [{"role": "load-balancer", "quantity": 2, "template": _EDGE_TEMPLATE}]

        gen.data = _metro(fabric_templates=templates)
        await gen._create_metro_devices(templates=templates, metro_id="metro-1")

        assert gen.create_devices.await_args_list[-1].kwargs["options"]["group_name"] == "loadbalancers"

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
        """An HA mapping for a role the metro would never build is dead code,
        and a role the metro builds but cannot pair is a silent single point of
        failure — keep the two in step."""
        assert set(_COLO_HA_KIND_BY_ROLE) < _COLO_VALID_FABRIC_ROLES

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
