"""Unit tests for the VLAN domain helpers of VxlanSegmentGenerator."""

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


class TestCreateVlanDomainSegment:
    """_create_vlan_domain_segment — one new (segment, domain) pair, untracked."""

    def test_domain_without_pool_logs_error(self):
        gen = _make_gen()
        domain_obj = MagicMock()
        domain_obj.vlan_pool = None
        gen.client.get = AsyncMock(return_value=domain_obj)

        asyncio.run(gen._create_vlan_domain_segment("seg-1", "vxlan-1000", "mlag-1"))

        gen.client.create.assert_not_called()
        error_msg = gen.logger.error.call_args[0][0]
        assert "vlan_pool" in error_msg

    def test_allocates_vlan_id_from_domain_pool_untracked(self) -> None:
        """The pair is shared desired state of the segment: saved with
        update_group_context=False, so no run's cleanup can delete it."""
        gen = _make_gen()
        domain_obj = MagicMock()
        domain_obj.vlan_pool = MagicMock(id="pool-vlan-1")
        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=domain_obj)
        gen.client.create = AsyncMock(return_value=activation)

        asyncio.run(gen._create_vlan_domain_segment("seg-1", "vxlan-1000", "mlag-1"))

        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["data"]["segment"] == {"id": "seg-1"}
        assert call_kwargs["data"]["vlan_domain"] == {"id": "mlag-1"}
        assert call_kwargs["data"]["vlan_id"] == {"from_pool": {"id": "pool-vlan-1"}}
        activation.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_known_pool_skips_reading_the_domain(self) -> None:
        """A pool id handed over by the caller is used as is, without a domain read."""
        gen = _make_gen()
        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.get = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        asyncio.run(gen._create_vlan_domain_segment("seg-1", "vxlan-1000", "domain-1", "pool-1"))

        gen.client.get.assert_not_called()
        assert gen.client.create.call_args.kwargs["data"]["vlan_id"]["from_pool"]["id"] == "pool-1"


class TestEnsureStandaloneVlanDomain:
    """The standalone domain and its pool belong to the device's generator:
    looked up by name, never created or saved here."""

    @staticmethod
    def _device() -> MagicMock:
        device = MagicMock(id="dev-1")
        device.name.value = "bl-dc101101"
        return device

    def test_existing_domain_is_read_not_saved(self) -> None:
        """The domain is found by its device-derived name and returned with its pool."""
        gen = _make_gen()
        domain = MagicMock(id="domain-1")
        domain.vlan_pool = MagicMock(id="pool-1")
        domain.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=domain)

        result = asyncio.run(gen._ensure_standalone_vlan_domain(self._device()))

        assert result == ("domain-1", "pool-1")
        kwargs = gen.client.get.call_args.kwargs
        assert kwargs["kind"] == ManagedStandaloneVlanDomain
        assert kwargs["name__value"] == "bl-dc101101-vlan-domain"
        assert kwargs["include"] == ["vlan_pool"]
        domain.save.assert_not_called()
        gen.client.create.assert_not_called()

    def test_domain_without_pool_returns_none_pool(self) -> None:
        """A domain whose pool is not attached yet hands back no pool id."""
        gen = _make_gen()
        domain = MagicMock(id="domain-1")
        domain.vlan_pool = None
        gen.client.get = AsyncMock(return_value=domain)

        assert asyncio.run(gen._ensure_standalone_vlan_domain(self._device())) == ("domain-1", None)

    def test_missing_domain_is_an_error(self) -> None:
        """The device generator owns the domain; a missing one fails the run instead of being created."""
        gen = _make_gen()
        gen.client.get = AsyncMock(return_value=None)

        with pytest.raises(GeneratorError, match="bl-dc101101-vlan-domain"):
            asyncio.run(gen._ensure_standalone_vlan_domain(self._device()))

        gen.logger.error.assert_called_once()
        gen.client.create.assert_not_called()
