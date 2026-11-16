from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.dc.rack_plan import RowLeafs
from generators.dcim.cabling_plan import PortFacts
from generators.dcim.template_interfaces import TemplatePort
from generators.plan import NodeSpec, TopologyPlan
from tests.unit.rack_generator_helpers import build_rack_generator, rack_reads
from tests.unit.simulators.fabric_designs import pod_design


class TestRackGeneratorMethods:
    def test_present_roles_lists_the_positive_quantity_roles(self) -> None:
        gen = build_rack_generator()
        assert gen._present_roles() == {"leaf", "tor"}

    def test_planned_previous_row_rack_slots_uses_design_capacity(self) -> None:
        gen = build_rack_generator()

        gen.data["row_index"] = 3
        # 8 compute + 1 network rack positions per row: a mixed row numbers both.
        gen.data["deployment"]["design"] = pod_design("S_MIXED")
        assert gen._planned_previous_row_rack_slots() == 18

        gen.data["row_index"] = 3
        gen.data["deployment"]["design"] = pod_design("S_TOR")  # compute_racks_per_row = 8
        assert gen._planned_previous_row_rack_slots() == 16

        gen.data["row_index"] = 3
        gen.data["deployment"]["design"] = pod_design("S_MIDDLE")
        assert gen._planned_previous_row_rack_slots() == 0

    def test_validate_profile_capacity_limits_accepts_valid_network_rack(self) -> None:
        gen = build_rack_generator()
        gen.data["deployment"]["design"] = pod_design("S_MIXED")
        gen.data["rack_type"] = "network"
        gen.data["row_index"] = 1
        gen.data["index"] = 1
        gen.data["leafs"] = [
            {"role": "leaf", "quantity": 2, "template": {"id": "tmpl-leaf"}},
        ]

        errors = gen._validate_profile_capacity_limits()

        assert errors == []

    def test_validate_profile_capacity_limits_rejects_excess_tors_per_compute_rack(self) -> None:
        gen = build_rack_generator()
        gen.data["deployment"]["design"] = pod_design("S_MIXED")  # max_tors_per_compute_rack = 2
        gen.data["rack_type"] = "compute"
        gen.data["tors"] = [
            {"role": "tor", "quantity": 3, "template": {"id": "tmpl-tor"}},
        ]

        errors = gen._validate_profile_capacity_limits()

        assert any("max_tors_per_compute_rack" in err for err in errors)

    def test_validate_profile_capacity_limits_rejects_row_overflow(self) -> None:
        gen = build_rack_generator()
        gen.data["deployment"]["design"] = pod_design("S_MIXED")  # rows = 2
        gen.data["row_index"] = 3

        errors = gen._validate_profile_capacity_limits()

        assert any("exceeds profile rows" in err for err in errors)

    @pytest.mark.asyncio
    async def test_get_leaf_devices_in_row_success(self) -> None:
        gen = build_rack_generator()
        rack_obj = MagicMock(id="rack-a")
        leaf_dev = MagicMock()
        leaf_dev.name = MagicMock(value="leaf-01")
        leaf_if = MagicMock()
        leaf_if.name = MagicMock(value="Eth1/1")
        leaf_if.device.peer.name.value = "leaf-01"
        leaf_if.interface_type.value = "10gbase-x-sfpp"

        gen.client.filters = AsyncMock(side_effect=[[rack_obj], [leaf_dev], [leaf_if]])

        row = await gen._read_row_leafs("pod-1", 1)

        assert row == RowLeafs(["leaf-01"], [PortFacts("leaf-01", "Eth1/1", "10gbase-x-sfpp")])

    @pytest.mark.asyncio
    async def test_get_leaf_devices_in_row_takes_the_racks_from_the_pod_payload(self) -> None:
        """With the row's network rack in the pod projection the racks are not read at all,
        not even once per retry attempt: only the devices and their interfaces are."""
        gen = build_rack_generator()
        gen.data["deployment"]["racks"] = [
            {"id": "rack-net", "name": "NET", "index": 1, "row_index": 1, "rack_type": "network"},
            {"id": "rack-c1", "name": "C1", "index": 2, "row_index": 1, "rack_type": "compute"},
            {"id": "rack-other", "name": "NET-2", "index": 1, "row_index": 2, "rack_type": "network"},
        ]
        leaf_dev = MagicMock()
        leaf_dev.name = MagicMock(value="leaf-01")
        leaf_if = MagicMock()
        leaf_if.name = MagicMock(value="Eth1/1")
        leaf_if.device.peer.name.value = "leaf-01"
        leaf_if.interface_type.value = "10gbase-x-sfpp"
        gen.client.filters = AsyncMock(side_effect=[[leaf_dev], [leaf_if]])

        row = await gen._read_row_leafs("pod-1", 1)

        assert (row.names, [port.port_name for port in row.downlinks]) == (["leaf-01"], ["Eth1/1"])
        assert gen.client.filters.await_args_list[0].kwargs["rack__ids"] == ["rack-net", "rack-c1"]
        assert gen.client.filters.await_count == 2

    @pytest.mark.asyncio
    async def test_get_leaf_devices_in_row_rereads_racks_until_the_network_rack_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row's network rack missing from the payload (created in the same load, after the
        query ran) is the race the retry loop waits out: the racks are re-read per attempt."""
        gen = build_rack_generator()
        gen.data["deployment"]["racks"] = [
            {"id": "rack-c1", "name": "C1", "index": 2, "row_index": 1, "rack_type": "compute"}
        ]
        leaf_dev = MagicMock()
        leaf_dev.name = MagicMock(value="leaf-01")
        leaf_if = MagicMock()
        leaf_if.name = MagicMock(value="Eth1/1")
        leaf_if.device.peer.name.value = "leaf-01"
        leaf_if.interface_type.value = "10gbase-x-sfpp"
        gen.client.filters = AsyncMock(
            side_effect=[
                [MagicMock(id="rack-c1")],
                [],
                [MagicMock(id="rack-c1"), MagicMock(id="rack-net")],
                [leaf_dev],
                [leaf_if],
            ]
        )
        monkeypatch.setattr("generators.dc.rack.asyncio.sleep", AsyncMock())

        row = await gen._read_row_leafs("pod-1", 1)

        assert (row.names, [port.port_name for port in row.downlinks]) == (["leaf-01"], ["Eth1/1"])
        assert gen.client.filters.await_args_list[3].kwargs["rack__ids"] == ["rack-c1", "rack-net"]

    @pytest.mark.asyncio
    async def test_get_leaf_devices_in_row_no_racks_logs_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The mocked logger does not raise as the real one does, so only the first call is asserted. Nothing appears
        # across every retry attempt: the error fires after retries are exhausted.
        gen = build_rack_generator()
        gen.client.filters = AsyncMock(return_value=[])
        monkeypatch.setattr("generators.dc.rack.asyncio.sleep", AsyncMock())

        await gen._read_row_leafs("pod-1", 1)

        first_call_message = gen.logger.error.call_args_list[0].args[0]
        assert "no racks" in first_call_message.lower()

    @pytest.mark.asyncio
    async def test_get_leaf_devices_in_row_recovers_after_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nothing found on the first attempt, everything present on the second — no error."""
        gen = build_rack_generator()
        rack_obj = MagicMock(id="rack-a")
        leaf_dev = MagicMock()
        leaf_dev.name = MagicMock(value="leaf-01")
        leaf_if = MagicMock()
        leaf_if.name = MagicMock(value="Eth1/1")
        leaf_if.device.peer.name.value = "leaf-01"
        leaf_if.interface_type.value = "10gbase-x-sfpp"

        gen.client.filters = AsyncMock(side_effect=[[], [], [rack_obj], [leaf_dev], [leaf_if]])
        sleep_mock = AsyncMock()
        monkeypatch.setattr("generators.dc.rack.asyncio.sleep", sleep_mock)

        row = await gen._read_row_leafs("pod-1", 1)

        assert row == RowLeafs(["leaf-01"], [PortFacts("leaf-01", "Eth1/1", "10gbase-x-sfpp")])
        sleep_mock.assert_awaited_once()
        gen.logger.error.assert_not_called()

    def test_the_local_leaf_target_is_the_downlinks_of_the_leafs_of_this_plan(self) -> None:
        gen = build_rack_generator()
        gen._leaf_ports = {
            "leaf-01": [TemplatePort("Eth1/1", "downlink", "10gbase-x-sfpp"), TemplatePort("Eth1/2", "uplink", None)]
        }

        target = gen._local_leaf_target(rack_reads(), ["leaf-01"], 2, "l2-leaf")

        assert target is not None
        ports, leafs, offset, strategy = target
        assert [(port.device_name, port.port_name) for port in ports] == [("leaf-01", "Eth1/1")]
        assert (leafs, offset, strategy) == (["leaf-01"], 0, "intra_rack_middle")

    def test_leafs_without_a_downlink_are_a_problem_raised_before_any_write(self) -> None:
        gen = build_rack_generator()
        gen._leaf_ports = {"leaf-01": [TemplatePort("Eth1/2", "uplink", None)]}

        assert gen._local_leaf_target(rack_reads(), ["leaf-01"], 2, "l2-leaf") is None
        assert gen._problems == ["No downlink interfaces on leafs — cannot cable l2-leafs."]

    def test_a_compute_rack_cables_to_the_leafs_of_its_row_from_its_own_slot(self) -> None:
        gen = build_rack_generator()
        gen.data["index"] = 3
        row = RowLeafs(["leaf-a", "leaf-b"], [PortFacts("leaf-a", "Eth1/1", None)])

        target = gen._local_leaf_target(rack_reads(row_leafs=row), [], 2, "access-leaf")

        assert target is not None
        ports, leafs, offset, strategy = target
        assert (ports, leafs, offset, strategy) == (row.downlinks, ["leaf-a", "leaf-b"], 4, "intra_rack_mixed")

    def test_a_compute_rack_without_row_leafs_has_a_problem(self) -> None:
        gen = build_rack_generator()

        assert gen._local_leaf_target(rack_reads(), [], 2, "access-leaf") is None
        assert "No middle-rack leafs in row 1" in gen._problems[0]

    def test_plan_leafs_plans_the_devices_and_skips_a_duplicate_template(self) -> None:
        gen = build_rack_generator()
        gen.calculate_cabling_offsets = MagicMock(return_value=0)
        plan = TopologyPlan(root_id="rack-1")

        created: list[str] = []
        gen._plan_leafs(plan, rack_reads(), created)

        devices = [spec for spec in plan.of(NodeSpec) if spec.kind == "DcimPhysicalDevice"]
        assert created == [spec.data["name"] for spec in devices] and len(created) == 2
        # The pod's mlag_create and the rack reach the device; the pod's sorting reaches the pairing
        assert gen._mlag_groups == []
        assert {spec.data["rack"]["id"] for spec in devices} == {"rack-1"}
        (layer,) = gen._layers
        assert layer.bottom_devices == tuple(created) and layer.top_role == "spine"
        assert len(layer.links) == 4
        # The pod sorts the leaf ports top-down: the first leaf's first link leaves its Eth1/2
        assert layer.links[0].bottom.port_name == "Eth1/2"

        # second pass finds its names already created this run and plans nothing more
        before = len(plan.specs)
        gen._plan_leafs(plan, rack_reads(), created)
        assert len(plan.specs) == before

    # The "no spine devices" guard moved to _derive_spine_info(); see TestDeriveSpineInfo.

    def test_plan_tors_uses_planned_slots_without_live_sibling_query(self) -> None:
        gen = build_rack_generator()
        gen.data["row_index"] = 2
        gen.data["deployment"]["design"] = pod_design("S_MIXED")  # 8 compute + 1 network rack positions per row
        gen._spine_device_names = ["spine-01"]

        gen.client.filters = AsyncMock(side_effect=AssertionError("live sibling query should not be used"))
        gen.calculate_cabling_offsets = MagicMock(return_value=0)
        plan = TopologyPlan(root_id="rack-1")

        gen._plan_tors(plan, rack_reads())

        gen.calculate_cabling_offsets.assert_called_once_with(
            device_count=1,
            device_type="tor",
            racks_in_previous_rows=9,
        )
        assert gen._layers and gen._layers[0].bottom_role == "tor"

    # Border-leaf generation moved to the DC; see tests/unit/generators/test_dc_firewall_lb.py.
