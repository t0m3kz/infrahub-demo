"""utils/vlan_domain.vlan_domain_id: the VLAN domain of a device is its MLAG when paired, else the device itself.

A local VLAN ID is allocated per VLAN DOMAIN, not DC-wide. An MLAG pair is one domain: both members must resolve to the
ManagedMLAG id, or each half picks a different local VLAN for the same segment and the port-channel between them
carries mismatched tags. The generator (generators/vlan_domain.py) and the transform (transforms/common.py) both call it.
"""

from __future__ import annotations

from utils.vlan_domain import vlan_domain_id


def test_mlag_capability_wins_over_the_device_id() -> None:
    assert vlan_domain_id("dev-leaf-01", [("ManagedMLAG", "mlag-domain-1")]) == "mlag-domain-1"


def test_both_members_of_a_pair_resolve_to_the_same_domain() -> None:
    caps = [("ManagedMLAG", "mlag-domain-1")]

    assert vlan_domain_id("dev-leaf-01", caps) == vlan_domain_id("dev-leaf-02", caps)


def test_standalone_device_is_its_own_domain() -> None:
    assert vlan_domain_id("dev-leaf-01", [("ManagedBGP", "bgp-1")]) == "dev-leaf-01"


def test_mlag_is_chosen_whatever_its_position() -> None:
    """A member of an MLAG pair is never a domain itself: the MLAG is chosen first."""
    assert vlan_domain_id("dev-leaf-01", [("ManagedBGP", "bgp-1"), ("ManagedMLAG", "mlag-1")]) == "mlag-1"


def test_no_capabilities_falls_back_to_the_device_id() -> None:
    assert vlan_domain_id("dev-leaf-01", []) == "dev-leaf-01"


def test_mlag_capability_without_an_id_is_ignored() -> None:
    """An MLAG the query did not select an id for would otherwise resolve the domain to None and drop every segment."""
    assert vlan_domain_id("dev-leaf-01", [("ManagedMLAG", None)]) == "dev-leaf-01"


def test_accepts_a_generator_of_pairs() -> None:
    """The callers pass a generator expression over SDK peers or query dicts."""
    peers = ({"typename": t, "id": i} for t, i in [("ManagedMLAG", "mlag-7")])

    assert vlan_domain_id("dev", ((p["typename"], p["id"]) for p in peers)) == "mlag-7"
