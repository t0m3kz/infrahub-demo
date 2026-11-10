"""Validate the cloud side of a connection: gateways, transports, their interfaces and tunnels (R1 to R11).

A transport (``CloudDirectConnect``, ``CloudVPNConnection``) is a device that holds interfaces: the
``CloudPeeringInterface`` that ride on the ports and LAG of the provider's ``TopologyProviderConnect`` (or tunnels). The schema accepts what these rules refuse (measured on 1.11.9): an
attachment with no target or with two, a transport riding on itself, a peering whose ``parent_interface`` (a one-sided
edge) is a port of a device that is no provider connect. Each rule is one method and reports ERRORs.

R1  an attachment has exactly one target, ``virtual_network`` or ``peer_gateway``, and its peer gateway is not its own
R2  a route table belongs to a ``hub`` gateway, and an attachment's route tables belong to the attachment's gateway
R3  ``rides_on``: no loop; a physical transport rides only on a physical one; a logical transport that rides on a
    transport of ours has no ``partner_account``
R4  a service endpoint's segments belong to the endpoint's virtual network; a tunnel key needs an IPsec tunnel
R5  a crypto profile belongs to an IPsec tunnel, and an IPsec tunnel has one (GRE has none, a key only with IPsec)
R6  a VPN tunnel has the provider's ``endpoint_ip`` and is an interface of a VPN connection
R7  retired: that every interface of a transport is on that transport is held twice already, by the server for the
    members of a LAG (``common_parent: device``) and by ``checks/interface_parents.py`` for the ``parent_interface`` of
    a peering (a ``DcimSubInterface``: the port of its own transport or of a provider connect)
R8  a peering that has a ``vlan_id`` rides on a port or LAG (a tunnel needs no parent), the parent of a peering is a
    port or LAG, and the VLANs of the peerings of one parent are distinct
R9  placement: a transport has exactly one of ``region`` and ``saas_region``; with an ``account`` it has no ``owner``,
    without one it needs ``saas_region`` and ``owner``; a peering is an interface of a transport and one of scope
    ``saas`` is on a transport with a ``saas_region``
R10 a public service endpoint (enabled, ``access: public``) has a ``TopologyProviderConnect`` of its provider at its
    place (its ``saas_region``, else the region of its virtual network): nothing but provider and place links them;
    exactly one of them is the public side (attached to THE INTERNET), a private port at the same place has no carrier
R11 that public side has an ``internet`` carrier circuit with an end on THE INTERNET (``TopologyInternet``)
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.circuit_underlay import INTERNET_KIND
from utils.data_cleaning import clean_data

ATTACHMENT = "CloudGatewayAttachment"
ROUTE_TABLE = "CloudGatewayRouteTable"
DIRECT_CONNECT = "CloudDirectConnect"
PEERING = "CloudPeeringInterface"
PORT = "DcimPhysicalInterface"
LAG = "DcimLAGInterface"
ENDPOINT = "CloudServiceEndpoint"
VPN_CONNECTION = "CloudVPNConnection"
HUB = "hub"
PHYSICAL = "physical"
IPSEC = "ipsec"
PROVIDER_CONNECT = "TopologyProviderConnect"
PUBLIC = "public"


def _ref(value: Any) -> str | None:
    """The id of a cleaned reference (clean_data collapses an object with only an ``id`` to the bare id)."""
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value else None


def _name(node: Any) -> str:
    if isinstance(node, dict):
        return str(node.get("name") or node.get("id") or "<unnamed>")
    return str(node or "<unnamed>")


def _dicts(values: Any) -> list[dict[str, Any]]:
    return [value for value in values or [] if isinstance(value, dict)]


def _internet_attached(connect: dict[str, Any]) -> bool:
    """A port of the device is an end of a live ``internet`` carrier whose other end is on THE INTERNET."""
    for port in _dicts(connect.get("interfaces")):
        for circuit in _dicts(port.get("capabilities")):
            if circuit.get("circuit_type") != "internet" or circuit.get("status") == "decommissioned":
                continue
            ends = _dicts(circuit.get("capabilities"))
            if any(isinstance(e.get("device"), dict) and e["device"].get("typename") == INTERNET_KIND for e in ends):
                return True
    return False


class CheckCloudConnection(InfrahubCheck):
    """The cloud side of a connection is consistent where the schema cannot say it (rules R1 to R11)."""

    query = "cloud_connection"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for attachment in _dicts(cleaned.get(ATTACHMENT)):
            self._r1_attachment_target(attachment)
            self._r2_attachment_tables(attachment)
        for table in _dicts(cleaned.get(ROUTE_TABLE)):
            self._r2_table_gateway(table)
        direct = [t for t in _dicts(cleaned.get(DIRECT_CONNECT)) if t.get("id")]
        transports = {str(t["id"]): t for t in [*direct, *_dicts(cleaned.get(VPN_CONNECTION))] if t.get("id")}
        by_id = {str(t["id"]): t for t in direct}
        for transport in transports.values():
            if transport.get("typename") == DIRECT_CONNECT:
                self._r3_transport(transport, by_id)
            self._r9_transport(transport)
        peerings = _dicts(cleaned.get(PEERING))
        for peering in peerings:
            self._r4_tunnel_key(peering)
            self._r5_crypto_profile(peering)
            self._r6_tunnel(peering)
            self._r8_parent(peering)
            self._r9_peering(peering, transports)
        self._r8_vlans(peerings)
        connects = _dicts(cleaned.get(PROVIDER_CONNECT))
        for endpoint in _dicts(cleaned.get(ENDPOINT)):
            self._r4_endpoint_segments(endpoint)
            self._r10_public_endpoint(endpoint, connects)

    def _error(self, node: dict[str, Any], kind: str, message: str) -> None:
        self.log_error(message=message, object_id=node.get("id"), object_type=kind)

    def _r1_attachment_target(self, attachment: dict[str, Any]) -> None:
        targets = [field for field in ("virtual_network", "peer_gateway") if attachment.get(field)]
        label = f"R1: attachment '{_name(attachment)}'"
        if len(targets) != 1:
            found = " and ".join(targets) if targets else "none"
            self._error(
                attachment, ATTACHMENT, f"{label} has {found} as target: exactly one of virtual_network, peer_gateway."
            )
        elif targets == ["peer_gateway"] and _ref(attachment["peer_gateway"]) == _ref(attachment.get("gateway")):
            self._error(attachment, ATTACHMENT, f"{label} has its own gateway as peer_gateway.")

    def _r2_table_gateway(self, table: dict[str, Any]) -> None:
        gateway = table.get("gateway")
        if isinstance(gateway, dict) and gateway.get("gateway_type") != HUB:
            self._error(
                table,
                ROUTE_TABLE,
                f"R2: route table '{_name(table)}' belongs to gateway '{_name(gateway)}' of type "
                f"'{gateway.get('gateway_type')}': only a hub has route tables.",
            )

    def _r2_attachment_tables(self, attachment: dict[str, Any]) -> None:
        gateway = _ref(attachment.get("gateway"))
        tables = [attachment.get("associated_route_table"), *(attachment.get("propagates_to_route_tables") or [])]
        for table in _dicts(tables):
            if gateway and _ref(table.get("gateway")) != gateway:
                self._error(
                    attachment,
                    ATTACHMENT,
                    f"R2: attachment '{_name(attachment)}' uses route table '{_name(table)}' of another gateway "
                    f"than its own '{_name(attachment.get('gateway'))}'.",
                )

    def _r3_transport(self, transport: dict[str, Any], transports: dict[str, dict[str, Any]]) -> None:
        up = transport.get("rides_on")
        if not isinstance(up, dict):
            return
        label = f"R3: transport '{_name(transport)}'"
        chain = [str(transport["id"])]
        node: dict[str, Any] | None = up
        while node is not None and node.get("id"):
            if str(node["id"]) in chain:
                self._error(transport, DIRECT_CONNECT, f"{label} rides on itself (loop through '{_name(node)}').")
                return
            chain.append(str(node["id"]))
            node = transports.get(str(node["id"]), {}).get("rides_on")
        kind = transport.get("connection_type")
        if kind == PHYSICAL and up.get("connection_type") != PHYSICAL:
            self._error(transport, DIRECT_CONNECT, f"{label} is physical but rides on the logical '{_name(up)}'.")
        if kind != PHYSICAL and transport.get("partner_account"):
            self._error(
                transport,
                DIRECT_CONNECT,
                f"{label} is logical on a transport of ours ('{_name(up)}') and also has a partner_account: "
                "a partner's connection rides on the partner's port.",
            )

    def _r10_public_endpoint(self, endpoint: dict[str, Any], connects: list[dict[str, Any]]) -> None:
        """R10 and R11 for one enabled public endpoint: its provider connect, and that device's internet carrier."""
        if endpoint.get("access") != PUBLIC or endpoint.get("status") == "decommissioned":
            return
        place = _ref(endpoint.get("saas_region")) or _ref((endpoint.get("virtual_network") or {}).get("region"))
        provider = _ref(endpoint.get("provider"))
        label = f"R10: public endpoint '{_name(endpoint)}'"
        if place is None:
            self._error(endpoint, ENDPOINT, f"{label} has neither a saas_region nor a virtual network with a region.")
            return
        found = [c for c in connects if _ref(c.get("provider")) == provider and _ref(c.get("location")) == place]
        if not found:
            self._error(
                endpoint,
                ENDPOINT,
                f"{label} has no provider connect device of its provider at its place: a TopologyProviderConnect "
                "with the endpoint's provider and a location equal to its region or saas_region.",
            )
            return
        attached = [c for c in found if _internet_attached(c)]
        if not attached:
            names = ", ".join(sorted(_name(c) for c in found))
            self._error(
                endpoint,
                ENDPOINT,
                f"R11: public endpoint '{_name(endpoint)}': none of its provider connects ({names}) has an internet "
                "carrier circuit to THE INTERNET.",
            )
        elif len(attached) > 1:
            names = ", ".join(sorted(_name(c) for c in attached))
            self._error(
                endpoint,
                ENDPOINT,
                f"{label} has {len(attached)} provider connects attached to THE INTERNET ({names}): exactly one is "
                "the public side.",
            )

    def _r4_endpoint_segments(self, endpoint: dict[str, Any]) -> None:
        network = _ref(endpoint.get("virtual_network"))
        for segment in _dicts(endpoint.get("segments")):
            if network and _ref(segment.get("virtual_network")) != network:
                self._error(
                    endpoint,
                    ENDPOINT,
                    f"R4: service endpoint '{_name(endpoint)}' lists segment '{_name(segment)}' of another virtual "
                    f"network than '{_name(endpoint.get('virtual_network'))}'.",
                )

    def _r4_tunnel_key(self, peering: dict[str, Any]) -> None:
        if peering.get("tunnel_key") and peering.get("tunnel_mode") != IPSEC:
            self._error(
                peering,
                PEERING,
                f"R4: peering '{_name(peering)}' has a tunnel_key but tunnel_mode '{peering.get('tunnel_mode')}': "
                "a tunnel key belongs to an IPsec tunnel.",
            )

    def _r5_crypto_profile(self, peering: dict[str, Any]) -> None:
        if peering.get("crypto_profile") and peering.get("tunnel_mode") != IPSEC:
            self._error(
                peering,
                PEERING,
                f"R5: peering '{_name(peering)}' has a crypto_profile but tunnel_mode '{peering.get('tunnel_mode')}': "
                "a crypto profile belongs to an IPsec tunnel.",
            )
        elif peering.get("tunnel_mode") == IPSEC and not peering.get("crypto_profile"):
            self._error(
                peering,
                PEERING,
                f"R5: peering '{_name(peering)}' is an IPsec tunnel with no crypto_profile: set the IKE/ESP profile.",
            )

    def _r8_parent(self, peering: dict[str, Any]) -> None:
        parent = peering.get("parent_interface")
        label = f"R8: peering '{_name(peering)}'"
        if isinstance(parent, dict):
            if parent.get("typename") not in (PORT, LAG):
                self._error(
                    peering, PEERING, f"{label} rides on a {parent.get('typename')}: a port or a LAG of its transport."
                )
        elif peering.get("vlan_id") is not None:
            self._error(peering, PEERING, f"{label} has vlan_id {peering['vlan_id']} but no parent_interface.")

    def _r8_vlans(self, peerings: list[dict[str, Any]]) -> None:
        seen: dict[tuple[str, Any], dict[str, Any]] = {}
        for peering in peerings:
            parent = _ref(peering.get("parent_interface"))
            vlan = peering.get("vlan_id")
            if parent is None or vlan is None:
                continue
            first = seen.setdefault((parent, vlan), peering)
            if first is not peering:
                self._error(
                    peering,
                    PEERING,
                    f"R8: peering '{_name(peering)}' has vlan_id {vlan} like '{_name(first)}' on the same parent "
                    f"'{_name(peering.get('parent_interface'))}'.",
                )

    def _r9_transport(self, transport: dict[str, Any]) -> None:
        kind = str(transport.get("typename") or DIRECT_CONNECT)
        label = f"R9: transport '{_name(transport)}'"
        places = [field for field in ("region", "saas_region") if transport.get(field)]
        if len(places) != 1:
            found = " and ".join(places) if places else "none"
            self._error(transport, kind, f"{label} has {found} as place: exactly one of region, saas_region.")
        if transport.get("account"):
            if transport.get("owner"):
                self._error(transport, kind, f"{label} has an account and an owner: the owner is for a SaaS only.")
        elif not (transport.get("saas_region") and transport.get("owner")):
            self._error(transport, kind, f"{label} has no account: it needs a saas_region and an owner.")

    def _r9_peering(self, peering: dict[str, Any], transports: dict[str, dict[str, Any]]) -> None:
        device = peering.get("device")
        transport = transports.get(_ref(device) or "")
        if transport is None:
            kind = device.get("typename") if isinstance(device, dict) else None
            self._error(
                peering,
                PEERING,
                f"R9: peering '{_name(peering)}' is an interface of '{_name(device)}' ({kind}), not of a transport.",
            )
        elif peering.get("scope") == "saas" and not transport.get("saas_region"):
            self._error(
                peering,
                PEERING,
                f"R9: peering '{_name(peering)}' has scope saas but its transport '{_name(transport)}' has no "
                "saas_region.",
            )

    def _r6_tunnel(self, peering: dict[str, Any]) -> None:
        device = peering.get("device")
        # clean_data renames the "__typename" key to "typename"
        kind = device.get("typename") if isinstance(device, dict) else None
        if peering.get("tunnel_index") is None and kind != VPN_CONNECTION:
            return
        label = f"R6: tunnel '{_name(peering)}'"
        if not peering.get("endpoint_ip"):
            self._error(peering, PEERING, f"{label} has no endpoint_ip (the provider's public address).")
        if kind != VPN_CONNECTION:
            self._error(peering, PEERING, f"{label} is an interface of a {kind}, not of a VPN connection.")
