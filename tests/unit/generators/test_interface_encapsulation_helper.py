"""The one reader of what an interface rides on (utils/interface_encapsulation.py) on cleaned query data."""

from __future__ import annotations

from tests.unit.encapsulation_helpers import cleaned_encapsulation
from utils.interface_encapsulation import parent_of, sub_interfaces_of, vlan_of


def test_an_interface_that_rides_on_a_port_reports_its_parent_and_tag() -> None:
    """parent_of and vlan_of read the interface's own fields."""
    iface = {"name": "eth1.3001", **cleaned_encapsulation(3001, {"name": "eth1", "device": {"id": "dev"}})}

    assert (parent_of(iface) or {})["name"] == "eth1"
    assert vlan_of(iface) == 3001


def test_an_interface_that_rides_on_nothing_reports_none() -> None:
    """An SVI, a loopback or a tunnel has no parent: no parent, no tag."""
    iface = {"name": "Vlan100"}

    assert parent_of(iface) is None
    assert vlan_of(iface) is None


def test_an_untagged_sub_interface_has_a_parent_and_no_tag() -> None:
    """A VM NIC pinned to a host port has no VLAN."""
    iface = cleaned_encapsulation(None, {"name": "eno1"})

    assert vlan_of(iface) is None
    assert (parent_of(iface) or {})["name"] == "eno1"


def test_a_parentless_sub_interface_reports_no_parent() -> None:
    """A null parent is no parent."""
    assert parent_of(cleaned_encapsulation(100, None)) is None


def test_the_sub_interfaces_of_a_port_are_its_sub_interfaces() -> None:
    """The reverse edge: sub_interfaces."""
    port = {"sub_interfaces": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}

    assert [sub["name"] for sub in sub_interfaces_of(port)] == ["a", "b", "c"]
    assert list(sub_interfaces_of({})) == []
