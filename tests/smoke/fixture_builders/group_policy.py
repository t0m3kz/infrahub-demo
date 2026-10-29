"""Group policy (NX-OS VXLAN GPO, EOS) fleet builders and device fixtures."""

from __future__ import annotations

from typing import Any

import yaml

from tests.smoke.fixture_builders.common import (
    PROJECT_ROOT,
    _edges,
    _make_interface,
    _make_segment_node,
    _node,
    _v,
    write_data_fixtures,
)
from tests.smoke.fixture_builders.fabric import build_device_data
from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.leaf import Leaf
from transforms.helpers.group_policy import group_policy_device_view
from utils.data_cleaning import clean_data

# ============================================================================
# Group policy fleet builders (DeviceGroupPolicyFields, SegmentGroupPolicyFields, SecurityTagGroupPolicyFields)
# ============================================================================


def _gp_device(
    name: str, role: str, device_type: str, version: str | None, memory_gb: dict[str, int], platform: str = "cisco_nxos"
) -> dict:
    """A DeviceGroupPolicyFields device; ``memory_gb`` is device type -> memory (06_device_types.yml)."""
    return {
        "name": _v(name),
        "role": _v(role),
        "platform": _node({"name": _v(platform)}),
        "device_type": _node({"name": _v(device_type), "memory_gb": _v(memory_gb[device_type])}),
        "software_image": _node({"version": _v(version)} if version else None),
    }


def _gp_rule(name: str, dst_segment: str | None, port: int | None, **kwargs: Any) -> dict:
    """A rule of a segment's own policy, as SegmentGroupPolicyFields selects it."""
    return {
        "id": f"rule-{name}",
        "index": _v(kwargs.get("index", 100)),
        "name": _v(name),
        "action": _v(kwargs.get("action", "permit")),
        "protocol": _v(kwargs.get("protocol", "tcp")),
        "port_start": _v(port),
        "port_end": _v(kwargs.get("port_end")),
        "disabled": _v(kwargs.get("disabled", False)),
        "apply_on_switch": _v(kwargs.get("apply_on_switch", True)),
        "source_segment": _node(None),
        "destination_segment": _node({"id": dst_segment} if dst_segment else None),
        "source_security_tag": _node({"id": kwargs["src_tag"]} if kwargs.get("src_tag") else None),
        "destination_security_tag": _node({"id": kwargs["dst_tag"]} if kwargs.get("dst_tag") else None),
    }


def _gp_segment(
    seg_id: str,
    name: str,
    owner: str,
    *,
    prefix: str | None,
    namespace: tuple[str, str] = ("PROD", "prod"),
    zone: str = "PROD-ZONE",
    isolation_mode: str = "normal",
    rules: list[dict] | None = None,
    tag: tuple[str, int] | None = None,
    sites: tuple[str, ...] = ("dc10",),
    inline: str | None = None,
) -> dict:
    """One SegmentGroupPolicyFields VXLAN segment, active in ``sites``."""
    segment: dict[str, Any] = {
        "id": seg_id,
        "__typename": "ManagedVxlanSegment",
        "name": _v(name),
        "isolation_mode": _v(isolation_mode),
        "owner": _node({"org_id": _v(owner)}),
        "security_zone": _node({"name": _v(zone)}),
        "security_tag": _node({"id": f"tag-{seg_id}", "name": _v(tag[0]), "group_id": _v(tag[1])} if tag else None),
        "gateway": _node(
            {
                "ip_prefix": _node(
                    {
                        "prefix": _v(prefix),
                        "ip_namespace": _node({"name": _v(namespace[0]), "namespace_type": _v(namespace[1])}),
                    }
                )
            }
            if prefix
            else None
        ),
        "security_policy": _node(
            {
                "name": _v(f"seg-{name}-egress"),
                "enabled": _v(True),
                "default_action": _v("deny"),
                "rules": _edges(rules or []),
            }
        ),
    }
    segment["stretch_scope"] = _v("local" if len(sites) == 1 else "dc_pair")
    segment["inline_service"] = _node({"id": f"svc-{inline}", "name": _v(inline)} if inline else None)
    segment["segment_deployments"] = _edges(
        [
            {"vni": _v(10000 + n), "deployment": _node({"id": site, "name": _v(site.upper())})}
            for n, site in enumerate(sites)
        ]
    )
    return segment


def _gp_nic(
    host: str,
    nic: str,
    switch: str,
    port: str,
    segment_ids: tuple[str, ...],
    *,
    lag: str | None = None,
    far_role: str | None = None,
) -> dict:
    """A host NIC cabled to ``switch`` ``port`` as SecurityTagPlacementFields selects it: the cable's two
    endpoints (the near end is the NIC), the far end tagged with ``segment_ids``. A LAG member (``lag`` being
    its port-channel, which carries the tags) is role lag; ``far_role`` overrides the far end's role."""
    tagged = [{"id": f"seg-{name}"} for name in segment_ids]
    near = {
        "id": f"if-{host}-{nic}",
        "name": _v(nic),
        "role": _v("uplink"),
        "device": _node({"name": _v(host)}),
        "capabilities": _edges([]),
        "lag": _node(None),
    }
    far = {
        "id": f"if-{switch}-{port}",
        "name": _v(port),
        "role": _v(far_role or ("lag" if lag else "server")),
        "device": _node({"name": _v(switch)}),
        "segments": _edges([] if lag else tagged),
        "lag": _node({"name": _v(lag), "segments": _edges(tagged)} if lag else None),
    }
    return {"id": near["id"], "name": _v(nic), "cable": _node({"endpoints": _edges([near, far])})}


def _gp_instance(name: str, nics: list[dict], *, host: str | None = None) -> dict:
    """A component's instance: a physical device with ``nics``, or (``host`` set) a VM on a host that has them."""
    if host is None:
        return {"id": f"inst-{name}", "__typename": "DcimPhysicalDevice", "name": _v(name), "interfaces": _edges(nics)}
    return {
        "id": f"inst-{name}",
        "__typename": "DcimVirtualDevice",
        "name": _v(name),
        "interfaces": _edges([]),
        "hosting_device": _node({"id": f"host-{host}", "name": _v(host), "interfaces": _edges(nics)}),
    }


def _gp_tag(
    tag_id: str, group_id: int, fqdn: str | None, seg_id: str | None, instances: list[dict] | None = None
) -> dict:
    """A SecurityTag with its component; ``instances`` (SecurityTagPlacementFields) only for the NX-OS device queries."""
    component: dict[str, Any] = {"fqdn": _v(fqdn), "network_segment": _node({"id": seg_id})}
    if instances is not None:
        component["instances"] = _edges(instances)
    components = [component] if fqdn else []
    return {
        "id": tag_id,
        "name": _v(fqdn or f"{tag_id}.retired"),
        "group_id": _v(group_id),
        "components": _edges(components),
    }


def _gp_memory_gb() -> dict[str, int]:
    """Device type -> memory (data/bootstrap/06_device_types.yml)."""
    device_types = yaml.safe_load((PROJECT_ROOT / "data/bootstrap/06_device_types.yml").read_text())["spec"]["data"]
    return {item["name"]: item.get("memory_gb") for item in device_types}


def _gp_profiles() -> list[dict]:
    """The bootstrap DcimGroupPolicyProfile rows as GroupPolicyProfileFields returns them."""
    profile_rows = yaml.safe_load((PROJECT_ROOT / "data/bootstrap/19b_group_policy_profile.yml").read_text())["spec"][
        "data"
    ]
    return [
        {
            "name": _v(row["name"]),
            "group_policy_kind": _v(row["group_policy_kind"]),
            "min_release": _v(row["min_release"]),
            "min_memory_gb": _v(row.get("min_memory_gb")),
            "unclassified_group_id": _v(row.get("unclassified_group_id")),
            "unaware_group_id": _v(row.get("unaware_group_id")),
            "platform": _node({"name": _v(row["platform"])}),
            "device_types": _edges([{"name": _v(name)} for name in row["device_types"]]),
        }
        for row in profile_rows
    ]


# ============================================================================
# NX-OS VXLAN GPO device configs (the fleet roots of queries/config/{leaf,border_leaf,border_spine}.gql)
# ============================================================================

# (name, VLAN, VNI, gateway, VRF, namespace type, L3 VNI): the five tagged tenant segments of DC10.
# NONPROD is fully classifiable by VLAN; PROD holds a microsegmented segment whose contracts name
# component groups, classified by the interface + VLAN of the host ports of dc1-leaf-01.
_GPO_SEGMENTS: list[tuple[str, int, int, str, str, str, int]] = [
    ("web-np", 110, 10110, "10.10.1.1/24", "NONPROD", "non_prod", 50011),
    ("app-np", 120, 10120, "10.10.2.1/24", "NONPROD", "non_prod", 50011),
    ("db-np", 130, 10130, "10.10.3.1/24", "NONPROD", "non_prod", 50011),
    ("pay-p", 210, 10210, "10.20.1.1/24", "PROD", "prod", 50012),
    ("portal-p", 220, 10220, "10.20.2.1/24", "PROD", "prod", 50012),
]
# The segments the host ports of PROD's components carry (tagged by the generator).
_GPO_PAY_WEB_TAGS = ("web-np", "app-np", "pay-p", "portal-p")
_GPO_PAY_DB_TAGS = ("pay-p",)
_GPO_NXOS = {
    "platform": "cisco_nxos",
    "leaf": "N9K-C9364C-GX",
    "access": "N9K-C93180YC-FX3",
    "border": "N9K-C9364D-GX2A",
}
_GPO_EOS = {
    "platform": "arista_eos",
    "leaf": "DCS-7280SR3-48YC8",
    "access": "DCS-7280SR3-48YC8",
    "border": "DCS-7280SR3-48YC8",
}


def _gpo_fleet_roots(
    *, fleet: dict[str, str], version: str, leaf_type: str | None = None, enforce: bool = True, lb: bool = False
) -> dict:
    """The fleet roots of the GPO device queries: DC10's VTEPs (``leaf_type`` overrides the leaf's type)
    next to DC12 (a legacy border leaf: never policy-aware), the bootstrap profile rows, the segments
    and their component tags. ``enforce`` is DC10's ``group_policy_enforce`` (A6: False stages it).

    NONPROD: web-np calls app-np (https and a port range, with a reverse udp row: two bidir contracts
    on one pair), app-np calls db-np, db-np answers with any and icmp, web-np reaches the internet
    (a service leg); db-np is stretched to DC12. PROD: portal-p calls the web component of the
    microsegmented pay-p, whose web component calls its db component.

    ``lb`` load-balances web-np on tcp/9443: app-np calls its VIP (a /32 selector: app-np ->
    UNCLASSIFIED), and the segment-less policy lb-dc1-lb-01-NON-PROD lets the leg reach web-np
    (UNCLASSIFIED -> web-np), read from web-np's inbound_rules next to an inbound rule of app-np's own
    policy (already a row through app-np: not counted twice).
    """
    memory_gb = _gp_memory_gb()
    platform = fleet["platform"]

    def device(name: str, role: str, device_type: str, release: str | None) -> dict:
        return _gp_device(name, role, device_type, release, memory_gb, platform)

    legacy = _gp_device("bl-dc12-01", "border-leaf", "N9K-C9336C-FX2", "10.3(3)", memory_gb)
    # PROD's components sit on the leaf of DC10: the web component on the trunk host port Ethernet10 (the
    # port of every segment), the db component, a VM, on Ethernet11 (an access port today: GPO trunks it),
    # its host's second NIC on DC12's border leaf (another data center: not this one's to classify).
    pay_web = [
        _gp_instance(
            "srv-pay-web-01", [_gp_nic("srv-pay-web-01", "eno1", "dc1-leaf-01", "Ethernet10", _GPO_PAY_WEB_TAGS)]
        )
    ]
    pay_db = [
        _gp_instance(
            "vm-pay-db-01",
            [
                _gp_nic("srv-pay-db-01", "eno1", "dc1-leaf-01", "Ethernet11", _GPO_PAY_DB_TAGS),
                _gp_nic("srv-pay-db-01", "eno2", "bl-dc12-01", "Ethernet5", _GPO_PAY_DB_TAGS),
            ],
            host="srv-pay-db-01",
        )
    ]
    segments = [
        _gp_segment(
            "seg-web-np",
            "web-np",
            "C010",
            prefix="10.10.1.0/24",
            namespace=("NONPROD", "non_prod"),
            zone="NONPROD-ZONE",
            rules=[
                _gp_rule("web-np-to-app-np-tcp-443", "seg-app-np", 443),
                _gp_rule("web-np-to-app-np-tcp-8000-8010", "seg-app-np", 8000, index=110, port_end=8010),
                _gp_rule("web-np-to-internet-tcp-443", None, 443, index=120),
            ],
            tag=("web-np", 40),
        ),
        _gp_segment(
            "seg-app-np",
            "app-np",
            "C010",
            prefix="10.10.2.0/24",
            namespace=("NONPROD", "non_prod"),
            zone="NONPROD-ZONE",
            rules=[
                _gp_rule("app-np-to-web-np-udp-5000", "seg-web-np", 5000, protocol="udp"),
                _gp_rule("app-np-to-db-np-tcp-5432", "seg-db-np", 5432, index=110),
                *([_gp_rule("app-np-to-web-np-vip-tcp-9443", None, 9443, index=120)] if lb else []),
            ],
            tag=("app-np", 41),
        ),
        _gp_segment(
            "seg-db-np",
            "db-np",
            "C010",
            prefix="10.10.3.0/24",
            namespace=("NONPROD", "non_prod"),
            zone="NONPROD-ZONE",
            rules=[
                _gp_rule("db-np-to-app-np-any", "seg-app-np", None, protocol="any"),
                _gp_rule("db-np-to-web-np-icmp", "seg-web-np", None, protocol="icmp", index=110),
            ],
            tag=("db-np", 42),
            sites=("dc10", "dc12"),
        ),
        _gp_segment(
            "seg-pay-p",
            "pay-p",
            "C011",
            prefix="10.20.1.0/24",
            namespace=("PROD", "prod"),
            isolation_mode="microsegmented",
            rules=[
                _gp_rule("pay-web-to-pay-db-tcp-5432", "seg-pay-p", 5432, src_tag="tag-pay-web", dst_tag="tag-pay-db")
            ],
            tag=("pay-p", 43),
        ),
        _gp_segment(
            "seg-portal-p",
            "portal-p",
            "C011",
            prefix="10.20.2.0/24",
            namespace=("PROD", "prod"),
            rules=[_gp_rule("portal-to-pay-web-tcp-443", "seg-pay-p", 443, dst_tag="tag-pay-web")],
            tag=("portal-p", 44),
        ),
    ]
    if lb:
        web = next(segment for segment in segments if segment["id"] == "seg-web-np")
        web["inbound_rules"] = _edges(
            [
                {
                    **_gp_rule(name, "seg-web-np", port, protocol=protocol),
                    "policy": _node({"enabled": _v(True), "segment": _node(owner)}),
                }
                for name, port, protocol, owner in (
                    ("app-np-to-web-np-udp-5000", 5000, "udp", {"id": "seg-app-np"}),
                    ("lb-to-web-np-tcp-9443", 9443, "tcp", None),
                )
            ]
        )
    return {
        "DcimGroupPolicyProfile": _edges(_gp_profiles()),
        "TopologyDataCenter": _edges(
            [
                {
                    "id": "dc10",
                    "name": _v("DC10"),
                    "group_policy_enforce": _v(enforce),
                    "devices": _edges([device("dc1-border-leaf-01", "border-leaf", fleet["border"], version)]),
                },
                {"id": "dc12", "name": _v("DC12"), "group_policy_enforce": _v(False), "devices": _edges([legacy])},
            ]
        ),
        "TopologyPod": _edges(
            [
                {
                    "parent": _node({"id": "dc10", "__typename": "TopologyDataCenter"}),
                    "devices": _edges(
                        [
                            device("dc1-leaf-01", "leaf", leaf_type or fleet["leaf"], version),
                            device("dc1-access-leaf-01", "access-leaf", fleet["access"], version),
                            # Not a VTEP: the query returns every device, the transform keeps the VTEPs.
                            device("dc1-spine-01", "spine", "N9K-C9336C-FX2", "10.3(3)"),
                        ]
                    ),
                }
            ]
        ),
        "ManagedNetworkSegment": _edges(segments),
        "SecurityTag": _edges(
            [
                _gp_tag("tag-pay-web", 50, "web.pay.demo.local", "seg-pay-p", pay_web),
                _gp_tag("tag-pay-db", 51, "db.pay.demo.local", "seg-pay-p", pay_db),
            ]
        ),
    }


def _gpo_segment_nodes(device_name: str, segments: tuple[str, ...]) -> list[dict]:
    """The capability nodes of ``segments`` (names of _GPO_SEGMENTS) on a server port of ``device_name``."""
    return [
        _make_segment_node(
            vlan_id=vlan,
            vni=vni,
            seg_name=name,
            gateway_ip=gateway,
            ns_name=vrf,
            l3_vni=l3_vni,
            vlan_domain_id=f"dev-{device_name}",
        )
        for name, vlan, vni, gateway, vrf, _ns_type, l3_vni in _GPO_SEGMENTS
        if name in segments
    ]


def build_gpo_device_data(
    *,
    device_name: str,
    role: str,
    segments: tuple[str, ...],
    fleet: dict[str, str] = _GPO_NXOS,
    version: str = "10.6(3)F",
    leaf_type: str | None = None,
    host_ports: dict[str, tuple[str, ...]] | None = None,
    enforce: bool = True,
    lb: bool = False,
) -> dict:
    """A VTEP of DC10 carrying ``segments`` (names of _GPO_SEGMENTS) on Ethernet10, plus one more server port
    per ``host_ports`` entry (name -> the segments it carries), with the fleet roots appended
    (``enforce``: DC10's ``group_policy_enforce``, False for the staged variant; ``lb``: web-np load-balanced)."""
    data = build_device_data(
        device_name=device_name,
        role=role,
        platform=fleet["platform"],
        scenario="ebgp_ibgp",
        include_segments=False,
    )
    node = data["DcimDevice"]["edges"][0]["node"]
    node["interfaces"]["edges"] = [
        edge for edge in node["interfaces"]["edges"] if edge["node"]["name"]["value"] != "Ethernet10"
    ]
    node["interfaces"]["edges"].append(
        {
            "node": _make_interface(
                name="Ethernet10",
                device_name=device_name,
                description="to server-01",
                role="server",
                segments=_gpo_segment_nodes(device_name, segments),
            )
        }
    )
    for port, port_segments in (host_ports or {}).items():
        node["interfaces"]["edges"].append(
            {
                "node": _make_interface(
                    name=port,
                    device_name=device_name,
                    role="server",
                    segments=_gpo_segment_nodes(device_name, port_segments),
                )
            }
        )
    return {**data, **_gpo_fleet_roots(fleet=fleet, version=version, leaf_type=leaf_type, enforce=enforce, lb=lb)}


def gpo_roots(raw: dict) -> dict[str, Any]:
    """The cleaned fleet roots of a ``build_gpo_device_data`` response (everything but the device root)."""
    return {key: value for key, value in clean_data(raw).items() if key != "DcimDevice"}


def gpo_segment(roots: dict[str, Any], seg_id: str) -> dict[str, Any]:
    """The cleaned segment ``seg_id`` among the fleet roots."""
    return next(segment for segment in roots["ManagedNetworkSegment"] if segment["id"] == seg_id)


def gpo_view(kind: str, roots: dict[str, Any], **arguments: Any) -> dict[str, Any] | None:
    """The device view of group-policy ``kind`` that ``group_policy_device_view`` gives these arguments, else None."""
    found = group_policy_device_view(roots, **arguments)
    return found[1] if found and found[0] == kind else None


def _gpo_golden_leaf_data(
    *, fleet: dict[str, str], version: str, leaf_type: str | None = None, enforce: bool = True
) -> dict:
    """The leaf of an existing golden config (``ebgp_ibgp``), with the GPO fleet roots appended."""
    return {
        **build_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            platform=fleet["platform"],
            scenario="ebgp_ibgp",
            include_segments=True,
        ),
        **_gpo_fleet_roots(fleet=fleet, version=version, leaf_type=leaf_type, enforce=enforce),
    }


GPO_FIXTURES: list[tuple[type, str, Any]] = [
    (
        Leaf,
        "leaf_cisco_nxos_gpo_reference",
        lambda: build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=("web-np", "app-np", "pay-p", "portal-p"),
            host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
        ),
    ),
    # DC10 stages group policy (group_policy_enforce false): groups, class/policy maps and contracts are
    # rendered, `security enforce` is not, and the VRF says why in an `! unenforced:` comment.
    (
        Leaf,
        "leaf_cisco_nxos_gpo_reference_staged",
        lambda: build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=("web-np", "app-np", "pay-p", "portal-p"),
            host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
            enforce=False,
        ),
    ),
    # A leaf of the same reference fabric with no tenant segment (the servers sit on the access
    # leaves): no VRF carries a contract, but the VTEP still enables GPO, so the site never
    # mixes policy-aware and policy-unaware nodes (spec 2.9).
    (
        Leaf,
        "leaf_cisco_nxos_gpo_reference_no_vrf",
        lambda: build_gpo_device_data(device_name="dc1-leaf-01", role="leaf", segments=()),
    ),
    (
        AccessLeaf,
        "access_leaf_cisco_nxos_gpo_reference",
        lambda: build_gpo_device_data(
            device_name="dc1-access-leaf-01", role="access-leaf", segments=("app-np", "db-np", "portal-p")
        ),
    ),
    (
        BorderLeaf,
        "border_leaf_cisco_nxos_gpo_reference",
        lambda: build_gpo_device_data(
            device_name="dc1-border-leaf-01",
            role="border-leaf",
            segments=tuple(name for name, *_rest in _GPO_SEGMENTS),
        ),
    ),
    # The same fleet with the leaf on a type and release outside the row: DC10 is no reference
    # fabric, so the device renders exactly the leaf_cisco_nxos_ebgp_ibgp config.
    (
        Leaf,
        "leaf_cisco_nxos_gpo_non_reference",
        lambda: _gpo_golden_leaf_data(fleet=_GPO_NXOS, version="10.6(3)F", leaf_type="N9K-C9336C-FX2"),
    ),
    # A reference site of the EOS kind (segment_security, MSS-G): the same fleet as the NX-OS ones, on 7280R3
    # VTEPs at 4.36.2F. The leaf renders the MSS-G block (enforcing), a staged DC10 renders it shut down.
    (
        Leaf,
        "leaf_arista_eos_gpo_reference_site",
        lambda: build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=("web-np", "app-np", "pay-p", "portal-p"),
            host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
            fleet=_GPO_EOS,
            version="4.36.2F",
        ),
    ),
    (
        Leaf,
        "leaf_arista_eos_gpo_reference_staged",
        lambda: build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=("web-np", "app-np", "pay-p", "portal-p"),
            host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
            fleet=_GPO_EOS,
            version="4.36.2F",
            enforce=False,
        ),
    ),
    # web-np load-balanced (segment-less lb-<context> policy): app-np -> UNCLASSIFIED and UNCLASSIFIED -> web-np
    # gain tcp/9443 (NX-OS: the bidir contracts; MSS-G: web-np's fallback and app-np's to_unclassified reply).
    (
        Leaf,
        "leaf_cisco_nxos_gpo_lb_context",
        lambda: build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=("web-np", "app-np", "pay-p", "portal-p"),
            host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
            lb=True,
        ),
    ),
    (
        Leaf,
        "leaf_arista_eos_gpo_lb_context",
        lambda: build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=("web-np", "app-np", "pay-p", "portal-p"),
            host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
            fleet=_GPO_EOS,
            version="4.36.2F",
            lb=True,
        ),
    ),
    (
        AccessLeaf,
        "access_leaf_arista_eos_gpo_reference",
        lambda: build_gpo_device_data(
            device_name="dc1-access-leaf-01",
            role="access-leaf",
            segments=("app-np", "db-np", "portal-p"),
            fleet=_GPO_EOS,
            version="4.36.2F",
        ),
    ),
    (
        BorderLeaf,
        "border_leaf_arista_eos_gpo_reference",
        lambda: build_gpo_device_data(
            device_name="dc1-border-leaf-01",
            role="border-leaf",
            segments=tuple(name for name, *_rest in _GPO_SEGMENTS),
            fleet=_GPO_EOS,
            version="4.36.2F",
        ),
    ),
]


def write_fixtures() -> tuple[int, int]:
    """Write the NX-OS and EOS group policy device fixtures."""
    # NX-OS VXLAN GPO: a reference fabric's VTEPs render the group policy, any other device nothing.
    print("\nGenerating the NX-OS and EOS group policy device fixtures:")
    return write_data_fixtures((transform_cls, dir_name, build()) for transform_cls, dir_name, build in GPO_FIXTURES)
