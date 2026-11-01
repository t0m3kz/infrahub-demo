"""The pure judges test_67 uses for a connection created from intent: the expected near port and the created carrier."""

from __future__ import annotations

from typing import Any

import pytest

from generators.helpers.near_port import NearPortError
from tests.integration.provider_connection_helpers import (
    carrier_state,
    created_connection_errors,
    expected_near_port,
    other_carrier_errors,
)


def _port(device: str, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"{device}:{name}",
        "name": name,
        "role": "cloud",
        "device": {"id": f"id-{device}", "name": device},
        "cable": None,
        "lag": None,
        "sub_interfaces": [],
        **extra,
    }


def _carrier(circuit_id: str, ends: list[tuple[str, str]], status: str = "active", **extra: Any) -> dict[str, Any]:
    return {
        "id": f"id-{circuit_id}",
        "circuit_id": circuit_id,
        "circuit_type": "virtual_connection",
        "status": status,
        "provider": "AWS",
        "connections": [],
        "locations": ["FR2", "eu-central-1"],
        "ends": sorted(ends),
        "end_ids": [f"{device}:{name}" for device, name in ends],
        "end_devices": [{"device": device, "device_id": f"id-{device}"} for device, _ in ends],
        **extra,
    }


def _inventory(*carriers: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {"physical": list(carriers), "virtual": []}


PORTS = [_port(d, f"Ethernet1/{n}") for d in ("eg-fr201", "eg-fr202") for n in (3, 4, 5)]


def test_the_first_free_port_of_the_first_device_is_chosen() -> None:
    """Both devices are equally loaded: the first by device name, then the first port by natural name."""
    chosen = expected_near_port(PORTS, _inventory(), {})
    assert (chosen.device_name, chosen.name) == ("eg-fr201", "Ethernet1/3")


def test_a_busy_port_is_skipped_and_the_least_loaded_device_goes_first() -> None:
    """A port that ends a live carrier is busy; a device that already serves a connection comes after the idle one."""
    ports = [_port("eg-fr201", "Ethernet1/3"), _port("eg-fr202", "Ethernet1/3"), _port("eg-fr202", "Ethernet1/10")]
    inventory = _inventory(_carrier("XC-1", [("eg-fr201", "Ethernet1/3"), ("EXT", "vnic-x")]))
    connections = {"PC-1": {"carriers": ["XC-1"]}}
    chosen = expected_near_port(ports, inventory, connections)
    assert (chosen.device_name, chosen.name) == ("eg-fr202", "Ethernet1/3")


def test_natural_order_puts_port_3_before_port_10() -> None:
    ports = [_port("eg-fr201", "Ethernet1/10"), _port("eg-fr201", "Ethernet1/3")]
    assert expected_near_port(ports, _inventory(), {}).name == "Ethernet1/3"


@pytest.mark.parametrize("busy", [{"cable": {"id": "c"}}, {"lag": {"id": "l"}}, {"sub_interfaces": [{"id": "s"}]}])
def test_cabled_lag_member_and_routed_ports_are_not_free(busy: dict[str, Any]) -> None:
    chosen = expected_near_port(
        [_port("eg-fr201", "Ethernet1/3", **busy), _port("eg-fr201", "Ethernet1/4")], _inventory(), {}
    )
    assert chosen.name == "Ethernet1/4"


def test_a_decommissioned_carrier_does_not_hold_its_port() -> None:
    inventory = _inventory(_carrier("XC-1", [("eg-fr201", "Ethernet1/3")], status="decommissioned"))
    assert expected_near_port([_port("eg-fr201", "Ethernet1/3")], inventory, {}).name == "Ethernet1/3"


def test_no_free_port_is_the_generators_error() -> None:
    inventory = _inventory(_carrier("XC-1", [("eg-fr201", "Ethernet1/3")]))
    with pytest.raises(NearPortError):
        expected_near_port([_port("eg-fr201", "Ethernet1/3")], inventory, {})


def _created(name: str = "PC-X") -> dict[str, Any]:
    return _carrier(
        f"{name}-XC",
        [("eg-fr201", "Ethernet1/3"), ("EXT-DX", f"vnic-{name}")],
        status="provisioning",
        connections=[name],
    )


ROW = {"carriers": ["PC-X-XC"], "account": {"provider": {"name": "AWS"}}}


def test_the_created_carrier_as_the_generator_makes_it_has_no_finding() -> None:
    errors = created_connection_errors(
        "PC-X",
        ROW,
        _inventory(_created()),
        near=("eg-fr201", "Ethernet1/3"),
        far_device="EXT-DX",
        carrier_type="virtual_connection",
    )
    assert errors == []


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (lambda c: c.update(status="active"), "status"),
        (lambda c: c.update(circuit_type="cross_connect"), "circuit_type"),
        (lambda c: c.update(provider="Lumen"), "provider"),
        (lambda c: c.update(ends=[("eg-fr201", "Ethernet1/4"), ("EXT-DX", "vnic-PC-X")]), "ends"),
        (lambda c: c.update(connections=[]), "provider_connections"),
    ],
)
def test_a_carrier_that_differs_is_named(change: Any, field: str) -> None:
    carrier = _created()
    change(carrier)
    errors = created_connection_errors(
        "PC-X",
        ROW,
        _inventory(carrier),
        near=("eg-fr201", "Ethernet1/3"),
        far_device="EXT-DX",
        carrier_type="virtual_connection",
    )
    assert any(field in error for error in errors), errors


def test_a_missing_or_duplicated_carrier_is_one_finding() -> None:
    kwargs: dict[str, Any] = {
        "near": ("eg-fr201", "Ethernet1/3"),
        "far_device": "EXT-DX",
        "carrier_type": "virtual_connection",
    }
    assert len(created_connection_errors("PC-X", ROW, _inventory(), **kwargs)) == 1
    assert len(created_connection_errors("PC-X", ROW, _inventory(_created(), _created()), **kwargs)) == 1
    assert created_connection_errors("PC-X", None, _inventory(), **kwargs) == ["PC-X: no such connection on the branch"]


def test_other_carriers_must_not_move() -> None:
    before = carrier_state(_inventory(_carrier("XC-1", [("a", "e1")])))
    after = _inventory(_carrier("XC-1", [("a", "e2")]), _created())
    errors = other_carrier_errors(before, after, {"PC-X-XC"})
    assert errors == ["XC-1: carrier changed"]
    assert other_carrier_errors(before, after, {"PC-X-XC", "XC-1"}) == []
