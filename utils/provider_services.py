"""How a provider's connections look: addressing, session count and session shape, one frozen row per flavour.

A provider knows its own model, so ``OrganizationProvider.connection_profile`` keys the table. The row for
(profile, path_type) says where the link addresses of a session come from, how long the link prefix is, how many
sessions (links) one ``TopologyProviderConnection`` carries and which end takes the lower address. The row for a
connection with no profile or an unknown pair is ``DEFAULT_FLAVOUR``: today's handoff behaviour (a /31 from our pool).

Which provider a connection has is derived ONCE, by :func:`connection_provider`, over the cleaned payload of a
``TopologyProviderConnection`` (``generators/helpers/provider_connection.py``, ``generators/topology/virtual_circuit.py``
and ``checks/provider_connections.py`` all call it; their queries select the same fields).

Consumers: ``generators/topology/provider_connection.py`` (how many sessions, the link prefixes),
``generators/topology/virtual_circuit.py`` (the length of the allocation, the offsets),
``checks/provider_connections.py`` (PC3, PC7). Change a row here and nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ADDRESS_SOURCE_OUR_POOL = "our_pool"
ADDRESS_SOURCE_PROVIDER = "provider_assigned"
END_US = "us"
END_PROVIDER = "provider"

PATH_DEDICATED = "dedicated"
PATH_INTERNET_VPN = "internet_vpn"
PATH_TYPES = (PATH_DEDICATED, PATH_INTERNET_VPN)
SERVICES = ("private", "transit", "public")

# Host offsets inside a link prefix of a given length: (lower end, upper end). A /31 has no network or broadcast
# address (RFC 3021) so its two hosts are offsets 0 and 1; a /30 or /29 keeps the network address, so 1 and 2.
LINK_OFFSETS: dict[int, tuple[int, int]] = {31: (0, 1), 30: (1, 2), 29: (1, 2)}


@dataclass(frozen=True)
class ConnectionFlavour:
    """The addressing and session model of one (provider profile, path type)."""

    address_source: str  # ADDRESS_SOURCE_OUR_POOL or ADDRESS_SOURCE_PROVIDER
    prefix_length: int  # of one session's link prefix
    links: int  # sessions (session circuits) one connection carries
    lower_end: str = END_US  # END_US or END_PROVIDER: the end that takes the lower host address
    ttl: int | None = None  # eBGP multihop TTL of the session, None = directly connected
    bfd: bool = False  # BFD on the session (handoff and vpn alike): on for a dedicated path, off over the internet


DEFAULT_FLAVOUR = ConnectionFlavour(ADDRESS_SOURCE_OUR_POOL, 31, 1, bfd=True)

# Measured against the vendors' own documentation, not a lab: the rows are the model, not a rendered syntax.
FLAVOURS: dict[tuple[str, str], ConnectionFlavour] = {
    # Direct Connect: the customer specifies the peer addresses, so they come from OUR pool. Site-to-Site VPN: AWS
    # hands a 169.254/30 per tunnel, two tunnels, the provider end is the lower one (AWS takes .1).
    ("aws", PATH_DEDICATED): ConnectionFlavour(ADDRESS_SOURCE_OUR_POOL, 31, 1, bfd=True),
    ("aws", PATH_INTERNET_VPN): ConnectionFlavour(ADDRESS_SOURCE_PROVIDER, 30, 2, END_PROVIDER),
    # ExpressRoute: Microsoft takes a /30 per link, the customer end is the lower address. A connection is ONE leg
    # (one circuit, one /30): the primary and the secondary leg are two connections, related by the carriers'
    # `redundancy_group`.
    ("azure", PATH_DEDICATED): ConnectionFlavour(ADDRESS_SOURCE_PROVIDER, 30, 1, END_US, bfd=True),
    ("azure", PATH_INTERNET_VPN): ConnectionFlavour(ADDRESS_SOURCE_PROVIDER, 30, 2, END_PROVIDER),
    # Cloud Interconnect: a link-local /29 per VLAN attachment; Cloud VPN: a /30 per tunnel.
    ("gcp", PATH_DEDICATED): ConnectionFlavour(ADDRESS_SOURCE_PROVIDER, 29, 1, END_PROVIDER, bfd=True),
    ("gcp", PATH_INTERNET_VPN): ConnectionFlavour(ADDRESS_SOURCE_PROVIDER, 30, 2, END_PROVIDER),
    ("oci", PATH_DEDICATED): ConnectionFlavour(ADDRESS_SOURCE_OUR_POOL, 31, 1, bfd=True),
    ("oci", PATH_INTERNET_VPN): ConnectionFlavour(ADDRESS_SOURCE_PROVIDER, 30, 2, END_PROVIDER),
    # A layer 2 service: the session is ours or the far party's, addressed from our pool.
    ("equinix", PATH_DEDICATED): ConnectionFlavour(ADDRESS_SOURCE_OUR_POOL, 31, 1, bfd=True),
    ("megaport", PATH_DEDICATED): ConnectionFlavour(ADDRESS_SOURCE_OUR_POOL, 31, 1, bfd=True),
    # Tunnels to the nearest Zscaler node: two tunnels (primary and secondary node), /30 from our pool.
    ("zscaler", PATH_INTERNET_VPN): ConnectionFlavour(ADDRESS_SOURCE_OUR_POOL, 30, 2, ttl=2),
}


def flavour_for(profile: str | None, path_type: str | None) -> ConnectionFlavour:
    """The flavour of a (profile, path type); ``DEFAULT_FLAVOUR`` for a missing profile or an unlisted pair."""
    return FLAVOURS.get((profile or "", path_type or ""), DEFAULT_FLAVOUR)


def link_offsets(prefix_length: int) -> tuple[int, int]:
    """The (lower, upper) host offsets inside a link prefix of this length; ValueError for an unsupported length."""
    try:
        return LINK_OFFSETS[prefix_length]
    except KeyError as exc:
        raise ValueError(f"unsupported link prefix length /{prefix_length}") from exc


def end_offsets(flavour: ConnectionFlavour) -> tuple[int, int]:
    """The host offsets of (our end, the provider's end) of a link prefix, per ``lower_end``."""
    lower, upper = link_offsets(flavour.prefix_length)
    return (lower, upper) if flavour.lower_end == END_US else (upper, lower)


def _dict(value: Any) -> Mapping[str, Any]:
    """A cleaned relationship as a mapping: a node that selected only ``id`` (the id string) or is empty has no fields."""
    return value if isinstance(value, Mapping) else {}


def _ref(value: Any) -> str | None:
    """The id of a cleaned reference (the bare id string clean_data leaves for a node that selected only ``id``)."""
    if isinstance(value, Mapping):
        value = value.get("id")
    return str(value) if value else None


def _provider_row(candidate: Any) -> dict[str, Any] | None:
    """``{"id", "name", "connection_profile"}`` of a cleaned OrganizationProvider; None for an empty slot or no id."""
    row = _dict(candidate)
    if not row.get("id"):
        return None
    return {"id": row["id"], "name": row.get("name"), "connection_profile": row.get("connection_profile")}


def _kit_owner(connection: Mapping[str, Any]) -> dict[str, Any] | None:
    """The OrganizationProvider that owns the far kit: a carrier end deployed in the far facility, else its stubs."""
    far = _dict(connection.get("far"))
    far_id = _ref(far)
    owners: list[Any] = []
    for carrier in connection.get("carrier_circuits") or []:
        for end in _dict(carrier).get("capabilities") or []:
            device = _dict(_dict(end).get("device"))
            if far_id and _ref(device.get("deployment")) == far_id:
                owners.append(device.get("owner"))
    for key in ("devices", "appliances"):
        owners.extend(_dict(device).get("owner") for device in far.get(key) or [])
    for owner in owners:
        if _dict(owner).get("typename") == "OrganizationProvider":
            row = _provider_row(owner)
            if row:
                return row
    return None


def connection_provider(connection: Mapping[str, Any]) -> dict[str, Any] | None:
    """The provider of a cleaned ``TopologyProviderConnection``: ``{"id", "name", "connection_profile"}``, else None.

    The first rung that names a provider (with an id) wins:

    1. ``account.provider``: a cloud;
    2. ``far.provider``: an external provider's own;
    3. ``far.parent.provider``: the SaaS cloud of a SaaS region;
    4. ``far.account.provider``: the account a provider-hosted presence is billed to;
    5. the OrganizationProvider that owns the far kit (``device.owner`` of a carrier end deployed in the far facility,
       else of a device or appliance the far facility hosts): a facility with no provider of its own, such as a
       colocation zone of a cloud-neutral operator, whose router is the operator's.

    The profile may be None (a provider with no ``connection_profile``): the flavour is then ``DEFAULT_FLAVOUR``.
    """
    far = _dict(connection.get("far"))
    for candidate in (
        _dict(connection.get("account")).get("provider"),
        far.get("provider"),
        _dict(far.get("parent")).get("provider"),
        _dict(far.get("account")).get("provider"),
    ):
        row = _provider_row(candidate)
        if row:
            return row
    return _kit_owner(connection)


def connection_flavour(connection: Mapping[str, Any]) -> ConnectionFlavour:
    """The flavour of a cleaned connection: its provider's profile and its ``path_type``."""
    provider = connection_provider(connection)
    return flavour_for(provider["connection_profile"] if provider else None, connection.get("path_type"))
