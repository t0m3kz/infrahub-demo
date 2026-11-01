"""Validate the decommission of provider connections: what is left behind (rules PC14 and PC15).

A ``TopologyProviderConnection`` set to ``status: decommissioned`` makes ``add_provider_connection`` tear down what it
built: the session circuits, the sub-interfaces / tunnel interfaces of the legs with their addresses, the peerings
``<connection>-L<n>-bgp``, the link prefixes and, when untouched, the generated carrier ``<connection>-XC`` (adopted by a
person: set to ``decommissioned`` and left listed). The connection itself stays for a person to delete. Shared things
(stub devices, their Loopback0 and BGP process, physical ports) are never touched. Targetless; it deletes nothing.

PC14 (decommissioned connections, the other provider rules exempt them)
     ERROR: the connection still has a session circuit, whatever its status (they must be gone)
     WARNING: a live carrier named ``<connection>-XC`` is still listed (the generator deletes it or sets it
     ``decommissioned``; a carrier of another name is a person's and is left alone)

PC15 (every connection, deleted or not) WARNING, conservative, only what an EDGE shows:
     - a sub-interface ``<port>.<n>`` or ``Tunnel<n>`` that is an end of no session circuit, on a port of role
       cloud / saas / partner (our side) or on a ``CloudNetworkAppliance`` of role provider (the service edge, whose NIC
       ``vnic-<connection>`` too when nothing hangs on it, not even a carrier). A sub-interface of a shared stub device
       (or of any other port) is never reported: a stub is a plain device whose ports other connections use.
     - a child prefix of a link pool (``Handoff-<namespace>-IPv4``, ``utils/limits.py`` HANDOFF_POOL_TEMPLATE) that holds
       no address on any interface, or an address of such a prefix on no interface
     - a ``ManagedBGPPeering`` named ``<session>-bgp`` / ``-underlay`` / ``-evpn`` (role regular, dci, dci-underlay)
       whose ``<session>`` is no session circuit and whose interfaces are gone or all reported by the first rule (the
       BGP process is shared and never the orphan)

NOT edge-based, hence not checked: the allocation identifier ``vc-p2p__<session id>`` lives in the pool's allocation
record and is readable from no prefix or address. An identifier-based sweep would need the pool allocations (the
resource manager's per-identifier records) to ask "does a session circuit with this id exist". Nor is the
address-on-no-interface read from the address (``IpamIPAddress`` has no reverse edge to its interface): the check reads
every interface's ``ip_address`` and compares ids. A prefix allocated by a run that has not yet addressed its ends is
reported too (a warning that goes away on the next run).
"""

from __future__ import annotations

import re
from typing import Any

from checks.base import WarningCheck
from utils.circuit_underlay import DECOMMISSIONED, subinterface_vlan
from utils.data_cleaning import clean_data
from utils.limits import HANDOFF_POOL_TEMPLATE

CONNECTION = "TopologyProviderConnection"
SESSION = "TopologySessionCircuit"
VIRTUAL_INTERFACE = "DcimVirtualInterface"
APPLIANCE = "CloudNetworkAppliance"
PROVIDER_PORT_ROLES = frozenset({"cloud", "saas", "partner"})
PROVIDER_DEVICE_ROLE = "provider"
NIC_PREFIX = "vnic-"
GENERATED_CARRIER_SUFFIX = "-XC"
_TUNNEL_NAME = re.compile(r"Tunnel\d+")
SESSION_PEERING_SUFFIXES = ("-bgp", "-underlay", "-evpn")
SESSION_PEERING_ROLES = frozenset({"regular", "dci", "dci-underlay"})
# "Handoff-{namespace}-IPv4" up to the namespace: the pools whose children are session link prefixes.
LINK_POOL_PREFIX = HANDOFF_POOL_TEMPLATE.split("{", 1)[0]


def _dicts(values: Any) -> list[dict[str, Any]]:
    return [value for value in values or [] if isinstance(value, dict)]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ref(value: Any) -> str | None:
    """The id of a cleaned reference: clean_data collapses an object with only an ``id`` to the bare id."""
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value else None


def _ids(values: Any) -> set[str]:
    return {ref for ref in (_ref(value) for value in values or []) if ref}


def _name(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "<unnamed>")


def _is_leg_interface(name: str) -> bool:
    """``<port>.<vlan>`` or ``Tunnel<n>``: the interfaces the provider connection generator writes."""
    return subinterface_vlan(name) is not None or _TUNNEL_NAME.fullmatch(name) is not None


class CheckProviderDecommission(WarningCheck):
    """A decommissioned provider connection is empty (PC14), and nothing is left of deleted ones (PC15)."""

    query = "provider_decommission"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        sessions = _dicts(cleaned.get(SESSION))
        self._validate_decommissioned(_dicts(cleaned.get(CONNECTION)), sessions)

        interfaces = _dicts(cleaned.get("DcimHasIPAddress"))
        session_ends = {end for session in sessions for end in _ids(session.get("capabilities"))}
        orphans = self._orphan_interfaces(interfaces, session_ends)
        held = {address for address in (_ref(i.get("ip_address")) for i in interfaces) if address}
        self._orphan_prefixes(_dicts(cleaned.get("CoreIPPrefixPool")), held)
        self._orphan_peerings(_dicts(cleaned.get("ManagedBGPPeering")), {s.get("name") for s in sessions}, orphans)

    # ------------------------------------------------------------------
    # PC14
    # ------------------------------------------------------------------

    def _validate_decommissioned(self, connections: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> None:
        by_connection: dict[str, list[dict[str, Any]]] = {}
        for session in sessions:
            connection_id = _ref(session.get("connection"))
            if connection_id:
                by_connection.setdefault(connection_id, []).append(session)
        for connection in connections:
            if connection.get("status") != DECOMMISSIONED:
                continue
            name = _name(connection)
            left = by_connection.get(str(connection.get("id")), [])
            if left:
                self._error(
                    connection,
                    f"PC14: decommissioned connection '{name}' still has {len(left)} session circuit(s) "
                    f"({', '.join(sorted(_name(s) for s in left))}): add_provider_connection removes them when it runs "
                    "for a decommissioned connection; rerun it, or delete them by hand.",
                )
            for carrier in _dicts(connection.get("carrier_circuits")):
                if carrier.get("status") != DECOMMISSIONED and _name(carrier) == f"{name}{GENERATED_CARRIER_SUFFIX}":
                    self.log_warning(
                        message=f"PC14: decommissioned connection '{name}' still lists its generated carrier "
                        f"'{_name(carrier)}' as {carrier.get('status')}: the generator deletes an untouched one and "
                        "sets an adopted one to decommissioned; rerun it, or delete the carrier by hand."
                    )

    # ------------------------------------------------------------------
    # PC15
    # ------------------------------------------------------------------

    def _orphan_interfaces(self, interfaces: list[dict[str, Any]], session_ends: set[str]) -> set[str]:
        """Report and return the ids of the sub-interfaces of the provider edge that belong to no session circuit."""
        orphans: set[str] = set()
        for interface in interfaces:
            if interface.get("typename") != VIRTUAL_INTERFACE or str(interface.get("id")) in session_ends:
                continue
            name = str(interface.get("name") or "")
            device = _dict(interface.get("device"))
            parent = _dict(interface.get("parent_interface"))
            service_edge = device.get("typename") == APPLIANCE and device.get("role") == PROVIDER_DEVICE_ROLE
            near_side = parent.get("role") in PROVIDER_PORT_ROLES
            carried = bool(_dicts(interface.get("capabilities")))
            if _is_leg_interface(name) and (near_side or service_edge):
                where = f"on {parent.get('name')} ({parent.get('role')})" if near_side else "of the service edge"
            elif service_edge and name.startswith(NIC_PREFIX) and not carried:
                where = "of the service edge"
            else:
                continue
            orphans.add(str(interface.get("id")))
            self._warn(
                f"PC15: interface '{name}' {where} of device '{device.get('name')}' is an end of no session circuit: "
                "the generator removes it with its connection's session, so its connection was deleted or its "
                "sessions torn down by hand. Delete the interface and its address by hand (nothing is deleted "
                "automatically: a person may have adopted it)."
            )
        return orphans

    def _orphan_prefixes(self, pools: list[dict[str, Any]], held: set[str]) -> None:
        for pool in pools:
            if not str(pool.get("name") or "").startswith(LINK_POOL_PREFIX):
                continue
            for resource in _dicts(pool.get("resources")):
                for child in _dicts(resource.get("children")):
                    self._orphan_prefix(pool, child, held)

    def _orphan_prefix(self, pool: dict[str, Any], child: dict[str, Any], held: set[str]) -> None:
        prefix = child.get("prefix")
        addresses = _dicts(child.get("ip_addresses"))
        free = [a for a in addresses if str(a.get("id")) not in held]
        if len(free) == len(addresses):
            listed = f" and its address(es) {', '.join(str(a.get('address')) for a in free)}" if free else ""
            self._warn(
                f"PC15: link prefix {prefix}{listed} from pool '{pool.get('name')}' is on no interface: the session "
                "circuit that allocated it (identifier vc-p2p__<session id>) is gone or was never addressed. Delete "
                "the addresses and the prefix by hand to free the range (the pool allocation is not readable here, so "
                "nothing is deleted automatically)."
            )
            return
        for address in free:
            self._warn(
                f"PC15: address {address.get('address')} of link prefix {prefix} (pool '{pool.get('name')}') is on no "
                "interface while the other end is: delete it by hand (nothing is deleted automatically)."
            )

    def _orphan_peerings(self, peerings: list[dict[str, Any]], session_names: set[Any], orphans: set[str]) -> None:
        for peering in peerings:
            name = str(peering.get("name") or "")
            suffix = next((s for s in SESSION_PEERING_SUFFIXES if name.endswith(s)), None)
            if not suffix or peering.get("peering_role") not in SESSION_PEERING_ROLES:
                continue
            if name[: -len(suffix)] in session_names:
                continue
            ends = _ids(peering.get("capabilities"))
            if ends and not ends <= orphans:
                continue
            self._warn(
                f"PC15: BGP peering '{name}' belongs to no session circuit '{name[: -len(suffix)]}' and rides on "
                f"{'no interface' if not ends else 'interfaces that belong to no session either'}: delete the peering "
                "by hand (its BGP processes are shared and stay; nothing is deleted automatically)."
            )

    def _warn(self, message: str) -> None:
        self.log_warning(message=message)

    def _error(self, node: dict[str, Any], message: str) -> None:
        self.log_error(message=message, object_id=node.get("id"), object_type=CONNECTION)
