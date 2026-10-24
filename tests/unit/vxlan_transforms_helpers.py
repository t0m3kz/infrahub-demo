"""Shared builders for the VXLAN transform unit tests (split from test_vxlan_transforms.py)."""


# ---------------------------------------------------------------------------
# Helpers: activation structures (as returned by clean_data())
# ---------------------------------------------------------------------------


def _make_activation(
    *,
    vlan_id: int = 100,
    vni: int | None = 10100,
    gateway_ip: str | None = None,
    arp_suppression: bool = True,
    customer_name: str = "seg-100",
    ns_name: str = "default",
    l3_vni: int | None = None,
    owner_name: str | None = None,
) -> dict:
    """Build a cleaned SegmentDeployment dict as returned by clean_data()."""
    ns: dict = {"name": ns_name}
    if l3_vni is not None:
        ns["l3_vni"] = l3_vni
    if owner_name is not None:
        ns["owner"] = {"name": owner_name}

    gateway: dict = {"ip_prefix": {"ip_namespace": ns}}
    if gateway_ip is not None:
        gateway["address"] = gateway_ip

    segment: dict = {
        "name": f"Owner - production - {customer_name}",
        "customer_name": customer_name,
        "arp_suppression": arp_suppression,
        "gateway": gateway,
    }

    return {
        "vlan_id": vlan_id,
        "vni": vni,
        "status": "active",
        "segment": segment,
    }
