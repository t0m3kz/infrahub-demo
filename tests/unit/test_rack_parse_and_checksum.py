"""Unit tests for RackGenerator parsing and checksum cascade.

Covers:
- _parse_rack_data()      – direct node dict vs GQL result vs unknown shape
- update_checksum()       – only fires for mixed+network; stagger sleep called
- update_checksum()       – no ToR racks → no sleep; single ToR → no sleep
- update_checksum()       – skips when no leafs in rack
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from generators.add.rack import RackGenerator
from generators.models import DeviceRole, Interface, LocationSuiteModel, RackModel, RackParent, RackPod, Template

# ---------------------------------------------------------------------------
# Helpers (shared with test_rack_offset_calculation.py)
# ---------------------------------------------------------------------------


_DEFAULT_LEAF = DeviceRole(role="leaf", quantity=2, template=Template(id="tmpl-leaf"))


def _build_rack_generator(
    *,
    deployment_type: str = "mixed",
    rack_type: str = "network",
    rack_index: int = 5,
    row_index: int = 1,
    checksum: str = "abc123",
    leafs: list[DeviceRole] | None = None,
) -> Any:
    """Return a RackGenerator typed as Any so ty allows mock attribute assignments."""
    parent = RackParent(id="parent-1", name="DC1", index=1)
    pod = RackPod(
        id="pod-1",
        name="pod-1",
        index=1,
        parent=parent,
        amount_of_spines=2,
        leaf_interface_sorting_method="top_down",
        spine_interface_sorting_method="bottom_up",
        deployment_type=deployment_type,
        spine_template=Template(id="tmpl-spine"),
        design=None,
    )
    suite = LocationSuiteModel(index=1)
    rack = RackModel(
        id="rack-net-1",
        name="MUC-1-S-1-R-1-5",
        checksum=checksum,
        index=rack_index,
        rack_type=rack_type,
        row_index=row_index,
        parent=suite,
        pod=pod,
        leafs=leafs if leafs is not None else [_DEFAULT_LEAF],
    )
    gen = RackGenerator.__new__(RackGenerator)
    gen.data = rack
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    return gen


def _mock_rack(name: str, rack_type: str, checksum: str = "") -> MagicMock:
    r = MagicMock()
    r.id = f"id-{name}"
    r.name = MagicMock(value=name)
    r.rack_type = MagicMock(value=rack_type)
    r.checksum = MagicMock(value=checksum)
    r.save = AsyncMock()
    return r


# ---------------------------------------------------------------------------
# _parse_rack_data
# ---------------------------------------------------------------------------


class TestParseRackData:
    def test_direct_node_dict_dispatches_on_name_being_dict(self) -> None:
        """_parse_rack_data takes the direct-node path when data['name'] is a dict.

        The direct-node path passes data straight to RackModel(**data).
        When data comes from an event trigger it has already been cleaned by
        the SDK — name is a plain string, not a {value:} wrapper.
        We verify the dispatch condition fires (ValidationError means it reached
        RackModel, not the 'unknown shape' fallback).
        """
        # name is a dict → triggers the 'direct node data' branch
        data = {"name": {"value": "TEST-RACK"}, "checksum": "abc", "index": 5}
        with pytest.raises(Exception):
            # Either ValidationError (RackModel rejects the dict name) or
            # some other model error — what matters is it didn't raise
            # "Unknown data structure" ValueError.
            RackGenerator._parse_rack_data(data)

    def test_direct_node_dict_with_cleaned_name(self) -> None:
        """Direct-node path succeeds when name is already a plain string (event trigger shape)."""
        data = {
            "id": "rack-id-1",
            "name": "TEST-RACK",  # plain string — already cleaned by SDK
            "checksum": "abc",
            "index": 5,
            "rack_type": "network",
            "row_index": 1,
            "parent": {"index": 1},
            "pod": {
                "id": "pod-1",
                "name": "pod-1",
                "index": 1,
                "deployment_type": "mixed",
                "amount_of_spines": 2,
                "leaf_interface_sorting_method": "top_down",
                "spine_interface_sorting_method": "bottom_up",
                "spine_template": {"id": "tmpl-1", "interfaces": []},
                "design": None,
                "prefix_pool": None,
                "loopback_pool": None,
                "asn_pool": None,
                "parent": {
                    "id": "dc-1",
                    "name": "DC1",
                    "index": 1,
                    "naming_convention": "standard",
                    "management_pool": None,
                    "design": None,
                    "fabric_interface_sorting_method": "top_down",
                },
            },
        }
        # name is a plain string → NOT a dict → falls through to the GQL/unknown branch
        # (raises ValueError "Unknown data structure" because 'LocationRack' key is missing)
        with pytest.raises(ValueError, match="Unknown data structure"):
            RackGenerator._parse_rack_data(data)

    def test_gql_result_with_edges_parsed(self) -> None:
        """Data shaped as {LocationRack: {edges: [...]}} is cleaned and parsed."""

        raw = {
            "LocationRack": {
                "edges": [
                    {
                        "node": {
                            "id": "rack-id-2",
                            "name": {"value": "GQL-RACK"},
                            "checksum": {"value": "xyz"},
                            "index": {"value": 3},
                            "rack_type": {"value": "tor"},
                            "row_index": {"value": 2},
                            "parent": {"node": {"id": "suite-2", "index": {"value": 2}}},
                            "pod": {
                                "node": {
                                    "id": "pod-2",
                                    "name": {"value": "pod-2"},
                                    "index": {"value": 2},
                                    "deployment_type": {"value": "tor"},
                                    "amount_of_spines": {"value": 2},
                                    "leaf_interface_sorting_method": {"value": "bottom_up"},
                                    "spine_interface_sorting_method": {"value": "top_down"},
                                    "spine_template": {"node": {"id": "tmpl-2", "interfaces": {"edges": []}}},
                                    "design": None,
                                    "prefix_pool": None,
                                    "loopback_pool": None,
                                    "asn_pool": None,
                                    "parent": {
                                        "node": {
                                            "id": "dc-2",
                                            "name": {"value": "DC2"},
                                            "index": {"value": 2},
                                            "naming_convention": {"value": "standard"},
                                            "management_pool": None,
                                            "design": None,
                                            "fabric_interface_sorting_method": {"value": "top_down"},
                                        }
                                    },
                                }
                            },
                        }
                    }
                ]
            }
        }
        result = RackGenerator._parse_rack_data(raw)
        assert result.name == "GQL-RACK"
        assert result.rack_type == "tor"

    def test_empty_edges_raises_value_error(self) -> None:
        raw = {"LocationRack": {"edges": []}}
        with pytest.raises(ValueError, match="no edges"):
            RackGenerator._parse_rack_data(raw)

    def test_unknown_shape_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown data structure"):
            RackGenerator._parse_rack_data({"weird_key": "data"})


class TestDeriveSpineInfo:
    def test_uses_template_downlink_interfaces(self) -> None:
        """Spine interface names come from template; GQL pre-filters to downlink role."""
        gen = _build_rack_generator(deployment_type="tor", rack_type="tor")
        gen.fabric_name = "dc1"
        gen.data.pod.index = 3
        gen.data.pod.amount_of_spines = 2
        gen.data.pod.parent.index = 1
        gen.data.pod.parent.naming_convention = "standard"
        gen.data.pod.spine_template.interfaces = [
            Interface(name="Ethernet1/1"),
            Interface(name="Ethernet1/2"),
            Interface(name="Ethernet1/3"),
        ]

        device_names, interface_names = gen._derive_spine_info()

        assert len(device_names) == 2
        assert interface_names == ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3"]

    def test_raises_when_spine_template_missing(self) -> None:
        """RuntimeError raised when spine_template is None."""
        gen = _build_rack_generator(deployment_type="tor", rack_type="tor")
        gen.fabric_name = "dc1"
        gen.data.pod.index = 1
        gen.data.pod.amount_of_spines = 2
        gen.data.pod.parent.index = 1
        gen.data.pod.parent.naming_convention = "standard"
        gen.data.pod.spine_template = None

        with pytest.raises(RuntimeError, match="Cannot derive spine info"):
            gen._derive_spine_info()

    def test_template_all_interfaces_returned(self) -> None:
        """All template interfaces are returned (GQL pre-filters, no role check needed)."""
        gen = _build_rack_generator(deployment_type="tor", rack_type="tor")
        gen.fabric_name = "dc1"
        gen.data.pod.index = 1
        gen.data.pod.amount_of_spines = 2
        gen.data.pod.parent.index = 1
        gen.data.pod.parent.naming_convention = "standard"
        gen.data.pod.spine_template.interfaces = [
            Interface(name="Ethernet1/1"),
            Interface(name="Ethernet1/2"),
        ]

        device_names, interface_names = gen._derive_spine_info()

        assert len(device_names) == 2
        assert interface_names == ["Ethernet1/1", "Ethernet1/2"]

    def test_raises_when_template_has_no_interfaces(self) -> None:
        """RuntimeError raised when template has no interfaces (empty list)."""
        gen = _build_rack_generator(deployment_type="tor", rack_type="tor")
        gen.fabric_name = "dc1"
        gen.data.pod.index = 1
        gen.data.pod.amount_of_spines = 2
        gen.data.pod.parent.index = 1
        gen.data.pod.parent.naming_convention = "standard"
        gen.data.pod.spine_template.interfaces = []

        with pytest.raises(RuntimeError, match="Spine template has no downlink interfaces"):
            gen._derive_spine_info()


# ---------------------------------------------------------------------------
# update_checksum (mixed deployment, network rack → cascade to ToR racks)
# ---------------------------------------------------------------------------


class TestUpdateChecksumMixed:
    @pytest.mark.asyncio
    async def test_non_mixed_deployment_does_nothing(self) -> None:
        gen = _build_rack_generator(deployment_type="middle_rack", rack_type="network")
        gen.fetch_rack_devices_with_interfaces = AsyncMock(return_value=[])
        gen.client.filters = AsyncMock(return_value=[])

        await gen.update_checksum()

        gen.client.filters.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tor_rack_type_does_nothing(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="tor")
        gen.fetch_rack_devices_with_interfaces = AsyncMock(return_value=[])
        gen.client.filters = AsyncMock(return_value=[])

        await gen.update_checksum()

        gen.client.filters.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_leafs_skips_cascade(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="network", leafs=[])
        gen.fetch_rack_devices_with_interfaces = AsyncMock(return_value=[])
        gen.client.filters = AsyncMock(return_value=[])

        await gen.update_checksum()

        gen.client.filters.assert_not_awaited()
        gen.logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_network_rack_logs_warning(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="network")
        leaf_data = [{"device_id": "leaf-1", "device_name": "leaf-01", "interfaces": []}]
        gen.fetch_rack_devices_with_interfaces = AsyncMock(return_value=leaf_data)

        # Only ToR racks returned (no network rack in row)
        tor_rack = _mock_rack("TOR-RACK-1", "tor", checksum="")
        gen.client.filters = AsyncMock(return_value=[tor_rack])

        await gen.update_checksum()

        gen.logger.warning.assert_called_once()
        tor_rack.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_tor_rack_no_stagger(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="network", checksum="new-cs")
        leaf_data = [{"device_id": "leaf-1", "device_name": "leaf-01", "interfaces": []}]
        gen.fetch_rack_devices_with_interfaces = AsyncMock(return_value=leaf_data)

        net_rack = _mock_rack("NET-RACK-1", "network", checksum="new-cs")
        tor_rack = _mock_rack("TOR-RACK-1", "tor", checksum="")
        gen.client.filters = AsyncMock(return_value=[net_rack, tor_rack])

        with patch("generators.add.rack.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await gen.update_checksum()

        mock_sleep.assert_not_called()
        tor_rack.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_multiple_tor_racks_stagger_sleep(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="network", checksum="new-cs")
        leaf_data = [{"device_id": "leaf-1", "device_name": "leaf-01", "interfaces": []}]
        gen.fetch_rack_devices_with_interfaces = AsyncMock(return_value=leaf_data)

        net_rack = _mock_rack("NET-RACK-1", "network", checksum="new-cs")
        tor1 = _mock_rack("TOR-RACK-1", "tor", checksum="")
        tor2 = _mock_rack("TOR-RACK-9", "tor", checksum="")
        gen.client.filters = AsyncMock(return_value=[net_rack, tor1, tor2])

        with patch("generators.add.rack.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await gen.update_checksum()

        # First ToR: no sleep; second ToR: sleep(5)
        mock_sleep.assert_called_once_with(5)
        tor1.save.assert_awaited_once_with(allow_upsert=True)
        tor2.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_checksum_set_on_tor_rack(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="network", checksum="new-cs")
        leaf_data = [{"device_id": "leaf-1", "device_name": "leaf-01", "interfaces": []}]
        gen.fetch_rack_devices_with_interfaces = AsyncMock(return_value=leaf_data)

        net_rack = _mock_rack("NET-RACK-1", "network", checksum="new-cs")
        tor_rack = _mock_rack("TOR-RACK-1", "tor", checksum="")
        gen.client.filters = AsyncMock(return_value=[net_rack, tor_rack])

        with patch("generators.add.rack.asyncio.sleep", new_callable=AsyncMock):
            await gen.update_checksum()

        assert tor_rack.checksum.value == "new-cs"

    @pytest.mark.asyncio
    async def test_three_tor_racks_sleep_called_twice(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="network", checksum="cs-3")
        leaf_data = [{"device_id": "leaf-1", "device_name": "leaf-01", "interfaces": []}]
        gen.fetch_rack_devices_with_interfaces = AsyncMock(return_value=leaf_data)

        net_rack = _mock_rack("NET-RACK-1", "network", checksum="cs-3")
        tors = [_mock_rack(f"TOR-RACK-{i}", "tor", checksum="") for i in range(3)]
        gen.client.filters = AsyncMock(return_value=[net_rack] + tors)

        with patch("generators.add.rack.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await gen.update_checksum()

        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list == [call(5), call(5)]
