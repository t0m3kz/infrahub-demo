"""Shared builders for the external VRF peer unit tests (split from test_external_vrf_peers.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.data_cleaning import clean_data

_ROOT = Path(__file__).parents[2]
_PROD = {"name": "PROD", "vrf_type": "prod"}
_NON_PROD = {"name": "NONPROD", "vrf_type": "non_prod"}


def _owner(org_id: str, typename: str = "OrganizationCustomer") -> dict[str, Any]:
    """A cleaned owner dict as the edge query returns it."""
    return clean_data({"__typename": typename, "org_id": {"value": org_id}, "display_label": f"{org_id} label"})


_C2, _C3, _C5, _C15 = _owner("C002"), _owner("C003"), _owner("C005"), _owner("C015")
_PROVIDER = _owner("P027", "OrganizationProvider")


def _raw_vrf(namespace: str = "PROD", deployment: str = "FR") -> dict[str, Any]:
    """The raw selected context used by office and tunnel registrants."""
    return {
        "id": f"vrf-{deployment}-{namespace}",
        "deployment": {"node": {"id": f"dep-{deployment}", "name": {"value": deployment}}},
        "security_zone": {
            "node": {
                "id": f"zone-{namespace}",
                "name": {"value": namespace},
                "vrf_type": {"value": "prod" if namespace == "PROD" else "non_prod"},
                "l3_vni": {"value": 10001 if namespace == "PROD" else 10002},
            }
        },
    }


def _raw_in_vrf(vrf: dict[str, Any] | None) -> dict[str, Any]:
    """The raw capability that puts an interface in ``vrf`` (the ManagedVRF among its ``zone_capabilities``)."""
    return {"zone_capabilities": {"edges": [{"node": {"__typename": "ManagedVRF", **vrf}}] if vrf else []}}


def _raw_gateway(gateway: str, namespace: str = "PROD") -> dict[str, Any]:
    """A gateway's explicit handoff capability supplies the exported office context."""
    vrf = _raw_vrf(namespace)
    return {
        "name": {"value": gateway},
        "interfaces": {
            "edges": [
                {
                    "node": {
                        "name": {"value": "eth0.1900"},
                        **_raw_in_vrf(vrf),
                        "capabilities": {
                            "edges": [
                                {
                                    "node": {
                                        "__typename": "TopologySessionCircuit",
                                        "circuit_type": {"value": "handoff"},
                                        "vrf": {"node": vrf},
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _peering(namespace: dict[str, Any] | None, *, peering_role: str | None = "regular") -> dict[str, Any]:
    """The eg-fr201 <-> EXT-AWS-DX-FR2 VIF peering as the transform sees it, local address in ``namespace``."""
    raw: dict[str, Any] = {
        "name": {"value": "VRF-PROD-EQXFR2-AWS-EUC1"},
        "session_type": {"value": "EBGP"},
        "peering_role": {"value": peering_role},
        "ttl": {"value": 1},
        "capabilities": {
            "edges": [
                {
                    "node": {
                        **_raw_in_vrf(
                            _raw_vrf(namespace["name"], deployment) if namespace and deployment == "FR" else None
                        ),
                        "name": {"value": port},
                        "device": {"node": {"name": {"value": device}}},
                        "ip_address": {
                            "node": {
                                "address": {"value": address},
                                "ip_namespace": {"node": {"name": {"value": "default"}}},
                            }
                        },
                    }
                }
                for device, port, address, deployment in (
                    ("eg-fr201", "Ethernet1/13.1000", "10.255.5.5/30", "FR"),
                    ("EXT-AWS-DX-FR2", "et-0/0/0.1000", "10.255.5.6/30", "PROVIDER"),
                )
            ]
        },
        "bgp_processes": {
            "edges": [
                {
                    "node": {
                        "local_as": {"node": {"asn": {"value": asn}}},
                        "capabilities": {"edges": [{"node": {"name": {"value": device}}}]},
                    }
                }
                for device, asn in (("eg-fr201", 4200000100), ("EXT-AWS-DX-FR2", 64512))
            ]
        },
    }
    for edge in raw["capabilities"]["edges"]:
        capabilities: Any = edge["node"]["zone_capabilities"]
        for vrf in (cap["node"] for cap in capabilities["edges"]):
            if namespace is None:
                continue
            vrf["security_zone"]["node"] = {
                key: value if key == "id" else {"value": value}
                for key, value in {"id": f"zone-{namespace['name']}", **namespace}.items()
            }
    return clean_data(raw)


def _subif(name: str, namespace: str | None) -> dict[str, Any]:
    return clean_data(
        {
            **_raw_in_vrf(_raw_vrf(namespace) if namespace else None),
            "name": {"value": name},
            "ip_address": {
                "node": {
                    "address": {"value": "10.255.5.5/30"},
                    "ip_namespace": {"node": {"name": {"value": "default"}}},
                }
            },
        }
    )


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
