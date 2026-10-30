"""utils/vlan_domain.mlag_id: the MLAG a device is a member of, from its (typename, id) capabilities.

The generator (generators/vlan_domain.py) and the check (checks/mlag_domains.py) both call it; two peers of one MLAG get
the same answer, which is how a pair is found.
"""

from __future__ import annotations

from utils.vlan_domain import mlag_id


def test_a_device_in_an_mlag_gets_its_id() -> None:
    assert mlag_id([("ManagedMLAG", "mlag-domain-1")]) == "mlag-domain-1"


def test_both_peers_of_a_pair_get_the_same_id() -> None:
    caps = [("ManagedBGP", "bgp-1"), ("ManagedMLAG", "mlag-1")]

    assert mlag_id(caps) == mlag_id(reversed(caps)) == "mlag-1"


def test_other_capabilities_are_ignored() -> None:
    assert mlag_id([("ManagedBGP", "bgp-1")]) is None


def test_no_capabilities_has_none() -> None:
    assert mlag_id([]) is None


def test_an_mlag_without_an_id_is_ignored() -> None:
    assert mlag_id([("ManagedMLAG", None)]) is None


def test_a_generator_of_pairs_works() -> None:
    peers = [{"typename": "ManagedMLAG", "id": "mlag-7"}]

    assert mlag_id((p["typename"], p["id"]) for p in peers) == "mlag-7"
