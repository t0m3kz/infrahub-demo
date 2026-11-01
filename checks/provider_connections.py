"""Validate the provider connections: who owns them, what they ride, what they route (rules PC1 to PC13).

A ``TopologyProviderConnection`` says "this facility of ours is connected to that provider facility": its carriers
(dedicated) or the internet (VPN), and the sessions routed on it. The schema holds the shape; what it cannot hold is
here. Targetless: a carrier circuit and a session circuit are judged against every connection.

PC1  a session circuit (handoff, vpn) that lands in a provider-hosted place (cloud region, SaaS region, virtual
     colocation) belongs to a connection
PC2  a carrier circuit that ends in a provider-hosted place belongs to at least one connection (a cross-connect may
     carry several); a carrier between two zones of ours is no provider connection
PC3  the (provider profile, path type) pair has a flavour, a dedicated connection states its VLAN, the sessions are as
     many as the flavour's links, of the circuit type the path type says (dedicated: handoff, internet_vpn: vpn), and
     a connection has at most one of attached_network / attached_gateway (its meaning, private or transit, is derived
     from the attachment)
PC4  the VLAN of a handoff session is the connection's VLAN and both ends are named ``<port>.<vlan>``
PC5  owner: with an ``account`` the connection has no owner of its own (it is the account's); registered prefixes need
     an owner that is a customer and sit in the connection's ip_namespace
PC6  stub consistency: each provider-side device of the sessions runs one BGP process, whose AS is ``provider_as``
     and the same on every stub
PC7  link ranges: a provider-assigned flavour has as many ``link_prefixes`` as sessions, of the flavour's length, in the
     connection's namespace, one on each session; an our-pool flavour has none
PC8  WARNING: a vpn session is model only, no tunnel configuration is rendered for it
PC9  environment class: the namespace's class is the account's environment
PC10 a dci or a tunnel circuit belongs to no connection
PC11 WARNING: the generator's output, ``carrier_circuits``: a dedicated connection with sessions lists at least one
     carrier, and each listed carrier has an end in the near facility (a warning, not an error: the list is written by
     ``add_provider_connection`` after the carrier exists, so it can lag the generator by one run)
PC12 ``near_port``, when set: dedicated, an end of one of the connection's listed carriers; internet_vpn, an end of a
     live internet carrier circuit of the near facility (the port the tunnels leave by); the generator never uses
     another port than the explicit one
PC13 WARNING: the carriers of one ``redundancy_group`` end, on the near side, on fewer distinct devices than there are
     carriers (two legs on one device); every carrier is judged, the near side of a carrier being the ends deployed in
     the near facility of a connection that lists it

``carrier_circuits`` is the OUTPUT of the generator, not a second source of a session's carrier: that stays derived
from the ports (``utils/circuit_underlay.py``).

Also two consistency rules of the cloud models, no removal behind them: a ``CloudNetworkSegment`` is owned by the owner
of its customer footprint, and a vpc ``CloudTransitGatewayAttachment`` lives in the account of its virtual network.

A decommissioned connection or session is exempt (nothing is built for it) from the rules above; what it must no longer
have (PC14) and the debris of deleted connections (PC15) are judged by ``checks/provider_decommission.py``.
"""

from __future__ import annotations

from typing import Any

from checks.base import WarningCheck
from utils.circuit_underlay import DECOMMISSIONED, HANDOFF_TYPE, VPN_TYPE, subinterface_vlan
from utils.data_cleaning import clean_data
from utils.exchange_transit import PROD_TYPE
from utils.ownership import connection_owner, customer_owner
from utils.provider_services import (
    ADDRESS_SOURCE_PROVIDER,
    FLAVOURS,
    PATH_DEDICATED,
    PATH_INTERNET_VPN,
    connection_provider,
    flavour_for,
)

CONNECTION = "TopologyProviderConnection"
SESSION = "TopologySessionCircuit"
CARRIER = "TopologyCarrierCircuit"
# The places a provider hosts: a circuit that lands in one belongs to a provider connection.
PROVIDER_HOSTED = frozenset({"TopologyCloudRegion", "TopologySaasRegion", "TopologyVirtualColocation"})
CONNECTION_SESSIONS = (HANDOFF_TYPE, VPN_TYPE)
SESSION_TYPE_OF = {PATH_DEDICATED: HANDOFF_TYPE, PATH_INTERNET_VPN: VPN_TYPE}
# CloudAccount.environment p is production, every other value is non-production.
PROD_ACCOUNT_ENVIRONMENT = "p"


def _ref(value: Any) -> str | None:
    """The id of a cleaned reference: clean_data collapses an object with only an ``id`` to the bare id."""
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value else None


def _ids(values: Any) -> set[str]:
    """The ids of a cleaned list of references."""
    return {ref for ref in (_ref(value) for value in values or []) if ref}


def _name(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "<unnamed>")


def _dicts(values: Any) -> list[dict[str, Any]]:
    return [value for value in values or [] if isinstance(value, dict)]


def _carrier_ends(carrier: dict[str, Any]) -> list[dict[str, Any]]:
    """The end ports of a carrier circuit (``capabilities``)."""
    return _dicts(carrier.get("capabilities"))


def _near_ends(carrier: dict[str, Any], near_id: str) -> list[dict[str, Any]]:
    """The ends of a carrier whose device is deployed in the facility ``near_id``."""
    return [end for end in _carrier_ends(carrier) if _ref((end.get("device") or {}).get("deployment")) == near_id]


def _live(node: dict[str, Any]) -> bool:
    return node.get("status") != DECOMMISSIONED


def _owner_key(owner: Any) -> str | None:
    """The key of a customer owner, None for no owner or an owner that is no customer."""
    return customer_owner(owner)


def _profile(connection: dict[str, Any]) -> str | None:
    """The connection profile of the connection's provider (``utils/provider_services.py`` ``connection_provider``)."""
    provider = connection_provider(connection)
    return provider["connection_profile"] if provider else None


def _hosted_places(session: dict[str, Any]) -> list[dict[str, Any]]:
    """The provider-hosted places a session circuit lands in: its locations and the deployments of its end devices."""
    places: dict[str, dict[str, Any]] = {}
    for location in _dicts(session.get("locations")):
        places.setdefault(str(location.get("id")), location)
    for port in _dicts(session.get("capabilities")):
        deployment = (port.get("device") or {}).get("deployment")
        if isinstance(deployment, dict):
            places.setdefault(str(deployment.get("id")), deployment)
    return [place for place in places.values() if place.get("typename") in PROVIDER_HOSTED]


def _far_ports(session: dict[str, Any], near_id: str | None) -> list[dict[str, Any]]:
    """The end ports of a session whose device is not deployed in the connection's near facility: the provider's."""
    return [
        port
        for port in _dicts(session.get("capabilities"))
        if _ref((port.get("device") or {}).get("deployment")) != near_id
    ]


class CheckProviderConnections(WarningCheck):
    """Provider connections own their circuits, sessions and addressing (rules PC1 to PC13)."""

    query = "provider_connections"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        connections = _dicts(cleaned.get(CONNECTION))
        sessions = _dicts(cleaned.get(SESSION))
        by_connection: dict[str, list[dict[str, Any]]] = {}
        for session in sessions:
            connection_id = _ref(session.get("connection"))
            if connection_id:
                by_connection.setdefault(connection_id, []).append(session)

        self._carriers = _dicts(cleaned.get(CARRIER))
        self._validate_sessions(sessions)
        self._validate_carriers(self._carriers)
        live_connections = [c for c in connections if _live(c)]
        for connection in live_connections:
            live_sessions = [s for s in by_connection.get(str(connection.get("id")), []) if _live(s)]
            self._validate_connection(connection, live_sessions)
        self._validate_redundancy(live_connections, _dicts(cleaned.get(CARRIER)))
        self._validate_cloud_segments(_dicts(cleaned.get("CloudNetworkSegment")))
        self._validate_attachments(_dicts(cleaned.get("CloudTransitGatewayAttachment")))

    # ------------------------------------------------------------------
    # PC1, PC2 and the session types (PC10): every circuit
    # ------------------------------------------------------------------

    def _validate_sessions(self, sessions: list[dict[str, Any]]) -> None:
        for session in sessions:
            if not _live(session):
                continue
            circuit_type = session.get("circuit_type")
            connected = _ref(session.get("connection")) is not None
            if circuit_type in CONNECTION_SESSIONS:
                hosted = _hosted_places(session)
                if hosted and not connected:
                    self._error(
                        session,
                        SESSION,
                        f"PC1: {circuit_type} circuit '{_name(session)}' lands in "
                        f"{', '.join(_name(place) for place in hosted)} (provider-hosted) but belongs to no provider "
                        "connection: a session towards a provider is routed on exactly one TopologyProviderConnection.",
                    )
            elif connected:
                self._error(
                    session,
                    SESSION,
                    f"PC10: {circuit_type} circuit '{_name(session)}' belongs to provider connection "
                    f"'{_name(session['connection'])}': only a {HANDOFF_TYPE} or a {VPN_TYPE} circuit is routed on a "
                    "provider connection.",
                )

    def _validate_carriers(self, carriers: list[dict[str, Any]]) -> None:
        for carrier in carriers:
            hosted = [place for place in _dicts(carrier.get("locations")) if place.get("typename") in PROVIDER_HOSTED]
            if hosted and not _ids(carrier.get("provider_connections")):
                self._error(
                    carrier,
                    CARRIER,
                    f"PC2: carrier circuit '{_name(carrier)}' ends in {', '.join(_name(place) for place in hosted)} "
                    "(provider-hosted) but belongs to no provider connection: list it in the carrier_circuits of the "
                    "connection it carries (a cross-connect may carry several).",
                )

    # ------------------------------------------------------------------
    # One connection
    # ------------------------------------------------------------------

    def _validate_connection(self, connection: dict[str, Any], sessions: list[dict[str, Any]]) -> None:
        self._validate_flavour(connection, sessions)
        self._validate_vlan(connection, sessions)
        self._validate_owner(connection)
        self._validate_stubs(connection, sessions)
        self._validate_link_prefixes(connection, sessions)
        self._validate_vpn_unrendered(connection, sessions)
        self._validate_environment(connection)
        self._validate_carriers_output(connection, sessions)

    def _validate_flavour(self, connection: dict[str, Any], sessions: list[dict[str, Any]]) -> None:
        """PC3."""
        name = _name(connection)
        path_type = connection.get("path_type")
        profile = _profile(connection)
        flavour = flavour_for(profile, path_type)
        if profile and (profile, path_type) not in FLAVOURS:
            self._error(
                connection,
                CONNECTION,
                f"PC3: connection '{name}' is {path_type} on a provider with profile '{profile}', which has no "
                f"{path_type} flavour (utils/provider_services.py FLAVOURS): "
                f"{', '.join(sorted(p for (pr, p) in FLAVOURS if pr == profile)) or 'none'} only.",
            )
        if path_type == PATH_DEDICATED and connection.get("vlan_id") is None:
            self._error(
                connection,
                CONNECTION,
                f"PC3: dedicated connection '{name}' has no vlan_id: its handoff sub-interfaces are <port>.<vlan>.",
            )
        if _ref(connection.get("attached_gateway")) and _ref(connection.get("attached_network")):
            self._error(
                connection,
                CONNECTION,
                f"PC3: connection '{name}' attaches to both a virtual network and a transit gateway: it has at most "
                "one of attached_network (private) and attached_gateway (transit).",
            )
        wanted = SESSION_TYPE_OF.get(path_type or "")
        for session in sessions:
            if wanted and session.get("circuit_type") != wanted:
                self._error(
                    session,
                    SESSION,
                    f"PC3: session '{_name(session)}' is a {session.get('circuit_type')} circuit but connection "
                    f"'{name}' is {path_type}: its sessions are {wanted} circuits.",
                )
        if len(sessions) != flavour.links:
            self._error(
                connection,
                CONNECTION,
                f"PC3: connection '{name}' has {len(sessions)} session circuit(s) but its flavour "
                f"({profile or 'default'}, {path_type}) carries {flavour.links}.",
            )

    def _validate_vlan(self, connection: dict[str, Any], sessions: list[dict[str, Any]]) -> None:
        """PC4: a handoff session is on the connection's VLAN, both ends named ``<port>.<vlan>``."""
        vlan = connection.get("vlan_id")
        for session in sessions:
            if session.get("circuit_type") != HANDOFF_TYPE:
                continue
            for port in _dicts(session.get("capabilities")):
                end_vlan = subinterface_vlan(str(port.get("name") or ""))
                label = f"{(port.get('device') or {}).get('name')}:{port.get('name')}"
                if end_vlan is None:
                    self._error(
                        session,
                        SESSION,
                        f"PC4: session '{_name(session)}' end '{label}' is not named <port>.<vlan>.",
                    )
                elif vlan is not None and end_vlan != vlan:
                    self._error(
                        session,
                        SESSION,
                        f"PC4: session '{_name(session)}' end '{label}' is on VLAN {end_vlan} but connection "
                        f"'{_name(connection)}' is on VLAN {vlan}.",
                    )

    def _validate_owner(self, connection: dict[str, Any]) -> None:
        """PC5."""
        name = _name(connection)
        account = connection.get("account") if isinstance(connection.get("account"), dict) else None
        own = connection.get("owner") if isinstance(connection.get("owner"), dict) else None
        if account and own:
            self._error(
                connection,
                CONNECTION,
                f"PC5: connection '{name}' has an account and an owner of its own: with an account the owner is "
                f"derived from it ('{_name(account)}'), leave owner empty.",
            )
        prefixes = _dicts(connection.get("registered_prefixes"))
        if not prefixes:
            return
        owner = connection_owner(_owner_key(own), _owner_key(account.get("owner")) if account else None)
        if not owner:
            self._error(
                connection,
                CONNECTION,
                f"PC5: connection '{name}' registers prefixes but has no customer owner (its owner, or its account's "
                "owner): the owner of a registered prefix is the object that registers it.",
            )
        namespace = (connection.get("ip_namespace") or {}).get("name")
        for prefix in prefixes:
            prefix_namespace = (prefix.get("ip_namespace") or {}).get("name")
            if prefix_namespace != namespace:
                self._error(
                    connection,
                    CONNECTION,
                    f"PC5: connection '{name}' registers {(prefix.get('prefix'))} of namespace '{prefix_namespace}' "
                    f"but its ip_namespace is '{namespace}'.",
                )

    def _validate_stubs(self, connection: dict[str, Any], sessions: list[dict[str, Any]]) -> None:
        """PC6: the provider-side devices run one BGP process each, in the provider's AS."""
        name = _name(connection)
        near_id = _ref(connection.get("near"))
        provider_as = (connection.get("provider_as") or {}).get("asn") if connection.get("provider_as") else None
        stubs: dict[str, dict[str, Any]] = {}
        for session in sessions:
            for port in _far_ports(session, near_id):
                device = port.get("device") or {}
                if device.get("id"):
                    stubs.setdefault(str(device["id"]), device)
        seen_asns: set[Any] = set()
        for device in stubs.values():
            processes = [c for c in _dicts(device.get("capabilities")) if c.get("typename") == "ManagedBGP"]
            if len(processes) != 1:
                self._error(
                    connection,
                    CONNECTION,
                    f"PC6: provider-side device '{device.get('name')}' of connection '{name}' has {len(processes)} "
                    "BGP process(es): a stub runs exactly one.",
                )
                continue
            asn = (processes[0].get("local_as") or {}).get("asn")
            seen_asns.add(asn)
            if provider_as is not None and asn != provider_as:
                self._error(
                    connection,
                    CONNECTION,
                    f"PC6: provider-side device '{device.get('name')}' runs BGP in AS {asn} but connection '{name}' "
                    f"states provider_as {provider_as}.",
                )
        if provider_as is None and len(seen_asns) > 1:
            self._error(
                connection,
                CONNECTION,
                f"PC6: the provider-side devices of connection '{name}' run BGP in different ASes "
                f"({', '.join(sorted(str(asn) for asn in seen_asns))}): the provider speaks with one.",
            )

    def _validate_link_prefixes(self, connection: dict[str, Any], sessions: list[dict[str, Any]]) -> None:
        """PC7."""
        name = _name(connection)
        flavour = flavour_for(_profile(connection), connection.get("path_type"))
        link_prefixes = _dicts(connection.get("link_prefixes"))
        if flavour.address_source != ADDRESS_SOURCE_PROVIDER:
            if link_prefixes or any(session.get("link_prefix") for session in sessions):
                self._error(
                    connection,
                    CONNECTION,
                    f"PC7: connection '{name}' states link prefixes but its flavour allocates the link addresses from "
                    "our pool: link_prefixes and a session's link_prefix are for provider-assigned ranges only.",
                )
            return
        if len(link_prefixes) != flavour.links:
            self._error(
                connection,
                CONNECTION,
                f"PC7: connection '{name}' lists {len(link_prefixes)} link prefix(es) but its flavour is "
                f"provider-assigned with {flavour.links} link(s): author one /{flavour.prefix_length} per link.",
            )
        namespace = (connection.get("ip_namespace") or {}).get("name")
        for prefix in link_prefixes:
            value = str(prefix.get("prefix") or "")
            if not value.endswith(f"/{flavour.prefix_length}"):
                self._error(
                    connection,
                    CONNECTION,
                    f"PC7: link prefix {value} of connection '{name}' is not a /{flavour.prefix_length} (its flavour).",
                )
            prefix_namespace = (prefix.get("ip_namespace") or {}).get("name")
            if prefix_namespace != namespace:
                self._error(
                    connection,
                    CONNECTION,
                    f"PC7: link prefix {value} is in namespace '{prefix_namespace}' but connection '{name}' is in "
                    f"'{namespace}'.",
                )
        listed = {str(prefix.get("prefix")) for prefix in link_prefixes}
        for session in sessions:
            link = (session.get("link_prefix") or {}).get("prefix")
            if link is None:
                self._error(
                    session,
                    SESSION,
                    f"PC7: session '{_name(session)}' has no link_prefix: connection '{name}' is provider-assigned.",
                )
            elif str(link) not in listed:
                self._error(
                    session,
                    SESSION,
                    f"PC7: session '{_name(session)}' link_prefix {link} is not among the link_prefixes of "
                    f"connection '{name}'.",
                )

    def _validate_vpn_unrendered(self, connection: dict[str, Any], sessions: list[dict[str, Any]]) -> None:
        """PC8: no template renders a tunnel yet."""
        for session in sessions:
            if session.get("circuit_type") == VPN_TYPE:
                self.log_warning(
                    message=f"PC8: vpn session '{_name(session)}' of connection '{_name(connection)}' is unrendered: "
                    "the model records the tunnel, no device configuration is generated for it."
                )

    def _validate_environment(self, connection: dict[str, Any]) -> None:
        """PC9: the namespace class is the account's environment class."""
        account = connection.get("account") if isinstance(connection.get("account"), dict) else None
        namespace = connection.get("ip_namespace") or {}
        namespace_type = namespace.get("namespace_type")
        environment = (account or {}).get("environment")
        if not account or not environment or not namespace_type:
            return
        account_class = PROD_TYPE if environment == PROD_ACCOUNT_ENVIRONMENT else "non_prod"
        if namespace_type in (PROD_TYPE, "non_prod") and namespace_type != account_class:
            self._error(
                connection,
                CONNECTION,
                f"PC9: connection '{_name(connection)}' is in namespace '{namespace.get('name')}' ({namespace_type}) "
                f"but its account '{_name(account)}' is environment '{environment}' ({account_class}): prod and "
                "non-prod never connect.",
            )

    def _validate_carriers_output(self, connection: dict[str, Any], sessions: list[dict[str, Any]]) -> None:
        """PC11 (WARNING) and PC12: what the generator wrote into ``carrier_circuits`` and the explicit near port."""
        name = _name(connection)
        listed = [c for c in _dicts(connection.get("carrier_circuits")) if _live(c)]
        near_id = _ref(connection.get("near"))
        if connection.get("path_type") == PATH_DEDICATED and sessions:
            if not listed:
                self.log_warning(
                    message=f"PC11: dedicated connection '{name}' has sessions but lists no carrier circuit: "
                    "add_provider_connection writes carrier_circuits (it has not run, or the carrier is missing)."
                )
            for carrier in listed:
                if near_id and not _near_ends(carrier, near_id) and near_id not in _ids(carrier.get("locations")):
                    self.log_warning(
                        message=f"PC11: carrier circuit '{_name(carrier)}' of connection '{name}' has no end in its "
                        f"near facility '{_name(connection['near']) if isinstance(connection.get('near'), dict) else near_id}'."
                    )
        near_port = _ref(connection.get("near_port"))
        if near_port and connection.get("path_type") == PATH_INTERNET_VPN:
            # A VPN lists no carrier: its near_port picks, among several internet-fed edges, the internet carrier's
            # port the tunnels leave by.
            internet = [
                carrier
                for carrier in self._carriers
                if carrier.get("circuit_type") == "internet"
                and _live(carrier)
                and near_id in _ids(carrier.get("locations"))
            ]
            if near_port not in {_ref(end) for carrier in internet for end in _carrier_ends(carrier)}:
                self._error(
                    connection,
                    CONNECTION,
                    f"PC12: near_port of VPN connection '{name}' is not an end of an internet carrier circuit of its "
                    "near facility: the tunnels leave by the port of an internet carrier.",
                )
        elif near_port:
            ends = {
                _ref(end) for carrier in _dicts(connection.get("carrier_circuits")) for end in _carrier_ends(carrier)
            }
            if near_port not in ends:
                self._error(
                    connection,
                    CONNECTION,
                    f"PC12: near_port of connection '{name}' is not an end of any of its carrier circuits: the "
                    "generator uses the explicit port or fails, it never picks another.",
                )

    def _validate_redundancy(self, connections: list[dict[str, Any]], carriers: list[dict[str, Any]]) -> None:
        """PC13 (WARNING): two legs of one redundancy group on one near device."""
        near_of: dict[str, set[str]] = {}
        for connection in connections:
            near_id = _ref(connection.get("near"))
            for carrier in _dicts(connection.get("carrier_circuits")):
                if near_id and carrier.get("id"):
                    near_of.setdefault(str(carrier["id"]), set()).add(near_id)
        groups: dict[str, list[dict[str, Any]]] = {}
        for carrier in carriers:
            group = carrier.get("redundancy_group")
            if group and _live(carrier):
                groups.setdefault(str(group), []).append(carrier)
        for group, members in sorted(groups.items()):
            devices: set[str] = set()
            legs = 0
            for carrier in members:
                near_ends = [
                    end
                    for end in _carrier_ends(carrier)
                    if _ref((end.get("device") or {}).get("deployment")) in near_of.get(str(carrier.get("id")), set())
                ]
                if near_ends:
                    legs += 1
                    devices |= {str((end.get("device") or {}).get("name")) for end in near_ends}
            if legs > 1 and len(devices) < legs:
                self.log_warning(
                    message=f"PC13: redundancy group '{group}' has {legs} carrier circuits on the near side but they "
                    f"end on {len(devices)} device(s) ({', '.join(sorted(devices))}): two legs on one device are no "
                    "redundancy."
                )

    # ------------------------------------------------------------------
    # Cloud consistency (no removal behind them)
    # ------------------------------------------------------------------

    def _validate_cloud_segments(self, segments: list[dict[str, Any]]) -> None:
        for segment in segments:
            footprint = segment.get("customer_deployment") or {}
            owner = _ref(segment.get("owner"))
            footprint_owner = _ref(footprint.get("owner")) if isinstance(footprint, dict) else None
            if owner and footprint_owner and owner != footprint_owner:
                self._error(
                    segment,
                    "CloudNetworkSegment",
                    f"Cloud segment '{_name(segment)}' is owned by '{(segment.get('owner') or {}).get('org_id')}' but "
                    f"its footprint '{_name(footprint)}' by '{(footprint.get('owner') or {}).get('org_id')}': a "
                    "segment has the owner of its customer deployment.",
                )

    def _validate_attachments(self, attachments: list[dict[str, Any]]) -> None:
        for attachment in attachments:
            if attachment.get("attachment_type") != "vpc":
                continue
            network = attachment.get("virtual_network") or {}
            account = _ref(network.get("account")) if isinstance(network, dict) else None
            owner_account = _ref(attachment.get("owner_account"))
            if account and owner_account and account != owner_account:
                self._error(
                    attachment,
                    "CloudTransitGatewayAttachment",
                    f"Transit gateway attachment '{_name(attachment)}' (vpc) lives in account "
                    f"'{_name(attachment['owner_account'])}' but its virtual network '{_name(network)}' is in account "
                    f"'{_name(network['account'])}': a vpc attachment is in the account of its network.",
                )

    def _error(self, node: dict[str, Any], kind: str, message: str) -> None:
        self.log_error(message=message, object_id=node.get("id"), object_type=kind)
