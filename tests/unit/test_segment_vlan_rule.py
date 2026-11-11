"""utils/segment_vlan.mlag_id: the MLAG a device is a member of, from its (typename, id) capabilities.

The generator (generators/segment_vlan.py) and the check (checks/mlag_domains.py) both call it; two peers of one MLAG get
the same answer, which is how a pair is found.
"""

from __future__ import annotations

import pytest

from utils.segment_vlan import mlag_id


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        pytest.param([("ManagedMLAG", "mlag-domain-1")], "mlag-domain-1", id="in-an-mlag"),
        pytest.param([("ManagedBGP", "bgp-1"), ("ManagedMLAG", "mlag-1")], "mlag-1", id="among-other-capabilities"),
        pytest.param([("ManagedBGP", "bgp-1")], None, id="other-capabilities-ignored"),
        pytest.param([], None, id="no-capabilities"),
        pytest.param([("ManagedMLAG", None)], None, id="mlag-without-id-ignored"),
    ],
)
def test_mlag_id(caps: list[tuple[str, str | None]], expected: str | None) -> None:
    """The MLAG id, in either capability order: both peers of a pair get the same answer."""
    assert mlag_id(caps) == mlag_id(reversed(caps)) == expected


def test_a_generator_of_pairs_works() -> None:
    peers = [{"typename": "ManagedMLAG", "id": "mlag-7"}]

    assert mlag_id((p["typename"], p["id"]) for p in peers) == "mlag-7"
