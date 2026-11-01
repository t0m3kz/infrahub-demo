"""Validate the cloud side of a connection: gateways, transports, tunnels and service endpoints (R1 to R6).

The schema accepts what these rules refuse (measured on 1.11.9): an attachment with no target or with two, a transport
riding on itself. Each rule is one method and reports ERRORs.

R1  an attachment has exactly one target, ``virtual_network`` or ``peer_gateway``, and its peer gateway is not its own
R2  a route table belongs to a ``hub`` gateway, and an attachment's route tables belong to the attachment's gateway
R3  ``rides_on``: no loop; a physical transport rides only on a physical one; a logical transport that rides on a
    port of ours has no ``partner_account``; a peering names a bundle or a circuit, not a member of a bundle
R4  a service endpoint's segments belong to the endpoint's virtual network; a tunnel key needs an IPsec tunnel
R5  a crypto profile belongs to an IPsec tunnel
R6  a VPN tunnel has the provider's ``endpoint_ip`` and rides on a VPN connection
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

ATTACHMENT = "CloudGatewayAttachment"
ROUTE_TABLE = "CloudGatewayRouteTable"
DIRECT_CONNECT = "CloudDirectConnect"
PEERING = "CloudPeeringInterface"
ENDPOINT = "CloudServiceEndpoint"
VPN_CONNECTION = "CloudVPNConnection"
HUB = "hub"
PHYSICAL = "physical"
IPSEC = "ipsec"


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


class CheckCloudConnection(InfrahubCheck):
    """The cloud side of a connection is consistent where the schema cannot say it (rules R1 to R6)."""

    query = "cloud_connection"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for attachment in _dicts(cleaned.get(ATTACHMENT)):
            self._r1_attachment_target(attachment)
            self._r2_attachment_tables(attachment)
        for table in _dicts(cleaned.get(ROUTE_TABLE)):
            self._r2_table_gateway(table)
        transports = {str(t["id"]): t for t in _dicts(cleaned.get(DIRECT_CONNECT)) if t.get("id")}
        for transport in transports.values():
            self._r3_transport(transport, transports)
        for peering in _dicts(cleaned.get(PEERING)):
            self._r3_peering_transport(peering, transports)
            self._r4_tunnel_key(peering)
            self._r5_crypto_profile(peering)
            self._r6_tunnel(peering)
        for endpoint in _dicts(cleaned.get(ENDPOINT)):
            self._r4_endpoint_segments(endpoint)

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
                f"{label} is logical on a port of ours ('{_name(up)}') and also has a partner_account: "
                "a partner's connection rides on the partner's port.",
            )

    def _r3_peering_transport(self, peering: dict[str, Any], transports: dict[str, dict[str, Any]]) -> None:
        transport = transports.get(_ref(peering.get("transport")) or "")
        if transport and transport.get("connection_type") == PHYSICAL and transport.get("rides_on"):
            self._error(
                peering,
                PEERING,
                f"R3: peering '{_name(peering)}' rides on '{_name(transport)}', a member of "
                f"'{_name(transport['rides_on'])}': a peering names the bundle, not a member.",
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

    def _r6_tunnel(self, peering: dict[str, Any]) -> None:
        transport = peering.get("transport")
        # clean_data renames the "__typename" key to "typename"
        kind = transport.get("typename") if isinstance(transport, dict) else None
        is_tunnel = peering.get("tunnel_index") is not None or kind == VPN_CONNECTION
        if not is_tunnel:
            return
        label = f"R6: tunnel '{_name(peering)}'"
        if not peering.get("endpoint_ip"):
            self._error(peering, PEERING, f"{label} has no endpoint_ip (the provider's public address).")
        if transport and kind != VPN_CONNECTION:
            self._error(peering, PEERING, f"{label} rides on a {kind}, not on a VPN connection.")
