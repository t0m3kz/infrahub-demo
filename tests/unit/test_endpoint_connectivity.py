"""Unit tests for endpoint connectivity generator.

Tests:
- clean_data() normalization of raw endpoint/rack/interface dicts for all
  deployment types (middle_rack, tor, mixed)
- Cable dict: simple ID-only format (no deep endpoint nesting)
- Rack devices with existing cables don't break parsing
- Connection fingerprinting and deduplication
- Interface speed matching and grouping
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from generators.helpers.cabling import InterfaceSpeedMatcher
from generators.types import ConnectionFingerprint
from utils.data_cleaning import clean_data

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
    """Build a complete raw (pre-clean_data) endpoint data dict."""
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
# clean_data() Normalization Tests (endpoint shape)
# ============================================================================


class TestEndpointDataParsing:
    """Test clean_data() normalizes raw endpoint dicts for all deployment types."""

    def test_middle_rack_no_rack_devices(self) -> None:
        """middle_rack: endpoint in compute rack, no leaf/tor devices in rack."""
        data = clean_data(_make_endpoint_data(rack_devices=[], deployment_type="middle_rack"))

        assert data["name"] == "server-111101"
        assert data["rack"] is not None
        assert data["rack"]["rack_type"] == "compute"
        assert data["rack"]["pod"]["deployment_type"] == "middle_rack"
        assert len(data["rack"]["devices"]) == 0
        assert len(data["interfaces"]) == 4

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
        data = clean_data(_make_endpoint_data(rack_devices=tor_devices, deployment_type="tor"))

        assert data["rack"] is not None
        assert data["rack"]["pod"]["deployment_type"] == "tor"
        assert len(data["rack"]["devices"]) == 2
        assert data["rack"]["devices"][0]["name"] == "tor-01"
        assert len(data["rack"]["devices"][0]["interfaces"]) == 2

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
        data = clean_data(_make_endpoint_data(rack_devices=tor_devices, deployment_type="tor"))

        assert data["rack"] is not None
        assert len(data["rack"]["devices"]) == 2

        # Verify cable data parsed correctly
        tor1 = data["rack"]["devices"][0]
        cabled_intfs = [i for i in tor1["interfaces"] if i.get("cable") is not None]
        free_intfs = [i for i in tor1["interfaces"] if i.get("cable") is None]
        assert len(cabled_intfs) == 2
        assert len(free_intfs) == 2
        assert cabled_intfs[0]["cable"] == "cable-1"

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
        data = clean_data(_make_endpoint_data(rack_devices=devices, deployment_type="mixed"))

        assert data["rack"] is not None
        assert data["rack"]["pod"]["deployment_type"] == "mixed"
        assert len(data["rack"]["devices"]) == 2
        assert data["rack"]["devices"][0]["role"] == "leaf"
        assert data["rack"]["devices"][1]["role"] == "tor"

    def test_null_cable_handled(self) -> None:
        """Interfaces with null cable (from GraphQL) parse as None."""
        raw = _make_endpoint_data(rack_devices=[])
        # Explicitly set cable to None (as GraphQL would return)
        raw["interfaces"][0]["cable"] = None
        data = clean_data(raw)

        assert data["interfaces"][0]["cable"] is None

    def test_cable_wrapped_in_node(self) -> None:
        """Cable data wrapped in GraphQL node structure is unwrapped one level (node stripped,
        the inner {"id": ...} dict itself is not further flattened by this recursive call)."""
        raw = _make_endpoint_data(rack_devices=[])
        # Simulate pre-clean_data GraphQL format
        raw["interfaces"][0]["cable"] = {"node": {"id": "cable-wrapped"}}
        data = clean_data(raw)

        assert data["interfaces"][0]["cable"] == {"id": "cable-wrapped"}


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
