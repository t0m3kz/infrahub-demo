"""Fabric-switch fixtures: the device x platform x scenario matrix, segment ACLs, BGP hardening, inline segments."""

from __future__ import annotations

from typing import Any

from tests.smoke.fixture_builders.common import (
    _edges,
    _make_bgp_peering,
    _make_interface,
    _make_policy_rule,
    _make_security_policy,
    _make_segment_deployment,
    _make_segment_node,
    _node,
    _v,
    _write_data_fixture,
    write_data_fixtures,
)
from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.border_spine import BorderSpine
from transforms.config.edge import Edge
from transforms.config.l2_leaf import L2Leaf
from transforms.config.leaf import Leaf
from transforms.config.spine import Spine
from transforms.config.super_spine import SuperSpine
from transforms.config.tor import ToR

# ============================================================================
# Scenario builders
# ============================================================================
# Roles that terminate customer segments, so their fixtures need segments attached.
# Mirrors utils/fabric_roles.py's VTEP_ROLES (without edge) plus l2-leaf (pure L2, no VNI) and an office's access-switch.
SEGMENT_ROLES = ("leaf", "border-leaf", "border-spine", "tor", "l2-leaf", "access-leaf", "access-switch")

# Fabric-wide EVPN route-target administrative ASN
# (TopologySegmentHosting.evpn_rt_as). Deliberately NOT any device's own ASN:
# every VTEP in the fabric must derive the same route-target or imports never
# match, so the golden output must show one constant regardless of the device's
# local ASN. See transforms/helpers/vxlan.py's rt_asn.
FABRIC_RT_ASN = 65000


def build_device_data(
    *,
    device_name: str,
    role: str,
    platform: str,
    scenario: str = "ebgp_ibgp",
    include_segments: bool = False,
    include_acls: bool = False,
    isolation_mode: str | None = None,
    security_fields: bool = False,
    vlan_segment_vrf: bool = False,
) -> dict:
    """Build a complete raw GraphQL response for a device config query.

    ``vlan_segment_vrf`` puts the VLAN-only seg-200 in VRF_A with an L3 VNI instead of the default
    namespace: an office LISP fabric refuses a gateway outside a VRF with an L3 instance-id.

    Scenarios:
        ebgp_ebgp: eBGP underlay (TTL=1) + eBGP overlay (TTL=2), per-device ASN
                   - Underlay and overlay both use the device's own ASN (65001),
                     so get_bgp_profile merges them into ONE `router bgp` stanza
                   - This is the schema default (9 of 10 demo topologies) and the
                     only scenario that produces an EVPN-OVERLAY peer group, so it
                     is the only one exercising next-hop-unchanged / retain
                     route-target all on the relaying tier
        ebgp_ibgp: eBGP underlay (TTL=1) + iBGP overlay (TTL=2)
                   - Underlay: per-device ASN (65001), remote spines have different ASNs (65100, 65101)
                   - Overlay: shared iBGP ASN (65000) for all devices
        ospf_ibgp: OSPF underlay + iBGP overlay (TTL=2)
                   - Shared ASN (65000) for iBGP overlay
                   - OSPF on P2P interfaces
    """
    router_id = "10.0.0.1/32"

    # Neighbor topology: 2 spines with different loopback and P2P IPs
    spine1_loopback = "10.0.0.100/32"
    spine2_loopback = "10.0.0.101/32"
    # P2P links: Ethernet1 → spine-01, Ethernet2 → spine-02
    local_p2p_1 = "10.1.0.1/31"
    remote_p2p_1 = "10.1.0.0/31"
    local_p2p_2 = "10.1.0.3/31"
    remote_p2p_2 = "10.1.0.2/31"

    device_capabilities: list[dict] = []
    use_ospf_on_interfaces = False

    if scenario == "ebgp_ebgp":
        # eBGP underlay + eBGP overlay, both on the device's OWN ASN. The
        # generator really does create two ManagedBGP processes here
        # (`-bgp-underlay` and `-bgp-overlay`, see generators/helpers/routing.py
        # _plan_overlay_processes) that share one per-device ASN, and
        # get_bgp_profile merges same-ASN processes into a single stanza. Keep
        # both processes in the fixture so that merge stays covered.
        device_asn = 65001
        spine1_asn = 65100
        spine2_asn = 65101

        underlay_peering_1 = _make_bgp_peering(
            device_name=device_name,
            device_ip=local_p2p_1,
            device_asn=device_asn,
            remote_name="spine-01",
            remote_ip=remote_p2p_1,
            remote_asn=spine1_asn,
            session_type="EBGP",
            ttl=1,
            local_iface_name="Ethernet1",
            remote_iface_name="Ethernet1/1",
            maximum_routes=1000 if security_fields else None,
            local_pref=200 if security_fields else None,
            med=50 if security_fields else None,
            send_extended_community=security_fields,
            remove_private_as=security_fields,
            password="underlay-s3cr3t" if security_fields else None,
        )
        underlay_peering_2 = _make_bgp_peering(
            device_name=device_name,
            device_ip=local_p2p_2,
            device_asn=device_asn,
            remote_name="spine-02",
            remote_ip=remote_p2p_2,
            remote_asn=spine2_asn,
            session_type="EBGP",
            ttl=1,
            local_iface_name="Ethernet2",
            remote_iface_name="Ethernet1/2",
        )
        device_capabilities.append(
            {
                "__typename": "ManagedBGP",
                "name": _v("bgp-underlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(device_asn)}),
                "router_id": _node({"address": _v(router_id)}),
                "peerings": _edges([underlay_peering_1, underlay_peering_2]),
            }
        )

        # Overlay eBGP peerings: multihop over loopbacks, remote ASN is the
        # spine's own ASN — every VTEP a different AS, which is exactly why the
        # RT cannot be `auto` and why the spine needs next-hop-unchanged.
        overlay_peering_1 = _make_bgp_peering(
            device_name=device_name,
            device_ip=router_id,
            device_asn=device_asn,
            remote_name="spine-01",
            remote_ip=spine1_loopback,
            remote_asn=spine1_asn,
            session_type="EBGP",
            ttl=2,
            send_extended_community=True,
        )
        overlay_peering_2 = _make_bgp_peering(
            device_name=device_name,
            device_ip=router_id,
            device_asn=device_asn,
            remote_name="spine-02",
            remote_ip=spine2_loopback,
            remote_asn=spine2_asn,
            session_type="EBGP",
            ttl=2,
            send_extended_community=True,
        )
        device_capabilities.append(
            {
                "__typename": "ManagedBGP",
                "name": _v("bgp-overlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(device_asn)}),
                "router_id": _node({"address": _v(router_id)}),
                "peerings": _edges([overlay_peering_1, overlay_peering_2]),
            }
        )

    elif scenario == "ebgp_ibgp":
        # eBGP underlay + iBGP overlay with separate ASNs
        underlay_asn = 65001  # per-device underlay ASN
        overlay_asn = 65000  # shared iBGP overlay ASN
        spine1_asn = 65100
        spine2_asn = 65101

        # Underlay eBGP peerings (TTL=1, P2P link IPs)
        underlay_peering_1 = _make_bgp_peering(
            device_name=device_name,
            device_ip=local_p2p_1,
            device_asn=underlay_asn,
            remote_name="spine-01",
            remote_ip=remote_p2p_1,
            remote_asn=spine1_asn,
            session_type="EBGP",
            ttl=1,
            local_iface_name="Ethernet1",
            remote_iface_name="Ethernet1/1",
            maximum_routes=1000 if security_fields else None,
            local_pref=200 if security_fields else None,
            med=50 if security_fields else None,
            send_extended_community=security_fields,
            remove_private_as=security_fields,
            password="underlay-s3cr3t" if security_fields else None,
        )
        underlay_peering_2 = _make_bgp_peering(
            device_name=device_name,
            device_ip=local_p2p_2,
            device_asn=underlay_asn,
            remote_name="spine-02",
            remote_ip=remote_p2p_2,
            remote_asn=spine2_asn,
            session_type="EBGP",
            ttl=1,
            local_iface_name="Ethernet2",
            remote_iface_name="Ethernet1/2",
        )

        # Underlay BGP process (per-device ASN)
        device_capabilities.append(
            {
                "__typename": "ManagedBGP",
                "name": _v("bgp-underlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(underlay_asn)}),
                "router_id": _node({"address": _v(router_id)}),
                "peerings": _edges([underlay_peering_1, underlay_peering_2]),
            }
        )

        # Overlay iBGP peerings (TTL=2, loopback IPs)
        overlay_peering_1 = _make_bgp_peering(
            device_name=device_name,
            device_ip=router_id,
            device_asn=overlay_asn,
            remote_name="spine-01",
            remote_ip=spine1_loopback,
            remote_asn=overlay_asn,
            session_type="IBGP",
            ttl=2,
            route_reflector_client=True,
        )
        overlay_peering_2 = _make_bgp_peering(
            device_name=device_name,
            device_ip=router_id,
            device_asn=overlay_asn,
            remote_name="spine-02",
            remote_ip=spine2_loopback,
            remote_asn=overlay_asn,
            session_type="IBGP",
            ttl=2,
            route_reflector_client=True,
        )

        # Overlay BGP process (shared iBGP ASN)
        device_capabilities.append(
            {
                "__typename": "ManagedBGP",
                "name": _v("bgp-overlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(overlay_asn)}),
                "router_id": _node({"address": _v(router_id)}),
                "peerings": _edges([overlay_peering_1, overlay_peering_2]),
            }
        )

    elif scenario == "ospf_ibgp":
        # OSPF underlay + iBGP overlay, no eBGP
        use_ospf_on_interfaces = True
        overlay_asn = 65000

        overlay_peering_1 = _make_bgp_peering(
            device_name=device_name,
            device_ip=router_id,
            device_asn=overlay_asn,
            remote_name="spine-01",
            remote_ip=spine1_loopback,
            remote_asn=overlay_asn,
            session_type="IBGP",
            ttl=2,
            route_reflector_client=True,
        )
        overlay_peering_2 = _make_bgp_peering(
            device_name=device_name,
            device_ip=router_id,
            device_asn=overlay_asn,
            remote_name="spine-02",
            remote_ip=spine2_loopback,
            remote_asn=overlay_asn,
            session_type="IBGP",
            ttl=2,
            route_reflector_client=True,
        )
        device_capabilities.append(
            {
                "__typename": "ManagedBGP",
                "name": _v("bgp-overlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(overlay_asn)}),
                "router_id": _node({"address": _v(router_id)}),
                "peerings": _edges([overlay_peering_1, overlay_peering_2]),
            }
        )
        device_capabilities.append(
            {
                "__typename": "ManagedOSPF",
                "name": _v("ospf-underlay"),
                "status": _v("active"),
                "process_id": _v(1),
                "version": _v("v2"),
                "router_type": _v("standard"),
                "reference_bandwidth": _v(100000),
                "router_id": _node({"address": _v(router_id)}),
            }
        )

    # Build interfaces
    ospf_area = "0.0.0.0" if use_ospf_on_interfaces else None

    interfaces = [
        _make_interface(
            name="Loopback0",
            device_name=device_name,
            description="Router ID",
            role="loopback",
            ip_address=router_id,
            typename="DcimVirtualInterface",
        ),
        _make_interface(
            name="Ethernet1",
            device_name=device_name,
            description="to spine-01",
            role="spine",
            ip_address=local_p2p_1,
            ospf_area=ospf_area,
            remote_name="Ethernet1/1",
            remote_ip=remote_p2p_1,
            remote_device="spine-01",
        ),
        _make_interface(
            name="Ethernet2",
            device_name=device_name,
            description="to spine-02",
            role="spine",
            ip_address=local_p2p_2,
            ospf_area=ospf_area,
            remote_name="Ethernet1/2",
            remote_ip=remote_p2p_2,
            remote_device="spine-02",
        ),
    ]

    segment_kwargs: list[dict[str, Any]] = []
    if include_segments:
        # Build optional security policy for with_acl scenario
        policy_vxlan = None
        policy_vlan = None
        if include_acls:
            policy_vxlan = _make_security_policy(
                name="policy-vxlan-seg",
                rules=[
                    _make_policy_rule(index=10, name="allow-https", protocol="tcp", port_start=443),
                    _make_policy_rule(index=20, name="allow-http", protocol="tcp", port_start=80),
                ],
            )
            policy_vlan = _make_security_policy(
                name="policy-vlan-seg",
                rules=[
                    _make_policy_rule(index=10, name="allow-ssh", protocol="tcp", port_start=22),
                ],
            )

        # This device's own VLAN domain. build_device_data never builds a
        # ManagedMLAG capability, so _resolve_own_vlan_domain_id falls through to
        # the device id — the vlan_domain_segments entry must use the same value
        # or the transform skips the VxlanSegment.
        vlan_domain_id = f"dev-{device_name}"

        # microsegmented: assign firewall but still render leaf ACL
        microseg = isolation_mode == "microsegmented"
        segment_kwargs.append(
            {
                "vlan_id": 100,
                # 10100 is inside the per-site L2 pool range (10001-39999).
                "vni": 10100,
                "seg_name": "seg-100",
                "seg_type": "ManagedVxlanSegment",
                "gateway_ip": "10.100.0.1/24",
                "ns_name": "VRF_A",
                # Non-default namespace + l3_vni is what makes this a symmetric-IRB
                # segment: it drives the L3 VNI / VRF stanza. 50001 is just a
                # fixture value, disjoint from the L2 range above.
                "l3_vni": 50001,
                "security_policy": policy_vxlan,
                "num_deployments": 1,
                "isolation_mode": isolation_mode,
                "has_firewall": microseg,
                "vlan_domain_id": vlan_domain_id,
            }
        )
        # SR OS models no VLAN-only service: it has no vpls/vprn to attach the gateway IRB
        # to, so the transform refuses a VLAN-only gateway segment (transforms/common.py
        # _sros_irbs; the refusal is covered by tests/unit/test_base_device_transform_sros.py)
        # and binds a segment ACL only inside the vpls of an L2 VNI. The fixture omits it.
        if platform != "nokia_sros":
            segment_kwargs.append(
                {
                    "vlan_id": 200,
                    "seg_name": "seg-200",
                    "seg_type": "ManagedVlanSegment",
                    "gateway_ip": "10.200.0.1/24",
                    **({"ns_name": "VRF_A", "l3_vni": 50001} if vlan_segment_vrf else {}),
                    "security_policy": policy_vlan,
                    "isolation_mode": isolation_mode,
                    "num_deployments": 1,
                }
            )

    # VXLAN/EVPN is rendered ONLY from capabilities, so the
    # customer-facing port must carry the segments. Attaching them solely to
    # deployment.segment_deployments (as this generator used to) left every
    # fixture with zero VXLAN output while still passing.
    if role in SEGMENT_ROLES:
        interfaces.append(
            _make_interface(
                name="Ethernet10",
                device_name=device_name,
                description="to server-01",
                role="server",
                ip_address=None,
                segments=[_make_segment_node(**kw) for kw in segment_kwargs],
            )
        )

    # deployment.segment_deployments is a DIFFERENT consumer: border-leaf's
    # DC-wide PBR rules. Keep both in sync from the same source.
    activations: list[dict] = [_make_segment_deployment(**kw) for kw in segment_kwargs]

    device_node: dict[str, Any] = {
        "__typename": "DcimPhysicalDevice",
        "id": f"dev-{device_name}",
        "name": _v(device_name),
        "role": _v(role),
        "platform": _node(
            {
                "id": f"plat-{platform}",
                "name": _v(platform),
                "netmiko_device_type": _v(platform),
                "napalm_driver": _v(platform),
                "ansible_network_os": _v(platform),
            }
        ),
        "primary_address": _node(
            {
                "address": _v(router_id),
                "ip_namespace": _node({"name": _v("default")}),
            }
        ),
        "tags": _edges([]),
        "capabilities": _edges(device_capabilities),
        "interfaces": _edges(interfaces),
        "deployment": _node(
            {
                "id": "dc-1",
                "name": _v("DC-1"),
                # TopologySegmentHosting.evpn_rt_as — the fabric-wide EVPN
                # route-target administrative ASN. Without it, get_vxlan_config
                # falls back to the device's OWN overlay ASN, which under
                # ebgp-ebgp gives every VTEP a different route-target. The
                # generator populates it, so the fixtures must too or the golden
                # output pins the broken fallback.
                "evpn_rt_as": _node({"asn": _v(FABRIC_RT_ASN)}),
                "segment_deployments": _edges(activations),
            }
        ),
    }

    return {"DcimDevice": _edges([device_node])}


# terminate_inline segment: gatewayed by a firewall HA pair (its gateway is
# the pair's VIP), so every fabric switch carries it as pure L2 — VLAN + L2
# VNI, no SVI / VRF / L3 VNI. The gateway still sits in VRF_A with an L3 VNI
# so the fixtures prove the fabric drops both.
_INLINE_SEGMENT_KWARGS: dict[str, Any] = {
    "vlan_id": 150,
    "vni": 10150,
    "seg_name": "seg-inline-150",
    "seg_type": "ManagedVxlanSegment",
    "gateway_ip": "10.150.0.1/24",
    "ns_name": "VRF_A",
    "l3_vni": 50001,
}


def _make_inline_segment_node(vlan_domain_id: str) -> dict:
    """The terminate_inline VXLAN segment, with this device's vlan_domain_segments row."""
    seg = _make_segment_node(**_INLINE_SEGMENT_KWARGS, vlan_domain_id=vlan_domain_id)
    seg["terminate_inline"] = _v(True)
    seg["inline_service"] = _node({"id": "fw-ha-dc1", "name": _v("dc1-fw-ha")})
    return seg


def build_inline_segment_data(*, device_name: str, role: str, platform: str) -> dict:
    """A leaf or border-leaf carrying one terminate_inline segment.

    leaf: the segment is on the customer port (Ethernet10, access) — VLAN +
    L2 VNI, no SVI. border-leaf: the segment is on a firewall-role service
    port (Ethernet20) facing an HA member, which the generator tags with it —
    rendered as a tagged trunk. Either way the segment is in the device's own
    VLAN domain (its device id: build_device_data builds no ManagedMLAG).
    """
    data = build_device_data(
        device_name=device_name, role=role, platform=platform, scenario="ebgp_ibgp", include_segments=False
    )
    device_node = data["DcimDevice"]["edges"][0]["node"]
    seg = _make_inline_segment_node(vlan_domain_id=device_node["id"])
    interfaces = device_node["interfaces"]["edges"]
    if role == "border-leaf":
        interfaces.append(
            {
                "node": _make_interface(
                    name="Ethernet20",
                    device_name=device_name,
                    description="to dc1-fw-01",
                    role="firewall",
                    segments=[seg],
                )
            }
        )
    else:
        customer_port = next(e["node"] for e in interfaces if e["node"]["name"]["value"] == "Ethernet10")
        customer_port["capabilities"] = _edges([seg])
    # DC-wide view (border-leaf PBR / BGW): the same segment, still no PBR.
    device_node["deployment"]["node"]["segment_deployments"] = _edges(
        [{"vni": _v(_INLINE_SEGMENT_KWARGS["vni"]), "status": _v("active"), "segment": _node(seg)}]
    )
    return data


# ============================================================================
# Device type × platform × scenario matrix
# ============================================================================

FABRIC_PLATFORMS = ["arista_eos", "cisco_nxos", "dell_sonic", "nokia_sros", "sonic"]
# l2-leafs are pure L2 aggregation — no Nokia SROS template exists for this role
# (not used by any l2-leaf design element in this project's data).
L2_LEAF_PLATFORMS = ["arista_eos", "cisco_nxos", "dell_sonic", "sonic"]
# ebgp_ebgp first: it is the schema default (schemas/extensions/topology/topology_dc.yml
# routing_strategy) and the only scenario that yields an eBGP EVPN overlay.
SCENARIOS = ["ebgp_ebgp", "ebgp_ibgp", "ospf_ibgp"]
# ACL smoke tests: leaf only (ACLs are rendered on VLAN SVIs, a leaf concern)
ACL_PLATFORMS = FABRIC_PLATFORMS
FIREWALL_PLATFORMS = ["paloalto_panos", "cisco_asa", "fortinet_fortios", "checkpoint_gaia"]

# (transform, role, dir_prefix, platforms).
#
# `role` must be the literal value from the role dropdown in schemas/base/dcim.yml
# — hyphenated, not underscored. get_bgp_profile() and _build_peer_groups() branch
# on the device's `role` straight out of the query, so an underscored fixture role
# silently skipped the border-leaf and super-spine branches. `dir_prefix` keeps the
# underscore so existing fixture directory names are unaffected.
DEVICE_CONFIGS: list[tuple[type, str, str, list[str]]] = [
    (Leaf, "leaf", "leaf", FABRIC_PLATFORMS),
    (Spine, "spine", "spine", FABRIC_PLATFORMS),
    (SuperSpine, "super-spine", "super_spine", FABRIC_PLATFORMS),
    (BorderLeaf, "border-leaf", "border_leaf", FABRIC_PLATFORMS),
    # border-spine collapses spine + border-leaf: a relaying tier that is also a
    # VTEP, so it is the only role exercising both the EVPN relay knobs and the
    # VXLAN block on the same device.
    (BorderSpine, "border-spine", "border_spine", FABRIC_PLATFORMS),
    (ToR, "tor", "tor", FABRIC_PLATFORMS),
    (L2Leaf, "l2-leaf", "l2_leaf", L2_LEAF_PLATFORMS),
    (AccessLeaf, "access-leaf", "access_leaf", FABRIC_PLATFORMS),
    (Edge, "edge", "edge", ["cisco_nxos", "cisco_ios"]),
]


def _write_fixture(
    transform_cls: type,
    dir_name: str,
    dev_name: str,
    role: str,
    platform: str,
    scenario: str,
    include_segments: bool,
    include_acls: bool = False,
    isolation_mode: str | None = None,
    security_fields: bool = False,
) -> tuple[int, int]:
    data = build_device_data(
        device_name=dev_name,
        role=role,
        platform=platform,
        scenario=scenario,
        include_segments=include_segments,
        include_acls=include_acls,
        isolation_mode=isolation_mode,
        security_fields=security_fields,
    )
    return _write_data_fixture(transform_cls, dir_name, data)


def write_fixtures() -> tuple[int, int]:
    """Write the fabric-switch fixtures: the device x platform x scenario matrix, ACLs, BGP hardening, inline segments."""
    generated = errors = 0

    # Standard scenarios (no ACLs)
    for transform_cls, role, type_prefix, platforms in DEVICE_CONFIGS:
        for platform in platforms:
            for scenario in SCENARIOS:
                g, e = _write_fixture(
                    transform_cls,
                    dir_name=f"{type_prefix}_{platform}_{scenario}",
                    dev_name=f"dc1-{type_prefix.replace('_', '-')}-01",
                    role=role,
                    platform=platform,
                    scenario=scenario,
                    include_segments=role in SEGMENT_ROLES,
                )
                generated += g
                errors += e

    # ACL scenario: leaf only, ebgp_ibgp base, with a security_policy on each segment
    print("\nGenerating ACL fixtures (leaf):")
    for platform in ACL_PLATFORMS:
        g, e = _write_fixture(
            Leaf,
            dir_name=f"leaf_{platform}_with_acl",
            dev_name="dc1-leaf-01",
            role="leaf",
            platform=platform,
            scenario="ebgp_ibgp",
            include_segments=True,
            include_acls=True,
        )
        generated += g
        errors += e

    # BGP security/hardening fields: maximum_routes, local_pref, med,
    # send_extended_community, remove_private_as, password on the underlay peering
    print("\nGenerating BGP security-fields fixtures (leaf):")
    for platform in FABRIC_PLATFORMS:
        g, e = _write_fixture(
            Leaf,
            dir_name=f"leaf_{platform}_bgp_security_fields",
            dev_name="dc1-leaf-01",
            role="leaf",
            platform=platform,
            scenario="ebgp_ibgp",
            include_segments=False,
            security_fields=True,
        )
        generated += g
        errors += e

    # terminate_inline segment: pure L2 on the fabric (no SVI / VRF / L3 VNI);
    # the border-leaf trunks it tagged towards the HA pair gatewaying it.
    print("\nGenerating inline-terminated segment fixtures (leaf, border-leaf):")
    g, e = write_data_fixtures(
        (
            transform_cls,
            f"{type_prefix}_{platform}_inline_segment",
            build_inline_segment_data(
                device_name=f"dc1-{type_prefix.replace('_', '-')}-01", role=role, platform=platform
            ),
        )
        for transform_cls, role, type_prefix in ((Leaf, "leaf", "leaf"), (BorderLeaf, "border-leaf", "border_leaf"))
        for platform in FABRIC_PLATFORMS
    )
    generated += g
    errors += e

    # Isolation mode: isolated — switchport protected, no intra-segment forwarding
    print("\nGenerating isolated segment fixtures (leaf):")
    for platform in ACL_PLATFORMS:
        g, e = _write_fixture(
            Leaf,
            dir_name=f"leaf_{platform}_isolated",
            dev_name="dc1-leaf-01",
            role="leaf",
            platform=platform,
            scenario="ebgp_ibgp",
            include_segments=True,
            include_acls=True,
            isolation_mode="isolated",
        )
        generated += g
        errors += e

    # Isolation mode: microsegmented — leaf ACL rendered even when firewall present
    print("\nGenerating microsegmented fixtures (leaf):")
    for platform in ACL_PLATFORMS:
        g, e = _write_fixture(
            Leaf,
            dir_name=f"leaf_{platform}_microsegmented",
            dev_name="dc1-leaf-01",
            role="leaf",
            platform=platform,
            scenario="ebgp_ibgp",
            include_segments=True,
            include_acls=True,
            isolation_mode="microsegmented",
        )
        generated += g
        errors += e
    return generated, errors
