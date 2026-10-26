"""Shared builders for the external VRF peer unit tests (split from test_external_vrf_peers.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parents[2]
_PROD = {"name": "PROD", "namespace_type": "prod"}
_NON_PROD = {"name": "NON-PROD", "namespace_type": "non_prod"}


def _owner(org_id: str, typename: str = "OrganizationCustomer") -> dict[str, Any]:
    """A cleaned owner dict as the edge query returns it."""
    return {"typename": typename, "org_id": org_id, "display_label": f"{org_id} label"}


_C2, _C3, _C5, _C15 = _owner("C002"), _owner("C003"), _owner("C005"), _owner("C015")
_PROVIDER = _owner("P027", "OrganizationProvider")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _peering(namespace: dict[str, Any] | None, *, peering_role: str | None = "regular") -> dict[str, Any]:
    """The eg-fr201 <-> EXT-AWS-DX-FR2 VIF peering as the transform sees it, local address in ``namespace``."""
    return {
        "name": "VRF-PROD-EQXFR2-AWS-EUC1",
        "session_type": "EBGP",
        "peering_role": peering_role,
        "ttl": 1,
        "maximum_routes": 100,
        "capabilities": [
            {
                "name": "Ethernet1/13.1000",
                "device": {"name": "eg-fr201"},
                "ip_address": {"address": "10.255.5.5/30", "ip_namespace": namespace},
            },
            {
                "name": "et-0/0/0.1000",
                "device": {"name": "EXT-AWS-DX-FR2"},
                "ip_address": {"address": "10.255.5.6/30", "ip_namespace": namespace},
            },
        ],
        "bgp_processes": [
            {"local_as": {"asn": 4200000100}, "capabilities": [{"name": "eg-fr201"}]},
            {"local_as": {"asn": 64512}, "capabilities": [{"name": "EXT-AWS-DX-FR2"}]},
        ],
    }


def _subif(name: str, namespace: str | None) -> dict[str, Any]:
    return {"name": name, "ip_address": {"address": "10.255.5.5/30", "ip_namespace": {"name": namespace}}}


_GW1 = "EQX-FR2-SDWAN-GW1"


def _bgp_facing(*ends: tuple[str, str]) -> list[dict[str, Any]]:
    """Cleaned device capabilities: one ManagedBGP whose peerings run eg-fr201 (local iface) -> remote device."""
    return [
        {"typename": "ManagedMLAG", "name": "mlag"},
        {
            "typename": "ManagedBGP",
            "peerings": [
                {
                    "name": f"peering-{local}-{remote}",
                    "capabilities": [
                        {"name": local, "device": {"name": "eg-fr201"}},
                        {"name": "eth0.1900", "device": {"name": remote}},
                    ],
                }
                for local, remote in ends
            ],
        },
    ]
