"""The one reader of ``ManagedInterfaceEncapsulation`` (utils/interface_encapsulation.py) on cleaned query data."""

from __future__ import annotations

from tests.unit.encapsulation_helpers import cleaned_encapsulation
from utils.interface_encapsulation import (
    ENCAPSULATION_KEY,
    encapsulation_name,
    encapsulation_of,
    parent_of,
    sub_interfaces_of,
    vlan_of,
)


def test_an_interface_that_rides_on_a_port_reports_its_parent_and_tag() -> None:
    """parent_of and vlan_of read the encapsulation among the interface's capabilities."""
    iface = {"name": "eth1.3001", **cleaned_encapsulation(3001, {"name": "eth1", "device": {"id": "dev"}})}

    assert (parent_of(iface) or {})["name"] == "eth1"
    assert vlan_of(iface) == 3001
    assert encapsulation_of(iface) is not None


def test_an_interface_that_rides_on_nothing_reports_none() -> None:
    """An SVI, a loopback or a tunnel has no encapsulation: no parent, no tag."""
    iface = {"name": "Vlan100"}

    assert encapsulation_of(iface) is None
    assert parent_of(iface) is None
    assert vlan_of(iface) is None


def test_a_non_encapsulation_capability_is_ignored() -> None:
    """Only the ManagedInterfaceEncapsulation kind counts, whatever else the key lists."""
    iface = {ENCAPSULATION_KEY: [{"typename": "ManagedVRF", "id": "vrf"}]}

    assert encapsulation_of(iface) is None


def test_an_untagged_encapsulation_has_a_parent_and_no_tag() -> None:
    """A VM NIC pinned to a host port has no VLAN."""
    iface = cleaned_encapsulation(None, {"name": "eno1"})

    assert vlan_of(iface) is None
    assert (parent_of(iface) or {})["name"] == "eno1"


def test_a_parentless_encapsulation_reports_no_parent() -> None:
    """A null parent (a peering whose parent the query did not return) is no parent."""
    assert parent_of(cleaned_encapsulation(100, None)) is None


def test_the_sub_interfaces_of_a_port_are_the_capabilities_of_its_encapsulations() -> None:
    """The reverse edge: encapsulations -> capabilities."""
    port = {"encapsulations": [{"capabilities": [{"name": "a"}]}, {"capabilities": [{"name": "b"}, {"name": "c"}]}]}

    assert [sub["name"] for sub in sub_interfaces_of(port)] == ["a", "b", "c"]
    assert list(sub_interfaces_of({})) == []


def test_the_encapsulation_name_is_device_and_interface() -> None:
    """Deterministic, so a data file or a generator upserts the object."""
    assert encapsulation_name("fw-1", "ethernet1/1.3001") == "fw-1:ethernet1/1.3001"
