"""Firewall fixtures and the leaf fixtures of the firewall-context features (internet permit, PBR, LB client)."""

from __future__ import annotations

import ipaddress
from typing import Any

from tests.smoke.fixture_builders.common import (
    _edges,
    _make_interface,
    _make_policy_rule,
    _make_segment_node,
    _node,
    _v,
    _write_data_fixture,
    write_data_fixtures,
)
from tests.smoke.fixture_builders.fabric import FIREWALL_PLATFORMS, build_device_data
from transforms.config.firewall import Firewall
from transforms.config.leaf import Leaf


def _make_firewall_interface(
    *,
    name: str,
    ip_address: str,
    zone_name: str,
    trust_level: int,
    description: str = "",
    vlan_id: int | None = None,
    parent_interface_name: str | None = None,
    namespace_name: str | None = None,
    zone_members: list[dict] | None = None,
) -> dict:
    """A firewall leg carrying its zone's segment. ``zone_members`` are the
    zone's segments with their rules (queries/config/firewall.gql's
    security_zone.network_segments): where a context-less firewall gets the
    rules of the zone it routes through this leg."""
    seg_name = f"seg-{zone_name}"
    zone_node = {
        "name": _v(zone_name),
        "trust_level": _v(trust_level),
        "network_segments": _edges(zone_members or []),
    }
    seg_node = {
        "__typename": "ManagedVxlanSegment",
        "id": seg_name,
        "name": _v(seg_name),
        "status": _v("active"),
        "arp_suppression": _v(True),
        "segment_deployments": _edges([{"vlan_id": _v(vlan_id), "vni": _v(None)}]),
        "security_zone": _node(zone_node),
        # queries/config/firewall.gql reads the segment subnet from
        # gateway.ip_prefix.prefix — a flat `prefix` key (which this used to
        # emit) is read by nothing, so zone address-object rendering was never
        # covered. The firewall's own leg address is the segment gateway here.
        "gateway": _node(
            {
                "address": _v(ip_address),
                "ip_prefix": _node(
                    {
                        "prefix": _v(str(ipaddress.ip_interface(ip_address).network)),
                        "ip_namespace": _node({"name": _v(namespace_name), "l3_vni": _v(None)}),
                    }
                ),
            }
        ),
        "security_policy": _node(None),
        "inbound_rules": _edges([]),
    }
    return {
        "__typename": "DcimVirtualInterface",
        "name": _v(name),
        "description": _v(description),
        "status": _v("active"),
        "role": _v("uplink"),
        "parent_interface": _node({"name": _v(parent_interface_name)} if parent_interface_name else None),
        "ip_address": _node(
            {
                "address": _v(ip_address),
                "ip_namespace": _node({"name": _v(namespace_name)}),
            }
        ),
        "ha_domain": _node(None),
        "capabilities": _edges([seg_node]),
    }


def _make_zone(
    *,
    name: str,
    trust_level: int,
    description: str = "",
    cidrs: list[str] | None = None,
    namespace_name: str | None = None,
) -> dict:
    segments = []
    for cidr in cidrs or []:
        seg_name = f"seg-{cidr.replace('/', '-').replace('.', '-')}"
        segments.append(
            {
                "id": seg_name,
                "__typename": "ManagedVxlanSegment",
                "name": _v(seg_name),
                "gateway": _node({"ip_prefix": _node({"prefix": _v(cidr)})}),
            }
        )
    return {
        "name": _v(name),
        "trust_level": _v(trust_level),
        "description": _v(description),
        "network_segments": _edges(segments),
    }


def _make_rule_segment(*, cidr: str, zone_name: str) -> dict:
    """A rule's source/destination segment (PolicyRuleSegmentFields), carrying the zone the rule matches on."""
    return {
        "id": f"seg-{cidr.replace('/', '-').replace('.', '-')}",
        "__typename": "ManagedVxlanSegment",
        "name": _v(f"seg-{cidr.replace('/', '-').replace('.', '-')}"),
        "security_zone": _node({"name": _v(zone_name)}),
        "gateway": _node({"ip_prefix": _node({"prefix": _v(cidr)})}),
        "customer_deployments": _edges([]),
    }


def _make_segment_rule(
    *,
    policy: str,
    index: int,
    name: str,
    action: str = "permit",
    protocol: str = "tcp",
    port_start: int | None = None,
    port_end: int | None = None,
    src_segment: dict | None,
    dst_segment: dict | None = None,
    dst_zone: str | None = None,
    log: bool = False,
    description: str = "",
    security_profile: str | None = None,
) -> dict:
    """A SecurityPolicyRule (SecurityPolicyRuleFields) of ``policy``, the policy of ``src_segment``.

    ``dst_zone`` is the rule's own ``destination_zone`` (the internet), set instead of ``dst_segment``.
    """
    return {
        "id": f"rule-{policy}-{name}",
        "index": _v(index),
        "name": _v(name),
        "action": _v(action),
        "protocol": _v(protocol),
        "port_start": _v(port_start),
        "port_end": _v(port_end),
        "log": _v(log),
        "disabled": _v(False),
        "description": _v(description),
        "apply_on_switch": _v(False),
        "source_segment": _node(src_segment),
        "destination_segment": _node(dst_segment),
        "source_prefixes": _edges([]),
        "destination_prefixes": _edges([]),
        "source_ip_addresses": _edges([]),
        "destination_ip_addresses": _edges([]),
        "security_profile": _node({"name": _v(security_profile)} if security_profile else None),
        **({"destination_zone": _node({"name": _v(dst_zone)})} if dst_zone else {}),
    }


def _make_policy_header(*, name: str, segment: dict) -> dict:
    """SecurityPolicyFields: the source segment's one policy, without its rules."""
    return {
        "id": f"policy-{name}",
        "name": _v(name),
        "enabled": _v(True),
        "default_action": _v("deny"),
        "segment": _node({"id": segment["id"], "name": segment["name"]}),
    }


def _make_rule_bearing_segments(endpoints: list[dict], policies: list[tuple[dict, list[dict]]]) -> dict[str, dict]:
    """Segment id -> the segment as SegmentRulesFields returns it: its own
    policy (the one whose header names it) with its rules, and the rules of
    every policy whose destination it is, each carrying its policy."""
    segments: dict[str, dict] = {
        endpoint["id"]: {"id": endpoint["id"], "security_policy": _node(None), "inbound_rules": _edges([])}
        for endpoint in endpoints
    }
    for header, rules in policies:
        owner = segments[header["segment"]["node"]["id"]]
        owner["security_policy"] = _node({**header, "rules": _edges(rules)})
        for rule in rules:
            destination = (rule["destination_segment"]["node"] or {}).get("id")
            if destination in segments:
                segments[destination]["inbound_rules"]["edges"].append({"node": {**rule, "policy": _node(header)}})
    return segments


def build_firewall_data(*, device_name: str, platform: str) -> dict:
    """Build a complete multi-root GQL response for a firewall config query.

    Includes:
      - DcimPhysicalDevice with DcimFirewallInterface nodes (one per zone)
      - SecurityZone nodes with member segment CIDRs
      - the zones' member segments with their policies and inbound rules,
        reached through the zone of each leg's segment: a firewall with no
        contexts enforces the rules of the zones it routes
    """
    dmz_segment = _make_rule_segment(cidr="10.0.2.0/24", zone_name="dmz")
    internal_segment = _make_rule_segment(cidr="10.0.1.0/24", zone_name="internal")
    internal_lan_segment = _make_rule_segment(cidr="10.0.10.0/24", zone_name="internal")
    dmz_to_internal = _make_policy_header(name="dmz-to-internal", segment=dmz_segment)
    internal_to_dmz = _make_policy_header(name="internal-to-dmz", segment=internal_segment)
    segments = _make_rule_bearing_segments(
        [dmz_segment, internal_segment, internal_lan_segment],
        [
            (
                dmz_to_internal,
                [
                    _make_segment_rule(
                        policy="dmz-to-internal",
                        index=10,
                        name="allow-https",
                        protocol="tcp",
                        port_start=443,
                        src_segment=dmz_segment,
                        dst_segment=internal_segment,
                        log=True,
                        description="Allow HTTPS from DMZ to Internal",
                        security_profile="strict-av",
                    ),
                    _make_segment_rule(
                        policy="dmz-to-internal",
                        index=20,
                        name="allow-ssh",
                        protocol="tcp",
                        port_start=22,
                        src_segment=dmz_segment,
                        dst_segment=internal_segment,
                        log=True,
                        description="Allow SSH from DMZ to Internal",
                    ),
                ],
            ),
            (
                internal_to_dmz,
                [
                    _make_segment_rule(
                        policy="internal-to-dmz",
                        index=10,
                        name="allow-any",
                        protocol="any",
                        src_segment=internal_segment,
                        dst_segment=dmz_segment,
                        log=False,
                        description="Allow all from Internal to DMZ",
                    ),
                ],
            ),
        ],
    )

    # Sub-interfaces on trunk uplink (ethernet1/1) — one /30 per zone/namespace.
    # Leaf IP is .2, FW IP is .1 in each /30.
    fw_interfaces = [
        _make_firewall_interface(
            name="ethernet1/1.10",
            ip_address="10.0.1.1/30",
            zone_name="internal",
            trust_level=100,
            description="Internal LAN link",
            vlan_id=10,
            parent_interface_name="ethernet1/1",
            namespace_name="VRF-INTERNAL",
            zone_members=[segments[internal_segment["id"]], segments[internal_lan_segment["id"]]],
        ),
        _make_firewall_interface(
            name="ethernet1/1.20",
            ip_address="10.0.2.1/30",
            zone_name="dmz",
            trust_level=50,
            description="DMZ link",
            vlan_id=20,
            parent_interface_name="ethernet1/1",
            namespace_name="VRF-DMZ",
            zone_members=[segments[dmz_segment["id"]]],
        ),
        _make_firewall_interface(
            name="ethernet1/1.30",
            ip_address="10.0.3.1/30",
            zone_name="external",
            trust_level=0,
            description="External link",
            vlan_id=30,
            parent_interface_name="ethernet1/1",
            namespace_name="VRF-EXTERNAL",
        ),
    ]

    device_node: dict = {
        "__typename": "DcimPhysicalDevice",
        "id": f"dev-{device_name}",
        "name": _v(device_name),
        "role": _v("firewall"),
        "platform": _node(
            {
                "id": f"plat-{platform}",
                "name": _v(platform),
            }
        ),
        "primary_address": _node(None),
        "tags": _edges([]),
        "capabilities": _edges([]),
        "interfaces": _edges(fw_interfaces),
        "deployment": _node(None),
    }

    zones = [
        _make_zone(
            name="internal",
            trust_level=100,
            description="Internal trusted network",
            cidrs=["10.0.1.0/24", "10.0.10.0/24"],
            namespace_name="VRF-INTERNAL",
        ),
        _make_zone(
            name="dmz",
            trust_level=50,
            description="Demilitarized zone",
            cidrs=["10.0.2.0/24"],
            namespace_name="VRF-DMZ",
        ),
        _make_zone(
            name="external",
            trust_level=0,
            description="External untrusted network",
            cidrs=[],
            namespace_name="VRF-EXTERNAL",
        ),
    ]

    return {
        "DcimPhysicalDevice": _edges([device_node]),
        "SecurityZone": _edges(zones),
    }


def build_leaf_internet_permit_data(*, device_name: str, platform: str) -> dict:
    """The with_acl leaf whose VXLAN segment (10.100.0.0/24) also permits https to the internet zone.

    Its ACL must deny every NON_INTERNET_PREFIXES range before the `permit tcp ... any eq 443`
    (a plain permit to any would open every same-VRF destination at the leaf).
    """
    data = build_device_data(
        device_name=device_name,
        role="leaf",
        platform=platform,
        scenario="ebgp_ibgp",
        include_segments=True,
        include_acls=True,
    )
    source = {
        "id": "seg-seg-100",
        "__typename": "ManagedVxlanSegment",
        "name": _v("seg-100"),
        "security_zone": _node({"name": _v("PROD-ZONE")}),
        "gateway": _node({"ip_prefix": _node({"prefix": _v("10.100.0.0/24")})}),
    }
    dest = {
        "id": "seg-seg-200",
        "__typename": "ManagedVxlanSegment",
        "name": _v("seg-200"),
        "security_zone": _node({"name": _v("PROD-ZONE")}),
        "gateway": _node({"ip_prefix": _node({"prefix": _v("10.200.0.0/24")})}),
    }
    # An intra-zone permit first, to show it is not shadowed by the band, then the Internet permit.
    intra = _make_policy_rule(index=10, name="allow-seg-200-ssh", protocol="tcp", port_start=22)
    intra["source_segment"] = _node(source)
    intra["destination_segment"] = _node(dest)
    rule = _make_policy_rule(index=20, name="allow-internet-https", protocol="tcp", port_start=443)
    rule["source_segment"] = _node(source)
    rule["destination_zone"] = _node({"name": _v("INTERNET-ZONE")})
    patched: set[int] = set()
    stack: list[Any] = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            policy = (node.get("security_policy") or {}).get("node")
            # The policy dict is shared by reference between the segment copies: patch it once.
            if isinstance(policy, dict) and policy.get("name") == _v("policy-vxlan-seg") and id(policy) not in patched:
                policy["rules"] = _edges([intra, rule])
                patched.add(id(policy))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    assert patched, "policy-vxlan-seg not found in the leaf fixture"
    return data


def build_leaf_pbr_data(*, device_name: str, platform: str) -> dict:
    """The internet-permit leaf plus a shared firewall context with a leg in the segment's VRF.

    seg-100 (VRF_A) permits ssh to seg-200 (bypassed: normal routing) and https to the Internet
    (never bypassed), so its PBR is: bypass 10.200.0.0/24, everything else to the context's VIP
    (the /29 leg 100.66.0.0/29 -> .4), which the leaf holds as a local transit leg
    (VLAN 3000, VNI 64000). seg-200 is in the default VRF and gets no PBR.
    """
    data = build_leaf_internet_permit_data(device_name=device_name, platform=platform)
    net = ipaddress.ip_network("100.66.0.0/29")
    legs = [
        {
            "name": _v("ethernet1/1.3000"),
            "device": _node({"name": _v(member), "role": _v("firewall")}),
            "ip_address": _node(
                {
                    "address": _v(f"{net[host]}/{net.prefixlen}"),
                    "ip_namespace": _node({"name": _v("VRF_A"), "namespace_type": _v("prod"), "l3_vni": _v(50001)}),
                }
            ),
        }
        for member, host in (("dc1-fw-01", 5), ("dc1-fw-02", 6))
    ]
    context = {
        "id": "ctx-shared",
        "name": _v("dc1-fw-shared"),
        "vlan_id": _v(3000),
        "tenant": _node(None),
        "served_deployments": _edges([]),
        "capabilities": _edges(legs),
    }
    firewall = {
        "node": {
            "capabilities": _edges([{"__typename": "ManagedFirewallHA", "contexts": _edges([context])}]),
        }
    }
    data["DcimDevice"]["edges"][0]["node"]["deployment"]["node"]["devices"] = {"edges": [firewall]}
    return data


def _lb_rule(
    *,
    policy: dict,
    name: str,
    port: int,
    src_segment: dict | None,
    dst_segment: dict | None,
    src_prefix: str | None = None,
    vip: str | None = None,
) -> dict:
    """A rule of ``policy`` (SecurityPolicyRuleFields) from a /29 leg selector or to a VIP /32 selector, in VRF_A."""
    rule = _make_segment_rule(
        policy=policy["name"]["value"],
        index=100,
        name=name,
        port_start=port,
        src_segment=src_segment,
        dst_segment=dst_segment,
    )
    namespace = _node({"name": _v("VRF_A")})
    if src_prefix:
        rule["source_prefixes"] = _edges(
            [{"id": f"pfx-{src_prefix}", "prefix": _v(src_prefix), "ip_namespace": namespace}]
        )
    if vip:
        rule["destination_ip_addresses"] = _edges([{"id": f"ip-{vip}", "address": _v(vip), "ip_namespace": namespace}])
    return rule


def build_leaf_lb_client_data(*, device_name: str, platform: str) -> dict:
    """A PBR leaf (shared firewall context, VRF_A leg) hosting both ends of a load-balanced flow.

    seg-web (10.110.0.0/24) calls the VIP 100.68.0.10/32 of app on tcp/443 (R_client, its own policy: a selector
    in its own VRF, so an ACL permit and a PBR bypass, never the firewall). seg-app (10.120.0.0/24) is the
    backend: the segment-less policy lb-dc1-lb-01-PROD lets the leg 100.67.0.0/29 reach it on tcp/8443
    (R_server, one of seg-app's inbound_rules), so seg-app's SVI ACL gets the established return leg to the
    /29 and its PBR bypasses the /29. Nothing is redirected to the load balancer: it source-NATs to its leg.
    """
    data = build_device_data(device_name=device_name, role="leaf", platform=platform, include_segments=False)
    node = data["DcimDevice"]["edges"][0]["node"]
    domain = node["id"]
    segments = {
        name: _make_segment_node(
            vlan_id=vlan,
            vni=10000 + vlan,
            seg_name=name,
            gateway_ip=gateway,
            ns_name="VRF_A",
            l3_vni=50001,
            vlan_domain_id=domain,
        )
        for name, vlan, gateway in (("seg-web", 110, "10.110.0.1/24"), ("seg-app", 120, "10.120.0.1/24"))
    }
    web, app = segments["seg-web"], segments["seg-app"]
    web_policy = _make_policy_header(name="policy-web", segment=web)
    lb_policy = {
        "id": "policy-lb-dc1-lb-01-PROD",
        "name": _v("lb-dc1-lb-01-PROD"),
        "enabled": _v(True),
        "default_action": _v("deny"),
        "segment": _node(None),
    }
    # A rule end is the PolicyRuleSegmentFields of the segment, never the segment node itself (no cycle).
    web_end, app_end = (
        {key: seg[key] for key in ("id", "__typename", "name", "customer_name", "security_zone", "gateway")}
        for seg in (web, app)
    )
    r_client = _lb_rule(
        policy=web_policy,
        name="web-to-app-vip-tcp-443",
        port=443,
        src_segment=web_end,
        dst_segment=None,
        vip="100.68.0.10/32",
    )
    r_server = _lb_rule(
        policy=lb_policy,
        name="lb-to-app-tcp-8443",
        port=8443,
        src_segment=None,
        dst_segment=app_end,
        src_prefix="100.67.0.0/29",
    )
    web["security_policy"] = _node({**web_policy, "rules": _edges([r_client])})
    web["inbound_rules"] = _edges([])
    app["security_policy"] = _node({**_make_policy_header(name="policy-app", segment=app), "rules": _edges([])})
    app["inbound_rules"] = _edges([{**r_server, "policy": _node(lb_policy)}])
    node["interfaces"]["edges"].append(
        {
            "node": _make_interface(
                name="Ethernet10",
                device_name=device_name,
                description="to server-01",
                role="server",
                ip_address=None,
                segments=[web, app],
            )
        }
    )
    pbr = build_leaf_pbr_data(device_name=device_name, platform=platform)
    node["deployment"]["node"]["devices"] = pbr["DcimDevice"]["edges"][0]["node"]["deployment"]["node"]["devices"]
    return data


def build_firewall_internet_data(*, device_name: str, platform: str) -> dict:
    """A firewall whose internal zone (10.0.1.0/24) permits https to the internet zone.

    The rule table renders it to INTERNET-ZONE with destination any (the zone names no address).
    """
    data = build_firewall_data(device_name=device_name, platform=platform)
    internal = _make_rule_segment(cidr="10.0.1.0/24", zone_name="internal")
    header = _make_policy_header(name="internal-to-internet", segment=internal)
    segments = _make_rule_bearing_segments(
        [internal],
        [
            (
                header,
                [
                    _make_segment_rule(
                        policy="internal-to-internet",
                        index=10,
                        name="allow-internet-https",
                        protocol="tcp",
                        port_start=443,
                        src_segment=internal,
                        dst_zone="INTERNET-ZONE",
                        description="Allow HTTPS from Internal to the Internet",
                    )
                ],
            )
        ],
    )
    device = data["DcimPhysicalDevice"]["edges"][0]["node"]
    device["interfaces"] = _edges(
        [
            _make_firewall_interface(
                name="ethernet1/1.10",
                ip_address="10.0.1.1/30",
                zone_name="internal",
                trust_level=100,
                description="Internal LAN link",
                vlan_id=10,
                parent_interface_name="ethernet1/1",
                namespace_name="VRF-INTERNAL",
                zone_members=[segments[internal["id"]]],
            ),
            _make_firewall_interface(
                name="ethernet1/1.30",
                ip_address="10.0.3.1/30",
                zone_name="INTERNET-ZONE",
                trust_level=0,
                description="Internet link",
                vlan_id=30,
                parent_interface_name="ethernet1/1",
                namespace_name="VRF-EXTERNAL",
            ),
        ]
    )
    data["SecurityZone"] = _edges(
        [
            _make_zone(name="internal", trust_level=100, cidrs=["10.0.1.0/24"], namespace_name="VRF-INTERNAL"),
            _make_zone(name="INTERNET-ZONE", trust_level=0, cidrs=[], namespace_name="VRF-EXTERNAL"),
        ]
    )
    return data


def build_firewall_ha_data(*, device_name: str, platform: str) -> dict:
    """Build a firewall GQL response that includes a ManagedHA capability.

    Same topology as build_firewall_data() but with a ManagedHA capability node
    so the HA template block is exercised.
    """
    data = build_firewall_data(device_name=device_name, platform=platform)

    ha_cap = {
        "__typename": "ManagedFirewallHA",
        "name": _v("dc1-fw-ha"),
        "group_id": _v(1),
        "mode": _v("active-passive"),
        "priority": _v(100),
        "preempt": _v(False),
        "ha_timer": _v("aggressive"),
        "capabilities": _edges(
            [
                {"name": _v("dc1-fw-01")},
                {"name": _v("dc1-fw-02")},
            ]
        ),
    }

    # Inject the HA capability and the HA sync port into the device node
    devices_edges = data["DcimPhysicalDevice"]["edges"]
    devices_edges[0]["node"]["capabilities"] = _edges([ha_cap])
    devices_edges[0]["node"]["interfaces"]["edges"].append(
        {
            "node": {
                "__typename": "DcimPhysicalInterface",
                "name": _v("ethernet1/7"),
                "description": _v("HA sync"),
                "status": _v("active"),
                "role": _v("ha"),
                "ip_address": _node(None),
                "capabilities": _edges([]),
            }
        }
    )

    return data


def build_firewall_inline_data(*, device_name: str, platform: str) -> dict:
    """Firewall HA pair (device = primary member) terminating a segment inline.

    Adds ethernet1/1.210 carrying a terminate_inline VXLAN segment whose
    inline_service is this HA pair: the member's own address is 10.1.0.2/24,
    the secondary's 10.1.0.3/24, and the segment gateway 10.1.0.1/24 is the
    pair's virtual IP.
    """
    data = build_firewall_ha_data(device_name=device_name, platform=platform)
    inline_iface = _make_firewall_interface(
        name="ethernet1/1.210",
        ip_address="10.1.0.1/24",
        zone_name="web",
        trust_level=60,
        description="Inline web segment",
        vlan_id=210,
        parent_interface_name="ethernet1/1",
        namespace_name="PROD",
    )
    inline_iface["ip_address"] = _node({"address": _v("10.1.0.2/24"), "ip_namespace": _node({"name": _v("PROD")})})
    segment = inline_iface["capabilities"]["edges"][0]["node"]
    segment["terminate_inline"] = _v(True)
    segment["inline_service"] = _node({"id": "ha-1", "name": _v("dc1-fw-ha")})
    segment["capabilities"] = _edges(
        [
            {"device": _node({"name": _v("dc1-fw-01")}), "ip_address": _node({"address": _v("10.1.0.2/24")})},
            {"device": _node({"name": _v("dc1-fw-02")}), "ip_address": _node({"address": _v("10.1.0.3/24")})},
        ]
    )
    data["DcimPhysicalDevice"]["edges"][0]["node"]["interfaces"]["edges"].append({"node": inline_iface})
    return data


def write_fixtures() -> tuple[int, int]:
    """Write the firewall fixtures, and the leaf fixtures of the firewall-context features (permit, PBR, LB client)."""
    generated = errors = 0

    # A permit to the internet zone: the leaf denies every
    # internal range before `permit ... any`, the firewall renders INTERNET-ZONE / any.
    print("\nGenerating internet-permit fixtures (leaf, firewall):")
    g, e = write_data_fixtures(
        [
            (
                Leaf,
                "leaf_cisco_nxos_internet_permit",
                build_leaf_internet_permit_data(device_name="dc1-leaf-01", platform="cisco_nxos"),
            ),
            (
                Firewall,
                "firewall_paloalto_panos_internet_permit",
                build_firewall_internet_data(device_name="dc1-fw-01", platform="paloalto_panos"),
            ),
        ]
    )
    generated += g
    errors += e

    # Leaf PBR (default-redirect to the context VIP, bypass for permits): NX-OS route-map, EOS policy-map type pbr,
    # SONiC PBR ACL table. The VIP's transit leg is local to the leaf, so this also covers its EVPN filter.
    print("\nGenerating leaf PBR fixtures:")
    g, e = write_data_fixtures(
        (Leaf, f"leaf_{platform}_pbr", build_leaf_pbr_data(device_name="dc1-leaf-01", platform=platform))
        for platform in ("cisco_nxos", "arista_eos", "sonic", "dell_sonic")
    )
    generated += g
    errors += e
    # Both ends of a load-balanced flow on a PBR leaf: the VIP /32 and the LB leg /29 are bypassed, never redirected.
    g, e = _write_data_fixture(
        Leaf, "leaf_cisco_nxos_lb_client", build_leaf_lb_client_data(device_name="dc1-leaf-01", platform="cisco_nxos")
    )
    generated += g
    errors += e

    # Firewall scenarios: zone-based policy per vendor
    print("\nGenerating firewall fixtures:")
    g, e = write_data_fixtures(
        (Firewall, f"firewall_{platform}_with_policy", build_firewall_data(device_name="dc1-fw-01", platform=platform))
        for platform in FIREWALL_PLATFORMS
    )
    generated += g
    errors += e

    # Firewall HA scenarios: same platforms, with ManagedHA capability
    print("\nGenerating firewall HA fixtures:")
    g, e = write_data_fixtures(
        (Firewall, f"firewall_{platform}_with_ha", build_firewall_ha_data(device_name="dc1-fw-01", platform=platform))
        for platform in FIREWALL_PLATFORMS
    )
    generated += g
    errors += e

    # Firewall HA pair terminating a segment inline: virtual IP + standby per vendor
    print("\nGenerating firewall inline-segment fixtures:")
    g, e = write_data_fixtures(
        (
            Firewall,
            f"firewall_{platform}_inline_segment",
            build_firewall_inline_data(device_name="dc1-fw-01", platform=platform),
        )
        for platform in [*FIREWALL_PLATFORMS, "juniper_junos"]
    )
    generated += g
    errors += e
    return generated, errors
