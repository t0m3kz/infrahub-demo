"""Shared builders for the BGP transform unit tests (GraphQL-shaped peering data)."""


def _make_peering_interfaces(
    local_name: str,
    local_ip: str,
    local_device: str,
    remote_name: str,
    remote_ip: str,
    remote_device: str,
    local_type: str = "DcimPhysicalInterface",
    remote_type: str = "DcimPhysicalInterface",
):
    """Build peering_interfaces list (already flattened from edges)."""
    return [
        {
            "typename": local_type,
            "name": local_name,
            "ip_address": {"address": local_ip},
            "device": {"name": local_device},
        },
        {
            "typename": remote_type,
            "name": remote_name,
            "ip_address": {"address": remote_ip},
            "device": {"name": remote_device},
        },
    ]


def _make_peering(
    *,
    name: str = "test-peering",
    session_type: str = "EBGP",
    ttl: int = 1,
    bfd: bool = True,
    route_reflector_client: bool = False,
    local_device: str = "leaf-01",
    local_ip: str = "10.1.0.1/31",
    remote_device: str = "spine-01",
    remote_ip: str = "10.1.0.0/31",
    local_iface_type: str = "DcimPhysicalInterface",
    remote_iface_type: str = "DcimPhysicalInterface",
    local_asn: int | None = None,
    remote_asn: int | None = None,
    maximum_routes: int | None = None,
    local_pref: int | None = None,
    med: int | None = None,
    send_extended_community: bool = False,
    remove_private_as: bool = False,
    password: str | None = None,
):
    peering = {
        "name": name,
        "session_type": session_type,
        "bfd_enabled": bfd,
        "send_community": True,
        "send_extended_community": send_extended_community,
        "maximum_routes": maximum_routes,
        "local_pref": local_pref,
        "med": med,
        "remove_private_as": remove_private_as,
        "password": {"password": password} if password is not None else None,
        "ttl": ttl,
        "route_reflector_client": route_reflector_client,
        "capabilities": _make_peering_interfaces(
            local_name="Ethernet1",
            local_ip=local_ip,
            local_device=local_device,
            remote_name="Ethernet2",
            remote_ip=remote_ip,
            remote_device=remote_device,
            local_type=local_iface_type,
            remote_type=remote_iface_type,
        ),
    }
    # Add bgp_processes for remote ASN resolution (eBGP)
    if local_asn is not None or remote_asn is not None:
        procs = []
        if local_asn is not None:
            procs.append({"capabilities": [{"name": local_device}], "local_as": {"asn": local_asn}})
        if remote_asn is not None:
            procs.append({"capabilities": [{"name": remote_device}], "local_as": {"asn": remote_asn}})
        peering["bgp_processes"] = procs
    return peering
