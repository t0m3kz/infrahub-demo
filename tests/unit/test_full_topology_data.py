"""The layout of the full-topology sample (data/demos/00_full_topology) read from its object files, without a server.

The sample has three sites that reach each other and the INTERNET: the DC (DC90), one colocation cage (DMC1) and the
C900 office. The cloud and SaaS legs land in the cage and never at the DC, the DC and the cage are joined by dark fibre,
the office has an edge of its own that its tunnels end on, and every site has internet access. The traversal check
(`tests/traversal/walks.py` `layout_rows`) asserts the same on a live server; this is the offline guard on the YAML.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.demo_data import ROOT, declared

SAMPLE = ROOT / "data" / "demos" / "00_full_topology"
CAGE_EDGE, FABRIC_PORT, OFFICE_EDGE, DC_EDGE = "eg-dmc101", "EXT-EQX-FABRIC-DMC1", "C900-OFFICE-EDGE1", "dc90-edge-01"


def _rows(kind: str, folder: str = "") -> list[dict[str, Any]]:
    """The entries of `kind` the object files under the sample (or one of its folders) declare."""
    return [obj.item for obj in declared(kind, SAMPLE / folder)]


def _ends(circuit: dict[str, Any]) -> list[str]:
    """The device names at the ends of a circuit, as `[device, interface]` HFIDs."""
    return [end[0] for end in circuit["capabilities"]]


def _carriers(*circuit_types: str) -> list[dict[str, Any]]:
    return [c for c in _rows("TopologyCarrierCircuit") if c["circuit_type"] in circuit_types]


def test_the_colocation_folder_is_one_metro_with_one_zone_and_its_edge() -> None:
    """02_colo is no longer reserved: one colocation, one metro, one zone (the cage) and its edge."""
    assert [z["name"] for z in _rows("TopologyColocationZone", "02_colo")] == ["DMC1"]
    assert [m["name"] for m in _rows("TopologyColocationMetro", "02_colo")] == ["DMC"]
    devices = {d["name"]: d for d in _rows("DcimPhysicalDevice", "02_colo")}
    assert set(devices) == {CAGE_EDGE, FABRIC_PORT}
    assert {d["deployment"] for d in devices.values()} == {"DMC1"}


def test_the_cage_has_one_patch_panel_with_one_cabled_pair_to_the_fabric_port() -> None:
    """Edge Ethernet1/1 - front 1 | rear 1 - Fabric port et-0/0/0; the front port is paired with the rear port."""
    assert [p["name"] for p in _rows("DcimPatchPanel", "02_colo")] == ["PP-DMC1-01"]
    fronts = [p for p in _rows("DcimFrontPatchPanelPort", "02_colo") if "rear_port" in p]
    assert [(p["name"], p["rear_port"]) for p in fronts] == [("1", "dmc1-s-1-r-1-1:PP-DMC1-01:1")]
    cables = {c["name"]: c for c in _rows("DcimCable", "02_colo")}
    assert set(cables) == {"CBL-DMC1-EDGE-PP01-F1", "CBL-DMC1-PP01-R1-FABRIC"}
    devices = {e["data"].get("device") for c in cables.values() for e in c["endpoints"]}
    assert devices == {CAGE_EDGE, FABRIC_PORT, None}  # None: the panel ports


def test_every_cloud_and_saas_carrier_starts_in_the_cage_and_none_at_the_dc() -> None:
    """A Direct Connect or a Fabric connection is delivered to a cage; no cross-connect or virtual connection has a DC end."""
    legs = _carriers("cross_connect", "virtual_connection")
    assert len(legs) == 7  # 2 Direct Connect, 3 Fabric connections, Dedicated Interconnect, Webex
    for leg in legs:
        near = [d for d in _ends(leg) if d in (CAGE_EDGE, FABRIC_PORT)]
        assert len(near) == 1, leg["name"]
        assert leg["locations"][0] == "DMC1", leg["name"]
        assert not [d for d in _ends(leg) if d.startswith("dc90-")], leg["name"]


def test_no_cloud_session_ends_on_the_dc_edge() -> None:
    """The handoffs and the AWS VPN: handoffs end on the cage edge, the VPN tunnels on the DC edge (internet access)."""
    for circuit in _rows("TopologySessionCircuit"):
        if circuit["circuit_type"] == "handoff":
            assert CAGE_EDGE in _ends(circuit), circuit["name"]
        if circuit["circuit_type"] == "vpn":
            assert DC_EDGE in _ends(circuit), circuit["name"]
    sub_interfaces = {i["name"] for i in _rows("DcimVirtualInterface") if i["device"] == DC_EDGE}
    assert sub_interfaces == {"Tunnel1", "Tunnel2", "Tunnel4"}


def test_two_dark_fibres_join_the_dc_border_leaves_to_the_cage_edge_and_a_dci_rides_each() -> None:
    """DF-* carriers and ICX-* session circuits over the same two ports, each end with one overlay process."""
    fibres = {c["circuit_id"]: c for c in _carriers("dark_fiber")}
    assert set(fibres) == {"DF-DC90-DMC1", "DF-DC90-DMC1-B"}
    dci = {c["name"]: c for c in _rows("TopologySessionCircuit") if c["circuit_type"] == "dci"}
    assert set(dci) == {"ICX-DC90-DMC1", "ICX-DC90-DMC1-B"}
    for fibre, circuit in zip(
        sorted(fibres.values(), key=lambda c: c["name"]), [dci[k] for k in sorted(dci)], strict=True
    ):
        assert fibre["capabilities"] == circuit["capabilities"]
        assert fibre["locations"] == circuit["locations"] == ["DC90", "DMC1"]
        assert sorted(_ends(fibre)) == sorted([CAGE_EDGE, fibre["capabilities"][0][0]])
        assert fibre["capabilities"][0][0] in ("dc90-bl-01", "dc90-bl-02")
    overlay = [p["capabilities"] for p in _rows("ManagedBGP") if p["process_role"] == "overlay"]
    for device in (CAGE_EDGE, "dc90-bl-01", "dc90-bl-02"):
        assert [c for c in overlay if c == [device]] == [[device]], device


def test_the_office_has_an_edge_of_its_own_and_both_tunnels_end_on_it() -> None:
    """The edge is deployed to the office; each tunnel has the office among its locations and the edge as one end."""
    edges = [d for d in _rows("DcimPhysicalDevice", "05_office") if d["name"] == OFFICE_EDGE]
    assert [e["deployment"] for e in edges] == ["C900-OFFICE"]
    tunnels = [c for c in _rows("TopologySessionCircuit") if c["circuit_type"] == "tunnel"]
    assert {t["name"] for t in tunnels} == {"SC-C900-OFFICE-DC90", "SC-C900-ZSCALER-TUN1"}
    for tunnel in tunnels:
        assert "C900-OFFICE" in tunnel["locations"]
        assert OFFICE_EDGE in _ends(tunnel)
        assert DC_EDGE not in _ends(tunnel) or tunnel["name"] == "SC-C900-OFFICE-DC90"


@pytest.mark.parametrize(
    ("site", "port", "isp_router"),
    [
        (DC_EDGE, "Ethernet1/2", "EXT-ISP-DEMO-PE1"),
        (CAGE_EDGE, "Ethernet1/14", "EXT-ISP-DEMO-B-PE1"),
        (OFFICE_EDGE, "GE1", "EXT-ISP-DEMO-PE1"),
    ],
)
def test_every_site_has_an_internet_access_to_an_isp_router_that_is_attached_to_the_internet(
    site: str, port: str, isp_router: str
) -> None:
    """The access carrier is `internet`; the ISP router has an uplink carrier to INTERNET:bus (rule I3)."""
    access = [c for c in _carriers("internet") if [site, port] in c["capabilities"]]
    assert len(access) == 1
    assert isp_router in _ends(access[0])
    uplinks = [c for c in _carriers("internet") if ["INTERNET", "bus"] in c["capabilities"]]
    assert any(isp_router in _ends(c) for c in uplinks)


def test_the_cage_carriers_name_the_ports_the_sub_interfaces_ride_on() -> None:
    """Each handoff sub-interface of the cage edge rides a port that is cabled (the Fabric port) or ends a cross-connect."""
    cabled = {"Ethernet1/1"}
    carried = {end[1] for c in _carriers("cross_connect") for end in c["capabilities"] if end[0] == CAGE_EDGE}
    for interface in _rows("DcimVirtualInterface", "02_colo"):
        parent = interface["parent_interface"][1]
        assert parent in cabled | carried, interface["name"]
