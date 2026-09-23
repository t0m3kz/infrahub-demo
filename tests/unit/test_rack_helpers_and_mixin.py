from __future__ import annotations

from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.helpers.rack import RackRolesHelper
from generators.topology.rack import RackGenerator, TopologyRackData


def _build_gen(*, deployment_type: Literal["middle_rack", "tor", "mixed"] = "mixed", rack_type: str = "network") -> Any:
    parent = {
        "id": "dc-1",
        "name": "DC1",
        "index": 1,
        "size": "S",
        "underlay_protocol": "ipv4",
        "naming_convention": "standard",
        "management_pool": {"id": "mgmt-pool", "name": "mgmt"},
    }
    pod = {
        "id": "pod-1",
        "name": "pod-1",
        "index": 1,
        "parent": parent,
        "leaf_interface_sorting_method": "top_down",
        "spine_interface_sorting_method": "bottom_up",
        "loopback_pool": {"id": "lo-pool", "name": "lo"},
        "prefix_pool": {"id": "p2p-pool", "name": "p2p"},
        "asn_pool": {"id": "asn-pool", "name": "asn"},
        "deployment_type": deployment_type,
        "layout": "S_MIXED",
        "fabric_templates": [
            {
                "role": "spine",
                "quantity": 2,
                "template": {
                    "id": "tmpl-spine",
                    "interfaces": [{"name": "Eth1/10"}, {"name": "Eth1/11"}],
                },
            }
        ],
    }
    suite = {"index": 1}
    leaf_template = {"id": "tmpl-leaf", "interfaces": [{"name": "Eth1/1"}, {"name": "Eth1/2"}]}
    tor_template = {
        "id": "tmpl-tor",
        "interfaces": [{"name": "Eth1/47", "role": "uplink"}, {"name": "Eth1/48", "role": "uplink"}],
    }
    rack = {
        "id": "rack-1",
        "name": "RACK-1",
        "index": 2,
        "rack_type": rack_type,
        "row_index": 2,
        "parent": suite,
        "pod": pod,
        "leafs": [{"role": "leaf", "quantity": 2, "template": leaf_template}],
        "tors": [{"role": "tor", "quantity": 2, "template": tor_template}],
    }

    gen = RackGenerator.__new__(RackGenerator)
    gen.data = cast(TopologyRackData, rack)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []

    # Fields normally prepared by _prepare_generation_context
    gen.fabric_name = "dc1"
    gen._naming_conv = "standard"
    gen._device_indexes = [1, 1, 1, 2, 2]
    gen._loopback_pool_id = "lo-pool"
    gen._management_pool_id = "mgmt-pool"
    gen._is_ipv6 = False
    gen._spine_device_names = ["dc1-pod1-spine-01", "dc1-pod1-spine-02"]
    gen._spine_interfaces = ["Eth1/10", "Eth1/11"]
    gen._technical_pool_id = "p2p-pool"
    gen._p2p_prefix_length = 31
    gen._routing_options = {"design": object(), "asn_pool": "asn-pool"}
    gen._created_device_names = set()
    gen._leaf_row_cache = None

    return gen


class TestRackRolesHelper:
    def test_expected_names_deterministic(self) -> None:
        gen = _build_gen()
        helper = RackRolesHelper(gen)

        names_a = helper.expected_names(role="leaf", quantity=2)
        names_b = helper.expected_names(role="leaf", quantity=2)

        assert names_a == names_b
        assert len(names_a) == 2

    def test_build_device_options_loopback_toggle(self) -> None:
        gen = _build_gen()
        helper = RackRolesHelper(gen)

        with_loopback = helper.build_device_options(allocate_loopback=True)
        without_loopback = helper.build_device_options(allocate_loopback=False)

        assert with_loopback["allocate_loopback"] is True
        assert with_loopback["loopback_pool"] == "lo-pool"
        assert with_loopback["loopback_prefix_length"] == 32
        assert without_loopback["allocate_loopback"] is False
        assert "group_name" not in without_loopback

    def test_build_device_options_group_name_override(self) -> None:
        gen = _build_gen()
        helper = RackRolesHelper(gen)

        options = helper.build_device_options(allocate_loopback=False, group_name="loadbalancers")

        assert options["group_name"] == "loadbalancers"

    def test_template_interfaces_filters_by_role(self) -> None:
        template = {
            "id": "tmpl",
            "interfaces": [
                {"name": "Eth1/1", "role": "uplink"},
                {"name": "Eth1/2", "role": "downlink"},
                {"name": "Eth1/3", "role": "uplink"},
            ],
        }

        all_ifaces = RackRolesHelper.template_interfaces(template)
        uplinks = RackRolesHelper.template_interfaces(template, role="uplink")

        assert all_ifaces == ["Eth1/1", "Eth1/2", "Eth1/3"]
        assert uplinks == ["Eth1/1", "Eth1/3"]

    def test_overlay_only_routing_options(self) -> None:
        """border_leafs_per_rack() was deleted along with border-leaf's rack-level
        generation (moved to TopologyDataCenter's fabric_templates) — only
        overlay_only_routing_options() coverage remains relevant here."""
        gen = _build_gen()
        helper = RackRolesHelper(gen)

        overlay = helper.overlay_only_routing_options()
        assert overlay["skip_underlay"] is True
        assert "skip_underlay" not in gen._routing_options


def _mock_pod_pools(*, loopback_id: str | None, prefix_id: str | None, asn_id: str | None = "asn-pool") -> MagicMock:
    """Mirror RelatedNode.id, which returns None when unset (unlike .peer.id,
    which raises ValueError if neither an id nor an hfid is set — see
    RelatedNode.get()'s docstring)."""
    pod_obj = MagicMock()
    pod_obj.loopback_pool = MagicMock(id=loopback_id)
    pod_obj.prefix_pool = MagicMock(id=prefix_id)
    pod_obj.asn_pool = MagicMock(id=asn_id)
    return pod_obj


class TestRackMixinAdditional:
    @pytest.mark.asyncio
    async def test_prepare_generation_context_missing_pools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pools stay missing across every retry attempt — the rack refuses to generate.

        Carrying on would build devices and cabling with no routing behind them,
        which looks healthy until you count BGP processes. Raising fails the
        generator task instead, and nothing has been created yet at this point.
        """
        gen = _build_gen()
        gen.data["pod"]["loopback_pool"] = None
        gen.client.get = AsyncMock(return_value=_mock_pod_pools(loopback_id=None, prefix_id=None))
        monkeypatch.setattr("generators.rack.asyncio.sleep", AsyncMock())

        with pytest.raises(RuntimeError, match="loopback_pool"):
            await gen._prepare_generation_context()

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_prepare_generation_context_pools_recover_after_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pools missing on the first refetch, present on the second — no error, ids updated."""
        gen = _build_gen()
        gen.data["pod"]["loopback_pool"] = None
        gen.data["pod"]["prefix_pool"] = None
        gen.client.get = AsyncMock(
            side_effect=[
                _mock_pod_pools(loopback_id=None, prefix_id=None),
                _mock_pod_pools(loopback_id="lo-pool-2", prefix_id="p2p-pool-2"),
            ]
        )
        sleep_mock = AsyncMock()
        monkeypatch.setattr("generators.rack.asyncio.sleep", sleep_mock)

        await gen._prepare_generation_context()

        gen.logger.error.assert_not_called()
        sleep_mock.assert_awaited_once()
        assert gen._loopback_pool_id == "lo-pool-2"
        assert gen._technical_pool_id == "p2p-pool-2"

    @pytest.mark.asyncio
    async def test_prepare_generation_context_waits_for_asn_pool_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An eBGP rack waits for asn_pool even when loopback/prefix are already there.

        add_pod creates the loopback/prefix pools first and only links the DC's
        fabric ASN pool onto the pod afterwards, so this exact snapshot — both
        IP pools resolved, asn_pool still None — is what a racing rack reads.
        Proceeding on it produced devices with no BGP process at all and no
        failed task to show for it.
        """
        gen = _build_gen()
        gen.data["pod"]["asn_pool"] = None
        gen.client.get = AsyncMock(
            side_effect=[
                _mock_pod_pools(loopback_id="lo-pool", prefix_id="p2p-pool", asn_id=None),
                _mock_pod_pools(loopback_id="lo-pool", prefix_id="p2p-pool", asn_id="asn-pool-late"),
            ]
        )
        sleep_mock = AsyncMock()
        monkeypatch.setattr("generators.rack.asyncio.sleep", sleep_mock)

        await gen._prepare_generation_context()

        gen.logger.error.assert_not_called()
        sleep_mock.assert_awaited_once()
        assert gen._routing_options["asn_pool"] == "asn-pool-late"

    @pytest.mark.asyncio
    async def test_prepare_generation_context_ospf_underlay_ignores_asn_pool(self) -> None:
        """An OSPF underlay allocates no per-device ASN, so a missing asn_pool
        is neither waited on nor an error."""
        gen = _build_gen()
        gen.data["pod"]["parent"]["routing_strategy"] = "ospf-ibgp"
        gen.data["pod"]["asn_pool"] = None
        gen.client.get = AsyncMock()

        await gen._prepare_generation_context()

        gen.client.get.assert_not_awaited()
        gen.logger.error.assert_not_called()
        assert "asn_pool" not in gen._routing_options

    @pytest.mark.asyncio
    async def test_prepare_generation_context_missing_asn_pool_refuses_to_generate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """asn_pool never arrives on an eBGP rack — the whole run fails.

        There is no legitimate state in which it is absent (add_dc creates the
        fabric ASN pool, add_pod links it onto the pod), so a rack that still
        cannot see it must not generate devices it will be unable to route.
        """
        gen = _build_gen()
        gen.data["pod"]["asn_pool"] = None
        gen.client.get = AsyncMock(
            return_value=_mock_pod_pools(loopback_id="lo-pool", prefix_id="p2p-pool", asn_id=None)
        )
        monkeypatch.setattr("generators.rack.asyncio.sleep", AsyncMock())

        with pytest.raises(RuntimeError, match="asn_pool"):
            await gen._prepare_generation_context()

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_prepare_generation_context_success_sets_fields(self) -> None:
        gen = _build_gen()

        await gen._prepare_generation_context()

        assert gen.deployment_id == "dc-1"
        assert gen.pod_name == "pod-1"
        assert gen.fabric_name == "dc1"
        assert gen._technical_pool_id == "p2p-pool"
        assert gen._p2p_prefix_length == 31
        assert gen._routing_options["asn_pool"] == "asn-pool"

    # _derive_super_spine_info() was dead code (never called) and was deleted
    # from RackMixin — see tests/unit/test_rack_parse_and_checksum.py's
    # TestDeriveSpineInfo for the still-live _derive_spine_info() coverage.
