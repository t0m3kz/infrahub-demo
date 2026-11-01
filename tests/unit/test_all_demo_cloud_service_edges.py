"""The provider side of a cloud region in the demo data: a service edge appliance with one virtual NIC per leg.

A cloud region is a provider place with no hardware (TopologyCloudRegion hosts ``appliances``, not ``devices``), so
the provider's side of a connection is a CloudNetworkAppliance of role ``provider`` built from an appliance template,
its interfaces are DcimVirtualInterface NICs and the carrier that ends on a NIC is a provider-delivered
``virtual_connection``. Physical provider kit (the Megaport MCR, the Equinix Fabric port, the ISP routers) stays a
DcimPhysicalDevice in its own zone or external-provider place.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from tests.integration.demo_data import BOOTSTRAP, ROOT, declared, name, object_templates

_DEMOS = ROOT / "data" / "demos"
_ALL = _DEMOS / "30_all"
_REGION_KIND = "TopologyCloudRegion"
_APPLIANCE = "CloudNetworkAppliance"
#: One provider and one region normally hold ONE service edge. The exception: the AWS Site-to-Site VPN gateway speaks
#: its own Amazon-side AS (64513), the Direct Connect edge AS64512, and a device carries one BGP process.
_TWO_SERVICES_IN_ONE_REGION = {("P004", "eu-central-1"): {"EXT-AWS-DX-FR2", "EXT-AWS-VPN-EUC1"}}


def _regions() -> set[str]:
    """Every cloud region the demos declare (11_cloud and 30_all/01_cloud)."""
    return {name(region) for region in declared(_REGION_KIND, _DEMOS)}


def _appliances() -> dict[str, dict[str, Any]]:
    """Name -> declared CloudNetworkAppliance of 30_all."""
    return {name(a): a.item for a in declared(_APPLIANCE, _ALL)}


def _region_appliances() -> dict[str, dict[str, Any]]:
    """The appliances deployed to a cloud region."""
    regions = _regions()
    return {n: a for n, a in _appliances().items() if a.get("deployment") in regions}


def _nics() -> dict[str, set[str]]:
    """Device -> names of its declared DcimVirtualInterface objects."""
    nics: dict[str, set[str]] = defaultdict(set)
    for interface in declared("DcimVirtualInterface", _ALL):
        nics[str(interface.item["device"])].add(str(interface.item["name"]))
    return nics


def _carriers() -> dict[str, dict[str, Any]]:
    """circuit_id -> declared carrier circuit."""
    return {str(c.item["circuit_id"]): c.item for c in declared("TopologyCarrierCircuit", _ALL)}


def test_the_regions_are_declared_and_host_no_physical_device() -> None:
    """No DcimPhysicalDevice of any demo is deployed to a cloud region: the region has no `devices`."""
    regions = _regions()
    assert {"eu-central-1", "westeurope", "europe-west4"} <= regions
    physical = [
        (name(device), device.item.get("deployment"))
        for device in declared("DcimPhysicalDevice", _DEMOS)
        if device.item.get("deployment") in regions
    ]
    assert physical == []


def test_each_region_stub_is_a_provider_appliance_from_an_appliance_template() -> None:
    """The stubs of eu-central-1 and westeurope are CloudNetworkAppliance objects of an appliance template whose role
    is `provider`, device type and platform declared in the bootstrap data, with no physical port."""
    stubs = _region_appliances()
    assert {n: a["deployment"] for n, a in stubs.items()} == {
        "EXT-AWS-DX-FR2": "eu-central-1",
        "EXT-AWS-VPN-EUC1": "eu-central-1",
        "EXT-AZURE-ER-PA4": "westeurope",
    }
    device_types = {name(d) for d in declared("DcimDeviceType", BOOTSTRAP)}
    platforms = {name(p) for p in declared("DcimPlatform", BOOTSTRAP)}
    templates = object_templates()
    for stub_name, stub in stubs.items():
        template = templates[stub["object_template"]]
        assert (template["role"], template["status"]) == ("provider", "active"), stub_name
        assert template["device_type"] in device_types and template["platform"] in platforms, stub_name
        assert "interfaces" not in template, f"{stub_name}: a cloud region has no hardware, so no template port"


def test_one_service_edge_per_provider_and_region_but_the_documented_vpn_gateway() -> None:
    """Appliances of one provider (the template's owner) in one region are one service edge, except the AWS VPN
    gateway, which speaks another AS than the Direct Connect edge."""
    templates = object_templates()
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for stub_name, stub in _region_appliances().items():
        groups[(templates[stub["object_template"]]["owner"], stub["deployment"])].add(stub_name)
    shared = {key: names for key, names in groups.items() if len(names) > 1}
    assert shared == _TWO_SERVICES_IN_ONE_REGION
    processes = {d: e for e in (p.item for p in declared("ManagedBGP", _ALL)) for d in e["capabilities"]}
    connections = {c.item["name"]: c.item for c in declared("TopologyProviderConnection", _ALL)}
    assert processes["EXT-AWS-DX-FR2"]["local_as"] == connections["PC-FR2-AWS-EUC1-PROD"]["provider_as"]
    assert connections["PC-FR2-AWS-EUC1-VPN-PROD"]["provider_as"] != processes["EXT-AWS-DX-FR2"]["local_as"]
    assert "EXT-AWS-VPN-EUC1" not in processes


def test_every_carrier_end_on_an_appliance_is_a_declared_nic_of_a_virtual_connection() -> None:
    """A carrier whose end is an appliance of a region is a virtual_connection and the end a declared virtual NIC;
    no cable ends on an appliance."""
    appliances = set(_appliances())
    nics = _nics()
    seen = 0
    for circuit_id, circuit in _carriers().items():
        for device, port in circuit["capabilities"]:
            if device not in appliances:
                continue
            seen += 1
            assert circuit["circuit_type"] == "virtual_connection", circuit_id
            assert port in nics[device], f"{circuit_id}: {device}/{port} is not a declared DcimVirtualInterface"
    assert seen == 4  # the Direct Connect, the hosted Direct Connect and the two ExpressRoute legs
    for cable in declared("DcimCable", _ALL):
        for end in cable.item["endpoints"]:
            assert end["data"].get("device") not in appliances, name(cable)


def test_each_connection_leg_has_its_own_nic_on_the_service_edge() -> None:
    """The NICs a provider connection's carriers end on in the far region are distinct across connections, and the
    NIC names of a device are unique."""
    stubs = _region_appliances()
    carriers = _carriers()
    legs: dict[tuple[str, str], str] = {}
    for connection in declared("TopologyProviderConnection", _ALL):
        row = connection.item
        if row["path_type"] != "dedicated":
            continue
        for circuit_id in row.get("carrier_circuits") or []:
            for device, port in carriers[circuit_id]["capabilities"]:
                if device in stubs:
                    previous = legs.setdefault((device, port), row["name"])
                    assert previous == row["name"], f"{device}/{port} is a leg of {previous} and {row['name']}"
    assert legs == {
        ("EXT-AWS-DX-FR2", "vnic-fr2"): "PC-FR2-AWS-EUC1-PROD",
        ("EXT-AWS-DX-FR2", "vnic-amne"): "PC-AMNE-AWS-EUC1-PROD",
        ("EXT-AZURE-ER-PA4", "vnic-pri"): "PC-PA4-AZURE-WEU-PRI-PROD",
        ("EXT-AZURE-ER-PA4", "vnic-sec"): "PC-PA4-AZURE-WEU-SEC-PROD",
    }
    names = [(str(i.item["device"]), str(i.item["name"])) for i in declared("DcimVirtualInterface", _ALL)]
    assert len(names) == len(set(names))


def test_physical_provider_kit_stays_physical_in_its_own_zone() -> None:
    """The Megaport MCR and the Equinix Fabric port are DcimPhysicalDevice objects outside every cloud region and their
    carrier with our cage edge is a physical cross-connect (the Fabric port is cabled)."""
    regions = _regions()
    physical = {name(d): d.item for d in declared("DcimPhysicalDevice", _ALL)}
    for kit, place in (("EXT-MEGAPORT-MCR1", "MCR-PAR1"), ("EXT-EQX-FABRIC-FR2", "FR2")):
        assert physical[kit]["deployment"] == place and place not in regions
    assert _carriers()["XC-EQXPA4-MEGAPORT"]["circuit_type"] == "cross_connect"
