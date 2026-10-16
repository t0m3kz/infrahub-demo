"""Unit tests for utils/app_instances.py: the one place an AppInstance is resolved to its physical host.

Shared by the generator that tags a component's switch ports and by the NX-OS GPO transform that classifies them.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.topology.app_instance_segment import AppInstanceSegmentGenerator
from generators.topology.endpoint import HOSTED_INSTANCE_KINDS
from utils.app_instances import HOSTED_INSTANCE_TYPENAMES, PHYSICAL_INSTANCE_TYPENAMES, physical_host


class TestPhysicalHost:
    @pytest.mark.parametrize("typename", sorted(PHYSICAL_INSTANCE_TYPENAMES))
    def test_a_physical_instance_is_its_own_host(self, typename: str) -> None:
        instance = {"id": "dev-1", "typename": typename, "interfaces": []}
        assert physical_host(instance) is instance

    @pytest.mark.parametrize("typename", sorted(HOSTED_INSTANCE_TYPENAMES))
    def test_a_hosted_instance_is_its_hosting_device(self, typename: str) -> None:
        host = {"id": "host-1", "name": "srv-1"}
        assert physical_host({"id": "vm-1", "typename": typename, "hosting_device": host}) is host

    @pytest.mark.parametrize("hosting", [None, {}])
    @pytest.mark.parametrize("typename", sorted(HOSTED_INSTANCE_TYPENAMES))
    def test_a_hosted_instance_without_a_hosting_device_has_no_host(self, typename: str, hosting: Any) -> None:
        assert physical_host({"id": "vm-1", "typename": typename, "hosting_device": hosting}) is None

    @pytest.mark.parametrize("typename", ["CloudInstance", "Something", None])
    def test_any_other_kind_has_no_host(self, typename: str | None) -> None:
        assert physical_host({"id": "x", "typename": typename, "hosting_device": {"id": "h"}}) is None

    def test_the_hosted_kinds_are_the_generators_hosted_instance_kinds(self) -> None:
        """endpoint.py filters the hosted instances by these classes; the typenames here must not drift from them."""
        assert {kind.__name__ for kind in HOSTED_INSTANCE_KINDS} == HOSTED_INSTANCE_TYPENAMES

    def test_the_generator_resolves_the_same_host(self) -> None:
        """Its id of the host is the shared resolution's, for every kind and for the unresolvable ones."""
        instances = [
            {"id": "dev-1", "typename": "DcimPhysicalDevice"},
            {"id": "ctrl-1", "typename": "ManagedControllerPhysical"},
            {"id": "vm-1", "typename": "DcimVirtualDevice", "hosting_device": {"id": "host-1"}},
            {"id": "vm-2", "typename": "ManagedControllerVirtual", "hosting_device": {"id": "host-2"}},
            {"id": "vm-3", "typename": "DcimVirtualDevice", "hosting_device": None},
            {"id": "vm-4", "typename": "DcimVirtualDevice", "hosting_device": {}},
            {"id": "cloud-1", "typename": "CloudInstance"},
        ]
        for instance in instances:
            host = physical_host(instance)
            assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) == (host or {}).get("id")
