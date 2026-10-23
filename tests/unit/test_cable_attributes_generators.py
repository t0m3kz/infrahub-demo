"""Cable ID, colour and length at every place a generator creates a DcimCable.

The three sites (CablingMixin's cabling, the HA sync cable, the MLAG peer-link cables) merge
``cable_attributes`` into the create data and read the devices' racks through ONE method,
``CablingMixin._cable_layouts``: three batched reads (devices, racks, suites) however many cables there are.
Cables declared in data files have no generator and are not touched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.common import CommonGenerator
from generators.connections import CablingMixin
from generators.devices import DeviceMixin
from generators.helpers.cable_attributes import cable_id
from generators.protocols import (
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    LocationRack,
    LocationSuite,
)

_SUITE = "suite-1"


class _CablingGen(CablingMixin, CommonGenerator):
    """The cabling mixin on its own."""


class _DeviceGen(DeviceMixin, CablingMixin, CommonGenerator):
    """The HA and MLAG sites, which reach _cable_layouts through the host they are mixed into."""


def _rel(peer_id: str | None) -> MagicMock:
    """A loaded relationship to ``peer_id`` (unset when None)."""
    return MagicMock(id=peer_id, initialized=peer_id is not None)


def _device(device_id: str, rack_id: str | None) -> MagicMock:
    """A DcimPhysicalDevice as filters(include=["rack"]) returns it."""
    return MagicMock(id=device_id, rack=_rel(rack_id))


def _rack(rack_id: str, row: int, index: int, suite_id: str | None = _SUITE) -> MagicMock:
    """A LocationRack as filters(include=["parent"]) returns it."""
    rack = MagicMock(id=rack_id, row_index=MagicMock(value=row), index=MagicMock(value=index))
    rack.parent = _rel(suite_id)  # assigned: `parent=` is a Mock constructor argument, not an attribute
    return rack


def _suite(suite_id: str = _SUITE) -> MagicMock:
    """A 4 x 9 suite with the schema's default pitches."""
    suite = MagicMock(id=suite_id)
    suite.rows.value, suite.racks_per_row.value = 4, 9
    suite.rack_pitch_cm.value, suite.row_pitch_cm.value = 60, 300
    return suite


class _Estate:
    """The racks, devices and suites a fake client answers the layout reads from, and the reads it counts."""

    def __init__(
        self,
        devices: dict[str, str | None],
        racks: dict[str, tuple[int, int]] | None = None,
        *,
        drop_suites: bool = False,
    ) -> None:
        self.devices = devices
        self.racks = racks or {}
        self.drop_suites = drop_suites
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def filters(self, *, kind: Any, **kwargs: Any) -> list[Any]:
        """Answer the device, rack and suite reads; every other kind is empty."""
        self.calls.append((kind, kwargs))
        if kind is DcimPhysicalDevice:
            return [_device(i, self.devices[i]) for i in kwargs["ids"] if i in self.devices]
        if kind is LocationRack:
            return [_rack(i, *self.racks[i]) for i in kwargs["ids"]]
        if kind is LocationSuite:
            return [] if self.drop_suites else [_suite(i) for i in kwargs["ids"]]
        return []

    def layout_reads(self) -> int:
        """How many reads the layout lookup made."""
        return sum(kind in (DcimPhysicalDevice, LocationRack, LocationSuite) for kind, _ in self.calls)


def _interface(name: str, device: str, device_id: str, interface_type: str = "100gbase-x-qsfp28") -> MagicMock:
    """A cabled-to-be interface of device ``device`` (display label) / ``device_id``."""
    intf = MagicMock()
    intf.id = f"id-{device}-{name}"
    intf.name = MagicMock(value=name)
    intf.device = MagicMock(id=device_id, display_label=device)
    intf.interface_type = MagicMock(value=interface_type)
    intf.cable = None
    intf.description = MagicMock()
    intf.status = MagicMock()
    intf.save = AsyncMock()
    return intf


def _cabling_gen(estate: _Estate, interfaces: dict[str, list[Any]]) -> Any:
    """A cabling host whose client answers interface reads (by device names) and the estate's layout reads."""
    gen: Any = _CablingGen.__new__(_CablingGen)
    gen.logger = MagicMock()
    gen.deployment_id = "dc-1"
    gen._resolve_pool = AsyncMock(return_value=None)
    gen.client = MagicMock()

    async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
        if kind is DcimPhysicalInterface:
            return interfaces[kwargs["device__name__values"][0]]
        return await estate.filters(kind=kind, **kwargs)

    gen.client.filters = AsyncMock(side_effect=_filters)
    gen.client.create = AsyncMock(side_effect=lambda **_: MagicMock(save=AsyncMock()))
    return gen


def _cable_data(gen: Any) -> list[dict[str, Any]]:
    """The data of every DcimCable create call, in order."""
    return [c.kwargs["data"] for c in gen.client.create.call_args_list if c.kwargs["kind"] is DcimCable]


class TestCableLayouts:
    """CablingMixin._cable_layouts: the one lookup, batched."""

    @pytest.mark.asyncio
    async def test_positions_come_from_the_device_rack_and_suite(self) -> None:
        """A device in a rack gets that rack's coordinates and its suite's grid and pitches."""
        estate = _Estate({"d1": "r1", "d2": "r2"}, {"r1": (1, 2), "r2": (3, 7)})
        gen = _cabling_gen(estate, {})

        layouts = await gen._cable_layouts(["d1", "d2"])

        first, second = layouts["d1"], layouts["d2"]
        assert (first.suite_id, first.row_index, first.index) == (_SUITE, 1, 2)
        assert (second.row_index, second.index) == (3, 7)
        assert (first.rows, first.racks_per_row, first.rack_pitch_cm, first.row_pitch_cm) == (4, 9, 60, 300)

    @pytest.mark.asyncio
    async def test_a_device_without_a_rack_or_a_virtual_one_has_no_position(self) -> None:
        """No rack, and a device the physical-device read does not return (a virtual one): None."""
        gen = _cabling_gen(_Estate({"d1": None, "d2": "r2"}, {"r2": (1, 1)}), {})

        layouts = await gen._cable_layouts(["d1", "d2", "virtual-3"])

        assert layouts["d1"] is None and layouts["virtual-3"] is None
        assert layouts["d2"] is not None

    @pytest.mark.asyncio
    async def test_a_rack_whose_suite_is_not_found_has_no_position(self) -> None:
        """A rack with no suite to take the grid from is not guessed at."""
        gen = _cabling_gen(_Estate({"d1": "r1"}, {"r1": (1, 1)}, drop_suites=True), {})

        assert (await gen._cable_layouts(["d1"]))["d1"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [2, 40, 400])
    async def test_the_number_of_reads_does_not_grow_with_the_devices(self, count: int) -> None:
        """Devices -> racks -> suites: three reads for 2 devices and for 400."""
        devices: dict[str, str | None] = {f"d{i}": f"r{i % 7}" for i in range(count)}
        racks = {f"r{i}": (1 + i % 4, 1 + i) for i in range(7)}
        estate = _Estate(devices, racks)
        gen = _cabling_gen(estate, {})

        await gen._cable_layouts(list(devices))

        assert estate.layout_reads() == 3

    @pytest.mark.asyncio
    async def test_devices_without_racks_cost_one_read(self) -> None:
        """No device has a rack: the rack and suite reads are skipped."""
        estate = _Estate({"d1": None, "d2": None})
        gen = _cabling_gen(estate, {})

        assert await gen._cable_layouts(["d1", "d2"]) == {"d1": None, "d2": None}
        assert estate.layout_reads() == 1

    @pytest.mark.asyncio
    async def test_no_devices_cost_nothing(self) -> None:
        """An empty device list reads nothing."""
        estate = _Estate({})
        gen = _cabling_gen(estate, {})

        assert await gen._cable_layouts([]) == {}
        assert estate.calls == []


class TestCablingSite:
    """CablingMixin._execute_cabling_plan: ID, colour and length on every cable, from three reads in all."""

    @staticmethod
    def _pairs(count: int, *, racked: bool) -> tuple[_Estate, dict[str, list[Any]]]:
        """``count`` leaf/spine device pairs, all in one rack when ``racked`` (else with no rack)."""
        devices: dict[str, str | None] = {}
        interfaces: dict[str, list[Any]] = {"leaf": [], "spine": []}
        for i in range(count):
            devices[f"leaf-id-{i}"] = "r1" if racked else None
            devices[f"spine-id-{i}"] = "r1" if racked else None
            interfaces["leaf"].append(_interface("Ethernet1/49", f"leaf-{i}", f"leaf-id-{i}"))
            interfaces["spine"].append(_interface("Ethernet1/1", f"spine-{i}", f"spine-id-{i}"))
        return _Estate(devices, {"r1": (2, 3)}), interfaces

    async def _run(self, count: int, *, racked: bool) -> tuple[Any, _Estate]:
        """create_cabling over ``count`` index-paired cables."""
        estate, interfaces = self._pairs(count, racked=racked)
        gen = _cabling_gen(estate, interfaces)
        plan = list(zip(interfaces["leaf"], interfaces["spine"]))
        with patch("generators.connections.CablingPlanner.build_cabling_plan", return_value=plan):
            await gen.create_cabling(["leaf"], ["Ethernet1/49"], ["spine"], ["Ethernet1/1"])
        return gen, estate

    @pytest.mark.asyncio
    async def test_cables_in_one_rack_carry_id_colour_and_length(self) -> None:
        """mmf -> aqua, the ID of the two endpoint labels, 100 cm inside one rack."""
        gen, _ = await self._run(2, racked=True)

        first = _cable_data(gen)[0]
        assert first["name"] == "leaf-0-Ethernet1/49__spine-0-Ethernet1/1"
        assert first["cable_id"] == cable_id(["leaf-0-Ethernet1/49", "spine-0-Ethernet1/1"])
        assert (first["color"], first["length_cm"]) == ("aqua", 100)

    @pytest.mark.asyncio
    async def test_cables_between_devices_without_racks_have_no_length_key(self) -> None:
        """No rack: no length_cm key at all (not None), the ID and the colour are still there."""
        gen, _ = await self._run(2, racked=False)

        for data in _cable_data(gen):
            assert "length_cm" not in data
            assert data["color"] == "aqua" and data["cable_id"].startswith("CBL-")

    @pytest.mark.asyncio
    async def test_copper_ends_get_the_blue_jacket(self) -> None:
        """Copper cables follow CableTypeDetector: both ends base-t -> blue."""
        estate = _Estate({"a": None, "b": None})
        interfaces = {
            "leaf": [_interface("Ethernet1/1", "tor-1", "a", "1000base-t")],
            "spine": [_interface("eth0", "srv-1", "b", "1000base-t")],
        }
        gen = _cabling_gen(estate, interfaces)
        plan = list(zip(interfaces["leaf"], interfaces["spine"]))
        with patch("generators.connections.CablingPlanner.build_cabling_plan", return_value=plan):
            await gen.create_cabling(["leaf"], ["Ethernet1/1"], ["spine"], ["eth0"])

        data = _cable_data(gen)[0]
        assert (data["type"], data["color"]) == ("copper", "blue")

    @pytest.mark.asyncio
    async def test_a_rerun_creates_identical_cable_data(self) -> None:
        """Same layout, same data on the second run: the attributes are part of the idempotent upsert."""
        first, _ = await self._run(3, racked=True)
        second, _ = await self._run(3, racked=True)

        assert _cable_data(first) == _cable_data(second)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("racked", [True, False])
    async def test_the_reads_do_not_grow_with_the_cables(self, racked: bool) -> None:
        """2 cables and 60 cables make the same number of layout reads (three with racks, one without)."""
        _, small = await self._run(2, racked=racked)
        _, large = await self._run(60, racked=racked)

        assert small.layout_reads() == large.layout_reads() == (3 if racked else 1)


class TestHaSyncSite:
    """DeviceMixin._ensure_ha_cable: the sync cable carries the attributes too."""

    @staticmethod
    def _gen(estate: _Estate) -> Any:
        gen: Any = _DeviceGen.__new__(_DeviceGen)
        gen.logger = MagicMock()
        gen.deployment_id = "dc-1"
        gen.client = MagicMock()
        gen.client.filters = AsyncMock(side_effect=estate.filters)
        gen.client.create = AsyncMock(side_effect=lambda **_: MagicMock(save=AsyncMock()))
        return gen

    @staticmethod
    def _sync(device_id: str, name: str) -> tuple[Any, Any]:
        device = MagicMock(id=device_id)
        device.name = MagicMock(value=name)
        iface = _interface("eth7", name, device_id)
        return device, iface

    @pytest.mark.asyncio
    async def test_the_sync_cable_gets_id_yellow_and_a_length(self) -> None:
        """smf -> yellow; both firewalls in one rack -> 100 cm."""
        gen = self._gen(_Estate({"d1": "r1", "d2": "r1"}, {"r1": (1, 1)}))

        await gen._ensure_ha_cable("fw-ha", [self._sync("d2", "fw-02"), self._sync("d1", "fw-01")])

        data = _cable_data(gen)[0]
        assert data["name"] == "CBL-fw-ha-SYNC"
        assert data["cable_id"] == cable_id(["fw-01-eth7", "fw-02-eth7"])
        assert (data["color"], data["length_cm"]) == ("yellow", 100)

    @pytest.mark.asyncio
    async def test_firewalls_without_racks_get_no_length_and_a_rerun_is_identical(self) -> None:
        """Devices with no rack: no length_cm key; two runs write the same data; one layout read."""
        runs = []
        for _ in range(2):
            estate = _Estate({"d1": None, "d2": None})
            gen = self._gen(estate)
            await gen._ensure_ha_cable("fw-ha", [self._sync("d1", "fw-01"), self._sync("d2", "fw-02")])
            runs.append(_cable_data(gen)[0])
            assert estate.layout_reads() == 1

        assert "length_cm" not in runs[0]
        assert runs[0] == runs[1]


class TestMlagPeerLinkSite:
    """MLAGWiringMixin._ensure_peer_link_cables: every peer-link cable carries the attributes."""

    @staticmethod
    def _device(device_id: str, name: str) -> MagicMock:
        device = MagicMock(id=device_id, deployment=None)
        device.name = MagicMock(value=name)
        return device

    @pytest.mark.asyncio
    async def test_peer_link_cables_get_id_yellow_and_the_intra_rack_length(self) -> None:
        """Two peer-link cables between ToRs of one rack; one batched layout lookup for the pair."""
        estate = _Estate({"d1": "r1", "d2": "r1"}, {"r1": (1, 1)})
        gen = TestHaSyncSite._gen(estate)
        ports_a = [_interface(f"Ethernet1/{n}", "tor-01", "d1") for n in (1, 2)]
        ports_b = [_interface(f"Ethernet1/{n}", "tor-02", "d2") for n in (1, 2)]

        await gen._ensure_peer_link_cables(
            "tor-01-tor-02-mlag", self._device("d1", "tor-01"), self._device("d2", "tor-02"), ports_a, ports_b
        )

        cables = _cable_data(gen)
        assert [c["name"] for c in cables] == ["CBL-tor-01-tor-02-mlag-PL1", "CBL-tor-01-tor-02-mlag-PL2"]
        assert len({c["cable_id"] for c in cables}) == 2
        assert cables[0]["cable_id"] == cable_id(["tor-01-Ethernet1/1", "tor-02-Ethernet1/1"])
        assert all(c["color"] == "yellow" and c["length_cm"] == 100 for c in cables)
        assert estate.layout_reads() == 3

    @pytest.mark.asyncio
    async def test_peer_link_cables_without_racks_have_no_length_and_a_rerun_is_identical(self) -> None:
        """No rack: no length_cm; the same data on a second run."""
        runs = []
        for _ in range(2):
            estate = _Estate({"d1": None, "d2": None})
            gen = TestHaSyncSite._gen(estate)
            ports_a = [_interface("Ethernet1/1", "tor-01", "d1")]
            ports_b = [_interface("Ethernet1/1", "tor-02", "d2")]
            await gen._ensure_peer_link_cables(
                "tor-01-tor-02-mlag", self._device("d1", "tor-01"), self._device("d2", "tor-02"), ports_a, ports_b
            )
            runs.append(_cable_data(gen))

        assert all("length_cm" not in c for c in runs[0])
        assert runs[0] == runs[1]
