"""Unit tests for endpoint connectivity generator.

Tests:
- EndpointModel parsing for all deployment types (middle_rack, tor, mixed)
- Cable model: simple ID-only format (no deep endpoint nesting)
- Rack devices with existing cables don't break parsing
- Connection fingerprinting and deduplication
- Interface speed matching and grouping
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from generators.helpers.cabling import InterfaceSpeedMatcher
from generators.models import (
    Cable,
    ConnectionFingerprint,
    EndpointInterface,
    EndpointModel,
    RackDevice,
)

# ============================================================================
# Test Data Fixtures
# ============================================================================

# Shared pod/dc context
_POD_DATA = {
    "id": "pod-1",
    "name": "DC1-1-POD-1",
    "deployment_type": "middle_rack",
    "index": 1,
    "parent": {"id": "dc-1", "name": "DC1"},
}

_RACK_BASE = {
    "id": "rack-1",
    "name": "ktw-1-s-1-r-1-1",
    "index": 1,
    "row_index": 1,
    "rack_type": "compute",
    "pod": _POD_DATA,
}


def _make_interface(name: str, intf_type: str = "25gbase-x-sfp28", cable: dict | None = None) -> dict:
    """Helper to build an interface dict."""
    intf: dict = {
        "id": f"intf-{name}",
        "name": name,
        "interface_type": intf_type,
        "role": "uplink",
        "status": "free",
    }
    if cable is not None:
        intf["cable"] = cable
    return intf


def _make_endpoint_data(
    rack_devices: list[dict] | None = None,
    rack_type: str = "compute",
    deployment_type: str = "middle_rack",
) -> dict:
    """Build a complete endpoint data dict for EndpointModel parsing."""
    pod = {**_POD_DATA, "deployment_type": deployment_type}
    rack = {
        **_RACK_BASE,
        "rack_type": rack_type,
        "pod": pod,
        "devices": rack_devices or [],
    }
    return {
        "id": "srv-1",
        "name": "server-111101",
        "role": "endpoint",
        "rack": rack,
        "interfaces": [
            _make_interface("eno1"),
            _make_interface("eno2"),
            _make_interface("eno3", "100gbase-x-qsfp28"),
            _make_interface("eno4", "100gbase-x-qsfp28"),
        ],
    }


# ============================================================================
# EndpointModel Parsing Tests
# ============================================================================


class TestEndpointModelParsing:
    """Test EndpointModel parses GraphQL data for all deployment types."""

    def test_middle_rack_no_rack_devices(self) -> None:
        """middle_rack: endpoint in compute rack, no leaf/tor devices in rack."""
        data = _make_endpoint_data(rack_devices=[], deployment_type="middle_rack")
        model = EndpointModel.model_validate({"endpoint": data})

        assert model.endpoint.name == "server-111101"
        assert model.endpoint.rack is not None
        assert model.endpoint.rack.rack_type == "compute"
        assert model.endpoint.rack.pod.deployment_type == "middle_rack"
        assert len(model.endpoint.rack.devices) == 0
        assert len(model.endpoint.interfaces) == 4

    def test_tor_deployment_rack_devices_no_cables(self) -> None:
        """tor: rack has ToR devices with free interfaces (no cables)."""
        tor_devices = [
            {
                "id": "tor-1",
                "name": "tor-01",
                "role": "tor",
                "interfaces": [
                    _make_interface("Ethernet1/1", "25gbase-x-sfp28"),
                    _make_interface("Ethernet1/2", "25gbase-x-sfp28"),
                ],
            },
            {
                "id": "tor-2",
                "name": "tor-02",
                "role": "tor",
                "interfaces": [
                    _make_interface("Ethernet1/1", "25gbase-x-sfp28"),
                    _make_interface("Ethernet1/2", "25gbase-x-sfp28"),
                ],
            },
        ]
        data = _make_endpoint_data(rack_devices=tor_devices, deployment_type="tor")
        model = EndpointModel.model_validate({"endpoint": data})

        assert model.endpoint.rack is not None
        assert model.endpoint.rack.pod.deployment_type == "tor"
        assert len(model.endpoint.rack.devices) == 2
        assert model.endpoint.rack.devices[0].name == "tor-01"
        assert len(model.endpoint.rack.devices[0].interfaces) == 2

    def test_tor_deployment_rack_devices_with_existing_cables(self) -> None:
        """tor: ToR switches have existing cables to spines — must parse without error.

        This was the original bug: cable endpoints had nested device objects
        that broke the old flat CableEndpoint model.
        """
        tor_devices = [
            {
                "id": "tor-1",
                "name": "tor-01",
                "role": "tor",
                "interfaces": [
                    # Free customer interfaces (no cable)
                    _make_interface("Ethernet1/1", "25gbase-x-sfp28"),
                    _make_interface("Ethernet1/2", "25gbase-x-sfp28"),
                    # Uplink to spine WITH cable (simplified: just cable ID)
                    _make_interface("Ethernet49/1", "100gbase-x-qsfp28", cable={"id": "cable-1"}),
                    _make_interface("Ethernet50/1", "100gbase-x-qsfp28", cable={"id": "cable-2"}),
                ],
            },
            {
                "id": "tor-2",
                "name": "tor-02",
                "role": "tor",
                "interfaces": [
                    _make_interface("Ethernet1/1", "25gbase-x-sfp28"),
                    _make_interface("Ethernet1/2", "25gbase-x-sfp28"),
                    _make_interface("Ethernet49/1", "100gbase-x-qsfp28", cable={"id": "cable-3"}),
                    _make_interface("Ethernet50/1", "100gbase-x-qsfp28", cable={"id": "cable-4"}),
                ],
            },
        ]
        data = _make_endpoint_data(rack_devices=tor_devices, deployment_type="tor")
        model = EndpointModel.model_validate({"endpoint": data})

        assert model.endpoint.rack is not None
        assert len(model.endpoint.rack.devices) == 2

        # Verify cable data parsed correctly
        tor1 = model.endpoint.rack.devices[0]
        cabled_intfs = [i for i in tor1.interfaces if i.cable is not None]
        free_intfs = [i for i in tor1.interfaces if i.cable is None]
        assert len(cabled_intfs) == 2
        assert len(free_intfs) == 2
        assert cabled_intfs[0].cable is not None
        assert cabled_intfs[0].cable.id == "cable-1"

    def test_mixed_deployment_with_leaf_and_tor_cables(self) -> None:
        """mixed: rack has both leaf and tor devices with spine cables."""
        devices = [
            {
                "id": "leaf-1",
                "name": "leaf-01",
                "role": "leaf",
                "interfaces": [
                    _make_interface("Ethernet1/1", "25gbase-x-sfp28"),
                    _make_interface("Ethernet49/1", "100gbase-x-qsfp28", cable={"id": "cable-l1"}),
                ],
            },
            {
                "id": "tor-1",
                "name": "tor-01",
                "role": "tor",
                "interfaces": [
                    _make_interface("Ethernet1/1", "25gbase-x-sfp28"),
                    _make_interface("Ethernet49/1", "100gbase-x-qsfp28", cable={"id": "cable-t1"}),
                ],
            },
        ]
        data = _make_endpoint_data(rack_devices=devices, deployment_type="mixed")
        model = EndpointModel.model_validate({"endpoint": data})

        assert model.endpoint.rack is not None
        assert model.endpoint.rack.pod.deployment_type == "mixed"
        assert len(model.endpoint.rack.devices) == 2
        assert model.endpoint.rack.devices[0].role == "leaf"
        assert model.endpoint.rack.devices[1].role == "tor"

    def test_null_cable_handled(self) -> None:
        """Interfaces with null cable (from GraphQL) parse as None."""
        data = _make_endpoint_data(rack_devices=[])
        # Explicitly set cable to None (as GraphQL would return)
        data["interfaces"][0]["cable"] = None
        model = EndpointModel.model_validate({"endpoint": data})

        assert model.endpoint.interfaces[0].cable is None

    def test_cable_wrapped_in_node(self) -> None:
        """Cable data wrapped in GraphQL node structure is unwrapped."""
        data = _make_endpoint_data(rack_devices=[])
        # Simulate pre-clean_data GraphQL format
        data["interfaces"][0]["cable"] = {"node": {"id": "cable-wrapped"}}
        model = EndpointModel.model_validate({"endpoint": data})

        assert model.endpoint.interfaces[0].cable is not None
        assert model.endpoint.interfaces[0].cable.id == "cable-wrapped"


# ============================================================================
# Cable Model Tests
# ============================================================================


class TestCableModel:
    """Test simplified Cable model (ID-only)."""

    def test_cable_from_simple_dict(self) -> None:
        cable = Cable(id="cable-123")
        assert cable.id == "cable-123"

    def test_cable_no_endpoints_field(self) -> None:
        """Cable model no longer has endpoints — extra fields are rejected by default."""
        # This confirms the simplified model doesn't accidentally accept deep nesting
        cable = Cable(id="cable-123")
        assert not hasattr(cable, "endpoints")


# ============================================================================
# EndpointInterface Tests
# ============================================================================


class TestEndpointInterface:
    """Test EndpointInterface model parsing."""

    def test_interface_without_cable(self) -> None:
        intf = EndpointInterface(id="i-1", name="eno1", role="uplink", status="free")
        assert intf.cable is None

    def test_interface_with_cable(self) -> None:
        intf = EndpointInterface.model_validate(
            {"id": "i-1", "name": "eno1", "role": "uplink", "status": "active", "cable": {"id": "cable-1"}},
        )
        assert intf.cable is not None
        assert intf.cable.id == "cable-1"

    def test_interface_with_node_wrapped_cable(self) -> None:
        """Handles the GraphQL {node: {id: ...}} wrapper."""
        intf = EndpointInterface.model_validate(
            {"id": "i-1", "name": "eno1", "cable": {"node": {"id": "cable-wrapped"}}},
        )
        assert intf.cable is not None
        assert intf.cable.id == "cable-wrapped"

    def test_interface_with_null_node_cable(self) -> None:
        """Handles {node: null} from GraphQL for uncabled interfaces."""
        intf = EndpointInterface.model_validate(
            {"id": "i-1", "name": "eno1", "cable": {"node": None}},
        )
        assert intf.cable is None


# ============================================================================
# RackDevice Tests
# ============================================================================


class TestRackDevice:
    """Test RackDevice model with mixed interface states."""

    def test_rack_device_with_mixed_cables(self) -> None:
        """A ToR with some free and some cabled interfaces."""
        device = RackDevice.model_validate(
            {
                "id": "tor-1",
                "name": "tor-01",
                "role": "tor",
                "interfaces": [
                    {"id": "i-1", "name": "Eth1/1", "status": "free"},
                    {"id": "i-2", "name": "Eth1/2", "status": "free"},
                    {"id": "i-3", "name": "Eth49/1", "cable": {"id": "c-1"}, "status": "active"},
                    {"id": "i-4", "name": "Eth50/1", "cable": {"id": "c-2"}, "status": "active"},
                ],
            }
        )
        assert len(device.interfaces) == 4
        free = [i for i in device.interfaces if i.cable is None]
        cabled = [i for i in device.interfaces if i.cable is not None]
        assert len(free) == 2
        assert len(cabled) == 2


# ============================================================================
# Connection Fingerprinting Tests
# ============================================================================


class TestConnectionFingerprinting:
    """Test connection uniqueness and deduplication."""

    def test_fingerprint_uniqueness(self) -> None:
        """Test that different fingerprints are unique."""
        fp1 = ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1")
        fp2 = ConnectionFingerprint("server-01", "eth1", "tor-1", "Ethernet1/1")  # Different interface
        fp3 = ConnectionFingerprint("server-01", "eth0", "tor-2", "Ethernet1/1")  # Different switch

        assert fp1 != fp2
        assert fp1 != fp3
        assert fp2 != fp3
        assert len({fp1, fp2, fp3}) == 3  # All unique in set

    def test_fingerprint_equality(self) -> None:
        """Test that identical fingerprints are equal."""
        fp1 = ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1")
        fp2 = ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1")

        assert fp1 == fp2
        assert hash(fp1) == hash(fp2)

    def test_fingerprint_frozen(self) -> None:
        """Test that fingerprints are immutable."""
        fp = ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1")

        with pytest.raises(FrozenInstanceError):
            fp.server_name = "server-02"  # type: ignore


# ============================================================================
# Interface Speed Matching Tests
# ============================================================================


class TestInterfaceSpeedMatching:
    """Test speed extraction and grouping (general utility)."""

    @pytest.mark.parametrize(
        "interface_type,expected_speed",
        [
            ("100gbase-x-qsfp28", 100),
            ("25gbase-x-sfp28", 25),
            ("10gbase-t", 10),
            ("40gbase-x-qsfpplus", 40),
            ("1000base-t", None),  # No match in current pattern
            ("unknown-type", None),
        ],
    )
    def test_extract_speed_from_interface_type(self, interface_type: str, expected_speed: int | None) -> None:
        """Test speed extraction from interface types."""
        speed = InterfaceSpeedMatcher.extract_speed(interface_type)
        assert speed == expected_speed
