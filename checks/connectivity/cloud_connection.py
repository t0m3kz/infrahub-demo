"""Validate the cloud side of a connection: gateways, transports, interfaces and tunnels (R1-R16).

R1-R11 cover attachment targets, route tables, endpoints, crypto, peerings, placement and public endpoints (R3, the
rides_on chain, is retired: what carries a leg is its carrier circuit or cable and its peering's parent port).
R12-R16 prove each footprint's ``connection``: a transit footprint has the one hub of its region and environment and its
networks are attached to it (R12), a hub route table never mixes owners (R13), a dedicated footprint's own peering and every serving
hub's link reach L1 and our session end (R14, R16, utils/path_trace.py), a vpn footprint's tunnels ride an edge with
internet access (R15). See domain-model.md.
"""

from __future__ import annotations

from typing import Any

from checks.base import WarningCheck
from utils.circuit_underlay import INTERNET_KIND
from utils.data_cleaning import clean_data
from utils.interface_encapsulation import parent_of, vlan_of
from utils.ownership import customer_owner
from utils.path_trace import Trace, session_circuits, trace_gateway, trace_peering
from utils.transit_hub import TRANSIT, hub_candidates, missing_hub_message

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
FOOTPRINT = "TopologyCustomerCloud"
GATEWAY = "CloudGateway"
NETWORK = "CloudVirtualNetwork"
DEDICATED = "dedicated"
VPN = "vpn"
INTERNET_CIRCUIT = "internet"


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


def _attachment_owner(attachment: dict[str, Any]) -> str | None:
    """Who a network attachment's routes belong to: the customer owning its network's account, else that account."""
    network = attachment.get("virtual_network")
    if not isinstance(network, dict):
        return None
    account = network.get("account")
    if not isinstance(account, dict):
        return _ref(account) and f"account {_ref(account)}"
    return customer_owner(account.get("owner")) or f"account {account.get('id')}"


def _transport_account(peering: dict[str, Any]) -> str | None:
    device = peering.get("device")
    return _ref(device.get("account")) if isinstance(device, dict) else None


def _has_internet(device: Any) -> bool:
    """A port of the device is an end of a live internet carrier circuit."""
    if not isinstance(device, dict):
        return False
    return any(
        circuit.get("typename") == "TopologyCarrierCircuit"
        and circuit.get("circuit_type") == INTERNET_CIRCUIT
        and circuit.get("status") != "decommissioned"
        for port in _dicts(device.get("interfaces"))
        for circuit in _dicts(port.get("capabilities"))
    )


def _best(traces: list[Trace]) -> Trace:
    """The trace that goes furthest (a reaching one wins)."""
    return max(traces, key=lambda trace: (trace.ok, len(trace.hops)))


class CheckCloudConnection(WarningCheck):
    """The cloud side of a connection is consistent where the schema cannot say it (rules R1 to R16)."""

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
        for transport in transports.values():
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
        attachments = _dicts(cleaned.get(ATTACHMENT))
        self._r13_route_tables(attachments)
        served: dict[str, dict[str, Any]] = {}
        for footprint in _dicts(cleaned.get(FOOTPRINT)):
            connection = footprint.get("connection") or TRANSIT
            if connection == TRANSIT:
                hub = self._r12_transit(footprint, cleaned, attachments)
                if hub is not None:
                    served.setdefault(str(hub["id"]), hub)
            elif connection == DEDICATED:
                self._r14_dedicated(footprint, peerings)
            elif connection == VPN:
                self._r15_vpn(footprint, peerings)
        for hub in served.values():
            self._r16_hub_link(hub, attachments, peerings)

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
        parent = parent_of(peering)
        if parent and parent.get("typename") not in (PORT, LAG):
            self._error(
                peering,
                PEERING,
                f"R8: peering '{_name(peering)}' rides on a {parent.get('typename')}: a port or a LAG of its transport.",
            )

    def _r8_vlans(self, peerings: list[dict[str, Any]]) -> None:
        seen: dict[tuple[str, Any], dict[str, Any]] = {}
        for peering in peerings:
            parent = _ref(parent_of(peering))
            vlan = vlan_of(peering)
            if parent is None or vlan is None:
                continue
            first = seen.setdefault((parent, vlan), peering)
            if first is not peering:
                self._error(
                    peering,
                    PEERING,
                    f"R8: peering '{_name(peering)}' has vlan_id {vlan} like '{_name(first)}' on the same parent "
                    f"'{_name(parent_of(peering))}'.",
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
        # clean_data renames "__typename" to "typename"
        kind = device.get("typename") if isinstance(device, dict) else None
        if peering.get("tunnel_index") is None and kind != VPN_CONNECTION:
            return
        label = f"R6: tunnel '{_name(peering)}'"
        if not peering.get("endpoint_ip"):
            self._error(peering, PEERING, f"{label} has no endpoint_ip (the provider's public address).")
        if kind != VPN_CONNECTION:
            self._error(peering, PEERING, f"{label} is an interface of a {kind}, not of a VPN connection.")

    # R12-R16: how a footprint reaches us (TopologyCustomerCloud.connection)

    def _r12_transit(
        self, footprint: dict[str, Any], cleaned: dict[str, Any], attachments: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """R12: the one hub of the footprint's region and environment (ERROR), and each network of its account and
        region attached to it with an associated route table (a WARNING: a network no hub reaches, a sandbox, is not
        reachable from us; add_customer_deployment_cloud attaches the networks it finds). Returns the hub.
        """
        gateways = _dicts(cleaned.get(GATEWAY))
        candidates = hub_candidates(footprint, gateways)
        if message := missing_hub_message(footprint, candidates):
            self._error(footprint, FOOTPRINT, f"R12: {message}")
            return None
        hub = next(g for g in gateways if str(g.get("id")) == candidates[0].id)
        account, region = _ref(footprint.get("account")), _ref(footprint.get("parent"))
        for network in _dicts(cleaned.get(NETWORK)):
            if _ref(network.get("account")) != account or _ref(network.get("region")) != region:
                continue
            boarded = [
                a
                for a in attachments
                if _ref(a.get("virtual_network")) == str(network.get("id"))
                and _ref(a.get("gateway")) == str(hub["id"])
                and _ref(a.get("associated_route_table"))
            ]
            if not boarded:
                self.log_warning(
                    f"R12: footprint '{_name(footprint)}' is transit but its virtual network '{_name(network)}' has "
                    f"no attachment on hub '{_name(hub)}' with a route table: it is not reachable from us. Re-run "
                    "add_customer_deployment_cloud for the footprint (it attaches every network of its account and "
                    "region), or attach it by hand."
                )
        return hub

    def _r13_route_tables(self, attachments: list[dict[str, Any]]) -> None:
        """R13: the network attachments associated with a route table are of one owner, and only that owner's networks
        propagate into it (a table associated by one owner that learns another's routes leaks them).
        """
        associated: dict[str, set[str]] = {}
        propagated: dict[str, set[str]] = {}
        tables: dict[str, dict[str, Any]] = {}
        for attachment in attachments:
            owner = _attachment_owner(attachment)
            if owner is None:
                continue
            table = attachment.get("associated_route_table")
            if isinstance(table, dict) and table.get("id"):
                tables.setdefault(str(table["id"]), table)
                associated.setdefault(str(table["id"]), set()).add(owner)
            for each in _dicts(attachment.get("propagates_to_route_tables")):
                if each.get("id"):
                    tables.setdefault(str(each["id"]), each)
                    propagated.setdefault(str(each["id"]), set()).add(owner)
        for table_id, owners in sorted(associated.items()):
            table = tables[table_id]
            if len(owners) > 1:
                self._error(
                    table,
                    ROUTE_TABLE,
                    f"R13: route table '{_name(table)}' is associated with networks of {', '.join(sorted(owners))}: "
                    "two owners on one routing domain reach each other. Give each footprint its own route table "
                    "(a transit footprint gets rt-<footprint> from add_customer_deployment_cloud).",
                )
                continue
            leaked = sorted(propagated.get(table_id, set()) - owners)
            if leaked:
                self._error(
                    table,
                    ROUTE_TABLE,
                    f"R13: route table '{_name(table)}' is associated with networks of {next(iter(owners))} and learns "
                    f"the routes of {', '.join(leaked)}: that owner's networks reach another owner's. Propagate a "
                    "network only into its owner's tables and the hub link's table towards us.",
                )

    def _r14_dedicated(self, footprint: dict[str, Any], peerings: list[dict[str, Any]]) -> None:
        """R14: a dedicated footprint has a peering of its own (a transport of its account) whose path reaches L1 and
        our session end.
        """
        account = _ref(footprint.get("account"))
        own = [p for p in peerings if account and _transport_account(p) == account and p.get("scope") != PUBLIC]
        label = f"R14: footprint '{_name(footprint)}'"
        if not own:
            self._error(
                footprint,
                FOOTPRINT,
                f"{label} is dedicated but no peering of a transport in its account "
                f"'{_name(footprint.get('account'))}' exists: author its Direct Connect / ExpressRoute / Interconnect "
                "with a CloudPeeringInterface, or set its connection to transit or vpn.",
            )
            return
        trace = _best([trace_peering(peering) for peering in own])
        if not trace.ok:
            self._error(
                footprint,
                FOOTPRINT,
                f"{label} is dedicated but its connection does not reach us: {trace.describe()}. Author the missing "
                "hop (the peering's parent, the cable or carrier circuit on the provider port, our sub-interface and "
                "the session circuit).",
            )

    def _r15_vpn(self, footprint: dict[str, Any], peerings: list[dict[str, Any]]) -> None:
        """R15: a vpn footprint has tunnels (peerings of a VPN connection of its account), each ending a session
        circuit on an edge of ours with a live internet carrier circuit.
        """
        account = _ref(footprint.get("account"))
        tunnels = [
            p
            for p in peerings
            if account
            and _transport_account(p) == account
            and isinstance(p.get("device"), dict)
            and p["device"].get("typename") == VPN_CONNECTION
        ]
        label = f"R15: footprint '{_name(footprint)}'"
        if not tunnels:
            self._error(
                footprint,
                FOOTPRINT,
                f"{label} is vpn but its account '{_name(footprint.get('account'))}' has no CloudVPNConnection with "
                "a tunnel: author the VPN connection and its tunnel peerings, or set its connection to transit or "
                "dedicated.",
            )
            return
        for tunnel in tunnels:
            circuits = session_circuits(tunnel)
            ends = [
                end for circuit in circuits for end in _dicts(circuit.get("capabilities")) if _ref(end) != _ref(tunnel)
            ]
            if not ends:
                self._error(
                    tunnel,
                    PEERING,
                    f"{label}: tunnel '{_name(tunnel)}' ends no session circuit on an edge of ours: author its vpn "
                    "session circuit to our tunnel interface.",
                )
            elif not any(_has_internet(end.get("device")) for end in ends):
                devices = ", ".join(sorted({_name(end.get("device")) for end in ends}))
                self._error(
                    tunnel,
                    PEERING,
                    f"{label}: tunnel '{_name(tunnel)}' rides on {devices}, which has no live internet carrier "
                    "circuit: give the edge its internet access (an internet carrier on one of its ports).",
                )

    def _r16_hub_link(
        self, hub: dict[str, Any], attachments: list[dict[str, Any]], peerings: list[dict[str, Any]]
    ) -> None:
        """R16: a hub that serves a transit footprint reaches L1 and our session end through its own link."""
        trace = trace_gateway(hub, attachments, peerings)
        if not trace.ok:
            self._error(
                hub,
                GATEWAY,
                f"R16: hub '{_name(hub)}' serves transit footprints but its link towards us does not reach us: "
                f"{trace.describe()}. Author the hub's link (an attachment to the edge gateway a peering lands on, "
                "the peering's parent port, its cable or carrier circuit, our sub-interface and the session circuit).",
            )
