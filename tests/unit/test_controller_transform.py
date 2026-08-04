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


def _raw_device(name: str, role: str = "leaf", status: str = "active", address: str | None = "10.0.0.1/32") -> dict:
    return {
        "node": {
            "__typename": "DcimPhysicalDevice",
            "id": f"id-{name}",
            "name": {"value": name},
            "role": {"value": role},
            "status": {"value": status},
            "primary_address": {"node": {"address": {"value": address}}} if address else {"node": None},
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


class TestNoMatch:
    def test_unknown_platform_for_security_manager_returns_empty(self) -> None:
        data = _raw_controller("SMS-X", "security_manager", platform_name="unknown_os")
        result = asyncio.run(_make_transform().transform(data))

        assert json.loads(result) == {}

    def test_no_controller_in_result_returns_empty(self) -> None:
        result = asyncio.run(_make_transform().transform({"ManagedController": {"edges": []}}))

        assert json.loads(result) == {}
