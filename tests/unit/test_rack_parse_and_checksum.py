"""Unit tests for RackGenerator parsing and row-dependent-rack fan-out.

Covers:
- _parse_rack_data()                    – direct node dict vs GQL result vs unknown shape
- _fan_out_to_row_dependent_racks()      – only fires for mixed+network with leafs
- generate() role-vs-deployment gate     – _role_compatibility_errors
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.models import (
    DeviceRole,
    Interface,
    LocationSuiteModel,
    PodDesign,
    RackModel,
    RackParent,
    RackPod,
    Template,
)
from generators.topology.rack import RackGenerator

# ---------------------------------------------------------------------------
# Helpers (shared with test_rack_offset_calculation.py)
# ---------------------------------------------------------------------------


_DEFAULT_LEAF = DeviceRole(role="leaf", quantity=2, template=Template(id="tmpl-leaf"))


def _design_for(deployment_type: str) -> PodDesign:
    """Build a PodDesign whose layout derives the given deployment_type.

    deployment_type is now a computed property (PodDesign.deployment_type),
    derived from network_racks_per_row / max_tors_per_compute_rack — see
    generators/models.py.
    """
    return PodDesign(
        id="design-1",
        name="test-design",
        rows=1,
        compute_racks_per_row=1,
        network_racks_per_row=0 if deployment_type == "tor" else 1,
        max_tors_per_compute_rack=0 if deployment_type == "middle_rack" else 1,
    )


def _build_rack_generator(
    *,
    deployment_type: str = "mixed",
    rack_type: str = "network",
    rack_index: int = 5,
    row_index: int = 1,
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
        spine_template=Template(id="tmpl-spine"),
        design=_design_for(deployment_type),
    )
    suite = LocationSuiteModel(index=1)
    rack = RackModel(
        id="rack-net-1",
        name="MUC-1-S-1-R-1-5",
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
    gen.client.filters = AsyncMock(return_value=[])
    gen.branch = "test-branch"
    gen.client.task = MagicMock()
    gen.client.task.filter = AsyncMock(return_value=[])
    return gen


def _mock_rack(name: str, rack_type: str, row_index: int = 1) -> MagicMock:
    r = MagicMock()
    r.id = f"id-{name}"
    r.name = MagicMock(value=name)
    r.rack_type = MagicMock(value=rack_type)
    r.row_index = MagicMock(value=row_index)
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


class TestFanOutToRowDependentRacks:
    """_fan_out_to_row_dependent_racks(): network rack -> add_rack for tor/compute
    racks in its own row (mixed deployment only), via run_generator
    instead of a checksum write."""

    @pytest.mark.asyncio
    async def test_non_mixed_deployment_does_nothing(self) -> None:
        gen = _build_rack_generator(deployment_type="middle_rack", rack_type="network")
        gen.client.filters = AsyncMock(return_value=[])
        gen.run_generator = AsyncMock()

        await gen._fan_out_to_row_dependent_racks()

        gen.client.filters.assert_not_awaited()
        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tor_rack_type_does_nothing(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="tor")
        gen.client.filters = AsyncMock(return_value=[])
        gen.run_generator = AsyncMock()

        await gen._fan_out_to_row_dependent_racks()

        gen.client.filters.assert_not_awaited()
        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_leafs_skips_fan_out(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="network", leafs=[])
        gen.client.filters = AsyncMock(return_value=[])
        gen.run_generator = AsyncMock()

        await gen._fan_out_to_row_dependent_racks()

        gen.client.filters.assert_not_awaited()
        gen.run_generator.assert_not_awaited()
        gen.logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_fans_out_to_tor_and_compute_racks_in_same_row(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="network")
        tor_rack = _mock_rack("TOR-RACK-1", "tor")
        compute_rack = _mock_rack("COMP-RACK-1", "compute")
        network_rack = _mock_rack("NET-RACK-2", "network")  # sibling network rack, not fanned out to
        gen.client.filters = AsyncMock(return_value=[tor_rack, compute_rack, network_rack])
        gen.run_generator = AsyncMock()

        await gen._fan_out_to_row_dependent_racks()

        gen.run_generator.assert_awaited_once()
        args, kwargs = gen.run_generator.call_args
        assert args[0] == "add_rack"
        assert sorted(args[1]) == sorted([tor_rack.id, compute_rack.id])
        # fire-and-forget: a network rack's own task must not stay RUNNING while
        # waiting on its row-dependent racks, or their own wait-for-parent guard
        # (checking for an in-flight "Run generator add_rack" on this same rack)
        # would deadlock against the very call waiting on it.
        assert kwargs["wait"] is False

    @pytest.mark.asyncio
    async def test_no_row_dependent_racks_calls_with_empty_list(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="network")
        gen.client.filters = AsyncMock(return_value=[])
        gen.run_generator = AsyncMock()

        await gen._fan_out_to_row_dependent_racks()

        gen.run_generator.assert_awaited_once_with("add_rack", [], wait=False)


class TestGenerateRackGating:
    @pytest.mark.asyncio
    async def test_empty_input_logs_error_and_stops(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="network")
        with patch("generators.topology.rack.parse_rack_data") as mock_parse:
            await gen.generate({})

        mock_parse.assert_not_called()
        gen.logger.error.assert_called_once_with("Generator received empty data")

    @pytest.mark.asyncio
    async def test_parse_failure_logs_error_and_stops(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="network")

        with patch("generators.topology.rack.parse_rack_data", side_effect=ValueError("boom")):
            await gen.generate({"any": "shape"})

        gen.logger.error.assert_called_once()
        assert "Generation failed due to boom" in gen.logger.error.call_args.args[0]

    @pytest.mark.asyncio
    async def test_endpoint_only_rack_skips_before_role_check(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="compute", leafs=[])
        gen.data.tors = []
        gen.data.l2_leafs = []
        gen.data.access_leafs = []
        gen.data.border_leafs = []
        gen._role_compatibility_errors = MagicMock()
        gen._prepare_generation_context = MagicMock()

        with patch("generators.topology.rack.parse_rack_data", return_value=gen.data):
            await gen.generate({"any": "shape"})

        gen._role_compatibility_errors.assert_not_called()
        gen._prepare_generation_context.assert_not_called()


# ---------------------------------------------------------------------------
# _role_compatibility_errors / generate() role-vs-deployment gate
#
# middle_rack has no cabling strategy for "tor" (_generate_tors always
# targets the pod spines; only middle_rack's offset=0 branch claims
# otherwise) — see ROLES_BY_DEPLOYMENT_TYPE. "tor" and "l2_leaf"/
# "access_leaf" are mutually exclusive on the same rack in any deployment.
# ---------------------------------------------------------------------------


class TestRoleDeploymentCompatibility:
    def test_middle_rack_with_tor_is_incompatible(self) -> None:
        gen = _build_rack_generator(deployment_type="middle_rack", rack_type="network")
        gen.data.tors = [DeviceRole(role="tor", quantity=2, template=Template(id="tmpl-tor"))]
        gen.data.l2_leafs = []
        gen.data.access_leafs = []
        gen.data.border_leafs = []

        errors = gen._role_compatibility_errors("middle_rack")

        assert len(errors) == 1
        assert "tor" in errors[0]
        assert "middle_rack" in errors[0]

    def test_middle_rack_with_access_leaf_is_compatible(self) -> None:
        gen = _build_rack_generator(deployment_type="middle_rack", rack_type="network")
        gen.data.tors = []
        gen.data.l2_leafs = []
        gen.data.access_leafs = [DeviceRole(role="access-leaf", quantity=2, template=Template(id="tmpl-al"))]
        gen.data.border_leafs = []

        assert gen._role_compatibility_errors("middle_rack") == []

    def test_tor_and_access_leaf_mutually_exclusive_regardless_of_deployment(self) -> None:
        gen = _build_rack_generator(deployment_type="mixed", rack_type="compute")
        gen.data.tors = [DeviceRole(role="tor", quantity=2, template=Template(id="tmpl-tor"))]
        gen.data.l2_leafs = []
        gen.data.access_leafs = [DeviceRole(role="access-leaf", quantity=2, template=Template(id="tmpl-al"))]
        gen.data.border_leafs = []

        errors = gen._role_compatibility_errors("mixed")

        assert len(errors) == 1
        assert "mutually exclusive" in errors[0]

    def test_mixed_tor_alone_is_compatible(self) -> None:
        """mixed+tor is a real, cabled path (calculate_cabling_offsets' mixed+tor branch)."""
        gen = _build_rack_generator(deployment_type="mixed", rack_type="compute", leafs=[])
        gen.data.tors = [DeviceRole(role="tor", quantity=2, template=Template(id="tmpl-tor"))]
        gen.data.l2_leafs = []
        gen.data.access_leafs = []
        gen.data.border_leafs = []

        assert gen._role_compatibility_errors("mixed") == []

    def test_tor_deployment_with_leaf_is_incompatible(self) -> None:
        gen = _build_rack_generator(deployment_type="tor", rack_type="tor", leafs=[])
        gen.data.leafs = [DeviceRole(role="leaf", quantity=2, template=Template(id="tmpl-leaf"))]
        gen.data.tors = []
        gen.data.l2_leafs = []
        gen.data.access_leafs = []
        gen.data.border_leafs = []

        errors = gen._role_compatibility_errors("tor")

        assert len(errors) == 1
        assert "leaf" in errors[0]

    @pytest.mark.asyncio
    async def test_generate_stops_before_prepare_context_on_role_conflict(self) -> None:
        """The role-compatibility gate runs before _prepare_generation_context, so a
        bad rack never reaches device creation."""
        gen = _build_rack_generator(deployment_type="middle_rack", rack_type="network")
        gen.data.tors = [DeviceRole(role="tor", quantity=2, template=Template(id="tmpl-tor"))]
        gen.data.l2_leafs = []
        gen.data.access_leafs = []
        gen.data.border_leafs = []
        gen._prepare_generation_context = MagicMock()

        with patch("generators.topology.rack.parse_rack_data", return_value=gen.data):
            await gen.generate({"any": "shape"})

        gen._prepare_generation_context.assert_not_called()
        gen.logger.error.assert_called()
