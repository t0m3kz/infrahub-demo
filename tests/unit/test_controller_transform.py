"""Unit tests for transforms/config/controller.py.

Covers:
  - ControllerPayload.transform() — dispatch to the right vendor builder by
    (controller_type, platform-name) pulled straight out of the query result.
  - No matching builder / no controller found → empty JSON object.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from transforms.config.controller import ControllerPayload


def _make_transform() -> ControllerPayload:
    return ControllerPayload.__new__(ControllerPayload)


def _raw_device(
    name: str,
    role: str = "leaf",
    status: str = "active",
    address: str | None = "10.0.0.1/32",
    branch_to_branch: str | None = None,
) -> dict:
    return {
        "node": {
            "__typename": "DcimPhysicalDevice",
            "id": f"id-{name}",
            "name": {"value": name},
            "role": {"value": role},
            "status": {"value": status},
            "primary_address": {"node": {"address": {"value": address}}} if address else {"node": None},
            "deployment": (
                {"node": {"branch_to_branch": {"value": branch_to_branch}}}
                if branch_to_branch is not None
                else {"node": None}
            ),
        }
    }


def _raw_controller(
    name: str,
    controller_type: str,
    platform_name: str | None = None,
    devices: list[dict] | None = None,
) -> dict[str, Any]:
    platform = {"node": {"id": "plat-1", "name": {"value": platform_name}}} if platform_name else {"node": None}
    return {
        "ManagedController": {
            "edges": [
                {
                    "node": {
                        "__typename": "ManagedControllerPhysical",
                        "id": f"id-{name}",
                        "name": {"value": name},
                        "controller_type": {"value": controller_type},
                        "platform": platform,
                        "managed_devices": {"edges": devices or []},
                    }
                }
            ]
        }
    }


class TestApicPayload:
    def test_builds_fabric_nodes_from_managed_devices(self) -> None:
        data = _raw_controller(
            "DC9-APIC1",
            "aci_apic",
            devices=[_raw_device("ss-dc9901", role="super-spine"), _raw_device("leaf-dc9901", role="leaf")],
        )
        result = asyncio.run(_make_transform().transform(data))
        payload = json.loads(result)

        assert payload["fabricName"] == "DC9-APIC1"
        assert [n["name"] for n in payload["nodes"]] == ["ss-dc9901", "leaf-dc9901"]
        assert payload["nodes"][0]["role"] == "spine"
        assert payload["nodes"][1]["role"] == "leaf"
        assert payload["nodes"][0]["oobMgmtAddr"] == "10.0.0.1"


class TestDcnmPayload:
    def test_builds_switch_inventory(self) -> None:
        data = _raw_controller("DC12-DCNM1", "dcnm", devices=[_raw_device("ss-dc121201", role="super-spine")])
        result = asyncio.run(_make_transform().transform(data))
        payload = json.loads(result)

        assert payload["fabric"] == "DC12-DCNM1"
        assert payload["switches"][0]["switchName"] == "ss-dc121201"
        assert payload["switches"][0]["ipAddress"] == "10.0.0.1"


class TestDnaCenterPayload:
    def test_builds_device_inventory(self) -> None:
        data = _raw_controller(
            "CAMPUS-DNAC1",
            "dna_center",
            devices=[
                _raw_device("acc-sw1", role="access-switch"),
                _raw_device("dist-sw1", role="distribution-switch"),
            ],
        )
        result = asyncio.run(_make_transform().transform(data))
        payload = json.loads(result)

        assert payload["siteName"] == "CAMPUS-DNAC1"
        assert [d["hostname"] for d in payload["devices"]] == ["acc-sw1", "dist-sw1"]
        assert payload["devices"][0]["role"] == "access-switch"
        assert payload["devices"][0]["managementIpAddress"] == "10.0.0.1"


class TestSecurityManagerVendorDispatch:
    def test_panos_platform_builds_panorama_payload(self) -> None:
        data = _raw_controller(
            "DC11-PANORAMA1", "security_manager", platform_name="panos", devices=[_raw_device("fw-dc1101")]
        )
        payload = json.loads(asyncio.run(_make_transform().transform(data)))

        assert "device-group" in payload
        assert payload["devices"][0]["hostname"] == "fw-dc1101"

    def test_junos_platform_builds_security_director_payload(self) -> None:
        data = _raw_controller("SD1", "security_manager", platform_name="junos", devices=[_raw_device("srx-1")])
        payload = json.loads(asyncio.run(_make_transform().transform(data)))

        assert "domain" in payload
        assert payload["devices"][0]["name"] == "srx-1"

    def test_checkpoint_gaia_platform_builds_sms_payload(self) -> None:
        data = _raw_controller(
            "DC12-CPSMS1", "security_manager", platform_name="checkpoint_gaia", devices=[_raw_device("fw-dc1201")]
        )
        payload = json.loads(asyncio.run(_make_transform().transform(data)))

        assert "sms" in payload
        assert payload["gateways"][0]["name"] == "fw-dc1201"


class TestLbManagerVendorDispatch:
    def test_f5_tmos_platform_builds_big_iq_payload(self) -> None:
        data = _raw_controller("DC12-BIGIQ1", "lb_manager", platform_name="f5_tmos", devices=[_raw_device("lb-dc1201")])
        payload = json.loads(asyncio.run(_make_transform().transform(data)))

        assert "deviceGroup" in payload
        assert payload["devices"][0]["hostname"] == "lb-dc1201"

    def test_netscaler_platform_builds_netscaler_adm_payload(self) -> None:
        data = _raw_controller(
            "DC11-NSADM1", "lb_manager", platform_name="netscaler", devices=[_raw_device("lb-dc1101")]
        )
        payload = json.loads(asyncio.run(_make_transform().transform(data)))

        assert "profile" in payload
        assert payload["instances"][0]["name"] == "lb-dc1101"


class TestVelocloudVcoPayload:
    def test_builds_enterprise_payload_from_managed_devices(self) -> None:
        data = _raw_controller(
            "EQX-FR2-SDWAN-VCO1",
            "sdwan_orchestrator",
            platform_name="velocloud",
            devices=[
                _raw_device("EQX-FR2-SDWAN-GW1", role="edge", address=None),
                _raw_device("C002-P-EDGE1", role="edge", address=None, branch_to_branch="full_mesh"),
            ],
        )
        payload = json.loads(asyncio.run(_make_transform().transform(data)))

        assert payload["enterprise"] == "EQX-FR2-SDWAN-VCO1"
        assert [e["hostname"] for e in payload["edges"]] == ["EQX-FR2-SDWAN-GW1", "C002-P-EDGE1"]

    def test_branch_to_branch_read_per_edge_from_its_own_office(self) -> None:
        """Different offices under the same VCO can carry different
        branch_to_branch policies — it's read per managed device's own
        deployment, not once for the whole controller."""
        data = _raw_controller(
            "VCO1",
            "sdwan_orchestrator",
            platform_name="velocloud",
            devices=[
                _raw_device("EDGE-HUB-ONLY", branch_to_branch="hub_only"),
                _raw_device("EDGE-FULL-MESH", branch_to_branch="full_mesh"),
            ],
        )
        payload = json.loads(asyncio.run(_make_transform().transform(data)))

        by_host = {e["hostname"]: e["branch-to-branch"] for e in payload["edges"]}
        assert by_host == {"EDGE-HUB-ONLY": "hub_only", "EDGE-FULL-MESH": "full_mesh"}

    def test_gateway_with_no_deployment_has_no_branch_to_branch(self) -> None:
        """The Gateway itself has no office/deployment carrying the field."""
        data = _raw_controller(
            "VCO1", "sdwan_orchestrator", platform_name="velocloud", devices=[_raw_device("GATEWAY1")]
        )
        payload = json.loads(asyncio.run(_make_transform().transform(data)))

        assert payload["edges"][0]["branch-to-branch"] is None


def _raw_iface(device: str, name: str, address: str, namespace: str) -> dict:
    return {
        "node": {
            "name": {"value": name},
            "ip_address": {
                "node": {"address": {"value": address}, "ip_namespace": {"node": {"name": {"value": namespace}}}}
            },
            "device": {"node": {"name": {"value": device}}},
        }
    }


def _raw_bgp_proc(device: str, asn: int) -> dict:
    return {
        "node": {
            "router_id": {"node": {"address": {"value": "10.255.5.1/31"}}},
            "local_as": {"node": {"asn": {"value": asn}}},
            "capabilities": {"edges": [{"node": {"name": {"value": device}}}]},
        }
    }


def _gateway_with_handoff(namespace: str = "PROD", local_iface: str = "eth0.1900") -> dict:
    """GW1 as data/demos/30_all/08_interconnects/04_sdwan/06_vrf_handoff.yml loads it."""
    device = _raw_device("EQX-FR2-SDWAN-GW1", role="edge", address=None)
    peering = {
        "node": {
            "id": "peering-1",
            "name": {"value": "VRF-PROD-EQXFR2-SDWAN"},
            "peering_role": {"value": "regular"},
            "session_type": {"value": "EBGP"},
            "ttl": {"value": 1},
            "address_families": {"edges": [{"node": {"afi": {"value": "ipv4"}, "safi": {"value": "unicast"}}}]},
            "interface_capabilities": {
                "edges": [
                    _raw_iface("eg-fr01", "Ethernet1/13.1900", "10.255.5.0/31", namespace),
                    _raw_iface("EQX-FR2-SDWAN-GW1", local_iface, "10.255.5.1/31", namespace),
                ]
            },
            "bgp_processes": {
                "edges": [_raw_bgp_proc("eg-fr01", 4200000101), _raw_bgp_proc("EQX-FR2-SDWAN-GW1", 65028)]
            },
        }
    }
    device["node"]["capabilities"] = {
        "edges": [
            {
                "node": {
                    "__typename": "ManagedBGP",
                    "name": {"value": "EQX-FR2-SDWAN-GW1-bgp"},
                    "status": {"value": "active"},
                    "local_as": {"node": {"asn": {"value": 65028}}},
                    "router_id": {"node": {"address": {"value": "10.255.5.1/31"}}},
                    "peerings": {"edges": [peering]},
                }
            }
        ]
    }
    return device


class TestVelocloudVrfHandoffs:
    """The Orchestrator pushes GW1's side of the PROD handoff to eg-fr01, so
    the VCO payload — not a device-config transform — has to carry it."""

    def _payload(self, device: dict) -> dict[str, Any]:
        data = _raw_controller("VCO1", "sdwan_orchestrator", platform_name="velocloud", devices=[device])
        return json.loads(asyncio.run(_make_transform().transform(data)))

    def test_gateway_vrf_session_becomes_segment_handoff(self) -> None:
        """Both ends come from the same BGP helper as eg-fr01's config."""
        handoffs = self._payload(_gateway_with_handoff())["edges"][0]["handoffs"]

        assert handoffs == [
            {
                "segment": "PROD",
                "interface": "eth0.1900",
                "vlan": 1900,
                "local-address": "10.255.5.1/31",
                "bgp": {
                    "local-asn": 65028,
                    "neighbor-ip": "10.255.5.0",
                    "neighbor-asn": 4200000101,
                    "neighbor": "eg-fr01",
                },
            }
        ]

    def test_default_namespace_session_is_not_a_handoff(self) -> None:
        """Only sessions in a tenant namespace are VRF handoffs."""
        assert self._payload(_gateway_with_handoff(namespace="default"))["edges"][0]["handoffs"] == []

    def test_untagged_interface_has_no_vlan(self) -> None:
        """No dot1q suffix on the interface name means no VLAN to hand off."""
        handoff = self._payload(_gateway_with_handoff(local_iface="eth1"))["edges"][0]["handoffs"][0]

        assert handoff["vlan"] is None

    def test_device_without_capabilities_has_no_handoffs(self) -> None:
        """An office Edge with no BGP capability gets an empty list."""
        assert self._payload(_raw_device("C005-P-EDGE1"))["edges"][0]["handoffs"] == []


class TestNoMatch:
    def test_unknown_platform_for_security_manager_returns_empty(self) -> None:
        data = _raw_controller("SMS-X", "security_manager", platform_name="unknown_os")
        result = asyncio.run(_make_transform().transform(data))

        assert json.loads(result) == {}

    def test_no_controller_in_result_returns_empty(self) -> None:
        result = asyncio.run(_make_transform().transform({"ManagedController": {"edges": []}}))

        assert json.loads(result) == {}
