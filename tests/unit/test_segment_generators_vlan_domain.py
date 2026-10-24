"""Unit tests for the VLAN domain resolution helpers of VxlanSegmentGenerator."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.logger import GeneratorError
from generators.protocols import ManagedStandaloneVlanDomain
from tests.unit.segment_generators_helpers import _make_gen

# ===========================================================================
# TestResolveVlanDomain / TestAssignSegmentToDcInterfaces
# ===========================================================================


class TestResolveVlanDomain:
    """Tests for _resolve_vlan_domain — MLAG-or-standalone domain resolution."""

    def test_device_with_mlag_capability_returns_mlag_domain(self):
        gen = _make_gen()
        device = MagicMock(id="dev-1")
        mlag_peer = MagicMock(id="mlag-1")
        mlag_peer.typename = "ManagedMLAG"
        device.capabilities = MagicMock(peers=[mlag_peer])

        result = asyncio.run(gen._resolve_vlan_domain(device))
        assert result == ("ManagedMLAG", "mlag-1")

    def test_device_without_mlag_capability_returns_standalone_domain(self):
        gen = _make_gen()
        device = MagicMock(id="dev-1")
        other_peer = MagicMock(id="bgp-1")
        other_peer.typename = "ManagedBGP"
        device.capabilities = MagicMock(peers=[other_peer])

        result = asyncio.run(gen._resolve_vlan_domain(device))
        assert result == ("DcimPhysicalDevice", "dev-1")

    def test_device_with_no_capabilities_attribute_returns_standalone_domain(self):
        gen = _make_gen()
        device = MagicMock(id="dev-1", spec=["id"])

        result = asyncio.run(gen._resolve_vlan_domain(device))
        assert result == ("DcimPhysicalDevice", "dev-1")


class TestResolveVlanDomains:
    """_resolve_vlan_domains: {device id: domain id}, the standalone domains found by ONE name read."""

    @staticmethod
    def _device(dev_id: str, name: str, mlag_id: str | None = None) -> MagicMock:
        """A device with capabilities fetched; MLAG-paired when ``mlag_id`` is given."""
        device = MagicMock(id=dev_id)
        device.name.value = name
        peers = []
        if mlag_id:
            peer = MagicMock(id=mlag_id)
            peer.typename = "ManagedMLAG"
            peers.append(peer)
        device.capabilities = MagicMock(peers=peers)
        return device

    @staticmethod
    def _domain(domain_id: str, name: str) -> MagicMock:
        """A ManagedStandaloneVlanDomain as filters() returns it."""
        domain = MagicMock(id=domain_id)
        domain.name.value = name
        domain.save = AsyncMock()
        return domain

    def test_standalone_domains_are_found_by_one_name_read_and_never_saved(self) -> None:
        """Two standalone devices and an MLAG member: one filters() on the names, MLAG needs no read."""
        gen = _make_gen()
        domains = [self._domain("dom-1", "sw-1-vlan-domain"), self._domain("dom-2", "sw-2-vlan-domain")]
        gen.client.filters = AsyncMock(return_value=domains)
        devices = [self._device("d1", "sw-1"), self._device("d2", "sw-2"), self._device("d3", "sw-3", "mlag-9")]

        result = asyncio.run(gen._resolve_vlan_domains(devices))

        assert result == {"d1": "dom-1", "d2": "dom-2", "d3": "mlag-9"}
        gen.client.filters.assert_awaited_once_with(
            kind=ManagedStandaloneVlanDomain, name__values=["sw-1-vlan-domain", "sw-2-vlan-domain"]
        )
        assert "include" not in gen.client.filters.call_args.kwargs
        for domain in domains:
            domain.save.assert_not_called()
        gen.client.create.assert_not_called()

    def test_only_mlag_devices_read_nothing(self) -> None:
        """MLAG domains come from the device's capabilities: no standalone-domain read."""
        gen = _make_gen()
        gen.client.filters = AsyncMock()

        result = asyncio.run(gen._resolve_vlan_domains([self._device("d1", "sw-1", "mlag-1")]))

        assert result == {"d1": "mlag-1"}
        gen.client.filters.assert_not_awaited()

    def test_missing_domain_is_an_error_naming_it(self) -> None:
        """The device generator owns the domain; a missing one fails the run instead of being created."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[self._domain("dom-1", "sw-1-vlan-domain")])

        with pytest.raises(GeneratorError, match="sw-2-vlan-domain"):
            asyncio.run(gen._resolve_vlan_domains([self._device("d1", "sw-1"), self._device("d2", "sw-2")]))

        gen.logger.error.assert_called_once()
        gen.client.create.assert_not_called()
