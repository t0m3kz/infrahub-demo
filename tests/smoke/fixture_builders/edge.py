"""Edge and border-leaf BGW fixtures: stretched segments, INTERNET handoff, external tenant-VRF peers, edge pair."""

from __future__ import annotations

from tests.smoke.fixture_builders.common import (
    _af_edges,
    _deployment_record,
    _edges,
    _make_bgp_peering,
    _make_interface,
    _make_policy_rule,
    _make_security_policy,
    _node,
    _v,
    _write_data_fixture,
)
from tests.smoke.fixture_builders.fabric import _INLINE_SEGMENT_KWARGS, _make_inline_segment_node, build_device_data
from tests.smoke.fixture_builders.route_protection_roots import TENANT_NAMESPACE, add_environment_roots
from tests.smoke.fixture_builders.transit import build_exchange_transit_bl_data
from tests.smoke.fixture_builders.vrf import _vrf_node
from transforms.config.border_leaf import BorderLeaf
from transforms.config.edge import Edge


def build_border_leaf_bgw_stretched_data(*, device_name: str, platform: str) -> dict:
    """A DC border-leaf that is an EVPN Multi-Site BGW with two stretched, hostless segments.

    The exchange-transit wiring (shared and dedicated firewall context, each serving one
    PROD segment) minus the customer port, plus a multisite-vip loopback and
    stretch_scope dc_pair on both segments: the BGW stitches them without any
    customer-facing port, so it renders their VLAN, VNI and anycast SVI but no tenant
    policy (no ACL, no PBR). Those belong to the leaves that host the segments.
    """
    data = build_exchange_transit_bl_data(device_name=device_name, platform=platform, with_customer_port=False)
    device_node = data["DcimDevice"]["edges"][0]["node"]
    device_node["interfaces"]["edges"].append(
        {
            "node": _make_interface(
                name="Loopback100",
                device_name=device_name,
                description="Multi-Site VIP",
                role="multisite-vip",
                ip_address="10.255.100.1/32",
                typename="DcimVirtualInterface",
            )
        }
    )
    for dep in device_node["deployment"]["node"]["segment_deployments"]["edges"]:
        segment = dep["node"]["segment"]["node"]
        segment["stretch_scope"] = _v("dc_pair")
        # Zero-trust policy on the segments, as on the leaves that host them: the ACL and PBR
        # the BGW must not take as input.
        segment["security_policy"] = _node(
            _make_security_policy(
                name="policy-bgw-seg",
                rules=[_make_policy_rule(index=10, name="allow-https", protocol="tcp", port_start=443)],
            )
        )
    # The DC-wide firewall contexts (what makes the leaves redirect these segments to the
    # firewall): the same contexts the firewall-role port is tagged with.
    fw_port = next(e["node"] for e in device_node["interfaces"]["edges"] if e["node"]["role"] == _v("firewall"))
    firewall = {
        "node": {
            "capabilities": _edges([{"__typename": "ManagedFirewallHA", "contexts": fw_port["capabilities"]}]),
        }
    }
    device_node["deployment"]["node"]["devices"] = {"edges": [firewall]}
    return data


def build_edge_internet_bgw_data(*, device_name: str, platform: str) -> dict:
    """A colocation edge that is its site's EVPN Multi-Site BGW and peers an ISP in INTERNET.

    ebgp_ebgp edge plus a multisite-vip loopback (what makes it a BGW) and one
    upstream transit peering: Ethernet1/14 in namespace INTERNET (namespace_type
    internet, l3_vni 60002) towards the ISP PE. No stretched segments, so the
    INTERNET L3 VNI comes from the transit session alone.
    """
    data = build_device_data(
        device_name=device_name, role="edge", platform=platform, scenario="ebgp_ebgp", include_segments=False
    )
    device_node = data["DcimDevice"]["edges"][0]["node"]
    interfaces = device_node["interfaces"]["edges"]
    interfaces.append(
        {
            "node": _make_interface(
                name="Loopback1",
                device_name=device_name,
                description="Multi-Site VIP",
                role="multisite-vip",
                ip_address="10.0.0.50/32",
                typename="DcimVirtualInterface",
            )
        }
    )
    internet_ns = _node({"name": _v("INTERNET"), "l3_vni": _v(60002), "namespace_type": _v("internet")})
    interfaces.append(
        {
            "node": {
                **_make_interface(
                    name="Ethernet1/14",
                    device_name=device_name,
                    description="to EXT-LUMEN-FRA1",
                    role="customer",
                    ip_address="203.0.113.0/31",
                ),
                "ip_address": _node({"address": _v("203.0.113.0/31"), "ip_namespace": internet_ns}),
            }
        }
    )
    peering = _make_bgp_peering(
        device_name=device_name,
        device_ip="203.0.113.0/31",
        device_asn=65001,
        remote_name="EXT-LUMEN-FRA1",
        remote_ip="203.0.113.1/31",
        remote_asn=3356,
        session_type="EBGP",
        ttl=1,
        local_iface_name="Ethernet1/14",
        remote_iface_name="Ethernet1/1",
        password="isp-s3cr3t",
    )
    for iface_edge in peering["capabilities"]["edges"]:
        node = iface_edge["node"]
        node["ip_address"]["node"]["ip_namespace"] = internet_ns
    # The INTERNET namespace's public space: aggregate + outbound filter.
    data["IpamPrefix"] = _edges(
        [{"prefix": _v("192.0.2.0/24"), "ip_namespace": _node({"namespace_type": _v("internet")})}]
    )
    device_node["capabilities"]["edges"].append(
        {
            "node": {
                "__typename": "ManagedBGP",
                "name": _v("bgp-transit"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(65001)}),
                "router_id": _node({"address": _v("10.0.0.1/32")}),
                "peerings": _edges([peering]),
            }
        }
    )
    return data


def _cidr_blocks(*blocks: tuple[str, str]) -> dict:
    """IpamPrefix edges from (prefix, namespace name) pairs: VPC cidr_blocks, office lan_prefixes."""
    return _edges([{"prefix": _v(prefix), "ip_namespace": _node({"name": _v(ns)})} for prefix, ns in blocks])


def _owner_node(typename: str, org_id: str, label: str) -> dict:
    """An ``owner { node { __typename org_id { value } display_label } }`` selection."""
    return _node({"__typename": typename, "org_id": _v(org_id), "display_label": label})


def _customer(org_id: str) -> dict:
    """The owner node of customer ``org_id`` (OrganizationCustomer)."""
    return _owner_node("OrganizationCustomer", org_id, f"{org_id} Customer")


_PROVIDER = _owner_node("OrganizationProvider", "P027", "P027 Provider")


def _regular_handoff(
    *,
    device_name: str,
    namespace: dict,
    iface: str,
    local_ip: str,
    remote_ip: str,
    remote: str,
    remote_iface: str,
    remote_asn: int,
    name: str,
    bfd: bool,
    far_role: str | None,
    far_kind: str | None = None,
) -> tuple[dict, dict]:
    """A handoff sub-interface in a tenant namespace and its ``regular`` eBGP peering, as add_virtual_circuit writes it.

    ``far_role`` is the process_role of the far end's BGP process in the peering (None: unset), ``far_kind`` the
    ``__typename`` of the far end's device (None: not selected, as in the older fixtures). Returns the
    (interface edge, peering) pair.
    """
    interface = {
        "node": {
            **_make_interface(
                name=iface,
                device_name=device_name,
                description=f"{namespace['node']['name']['value']} handoff to {remote}",
                role="uplink",
                ip_address=local_ip,
                typename="DcimVirtualInterface",
            ),
            "ip_address": _node({"address": _v(local_ip), "ip_namespace": namespace}),
        }
    }
    peering = _make_bgp_peering(
        device_name=device_name,
        device_ip=local_ip,
        device_asn=65001,
        remote_name=remote,
        remote_ip=remote_ip,
        remote_asn=remote_asn,
        session_type="EBGP",
        ttl=1,
        bfd=bfd,
        local_iface_name=iface,
        remote_iface_name=remote_iface,
        send_community=False,
        send_extended_community=False,
        password=f"{name.lower()}-key",
    )
    peering["name"] = _v(name)
    peering["peering_role"] = _v("regular")
    peering["address_families"] = _af_edges(("ipv4", "unicast"))
    if far_role:
        peering["bgp_processes"]["edges"][1]["node"]["process_role"] = _v(far_role)
    if far_kind:
        peering["capabilities"]["edges"][1]["node"]["device"]["node"]["__typename"] = far_kind
    for iface_edge in peering["capabilities"]["edges"]:
        iface_edge["node"]["ip_address"]["node"]["ip_namespace"] = namespace
    return interface, peering


def build_edge_unfiltered_appliance_data(*, device_name: str) -> dict:
    """A provider-hosted Network Edge router (CloudNetworkAppliance, cisco_xe, role edge) with two PROD handoffs.

    Its base ports are DcimVirtualInterface NICs (the appliance has no hardware and no cable). The platform has
    no external-policy renderer, and a hosted appliance must still get its artifact, so the
    external session renders with an UNFILTERED banner comment:
      - Ethernet1/1.1100 PROD: EXT-AWS-DX-FR2 (DcimPhysicalDevice, process_role unset): external, banner.
      - Ethernet1/2.1200 PROD: eg-fr201 (DcimPhysicalDevice) whose BGP process in the peering has
        process_role overlay: one of our own devices, internal, no banner.
    """
    data = build_device_data(
        device_name=device_name, role="edge", platform="cisco_xe", scenario="ebgp_ebgp", include_segments=False
    )
    device_node = data["DcimDevice"]["edges"][0]["node"]
    device_node["__typename"] = "CloudNetworkAppliance"
    # A hosted appliance has no hardware: its base ports are virtual NICs (no cable, no interface type, no MTU).
    for edge in device_node["interfaces"]["edges"]:
        port = edge["node"]
        if port["__typename"] == "DcimPhysicalInterface":
            port["__typename"] = "DcimVirtualInterface"
            for physical_only in ("cable", "interface_type", "mtu", "segments"):
                port.pop(physical_only, None)
            port["inline_segment"] = _node(None)
    prod = _node({"name": _v("PROD"), "l3_vni": _v(60000), "namespace_type": _v("prod")})
    peerings = []
    for iface, local_ip, remote_ip, remote, remote_iface, remote_asn, name, far_role in (
        ("Ethernet1/1.1100", "10.255.9.0/31", "10.255.9.1/31", "EXT-AWS-DX-FR2", "et-0/0/0.1100", 64512,
         "HANDOFF-AMNE-AWS-EUC1-PROD", None),
        ("Ethernet1/2.1200", "10.255.8.1/31", "10.255.8.0/31", "eg-fr201", "Ethernet1/12.1200", 4200000100,
         "HANDOFF-FR2-AMNE-PROD", "overlay"),
    ):  # fmt: skip
        interface, peering = _regular_handoff(
            device_name=device_name,
            namespace=prod,
            iface=iface,
            local_ip=local_ip,
            remote_ip=remote_ip,
            remote=remote,
            remote_iface=remote_iface,
            remote_asn=remote_asn,
            name=name,
            bfd=False,
            far_role=far_role,
            far_kind="DcimPhysicalDevice",
        )
        device_node["interfaces"]["edges"].append(interface)
        peerings.append(peering)
    device_node["capabilities"]["edges"].append(
        {
            "node": {
                "__typename": "ManagedBGP",
                "name": _v("bgp-overlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(65001)}),
                "router_id": _node({"address": _v("10.0.0.1/32")}),
                "peerings": _edges(peerings),
            }
        }
    )
    data["IpamPrefix"] = _edges([])
    return data


def build_edge_external_vrf_peers_data(*, device_name: str, platform: str) -> dict:
    """A colocation edge with external eBGP peers inside the tenant VRFs.

    ebgp_ebgp edge (no VTEP) plus five peering_role regular sessions, as add_virtual_circuit writes a
    handoff (send_community and send_extended_community both False), on sub-interfaces:
      - Ethernet1/13.1000 PROD: the AWS transit VIF (vlan 1000). Its hybrid attachment sits on
        rt-shared, so only the VPCs propagating there are accepted: 10.30.0.0/16 (account owner C003)
        and 10.32.0.0/16 (C015). 10.150.0.0/16 propagates to rt-isolated only, and 10.33.0.0/16 sits
        in a provider-owned account: neither is accepted. Owners C003 and C015.
      - Ethernet1/13.1001 NON-PROD: a private VIF whose VPC (owner C003) lists a NON-PROD and a PROD
        block; only the NON-PROD one is accepted.
      - Ethernet1/13.1900 PROD: the SD-WAN gateway handoff. It accepts what the offices behind
        EQX-FR2-SDWAN-GW1 register: C002's PROD lan_prefixes (10.210.2.0/24), C003's (10.210.3.0/24)
        and C005's hosted office-lan segment (10.176.5.0/24). C003 therefore has a VPC block on the hub VIF AND an office LAN behind GW1. A
        NON-PROD LAN, an office owned by a provider, an office registering nothing, an office behind another
        gateway and an office without a gateway are not accepted.
      - Ethernet1/13.1300 PROD: EXT-MEGAPORT-MCR1, no far end registers a prefix, so the session has
        no owner: deny-all both ways, with a comment.
      - Ethernet1/12.1200 PROD: eg-am-ne01, a CloudNetworkAppliance (provider-hosted Network Edge) whose
        BGP process in the peering has process_role overlay. An appliance is never ours, so the hop is
        external; nothing is registered behind it: deny-all both ways, with a comment.
    Out (the same-owner exchange, utils/ownership.py): per session, for each of its owners, that owner's
    on-prem segments in the namespace plus what it registered behind another session of the namespace
    (C003's office LAN goes to the hub VIF, C003's VPC block to GW1), minus the session's own IN (C005's
    office-hosted segment is registered behind GW1, so it is never announced back to it). Never another
    owner's prefix: C015 sits on the hub VIF only, so GW1 never learns of it. Segments of C003, C015,
    C002 and C005 in PROD and NON-PROD; C001's segment is behind no session and never announced, a
    segment without a customer owner, a PROD container, a MANAGEMENT segment and
    a segment without a gateway are never announced. A VIF on another device with the same VLAN is
    ignored.
    """
    data = build_device_data(
        device_name=device_name, role="edge", platform=platform, scenario="ebgp_ebgp", include_segments=False
    )
    device_node = data["DcimDevice"]["edges"][0]["node"]
    interfaces = device_node["interfaces"]["edges"]
    namespaces = {
        "PROD": _node({"name": _v("PROD"), "l3_vni": _v(60000), "namespace_type": _v("prod")}),
        "NON-PROD": _node({"name": _v("NON-PROD"), "l3_vni": _v(60001), "namespace_type": _v("non_prod")}),
    }
    handoffs = [
        # (sub-interface, namespace, local ip, remote ip, remote device, remote iface, remote asn, peering, bfd,
        #  process_role of the far end's BGP process, kind of the far end's device)
        ("Ethernet1/13.1000", "PROD", "10.255.5.5/30", "10.255.5.6/30", "EXT-AWS-DX-FR2", "et-0/0/0.1000", 64512,
         "VRF-PROD-EQXFR2-AWS-EUC1", True, "regular", None),
        ("Ethernet1/13.1001", "NON-PROD", "10.255.6.5/30", "10.255.6.6/30", "EXT-AWS-DX-FR2", "et-0/0/0.1001", 64512,
         "VRF-NONPROD-EQXFR2-AWS-EUC1", True, "underlay", None),
        ("Ethernet1/13.1900", "PROD", "10.255.5.0/31", "10.255.5.1/31", "EQX-FR2-SDWAN-GW1", "eth0.1900", 65028,
         "VRF-PROD-EQXFR2-SDWAN", False, None, None),
        ("Ethernet1/13.1300", "PROD", "10.255.7.0/31", "10.255.7.1/31", "EXT-MEGAPORT-MCR1", "ae1.1300", 64520,
         "VRF-PROD-EQXFR2-MEGAPORT", False, None, None),
        ("Ethernet1/12.1200", "PROD", "10.255.8.0/31", "10.255.8.1/31", "eg-am-ne01", "Ethernet1/12.1200", 4200000200,
         "HANDOFF-FR2-AMNE-PROD", False, "overlay", "CloudNetworkAppliance"),
    ]  # fmt: skip
    peerings = []
    for iface, ns, local_ip, remote_ip, remote, remote_iface, remote_asn, name, bfd, far_role, far_kind in handoffs:
        interface, peering = _regular_handoff(
            device_name=device_name,
            namespace=namespaces[ns],
            iface=iface,
            local_ip=local_ip,
            remote_ip=remote_ip,
            remote=remote,
            remote_iface=remote_iface,
            remote_asn=remote_asn,
            name=name,
            bfd=bfd,
            far_role=far_role,
            far_kind=far_kind,
        )
        interfaces.append(interface)
        peerings.append(peering)
    device_node["capabilities"]["edges"].append(
        {
            "node": {
                "__typename": "ManagedBGP",
                "name": _v("bgp-overlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(65001)}),
                "router_id": _node({"address": _v("10.0.0.1/32")}),
                "peerings": _edges(peerings),
            }
        }
    )

    def segment_prefix(
        prefix: str,
        ns_name: str,
        ns_type: str,
        *,
        owner: dict | None,
        member_type: str = "address",
    ) -> dict:
        return {
            "owner": owner if owner is not None else _node(None),
            "gateway": _node(
                {
                    "ip_prefix": _node(
                        {
                            "prefix": _v(prefix),
                            "member_type": _v(member_type),
                            "ip_namespace": _node({"name": _v(ns_name), "namespace_type": _v(ns_type)}),
                        }
                    )
                }
            ),
        }

    data["IpamPrefix"] = _edges([])
    data["ManagedNetworkSegment"] = _edges(
        [
            segment_prefix("10.5.20.0/24", "PROD", "prod", owner=_customer("C003")),
            segment_prefix("10.5.10.0/24", "PROD", "prod", owner=_customer("C003")),
            segment_prefix("10.5.0.0/16", "PROD", "prod", owner=_customer("C003"), member_type="prefix"),
            segment_prefix("10.5.77.0/24", "PROD", "prod", owner=None),
            segment_prefix("10.7.10.0/24", "PROD", "prod", owner=_customer("C015")),
            segment_prefix("10.8.2.0/24", "PROD", "prod", owner=_customer("C002")),
            segment_prefix("10.176.5.0/24", "PROD", "prod", owner=_customer("C005")),
            segment_prefix("10.9.1.0/24", "PROD", "prod", owner=_customer("C001")),
            segment_prefix("10.9.9.0/24", "PROD", "prod", owner=_customer("C009")),
            segment_prefix("10.6.10.0/24", "NON-PROD", "non_prod", owner=_customer("C003")),
            segment_prefix("10.6.20.0/24", "NON-PROD", "non_prod", owner=_customer("C015")),
            segment_prefix("10.6.30.0/24", "NON-PROD", "non_prod", owner=_customer("C002")),
            segment_prefix("10.250.0.0/24", "MANAGEMENT", "management", owner=_customer("C003")),
            {"owner": _customer("C003"), "gateway": _node(None)},
        ]
    )
    on_this_edge = _node({"physical_devices": _edges([{"name": _v(device_name)}])})

    def vpc(owner: dict | None, *blocks: tuple[str, str]) -> dict:
        """A cloud virtual network: its cidr_blocks and the owner of its account."""
        return _node(
            {
                "cidr_blocks": _cidr_blocks(*blocks),
                "account": _node({"owner": owner if owner is not None else _node(None)}),
            }
        )

    def vpc_attachment(table: str, virtual_network: dict) -> dict:
        return {
            "attachment_type": _v("vpc"),
            "propagates_to_route_tables": _edges([{"id": table}]),
            "virtual_network": virtual_network,
        }

    data["CloudVirtualInterface"] = _edges(
        [
            {
                "name": _v("vif-transit-hub-euc1"),
                "vlan_id": _v(1000),
                "direct_connect": on_this_edge,
                "virtual_network": _node(None),
                "transit_gateway_attachment": _node(
                    {
                        "associated_route_table": _node({"id": "rt-shared"}),
                        "transit_gateway": _node(
                            {
                                "attachments": _edges(
                                    [
                                        {
                                            "attachment_type": _v("hybrid"),
                                            "propagates_to_route_tables": _edges([{"id": "rt-shared"}]),
                                            "virtual_network": _node(None),
                                        },
                                        vpc_attachment("rt-shared", vpc(_customer("C003"), ("10.30.0.0/16", "PROD"))),
                                        vpc_attachment("rt-shared", vpc(_customer("C015"), ("10.32.0.0/16", "PROD"))),
                                        vpc_attachment("rt-shared", vpc(_PROVIDER, ("10.33.0.0/16", "PROD"))),
                                        vpc_attachment(
                                            "rt-isolated", vpc(_customer("C003"), ("10.150.0.0/16", "PROD"))
                                        ),
                                    ]
                                )
                            }
                        ),
                    }
                ),
            },
            {
                "name": _v("vif-private-dev-euc1"),
                "vlan_id": _v(1001),
                "direct_connect": on_this_edge,
                "virtual_network": vpc(_customer("C003"), ("10.60.0.0/16", "NON-PROD"), ("10.31.0.0/16", "PROD")),
                "transit_gateway_attachment": _node(None),
            },
            {
                "name": _v("vif-elsewhere"),
                "vlan_id": _v(1000),
                "direct_connect": _node({"physical_devices": _edges([{"name": _v("dc1-edge-02")}])}),
                "virtual_network": vpc(_customer("C003"), ("10.99.0.0/16", "PROD")),
                "transit_gateway_attachment": _node(None),
            },
        ]
    )

    def office(
        gateway: str | None,
        owner: dict,
        *lans: tuple[str, str],
        hosted: tuple[tuple[str, str, dict], ...] = (),
    ) -> dict:
        """A footprint's gateway and owner, its lan_prefixes and (``hosted``) the segments it hosts itself.

        ``hosted`` entries are (gateway prefix, namespace, segment owner).
        """
        return {
            "owner": owner,
            "sdwan_gateway": _node({"name": _v(gateway)} if gateway else None),
            "lan_prefixes": _cidr_blocks(*lans),
            "segment_deployments": _edges(
                [
                    {
                        "segment": _node(
                            {
                                "owner": segment_owner,
                                "gateway": _node(
                                    {
                                        "ip_prefix": _node(
                                            {"prefix": _v(prefix), "ip_namespace": _node({"name": _v(namespace)})}
                                        )
                                    }
                                ),
                            }
                        )
                    }
                    for prefix, namespace, segment_owner in hosted
                ]
            ),
        }

    gw1 = "EQX-FR2-SDWAN-GW1"
    data["TopologyCustomerOffice"] = _edges(
        [
            office(
                gw1,
                _customer("C005"),
                hosted=(("10.176.5.0/24", "PROD", _customer("C005")),),
            ),
            office(gw1, _customer("C002"), ("10.210.2.0/24", "PROD"), ("10.211.2.0/24", "NON-PROD")),
            office(gw1, _customer("C003"), ("10.210.3.0/24", "PROD")),
            office(gw1, _customer("C007")),
            office(gw1, _PROVIDER, ("10.210.7.0/24", "PROD")),
            office("EQX-AM5-SDWAN-GW1", _customer("C002"), ("10.210.9.0/24", "PROD")),
            office(None, _customer("C002"), ("10.210.15.0/24", "PROD")),
        ]
    )

    def tunnel(owner: dict, prefixes: tuple[tuple[str, str], ...], *ends: str) -> dict:
        """A tunnel circuit: its owner, the (prefix, namespace) pairs it registers and the device of each end port."""
        return {
            "owner": owner,
            "registered_prefixes": _cidr_blocks(*prefixes),
            "capabilities": _edges([{"device": _node({"name": _v(end)})} for end in ends]),
        }

    # The tunnels registering space behind a gateway: C001's partner range behind GW1 (accepted from GW1's
    # session, announced to C001's other sessions), a provider-owned one (never accepted), a NON-PROD one
    # (not the PROD session's) and the office's own inventory tunnel, which registers nothing.
    data["TopologySessionCircuit"] = _edges(
        [
            tunnel(_customer("C001"), (("198.18.0.0/24", "PROD"),), gw1, "EXT-ACME-EDGE1"),
            tunnel(_PROVIDER, (("198.19.0.0/24", "PROD"),), gw1, "EXT-PARTNER-EDGE1"),
            tunnel(_customer("C001"), (("198.20.0.0/24", "NON-PROD"),), gw1, "EXT-PARTNER-EDGE2"),
            tunnel(_customer("C002"), (), gw1, "C002-OFFICE-EDGE1"),
        ]
    )
    return data


def build_edge_bgw_pair_data(*, device_name: str, platform: str) -> dict:
    """A colocation metro edge that is one of two BGWs sharing a site VIP.

    BGW edge (Loopback1 IPv6 VTEP, Loopback100 multisite-vip) with two DCI dark
    fibres (Ethernet1/10 and 1/11, MTU 9214), each carrying two sessions with one
    per-circuit key: the underlay (peering_role dci-underlay, ttl 1, bfd, on the
    port /127) and the EVPN session (peering_role dci, ttl 2 multihop, no bfd,
    Loopback1 to the remote loopback), and an interlink
    (Ethernet1/8, role interlink) cabled to its sibling edge, with a FABRIC iBGP
    session (ttl 1, ipv6 unicast + l2vpn evpn) in the shared site AS on that /127.
    Ethernet1/13 and 1/14 (uplinks to an ISP / cross-connect) carry nothing.
    """
    data = build_device_data(
        device_name=device_name, role="edge", platform=platform, scenario="ebgp_ebgp", include_segments=False
    )
    device_node = data["DcimDevice"]["edges"][0]["node"]
    interfaces = device_node["interfaces"]["edges"]
    # The colocation edge's VTEP is a dedicated IPv6 loopback.
    interfaces.append(
        {
            "node": _make_interface(
                name="Loopback1",
                device_name=device_name,
                description="VTEP",
                role="loopback-vtep",
                ip_address="2001:db8:100::1/128",
                typename="DcimVirtualInterface",
            )
        }
    )
    interfaces.append(
        {
            "node": _make_interface(
                name="Loopback100",
                device_name=device_name,
                description="Multi-Site VIP",
                role="multisite-vip",
                ip_address="2001:db8:100::f1/128",
                typename="DcimVirtualInterface",
            )
        }
    )
    # One segment on a firewall-role port gives the BGW its VXLAN block (an edge
    # without a stretched segment or an INTERNET session renders none).
    seg = _make_inline_segment_node(row_device_id=device_node["id"])
    interfaces.append(
        {
            "node": _make_interface(
                name="Ethernet1/15",
                device_name=device_name,
                description="to dc1-fw-01",
                role="firewall",
                segments=[seg],
            )
        }
    )
    device_node["deployment"]["node"]["segment_deployments"] = _edges(
        [_deployment_record(seg, _INLINE_SEGMENT_KWARGS["vni"])]
    )
    sibling = "dc1-edge-02"
    interfaces.append(
        {
            "node": _make_interface(
                name="Ethernet1/8",
                device_name=device_name,
                description=f"to {sibling} Ethernet1/8",
                role="interlink",
                ip_address="2001:db8:100:ff::/127",
                remote_name="Ethernet1/8",
                remote_ip="2001:db8:100:ff::1/127",
                remote_device=sibling,
            )
        }
    )
    dci_peerings = []
    for index, (port, remote_ip, local_ip) in enumerate(
        [
            ("Ethernet1/10", "2001:db8:dc10::1/127", "2001:db8:dc10::/127"),
            ("Ethernet1/11", "2001:db8:dc11::1/127", "2001:db8:dc11::/127"),
        ]
    ):
        remote = f"dc{10 + index}-border-leaf-01"
        dci_port = _make_interface(
            name=port,
            device_name=device_name,
            description=f"to {remote}",
            role="dci",
            ip_address=local_ip,
            remote_name="Ethernet1/35",
            remote_ip=remote_ip,
            remote_device=remote,
        )
        # The generator sets the routed-port MTU on the physical DCI ports.
        dci_port["mtu"] = _v(9214)
        interfaces.append({"node": dci_port})
        # One key per circuit, on both of its sessions.
        key = f"dci-s3cr3t-{index}"
        # Underlay: eBGP unicast over the physical port, bfd, capped.
        underlay = _make_bgp_peering(
            device_name=device_name,
            device_ip=local_ip,
            device_asn=65001,
            remote_name=remote,
            remote_ip=remote_ip,
            remote_asn=65100 + index,
            session_type="EBGP",
            ttl=1,
            local_iface_name=port,
            remote_iface_name="Ethernet1/35",
            password=key,
        )
        underlay["peering_role"] = _v("dci-underlay")
        underlay["address_families"] = _af_edges(("ipv6", "unicast"))
        # EVPN: eBGP multihop between the loopbacks, no bfd, extended communities.
        evpn = _make_bgp_peering(
            device_name=device_name,
            device_ip="2001:db8:100::1/128",
            device_asn=65001,
            remote_name=remote,
            remote_ip=f"2001:db8:dc1{index}:ffff::1/128",
            remote_asn=65100 + index,
            session_type="EBGP_MULTIHOP",
            ttl=2,
            bfd=False,
            send_extended_community=True,
            local_iface_name="Loopback1",
            remote_iface_name="Loopback0",
            password=key,
        )
        evpn["peering_role"] = _v("dci")
        evpn["address_families"] = _af_edges(("l2vpn", "evpn"))
        dci_peerings.extend([underlay, evpn])
    fabric = _make_bgp_peering(
        device_name=device_name,
        device_ip="2001:db8:100:ff::/127",
        device_asn=65001,
        remote_name=sibling,
        remote_ip="2001:db8:100:ff::1/127",
        remote_asn=65001,
        session_type="IBGP",
        ttl=1,
        local_iface_name="Ethernet1/8",
        remote_iface_name="Ethernet1/8",
        password="fabric-s3cr3t",
    )
    fabric["peering_role"] = _v("fabric")
    fabric["address_families"] = _af_edges(("ipv6", "unicast"), ("l2vpn", "evpn"))
    device_node["capabilities"]["edges"].append(
        {
            "node": {
                "__typename": "ManagedBGP",
                "name": _v("bgp-overlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(65001)}),
                "router_id": _node({"address": _v("10.0.0.1/32")}),
                "peerings": _edges([*dci_peerings, fabric]),
            }
        }
    )
    _enforce_route_protection(data, device_name=device_name, sibling=sibling)
    return data


def _vip_interface(device: str, role: str, vip: str, loopback: str) -> dict:
    """One ``DcimVirtualInterface(role: multisite-vip)`` root node with its device's loopback (bgp_facts reads it)."""
    return {
        "ip_address": _node({"address": _v(vip)}),
        "device": _node(
            {
                "name": _v(device),
                "role": _v(role),
                "interfaces": _edges([{"role": _v("loopback-vtep"), "ip_address": _node({"address": _v(loopback)})}]),
            }
        ),
    }


def _enforce_route_protection(data: dict, *, device_name: str, sibling: str) -> None:
    """Turn the route protection on for the BGW pair fixture: the metro enforces and names its loopback supernet.

    The remote border gateways (one per DC) and the sibling edge carry their VIPs on the ``DcimVirtualInterface``
    root, which is what a DCI or fabric session derives its far-end addresses from (transforms/helpers/bgp_facts.py).
    """
    deployment = data["DcimDevice"]["edges"][0]["node"]["deployment"]["node"]
    deployment["routing_filter_enforce"] = _v(True)
    deployment["loopback_pool"] = _node({"resources": _edges([{"prefix": _v("2001:db8:100::/120")}])})
    # The edge is a border gateway in the INTERNET instance (its default exit) and a member of the tenant one.
    add_environment_roots(data, tenant_members=[device_name], internet_members=[device_name])
    node = data["DcimDevice"]["edges"][0]["node"]
    memberships = [
        {"namespace": {"name": TENANT_NAMESPACE, "l3_vni": 50001, "namespace_type": "prod"}},
        {"namespace": {"name": "INTERNET", "l3_vni": 60002, "namespace_type": "internet"}},
    ]
    existing = {
        edge["node"]["namespace"]["node"]["name"]["value"]
        for edge in (node.get("shared_capabilities") or {}).get("edges") or []
    }
    node.setdefault("shared_capabilities", {"edges": []})["edges"].extend(
        {"node": _vrf_node(m["namespace"])} for m in memberships if m["namespace"]["name"] not in existing
    )
    data["DcimVirtualInterface"] = _edges(
        [
            _vip_interface(device_name, "edge", "2001:db8:100::f1/128", "2001:db8:100::1/128"),
            _vip_interface(sibling, "edge", "2001:db8:100::f1/128", "2001:db8:100::2/128"),
            _vip_interface("dc10-border-leaf-01", "border-leaf", "2001:db8:dc10::f10/128", "2001:db8:dc10::1/128"),
            _vip_interface("dc11-border-leaf-01", "border-leaf", "2001:db8:dc11::f11/128", "2001:db8:dc11::1/128"),
        ]
    )


def write_fixtures() -> tuple[int, int]:
    """Write the edge and border-leaf BGW fixtures: stretched segments, INTERNET handoff, external peers, edge pair."""
    fixtures = (
        (
            "border-leaf BGW stretched segments",
            BorderLeaf,
            "border_leaf_cisco_nxos_bgw_stretched",
            # DC border-leaf BGW: stretched segments without a customer port render no tenant policy.
            build_border_leaf_bgw_stretched_data(device_name="dc1-border-leaf-01", platform="cisco_nxos"),
        ),
        (
            "INTERNET BGW edge",
            Edge,
            "edge_cisco_nxos_internet_bgw",
            # Upstream INTERNET handoff on a BGW edge: L3 VNI 60002, global RT, transit filters.
            build_edge_internet_bgw_data(device_name="dc1-edge-01", platform="cisco_nxos"),
        ),
        (
            "external tenant-VRF peers edge",
            Edge,
            "edge_cisco_nxos_external_vrf_peers",
            # External eBGP peers in the tenant VRFs (cloud VIFs, SD-WAN gateway): per-peer
            # inbound prefix-list of the far end's registered prefixes, per-VRF on-prem outbound.
            build_edge_external_vrf_peers_data(device_name="dc1-edge-01", platform="cisco_nxos"),
        ),
        (
            "unfiltered hosted appliance edge",
            Edge,
            "edge_cisco_xe_unfiltered_appliance",
            # A hosted appliance (cisco_xe) has no external-policy renderer: its external session renders with a banner.
            build_edge_unfiltered_appliance_data(device_name="eg-am-ne02"),
        ),
        (
            "metro edge pair BGW",
            Edge,
            "edge_cisco_nxos_bgw_pair",
            # Metro edge pair: fabric-tracking on the interlink, FABRIC-PEERS iBGP session.
            build_edge_bgw_pair_data(device_name="dc1-edge-01", platform="cisco_nxos"),
        ),
    )
    generated = errors = 0
    for title, transform_cls, dir_name, data in fixtures:
        print(f"\nGenerating {title} fixture:")
        g, e = _write_data_fixture(transform_cls, dir_name, data)
        generated += g
        errors += e
    return generated, errors
