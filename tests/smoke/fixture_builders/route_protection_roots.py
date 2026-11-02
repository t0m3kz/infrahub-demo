"""The extra query roots the route protection reads (queries/config/*.gql), as raw GraphQL for the smoke fixtures.

``transforms/helpers/bgp_facts.py`` derives the EVPN filters from the prefixes of the environment namespaces, the VRF
instances (a border gateway in the INTERNET one is the default exit) and the number of L2 VNIs. A fixture that wants
the EVPN filters adds these roots next to its device root.
"""

from __future__ import annotations

from tests.smoke.fixture_builders.common import _edges, _node, _v

# The routed space of the fixtures: the segment 10.100.0.0/24 in VRF_A (a tenant namespace of the fixtures), one more
# prefix registered there, and the public aggregate in INTERNET.
TENANT_NAMESPACE = "VRF_A"
TENANT_PREFIXES = ("10.100.0.0/24", "10.100.8.0/24")
INTERNET_PREFIXES = ("203.0.113.0/24",)


def prefix_node(prefix: str, namespace: str, namespace_type: str) -> dict:
    """One ``IpamPrefix`` node of an ``environment_prefixes_*`` root."""
    return {
        "prefix": _v(prefix),
        "ip_namespace": _node({"name": _v(namespace), "namespace_type": _v(namespace_type)}),
    }


def _members(names: list[str], roles: dict[str, str]) -> dict:
    """The ``shared_devices`` edges of a VRF instance: each member with its role (a border originates the tenant default)."""
    return _edges([{"name": _v(name), "role": _v(roles.get(name, "leaf"))} for name in names])


def add_environment_roots(
    data: dict, *, tenant_members: list[str], internet_members: list[str], roles: dict[str, str] | None = None
) -> None:
    """Add the environment prefixes, the VRF instances and the segments to a device query response.

    ``tenant_members`` / ``internet_members`` are the devices in the tenant and in the INTERNET VRF instance, ``roles``
    their roles by name (a device not named is a leaf).
    """
    roles = roles or {}
    data["environment_prefixes_prod"] = _edges([prefix_node(p, TENANT_NAMESPACE, "prod") for p in TENANT_PREFIXES])
    data["environment_prefixes_non_prod"] = _edges([])
    data["environment_prefixes_internet"] = _edges([prefix_node(p, "INTERNET", "internet") for p in INTERNET_PREFIXES])
    data["environment_prefixes_management"] = _edges([])
    data["ManagedVRF"] = _edges(
        [
            {
                "namespace": _node({"name": _v(TENANT_NAMESPACE), "namespace_type": _v("prod")}),
                "shared_devices": _members(tenant_members, roles),
            },
            {
                "namespace": _node({"name": _v("INTERNET"), "namespace_type": _v("internet")}),
                "shared_devices": _members(internet_members, roles),
            },
        ]
    )
    data["ManagedVxlanSegment"] = _edges([{"id": "segment-1"}, {"id": "segment-2"}])
