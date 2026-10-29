"""Shared builders and writers of the config fixtures: GraphQL response nodes, transform runner, fixture files."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from tests.smoke.fixture_builders.vrf import with_vrf_objects
from transforms.common import BaseDeviceTransform

# ============================================================================
# GraphQL response builders (raw format, before clean_data)
# ============================================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SMOKE_DIR = Path(__file__).resolve().parents[1] / "configs"


def _v(val: Any) -> dict:
    return {"value": val}


def _node(inner: dict | None) -> dict:
    return {"node": inner}


def _edges(nodes: list[dict]) -> dict:
    return {"edges": [{"node": n} for n in nodes]}


def _af_edges(*pairs: tuple[str, str]) -> dict:
    """RoutingBGPAddressFamily edges as the peering query returns them."""
    return _edges([{"afi": _v(afi), "safi": _v(safi)} for afi, safi in pairs])


def _make_bgp_peering(
    *,
    device_name: str,
    device_ip: str,
    device_asn: int,
    remote_name: str,
    remote_ip: str,
    remote_asn: int,
    session_type: str = "EBGP",
    ttl: int = 1,
    bfd: bool = True,
    route_reflector_client: bool = False,
    local_iface_name: str | None = None,
    remote_iface_name: str | None = None,
    send_community: bool = True,
    send_extended_community: bool = False,
    maximum_routes: int | None = None,
    local_pref: int | None = None,
    med: int | None = None,
    remove_private_as: bool = False,
    password: str | None = None,
) -> dict:
    """Build a BGP peering with peering_interfaces and bgp_processes."""
    # For overlay (TTL!=1): interfaces are loopbacks
    # For underlay (TTL=1): interfaces are physical P2P links
    local_iface_type = "DcimVirtualInterface" if ttl != 1 else "DcimPhysicalInterface"
    remote_iface_type = "DcimVirtualInterface" if ttl != 1 else "DcimPhysicalInterface"
    if local_iface_name is None:
        local_iface_name = "Loopback0" if ttl != 1 else "Ethernet1"
    if remote_iface_name is None:
        remote_iface_name = "Loopback0" if ttl != 1 else "Ethernet1/1"

    return {
        "id": f"peering-{device_name}-{remote_name}-{session_type.lower()}",
        "name": _v(f"peer-{device_name}-{remote_name}-{session_type.lower()}"),
        "session_type": _v(session_type),
        "bfd_enabled": _v(bfd),
        "send_community": _v(send_community),
        "send_extended_community": _v(send_extended_community),
        "maximum_routes": _v(maximum_routes),
        "local_pref": _v(local_pref),
        "med": _v(med),
        "remove_private_as": _v(remove_private_as),
        "password": _node({"password": _v(password)}) if password is not None else _node(None),
        "ttl": _v(ttl),
        "route_reflector_client": _v(route_reflector_client),
        "capabilities": _edges(
            [
                {
                    "__typename": local_iface_type,
                    "name": _v(local_iface_name),
                    "ip_address": _node({"address": _v(device_ip)}),
                    "device": _node({"name": _v(device_name)}),
                },
                {
                    "__typename": remote_iface_type,
                    "name": _v(remote_iface_name),
                    "ip_address": _node({"address": _v(remote_ip)}),
                    "device": _node({"name": _v(remote_name)}),
                },
            ]
        ),
        "bgp_processes": _edges(
            [
                {
                    "id": f"bgp-{device_name}",
                    "capabilities": _edges([{"name": _v(device_name)}]),
                    "router_id": _node({"address": _v(device_ip)}),
                    "local_as": _node({"asn": _v(device_asn)}),
                },
                {
                    "id": f"bgp-{remote_name}",
                    "capabilities": _edges([{"name": _v(remote_name)}]),
                    "router_id": _node({"address": _v(remote_ip)}),
                    "local_as": _node({"asn": _v(remote_asn)}),
                },
            ]
        ),
    }


def _make_interface(
    *,
    name: str,
    device_name: str,
    description: str = "",
    role: str = "spine",
    ip_address: str | None = None,
    ns_name: str = "default",
    typename: str = "DcimPhysicalInterface",
    ospf_area: str | None = None,
    ospf_authentication_mode: str | None = None,
    ospf_password: str | None = None,
    remote_name: str | None = None,
    remote_ip: str | None = None,
    remote_device: str | None = None,
    segments: list[dict] | None = None,
) -> dict:
    iface: dict[str, Any] = {
        "__typename": typename,
        "name": _v(name),
        "description": _v(description),
        "status": _v("active"),
        "role": _v(role),
    }

    if typename == "DcimPhysicalInterface":
        iface["interface_type"] = _v("10gbase-x-sfpp")
        iface["mtu"] = _v(9000)

        if ip_address:
            iface["ip_address"] = _node(
                {
                    "address": _v(ip_address),
                    "ip_namespace": _node({"name": _v(ns_name)}),
                }
            )
        else:
            iface["ip_address"] = _node(None)

        if remote_name and remote_ip and remote_device:
            iface["cable"] = _node(
                {
                    "id": f"cable-{name}",
                    "endpoints": _edges(
                        [
                            {
                                "__typename": "DcimPhysicalInterface",
                                "name": _v(name),
                                "ip_address": _node({"address": _v(ip_address or "0.0.0.0/32")}),
                                "device": _node({"name": _v(device_name)}),
                            },
                            {
                                "__typename": "DcimPhysicalInterface",
                                "name": _v(remote_name),
                                "ip_address": _node({"address": _v(remote_ip)}),
                                "device": _node({"name": _v(remote_device)}),
                            },
                        ]
                    ),
                }
            )
        else:
            iface["cable"] = _node(None)
    elif typename == "DcimVirtualInterface":
        if ip_address:
            iface["ip_address"] = _node(
                {
                    "address": _v(ip_address),
                    "ip_namespace": _node({"name": _v(ns_name)}),
                }
            )
        else:
            iface["ip_address"] = _node(None)

    services: list[dict] = []
    if ospf_area is not None:
        services.append(
            {
                "__typename": "RoutingOSPFInterface",
                "name": _v(f"ospf-{name}"),
                "mode": _v("point-to-point"),
                "metric": _v(None),
                "authentication_mode": _v(ospf_authentication_mode),
                "password": _node({"password": _v(ospf_password)}) if ospf_password is not None else _node(None),
                "peering": _node(
                    {
                        "ospf_process": _edges(
                            [
                                {
                                    "process_id": _v(1),
                                    "router_id": _node({"address": _v("10.0.0.1/32")}),
                                    "capabilities": _edges([{"name": _v(device_name)}]),
                                }
                            ]
                        ),
                        "ospf_area": _node(
                            {
                                "area": _v(ospf_area),
                                "area_type": _v("standard"),
                            }
                        ),
                        "network_type": _v("point-to-point"),
                        "cost": _v(None),
                    }
                ),
            }
        )
    # Segments are interface capabilities too — this is the only path the
    # rendering transform reads them from (_collect_activations_from_interfaces).
    if segments:
        services.extend(segments)
    iface["capabilities"] = _edges(services)
    return iface


def _make_policy_rule(
    *,
    index: int,
    name: str,
    action: str = "permit",
    protocol: str = "tcp",
    port_start: int | None = None,
    port_end: int | None = None,
    log: bool = False,
) -> dict:
    return {
        "index": _v(index),
        "name": _v(name),
        "action": _v(action),
        "protocol": _v(protocol),
        "port_start": _v(port_start),
        "port_end": _v(port_end),
        "log": _v(log),
        "disabled": _v(False),
        "source_segment": _node(None),
        "destination_segment": _node(None),
    }


def _make_security_policy(*, name: str, rules: list[dict]) -> dict:
    return {
        "name": _v(name),
        "default_action": _v("deny"),
        "enabled": _v(True),
        "rules": _edges(rules),
    }


def _vlan_domain_rows(vlan_id: int, vlan_domain_id: str | None) -> dict:
    """The ``vlan_domain_segments`` edges of one deployment: the row of this device's own VLAN domain, if any."""
    return _edges([{"vlan_id": _v(vlan_id), "vlan_domain": _node({"id": vlan_domain_id})}] if vlan_domain_id else [])


def _make_segment_node(
    *,
    vlan_id: int,
    vni: int | None = None,
    seg_name: str = "seg-100",
    gateway_ip: str | None = None,
    ns_name: str = "default",
    security_policy: dict | None = None,
    num_deployments: int = 1,
    isolation_mode: str | None = None,
    has_firewall: bool = False,
    vlan_domain_id: str | None = None,
    l3_vni: int | None = None,
    routing_identity: tuple[int | None, int | None] | None = None,
) -> dict:
    """Build a ManagedVxlanSegment node, as the query returns it.

    ``routing_identity`` is the (segment routing_tag, owner routing_tag) the fragment selects
    (queries/fragments/network_segment.gql); without it the node carries neither field, as before.

    This is the shape that hangs off ``interfaces[].capabilities`` —
    the ONLY place BaseDeviceTransform._collect_activations_from_interfaces
    looks for segments, and therefore the only path that produces VXLAN/EVPN
    output for leaf/tor/access-leaf roles.

    The VNI comes from ``segment_deployments`` and the LOCAL
    vlan_id from the ``vlan_domain_segments`` rows under it, matched to this
    device's own VLAN domain — its ManagedMLAG if paired, else its own device id.
    A segment with no row for the device's own domain is skipped entirely
    by the transform, so ``vlan_domain_id`` must match or no VXLAN is rendered.

    The VRF and its L3 VNI are reached ONLY via
    ``gateway.ip_prefix.ip_namespace`` (see utils.segment_addressing.routed_segment_namespace and
    _get_segment_gateways in transforms/helpers/segments.py). A flat
    ``segment.prefix`` key — which this generator used to emit — is read by
    nothing, which is why no fixture ever rendered a VRF or an L3 VNI.
    See queries/fragments/network_segment.gql's NetworkSegmentFields.
    """
    seg: dict[str, Any] = {"id": f"seg-{seg_name}"}

    if isolation_mode is not None:
        seg["isolation_mode"] = _v(isolation_mode)

    # Embed a firewall node reference so acl.py seg_has_firewall detects it
    if has_firewall:
        seg["inline_service"] = _node({"id": "fw-dc1-01", "name": _v("dc1-fw-01")})
    else:
        seg["inline_service"] = _node(None)

    security_zone_node = _node(None)

    # gateway.ip_prefix carries the segment's subnet and its IpamNamespace (the
    # VRF). Derive the prefix from the gateway address so the two stay coherent.
    gateway_node: dict[str, Any] | None = None
    if gateway_ip:
        iface = ipaddress.ip_interface(gateway_ip)
        gateway_node = {
            "address": _v(gateway_ip),
            "ip_prefix": _node(
                {
                    "prefix": _v(str(iface.network)),
                    "ip_namespace": _node({"name": _v(ns_name), "l3_vni": _v(l3_vni)}),
                }
            ),
        }

    seg.update(
        {
            "__typename": "ManagedVxlanSegment",
            "name": _v(seg_name),
            "customer_name": _v("Customer-A"),
            "arp_suppression": _v(True),
            "security_zone": security_zone_node,
            "gateway": _node(gateway_node),
            # The deployment the segment is realized in: its VNI and, under it, the
            # per-VLAN-domain LOCAL vlan_id rows. The transform needs a row whose
            # vlan_domain.id equals this device's own domain or it skips the segment
            # and renders no VXLAN at all; the VNI is the one of the deployment holding
            # that row.
            "segment_deployments": _edges(
                [{"vni": _v(vni), "vlan_domain_segments": _vlan_domain_rows(vlan_id, vlan_domain_id)}]
            ),
        }
    )

    if routing_identity is not None:
        segment_tag, owner_tag = routing_identity
        seg["routing_tag"] = _v(segment_tag)
        seg["owner"] = _node({"id": "cust-c005", "org_id": _v("C005"), "routing_tag": _v(owner_tag)})

    if security_policy is not None:
        seg["security_policy"] = _node(security_policy)

    # deployments list drives stretched-segment detection (_collect_activations_from_interfaces)
    seg["deployments"] = _edges([{"id": f"fake-dc-{i}"} for i in range(num_deployments)])

    return seg


def _deployment_record(seg: dict, vni: int | None) -> dict:
    """A ManagedSegmentDeployment record of a device's deployment for a segment node built by _make_segment_node:
    its VNI and, under the record itself, the rows of the segment's own deployment (a border gateway's VLAN)."""
    rows = (
        seg["segment_deployments"]["edges"][0]["node"]["vlan_domain_segments"]
        if seg.get("segment_deployments")
        else _edges([])
    )
    return {"vni": _v(vni), "status": _v("active"), "vlan_domain_segments": rows, "segment": _node(seg)}


def _make_segment_deployment(**kwargs: Any) -> dict:
    """Wrap a segment node in a ManagedSegmentDeployment record.

    This is the ``TopologySegmentHosting.segment_deployments`` shape, reached
    from a device's own ``deployment`` — used by border-leaf for DC-wide PBR
    rules (see transforms/config/border_leaf.py), NOT for VXLAN. VXLAN comes
    exclusively from capabilities via _make_segment_node.
    """
    seg = _make_segment_node(**kwargs)
    return {
        "vlan_id": _v(kwargs.get("vlan_id")),
        "vni": _v(kwargs.get("vni")),
        "status": _v("active"),
        # A border gateway's own row sits under the deployment record itself.
        "vlan_domain_segments": _vlan_domain_rows(kwargs.get("vlan_id") or 0, kwargs.get("vlan_domain_id")),
        "segment": _node(seg),
    }


def make_transform(transform_cls: type) -> Any:
    """An instance of `transform_cls` with a mocked SDK client (no server), rooted at the repository."""
    mock_client = MagicMock()
    mock_client.clone.return_value = mock_client  # SDK clones client in __init__
    mock_client.schema = MagicMock()
    mock_client.schema.get = AsyncMock(return_value=MagicMock())
    mock_client.execute_graphql = AsyncMock(side_effect=Exception("no server"))

    return transform_cls(
        client=mock_client,
        infrahub_node=MagicMock(),
        root_directory=str(PROJECT_ROOT),
    )


def _render(instance: Any, data: dict) -> str:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(instance.transform(data))
    finally:
        loop.close()


def run_transform(transform_cls: type, data: dict) -> str:
    """Render `data` with `transform_cls`; a device's config is given the VRF instances and SVIs it demands first.

    The unit tests that build their own device data go through here too (the hook is idempotent).
    """
    instance = make_transform(transform_cls)
    if isinstance(instance, BaseDeviceTransform):
        data = with_vrf_objects(instance, data)
    return _render(instance, data)


def _write_data_fixture(transform_cls: type, dir_name: str, data: dict) -> tuple[int, int]:
    """Write `data` as input.json and its rendered config as output.txt.

    The one hook every writer goes through: a device that carries VRFs gets the ManagedVRF instances and SVI
    objects its config demands appended to the input first (tests/smoke/fixture_builders/vrf.py), the way the
    VRF generator leaves them in the model.
    """
    test_dir = SMOKE_DIR / dir_name
    test_dir.mkdir(parents=True, exist_ok=True)

    try:
        instance = make_transform(transform_cls)
        if isinstance(instance, BaseDeviceTransform):
            data = with_vrf_objects(instance, data)
    except Exception as e:
        print(f"  ✗ {dir_name}: {e}")
        return 0, 1

    input_path = test_dir / "input.json"
    with open(input_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    try:
        output = _render(instance, data)
        with open(test_dir / "output.txt", "w") as f:
            # Match end-of-file-fixer's normalization (exactly one trailing
            # newline) so the pre-commit hook never re-touches this file.
            f.write(output.rstrip("\n") + "\n")
        print(f"  ✓ {dir_name}")
        return 1, 0
    except Exception as e:
        print(f"  ✗ {dir_name}: {e}")
        return 0, 1


def write_data_fixtures(fixtures: Iterable[tuple[type, str, dict]]) -> tuple[int, int]:
    """Write every (transform class, fixture directory, input data) triple; return (generated, errors)."""
    generated = errors = 0
    for transform_cls, dir_name, data in fixtures:
        g, e = _write_data_fixture(transform_cls, dir_name, data)
        generated += g
        errors += e
    return generated, errors
