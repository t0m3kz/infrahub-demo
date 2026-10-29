"""Unit tests for the VRF instances in the config transforms (transforms/helpers/vrf.py and the common stage).

Covers:
  - the refusal           — VrfRealizationError for a session, an address, a routed VLAN with no SVI object, an SVI in
                            the wrong VRF and a transit leg in a VRF the device has no instance of; one raise lists all
  - the hosted appliance  — a CloudNetworkAppliance on cisco_xe renders a comment banner and logs a WARNING instead
  - what is not demanded  — a device that never carries VRFs, ``default``, an inline-terminated segment
  - the realization rule  — an instance is an L3 instance only with a routed activation (or an exit)
  - the readers           — device_vrfs, svi_vrfs, l3_vrf_names
The inputs are the committed smoke inputs (which already carry the instances and SVIs the fixture hook adds) with a
node taken out, rendered through the real transforms WITHOUT the hook.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tests.smoke.fixture_builders.common import make_transform
from transforms.config.border_leaf import BorderLeaf
from transforms.config.edge import Edge
from transforms.config.leaf import Leaf
from transforms.config.spine import Spine
from transforms.helpers.vrf import (
    VrfRealizationError,
    device_vrfs,
    l3_vrf_names,
    svi_vrfs,
    unrealized_vrfs,
    vrf_requirements,
)
from transforms.helpers.vxlan import get_vxlan_config
from utils.circuit_underlay import NETWORK_APPLIANCE_KIND
from utils.data_cleaning import clean_data

_CONFIGS = Path(__file__).resolve().parents[1] / "smoke" / "configs"
_LEAF = "leaf_arista_eos_ebgp_ibgp"
_BORDER = "border_leaf_cisco_nxos_exchange_transit"
_EDGE_PEERS = "edge_cisco_nxos_external_vrf_peers"
_APPLIANCE = "edge_cisco_xe_unfiltered_appliance"


def _raw(name: str) -> dict[str, Any]:
    return json.loads((_CONFIGS / name / "input.json").read_text())


def _node(data: dict[str, Any]) -> dict[str, Any]:
    return data["DcimDevice"]["edges"][0]["node"]


def _render(transform_cls: type, data: dict[str, Any]) -> str:
    """Render raw query data through the real transform, with no fixture hook in between."""
    instance = make_transform(transform_cls)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(instance.transform(copy.deepcopy(data)))
    finally:
        loop.close()


def _without_instance(data: dict[str, Any], namespace: str) -> dict[str, Any]:
    edges = _node(data)["vrfs"]["edges"]
    edges[:] = [edge for edge in edges if edge["node"]["namespace"]["node"]["name"]["value"] != namespace]
    return data


def _without_svi(data: dict[str, Any], name: str) -> dict[str, Any]:
    edges = _node(data)["interfaces"]["edges"]
    edges[:] = [edge for edge in edges if edge["node"]["name"]["value"] != name]
    return data


def _instances(data: dict[str, Any]) -> list[str]:
    return [edge["node"]["namespace"]["node"]["name"]["value"] for edge in _node(data)["vrfs"]["edges"]]


def _svis(data: dict[str, Any]) -> dict[str, str | None]:
    return {
        edge["node"]["name"]["value"]: (((edge["node"]["vrf"]["node"] or {}).get("namespace") or {}).get("node") or {})
        .get("name", {})
        .get("value")
        for edge in _node(data)["interfaces"]["edges"]
        if edge["node"]["role"]["value"] == "svi"
    }


class TestTheGoldenInputsAreComplete:
    @pytest.mark.parametrize("name", [_LEAF, _BORDER, _EDGE_PEERS, _APPLIANCE])
    def test_a_committed_input_renders_without_the_hook(self, name: str) -> None:
        """The fixture hook leaves a complete model behind: the transform has nothing to refuse."""
        data = _raw(name)
        cls = {"leaf": Leaf, "border": BorderLeaf, "edge": Edge}[name.split("_")[0]]
        assert _render(cls, data)


class TestRefusal:
    def test_a_routed_segment_in_a_vrf_without_an_instance_is_refused(self) -> None:
        data = _without_instance(_raw(_LEAF), "VRF_A")
        assert "VRF_A" in _svis(data).values()

        with pytest.raises(VrfRealizationError, match="no VRF instance of namespace VRF_A"):
            _render(Leaf, data)

    def test_a_vrf_session_in_a_vrf_without_an_instance_is_refused(self) -> None:
        """The session's local address is in the namespace: the edge needs an instance of it."""
        data = _without_instance(_raw(_EDGE_PEERS), "PROD")

        with pytest.raises(VrfRealizationError, match="no VRF instance of namespace PROD"):
            _render(Edge, data)

    def test_a_transit_leg_in_a_vrf_without_an_instance_is_refused(self) -> None:
        data = _without_instance(_raw(_BORDER), "INTERNET")

        with pytest.raises(VrfRealizationError, match="no VRF instance of namespace INTERNET"):
            _render(BorderLeaf, data)

    def test_a_routed_vlan_without_an_svi_object_is_refused(self) -> None:
        data = _without_svi(_raw(_LEAF), "Vlan100")

        with pytest.raises(VrfRealizationError, match="no SVI Vlan100 for a routed segment"):
            _render(Leaf, data)

    def test_an_svi_in_another_vrf_than_its_segment_is_refused(self) -> None:
        data = _raw(_LEAF)
        for edge in _node(data)["interfaces"]["edges"]:
            if edge["node"]["name"]["value"] == "Vlan100":
                edge["node"]["vrf"] = {"node": None}

        with pytest.raises(VrfRealizationError, match="SVI Vlan100 is in VRF default, its segment routes in VRF_A"):
            _render(Leaf, data)

    def test_one_raise_lists_everything_missing(self) -> None:
        data = _without_svi(_without_instance(_raw(_LEAF), "VRF_A"), "Vlan200")

        with pytest.raises(VrfRealizationError) as error:
            _render(Leaf, data)

        message = str(error.value)
        assert "no VRF instance of namespace VRF_A" in message and "no SVI Vlan200" in message
        assert message.startswith("dc1-leaf-01: arista_eos config")

    def test_the_exception_is_a_value_error(self) -> None:
        """Callers (the SDK, the existing refusals) already handle ValueError."""
        assert issubclass(VrfRealizationError, ValueError)

    def test_a_device_that_never_carries_vrfs_is_never_refused(self) -> None:
        """A spine has no segment, VRF or SVI; whatever its data says is not demand."""
        data = _raw(_LEAF)
        _node(data)["role"]["value"] = "spine"
        _without_instance(data, "VRF_A")

        assert unrealized_vrfs(clean_data(data)["DcimDevice"][0]) == []
        assert vrf_requirements(clean_data(data)["DcimDevice"][0]).namespaces == frozenset()
        assert _render(Spine, data)


class TestHostedAppliance:
    def test_an_appliance_on_cisco_xe_gets_a_banner_and_a_warning_instead_of_a_refusal(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        data = _without_instance(_raw(_APPLIANCE), "PROD")
        assert _node(data)["__typename"] == NETWORK_APPLIANCE_KIND

        with caplog.at_level(logging.DEBUG):
            output = _render(Edge, data)

        assert "! VRF-UNREALIZED: no VRF instance of namespace PROD" in output
        warnings = [r for r in caplog.records if r.name == "transforms.common" and "no instance of" in r.getMessage()]
        assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_the_banner_is_a_comment_above_the_hostname(self) -> None:
        output = _render(Edge, _without_instance(_raw(_APPLIANCE), "PROD"))
        lines = output.splitlines()
        banner = next(i for i, line in enumerate(lines) if "VRF-UNREALIZED" in line)
        assert lines[banner].startswith("! ") and lines[banner + 1].startswith("hostname ")

    def test_a_complete_appliance_renders_no_banner(self) -> None:
        assert "VRF-UNREALIZED" not in _render(Edge, _raw(_APPLIANCE))

    def test_an_appliance_on_another_platform_is_refused(self) -> None:
        data = _without_instance(_raw(_APPLIANCE), "PROD")
        _node(data)["platform"]["node"]["name"] = {"value": "arista_eos"}

        with pytest.raises(VrfRealizationError):
            _render(Edge, data)

    def test_a_physical_device_on_cisco_xe_is_refused(self) -> None:
        data = _without_instance(_raw(_APPLIANCE), "PROD")
        _node(data)["__typename"] = "DcimPhysicalDevice"

        with pytest.raises(VrfRealizationError):
            _render(Edge, data)


class TestRequirements:
    @staticmethod
    def _device(name: str) -> dict[str, Any]:
        transform = make_transform({"leaf": Leaf, "border": BorderLeaf, "edge": Edge}[name.split("_")[0]])
        device, _, _ = transform.prepared_device(_raw(name))
        return device

    def test_the_demand_names_segments_sessions_and_legs(self) -> None:
        assert set(vrf_requirements(self._device(_LEAF)).namespaces) == {"VRF_A"}
        assert set(vrf_requirements(self._device(_BORDER)).namespaces) == {"INTERNET", "MANAGEMENT", "PROD"}

    def test_a_routed_segment_needs_the_svi_of_its_vlan_in_its_namespace(self) -> None:
        """VLAN 200's gateway is in the default namespace: its SVI is needed, in no VRF."""
        assert vrf_requirements(self._device(_LEAF)).svis == {100: "VRF_A", 200: None}

    def test_the_default_namespace_is_never_demanded_and_its_svi_has_no_vrf(self) -> None:
        device = self._device(_LEAF)
        for activation in device["segment_deployments"]:
            activation["segment"]["gateway"]["ip_prefix"]["ip_namespace"] = {"name": "default"}
        device["interfaces"] = [iface for iface in device["interfaces"] if iface.get("role") != "svi"]
        requirements = vrf_requirements(device)
        assert requirements.namespaces == frozenset()
        assert requirements.svis == {100: None, 200: None}

    def test_an_inline_terminated_segment_needs_neither_vrf_nor_svi(self) -> None:
        device = self._device(_LEAF)
        for activation in device["segment_deployments"]:
            activation["segment"]["terminate_inline"] = True
        device["interfaces"] = [iface for iface in device["interfaces"] if iface.get("role") != "svi"]
        requirements = vrf_requirements(device)
        assert requirements.svis == {}
        assert requirements.namespaces == frozenset()


class TestReaders:
    def test_device_vrfs_reads_the_namespace_of_each_instance_once(self) -> None:
        device = clean_data(_raw(_BORDER))["DcimDevice"][0]
        assert {ns["name"]: ns["namespace_type"] for ns in device_vrfs(device)} == {
            "INTERNET": "internet",
            "MANAGEMENT": "management",
            "PROD": "prod",
        }

    def test_svi_vrfs_maps_the_vlan_to_the_vrf_name(self) -> None:
        interfaces = [
            {"name": "Vlan100", "role": "svi", "vrf": {"namespace": {"name": "PROD"}}},
            {"name": "Vlan200", "role": "svi", "vrf": None},
            {"name": "Vlan4094", "role": "mlag-control"},
            {"name": "Vlanx", "role": "svi"},
        ]
        assert svi_vrfs(interfaces) == {100: "PROD", 200: None}
        assert svi_vrfs(None) == {}

    def test_l3_vrf_names_prefers_the_lisp_site(self) -> None:
        assert l3_vrf_names({"l3_instances": [{"vrf": "A"}]}, {"l3_vni_mappings": [{"vrf_name": "B"}]}) == ["A"]
        assert l3_vrf_names(None, {"l3_vni_mappings": [{"vrf_name": "B"}]}) == ["B"]
        assert l3_vrf_names(None, None) == []


class TestRealizationRule:
    def test_a_vrf_lite_instance_on_an_edge_with_an_overlay_process_stays_vrf_lite(self) -> None:
        """The edge has an overlay BGP process and PROD / NON-PROD instances for its handoffs: no L3 VNI for them."""
        device = clean_data(_raw(_EDGE_PEERS))["DcimDevice"][0]
        assert {ns["name"] for ns in device_vrfs(device)} >= {"PROD", "NON-PROD"}
        assert any(cap.get("name") == "bgp-overlay" for cap in device["capabilities"])

        assert get_vxlan_config(device, "cisco_nxos", device_role="edge", activations=[], fabric_rt_asn=65100) is None
        assert "vni" not in _render(Edge, _raw(_EDGE_PEERS)).split("vrf context PROD")[1].split("vrf context")[0]

    def test_the_golden_l3_vnis_are_the_routed_vrfs(self) -> None:
        config = get_vxlan_config(
            clean_data(_raw(_LEAF))["DcimDevice"][0],
            "arista_eos",
            device_role="leaf",
            activations=make_transform(Leaf).prepared_device(_raw(_LEAF))[0]["segment_deployments"],
            fabric_rt_asn=65100,
        )
        assert config is not None
        assert [m["vrf_name"] for m in config["l3_vni_mappings"]] == ["VRF_A"]


class TestFixtureHook:
    """tests/smoke/fixture_builders/vrf.py adds what the transform's own requirements demand, once."""

    @pytest.mark.parametrize("name", [_LEAF, _BORDER, _EDGE_PEERS])
    def test_the_hook_restores_what_was_taken_out_and_is_idempotent(self, name: str) -> None:
        from tests.smoke.fixture_builders.vrf import with_vrf_objects

        cls = {"leaf": Leaf, "border": BorderLeaf, "edge": Edge}[name.split("_")[0]]
        transform = make_transform(cls)
        golden = _raw(name)
        stripped = copy.deepcopy(golden)
        for namespace in _instances(golden):
            _without_instance(stripped, namespace)
        for svi in _svis(golden):
            _without_svi(stripped, svi)

        restored = with_vrf_objects(transform, stripped)

        assert sorted(_instances(restored)) == sorted(_instances(golden))
        assert _svis(restored) == _svis(golden)
        assert with_vrf_objects(transform, restored) == restored
        assert _render(cls, restored)


class TestMembershipOfAnotherDeployment:
    def test_the_transform_renders_the_vrf_set_as_given(self) -> None:
        """A membership whose deployment is not the device's is rendered like any other: the check reports it."""
        data = _raw(_EDGE_PEERS)
        stray = {
            "node": {
                "namespace": {
                    "node": {
                        "name": {"value": "OTHER-DC"},
                        "l3_vni": {"value": 50009},
                        "namespace_type": {"value": "prod"},
                    }
                },
                "deployment": {"node": {"name": {"value": "some-other-dc"}}},
            }
        }
        _node(data)["vrfs"]["edges"].append(stray)

        assert "OTHER-DC" not in _render(Edge, _raw(_EDGE_PEERS))
        assert "vrf context OTHER-DC" in _render(Edge, data)
