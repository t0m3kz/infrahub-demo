"""Unit tests for AppInstanceSegmentGenerator (generators/topology/app_instance_segment.py)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.app_instance_segment import AppInstanceSegmentGenerator

# Harness helpers


def _make_gen() -> Any:
    gen = AppInstanceSegmentGenerator.__new__(AppInstanceSegmentGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _component_response(
    *,
    fqdn: str = "web.c001.demo.local",
    segment: dict[str, Any] | None = None,
    instances: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "AppComponent": [
            {
                "id": "comp-1",
                "fqdn": fqdn,
                "network_segment": segment,
                "instances": instances or [],
            }
        ]
    }


_VXLAN_SEGMENT: dict[str, Any] = {
    "id": "seg-1",
    "name": "seg",
    "typename": "ManagedVxlanSegment",
    "environment": "p",
    "security_zone": {"id": "zone-prod", "name": "PROD", "vrf_type": "prod", "l3_vni": 60000},
}


def _iface(name: str, *, role: str = "uplink", cabled: bool = True, iface_id: str | None = None) -> MagicMock:
    intf = MagicMock()
    intf.id = iface_id or f"id-{name}"
    intf.name = MagicMock(value=name)
    intf.role = MagicMock(value=role)
    if cabled:
        intf.cable = MagicMock(id=f"cable-{name}")
    else:
        intf.cable = None
    return intf


# generate() — guard clauses and dispatch


def _gen_with_lock() -> Any:
    """A generator whose resource_lock records the keys it was taken for."""
    gen = _make_gen()
    gen.locked_keys = []

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        gen.locked_keys.append(key)
        yield

    gen.resource_lock = _lock
    return gen


class TestGenerate:
    """generate() reconciles the component's SEGMENT under the segment lock: the VLAN rows first."""

    @staticmethod
    def _ready(*, added: int = 0, removed: int = 0, rows_changed: bool = False) -> Any:
        gen = _gen_with_lock()
        gen._fetch_segment_vlan_state = AsyncMock(
            return_value={"segment": {"id": "seg-1", "ports": []}, "deployments": []}
        )
        gen._derived_segment_port_ids = AsyncMock(return_value=set())
        gen._port_devices = AsyncMock(return_value={})
        gen._reconcile_segment_port_rows = AsyncMock(return_value=(added, removed))
        gen._reconcile_segment_vlans_locked = AsyncMock(return_value=rows_changed)
        gen.ensure_segment_addressing = AsyncMock()
        # The VRF pass over the domains' devices has its own tests (test_vrf_callers.py).
        gen.reconcile_vrfs = AsyncMock()
        gen._domain_member_device_ids = AsyncMock(return_value=[])
        return gen

    def test_no_component_data_logs_error_and_returns(self) -> None:
        gen = _make_gen()
        asyncio.run(gen.generate({"AppComponent": []}))
        gen.logger.error.assert_called_once()
        gen.client.get.assert_not_awaited()

    def test_no_network_segment_logs_info_and_returns(self) -> None:
        gen = _make_gen()
        data = _component_response(segment=None, instances=[{"id": "dev-1", "typename": "DcimPhysicalDevice"}])
        asyncio.run(gen.generate(data))
        gen.client.get.assert_not_awaited()
        gen.logger.error.assert_not_called()

    def test_no_instances_still_reconciles_the_segment(self) -> None:
        """A component whose instances were all removed still untags their old ports."""
        gen = self._ready()
        data = _component_response(segment=_VXLAN_SEGMENT, instances=[])

        asyncio.run(gen.generate(data))

        gen._reconcile_segment_port_rows.assert_awaited_once()
        gen._reconcile_segment_vlans_locked.assert_awaited_once()
        gen.logger.error.assert_not_called()

    def test_missing_vxlan_segment_is_skipped_without_error(self) -> None:
        """A VXLAN segment deleted since the trigger fired: its state is gone, nothing to reconcile, no error."""
        gen = self._ready()
        gen._fetch_segment_vlan_state = AsyncMock(return_value=None)
        data = _component_response(
            segment=_VXLAN_SEGMENT, instances=[{"id": "dev-1", "typename": "DcimPhysicalDevice"}]
        )

        asyncio.run(gen.generate(data))

        gen._reconcile_segment_port_rows.assert_not_awaited()
        gen._reconcile_segment_vlans_locked.assert_not_awaited()
        gen.client.get.assert_not_awaited()
        gen.logger.error.assert_not_called()

    def test_cloud_segment_is_skipped_without_error(self) -> None:
        """A cloud segment has no on-prem port: no lookup, no lock, no reconcile."""
        gen = self._ready()
        data = _component_response(
            segment={"id": "seg-1", "name": "c003-vaultex-subnet-prod-p", "typename": "CloudNetworkSegment"},
            instances=[{"id": "cloud-1", "typename": "CloudInstance"}],
        )

        asyncio.run(gen.generate(data))

        gen.client.get.assert_not_awaited()
        gen._reconcile_segment_port_rows.assert_not_awaited()
        gen.logger.error.assert_not_called()
        assert gen.locked_keys == []

    def test_reconciles_rows_then_tags_under_the_segment_lock(self) -> None:
        """Rows and tags are reconciled for the segment, holding its lock: rows first, then the tags on them."""
        gen = self._ready(added=1, rows_changed=True)
        order: list[str] = []
        gen._reconcile_segment_vlans_locked.side_effect = lambda *_a: order.append("rows") or True
        gen._reconcile_segment_port_rows.side_effect = lambda **_k: order.append("tags") or (1, 0)
        data = _component_response(
            segment={**_VXLAN_SEGMENT, "name": "c001-web-p"},
            instances=[{"id": "dev-1", "typename": "DcimPhysicalDevice"}],
        )

        asyncio.run(gen.generate(data))

        assert gen.locked_keys == ["segment-vlan-domains-seg-1"]
        assert order == ["rows", "tags"]
        assert gen._derived_segment_port_ids.call_args.args[:2] == ("seg-1", "c001-web-p")
        # Rows changed: the state is re-read before the tags are reconciled against the rows.
        assert gen._fetch_segment_vlan_state.await_count == 2
        assert gen._reconcile_segment_vlans_locked.call_args.args[:2] == ("seg-1", "c001-web-p")
        assert gen._reconcile_segment_port_rows.call_args.kwargs["ports"] == []

    def test_unchanged_rows_reuse_the_state(self) -> None:
        """No row change: one state read serves both reconciliations."""
        gen = self._ready()
        data = _component_response(
            segment=_VXLAN_SEGMENT, instances=[{"id": "dev-1", "typename": "DcimPhysicalDevice"}]
        )

        asyncio.run(gen.generate(data))

        gen._fetch_segment_vlan_state.assert_awaited_once()

    def test_generate_saves_nothing_into_the_run_group(self) -> None:
        """No create/save from this run: tags and activations are untracked shared state."""
        gen = self._ready(added=1, removed=1)
        data = _component_response(
            segment=_VXLAN_SEGMENT, instances=[{"id": "dev-1", "typename": "DcimPhysicalDevice"}]
        )

        asyncio.run(gen.generate(data))

        gen.client.create.assert_not_called()
        gen.client.get.assert_not_called()


# _resolve_physical_device_id


class TestResolvePhysicalDeviceId:
    def test_physical_device_resolves_to_itself(self) -> None:
        instance = {"id": "dev-1", "typename": "DcimPhysicalDevice"}
        assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) == "dev-1"

    def test_physical_controller_resolves_to_itself(self) -> None:
        instance = {"id": "ctrl-1", "typename": "ManagedControllerPhysical"}
        assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) == "ctrl-1"

    def test_virtual_device_resolves_to_hosting_device(self) -> None:
        instance = {"id": "vm-1", "typename": "DcimVirtualDevice", "hosting_device": {"id": "host-1"}}
        assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) == "host-1"

    def test_virtual_controller_resolves_to_hosting_device(self) -> None:
        instance = {"id": "ctrl-vm-1", "typename": "ManagedControllerVirtual", "hosting_device": {"id": "host-2"}}
        assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) == "host-2"

    def test_virtual_device_without_hosting_device_returns_none(self) -> None:
        instance = {"id": "vm-1", "typename": "DcimVirtualDevice", "hosting_device": None}
        assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) is None

    def test_cloud_instance_returns_none(self) -> None:
        instance = {"id": "cloud-1", "typename": "CloudInstance"}
        assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) is None

    def test_unknown_typename_returns_none(self) -> None:
        assert AppInstanceSegmentGenerator._resolve_physical_device_id({"id": "x", "typename": "Something"}) is None


# _derived_segment_ports / _port_devices / _reconcile_segment_port_rows


def _components(*instance_lists: list[dict[str, Any]]) -> dict[str, Any]:
    """A raw segment_components.gql response, one component per instance list."""
    edges = []
    for index, instances in enumerate(instance_lists):
        inst_edges = []
        for inst in instances:
            node: dict[str, Any] = {"id": inst["id"], "__typename": inst["typename"]}
            if "host" in inst:
                node["hosting_device"] = {"node": {"id": inst["host"]}}
            inst_edges.append({"node": node})
        edges.append({"node": {"id": f"comp-{index}", "instances": {"edges": inst_edges}}})
    return {"AppComponent": {"edges": edges}}


class TestDerivedSegmentPorts:
    """The derived ports = the union of every component's instances' targets plus the terminated service ports."""

    def test_union_across_components(self) -> None:
        """Ports of every component's instances, deduped across shared hosts."""
        gen = _make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value=_components(
                [{"id": "dev-1", "typename": "DcimPhysicalDevice"}],
                [
                    {"id": "vm-1", "typename": "DcimVirtualDevice", "host": "dev-1"},
                    {"id": "dev-2", "typename": "DcimPhysicalDevice"},
                ],
            )
        )
        gen._customer_facing_target_ids = AsyncMock(return_value={"if-a", "po-b"})

        assert asyncio.run(gen._derived_segment_port_ids("seg-1", "seg", {})) == {"if-a", "po-b"}
        # One resolution for every host of the segment (a VM resolves to its host, deduped).
        gen._customer_facing_target_ids.assert_awaited_once_with(["dev-1", "dev-2"])
        assert gen.client.execute_graphql.call_args.kwargs["variables"] == {"segment_id": "seg-1"}

    def test_cloud_instances_contribute_nothing(self) -> None:
        """An unresolvable instance has no port."""
        gen = _make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value=_components([{"id": "cloud-1", "typename": "CloudInstance"}])
        )
        gen._customer_facing_target_ids = AsyncMock(return_value=set())

        assert asyncio.run(gen._derived_segment_port_ids("seg-1", "seg", {})) == set()
        gen._customer_facing_target_ids.assert_awaited_once_with([])


class TestPortDevices:
    def test_no_port_reads_nothing(self) -> None:
        """No derived port: no query."""
        gen = _make_gen()
        gen.client.execute_graphql = AsyncMock()

        assert asyncio.run(gen._port_devices(set())) == {}
        gen.client.execute_graphql.assert_not_awaited()

    def test_one_read_maps_each_port_to_its_device(self) -> None:
        """One query for every port; a port the server does not return has no device."""
        gen = _make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value={"DcimInterface": {"edges": [{"node": {"id": "if-a", "device": {"node": {"id": "sw-1"}}}}]}}
        )

        assert asyncio.run(gen._port_devices({"if-a", "if-gone"})) == {"if-a": "sw-1", "if-gone": None}
        gen.client.execute_graphql.assert_awaited_once()
        assert gen.client.execute_graphql.call_args.kwargs["variables"] == {"ids": ["if-a", "if-gone"]}


class TestReconcileSegmentPortRows:
    """Desired edges = each desired port joined to its device's row; every port edge of the segment is the reconcile's."""

    @staticmethod
    def _gen() -> tuple[Any, list[dict[str, Any]]]:
        """A generator whose mutations are recorded in the returned list."""
        gen = _make_gen()
        mutations: list[dict[str, Any]] = []

        async def _execute(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
            if variables and "data" in variables:
                mutations.append(variables["data"] | {"mutation": query.split("{")[1].split("(")[0].strip()})
                return {}
            raise AssertionError(f"unexpected query {query}")

        gen.client.execute_graphql = AsyncMock(side_effect=_execute)
        gen.client.get = AsyncMock()
        return gen, mutations

    @staticmethod
    def _reconcile(
        gen: Any,
        port_devices: dict[str, str | None],
        current: dict[str, str],
        rows: dict[str, tuple[str, str]] | None = None,
        requested: dict[str, str] | None = None,
    ) -> tuple[int, int]:
        """Reconcile with ``current`` port edges ``{port id: row id}`` and the rows ``{device id: (row id."""
        ports = [{"id": port_id, "device_id": "sw-1", "row_id": row_id} for port_id, row_id in current.items()]
        return asyncio.run(
            gen._reconcile_segment_port_rows(
                segment_name="seg",
                ports=ports,
                port_devices=port_devices,
                requested=requested or {},
                rows_by_device=rows if rows is not None else {"sw-1": ("row-1", "sd-1"), "sw-2": ("row-2", "sd-1")},
            )
        )

    def test_missing_ports_are_joined_to_their_switch_row(self) -> None:
        """Each desired port gets an edge to its own device's row."""
        gen, mutations = self._gen()

        assert self._reconcile(gen, {"if-a": "sw-1", "po-b": "sw-2"}, {}) == (2, 0)
        assert [(m["mutation"], m["id"], m["name"], m["nodes"]) for m in mutations] == [
            ("RelationshipAdd", "if-a", "capabilities", [{"id": "row-1"}]),
            ("RelationshipAdd", "po-b", "capabilities", [{"id": "row-2"}]),
        ]
        gen.client.get.assert_not_awaited()

    def test_port_no_instance_lands_on_is_untagged(self) -> None:
        """An instance that moved leaves its old port: the edge is removed, the new one added."""
        gen, mutations = self._gen()

        assert self._reconcile(gen, {"if-new": "sw-1"}, {"if-old": "row-1"}) == (1, 1)
        assert [(m["mutation"], m["id"]) for m in mutations] == [
            ("RelationshipAdd", "if-new"),
            ("RelationshipRemove", "if-old"),
        ]

    def test_a_port_already_tagged_is_left_alone(self) -> None:
        """A port still desired keeps its edge: nothing written."""
        gen, mutations = self._gen()

        assert self._reconcile(gen, {"if-shared": "sw-1"}, {"if-shared": "row-1"}) == (0, 0)
        assert mutations == []

    def test_a_requested_port_is_joined_to_its_switch_row(self) -> None:
        """A port only a deployment requests joins its switch's row when the row is under that deployment."""
        gen, mutations = self._gen()

        assert self._reconcile(gen, {"if-r": "sw-1"}, {}, requested={"if-r": "sd-1"}) == (1, 0)
        assert [(m["mutation"], m["id"], m["nodes"]) for m in mutations] == [
            ("RelationshipAdd", "if-r", [{"id": "row-1"}])
        ]

    def test_a_withdrawn_request_has_its_edge_removed(self) -> None:
        """The port is no longer requested (nor derived): its edge goes."""
        gen, mutations = self._gen()

        assert self._reconcile(gen, {}, {"if-r": "row-1"}) == (0, 1)
        assert [(m["mutation"], m["id"], m["nodes"]) for m in mutations] == [
            ("RelationshipRemove", "if-r", [{"id": "row-1"}])
        ]

    def test_a_requested_port_whose_row_is_under_another_deployment_is_skipped(self) -> None:
        """The switch's row is under another deployment than the one requesting the port: a WARNING, no edge."""
        gen, mutations = self._gen()

        assert self._reconcile(gen, {"if-r": "sw-1"}, {}, requested={"if-r": "sd-other"}) == (0, 0)
        assert mutations == []
        gen.logger.warning.assert_called_once()
        assert "if-r" in gen.logger.warning.call_args.args[0]

    def test_a_port_whose_device_has_no_row_is_reported_and_skipped(self) -> None:
        """No row of the segment on the port's device (no deployment there), or no device: a WARNING each, no edge."""
        gen, mutations = self._gen()

        assert self._reconcile(gen, {"if-x": "sw-9", "if-y": None}, {}) == (0, 0)
        assert mutations == []
        warnings = " ".join(str(call.args[0]) for call in gen.logger.warning.call_args_list)
        assert "if-x" in warnings and "if-y" in warnings


class TestCustomerFacingTargets:
    @staticmethod
    def _far_end(far_id: str, *, role: str = "customer", port_channel: str | None = None) -> MagicMock:
        far_end = MagicMock(id=far_id)
        far_end.role = MagicMock(value=role)
        far_end.lag = MagicMock(peer=MagicMock(id=port_channel) if port_channel else None)
        return far_end

    @staticmethod
    def _cable_of(nic: MagicMock, far_id: str) -> MagicMock:
        """The DcimCable the nic's cable id points at, ending on `far_id`."""
        cable = MagicMock(id=nic.cable.id)
        cable.endpoints.peers = [MagicMock(id=nic.id), MagicMock(id=far_id)]
        return cable

    def test_no_devices_reads_nothing(self) -> None:
        """No device (every instance unresolvable) must not query: an empty device__ids filter matches every NIC."""
        gen = _make_gen()
        gen.client.filters = AsyncMock()

        assert asyncio.run(gen._customer_facing_target_ids([])) == set()

        gen.client.filters.assert_not_awaited()

    def test_no_cabled_nics_returns_empty(self) -> None:
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[_iface("eth0", cabled=False)])

        assert asyncio.run(gen._customer_facing_target_ids(["dev-1"])) == set()
        assert gen.client.filters.await_count == 1  # no cable, so no far-end lookup

    def test_uplink_far_end_is_a_target(self) -> None:
        gen = _make_gen()
        nic = _iface("eth0")
        gen.client.filters = AsyncMock(side_effect=[[nic], [self._cable_of(nic, "far-1")], [self._far_end("far-1")]])

        assert asyncio.run(gen._customer_facing_target_ids(["dev-1"])) == {"far-1"}

    def test_bonded_members_dedup_to_one_port_channel(self) -> None:
        """Two bond members resolving to the SAME port-channel give one target."""
        gen = _make_gen()
        eth0, eth1 = _iface("eth0", role="lag"), _iface("eth1", role="lag")
        gen.client.filters = AsyncMock(
            side_effect=[
                [eth0, eth1],
                [self._cable_of(eth0, "far-0"), self._cable_of(eth1, "far-1")],
                [
                    self._far_end("far-0", role="lag", port_channel="po-1"),
                    self._far_end("far-1", role="lag", port_channel="po-1"),
                ],
            ]
        )

        assert asyncio.run(gen._customer_facing_target_ids(["dev-1"])) == {"po-1"}

    def test_every_device_is_resolved_in_three_queries(self) -> None:
        """Three devices, five cabled NICs: one NIC query, one cable query, one far-end query."""
        gen = _make_gen()
        nics = [_iface(f"eth{n}") for n in range(5)]
        gen.client.filters = AsyncMock(
            side_effect=[
                nics,
                [self._cable_of(nic, f"far-{nic.name.value}") for nic in nics],
                [self._far_end(f"far-{nic.name.value}") for nic in nics],
            ]
        )

        targets = asyncio.run(gen._customer_facing_target_ids(["dev-1", "dev-2", "dev-3"]))

        assert targets == {f"far-eth{n}" for n in range(5)}
        assert gen.client.filters.await_count == 3
        assert gen.client.filters.await_args_list[0].kwargs["device__ids"] == ["dev-1", "dev-2", "dev-3"]
        gen.client.get.assert_not_called()

    def test_unresolvable_far_end_yields_nothing(self) -> None:
        gen = _make_gen()
        nic = _iface("eth0")
        cable = self._cable_of(nic, "far-1")
        cable.endpoints.peers = [MagicMock(id=nic.id)]  # only the near end
        gen.client.filters = AsyncMock(side_effect=[[nic], [cable]])

        assert asyncio.run(gen._customer_facing_target_ids(["dev-1"])) == set()


# _customer_facing_target_id


class TestCustomerFacingTargetId:
    def test_plain_uplink_far_end_returned_as_is(self) -> None:
        gen = _make_gen()
        far_end = MagicMock(id="far-1")
        far_end.role = MagicMock(value="customer")

        assert gen._customer_facing_target_id(far_end) == "far-1"

    def test_lag_far_end_resolves_to_its_port_channel(self) -> None:
        gen = _make_gen()
        far_end = MagicMock(id="far-1")
        far_end.role = MagicMock(value="lag")
        far_end.lag = MagicMock(peer=MagicMock(id="po-1"))

        assert gen._customer_facing_target_id(far_end) == "po-1"

    def test_lag_far_end_without_port_channel_warns_and_returns_none(self) -> None:
        gen = _make_gen()
        far_end = MagicMock(id="far-1")
        far_end.role = MagicMock(value="lag")
        far_end.lag = MagicMock(peer=None)

        assert gen._customer_facing_target_id(far_end) is None
        gen.logger.warning.assert_called_once()
