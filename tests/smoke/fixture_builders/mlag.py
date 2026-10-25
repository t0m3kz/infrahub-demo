"""MLAG / vPC pair fixtures, including the NX-OS virtual peer-link."""

from __future__ import annotations

from tests.smoke.fixture_builders.common import (
    _edges,
    _make_interface,
    _node,
    _v,
    _write_data_fixture,
    write_data_fixtures,
)
from transforms.config.border_leaf import BorderLeaf
from transforms.config.leaf import Leaf
from transforms.config.tor import ToR


def build_mlag_device_data(
    *,
    device_name: str,
    peer_name: str,
    role: str,
    platform: str,
    domain_id: int = 1,
    domain_name: str | None = None,
) -> dict:
    """Build GQL response for a device with ManagedMLAG and Port-Channel peer-link.

    Generates: Loopback0, 2 uplink P2P interfaces, Port-Channel100 (mlag-peer),
    and 2 member physical interfaces (Ethernet1/33 + Ethernet1/34).
    No BGP/OSPF capabilities — MLAG-only scenario.
    """
    if domain_name is None:
        domain_name = f"POD1-{device_name.split('-')[-1].upper()}-{peer_name.split('-')[-1].upper()}-MLAG"

    # MLAG control session addressing mirrors generators/mlag.py: SONiC gets an
    # IPv4 /31 (ICCP), vPC has none (keepalive over mgmt0), others an IPv6 /127.
    if platform in {"sonic", "dell_sonic"}:
        control_addresses: tuple[str, str] | None = ("10.254.0.0/31", "10.254.0.1/31")
    elif platform == "cisco_nxos":
        control_addresses = None
    else:
        control_addresses = ("fd00:2400::/127", "fd00:2400::1/127")

    def member(name: str, management_address: str, control_address: str | None) -> dict:
        interfaces = []
        if control_address:
            interfaces.append(
                {
                    "__typename": "DcimVirtualInterface",
                    "name": _v("Vlan4094"),
                    "role": _v("mlag-control"),
                    "ip_address": _node({"address": _v(control_address)}),
                }
            )
        return {
            "name": _v(name),
            "role": _v(role),
            "primary_address": _node({"address": _v(management_address)}),
            "interfaces": _edges(interfaces),
        }

    mlag_cap = {
        "__typename": "ManagedMLAG",
        "id": "mlag-dom-1",
        "name": _v(domain_name),
        "domain_id": _v(domain_id),
        "reload_delay": _v(300),
        "reload_delay_non_mlag": _v(330),
        "capabilities": _edges(
            [
                member(device_name, "172.16.0.1/32", control_addresses[0] if control_addresses else None),
                member(peer_name, "172.16.0.2/32", control_addresses[1] if control_addresses else None),
            ]
        ),
    }

    # Determine interface names and LAG interface shape by platform
    # cisco_nxos uses Ethernet1/N format; others use EthernetN
    if platform == "cisco_nxos":
        uplink1, uplink2, member1, member2 = "Ethernet1/1", "Ethernet1/2", "Ethernet1/33", "Ethernet1/34"
        lag_name, lag_id_val = "port-channel100", 100
    elif platform in {"sonic", "dell_sonic"}:
        uplink1, uplink2, member1, member2 = "Ethernet1", "Ethernet2", "Ethernet33", "Ethernet34"
        lag_name, lag_id_val = "PortChannel100", 100
    else:
        uplink1, uplink2, member1, member2 = "Ethernet1", "Ethernet2", "Ethernet33", "Ethernet34"
        lag_name, lag_id_val = "Port-Channel100", 100

    loopback = _make_interface(
        name="Loopback0",
        device_name=device_name,
        description="Router ID",
        role="loopback",
        ip_address="10.0.2.1/32",
        typename="DcimVirtualInterface",
    )

    up1 = _make_interface(
        name=uplink1,
        device_name=device_name,
        description="to spine-01",
        role="uplink",
        ip_address="10.1.2.1/31",
    )
    up2 = _make_interface(
        name=uplink2,
        device_name=device_name,
        description="to spine-02",
        role="uplink",
        ip_address="10.1.2.3/31",
    )

    lag_iface: dict = {
        "__typename": "DcimLAGInterface",
        "name": _v(lag_name),
        "description": _v("MLAG Peer-Link"),
        "status": _v("active"),
        "role": _v("mlag-peer"),
        "lag_id": _v(lag_id_val),
        "lacp_mode": _v("active"),
        "mtu": _v(9000),
        "minimum_links": _v(1),
        "ip_address": _node(None),
        "mlag_domain": _node({"id": "mlag-dom-1", "name": _v(domain_name)}),
        "member_interfaces": _edges(
            [
                {"name": _v(member1)},
                {"name": _v(member2)},
            ]
        ),
        "capabilities": _edges([]),
    }

    mem1 = _make_interface(
        name=member1,
        device_name=device_name,
        description="MLAG peer-link member 1",
        role="mlag-peer",
    )
    mem2 = _make_interface(
        name=member2,
        device_name=device_name,
        description="MLAG peer-link member 2",
        role="mlag-peer",
    )

    device_interfaces = [loopback, up1, up2, lag_iface, mem1, mem2]
    if control_addresses:
        # Rendered by the MLAG include only — the transform must keep it out of
        # the generic interface list.
        device_interfaces.append(
            _make_interface(
                name="Vlan4094",
                device_name=device_name,
                description="MLAG control — DC1-POD1-L1-L2-MLAG",
                role="mlag-control",
                ip_address=control_addresses[0],
                typename="DcimVirtualInterface",
            )
        )
    if platform in {"sonic", "dell_sonic"}:
        device_interfaces.extend(
            [
                {
                    **lag_iface,
                    "name": _v("PortChannel101"),
                    "role": _v("lag"),
                    "lag_id": _v(101),
                    "mlag_domain": _node({"id": "mlag-dom-1", "name": _v(domain_name)}),
                    "member_interfaces": _edges([{"name": _v("Ethernet10")}]),
                },
                _make_interface(
                    name="Ethernet10", device_name=device_name, description="MLAG host member", role="customer"
                ),
            ]
        )

    device_node: dict = {
        "__typename": "DcimPhysicalDevice",
        "id": f"dev-{device_name}",
        "name": _v(device_name),
        "role": _v(role),
        "platform": _node(
            {
                "id": f"plat-{platform}",
                "name": _v(platform),
            }
        ),
        "primary_address": _node(
            {
                "address": _v("172.16.0.1/32"),
                "ip_namespace": _node({"name": _v("default")}),
            }
        ),
        "tags": _edges([]),
        "capabilities": _edges([mlag_cap]),
        "interfaces": _edges(device_interfaces),
        "deployment": _node(
            {
                "id": "dc-1",
                "name": _v("DC-1"),
                "segment_deployments": _edges([]),
            }
        ),
    }

    return {"DcimDevice": _edges([device_node])}


def build_mlag_virtual_peer_link_data(*, device_name: str, peer_name: str, role: str, platform: str) -> dict:
    """A pod with ``mlag_create: virtual``: the peer-link is a member-less ``loopback100``
    DcimVirtualInterface (role mlag-peer, no IP on NX-OS) instead of a port-channel
    (generators/mlag.py _ensure_virtual_peer_link), so no physical peer-link exists to bind."""
    data = build_mlag_device_data(
        device_name=device_name,
        peer_name=peer_name,
        role=role,
        platform=platform,
        domain_id=1,
        domain_name="DC1-POD1-L1-L2-MLAG",
    )
    node = data["DcimDevice"]["edges"][0]["node"]
    kept = [
        edge
        for edge in node["interfaces"]["edges"]
        if edge["node"]["role"]["value"] != "mlag-peer"  # drops the port-channel and its member ports
    ]
    virtual = _make_interface(
        name="loopback100",
        device_name=device_name,
        description="MLAG virtual peer-link — DC1-POD1-L1-L2-MLAG",
        role="mlag-peer",
        typename="DcimVirtualInterface",
    )
    node["interfaces"]["edges"] = [*kept, {"node": virtual}]
    return data


def write_fixtures() -> tuple[int, int]:
    """Write the MLAG / vPC pair fixtures and the NX-OS virtual peer-link vPC fixture."""
    # MLAG/VPC scenarios: leaf, border_leaf, tor — platforms with MLAG templates
    mlag_platforms = ["arista_eos", "cisco_nxos", "dell_sonic", "sonic"]
    # (transform, dir_prefix, role) — note the order differs from DEVICE_CONFIGS.
    # `role` is the schema dropdown value (hyphenated); dir_prefix keeps the
    # underscore so fixture directory names are unchanged.
    mlag_device_types: list[tuple[type, str, str]] = [
        (Leaf, "leaf", "leaf"),
        (BorderLeaf, "border_leaf", "border-leaf"),
        (ToR, "tor", "tor"),
    ]
    print("\nGenerating MLAG/VPC fixtures:")
    generated, errors = write_data_fixtures(
        (
            transform_cls,
            f"{type_prefix}_{platform}_mlag",
            build_mlag_device_data(
                device_name="DC1-POD1-L1",
                peer_name="DC1-POD1-L2",
                role=role,
                platform=platform,
                domain_id=1,
                domain_name="DC1-POD1-L1-L2-MLAG",
            ),
        )
        for transform_cls, type_prefix, role in mlag_device_types
        for platform in mlag_platforms
    )

    # vPC on a pod with a virtual peer-link: NX-OS cannot bind `vpc peer-link` to loopback100, so the
    # whole vPC block is a comment (no `interface loopback100 ... vpc peer-link`).
    print("\nGenerating the NX-OS virtual peer-link vPC fixture:")
    g, e = _write_data_fixture(
        Leaf,
        "leaf_cisco_nxos_vpc_virtual",
        build_mlag_virtual_peer_link_data(
            device_name="DC1-POD1-L1", peer_name="DC1-POD1-L2", role="leaf", platform="cisco_nxos"
        ),
    )
    return generated + g, errors + e
