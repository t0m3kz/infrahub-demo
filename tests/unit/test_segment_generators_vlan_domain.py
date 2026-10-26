"""Unit tests for the VLAN domain resolution helpers of VxlanSegmentGenerator."""

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


class TestResolveVlanDomains:
    """_resolve_vlan_domains: {device id: domain id}, the MLAG pair or the device itself, without a read."""

    def test_device_with_mlag_capability_is_in_the_mlag_domain(self) -> None:
        assert _make_gen()._resolve_vlan_domains([_device("dev-1", "mlag-1")]) == {"dev-1": "mlag-1"}

    def test_device_without_mlag_capability_is_its_own_domain(self) -> None:
        assert _make_gen()._resolve_vlan_domains([_device("dev-1")]) == {"dev-1": "dev-1"}

    def test_device_with_no_capabilities_is_its_own_domain(self) -> None:
        assert _make_gen()._resolve_vlan_domains([_device("dev-1", with_capabilities=False)]) == {"dev-1": "dev-1"}

    def test_both_members_of_a_pair_share_the_domain_and_a_standalone_keeps_its_own(self) -> None:
        """Two members of one MLAG resolve to it (never to themselves); the third device is its own domain."""
        devices = [_device("d1", "mlag-9"), _device("d2", "mlag-9"), _device("d3")]

        assert _make_gen()._resolve_vlan_domains(devices) == {"d1": "mlag-9", "d2": "mlag-9", "d3": "d3"}

    def test_resolution_reads_and_writes_nothing(self) -> None:
        """A domain is never saved here: the device generator owns the MLAG, the device is the other domain."""
        gen = _make_gen()

        gen._resolve_vlan_domains([_device("d1"), _device("d2", "mlag-1")])

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()
