"""The extra query roots the route protection reads (queries/config/*.gql), as raw GraphQL for the smoke fixtures."""

from __future__ import annotations

from tests.smoke.fixture_builders.common import _edges, _node, _v
from tests.smoke.fixture_builders.vrf import routing_vrf, routing_zone

# The routed space of the fixtures: the segment 10.100.0.0/24 in PROD
TENANT_NAMESPACE = "PROD"
TENANT_PREFIXES = ("10.100.0.0/24", "10.100.8.0/24")
INTERNET_PREFIXES = ("203.0.113.0/24",)


def prefix_node(prefix: str, namespace: str, vrf_type: str) -> dict:
    """One ``IpamPrefix`` node of an ``environment_prefixes_*`` root."""
    return {
        "prefix": _v(prefix),
        "ip_namespace": {"node": {"name": {"value": "default"}}},
    }


def _members(names: list[str], roles: dict[str, str]) -> dict:
    """The ``shared_devices`` edges of a VRF instance: each member with its role (a border originates the tenant default)."""
    return _edges([{"name": _v(name), "role": _v(roles.get(name, "leaf"))} for name in names])


def add_environment_roots(
    data: dict, *, tenant_members: list[str], internet_members: list[str], roles: dict[str, str] | None = None
) -> None:
    """Add the environment prefixes, the VRF instances and the segments to a device query response."""
    roles = roles or {}
    data["environment_zones"] = _edges(
        [
            {
                **routing_zone(name),
                "vrfs": _edges(
                    [
                        {
                            **routing_vrf(name),
                            "session_circuits": _edges(
                                [
                                    {
                                        "name": _v(f"fixture-registration-{name}"),
                                        "vrf": _node(routing_vrf(name)),
                                        "registered_prefixes": _edges(
                                            [prefix_node(prefix, name, purpose) for prefix in prefixes]
                                        ),
                                    }
                                ]
                            ),
                        }
                    ]
                ),
            }
            for name, purpose, prefixes in (
                (TENANT_NAMESPACE, "prod", TENANT_PREFIXES),
                ("INTERNET", "internet", INTERNET_PREFIXES),
            )
        ]
    )
    data["ManagedVRF"] = _edges(
        [
            {
                "security_zone": _node(routing_zone(TENANT_NAMESPACE)),
                "shared_devices": _members(tenant_members, roles),
            },
            {
                "security_zone": _node(routing_zone("INTERNET")),
                "shared_devices": _members(internet_members, roles),
            },
        ]
    )
    data["ManagedVxlanSegment"] = _edges([{"id": "segment-1"}, {"id": "segment-2"}])
