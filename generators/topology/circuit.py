"""Circuit generators: a physical circuit's derived locations and the BGP session a circuit's ``peering_role`` asks for.

PhysicalCircuitGenerator (add_circuit)
    First the circuit's placement: an endpoint device whose ``deployment``
    is itself a facility (TopologyDataCenter, TopologyExternalProviders,
    TopologyCloudRegion) places that end, so the circuit's locations are the
    authored ones plus these derived ones, exactly two distinct. A colocation
    end is never derived (the on-ramp edge deploys to the metro, which has
    several cages) and stays authored. A derived facility that contradicts an
    authored one of the same kind, or anything but two locations, fails the
    run before any write; the missing derived ones are added with one
    RelationshipAdd, so an already complete circuit writes nothing.
    Then ``peering_role: dci`` builds the EVPN Multi-Site DCI session over the
    circuit's two endpoints (capabilities, exactly two by schema): a
    P2P prefix from the DCI pool, both addresses, the two interfaces
    addressed, and one directly connected eBGP peering between the two
    devices' overlay ManagedBGP processes, keyed with the DC fabric's
    ``{fabric}-overlay-key``. ``underlay`` and ``transit`` are accepted but
    not generated yet; ``none`` writes nothing. Every run then fans out
    add_virtual_circuit to the overlay virtual circuits riding the circuit,
    so a status change reaches them.

VirtualCircuitGenerator (add_virtual_circuit)
    ``peering_role: overlay`` on a tunnel link type builds one eBGP peering
    between the two endpoint interfaces (capabilities) over their
    existing addresses, after waiting for the add_circuit runs of the
    physical circuits it rides. It allocates nothing and never writes the
    interfaces: a tunnel port carries several virtual circuits.

Ownership (the run's tracking group, delete_unused_nodes=True): a run owns
what exists only because of its circuit — the DCI prefix, its two addresses
and the peering. The interfaces it addresses come from the device templates
and are written untracked, as are the derived locations it adds to the
circuit (its target, not its output); devices, BGP processes, the fabric key and the
global address families are only referenced. Every precondition fails
(``self.logger.error`` raises) before the first write, so a refused run never
returns after a partial save, which its cleanup would turn into deletions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrahub_sdk.protocols import CoreIPPrefixPool

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..pools import PoolMixin
from ..protocols import RoutingPassword, TopologyPhysicalCircuit, TopologyVirtualCircuit
from ..routing import EVPN_AF, IPV4_UNICAST_AF, IPV6_UNICAST_AF, RoutingMixin, _overlay_key_name

PHYSICAL_CIRCUIT_GENERATOR = "add_circuit"
VIRTUAL_CIRCUIT_GENERATOR = "add_virtual_circuit"

# Roles the schema accepts on a physical circuit that this version does not build.
_NOT_IMPLEMENTED_PHYSICAL_ROLES = frozenset({"underlay", "transit"})

# Overlay sessions are ours to build only across a tunnel we terminate. Every
# other link type is provider-managed (cloud on-ramps, exchange fabrics, MPLS):
# the session there is the provider's, not something this generator builds.
_TUNNEL_LINK_TYPES = frozenset({"sd_wan", "vpn_ipsec", "vpn_ssl", "gre", "geneve"})
_PHYSICAL_BACKED_LINK_TYPES = frozenset(
    {
        "direct_connect_aws",
        "express_route_azure",
        "interconnect_gcp",
        "fast_connect_oracle",
        "equinix_fabric",
        "megaport",
        "packetfabric",
        "mpls_l3vpn",
    }
)

# DCI P2P pools (data/bootstrap/20_dci_pools.yml), by the DC fabric's address
# family: an EVPN Multi-Site tunnel only forms between border gateways whose
# VTEPs share one, so the DCI follows the fabric's underlay_protocol.
_DCI_POOLS: dict[int, tuple[str, int]] = {4: ("DCI-Technical-IPv4", 31), 6: ("DCI-Technical-IPv6", 127)}
_UNICAST_AF: dict[int, tuple[str, str]] = {4: IPV4_UNICAST_AF, 6: IPV6_UNICAST_AF}

# Circuit status -> the status of the interfaces it lands on (templates render
# a non-active interface shut). A decommissioned circuit builds nothing.
_INTERFACE_STATUS_FOR_CIRCUIT = {
    "provisioning": "provisioning",
    "active": "active",
    "maintenance": "maintenance",
    "down": "outage",
}
_DECOMMISSIONED = "decommissioned"
# Interface kinds that carry an ip_address relationship.
_ADDRESSABLE_INTERFACE_KINDS = frozenset({"DcimPhysicalInterface", "DcimVirtualInterface"})

# Device deployments that are themselves the facility a physical circuit end
# terminates on, so that end's location is derived from its device. Not a
# colocation zone (devices deploy to the metro, which has several cages) and
# not an office (generators/topology/sdwan_edge.py authors its circuit's
# locations); those ends stay authored.
_DERIVABLE_FACILITY_KINDS = frozenset({"TopologyDataCenter", "TopologyExternalProviders", "TopologyCloudRegion"})


@dataclass(frozen=True)
class CircuitLocation:
    """A place a circuit terminates on: a circuit location or an endpoint device's deployment."""

    id: str
    kind: str
    label: str

    @classmethod
    def parse(cls, node: Any) -> CircuitLocation | None:
        """From a cleaned node dict (id, typename, display_label); None for an empty slot."""
        if not isinstance(node, dict) or not node.get("id"):
            return None
        return cls(id=node["id"], kind=node.get("typename") or "", label=node.get("display_label") or node["id"])


def circuit_locations(circuit: dict[str, Any]) -> list[CircuitLocation]:
    """The circuit's current (authored or earlier derived) locations, deduplicated by id."""
    locations: dict[str, CircuitLocation] = {}
    for node in circuit.get("locations") or []:
        location = CircuitLocation.parse(node)
        if location is not None:
            locations.setdefault(location.id, location)
    return list(locations.values())


@dataclass(frozen=True)
class CircuitEndpoint:
    """One end of a circuit, parsed from the query's interface selection."""

    interface_id: str
    interface_kind: str
    interface_name: str
    device_id: str
    device_name: str
    address_id: str | None
    # The interface's address with its prefix length ("10.0.0.1/31"), when it has one.
    address: str | None
    # Lower-cased TopologyDataCenter name when the device belongs to a DC
    # fabric (its deployment), which names the fabric's routing objects.
    fabric: str | None
    underlay_protocol: str | None
    # (id, name, process_role) of every ManagedBGP process on the device.
    bgp_processes: tuple[tuple[str, str, str], ...]
    # The device's deployment, whatever its kind; None when it has none.
    deployment: CircuitLocation | None

    @property
    def label(self) -> str:
        return f"{self.device_name}/{self.interface_name}"

    @classmethod
    def parse(cls, interface: dict[str, Any] | None) -> CircuitEndpoint | None:
        """Build an endpoint from a cleaned interface dict; None for an empty slot."""
        if not interface or not interface.get("id"):
            return None
        device = interface.get("device") or {}
        deployment = device.get("deployment") or {}
        is_dc = isinstance(deployment, dict) and deployment.get("typename") == "TopologyDataCenter"
        fabric = (deployment.get("name") or "").lower() if is_dc else ""
        address = interface.get("ip_address") or {}
        return cls(
            interface_id=interface["id"],
            interface_kind=interface.get("typename") or "",
            interface_name=interface.get("name") or "?",
            device_id=device.get("id") or "",
            device_name=device.get("name") or "?",
            address_id=address.get("id") if isinstance(address, dict) else None,
            address=address.get("address") if isinstance(address, dict) else None,
            fabric=fabric or None,
            underlay_protocol=deployment.get("underlay_protocol") if is_dc else None,
            bgp_processes=tuple(
                (cap["id"], cap.get("name") or "", cap.get("process_role") or "")
                for cap in device.get("capabilities") or []
                if isinstance(cap, dict) and cap.get("typename") == "ManagedBGP" and cap.get("id")
            ),
            deployment=CircuitLocation.parse(deployment),
        )

    def bgp_process(self, role: str) -> str | None:
        """Id of the device's single ManagedBGP process of this role, else None."""
        matching = [proc_id for proc_id, _, proc_role in self.bgp_processes if proc_role == role]
        return matching[0] if len(matching) == 1 else None

    def session_process(self) -> str | None:
        """Id of the process an overlay session runs on: the device's only
        ManagedBGP, else its only role=regular one; None when ambiguous."""
        if len(self.bgp_processes) == 1:
            return self.bgp_processes[0][0]
        return self.bgp_process("regular")


def _parse_endpoints(interfaces: list[dict[str, Any]]) -> list[CircuitEndpoint]:
    """Endpoints in query order, deduplicated by interface id."""
    endpoints: list[CircuitEndpoint] = []
    seen: set[str] = set()
    for interface in interfaces:
        endpoint = CircuitEndpoint.parse(interface)
        if endpoint is None or endpoint.interface_id in seen:
            continue
        seen.add(endpoint.interface_id)
        endpoints.append(endpoint)
    return endpoints


def circuit_endpoints(circuit: dict[str, Any]) -> list[CircuitEndpoint]:
    """The circuit's ends, from its one ``capabilities`` edge (physical and virtual circuits alike)."""
    return _parse_endpoints(circuit.get("capabilities") or [])


def endpoint_problem(endpoints: list[CircuitEndpoint]) -> str | None:
    """Why these endpoints cannot carry a session, or None: a session needs
    exactly two interfaces on two different devices."""
    if len(endpoints) != 2:
        return f"exactly 2 endpoint interfaces, found {len(endpoints)}"
    if endpoints[0].device_id == endpoints[1].device_id:
        return f"endpoints on 2 different devices, both are on {endpoints[0].device_name}"
    return None


def order_endpoints(endpoints: list[CircuitEndpoint]) -> tuple[CircuitEndpoint, CircuitEndpoint]:
    """(first, second): a non-DC end before a DC end, then by device name.

    The first end takes the lower P2P address — the facility end of a dark
    fibre the even one, the DC end the odd one.
    """
    first, second = sorted(endpoints, key=lambda e: (e.fabric is not None, e.device_name, e.interface_name))
    return first, second


def plan_locations(
    current: list[CircuitLocation], endpoints: list[CircuitEndpoint]
) -> tuple[list[CircuitLocation], str | None]:
    """(derived locations the circuit still lacks, problem or None).

    An endpoint device whose deployment is a derivable facility places its end.
    A current location of a kind some end derives, but not derived itself
    (DC10 authored while the device sits in DC11), is a contradiction. The
    current and derived locations together must be exactly two distinct ones.
    """
    derived: dict[str, tuple[CircuitLocation, str]] = {}
    for endpoint in endpoints:
        deployment = endpoint.deployment
        if deployment is not None and deployment.kind in _DERIVABLE_FACILITY_KINDS:
            derived.setdefault(deployment.id, (deployment, endpoint.device_name))
    for location in current:
        if location.id in derived:
            continue
        sources = [f"{device} is in {loc.label}" for loc, device in derived.values() if loc.kind == location.kind]
        if sources:
            return [], (
                f"location {location.label} ({location.kind}) contradicts the endpoint devices' deployment: "
                + ", ".join(sources)
            )
    current_ids = {location.id for location in current}
    missing = [location for location, _ in derived.values() if location.id not in current_ids]
    placed = current + missing
    if len(placed) != 2:
        labels = ", ".join(location.label for location in placed) or "none"
        return [], (
            f"needs exactly 2 distinct locations (authored, plus a DC, ISP or cloud region derived from an "
            f"endpoint device's deployment), found {len(placed)}: {labels}"
        )
    return missing, None


class _CircuitGenerator(PoolMixin, RoutingMixin, CommonGenerator):
    """Shared plumbing: one circuit per run, its fabric key, address families."""

    graphql_root_key = ""
    client: Any

    def _single_circuit(self, data: dict[str, Any]) -> dict[str, Any] | None:
        circuits = clean_data(data).get(self.graphql_root_key) or []
        if not circuits:
            self.logger.error(f"No {self.graphql_root_key} data in GraphQL response")
            return None
        return circuits[0]

    async def _overlay_key(self, circuit_name: str, endpoints: list[CircuitEndpoint]) -> Any | None:
        """The DC fabric's pre-loaded overlay key, or None when no end is a DC device.

        Looked up, never written: add_dc owns it. A DC end whose key is missing
        fails the run (logger.error raises) instead of building an
        unauthenticated session. With a DC at both ends the fabric that sorts
        first keys the session.
        """
        fabrics = sorted({e.fabric for e in endpoints if e.fabric})
        if not fabrics:
            return None
        key_name = _overlay_key_name(fabrics[0])
        key = await self.client.get(kind=RoutingPassword, name__value=key_name, raise_when_missing=False)
        if key is None:
            self.logger.error(
                f"{circuit_name}: RoutingPassword '{key_name}' not found — the session is keyed with the "
                f"{fabrics[0]} fabric's overlay key, which add_dc creates; not building an unauthenticated session"
            )
            return None
        return key


class PhysicalCircuitGenerator(CablingMixin, _CircuitGenerator):
    """add_circuit — a TopologyPhysicalCircuit's derived locations and the session its peering_role asks for."""

    graphql_root_key = "TopologyPhysicalCircuit"

    async def generate(self, data: dict[str, Any]) -> None:
        circuit = self._single_circuit(data)
        if circuit is None:
            return
        circuit_name: str = circuit.get("circuit_id") or circuit.get("name") or ""
        if not circuit_name:
            self.logger.error("TopologyPhysicalCircuit missing circuit_id/name")
            return

        role: str = circuit.get("peering_role") or "none"
        status: str = circuit.get("status") or ""
        endpoints = circuit_endpoints(circuit)
        summary = " | ".join(e.label for e in endpoints) or "no endpoints"
        self.logger.info(f"Processing physical circuit {circuit_name} (role={role}, status={status}): {summary}")
        missing_locations, problem = plan_locations(circuit_locations(circuit), endpoints)
        if problem:
            self.logger.error(f"{circuit_name} ({summary}): {problem}")
            return

        if role == "dci":
            await self._build_dci_session(circuit, circuit_name, endpoints)
        elif role in _NOT_IMPLEMENTED_PHYSICAL_ROLES:
            self.logger.info(f"  peering_role '{role}' is not implemented yet — nothing generated for {circuit_name}")
        else:
            problem = endpoint_problem(endpoints)
            if problem:
                self.logger.info(f"  {circuit_name}: a session would need {problem} (peering_role none, no session)")

        await self._add_locations(circuit["id"], circuit_name, missing_locations)
        await self._fan_out_virtual_circuits(circuit["id"], circuit_name)
        self.logger.info(f"Physical circuit {circuit_name} — completed")

    async def _build_dci_session(
        self, circuit: dict[str, Any], circuit_name: str, endpoints: list[CircuitEndpoint]
    ) -> None:
        """P2P from the DCI pool, both interfaces addressed, one eBGP DCI peering."""
        problem = endpoint_problem(endpoints)
        if problem:
            self.logger.error(f"{circuit_name}: peering_role dci needs {problem}")
            return
        status: str = circuit.get("status") or ""
        if status == _DECOMMISSIONED:
            self.logger.info(
                f"  {circuit_name} is decommissioned — no DCI session; cleanup removes the prefix, "
                "addresses and peering an earlier run built"
            )
            return

        first, second = order_endpoints(endpoints)
        dc_ends = sorted((e for e in (first, second) if e.fabric), key=lambda e: e.fabric or "")
        if not dc_ends:
            self.logger.error(
                f"{circuit_name}: peering_role dci needs a DC fabric device at one end "
                f"({first.device_name}, {second.device_name} belong to no TopologyDataCenter)"
            )
            return
        processes: list[str] = []
        for endpoint in (first, second):
            if endpoint.interface_kind not in _ADDRESSABLE_INTERFACE_KINDS:
                self.logger.error(
                    f"{circuit_name}: {endpoint.label} is a {endpoint.interface_kind}, it takes no address"
                )
                return
            process_id = endpoint.bgp_process("overlay")
            if process_id is None:
                self.logger.error(
                    f"{circuit_name}: {endpoint.device_name} has no single overlay ManagedBGP process for the DCI "
                    "session (add_dc/add_pod or add_colocation_metro creates it)"
                )
                return
            processes.append(process_id)
        key = await self._overlay_key(circuit_name, [first, second])
        if key is None:
            return
        family = 4 if dc_ends[0].underlay_protocol == "ipv4" else 6
        pool_name, prefix_length = _DCI_POOLS[family]
        pool = await self.client.get(kind=CoreIPPrefixPool, name__value=pool_name, raise_when_missing=False)
        if pool is None:
            self.logger.error(f"{circuit_name}: DCI pool '{pool_name}' not found (data/bootstrap/20_dci_pools.yml)")
            return
        interfaces: list[Any] = []
        for endpoint in (first, second):
            node = await self.client.get(
                kind=endpoint.interface_kind, id=endpoint.interface_id, raise_when_missing=False
            )
            if node is None:
                self.logger.error(f"{circuit_name}: interface {endpoint.label} not found")
                return
            interfaces.append(node)

        # Every precondition holds — the writes start here.
        address_families = [
            await self.ensure_address_family(*_UNICAST_AF[family]),
            await self.ensure_address_family(*EVPN_AF),
        ]
        prefix = await self.client.allocate_next_ip_prefix(
            resource_pool=pool,
            identifier=f"dci-p2p__{circuit['id']}",
            prefix_length=prefix_length,
            member_type="address",
            data={"role": "technical", "is_pool": True, "description": f"DCI {circuit_name}"},
        )
        if prefix is None:
            self.logger.error(f"{circuit_name}: DCI pool '{pool_name}' returned no prefix — exhausted?")
            return
        # Owned by this circuit. allocate_next_ip_prefix() is a pool mutation,
        # not a save(), so the prefix joins the run's tracking group only here.
        self.client.group_context.related_node_ids.append(prefix.id)
        self.logger.info(f"  DCI prefix {prefix.prefix.value} for {circuit_name}")

        addresses = await self.upsert_p2p_addresses(prefix, description=f"DCI {circuit_name}")
        interface_status = _INTERFACE_STATUS_FOR_CIRCUIT.get(status, "provisioning")
        for endpoint, node, address, peer in (
            (first, interfaces[0], addresses[0], second),
            (second, interfaces[1], addresses[1], first),
        ):
            await self._update_interface(
                node,
                address_id=address.id,
                description=f"DCI {circuit_name} to {peer.device_name} {peer.interface_name}",
                status=interface_status,
            )
            self.logger.info(f"  {endpoint.label} = {address.address.value}")

        await self.upsert_bgp_peering(
            # DCI-<circuit>, never the circuit's own name: peerings and
            # circuits share one name space.
            name=f"DCI-{circuit_name}",
            description=f"EVPN Multi-Site DCI {first.device_name} <-> {second.device_name} over {circuit_name}",
            status="active" if status == "active" else "provisioning",
            session_type="EBGP",
            # dci keeps the session out of the underlay peer group (ttl 1
            # otherwise reads as underlay) and marks its local interface
            # for `evpn multisite dci-tracking` (transforms/helpers/).
            peering_role="dci",
            process_ids=processes,
            address_family_ids=address_families,
            password_id=key.id,
            interface_ids=[first.interface_id, second.interface_id],
        )

    async def _update_interface(self, node: Any, *, address_id: str, description: str, status: str) -> None:
        """Address an endpoint interface, untracked, and only if something changed.

        The interface comes from the device's object template, not from this
        run: tracked, a later run that stopped addressing it would delete the
        port. A plain save() on the fetched node sends only the modified fields.
        """
        changed = False
        if getattr(getattr(node, "ip_address", None), "id", None) != address_id:
            node.ip_address = address_id
            changed = True
        if node.description.value != description:
            node.description.value = description
            changed = True
        if node.status.value != status:
            node.status.value = status
            changed = True
        if changed:
            await node.save(update_group_context=False)

    async def _add_locations(self, circuit_id: str, circuit_name: str, locations: list[CircuitLocation]) -> None:
        """Add the derived locations the circuit lacks: one RelationshipAdd, untracked.

        The circuit is the run's target, not its output (tracked, cleanup
        could delete it), and a RelationshipAdd names only these peers, so it
        never drops an authored location. Written after the session, whose
        preconditions all fail before the first write.
        """
        if not locations:
            return
        node = await self.client.get(kind=TopologyPhysicalCircuit, id=circuit_id)
        await node.add_relationships(relation_to_update="locations", related_nodes=[loc.id for loc in locations])
        self.logger.info(f"  {circuit_name}: derived location(s) {', '.join(loc.label for loc in locations)}")

    async def _fan_out_virtual_circuits(self, circuit_id: str, circuit_name: str) -> None:
        """Re-run add_virtual_circuit for the overlay circuits riding this one.

        Fire-and-forget: each of those runs waits for this run itself
        (wait_for_parent_generator_and_refetch), so it sees what this run wrote.
        """
        overlays = await self.client.filters(
            kind=TopologyVirtualCircuit, physical_circuits__ids=[circuit_id], peering_role__value="overlay"
        )
        overlay_ids = sorted(vc.id for vc in overlays)
        if overlay_ids:
            self.logger.info(f"  {circuit_name} carries {len(overlay_ids)} overlay virtual circuit(s)")
            await self.run_generator(VIRTUAL_CIRCUIT_GENERATOR, overlay_ids, wait=False)


class VirtualCircuitGenerator(_CircuitGenerator):
    """add_virtual_circuit — the overlay session across a tunnel virtual circuit."""

    graphql_root_key = "TopologyVirtualCircuit"

    @staticmethod
    def _infer_transport_mode(link_type: str) -> str:
        if link_type in _TUNNEL_LINK_TYPES:
            return "internet_backed"
        if link_type in _PHYSICAL_BACKED_LINK_TYPES:
            return "physical_backed"
        return "provider_virtual_only"

    async def generate(self, data: dict[str, Any]) -> None:
        circuit = self._single_circuit(data)
        if circuit is None:
            return
        circuit_name: str = circuit.get("name") or ""
        if not circuit_name:
            self.logger.error("TopologyVirtualCircuit missing name")
            return
        self._log_summary(circuit, circuit_name)
        if not self._wants_overlay_session(circuit, circuit_name):
            self.logger.info(f"Virtual circuit {circuit_name} — completed")
            return

        physical = circuit.get("physical_circuits") or []
        if not physical:
            self.logger.error(
                f"{circuit_name}: an overlay session on own infrastructure needs its physical_circuits "
                "(the underlay it rides) — none listed"
            )
            return
        refreshed: dict[str, Any] | None = None
        for underlay in physical:
            refreshed = await self.wait_for_parent_generator_and_refetch(
                PHYSICAL_CIRCUIT_GENERATOR, underlay["id"]
            ) or (refreshed)
        if refreshed is not None:
            circuit = self._single_circuit(refreshed)
            if circuit is None or not self._wants_overlay_session(circuit, circuit_name):
                return

        await self._build_overlay_session(circuit, circuit_name)
        self.logger.info(f"Virtual circuit {circuit_name} — completed")

    def _wants_overlay_session(self, circuit: dict[str, Any], circuit_name: str) -> bool:
        role = circuit.get("peering_role") or "none"
        if role != "overlay":
            self.logger.info(f"  peering_role {role} — no session for {circuit_name}")
            return False
        link_type = circuit.get("link_type") or ""
        if link_type not in _TUNNEL_LINK_TYPES:
            self.logger.warning(
                f"  {circuit_name}: link_type '{link_type}' is provider-managed, not a tunnel of ours — "
                "peering_role overlay ignored, no session"
            )
            return False
        return True

    async def _build_overlay_session(self, circuit: dict[str, Any], circuit_name: str) -> None:
        status: str = circuit.get("status") or ""
        live_underlay = [pc for pc in circuit.get("physical_circuits") or [] if pc.get("status") != _DECOMMISSIONED]
        if status == _DECOMMISSIONED or not live_underlay:
            self.logger.info(
                f"  {circuit_name}: circuit or its whole underlay is decommissioned — no session; "
                "cleanup removes the peering an earlier run built"
            )
            return

        endpoints = circuit_endpoints(circuit)
        problem = endpoint_problem(endpoints)
        if problem:
            self.logger.error(f"{circuit_name}: peering_role overlay needs {problem}")
            return
        first, second = order_endpoints(endpoints)
        processes: list[str] = []
        for endpoint in (first, second):
            if not endpoint.address_id:
                self.logger.error(
                    f"{circuit_name}: {endpoint.label} has no IP address — the overlay session runs over "
                    "the endpoints' existing addresses"
                )
                return
            process_id = endpoint.session_process()
            if process_id is None:
                names = ", ".join(name for _, name, _ in endpoint.bgp_processes) or "none"
                self.logger.error(
                    f"{circuit_name}: {endpoint.device_name} needs one ManagedBGP process for the overlay session "
                    f"(its only one, or its only role=regular one); has: {names}"
                )
                return
            processes.append(process_id)
        key = await self._overlay_key(circuit_name, [first, second])
        if key is None and any(e.fabric for e in (first, second)):
            return

        underlay_active = any(pc.get("status") == "active" for pc in live_underlay)
        # The session rides the endpoints' own addresses, directly connected
        # (ttl 1): it carries that address family's unicast routes.
        family = 6 if ":" in (first.address or "") else 4
        await self.upsert_bgp_peering(
            # <vc>-bgp: peerings and circuits share one name space.
            name=f"{circuit_name}-bgp",
            description=f"Overlay eBGP {first.label} <-> {second.label} over {circuit_name}",
            status="active" if status == "active" and underlay_active else "provisioning",
            session_type="EBGP",
            peering_role="overlay",
            process_ids=processes,
            address_family_ids=[await self.ensure_address_family(*_UNICAST_AF[family])],
            password_id=key.id if key is not None else None,
            interface_ids=[first.interface_id, second.interface_id],
        )

    def _log_summary(self, circuit: dict[str, Any], circuit_name: str) -> None:
        """Log the circuit and warn where its transport and underlay disagree."""
        link_type: str = circuit.get("link_type") or ""
        transport_mode: str = circuit.get("transport_mode") or self._infer_transport_mode(link_type)
        interfaces = circuit.get("capabilities") or []
        physical = circuit.get("physical_circuits") or []
        ends = " | ".join(
            f"{(iface.get('device') or {}).get('name') or '?'}/{iface.get('name', '?')}" for iface in interfaces
        )
        underlay = ", ".join(f"{pc.get('circuit_id') or pc.get('name') or '?'}" for pc in physical) or "none"
        self.logger.info(
            f"Processing virtual circuit {circuit_name} (type={link_type or '?'}, transport={transport_mode}, "
            f"role={circuit.get('peering_role') or 'none'}): {ends or 'no interfaces'}; underlay: {underlay}"
        )
        if len(interfaces) != 2:
            self.logger.warning(f"  Virtual circuit {circuit_name}: expected 2 interfaces, found {len(interfaces)}")
        underlay_types = {(pc.get("circuit_type") or "").lower() for pc in physical}
        if transport_mode == "internet_backed" and physical and "internet" not in underlay_types:
            self.logger.warning(
                f"  Virtual circuit {circuit_name}: internet_backed should ride at least one circuit_type=internet underlay"
            )
        elif transport_mode == "physical_backed" and not physical:
            self.logger.warning(f"  Virtual circuit {circuit_name}: physical_backed without physical underlay")
