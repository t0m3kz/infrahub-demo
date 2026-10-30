"""Unit tests for the MLAG lookup of a device that VlanDomainMixin pairs rows by."""

from __future__ import annotations

from unittest.mock import MagicMock

from tests.unit.segment_generators_helpers import _make_gen


def _device(dev_id: str, mlag_id: str | None = None, *, with_capabilities: bool = True) -> MagicMock:
    """A device; MLAG-paired when ``mlag_id`` is given, with no capabilities fetched when asked."""
    if not with_capabilities:
        return MagicMock(id=dev_id, spec=["id"])
    device = MagicMock(id=dev_id)
    peers = []
    other = MagicMock(id="bgp-1")
    other.typename = "ManagedBGP"
    peers.append(other)
    if mlag_id:
        peer = MagicMock(id=mlag_id)
        peer.typename = "ManagedMLAG"
        peers.append(peer)
    device.capabilities = MagicMock(peers=peers)
    return device


class TestMlagOf:
    """_mlag_of: the id of the MLAG a device is in, without a read."""

    def test_device_with_mlag_capability_is_in_the_mlag(self) -> None:
        assert _make_gen()._mlag_of(_device("dev-1", "mlag-1")) == "mlag-1"

    def test_device_without_mlag_capability_has_none(self) -> None:
        assert _make_gen()._mlag_of(_device("dev-1")) is None

    def test_device_with_no_capabilities_has_none(self) -> None:
        assert _make_gen()._mlag_of(_device("dev-1", with_capabilities=False)) is None

    def test_both_members_of_a_pair_resolve_to_the_same_mlag(self) -> None:
        gen = _make_gen()

        assert gen._mlag_of(_device("d1", "mlag-9")) == gen._mlag_of(_device("d2", "mlag-9")) == "mlag-9"

    def test_lookup_reads_and_writes_nothing(self) -> None:
        gen = _make_gen()

        gen._mlag_of(_device("d2", "mlag-1"))

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()
